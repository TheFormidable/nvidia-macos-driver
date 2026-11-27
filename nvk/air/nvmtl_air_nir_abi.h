/*
 * NullMoth NVIDIA driver for macOS
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 *
 * The interface between the Metal plugin and NVK's AIR-to-NIR frontend (nvk_air_to_nir.c).
 *
 * The plugin hands NVK a kernel's AIR (the LLVM bitcode of a Metal function) and gets back a NIR shader built in
 * NVK's own copy of the compiler. It then creates the compute pipeline with that shader in
 * VkPipelineShaderStageNirCreateInfoMESA (module VK_NULL_HANDLE, pName "main", no specialization), with the layout it
 * uses for translated kernels: set 0 holds the buffer-address table at binding 640, a uniform buffer with a dynamic
 * offset (a storage buffer with NVMTL_AIR_NIR_TABLE_SSBO), and 48 bytes of push constants hold the dispatch grid as
 * the SPIR-V translator's kernels read it:
 *    words 0-2   threads_per_grid       (0x80000000 | threadgroup size: an indirect dispatch, read the group count)
 *    words 3-5   thread_base            added to the global invocation id
 *    words 6-8   threadgroup_base       added to the workgroup id
 *    words 9-11  threadgroups_per_grid  (0x80000000: an indirect dispatch, read the group count)
 * Every Metal buffer argument [[buffer(n)]] is read through entry n of the address table, a 64-bit GPU address, so the
 * kernel binds no storage-buffer descriptor.
 *
 * Textures and samplers use the bands of set 0 the plugin's set layout has and the translator's kernels bind
 * (translator/translator/docs/REFLECTION.md, plugin/nvmtl_vk.c binding_type):
 *    32 + n   [[texture(n)]] read or sampled: a sampled image (a uniform texel buffer for texture_buffer)
 *   480 + n   [[texture(n)]] with access::write or access::read_write: a storage image (a storage texel buffer for
 *             texture_buffer), declared without a format (the plugin tells the translator the device reads and writes
 *             storage images without one, nvmtl_translate_set_caps bits 0 and 1)
 *   160 + n   [[sampler(n)]], and each constexpr sampler at the slot the translator's reflection gives it (the first
 *             sampler slot no [[sampler(n)]] argument takes, in the order of their __air_sampler_state globals): the
 *             result lists them with their AIR state words, which the plugin binds as it binds the translator's
 *             StaticSampler entries
 * The result's masks say which of them the kernel reads.
 *
 * No LLVM is linked into NVK: the frontend opens the LLVM library named in the request (the plugin's air-runtime)
 * and finds its C API there.
 *
 * Both structs start with their own size, so the plugin and NVK can be built from different trees: a field added at
 * the end is invisible to an older caller, and a shorter struct from one is read only as far as it goes.
 */
#ifndef NVMTL_AIR_NIR_ABI_H
#define NVMTL_AIR_NIR_ABI_H

#include <stddef.h>
#include <stdint.h>

#define NVMTL_AIR_NIR_TABLE_BINDING 640u
#define NVMTL_AIR_NIR_PUSH_BYTES    48u
#define NVMTL_AIR_NIR_MAX_BUFFERS   31u   /* binding 31 of the translator's band is reserved, so slots 0-30 */

/* request.flags */
#define NVMTL_AIR_NIR_TABLE_SSBO    (1u << 0)   /* binding 640 is a storage buffer, not a dynamic uniform buffer */
#define NVMTL_AIR_NIR_NO_PASSES     (1u << 1)   /* do not run LLVM's inliner and SROA on the module first */
#define NVMTL_AIR_NIR_FC_VALUES     (1u << 2)   /* fc_* hold the function's complete set of function-constant values */

enum nvmtl_air_nir_status {
   NVMTL_AIR_NIR_OK          = 0,
   NVMTL_AIR_NIR_BAD_REQUEST = 1,   /* a field of the request is wrong, or the device is not NVK's */
   NVMTL_AIR_NIR_NO_LLVM     = 2,   /* the LLVM library would not open or lacks a function this needs */
   NVMTL_AIR_NIR_BAD_AIR     = 3,   /* the bitcode would not parse, or has no such kernel */
   NVMTL_AIR_NIR_UNSUPPORTED = 4,   /* the kernel uses something this frontend does not translate yet */
   NVMTL_AIR_NIR_NO_MEMORY   = 5,
};

struct nvmtl_air_nir_request {
   uint32_t size;                    /* sizeof(struct nvmtl_air_nir_request) */
   uint32_t flags;
   const char *llvm_library;         /* path of the LLVM shared library to read the bitcode with */
   const void *bitcode;              /* the function's AIR: an LLVM bitcode module, borrowed for the call */
   size_t bitcode_size;
   const char *entry;                /* the kernel's name; NULL takes the module's only kernel */
   uint32_t local_size[3];           /* threadgroup size the pipeline is compiled for */
   /* bytes of each [[threadgroup(n)]] argument, as setThreadgroupMemoryLength:atIndex: set them; a kernel with such an
    * argument whose length is 0 here is refused */
   uint32_t threadgroup_lengths[NVMTL_AIR_NIR_MAX_BUFFERS];

   /* Since version 2, read only with NVMTL_AIR_NIR_FC_VALUES and a size that reaches them. The function constants the
    * function was made with (newFunctionWithName:constantValues:), as the plugin hands them to the translator's
    * nvmtl_translate_fc: [[function_constant(index)]] index, payload size in bytes and payload (a bool is one byte, 0
    * or 1). A constant left out is not defined: air.is_function_constant_defined reads false, and a kernel that still
    * reads its value once the others are folded is refused (the translator reads it as 0). Without the flag a kernel
    * that reads any function constant is refused, as in version 1. */
   uint32_t fc_count;
   const uint32_t *fc_indices;
   const uint32_t *fc_sizes;
   const uint8_t *const *fc_payloads;
};
#define NVMTL_AIR_NIR_REQUEST_V1_SIZE 176u   /* the request without the fc_* fields */

struct nvmtl_air_nir_result {
   uint32_t size;                    /* sizeof(struct nvmtl_air_nir_result), set by the caller */
   int32_t status;                   /* enum nvmtl_air_nir_status */
   void *nir;                        /* nir_shader *, owned by the caller: nvmtl_air_nir_free_v1 */
   uint32_t buffer_mask;             /* Metal buffer slots the kernel reads through the address table */
   uint32_t shared_bytes;            /* threadgroup memory the kernel uses, static and arguments together */
   uint32_t scratch_bytes;           /* thread-private memory LLVM could not keep in registers */
   uint32_t instructions;            /* LLVM instructions translated */
   char message[256];                /* why it was refused, or a one-line summary on success */

   /* Since version 2, written when the caller's size reaches them. Bit n of word n / 32: */
   uint32_t texture_mask[4];         /* [[texture(n)]] read through binding 32 + n */
   uint32_t storage_mask[4];         /* [[texture(n)]] written or read-written through binding 480 + n */
   uint32_t sampler_mask;            /* sampler slots read through binding 160 + n: arguments and constexpr samplers */
   uint32_t static_sampler_count;
   struct {
      uint32_t slot;                 /* binding 160 + slot */
      uint32_t pad;
      uint64_t words[2];             /* the AIR state words of its __air_sampler_state global (filter, address, ...) */
   } static_samplers[16];
};
#define NVMTL_AIR_NIR_RESULT_V1_SIZE 288u    /* the result up to its message */

#ifdef __cplusplus
extern "C" {
#endif
/* VkDevice is NVK's. Returns result->status. */
typedef int (*nvmtl_air_to_nir_v1_fn)(void *device, const struct nvmtl_air_nir_request *request,
                                      struct nvmtl_air_nir_result *result);
typedef void (*nvmtl_air_nir_free_v1_fn)(void *nir);
#ifdef __cplusplus
}
#endif

#endif
