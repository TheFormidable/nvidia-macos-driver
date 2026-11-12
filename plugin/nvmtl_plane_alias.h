/* nvmtl_plane_alias.h - two-plane IOSurfaces (NV12 and its kin) whose planes live in the accelerator's own VRAM.

   A single-plane IOSurface texture is a linear image placed over the VRAM the accelerator family keeps for the surface,
   and the kernel copies that VRAM back to the surface's memory when a CPU lock needs it (the page-off). The family keeps
   one such VRAM per (surface, plane), and each holds its plane alone from offset 0, but the surface's memory holds both
   planes, at offsets the kernel cannot read for itself. So the plugin names each plane's byte range when it asks for the
   plane's VRAM (the ranged grant, NVRM_SS_PLANE_RANGED in kexts/NVRM/nvrm_surfshare_abi.h), and the kernel then copies
   that plane into that range and nowhere else. A surface takes this path only as a whole (both planes eligible), and only
   in a process where a round trip on a private surface of its own came back right (the self-test, whose patterns and
   checker are here); everything else keeps the private image that is copied in and out.

   Pure C (<stdint.h>, <stddef.h>); tools/test_plane_alias.c proves every rule on the host. */
#ifndef NVMTL_PLANE_ALIAS_H
#define NVMTL_PLANE_ALIAS_H
#include <stddef.h>
#include <stdint.h>

/* The plugin spells the surface ABI's numbers as literals (as nvmtl_vk.c does for the NVRM_SS_* statuses);
   tools/test_plane_alias.c compares this one with the kext's header. Whatever the kernel answers a grant (refusals
   included: NVRM_SS_OFF, NVRM_SS_BAD_RANGE, NVRM_SS_NO_DMA, an older kext's NVRM_SS_NO_CACHE), NVK turns any refusal
   into a failed import, and the texture is then a private image. */
#define NVMTL_PA_PLANE_RANGED 0x20000000u   /* NVRM_SS_PLANE_RANGED: a bit of the grant's plane word */

/* One plane as IOSurface describes it: byte offset in the surface's memory, bytes per row, width and height in
   elements, bytes per element, and the pixels an element covers (1 x 1 for every format this path takes). */
typedef struct { uint64_t off; uint32_t row, w, h, bpe, ew, eh; } nvmtl_pa_plane;
/* The surface: its allocation's size, how many planes it has, the first two. */
typedef struct { uint64_t alloc; uint32_t planes; nvmtl_pa_plane p[2]; } nvmtl_pa_surface;

/* The bytes of a plane the page-off copies: every row, padding included. */
static inline uint64_t nvmtl_pa_len(const nvmtl_pa_plane *p) { return (uint64_t)p->row * p->h; }

/* Why a surface plane is not eligible; 0 = it is. */
enum {
    NVMTL_PA_OK = 0,
    NVMTL_PA_PLANES,    /* the surface does not have exactly two planes */
    NVMTL_PA_INDEX,     /* the texture names a plane other than 0 or 1 */
    NVMTL_PA_EMPTY,     /* a plane without width, height, row or element size */
    NVMTL_PA_BLOCK,     /* a plane whose elements cover more than one pixel */
    NVMTL_PA_ROW,       /* a plane's row cannot hold its width */
    NVMTL_PA_PITCH,     /* a plane's row is not a multiple of 32 bytes (the linear pitch unit of the texture header) */
    NVMTL_PA_LARGE,     /* a plane's offset or length does not fit the 32 bits the grant carries each in */
    NVMTL_PA_OUTSIDE,   /* a plane's last row ends past the allocation */
    NVMTL_PA_OVERLAP,   /* the two planes share bytes */
    NVMTL_PA_FORMAT,    /* the texture's bytes per pixel are not the plane's bytes per element */
    NVMTL_PA_SIZE,      /* the texture is not the plane's size, so the page-off would copy rows nobody wrote */
    NVMTL_PA_N
};
static inline const char *nvmtl_pa_reason(int why)
{
    switch (why) {
    case NVMTL_PA_OK:      return "eligible";
    case NVMTL_PA_PLANES:  return "not exactly two planes";
    case NVMTL_PA_INDEX:   return "a plane index above 1";
    case NVMTL_PA_EMPTY:   return "a plane without width, height, row or element size";
    case NVMTL_PA_BLOCK:   return "a plane whose elements cover more than one pixel";
    case NVMTL_PA_ROW:     return "a plane row shorter than its width";
    case NVMTL_PA_PITCH:   return "a plane row that is not a multiple of 32 bytes";
    case NVMTL_PA_LARGE:   return "a plane offset or length of 4 GB or more";
    case NVMTL_PA_OUTSIDE: return "a plane that ends past the allocation";
    case NVMTL_PA_OVERLAP: return "planes that share bytes";
    case NVMTL_PA_FORMAT:  return "the texture's pixel size is not the plane's";
    case NVMTL_PA_SIZE:    return "the texture is not the plane's size";
    default:               return "an unknown reason";
    }
}
/* The geometry of one plane alone. */
static inline int nvmtl_pa_plane_ok(const nvmtl_pa_plane *p, uint64_t alloc)
{
    if (!p->w || !p->h || !p->row || !p->bpe) return NVMTL_PA_EMPTY;
    if (p->ew != 1 || p->eh != 1) return NVMTL_PA_BLOCK;
    if ((uint64_t)p->w * p->bpe > p->row) return NVMTL_PA_ROW;
    if (p->row % 32) return NVMTL_PA_PITCH;
    const uint64_t len = nvmtl_pa_len(p);
    if (p->off > 0xffffffffull || len > 0xffffffffull) return NVMTL_PA_LARGE;
    if (p->off > alloc || len > alloc - p->off) return NVMTL_PA_OUTSIDE;
    return NVMTL_PA_OK;
}
/* Whether the texture `plane` of `s`, w x h at `bpp` bytes per pixel, may live in the plane's own VRAM. Both planes'
   geometry is checked whichever one is asked for: a surface is planar VRAM or private as a whole, so that a writer and a
   reader of it never disagree about where the newest pixels are. */
static inline int nvmtl_pa_eligible(const nvmtl_pa_surface *s, uint32_t plane, uint32_t w, uint32_t h, uint32_t bpp)
{
    if (!s || s->planes != 2) return NVMTL_PA_PLANES;
    if (plane > 1) return NVMTL_PA_INDEX;
    for (int i = 0; i < 2; i++) { const int r = nvmtl_pa_plane_ok(&s->p[i], s->alloc); if (r) return r; }
    const uint64_t a0 = s->p[0].off, e0 = a0 + nvmtl_pa_len(&s->p[0]), a1 = s->p[1].off, e1 = a1 + nvmtl_pa_len(&s->p[1]);
    if (a0 < e1 && a1 < e0) return NVMTL_PA_OVERLAP;
    const nvmtl_pa_plane *p = &s->p[plane];
    if (bpp != p->bpe) return NVMTL_PA_FORMAT;
    if (w != p->w || h != p->h) return NVMTL_PA_SIZE;
    return NVMTL_PA_OK;
}
/* The two words of a ranged grant: the plane with the flag, and (offset << 32) | length. Only for an eligible plane,
   whose offset and length both fit 32 bits. */
static inline uint32_t nvmtl_pa_plane_word(uint32_t plane) { return plane | NVMTL_PA_PLANE_RANGED; }
static inline uint64_t nvmtl_pa_size_word(uint64_t off, uint64_t len) { return (off << 32) | (len & 0xffffffffull); }

/* What the kernel says before a process tries: the accelerator's NVAccelSurfPlanes property and
   debug.nvaccel_pageoff_unlocked_wait (a grant needs both). NULL = go on with the self-test, else why not. */
static inline const char *nvmtl_pa_kernel_says(int planes_offered, int unlocked_wait)
{
    if (planes_offered != 1) return "the accelerator offers no plane grants (an older NVAccel, or boot-arg nvaccel_surf_planes=0)";
    if (unlocked_wait != 1) return "the accelerator's page-off waits with its lock held (debug.nvaccel_pageoff_unlocked_wait is not 1)";
    return NULL;
}

/* The self-test's state, one per process, moved by compare-and-swap: only PASSED lets a real surface take the path. */
enum { NVMTL_PA_UNTESTED = 0, NVMTL_PA_RUNNING = 1, NVMTL_PA_PASSED = 2, NVMTL_PA_FAILED = 3 };

/* The private surface of the self-test: 128 x 64 '420v' (8-bit NV12, video range). Plane 0 at byte 256, plane 1 after a
   gap of 96 bytes, 256 spare bytes after it: neither plane starts on a page, and a copy to the wrong place, of the wrong
   length or of the wrong plane lands on bytes the test watches. Both planes' rows are full (no padding), so every byte of
   a plane is one the GPU writes. */
#define NVMTL_PA_TEST_FORMAT 0x34323076u   /* '420v' */
#define NVMTL_PA_TEST_W 128u
#define NVMTL_PA_TEST_H 64u
static inline void nvmtl_pa_test_layout(nvmtl_pa_surface *s)
{
    const nvmtl_pa_plane y = { 256, 128, NVMTL_PA_TEST_W, NVMTL_PA_TEST_H, 1, 1, 1 };
    const nvmtl_pa_plane c = { 256 + 128ull * NVMTL_PA_TEST_H + 96, 128, NVMTL_PA_TEST_W / 2, NVMTL_PA_TEST_H / 2, 2, 1, 1 };
    s->planes = 2; s->p[0] = y; s->p[1] = c;
    s->alloc = c.off + nvmtl_pa_len(&c) + 256;
}

/* The self-test's patterns: a byte of generation `gen` at byte `i` of the allocation. Each generation and each position
   gives its own byte (up to the chance of one in 256 of an equal byte), so a plane that is copied to another offset, a
   stale copy and a copy of the other plane all show. */
static inline uint8_t nvmtl_pa_pat(uint32_t gen, uint64_t i)
{
    uint64_t x = (i + 1) * 0x9E3779B97F4A7C15ull ^ ((uint64_t)gen + 1) * 0xD1B54A32D192ED03ull;
    x ^= x >> 31; x *= 0xBF58476D1CE4E5B9ull; x ^= x >> 29; x *= 0x94D049BB133111EBull; x ^= x >> 32;
    return (uint8_t)x;
}
/* Generation `gen` into the allocation's bytes [from, to). */
static inline void nvmtl_pa_fill(uint8_t *base, uint64_t from, uint64_t to, uint32_t gen)
{
    for (uint64_t i = from; i < to; i++) base[i] = nvmtl_pa_pat(gen, i);
}
/* Generation `gen` into the pixel bytes of plane `p` inside the allocation (rows' padding untouched). */
static inline void nvmtl_pa_plane_fill(uint8_t *base, const nvmtl_pa_plane *p, uint32_t gen)
{
    const uint64_t bytes = (uint64_t)p->w * p->bpe;
    for (uint32_t y = 0; y < p->h; y++) nvmtl_pa_fill(base, p->off + (uint64_t)y * p->row, p->off + (uint64_t)y * p->row + bytes, gen);
}
/* The plane's pixels of generation `gen` as a tight image (w * bpe bytes a row): what a GPU write of that generation puts
   in the plane's texture, and what a read of the texture must give back. */
static inline void nvmtl_pa_plane_image(const nvmtl_pa_plane *p, uint32_t gen, uint8_t *dst)
{
    const uint64_t bytes = (uint64_t)p->w * p->bpe;
    for (uint32_t y = 0; y < p->h; y++)
        for (uint64_t x = 0; x < bytes; x++) dst[(uint64_t)y * bytes + x] = nvmtl_pa_pat(gen, p->off + (uint64_t)y * p->row + x);
}
/* The first byte of a tight image of plane `p` that is not generation `gen`, or -1. */
static inline int64_t nvmtl_pa_image_check(const uint8_t *img, const nvmtl_pa_plane *p, uint32_t gen, uint8_t *got, uint8_t *want)
{
    const uint64_t bytes = (uint64_t)p->w * p->bpe;
    for (uint32_t y = 0; y < p->h; y++)
        for (uint64_t x = 0; x < bytes; x++) {
            const uint8_t e = nvmtl_pa_pat(gen, p->off + (uint64_t)y * p->row + x), v = img[(uint64_t)y * bytes + x];
            if (v != e) { if (got) *got = v; if (want) *want = e; return (int64_t)((uint64_t)y * bytes + x); }
        }
    return -1;
}
/* What the whole allocation must hold: plane i's pixel bytes are generation gen_of_plane[i], every byte outside both
   planes is gen_rest, and a row's padding inside a plane is not looked at (no GPU write reaches it, and a page-off copies
   whatever the VRAM held there). The offset of the first wrong byte, or -1; *got and *want say what was there and what
   should have been. */
static inline int64_t nvmtl_pa_verify(const uint8_t *base, const nvmtl_pa_surface *s, const uint32_t gen_of_plane[2],
                                      uint32_t gen_rest, uint8_t *got, uint8_t *want)
{
    for (uint64_t i = 0; i < s->alloc; i++) {
        uint32_t gen = gen_rest; int skip = 0;
        for (int k = 0; k < 2; k++) {
            const nvmtl_pa_plane *p = &s->p[k];
            if (i < p->off || i - p->off >= nvmtl_pa_len(p)) continue;
            if ((i - p->off) % p->row < (uint64_t)p->w * p->bpe) gen = gen_of_plane[k]; else skip = 1;
            break;
        }
        if (skip) continue;
        const uint8_t e = nvmtl_pa_pat(gen, i);
        if (base[i] != e) { if (got) *got = base[i]; if (want) *want = e; return (int64_t)i; }
    }
    return -1;
}
/* A self-test's outcome: step 0 = passed; otherwise the step that failed (the steps are those of
   nvmtl_vk_plane_selftest), what failed, and for a wrong byte its offset in the allocation (or in the image read back,
   as `what` says) with the byte found and the byte expected (at = -1: no byte to show). */
typedef struct { int step; const char *what; int64_t at; uint8_t got, want; } nvmtl_pa_result;
static inline void nvmtl_pa_fail(nvmtl_pa_result *r, int step, const char *what, int64_t at, uint8_t got, uint8_t want)
{
    r->step = step; r->what = what; r->at = at; r->got = got; r->want = want;
}
#endif
