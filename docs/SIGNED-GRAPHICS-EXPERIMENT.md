# Signed graphics payload experiment (WhateverXe model)

> **State at 3.0.20 (Release 2).** This route loads the graphics stack **with AMFI enforcing**, without
> `amfi_get_out_of_my_way=0x1 amfi=0x80` in boot-args. It is proven on the fork's one test machine (RTX 5050, macOS
> 26.7.3): its first boot that way was on 2026-11-26 from a USB stick ([Confirmed on hardware](#confirmed-on-hardware-2026-11-26)),
> and 3.0.20, built and signed on that Mac with the owner's own CA, has run signed there with AMFI enforcing. It is still
> **experimental** for every other machine, and gate 4 below is open. The documented default install keeps the two
> AMFI boot arguments. Keep an EFI that still has them ([Going back](#going-back-to-the-amfi-boot-arguments)).

**Release 2's ready-made anchor.** The 3.0.20 release package comes with its graphics stack signed with the
maintainer's CA, and the matching anchor kext, `NVAnchorInjector.kext.zip`, is published on the
[release page](https://github.com/TheFormidable/nvidia-macos-driver/releases/tag/3.0.20). The OpenCore steps below are
the same with `NVAnchorInjector.kext` as the anchor kext. That anchor makes AMFI accept anything signed with the
maintainer's key on your Mac, not only this driver: make your own chain (next section) to trust only yourself.

This experiment follows the separation of signing from packaging documented by
[WhateverXe-Graphics](https://github.com/b00t0x/WhateverXe-Graphics/blob/main/docs/SOURCES.md), and loads its trust
anchor through [AMFIAnchor](https://github.com/b00t0x/AMFIAnchor), both by b00t0x. AMFIAnchor documents a modified
system-wide trust model: it is not equivalent to stock Apple security, and the Intel results of those projects do
not qualify this driver.

**Keys never go into a repository.** The signing chain is made on your own Mac, and its private keys stay there.
No WEX certificates, signed Apple binaries or private keys are vendored in this repository, and the package importer
never creates, installs, or changes a trust anchor, generates a certificate or holds a signing key. Keep signing and
trust administration independent of the public repository and CI.

The steps in short ([docs/BUILDING.md](BUILDING.md#signing-the-graphics-stack-for-amfi-optional) has them beside the
build): make the chain and the anchor kext once, sign a finished package's three graphics units, import them into a
new package tree, install from it, and load the anchor from OpenCore.

## Create the signing identity (local only)

Build the private signing chain and the matching codeless anchor kext on your
own machine, following
[AMFIAnchor's signing documentation](https://github.com/b00t0x/AMFIAnchor/blob/main/Docs/Signing.md).
Clone [AMFIAnchor](https://github.com/b00t0x/AMFIAnchor) so its own
`tools/make_injector.py` builds the kext:

    bash tools/make_amfi_anchor.sh --amfianchor /path/to/AMFIAnchor

This writes a dedicated 4096/4096/2048-bit RSA chain, each certificate carrying
the Code Signing EKU and the platform marker `1.2.840.113635.100.6.22`, into
`~/nullmoth-signing` (override with `--keydir` or `NULLMOTH_SIGNING_DIR`). It refuses to write keys inside
any git checkout. Only the **public** `root.cert.pem` and the generated anchor
kext are meant to leave the machine; the private keys never go to GitHub, and no
other project's signing identity is reused. It prints the leaf fingerprint used
by the steps below.

Keep that folder: the anchor is made from its root certificate, so signing a newer package with the same anchor needs
the same keys. `--force` on the same folder keeps the root and its key and issues a new intermediate and leaf
certificate (the leaf is valid 365 days).

## Build and inventory

Build the ordinary package (`./build.sh package`, [BUILDING.md](BUILDING.md)). The input must be the complete
finished pkgroot, after `package/finish_package.sh` has sealed all bundles ad hoc. Do not run finish_package.sh again
on signed output.

    python3 tools/stage_signed_graphics.py --base ~/nullmoth-build/package/pkgroot

Use the actual pkgroot path printed by the build if different. This lists the
Mach-O inventory without writing anything. The route covers
NVMTLDriver.bundle, NVKZinkGLDriver.bundle and nvmtl, not kernel extensions:
those loaded in the confirmed boot as in the ordinary install, and their trust state is qualified separately.
The signer must review each file,
preserve required entitlements and seal nested code before enclosing bundles.

`tools/sign_graphics.sh` performs this signing with the chain from
`make_amfi_anchor.sh`. It needs `rcodesign` (apple-codesign 0.29.0). It copies the three units out of a finished pkgroot
(symlinks preserved), signs every Mach-O deepest-first and seals the two
bundles, writing a separate signed directory. It never runs as root and changes
nothing installed:

    bash tools/sign_graphics.sh \
      --base ~/nullmoth-build/package/pkgroot \
      --keydir ~/nullmoth-signing \
      --output ~/nullmoth-build/signed-GPUBundles

Review the signed Mach-O inventory before trusting the output; a signing
authority can confer trust outside the graphics stack.

## Import externally signed payloads

The signed input is a separate directory containing the three units at its
top level, with identical resources and symlinks. The importer takes the
expected local signing certificate fingerprint and uses rcodesign for
cryptographic checks. The code UUID check is a build-identity check, not proof
that executable code is otherwise unchanged. Review executable provenance
separately. Sign only audited code; a signing authority may confer trust
outside the graphics stack.

    python3 tools/stage_signed_graphics.py \
      --base /path/to/pkgroot \
      --signed-gpu /path/to/signed-GPUBundles \
      --leaf-fingerprint EXPECTED_CERTIFICATE_FINGERPRINT \
      --output /path/to/pkgroot-signed

The importer verifies the original manifest, requires the same payload path
set and build UUIDs, verifies signatures, checks bundle seals, creates a
separate output and recomputes the complete manifest. Nothing is installed:
install from the new tree with `install.sh`, as from any package.

`package/check_package.sh` (the last step of an ordinary package build) is not run on the signed tree; the importer's
checks take its place. No automatic EFI or policy change is provided. `install.sh` itself still says, when boot-args
lack the two AMFI arguments, that WindowServer will refuse the plugin unless they are added or the graphics stack is
signed and loaded through AMFIAnchor; on a signed install that note is expected.

## OpenCore configuration

The anchor is an OpenCore kext, not part of the installed package. In
`Kernel > Add`, load in this order, each with `PlistPath` set to
`Contents/Info.plist`:

| Order | BundlePath | ExecutablePath |
| --- | --- | --- |
| 1 | `Lilu.kext` | `Contents/MacOS/Lilu` |
| 2 | `AMFIAnchor.kext` | `Contents/MacOS/AMFIAnchor` |
| 3 | the anchor kext from `make_amfi_anchor.sh` | Empty string |

Use Lilu 1.7.2 or later and `AMFIAnchor.kext` from
[b00t0x/AMFIAnchor releases](https://github.com/b00t0x/AMFIAnchor/releases); set
`MinKernel` `20.0.0` and `MaxKernel` `25.99.99` on entries 2 and 3. After a boot,
`ioreg -r -c AMFIAnchor -l` should show `InstallStatus` 1 and the anchor under
`LoadedAnchors`. (The boot confirmed on the test machine loaded AMFIPass after the anchor.)

The point of the anchor is to drop the global AMFI-disabling boot arguments:
with the anchor registered and the graphics payloads signed under its CA, the machine boots **without**
`amfi_get_out_of_my_way=0x1 amfi=0x80`. On the test machine this works. On yours, try it on a copy of your EFI
(a USB stick, as on the test machine): keep a known-good EFI that still has those arguments, and do not remove them
from your working configuration until a signed-only boot has reached a stable Metal desktop on your macOS build
(gate 3 below).

## Going back to the AMFI boot arguments

If a boot with AMFI enforcing ends without a Metal desktop (no acceleration, or `Failed to create MetalDevice` or
`mapping process is a platform binary` in the boot's log), or you want to leave the route:

1. Start from the EFI that still has `amfi_get_out_of_my_way=0x1 amfi=0x80` (pick its disk at the firmware's boot
   menu). Without such an EFI, put the two arguments back into `boot-args` (`NVRAM > Add`, keeping the
   key in `NVRAM > Delete`) of the `config.plist` you boot, from another system.
2. To drop the route for good, also set `Enabled` false on `AMFIAnchor.kext` and the anchor kext in `Kernel > Add`.
   Lilu and AMFIPass stay: the ordinary install needs them.
3. Check after the restart: `sysctl -n kern.bootargs` shows both AMFI arguments, and
   `system_profiler SPDisplaysDataType` shows the card with Metal.
4. If the desktop still has no Metal device, reinstall the ordinary (ad hoc signed) package the signed one was made
   from: `sudo ./install.sh` in its pkgroot.

`-nvoff` in boot-args starts macOS without the driver ([README](../README.md#if-something-goes-wrong)); it is the
way back when the driver, not AMFI, is the problem. Keep the private keys either way: a new anchor needs them.

## Confirmed on hardware (2026-11-26)

On an RTX 5050 / macOS 26.7.3 (25G342), booting the anchor EFI (Lilu →
AMFIAnchor 0.1.2 → this project's anchor → AMFIPass) with a package whose three
graphics units were signed by the private chain:

- `ioreg -r -c AMFIAnchor -l` reports `InstallStatus = 1` and `LoadedAnchors`
  containing the project anchor (`org.theformidable.nvidia-signing`, 550-byte SPKI).
- `sysctl -n kern.bootargs` shows no `amfi_get_out_of_my_way` or `amfi=` — AMFI
  enforcing.
- The unified log for that boot has no `Failed to create MetalDevice` or
  `mapping process is a platform binary` entries.
- `system_profiler SPDisplaysDataType` reports the RTX 5050 with Metal 4 driving
  the display.

This satisfies gate 3 (actual WindowServer load policy and a stable Metal device
with AMFI enabled) for this configuration. That first boot was from an external disk (a USB stick). The same day
3.0.20 was installed on that machine, its package built and its graphics stack signed there with the owner's chain,
and it ran with AMFI enforcing through AMFIAnchor; its screen-sharing measurement (Firefox sharing a window) was taken
that way ([STATUS.md](STATUS.md), 3.0.20). Gate 4 remains open: repeated boots, sleep/wake, sustained GPU/gaming
load, and separately App Store and iMessage, are not yet qualified. Writing the EFI to the internal disk and
persistence are the operator's decision, and the private keys that back the anchor must be kept.

## Acceptance gates

1. Verify complete signed payload, original source provenance, nested code,
   entitlements and runtime dependency closure.
2. Establish a supported recovery display before a hardware experiment.
3. Verify actual WindowServer load policy and a stable Metal device with AMFI
   checks enabled; test tampered/untrusted code is rejected. Met on the test machine for the load policy and the
   Metal device (above); no tampered-code trial is recorded here.
4. Qualify repeated boots, Metal, OpenGL, video and gaming. Separately test
   App Store and iMessage; their failures have not been shown to be caused
   by the GPU driver's load policy. Open.
