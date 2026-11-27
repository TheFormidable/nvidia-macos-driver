/*
 * NullMoth NVIDIA driver for macOS
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 *
 * nvmtl_air_nir.h - the parts of the plugin's AIR-to-NIR road for compute pipelines (NVMTLAirNir.m) that are pure C, so
 * tools/test_air_nir_plugin.c proves them without the plugin: the switch, the descriptor masks a result gives the
 * plugin's set-0 bands, the threadgroup sizes the road runs, the constexpr-sampler agreement with the translator, and
 * the shader-cache entry.
 *
 * nvmtl_air_nir_abi.h beside it is a copy of nvk/air/nvmtl_air_nir_abi.h (the plugin is built from plugin/ alone);
 * tools/test_air_nir_plugin.py holds the two to the same bytes.
 */
#ifndef NVMTL_AIR_NIR_H
#define NVMTL_AIR_NIR_H
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include "nvmtl_air_nir_abi.h"

/* NVMTL_AIR_NIR (environment or the settings file): the one exit. "0" keeps every compute pipeline on the SPIR-V road,
 * as before. Anything else, or unset, is on, except in WindowServer, which stays on the SPIR-V road for now. */
static inline int nvmtl_air_nir_switch(const char *value, const char *program)
{
    if (value && value[0] == '0') return 0;
    if (program && !strcmp(program, "WindowServer")) return 0;
    return 1;
}

/* The plugin's set-0 bands, by index into its binding table (plugin/nvmtl_vk.c NVMTL_NBUF, NVMTL_NTEX, NVMTL_NSAMP,
 * NVMTL_ADDRESS_BINDING): buffers 0-31, textures 32-159, samplers 160-191, the address table 192 (binding 640); the
 * storage textures (480 + n) are counted by stex_hi, not in the mask. */
#define NVMTL_AN_TEX0 32u
#define NVMTL_AN_SAMP0 160u
#define NVMTL_AN_TABLE 192u
#define NVMTL_AN_BIT(m, i) (((m)[(i) / 32] >> ((i) % 32)) & 1u)

/* What the dispatch writes for a kernel on this road, from its result: bmask (the bindings it reads: the table, each
 * sampled texture and sampler slot), stex_hi (one past its highest storage texture) and tbm (texel buffers: words 0-1
 * the texture band, 2-3 the storage band), as the SPIR-V road reads them from a module (nvmtl_spirv_bmask,
 * nvmtl_spirv_stex_hi, nvmtl_spirv_texel_mask). -1 for a result that names a texel buffer that is not a texture. */
static inline int nvmtl_air_nir_masks(const struct nvmtl_air_nir_result *r, uint64_t bmask[4], uint32_t *stex_hi, uint64_t tbm[4])
{
    memset(bmask, 0, 4 * sizeof *bmask); memset(tbm, 0, 4 * sizeof *tbm); *stex_hi = 0;
    bmask[NVMTL_AN_TABLE >> 6] |= 1ull << (NVMTL_AN_TABLE & 63);
    for (uint32_t i = 0; i < 128; i++) {
        if ((NVMTL_AN_BIT(r->texbuf_mask, i) && !NVMTL_AN_BIT(r->texture_mask, i)) ||
            (NVMTL_AN_BIT(r->stexbuf_mask, i) && !NVMTL_AN_BIT(r->storage_mask, i))) return -1;
        if (NVMTL_AN_BIT(r->texture_mask, i)) {
            const uint32_t k = NVMTL_AN_TEX0 + i;
            bmask[k >> 6] |= 1ull << (k & 63);
            if (NVMTL_AN_BIT(r->texbuf_mask, i)) tbm[i >> 6] |= 1ull << (i & 63);
        }
        if (NVMTL_AN_BIT(r->storage_mask, i)) {
            *stex_hi = i + 1;
            if (NVMTL_AN_BIT(r->stexbuf_mask, i)) tbm[2 + (i >> 6)] |= 1ull << (i & 63);
        }
    }
    for (uint32_t i = 0; i < 32; i++)
        if ((r->sampler_mask >> i) & 1u) { const uint32_t k = NVMTL_AN_SAMP0 + i; bmask[k >> 6] |= 1ull << (k & 63); }
    return 0;
}

/* A threadgroup size the road compiles: every dimension from 1 to the device's per-dimension limit w and at most
 * max_inv (and the frontend's 1024) invocations. Any other size (one the SPIR-V road linearizes) runs that road. */
static inline int nvmtl_air_nir_size_ok(const uint32_t l[3], const uint32_t w[3], uint32_t max_inv)
{
    for (int d = 0; d < 3; d++) if (!l[d] || l[d] > w[d]) return 0;
    const uint64_t n = (uint64_t)l[0] * l[1] * l[2];
    return n <= max_inv && n <= 1024;
}

/* The constexpr samplers are bound by the translator's reflection (StaticSampler entries, nvmtl_static_samplers): the
 * road is taken only when the result puts them at exactly the slots that reflection names. slots[] needs no order. */
static inline int nvmtl_air_nir_statics_match(const struct nvmtl_air_nir_result *r, const uint32_t *slots, unsigned n)
{
    uint64_t a = 0, b = 0;
    if (r->static_sampler_count > 32 || n > 32) return 0;
    for (unsigned k = 0; k < r->static_sampler_count; k++) { if (r->static_samplers[k].slot >= 32) return 0; a |= 1ull << r->static_samplers[k].slot; }
    for (unsigned k = 0; k < n; k++) { if (slots[k] >= 32) return 0; b |= 1ull << slots[k]; }
    return a == b && __builtin_popcountll(a) == (int)r->static_sampler_count && __builtin_popcountll(b) == (int)n;
}

/* A shader-cache entry (the plugin's spvcache folder, stage "nir1"): this header, the result as the frontend wrote it
 * (its nir pointer zero), then the serialized shader. A refusal is kept too, with its status and message and no
 * shader, so a process does not translate a kernel the frontend refused again. A refusal of the plugin's own (NVK
 * would not create the pipeline) has status NVMTL_AN_NO_PIPELINE. */
#define NVMTL_AN_ENTRY_MAGIC 0x31524e41u   /* "ANR1" */
#define NVMTL_AN_NO_PIPELINE 100
struct nvmtl_air_nir_entry_head {
    uint32_t magic;
    uint32_t result_size;   /* sizeof(struct nvmtl_air_nir_result) of the writer: another size is a miss */
    int32_t status;
    uint32_t blob_size;
};
static inline size_t nvmtl_air_nir_entry_size(size_t blob_size)
{
    return sizeof(struct nvmtl_air_nir_entry_head) + sizeof(struct nvmtl_air_nir_result) + blob_size;
}
/* out holds nvmtl_air_nir_entry_size(blob_size) bytes; returns that size */
static inline size_t nvmtl_air_nir_entry_pack(uint8_t *out, int32_t status, const struct nvmtl_air_nir_result *r,
                                              const void *blob, size_t blob_size)
{
    struct nvmtl_air_nir_entry_head h = { NVMTL_AN_ENTRY_MAGIC, (uint32_t)sizeof *r, status, (uint32_t)blob_size };
    struct nvmtl_air_nir_result copy = *r;
    copy.nir = NULL;
    memcpy(out, &h, sizeof h);
    memcpy(out + sizeof h, &copy, sizeof copy);
    if (blob_size) memcpy(out + sizeof h + sizeof copy, blob, blob_size);
    return nvmtl_air_nir_entry_size(blob_size);
}
/* 0 and the parts for an entry this build wrote (a shader for status OK, none for a refusal), -1 for anything else */
static inline int nvmtl_air_nir_entry_unpack(const uint8_t *in, size_t n, int32_t *status, struct nvmtl_air_nir_result *r,
                                             const uint8_t **blob, size_t *blob_size)
{
    struct nvmtl_air_nir_entry_head h;
    if (!in || n < sizeof h + sizeof *r) return -1;
    memcpy(&h, in, sizeof h);
    if (h.magic != NVMTL_AN_ENTRY_MAGIC || h.result_size != sizeof *r || n != nvmtl_air_nir_entry_size(h.blob_size)) return -1;
    if ((h.status == NVMTL_AIR_NIR_OK) != (h.blob_size != 0)) return -1;
    memcpy(r, in + sizeof h, sizeof *r);
    if (r->size != sizeof *r) return -1;
    r->nir = NULL;
    r->message[sizeof r->message - 1] = 0;
    *status = h.status;
    *blob = h.blob_size ? in + sizeof h + sizeof *r : NULL;
    *blob_size = h.blob_size;
    return 0;
}
#endif
