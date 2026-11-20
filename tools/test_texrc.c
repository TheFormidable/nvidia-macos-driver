/* test_texrc.c - the texture recycle cache (plugin/nvmtl_texrc.h): which dead images it keeps, which texture takes one,
   and when it lets them go.
   Build and run anywhere (no GPU, no Metal):  cc -std=c11 -Wall -Wextra -I plugin tools/test_texrc.c -o /tmp/test_texrc && /tmp/test_texrc
   Each arm is a sentence about a failure the rule must produce or refuse. A model of the plugin's VRAM counts checks
   that a parked image stays counted until it is really freed and is counted once when it is taken back. */
#include <stdio.h>
#include "nvmtl_texrc.h"

static int fails;
static void arm(const char *what, int ok) { printf("%s  %s\n", ok ? "ok  " : "FAIL", what); if (!ok) fails++; }

#define MB (1ull << 20)
#define S  1000000000ull
static int handles[256];
static nvmtl_texrc_entry ent(nvmtl_texrc_key k, int h, uint64_t bytes)
{
    nvmtl_texrc_entry e = { k, &handles[2 * h], &handles[2 * h + 1], bytes, 0 };
    return e;
}

/* the plugin's counts: what nvmtl_commit_image adds, what the destroy takes away, and the memory RM really holds */
static uint64_t counted, allocated;
static void model_create_fresh(uint64_t b) { allocated += b; counted += b; }
static void model_free(uint64_t b) { allocated -= b; counted -= b; }

int main(void)
{
    const nvmtl_texrc_key a = nvmtl_texrc_key_of(0x8u, 1, 37, 1920, 1080, 1, 1, 1, 0x17u);
    nvmtl_texrc_entry out[NVMTL_TEXRC_N], got;
    int x = 0, y = 0;

    arm("the switch unset leaves the cache on", nvmtl_texrc_enabled(NULL, "java"));
    arm("NVMTL_TEX_RECYCLE=0 turns it off", !nvmtl_texrc_enabled("0", "java"));
    arm("NVMTL_TEX_RECYCLE=1 leaves it on", nvmtl_texrc_enabled("1", "java"));
    arm("WindowServer never parks, whatever the switch says", !nvmtl_texrc_enabled(NULL, "WindowServer") && !nvmtl_texrc_enabled("1", "WindowServer"));

    arm("an ordinary dead texture with its own VRAM is parked", nvmtl_texrc_eligible(1, 1, &x, &y, &x, &y, 0, 0, 0, 0, 8 * MB));
    arm("with the cache off it is destroyed", !nvmtl_texrc_eligible(0, 1, &x, &y, &x, &y, 0, 0, 0, 0, 8 * MB));
    arm("with the image grace off it is destroyed (nothing proves the GPU is done with it)", !nvmtl_texrc_eligible(1, 0, &x, &y, &x, &y, 0, 0, 0, 0, 8 * MB));
    arm("an image that was never marked recyclable is destroyed", !nvmtl_texrc_eligible(1, 1, &x, &y, NULL, NULL, 0, 0, 0, 0, 8 * MB));
    arm("an image re-backed to other memory is destroyed", !nvmtl_texrc_eligible(1, 1, &x, &x, &x, &y, 0, 0, 0, 0, 8 * MB));
    arm("an image migrated to another VkImage is destroyed", !nvmtl_texrc_eligible(1, 1, &y, &y, &x, &y, 0, 0, 0, 0, 8 * MB));
    arm("a placed image is destroyed", !nvmtl_texrc_eligible(1, 1, &x, &y, &x, &y, 1, 0, 0, 0, 8 * MB));
    arm("a pooled image is destroyed", !nvmtl_texrc_eligible(1, 1, &x, &y, &x, &y, 0, 1, 0, 0, 8 * MB));
    arm("a sparse image is destroyed", !nvmtl_texrc_eligible(1, 1, &x, &y, &x, &y, 0, 0, 1, 0, 8 * MB));
    arm("an image in system memory is destroyed", !nvmtl_texrc_eligible(1, 1, &x, &y, &x, &y, 0, 0, 0, 1, 8 * MB));
    arm("an image without memory is destroyed", !nvmtl_texrc_eligible(1, 1, &x, NULL, &x, NULL, 0, 0, 0, 0, 8 * MB));
    arm("an image larger than one entry may be is destroyed", !nvmtl_texrc_eligible(1, 1, &x, &y, &x, &y, 0, 0, 0, 0, NVMTL_TEXRC_MAX1 + 1));
    arm("an image of exactly the largest entry is parked", nvmtl_texrc_eligible(1, 1, &x, &y, &x, &y, 0, 0, 0, 0, NVMTL_TEXRC_MAX1));

    {   nvmtl_texrc_key k[9]; for (int i = 0; i < 9; i++) k[i] = a;
        k[0].flags ^= 1; k[1].type = 2; k[2].format = 44; k[3].w = 1919; k[4].h = 1079; k[5].d = 2; k[6].mips = 2; k[7].layers = 2; k[8].usage ^= 8;
        int any = 0; for (int i = 0; i < 9; i++) any |= nvmtl_texrc_key_eq(&k[i], &a);
        arm("a creation that differs in any one field (flags, type, format, extent, mips, layers, usage) does not match", !any);
        arm("the same creation matches", nvmtl_texrc_key_eq(&a, &a)); }

    {   nvmtl_texrc_state s; memset(&s, 0, sizeof s);
        nvmtl_texrc_key b = a; b.format = 44;
        nvmtl_texrc_entry e1 = ent(a, 1, 8 * MB), e2 = ent(a, 2, 8 * MB);
        uint32_t n = nvmtl_texrc_put(&s, &e1, 10 * S, out); n += nvmtl_texrc_put(&s, &e2, 11 * S, out + n);
        arm("parking two frees nothing", n == 0 && s.n == 2 && s.bytes == 16 * MB);
        arm("a texture of another creation takes nothing", !nvmtl_texrc_take(&s, &b, &got) && s.n == 2);
        arm("a texture of the same creation takes the newest one", nvmtl_texrc_take(&s, &a, &got) && got.img == e2.img && got.mem == e2.mem && got.bytes == 8 * MB);
        arm("and it is no longer held", s.n == 1 && s.bytes == 8 * MB && s.reused == 1);
        arm("the next one takes the older one, image and memory together", nvmtl_texrc_take(&s, &a, &got) && got.img == e1.img && got.mem == e1.mem);
        arm("an empty cache takes nothing", !nvmtl_texrc_take(&s, &a, &got) && s.n == 0 && s.bytes == 0); }

    {   nvmtl_texrc_state s; memset(&s, 0, sizeof s);
        uint32_t n = 0;
        for (int i = 0; i < (int)NVMTL_TEXRC_N; i++) { nvmtl_texrc_entry e = ent(a, i, 1 * MB); n += nvmtl_texrc_put(&s, &e, 5 * S + (uint64_t)i, out); }
        arm("the cache holds its count of entries", n == 0 && s.n == NVMTL_TEXRC_N);
        nvmtl_texrc_entry e = ent(a, 100, 1 * MB);
        n = nvmtl_texrc_put(&s, &e, 5 * S + 100, out);
        arm("one more evicts the oldest, and only it", n == 1 && out[0].img == &handles[0] && s.n == NVMTL_TEXRC_N && s.evicted == 1); }

    {   nvmtl_texrc_state s; memset(&s, 0, sizeof s);
        uint32_t n = 0;
        for (int i = 0; i < 4; i++) { nvmtl_texrc_entry e = ent(a, i, 32 * MB); n += nvmtl_texrc_put(&s, &e, 5 * S, out); }
        arm("the cache holds its bytes", n == 0 && s.bytes == NVMTL_TEXRC_BYTES);
        nvmtl_texrc_entry e = ent(a, 10, 16 * MB);
        n = nvmtl_texrc_put(&s, &e, 5 * S, out);
        arm("one more evicts the oldest until it fits", n == 1 && out[0].img == &handles[0] && s.bytes == 112 * MB);
        int over = 0; for (int i = 0; i < 64; i++) { nvmtl_texrc_entry f = ent(a, i % 100, (uint64_t)(1 + i % 32) * MB); nvmtl_texrc_put(&s, &f, 5 * S, out); over |= s.bytes > NVMTL_TEXRC_BYTES || s.n > NVMTL_TEXRC_N; }
        arm("no sequence of parks passes either bound", !over); }

    {   nvmtl_texrc_state s; memset(&s, 0, sizeof s);
        nvmtl_texrc_entry e1 = ent(a, 1, 4 * MB), e2 = ent(a, 2, 4 * MB);
        nvmtl_texrc_put(&s, &e1, 10 * S, out); nvmtl_texrc_put(&s, &e2, 10 * S + S / 2, out);
        arm("nothing ages within a second", nvmtl_texrc_age(&s, 11 * S, out) == 0 && s.n == 2);
        uint32_t n = nvmtl_texrc_age(&s, 11 * S + 1, out);
        arm("an entry not taken for more than a second leaves, the younger stays", n == 1 && out[0].img == e1.img && s.n == 1 && s.aged == 1);
        arm("a clock reading before the park ages nothing", nvmtl_texrc_age(&s, 1 * S, out) == 0 && s.n == 1);
        nvmtl_texrc_entry e3 = ent(a, 3, 4 * MB);
        n = nvmtl_texrc_put(&s, &e3, 13 * S, out);
        arm("a park lets aged entries go first", n == 1 && out[0].img == e2.img && s.n == 1 && s.e[0].img == e3.img); }

    {   nvmtl_texrc_state s; memset(&s, 0, sizeof s);
        for (int i = 0; i < 5; i++) { nvmtl_texrc_entry e = ent(a, i, 8 * MB); nvmtl_texrc_put(&s, &e, 5 * S, out); }
        uint32_t n = nvmtl_texrc_give_back(&s, out);
        arm("the VRAM gate gets every parked image back", n == 5 && s.n == 0 && s.bytes == 0 && s.given == 5);
        arm("an empty cache gives back nothing", nvmtl_texrc_give_back(&s, out) == 0); }

    {   /* the counts: a texture is made, dies into the cache, is taken back, dies again and is aged out */
        nvmtl_texrc_state s; memset(&s, 0, sizeof s);
        const uint64_t b = 8 * MB;
        model_create_fresh(b);
        nvmtl_texrc_entry e = ent(a, 1, b); nvmtl_texrc_put(&s, &e, 1 * S, out);       /* park: nothing uncounted */
        arm("a parked image stays counted as allocated VRAM", counted == b && allocated == b);
        if (nvmtl_texrc_take(&s, &a, &got)) { counted -= got.bytes; counted += got.bytes; }  /* reuse hands the count to the commit */
        arm("a taken image is counted once, and no new memory was allocated", counted == b && allocated == b);
        nvmtl_texrc_put(&s, &got, 2 * S, out);
        const uint32_t n = nvmtl_texrc_age(&s, 4 * S, out);
        for (uint32_t k = 0; k < n; k++) model_free(out[k].bytes);
        arm("an aged-out image is uncounted when it is really freed", counted == 0 && allocated == 0); }

    printf(fails ? "FAILED %d\n" : "ok\n", fails);
    return fails != 0;
}
