# Metal AIR to NIR, without SPIR-V

The plugin runs a Metal function on NVK by translating its AIR (the LLVM bitcode inside a metallib) to SPIR-V with
the translator (`translator/`), which NVK then turns into NIR and NAK into the GPU's code. `nvk/air` is a second
road that skips SPIR-V: NVK reads the AIR itself and builds the NIR directly, for compute kernels and, since round 4,
vertex and fragment functions.

Phase 1 is the frontend and its tests (on Linux, then on the card). Since phase 2 (round 3, 2026-11-27) the plugin
asks it first for every compute pipeline, and since phase 3 (round 4, the same day) for every render pipeline, and
keeps the SPIR-V road as the fallback (below, "In the plugin" and "Render pipelines"). NVMTL_AIR_NIR=0 turns it off;
it is off in WindowServer for now.

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

Vertex and fragment functions (round 4): `[[stage_in]]` attributes of one Location (float, half, and 8-, 16- and
32-bit integer scalars and vectors), `[[vertex_id]]`, `[[instance_id]]`, `[[base_vertex]]`, `[[base_instance]]`;
outputs `[[position]]`, `[[point_size]]`, `[[clip_distance]]`, `[[render_target_array_index]]`,
`[[viewport_array_index]]` and user varyings of one Location with their interpolation (`center` / `centroid` /
`sample`, `perspective` / `no_perspective` / `flat`; integers flat); fragment inputs `[[position]]`,
`[[front_facing]]`, `[[point_coord]]`, `[[sample_id]]`, `[[sample_mask]]`, `[[render_target_array_index]]`; outputs
`[[color(n)]]` (n < 8) of float, half, signed and unsigned types, `[[depth(any|greater|less)]]` (with that depth
layout) and `[[sample_mask]]`; `discard_fragment` (a demote, as the translator's), `[[early_fragment_tests]]`, sampling
at the level the derivatives give (with a bias), `dfdx`, `dfdy`, `fwidth`; buffers, textures, samplers and function
constants as in a kernel, the SIMD-group builtins.

Where NAK has no instruction for a NIR operation and does not ask NIR to lower it (`[u|i][r]hadd`, `fisnormal`),
the frontend writes the expansion itself: a NIR opcode that reaches NAK unsupported is a compiler panic and a failed
pipeline, not a refusal.

Everything else is refused with a message, never guessed (`result.message`, status `NVMTL_AIR_NIR_UNSUPPORTED`):
argument buffers, imageblocks, ray tracing, function constants read without their values, function pointers and
recursive calls, doubles, run-time-sized `alloca`s, a `select` or phi between two kinds of memory, mesh and object
functions; of vertex and fragment functions: post-tessellation vertex functions (and any metadata after a function's
arguments other than `early_fragment_tests`), a stage input or output of more than one Location (a matrix, an array, a
nested structure), a stage input that exists only under a function constant, `[[color(n)]]` inputs (framebuffer
fetch), `[[stencil]]` outputs (stencil export), dual-source outputs (`[[color(0), index(1)]]`), raster order groups,
`[[primitive_id]]`, `[[barycentric_coord]]`, the amplification builtins and any other builtin not listed above,
threadgroup arguments, a varying the request gives no Location; and of textures: multisampled ones, a written cube or depth texture and a texel read of a cube
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
4. Make the road the only translation for what it takes: today `newFunctionWithName:` still runs the translator for
   every function (the fallback, the reflection, the hazard model, the constexpr samplers' and argument buffers'
   binding and the varyings' Locations read its output), so the road adds the frontend's work to the translator's. What
   is left before that:
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
