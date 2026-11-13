# Testing release 3.0 on a card

While [README.md](../README.md) opens with the notice "Work in progress: do not install this branch yet", nothing
here is to be installed: this page is the check list for the day the notice is gone, and until then it is only to be
read. Nothing in it has been seen on an NVIDIA card. Each "pass" below is what the code is written to show, not a
result anyone has observed.

En español: [TESTING-ON-HARDWARE.es.md](TESTING-ON-HARDWARE.es.md).

The steps come in the order a tester meets them: before installing, the first boot, the desktop, games and video,
sleep and wake, and two GPUs for whoever has them. Each says what a pass looks like and what a failure looks like.
When one fails, stop there: write down what you saw, use the way back of that change (the table at the end) for one
boot or one application launch, and say in the report whether the problem went away with it. What each line and
counter means is explained once, in the document each step links to; this page only says what to run and what to
expect. [TROUBLESHOOTING.md](TROUBLESHOOTING.md), "The new paths of release 3.0", says what to do about each kind of
failure.

## Where the driver writes

- **Kernel**: `log show --last boot --predicate 'process == "kernel"'` (every `log show --last boot` takes about a
  minute). NVRM's lines start with `NVRM-xnu:`, the
  framebuffer's with `NVRM-fb:`, the accelerator's with `NVAccel:`. The counters are sysctls under `debug.`
  (`sysctl debug | grep nvrm_`, `sysctl debug | grep nvrmfb_` and `sysctl debug | grep nvaccel_`).
- **The Metal plugin** (what each application and WindowServer do with Metal): a package built with `./build.sh`
  carries the release plugin, which writes its own log, `nvmtl.log`, only for a process started with
  `NVMTL_PROCESS_ENV=1` in its environment (not as root). For every other process, WindowServer included, only the
  lines that report a failure (they contain words such as FAIL, refused, fault, missing or cannot) reach the system
  log, with the prefix `NullMoth:`: `log show --last boot --predicate 'eventMessage CONTAINS "NullMoth:"'`. An
  application started from Terminal with `NVMTL_PROCESS_ENV=1` writes `/tmp/nvmtl.log` (or `nvmtl.log` in its
  `$TMPDIR` when `/tmp` is not writable for it). Every step below that looks for a line "in `nvmtl.log`" needs such a
  launch; for WindowServer, use the system log.
- **NVK** (the Vulkan layer under the plugin) prints its lines, which start with `nvrm:`, on the standard error of the
  application: start the application from Terminal to see them.

Settings in `/Library/GPUBundles/nvmtl/nvrm610.conf` go into your own block at the end of the file, between
`# --- my settings, kept on update ---` and `# --- end my settings ---` (section 1, step 5, says why and what an
install keeps); the last line for a name wins, and a process
reads the file when it starts (restart the application, or log out and in for WindowServer). Boot arguments go on the
`boot-args` line of OpenCore's `config.plist` (README.md, "Wiring into an existing OpenCore setup") and need a
restart. Take every test setting out again when its test is done.

## 1. Before installing

1. **A way back you have tried.** Keep the unpacked package of the release you install over (2.9) at hand: installing
   it again with its own `install.sh` puts every file back ([BUILDING.md](BUILDING.md), "Installing the result and
   going back", which also says what `uninstall.sh` does and does not do). Before it changes anything, the installer
   copies what is installed now (the kexts, the plugin bundle, the folder `/Library/GPUBundles/nvmtl` with its
   settings file, the kernel collection, the firmware) into a new folder `/Library/NullMoth/backup-<date>-<time>.<random>`,
   whose name it prints on its line `== 4. back up what is there now -> ...`; note it. It also leaves a job that runs at
   each start: when the new driver has turned itself off after two starts in a row that never lit its display (or
   after WindowServer kept dying on the NVIDIA display), the job puts everything back from that folder and restarts
   once. It removes itself at the first start after one that lit the display, so it does not help with a problem that
   comes later. Know how you will start the Mac when the
   desktop does not come up ([TROUBLESHOOTING.md](TROUBLESHOOTING.md), "4. Getting back in"; the 1401 picker entries
   exist only on a Mac set up with the 1401 app). Turn on Remote Login (System Settings, General, Sharing) and make
   sure you can reach the Mac over `ssh` from another computer: several failures below leave the firmware's picture
   or a frozen desktop, and the logs are then read over the network.
   Pass: you have started the Mac once by that way back, and logged in over `ssh`. Fail: either does not work; do not
   go on.
2. **A clean starting point.** `nvram boot-args` shows the `boot-args` line: it should hold the install line of
   README.md and nothing else of the driver's but settings you chose for the machine, such as `nvfbhidpi=1`
   (`gfxrst=4` stays if WhateverGreen needs it). Your own block of `nvrm610.conf` (step 5) should hold no test
   setting. Remove what an earlier test left, restart, and check again.
3. **Numbers from 2.9.** On the release that runs now, save what the driver reports, to compare later:

   ```bash
   sw_vers > ~/before-3.0.txt; nvram boot-args >> ~/before-3.0.txt
   sysctl debug | grep -e nvrm -e nvaccel >> ~/before-3.0.txt
   ```

   Note how long the desktop takes to come up after the picker, and the CPU use of WindowServer on an idle desktop
   and with a video playing (Activity Monitor).
4. **The package.** From its `pkgroot`, `sudo env CHECK=1 ./install.sh` checks the package and the Mac and changes
   nothing. Pass: its last line is `CHECK PASS (nothing changed)`. Fail: a `STOP:` line says what it refused; fix that
   first.
5. **Your settings file.** Read it before you install: `cat /Library/GPUBundles/nvmtl/nvrm610.conf`. The installer
   replaces the whole folder `/Library/GPUBundles/nvmtl` with the package's, and of your old file it puts back, at the
   end of the package's file, only the lines between `# --- my settings, kept on update ---` and
   `# --- end my settings ---` (each marker exactly so, on a line of its own), and of those only `NAME=value` lines
   whose name starts with `NVMTL_`, `NVK_` or `NVRM_` and whose value holds nothing but letters, digits and
   `. _ , = * -`. Every other line is replaced by the package's file: a file without the two markers (2.9's own has
   none) comes out of the install as the package's file. A line whose name has none of the three prefixes is read by
   nothing at all. Your old file is not lost: it is `nvmtl/nvrm610.conf` in the backup folder of step 1. To carry a
   line of your own over, put it between the two markers: in the old file before you install (the installer then
   keeps it), or in the new file afterwards. To see which of your lines are not the package's, compare your file with
   the package's own, `Library/GPUBundles/nvmtl/nvrm610.conf` under its `pkgroot`. Going back to 2.9 by installing it
   again keeps only that block too.

## 2. The first boot

1. **The card started.** Install, restart. Pass: the login window comes up on the NVIDIA card;
   `kmutil showloaded --list-only | grep nullmoth` shows four lines (NVRM, NVAccel, NVRMFB, NVRMAGDC), five on
   macOS 26 where the installer adds its audio driver `com.nullmoth.NMXAudio` for an HD Audio controller;
   `system_profiler SPDisplaysDataType` shows the GeForce with Metal supported. Fail: the firmware's picture stays or the screen is black. Over `ssh`, look in the
   kernel log for `NOT STARTING THE GPU` (the line gives the reason) and go on with steps 3 and 5 below, which tell
   the reasons apart.
2. **No leftover switch.** `sysctl debug.nvrm_switches debug.nvrm_switches_set debug.nvrm_switches_nondefault`.
   Pass: `debug.nvrm_switches_nondefault` is 0. Fail: a name marked with `!` is set; remove it from `boot-args` and
   restart before anything else ([SWITCHES.md](SWITCHES.md) says what it is).
3. **The Apple class checks.** Nobody has compared the sizes of Apple's classes that this build was made for with
   the ones your macOS registers: on each macOS you have (15 and 26) the first start is that comparison, and it is the
   test that matters. Right after it (over `ssh` if the screen did not come up):

   ```bash
   ioreg -l -w0 | grep -E '"(nvrmfb|nvagdc|nvaccel|nvrm)-layout'
   sysctl debug.nvrmfb_layout debug.nvrmfb_layout_vtdiff debug.nvrmfb_agdc debug.nvrmfb_agdc_layout \
          debug.nvaccel_layout debug.nvaccel_layout_bad debug.nvaccel_layout_vtlong debug.nvaccel_layout_vtshort
   log show --last boot --predicate 'eventMessage CONTAINS "Apple class layout" OR eventMessage CONTAINS "Apple vtable lengths"'
   ```

   `sysctl` prints `unknown oid` for a name that no kext registered, goes on with the others and then exits with the
   number of unknown names; the `log show` takes about a minute. What the code is written to show:
   - Pass: `nvrmfb-layout` reads `ok: 3 classes, vtables not compared; macOS <build> (Darwin <version>)`,
     `nvagdc-layout` `ok: 1 class, vtables not compared; macOS ...`, and `nvaccel-layout` starts with `ok:` and
     the number of classes and vtables it compared; there is no `nvrm-layout` line (NVRM writes one only when it
     declines). `debug.nvrmfb_layout`, `debug.nvrmfb_agdc`, `debug.nvrmfb_agdc_layout` and `debug.nvaccel_layout`
     are 1 and `debug.nvaccel_layout_bad` is 0. A `differ:` or `not compared:` line in `nvrmfb-layout-vtables` or
     `nvaccel-layout-vtables`, or `debug.nvrmfb_layout_vtdiff`, `debug.nvaccel_layout_vtlong` or
     `debug.nvaccel_layout_vtshort` above 0, is not a failure (a vtable length is reported, never a reason to
     refuse), but send it with `sw_vers`.
   - Fail: a line starts with `refused:` (it names the class and both sizes). What each refusal does:
     - `nvrmfb-layout` refused, read by NVRM before it started the card: the kernel log has
       `NOT STARTING THE GPU: NVRMFB cannot drive a display on this macOS ...`, `nvrm-layout` reads `declined: ...`,
       the firmware's picture stays and the boot guard does not count this start. `debug.nvrmfb_layout` does not
       exist (the framebuffer did not start).
     - The same refusal read only after NVRM had started: `auto-go: NOT STARTING THE GPU: the framebuffer refused
       this macOS's classes after NVRM had started (...)` and `nvrm-layout` `declined late ...`. The firmware's picture
       stays, and this start is counted: two such starts in a row without a desktop that stays up turn the driver off,
       and the installer's job of section 1, step 1, puts the previous driver back.
     - `nvaccel-layout` refused: `NVAccel: does not attach: ... the display runs without acceleration` and NVRM's
       `auto-go: the accelerator declined this macOS's classes ...`; `debug.nvrm_boot_heads_why` is 6, there is no
       `debug.nvaccel_layout` and no `nvagdc-layout` (AppleGraphicsDeviceControl is made only for an accelerator that
       started). The framebuffer lights the display, so the boot guard counts the start as good and the installer's
       job puts nothing back. A desktop without acceleration on this driver has not been seen: if the screen stays
       black or shows only a cursor, use `ssh`.
     - `nvagdc-layout` refused: `debug.nvrmfb_agdc` is -6 and `debug.nvrmfb_agdc_layout` 2; the desktop runs without
       AppleGraphicsDeviceControl, and the start counts as good.

     Send the output of the three commands with `sw_vers`. Then boot once with `nvlayout=0` only to see whether the
     rest works (the check still runs, and its line then starts with `overridden (nvlayout=0):`), and take it out
     again (TROUBLESHOOTING.md has what that boot risks).
4. **Start-up time.** `sysctl debug | grep nvrm_boot_` gives each phase of the start in milliseconds. Pass: the
   desktop comes up no later than with 2.9, and `debug.nvrm_boot_heads_why` is not 7 while a display is connected.
   Fail: it comes up later, or 7 with a display connected; boot once with `-nvrmoldwaits` and send both sets of
   numbers.
5. **DMA (VT-d).** `sysctl debug.nvrm_dma_mode`. Pass: 0 on a machine without VT-d (or with the DMAR table dropped),
   and every other `debug.nvrm_dma_*` counter 0. With VT-d on, 1 or 2: follow
   [DMA-IOMMU.md](DMA-IOMMU.md), "Checking a VT-d machine". Fail: 3 means the driver did not start the card behind
   the mapper (the log line `NVRM-xnu: DMA: refused:` says why); send it.
6. **The RM's helpers.** Read twice, ten seconds apart:

   ```bash
   sysctl debug.nvrm_wq_mode debug.nvrm_atomic_mode debug.nvrm_bh_thread debug.nvrm_rc_armed debug.nvrm_rc_ticks \
          debug.nvrm_nano_wait_timeouts
   ```

   Pass: `nvrm_wq_mode` 1, `nvrm_atomic_mode` 1, `nvrm_bh_thread` and `nvrm_rc_armed` 1 with one card,
   `nvrm_rc_ticks` about ten higher the second time, `nvrm_nano_wait_timeouts` 0. Fail: -1 in either mode (a worker
   or a self-test did not start), 0 in a count, or ticks standing still; send the values and the `NVRM-xnu:` lines
   ([INTERRUPT-PATH-AND-RC-TIMER.md](INTERRUPT-PATH-AND-RC-TIMER.md) has every value).
7. **The surface fence after login.** Follow step 1 of [SURFACE-FENCE.md](SURFACE-FENCE.md), "What to look at on the
   card". Pass: `debug.nvrm_fence_state` 1, `debug.nvrm_fence_accel` 1, `debug.nvrm_fence_map_fail` and
   `debug.nvrm_fence_map_mismatch` 0. This is the first time a channel is ever registered with the fence. Fail: any
   other value; send the `NVRM-xnu: fence:` lines.
8. **Channels.** `sysctl debug | grep nvrm_chan_`. Pass: all 0. Fail: anything else on a desktop that has done
   nothing yet; send it with `sysctl debug.nvrm_xid_count`.

## 3. The desktop

1. **Recording the screen: the first test that only a card can make.** Record the screen (or share it) for ten
   minutes while dragging windows over a playing video and opening Mission Control. A screenshot or a recording now
   waits for WindowServer's GPU work without holding the accelerator lock; if Apple's code holds another lock across
   that wait which a thread with the accelerator lock also needs, the desktop freezes for good. Pass: no freeze, and
   afterwards `sysctl debug | grep nvaccel_pageoff` shows `nvaccel_pageoff_unlocked_wait` 1,
   `nvaccel_pageoff_unlocked_waits` higher than before the recording, `nvaccel_pageoff_unlocked_standdown` 0 and
   `nvaccel_pageoff_unlocked_wait_max_us` not above 2000000 (2 s) by more than the moment it takes to get the lock
   back. Fail: the desktop freezes and stays frozen. Over `ssh`
   run `sudo spindump` while it is frozen (it samples every process, WindowServer and kernel_task included, and says
   where it wrote its file), then restart, boot with `nvaccel_pageoff_locked=1` and repeat the test. A freeze that
   this boot argument cures is the case described here: send the spindump, `sysctl debug | grep nvaccel_pageoff`
   and the `NVAccel: page-off:` kernel lines. `nvaccel_pageoff_unlocked_standdown` 1 is also a failure (the
   path turned itself off; the `page-off:` line says why).
2. **Frames.** Use the desktop for a minute, then play a 4K video or a game with the GPU busy, and capture while the
   GPU is loaded: steps 2 to 4 of SURFACE-FENCE.md, "What to look at on the card". Pass: no tearing, flicker or
   half-drawn frame; the counters as that list says (`debug.nvrm_fence_expired_gpu`, `debug.nvrm_fence_expired_cpu`,
   `debug.nvrm_fence_strikes` and `debug.nvrm_fence_owners_distrusted` 0; `debug.nvaccel_surface_fence_timeouts`
   0). Fail: a visible fault, or one of those counters above 0; boot once with `nvrmfence=0` and say whether it goes
   away.
3. **A process that dies.** Step 6 of the same list: `kill -9` a Metal application mid-frame, then stop one
   with `kill -STOP` and continue it with `kill -CONT`. Pass: no stall of the desktop; at most one hitch of up to
   100 ms for the stopped one. Fail: the desktop stalls, or `debug.nvrm_fence_state` becomes 2.

### Two-plane surfaces

With a video playing, capture the screen for a minute (screen sharing, a recording, or a capture application).
First check that the path is offered: `sysctl debug.nvaccel_surf_planes debug.nvaccel_pageoff_unlocked_wait` (both
1) and `ioreg -l | grep NVAccelSurfPlanes` (1). After the capture:

```bash
sysctl debug | grep -E 'nvaccel_(surf_plane|surfvram_ranged|pageoff_ranged)'
```

Pass: the capture shows the right colours and the current frame; `nvaccel_surfvram_ranged` and
`nvaccel_pageoff_ranged` rise; the other `nvaccel_surfvram_ranged_*` counters and `nvaccel_pageoff_ranged_fail` stay
0 on a machine without VT-d (`nvaccel_pageoff_ranged_temp` above 0 is not a failure); no `planar surfaces: self-test
FAILED` line in the system log. Send `nvaccel_surf_plane_validbits` (1 or 2) either way. Fail: a green or purple
cast, an old frame, frames that flicker between two, one of those counters above 0, or that FAILED line. Send the
`NVAccel: surfvram` kernel lines, then boot once with `nvaccel_surf_planes=0` and say whether it goes away.
[STUDIO-475-476.md](STUDIO-475-476.md) has the longer runs that measure this path.

## 4. Games and applications

1. **Waiting for the GPU.** Run a game or a GPU-heavy application for ten minutes, then:

   ```bash
   sysctl debug.nvrm_evt_clients debug.nvrm_evt_slept debug.nvrm_evt_woken debug.nvrm_evt_timeouts \
          debug.nvrm_evt_capped debug.nvrm_evt_user_missed debug.nvrm_evt_user_fallbacks
   ```

   Pass: `nvrm_evt_clients` 2 or more (applications sleep on the event, not only WindowServer); most sleeps woken;
   `nvrm_evt_user_missed` small next to `nvrm_evt_woken`; `nvrm_evt_user_fallbacks` and `nvrm_evt_capped` 0; the
   application's CPU use lower than with 2.9 or the same, and its frame rate the same. Fail: more stutter or CPU than
   with 2.9, or `nvrm_evt_user_fallbacks` above 0. Launch the application once from Terminal with
   `NVMTL_PROCESS_ENV=1 NVRM_EVENT_WAIT=0` in front of its command and compare
   ([EVENT-WAIT.md](EVENT-WAIT.md) explains every number).
2. **A GPU fault that the application survives.** Follow [CHANNEL-RECOVERY.md](CHANNEL-RECOVERY.md), "What to look at
   on the machine", in its order: first an hour of normal use with every `debug.nvrm_chan_*` counter at 0, then the
   injected faults on a throwaway Metal application, and WindowServer last. Pass: what each item there says (one
   hitch or one wrong frame, and the application goes on; the desktop and other applications do not notice). Fail:
   the application loses its device, the desktop freezes, or `debug.nvrm_chan_gave_up` rises; send the counters,
   the `nvrm:` lines of the application's output and the `NullMoth:` lines of the system log. Take the
   `NVK_FAULT_INJECT` line out of `nvrm610.conf` afterwards.
3. **Memory over a long session.** Play an hour of a game that streams textures (an open-world game) twice: once as
   installed, once with `NVMTL_IMAGE_GRACE=0` in your own block of `nvrm610.conf`. Launch it from Terminal with
   `NVMTL_PROCESS_ENV=1` both times, so that its `nvmtl.log` is written. Compare the process's memory in Activity
   Monitor at 5, 30 and 60 minutes. Pass: with the grace it may sit a few frames' worth of textures higher, but it
   does not keep growing where the run without it stays flat; the `vk: image grace:` lines in `nvmtl.log` show a
   small held count that falls back near 0 when the game is idle. Fail: memory keeps growing with the grace and not
   without it, or a line says dead images were released 1024 submits after their death. Send `nvmtl.log`. Three
   limits of this release apply here (CHANGELOG.md, "Known limits of what is on"): nothing is released early when the
   application reaches its memory limit; an application that keeps a command buffer uncommitted holds every texture
   released after it until that command buffer is committed or dropped, or 1024 submits later; the bindless slot of
   a released texture follows the older rule.
4. **Rendering.** Play the games and run the applications you ran on 2.9, on RTX 50 in particular (the shader
   compiler carries Mesa's later fixes). Pass: the same picture as 2.9 or better; shaders are compiled again once,
   so the first start of each takes longer. Fail: a rendering fault that 2.9 did not have; send it with the game's
   name and the card. If you build from source, `NVK_SKIP_PATCHES` builds NVK without one of the compiler patches to
   find which one is behind it ([NVK-BASE-AND-HEADERS.md](NVK-BASE-AND-HEADERS.md)).

## 5. Video

1. **Which decoder made the frames (RTX 50).** Play an H.264 video in Safari, then:

   ```bash
   log show --last 10m --predicate 'eventMessage CONTAINS "decode self-test"'
   ```

   A video that was already starting while the card ran the test (typically the first one after a restart) can
   still be on the built-in decoder: reload it once before judging. The decode service (`VTDecoderXPCService`,
   which decodes for Safari, QuickTime and FaceTime) asks for the codec library's H.264 decoder only after the card
   has decoded two built-in test streams and every frame matched bit for bit, once per decode-service process.
   - Pass: a `NullMoth: nvencvt:` line with `decode self-test: PASS` (it says how long the test took), and the video
     plays with the right colours.
   - Fail: `decode self-test: FAIL at <step>`. The built-in decoder is then the only one, as in 2.9 (green on RTX 50);
     the step says how far the card got. A failed test can leave one Xid in the kernel log; that is its trace, not a
     second fault. Send the line.
   - Fail: a line that says the installed codec library is a build without the Blackwell decode self-test and was
     REFUSED on this card: the library is older than the plugin beside it. Build both from the same revision and
     install both.
   - No line at all: the process never asked (the card is not an RTX 50 by its PCI id, `NVMTL_NVENCVT=0` is set, or
     the library is not installed root-owned), or no decode service has started yet.
   - PASS but a broken video: put `NVMTL_NVENCVT=0` in `nvrm610.conf` to go back to the 2.9 decoder, and send the
     line with the video's details.

   For the library's own log, `sudo touch /tmp/nvencvt.log` before the process starts; each process that loads the
   library then writes `/tmp/nvencvt-<pid>.log`. The decode service runs in a sandbox and may not be allowed to do
   so: if no such file appears for it, that says nothing about the test, whose verdict is the system log line above.
2. **RTX 30 and 40.** Play H.264, HEVC, VP9 and AV1 videos in Safari and QuickTime. Pass: as with 2.9. Fail: a video
   that played with 2.9 does not; send the video's details and `log show --last 10m --predicate 'eventMessage
   CONTAINS "nvencvt"'`.

## 6. Sleep and wake

1. Sleep the Mac from the Apple menu, wait a minute, wake it. Do it three times, once while a video plays and once
   while a game runs. Pass: the desktop comes back each time; `sysctl debug.nvrm_fence_state` is 1 afterwards
   (`debug.nvrm_fence_end_pm` counts the holds that were in flight at the moment of each sleep, which can be none, so
   it may stay where it was); `debug.nvrm_irq_settle_timeouts` 0; the kernel log has a
   `NVRM-xnu: sleep #` and a `NVRM-xnu: wake #` line for each; no application lost its GPU and no frame pending at the
   sleep was reported lost. With VT-d on, `debug.nvrm_dma_live` reads the same before and after a sleep with nothing
   else running. Fail: a black or frozen screen after the wake, `debug.nvrm_fence_state` 3 (the GPU did not
   resume), or an application that lost its device; send the `NVRM-xnu:` and `NVRM-fb:` lines of that boot
   ([SLEEP-WAKE.md](SLEEP-WAKE.md) lists them).

## 7. Two GPUs

Only for a machine with two NVIDIA cards. Set it up first as [MULTI-GPU.md](MULTI-GPU.md) describes, so that the
driver runs the card the display is on, and repeat sections 2 and 6. Pass: everything as with one card, and sleep and
wake bring the desktop back. Then, if you want to try the experimental mode in which every card is accelerated, boot
with `nvmultigpu=1` ([MULTI-GPU-DESIGN.md](MULTI-GPU-DESIGN.md)) and repeat both sections. Pass:
`debug.nvrm_bh_thread` and `debug.nvrm_rc_armed` read 2; each card's start is in the kernel log; after a wake both
cards are back and `debug.nvrm_fence_state` is 1. Fail: a card missing from those counters, or a wake that loses one;
send the kernel log of that boot.

## The ways back

One per change; each restores the 2.9 behaviour of that change only. They exist to tell whether a change is behind a
problem, not to be left set.

| Change | Way back | Where |
|---|---|---|
| Surface fence | `nvrmfence=0` | boot argument |
| Page-off wait without the accelerator lock | `nvaccel_pageoff_locked=1` | boot argument |
| Two-plane surfaces in VRAM | `nvaccel_surf_planes=0` | boot argument |
| Apple class layout checks | `nvlayout=0` | boot argument |
| Start-up waits | `-nvrmoldwaits` | boot argument |
| RM work queues | `nvrmworkq=0` | boot argument |
| Answers by context | `nvrmatomic=0` | boot argument |
| Bottom-half thread | `nvrmbhthread=0` | boot argument |
| RC timer | `nvrmrctimer=0` | boot argument |
| Nano timer cancel wait | `nvrmnanowait=0` | boot argument |
| DMA through VT-d | `nvrmdma=0` (with a mapper the card is then not started) | boot argument |
| Event wait | `NVRM_EVENT_WAIT=0` (every process polls, WindowServer included) | `nvrm610.conf` |
| Channel recovery | `NVK_CHANNEL_RECOVER=0` | `nvrm610.conf` |
| Error report for dropped work | `NVMTL_DROP_REPORT=0` | `nvrm610.conf` |
| Image grace | `NVMTL_IMAGE_GRACE=0` | `nvrm610.conf` |
| RTX 50 H.264 decode in the decode service | `NVMTL_NVENCVT=0` (the codec library off everywhere) | `nvrm610.conf` |
| Upstream compiler fixes in NVK | `NVK_SKIP_PATCHES="<patch names>"` | building from source |

CHANGELOG.md, "The ways back", and SWITCHES.md say what each one restores in full.

## What to attach to a report

- `sw_vers`, the card, and `nvram boot-args`.
- `sysctl debug.nvrm_switches debug.nvrm_switches_set debug.nvrm_switches_nondefault`.
- Your own block of `/Library/GPUBundles/nvmtl/nvrm610.conf`.
- `sysctl debug | grep -e nvrm -e nvaccel`, taken while or right after the problem shows, and the same from 2.9
  (`~/before-3.0.txt`).
- The kernel log of the boot: `log show --last boot --predicate 'process == "kernel"' > ~/kernel.txt`.
- The plugin's failure lines: `log show --last boot --predicate 'eventMessage CONTAINS "NullMoth:"' > ~/nullmoth.txt`.
- For an application started with `NVMTL_PROCESS_ENV=1`: its `nvmtl.log`, and the `nvrm:` lines it printed in
  Terminal.
- Which section and item above failed, and whether its way back made the problem go away.
