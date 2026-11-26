# AMFI compatibility and graphics recovery

> **Since resolved for one configuration (2026-11-26).** The trust-chain route
> described below as an architecture milestone was built and reached gate 3 on
> an RTX 5050 / macOS 26.7.3 (25G342): signing the graphics bundles with a
> private CA and registering that CA as an [AMFIAnchor](https://github.com/b00t0x/AMFIAnchor)
> anchor let WindowServer load them with AMFI enforcing, the GPU driving the
> display at Metal 4, no `amfi_get_out_of_my_way`/`amfi=` boot arguments. That
> is a single boot and a staging experiment, not the tested default — see
> [SIGNED-GRAPHICS-EXPERIMENT.md](SIGNED-GRAPHICS-EXPERIMENT.md). The failure
> analysis and the open acceptance work below remain accurate.

## Observed failure
On macOS build 25G342, RTX 5050, AMFIPass 1.4.1 and Lilu 1.7.3, a trial without the global AMFI-disabling arguments reached the NVIDIA kernel driver's initialization but WindowServer aborted in CoreDisplay. The observed policy is platform-binary library validation of NVMTLDriver. The existing package seals that bundle ad hoc. Integrity validation and permission to load a third-party bundle into an Apple platform process are distinct.

This branch does not make the bundle a platform binary, remove the legacy requirement, change AMFI or IPC policy, or modify Apple services. It is not a secure-mode driver release.

## Read-only audit
Run from the checkout on macOS:

    python3 tools/amfi_diagnostics.py --log ~/Desktop/amfi-prueba-A.log

The audit reports code signature integrity, signature metadata and direct dependencies, selected boot arguments, and counts of log evidence without dumping the raw log. An absent log or a successful codesign check does not establish authorization.

## Recovery
The existing bootguard now checks the start count and restart latch after writing them and checks that the good-start marker is absent. It refuses automatic reboot if those checks fail. Readback is not proof of persistence across a restart. Keep a known-good EFI and a tested alternate display path; a previous driver cannot repair a mismatched security policy or guarantee a usable framebuffer. Do not repeatedly reboot on a failed trial.

## Architecture milestone
Investigate supported, authorized GPU integration with Apple; identify exactly which Metal ABI interfaces must run in the client process. Isolate shader compilation and other work that can live in a separately signed, least-privileged process with bounded IPC, and keep the hot-path submission local where authorized. An external service does not by itself satisfy WindowServer's policy. A developer certificate or notarization does not grant Apple platform status.

A future experimental profile must fail closed before claiming an unsupported display, preserve a tested recovery route, and prove legitimate load authorization on each supported macOS build. A supported secondary GPU can provide a desktop while testing independent compute paths; this is not equivalent to system-wide Metal support on NVIDIA.

Acceptance: healthy desktop and Metal rendering, repeated restarts, simulated failures of NVRAM writes and readback, no unauthorized code-loading policy changes, then separate App Store and iMessage validation. No claim of iServices repair until reproduced.
