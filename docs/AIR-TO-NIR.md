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
  argument, and the path of an LLVM library.
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
- Memory: device and constant pointers are 64-bit addresses (`load_global`, `store_global`, `global_atomic`);
  threadgroup pointers are offsets into shared memory, the arguments first in index order and the kernel's static
  variables after them; `alloca`s LLVM could not remove are fixed slots of scratch memory; constant tables
  (address-space-2 globals with a value) go into the shader's constant data.

The design follows the one Stela showed works (an AIR-to-NIR compiler for NVK, GPL-2.0-or-later, read from its tree
of 2026-11-22). No code of Stela is used: this tree is under PolyForm Noncommercial and ships NVK, so GPL code
compiled into it could not be published.

## What phase 1 translates

Compute kernels with: buffer arguments in address spaces 1 and 2 (and OpenCL's `air.constant`), threadgroup
arguments, the thread and grid builtins (positions, indices, sizes, SIMD-group index and width; OpenCL's
`get_global_id` family); integer and float arithmetic, comparisons, `select`, casts, `air.convert.*`, vectors of 1
to 4, 8 and 16 components, odd integer widths SROA leaves (`i24`, `i48`: kept zero-extended in the next width);
branches, `switch`, loops and phis; loads and stores of every kind of memory, `memcpy`/`memset` of a fixed length;
threadgroup and SIMD-group barriers and fences; device and threadgroup atomics (add, sub, min, max, and, or, xor,
exchange, compare-exchange, load, store; float add on device memory); most of Metal's math library
(`air.[fast_]<fn>`) and the matching LLVM intrinsics; SIMD-group and quad functions (`simd_sum` and the other
reductions, prefix sums, shuffles, broadcast, ballot, all, any) on the warp of 32; structures and arrays held as
values; arguments that exist only under a function constant (bound as the kind they wrap).

Where NAK has no instruction for a NIR operation and does not ask NIR to lower it (`[u|i][r]hadd`, `fisnormal`),
the frontend writes the expansion itself: a NIR opcode that reaches NAK unsupported is a compiler panic and a failed
pipeline, not a refusal.

Everything else is refused with a message, never guessed (`result.message`, status `NVMTL_AIR_NIR_UNSUPPORTED`):
textures and samplers, argument buffers, imageblocks, ray tracing, function constants read by the kernel
(`air.is_function_constant_defined`, a constant without a value: the request has no field for their values yet),
function pointers and recursive calls, doubles, run-time-sized `alloca`s, a `select` or phi between two kinds of
memory, and vertex, fragment and mesh functions.

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

On 2026-11-27, RTX 5050, the installed plugin's `air-runtime/libLLVM.dylib` (LLVM 21.1.8), all 29 tests pass
and the counters stayed at 0: float math (fast and precise), integer operations, conversions with half, loops with
phis and early exits, switches, threadgroup reductions and arguments, device and threadgroup atomics with
compare-exchange, atomic float add, constant tables, structs with `float3` and `packed_float3`, a `float4x4` in a
constant buffer, vectors and swizzles, thread-private arrays, pointer selects and pointer loops, 64-bit integers, 2D
and 3D grids (direct, indirect, with a base), SIMD-group builtins and functions, char and short buffers, structures
held as values, and one kernel the frontend must refuse (it reads a function constant). The run found two bugs that
the Linux test could not: `any()` of a bool vector was translated as `all()`, and `hadd`/`rhadd` made NAK panic.

`tools/air-nir/sweep.py` runs `airsweep.c` over AIR files (the plugin's cache keeps text IR,
`~/Library/Caches/nvmtl/aircache/*.ll`): every compute kernel through the frontend and pipeline creation, nothing
dispatched, refusals counted by reason. Of the 8273 compute kernels in this Mac's cache on 2026-11-27 (most of them
Apple's own: MPS, Espresso), 699 give a pipeline, none fails in NAK and none crashes; the rest are refused: 6953 for
textures and samplers, 550 because they read function constants, and 71 for imageblocks, `stage_in`, argument
buffers, visible function tables, `air.is_uniform` and two pack functions. The sweep found that a kernel reading a
function constant compiled as if it were 0: Apple's compiler copies each constant (an externally initialized
`MTL_FC_INIT_` global) into an `addrspace(2)` global that is not constant, in the module's static initializer, and
the frontend took that global's initializer as its value. Such kernels are refused now.

## Phases

1. The frontend and its Linux test (this).
2. The plugin calls it for the kernels the translator refuses, on by default with `NVMTL_AIR_NIR=0` to turn it off,
   and a round on the card checks what they compute.
3. Textures and samplers, then function constants (their values in the request).
4. Vertex and fragment functions.
5. Prefer it to SPIR-V where it measures the same or better.
