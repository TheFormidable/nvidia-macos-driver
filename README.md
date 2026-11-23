# NVIDIA driver for macOS — TheFormidable fork

Based on [NullMoth](https://github.com/nullmoth/nvidia-macos-driver), this fork develops NVIDIA acceleration for
Intel macOS and OpenCore PCs, with additional contributions from HighDelay's NVK fork and the companion
[NVMetrics](https://github.com/TheFormidable/NVMetrics) project.

> **Development status — November 23, 2026.** `fable` is at 3.0.14, the first heading of
> [CHANGELOG.md](CHANGELOG.md); 3.0.1 to 3.0.14 are merged. None is a published release or a certified binary: each
> is a source version, built and installed by hand. One machine has run this fork (AMD Ryzen 5 5500, RTX 5050, macOS
> 26.7.3, one 1920x1080 display at 100 Hz): 3.0, 3.0.1, 3.0.2, 3.0.6, 3.0.7, 3.0.8, 3.0.9, 3.0.11 and 3.0.12 were
> installed and started there, the packages from 3.0.6 on built on that machine. 3.0.3 to 3.0.5 reached the card only
> inside 3.0.6, and there is no 3.0.10. 3.0.13 has not been built on a Mac or run on a card. 3.0.14 adds statistics
> of the shader compiler, a development tool ([PR #18](https://github.com/TheFormidable/nvidia-macos-driver/pull/18)),
> tested on Linux only. 3.0.15, more warps per SM for heavy shaders
> ([PR #20](https://github.com/TheFormidable/nvidia-macos-driver/pull/20)), was measured on the card, gave nothing
> and was not kept ([CHANGELOG.md](CHANGELOG.md#3015-tried-on-top-of-3014-and-not-kept)).

**Everything is on by default.** Nothing has to be switched on after `install.sh`: every path in the table below runs
on an ordinary install. A change that could go wrong on some machine has one way back, nearly always written `name=0`
(a boot argument or a line of the settings file); they are listed under
[What is on by default, and the ways back](#what-is-on-by-default-and-the-ways-back).

| Area | What the driver does today | Evidence / limits |
|---|---|---|
| Metal / desktop | AIR → SPIR-V → NVK/NAK. Surface fences, sleeping event waits, channel recovery, video memory for a screen share's captured frames granted without a fill from the processor, asynchronous texture uploads, a game's shaders translated ahead (3.0.1), and the plugin and translator changes of 3.0.4 to 3.0.6 | The RTX 5050 machine's desktop runs on it. The card checks of 3.0.7 ran 133 rows: 130 passed and 3 failed ([CHANGELOG.md](CHANGELOG.md), 3.0.7, "What ran where"). No change of 3.0.4 to 3.0.6 has been measured on its own. |
| OpenGL | Mesa's Zink on NVK is the OpenGL driver on macOS 15.5 or later; a context its guard refuses stays on Apple's OpenGL over Metal. A swap runs one frame ahead of the GPU (3.0.7), asks WindowServer for the window's opacity once and keeps the answer 100 ms (3.0.8), and with the swap interval at 0 leaves its frame to a sender thread instead of waiting for the display (3.0.11) | On the RTX 5050 with macOS 26.7.3 the OpenGL rows of the 3.0.7 card run went through Zink. One modded Minecraft pack, in captures whose scenes were not matched: 19 and 74 frames a second on 3.0.2 (Apple's renderer), 55 to 64 on 3.0.6 (Zink); started from a test boot loader with the write-combining table, 61 on 3.0.6 and 90 on 3.0.7. With 3.0.11 installed its owner read 490 on the game's own counter in Minecraft without mods on Zink; no capture was taken. |
| Vulkan for applications | A native Vulkan runtime for applications that select it when they are launched: NVK renders and each frame is copied on the GPU into the window, with no MoltenVK. It offers FIFO, IMMEDIATE and MAILBOX; an IMMEDIATE or MAILBOX swapchain shows the newest frame and does not hold the application to the display (3.0.9, 3.0.11) | Minecraft 26.3 without mods on the RTX 5050 with vertical sync off: 200 frames a second on the 100 Hz display through 3.0.8, in a capture that has its render thread waiting for the window. With 3.0.9 installed its owner read up to 1000 on the game's own counter; no capture was taken. |
| Write-combining on AMD machines | Where a kernel patch (AMD_Vanilla's "Fix PAT") leaves the CPU's page attribute table without a write-combining entry, NVRM puts the entry back at start and after each wake, so what the CPU writes into video memory is combined again (3.0.13). A machine whose table has the entry sees no write | 3.0.13 is not built on a Mac yet. The same table, set from a test boot loader on the RTX 5050 machine with 3.0.6: the share of a game's main thread in buffer uploads fell from 11-14 % to 1.4 %, and in importing the window's surface from 7-8 % to 1.2 %. Frames per second were not compared like for like. |
| DLSS / MetalFX | `install.sh` fetches NVIDIA's own DLSS files and builds their kernels for the card (3.0.13). With the files in place a Metal game's MetalFX temporal upscaler is served by DLSS Super Resolution and its MetalFX frame interpolator by DLSS Frame Generation, with no setting | RTX cards only; about 750 MB the first time. The architecture choice is measured on an RTX 5060 only: the Turing, Ampere and Ada rows have run on no card. |
| GPU capabilities | Read-only selector 14, ABI 1.0, a per-GPU cache refreshed on lifecycle changes | 15 verified / 150 unknown on one RTX 5050, manual authorization false. |
| GPU monitoring and power | `nvmtl-smi` and the separate NVMetrics app; RUSD telemetry; a machine-wide boost policy that a root process changes while applications run, so idle mode goes on and off without closing anything (3.0.1); experimental guarded fan control in NVMetrics | Do not treat observed RPM or a successful GET as general write support. |
| Sleep and wake | System sleep with video memory preserved; a display head that does not light at power-on is tried again (3.0.5) | [docs/SLEEP-WAKE.md](docs/SLEEP-WAKE.md). The retry was taken from an open pull request of the original project and has no card result of its own here. |
| Media | VideoToolbox decode and encode; in Firefox the video library and the plugin's surface imports work inside its sandbox (3.0.5) | Feature- and generation-dependent: no claim of full Windows, CUDA, DRM or Metal parity. No VP9 or AV1 hardware decode on RTX 50. |
| Shader caches | A shader-compiler switch that is set gets a cache key of its own, so it is never served a binary compiled under another setting (3.0.12) | Reproduced and checked on Linux; with no switch set the key is unchanged. 3.0.12 is installed on the RTX 5050 machine. |

See the [current status and evidence](docs/STATUS.md), [documentation index](docs/README.md),
[changelog](CHANGELOG.md), and [hardware test checklist](docs/TESTING-ON-HARDWARE.md).
Keep a tested rollback package before installing. Release 2.9 remains the inherited baseline for many fallback switches.

Four areas incorporate work from the HighDelay/nvidia-macos-driver-nvk fork. Their individual validation status
is documented in the linked pages; source integration is not evidence of end-to-end hardware success:

- **OpenGL through Zink.** On macOS 15.5 or later the driver routes OpenGL through Mesa's Zink on NVK instead of
  Apple's OpenGL over Metal, with no setting. The boot argument `nvzink=0` turns it off and gives every application
  Apple's OpenGL back. See [docs/ZINK-OPENGL.md](docs/ZINK-OPENGL.md).
- **A Vulkan runtime for Minecraft and other Vulkan applications.** The driver installs a native Vulkan runtime at
  `/Library/GPUBundles/nvmtl/minecraft`: rendering goes to NVK and each frame is copied on the GPU into the window,
  with no MoltenVK. It is installed and ready on every install; which Vulkan library an application loads is the
  application's own launch setting, so it is named there: Minecraft 26.3 with the JVM argument
  `-Dorg.lwjgl.vulkan.libname=/Library/GPUBundles/nvmtl/minecraft/lib/libnvk_minecraft.dylib`, MPV and other
  applications that use the Vulkan loader with
  `VK_DRIVER_FILES=/Library/GPUBundles/nvmtl/minecraft/nvk_bridge_icd.json`. With vertical sync off (an IMMEDIATE or
  MAILBOX swapchain) the runtime shows the newest finished frame and the application is not held to a multiple of the
  display's rate; `NVK_BRIDGE_PRESENT_NEWEST=0` in the settings file goes back to presenting every frame on the
  application's thread. See [tools/nvk-bridge/INSTALL.md](tools/nvk-bridge/INSTALL.md) and
  [tools/nvk-bridge/APPLICATIONS.md](tools/nvk-bridge/APPLICATIONS.md).
- **Shader translator repairs.** Argument-buffer textures, sampler arrays, buffer-address lowering, helper inlining and
  Safari's WebGPU textures.
- **Metal plugin fixes.** Spotlight's half-float icons, stale icons in native applications, Photo Booth's preview and
  the scheduled presents of drawables an application makes itself.

Building it: [docs/BUILDING.md](docs/BUILDING.md). What to check on a card, in order:
[docs/TESTING-ON-HARDWARE.md](docs/TESTING-ON-HARDWARE.md).

A Metal driver for NVIDIA GeForce cards on macOS 15 Sequoia (Intel Macs and OpenCore PCs). It is open source; the
kernel extensions, the Metal plugin and the shader translator are in this repository. The 1401 app that installs it
(Mac and Windows) is its own project: [1401](https://github.com/nullmoth/1401).

Supported cards are Turing and newer: GTX 16, RTX 20, 30, 40 and 50, TITAN RTX, Quadro RTX and the T-series. Most
testing of release 2.9 was done on an RTX 5060, so other cards may have problems that haven't been found yet. See
[card support](docs/CARD-SUPPORT.md) for what has been reported working.

- Changes: [CHANGELOG.md](CHANGELOG.md); [fork releases](https://github.com/TheFormidable/nvidia-macos-driver/releases) and [upstream releases](https://github.com/nullmoth/nvidia-macos-driver/releases) (different builds)
- Problems: [troubleshooting](docs/TROUBLESHOOTING.md), then report the exact fork revision to the [fork's issue tracker](https://github.com/TheFormidable/nvidia-macos-driver/issues)
- Installing macOS from Windows: [1401](https://github.com/nullmoth/1401)
- How it works: [docs/HOW-IT-WORKS.md](docs/HOW-IT-WORKS.md)
- Every switch and what it does: [docs/SWITCHES.md](docs/SWITCHES.md)
- Contributing: [CONTRIBUTING.md](CONTRIBUTING.md)

If your PC doesn't boot macOS with OpenCore yet, set that up first with the
[OpenCore Install Guide](https://dortania.github.io/OpenCore-Install-Guide/), then install the driver.

Support the work: https://buymeacoffee.com/nullmoth

| | |
|---|---|
| macOS | 15.5 Sequoia or later (2.9 was tested on 15.7.x and 15.8.1), or 26 Tahoe, x86_64 |
| GPUs | NVIDIA Turing and later; upstream development RTX 5060, scoped fork validation RTX 5050 |
| Metal | Metal 3: argument buffers tier 2, ray tracing, mesh shaders, MPS, MetalFX path |
| Also | OpenGL (Zink on NVK on macOS 15.5 or later; Apple's GL-on-Metal with `nvzink=0`), Vulkan for applications that select it, MetalFX served by DLSS on RTX cards, OpenCL, Core Image, Core ML |

## How it works

```
Metal app ─► NVMTLDriver.bundle ─► translator (Apple AIR → SPIR-V) ─► NVK (Mesa Vulkan + NAK compiler) ─► kexts ─► GPU
```

| Component | Path on disk | Source |
|---|---|---|
| Metal driver plugin | `/Library/GPUBundles/NVMTLDriver.bundle` | `plugin/` |
| Shader translator | inside the plugin (`libnvmtl_translate.dylib`) | `translator/` (LGPL-3.0, based on metal2vulkan) |
| Vulkan back end (NVK) | `/Library/GPUBundles/nvmtl/` | `nvk/nvk-macos.patch` and the chain in `build/build_nvk.sh` on Mesa `90a959eb` (26.3.0-rc3) |
| Kernel extensions | `/Library/Extensions/NVRM, NVAccel, NVRMFB, NVRMAGDC` | `kexts/` |
| Native OpenGL (Zink) | `/Library/GPUBundles/NVKZinkGLDriver.bundle`, `/Library/GPUBundles/nvmtl/zink` | `zink/` on Mesa 26.3.0 (`0a6fa60a`), `build/build_zink.sh` |
| Vulkan runtime for applications | `/Library/GPUBundles/nvmtl/minecraft` | `tools/nvk-bridge/` |
| GPU firmware (GSP) | `/Library/NullMoth/nvfw/nvidia/620.70.06` | NVIDIA, unmodified; fetched from NVIDIA's driver package at install (`package/fw_fetch.sh`) |

The kernel side runs NVIDIA's own open GPU kernel modules (620.70.06) under macOS. NVRMFB is the display framebuffer,
NVAccel the accelerator WindowServer composites through, NVRMAGDC the display-policy shim.

## What is on by default, and the ways back

Nothing in this driver has to be turned on. A change that replaced an older path keeps one way back to that path, so
that a problem can be pinned on it: set the way back, see whether the problem goes, and report both results. None of
them is needed on a Mac that works.

- A **boot argument** goes into OpenCore's `boot-args` and needs a restart.
- A **settings line** goes into your own block of `/Library/GPUBundles/nvmtl/nvrm610.conf` (between
  `# --- my settings, kept on update ---` and `# --- end my settings ---`), from the first column. It acts on the
  processes started after it: restart the application, or log out and in for WindowServer.

| On by default | Since | Way back | Kind |
|---|---|---|---|
| OpenGL through Zink on NVK (macOS 15.5 or later) | 3.0 | `nvzink=0`: Apple's OpenGL over Metal for every application | boot argument |
| Zink: a swap runs one frame ahead of the GPU; the window's video memory is imported once and kept | 3.0.7 | `NVK_ZINK_PRESENT_AHEAD=0`; `NVK_ZINK_SURFACE_KEEP=0` | settings line |
| Zink: the window's opacity is asked once per swap and the answer kept 100 ms | 3.0.8 | `NVK_ZINK_OPACITY_KEEP=0` | settings line |
| Zink: with the swap interval at 0 the newest frame is shown and the swap does not wait for the display | 3.0.11 | `NVK_ZINK_PRESENT_NEWEST=0` | settings line |
| Vulkan runtime: an IMMEDIATE swapchain shows the newest frame (3.0.9); MAILBOX is offered (3.0.11) | 3.0.9 | `NVK_BRIDGE_PRESENT_NEWEST=0` | settings line |
| Write-combining put back into the page attribute table where a kernel patch took it out | 3.0.13 | `nvpatwc=0` | boot argument |
| The installer fetches the DLSS files | 3.0.13 | `sudo env NULLMOTH_DLSS=0 ./install.sh` skips the step | installer |
| Read-only GPU capability cache | 3.0.3 | `nvgpucaps=0` | boot argument |
| Video memory for captured frames (screen sharing) granted without a second fill | 3.0 | `nvfbvramfast=0` | boot argument |
| New blocks of video memory cleared by the GPU's copy engine | 3.0.1 | `nvfbceclear=0` | boot argument |
| Asynchronous texture uploads outside WindowServer | 3.0.1 | `NVMTL_ASYNC_TEX_UPLOAD=0` | settings line |
| A game's shaders translated ahead | 3.0.1 | `NVMTL_PRETRANSLATE=0` | settings line |
| NVK's own write-only mapped memory in video memory, outside WindowServer | 3.0.1 | `NVK_LOCAL_MAP_SYSMEM=1` | settings line |
| Surface fences between processes | 3.0 | `nvrmfence=0` | boot argument |
| Waits for the GPU sleep on its event instead of polling | 3.0 | `NVRM_EVENT_WAIT=0` | settings line |
| A GPU fault on one channel rebuilds that channel | 3.0 | `NVK_CHANNEL_RECOVER=0` | settings line |
| The Metal plugin's changes of the performance audit (fifteen, one switch each) | 3.0.6 | [CHANGELOG.md](CHANGELOG.md), 3.0.6, "The ways back" | settings lines |
| Changes taken from pull requests to the original project (uniforms through a constant buffer, Firefox, whole-object views in the translator, the order of "scheduled") | 3.0.4, 3.0.5 | [CHANGELOG.md](CHANGELOG.md), 3.0.4 and 3.0.5, "The ways back" | settings lines |

The table holds the ways back most likely to be wanted. Every release's section of [CHANGELOG.md](CHANGELOG.md) has
its own list under "The ways back", and [docs/SWITCHES.md](docs/SWITCHES.md) lists every name the driver reads with
what is in effect when it is not set. To turn the whole driver off for one start, the boot argument `-nvoff` makes
NVRM leave the card alone ([docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md), "Getting back in").

## Install — by hand

For this fork, build the package using the instructions below; the upstream 1401 app is not a distribution channel
for an unsigned development build of this fork. `install.sh` installs the driver files but
does not change OpenCore, and the driver does not start until OpenCore has these settings:

- `boot-args` include `nvfb=1 nvaccel=1 nvfbheads=4 -nvkmsnosmooth amfi_get_out_of_my_way=0x1 amfi=0x80
  ipc_control_port_options=0`, and not `nv_disable=1` or `-wegnoegpu` (both hide the NVIDIA card)
- `csr-active-config` includes the bits `0x0A43`
- `Misc > Security > SecureBootModel` is `Disabled`
- `Lilu.kext` and `AMFIPass.kext` are in `Kernel > Add` and enabled

Download `nullmoth-nvidia-<version>.tar.gz` from **Releases**, then:

```bash
tar -xzf nullmoth-nvidia-*.tar.gz && cd pkgroot
shasum -a 256 -c SHA256SUMS
sudo ./install.sh
sudo shutdown -r now
```

Every file must say OK in the check. `install.sh` copies the files and rebuilds the Auxiliary Kernel Collection; the
restart is required, because logging out does not load the driver. It also downloads what the package may not carry,
from NVIDIA's own sites and checked against pinned hashes: the GPU firmware, and since 3.0.13 the DLSS files (about
750 MB the first time, nothing when the same versions are in place; a failure there leaves the driver installed, and
`sudo env NULLMOTH_DLSS=0 ./install.sh` skips the step). (Paste the lines without anything after them: the
Terminal on macOS does not treat `#` as a comment, so a pasted comment becomes part of the command.)

macOS asks you to **allow the extensions** in System Settings → Privacy & Security the first time. Allow, then reboot again.

## Upstream 1401 Mac app

Download the latest `1401-Mac-<version>.dmg` from **Releases**, open it, and run **1401** (the driver package is inside the disk
image, so nothing else to download). Follow its steps. A supported setup must use a verified or explicitly selected OpenCore partition. The app finds the OpenCore that started your Mac (in `EFI/OC` or `EFI/BOOT`, on an
EFI or FAT32 partition), shows every change before making it, backs the config up, installs the driver, and adds
**1401: Remove NVIDIA driver** to the OpenCore boot picker. Choosing that entry removes the driver at the next start and
puts the Mac back exactly as it was before the install, OpenCore config included, then restarts by itself. The app also
maps your USB ports and, if the driver ever crashes the Mac, offers to make a crash report you can upload yourself
(it never sends anything on its own).

If something goes wrong, open 1401 > Crash report > Send logs to NullMoth. It uploads what 1401 did, the driver's
state, crash reports and OpenCore's startup log, with names, serial numbers and addresses removed, and gives you a
report ID to put in your issue or in Discord.

Keep the USB stick or disk that OpenCore started from plugged in while the app runs, and for every restart after:
that is the config the app changes. The app never copies OpenCore to your internal disk. The first start with the
driver pauses for up to a minute at "PCI configuration end" while the GPU comes up.

## Wiring into an existing OpenCore setup

The kexts install into `/Library/Extensions` and load from the Auxiliary Kernel Collection — **do not** also inject
them from `EFI/OC/Kexts`. OpenCore only needs to set SIP and boot-args.

**`NVRAM → Add → 7C436110-AB2A-4BBB-A880-FE41995C9F82`**

| Key | Value | Why |
|---|---|---|
| `csr-active-config` | `<430A0000>` (Data) | the value tested with release 2.9: unsigned kexts, plus what root patches need |
| `boot-args` | `nvfb=1 nvaccel=1 nvfbheads=4 -nvkmsnosmooth amfi_get_out_of_my_way=0x1 amfi=0x80 ipc_control_port_options=0` | framebuffer + accelerator, 4 display heads; the AMFI args let WindowServer load the driver bundle |

Optional boot arguments, for troubleshooting:

| boot-arg | Effect |
|---|---|
| `-nvrmnoflip` | keeps the composited frame on the copy path: NVRM never turns on zero-copy scan-out (`debug.nvaccel_iop_flip`), so the display reads a finished copy instead of the surface the GPU may still be drawing. Try it first when the desktop flashes or tears while the cursor moves. |
| `-nvrmnogo` | the driver idles until `nvrmctl go <pass>` instead of starting the GPU itself. Where no other GPU has a display, two starts in a row with it that never run `nvrmctl go` turn the driver off at the next start: the boot-guard job keeps the count of a start in which NVRM claimed the card and nothing was lit. |
| `-nvrmnobootscreen` | do not snapshot the boot screen. |
| `-nvrmnopm` | turns off system sleep support: NVRM stays out of the power tree, as before it had one. A Mac that sleeps then comes back without the GPU. See [docs/SLEEP-WAKE.md](docs/SLEEP-WAKE.md). |

None of these is needed on a Mac that works. Every boot argument, environment name and sysctl the driver reads is
listed in [docs/SWITCHES.md](docs/SWITCHES.md), with what is in effect when it is not set. To see what a Mac has set
beside the `boot-args` line above:

```bash
sysctl debug.nvrm_switches debug.nvrm_switches_nondefault
```

The first lists the names; a `!` marks one an ordinary install does not need (an opt-out of a default path, a test
hook, a development switch, a name this version no longer reads). The second is 0 on an ordinary install.

Add every key you set to `NVRAM → Delete` as well, so the values are rewritten each boot.

| Setting | Value | Why |
|---|---|---|
| `UEFI → Quirks → ResizeGpuBars` | keep the existing value | Forced resizing can stop firmware before the picker; `-1` preserves firmware sizing |
| `Booter → Quirks → ResizeAppleGpuBars` | keep the existing value | Large firmware BARs can stop PCI configuration; `0` is the installer-safe small-BAR setting |
| `Kernel → Block` | `com.apple.iokit.IONDRVSupport`, Strategy `Exclude` | otherwise the firmware framebuffer takes display index 0 from NVRMFB |
| `Misc → Security → SecureBootModel` | `Disabled` | Apple Secure Boot refuses kexts Apple did not sign |

The macOS **installer** uses `ResizeAppleGpuBars` `0`, `ResizeGpuBars` `-1`, and IONDRVSupport
not excluded: it runs on the firmware framebuffer. The Mac app keeps your existing BAR settings when it installs or
updates the driver, and the driver sets up the BAR itself after macOS starts, so don't copy BAR values from someone
else's board.

An installed macOS whose OpenCore does not exclude IONDRVSupport also runs the driver: its framebuffer kext claims the
NVIDIA card before Apple's firmware framebuffer can. The difference shows when the driver does not draw the card,
mainly when it declined this macOS: Apple's firmware framebuffer then shows the firmware's picture as a slow desktop
without acceleration (when that picture still lies inside the card's memory window), where the table's setting leaves
the screen dark ([docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md), the layout-refusal entry).

Also required:
- **BIOS:** Above 4G Decoding ON (the card maps memory above 4 GB), CSM OFF.
- **SMBIOS:** a Mac model that runs macOS 15 with a discrete GPU (tested with release 2.9: `iMacPro1,1`, which 1401 uses).
- **Remove** any `nv_disable=1`, WhateverGreen NVIDIA patches, or `agdpmod=pikera` for this card.
- **With WhateverGreen installed** (for an Intel or AMD GPU), add `gfxrst=4` to boot-args. Without it, WhateverGreen's
  boot screen copy can read freed memory when the NVIDIA display opens, and the Mac panics.
- **No `DeviceProperties`** are needed for the NVIDIA card.
- **Sound on macOS 26: remove `alcid=…` and `alcverbs` from boot-args.** macOS 26 has no AppleHDA, so AppleALC loads
  and does nothing, and while either argument is set the installer's audio driver (`com.nullmoth.NMXAudio`) leaves the
  onboard sound to AppleALC: the Mac shows only the NVIDIA HDMI/DisplayPort outputs, with no speakers, headphone jack
  or microphone. Its boot log says so: `log show --last boot --style compact --predicate 'senderImagePath CONTAINS[c]
  "NMXAudio"'` prints `leaving <vendor:device> alone: another codec stack is switched on (alcid/alcverbs)`. Edit the
  `boot-args` in `config.plist` (OpenCore rewrites them each boot, so `sudo nvram` is not enough) and restart. AppleALC
  can stay; keep `alcid` only if you restored AppleHDA yourself (SimpleHDA, a root patch). The installer prints a note
  when it finds either argument.

Verify after the reboot:

```bash
kmutil showloaded --list-only | grep nullmoth
system_profiler SPDisplaysDataType | head -20
```

The first shows 4 lines (5 on macOS 26 with an HD Audio controller, where the installer adds its audio driver,
`com.nullmoth.NMXAudio`); the second shows your GeForce with Metal: supported.

## Dual boot

Nothing in the driver touches other disks. OpenCore's picker boots Windows (`ScanPolicy` 0 or including NTFS) and Linux
(`OpenLinuxBoot.efi` in `UEFI → Drivers`, `LauncherOption = Full`) alongside macOS.

## Uninstall

```bash
sudo ./uninstall.sh && sudo shutdown -r now
```

## Build from source

On a Mac with macOS 15.6 or later and Xcode 26 or later (the full Xcode, not only the command line tools; the whole
driver has been built with Xcode 26.3 (SDK 26.2), the newest Xcode that runs on macOS 15, and with Xcode 26.5, not yet
with Xcode 26.0 to 26.2), clone this fork's development branch:

```bash
git clone -b fable https://github.com/TheFormidable/nvidia-macos-driver.git
cd nvidia-macos-driver
./build.sh doctor
./build.sh
```

`./build.sh doctor` says what this Mac has, lacks or has too old. `./build.sh` examines the Mac and, after asking,
gets the build tools it lacks: on an Intel Mac it builds them from pinned sources into its working directory and calls
no Homebrew, even when Homebrew is installed; on Apple silicon it installs the missing Homebrew formulas with the
Homebrew of the Mac's own processor (only those, and what Homebrew installs for them; `build.sh` upgrades and removes
nothing, though Homebrew may upgrade an outdated dependency of a formula it installs). It makes a Python environment
and a Rust toolchain inside its working directory (`~/nullmoth-build` by default, never inside the checkout),
downloads NVIDIA's `open-gpu-kernel-modules` 620.70.06 and 610.57.04, Mesa 26.3.0-rc3, the Vulkan loader and the other
pinned sources, checks each against the commit id or SHA-256 in `build/pins.sh`, and builds the kexts, NVK, the
plugin, the translator, the video library and the tools. Each part is a step with its own log; a step whose inputs did
not change is skipped the next time. It never calls `sudo` and never installs the driver: at the end it prints the
`install.sh` command to run yourself. After a whole build and package on the Intel Mac named at the top, nothing
outside the working directory had changed (Homebrew's folders, the shell's settings files, the user's own Rust) and no
file it made named the user's home folder.

On an Intel Mac the first `./build.sh` builds the missing build tools (LLVM 22 above all) from pinned sources. From an
empty working directory `deps` took 46.5 minutes on that Intel Mac (building LLVM about 30 of them) and `fetch` 40
seconds there, and `deps` took 3 hours 53 minutes on GitHub's Intel runner; it needs 10 GB of free space while LLVM
builds. In a later run on that Mac with those tools kept, `deps` took 49 seconds, the build of all 17 components 5
minutes 41 seconds and the package 27 seconds, and the working directory then held about 8 GB. On Apple silicon `deps`
installs Homebrew's ready-made tools instead. That path has run only against a stand-in for Homebrew: no whole build
with `build.sh` has run on an Apple-silicon Mac. GitHub's Apple-silicon runner compiles and links the kexts (at
commit 95811c2 they had the same bytes as the kexts built on GitHub's Intel runner), and at commit ee7f332 the host
tests passed whole there for the first time (304 passed, 0 failed, 3 skipped, 9 not run, of 316).

To make an installable package, give it a release package as the base (some carried files have no source in this
repository, so a package is the release's with every part this checkout builds replaced):

```bash
./build.sh --base-package ~/Downloads/nullmoth-nvidia-2.9.tar.gz package
```

Such a package has been staged on both Intel Macs above, and one made at commit aa27c73 on the first of them has been
installed and started on one machine with an RTX 5050. The packages of 3.0.6 to 3.0.12 were since built on that
machine itself and installed there ([docs/STATUS.md](docs/STATUS.md)).

`./build.sh help` lists the commands, components and options; `./build.sh env` prints every variable the build
scripts read, to run one of them by hand. [docs/BUILDING.md](docs/BUILDING.md) explains everything, including what
to do when a step fails ([en español](docs/BUILDING.es.md)).

`build.sh` was written on Linux and tested there against stand-ins for Xcode, Homebrew and the build scripts;
[docs/BUILDING.md](docs/BUILDING.md) says which of its paths have since run on the two Intel Macs above and which only
against the stand-ins. A step can still stop on something neither the stand-ins nor those two Macs showed (another
Xcode or SDK, a formula name, a compiler warning). When a step stops, the message names it and gives a report file to
attach to an issue.

Rebuild both accelerators whenever `nvrm_vram_abi.h` or `nvrm_gpuva_abi.h` changes: an accelerator built against an
older ABI loads, but NVRMFB refuses all its VRAM requests and the desktop stays black. `./build.sh` does this by
itself (the accel step's inputs include kexts/NVRM).

Two checks need no NVIDIA hardware and no built RM archive:

```bash
bash build/kext_check.sh
python3 tools/run_all_tests.py
```

The first compiles every kext source without linking and checks every kernel symbol the kexts import against the
export lists of macOS 15 and macOS 26; it runs on a Mac or on Linux (clang, llvm-nm, python3, git, make, patch) and
downloads its pinned inputs into `build-out/` on the first run. The second runs the host tests (on a Mac; on Linux
the tests that need a Mac fail, among them every test that compiles Objective-C). `.github/workflows/ci.yml` runs
both, and the kext and plugin builds, on every push once Actions are enabled for the repository.
`.github/workflows/full-build.yml` runs by hand (Actions, full-build, Run workflow): on GitHub's Intel runner it builds
the whole driver with `./build.sh` in a new working directory (its tools and sources restored from GitHub's cache when
it has them), stages a package over the 2.9 release package, runs the host tests and checks that nothing was taken
from Homebrew. One run takes between one and four and a half hours of GitHub's Intel Mac; at commit ee7f332 every step
of it succeeded.

A change that adds a switch (an environment name, a boot argument, a sysctl) needs a row in `tools/switches.tsv`;
`python3 tools/switch_registry.py check` says which, and what a new switch may be.

## License

Free of charge, source code included. Nobody may sell it or use it to make money.

- `plugin/`, `kexts/`, `build/`, `package/`: PolyForm Noncommercial 1.0.0 (see `LICENSE`), © NullMoth Systems,
  except the files of `build/` named in the next item. Personal, research, educational and non-profit use is allowed;
  any commercial use is not.
- `zink/`, `build/build_zink.sh`, `build/opengl_api.py`, `tools/gld_bundle_probe.c`: MIT (`licenses/Dino-Zink-MIT.txt`),
  from the HighDelay/nvidia-macos-driver-nvk fork; Zink itself is Mesa (MIT). `build/prepare_zink.py` and
  `build/generate_gld_forward.py` come from the same fork, where commit eaf7d128 added them together with
  `build/build_zink.sh`; neither carries a licence line of its own, and that fork's `docs/ZINK-OPENGL.md` says of the
  contribution eaf7d128 is part of: "its original MIT attribution is retained". `tools/nvk-bridge/`: PolyForm
  Noncommercial 1.0.0, from the same fork.
- `translator/`: LGPL-3.0-or-later (see `translator/LICENSE`): it is based on metal2vulkan, whose licence carries over.
- `nvk/`: changes to Mesa (MIT).
- GSP firmware: NVIDIA's redistributable firmware licence.

See `NOTICE` for third-party credits.

This fork preserves the original component licenses and upstream credits; it is not an official NullMoth release.
