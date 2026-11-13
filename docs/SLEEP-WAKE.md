# System sleep and wake

Before this change the driver had no system sleep support. On a desktop the PCIe slot loses power in S3, so the GPU
came back reset while NVRM still thought GSP-RM was running. On wake NVRMFB re-lit its head through NVKMS, the push to
the core channel spun in `nvEvoMakeRoom` forever, and the Mac needed a hard reset ("Darkwake Exit Failure",
`Sleep Wake Failure` report). The work below makes sleep and wake behave as on Linux, plus the steps macOS needs on top.

Tested on an RTX 5050 (GB207, FSP boot), MacPro7,1 SMBIOS, macOS 15.8.1: repeated sleep and wake with the desktop,
windows and Metal apps intact. That test was of 2.9. The additions of release 3.0 (the interrupt gate, the paused 1 Hz
timer, the surface fence's sleep step, the bounded waits for RM work, DMA through VT-d) have not run through a sleep
on a card yet; they are marked "(3.0)" below.

## What happens

| Step | Where | What |
|---|---|---|
| System is about to sleep | NVAccel | A root-domain priority power interest opens the *sleep window*: page-offs IOGraphicsAccelerator2 does now keep their VRAM (see below). |
| Display goes off | NVRMFB | As before (DPMS). |
| NVRM `setPowerState(0)` | NVRM | Pause the notebook power manager; then (3.0) the surface fence (every hold ends and new ones are refused until the wake) and the RM's 1 Hz timer; tell the framebuffers; `nvKmsSuspend` (heads off, while the GPU still serves the core channel); close the RM gate; stop user channels; save PCI config, Resizable BAR control and the root port window; (3.0) wait for the RM's global work queue, 5 s at most; `rm_power_management(STANDBY)`. Then (3.0) the interrupt source is turned off and the interrupt path is closed: no handler enters the RM from here on, and the driver waits (2 s at most) until no handler is inside and no bottom half is pending or running. |
| — | RM | With `PreserveVideoMemoryAllocations=1` RM copies **every** VRAM allocation out through its temporary-file interface. NVRM backs that "file" with pageable kernel memory (no disk I/O). GSP-RM is unloaded. |
| Slot powered again, NVRM `setPowerState(1)` | NVRM | Restore the root port window and the Resizable BAR size (the firmware resets BAR1 to 256 MB), check `PMC_BOOT_0`; open the interrupt path again before the interrupt source is turned on, so that the RM's resume gets its interrupts (3.0); `rm_power_management(RESUME)` (GSP boots again through FSP and VRAM is copied back), restart user channels, `nvKmsResume`, open the RM gate; when the resume worked, resume the power manager and (3.0) the 1 Hz timer; tell the surface fence that the GPU is back, or that it is lost, after which the fence stays off until the next boot (3.0); tell the framebuffers. |
| NVRMFB `setPowerState(1)` | NVRMFB | Re-light the head once NVRM says the GPU is back, then do IOGraphics' own wake work (below). |
| CPU back on | NVAccel | The sleep window closes: page-offs give VRAM back again. |

## Why each part is needed

**NVRM in the power tree.** NVRM was not a power child of the GPU's `IOPCIDevice`, so nothing suspended or resumed RM.
It now joins the tree under the PCI device. `-nvrmnopm` restores the old behaviour.

**All of VRAM is preserved.** By default RM saves only its own allocations. WindowServer's GPU channels (NVK through
NVAccel) keep push buffers and semaphores in VRAM; without them the compositor waited on fences forever after wake.
NVRM sets the RM registry key `PreserveVideoMemoryAllocations=1`. On Linux RM then writes VRAM to a file under
`NVreg_TemporaryFilePath`; here `os_open_temporary_file`/`os_write_file`/`os_read_file` (`os-xnu2.cpp`) store it in
4 MB chunks of pageable kernel memory, so the VM can compress it on a machine short of RAM, with wired memory as the
fallback. RAM keeps power in S3, and no disk is touched while the system goes to sleep. The RM itself is unmodified.

**The RM gate.** Linux freezes user space before suspend and also takes `nv_system_pm_lock` in every ioctl. macOS freezes
nothing: on wake WindowServer and Metal apps called into RM while the GPU was still in standby, which corrupted GMMU
state or handed errors to NVK. Every RM entry made for a client (escape ioctls, client close, NVAccel's GPU VA and
surface-share calls, `nvkms_call_rm`) now waits at a gate that the PM thread closes for the suspend and opens after
the resume. NVKMS has its own gate: ioctls wait (30 s at most), flips fail at once, timers wait.

**PCI state.** IOPCIFamily restores the config space it shadows, but not the Resizable BAR control register, and the
firmware's S3 resume path resets BAR1 to 256 MB. NVRM writes back the BAR1 size, the BARs and the root port window it
saved before standby.

**NVRMFB and IOGraphics.** NVRMFB overrides `IOFramebuffer::setPowerState`, so the family's own power work never runs,
and `IOFramebuffer::handleEvent()` does nothing off the IOGraphics controller's power thread. At system sleep IOGraphics
clears `pagingState` and sends `kIOFBNotifyWillSleep` to every client; after wake WindowServer's wake acknowledgement
(`_extEntry`) slept until `pagingState` came back, and the accelerator stayed disabled. On a wake after a GPU standby
NVRMFB now does what IOGraphics does (`IOFBController::computeState` and `handleEvent(kIOFBNotifyDidWake)` in
IOGraphics-599): deliver `DidWake` with `true` and then `false` (the second re-enables the accelerators), deliver
`DidPowerOn`, set `pagingState` and wake WindowServer on `fServerConnect`, under the framebuffer's work loop gate. It
never pushes to NVKMS before NVRM has resumed the GPU (`NVRMGpuReady`, `NVRMGpuPower`).

**NVAccel and VRAM across sleep.** At sleep IOGraphicsAccelerator2 pages off its VRAM resources, and the wake's mode
change pages them back on. If page-on got fresh VRAM, WindowServer kept drawing into the memory it had imported before
sleep while the display showed the new one: a black desktop with only the cursor. NVAccel's sticky VRAM
(`debug.nvaccel_vm_sticky`, on by default) keeps a surface's VRAM across page-off, and its next page-on takes the same
VRAM back; RM has kept the contents through the sleep, so nothing is lost.

## What else happens around a sleep (3.0)

**The sleep line.** `NVRM-xnu: sleep #n: user channels stopped ... rm_power_management(STANDBY) ...` now also says how
much VRAM the RM wrote to its save file during standby (`<n> MB of VRAM (<bytes> bytes) written to the save file`).
That is the VRAM this sleep preserved through the file; a RM that preserves VRAM by another method writes 0 here, and
two GPUs going to sleep at the same moment can each count some of the other's bytes.

**RM work.** Before the GPU goes to standby the driver waits for the RM's global work queue, 5 s at most, because all
of this runs inside the system's power change ([INTERRUPT-PATH-AND-RC-TIMER.md](INTERRUPT-PATH-AND-RC-TIMER.md),
"RM work items").

**The surface fence** ([SURFACE-FENCE.md](SURFACE-FENCE.md)). It is told about sleep before the user channels stop:
every hold ends, NVAccel's waits for a frame go on at once, a reader in the Metal plugin falls back to the writer's
busy tag for up to 10 ms as in 2.9, and new holds are refused until the wake (`debug.nvrm_fence_end_pm` grows by the
holds that were in flight). Its views of the channels' pages are ordinary wired memory and survive the sleep.
`debug.nvrm_fence_state` stays 1 through the sleep (the fence is only paused), and after the wake
`debug.nvrm_fence_opens` grows again as holds are opened; if the GPU did not resume, the state is
3 and the driver keeps 2.9's marks and tags until the next boot. A GPU that goes away while it is suspended
(unplugged, or its driver stopped, during sleep or while its slot is powered off by `nvrtd3=1`) never wakes; the
driver tells the fence so when that GPU stops, so that the fence does not stay closed for a second card
(`nvmultigpu=1`) or for a card that is started later.

**Waits that span the sleep.** NVK's no-progress budget (`NVRM_FLUSH_TIMEOUT_MS`) and the Metal plugin's fence wait
(the one that gives a fence up as lost after ten budgets: 50 s in WindowServer) are measured in awake time
(`CLOCK_UPTIME_RAW`): a wait that is in progress when the machine sleeps continues after wake with the time it had
left, so a frame that was pending at sleep is not declared lost, and a channel is not rung again or rebuilt, merely
because the machine slept. Application timeouts (`vkWaitForFences`) are still measured on the ordinary clock. With
`NVK_CHANNEL_RECOVER=0` NVK's budget is on the ordinary clock, as in 2.9 (a wait across a long sleep can then be
called lost at wake).

**VT-d.** With a DMA mapper (DMA modes 1 and 2, [DMA-IOMMU.md](DMA-IOMMU.md)) the GPU's bus addresses stay valid
across sleep: AppleVTD keeps its tables in memory and restores them before drivers wake. `debug.nvrm_dma_live` reads
the same before and after a sleep with nothing else running. Not yet observed on hardware.

## Switches and diagnostics

| Name | Kind | Effect |
|---|---|---|
| `-nvrmnopm` | boot-arg | NVRM stays out of the power tree and keeps the stock RM settings: the behaviour before this change. |
| `debug.nvaccel_vm_sticky` | sysctl (rw) | `1` (default) a surface keeps its VRAM across page-off; `0` page-off gives it back and the desktop is black after wake. |
| `debug.nvaccel_vm_parked`, `debug.nvaccel_vm_unparked` | sysctl | Page-offs that kept their VRAM, and page-ons answered with it. |
| `debug.nvrm_pm_waited` | sysctl | RM calls that waited at the gate for a resume. |
| `debug.nvrm_isr_count`, `debug.nvrm_isr_handled` | sysctl | GPU interrupts taken and claimed by RM. |
| `nvrm-pm` | NVRM registry property | `running`, `suspended` or `lost (resume failed)`. |
| `debug.nvrm_isr_skipped` | sysctl | GPU interrupts that arrived while the interrupt path was closed (sleep, shutdown, stop) and were not given to the RM. |
| `debug.nvrm_irq_settle_timeouts` | sysctl | Waits for the interrupt path to go quiet before standby that ran out. Should stay 0. |
| `debug.nvrm_fence_end_pm`, `debug.nvrm_fence_state` | sysctl | Surface holds ended by a sleep; the fence's state (1 on, 3 off after a failed resume). |

Kernel log lines to look for (`/usr/bin/log show --predicate 'process == "kernel"'`): `NVRM-xnu: sleep #`,
`NVRM-xnu: wake #`, `VRAM save file closed: N MB held`, `NVRM-fb: system wake:`.

## Not covered

- NMIntelFB (laptops whose panel is on the Intel GPU) has the same `setPowerState`/`handleEvent` pattern as NVRMFB and
  does not get the IOGraphics wake work yet.
- Hibernation (`hibernatemode` 3/25) is untested. Desktops default to `hibernatemode 0`.
- An open SSH session (`ttyskeepawake 1`) turns Apple menu → Sleep into a dark wake, not a sleep.
- Some boards wake every minute through USB (`pmset -g log`: `DarkWake ... due to XDCI ... XHCI`). That is the
  firmware's USB wake GPE (0x6D), not the driver; the usual `GPRW` → `XPRW` rename with `SSDT-GPRW` stops it, and then
  only the power button wakes the Mac.
