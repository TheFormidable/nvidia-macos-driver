Release 2 of this fork (**3.0.20**). It covers everything this fork added after release 1 (3.0.2): versions 3.0.3 to 3.0.20. This page lists only what this fork built itself. The changes merged from the original project are left out: its 3.1 (merged as 3.0.18) and the open pull requests by other authors (taken as 3.0.4 and 3.0.5). Each of those is still in [`CHANGELOG.md`](../../CHANGELOG.md) under its own version, with its author. There is no 3.0.10, and 3.0.15 was tried and not kept.

The headline items:

- **One package for every macOS from 10.7 Lion to 26 Tahoe.** The installer reads the macOS version and installs the matching part by itself.
- **Much faster OpenGL and Vulkan games.** Zink on NVK is the OpenGL driver now. Swaps no longer wait for the GPU or the display, the native Vulkan frontend shows the newest frame, and NVK uses the GPU's hierarchical Z.
- **The graphics stack can load with AMFI left on.** The package's graphics stack is signed with this fork's CA. Add this release's anchor kext to OpenCore through AMFIAnchor, or sign the package with a CA of your own.
- **No manual "Allow" after a first install.**
- **Screen sharing no longer stalls WindowServer.**

> [!WARNING]
> **This is a pre-release, tested on one machine.** That machine is an AMD Ryzen 5 5500 hackintosh with an RTX 5050 on macOS 26.7.3.
>
> - **macOS 15** has not been tried with this fork.
> - **macOS 10.7 to 14: the kits have never run on a card or on those releases.** Each kit's kernel extensions went through that release's own kext linker (10.7 to 10.15) or were linked at build time (11 to 14). That only means the kernel of that release would accept loading them.
> - **If it goes wrong:** add `-nvoff` to boot-args to start without the driver. On 15 and 26, if two boots in a row do not light the display, the driver brings back the previous version by itself on the third. `sudo ./uninstall.sh` from the package folder removes the driver.

> [!WARNING]
> **The PAT change (3.0.13) runs in the kernel, on every CPU, at boot and at every wake.** On a machine whose page attribute table has anything but write-combining (WC) in entry 6, NVRM rewrites that entry to WC on every CPU at once, with interrupts off. AMD hackintoshes get exactly that table from AMD_Vanilla's default "Fix PAT" patch. A real Mac, or a machine whose entry 6 is already WC, sees no write at all.
>
> - **The rewrite itself has not run on real hardware yet.** On the test machine, OpenCore already puts WC back in entry 6, so the driver finds WC there and changes nothing (`debug.nvrm_pat_passes: 0`).
> - **If it goes wrong**, expect it early: a hang at boot, a freeze, or trouble right after waking from sleep. Add `nvpatwc=0` to boot-args and the table is left as the kernel wrote it.
> - **To check it**, after a boot and again after a sleep, run `sysctl debug.nvrm_pat_wc debug.nvrm_pat_why debug.nvrm_pat_found debug.nvrm_pat_passes debug.nvrm_pat_cpus hw.ncpu`. `nvrm_pat_wc` should be 1. When `nvrm_pat_passes` is above 0, `nvrm_pat_cpus` should equal `hw.ncpu`.

## Install

Download `nullmoth-nvidia-3.0.20.tar.gz` below (SHA-256 `670162b02223f014ea1a70e4207422454ac73df521a7e659a190e2d03d66702e`). It was built from commit `bf5de958` of `fable` over the NullMoth 2.9 release package, and it carries the 13 kits inside. Its graphics stack (NVMTLDriver.bundle, NVKZinkGLDriver.bundle and the nvmtl libraries) is signed with this fork's CA. With the two AMFI boot arguments of the README that signature changes nothing; with the [anchor kext](#amfi-left-on-the-anchor-kext-optional) it lets AMFI stay on. Then, in Terminal:

```
tar -xzf nullmoth-nvidia-3.0.20.tar.gz
cd pkgroot
sudo env CHECK=1 ./install.sh
sudo ./install.sh && sudo shutdown -r now
```

The third line only rehearses the install and changes nothing. Install only when it ends with `CHECK PASS`. The commands are the same on every release. OpenCore needs the settings in the [README](../../README.md#opencore-settings). On 10.7 to 14 the kit's installer prints the boot arguments it needs.

Before you install:
- **DLSS is downloaded by default**: about 750 MB the first time. These are NVIDIA's own files, checked against pinned hashes. `sudo env NULLMOTH_DLSS=0 ./install.sh` skips the step.
- **macOS 26 and `alcid`:** macOS 26 has no AppleHDA, so AppleALC does nothing there. The audio driver leaves the onboard sound alone while `alcid` or `alcverbs` is in boot-args. Remove them to get speakers, headphones and microphone back.
- **The first launch of each game is slower.** NVK's shader cache is keyed to its library, so each game's shaders are compiled again once.

### AMFI left on: the anchor kext (optional)

`NVAnchorInjector.kext.zip` below (SHA-256 `38baea8865d391ea6b347529fc0301a7305763a2d736a1b36826bfdc8763ae54`) holds the anchor kext for OpenCore. With it, the driver can run on macOS 15 and 26 without `amfi_get_out_of_my_way=0x1 amfi=0x80` in boot-args (tried on 26 only). It is a codeless kext: it carries only the public key of this fork's root certificate (key id `b3556ae9e7564be4ef5b453ac115790ca3f5f792`; the certificate's SHA-256 fingerprint is `DA:F9:81:B8:ED:2F:22:75:31:CE:88:E5:D5:17:07:02:32:33:24:6B:3E:8F:6D:AB:FB:5F:79:88:1F:1A:A7:DA`). The private keys stay on the maintainer's Mac and are not on GitHub.

> [!IMPORTANT]
> **Install the anchor only if you trust this fork's signer.** With it, AMFI on your Mac accepts anything signed with this fork's key, not only this driver. To trust only yourself, make your own CA and anchor, and sign the package with them ([docs/SIGNED-GRAPHICS-EXPERIMENT.md](../SIGNED-GRAPHICS-EXPERIMENT.md)).

1. Work on a copy of your EFI on a USB stick. Keep the EFI that still has the two AMFI arguments.
2. Put `NVAnchorInjector.kext` (from the zip) and `AMFIAnchor.kext` from [AMFIAnchor's releases](https://github.com/b00t0x/AMFIAnchor/releases) (0.1.2 on the test machine) in `EFI/OC/Kexts`, beside Lilu (1.7.2 or later).
3. In `Kernel → Add`, in this order, each with `PlistPath` `Contents/Info.plist`:
   - `Lilu.kext`, `ExecutablePath` `Contents/MacOS/Lilu`;
   - `AMFIAnchor.kext`, `ExecutablePath` `Contents/MacOS/AMFIAnchor`;
   - `NVAnchorInjector.kext`, with an empty `ExecutablePath`.

   Set `MinKernel` `20.0.0` and `MaxKernel` `25.99.99` on the last two. Keep AMFIPass: the test machine loads it after the anchor.
4. Remove `amfi_get_out_of_my_way=0x1 amfi=0x80` from boot-args in the stick's `config.plist`, and start from the stick.
5. Check that it worked:
   - `ioreg -r -c AMFIAnchor -l | grep -E 'InstallStatus|LoadedAnchors'` shows `InstallStatus` 1 and `org.theformidable.nvidia-signing`;
   - `sysctl -n kern.bootargs` has no AMFI argument;
   - `system_profiler SPDisplaysDataType` shows the card with Metal.

The route covers macOS 15 and 26. The kits for 10.7 to 14 are not signed with this CA: on those releases, use the boot arguments their installer prints. The signing certificate is valid until 26 November 2027. This was tried on one machine only. If the desktop comes up without Metal, start from the EFI that still has the two AMFI arguments.

## What is new since 3.0.2

### One package for every macOS (3.0.19)

- **The 13 kits for macOS 10.7 to 14 ride inside the package.** The installer reads the macOS version and `package/pick_kit.sh` picks the part to install: the usual driver on 15 and 26, the kit for that release on 10.7 to 14. Nothing to choose: the same `sudo ./install.sh` everywhere. On the older releases the installer copies the kit to a root-owned folder, checks every file against the package's checksums, and runs the kit's own installer. `uninstall.sh` runs the kit's own remover.
- **The kits are built by this fork, from its own tree,** with Apple's SDK and Kernel Debug Kit for each release. The older-release support itself comes from the original's 3.1. The headers regenerated from each release's accelerator family are kept in the repository (`build/legacy/era-headers`), so a rebuild needs no full macOS installer.
- **What we fixed to make them build:**
  - The kernel driver builds for 10.7 to 14 again: three things this fork had added after 2.9 needed a newer kernel.
  - The Metal accelerator now links for 10.12 to 14.
  - A kext that its release's linker refuses is held back instead of stopping the whole install.
  - Every program in the kits is signed, and the kits carry no Apple file.

| macOS | kernel extensions | Metal | OpenGL |
|---|---|---|---|
| 10.7 | NVRM, NVRMFB | not on that release | yes |
| 10.8 | NVRM, NVRMFB (NVRMAGDC held back: that release's linker refused it) | not on that release | yes |
| 10.9, 10.10 | NVRM, NVRMFB, NVRMAGDC | not on that release | yes |
| 10.11 | NVRM, NVRMFB, NVRMAGDC | no (its accelerator family lacks the GPU page-table methods) | yes |
| 10.12, 10.13 | NVRM, NVRMFB, NVRMAGDC, NVAccel | yes | yes |
| 10.14 to 14 | all four | yes | through Apple's OpenGL-on-Metal (see the kit's `options/`) |

### Installing gets out of the way

- **AMFI can stay on (optional).** Instead of the global `amfi_get_out_of_my_way=0x1 amfi=0x80` boot arguments, you can do this:
  - Sign the graphics stack (NVMTLDriver.bundle, NVKZinkGLDriver.bundle and the nvmtl libraries) with a CA. The release package comes signed with this fork's CA, and its anchor kext is published with it ([above](#amfi-left-on-the-anchor-kext-optional)). You can also use a private CA that you create on your own Mac (`tools/make_amfi_anchor.sh`, `tools/sign_graphics.sh`).
  - Register that CA in OpenCore as an [AMFIAnchor](https://github.com/b00t0x/AMFIAnchor) anchor: Lilu, then AMFIAnchor.kext, then the anchor kext.

  On the test machine, WindowServer then loads the Metal plugin with AMFI enforcing and the GPU reports Metal 4, and 3.0.20 ran signed that way. This is still experimental: one machine. Signing keys never leave the Mac they were made on. [docs/SIGNED-GRAPHICS-EXPERIMENT.md](../SIGNED-GRAPHICS-EXPERIMENT.md) has the steps.
- **A first install no longer needs "Allow" in System Settings > Privacy & Security.** The installer writes the approval for its own kexts into macOS's kext policy database, so one restart is enough. Where SIP keeps that database read-only, macOS asks as before, and the installer's last message says which case happened. Not yet tried on a Mac.
- **3.0.13: the installer warns about `alcid`/`alcverbs` on macOS 26**, where they leave the onboard sound without a driver.

### OpenGL games (Zink on NVK)

- **3.0.3: OpenGL goes through Zink on NVK on macOS 26.7.3.** The layout guard now accepts the CGL context macOS 26.7.3 hands out, whose renderer does not yet hold the GL engine. Before, OpenGL fell back to Apple's OpenGL renderer over the Metal plugin.
- **3.0.7: a swap no longer sleeps until the GPU has finished its frame.** The window's video memory is also imported once instead of for every frame. It is one change in the kernel, NVK and Zink, with a presentation thread per process.
- **3.0.8: a swap asks WindowServer for the window's opacity once per tenth of a second**, not on every frame.
- **3.0.11: with vertical sync off (swap interval 0), a swap no longer waits for the display.** The frame is parked, and a sender thread hands the newest one to the window.
- **3.0.16: `glBufferSubData` no longer waits for the GPU.** Where Mesa's threaded context would have waited for a busy buffer's whole batch, the upload now goes through a staging buffer and a copy on the GPU.

### Vulkan games (the native Vulkan frontend)

- **3.0.9: an IMMEDIATE swapchain shows the newest frame, and `vkQueuePresentKHR` no longer waits for the window's layer.** Before, Minecraft's Vulkan renderer was held at twice the display's rate.
- **3.0.11: MAILBOX is offered.** It uses the same presentation thread with the display's sync kept on, so a game that asks for MAILBOX is no longer held to FIFO.

### On the GPU

- **3.0.17: NVK uses the GPU's hierarchical Z (ZCULL).** It throws away whole blocks of hidden fragments before they are shaded. NVK had the code but never ran it on macOS, because the NVRM backend reported no ZCULL unit. It now asks RM for the unit's geometry, the way Linux does. ZCULL stays off in WindowServer.
- **3.0.13: write-combined mappings are write-combined again** on machines with a "Fix PAT" kernel patch (see the warning above). With WC in place on the test machine, the game thread's share in `glBufferSubData` uploads fell from 11-14 % to 1.4 %. Its share in importing the window's surface fell from 7-8 % to 1.2 %.
- **3.0.13: the installer fetches the DLSS files by default and builds their kernels for the card.** Before, this step needed `NULLMOTH_DLSS=1`. With the files in place, the Metal plugin serves a game's MetalFX temporal upscaler with DLSS Super Resolution and its frame interpolator with DLSS Frame Generation, so a game that uses MetalFX gets them with no setting (RTX cards).
- **3.0.6: NVRM says whether write-combining is real on this machine** (`debug.nvrm_pat_wc`, `debug.nvrm_pat_why`).
- **3.0.3, GPU Caps:** a read-only, versioned cache of what each GPU can do, filled from RM at start. It writes nothing to the card.

### Screen sharing (3.0.20)

- **A page-off reads the GPU's memory with the copy engine without holding the accelerator lock.** Firefox, OBS, Zoom and other programs read captured frames with the processor. Each frame was a page-off that held WindowServer's accelerator lock for about 2 ms, about 90 times a second. Now only the processor's part, about 1 ms, holds the lock, and the copy is thrown away and redone with the lock held if anything it read from changed. On the test machine, sharing a window from Firefox, WindowServer's waits fell from 301 and 420 ms to 130 and 116 ms in ten seconds (-72 % of the time).
- Google Meet still gets 5 frames a second from Firefox and Safari. The page asks that of every browser that does not call itself Chrome, so the driver cannot change it. [docs/TROUBLESHOOTING.md](../TROUBLESHOOTING.md) has the workaround.

### Metal apps and Apple's OpenGL renderer (3.0.6, a performance audit of games)

- Apple's OpenGL renderer no longer sleeps on a thread hop at every context flush. Its resource waits wake on an event instead of polling every 50 µs.
- Command buffers cost less to make. Textures that Apple's renderer makes and drops for every `glBlitFramebuffer` are recycled. Finished asynchronous texture uploads are given back at the next upload.
- A draw records less on the CPU: NVK no longer walks 642 empty bindings for every descriptor set, and the plugin skips binds the command buffer already holds.
- A window surface drawn through a private image no longer makes the game wait for the previous frame's copy-out.
- Compute kernels:
  - Threadgroup-size variants no longer stall other threads.
  - The compile from pipeline creation is reused.
  - A refused function-constant variant is not translated again.
- Three shader translations that failed on the card are fixed: a sampler picked through call-site attributes, the elements of an argument-buffer array of handles, and a loop that switches between two buffers.
- The VRAM pager no longer puts the allocating thread to sleep, and the memory table is looked up through an index.
- DLSS no longer leaks image views, and a view can no longer outlive its image.

### Shader compiler and caches

- **3.0.12: a compiler switch that is turned on or off no longer gets shaders cached under the other setting.** 35 switches are now in NVK's cache key.
- **3.0.14, for developers:** `NAK_STATS=1` writes one JSON line per compiled shader. A fixed corpus of 300 shader stages (`tools/nak-corpus/`) and `tools/nak_stats_diff.py` compare two runs. Nothing changes unless it is set.

## Readings on the test machine

The test machine is an AMD Ryzen 5 5500 with OpenCore and AMD_Vanilla, an RTX 5050, and one 1920x1080 display at 100 Hz, running macOS 26.7.3. These readings come mostly from the game's own counter. The scenes were not always the same, so they are not like-for-like benchmarks.

| Game | Release 1 (3.0.2) | Release 2 |
|---|---|---|
| All the Mods 10 (heavily modded Minecraft, OpenGL) | 19 to 74 fps, through Apple's OpenGL renderer | 55 to 64 fps on 3.0.6 through Zink; 90 fps on 3.0.7; 123 to 290 fps on 3.0.12 |
| All the Mods 10 with the Complementary Unbound shader pack | | 75 to 190 fps on 3.0.12 |
| Minecraft 26.3 without mods, Vulkan renderer, vsync off | 200 fps (twice the display) | up to 1000 fps on 3.0.9 |
| Minecraft without mods, OpenGL through Zink, vsync off | | 490 fps on 3.0.11 |

Not measured yet: what ZCULL (3.0.17) gives, and 3.0.16 on a card while the wait it removes is happening.

## The ways back

Every change that runs by default has one way back:

- Settings lines go in `/Library/GPUBundles/nvmtl/nvrm610.conf`, between the "my settings" markers, so that an update keeps them.
- Boot arguments go in boot-args.

| Change | Version | Way back |
|---|---|---|
| Page-off copy without the accelerator lock | 3.0.20 | boot argument `nvaccel_pageoff_copy_locked=1` |
| Hierarchical Z (ZCULL) | 3.0.17 | `NVK_ZCULL=0` |
| `glBufferSubData` through a staging buffer | 3.0.16 | `NVK_ZINK_SUBDATA_STAGE=0` |
| WC put back in the PAT | 3.0.13 | boot argument `nvpatwc=0` |
| DLSS downloaded by the installer | 3.0.13 | `sudo env NULLMOTH_DLSS=0 ./install.sh` |
| Zink, newest frame with vsync off | 3.0.11 | `NVK_ZINK_PRESENT_NEWEST=0` |
| Vulkan frontend, newest frame (IMMEDIATE and MAILBOX) | 3.0.9 | `NVK_BRIDGE_PRESENT_NEWEST=0` |
| Zink, opacity asked once per 100 ms | 3.0.8 | `NVK_ZINK_OPACITY_KEEP=0` |
| Zink, swap one frame ahead / surface imported once | 3.0.7 | `NVK_ZINK_PRESENT_AHEAD=0` / `NVK_ZINK_SURFACE_KEEP=0` |
| GPU Caps discovery at start | 3.0.3 | boot argument `nvgpucaps=0` |
| OpenGL through Zink | | boot argument `nvzink=0` |
| The whole driver, for one start | | boot argument `-nvoff` |

The Metal plugin's changes of 3.0.6 have one `=0` settings line each: `NVMTL_GL_FAST_FLUSH`, `NVMTL_GL_WAIT_EVENT`, `NVMTL_CB_LEAN`, `NVMTL_TEX_RECYCLE`, `NVMTL_ASYNCUP_REAP`, `NVMTL_BIND_CACHE`, `NVMTL_DS_SKIP`, `NVMTL_SURFIN_SKIP_OWN_OUT`, `NVMTL_RES2_NOWAIT`, `NVMTL_CVAR_PARALLEL`, `NVMTL_CVAR_SEED`, `NVMTL_FC_REFUSAL_MEMO`, `NVMTL_XLATE_ADDR_RETRY`, `NVMTL_ARGBUF_ARRAYS` and `NVMTL_DLSS_FG_SAME_IMAGE`. [`docs/SWITCHES.md`](../SWITCHES.md) explains each one.

## Tested

- **Release 2 on the test machine (macOS 26.7.3):**
  - 3.0.20 was built on the machine, signed with its owner's CA and installed with AMFI enforcing through AMFIAnchor.
  - It was measured sharing a window from Firefox (above).
  - 3.0.19 was installed before it: the installer passed every step, the Mac restarted to the desktop, and `debug.nvrm_pat_wc` read 1.
  - Most changes from 3.0.6 to 3.0.12 were installed and played there as they were made.
- **Builds:**
  - The 13 kits and the package were built on that Mac, and `check_package.sh` passes.
  - Every kit went through its release's kext linker (10.7 to 10.15).
  - `pick_kit.sh` picks the right part for 10.7.5, 10.13.6, 10.14.6, 12.7.6, 14.8.3 and 26.7.3.
- **Linux only:**
  - The installer's kit choice (`tools/test_pick_kit.py`).
  - The kext approval (`tools/test_kext_preapprove.py`).
  - The package, install and uninstall tests.
- **Not tested:**
  - macOS 15, and any older macOS release.
  - Any card other than the RTX 5050.
  - The kext approval on a Mac.
  - ZCULL's gain in a game.
