// h264kat_core.h: the parts of the H.264 decode self-test (h264kat.h) that need no GPU: the hash of a decoded frame,
// the match of the frames a decoder gave back against the frames a stream must give, and the one line that reports a
// run. Plain C with no dependency on the rest of the library, so tools/test_h264_kat.c runs all of it on any machine.
#pragma once
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

// One frame's answer: FNV-1a 64 over the cropped luma rows, and over the cropped rows of interleaved chroma.
typedef struct { uint64_t y, uv; } KatFrame;
// A test stream (Annex B: parameter sets and slices), the size of its cropped picture and the frames it decodes to.
// A stream has at most KAT_MAX_FRAMES frames, all different pictures: they are matched as a set.
typedef struct { const char *name; unsigned w, h; const uint8_t *bytes; size_t len; const KatFrame *frames; unsigned nframes; } KatVector;
enum { KAT_MAX_FRAMES = 32 };

#define KAT_FNV0 0xcbf29ce484222325ull
static inline uint64_t kat_fnv1a(uint64_t h, const uint8_t *p, size_t n) { while (n--) { h ^= *p++; h *= 0x100000001b3ull; } return h; }
// w bytes of each of rows rows (stride bytes apart): a luma plane w x h, or an NV12 chroma plane w x h / 2
static inline uint64_t kat_plane_hash(const uint8_t *p, size_t stride, unsigned w, unsigned rows)
{
    uint64_t h = KAT_FNV0;
    for (unsigned r = 0; r < rows; r++) h = kat_fnv1a(h, p + (size_t)r * stride, w);
    return h;
}
static inline KatFrame kat_frame_hash(const uint8_t *y, size_t ystride, const uint8_t *uv, size_t uvstride, unsigned w, unsigned h)
{
    const KatFrame f = { kat_plane_hash(y, ystride, w, h), kat_plane_hash(uv, uvstride, w, h / 2) };
    return f;
}
// Every expected frame exactly once, in any order (a decoder may hand frames back in decode order): the index of the
// expected frame this one is, marked in *used so that it cannot be matched again; -1 when it is none of those left.
static inline int kat_match(const KatVector *v, uint32_t *used, KatFrame f)
{
    for (unsigned i = 0; i < v->nframes && i < KAT_MAX_FRAMES; i++)
        if (!(*used & (1u << i)) && v->frames[i].y == f.y && v->frames[i].uv == f.uv) { *used |= 1u << i; return (int)i; }
    return -1;
}
// all nframes bits: what *used must be once a stream is through
static inline uint32_t kat_all(const KatVector *v) { return v->nframes >= 32 ? 0xffffffffu : (1u << v->nframes) - 1; }
// Is this the hash of some expected frame's luma (chroma = 0) or chroma (chroma = 1) plane? Used only to say, after a
// frame did not match, whether one of its planes would have matched read in another byte layout.
static inline int kat_plane_known(const KatVector *v, int chroma, uint64_t h)
{
    for (unsigned i = 0; i < v->nframes; i++) if ((chroma ? v->frames[i].uv : v->frames[i].y) == h) return (int)i;
    return -1;
}

// ---- the result of a run and the line that reports it ----
// Where a run stopped. Everything but KAT_PASS leaves the decoder unregistered.
enum {
    KAT_PASS = 0,
    KAT_NO_RM,        // no RM client: the driver's user client did not open
    KAT_NO_CLASS,     // the card's class list has no NVDEC or no channel class this library knows
    KAT_NO_VASPACE,   // the GPU address space was refused
    KAT_NO_MEMORY,    // malloc
    KAT_BAD_VECTOR,   // an embedded stream is not what its table entry says (a build defect, not the card)
    KAT_OPEN,         // the decode session did not open: channel, engine object or buffers
    KAT_DECODE,       // a picture was not decoded: no completion from the engine, or the decoder refused the stream
    KAT_TIME,         // the run passed its time limit
    KAT_SIZE,         // a frame came back with another size than the stream's
    KAT_STATUS,       // the engine completed a picture and reported an error for it
    KAT_HASH,         // a frame's bytes are not those of any frame the stream decodes to
    KAT_COUNT,        // frames missing at the end of a stream
    KAT_STEPS
};
enum { KAT_LAYOUT_UNTRIED = -2, KAT_LAYOUT_NONE = -1 };   // otherwise one of blocklinear.h's GOB_* values
typedef struct {
    int step;                        // KAT_PASS, or where the first failure was
    unsigned streams;                // streams that passed
    unsigned frames;                 // frames that matched, all streams
    unsigned pictures;               // frames the decoder gave back, all streams
    unsigned mismatched;             // of those, frames that are none of the expected ones
    const char *stream;              // the stream a failure is in (its name), NULL before the first one
    unsigned sw, sh;                 // that stream's picture size
    unsigned want, given;            // the frames that stream decodes to, and the frames the decoder gave back for it
    unsigned picture;                // which of that stream's frames, counted from 1 in the order the decoder gave them
    unsigned mbs_err, err_status;    // KAT_STATUS: the engine's count of macroblocks in error and its error word
    unsigned chan_error;             // KAT_DECODE, KAT_OPEN: the channel's error notifier
    unsigned got_w, got_h;           // KAT_SIZE
    unsigned nvdec, channel;         // the classes the run used (0 = none found)
    int used_y, used_uv;             // mismatched != 0: the byte layouts the decoder read the first such frame's planes
    int fits_y, fits_uv;             // in, and the layout in which each plane is an expected one
    unsigned ms;                     // the whole run
} KatResult;

static inline const char *kat_layout_name(int l)
{
    return l == 0 ? "Turing" : l == 1 ? "Blackwell 8-bit" : l == 2 ? "Blackwell 16-bit"
         : l == KAT_LAYOUT_NONE ? "no known layout" : "not tried";
}
static inline const char *kat_step_name(int step)
{
    static const char *const n[KAT_STEPS] = { "pass", "RM client", "class list", "address space", "memory", "test vector",
        "session open", "decode", "time limit", "frame size", "engine status", "frame hash", "frame count" };
    return step >= 0 && step < KAT_STEPS ? n[step] : "?";
}
// One plane of a frame that did not match: was it right as read, would another layout have made it right?
static inline int kat_plane_words(char *buf, size_t n, const char *plane, int used, int fits)
{
    if (fits == used) return snprintf(buf, n, "%s (read as %s) is an expected plane", plane, kat_layout_name(used));
    if (fits == KAT_LAYOUT_NONE) return snprintf(buf, n, "%s (read as %s) is wrong in every known layout", plane, kat_layout_name(used));
    if (fits == KAT_LAYOUT_UNTRIED) return snprintf(buf, n, "%s (read as %s) is wrong, other layouts not tried", plane, kat_layout_name(used));
    return snprintf(buf, n, "%s (read as %s) is wrong and is an expected plane read as %s", plane, kat_layout_name(used), kat_layout_name(fits));
}
// The report, one line without a newline, at most n - 1 bytes (snprintf's rule; returns what snprintf returns). A
// buffer of KAT_LINE bytes holds every report whole.
// "H.264 decode self-test: PASS (2 streams, 11 frames, 143 ms) NVDEC 0xcfb0 channel 0xc96f" or
// "H.264 decode self-test: FAIL at <step> (<where>: <what was seen>) NVDEC 0x.. channel 0x.., <matched> of <given
// back> frames matched in <ms> ms"
enum { KAT_LINE = 640 };
static inline int kat_describe(const KatResult *r, char *buf, size_t n)
{
    if (r->step == KAT_PASS)
        return snprintf(buf, n, "H.264 decode self-test: PASS (%u streams, %u frames, %u ms) NVDEC 0x%x channel 0x%x",
                        r->streams, r->frames, r->ms, r->nvdec, r->channel);
    char where[96], what[96] = "", planes[336] = "";   // planes: the longest lead, both plane texts and the tail fit
    if (r->stream && r->picture) snprintf(where, sizeof where, "stream %s %ux%u frame %u", r->stream, r->sw, r->sh, r->picture);
    else if (r->stream) snprintf(where, sizeof where, "stream %s %ux%u", r->stream, r->sw, r->sh);
    else snprintf(where, sizeof where, "before the first stream");
    switch (r->step) {
    case KAT_OPEN: case KAT_DECODE:
        snprintf(what, sizeof what, ": channel error notifier 0x%x", r->chan_error); break;
    case KAT_SIZE:
        snprintf(what, sizeof what, ": the frame is %ux%u", r->got_w, r->got_h); break;
    case KAT_STATUS:
        snprintf(what, sizeof what, ": %u macroblocks in error, error status 0x%x", r->mbs_err, r->err_status); break;
    case KAT_COUNT:
        snprintf(what, sizeof what, ": %u of its %u frames came back", r->given, r->want); break;
    default: break;
    }
    if (r->mismatched) {
        char y[128], uv[128];
        kat_plane_words(y, sizeof y, "luma", r->used_y, r->fits_y); kat_plane_words(uv, sizeof uv, "chroma", r->used_uv, r->fits_uv);
        // both planes right and still no match: the frame came back twice, or its planes are those of two frames
        snprintf(planes, sizeof planes, "%s %s, %s%s", r->step == KAT_HASH ? ":" : "; a frame's bytes are wrong too:", y, uv,
                 r->fits_y == r->used_y && r->fits_uv == r->used_uv ? ", but not of one frame still expected" : "");
    }
    return snprintf(buf, n, "H.264 decode self-test: FAIL at %s (%s%s%s) NVDEC 0x%x channel 0x%x, %u of %u frames matched in %u ms",
                    kat_step_name(r->step), where, what, planes, r->nvdec, r->channel, r->frames, r->pictures, r->ms);
}
