/* test_plane_alias.c - which two-plane IOSurface planes may live in their own VRAM, the words of the ranged grant, and
   the self-test's patterns and checker (plugin/nvmtl_plane_alias.h), proven on the host.
   Build and run from the repository root:
     cc -std=c11 -Wall -Wextra -Werror -I plugin -I kexts/NVRM tools/test_plane_alias.c -o /tmp/test_plane_alias && /tmp/test_plane_alias */
#include "nvmtl_plane_alias.h"
#include "nvrm_surfshare_abi.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int fails;
#define ARM(name, cond) do { const int ok = !!(cond); printf("%s  %s\n", ok ? "ok  " : "FAIL", name); if (!ok) fails++; } while (0)

/* An NV12 surface as CoreVideo lays one out: plane 0 at `off0`, rows of `row` bytes, chroma right after luma. */
static nvmtl_pa_surface nv12(uint32_t w, uint32_t h, uint32_t row, uint64_t off0, uint32_t bpe0)
{
    nvmtl_pa_surface s; memset(&s, 0, sizeof s);
    s.planes = 2;
    s.p[0] = (nvmtl_pa_plane){ off0, row, w, h, bpe0, 1, 1 };
    s.p[1] = (nvmtl_pa_plane){ off0 + (uint64_t)row * h, row, w / 2, h / 2, 2 * bpe0, 1, 1 };
    s.alloc = s.p[1].off + nvmtl_pa_len(&s.p[1]);
    return s;
}
/* How many of the eligibility rules a function breaks, so the controls can show each rule is needed. */
static int eligibility_rules(int (*el)(const nvmtl_pa_surface *, uint32_t, uint32_t, uint32_t, uint32_t))
{
    int bad = 0;
    nvmtl_pa_surface s = nv12(1920, 1080, 1920, 0, 1), t;
    bad += el(&s, 0, 1920, 1080, 1) != NVMTL_PA_OK;
    bad += el(&s, 1, 960, 540, 2) != NVMTL_PA_OK;
    t = s; t.planes = 1; bad += el(&t, 0, 1920, 1080, 1) != NVMTL_PA_PLANES;
    t = s; t.planes = 3; bad += el(&t, 0, 1920, 1080, 1) != NVMTL_PA_PLANES;
    bad += el(&s, 2, 960, 540, 2) != NVMTL_PA_INDEX;
    bad += el(&s, 0, 1920, 1080, 4) != NVMTL_PA_FORMAT;          /* plane 0 wrapped as BGRA */
    bad += el(&s, 1, 960, 540, 1) != NVMTL_PA_FORMAT;            /* chroma wrapped as R8 */
    bad += el(&s, 0, 1920, 1088, 1) != NVMTL_PA_SIZE;            /* taller than the plane */
    bad += el(&s, 0, 960, 1080, 1) != NVMTL_PA_SIZE;             /* a texture over part of the plane */
    bad += el(&s, 1, 960, 270, 2) != NVMTL_PA_SIZE;
    t = nv12(1920, 1080, 1940, 0, 1); bad += el(&t, 0, 1920, 1080, 1) != NVMTL_PA_PITCH;   /* row not a multiple of 32 */
    t = s; t.p[1].row = 1888; bad += el(&t, 0, 1920, 1080, 1) != NVMTL_PA_ROW;            /* the other plane's row is short */
    t = s; t.alloc -= 1; bad += el(&t, 0, 1920, 1080, 1) != NVMTL_PA_OUTSIDE;              /* the last chroma row ends past it */
    t = s; t.p[1].off -= 32; t.alloc -= 32; bad += el(&t, 1, 960, 540, 2) != NVMTL_PA_OVERLAP;
    t = s; t.p[1].ew = 2; bad += el(&t, 0, 1920, 1080, 1) != NVMTL_PA_BLOCK;
    t = s; t.p[0].bpe = 0; bad += el(&t, 1, 960, 540, 2) != NVMTL_PA_EMPTY;
    t = s; t.p[1].off = 1ull << 32; t.alloc = (1ull << 33); bad += el(&t, 0, 1920, 1080, 1) != NVMTL_PA_LARGE;
    return bad;
}
/* Controls: a version of the rules that forgets one of them. */
static int el_no_overlap(const nvmtl_pa_surface *s, uint32_t p, uint32_t w, uint32_t h, uint32_t b)
{ const int r = nvmtl_pa_eligible(s, p, w, h, b); return r == NVMTL_PA_OVERLAP ? NVMTL_PA_OK : r; }
static int el_only_asked(const nvmtl_pa_surface *s, uint32_t p, uint32_t w, uint32_t h, uint32_t b)
{   /* checks the asked plane alone: a surface could be half planar VRAM, half private */
    if (!s || s->planes != 2) return NVMTL_PA_PLANES;
    if (p > 1) return NVMTL_PA_INDEX;
    const int r = nvmtl_pa_plane_ok(&s->p[p], s->alloc); if (r) return r;
    if (b != s->p[p].bpe) return NVMTL_PA_FORMAT;
    return (w != s->p[p].w || h != s->p[p].h) ? NVMTL_PA_SIZE : NVMTL_PA_OK;
}
static int el_no_size(const nvmtl_pa_surface *s, uint32_t p, uint32_t w, uint32_t h, uint32_t b)
{ const int r = nvmtl_pa_eligible(s, p, w, h, b); return r == NVMTL_PA_SIZE ? NVMTL_PA_OK : r; }

int main(void)
{
    /* ---- the ABI word ---- */
    ARM("the plane flag is the kext's NVRM_SS_PLANE_RANGED, and not one of the two flags 2.9 already uses",
        NVMTL_PA_PLANE_RANGED == NVRM_SS_PLANE_RANGED && !(NVMTL_PA_PLANE_RANGED & (NVRM_SS_PLANE_GPU_MARK | NVRM_SS_PLANE_TRYLOCK)));
    ARM("an older kext sees a plane above 15 in a ranged grant (and refuses it), whichever plane it is",
        (nvmtl_pa_plane_word(0) & ~(NVRM_SS_PLANE_GPU_MARK | NVRM_SS_PLANE_TRYLOCK)) > 15 &&
        (nvmtl_pa_plane_word(1) & ~(NVRM_SS_PLANE_GPU_MARK | NVRM_SS_PLANE_TRYLOCK)) > 15);
    ARM("the plane word keeps the plane index in its low bits",
        (nvmtl_pa_plane_word(0) & 15) == 0 && (nvmtl_pa_plane_word(1) & 15) == 1);
    { const uint64_t w = nvmtl_pa_size_word(0x12345678u, 0x9abcdef0u);
      ARM("the size word is offset << 32 | length, as the kext decodes it (high word, low word)",
          (w >> 32) == 0x12345678u && (w & 0xffffffffu) == 0x9abcdef0u); }
    ARM("a length never spills into the offset", nvmtl_pa_size_word(0, 0xffffffffull) == 0xffffffffull &&
        nvmtl_pa_size_word(1, 0x1ffffffffull) == ((1ull << 32) | 0xffffffffull));

    /* ---- eligibility ---- */
    ARM("every eligibility rule holds (NV12 1080p and fifteen layouts or textures that must be refused)", eligibility_rules(nvmtl_pa_eligible) == 0);
    { nvmtl_pa_surface s = nv12(3840, 2160, 3840, 0, 1);
      ARM("NV12 4K: both planes eligible", nvmtl_pa_eligible(&s, 0, 3840, 2160, 1) == 0 && nvmtl_pa_eligible(&s, 1, 1920, 1080, 2) == 0); }
    { nvmtl_pa_surface s = nv12(1920, 1080, 2048, 4096, 1);
      ARM("a header before plane 0 and a padded row (CoreVideo's 64-byte alignment) are eligible",
          nvmtl_pa_eligible(&s, 0, 1920, 1080, 1) == 0 && nvmtl_pa_eligible(&s, 1, 960, 540, 2) == 0); }
    { nvmtl_pa_surface s = nv12(1920, 1080, 3840, 0, 2);
      ARM("two-plane 16-bit (P010-like): R16 and RG16 planes are eligible at 2 and 4 bytes",
          nvmtl_pa_eligible(&s, 0, 1920, 1080, 2) == 0 && nvmtl_pa_eligible(&s, 1, 960, 540, 4) == 0); }
    { nvmtl_pa_surface s = nv12(1920, 1080, 1920, 0, 1); s.p[1].off = s.alloc; s.alloc += nvmtl_pa_len(&s.p[1]);
      ARM("chroma at the very end of the allocation is inside it", nvmtl_pa_eligible(&s, 1, 960, 540, 2) == 0); }
    { nvmtl_pa_surface s = nv12(1920, 1080, 1920, 0, 1); s.p[0].off = 0xffffffffull - nvmtl_pa_len(&s.p[0]);
      s.p[1].off = 0xffffffffull + 1; s.alloc = s.p[1].off + nvmtl_pa_len(&s.p[1]);
      ARM("an offset of 4 GB is refused; one just below is accepted",
          nvmtl_pa_eligible(&s, 0, 1920, 1080, 1) == NVMTL_PA_LARGE && nvmtl_pa_plane_ok(&s.p[0], s.alloc) == 0); }
    { nvmtl_pa_surface s = nv12(1920, 1080, 1920, 0, 1); s.p[1].off = UINT64_MAX - 16; s.alloc = UINT64_MAX;
      ARM("offset + length is never formed where it could wrap", nvmtl_pa_plane_ok(&s.p[1], s.alloc) == NVMTL_PA_LARGE); }
    { nvmtl_pa_surface s = nv12(1920, 1080, 1920, 0, 1); s.p[1].off = 0;
      ARM("planes at the same offset overlap", nvmtl_pa_eligible(&s, 0, 1920, 1080, 1) == NVMTL_PA_OVERLAP); }
    { nvmtl_pa_surface s = nv12(1920, 1080, 1920, 0, 1);
      ARM("planes that touch do not overlap", s.p[1].off == nvmtl_pa_len(&s.p[0]) && nvmtl_pa_eligible(&s, 0, 1920, 1080, 1) == 0); }
    ARM("no surface at all is not eligible", nvmtl_pa_eligible(NULL, 0, 1, 1, 1) == NVMTL_PA_PLANES);
    { int named = 1; for (int r = 0; r < NVMTL_PA_N; r++) named &= strcmp(nvmtl_pa_reason(r), "an unknown reason") != 0;
      ARM("every reason has words for the log", named && !strcmp(nvmtl_pa_reason(NVMTL_PA_N), "an unknown reason")); }
    ARM("control: rules without the overlap check are told apart", eligibility_rules(el_no_overlap) != 0);
    ARM("control: rules that look at the asked plane alone are told apart", eligibility_rules(el_only_asked) != 0);
    ARM("control: rules without the size check are told apart", eligibility_rules(el_no_size) != 0);

    /* ---- what the kernel says ---- */
    ARM("the self-test starts only when the accelerator offers plane grants and its page-off waits unlocked",
        nvmtl_pa_kernel_says(1, 1) == NULL && nvmtl_pa_kernel_says(0, 1) && nvmtl_pa_kernel_says(1, 0) &&
        nvmtl_pa_kernel_says(-1, 1) && nvmtl_pa_kernel_says(1, 2));

    /* ---- the self-test's surface ---- */
    nvmtl_pa_surface t; memset(&t, 0, sizeof t); nvmtl_pa_test_layout(&t);
    ARM("the self-test surface is eligible as R8 and RG8 at the planes' sizes",
        nvmtl_pa_eligible(&t, 0, NVMTL_PA_TEST_W, NVMTL_PA_TEST_H, 1) == 0 &&
        nvmtl_pa_eligible(&t, 1, NVMTL_PA_TEST_W / 2, NVMTL_PA_TEST_H / 2, 2) == 0);
    ARM("its planes start off any page, with a gap between them and bytes after the last",
        t.p[0].off && t.p[0].off % 4096 && t.p[1].off % 4096 && t.p[1].off > t.p[0].off + nvmtl_pa_len(&t.p[0]) &&
        t.alloc > t.p[1].off + nvmtl_pa_len(&t.p[1]));
    ARM("its rows are full, so the GPU writes every byte a page-off copies",
        (uint64_t)t.p[0].w * t.p[0].bpe == t.p[0].row && (uint64_t)t.p[1].w * t.p[1].bpe == t.p[1].row);
    ARM("its format is '420v'", NVMTL_PA_TEST_FORMAT == (((uint32_t)'4' << 24) | ((uint32_t)'2' << 16) | ((uint32_t)'0' << 8) | 'v'));

    /* ---- patterns ---- */
    { unsigned same = 0, shifted = 0, n = 0;
      for (uint32_t a = 1; a <= 6; a++) for (uint32_t b = a + 1; b <= 6; b++)
          for (uint64_t i = 0; i < 16384; i++) { same += nvmtl_pa_pat(a, i) == nvmtl_pa_pat(b, i); n++; }
      for (uint64_t i = 0; i < 16384; i++) shifted += nvmtl_pa_pat(1, i) == nvmtl_pa_pat(1, i + t.p[0].off);
      ARM("two generations agree on about one byte in 256, no more", same * 128 < n);
      ARM("a pattern copied to another offset does not match itself", shifted * 128 < 16384); }

    /* ---- the checker ---- */
    uint8_t *m = calloc(1, (size_t)t.alloc);
    if (!m) { printf("FAIL out of memory\n"); return 1; }
    const uint32_t bb[2] = { 2, 2 }, bc[2] = { 2, 3 };
    nvmtl_pa_fill(m, 0, t.alloc, 1); nvmtl_pa_plane_fill(m, &t.p[0], 2); nvmtl_pa_plane_fill(m, &t.p[1], 2);
    uint8_t got = 0, want = 0;
    ARM("a surface that holds what it should passes", nvmtl_pa_verify(m, &t, bb, 1, &got, &want) == -1);
    ARM("the same surface checked for another plane-1 generation fails at plane 1's first byte",
        nvmtl_pa_verify(m, &t, bc, 1, &got, &want) == (int64_t)t.p[1].off && got == nvmtl_pa_pat(2, t.p[1].off) && want == nvmtl_pa_pat(3, t.p[1].off));
    { const uint64_t at[] = { t.p[0].off + 1000, t.p[1].off + 7, t.p[0].off + nvmtl_pa_len(&t.p[0]) + 5, t.alloc - 1, 0, t.p[0].off - 1 };
      const char *where[] = { "a byte inside plane 0", "a byte inside plane 1", "a byte in the gap", "the last byte of the tail",
                              "the first byte", "the byte before plane 0" };
      for (unsigned k = 0; k < sizeof at / sizeof *at; k++) {
          char name[96]; snprintf(name, sizeof name, "a wrong byte is found where it is: %s", where[k]);
          m[at[k]] ^= 0x5a;
          ARM(name, nvmtl_pa_verify(m, &t, bb, 1, &got, &want) == (int64_t)at[k] && got != want);
          m[at[k]] ^= 0x5a;
      } }
    { /* plane 1 paged off over plane 0's place: the wrong-offset copy the ranged grant exists to prevent */
      uint8_t *img = malloc((size_t)nvmtl_pa_len(&t.p[1]));
      if (img) { memcpy(img, m + t.p[1].off, (size_t)nvmtl_pa_len(&t.p[1])); memcpy(m + t.p[0].off, img, (size_t)nvmtl_pa_len(&t.p[1])); free(img); }
      ARM("plane 1's bytes copied to plane 0's place are found", nvmtl_pa_verify(m, &t, bb, 1, &got, &want) == (int64_t)t.p[0].off);
      nvmtl_pa_plane_fill(m, &t.p[0], 2); }
    { nvmtl_pa_surface p = nv12(64, 32, 96, 64, 1); uint8_t *n = calloc(1, (size_t)p.alloc);
      if (n) {
          nvmtl_pa_fill(n, 0, p.alloc, 4); nvmtl_pa_plane_fill(n, &p.p[0], 5); nvmtl_pa_plane_fill(n, &p.p[1], 6);
          const uint32_t g[2] = { 5, 6 };
          n[p.p[0].off + 64 + 3] ^= 0xff;   /* inside plane 0's row padding: nobody's bytes */
          ARM("a plane's row padding is not looked at, its pixels and the bytes around it are",
              nvmtl_pa_verify(n, &p, g, 4, NULL, NULL) == -1 && (n[p.p[0].off + 63] ^= 1, nvmtl_pa_verify(n, &p, g, 4, NULL, NULL)) == (int64_t)(p.p[0].off + 63));
          free(n);
      } }
    { uint8_t img[128 * 64];
      nvmtl_pa_plane_image(&t.p[0], 7, img);
      ARM("a texture's image of a generation is the plane's bytes of it", nvmtl_pa_image_check(img, &t.p[0], 7, NULL, NULL) == -1);
      nvmtl_pa_plane_fill(m, &t.p[0], 7);
      ARM("writing that image at the plane's rows gives what a CPU fill gives", !memcmp(img, m + t.p[0].off, sizeof img));
      img[300] ^= 1;
      ARM("a wrong byte in a read-back image is found", nvmtl_pa_image_check(img, &t.p[0], 7, &got, &want) == 300 && got != want); }
    { nvmtl_pa_result r; memset(&r, 0, sizeof r); nvmtl_pa_fail(&r, 4, "x", 12, 1, 2);
      ARM("a failure keeps the step, what, the byte's offset and both bytes", r.step == 4 && !strcmp(r.what, "x") && r.at == 12 && r.got == 1 && r.want == 2); }
    free(m);
    printf(fails ? "%d failed\n" : "all passed\n", fails);
    return fails ? 1 : 0;
}
