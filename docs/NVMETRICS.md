# NVMetrics integration and GPU telemetry

NVMetrics is a separate macOS application and privileged runtime:
[TheFormidable/NVMetrics](https://github.com/TheFormidable/NVMetrics).
The driver does not install this application or its RM guard. This page describes
the interface and the observations available as of 2026-11-19, not a release
announcement for either repository.

## Driver-facing interface

| Interface | Purpose | Boundary |
|---|---|---|
| NVRM selector 7 | Advertise supported user-client capabilities | Check before using selector 14 |
| NVRM selector 13 | Root-controlled live boost policy | Not a fan, clock offset or voltage control |
| NVRM selector 14 | GPU Caps ABI 1.0, cached per-GPU read-only snapshot | Unknown is not supported; manual authorization is false in the recorded test |
| RUSD (RM class 0xde) | Shared sensor observations, also consumed by `tools/nvmtl-smi` | Validate read result, timestamp and freshness; no automatic write authority |

See [GPU-CAPS.md](GPU-CAPS.md) for kernel cache lifecycle and native evidence and
[GPU-UTILIZATION.md](GPU-UTILIZATION.md) for NVAccel's IORegistry counters.

## Experimental fan control: physical findings

On the tested RTX 5050 with two observed RPM channels, a supervised NVMetrics
0.15.6 audit reported a successful 50% Manual session and verified return to
AUTO (50 telemetry samples, 275.8 seconds total, 2026-11-18).
This demonstrates only the tested RM record layout and guarded session, not
autonomous safe cooling on every card. Neither the number of RPM slots nor a
successful RM GET establishes cooler topology, calibrated limits or write
authorization for another GPU.

The same hardware later reproduced an unexpected Manual termination under
0.15.7 (Flight Recorder run b641d8b5910c8461, sequence 5651, 30%):

- GPU thermal timestamp, live before and after: UINT64_MAX (WRITE_IN_PROGRESS).
- First read failed; retries = 0, retry_us = 13388.
- Guard returned to AUTO and verified RM readback; the next six observed
  GPU samples were valid.

The trace is consistent with the 250 μs nanosleep exceeding the 2 ms retry
budget under background scheduling. It does not prove a permanently stuck
GSP producer or a kernel bug: no actual retry occurred.
The 0.15.8 candidate replaces that sleep with bounded immediate/spin retries,
records scheduler interruptions, and improves attribution. Host tests were
reported passing; native confirmation of the fix is still pending in the
available evidence. No driver patch was made for this incident.

## Diagnostics and limits

Use the companion application's own matching-version client and diagnostics.
For an installed compatible runtime, these queries are read-only:

```bash
CLIENT="/Applications/NVMetrics.app/Contents/Resources/GuardTools/Runtime/nvmetrics-runtime"
"$CLIENT" client inspect
"$CLIENT" client control_status
nvmetrics-agent caps
```

For a source checkout of the matching Flight Recorder revision, after the
session has ended and AUTO is verified, the report tool is:

```bash
sudo python3 rm-controller/diagnostics/rusd_flight_report.py \
  /Library/PrivilegedHelperTools/org.nvmetrics.rmguard/rusd-flight.jsonl
```

An empty log does not demonstrate a clean session, and sampled windows do not
prove an unobserved event did not occur. Preserve the session summary and linked
controller events. Do not disable thermal limits, heartbeat, independent rescue
or watchdog to work around a transient sensor read. Manual user-space control
is distinct from the driver capability ABI and is not a general NVIDIA
overclocking or fan-curve implementation.
