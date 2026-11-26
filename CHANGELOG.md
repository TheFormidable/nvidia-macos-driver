# Changelog

## 3.0.20

**Not released.** On top of 3.0.19. One change, in the OpenGL adapter of Zink: the preview of one's own screen share
in a video call ran slowly in Firefox.

### What changed

- **Firefox and the other browsers built on Gecko keep Apple's OpenGL over the Metal plugin** (`zink/nvk_gecko.h`,
  `zink/nvk_cgl.c`). Reported on the RTX 5050 machine with 3.0.19: sharing a tab from a Google Meet call, the preview
  of the share in the Meet tab moved slowly, while the shared tab itself and what the other side of the call received
  kept their rate. Since 3.0.6 Firefox's OpenGL went to Zink. Gecko draws everything it shows into IOSurfaces it binds
  with `CGLTexImageIOSurface2D` (WebRender's CoreAnimation tiles and video surfaces, WebGL's canvases), and in a Zink
  context each of those is a private image, copied in from the surface's pages and copied back out, each copy followed
  by a wait for the GPU, at every `glFlush`, `glFinish` and `glFenceSync` (`zink/nvk_iosurface.c`). A decoded video,
  as on the shared YouTube tab, reaches the screen as a layer of its own and pays none of this; the call's preview is
  drawn by WebRender every frame and pays it for each surface it touches. On Apple's OpenGL the Metal plugin draws
  into the surface's own video memory and copies nothing, as it did for Firefox up to 3.0.5.
  - A process counts as Gecko's when an image whose file is named exactly `XUL` is loaded in it (Gecko's library:
    Firefox, LibreWolf, Zen, Floorp, Waterfox, Tor Browser, Thunderbird, each with its helper processes). It is
    looked at once, when the process makes its first OpenGL context, right after the check for OBS and before anything
    of GLEngine is read or an NVK device is opened.
  - The adapter's two switches are read together, once (`read_cgl_switches`): the settings reader is not for two
    threads at once.
- `tools/test_zink_gecko.py` (new): the header built and run on any machine with every case and four controls that
  must fail, the place of the check in `nvk_cgl_prepare_gld`, the switch, the registry row and the texts.
  `tools/test_cgl_gld_guard.py` and `tools/test_zink_opacity.py` read the new statement and the renamed reader.

### What ran where

Linux only: `test_zink_gecko.py`, `test_cgl_gld_guard.py`, `test_zink_opacity.py`, `test_zink_context_refs.py` and
the registry's `check`. Nothing of it has been compiled against Apple's SDK or run in Firefox on a card; whether
Firefox's preview runs at the share's rate with it is for the card to say.

### The ways back

| Way back | Kind | What it does |
|---|---|---|
| `NVK_ZINK_GECKO_APPLE=0` | settings line | Firefox and the other Gecko browsers draw through Zink like any other OpenGL application, as in 3.0.19; restart the browser after changing it |

## 3.0.19

**Not released.** This section describes the branch being prepared as 3.0.19, on top of 3.0.18 (below, not released
either). **One package for every release:** the package carries the kits for macOS 10.7 to 14, built by this fork from
its own tree with Apple's SDK and Kernel Debug Kit of each release, and the installer picks the kit of the macOS it runs
on. Nothing to choose: the same `sudo ./install.sh` on 10.7 to 14, 15 and 26. **No kit has been run on a card, and no
older release has been installed on;** the kits are the original's older-release lane (3.0.18) built here.

### What changed

- `package/pick_kit.sh` (new) names the part of the package for a macOS version: `modern` on 15 and 26, `legacy
  <release>` on 10.7 to 14, `none <reason>` otherwise (10.6 and older, an unknown release, or a package without the
  kit). Plain bash 3.2, as on 10.7.
- `package/install.sh`: right after the root check, on any release but 15 and 26, it runs `pick_kit.sh`, copies that
  kit to a root-owned folder in /var/tmp, checks every file of the copy against the package's `SHA256SUMS` (one file at
  a time with plain `shasum`, since 10.7's has no `--strict`; a malformed line or an unlisted file stops it), and runs
  the kit's own `install-legacy.sh` from the copy (`CHECK=1` becomes its `--check`). It does not change boot
  arguments; the kit's installer says what to add. On 15 and 26 nothing changed.
- `package/uninstall.sh`: on 10.7 to 14 it runs the remover the kit left in `/Library/NullMoth/legacy/<release>/`.
- `build/package_local.sh` and `./build.sh package`: `NM_LEGACY_KITS=<folder of build/legacy/package_kits.sh>` puts the
  13 kits in `legacy/<release>/` of the package, each checked against `legacy_kits.json` and its own `SHA256SUMS`. All
  13 or the package is not made. Without it the package has no kits and `install.sh` says so on an older release.
- `tools/test_pick_kit.py` (new): every release and the versions around them; the installer's dispatch lifted out and
  run with stand-ins, with a changed kit file, an unlisted file, a malformed line and a list without the kit as controls.
- NVRM builds for 10.7 to 14 again. Three things this fork added since 2.9 need a newer kernel than the oldest kits:
  - Thread names (10.15) do nothing before 10.15 (`kexts/compat/nm_era.h`).
  - The DMA read-back (`IOMapper::mapToPhysicalAddress`, 10.11) counts nothing as verified before 10.11. A GPU behind
    a DMA mapper is then refused at the self-test, as with `nvrmdma=0`; without a mapper nothing changes
    (`kexts/NVRM/nvrm_dma.cpp`).
  - The host-contiguous allocation option (10.8) is no option on 10.7. It is used only while DMA is remapped, which
    never happens there.
- NVAccel builds for 10.11 to 10.14 with the headers regenerated from each release's own accelerator family and Apple's
  vendor drivers (from the full installers). `NVL_VT` in `kexts/NVRM/nvrm_layout.h` (and its copy in NVRMAGDC) writes
  out `NVL_BUILT` instead of calling it: an argument handed on to another macro is expanded first, and the
  regenerated headers define `IOAccelLegacyDisplayPipe` as `IOAccelDisplayPipe2`, so the call named a struct that
  `NVL_TAIL` never declared. Builds without that alias expand the same as before.
  - On those headers the class's vtable is `IOAccelDisplayPipe2`'s, so the layout check's vtable symbol follows the
    alias (`kexts/NVRM/accel/nvaccel_layout.h`). Without it, 10.14's kext linker refused NVAccel.
  - Before 10.14, `NVResource::calculateIOSurfaceDeviceCacheVRAMBytes` takes `unsigned int *` for its second result,
    as the regenerated 10.12 and 10.13 headers declare (`kexts/NVRM/accel/nvaccel_stubs.h`). It wrote 4 bytes there
    already; now the override matches. `build/legacy/gen_era_headers.py` gives it `uint64_t`: with no matching newer
    declaration it fell back to `void`, and a value a caller ignores is harmless where a missing one is not.
  - 10.11 is not covered: its family lacks the GPU page-table methods NVAccel overrides. That kit keeps OpenGL.
- `build/legacy/assemble_kits.sh`: a kext the release's own kext linker refused goes to `held-back/`, not `kexts/`.
  `install-legacy.sh` test-links every kext in `kexts/` and stops the whole install on one refusal.
- The kits' programs are all signed (ad hoc), as `package/check_package.sh` asks of the package. There were 23
  unsigned Mach-O files:
  - `build/legacy/userland/stage_legacy.sh` signed `NVMTLDriver` first. Signing a bundle's main program seals the
    bundle, and codesign refused because its neighbours were not signed yet; the error was hidden. It now signs them
    first, the plugin last, and a failure stops the stage.
  - `gl/package/stage.sh` signed nothing. It now signs the Vulkan driver, then the engine, before it writes
    `MANIFEST.json`.
  - `build/legacy/package_kits.sh` signs again what its own changes unsealed (the bundle version it writes into
    `NVMTLDriver.bundle`'s Info.plist, the rewrites of `scrub_kit.py`), then stops on any program still unsigned and
    names it.
- The 10.13 and 10.14 kits no longer carry a copy of Apple's SkyLight `Configuration.plist` (the file as shipped and
  one with the key added) in `options/compositor-route-A-configuration-key/`: `build/legacy/userland/stage_options.sh`
  now writes only the README, whose steps add the key to the Mac's own copy and save it first. `package_kits.sh` stops
  if such a file is in a kit, because the kits are published.
- `build/legacy/scrub_kit.py` also takes out the build machine's folder where the link log (`kexts/link-test.txt`)
  writes it with underscores for slashes (`_Users_<name>_...`, the names `kxld_run.py` gives its thinned copies), and
  the machine's per-user temporary folder (`/var/folders/<id>/<id>`). The 10.11 and 10.12 kits named the build
  account that way.
- `./build.sh package` builds the package again when the kits in `NM_LEGACY_KITS` were packed again: its recipe now
  carries the SHA-256 of that folder's `legacy_kits.json`, which names every kit by its own SHA-256. Before, it held
  only the folder's path, so new kits at the same path left the package "up to date" with the old ones.
- **DLSS is downloaded again on install, as it should have been since 3.0.13.** `package/install.sh` runs the DLSS step
  only when `SHA256SUMS` lists `dlss_fetch.sh` and its tools, and it looked for the lines `<hash>  dlss_fetch.sh`.
  This fork's packages write `<hash>  ./dlss_fetch.sh` (`build/package_local.sh` through `sums_refresh.sh`), so every
  install printed "this package's SHA256SUMS does not cover the DLSS step; skipping it" and downloaded nothing. The
  same checks of the optional Safari copy (`NULLMOTH_SAFARI_GPU_COPY=1`) and GPU boost daemon (`NULLMOTH_GPU_BOOST=1`)
  had the same fault. All seven now accept both spellings. Found by installing 3.0.19 on the test machine.

### What ran where

Linux: `tools/test_pick_kit.py` passes; the tests that read install.sh, uninstall.sh, the package and build.sh's
settings pass as before.

On the owner's Mac (macOS 26.7.3, 2026-11-25), from 2e4b9cc4, in a separate work folder, with Apple's SDK and Kernel
Debug Kit of each release: the 13 kits were built and packed, and so was the full package with them inside
(`nullmoth-nvidia-local-2e4b9cc4.tar.gz`, 415,814,046 bytes; `check_package.sh` passes over its 591 files). Each kit's
kexts went through that release's kernel extension linker (kxld), which exists up to 10.15; from 11 to 14 they are only
linked at build time.

| macOS | kexts | link test | Metal | OpenGL |
|---|---|---|---|---|
| 10.7 | NVRM, NVRMFB | linked | not on that release | yes |
| 10.8 | NVRM, NVRMFB (NVRMAGDC held back) | linked | not on that release | yes |
| 10.9, 10.10 | NVRM, NVRMFB, NVRMAGDC | linked | not on that release | yes |
| 10.11 | NVRM, NVRMFB, NVRMAGDC | linked | userland only, no accelerator | yes |
| 10.12, 10.13 | all four | linked | yes | yes |
| 10.14, 10.15 | all four | linked | yes | no (by design) |
| 11 to 14 | all four | build time only | yes | no (by design) |

`pick_kit.sh` was run against the built package without installing: it picks the 10.7, 10.13, 10.14, 12 and 14 kits
for 10.7.5, 10.13.6, 10.14.6, 12.7.6 and 14.8.3, and the usual driver for 26.7.3. The accelerator headers of 10.10 to
10.14 made on that run are kept in `build/legacy/era-headers` (see its README). Nothing was installed, and no older
release has run on a card.

### AMFI compatibility (experimental, [PR #28](https://github.com/TheFormidable/nvidia-macos-driver/pull/28))

An experimental route to drop the global AMFI-disabling boot arguments `amfi_get_out_of_my_way=0x1 amfi=0x80`. Instead
of turning off macOS's signature check for the whole system, the graphics bundles are signed with your own certificate
authority, and that CA is registered in OpenCore as an [AMFIAnchor](https://github.com/b00t0x/AMFIAnchor) trust anchor;
AMFI stays enforcing and only code signed by your CA is trusted to load. It follows the separation of signing from
packaging documented by [WhateverXe-Graphics](https://github.com/b00t0x/WhateverXe-Graphics), without reusing any other
project's signing identity or vendoring keys. **A staging experiment, not the tested default — keep the two AMFI boot
arguments until a signed-only boot is qualified on your machine.**

- `tools/make_amfi_anchor.sh` (new): builds a dedicated 4096/4096/2048-bit RSA signing chain and, from its **public**
  root, the codeless anchor kext (via a cloned AMFIAnchor's own `make_injector.py`), all on the operator's machine. It
  refuses to write keys inside a git checkout; the private keys never leave the machine and never go to GitHub.
- `tools/sign_graphics.sh` (new): re-signs the three graphics units (`NVMTLDriver.bundle`, `NVKZinkGLDriver.bundle`,
  `nvmtl`) of a finished pkgroot with that chain, deepest Mach-O first then the bundle seals, into a separate signed
  directory. It never runs as root and changes nothing installed. rcodesign 0.29.0 cannot verify a bundle, so the
  post-seal check verifies each Mach-O with rcodesign and the resource seal with `codesign` (a private chain the host
  does not trust seals correctly; `CSSMERR_TP_NOT_TRUSTED` is not a failure).
- `tools/stage_signed_graphics.py` (new): imports the signed units back over a pkgroot, checking the build UUIDs are
  unchanged and the expected leaf certificate is present, and recomputes `SHA256SUMS`. Nothing is installed.
- `docs/SIGNED-GRAPHICS-EXPERIMENT.md` (new) documents the local signing identity, the signing and import steps, the
  OpenCore load order (Lilu → AMFIAnchor → the anchor → AMFIPass), and the acceptance gates.
  `docs/AMFI-COMPATIBILITY.md` carries the original failure analysis and now points to this result.

**What ran where.** Linux: the signing chain, `make_injector.py` anchor build and certificate profile were validated
in the container. On the owner's RTX 5050 / macOS 26.7.3 (25G342), external disk, one boot: with the three graphics
units signed by the private chain and the anchor EFI loaded, `ioreg -r -c AMFIAnchor -l` reported `InstallStatus` 1 and
the anchor `org.theformidable.nvidia-signing` (550-byte SPKI); `kern.bootargs` had no AMFI arguments (enforcing); the
boot's unified log had no `Failed to create MetalDevice` or platform-binary rejection; `system_profiler` showed the
RTX 5050 driving the display at Metal 4. This satisfies gate 3 (WindowServer load policy and a stable Metal device with
AMFI enabled) for one configuration and a single boot. Gate 4 — repeated boots, sleep/wake, sustained GPU/gaming load,
and separately App Store and iMessage — is not qualified, and internal-disk persistence is left to the operator.

## 3.0.18

**Not released.** This section describes the branch being prepared as 3.0.18, on top of 3.0.17 (below, not released
either). It brings in everything the original project ([NullMoth](https://github.com/nullmoth/nvidia-macos-driver))
has published up to its 3.1 (commit 900470d4, 2026-11-23), as a merge: until now its changes were picked one by one
(3.0, 3.0.4, 3.0.5) and its support for older macOS releases was left out. **Nothing of 3.0.18 has been built on a Mac
or run on a card.** Where the original and this fork had made the same fix, this fork's version is kept.

### From the original's 3.1

- **The lock screen and login window crashing in a loop** (SecurityAgent or loginwindow, "Floating point exception" in
  VFX, its #624 #569). The shader translator follows a value a shader keeps in a local structure and reads back in a
  helper (VFX keeps its acceleration structure that way), inlines helpers that return a texture or a pointer, and keeps
  ray-tracing kernels' structure through local objects. The original reports every VFX compute kernel building (312 of
  312). `NVMTL_INLINE_RETRY_DUMP=<file>` writes the module the inline retry produced (a diagnostic).
- **"Internal compiler error in NAK" on some large kernels**: a memory read wider than its alignment reached the last
  compiler stage. `nvk/nvk-nak-late-split.patch` splits it into reads the hardware accepts, and ray queries read a
  geometry entry as two 4-byte loads (`nvk/hwrt/ttu-rq-split.patch`; in the original this is an edit of
  `hwrt/ttu-rq.patch`, here a patch of its own so that 2.9's chain still rebuilds as shipped). Both are at the end of
  the 3.0 chain.
- **The accelerator and the display driver refusing to start, without a word, on the second start after a failed
  one** (its #633): every driver gets the same answer from the boot guard for the whole start.
- **Installing with no internet**: the installer finds NVIDIA's driver package (the GPU firmware's source) in
  Downloads, on the Desktop, in /Users/Shared or on a USB stick, continues a download that stopped, and prints the
  download's progress (its #644 #639 #598).
- **The Ryzen laptop power profile** (`tools/ryzenadj-macos`) is linked with the LC_UUID macOS 26 requires (its #590
  #631).

### From the original's 3.0, not taken before

- **Older macOS releases (experimental, as in the original).** The sources of its driver kits for macOS 14 Sonoma down
  to Mac OS X 10.7 Lion: `build/legacy/` (the kit builds, which need each release's own SDK and Kernel Debug Kit from
  Apple), `package/legacy/` (`install-legacy.sh`, `uninstall-legacy.sh`), `gl/` (OpenGL for those releases),
  `kexts/compat/nm_era.h`, `kexts/NVRM/accel/nvres_fields.h`, `plugin/nvmtl_avail.h`, `vendor/nvenc/src/nv_floor.h`,
  `nvk/nvk-legacy-floor.patch` and `NVK_FLOOR` in `build/build_nvk.sh`. A build for macOS 15 and 26 defines no
  `NM_ERA` and compiles what it compiled before. None of the kits has been built here or run on a card; this fork's own
  code since 2.9 has not been checked against the older layouts.
- **The panel brightness kept on disk** when the firmware drops NVRAM writes (OpenCore with `DisableVariableWrite` and
  no emulated NVRAM, its #582). The installer adds a job (`/Library/NullMoth/backlight.sh`,
  `com.nullmoth.backlight`) that saves the level and, at the next start, restores it through
  `debug.nvrmfb_backlight_restore` unless the user already moved the brightness. On a machine without an internal
  panel the name answers nothing and the job ends after two minutes.
- **`gfxrst=4` with WhateverGreen**: when WhateverGreen is loaded and the boot-args set no `gfxrst`, the installer adds
  `gfxrst=4` to the NVRAM boot-args (WhateverGreen's boot screen copy can panic the Mac when the NVIDIA display first
  opens) and records it, and `uninstall.sh` takes out only that. An OpenCore config that writes its own boot-args at
  every start replaces the NVRAM value; the installer still says to add it there.
- `DEVELOPING.md` and the original's test workflow (`.github/workflows/tests.yml`). Its `CODEOWNERS` is not taken.

### What changed here to take it

- The page-off path and this fork's own reads of the accelerator's resource object use the original's field names
  (`kNvResType`, `kNvResValid`, `kNvResWidth`, ...) and `NV_FAM_PAGEOFF`; on macOS 15 and 26 they are the same offsets
  and the same call.
- `debug.nvrmfb_backlight_restore` is registered with the display driver's other sysctls, after the reference that
  keeps the kext loaded (the original registered it when the panel's handler was published), and answers `ENODEV`
  until a panel's handler exists.
- `tools/switch_registry.py`: the one writable sysctl a row after 2.9 may have is a write-only channel that a job this
  driver installs writes, tier internal (`CHANNELS`: only `debug.nvrmfb_backlight_restore`); every other rule is
  unchanged. New rows: that sysctl, `NVMTL_INLINE_RETRY_DUMP` (diag) and `NVK_FLOOR` (build host setting).
- A duplicate of the utilization sampler's hang limit (`kUtilHangNs`, already in `nvrm_utilization_pace.h`) that the
  merge brought into `NVRM.cpp` was removed.

### The ways back

None new: nothing of 3.0.18 adds a path that needs one. The backlight job is removed with
`launchctl bootout system/com.nullmoth.backlight` and its two files; `uninstall.sh` removes it and the `gfxrst=4` it
added.

### What ran where

On Linux only. `build/kext_check.sh` compiled every kext source (NVRM, NVAccel for 15 and 26, NVRMFB, NVRMAGDC and the
panel drivers) with no error or warning, and every import resolves on xnu-11215.1.10 and xnu-12377.1.9. The 3.0 NVK
chain applies to Mesa 26.3.0-rc3 without fuzz, also with each of `nvk-mesa-26.3.0`, `nvk-nak-fixes-main` and
`nvk-nak-sched-main` left out, and gives the same tree as the original's edit of `hwrt/ttu-rq.patch`. The host suite
(`tools/run_all_tests.py`): the tests the merge broke were fixed (field names in the lifted page-off code, the
encoder's main-port name, the chain's order); `test_backlight_keeper.py` (from the original) fails 3 of its arms on
Linux on the original's own tree as well. The translator was built and its library tests run. The plugin, the
translator's macOS build and NVK were not built for macOS, and nothing ran on a card.

## 3.0.17

**Not released.** This section describes the branch being prepared as 3.0.17, on top of 3.0.16 (below, not released
either; 3.0.15 was closed without merging). It turns on the GPU's hierarchical
Z, which NVK already drives on Linux. **Nothing of 3.0.17 has been built on a Mac or run on a card**; the decision it
adds is host-tested and the changed code was compiled on Linux against the real RM headers only.

### Fixes and changes

#### NVK: hierarchical Z (ZCULL) over NVRM

- **NVK now uses the GPU's ZCULL unit on macOS.** ZCULL keeps a coarse copy of the depth buffer on the chip and throws
  away whole blocks of fragments that are behind depth already drawn, before they are shaded. NVK 26.3 has the code
  (`nvk_image.c` gives a depth image a ZCULL plane, `nvk_cmd_draw.c` programs the unit at the start of a render pass),
  but it runs only when the device reports the unit's geometry, which on Linux the kernel does
  (`DRM_NOUVEAU_GET_ZCULL_INFO`). The NVRM backend reported none, so ZCULL never ran here. The new patch
  `nvk/nvk-zcull.patch`, last in the 3.0 chain, changes `nvkmd_nvrm_pdev.c` only: at device creation it asks RM for the
  same figures, `NV2080_CTRL_CMD_GR_GET_ZCULL_INFO` and the ZCULL engine's context properties, and fills
  `nv_device_info.zcull_info` field by field as Linux's GSP code does (`r570_gr_get_ctxbufs_and_zcull_info`). Every
  graphics channel has bound a ZCULL context-switch buffer `SEPARATE_BUFFER` since 2.9 (`NVRM_610_ZCULL`), so the
  unit's contents survive a switch to WindowServer or another application.
- **Where it acts.** NVK keeps a ZCULL plane only for a depth image whose usages are depth attachment and read
  (sampled, input attachment, copy source); a render pass with such an image keeps ZCULL across passes, and any render
  pass whose depth is cleared at load uses ZCULL for that pass. A depth image that can also be a copy destination gets
  no plane, because NVK cannot keep the coarse copy right after a copy into it (upstream's rule, unchanged). Zink
  (OpenGL) and the Metal plugin create their depth images as copy destinations, so for OpenGL games and Metal
  applications ZCULL acts in the passes that clear depth at load, which is how a frame usually starts; a native Vulkan
  application that creates its depth image with the usual usages gets it in every pass.
- **Kept off** in WindowServer (it draws no depth worth culling, and a fault there takes the desktop), with
  `NVRM_610_ZCULL=0` (no context buffer), when either RM call fails or returns a size of 0, and when a figure is one
  that NIL would divide by or that passes its 16 subregions (an assert there would end the process).
  `NVK_ZCULL_LOG=1` (or `NVK_DIAG=1`) writes one line per device on stderr with the decision, the reason when it is
  off, and RM's figures.

What it gives is not measured. It costs nothing where nothing is hidden and gains most where much is drawn behind
other things (dense scenes, foliage, shader packs that draw the scene more than once). If a frame shows things through
walls or flickering depth, `NVK_ZCULL=0` is the first thing to try.

### The ways back

| Way back | Kind | What it does |
|---|---|---|
| `NVK_ZCULL=0` | settings line (or environment) | NVK reports no ZCULL unit and draws without hierarchical Z, as in 3.0.16 |

### What ran where

On Linux only. `tools/test_nvk_zcull.py` (new) checks the patch's place in the chain and the one file it changes,
that each of RM's figures lands in the field of the same meaning, that the decision is the one the device setup
stores, and compiles the decision out of the patch with `-Wall -Wextra -Werror` and runs it over 20 cases. The changed
code was compiled on its own with clang and gcc against the RM 610.57.04 headers and Mesa 26.3.0-rc3's
`nv_device_info.h`; the patch applies at the end of the 3.0 chain on Mesa 26.3.0-rc3. `test_nak_stats.py` (its chain
check now reads "after nvk-tex-uhandle-safe"), `test_nvk_chain.py`, `test_nvk_nak_switch_key.py`,
`test_switch_registry.py`, `test_build_nvk_fingerprint.py`, `test_tree_layout.py` and `tools/switch_registry.py check`
pass. The whole NVK library was not cross-built for macOS, nothing was built on a Mac and nothing ran on a card.

## 3.0.16

**Not released.** This section describes the branch being prepared as 3.0.16, on top of 3.0.14 (below; 3.0.15 was
tried and not kept, and its number is not used again). In OpenGL on Zink, a buffer upload that would make the game's
thread wait for the GPU goes through a staging buffer instead. It is on by default and has one way back,
`NVK_ZINK_SUBDATA_STAGE=0`. It was built on the RTX 5050 machine and its Zink library copied by hand, but **the card
has not yet shown the wait it removes while 3.0.16 was in place** (below, "What ran where").

### Fixes and changes

#### Zink: glBufferSubData no longer waits for the GPU

- **What the card showed.** In ATM10 with a shader pack on the RTX 5050 (the captures of 3.0.15 under 3.0.14 below),
  the game's render thread spent 19 to 25 % of its time asleep in `glBufferSubData` (or a `glBufferData` of the same
  size, which Mesa turns into the same call): `tc_buffer_subdata` → `tc_buffer_map` → `zink_buffer_map` →
  `zink_wait_on_batch` → `nvrm_evw_nvk_wait`. That is about 1.9 ms a frame at 75 to 79 frames a second. The wait
  does not come every time: a capture with the same versions installed (3.0.12, NVK of 3.0.15), taken in the same
  place minutes before, had none, with the GPU at 96 % instead of 63 to 68 %. What the game draws when it starts was
  not found; the captures of 3.0.6 and 3.0.7 do not show it.
- **Why it waited.** Mesa's threaded context (`u_threaded_context.c`) normally writes a busy buffer through a staging
  buffer and a copy queued on the GPU. It maps the buffer itself, and waits, in two cases. The first is a write of more
  than 320 bytes into a buffer the application has mapped (`PIPE_MAP_DIRECTLY`, which keeps the threaded context from
  discarding the range). The second is a write it infers to be unsynchronized (a range never written, or an idle
  buffer) that overlaps the range of a staging upload not executed yet. `tc_buffer_map` then takes the unsynchronized
  flag away, and the mapping waits for the buffer's whole batch. Mesa's record of that range is never reset, so the
  overlap is often only with an upload that is long done. Which of the two the game hits was not measured.
- **What changes.** `zink/mesa-zink-nvk.patch` adds `tc_subdata_stage_instead` to `u_threaded_context.h` and an option
  of the threaded context, `subdata_never_wait`. Zink sets the option on NVK on macOS. With it, `tc_buffer_subdata`
  writes those two cases through the threaded context's own staging path. A subdata call writes every byte of its
  range, and the copy is queued after every earlier call, so the buffer ends up as the mapping would have left it.
  The range becomes valid when it is copied. Excluded, as before:
  - a buffer of user memory, with CPU storage, in a client pointer heap, sparse or unmappable;
  - a persistent mapping;
  - a write the threaded context already stages;
  - a write of 320 bytes or fewer that is not unsynchronized, which is queued as it is.
- **What the application could see.** With a buffer the application keeps mapped, the bytes `glBufferSubData` writes
  reach the mapping when the GPU executes the copy, not when the call returns. The GL specification asks an
  application to wait on a fence before it reads what the GL wrote into a mapped buffer. A program that reads its
  mapping right after `glBufferSubData` without one would see the old bytes. `NVK_ZINK_SUBDATA_STAGE=0` is for that
  case.

### The ways back

`NVK_ZINK_SUBDATA_STAGE=0` in the settings file (`/Library/GPUBundles/nvmtl/nvrm610.conf`), or in the application's
own environment with `NVMTL_PROCESS_ENV=1`, as for the other switches of the Zink frontend. A process reads it once,
at its first GL context. With it the option is left off and `tc_buffer_subdata` runs as in 3.0.14.

### What ran where

On Linux only:
- `build/prepare_zink.py` applies the patch to the pinned Mesa 26.3.0.
- `u_threaded_context.c`, `zink_context.c` and `zink_resource.c` compile with GCC without a warning. Zink's lines that
  set the option, and the frontend's reader in `zink/nvk_present.c`, are macOS-only and were not compiled.
- The new host test `tools/test_zink_subdata_stage.py` passes and fails on 3.0.14. It checks the decision function
  over every case named above, the code around it, the switch, and eight broken copies, each of which has to fail.
- `tools/switch_registry.py check` passes.

On the card (2026-11-24): the package of 394be13a was built on the RTX 5050 machine without a warning, and
`zink/libnvk_zink.dylib` copied by hand over 3.0.12 with NVK of 3.0.14. Two captures at the place in ATM10 above, with
the shader pack, as installed and with `NVK_ZINK_SUBDATA_STAGE=0`: in both the render thread did not wait in
`zink_buffer_map` (no sample), the GPU ran at 98 and 96 % (120 and 117 W) and the display flipped 102 and 97 times a
second, at its 100 Hz. The state that waits did not come in either, so these captures neither prove nor refute the
change; they show it changes nothing where there is no wait. The capture that decides it is one taken while the wait
is there (the GPU below about 80 % at that place).

## 3.0.14

**Not released.** This section describes the branch being prepared as 3.0.14, on top of 3.0.13 (below, not released
either). It is phase 0 of the shader-compiler work: an instrument that changes nothing the driver does. Every later
change to NAK is to be judged by the same figures over the same shaders, and accepted or reverted on them, so the
compiler now writes those figures one shader per line, a fixed set of shaders lives in the repository, and a script
compares two runs. **Nothing of 3.0.14 has been built on a Mac or run on a card**; it is host-tested only.

### Fixes and changes

#### NVK: per-shader statistics for compiler work

- **`NAK_STATS=1` writes one JSON line per compiled shader stage on stderr.** The new patch `nvk/nvk-nak-stats.patch`,
  last in the 3.0 chain, changes `src/nouveau/compiler/nak/api.rs` only. After the shader is finished and before it is
  encoded, the compiler writes `{"nak_stats":1,"stage":..,"sm":..,"blake3":..}` and then `instrs`, `instrs_warp`,
  `instrs_uniform`, `gprs`, `warps_per_sm`, `static_cycles`, the four spill and fill counts and `slm`. NAK already
  counted most of these (`NAK_DEBUG=print` and Vulkan's pipeline executable statistics show them); new are the split
  between the uniform data path and the main one (`instrs_uniform` counts the instructions whose destinations are
  uniform registers) and the one-line form. `blake3` is the hash of the shader's SPIR-V. A compute shader that NAK
  builds twice reports the build it keeps. Unset, or set to anything but 1, nothing is read from the shader and
  nothing is written. The switch changes no code, so it is not in the shader cache key (`tools/test_nvk_nak_switch_key.py`
  lists it with the other switches that rightly are not): a shader served from a cache prints nothing.
- **A corpus of shaders and a runner, `tools/nak-corpus/`.** 150 graphics pipelines (300 shader stages): the 30
  written by hand for the 3.0.12 fixes and 120 that `gen.py` writes (many textures, branches, loops, shadow, cube,
  array, 3D, offset, gradient, fetch and gather forms, most of them under register pressure). `run.sh <out.jsonl>
  [NAME=value ...]` builds `pipetest.c`, compiles every case once with the caches off and `NAK_STATS=1`, and writes
  one line per stage with the case's name added; it needs glslangValidator and a Vulkan loader pointed at the NVK to
  measure. `baseline-sm120-host.jsonl` is the run of this release's NAK on Linux (Mesa's nouveau drm-shim as a GB207,
  shader model 120). Not in the corpus, because they cannot be put in the repository: the shaders of a game's shader
  pack (the census of ATM10 with Complementary Unbound was kept outside it) and Apple's Core Animation kernels.
- **`tools/nak_stats_diff.py BASE NEW`** prints, for instructions (all, main pipe, uniform), registers, warps per SM,
  spills and static cycles, the total over the shaders both runs have, the change, how many shaders went up and down,
  and the shaders that changed most. `--gate-instrs PCT` fails when instructions rise by more than PCT percent and
  `--gate-spills` when any shader spills more than before. Pairs of instructions issued together are not counted:
  the GPUs this driver supports have no dual issue (NAK models it only for Kepler).

What it showed on the corpus, as a check of the instrument: two runs of the same build give the same file byte for
byte. With `NAK_TEX_UHANDLE=1` (off by default), instructions go down 0.48 % (main pipe down 1.68 %, uniform up 0.63 %)
and static cycles 0.43 %, but 71 stages spill more registers (spills to registers, none to memory), so
`--gate-spills` refuses it.

### The ways back

Nothing is on by default. Leaving `NAK_STATS` unset is the old behaviour.

### What ran where

On Linux only. The fork's NVK and NAK (Mesa 26.3.0-rc3 with the whole 3.0 chain, this patch included) were built for
x86_64 on Mesa's nouveau drm-shim as a GB207; every corpus case compiled, with and without `NAK_TEX_UHANDLE=1`. The
new host test `tools/test_nak_stats.py` passes and fails on 3.0.13; `tools/test_nvk_nak_switch_key.py`,
`test_nvk_chain.py`, `test_switch_registry.py`, `test_build_nvk_fingerprint.py`, `test_package_licenses.py`,
`test_tree_layout.py` and `tools/switch_registry.py check` pass. The patch was not cross-built for macOS, nothing was
built on a Mac and nothing ran on a card.

### 3.0.15, tried on top of 3.0.14 and not kept

[PR #20](https://github.com/TheFormidable/nvidia-macos-driver/pull/20) was phase 1 of the shader-compiler plan:
a heavy graphics shader below 32 warps per SM was built a second time with fewer registers, and that build was kept
when it gained warps for at most 5 % more instructions. The switch was `NAK_OCC_TARGET=0`, and the pick never ran
in WindowServer. On Linux it met the instruction gate. Of ATM10's 409 stages with Complementary Unbound, 27 fragment
stages gained warps (28 % more over them) for 2.6 % more instructions, but they went from no spill to local memory to
4510 spill and fill instructions.

On the card it did not meet the plan's criterion (the same frames for less GPU or fewer watts), so it was closed
without merging and its patch is not in the tree. The test, on 2026-11-23 on the RTX 5050: NVK 3.0.15 was copied by
hand over an installed 3.0.12. The place in ATM10 with the shader pack and the view were fixed. `fps_capture.sh` ran
20 s per capture, in the order on, off (`NAK_OCC_TARGET=0` through `launchctl setenv`), on:

| capture | phase 1 | game frames/s | watts | GPU % | joules per frame |
|---|---|---|---|---|---|
| 175040 | on | 75.4 | 96.9 | 63.2 | 1.285 |
| 175746 | off | 77.0 | 98.8 | 66.7 | 1.283 |
| 180425 | on | 78.8 | 100.6 | 67.8 | 1.277 |

Game frames are the frames that went through Zink's surface fences (`debug.nvaccel_surface_fence_waits`). The GPU
clock was 2912 to 2925 MHz throughout, with no Xid. The two "on" captures differ from each other by as much as either
differs from "off". A first capture, taken with phase 1 off, is left out: only 34 frames went through the surface
fences in 20 s while the display flipped 102 times a second and the GPU ran at 96 % and 121 W, so the screen showed
something else.

What limited that scene was the game's render thread, not its shaders. In the three captures it spends 19 to 25 % of
its time asleep in `glBufferSubData`, in `tc_buffer_subdata` → `tc_buffer_map` → `zink_buffer_map` →
`zink_wait_on_batch` → `nvrm_evw_nvk_wait`. That is Zink waiting for the GPU to finish with a buffer before the game
may write it again.

The version number 3.0.15 is not used again. On the card the compiler's own figures counted for less than this wait.

## 3.0.13

**Not released.** This section describes the branch being prepared as 3.0.13, on top of 3.0.12 (below, not released
either). Two things that were left to the user are now done by default: the kernel driver puts write-combining back
into the CPU's page attribute table where an AMD kernel patch took it out, and the installer fetches the DLSS files.
**Nothing of 3.0.13 has been built on a Mac or run on a card.**

### Fixes and changes

- **Write-combined mappings are write-combined again on machines with a fix-PAT kernel patch.** Since 3.0.6
  `debug.nvrm_pat_wc` has said that on an AMD machine with AMD_Vanilla's default "Fix PAT" patch (Algrey / Zormeister)
  entry 6 of the page attribute table is uncached-minus, so every CPU write through BAR1 (NVK's mapped video memory,
  the kernel's CPU fill of the framebuffer) leaves the CPU one store at a time. The fix was the user's: a byte in
  OpenCore's Replace. NVRM now makes it itself. When the table it reads at start has anything but WC in entry 6, and
  holds no reserved encoding, it writes on every CPU at once, inside a rendezvous with interrupts off
  (`mp_rendezvous_no_intrs`), the one change stock XNU's `pat_init` makes: entry 6 to WC, the other seven entries as
  they were. Each CPU follows the order of XNU's own `mtrr_update_action`: no-fill cache mode, write back and
  invalidate the caches, flush the TLB, write the table, flush both again, restore CR0 and CR4. The patched kernel
  writes its table back on every CPU at wake, so NVRM does the pass again at the start of `setPowerState` for a
  power-on, before the GPU resumes, where the start pass changed something. On the default AMD_Vanilla table the
  result is 0x0001010600070106, the table a test boot from a USB stick with that byte changed ran with on a Ryzen 5
  5500 and an RTX 5050 under macOS 26.7.3 on 2026-11-21: it booted normally, and in ATM10 by Zink the main thread's
  share in `glBufferSubData` uploads fell from 11-14 % to 1.4 % and in importing the window's surface from 7-8 % to
  1.2 % (frames per second were not compared like for like). A Mac with macOS's own table, or a machine where entry
  6 is already WC, sees no write and no rendezvous at start. The verdict sysctls now describe the table in force after
  the change, `debug.nvrm_pat_found` the one the kernel wrote, `debug.nvrm_pat_passes` how many passes changed a CPU
  (start, then one per wake) and `debug.nvrm_pat_cpus` how many CPUs had WC in entry 6 after the last one (compare with
  `hw.ncpu`). A CPU that restarted on its own outside a system wake would come back with the kernel's table; macOS
  does not do that on x86, and the counter would show it. Checked with `tools/test_nvrm_pat.c` (the change on every
  named table, reserved bits, the other entries kept) and `tools/test_nvrm_pat.py` (the one MSR write under `kexts/`,
  its order, its callers and the boot argument).
- **The DLSS files are installed by default.** `install.sh` ran `dlss_fetch.sh` only with `NULLMOTH_DLSS=1`; it now
  runs it unless `NULLMOTH_DLSS=0` is set. The step downloads NVIDIA's own files (about 750 MB the first time; an
  install with the same versions in place downloads nothing), checks their pinned hashes and builds the DLSS kernels
  for the card, as before. With the files present the Metal plugin already served the MetalFX temporal upscaler with
  DLSS Super Resolution and the MetalFX frame interpolator with DLSS Frame Generation without any switch, so a Metal
  game that uses MetalFX gets them with no setting. A failure in the step still leaves the driver installed.
- **No onboard sound on macOS 26 with `alcid` in boot-args: the installer says so, and the README explains it.** macOS
  26 has no AppleHDA, so AppleALC does nothing there, and the audio driver (NMXAudio) leaves the onboard controller
  alone while `alcid` or `alcverbs` is set: only the NVIDIA display outputs appeared, with no speakers, headphone jack
  or microphone (an RTX 5050 on 26.7.3 with `alcid=66`; removing it brought them back). This was not written down
  anywhere. `install.sh` now prints a note when it installs NMXAudio and boot-args carry either argument, and the
  README's OpenCore section says what to remove (`tools/test_install_alc_note.py`).

### The ways back

| Change | Way back |
|---|---|
| Entry 6 of the page attribute table put back to WC | boot argument `nvpatwc=0`: the table is left as the kernel wrote it, as in 3.0.12 |
| DLSS files installed by default | `sudo env NULLMOTH_DLSS=0 ./install.sh` skips the step; `uninstall.sh` removes `/Library/NullMoth/dlss` |

### What ran where

On Linux: `tools/test_nvrm_pat.c` and `tools/test_nvrm_pat.py` pass; the switch registry check passes with the new
boot argument and the three new read-only sysctls. The 68 host tests that read `NVRM.cpp`, `install.sh` or the switch
registry give the same result as on 3.0.12 (the 11 that fail there fail on 3.0.12 too, in this shallow clone or as
root). The new kernel code was compiled for x86_64 macOS with `-fapple-kext -mkernel` in a stub translation unit,
and its disassembly has the instructions in the order above; NVRM.kext itself was not compiled. `dlss_fetch.sh` is
unchanged.

## 3.0.12

**Not released.** This section describes the branch being prepared as 3.0.12, on top of 3.0.11 (below, not released
either). It is two fixes in NVK, each a patch of its own at the end of the 3.0 chain, and neither changes what the
driver does while no switch is set. Both were found by reading the shader compiler (NAK) and NVK's shader caches, and
both were then reproduced on Linux, where this NVK and NAK were built on Mesa's nouveau drm-shim as a GB207 (shader
model 120): pipelines compile there without a card, and nothing is drawn. The measurements below were made on 3.0.8's
code; 3.0.9 and 3.0.11 changed nothing in NVK, so they hold for 3.0.11's. **Nothing of 3.0.12 has been built on a Mac
or run on a card.** No frame rate changes with it, and none was expected to.

**Since this section was written:** 3.0.12 was built on the Mac of the 3.0.7 captures on 2026-11-22 (NVK and the
components built from it were rebuilt, with no warning: the first time the two patches went through the `patch` of macOS, Apple's clang and the
Mac's Rust), `tools/test_nvk_nak_switch_key.py` and `tools/test_nvk_chain.py` pass there, and the package was installed
there with the installer and a restart: `/Library/NullMoth/driver-version` reads 3.0.12 and the installed NVK equals
the package's. `tools/test_nak_tex_uhandle_safe.py` was not run there. Nothing else below changes: the sentences that
say nothing of it was built on a Mac are of before that.

### Fixes and changes

#### NVK: the shader caches and the compiler's switches

- **A compiler switch that is turned on or off no longer gets binaries compiled under the other setting.** NVK's
  shader cache key (`nvk_physical_device_compiler_flags`) feeds `pipelineCacheUUID`, `shaderBinaryUUID` and the disk
  cache's driver flags. Of the switches NullMoth's patches read between SPIR-V and the cached shader, only
  `NVMTL_NAK_ARR_SCRATCH` was in it, beside what upstream Mesa keys there (`NAK_DEBUG`, two flags of `NVK_DEBUG` and
  one drirc option). A process that set any other one was served what an earlier process had compiled without it, from
  the disk cache and from the pipeline cache data that Zink keeps in a cache of its own, and the other way round. On
  the Linux build, with 3.0.8's code and one disk cache, a process with `NVMTL_NAK_NO_PREPASS=1` got a cache hit and a
  fragment shader of 11 instructions and 94 static cycles, where a compile under that switch gives 13 and 129; a
  process with `NAK_TEX_UHANDLE=1` got a hit too, which hid the failure of the next item. The new NVK patch
  `nvk/nvk-nak-switch-key.patch` puts a 32-bit digest (FNV-1a) of the name and value of every such switch that is set
  into bits 16..47 of the key, for the two identifiers and for the disk cache alike. It covers 35 switches: 34 read
  with `getenv`, listed in the patch, and `NVMTL_NVK_NO_LCONST16`, read as the compiler reads it, from the settings
  file too. The switches that only log are in, so that asking for a log compiles. Two of the 34 are upstream Mesa's
  own, for its SPIR-V reader, and upstream keys them nowhere: `MESA_SPIRV_FORCE_UNSTRUCTURED`, and
  `MESA_SPIRV_READ_PATH`, which swaps a module for a file after the module's hash was taken. With 3.0.8's code a
  process that did not set the second was then served the replacement's binary (21 instructions where its own shader
  compiles to 11), and one that set the first was served the default's (62 instructions for 68). For
  `MESA_SPIRV_READ_PATH` it is the value as written that is in the key, not the folder's files: after a file there is
  added, changed or removed, or when the value is a relative path used from another folder, clear the cache. Three of
  NullMoth's are left out with their reasons in the test: two change only the QMD, which is filled at each dispatch, and
  `NVK_DUMP_SHADER_DIR` acts when a shader is uploaded, which a cached one is too (as upstream's `NV50_PROG_DEBUG`
  does). **What it changes:** with no switch set the digest is 0 and the key is computed exactly as in 3.0.8. With one
  set, the process compiles its shaders once under a key of its own and finds them again at its next start, and a
  process without the switch finds the default key's entries as they were. The value is hashed whole, so a value that
  means the same as unset (`=0` for most), or the same as another value, still has a key of its own: one more compile,
  never a wrong binary. **Not covered:** the key is taken when NVK makes its device, from the environment as it is then,
  and the compiler reads its switches later: some at every compile, many once for as long as the library stays loaded. A
  switch that enters or leaves the process's environment in between is compiled under a key that does not say so, the
  default key included, as on 3.0.8. The Metal plugin does change the environment when it loads: it sets what the
  settings file and the application's profile name and, in a release build, forgets what was inherited unless
  `NVMTL_PROCESS_ENV=1`. In a Metal process that happens before NVK makes its device, so a switch from the settings file
  or from a profile counts there like one set by hand. In a process that reaches NVK through Zink, or in a Vulkan
  application on the frontend library, which of the two comes first on a Mac was not determined: the frontends
  themselves ask Metal for its devices only after NVK has made one (`zink/native_drawable.m`,
  `tools/nvk-bridge/bridge.mm`), and Apple's own renderer, which runs before Zink is brought in, may have loaded the
  plugin by then. No switch. Checked with `tools/test_nvk_nak_switch_key.py`.

#### NAK: the uniform texture handle experiment

- **With `NAK_TEX_UHANDLE=1`, a texture whose handle left its uniform register compiles.** The experiment (off by
  default, shader model 100 and later) keeps the handle of a TEX in a uniform register, and the encoder asserts that
  it is one. Two passes could give that source a warp register instead: `opt_uniform_instrs`, when it lowers the
  instruction that loads the handle to a warp one (reproduced with the two loads that cannot be uniform: an array of
  samplers indexed by a uniform value, and a descriptor past the part of its set that is mapped as a constant buffer),
  and `spill_values`, when more uniform values are live than there are uniform registers and the texture is in a
  non-uniform block. The compile then panicked: NVK answered `VK_ERROR_UNKNOWN` for the pipeline, and the process
  aborted only under `NAK_DEBUG=panic`. The new NVK patch `nvk/nvk-tex-uhandle-safe.patch` puts such a texture back
  into the layout `from_nir` already builds for a handle that is not uniform (the handle first in the second source,
  the split of the coordinates undone): in the texture's legalize for the first case, and in `assign_regs` after the
  uniform registers were spilled for the second, where a value that ends up twice among the texture's vector sources
  gets a copy. The encoder and its assertion are unchanged. On the Linux build, of 25 test pipelines 17 failed with
  the switch set on 3.0.8's code, and all 25 compile with the patch; 4 of the 17 then compile to exactly the code the
  compiler gives with the switch unset, and the other 13 to code of their own. **With the switch unset nothing
  changes:** the handle is then always Zero, the new function returns before it writes anything and the loop in
  `assign_regs` skips every block; the assembly and statistics NAK prints for the 25 pipelines are the same before and
  after the patch. This does not make the experiment ready to turn on: its encoding rests on one reference instruction
  and has not run on a card, and it is not a gain everywhere: with the switch set the static cycle count goes up on 10
  of the 25 test pipelines (by up to 84, 495 to 579), down on 7 and stays on 8. No switch. Checked with
  `tools/test_nak_tex_uhandle_safe.py`.

Two comments in `nvk_shader.c` are now out of date and stay as they are, because a patch after the three upstream-fix
patches may not touch that file (`tools/test_nvk_chain.py`): "bits 15..47 are free" of the key, and
`NVMTL_NVK_NO_LCONST16` "not in the shader cache key". The patch header and `docs/NVK-BASE-AND-HEADERS.md` say so.

### The ways back

None new. With no switch set, the cache key and the compiler's output are what 3.0.11 computes, so there is nothing to
turn off; leaving `nvk-nak-switch-key` or `nvk-tex-uhandle-safe` out of the chain in `build/build_nvk.sh` restores
3.0.11's code. As with every new build of NVK, the library's build id changes, so the shader caches of 3.0.11 are not
reused and shaders are compiled again once.

### What ran where

**Nothing of 3.0.12 was built on a Mac or ran on a card.** On Linux: both patches apply to the tree with the rest of
the chain applied, without fuzz or offset, also with any of the three upstream-fix patches left out, and
`nvk/dlss/patch_dlss_cache.py` still applies after them. The whole library (C, C++ and Rust, 942 units) cross-builds
for macOS with LLVM 18 and rustc 1.97 with 0 errors and 0 warnings, and leaves the same 13 symbols unresolved as
before (the Objective-C file of the Metal WSI, which that build does not compile). The Linux build is the same chain
with upstream's nouveau back end added beside the NVRM one, so that the drm-shim can stand in for the kernel; NAK's
sources in it are byte for byte the chain's. On it: the disk cache with a switch turned on and off between processes,
with the results above (before the patch a hit with the other setting's binary, after it a miss, a compile, and a hit
at the next start, and the default entries found again), the same for the two switches of Mesa's SPIR-V reader;
`NVMTL_NAK_ARR_SCRATCH` keeps a key of its own as before, and `NAK_SMEM_TARGET_CAP_KB`, which is left out, still hits
the default entries; the 25 pipelines of the second item with the switch set and unset; and 125 more shaders written
for the review of this change (many textures, both arms of a branch, loops, the shadow, cube, array, 3D, offset,
gradient, fetch and gather forms, sampler arrays with a dynamic index), each compiled with and without NAK's forced
spilling: with the switch set 84 of the 250 compiles failed on 3.0.8's code and none fails with the patch, and with
the switch unset the 250 outputs are the same before and after it. `tools/test_nvk_nak_switch_key.py` replays the
reads of the 35 patches of the chain and builds the patch's own digest code with a stand-in environment (25 arms),
against 26 copies of the tree with one rule broken each, 24 of which have to fail the arm that names their rule and no
other and two of which have to pass. `tools/test_nak_tex_uhandle_safe.py` builds the patch's own Rust against
stand-ins for the NAK types it uses and runs it over 13,068 cases (every shape of a texture's sources with a Zero, a
uniform and a warp handle, then two such textures in one block and in two), against 13 copies with one rule broken
each, every one of which fails as it has to, and reads the patch for where its two calls are. The repository's whole
host suite was run on Linux with the change: 349 of its 417 tests pass, and the 46 that fail are the same 46 that fail
there without it. Not checked: a build with Apple's clang and the Mac's Rust, the patches under the BSD `patch` of
macOS (their headers pass the check `tools/test_nvk_chain.py` has for the upstream ones, which both tests now make for
these two), any shader of a real application, any stage other than vertex and fragment, and anything that needs the
card: no image was compared, and the uniform-handle form itself was not run.

## 3.0.11

**Not released.** This section describes the branch being prepared as 3.0.11, on top of 3.0.9 (below; 3.0.10 was
another branch's number, which became 3.0.12). It carries what 3.0.9 did for the native Vulkan frontend's IMMEDIATE
swapchains to the two places next to it: OpenGL on Zink with the swap interval at 0, and the Vulkan frontend's MAILBOX
mode. **Nothing of it has run on a card**; what was checked and what was not is at the end.

**Since this section was written:** 3.0.11 was built on the Mac of the 3.0.9 measurement on 2026-11-22 (the Vulkan
frontend and Zink were rebuilt, with no warning; the tests of this change that were run there pass) and installed
there, and the Mac restarted without a fault. In Minecraft without mods on OpenGL through Zink its owner then read 490
frames a second on the game's own counter. That is a reading, not a measurement: no capture was taken, and there is
no figure from before on Zink to set it against. What "Not checked" lists below still stands.

### Fixes and changes

#### OpenGL on Zink: a swap with the interval at 0 no longer waits for the display

Through 3.0.9 a swap of an OpenGL context on Zink (`present_drawable`, `zink/nvk_cgl.c`) waited for the frame before it
to be published and then asked the window's layer for its next drawable itself, on the application's thread. The layer
hands a drawable out only when the compositor has given one back, so with vertical sync off an application still drew
no faster than about two frames for each composition of the display: the same tie that held Minecraft's Vulkan
renderer at 200 frames a second on a 100 Hz display before 3.0.9. That figure was measured on the Vulkan frontend; for
Zink the tie is read from the code and was not measured.

Since 3.0.11 such a swap leaves its frame parked and returns (`zink/nvk_newest.h`):

- The swap copies the frame into the context's scratch image and flushes, as before, then waits for the GPU to have
  finished the frame parked before this one, and returns. It waits for nothing of the display. An application stays
  one frame ahead of the GPU, as it was (`nvk_zink_present_park`, `zink/nvk_zink.c`).
- A sender thread does the rest of what the swap did: it waits for the frame in flight to be published, asks the
  layer for a drawable, and hands over whatever frame is parked when the layer answers, the newest
  (`nvk_zink_present_send`). The copy, the wait for the GPU, the mark, the tag and the drawable's present are the code
  of 3.0.7, on the presentation thread. A frame that a newer one replaced before the layer had room is never copied.
- The last frame an application draws is always shown: the sender runs until nothing is parked. An application that
  draws only when something changes loses nothing.
- A front-buffer flush (an application that draws without a back buffer and shows its drawing with `glFlush`) parks
  the front buffer the same way. Through 3.0.9 each such flush waited for its own frame to be on the layer.
- Only the plain case goes this way: a context with a view, with the interval at 0, after a frame that was presented
  by its own swap or flush one frame ahead. The first frame of a context, a resize, a view that goes away, a
  diagnostic observer, a surface NVK does not import ahead, an uncertain completion and the child of a fork are
  presented by the swap, as in 3.0.9, after the sender has been stopped. A context without a view (it presents
    through Apple's own swap, on the application's context and thread) is not changed and cannot be this way. A sender
  that cannot
  send gives up and the next swap presents by itself; after eight in a row with no frame sent in between the context
  stops parking.
- With the interval not 0 nothing changes. The interval is now read by the swap and passed to the layer's acquire
  (`nvk_native_acquire`), which no longer calls CGL, so a thread the context is not current on may ask.

One switch, on by default: a line `NVK_ZINK_PRESENT_NEWEST=0` in the settings file
(`/Library/GPUBundles/nvmtl/nvrm610.conf`) makes every swap present by itself, as in 3.0.9. It is read once per
process with the two it depends on; with `NVK_ZINK_PRESENT_AHEAD=0` no frame is parked either. When a context that
parked frames is destroyed it writes one line to stderr, `NVK Zink: newest_frames_drawn=N newest_frames_sent=M`.

Known limits. A frame the sender could not send is not sent again by itself: the next swap, or the next flush that
has new drawing, presents its own. With the window minimized or hidden the layer hands out no drawable; through 3.0.9
each swap then
waited up to a second, and now the application draws at its own rate while the sender waits. A change of the swap
interval, a resize and a context's destruction wait for a frame the sender is sending, which is as long as the layer
takes to answer (it gives up after a second) and the GPU to finish. The waits for the GPU are 5 s, as everywhere in
the Zink frontend, on a clock that a sleep of the machine can run out. Nothing paces the sender: it copies a frame
for every drawable the layer hands out, which can be two for each refresh.

#### The native Vulkan frontend offers MAILBOX

The frontend offered FIFO and IMMEDIATE and refused a MAILBOX swapchain, so an application that asks for MAILBOX to
run without a limit, and falls back to FIFO without it, was held to the display. MAILBOX is what 3.0.9's thread does:
the newest frame, one at a time. Since 3.0.11 it is on the list of present modes, after the other two, and a MAILBOX
swapchain gets the presentation thread of an IMMEDIATE one with the layer's display synchronization left on: the
application is not held to the display and the thread copies one frame for each refresh. On a device without a copy
queue of the frontend's own, and in the child of a fork, a MAILBOX swapchain presents as FIFO does.
`NVK_BRIDGE_PRESENT_NEWEST=0` takes MAILBOX off the list and refuses it again, as before 3.0.11. An application that
prefers MAILBOX to IMMEDIATE when both are offered will now choose it.

### What was checked, and what was not

- `tools/test_zink_newest.py` (new): the agreement between the swap and the sender, `zink/nvk_newest.h` as it is, with
  real threads: a frame parked while another is being sent is sent by the same sender; a stop waits for the frame
  being sent and leaves a parked one parked; a sender that gave up is reported to the next swap; 20000 frames at an
  uneven pace against a slow layer end with the last one sent, never two senders, nothing out of order. Plain and
  under the thread sanitizer, six copies of the header with one rule broken each (every one fails), and a reading of
  the adapter and the frontend for the order of their steps and for every path that has to stop the sender.
- `tools/test_zink_present_queue.py`: the half of `nvk_present.c` that presents one frame ahead, with the two new
  arguments of `nvk_present_ahead_submit`, passes as before; `nvk_zink_copy_vram` and `nvk_zink_present_native` are
  3.0.6's text, unchanged.
- `tools/test_nvk_bridge.py` and `tools/test_nvk_bridge_newest.py` pass with MAILBOX.
- The four files of the Zink frontend were compiled for macOS against the public 11.3 SDK and the pinned Mesa
  (objects only), and `nvk_cgl.c`, `native_drawable.m`, `bridge.mm` and `window_check.mm` pass a syntax check with
  `-Wall -Wextra -Werror` there. Nothing was linked or run on a Mac.
- **Not checked:** a frame on a card through either new path; the frame rate of any application; tearing or pacing
  on screen; a resize, a minimize, a fullscreen switch and a second display under the sender; a MAILBOX swapchain in
  any application. The card suite does not run them: `zink_win_check` sets the present observer, which makes every
  swap present by itself, and `window_check` (which now accepts MAILBOX on the list and no longer expects it to be
  refused) presents with FIFO and IMMEDIATE. The whole host suite was not run again; the tests above are the ones
  that read or run the files this branch touches, with `tools/test_switch_registry.py`.


## 3.0.9

**Not released.** This section describes the branch being prepared as 3.0.9, on top of 3.0.8 (below, not released
either). It is one change, in the native Vulkan frontend (`tools/nvk-bridge`, installed as `libnvk_minecraft.dylib`):
how a swapchain of present mode IMMEDIATE gets its frames to the window. It starts from a measurement, on the Mac the
captures below came from (AMD Ryzen 5 5500, RTX 5050, one 1920x1080 display at 100 Hz, macOS 26.7.3). Minecraft 26.3
without mods, on its Vulkan renderer through this frontend, with vertical sync off and no frame limit, stood at 200
frames a second: twice the display's rate. A `sample` capture of 21 s has the GPU 35 % busy and the game's render
thread 69.0 % inside the frontend's `vkQueuePresentKHR`: 47.2 % of the thread waiting in `-[CAMetalLayer
nextDrawable]`, 11.0 % waiting for the copy's fence, 5.9 % importing the drawable's video memory and 2.2 % freeing it;
the game's own work was 31 %. The display took 98 frames a second and the frontend presented about 196: the layer,
which has three drawables at most, handed out two for each composition of the screen, and every present waited for one
on the game's thread. So the frame rate was a multiple of the display's rate whatever the game and the GPU could do,
in the mode whose meaning is that the application does not wait for the display. The frontend's source had not changed
since 3.0 added it. **Nothing of 3.0.9 has been built on a Mac or run on a card, and no gain has been measured.** What
the game reaches without that wait is for a capture on the card to say; the display goes on showing 100 frames a
second.

**Since this section was written:** 3.0.9 was built on that Mac on 2026-11-22 (only the frontend was rebuilt, with no
warning; `tools/test_nvk_bridge.py` and `tools/test_nvk_bridge_newest.py` pass there) and installed there, and the Mac
restarted without a fault. With vertical sync off its owner then read up to 1000 frames a second on the game's own
counter, where the capture above has 200. That is a reading, not a measurement: no capture was taken on 3.0.9, and no
card check presents through the new thread. The sentences below that say nothing of it was built on a Mac are of
before that.

### Fixes and changes

#### The native Vulkan frontend: presentation

- **An IMMEDIATE swapchain shows the newest frame, and its `vkQueuePresentKHR` does not wait for the layer.** Through
  3.0.8 every present, of either mode, took the layer's next drawable on the application's thread, copied the image
  into it on the GPU, waited for the copy and presented, with the device's submission lock held for the whole call. A
  FIFO swapchain still does exactly that, through the same code. An IMMEDIATE swapchain now has a thread of its own
  (`tools/nvk-bridge/present_newest.h`, which `bridge.mm` includes). `vkQueuePresentKHR` submits a fence on the
  application's queue, behind the semaphores the application passed, hands the image to that thread and returns. The
  thread waits for a presented frame, then for a drawable of the layer, with nothing held; when it has one it takes the
  newest frame the application has presented by then, waits for that frame's fence, copies the image into the
  drawable, marks and tags the drawable as before and presents it. A frame that a newer one overtook before a
  drawable came is never copied: its image goes back to the application once the GPU has finished rendering it.
  `vkAcquireNextImageKHR` on such a swapchain waits for the GPU to finish an overtaken frame, or for the thread when
  the thread has every image the application does not hold. For an application that acquires one image at a time
  (with more than two images: one that leaves at least two with the swapchain) that is the thread's copy, never a
  drawable; one that asks for the last image it does not hold can wait there for the layer, up to the layer's timeout
  of one second. The thread takes the device's submission lock for the copy's `vkQueueSubmit` alone, so the
  application's own submissions do not wait behind the import of the drawable's video memory, the wait for the copy or
  the mark. The layer is configured as before (three drawables at most, display sync off for IMMEDIATE); nothing
  paces the thread, and MAILBOX is still not offered.
- **When a swapchain or a present stays synchronous.** A swapchain gets a thread only when its mode is IMMEDIATE, the
  device has a copy queue of the frontend's own (the thread submits to it; where the frontend shares a queue with the
  application it does not), the process is not a child of `fork` made after the frontend started a thread, the switch
  below is on and the thread could be started; `NVK Bridge: swapchain presentation=newest frame` or `=synchronous` in
  the application's standard error says which, for each swapchain. On a swapchain with a thread, a present is still
  made on the application's thread, as in 3.0.8, when it presents several swapchains at once, when a present observer
  is set (the test hook `nvkbridgeSetPresentObserver`, which is called on the presenting thread), when the thread has
  ended, and after the thread could not show a frame. Such a present first gives up the frame the thread has not
  taken and waits for the copy the thread is in, so no older frame reaches the layer after it.
- **Errors are reported one present late.** A present that hands its frame over answers `VK_SUCCESS` before the frame
  is on the layer. When the layer gives the thread no drawable within its timeout of one second (a hidden or
  minimized window), or the thread's copy is refused (a drawable of another size after a resize) or fails, that frame
  is not shown, and the next present of that surface is synchronous and answers for itself what 3.0.8 answered
  (`VK_ERROR_OUT_OF_DATE_KHR` for a layer that gives no drawable). Presents of that surface stay synchronous, also on
  the swapchain that replaces this one, until one of them reaches the layer. A window whose layer gives no drawable
  therefore holds the application to one present a second once the thread's first wait has run out, about a second
  after the last frame that was shown, where 3.0.8 held it from the first present; in that second the application's
  presents are handed over and return at once. The thread also asks the renderer whether a fence signalled because
  the channel was rebuilt after a fault (`nvmtl_nvk_fence_dropped_v1`): of the frame's own fence before it copies, and
  of its copy's fence before it publishes. A frame whose fence was dropped is not copied, a copy that never ran is not
  published, and each counts as a frame that could not be shown. The first question covers the fence's own
  submission: rendering that a rebuild dropped while the fence submitted after it survived is copied as the image
  holds it, as on the synchronous path.
- **How long the GPU has, and a machine that sleeps.** The time a presentation thread gives the GPU, for a frame
  handed to it and for its own copy, is a backstop behind the renderer's own verdicts, which arrive through the same
  wait. On a channel that stands still the renderer rebuilds the channel (the fence then signals) or loses the device
  (the wait fails): after 5 s awake with its channel recovery on, which is its default, and after
  `NVRM_FLUSH_TIMEOUT_MS` (5 s) with it off. It goes on waiting only for a GPU that the kext calls suspended for a
  system sleep or wake, for at most 360 s of awake time, and then runs the same sequence. The frontend's own limit is
  therefore 400 s, longer than those together (`tools/test_nvk_bridge_newest.py` compares it with the renderer's
  constants), and each wait is one call for the whole of it, never slices: the renderer's clock for a channel that
  stands still starts again with every call. When the limit does run out, the device's completion is uncertain, as
  after a copy that did not complete within 5 s before, and on the synchronous path still: the frontend then answers
  `VK_ERROR_DEVICE_LOST` and releases nothing of the device. The renderer ends a wait at a time on a clock that goes on
  during a sleep, so a wait that ran out is believed only when the machine did not sleep through it, and is made again
  when it did: the frontend compares `CLOCK_UPTIME_RAW`, which stands still during a sleep, with
  `CLOCK_MONOTONIC_RAW`, and takes a second or more between them as a sleep. `vkAcquireNextImageKHR` with a timeout
  shorter than the limit waits for that timeout and answers `VK_TIMEOUT`, without a verdict on the device.
- **Replacing and destroying.** `vkCreateSwapchainKHR` with an old swapchain, `vkDestroySwapchainKHR` and
  `vkDestroyDevice` first tell the old swapchain's thread to present nothing more and wait for the copy it is in,
  before they take the submission lock (the thread needs that lock to finish a copy). When the device's completion
  is already uncertain they wait for nothing: every call on the device then answers that it is lost, and nothing of
  it is released. `vkDestroySwapchainKHR` then waits for the rendering of the frames that were presented and never
  copied before the images go, with the same limit. The thread itself ends when it next looks, at the latest after the
  layer's timeout if it was waiting for a drawable; until then it keeps the swapchain's record and the layer alive,
  and touches neither images nor the device. It can so still be inside the layer's `nextDrawable` while
  `vkCreateSwapchainKHR` configures the layer for the swapchain that replaces its own; the drawable it then gets
  goes back to the layer unpresented. A new swapchain's thread is started last, after the swapchain is in the
  frontend's registry, so a creation that fails leaves no thread behind. `vkQueueWaitIdle` and `vkDeviceWaitIdle` are
  unchanged: they do not wait for a frame the thread has not shown yet.
- **The thread.** One per IMMEDIATE swapchain, detached, named `NVK Bridge presentation`, of user-interactive
  quality of service, started with every signal blocked except those of a fault, with an autorelease pool for each
  round, as the Zink frontend's presentation thread (`zink/nvk_present_queue.c`). Nothing leaves it: on an exception
  it stops and the swapchain presents synchronously from then on. The library is pinned in the process when the first
  thread starts (`RTLD_NODELETE`, opened again by its own path with `RTLD_LOCAL`); where that fails no thread is
  started and the swapchains present synchronously.
- **What the frontend says.** The device's line reads `presentation completion=by swapchain` where it read
  `=synchronous`. The line written when a swapchain is destroyed gains `newest_frames_presented`,
  `newest_frames_shown` and `newest_frames_dropped`, counted for the process: frames handed to a thread, frames a
  thread showed, and frames overtaken or given up. A new export, `nvkbridgeGetPresentStats(uint64_t *out, uint32_t
  count)`, gives those three, the frames a thread could not show and the nanoseconds the threads waited for the
  layer. `nvkbridgeGetStats` is unchanged, and its copy counter counts the threads' copies too.

Nothing changes for OpenGL through Zink. `libnvk_zink_vulkan.dylib` is a copy of this frontend, but Zink creates no
swapchain in it: it presents through its own adapter, so the copy starts no thread and does not open the settings file
for this. Zink's own presentation has the same tie to the layer with vertical sync off (`present_drawable` in
`zink/nvk_cgl.c` waits for the frame before and takes the next drawable on the application's thread); that is not
part of 3.0.9.

`tools/switch_registry.py` now reads the frontend's own sources (`bridge.mm` and the header it includes, not the test
programs beside them) for the switches they name, and `tools/card_selftest.py` counts the header among the driver's
sources.

### The way back

New, of tier escape, with a row in `tools/switches.tsv`: `NVK_BRIDGE_PRESENT_NEWEST=0`, a line of
`/Library/GPUBundles/nvmtl/nvrm610.conf`, spelled as the switches of 3.0.7 and 3.0.8 may be (blanks around the name and
the `=`; off when the value begins with 0; the last line that names it wins). The frontend reads it from the file
alone, since it reads no environment variable (`NVMTL_PROCESS_ENV` does not apply to it), once per process when the
process makes its first IMMEDIATE swapchain, from the file's first 8191 bytes. Every swapchain then presents
synchronously, as in 3.0.8, and the frontend says so in one line. Without the file, an application's own vertical
sync setting (a FIFO swapchain) has the same effect for that application.

### What ran where

**Nothing of 3.0.9 was built on a Mac or ran on a card.** On Linux: `bridge.mm`, and `host_test.mm`, which includes
it, pass the syntax check against the 11.3 SDK as Objective-C++17 with ARC and `-Wall -Wextra -Werror`, with the SDK's
older name standing in for `kIOMainPortDefault`; nothing of the frontend was compiled to an object or linked here.
`tools/test_nvk_bridge_newest.py` runs `present_newest.h` as it is, with a GPU, a layer, a machine that can sleep and
an application of its own (`tools/nvk_bridge_newest_fixture.cpp`, 38 checks: an application nothing limits, with 2, 3
and 8 images; synchronous presents mixed in; a swapchain replaced with frames in flight; a copy that fails; an
exception; a hidden window; a GPU slower than the application; a GPU that stops; a wait that fails; a machine that
sleeps through a wait of the thread and of `vkAcquireNextImageKHR`; the two clocks). The test was run with gcc 13 and
again with clang 18, each plain, under ThreadSanitizer and under AddressSanitizer with UndefinedBehaviorSanitizer;
against 21 copies of the header with one rule broken each, every one of which fails in the check that looks at its
rule (the test names it); and against an application's side that takes the submission lock before it waits for the
thread, in a present and in an acquire, each of which stands still as it has to. The same test reads `bridge.mm` for
how it uses the header (what is done before the submission lock, when a present is handed over, the fence behind the
rendering, the whole waits and the two clocks, the thread's setup, its pin and that it starts last, the dropped-fence
questions against NVK's own declaration, the limit against NVK's constants), with 18 copies of `bridge.mm` broken in
one place each, one copy of NVK's patch whose declaration takes other arguments and one copy of the header with a
limit of 30 s. `tools/test_nvk_bridge.py` now reads the frontend and its header together for the files they open
(two, both to read) and the environment (none).
The fixture's machine sleeps when its script says so; that `CLOCK_UPTIME_RAW` and `CLOCK_MONOTONIC_RAW` move apart by
the length of a sleep on the Mac, and that NVK's wait behaves across one as read from its source, was not run.

Not checked, because it needs the layer, WindowServer and the card: that the frames the thread shows are the right
ones, whole and in order on the screen; what the game's frame rate becomes; what a resize, a minimize and a second
display do to it; the thread's cost on the GPU when the application presents far more frames than the layer takes
(the thread copies for every drawable the layer hands out, about two for each composition in the capture above).
**No check of the card suite runs the new path:** `smoke.mm` uses FIFO, and `window_check.mm` sets the present
observer for its whole run, so the swapchain of its IMMEDIATE phase, when it has a thread (the program now prints the
frontend's `swapchain presentation=` line for it, as information), never hands that thread a frame and its presents
are synchronous. The suite therefore goes on checking the synchronous path, which it has to pass as before, and on a
swapchain with a thread the new bookkeeping of images under it. `host_test.mm` was not run and has no case for a
swapchain with a thread.

Known limits. With `NVRM_ASYNC=0`, or on a channel that has no slot for asynchronous submission, the fence a present
submits still waits for the GPU on the application's thread. A presentation thread is not joined when the process
exits. A child of `fork` that goes on using a swapchain its parent made is not supported (it was not before). The
synchronous path's wait for its copy is 3.0.8's, 5 s on the renderer's clock: a synchronous present that is in flight
when the machine goes to sleep can still find its wait run out when it wakes, as before. A sleep shorter than a second
is not seen as one. With the renderer's channel recovery off (`NVK_CHANNEL_RECOVER=0`, or `NVRM_ASYNC=0`) the
renderer's own clock for a channel that stands still counts a sleep, so a wait in flight across one fails in the
renderer, as it did before 3.0.9. The 400 s of a presentation thread are a constant of the frontend, which reads no
environment: `NVRM_FLUSH_TIMEOUT_MS` raised beyond them is not followed. They are also how long an application can
stand in `vkAcquireNextImageKHR`, or in a present, a resize or a destroy that waits for the thread, on a device that
finishes nothing for a reason the renderer does not see, before it is answered `VK_ERROR_DEVICE_LOST`; on the
synchronous path that is 5 s. The renderer does not tell the frontend that the GPU is suspended; the frontend infers
a sleep from the clocks, after the wait.

## 3.0.8

**Not released.** This section describes the branch being prepared as 3.0.8, on top of 3.0.7 (below, not released
either). It is one change, in the Zink frontend's CGL adapter: how often a swap asks WindowServer whether the window's
drawable is opaque. It starts from a measurement. 3.0.7 was built on one Mac from an empty folder and installed there
(AMD Ryzen 5 5500, RTX 5050, one 1920x1080 display at 100 Hz, macOS 26.7.3, started from a test copy of its boot
loader configuration in which `debug.nvrm_pat_wc` reads 1). A `sample` capture of the modded Minecraft pack of the
audit put it at 90 frames a second with the GPU 83 % busy, where the comparable capture on 3.0.6 had 61 and 51 % (not
the identical scene). Of the game's render thread, 63.3 % was the game's own code and 12.0 % the swap; of the swap,
288 of the thread's 5036 samples (5.7 %) were `present_drawable` asking CGL for `kCGLCPSurfaceOpacity`, which CGL
answers with a message to WindowServer and its reply, twice per swap. **Nothing of 3.0.8 has been built on a Mac or
run on a card, and no gain has been measured.** The most the render thread can get back is those 5.7 %; what that
gives in frames is for a capture on the card to say.

**Since this section was written:** 3.0.8 was built on that Mac (only the Zink frontend changed) and put in place
there on 2026-11-22 without a restart, by copying the libraries that changed. No capture was taken with it, so what
the change gives back is still not measured.

### Fixes and changes

#### OpenGL through Zink: the swap

- **A swap asks for the drawable's opacity once, and the answer is kept for a tenth of a second.** Through 3.0.7 a
  swap asked twice: once for the layer (`nvk_native_acquire`, which sets the CAMetalLayer's `opaque`) and once for the
  copy (`nvk_zink_present_frame`, whose blit writes alpha 1 into an opaque drawable); a swap that went on to the
  IOSurface path asked there instead of for the copy. Now a swap asks at most once and gives the layer, the copy and the
  IOSurface path the same answer, so they cannot disagree within a swap. A context keeps that answer
  (`zink/nvk_opacity.h`) and asks again on the first swap at least 100 ms after it last asked, on a clock that does not
  move while the machine sleeps. It asks at once on its first swap, after an ask that failed (which answers opaque, as
  3.0.7 did, and is not kept), and after the adapter has seen the context's output change: a new size of the drawable, a
  view bound or unbound, the native target removed, and a native acquire that gave no drawable (the layer is being set
  up, the window is minimized, the native path fell back, or the view went away), except a swap that again finds no
  view. **What it changes for an application:** after an application changes `kCGLCPSurfaceOpacity` of a context that
  has swapped, the swaps of up to the next 100 ms are still composed with the old value, both the layer's `opaque` and
  the alpha the copy writes, where 3.0.7 used the new value from the next swap. Where `CGLSetParameter` of that
  parameter reaches the GLI integer callback the adapter already replaces for `CGLTexImageIOSurface2D`, the next swap
  asks at once; whether it does is not known, and nothing depends on it. Way back: `NVK_ZINK_OPACITY_KEEP=0`. Checked
  with `tools/test_zink_opacity.py`.

The card check `zink.win-ctl` changes opacity on a window that has already swapped and judges the three frames after
each change. It now lets 150 ms pass after each change before those frames, so that it judges the same thing on 3.0.7
and on 3.0.8; its case of a new context whose opacity is set before its first swap is unchanged, since a context's
first swap always asks. The line citations of the card checks follow the code that moved, and one that named the wrong
lines before (the borrowed bind of `nvk_cgl_present`, in `zink_win_check.m`) names the right ones; nothing else in
them changed.

### The way back

New, of tier escape, with a row in `tools/switches.tsv`: `NVK_ZINK_OPACITY_KEEP=0`, a line of
`/Library/GPUBundles/nvmtl/nvrm610.conf` (or the process's own environment with `NVMTL_PROCESS_ENV=1`), read once per
process by `libnvk_cgl.dylib` when its first context presents, spelled as the two switches of 3.0.7 may be. Every swap
then asks WindowServer again, once, and sees an application's change at the next swap as 3.0.7 did. A swap still asks
only once and gives the layer and the copy the same answer; that has no switch.

### What ran where

**Nothing of 3.0.8 was built on a Mac or ran on a card.** On Linux: the four files of the Zink frontend compile for
macOS in the cross-build with 0 warnings and 0 errors (of what they include only a comment of `nvk_zink_cfg.h`
changed), and `nvk_cgl.c`, `appkit.m` and `native_drawable.m` pass the syntax check against the 11.3 SDK with `-Wall
-Wextra -Werror`, `native_drawable.m` with the SDK's older name standing in for `kIOMainPortDefault`.
`tools/card/zink_win_check.m` passes the same syntax check with its row's flags, with the three deprecation warnings
it had before. `tools/test_zink_opacity.py` runs `nvk_opacity.h` as it is with a clock and a reader of its own (25
arms, plain and under AddressSanitizer and UndefinedBehaviorSanitizer, with gcc 13 and clang 18), against 13 copies
with one rule broken each, every one of which fails as it has to, and reads the adapter for one ask per swap, the
switch, and each place that makes the next swap ask, against 3 copies of the adapter with one such place broken
each, every one of which fails that reading. `tools/test_zink_present_queue.py` passes with the end of
`present_drawable` as it is now. Not checked: the clock's cost on a Mac (the frontend's queue already uses it),
whether `CGLSetParameter` reaches the integer callback, and anything that needs WindowServer.

Three figures in the sections of 3.0.7 and 3.0.6 are corrected in place, because the captures they came from, read
again, do not bear them out: the 61 frames a second with the changed page attribute table had been set against two
captures at 55 that were not like for like, so what the table change adds in frames is not known; the sleep in the
swap of 3.0.6 was 30 to 44 % of the game's thread and 4.6 to 7.9 ms a frame, not 30 to 40 % and 4.7 to 7.3 ms; and the
captures of 3.0.6 are of the same pack as the 19 frames a second of 3.0.2, not of a scene matched to it. The 3.0.7
section also says now what 3.0.7 did on a Mac and on its card.

## 3.0.7

**Not released.** This section describes the branch being prepared as 3.0.7, on top of 3.0.6 (below, not released
either). It is one change in three parts, in the kernel, in NVK and in the Zink frontend: how an OpenGL window on Zink
gets its frame to the screen. It starts from a measurement. 3.0.6 was built and installed on the Mac the audit's
captures came from (AMD Ryzen 5 5500, RTX 5050, one 1920x1080 display at 100 Hz, macOS 26.7.3), the first time
anything after 3.0.2 ran on a card. The modded Minecraft pack of the audit draws through Zink on NVK there now, as
3.0.3 meant it to, and three `sample` captures with the kernel's counters put it at 55 to 64 frames a second with the
GPU 41 to 53 % busy, in the same pack, which gave 19 and 74 in the two captures of 3.0.2 through Apple's renderer (the
scenes were not matched). In those captures the game's thread spent 30 to 44 % of its time asleep in the swap,
until the GPU had finished the frame it had just sent (4.6 to 7.9 ms a frame), and 6 to 8 % importing the drawable's
video memory into NVK again, for a layer that hands out the same three surfaces in turn. Up to 13 % more went to
copying the game's buffer uploads into video memory that this machine maps uncached instead of write-combined
(`debug.nvrm_pat_wc` reads 0 there); that is decided by the boot loader's kernel patches, and nothing here changes it.
It was measured afterwards, on the same Mac and the same 3.0.6: started from a boot loader that sets entry 6 of the
page attribute table to write-combined (`debug.nvrm_pat_wc` reads 1), the uploads fall from 11 to 14 % of that thread
to 1.4 %, the import falls from 7 to 8 % to 1.2 % (the kernel call that binds its address range is twenty times as
fast), and that capture gives 61 frames a second. What that is worth in frames is not known: the two captures at 55
that it was set against were not taken like for like (one ran with `NVK_LOCAL_MAP_SYSMEM=1`, the other also with
`NVK_BAR_VRAM=0`), and the only other capture without settings, of a lighter scene and without the table change, gave
64. What is measured is the split of the thread; how many frames a second the table change adds is not. The sleep in
the swap is then 38 % of the thread, 6.3 ms a frame, and what is left of the import is small. **3.0.7 was built on
that Mac from an empty folder, installed there and started normally, and its card checks ran there; no gain of either
change on its own has been measured** (below, "What ran where"). Before that, the kernel part and the frontend were
compiled on Linux, of NVK only the syntax of the units its new patch changes was checked, and the tests were host
tests on Linux. By arithmetic alone a swap that neither sleeps nor imports would let that scene run 1.6 to 1.9 times
as fast, if the GPU keeps up, and the GPU had about that much room. A capture of the pack on 3.0.7, with the same test
boot configuration, gives 90 frames a second where the comparable capture on 3.0.6 gave 61 (the scenes are not
identical), the GPU 83 % busy where it was 51 %, and the swap 12.0 % of the game's thread where that thread was asleep
in the swap 38.4 % of the time; the presentation thread waits for the GPU 68.7 % of its time. No card row runs a swap
one frame ahead yet. Each of the two changes of what runs by default has one way back.

### Fixes and changes

#### OpenGL through Zink: the swap

- **A swap no longer sleeps until the GPU has finished its frame.** Through 3.0.6 the thread that swapped did all of
  it for every frame: it imported the drawable's video memory, sent the copy into it, slept until the GPU had
  finished, marked and tagged the surface and presented the drawable. Now the swap draws the frame into the scratch
  image, flushes, sends the copy to the same queue right behind it and returns. One thread of the process, the
  presentation thread, waits for the copy's fence and then does what the swap did, in the same order: the mark, the
  tag, the drawable. The context's next swap waits for that to have happened before it asks the layer for a drawable
  and before it draws into the scratch image again. So a context never has more than one frame on its way, the layer
  has its drawable back before the next is asked for, what is on screen is at most one frame behind what was swapped,
  and the time the GPU takes for a frame runs beside the game's work on the next one instead of in front of it. A
  front-buffer flush, and a swap while a diagnostic observer is set, wait for their own frame as before. A frame the
  presentation thread could not publish (the mark of an import made for that frame was refused, the channel was
  rebuilt under the copy) is released unpresented, and the next swap of that context says so on standard error, once
  and then at every doubling of the count; the swap that sent it had already answered 1. A GPU that does not finish a
  copy in 5 s, or a presentation thread that stays at one frame for 12 s (5 s for the copy and 5 s more for a frame it
  had to send again), quarantines the share group as 3.0.6 does for a copy that does not finish: nothing is freed and
  nothing more is presented. The 12 s are that thread's and not the waiting swap's, because it works the frames of
  every window of the process in turn: a frame that stands behind another window's slow one is not given up while that
  one is within its time, and a frame whose work is already past it is given up at once. That thread takes none of the
  application's signals, except the ones a fault of its own raises (SIGSEGV, SIGBUS, SIGILL, SIGFPE, SIGTRAP, SIGSYS),
  so a crash in it is reported as a crash. Whatever this path does not take (NVK refuses the import, the surface is
  not one the copy can fill) goes through 3.0.6's path for that frame, which stays in the file unchanged; after three
  such frames in a row a context stays with it and lets its kept imports go. The command pool, the fence and the
  renderer's entry points, which 3.0.6 made and looked up for every frame, are the context's now. Way out:
  `NVK_ZINK_PRESENT_AHEAD=0`. Checked with `tools/test_zink_present_queue.py`: the queue with real threads, and this
  half of `zink/nvk_present.c`, cut out of the file as it is, against a scripted GPU.
- **A drawable's video memory is imported once instead of for every frame.** 3.0.6 imported the surface it was handed
  into NVK for every frame, a new address range and a new binding each time, and freed the import after the frame. A
  context now keeps its imports, of up to four surfaces, and lets one go when it has presented 64 frames without it,
  when a swap finds no drawable, when it goes back to 3.0.6's path, and when it is destroyed. A context that stops
  swapping keeps what it has, at most four, until then. A kept import must not outlive what it imported: the surface's
  family can release the block of video memory and allocate another, and a frame copied into the old block would be
  marked as if it were in the surface. So the kernel names the block. The grant of a surface's video memory
  (`NVRM_ESC_SURF_VRAM`) answers with the block it shared, in a word of the reply that was padding (the low half of
  the allocation's cookie; 0 whenever the grant is refused, and a caller that reads 0 keeps nothing), and a mark
  (`NVRM_ESC_SURF_DIRTY`) that names a block is taken only while the surface still has it; otherwise the kernel marks
  nothing, answers `NVRM_SS_MOVED` and counts it in the read-only `debug.nvaccel_surf_mark_moved`. A mark that names
  no block is taken as before, which is every mark of the Metal plugin and of a frontend before 3.0.7. NVK gets two
  exports for it, in `nvk/nvk-surface-keep.patch` at the end of the 3.0 chain: the block the calling thread's last
  import was granted, and a mark that names a block. When the mark for a kept import is refused, for that reason or
  any other, the frame is still whole in the scratch image: the presentation thread lets the import go and sends the
  frame again at once the way 3.0.6 sent every frame, before it answers, so whether a frame reaches the screen does
  not depend on the application swapping again. It is not sent again once the share group is quarantined: nothing is
  sent then, as in 3.0.6. After four refused marks in a context's life the context lets its kept imports go, imports
  for every frame from then on and says so once on standard error. With an NVK or a kernel from before 3.0.7 no block
  is named and nothing is kept. Way out: `NVK_ZINK_SURFACE_KEEP=0`. Checked with `tools/test_surf_keep.py` (the
  kernel's two rules and the lines of the escape that apply them) and with `tools/test_zink_present_queue.py`.

What the two rest on and could not be run here: a duplicate of the RM object keeps a surface's video memory alive in
this process after the surface gave it up, which the Metal plugin's imports rely on too; a drawable may be presented
and released by a thread other than the one that asked for it, which the card check `zink.win-thread` does with the
swap itself; and the waits of an application that reads back or queries its own frame move into the next frame. The
card checks of Zink set the observer, so they run the swap that waits; no row runs a swap one frame ahead yet, and
that row is still to be written. Their line citations follow the code that moved, and the head comment of
`zink_win_check.m` now says that a frame which is sent again is observed twice; nothing else in them changed.

### The ways back

Both are new, of tier escape, with a row in `tools/switches.tsv`. A process reads them once, when its first context
presents, from `/Library/GPUBundles/nvmtl/nvrm610.conf` (or from its own environment with `NVMTL_PROCESS_ENV=1`). A
line may have blanks before the name, around the `=` and before the value, so every spelling that turns a switch of
the Metal plugin off in that file turns one of these off (the plugin itself keeps a blank after the `=` as part of the
value), and the last line that names a switch decides.

- `NVK_ZINK_PRESENT_AHEAD=0`: every swap waits for the GPU and publishes its own frame, through the code of 3.0.6,
  which also imports for every frame.
- `NVK_ZINK_SURFACE_KEEP=0`: swaps stay one frame ahead, and the drawable's video memory is imported for every frame.

No switch: the word of the grant that names the block (a caller that sends 0 in its marks gets what 3.0.6 gives), the
two exports of NVK (only this frontend calls them), and the counter, which decides nothing. A frontend of 3.0.7 over
an older NVK or kernel, and an older frontend over these, import for every frame as 3.0.6 does. Neither switch reaches
WindowServer or a Metal application, which do not load this frontend, and the boot argument `nvzink=0` still takes
OpenGL back to Apple's renderer whole.

### What ran where

**3.0.7 was built on a Mac from an empty folder, installed there and ran on its card; no change of this section has
been measured on its own.** What ran, and where:

- **Linux, compile checks of the final tree.** The kext compile check of all 25 units passes with 0 warnings. The
  import check against the exports of macOS 15 and 26 gives the one line it gives for 3.0.5 and 3.0.6
  (`_ceutilsMemset_IMPL`). The plugin's Objective-C check reports 0 new errors and 0 new warnings; the plugin is not
  changed. The four files of the Zink frontend compile for macOS in the cross-build with 0 warnings, and `nvk_cgl.c`,
  `appkit.m` and `native_drawable.m` pass a syntax check against the 11.3 SDK. None of these links or runs the driver.
- **NVK.** `nvk/nvk-surface-keep.patch` applies without fuzz to a tree with the rest of the chain applied. The two C
  units it changes and 29 of NVK's other 52 pass clang 18's syntax check for macOS, against the public 11.3 SDK and
  the 610 kernel-module headers, with the same diagnostics as without the patch; the remaining 23 need generated
  headers that were not made here, with or without it. NVK was not built or linked with the patch anywhere.
- **Linux, the tests of this change.** `tools/test_zink_present_queue.py` builds three programs out of the sources as
  they are. The queue's has 24 arms and the frame's 69 (in seven sets: with and without kept imports, switched off, an
  older NVK, an older kernel, how the settings' lines may be written, a fork); both run plain and again under
  ThreadSanitizer and AddressSanitizer (there the queue without its two fork arms, and the frame's first four sets).
  The third runs the end of `present_drawable` and `native_frame_done`, cut out of `nvk_cgl.c`, for who releases the
  drawable in each answer of the frontend: 11 arms. With them go 31 copies of the sources with one rule broken each
  (10 of the queue, 16 of the frame, 5 of the drawable's), every one of which fails as it has to, and the text of
  3.0.6's path, which every way back leads to, held to what it was. `nvk_zink_present_frame` itself is not run, only
  read for the order of its steps, and the 12 s are read too: the programs run with a limit of 0.4 s.
  `tools/test_surf_keep.py`: 14 arms and 6 such controls. Both with gcc 13 and with clang 18.
- **Linux, the host suite** (`python3 tools/run_all_tests.py`), on the final tree. 353 passed, 40 failed, 11 skipped
  and 10 not run, of 414. The two tests of this change are new and pass. The 40 are 40 of the 41 that fail on Linux at
  3.0.6, by name, because they need a Mac; the 41st, `test_build_sh.py`, passes here now and passes on the 3.0.6 tree
  too when it is run again, so that one was this machine and not either tree. With wrappers that let the Objective-C
  tests compile and run on Linux, 378 passed and 16 failed: 16 of the 17 that fail that way at 3.0.6, the same test
  apart.
- **A second reading.** The code was read again by area (the kernel and NVK, the queue, the frame, the frontend and
  CGL, the tests and texts), and each thing found was then checked by a reader told to refute it. 21 stood. The
  corrections were then read once more by three readers, each with a refuter, and five more things stood, all small: a
  frame was sent again even in a share group that had been quarantined meanwhile, the 12 s were counted from when a
  swap began to wait and so charged a frame with the time of the frames ahead of it, and three texts said more than
  the code did. Of the 26, none was a wrong or a foreign frame on screen, a crash, a hang or memory used after it was
  freed; the weightiest was that a frame whose mark was refused waited for the application's next swap, which an
  application that draws only when something changes may never make. What is described above is the code after all of
  them: the frame sent again from the presentation thread, kept imports let go as soon as a context stops keeping, the
  fault signals, the word of a refused grant, the settings' blanks, and the tests that were missing (a fence that
  keeps its state, a kernel that remembers each surface's block, the age to the frame, the drawable's ownership run
  instead of read). Three are answered by saying so here and not by code: no card row runs a swap one frame ahead,
  `nvk_zink_present_frame` is read and not run (as this was written; it has since run on the card in one capture,
  below), and a context that stops swapping keeps its imports until it is destroyed.
- **A Mac, the driver built.** At 9682b3a2, whose tree is the one of 7159e8dc, on the Mac of the captures: the outputs
  of earlier builds were removed (`./build.sh clean outputs`) and all 19 components were built from nothing in 512 s
  with 0 warnings, reusing the build tools and the pinned sources already there; then `package`, which installs and
  loads nothing. Every file of the package matches `SHA256SUMS` and the plugin bundle's seal verifies.
- **The card.** That package was installed over 3.0.6, which the installer kept as the fallback, and the Mac started
  normally; the boot guard counted the start as good. The 48 files under `/Library/GPUBundles` equal the package, and
  `debug.nvaccel_surf_mark_moved` exists and reads 0. The Mac was started from a test copy of its boot loader
  configuration, not the one on its disk, in which the page attribute table has a write-combining entry:
  `debug.nvrm_pat_wc` reads 1.
- **The card checks** (`tools/card_selftest.py` at 9682b3a2). Of 134 rows 133 ran, 130 passed and 3 failed; 1 was
  skipped (`vulkan.smoke-sdl`, which needs SDL3). The OpenGL rows ran through Zink, the card reported no fault (Xid 0)
  and WindowServer stayed the same process. The three rows 3.0.6 had left to pass on the card,
  `xlate.paths-samplerphi`, `xlate.paths-abarray` and `xlate.paths-walk`, pass. A second round measured the three that
  failed; the fixes of the three checks are not in this release:
  - `zink.win-thread`: the 120 frames of its secondary thread did not finish in their time, and every frame of every
    Zink window row took about 250 ms. That time is the check's own and not the swap's: in a profile of
    `zink.win-basic`, 97.2 % of the presentation thread's samples are self time in the check's pixel reader
    (`zw_pixels`, `tools/card/zink_win_check.m`), which reads its copy of each frame byte by byte from video memory
    behind BAR1. With `NVK_BAR_VRAM=0` for that process alone `zink.win-basic` took 1.5 s instead of 15.4 s, and the
    secondary-thread case passed (120 frames in 1.2 s, the run 2.1 s); `NVK_ZINK_PRESENT_AHEAD=0` changed nothing.
  - `v301.async-upload`, case "limit": the driver printed no line of 256 pending uploads. The row's expectation
    predates the reaper of 3.0.6, which gives finished uploads back; with `NVMTL_ASYNCUP_REAP=0` its 9 cases pass.
  - `zink.migrate`, the probe of a context that a second thread takes over after its first thread released it through
    CGL alone, so that Mesa still holds it current there while that thread binds another: it ends through its own
    crash handler, exit 70 and only "CRASH " on standard output, in all five runs, with no signal name, no backtrace,
    no crash report and the kernel's counters unchanged. Where it faults is not known. The code that case runs is
    older than 3.0.7 and reaches none of 3.0.7's changes, so it is taken for a defect of the frontend that predates
    3.0.7; that is read in the code, not shown by a backtrace or a run of an older release.
- **A capture of the modded pack** with `sample` and the kernel's counters, with the test boot configuration: 90
  frames a second, the GPU 83 % busy. Of the 5036 samples of the game's render thread 63.3 % are the game's own code
  and 12.0 % the swap; within the swap 5.7 % of the thread asks WindowServer for the drawable's opacity, twice per
  swap (3.0.8, above), 3.7 % flushes the frame and 2.1 % waits for the presentation thread to have presented the
  previous one. The presentation thread waits for the GPU 68.7 % of its time and is idle 30.5 %. The comparable
  capture on 3.0.6, on the same Mac with the same test boot configuration but not of the identical scene, gave 61
  frames a second with the GPU 51 % busy and the thread asleep in the swap 38.4 % of the time.
- **Not run anywhere:** a card row that runs a swap one frame ahead (the rows set the observer, which makes the swap
  wait); a measurement of either change on its own; and 3.0.7 with the boot loader configuration on the Mac's disk,
  where `debug.nvrm_pat_wc` reads 0.

## 3.0.6

**Not released.** This section describes the branch being prepared as 3.0.6, on top of 3.0.5 (below, not released
either). It is the code half of a performance audit of games. The audit started from a measurement: two `sample`
captures and the driver's counters, taken on one Mac (AMD Ryzen 5 5500, RTX 5050, one 1920x1080 display at 100 Hz,
macOS 26.7.3) running 3.0.2 and a heavily modded Minecraft pack that does not pass 23 frames a second. That game draws
with OpenGL through Apple's OpenGL renderer over this driver's Metal plugin; the GPU was 37 to 60 % busy with video
memory to spare, and the game's render thread was the limit. Capture A is a moment at 74 frames a second, capture B
one at 19. Between 15 % (A) and 35 % (B) of the render thread was spent in waits that belong to the plugin, and that
is what the first group below goes after. The rest are defects and costs the audit found by reading the code, each
judged by a second reviewer before it was written and by a third after: compute shaders, three shader translations
that were refused, a DLSS leak, the video-memory pager's waits, and three diagnostics. The last group holds what the
host suite found when it first ran on a Mac: one defect of the plugin, and three tests that failed for other reasons.
Every change of what runs by default has one way back, a new row of tier escape in `tools/switches.tsv`; a change with
none either gives exactly what 3.0.5 gave or only writes a log line. **3.0.6 was built on that Mac, installed there by
its owner and runs on that card, and none of the gains of its own changes has been measured** (below, "What ran
where"). What was measured there is the route. With 3.0.6 installed, the first driver after 3.0.2 to run on a card,
that game draws through Zink on NVK (one translation instead of two), which 3.0.3 let engage on that macOS build, and
three captures put it at 55 to 64 frames a second in the same pack, which gave 74 and 19 in captures A and B (the
scenes were not matched). With Zink engaged the changes of the first group below, to Apple's renderer, do not act, and
no capture was taken with the boot argument `nvzink=0`, which brings that renderer back; on that route the waits
measured in 3.0.2 put the ceiling of those changes between 1.2 and 1.9 times the frame rate. The shares quoted from
the captures say where the time went in 3.0.2, not what this branch gives back. The entries below say "not run on a
card" as they were written, before that install; none of them has been measured on its own since.

### Fixes and changes

#### Apple's OpenGL renderer over the plugin (the measured route)

- **Apple's OpenGL renderer no longer sleeps on a thread hop at every context flush.** The renderer calls
  -commitAndWaitUntilSubmitted on the order of a hundred times a frame, because each glBufferSubData on a buffer the
  pending command buffer uses flushes the context. Each call committed the command buffer and asked the nvmtl.ahead
  queue for a submit-ahead pass. It then slept until that thread had taken the command-buffer class lock, submitted
  the part and woken it. That sleep was 7.8 % and 20.1 % of the render thread in the two captures, and nvmtl.ahead
  spent 35-39 % of its time waiting for that lock. Outside WindowServer the committing thread now runs that same
  submit-ahead step itself: the same nvmtlSubmitAhead decisions, under the same lock, over its own queue in order up
  to its own command buffer. It then waits exactly as before, so it still returns only once the command buffer is
  scheduled. The status, the result, the scheduled handlers and command buffers held until a surface publication are
  unchanged. A command buffer the step cannot submit waits as before: the in-flight cap, a caller copy, an event wait,
  or an earlier command buffer not yet committed or being submitted by the worker. WindowServer keeps its path. A log
  line at powers of two says how many of the flushes that ran the step were scheduled when it returned. Way out:
  NVMTL_GL_FAST_FLUSH=0 brings back the plain wait. Checked with the host tests test_fast_flush.c and
  test_fast_flush.py and the plugin's Objective-C check (0 new errors or warnings). Not run on a GPU or a Mac, so no
  gain is measured.
- **A command buffer costs less to make.** Apple's OpenGL renderer makes on the order of a hundred command buffers a
  frame, and each one paid for things it did not need. -[NVMTLCommandQueue commandBuffer] made the command buffer's
  resource set a second time and freed the empty one that -init had just made. It took the command-queue class lock on
  every call just to find the queue's capacity semaphore. Every command buffer made two dispatch groups, whether or
  not a handler was ever dispatched. Now the set from -init is kept. The semaphore is made with the queue, with the
  same count, and read without the lock. Each handler group is made when the first handler of its kind is dispatched,
  under the lock its waiters check it under, so every wait still waits for the same handlers. In capture A these costs
  came to about 0.7-1 % of the render thread's time. That share is read from the sample; no gain has been measured.
  Way out: NVMTL_CB_LEAN=0 makes both groups with every command buffer and looks the semaphore up under the lock, as
  in 3.0.5. The second resource set is gone either way, because it changed nothing. Checked with tools/test_cb_lean.c
  (a model of every order of dispatches and waits, with the groups made up front and on first use),
  tools/test_cb_lean.py (the wiring) and the Objective-C check. Nothing was run on a card.
- **A window surface drawn through a private image now says why in the system log, and the game's render thread no
  longer waits for the previous frame's copy-out when it binds that surface.** An OpenGL game's window surface that
  cannot live in the accelerator family's VRAM is drawn through an image of the game's own process. That image is
  copied to the surface's pages after every GPU write. Which condition caused this was written only to the plugin's
  own log, which a release plugin keeps only for a process started with NVMTL_PROCESS_ENV=1. Each reason is now
  written to the system log once per process, tagged "private-surface:". The reasons are: the import had no
  descriptor, NVK's import of the family VRAM failed, the buffer or the linear image over it was refused, the texture
  is larger than the surface, the row is not a multiple of 32 bytes, surface sharing is off, or the surface is in VRAM
  but its mark as newest was refused. Find it with `log show --last 1d --style compact --predicate 'eventMessage
  CONTAINS "private-surface:" OR eventMessage CONTAINS "surfvram: surface" OR eventMessage CONTAINS "dirty refused"'`.
  Nothing is written in WindowServer, or on AMD and Intel GPUs, which copy by design. The copy-out also held the
  surface's monitor for its whole GPU round trip. In capture A the game's render thread spent 5.4 % of its time
  blocked on that monitor while binding its window surface at swap time, only to find the image and the pages already
  in step afterwards. A command encoder's bind now passes this process's own copy-out of a private image while it is
  in flight, unless a zero fill was lost or another writer published into the surface's VRAM meanwhile. CPU reads and
  writes of the texture still wait, and WindowServer is unchanged. Way out: NVMTL_SURFIN_SKIP_OWN_OUT=0 waits as
  before. Two smaller equivalent changes: a bind of a texture with no surface behind it returns at once, and the CPU
  frame-dump check no longer reads the clock for such textures. Not run on a card, so no gain is measured. Host tests:
  tools/test_surf_private.c (which fails on each of seven changes to its rules) and tools/test_surf_private.py pass,
  and objccheck reports 0 new errors and 0 new warnings. test_surface_publication.py and test_protected_surface_out.py
  got stubs for the new calls in their fixtures but cannot run in this environment.
- **Apple's OpenGL renderer's resource waits wake when the last command buffer lets go, instead of polling every 50
  us.** GLDBufferRec::restoreData, reached from glDrawElements, calls -[NVMTLBuffer waitUntilComplete] after
  -[NVMTLCommandBuffer waitUntilCompleted]. That call, and its texture twin, slept 50 us between looks at the
  resource's command-buffer count. In capture B it took 4.7 % of the game's render thread, nearly all of it in usleep,
  on top of 6.9 % in waitUntilCompleted. The waiter now sleeps on a condition shared by the whole process, with the
  address of the count it waits for listed beside it. Any change that brings that count to zero wakes it. While nobody
  waits, a count reaching zero costs one extra load and nothing more, so WindowServer and every other process behave
  as before. The 10 s limit and its log lines are kept, and the limit no longer depends on the wall clock. A new
  census line logs, at doubling counts, how often the waits slept and for how long. From the code: by the time
  waitUntilCompleted returns, that command buffer's references are already dropped, so the remaining wait is for other
  command buffers that still name the resource. No gain is measured yet. NVMTL_GL_WAIT_EVENT=0 restores the polling.
  Run: tools/test_idle_wait.c (racing threads with no lost wake-up, a control, and a waiter on a negative count),
  tools/test_idle_wait.py (wiring), the objccheck (0 new errors and warnings), and the tests that touch these files.
  Not run on a GPU or on macOS.
- **Textures that Apple's OpenGL renderer makes and drops for every glBlitFramebuffer no longer cost four kernel round
  trips each.** In capture B, the render thread spent 8.2 % of its samples in -[NVMTLDevice
  newTextureWithDescriptor:]: vkAllocateMemory, plus the image's GPU virtual range being allocated and mapped in
  vkBindImageMemory. The completion worker spent 8.3 % freeing them again (nvk_DestroyImage, nvk_FreeMemory). An
  ordinary colour texture from newTextureWithDescriptor now keeps its image and its own VRAM once the image grace has
  released it, which means nothing open or in flight can read it any more. The next texture made with exactly the same
  Vulkan creation takes both and makes its own view. Its content is undefined, as Metal allows, and its layout
  tracking starts at UNDEFINED. At most 32 images and 128 MB are held, 32 MB per image at most. An image not reused
  within about a second is freed at the next completed submit or park. Held memory stays counted as allocated VRAM,
  and it is the first thing given back when room is needed: at the VRAM gate; when the kernel refuses an allocation
  for an image, a private buffer, a stable-address buffer or a placement heap, each of which is then tried once more;
  and by the residency pager, before it moves any live texture to system memory. A texture whose grace was released by
  force, because a command buffer from before its death stayed open too long, is freed and never held. A fault report
  says when a dead texture's image and memory are held for reuse, and a slot that still names such a texture's
  destroyed view stays reported as dead after the image is reused. Placed, pooled, sparse, system-memory, IOSurface,
  depth, multisample, re-backed and migrated images are never held, and WindowServer never holds any. Way out:
  NVMTL_TEX_RECYCLE=0 frees every dead texture as before. Checked here with a host model of the cache
  (tools/test_texrc.c, every mutation of its rules fails it), text checks of the wiring (tools/test_texrc.py), the
  existing grace and channel-recovery checks, and objccheck. It has not run on a GPU, and no gain has been measured.
- **Finished asynchronous texture uploads are given back at the next upload.** An asynchronous replaceRegion or
  IOSurface upload kept three things until its texture was used again (uploaded again, read back, mapped or
  destroyed): its record, its staging buffer and one of the process's 256 places. A texture uploaded once and then
  only sampled held all of that for its whole life. Once 256 such textures existed, every later upload to any other
  texture went back to submitting and waiting for the whole GPU queue. Now each upload first checks, without waiting,
  which earlier uploads the GPU has finished. It gives back their places, staging buffers and records, and leaves only
  a small header on each texture. A copy that a GPU fault recovery dropped is still made again at the texture's next
  use. The wait for the previous upload of the same texture is unchanged, and so are the copy's barriers, so animated
  textures still wait as before. WindowServer gives nothing back early. Way out: NVMTL_ASYNCUP_REAP=0 restores the old
  lifetime. With the switch at 0, and in WindowServer, an upload's record is still reached through the small header;
  what is kept and what is waited for is as in 3.0.5. Checked with host tests (tools/test_up_reap.c, including a
  three-thread race; tools/test_up_reap.py; the updated tools/test_plugin_defaults_wiring.py) and with the Objective-C
  check. Not run on a GPU; no gain measured.
- **A draw records less on the CPU: NVK no longer walks 642 empty bindings for every descriptor set, and the plugin
  skips binds the command buffer already holds.** In capture A, 4.2 % of the render thread is in
  nvk_AllocateDescriptorSets, most of it the function's own time. nvk_descriptor_set_create looked for immutable
  samplers by visiting every binding number up to the layout's highest, and the plugin's layout goes up to 641. The
  new NVK patch nvk/nvk-desc-alloc.patch, last in the 3.0 chain, skips that walk for a layout with no immutable
  samplers. A new set comes out exactly as before. In the plugin, a command buffer remembers its index buffer, vertex
  buffers, topology and primitive restart, and does not record them again when a draw repeats them. NVK records every
  buffer bind as a pushbuffer write and a macro run on the GPU. WindowServer still records every bind.
  `NVMTL_BIND_CACHE=0` turns this off. A setDepthStencilState that repeats the state the encoder last applied, in the
  same pass and command buffer, records nothing. Mesa already dropped those equal values, so this saves only CPU
  calls. WindowServer still applies every set. `NVMTL_DS_SKIP=0` turns this off. Without a switch, because nothing
  they produce changes: the ShaderWrite census of setFragmentTexture looks at each texture only once, and a draw reads
  its pipeline's functions from ivars. A shader with nothing embedded skips four associated-object lookups. What was
  run: on Linux only. The new host tests ran (tools/test_nvk_desc_alloc.py, test_draw_cache.c and .py,
  test_ds_skip.py, the new arms of test_repeat_bind.c, test_sw_census.py, test_embedded_fast.py), with mutations of
  each header and of the wiring to check that the tests fail when the logic is wrong. The existing tests that read the
  changed code also ran, and objccheck reported 0 new errors and 0 new warnings. Nothing was built on a Mac or run on
  a card, and no gain has been measured.

#### Compute shaders and shader translation

- **A compute kernel's threadgroup-size variants no longer stall other threads, the creation-time compile is reused,
  and a refused function-constant variant is not translated again.** nvmtl_compute_variant compiled each new
  threadgroup-size variant (spirv_to_nir and the GPU compiler) while holding one process-wide lock, so a dispatch of
  any kernel on any other thread waited for that compile. The variant is now compiled outside the lock, and the table
  is checked again before the result goes in. If another thread put in the same size first, its pipeline is used and
  the unused copy is destroyed before any command buffer sees it. A thread whose own compile failed also takes a
  pipeline another thread kept for that size. NVMTL_CVAR_PARALLEL=0 compiles under the lock again, as in 3.0.5. The
  pipeline that newComputePipelineState compiles, so that a kernel NVK cannot build is refused, was compiled
  unspecialized, while every dispatch uses a specialized variant. It was never bound, and the first dispatch compiled
  the kernel a second time. It is now compiled with the specialization a dispatch at the kernel's default size uses.
  Those are the module's own default values, so it builds and refuses exactly what it did before. It is kept as that
  size's variant. A module whose threadgroup size is not given by single SpecId defaults stays unspecialized.
  NVMTL_CVAR_SEED=0 brings back the unspecialized compile with an empty variant table. A function-constant variant
  that the translator itself refused was translated again on every request. That refusal is now remembered for the
  process under the variant's cache key, and the same message is returned. A module stored on disk is still read
  first, nothing is remembered with NVMTL_FCCACHE_OFF, at most 4096 refusals are kept, and other failures are not
  remembered. NVMTL_FC_REFUSAL_MEMO=0 sends every request to the translator again, as in 3.0.5: the answer is the
  same, but the re-translate FAILED line and the NVMTL_FC_FAIL_DUMP and NVMTL_FC_DUMP_ALL files come back on each
  repeat, and the rest of the variant cache stays on. Run: new host tests test_cvar.c/.py, test_cvar_seed.c/.py and
  test_fc_refusal.c/.py, plus the existing tests that read the files touched; objccheck reports 0 new errors and 0 new
  warnings. Not run: anything on a card or on macOS. No speed gain is measured: in the two captures of the slow game
  no variant compile and no function-constant translation shows up.
- **Three shader translations that failed on the card are fixed: a sampler picked through call-site attributes, the
  elements of an argument-buffer array of handles, and a loop that switches between two buffers.** These are the rows
  `xlate.paths-samplerphi`, `xlate.paths-abarray` and `xlate.paths-walk` of the card checks, which failed on an RTX
  5050. (1) A sample through a sampler that a switch picks among several [[sampler(n)]] arguments was refused
  ("sampler operand is a pointer whose exact sampler state was not recovered") whenever Apple's compiler wrote
  parameter attributes on the call (`ptr addrspace(2) readonly captures(none) %sel`). The inline retry now finds the
  operand through those attributes and keeps them on each cloned call (`translator/translator/src/native/inline.rs`).
  Modules that translated before are unchanged, so there is no switch. A sampler picked by a chain of selects already
  sampled once per candidate; a test now pins that. (2) An argument encoder built from a function's AIR gave an array
  of textures one field. So `setTexture:atIndex:` past element 0 of `array<texture2d<float>, 8>` or `texture2d<float>
  t[8]` was refused, and those textures read as black. Each element of a texture, sampler or buffer-pointer array now
  has its own [[id]], 8 bytes apart, read from the member's count, size and location pair
  (`plugin/nvmtl_argbuf_elems.h`, `nvmtl_air_argbuf_walk` in `plugin/NVMTLLibraryLoad.m`). Way out:
  `NVMTL_ARGBUF_ARRAYS=0`. (3) A kernel whose loop picks its next buffer with a select between two buffer arguments
  was refused ("cannot reinterpret pointer for owned OpSelect type mismatch"). It is now built with every buffer read
  through its device address, but only as a last resort for the whole translation. The wrapper tries several option
  sets for one module (formatless storage images, then the default options; argument-buffer samplers, then ordinary
  ones), and a first attempt built with addresses could have taken the place of a later attempt that translated the
  module in 3.0.5. So the translator builds with addresses only inside a scope that the wrapper enters once, after
  every attempt made in 3.0.5 has failed with one of the two cross-root refusals (`with_device_address_last_resort` in
  `translator/translator/src/construction.rs`, `with_address_last_resort` in `translator/wrapper/src/lib.rs`). If that
  last try fails too, the error is the same text as before. The specialization-constant entry never takes the last
  resort, and neither does a function the plugin may still link at pipeline creation. The translator cannot tell which
  process it is in, so this is not kept out of WindowServer; only modules that were refused change. A function-
  constant set that both the chain and the last resort refuse runs the chain twice each time it is asked for. Way out:
  `NVMTL_XLATE_ADDR_RETRY=0`; the plugin then adds `-ar0` to its shader cache tag, so a module translated with the
  retry on is never served from the cache with it off. How Apple's compiler of macOS 26 shapes these kernels on the
  card is not known: the fixtures are the shapes the failing rows reported. Not run on the card: the three rows still
  have to pass there. No performance change is claimed.

#### Video memory and DLSS

- **The VRAM pager no longer puts the allocating thread to sleep.** When a texture, Private buffer or heap was refused
  VRAM, the thread creating it asked the res2 pager for room. While the pager's worker held the pass, that thread
  polled for up to 30 ms. Then it slept up to 16 ms more, waiting for the VRAM of the evicted textures to be freed.
  That VRAM is freed only after the command buffers using it complete, which at 20 fps can take longer than the wait.
  When either wait ran out, the allocation went to system memory anyway. Now the thread hands the work to the worker
  and goes on. After the pass it frees whatever has already drained, without waiting, and the worker frees the rest
  later. The resource takes the same system-memory fallback it took when the old wait timed out. The call made after
  vkAllocateMemory itself failed always has that fallback, because NVK offers a system-memory type that every image
  accepts. Three Private buffers had no fallback: the visibility accumulation scratch and the acceleration-structure
  user-ID tables. A failed visibility scratch set an error on the app's command buffer. These three now retry in
  system memory, as the acceleration-structure scratch already did. This matters only for titles that fill the card;
  the measured profile shows no VRAM pressure. WindowServer keeps the old waits. Way out: NVMTL_RES2_NOWAIT=0 restores
  both waits, the 250 ms pause after a timed-out wait and the plain refusal of those three buffers, exactly as in
  3.0.5. Run: tools/test_res2_nowait.c and tools/test_res2_nowait.py, the host tests that mention NVMTLObjects.m or
  res2, and the plugin Objective-C check (0 new errors, 0 new warnings). Not run on a GPU or on macOS.
- **DLSS no longer leaks image views, and a view can no longer outlive its image.** DLSS keeps its own views of the
  scaler's images in a table that held 48 entries. Once it was full, every evaluation made a new VkImageView that was
  neither cached nor destroyed, so a long session could slowly fill NVK's descriptor table. Two paths also destroyed
  an image without taking its DLSS views out of the table: the residency pager's re-back of a purgeable texture, and
  the release of a migrated texture's old backing. Those entries leaked, and a later image that reused the same handle
  value could be handed the dead view. The table now grows and behaves the same up to 48 entries. The backing release
  drops the image's DLSS views the way the image destroy does, and the re-back retires them through the image grace
  like the texture's main view. Frame Generation also skips its priming evaluation when the previous colour texture is
  a different Metal object (such as a texture view) for the very image it was given at the last encode, since the core
  would be handed the same view again. A stale comment saying Frame Generation is not driven is corrected. Way out:
  NVMTL_DLSS_FG_SAME_IMAGE=0 brings back 3.0.5's priming decision, in which only the same texture object counted as
  already seen, so the core is fed the same sequence of evaluations as before. The table's growth and the dropped
  views have no switch, since they only stop leaks and drop dead entries. Run: a host test of the cache and the
  priming decision (tools/test_dlss_views.c), text checks of the wiring (tools/test_dlss_views.py,
  tools/check_view_grace.py) and the Objective-C check. Not run: DLSS on a card. No performance gain is claimed: DLSS
  is not part of the measured profile.
- **The memory table is looked up through an index.** The plugin records every device memory so a fault report can
  name what an address held. Every vkFreeMemory and every image commit looked a memory up by scanning that table
  backwards under g_mm_lock: one step per memory allocated since the one sought, and the whole table for an old heap
  or pool memory. The table now has a hash index like the image history's. It returns the same record the scan
  returned in no more steps, and its walk always ends even when a fault report reads it without the lock. No way out
  is needed: the answers are identical, and the table still keeps every record. The histories still grow by about 300
  bytes per texture, as in 3.0.5. A compaction was tried and withdrawn: on a host model it held the commit and dealloc
  locks for 30 to 150 ms each time it ran. Run: tools/test_mem_index.c (the index checked against the scan with
  reissued handles), tools/test_mem_index.py (three broken indexes must fail it), tools/test_slots.c, the Objective-C
  check. Not run on a card; no gain was measured.

#### Diagnostics

- **NVRM says whether write-combined mappings are really write-combined on this machine.** On AMD hackintoshes,
  AMD_Vanilla enables a fix-PAT kernel patch by default (Algrey / Zormeister). It replaces the write in which XNU sets
  entry 6 of the CPU's page attribute table to write-combining with a fixed table, 0x0007010600070106. In that table
  entry 6 is uncached-minus. Every kIOMapWriteCombineCache mapping, BAR1 included, is then uncached unless the
  firmware's MTRRs mark the range write-combining, and each CPU store goes out as its own bus transaction. No log or
  report said which table a machine runs. NVRM now reads IA32_PAT once at start with rdmsr_carefully and publishes it
  read-only: `sysctl debug.nvrm_pat_wc debug.nvrm_pat_why` (debug.nvrm_pat holds the raw value, which sysctl prints in
  decimal), plus one log line "page attribute table 0x... (entry 6 .., entry 2 ..; <table>): <verdict>".
  debug.nvrm_pat_wc is 1 when write-combined mappings are write-combined. It is 0 when they are not: uncached, write-
  through or write-protect, and for uncached-minus unless an MTRR says WC. It is 2 when the entry is write-back, so
  the firmware's MTRRs decide (the Shaneee table), and -1 when the table could not be read or decoded. The diagnostic
  decides nothing: no MSR is written, no mapping changes and no code path reads the verdict, so there is no switch.
  Checked on Linux: the kext compile check of all units, the import check (rdmsr_carefully is exported by
  com.apple.kpi.unsupported on macOS 15 and 26), and the host tests tools/test_nvrm_pat.c and tools/test_nvrm_pat.py.
  It was not run on a Mac or on the card, and the value on the measured machine is not known yet.
- **The OpenGL path now says in the system log how two of its silent decisions went, once per process.** In a release
  build the system log only got lines that name a failure, so two decisions logged nothing there. The first is the
  patch that lets Apple's OpenGL renderer use the last vertex as provoking vertex. It accepts only the renderer table
  of macOS 15.8.1, and on any other build it quietly left the renderer rewriting the index buffer of every indexed
  draw. The second is the GL vblank wait correction, which logged neither which branch a frame took nor the periods it
  used. A process that loads Apple's renderer on a device with last-vertex provoking now writes one line naming the
  outcome: patched, set by Apple, profile differs (with its 20 bytes and the macOS build), no profile symbol, or page
  not writable. On a device without last-vertex provoking nothing is registered and no line is written, as in 3.0.5.
  The first vblank wait of a process writes one line naming its branch, its entry count, the display's period and
  CoreAnimation's spacing. Find them with `log show --last 1d --style compact --predicate 'eventMessage CONTAINS "GL
  provoking vertex"'` and `log show --last 1d --style compact --predicate 'eventMessage CONTAINS "GL vblank wait"'`.
  Nothing else changes: the patch accepts the same table and writes in the same cases, and the dyld callback is
  registered exactly when 3.0.5 registered it. The vblank branch comes from the same rule as before, which a host test
  checks over an edge grid. There is no switch, because this is logging only. Run: tools/test_gl_diag.c and
  tools/test_gl_diag.py (new), the existing tests that read these files, and objccheck (0 new). Not run: anything on a
  Mac or a card. No gain is claimed; these lines provide the data a later decision needs.

#### Found when the host suite first ran on a Mac

- **WindowServer's zero-surface flush drains its own autoreleases before it hands its textures to another thread.**
  Since 3.0.1, a WindowServer surface that is all zero is cleared on the GPU instead of uploaded. When those fills go
  out as their own submit, the textures are handed to another thread to be released: the flush can run inside a
  submit, under the submit mutex, and a texture's last release frees its surface. That holds only if the array handed
  over is this thread's last reference. The flush kept its snapshot of the owed textures out of the caller's
  autorelease pool, but not the next call, which empties the weak table they were in, nor anything else it calls. On a
  Mac a reference to each texture does end up in the caller's pool (the test below shows it; emptying the weak table
  is the likely source, being the one call on the textures that ran outside a pool), and that pool is popped on the
  submitting thread as soon as the flush returns. If the other thread had released first, the pop was the texture's
  last release, inside the submit. The flush's whole body now runs in a pool of its own, and the hand-off happens
  after that pool has drained, while the array still holds every texture. The same calls run in the same order under
  the same lock, so there is no switch. `tools/test_surfin_zero.py`, the test of this rule, failed on a Mac at 3.0.5
  and passes on Linux, where the weak table of gnustep-base removes without loading. The test now calls the flush in a
  pool, as the plugin does, and prints what it counted. On the Mac, at the commit that changes only the test, it
  failed with "the watched root was freed 1 time(s), 1 of them on the submitting thread"; with the flush changed it
  passes. On Linux a model of a table that autoreleases what it removes gives the same failure against the old flush
  and passes against the new one, and a release in place of the hand-off fails the test, as it must. Not run in
  WindowServer or on a card.
- **Two host tests that had failed since 3.0.3 pass again.** `tools/test_evwait_wiring_kernel.py` looked for the exact
  text of selector 7's answer, which the GPU capability producer rewrote: the selector still answers both event-wait
  bits, and now ORs a third bit into them. The check accepts that shape and nothing looser, with three new controls (a
  later feature assigned over the bits, the bits masked off, a second value returned).
  `tools/test_package_licenses.py` asks `NOTICE` and `docs/PROVENANCE.md` to name every file the NVK patches add under
  the driver's own licence. `nvk/nvk-gpu-caps.patch` added a fifth, `nvrm_gpu_capabilities_abi.h`, and both documents
  now name it. No code changes.
- **The bind cache's switch is defined ahead of the pipeline bind.** `tools/test_graphics_bindings.py` compiles the
  text of `plugin/nvmtl_vk.c` between two functions on its own, and this branch had written `nvmtl_bind_cache_on`
  between them, so the test no longer compiled. It needs Vulkan headers and is skipped on Linux; the Mac showed it.
  The function moved, unchanged.

### The ways back

Each is new, of tier escape, with a row in `tools/switches.tsv`, read once per process, and is a line of
`/Library/GPUBundles/nvmtl/nvrm610.conf` (or the process's own environment with `NVMTL_PROCESS_ENV=1`). With it at 0
the code path is the one of 3.0.5.

- `NVMTL_GL_FAST_FLUSH=0`: `commitAndWaitUntilSubmitted` only waits for the submit-ahead thread or the worker.
- `NVMTL_CB_LEAN=0`: every command buffer makes its two handler groups up front, and every `-commandBuffer` takes the
  queue class lock to find the capacity semaphore.
- `NVMTL_SURFIN_SKIP_OWN_OUT=0`: a bind of a surface texture waits on the surface's monitor for the copy-out in
  flight.
- `NVMTL_GL_WAIT_EVENT=0`: `waitUntilComplete` of a buffer or texture polls every 50 us.
- `NVMTL_TEX_RECYCLE=0`: every dead texture is destroyed and every new one created and allocated.
- `NVMTL_ASYNCUP_REAP=0`: a finished asynchronous upload keeps its staging buffer and its place until its texture is
  next written, read back or freed.
- `NVMTL_BIND_CACHE=0`: the index buffer, the vertex buffers, the topology and primitive restart are recorded before
  every draw.
- `NVMTL_DS_SKIP=0`: a repeated `setDepthStencilState` is applied again.
- `NVMTL_CVAR_PARALLEL=0`: a compute variant is compiled under the variant table's lock.
- `NVMTL_CVAR_SEED=0`: the creation-time compute pipeline is unspecialized and thrown away.
- `NVMTL_FC_REFUSAL_MEMO=0`: a refused function-constant variant is not remembered; every request goes to the
  translator, and its FAILED line and dump files are written on each repeat.
- `NVMTL_RES2_NOWAIT=0`: a thread refused video memory waits for the pager (30 ms and 16 ms, and the 250 ms pause),
  and the three Private buffers without a fallback are refused.
- `NVMTL_DLSS_FG_SAME_IMAGE=0`: Frame Generation records its priming evaluation whenever the previous colour texture
  is not the very object it last saw.
- `NVMTL_ARGBUF_ARRAYS=0`: an argument-buffer array of handles gets one field, its first element.
- `NVMTL_XLATE_ADDR_RETRY=0`: a module refused for a select between two buffer roots stays refused. The plugin then
  adds `-ar0` to its shader cache tag, so the two modes never share a cached module.

`NVMTL_TEX_RECYCLE`, `NVMTL_ASYNCUP_REAP`, `NVMTL_BIND_CACHE`, `NVMTL_DS_SKIP`, `NVMTL_GL_FAST_FLUSH`,
`NVMTL_SURFIN_SKIP_OWN_OUT` and `NVMTL_RES2_NOWAIT` change nothing in WindowServer, which keeps the path of 3.0.5
whatever they say.

No switch: the NVK patch `nvk/nvk-desc-alloc.patch` (a new descriptor set comes out exactly as before; leaving the
patch out of the chain in `build/build_nvk.sh` restores the walk), the single resource set per command buffer, the
ShaderWrite census flag, the pipeline state's function ivars and the embedded-sampler short-circuit, the memory
table's index, the growth of the DLSS view table and the views dropped with their image, what the texture recycle
cache gives back and when (it acts only on that cache, which `NVMTL_TEX_RECYCLE=0` leaves empty), the sampler found
through call-site attributes in the translator (only modules that were refused change), the pool around WindowServer's
zero-surface flush (the same calls in the same order under the same lock), and the three diagnostics
(`debug.nvrm_pat`, `debug.nvrm_pat_wc` and `debug.nvrm_pat_why`, read-only, and the two OpenGL log lines), which
decide nothing.

### What ran where

**The driver was built on a Mac and runs on one card; no change of this section has been measured on its own.** What
ran, and where:

- **Linux, compile checks of the final tree.** The kext compile check of all 25 units passes. The import check against
  the exports of macOS 15 and 26 gives the one line it gives for 3.0.5 (`_ceutilsMemset_IMPL`). The plugin's
  Objective-C check, at two SDK levels against a stand-in SDK, reports 0 new errors and 0 new warnings. None of these
  links or runs the driver.
- **Linux, the host suite** (`python3 tools/run_all_tests.py`), on the final tree. 350 passed, 41 failed, 11 skipped
  and 10 not run, of 412. The 41 fail on Linux at 3.0.5 too (311 passed and 43 failed of 376 there), by name, because
  they need a Mac: Apple's clang, Mach-O tools, frameworks; the other two of those 43 are the two tests fixed above.
  With wrappers that let the Objective-C tests compile and run on Linux, 375 passed and 17 failed: the 19 that fail at
  3.0.5 (337 and 19) less the same two. Six tests need Vulkan headers and are skipped in both runs. Given the headers
  and a stand-in for the Mach headers they include, five of them pass on Linux; the sixth,
  `tools/test_opengl_perf.py`, needs Apple's Objective-C runtime.
- **Linux, the translator's unit tests** (`cargo test`). 2520 passed and 25 failed in the translator, 61 passed and 2
  failed in the wrapper; the failures are the ones 3.0.5 has, and only the tests added here changed.
- **Review.** Each change was written by one engineer and read by a second whose task was to break it. Four more then
  read the whole branch by area, each finding checked by another reader. They found no defect of rendering, crash or
  hang. They found five of other kinds, all fixed in the entries above: two places where the textures kept for reuse
  were not given back before memory still in use was touched, fault reports that mislabelled such textures, and two
  changes with no way back. One change was withdrawn, as its entry says.
- **A Mac, the host suite.** The same command ran on the Mac the captures came from (macOS 26.7.3, with 3.0.2
  installed), from a separate checkout, with the Vulkan headers an earlier build of NVK had fetched there. Nothing was
  installed or loaded for it, and no driver was built. At e0624e87, this branch before the four commits of "Found when
  the host suite first ran on a Mac": 394 passed, 4 failed, 4 skipped and 10 not run, of 412. The four are the tests
  those commits are about. Three of them fail on that Mac at 3.0.5 too, and the fourth is the one this branch had
  broken. At 3994f024, the last commit that changes code or tests, the 76 tests that name a file changed since ran
  again, those four among them: 74 passed, none failed, 1 was skipped and 1 not run. The other 336 were not run again
  there. The skipped tests need NVIDIA's video headers or ffmpeg, and one runs `cargo`, which was given an empty cache
  on purpose so that it would leave the tester's own alone; the tests not run need a built translator. After both runs
  the checkout was clean, nothing under the system's folders was newer than the run, and WindowServer was the same
  process. The suite does leave crash reports in the tester's own log folder, 48 over the two runs: they come from
  control programs the tests build and end by a signal on purpose.
- **Before the suite went to a Mac**, every test and script changed since the last such reading (133 files, everything
  after c5e90f2e) was read for what it starts and writes on a Mac, and the whole suite was run on Linux with every
  program start recorded. One test was changed for it: `tools/test_translator_whole_views.py` runs the tester's own
  `cargo`, and when the runner stopped it at its limit, the translator's `Cargo.lock` stayed rewritten. It now stops
  cargo itself before that limit and puts the file back. The two test files changed after that run were read the same
  way before they went back to the Mac.
- **A Mac, the driver built.** At 52a77475, on the same Mac, in the working directory that had built 3.0.2
  (`./build.sh build`, then `package`; the build installs and loads nothing): the eleven components whose sources
  changed were built again in 420 s, and the three that count their warnings (NVRMFB, the plugin, libnvencvt) report
  0. The package is 3.0.6, every file matches `SHA256SUMS`, the plugin bundle's seal verifies, and the installer is
  byte for byte the one of 3.0.2. The commit after it, 3cdddbc5, only corrects the release named in three rows of the
  switch registry.
- **The card.** The Mac's owner installed that package on 2026-11-20 over 3.0.2, so 3.0.3, 3.0.4 and 3.0.5 ran on a
  card for the first time with it. The installer's check passed and kept 3.0.2 as the fallback, the Mac started
  normally, and no fault was reported in the hours of use that followed, the modded pack among them.
  `debug.nvrm_pat_wc` reads 0 there, with the table 0x0007010600070106 that the fix-PAT kernel patch of AMD_Vanilla
  leaves: write-combined mappings are not write-combined on that machine, which is what the diagnostic was written to
  tell. The modded pack draws through Zink on NVK: three `sample` captures with the kernel's counters give 55 to 64
  frames a second, the GPU 41 to 53 % busy, in the same pack that gave 74 and 19 on 3.0.2 through Apple's renderer
  (the scenes were not matched). Those captures are where 3.0.7 starts (above).
- **Not run anywhere:** the card checks on 3.0.6, so the three rows `xlate.paths-samplerphi`, `xlate.paths-abarray`
  and `xlate.paths-walk` still have to pass on the card; and a measurement of any one change on its own. The gain of
  the changes to Apple's renderer is unmeasured: the game they were measured for no longer uses that renderer.

## 3.0.5

**Not released.** This section describes the branch being prepared as 3.0.5, on top of 3.0.4 (below, in the same pull
request and not released either). It brings into this tree, adapted, three pull requests that are still open in the
original project (https://github.com/nullmoth/nvidia-macos-driver): 597 by Geek (a display that stays dark after a
wake), the parts of 487 by dreamelite96 that apply to Firefox, and what 3.0.4 had not taken of 524 by emretascii (its
commits are signed "Antigravity (local port)"): the shader translator's whole-object views (League of Legends, the
iOS Simulator), and in the plugin protected video, the 4K 240 Hz WindowServer crash, the order of "scheduled" and the
Simulator. 596 is left aside. Every change of what runs by default has one way back, a new row of tier escape in
`tools/switches.tsv`. On an RTX 50, 487 brings no VP9 or AV1 decoding (below). Nothing of 3.0.5 has been built on a
Mac or run on a card: it was compiled and its host tests were run on Linux only (below, "What ran where").

### Fixes and changes

#### A display that stays dark after a wake (597)

- **A head that does not light at power-on is tried again (597, adapted).** NVRMFB's `setPowerState(1)` threw away the
  result of its own attempt to light the head. After a display sleep it is the only thing that lights the head, and
  after a system wake the GPU wake call (`gpuWakeFire`, queued by NVRM's `NVRMGpuPower` 1) can run first, find the
  framebuffer still powered off and leave the head to it. Either way a failure there was not tried again, and only a
  failed commit of the mode was logged: the display stayed dark while WindowServer kept drawing (597's report: a system
  wake whose log ends at "GPU back: head 0 left as is (slept 1, power 0)" and "setPowerState 1"). A failure in
  `setPowerState(1)` now queues the GPU wake call 500 ms later and counts as the first attempt. A failed attempt of the
  call queues it again 500 ms later, so a wake gets three attempts in all, whichever of the two makes the first. The
  count starts again when the head is lit, when the call gives up or finds nothing to do, at a GPU standby, and with a
  failed `setPowerState(1)`. `NVRMGpuPower` 1 keeps the count: when it comes just after a failed `setPowerState(1)`
  (NVRM marks the GPU running before NVKMS has resumed), it runs the queued retry at once, and the wake still gets three
  attempts, not four (`kexts/NVRMFB/fb/nvrm-fb.cpp`). Adapted: 597 queued the call at once from `setPowerState(1)`, then
  slept 500 ms inside the call and tried once more, which held the display gate through the wait. That gate is one
  recursive lock for every framebuffer, and flips, the cursor, mode changes and the video memory worker take it too.
  Here the call is queued again through `scheduleCall` with a deadline (`THREAD_CALL_OPTIONS_ONCE`, as the video memory
  worker does). Also from 597, two log lines: which branch a power-on took (`power on: paging N, GPU standby seen N`;
  with 0 and 0 it is normal at the first power-on after start, once for each framebuffer), and how long a system wake's
  replay of the `DidWake(false)` owed beyond the first took, one for each further sleep since the last full wake
  (`replay N ms` in the `system wake:` line; the first is not timed, so after a single sleep it says 0 ms). Not taken
  from 597: moving `pagingState` and WindowServer's wakeup ahead of that replay. That change would affect every system
  wake. The replay already runs under the IOGraphics work loop gate that WindowServer sleeps on, so moving the wakeup
  earlier gives it nothing until the gate is let go. It could also let WindowServer in before the accelerators are
  enabled again. The new `replay N ms` line will show whether that replay, after several sleeps, is ever slow enough to
  matter. 597's `tools/test_nvrmfb_wake_order.py` is not taken with it. `docs/SLEEP-WAKE.md` describes the path ("A head
  that does not light").

#### Firefox (487)

- **Firefox's sandbox no longer leaves the video library and the plugin's surface imports without a descriptor (487).**
  Firefox's sandboxes refuse `/dev/null`. libnvencvt bound each of its kernel connections to the descriptor that open
  returned, -1 there, which the kernel takes as "detached", and went on with it; `rm_fd_token` (`vendor/nvenc/src/rm.c`)
  now gives the read end of an empty pipe when the open is refused (EPERM, EACCES), and when no descriptor can be had at
  all the connection is closed and the open fails, with a line on standard error. On RTX 30 and 40, where the plugin
  registers libnvencvt by default in every process that creates a Metal device (`nvmtl_nvencvt_selected`,
  `plugin/NVMTLDevice.m`), this is what lets its decoders run in Firefox's sandboxed processes: bound to -1, all of the
  library's connections had the same number, the kernel finds the connection that a mapping or an event names by that
  number (`nv_xnu_lookup_fd`, the first one), and a session could not start. Where the library has registered in a
  Firefox process, VP9 and AV1 (on RTX 40 also H.264 and HEVC) now go to its hardware decoders by default; this has not
  run with Firefox here. On RTX 50 the library registers only in the decode service, so this changes nothing there. The
  plugin's import of a surface's family VRAM (`nvmtl_vk_surface_vram_words`, `plugin/nvmtl_vk.c`) opened `/dev/null`
  with a plain `open()`, failed there without a word and left the surface on a private image and its copies; it now
  opens it with `nvmtl_icd_open` (`plugin/nvmtl_fd_token.h`), as NVK's own opens already did, and logs the import it
  could not make when even that fails. This is the one change of the group that acts on an RTX 50 by default: in a
  Firefox process whose sandbox refuses `/dev/null` and that draws with Metal, IOSurface textures now live in the
  surface's own video memory, as in every other process, instead of private images and their copies. No sandboxed
  Firefox process has run that path here, so it has one way back, `NVMTL_SURFACE_VRAM_PIPE=0`. The library keeps a copy
  of the fallback of its own: `vendor/nvenc` is MIT and the plugin's header is not.
- **In Firefox's processes the video library keeps each picture it emitted until eight later ones are out (487,
  adapted; `NVENCVT_DEC_HOLD=0` turns it off).** Firefox's media process passes a decoded picture's IOSurface on by
  number and its GPU process takes it a few milliseconds later; the pull request's author saw the pool hand the pixel
  buffer out again in between, and older or half-written pictures, on an RTX 4060 Ti. `vendor/nvenc/vt/nvencvt_dec.c`
  holds the reference in a ring of eight per session (its own lock; released when the session is invalidated, after
  the output thread has ended and before the session's pool). The pull request held `NVDEC_HOLD` pictures (0 to 16,
  default 8) in every process and took the count from a variable read on each session's start; here the count is
  fixed, the switch is read once when the decoders register, and the hold is kept to `plugin-container`, where the race
  was seen. Elsewhere (the decode service that Safari and QuickTime use, Chromium's GPU process, ffmpeg) nothing has
  shown it, and the hold would only cost memory: about 25 MB per 1080p session, 100 to 200 MB at 4K.
- **The VP9 and AV1 decoders keep NVDEC's count of blocks in error, and their log says it (487).** `err_blocks` in
  `src/vp9dec.c` and `src/av1dec.c`; the decode log lines print it with the frame's number (AV1 also the decoded
  surface, its film-grain twin and the tiles), `NVENCVT_LOG=2` logs every VP9 and AV1 frame instead of the first three
  and the errors, and each line of the decoders' log starts with the time of day.
- **The shared import cache gives an 8 GB card the large card's limit (487).** With `NVMTL_SURF_IMPORT_CACHE=1` (off by
  default) surfaces up to 64 MB are kept on a card with 7 GiB of VRAM or more, 16 MB below: the line was 8 GiB, and an
  8 GB card reports a little less (an RTX 4060 Ti 8188 MB), so it got 16 MB and the 4K and 5K surfaces WindowServer
  composites were left out (`plugin/NVMTLDevice.m`).
- `docs/WINDOWS-PARITY-MEDIA.md` has a Firefox section: what NVEncVT offers it on RTX 30 and 40, these changes, and that
  on RTX 50 none of this brings VP9 or AV1 decoding (NVEncVT does not register those decoders on Blackwell; Firefox
  decodes them in software as before), while Firefox's H.264 there stays on the driver's own decoder.
  `vendor/nvenc/SOURCE` lists the library's changes.
- Not taken from 487: the launcher `tools/firefox-nvdec` (it preloads a small loader, `libnvencvt-loader.dylib`,
  into every Firefox process with `DYLD_INSERT_LIBRARIES`, which works only because the driver's AMFI boot arguments
  let Firefox's runtime accept it; the loader opens the library only in the processes `NVENCVT_INJECT_ONLY` names,
  `plugin-container`, and that variable would be a switch to set to get the decoders, which the switch registry
  refuses); compiling the `/tmp` diagnostics out behind `NVENCVT_TMP_DIAGNOSTICS`
  (this tree honours a flag file only when root made it and writes its outputs without following links, 0600,
  `vt/nvencvt_diag.h`), with the bound of the barcode check that came in the same hunk; compiling out the renaming of
  the encoders' names behind `NVENCVT_REGISTRY_RENAME` (it would take away the names apps show for a working path
  that has `NVENCVT_NO_RENAME` already, and has nothing to do with Firefox); `docs/NVENCVT.md`, which this tree does
  not have. Zink's presentation (`zink/nvk_present.c`) and the nvk-bridge (`tools/nvk-bridge/bridge.mm`) open
  `/dev/null` the same way, but there the fallback alone would not help: their copy of NVK opens `/dev/null` for its own
  kernel connections with nothing rebound, so in such a sandbox Zink gets no device and leaves the context with
  Apple's renderer before it imports anything.

#### The shader translator: League of Legends and the iOS Simulator (524)

- **The shader translator retypes a local variable only through a pointer proven to address all of it (524, League
  of Legends).** `infer_local_alloca_pointees` (`translator/translator/src/native/ir/alloca.rs`) let a GEP through any
  pointer derived from a local offer its source type as the local's own type: a pointer to one member, a dynamic
  offset, or a phi with a null or unknown arm. A member array as large as the struct around it (a matrix's columns)
  could then replace the struct's storage. Now a new graph of whole-object aliases (`whole_object_aliases`: identity
  casts, GEPs whose only index is zero, and phis whose every arm is a local pointer, followed through loops) must
  prove that the pointer addresses the whole of exactly one local, and that every pointer on the way leads back to
  that local. Only then does the pointer retype the local. Those proven pointers also count for the two rules that 3.0.4
  applied only to the local and its identity casts: a load or store through a zero-index GEP or such a phi that reads
  the local's float3 rows or a shorter vector wide now retypes the local, and so does a helper that takes such a
  pointer. And a helper's byte view of a whole local that holds no pointers now makes the local a byte array of its
  size (`infer_local_alloca_pointees`, at the call). That is what the iOS Simulator's shaders need: without it the
  translator of 3.0.4 refused six of the ten Simulator fixtures of 524 (a union of scalars, vectors or 32-bit words,
  read by a helper through a byte view at a dynamic index): "a struct member index must be an OpConstant", and the
  shader fell back. All ten now translate and pass the validator (`translator/wrapper/tests/simulator_owned.rs`). On
  the tree's other fixtures nothing changes: 128 of the 130 modules that 3.0.4 produced come out byte for byte the
  same. The other two (one Simulator fixture, translated as a kernel and with the stage detected) come out different
  but still valid. The 12 modules 3.0.4 did not produce are the six Simulator fixtures that now translate, each under
  both stages. The rule and its 21 unit tests come from pull request 524 of the original
  project (its part T3), unchanged but for formatting; the tests are in `native/ir/whole_view_tests.rs`.
- **A pointer parameter read both as a pointer-free struct or array and as bytes gets one byte array of that size
  (524).** `infer_pointer_pointees` (`native/ir/pointer_pointees.rs`) gave up on such a parameter or kept the type it
  saw first. Now, when every aggregate view of the parameter has one size, the parameter gets a byte array of that
  size. This is not limited to helpers: it applies to every pointer parameter of every function, a kernel's own
  device, constant and threadgroup buffer arguments included, in place of their metadata type, and the unit tests
  have no such buffer. A pointee that holds pointers, or one whose declared size (`air.arg_type_size`) is a different
  size, stays as it was. A typed pointee of the same size that holds no pointers and came from the function's own
  GEPs now also gives way to a callee's pure byte image of the object, as one from metadata already did in 3.0.4 (that
  case does not depend on the switch). None of the ten Simulator fixtures needs this rule: their helpers read the
  union only as bytes, and the rule above makes them translate.
- **Not taken from 524:** the emitter half of its part T4. That half gives a pointer select deferred typed arms and a
  branched load per select, and rewrites raw select and GEP chains (`native/emitter/pointers/raw_select.rs`,
  `select_gep.rs`). Four of its select fixtures were left out with it. On this tree it breaks 11 translator tests
  that pass without it, two of them with translation errors: 3.0.4 loads both arms of a pointer select and selects
  the value, and those tests hold that. 524's `raw-select-then-gep` fixture has the same shape as one of them, so no
  narrower version satisfies both. 524's parts T1 (strict `native_powr`) and T2 (scalar atomics on a threadgroup
  argument's root) were already in this tree, and T2's test passes on it unchanged.

#### Metal: protected video, WindowServer at 4K 240 Hz, the order of "scheduled", the iOS Simulator (524)

- **A protected IOSurface (DRM video) whose pixels live in its own VRAM is published on the GPU side instead of failing
  the command buffer (524, P3, adapted).** Such a surface gives the process no CPU mapping (`IOSurfaceGetBaseAddress`
  is NULL), so `nvmtlSurfaceOutDirtied:` could never copy it out and the command buffer failed with "Could not publish
  event segment writes". When the surface's family VRAM is on (`_vramOn`), it is now marked newest in the kernel as any
  other surface is, after the GPU copy of a tiled image into it. Two parts of P3 are not taken as written: it also
  returned YES for a protected surface drawn through a private image and for one whose VRAM mark the kernel refused,
  which would leave the surface's old pixels in place without a word. Both still fail here, as in 3.0.4, now with a log
  line that names the surface as a protected one (`nvmtlSurfaceCopyable` says so too). P3 also skipped the GPU copy of a
  tiled image; it is kept. No switch: nothing that worked before changes.
- **WindowServer's no-copy buffers that cannot be imported and are mapped read only are copied once (524, "fix 4K 240 Hz
  WindowServer crash", adapted; on, `NVMTL_WS_NOCOPY_ONCE=0` turns it off).** In 3.0.4 such a buffer is copied from the
  caller's pages before every submission and written back after every part that may have written it; the write back into
  read-only pages faults, and in WindowServer that is a crash. The pull request copied every such range of WindowServer
  once and handed WindowServer the driver's copy. Nothing in this tree shows how WindowServer fills those pages, and if
  it wrote them after making the buffer the GPU would draw stale bytes with no message, so this takes it only where
  nothing can write them behind the driver's back: every page of the range is mapped without write access, privately
  (copy-on-write or private, not shared, not aliased, not "empty") and with no file or device pager, as `mach_vm_region`
  reports it (`plugin/nvmtl_nocopy_once.h`). The basic answer (`VM_REGION_BASIC_INFO_64`) is asked first, and the
  extended one, which walks the region's pages, only for a region that the basic answer leaves possible (read only, not
  shared): a writable range costs one cheap call. It relies on WindowServer not making those pages writable again and
  writing them while the buffer lives. Such a buffer keeps no caller copy (`_hostPtr`): it is not copied at any
  submission, and a WindowServer part that uses it is no longer held for the worker but can be submitted ahead, with
  `NVMTL_GSUB_HOST_READS=0` too; `NVMTL_WS_NOCOPY_ONCE=0` holds those parts again. `-contents` still returns the
  caller's pointer, the deallocator still gets the caller's pointer and length, and the buffer stays non-purgeable.
- **A part whose caller copies are all buffers it only reads, and that no sealed part may have written, is submitted
  ahead (524, adapted; on, `NVMTL_GSUB_HOST_READS=0` turns it off).** In 3.0.4 any caller copy (`_hostPtr`) held the
  part for the worker. A buffer that a sealed part may write is now marked at the seal (`_gpuWritten`, never cleared),
  and the submit-ahead pass asks again at every try, under the class lock, whether one of the part's read-only caller
  copies has been marked since. The pull request decided once, when the part was prepared, so a writer sealed after the
  reader could have its write in flight under the reader's copy in. A caller copy reached through a texture, a heap or
  a texture alias still holds, as does any part whose resources are not a tracked set.
- **"Scheduled" is reported once the queue has accepted the part and its kernel fence is armed (524, in part; on,
  `NVMTL_SCHEDULED_AFTER_SUBMIT=0` turns it off).** In 3.0.4 it was reported before the submission (in the worker before
  the copies in too, in the submit-ahead pass right after them), so a scheduled handler that hands a surface to another
  process could run before anything held it. The worker's order is 524's; the same order in the submit-ahead pass and
  after the synchronous submit is this tree's. A part the queue refused is still reported, by the worker, before its
  command buffer fails, and a command buffer whose first part is empty (one that begins with an event wait) still
  reports when that part is retired, as in 3.0.4.
- **The iOS Simulator's GPU processes publish every surface write before they report "scheduled" (524, P5, adapted).**
  `SimMetalHost` and `SimRenderServer` get mode 1 of `NVMTL_SCHEDULE_AFTER_SURFACE_WRITES` by process name, as the
  Chromium family does (`plugin/nvmtl_schedule_policy.h`); P5 asked for a `simulator` value of the switch instead, which
  is not taken. An explicit digit still wins, and `NVMTL_SCHEDULE_SIMULATOR=0` (new) keeps the two on mode 3.
- **A command buffer held until its surface writes are published reads the hold under its completion lock, and
  reports "scheduled" as soon as those writes are published when a later part of it waits for an event (524, P5,
  adapted; on, `NVMTL_SCHEDULE_EARLY=0` turns the early report off).** Held to the end, a command buffer whose later
  part waits for an event that only a command buffer committed from its scheduled handler signals never finished (the
  Simulator does this). P5 reported early whenever no later part held; here the early report also needs a later event
  wait (so a reader handed a surface from the scheduled handler does not find its busy tag still pending), and in
  WindowServer it waits while a texture of the command buffer is still deferred for another holder or a later part
  writes any surface. The hold was read before the lock in 3.0.4; it is now set by the seal and read and released
  under the completion lock.
- Not taken from 524: P1 (the surface import cache of 128 slots with its kept host pages), P4, its deallocator
  changes, its read-only blit copy back (578 already brought that, in 3.0.4), P6 and the submit-ahead scan from
  `_nextSubmit`, P8, its skip of the first copy of non-WindowServer no-copy buffers of 1 MiB or more, and the copy once
  of writable or shared WindowServer pages.

### The ways back

- None for 597: the retry runs only after an attempt to light the head has failed. When the head lights, the only
  change is log lines: one more line for each power-on that does not do the family's wake work, and `replay N ms` in
  the system wake line. No switch was added.

- `NVMTL_SURFACE_VRAM_PIPE=0` (a line of `nvrm610.conf`; new, tier escape, row in `tools/switches.tsv`): the plugin's
  family VRAM import opens `/dev/null` with a plain `open()` again, so a process whose sandbox refuses it draws its
  surfaces through private images and copies, as in 3.0.4. The way back from the import alone is narrower than
  `NVMTL_NO_SURFACE_SHARE=1`, which turns surface sharing off in every process. Whether a sandbox that refuses
  `/dev/null` lets the plugin read `nvrm610.conf` has not been checked.
- `NVENCVT_DEC_HOLD=0` (environment, or a line of the root-owned `/usr/local/etc/nvencvt.conf`; new, tier escape, row
  in `tools/switches.tsv`) releases every decoded picture when it is emitted, in Firefox's processes too, as in 3.0.4.

The library's connections in such a sandbox have no switch of their own: `NVMTL_NVENCVT=0` (not new) turns the
library off in every process. 487's other changes have no switch either: they correct what was there (a limit that
missed the cards it named, a log without the engine's error count).

- `NVMTL_WHOLE_VIEWS=0` (the settings file; new, tier escape, row in `tools/switches.tsv`) turns both translator changes
  off. Any pointer derived from a local then retypes it, a pointer-free local that a helper reads as bytes keeps its
  declared type, and a parameter's mixed views keep the type they had, as in 3.0.4. The translator reads the variable
  once per process (`translator/translator/src/native/view_switches.rs`), by the first byte of its raw value, as the
  plugin reads it, so a value that is not UTF-8 means the same to both. When it is 0 the plugin adds `-wv0` to its
  shader cache tag (`nvmtl_spvcache_escape_tag` in `plugin/nvmtl_spvcache_name.h`, called from `nvmtl_xlate_ready` in
  `plugin/NVMTLLibraryLoad.m`), so the two modes never share a cached module. If the tag with its mark does not fit, the
  shader cache is off for that process, as when the mode marks (`nvmtl_spvcache_mode_tag`) do not fit. With it at 0, the
  translator gives exactly the bytes of 3.0.4 on every `.ll` fixture of the tree, and refuses the same 30 translations.

- `NVMTL_WS_NOCOPY_ONCE=0` (the settings file; new, tier escape) copies WindowServer's read-only no-copy ranges before
  each submission and writes them back, as in 3.0.4.
- `NVMTL_GSUB_HOST_READS=0` (the settings file; new, tier escape) holds every part with a caller copy for the worker, as
  in 3.0.4.
- `NVMTL_SCHEDULED_AFTER_SUBMIT=0` (the settings file; new, tier escape) reports "scheduled" before the submission, as
  in 3.0.4.
- `NVMTL_SCHEDULE_EARLY=0` (the settings file; new, tier escape) keeps a held command buffer's "scheduled" until it has
  finished, as in 3.0.4.
- `NVMTL_SCHEDULE_SIMULATOR=0` (the settings file; new, tier escape) puts `SimMetalHost` and `SimRenderServer` back
  on mode 3, as in 3.0.4, and changes no other process. `NVMTL_SCHEDULE_AFTER_SURFACE_WRITES=3` does it too, but for
  every process, the Chromium family's included.

The protected-surface publication and the hold read under the lock have no switch: the first only changes a path that
always failed, the second corrects an unlocked read.

### What ran where

Each group was ported and tested on a tree of 3.0.4 (the branch at e3ea9d6b; called 3.0.4 below), before the
review fixes of 0db28f63 and the merge with `fable` at 2976003c, where `tools/test_switch_registry.py` still
reported the boot argument `nvgpucaps` without a row. The last part gives the runs on the branch with all of it.

#### 597

- 597, on Linux only; nothing was built on a Mac or taken through a sleep or a display sleep on a card.
  `tools/test_display_power_retry.py` is extended as 597 extends it, with the delay changed: a failed power-on queues
  the wake call 500 ms later and counts its own attempt. It also checks that a lit head and a wake before the GPU has
  resumed queue nothing, a failed system wake, and a framebuffer without a wake call. The new
  `tools/test_gpu_wake_retry.py` compiles `gpuWakeFire`, the `NVRMGpuPower` 0 branch and the lines of the 1 branch that
  queue the wake call, as they are, and checks the retry 500 ms later, the limit of three attempts (also after a failed
  `setPowerState(1)`, and when `NVRMGpuPower` 1 follows one), the count starting again (on success, on giving up, at a
  GPU standby), and that nothing waits under the display gate. Both tests pass on this branch and fail on 3.0.4
  (`--baseline e3ea9d6b`). They also fail on 597's own commit (b93d8ac0, `--baseline upstream/pr597`), which queues the
  call at once and sleeps under the gate. `build/kext_check.sh` (clang 18 with Apple's kext warning groups, MacKernelSDK
  headers, the NVIDIA 620.70.06 tree with this repository's patches): all 31 units compile as on 3.0.4, NVRMFB has 0
  warnings and imports the same 512 symbols, and every import is provided by macOS 15 and 26. Through
  `tools/run_all_tests.py`: the 59 `tools/test_*` that read `nvrm-fb.cpp` (`tools/test_display_power_retry.py` among
  them), `tools/test_gpu_wake_retry.py`, `tools/test_irq_timer_wiring.py` and `tools/test_rtd3_hold.cpp` (they cover
  NVRM's own `setPowerState` in `kexts/NVRM/NVRM.cpp`, which 597 does not change, the second through a copy of it), and
  the CI's switch and kext checks: 66 tests on this branch and 65 on 3.0.4. All pass on both except
  `tools/test_switch_registry.py` (the row of `nvgpucaps`, above). Not run: `tools/card_selftest.py` (it needs a card).
  `tools/check_nvrmfb_backlight_target.py` and `tools/check_nvrmfb_display_audio_mapping.py` read the file too, but
  `run_all_tests.py` does not run them, and they fail the same way on 3.0.4 and on this branch (out of date). 597's
  author tested their version, not this one, on an RTX 3080 with macOS 26.7.3 over 20 sleep and wake cycles.

#### 487

On Linux only (x86_64, clang 18); nothing of this has been built on a Mac or run on a card, and nothing of it has run
with Firefox. libnvencvt was not compiled whole: there is no macOS SDK and no open-gpu-doc tree here.

- Four new host tests, each run with this change and on the base (the staged 3.0.4 tree, and HEAD e3ea9d6b): all
  pass with it and fail without it. `tools/test_surface_vram_fd.py` (the plugin's import under a sandbox that refuses
  `/dev/null`, with the pipe failing, with NVK refusing the import, and no descriptor left open; run again with
  `NVMTL_SURFACE_VRAM_PIPE=0`, where the plain `open()` is back, and with `=1`; a copy whose switch did nothing failed
  it),
  `tools/test_nvencvt_rm_fd.py` (the library's connections the same way; nothing bound and the connection closed when
  there is no descriptor), `tools/test_nvencvt_dec_hold.py` (the hold in `plugin-container`, at `NVENCVT_DEC_HOLD=0` and
  in other processes, two threads emitting into one session, also under ThreadSanitizer; the log lines compiled with
  `-Wformat=2 -Werror`; `err_blocks`; the wiring) and `tools/test_import_cache_limit.py`. Each compiles the functions
  it checks from the source as they are.
- `vendor/nvenc/src/rm.c` was also compiled whole (`-fsyntax-only -Wall -Wextra -Wshadow`) against
  open-gpu-kernel-modules 620.70.06 and a stand-in for IOKit: no diagnostic, as on the base.
- The tests that read the changed files, or the registry (135 with the four new ones, through `tools/run_all_tests.py`):
  121 pass, 2 are skipped (`test_h264_kat_selftest.py` and `test_nvenc_parsers.py` need NVIDIA's headers), 2 are not
  run (no build line), and 10 fail as they fail on the base on this machine: six need macOS headers (`libproc.h`,
  `mach/mach.h`), `test_nvencvt_diag.py` runs as root, `test_build_sh.py` fails its arm "a changed NVRM patch re-runs
  the RM" as on `fable`, `test_evwait_wiring_kernel.py` fails "selector 7 answers both bits" in kernel code this group
  does not touch (the same on an export of the base), and `test_switch_registry.py` reports only the boot argument
  `nvgpucaps` without a row, which `fable` already has. `test_nvencvt_blackwell.py` caught a comment that named the
  registering function a second time; it was reworded.

#### 524, the translator

On Linux only (x86_64, rustc and cargo 1.97.0, gcc 13 and clang 18, `cargo --offline`). Nothing of this was built on
a Mac or run on a card. SPIR-V was checked with the validator of the `spirv-tools` crate (Vulkan 1.2), standing in
for `spirv-val`.

- `tools/test_translator_whole_views.py` (new) runs the 21 unit tests of 524, the 2 tests of the way back and the
  switch's own 2 tests with cargo: 25 pass on this branch. With `--baseline e3ea9d6b` it runs 524's tests on the
  translator of 3.0.4: 8 of 21 fail, so the test fails (`--baseline` alone means HEAD, which on this branch already
  has the change). The baseline is built in a cargo folder of its own
  (`<target>/whole-views-baseline`), so a plain run after it, or a plain `cargo test`, still runs this tree's tests:
  checked here in that order (plain, baseline, plain: 25 pass, 8 fail, 25 pass). It skips (exit 2) without cargo or
  without the crates offline.
- `tools/test_xlate_cache_tag.py` (new) compiles the tag statements of `nvmtl_xlate_ready` as they are and runs them:
  12 checks pass (`-wv0` for 0, 00 and a value that is not UTF-8 but starts with the byte 0, after `-td1`; no mark for
  1, empty, off or " 0"; untagged without a translator, and when the mark does not fit). On 3.0.4 5 of them fail. It
  also fails when the call is moved below the line that gives up without a translator.
- `translator/wrapper/tests/simulator_owned.rs` passes on this branch; on 3.0.4, and here with `NVMTL_WHOLE_VIEWS=0`,
  6 of its 10 fixtures fail. 524's T2 test (`workgroup_atomic_root.rs`, not added) passes 2 of 2 on 3.0.4 and here.
- `cargo test --no-fail-fast` in `translator/translator`: 2507 pass, 27 fail. 3.0.4 gives 2482 pass and the same 27
  failures, each at the same line: 24 corpus and reflection tests, most of which say they found almost nothing to
  read in `validation/fixtures/public` (5 fixtures in this checkout), and 3 `psb_value_select` tests whose module is
  SPIR-V 1.6, which the stand-in validator refuses under Vulkan 1.2. With `NVMTL_WHOLE_VIEWS=0`: 2499 pass, and the
  same 27 fail plus the 8 of 524's tests that hold the new rule. In `translator/wrapper`: 52 pass, 2 ignored, and 2
  fail as on 3.0.4 (`ca_named_modules_validate` and `compositor_pixel_sampler_spirv_validates`: they read shader
  files from the relative path `$HOME/nvmtl-tests/...`, which Rust does not expand, so they fail on any machine).
- Every `.ll` file of the translator (80), translated as a kernel and with the stage detected, by the CLI of 3.0.4
  and by this branch with the switch at 0 and unset: the switch at 0 gives the bytes of 3.0.4 on all 130 modules and
  the same 30 refusals. With the switch unset, only the Simulator fixtures change (above): 128 of 3.0.4's 130 modules
  are the same, 2 differ, 12 are new, and 18 translations are refused.
- `tools/test_app_profile.c` (four new checks of the `-wv0` mark, also with AddressSanitizer and UBSan): 60 pass. On
  3.0.4 it does not build, because `nvmtl_spvcache_escape_tag` does not exist there.
- `tools/switch_registry.py check` and `tools/run_all_tests.py test_switch_registry test_switch_report
  test_kext_check test_ci_workflow test_sysctl_allowlist`: 4 of 5 pass, and
  `test_switch_registry` reports only the row of `nvgpucaps` (above).
- The tools tests that read the changed files (17, through `tools/run_all_tests.py`): 15 pass.
  `tools/test_spvcache_dir.py` and `tools/test_aircache_dir.py` fail the same way on 3.0.4: they run as root here,
  and root can write to the folders they make read-only. `tools/test_fcwarm.py` needs a built translator library and
  was not run.

#### 524, the plugin

On Linux only (x86_64, clang; the Objective-C tests through libobjc2 and gnustep-base, not Apple's Foundation);
nothing of this has been built on a Mac or run on a card, and neither the 4K 240 Hz crash, a DRM video, nor the iOS
Simulator has been tried.

- Five new host tests, each run on this branch and on 3.0.4 (the staged tree): `tools/test_protected_surface_out.py`
  (P3), `tools/test_nocopy_once.py` (the copy once, with real read-only pages, a file mapping and the copy back's fault;
  the region query reads /proc/self/maps on Linux and calls `mach_vm_region` on a Mac; a writable range never asks for
  the extended answer), `tools/test_host_reads_ahead.py` (the read-only submit-ahead, the re-check after a later seal
  and the place of "scheduled", on the production `NVMTLResSet`, `-nvmtlSubmitAhead` and `-nvmtlAdvancePart`),
  `tools/test_schedule_hold.py` (the hold under the lock, with a second thread, and the early report) and new arms of
  `tools/test_schedule_policy.c` (the Simulator, and `NVMTL_SCHEDULE_SIMULATOR=0`). All pass on this branch, with each
  switch on and off, and fail on 3.0.4.
- The tests that read the changed code pass: `tools/test_submission_copies.py` (adapted to the new fields) and
  `tools/test_fence_wiring_plugin.py` (adapted to the hold released under the lock), `tools/test_readonly_copyback.py`,
  `tools/test_bytes_dup_seal.py`, `tools/test_surface_publication.py`, `tools/test_surface_alloc_bound.py`,
  `tools/test_surfin_zero.py`, `tools/test_surfin_zero_plugin.py`, `tools/test_surface_queue.py`,
  `tools/test_surf_planes_plugin.py`, `tools/test_host_buffer_release.py`, `tools/test_resset_lifetime.py`,
  `tools/test_resset_seal.py`, `tools/test_macos26_gaps.py` and `tools/test_buffer_texture_bounds.py` (with the scratch
  Metal stand-in). `tools/test_switch_registry.py` reports only the row of `nvgpucaps` (above).

#### The branch with all of it

- `tools/run_all_tests.py` on the branch with 3.0.4 and 3.0.5 and the merge with `fable` at 2976003c: 339 of 376
  passed, 23 failed, 4 skipped, 10 not run (they need a built library, a pristine NVIDIA tree or a build line). Each of
  the 23 also failed in the 3.0.4 run (above), with the same checks failing, for the reasons given there: they fail on
  `fable` too, or need what this machine lacks (the tag v2.8.1, a normal user instead of root, a Mac or its tools such
  as `-arch`, `plutil` and `flex`, Metal's texture descriptor). One check more failed in `tools/test_build_sh.py` ("a
  changed NVRM patch re-runs the RM and everything built from it"); run alone, the test fails the same checks on this
  branch and on `fable` at ac9bb6e4, because this machine has no `flex`. A first run
  ran out of disk on this machine in two installer tests (`tools/test_lastgood_install_twice.py` and
  `tools/test_lastgood_refusal.py`); with the disk freed, both pass.
- `tools/switch_registry.py check`: 0 problems, and `tools/test_switch_registry.py` passes.

## 3.0.4

**Not released.** This section describes the branch being prepared as 3.0.4, a patch on top of `fable` at ac9bb6e4
(3.0.3, not released either), since merged with `fable` at 2976003c (its documentation refresh). It brings into this tree seven pull requests to the original project by luckiest-luk
(https://github.com/nullmoth/nvidia-macos-driver, pull requests 573 and 575 to 580), which were closed there on
2026-11-18 without being merged. Six are taken with their author, 575 with one correction of ours; of 576 only the
half that gives back the caller's pages is taken, applied by hand (below). Nothing of 3.0.4 has been built on a Mac or
run on a card: it was compiled and its host tests were run on Linux only (below, "What ran where").

### Fixes and changes

- **OpenGL and WebGL uniforms go through a constant buffer, as the code meant them to (573).** `nvmtl_gl_uniform_index`
  (`plugin/NVMTLLibraryLoad.m`) also required `names[@"access"][key]` to be `read`, but the AIR name table
  (`nvmtl_air_names`) has no `access` key, so no GL function ever had a promotable uniform buffer and the promotion
  of the uniform buffer to an NVK constant bank (`nvmtl_uniform_cbuf_on`) never ran, here or in the original project.
  The check that the buffer is read only stays, on the reflection (address space 2, access ReadOnly). This turns on,
  by default, a path that had not run in this tree; `NVMTL_UNIFORM_CBUF=0` turns it off.
- **ANGLE's uniforms (Chrome, Safari's WebGL and other browsers) are promoted too (579).** The plugin accepts the
  buffer ANGLE names `ANGLE_userUniforms`, and `translator/wrapper/src/uniform_cbuf.rs` promotes scalar and vector
  32-bit members, fixed arrays and nested structs when it can prove every access is a read at a constant, in-bounds
  index; anything else (a write, a volatile or escaping pointer, a dynamic index, a matrix, an unknown layout) keeps the
  path it had.
- **A draw that changes only the promoted uniforms' offset keeps its address table (580),** and **an address table is
  built in ordinary memory and copied to its mapped page in one go, with the empty slot's address asked once (577).**
- **A texture over a buffer is bounds-checked without overflow (575).** `nvmtl_buffer_texture_fits`
  (`plugin/nvmtl_buffer_texture.h`) checks with subtractions and divisions instead of sums and products that could
  wrap, and does not require padding after the last row. It is used by the buffer-aliased texture of `nvmtl_vk.c` and
  by the copy fallback of `newTiledTextureWithDescriptor`, which also refuses what it cannot copy (not 2D, depth,
  layers or samples). Two parts of 575 are not taken as written: it refused a mipmapped texture and any format the
  public table does not describe as uncompressed. Core Animation asks for mipmapped textures over buffers (the Dock's
  badges, Spotlight's half-float icons), and `nvmtl_mip_autofill` fills their other levels from level 0 (d39bc6e3), so
  level 0 is still copied; a compressed or unlisted format keeps the check it had in 3.0.3, `bytesPerRow` bytes for
  each row, now written so it cannot wrap. `tools/test_buffer_texture_bounds.py` holds both cases.
- **A buffer the GPU only read in a blit is not copied back to the caller's memory (578, on;
  `NVMTL_BLIT_READONLY=0` turns it off).** The command buffer's
  resource set marks the source of `copyFromBuffer:toBuffer:` and `copyFromBuffer:toTexture:` read only, any other use
  makes it writable again, and the copy back at completion skips a read-only buffer with a caller copy (not a managed
  shadow, not under a texture alias). The set taken when a command buffer is sealed is returned retained, so it does
  not wait in the application's autorelease pool.
- **The caller's pages of an imported `newBufferWithBytesNoCopy` buffer are given back after their GPU memory is freed,
  and with their address (576, half).** Such a buffer has no `_hostPtr`, and `-dealloc` retired it before calling the
  caller's deallocator, so the deallocator got NULL and 0, and got them at once, while the buffer grace could still hold
  the import for a submit in flight. The pointer and length are now taken before retiring, and for an import the
  deallocator runs in `nvmtl_vk_buffer_destroy_body` after `vkFreeMemory`, outside every lock. A lost device (nothing
  destroys the buffer) runs it in `-dealloc`, as before; a grace record that cannot be allocated keeps the import and
  its pages. If the device is lost after the grace took the import, neither the import nor the pages are given back and
  the deallocator does not run, like every GPU object after a loss, since the GPU may still use them. A deallocator that
  frees another buffer while the grace's reaper runs it leaves each death noted once in the fault report (the reaper's
  flag is put back around it, and around the immediate destroy, instead of cleared). Not taken from 576: its changes to
  how submits and the prologue are counted, which this tree had already made its own way (`nvmtl_cmd_life.h`).

### The ways back

- `NVMTL_UNIFORM_CBUF=0` (environment, or the settings file; its row is now tier escape, since it turns off what is
  on by default) keeps GL and ANGLE uniforms in the address table, as in 3.0.3 (573, 579, 580).
- `NVMTL_BLIT_READONLY=0` (the settings file; new, tier escape, row in `tools/switches.tsv`) records a blit's source
  buffer as written, so a caller copy of it is copied back at completion, as in 3.0.3 (578).
- `NVMTL_BUFFER_GRACE=0` (environment; not new) turns off the buffer grace, and with it an import's deallocator runs at
  once in `-dealloc`, now with the caller's pointer and length (576's half).

The other changes have no switch: they correct what was there (a bound that could wrap, a deallocator called with
NULL, 0 and too early, an address table written word by word into mapped memory).

### What ran where

On Linux only (x86_64, clang 18; the Objective-C tests through libobjc2 and gnustep-base, not Apple's Foundation);
nothing of 3.0.4 has been built on a Mac or run on a card.

- Five new host tests, each run on this branch and on `fable` at ac9bb6e4: `tools/test_gl_uniform_index.py` (573),
  `tools/test_uniform_snapshot.py` (580), `tools/test_address_snapshot.py` (577), `tools/test_readonly_copyback.py`
  (578; also run sanitized) and `tools/test_host_buffer_release.py` (576, written here: imported, managed and copied
  buffers, a submit in flight, the grace off, a lost device, a failed grace allocation, no grace lock held in the
  deallocator, a deallocator that frees another buffer during a reap; release and sanitized). All pass on this branch
  and fail on ac9bb6e4. `tools/test_readonly_copyback_gpu.m` (578) needs Metal and a card and was not run.
- `tools/test_buffer_texture_bounds.py` (575) needs Metal's texture descriptor, which the Linux Foundation stand-in
  lacks; it was run with a scratch stand-in that adds `MTLPixelFormat`, `MTLTextureDescriptor` and `replaceRegion`
  (not committed): it passes on this branch and fails on ac9bb6e4, and 575 as written fails its mipmapped case.
- The tests that read the changed code: `tools/test_submission_copies.py`, `tools/test_bytes_dup_seal.py` (it now
  accepts 578's restart loop), `tools/test_ci_workflow.py` (`ci.yml` skips the two new tests that need Vulkan headers,
  as it skips the other five), `tools/test_surfin_zero_plugin.py` and `tools/test_switch_registry.py`: all pass
  (the last since the merge with 2976003c, which gave the boot argument `nvgpucaps` its row);
  `tools/switch_registry.py check`: 0 problems.
- The translator: `cargo test --release` in `translator/wrapper`: 52 pass, including the 14 of `uniform_cbuf` (579's
  new ones among them), 2 ignored, and 2 fail as on ac9bb6e4, `ca_named_modules_validate` and
  `compositor_pixel_sampler_spirv_validates`, which read shader files from the relative path `$HOME/nvmtl-tests/...`
  (Rust does not expand it, so they fail on any machine).
- `tools/run_all_tests.py`: 323 of 365 passed, 28 failed, 4 skipped, 10 not run (they need a built library). Of the
  28, two were this branch's, `tools/test_bytes_dup_seal.py` and `tools/test_ci_workflow.py`, corrected above, and
  `tools/test_buffer_texture_bounds.py` needs Metal's descriptor (above). Ten others, the ones near the changed code,
  were run on ac9bb6e4 and fail there the same way. The last fifteen fail for this machine: the tag v2.8.1 is missing
  from this checkout, the tests run as root, and some need a Mac or its tools.
- Three reviews of the diff, for the uniforms (573, 579, 580, 577), for the copies (575, 578) and for 576's half,
  each finding checked by a second reader. They found the mipmapped textures 575 refused, that 578 had no way back,
  and the reaper's flag a deallocator could clear; all three are corrected above. A separate switch for 576's half was
  not added: `NVMTL_BUFFER_GRACE=0` already runs the deallocator at once in `-dealloc`, and giving the pages back
  there while the grace still holds the import would hand the caller pages a submit may still read.
  `tools/test_surfin_zero_plugin.py` reads the reaper's flag and follows the change.

## 3.0.3

**Not released.** This section describes the branch being prepared as 3.0.3, a patch on top of `fable` at ae84aec7
(3.0.2), now merged with GPU Caps Phase 1/2B on `fable` (PRs #6 and #7). The Zink correction itself was
compiled and host-tested on Linux only (below, "What ran where"); GPU Caps has separate physical evidence
from an RTX 5050. No native build or card result for the combined 3.0.3 tree is recorded yet.
See [current status](docs/STATUS.md).

### Fixes and changes

- **The layout guard accepts a CGL context whose `rend` does not yet hold the GLI engine; on macOS 26.7.3 this is
  expected to let OpenGL go through Zink. Not run on a Mac.** In card-test round 11 on an RTX 5050 with macOS 26.7.3
  (25G342) 39 Zink rows were inactive: 38 with "NVK Zink: OpenGL stays Apple's; GLEngine's private layout does not
  match the record (the CGL context's renderer is not this GLI engine)", and the glbench row on its own test of
  GL_RENDERER, with its standard error not read. The layout guard (`zink/nvk_cgl_guard.h`, 09fecac8) required the CGL
  context's first member, `rend`, to be the GLI engine (GLEngine's per-context object, the pointer gldCreateContext
  hands over) while gldCreateContext runs. No record shows that it is. The change rests on an assumption that no
  record confirms either: that CGL stores the new GLI engine in `rend` only after GLEngine's context creation, which
  calls gldCreateContext, has returned. The source fork's adapter, which ran on 26.7.1 (25G241), never reads `rend`
  there and compares it with the engine only on a current context, as this adapter does. In those 38 rows the checks
  before it had held for the context the line was printed for, including the one that ties the recorded pointer to the
  public layout (the derived CGL context starts a heap block of at least 0x1e90 bytes). The guard accepts a `rend`
  that is null, the engine, no aligned user address, or a value that starts no heap block, points into neither the
  engine nor the context and lies in heap memory or in no mapped region, and refuses another GLI engine of the same
  CGL context, another heap block, a pointer into the engine or the context, or a pointer into an image, a stack or
  other memory malloc did not map. The saved dispatch table, whose check had never run on a Mac, is accepted as its
  own heap block, as a pointer-aligned span of the CGL context's block past the public struct, or as a span of
  readable, writable heap memory at no block start (the CGL private object's engine records are 0x1e90 bytes apart,
  room for a 0x18-byte head and a `GLIFunctionDispatch`); where it lies is asked of the task's VM map
  (`mach_vm_region_recurse`, which reads no memory). It is refused as a block smaller than `GLIFunctionDispatch`,
  inside the GLI engine, another GLI engine of the same CGL context, the CGL context's public part, past the context's
  block or its heap region, reaching the start of the GLI engine or of the CGL context before it ends, in an image, a
  stack, read-only or unmapped memory, or no aligned user address. A CGL context that is the GLI engine itself is
  refused too. A saved table placed where none of these hold keeps the context Apple's even when `rend` is as
  expected; the line then says where it is. What prepare writes, and when, is what the source fork writes.
- **What this cannot rule out.** If the assumption is wrong in a way the guard cannot see, so that `rend` never
  becomes the adopted engine, the context does not go back to Apple's renderer: every GL call of that context is
  dropped and returns 0 or nothing, `glGetString` included. The context draws nothing, and an application that checks
  what `glGetString` gives may report that OpenGL is unavailable or stop at start. The adapter then prints, once per
  process, for the first such call, "NVK Zink: a call through a Zink context was dropped; it did not come with the GLI
  engine Zink adopted for the current CGL context (...)", with what the call came with, what `rend` holds and whether
  the two are one value; `nvk_cgl_last_error` gives "The current CGL context's renderer is not the GLI engine its
  bridge was prepared for" in place of "No attached Zink context current on this thread". The line says that one call
  was dropped: calls that come with the adopted engine, and the process's other contexts, are served. The source fork
  drops such a call the same way, with no line. The verdict on `rend` is per context, so where CGL does not clear a
  context's block one context of a process can stay Apple's while another goes to Zink. A saved-table word that starts
  a foreign heap block of at least 0x1e78 bytes, or points into a foreign heap object in writable heap memory, is
  written over, as it was in 3.0.2 for a block. `docs/ZINK-OPENGL.md`, "What the checks do not prove".
- **One GLI engine per CGL context.** An engine that passes the checks is adopted, or left unadopted where the Zink
  screen is not ready (it could not be made, or it was marked uncertain after GPU work whose completion could not be
  confirmed) or memory ran out, and `nvk_cgl_prepare_gld` remembers both outcomes for the CGL context (the second for
  one context, the last). A later engine of such a context (another renderer of its pixel format) is
  left unadapted and prints no line, whatever its own check says: the line would say that OpenGL stays Apple's for a
  layout that differs, which is not what decided that context. In a context Zink serves, `nvk_cgl_last_error` gives
  "The CGL context already has a GLI engine served by Zink", and a call that reaches the context's adapters with the
  unadapted engine is not handed to Apple's renderer; it is dropped, with the dropped-call line. Three limits. A later
  engine whose check stops before the context's names no context and is refused with the line. A refusal is not
  remembered: a later engine of a context whose earlier engine was refused is checked like a first engine and adopted
  when its own check passes, and Zink then serves a context the line said stays Apple's. And after another context is
  left unadopted in between, a later engine of the earlier one is checked like a first engine too: refused with the
  line when its check refuses, adopted when its check passes and its bridge can be allocated (only where the earlier
  engine was left for want of memory; a Zink screen that is not ready does not become ready later). The source fork
  adopts each engine, and the destroy of one then writes its own engine pointer back into a context the other still
  uses.
- **CGLTexImageIOSurface2D's hook reads the CGL private object where the allocator vouches for it, and says when it is
  not placed.** `install_integer_dispatch` runs when Zink first serves a context, in every process, and
  `integer_dispatch_slot`, which finds the callback vector for it, had two checks that had never run on a Mac either.
  It required the CGL private object and its engine list to be heap blocks of their own. It reads the private object
  as a heap block of its own or as a pointer-aligned span of the CGL context's block past the public struct, and the
  engine list as a heap block of its own or as a pointer-aligned span of the private object's block past the engine
  count and list pointer, each holding what is read (`nvk_cgl_room`). The callback vector is cloned, as before, only
  when it is a heap block of 16 to 512 pointers whose entries 4, 6, 9 and 15 `dladdr` names gliCreateContext,
  gliDestroyContext, gliSetInteger and gliGetVersion. A refusal of the private object or the list printed "NVK Zink:
  OpenGL stays Apple's; ...", which was not what happened: the context stays Zink's and only CGLTexImageIOSurface2D
  keeps Apple's handler. The other cases (a null list pointer, an engine count outside 1 to 16, no record that names
  the engine, a vector that is null, starts no heap block or is a block of another size, entries `dladdr` does not
  give those names) printed nothing. Every one of them prints "NVK Zink: CGLTexImageIOSurface2D in a Zink context
  stays Apple's; GLEngine's private layout does not match the record (...)", once per process, under a flag of its
  own, with the case in parentheses; only a failed allocation of the clone prints nothing. On a Mac where the hook is
  not placed, the line appears in every OpenGL process Zink serves, whether or not the application calls
  CGLTexImageIOSurface2D, and every Zink row of the card checks fails its "messages" check.
- **A refusal says what the memory held.** The line, "NVK Zink: OpenGL stays Apple's; GLEngine's private layout does
  not match the record (...)", keeps its prefix (so a card row stays "inactive") and names in the parentheses, for
  each pointer checked, null, this GLI engine, another GLI engine of this CGL context, a heap block of a given size, a
  pointer into a named block at an offset, or the value itself with the VM tag, protection and remaining bytes of the
  region that holds it, with the sizes of the GLI engine's and the CGL context's blocks. The line and `sw_vers` say
  which check refused and what each word held. It is printed for the first context refused in a process only: later
  contexts of that process can be refused for another reason, or go to Zink, without a line.
- **The card checks name a context whose calls return nothing.** A Zink row whose `glGetString(GL_RENDERER)` gives 0
  fails with "GL_RENDERER is NULL; nvk_cgl_last_error: ..." and the driver's first line where it wrote one
  (`zk_not_zink` in `tools/card/zink_common.h`), whatever else the process printed: with a context current Apple's
  renderer gives a string, so a null one is the adapter's. `zk_string` hands 0 on as the text "(null)", which
  `zk_not_zink` did not take for 0, so that row failed with "the context is Apple's (GL_RENDERER (null))", or went
  inactive where the process had also printed the refusal line for another context. The last error is the one of the
  thread that read the renderer (`zink_win_check.m`'s render-thread rows read it there). The glbench row
  (`tools/card/checks_zink.py`) keeps glbench's own exit status on "GL_RENDERER (null)" and fails; it was inactive,
  and the runner does not read the standard error of an inactive row.

### The ways back

None new. Boot argument `nvzink=0` keeps Apple's OpenGL for every application, as before.

### What ran where

Zink correction: on Linux only (x86_64, gcc and clang); no native build or physical result for this correction yet.

- `tools/test_cgl_gld_guard.py` compiles `zink/nvk_cgl_guard.h` for the host with `-Wall -Wextra -Werror` under gcc 13
  and clang 18 and feeds it synthetic heap blocks and VM regions: 266 cases (placements of the CGL context, `rend` and
  the saved table that the guard accepts and that it refuses, at their boundaries; a span of memory under each VM tag
  from 0 to 31 and each protection; the outputs on acceptance and refusal; each refusal's text, with the sizes it
  prints; the text of the dropped-call line; which engines are later engines of a context adopted or left unadopted;
  and `nvk_cgl_room`), all ok, and its 25 other checks, all ok. Sixteen of those read `zink/nvk_cgl.c`, which the host
  cannot compile: prepare reads through no recorded offset before the check and returns from every refusal before the
  Zink screen and any write; from its first statement to the allocation of the bridge it holds the statements the test
  lists and no other, so no other return and no other write to the refusal's reason; a later engine of a context
  adopted or left unadopted returns before the refusal line; an engine that passed the check and was not adopted is
  remembered with its context and forgotten when its GLD context is destroyed; the three lines are printed in one
  place, each once per process, and the first starts with the text the card checks read as inactive; no macro in
  `nvk_cgl.c` or in the headers of `zink/` it includes replaces that text or the calls the lines are printed with;
  `bridge_current`, statement by statement up to the branch that needs a Zink context, drops a call that comes with
  another engine, and nothing else sets its flag; the VM question goes to `mach_vm_region_recurse`;
  `integer_dispatch_slot` and `install_integer_dispatch` are compared whole with the statements the test lists: the
  first asks `nvk_cgl_room` before each read, writes each reason whole into the caller's buffer and prints nothing,
  and the second writes a reason and prints its line for every way the hook is left out except a failed allocation of
  the clone. Four say that this file, `docs/ZINK-OPENGL.md` and both TESTING-ON-HARDWARE files quote the three lines
  as the code builds them, with "..." for the reason. To see that the test notices, 213 single changes were made to
  copies of the guard header (126) and of `nvk_cgl.c` (87), one at a time: a condition dropped, a bound moved by one,
  two values swapped, a return removed or added, a reason blanked after it is written, a flag set outside its line, a
  macro that replaces a name. The test failed for 209. The other four cannot change what the guard does: one differs
  only for a heap block of exactly 0xc6a6 bytes, a size malloc does not report; one drops a test the VM answer already
  implies; two test the alignment of an offset into a heap block in place of the pointer's, and a block starts at an
  aligned address.
- `zink/nvk_cgl.c` and `zink/appkit.m` checked with clang 18 `-fsyntax-only` against the public macOS 11.3 SDK's own
  headers (`-isysroot`, target `x86_64-apple-macos15.0`, with the flags of `build/build_zink.sh`: `-O2 -Wall -Wextra
  -Werror -Werror=unguarded-availability-new -Wno-deprecated-declarations`, and `cgl_api.inc` and `cgl_dispatch.inc`
  made from that SDK's OpenGL headers by `build/generate_cgl_dispatch.py`): no diagnostic. That check found one error
  while this change was written: the 11.3 SDK names VM tag 13 `VM_MEMORY_MALLOC_PGUARD`, so the tag is compared
  under whichever of the two names the code knows the SDK defines. The same check of ae84aec7's `nvk_cgl.c` passes,
  and a call to `mach_vm_region_recurse` missing an argument stops it. `zink/native_drawable.m`, which this change
  does not touch, stops there at `kIOMainPortDefault`, a name the 11.3 SDK does not have. Not compiled with Xcode
  against a current SDK, not linked.
- `tools/test_cgl_dispatch.py`, `tools/test_zink_context_refs.py`, `tools/test_nvk_local_map.py`,
  `tools/test_tree_layout.py`, `tools/test_switch_registry.py` and `tools/test_card_selftest.py` (the host tests that
  read `zink/nvk_cgl.c`, `docs/ZINK-OPENGL.md` or `CHANGELOG.md`, or the card checks of the Zink rows): all pass;
  `tools/switch_registry.py check`: 0 problems.
- `tools/test_build_sh.py` (build.sh against stand-in tools, under bash 5.2, not macOS's bash 3.2): 393 of 393 checks
  passed; the one that differs from 3.0.2 says that a package records 3.0.3, CHANGELOG.md's first heading.
- The card checks. The line numbers of `zink/nvk_cgl.c` that their comments cite (`tools/card/zink_common.h`,
  `zink_gl_check.m`, `zink_win_check.m`) follow the moved lines (two that 3.0.2 had a few lines off are corrected),
  and the comments of the profile-byte row and of the IOSurface row quote the new texts. The two programs were checked
  with clang 18 `-fsyntax-only` against the public macOS 11.3 SDK, with the flags of their rows and Mesa's Vulkan
  headers: no error, and no warning but three that say `NSOpenGLContext`'s `view` is deprecated, in lines this change
  does not touch (what this change adds to `zink_win_check.m` keeps the render thread's last error for `zk_not_zink`).
  The glbench row's shell fragment was run under sh and bash with a first line that names Zink, "(null)", another
  renderer and none. Not compiled with Xcode, not linked, not run.

## GPU Caps experimental integration (2026-11-17 to 2026-11-19)

- Phase 1: versioned read-only selector 14 and per-GPU capability cache.
- Phase 2B: additional RM observations, refreshed on lifecycle changes; validated on one RTX 5050. The reported verified count increased from 9 to 15; manual authorization remained false. No fan-write policy was changed.
- Both phases are merged into `fable`. See [GPU Caps integration and evidence](docs/GPU-CAPS.md).
  This is not a certification of the combined 3.0.3 source tree.

## 3.0.2

**Not released.** This section describes the branch being prepared as 3.0.2, a patch on top of `fable` at b1b09c1b. That
commit holds the 3.0.1 changes below, merged and not released either (their last commit is 44091839), and the card
checks (`tools/card_selftest.py`). A package built from 44091839 has been installed on one Mac with an RTX 5050 (macOS
26.7.3); the fault below was found on its first boot there. Nothing committed after b1b09c1b has run on a card or on a
Mac: it was compiled and its host tests were run on Linux only (below, "What ran where").

### Fixes and changes

- **WindowServer's main thread no longer waits for a shader variant the warm-up has not started.** On the first boot
  after that package was installed, the userspace watchdog killed WindowServer 130 s after it started (bug_type 409, its
  main thread 40 s without a check-in); the WindowServer started next came up normally. The package was the first whose
  Metal plugin used this build's translator (3.0.1, "Metal apps get this build's shader translator"), so every cached
  function-constant variant was translated again. In all 12 samples of the watchdog's spindump the main thread was in
  `newFunctionWithName:constantValues:error:`, waiting, for up to 60 s, for the function-constant warm-up of 2.9 to
  translate one variant, while four of the warm-up's threads took turns on its two slots; no translation takes 40 s, so
  what it waited for was queued behind others. (The warm-up exists because that main thread, made to translate dozens of
  Core Animation's variants one after another, 1.3 to 2 s each, was killed by the same watchdog.) Now a variant whose
  cache file is on disk is read at once, whatever the warm-up is doing. A variant the warm-up has not started is taken
  out of its queue by the thread that asks for it, which translates it as any miss (the same cache file, the same
  records and logs), and the warm-up never translates it; two threads that ask for it at once both translate it, as they
  would without the warm-up. A variant the warm-up is translating is waited for, for at most 5 s
  (`NVMTL_FCWARM_JOIN_MS`, a constant in `plugin/NVMTLLibraryLoad.m`; it was 60 s, and the longest translation measured
  is 3.2 s), and then translated by the caller, as before. Two workers take the queued variants one after another,
  oldest queued first, so the warm-up runs at most two translations at a time on at most two threads; it used to give
  its queue one block per recorded variant, up to 1024, each of which first waited for one of the two slots on a thread
  of the queue (four of them in the spindump). A development build's log, or in a release build the `nvmtl.log` of a
  process started with `NVMTL_PROCESS_ENV=1`, says "TOOK OVER" for each variant taken out of the queue and "JOINED" for
  each wait; a release WindowServer writes neither line to the system log. `NVMTL_FCWARM_OFF=1` still turns the warm-up
  off, and its records (`.fcwarm.v1`) and the cache files are as in 3.0.1. Not measured on a card.

  This bounds each call, not a run of them. One call now waits at most 5 s before it translates its variant itself,
  where it could wait 60 s and more. When Core Animation asks for dozens of cold variants in one turn of the main
  thread, as it did here, their times still add up: the main thread spends about a third to a half of their translation
  times added up while both workers are free (a half when it asks for them in the order the workers take them, which is
  what 3.0.1 cost in any order), and all of it while both workers are busy with another function's variants. The workers
  run at utility priority, so on a busy Mac a wait can last longer than the caller's own translation would have, up to
  the 5 s. For 67 variants (as many as WindowServer held when the warm-up was written) at 1.3 to 2 s each, that is about
  29 to 67 s while the workers are free. So the first boot after a translator change can still pass the watchdog's 40 s
  and lose WindowServer once, as it did here; the WindowServer started next finds a warmer cache. A translator change is
  any package whose `libnvmtl_translate.dylib` differs by one byte from the installed one, as the cache is named after
  its SHA-256; the translator sources of 3.0.2 are those of 3.0.1, but whether a rebuild gives the same bytes has not
  been checked.

### The ways back

None new. `NVMTL_FCWARM_OFF=1` in `nvrm610.conf` turns the whole warm-up off, as before; then every variant missing from
the cache is translated by the thread that asks for it, as before 2.9.

### What ran where

On Linux only (x86_64, clang 18 with GNUstep's Objective-C runtime and Foundation, and swift-corelibs-libdispatch);
nothing of 3.0.2 has been built on a Mac or run on a card.

- `tools/test_fcwarm.py` compiles the warm-up and the cache code out of `plugin/NVMTLLibraryLoad.m` against a
  `libnvmtl_translate` built on Linux from the translator sources of b1b09c1b. At b1b09c1b its harness did not compile
  (the cache code it cuts out of the plugin had moved); it builds again. Core Animation's fixed_frag shader is not in
  the tree, so the AIR fixture `translator/translator/tests/fixtures/gl/texture_env_three_sites_behind_a_switch.ll`
  (three Boolean function constants) stood in for it, with `--pad-ms 300` so that each of its misses is recorded: 23 of
  23 checks passed in six runs, one of them with every processor busy. Against the warm-up of b1b09c1b (its dispatch
  groups boxed in `NSValue`, as Linux's libdispatch objects are not Objective-C objects) 6 of the 23 fail, in each of
  two runs: the bound (60 s), the three checks of the queued variant (the caller waited 5.7 s for the queue, the
  background translated it, nothing was taken over), the slow worker (waited for 13 s) and the threads (4). The check of
  a file on disk while a worker translates its variant passes there, and fails when the warm-up is asked before the file
  is read (the caller then waited 3.7 s for the worker).
- The same test's harness built with ThreadSanitizer, in six scenes with one asking thread (a queued variant taken over,
  a running one joined, one that outlasts the bound, a file on disk while its variant is translated, five asked for in a
  row, two one after the other), and a stress harness made from it, with `gFcWarmLock` marked as a mutex, in 16 boots of
  four threads asking for random variants at once. No report is about the warm-up's queue, jobs, lock or counters, and
  none is a lock-order inversion of the plugin's locks. The reports are in GNUstep's runtime and Foundation; in
  hand-offs through libdispatch and Objective-C reference counts, which ThreadSanitizer does not see there (blocks
  copied for `dispatch_async`, objects freed by the last of two threads, `dispatch_once`); and in two switches the
  plugin reads once from the environment into a plain `static int`, `nvmtl_fcwarm_on` (in all 16 boots) and
  `nvmtl_fc_spec_on`, which two threads' first lookups can set at the same time, both to the same value. Both have been
  so since 2.9, like most of the plugin's switches. In the scenes with one asking thread the warm-up of b1b09c1b gives
  the same kinds of reports, and more hand-offs.
- The stress harness without ThreadSanitizer, in 30 boots of four asking threads at the 5 s bound and 30 at a bound of
  150 ms, so that waits run out and a caller and a worker write the same file at once: every boot ended with no worker,
  job or queued variant left; the background translated no recorded variant twice and never the one not recorded; every
  cache file had a live miss's name and bytes, and every call the same bytes; no call outlasted the bound by more than
  3.1 s (two padded translations and a margin); and no log line, dispatch, wait, cache write or translation was made
  with an `os_unfair_lock` held.
- The plugin's Objective-C compiled with `-fsyntax-only` against a stand-in macOS SDK, at the SDK level of macOS 15.5
  and at that of Xcode 26.5: no new diagnostic. Its C and the C and C++ host tests, compiled for x86_64 macOS: the same
  diagnostics as at b1b09c1b (two C++ host tests do not link here at either). The host tests that read
  `NVMTLLibraryLoad.m` or `CHANGELOG.md` pass as they do at b1b09c1b, `tools/test_card_selftest.py` among them;
  `test_aircache_dir.py` and `test_spvcache_dir.py` fail at both, the same checks, because they need a folder the test's
  user cannot write and they ran as root.
- `tools/test_build_sh.py` (build.sh against stand-in tools, under bash 5.2, not macOS's bash 3.2): 393 of 393 checks
  passed, as at b1b09c1b; the one that differs says that a package records 3.0.2, CHANGELOG.md's first heading.

## 3.0.1

**Not released.** This section describes the branch being prepared as 3.0.1, on top of the 3.0 branch below (`fable` at
9f85871b). A package built from b80056ef, before the fix in "Metal apps get this build's shader translator" below, has
been installed on one Mac with an RTX 5050 (macOS 26.7.3), and card checks were run on it there; the fault that entry
fixes was found from them. Nothing committed after b80056ef has run on a card or on a Mac: it was compiled and its host
tests were run on Linux only.

### Fixes and changes

- **Idle mode switches while apps run.** NVK's boost lease took its shape from `NVRM_610_BOOST` once, when a process
  opened the device, so NVMetrics' idle mode (`NVRM_610_BOOST=0` in `nvrm610.conf`) reached only the apps started after
  it, and switching it off left the open ones without boost until they restarted. NVRM.kext now holds a boost policy of
  three values: undecided (every boot starts there, and each process follows `NVRM_610_BOOST` as in 3.0), idle mode and
  normal. A root process sets it through a new user-client selector (13); there is no writable sysctl, and
  `debug.nvrm_boost_policy` shows it read-only, with the number of changes in `debug.nvrm_boost_policy_sets`. Each
  Vulkan process reads it at most once a second, just before it would renew its lease (`nvk/nvk-boost-policy.patch`):
  in idle mode it asks for nothing and clears what it still has, in normal it boosts per burst again (or holds, with
  `NVRM_610_BOOST=hold`). A process that holds and submits nothing is not reached: it keeps its hold until it submits
  again or exits. A hold is cleared as it was asked (with the CUDA bit) and then as 3.0 cleared it, since which of the
  two the firmware honours is not in the open sources, and one renewal runs at a time in a process, also when an RM
  call waits through a sleep. The selector takes no gate and makes no RM call, so it answers during a sleep too; a 3.0
  kext does not have it, and NVK then keeps `NVRM_610_BOOST` as before. Boosts of other RM clients, such as NVMetrics'
  own Automático/Máximo or the optional `nvmtl-boost` daemon, are theirs and are not touched. NVMetrics switches both
  the file (for after a reboot) and the policy in one password prompt (`nvmetrics-agent policy idle|normal`).
- **`NVRM_610_BOOST` in `nvrm610.conf` reaches every Vulkan app.** NVK's own reader matched only 2.9's short spelling
  `BOOST=` in the file, and only when its first appearance started a line, so a Vulkan app without the Metal plugin
  (the plugin copies the file into the environment) ignored the `NVRM_610_BOOST=0` line NVMetrics writes. The full
  name is now read first (the last such line wins), then the short one, without printing anything.
- **A refused boost is asked again after 30 s.** 3.0 stopped asking for the rest of the process after one refusal, so a
  refusal while the GPU came back from sleep left that app without boost until it was restarted.
- **New blocks of video memory are cleared by the copy engine.** 3.0 stopped filling a granted block with zeros while
  the RM's own clearing is believed, but only until the GPU's first standby (a system sleep): from then on, and for good
  once a block read as not zero, every new block was filled from the processor through BAR1 again, at about 330 MB/s, 27
  ms for the 9 MB of each captured frame of a screen share, with the display gate held when the block was made on the
  caller's thread (the worker that makes blocks ahead filled them after letting go of the gate). Such a block is now
  cleared by the GPU's copy engine: NVRMFB asks NVRM (`nvVidmemClear`), and NVRM runs the RM's own memset on the block
  (`kexts/NVRM/nvrm_pageoff.c`, the way the page-off copy already runs) with the API and GPU locks held for the clear
  only. NVRM never waits for a standby there: with the GPU not running it answers at once and the block is filled from
  the processor as before, as it is where the RM made no copy-engine utility of its own (the clear makes none; the
  page-off still makes one when it first runs, as in 3.0, and the clear then uses it). The engine's end is waited for a
  quarter of a second at most, by the clock. A reading of the engine's progress that cannot be right (more finished than
  was ever sent, which is what a failed read of its semaphore gives) is not believed, here and in the page-off's own
  waits, where it would have ended every later wait of NVRM on the engine at once until the RM made its copy-engine
  utility again at the next sleep. The same 65 probes that check the RM's clearing then check the engine's; a block that
  does not read as zero is filled from the processor, and so is every block for the rest of the boot. Work whose end was
  not seen leaves its block allocated until restart, never handed out or freed, so that late zeros cannot land in
  someone else's memory. `debug.nvrmfb_vram_ce_clear` says whether the clear is on (1), off by boot argument (0) or
  stood down (2); `debug.nvrmfb_vram_ce_cleared`, `_ce_us`, `_ce_fallbacks` and `_ce_kept` count it, and the old
  `_filled` / `_fill_us` count what was still filled from the processor. `kexts/NVRMFB/fb/nvrm-vram-clear.h` holds the
  rules, `tools/test_vram_clear.c` every answer against them. The cause of the 330 MB/s itself is not established: the
  RM asks for a write-combined mapping, and by the macOS 26 sources NVRM's way of making it gets one.
- **Asynchronous texture uploads are on by default outside WindowServer.** `replaceRegion` submitted its copy and waited
  for the GPU before it returned, so an app that uploads many textures per frame stalled once per upload. The
  asynchronous path (its own staging buffer and command buffer, finished before the image is overwritten, read back or
  freed: `plugin/nvmtl_tex_upload.h`) had already run by default since 2.6.3 for CPU-written surface uploads outside
  WindowServer (`NVMTL_ASYNC_SURFACE_UPLOAD`); with `NVMTL_ASYNC_TEX_UPLOAD` it now runs for every region upload outside
  WindowServer, whose uploads stay synchronous. `=1` still turns it on anywhere. What waits is limited: at most 256
  uploads and 256 MiB of staging memory per process, and past either an upload waits for its copy as in 3.0 (the first
  one is logged), so an app that uploads many textures once and never reads them back does not keep a staging copy of
  each. The limits are judged without a lock, so threads uploading at the same moment can pass them by one upload each.
  An upload into a texture over an IOSurface is finished before the surface is published, because for a surface in the
  family's video memory publishing copies nothing and another process could read the old pixels as the new ones. Two
  threads uploading into one texture at once no longer lose the bookkeeping of one upload. Known and left as it was:
  after the GPU channel was rebuilt, an upload it dropped is made again the next time the texture is finished, over any
  copy or blit made into the texture in between (3.0's synchronous upload logged the failure and did not repeat it).
- **Shaders of a game are translated ahead.** In a game (the games category or `LSSupportsGameMode` in its `Info.plist`,
  the test macOS's own Game Mode uses), every entry point of a new shader library is translated by a small pool and
  stored in the shader cache (`NVMTL_PRETRANSLATE`, which only the Cyberpunk profile set before), so the first run of a
  level does not stop for each new shader. Other processes are left as they were; `=1` still turns it on in any process.
  The pool's threads run at utility priority, no longer background: a game's thread that needs a shader the pool is
  translating waits for it on a condition variable, which lends the pool no priority. At most 512 shaders wait in the
  pool's queue; one past that is counted as skipped and translated when it is first asked for, as without the pool.
  Neither of these two was measured on a card yet; the rules are in `plugin/nvmtl_switch_defaults.h`,
  `plugin/nvmtl_tex_upload.h` and `plugin/nvmtl_pretranslate.h`, and `tools/test_switch_defaults.c`,
  `tools/test_tex_upload.c`, `tools/test_pretranslate.c` and `tools/test_plugin_defaults_wiring.py` hold them.
- **WindowServer does not upload a surface whose pages are all zero.** While the screen is shared, WindowServer's
  main thread spent about 16 ms of each captured frame in the Metal plugin, by spindump samples taken before 3.0's
  fast grants. 4.2 ms of it were the surface-in of the new capture surface, which nobody had written: its CPU pages
  were imported, copied into the video memory the kernel had just granted, and waited for. On a surface's first
  surface-in the plugin now reads its pages under the same read-only lock the upload takes (`NVMTL_SURFIN_ZERO`,
  `plugin/nvmtl_zero_scan.h`); when every byte is zero it clears the surface's video memory with a GPU fill ahead of
  the next command buffer and does not upload. Only single-plane surfaces in the family's video memory that no other
  process has drawn into are taken this way, and no VRAM tag is published for them, so other processes upload as
  before; a fill that does not run (its prologue failed or rode a dropped submission) makes the surface decide again at
  its next use, though the command buffers already submitted by then read the video memory as it was, as after a
  failed upload in 3.0. When the prologue's End fails, WindowServer now also marks the managed uploads it carried to be
  made again (they were lost there before); other processes keep 3.0's handling. It is off with
  `NVMTL_PRESUBMIT_PROLOGUE=0`, whose fills would not be ordered against other threads' submits. A later change of the
  surface is uploaded at once, without a second scan. The scan itself costs from
  about 0.5 ms (pages resident) to about 3 ms (pages faulted in), so the saving is an estimate of 1.2 to 3.7 ms per
  captured frame from those samples, NOT measured; the census line "surface-in zero census" counts scans, zero
  answers, their mean time and the full scans that ended non-zero. `tools/test_zero_scan.c`,
  `tools/test_ws_capture.c`, `tools/test_surfin_zero.py` and `tools/test_surfin_zero_plugin.py` hold it.
- **WindowServer frees a dead texture's video-memory import on a thread of its own.** When Core Animation released a
  texture over a surface in the family's video memory and no submitted work could still read it, the RM frees of its
  import ran on WindowServer's main thread: about 1.2 ms of each captured frame in the same samples. Such a free now
  goes to a serial queue (`NVMTL_WS_FREE_OFFMAIN`, `plugin/nvmtl_ws_capture.h`); only imports of the family's video
  memory move, at most 64 MB of them wait at a time (past that a free runs where it did), and the death is recorded
  for fault reports before the free is queued. A free that waited for a submit already ran this way, after the surface
  was released. The free still takes the RM's per-client gate on that thread, so part of the saving can come back as
  waits there, and with the change above on fewer of these frees find no work in flight (the rest are freed later on
  the completion thread, as before), so the two savings overlap; it is an estimate from samples taken before 3.0's
  fast grants, NOT measured.
  `tools/test_ws_capture.c` and `tools/test_surfin_zero_plugin.py` hold it.
- **NVK's own mapped memory that the processor only writes stays in video memory outside WindowServer.** The shader
  heap, the descriptor tables and sets and the other device-local memory NVK itself maps were kept in system RAM,
  uncached on the GPU side, so the GPU fetched shader code and descriptors over PCIe; `NVK_LOCAL_MAP_SYSMEM=0` kept them
  in video memory behind BAR1, and only the Cyberpunk profile set it (its comment says about twice the frame rate there;
  no record of that measurement is in the tree). Video memory is now the default in every process except WindowServer,
  where BAR1 covers the whole VRAM, for the part of that memory that is not shared with another process and that the
  processor does not read back while the app runs: shader code, the image and sampler tables, the zero page and indirect
  execution sets (`nvk/nvk-local-map-vram.patch`). What it reads back keeps system RAM: descriptor pools (a descriptor
  copy reads its source set, the Metal plugin copies unchanged descriptors on every draw, a read through BAR1 is a trip
  to the card, and the plugin's pool alone is 21 MB per process), USERD and the event heap. WindowServer keeps system
  RAM: 3.0 made the placement opt-in after its channel stalled with it. That stall has the shape of a lost doorbell that
  was fixed later, but nobody has shown on a card that the two are the same, so this is the change of 3.0.1 to suspect
  first if an app freezes: `NVK_LOCAL_MAP_SYSMEM=1` (the settings tool's "Keep CPU-written memory in system RAM", on)
  keeps all of it in system RAM in every process, as 3.0 did. `=0` means what it meant (everything in video memory,
  WindowServer included), and `NVK_BAR_VRAM=0` also keeps the new default off. When the video memory is full, or BAR1
  has no room for the map, an allocation the default placed is made in system RAM as before instead of failing. Since
  USERD keeps system RAM, the read of GPPut before a doorbell no longer goes through BAR1, where it used to hold the
  doorbell back until everything written through BAR1 ahead of it had landed; that was the order of the one placement
  that ran on a card (`=0`). Each device therefore keeps one page behind BAR1 (64 KiB of video memory with big pages)
  and reads a word of it before it writes GPPut for a doorbell, so shader code and table entries written just before a
  submission are in the video memory before the GPU can see the work. NVIDIA's own driver does the same where GPPut is
  in system RAM. The read orders what has left the processor's write-combining buffers, so a shader upload and a sync of
  memory the default placed now drain them when they are done. Memory an app maps itself is behind BAR1 in system-RAM
  mode too, as in 3.0, and gets this read only under the default. That the GPU could otherwise fetch them too early was
  never seen on a card; the read costs one trip to the card per doorbell. If the RM's standby fails at a sleep, what the
  default placed is lost with the rest of the video memory, in every process and not only in WindowServer. Not measured
  on a card; `tools/test_nvk_local_map.py` compiles the rule, the placement and the fallback and holds every case.
- **NVK's table of CPU mappings has a lock.** On the NVRM back end every CPU map of a Vulkan memory object adds its
  address to one hash table of the device, and every unmap or free removes it, with no lock: two threads mapping or
  freeing two different memories at once could corrupt the heap. Each insert, search and removal now holds a mutex of
  the device that is taken around the table alone, and an unmap takes its entry out before the kernel unmaps, so a new
  mapping given the same address cannot lose its entry (`nvk/nvk-nvrm-fixes.patch`). A device whose table could not
  be created no longer closes the process's standard input on its way out.
- **`VK_EXT_map_memory_placed` is no longer offered on NVRM.** The NVRM back end ignored the address the application
  asked for and could not map over a reserved range, but the extension was advertised, and Zink turns on client-pointer
  maps when it sees it. It is now offered only where the back end can place a map (Linux nouveau keeps it).
- **Three settings read once.** `NVRM_FLUSH_TIMEOUT_MS` was read on every synchronous flush, and
  `NVK_SYSMEM_GPU_UNCACHED` and `NVK_BAR_VRAM` on every memory allocation, each time with a scan of `nvrm610.conf` and a
  line on stderr when the file set them. They are now read once per process; their values mean what they did.
- **Upstream NVK fixes from Mesa main.** `nvk/nvk-mesa-main-2.patch` carries eight Mesa main commits, unchanged:
  push descriptor sets are uploaded only when they changed and only to their layout's size (they were uploaded again
  at every draw), the single color-write mode is used when every target has the same mask, image format queries take
  stencil usage into account, the swapchain private-data mutex is initialised on macOS too, attachment locations
  of secondary command buffers are read from the right structure, and the range of a map that is synchronised is
  computed correctly (no effect on NVRM, whose maps are coherent). Mesa's NAK compiler changes since are not taken.
  These four entries were compiled on Linux only; nothing in them has run on a card.
- **Metal apps get this build's shader translator.** The Metal plugin opens the `libnvmtl_translate.dylib` beside it
  in `NVMTLDriver.bundle` whenever there is one, and the one in `nvmtl/` only when there is none. Release 2.9, the
  package `./build.sh package` starts from, carries its translator beside the plugin, and the packages built from the
  3.0 branch, and those built from this branch up to b80056ef (which record version 3.0.1 like the fixed ones), put
  this build's in `nvmtl/` only: Metal apps translated with release 2.9's translator, so none of the translator's
  repairs since 2.9 reached them. The package now puts this build's translator beside the plugin too, and the package
  step stops when the two copies are not the same build. Shaders cached with 2.9's translator are translated again
  once: the name of each shader cache entry carries the SHA-256 of the translator file that made it.

### The ways back

One for each change that has a switch, as in 3.0 below; each restores the 3.0 behaviour of that change only. The
changes above that are not in the table have none. One exception: in Cyberpunk 2077 the built-in profile already set
`NVK_LOCAL_MAP_SYSMEM=0` and `NVMTL_PRETRANSLATE=1` in 3.0, and a line in your own block of the file overrides the
profile too, so `NVK_LOCAL_MAP_SYSMEM=1` or `NVMTL_PRETRANSLATE=0` there turns off what 3.0 had on in that game.

| Change | Way back | Kind |
|---|---|---|
| Copy-engine clear of new blocks of video memory | `nvfbceclear=0` | boot argument |
| Asynchronous texture uploads outside WindowServer | `NVMTL_ASYNC_TEX_UPLOAD=0` | `nvrm610.conf` |
| Shaders of a game translated ahead | `NVMTL_PRETRANSLATE=0` | `nvrm610.conf` |
| NVK's own write-only mapped memory in video memory outside WindowServer | `NVK_LOCAL_MAP_SYSMEM=1` | `nvrm610.conf` |
| WindowServer clears an all-zero surface on the GPU instead of uploading it | `NVMTL_SURFIN_ZERO=0` | `nvrm610.conf` |
| WindowServer frees dead video-memory imports on a thread of its own | `NVMTL_WS_FREE_OFFMAIN=0` | `nvrm610.conf` |

## 3.0

**Not released.** This section describes the branch being prepared as 3.0. No test of it on an NVIDIA card has been
reported yet. At commit 3933ff6, `./build.sh` built all 17 components of the driver with no warning on one Intel
Mac (12 threads, macOS 26.7, Xcode 26.5): the kernel extensions (NVRM, NVAccel for macOS 15 and 26, NVRMFB, NVRMAGDC),
NVK, the Metal plugin, the shader translator, the video library and the other components; it then staged a package over
the 2.9 release package there, and the host tests passed there (303 passed, 0 failed, 3 skipped, 9 not run, of 315). At
commit ee7f332 it built the 17 components and staged the package there again; the host tests were not run there at that
commit. At commit aa27c73 it built the 17 components with no warning, staged the package and ran the host tests
there (305 passed, 0 failed, 3 skipped, 9 not run, of 317). It had built that Mac's build tools from an empty working
directory at commit 650db45. At an earlier state of the branch that macOS's `kmutil` found every one of
the 506 kernel symbols NVRM imports, and 291 of the 310 host tests passed there; the seven that failed have been fixed
since. On GitHub's Intel runner (macos-15-intel: 4 processors, macOS 15, Xcode 26.3 with the 26.2 SDK), at commit
3933ff6, the workflow `full-build` built all 17 components and staged the package (its test step failed there because
its clone had no history, which is corrected after 3933ff6), and in the workflow `ci` the host tests passed
(303 passed, 0 failed, 3 skipped, 9 not run, of 315). At commit ee7f332 every step of the workflow `full-build`
succeeded there for the first time: the build, the package, the host tests (304 passed, 0 failed, 3 skipped, 9 not
run, of 316) and its twelve checks of what was built. In the workflow `ci` at that commit the host tests passed with
the same numbers on that runner and, for the first time whole, on GitHub's Apple-silicon runner (macos-15). At commit
95811c2 the kernel extensions built on GitHub's Intel and Apple-silicon runners had the same bytes; no whole build has
run on an Apple-silicon Mac. A package built from aa27c73 on the Intel Mac has been installed over 2.9 on one machine
with an RTX 5050, which started with it twice without the installer's fallback job putting 2.9 back. Nothing committed
after ee7f332 has run on GitHub, and nothing committed after aa27c73 has been built or tested on a Mac: the changes made
after it, among those below, were compiled and their host tests run on Linux only (those that need a Mac have not run).
The check list for a first run on a card is [docs/TESTING-ON-HARDWARE.md](docs/TESTING-ON-HARDWARE.md).

Nothing new in this release has to be switched on: every change below is active as installed. Each risky change that
alters how the driver runs has one way back to the 2.9 behaviour, read once (a boot argument when the driver starts,
a line of `/Library/GPUBundles/nvmtl/nvrm610.conf` when a process starts). These are listed under "The ways back" below
and in [docs/SWITCHES.md](docs/SWITCHES.md), and are there to tell whether a new path is behind a problem, not to be
left set. Several default-on changes have no switch of their own: the interrupt path's closing at sleep, shutdown and
stop; the adapter-teardown order and the idle power-down hold; the per-GPU interrupt mark and the real CPU count; the
unbounded kernel-mapping table; the nano timers apart from their cancel wait; the event connection's close; the
utilization sampler's pause after a slow reading; the framebuffer's scanout floor on a BAR1 of 512 MB or more, its
derived refresh rates checked with NVKMS, its owed connect interrupt and its keeping of an override's
scale-resolutions; the hybrid panel framebuffers' start order; NVK's uncached command and upload stream; the plugin's
fence clock, its refusal of a copy into a multisample image, its kept visible-link answers, its kept grid-only scans,
its one VRAM mark per surface plane per commit, its done tags written before its holds close, WindowServer's early
start of the two-plane round trip, its reading of the macOS version and its keeping of a device it
refuses before Metal's own init, the usage its IOSurface drawables report, its no-op scheduled-present handler for
drawables an application made itself, its half-float mip levels made on the GPU, the storage view of textures of
Unknown usage, its A8 attachment views and the tiled stand-ins of linear attachments in framebuffer-fetch passes; the
shader translator's repairs below; and the kernel extensions staying loaded once started. The way back from these is to
reinstall 2.9 (and, for the power paths, the boot arguments `-nvrmnopm` and `-nvrmnoshutdown`).

### Fixes and changes

#### Desktop, windows and screen capture
- The kernel now knows which process is still drawing into which surface, on which GPU channel and up to which point
  (the surface fence, selector 12 of NVRM's user client). A reader of a surface (WindowServer compositing a window, a
  capture, NVAccel flipping or copying a frame) sleeps until the GPU has really finished the writer's work and is
  woken by the GPU's interrupt, instead of polling a mark that gave up after 10 ms or 100 ms. It stops waiting when
  the writer exits, when its channel faults or when the GPU goes to sleep. Every wait is bounded (2 s for GPU work,
  100 ms for a writer that is stuck on its CPU), and a process whose holds are given up three times goes back to
  2.9's marks and tags. A process that exits or is killed releases the surfaces it was drawing before the driver
  frees its channels. `sysctl debug | grep nvrm_fence` shows what it does. Way back: boot argument `nvrmfence=0`.
  See [docs/SURFACE-FENCE.md](docs/SURFACE-FENCE.md).
- Metal plugin: IOSurface synchronisation between processes goes through that fence: a command buffer that writes a
  surface holds it in NVRM until its work is done, and a reader in another process sleeps until then instead of
  polling a tag for at most 10 ms (and reading a half-drawn frame when the GPU needed longer). WindowServer's swap
  waits for the same holds. A process whose kernel, NVK or channel cannot take part keeps the 2.9 behaviour, per
  command buffer, and a reader that the kernel sends away before the write is known to be complete still gives the
  writer the 10 ms that 2.9 gave it.
- NVK registers its channels with the surface fence and gives the Metal plugin the calls to open, arm, close and wait
  for surface holds. With an older kext, or with `nvrmfence=0`, nothing changes. Waits on the copy-engine (upload)
  queue can sleep in the kernel instead of polling.
- The accelerator waits for WindowServer's GPU work through the surface fence: a flip, a copied frame or a screenshot
  of a window now waits until the GPU has really finished it (up to 2 s, instead of giving up after 100 ms and showing
  a half-drawn frame), and stops waiting at once when the drawing process exits. A wait made while holding the
  accelerator lock keeps the old 100 ms bound. With `nvrmfence=0` the 2.9 behaviour is back.
- A screenshot, a screen recording or any CPU read of a window that WindowServer's GPU is still drawing no longer
  holds the accelerator lock while it waits for the GPU, so the rest of WindowServer keeps running meanwhile. This
  existed in 2.9 behind `debug.nvaccel_pageoff_unlocked_wait=1` (that sysctl is now read-only and reads 1); its
  defects were fixed first: when another thread copied the same surface while this one waited, the copy is not made
  a second time over what was written since, and a copy that came before the GPU had finished is made again instead
  of being taken for the result; a failure to take the lock back is handled; a reference that turns out to be the
  last one is kept instead of freed under the caller, and nothing is read through such an object; a page-off reached
  from the driver's own locked code keeps the lock. If anything unexpected is seen the path turns itself off for the
  rest of the boot (`debug.nvaccel_pageoff_unlocked_standdown` = 1, one log line `page-off: unlocked wait OFF for
  this boot (...)`). Way back: boot argument `nvaccel_pageoff_locked=1`.
- Two-plane (NV12) surfaces, such as screen-capture and video frames, can now live in VRAM one plane at a time: the
  accelerator pages each plane back into its own place in the surface. The plugin uses this only after a round trip
  on a private surface came back right on the card, once per process. Way back: boot argument
  `nvaccel_surf_planes=0`.
- The accelerator now checks, before it attaches, that the accelerator family classes it builds on have the size
  this driver was built for. If a macOS update changed one, the accelerator does not attach and the desktop runs
  without acceleration instead of crashing; the kernel log names the class
  (`NVAccel: Apple class layout refused: ...`). It also reports how many virtual functions those classes have
  (`nvaccel-layout-vtables`); that is a report and never a reason to decline.
- NVRMFB and NVRMAGDC compare the Apple classes they are built on with the running macOS before they start. NVRMFB
  compares the size of IOFramebuffer, IOGraphicsDevice and IODisplayParameterHandler; NVRMAGDC the size of
  AppleGraphicsDeviceControl. On a different size the kext does not start (NVRMFB) or does not create its
  device-control object (NVRMAGDC, `debug.nvrmfb_agdc` = -6) and says which class differs, instead of running on a
  misplaced base. When NVRMFB refuses, NVRM does not start the card and the driver shows nothing on it, since every
  framebuffer of the driver is built on those classes; that is not counted as a failed start. With the OpenCore setup
  of README.md, which excludes Apple's IONDRVSupport, no desktop comes up on the card; where IONDRVSupport is not
  excluded, Apple's firmware framebuffer takes the card and shows a slow desktop without acceleration when the
  firmware's picture still lies inside the card's memory window (otherwise the screen stays dark there too). After an
  install over an earlier driver, while the installer's fallback job is still there (until the first start after the
  one in which it found the display lit), the job finds the refusal at that start, keeps it (the class and both sizes)
  in `/Library/NullMoth/lastgood.log` and, while the Mac still runs the macOS build the install was made on, puts the
  earlier driver back and restarts (where another GPU shows a display and a user is logged in by then, the restart is
  left to that user; a display on the refused card, which only Apple's firmware framebuffer can draw, does not hold it
  back, even after an automatic login). The verdicts are on the GPU's PCI device as `nvrmfb-layout`, `nvagdc-layout`
  and `nvaccel-layout`. NVRMFB also publishes how many virtual functions IOFramebuffer and IODisplayParameterHandler
  have in the running macOS (`nvrmfb-layout-vtables`); that is a report and never a reason to refuse. No result of
  the three checks on a Mac has been reported yet, and the sizes of these classes in macOS 26.7 have not been reported:
  each check decides when its kext starts. Way back for all three checks: boot argument `nvlayout=0`.
- WindowServer marks each surface plane as newest in video memory once per commit, not once for every part of the
  command buffer and every texture that writes it. Each mark is a call into the accelerator that takes its lock, and a
  video playing in a window made many of them per frame.
- A screen share through the system's picker (ScreenCaptureKit: Google Meet in any browser, Discord) no longer
  costs WindowServer a fill of 9 MB from the processor and a wait for the display on every captured frame. Reported:
  while a share runs, the desktop and a game lose most of their frame rate (Minecraft Java from 199 to 40 frames a
  second, to 20-30 with OpenGL), although the other side of the call sees a smooth picture. Measured on that Mac
  (RTX 5050, one 1920x1080 display at 100 Hz, this branch at aa27c73): WindowServer falls from 100 frames a second
  to 20, and the game runs at twice WindowServer's rate. For every captured frame WindowServer makes a new surface
  of 9,043,968 bytes of video memory and destroys it in the same frame. Of the 50 ms each frame took on its main
  thread, 27.4 ms were the framebuffer filling the new block with zeros from the processor, through BAR1 (about
  330 MB/s), and 6.0 ms were the wait for the display gate, which a flip holds while it waits for its latch. Two
  changes in the framebuffer (`kexts/NVRMFB/fb/nvrm-vram-pool.h`), with one way back for both, the boot argument
  `nvfbvramfast=0`:
  - The fill is left out while the RM's own clearing is believed. The RM clears video memory when it is freed,
    refuses to allocate while its scrubber is not valid and reports this as a capability of the device
    (`NV0080_CTRL_FB_CAPS_VIDMEM_ALLOCS_ARE_CLEARED`); NVRM asks once, when the adapter is up. The framebuffer
    reads 65 places of every block it does not fill. The first block that is not zero there, and the first standby
    of the GPU (a system sleep, or an idle power-down where `nvrtd3=1` allows them: the slot loses power, and
    nothing in the RM's open sources clears the memory that was free), each end the belief until the next restart:
    from then on every block is filled as in 2.9, and the blocks that were ready go back unused. After a standby
    the same 65 places are still read before the fill and only counted
    (`debug.nvrmfb_vram_wake_clean`, `debug.nvrmfb_vram_wake_dirty`), so that a card can show whether its memory
    comes back clean. That the memory is clean at boot is the RM's word and NVIDIA's comment, not something the
    open sources show for this card; the 65 places are the only check on it.
  - A size that is granted three times in a row and handed back twice in a row, each within a second of the one
    before, is in churn. For such a size a worker thread keeps up to three blocks ready (64 MB in all, or two blocks
    of a larger size, never more than an eighth of the BAR1 budget; only on a BAR1 over 512 MB), and a grant takes
    one without waiting for the display gate. Every block is still a new allocation with its own cookie, made by the
    same code with the gate held, one per hold; a block that has to be filled is filled by the worker after it let
    go of the gate. Ready blocks go back within a second and a half of the last grant or release of their size,
    before a grant is refused for lack of room, after a standby (none made before it is handed out) and when the
    framebuffer stops. The worker runs as an ordinary kernel thread, below the calls that carry flips and vblanks,
    and calls into the display code only after it has seen the GPU up: a block it holds when a standby begins waits
    and goes back after the wake. Releases are unchanged.
  About 15 to 16 ms of other work per captured frame stay on WindowServer's main thread in that measurement (the
  upload of the new surface, NVK's and the RM's objects for it, the composite), so the most these two changes can
  give there is about 60 frames a second in WindowServer where there were 20 (50 if WindowServer then waits for
  every other refresh of the 100 Hz display), and less after the Mac has slept (the worker then fills each block
  itself, about 27 ms each, so it cannot make one for every frame). Compiled and host-tested on Linux only
  (`tools/test_vram_pool.c`); not yet run on a card.
- Three smaller changes for the same report were written before that measurement, from the code alone. The
  measurement shows they are not what costs the frame rate on that Mac, whose capture surfaces have one plane; they
  stay because each is right for two-plane (NV12) capture and video surfaces, where each surface cost several waits
  in a row:
  - Drawn through private images (always in 2.9; in this release until the round trip below has passed): the two
    planes are copied back to the surface together, under one lock and with one wait for the GPU, where each plane
    took the lock and waited by itself; and since the lock is taken once, the plane copied first no longer looks
    stale and is no longer uploaded again at its next use, which was a third wait, on the compositor's thread. A
    plane whose copy fails is reported by itself and the other one stands. Way back: `NVMTL_SURFOUT_PLANES=0` in
    `nvrm610.conf`.
  - With the planes in VRAM: marking one plane newest moves the surface's one seed, which left the tag of the plane
    marked first one seed behind, so at its next use WindowServer locked the surface (a copy of both planes back to
    system memory in the kernel) and uploaded that plane again, once per captured frame. The other planes' tags now
    follow a mark that took and around which the seed moved by exactly one. That move is the mark's own only where
    every mark moves the seed by one, which is macOS's doing and not this driver's, so the round trip below now reads
    the seed around each of its marks (one of them a mark of video memory that is already newest), and the tags
    follow only in a process whose round trip saw every mark move it by exactly one (the log says which:
    `planar surfaces: each of the self-test's ... marks moved the surface's seed by exactly one`). Elsewhere nothing
    follows, as before. Way back: `NVMTL_PLANE_FOLLOW=0` in `nvrm610.conf`.
  - WindowServer starts the round trip that allows planes in VRAM five seconds after its device is made, not at its
    first two-plane surface: every surface it wrapped before the verdict stayed on private images for as long as it
    was kept, so a share that was the first to bring such surfaces ran on them, and a round trip started while a
    game kept the GPU busy could miss its 2 s bound and leave WindowServer on private images until the next login.
  Compiled and host-tested on Linux only; not yet run on a card, so what macOS does to the seed on a mark is not
  known (the round trip's log line says it). Reading `sysctl debug | grep -E 'nvaccel|nvrm_fence|nvrmfb_vram'`
  twice, ten seconds apart, during a share shows what is left (the counters below among them).
- A monitor that sends a placeholder 1920x1080 identification while it is still waking (after some warm restarts) and
  its real one a few seconds later gets its own resolutions on a card whose BAR1 is 512 MB or more, as long as its
  largest desktop needs no more memory than a 3840x2160 one: there the framebuffer reserves the memory a display scans
  out of with a floor of a 3840x2160 desktop at start, and a mode list that arrives later is measured against the
  memory really allocated (a larger one keeps the first identification's limits until a cold start). Before, the
  reservation followed the first identification and the monitor stayed a 1920x1080 television until a cold start.
  Below 512 MB of BAR1 the floor is not applied, because there it would take a large part of the accelerator's surface
  budget; without `nvfbhidpi` each lit head on a larger BAR1 reserves about 24 MB more video memory for a 1920x1080
  monitor.
- Refresh rates derived from a panel mode of 240 Hz or more (its half and its quarter) are offered only when NVKMS
  accepts their timing; a refused one is logged and left out. Before, a panel that takes only its own timings showed
  the rate as selected and stayed at the previous one.
- A display connected after start-up no longer stays dark when its connect interrupt comes due before macOS has
  registered the handler for it: the interrupt is kept owed and scheduled again 100 ms after the handler is
  registered.
- A display override's own HiDPI sizes (its scale-resolutions) are kept until the display is rotated: the framebuffer
  takes back only the scaled sizes it offered itself for a rotated display.
- Metal plugin: the source fork reports Spotlight's minified half-float application icons missing, because Core
  Animation leaves the lower levels of R16Float, RG16Float and RGBA16Float textures unwritten. The plugin now makes
  those levels with the GPU's filtered blits. From the HighDelay/nvidia-macos-driver-nvk fork, which reports it on an
  RTX 2060 SUPER, macOS 26.7.1; built and checked on Linux only here.
- Metal plugin: the source fork reports stale layers in native applications' icons and in the Applications grid. A
  shader-writable wrapper in Managed storage of a single-plane surface in the family's VRAM is now drawn, sampled and
  read by framebuffer fetch through a tiled image of its own, which is copied into the linear VRAM import before the
  surface is marked and copied back from it when another writer made the VRAM newer. With framebuffer fetch on (every
  process but WindowServer), a pass that has a linear colour attachment beside a tiled, unaligned or depth attachment
  draws the linear one through a tiled stand-in, at the cost of one more image and two GPU copies per such pass whether
  or not a pipeline of the pass reads the attachment; that cost has not been measured. From the
  HighDelay/nvidia-macos-driver-nvk fork, which reports fresh native icons and the Applications grid drawn correctly on
  an RTX 2060 SUPER, macOS 26.7.1; built and checked on Linux only here. Way back for the tiled image:
  `NVMTL_MANAGED_SURFACE_TILED=0` in `nvrm610.conf`; the stand-ins have no switch of their own (`NVMTL_NO_FBFETCH=1`
  turns framebuffer fetch off, and them with it).
- Metal plugin: a texture of Unknown usage gets a storage view when it is made, so shader writes through its mip and
  slice views land; the first format queries of several threads at once no longer make textures without their storage
  view; A8 colour attachments are attached through identity views, and a framebuffer-fetch read of one returns Metal's
  (0,0,0,r) instead of (r,0,0,1). A linear alias of a buffer takes the format features of DRM modifier 0 when NVK lists
  that modifier for the format, which can allow render-target use that plain LINEAR tiling's features do not; a format
  for which NVK lists no modifier (B10G11R11_UFLOAT and E5B9G9R9_UFLOAT among them) keeps plain LINEAR tiling's
  features. From the HighDelay/nvidia-macos-driver-nvk fork; built and checked on Linux only here.

#### Applications and games
- An application tells readers that a surface is finished (its done tag, then the close of its surface fence hold)
  before it reports the command buffer as scheduled and before it hands its drawables over, not after: WindowServer
  compositing a frame that was just presented found it marked as still being drawn and slept until the
  application's completion thread got that far. The done tag is also written before the hold is closed, where a
  reader arriving between the two polled the tag. Way back for the first: `NVMTL_BUSY_DONE_EARLY=0` in
  `nvrm610.conf`; the order of tag and close has none.
- Applications no longer poll through long GPU waits: after 1 ms a waiting thread sleeps on the GPU's completion
  event, as WindowServer already did, and a thread whose waits run longer than about 2 ms sleeps almost at once;
  waits shorter than 1 ms are unchanged. A process whose wake-ups do not arrive goes back to polling by itself; an
  event that merely arrives late (the GPU shows a completion just before its event) does not count towards that, so
  a game or compositor that waits at a steady cadence keeps sleeping. Going back to polling is not for the life of
  the process, because wake-ups can stop for a moment and come back: each of its threads tries the event again after a
  pause (256 of its waits, doubled after each failed try up to 65536), and the first wake that arrives puts the whole
  process back on the event. Only a kext that cannot wait at
  all keeps a process polling for good. Way back: `NVRM_EVENT_WAIT=0` in
  `nvrm610.conf`. Like 2.9's `0`, it makes every process poll, WindowServer included, which costs WindowServer more
  CPU than both 2.9's default and this release's. See [docs/EVENT-WAIT.md](docs/EVENT-WAIT.md).
- A GPU fault on one channel no longer ends the application's Metal device. When a channel faults with an error that
  ends only that channel, or makes no progress for 5 s in WindowServer (60 s in any other process), NVK builds a new
  channel, sends the channel-wide state again, replays the submissions that had not started and drops the one that was
  running; a stalled channel's doorbell is rung again first. A fault on any copy engine is rebuilt like this too: on
  an RTX 50 (Blackwell) card the channel NVK uses for uploads runs on copy engine 4, whose fault the hardware reports
  as Xid 71. A copy channel replays all of its work instead of dropping any, and one that faults again inside the work
  its rebuild replayed ends the device at once. A fault the GPU cannot recover from this way (firmware, bus, memory
  errors, a failed wake, an error that is not on the list of those that end only one channel, a fourth rebuild of one
  channel within 30 s) ends the device exactly as in 2.9. A Mac put to sleep with a game or WindowServer waiting on
  the GPU no longer loses the device or rebuilds a channel that was only stopped for the sleep: the kext answers NVK's
  channel note without waiting for the GPU's resume and reports the GPU as suspended until it lets RM calls through
  again, neither the thread that submits to a channel nor its doorbell ring makes an RM call or escape while it holds
  the channel's lock (only the recovery sequence calls RM with the lock held, besides the test hook when
  `NVK_FAULT_INJECT` arms it; for a channel that merely stands still it starts only once the kext no longer reports the
  GPU suspended, or after 360 s), and a stall seen while the GPU is suspended restarts the no-progress budget instead of
  rebuilding, at every sleep, for up to 360 s of awake time per sleep. A wait that stands still behind another channel
  that hangs without a fault (the graphics channel behind the upload channel, for example) has that channel rebuilt too,
  within the same limits; before, depending on timing, the application could lose its device instead, with the line "the
  channel it waits on is still progressing". Way back: `NVK_CHANNEL_RECOVER=0` in `nvrm610.conf`. See
  [docs/CHANNEL-RECOVERY.md](docs/CHANNEL-RECOVERY.md).
- After such a recovery the Metal command buffer whose work was dropped completes with an error instead of as success,
  and the process keeps its device; the plugin no longer waits for, and counts, the completion record such a command
  buffer can never write (eight of those used to switch command-pool reuse off for the process). A device loss that
  is not recovered now names its kind (firmware, bus, failed wake, ECC) in the log. Way back for the error report:
  `NVMTL_DROP_REPORT=0` in `nvrm610.conf` (the command buffer is reported as completed).
  When a rebuild drops a command buffer that carried uploads of managed buffers (the plugin's pre-submit prologue),
  those ranges are uploaded again at the next submission instead of staying unwritten on the GPU, and a line in the
  system log says so for the first such drop of a process and every 256th (`vk: after a GPU fault, U managed uploads
  and Z zero fills rode a dropped command buffer ...`). The GPU zero fills of new buffers that rode it (Private buffers
  and, by default, the VRAM copy of a new managed buffer) are not made again: such a buffer holds what the VRAM held
  wherever nothing was written or uploaded since.
- NVK's own warnings and errors no longer go to a file in `/private/tmp` from every Metal process. An installed
  plugin writes them to `nvmtl.log` only for a process started with `NVMTL_PROCESS_ENV=1`; from every other process,
  WindowServer included, NVK's errors and the two channel-recovery lines (`nvrm channel slot N rebuilt after ...` and
  `nvrm channel slot N not rebuilt: ...`) reach the system log with the prefix `NullMoth:`, while its other warnings
  stay out of it.
- In a program that uses NVK's Vulkan driver without the Metal plugin, a setting written twice in
  `/Library/GPUBundles/nvmtl/nvrm610.conf` now takes its last line, and a comment that names a setting is ignored: the
  rule a Metal process already followed and the one the documents describe. NVK's reader stays stricter than the
  plugin's: it reads only the first 8191 bytes of the file, where the plugin reads it whole, it takes a line only when
  the line begins with the name and its `=` with no blank before either, and the value is everything up to the end of
  the line, blanks included, up to 63 bytes. A line written `NAME=value` from the first column reads the same in both.
  This now holds for NVRM_ASYNC, NVK_DIAG, NVK_QUEUE_COUNT and NVMTL_GDQ_DQ too, which took their first line in such a
  program (for the settings NVK reads from the file itself; the eight `NVRM_610_` items keep the older rule there).
- The Metal plugin's fence wait (the one that gives a fence up as lost after ten budgets: 50 s in WindowServer) no
  longer counts time asleep, so a frame that was pending at sleep is not declared lost at wake.
- A texture the application releases while the GPU may still use it is now freed only once no command buffer that
  could still read it is open or in flight (the image grace, before only in the Cyberpunk 2077 profile). A late read
  is then a stale read of a dead texture instead of a GPU fault that takes the channel down. Way back:
  `NVMTL_IMAGE_GRACE=0` in `nvrm610.conf` (released textures are freed at once, as 2.9 did by default).
- Fixed: with the image grace on (the Cyberpunk 2077 profile turned it on), released textures were never freed and
  memory grew for the whole session. The grace counted some command buffers as begun and never as finished (the
  managed-upload prologue, the counter reset, submits that failed early), so the point it waited for moved further
  away every frame. It also could free a texture too early when a command buffer begun later was submitted before one
  that was open when the texture died. Both are gone: each command buffer is counted as finished exactly once, and a
  texture waits exactly for the command buffers open when it died.
- NVK shader compiler: the fixes Mesa made after 26.3.0-rc3 are built in. Mesa 26.3.0's fix for RTX 50 (a register
  could be read before its uniform load had finished) and its NIR fixes, plus eight NAK commits from Mesa main:
  barrier handling, RTX 50 matrix-instruction latencies, instruction scheduling, and flattening of small branches.
  2.9 kept them behind `NVK_EXTRA_PATCHES=nvk-upstream-nak`, a build option that did not build. Shaders are compiled
  again once after the update. When building from source, `NVK_SKIP_PATCHES` leaves one of the three patches out to
  find the cause of a rendering fault (see [docs/NVK-BASE-AND-HEADERS.md](docs/NVK-BASE-AND-HEADERS.md)).
- A copy from a buffer or from host memory into a multisample image is refused before it reaches Vulkan, and the first
  eight are named in the log: it is not valid Vulkan, and the copy engine raises an error for it that the GPU firmware
  may not recover from.
- System Settings > Displays could hang for seconds when it opened: the translator parsed a function's whole shader
  text again each time newFunctionWithName:constantValues: asked whether its visible functions must be linked first.
  The plugin now keeps the translator's definite answer per shader text; an answer read from the translator's error
  text is never kept.
- Programs built with an SDK older than macOS 11, which the kernel tells the system version 10.16, are no longer
  refused the NVIDIA device on macOS 15 and 26: the plugin then reads the version from kern.osrelease. A device the
  plugin refuses before Metal's own initialisation (the version check, or another card under `nvmultigpu=1`) no
  longer crashes the program: the uninitialised object is kept instead of being freed into Metal's dealloc.
- NVK writes its command and upload stream into plain system memory instead of memory the GPU caches. A small push
  written behind the previous one in the same cache line could be fetched by the GPU as the line was before the
  write, and the channel stopped with an invalid pushbuffer entry (Xid 32). On 2.9 this could take the GPU from a
  process at login (a transparent Dock, missing icons, a lock screen that crashed in a loop); with this release's
  channel recovery the same fault rebuilds the channel and drops the submission that was running (on the upload
  channel that work is replayed, and a second fault there ends the device). The command pool's own memory is
  unchanged and still follows `NVK_CMDPOOL_GPU_UNCACHED`.
- OpenGL applications run on NVK through Mesa's Zink on macOS 15.5 and later. The accelerator names
  `NVKZinkGLDriver.bundle` as its OpenGL driver (2.9 named Apple's AppleMetalOpenGLRenderer, and before macOS 15.5 it
  still does). The bundle forwards every GLD entry to Apple's renderer and hands each new OpenGL context to a CGL
  adapter, which points the context's dispatch table at Zink (Mesa 26.3.0) running on a private copy of this build's
  NVK. A context with an AppKit view presents through its CAMetalLayer, its frames copied on the GPU into the IOSurface
  of the layer's drawable; a context without a view, or whose layer cannot be used, presents through an IOSurface that
  NVK imports. No frame goes through a CPU copy. The dispatch table is generated when the driver is built, from the
  macOS SDK's public OpenGL headers. The adapter also reads and writes through offsets in GLEngine's private context
  data that the source fork recorded on macOS 15.8.1 (24H32) and 26.7.1 (25G241); before any such read or write it
  checks that the memory is shaped as recorded. That check runs before the process's Zink screen is made, so a refused
  layout opens no NVK device. A context stays on Apple's renderer when that check fails (the first refusal in a process
  is written to its standard error), when Zink cannot start in the process, when the adapter is missing, and in OBS:
  there OpenGL always stays Apple's and a screen capture asks for BGRA8 instead of RGBA16F (RGBA16F can fail before the
  first frame on this driver). A Zink context stays allocated while it is current in Mesa on any thread: a context that
  CGL destroys while it is still Mesa's current context on another thread is freed at that thread's next bind. When the
  installed AppleMetalOpenGLRenderer lacks a GLD entry the bundle forwards, the bundle points that entry at a stub that
  returns an error and forwards the others, unless it is gldGetVersion, gldCreateContext or gldDestroyContext, which the
  bundle calls itself (or Apple's renderer cannot be opened at all): then the bundle offers no OpenGL for the card, and
  a start with `nvzink=0` gives Apple's renderer back. From the HighDelay/nvidia-macos-driver-nvk fork; built and
  checked on Linux only, it has not run on a Mac. Way back: boot argument `nvzink=0` (Apple's OpenGL for every
  application, as in 2.9). See [docs/ZINK-OPENGL.md](docs/ZINK-OPENGL.md).
- Minecraft 26.3, MPV and other Vulkan applications that draw into a window can render with NVK instead of MoltenVK: the
  package installs a native Vulkan runtime at `/Library/GPUBundles/nvmtl/minecraft`, which renders with a private copy
  of this build's NVK and copies each frame on the GPU into the window's CAMetalLayer drawable. Nothing selects it by
  default; an application chooses it when it is launched. Minecraft takes the JVM argument
  `-Dorg.lwjgl.vulkan.libname=/Library/GPUBundles/nvmtl/minecraft/lib/libnvk_minecraft.dylib` and Vulkan in its graphics
  settings; MPV and other applications that use the Vulkan loader take
  `VK_DRIVER_FILES=/Library/GPUBundles/nvmtl/minecraft/nvk_bridge_icd.json` in their environment. Applications that do
  not ask for it are unchanged. Limits: one window per swapchain, B8G8R8A8 images (SRGB and UNORM), FIFO and immediate
  present modes, no two identical NVIDIA GPUs, and for every frame a CPU wait and two kernel calls that take the
  accelerator's lock (not measured on a card). The package carries the two guides beside `install.sh` as
  `MINECRAFT-NVK.md` and `NVK-APPLICATIONS.md`. From the HighDelay/nvidia-macos-driver-nvk fork, which reports Minecraft
  26.3 rendering through it on an RTX 2060 SUPER, macOS 26.7.1, with its own NVK build; this tree's runtime was built
  and checked on Linux only. See [tools/nvk-bridge/INSTALL.md](tools/nvk-bridge/INSTALL.md) and
  [tools/nvk-bridge/APPLICATIONS.md](tools/nvk-bridge/APPLICATIONS.md).
- Photo Booth's camera preview shows, upright. QuartzCore wraps a drawable that is not framebuffer-only with a usage of
  ShaderRead or Unknown, and Core Image refuses a destination without ShaderWrite, so the preview stayed black. An
  application's single-plane IOSurface wrapper that is not framebuffer-only now asks for a storage view before the
  family's VRAM is imported and reports ShaderWrite only when it has one; a writable wrapper whose linear import lost
  the storage view or the colour attachment gets a private image instead. WindowServer's surfaces keep the usage they
  asked for. In Photo Booth only, the image Core Image renders into the preview's drawables is reflected within the
  rendered bounds: the source fork reports that Photo Booth's call writes the rows bottom first. From the
  HighDelay/nvidia-macos-driver-nvk fork, which reports the preview visible and upright on an RTX 2060 SUPER, macOS
  26.7.1, with a USB webcam; built and checked on Linux only here. Way back for the reflection:
  `NVMTL_PHOTOBOOTH_FLIP=0` in `nvrm610.conf`; the usage rule has none. See
  [docs/PHOTOBOOTH-PREVIEW.md](docs/PHOTOBOOTH-PREVIEW.md).
- A drawable an application made itself (not a CAMetalDrawable) that answers `addPresentScheduledHandler:` is given an
  empty handler before it is presented. The source fork reports that Parallels' AsyncDrawable calls that handler from
  -present without checking it, so a command buffer that registered none crashed it there. Neither the source fork nor
  this tree has run it in a Parallels virtual machine; built and checked on Linux only here.
- Shader translator: an argument buffer's array of textures, and a single texture buffer in one, is read through the
  bindless heap, each element by its own handle, so a shader that picks a texture by an index known only when it runs
  gets that texture; declared as one array binding it had one descriptor, and a robust driver clamps the index to
  element 0. Only an array of texture buffers keeps its binding. The developer switch `NVMTL_NO_TIER2_FIXED=1`, which
  turns off the fixed-field argument-buffer path as a whole, keeps the descriptor path for these too.
- Shader translator: Safari's WebGPU shaders keep a texture the optimizer copied through a 64-bit integer, read the
  sample they ask for from a multisampled texture, and get a descriptor for a texture they only query for its size or
  sample count. The source fork reports that a WebGPU game in Safari drew its scene after these repairs, on an RTX 2060
  SUPER, macOS 26.7.1; it has not run here. See [docs/SAFARI-WEBGPU-TEXTURES.md](docs/SAFARI-WEBGPU-TEXTURES.md).
- Shader translator: `array<sampler, 1>` is a sampler array, every element of a sampler array keeps its own run-time
  sampler state, a sampler picked among 2 to 16 in a retry translates (the bound was 2 to 8), GL's ivec2 uniform read
  through a vec4 slot translates, and inlining a helper keeps a constant-expression argument (a vector splat, for
  example) whole.
- Shader translator: a texture or sampler kept in a field of a local object reaches its sample call; address-space
  parsing is strict; `array_ref<void>` table reads and stores of a device pointer into a local translate; the RGB10A2
  packing residual is computed without contraction.
- Shader translator: buffer-address lowering keeps the descriptor path for a shader whose address-table pointer
  reaches a call, a phi or a return, instead of changing types under it. For a buffer that keeps its descriptor it
  finds every chain of pointers first and then checks every use wherever the use stands in the function, so a loop
  that picks the buffer by a phi's back-edge value no longer leaves it on a descriptor the phi cannot use.
- Shader translator: static-initializer folding builds its token map once per function, and the plugin's sampler
  entry point shares one parse per translation.
- The shader translator's repairs above come from the HighDelay/nvidia-macos-driver-nvk fork and were built and checked
  on Linux only here ([docs/TRANSLATOR-VALIDATION.md](docs/TRANSLATOR-VALIDATION.md) lists the checks and where each
  ran).

#### Video
- RTX 50: H.264 video in Safari, QuickTime and FaceTime (green picture). The decoder from the vendored codec library is
  now used in the system decode service by default, after the card has decoded two built-in test streams bit for bit;
  if that test fails or cannot run, nothing changes and one line in the system log says why. The plugin loads on
  RTX 50 only a build of the library that carries this test; an older build is refused there. 2.9 listed this under
  "Experimental" (opt-in). Way back: `NVMTL_NVENCVT=0` in `nvrm610.conf`. Not yet confirmed on a card.
- Codec library: the card's generation is now read from the GPU itself. The library took the first NVIDIA PCI function
  with a device ID it knew, and the HDMI audio function of an RTX 40 and of most RTX 50 cards has an ID in the RTX 30
  range; where the system listed that function before the GPU, an RTX 40 could lose the library's encoders and its
  H.264 and HEVC decoders, and an RTX 50 could be treated as an RTX 30. Found by reading the code; nobody has reported
  the symptom.
- The codec library's build compares the NVIDIA structures it uses with the kernel module's version and stops on a
  difference.
- The plugin keeps the result of a compute kernel's grid-only scan per library text and kernel name, so a pipeline
  built for each converted frame (VideoToolbox's pixel transfer, a camera preview) no longer scans the whole module
  text again.
- A camera preview that stayed black: textures in the packed 4:2:2 video formats (BGRG422, GBGR422), in which
  CoreVideo hands camera frames to Metal, are made by default, as Apple's drivers make them. A camera frame whose rows
  would end past its surface's memory is skipped with a log line instead of being read, and before a frame is read the
  plugin waits, for a bounded time as for other IOSurface textures, for another process's GPU write of it or for
  WindowServer's. Way back: `NVMTL_YUV422=0` in `nvrm610.conf` (4:2:2 textures are refused, as in 2.9). Not yet
  confirmed on a card.

#### Kernel (NVRM)
- The RM's background work runs on one worker per GPU, in order, as on Linux, instead of one thread per item.
  Stopping the driver, shutting down and going to sleep wait for that work with a time limit. RM work items marked to
  be dropped during an unload are dropped only while a GPU is being taken down or put to sleep, as on Linux; before,
  they were also dropped while the GPU was waking up, so some work the RM queued around a wake never ran. Way back:
  boot argument `nvrmworkq=0`.
- The driver tells the RM the truth when it calls in from a context that must not sleep: the RM then takes its own
  non-blocking branches, small allocations come from a pool, and locks are tried before the blocking call. Way back:
  boot argument `nvrmatomic=0`.
- The RM's bottom half runs on a thread of its own per GPU, as on Linux, so a bottom half that waits for the GPU lock
  no longer delays the next interrupt. A requested bottom half is always run, also when the GPU stops at the same
  moment. Way back: boot argument `nvrmbhthread=0`.
- The RM's 1 Hz timer runs (bus presence check, NVENC session statistics). It pauses around sleep, RTD3 and shutdown,
  and is stopped and let go before the GPU's state is freed. Way back: boot argument `nvrmrctimer=0`.
- RM nano timers can no longer run their callback after the timer was destroyed (use after free), and timers longer
  than 71 minutes no longer fire early. Way back for the new wait only: boot argument `nvrmnanowait=0`.
- Sleep, shutdown and the stop of a GPU close the interrupt path and wait, bounded, until it is quiet. The stop and
  the full shutdown (`nvrmshutdown=full`) take the adapter down in Linux's order, `rm_disable_adapter` first; the
  waits that may take 30 s when a GPU is stopped are 2 s at shutdown, and when the system's 8 s run out the interrupt
  path is closed anyway.
- Two GPUs no longer corrupt each other's interrupt-context mark in the RM. The RM sees the real number of CPUs.
- The start-up waits for the displays, the arming of the accelerator and the boot raster measurement end as soon as
  what they wait for is there, instead of at the next whole second, and never later than before. Each phase is in
  `debug.nvrm_boot_*` and in the `nvrm-boot-timing` property. With `nvrmsettle=0` the bring-up can no longer begin
  before the driver's start has finished (it waits at most 5 s). On a card that deliberately settles for more than 5 s
  during start-up, lighting its display by hand in that window no longer counts the start against the safety that
  turns the driver off after two starts that never lit a display. Way back: boot argument `-nvrmoldwaits`.
- With Intel VT-d active (AppleVTD), the GPU is given bus addresses mapped through the DMA mapper, as the Linux driver
  does with an IOMMU, instead of physical addresses that the mapper translates to the wrong place. Every mapping is
  read back from the mapper before the GPU sees it, and the GPU is not started at all if a two-page self-test does not
  read back (that is not counted as a failed start). The accelerator's copies of GPU memory to the CPU go through a
  buffer mapped the same way. Without VT-d nothing changes. `sysctl debug.nvrm_dma_mode` and the `nvrm-dma` property
  show which case a machine is in; `nvrmdma=0` refuses to start the GPU behind a mapper (diagnostic). No run with a
  mapper on real hardware has been reported yet: see [docs/DMA-IOMMU.md](docs/DMA-IOMMU.md).
- The event wait reports why each wait returned and counts it (`debug.nvrm_evt_*`); a thread turned away because 256
  threads already sleep is told so instead of looking woken. Closing a process no longer races an RM event post into
  the freed connection: the close waits for a post in flight (as the Linux driver does) and keeps the connection
  rather than freeing it if the post never finishes.
- A user client that was never opened completely (its attach or start failed) no longer leaves a stale entry in
  NVRM's table of clients; later lookups read that entry after the client's memory had been freed.
- The table of the driver's kernel mappings has no size limit any more (it could fill on cards with a large BAR1,
  after which mappings were never released); lookups no longer scan it.
- `nvregdwords=` values reach the RM as written. Before, the function that converts them had no body and every value
  arrived as 0. The log lists each key with the value the RM received, once per boot; an entry with a second `=`
  (which the RM skips) is logged as ignored, and an empty entry (`;;`) no longer ends the list.
- Firmware bug checks and RM assertion failures are logged and counted (`debug.nvrm_bugchecks`,
  `debug.nvrm_rm_asserts`) instead of being ignored; neither stops the machine.
- NVRM counts every Xid the RM logs (`debug.nvrm_xid_count`, `debug.nvrm_xid_last`) and what NVK did about faulted or
  stalled channels (`debug.nvrm_chan_*`). The kext only counts and answers the GPU's power state; no new switch.
- The sleep line says how much VRAM the RM wrote to its save file during standby.
- NVRM.kext keeps itself loaded once a start has got past the framebuffer's class check on a card. Nothing it
  registers (sysctls, its work-queue thread, timers) was ever taken back, and the kernel unloads a driver by itself
  about a minute after its last object is gone (a card the DMA check declined, an unplugged card): the next
  `sysctl -a` then walked memory that was gone. `kmutil unload -b com.nullmoth.NVRM` is now refused with the kext in
  use. Installing, updating and removing the driver are not affected (they take effect at the restart).
- `nvrmctl go 2` after a second pass that failed once its interrupt source was set up is refused, instead of setting
  up a second interrupt source over the first (the first was then never removed and delivered interrupts to freed
  state when the GPU stopped).
- With `nvrtd3=1`, stopping a GPU (unplug) first takes it away from the idle power-down monitor, which could otherwise
  suspend or resume the GPU while it was being torn down, and went on calling into an object that had been freed.
- With `nvrtd3=1`, system sleep and shutdown no longer race the idle power-down monitor. Its polls went on during both
  and ran the GPU's own suspend and resume on another thread: the GPU could be suspended twice at once, resumed while
  the system went to sleep (it then woke marked running on a slot that had lost power, and was not resumed), or have
  its power resource switched off under the system's resume. The system now holds the GPU from the start of its sleep
  until the end of its wake, and for good at shutdown; the monitor's calls do nothing meanwhile, except the power-on
  that the wake itself asks for. The monitor also suspends only a running GPU and resumes or powers off only a
  suspended one, so a poll that a sleep and wake cut in two no longer switches the slot off under a running GPU, and a
  GPU whose wake failed is left alone.
- When NVRM declines a card early (the framebuffer refused this macOS's Apple classes), NVAccel does not hold the
  card's matching for its 30 s wait: it stops at once and logs `NVRM declined this card ... (nvrm-layout: ...)`.
- Only WindowServer, running under a system account (user id below 500, the rule NVRM's client table already uses),
  can take a compositor-class hold in the surface fence or NVAccel's pending marks and so delay another process's
  flips, copied frames and page-offs for up to 2 s at a time. A program that merely names its executable
  "WindowServer" cannot; the real WindowServer is unaffected.
- A surface-fence wait or look hands the writing process's pid and the state of its hold back only to the writer
  itself. Whether a surface (or a given process) has a write pending can be asked by any process.
- A process that fails channel registrations of the surface fence in a loop cannot flood the system log: only the
  first 8 failures since boot reach it; later ones go to the debug output, and `debug.nvrm_fence_map_fail` and
  `debug.nvrm_fence_map_mismatch` count them all.
- A GPU utilization reading that takes longer than 500 ms pauses the once-a-second sampler for 20 s, and three such
  readings in a row stop it for the rest of the boot; each slow reading is logged with its duration. The reading holds
  the RM's locks while it waits for the firmware, and a firmware that answered slowly could keep other processes,
  WindowServer and the shutdown waiting on them every second. The time includes the wait for those locks, so one
  reading held up by a mode set or a channel recovery costs 20 s of GPU usage statistics, not the rest of the boot; a
  fast reading, answered or failed, starts the count again.
- Hybrid laptop panels (NMIntelFB, NMGopFB; `nmhybrid=1` or `2`): each framebuffer sets its dependent ID and index
  before IOFramebuffer::start, as NVRMFB does, so it joins the shared controller at start and the first power change
  no longer meets a missing controller (a page fault at boot), and the accelerator finds the panel's index 4 when the
  panel publishes. A panel surface that cannot be mapped leaves the panel to the firmware framebuffer, and a failed
  IOFramebuffer::start of NMIntelFB undoes its timers, its display-firmware target and its backlight service through
  the same routine stop() uses.

#### Installing and the way back
- `install.sh` installs native OpenGL's `NVKZinkGLDriver.bundle` beside `NVMTLDriver.bundle`, backs it up with the
  other bundles and puts the earlier one back (or none, when there was none) on a rollback and in the fallback job;
  its runtime (`nvmtl/zink`) and the native Vulkan runtime (`nvmtl/minecraft`) go with `nvmtl`. A package without the
  bundle or without `nvmtl/zink` is refused before anything changes, because the accelerator names that bundle as the
  OpenGL driver. `uninstall.sh` removes the bundle with the others. Once the bundle is in place, `install.sh` asks
  macOS's trusted GPU-bundle lookup for it with `nvmtl/zink/gld_bundle_probe` (`tools/gld_bundle_probe.c`, from the
  HighDelay/nvidia-macos-driver-nvk fork), with `NVMTLDriver.bundle` as the control: when the lookup finds
  `NVMTLDriver.bundle` but not the OpenGL bundle, the install stops and rolls back; when it does not find
  `NVMTLDriver.bundle`, the installer says it could not check the OpenGL bundle and goes on. The installer and
  uninstaller of 2.9 and of earlier 3.0 packages do not know the bundle: going back with one of them leaves
  `/Library/GPUBundles/NVKZinkGLDriver.bundle` in place unless this package's `uninstall.sh` runs first.
- The installer's fallback job (it puts the earlier driver back when the new one cannot start) copies the earlier
  kernel collection out of the backup before it changes anything and puts it in place by a rename. Before, it copied
  over the collection in place without checking the copy: one that failed half-way (a full volume) left a cut
  collection and no job to try again; now such a failure changes nothing and the job stays for the next start.
- The fallback job looks for a lit display at each of its looks, whichever of it and the kexts starts first, and
  stays through the start in which it first finds the display lit, so a WindowServer that keeps dying in that start
  still finds the way back; it removes itself at a later start. A start count of two that already includes the
  present start (a card NVRM has claimed) no longer puts the earlier driver back while this start may still light the
  display.
- A second run of `install.sh` before a start that lit the display (to add the DLSS files or the boost switch, or a
  newer build over an untried one) keeps the first run's backup as the way back, with that backup's own macOS build,
  and says so in step 6b. Before, the fallback job would have put back the driver that was on trial.
- The boot-guard job ends an install's trial only on a desktop of this driver: a card NVRM claimed, and either its
  display path reported up (`debug.nvrmfb_guard_cleared`) or a display on another GPU. A desktop with no NVIDIA driver
  running only clears the start count, and a claimed card with nothing lit and no display on another GPU keeps its
  count. Before, any WindowServer that stayed up 45 s ended the trial, so the way back could be gone before the new
  driver had shown a picture.
- A WindowServer that keeps dying while the accelerator refused this macOS's classes (`nvaccel-layout` says
  `refused`) now turns the driver off and restarts once from the login window, as with an armed accelerator, when
  every display hangs off the card NVRM claimed.
- The installer's last lines no longer tell you to undo the install with `uninstall.sh`, which removes the driver and
  restores nothing: they name the folder that holds what was installed before and say to install its own package
  again.
- A rehearsal (`sudo env CHECK=1 ./install.sh`) that stops in step 3 no longer leaves a `/var/tmp/nullmoth.XXXX` folder
  with a copy of every third-party kext behind.
- The uninstaller also removes the hybrid laptop panel drivers (NMIntelFB, its virtual test build NMIntelFBVirtual,
  and NMGopFB) with the rest of the driver. Before, it took one left in `/Library/Extensions` for another product's
  kext and stopped ("the removal collection still lists the NVIDIA driver or selector") before changing anything, so
  the driver could not be removed from a hybrid laptop that had one.
- Another com.nullmoth kext that the uninstaller does not remove (NMHDA, NMKbdBacklight, NMDRM, NMSMN) left in
  `/Library/Extensions` no longer stops the removal with "the removal collection still lists the NVIDIA driver or
  selector": the two checks of the rebuilt collection look for the identifiers of the kexts the uninstaller removes,
  each as a whole name, and a kext of this driver still stops it. The AMD/Intel GPU driver's accelerator (NMXAccel)
  still stops it, now with its own reason: it uses this driver's Metal plugin, which the removal takes away.

#### Switches and reports
- `sysctl debug.nvrm_switches` lists the driver boot arguments a Mac has set beside the install line and marks with a
  `!` the ones an ordinary install does not need (an opt-out, a test hook, a development switch, a name this version
  no longer reads). `sysctl debug.nvrm_switches_nondefault` is 0 on an ordinary install. A Mac that behaves unlike
  others with the same card often still carries a switch from an earlier test; this shows it. Settings in
  `nvrm610.conf` are not part of this list.
- [docs/SWITCHES.md](docs/SWITCHES.md) lists every switch the driver reads (environment names, boot arguments,
  sysctls, flag files), what each does, and what is in effect when it is not set.
- The GPU policy file of a hybrid laptop (`/Library/GPUBundles/nvmtl-gpu-policy.txt`) takes a comment after a rule,
  as the example in [docs/HYBRID-LAPTOPS.md](docs/HYBRID-LAPTOPS.md) shows them. In 2.9 a `#` started a comment only
  at the start of a line, so a file copied from that example set no mode and named no process, and `mode apple` with a
  blank after it set nothing either. Such a file takes effect with this release: copied whole, the example gives
  `mode apple` (the last of its two `mode` lines) and its two process rules.
- Three read-only counters say what the accelerator's lock costs the threads that mark surfaces, a screen share's
  copies among its holders: `debug.nvaccel_surf_mark_lock_waits` (marks and grants that found the lock taken and
  waited), `debug.nvaccel_surf_mark_lock_wait_us` (the time they waited, in total) and
  `debug.nvaccel_surf_mark_lock_wait_max_us` (the longest wait; a value near 100000 means a page-off waited for the
  GPU with the lock held). They change nothing the driver does.
- Read-only counters for the granted blocks of video memory (`sysctl debug | grep nvrmfb_vram`): what the RM
  answered (`_rm_clears`), standby, resume and lost messages so far, one for each framebuffer of the card
  (`_power_events`), blocks not filled and filled (`_fill_skipped`, `_filled`, the time the fills took in
  `_fill_us`), whether a block was not zero although the RM clears (`_dirty`), what new blocks read after a sleep
  (`_wake_clean`, `_wake_dirty`), and the ready blocks: grants served from one and grants of a size in churn that
  found none (`_ready_hits`, `_ready_misses`), blocks that became ready and blocks that went back unused
  (`_ready_made`, `_ready_back`), bytes ready now (`_ready_bytes`). `debug.nvrmfb_vram_fast` shows whether
  `nvfbvramfast=0` is set.

### The ways back

One per change; each restores the 2.9 behaviour of that change only. Boot arguments go on the `boot-args` line and
need a restart. `nvrm610.conf` lines go into your own block at the end of `/Library/GPUBundles/nvmtl/nvrm610.conf`
(between `# --- my settings, kept on update ---` and `# --- end my settings ---`) and take effect when a process
starts: restart the application, or log out and in for WindowServer. Each is reviewed at 3.2: removed if the new path
has proven itself, renewed with a reason otherwise.

| Change | Way back | Kind |
|---|---|---|
| Surface fence (kernel, NVAccel, NVK, plugin) | `nvrmfence=0` | boot argument |
| Page-off wait without the accelerator lock | `nvaccel_pageoff_locked=1` | boot argument |
| Two-plane surfaces in VRAM | `nvaccel_surf_planes=0` | boot argument |
| Apple class layout checks (NVRMFB, NVRMAGDC, NVAccel) | `nvlayout=0` | boot argument |
| Start-up waits and start gate | `-nvrmoldwaits` | boot argument |
| RM work queues | `nvrmworkq=0` | boot argument |
| Answers by context (atomic context) | `nvrmatomic=0` | boot argument |
| Bottom-half thread | `nvrmbhthread=0` | boot argument |
| RC timer | `nvrmrctimer=0` | boot argument |
| Nano timer cancel wait | `nvrmnanowait=0` | boot argument |
| DMA through VT-d | `nvrmdma=0` (with a mapper: the GPU is not started; without one: nothing changes) | boot argument |
| Granted blocks of video memory: no second zero fill, blocks ready for a size in churn | `nvfbvramfast=0` | boot argument |
| Event wait in every process | `NVRM_EVENT_WAIT=0` (every process polls, WindowServer included) | `nvrm610.conf` |
| Channel recovery | `NVK_CHANNEL_RECOVER=0` | `nvrm610.conf` |
| Error report for dropped work | `NVMTL_DROP_REPORT=0` | `nvrm610.conf` |
| Image grace | `NVMTL_IMAGE_GRACE=0` | `nvrm610.conf` |
| H.264 decode on RTX 50 in the decode service | `NVMTL_NVENCVT=0` (the codec library off everywhere) | `nvrm610.conf` |
| 4:2:2 video textures (camera frames) | `NVMTL_YUV422=0` (4:2:2 textures are refused) | `nvrm610.conf` |
| Capture frames on private images: both planes copied back together | `NVMTL_SURFOUT_PLANES=0` | `nvrm610.conf` |
| Capture frames in VRAM: the other plane's tag follows a mark | `NVMTL_PLANE_FOLLOW=0` | `nvrm610.conf` |
| Done tags and holds before scheduled and present | `NVMTL_BUSY_DONE_EARLY=0` | `nvrm610.conf` |
| Photo Booth's preview reflected upright | `NVMTL_PHOTOBOOTH_FLIP=0` | `nvrm610.conf` |
| Shader-writable Managed surface wrappers drawn through a tiled image | `NVMTL_MANAGED_SURFACE_TILED=0` (drawn straight into the linear VRAM import) | `nvrm610.conf` |
| Native OpenGL through Zink on macOS 15.5 and later | `nvzink=0` (Apple's OpenGL over Metal for every application) | boot argument |

`sysctl debug.nvrm_switches` shows which of the boot arguments a Mac carries.

### Not turned on in this release, and why
- `NVMTL_MISALIGNED_BUFFER_COPY` stays off. It binds a copy taken when the command is encoded, so it reads stale bytes
  when an earlier command in the same command buffer, or GPU work still running, wrote that buffer; its write-back on
  completion can overwrite newer data in the same bytes; it costs up to a 4 MB copy per unaligned binding. A stale
  index read where 2.9 read zeros can turn into an out-of-range GPU access. It fixes one read-only case (Photoshop's
  duplicate-layer kernel) and is not safe everywhere.
- `NVMTL_ARGBUF_SAMPLERS` stays off. It changes the code the translator generates for samplers read from argument
  buffers; on by default it changed 3 of 12,446 shaders of the translator's test corpus, and those three have not been
  examined yet.

### Known limits of what is on
- Image grace: a released texture is not freed early when an application reaches its VRAM limit (no release and no
  wait at the memory limit yet). An application that keeps a Metal command buffer uncommitted holds every texture
  released after it until it commits or drops that command buffer, or until 1024 submits have completed (then the
  textures are freed as without the grace, and the log says so). The bindless slot of a released texture still
  follows the older rule (it waits for the submits in flight, not for the command buffers open when it died).
- Event wait: there is no setting that restores exactly 2.9's default (WindowServer sleeping, applications polling).
  To test one application without the event wait, launch it with `NVMTL_PROCESS_ENV=1 NVRM_EVENT_WAIT=0` in its own
  environment.
- With `nvrmfence=0` the driver's surface sync is 2.9's, but the other changes of this release stay on, and NVK still
  opens its idle fence connection in every process (see [docs/SURFACE-FENCE.md](docs/SURFACE-FENCE.md), "The one
  switch").

### For developers
- `./build.sh` builds the whole driver on a Mac with one command: it checks Xcode and the tools, installs what is
  missing after asking (on an Intel Mac the build tools are built from pinned sources into its working directory; on
  Apple silicon they are Homebrew formulas, with the Homebrew of the Mac's own processor, and build.sh itself
  upgrades and removes nothing you have; Python and Rust inside its own working directory),
  fetches every outside source at a pinned commit or checksum (`build/pins.sh`), builds each component as a logged,
  skippable step, and can stage a package from the 2.9 release package (another release, or a file that holds more
  than a release package does, is refused). A package it makes records the version of the
  checkout it was built from (the first heading of this changelog, or `NM_PACKAGE_VERSION`) as the installed version
  that Send logs and the app's update check read, not its base package's, and its headers do not name the account
  that built it. It never uses `sudo` and never installs the driver.
  Needs macOS 15.6 or later and Xcode 26. At commit 3933ff6 it built every component and staged a package with
  Xcode 26.5 on the Intel Mac and with Xcode 26.3 (SDK 26.2, the newest Xcode that runs on macOS 15) on GitHub's
  Intel runner, both named at the top of this section; Xcode 26.0 to 26.2 have not been tried, and on Apple silicon
  it has run only against stand-ins. See
  [docs/BUILDING.md](docs/BUILDING.md) ([en español](docs/BUILDING.es.md)).
- Building the kexts on an Intel Mac, with 2.9's scripts too, made an NVRM.kext that macOS cannot load: NVIDIA's
  makefiles gave the display code an x86-only option because the Mac's own compiler accepted it, and every return
  then jumped to a function that only Linux has. NVIDIA's RM and display code are now compiled with the same commands
  on every Mac, those of the Apple-silicon Mac the releases are built on, and a kext that imports such a function
  stops the build instead of being packaged.
- Building the shader translator stopped on an Intel Mac at its warning ceiling. The release script starts cargo from
  the Rust toolchain's own folder, and the strip that rustc then asks of Rust's `rust-objcopy` aborted for every crate
  it linked: that program finds Rust's libLLVM only when rustup's proxies start it. The script now tells cargo to
  leave that strip out (its own `strip -S` already removed the same debug map), and its check that the dylib names no
  build machine path reads every byte of the file, where macOS's `strings` read only its sections. At commit 3933ff6
  the translator built with no warning on the Intel Mac and built on GitHub's Intel runner.
- The Vulkan loader that `package/vulkan-for-apps.sh` offers to applications is built and installed in a temporary
  folder, and the loader's CMake compiled an `etc` folder inside it into the library as a folder to look in for
  manifests and settings: the library carried the folder's name, looked there on every Mac, and two builds of the same
  source on one Mac had different bytes, with 2.9's script too. The script now gives CMake `-DSYSCONFDIR=/etc`: the
  library still looks in `/etc`, and a library that names the temporary folder stops the build before it is copied
  out. With that option two builds on one Intel Mac had the same bytes.
- With an SDK older than 26.4 (Xcode 26.3 and earlier, the newest a Mac with macOS 15 runs) the plugin's device class
  now declares `supportsPlacementSparse` itself: `MTLDevice` declares it from the 26.4 SDK on, and a build with
  Xcode 26.3 stopped there. At commit 3933ff6 the plugin built with the 26.2 SDK of Xcode 26.3 on GitHub's Intel
  runner. With the 26.4 SDK or a later one nothing changes: that declaration is not compiled, and the plugin built
  with Xcode 26.5 on the Intel Mac had the same bytes before this change (commit 650db45) and after it (3933ff6).
- `tools/switches.tsv` is the registry of every switch, and `tools/test_switch_registry.py` fails when the source
  reads a name it does not have. A switch added from now on cannot be an opt-in or a writable sysctl: the test says
  what it may be instead (an opt-out of a default path with a review release, a test hook that does nothing unset, a
  read-only counter).
- `build/kext_check.sh` compiles every kext source without linking, on a Mac or on Linux, and checks every kernel
  symbol the kexts import against the export lists of macOS 15 and macOS 26. A symbol only Apple's own kexts may link,
  or one only one of the two systems exports, fails the check instead of failing to load on a user's Mac.
- `.github/workflows/ci.yml` runs that check, the host tests and the Mac builds on every push once Actions are enabled
  for the repository.
- `.github/workflows/full-build.yml` runs by hand (one run takes between one and four and a half hours of GitHub's
  Intel Mac): on GitHub's Intel runner it builds the whole driver with `./build.sh` in a new working directory (its
  tools and sources restored from GitHub's cache when it has them), stages a package over the 2.9 release package,
  runs the host tests and checks that nothing was taken from Homebrew. At commit ee7f332 every step of it succeeded.
- `tools/run_all_tests.py` says, after a failed test, when the checkout lacks the history that some of the tests read
  from git (a clone made with `--depth 1`, a clone without the release tags, a tree that is not a git checkout), and
  names the command that brings it.
- NVK's library came out with the same bytes in three builds on the Intel Mac (once with build tools made by hand,
  then with `./build.sh` at commits 650db45 and 3933ff6, which do not differ in NVK's sources or patches), and
  NVRM.kext's binary and the plugin had the same bytes at those two commits. The other kexts, the shader translator
  and the video library were not compared.
- On an Intel Mac `./build.sh deps` no longer needs Homebrew: it builds the missing build tools (ninja, CMake, Python
  3.14 with its OpenSSL, zstd and xz, pkgconf, SPIRV-Tools, glslang, LLVM 22 with clang) from pinned sources with
  `build/toolchain.sh` into the working directory, for macOS 15.0 and later, and checks that each links only macOS's
  libraries and its own. `deps` from an empty working directory took 46.5 minutes on the Intel Mac at commit 650db45
  (building LLVM about 30 of them) and 3 hours 53 minutes on GitHub's Intel runner at commit 96bf32c; it needs 10 GB
  of free space while LLVM builds.
  `NM_TOOLS_FROM` (`auto`, `source`, `homebrew`) chooses; `auto` keeps Homebrew's ready-made builds on Apple silicon,
  a path tried only against a stand-in for Homebrew. On an Intel Mac build.sh does not use Homebrew, even when it is
  installed: with tools from source only the toolchain's tools and your own settings are used, and no `brew` is
  called. With `homebrew` a tool you already have is used as before.
  Homebrew's SPIRV-LLVM-Translator is now taken only with Homebrew's own `llvm`; with any other LLVM the pinned
  translator is built against it, and built again when the LLVM in use changes; a translator built before `deps`
  noted its LLVM is built once more (on the Intel Mac at commit 3933ff6, with every other tool already there, that
  `deps` took 49 seconds).
- Every step of `./build.sh`, its SPIRV-LLVM-Translator build and its pip now get `SDKROOT` set to the macOS SDK of
  the Xcode chosen (your own `SDKROOT` is still not passed on). Before, `SDKROOT` was removed, and Apple's clang then
  searched `/usr/local/include` and `/usr/local/lib`, Homebrew's folders on an Intel Mac, before the SDK: two Intel
  Macs with different things in Homebrew could build different drivers. `./build.sh env` prints it and doctor names it.
  Your `TOOLCHAINS` (it makes xcrun pick another toolchain's compiler) no longer reaches the steps, and neither it nor
  your `SDKROOT` reaches `build.sh` itself: with an `SDKROOT` that named an SDK which was not there, a build stopped
  before its first step. doctor names the way to another SDK: its Xcode, with
  `DEVELOPER_DIR`.
- `./build.sh deps` with Homebrew: a `brew install` that ends with an error only because a file of yours was in the
  way of one of its links (a `python3` from python.org's installer, say) no longer stops it; it warns and goes on.
  A Python formula that `deps` unlinked (so that your shell's `python3` stays the one it was) is unlinked again
  when a later `deps` links it once more as a dependency of another formula. Both have been tried only against a
  stand-in for Homebrew.
- `./build.sh doctor` reads the `VERSION` of a base package given as an unpacked folder and names a release other than
  2.9 at once, instead of the package step stopping on it at the end of the build.
- The git calls of `./build.sh`'s build steps no longer read your global or system git configuration: your hooks, file
  system monitor, commit signing, filters and URL rewrites stay out of them (`fetch` already kept your hooks, file
  system monitor, attributes file and template folder out of its own).
- `./build.sh` builds nineteen components: `nvkbridge` (the native Vulkan runtime, `tools/nvk-bridge/build.sh`, after
  `boost`) and `zink` (native OpenGL, `build/build_zink.sh`, last) are new. Zink is built from Mesa 26.3.0 (commit
  0a6fa60a, pin `NM_PIN_ZINKMESA`), which `fetch` adds to the same Mesa git as NVK's, for macOS 15.0 and later, with a
  call to a newer macOS interface an error. The package step stops when native OpenGL lacks the `nvkbridge`
  component's two libraries, which are its Vulkan driver. Built and checked on Linux only: a darwin cross-build of
  Zink, and syntax-only compiles of the CGL adapter, the GL bundle and the runtime's three Objective-C++ files against
  a stand-in SDK (Zink's two AppKit files were not compiled); neither component has been built on a Mac.
- Native OpenGL's build needs Bison 3.0 or later (macOS's `/usr/bin/bison` is 2.3): `./build.sh deps` builds GNU
  Bison 3.8.2 from its release tarball, SHA-256 checked, on an Intel Mac and installs Homebrew's `bison` on Apple
  silicon. Without one, a build that includes `zink` (`all` and `package` do) stops before its first component's first
  step, naming bison and `./build.sh deps`. Bison's recipe has not run on a Mac or anywhere else.
- New host tests: `tools/test_nvk_bridge.py` (the runtime's interfaces with NVK and the plugin, and on a Mac with
  Vulkan headers its host test built with ASan and UBSan), `tools/test_cgl_dispatch.py` and
  `tools/test_cgl_gld_guard.py` (the dispatch generator's rules and the layout guard's checks, on synthetic input),
  `tools/test_photobooth_preview.py`, `tools/test_surface_drawable_usage.py`, `tools/test_format_capability_init.py`,
  `tools/test_a8_framebuffer_fetch.py` and `tools/test_buffer_tiling_features.py`. Programs that need a Mac with the
  driver (`tools/test_*_gpu.m`, `tools/test_custom_drawable.m`, `tools/test_surface_render_formats.m`,
  `tools/photobooth_surface_probe.m`) say in their header how to build and run them; `tools/run_all_tests.py` does
  not run them, and none has run.
- `NVK_SKIP_PATCHES` for `build/build_nvk.sh` leaves one of the three upstream patches out of an NVK build, to find the
  cause of a rendering fault; such a library says so in its build summary.
- `./build.sh` refuses a working directory, `NULLMOTH_BUILD_ROOT`, `NULLMOTH_RUST_BIN`, `NULLMOTH_CARGO_HOME`,
  `NM_OGKM620` or `NM_OGKM620_PRISTINE` whose path holds a character other than letters (of any alphabet), digits and
  `% + - . / @ _`, and the three release scripts refuse such a path themselves. They put these paths unquoted into the
  commands they hand to the build Mac's shell, where a folder such as `R&D` split a command and could delete a folder
  beside it.
- The commands the plugin, translator and video (nvencvt) release scripts run on the build Mac get the step's
  `SDKROOT`, so Apple's clang and ld no longer search `/usr/local/include` and `/usr/local/lib` (Homebrew's on an
  Intel Mac) before the SDK of the Xcode `build.sh` chose.
- With `NM_MESA_GIT` set, the plugin step stops with the reason when the folder the release plugin is compiled against
  (`<workdir>/build/gpu-src/mesa/include`) has no Vulkan headers, instead of starting and failing on a missing
  `vulkan_core.h` (or compiling against Homebrew's under `/usr/local`).
- What `deps` builds itself (the translator's cmake, the Python environment and pip, cargo's bindgen and cbindgen) no
  longer takes your compiler variables (`CC`, `CXX`, `CFLAGS`, `LDFLAGS` ...), cmake's search paths, `PYTHONPATH` and
  `PYTHONHOME`, the pip options that move or change what it installs (`PIP_TARGET`, `PIP_USER` ...), or `RUSTFLAGS`,
  `RUSTC_WRAPPER` and `CARGO_TARGET_DIR`; every step also loses `PYTHONPATH` and `PYTHONHOME`, and doctor names those
  that are set.
- A download with the wrong SHA-256 is refused with both checksums, the expected one and the one it had. A
  `glslangValidator` or `llvm-config` that fails when asked its version no longer ends `deps`' checks before they list
  what is missing.
- `clean` with a `NULLMOTH_BUILD_ROOT` outside the working directory finishes and names the folders it leaves there,
  instead of stopping halfway. `clean` and `help` work on a system other than macOS too, and the messages say so.
- The git that fetches the pinned sources never asks for a user name on the terminal: a remote that wants one fails
  at once with git's own message.
- After `package`, `./build.sh` prints the three install commands bare and the explanation on a line of its own. Pasted
  into Terminal, whose zsh does not take `#` as a comment, the old trailing `# a dry run: ...` reached `install.sh` as
  arguments, and it refused with "expected --payload directory or no arguments".

## 2.9

2.9 is the last 2.x release for macOS 15 Sequoia and macOS 26 Tahoe. It carries every fix we had for both systems, on GTX 16 and RTX 20, 30, 40, and 50 cards, plus a large set of new features. New features that have not been proven on enough hardware are **off by default** and listed under "Experimental" below: you switch them on one at a time from the 1401 Mac app.

Update from the 1401 Mac app, or download the package below. Restart after installing.

### Fixes

#### Login, desktop, and stability
- Login window on macOS 26 that showed a black screen with only a cursor: the login window's 10-bit frames now reach the panel.
- WindowServer hang after display sleep: a frame with nowhere to go is dropped instead of switching the asynchronous swap off (#470, #353).
- After a GPU loss, apps get a proper "device removed" error instead of a nil command buffer that made WindowServer abort. The log names the cause.
- Login hangs while shaders translate: Core Animation shader variants translate about three times faster (1.6 s to 0.48 s each), new libraries translate in the background once, and recorded variants are warmed into the cache before they are asked for.
- WindowServer keeps its shader cache across restarts (the cache folder is now chosen by a real write, so a sandboxed WindowServer no longer loses it). Settings panes open faster.
- Shader cache entries prove they are whole before they are used.
- Boot guard: the start counter is saved on every change, so a start that panics still counts (#529), and a WindowServer that keeps dying no longer counts as a good desktop.
- A failed mode set at boot no longer kills the framebuffer.
- A stuck GSP fails its call at the first timeout instead of holding the system.
- A texture that owns a view of its image is never moved between memory homes.
- Static samplers are created on the shader's own device (PR #530).

#### Boot, BAR1, and shutdown
- **Shutdown and restart**: the GPU is quiesced and the BAR layout is handed back to firmware at power-off and restart. This is aimed at the boards that would not POST after a macOS shutdown (#488) and at shutdowns that hung on the last frame.
- BAR1 placement is clipped to the limit macOS can map (Legion and other laptops whose BAR1 landed above it), and apertures are staged before any PCI write.
- A BAR missing from the device's memory list is recovered from assigned-addresses or the BAR register (boards without Above 4G / Resizable BAR, #344, #346).
- A firmware BAR1 above 8 GB is resized to 8 GB (16 GB BAR1 stalls, #23, #105, #155). A BAR1 that must grow past 4 GB is placed from the fallback range when other devices already sit above 4 GB.
- The headless deadline starts after the settle delay, and the boot hold is released when no connector has a display (hybrid laptops with the panel on the integrated GPU waited 40 s, #516).
- Bus mastering that firmware leaves on is switched off until the card is started.
- A card the model table lacks is named from its GPU profile instead of showing only a PCI id.
- A40 and L40 added to the device table. The table is checked against NVIDIA's 620.70.06 list.
- Small BAR1 windows: surface requests are rounded to size classes to reduce fragmentation.
- One user-client connection can carry many CPU mappings, which removes a per-mapping connection cost.

#### Displays
- 240 Hz, 300 Hz, and faster internal panels that were black above 60 Hz: the timing range now reaches 2.7 GHz pixel clock and 540 Hz (#429).
- HiDPI at high refresh: the highest refresh rates are kept when macOS lists more timings than fit (180 Hz HiDPI modes, #359), and the head-scaler limit is learned instead of assumed (#360).
- Derived half and quarter refresh rates for 240 Hz and faster panels (#478).
- 4K over DisplayPort-to-HDMI adapters: hardware YUV 4:2:0 for modes RGB cannot carry (general port of PR #459).
- DSC decisions follow the connector's own limit (DisplayPort versus HDMI), which fixes dual-screen flicker on some cards (#77).
- Two identical monitors each get their own registry location, so their colour profiles and identities no longer merge (#509).
- Login-window gamma is applied before login (#413).
- Hot-plug after a boot with no display attached now reaches the idle display ports (#288). A boot that finds no display looks again, 6 times, 3 s apart (#88).
- Mode changes wait a bounded time for the display gate instead of hanging in the kernel.
- Laptop brightness: a start-up request for full brightness no longer overwrites your saved level (#525). Internal panels with no registered backlight get eDP backlight control over DPCD.
- Rotated displays can offer other resolutions (`nvfbrotscale=1`, #138).
- EDID override files and smaller boot rasters are supported (idea from PR #468).

#### Video
- Hardware video encode on RTX 40 (Ada) and RTX 30 (Ampere): NVENC registers on those cards, and in the system decode service. VP9 and AV1 decode added (vendored mickys/nvenc 0.9.5, MIT).
- FFmpeg and other clients that ask for low-latency rate control are accepted (#275).
- Screen Sharing high-performance mode: the encoders and decoders answer the pixel-rate questions the Screen Sharing host and viewer ask.
- HEVC Main 4:4:4 profiles are named for clients that request them (opt-in).
- Hostile-stream fixes in the HEVC, H.264, and AV1 parsers, found by fuzzing.

#### Audio
- HDMI and DisplayPort volume and mute for display audio (opt-in `nmhdavol=1`, #501).
- The audio driver is built from the repository's own sources at release, with its pin table.

#### Apps and shaders
- Roblox and Photoshop compute shaders built with the newest Metal toolchain were refused: the translator now accepts the extra operand AIR 2.9 adds to atomics.
- Apps that build render shaders at start-up and then opened no document (#511): program-scope sampler tables are lowered.
- WebGL terminals that drew no text: texture and sampler reads are kept.
- DaVinci Resolve viewer shader: vectors loaded from struct-wrapped scalar arrays.
- Samplers chosen by a condition or an index are sampled per candidate instead of refused.
- Compute pipelines honour the stage-input descriptor (strides, offsets, formats).
- Pull-model interpolation (`interpolant<T>`), nested argument encoders, threadgroup vector arrays indexed as scalars, pointer-select comparisons.
- OpenGL: binding a texture or sampler no longer formats a string on every draw (#531).
- Compute kernels that write through unaligned buffer offsets get their bytes back (#498, opt-in).
- The accelerator publishes the revision and renderer id fields Apple's accelerators publish (#384, #379).
- Device capability answers (vertex amplification count, 32-bit float filtering, MSAA, texture LOD and others) are decided from what the Vulkan device really reports.

#### Power
- The GPU is boosted per burst of work instead of for the whole life of every app, so it can drop to its idle state between bursts (#522, #500, #314).
- Kernel logging is quieter: per-frame, per-query, and per-client lines appear only when asked for.

### New features
- **NVIDIA 620.70.06 base with a reproducible patch chain**: the kernel parts are built from NVIDIA's pristine open source release with every patch applied by the build, and the build refuses any other tree.
- **NVK on Mesa 26.3.0-rc3.**
- **Frame-rate limiter** per application (`NVMTL_FPS_LIMIT`), and your per-application list survives updates.
- **Driver settings** window in the 1401 Mac app: one catalogue of display and performance settings, with your card's PCIe link and BAR facts. Every change is confirmed.
- **Game Mode** (NullMoth's own): a full-screen game in front raises its own GPU priority and lowers background GPU work; everything is restored when you leave. Off by default, with a menu-bar indicator.
- **Colour controls**: output depth, format, range, and vibrance.
- **Metal features**: Metal 4 counter heaps, GPU-written indirect command buffer ranges, nested argument encoders, GBGR422/BGRG422 textures, framebuffer-fetch emulation for colour-reading pipelines.
- **Vulkan for apps** (opt-in): a Khronos loader built from pinned source, so applications can use the driver's Vulkan directly, including windows through `VK_EXT_metal_surface`.
- **Tools**: `nvmtl-smi` (read-only GPU report), `nvmtl_gpus` (cards, link speed, driver state), `nullmoth_settings` (safe settings editor), `nvmtl-boost` (run-time boost switch).
- **Fan speeds on every desktop**: 1401 gives AMD desktops SMCSuperIO too, with VirtualSMC and its sensor plugins built from source.
- **Ryzen laptops**: an optional power-limit profile (pinned, verified RyzenAdj build with a root-only kernel helper).
- **iPhone and iPad apps**: the Mac app's ARM apps page can take an unencrypted `.ipa` and turn it into a Mac app.

### Experimental (off by default)
The 1401 Mac app has a new pane, **Experimental drivers and features**, with a switch per item and Select all / Deselect all. The 1401 builder now places NullMoth's own EFI drivers that fit your PC into the EFI switched off, and this pane turns each one on or off. Please report what works.

- HDR10 and 30-bit output (`nvfbhdr=1`, `nvfb10bit=1`), variable refresh rate (`nvfbvrr=1`).
- Hardware ray tracing on the RT cores (`NVK_HWRT_QUERY=1`), including an 8-wide encoder and curve geometry.
- Hybrid laptops: render on NVIDIA and present on the integrated GPU's panel (Intel and AMD panel drivers, `nmhybrid`), per-app GPU policy.
- Idle GPU deep sleep on laptops (`nvrtd3=1`) and idle PCIe link power saving (`nvrmaspm=1`).
- Notebook power budget (`nvrmtgp=1`), power cap.
- Keyboard backlight for ASUS, Lenovo, Dell, and MSI laptops (`nmkbl=1`).
- Display audio volume (`nmhdavol=1`).
- DDC/CI monitor control (`nvrmfbddc=1`), surround (several displays as one, `nvfbsurround`), hot-plug polling for cards that send no display-change event (`nvfbhotplugpoll`), keep the frame on screen across mode changes (`nvfbkeepfront=1`).
- Multi-GPU: one card per process (`nvmultigpu=1`).
- Sparse textures, tile shading, vertex amplification, GPU-encoded indirect command buffers, 4:2:2 video textures, one-module function constants.
- Full shutdown of the resource manager at power-off (`nvrmshutdown=full`).
- Driver off on a Resizable BAR card with the firmware screen kept (`nvoffbar=1`).
- H.264 hardware decode in the system decode service on RTX 50 (green video in Safari; opt-in while we test it).

### Security
- A user client's token table grows as needed instead of pinning about 1.8 GB of kernel memory on first use.
- A use-after-free window in the accelerator's page-off wait is closed.
- DisplayPort AUX and EDID writes from user space are range-checked.
- Every file the driver writes under a fixed name is created without following links. Diagnostic switch files in `/tmp` count only when root made them.
- The GPU policy file and the experimental-driver source folder are honoured only when root owns them and nobody else can write them.
- The 1401 Mac app's root actions resolve their package against the app's own pinned list, never from the page.
- Firmware calls added in 2.9 check their own opt-in where they are made.

### Package and licensing
- NVIDIA's GSP firmware is no longer shipped. The installer fetches it from NVIDIA's own driver package (pinned SHA-256), and a 1401 stick carries that package for offline installs.
- The package no longer contains any third-party closed compiler components. Shader decoding uses NullMoth's own out-of-process decoder built from upstream LLVM.
- Every file in the package is listed in `SHA256SUMS`; the installer and the release gate refuse an unlisted file. Every program and library is signed.
- Licence texts and NOTICE credits added for every component and every community idea used. `docs/PROVENANCE.md` says what ships, from where, and under which licence.
- All compiler warnings fixed across kexts, plugin, translator, NVK, and apps, and every build stops if warnings rise.

### 1401 (Mac and Windows apps)
- Arrow Lake: a corrected MADT when firmware lists only x2APIC processors (#252, #64); extra processor devices hidden on HX laptops (#141, #110).
- AMD laptops: ALIB fix built per machine, 0xFF000000 MMIO whitelist, SSDT-PLUG tables off, relocation block when the installer needs it.
- DSDT repair: each deferred Scope is guarded, so macOS no longer drops the whole DSDT on some Lenovo and Dell laptops.
- Coffee Lake-H laptops: the firmware page every reported panic named is reserved (#328, #451).
- Resizable BAR: an installed EFI never resizes the GPU window above 8 GB, and the BIOS guide no longer contradicts itself.
- VMD and unusable internal drives are planned for (BIOS path per maker, reversible Windows preparation, external-drive option) instead of stopping the build.
- Full macOS installer option (verified, resumable download) for PCs where the online Recovery install fails. Finish a new macOS 26 install from Recovery when setup cannot draw (#86).
- Apple ID: every PCI Ethernet controller is marked built-in so it is en0.
- USB map import (your own UTBMap.kext), new serial, own recovery image, and own config.plist options.
- After a macOS update that left the driver parked, the Mac app says so (#484), retries the kernel collection build, and turns the driver back on.
- Updates keep your hidden auxiliary boot entries (#371). Vault is set to Optional (#153).
- Send logs: no more hang on "Waiting for your Mac password" (#369, #373, #525); flicker samples, display traces, WindowServer watchdog samples, and failed shaders are included; a per-frame log line can no longer fill the kernel log (#16).
- Clearer stops in the writer: antivirus removing built files, Windows "Access is denied" on the stick, Apple download failures, installer download errors.
- Stick logs name the board and CPU. The first-login report says whether the accelerator offered its Metal plugin.
- Intel Xe support updated (WhateverXe 0.8.0, 60 GPU ids). Community drivers reviewed against their licences.
- AMD/Intel side: H.264 and HEVC decode through Vulkan video where macOS has no hardware decoder, Mesa 26.3.0-rc3.

### Known limits
- Pre-GTX 16 cards (Maxwell, Pascal, Volta, Kepler) are not supported; the installer says so.
- Experimental items are unproven across cards. Turn on one at a time.
- Cyberpunk 2077 interior lighting on first launch is unchanged from 2.8.

Thanks to WatchAndyTW (DaVinci Resolve viewer shader), backdash756 (YUV 4:2:0 over DisplayPort-to-HDMI, PR #459), GeekAudio (BAR1 placement, EDID overrides), and steve12311 (static samplers, PR #530), and to everyone who sent logs, issues, and pull requests. Donations help keep this going.

## 2.8.1

- Updates no longer fail when GitHub's downloads are down. The Mac app now falls back to a copy of each release on nullmothsystems.com when a GitHub download fails, for both driver updates and its own updates. 1401 for Windows does the same for the driver package it puts on the stick. Every file is checked against the release's SHA-256 list and the NullMoth release signature, wherever it came from, and a file that differs by one byte is refused.
- The driver itself is unchanged from 2.8 (same kernel extensions, Metal plugin and Vulkan driver); only its version number moves so the app and the driver keep the same version.

## 2.8

- NVIDIA 620: the kernel driver now runs NVIDIA's 620.70.06 open kernel modules (was 615.78.08) with their GPU firmware. Two new cards come with it: RTX PRO 5500 Blackwell and RTX Spark N1X (249 device ids in all).
- NVIDIA 620: the CPU mapping hardening from 2.7.2 is carried over to 620's new mapping model. A system-memory mapping is now limited to the length the driver validated, tied to that mapping and its list, and a re-bind or close can no longer leave a mapping behind.
- NVIDIA 620: the display driver leaves the colour format to NVIDIA's modesetting, so a display mode that offers only YCbCr no longer comes up black.
- If /Library is not mounted yet when the driver starts, it loads its GPU firmware from a fallback copy.
- DLSS 4 / 4.5 (optional, RTX 20 series and newer): DLSS Super Resolution runs behind the MetalFX temporal upscaler, so a Mac game that uses MetalFX upscaling gets DLSS on an NVIDIA card. DLSS Frame Generation runs behind the MetalFX frame interpolator and takes its sizes and the game's real motion vectors from the upscaler the game attached. DLSS Ray Reconstruction runs behind the MetalFX denoised upscaler on macOS 26.
- DLSS files: the package carries no NVIDIA DLSS code. With the optional step on, the installer downloads NVIDIA's own files on your Mac, refuses any file whose SHA-256 differs from the pinned one, and compiles the DLSS GPU kernels for your card (about two minutes). Run it with `sudo env NULLMOTH_DLSS=1 ./install.sh`. RTX 20, 30, 40 and 50 series cards are covered. DLSS never runs inside WindowServer, and its data stays in a private per-user folder.
- Metal 4: command queues, command buffers, encoders, argument tables and the Metal 4 compiler work over the driver's Metal 3 objects, and the device reports the Metal 4 GPU family. MetalFX's Metal 4 upscaler and frame interpolator are served by DLSS.
- Games with ray tracing and frame generation no longer run out of video memory and crash: large automatic heaps (256 MB and up) get memory in 64 MB pieces as they are used instead of all at once. A 2 GB heap the game had made held 22 KB of data.
- Freezes in long game sessions are gone: freeing or reusing an image no longer scans every image the app ever made (hash index).
- A texture copy between two size-compatible formats is performed, as Metal allows, instead of being refused.
- Drawing into an attachment whose channel order differs from the pipeline's (BGRA into RGBA) works the way it does on Apple GPUs.
- Resources in a heap stay alive until the GPU work that uses them has finished.
- Less CPU time per draw: parallel render encoders record strided vertex buffers and heaps directly, and a buffer's GPU address is returned without a lock when it is already known.
- Fewer stalls: the Vulkan layer carves command memory from shared 1 MB slabs and reuses it across command pools instead of opening a kernel connection per pool.
- Ray tracing in Cyberpunk 2077: the game offers ray tracing only when the reported working set is above 6145 MB. On a card with at least 7.2 GB of memory the driver answers 6200 MB while ray tracing is available; a smaller card keeps its own answer, so it is never told it holds more than it has.
- Cyberpunk 2077 (ARM apps translation) is told macOS 27 so it offers MetalFX frame generation, which the driver provides on macOS 15 and 26. Measured on an RTX 5060 at 1080p, all ray tracing on, DLSS upscaling and frame generation: 85.84 fps average, 68.18 fps minimum (was a crash, or about 47 fps without ray tracing in 2.7.2). Geekbench 7 Metal on the same card: 116,053.
- Shaders: an atomic on an unindexed local array works, and translation is faster (each shader is parsed once).
- Managed no-copy buffers of every size get a copy in video memory.
- OpenGL: the uniform buffer reads its slot through the handle check.
- If Cyberpunk 2077 had ray tracing or frame generation greyed out or off before, turn Ray Tracing and Frame Generation back on once in its graphics settings: the game remembers its own earlier answer.
- Known issue: some interiors in Cyberpunk 2077 can render too bright on the first launch after a restart. Quit and launch again. A fix is planned for 2.8.1.

## 2.7.2

- Apps built for macOS 26 that read a pipeline's required threadgroup size no longer quit with an "unrecognized selector" error. Compute pipelines report the size they were created with, and a required size also sets the pipeline's maximum threads per threadgroup.
- The depth bounds test works. Before, setting depth bounds on a render encoder did nothing, so pixels that should have been skipped were drawn. Pixels are now skipped when the depth already stored in the depth attachment is outside the bounds, as on Apple GPUs.
- Texture views can be made from a texture view descriptor (macOS 26), with a pixel format, mip levels, slices, and a swizzle.
- Setting a colour attachment map on a Metal 3 render encoder no longer stops the app. As on Apple GPUs, the map has no effect on a Metal 3 pipeline.
- Fragment barycentrics are reported only on GPUs whose Vulkan driver provides them, so an app never builds a pipeline the GPU cannot run. On AMD GCN 3 and 4 the Vulkan driver now uses half-precision arithmetic where the hardware has it.
- The 2.x driver is built for macOS 15 and macOS 26. On any other macOS version it creates no Metal device.
- AMD and Intel GPU driver (test program, opt-in): AMD GPUs no longer stop at start-up after "register mmio size" (a PCI bridge lookup could loop forever). Safe start-up for RDNA 2, 3 and 3.5, GCN 1 to 3, Polaris cards macOS has no driver for, and Vega APUs. AMD GPUs that Apple's own drivers already run are left alone.
- Security: fixed races in the NVIDIA kernel driver that let a logged-in user crash the Mac or corrupt kernel memory by binding, mapping, and closing GPU connections at the same time.
- Security: the display driver no longer publishes kernel addresses where any app can read them.
- Security: GPU profiling and channel priority now need an administrator. They were open to every user.
- Security: a setuid or root process ignores the driver's environment settings, and a release build only loads a library named by one from a root-only folder.

## 2.7.1

- OpenGL apps: setting the same render pipeline again inside an open render pass is skipped, so draw-heavy OpenGL apps spend less CPU per draw.
- ARM apps: the installer keeps /Library/NullMoth readable, so the ARM translator can be reached without the `sudo chmod 711 /Library/NullMoth` workaround.
- Known issue: Cyberpunk 2077 can still render interiors too bright on some launches. Quit and launch the game again.

## 2.7

- ARM apps (opt-in, experimental): an optional translator for Mac apps built only for Apple silicon. It compiles an app's ARM code into Intel (x86-64) code ahead of time, so the app can run on Intel and AMD hackintoshes. Turn it on in 1401 › ARM apps (opt-in); on Windows, tick Also set up ARM apps (opt-in) on a fresh install. Your original app is never changed, and Turn off removes the translator and every translated copy.
- ARM apps works today for command-line tools (lz4, xxhsum, cmake and z3 give byte-identical output to Apple silicon). Apps with windows are experimental; Go, Flutter, Java/Electron and SVE video codecs do not work yet, and games are not supported in 2.7. It needs a CPU with AES instructions and the Xcode Command Line Tools.
- macOS 26 Tahoe has sound. Tahoe ships no HD Audio driver, so PCs on it had no output device at all. The driver package now carries NullMoth's own HD Audio driver and installs it on Tahoe when your PC has an HD Audio controller: onboard Realtek and other codecs (rear and front analog) and NVIDIA HDMI/DisplayPort audio. macOS 15 keeps Apple's own driver and is not touched.
- If Logic Pro, GarageBand, or MainStage says "Selected Driver not found" after updating, open its Settings › Audio and pick your output again. The old device was Apple's, and it no longer exists.
- Kernel panics fixed. The driver kept a saved interrupt state inside a shared lock object, which macOS reported as "element modified after free" and which could end in a panic. The state now stays with the caller, as NVIDIA's code expects.
- Games that run several compute passes at once no longer hit random GPU faults (FAULT_PTE / FAULT_PDE) or show garbage lighting. Concurrent compute work and render passes are now ordered correctly.
- Resources declared with useHeap: and residency sets stay alive until the GPU is done with them. Before, a heap could be freed under work that still used it.
- Freed buffers, images, and views keep their GPU address until the work that was in flight when they died has finished.
- Long sessions no longer run out of GPU connections: the command-pool cache is bounded.
- The memory a game is told it may use leaves a reserve on the card, so big games run out of VRAM less often.
- A shader that uses a heap binding its heap was not created with is refused instead of running into a GPU fault.
- Metal ray tracing: acceleration structures build on the GPU, instance transforms honour the matrix layout the app declares (before, every column-major transform was wrong), builds are thread-safe, and VRAM is checked before a build.
- Bindless argument-buffer heaps carry storage images and texel buffers, so a shader can write a texture it found through a heap.
- 10-bit BGR10A2 textures can be written by compute shaders (HDR and 10-bit output render correctly).
- sRGB images keep their storage use through a compatible UNORM view, so writes to them land.
- A render pipeline that does not match the open render pass restarts the pass with its attachments kept, instead of the draw being dropped.
- Indirect compute dispatches prepare the textures declared with useResource:usage: first.
- Shader translation: make_filled_simdgroup_matrix, barrier scopes, byte-addressed threadgroup memory, pointer phis, device-address arithmetic, and pixel-coordinate sampling now translate correctly.
- Functions that call visible functions translate once their callees are known.
- Application profiles: the driver applies per-application settings and shader fixes for one named application, in that process only, with its own shader cache. Cyberpunk 2077's light clustering now renders correctly at about 40 fps on an RTX 5060.
- GPU compute: convolution kernels use the tensor cores, with a speed gate that keeps Apple's MPS kernel wherever ours is not clearly faster.
- Shader compiler: small per-thread arrays stay in registers, plus better scheduling, conversion, and load handling.
- Compute pipelines skip buffer bounds checks (graphics keep them), and small private buffers and images share VRAM blocks.
- Less CPU per draw and dispatch: binding tables in fixed arrays, fresh system memory is not zeroed twice, and buffers are parked for reuse.
- Diagnostics that cost speed are compiled out of the release build.
- Known issue: Cyberpunk 2077's first launch after a restart can render interiors too bright while the game streams its files in. Quit and launch the game again.

## 2.6.6

- Launchpad and Chrome icons are no longer black. 2.6.5 changed how textures made from an IOSurface with no stated use were set up, and shader writes to them went nowhere (PR #461, thanks Geek).
- Logic Pro no longer crashes when it opens the Dolby Atmos mixer. A texture upload reached the GPU with no image behind it; the copy is now refused, and the log names the texture so the cause can be found.
- QQ: avatars keep their colour (a clear that finished late could wipe a new upload, PR #458, thanks Geek) and SVG toolbar icons draw (the shader translator refused ANGLE shaders with an empty inline-assembly statement, PR #457, thanks GeekAudio).
- Hades menu text draws: the shader translator kept a pointer read through a branch merge (PR #466, thanks mickys).
- Games that ask for rasterization-rate-map coordinates, such as Darkest Dungeon II, get their shaders: the driver reports that it has no rate maps and the translator passes those coordinates through unchanged (PR #469, thanks mickys and Geek).
- A failed buffer-to-texture copy names its format, sizes and region in the log (PR #465, thanks mickys).
- The installer notes when the boot-args have no AMFI argument. Without one, WindowServer refuses the Metal driver and the desktop comes up with no Metal device.
- Tahoe PCs whose integrated GPU drives a screen no longer stop at a black screen with a cursor: a GPU macOS no longer accelerates does not count as the one showing the desktop, so WindowServer starts.
- macOS Sequoia 15.0 to 15.4 can install the driver again. The Vulkan library in 2.6.2 to 2.6.5 was marked as needing 15.5 though nothing in it does.
- Security: the driver package is signed, and the Mac app installs a driver update only with a valid NullMoth signature. An installer rollback no longer reuses a firmware folder another account could have placed in /Users/Shared.

## 2.6.5

- WebGL pages with shadow-casting lights draw their shadows: when a page's shadow helper read its sampler through ANGLE's texture table, the shader translator refused the shader, and a second light's shadow map was read as empty (#449, thanks Luminous418).
- WindowServer does less work per frame: compositor draws no longer rewrite descriptors for the framebuffer-fetch set (PR #439), and command buffers whose completion slot was reused are recognised instead of shutting down the pool (PR #446). Drawables are presented on their own queue, which ends a white-window hang with the Metal HUD on (PR #445). Screen recordings keep their frames in order (PR #438). Thanks GeekAudio (mickys).
- Photo Booth's preview is no longer black (PR #433, thanks daggersinmybackk).
- Vivaldi, Opera and Arc get Metal through ANGLE, found by the libraries the process loads (PR #450, thanks SameerAlSahab).
- Shader translator: depth gather-compare (Civ VI shadows, PR #447), sampler choices across compare chains (PR #448), cube samples through 2D-array views (Stellaris, PR #434), Apple's GL helpers inlined (PR #435). Thanks GeekAudio (mickys).
- Plugin: Apple's ARGB8 texture format for sampling and uploads (Homeworld, PR #440), depth texture types and sizes reported correctly (Northgard, Dome Keeper, PRs #441 and #432), array lengths kept in struct reflection and encoders return their command buffer (Hades, PRs #437 and #436), block-aligned compressed mip copies (Cult of the Lamb, PR #444), uploads ordered after GPU reads (PR #442), and a trace-ID lock conflict removed (Soulstone Survivors, PR #443). Thanks GeekAudio (mickys).

## 2.6.4

- three.js scenes with two or more shadow-casting lights no longer lose their WebGL2 context in Chrome, Brave and other browsers: the shader translator refused a shader's array of samplers (#426, patch by Luminous418). Each sampler in the array now reaches the shader with its own state. Known: WebGL shadow maps still draw the whole shadowed surface dark, with one light or several (it did in 2.6.3 too); that is the next fix.
- Arrays of textures read the right texture: every element past the first read the first one, whether the index was fixed or computed in the shader. Shadow maps, texture atlases and material arrays in games and WebGL pages were affected.
- Machinarium, and other apps whose shaders were built with older Metal tools, draw their scenes: the translator did not recognise a sampler written directly into the shader code, and refused the shader (patch by daggersinmybackk).

## 2.6.3

- Infuse and other players that scale video with Apple's Lanczos filter play again: the shader that filter uses was refused, and the app stopped with "Unable to get MPS kernel lanczosVerticalScale" (#405). Its output now matches a real Mac's pixel for pixel.
- Geekbench's GPU benchmark runs every test, including the machine-learning ones that use depthwise convolution (#377, PR #385, thanks GeekAudio and Geek).
- Unity WebGL games and other web pages whose shaders pass a buffer to a helper function render again (#406). Chrome no longer loses its WebGL2 context on pages that write only to textures (#419, patch by eisda).
- DaVinci Resolve no longer stops at startup on a GTX 1660 Ti: the shader translator read debug notes in Apple's shader code as part of the instructions (PR #418, thanks fayzetwin1).
- llama.cpp gets correct results instead of zeros (#401).
- Dragging windows and copying between apps no longer stalls on cards with a small 256 MB BAR1 window: surfaces keep the memory they were given between copies and are read back directly (#368, #399, patch by mrtzygt). A setting you put in your own block of nvrm610.conf now wins over the shipped value.
- Window dragging no longer stutters, and a crash when a window closed during a copy is fixed (#251, #340, #412). The Dock and Control Center blur is smooth instead of blocky (#363). HiDPI "looks like" sizes appear in Displays (#354). Scrolling in Chromium apps is smoother, and System Settings panes open faster. These come from watchthelight's NvGfx (thanks watchthelight).
- Display rotation in System Settings (#230, #307, #350, #355, PR #398, thanks razvanccr).
- HDMI and DisplayPort audio follows the right display when several are connected, and comes back after a mode change (PR #366, thanks Ersiter).
- A laptop's built-in screen keeps the brightness you set across restarts and power changes (PR #312, thanks Ersiter).
- Activity Monitor shows how busy the NVIDIA GPU is (PR #380, thanks GeekAudio).
- Boards whose firmware gives the GPU no 64-bit window keep BAR1 where the firmware put it, and a BAR1 of 512 MB or more that was not moved can flip (PRs #408 and #409, thanks mischa85).
- Microsoft Edge and other Chromium browsers get Metal through ANGLE by name (PR #404, thanks DoveKi).
- Minecraft shader packs BSL and Complementary work, and OpenGL apps use uniform buffers (PR #343, thanks Dat 'Del4y' Nguyen).
- The shader translator is faster (PR #187, thanks modest-tao).

## 2.6.2

- The same driver as 2.6.0, released with fixes to the 1401 apps for Mac and Windows.

## 2.6.1

- The same driver as 2.6.0, released with the redesigned 1401 apps for Mac and Windows.

## 2.6.0

- An update of the driver takes one restart. macOS shows a notice that the extensions were updated while the installer replaces them, and the installer used to answer it with "allow the extensions, then reboot once more"; the new driver already loads on the first restart (measured: every extension approved, none held back), so on an update it now says one restart is enough. A first install still asks you to allow the extensions once.

## 2.5.5

- Discord videos play again. 2.5.4 stopped registering the hardware video decoder in every sandboxed process, and Discord decodes inside its own sandboxed GPU process, so its videos did not play at all. Only Apple's sandboxed apps (Safari, QuickTime, Preview) now hand decoding to macOS's video decoding service; Chromium and Electron apps decode in their own process as in 2.5.3.
- Remote-desktop apps such as UU Remote show the picture: the hardware H.264 and HEVC encoders marked every keyframe "not a sync frame: false" where Apple's encoders leave the mark out, and apps that look for the mark's absence never found a keyframe on every card generation (#388, #358, thanks a1461750564 and sherves for measuring it). The encoders for the newest cards also load on macOS 15.0 to 15.4 now; they asked for 15.5.
- Screen Sharing and other screen-capture apps show the screen instead of black: a texture made from a buffer could only be viewed as its own format and its sRGB twin, and screen capture views a BGRA surface as RGBA when it scales or converts.
- Brave and Microsoft Edge get Metal through ANGLE the way Chrome does (PR #381, thanks SameerAlSahab; Edge tested by sherves).
- On a card with a small BAR1 window, NVAccel no longer logs every refused allocation: a third of all log lines on an idle desktop (#387, thanks wwwljxw). It logs the first eight and then every 256th with the running count, as the framebuffer does; `debug.nvaccel_vm_refused` keeps the exact total.

## 2.5.4

- Desktops whose only graphics card is the NVIDIA card no longer loop on a black screen when the card's display does not come up: WindowServer used to stop at start with "Failed to create MetalDevice" every ten seconds until the startup guard switched the driver off. It now starts on the firmware's framebuffer, the desktop appears without acceleration, and the first-login report sends the logs that show why the display did not come up.
- Discord gets Metal through ANGLE the way Chrome does (PR #348, thanks lukeitsback).
- Safari, QuickTime, Preview and other sandboxed apps decode H.264 and HEVC through macOS's video decoding service instead of failing inside their sandbox (PR #349, thanks lukeitsback).
- The installer accepts a /Library folder whose access-control list only takes access away, and names any entry that would let another account write there.
- When a kext you added to /Library/Extensions stops macOS from building its kernel collection, the installer names that kext and says how to move it, instead of only "test kernel collection build failed".
- The package carries the licence texts of everything in it (NVIDIA's firmware licence, the open kernel modules' licence and Mesa's), installed to /Library/NullMoth/licenses; the uninstaller removes them.

## 2.5.3

- NVIDIA's driver options given as `nvregdwords=` no longer stop the startup. From 2.5.0 to 2.5.2 any option set this way ended the boot with a kernel panic in the driver; the options now reach NVIDIA's registry, as the 2.5.0 notes describe.
- HDMI and DisplayPort audio, stated precisely: on macOS 15 the driver gives AppleHDA the display's audio link, and the display shows up under Sound > Output. macOS 26 removed AppleHDA, so on macOS 26 this package does not provide display audio by itself.

## 2.5.2

- HDMI and DisplayPort audio: a connected display shows up under Sound > Output with no setup. The driver publishes the display-to-codec link AppleHDA needs, keeps it current through hot-plug and monitor swaps, and lets a machine whose display audio is on another codec pin name it (PR #284, thanks Hi-Jiajun; PR #313, thanks Ersiter).
- Laptops whose NVIDIA bring-up takes longer than 40 seconds keep their desktop: the startup hold waits while the GPU is still coming up, up to 120 seconds (`nvrmholdcap=<seconds>` sets it) (PR #317, thanks watchthelight).
- Machines with several NVIDIA cards can pin the desktop to the card that drives the boot screen, and the choice survives updates (opt-in, see docs/AUTO-PRIMARY-GPU.md; PR #247, thanks is-pan).
- High-refresh displays: `sudo sysctl debug.nvrmfb_flip_latch=2` checks each flip at the next present instead of waiting for it, giving back about half a refresh period per frame (opt-in; #300, thanks xiaoming-crypto).
- The system log is quiet again: the driver logs failed GPU allocations, not every successful one (#242).
- Apps using Vision barcode detection (QQ and others) and Geekbench's Background Blur no longer sit at full CPU for minutes compiling one MPS kernel (#334, #111). That kernel still does not run on the GPU yet.


## 2.5.1

- Apple TV plays video again, and dragging windows no longer kills apps like Telegram: CoreAnimation's extended-range video formats (BGRA10_XR / PBGRAXb and BGR10_XR) are supported; the driver refused them, so video and some window pipelines failed (#309, #280).
- Shader writes to sRGB textures land, encoded as on a real Mac; they were silently dropped, which broke Blender's edit-mode viewport (#321).
- Scaled and HiDPI desktop modes work: a desktop size different from the display's timings is scaled by the GPU's head scaler instead of being refused (PR #273, thanks Leaf-lsgtky and lsCoding666, #221).
- Faster window updates: CPU-drawn surfaces upload from their own pages through the copy engine (PR #296, thanks mickys).
- GPU temperature in iStat and other monitors on cards whose sensor register answers (tested on Ampere; PR #323, thanks GeekAudio).

## 2.5.0

- Built on NVIDIA's 615.78.08 driver (from 610.57.04): NVIDIA's own kernel driver, display code and GSP firmware for every card, with all of NVIDIA's fixes since 610, including the blocky corruption in The Last of Us Part II.
- New cards: NVIDIA T400, T500, T550, T600, T1000 and T1200 (desktop and laptop), and the second RTX 5070 device ID.
- The GPU's identity now comes from the PDI NVIDIA stores in the card's PCIe configuration space, the way NVIDIA's driver reads it; older boards fall back to the previous method.
- NVIDIA's driver options work on macOS: the boot-arg `nvregdwords=Key=Value;Key=Value` is NVIDIA's `NVreg_RegistryDwords`, read by NVIDIA's own parser. `nvregdwords=RmDisableDisplayGlitchPerfLimit=1` is NVIDIA's new lower idle power with several monitors (it can glitch the display briefly when clocks change).
- 12-bit HDMI deep colour: the boot-arg `nvmaxbpc=12` allows it on displays that report it. The default stays at 10 bits, as NVIDIA ships it.

## 2.4.4

- Hardware H.264 and HEVC encode on every GTX 16 card: GTX 1650, 1650 SUPER, 1660, 1660 SUPER and 1660 Ti use the same encoders as RTX 20.
- HEVC encode on GTX 16 and RTX 20 cards keeps going past the second frame; it stopped with an error from frame 2 on.
- Laptops whose screen is on the Intel or AMD GPU offer the NVIDIA Metal device about 8 seconds after startup instead of 60, so apps opened at login see it too.
- WindowServer no longer crashes in the Vulkan driver when a binding holds a handle that cannot be a live object: the slot gets a placeholder and the log names it. Two RTX 3060 machines restarted WindowServer a few seconds after each login on this.
- An app no longer freezes for good when one finished GPU job is missing its completion mark (#322): Terminal and iTerm2 printing fast stopped redrawing and taking input until force-quit.
- Shaders that call functions through a visible or intersection function table load again; they returned nothing since 2.3.6 (PR #318, thanks watchthelight).
- World of Warcraft starts again: since 2.3.6 it showed a black screen and "unable to start up 3D acceleration", and once past that the terrain lighting shader faulted (PR #304, thanks razvanccr).
- Apps built on wgpu (Tern and others) start again: a fragment shader that clears the padding of a local struct failed to translate and the app stopped at launch (#330).

## 2.4.3

- Fix a desktop freeze followed by a kernel panic ("thread_call group 'high' reached max thread cap") a few seconds to a few minutes after logging in (#295). The display driver started a new kernel thread for every vblank and every cursor move while an earlier one was still waiting; each of its callbacks now runs one at a time.
- Metal apps translate a shader they have already seen without hashing it again: the digest kept on the translated text was never reused (PR #294, thanks watchthelight).

## 2.4.2

- Fix the black screen and the startup that stops on the verbose text after updating to 2.4 or 2.4.1. The display driver checks for a part of the display interface that the 2.4 and 2.4.1 core driver was built without, so it refused to start and the screen never moved off the startup text. 2.4.2 builds the core driver with it again; nothing else changes.

## 2.4.1

- Fix a black screen after updating to 2.4. The 2.4 package carried a framebuffer kext built from an outdated copy of its source, missing the display code the earlier releases had; 2.4.1 ships the complete framebuffer with the 2.4 changes.

## 2.4

- Hardware H.264 and HEVC video encode on RTX 20 (Turing) and RTX 30 (Ampere) cards, selected automatically per card; earlier builds used the hardware encoder on RTX 50 only.
- Security hardening: caller values are bounded, each client is serialised, and kernel addresses, registers and firmware stay private.
- macOS 26: the system icons and the glass menus and panels draw correctly again.
- Install from a root-owned copy of the package, with the previous driver kept as an automatic fallback.
- The kernel driver reads the power source (battery or mains) and reports it to the GPU firmware.
- Clear a stale shader cache left by an earlier build, which could show a black screen at login after an update.
- A boot counts as good once the desktop is up.

## 2.3.6

Desktop and windows
- Dragging and scrolling windows: a surface whose video memory had been set aside is put back before its update is
  recorded, so the driver no longer copies the whole window back to system memory on every frame (PR #245, thanks
  HighDelay; #253 measured the same readback on an RTX 5070 Ti at 4K)
- Moving the mouse no longer stalls the compositor: the cursor position is applied on its own thread instead of under
  the system's cursor lock, and OpenGL apps wait for the display's real refresh rate instead of 60 Hz on faster
  panels (thanks pengubow)
- Core Animation passes that read the colour attachment with framebuffer fetch off now read a defined image instead of an
  unbound descriptor, which could put garbage on screen inside WindowServer (PR #270, thanks watchthelight)
- Text and badge masks drawn into A8 targets land in the right lane, and Dock badges show their numbers again (PRs #271
  and #274, thanks watchthelight)

Apps and games
- OpenGL: Minecraft's vertex shaders translate, the draw path no longer allocates per draw, texel-buffer descriptors and
  unchanged bindings are reused, depth bias and per-instance step rates take effect, samplers work inside argument buffers
  (thanks HighDelay)
- Chrome and Chromium use Metal through ANGLE by default, so their GPU process starts; Electron apps stay opt-in with
  NVMTL_ANGLE_METAL=1, and NVMTL_ANGLE_METAL=0 turns it off everywhere (PR #263, thanks Hi-Jiajun)
- Shaders with `[[visible]]` function references that function constants turn off are no longer refused, which lost
  pipelines in the login window, Spotlight, icon services and the Dock
- Metal reports the card's whole video memory as its working set, as a Mac does (8 GB cards said 5.9 GB, 12 GB cards 8.9 GB)

## 2.3.5

Stability
- The login screen no longer stays black for a minute after the display comes up. The driver waited 60 seconds before
  letting the compositor's frames reach the panel, on the assumption that the screen was already showing them another
  way; on macOS 26 it was not, so every boot sat on a black login screen for exactly that minute

## 2.3.4

Pre-release for the early-updates testers; it becomes the general release only after every GPU generation passes the
field gate.

Stability
- Window dragging, Launchpad and games stutter less: each screen update waits at most one refresh period for the
  previous one, instead of a fixed tenth of a second; the wait wakes on the display's own event
- macOS Sequoia screenshots and window captures: the capture ordering fix from 2.3.2 returns, and it now follows what the
  installed kernel driver supports, so a Metal driver and a kernel driver from different versions never mismatch again
- Turing (GTX 16 and RTX 20): the login screen no longer stays black with a cursor when the first frame has not been
  drawn yet; the driver only shows video memory the Metal driver has written
- Turing: the ten-second start wait the installer used to add is now the driver's own default, so a laptop card or a Mac
  whose boot arguments were rewritten gets it too (a value you set yourself still wins)
- Laptops with a hybrid display: the driver no longer copies the boot screen from another card's memory, which could
  stop the Mac during startup. When WhateverGreen is present next to the driver, the installer notes the gfxrst setting it needs
- A Mac reset during the driver's own start-up wait (the ten-second Turing settle, the 100-second wait when the card's
  memory window cannot be moved) no longer counts toward switching the driver off. Two such resets used to leave a
  working Mac on the fallback driver, reported at the next login as "driver loaded: no"
- A GPU that is busy is not a GPU that is gone: the Metal driver keeps waiting for a late frame instead of disabling
  itself for the rest of the process. In WindowServer that disablement meant every later command buffer was refused
  and the session restarted ("Failed to get a MTLCommandBuffer")
- Every kernel driver now carries the package version. A Mac that still runs an older kernel driver after an update
  (the kernel collection was not rebuilt) is named in Send logs and in the "reached the desktop" report instead of
  counting as the new version

Logging (so a report says what happened)
- The kernel driver logs its own version at start
- A full video-memory window logs the first refusals and then every 256th, with one line saying who holds the window
- Spare framebuffer instances (more instances than displays) say so instead of reporting a failure
- A display that stops flipping logs how long the frame had been pending and the flip counters
- Every ten seconds of screen activity, one line per display records the flip rate against the panel rate, missed
  refreshes and skipped frames
- A refused mode set or flip in the kernel names the stage that refused it
- The Metal driver says why it could not begin a command buffer (a lost GPU, a failed allocation), the line
  WindowServer crashes without

## 2.3.3

Stability
- 2.3.2 made some Macs worse: blank or torn windows and a Dock missing its apps on macOS Sequoia, WindowServer
  crashes on some Tahoe desktops. Its display changes (screenshots and the lock screen, the brightness and video-memory
  logging, the Turing flip change) are withdrawn from the package until they pass a staged test with the testers.
  2.3.3 ships the 2.3.1 driver, which has the longest clean record in the field, on both macOS 15 and macOS 26
- macOS Sequoia: installing and updating works again (the 2.3.2 fix is kept: the Metal driver is built for macOS 15)
- After a rollback, the installed version record follows the files that are really installed (kept from 2.3.2)

## 2.3.2

Fixes
- macOS Sequoia: installing or updating to 2.3.0 or 2.3.1 stopped with "runtime payload requires macOS 26.0.0". The
  Metal driver in those packages was built for macOS 26 only; it is built for macOS 15 again, and a release can no
  longer ship anything that needs a newer macOS than 15
- Screenshots, window captures and the lock screen showed a blurred or empty window. macOS reads a window right
  after drawing it, and the driver now finishes the drawing first, as Apple's drivers do
- Send logs now includes how much of the card's video memory window is in use, so a black screen with only a
  cursor can be told apart from a leak
- After an update was rolled back, Send logs still named the new version. The installed version now follows the files
  that are actually installed, so reports say what your Mac is really running
- The Metal driver never hands the GPU a missing texture or sampler; it uses an empty one and logs it (a cause of
  Safari crashes in older versions)
- Laptops with the screen on the NVIDIA GPU: every step of the brightness setup is logged, so brightness that stays
  greyed out can be traced from Send logs

AMD / Intel test program
- A test driver that fails to start no longer panics the Mac: memory still mapped when the start gave up is kept
  until it is unmapped, instead of being freed under the mapping
- Intel 11th and 12th gen graphics (Tiger Lake, Rocket Lake, Alder Lake-S) and DG1 now reach the Intel driver. Two
  drivers list these chips and the device was offered to the wrong one, so neither took it
- When a start fails, its first error lines are kept and reported on the next start, even if the Mac stopped
- The test driver's log names AMD and Intel devices correctly

## 2.3.1

Safer updates
- If an update cannot bring your display up, your Mac no longer stays on a black screen. After two starts in a row
  that never reached the display, the driver turns itself off so macOS still starts, and the driver you had before the
  update is put back automatically with one restart. Nothing stays running once an update has worked
- Releases now also have to pass app checks on the new Metal driver (Terminal text, window capture) and may not do
  worse than the release before

## 2.3.0

Fixes
- Plugging a different monitor into the same port: macOS sees the new monitor (its name, size and modes) instead
  of keeping the one it replaced
- Terminal and other apps that draw text with programmable blending: text no longer comes out missing or garbled
  (GTX 16 and others). Draws that read colour from a pass without every attachment were skipped; they are now drawn,
  and overlapping triangles in one draw read each other's colour in order (thanks pengubow)
- Above 60 Hz: the display and the accelerator woke up once per millisecond to check for a finished flip, which
  users' flicker recordings showed as about 56 flips a second on a 143 Hz panel while windows moved. They now wake on
  the flip itself (the display on macOS 15 and 26, the accelerator on macOS 26)
- When a display's mode set is refused, the driver now tries the mode the firmware is already running before giving
  up, and the log says why the mode was refused (reports of a black screen at login on some RTX 3060 boards)
- Draws the Metal driver had to drop are now in the logs that Send logs collects

AMD / Intel test program
- 1401 can join the AMD and Intel GPU test program (AMD/Intel panel, Join). Testers get the newest signed test build
  of the AMD/Intel driver, and after every restart 1401 sends what the driver did, so a start that hangs or panics
  still reaches us. Leave the program from the same panel at any time

## 2.2.0

This release makes the driver do one thing: drive NVIDIA GPUs. Everything else it used to carry is now its own
product or is gone, which makes the package smaller, leaves less running on your Mac, and leaves less to go wrong.

Leaner
- The package carries the NVIDIA driver only: the AMD/Intel GPU driver, the NPU driver and the audio driver are
  separate products with their own packages and installers (the package drops from about 1 GB)
- The boot logo is gone: the OpenCore logo driver, the boot progress screen and the display driver's code that kept
  the logo on screen. Installing, updating or removing the driver takes an existing logo out of OpenCore
- Three background jobs are gone: a display tracker that streamed the driver's log all the time, a WindowServer
  restart at every boot (a flicker workaround from before the flicker fix in 2.1.0), and a crash-check login item.
  Logs are collected only when you press Send logs. Installing or removing the driver takes out what an earlier
  version left
- The 1401 Mac app moved to the 1401 project; it installs this driver with the package's own installer

Fixes
- A display whose firmware boot timing NVIDIA's display code does not accept (seen on an RTX 3060 at 1920x1080) now
  switches to the validated timing for the same resolution instead of crashing WindowServer at login
- Checksum lists are checked strictly: a malformed line in SHA256SUMS used to be skipped while the check still passed
- install.sh fixes PATH before running anything as root, like uninstall.sh
- Crash reports sent with Send logs keep the library identifiers needed to read them (machine identifiers are still
  removed)

## 2.1.0

Driver
- Each GPU generation (GTX 16, RTX 20, 30, 40 and 50) is now described by the card itself: the driver reads the
  architecture and the newest engine classes from the GPU and publishes them, and the Metal driver picks the video
  decoder and the scan-out model from that instead of a table of device IDs
- Cards with a small BAR1 (Resizable BAR off, or a board that does not offer it): the driver moves BAR1 away from the
  boot screen at its own size, so GTX 16 / Turing cards get the same zero-copy display path as the others. A BAR1 that
  macOS left unassigned is reported instead of failing with no reason
- The desktop no longer waits for the GPU on every frame (#196): each window the compositor draws is marked when the
  GPU finishes it, and the display waits only for that mark
- A BAR1 the firmware already placed large is left as it is, and the old setup comes back if a resize fails
- Gamma tables are applied; HiDPI desktop modes can be turned on with nvfbhidpi
- The installer keeps your own settings block in nvrm610.conf

Metal
- Blender: Eevee renders lit scenes. NVIDIA GPUs cannot write the stencil value from a shader, which Blender's Metal
  backend relies on to sort its lighting; the driver now rebuilds that value by drawing once per stencil bit. Also
  fixed: a light loop that hung the GPU (the first active SIMD lane, and loop values lost when helpers were inlined),
  render passes with no attachments, layered 3D colour targets, framebuffer fetch, samplers held in argument buffers,
  and image atomics in helper functions. Blender's shadow pages no longer crash it
- Indirect compute dispatches read their grid size on the GPU instead of stopping to read it on the CPU
- Hardware video decoders are registered with macOS's decode service (#175)

AMD / Intel test driver
- The log now says which driver started, which PCI drivers registered and which device each one matched, so a GPU
  no driver took can be traced from a user's log

Mac app
- Map my GPU (experimental, opt-in)

## 2.0.4

Mac app
- Prepare this Mac for Tahoe is back: on macOS 15 one button installs or updates the driver (it carries the Tahoe
  driver too), parks it and sets the installer's GPU settings, then opens Software Update. After the update the
  first start sets the driver up for Tahoe and restarts once by itself. Cancel puts everything back, also with a 1401
  stick plugged in next to the internal disk (it changes back the config the preparation changed)
- Intel and AMD laptops and PCs whose own GPU OpenCore switches off (disable-gpu, or -wegnoigpu on Intel) can try the
  AMD / Intel test driver: the app no longer says "no GPU" and stops; turning the test driver on turns that GPU back
  on (also on Core Ultra, where the NPU used to hide it), and turning it off switches it off again exactly as it was

1401 for Windows
- When the stick's disk number is gone at write time, the log now lists the USB disks Windows shows instead

## 2.0.3

Driver
- System sleep and wake (PR #182, thanks danlt150): the GPU's memory is kept across sleep, the PCI setup and
  Resizable BAR are restored on wake, and the display driver does the wake steps macOS expects. Before, the GPU
  lost its state in sleep and the desktop could freeze on wake
- The Metal driver finds a submission's copy buffers once instead of on every check (PR #223, thanks HighDelay)
- The display driver no longer fills the log with the same "hdcs" line many times a second (#216)
- Security (#214): diagnostic sysctls that let any user read physical addresses and memory are gone from normal
  builds (they exist only with the nvrmdiag=1 boot argument, and only root can read them); starting the GPU by
  hand is root-only; the kernel addresses used for debugging are left out of shipped builds

Mac app
- AMD boards (AM4 and AM5) whose OpenCore config rewrites the firmware memory map get DisableVariableWrite, so
  macOS cannot write NVRAM through that map (boards that stopped starting after an install)
- When no NVIDIA card is visible, the app says why: -wegnoegpu or -wegonogpu in boot-args, a disable-gpu property,
  or a laptop BIOS set to integrated graphics only
- Macs with only an AMD or Intel GPU can turn on the experimental AMD / Intel GPU driver; it installs from the
  downloaded driver package, without the NVIDIA driver
- Laptops whose built-in screen is wired to the NVIDIA GPU get an opt-in switch for brightness control

1401 for Windows
- Intel PCs (12th gen and newer) that stop at EB.MM.AKM because no kernel slide fit: rebuilding with the stick
  plugged in reads its startup log and turns on DevirtualiseMmio for the next start
- AMD PCs get the same NVRAM write guard as the Mac app (DisableVariableWrite when the memory map is rewritten)
- A stick whose disk number changes during the write is found again instead of stopping the build; a volume that
  disappears is skipped
- When Apple's download server resets the connection, the stop message says what to try on the network
- When Windows cannot format the stick, its own error is now in the log before the fallback

## 2.0.2

1401 for Windows
- A PC with no network adapter that works in the macOS installer gets its stick written, with a note to connect a
  supported Ethernet or Wi-Fi adapter before installing
- A stick that Windows gives a new disk number just before the erase is found again and written

No changes to the driver or the Mac app since 2.0.0.

## 2.0.1

1401 for Windows
- PCs with an NVIDIA card can build for macOS 26 Tahoe. 2.0.0 offered only macOS 15 for them, so picking
  Tahoe stopped the build

No changes to the driver or the Mac app since 2.0.0.

## 2.0.0

macOS 15 Sequoia and macOS 26 Tahoe.

Security
- Fixes for every finding in the security report by @Mahasvan (#214). Thank you for reporting them to us first.
- The driver's administrator check now reads the caller's real user id; it used to answer yes for everyone
- Copies between NVKMS and a process can no longer reach kernel memory, and the NVKMS entry point is root-only
- First-time GPU bring-up requires root
- A process cannot map memory that belongs to another process
- Unknown client types are refused, the bind call checks its range and always drops a stale mapping, waits on GPU
  events are capped, and one process can no longer fill the client table (slots are kept for the system)
- The kernel-address check refuses to arm on any macOS build it was not made for
- The Mac app verifies a signature on its own updates before unpacking them, runs with the hardened runtime, and
  refuses to run its root scripts from a folder other users can write to

Driver
- GeForce RTX 50 series: compute kernels compiled by NVIDIA's own compiler now run on the card
- Fix the black screen with only a cursor after updating to 1.15 (video memory reserved over the boot screen was handed
  out again), including displays larger than the boot screen's reserved memory, such as 3840x2560 on cards with a
  256 MB memory window
- Fix flicker caused by a reused buffer showing an old frame
- A second monitor keeps its high refresh rates, and panels faster than 240 Hz are paced correctly
- Ultrawide, 4K and HiDPI modes are no longer capped at twice the boot resolution
- Cards with a small memory window: the display's own share is counted, so the desktop stops running out of space;
  a Turing card left at 64 MB is resized to the largest size it offers
- A display connected after startup comes up without a restart (#142, thanks @lsCoding666)
- macOS learns the panel's physical size, so scaling is right instead of assuming 72 dpi (#199, thanks @GeekAudio)
- Laptops with the screen on the Intel GPU: the driver no longer takes the Intel GPU's display slot (#131, #119, thanks
  @TararaDotJoshua), and muxless Turing GPUs get the startup settle they need
- Tahoe: ship the macOS 26 accelerator built from this release (1.15 carried an older one: black screen on Tahoe)
- Fix crashes in Photos and media analysis, in apps that split a command buffer at an event, in Chess, and in apps
  that free Vulkan memory (thanks @Exop63)
- Fix memory running out during Super Resolution and other long machine-learning work
- A texture copied for display stays in step on every submission (thanks @KolliasG7); work is reported as scheduled
  only after IOSurface writes finish (#147, thanks @imyago9); a surface keeps its video memory across page-off (#200,
  thanks @imbrahiam)
- World of Warcraft Classic and other Metal games: cube-map arrays, bounds-checked buffer reads and several shader fixes
  (#171, thanks @razvanccr); Firefox WebRender shaders translate (thanks @advokat11); MSAA sample positions on cards
  without programmable 1x positions (#136, thanks @eisda)
- Faster pipeline builds: built in parallel, one hash per shader module, planner results reused, and one private image
  per IOSurface (#173, #177, #181, #188, #189, thanks @modesttao)
- Chrome, Edge and Electron apps draw on the CPU again by default, which ends the artifacts, stutter and voice-call
  crashes in Discord on several cards; `NVMTL_ANGLE_METAL=1` turns their GPU path on
- Optional asynchronous texture uploads, off by default (`NVMTL_ASYNC_TEX_UPLOAD=1`)
- Multi-GPU display selection workaround documented in docs/MULTI-GPU.md (#197, thanks @is-pan)

Experimental, off unless you turn it on
- AMD and Intel GPU support (NMXGPU) and the NPU driver, for macOS 15 and 26: Setup > "AMD / Intel GPU driver (test)"

Mac app
- Finish a macOS update the update helper never finished, and keep this Mac's own BAR settings afterwards
- "Remove NVIDIA driver" stops with an explanation, instead of failing half-way, when System Integrity Protection is on
- The Remove NVIDIA driver startup entry is always visible, and removing the driver keeps the screen on
- Warn when the Mac starts OpenCore only from the USB stick and the internal disk has none
- Explain a failed config backup on a read-only OpenCore partition, and say which step comes first when Install is
  greyed out
- Send logs copies files before running log queries and includes crash reports under any name

1401 for Windows
- Pick macOS 15 Sequoia or macOS 26 Tahoe before the build; see the 1401 changelog for the full list

Known issues
- Preview not showing pictures and lost window transparency on some macOS 15 setups
- Chrome decodes video on the CPU while its GPU sandbox is on (#175)
- Thunderbolt eGPUs: no display
- Laptops with the screen on the Intel GPU: the built-in screen is not accelerated
- HDMI/DisplayPort audio: the display now reports its audio formats to macOS, but sound over HDMI/DisplayPort is not
  working yet

## 1.15.0

The last release for macOS 15 Sequoia.

Driver
- Fix black screen with only a cursor on GTX 16 and other cards without Resizable BAR (regression in 1.14)
- Fix Unity 6 games hanging on the first frame
- Fix windows and web pages showing old content after a redraw
- Fix high WindowServer CPU and low frame rate at 4K after a few minutes
- Fix slow scrolling (window copies up to 35x faster)
- Fix monitors shown as a 30-inch TV in About This Mac
- Fix 165 Hz mode missing on some monitors (since 1.14.2)
- Retry a display that stays dark after sleep on the next wake
- Fix Chrome, Edge and Electron apps drawing everything on the CPU (no WebGL, slow scrolling); their GPU path is on
  by default again and `NVMTL_ANGLE_METAL=0` turns it off
- Allow Chrome, Edge and Electron apps to use the GPU with their sandbox on
- Fix 5120x1440 and other ultrawide modes missing when the firmware started at a smaller resolution
- Fix green lines in Photos and other VideoToolbox scaling (#179, #180, thanks @modesttao)
- Add T400, T600, T1000 and T-series laptop GPUs (#132, thanks @TararaDotJoshua)

Mac app
- Show download progress in Update driver, which looked stuck at "Downloading the newest driver..."
- Ask for access to USB sticks instead of failing with "no OpenCore config"
- Say when the boot stick is unplugged
- Recover from an interrupted install instead of stopping at "installer guard already exists"
- Download from the NullMoth mirror when GitHub is blocked
- Send logs: always finish, keep the start of each boot, stop re-sending old logs

1401 for Windows
- Fix the build stopping on ACPI tables that declare a method twice (BIOSTAR B650 and similar)

Other
- Grammar fixes in the release notes (#128, #129, thanks @lunar-me)
- Translator test fix (#172, thanks @modesttao)
- Release notes moved into this file; add CONTRIBUTING.md and a pull request template

Known issues
- If Chrome pages show corrupted areas on your card, start it with `NVMTL_ANGLE_METAL=0` and send logs
- Ghost or black areas after windows overlap with a BAR of 4 GB or more (#127)
- Chrome decodes video on the CPU while its GPU sandbox is on: VideoToolbox picks a decoder before the driver loads (#175)
- Thunderbolt eGPUs: no display
- Laptops with the screen on the Intel GPU: the built-in screen is not accelerated
- No HDMI/DisplayPort audio

## 1.14.7
- Stop driver updates from turning the EFI boot logo back on
- Keep the existing OpenCore BAR settings when installing
- Show install, cancel and failure in the updater after the download finishes

## 1.14.6
- Fix Firefox WebGL inside the GPU sandbox
- Turn off the Chromium ANGLE Metal path by default after corruption reports on RTX 30 and 50
- Retry Apple recovery downloads that return HTTP 403

## 1.14.5
- Fix Unity 6 games hanging on the first frame
- Keep Chromium WebGL on the GPU when the sandbox blocks a memory query
- Send logs: give each log query its own time limit

## 1.14.4
- Fix full-screen flicker on machines that copy frames to the screen
- Send logs includes the AMD/Intel test driver

## 1.14.3
- Stop macOS writing to firmware memory on Ryzen 7000/8000/9000 (AM5)

## 1.14.2
- Fix black login window on high refresh rate monitors
- Fix SecurityAgent and Installer crashes on some RTX 40 PCs
- Fix apps quitting on a GPU check (Safari, photo analysis)
- Fix the AMD/Intel test driver firmware download

## 1.14.1
- Fix PCs that stopped starting after 1.14.0
- Fix the boot logo and test driver switches doing nothing

## 1.14.0
- Fix black screen with a cursor about a minute after start-up (#16, #26)
- Fix 4K at 120 Hz and other DSC modes going black (#77)
- Turn the monitor off when the display sleeps (#90)
- Bring a display back after switching inputs (#110)
- Fix black screen after 100 seconds on older Intel boards without Resizable BAR

## 1.13.0
- Fix Turing cards stuck at start-up after updating
- Fix "Remove NVIDIA driver" not removing the driver, or hanging
- Crash reports no longer show crashes from before the update

## 1.12.0
- Use the NVIDIA GPU on laptops whose screen is on the Intel GPU (as a second Metal device)
- Record display state from the first second of each boot for flicker reports
- Fix Live Text crashing in the Vulkan driver

## 1.11.0
- Download Lilu and AMFIPass when they are missing from the EFI
- Fix Firefox crashing at launch (`ipc_control_port_options=0`)
- Fix "STOP back up nvmtl"
- Add USBToolBox when it is missing

## 1.10.0
- Fix AM5 Ryzen PCs stuck at `EB.MM.AKM`
- Drop the DMAR table on Intel Core Ultra
- Reach Apple's recovery server directly when DNS fails

## 1.9.0
- Try one more memory setting on AM5 boards
- Erase stubborn USB sticks with diskpart

## 1.8.0
- Use known-good settings for boards that have booted before
- Fix "Format-Volume: Invalid property" and "WinError 2" when erasing

## 1.7.0
- Pass the board's DSDT to macOS again (was dropped on about 1 in 5 PCs)
- Send logs delivers the flicker capture and OpenCore config

## 1.6.0
- Fix the driver removing itself after login on some boards
- Fix `RmInitAdapter failed` on Ryzen with a custom EFI
- Send logs includes the OpenCore config (serials removed)

## 1.5.0
- Finish the macOS download on networks that cut connections

## 1.4.0
- Clear flicker at login with a compositor restart
- Build on PCs whose ACPI tables crash the disassembler

## 1.3.0
- Fix Send logs freezing the app
- Fix the crash loop on 16 GB RTX 50 cards
- Light up a monitor plugged in after login

## 1.2.0
- Fix "Remove NVIDIA driver" so the driver stays off until removed
- Show the picture with macOS's display driver while ours is off

## 1.1.0
- Fix recycled allocations reaching queued GPU work
- Fail a rejected display request on its own instead of stopping the display

## 1.0.x
First public releases: installer, updater, removal and USB map fixes.
