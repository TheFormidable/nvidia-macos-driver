# Interrupt path, RC timer and nano timers: what differs from Linux, and the switches

Status: written from a reading of this tree against NVIDIA's open kernel modules 620.70.06. **None of it has run on a
GPU yet.** The code compiles for macOS 15 and 26 and its locking logic is tested on the host
(`tools/test_quiesce_gates.cpp`, `tools/test_irq_timer_wiring.py`). Every change below is **on by default**; each one
that changes behaviour has a boot-arg that turns it off. The old spellings `nvrmbhthread=1` and `nvrmrctimer=1` are
still accepted and mean the default.

| Boot-arg | Turns off | Use it when |
|---|---|---|
| `nvrmbhthread=0` | the bottom-half thread: `rm_isr_bh` runs on the work loop right after `rm_isr`, as in 2.9 | a hang with `debug.nvrm_bh_runs` frozen, or RM messages about lock ownership |
| `nvrmrctimer=0` | the RC timer: the RM's 1 Hz list never runs, as in 2.9 | a "GPU has fallen off the bus" (Xid 79) that does not happen with it |
| `nvrmnanowait=0` | the wait of a nano timer cancel or destroy for a callback that is running | `debug.nvrm_nano_wait_timeouts` grows |

All of them are read once, at the first use. All the counters below are read-only sysctls (`sysctl debug | grep nvrm_`),
readable over SSH while the screen is frozen.

## The interrupt path

Linux registers the GPU interrupt with `request_threaded_irq`: `rm_isr` runs in the hard interrupt and `rm_isr_bh` on
the interrupt's own kernel thread. Here the primary filter only counts and time-stamps the MSI; `rm_isr` runs on the
GPU's work-loop thread (`NVRM::interruptOccurred`), and `rm_isr_bh` on a thread of its own, one per GPU, named
`NVRM bottom half` (it shows in spindumps). A bottom half that waits for the GPU lock, for example behind a long RM
control from another process, then no longer delays the next `rm_isr`.

When `rm_isr` asks for a bottom half, it has handed the GPU lock over to it: only `rm_isr_bh` gives it back. So the
request alone decides (as on Linux), and a requested bottom half is always run, also when the GPU stops at the same
moment: the thread runs what is pending before it exits.

**The interrupt gate.** `interruptOccurred` enters the RM only through a gate. Sleep, RTD3, shutdown and stop disable
the interrupt source and then close the gate and wait, bounded, until no handler is inside and no bottom half is
pending or running (`irqSettle`). Wake and start open it again before the source is enabled (`irqOpen`). An MSI that
arrives at the closed gate is counted and not given to the RM, which is what a disabled source means; the RM raises
again after its resume.

**Which thread is in `rm_isr`.** The RM asks `os_is_isr()` to tell interrupt from thread context. It used to be one
global for all GPUs; with two cards the second card's handler overwrote and cleared the first one's mark while that one
was still inside `rm_isr`. Each GPU now has its own slot (8, one per possible card); a ninth card is not started.

| Name | Kind | Meaning |
|---|---|---|
| `nvrm-bottom-half` | NVRM registry property | `own thread`; `work loop (nvrmbhthread=0)`; or `work loop (no thread)` when the thread could not be made |
| `debug.nvrm_bh_thread` | sysctl | GPUs whose bottom half runs on its own thread (1 with one card) |
| `debug.nvrm_bh_runs` | sysctl | Bottom halves run |
| `debug.nvrm_bh_wake_max_us` | sysctl | Longest wait between a bottom half's request and the start of its run: the cost of the hand-off, well under 1000 expected |
| `debug.nvrm_bh_max_us` | sysctl | Longest bottom half, its wait for the GPU lock included |
| `debug.nvrm_bh_over_1ms`, `debug.nvrm_bh_over_4ms` | sysctl | Bottom halves over 1 ms and over 4 ms |
| `debug.nvrm_isr_count`, `debug.nvrm_isr_handled` | sysctl | MSIs taken, and those the RM claimed |
| `debug.nvrm_isr_latency_max_us` | sysctl | Longest wait between an MSI and its `rm_isr` |
| `debug.nvrm_isr_late_1ms`, `debug.nvrm_isr_late_4ms` | sysctl | Such waits over 1 ms and over 4 ms |
| `debug.nvrm_isr_during_bh` | sysctl | MSIs that arrived while a bottom half was running; with the thread they no longer wait for it |
| `debug.nvrm_isr_skipped` | sysctl | MSIs that found the gate closed (around sleep, shutdown, stop) |
| `debug.nvrm_irq_settle_timeouts` | sysctl | Waits for the interrupt path to go quiet that ran out. Should stay 0 |
| `debug.nvrm_bh_stuck` | sysctl | Bottom-half threads that did not exit within 5 s of the GPU's stop. Should stay 0 |

How to read them: compare `debug.nvrm_isr_late_*` and `debug.nvrm_isr_latency_max_us` during a session that stuttered
in 2.9 (4K playback, screen capture, a browser with video); they should be lower or equal. If something hangs and
`debug.nvrm_bh_runs` stops growing, boot with `nvrmbhthread=0` and say so in the report.

## The RC timer

On Linux `nv_start_rc_timer` arms a 1 s timer whose callback is `rm_run_rc_callback`; the RM starts it while it
initialises the adapter and stops it in `rm_disable_adapter`. In 2.9 both functions were stubs and the RM's 1 Hz list
never ran. Now a thread call does the timer's job, one per GPU.

What runs on that list on an RTX 50 card with NVIDIA's defaults:
- the check that the GPU still answers on the bus (one read of `NV_PMC_BOOT_0`; on a mismatch the RM reads again and
  only then declares the GPU lost);
- the NVENC session statistics, only while an encode session exists;
- the channel watchdog's tick and the GSP log poll, which do nothing here: the watchdog stays disabled (nothing sends
  `NV2080_CTRL_CMD_RC_ENABLE_WATCHDOG`) and no GSP log is loaded.

Not on the list: the GSP periodic health check. NVIDIA ships it off and the timer does not turn it on; it is registered
only with `nvregdwords=RmGspPeriodicHealthcheck=1`. A hung channel is therefore not seen by the RM watchdog; it is seen
by the user-space driver's own wait budget.

Differences from Linux, on purpose: the tick re-arms whatever `rm_run_rc_callback` returns (Linux lets the timer die on
the first refusal, such as a tick that lands during a power transition); and the driver pauses the timer itself before
RM standby (sleep, RTD3 and the shutdown path) and resumes it only after a resume that worked, instead of leaving it to
fire at a GPU without power. When the GPU stops, the timer is stopped by `rm_disable_adapter` and then let go of for
good, before the GPU's state is freed: no tick can reach a GPU that is gone.

| Name | Kind | Meaning |
|---|---|---|
| `debug.nvrm_rc_armed` | sysctl | GPUs whose RC timer runs (1 with one card) |
| `debug.nvrm_rc_ticks` | sysctl | Ticks that called the RM: +1 a second while awake |
| `debug.nvrm_rc_runs` | sysctl | Ticks the RM accepted. A busy GPU lock still counts here: the RM then runs the list when the lock is released |
| `debug.nvrm_rc_refused` | sysctl | Ticks the RM refused (GPU not at full power, in reset or lost). The timer goes on |
| `debug.nvrm_rc_stale` | sysctl | Late ticks turned away after a stop or a suspend |
| `debug.nvrm_rc_wait_timeouts` | sysctl | Waits for a running tick that ran out. Should stay 0 |

What to watch for: a kernel log line from the RM about a lost GPU (Xid 79) that does not appear with `nvrmrctimer=0`,
especially right after wake, with `nvrtd3=1`, or with `nvrmaspm=1` or a Thunderbolt enclosure (a platform that
glitches BAR0 reads).

## Nano timers

On an RTX 50 card every timer event of the RM is an OS nano timer, so this is the RM's whole timer service. On Linux it
is an hrtimer, and cancelling one waits for a callback that is running. Here a thread call cannot be stopped once a
thread-call thread has taken it off its queue (`thread_call_cancel_wait` is not exported to kexts), so a callback could
start after the timer was destroyed and read freed memory, then queue a work item for a freed event. That window was
the whole callback, not a few instructions.

Now:
- timer storage is never freed: timers live in slabs of 32 that are kept and reused (4096 at most; beyond that a create
  answers NULL, as a failed allocation did before);
- each arm carries a generation; cancel and destroy move it on, so a late callback finds a stale generation and returns
  without calling the RM;
- cancel and destroy then wait for a callback that is running, as `hrtimer_cancel` does: at most 2 s from a thread,
  50 ms where the caller may not sleep, and not at all from inside the callback itself. A timer whose wait ran out is
  never reused;
- a timer's deadline is computed in 64-bit nanoseconds (the 32-bit microsecond count ran out at 71 minutes);
- when a GPU stops, every nano timer the RM did not destroy is stopped, counted and kept out of use.

| Name | Kind | Meaning |
|---|---|---|
| `debug.nvrm_nano_slots` | sysctl | Timer slots made (tens) |
| `debug.nvrm_nano_live` | sysctl | Timers in use; returns to its idle value after the apps quit |
| `debug.nvrm_nano_fires` | sysctl | Callbacks run |
| `debug.nvrm_nano_stale` | sysctl | Late callbacks turned away |
| `debug.nvrm_nano_waits` | sysctl | Cancels and destroys that waited for a running callback |
| `debug.nvrm_nano_wait_timeouts` | sysctl | Such waits that ran out. Should stay 0; `nvrmnanowait=0` removes the wait |
| `debug.nvrm_nano_leaked` | sysctl | Timers the RM had not destroyed when its GPU stopped |
| `debug.nvrm_nano_bad_handle` | sysctl | Calls with a timer that was not in use (a second destroy, for example); each is a no-op |

## CPU facts

`os_get_cpu_count()` answers the kernel's number of logical CPUs (it was a flat 4); the RM sizes a per-CPU table by it
and reports it. `os_get_cpu_number()` stays 0 on purpose: the RM files the lockless interrupt state of `rm_isr` under
the CPU number and looks it up again with the number of that moment. On Linux `rm_isr` runs in the hard interrupt and
cannot move; here it runs on a thread that can move to another CPU in between, and one fixed number is collision-free
because each GPU has one such thread.

## Order of the teardown

`stop()` and the full shutdown path (`nvrmshutdown=full`) take the adapter down in Linux's order: the GPU's work queue
flushed (30 s at most in `stop()`, 2 s in the shutdown path), `rm_disable_adapter` (it stops the RC timer and masks the
GPU's interrupts), the queue stopped, the interrupt source off and the gate settled (2 s), `rm_shutdown_adapter`, then
the RC timer and the nano timers let go. A GPU that stops while it is suspended skips `rm_disable_adapter`: its RM is
in standby. When the system's 8 s bound for the shutdown runs out, the interrupt source is turned off and the gate
closed anyway, whatever the shutdown thread is doing.

## The wait queue

`os_wait_queue` remembers a wake, as the Linux completion behind it does. Only the RM's NVLink fabric code uses it, so
no GeForce card reaches it. No switch.
