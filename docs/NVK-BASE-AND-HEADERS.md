# NVK base revision and NVRM header provenance

## Mesa base

`nvk/MESA_BASE` is Mesa commit 90a959eb, the mesa-26.3.0-rc3 tag (2.9). 2.8 and 2.8.1 were built on 17ca6174 (2026-10-05, 26.3.0-devel); 32 nouveau commits separate the two and the whole patch chain applies to both without fuzz. The NAK features that matter for compute speed
are in that base, not behind a future update: the uniform base address on global loads (`uniform_addr`, `set_ureg_addr`,
`nak_nir_opt_uniform_address`, `nir_opt_offsets` for SM73 and newer), LDSM, and the cooperative-matrix lowering.
A rebase onto a newer Mesa therefore buys only the upstream commits made since. There are 62 of them touching
`src/nouveau` (about 930 added and 650 removed lines), and the whole 2.9 patch chain applies to the newer tree without a
reject. Moving the base would still re-base about 17,600 lines of patches that WindowServer composites through, with no
card available to validate, so the base stays. The upstream fixes that matter for current cards are carried instead as
three patches of the default chain (`build/build_nvk.sh`), right after the 2.9 patches and before this driver's own
`nvk-evwait`, `nvk-fence`, `nvk-channel-recover` and `nvk-stream-uncached`. Each holds upstream commits with their
text unchanged; the header of each patch and `NOTICE` list them.

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

`NVK_SKIP_PATCHES="<name> ..."` for `build/build_nvk.sh` leaves any of the three out of a build; no other name is
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
runner (macos-15-intel: 4 processors, macOS 15, Xcode 26.3 with the 26.2 SDK) built it at that commit too. Not
checked anywhere: the image on a card. Mesa's CI has no RTX 50, and the scheduling patch has not been in a Mesa
release. The library's build id changes, so shader caches written by a 2.9 build are not reused and shaders are
compiled again once.

## NVRM headers

NVK is compiled against the 610.57.04 open kernel module headers with the NVOS change in `nvk/ogkm-nvos-620.patch`; the
kernel module is built from 620.70.06. `tools/nvk_header_abi.py` compares, between module versions, every struct and
constant in the 135 headers the NVRM backend preprocesses to, restricted to the 198 names the backend sources use. Those counts are of the 2.9 backend; the channel rebuild of this
release added eight names (`NV2080_CTRL_CMD_GPU_GET_RECOVERY_ACTION` and its parameters, `NV906F_CTRL_CMD_RESET_CHANNEL`
with its parameters and `_REASON_VERIF`, and the status codes `NV_ERR_GPU_IN_FULLCHIP_RESET`, `NV_ERR_RESET_REQUIRED`,
`NV_ERR_GPU_IS_LOST`), whose definitions are the same in 610.57.04 and 620.70.06 when compared by hand; the tool has not
been run again over the new backend.

| compared with 620.70.06 | structs differing | structs missing | defines differing | defines missing |
|---|---|---|---|---|
| 610.57.04 + NVOS change (shipped) | 0 | 0 | 0 | 0 |
| 615.78.08 (the headers 2.8 was built with) | 0 | 0 | 0 | 0 |

Control: the same tool without the restriction (`--all`) reports 13 structs differing, 7 missing, 20 constants differing and
126 missing between the shipped headers and 620.70.06, so the comparison does see changes when they exist; none of them
is a name the backend uses. The header list is produced by preprocessing the backend sources with `-MM` and keeping the
files that resolve into the module tree.
