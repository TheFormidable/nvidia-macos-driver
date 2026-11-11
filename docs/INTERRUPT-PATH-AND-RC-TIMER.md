# Interrupt path and RC timer: what differs from Linux, and the switches to try

Status: written from a reading of this tree against NVIDIA's open kernel modules 620.70.06. **None of it has been
compiled or run.** Both behaviour changes are off unless their boot-arg is set, so a build without the boot-args behaves
as 2.9 does, plus a few read-only counters.

## The interrupt path

Linux registers the GPU interrupt with `request_threaded_irq` (`kernel-open/nvidia/nv.c`). `rm_isr` runs in the hard
interrupt and `rm_isr_bh` on the interrupt's own kernel thread. NVRM.kext runs both on one work loop thread
(`NVRM::interruptOccurred`). A bottom half that waits for the GPU lock, for example behind a long RM control from
another process, therefore also delays the next `rm_isr`, and with it the work `rm_isr` does without that lock.

Whether this costs anything on a given machine is a measurement, not a given. Read the counters before and after a
session that shows the problem (4K playback, screen capture, a browser with video):

| Name | Kind | Meaning |
|---|---|---|
| `debug.nvrm_isr_latency_max_us` | sysctl | Longest wait between an MSI and its `rm_isr`, since boot. |
| `debug.nvrm_isr_late_1ms`, `debug.nvrm_isr_late_4ms` | sysctl | Such waits over 1 ms and over 4 ms. |
| `debug.nvrm_isr_during_bh` | sysctl | MSIs that arrived while a bottom half was still running. |
| `debug.nvrm_bh_runs` | sysctl | Bottom halves run. |
| `debug.nvrm_bh_max_us` | sysctl | Longest bottom half, its wait for the GPU lock included, since boot. |
| `debug.nvrm_bh_over_1ms`, `debug.nvrm_bh_over_4ms` | sysctl | Bottom halves over 1 ms and over 4 ms. |
| `nvrmbhthread=1` | boot-arg | The bottom half runs on a thread of its own, one per GPU, as on Linux. |
| `nvrm-bottom-half` | NVRM registry property | Present, with `own thread (nvrmbhthread=1)`, when that thread is in use. |

How to read them: if `nvrm_isr_late_4ms` and `nvrm_bh_over_4ms` stay at or near zero during a bad session, the shared
thread is not the cause and `nvrmbhthread=1` will not help. If they grow together with the stutter, try the boot-arg and
compare `nvrm_isr_late_*` again; with the thread in use `nvrm_isr_during_bh` still counts, but those MSIs no longer wait.

## The RC timer

On Linux `nv_start_rc_timer` arms a 1 s timer whose callback is `rm_run_rc_callback`. That runs the RM's 1 Hz callback
list: the GSP firmware heartbeat check, the check that the GPU still answers on the bus, the NVENC session statistics
and, once a client enables it, the channel watchdog. In 2.9 `nv_start_rc_timer` and `nv_stop_rc_timer` are stubs and
nothing calls `rm_run_rc_callback`, so that list never runs.

| Name | Kind | Meaning |
|---|---|---|
| `nvrmrctimer=1` | boot-arg | A thread call ticks once a second per GPU and calls `rm_run_rc_callback`. Paused across system sleep. |
| `debug.nvrm_rc_ticks` | sysctl | Ticks. Present only with the boot-arg. Should grow by one a second while the GPU is awake. |
| `debug.nvrm_rc_runs` | sysctl | Ticks on which `rm_run_rc_callback` returned `NV_OK`. |

What it does not do: the channel watchdog stays off. The RM initializes it disabled and waits for a client to send
`NV2080_CTRL_CMD_RC_ENABLE_WATCHDOG`; nothing in this tree sends it.

What to watch for with it on: a kernel log line from the RM about a GSP heartbeat or a lost GPU that does not appear
with it off, especially around sleep, wake and (on laptops) `nvrtd3=1`.

## The wait queue

`os_wait_queue` now remembers a wake, as the Linux completion behind it does. Only the RM's NVLink fabric code uses it,
so no GeForce card reaches it. No switch.

## Looked at and left alone

`nv_destroy_nano_timer` cancels without waiting for a running callback, where Linux's `hrtimer_cancel` waits.
`thread_call_cancel_wait` is not available to kexts, and the callback reads the timer only in its first two loads before
it hands the event to an RM work item, so what a wait would close is a window of a few instructions. Not changed.
