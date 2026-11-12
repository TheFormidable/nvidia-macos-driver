// h264kat.c: see h264kat.h.
#include "h264kat.h"
#include "h264kat_vectors.h"
#include "dec.h"
#include "blocklinear.h"
#include <pthread.h>
#include <stdlib.h>
#include <string.h>
#include <syslog.h>
#include <time.h>

enum { KAT_LIMIT_MS = 5000 };
static double now_ms(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec * 1e3 + t.tv_nsec / 1e6; }

// The first failure is the one reported. A wrong frame or an engine error does not end its stream: the frames after
// it are still counted, so the line says whether one picture was wrong or all of them.
static bool kat_fail(KatResult *r, int step, unsigned picture)
{
    if (r->step != KAT_PASS) return false;
    r->step = step; r->picture = picture;
    return true;
}

typedef struct {
    const KatVector *v; H264Dec *d;
    uint8_t *y, *uv;                // one frame read back: w x h luma, w x h / 2 interleaved chroma
    uint32_t used;                  // the expected frames seen so far
    unsigned given;                 // frames the decoder gave back
} KatRun;

// the error notifier of a session's channel, if the session got as far as having one
static unsigned kat_chan_error(H264Dec *d) { return d->chan.notifier.cpu ? chan_error(&d->chan) : 0; }

// A frame that is none of the expected ones: for each plane, was it right as read, and if not, are the same bytes an
// expected plane when read in another of the known layouts? This only fills the report. Trying the layouts on the
// surface is what gob_untile_plane is for when the picture is not cropped at its left or top; the test streams are not.
static void kat_diagnose(KatRun *k, int surf, KatFrame f, KatResult *r)
{
    H264Dec *d = k->d; const KatVector *v = k->v;
    r->used_y = d->gobY; r->used_uv = d->gobC;
    r->fits_y = kat_plane_known(v, 0, f.y) >= 0 ? d->gobY : KAT_LAYOUT_NONE;
    r->fits_uv = kat_plane_known(v, 1, f.uv) >= 0 ? d->gobC : KAT_LAYOUT_NONE;
    if (d->x0[surf] || d->y0[surf]) {
        if (r->fits_y == KAT_LAYOUT_NONE) r->fits_y = KAT_LAYOUT_UNTRIED;
        if (r->fits_uv == KAT_LAYOUT_NONE) r->fits_uv = KAT_LAYOUT_UNTRIED;
        return;
    }
    static const int layouts[] = { GOB_TURING, GOB_BLACKWELL8, GOB_BLACKWELL16 };
    for (size_t i = 0; i < sizeof layouts / sizeof layouts[0]; i++) {
        const int l = layouts[i];
        if (r->fits_y == KAT_LAYOUT_NONE && l != d->gobY) {
            gob_untile_plane(l, k->y, v->w, d->luma[surf].cpu, v->w, v->h, d->pitch);
            if (kat_plane_known(v, 0, kat_plane_hash(k->y, v->w, v->w, v->h)) >= 0) r->fits_y = l;
        }
        if (r->fits_uv == KAT_LAYOUT_NONE && l != d->gobC) {
            gob_untile_plane(l, k->uv, v->w, d->chroma[surf].cpu, v->w, v->h / 2, d->pitch);
            if (kat_plane_known(v, 1, kat_plane_hash(k->uv, v->w, v->w, v->h / 2)) >= 0) r->fits_uv = l;
        }
    }
}

// The frames the decoder has completed. The engine's status is that of the picture just decoded, which is the frame
// taken here: the streams hold frame pictures only, and each is queued as soon as it is decoded.
static void kat_take(KatRun *k, KatResult *r)
{
    H264Dec *d = k->d; const KatVector *v = k->v;
    while (h264dec_pop(d)) {
        const int surf = d->cur_surf;
        k->given++; r->pictures++;
        if (d->cur_w != (int)v->w || d->cur_h != (int)v->h) {      // not read: the buffers are the stream's size
            if (kat_fail(r, KAT_SIZE, k->given)) { r->got_w = (unsigned)d->cur_w; r->got_h = (unsigned)d->cur_h; }
            h264dec_release(d, surf);
            continue;
        }
        if ((d->mbs_err || d->err_status) && kat_fail(r, KAT_STATUS, k->given)) { r->mbs_err = d->mbs_err; r->err_status = d->err_status; }
        h264dec_read_nv12(d, surf, k->y, v->w, k->uv, v->w, v->w, v->h);
        const KatFrame f = kat_frame_hash(k->y, v->w, k->uv, v->w, v->w, v->h);
        if (kat_match(v, &k->used, f) >= 0) r->frames++;
        else {
            if (!r->mismatched++) kat_diagnose(k, surf, f, r);
            kat_fail(r, KAT_HASH, k->given);
        }
        h264dec_release(d, surf);
    }
}

// The coded size of the stream's sequence parameter set, which is the size a VideoToolbox session opens the decoder
// with, if the stream is what its table entry says and what the decoder takes (8-bit 4:2:0 frames); false otherwise.
static bool kat_coded_size(const KatVector *v, unsigned *W, unsigned *H)
{
    H264Params *pp = calloc(1, sizeof *pp);
    if (!pp) return false;
    bool ok = false; size_t pos = 0, n = 0; const uint8_t *nal;
    while (!ok && h264_next_annexb(v->bytes, v->len, &pos, &nal, &n)) {
        if ((nal[0] & 31) != H264_NAL_SPS || h264_parse_sps(pp, nal, n)) continue;
        for (int i = 0; i < 32 && !ok; i++) {
            const H264Sps *sp = &pp->sps[i];
            if (!sp->valid) continue;
            *W = (unsigned)(sp->pic_width_in_mbs_minus1 + 1) * 16; *H = (unsigned)(sp->pic_height_in_map_units_minus1 + 1) * 16;
            ok = sp->frame_mbs_only_flag && sp->chroma_format_idc == 1 && !sp->bit_depth_luma_minus8 && !sp->bit_depth_chroma_minus8 &&
                 sp->width == (int)v->w && sp->height == (int)v->h && *W <= 4096 && *H <= 4096;
        }
    }
    free(pp);
    return ok && v->nframes && v->nframes <= KAT_MAX_FRAMES && !((v->w | v->h) & 1);
}

// One stream through one decode session, opened and closed here. late_ms: when (now_ms) the whole run must be over.
static void kat_stream(Rm *rm, const KatVector *v, double late_ms, KatResult *r)
{
    r->stream = v->name; r->sw = v->w; r->sh = v->h; r->want = v->nframes; r->given = 0;
    unsigned W = 0, H = 0;
    if (!kat_coded_size(v, &W, &H)) { kat_fail(r, KAT_BAD_VECTOR, 0); return; }
    KatRun k = { .v = v };
    k.d = calloc(1, sizeof *k.d); k.y = malloc((size_t)v->w * v->h); k.uv = malloc((size_t)v->w * (v->h / 2));
    if (!k.d || !k.y || !k.uv) { kat_fail(r, KAT_NO_MEMORY, 0); free(k.d); free(k.y); free(k.uv); return; }
    if (h264dec_open(k.d, rm, W, H)) {
        if (kat_fail(r, KAT_OPEN, 0)) r->chan_error = kat_chan_error(k.d);
    } else {
        size_t pos = 0, n = 0; const uint8_t *nal; int q = 0; bool late = false;
        while (q >= 0 && h264_next_annexb(v->bytes, v->len, &pos, &nal, &n)) {
            if (now_ms() > late_ms) { late = true; break; }
            q = h264dec_nal(k.d, nal, n);
            kat_take(&k, r);
        }
        if (q >= 0 && !late) { q = h264dec_flush(k.d); kat_take(&k, r); }
        if (late) kat_fail(r, KAT_TIME, 0);
        else if (q < 0) { if (kat_fail(r, KAT_DECODE, k.given + 1)) r->chan_error = kat_chan_error(k.d); }
        else if (k.used != kat_all(v)) kat_fail(r, KAT_COUNT, 0);
    }
    r->given = k.given;
    h264dec_close(k.d);             // also after an open that failed half-way: it frees what was made
    free(k.d); free(k.y); free(k.uv);
}

int h264dec_selftest_streams(Rm *rm, const KatVector *v, unsigned n, unsigned limit_ms, KatResult *r)
{
    const double t0 = now_ms();
    memset(r, 0, sizeof *r);
    r->used_y = r->used_uv = r->fits_y = r->fits_uv = KAT_LAYOUT_UNTRIED;
    r->nvdec = rm_class(rm, RM_CLS_NVDEC); r->channel = rm_class(rm, RM_CLS_CHANNEL);
    if (!r->nvdec || !r->channel || !rm_class(rm, RM_CLS_USERMODE)) kat_fail(r, KAT_NO_CLASS, 0);
    else if (!rm->vaspace && rm_va_init(rm)) kat_fail(r, KAT_NO_VASPACE, 0);
    else
        for (unsigned i = 0; i < n && r->step == KAT_PASS; i++) {
            kat_stream(rm, &v[i], t0 + limit_ms, r);
            if (r->step == KAT_PASS) r->streams++;
        }
    r->ms = (unsigned)(now_ms() - t0);
    return r->step == KAT_PASS ? 0 : -1;
}

int h264dec_selftest(Rm *rm, KatResult *r)
{
    return h264dec_selftest_streams(rm, kat_vectors, (unsigned)(sizeof kat_vectors / sizeof kat_vectors[0]), KAT_LIMIT_MS, r);
}

// ---- once per process ----
static pthread_mutex_t gKatLock = PTHREAD_MUTEX_INITIALIZER;
static int gKatState;               // 0 not run, 1 passed, 2 failed; under gKatLock, as gKatLine
static char gKatLine[H264KAT_LINE];

bool h264kat_proven(const char *card, char *line, size_t n, bool *first)
{
    // Held across the run: a second registration that arrives meanwhile must get the verdict, not a second run.
    pthread_mutex_lock(&gKatLock);
    const bool run = gKatState == 0;
    if (run) {
        KatResult r; Rm rm = { 0 };
        if (rm_open(&rm)) {
            memset(&r, 0, sizeof r);
            r.step = KAT_NO_RM; r.used_y = r.used_uv = r.fits_y = r.fits_uv = KAT_LAYOUT_UNTRIED;
        } else h264dec_selftest(&rm, &r);
        rm_close(&rm);              // also after an open that failed: it closes the connections that were made
        char what[KAT_LINE];
        kat_describe(&r, what, sizeof what);
        snprintf(gKatLine, sizeof gKatLine, "%s %s%s", card, what,
                 r.step == KAT_PASS ? "" : " - libnvencvt registers no decoder in this process");
        gKatState = r.step == KAT_PASS ? 1 : 2;
        syslog(r.step == KAT_PASS ? LOG_NOTICE : LOG_ERR, "NullMoth: nvencvt: %s", gKatLine);
    }
    const bool ok = gKatState == 1;
    if (line && n) snprintf(line, n, "%s", gKatLine);
    if (first) *first = run;
    pthread_mutex_unlock(&gKatLock);
    return ok;
}
