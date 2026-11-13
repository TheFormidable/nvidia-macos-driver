# Event waits: when a waiting thread sleeps instead of polling

A thread that waits for GPU work (a fence wait, or a channel waiting for its own work to finish) can either poll the
completion value or sleep in the NVRM kext until the GPU's graphics completion event arrives. Polling costs CPU for as
long as the wait lasts; sleeping costs one wake-up latency at the end (about 0.13 ms measured below). Since this
release every process uses both, chosen per wait:

- WindowServer: as before, the wait spins for 20 microseconds and then sleeps on the event, at most 2 ms per sleep.
  As before, that sleep does not look at the caller's timeout: a wait with a timeout can return up to 2 ms after it in
  WindowServer (and in any process run with `NVRM_EVENT_WAIT=1`).
- every other process: the first millisecond of a wait is polled exactly as before (a short pause loop up to 50
  microseconds, then `sched_yield`), so short waits keep their latency. A wait that is still running after 1 ms
  sleeps on the event instead of polling with `usleep(50)`. A thread whose waits run longer than about 2 ms (the
  50-microsecond pause loop plus one 2 ms sleep) soon sleeps right after the pause loop: from its third wait when they
  run longer than 3 ms, after some fifty waits when they are shorter. Waits between 1 and 2 ms are polled for their
  first millisecond and then sleep, except about one in forty that sleeps right after the pause loop. (The thread
  keeps a running mean of the time into each wait at which its last polling-or-sleeping step began;
  `tools/test_evwait_policy.c` checks these numbers.) A sleep never runs past the caller's own timeout and does not
  start when less than 100 microseconds of it remain. The same holds for the sleep on the copy-engine queue's own
  semaphore ([SURFACE-FENCE.md](SURFACE-FENCE.md)), in every process: it is asked for in whole milliseconds, so it
  starts only when at least one millisecond of the caller's timeout remains.
- a sleep lasts at most 2 ms, so a missing event costs latency, never a hang. The GPU makes a completion visible
  before the kext posts its event (the interrupt has to be serviced and RM has to run first), so now and then a sleep
  runs out in that gap with the work already done: that is a missed wake, and on a healthy machine
  `debug.nvrm_evt_user_missed` moves a little because of it. The late event shows at the thread's next call, and any
  answer that shows the event's count moved ends the run of missed wakes. Only sixteen missed wakes in a row with the
  count standing still all the while mean the event is dead: then the process stops using the event and polls for
  the rest of its life; it prints one line
  (`nvrm: event wait OFF for this process (16 wakes in a row did not arrive) - waits poll as before`) and the kext
  counts it in `debug.nvrm_evt_user_fallbacks`. A kext that cannot wait at all is detected at the first call and the
  process polls, as before.

## The setting

`NVRM_EVENT_WAIT` in `/Library/GPUBundles/nvmtl/nvrm610.conf` overrides the choice for every process, WindowServer
included:

| Value | Every process |
|---|---|
| unset or empty (the default) | WindowServer sleeps after 20 microseconds; every other process follows the rule above |
| `0` | polls, as every process did in 2.9 with `0`. This is the way back if the event wait is suspected; it also makes WindowServer poll, which costs WindowServer more CPU than both 2.9's default and this release's |
| `2` | the rule above, WindowServer included |
| `1` (or any other value) | sleeps after 20 microseconds, as WindowServer always did |

No value gives exactly 2.9's default (WindowServer sleeping, applications polling). Release builds remove inherited
`NVRM_` variables before the driver is loaded, so the file is the way to set it, and a line there applies to every
process that starts afterwards (log out and in for WindowServer). To try one application without the event wait,
launch it with `NVMTL_PROCESS_ENV=1 NVRM_EVENT_WAIT=0` in its own environment: `NVMTL_PROCESS_ENV=1` makes a release
plugin keep that process's own settings.

`NVRM_GSUB_LOG=1` in the same file prints, when a channel closes, how many waits slept on the event (`event sleeps`),
were woken by it, ran out, or slept on the copy queue's own semaphore (`chansleeps`), and whether the process went
back to polling (`fellback`).

## What the kext reports

Selector 6 still returns the event count in its low 32 bits, so an older user space reads the same number. From this
release the kext also says, in bits 32..39, why the call returned: the count was read, it had already moved, an event
woke the sleep, the sleep ran to its timeout, the sleep was interrupted, it woke with nothing posted, or too many
threads (256) were already asleep. In the last case the call returns at once, as before, but the caller now knows it
did not sleep and takes one polling step instead of coming straight back.

Read these together, while an application that waits on the GPU is running:

    sysctl debug.nvrm_evt_posts debug.nvrm_evt_matched debug.nvrm_evt_waits debug.nvrm_evt_slept \
           debug.nvrm_evt_woken debug.nvrm_evt_timeouts debug.nvrm_evt_capped debug.nvrm_evt_clients \
           debug.nvrm_evt_user_missed debug.nvrm_evt_user_fallbacks \
           debug.nvrm_evt_post_drain_waits debug.nvrm_evt_post_drain_stuck

- `nvrm_evt_clients` 2 or more: applications, not only WindowServer, sleep on the event.
- `nvrm_evt_woken` against `nvrm_evt_slept`: while the GPU is busy most sleeps should end woken; timeouts during long
  GPU jobs are normal. Almost all timeouts while `nvrm_evt_posts` keeps rising means events arrive late.
- `nvrm_evt_user_missed` is the number of full 2 ms sleeps that ended on their timeout although the GPU work was
  already done. It should be small next to `nvrm_evt_woken`; a few are normal, for the reason given above.
- `nvrm_evt_user_fallbacks` 0. The kext also prints
  `NVRM-xnu: pid N went back to polling: its event wakes did not arrive (fd F)`, for the first eight fall-backs since
  boot only (any process can send the note, so the output is bounded); the counter counts all of them. That line and
  the two below go only to the kernel's debug output (`kprintf`: a serial console or a debugger), not to the log that
  `log show` reads, so on an ordinary Mac only the counters show them. Only this counter above 0, together with the line
  `nvrm: event wait OFF for this process (16 wakes in a row did not arrive)` in the application's output, means a
  process stopped trusting the completion event.
- `nvrm_evt_capped` 0 (more than 256 threads asleep at once otherwise).
- `nvrm_evt_post_drain_stuck` 0. When a process closes, the kext waits for an RM event post that is still being
  delivered to it (microseconds, normally never) before it frees the connection. It waits twice: up to 200 ms when
  the connection closes (if the post is still in flight then, the debug output says
  `NVRM-xnu: client fd N: an event post is still in flight after 200 ms` and the close goes on) and up to 200 ms
  again when the connection's memory is about to be freed. Only a post that is still in flight after that keeps the
  connection's memory for good, with `NVRM-xnu: client fd N: an event post never finished - the connection is kept,
  not freed`. `nvrm_evt_post_drain_waits` counts both waits.

## The measurement behind the 1 ms threshold

The measurement below compared polling (`0`) with the eager sleep (`1`) on 2.9's code; the adaptive rule that is now
the default for applications was not part of it. On a 0.6 ms compute wait the eager sleep cost 0.127 ms of latency per
wait, which the default no longer pays on waits that short; on longer waits it saved CPU. It was made on one machine
and does not show what the new default does there.

## Measured system

- NVIDIA GeForce RTX 5060 Laptop GPU, 8 GiB, PCI ID `10de:2d19`, NVK GB206-A.
- Intel Core i7-13700HX, 16 GiB RAM, AC power.
- macOS 26.7.1 (`25G241`), SMBIOS `MacBookPro16,2`.
- Built-in display at 2560 x 1600, 180 Hz, NVIDIA-driven.
- Driver source baseline: v1.13.0 (`0960b2ef5dbc4a573056d4c8e042a5bd2e15d651`).
- Both arms used the same locally repaired plugin and Tahoe NVAccel. The plugin
  included the specialized AIR symbol / sampler linking fixes from PR #79, and
  NVAccel had been rebuilt for the Tahoe ABI. This is **not an unmodified release
  package result** and does not qualify macOS 26 support generally.

## Results

Each workload was run in five paired rounds, reversing the policy order on
alternate rounds. Every process did two warmups and 25 measured iterations:
125 measurements per workload and policy. The table reports the median of the
five per-run medians, not a pooled median. The raw timing samples and per-run
results are in [EVENT-WAIT-RTX5060.json](EVENT-WAIT-RTX5060.json).

| Workload / metric | Polling (`0`) | Event wait (`1`) | Change |
| --- | ---: | ---: | ---: |
| Integer compute / process CPU time | 0.612 ms | 0.286 ms | -53.3% |
| Integer compute / elapsed time | 0.607 ms | 0.734 ms | +20.9% (+0.127 ms) |
| Core Image blur / process CPU time | 6.559 ms | 4.127 ms | -37.1% |
| Core Image blur / elapsed time | 5.608 ms | 3.926 ms | -30.0% |

Elapsed time includes encoding, submission, execution, and synchronous waiting.
It is measured with `CLOCK_MONOTONIC`, not GPU timestamps. Process CPU time is
`getrusage(RUSAGE_SELF)` user plus system time across all process threads, so it
can exceed elapsed time. Compilation and the final compute correctness check
are outside the timed iterations. These are not whole-system CPU utilization
or GPU throughput measurements.

The compute workload transforms 1,048,576 integers with 64 dependent integer
iterations each; every result is compared with a CPU reference. The image test
renders a 1024 x 1024 RGBA8 procedural image through a radius-10 Core Image
Gaussian blur into a CPU bitmap, with intermediate caching disabled. Every
image run had identical output bytes by hash and opaque alpha; the test does
not establish independent image-reference accuracy.

Image timings varied considerably between runs. The five polling run medians
were 5.161, 5.608, 3.047, 6.145, and 8.171 ms; event-wait medians were 3.714, 4.558,
4.517, 3.690, and 3.926 ms. No confidence interval or universal speedup is claimed.
No GPU power, clock, temperature, fan-speed, or battery-life data was collected.
The test was short, on one system, with its normal desktop session running.

## Correctness and installation checks

- The compute reference checks passed for both policies.
- All paired image results had the same pixel hash (`10771374122383686649`).
- A cross-queue test committed a consumer's event wait before the producer's
  fill and signal. Both policies passed 32 rounds and verified all 128 MiB.
- Shared-to-private, private-to-shared, and private-to-private 32 MiB copies
  passed full byte comparisons. Their timings are not used to claim a speedup.
- No probe timeout or WindowServer restart occurred in the reported paired
  runs. The driver's VRAM refusal counter remained zero.
- After enabling the setting in the local configuration, new processes with
  no overrides logged `event wait ON` and passed the same functional checks.
  No driver binary was replaced by this configuration trial.

Existing icon and Tahoe ABI repairs are disclosed as the test baseline, not
submitted as new work here. Alternative compiler flags, memory-mapping changes,
and an experimental wait-threshold patch were not deployed or included.

## Reproduce without editing the system configuration

On an x86_64 Mac with the installed NVMTL driver as the default Metal device and
Apple Command Line Tools available, run from the repository root:

```sh
python3 tools/bench_event_wait.py /tmp/nvmtl-event-wait-results
```

Use a new output directory. The runner compiles the probe into a temporary
directory, alternates both policies for five rounds, verifies the event-wait
log, compares image hashes, and stops on a probe failure, timeout, or a changed
WindowServer PID. It then checks cross-queue synchronization and buffer copies
with both policies. `--rounds 1` is a smoke test, not the five-round measurement.
The tool writes local results and logs; it does not upload anything or edit the
driver configuration. Normal driver shader-cache activity can still occur.

Review local stderr logs before sharing them. The checked-in fixture retains
only timing samples, correctness results, policy, and round numbers; it excludes
local user paths, process IDs, and raw system logs.

For an individual probe, compile and run:

```sh
xcrun clang -O2 -fobjc-arc tools/bench_event_wait.m \
  -framework Foundation -framework Metal -framework CoreGraphics \
  -framework CoreImage -o /tmp/nvmtl-event-wait-probe
/tmp/nvmtl-event-wait-probe compute NVRM_EVENT_WAIT=0
/tmp/nvmtl-event-wait-probe compute NVRM_EVENT_WAIT=1
```

The submitted probe preserves the measured compute/image workloads and timing
boundaries, with unused experimental driver-loading paths removed and the
override restricted to `NVRM_EVENT_WAIT=0` or `1`. Fresh measurements will vary.
