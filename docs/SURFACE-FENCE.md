# The surface fence: the kernel knows who is still drawing into a surface

Status: the kernel code compiles with the kext's flags at 0 warnings and uses only symbols that macOS 15 and macOS 26
export to kexts; its logic (the table, the bounds, the wait loop) runs in host tests, also under real threads.
**None of it has been loaded or run on a Mac.** It is on by default. Boot-arg `nvrmfence=0` switches it off, and the
driver's surface sync then behaves as 2.9's did ("The one switch" lists what still differs).
Nothing of 2.9's surface sync was removed: it is what every fallback below lands on.

## What it is for

A window's picture lives in an IOSurface. One process draws into it with the GPU, another reads it: WindowServer
composites an application's surface, a capture reads WindowServer's, NVAccel flips or copies a surface WindowServer
has drawn. The reader must not read before the GPU has finished the writer's work.

2.9 had two mechanisms for that, and neither knew what the GPU was doing:

- WindowServer told NVAccel "this surface is pending" with a mark. NVAccel waited for the mark to clear, at most
  100 ms, and then went on, finished or not.
- An application wrote a "busy" tag on the IOSurface. A reader polled the tag every 100 us and gave up after 10 ms.

A writer that crashed left its mark or tag behind until the clock ended it, and a GPU that needed 150 ms for a frame
gave the reader a half-drawn picture.

The surface fence puts the missing knowledge into NVRM.kext: which connection is still drawing into which surface,
on which GPU channel, and up to which point of that channel's work. A reader now sleeps in the kernel until that
work is done and is woken by the GPU's interrupt. The kernel lets it go at once when the writer exits, when its
channel faults or when the GPU goes to sleep.

## How it works

**A hold** says "this connection is still drawing into this surface". A producer opens one hold per surface a command
buffer writes, at commit. A hold is in one of three states:

| State | Meaning |
|---|---|
| open | the GPU work is not submitted yet; the kernel has no target for it |
| armed | the kernel knows the channel and the completion value at which the work is done, and watches for it |
| gpu-done | the GPU is finished; an application has not published the surface yet |

**Two kinds of hold**, one for each of 2.9's mechanisms, so that who can delay whom did not change:

| Kind | Who opens it | When it ends | Whom it delays |
|---|---|---|---|
| compositor | WindowServer only (any other process is refused) | in the kernel, at the first look (a reader's, or the writer's next request) after the GPU has reached the armed value; also at its close, when WindowServer exits, when the channel faults | NVAccel's flips, copies and page-offs; readers of that surface in other processes |
| application | every other process | when the application says it has published (close); also when it exits or its channel faults | only a reader in another process that found this writer's tag on the surface |

**A reader** asks "is anything pending on this surface?" and sleeps until the answer is no. It waits for the holds
that existed when it arrived and for nothing opened later, so a surface that is redrawn back to back cannot keep a
reader forever. The answer tells it how the wait ended: nothing was pending; ready; the writer is gone (exit, channel
fault, GPU asleep); a hold was given up at its bound; the reader's own timeout ran out; a signal interrupted it.

Only "ready" says that the surface is complete. On the other answers the reader goes on by its own rule. NVAccel
goes on, as 2.9 did at its bound. A reader in the Metal plugin goes on when the kernel has kept it 10 ms or longer;
one that was let go sooner does what 2.9 did, which is to poll the writer's busy tag for up to 10 ms, or not at all
once the tag is 100 ms old. So no reader reads earlier than in 2.9 unless the hold ended as complete: the GPU
reached its value, or the writer closed it. The exception is NVAccel when every hold ends at once (sleep, the last
GPU stopping, the layer switching itself off): see "Limits to know".

The kernel remembers how the last 64 holds ended. A reader that waited through more endings than that is told the
worst of all of them, whoever's they were: "ready" only when every one was a completion.

## How the kernel sees the GPU work

NVK registers each of its channels once. It gives the kernel two addresses in its own memory: the channel's
semaphore page, where the GPU stores a 64-bit completion value at offset 0 each time a submission finishes, and the
channel's error notifier, whose status changes when the channel faults. NVK also stores, at offset 0xFF0 of the
semaphore page, the number of the last submission it sent, before that submission can run.

The kernel does not map these pages into its own address space. It wires them (they cannot be paged out or given
back to the system under it) and reads them through the kernel's map of physical memory, 8 bytes at a time. So there
is no kernel address that could fault when the process, or the RM, takes its own mapping away, and the pages stay
wired until the registration ends: when NVK deletes it, when the proof below fails, when the connection closes or
when its process exits.

A process can also hand in a page that it has mapped from a device, and such a page is wired as readily. So once a
page is wired the kernel looks at where it is, and in two cases gives it back at once and does not watch the channel
(`map_fail`):

- The page is beyond the kernel's map of physical memory. That map covers the installed memory and the 4 GB above
  it, and nothing further; a page beyond it has no address there, and reading it would stop the machine. The GPU's
  large memory window is such a place on the machines where this driver moves it to 256 GB and up.
- The page is inside one of the GPU's own memory windows, wherever they are: firmware also leaves them below 4 GB or
  right above the installed memory, inside the map. With `nvmultigpu=1` the windows of every started card are
  compared, not only the first card's. NVRM maps these windows, the registers included, into the processes the RM
  grants them to, and reading one would touch the GPU itself, also while it sleeps.

NVK's semaphore pages and notifiers are system memory, so this refuses nothing that NVK sends. A page of some other
device that lies inside the map cannot be told from system memory. The kernel only ever reads such a page, as the
process that mapped it can, and the proof below drops it unless it behaves like memory.

Before the kernel trusts such a view it has to prove itself twice:

1. At registration the process stores a value of its choice at offset 0xFF8 of the page and names it; the kernel
   must read exactly that value through its own view.
2. The process then stores a second value there and asks again; the kernel must now read the second value. A view
   that is a snapshot or a private copy of the page still shows the first one and fails.

A channel that fails either step is not watched: nothing is ever armed on it, a line goes to the log
(`NVRM-xnu: fence: pid N: ... - this channel is not watched`), `map_fail` or `map_mismatch` counts it, and that
process's surfaces keep 2.9's marks and tags. The cost of any failure here is the fence for that process while the
channel exists; the channel itself keeps working.

When NVK rebuilds a channel after a fault, the channel gets a new error notifier. NVK tells the kernel, which wires
the new page, swaps it in, forgets the old fault and lets the old page go. Nothing else about the channel changes
(`chan_rebinds` counts it).

## Nobody waits forever

Every hold has a bound. A hold that outlives it is given up: it delays nobody any more, the readers that were waiting
go on (their answer says "given up"), and the connection that owns it gets a strike.

| Hold | Bound |
|---|---|
| armed (waiting for the GPU) | 2000 ms since it was first armed |
| open or gpu-done (waiting for its writer's CPU) | 100 ms; 2000 ms while that connection still has GPU work in flight |
| an application hold of a connection in a penalty window | 20 ms |
| a compositor hold of a connection in a penalty window | 100 ms |

- A strike opens a penalty window for that connection: 10 s after the first strike, 20 s after the second, doubling.
  Inside it every hold of the connection has the shorter bound, and holds given up there are not strikes. For a
  compositor hold that bound is 100 ms, never less: NVAccel's page-off under the accelerator lock waits at most
  100 ms and 2.9 gave a WindowServer frame that long, so a WindowServer in a penalty window gets no less time for a
  frame than 2.9 gave it. An application that is stopped in a debugger costs its readers one 100 ms wait and then
  20 ms each; a stopped WindowServer costs 100 ms each, as in 2.9.
- Where a hold given up at its bound lets its readers go on, a plugin reader goes on at once when it sat out at least
  10 ms of that bound; one that arrived in the bound's last 10 ms asks the busy tag first. After the 100 ms and
  2000 ms bounds that tag is 100 ms old or older (to within microseconds for a hold that was never armed), so the tag
  ends the wait at once or within a poll step or two; only the 20 ms penalty bound can add a poll of up to 10 ms.
- One strike is forgiven for every 10 minutes without one.
- At the third strike the connection is distrusted: its new holds are refused. The holds it already has stay, because
  their surfaces were taken out of the older mechanism when they were opened; each ends by completion, its close or
  the penalty bound the strike set.
  That process is back on 2.9's marks and tags until it is restarted. Three stalls without a fault that were each recovered (for
  example three channel rebuilds after injected stalls within those 10 minutes) are enough when a reader waited on
  them and each came after the penalty window of the one before (10 s, then 20 s); a fault the channel reports in its
  notifier ends the holds without a strike. So `owners_distrusted` above 0 after such a test is expected and not a
  second problem.
- When eight connections have been distrusted since boot, the layer switches itself off until the next boot
  (`state` 2) and everything is 2.9. A WindowServer that crashes in a loop therefore ends in 2.9's behaviour, not in
  a hang.

Bounds are checked when a reader looks. A hold that nobody reads delays nobody, is never given up and gets no
strike; if its close never comes, its slot is taken back when room is needed (`reclaimed`).

A reader also has its own timeout: at most 2000 ms for a wait on a surface, at most 100 ms for a producer's wait on
its own channel. NVAccel uses 2000 ms where it has let go of the accelerator lock and 100 ms where it holds it. A
wait returns at its timeout plus what the system's timer adds to any timed sleep (well under a millisecond).

## What wakes a waiter

- **The GPU's interrupt.** After every interrupt the work loop serves, whatever the RM answered, and after every
  bottom half, NVRM calls the fence's kick. The kick takes no lock; it moves a counter and wakes the sleepers only when there are any. A woken reader reads the
  completion values again.
- **A change of the table.** A hold was closed, ended or given up.
- **Its own time slice**, in case neither came: 2 ms, doubling to 32 ms.

The kernel also checks whether interrupts really arrive for a channel. Until 8 completions have been seen on a
channel, and after that for as long as more than half of the recent ones were found by the slice and not by an
interrupt, waits on that channel's holds look every 250 us instead. A channel judged that way counts in `chan_noirq`
and gets a log line, and a producer that waits for its own channel is told to poll, as 2.9 did (`seq_refused`). The
judgement follows the recent past, so a channel whose interrupts return is trusted again.

The kick comes from every GPU interrupt, not only from the completion of the channel a reader waits for. A reader
woken for nothing reads two values and sleeps again.

## Who does what

**NVRM.kext** connects the fence to the rest of the driver. User-client selector 12 carries one request of exactly
96 bytes in and out. It does not take the lock that serialises a connection's RM calls, so a wait (up to 2 s) does
not hold up the process's other calls. macOS itself keeps one lock per connection, shared by all calls on it and
taken alone by a close of the connection and by a memory map or unmap through it: those wait until a fence wait on
the same connection returns. This is why user space keeps all its fence calls on one connection that it uses for
nothing else and never closes. NVAccel asks NVRM once for the fence's wait function (platform function `nvFenceOps`).
The fence starts with each GPU once the adapter is up, stops right after the notebook power manager when the GPU
stops, and first in a full shutdown (boot-arg `nvrmshutdown=full` with no connection open; the default shutdown puts
the GPU in standby, which tells the fence about sleep as a system sleep does); it is told about sleep before the user channels stop and about the
wake after the GPU is back. When a connection closes, the holds and channels it registered end before the RM frees
what that connection owns. When a process exits, all its holds and channels end at the first of its connections to
close, before the RM frees anything of it (NVRM.kext's `clientClose` calls `nvrm_fence_pid_gone` for this; see "A
process exits" below). After every interrupt the work loop serves, whatever the RM answered,
and after every bottom half, the fence is kicked so that waits on finished work end at once.

**NVK** registers every channel that takes one of its asynchronous submission slots (all of them unless `NVRM_ASYNC=0`
or the slots run out) with the kernel when the channel is created: it gives the kernel the
address of the channel's semaphore page and of its error notifier, writes a probe value, and asks the kernel to
confirm it sees a second value written after the registration. Only then does the kernel arm anything on that channel. Before
each submission reaches the GPU, NVK writes the submission's number at offset 0xFF0 of the same page, so the kernel
can tell a writer whose GPU work is still running from a writer that is stuck. When a channel is destroyed NVK
deletes the registration before it frees either page. A channel the kernel will not watch keeps working; while it
exists, that process's surfaces use the 2.9 marks and tags. A wait on the copy-engine queue, which raises no
completion event, can now sleep in the kernel on that channel's own semaphore, for at most 2 ms at a time and never
past the caller's timeout. Whether the kernel sleeps on a channel is the kernel's decision (it stops when that
channel's completions turn out not to wake it); whenever it declines, the wait polls as in 2.9 and asks again 50 ms
later.

**The Metal plugin** is the producer and one of the readers; its part is described in "The Metal plugin" below.

### NVAccel

NVAccel asks NVRM for the fence's wait function (platform function `nvFenceOps`) when the accelerator starts. NVRM
hands it out as soon as it is asked, also before its own fence has started on the GPU's start-up thread: until then
a wait finds nothing pending, without taking a lock. When NVRM refuses, its waits ask again, never more than once
a second (for `nvrmfence=0` see "The one switch"); until NVAccel has found NVRM they do not ask, and the first wait
after it has found it asks. `debug.nvaccel_surface_fence`
turns 1 when it has the function (one log line `surface fence: NVRM's fence v1`). The function is fetched once for
all GPUs and used for the rest of the boot; NVAccel keeps one reference on the NVRM that answered so that NVRM.kext
stays loaded under it. The accelerator built for AMD and Intel GPUs never looks for NVRM.
From then on every wait for a surface WindowServer is drawing asks the fence first and then the 2.9 mark table:

- the flip (on the asynchronous flip thread or WindowServer's), the copied frame and the hybrid paths wait as long
  as the GPU needs, at most 2 s;
- a page-off that has let go of the accelerator lock waits the same way, at most 2 s;
- a page-off that holds the accelerator lock keeps 2.9's 100 ms, fence and marks together. That bound holds whatever
  NVRM answers: the time its wait took comes off the 100 ms before the marks are looked at.

A wait ends when the GPU work is done, when the writer's process exits or its channel faults (counted as done:
`nvaccel_surface_fence_dead`), when the GPU goes to sleep, or at its bound (`nvaccel_surface_fence_timeouts`), and
the flip, the copy or the page-off then goes on, as in 2.9. A page-off also counts a surface as pending when the
fence has a hold on it (`nvaccel_surface_fence_pending`), which is what lets it wait without the accelerator lock.
NVAccel only ever waits for compositor holds: an application that is still drawing never delays a flip.

### The Metal plugin

- **What it does.** At commit, a command buffer that writes IOSurfaces opens one kernel hold per surface (at most 16;
  a command buffer that writes more keeps the 2.9 mechanism whole), in WindowServer as a compositor hold (ended by the
  kernel when the GPU reaches the armed value, when NVAccel publishes NVAccelSurfaceWait) and elsewhere as an
  application hold (ended when the producer has published). Each hold is armed with the VkFence of the last part that
  writes the surface, right after that part is submitted. Every command buffer closes what it still holds when it
  finishes, on every path (completion, a failed or lost submission, a lost device); WindowServer also closes each
  surface as it releases it.
- **Readers.** A busy tag whose pad word is `0x4B464E43` ("the kernel holds this write") makes the reader sleep in the
  kernel, up to 2 s, until the holds it found end. Only the answer that the write is complete ends the reader's wait
  by itself. When the kernel answers that it let the hold go or holds nothing (the writer exited, a channel fault, the
  GPU going to sleep, the fence stopping, a hold at its bound), the reader goes on at once only if the kernel has by
  then kept it 10 ms; otherwise it does what 2.9 did with the tag, which the writer still writes and clears: a tag
  older than 100 ms is passed at once, a newer one is polled for up to 10 ms. An interrupted wait or an error does the
  same. So a reader reads a surface earlier than 2.9 did only on the kernel's answer that the write is complete, and
  never waits longer than the kernel's bound for the hold plus 10 ms. (When more than 64 holds ended during one wait,
  the kernel no longer knows how this one ended. It then gives that answer only if every one of them was a
  completion; otherwise it answers with the worst that ended, given up or gone, and the reader does as above. Sleep,
  a stop of the fence or the exit of a process with that many holds open can do it.) A tag that names no writer
  (pid 0) is never asked of the kernel. A reader in any process but WindowServer that imports a surface no
  application wrote waits for WindowServer's compositor holds on it (a capture of a frame WindowServer is still
  drawing).
- **Exactly one mechanism per command buffer.** The decision is the open's: a hold the kernel took replaces that
  surface's pending mark (not sent) and is announced in the busy tag's pad word; a hold the kernel refused (table
  full, a channel the kernel does not watch, a distrusted process, an older kext, `nvrmfence=0`) leaves the command
  buffer's tags and marks exactly as 2.9 wrote them. Surface 0 (WindowServer's once-a-second probe of the NVAccel
  marks) always reaches the kernel.
- **Log lines** (the plugin's log, `nvmtl.log`, which an installed release plugin writes only for a process started
  with `NVMTL_PROCESS_ENV=1`: [TROUBLESHOOTING.md](TROUBLESHOOTING.md), "The new paths of release 3.0"):
  `surface fences: kernel (NVRM fence v1, compositor)` when the fence
  is on and NVAccel reads the kernel's table; `surface fences: kernel (NVRM fence v1; NVAccel does not read it, so
  WindowServer keeps its pending marks)` when NVAccel does not; `surface fences: off (...)` with the reason (`NVK or
  the kext answered 0x80000002: no fence there` for an older kext; `the kext's fence is not on for this process:
  state S` for `nvrmfence=0` (S 0), a fence that stopped or switched itself off (3 or 2), or NVK's
  `NVRM_ASYNC=0` (S 1); `request N answered off` when a later request is refused that way; an NVK without the fence
  writes no line); one line per change. `surface fences: hold refused (0x8000000N) - that command buffer keeps the older tags and marks (N so
  far)`: the first eight and then every 2000th. `surface fences: surface S - the kernel gave up pid P's hold at its
  bound after U us`: the kernel ended a hold the reader waited for at its safety bound (first eight, then every
  2000th).
- **Cost.** One OPEN (two for 9 to 16 surfaces), one ARM per part that is the last writer of a surface (two when it is
  the last writer of more than 8), one CLOSE per
  command buffer (WindowServer: plus one per surface), only for command buffers that write an IOSurface. While the
  fence is off, one QUERY a second at most.

## What falls back to what

Each precondition is checked while the driver runs. When one fails, the part named keeps 2.9's behaviour and a
counter says so.

| What is missing or refused | Counter or sign | What runs instead |
|---|---|---|
| The kext has no fence (an older NVRM.kext is still loaded), or `nvrmfence=0` | `state` 0, or no `debug.nvrm_fence_state` at all | everything as 2.9 |
| The layer switched itself off, a resume failed, or the last GPU stopped | `state` 2 or 3 | everything as 2.9 |
| NVAccel does not read the fence | `accel` 0 | WindowServer keeps 2.9's marks; application holds work without it |
| A channel's pages cannot be wired or are not system memory, or the view fails its proof | `map_fail`, `map_mismatch`, a log line | that channel is not watched; that process uses 2.9's marks and tags |
| No room: the table (1024 holds), a connection's quota (128 holds, 32 channels, 4 connections per process) | `refused_full` | that command buffer uses 2.9's marks or tags |
| More than 256 threads asleep in user-space waits | `wait_full` | that reader polls as 2.9 (NVAccel's waits are never refused for this) |
| A compositor hold asked for by a process that is not WindowServer | `refused_denied` | refused |
| A channel's completions arrive without interrupts | `chan_noirq`, `done_on_slice`, a log line | the kernel looks every 250 us for holds on it; producers' own waits on it poll |
| A writer does not finish or close what it holds | `expired_gpu`, `expired_cpu`, `strikes`, a log line | readers go on at the bound; after three strikes that process is on 2.9 |
| The GPU is asleep | `end_pm` | every hold ends, new ones are refused until the wake; 2.9's marks and tags meanwhile |
| A reader is interrupted by a signal | `wait_aborted` | it polls as 2.9 |
| The kernel answers a plugin reader that it holds nothing, or that it let the hold go (dead writer, sleep, stop, a bound), before it has kept that reader 10 ms | `wait_none`, `wait_dead`, `wait_timeout` | it polls the busy tag as 2.9 (a tag older than 100 ms: no wait) |

## The one switch

Boot-arg `nvrmfence=0`, read once when the first GPU starts (or when NVAccel asks first). With it
`debug.nvrm_fence_state` is 0, every request on selector 12 but QUERY (which reports state 0, not active) is answered
"off" before a page of the process is touched, NVAccel is refused the wait function with an answer that tells it the
fence is off for the boot, and the kick returns at once. The plugin, NVK and NVAccel take their
2.9 paths for surface sync. There is no opt-in, no environment name and no writable sysctl. It is the first thing to
try if anything about frames looks worse than in 2.9. `nvrm_fence_off_by_bootarg()` tells NVRM.kext's other parts
that the fence is off for the whole boot, as opposed to not started yet.

It switches the surface fence off, not everything this release changed next to it. With `nvrmfence=0` these still
differ from 2.9:

- NVAccel writes one line, `surface fence: off for this boot (boot-arg nvrmfence=0) -- waits use the 2.9 marks; NVRM
  is not asked again`, and does not ask NVRM for the fence again before the next boot.
- Every process that uses the GPU through NVK still opens its fence connection (one idle control connection per
  process) and asks once (the answer: not active, state 0). Each channel it creates still sends one registration on
  it, answered "off", and the Metal plugin asks again at most once a second while it has surfaces to draw.
- NVK still writes the word at 0xFF0 of each channel's semaphore page before each submission, and the one at 0xFF8
  when it registers the channel. Nothing reads them.
- NVK's wait for a completion event and NVAccel's page-off without the accelerator lock are not part of the fence and
  have their own ways back: environment `NVRM_EVENT_WAIT=0` and boot-arg `nvaccel_pageoff_locked=1`.

## The counters

`sysctl debug | grep nvrm_fence` shows all of them. All are read-only; a value is a moment old at most. "Since
boot" unless it says "now".

`debug.nvrm_fence_state`:

| Value | Meaning |
|---|---|
| 0 | off for this boot by `nvrmfence=0` |
| 1 | on |
| 2 | switched itself off: eight connections were distrusted since boot. Off until the next boot. |
| 3 | not started yet, stopped with the last GPU, or off after a resume that failed (then until the next boot) |

| Counter (`debug.nvrm_fence_` + name) | Meaning | What to expect |
|---|---|---|
| `accel` | 1 once NVAccel fetched the wait function | 1 within a second or two of the start |
| `kicks` | interrupts the work loop served (whatever the RM answered) plus bottom-half runs, while on | grows whenever the GPU works. It is not the number of interrupts: several MSIs can be served by one run of the work loop, so it stays at or below `debug.nvrm_isr_count` + `debug.nvrm_bh_runs` |
| `opens` | holds opened | grows with drawing |
| `arms` | holds given a GPU target | grows with `opens` |
| `arm_done` | of those, already complete when armed | any value |
| `closes` | holds closed by their writer | grows with `opens` |
| `close_gone` | closes that found no hold | grows in normal use: WindowServer's holds end in the kernel before its close arrives |
| `auto` | compositor holds ended by the kernel at GPU completion | grows with every WindowServer frame |
| `end_owner` | holds ended because their writer's connection closed or its process exited | grows when a process that was drawing exits or is killed |
| `pid_gone` | connections emptied because their process was exiting, at its first connection to close | grows when a GPU process exits and another of its connections closes before its fence connection (see "A process exits"); when the fence connection closes first, only `end_owner` moves |
| `end_chan_err` | holds ended by a channel fault | 0 unless a channel faulted (an Xid in the log) |
| `end_chan_del` | holds ended because their channel was deleted first | 0 or small |
| `end_pm` | holds ended by GPU sleep | grows by the holds in flight at each sleep |
| `refused_full` | requests refused for room: table or quota | 0 |
| `refused_denied` | compositor holds asked for by a process that is not WindowServer | 0 |
| `refused_stale` | requests naming an unknown hold or channel | small |
| `refused_distrust` | holds refused to a distrusted connection | 0 |
| `chans` | channels watched now | a few per process that uses the GPU |
| `chan_adds` | channels registered | above 0 after login |
| `chan_noirq` | channels whose completions arrive without an interrupt, now | 0; see "What to look at", point 5 |
| `chan_rebinds` | rebuilt channels given a new error notifier | 0 unless a channel was rebuilt after a fault |
| `map_fail` | channel pages that could not be wired, or that are not system memory | 0 |
| `map_mismatch` | channels whose page did not show what the process wrote | 0 |
| `live` | holds that delay readers now | returns to 0 when nothing draws |
| `live_max` | most such holds at once | far below 1024 |
| `reclaimed` | slots of given-up or forgotten holds taken back for new ones | 0 or small |
| `waits` | waits that found something pending | grows with use |
| `wait_none` | waits and looks that found nothing pending | the large one: most readers find nothing in their way |
| `look_pending` | looks (no sleep) that found something pending | grows with page-offs of surfaces being drawn |
| `wait_ready` | waits ended by completion | nearly all of `waits` |
| `wait_dead` | waits ended by writer exit, channel fault or GPU sleep | small |
| `wait_timeout` | waits ended because a hold was given up | 0 |
| `wait_budget` | waits ended by the caller's own timeout | 0 or small (NVAccel's 100 ms waits under its lock) |
| `wait_aborted` | waits ended by a signal | small |
| `wait_full` | waits refused because too many threads sleep | 0 |
| `wake_irq` | sleeps ended by an interrupt | grows whenever readers sleep while the GPU works |
| `wake_state` | sleeps ended by a change of the table | grows with application holds (they end by a close) |
| `wake_slice` | sleeps ended by their own time slice | small next to `wake_irq` |
| `done_on_irq` | waits that found their surface free after an interrupt | grows with use |
| `done_on_state` | the same after a change of the table | grows with use |
| `done_on_slice` | the same after a time slice: no interrupt told them | near 0; far below `done_on_irq` |
| `expired_gpu` | holds given up after 2000 ms | 0 |
| `expired_cpu` | holds given up after 100 ms with their writer's GPU idle | 0 (1 for each `kill -STOP` test) |
| `expired_penalty` | holds given up in a penalty window: after 20 ms, a compositor hold after 100 ms | 0 |
| `strikes` | strikes against connections whose holds were given up | 0 |
| `owners_distrusted` | connections refused after three strikes | 0 |
| `close_gpu_agree` | armed holds closed with the kernel seeing their work complete | grows with application drawing |
| `close_gpu_behind` | armed holds closed before the kernel saw their work complete | 0 or a handful |
| `wait_max_us` | longest wait that slept, microseconds | 2,000,000 at most, plus the timer's lateness |
| `wait_lt1ms`, `wait_lt10ms`, `wait_lt100ms`, `wait_ge100ms` | waits that slept under 1 ms, 1 to 10 ms, 10 to 100 ms, 100 ms or more | `wait_ge100ms` says how often 2.9 would have gone on with an unfinished frame |
| `seq_waits` | producer waits for its own channel | grows with uploads on the copy queue |
| `seq_slept` | of those, slept in the kernel | those whose work was not done yet |
| `seq_refused` | of those, told to poll: the channel raises no interrupt | 0 if copy-queue completions wake the kernel; see point 5 |

A wait that outlasted more than 64 endings is counted under the worst of all of them (`wait_timeout` before
`wait_dead` before `wait_ready`), also when the hold that was given up or lost was not one it waited for.

"It works" is: `state` 1, `accel` 1, `auto` and `done_on_irq` grow with use, and `done_on_slice`, `chan_noirq`,
`expired_*`, `map_*`, `strikes`, `owners_distrusted` and `close_gpu_behind` stay at or near 0.

NVAccel's own counters for its waits are `debug.nvaccel_surface_fence` and `debug.nvaccel_surface_fence_waits`,
`_ready`, `_dead`, `_timeouts`, `_errors`, `_pending`, `_wait_max_us`.

## The log lines

All start with `NVRM-xnu: fence:`. Of each kind the first 8 are written, then every 256th.

| Line | Meaning |
|---|---|
| `surface 0x.. writer pid N state S for M ms - given up (...), strike K` | a hold outlived its bound; S is `open`, `armed` or `gpu-done`, which tells where the writer was stuck |
| `pid N: K holds given up - this connection's new holds are refused from now on (it keeps the older surface sync)` | third strike: that process is on 2.9's marks and tags until it restarts; the holds it already had end on their own (completion, close, or the penalty bound) |
| `8 connections refused since boot - the surface fence is off until the next boot (debug.nvrm_fence_state 2)` | the layer switched itself off |
| `channel C of pid N completes without an interrupt - it is polled every 250 us instead` | see `chan_noirq` |
| `pid N: ... - this channel is not watched (its surfaces keep the older surface sync)` | a channel's page could not be wired, or the kernel's view of it failed its proof; the text says which |

NVK prints at most one line per process to stderr, starting `nvrm: surface fence:`, when something keeps the fence
from a channel or from the process. A kext without the fence (2.9's, still loaded after an update until the restart)
is not a fault and is said only with `NVK_DIAG=1` in `nvrm610.conf`, as `nvrm: surface fence: the kext has none` in
`/private/tmp/nvk-diag-<pid>.log`.

## Sleep, wake, shutdown, a process that dies, a channel that faults

- **Sleep.** Before the user channels stop, every hold ends (NVAccel's waits leave at once; a plugin reader that
  leaves this way polls the writer's busy tag for up to 10 ms, as in 2.9, before it reads) and new holds are refused
  until the wake. The kernel's views of the channel pages are system memory and survive. After a resume that failed the
  layer stays off until the next boot (`state` 3).
  Not covered: a hold armed on a channel whose work the RM preempts at sleep ends as if it were done, so a reader that
  reads the surface during the sleep or right after the wake can see a frame the GPU never finished, as after a wait
  that reached its bound. Holds are not kept across the sleep, even those on channels the kernel has verified; see
  "What is deliberately not done".
- **Shutdown, or the last GPU stops.** Every hold ends and every sleeper is woken before the adapter goes down.
  Not covered: the channels stay registered, and their pages stay wired, until the connections that registered them
  close; the kernel stops reading them, since no hold is armed on them and new ones are refused.
- **The layer switches itself off** (`state` 2, eight distrusted connections). New holds are refused at once, and
  every hold that still exists ends at that moment: the readers waiting on them are told the hold was given up.
  NVAccel's waits find nothing in the fence from then on and use 2.9's marks; a plugin reader that was told so polls the writer's busy tag
  as 2.9 did (unless the kernel had already kept it 10 ms), and later readers are answered "off" and use the tag.
  Not covered: work those holds stood for may still be running on the GPU, so a reader can then see an unfinished
  frame, as after a wait that reached its bound. (A single distrusted connection is different: its existing holds stay
  and end on their own, see "Nobody waits forever".)
- **A process exits or is killed.** Each connection, when it closes, ends its own holds and lets its own pages go
  before the RM frees what that connection owns. NVK keeps its fence calls on a connection of their own and its
  channels on another, and macOS closes a dying process's connections in no set order, so for the holds to end
  before the RM frees the channels they are armed on, the first connection of the process to close must end all of
  them. NVRM.kext does that by calling `nvrm_fence_pid_gone` in `clientClose`, before the RM's cleanup, when the
  connection's process is exiting (`pid_gone` counts it); and from the moment a process is exiting the fence refuses
  it new holds and channels. So when the process is WindowServer, NVAccel's waits on its surfaces end at once, where 2.9 waited 100 ms for a
  mark nobody would clear; a plugin reader falls back to the busy tag, which costs it what 2.9 did (at most 10 ms per read for the
  first 100 ms).
- **A channel faults.** The RM writes the fault into the channel's notifier in the bottom half, the kick follows,
  and holds armed on that channel end. If NVK rebuilds the channel, later holds are watched again.
- **Two GPUs** (`nvmultigpu=1`). There is one table for all. While one GPU is suspended, new holds are refused for
  the processes of the other one too, until that GPU has resumed or stopped.

## What is deliberately not done

- **Exportable sync objects.** A `VkFence` or a timeline semaphore cannot be exported to, or imported from, another
  process. The kernel knows a channel and a completion value only for surface holds; Vulkan's sync-file import and
  export stay unimplemented in NVK.
- A wait inside the GPU (one channel's work waiting for another process's semaphore). The reader's CPU thread waits.
- NVAccel waiting for application holds. 2.9 did not either.
- Keeping holds across sleep. Every hold ends before the user channels stop, also an armed one on a channel the
  kernel has verified. Keeping those would mean pausing without ending them, ending them only on a failed resume,
  and giving them the 2000 ms bound across the sleep; but the RM preempts the user channels before the interrupts
  stop, a channel may never complete after the wake, and NVAccel's waits cannot be interrupted, so a flip or page-off
  could sit out a full bound on a GPU that is asleep.
- Showing the previous picture when a flip reaches its bound. The flip goes on, as in 2.9.
- A check that the process that opens a hold can see the surface. A hold names a surface by its id.
- Freeing the table, the lock or the sysctls at unload. NVRM.kext is not unloaded while the system runs.

## Limits to know

- The test "is this WindowServer" is the process name, as for 2.9's marks. It keeps applications from delaying the
  compositor by accident; it is not a security boundary. The bounds are: no hold, of either kind, delays anybody
  past them.
- A hostile local process can fill its own quotas or the 256 sleeper places and push others back to 2.9's
  behaviour, and eight distrusted connections (one process can supply them one after another) switch the layer off for the boot. It cannot make another process wait
  longer than the bounds.
- A process that registers a channel and frees or unmaps the page without deleting the registration keeps that page
  wired until its connection closes, and more than the page: macOS cannot take the freed memory apart while the
  kernel's view of one page of it exists, and it waits for that on the one thread that reaps dead memory, so other
  memory queued for that thread is not returned either until the registration ends. NVK deletes first, and at a
  process's exit the views go before its memory does; a process that breaks the rule on purpose can hold memory
  back this way for as long as it keeps its connection open, as it can with any driver that wires its pages.
- A GPU that goes to sleep in the last moment of its start is not announced to the fence. Holds opened then end at
  their bounds, with a strike.
- When every hold ends at once (the GPU goes to sleep, the last GPU stops, the layer switches itself off),
  WindowServer's work in flight has neither a hold nor a 2.9 mark: no mark was sent for a surface the kernel held.
  NVAccel's flip, copy or page-off of such a surface then goes on at once and can show a frame the GPU has not
  finished, once, as 2.9 did when its 100 ms ran out. An application's surface still carries its busy tag, and its
  readers fall back to that.

## What to look at on the card

Read with `sysctl debug.nvrm_fence_state` and so on.

1. **After login.** `state` 1, `accel` 1, `chan_adds` above 0, `map_fail` 0, `map_mismatch` 0. If `map_fail` or
   `map_mismatch` is not 0, send the `NVRM-xnu: fence:` lines: the driver is running as 2.9 for those processes and
   that is the thing to fix first. This is the step that has never run anywhere: the first registration of a
   channel happens in WindowServer at the login window.
2. **Desktop in use for a minute.** `opens`, `arms` and `closes` grow together; `auto` grows with every WindowServer
   frame; `done_on_irq` is far larger than `done_on_slice`; `chan_noirq` 0; `expired_gpu`, `expired_cpu`, `strikes`
   and `owners_distrusted` 0; `close_gpu_behind` 0 or a handful. 2.9's counters
   `debug.nvaccel_surface_gpu_marks`, `_waits` and `_timeouts` stand still, because nothing uses the old marks.
3. **A 4K video or a game with the GPU busy** (the case where `debug.nvaccel_surface_gpu_timeouts` rose in 2.9): no
   tearing or flicker; `debug.nvaccel_surface_fence_waits` grows, `debug.nvaccel_surface_fence_timeouts` stays 0,
   and `wait_ge100ms` shows how often a flip really needed more than 100 ms.
4. **Capture, browser and video while the GPU is loaded** (frames over 10 ms): no half-drawn frames; `wait_lt100ms`
   grows where 2.9 would have given up.
5. **`chan_noirq` above 0, `done_on_slice` growing fast, or `seq_refused` growing.** Interrupts do not arrive for
   some channels (the copy-engine queue is the open question). It still works, in 250 us looks; report which
   processes (the log line names the pid and says `(copy engine)` for such a channel).
6. **Kill a GPU application mid-frame** (`kill -9`): `end_owner` grows (and `pid_gone`, unless the process's fence
   connection happened to close first), no stall in WindowServer.
   Expect up to 10 ms per composite of that application's surfaces for 100 ms, as in 2.9 (the plugin reader polls
   the dead writer's busy tag): not a stall, but not zero.
   **Stop one** (`kill -STOP`, then `-CONT`): one `expired_cpu` and one `strikes`, a single hitch of at most
   100 ms, nothing after.
7. **Sleep and wake.** `end_pm` grows by the holds that were in flight; `state` is 1 afterwards and `opens` grows
   again.
8. **Any `owners_distrusted`, or `state` 2.** Something systematically fails to complete or close; the log line
   names the surface, the pid and the state the hold was stuck in. The driver is on 2.9's behaviour for that process
   (or entirely, with `state` 2) until it restarts (or the machine does).
9. **Way back.** Boot once with `nvrmfence=0`: `state` 0, 2.9's counters move again, surface sync is 2.9's ("The one
   switch" says what still differs).
10. **Cost.** The kick is one test, one atomic add and one more test, plus a wakeup when a reader sleeps; no counter
    shows it apart. `debug.nvrm_isr_latency_max_us` does not include it (it ends where the RM's handler starts).
    `debug.nvrm_bh_max_us` and `debug.nvrm_bh_over_1ms` do include the kick after each bottom half: compare them over
    the same use with and without `nvrmfence=0`; a difference beyond noise is worth reporting.
