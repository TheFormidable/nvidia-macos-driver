# Interrupt path, RC timer, nano timers and RM work: what differs from Linux, and the switches

Status: written from a reading of this tree against NVIDIA's open kernel modules 620.70.06. **None of it has run on a
GPU yet.** Apple's compiler on a Mac with macOS 26 built an earlier state of this code; the later changes (the reuse of
a work queue, the idle power-down monitor at stop, sleep and shutdown, the kext's reference on itself, the count of
retired nano timer slots) were compiled only by this repository's cross-compile check. The kernel functions the code
calls are checked against the export lists of macOS 15 and 26. The gates and the hold against the idle power-down
monitor are run with threads on the host (`tools/test_quiesce_gates.cpp`, `tools/test_rtd3_hold.cpp`);
`tools/test_irq_timer_wiring.py` checks the source text. Every change below is **on by default**; the five in the
table below have a boot-arg that turns them off, the others (the interrupt gate, the per-GPU interrupt mark, the CPU count,
the order of the teardown, the hold against the idle power-down monitor, the kext staying loaded) have none. The old
spellings `nvrmbhthread=1` and `nvrmrctimer=1` are still accepted and mean the default.

| Boot-arg | Turns off | Use it when |
|---|---|---|
| `nvrmbhthread=0` | the bottom-half thread: `rm_isr_bh` runs on the work loop right after `rm_isr`, as in 2.9 | a hang with `debug.nvrm_bh_runs` frozen, or RM messages about lock ownership |
| `nvrmrctimer=0` | the RC timer: the RM's 1 Hz list never runs, as in 2.9 | a "GPU has fallen off the bus" (Xid 79) that does not happen with it |
| `nvrmnanowait=0` | the wait of a nano timer cancel or destroy for a callback that is running | `debug.nvrm_nano_wait_timeouts` grows |
| `nvrmworkq=0` | the RM work queues: every RM work item gets a thread of its own and a flush waits for all of them, as in 2.9 (the driver's own bounded waits at stop, shutdown and sleep stay, on that flush) | a hang with `debug.nvrm_wq_flush_slow` growing, or `debug.nvrm_wq_abandoned` above 0 |
| `nvrmatomic=0` | the answers by context: every OS-layer function answers as in 2.9, whatever the context | a hang or panic in an RM call made with interrupts off (with `debug.nvrm_atomic_mode` -1 the old answers are already in force: the switch changes nothing) |

All of them are read once, at the first use. All the counters below are read-only sysctls (`sysctl debug | grep nvrm_`),
readable over SSH while the screen is frozen.

## The interrupt path

Linux registers the GPU interrupt with `request_threaded_irq`: `rm_isr` runs in the hard interrupt and `rm_isr_bh` on
the interrupt's own kernel thread. Here the primary filter only counts and time-stamps the MSI; `rm_isr` runs on the
GPU's work-loop thread (`NVRM::interruptOccurred`), and `rm_isr_bh` on a thread of its own, one per GPU, named
`NVRM bottom half` (it shows in spindumps). A long bottom half (it runs with the GPU lock that `rm_isr` took for it)
then no longer delays the next `rm_isr`.

When `rm_isr` asks for a bottom half, it has handed the GPU lock over to it: only `rm_isr_bh` gives it back. So the
request alone decides (as on Linux), and a requested bottom half is always run, also when the GPU stops at the same
moment: the thread runs what is pending before it exits.

**The surface fence's kick.** After every interrupt the work loop serves (whatever `rm_isr` answered) and after every
bottom half, the driver kicks the surface fence ([SURFACE-FENCE.md](SURFACE-FENCE.md)): one test whether the fence is
on, one atomic add, and a wakeup only when a thread sleeps in a fence wait. It takes no lock and never runs in the
primary interrupt filter. A channel's semaphore release is in memory before its non-stall interrupt is raised, and a
channel error reaches its notifier in the bottom half, so these two places are where a fence wait can be decided.
`debug.nvrm_fence_kicks` counts them while the fence is on; it is at most `debug.nvrm_isr_count` +
`debug.nvrm_bh_runs` and equal to neither, because one run of the work loop can serve several MSIs.

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
| `debug.nvrm_bh_max_us` | sysctl | Longest bottom half, microseconds |
| `debug.nvrm_bh_over_1ms`, `debug.nvrm_bh_over_4ms` | sysctl | Bottom halves over 1 ms and over 4 ms |
| `debug.nvrm_isr_count`, `debug.nvrm_isr_handled` | sysctl | MSIs taken, and those the RM claimed |
| `debug.nvrm_isr_latency_max_us` | sysctl | Longest wait between an MSI and its `rm_isr` |
| `debug.nvrm_isr_late_1ms`, `debug.nvrm_isr_late_4ms` | sysctl | Such waits over 1 ms and over 4 ms |
| `debug.nvrm_isr_during_bh` | sysctl | MSIs that arrived while a bottom half was running; with the thread they no longer wait for it |
| `debug.nvrm_isr_skipped` | sysctl | MSIs that found the gate closed (around sleep, shutdown, stop) |
| `debug.nvrm_irq_settle_timeouts` | sysctl | Waits for the interrupt path to go quiet that ran out. Should stay 0 |
| `debug.nvrm_bh_stuck` | sysctl | Bottom-half threads that did not exit within 5 s of the GPU's stop. Should stay 0 |

How to read them: 2.9 has none of these counters, so compare `debug.nvrm_isr_late_*` and
`debug.nvrm_isr_latency_max_us` after the same session (one that stuttered in 2.9: 4K playback, screen capture, a
browser with video) booted once as it is and once with `nvrmbhthread=0`; with the thread they should be lower or
equal. If something hangs and
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
RM standby (sleep, RTD3 and the shutdown path; it waits up to 2 s for a tick that is running) and resumes it only
after a resume that worked, instead of leaving it to fire at a GPU without power. When the GPU stops, the timer is
stopped by `rm_disable_adapter` (up to 10 s for a running tick) and then let go of for good, before the GPU's state is
freed: no tick can reach a GPU that is gone.

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
  50 ms where the caller may not sleep, and not at all from inside the callback itself. A timer that is destroyed
  while a callback may still be inside (the wait ran out, or was not made) is never reused: its slot is out of use for
  the rest of the boot, and counted. With `nvrmnanowait=0` that is the price of every destroy that meets a running
  callback; the RM's callback only queues a work item, so the two rarely meet;
- a timer's deadline is computed in 64-bit nanoseconds (the 32-bit microsecond count ran out at 71 minutes);
- when a GPU stops, every nano timer the RM did not destroy is stopped, counted and kept out of use.

| Name | Kind | Meaning |
|---|---|---|
| `debug.nvrm_nano_slots` | sysctl | Timer slots made, 32 at a time |
| `debug.nvrm_nano_live` | sysctl | Timers in use; returns to its idle value after the apps quit |
| `debug.nvrm_nano_fires` | sysctl | Callbacks run |
| `debug.nvrm_nano_stale` | sysctl | Late callbacks turned away |
| `debug.nvrm_nano_waits` | sysctl | Cancels and destroys that waited for a running callback |
| `debug.nvrm_nano_wait_timeouts` | sysctl | Such waits that ran out. Should stay 0; `nvrmnanowait=0` removes the wait |
| `debug.nvrm_nano_leaked` | sysctl | Timers the RM had not destroyed when its GPU stopped |
| `debug.nvrm_nano_retired` | sysctl | Slots taken out of use for the rest of the boot: the leaked ones, and each timer destroyed while a callback could still be inside. Should stay 0 while the GPU runs; the pool is 4096 less this |
| `debug.nvrm_nano_bad_handle` | sysctl | Calls with a timer that was not in use (a second destroy, for example); each is a no-op |

## RM work items

The RM hands work to the OS layer (`os_queue_work_item`) and later waits for it (`os_flush_work_queue`), as it does on
Linux with one kthread queue per GPU plus a global one. On macOS each item used to get a thread of its own, so items
of one GPU ran in any order and at the same time, and a flush waited for every item of every GPU. Now there is a
global queue and, from the second start pass on, one queue per GPU, each with one worker thread (`nvrm-wq0` for the
global queue, `nvrm-wq1`.. for GPUs): items run one at a time in the order they were queued, and a flush returns once
everything queued before it has run. The RM's own flushes wait as long as they need to (each 5 s of waiting logs the
item that is running); the driver's own waits are bounded: 30 s per step when a GPU is stopped, 2 s per step in the full
shutdown (`nvrmshutdown=full`), 5 s for the global queue before the GPU goes to standby (also on the default
shutdown path, which puts the GPU in standby). A GPU queue whose bounded wait runs out is given up
on: the teardown goes on, and later flushes of that queue return at once. If a GPU cannot get a queue of its own (four
at a time, less one for each queue that was given up on; or no thread) its items go to the global queue; that is never
a reason to fail its start. The queue of a GPU that stopped cleanly is used again by the next GPU that starts. Items
the RM marks to be dropped during an unload are dropped only while a GPU is being taken down or put to sleep, as on
Linux, not while it wakes up. `nvrmworkq=0` brings back the thread per item; the driver's own bounded waits stay.

| Name | Kind | Meaning |
|---|---|---|
| `debug.nvrm_wq_mode` | sysctl | 1 serialized, 0 `nvrmworkq=0`, -1 the worker could not start |
| `debug.nvrm_wq_queued`, `debug.nvrm_wq_run` | sysctl | RM work items queued, and run |
| `debug.nvrm_wq_depth_max`, `debug.nvrm_wq_item_max_us` | sysctl | Most items pending or running on one queue; longest item, microseconds |
| `debug.nvrm_wq_enq_atomic`, `debug.nvrm_wq_enq_fail` | sysctl | Items queued where blocking is not allowed; items refused (queue stopping, or no node where blocking is not allowed) |
| `debug.nvrm_wq_flushes`, `debug.nvrm_wq_flush_slow` | sysctl | Flushes; 5 s periods a flush waited |
| `debug.nvrm_wq_flush_self`, `debug.nvrm_wq_flush_atomic` | sysctl | Flushes asked for by a queue's own worker (answered at once); flushes refused because the caller could not block |
| `debug.nvrm_wq_gpu_queues`, `debug.nvrm_wq_abandoned` | sysctl | Per-GPU queues running; per-GPU queues given up on after a bounded wait ran out. Should stay 0 |

## Calls the RM makes where blocking is not allowed

The RM calls its OS layer under its own spinlocks, with interrupts or preemption off. Each OS-layer function now asks
the kernel which kind of context it is in (`preemption_enabled()`, checked at start-up against a lock taken with
interrupts off) and, where it may not block: `os_semaphore_may_sleep()` answers no, so the RM takes its own
non-blocking branches; allocations come from a 256 KB pool in the kext (requests up to 64 KB; anything else takes the
heap call it always took, and is counted); frees of heap blocks are handed to a thread call; a lock is tried, then
tried again every 20 us for up to 20 ms, and only then is the blocking call made that was always made there;
`os_schedule()` returns at once. Everywhere else every function does exactly what it did. `nvrmatomic=0` makes every
answer the old one.

| Name | Kind | Meaning |
|---|---|---|
| `debug.nvrm_atomic_mode` | sysctl | 1 on, 0 `nvrmatomic=0`, -1 the start-up self-test failed (then every answer is the old one) |
| `debug.nvrm_atomic_maysleep_false` | sysctl | Times `os_semaphore_may_sleep` answered no |
| `debug.nvrm_atomic_alloc`, `debug.nvrm_atomic_alloc_miss`, `debug.nvrm_atomic_pool_peak` | sysctl | Allocations the pool served; allocations where blocking is not allowed that it could not serve; most pool bytes in use |
| `debug.nvrm_atomic_free_deferred` | sysctl | Heap frees handed to a thread call |
| `debug.nvrm_atomic_delay`, `debug.nvrm_atomic_schedule` | sysctl | `os_delay` and `os_schedule` where blocking is not allowed |
| `debug.nvrm_atomic_lock_try`, `debug.nvrm_atomic_lock_spun`, `debug.nvrm_atomic_lock_fallback` | sysctl | Lock acquires where blocking is not allowed; of those, acquires that had to retry; that took the blocking call after 20 ms |
| `debug.nvrm_atomic_lockalloc`, `debug.nvrm_atomic_blocking_entry` | sysctl | Lock allocations, and blocking OS-layer calls entered, where blocking is not allowed |

Two more facts the RM now reports instead of having them ignored: `debug.nvrm_bugchecks` and
`debug.nvrm_bugcheck_last` (bug checks the firmware or the RM asked for; -1 none) and `debug.nvrm_rm_asserts` (RM
assertions that failed); neither stops the machine. `debug.nvrm_stub_hits` names the OS-layer functions without a body
that the RM called (`debug.nvrm_stub_hit_count` counts them).

## CPU facts

`os_get_cpu_count()` answers the kernel's number of logical CPUs (it was a flat 4); the RM sizes a per-CPU table by it
and reports it. `os_get_cpu_number()` stays 0 on purpose: the RM files the lockless interrupt state of `rm_isr` under
the CPU number and looks it up again with the number of that moment. On Linux `rm_isr` runs in the hard interrupt and
cannot move; here it runs on a thread that can move to another CPU in between, and one fixed number is collision-free
because each GPU has one such thread.

## Order of the teardown

`stop()` and the full shutdown path (`nvrmshutdown=full`, taken only when no user client is open) take the adapter down in
Linux's order: the GPU's work queue
flushed (30 s at most in `stop()`, 2 s in the shutdown path), `rm_disable_adapter` (it stops the RC timer and masks the
GPU's interrupts), the queue stopped, the interrupt source off and the gate settled (2 s), `rm_shutdown_adapter`, then
the RC timer and the nano timers let go. A GPU that stops while it is suspended skips `rm_disable_adapter`: its RM is
in standby. When the system's 8 s bound for the shutdown runs out, the interrupt source is turned off and the gate
closed anyway, whatever the shutdown thread is doing.

Before any of that, right after the surface fence is stopped, `stop()` takes the GPU away from the idle power-down
monitor (`nvrtd3=1`). That monitor is a timer that is never stopped; when the GPU has been idle for a minute it runs
the GPU's suspend path, and later its resume path, which opens the interrupt gate again. Its calls reach the GPU only
through a gate of their own: `stop()` closes it and waits until no call is inside (30 s at most; a call that is still
inside then keeps the GPU object alive and is logged). From then on the monitor has no GPU for the rest of the boot,
also when another card is started later.

System sleep and shutdown run the same suspend and resume paths themselves, so they hold the GPU against the monitor
for as long as they last: `setPowerState(0)` until the wake has run, `systemWillShutdown` for good. The hold waits
first for a monitor call that is already inside (30 s at sleep, 8 s at shutdown); every call that comes meanwhile does
nothing: a power-off it asks for fails, and the poll then counts the GPU idle from zero. The one exception is the
power resource's switch-on that the wake itself asks for, through the monitor's call, before the GPU's resume. Without
the hold a poll could suspend the GPU a second time while the system did, resume it while the system went to sleep
(it then woke marked running on a slot that had lost power), or switch the power resource off under the system's
resume. The hold alone does not cover a poll that is cut in two: its suspend and its power-off (or its power-on and
its resume) are separate calls, and a whole sleep and wake can pass between them. So each call also looks at the GPU's
state: the suspend path runs only on a running GPU, the resume path and the power-off only on a suspended one, and a
GPU whose wake failed is left alone. `tools/test_rtd3_hold.cpp` runs the hold and these checks against a model of the
GPU, with a negative control for each.

## The kext stays loaded

Nothing NVRM.kext registers is ever taken back: its sysctls (the kernel links them into its own list, but they are
the kext's data), the worker thread of the global work queue, the thread calls behind the timers, and the functions
NVAccel and NVRMFB call through pointers. That is only safe while the kext's code and data stay where they are, and
the kernel does unload kexts: by itself, about a minute after the last object of a kext's classes is gone (a start
that declined the card, a card that was unplugged), and when `kmutil unload` asks for it. So from the moment a start
registers its first sysctl, NVRM.kext holds one reference on itself for the rest of the boot. With it the kernel
refuses the unload before it terminates a service: `kmutil unload -b com.nullmoth.NVRM` fails with the kext in use and
the display keeps running. Should the reference ever not be obtained (logged at that start as
`no reference on this kext`), the kext's stop function refuses instead; the code then stays, but the driver cannot
start a card again before a restart.

A GPU that goes away is still stopped and torn down as described above; only the code stays. Installing, updating
and removing the driver are not affected: each rebuilds the kernel collection and takes effect at the restart it asks
for, and none of them unloads the running kext. Not yet observed on hardware.

## The wait queue

`os_wait_queue` remembers a wake, as the Linux completion behind it does. Only the RM's NVLink fabric code uses it, so
no GeForce card reaches it. No switch.
