/*
 * NullMoth NVIDIA driver for macOS
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 *
 * The interface between the Metal plugin and NVK's AIR-to-NIR frontend (nvk_air_to_nir.c).
 *
 * The plugin hands NVK a function's AIR (the LLVM bitcode of a Metal function) and gets back a NIR shader built in
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
 * Since version 4 the function may also be a vertex or a fragment function (found in !air.vertex or !air.fragment), for
 * a render pipeline the plugin creates with its render layout: set 0 is the vertex stage's (the same bands as above,
 * the table at 640), set 1 the fragment stage's (the same bands), set 2 the heap; no push constants are read. Vertex
 * inputs ([[attribute(n)]] of a stage_in) are read at Location n, as the vertex descriptor binds them (the plugin picks
 * each attribute's VkFormat by the component kind the result names, as it reads a SPIR-V module's); the vertex outputs
 * and fragment inputs (varyings) take the Locations the request gives by their user semantic, every varying as 32-bit
 * components (a half as a float, an 8- or 16-bit integer or a bool as a 32-bit integer, flat), so the two stages agree
 * because both come from this frontend. Builtins are Vulkan's (VertexIndex, InstanceIndex, BaseVertex, BaseInstance,
 * Position, PointSize, ClipDistance, Layer, ViewportIndex, FragCoord, FrontFacing, PointCoord, SampleId, SampleMask,
 * FragDepth), as the translator's stages read and write them.
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
/* Since version 3 (an older NVK ignores them, and then refuses or translates as without them): */
#define NVMTL_AIR_NIR_TEXT_IR       (1u << 3)   /* bitcode is LLVM text IR (as the plugin keeps AIR), bitcode_size bytes */
#define NVMTL_AIR_NIR_TG_DEFAULT    (1u << 4)   /* a [[threadgroup(n)]] argument whose length is 0 gets the translator's
                                                 * default: 512 elements of its type, at most 32768 bytes, at least one */
#define NVMTL_AIR_NIR_UNCHECKED     (1u << 5)   /* the pipeline is created with buffer robustness off
                                                 * (VkPipelineRobustnessCreateInfoEXT, storage and uniform buffers
                                                 * DISABLED), so the table is addressed as such a pipeline lowers it */
#define NVMTL_AIR_NIR_ALIGN4        (1u << 6)   /* no device or constant buffer access promises more than 4-byte
                                                 * alignment: the build for a buffer bound at an address its kernel's
                                                 * accesses are not aligned to (the plugin's align guard) */
/* Since version 4: */
#define NVMTL_AIR_NIR_VARYINGS      (1u << 7)   /* varying_* give the Location of every vertex output and fragment input
                                                 * by its user semantic; a varying they do not name is refused. Without
                                                 * it each takes the translator's own (fragment inputs in their order,
                                                 * vertex outputs by air.location_index or their order). */
/* Since version 5: */
#define NVMTL_AIR_NIR_REFLECT       (1u << 8)   /* the result carries the function's reflection (result.reflection) */
#define NVMTL_AIR_NIR_SAMPLER_STATES (1u << 9)  /* sampler_state_* give the state of argument samplers the dispatch or
                                                 * draw binds (a pixel-coordinate one is sampled as the translator's
                                                 * with_runtime_sampler variant samples it) */
/* Since version 6: */
#define NVMTL_AIR_NIR_COLOR_INPUTS  (1u << 10)  /* a fragment function's [[color(n)]] inputs (framebuffer fetch) are read
                                                 * as the translator reads them: an input attachment at set
                                                 * NVMTL_AIR_NIR_FETCH_SET, binding NVMTL_AIR_NIR_FETCH_BINDING + n,
                                                 * input attachment index n (result.color_inputs names them). Without it
                                                 * such a function is refused, as before. */

/* result.stage */
#define NVMTL_AIR_NIR_STAGE_KERNEL   0u
#define NVMTL_AIR_NIR_STAGE_VERTEX   1u
#define NVMTL_AIR_NIR_STAGE_FRAGMENT 2u
#define NVMTL_AIR_NIR_FRAGMENT_SET   1u          /* the descriptor set a fragment stage's resources are in */
#define NVMTL_AIR_NIR_FETCH_SET      3u          /* [[color(n)]] inputs: the set the plugin binds the pass's colour views at */
#define NVMTL_AIR_NIR_FETCH_BINDING  192u        /* ... at binding 192 + n, an input attachment (the translator's band) */

/* result.fragment_flags */
#define NVMTL_AIR_NIR_FS_DEPTH        (1u << 0)  /* writes [[depth(any|greater|less)]] */
#define NVMTL_AIR_NIR_FS_SAMPLE_MASK  (1u << 1)  /* writes [[sample_mask]] */
#define NVMTL_AIR_NIR_FS_DISCARD      (1u << 2)  /* calls discard_fragment (a demote, as the translator's) */
#define NVMTL_AIR_NIR_FS_EARLY_TESTS  (1u << 3)  /* [[early_fragment_tests]] */
#define NVMTL_AIR_NIR_FS_PER_SAMPLE   (1u << 4)  /* reads [[sample_id]] or a sample-interpolated varying */

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

   /* Since version 4, read only with NVMTL_AIR_NIR_VARYINGS and a size that reaches them: a vertex or fragment
    * function's varyings. varying_keys[i] is a user semantic as the AIR names it (air.vertex_output or
    * air.fragment_input: user(x) for [[user(x)]], the compiler's generated(...) name for the others; the name
    * (air.arg_name) of one without a semantic), varying_locations[i] the Location it takes (0 to 31): the translator's
    * reflection's for a vertex output, and for a fragment input the vertex output's Location of the same semantic, as
    * the plugin links them (nvmtl_varying_link). */
   uint32_t varying_count;
   const char *const *varying_keys;
   const uint32_t *varying_locations;

   /* Since version 5, read only with NVMTL_AIR_NIR_SAMPLER_STATES and a size that reaches them: the state of the
    * [[sampler(n)]] arguments the pipeline is bound with, as the plugin hands them to the translator's
    * nvmtl_translate_sampler (the AIR constexpr-sampler word layout: filters, address modes, coordinates at bit 15,
    * ...). An argument sampler given here with pixel coordinates is sampled as such; one not given is a normalized
    * one, as before. */
   uint32_t sampler_state_count;
   const uint32_t *sampler_state_slots;     /* [[sampler(n)]] */
   const uint64_t *sampler_state_words;     /* two words per sampler */
};
#define NVMTL_AIR_NIR_REQUEST_V1_SIZE 176u   /* the request without the fc_* fields */
#define NVMTL_AIR_NIR_REQUEST_V3_SIZE 208u   /* the request without the varying_* fields */
#define NVMTL_AIR_NIR_REQUEST_V4_SIZE 232u   /* the request without the sampler_state_* fields */

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
   uint32_t texbuf_mask[4];          /* the texture_mask bits that are texture_buffers (uniform texel buffers) */
   uint32_t stexbuf_mask[4];         /* the storage_mask bits that are texture_buffers (storage texel buffers) */
   uint32_t sampler_mask;            /* sampler slots read through binding 160 + n: arguments and constexpr samplers */
   uint32_t static_sampler_count;
   struct {
      uint32_t slot;                 /* binding 160 + slot */
      uint32_t pad;
      uint64_t words[2];             /* the AIR state words of its __air_sampler_state global (filter, address, ...) */
   } static_samplers[32];            /* every constexpr sampler of the module, in slot order */

   /* Since version 3, written when the caller's size reaches it: */
   uint32_t global_align;            /* the largest alignment a device or constant buffer access promises (bytes) */
   uint32_t reserved;

   /* Since version 4, written when the caller's size reaches them: */
   uint32_t stage;                   /* NVMTL_AIR_NIR_STAGE_* */
   uint32_t vertex_inputs;           /* vertex: bit n, [[attribute(n)]] is read at Location n */
   uint8_t vertex_input_kind[32];    /* its components: 1 float, 2 signed integer, 3 unsigned integer (the plugin picks the
                                      * attribute's VkFormat by it, as nvmtl_spv_input_kinds reads a module) */
   uint32_t varyings;                /* the Locations the vertex writes or the fragment reads */
   uint32_t color_outputs;           /* fragment: bit n, [[color(n)]] is written */
   uint32_t fragment_flags;          /* NVMTL_AIR_NIR_FS_* */
   uint32_t reserved4;

   /* Since version 5, written with NVMTL_AIR_NIR_REFLECT when the caller's size reaches them and the function is
    * translated: the function's reflection as the translator's nvmtl_reflect gives it (JSON, the keys and values the
    * plugin reads: bindings, varyings, vertex_attributes, local_size, ...), malloc'd (the caller frees it with
    * free()), reflection_size bytes without a terminating 0 (one follows); NULL and why in reflection_why when this
    * frontend does not model what the function's reflection would hold. */
   char *reflection;
   size_t reflection_size;
   char reflection_why[128];

   /* Since version 6, written when the caller's size reaches them: */
   uint32_t color_inputs;            /* fragment, with NVMTL_AIR_NIR_COLOR_INPUTS: bit n, a [[color(n)]] input is declared
                                      * (read or not, as the translator declares every one): the plugin binds the open
                                      * pass's colour view n at set 3, binding 192 + n, as for the SPIR-V road's module */
   uint32_t reserved6;
};
#define NVMTL_AIR_NIR_RESULT_V1_SIZE 288u    /* the result up to its message */
#define NVMTL_AIR_NIR_RESULT_V2_SIZE 1128u   /* the result up to global_align */
#define NVMTL_AIR_NIR_RESULT_V3_SIZE 1136u   /* the result up to stage */
#define NVMTL_AIR_NIR_RESULT_V4_SIZE 1192u   /* the result up to reflection */
#define NVMTL_AIR_NIR_RESULT_V5_SIZE 1336u   /* the result up to color_inputs */

#ifdef __cplusplus
extern "C" {
#endif
/* VkDevice is NVK's. Returns result->status. */
typedef int (*nvmtl_air_to_nir_v1_fn)(void *device, const struct nvmtl_air_nir_request *request,
                                      struct nvmtl_air_nir_result *result);
typedef void (*nvmtl_air_nir_free_v1_fn)(void *nir);
/* Since version 3. A copy of nir (a result's) for another threadgroup size: the frontend reads the size only through
 * the shader's workgroup size, so the copy is the kernel translated for local_size. NULL when out of memory or the
 * size is not 1 to 1024 invocations. Freed with nvmtl_air_nir_free_v1. */
typedef void *(*nvmtl_air_nir_clone_v1_fn)(void *nir, const uint32_t local_size[3]);
/* Since version 3. nir written as a blob (nir_serialize, names kept) into *data, malloc'd (the caller frees it with
 * free()), and read back into a shader of the device's compiler. Only a blob this same NVK wrote may be read: the
 * format is NVK's build's own. 0 / non-NULL on success. */
typedef int (*nvmtl_air_nir_serialize_v1_fn)(void *nir, void **data, size_t *size);
typedef void *(*nvmtl_air_nir_deserialize_v1_fn)(void *device, const void *data, size_t size);
/* Since version 5. The version of the request and result this NVK reads and writes (NVMTL_AIR_NIR_VERSION); an NVK
 * without the export is version 4 or older, and ignores NVMTL_AIR_NIR_REFLECT and NVMTL_AIR_NIR_SAMPLER_STATES (the
 * plugin then neither leaves the translator out nor hands it a sampler's state). A version-5 NVK ignores
 * NVMTL_AIR_NIR_COLOR_INPUTS and refuses [[color(n)]] inputs. */
typedef int (*nvmtl_air_nir_version_v1_fn)(void);
#define NVMTL_AIR_NIR_VERSION 6
#ifdef __cplusplus
}
#endif

#endif
