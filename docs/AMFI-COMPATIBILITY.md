# AMFI compatibility and graphics recovery

> **State at 3.0.20 (Release 2).** The documented install keeps AMFI from enforcing with the boot arguments
> `amfi_get_out_of_my_way=0x1 amfi=0x80`. The failure below is what happens without them and with the ordinary package.
> The optional trust-chain route ([PR #28](https://github.com/TheFormidable/nvidia-macos-driver/pull/28)) answers it:
> the graphics stack signed with a private CA made on your Mac, and that CA registered in OpenCore as an
> [AMFIAnchor](https://github.com/b00t0x/AMFIAnchor) (b00t0x) anchor. It is proven on the fork's one test machine (RTX
> 5050, macOS 26.7.3, 25G342): its first boot with AMFI enforcing (2026-11-26, from a USB stick) had WindowServer load
> the signed plugin and the GPU drive the display at Metal 4, and 3.0.20 has since run signed that way with AMFI on. It
> is still **experimental** for every other machine. The steps, the checks and the way back are in
> [SIGNED-GRAPHICS-EXPERIMENT.md](SIGNED-GRAPHICS-EXPERIMENT.md).

## Observed failure
On macOS build 25G342, RTX 5050, AMFIPass 1.4.1 and Lilu 1.7.3, a trial without the global AMFI-disabling arguments reached the NVIDIA kernel driver's initialization but WindowServer aborted in CoreDisplay. The observed policy is platform-binary library validation of NVMTLDriver. The ordinary package seals that bundle ad hoc. Integrity validation and permission to load a third-party bundle into an Apple platform process are distinct.

The driver does not make the bundle a platform binary, remove the legacy requirement, change AMFI or IPC policy, or modify Apple services. The ordinary package is not a secure-mode driver release; the anchor route adds trust for your own CA only, in OpenCore, on your machine.

## Read-only audit
Run from the checkout on macOS, with the unified log of the boot saved first:

    log show --last boot > ~/amfi-boot.log
    python3 tools/amfi_diagnostics.py --log ~/amfi-boot.log

The audit reports code signature integrity, signature metadata and direct dependencies of the installed `NVMTLDriver.bundle` (another with `--bundle`), the AMFI boot arguments in effect, and counts of log evidence (library-validation and platform-binary rejections of the plugin, `Failed to create MetalDevice`) without dumping the raw log. An absent log or a successful codesign check does not establish authorization. With the anchor route, `ioreg -r -c AMFIAnchor -l` is the check that the anchor loaded (`InstallStatus` 1, your anchor under `LoadedAnchors`).

## Recovery
The bootguard checks the start count and restart latch after writing them and checks that the good-start marker is absent. It refuses automatic reboot if those checks fail. Readback is not proof of persistence across a restart. Keep a known-good EFI and a tested alternate display path; a previous driver cannot repair a mismatched security policy or guarantee a usable framebuffer. Do not repeatedly reboot on a failed trial.

A trial of the anchor route that ends without a Metal desktop is undone by going back to the two AMFI boot arguments: start from the EFI that still has them ([SIGNED-GRAPHICS-EXPERIMENT.md](SIGNED-GRAPHICS-EXPERIMENT.md#going-back-to-the-amfi-boot-arguments)).

## Architecture milestone
Beyond the anchor route: investigate supported, authorized GPU integration with Apple; identify exactly which Metal ABI interfaces must run in the client process. Isolate shader compilation and other work that can live in a separately signed, least-privileged process with bounded IPC, and keep the hot-path submission local where authorized. An external service does not by itself satisfy WindowServer's policy. A developer certificate or notarization does not grant Apple platform status.

A future experimental profile must fail closed before claiming an unsupported display, preserve a tested recovery route, and prove legitimate load authorization on each supported macOS build. A supported secondary GPU can provide a desktop while testing independent compute paths; this is not equivalent to system-wide Metal support on NVIDIA.

Acceptance: healthy desktop and Metal rendering, repeated restarts, simulated failures of NVRAM writes and readback, no unauthorized code-loading policy changes, then separate App Store and iMessage validation. No claim of iServices repair until reproduced.
