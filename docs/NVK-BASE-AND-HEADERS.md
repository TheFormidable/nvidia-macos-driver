# NVK base revision and NVRM header provenance

## Mesa base

`nvk/MESA_BASE` is Mesa commit 90a959eb, the mesa-26.3.0-rc3 tag, the base since 2.9 and still the base of 3.0.20.
NVK therefore reports Vulkan 1.4.363, the version of the Vulkan headers in that tree (`VK_HEADER_VERSION` 363); the
Khronos loader the package offers to applications (v1.4.367, also kept until Mesa 27) does not change that number.

**Decision (2026-11-26): the base stays on 26.3 until Mesa 27.** The owner chose to wait for the Mesa 27 release before
rebasing the chain. Until then, upstream fixes that matter for current
cards are carried as patches of upstream commits (below), and NVK's Vulkan version stays 1.4.363.

2.8 and 2.8.1 were built on 17ca6174 (2026-10-05, 26.3.0-devel); 32 nouveau commits separate the two and the whole 2.9 patch chain applies to both without fuzz. The NAK features that matter for compute speed
are in that base, not behind a future update: the uniform base address on global loads (`uniform_addr`, `set_ureg_addr`,
`nak_nir_opt_uniform_address`, `nir_opt_offsets` for SM73 and newer), LDSM, and the cooperative-matrix lowering.
A rebase onto a newer Mesa therefore buys only the upstream commits made since. When this was counted for 2.9
(2026-11-08) there were 62 of them touching `src/nouveau` (about 930 added and 650 removed lines), and the whole 2.9
patch chain applied to the newer tree without a reject. Moving the base still means re-basing a patch chain that
WindowServer composites through (about 17,600 lines at 2.9, more since), and validating the result again on a card. The
upstream fixes are carried instead as three patches of the default chain (`build/build_nvk.sh`), right after the 2.9
patches and before this driver's own `nvk-evwait`, `nvk-fence`, `nvk-channel-recover` and `nvk-stream-uncached`. Each
holds upstream commits with their text unchanged; the header of each patch and `NOTICE` list them.

- `nvk/nvk-mesa-26.3.0.patch`: what the 26.3.0 release (0a6fa60a) has over rc3 in the 13 files the NVK library is
  compiled from, 17 commits of the 26.3 branch. For RTX 50 it holds the uniform `Ldcg` scoreboard fix on SM120: a
  register could be read before the load into it had finished. The rest are NIR and util fixes that NVK runs
  (`nir_lower_goto_ifs`, the `load_ubo` range in `nir_lower_mem_access_bit_sizes`, `nir_opt_access` with shader
  interlock, `nir_lower_multiview`). The release's `VERSION` and `meson.build` changes are left out: the build needs
  the same meson as before, and the library still reports its Mesa version as 26.3.0-rc3.
- `nvk/nvk-nak-fixes-main.patch`: four NAK fixes that are on Mesa main and in no release: the barrier source type of
  `OpBSSy`, the insert order in `opt_bar_prop` when annotations are off, and longer SM120 IMMA/HMMA latencies (with
  the `Copy`/`Clone` derive they need).
- `nvk/nvk-nak-sched-main.patch`: four NAK code generation changes that are on Mesa main and in no release:
  `ready_cycle` in the scheduler that runs before register allocation (it used a value that was always zero, so
  latencies never influenced the order), `opt_fall_through` always applied, and `nir_opt_peephole_select` with limit
  8 in every stage. None of the four is specific to one card generation; on RTX 50 the scheduler now orders by the
  SM120 latency table.

2.9 listed two more commits in an opt-in file (`IDP.4A` never encoded with an immediate, input-attachment
`texture_offset`); both are already in rc3, which is why that file did not apply and is gone.

3.0.1 adds a fourth patch of upstream commits, `nvk/nvk-mesa-main-2.patch`, after `nvk-boost-policy` and before
`nvk-nvrm-fixes` and `nvk-local-map-vram`: eight commits of Mesa main for NVK and the Vulkan runtime, text unchanged, listed in its header
and in `NOTICE` (push descriptor sets uploaded only when they changed and only to the layout's size, the single
color-write mode used when every target has the same mask, stencil usage in image format queries, the swapchain
private-data mutex initialised outside Android, attachment locations of secondary command buffers). It touches no
compiler file and no file of the three patches above. It is not one of the upstream-fix patches that
`NVK_SKIP_PATCHES` can leave out: it is not a compiler change, and `nvk-nvrm-fixes` after it is made on top of it.
NAK changes on main since (the `imad64` and `fmnmx`/`fclamp` series, the zcull depth hints, as listed for 3.0.1) are
not taken: they change the code of shaders, and each would need its own check on a card.

The chain goes on with `nvk-gpu-caps` (3.0.3), which carries the byte-identical
NVRM GPU Caps ABI header. It does not rebase Mesa or implement GPU writes.
See [GPU-CAPS.md](GPU-CAPS.md); `tools/test_shared_headers.py` checks the two copies.
After it come four changes of our own, each described in its header: `nvk-desc-alloc` (3.0.6: a descriptor set layout
without immutable samplers is not walked for every new set), `nvk-surface-keep` (3.0.7: two exports for a caller
that keeps its import of a surface's video memory across frames; see [ZINK-OPENGL.md](ZINK-OPENGL.md)),
`nvk-nak-switch-key` (3.0.12: the environment switches the shader compiler reads, two of Mesa's SPIR-V reader among
them, are part of the shader cache key, as a digest that is 0 when none is set) and `nvk-tex-uhandle-safe` (3.0.12:
with `NAK_TEX_UHANDLE=1`, which is off by default, a texture whose handle left its uniform register no longer fails
the compile). The last one is a change to
the compiler, in `assign_regs.rs` and `sm70_encode.rs`; with the switch unset its code leaves every texture and every
block as it finds them (`tools/test_nak_tex_uhandle_safe.py`). Neither touches a file of the three upstream-fix
patches, so a build with `NVK_SKIP_PATCHES` applies both: that is why the key's code is in `nvk_physical_device.c`,
where the key is hashed, and not in `nvk_shader.c`, where `nvk_physical_device_compiler_flags` is and which
`nvk-nak-sched-main` changes. Two comments in `nvk_shader.c` are therefore out of date: "bits 15..47 are free" of that
function's value (the digest has 16..47), and `NVMTL_NVK_NO_LCONST16` "not in the shader cache key" (it is).

The chain ends with four more:
- `nvk-nak-stats` (3.0.14): `NAK_STATS=1` writes one JSON line of compiler statistics per shader stage on stderr, for
  `tools/nak-corpus/` and `tools/nak_stats_diff.py`; unset, nothing is read or written. It changes `nak/api.rs` only.
- `nvk-zcull` (3.0.17): the NVRM backend asks RM for the ZCULL unit's geometry (`NV2080_CTRL_CMD_GR_GET_ZCULL_INFO`
  and the ZCULL engine's context properties) and fills `nv_device_info.zcull_info` as Linux's GSP code does, so the
  hierarchical Z NVK 26.3 already has runs here. It changes `nvkmd_nvrm_pdev.c` only. Off in WindowServer, with
  `NVK_ZCULL=0` or `NVRM_610_ZCULL=0`, and when RM's figures are missing or out of range; `NVK_ZCULL_LOG=1` logs the
  decision.
- `nvk-nak-late-split` and `hwrt/ttu-rq-split` (3.0.18, from the original project's 3.1): a memory read wider than its
  alignment that reached the last compiler stage is split into reads the hardware accepts, and ray queries read a
  geometry entry as two 4-byte loads. `ttu-rq-split` is a patch of its own (in the original it is an edit of
  `hwrt/ttu-rq.patch`) so that 2.9's chain still rebuilds as shipped.

`nvk-legacy-floor` (from the original's older-release work, 3.0.18) is not in the release chain: a build of NVK for a
macOS floor below 12 applies it last, with `NVK_EXTRA_PATCHES=nvk-legacy-floor` and `NVK_FLOOR=<floor>`
(`build/build_nvk.sh`).

`NVK_SKIP_PATCHES="<name> ..."` for `build/build_nvk.sh` leaves any of the three upstream-fix patches out of a build; no other name is
accepted. It is there to find the cause of a rendering fault: build without `nvk-nak-sched-main` first (it is the only
one that is not a fix, and it changes the instruction order of every shader), then without all three.
`NVK_EXTRA_PATCHES=nvk-upstream-nak`, the 2.9 way to ask for these fixes, is accepted and ignored with a note. A
library built that way is for the fault hunt only. Its summary line, the one line the release build shows and keeps
in `BUILD.txt`, ends with `WITHOUT <names> (NVK_SKIP_PATCHES)` (and `PLUS <names> (NVK_EXTRA_PATCHES)` for added
patches), so a variable that is still set at the next build cannot go unnoticed. Unset both for a library that is
meant to be used.

Checked offline, with the whole library (C, C++ and Rust) cross-built on Linux for macOS with LLVM 18: the chain
applies from the pristine base with no fuzz; the library builds and links after each of the three patches and with
every combination of them left out, with no new warning; its exported and undefined symbols are the same as without
the three. Every changed and context line of the three patches was compared with the upstream commits. NAK's unit
tests that need no GPU pass, and a random test of the changed scheduler (1,900 generated functions, each under nine or
ten combinations of shader model and register limit, SM120 and older models, assertions on) found no case where the
pass fails, loses an instruction, or puts a use before its definition. A second random test ran the whole back end
(legalisation, the scheduler before register allocation, register allocation, jump threading, the scheduler after it,
the dependency calculation and the encoder) on 3,600 generated cases for SM120 and for older models, once with and
once without the scheduling patch, with the flags the library is built with: every case that encodes without the patch
encodes with it. On one Intel Mac (12 threads, macOS 26.7, Xcode 26.5) the library has been built with the whole chain
and LLVM 22, and had the same bytes in three builds (one with tools built by hand, then with `./build.sh` at commits
650db45 and 3933ff6, which do not differ in NVK's sources or patches), at 3933ff6 with no warning; GitHub's Intel
runner (macos-15-intel: 4 processors, macOS 15, Xcode 26.3 with the 26.2 SDK) built it at that commit too.
Every later patch of the release chain has since been built on the test machine's Mac inside a package that was
installed and run on its RTX 5050 (macOS 26.7.3): `nvk-stream-uncached` and the four of 3.0.1 from 3.0.1 on,
`nvk-desc-alloc` from 3.0.6, `nvk-surface-keep` from 3.0.7, `nvk-nak-switch-key` and `nvk-tex-uhandle-safe` with
3.0.12 (NVK under macOS's `patch`, Apple's clang and the Mac's Rust, no warning), `nvk-nak-stats` and `nvk-zcull` with
3.0.17, and the two of 3.0.18 with 3.0.19 and 3.0.20 (installed 2026-11-26). Those builds run the desktop, OpenGL
through Zink and the Vulkan frontend there. Not checked on the card: the image with `NAK_TEX_UHANDLE=1`, and whether
ZCULL turns on (`NVK_ZCULL_LOG=1` says) and what it gives. Before they reached the card, 3.0.12 to 3.0.17 were checked
in a Linux build of this NVK and NAK on Mesa's nouveau drm-shim as a GB207 (shader model 120), where pipelines compile
without a card and nothing is drawn, and `nvk-zcull`'s new code was compiled on its own against the RM headers. Mesa's
CI has no RTX 50, and the scheduling patch has not been in a Mesa release. Each new NVK build changes the library's
build id, so shader caches written by an earlier build are not reused and shaders are compiled again once.

## NVRM headers

NVK is compiled against the 610.57.04 open kernel module headers with the NVOS change in `nvk/ogkm-nvos-620.patch`; the
kernel module is built from 620.70.06. `tools/nvk_header_abi.py` compares, between module versions, every struct and
constant in the 135 headers the NVRM backend preprocesses to, restricted to the 198 names the backend sources use. Those counts are of the 2.9 backend; 3.0's channel rebuild
added eight names (`NV2080_CTRL_CMD_GPU_GET_RECOVERY_ACTION` and its parameters, `NV906F_CTRL_CMD_RESET_CHANNEL`
with its parameters and `_REASON_VERIF`, and the status codes `NV_ERR_GPU_IN_FULLCHIP_RESET`, `NV_ERR_RESET_REQUIRED`,
`NV_ERR_GPU_IS_LOST`), whose definitions are the same in 610.57.04 and 620.70.06 when compared by hand, and 3.0.17's
ZCULL query adds `NV2080_CTRL_CMD_GR_GET_ZCULL_INFO` and the ZCULL context properties. The tool has not been run again
over the backend since 2.9.

| compared with 620.70.06 | structs differing | structs missing | defines differing | defines missing |
|---|---|---|---|---|
| 610.57.04 + NVOS change (shipped) | 0 | 0 | 0 | 0 |
| 615.78.08 (the headers 2.8 was built with) | 0 | 0 | 0 | 0 |

Control: the same tool without the restriction (`--all`) reports 13 structs differing, 7 missing, 20 constants differing and
126 missing between the shipped headers and 620.70.06, so the comparison does see changes when they exist; none of them
is a name the backend uses. The header list is produced by preprocessing the backend sources with `-MM` and keeping the
files that resolve into the module tree.
