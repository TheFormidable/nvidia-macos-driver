# Fork status and evidence — 2026-11-26

This is the evidence ledger of TheFormidable's fork, branch `fable`: for each version, where it was built and tested,
what ran on the card, and what has not been shown yet. The fork split from
[NullMoth](https://github.com/nullmoth/nvidia-macos-driver) at its 2.9 (7412cdd) and merged its 3.1 (900470d4) in
3.0.18.

- **3.0.20 is Release 2** of the fork, a pre-release, and the first heading of [CHANGELOG.md](../CHANGELOG.md). What it
  brings over release 1 is in [releases/RELEASE-2.md](releases/RELEASE-2.md).
- **Release 1 was 3.0.2**, published on 2026-11-17
  ([release 3.0.2](https://github.com/TheFormidable/nvidia-macos-driver/releases/tag/3.0.2)); the fork made no other
  release before 3.0.20. There is no 3.0.10, and 3.0.15 was tried and not kept.
- Every path of the driver is on by default; a user turns nothing on. A risky change has one way back (`name=0`),
  listed in the [README](../README.md#what-is-on-by-default-and-the-ways-back), in each version's "The ways back" in
  the changelog and in [SWITCHES.md](SWITCHES.md).
- Evidence the original project published is marked as the original's. The NVMetrics application has its own record
  ([NVMETRICS.md](NVMETRICS.md)).

## Evidence ledger

One machine has run this fork: an AMD Ryzen 5 5500 hackintosh (OpenCore, AMD_Vanilla), an RTX 5050 ASUS (10de:2d83,
1043:8a3f, GB207-A, Blackwell), macOS 26.7.3 x86_64, one 1920x1080 display at 100 Hz, Xcode 26.5, RM/GSP 620.70.06.
Installed and run there: 3.0, 3.0.1, 3.0.2, 3.0.6, 3.0.7, 3.0.8, 3.0.9, 3.0.11, 3.0.12, 3.0.17, 3.0.19 and 3.0.20;
each version in between, but 3.0.15, reached the card inside the next one installed. macOS 15 has not been tried, and no other card
has run a 3.0.x build. "Read by its owner" means a figure from the game's own frame counter, reported in words: no
capture was taken and no scene was matched. A capture is a `sample` capture with the kernel's counters.

| Version | Built and tested where | On the card | Not shown yet |
|---|---|---|---|
| 2.9 (the original's, the base) | The original project's release; its package is the base `./build.sh package` stages over | The original reported RTX 5060 rendering and image-processing tests and RTX 5050 sleep/wake, on macOS 15.8.1 (its evidence, not this fork's) | Everything this fork changed after it |
| 3.0 | At aa27c73 on one Mac (macOS 26.7, Xcode 26.5): 17 components with no warning, the package, 305 of 317 host tests passed and none failed; earlier commits also on GitHub's macOS 15 runners | Installed over 2.9 and started twice without the fallback job putting 2.9 back. A screen share measured at aa27c73 took WindowServer from 100 to 20 frames a second, which 3.0's grants of video memory without a second fill address; with them installed, Minecraft on Vulkan read by its owner at 120 frames a second while sharing, where it had been 40 | OpenGL during a share; the counters after a sleep; the commits after aa27c73 were host-tested on Linux only |
| 3.0.1 | b80056ef built and packaged; the commits after it up to 44091839 host-tested on Linux | Card checks ran on b80056ef and found the translator fault 3.0.1 fixes. 44091839 installed: on its first boot the userspace watchdog killed WindowServer once, the next WindowServer started normally (the fault 3.0.2 fixes) | Superseded by the installs below |
| 3.0.2 (Release 1) | Its fix compiled and host-tested on Linux (`tools/test_fcwarm.py`) | Installed on the RTX 5050. The two captures the 3.0.6 audit started from were taken on it: a heavily modded Minecraft pack through Apple's OpenGL renderer at 19 and at 74 frames a second | A run of cold shader variants can still add up at the first start after a translator change (CHANGELOG, 3.0.2) |
| 3.0.3 | The Zink layout guard on Linux only (`tools/test_cgl_gld_guard.py`, 266 cases; syntax checks against the macOS 11.3 SDK) | Inside 3.0.6: OpenGL goes through Zink on macOS 26.7.3, where a card-check run before 3.0.3 had 39 Zink rows inactive (38 refused by the old guard). GPU Caps (PRs #6 and #7), on its own branch on 2026-11-17: Phase 1 9 verified and 156 unknown records, 1,000 read-only calls, the generation advanced after a resume; Phase 2B 15 verified and 150 unknown, generation 1 → 2, manual authorization false | GPU Caps on other OS and card combinations, and the remaining unknown records; the combined 3.0.3 tree is not certified by it |
| 3.0.4 | Pull requests 573 and 575 to 580 to the original by luckiest-luk. Linux only: five new host tests pass on it and fail on 3.0.3 | Inside 3.0.6 | Its single fixes on the card (uniforms through a constant buffer, the snapshots, buffer-texture bounds, read-only copy-back, giving back a host buffer's pages); `tools/test_readonly_copyback_gpu.m` needs a card and has not run |
| 3.0.5 | Pull requests still open in the original: 597 by Geek, the Firefox parts of 487 by dreamelite96, the rest of 524 by emretascii. Linux only | Inside 3.0.6 | Its single fixes on the card: a head that stays dark after a wake, video in Firefox's sandbox, League of Legends and the iOS Simulator in the translator, protected video, WindowServer at 4K 240 Hz |
| 3.0.6 | At 52a77475 on the test machine (eleven components rebuilt in 420 s, 0 warnings where they are counted); the host suite ran there too | Installed 2026-11-20 over 3.0.2, the first driver after 3.0.2 on a card; no fault in the hours of use that followed. OpenGL through Zink. With the machine's own OpenCore `debug.nvrm_pat_wc` reads 0 (AMD_Vanilla's Fix PAT, table 0x0007010600070106). The modded pack: 55 to 64 frames a second in three captures (scenes not matched with 3.0.2's). From a test OpenCore on a USB stick that sets entry 6 of the page attribute table to write-combining: `debug.nvrm_pat_wc` reads 1, and the game's main thread spends 1.4 % in buffer uploads (11-14 % before) and 1.2 % importing the window's surface (7-8 % before) | The card checks; a gain of any one change; what the table change adds in frames (the captures it was set against were not like for like); nothing captured with `nvzink=0` |
| 3.0.7 | At 9682b3a2 on the test machine, from an empty folder of build outputs: 19 components in 512 s, 0 warnings | Installed over 3.0.6, started normally; the boot guard counted the start as good. Card checks: 133 of 134 rows ran, 130 passed, 3 failed (`zink.win-thread`, `v301.async-upload` "limit", `zink.migrate`, described in the changelog), 1 skipped; the OpenGL rows ran through Zink, Xid 0, WindowServer the same process. From the test OpenCore, the pack at 90 frames a second with the GPU 83 % busy, where the comparable capture on 3.0.6 gave 61 and 51 % (scenes not identical) | No card row runs a swap one frame ahead; neither change measured on its own; 3.0.7 from the machine's own OpenCore (`nvrm_pat_wc` 0); no later version records a fix of the three failed rows |
| 3.0.8 | At 9969d3e8 on the test machine (only the Zink frontend changed) | Put in place on 2026-11-22 without a restart, by copying the changed libraries | No capture: the most it can give back is the 5.7 % of the render thread the opacity queries took |
| 3.0.9 | At cb4df8b1 on the test machine (the Vulkan frontend, no warning); `test_nvk_bridge.py` and `test_nvk_bridge_newest.py` pass there | Installed 2026-11-22, restarted without a fault. Minecraft 26.3 on its Vulkan renderer, vertical sync off: up to 1000 frames a second read by its owner, where a capture on 3.0.8 had 200 | No capture on 3.0.9; no card row presents through the new thread |
| 3.0.11 | At 577f5f09 on the test machine (the Vulkan frontend and Zink, no warning); its three host tests pass there | Installed 2026-11-22, restarted without a fault. Minecraft without mods on OpenGL through Zink: 490 frames a second read by its owner | No figure from before on Zink, no capture; a MAILBOX swapchain in an application; a resize, a minimize or a second display under the sender thread |
| 3.0.12 | At f561d334 on the test machine (NVK, no warning: the two new patches through macOS's `patch`, Apple's clang and the Mac's Rust); `test_nvk_nak_switch_key.py` and `test_nvk_chain.py` pass there | Installed 2026-11-22 with the installer and a restart: `driver-version` reads 3.0.12, the installed NVK equals the package's, `IOGLBundleName` is `NVKZinkGLDriver`. The modded pack, read by its owner: 123 to 290 frames a second, and 75 to 190 with a shader pack on | It changes nothing while no compiler switch is set; the uniform-handle experiment (`NAK_TEX_UHANDLE=1`) has not run on a card; `test_nak_tex_uhandle_safe.py` was not run on the Mac |
| 3.0.13 | Linux: `test_nvrm_pat.c` and `test_nvrm_pat.py` pass, the switch registry check passes, the new kernel code compiled for macOS in a stub unit with its instructions in the intended order | Not installed on its own; its code is in the packages installed since 3.0.17. Onboard sound on macOS 26 (PR #17, the note its installer prints): with `alcid=66` in boot-args only the NVIDIA display outputs appeared, and removing it brought the onboard outputs back. Its default DLSS step skipped itself in every package of this fork until 3.0.19 fixed the checksum lookup | NVRM rewriting entry 6 of the page attribute table on a machine whose entry 6 is not write-combining, at a start and after a wake |
| 3.0.14 | Linux only, a developer tool ([PR #18](https://github.com/TheFormidable/nvidia-macos-driver/pull/18): `NAK_STATS=1`, a corpus of 300 shader stages, `tools/nak_stats_diff.py`): the fork's NVK built on Mesa's nouveau drm-shim as shader model 120, every corpus stage compiles, `test_nak_stats.py` passes | Its patch is in the packages installed since 3.0.17 | No run of `NAK_STATS=1` on a Mac is recorded |
| 3.0.15, not kept | [PR #20](https://github.com/TheFormidable/nvidia-macos-driver/pull/20): met the compiler gate on Linux | Its NVK copied by hand over 3.0.12. Three captures at a fixed place in ATM10 with the shader pack, phase 1 on, off, on: 1.285, 1.283 and 1.277 J per frame, 75 to 79 game frames a second with the GPU at 63 to 68 %. No gain, so it was closed without merging; the render thread waited 19 to 25 % of its time in `glBufferSubData` | Its patch is not in the tree ([CHANGELOG.md](../CHANGELOG.md#3015-tried-on-top-of-3014-and-not-kept)) |
| 3.0.16 | Linux: the patch applies to Mesa 26.3.0 with `prepare_zink.py`; the threaded context and Zink compile with GCC without a warning; `test_zink_subdata_stage.py` passes. At 394be13a on the test machine, without a warning | Its Zink copied by hand over 3.0.12 (2026-11-24): two ATM10 captures, as installed and with `NVK_ZINK_SUBDATA_STAGE=0`, both without the wait (GPU 96 to 98 %). In the packages installed since 3.0.17 | Whether ATM10's render thread stops waiting in `glBufferSubData`: the state that waits did not come while 3.0.16 was in place, and no capture taken during the wait is recorded since |
| 3.0.17 | Linux only: `test_nvk_zcull.py` passes; the new device-setup code compiled on its own against the RM 610.57.04 headers; the patch applies last in the 3.0 chain | Installed and run on the test machine | Whether RM answers the ZCULL query on the RTX 5050 (`NVK_ZCULL_LOG=1` says), and any gain or artefact in a game: no result recorded |
| 3.0.18 | The merge of the original's 3.1. Linux: every kext source compiles with no error or warning and every import resolves; the 3.0 NVK chain applies without fuzz; the host tests the merge broke were fixed (`test_backlight_keeper.py`, from the original, fails 3 of its arms on Linux on the original's own tree too) | Inside 3.0.19 and 3.0.20 | None of the original's fixes exercised on its own (the lock screen's crash loop, NAK's late split of wide reads, the boot guard's one answer per start, the offline install, the backlight job) |
| 3.0.19 | Linux: `tools/test_pick_kit.py` passes. On the owner's Mac (macOS 26.7.3, 2026-11-25), from 2e4b9cc4: the 13 kits for 10.7 to 14 and the package with them inside (`nullmoth-nvidia-local-2e4b9cc4.tar.gz`, `check_package.sh` passes over 591 files); each kit's kexts went through that release's kext linker where it exists (up to 10.15); `pick_kit.sh` named the right part for 10.7.5, 10.13.6, 10.14.6, 12.7.6, 14.8.3 and 26.7.3, without installing | Installed on the test machine; that install found the DLSS step's checksum bug fixed in 3.0.19. Firefox sharing a window: WindowServer's marks and grants waited 301 times and 420 ms in ten seconds (3.0.20's starting point). The AMFIAnchor route ([PR #28](https://github.com/TheFormidable/nvidia-macos-driver/pull/28)), one boot from an external disk on 2026-11-26 (25G342): graphics bundles signed by a private CA, OpenCore loading Lilu → AMFIAnchor 0.1.2 → the anchor → AMFIPass; `InstallStatus` 1 with the anchor `org.theformidable.nvidia-signing`, no AMFI boot arguments (AMFI enforcing), no `Failed to create MetalDevice` or platform-binary rejection, the RTX 5050 at Metal 4: gate 3 of [SIGNED-GRAPHICS-EXPERIMENT.md](SIGNED-GRAPHICS-EXPERIMENT.md) | No kit has run on a card or on an older macOS; the AMFI route's gate 4 (repeated boots, sleep/wake, sustained GPU load, and separately App Store and iMessage) and internal-disk persistence |
| 3.0.20 (Release 2) | Linux: NVAccel compiled for macOS 15 and 26 against MacKernelSDK with no warning; `test_pageoff_copy_unlocked.py` and the other page-off tests pass. On the test machine: the package built and its graphics stack signed with the owner's own CA | Installed 2026-11-26 and loaded with AMFI enforcing through AMFIAnchor; three of the page-off tests pass there. Firefox 161 sharing a window of a 60 fps test pattern for ten seconds: 835 page-offs, 809 copied without the lock (13 thrown away and copied again with it, none failed); WindowServer's marks and grants waited 130 times and 116 ms, against 301 and 420 ms on 3.0.19. The WebRTC getDisplayMedia sample runs smoothly in Firefox; Google Meet stays at 5 frames a second in Firefox and Safari, which Meet asks of browsers that do not call themselves Chrome ([TROUBLESHOOTING.md](TROUBLESHOOTING.md)) | OBS and Zoom not measured. The kexts' approval at a first install ([PR #32](https://github.com/TheFormidable/nvidia-macos-driver/pull/32), `tools/test_kext_preapprove.py`) joined 3.0.20 after that install and is host-tested only |

Build UUIDs and the exact meaning of the GPU Caps records are in [GPU-CAPS.md](GPU-CAPS.md) and
[Phase 1 evidence](GPU-CAPS-PHASE1-VALIDACION.md).

## Implemented vs proven

- **Metal:** the plugin on NVK/NAK and the AIR-to-SPIR-V translator. Surface fences, sleeping event waits, channel
  recovery, captured-frame VRAM work, asynchronous texture uploads, shaders translated ahead and the changes of the
  3.0.6 audit are present and on. Do not infer per-feature hardware success from a desktop boot.
- **OpenGL:** Zink on NVK is the default on macOS 15.5 or later and is what the test machine runs on macOS 26.7.3
  since 3.0.6. A swap runs one frame ahead (3.0.7), keeps the opacity answer (3.0.8) and with the swap interval at 0
  shows the newest frame without waiting for the display (3.0.11). A refused context can stay on Apple's renderer; a
  dropped call can return NULL. `nvzink=0` goes back to Apple's OpenGL; on another macOS build check GL_RENDERER in
  each process.
- **OpenGL buffer uploads without a wait (3.0.16):** on by default, `NVK_ZINK_SUBDATA_STAGE=0` goes back.
  On the card only where the game did not wait: two ATM10 captures, with it and without it, the same; not yet shown
  removing the wait.
- **Vulkan:** the application runtime (`tools/nvk-bridge`) is installed with the driver and independent of the Metal
  path; an application selects it when it is launched. IMMEDIATE and MAILBOX swapchains show the newest frame (3.0.9,
  3.0.11). Seen with Minecraft 26.3 only. The Khronos loader offered to applications is v1.4.367; NVK itself
  advertises Vulkan 1.4.363, from the headers of its Mesa 26.3.0 base (`nvk/MESA_BASE`, 26.3.0-rc3), and stays there
  until Mesa is rebased on Mesa 27 ([NVK-BASE-AND-HEADERS.md](NVK-BASE-AND-HEADERS.md)). The plugin's own bundled
  loader stays 1.4.362.
- **Hierarchical Z (3.0.17):** on outside WindowServer, `NVK_ZCULL=0` goes back. No result on the card recorded.
- **Write-combining (3.0.13):** NVRM restores entry 6 of the page attribute table on every CPU, at start and after
  each wake, where an AMD kernel patch changed it (`nvpatwc=0` leaves it). The table itself is proven on the test
  machine from a test OpenCore (3.0.6, 3.0.7); NVRM writing it on a machine whose entry 6 is not write-combining is
  not.
- **DLSS:** the installer fetches NVIDIA's files by default since 3.0.13 (`sudo env NULLMOTH_DLSS=0 ./install.sh`
  skips it); in this fork's packages the step runs only since 3.0.19 (before, a checksum lookup skipped it). With the files in place the plugin serves MetalFX
  with DLSS Super Resolution and Frame Generation. This fork has no DLSS game result of its own; the original's
  figures are from an RTX 5060.
- **Older macOS (3.0.19):** one package for 10.7 to 26; `install.sh` reads the macOS version and `package/pick_kit.sh`
  picks the part (the usual driver on 15 and 26, the kit on 10.7 to 14). The kits are built by this fork from its own
  tree with Apple's SDK and Kernel Debug Kit of each release (`build/legacy/`, `build/legacy/package_kits.sh`, then
  `NM_LEGACY_KITS=<folder>` for `./build.sh package`). None has run on a card or an older macOS.
- **AMFI left on (experimental):** the graphics stack signed with a private CA made on the user's Mac and registered
  through [AMFIAnchor](https://github.com/b00t0x/AMFIAnchor) (b00t0x) loads with AMFI enforcing; gate 3 on the test
  machine, where 3.0.20 runs that way. The documented default install keeps the two AMFI boot arguments
  ([SIGNED-GRAPHICS-EXPERIMENT.md](SIGNED-GRAPHICS-EXPERIMENT.md), [AMFI-COMPATIBILITY.md](AMFI-COMPATIBILITY.md)).
- **First install without Allow (PR #32, in release 2):** the installer records the driver's kexts as allowed in
  macOS's kext policy database, so one restart is enough; where SIP keeps the database read-only, macOS asks as
  before. Host-tested only.
- **Power:** per-burst boost and live policy selector 13; optional daemon and notebook power controls have their own
  qualification requirements. No arbitrary overclock, voltage or power-limit API is added by GPU Caps.
- **Telemetry:** RUSD observations and versioned read-only GPU Caps selector 14. Six added verified records in Phase
  2B do not authorize fan SET operations.
- **Media and browsers:** the H.264 Blackwell decode path is gated by a self-test; there is no VP9 or AV1 hardware
  decode on RTX 50. DRM streaming and end-to-end WebRTC/4K capture remain to qualify on specific OS, browser and codec
  paths. Safari's GPU process cannot load the plugin (WebKit's sandbox), so WebGL and WebGPU in Safari use software.
- **Shader-compiler statistics (3.0.14):** `NAK_STATS=1` writes one JSON line per compiled shader stage;
  `tools/nak-corpus/` and `tools/nak_stats_diff.py` measure a compiler change over a fixed set of 300 stages.
  Host-tested only.
- **Tried and not kept (3.0.15):** building heavy graphics shaders again with fewer registers for more warps per SM
  met the compiler gate on Linux but gave the same joules per frame on the card; ATM10 there is held up by Zink
  waiting in `glBufferSubData`, not by its shaders.
- **Other gaps:** full multi-GPU acceleration, hybrid laptop offload, hardware ray-tracing opt-ins and Windows/Apple
  feature parity require dedicated evidence. CUDA and proprietary firmware capabilities are not implemented by this
  fork.

## Validation priorities

1. **macOS 15:** not tried by the fork. Install on 15.5 or later with a known-good way back and run the
   [hardware checklist](TESTING-ON-HARDWARE.md).
2. **The kits for 10.7 to 14:** never run on a card or on those releases. Any first install, with the boot arguments
   the kit's own installer prints.
3. **The PAT rewrite:** never exercised on a machine without write-combining in entry 6 (an AMD machine on AMD_Vanilla's
   default Fix PAT). After the start, and again after a sleep, read `sysctl debug.nvrm_pat_found debug.nvrm_pat_wc
   debug.nvrm_pat_why debug.nvrm_pat_passes debug.nvrm_pat_cpus hw.ncpu`; `nvpatwc=0` is the way back.
4. **The kexts' approval:** untried on a Mac. A first install on a Mac that never had the driver should need one
   restart and no Allow; the installer's last message says which case happened.
5. **AMFI left on:** qualified on one machine only, gate 3. Gate 4 (repeated boots, sleep/wake, sustained GPU load,
   App Store and iMessage) is open.
6. **NVK's Vulkan version:** 1.4.363 until Mesa is rebased on Mesa 27.
7. **3.0.16 and 3.0.17:** a capture while ATM10's render thread waits in `glBufferSubData` (the GPU below about 80 %
   at that place), with and without `NVK_ZINK_SUBDATA_STAGE=0`; the `NVK_ZCULL_LOG=1` line on the card, and the same
   scene with and without `NVK_ZCULL=0`.
8. **Presentation, like for like:** the same scene with and without `NVK_ZINK_PRESENT_NEWEST=0` (OpenGL),
   `NVK_BRIDGE_PRESENT_NEWEST=0` (Vulkan) and `NVK_ZINK_OPACITY_KEEP=0`; card rows that swap one frame ahead and
   through the newest-frame paths (today's rows set the present observer, which makes every swap present by itself).
9. **With every result:** the exact commit, package manifest, OS build, GPU PCI and subsystem IDs and loaded kext
   revisions. Verify read-only GPU Caps before and after a resume. Track NVMetrics runtime trials separately; keep
   manual fan control under its qualified, supervised path with verified AUTO.

Historical reports in the changelog and component documents keep their own dates, commits and original test scope.
For all guides see [the index](README.md).
