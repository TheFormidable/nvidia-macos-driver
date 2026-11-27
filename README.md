# NVIDIA driver for macOS

Metal, OpenGL and Vulkan acceleration for NVIDIA GeForce cards (Turing and newer) on Intel Macs and OpenCore PCs. This
is TheFormidable's fork of [NullMoth](https://github.com/nullmoth/nvidia-macos-driver). It adds work from HighDelay's
NVK fork and pairs with the companion [NVMetrics](https://github.com/TheFormidable/NVMetrics) app. The kernel
extensions, the Metal plugin, the shader translator, NVK and the installer are all in this repository.

> **Release 2 — 3.0.20 (pre-release, November 2026).** One package installs on every macOS from 10.7 Lion to 26
> Tahoe: the installer picks the part for the release it runs on.
>
> - **Tested on one machine only:** an AMD Ryzen 5 5500 hackintosh with an RTX 5050 on macOS 26.7.3.
> - **macOS 15** has not been tried with this fork.
> - **macOS 10.7 to 14:** the kits have never run on a card.
>
> What is new since release 1 (3.0.2) is in [docs/releases/RELEASE-2.md](docs/releases/RELEASE-2.md). Every change
> is in [CHANGELOG.md](CHANGELOG.md). The evidence, version by version, is in [docs/STATUS.md](docs/STATUS.md).

**Contents:** [What it does](#what-it-does) · [Requirements](#requirements) · [Install](#install) ·
[What is on by default, and the ways back](#what-is-on-by-default-and-the-ways-back) ·
[If something goes wrong](#if-something-goes-wrong) · [Build from source](#build-from-source) ·
[How it works](#how-it-works) · [Documentation](#documentation) · [Credits and license](#credits-and-license)

## What it does

Everything below is on after `install.sh`; nothing has to be switched on. The figures are readings from the test
machine, mostly from each game's own counter. The scenes were not always the same, so they are not like-for-like
benchmarks.

| Area | What the driver does | On the test machine |
|---|---|---|
| Metal | Apple AIR → SPIR-V → NVK with the NAK compiler, through the `NVMTLDriver.bundle` plugin. Also: surface fences between processes, sleeping event waits, channel recovery after a GPU fault, asynchronous texture uploads, and a game's shaders translated ahead | Runs the desktop. `system_profiler` reports the card with Metal 4 on macOS 26 |
| OpenGL | Mesa's Zink on NVK, on macOS 15.5 or later. A swap runs one frame ahead of the GPU. With vsync off, the newest frame is shown without waiting for the display. Buffer uploads that would wait go through a staging buffer | A heavily modded Minecraft pack: 19-74 fps on release 1, 123-290 fps on 3.0.12. Minecraft without mods: 490 fps |
| Vulkan for applications | A native Vulkan runtime for applications that select it, with no MoltenVK. It offers FIFO, IMMEDIATE and MAILBOX, and IMMEDIATE and MAILBOX show the newest frame. NVK reports Vulkan 1.4.363 (Mesa 26.3.0); it moves with Mesa 27 | Minecraft 26.3, vsync off: 200 fps on release 1, up to 1000 fps on 3.0.9 |
| GPU work | NVK uses the GPU's hierarchical Z (ZCULL). On AMD machines with a "Fix PAT" kernel patch, write-combining is put back into the CPU's page attribute table | ZCULL is not measured yet. With write-combining in place, the game thread's share in buffer uploads fell from 11-14 % to 1.4 % |
| DLSS / MetalFX | The installer fetches NVIDIA's DLSS files and builds their kernels for the card. A Metal game's MetalFX upscaler and frame interpolator are then served by DLSS Super Resolution and Frame Generation | RTX cards only; about 750 MB the first time |
| Screen sharing | Captured frames get video memory without a second fill. A program that reads them with the processor (Firefox, OBS, Zoom) no longer holds WindowServer behind the copy engine | Sharing a window from Firefox: WindowServer's waits fell by 72 % (3.0.20) |
| Older macOS | The kits for 10.7 to 14 ride inside the package, built by this fork with Apple's SDK and Kernel Debug Kit for each release | Never run on a card |
| Monitoring and power | `nvmtl-smi`, the NVMetrics app, RUSD telemetry, a machine-wide boost policy, system sleep with video memory kept, and a read-only GPU capability cache | Fan control in NVMetrics is experimental and guarded |
| Media | VideoToolbox decode and encode, and video in Firefox's sandbox | No VP9 or AV1 hardware decode on RTX 50 |

Known limits:

- **Safari:** Safari's GPU process cannot load the plugin because of WebKit's sandbox, so WebGL and WebGPU in Safari
  draw in software. Chromium browsers and Firefox use the GPU.
- **Google Meet:** Meet asks 5 frames a second of Firefox and Safari when sharing, whatever the driver does
  ([troubleshooting](docs/TROUBLESHOOTING.md)).
- **No CUDA.**

## Requirements

| | |
|---|---|
| Card | NVIDIA Turing or newer: GTX 16, RTX 20, 30, 40 and 50, TITAN RTX, Quadro RTX and the T series ([card support](docs/CARD-SUPPORT.md)). This fork has run on an RTX 5050 only |
| macOS | 15.5 Sequoia or later, or 26 Tahoe, x86_64. The kits for 10.7 to 14 are experimental and untested |
| Machine | An Intel Mac or a PC that already boots macOS with OpenCore ([OpenCore Install Guide](https://dortania.github.io/OpenCore-Install-Guide/)). BIOS: Above 4G Decoding on, CSM off |

## Install

### OpenCore settings

The kexts install into `/Library/Extensions` and load from the Auxiliary Kernel Collection: **do not** also inject them
from `EFI/OC/Kexts`. `install.sh` does not change OpenCore. The driver starts only once OpenCore has these settings.

**`NVRAM → Add → 7C436110-AB2A-4BBB-A880-FE41995C9F82`** (add each key to `NVRAM → Delete` too, so it is rewritten at
every boot):

| Key | Value | Why |
|---|---|---|
| `boot-args` | `nvfb=1 nvaccel=1 nvfbheads=4 -nvkmsnosmooth amfi_get_out_of_my_way=0x1 amfi=0x80 ipc_control_port_options=0` | The framebuffer and the accelerator, 4 display heads. The two AMFI arguments let WindowServer load the Metal plugin; to keep AMFI on instead, see [AMFI left on](#amfi-left-on-optional) |
| `csr-active-config` | `<430A0000>` (Data, bits `0x0A43`) | Unsigned kexts, and root may record the kexts' approval |

| Setting | Value | Why |
|---|---|---|
| `Kernel → Add` | `Lilu.kext` and `AMFIPass.kext`, enabled | Required |
| `Kernel → Block` | `com.apple.iokit.IONDRVSupport`, Strategy `Exclude` | Otherwise the firmware framebuffer takes display index 0 from NVRMFB |
| `Misc → Security → SecureBootModel` | `Disabled` | Apple Secure Boot refuses kexts Apple did not sign |
| `UEFI → Quirks → ResizeGpuBars`, `Booter → Quirks → ResizeAppleGpuBars` | keep your existing values | The driver sets up the BAR itself after macOS starts; do not copy BAR values from another board |

Also:

- **Remove** `nv_disable=1`, `-wegnoegpu`, WhateverGreen's NVIDIA patches and `agdpmod=pikera` for this card. No
  `DeviceProperties` are needed for it.
- **SMBIOS:** a Mac model that runs your macOS with a discrete GPU (`iMacPro1,1` on the test machine).
- **With WhateverGreen** (for an Intel or AMD GPU), add `gfxrst=4` to boot-args: without it, WhateverGreen's boot
  screen copy can panic the Mac when the NVIDIA display opens. The installer adds it to the NVRAM boot-args when
  WhateverGreen is loaded and none is set, but an OpenCore that writes its own boot-args at every start replaces that.
- **On macOS 26, remove `alcid=…` and `alcverbs`.** macOS 26 has no AppleHDA, and while either is set the driver's
  audio kext leaves the onboard sound to AppleALC, so there are no speakers, headphone jack or microphone. Keep them
  only if you restored AppleHDA yourself. The installer prints a note when it sees them.

The macOS *installer* runs on the firmware framebuffer: there, use `ResizeAppleGpuBars` `0`, `ResizeGpuBars` `-1`, and
do not exclude IONDRVSupport.

### Install the package

Download the package (`nullmoth-nvidia-3.0.20.tar.gz` for release 2) from this fork's
[Releases](https://github.com/TheFormidable/nvidia-macos-driver/releases), then in Terminal:

```bash
tar -xzf nullmoth-nvidia-3.0.20.tar.gz && cd pkgroot
shasum -a 256 -c SHA256SUMS
sudo env CHECK=1 ./install.sh
sudo ./install.sh && sudo shutdown -r now
```

- Every file must say OK in the check.
- `CHECK=1` rehearses the install and changes nothing: go on only when it ends with `CHECK PASS`.
- `install.sh` copies the files, rebuilds the Auxiliary Kernel Collection and records the kexts' approval in macOS's
  kext policy database, so **one restart is enough**, with no **Allow** in System Settings → Privacy & Security. If
  SIP keeps that database read-only, macOS asks the first time instead: allow the extensions there, then restart
  again. The installer's last message says which happened. Logging out does not load the driver.
- It also fetches what the package does not carry, from NVIDIA's own sites and checked against pinned hashes: the GPU
  firmware, and the DLSS files (about 750 MB the first time). `sudo env NULLMOTH_DLSS=0 ./install.sh` skips DLSS.
- Paste the lines without anything after them: Terminal does not treat `#` as a comment.

The first start with the driver can pause for up to a minute at "PCI configuration end" while the GPU comes up. The
first launch of each game is slower: NVK compiles its shaders again once for every new version.

### Check that it worked

```bash
kmutil showloaded --list-only | grep nullmoth
system_profiler SPDisplaysDataType | head -20
cat /Library/NullMoth/driver-version
```

- The first command shows 4 lines (5 on macOS 26 with an HD Audio controller, where the installer adds its audio
  driver).
- The second shows your GeForce with Metal support.
- The third shows the installed version.

### AMFI left on (optional)

The two AMFI boot arguments turn off code-signing enforcement for the whole system. There is an alternative:

1. Create your own CA on your Mac (`tools/make_amfi_anchor.sh`). The keys are written outside any git checkout and
   never leave your Mac.
2. Sign the graphics stack with it (`tools/sign_graphics.sh`): NVMTLDriver.bundle, NVKZinkGLDriver.bundle and the
   nvmtl libraries.
3. Register the CA in OpenCore as an [AMFIAnchor](https://github.com/b00t0x/AMFIAnchor) anchor: `Lilu.kext`, then
   `AMFIAnchor.kext`, then the anchor kext the script made.

Release 2 makes steps 1 and 2 optional: its package comes with the graphics stack signed with this fork's CA, and the
matching anchor kext, `NVAnchorInjector.kext.zip`, is on the
[release page](https://github.com/TheFormidable/nvidia-macos-driver/releases/tag/3.0.20) with the OpenCore steps.
Install that anchor only if you trust this fork's signer: AMFI then accepts anything signed with its key, not only this
driver. Make your own CA to trust only yourself.

WindowServer then loads the plugin with AMFI enforcing. On the test machine this worked from its first boot, the GPU
reported Metal 4, and 3.0.20 ran signed that way. It is still an **experiment**: one machine, and App Store, iMessage,
and repeated sleep and wake are not qualified. Keep an EFI with the AMFI arguments to go back to. The steps are in
[docs/SIGNED-GRAPHICS-EXPERIMENT.md](docs/SIGNED-GRAPHICS-EXPERIMENT.md), and the background is in
[docs/AMFI-COMPATIBILITY.md](docs/AMFI-COMPATIBILITY.md).

### macOS 10.7 to 14

Use the same package and the same commands. `install.sh` reads the macOS version, and `package/pick_kit.sh` names the
kit for it. The installer copies that kit to a root-owned folder, checks every file against the package's checksums,
and runs the kit's own installer, which prints the boot arguments it needs. What each kit carries:

- **10.7 to 10.11:** OpenGL only.
- **10.12 and later:** Metal.
- **10.13 and 10.14:** the Metal desktop needs one of two choices described in the kit's `options/`.

The [release notes](docs/releases/RELEASE-2.md#one-package-for-every-macos-3019) have the full table. Before 10.11, a
GPU behind VT-d is refused at the driver's self-test. **No kit has run on a card yet.**

### Dual boot and uninstall

Nothing in the driver touches other disks: OpenCore's picker boots Windows and Linux as before. To remove the driver,
run this from the package folder:

```bash
sudo ./uninstall.sh && sudo shutdown -r now
```

On 10.7 to 14 it runs the kit's own remover.

## What is on by default, and the ways back

Nothing in this driver has to be turned on. A change that replaced an older path keeps one way back to that path, so
that a problem can be pinned on it: set the way back, see whether the problem goes, and report both results. None of
them is needed on a Mac that works.

- A **boot argument** goes into OpenCore's `boot-args` and needs a restart.
- A **settings line** goes into your own block of `/Library/GPUBundles/nvmtl/nvrm610.conf`, between
  `# --- my settings, kept on update ---` and `# --- end my settings ---`, from the first column. It acts on the
  processes started after it: restart the application, or log out and in for WindowServer.

| On by default | Since | Way back | Kind |
|---|---|---|---|
| A page-off reads video memory with the copy engine without holding the accelerator lock (screen sharing) | 3.0.20 | `nvaccel_pageoff_copy_locked=1` | boot argument |
| The installer records the kexts' approval (no manual Allow) | 3.0.20 | none needed: where SIP keeps the database read-only, macOS asks as before | installer |
| Hierarchical Z (ZCULL): hidden fragments culled before they are shaded, outside WindowServer | 3.0.17 | `NVK_ZCULL=0` | settings line |
| Zink: a glBufferSubData that would wait for the GPU goes through a staging buffer | 3.0.16 | `NVK_ZINK_SUBDATA_STAGE=0` | settings line |
| Write-combining put back into the page attribute table where a kernel patch took it out | 3.0.13 | `nvpatwc=0` | boot argument |
| The installer fetches the DLSS files | 3.0.13 | `sudo env NULLMOTH_DLSS=0 ./install.sh` skips the step | installer |
| Zink: with the swap interval at 0 the newest frame is shown and the swap does not wait for the display | 3.0.11 | `NVK_ZINK_PRESENT_NEWEST=0` | settings line |
| Vulkan runtime: an IMMEDIATE swapchain shows the newest frame (3.0.9); MAILBOX is offered (3.0.11) | 3.0.9 | `NVK_BRIDGE_PRESENT_NEWEST=0` | settings line |
| Zink: the window's opacity is asked once and the answer kept 100 ms | 3.0.8 | `NVK_ZINK_OPACITY_KEEP=0` | settings line |
| Zink: a swap runs one frame ahead of the GPU; the window's video memory is imported once and kept | 3.0.7 | `NVK_ZINK_PRESENT_AHEAD=0`; `NVK_ZINK_SURFACE_KEEP=0` | settings line |
| The Metal plugin's changes of the performance audit (fifteen, one switch each) | 3.0.6 | [CHANGELOG.md](CHANGELOG.md), 3.0.6, "The ways back" | settings lines |
| Changes taken from pull requests to the original project (uniforms through a constant buffer, Firefox, whole-object views in the translator, the order of "scheduled") | 3.0.4, 3.0.5 | [CHANGELOG.md](CHANGELOG.md), 3.0.4 and 3.0.5, "The ways back" | settings lines |
| Read-only GPU capability cache | 3.0.3 | `nvgpucaps=0` | boot argument |
| New blocks of video memory cleared by the GPU's copy engine | 3.0.1 | `nvfbceclear=0` | boot argument |
| Asynchronous texture uploads outside WindowServer | 3.0.1 | `NVMTL_ASYNC_TEX_UPLOAD=0` | settings line |
| A game's shaders translated ahead | 3.0.1 | `NVMTL_PRETRANSLATE=0` | settings line |
| NVK's own write-only mapped memory in video memory, outside WindowServer | 3.0.1 | `NVK_LOCAL_MAP_SYSMEM=1` | settings line |
| OpenGL through Zink on NVK (macOS 15.5 or later) | 3.0 | `nvzink=0`: Apple's OpenGL over Metal for every application | boot argument |
| Video memory for captured frames (screen sharing) granted without a second fill | 3.0 | `nvfbvramfast=0` | boot argument |
| Surface fences between processes | 3.0 | `nvrmfence=0` | boot argument |
| Waits for the GPU sleep on its event instead of polling | 3.0 | `NVRM_EVENT_WAIT=0` | settings line |
| A GPU fault on one channel rebuilds that channel | 3.0 | `NVK_CHANNEL_RECOVER=0` | settings line |

The table holds the ways back most likely to be wanted. Every version's section of [CHANGELOG.md](CHANGELOG.md) has
its own list under "The ways back". [docs/SWITCHES.md](docs/SWITCHES.md) lists every name the driver reads, with what
is in effect when it is not set. To see what this Mac has set beyond the install line:

```bash
sysctl debug.nvrm_switches debug.nvrm_switches_nondefault
```

The first lists the names; a `!` marks one an ordinary install does not need. The second is 0 on an ordinary install.

## If something goes wrong

- **The Mac does not reach the desktop:** add `-nvoff` to the boot-args in OpenCore's `config.plist` (keep a USB
  stick or a second EFI with it ready). NVRM then leaves the card alone. On 15 and 26, if two boots in a row do not light the display, the
  driver brings back the previous installed version by itself on the third.
- **Something is wrong after a change:** try that change's way back (above), then report both results.
- **Hangs or freezes right at boot or after sleep on an AMD machine:** add `nvpatwc=0` (the PAT change of 3.0.13).
- **Flashing or tearing while the cursor moves:** try `-nvrmnoflip`, which keeps the composited frame on the copy path.
- [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) covers the rest, starting with "Getting back in".
- **Report a problem** on the [fork's issue tracker](https://github.com/TheFormidable/nvidia-macos-driver/issues).
  Include the version (`cat /Library/NullMoth/driver-version`), the macOS build, the card, your boot-args, the output
  of `install.sh`, and `sysctl debug.nvrm_switches debug.nvrm_switches_nondefault`.

Other troubleshooting boot arguments:

- `-nvrmnogo`: the driver idles until `nvrmctl go <pass>`.
- `-nvrmnobootscreen`: no boot-screen snapshot.
- `-nvrmnopm`: no system-sleep support ([docs/SLEEP-WAKE.md](docs/SLEEP-WAKE.md)).

## Build from source

On a Mac with macOS 15.6 or later and the full Xcode 26 (not only the command line tools):

```bash
git clone -b fable https://github.com/TheFormidable/nvidia-macos-driver.git
cd nvidia-macos-driver
./build.sh doctor
./build.sh
./build.sh --base-package ~/Downloads/nullmoth-nvidia-2.9.tar.gz package
```

- `doctor` says what the Mac has, lacks or has too old.
- `./build.sh` gets the missing build tools after asking, fetches the pinned sources (`build/pins.sh`, every one
  checked by commit or SHA-256) and builds all 19 components. On an Intel Mac it builds the tools from source into its
  working directory (`~/nullmoth-build`), with no Homebrew.
- It never calls `sudo` and never installs the driver.
- `package` stages an installable package over the original's 2.9 release package, which carries a few files that have
  no source here. Add `NM_LEGACY_KITS=<folder>` to put the 13 kits for macOS 10.7 to 14 inside it.

[docs/BUILDING.md](docs/BUILDING.md) explains every command, option and component, how to build the kits for older
macOS and how to sign the graphics stack ([en español](docs/BUILDING.es.md)). [DEVELOPING.md](DEVELOPING.md) covers
the host tests (`python3 tools/run_all_tests.py`) and the kernel-symbol check (`bash build/kext_check.sh`), which need
no NVIDIA card.

## How it works

```
Metal app ─► NVMTLDriver.bundle ─► translator (Apple AIR → SPIR-V) ─► NVK (Mesa Vulkan + NAK compiler) ─► kexts ─► GPU
OpenGL app ─► NVKZinkGLDriver.bundle (Mesa Zink) ─► NVK ─► kexts ─► GPU
Vulkan app ─► Khronos loader ─► NVK through tools/nvk-bridge ─► kexts ─► GPU
```

| Component | Installed at | Source |
|---|---|---|
| Metal driver plugin | `/Library/GPUBundles/NVMTLDriver.bundle` | `plugin/` |
| Shader translator | inside the plugin (`libnvmtl_translate.dylib`) | `translator/` (based on metal2vulkan) |
| Vulkan back end (NVK and NAK) | `/Library/GPUBundles/nvmtl/` | the patch chain in `build/build_nvk.sh` on Mesa `90a959eb` (26.3.0-rc3, `nvk/MESA_BASE`) |
| Native OpenGL (Zink) | `/Library/GPUBundles/NVKZinkGLDriver.bundle`, `/Library/GPUBundles/nvmtl/zink` | `zink/` on Mesa 26.3.0, `build/build_zink.sh` |
| Vulkan runtime for applications | `/Library/GPUBundles/nvmtl/minecraft` | `tools/nvk-bridge/` |
| Kernel extensions | `/Library/Extensions/` NVRM, NVAccel, NVRMFB, NVRMAGDC | `kexts/` |
| Kits for macOS 10.7 to 14 | `legacy/<release>/` in the package; on those releases the kit's own installer puts it in place | `build/legacy/`, `package/legacy/`, `gl/` |
| GPU firmware (GSP) | `/Library/NullMoth/nvfw/nvidia/620.70.06` | NVIDIA, unmodified; fetched from NVIDIA's driver package at install (`package/fw_fetch.sh`) |

The kernel side runs NVIDIA's own open GPU kernel modules (620.70.06) under macOS. NVRMFB is the display framebuffer,
NVAccel the accelerator WindowServer composites through, and NVRMAGDC the display-policy shim.

Using the Vulkan runtime is the application's own launch setting:

- **Minecraft 26.3:** the JVM argument
  `-Dorg.lwjgl.vulkan.libname=/Library/GPUBundles/nvmtl/minecraft/lib/libnvk_minecraft.dylib`.
- **MPV and other applications that use the Vulkan loader:**
  `VK_DRIVER_FILES=/Library/GPUBundles/nvmtl/minecraft/nvk_bridge_icd.json`.

See [tools/nvk-bridge/APPLICATIONS.md](tools/nvk-bridge/APPLICATIONS.md). [docs/HOW-IT-WORKS.md](docs/HOW-IT-WORKS.md)
explains the whole stack.

## Documentation

- **Using it:**
  - [Troubleshooting](docs/TROUBLESHOOTING.md)
  - [Every switch](docs/SWITCHES.md) (generated from the source)
  - [Supported cards](docs/CARD-SUPPORT.md)
  - [Hybrid laptops](docs/HYBRID-LAPTOPS.md)
  - [Sleep and wake](docs/SLEEP-WAKE.md)
  - [Zink OpenGL](docs/ZINK-OPENGL.md)
  - [NVMetrics](docs/NVMETRICS.md)
- **Status and releases:**
  - [Release 2 notes](docs/releases/RELEASE-2.md)
  - [Changelog](CHANGELOG.md)
  - [Status and evidence](docs/STATUS.md)
  - [Hardware test checklist](docs/TESTING-ON-HARDWARE.md) ([en español](docs/TESTING-ON-HARDWARE.es.md))
- **Building and developing:**
  - [Build guide](docs/BUILDING.md) ([en español](docs/BUILDING.es.md))
  - [Developing](DEVELOPING.md)
  - [Contributing](CONTRIBUTING.md)
  - [Security](SECURITY.md)
  - [Provenance](docs/PROVENANCE.md)
- **Everything else:** the [documentation index](docs/README.md).

## Credits and license

This fork is not an official NullMoth release. It keeps the original components' licences and credits:

- **The driver:** [NullMoth](https://github.com/nullmoth/nvidia-macos-driver), the original project.
- **Zink OpenGL and the Vulkan application runtime:** [HighDelay](https://github.com/HighDelay/nvidia-macos-driver-nvk)'s
  NVK fork.
- **The shader translator:** based on metal2vulkan.
- **NVK, NAK and Zink:** Mesa.
- **The kernel modules and the GPU firmware:** NVIDIA.
- **The AMFI anchor:** [AMFIAnchor](https://github.com/b00t0x/AMFIAnchor).
- **Changes adapted from open pull requests** to the original project name their authors in [CHANGELOG.md](CHANGELOG.md)
  (3.0.4, 3.0.5).

It is free of charge, with the source code included. Nobody may sell it or use it to make money.

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
