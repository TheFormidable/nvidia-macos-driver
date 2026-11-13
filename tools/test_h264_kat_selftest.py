#!/usr/bin/env python3
"""libnvencvt's H.264 decode self-test, run on a machine without the card.

On an RTX 50 the library registers its H.264 decoder only after the card has decoded the embedded test streams
bit-exactly (vendor/nvenc/src/h264kat.c). That code runs on the card, so a mistake in it would show as a decoder that
never registers on a card that could decode. Here the real self-test and the real decoder (h264kat.c, dec.c,
h264parse.c) are built for this machine around a card the test plays itself: an RM client that hands out plain memory,
and an engine that looks at what the decoder submitted for each picture (the bitstream bytes, the slice offsets, the
picture setup, the surfaces, the references) and writes that picture's planes in a byte layout of the test's choosing,
with the status and the completion a real engine gives. The engine's layouts are written from the model in
tools/test_nvencvt_blackwell.py, not from the library's code.

  * the run passes on a card that answers correctly: with the embedded answers when ffmpeg is here to supply the
    pictures, and with pictures the test makes up (hashed into a table of its own) in any case;
  * each way a card can fail is reported as that failure: planes in another layout (named in the report), a wrong
    picture, an engine error, a picture that never completes, a channel or memory that is refused, a class list
    without a decoder, a run over its time limit;
  * the verdict is found once per process and logged once, and every buffer is given back, after a failure as well.

What this cannot show is what the card does: that is the self-test's own job, on the card.

The NVIDIA headers the decoder compiles against (open-gpu-kernel-modules and open-gpu-doc at the revisions in
vendor/nvenc/SOURCE) are looked for in $NVENCVT_HEADERS, a folder holding ogkm/ and ogd/ as build/build_nvencvt.sh
lays them out, or in ~/nvencvt-headers; with none the test says so and exits 2 (not a pass).

usage: test_h264_kat_selftest.py [repo root]"""
import os, pathlib, platform, re, shutil, subprocess, sys, tempfile

root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1])
V = root / 'vendor/nvenc/src'
fails = 0


def check(name, ok, detail=''):
    global fails
    print(('ok   ' if ok else 'FAIL ') + name + (f' ({detail})' if detail else ''))
    fails += 0 if ok else 1


def function(src, signature):
    i = src.find(signature)
    return None if i < 0 else src[i:src.index('\n}\n', i) + 3]


HARNESS = r'''
#include "h264kat.h"
#include "h264kat_vectors.h"
#include "dec.h"
#include "blocklinear.h"
#include "class/clc56f.h"
#include "clc9b0.h"
#include "nvdec_drv.h"
#include <pthread.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <syslog.h>
#include <time.h>

static int fails;
#define ARM(name, cond) do { const int ok_ = !!(cond); printf("%s  %s\n", ok_ ? "ok  " : "FAIL", name); if (!ok_) fails++; } while (0)
#define NVEC ((unsigned)(sizeof kat_vectors / sizeof kat_vectors[0]))
static int has(const char *line, const char *part) { return strstr(line, part) != NULL; }

// ---- the card: what it is, and what goes wrong on it (one scenario at a time) ----
static struct {
    NvU32 cls[4];                    // its class list: NVENC, NVDEC, channel, usermode
    int layY, layC;                  // the byte layouts its engine writes luma and chroma in
    bool no_rm, no_va, no_chan;      // rm_open, rm_va_init, chan_create are refused
    int no_mem_at;                   // the n-th buffer of a session is refused (0: none)
    unsigned silent_at, error_at, other_at, blank_at;   // picture n of the run, counted from 1: never completes, reports
                                     // an error, comes out as another picture of its stream, comes out grey
    unsigned mbs_err, err_status;    // what error_at reports
    unsigned nap_ms;                 // every picture takes this long
    NvU32 notifier;                  // the channel's error notifier
} card;
// ---- what was seen of the library ----
static struct {
    int rm_open, rm_close, va, chan, chan_gone, bufs, live;   // live: buffers handed out and not given back
    unsigned pictures;               // pictures the engine was given
    int set_object; NvU32 object;    // SET_OBJECT methods, and the class of the last
    int told; char told_what[200];   // what the decoder told the engine that is not true of the picture, and the first
    int logs, log_pri; char log[1200];
} seen;
static unsigned session, holds[DEC_SURFACES];   // the session that last decoded into each surface

// ---- the streams as this library's parser reads them: each picture's bytes as the engine must get them ----
typedef struct { size_t off, len; unsigned slices; NvU32 slice_off[8]; int poc; unsigned rank; } Pic;
static struct { uint8_t *au; Pic pic[KAT_MAX_FRAMES]; unsigned n, W, H, idr; uint8_t *frame[KAT_MAX_FRAMES]; } strm[8];
static void study(unsigned i)
{
    const KatVector *v = &kat_vectors[i];
    static H264Params p; H264PocState ps; H264Slice prev, cur; int have = 0;
    memset(&p, 0, sizeof p); memset(&ps, 0, sizeof ps); memset(&prev, 0, sizeof prev);
    strm[i].au = malloc(v->len + 16);
    size_t pos = 0, n = 0, w = 0; const uint8_t *nal; Pic *pc = NULL;
    while (h264_next_annexb(v->bytes, v->len, &pos, &nal, &n)) {
        const int t = nal[0] & 31;
        if (t == H264_NAL_SPS) h264_parse_sps(&p, nal, n);
        if (t == H264_NAL_PPS) h264_parse_pps(&p, nal, n);
        if ((t != H264_NAL_SLICE && t != H264_NAL_IDR) || h264_parse_slice_header(&p, nal, n, &cur)) continue;
        if (!have || h264_new_picture(&p, &prev, &cur)) {
            if (strm[i].n == KAT_MAX_FRAMES) break;
            const H264Sps *sps = &p.sps[p.pps[cur.pps_id].sps_id]; int top, bot;
            pc = &strm[i].pic[strm[i].n++]; pc->off = w;
            h264_poc(&ps, sps, &cur, &top, &bot); pc->poc = top;
            strm[i].W = (unsigned)(sps->pic_width_in_mbs_minus1 + 1) * 16; strm[i].H = (unsigned)(sps->pic_height_in_map_units_minus1 + 1) * 16;
            strm[i].idr += cur.idr != 0;
        }
        if (pc->slices < 8) pc->slice_off[pc->slices] = (NvU32)pc->len;
        pc->slices++;
        strm[i].au[w++] = 0; strm[i].au[w++] = 0; strm[i].au[w++] = 1; memcpy(strm[i].au + w, nal, n); w += n; pc->len += n + 3;
        prev = cur; have = 1;
    }
    for (unsigned a = 0; a < strm[i].n; a++) for (unsigned b = 0; b < strm[i].n; b++) strm[i].pic[a].rank += strm[i].pic[b].poc < strm[i].pic[a].poc;
}
// stream i's frames in display order: from a file of NV12 frames, or made up
static void pictures(unsigned i, const char *file)
{
    const KatVector *v = &kat_vectors[i]; const size_t fs = (size_t)v->w * v->h * 3 / 2;
    FILE *f = file ? fopen(file, "rb") : NULL;
    uint64_t s = 0x9e3779b97f4a7c15ull * (i + 1);
    for (unsigned k = 0; k < v->nframes; k++) {
        uint8_t *b = strm[i].frame[k] = malloc(fs);
        if (file) { if (!f || fread(b, 1, fs, f) != fs) { printf("FAIL  %s does not hold %u frames of %ux%u\n", file, v->nframes, v->w, v->h); exit(1); } }
        else for (size_t j = 0; j < fs; j++) { s ^= s << 13; s ^= s >> 7; s ^= s << 17; b[j] = (uint8_t)(s >> 32); }
    }
    if (f) fclose(f);
}

// ---- the engine's plane layouts: where each run of bytes of a 64 x 8 GOB lies, as Mesa NIL's copy.rs lists them ----
typedef struct { unsigned off, x, y, n; } GobLine;
static unsigned gob_lines(int layout, GobLine *out)
{
    static const unsigned turing[4][3] = { { 0x00, 0, 0 }, { 0x40, 16, 0 }, { 0x80, 0, 4 }, { 0xc0, 16, 4 } },
                          bw16[4][3] = { { 0x00, 0, 0 }, { 0x80, 16, 0 }, { 0x40, 0, 4 }, { 0xc0, 16, 4 } };
    unsigned k = 0;
    if (layout == GOB_BLACKWELL8) {
        for (unsigned x = 0; x < 8; x++) for (unsigned y = 0; y < 8; y++) out[k++] = (GobLine){ x * 0x40 + y * 8, x * 8, y, 8 };
        return k;
    }
    const unsigned (*g)[3] = layout == GOB_TURING ? turing : bw16;
    for (unsigned i = 0; i < 2; i++) for (unsigned j = 0; j < 4; j++) for (unsigned r = 0; r < 4; r++)
        out[k++] = (GobLine){ i * 0x100 + g[j][0] + r * 0x10, i * 32 + g[j][1], g[j][2] + r, 16 };
    return k;
}
// a plane as an engine writes it: rows of 64 x 16 tiles, each two GOBs one above the other
static void engine_tile(int layout, uint8_t *plane, const uint8_t *lin, size_t width, size_t rows, size_t pitch)
{
    GobLine l[64]; const unsigned nl = gob_lines(layout, l);
    for (size_t ty = 0; ty < (rows + 15) / 16; ty++) for (size_t tx = 0; tx < pitch / 64; tx++) for (size_t g = 0; g < 2; g++) {
        const size_t base = ty * pitch * 16 + tx * 1024 + g * 512;
        for (unsigned i = 0; i < nl; i++) {
            const size_t X = tx * 64 + l[i].x, Y = ty * 16 + g * 8 + l[i].y;
            if (Y >= rows || X >= width) continue;
            memcpy(plane + base + l[i].off, lin + Y * width + X, width - X < l[i].n ? width - X : l[i].n);
        }
    }
}

// ---- the library's own plane layouts: these functions are the text of blocklinear.c (the file as a whole needs macOS
// frameworks); gob_untile_plane is the same loop without its threads ----
@LIFTED@
void gob_untile_plane(int layout, uint8_t *dst, size_t dst_stride, const uint8_t *src, size_t width, size_t rows, size_t pitch)
{
    for (size_t y = 0; y < rows; y++) for (size_t x = 0; x < width; x += 8)
        memcpy(dst + y * dst_stride + x, src + gob_offset(layout, x, y, pitch), width - x < 8 ? width - x : 8);
}

// ---- RM: plain memory ----
static int session_bufs; static NvU64 next_gpu = 0x200000000ull;
int rm_open(Rm *rm) { memset(rm, 0, sizeof *rm); seen.rm_open++; return card.no_rm ? -1 : 0; }
void rm_close(Rm *rm) { (void)rm; seen.rm_close++; }
NvU32 rm_class(Rm *rm, int kind) { (void)rm; return kind >= 0 && kind < 4 ? card.cls[kind] : 0; }
NV_STATUS rm_va_init(Rm *rm) { seen.va++; if (card.no_va) return NV_ERR_GENERIC; rm->vaspace = 0x4e560001; return NV_OK; }
NV_STATUS rm_mem_alloc(Rm *rm, RmMem *m, NvU64 size, NvU64 align, bool sysmem, bool cpuMap, bool gpuMap)
{
    (void)rm; (void)align; (void)cpuMap; (void)gpuMap;
    memset(m, 0, sizeof *m); m->map.fd = -1;
    if (card.no_mem_at && ++session_bufs == card.no_mem_at) return NV_ERR_NO_MEMORY;
    m->size = (size + 0xfff) & ~0xfffull; m->sysmem = sysmem;
    if (!(m->cpu = malloc(m->size))) return NV_ERR_NO_MEMORY;
    memset(m->cpu, 0xee, m->size);                     // not zero: a read outside what the engine wrote must show
    m->gpu = next_gpu; next_gpu += m->size + 0x10000; m->hPhys = 1;
    seen.bufs++; seen.live++;
    return NV_OK;
}
void rm_mem_free(Rm *rm, RmMem *m) { (void)rm; if (m->cpu) { free(m->cpu); seen.live--; } memset(m, 0, sizeof *m); }

// ---- the channel: the methods of one submission, then the engine ----
static struct { NvU32 mthd; int count; NvU32 a[3]; } mth[128]; static int nmth;
static int method(NvU32 m, NvU32 a[3])                  // the arguments of method m in this submission; 0 when it is absent
{
    for (int i = nmth - 1; i >= 0; i--) if (mth[i].mthd == m) { memcpy(a, mth[i].a, sizeof mth[i].a); return mth[i].count; }
    return 0;
}
NV_STATUS chan_create(Rm *rm, Chan *c, NvU32 engineType, NvU32 objClass, void *objParams)
{
    (void)engineType; (void)objClass; (void)objParams;
    memset(c, 0, sizeof *c); c->rm = rm; session_bufs = 0; session++; nmth = 0; seen.chan++;
    if (card.no_chan) return NV_ERR_GENERIC;
    return rm_mem_alloc(rm, &c->notifier, 0x1000, 0x1000, true, true, true);
}
void chan_destroy(Chan *c) { if (!c->rm) return; seen.chan_gone++; rm_mem_free(c->rm, &c->notifier); memset(c, 0, sizeof *c); }
void chan_mthd(Chan *c, int subch, NvU32 mthd, int count, ...)
{
    (void)c; (void)subch;
    va_list ap; va_start(ap, count);
    if (nmth < 128) { mth[nmth].mthd = mthd; mth[nmth].count = count; for (int i = 0; i < count && i < 3; i++) mth[nmth].a[i] = va_arg(ap, NvU32); nmth++; }
    va_end(ap);
}
bool chan_rewind(Chan *c) { (void)c; return true; }
void chan_done(Chan *c) { (void)c; }
NvU32 chan_error(Chan *c) { (void)c; return card.notifier; }

#define TOLD(cond, what) do { if (!(cond) && !seen.told++) snprintf(seen.told_what, sizeof seen.told_what, "picture %u: %s", seen.pictures, what); } while (0)
static NvU32 hi(NvU64 a) { return (NvU32)(a >> 8); }
static void engine(H264Dec *d)
{
    const nvdec_h264_pic_s *ps = d->setup.cpu; NvU32 a[3], b[3];
    const unsigned n = ++seen.pictures;
    if (method(NVC56F_SET_OBJECT, a)) { seen.set_object++; seen.object = a[0]; }
    TOLD(method(NVC9B0_SET_APPLICATION_ID, a) == 1 && a[0] == NVC9B0_SET_APPLICATION_ID_ID_H264, "the application is not H.264");
    TOLD(method(NVC9B0_SET_DRV_PIC_SETUP_OFFSET, a) == 1 && a[0] == hi(d->setup.gpu), "the picture setup is not at the address given");
    TOLD(method(NVC9B0_SET_IN_BUF_BASE_OFFSET, a) == 1 && a[0] == hi(d->bs.gpu), "the bitstream is not at the address given");
    TOLD(method(NVC9B0_SET_SLICE_OFFSETS_BUF_OFFSET, a) == 1 && a[0] == hi(d->offs.gpu), "the slice offsets are not at the address given");
    TOLD(method(NVC9B0_SET_NVDEC_STATUS_OFFSET, a) == 1 && a[0] == hi(d->status.gpu), "the status is not at the address given");
    TOLD(method(NVC9B0_EXECUTE, a) == 1, "no EXECUTE");
    // which picture this is: by its bytes
    int si = -1, pi = -1;
    for (unsigned i = 0; i < NVEC && si < 0; i++) for (unsigned k = 0; k < strm[i].n; k++)
        if (ps->stream_len == strm[i].pic[k].len && !memcmp(d->bs.cpu, strm[i].au + strm[i].pic[k].off, ps->stream_len)) { si = (int)i; pi = (int)k; break; }
    TOLD(si >= 0, "the bitstream is not one picture of a test stream, slice for slice");
    if (si < 0) return;                                 // an engine given nonsense does not complete
    const Pic *p = &strm[si].pic[pi]; const KatVector *v = &kat_vectors[si];
    TOLD(ps->slice_count == p->slices && !memcmp(d->offs.cpu, p->slice_off, 4 * (p->slices < 8 ? p->slices : 8)), "the slice count or the slice offsets");
    TOLD((unsigned)ps->PicWidthInMbs * 16 == strm[si].W && (unsigned)ps->FrameHeightInMbs * 16 == strm[si].H, "the coded size");
    TOLD(ps->pitch_luma == d->pitch && ps->pitch_chroma == d->pitch && !(d->pitch % 64) && d->pitch >= strm[si].W, "the pitch");
    TOLD(ps->CurrFieldOrderCnt[0] == p->poc, "the picture order count");
    TOLD(ps->frame_mbs_only_flag && !ps->field_pic_flag && ps->chroma_format_idc == 1 && ps->tileFormat == 1, "a 4:2:0 frame picture in tiles");
    const unsigned surf = ps->CurrPicIdx;
    TOLD(surf < DEC_SURFACES && method(NVC9B0_SET_PICTURE_INDEX, a) == 1 && a[0] == surf, "the surface index");
    if (surf >= DEC_SURFACES) return;
    TOLD(method(NVC9B0_SET_PICTURE_LUMA_OFFSET0 + 4 * surf, a) == 1 && a[0] == hi(d->luma[surf].gpu) &&
         method(NVC9B0_SET_PICTURE_CHROMA_OFFSET0 + 4 * surf, b) == 1 && b[0] == hi(d->chroma[surf].gpu), "the surface is not at the addresses given");
    int refs = 0;
    for (int i = 0; i < 16; i++) if (ps->dpb[i].state) {
        refs++;
        TOLD(ps->dpb[i].index != surf && ps->dpb[i].index < DEC_SURFACES && holds[ps->dpb[i].index] == session,
             "a reference is the surface being written, or one this session decoded nothing into");
    }
    TOLD(pi == 0 ? refs == 0 : refs > 0, "the first picture has a reference, or a later one has none");
    // the picture, its status, its completion
    unsigned rank = p->rank;
    if (n == card.other_at) rank = rank ? rank - 1 : rank + 1;
    const uint8_t *fr = strm[si].frame[rank];
    if (n == card.blank_at) { memset(d->luma[surf].cpu, 0x80, d->luma[surf].size); memset(d->chroma[surf].cpu, 0x80, d->chroma[surf].size); }
    else {
        engine_tile(card.layY, d->luma[surf].cpu, fr, v->w, v->h, d->pitch);
        engine_tile(card.layC, d->chroma[surf].cpu, fr + (size_t)v->w * v->h, v->w, v->h / 2, d->pitch);
    }
    holds[surf] = session;
    nvdec_status_s *st = d->status.cpu; memset(st, 0, sizeof *st);
    st->mbs_correctly_decoded = (unsigned)(ps->PicWidthInMbs * ps->FrameHeightInMbs);
    if (n == card.error_at) { st->mbs_in_error = card.mbs_err; st->error_status = card.err_status; }
    if (card.nap_ms) { struct timespec t = { 0, (long)card.nap_ms * 1000000L }; nanosleep(&t, NULL); }
    if (n == card.silent_at) return;
    const int ns = method(NVC9B0_SEMAPHORE_A, a);
    TOLD(ns == 3 && (((NvU64)a[0] << 32) | a[1]) == d->sem.gpu && method(NVC9B0_SEMAPHORE_D, b) == 1, "the completion is not asked for at the session's semaphore");
    if (ns == 3) *(volatile NvU32 *)d->sem.cpu = a[2];
}
NV_STATUS chan_submit(Chan *c) { engine((H264Dec *)((char *)c - offsetof(H264Dec, chan))); nmth = 0; return NV_OK; }

// ---- the system log ----
static void logged(int pri, const char *fmt, va_list ap) { seen.logs++; seen.log_pri = pri; vsnprintf(seen.log, sizeof seen.log, fmt, ap); }
void syslog(int pri, const char *fmt, ...) { va_list ap; va_start(ap, fmt); logged(pri, fmt, ap); va_end(ap); }
#ifdef __GLIBC__
void __syslog_chk(int pri, int flag, const char *fmt, ...);
void __syslog_chk(int pri, int flag, const char *fmt, ...) { (void)flag; va_list ap; va_start(ap, fmt); logged(pri, fmt, ap); va_end(ap); }
#endif

// ---- scenarios ----
static KatVector tab[8]; static KatFrame made[8][KAT_MAX_FRAMES + 1];
static unsigned total;                                   // the frames of all streams
static void blackwell(void)
{
    memset(&card, 0, sizeof card);
    card.cls[RM_CLS_NVENC] = 0xcfb7; card.cls[RM_CLS_NVDEC] = 0xcfb0; card.cls[RM_CLS_CHANNEL] = 0xc96f; card.cls[RM_CLS_USERMODE] = 0xc361;
    card.layY = GOB_BLACKWELL8; card.layC = GOB_BLACKWELL16;
}
static KatResult run_table(const KatVector *t, unsigned n, unsigned limit_ms)
{
    Rm rm; KatResult r; memset(&rm, 0, sizeof rm); memset(&seen, 0, sizeof seen);
    h264dec_selftest_streams(&rm, t, n, limit_ms, &r);
    return r;
}
static KatResult run(void) { return run_table(tab, NVEC, 5000); }
static const char *line_of(const KatResult *r) { static char l[KAT_LINE]; kat_describe(r, l, sizeof l); return l; }
static bool clean(void) { return seen.live == 0 && seen.chan == seen.chan_gone; }   // everything given back

static void scenarios(bool real)
{
    char name[300]; KatResult r;
    const unsigned na = kat_vectors[0].nframes;

    // ---- a card that answers correctly ----
    blackwell(); r = run();
    printf("     %s\n", line_of(&r));
    ARM("a Blackwell card that decodes correctly passes: every stream, every frame, nothing else", r.step == KAT_PASS && r.streams == NVEC &&
        r.frames == total && r.pictures == total && !r.mismatched && r.nvdec == 0xcfb0 && r.channel == 0xc96f && has(line_of(&r), "PASS"));
    if (seen.told) printf("     %s\n", seen.told_what);
    ARM("the engine got each picture's own bytes, slice offsets, size, order count, surface and references", !seen.told && seen.pictures == total);
    ARM("one decode session a stream, the decoder class set on each, and every buffer given back",
        seen.chan == (int)NVEC && seen.set_object == (int)NVEC && seen.object == 0xcfb0 && seen.va == 1 && clean());
    blackwell(); card.cls[RM_CLS_NVENC] = 0xc9b7; card.cls[RM_CLS_NVDEC] = 0xc9b0; card.cls[RM_CLS_CHANNEL] = 0xc56f; card.layY = card.layC = GOB_TURING;
    r = run();
    ARM("the test is not tied to one generation: an Ada class list with Turing planes passes as well", r.step == KAT_PASS && r.frames == total && r.nvdec == 0xc9b0 && !seen.told);

    // ---- planes in another layout ----
    int good = 0, named = 0;
    for (int ly = 0; ly < 3; ly++) for (int lc = 0; lc < 3; lc++) {
        blackwell(); card.layY = ly; card.layC = lc; r = run();
        if (ly == GOB_BLACKWELL8 && lc == GOB_BLACKWELL16) { good += r.step == KAT_PASS; continue; }
        named += r.step == KAT_HASH && r.picture == 1 && r.streams == 0 && r.frames == 0 && r.mismatched == na && r.pictures == na &&
                 r.used_y == GOB_BLACKWELL8 && r.used_uv == GOB_BLACKWELL16 && r.fits_y == ly && r.fits_uv == lc && clean();
        if (ly == GOB_TURING && lc == GOB_TURING) {
            printf("     %s\n", line_of(&r));
            ARM("a card that lays both planes as older cards do: the report says so in words",
                has(line_of(&r), "FAIL at frame hash (stream a 320x180 frame 1: luma (read as Blackwell 8-bit) is wrong and is an expected plane read as Turing, "
                                 "chroma (read as Blackwell 16-bit) is wrong and is an expected plane read as Turing)"));
        }
    }
    ARM("of the nine ways a card can lay the two planes only the one the decoder reads passes; each other fails at the "
        "first frame, with the card's layout of each plane in the result", good == 1 && named == 8);

    // ---- wrong pictures ----
    blackwell(); card.blank_at = 3; r = run();
    ARM("one grey picture: that frame fails, wrong in every known layout, and the others still match", r.step == KAT_HASH && r.picture == 3 &&
        r.frames == na - 1 && r.mismatched == 1 && r.fits_y == KAT_LAYOUT_NONE && r.fits_uv == KAT_LAYOUT_NONE && r.streams == 0 && clean());
    blackwell(); card.other_at = 2; r = run();
    printf("     %s\n", line_of(&r));
    ARM("a picture that comes out as another picture of the stream: a frame is then given twice and one is missing",
        r.step == KAT_HASH && r.frames == na - 1 && r.mismatched == 1 && r.fits_y == r.used_y && r.fits_uv == r.used_uv &&
        has(line_of(&r), "but not of one frame still expected") && clean());

    // ---- the engine's own verdict ----
    blackwell(); card.error_at = 2; card.mbs_err = 3; card.err_status = 0x10; r = run();
    printf("     %s\n", line_of(&r));
    ARM("macroblocks in error fail the run although every frame's bytes matched", r.step == KAT_STATUS && r.picture == 2 && r.mbs_err == 3 &&
        r.err_status == 0x10 && r.frames == na && !r.mismatched && r.streams == 0 && seen.pictures == na && clean());
    blackwell(); card.error_at = na; card.err_status = 1; r = run();
    ARM("an error status alone fails it too", r.step == KAT_STATUS && r.picture == na && !r.mbs_err && r.err_status == 1 && r.frames == na);
    blackwell(); card.error_at = na + 1; card.mbs_err = 1; r = run();
    ARM("and so does an error in the second stream", r.step == KAT_STATUS && r.picture == 1 && r.streams == 1 && r.stream && !strcmp(r.stream, kat_vectors[1].name));

    // ---- a picture that never completes ----
    blackwell(); card.silent_at = 4; card.notifier = 0x1f; r = run();
    printf("     %s\n", line_of(&r));
    ARM("an engine that does not complete a picture: the run ends there, with the channel's error notifier", r.step == KAT_DECODE && r.picture == 4 &&
        r.chan_error == 0x1f && r.frames == 3 && seen.pictures == 4 && r.ms >= 900 && clean());

    // ---- nothing to decode with ----
    blackwell(); card.no_chan = true; r = run();
    ARM("a channel that is refused", r.step == KAT_OPEN && !r.chan_error && r.stream && !r.picture && seen.pictures == 0 && seen.chan == 1 && clean());
    blackwell(); card.no_mem_at = 5; card.notifier = 0x2a; r = run();
    ARM("a decoder buffer that is refused: the session's other buffers are given back", r.step == KAT_OPEN && r.chan_error == 0x2a && seen.bufs == 4 && clean());
    blackwell(); card.no_mem_at = 30; r = run();
    ARM("a surface that is refused", r.step == KAT_OPEN && seen.bufs == 29 && seen.pictures == 0 && clean());
    for (int k = 1; k < 4; k++) {
        blackwell(); card.cls[k] = 0; r = run();
        snprintf(name, sizeof name, "a class list without %s: nothing is opened", k == RM_CLS_NVDEC ? "a decoder" : k == RM_CLS_CHANNEL ? "a channel class" : "the doorbell class");
        ARM(name, r.step == KAT_NO_CLASS && !r.stream && seen.va == 0 && seen.chan == 0 && seen.bufs == 0);
    }
    blackwell(); card.no_va = true; r = run();
    ARM("an address space that is refused", r.step == KAT_NO_VASPACE && seen.va == 1 && seen.chan == 0);

    // ---- time ----
    blackwell(); card.nap_ms = 100; r = run_table(tab, NVEC, 300);
    printf("     %s\n", line_of(&r));
    ARM("a card too slow for the time limit: the run stops between pictures and says so", r.step == KAT_TIME && r.frames >= 1 && r.frames < na &&
        r.frames == seen.pictures && r.ms >= 300 && clean());

    // ---- a stream of more pictures than the decoder has surfaces: the small stream three times over ----
    if (3 * na <= KAT_MAX_FRAMES && 3 * na > DEC_SURFACES) {
        static KatFrame lf[KAT_MAX_FRAMES]; KatVector lv = tab[0];
        uint8_t *lb = malloc(3 * tab[0].len);
        for (unsigned i = 0; i < 3; i++) { memcpy(lb + i * tab[0].len, tab[0].bytes, tab[0].len); memcpy(lf + i * na, tab[0].frames, na * sizeof lf[0]); }
        lv.bytes = lb; lv.len = 3 * tab[0].len; lv.frames = lf; lv.nframes = 3 * na;
        blackwell(); r = run_table(&lv, 1, 5000);
        ARM("more pictures than the decoder has surfaces: each frame is given back before the next is decoded, so none runs out",
            r.step == KAT_PASS && r.frames == 3 * na && seen.pictures == 3 * na && clean());
        free(lb);
    }

    // ---- tables that are not what their streams are ----
    KatVector t2[8]; memcpy(t2, tab, sizeof t2);
    memcpy(made[7], tab[0].frames, na * sizeof(KatFrame)); made[7][na] = (KatFrame){ 1, 2 };
    t2[0].frames = made[7]; t2[0].nframes = na + 1;
    blackwell(); r = run_table(t2, NVEC, 5000);
    printf("     %s\n", line_of(&r));
    ARM("a frame that never comes fails the stream when it ends", r.step == KAT_COUNT && r.given == na && r.want == na + 1 && r.frames == na && r.streams == 0 && clean());
    memcpy(t2, tab, sizeof t2); t2[0].w += 2;
    blackwell(); r = run_table(t2, NVEC, 5000);
    ARM("a table entry whose size is not the stream's is refused before anything is opened", r.step == KAT_BAD_VECTOR && seen.chan == 0);
    memcpy(t2, tab, sizeof t2); t2[0].bytes += 40; t2[0].len -= 40;
    blackwell(); r = run_table(t2, NVEC, 5000);
    ARM("and so is a stream without its parameter sets", r.step == KAT_BAD_VECTOR && seen.chan == 0);
    blackwell(); r = run_table(tab, 0, 5000);
    printf("     %s\n", line_of(&r));
    ARM("no stream at all is a failure, not a pass of nothing, and nothing is opened for it", r.step == KAT_BAD_VECTOR && !r.streams && !r.frames &&
        !r.stream && seen.va == 0 && seen.chan == 0 && seen.bufs == 0 && has(line_of(&r), "FAIL at test vector") && !has(line_of(&r), "PASS"));
    memcpy(t2, tab, sizeof t2); t2[0].nframes = 0;
    blackwell(); r = run_table(t2, 1, 5000);
    ARM("nor is a stream that is said to decode to no frame", r.step == KAT_BAD_VECTOR && !r.streams && seen.chan == 0);
    // a stream whose pictures change size on the way: the large stream with the small one behind it, under the large size
    if (NVEC >= 2 && tab[1].w > tab[0].w && tab[1].h > tab[0].h && tab[1].nframes + na <= KAT_MAX_FRAMES) {
        static KatFrame mf[KAT_MAX_FRAMES]; KatVector mv = tab[1]; const unsigned nb = tab[1].nframes;
        uint8_t *mb = malloc(tab[1].len + tab[0].len);
        memcpy(mb, tab[1].bytes, tab[1].len); memcpy(mb + tab[1].len, tab[0].bytes, tab[0].len);
        memcpy(mf, tab[1].frames, nb * sizeof mf[0]); memcpy(mf + nb, tab[0].frames, na * sizeof mf[0]);
        mv.bytes = mb; mv.len = tab[1].len + tab[0].len; mv.frames = mf; mv.nframes = nb + na;
        blackwell(); r = run_table(&mv, 1, 5000);
        printf("     %s\n", line_of(&r));
        ARM("frames of another size than the table's are not read: the run fails at the first, with the size it had",
            r.step == KAT_SIZE && r.picture == nb + 1 && r.got_w == tab[0].w && r.got_h == tab[0].h && r.frames == nb &&
            r.pictures == nb + na && !r.mismatched && r.streams == 0 && clean());
        free(mb);
    }
    (void)real;
}

// ---- the verdict of a process ----
static struct { bool ok, first; char line[H264KAT_LINE]; } ask[3];
static void *asker(void *p) { const int i = (int)(intptr_t)p; ask[i].ok = h264kat_proven("Blackwell", ask[i].line, sizeof ask[i].line, &ask[i].first); return NULL; }
static void verdict(const char *what, bool real)
{
    blackwell(); memset(&seen, 0, sizeof seen);
    const bool pass = !strcmp(what, "proven-pass");
    if (!strcmp(what, "proven-norm")) card.no_rm = true;
    if (!strcmp(what, "proven-wrong")) card.layY = card.layC = GOB_TURING;
    card.nap_ms = 3;                                     // long enough for the second caller to arrive while the first runs
    pthread_t t[2];
    for (intptr_t i = 0; i < 2; i++) pthread_create(&t[i], NULL, asker, (void *)i);
    for (int i = 0; i < 2; i++) pthread_join(t[i], NULL);
    asker((void *)(intptr_t)2);
    printf("     %s\n", seen.log);
    ARM(pass ? "the process's verdict is a pass, for every caller" : "the process's verdict is a failure, for every caller",
        ask[0].ok == pass && ask[1].ok == pass && ask[2].ok == pass);
    ARM("the test ran once: two callers at the same moment and a later one, one RM client opened and closed",
        ask[0].first + ask[1].first + ask[2].first == 1 && !ask[2].first && seen.rm_open == 1 && seen.rm_close == 1 &&
        seen.pictures == (pass ? total : !strcmp(what, "proven-norm") ? 0 : kat_vectors[0].nframes) && clean());
    ARM("every caller is given the same line", !strcmp(ask[0].line, ask[1].line) && !strcmp(ask[0].line, ask[2].line) && ask[0].line[0]);
    ARM(pass ? "it is logged once, as a notice" : "it is logged once, as an error", seen.logs == 1 && seen.log_pri == (pass ? LOG_NOTICE : LOG_ERR));
    ARM("the logged line is that line behind the driver's prefix", !strncmp(seen.log, "NullMoth: nvencvt: ", 19) && !strcmp(seen.log + 19, ask[0].line));
    if (pass) {
        char want[160]; snprintf(want, sizeof want, "Blackwell H.264 decode self-test: PASS (%u streams, %u frames, ", NVEC, total);
        ARM("a pass names the card, the streams, the frames and the classes", !strncmp(ask[0].line, want, strlen(want)) &&
            has(ask[0].line, " ms) NVDEC 0xcfb0 channel 0xc96f") && !has(ask[0].line, "FAIL") && !has(ask[0].line, "registers no decoder"));
    } else if (!strcmp(what, "proven-norm")) {
        ARM("no RM client: the line says where it stopped and that no decoder registers",
            !strcmp(ask[0].line, "Blackwell H.264 decode self-test: FAIL at RM client (before the first stream) NVDEC 0x0 channel 0x0, "
                                 "0 of 0 frames matched in 0 ms - libnvencvt registers no decoder in this process"));
        ARM("and nothing else was touched", seen.va == 0 && seen.chan == 0 && seen.bufs == 0);
    } else {
        ARM("pictures that are not the streams': the line says which frame and that no decoder registers",
            has(ask[0].line, "Blackwell H.264 decode self-test: FAIL at frame hash (stream a 320x180 frame 1: ") &&
            has(ask[0].line, " - libnvencvt registers no decoder in this process") && !has(ask[0].line, "PASS"));
        if (real) ARM("with the real pictures it names the layout the card used",
                      has(ask[0].line, "luma (read as Blackwell 8-bit) is wrong and is an expected plane read as Turing"));
    }
}

int main(int argc, char **argv)
{
    if (argc < 2) return 2;
    const bool real = argc == 2 + (int)NVEC;             // a file of decoded frames for each stream: the embedded answers apply
    for (unsigned i = 0; i < NVEC; i++) {
        study(i); pictures(i, real ? argv[2 + i] : NULL);
        tab[i] = kat_vectors[i]; total += kat_vectors[i].nframes;
        if (!real) {
            const KatVector *v = &kat_vectors[i];
            for (unsigned k = 0; k < v->nframes; k++) made[i][k] = kat_frame_hash(strm[i].frame[k], v->w, strm[i].frame[k] + (size_t)v->w * v->h, v->w, v->w, v->h);
            tab[i].frames = made[i];
        }
    }
    if (!strcmp(argv[1], "run")) {
        bool one = true, order = false;
        for (unsigned i = 0; i < NVEC; i++) {
            one = one && strm[i].n == kat_vectors[i].nframes && strm[i].idr == 1;
            printf("     %s: frames in the order they are decoded:", kat_vectors[i].name);
            for (unsigned k = 0; k < strm[i].n; k++) { printf(" %u", strm[i].pic[k].rank); order = order || strm[i].pic[k].rank != k; }
            printf("\n");
        }
        ARM("each test stream is one sequence of frame pictures, as many as its table entry has frames", one);
        ARM("a stream is decoded in another order than it is shown (so frames must be matched as a set)", order);
        scenarios(real);
    } else verdict(argv[1], real);
    printf("%s: %d failed\n", fails ? "FAIL" : "PASS", fails);
    return fails ? 1 : 0;
}
'''

# ---- the headers ----
# Looked for, in order: $NVENCVT_HEADERS, the folder build.sh fetches them into ($NULLMOTH_BUILD_ROOT/build/
# nvencvt-headers when that is set, else ~/nullmoth-build/build/nvencvt-headers, build.sh's default working directory),
# and the older ~/nvencvt-headers. A build made with --workdir or NM_WORK elsewhere is not visible from a test, so the
# skip message names the variable to set.
hdr_candidates = [os.environ.get('NVENCVT_HEADERS', '')]
_build_root = os.environ.get('NULLMOTH_BUILD_ROOT')
if _build_root:
    hdr_candidates.append(str(pathlib.Path(_build_root) / 'build/nvencvt-headers'))
hdr_candidates += [str(pathlib.Path.home() / 'nullmoth-build/build/nvencvt-headers'),
                   str(pathlib.Path.home() / 'nvencvt-headers')]
hdr = None
for c in hdr_candidates:
    if c and all((pathlib.Path(c) / p).exists() for p in ('ogkm/src/common/sdk/nvidia/inc/nvtypes.h',
                                                           'ogkm/src/nvidia/arch/nvalloc/unix/include/nv-ioctl.h',
                                                           'ogd/classes/video/nvdec_drv.h')):
        hdr = pathlib.Path(c)
        break
cc = shutil.which('cc') or shutil.which('clang') or shutil.which('gcc')
if not hdr:
    print('SKIP: the NVIDIA headers (ogkm/ and ogd/, see vendor/nvenc/SOURCE) were not found in NVENCVT_HEADERS, '
          '$NULLMOTH_BUILD_ROOT/build/nvencvt-headers or ~/nullmoth-build/build/nvencvt-headers; set NVENCVT_HEADERS, '
          'or NULLMOTH_BUILD_ROOT if the build used --workdir or NM_WORK elsewhere'); sys.exit(2)
if not cc:
    print('SKIP: no C compiler'); sys.exit(2)

bl = (V / 'blocklinear.c').read_text()
lifted = [function(bl, s) for s in ('size_t nvenc_in_offset(', 'static size_t gob_inner(', 'size_t gob_offset(', 'int gob_layout_for(')]
m = re.search(r'(?m)^size_t nvenc_in_plane_size\(.*\}$', bl)
check('the layout functions are where the harness takes them from', all(lifted) and m is not None)
if not all(lifted) or not m:
    sys.exit(1)

with tempfile.TemporaryDirectory(prefix='nmkat-') as d:
    d = pathlib.Path(d)
    (d / 'mock.c').write_text(HARNESS.replace('@LIFTED@', '\n'.join(lifted) + m.group(0) + '\n'))
    inc = ['-I', str(V), '-I', str(hdr / 'ogkm/src/common/sdk/nvidia/inc'), '-I', str(hdr / 'ogkm/src/nvidia/arch/nvalloc/unix/include'),
           '-I', str(hdr / 'ogd/classes/video')]
    if platform.system() != 'Darwin':            # rm.h asks IOKit for one type; nothing here talks to IOKit
        (d / 'stub/IOKit').mkdir(parents=True)
        (d / 'stub/IOKit/IOKitLib.h').write_text('#include <stddef.h>\n#include <stdint.h>\ntypedef unsigned io_connect_t;\n')
        inc += ['-I', str(d / 'stub')]
    # sanitizers where this compiler has their runtime: a read or write outside a buffer is then a failure, not luck
    (d / 'probe.c').write_text('int main(void) { return 0; }\n')
    san = ['-fsanitize=address,undefined', '-fno-sanitize-recover=undefined']
    if subprocess.run([cc, *san, str(d / 'probe.c'), '-o', str(d / 'probe')], capture_output=True).returncode:
        san = []
        print('note: this compiler has no sanitizer runtime; built without')
    flags = ['-O1', '-g', '-Wall', '-Wextra', '-U_FORTIFY_SOURCE', *san, *inc]
    objs, built = [], True
    for f in ('h264kat.c', 'dec.c', 'h264parse.c'):
        r = subprocess.run([cc, *flags, '-c', str(V / f), '-o', str(d / (f + '.o'))], capture_output=True, text=True)
        built = built and r.returncode == 0
        objs.append(str(d / (f + '.o')))
        if f == 'h264kat.c':
            check('the self-test compiles for this machine without a warning (-Wall -Wextra)', r.returncode == 0 and 'warning' not in r.stderr, r.stderr.strip()[-600:])
        elif r.returncode:
            check(f'{f} compiles for this machine', False, r.stderr.strip()[-600:])
    r = subprocess.run([cc, *flags, str(d / 'mock.c'), *objs, '-o', str(d / 'mock'), '-lpthread'], capture_output=True, text=True) if built else None
    check('the self-test, the decoder and the parser link against the card the test plays', bool(r) and r.returncode == 0, (r.stderr.strip()[-900:] if r else ''))
    if r and r.returncode == 0:
        env = {k: v for k, v in os.environ.items() if k != 'NVDEC_GOB'}

        def play(title, *args):
            global fails
            print(f'-- {title}')
            p = subprocess.run([str(d / 'mock'), *map(str, args)], capture_output=True, text=True, env=env, timeout=300)
            lines = [l for l in p.stdout.split('\n') if l.strip()]
            for l in lines:
                if l.startswith(('ok  ', 'FAIL', '     ')) and not l.startswith(('FAIL:', 'PASS:')):
                    print(l)
            bad = sum(l.startswith('FAIL ') for l in lines)
            fails += bad
            done = bool(lines) and lines[-1].startswith(('PASS:', 'FAIL:'))
            check(f'{title}: ran to its end', done and (p.returncode == 0) == (bad == 0), (p.stderr.strip()[-900:] if not done or p.returncode not in (0, 1) else ''))

        play('pictures the test makes up, every scenario', 'run')
        play('a process whose RM client does not open', 'proven-norm')
        play('a process on a card whose pictures are wrong', 'proven-wrong')
        gen = root / 'tools/gen_h264_kat.py'
        if shutil.which('ffmpeg') and gen.exists():
            g = subprocess.run([sys.executable, str(gen), '--from', str(V / 'h264kat_vectors.h'), '--dump', str(d), str(d / 'again.h')],
                               capture_output=True, text=True)
            check('ffmpeg decodes the embedded streams', g.returncode == 0, (g.stderr or g.stdout).strip()[-300:])
            if g.returncode == 0:
                nv12 = [d / f'{n}.nv12' for n in re.findall(r'(?m)^    \{ "(\w+)", ', (V / 'h264kat_vectors.h').read_text())]
                play('the embedded answers with ffmpeg\'s pictures, every scenario', 'run', *nv12)
                play('a process on a card that decodes correctly', 'proven-pass', *nv12)
                play('a process on a card that lays its planes as older cards do', 'proven-wrong', *nv12)
        else:
            print('not run: the arms with the embedded answers need ffmpeg to supply the pictures')

print(f'{fails} failed' if fails else 'all passed')
sys.exit(1 if fails else 0)
