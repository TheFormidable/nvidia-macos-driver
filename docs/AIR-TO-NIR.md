# Metal AIR to NIR, without SPIR-V

The plugin runs a Metal function on NVK by translating its AIR (the LLVM bitcode inside a metallib) to SPIR-V with
the translator (`translator/`), which NVK then turns into NIR and NAK into the GPU's code. `nvk/air` is a second
road for compute kernels that skips SPIR-V: NVK reads the AIR itself and builds the NIR directly.

Phase 1 (this tree) is the frontend and its test on Linux. Nothing calls it yet: the plugin still translates every
kernel to SPIR-V, so the driver behaves exactly as before.

## Pieces

| file | what |
|---|---|
| `nvk/air/nvk_air_to_nir.c` | the frontend, compiled into NVK (`libvulkan_nouveau.dylib`) |
| `nvk/air/nvmtl_air_nir_abi.h` | the interface between the plugin and NVK: request, result, layout contract |
| `nvk/nvk-air-nir.patch` | adds both to NVK's build; last in the 3.0 chain of `build/build_nvk.sh`, which copies the files in |
| `tools/air-nir/` | `run.sh`, `airtest.c` and two fixtures: the Linux test |
| `tools/air-nir/metal/`, `airrun.c`, `run-mac.sh` | the test on the card (macOS): Metal kernels, run and checked against the CPU |
| `tools/air-nir/airsweep.c`, `sweep.py` | every compute kernel of the plugin's AIR cache through the frontend and NAK, no dispatch |

## How it works

- NVK exports `nvmtl_air_to_nir_v1(VkDevice, request, result)` and `nvmtl_air_nir_free_v1(nir)`. The request carries
  the bitcode, the kernel's name, the threadgroup size the pipeline is for, the length of each `[[threadgroup(n)]]`
  argument, the path of an LLVM library and, since version 2, the function's function-constant values (below).
  Both structs start with their size: a version-1 request (176 bytes) and a version-1 result (288 bytes, up to its
  message) are read and written as far as they go.
- NVK links no LLVM (it is built with `-Dllvm=disabled`). The frontend opens the library the request names (the
  plugin's `air-runtime/libLLVM.dylib`, which already reads AIR) once per process and uses its C API to parse the
  bitcode, inline every function the kernel calls (each is marked `alwaysinline` first: Apple's compiler builds with
  `optsize`, whose inlining threshold leaves calls behind) and run SROA, and walk the kernel.
- It builds NIR with `nir_builder` in NVK's own copy of the compiler and returns the `nir_shader`. The caller creates
  the pipeline with it in `VkPipelineShaderStageNirCreateInfoMESA` (module `VK_NULL_HANDLE`, `pName` "main"), which
  Mesa's pipeline code already accepts: it clones the shader, hashes it for the cache and compiles it as it would
  one from SPIR-V.
- The result reads its buffers and grid as the translator's kernels do, so the plugin binds both the same way: set 0
  binding 640 is the buffer-address table (a dynamic uniform buffer, or a storage buffer with
  `NVMTL_AIR_NIR_TABLE_SSBO`), one 64-bit GPU address per `[[buffer(n)]]`, and 48 bytes of push constants hold
  `threads_per_grid`, `thread_base`, `threadgroup_base` and `threadgroups_per_grid` (bit 31 set: an indirect
  dispatch, read the group count).
- The CFG goes in unstructured (`nir_goto`/`nir_goto_if`, LLVM's blocks in breadth-first order) and
  `nir_lower_goto_ifs` structures it; phis become local variables that `nir_lower_vars_to_ssa` folds back.
- Structures and arrays held as values (`insertvalue`, `extractvalue`, and loads, stores, phis and selects of them)
  are trees of their elements, each its own value.
- Textures and samplers use the bands of set 0 the plugin's set layout has and the translator's kernels bind
  (`translator/translator/docs/REFLECTION.md`, `binding_type` in `plugin/nvmtl_vk.c`): a texture that is read or
  sampled is a sampled image at binding `32 + n` (a `texture_buffer` a uniform texel buffer) and is read with
  `txf`/`txl`/`txd`/`tg4`; a texture with `access::write` or `read_write` is a storage image at `480 + n` declared
  without a format, as the plugin tells the translator to declare them (`nvmtl_translate_set_caps` bits 0 and 1),
  read and written with image loads and stores (a `texture_buffer` a storage texel buffer); an argument sampler is
  `160 + n`; each constexpr sampler takes the slot the translator's reflection gives it (`add_static_samplers`: the
  first sampler slot no `[[sampler(n)]]` argument takes, in the order of their `__air_sampler_state` names) and is
  listed in the result with its two AIR state words, for the plugin to bind as it binds the translator's
  `StaticSampler` entries. The result's masks name every texture, storage-texture, texel-buffer and sampler binding
  the kernel reads.
- Function constants (`NVMTL_AIR_NIR_FC_VALUES`, request version 2): the values the function was made with, keyed by
  `[[function_constant(index)]]` with a payload of the constant's size, as the plugin hands them to the translator's
  `nvmtl_translate_fc`. Each given `MTL_FC_INIT_n` global becomes a constant, `air.is_function_constant_defined` a
  constant (false for one the request leaves out), and the module's static initializer (`air.static_init`) is run
  by a small evaluator, so the globals it fills (the constants' copies, the `...Defined` flags,
  `__metal_implicit_fc_pred_*`) become the constants it leaves; SCCP, instsimplify, simplifycfg and ADCE then fold
  what they decide. A kernel that still reads a constant the request does not give is refused, naming it (the
  translator reads it as 0); without the flag a kernel that reads any function constant is refused. An argument
  that exists only under a function constant that is false is not bound.
- Memory: device and constant pointers are 64-bit addresses (`load_global`, `store_global`, `global_atomic`);
  threadgroup pointers are offsets into shared memory, the arguments first in index order and the kernel's static
  variables after them; `alloca`s LLVM could not remove are fixed slots of scratch memory; constant tables
  (address-space-2 globals with a value) go into the shader's constant data.

The design follows the one Stela showed works (an AIR-to-NIR compiler for NVK, GPL-2.0-or-later, read from its tree
of 2026-11-22). No code of Stela is used: this tree is under PolyForm Noncommercial and ships NVK, so GPL code
compiled into it could not be published.

## What it translates

Compute kernels with: buffer arguments in address spaces 1 and 2 (and OpenCL's `air.constant`), threadgroup
arguments, the thread and grid builtins (positions, indices, sizes, SIMD-group index and width; OpenCL's
`get_global_id` family); integer and float arithmetic, comparisons, `select`, casts, `air.convert.*`, vectors of 1
to 4, 8 and 16 components, odd integer widths SROA leaves (`i24`, `i48`: kept zero-extended in the next width);
branches, `switch`, loops and phis; loads and stores of every kind of memory, `memcpy`/`memset` of a fixed length;
threadgroup and SIMD-group barriers and fences; device and threadgroup atomics (add, sub, min, max, and, or, xor,
exchange, compare-exchange, load, store; float add on device memory); most of Metal's math library
(`air.[fast_]<fn>`) and the matching LLVM intrinsics; SIMD-group and quad functions (`simd_sum` and the other
reductions, prefix sums, shuffles, broadcast, ballot, all, any) on the warp of 32; structures and arrays held as
values; function constants with their values; textures (`texture1d`/`2d`/`2d_array`/`3d`/`cube`/`cube_array`,
`texture_buffer`, `depth2d`/`depth2d_array`/`depthcube`) read at a level, sampled at a level or with gradients,
gathered, compared (`depth2d`), written and read-written (storage), fenced, and their sizes and level counts;
argument and constexpr samplers.

Where NAK has no instruction for a NIR operation and does not ask NIR to lower it (`[u|i][r]hadd`, `fisnormal`),
the frontend writes the expansion itself: a NIR opcode that reaches NAK unsupported is a compiler panic and a failed
pipeline, not a refusal.

Everything else is refused with a message, never guessed (`result.message`, status `NVMTL_AIR_NIR_UNSUPPORTED`):
argument buffers, imageblocks, ray tracing, function constants read without their values, function pointers and
recursive calls, doubles, run-time-sized `alloca`s, a `select` or phi between two kinds of memory, vertex, fragment
and mesh functions; and of textures: multisampled ones, a written cube or depth texture and a texel read of a cube
(the translator goes through a 2D-array view of them), sampling an integer texture (the translator emulates it with
fetches), a bias in a kernel, a minimum-LOD clamp, an offset known only at run time, a write or a storage read at a
level other than 0, `air.is_null_texture`/`get_null_texture`, and constexpr samplers with pixel coordinates,
bicubic filtering or min/max reduction (the translator emulates the first two).

An argument sampler's state is not known when the kernel is translated: the frontend treats it as one with
normalized coordinates, as a sampler the plugin creates from a normalized `MTLSamplerState` is. A pipeline bound
with a pixel-coordinate sampler needs that state in the request (the translator takes it through
`with_runtime_sampler`), which phase 3 adds.

Math functions compiled without fast math (`-fno-fast-math`) are translated to the same hardware approximations as
the fast ones (`MUFU`): within the bounds `airrun` checks for the arguments it uses, not Metal's ulp limits for
precise math (a `sin` of a large argument is the main difference).

## Testing it on Linux

`tools/air-nir/run.sh` runs the frontend and NAK on the host build of this NVK on Mesa's nouveau drm-shim as an
RTX 5050 (shader model 120), as `tools/nak-corpus` does. With no arguments it takes its two fixtures and every kernel
among the translator's LLVM fixtures. On 2026-11-27: 62 files, 48 compiled to SM120 code and 14 refused (12 are the
ray-tracing fixtures, which use acceleration structures or argument buffers or hold several kernels; one calls through
a function pointer; one selects between a constant table and a buffer), none failed. The drm-shim does not run the code: this checks that the NIR is valid and compiles, not what it computes. The card round below changed the frontend and has not been measured on Linux again; `sweep.py` on the Mac, over the
same fixtures one kernel at a time, gives 49 pipelines and 20 refusals (`pick_fc` is now among them: it reads a
function constant) and one file LLVM 21 does not parse.

## Testing on the card (macOS)

`tools/air-nir/run-mac.sh NVK.dylib [TEST...]` compiles the kernels in `tools/air-nir/metal` with Apple's Metal
compiler (`xcrun -sdk macosx metal -c`; `fmath_precise` with `-fno-fast-math`), builds `airrun.c` and runs it on the
NVK library named: a build of this tree (`build/build_nvk.sh`'s out dir), opened directly as an ICD, never the
installed one and never in WindowServer. For each test it translates the kernel through `nvmtl_air_to_nir_v1`, creates
the pipeline with the plugin's layout, fills host-visible buffers bound by their device addresses through the table at
binding 640, dispatches whole threadgroups (at most 64K threads, buffers of at most 16 MB), waits at most 2 s and
compares every output with a reference computed on the CPU (exact for integers, within stated bounds for the GPU's
transcendental approximations). It reads NVRM's fault counters (`debug.nvrm_xid_count`, `nvrm_chan_recovered`,
`nvrm_chan_gave_up`, `nvrm_chan_dropped`) around every dispatch and stops all GPU work if one rises.
`AIRRUN_CORRUPT=1` tests the checks themselves: a result with a byte changed in every buffer must fail each test.
Textures are made as the plugin makes them (`nvmtl_vk_image_create_typed`: optimal tiling; transfer, sampled, storage
when written and colour-attachment usage; a cube image cube-compatible) and bound as it binds them (sampled images in
`SHADER_READ_ONLY_OPTIMAL`, storage images in `GENERAL`, texel buffers as buffer views), uploaded before the dispatch
and copied back after it when written. Each test's set layout is built from the result's masks with the plugin's
descriptor types, and its constexpr samplers from the result's state words as the plugin builds them from the
translator's `StaticSampler` state, so a binding the masks leave out fails the test.

Round 2 (2026-11-27) added function constants and textures:

| test | what | tolerance |
|---|---|---|
| fc_kernel_a, fc_kernel_b | bool, float, int (a switch), uint with a default, a buffer under a bool; two value sets | exact |
| fc_kernel_c, fc_kernel_none | a set that leaves out a constant a live path reads; no values | must be refused |
| fc_texture, fc_texture_none | a texture index from a function constant, and `sincos`; without values | read exact, sincos 2e-5 |
| tex_read | rgba8 at two levels, r8, rgba16f into half, r32f, r32ui | exact |
| tex_write | rgba8, r32f, rgba16f written; r32ui read and written | rgba8 1 LSB, r32f 1e-4, rgba16f 1e-3 relative |
| tex_array3d | 2D-array and 3D reads; 3D and 2D-array writes | exact |
| tex_sample | nearest/clamp, linear/repeat at levels 0 and 1, a constexpr linear sampler (slot 2), gather | nearest and gather exact, linear 1/128 |
| tex_cube | a cube by direction and a 2D array by layer, nearest | exact |
| tex_query | width/height at a level, levels, array size, depth, a storage texture's and a texture_buffer's size | exact |
| tex_buffer | texture_buffer read (uniform texel buffer) and written (storage texel buffer) | exact |
| tex_depth | depth2d sample, sample_compare through a constexpr compare sampler (slot 1), read | exact |
| tex_fence | a read_write texture written, fenced and read again | exact |
| tex_pixel | constexpr pixel samplers: nearest clamp_to_edge and clamp_to_zero, linear clamp_to_edge, at centres, edges and outside | nearest exact, linear 1/128 |
| tex_pixel_array | a nearest pixel sampler on a 2D array, and `uniform<T>` | exact |
| tex_offset | an offset known only at run time, a constant offset, a gather with an offset | exact |

Linear filtering is compared within 1/128: NVIDIA's filter weights have 8 bits. On the RTX 5050 all 47 tests (the 29
of round 1 and these 18) pass, three runs in a row, `AIRRUN_CORRUPT=1` makes every one of them fail, and the counters
stayed at 0.

On 2026-11-27, RTX 5050, the installed plugin's `air-runtime/libLLVM.dylib` (LLVM 21.1.8), all 29 tests of round 1
pass and the counters stayed at 0: float math (fast and precise), integer operations, conversions with half, loops with
phis and early exits, switches, threadgroup reductions and arguments, device and threadgroup atomics with
compare-exchange, atomic float add, constant tables, structs with `float3` and `packed_float3`, a `float4x4` in a
constant buffer, vectors and swizzles, thread-private arrays, pointer selects and pointer loops, 64-bit integers, 2D
and 3D grids (direct, indirect, with a base), SIMD-group builtins and functions, char and short buffers, structures
held as values, and one kernel the frontend must refuse (it reads a function constant). The run found two bugs that
the Linux test could not: `any()` of a bool vector was translated as `all()`, and `hadd`/`rhadd` made NAK panic.

`tools/air-nir/sweep.py` runs `airsweep.c` over AIR files (the plugin's cache keeps text IR,
`~/Library/Caches/nvmtl/aircache/*.ll`): every compute kernel through the frontend and pipeline creation, nothing
dispatched, refusals counted by reason. After round 2, of the same 8273 kernels 3481 give a pipeline, none fails in
NAK and none crashes; 4555 are refused because they read function constants and the cache holds no values for them
(of the 3718 that read none, 3481 compile); the 13 function-constant sets the plugin recorded on this Mac
(`spvcache/.fcwarm.v1`: 11 of them VideoToolbox's and MPS's texture kernels) all compile with their values. The rest:
61 texture arrays (`array_ref<texture...>`), 38 integer textures sampled, 33 imageblocks, 21 argument buffers
(`air.indirect_buffer`), 17 `stage_in`, 11 multisampled textures, 9 written cube textures, 9 `air.is_null_texture`, 7
linear pixel-coordinate samples other than 2D clamp_to_edge, 4 `gather_depth`, 4 `thread_index_in_quadgroup`.

The round-1 sweep, for comparison: of the 8273 compute kernels in this Mac's cache on 2026-11-27 (most of them
Apple's own: MPS, Espresso), 699 give a pipeline, none fails in NAK and none crashes; the rest are refused: 6953 for
textures and samplers, 550 because they read function constants, and 71 for imageblocks, `stage_in`, argument
buffers, visible function tables, `air.is_uniform` and two pack functions. The sweep found that a kernel reading a
function constant compiled as if it were 0: Apple's compiler copies each constant (an externally initialized
`MTL_FC_INIT_` global) into an `addrspace(2)` global that is not constant, in the module's static initializer, and
the frontend took that global's initializer as its value. Such kernels are refused now.

## Phases

1. The frontend and its Linux test; then its test on the card (round 1), function constants and textures (round 2).
2. The plugin calls it (round 3), first for the kernels the translator refuses, with `NVMTL_AIR_NIR=0` to turn it
   off. What that needs from the plugin side:
   - the call: `newComputePipelineStateWithFunction:` (`plugin/NVMTLDevice.m`) creates every compute pipeline through
     `nvmtl_vk_compute_pipeline_create` (`plugin/nvmtl_vk.c`) from `f->_spirv`; a NIR road beside it takes the
     function's AIR (`f->_air`, text: written to bitcode with the air-runtime's LLVM, or the bitcode kept from the
     metallib), its constants (`f->_fc`, index to payload bytes, exactly what `nvmtl_translate_with_fc` takes), the
     threadgroup size and `setThreadgroupMemoryLength:` lengths, and creates the pipeline with the same pipeline
     layout (set 0, 48 bytes of push constants) and `VkPipelineShaderStageNirCreateInfoMESA`;
   - the layout: the masks of the result say which sampled, storage and texel-buffer bindings the kernel reads (the
     plugin now finds texel buffers in the SPIR-V); the constexpr samplers come with their slots and AIR words, the
     same slots the translator's reflection gives the plugin;
   - variants: one translation per threadgroup size and per set of threadgroup-memory lengths, as the SPIR-V road
     re-translates sampler and threadgroup variants at dispatch;
   - caching: `nir_serialize` of the result, keyed like the `fc1` SPIR-V entries (the AIR digest, the entry, the
     constants) plus the threadgroup size and lengths, and NVK's pipeline cache for the binary;
   - a refusal (`NVMTL_AIR_NIR_UNSUPPORTED`, or a pipeline NVK will not create) falls back to the SPIR-V road
     unchanged, with the reason logged once per function;
   - a pipeline bound with a pixel-coordinate argument sampler (its state is known only at the dispatch) stays on the
     SPIR-V road until the request carries sampler state.
3. Vertex and fragment functions.
4. Prefer it to SPIR-V where it measures the same or better.
