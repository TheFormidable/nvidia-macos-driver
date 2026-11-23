# Fork status and evidence — 2026-11-19

This is the current snapshot of TheFormidable's `fable` branch after PR #6 and #7
(base revision `ac9bb6e4`). The first numeric heading in [CHANGELOG.md](../CHANGELOG.md)
is 3.0.3: the development package version, not a published or qualified release.
The upstream baseline is NullMoth 2.9. Keep inherited upstream evidence separate
from this fork's experiments and from the NVMetrics application.

## Evidence ledger

| Revision / scope | What was observed | What is still unknown |
|---|---|---|
| Upstream 2.9 | RTX 5060 rendering and image-processing tests on macOS 15.8.1; RTX 5050 sleep/wake on 15.8.1 | Other boards, OS builds and every workload |
| 3.0 at aa27c73 | Native build, package, boot twice on RTX 5050; capture performance measured on a 100 Hz 1080p display | Whether all subsequent fixes eliminate capture stalls, tearing or regressions |
| 3.0.1 at b80056ef / 44091839 | Card checks on RTX 5050, macOS 26.7.3; first-boot WindowServer watchdog incident after installing 44091839, second WindowServer started normally | Native acceptance of 3.0.2 warm-up correction |
| Card-test round 11, pre-3.0.3 | 39 inactive Zink rows; 38 renderer-word refusals, one glbench row inactive | Whether the revised CGL guard enables correct rendering |
| GPU Caps Phase 1, 2026-11-17 | Native NVRM, 9 verified / 156 unknown; 1,000 read-only calls reported; generation advanced after resume | General compatibility and remaining unknown records |
| GPU Caps Phase 2B, 2026-11-17 | Native build, package CHECK and desktop; 15 verified / 150 unknown, generation 1 → 2, manual authorization false | Other OS/card combinations and integrated release |
| PR #6 / #7 combined | Merge contains both the Zink correction and GPU Caps; shared-header, static-cache, evidence and Zink guard host checks passed during merge resolution | Native build, boot and full card regression of the combined tree |

The native GPU Caps platform was RTX 5050 ASUS (10de:2d83, 1043:8a3f),
GB207-A, macOS 26.7.3 x86_64, Xcode 26.5, RM/GSP 620.70.06. Build UUIDs and
the exact meaning of the records are in [GPU-CAPS.md](GPU-CAPS.md) and
[Phase 1 evidence](GPU-CAPS-PHASE1-VALIDACION.md).

## Implemented vs proven

- **Rendering:** Metal plugin on NVK/NAK and AIR-to-SPIR-V translation. Surface
  fence, sleeping event waits, channel recovery and captured-frame VRAM work are
  present. Do not infer per-feature hardware success from a desktop boot.
- **OpenGL:** Zink GLD path integrated; the 3.0.3 guard correction is host-tested
  only. A refused context can stay on Apple's renderer; a dropped call can return
  NULL. Use `nvzink=0` for rollback and verify GL_RENDERER in each process.
- **Vulkan:** the opt-in application bridge and its runtime are present, independent
  of the Metal path. Test presentation and rendering separately.
- **Power:** per-burst boost and live policy selector 13; optional daemon and
  notebook power controls have their own qualification requirements. No arbitrary
  overclock, voltage or power-limit API is added by GPU Caps.
- **Telemetry:** RUSD observations and versioned read-only GPU Caps selector 14.
  Six added verified records in Phase 2B do not authorize fan SET operations.
- **Media:** H.264 Blackwell decode path is gated by a self-test; do not advertise
  Blackwell AV1/VP9 hardware decode from that gate. DRM streaming and end-to-end
  WebRTC/4K capture remain issues to qualify on specific OS/browser/codec paths.
- **Shader-compiler statistics (3.0.14):** `NAK_STATS=1` writes one JSON line per
  compiled shader stage; `tools/nak-corpus/` and `tools/nak_stats_diff.py` measure a
  compiler change over a fixed set of 300 stages. Host-tested only: built and run on
  Linux on Mesa's nouveau drm-shim as shader model 120, never on a Mac or a card.
- **Other gaps:** full multi-GPU acceleration, hybrid laptop offload, hardware
  ray-tracing opt-ins and Windows/Apple feature parity require dedicated evidence.
  CUDA and proprietary firmware capabilities are not implemented by this fork.

## Validation priorities

1. Build the merged branch with `./build.sh doctor` / `./build.sh`; stage against
   the pinned 2.9 package and validate integrity with CHECK before installation.
   Preserve a known-good driver and OpenCore recovery route.
2. Record exact commit, package manifest, OS build, GPU PCI/subsystem IDs and
   loaded kext revisions; run the [hardware checklist](TESTING-ON-HARDWARE.md).
3. Re-run the Zink rows, inspect actual GL_RENDERER and diagnostic lines; compare
   against `nvzink=0`. Do not count inactive rows as passes.
4. Verify read-only GPU Caps before and after resume. Compare capture performance,
   WindowServer stability and media results against the last known-good package.
5. Track NVMetrics runtime trials independently; see [NVMETRICS.md](NVMETRICS.md).
   Keep manual control under its qualified, supervised path with verified AUTO.

Historical reports in the changelog and component documents retain their own
dates, commits and original test scope. For all guides see [the index](README.md).
