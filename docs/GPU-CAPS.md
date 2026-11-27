# GPU Caps — experimental read-only API

In this fork since 3.0.3: Phase 1 and Phase 2B were merged into `fable` by
[PR #7](https://github.com/TheFormidable/nvidia-macos-driver/pull/7) on 2026-11-19 (development branches
`feature/gpu-caps-phase1` and `feature/gpu-caps-phase2B`). The code is unchanged since, in Release 2 (3.0.20). It is on
by default; boot argument `nvgpucaps=0` turns the producer off (below). Companion application:
[NVMetrics](https://github.com/TheFormidable/NVMetrics), which has its own releases and runtime supervision. The two
other pages are [Phase 1 evidence](GPU-CAPS-PHASE1-VALIDACION.md) and [Phase 2](GPU-CAPS-PHASE2.md); this page has the
current counts.

## Contract

`kexts/NVRM/nvrm_gpu_capabilities_abi.h` defines the versioned ABI (1.0). Selector 14 returns a cached per-GPU capability snapshot, not arbitrary RM access. Phase 2B acquires additional RM observations outside the leaf cache lock during lifecycle refresh. No RUSD discovery is performed in the kernel. Unknown values are not fabricated. Successful queries do not imply authorization to write. The ABI includes fields that are not populated yet; the partial flag is significant.

## Native evidence (2026-11-17)

Measured on the feature branches, before 3.0.3, and not again on a later version. This fork's test machine: macOS
26.7.3 x86_64 (an AMD Ryzen 5 5500 hackintosh), Xcode 26.5, RTX 5050 ASUS (10de:2d83, subsystem 1043:8a3f), GB207-A, RM/GSP 620.70.06. Phase 1: 9 verified, 156 unknown. Phase 2B: 15 verified, 150 unknown; reported generation 1 then 2 after resume; `manual_authorized=false`. Phase 2B NVRM UUID `8DF107FB-C17F-343D-8848-B70CDDB2E387`; build and package CHECK succeeded, screen and capability queries worked after installation. This is a partial hardware validation, not a full stability certification.

The companion userspace RUSD probe returned tachometer mask 0x3, thermal mask 0x3, power mask 0x7. These identify independently observed channels, not physical cooler topology, firmware limits or permission to write. A later NVMetrics package was compiled and installed successfully by the tester.

## Boundaries

- No dynamic fan-write authorization, guaranteed Fan Stop, calibrated RPM limits, universal control identifiers, or verified thermal thresholds are provided by this work.
- Do not equate successful RM GETs, observed tachometers or a matching PCI model with write permission. Retain fail-closed handling and existing manual qualification.
- Other GPU models, multi-GPU behavior, wider OS compatibility and all firmware variants remain unvalidated.
- Existing installer rollback, lastgood, bootguard and NVRAM guard are unchanged. Use the existing package CHECK and known-good recovery path before experimental installation. The scope of this validation is NVRM and the capability API, not every driver component.

## Consumers and further validation

GPU Caps is enabled by default. Boot argument `nvgpucaps=0` leaves the snapshot
in the OFF state and skips RM capability discovery; it does not disable the GPU.

On a compatible native build, use `nvmetrics-agent caps` from the companion project to inspect the capability
verdict and generation, and compare before and after sleep. Selector 7 advertises availability; selector 14
returns the cache. The GPU index, ABI version and returned size must be validated by the consumer. The
host-side evidence tool in [Phase 2](GPU-CAPS-PHASE2.md) interprets captures independently of the kernel cache.
See [NVMetrics integration](NVMETRICS.md) for the separate runtime control and RUSD issue.
