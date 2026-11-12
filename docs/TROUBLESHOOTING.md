# Why a machine does not work, and how to fix it

Every entry below comes from real logs people sent. Find the place your machine stops, read why, and do the fix. If yours
is not here, press **Send logs** (Mac app) or **Scan for logs and send them** (1401 for Windows) and post the report ID.

Before anything else: **use the newest 1401 and driver.** Most problems below are already fixed in a later version, and a
stick made by an old 1401 keeps the old problem until you make it again.

Problems building the driver from source are in [BUILDING.md](BUILDING.md), "When a build fails". The changes of
release 3.0 and the way back from each are in [CHANGELOG.md](../CHANGELOG.md); what to check on a card that runs it
for the first time is in [TESTING-ON-HARDWARE.md](TESTING-ON-HARDWARE.md).

---

## 1. Making the stick (1401 for Windows)

| What you see | Why | Fix |
|---|---|---|
| "HTTP Error 502: Bad Gateway" from osrecovery.apple.com | A proxy or filter on your network breaks plain HTTP to Apple. | Fixed in 1.14 (1401 retries over HTTPS). Before that: a phone hotspot or another network. |
| "stream ended at 1048576 bytes", "HTTP Error 403/405", "could not reach osrecovery.apple.com" | Something between you and Apple cut the download: antivirus web scanning, a filtered network, or Apple refusing that network. | Turn off antivirus web scanning, or use a phone hotspot or a VPN, and press Next again. Or download `BaseSystem.dmg` and `BaseSystem.chunklist` with macrecovery on any computer and put them in the 1401\Recovery folder. |
| "EFI failed validation: ValidatorUnavailable: Windows application control blocked …" | Windows Smart App Control would not run OpenCore's validator. | Fixed in 1.14: 1401 checks the config against OpenCore's own sample instead. |
| "The only graphics card Windows reports is '…', with no maker or PCI ID" | Windows has no driver for the card, so the scan could not name it. | Fixed in 1.14 when the card's name says the maker (it builds with basic display). Otherwise install the card's Windows driver, restart, and run Check this PC again. |
| "Windows error 433", "the stick stopped accepting writes", "the disk is read-only", "could not erase the stick (diskpart)" | The USB stick or its port: a loose port, a failing or fake stick, or a lock switch. | Use a USB port on the back of the PC, another stick, and slide any lock switch off. |
| "this stick changed the macOS image after it was written" | The stick did not keep what was written (a failing or fake-capacity stick). At start-up this showed as "OCB: DMG has been altered". | Use another USB stick. 1.14 reads the image back to catch this before you boot. |
| "This PC has not been checked yet", "The build has no verified machine profile", "The scan changed after the EFI was built" | The steps ran out of order. | Run **Check this PC**, then **Build**, then **Create the stick**, in one go. |
| "VMware SVGA 3D" is the only graphics card | 1401 is running inside a virtual machine. | Run 1401 on the PC itself. |

## 2. Starting the stick or macOS

| What you see | Why | Fix |
|---|---|---|
| `EB.MM.AKM`, `STOP 0x16`, "StartImage failed - Aborted", "Couldn't allocate runtime area" (often Ryzen 7000/9000) | The firmware's memory map leaves boot.efi no room for macOS. | Make the stick again with the newest 1401: it builds Ryzen 7000/9000 with the right memory settings and, if one setting fails, tries the next. Restarting an old stick changes nothing. |
| Stops at `EXITBS:START` | macOS took over from the firmware and stopped before printing anything. On Intel 12th gen and newer and on AMD laptops this is usually the CPU core layout or the memory map. | Newest 1401 first. Then send the stick's `opencore-*.txt` (1401 offers to send it when the stick is plugged in). If a kernel panic shows, take a photo of the whole screen in verbose mode. |
| "This version of Mac OS X is not supported on this platform!" with a `Mac-…` code | The Mac model in the config is not one that macOS version installs on. macOS 26 refuses iMacPro1,1, and old models are refused by macOS 15. | 1.14 builds desktops with a graphics card as MacPro7,1, which runs both macOS 15 and 26. On your own EFI, 1401's "Check your EFI" names the problem. |
| The macOS install freezes about 19 minutes in (Caps Lock does nothing), Ryzen 7000/8000/9000 | These boards hang when the installer writes firmware settings. | Fixed in 1.14: make the stick again. On your own EFI: config.plist → Booter → Quirks → DisableVariableWrite = true. |
| The stick's boot hangs right after picking it, sometimes, and works after making the stick again | The stick or port is slow or failing while OpenCore reads the 900 MB recovery image. | Use a port on the back of the PC, or another stick. |
| "OCB: LoadImage failed - Unsupported", "Failed to retrieve boot file from DMG" | The recovery image on the stick is damaged. | Make the stick again on another USB stick (1.14 checks it after writing). |
| "OS.dmg.root_hash … Err(0xE)" | A stick made by an old 1401, or a recovery image for a different macOS. | Make the stick again with the newest 1401. |
| Stops at "PCI configuration" | Usually Resizable BAR or Above 4G Decoding settings. | In the BIOS: Above 4G Decoding on, CSM off. If it still stops, send the stick logs. |
| Loops at `AppleKeyStore` after installing the driver, GTX 16 / RTX 20 (Turing) | The card's firmware needed more time before the driver turned the display on. | Fixed in 1.13 (Setup adds `nvrmsettle=10000` on Turing). |

## 3. After installing the NVIDIA driver

| What you see | Why | Fix |
|---|---|---|
| The desktop shows for about a minute, then black with only the cursor | One minute after start-up the driver switched to showing frames straight from the card's memory. That is blank when the desktop is drawn on the Intel/AMD integrated GPU, or when the card's BAR is the small 256 MB window. | Fixed in 1.14 (the driver keeps the copy path on those machines). Before 1.14: add `-nvrmnoflip` to boot-args. |
| Black or flickering screen at 100–240 Hz, or after changing resolution or refresh rate | Fast modes above DisplayPort 1.4's bandwidth need compression (DSC), and macOS was not told. | 1.14 sends the compression settings. If it still happens, press **Send logs** while it happens: the app records the display path. |
| macOS 26 (Tahoe): black desktop | The macOS 26 version of the accelerator in the package was out of date. | Fixed in 1.14. |
| No picture after switching the monitor to another input and back | The driver only brought up displays it had not seen before. | Fixed in 1.14. Before that: unplug and replug the cable. |
| Black after about 100 s on an older Intel board without Resizable BAR (B150 and similar), log says "cannot hold … not placing" | The firmware lists no 64-bit PCI range, so the card's memory window had nowhere to go. | Fixed in 1.14. |
| The monitor stays on with a frozen picture when the display sleeps | The driver did not turn the display off. | Fixed in 1.14. |
| After system sleep the screen stays black and the Mac needs a hard reset (`Sleep Wake Failure`, "Darkwake Exit Failure") | The driver did not suspend or resume the GPU, so on wake it pushed to a GPU that had lost power. | Fixed by system sleep support ([docs/SLEEP-WAKE.md](SLEEP-WAKE.md)). Before that: keep system sleep off (`sudo pmset -a sleep 0`) and do not use Apple menu → Sleep. |
| After wake the desktop is black with only the cursor, or frozen on the picture from before sleep | WindowServer was never told the framebuffer woke, or its VRAM was replaced at wake. | Fixed by system sleep support. Send logs if you still see it: they hold the `NVRM-xnu: wake #` and `NVRM-fb: system wake` lines. |
| The Mac wakes about every minute while asleep (`pmset -g log`: "DarkWake ... due to XDCI ... XHCI") | The firmware lets USB wake the system (GPE 0x6D). Not the driver. | Rename `GPRW` to `XPRW` in `ACPI → Patch` with `SSDT-GPRW`. Then only the power button wakes the Mac. |
| Windows go black or show an old picture until clicked; Preview shows images as blocks | An image's contents were marked up to date before the GPU had drawn them. | 1.14 includes the fix (community patch). Send logs if you still see it. |
| "Your browser does not support WebGL" | Some WebGL shaders failed to translate. | Fixed in 1.14. |
| Photos / face detection keeps crashing (mediaanalysisd) | A Metal object was freed twice in our driver. | Fixed in 1.14. |
| Hardware video encoding off on RTX 5070 Ti and similar cards | The card has no first encoder engine, and the encoder only tried that one. | Fixed in the encoder (it uses the first engine the card has). |
| "Quadro T1200 / T-series … is older than the driver supports" | The installer's list did not include every Turing workstation card. | Fixed in 1.14: every Turing or newer card is accepted. |
| Kernel panic as the login screen appears (WindowServer, `IOFramebuffer::postOpen`, page fault in `memcpy`), with WhateverGreen installed | WhateverGreen copies the boot screen on the first framebuffer start from an address it saved at boot. macOS can free that mapping before the NVIDIA display opens (seen on a laptop whose boot screen is on the Intel GPU). | Add `gfxrst=4` to boot-args. It turns that copy off; the driver draws its own start-up screen. |
| The app edits the wrong OpenCore partition | Two OpenCore partitions on the macOS disk. | Fixed in 1.14 (the boot arguments decide which one started the Mac). |
| A problem that other Macs with the same card do not have | The Mac may still carry a boot argument or a setting from an earlier test: an opt-out of a default path, a test hook, a development switch. | `sysctl debug.nvrm_switches` lists the driver boot arguments set beside the install line, with a `!` on each one an ordinary install does not need; `sysctl debug.nvrm_switches_nondefault` should say 0. Remove the marked ones from `boot-args` and restart. Settings in `/Library/GPUBundles/nvmtl/nvrm610.conf` are not in that list: look at the file too. From a checkout, `python3 tools/switch_registry.py doctor` also reads the settings file and names entries that are misspelled or that this version no longer reads. [SWITCHES.md](SWITCHES.md) says what each one does. |
| RTX 50: video in Safari, QuickTime or FaceTime is green, or an H.264 video there does not play | These apps decode in a system service. The driver's own H.264 decoder gives them a green picture on RTX 50; the newer decoder is used there only after the card passes a short decode test, and is not proven on every stream. | In Terminal: `log show --last 10m --predicate 'eventMessage CONTAINS "decode self-test"'` after playing a video. `PASS` and a broken video: add `NVMTL_NVENCVT=0` to `nvrm610.conf` to go back to the old decoder, and send the line with the video's details. `FAIL at ...`: the old decoder is in use (green); send the line, it says what the card refused. A line `... is a build without the Blackwell decode self-test - REFUSED on this card`: the codec library installed beside the plugin is older than the plugin; build and install both from the same revision. |

### Several NVIDIA cards

If several NVIDIA cards attempt startup, or a custom kernel collection loads none of
the driver components, see [selecting one GPU](MULTI-GPU.md). The workaround was boot-tested
on four RTX 4090s with 1.14.7 and macOS 15.8.1; it restricts NVRM/NVAccel to the display card
and rebuilds the final collection from permanent installation paths.

### The new paths of release 3.0

Every change of 3.0 is on by default and has one way back to the 2.9 behaviour (the table "The ways back" in
[CHANGELOG.md](../CHANGELOG.md)). Use a way back to find out whether a new path is behind a problem, then report what
you saw with it and without it. A boot argument needs a restart; a line in `nvrm610.conf` goes into your own block at
the end of the file and acts on the processes started after it (log out and in for WindowServer).

Unlike the rest of this page, these entries do not come from logs people sent yet: nothing of 3.0 has run on a card.
They say what the driver reports and what each line means. A line quoted here as in `nvmtl.log` is written by an
installed (release) plugin only for a process started with `NVMTL_PROCESS_ENV=1`; for every other process, WindowServer
included, only the plugin's failure lines reach the system log, with the prefix `NullMoth:`
([TESTING-ON-HARDWARE.md](TESTING-ON-HARDWARE.md), "Where the driver writes").

**The desktop came up later than you expect.** `sysctl debug | grep nvrm_boot_` (or the `nvrm-boot-timing` property of
NVRM in IORegistryExplorer) gives every phase of the start-up in milliseconds and says why the wait for the displays
ended (`debug.nvrm_boot_heads_why`; 7 is the 20 s bound: no display was reported in time). To compare with the waits
of 2.9, boot once with `-nvrmoldwaits` and send both sets of numbers.

**The card did not start and the firmware's picture stayed.** The kernel log has `NOT STARTING THE GPU` with the
reason. "NVRMFB cannot drive a display on this macOS" means the framebuffer's Apple classes differ from the ones it
was built for (`nvrm-layout` and `nvrmfb-layout` on the PCI device say the same; see the next item). A reason about
DMA means a DMA mapper (VT-d) translates the card and the driver may not or cannot use it (`nvrm-dma` names it; see
below). Neither counts as a failed start, so neither turns the driver off.

**`nvrmfb-layout`, `nvagdc-layout` or `nvaccel-layout` says refused, or no acceleration after a macOS update.** Check
with `ioreg -l -w0 | grep -E '"(nvrmfb|nvagdc|nvaccel|nvrm)-layout'` and `sysctl debug.nvrmfb_layout
debug.nvrmfb_layout_classes debug.nvrmfb_layout_vtables debug.nvrmfb_layout_vtdiff debug.nvrmfb_agdc
debug.nvrmfb_agdc_layout debug.nvaccel_layout`. On a good boot `nvrmfb-layout` reads `ok: 3 classes, vtables not
compared; macOS ...` and `nvagdc-layout` reads `ok: 1 class, vtables not compared; macOS ...`. A line that starts
with `refused:` names the class and both sizes: that macOS lays the class out differently from the headers the
driver was built with, and the kext did not start on it so that nothing writes over Apple's memory. With NVRMFB
refused, NVRM does not start the GPU, the firmware's picture stays and the Mac can be reached over the network; with
AGDC refused the display runs without AppleGraphicsDeviceControl; with the accelerator refused
(`NVAccel: Apple class layout refused: ...` in the log) the desktop runs without acceleration. Report the line with
`sw_vers`. `nvlayout=0` in the boot arguments starts them anyway (the check still runs and logs
`overridden (nvlayout=0): ...`), at the risk the check exists for (a crash or a frozen desktop); use it only to
confirm. `nvrmfb-layout-vtables` and `nvaccel-layout-vtables` never stop anything: a `differ:` or `not compared:`
line on a Mac whose display works is worth a report with the macOS build.

**VT-d (a DMA mapper) is on.** `sysctl debug.nvrm_dma_mode` says which case the machine is in
([DMA-IOMMU.md](DMA-IOMMU.md)).
- `NVRM-xnu: DMA: refused: ...` in the log, `debug.nvrm_dma_mode` = 3: VT-d translates the GPU and the driver did not
  start it: either `nvrmdma=0` is in the boot arguments, or the two-page self-test through the mapper did not read
  back. The driver will not run the GPU on untranslated addresses behind VT-d (that is what made 2.9 fail on such
  machines). Turn VT-d off in the firmware (or keep the installer's dropped DMAR table) to get mode 0, and send the
  log line.
- Desktop broken or GPU errors in mode 1 or 2: send `sysctl debug | grep -e nvrm_dma -e nvaccel_dma` and any AppleVTD
  fault lines naming the GPU (`log show --last boot | grep -i vtd`). Boot once with `nvrmdma=0`: if that gives a clean
  "GPU not started", the mapping code is at fault, not VT-d itself. Way back: VT-d off.
- Windows do not update after a screenshot or a screen recording: `debug.nvaccel_dma_mode` 1 or 2 with
  `debug.nvaccel_dma_map_fail` or `debug.nvaccel_dma_refused` rising means NVRM could not map the accelerator's buffer;
  `debug.nvaccel_dma_family` rising means the family's own copy ran instead.

**Frames look worse than in 2.9: stutter, tearing, a window that updates late or shows an old frame.**
- First, is the surface fence in use? `sysctl debug.nvrm_fence_state debug.nvrm_fence_accel debug.nvrm_fence_map_fail
  debug.nvrm_fence_map_mismatch debug.nvrm_fence_owners_distrusted`. State 1 and accel 1 with the other three at 0 is
  the normal case. State 0: `nvrmfence=0` is in the boot arguments. State 2: the fence switched itself off because
  eight processes did not finish what they held; state 3 after a wake: the GPU did not resume. In both the driver
  runs as 2.9 until the next boot; send the `NVRM-xnu: fence:` lines of `log show --last boot`.
- Boot once with `nvrmfence=0`. If that cures it, send `sysctl debug | grep -e nvrm_fence -e nvaccel_surface` from the
  boot without the boot argument: `wait_ge100ms`, `expired_gpu`, `expired_cpu`, `chan_noirq` and `done_on_slice` say
  whether readers waited long for a slow writer, a writer did not finish, or interrupts did not arrive for a channel.
  With `nvrmfence=0` NVAccel logs `surface fence: off for this boot (boot-arg nvrmfence=0)` once; that is expected.
  The line `surface fence: not offered by NVRM (yet)` without the boot argument means NVRM.kext is older than
  NVAccel.kext, or the two are of different releases. `nvrmfence=0` does not turn off the event wait or the unlocked
  page-off; they have their own ways back (`NVRM_EVENT_WAIT=0`, `nvaccel_pageoff_locked=1`).
- `NVRM-xnu: fence: pid N: ... - this channel is not watched` in the log: the kernel could not wire a page of that
  process's GPU channel, or its view of the page did not follow what the process wrote. That process uses 2.9's marks
  and tags; everything else is unaffected. Send the line: which of the two it was decides the fix.
- In the plugin's log (`nvmtl.log`): `surface fences: off (NVK or the kext answered 0x80000002 ...)` means the kext's
  fence is off (`nvrmfence=0`, or the kext or NVK is older than the plugin) and the plugin uses the 2.9 busy tags and
  pending marks; nothing to do unless the fence was expected to be on. Many `surface fences: hold refused
  (0x80000007)` lines in one process mean a Vulkan channel of that process is not watched by the kernel: that process
  keeps the 2.9 mechanism; send the `NVRM-xnu: fence:` kernel lines.
- In an application's output: a line starting `nvrm: surface fence:` means the kext has the surface fence but that
  process is not using it; the line says why. Report it with `sysctl debug.nvrm_fence_state
  debug.nvrm_fence_map_fail debug.nvrm_fence_map_mismatch`. No such line does not mean the fence is in use: with a
  kext older than the fence nothing is printed unless `NVK_DIAG=1` is set in `nvrm610.conf`, and then
  `nvrm: surface fence: the kext has none` is in `/private/tmp/nvk-diag-<pid>.log`. `debug.nvrm_fence_state` says
  whether the loaded kext has it.

**The desktop freezes while recording the screen, taking screenshots or showing window thumbnails.** Boot once with
`nvaccel_pageoff_locked=1` added to the boot arguments. If the freeze is gone, the page-off wait without the
accelerator lock is the cause: please report it with the output of `sysctl debug | grep nvaccel_pageoff` and the
kernel log lines that start with `page-off:`, and keep a spindump of WindowServer and kernel_task taken while it is
frozen. `debug.nvaccel_pageoff_unlocked_standdown` = 1 without the boot argument means the driver already turned that
path off for the boot; the log line says why.

**Screen sharing, recording or video shows wrong colours, a green or purple cast, a frame from a moment ago, or
frames that flicker between two.** Boot once with `nvaccel_surf_planes=0` and say whether it goes away. Before that,
send `sysctl debug | grep -E 'nvaccel_(surf_plane|surfvram_ranged|pageoff_ranged)'`, the kernel log lines that start
with `surfvram: surface ... ranged` and `surfvram pageoff: res ... plane`, and the `planar surfaces:` lines of
`nvmtl.log`.

**Applications use more CPU or stutter, with frame times between 1 and 3 ms.** Try `NVRM_EVENT_WAIT=0` in
`nvrm610.conf` and relaunch the application: that restores polling for every process, WindowServer included (which
then uses more CPU than before). If it helps, report it with `sysctl debug.nvrm_evt_woken debug.nvrm_evt_timeouts
debug.nvrm_evt_user_missed debug.nvrm_evt_user_fallbacks` taken while the application ran.
`debug.nvrm_evt_user_missed` is the number of full 2 ms sleeps that ended on their timeout although the GPU work was
already done; a few of them are normal (the GPU shows a completion a moment before its event arrives). Only
`debug.nvrm_evt_user_fallbacks` above 0, together with the line `nvrm: event wait OFF for this process (16 wakes in a
row did not arrive)` in the application's output, means that a process stopped trusting the completion event.
`NVRM_GSUB_LOG=1` in `nvrm610.conf` prints, when a channel closes, how its waits went. [EVENT-WAIT.md](EVENT-WAIT.md)
explains the numbers.

**A window or game glitched once (a black frame, a missing frame, a stutter) and went on.**
- A line `nvrm: channel slot ... rebuilt` in an application's output, or `nvk-reason: nvrm channel slot ... rebuilt`
  in `nvmtl.log`, means the GPU faulted on one of that application's channels and NVK recovered it; the frames that
  were on the GPU then may be missing. A line `not rebuilt` names the kind of loss and the reason; that application
  lost its device as in 2.9. To rule the recovery out, put `NVK_CHANNEL_RECOVER=0` in `nvrm610.conf` and restart the
  application.
- `vk: after a GPU fault, a channel rebuild dropped this command buffer` in `nvmtl.log`: one frame (or the frames in
  flight) of that process was lost. Send the line and the `fault context:` lines after it. If the desktop froze after
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

- Start once without the driver: pick **1401: Start once without NVIDIA driver** in the OpenCore picker, press Y,
  then choose macOS or Recovery. The driver is back at the next start. (1.14)
- Remove the driver: pick **1401: Remove NVIDIA driver**, press Y, then choose macOS. It removes itself and restarts.
  Removal also puts the saved screen resolution and refresh rate back to a safe default, so the Mac comes up with a
  picture even if the driver had been set to a mode the built-in graphics cannot show.
- If you cannot see the entry: the installer and every update make the OpenCore picker show it — the picker appears
  on start-up, uses the Builtin picker (Apple's own picker never lists these entries), stops hiding entries behind the
  spacebar, and lists both NullMoth entries without pressing Space. Re-run the 1401 app (Install or Repair) once from a
  working start to apply this to a picker built before 1.15. If the entry still does not appear, the app was editing an
  OpenCore partition other than the one that starts the Mac — keep the disk or USB stick the Mac starts from attached,
  and choose its `config.plist` in the app.
- If the screen is black even at the picker, or the Mac starts only from a USB stick you no longer have: boot into
  macOS Recovery (the driver cannot run there), open **Terminal**, run
  `nvram 7C436110-AB2A-4BBB-A880-FE41995C9F82:nullmoth-remove=1`, then restart into macOS. The Mac removes the driver by
  itself, with no picture needed, and restarts clean. This is the same removal the picker entry runs.
- Boot logo: earlier versions could add `NullMothLogo.efi` to OpenCore. It is no longer part of the driver; installing,
  updating or removing the driver takes its entry and file out. If the picker never appears on an EFI that still has
  it, disable that entry in the EFI configuration from another operating system.

Each path that became the default in release 3.0 has one opt-out, for telling whether that path is behind a
problem. They are listed with their exact spelling and what they restore under "The ways back" in
[SWITCHES.md](SWITCHES.md) and in [CHANGELOG.md](../CHANGELOG.md). If one of them makes a problem go away, send logs
with it set and say which one: the opt-outs are removed again once the paths have been proven, at the release each row
names.

## 5. GPUs the driver does not cover yet

GTX 10 and older (Pascal, Kepler), AMD RX 7000/9000, Intel Iris Xe and Arc, and Ryzen AI graphics run macOS with basic
display (no graphics acceleration). To help support them, run **1401 Probe 2.1** on that machine: its GPU test records
what Linux's own driver does on your GPU, which is how support for a new GPU gets built.
