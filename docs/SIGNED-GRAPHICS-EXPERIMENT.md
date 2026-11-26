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

## Acceptance gates

1. Verify complete signed payload, original source provenance, nested code,
   entitlements and runtime dependency closure.
2. Establish a supported recovery display before a hardware experiment.
3. Verify actual WindowServer load policy and a stable Metal device with AMFI
   checks enabled; test tampered/untrusted code is rejected.
4. Qualify repeated boots, Metal, OpenGL, video and gaming. Separately test
   App Store and iMessage; their failures have not been shown to be caused
   by the GPU driver's load policy.
