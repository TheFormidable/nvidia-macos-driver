# Security

## Reporting a vulnerability

For a vulnerability in this fork, report privately through GitHub: [open a security advisory](https://github.com/TheFormidable/nvidia-macos-driver/security/advisories/new).
Please do not open a public issue for a vulnerability. Include the driver version (`cat /Library/NullMoth/driver-version`),
the macOS build, the steps that show the problem, and the exact commit. For a vulnerability in the original project's
own releases, use [NullMoth's advisory channel](https://github.com/nullmoth/nvidia-macos-driver/security/advisories/new).

## Where the driver comes from

This repository is an independent fork. [Fork releases](https://github.com/TheFormidable/nvidia-macos-driver/releases)
and builds from this source are not [official NullMoth releases](https://github.com/nullmoth/nvidia-macos-driver/releases),
whatever the package's file name says (`nullmoth-nvidia-local-<commit>.tar.gz`). A package's `SHA256SUMS` verifies its
contents against its manifest, not the identity of its publisher: `install.sh` checks a package's integrity, not who
built it. Verify where a package comes from, and keep a known-good package to go back to.

GPU Caps is read-only. It does not authorize fan, voltage, clock or power writes. NVMetrics is a separate project:
its experimental manual cooling relies on qualified hardware, live telemetry, watchdog and verified AUTO recovery.

## What the driver changes on your Mac

macOS will not load a GPU driver Apple did not sign unless some of its protections are relaxed. The OpenCore settings
in the README ("Install") make these system-wide changes:

- **Boot arguments** `amfi_get_out_of_my_way=0x1` and `amfi=0x80` turn off macOS's check of app signatures and the
  entitlements they claim. A program running as you can load code into your other apps, including ones with camera,
  microphone, or Full Disk Access, and can give itself entitlements Apple would normally refuse. `ipc_control_port_options=0`
  relaxes a related protection on task ports.
- **System Integrity Protection** is partly off (`csr-active-config` 0x0A43), and Apple Secure Boot is off
  (`SecureBootModel` `Disabled`).

`install.sh` also:

- records the driver's kexts as allowed in macOS's kext policy database (the SIP-protected
  `/var/db/SystemPolicyConfiguration/KextPolicy`, which the SIP setting above lets root write), scoped to the
  driver's own kext identifiers, so that a first install needs no **Allow** in System Settings. Where the database is
  read-only, macOS asks as before;
- downloads the GPU firmware and the DLSS files from NVIDIA's own sites and checks each against a pinned hash
  (`sudo env NULLMOTH_DLSS=0 ./install.sh` skips DLSS).

### AMFI left on (optional, experimental)

There is an alternative to the two AMFI boot arguments: sign the driver's graphics stack (NVMTLDriver.bundle,
NVKZinkGLDriver.bundle and the nvmtl libraries) with a certificate authority of your own, and register that CA in
OpenCore as an [AMFIAnchor](https://github.com/b00t0x/AMFIAnchor) (b00t0x) anchor. AMFI then stays enforcing for the
whole system, and the one thing added to what it trusts is code signed by your CA: a much narrower change than
turning off signature checks entirely. On the fork's one test machine this works: 3.0.20 has run signed that way, with AMFI
enforcing. It is still **experimental** for everyone else, and the documented default install keeps the two AMFI
arguments.

- It still relaxes SIP and Secure Boot as above.
- Your CA becomes a system trust anchor: anything signed with it is trusted, not only the driver. Its private keys are
  made on your Mac (`tools/make_amfi_anchor.sh` refuses a folder inside any git repository); keep them there, never
  in a repository or on GitHub. Only the public root certificate and the anchor kext leave the machine.
- Keep an EFI that still has the two AMFI arguments, to go back to.

The steps, the checks and how to go back are in
[docs/SIGNED-GRAPHICS-EXPERIMENT.md](docs/SIGNED-GRAPHICS-EXPERIMENT.md).

## Undoing the changes

`uninstall.sh` removes the driver, and takes `gfxrst=4` back out of the NVRAM boot-args when the installer added it.
It does not change OpenCore: take the boot arguments out of `config.plist` and set `csr-active-config` and
`SecureBootModel` back yourself. The approval rows the installer wrote stay in the kext policy database. While these
settings are on, only run software you trust, as you would on any Mac with these protections off.
