# Ray tracing working-set gate: measurement procedure

Some games decide whether to offer ray tracing from the working-set figure the Metal device reports
(`recommendedMaxWorkingSetSize`). The plugin keeps a knob for this, `NVMTL_WS_RT_GATE_MB`, default off (unset or 0):
while ray tracing is available, the answer is raised to at least that many MB (`nvmtl_rt_gate_working_set` in
`plugin/NVMTLDevice.m`). The one game profile that sets it, Cyberpunk 2077's (`plugin/nvmtl_app_profile.h`), sets 6200:
the game offers ray tracing only above 6145 MB, and an 8 GB card answers less once the desktop's share is taken. That
figure was found by the black-box procedure below, and this page is how to re-measure it on any card without reading
the game.

## What is measured

The smallest reported working set, in MB, at which the game's graphics menu offers its ray tracing options, with the
rest of the system unchanged (same driver, same card, same game build).

## Procedure (one person, one card, about 20 minutes)

A release plugin keeps a process's own driver variables, and writes its log (`/tmp/nvmtl.log`, or `$TMPDIR/nvmtl.log`
when that file cannot be opened), only for a process started with `NVMTL_PROCESS_ENV=1`. A value set that way wins over
the profile's 6200.

1. Install the driver build under test, start the game once with `NVMTL_PROCESS_ENV=1`, and confirm ray tracing is
   reported available: `grep -i "raytracing" /tmp/nvmtl.log` shows `QUERY supportsRaytracing -> YES`.
2. Start the game from a shell with `NVMTL_PROCESS_ENV=1 NVMTL_WS_RT_GATE_MB=<n>`. Use a starting value well below the
   card's own figure so the knob does nothing (the knob only raises the answer, it never lowers it), then raise it.
   When it acts, the log has `working set: answered <n> MB instead of <m> MB while ray tracing is available`.
3. Open the graphics menu and note whether the ray tracing options are present and selectable.
4. Bisect `<n>` between a value that hides the options and a value that shows them until the interval is 16 MB wide.
   Restart the game for every value: the game reads the figure once at device set-up.
5. Record the pair (last value hidden, first value shown), the card, and the driver build.
6. Check the same value against the low-memory behaviour: with `<n>` just below the threshold, texture quality
   options that need more memory are expected to be limited; with `<n>` above, they are not.
7. A card with less than 1 GB of memory above `<n>` keeps its own answer (the plugin refuses to over-report; for the
   profile's 6200 that is a card under 7224 MB). On such a card the procedure ends at step 2 with
   `working set: the <n> MB ray tracing floor is not applied on this <m> MB card` in the log: that is the expected
   result, not a failure.

## Pass criteria

- The threshold found in step 4 is within 16 MB of the profile default. If it differs, change the default and
  record the measurement here.
- With the knob unset, behaviour is identical to a build without the knob.

## Measurements

The profile's 6200 MB dates from the original project's 2.8. No measurement by this procedure is recorded in this
repository, on this fork's RTX 5050 or elsewhere.

| date | card | driver build | hidden at | shown at |
|---|---|---|---|---|
| none recorded | | | | |
