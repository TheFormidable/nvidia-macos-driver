# Fork status and evidence — 2026-11-23

This is the current snapshot of TheFormidable's `fable` branch with [PR #18](https://github.com/TheFormidable/nvidia-macos-driver/pull/18)
and [PR #20](https://github.com/TheFormidable/nvidia-macos-driver/pull/20) merged. The first numeric heading in [CHANGELOG.md](../CHANGELOG.md) is 3.0.15: the development package version, not
a published or qualified release. 3.0.1 to 3.0.15 are merged; there is no 3.0.10. 3.0.14 adds statistics of the shader
compiler, a development tool tested on Linux only. 3.0.15 compiles a heavy graphics shader again with fewer registers
when that runs more warps per SM, on by default with `NAK_OCC_TARGET=0` as the way back, host-tested only. The upstream baseline
is NullMoth 2.9. Keep inherited upstream evidence separate from this fork's experiments and from the NVMetrics
application.

Every path of the driver is on by default; a user turns nothing on. What exists for a risky change is one way back
(`name=0`), listed in the [README](../README.md#what-is-on-by-default-and-the-ways-back), in each release's "The ways
back" in the changelog and in [SWITCHES.md](SWITCHES.md).

## Evidence ledger

One machine has run this fork: AMD Ryzen 5 5500, RTX 5050 ASUS (10de:2d83, 1043:8a3f, GB207-A), macOS 26.7.3 x86_64,
one 1920x1080 display at 100 Hz, Xcode 26.5, RM/GSP 620.70.06. "Read by its owner" means a figure from the game's own
frame counter, reported in words: no capture was taken and no scene was matched.

| Revision / scope | What was observed | What is still unknown |
|---|---|---|
| Upstream 2.9 | RTX 5060 rendering and image-processing tests on macOS 15.8.1; RTX 5050 sleep/wake on 15.8.1 | Other boards, OS builds and every workload |
| 3.0 at aa27c73 | Native build, package, boot twice on RTX 5050; capture performance measured on a 100 Hz 1080p display (WindowServer fell from 100 to 20 frames a second during a screen share). With the fast grants of video memory installed, Minecraft on Vulkan read by its owner at 120 frames a second while sharing, where it had been 40 | OpenGL during a share; the counters after a sleep |
| 3.0.1 at b80056ef / 44091839 | Card checks on RTX 5050; first-boot WindowServer watchdog incident after installing 44091839, second WindowServer started normally | Superseded by the installs below |
| 3.0.2 | Installed on the RTX 5050. The two captures the performance audit started from were taken on it: a modded Minecraft pack through Apple's OpenGL renderer at 19 and at 74 frames a second | A run of cold shader variants can still add up at a first start (CHANGELOG, 3.0.2) |
| Card-test round 11, pre-3.0.3 | 39 inactive Zink rows; 38 renderer-word refusals, one glbench row inactive | Resolved by 3.0.3's guard, seen on the card with 3.0.6 and 3.0.7 below |
| GPU Caps Phase 1, 2026-11-17 | Native NVRM, 9 verified / 156 unknown; 1,000 read-only calls reported; generation advanced after resume | General compatibility and remaining unknown records |
| GPU Caps Phase 2B, 2026-11-17 | Native build, package CHECK and desktop; 15 verified / 150 unknown, generation 1 → 2, manual authorization false | Other OS/card combinations |
| 3.0.3 to 3.0.5 | Never installed on their own: compiled and host-tested on Linux, then reached the card inside 3.0.6 | The single fixes of 3.0.4 and 3.0.5 (Firefox's sandbox, League of Legends and the iOS Simulator in the translator, protected video, WindowServer at 4K 240 Hz, a head that stays dark after a wake) have not been exercised on the card |
| 3.0.6 at 52a77475 | Built on that machine, installed 2026-11-20 and started: the first driver after 3.0.2 to run on a card. OpenGL goes through Zink on macOS 26.7.3. `debug.nvrm_pat_wc` reads 0 with the machine's own boot loader (AMD_Vanilla's Fix PAT). The modded Minecraft pack: 55 to 64 frames a second in three captures (scenes not matched with 3.0.2's) | No gain of a single change of 3.0.6 measured; nothing captured with `nvzink=0` |
| Page attribute table from a test boot loader, 3.0.6 | Started from a USB stick whose OpenCore sets entry 6 to write-combining: booted normally, `debug.nvrm_pat_wc` reads 1; the game's main thread spends 1.4 % in buffer uploads (11-14 % before) and 1.2 % importing the window's surface (7-8 % before) | What the table adds in frames a second: the captures it was set against were not taken like for like |
| 3.0.7 at 9682b3a2 | Built from an empty folder (19 components, 512 s, 0 warnings), installed over 3.0.6, started normally; the boot guard counted the start as good. Card checks: 133 rows ran, 130 passed, 3 failed, 1 skipped; the OpenGL rows ran through Zink, Xid 0, WindowServer the same process. A capture of the pack from the test boot loader: 90 frames a second where the comparable capture on 3.0.6 gave 61 (scenes not identical) | No card row runs a swap one frame ahead; the three failed rows are described in the changelog and their fixes are not in the tree |
| 3.0.8 at 9969d3e8 | Built on that machine (only the Zink frontend changed) and put in place there without a restart, by copying the changed libraries | No capture: the most it can give back is the 5.7 % of the render thread the opacity queries took |
| 3.0.9 at cb4df8b1 | Built on that machine (the Vulkan frontend, no warning), `test_nvk_bridge.py` and `test_nvk_bridge_newest.py` pass there; installed 2026-11-22, restarted without a fault. Minecraft 26.3 on its Vulkan renderer, vertical sync off: up to 1000 frames a second read by its owner, where a capture on 3.0.8 had 200 | No capture on 3.0.9; no card row presents through the new thread |
| 3.0.11 at 577f5f09 | Built on that machine (the Vulkan frontend and Zink, no warning), its three host tests pass there; installed 2026-11-22, restarted without a fault. Minecraft without mods on OpenGL through Zink: 490 frames a second read by its owner | No figure from before on Zink, no capture; a MAILBOX swapchain in an application; a resize, a minimize or a second display under the sender thread |
| 3.0.12 at f561d334 | Built on that machine (NVK, no warning: the two new patches under macOS's `patch`, Apple's clang and the Mac's Rust), `test_nvk_nak_switch_key.py` and `test_nvk_chain.py` pass there; installed 2026-11-22 with the installer and a restart. `driver-version` reads 3.0.12, the installed NVK equals the package's, `IOGLBundleName` is `NVKZinkGLDriver`. The modded Minecraft pack on it, read by its owner: 123 to 290 frames a second, and 75 to 190 with a shader pack on | 3.0.12 changes nothing while no compiler switch is set; the uniform-handle experiment has not run on a card |
| 3.0.13 at a79482f3 | Linux only: `test_nvrm_pat.c` and `test_nvrm_pat.py` pass, the switch registry check passes, the new kernel code compiled for macOS in a stub unit with its instructions in the intended order | Not built on a Mac, not installed: the first start and the first wake with NVRM writing the table itself, and the installer's DLSS step run by default |
| Onboard sound on macOS 26 (PR #17) | On the RTX 5050 machine with `alcid=66` in boot-args only the NVIDIA display outputs appeared; removing it brought the onboard outputs back | Other boards and codecs |
| 3.0.14, [PR #18](https://github.com/TheFormidable/nvidia-macos-driver/pull/18) | Linux only (a development tool: `NAK_STATS=1`, a shader corpus and a comparison script): the fork's NVK built on Mesa's nouveau drm-shim as shader model 120, all 300 corpus stages compile, `test_nak_stats.py` passes |
| 3.0.15, [PR #20](https://github.com/TheFormidable/nvidia-macos-driver/pull/20) | Built on the RTX 5050 machine from fdbc830d without a warning (before the WindowServer rule), not yet run on the card. On Linux: the corpus does not change; of ATM10's 409 stages, 27 fragment stages gain warps (28 % more over them) for 2.6 % more instructions, with 4510 new spill and fill instructions to local memory; `NAK_OCC_TARGET=0` gives 3.0.14's output; `test_nak_occ_target.py` passes | Not cross-built for macOS; nothing of it on a Mac or a card |

Build UUIDs and the exact meaning of the GPU Caps records are in [GPU-CAPS.md](GPU-CAPS.md) and
[Phase 1 evidence](GPU-CAPS-PHASE1-VALIDACION.md).

## Implemented vs proven

- **Rendering:** Metal plugin on NVK/NAK and AIR-to-SPIR-V translation. Surface fence, sleeping event waits, channel
  recovery, captured-frame VRAM work, asynchronous texture uploads, shaders translated ahead in games and the changes
  of the 3.0.6 audit are present and on. Do not infer per-feature hardware success from a desktop boot.
- **OpenGL:** Zink on NVK is the default on macOS 15.5 or later and is what the RTX 5050 machine runs on macOS 26.7.3
  since 3.0.6. A swap runs one frame ahead (3.0.7), keeps the opacity answer (3.0.8) and with the swap interval at 0
  shows the newest frame without waiting for the display (3.0.11). A refused context can stay on Apple's renderer; a
  dropped call can return NULL. Use `nvzink=0` for rollback and verify GL_RENDERER in each process on another macOS
  build.
- **Vulkan:** the application runtime is installed with the driver and independent of the Metal path; an application
  selects it when it is launched. IMMEDIATE and MAILBOX swapchains show the newest frame (3.0.9, 3.0.11). Seen with
  Minecraft 26.3 only.
- **Write-combining:** from 3.0.13 NVRM restores entry 6 of the page attribute table where an AMD kernel patch
  changed it (`nvpatwc=0` leaves it). The table itself is proven on the RTX 5050 machine from a test boot loader; the
  driver writing it is not.
- **DLSS:** the installer fetches NVIDIA's files by default from 3.0.13 (`NULLMOTH_DLSS=0` skips); the plugin serves
  MetalFX with DLSS Super Resolution and Frame Generation when they are in place. The architecture choice is measured
  on an RTX 5060 only.
- **Power:** per-burst boost and live policy selector 13; optional daemon and notebook power controls have their own
  qualification requirements. No arbitrary overclock, voltage or power-limit API is added by GPU Caps.
- **Telemetry:** RUSD observations and versioned read-only GPU Caps selector 14. Six added verified records in Phase
  2B do not authorize fan SET operations.
- **Media:** H.264 Blackwell decode path is gated by a self-test; do not advertise Blackwell AV1/VP9 hardware decode
  from that gate. DRM streaming and end-to-end WebRTC/4K capture remain issues to qualify on specific OS/browser/codec
  paths.
- **Shader-compiler statistics (3.0.14):** `NAK_STATS=1` writes one JSON line per compiled shader stage;
  `tools/nak-corpus/` and `tools/nak_stats_diff.py` measure a compiler change over a fixed set of 300 stages.
  Host-tested only: built and run on Linux on Mesa's nouveau drm-shim as shader model 120, never on a Mac or a card.
- **More warps per SM for heavy graphics shaders (3.0.15):** on by default, `NAK_OCC_TARGET=0` goes back, never
  in WindowServer. Host-tested only: the figures are the compiler's own on Linux; no frame rate or GPU utilisation has been measured on a card.
- **Other gaps:** full multi-GPU acceleration, hybrid laptop offload, hardware ray-tracing opt-ins and Windows/Apple
  feature parity require dedicated evidence. CUDA and proprietary firmware capabilities are not implemented by this
  fork. No card other than the RTX 5050 has run a 3.0.x build of this fork.

## Validation priorities

1. Build 3.0.13 with `./build.sh doctor` / `./build.sh`, stage it against the pinned 2.9 package and install it with
   a known-good driver and an OpenCore recovery route kept. After the start, and again after a sleep, read
   `sysctl debug.nvrm_pat_found debug.nvrm_pat_wc debug.nvrm_pat_why debug.nvrm_pat_passes debug.nvrm_pat_cpus hw.ncpu`.
2. Capture the presentation changes like for like: the same scene with and without `NVK_ZINK_PRESENT_NEWEST=0`
   (OpenGL) and `NVK_BRIDGE_PRESENT_NEWEST=0` (Vulkan), and 3.0.8's opacity change with `NVK_ZINK_OPACITY_KEEP=0`.
3. Add card rows that swap one frame ahead and through the newest-frame paths; today's rows set the present observer,
   which makes every swap present by itself.
4. Record exact commit, package manifest, OS build, GPU PCI/subsystem IDs and loaded kext revisions; run the
   [hardware checklist](TESTING-ON-HARDWARE.md) on any other card or macOS build.
5. Verify read-only GPU Caps before and after resume. Track NVMetrics runtime trials independently; see
   [NVMETRICS.md](NVMETRICS.md). Keep manual control under its qualified, supervised path with verified AUTO.

Historical reports in the changelog and component documents retain their own dates, commits and original test scope.
For all guides see [the index](README.md).
