# Metal AIR to NIR, without SPIR-V

The plugin runs a Metal function on NVK by translating its AIR (the LLVM bitcode inside a metallib) to SPIR-V with
the translator (`translator/`), which NVK then turns into NIR and NAK into the GPU's code. `nvk/air` is a second
road that skips SPIR-V: NVK reads the AIR itself and builds the NIR directly, for compute kernels and, since round 4,
vertex and fragment functions.

Phase 1 is the frontend and its tests (on Linux, then on the card). Since phase 2 (round 3, 2026-11-27) the plugin
asks it first for every compute pipeline, and since phase 3 (round 4, the same day) for every render pipeline, and
keeps the SPIR-V road as the fallback (below, "In the plugin" and "Render pipelines"). Since phase 4 (round 5, the same
day) the frontend also gives the function's reflection, and `newFunctionWithName:` leaves the translator out of every
function the frontend translates: its SPIR-V is made only when something needs it ("Functions made without the
translator"). NVMTL_AIR_NIR=0 turns all of it off; it is off in WindowServer for now.

## Pieces

| file | what |
|---|---|
| `nvk/air/nvk_air_to_nir.c` | the frontend, compiled into NVK (`libvulkan_nouveau.dylib`) |
| `nvk/air/nvmtl_air_nir_abi.h` | the interface between the plugin and NVK: request, result, layout contract |
| `nvk/nvk-air-nir.patch` | adds both to NVK's build; last in the 3.0 chain of `build/build_nvk.sh`, which copies the files in |
| `tools/air-nir/` | `run.sh`, `airtest.c` and two fixtures: the Linux test |
| `tools/air-nir/metal/`, `airrun.c`, `run-mac.sh` | the test on the card (macOS): Metal kernels, run and checked against the CPU |
| `tools/air-nir/airsweep.c`, `sweep.py` | every compute kernel of the plugin's AIR cache through the frontend and NAK, no dispatch |
| `plugin/NVMTLAirNir.m` | the plugin's road: the request, the shader cache, the pipeline states, the variants, the log |
| `plugin/nvmtl_air_nir.h` | its pure parts (the switch, the masks, the sizes, the cache entry), tested by `tools/test_air_nir_plugin.c` |
| `plugin/nvmtl_air_nir_abi.h` | a copy of the ABI header (the plugin is built from `plugin/` alone); `tools/test_air_nir_plugin.py` holds the two equal |
| `tools/air-nir/airrun_tests.h` | the tests' grids, inputs and references, shared by `airrun.c` and `plugin_run.m` |
| `tools/air-nir/plugin_run.m`, `plugin-run.sh` | the same tests through the plugin's own compute path, from a build, nothing installed |
| `tools/air-nir/metal-render/`, `render_tests.h` | vertex and fragment pairs through the plugin's own render path (plugin_run), offscreen, checked per pixel |
| `airsweep.c --reflect`, `sweep.py --reflect` | every kernel, vertex and fragment function of AIR files: the frontend's reflection against the translator's (round 5) |

## How it works

- NVK exports `nvmtl_air_to_nir_v1(VkDevice, request, result)` and `nvmtl_air_nir_free_v1(nir)`. The request carries
  the bitcode, the kernel's name, the threadgroup size the pipeline is for, the length of each `[[threadgroup(n)]]`
  argument, the path of an LLVM library and, since version 2, the function's function-constant values (below).
  Both structs start with their size: a version-1 request (176 bytes) and a version-1 result (288 bytes, up to its
  message) are read and written as far as they go. Version 3 (round 3) adds request flags an older NVK ignores:
  `NVMTL_AIR_NIR_TEXT_IR` (the module is LLVM text IR, as the plugin keeps AIR; parsed with `LLVMParseIRInContext`),
  `NVMTL_AIR_NIR_TG_DEFAULT` (a `[[threadgroup(n)]]` argument with no length gets the translator's default, 512
  elements of its type up to 32 KB), `NVMTL_AIR_NIR_UNCHECKED` (the pipeline is made with buffer robustness off, as the
  plugin makes compute pipelines, so the table is addressed as such a pipeline lowers it) and `NVMTL_AIR_NIR_ALIGN4`
  (no device or constant buffer access promises more than 4 bytes: the align guard's build); the result's
  `global_align` (the largest alignment a buffer access promises); and three exports: `nvmtl_air_nir_clone_v1` (the
  shader for another threadgroup size: the translation reads the size only through the shader's workgroup size),
  `nvmtl_air_nir_serialize_v1` and `nvmtl_air_nir_deserialize_v1` (for the plugin's shader cache; only the NVK that
  wrote a blob reads it).
- Version 4 (round 4) reads vertex and fragment functions too: the function is looked for in `!air.kernel`,
  `!air.vertex` and `!air.fragment` (a mesh or object function is refused), and the result says which it was
  (`stage`). New request fields, read with `NVMTL_AIR_NIR_VARYINGS` and a size that reaches them: `varying_count`,
  `varying_keys` and `varying_locations`, the Location each varying takes by its user semantic as the AIR names it
  (`user(x)`, or the compiler's `generated(...)` name for one without; its field name when it has neither). Without the
  flag each varying takes the translator's own Location (fragment inputs in their order, vertex outputs by
  `air.location_index` or their order). New result fields: `stage`, `vertex_inputs` and `vertex_input_kind[32]` (the
  attributes a vertex stage reads, and whether each is float, signed or unsigned, for the plugin to pick its VkFormat as
  it reads a SPIR-V module's), `varyings` (the Locations written or read), `color_outputs`, and `fragment_flags`
  (`NVMTL_AIR_NIR_FS_DEPTH`, `_SAMPLE_MASK`, `_DISCARD`, `_EARLY_TESTS`, `_PER_SAMPLE`). An older NVK refuses a vertex
  or fragment function ("no kernel named"), and an older plugin's version-3 request is read as before.
- Version 5 (round 5): with `NVMTL_AIR_NIR_REFLECT` the result carries the function's reflection (`reflection`, JSON,
  malloc'd, the caller frees it; or `reflection_why`), read from the module once its function constants are in and
  before LLVM's passes, by the translator's rules for each stage (`translator/translator/src/meta`, `reflect`), with the
  translator's keys and values for everything the plugin reads of it: `bindings` (kind, metal_index, descriptor,
  param_index, address_space, declared_size, extent, type_layout, type_name, texture_shape, access as the entry's
  argument attributes and uses refine it, the constexpr samplers' decoded state with their `raw_words`), `varyings`,
  `vertex_attributes`, `local_size` (a kernel's default, the translator's 64x1x1, which its SpecIds default to), and
  `tessellation`, `fragment_sample_positions` and `argument_buffer_fields` (null or empty: the frontend refuses what
  would fill them). Keys no reader of the plugin's takes (footprints, descriptor_layout, render_targets, the function
  constants, the BufferAddressTable binding) are not written. With `NVMTL_AIR_NIR_SAMPLER_STATES` the request carries
  the state of argument samplers (`sampler_state_*`: slot and the two AIR constexpr-sampler words), and a sampler given
  there with pixel coordinates is sampled as a constexpr one with that state is (the plugin's VkSampler of a
  pixel-coordinate MTLSamplerDescriptor is normalized with the same filter and address modes). NVK exports
  `nvmtl_air_nir_version_v1` (5): the plugin uses neither flag with an NVK that does not (an older one ignores both).
- A render stage's contract, the plugin's render layout: set 0 holds the vertex stage's resources, set 1
  (`NVMTL_AIR_NIR_FRAGMENT_SET`) the fragment stage's, each with the same bands as a kernel's (the table at 640,
  textures at 32 + n, samplers at 160 + n, storage textures at 480 + n), and no push constants are read. A
  `[[stage_in]]` attribute `[[attribute(n)]]` is the vertex input at Location n, as the vertex descriptor binds it;
  builtins are Vulkan's (VertexIndex, InstanceIndex, BaseVertex, BaseInstance, Position, PointSize, ClipDistance,
  Layer, ViewportIndex, FragCoord, FrontFacing, PointCoord, SampleId, SampleMask, FragDepth), read through system-value
  variables as spirv_to_nir declares them, so NVK lowers them as it lowers the SPIR-V road's. Every varying travels as
  32-bit components (a half as a float, a narrow integer or a bool as a 32-bit integer, flat): the two stages agree
  because both come from this frontend.
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

Compute kernels with: buffer arguments in address spaces 1 and 2 (and OpenCL's `air.constant`; since round 6 also one
whose metadata names no address space, the GL-to-Metal compiler's, by its pointer type as the translator reads it), threadgroup
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
argument and constexpr samplers. Since round 6: `texture2d_ms`/`texture2d_ms_array` read by sample (a texel fetch of
that sample) with their sizes and `get_num_samples`; a sample of an integer texture (through a sampler whose state is
known, constexpr or given by the request, the translator's nearest fetch: `floor(coord * size) + offset` at the level
truncated, by each axis's address mode, zero outside with `clamp_to_zero`, the layer clamped; through an argument
sampler, the hardware's sample, as the translator's); `pack`/`unpack` of `unorm4x8`, `snorm4x8`, `unorm2x16`,
`snorm2x16` and `half2x16` (to float or half) and `[fast_]ldexp` of floats, NIR's ops for the GLSL.std.450 ones the
translator lowers them to.

Vertex and fragment functions (round 4): `[[stage_in]]` attributes of one Location (float, half, and 8-, 16- and
32-bit integer scalars and vectors), `[[vertex_id]]`, `[[instance_id]]`, `[[base_vertex]]`, `[[base_instance]]`;
outputs `[[position]]`, `[[point_size]]`, `[[clip_distance]]`, `[[render_target_array_index]]`,
`[[viewport_array_index]]` and user varyings of one Location with their interpolation (`center` / `centroid` /
`sample`, `perspective` / `no_perspective` / `flat`; integers flat); fragment inputs `[[position]]`,
`[[front_facing]]`, `[[point_coord]]`, `[[sample_id]]`, `[[sample_mask]]`, `[[render_target_array_index]]`; outputs
`[[color(n)]]` (n < 8, a literal or since round 5 an expression of function constants, and the old form without the
index operand) of float, half, signed and unsigned types, `[[depth(any|greater|less)]]` (with that depth
layout) and `[[sample_mask]]`; since round 5 an output that exists only under a function constant (the request's values
say whether it does; one whose constant is false is written nowhere, as the translator drops it), and since round 6 a
stage input that does (a vertex attribute or a fragment input; one whose constant is false declares nothing and reads
as zeros where the same constant folds its uses away); `discard_fragment` (a demote, as the translator's), `[[early_fragment_tests]]`, sampling
at the level the derivatives give (with a bias), `dfdx`, `dfdy`, `fwidth`; buffers, textures, samplers and function
constants as in a kernel, the SIMD-group builtins.

Where NAK has no instruction for a NIR operation and does not ask NIR to lower it (`[u|i][r]hadd`, `fisnormal`),
the frontend writes the expansion itself: a NIR opcode that reaches NAK unsupported is a compiler panic and a failed
pipeline, not a refusal.

Everything else is refused with a message, never guessed (`result.message`, status `NVMTL_AIR_NIR_UNSUPPORTED`):
imageblocks (round 10: by kind, below), ray tracing, function constants read without their values, function pointers and
recursive calls, doubles, run-time-sized `alloca`s, a `select` or phi between two kinds of memory, mesh and object
functions; of vertex and fragment functions: an isoline patch, and any metadata after a function's arguments other than
`early_fragment_tests` and a post-tessellation function's `air.patch` (post-tessellation vertex functions take the road
since round 10, as their tessellation evaluation stage), a stage input or output of more than one Location (a matrix, an array, a
nested structure), a stage input or output under a function constant the request gives no value for, `[[color(n)]]` inputs (framebuffer
fetch) without `NVMTL_AIR_NIR_COLOR_INPUTS` (round 8) and of 16- or 8-bit integers, `[[stencil]]` outputs (stencil export), a
raster order group on anything but a `[[color(n)]]` input, a texture or a buffer (on those since round 10 it is bound as
the translator binds it, without interlock; dual-source outputs, `[[color(0), index(1)]]`, take the road since round 10),
`[[primitive_id]]`, `[[barycentric_coord]]`, the amplification builtins and any other builtin not listed above,
threadgroup arguments, a varying the request gives no Location; and of textures: `array_ref<...>` (MPS's, passed through
objects in thread memory), and of `array<texture2d<T>, N>` (round 8) a written element or a `texture_buffer` element
chosen at run time, multisampled depth textures and written multisampled ones, a written cube or depth texture and a texel read of a cube
(the translator goes through a 2D-array view of them), a gradient sample or a cube sample of an integer texture through a
sampler known here (the translator's nearest fetch has neither), a bias in a kernel, a minimum-LOD clamp on a nearest
fetch, a gather, a comparison, a pixel-coordinate or a bicubic sample (since round 10 any other is applied as the
translator applies it), an offset known only at run time, a write or a storage read at a
level other than 0, `get_null_texture` (round 10: the translator's placeholder binding is not reproduced; and, since round 5, a texture or sampler handle used as a plain value:
a `ptrtoint` and a compare, a select or a phi of two handles that known branches do not decide (round 8), which Apple's compiler writes for a null-texture test; the
frontend read such a handle's missing value and crashed, below), and samplers with pixel coordinates (constexpr, or an
argument one whose state the request gives) other than nearest with a clamp and linear 2D `clamp_to_edge`, and with
min/max reduction (the translator emulates the first; bicubic samples, pixel and normalized, take the road since round
10 as the translator emulates them); `pack`/`unpack` of `srgb`, `rgb10a2`,
`rg11b10f`, `rgb9e5` and `rgb565`.

An argument sampler's state is not known when the function is translated: the frontend treats it as one with
normalized coordinates, as a sampler the plugin creates from a normalized `MTLSamplerState` is. A dispatch or draw
bound with a pixel-coordinate sampler makes a variant whose request carries the bound state (version 5, round 5), as
the translator takes it through `with_runtime_sampler`.

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

## In the plugin (round 3)

`newComputePipelineStateWithFunction:` (and every descriptor and options variant, which end there) asks
`plugin/NVMTLAirNir.m` first. The request carries the function's AIR as the plugin keeps it (text IR), its
function-constant values (`f->_fc`, all of them, `NVMTL_AIR_NIR_FC_VALUES`), the threadgroup size the translator's module
defaults to (its SpecId defaults, as the SPIR-V road's creation-time pipeline is compiled for; 1x1x1 when it has none),
and the flags above. The pipeline is created from the NIR with the pipeline layout every compute pipeline has (both
stage sets and the heap, 48 bytes of push constants: the SPIR-V road's, so the dispatch binds what it binds for any
kernel), and its descriptor masks come from the result: the table (binding 640), each sampled texture (32 + n) and
sampler slot (160 + n), the storage textures (480 + n, up to the highest), and which of them are texel buffers. The
constexpr samplers are bound by the plugin's existing path, from the translator's reflection: the road is taken only
when the result puts them at exactly the slots that reflection names (the slots are compared, not the state words).

The SPIR-V road, exactly as before, takes:
- a kernel the frontend refuses, with the frontend's reason, among them an incomplete function-constant set (the
  frontend refuses a kernel that still reads a constant the set does not give; the translator reads it as 0);
- a kernel NVK will not make a pipeline of, and one whose constexpr samplers would not sit where reflection binds them;
- a kernel whose AIR is the translator's own rewrite (a mesh or object stage's kernel, a tile function's, a kernel
  with a stage-in layout), one a replacement module is configured for (`NVMTL_BDA_PROBE_FILE`), and one the
  application profile edits;
- a dispatch that binds a pixel-coordinate argument sampler: its variant is the translator's, as before (the request
  carries no sampler state yet);
- a dispatch of a threadgroup size the road does not run (over the device's per-dimension limit, which the SPIR-V road
  linearizes): the kernel's SPIR-V twin, made the first time;
- every kernel with `NVMTL_AIR_NIR=0`, in WindowServer, and with an NVK that has no frontend (an older one) or that the
  plugin did not open directly (through the loader, its `VkDevice` is not NVK's own). The exports are looked up with
  `dlsym` from the NVK the plugin opened, all five or none.

Variants: a threadgroup-size variant of a kernel on the road is a copy of its NIR with the size changed
(`nvmtl_compute_variant`; the creation-time size is kept as the first variant); a threadgroup-memory variant
(`setThreadgroupMemoryLength:`) is translated again with the lengths; and when the kernel's buffer accesses promise more
than 4 bytes, the align guard watches all of its buffers, and a dispatch with one bound at an address the promise does
not hold runs a build translated again with `NVMTL_AIR_NIR_ALIGN4` (the SPIR-V road's SAFE module).

Cache: the result and the serialized NIR are an entry of the plugin's shader cache (the spvcache folder, stage `nir1`,
named and swept by the translator's build tag as every entry there), keyed like the function-constant SPIR-V entries
(the AIR's digest, the entry, the constants) and by the threadgroup size, the threadgroup-memory lengths, the request's
flags and the identity of the NVK (its Mach-O UUID) and of the LLVM library. A refusal is an entry too, so a process
does not ask the frontend about it again. NVK's own pipeline cache keeps the compiled binary, as for the SPIR-V road.

The function still gets its SPIR-V when it is made (newFunctionWithName: translates, as before): the fallback, the
reflection, the hazard model and the binding of constexpr samplers and argument-buffer resources read it. So in phase 2
the road adds the frontend's work to the translator's rather than replacing it; phase 4 is where the translation would
be left out for the kernels the road takes.

The log (the plugin's log file; a release plugin writes it only for a process with `NVMTL_PROCESS_ENV=1`):
`compute: AIR to NIR ON ...` or `... off (<why>)` once per process, also in the system log;
`air-nir: kernel <name>: AIR to NIR (<ms>, translated | its NIR from the shader cache)` the first time a function takes
the road; `air-nir: kernel <name> stays on the SPIR-V road: <reason>` once per function that falls back;
`compute roads in <process>: AIR to NIR <n> pipeline(s) and <n> variant(s), SPIR-V <n> pipeline(s) and <n> variant(s);
<n> took the SPIR-V road after AIR to NIR said no` at every power of two of their sum, also in the system log.

### Tested through the plugin (round 3)

`tools/air-nir/plugin-run.sh NVMTLDriver NVK.dylib OUTDIR [TEST...]` runs the tests of `tools/air-nir/metal` through the
plugin's own compute path without installing anything: a development build of the plugin (`build/build_plugin_dev.sh`)
in a bundle folder of its own (the installed bundle's air-runtime and translator linked in, read only) is opened in a
normal process and its `NVMTLDevice` made over the NVAccel service, as the card programs of `tools/` do with a selected
driver; it opens the NVK named. Each test makes its library from a metallib, its function (with its constants), its
pipeline state (the road it took is read from the pipeline state), its buffers, textures and samplers through Metal,
dispatches through a compute encoder (indirectly for `grid2d_indirect`), copies written textures back and checks the
outputs against the CPU's reference (`airrun_tests.h`, shared with `airrun.c`). Then the same dispatch 21 more times in
one command buffer, on copies of its buffers in video memory, timed by the plugin's per-dispatch timestamp queries
(`NVMTL_DISPATCH_TIME`). Caches go to OUTDIR (empty at each run, or an earlier run's with `PLUGIN_RUN_CACHE`), Mesa's
disk cache is off, every command buffer is waited for at most 2 s and NVRM's four fault counters are read around every
test. `grid2d_base` is not run (Metal has no dispatch base).

On the RTX 5050 (2026-11-27, NVK of this tree, the installed plugin's air-runtime and translator):
- AIR to NIR first: the 42 tests that dispatch all take the road and pass; the four the frontend must refuse
  (`fc_refuse`, `fc_kernel_c`, `fc_kernel_none`, `fc_texture_none`) take the SPIR-V road, with the reason in the log;
  `tg_arg`'s threadgroup-memory variant takes the road too. With `PLUGIN_RUN_CORRUPT=1` all 42 fail. Two runs with
  empty caches and one with the caches of the first (every shader on the road then came from the cache) gave the same;
  the counters stayed at 0.
- `NVMTL_AIR_NIR=0`: every pipeline on the SPIR-V road; 38 of the 42 pass. `fmath` and `fmath_precise` (an `fma`
  1.3e-6 off, the check allows 1e-6), `math_lib` (one value 1e-4 off) and `tex_offset` (a sample with an offset
  known only at run time) fail the checks these tests were written with for the frontend; the plugin of the commit
  before this round gives exactly the same four failures, with the same values.
- Pipeline creation, first pair of runs, empty caches (41 kernels, the first pipeline of the process left out: it pays for NVK and NAK
  starting, 43 ms on the road and 54 ms on the SPIR-V road): on the road (frontend and NAK) median 2.2 ms, max 10.2 ms;
  on the SPIR-V road (the translator at newFunctionWithName, then the address lowering and NAK) median 6.9 ms
  (translator 4.5, pipeline 2.6), max 28.0 ms. With the caches of that run: 1.6 ms (max 10.3) and 2.6 ms (max 21.7).
- GPU time of the dispatch, second pair of runs (median of 21, each kernel's own grid of at most 64K threads, so mostly
  the launch): the median over kernels is 7.9 us on the road and 9.5 us on the SPIR-V road (the longest single time,
  `dev_atomics` apart, 22.6 and 33.1 us). Of the 41 kernels timed (`grid2d_indirect`'s indirect dispatch is not), 38 are more than 2% faster on the
  road, `tex_write` and `tex_fence` within 2%, and `dev_atomics` (contended atomics on few addresses) slower: 657
  against 629 us, max 1036 and 985. The first pair gave the same per kernel to a few percent, except for a stretch of consecutive tests that took about twice as long in one run than in the other,
  on either road (the GPU's clock changing during a run, not the code).

## Render pipelines in the plugin (round 4)

`newRenderPipelineStateWithDescriptor:` (and every variant that ends there) asks the road first, after it has made what
the SPIR-V road needs anyway (the linked stages, the colour and depth formats, the blend state, the vertex input, the
multiview variants), and before the SPIR-V road's varying link and pipeline: both stages go through the frontend
(`nvmtl_air_nir_render_pipeline`, `plugin/NVMTLAirNir.m`) and the pipeline is made from their NIR
(`nvmtl_vk_pipeline_create_nir`, `plugin/nvmtl_vk.c`) with the SPIR-V road's pipeline layout (set 0 the vertex
stage's, set 1 the fragment stage's, set 2 the heap, the fragment stage's push range of the sample positions when the
device has them), render pass, vertex input and fixed and dynamic state (`nvmtl_graphics_pipeline_make`, now one
function both roads call), so a draw binds what it binds for any pipeline. The descriptor masks of each stage come from
its result, the vertex attributes' VkFormats from the component kinds the vertex result names. Each stage's request
carries its AIR text, its function-constant values and its varyings: the Locations the translator's reflection gives
them, a fragment input's moved onto the vertex output of the same name by the same remap the SPIR-V road applies to
its fragment module (`nvmtl_varying_remap`, which `nvmtl_varying_link` now calls too), so both roads link a pipeline
the same way. A render stage is translated with the device's robustness (render pipelines keep robustBufferAccess) and
with `NVMTL_AIR_NIR_ALIGN4`, since the SPIR-V road builds every render module whose accesses promise more than 4 bytes
SAFE. Its constexpr samplers are bound from the translator's reflection, as the SPIR-V road's are: the road is taken
only when the result puts them at the slots reflection names (and the state words are compared and logged).

The whole pipeline takes the SPIR-V road, exactly as before, when:
- either stage is refused (the reason names the stage and the frontend's message), or is not the function the
  descriptor names it as;
- it has no fragment function (the SPIR-V road's empty one stands in), or a stage has no AIR (the pixel-sampler and
  other variants made from SPIR-V);
- it needs one of the SPIR-V road's rewrites of render stages: an A8 colour attachment, vertex amplification,
  tessellation, the GL uniform buffer, framebuffer fetch (`[[color(n)]]` reads, A8 remaps and fetch copies), stencil
  export (emulated), programmable sample positions, a stage-in rewrite (`_spirvOnly`), an application-profile edit of
  either stage, `NVMTL_BDA_PROBE_FILE`;
- NVK will not make the pipeline of the two NIR shaders;
- the road is off (`NVMTL_AIR_NIR=0`, WindowServer, an NVK without the frontend).
The reason is logged once per pipeline (`air-nir: pipeline <vertex> / <fragment> stays on the SPIR-V road: <why>`), the
road taken once per pipeline (`air-nir: pipeline <vertex> / <fragment>: AIR to NIR (<ms>, translated | both stages'
NIR from the shader cache)`), and the counts at every power of two of their sum, apart from the compute counts:
`render roads in <process>: AIR to NIR <n> pipeline(s), SPIR-V <n> pipeline(s); <n> took the SPIR-V road after AIR to
NIR said no`. The pipeline state's `_road` is 1 on the road.

Cache: each stage is its own `nir1` entry, keyed as a kernel's (`abi` 4) and by its stage, its varyings (semantic to
Location) and, for the vertex stage, the vertex descriptor (the translation does not read it: a later change that does
cannot then read a stale entry). NVK's pipeline cache keeps the compiled binaries.

### Tested through the plugin (round 4)

`plugin-run.sh` now also builds the vertex and fragment pairs of `tools/air-nir/metal-render` and runs them through the
plugin's render path (`render_tests.h`, `TEST` names one, `render` all of them, `compute` all the kernels): each pair's
pipeline from its descriptor, one draw into offscreen targets of 64x64 (at most four colour attachments and a depth
attachment), the targets read back and compared per pixel with the CPU's reference; pixels within 1.5 pixels of an
edge of a primitive that does not cover the whole target are not compared. Then passes of 1 and of 22 draws, three of
each, for the GPU time of a draw: (median of 22 - median of 1) / 21, from each command buffer's GPU interval (the
plugin's timestamps around it; it has none per draw). `render.tsv` in the run's folder has the results.

| test | what | tolerance |
|---|---|---|
| r_stage_in | stage_in from a vertex descriptor: float2, float3, float4, uchar4 normalized, half2, uchar4 (integer), at buffer 2 | 1e-4 |
| r_instanced | vertex_id, instance_id, base_instance: 8 instances from base instance 2, vertices from 3, corners from a buffer | exact |
| r_link | four varyings with user semantics, declared in another order in the fragment stage (the link moves them) | 1e-4 |
| r_interp | perspective, no_perspective and flat varyings on a triangle whose vertices have w 1, 2 and 4 | 2e-3; flat exact (the first vertex's) |
| r_texture | an argument sampler (nearest) and a constexpr sampler (linear), at the level the derivatives give | nearest 1e-6, linear 1/128 |
| r_discard | discard_fragment on a checkerboard, [[position]] in the fragment stage | 1e-6 |
| r_depth | [[depth(any)]] into a Depth32Float attachment (less, write), read back; [[position]].z and .w | 1e-6 |
| r_mrt | two colour attachments, rgba8 and r32float | rgba8 1 LSB, r32float 1e-4 relative |
| r_front | [[front_facing]] with culling off: a clockwise and a counter-clockwise triangle | exact |
| r_points | [[point_size]] 8 and [[point_coord]] with a point draw | 1e-5 |
| r_fc | function constants (bool, float, int) in the fragment function | 1e-6 |
| r_buffers | a fragment reading a constant palette, a device index buffer and a constant count through the table | exact |
| r_clip | [[clip_distance]]: the part of the target where it is negative is clipped | 1e-4 |
| r_primid | [[primitive_id]], which the frontend refuses: must take the SPIR-V road, and draw right there | exact |

On the RTX 5050 (2026-11-27, NVK of this tree, the installed plugin's air-runtime and translator), two runs of every
test (46 compute, `align_misaligned`, 14 render) on each road, one with the caches of the first and one with
`PLUGIN_RUN_CORRUPT=1`; the fault counters stayed at 0 throughout:
- AIR to NIR first: the 13 render pairs take the road and pass, `r_primid` takes the SPIR-V road (`fragment rq_fs:
  the fragment argument kind air.primitive_id`) and passes; the compute tests are as in round 3 (42 on the road and
  passing, the four that must be refused on the SPIR-V road); `align_misaligned` passes (below). With the caches of
  the first run every render pipeline's two stages came from the shader cache, with the same results. With
  `PLUGIN_RUN_CORRUPT=1` every test that dispatches or draws fails.
- `NVMTL_AIR_NIR=0`: all 14 render pairs pass on the SPIR-V road; the compute tests as in round 3 (38 of 42, the same
  four failures); `align_misaligned` fails (below).
- Pipeline creation of the 13 render pairs on the road, the two runs: median 2.7 and 2.8 ms (max 4.0 and 4.5); on the
  SPIR-V road 2.8 and 2.7 ms (max 3.9 and 3.7), which is NVK's and NAK's work only: the translator ran when the
  functions were made, on either road (both functions: median 3.5 ms). With the caches: 1.9 ms (max 2.9). Compute, as
  in round 3: 2.2 ms on the road, 2.5 ms on the SPIR-V road plus 4.5 ms of translator at `newFunctionWithName:`.
- GPU time of a draw (64x64 targets, so mostly the draw's fixed cost): the same on both roads, median 0.13 us, except
  `r_texture`: 0.36 us on the road, 0.52 to 0.58 us on the SPIR-V road.

Leftovers of round 3:
- The align guard through the plugin (`align_misaligned`, `metal/align_guard.metal`): a kernel whose float4 accesses
  promise 16 bytes, dispatched with its buffers at offsets 0 and 0, then 4 and 8. On the road the first runs the
  kernel's own build, the second the frontend's `NVMTL_AIR_NIR_ALIGN4` build (the log says `align guard: buffer 0 bound
  at GPU address ... is not 16-aligned - this kernel now runs its SAFE module`), and both results are exact. On the
  SPIR-V road the misaligned dispatch writes nothing where it should (its float4 accesses go to the address rounded
  down: element 0 reads 0) and no SAFE module is made: the plugin of the commit before round 3 gives the same, so the
  SPIR-V road's guard does not see this kernel's promise. Not changed here.
- The constexpr samplers' state words: for the round-2 kernels that have them (`tex_sample` slot 2, `tex_depth` slot 1,
  `tex_pixel` slots 0 to 2, `tex_pixel_array` slot 0) and `r_texture`'s fragment stage (slot 1), the frontend's words
  are the translator's reflection's (`raw_words`) exactly. The plugin now logs the comparison once per function.

## Functions made without the translator (round 5, phase 4)

`newFunctionWithName:` and its function-constant variants (`constantValues:`, the completion-handler and descriptor
forms, which end there) ask the frontend first (`nvmtl_air_nir_lazy`, `plugin/NVMTLAirNir.m`): the function is translated
with `NVMTL_AIR_NIR_REFLECT` as its pipeline will ask (a kernel at its reflection's default size, 64x1x1, with the flags
and threadgroup-memory lengths `nvmtl_air_nir_make` uses, so its NIR is stored where the pipeline looks it up; a vertex
or fragment function with its own Locations), and when the frontend translates it and gives its reflection, the
function is made with that reflection (`_nrefl`, also `_refl`, which every reader of the reflection takes) and no
SPIR-V (`_spirvLazy`). The translator then runs only the first time something needs SPIR-V of the function
(`nvmtl_function_spirv`, which makes it exactly as `newFunctionWithName:` would have: `nvmtlEntrySpirv`, or
`nvmtl_translate_with_fc_cached` with the constants): a pipeline the frontend refuses or NVK will not make, a render
pipeline that needs a rewrite of the SPIR-V road's (A8, multiview, tessellation, the GL uniform buffer, framebuffer
fetch, stencil export, programmable sample positions), a variant the road does not make, a dual-source blend check,
`NVMTL_BDA_PROBE_FILE`. A function the frontend refuses or does not reflect is made as before, and so is every function
with `NVMTL_AIR_NIR=0`, in WindowServer, with an application profile (it edits SPIR-V), in the check run, and OpenCL's
(by-value `air.constant` arguments and `__entry_` wrappers, which stay deferred to `newFunctionWithPluginData`). The
frontend's reflection is kept in the shader cache (stage `nrf1`, keyed by the AIR, the entry, the constants, the stage
and the identity of NVK and LLVM; a function it does not take is an entry too). The plugin logs once per process, at
exit, `functions in <process>: <n> made without the translator (<ms> of the frontend), <k> of them translated later
(<ms> of the translator)` (also in the system log), and in its log file the function each time one is translated later
and why.

What the plugin read of the SPIR-V of every function before, and where it comes from now for a function made without
the translator: the reflection (arguments, varyings, vertex attributes, constexpr samplers, embedded resources) from
the frontend's; the kernel's default threadgroup size (`nvmtl_cvar_seed_size` of the SPIR-V's SpecId defaults) from the
reflection's `local_size`; the hazard model's writes and the GL uniform check from the reflection, as before; the
binding masks from the frontend's result, as on the road before; the tessellation, framebuffer-fetch and stencil-export
checks of the render road are not made (the frontend refuses a post-tessellation vertex function, `[[color(n)]]` inputs
and `[[stencil]]`); the multiview check, the A8 variant and the SPIR-V road translate first. The SPIR-V FNV hash the
per-kernel profile rows and the fault-context records carry is 0 for a function made without the translator (their AIR
key is there as before).

The check run (`NVMTL_AIR_NIR_REFL_CHECK=1`, development plugins only): every function is made as before, and the
plugin log compares the frontend's reflection of it with the translator's, key by key (the frontend's keys; the
bindings as a set, the translator's BufferAddressTable left out), and its default size with its SPIR-V's.

Argument samplers with pixel coordinates (step 3): the dispatch's or draw's bound pixel-coordinate samplers (the
translator's state names `NVMTLSamplerState` keeps) become the AIR state words in the request (`nvmtl_air_nir_sampler_words`),
so that variant is the frontend's: a compute variant is translated with them (and the align guard's build of it too), a
render variant's two stages are copies of the pipeline's that carry them and take only the road (`_roadOnly`); when the
road does not make it, the translator's variant is made as before. A sampler whose state the words cannot hold
(`MirrorClampToEdge`) keeps the translator's variant.

### Tested (round 5)

- Reflection: `NVMTL_AIR_NIR_REFL_CHECK=1` through `plugin-run.sh` over all 61 tests: the frontend's reflection equals
  the translator's for all 70 functions it translates (43 kernels with their function-constant sets, 14 vertex and 13
  fragment functions; the default size equals the SPIR-V's for every kernel); the frontend refuses the other five (four
  that must be refused, and `r_primid`'s fragment). `sweep.py --reflect` over the plugin's AIR cache (22,689 files, 10,814
  kernel, vertex and fragment functions, no function-constant values; 32 minutes; with the frontend as it was before
  its outputs under function constants and colour indices from them, which the next sentence but one covers): the
  frontend translates 5,001 (3,481
  kernels, 488 vertex and 1,032 fragment functions) and its reflection equals the translator's for every one of them, by
  every key it gives; it gives one for every function it translates; it refuses 5,811 (most read function constants the
  cache holds no values for; then buffer arguments whose metadata names no address space (475: the parameter's pointer type says it), post-tessellation vertex functions,
  argument indices from function constants, texture arrays, framebuffer fetch); two the translator's reflection did not
  finish (below). With function-constant values (all 0, and all 1), the 36 functions of the cache with the output and
  colour-index forms of the last item of this list that the frontend now translates compare equal too.
- The sweep found that the frontend crashed (SIGSEGV) on two SceneKit fragment functions of the cache: a null-texture
  test written as `ptrtoint` of a texture handle, whose NIR value it read though a handle has none. On 3.0.21 any
  process making a render pipeline with such a fragment would crash in `newRenderPipelineStateWithDescriptor:`; such
  uses are refused now (`air_vdef`), and those pipelines take the SPIR-V road.
- The translator's reflection (`nvmtl_reflect`, 3.0.21's) did not return in 20 minutes on the two copies of MPS's
  `cnnConvArrayGeneralA14` in the cache (38 MB of text IR each; it loops in `lower_air_text`'s
  `inline_and_forward_object_fields`); the frontend refuses them in 8 s (an argument index from function constants it is
  not given). A function on the road no longer asks the translator's reflection; `airsweep` now ends a function after 60 s
  (a CRASH with signal 14) and the sweep goes on.
- Through the plugin, on the RTX 5050, the NVK of this tree and the installed plugin's air-runtime and translator (two
  runs on each road, one with the caches of the first and one with `PLUGIN_RUN_CORRUPT=1`; the fault counters stayed at 0):
  all 47 compute tests (the 46 of round 4 and `tex_argpixel`), `align_misaligned` and the 16 render pairs (the 14 of round
  4, `r_argpixel` and `r_fcout`) pass on the road, the five the frontend must refuse taking the SPIR-V road, and 75
  functions are made without the translator, one of which is translated later (`r_primid`'s vertex function, whose
  pipeline takes the SPIR-V road). With `NVMTL_AIR_NIR=0` the same five tests as in round 4 fail (`fmath`,
  `fmath_precise`, `math_lib`, `tex_offset`, `align_misaligned`: old, not the road's), and the new three pass. With
  `PLUGIN_RUN_CORRUPT=1` every test that dispatches or draws fails.
- The plugin and an NVK of another version (a release plugin loads only the installed NVK, so the package's release
  plugin ran on the installed 3.0.21 NVK): a version-4 NVK ignores `NVMTL_AIR_NIR_REFLECT` and
  `NVMTL_AIR_NIR_SAMPLER_STATES`, and sampling a pixel-coordinate sampler as a normalized one gave wrong results
  (`tex_argpixel`, `r_argpixel` failed). NVK now exports `nvmtl_air_nir_version_v1` (5); with an NVK without it the
  plugin makes every function with the translator and keeps pixel-sampler variants on the SPIR-V road, and says so in
  the road's line. The development plugin with the 3.0.21 NVK: those tests pass, their variants on the SPIR-V road.

| test | what | tolerance |
|---|---|---|
| tex_argpixel | argument samplers bound with pixel coordinates: nearest clamp_to_edge, nearest clamp_to_zero, linear clamp_to_edge | nearest exact, linear 1/128 |
| r_argpixel | a fragment sampling through argument samplers with pixel coordinates, nearest and linear | nearest 1e-6, linear 1/128 |
| r_fcout | vertex outputs under function constants (one true, one false), `[[color(fc + 1)]]` | 1e-4 |

  The variants of `tex_argpixel` and `r_argpixel` are made on the road (`compute pixel sampler variant built: tex_argpixel
  (AIR to NIR)`, `render pixel sampler variant built on the AIR-to-NIR road`).
- The time saved (function creation plus pipeline creation, the first of each kind in the process left out; runs 1 and 2):

| | AIR to NIR | NVMTL_AIR_NIR=0 |
|---|---|---|
| compute, 42 kernels: newFunctionWithName: | median 0.67 / 0.68 ms, max 1.33 / 1.33 | median 4.56 / 4.56 ms (the translator), max 10.91 / 10.91 |
| compute: pipeline | median 1.53 / 1.53 ms, max 10.47 / 9.46 | median 2.59 / 2.49 ms, max 22.32 / 21.75 |
| compute: both | median 2.24 / 2.22 ms, max 11.11 / 10.15, all 124 / 115 ms | median 6.89 / 6.90 ms, max 28.54 / 27.98, all 335 / 332 ms |
| render, 14 pairs: both newFunctionWithName: | median 0.77 / 0.80 ms, max 1.60 / 1.61 | median 3.48 / 3.49 ms, max 6.74 / 5.55 |
| render: pipeline | median 1.98 / 2.08 ms, max 2.60 / 2.38 | median 2.94 / 2.82 ms, max 3.64 / 3.83 |
| render: both | median 2.84 / 2.91 ms, max 3.67 / 3.71, all 40 / 40 ms | median 6.61 / 6.65 ms, max 9.96 / 8.67, all 91 / 90 ms |

  So a kernel is made in a third of the time and a render pipeline in less than half (4.6 and 3.6 ms saved per test,
  the median of the differences). With the caches of the first run: 1.83 ms (compute) and 2.09 ms (render), the
  frontend's work 8.8 ms in all for the 75 functions (40.9 ms without its caches). (The runs of the package's code; an
  earlier pair, before the version check below, gave the same to 0.1 ms.)
- Three refusals real processes met since the 3.0.21 boot are gone (the plugin cached 33 refusals then): an output
  that exists only under a function constant (`the vertex output air.function_constant (float)`, 10 of them), a colour
  output whose index is an expression of function constants or has no index operand (`colour output 0 has malformed
  metadata`, 4), and the text IR of a linked function that defined a constexpr sampler's global twice (`redefinition of
  global '@__air_sampler_state'`, 4): the translator's linker appended a dependency module's global even when the output
  already defined it with the very same line; it no longer does (`append_dependency_module`, a unit test), which changes
  nothing the translator reads, and a global defined again with another line is kept as before. The cached refusals
  hold only the message, not the function or its constants, so they cannot be run again; over the functions of the AIR
  cache with the first two forms, each with all its function constants 0 and all 1: 3.0.21's frontend translates none
  of 170, this one 36, whose reflections equal the translator's; the rest are refused for other reasons (argument
  buffers, stage inputs under a function constant, framebuffer fetch, `unpack`). Framebuffer fetch (`the fragment
  argument kind air.render_target`, 15) stays refused.

## The refusals real functions hit next (round 6)

Round 6 took the refusals the round-5 sweep of the AIR cache counted most and the 3.0.22 boot's processes met, each
with the translator's ABI (its handling read first, the bindings unchanged):

- **A buffer whose metadata names no `air.address_space`** (475 functions of the cache, nearly all `__main` of the
  GL-to-Metal compiler, Minecraft's among them): the parameter's pointer type gives the address space, as the
  translator reads `addrspace(N)` off the define line (`meta::function_param_pointer_address_spaces`); an address
  space 0 pointer stays refused. The reflection already read it so.
- **A stage input that exists only under a function constant** (a vertex attribute or a fragment input, 71 of the
  cache): the request's values say whether it exists, as for outputs (round 5); one whose constant is false declares
  no variable and reads as zeros, which the same constant folds away. Without the constant's value it stays refused.
- **A sample of an integer texture** (48): through a sampler whose state is known here (constexpr, or given by the
  request) the translator's nearest fetch (`images/sample.rs`, `fetch_coord.rs`): `floor(coord * size) + offset`
  per axis at the level truncated (0 for a bias or the derivatives' level), each axis by its address mode (repeat as
  `SMod`, mirrored repeat over twice the size, the clamps to the edge), zero outside where the mode clamps to zero
  (`clamp_to_zero`, or `clamp_to_border` with a transparent black border), the layer clamped to the last one; through
  an argument sampler the hardware's sample, as the translator's `OpImageSampleExplicitLod`. A gradient sample of one
  stays refused, as the translator refuses it, and so does a cube through a known sampler.
- **Multisampled textures** (`texture2d_ms`, `texture2d_ms_array`, 1 at boot, 33 in the cache): a read by sample is a
  texel fetch of that sample (`txf_ms`), `get_width`/`get_height` a size query without a level, `get_num_samples` the
  sample count. A multisampled depth texture stays refused.
- **`pack`/`unpack`** of `unorm4x8`, `snorm4x8`, `unorm2x16`, `snorm2x16` and `half2x16` (8 at boot), to float or half,
  are NIR's `pack_*`/`unpack_*` ops, what `spirv_to_nir` makes of the GLSL.std.450 instructions the translator lowers
  them to (`dispatch_texture.rs packed_format`); **`[fast_]ldexp`** of a float scalar or vector (2 at boot) NIR's
  `ldexp`, a scalar exponent of a vector splatted (`lower_fast_ldexp`).
- **A global defined twice** (`redefinition of global '@__air_sampler_state'`, 4 at boot): the text came with the
  global twice, its second definition without `, align 8` (the line the refusal quotes), so round 5's fix in the
  translator's linker (the very same line) did not catch it; the state is the one the visible functions `glow_v1` and
  `glassHighlight_v1` of the AIR cache each define, so it is a fragment linked with them; the text as the plugin hands it over is not on disk (the AIR cache keeps
  each module alone). The frontend now drops a later definition with the same type and initializer (the lines equal
  once a trailing `, align N` is set aside) before LLVM parses the text, and refuses one that differs, naming it.
  `airsweep` hands the text of a file this LLVM will not parse to the frontend when its list line names the kernel
  (`TEXTOK` / `TEXTREFUSED`).

Two things the sweep found on the way, both in the reflection: a constexpr sampler state of one `i64` (an older
module's) gave its first word's low half as the frontend's LOD bias (LLVM's aggregate element of a scalar is the scalar),
where the translator reads a second word of 0 (4 functions); and where a function-constant gate is a predicate the
static initializer computes, the frontend (which runs the initializer with LLVM, `is_function_constant_defined` true for
a given constant) and the translator's text evaluation of it (`meta::globals`, which takes `is_function_constant_defined`
as false and every block in text order) can disagree on which outputs exist. The frontend gives no reflection there
(`not reflected`: the plugin asks the translator) when the initializer calls `is_function_constant_defined` of a given
constant or branches; its translation keeps its own reading.

Still refused, next: arrays of textures (`array<texture2d<T>, N>` and `array_ref<...>`, 61 in the cache: the translator
binds one descriptor array at 32 + n with count N, `TextureArray`, which the plugin's set-0 layout for the road does
not declare, and the 56 `array_ref` ones, MPS histograms, pass the array through C++ objects in thread memory); argument
buffers (102); a texture call through a phi of two texture handles (42 of the GL-to-Metal `__main`s); framebuffer fetch,
post-tessellation vertex functions and imageblocks.

### Tested (round 6)

- Through the plugin (`r6/final-runs.sh`: two runs on each road, one with the caches of the first, one with
  `PLUGIN_RUN_CORRUPT=1`; the fault counters stayed at 0): on the road all 50 compute tests (round 5's 47, `tex_intsample`,
  `pack`, `tex_ms`), `align_misaligned` and the 18 render pairs (round 5's 16, `r_nospace`, `r_fcin`) pass, 69 in all,
  the five the frontend must refuse taking the SPIR-V road; 81 functions are made without the translator. With
  `NVMTL_AIR_NIR=0` the same five tests as in rounds 4 and 5 fail and the new five pass. With `PLUGIN_RUN_CORRUPT=1`
  every test that dispatches or draws fails.

| test | what | tolerance |
|---|---|---|
| tex_intsample | a `texture2d<uint>` sampled through constexpr samplers (repeat, mirrored repeat, clamp_to_zero, clamp_to_edge at level 1) and an argument sampler | exact |
| pack | `unpack_*` of the five formats to float and half, `pack_float_to_*` of four, `ldexp` of a vector and a scalar | unorm/snorm 1e-6, half 1e-3, ldexp exact, packs 1 LSB |
| tex_ms | a 4-sample `texture2d_ms<float>` (cleared by a render pass, `plugin_run` only) read by sample, its size and sample count | 1/255 |
| r_nospace | `r_buffers` with `air.address_space` taken out of every buffer's metadata (`plugin-run.sh` makes it) | exact |
| r_fcin | a vertex attribute and a fragment input under a true function constant, another pair under a false one | 1e-4 |

- `airsweep` on a constexpr-sampler kernel whose text defines `@__air_sampler_state` twice: the same definition (once
  without `, align 8`) translates as the original does; a differing one is refused (`the text defines the global
  @__air_sampler_state twice, differently`).
- `sweep.py --reflect` over the AIR cache (round 5's list, 21,196 files): 10,814 kernel, vertex and fragment functions without function-constant
  values (7 minutes): the frontend translates 5,517 (round 5: 5,001; 3,539 kernels, 684 vertex and 1,294 fragment
  functions) and its reflection equals the translator's for every one of them by every key it gives (different 0, not
  reflected 0); it refuses 5,295 (5,811); two are the MPS modules whose translator reflection does not finish (the 60 s
  alarm, as in round 5). Refused, the first ten: a function constant read without its value 4,025, an argument index
  from one 570, post-tessellation vertex functions 223, `array_ref` texture arrays 56, framebuffer fetch 56, fragment
  argument buffers 44, a texture call through a phi of two handles 42, vertex argument buffers 37, a stage input under
  a function constant without its value 37, imageblocks 33. Of the 475 buffers without an address space 431 translate.
  With function-constant values (each function's constants all 0, and all 1): the 71 functions with a stage input
  under a constant give 14 equal and 8 not reflected (initializer-computed gates), the other 120 variants refused for
  other reasons (`viewport_array_index` read in a fragment 74, framebuffer fetch 28, argument buffers 14, amplification
  4); round 5's 170 output and colour-index variants give 46 equal (round 5: 36) and 16 not reflected.
- WindowServer (data for its decision; the road stays off there): its own caches (`<user cache>/WindowServer/nvmtl`)
  are readable only by `_windowserver`, but its functions are on disk: SkyLight's `SkyLightShaders.air64.metallib` (31
  functions: `SimpleVertex`, `SimpleTextureFragment`, `UberResampleLanczosFragmentBGRA` and the other names its log
  shows) and QuartzCore's `default.metallib` (180 AIR modules). Their modules taken out of the metallibs and run through
  the frontend in a normal process (translation, reflection against the translator's, compute pipelines; no dispatch,
  WindowServer not touched): of 211 functions without function-constant values, 166
  translate and their reflection equals the translator's (120 fragment, 39 vertex, 7 kernels); 45 are refused:
  imageblocks 16, framebuffer fetch 11, a function constant read without its value 6, a stage input (5) or an output (2)
  under a constant without its value, linear pixel-coordinate samples other than 2D `clamp_to_edge` 5. SkyLight's
  alone: 18 of 31 (`SimpleVertex` and `SimpleTextureFragment` among them; `UberResampleLanczosFragmentBGRA` reads a
  function constant). The 7 kernels make their compute pipelines; the 17 other kernels are refused (16 imageblock). With
  each module's constants all 0 and all 1 (the 13 modules that read constants, 26 variants): 10 equal, 8 not reflected
  (initializer-computed gates), 8 refused (framebuffer fetch 6, pixel-coordinate linear 2). The vertex and fragment
  functions were translated and reflected one by one; their pipelines need the pairs and descriptors WindowServer
  makes, which are not on disk.

## Terminal's GPU fault, the per-process fallback and WindowServer (round 7)

**The fault.** On 3.0.21 to 3.0.23 every Terminal launch faulted the GPU (Xid 31) 40 to 70 ms after its fourth render
pipeline was made, rebuilt its channel three times and lost the device, and its window stopped drawing. Of its eight
pipelines one took the road: QuartzCore's `buffer_image_blit_vert_lpf` / `buffer_image_blit_frag_lpf` (its two `nir1`
shader-cache entries were born in Terminal's first launch of the build and touched by every later one; no other process
of the boot made that pipeline on the road). The fragment reads one byte of a device buffer at
`int(min(floor(uv.x), x_max) + offset + min(floor(uv.y), y_max) * bpr)`. NVK's frontend lowered `air.convert.s.i32.f.f32`
to a bare `f2i32`, and NAK's F2I gives `0x80000000` for a NaN, so a NaN index read 2 GiB below the buffer; Metal, and
the translator's every `OpConvertFToS`/`OpConvertFToU` (`translator/src/passes/finalize.rs`
`metal_nan_to_zero_conversions`), give 0. The same pipeline with no NaN in its inputs drew right on the road
(`r_rleblit`, `r_qcblit` with QuartzCore's own AIR, an R8 target, managed buffers, a warm cache): its NIR in the harness
is byte for byte the NIR Terminal cached. `conv_edge` (a NaN converted: `0x80000000` on the old road, 0 on the SPIR-V
road) and `r_rleblit_nan` (the blit with a NaN offset: on the 3.0.23 NVK one Xid 31 and one channel rebuild in the
harness, as in Terminal) reproduce it; both pass on both roads now.

**The translator's Metal rewrites, and the frontend's match of each** (the coordinator's audit; translator file, then
the frontend's place in `nvk/air/nvk_air_to_nir.c`):

| rewrite in the translator | the frontend before | now | test |
|---|---|---|---|
| a NaN converted to an integer is 0 (`finalize.rs:288` `metal_nan_to_zero_conversions`) | `f2i`/`f2u`: F2I's `0x80000000` | fixed: `air_f2int` (every `air.convert` from a float, `fptosi`, `fptoui`) | conv_edge, r_rleblit_nan |
| `OpIsNan`, `OpIsInf` keep NaN and infinities (spirv_to_nir's `nir_fp_preserve_nan`) | `x != x` under fast math, which NIR may fold to false | fixed: `air_fisnan`, `isinf` and `isfinite` with NaN and infinities kept | conv_edge |
| precise `fmin`/`fmax`/`min`/`max`/`clamp`/`fmin3`/`fmax3`/`fmedian3`, `llvm.minnum`/`maxnum` are NMin/NMax/NClamp (`passes/air_calls/bfloat_glsl.rs:217` `nan_aware_if_precise`); `fast_` ones FMin/FMax/FClamp | fast-math `fmin`/`fmax` for both | fixed: `air_fminmax` keeps NaN for the precise ones and infinities for both, as spirv_to_nir does | minmax_nan (passed before too: NIR did not fold these cases) |
| `round` is Metal's: truncation, plus one toward the sign when the remainder reaches a half (`float_imageblock.rs:942` `lower_metal_round`) | `sign(x) * floor(abs(x) + 0.5)`: 0.49999997 rounded to 1, 2^23 + 1 to 2^23 + 2 | fixed: `air_metal_round` | round_edge |
| `pow` has C's special values (`rawbyte_unary.rs:465` `lower_metal_pow`) | `fpow`: NaN for every negative base | fixed: `air_metal_pow` (`powr` stays `fpow`) | pow_edge |
| a float narrowed to a half is NoContraction (`finalize.rs:351` `exact_narrowing_float_conversions`) | `f2f16_rtne` under fast math | fixed: `air_f2f16` is exact | narrow_exact (passed before too) |
| a buffer of a runtime array indexed with data stays a descriptor, bounds-checked for graphics with robustBufferAccess (`wrapper/src/buffer_addresses.rs:203` `kept_roots`) | read through its address: past its end an MMU fault | fixed: such a vertex or fragment function is refused, `air_refuse_unchecked_indexing` (compute is not bounds-checked on either road) | r_oob |
| `fmod` is `x - y * trunc(x / y)` (`float_imageblock.rs:384`) | `frem`, the same definition | matched | fmath (round 3) |
| `sdiv`/`srem`/`udiv`/`urem`, shifts: one SPIR-V instruction each (`native/emitter/body/mod.rs:45`), spirv_to_nir's `idiv`/`irem`/`udiv`/`umod`/`ishl` | the same NIR operations | matched | imath (round 3) |
| `air.compile.denorms_disable` sets `denorm_flush_to_zero_f32` (`lib.rs:169`), which nothing emits | no float-control mode | matched (neither road flushes) | - |
| `saturate` is NClamp/FClamp(x, 0, 1) (`float_imageblock.rs` `saturate`) | `fsat` | matched in value (NaN gives 0 on both) | fmath |
| `sinpi`/`cospi`/`tanpi`, `exp10`/`log10`, `fast_tanh`, `ldexp`, half functions in float (`lower_metal_pi_scaled_sin_cos`, `lower_fast_tanh`, `lower_fast_ldexp`, `half_glsl_op`) | the frontend's own formulas | differ in precision only (no defined edge value at stake); left | fmath, math_lib |

**The per-process fallback** (`plugin/NVMTLAirNir.m` `nvmtl_air_nir_gpu_fault`, called from `plugin/nvmtl_vk.c`
`nvmtl_vk_note_dropped` and `nvmtl_vk_note_loss`): a channel rebuild that drops a command buffer, or a device lost, in a
process that made any pipeline or variant on the road turns the road off there for good (`nvmtl_air_nir_on` answers 0
from then on). New pipelines and variants take the SPIR-V road; a render pipeline of the road draws as its SPIR-V twin
from its next bind (`nvmtl_air_nir_render_bound`, the twin made once from its descriptor), a compute pipeline of the road
dispatches as its twin (`nvmtl_air_nir_for_size`); pipelines already in command buffers keep theirs. The process name
goes on `airnir-off-<build>.txt` in the plugin's shader-cache folder (the build: a digest of this plugin's and this NVK's
identity; another build's list is removed when a process opens the folder), and a process on it starts on the SPIR-V
road (`compute and render pipelines: AIR to NIR off in <process> (the process faulted the GPU on the road before ...)`).
Said once in the system log: `AIR to NIR off in <process> from now on: a GPU fault (...) after N pipeline(s) ...`. To
see the list: `ls ~/Library/Caches/nvmtl/spvcache/airnir-off-*` (a sandboxed app's is in its container's Caches).

**WindowServer** takes the road since 3.0.24, with the per-process fallback and a crash marker
(`nvmtl_air_nir_ws_arm`): `airnir-ws-<build>.marker` in its own cache folder (`<user cache>/WindowServer/nvmtl/spvcache`)
is written before its first pipeline on the road and removed after 60 s on the road without a GPU fault; a WindowServer
start that finds it (the one before died in its first minute) puts WindowServer on the fallback list, so that start and
the rest of the driver build stay on the SPIR-V road. `NVK_FAULT_INJECT=ws:<seconds>:<process>` (a development plugin
only; NVK takes no mode `ws`) runs that logic in a normal process.

### Tested (round 7)

- Through the plugin (`r7/final-runs.sh`: two runs on each road, one with the caches of the first, one with
  `PLUGIN_RUN_CORRUPT=1`, then the marker and fallback tests; QuartzCore's two blit functions given with
  `PLUGIN_RUN_EXTRA_AIR`): on the road 81 pass, `grid2d_base` skipped as before (round 6's 69, the five edge tests and
  seven render pairs); eight take the SPIR-V road as they must: the four function-constant refusals, `r_primid`,
  `r_oob`, and `r_buffers` and `r_nospace`, whose fragments index their palette with data (`pal[idx[i] & 15]`), so the
  bounds rule sends them where the palette stays a checked descriptor. 97 functions are made without the translator.
  With `NVMTL_AIR_NIR=0` the same five tests as in rounds 4 to 6 fail (`fmath`, `fmath_precise`, `math_lib`,
  `tex_offset`, `align_misaligned`) and the new ones pass. With `PLUGIN_RUN_CORRUPT=1` every test that dispatches or
  draws fails. The fault counters moved only for the one fault injected on purpose (`fallback`: one channel rebuild,
  `chan_recovered` and `chan_dropped` +1, `xid_count` and `chan_gave_up` unchanged); WindowServer logged no GPU error.
- Before the fix, with the installed 3.0.23 NVK under the development plugin: `conv_edge` (NaN converted to
  `0x80000000`), `round_edge` (0.49999997 rounded to 1) and `pow_edge` (`pow(-2, 3)` NaN) fail, and `r_rleblit_nan`
  faults the GPU once (Xid 31, one channel rebuild, as in Terminal); after it all pass on both roads.
- `fallback` (`NVK_FAULT_INJECT=mmu:6:plugin_run`, set by `plugin_run`): the blit and `conv_edge` made on the road; the
  sixth flush faults and is dropped; the plugin says the road is off, the blit then draws right as its SPIR-V twin,
  `conv_edge` dispatches right as its twin, a new pipeline takes the SPIR-V road, and the list names `plugin_run`.
  `fallback_next` (a second process with those caches): the road is off from the start because the list names it.
- `ws_marker`, `ws_marker_crash`, `ws_marker_next` (`NVK_FAULT_INJECT=ws:2:plugin_run`): the marker is set before the
  first pipeline on the road and cleared after a clean 2 s; a process that ends with it set leaves it; the next start
  finds it, stays on the SPIR-V road, puts itself on the list and removes the marker.
- Host: `tools/test_air_nir_plugin.py` (53: the list's and the marker's helpers, the fault hooks, the NaN rule and the
  marker, each with its negative control), `tools/test_switch_registry.py`.

## Framebuffer fetch, linked samplers, arrays of textures and choices of handles (round 8)

Part A of round 8 (3.0.24, the desktop since its boot): no fault, no fallback; of the 440 `nir1` shader-cache entries
the boot's processes made, 209 were refusals, 161 of them framebuffer fetch (`the fragment argument kind
air.render_target`: Core Animation's and RenderBox's fragments read `[[color(n)]]`), 10 a constexpr sampler's global
defined twice differently. Round 8 took those two first, then the rest of round 7's list.

**Framebuffer fetch on the road** (ABI version 6, `NVMTL_AIR_NIR_COLOR_INPUTS`). How the SPIR-V road carries a
`[[color(n)]]` read: the translator's stage-input pass makes it an input attachment (`SubpassData`, not multisampled) at
set 3, binding 192 + n, `InputAttachmentIndex` n, read once at the entry with `OpImageRead` at offset (0, 0) as a vec4
of 32-bit floats (a float or half input) or unsigned 32-bit integers, then cut to the argument's components and
narrowed to a half (`passes/stage_input/mod.rs`, `resources/collapse.rs` `adapt_input_attachment_read`). The plugin
finds the bindings 192 + n in the module (`nvmtl_spirv_fetch_mask`) and, with fetch on (every process but
WindowServer), declares every pass's colour attachments as input attachments in `GENERAL` layout, puts set 3 (the
input-attachment layout) in the pipeline layout, binds the open pass's colour views there behind a by-region barrier
before each draw (`fetch_mask`), and draws a small triangle list one triangle at a time so later triangles read earlier
ones (`nvmtl_fetch_split`); with fetch off (WindowServer) set 3 holds the zeroed dummy image (`fetch_zero`); with fetch
emulated by copies (`NVMTL_FETCH_COPY=1`, WindowServer's `nvfetchcopy=1`) the module is rewritten to texel fetches of
copies (`nvmtl_fetch_copy.h`). Now: the frontend makes the same read, as NIR (`nvk/air/nvk_air_to_nir.c`, the
`air.render_target` branch of `air_bind_stage_input`): an image variable `glsl_image_type(SUBPASS, ...)` at set 3,
binding 192 + n, `data.index` n, and one `image_deref_input_attachment_load` at the entry with base n, range 1, offset
(0, 0), what `spirv_to_nir` and `vk_nir_lower_input_attachment_loads` make of that `OpImageRead`, so NVK lowers it as it
lowers the SPIR-V road's (`nir_lower_input_attachments` in `nvk_preprocess_nir`, which runs for NIR stages too); every
input declared is in `result.color_inputs`, read or not, as the module's decorations are; the reflection gives each as
the translator's `ColorInput` binding. The road's render pipeline (`nvmtl_vk_pipeline_create_nir`) takes the SPIR-V
road's `fetch_mask`/`fetch_zero` from that mask and its set 3, so the draw binds and splits exactly as before. A process
that emulates fetch by copies leaves such a pipeline to the SPIR-V road (the plugin does not ask for color inputs there);
so does an NVK older than version 6. An input of 16- or 8-bit integers stays refused (the translator does not convert
them), and one under a function constant: known false, zeros and nothing declared (`function_constant_disabled`), not
given, refused.

**A linked function keeps its own constexpr sampler.** The 10 refusals: RenderBox's `custom_effect_fragment` (and its
`accumulator_`, `filter_` and `primitive_custom_fragment` siblings) calls the effect through `custom_fn.MTL_VISIBLE_FN_REF`,
linked at pipeline creation; each module names its first constexpr sampler `@__air_sampler_state`, with different
states (the fragment's repeat and linear, `0x...80a92`; the effects' clamp-to-zero, pixel coordinates, `0x...88a00`, and
others), not equivalent. The translator's linker (`linked_functions.rs` `append_dependency_module`) appended the effect's
definition beside the fragment's, so its emitter resolved both functions' samplers to the last definition: on the SPIR-V
road the fragment sampled with the effect's state (`r_linksamp`, fixed); and it dropped the dependency's metadata, so a
linked function's own constexpr samplers were missing from `!air.sampler_states` (the emitter bound them, the
reflection, which the plugin binds from, did not list them). Fixed at the source: a dependency's local global that the
module defines with another line gets a fresh name (`name.linkedN`) with every use in that module; a line that differs
only in a trailing `, align N` is the same definition; the dependency's constexpr samplers are added to the module's
`!air.sampler_states` (new nodes after the module's highest one). A render pipeline's `_vfn`/`_ffn`, which the draw binds the
constexpr samplers and embedded resources of, are now the stages the pipeline was built from (the linked ones; the same
objects as the descriptor's when nothing is linked), not the caller's unlinked functions. The frontend drops, before LLVM parses the text, a declaration of a function the text defines and a
repeated declaration of the same signature (the linker leaves both; LLVM takes a name once); a function declared twice
with different signatures is refused.

**Arrays of textures** (`NVMTL_AIR_NIR_TEXTURE_ARRAYS`). `array<texture2d<T>, N>` at `[[texture(n)]]` arrives as a byval
pointer to N handles; element k is texture n + k. The translator under bindless-all (the default) reads every element
of a sampled array through the per-draw table and the bindless heap (`resources/texture_array.rs`
`route_texture_array_elements_through_heap`: the slot at table entry 64 + n + min(i, N - 1), low 32 bits masked to the
heap's 131072 slots, into the heap at set 2, binding 0, non-uniform), because the plugin declares every texture index
as its own count-1 binding and an `[N x image]` variable at 32 + n read element 1 past it; a written array is a
descriptor array at 480 + n. The frontend: an element whose index is known is texture n + k at its own binding (32 + n
+ k, 480 + n + k when written), as a `[[texture(n + k)]]` is; a sampled element chosen at run time takes the
translator's heap path exactly (`NVMTL_AIR_NIR_HEAP`, which the plugin sets when its layouts have the heap;
`result.resource_flags` `NVMTL_AIR_NIR_RES_HEAP`). A written element or a `texture_buffer` element chosen at run time,
and `array_ref<...>` (MPS's histograms pass it through objects in thread memory), stay refused. The reflection gives an
array's `array_length` and its descriptor count N, as the translator's.

**Texture calls through a choice of argument handles.** Apple's GL compiles a GLSL helper that takes a `sampler2D`
(Minecraft's `sample_lightmap`) by passing every texture and sampler and a switch on a literal the caller stored; after
the inliner and SROA the frontend met a phi of the argument handles. The translator folds such a phi to the one incoming
value its known branches leave (`passes/resource_phi_fold.rs`); the frontend now, when the function has a phi or select
of parameters in address spaces 1 and 2, runs LLVM's `sccp,instsimplify,simplifycfg,adce` after SROA, which folds the
same branches. A choice left after that stays refused where a texture call meets it.

**Argument indices from function constants** (`[[buffer(bidx + 1)]]`, `[[texture(tidx * 2)]]`: a
`__metal_implicit_attr_int_expr` global the static initializer fills) already translate with the constants' values, on
both roads (`fc_argindex`); the 572 of the cache sweep are refused only because the sweep gives no values.

**Argument buffers** (102 functions of the cache) are not done in round 8: a texture in an argument buffer is a
resource ID in memory (`NVMTLEmbedded.m`: bit 62 and a heap slot, bit 63 for a sampler), which the translator reads
through the heap, and the plugin also binds some of an argument buffer's resources into per-draw slots from the CPU
copy (embedded plans); matching both exactly is the next round's.

### Tested (round 8)

| test | what | tolerance |
|---|---|---|
| r_fetch | `[[color(0)]]` of an RGBA32Float attachment read and blended by hand, two instances (the second reads the first's) | 1e-6 |
| r_fetch_mrt | `[[color(0..2)]]` read as half4 (RGBA8Unorm, swizzled), float (R32Float), uint (R32Uint), two instances | rgba8 2 LSB, 1e-5, exact |
| r_fetch_noatt | `[[color(1)]]` read with no attachment 1: the dummy image's zeros | 1e-6 |
| r_linksamp | a fragment's constexpr sampler (nearest, clamp_to_zero) and a linked visible function's (nearest, repeat), both `@__air_sampler_state` in their modules | exact |
| r_handlechoice | textures and samplers chosen by a switch on a known value (built with `-O0`; skipped on the SPIR-V road, whose translator does not take that module) | 1/128 |
| tex_arrays | `array<texture2d<float>, 4>` read by known and run-time indices (one past the end: the last element), `array<texture2d<float, access::write>, 2>` written | 1e-6; rgba16f 2e-3 |
| fc_argindex | a buffer and a texture whose indices are function-constant expressions | 1e-6 |

- Through the plugin (`r8/final-runs.sh`, the translator of this tree, two runs on each road, one with the caches of
  the first, one with `PLUGIN_RUN_CORRUPT=1`; `xid_count` and `chan_gave_up` stayed at 0, and `chan_recovered` and
  `chan_dropped` rose only for the faults `fallback` injects on purpose, one channel rebuild in the harness's own
  process each; WindowServer logged no GPU error): on the road 88 pass (round 7's 81 and the seven above), `grid2d_base` skipped as before; the same eight as
  in round 7 take the SPIR-V road as they must. With `NVMTL_AIR_NIR=0` the same five as in rounds 4 to 7 fail (`fmath`,
  `fmath_precise`, `math_lib`, `tex_offset`, `align_misaligned`) and `tex_arrays` (element 1 of the written array is
  lost: the translator's descriptor array at 480 + n meets the plugin's count-1 bindings; old), `r_handlechoice` is
  skipped, the others pass. With `PLUGIN_RUN_CORRUPT=1` every test that dispatches or draws fails. The fetch tests with
  `NVMTL_NO_FBFETCH=1` (zeros) pass on both roads; with `NVMTL_FETCH_COPY=1` too they take the SPIR-V road and pass.
  `r_linksamp` with the installed 3.0.24 translator fails on the SPIR-V road (the fragment's sampler read as repeat).
  The frontend's reflection equals the translator's for every new function (`NVMTL_AIR_NIR_REFL_CHECK=1`).
- `sweep.py --reflect` over the AIR cache (round 5's list): of 10,814 functions the frontend translates 5,559 (round 6:
  5,517; 3,539 kernels, 689 vertex and 1,331 fragment functions), every reflection equal to the translator's (different
  0, not reflected 0); it refuses 5,253 (5,295); two are the MPS modules whose translator reflection does not finish.
  Refused, the first ten: a function constant read without its value 4,023, an argument index from one 572 (no values
  in the sweep), post-tessellation vertex functions 223, a stage input under a constant without its value 71,
  `array_ref` texture arrays 56, fragment argument buffers 44, vertex argument buffers 37, imageblocks 33, a
  `[[color(n)]]` input under a constant without its value 27, kernel argument buffers 21. Framebuffer fetch (56 in round
  6, apart from those 27) and the texture call through a choice of handles (42) are gone.
- WindowServer's functions on disk (SkyLight's and QuartzCore's metallibs, offline in a normal process): 173 of 211
  translate with reflections equal to the translator's (round 6: 166); refused: imageblocks 16, linear pixel-coordinate
  samples other than 2D `clamp_to_edge` 5, stage inputs and outputs under constants without values 7, the bounds rule
  of round 7 2, raster order groups 2, function constants without values 6. With each module's constants all 0 and all
  1 (26 variants): 10 equal, 8 not reflected (initializer-computed gates), 8 refused.
- Host: `tools/test_air_nir_plugin.py` (57, with the two round-8 checks and their negative controls),
  `tools/test_embedded_fast.py`, `test_fetch_copy.py`, `test_a8_framebuffer_fetch.py`, `test_graphics_bindings.py`,
  `test_address_snapshot.py`, `test_switch_registry.py`, `test_refusal_reasons.py`; the translator's unit tests (2,304,
  with the two linker tests) and its integration tests (2,333 pass; the four of `declared_capabilities_are_needed.rs`
  fail as they do at the round-7 commit: they inspect no fixture here).

## Argument buffers, bounds-checked buffers, pixel-coordinate samples and written arrays (round 9)

Part A of round 9 (3.0.25, the desktop since its boot, read only): one GPU fault, in Terminal at 19:32 (Xid 31, one
channel rebuilt; WindowServer logged nothing). Its pipeline on the road was QuartzCore's `buffer_image_blit_vert_lpf` /
`buffer_image_blit_frag_lpf` again, but not round 7's NaN: the fragment's helper invocations (the lanes of a 2x2 quad
outside the glyph's rectangle) read `bpr` bytes before the glyph buffer. On the SPIR-V road those lanes never read
anything: the plugin rewrites an invocation-independent fragment module that reads only physical buffers so that its
helper invocations return at the entry (`plugin/nvmtl_independent_helpers.h`, applied in `nvmtl_address_shader_g0`,
`plugin/nvmtl_vk.c`). Round 7's NaN fix was right, but Terminal had not run that pipeline on 3.0.24.

**Helper invocations return, as on the SPIR-V road.** The frontend decides the same thing on its NIR
(`air_helpers_independent`: no texture, image or sampler variable, no texture instruction, only plain loads, stores,
inputs and system values, and at least one global access, as `nvmtl_helpers_independent` asks of the module's
instructions and of a physical pointer type) and puts the whole entry under `if (!helper_invocation)`
(`air_guard_helpers`), saying so in `result.fragment_flags` (`NVMTL_AIR_NIR_FS_HELPERS_RETURN`, ABI version 7). The
tests `r_rleblit_helpers` and `r_qcblit_helpers` (QuartzCore's own functions) draw rectangles whose quads straddle the
buffer's start, with a 256 MB buffer behind the first: on 3.0.25 the road faults there (shown once, in the harness's own
process), now neither road does.

**The plugin's rewrites of a SPIR-V module, and the road** (what the SPIR-V road does to the translator's module before
or at pipeline creation, and where the road does the same or stays off). Line numbers are this commit's.

| rewrite (SPIR-V road) | where | the road |
|---|---|---|
| refuse a module that indexes the heap without one | `nvmtl_vk.c:6766` (compute), `:6510` (render) `nvmtl_heap_binding_refused` | asks for the heap only with its bindings (`NVMTL_AIR_NIR_HEAP`, `_ARGBUF` with five), `NVMTLAirNir.m:283` |
| buffer-address lowering, buffers kept as descriptors (`kept_roots`) | `nvmtl_vk.c:6768`, `nvmtl_address_shader_g` | the same buffers through their descriptors (`NVMTL_AIR_NIR_DESCRIPTORS`, below) |
| the table at 640 a Uniform block | `nvmtl_address_shader_g_uncached` | `NVMTL_AIR_NIR_TABLE_SSBO` off: the same uniform table |
| align guard, SAFE build on a misaligned binding | `nvmtl_vk.c:6996` `nvmtl_align_guard_pick` | `NVMTL_AIR_NIR_ALIGN4` build (`nvmtl_air_nir_safe_build`), the same switch |
| read-only metadata | `nvmtl_vk.c:5762` | access flags only, no effect on results |
| `InBounds` chains relaxed to plain ones | `nvmtl_vk.c:5686` `nvmtl_relax_physical_bounds` | plain 64-bit address arithmetic: the same addresses |
| helper invocations return | `nvmtl_vk.c:5689` `nvmtl_independent_helpers` | the same (`air_guard_helpers`, above) |
| the application profile's edits | `NVMTLAirNir.m:575` `nvmtl_app_profile_edit_module` | a function it edits stays on the SPIR-V road (`NVMTLAirNir.m:576`, `:768`) |
| the threadgroup-size seed | `nvmtl_vk.c:6752` `nvmtl_cvar_seed` | translated for the dispatch's size |
| the BDA probe's hashes | `NVMTLDevice.m:3043` | off the road with `NVMTL_BDA_PROBE_FILE` (`NVMTLAirNir.m:574`, `:754`, `:1087`) |
| A8 framebuffer-fetch remap, A8 variant | `nvmtl_vk.c:6525`, `NVMTLDevice.m:2574` | refused (`NVMTLAirNir.m:755`) |
| vertex amplification (multiview variants) | `NVMTLDevice.m:2586` | refused (`NVMTLAirNir.m:756`) |
| tessellation (TES patch, generated TCS and VS) | `nvmtl_vk.c:6544` `nvmtl_tess_patch_tese` | refused (`NVMTLAirNir.m:761`; the frontend refuses a post-tessellation vertex function); since round 10 the same patch made by the frontend, the same TCS and VS (below) |
| the GL uniform buffer | `NVMTLDevice.m:2658` | refused (`NVMTLAirNir.m:762`) |
| framebuffer fetch by copies | `nvmtl_vk.c:6620` | refused (`NVMTLAirNir.m:763`) |
| stencil export emulation | `nvmtl_vk.c:5243` `nvmtl_spirv_stencil_emu` | refused (`NVMTLAirNir.m:766`) |
| programmable sample positions | `nvmtl_vk.c:5584` | refused (`NVMTLAirNir.m:767`) |
| the fragment's set moved to 1 | `nvmtl_vk.c:6590` `nvmtl_spirv_retarget_set` | the frontend puts a fragment's resources in set 1 |
| varyings linked by user semantic | `NVMTLDevice.m:1313` `nvmtl_varying_link` | the same remap handed over as Locations (`NVMTL_AIR_NIR_VARYINGS`) |
| fetch mask and zeros | `nvmtl_vk.c:6593` | from `result.color_inputs` |
| vertex input component kinds | `nvmtl_vk.c:6562` `nvmtl_spv_input_kinds` | `result.vertex_input_kind` |
| `NVMTL_SKIP_FS` | `nvmtl_vk.c:6638` | a diagnostic of the SPIR-V road only |

NVK's own patches (`nak_nir.c`, `from_nir.rs`, `nvk_shader.c`) apply to both roads. Two differed: the helper return
(fixed) and the kept descriptors (fixed below).

**Argument buffers** (ABI version 7, `NVMTL_AIR_NIR_ARGBUF`, `NVMTL_AIR_NIR_TYPED_DUMMIES`). An `air.indirect_buffer`
argument is a buffer whose address comes from the table. What it holds is what Metal writes into it (tier 2): a texture
field its `gpuResourceID` (bit 62 and the texture's heap slot), a sampler field its own (bit 63, a sequence, the sampler
heap's slot), a pointer field a GPU address. The translator reads a handle loaded from memory through the bindless heap:
the low word masked to the heap's 131072 slots (2048 for samplers) picks the sampled image (set 2, binding 0), storage
image (2), uniform or storage texel buffer (3, 4) or sampler (1), non-uniform, slot 0 the typed dummy of the image's
shape (`passes/resources/dummy_slots.rs`). Fields it can bind per draw it does: a written texture that is one field of a
directly bound argument buffer at a known offset is the embedded binding 480 + its synthetic index (the highest
top-level texture index + 1, in field order), a sampler field 160 + its synthetic index when every sampler read from
memory is one (`meta/embedded.rs`). The frontend does exactly that (`air_ab_detect`, `air_handle_texture`,
`air_handle_sampler`), and loads through a pointer read from memory as the translator's `null_bda_load_helper` does (0
when the pointer is 0; stores unguarded). Its reflection lists the translator's `EmbeddedArgBufferTexture`,
`EmbeddedArgBufferSampler` and `EmbeddedArgBufferBuffer` bindings and argument-buffer fields; the per-draw slots it
reads go to the plugin as `air_nir_per_draw` in the reflection, and the plugin's embedded plan for a road pipeline binds
those and no others (`NVMTLEmbedded.m nvmtl_embedded_plan`). Two fixes on both roads: the plugin bound an argument
buffer's `texture_buffer` as an image (its texel buffer now, `nvmtl_bind_embedded`), and the translator lost element 1
of a written texture array in an argument buffer (`register_embedded_textures` now routes such an array, and a written
field loaded at a run-time offset, through the storage heap). An argument buffer that an index read from memory selects
within is one the translator keeps a descriptor (below): the frontend refuses it (`r_argbuf_data`).

**Buffers the translator keeps bounds-checked** (`NVMTL_AIR_NIR_DESCRIPTORS`). The translator's wrapper lowers a buffer
to its address unless an access chain indexes its runtime array with a value it counts as data and its pointers reach
only chains, loads, stores and `OpArrayLength` (`wrapper/src/buffer_addresses.rs kept_roots`); such a buffer stays a
storage-buffer descriptor, which a render pipeline bounds-checks (robustBufferAccess: a read past its end gives 0).
Round 7 refused a vertex or fragment function that indexed an unsized buffer with a loaded value; over the cache that
missed 30 functions the translator keeps a buffer of (Apple's GL `__main`: a sized byte buffer indexed through local
variables), which the road read unchecked. Now: the frontend finds, on the module as the request gives it (before LLVM's
inliner and SROA, as the translator reads it), every buffer a GEP indexes with something the translator may count as
data (a load, a call, a phi, an internal function's parameter, a kernel's grid position (push constants), a fragment's
`[[color(n)]]` input, 24 values deep) and refuses with those in `result.descriptor_wanted`. The plugin then makes the
translator's module (`nvmtl_function_spirv`), lowers it as the SPIR-V road's pipeline does and reads the set-0 storage
buffers left below binding 32 (`nvmtl_vk_descriptor_buffers`, `nvmtl_air_nir_kept_buffers`), and asks again with them.
The frontend reads those buffers through their descriptors (the stage's set, binding n, the pipeline's robustness,
32-bit offsets), in components of 8, 16 and 32 bits: NVK checks a bounds-checked access component by component against
the range rounded up to 4 bytes (`nir_lower_explicit_io`, `NVK_SSBO_BOUNDS_CHECK_ALIGNMENT`), so such a component reads
what the translator's module reads whether that reads it alone or within its aligned word. Refused with a message: a
64-bit component (the translator reads one as one value in some modules and as two words in others, which differ when
the buffer ends between them), a store (guarded against offsets past 4 GiB in one of its forms and not the other), an
access below its natural alignment, and the pointer used as a value; a phi or select of one buffer's pointers stays its
offset (RenderBox's uber vertex function searches its segments that way). The plugin binds them as the SPIR-V road does
(`nvmtl_air_nir_masks`: bits 0-31). A kernel without buffer robustness (the default) is not asked: below 4 GiB its kept
buffer is at the same address either way. Over the 2,541 vertex and fragment functions of the cache, no buffer the
translator keeps is left unasked (`airsweep` with `AIRSWEEP_KEPT_ALL=1`); 215 are asked (for 56 the translator keeps one
of the buffers asked about), 187 then translate, 28 are refused. A buffer bound at an offset below NVK's 16-byte
storage-buffer alignment cannot be a descriptor: the plugin binds the dummy there on both roads (old on the SPIR-V road;
the SAFE build of a kernel without robustness now reads every buffer through its address, below).

**Pixel-coordinate samples, as the translator lowers them.** A sampler with pixel coordinates whose state is known here
(constexpr, or given by the request): the hardware filter at `coord / size` only for a 2D linear `clamp_to_edge` sample
with no mip filter, level 0 (none outside a fragment function: a fragment's implicit sample would let the hardware pick
a level) and no offset (`sample.rs pixel_linear_hw_coord`); every other linear sample of a float texture (1D, 2D, 3D, 2D
arrays, any level, offsets, every address mode the words carry: Metal's compiler allows `clamp_to_zero`, `clamp_to_edge`
and `clamp_to_border`, the translator also emulates `repeat` and `mirrored_repeat`) the two, four or eight texels at
`floor(coord - 0.5)` weighted by the fraction, the coordinate first kept finite (NaN to -9) and, for the periodic modes,
wrapped into one period (`lower_pixel_linear_sample`, `clamp_pixel_coord_component_finite`); a nearest sample, an
integer texture's or an array's the texel at `floor(coord)` addressed by the modes (`build_pixel_fetch_coord`); a gather
the four texels around `coord - 0.5` (`lower_pixel_gather_2d`). Before, the frontend sent a fragment's implicit-level
sample, a depth texture's, a gather and a sample with an offset through the hardware filter. Refused, as the translator
refuses them (`StaticSamplerState::validate_lowering`): mixed min and mag filters, linear mips, anisotropy, an LOD bias
or minimum clamp, a min/max reduction, a border other than transparent black; and bicubic samples (the translator's
sixteen-texel emulation is not ported).

**Written texture arrays, on both roads.** The translator declared `array<texture2d<T, access::write>, N>` (and arrays
of `texture_buffer`s) as one array binding at 480 + n, which the plugin's layout holds with one descriptor: every
element but the first was lost (`tex_arrays` failed on the SPIR-V road since round 8).
`resources::split_storage_texture_arrays` now gives an element chosen by a constant index its own binding (480 + n + k;
32 + n + k for a sampled `texture_buffer`), as the frontend does, and reads a written image's element chosen at run time
through the storage heap at the slot the per-draw table holds for texture n + min(index, N - 1). The frontend reads such
an element the same way (it refused it), and every heap image access is non-uniform, as the translator decorates it. The
plugin writes a bound storage texture's view at binding 2 of its table slot (`nvmtl_bl_storage`; the slot's sampled view
came from `nvmtl_bl_slot`), reset when the slot is dropped. A `texture_buffer` element chosen at run time keeps the
translator's array binding and is refused.

**MPS's `array_ref` of textures.** Its length is a function constant's; the translator reaches the texture indices left
from its base, 128 at most, every element through the heap; an array argument reaching past index 128 keeps the elements
below it, as the translator clamps them. MPS's four batch copy kernels now translate with their constants, reflection
equal to the translator's (`tex_aref` rewrites an `array<>` argument to an `array_ref` to test it on the card). The 55
others link MPS's visible functions through the `array_ref` (`prefixPrimary_*.MTL_VISIBLE_FN_REF`) and call
`air.get_null_texture_2d`: refused; they need visible-function linking for kernels on the road and null texture handles.

**Post-tessellation vertex functions** (223 of the cache) stay refused. The plugin's tessellation path works on the
translator's TessellationEvaluation module: it reads it (`nvt_scan_tese`), patches its spacing, vertex order and
patch-id input (`nvmtl_tess_patch_tese`) and builds a TCS and a pass-through VS from what it read
(`nvmtl_tess_make_tcs`, `nvmtl_tess_make_vs`). To feed it from the road the frontend would make a
`MESA_SHADER_TESS_EVAL` shader with the translator's patch-input Locations, `gl_TessCoord` and patch id, the plugin
would set those modes on NIR and generate the TCS and VS from a description in the result instead of the module, and NVK
would take a pipeline mixing SPIR-V and NIR stages; none of that exists.

**Refusals confirmed.** A kernel with an imageblock argument (33) is refused (`ib_refuse`): the frontend has no tile
memory. A stage input or a varying under a function constant the pipeline does not give: the translator reads it as 0
(the argument does not exist), the frontend cannot know the pipeline means 0 and refuses (`r_fcin_missing` checks that
the pipeline takes the SPIR-V road and draws right there).

**The SPIR-V road's five old failures**, decided by the Metal Shading Language specification and fixed in the
translator, so both roads pass the same tests:
- `fmath`, `fmath_precise` (fma): the specification's accuracy tables (section 8.2 in version 4.1) give single-precision
  `fma` as correctly rounded, one rounding. GLSL.std.450's `Fma` reaches NIR as `ffma_weak`, which NIR splits into a
  multiply and an add when it may contract; the translator now decorates it `NoContraction`, which keeps it fused, as
  the frontend's `ffma`.
- `math_lib` (asin + acos): GLSL.std.450's `Asin` and `Acos` are polynomial approximations ~1e-4 off near +-1, far
  outside the few ulp the specification's tables give them; the translator now computes `atan2(x, sqrt(max(1 - x*x,
  0)))` and `atan2(sqrt(max(1 - x*x, 0)), x)` (`lower_metal_asin_acos`), as the frontend does (whose `fmax` now keeps
  infinities as vtn's GLSL `FMax` does).
- `tex_offset`: a sample's offset is an `int2` (signed); one known only at run time that arrived as uint words was
  converted unsigned (-4 became 4294967292) in `dynamic_i32_offset_components`; now signed.
- `align_misaligned`: Metal asks a device buffer's binding offset to be aligned to the type the function reads
  (`setBuffer:offset:atIndex:`); the plugin's align guard runs a SAFE build when it is not, as Apple's GPUs read it. The
  guard did not see a buffer the lowering kept a descriptor, and a descriptor cannot hold offset 4. The guard now counts
  those accesses (`wrapper/src/align_guard.rs`), and the SAFE build of a kernel without buffer robustness lowers every
  buffer to its address (`nvmtl_lower_buffer_addresses_ex` flag 8). With `NVMTL_COMPUTE_ROBUST=1` the test is skipped: a
  bounds-checked descriptor still cannot be bound there.

**A sampler's heap slot outlived its sampler, on both roads** (found by a final run: `r_argbuf_data` one pixel one 8-bit
step off, once in about forty runs). A heap slot names one sampler state for the process
(`nvmtl_vk_bindless_sampler_slot`, keyed by the VkSampler), but the sampler interner destroyed the VkSampler when the
last sampler object of that state was released. A sampler of another state made later at the same handle was given the
old slot, whose descriptor still named the destroyed sampler: r_argbuf's argument-buffer samplers read as nearest
`repeat` (the pixel was `clamp_to_edge`'s texel 15 read as texel 0, and the misses at pixel (0,0) were the fragment
`Inner`'s `mirrored_repeat` read as `repeat`), 57 of 600 setups when its resources were made again in one process
(`PLUGIN_RUN_TIMES`). The heap now holds a reference on each sampler it places (`nvmtl_sampler_retain`); 600 of 600
pass. `r_argbuf_reuse` makes six samplers of other states, each released before the next, then r_argbuf's five, and
fails when two share a slot with different states: 6 of 6 runs without the fix, none with it.

### Tested (round 9)

| test | what | tolerance |
|---|---|---|
| ab_kernel | a kernel's argument buffer written by the CPU (tier 2): a texture and sampler, a device buffer, an inline constant, arrays of textures (known and run-time indices) and samplers, a nested one, a written texture, an array of argument buffers indexed at run time | 1/128 linear, 1e-6, 2e-3 rgba16f |
| ab_encoder | argument buffers through `MTLArgumentEncoder`: lengths, a nested encoder, an array of them, written texture arrays, texture buffers read and written | exact |
| r_argbuf | argument buffers in a vertex and a fragment function: fields, arrays of textures and samplers indexed at run time (instance, position), nested ones, arrays of them | 4e-5 |
| r_argbuf_data | r_argbuf indexed by values read from buffers: the translator keeps the argument buffer a descriptor; SPIR-V road only | 4e-5 |
| r_rleblit_helpers, r_qcblit_helpers | quads straddling a buffer's start with 256 MB behind: helper invocations return | 1 LSB |
| r_oob, r_oob_nospace | a palette read at indices the data gives, past its end (0) and past 4 GiB (the 32-bit offset wraps), with and without `air.address_space` | exact |
| r_oob2 | a vertex function's palette past its end, 8- and 16-bit elements at buffers' tails (13 and 10 bytes), offsets past 4 GiB | exact |
| r_bsearch | a binary search through a phi and a select of pointers into a bounds-checked buffer, the bound past its end reading 0 | exact |
| r_buffers, r_nospace | round 4's, now through the descriptors the translator keeps | exact |
| oob_kernel | `NVMTL_COMPUTE_ROBUST=1` only: a kernel's bounds-checked palette past its end and past 4 GiB | exact |
| tex_pixmodes | pixel-coordinate samples, linear and nearest, edge and zero, 1D, 2D, 3D, 2D arrays, level 1, offsets, gathers, NaN coordinates | 2e-6; the hardware filter 0.035 |
| tex_pixwrap | the same with repeat and mirrored_repeat (state words rewritten in the module) | 2e-6 |
| tex_pixborder | a pixel-coordinate sampler with an opaque border: refused | - |
| r_pixfrag | a fragment's implicit-level pixel-coordinate samples (the emulation), level(0) (the hardware filter), an offset | 2e-6; 0.035 |
| tex_warrays | a written texture array indexed at run time and past its end, texture_buffer arrays by known indices | exact |
| tex_aref | an `array_ref` of textures, known and run-time indices, one past the bound ones | 1e-6 |
| r_fcin_missing | a stage input under a constant not given: SPIR-V road only | 1e-4 |
| ib_refuse | an imageblock argument: refused | - |
| r_argbuf_reuse | r_argbuf after six samplers of other states held heap slots and were released: no slot changes its state | 4e-5 |

- On the card (`tools/air-nir/plugin-run.sh` with the development plugin, this tree's NVK and the rebuilt translator,
  `MESA_SHADER_CACHE_DISABLE=true`; the four fault counters the same before and after every run: Xid 0, recovered 3,
  gave up 0, dropped 3, none new; WindowServer logged no GPU error): with `NVMTL_AIR_NIR=1` 106 pass twice and once with
  the first run's caches (97 on the road; `fc_refuse`, `fc_kernel_c`, `fc_kernel_none`, `fc_texture_none`, `r_primid`,
  `r_fcin_missing` and `r_argbuf_data` take the SPIR-V road as they must; `ib_refuse` and `tex_pixborder` are refused by
  both), `grid2d_base` and `oob_kernel` (only with `NVMTL_COMPUTE_ROBUST=1`) skipped. With `NVMTL_AIR_NIR=0` 105 pass
  twice, `r_handlechoice` skipped as well: none fails on the SPIR-V road (five did in rounds 4 to 8). With
  `PLUGIN_RUN_CORRUPT=1` every test that dispatches or draws fails on both roads (the six that pass are refusals).
  Compute with `NVMTL_COMPUTE_ROBUST=1`: 66 pass on each road (`oob_kernel` among them), `align_misaligned` skipped. The
  fetch tests with `NVMTL_NO_FBFETCH=1` pass on both roads and with `NVMTL_FETCH_COPY=1`; the seven pixel-coordinate
  tests with `NVMTL_NO_PIXEL_HW_FILTER=1` pass on both roads; `r_argbuf`, `r_argbuf_data` and `r_argbuf_reuse` each made
  50 times from their setup in one process (`PLUGIN_RUN_TIMES=50`) pass 150 of 150 on each road; the `ws_marker` trio
  passes.
- `sweep.py --reflect` over the AIR cache (round 5's list): of 10,814 functions the frontend translates 5,636 (round 8:
  5,559; 3,565 kernels, 718 vertex and 1,353 fragment functions), every reflection equal to the translator's (different
  0, not reflected 0); it refuses 5,176 (5,253); two are the MPS modules whose translator reflection does not finish.
  215 asked again with descriptor buffers: 187 then translate, 28 are refused. Refused, the first ten: a function
  constant read without its value 4,026, an argument index from one 572, post-tessellation vertex functions 223, a stage
  input under a constant without its value 72, a texture argument under one 59, imageblocks 33, a sample with a minimum
  LOD clamp 30, a `[[color(n)]]` input under a constant without its value 27, an output under one 24, a kernel's
  `stage_in` 17. Argument buffers (102 in round 8) and `array_ref` texture arrays (56) are gone from the list.
- WindowServer's functions on disk (offline in a normal process): 177 of 211 translate with reflections equal to the
  translator's (round 8: 173; 10 of them after the descriptor retry); refused: imageblocks 16, function constants
  without values 9, stage inputs under constants without values 5, outputs under one 2, raster order groups 2. With each
  module's constants all 0 and all 1 (26 variants): 12 equal, 14 not reflected (initializer-computed gates).
- Host: `tools/test_air_nir_plugin.py` (with the descriptor-mask and kept-buffer checks), `test_embedded_fast.py`,
  `test_a8_framebuffer_fetch.py`, `test_graphics_bindings.py`, `test_address_snapshot.py`, `test_switch_registry.py`,
  `test_refusal_reasons.py`, `test_cgl_gld_guard.py`, `test_host_buffer_release.py`, `test_import_cache_limit.py`,
  `test_mem_unmap_no_double_free.py`, `test_nocopy_once.py`, `test_nvk_local_map.py`, `test_readonly_copyback.py`,
  `test_static_sampler_device.py`, `test_texrc.py`, `test_submission_copies.py`, `test_slot_handle_guard.py`,
  `test_surfin_zero_plugin.py`, `test_zink_subdata_stage.py`, `test_opengl_perf.py` pass; `test_fetch_copy.py` skips (no
  glslangValidator).
- The translator's tests with `--no-fail-fast` (round 8's run stopped at the first failing test binary): 2,534 pass and
  24 fail, the wrapper's 61 pass and 2 fail, and round 8's translator tree fails exactly the same 26
  (`r9/ttests-r8.sh`). Of them 17 need more fixtures than the 5 public ones shipped (`deterministic_output`,
  `declared_capabilities_are_needed`, `live_module_scope_variables`, `name_independence`,
  `nonwritable_covers_the_module`, `reflection_*`, `no_module_needs_a_geometry_stage`: "swept almost nothing"); five
  report the bindless heap's set 2, binding 0 as declared and not reflected (the plugin binds the heap itself); two
  check image shapes (a texel-read cube reflected as a cube, not the 2D array it binds; one of four textures given a
  shape); the wrapper's two read a module from a literal `$HOME` path.

## Tessellation, raster order groups, dual-source blending, minimum-LOD clamps and kept buffers (round 10)

Part A of round 10 (3.0.26, the desktop since its boot, read only): clean. Terminal on the road (4 pipelines, none on
SPIR-V, no fault, no fallback list), no Xid, channel recovery, device loss or "AIR to NIR off in" anywhere, the four
counters 0, WindowServer 62 of 64 pipelines on the road and no GPU error, no crash or hang report. Of 204 nir1 entries
since the boot 57 were refusals: 55 the descriptor retry's first request, a dual-source colour output (taken now) and a
kernel reading a function constant its request does not give.

**Post-tessellation vertex functions take the road** (ABI version 8, `NVMTL_AIR_NIR_TESS`). The plugin's tessellation
path works on the translator's TessellationEvaluation module: it sets the module's spacing and vertex order, moves its
`PrimitiveId` to a per-patch input at the Location after every other (`nvmtl_tess_patch_tese`), and builds a
tessellation control stage that writes every input the module declares, pulled from the vertex buffers by the vertex
descriptor, with the patch id and the factors (`nvmtl_tess_make_tcs`), and a pass-through vertex stage. The frontend
now makes that same evaluation stage (`MESA_SHADER_TESS_EVAL`): each control-point field an input array of n at its
Location (the accessor `X.MTL_CONTROL_POINT_FN(index, ...)` reads it, as `lower_patch_control_point_calls`), each
`patch_input` and a TES's `[[instance_id]]` a per-patch input at theirs (instance_id after the highest the metadata
names, `tessellation_system_input_location`), `[[position_in_patch]]` the tessellation coordinate, `[[patch_id]]` the
per-patch input after every other, the spacing and vertex order the request gives (`tess_spacing`, `tess_order`: the
plugin's own mapping of Metal's partition mode and winding, `NVMTL_TESS_ORDER_FLIP` included), the reflection's
`TessellationInterface` and stage `TessellationEvaluation` equal to the translator's. The result lists the inputs
(`tess_inputs`, `tess_patch_id_location`), the plugin builds its control and vertex stages from them as from the module
(`nvmtl_air_nir_tese`), and the pipeline holds the two SPIR-V modules beside the NIR evaluation and fragment stages: Mesa's
`vk_pipeline` takes a NIR or a SPIR-V stage each on its own, and merges the tessellation modes of the stages. The
pipeline's tessellation description (factor format, control points, amplification, the instance Location) is made once
for both roads (`nvmtl_tess_desc_make`), from the reflection on the road; `patchType` and `patchControlPointCount` read the
reflection of a function made without the translator. Refused, as the SPIR-V road refuses the pipeline: an input other
than a 32-bit scalar or vector (the control stage writes no other), an isoline patch. All 223 post-tessellation
functions of the AIR cache (SceneKit's) now translate with reflections equal to the translator's.

**Raster order groups, as the translator binds them.** The translator reads `air.raster_order_group` only of a fragment
imageblock's members (pixel interlock there); a `[[color(n)]]` input, a texture or a buffer in a group it binds as one
without it, with no interlock (WindowServer's `inplace_copy_lpf`/`_lph`, QuartzCore's, carry one on a written texture).
The frontend now does the same; a group on anything else stays refused. Like the SPIR-V road, the road does not order
those accesses as Metal does.

**Imageblocks stay refused, by kind.** An implicit imageblock is a tile function's view of the colour attachments, which
the plugin runs only through the translator's rewrite of its AIR (`NVMTL_TILE_SHADING`, `tile_imageblock.rs`), a module
that stays on the SPIR-V road; an explicit one (`imageblock_data`, `write_imageblock_slice_to_texture_2d`: MPS's Lanczos
and DoG kernels, CoreAnimation's variable blur) the translator stages in a workgroup array whose width, capacity and cell
scale its IR analysis infers (`native/ir`: the cross-coordinate cells, the APV extent, `threads_per_threadgroup`, the lane
scales) and copies through its slice writer, which are not ported.

**Dual-source blending** (`[[color(0), index(1)]]`): the frontend's Location 0 output of index 1 (`data.index`, which
`nir_lower_io` hands NAK as the dual-source blend index), as the translator decorates it `Index 1`; the result says so
(`NVMTL_AIR_NIR_FS_DUAL_SOURCE`). The pipeline checked a dual-source blend against the fragment's SPIR-V, which made the
translator run for a fragment made without it; the road asks the result, and the SPIR-V road checks the module it makes.

**Minimum-LOD clamps, fixed on both roads.** Metal's `min_lod_clamp(c)` keeps a sample at level c or above; the
translator applied it only to gradient samples, so a sample with `bias(b)` and `min_lod_clamp(c)` read levels below c
(D3DMetal's converted shaders pass one with every sample: the 30 `shader_main_*` fragments the cache refused for it).
With the device's `shaderResourceMinLod` (now enabled at device creation; the translator's capability bit 4 and its SPIR-V
cache tag `-ml1`), an implicit level takes the clamp as `MinLod` and an explicit one is raised to it (GLSL `FMax`), colour
and depth samples alike (`apply_sample_min_lod_clamp`; the owned-module contract takes `ImplicitLod` with `MinLod`). The
frontend applies it the same way (`NVMTL_AIR_NIR_MIN_LOD`, `nir_tex_src_min_lod`; a gradient sample's always, as the
translator's); a clamp on a nearest fetch, a gather, a comparison, a pixel-coordinate or a bicubic sample stays refused
(the translator's lowering of those has no place for it). Without the device's clamp a clamp on a sample without
gradients is not applied, on both roads, as before. Found on the way: an integer texture sampled through an argument
sampler in a fragment function was sampled at its implicit level on the road, where the translator samples level 0
(`force_lod0`); now level 0, its bias dropped.

**Bicubic samples, as the translator emulates them** (`lower_pixel_bicubic_sample`): the sixteen texels (four in 1D)
around `coord - 0.5`, each weighted by Catmull-Rom's weights of the fraction (the same operations in the same order),
addressed by the sampler's modes, zero outside where it clamps to zero, at the level given; pixel coordinates and
normalized ones (scaled by the size at that level), 1D and 2D float textures.

**Buffers the translator keeps bounds-checked: stores, 64-bit values, argument buffers.** Which of the translator's
access forms a module has is now read off the module by the plugin (`nvmtl_air_nir_kept_forms`, request
`descriptor_elem_bits` and `descriptor_store_guarded`): the scalar width of each kept buffer's runtime-array element, and
whether a store to it sits under a 64-bit compare (the raw store, `emit_raw_store`: a byte offset at most
`UINT32_MAX - (size - 1)`). The frontend reads a 64-bit value from an array of 64-bit elements as one value (NVK checks
it whole: 0 when it straddles the end) and from a word array as two words, low first, each checked; it stores components
of the element's width (64 bits to a word array as two words), at the 32-bit offset the typed access computes, when the
module does not guard them; anything else to a kept buffer stays refused. An argument buffer the translator keeps a
descriptor (an index read from memory selects within it) is read the same way: its handles' low words, its pointers' two
words, its fields where they are (`r_argbuf_data`, on the SPIR-V road only until now). A kept buffer bound at an offset
below NVK's 16-byte storage-buffer alignment cannot be a descriptor: both roads bind the plugin's dummy there and read
it as it is (`r_oob_misaligned`); Metal would read the buffer from that offset, and a store through such a binding writes the
shared dummy, on both roads. **Still refused**: an array of `texture_buffer`s indexed at run time. The translator keeps
it one array binding, which the plugin's layout holds with one descriptor, and the SPIR-V road reads its elements wrongly
(`tex_tbarray`: zeros); none of the cache's functions does it.

**MPS's null textures stay refused.** The 55 kernels (`MPSTextureFunctions`, `MPSHistogram`) link visible functions MPS
supplies at pipeline creation (`prefixPrimary_*`, `postfixPrimary_*`: none of them in the AIR cache, so their linked form
cannot be checked offline) and call `air.get_null_texture_*`, which the translator lowers to a placeholder image it
declares at the first texture binding its own module leaves free, tracking the value (`lower_get_null_texture`,
`default_null_image_of`, `null_image_values`); the frontend reproduces neither and refuses, naming it. A linked kernel's
text reaches the frontend as any other.

### Tested (round 10)

| test | what | tolerance |
|---|---|---|
| r_tess_quad | quad patches, integer spacing, factor 4: s^2, t^3 and (s - 1/2)^2 piecewise linear at i/4, a tint per patch, the patch id | 2e-5 |
| r_tess_pow2 | pow2 spacing at factor 3: 4 segments | 2e-5 |
| r_tess_fodd, r_tess_feven | fractional odd (2.5) and even (3.5) spacing: symmetric segments, not the uniform subdivision | 2e-5 |
| r_tess_ccw | counter-clockwise output: the other face | 2e-5 |
| r_tess_tri | triangle patches, factors from a kernel per patch (one 0: not drawn), two instances, a buffer at the instance | 1e-4 |
| r_rog | raster order groups on `[[color(0)]]`, a written texture and a device buffer, two instances | 1e-6 |
| r_dual | `[[color(0), index(1)]]` blending attachment 0 with Source1Color and Source1Alpha | 1e-6 |
| r_minlod | min_lod_clamp with bias (constant and from a buffer), with gradients, and of 0, on a 7-level texture | 1e-6 |
| tex_bicubic | bicubic samples, normalized and pixel, 1D and 2D, every address mode, a level, offsets | 5e-5 |
| r_kept | a kept ulong palette past and straddling its end, a float and a ulong buffer written past their ends | exact |
| r_oob_misaligned | a kept palette bound at offset 4: the dummy as it is, the same on both roads | exact |
| r_argbuf_data | (round 9's) an argument buffer the translator keeps a descriptor, now on both roads | 4e-5 |
| tex_tbarray | `texture_buffer` arrays indexed at run time: refused | - |

A test marked `road` (render) or `MUST_ROAD` (compute) must take the road when it is on; with round 9's NVK and plugin
each of these takes the SPIR-V road and fails there (`r10/oldbuild.sh`), and with 3.0.26's translator `r_minlod` reads
level 0 on the SPIR-V road. The tessellation tests' attachments are equal byte for byte on both roads, and with
`NVMTL_TESS_ORDER_FLIP=1` (the negative control) `r_tess_quad` and `r_tess_ccw` fail on both.

- `sweep.py --reflect` over the AIR cache (round 5's list, `r10/sweeps.sh`): of 10,814 functions the frontend translates
  5,893 (round 9: 5,636; 3,565 kernels, 941 vertex and 1,387 fragment functions), every reflection equal to the
  translator's (different 0, not reflected 0); new: the 223 post-tessellation functions, 30 fragments with a
  minimum-LOD clamp, 4 with a raster order group; none that translated before is refused now. It refuses 4,919 (5,176);
  the two MPS modules whose translator reflection does not finish stay crashes. 437 asked again with descriptor
  buffers: 409 then translate, 28 are refused. Refused, the first ten: a function constant read without its value
  4,026, an argument index from one 572, a stage input under a constant without its value 72, a texture argument under
  one 59, a `[[color(n)]]` input under one 27, implicit imageblocks 26, an output under a constant 24, a kernel's
  `stage_in` 17, `amplification_id` 14, an instance acceleration structure 13 (explicit imageblocks 7).
- WindowServer's functions on disk (offline in a normal process): 179 of 211 translate with reflections equal to the
  translator's (round 9: 177: QuartzCore's `inplace_copy_lpf`/`_lph`); refused: implicit imageblocks 13, function
  constants without values 9, stage inputs under constants without values 5, explicit imageblocks 3, outputs under one 2.
  With each module's constants all 0 and all 1 (26 variants): 12 equal, 14 not reflected (initializer-computed gates).
- On the card (`tools/air-nir/plugin-run.sh` with the development plugin, this tree's NVK and the rebuilt translator,
  `MESA_SHADER_CACHE_DISABLE=true`, `r10/final-runs.sh`; the four fault counters 0 before and after every run, none
  new; WindowServer logged no GPU error): with `NVMTL_AIR_NIR=1` 119 pass twice and once with the first run's caches
  (110 on the road; `fc_refuse`, `fc_kernel_c`, `fc_kernel_none`, `fc_texture_none`, `tex_tbarray`, `r_primid` and
  `r_fcin_missing` take the SPIR-V road as they must; `ib_refuse` and `tex_pixborder` are refused by both),
  `grid2d_base` and `oob_kernel` skipped. With `NVMTL_AIR_NIR=0` 118 pass twice, `r_handlechoice` skipped as well. With
  `PLUGIN_RUN_CORRUPT=1` every test that dispatches or draws fails on both roads (the seven that pass are refusals).
  Compute with `NVMTL_COMPUTE_ROBUST=1`: 68 pass on each road. The fetch tests and `r_rog` with `NVMTL_NO_FBFETCH=1`
  pass on both roads and with `NVMTL_FETCH_COPY=1`; the pixel-coordinate tests and `tex_bicubic` with
  `NVMTL_NO_PIXEL_HW_FILTER=1` pass on both roads; the three argument-buffer tests 50 times from their setup pass 150 of
  150 on each road; the new render tests' attachments are equal byte for byte on both roads (16 of 16); the `ws_marker`
  trio passes. Found by a whole run: the plugin's 256-byte dummy buffer, bound for a kept buffer at a misaligned offset
  and behind a texel-buffer dummy, holds what an earlier draw wrote through a dummy binding (0xff where `r_oob_misaligned`
  read), on both roads; the test now reads it as it is.
- The translator's tests (`r10/ttests.sh`, `--no-fail-fast`): 2,595 pass and 26 fail, exactly the 26 that 57b79dda's tree
  fails (round 9's list: fixtures the public tree does not ship, the heap's set 2, two image shapes, the wrapper's
  literal `$HOME` path); the three new ones (`min_lod_clamp_tests`) pass.
- Host: `tools/test_air_nir_plugin.py` (a new check of the tessellation road and its negative control),
  `test_embedded_fast.py`, `test_a8_framebuffer_fetch.py`, `test_graphics_bindings.py`, `test_address_snapshot.py`,
  `test_switch_registry.py` (docs/SWITCHES.md regenerated: NVMTLAirNir.m reads `NVMTL_TESS_ORDER_FLIP`),
  `test_refusal_reasons.py`, `test_cgl_gld_guard.py`, `test_host_buffer_release.py`, `test_import_cache_limit.py`,
  `test_mem_unmap_no_double_free.py`, `test_nocopy_once.py`, `test_nvk_local_map.py`, `test_readonly_copyback.py`,
  `test_static_sampler_device.py`, `test_texrc.py`, `test_submission_copies.py`, `test_slot_handle_guard.py`,
  `test_surfin_zero_plugin.py`, `test_zink_subdata_stage.py`, `test_opengl_perf.py` pass; `test_fetch_copy.py` skips (no
  glslangValidator).
- Left: imageblocks (the translator's tile rewrite and cell staging), null textures, run-time `texture_buffer` arrays
  (and the SPIR-V road's wrong reads of them), a store to a kept buffer in the translator's raw form (its 4 GiB guard) and
  64-bit values in kept buffers of mixed element widths; Metal's ordering of raster order groups and its reads of a kept
  buffer bound at a misaligned offset, on both roads.

## Phases

1. The frontend and its Linux test; then its test on the card (round 1), function constants and textures (round 2).
2. The plugin calls it (round 3, done: "In the plugin" above), for every compute kernel first, with `NVMTL_AIR_NIR=0` to
   turn it off. What round 2 listed for it, and where each landed:
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
   Left open by round 3: the request carries no argument-sampler state (pixel-coordinate samplers stay on SPIR-V); the
   align guard's build is checked by `airrun` (`AIRRUN_ALIGN4=1`: all 47 pass, no access promising more than 4 bytes)
   but has not run through the plugin with a misaligned buffer (if the guard missed one, that dispatch would fault);
   the constexpr samplers' state words are not compared with the translator's; WindowServer stays off; the measured
   kernels are the test kernels, not applications'.
3. Vertex and fragment functions (round 4, done: "Render pipelines" above). The stages' inputs, outputs and builtins
   are the frontend's, the link is the SPIR-V road's remap handed over as Locations. Left on the SPIR-V road: the
   pipelines that need one of its rewrites of render stages (framebuffer fetch copies, stencil-export emulation, A8
   targets, multiview, tessellation, the GL uniform buffer, programmable sample positions), a pipeline without a
   fragment function, and the builtins the frontend refuses (`[[primitive_id]]`, `[[barycentric_coord]]`, the
   amplification ones); those rewrites would have to be done on NIR or by the frontend for the road to take them.
4. Make the road the only translation for what it takes (round 5, done: "Functions made without the translator"
   above): the frontend gives the reflection, `newFunctionWithName:` leaves the translator out of every function the
   frontend translates and reflects, and a pixel-coordinate argument sampler's state reaches the frontend. What round 4
   listed, and where it landed:
   - the SPIR-V twin on demand: translate a function only when a pipeline or variant of it takes the SPIR-V road (a
     refusal, a rewrite, a threadgroup size the road does not run, a pixel-coordinate argument sampler, the variants
     made from SPIR-V), which needs `newFunctionWithName:` to keep the AIR and the constants and translate later;
   - what is read from the translation today without a pipeline: the reflection (arguments, varyings, constexpr
     samplers, the kernel's default threadgroup size, `fragment_sample_positions`), which the translator can give
     without the SPIR-V (`ShaderReflection::from_*` or a reflection-only call), or the frontend's result can give;
     the hazard model's and the texel-buffer masks' reads of SPIR-V; the GL uniform index and the multiview and
     tessellation checks, which read the SPIR-V;
   - the pixel-coordinate argument sampler in the request (`with_runtime_sampler` on the translator's side), so that
     variant can stay on the road;
   - WindowServer, after the road has run on applications' kernels and pipelines (the measured ones are the tests');
   - a measurement on applications' pipelines (the sweep covers compute kernels only) before the translator is left out
     by default.
   Done in round 5: the SPIR-V on demand (`nvmtl_function_spirv`); the reflection, default size and varyings from the
   frontend; the hazard model's and the GL check's reads from the reflection; the tessellation, fetch and stencil checks
   known from what the frontend refuses; the pixel-coordinate sampler in the request; the sweep over vertex and fragment
   functions too. Left: WindowServer (off; the next decision, after applications have run on this road), the refusals
   that still send real functions to the SPIR-V road (framebuffer fetch, argument buffers, stage inputs under a function
   constant, and the rest of the sweep's list), and applications' own pipelines measured on the card.
