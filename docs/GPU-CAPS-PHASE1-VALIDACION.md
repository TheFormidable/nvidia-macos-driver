# GPU Caps phase 1 — experimental read-only ABI

## Scope

NVRM selector 14 returns a bounded, versioned, read-only, per-GPU snapshot to NVMetrics. Selector 7 advertises GPU Caps only when the lock and cache buffer exist. The NVK patch carries an identical copy of the shared ABI header.

GPU control/fan writes are **not** authorized by this ABI. Unsupported or unproven capabilities remain `unknown`; their records are not proof of hardware support.

## Native test evidence — 2026-11-17

Platform: macOS Tahoe 26.7.3 x86_64, Xcode 26.5, NVIDIA RTX 5050 (PCI 10DE:2D83, subsystem 1043:8A3F), RM 620.70.06.

- Host static tests and shared ABI checks passed.
- Native NVRM.kext rebuilt with selector-7 guard. Build UUID: `7531E787-567D-33E8-8A0F-74B00A4E8A10`.
- The installed experimental driver started normally and displayed the desktop.
- `nvmetrics-agent caps` returned `verdict=usable`, exit code `0`, `manual_authorized=false`, 9 verified and 156 unknown capabilities.
- 1,000 repeated read-only calls completed without reported errors (user-reported test result).
- After sleep/resume: `verdict=usable`, cache generation advanced from `1` to `2`, with the same verified/unknown counts.

## Limits

Validated on one card and one macOS environment, not a general compatibility guarantee. Thermal/fan/power capabilities remain unknown, not verified. The selector does not implement RPM discovery or manual fan control. Full system reliability, multiple GPUs, kernel memory stress, crash recovery, and long-duration regression remain to be evaluated.

This is the historical Phase 1 evidence. Phase 1 and Phase 2B are now merged into `fable` (PR #7); see
[GPU-CAPS.md](GPU-CAPS.md) for the newer counts and boundaries. Neither result certifies a stable release.
