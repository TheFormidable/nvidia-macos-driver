# Signed graphics payload experiment (WhateverXe model)

This experiment follows the separation of signing from packaging documented by
[WhateverXe-Graphics](https://github.com/b00t0x/WhateverXe-Graphics/blob/main/docs/SOURCES.md).
The existing AMFIAnchor project documents a modified system-wide trust model.
It is not equivalent to stock Apple security, and the Intel results do not
qualify NullMoth or macOS 25G342.

The package importer never creates, installs, or changes a trust anchor. It
does not generate a certificate or hold a signing key. No WEX certificates,
signed Apple binaries or private keys are vendored. Keep signing and trust
administration independent of the public repository and CI.

## Create the signing identity (local only)

Build the private signing chain and the matching codeless anchor kext on your
own machine, following
[AMFIAnchor's signing documentation](https://github.com/b00t0x/AMFIAnchor/blob/main/Docs/Signing.md).
Clone [AMFIAnchor](https://github.com/b00t0x/AMFIAnchor) so its own
`tools/make_injector.py` builds the kext:

    bash tools/make_amfi_anchor.sh --amfianchor /path/to/AMFIAnchor

This writes a dedicated 4096/4096/2048-bit RSA chain, each certificate carrying
the Code Signing EKU and the platform marker `1.2.840.113635.100.6.22`, into
`~/nullmoth-signing` (override with `--keydir`). It refuses to write keys inside
any git checkout. Only the **public** `root.cert.pem` and the generated anchor
kext are meant to leave the machine; the private keys never go to GitHub, and no
other project's signing identity is reused. It prints the leaf fingerprint used
by the steps below.

## Build and inventory

Build the ordinary package from this branch using the existing build instructions.
The input must be the complete finished pkgroot, after finish_package.sh has
sealed all bundles ad hoc. Do not run finish_package.sh again on signed output.

    python3 tools/stage_signed_graphics.py --base ~/nullmoth-build/package/pkgroot

Use the actual pkgroot path printed by the build if different. This lists the
Mach-O inventory without writing anything. The first experiment covers
NVMTLDriver.bundle, NVKZinkGLDriver.bundle and nvmtl, not kernel extensions.
The kernel extensions initialized in the reported test, but their trust state
must be qualified separately. The independent signer must review each file,
preserve required entitlements and seal nested code before enclosing bundles.
See upstream [signing documentation](https://github.com/b00t0x/AMFIAnchor/blob/main/Docs/Signing.md)
for the reference workflow; do not reuse another project's private signing
identity.

`tools/sign_graphics.sh` performs this signing with the chain from
`make_amfi_anchor.sh`. It copies the three units out of a finished pkgroot
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
separate output and recomputes the complete manifest. Nothing is installed.

This is a staging experiment, not a release. In particular the ordinary
check_package.sh requires host codesign verification and has not yet been
qualified with an independently signed chain. The existing installer still
documents the legacy global AMFI arguments; do not treat its advice as proof
that the new package can boot. No automatic EFI or policy change is provided.

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
`LoadedAnchors`.

The point of the anchor is to drop the global AMFI-disabling boot arguments:
with the anchor registered and the graphics payloads signed under its CA, the
goal is a boot **without** `amfi_get_out_of_my_way=0x1 amfi=0x80`. This is the
hypothesis under test, not a proven result — keep a known-good EFI that still
has those arguments, and do not remove them from your working configuration
until a signed-only boot has actually reached a stable Metal desktop on the
target macOS build (gate 3 below). The installer still prints the legacy
arguments; that advice is unchanged and is not evidence the signed package can
boot without them.

## Acceptance gates

1. Verify complete signed payload, original source provenance, nested code,
   entitlements and runtime dependency closure.
2. Establish a supported recovery display before a hardware experiment.
3. Verify actual WindowServer load policy and a stable Metal device with AMFI
   checks enabled; test tampered/untrusted code is rejected.
4. Qualify repeated boots, Metal, OpenGL, video and gaming. Separately test
   App Store and iMessage; their failures have not been shown to be caused
   by the GPU driver's load policy.
