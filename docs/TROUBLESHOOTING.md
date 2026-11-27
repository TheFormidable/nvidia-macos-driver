# Why a machine does not work, and how to fix it

This page is for TheFormidable's fork of the driver at 3.0.20 (Release 2). Find the place your machine stops, read why,
and do the fix. Most entries of sections 2 and 3 come from logs that users of the original NullMoth project sent; those
under "What 3.0.1 to 3.0.20 added" and "The new paths of release 3.0" come from the fork's one test machine (an RTX 5050
on macOS 26.7.3) and from the code.

Before anything else, note the installed version (`cat /Library/NullMoth/driver-version`) and keep a way back you have
tried: the package of the driver you installed over, and `-nvoff` (section 4). Do not install another development
build just to troubleshoot without one. Every change and its way back is in [CHANGELOG.md](../CHANGELOG.md) and in the
[README](../README.md#what-is-on-by-default-and-the-ways-back); what to check on a card that runs the driver for the
first time is in [TESTING-ON-HARDWARE.md](TESTING-ON-HARDWARE.md); what has run on a card is in [STATUS.md](STATUS.md);
telemetry and guarded fan control are in [NVMETRICS.md](NVMETRICS.md).

## Reporting a problem

If yours is not here, open an issue on the
[fork's issue tracker](https://github.com/TheFormidable/nvidia-macos-driver/issues) (not the original project's: its
builds differ) and attach:

- the version (`cat /Library/NullMoth/driver-version`), `sw_vers`, the card, and your `boot-args` (from OpenCore's
  `config.plist`, or `nvram boot-args`);
- the whole output of `sudo ./install.sh`, copied from Terminal;
- `sysctl debug.nvrm_switches debug.nvrm_switches_nondefault`, and `sysctl debug | grep -e nvrm -e nvaccel` taken
  while or right after the problem shows;
- the kernel log of that boot, `log show --last boot --predicate 'process == "kernel"' > ~/kernel.txt`, and the
  plugin's failure lines, `log show --last boot --predicate 'eventMessage CONTAINS "NullMoth:"' > ~/nullmoth.txt`;
- `/Library/NullMoth/lastgood.log` and `/Library/NullMoth/bootguard.log` when they exist, and the lines the entry
  below asks for;
- which way back you tried, and whether the problem went away with it.

Read the files before you post them: logs and paths carry your account name and your Mac's name. The full list is in
[TESTING-ON-HARDWARE.md](TESTING-ON-HARDWARE.md), "What to attach to an issue".

---

## 1. Building from source

Problems building the driver are in [BUILDING.md](BUILDING.md), "When a build fails"; three more are here:

- `./build.sh deps` prints rustup's warning "default toolchain implicitly overridden" twice. That is expected: build.sh
  installs Rust with no default toolchain and names the pinned version itself. Nothing to do.
- The translator's step stops with `STOP: translator has ... compiler warnings, more than its ceiling of 0`, after a
  warning that stripping debug info with `rust-objcopy` failed. build.sh tells cargo not to strip, but a cargo
  configuration outside the checkout can bring that strip back for the translator's build scripts and procedural macros:
  a `[profile.release.package."*"]`, `[profile.release.package.<name>]` or `[profile.release.build-override]` table with
  its own `strip`, in a `.cargo/config.toml` of any folder above the checkout or the working directory (your own
  `~/.cargo/config.toml` when either is inside your home folder). `./build.sh doctor` names every cargo configuration
  file it finds in those folders; take the `strip` out of that table. A `strip` in `[profile.release]` itself does not
  do this.
- After `./build.sh test` on a Mac, `~/Library/Logs/DiagnosticReports` holds about two dozen new crash reports of small
  programs with names such as ca, cb, cc, ck, ta, tb, td, oldv, resset and t. That is expected: the tests' controls
  make these programs fail on purpose (an abort or a failed assert that the control expects), and macOS records each
  one. Every run of the host tests on a Mac leaves them; they are harmless and can be deleted.

## 2. Starting macOS

A start that stops before the driver loads (the firmware's memory map, `EXITBS:START`, the Mac model) is OpenCore's to
fix: see the [OpenCore Install Guide](https://dortania.github.io/OpenCore-Install-Guide/). To tell whether the driver is
involved, start once with `-nvoff` (section 4).

| What you see | Why | Fix |
|---|---|---|
| Stops at "PCI configuration", or pauses there | The first start with the driver can pause for up to a minute at "PCI configuration end" while the GPU comes up. A stop for good is usually the Resizable BAR or Above 4G Decoding settings. | Give the first start a minute. In the BIOS: Above 4G Decoding on, CSM off. If it still stops, report it with a photo of the whole screen with `-v` in boot-args. |
| The macOS install freezes about 19 minutes in (Caps Lock does nothing), Ryzen 7000/8000/9000 | These boards hang when the installer writes firmware settings. | `config.plist` → Booter → Quirks → `DisableVariableWrite` = true. Without emulated NVRAM, macOS's NVRAM writes may then not last, so the driver's boot guard cannot count failed starts and the `nullmoth-remove` flag of section 4 may not hold: keep `-nvoff` ready in `config.plist`. |
| Loops at `AppleKeyStore` after installing the driver, GTX 16 / RTX 20 (Turing) | The card's firmware needs about ten seconds after the BAR1 move before the display is armed. | The driver waits ten seconds on Turing by itself; `nvrmsettle=<milliseconds>` in boot-args replaces that wait. If it still loops, report it with the card's PCI ID. |

## 3. After installing the NVIDIA driver

| What you see | Why | Fix |
|---|---|---|
| The desktop shows for about a minute, then black with only the cursor | One minute after start-up the driver switches to showing frames straight from the card's memory. That is blank when the desktop is drawn on the Intel/AMD integrated GPU, or when the card's BAR is the small 256 MB window. | The driver keeps the copy path on those machines. If it still happens, add `-nvrmnoflip` to boot-args and report it. |
| Black or flickering screen at 100–240 Hz, or after changing resolution or refresh rate | Fast modes above DisplayPort 1.4's bandwidth need compression (DSC), and macOS has to be told. | The driver sends the compression settings. If it still happens, report it with the mode, the monitor and its cable, and the `NVRM-fb:` lines of the kernel log taken right after it happens. |
| After system sleep the screen stays black and the Mac needs a hard reset (`Sleep Wake Failure`, "Darkwake Exit Failure") | The driver did not suspend or resume the GPU, so on wake it pushed to a GPU that had lost power. | Fixed by system sleep support ([SLEEP-WAKE.md](SLEEP-WAKE.md)). If you still see it, keep system sleep off (`sudo pmset -a sleep 0`, and no Apple menu → Sleep) and report it with the `NVRM-xnu: wake #` and `NVRM-fb: system wake` lines of the kernel log. |
| After wake the desktop is black with only the cursor, or frozen on the picture from before sleep | WindowServer was never told the framebuffer woke, or its VRAM was replaced at wake. | Fixed by system sleep support. If you still see it, report it with the `NVRM-xnu: wake #` and `NVRM-fb: system wake` lines. |
| The Mac wakes about every minute while asleep (`pmset -g log`: "DarkWake ... due to XDCI ... XHCI") | The firmware lets USB wake the system (GPE 0x6D). Not the driver. | Rename `GPRW` to `XPRW` in `ACPI → Patch` with `SSDT-GPRW`. Then only the power button wakes the Mac. |
| Kernel panic as the login screen appears (WindowServer, `IOFramebuffer::postOpen`, page fault in `memcpy`), with WhateverGreen installed | WhateverGreen copies the boot screen on the first framebuffer start from an address it saved at boot. macOS can free that mapping before the NVIDIA display opens (seen on a laptop whose boot screen is on the Intel GPU). | Add `gfxrst=4` to boot-args. It turns that copy off; the driver draws its own start-up screen. The installer adds it to the NVRAM boot-args when WhateverGreen is loaded and none is set, but an OpenCore that writes its own boot-args at every start (the README's settings) replaces that: add it in `config.plist`. |
| WebGL or WebGPU in Safari is slow or drawn in software, while Chromium browsers and Firefox use the GPU | Safari's GPU process (`com.apple.WebKit.GPU`) is sandboxed and cannot load the Metal plugin; its log says `file system sandbox blocked mmap()` of `NVMTLDriver`. | Use a Chromium browser or Firefox for GPU-heavy pages. An optional copy of the plugin on the Preboot volume (`sudo env NULLMOTH_SAFARI_GPU_COPY=1 ./install.sh`) exists, but whether Safari's sandbox lets it load is not known ([SAFARI-WEBGL.md](SAFARI-WEBGL.md) says how to check). |
| A problem that other Macs with the same card do not have | The Mac may still carry a boot argument or a setting from an earlier test: an opt-out of a default path, a test hook, a development switch. | `sysctl debug.nvrm_switches` lists the driver boot arguments set beside the install line, with a `!` on each one an ordinary install does not need; `sysctl debug.nvrm_switches_nondefault` should say 0. Remove the marked ones from `boot-args` and restart. Settings in `/Library/GPUBundles/nvmtl/nvrm610.conf` are not in that list: look at the file too. From a checkout, `python3 tools/switch_registry.py doctor` also reads the settings file and names entries that are misspelled or that this version no longer reads. [SWITCHES.md](SWITCHES.md) says what each one does. |
| RTX 50: video in Safari, QuickTime or FaceTime is green, or an H.264 video there does not play | These apps decode in a system service. The driver's own H.264 decoder gives them a green picture on RTX 50; the newer decoder is used there only after the card passes a short decode test, and is not proven on every stream. There is no VP9 or AV1 hardware decode on RTX 50. | In Terminal: `log show --last 10m --predicate 'eventMessage CONTAINS "decode self-test"'` after playing a video. `PASS` and a broken video: add `NVMTL_NVENCVT=0` to `nvrm610.conf` to go back to the old decoder, and report the line with the video's details. `FAIL at ...`: the old decoder is in use (green); report the line, it says what the card refused. A line `... is a build without the Blackwell decode self-test - REFUSED on this card`: the codec library installed beside the plugin is older than the plugin; build and install both from the same revision. `log show` always prints its header and one line of its own (the `log` command matches its own words): only the `NullMoth: nvencvt:` lines are the driver's. A video that was already starting while the test ran (the first one after a restart, typically) can still be on the old decoder: reload it once before judging. |

### Several NVIDIA cards

If several NVIDIA cards attempt startup, or a custom kernel collection loads none of
the driver components, see [selecting one GPU](MULTI-GPU.md). The original project boot-tested the workaround
on four RTX 4090s with 1.14.7 and macOS 15.8.1; it restricts NVRM/NVAccel to the display card
and rebuilds the final collection from permanent installation paths.

### What 3.0.1 to 3.0.20 added

Everything these versions added is on by default; nothing has to be turned on. `cat /Library/NullMoth/driver-version`
says which version is installed. Each entry's fix is the way back of that one change (the whole list is in
[README.md](../README.md#what-is-on-by-default-and-the-ways-back)): a boot argument goes into OpenCore's `boot-args`
and needs a restart; a settings line goes into your own block of `/Library/GPUBundles/nvmtl/nvrm610.conf` (between
`# --- my settings, kept on update ---` and `# --- end my settings ---`), from the first column, and acts on the
applications started after it. Apart from the sound entry, these entries come from one machine (an RTX 5050 on macOS
26.7.3) and from reading the code, not from logs people sent.

| What you see | Why | Fix |
|---|---|---|
| macOS 26: no speakers, headphone jack or microphone; only the NVIDIA HDMI/DisplayPort outputs appear | macOS 26 has no AppleHDA, so AppleALC does nothing there, and the installer's audio driver (NMXAudio) leaves the onboard sound alone while boot-args carry `alcid=…` or `alcverbs`. | Remove both from `boot-args` in `config.plist` and restart ([README.md](../README.md#opencore-settings)). The installer prints a note when it finds either. |
| An OpenGL application draws wrong, shows an empty window or does not start | On macOS 15.5 or later OpenGL runs on NVK through Zink instead of Apple's OpenGL over Metal. | Restart once with the boot argument `nvzink=0`, which gives every application Apple's OpenGL. If the problem goes, report the application and its `NVK Zink:` lines ([ZINK-OPENGL.md](ZINK-OPENGL.md), "The way out"). `ioreg -l -w0 \| grep IOGLBundleName` says which driver the accelerator names (`NVKZinkGLDriver` for Zink). |
| An OpenGL game is correct but stutters, or shows frames unevenly with vertical sync off | How a swap on Zink reaches the window changed in 3.0.7 (one frame ahead of the GPU), 3.0.8 (the opacity answer is kept) and 3.0.11 (with the swap interval at 0 a sender thread shows the newest frame). | In the settings file, one at a time, restarting the game after each: `NVK_ZINK_PRESENT_NEWEST=0`, then `NVK_ZINK_OPACITY_KEEP=0`, then `NVK_ZINK_PRESENT_AHEAD=0`. Report which one changed it. Vertical sync on in the game also takes the 3.0.11 path out. |
| A game on the Vulkan runtime (Minecraft's Vulkan renderer) stutters or shows frames unevenly with vertical sync off | Since 3.0.9 an IMMEDIATE swapchain, and since 3.0.11 a MAILBOX one, hands its frames to a presentation thread that shows the newest. | `NVK_BRIDGE_PRESENT_NEWEST=0` in the settings file and restart the game: every present waits on the game's thread, as in 3.0.8. The game's log then has `NVK Bridge: swapchain presentation=synchronous` ([INSTALL.md](../tools/nvk-bridge/INSTALL.md), "Going back"). |
| With vertical sync off a game stands at exactly twice the display's refresh rate (200 frames a second on a 100 Hz display), or another multiple of it | Through 3.0.8 the Vulkan runtime, and through 3.0.9 OpenGL on Zink, waited in every present for the window's next drawable. | Install 3.0.9 or later for the Vulkan runtime and 3.0.11 or later for OpenGL. The two changes are in those two frontends only: an application that presents through Metal itself (a Metal game, MoltenVK, Apple's OpenGL with `nvzink=0`) is not changed by them. |
| An OpenGL program reads old bytes from a buffer it keeps mapped right after `glBufferSubData`, or an OpenGL game draws wrong since 3.0.16 | Since 3.0.16 a `glBufferSubData` on Zink that would wait for the GPU goes through a staging buffer and a copy on the GPU. Into a buffer the application keeps mapped, the bytes then arrive when the GPU runs the copy, not when the call returns; the GL asks for a fence before such a read. | `NVK_ZINK_SUBDATA_STAGE=0` in the settings file and restart the application: uploads run as in 3.0.14. Report the application. |
| A game shows things through walls, or depth that flickers, since 3.0.17 | NVK uses the GPU's hierarchical Z (ZCULL) in every process but WindowServer. No game result on a card has been recorded yet. | `NVK_ZCULL=0` in the settings file and restart the game. Started from Terminal with `NVMTL_PROCESS_ENV=1 NVK_ZCULL_LOG=1` in front of its command, NVK prints `nvrm: zcull on (...)` or `nvrm: zcull off: <reason> (...)`: report that line with the game. |
| AMD machine: `sysctl debug.nvrm_pat_wc` reads 0, and uploads of buffers and textures are slow | AMD_Vanilla's "Fix PAT" kernel patch leaves the CPU's page attribute table without a write-combining entry, so what the CPU writes into video memory leaves it one store at a time. | 3.0.13 puts the entry back by itself at start and after each wake: `sysctl debug.nvrm_pat_found debug.nvrm_pat_wc debug.nvrm_pat_why debug.nvrm_pat_passes debug.nvrm_pat_cpus hw.ncpu` then shows `nvrm_pat_wc` 1, at least one pass and as many CPUs as `hw.ncpu`. Through 3.0.12 the only fix was the patch's own byte in OpenCore. |
| An AMD machine hangs or freezes at start, or misbehaves after a wake, after an update to 3.0.13 or later | NVRM writes the page attribute table on every CPU at once, with interrupts off, at start and after each wake (the row above). That rewrite has not yet run on a machine whose entry 6 is not write-combining. | Boot argument `nvpatwc=0`: the table is left as the kernel wrote it, as in 3.0.12. After two failed starts the boot guard turns the driver off by itself (section 4). Report the sysctls of the row above, after a start and after a wake. |
| `install.sh` spends minutes at "DLSS 4 / 4.5 files", prints that the DLSS files were not installed, or prints `this package's SHA256SUMS does not cover the DLSS step; skipping it` | Since 3.0.13 the installer downloads NVIDIA's DLSS files by default (about 750 MB the first time) and builds their kernels for the card. This fork's packages before 3.0.19 skipped the step with that line: the installer looked for checksum lines spelled differently from the ones the packages write. | A failure there leaves the driver installed; run the installer again later to retry, from 3.0.19 or later. `sudo env NULLMOTH_DLSS=0 ./install.sh` skips the step, and files already under `/Library/NullMoth/dlss` stay in use. |
| After a first install, macOS asks to allow the extensions in System Settings → Privacy & Security | Since release 2 the installer records the driver's kexts as allowed in macOS's kext policy database, so one restart is enough. Where SIP keeps that database read-only it cannot, and macOS asks as before. This has not been tried on a Mac yet. | Allow the extensions there and restart once more. The installer's last message says which case happened (`macOS's approval database was read-only here, so it will ask ...`). Report it with your `csr-active-config`. |
| On macOS 10.7 to 14 the installer stops with `this package carries no kit for macOS ...` and `no changes made` | The package was built without the kits for older releases (`NM_LEGACY_KITS`). | Use a package that carries the 13 kits, as the release package does. No kit has run on a card yet: report what the kit's own installer printed and what happened after the restart. |
| The first start of a game after a driver update loads more slowly than before | A new build of NVK has a new build id, so the shader caches of the version before are not reused and every shader is compiled once more. | Nothing to do: the next start finds the new caches. |
| The desktop and a game lose most of their frame rate while the screen is shared (Google Meet, Discord) | Before 3.0 every captured frame cost WindowServer a fill of new video memory from the processor and a wait for the display. Through 3.0.19, while a program read the captured frames with the processor (Firefox, OBS, Zoom), WindowServer also waited for each frame's copy behind the accelerator lock. | Fixed in 3.0 and in 3.0.20. `sysctl debug \| grep nvrmfb_vram_` has the counters of the first; the boot argument `nvfbvramfast=0` goes back to the old path for a comparison. |
| Since 3.0.20 a screen share or recording from Firefox, OBS or Zoom shows a wrong or old frame, or the desktop freezes while it runs | A page-off now reads the GPU's memory with the copy engine without the accelerator lock, and writes it into the surface only when nothing it read from changed meanwhile. | Boot argument `nvaccel_pageoff_copy_locked=1`: the copy keeps the lock, as in 3.0.19. Report it with `sysctl debug \| grep nvaccel_pageoff` and whether the way back changed it ("The new paths of release 3.0", the freeze while recording, has more). |
| Your own preview of a screen share in Google Meet moves at about 5 frames a second in Firefox or Safari, while Chrome or Brave is smooth | Meet asks 5 frames a second of every browser whose user agent does not say Chrome ([Mozilla bug 1983227](https://bugzilla.mozilla.org/show_bug.cgi?id=1983227)); the driver gives the page what it asks. Other pages are not limited: the WebRTC getDisplayMedia sample runs smoothly in both. | Firefox: the add-on "User-Agent Switcher and Manager" (recommended by Mozilla), set to send a Chrome user agent on `meet.google.com` only. `general.useragent.override` in `about:config` works too, but for every site. Safari has no per-site setting: Develop > User Agent > Google Chrome changes it for one tab. Measured on the RTX 5050 on 2026-11-26. |
| With AMFI left on (the optional AMFIAnchor route), the desktop has no acceleration, or the boot's log has `Failed to create MetalDevice` or `mapping process is a platform binary` | WindowServer did not trust the plugin: the anchor did not load, or a graphics bundle is not signed by your CA. The route is experimental and qualified on one machine only. | `ioreg -r -c AMFIAnchor -l` should report `InstallStatus = 1` with your anchor in `LoadedAnchors`. To go back, start from the EFI that keeps `amfi_get_out_of_my_way=0x1 amfi=0x80`. The steps are in [SIGNED-GRAPHICS-EXPERIMENT.md](SIGNED-GRAPHICS-EXPERIMENT.md). |

### The new paths of release 3.0

Every change of 3.0 is on by default. Each risky change has one way back to the 2.9 behaviour (the table "The ways
back" in [CHANGELOG.md](../CHANGELOG.md)); several default-on changes have none, and the way back from those is to
reinstall 2.9 (and `-nvrmnopm` / `-nvrmnoshutdown` for the power paths). Use a way back to find out whether a new path is behind a problem, then report what
you saw with it and without it. A boot argument needs a restart; a line in `nvrm610.conf` goes into your own block at
the end of the file and acts on the processes started after it (log out and in for WindowServer).

These entries come from the code and from the fork's one test machine, not from logs people sent. They say what the
driver reports and what each line means. A line quoted here as in `nvmtl.log` is written by an
installed (release) plugin only for a process started with `NVMTL_PROCESS_ENV=1`; for every other process, WindowServer
included, only the plugin's failure lines reach the system log, with the prefix `NullMoth:`
([TESTING-ON-HARDWARE.md](TESTING-ON-HARDWARE.md), "Where the driver writes").

**The desktop came up later than you expect.** `sysctl debug | grep nvrm_boot_` (or the `nvrm-boot-timing` property of
NVRM in IORegistryExplorer) gives every phase of the start-up in milliseconds and says why the wait for the displays
ended (`debug.nvrm_boot_heads_why`; 7 is the 20 s bound: no display was reported in time). To compare with the waits
of 2.9, boot once with `-nvrmoldwaits` and report both sets of numbers.

**The card did not start: no desktop comes up on it (the screen keeps the last picture of the start, or is black), or
the Mac restarted by itself and is on the previous driver again, or a desktop came up slow and without acceleration
right after an install.** In the first case reach the Mac over `ssh`; in the third, open Terminal on that desktop: the
kernel log has `NOT STARTING THE GPU` with the reason. "NVRMFB cannot drive a display on this macOS" means the
framebuffer's Apple classes differ from the ones it was built for (`nvrm-layout` and `nvrmfb-layout` on the PCI device
say the same; the next item has what follows from it, the restart by itself included). A reason about DMA means a DMA
mapper (VT-d) translates the card and the driver may not or cannot use it (`nvrm-dma` names it; see below). Neither
counts as a failed start, so neither turns the driver off. A framebuffer refusal that NVRM reads only after it has
started is logged as `auto-go: NOT STARTING THE GPU: the framebuffer refused this macOS's classes after NVRM had
started (...)`. That start stays counted; what follows is the same as for the refusal read before the start.

**`nvrmfb-layout`, `nvagdc-layout` or `nvaccel-layout` says refused, or no acceleration after a macOS update.** Check
with `ioreg -l -w0 | grep -E '"(nvrmfb|nvagdc|nvaccel|nvrm)-layout'` and `sysctl debug.nvrmfb_layout
debug.nvrmfb_layout_classes debug.nvrmfb_layout_vtables debug.nvrmfb_layout_vtdiff debug.nvrmfb_agdc
debug.nvrmfb_agdc_layout debug.nvaccel_layout`. On a good boot `nvrmfb-layout` reads `ok: 3 classes, vtables not
compared; macOS ...` and `nvagdc-layout` reads `ok: 1 class, vtables not compared; macOS ...`. A line that starts
with `refused:` names the class and both sizes: that macOS lays the class out differently from the headers the driver
was built with, and the kext did not start on it so that nothing writes over Apple's memory. With NVRMFB refused,
NVRM does not start the GPU and the driver shows nothing on the card, at that start or at any later start of this
driver: every framebuffer of the driver is built on those classes, the one that shows a desktop with the driver off
included (section 4). With the OpenCore setup of README.md, which excludes Apple's IONDRVSupport, no desktop comes up on
the card; the Mac can be reached over the network. Where OpenCore does not exclude IONDRVSupport, Apple's own firmware
framebuffer takes the card instead and shows a desktop on the firmware's picture: one resolution, drawn by the
processor, no acceleration, so it comes up slowly and everything on it is slow (when the firmware's picture no longer
lies inside the card's memory window, the screen stays dark there too).
`ioreg -r -c IOFramebuffer -w0 | grep -o '<class [A-Za-z]*'` then names `IONDRVFramebuffer` instead of the driver's
`NVRMNVDAFramebuffer`. After an install over an earlier driver, `install.sh`'s fallback job looks for this refusal every
five seconds during the first four minutes of a start; when it finds it, it copies the `nvrm-layout` and `nvrmfb-layout`
lines into `/Library/NullMoth/lastgood.log` and, while the Mac still runs the macOS build the install was made on, puts
the earlier driver back and restarts. On another build it only writes those lines: the earlier driver ran on the build
of the install, and after a macOS update the refusal may be the update's doing. Where another GPU shows a display
and a user is logged in when it has put the driver back, it leaves the restart to that user: the log then ends with
`previous driver restored. A display is lit and a user is logged in, so the Mac is not restarted now`, and a restart
brings the previous driver back. A display on this card does not hold the restart back, since only Apple's firmware
framebuffer can draw it then: after an automatic login onto Apple's slow desktop the Mac restarts as well, and the
log's last line says `The only lit display is on the card whose driver was refused ... restarting`. A slow desktop
without acceleration that stays after an install while the log does not end with the `not restarted now` line means
that nothing was put back: the log says why (another macOS build, or the backup is gone), or there is no such job.
If `IONDRVFramebuffer` holds the card while `ioreg -l -w0 | grep -e nvrm-layout -e nvrmfb-layout` prints an
`nvrmfb-layout` line that says `refused` and no `nvrm-layout` line, the framebuffer refused after NVRM had already
started the card, and the fallback job does not see that refusal. Where no other GPU has a display, the boot-guard job
does not count such a start as good (`/Library/NullMoth/bootguard.log` ends it with `... keeps its count`), so the
driver turns itself off at the third start, and the fallback job, while it is still installed, then puts the earlier
driver back and restarts. With a display on another GPU, or without the job, put the earlier driver back by hand:
install its own package again (the installer's last lines named the folder that keeps it). The fallback job takes itself
away at the first start after the one in which it found the display lit (a second run of install.sh before such a start
keeps the first run's backup as the way back), and a first install has none: a refusal then (after a macOS update that
changed a class, for example) ends every start the same way, without a desktop on the card or on Apple's slow one, until
you boot with `nvlayout=0` or take the driver out (section 4). With AGDC refused the display runs without
AppleGraphicsDeviceControl; with the accelerator refused (`NVAccel: Apple class layout refused: ...` in the log) the
desktop runs without acceleration; if instead WindowServer keeps dying there (a black screen with only the pointer) and
every display is on this card, the boot-guard job turns the driver off and restarts once from the login window, and the
fallback job, while it is still installed, puts the earlier driver back at that next start; with a display on another
GPU the boot-guard job leaves that loop alone.
Report the line (after a restart by itself, `/Library/NullMoth/lastgood.log`) with `sw_vers`. `nvlayout=0` in the
boot arguments starts them anyway (the check still runs and logs `overridden (nvlayout=0): ...`), at the risk the
check exists for (a crash or a frozen desktop); use it only to confirm. `nvlayout=0` changes the verdict, not the
looking: the kexts read Apple's class tables either way (the boot argument is read afterwards, only to decide what to
do with what they found), and the firmware framebuffer that shows the desktop with the driver off reads the same
tables, so `nvlayout=0` is not a way back from a crash that happens while those tables are read; the way back from
such a crash is to take the driver out and put the previous one back (section 4). `nvrmfb-layout-vtables` and
`nvaccel-layout-vtables` never stop anything: a `differ:` or `not compared:` line on a Mac whose display works is
worth a report with the macOS build.

**VT-d (a DMA mapper) is on.** `sysctl debug.nvrm_dma_mode` says which case the machine is in
([DMA-IOMMU.md](DMA-IOMMU.md)).
- `NVRM-xnu: DMA: refused: ...` in the log, `debug.nvrm_dma_mode` = 3: VT-d translates the GPU and the driver did not
  start it: either `nvrmdma=0` is in the boot arguments, or the two-page self-test through the mapper did not read
  back or gave the pages' physical addresses (nothing translated; the `NVRM-xnu: DMA self-test failed` line before it
  says which). The driver will not run the GPU on untranslated addresses behind VT-d (that is what made 2.9 fail on
  such machines). Turn VT-d off in the firmware (or drop the DMAR table in OpenCore, `ACPI → Delete`) to get mode 0, and report the
  log line.
- Desktop broken or GPU errors in mode 1 or 2: report `sysctl debug | grep -e nvrm_dma -e nvaccel_dma` and any AppleVTD
  fault lines naming the GPU (`log show --last boot | grep -i vtd`). Boot once with `nvrmdma=0`: if that gives a clean
  "GPU not started", the mapping code is at fault, not VT-d itself. Way back: VT-d off.
- Windows do not update after a screenshot or a screen recording: `debug.nvaccel_dma_mode` 1 or 2 with
  `debug.nvaccel_dma_map_fail` or `debug.nvaccel_dma_refused` rising means NVRM could not map the accelerator's buffer;
  `debug.nvaccel_dma_family` rising means the family's own copy ran instead.

**Frames look worse than in 2.9: stutter, tearing, a window that updates late or shows an old frame.**
- First, is the surface fence in use? `sysctl debug.nvrm_fence_state debug.nvrm_fence_accel debug.nvrm_fence_map_fail
  debug.nvrm_fence_map_mismatch debug.nvrm_fence_owners_distrusted`. State 1 and accel 1 with the other three at 0 is
  the normal case. State 0: `nvrmfence=0` is in the boot arguments. State 2: the fence switched itself off because
  eight connections to the fence were distrusted since boot for holds they did not end; state 3 after a wake: the
  GPU did not resume. In both the driver runs as 2.9 until the next boot; report the `NVRM-xnu: fence:` lines of
  `log show --last boot`.
- Boot once with `nvrmfence=0`. If that cures it, report `sysctl debug | grep -e nvrm_fence -e nvaccel_surface` from the
  boot without the boot argument: `wait_ge100ms`, `expired_gpu`, `expired_cpu`, `chan_noirq` and `done_on_slice` say
  whether readers waited long for a slow writer, a writer did not finish, or interrupts did not arrive for a channel.
  With `nvrmfence=0` NVAccel logs `surface fence: off for this boot (boot-arg nvrmfence=0)` once; that is expected.
  The line `surface fence: not offered by NVRM (yet)` without the boot argument means NVRM.kext is older than
  NVAccel.kext, or the two are of different releases. `nvrmfence=0` does not turn off the event wait or the unlocked
  page-off; they have their own ways back (`NVRM_EVENT_WAIT=0`, `nvaccel_pageoff_locked=1`).
- `NVRM-xnu: fence: pid N: ... - this channel is not watched` in the log: the kernel could not wire a page of that
  process's GPU channel, or the page is not system memory (it lies in one of the GPU's own memory windows, or beyond
  installed memory + 4 GB), or its view of the page did not follow what the process wrote. That process uses 2.9's
  marks and tags; everything else is unaffected. Report the line: which of them it was decides the fix.
- In the plugin's log (`nvmtl.log`): `surface fences: off (...)` means the plugin uses the 2.9 busy tags and
  pending marks, and the words in the parentheses say why: `the kext's fence is not on for this process: state 0` with
  `nvrmfence=0` (state 3 or 2 when the fence stopped or switched itself off, state 1 with NVK's `NVRM_ASYNC=0`), `NVK or
  the kext answered 0x80000002: no fence there` with a kext older than the plugin, `request N answered off` when a
  later request was refused that way. With an NVK older than the plugin there is no such line at all. Nothing to do
  unless the fence was expected to be on. Many `surface fences: hold refused
  (0x80000007)` lines in one process mean a Vulkan channel of that process is not watched by the kernel: that process
  keeps the 2.9 mechanism; report the `NVRM-xnu: fence:` kernel lines.
- In an application's output: a line starting `nvrm: surface fence:` means that process is not using the surface
  fence; the line says why (`no connection to the kext`: NVK could not open the kext at all; any other reason: the
  kext has the fence but not for that process or channel). Report it with `sysctl debug.nvrm_fence_state
  debug.nvrm_fence_map_fail debug.nvrm_fence_map_mismatch`. No such line does not mean the fence is in use: with a
  kext older than the fence nothing is printed unless `NVK_DIAG=1` is set in `nvrm610.conf`, and then
  `nvrm: surface fence: the kext has none` is in `/private/tmp/nvk-diag-<pid>.log`. `debug.nvrm_fence_state` says
  whether the loaded kext has it.

**The desktop freezes while recording the screen, taking screenshots or showing window thumbnails.** Boot once with
`nvaccel_pageoff_locked=1` added to the boot arguments. If the freeze is gone, the page-off wait without the
accelerator lock is the cause: please report it with the output of `sysctl debug | grep nvaccel_pageoff` and the
kernel log lines `NVAccel: page-off: ...`, and keep a spindump of WindowServer and kernel_task taken while it is
frozen. `debug.nvaccel_pageoff_unlocked_standdown` = 1 without the boot argument means the driver already turned that
path off for the boot; the log line says why. Since 3.0.20 the page-off's copy also reads the GPU's memory without
the lock; `nvaccel_pageoff_copy_locked=1` keeps only the copy under it, as in 3.0.19, and is the first thing to try when
the freeze or a wrong frame comes with a program reading the capture (Firefox, OBS, Zoom).

**Screen sharing, recording or video shows wrong colours, a green or purple cast, a frame from a moment ago, or
frames that flicker between two.** Boot once with `nvaccel_surf_planes=0` and say whether it goes away. Before that,
save for the report `sysctl debug | grep -E 'nvaccel_(surf_plane|surfvram_ranged|pageoff_ranged)'`, the kernel log lines
`NVAccel: surfvram: surface ... ranged ...` and `NVAccel: surfvram pageoff: res ... plane ...`, and the
`planar surfaces:` lines of `nvmtl.log`.

**Applications use more CPU or stutter, with frame times between 1 and 3 ms.** Try `NVRM_EVENT_WAIT=0` in
`nvrm610.conf` and relaunch the application: that restores polling for every process, WindowServer included (which
then uses more CPU than before). If it helps, report it with `sysctl debug.nvrm_evt_woken debug.nvrm_evt_timeouts
debug.nvrm_evt_user_missed debug.nvrm_evt_user_fallbacks` taken while the application ran.
`debug.nvrm_evt_user_missed` is the number of full 2 ms sleeps that ended on their timeout although the GPU work was
already done; a few of them are normal (the GPU shows a completion a moment before its event arrives).
`debug.nvrm_evt_user_fallbacks` counts each time a process went back to polling because its wakes stopped arriving
(the line `nvrm: event wait OFF for this process (16 wakes in a row did not arrive; it is tried again now and then)`
in its output). The process tries the event again and, when its wakes arrive, prints `nvrm: event wait back ON for
this process` and sleeps on it again. A rise is not the expected answer to a display mode change, a display waking or
a wake from sleep (the event is posted without the GPU lock, so a long RM call does not hold it back): report it, and
say what the machine was doing. A counter that keeps rising while `debug.nvrm_evt_woken` stands still means the event
is not being posted.
`NVRM_GSUB_LOG=1` in `nvrm610.conf` prints, when a channel closes, how its waits went. [EVENT-WAIT.md](EVENT-WAIT.md)
explains the numbers.

**A window or game glitched once (a black frame, a missing frame, a stutter) and went on.**
- A line `nvrm: channel slot ... rebuilt` in an application's output, or `nvk-reason: W ...: nvrm channel slot ...
  rebuilt` in `nvmtl.log`, means the GPU faulted on one of that application's channels and NVK recovered it; the
  frames that were on the GPU then may be missing. A line `not rebuilt` names the kind of loss and the reason; that
  application lost its device as in 2.9. To rule the recovery out, put `NVK_CHANNEL_RECOVER=0` in `nvrm610.conf` and
  restart the application.
- `vk: after a GPU fault, a channel rebuild dropped this command buffer` in `nvmtl.log`: one frame (or the frames in
  flight) of that process was lost. Report the line and the `fault context:` lines after it. If the desktop froze after
  such a line in WindowServer, add `NVMTL_DROP_REPORT=0` to `nvrm610.conf` and say whether that changes it.
- `sysctl debug.nvrm_xid_count debug.nvrm_chan_recovered debug.nvrm_chan_gave_up debug.nvrm_chan_last_kind
  debug.nvrm_chan_last_why` tells, for the whole machine since boot, how many GPU faults the driver saw, how many
  channels were rebuilt and how many recoveries were given up, and for the last give-up the kind of loss and the
  reason ([CHANNEL-RECOVERY.md](CHANNEL-RECOVERY.md) has the numbers). On a healthy machine the `debug.nvrm_chan_*`
  counters stay 0. Include that line in a report about a glitch, a black frame or an application that lost its GPU.

**The memory of a game grows over a long session, or a game faults on a texture it has just released.** The plugin
keeps a released texture until no command buffer that could read it is open or in flight. The log says how much it
holds: `vk: image grace: N dead images released so far, M still held` (every 4096 releases) and `vk: image grace: N
dead images held at once (most so far; K command buffers open)` (each time the most held doubles, from 256). M should
stay small (what one or two frames release) and fall back near 0 when the game is idle. A line `vk: image grace: N
dead images released 1024 submits after their death with a command buffer from before it still open` means a command
buffer stayed open for a long time (an application may keep one uncommitted); please report it with the log. To rule
the grace out, put `NVMTL_IMAGE_GRACE=0` in your own block of `nvrm610.conf` and restart the application: textures are
then freed at once, as in 2.9.

## 4. Getting back in

- **Start without the driver:** add `-nvoff` to `boot-args` in OpenCore's `config.plist` and restart. NVRM then leaves
  the card alone and the firmware's picture comes up; take `-nvoff` out again to get the driver back. The README's
  OpenCore settings list `boot-args` under `NVRAM → Delete`, so OpenCore writes them at every start and changing them
  with `nvram` does not last: `-nvoff`, or a boot argument taken away, has to go into `config.plist` (from another
  system, or from Recovery's Terminal with the EFI partition mounted). Keep a USB stick or a second EFI with `-nvoff`
  ready.
- **Two failed starts turn the driver off by themselves:** two starts in a row whose display never came up turn the
  driver off at the next one, as `-nvoff` does; after an install over an earlier driver, `install.sh`'s job then puts
  that driver back and restarts. A start whose desktop comes up with no NVIDIA driver running, or with the card
  started, nothing lit and no display on another GPU, does not count as good (`/Library/NullMoth/bootguard.log` says
  which), so it does not end the install's trial. Where NVRAM writes do not last (section 2) the guard cannot count.
- **Keep the driver off from Recovery or over the network:** when you cannot edit `config.plist`, boot into macOS
  Recovery (the driver cannot run there), open **Terminal** and run
  `nvram 7C436110-AB2A-4BBB-A880-FE41995C9F82:nullmoth-remove=1`, then restart into macOS. Over the network (Remote
  Login on, from another computer) the same is `sudo nvram 7C436110-AB2A-4BBB-A880-FE41995C9F82:nullmoth-remove=1`,
  then `sudo shutdown -r now`. The flag keeps every part of the driver off at each start (the firmware's picture)
  until `sudo nvram -d 7C436110-AB2A-4BBB-A880-FE41995C9F82:nullmoth-remove` or the next run of `install.sh` clears
  it. It does not remove the driver: remove or replace it from that desktop (next item).
- **Remove or replace the driver:** from the package folder, `sudo ./uninstall.sh && sudo shutdown -r now`
  ([README.md](../README.md#dual-boot-and-uninstall)); `uninstall.sh` removes the driver and restores nothing. To go
  back to the driver from before, install its own package again: the installer's last lines name the folder that keeps
  what it replaced.
- **When the framebuffer refused this macOS's classes** (section 3), the driver gives none of the starts above a
  picture: the framebuffer that draws the firmware's picture with the driver off is built on the same classes. They
  have a picture only where OpenCore does not exclude IONDRVSupport (Apple's own firmware framebuffer then draws it,
  slowly, when it still lies inside the card's memory window). A refusal that NVRM reads before it starts the card is
  not counted either, so those starts never turn the driver off. Over the network, put `nvlayout=0` into OpenCore's
  `config.plist` (section 3 says what that start risks), or take the driver out with `uninstall.sh`.
- Once an accelerator has started, NVAccel.kext (like NVRM.kext) holds itself loaded until the next restart: unloading
  it by hand does not work.

Each path that became the default since release 3.0 has one way back, for telling whether that path is behind a
problem. They are listed with their exact spelling and what they restore under "The ways back" in
[SWITCHES.md](SWITCHES.md) and in [CHANGELOG.md](../CHANGELOG.md). If one of them makes a problem go away, report it
with that way back set and say which one; each is reviewed at the release its `review` column in SWITCHES.md names.

## 5. GPUs the driver does not cover yet

This driver covers NVIDIA Turing and newer ([CARD-SUPPORT.md](CARD-SUPPORT.md)). GTX 10 and older (Pascal, Kepler),
AMD RX 7000/9000, Intel Iris Xe and Arc, and Ryzen AI graphics run macOS with basic display (no graphics
acceleration), and this driver does not change that.
