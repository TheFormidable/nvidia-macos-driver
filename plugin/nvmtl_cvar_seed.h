/* nvmtl_cvar_seed.h - the size a compute kernel's creation-time pipeline is compiled for.
   newComputePipelineState compiles the kernel once to refuse one that NVK cannot build, as Metal requires
   (nvmtl_vk_compute_pipeline_create_g_lifetime_body). That pipeline was compiled without specialization, and since every
   dispatch binds a threadgroup-size variant specialized with SpecIds 0-3 (nvmtl_compute_variant), it was never bound: the
   first dispatch of any size compiled the kernel a second time. It is compiled instead with the specialization a dispatch
   of the translator's default size would use and starts the kernel's variant table, so a dispatch of that size compiles
   nothing more. The values are the module's own defaults, so the creation-time compile builds what it built before and
   refuses what it refused before. NVMTL_CVAR_SEED=0 creates it unspecialized with an empty table, as in 3.0.5.
   Pure C so tools/test_cvar_seed.c proves the rules on hand-made modules. */
#ifndef NVMTL_CVAR_SEED_H
#define NVMTL_CVAR_SEED_H
#include <stdint.h>
#include <stddef.h>

/* The specialization data of size l: SpecIds 0-2 the size, SpecId 3 set when a linearizable kernel's size is over the
   per-dimension limit w (nvmtl_vk_tg_reject lets such a size through only when its invocations fit). */
static inline void nvmtl_cvar_spec_data(const uint32_t l[3], uint32_t lin, const uint32_t w[3], uint32_t sd[4])
{
    sd[0] = l[0]; sd[1] = l[1]; sd[2] = l[2];
    sd[3] = (lin && (l[0] > w[0] || l[1] > w[1] || l[2] > w[2])) ? 1u : 0u;
}

/* The default value of the 32-bit scalar specialization constant decorated SpecId id: 1 and *v when the module has one,
   0 otherwise (no such SpecId, a constant that is not a plain 32-bit OpSpecConstant/True/False, a malformed module).
   Decorations and constants come before the first function, so the walk stops there. */
static inline int nvmtl_spirv_specid_default(const uint32_t *w, size_t words, uint32_t id, uint32_t *v)
{
    if (!w || words < 5 || w[0] != 0x07230203u) return 0;
    uint32_t target = 0; int have = 0;
    for (size_t i = 5; i < words; ) {
        const uint32_t wc = w[i] >> 16, op = w[i] & 0xffffu;
        if (!wc || i + wc > words) return 0;
        if (op == 54) return 0;                                                   /* OpFunction */
        if (op == 71 && wc == 4 && w[i + 2] == 1 && w[i + 3] == id) {   /* OpDecorate SpecId */
            /* a second constant with the same SpecId takes the same value, and its own default may differ */
            if (have) return 0;
            target = w[i + 1]; have = 1;
        }
        if (have && op == 50 && wc == 4 && w[i + 2] == target) { *v = w[i + 3]; return 1; }   /* OpSpecConstant, one word */
        if (have && (op == 48 || op == 49) && wc == 3 && w[i + 2] == target) { *v = op == 48; return 1; }   /* True / False */
        i += wc;
    }
    return 0;
}

/* The size the creation-time pipeline is specialized to: 1 and d[3] when SpecIds 0, 1 and 2 have defaults and SpecId 3
   is absent or defaults to 0, so that specializing to d with SpecId 3 = 0 is specializing to the module's own defaults,
   which Vulkan defines to be the same pipeline as no specialization. 0 when the module sizes its threadgroup otherwise
   (a fixed size, no SpecIds): the creation pipeline then stays unspecialized. */
static inline int nvmtl_cvar_seed_size(const uint32_t *w, size_t words, uint32_t d[3])
{
    uint32_t lin = 0;
    for (uint32_t k = 0; k < 3; k++) if (!nvmtl_spirv_specid_default(w, words, k, &d[k])) return 0;
    if (nvmtl_spirv_specid_default(w, words, 3, &lin) && lin) return 0;
    return 1;
}

/* A size that any dispatch accepts as it is: every dimension from 1 to the device's per-dimension limit w and its
   invocations within max_inv, so it is neither refused nor linearized (SpecId 3 stays 0). */
static inline int nvmtl_cvar_size_fits(const uint32_t d[3], const uint32_t w[3], uint32_t max_inv)
{
    for (int k = 0; k < 3; k++) if (!d[k] || d[k] > w[k]) return 0;
    return (uint64_t)d[0] * d[1] * d[2] <= max_inv;
}

#endif
