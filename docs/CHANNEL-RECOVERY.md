# Channel recovery: a GPU fault costs the frames in flight, not the device

Status: the kext's part and the Metal plugin's part have been compiled and linked with Apple's own toolchain on one
Mac (an earlier state of this branch); NVK's part has only been cross-built for macOS on another system. Their pure
logic runs in host tests. **None of it has been loaded or run on a
Mac, and no rebuild of a real channel has been seen.** It is
on by default. `NVK_CHANNEL_RECOVER=0` in `/Library/GPUBundles/nvmtl/nvrm610.conf` switches it off, and NVK then runs
the 2.9 code.

## What changed

In 2.9 a GPU fault on one channel ended the application's Metal device: every later command buffer was refused, and
in WindowServer the session ended. The GPU itself was fine; only the faulting channel had been torn down.

A GPU fault on one channel no longer ends the application's Metal device. When a wait finds that its channel faulted
with an exception that ends only that channel (Xid 13, 31, 32, 39, 40, 41, 43, 44, 45, 69, 109), or that the channel
made no progress for `NVRM_FLUSH_TIMEOUT_MS` (when it is not set, the Metal plugin sets it to 5 s in WindowServer
and 60 s in every other process), NVK builds a new channel beside the old one, frees the
old one, and continues on the new one with the same semaphore and command ring: the channel-wide state (objects,
descriptor heaps, shader local memory) is sent again, the submissions that had not started are replayed, and the one
that was running is dropped. A dropped submission is reported complete, and the plugin can ask whether a fence's
submission ran (`nvmtl_nvk_fence_dropped_v1`). A fault inside replayed work drops everything that was in flight.
Before any of that, a wait that sees no progress for half the budget rings the channel's doorbell again.

Not rebuilt, and ending exactly as in 2.9 (`VK_ERROR_DEVICE_LOST`): a firmware crash or a reset request (Xid 119,
120, or RM asking for a recovery action), the GPU off the bus (Xid 79), uncorrectable memory errors (Xid 48, 94, 95,
140), a failed wake from sleep, any other Xid, a fourth rebuild of the same channel within 30 s, or RM refusing to
build the new channel or to free the old one. The usual lines are printed, followed by

    nvrm: channel slot <n> not rebuilt: loss kind <kind> (Xid <x>), <reason>

A rebuild prints one line on stderr and one in `nvmtl.log` (`nvk-reason:`). An installed (release) plugin writes
`nvmtl.log` only for a process started with `NVMTL_PROCESS_ENV=1`; what reaches the system log otherwise is in
[TROUBLESHOOTING.md](TROUBLESHOOTING.md), "The new paths of release 3.0". The line:

    nvrm: channel slot <n> rebuilt after a <kind> loss (Xid <x>): <d> flush(es) dropped, <r> replayed (<level>), <t> us

Nothing is printed on a healthy GPU.

## What the Metal plugin does

- When NVK rebuilds a channel it signals the fence of the work it did not run from the CPU. The plugin asks NVK
  (`nvmtl_nvk_fence_dropped_v1`) after every fence wait that succeeded. For a dropped submission the Metal command
  buffer completes with an error (`MTLCommandBufferErrorInternal`), the backend stays up and the next command buffer
  runs; nothing is quarantined, its command pool and pages are destroyed (not reused, not leaked), and no completion
  witness is waited for. A surface upload that was dropped returns its failure to the caller, which uploads again at
  the next use; an application's asynchronous surface or texture upload whose copy was dropped is copied again from
  its staging buffer when something next finishes that upload (a readback, a migration, the next upload of the same
  image), but only if the image has only been sampled since; if a render pass, a copy or a storage use came after it,
  the copy is not made again (it could overwrite what that work wrote) and the image keeps what it holds. An image
  that is being freed is not copied again. The surface-fence holds of the command buffer are closed on its error path
  as on every other.
- Log lines (`nvmtl.log`; in a release build also the system log, since the line holds the word "fault"):
  `vk: after a GPU fault, a channel rebuild dropped this command buffer (N so far in <process>, fence after W ms): it
  did not run and is reported as failed; the device stays up; NVK: Xid X, R channel rebuilds, D flushes dropped, P
  replayed` for the first and every 256th, and on the first one the `fault context:` lines of the work that was
  running. `vk: tex upload async: the copy a channel rebuild dropped was made again -> 0` after the async-upload copy,
  or `vk: tex upload async: the copy a channel rebuild dropped is not made again - the image was used since (layout
  N), the image keeps what it held`.
- A loss that is not rebuilt still ends the process's device exactly as before; the loss line in `nvmtl.log` is now
  followed by `vk: NVK channel recovery: last loss <KIND (...)>, Xid X, not rebuilt because: <reason>; ...` where KIND
  is CHANNEL, STALL, MEMORY (ECC), GSP (firmware), BUS, RESUME (failed wake) or UNKNOWN. With `NVK_CHANNEL_RECOVER=0`
  the line says recovery is off.
- `NVMTL_DROP_REPORT=0` in `nvrm610.conf`: a dropped command buffer is reported as completed instead. Use it only if
  the desktop freezes after a recovered fault in WindowServer (the failed-part path had not been seen there with a
  frame that never rendered).

## What the kext does: it counts

NVRM.kext takes no part in the rebuild. NVK tells it what it did through a private escape (0xE8, through the
connection NVK already uses for every RM call), and the kext keeps machine-wide counters, so that one command shows
what every process did since boot:

    sysctl debug.nvrm_xid_count debug.nvrm_xid_last debug.nvrm_chan_recovered debug.nvrm_chan_dropped \
           debug.nvrm_chan_gave_up debug.nvrm_chan_last_kind debug.nvrm_chan_last_why debug.nvrm_chan_last_xid \
           debug.nvrm_chan_kicks debug.nvrm_chan_injected

| sysctl (all read-only) | Meaning |
|---|---|
| `debug.nvrm_xid_count` | Xids the RM logged since the kext loaded (every Xid, whichever process or channel it hit) |
| `debug.nvrm_xid_last` | the last Xid the RM logged |
| `debug.nvrm_chan_kicks` | stalled channels whose doorbell NVK rang again |
| `debug.nvrm_chan_recovered` | channels NVK rebuilt |
| `debug.nvrm_chan_dropped` | submissions those rebuilds did not run (at most 64 counted per rebuild) |
| `debug.nvrm_chan_gave_up` | recoveries given up: that process lost its device, as in 2.9 |
| `debug.nvrm_chan_injected` | faults made by the test hook (`NVK_FAULT_INJECT`) |
| `debug.nvrm_chan_last_xid` | Xid of the last channel event a process reported |
| `debug.nvrm_chan_last_kind` | kind of loss of the last give-up: 1 channel, 2 stall, 3 memory (ECC), 4 GSP (firmware), 5 bus, 6 failed wake, 7 unknown; 0 not classified; -1 a number this kext does not know |
| `debug.nvrm_chan_last_why` | why it was given up: 1 off, 2 kind not rebuilt, 3 budget (more than 3 rebuilds in 30 s), 4 plan, 5 build of the new channel refused, 6 free of the old channel refused, 7 a copy channel faulted again, 8 busy, 9 the queue was being destroyed, 10 shape, 11 no channel state captured, 12 the stall's cause is another channel that is still moving, 13 given up before; -1 a number this kext does not know |

On a healthy machine all of them except the two Xid counters stay 0. The escape also answers the GPU's power state
(running, asleep, or lost after a failed wake): that is how NVK tells a failed wake, which cannot be recovered, from a
channel fault. The counters cannot be written, and nothing in the kernel acts on them: a process could make them lie,
which is why they are only a diagnostic.

A kext older than this branch does not know escape 0xE8: the RM refuses it, NVK treats the power state as unknown,
does not ask again in that process, and the rebuild works without it. A new kext with an older NVK is never asked.
`debug.nvrm_xid_count` counts with any NVK.

## Switches

A release Metal plugin removes inherited `NVK_` variables before NVK is loaded, so both switches below reach a release
build ONLY as lines of `/Library/GPUBundles/nvmtl/nvrm610.conf` (root-owned); the environment works only for a
development build or with `NVMTL_PROCESS_ENV=1`.

- `NVK_CHANNEL_RECOVER=0` turns all of it off: no doorbell ring, no rebuild, no extra lock, the 2.9 code runs. Any
  other value, or none, leaves it on. It is also off with `NVRM_ASYNC=0` (NVK's older synchronous submission).
- `NVK_FAULT_INJECT=<mode>:<n>[:<process name>]` is a test hook, once per process, on the n-th submission of the first
  graphics queue. Modes: `stall` (that submission is not announced to the GPU: the doorbell ring has to find it),
  `fake` (the channel is reported faulted while it is healthy: the whole rebuild runs against the real driver),
  `reset` (RM resets the channel: a real channel error), `mmu` (one extra command-buffer entry at an unmapped address:
  a real MMU fault). As a line of `nvrm610.conf` it acts in every Metal process that starts after it, once in each;
  with a third field it acts only in the process of that name (the file name of the program, for an app the file in
  its `Contents/MacOS` folder). It never acts in WindowServer or loginwindow unless that name is given as the third
  field, e.g. `NVK_FAULT_INJECT=fake:2000:WindowServer`. Remove the line afterwards.
- `NVMTL_DROP_REPORT=0`: see the plugin section above.

Statistics per process: `nvmtl_nvk_recover_stats_v1` (re-rings, rebuilds by level, flushes dropped and replayed,
give-ups, the last Xid, kind and reason, injections, whether the kext answers the channel note: 0 not asked, 1 yes,
2 no, the last and longest rebuild time, fences answered "dropped").

## Interplay with sleep and with the surface fence

NVK's no-progress budget (`NVRM_FLUSH_TIMEOUT_MS`) is measured in awake time (`CLOCK_UPTIME_RAW`), as the plugin's
waits are: a wait that is in progress when the machine sleeps continues after wake with the time it had left, so a
channel is not rung again or rebuilt merely because the machine slept. Application timeouts (`vkWaitForFences`) are
still measured on the ordinary clock. With `NVK_CHANNEL_RECOVER=0` the budget is on the ordinary clock, as in 2.9.

A stall without a fault is seen by the surface fence at its own bound (2 s; when a reader waited on the stalled hold,
a strike against that connection) and by NVK at half its budget (doorbell) and at the whole budget (rebuild). Three
such strikes, each after the penalty window of the one before (10 s, then 20 s) and less than 10 minutes apart, make
the kext distrust that connection for surface sync (its process falls back to the 2.9 marks) even though NVK
recovered each stall ([SURFACE-FENCE.md](SURFACE-FENCE.md), "Nobody waits forever").
`debug.nvrm_fence_owners_distrusted` > 0 after injected stalls is therefore expected. A rebuilt channel keeps its
semaphore page, so the kernel keeps reading completions from the same page; NVK re-points the kernel at the
new error notifier (`debug.nvrm_fence_chan_rebinds` counts it), or, if the kernel refuses, deletes the registration
and the process uses the 2.9 marks. The value of a dropped submission is written into that semaphore by NVK, so a hold
armed on it ends.

The driver's own robust-channel recovery (the RC timer) and NVK's rebuild can both act on the same faulted channel.
NVK frees the old channel itself; if that free fails (for example because RM tore the channel down first), the
rebuild gives up (`debug.nvrm_chan_last_why` 6) and the application loses its device as in 2.9.

## What to look at on the machine

1. After boot and after an hour of desktop, a game and a video: `debug.nvrm_chan_*` all 0. This is the check that the
   new code changes nothing on a healthy GPU.
2. `NVK_FAULT_INJECT=stall:300:<the app's process name>` for a throwaway Metal app (in `nvrm610.conf`, and restart
   the app; without the third field every Metal process started afterwards gets the stall too): a hitch of half the
   budget (30 s in an application), then normal running; `debug.nvrm_chan_kicks` 1, `debug.nvrm_chan_injected` 1,
   nothing dropped.
3. `NVK_FAULT_INJECT=fake:300:<the app's process name>`: one wrong or missing frame and the app goes on;
   `debug.nvrm_chan_recovered` 1, `debug.nvrm_chan_dropped` 1, `debug.nvrm_chan_gave_up` 0. If `gave_up` is 1,
   `debug.nvrm_chan_last_why` says which RM call refused (5 build, 6 free).
4. `NVK_FAULT_INJECT=reset:300:<the app's process name>`, then `mmu:300:<the app's process name>`:
   `debug.nvrm_xid_count` rises (43, then 31 or 32), `debug.nvrm_chan_last_xid` matches, and the outcome is that of
   step 3. Another app and the desktop must not notice.
5. Only after that, WindowServer: `NVK_FAULT_INJECT=fake:2000:WindowServer`, log out and in; one glitch and the session
   stays up. If the desktop freezes afterwards, repeat with `NVMTL_DROP_REPORT=0` and report which behaves. Remove both
   lines afterwards.
