# How the driver works

This page follows a frame from an application down to the GPU, then covers how the package is installed, approved,
recovered and removed, and how each part is built. Paths are the ones on an installed Mac; the source folder for each
piece is in brackets.

> This describes release 2 of this fork, 3.0.20. One machine has run it: an RTX 5050 (Blackwell) on macOS 26.7.3,
> where 3.0.20 was installed on 2026-11-26 ([STATUS.md](STATUS.md)). macOS 15 has not been tried by this fork, and the
> kits for macOS 10.7 to 14 have never run on a card. Every path described here is on by default; the ways back are in
> the [README](../README.md#what-is-on-by-default-and-the-ways-back). The driver needs a Mac or a PC that already
> boots macOS with OpenCore ([OpenCore Install Guide](https://dortania.github.io/OpenCore-Install-Guide/)).

## 1. The layers

```
Metal app / WindowServer / Core Image / MPS / Apple's OpenGL over Metal
        |
        v
NVMTLDriver.bundle  (Metal driver plugin)                          [plugin/]
        |  shaders: Apple AIR -> SPIR-V                              [translator/]
        |  Vulkan calls through the plugin's own Khronos loader (1.4.362)
        v
NVK  (Mesa's Vulkan driver for NVIDIA, with the NAK compiler)       [nvk/, build/build_nvk.sh]
        |  SPIR-V -> NVIDIA machine code, command buffers
        v
NVRM.kext  (NVIDIA's open GPU kernel modules 620.70.06, ported to XNU)  [kexts/NVRM]
        |  talks to the GPU's GSP firmware
        v
NVIDIA GPU, Turing or newer  (GSP firmware from /Library/NullMoth/nvfw/nvidia/620.70.06)
```

Two more routes reach NVK without the Metal plugin or the shader translator:

```
OpenGL app -> NVKZinkGLDriver.bundle -> Mesa's Zink -> Vulkan frontend + NVK copy -> NVRM     [zink/]
Vulkan app -> a Khronos loader (or the app itself) -> Vulkan frontend -> NVK copy -> NVRM     [tools/nvk-bridge/]
```

Display and compositing run beside them:

```
WindowServer -> IOFramebuffer = NVRMFB.kext            (NVKMS heads, modes, vblank)       [kexts/NVRMFB]
             -> IOAccelerator = NVAccel.kext           (surfaces WindowServer composites)  [kexts/NVRM/accel]
             -> AppleGraphicsDeviceControl policy = NVRMAGDC.kext                           [kexts/NVRMAGDC]
```

## 2. Kernel side

**NVRM.kext.** NVIDIA's open-source resource manager, built for macOS with an XNU OS layer (`os-xnu*.cpp`) in place of
Linux's. At start it:
1. claims the PCI device and maps its BARs. It sizes BAR1 (the GPU-visible window into video memory) itself through
   the card's Resizable BAR capability: a BAR1 under 4 GB grows to 8 or 4 GB where the card offers it, one above 8 GB
   is shrunk to the 8 GB proven on hardware (`-nvrmkeepbar1` keeps the firmware's size), and BAR1 is moved out from
   under the firmware console;
2. holds the I/O registry busy so WindowServer does not start on the firmware screen;
3. on its own thread, loads the GSP firmware from `/Library/NullMoth/nvfw/nvidia/620.70.06/` and boots the GPU with
   `rm_init_adapter()`;
4. brings up NVKMS (NVIDIA's mode-setting) and releases the hold once the display is armed.

The boot argument `-nvoff` makes NVRM leave the card alone. The recovery path in section 5 uses it.

Since 3.0.13 NVRM also reads the CPU's page attribute table at start. On an AMD machine with AMD_Vanilla's "Fix PAT"
kernel patch, entry 6 of that table, the one macOS uses for write-combined mappings, is uncached-minus, so every CPU write
through BAR1 leaves the CPU one store at a time. NVRM then writes the entry back to write-combining on every CPU at
once, the one change stock XNU makes itself, and repeats it at each wake, where the patched kernel has put its own
table back. A machine whose table already has the entry sees no write; that is the case on the test machine, whose
OpenCore already puts write-combining back, so the driver's own write has not yet run on hardware.
`sysctl debug.nvrm_pat_wc debug.nvrm_pat_why debug.nvrm_pat_passes` say what is in force; the boot argument
`nvpatwc=0` leaves the table as the kernel wrote it.

NVRM also exposes versioned read-only GPU Caps through selector 14 and live boost policy through selector 13.
The capability snapshot is not a write-permission API. [GPU-CAPS.md](GPU-CAPS.md) and
[NVMETRICS.md](NVMETRICS.md) describe their userspace consumers and limits.

NVRM is also a power child of the GPU's PCI device: on system sleep it suspends NVKMS and puts RM in standby with all of
video memory preserved, and on wake it restores the PCI state and resumes both. NVRMFB then does the wake work
IOGraphics expects. [SLEEP-WAKE.md](SLEEP-WAKE.md) has the details.

**NVRMFB.kext** is the macOS framebuffer (an `IOFramebuffer`). It publishes each NVKMS head as a display, reads real
mode timings and EDIDs, and drives vblank from the GPU. **NVAccel.kext** is the `IOAccelerator` that WindowServer and
IOSurface use for the shared surfaces the plugin renders into; it is the one kext built separately for macOS 15 and for
26 (`Library/NullMoth/kexts/<15|26>` in the package), because Tahoe made the accelerator-family methods it inherits
private. When a program reads a surface with the processor (a screen capture in Firefox, OBS or Zoom), NVAccel pages
the surface off video memory; since 3.0.20 the copy engine fills a buffer of the page-off's own without the
accelerator lock, and the lock is taken back only to write that buffer into the surface, which keeps WindowServer from
waiting behind the copy (`nvaccel_pageoff_copy_locked=1` goes back). **NVRMAGDC.kext** answers Apple's
graphics-device-control policy queries for the card.

All four kexts install into `/Library/Extensions` and load from the Auxiliary Kernel Collection that `install.sh`
rebuilds with `kmutil`. They are not injected by OpenCore: the display kexts link against Apple frameworks
(IOGraphicsFamily, IOAcceleratorFamily2) that only exist in macOS's own kernel collections.

## 3. User space

**NVMTLDriver.bundle** (`/Library/GPUBundles/`) is the Metal driver Apple's Metal.framework loads for the card. It
implements Metal's device, queues, command buffers, encoders, resources, argument buffers, ray tracing and mesh
pipelines on top of Vulkan (`nvmtl_vk.c`), with contract notes for Apple-specific behavior (sample positions, sampler
descriptors, read-only metadata, physical bounds) in `plugin/nvmtl_*.h`. It reaches NVK through its own Vulkan loader,
`/Library/GPUBundles/nvmtl/libvulkan.dylib` (Khronos 1.4.362, carried prebuilt from the 2.9 base package).

**Surfaces shared between processes.** A window's picture is an IOSurface that one process draws into and another
reads. Since release 3.0 the kernel tracks, per surface, which process still has GPU work in flight
([SURFACE-FENCE.md](SURFACE-FENCE.md)), and a reader sleeps until that work is done; 2.9's pending marks and busy tags
remain as the fallback. Two-plane video surfaces (NV12: decoded video, screen-capture and camera frames) can now live
in VRAM, each plane in its own, as single-plane surfaces always could; a process tries it only after a self-test on a
private 128 x 64 surface has passed in that process, and until then, or for good if it fails, two-plane surfaces stay
private images copied in and out as in 2.9 ([STUDIO-475-476.md](STUDIO-475-476.md)).

**Shader translation** (`libnvmtl_translate.dylib`, `translator/`) converts Apple AIR, the bitcode Metal shaders compile
to, into SPIR-V that NVK can compile. It is based on metal2vulkan (LGPL-3.0).

**NVK** (`/Library/GPUBundles/nvmtl/`) is Mesa's Vulkan driver for NVIDIA plus the NAK shader compiler, patched to run
on macOS against NVRM instead of the Linux DRM interface. The base is Mesa `90a959eb` (the mesa-26.3.0-rc3 tag,
`nvk/MESA_BASE`), with what the 26.3.0 release changed in NVK's files on top (section 7 and
[NVK-BASE-AND-HEADERS.md](NVK-BASE-AND-HEADERS.md)).

**OpenGL** (`/Library/GPUBundles/NVKZinkGLDriver.bundle`, `/Library/GPUBundles/nvmtl/zink`; `zink/`). On macOS 15.5
or later the accelerator names this bundle as its OpenGL driver, so an OpenGL application runs on NVK through Mesa's
Zink (Mesa 26.3.0): one translation, where Apple's OpenGL goes through Metal and then through the plugin. Zink's Vulkan
driver in `nvmtl/zink` is a second file copy of the Vulkan frontend below and of its NVK copy. The frontend copies each
frame on the GPU into the window's surface. A swap runs one frame ahead of the GPU and keeps its import of the
window's video memory (3.0.7), asks for the window's opacity once per 100 ms (3.0.8), and with the swap interval at 0
leaves its frame to a sender thread that shows the newest one, so the application is not held to the display (3.0.11).
A `glBufferSubData` that would wait for the GPU goes through a staging buffer (3.0.16). A layout guard checks GLEngine's
private context before any read or write (3.0.3 made it accept the context macOS 26.7.3 hands out); a context the
guard refuses, and every application with the boot argument `nvzink=0`, stays on Apple's OpenGL over Metal.
[ZINK-OPENGL.md](ZINK-OPENGL.md) has the details.

**Vulkan for applications** (`/Library/GPUBundles/nvmtl/minecraft`; `tools/nvk-bridge/`). A frontend library gives a
Vulkan application NVK with windows: the application renders with a separate copy of NVK (`libnvk_renderer.dylib`,
so its objects never mix with the plugin's NVK in the same process) and the frontend copies each frame on the GPU
into a `CAMetalLayer`, with no MoltenVK. It offers the FIFO, IMMEDIATE and MAILBOX present modes; for the last two a
presentation thread shows the newest finished frame whenever the window can take one (3.0.9, 3.0.11;
`NVK_BRIDGE_PRESENT_NEWEST=0` goes back). Nothing selects it by itself: an application chooses it when it is launched,
Minecraft with `-Dorg.lwjgl.vulkan.libname=/Library/GPUBundles/nvmtl/minecraft/lib/libnvk_minecraft.dylib`, an
application that uses a Vulkan loader with `VK_DRIVER_FILES=/Library/GPUBundles/nvmtl/minecraft/nvk_bridge_icd.json`
([INSTALL.md](../tools/nvk-bridge/INSTALL.md), [APPLICATIONS.md](../tools/nvk-bridge/APPLICATIONS.md)).

Three Vulkan versions are in play, and only NVK's is what an application sees as the device's:

| What | Version | Where it comes from |
|---|---|---|
| NVK, the driver | Vulkan 1.4.363 | the Vulkan headers of its Mesa 26.3 base (`VK_HEADER_VERSION` 363 at `90a959eb`); the frontend's manifest carries the same number. It moves only when Mesa is rebased ([NVK-BASE-AND-HEADERS.md](NVK-BASE-AND-HEADERS.md)) |
| The Khronos loader offered to applications | v1.4.367 | built unmodified from source by `build/build_vulkan_loader.sh`, staged as `Library/NullMoth/vulkan/libvulkan.1.dylib`, not installed by default: `sudo bash vulkan-for-apps.sh --install --with-loader Library/NullMoth/vulkan/libvulkan.1.dylib` puts it in `/usr/local/lib` and writes a loader manifest for the driver's NVK (whose own windows are Mesa's Metal swapchain, copied on the CPU) |
| The Metal plugin's own loader | 1.4.362 | `nvmtl/libvulkan.1.4.362.dylib`, prebuilt in the 2.9 base package; no source in this repository ([PROVENANCE.md](PROVENANCE.md)) |

**DLSS** (`/Library/NullMoth/dlss`; `dlss/`, `plugin/nvmtl_dlss.c`). The package carries no NVIDIA DLSS code. Since
3.0.13 `install.sh` downloads NVIDIA's own files (about 750 MB the first time), checks their pinned hashes and builds
the DLSS kernels for the card, unless `NULLMOTH_DLSS=0` is set; 3.0.19 fixed the check that had skipped the step in
every package of this fork. With the files in place the plugin serves the MetalFX temporal upscaler with DLSS
Super Resolution and the MetalFX frame interpolator with DLSS Frame Generation; WindowServer never loads them.

**NVIDIAShared.bundle** is no longer in the package. Packages up to 2.8.1 carried it with two of NVIDIA's closed
compiler libraries; the plugin used it only when a developer switch (`NVMTL_VENDOR_COMPILER`) was set. The installer
removes an old copy on update and the package gate refuses a tree that holds one. The plugin's MPS convolution fast path
is `plugin/nvconv.metal`.

## 4. OpenCore settings the driver needs

The [README](../README.md#opencore-settings) has the table to copy. Why each part is there:

| Setting | Value | Why |
|---|---|---|
| `NVRAM > Add > 7C436110-…-FE41995C9F82 > boot-args` | `nvfb=1 nvaccel=1 nvfbheads=4 -nvkmsnosmooth amfi_get_out_of_my_way=0x1 amfi=0x80 ipc_control_port_options=0` | turns on the framebuffer and accelerator, 4 display heads; the AMFI arguments let WindowServer load the driver bundle. The optional trust chain of section 5 drops the two AMFI arguments |
| `NVRAM > Add > … > csr-active-config` | `<430A0000>` (bits `0x0A43`) | the SIP value the driver was tested with: its kexts are not Apple-signed, and root may write the kext approval database (section 5) |
| `Kernel > Add` | `Lilu.kext`, `AMFIPass.kext`, enabled | required by the README's setup; with the trust chain of section 5, `AMFIAnchor.kext` and the anchor kext load between them |
| `Misc > Security > SecureBootModel` | `Disabled` | Apple Secure Boot refuses kexts Apple did not sign |
| `UEFI > Quirks > ResizeGpuBars`, `Booter > Quirks > ResizeAppleGpuBars` | keep the existing values | NVRM sizes and places BAR1 itself (section 2) |
| `Kernel > Block` | `com.apple.iokit.IONDRVSupport`, `Exclude` | otherwise the firmware framebuffer takes display index 0 from NVRMFB |
| BIOS | Above 4G Decoding on, CSM off | the card maps memory above 4 GB |

Optional boot arguments, for troubleshooting: `-nvrmnoflip` keeps NVAccel on the copy path (no zero-copy scan-out
of the composited surface) and is the first thing to try when the desktop flashes or tears while the cursor moves;
`-nvrmnogo` leaves the GPU idle until `nvrmctl go <pass>`; `-nvrmnobootscreen` skips the boot-screen snapshot.

Installing macOS itself needs different values. The macOS installer has no NVIDIA driver, so it runs on the firmware's
screen, which only survives macOS's PCI setup with a small BAR: `ResizeAppleGpuBars = 0`, `ResizeGpuBars = -1`, and
`IONDRVSupport` not excluded. Set the values above again once macOS is installed, before the driver.

With `IONDRVSupport` not excluded the installed driver still gets the card: `NVRMFBClaim` (probe score 30000) holds
the card's `IOFramebuffer` match category before Apple's `IONDRVFramebuffer` (20000) is tried, and `NVRMBootFB` (25000)
comes before it with the driver off. Apple's framebuffer takes the card only when both step aside, mainly after the
framebuffer's layout check refused this macOS. It then shows the firmware's picture, when that picture still lies
inside the card's memory window, as a slow desktop without acceleration, where the exclusion leaves the screen dark.

## 5. Installing, approving, recovering and removing

The package is installed by hand from Terminal (README, *Install*); `install.sh` never changes OpenCore.

**One package for every release (3.0.19).** Right after the root check, `install.sh` reads the macOS version. On 15
and 26 it goes on as below. On any other release it runs `package/pick_kit.sh <version> <package>`, which prints
`modern`, `legacy <release>` or `none <reason>`: it knows 13 kits, 10.7, 10.8, 10.9, 10.10, 10.11, 10.12, 10.13,
10.14, 10.15, 11, 12, 13 and 14, each in `legacy/<release>/` of the package, and refuses 10.6 and older, an unknown
release, or a package built without the kits. For a kit, the installer copies it to a root-owned folder in `/var/tmp`,
checks every file of the copy one by one against the package's `SHA256SUMS` (a malformed line or an unlisted file
stops it) and runs the kit's own `install-legacy.sh` from there (`CHECK=1` becomes its `--check`). The kit's installer
says which boot arguments to add. A kit holds that release's kexts (NVRM and NVRMFB everywhere, NVRMAGDC from 10.9,
NVAccel from 10.12), the Metal userland where the release has Metal, and the OpenGL engine where one is staged
(10.7 to 10.13). The kits come from the original project's older-release work (its 3.0 and 3.1); this fork builds them
from its own tree (section 7). None has run on a card or on an older macOS.

**On 15 and 26** the installer:
1. checks the Mac (x86_64, the macOS build, an NVIDIA GPU, a device id in `SUPPORTED_IDS`; see
   [CARD-SUPPORT.md](CARD-SUPPORT.md));
2. copies the package to a root-owned private folder and checks it there: `shasum --strict -c SHA256SUMS`, then
   `sums_complete.sh`, because `shasum -c` reads only the files the list names;
3. test-builds the Auxiliary Kernel Collection, so a Mac that cannot take the driver is left as it was, and fetches
   what the package does not carry: the GPU firmware from NVIDIA's 620.70.06 Linux driver package
   (`package/fw_fetch.sh`, each file checked against a pinned SHA-256; with no network it also finds the driver
   package in Downloads, on the Desktop, in /Users/Shared or on a USB stick);
4. backs up what is installed;
5. copies the files, records the kexts' approval (below) and builds the live kernel collection;
6. installs the boot guard's jobs (below), then the DLSS step (`package/dlss_fetch.sh`, skipped with
   `NULLMOTH_DLSS=0`; a failure there leaves the driver installed).

`sudo env CHECK=1 ./install.sh` rehearses the install and changes nothing; go on only when it ends with `CHECK PASS`.

**Kext approval (3.0.19).** macOS loads a third-party kext only once its bundle is allowed (`allowed=1` in the
`kext_policy` table of `/var/db/SystemPolicyConfiguration/KextPolicy`). The installer writes the row the **Allow**
button in System Settings > Privacy & Security writes, for exactly the kexts it installed (bundle ids constrained to
`[A-Za-z0-9.]`), and refreshes the recorded code hash of a kext already allowed at that path. The driver's SIP value
lets root write that SIP-protected database, so a first install needs one restart and no Allow. Where the database is
read-only the install still works, macOS asks as before, and the installer's last message says which happened. This is
host-tested (`tools/test_kext_preapprove.py`) and has not yet been tried on a Mac.

**AMFI left on (optional, experimental).** The two AMFI boot arguments turn off code-signing enforcement for the whole
system so that WindowServer loads the unsigned plugin. Instead, the graphics stack can be signed with a private CA and
that CA trusted at boot:
1. `tools/make_amfi_anchor.sh --amfianchor <AMFIAnchor checkout>` creates, on your Mac only, a 4096/4096/2048-bit RSA
   chain with the Code Signing EKU, refuses to write keys inside a git checkout, and runs
   [AMFIAnchor](https://github.com/b00t0x/AMFIAnchor)'s own `make_injector.py` on the public root to build a codeless
   anchor kext;
2. `tools/sign_graphics.sh` re-signs `NVMTLDriver.bundle`, `NVKZinkGLDriver.bundle` and the `nvmtl` libraries of a
   finished package with that chain, deepest Mach-O first, into a separate folder, and
   `tools/stage_signed_graphics.py` imports them back over the package, checking build UUIDs and the leaf certificate,
   and recomputes `SHA256SUMS`;
3. OpenCore loads `Lilu.kext`, then `AMFIAnchor.kext` (b00t0x), then the anchor kext, then `AMFIPass.kext`.

WindowServer then loads the plugin with AMFI enforcing. On the test machine the anchor registered at the first boot
(2026-11-26), the GPU reported Metal 4, and 3.0.20 ran signed that way. Repeated boots, sleep and wake, sustained load,
App Store and iMessage are not qualified, so the documented install keeps the AMFI arguments.
[SIGNED-GRAPHICS-EXPERIMENT.md](SIGNED-GRAPHICS-EXPERIMENT.md) has the steps, [AMFI-COMPATIBILITY.md](AMFI-COMPATIBILITY.md)
the background.

**The way back if the driver stops macOS starting.** The kernel's boot guard (`nvrm_off.h`) turns the driver off after
two starts in a row that never brought the display up, so the Mac reaches a desktop on the firmware's framebuffer. A
one-shot job the installer leaves (`com.nullmoth.lastgood`) then puts the previous driver back from the install's
backup and restarts once; it removes itself once a start of the new driver has lit the display. A fresh install has
nothing to go back to, and the guard alone keeps the Mac usable. By hand, `-nvoff` in boot-args starts without the
driver. A failure the guard cannot count (the framebuffer refused this macOS's classes, `nvrm-layout = "declined ..."`
on the PCI device) is handled by the same job on the macOS build the install was made on.

**Remove.** `uninstall.sh` removes the files, the jobs, the `gfxrst=4` the installer may have added to the NVRAM
boot-args and the licence texts, and rebuilds the kernel collection. On 10.7 to 14 it runs the kit's remover kept in
`/Library/NullMoth/legacy/<release>/`.

## 6. The package

The tarball holds one folder, `pkgroot/`: every installed file at its install path (`Library/...`), and at its top
`install.sh`, `uninstall.sh`, `pick_kit.sh`, the fetchers, `licenses/` (installed to `/Library/NullMoth/licenses`),
`SHA256SUMS` and, in a package built with them, the 13 kits in `legacy/<release>/`. What the package does not carry,
`install.sh` fetches from NVIDIA's own sites and checks against pinned hashes: the GSP firmware and the DLSS files.

`package/finish_package.sh <pkgroot> --nvencvt <lib> --air-dis <nvmtl-air-dis> --nmsmn <kext> <ryzenadj> <sha256> [--boost <bin>] [--vulkan-loader <dir>]` is the last stage of a package build: it stages those parts in one order, adds `sums_complete.sh`, and runs `package/check_package.sh <pkgroot>`, which fails if `libnvencvt.dylib` is missing, unsigned or not in `SHA256SUMS`, if the Ryzen power parts are missing, if any file in the tree has no line in `SHA256SUMS`, if a program in it is signed by Apple, or if a library in it loads from outside the system and the folders the package installs. `package/stage_nvencvt.sh <pkgroot> <libnvencvt.dylib>` puts the video codec library into a package tree, signs it and refreshes `SHA256SUMS`; `install.sh` stops before changing anything if a package lacks it. `nvmtl-air-dis` (`plugin/air_dis.c`, built by `build/build_air_dis.sh`) is the plugin's out-of-process AIR decoder, on the same upstream LLVM as `air-runtime`; packages up to 2.8.1 carried the Metal toolchain's `air-opt` for this instead, which is Apple's program and is no longer shipped.

## 7. Building

See the README, *Build from source*, and [BUILDING.md](BUILDING.md). The kexts need NVIDIA's `open-gpu-kernel-modules`
at tag `620.70.06`. NVK needs Mesa `90a959eb` (the mesa-26.3.0-rc3 tag; 2.8 and 2.8.1 used `17ca6174`) with the chain
`build/build_nvk.sh` applies with `patch -p1` from the Mesa root, in this order:

- the 2.9 chain: `nvk/nvk-macos.patch`, `nvk/nvk-fixes-after-def16.patch`, `nvk/nvk-620-kernel.patch`,
  `nvk/nvk-recycle.patch`, `nvk/nvk-slab.patch`, `nvk/nvk-slabdef.patch`, `nvk/nvk-hubfix.patch`,
  `nvk/nvk-perf-boost-burst.patch`, `nvk/nvk-rated-tdp.patch`, `nvk/nvk-gps-maxpower.patch`, `nvk/nvk-metal-wsi.patch`,
  `nvk/nvk-card-select.patch`, `nvk/hwrt/ttu-nak.patch`, `nvk/hwrt/ttu-rq.patch`, `nvk/curves/curves.patch`,
  `nvk/hwrt/wide.patch`, `nvk/nvk-warnings.patch`, `nvk/nvk-unbound-stage.patch`, `nvk/nvk-diag-nofollow.patch` (the
  `hwrt` patches are the ray-query part for the RT cores, `wide.patch` its 8-wide node option, off unless
  `NVK_HWRT_QUERY=1`, see [HWRT.md](HWRT.md));
- three patches of upstream Mesa commits newer than the base, text unchanged: `nvk/nvk-mesa-26.3.0.patch`,
  `nvk/nvk-nak-fixes-main.patch`, `nvk/nvk-nak-sched-main.patch` ([NVK-BASE-AND-HEADERS.md](NVK-BASE-AND-HEADERS.md));
- the driver's own: `nvk/nvk-evwait.patch` (waits sleep on the GPU's completion event in every process,
  [EVENT-WAIT.md](EVENT-WAIT.md)), `nvk/nvk-fence.patch` (channels registered with the kext's surface fence, and the
  plugin's calls into it, [SURFACE-FENCE.md](SURFACE-FENCE.md)), `nvk/nvk-channel-recover.patch` (a faulted channel
  rebuilt instead of the device lost, [CHANNEL-RECOVERY.md](CHANNEL-RECOVERY.md)), `nvk/nvk-stream-uncached.patch`
  (stream chunks not GPU-cacheable), `nvk/nvk-boost-policy.patch` (the boost lease follows the kext's boost policy);
- 3.0.1: `nvk/nvk-mesa-main-2.patch` (eight upstream Mesa main commits for NVK and the Vulkan runtime, unchanged),
  `nvk/nvk-nvrm-fixes.patch` (the NVRM back end's mapping table under a lock, no placed maps offered on NVRM, three
  settings read once), `nvk/nvk-local-map-vram.patch` (NVK's own mapped device-local memory that the CPU only writes
  stays in video memory by default outside WindowServer);
- later releases: `nvk/nvk-gpu-caps.patch` (3.0.3), `nvk/nvk-desc-alloc.patch` (3.0.6), `nvk/nvk-surface-keep.patch`
  (3.0.7), `nvk/nvk-nak-switch-key.patch` and `nvk/nvk-tex-uhandle-safe.patch` (3.0.12), `nvk/nvk-nak-stats.patch`
  (3.0.14), `nvk/nvk-zcull.patch` (3.0.17), then from the original's 3.1 (3.0.18) `nvk/nvk-nak-late-split.patch` and
  `nvk/hwrt/ttu-rq-split.patch`;

then `nvk/hwrt/nvk_hwrt.h` and `nvk_hwrt_layout.h` are copied into `src/nouveau/vulkan`, and
`nvk/dlss/patch_dlss_cache.py <mesa tree>` runs last. NVK compiles against the `open-gpu-kernel-modules` 610.57.04
headers with `nvk/ogkm-nvos-620.patch` and `nvk/ogkm-warnings.patch` applied to that tree; 620's `nv.h` does not compile
in user space, so the rest stays 610.

`nvk/nvk-metal-wsi.patch` turns on `VK_EXT_metal_surface`: it adds the extension to NVK's instance table, counts the Metal platform as a window system, and runs Mesa's Metal swapchain in its software mode (a linear host-visible image copied into the `CAMetalLayer` drawable by Mesa's layer helper, because the hardware mode needs a driver that can bind a Metal texture to a `VkImage`). `nvk/nvk-card-select.patch` makes the RM connection honour `NVK_ACCEL_ID`, the registry entry id of one accelerator, so a process bound to one of several cards opens that card (unset, the first accelerator, as before). The build uses `-Dplatforms=macos` for the NVK build and still `-Dplatforms=` for the native `mesa_clc` tool build.

`build/build_nvk.sh <mesa git> <610.57.04 tree> <out> [3.0|2.9|2.8]` does all of it: it exports the Mesa base with
`git archive`, applies the header patch and the chain of the release asked for (default 3.0; `2.9` and `2.8` rebuild
those releases' libraries to compare with), builds `mesa_clc` and `vtn_bindgen2` from that same tree (tools from
another Mesa revision deserialize NVK's internal shaders as garbage), cross-builds `libvulkan_nouveau.dylib` for x86_64
and prints its LC_UUID and export count, and what was left out of or added to the chain, if anything
(`NVK_SKIP_PATCHES`, `NVK_EXTRA_PATCHES`). The `2.8` argument builds the set the 2.8 / 2.8.1 package was made from
(`nvk-macos`, `nvk/release-2.8/nvk-fixes-after-def16.patch`, `nvk-620-kernel`, `nvk-recycle`, `nvk-slab` and
`nvk-slabdef` only) on `17ca6174`. Rebuilding 2.8 gives the shipped library byte for byte except the 16 LC_UUID
bytes: the linker derives the UUID from the unstripped file, which records the absolute path of every object, so a
different build directory gives a different UUID. `build/nvk_compare.py` states exactly which bytes differ. The NAK
changes under `nvk/speed267` are already contained in `nvk-macos.patch`; the scripts there record how they were made.

The Vulkan frontend (`tools/nvk-bridge/build.sh`, the `nvkbridge` component) is built after NVK and copies it as its
renderer; native OpenGL is built from Mesa `mesa-26.3.0` (0a6fa60a) by `build/build_zink.sh`; the application Vulkan
loader by `build/build_vulkan_loader.sh` at v1.4.367, each commit checked after the clone.

The kits for 10.7 to 14 are not built by `./build.sh`. They are built by hand on a Mac with Apple's SDK and Kernel
Debug Kit of each release, with the scripts in `build/legacy/`, and packed by `build/legacy/package_kits.sh`; then
`NM_LEGACY_KITS=<that folder> ./build.sh package` puts all 13 in the package or makes none. The accelerator headers of
10.10 to 10.14, regenerated from each release's accelerator family, are kept in
[build/legacy/era-headers](../build/legacy/era-headers/README.md), so a rebuild needs no full macOS installer.
[BUILDING.md](BUILDING.md), "Building the kits for macOS 10.7 to 14", has the steps.

## 8. Video, power and diagnostics

The video codecs in `libnvencvt.dylib` are built from `vendor/nvenc` (mickys/nvenc, MIT) by `build/build_nvencvt.sh`. The
package puts the library beside the plugin in `NVMTLDriver.bundle/Contents/MacOS/`; the plugin loads it only when it and
its folder are root-owned. `NVMTL_NVENCVT` in `nvrm610.conf` decides where it registers: unset is automatic on Ada and
Ampere, and on RTX 50 in the decode service only; `0` is never; `1` is every process on any card the library accepts;
a list of process names limits it to those. On RTX 50 the library registers nothing, whatever the value, until the
card has decoded its two built-in H.264 test streams bit for bit in that process (one line in the system log,
`Blackwell H.264 decode self-test: PASS` or `FAIL at <step>`); a process where that fails keeps the decoders it had.
The plugin asks the library whether it has that test before it uses it on RTX 50 (the library exports
`NVEncVT_BlackwellSelfTest`), so an older build of the library beside a newer plugin is refused there instead of
registering an untested decoder. `build/build_nvencvt.sh` also compares every struct and constant the library takes
from NVIDIA's 610.57.04 headers with the 620.70.06 tree the kernel module is built from
(`tools/nvencvt_header_abi.py`) and stops on a difference; without that tree on the build Mac it says that it skipped the
comparison and builds on. There is no VP9 or AV1 hardware decode on RTX 50 ([WINDOWS-PARITY-MEDIA.md](WINDOWS-PARITY-MEDIA.md)).

The GPU's boost policy is `NVRM_610_BOOST`: unset is a short lease renewed while work is submitted, `hold` keeps one boost
for the device's life, `0` never boosts. From 3.0.1 NVRM.kext also holds a machine-wide policy (undecided, idle mode,
normal; `kexts/NVRM/nvrm_boost_policy_abi.h`) that a root process sets through user-client selector 13 and that each
process reads there before it renews (`nvk/nvk-boost-policy.patch`): idle mode clears what a process has asked for within
a second of its next submission, normal brings the lease back even to a process started with `NVRM_610_BOOST=0`, and
undecided, the state at every boot, leaves each process to `NVRM_610_BOOST`. NVMetrics sets it with
`nvmetrics-agent policy idle|normal`; `sysctl debug.nvrm_boost_policy` shows it (read-only).
`NVRM_610_MAXPOWER_MW=<mW>` (off unless set) asks the GPU performance-scaling code for that maximum power through the public root-client GPS control and reads it back.
`NVRM_610_RATEDTDP` is off unless set: `l` holds the card at its rated TDP performance state (a power and clock cap), `k` locks it, `x` allows the turbo ceiling.

`tools/nvmtl-smi` (build with `tools/nvmtl-smi/build.sh <patched Mesa tree> <out>`) prints the GPU's temperature, power, clocks, pstate, utilisation, throttle reasons and fan from the resource manager's shared-data block.

Opt-in D3cold (`kexts/NVRM/nv_rtd3.cpp`): `nvrtd3=1` and `nvrtd3res=<ACPI path of the GPU's power resource>` on the boot line. After 60 idle seconds (no framebuffer, no user client) the driver runs its sleep path and then the resource's `_OFF`; the first client or framebuffer runs `_ON` and then the wake path; system wake runs `_ON` before the resume.

Kernel log volume: start-up, mode changes, sleep and wake, errors and warnings always log. The per-query lines of NVRMFB are on with the boot argument `nvfblog=1`, those of NVAccel with `-nvaccelverbose`. NVRM's lines are boot, sleep and wake events, and warnings (the surface fence, for one, says when it gives a writer up or polls a channel that raises no interrupt). The plugin's log lines are rate-limited per call site. `sysctl debug.nvrm_switches debug.nvrm_switches_nondefault` lists the driver's boot arguments set beside the install line, and how many of them an ordinary install does not need; [SWITCHES.md](SWITCHES.md) lists every name the driver reads.
