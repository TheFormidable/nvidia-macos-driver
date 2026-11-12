/* test_h264_kat.c - the parts of libnvencvt's H.264 decode self-test that need no GPU (vendor/nvenc/src/h264kat_core.h)
   and the embedded test streams (h264kat_vectors.h), read with the library's own H.264 parser.
   Build and run anywhere:
     cc -std=c11 -Wall -Wextra -Werror -I vendor/nvenc/src tools/test_h264_kat.c vendor/nvenc/src/h264parse.c -o /tmp/test_h264_kat && /tmp/test_h264_kat
   With arguments, one file per stream in table order, each the stream's frames as raw NV12 from a decoder that is not
   this library (tools/test_h264_kat_vectors.py passes ffmpeg's): every frame of every file must be an expected frame,
   each exactly once. That is the check that the embedded answers are the right ones. */
#include "h264kat_core.h"
#include "h264kat_vectors.h"
#include "h264parse.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int fails;
#define ARM(name, cond) do { const int ok_ = !!(cond); printf("%s  %s\n", ok_ ? "ok  " : "FAIL", name); if (!ok_) fails++; } while (0)
#define NVEC (sizeof kat_vectors / sizeof kat_vectors[0])

static int has(const char *line, const char *part) { return strstr(line, part) != NULL; }

/* what the library's parser finds in a stream */
typedef struct { unsigned nals, sps, pps, slices, other, pictures, bad; int w, h, cw, ch, profile, cabac, t8x8, refs, x0, y0, frames_only; } Census;
static Census census(const KatVector *v)
{
    static H264Params p; Census c; H264Slice prev, cur; int have = 0;
    memset(&p, 0, sizeof p); memset(&c, 0, sizeof c); memset(&prev, 0, sizeof prev);
    size_t pos = 0, n = 0; const uint8_t *nal;
    while (h264_next_annexb(v->bytes, v->len, &pos, &nal, &n)) {
        const int t = nal[0] & 31;
        c.nals++;
        if (t == H264_NAL_SPS) {
            c.sps++;
            if (h264_parse_sps(&p, nal, n)) { c.bad++; continue; }
            for (int i = 0; i < 32; i++) if (p.sps[i].valid) {
                const H264Sps *s = &p.sps[i];
                c.w = s->width; c.h = s->height; c.profile = s->profile_idc; c.refs = s->max_num_ref_frames;
                c.cw = (s->pic_width_in_mbs_minus1 + 1) * 16; c.ch = (s->pic_height_in_map_units_minus1 + 1) * 16;
                c.x0 = s->crop_x0; c.y0 = s->crop_y0; c.frames_only = s->frame_mbs_only_flag;
            }
        } else if (t == H264_NAL_PPS) {
            c.pps++;
            if (h264_parse_pps(&p, nal, n)) { c.bad++; continue; }
            for (int i = 0; i < 256; i++) if (p.pps[i].valid) { c.cabac = p.pps[i].entropy_coding_mode_flag; c.t8x8 = p.pps[i].transform_8x8_mode_flag; }
        } else if (t == H264_NAL_SLICE || t == H264_NAL_IDR) {
            c.slices++;
            if (h264_parse_slice_header(&p, nal, n, &cur)) { c.bad++; continue; }
            if (!have || h264_new_picture(&p, &prev, &cur)) c.pictures++;
            prev = cur; have = 1;
        } else c.other++;
    }
    return c;
}

int main(int argc, char **argv)
{
    /* ---- the hash ---- */
    ARM("FNV-1a 64 gives the published values for the empty string, \"a\" and \"foobar\"",
        kat_fnv1a(KAT_FNV0, (const uint8_t *)"", 0) == 0xcbf29ce484222325ull &&
        kat_fnv1a(KAT_FNV0, (const uint8_t *)"a", 1) == 0xaf63dc4c8601ec8cull &&
        kat_fnv1a(KAT_FNV0, (const uint8_t *)"foobar", 6) == 0x85944171f73967e8ull);
    {
        /* a 4 x 2 plane in rows of 7 bytes: the three bytes after each row are not part of the picture */
        uint8_t a[14] = { 1, 2, 3, 4, 9, 9, 9, 5, 6, 7, 8, 9, 9, 9 }, b[8] = { 1, 2, 3, 4, 5, 6, 7, 8 };
        const uint64_t tight = kat_plane_hash(b, 4, 4, 2);
        ARM("a plane's hash is that of its picture bytes, whatever the row stride", kat_plane_hash(a, 7, 4, 2) == tight);
        a[5] ^= 0xff;
        ARM("bytes between rows do not count", kat_plane_hash(a, 7, 4, 2) == tight);
        a[8] ^= 1;
        ARM("control: one bit inside the picture changes the hash", kat_plane_hash(a, 7, 4, 2) != tight);
        uint8_t y[8] = { 1, 2, 3, 4, 5, 6, 7, 8 }, uv[4] = { 10, 20, 30, 40 };
        const KatFrame f = kat_frame_hash(y, 4, uv, 4, 4, 2);
        ARM("a frame is hashed as h rows of luma and h / 2 rows of interleaved chroma, each w bytes",
            f.y == tight && f.uv == kat_fnv1a(KAT_FNV0, uv, 4));
    }

    /* ---- the streams, through the library's parser ---- */
    ARM("there are two streams, a small one with B pictures and a full-size one",
        NVEC == 2 && kat_vectors[0].w == 320 && kat_vectors[0].h == 180 && kat_vectors[1].w == 1920 && kat_vectors[1].h == 1080);
    for (unsigned i = 0; i < NVEC; i++) {
        const KatVector *v = &kat_vectors[i]; const Census c = census(v); char name[200];
        printf("     %s: %ux%u (coded %dx%d), %zu bytes, %u NAL units (%u SPS, %u PPS, %u slices), %u pictures, profile %d, %d reference frames\n",
               v->name, v->w, v->h, c.cw, c.ch, v->len, c.nals, c.sps, c.pps, c.slices, c.pictures, c.profile, c.refs);
        snprintf(name, sizeof name, "%s holds parameter sets and slices and nothing else, and the parser accepts every one", v->name);
        ARM(name, c.sps >= 1 && c.pps >= 1 && c.slices >= v->nframes && !c.other && !c.bad);
        snprintf(name, sizeof name, "%s: the cropped size in its SPS is the table's, nothing is cropped at the left or top, frames only", v->name);
        ARM(name, c.w == (int)v->w && c.h == (int)v->h && !c.x0 && !c.y0 && c.frames_only);
        snprintf(name, sizeof name, "%s: High profile, CABAC and the 8x8 transform", v->name);
        ARM(name, c.profile == 100 && c.cabac && c.t8x8);
        snprintf(name, sizeof name, "%s: the parser finds as many pictures as the table has frames", v->name);
        ARM(name, c.pictures == v->nframes);
        snprintf(name, sizeof name, "%s: an even size, at most %d frames, no two frames the same picture", v->name, KAT_MAX_FRAMES);
        int distinct = 1;
        for (unsigned a = 0; a < v->nframes; a++) for (unsigned b = a + 1; b < v->nframes; b++)
            if (v->frames[a].y == v->frames[b].y && v->frames[a].uv == v->frames[b].uv) distinct = 0;
        ARM(name, !(v->w & 1) && !(v->h & 1) && v->nframes >= 1 && v->nframes <= KAT_MAX_FRAMES && distinct);
    }
    {
        const Census a = census(&kat_vectors[0]), b = census(&kat_vectors[1]);
        ARM("the small stream has two slices a picture and more than one reference frame; its 192 coded rows are cropped to 180",
            a.slices == 2 * a.pictures && a.refs > 1 && a.ch == 192);
        ARM("the full-size stream's 1088 coded rows are cropped to 1080", b.cw == 1920 && b.ch == 1088);
    }

    /* ---- the set match ---- */
    {
        const KatVector *v = &kat_vectors[0]; uint32_t used = 0; int ok = 1;
        for (unsigned i = v->nframes; i-- > 0;) if (kat_match(v, &used, v->frames[i]) != (int)i) ok = 0;
        ARM("every expected frame is matched, in any order, and the set is then complete", ok && used == kat_all(v));
        ARM("control: a frame that comes back a second time matches nothing", kat_match(v, &used, v->frames[0]) == -1);
        used = 0;
        KatFrame f = v->frames[1]; f.y ^= 1;
        ARM("control: a frame with one bit of its luma hash changed matches nothing", kat_match(v, &used, f) == -1 && !used);
        f = v->frames[1]; f.uv = v->frames[2].uv;
        ARM("control: the luma of one frame with the chroma of another matches nothing", kat_match(v, &used, f) == -1 && !used);
        ARM("control: a frame of the other stream matches nothing", kat_match(v, &used, kat_vectors[1].frames[0]) == -1 && !used);
        ARM("an incomplete set is not the complete one", kat_match(v, &used, v->frames[3]) == 3 && used != kat_all(v));
        ARM("a single plane is recognised on its own (for the report on a frame that did not match)",
            kat_plane_known(v, 0, v->frames[2].y) == 2 && kat_plane_known(v, 1, v->frames[5].uv) == 5 &&
            kat_plane_known(v, 0, v->frames[2].uv) == -1 && kat_plane_known(v, 1, v->frames[2].y ^ 1) == -1);
        const KatVector one = { "x", 2, 2, NULL, 0, v->frames, 1 }, full = { "y", 2, 2, NULL, 0, v->frames, 32 };
        ARM("the complete set of 1 frame and of 32 frames", kat_all(&one) == 1u && kat_all(&full) == 0xffffffffu);
    }

    /* ---- the report ---- */
    {
        char line[KAT_LINE]; KatResult r; memset(&r, 0, sizeof r);
        r.streams = 2; r.frames = 11; r.pictures = 11; r.ms = 143; r.nvdec = 0xcfb0; r.channel = 0xc96f;
        r.used_y = r.used_uv = r.fits_y = r.fits_uv = KAT_LAYOUT_UNTRIED;
        kat_describe(&r, line, sizeof line);
        ARM("a pass is reported with its streams, frames, time and classes",
            !strcmp(line, "H.264 decode self-test: PASS (2 streams, 11 frames, 143 ms) NVDEC 0xcfb0 channel 0xc96f"));
        const KatResult pass = r;

        r = pass; r.step = KAT_HASH; r.streams = 0; r.frames = 0; r.pictures = 8; r.mismatched = 8; r.stream = "a"; r.sw = 320; r.sh = 180; r.picture = 1;
        r.used_y = 1; r.used_uv = 2; r.fits_y = 1; r.fits_uv = 0;
        kat_describe(&r, line, sizeof line);
        ARM("a wrong frame: the stream and frame, which plane was wrong, and the layout in which it is right",
            has(line, "FAIL at frame hash (stream a 320x180 frame 1: ") && has(line, "luma (read as Blackwell 8-bit) is an expected plane") &&
            has(line, "chroma (read as Blackwell 16-bit) is wrong and is an expected plane read as Turing") && has(line, "0 of 8 frames matched") && !has(line, "PASS"));
        r.fits_y = KAT_LAYOUT_NONE; r.fits_uv = KAT_LAYOUT_UNTRIED;
        kat_describe(&r, line, sizeof line);
        ARM("a plane that is right in no layout, and one whose other layouts were not tried, are said so",
            has(line, "luma (read as Blackwell 8-bit) is wrong in every known layout") && has(line, "chroma (read as Blackwell 16-bit) is wrong, other layouts not tried"));
        r.fits_y = 1; r.fits_uv = 2;
        kat_describe(&r, line, sizeof line);
        ARM("both planes right and still no match is told apart (a frame twice, or planes of two frames)", has(line, "but not of one frame still expected"));

        r = pass; r.step = KAT_STATUS; r.stream = "b"; r.sw = 1920; r.sh = 1080; r.picture = 2; r.mbs_err = 17; r.err_status = 0x40; r.frames = 9; r.pictures = 10;
        kat_describe(&r, line, sizeof line);
        ARM("an engine error: the frame, the macroblocks in error and the error word",
            has(line, "FAIL at engine status (stream b 1920x1080 frame 2: 17 macroblocks in error, error status 0x40)") && has(line, "9 of 10 frames matched"));
        r.mismatched = 1; r.used_y = 0; r.used_uv = 0; r.fits_y = KAT_LAYOUT_NONE; r.fits_uv = KAT_LAYOUT_NONE;
        kat_describe(&r, line, sizeof line);
        ARM("an engine error with wrong bytes as well names both", has(line, "error status 0x40; a frame's bytes are wrong too: luma (read as Turing) is wrong in every known layout"));

        r = pass; r.step = KAT_DECODE; r.stream = "a"; r.sw = 320; r.sh = 180; r.picture = 1; r.chan_error = 0xffff; r.frames = 0; r.pictures = 0;
        kat_describe(&r, line, sizeof line);
        ARM("a picture that did not decode: the channel's error notifier", has(line, "FAIL at decode (stream a 320x180 frame 1: channel error notifier 0xffff)"));
        r.step = KAT_OPEN; r.picture = 0;
        kat_describe(&r, line, sizeof line);
        ARM("a session that did not open", has(line, "FAIL at session open (stream a 320x180: channel error notifier 0xffff)"));
        r.step = KAT_COUNT; r.want = 8; r.given = 6; r.frames = 6; r.pictures = 6;
        kat_describe(&r, line, sizeof line);
        ARM("frames missing at the end of a stream", has(line, "FAIL at frame count (stream a 320x180: 6 of its 8 frames came back)"));
        r.step = KAT_SIZE; r.picture = 3; r.got_w = 320; r.got_h = 192;
        kat_describe(&r, line, sizeof line);
        ARM("a frame of another size", has(line, "FAIL at frame size (stream a 320x180 frame 3: the frame is 320x192)"));
        r.step = KAT_TIME; r.picture = 0;
        kat_describe(&r, line, sizeof line);
        ARM("the time limit", has(line, "FAIL at time limit (stream a 320x180)"));

        memset(&r, 0, sizeof r); r.step = KAT_NO_RM; r.used_y = r.used_uv = r.fits_y = r.fits_uv = KAT_LAYOUT_UNTRIED;
        kat_describe(&r, line, sizeof line);
        ARM("no RM client: nothing ran", has(line, "FAIL at RM client (before the first stream) NVDEC 0x0 channel 0x0, 0 of 0 frames matched"));
        int named = 1;
        for (int s = 0; s < KAT_STEPS; s++) if (!strcmp(kat_step_name(s), "?")) named = 0;
        ARM("every step has a name, and a value outside them does not read past the table", named && !strcmp(kat_step_name(KAT_STEPS), "?") && !strcmp(kat_step_name(-1), "?"));

        /* the longest report there is must fit, and a short buffer must be cut cleanly */
        r = pass; r.step = KAT_STATUS; r.stream = "b"; r.sw = 1920; r.sh = 1080; r.picture = 32; r.mbs_err = 0xffffffffu; r.err_status = 0xffffffffu;
        r.mismatched = 1; r.used_y = 2; r.used_uv = 2; r.fits_y = 1; r.fits_uv = 1; r.frames = 0xffffffffu; r.pictures = 0xffffffffu; r.ms = 0xffffffffu;
        const int need = kat_describe(&r, line, sizeof line);
        ARM("the longest report fits the line buffer", need > 0 && need < KAT_LINE && (int)strlen(line) == need);
        char small[24]; memset(small, 'x', sizeof small);
        kat_describe(&r, small, 16);
        ARM("a short buffer is cut at its end and nothing past it is written", strlen(small) == 15 && small[16] == 'x');
    }

    /* ---- an independent decode ---- */
    for (unsigned i = 0; i < NVEC && argc > 1; i++) {
        const KatVector *v = &kat_vectors[i]; char name[160];
        if ((int)i + 1 >= argc) { snprintf(name, sizeof name, "%s: a file of decoded frames was given", v->name); ARM(name, 0); continue; }
        const size_t fs = (size_t)v->w * v->h * 3 / 2; uint8_t *buf = malloc(fs); FILE *f = fopen(argv[i + 1], "rb");
        uint32_t used = 0; unsigned got = 0, matched = 0;
        while (buf && f && fread(buf, 1, fs, f) == fs) {
            got++;
            if (kat_match(v, &used, kat_frame_hash(buf, v->w, buf + (size_t)v->w * v->h, v->w, v->w, v->h)) >= 0) matched++;
        }
        snprintf(name, sizeof name, "%s: the other decoder's %u frames are the %u expected ones, each once", v->name, got, v->nframes);
        ARM(name, got == v->nframes && matched == got && used == kat_all(v));
        if (buf && got) {
            uint32_t u2 = 0;
            buf[fs / 3] ^= 1;                                    /* the last frame read, one luma bit changed */
            snprintf(name, sizeof name, "%s control: that decoder's frame with one bit changed matches nothing", v->name);
            ARM(name, kat_match(v, &u2, kat_frame_hash(buf, v->w, buf + (size_t)v->w * v->h, v->w, v->w, v->h)) == -1);
        }
        if (f) fclose(f);
        free(buf);
    }
    printf("%s: %d failed\n", fails ? "FAIL" : "PASS", fails);
    return fails ? 1 : 0;
}
