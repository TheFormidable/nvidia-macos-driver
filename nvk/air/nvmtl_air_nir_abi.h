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
};

struct nvmtl_air_nir_result {
   uint32_t size;                    /* sizeof(struct nvmtl_air_nir_result), set by the caller */
   int32_t status;                   /* enum nvmtl_air_nir_status */
   void *nir;                        /* nir_shader *, owned by the caller: nvmtl_air_nir_free_v1 */
   uint32_t buffer_mask;             /* Metal buffer slots the kernel reads through the address table */
   uint32_t shared_bytes;            /* threadgroup memory the kernel uses, static and arguments together */
   uint32_t scratch_bytes;           /* thread-private memory LLVM could not keep in registers */
   uint32_t instructions;            /* LLVM instructions translated */
   char message[256];                /* why it was refused, or a one-line summary on success */
};

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
