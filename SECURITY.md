# Security

## Reporting a vulnerability

For fork-specific vulnerabilities, report privately through GitHub: [open a security advisory](https://github.com/TheFormidable/nvidia-macos-driver/security/advisories/new).
Please do not open a public issue for a vulnerability. Include the driver version, the macOS version, and the steps that
show the problem, including the exact commit. For an upstream vulnerability, use [NullMoth's advisory channel](https://github.com/nullmoth/nvidia-macos-driver/security/advisories/new).

## Where the driver comes from

This repository is an independent fork. [Fork releases](https://github.com/TheFormidable/nvidia-macos-driver/releases)
and source builds are distinct from the [official NullMoth releases](https://github.com/nullmoth/nvidia-macos-driver/releases),
1401, and [nullmothsystems.com](https://nullmothsystems.com). Upstream 1401 verifies the NullMoth release signature;
do not assume that it trusts or distributes this fork's builds. A source package's checksum verifies its contents
against its manifest, not the identity of its publisher. Verify provenance and keep a known-good recovery package.

GPU Caps is read-only. It does not authorize fan, voltage, clock or power writes. NVMetrics is a separate project:
its experimental manual cooling relies on qualified hardware, live telemetry, watchdog and verified AUTO recovery.

## What the driver changes on your Mac

macOS will not load a GPU driver Apple did not sign unless some of its protections are relaxed, so the setup makes two
system-wide changes:

- **Boot arguments** `amfi_get_out_of_my_way=0x1` and `amfi=0x80` turn off macOS's check of app signatures and the
  entitlements they claim. A program running as you can load code into your other apps, including ones with camera,
  microphone, or Full Disk Access, and can give itself entitlements Apple would normally refuse. `ipc_control_port_options=0`
  relaxes a related protection on task ports.
- **System Integrity Protection** is partly off (`csr-active-config` 0x0A43).

Removing the driver with the Mac app restores what its setup changed. `uninstall.sh` on its own does not change them. While
these settings are on, only run software you trust, as you would on any Mac with these protections off.
