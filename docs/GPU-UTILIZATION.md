# GPU utilization

NVAccel publishes `Device Utilization %`, `GPU Core Utilization %`,
`GPU Memory Utilization %`, `GPU Video Engine Utilization %` (encode), and
`GPU Video Decode Utilization %` in `PerformanceStatistics`. The device/core
value is RM's GR engine utilization. Memory is bandwidth utilization, not VRAM
allocation. Values are whole percentages rounded from RM's percent-times-100
units. No helper daemon, writable sysctl, or additional sensor kext is needed.

The accelerator asks the NVRM service on its own PCI device for a sample once
per second, on a separate work loop. NVRM uses a private kernel RM client and
the read-only `PERF_GET_GPUMON_PERFMON_UTIL_SAMPLES_V2` command. Its roughly
56 KB parameter buffer lives on the heap. Queries skip a closed system power
gate; teardown drains queries before releasing the RM client.

The latest nonzero timestamp wins within the bounded 72-entry ring. On the
tested RTX 3080, firmware leaves `count` at zero and returns a byte-offset
`tracker`; a tracker and its sample payload can also disagree by one entry.
Reading only `samples[0]` can return a sample about 14 seconds old. The parser
accepts entry and aligned byte offsets, validates sizes and utilization ranges,
and never substitutes an older sample for an invalid newest sample.

The existing 250 ms statistics publisher copies the cached reading without
calling RM on the graphics work loop. Failed queries and samples unchanged for
more than five seconds remove utilization keys instead of reporting a false
0%. Cache age includes time spent asleep. Both NVRM and NVAccel must be built
from this change; an older NVRM does not implement the query interface.

A read holds the RM's locks until the firmware answers, and the RM waits about
6 s for an answer before it gives up. Issued again every second, a read the
firmware does not answer keeps other processes, the window server and shutdown
waiting on those locks. So a read that takes longer than 0.5 s pauses the
sampler: NVRM makes no read for the next 20 seconds, and the utilization keys
are removed meanwhile, as for a failed query. Three slow reads in a row stop it
until the next boot. The time includes the wait for the locks, so a mode set or
a channel recovery that holds them can also make one read slow; a fast read,
answered or failed, starts the count again. Each slow read is logged with its
time (`utilization: a read took N ms`). The rule is in
`kexts/NVRM/nvrm_utilization_pace.h`, and `tools/test_utilization_pace.py`
compiles and checks it on a host.

## Parser test

```sh
clang++ -std=c++17 -Wall -Wextra -Werror tools/test_nvrm_utilization.cpp -o /tmp/nvrm-utilization-test
/tmp/nvrm-utilization-test
```

Define `NVRM_TEST_NVIDIA_HEADERS` and add NVIDIA's `src/common/sdk/nvidia/inc`
and `src/common/inc` include paths to exercise the same parser with the public
RM structures. Optional positional arguments replay raw V2 parameter buffers
captured by a read-only probe.

Build NVAccel with `build/accel_build.sh`, once with `NM_TAHOE=1` for macOS 26
and once without it for macOS 15. Rebuild NVRM with its matching Darwin RM and
NVKMS archives. Before deploying, boot the paired kexts on real hardware and
check idle/load/recovery, sleep/wake, missing telemetry, and GPU teardown.
