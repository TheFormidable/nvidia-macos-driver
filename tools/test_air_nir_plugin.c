/* SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
   test_air_nir_plugin.c - the pure parts of the plugin's AIR-to-NIR road (plugin/nvmtl_air_nir.h):
   the switch, the descriptor masks a frontend result gives the plugin's set-0 bands, the threadgroup sizes the road
   runs, the constexpr-sampler agreement with the translator's reflection, and the shader-cache entry, which must read
   back exactly what was written and turn away anything else (another build's layout, a cut or changed file).
   Build and run anywhere (no GPU, no Metal):  cc -std=c11 -Wall -Wextra -I plugin tools/test_air_nir_plugin.c -o /tmp/test_air_nir_plugin && /tmp/test_air_nir_plugin
   Each arm is a sentence the rule must hold to. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "nvmtl_air_nir.h"

static int fails;
static void arm(const char *what, int ok) { printf("%s  %s\n", ok ? "ok  " : "FAIL", what); if (!ok) fails++; }
#define SET(m, i) ((m)[(i) / 32] |= 1u << ((i) % 32))
#define HAS(m, i) (((m)[(i) >> 6] >> ((i) & 63)) & 1ull)

int main(void)
{
    /* the switch */
    arm("unset is on", nvmtl_air_nir_switch(NULL, "Safari") == 1);
    arm("NVMTL_AIR_NIR=0 is off", nvmtl_air_nir_switch("0", "Safari") == 0);
    arm("NVMTL_AIR_NIR=1 is on (only 0 turns it off)", nvmtl_air_nir_switch("1", "Safari") == 1);
    arm("WindowServer stays on the SPIR-V road, unset", nvmtl_air_nir_switch(NULL, "WindowServer") == 0);
    arm("WindowServer stays on the SPIR-V road even with NVMTL_AIR_NIR=1 (the switch is only a way out)", nvmtl_air_nir_switch("1", "WindowServer") == 0);
    arm("a process whose name is unknown is on", nvmtl_air_nir_switch(NULL, NULL) == 1);

    /* the masks */
    struct nvmtl_air_nir_result r;
    memset(&r, 0, sizeof r); r.size = sizeof r;
    uint64_t b[4], tb[4]; uint32_t hi = 99;
    arm("a kernel with only buffers writes the table and nothing else", nvmtl_air_nir_masks(&r, b, &hi, tb) == 0 && hi == 0 &&
        HAS(b, NVMTL_AN_TABLE) && __builtin_popcountll(b[0]) + __builtin_popcountll(b[1]) + __builtin_popcountll(b[2]) + __builtin_popcountll(b[3]) == 1 &&
        !(tb[0] | tb[1] | tb[2] | tb[3]));
    SET(r.texture_mask, 0); SET(r.texture_mask, 127); SET(r.texbuf_mask, 127);
    SET(r.storage_mask, 5); SET(r.storage_mask, 70); SET(r.stexbuf_mask, 70);
    r.sampler_mask = (1u << 0) | (1u << 31);
    arm("textures, samplers, storage textures and texel buffers land in their bands",
        nvmtl_air_nir_masks(&r, b, &hi, tb) == 0 && HAS(b, 32) && HAS(b, 159) && HAS(b, 160) && HAS(b, 191) && HAS(b, 192) &&
        !HAS(b, 31) && !HAS(b, 33) && hi == 71 && ((tb[1] >> 63) & 1) && !(tb[0] & 1) && ((tb[3] >> 6) & 1) && !((tb[2] >> 5) & 1));
    arm("no buffer binding of the band 0-31 is written (buffers go through the table)", (b[0] & 0xffffffffull) == 0);
    struct nvmtl_air_nir_result bad = r; SET(bad.texbuf_mask, 3);
    arm("a texel buffer that is not a texture is refused", nvmtl_air_nir_masks(&bad, b, &hi, tb) == -1);
    bad = r; SET(bad.stexbuf_mask, 9);
    arm("a storage texel buffer that is not a storage texture is refused", nvmtl_air_nir_masks(&bad, b, &hi, tb) == -1);

    /* sizes */
    const uint32_t w[3] = { 1024, 1024, 64 };
    arm("64x1x1 runs", nvmtl_air_nir_size_ok((uint32_t[3]){ 64, 1, 1 }, w, 1024));
    arm("1024x1x1 runs", nvmtl_air_nir_size_ok((uint32_t[3]){ 1024, 1, 1 }, w, 1024));
    arm("1x1x128 (over the per-dimension limit, which the SPIR-V road linearizes) does not", !nvmtl_air_nir_size_ok((uint32_t[3]){ 1, 1, 128 }, w, 1024));
    arm("32x32x2 (2048 invocations) does not", !nvmtl_air_nir_size_ok((uint32_t[3]){ 32, 32, 2 }, w, 2048));
    arm("a zero dimension does not", !nvmtl_air_nir_size_ok((uint32_t[3]){ 0, 1, 1 }, w, 1024));

    /* constexpr samplers */
    memset(&r, 0, sizeof r); r.size = sizeof r;
    r.static_sampler_count = 2; r.static_samplers[0].slot = 1; r.static_samplers[1].slot = 2;
    arm("the same slots in another order agree", nvmtl_air_nir_statics_match(&r, (uint32_t[]){ 2, 1 }, 2));
    arm("another slot does not", !nvmtl_air_nir_statics_match(&r, (uint32_t[]){ 1, 3 }, 2));
    arm("one fewer does not", !nvmtl_air_nir_statics_match(&r, (uint32_t[]){ 1 }, 1));
    arm("a slot twice does not stand for two", !nvmtl_air_nir_statics_match(&r, (uint32_t[]){ 1, 1 }, 2));
    r.static_samplers[1].slot = 1;
    arm("a result with two samplers in one slot does not agree with one", !nvmtl_air_nir_statics_match(&r, (uint32_t[]){ 1 }, 1));

    /* the cache entry */
    memset(&r, 0, sizeof r); r.size = sizeof r; r.status = NVMTL_AIR_NIR_OK; r.nir = (void *)0x1234; r.buffer_mask = 0x5;
    r.global_align = 16; snprintf(r.message, sizeof r.message, "k: 9 instructions");
    const uint8_t blob[7] = { 1, 2, 3, 4, 5, 6, 7 };
    uint8_t *e = malloc(nvmtl_air_nir_entry_size(sizeof blob));
    const size_t n = nvmtl_air_nir_entry_pack(e, NVMTL_AIR_NIR_OK, &r, blob, sizeof blob);
    struct nvmtl_air_nir_result back; int32_t st = -1; const uint8_t *bb = NULL; size_t bn = 0;
    arm("an entry reads back: status, result and shader", n == nvmtl_air_nir_entry_size(sizeof blob) &&
        nvmtl_air_nir_entry_unpack(e, n, &st, &back, &bb, &bn) == 0 && st == NVMTL_AIR_NIR_OK && bn == sizeof blob &&
        !memcmp(bb, blob, bn) && back.buffer_mask == 0x5 && back.global_align == 16 && !strcmp(back.message, r.message));
    arm("the shader pointer is never stored nor read back", back.nir == NULL);
    arm("a cut entry is a miss", nvmtl_air_nir_entry_unpack(e, n - 1, &st, &back, &bb, &bn) == -1);
    uint8_t *x = malloc(n + 1); memcpy(x, e, n); x[n] = 0;
    arm("an entry with bytes after it is a miss", nvmtl_air_nir_entry_unpack(x, n + 1, &st, &back, &bb, &bn) == -1);
    memcpy(x, e, n); x[0] ^= 1;
    arm("another magic is a miss", nvmtl_air_nir_entry_unpack(x, n, &st, &back, &bb, &bn) == -1);
    memcpy(x, e, n); ((struct nvmtl_air_nir_entry_head *)x)->result_size += 8;
    arm("a result of another size (another build's layout) is a miss", nvmtl_air_nir_entry_unpack(x, n, &st, &back, &bb, &bn) == -1);
    memcpy(x, e, n); ((struct nvmtl_air_nir_entry_head *)x)->status = NVMTL_AIR_NIR_UNSUPPORTED;
    arm("a refusal that carries a shader is a miss", nvmtl_air_nir_entry_unpack(x, n, &st, &back, &bb, &bn) == -1);
    memcpy(x, e, n); ((struct nvmtl_air_nir_result *)(x + sizeof(struct nvmtl_air_nir_entry_head)))->size = 8;
    arm("a result whose own size field is wrong is a miss", nvmtl_air_nir_entry_unpack(x, n, &st, &back, &bb, &bn) == -1);
    free(x); free(e);
    memset(&r, 0, sizeof r); r.size = sizeof r; snprintf(r.message, sizeof r.message, "argument buffers");
    e = malloc(nvmtl_air_nir_entry_size(0));
    const size_t rn = nvmtl_air_nir_entry_pack(e, NVMTL_AIR_NIR_UNSUPPORTED, &r, NULL, 0);
    arm("a refusal is kept with its message and no shader",
        nvmtl_air_nir_entry_unpack(e, rn, &st, &back, &bb, &bn) == 0 && st == NVMTL_AIR_NIR_UNSUPPORTED && bn == 0 && !bb &&
        !strcmp(back.message, "argument buffers"));
    ((struct nvmtl_air_nir_entry_head *)e)->status = NVMTL_AIR_NIR_OK;
    arm("a success without a shader is a miss", nvmtl_air_nir_entry_unpack(e, rn, &st, &back, &bb, &bn) == -1);
    free(e);
    arm("the ABI's version-2 result ends where version 3 starts", offsetof(struct nvmtl_air_nir_result, global_align) == NVMTL_AIR_NIR_RESULT_V2_SIZE);
    arm("the ABI's version-3 result ends where version 4 starts", offsetof(struct nvmtl_air_nir_result, stage) == NVMTL_AIR_NIR_RESULT_V3_SIZE);
    arm("the ABI's version-3 request ends where version 4 starts", offsetof(struct nvmtl_air_nir_request, varying_count) == NVMTL_AIR_NIR_REQUEST_V3_SIZE);
    arm("a stage is one of three, and a fragment stage's set is the plugin's second",
        NVMTL_AIR_NIR_STAGE_KERNEL == 0 && NVMTL_AIR_NIR_STAGE_VERTEX == 1 && NVMTL_AIR_NIR_STAGE_FRAGMENT == 2 && NVMTL_AIR_NIR_FRAGMENT_SET == 1);
    /* a render stage's masks land in the same bands (its set is chosen by the pipeline layout, not by the masks) */
    memset(&r, 0, sizeof r); r.size = sizeof r; r.stage = NVMTL_AIR_NIR_STAGE_FRAGMENT;
    SET(r.texture_mask, 0); r.sampler_mask = 1u << 1;
    arm("a fragment result's texture and sampler land in the bands a kernel's do",
        nvmtl_air_nir_masks(&r, b, &hi, tb) == 0 && HAS(b, 32) && HAS(b, 161) && HAS(b, NVMTL_AN_TABLE) && hi == 0);

    printf(fails ? "FAILED %d\n" : "ok\n", fails);
    return fails ? 1 : 0;
}
