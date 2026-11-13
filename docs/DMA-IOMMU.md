# GPU memory access with VT-d (the DMA mapper)

This page is for the owner of a machine: what the driver does when Intel VT-d (on macOS: AppleVTD, "the DMA
mapper") sits between the GPU and memory, how to see which case a machine is in, and what has and has not been
tried on real hardware.

## Why it matters

The GPU reads and writes system memory itself: the firmware it boots from, its command buffers, the pages an app
shares with it, the copies the accelerator makes of surfaces. Every such address goes out on the PCI bus.

- **Without a mapper** the address on the bus is the physical address of the page. Up to 2.9 the driver always gave
  the GPU physical addresses, and that is correct here. This is the case on every machine the driver works on today:
  VT-d off in the firmware, the DMAR table dropped (the installer does this on Core Ultra machines), or the mapper
  disabled.
- **With a mapper** every address on the bus is translated by AppleVTD. A physical address then means nothing, or
  worse, means some other device's buffer. 2.9 did exactly that. Nobody has tried 2.9 on such a machine; what to
  expect, from how VT-d works, is GPU firmware that does not boot or, worse, GPU writes landing in memory that belongs
  to something else.

The driver now asks IOKit, once per GPU at start, which case it is in, and acts on the answer.

## The modes

| Mode | When | What the driver does |
|---|---|---|
| 0, none | No mapper translates the GPU (no system mapper, or IOKit marked the GPU untranslated) | Exactly what 2.9 did: physical addresses everywhere. |
| 1, system mapper | AppleVTD's shared space translates the GPU | Every block of system memory the GPU uses is mapped through the mapper, as the Linux driver does with an IOMMU. Each mapping is read back from the mapper's own tables before the GPU is given its address: its first and last page always, and every page (up to 4096) of the first 64 mappings and of every 64th after them. |
| 2, device mapper | IOKit gave the GPU a translated space of its own | As mode 1, on that space. The driver never asks for one; it uses what IOKit chose. |
| 3, refused | A mapper translates the GPU and the driver may not or cannot use it (boot-arg `nvrmdma=0`, a failed self-test, or a build that does not route the GPU's memory requests through the mapper) | The GPU is not started. This is a clean "not started", not a failed boot: the boot guard does not count it. |

In modes 1 and 2:
- before the GPU is started, two pages are mapped and read back from the mapper, and at least one of their bus
  addresses must differ from the page's physical address, and the two bus addresses from each other (the self-test:
  the mapper answers an address it has no entry for with that same address, so a read-back alone would also pass an
  untranslated mapping); if that fails the mode becomes 3;
- a mapping the mapper splits where the GPU needs one run, an address above what the GPU can reach, or a page whose
  bus address does not lead back to it, is refused (the allocation fails; nothing wrong reaches the GPU);
- the driver keeps at most 64 GB (system mapper) or 6 GB (device mapper) mapped at once, because AppleVTD waits
  without a time limit when its space is full;
- the accelerator's surface copies (page-off) go through a staging buffer that is itself mapped through the mapper;
  a list of raw physical pages is refused before the copy engine sees it;
- when a GPU stops, anything still mapped for it is unmapped (and counted).

Sleep and wake: the driver does nothing special. According to AppleVTD's published source it keeps its tables in
memory and turns translation back on before drivers are woken, so a mapping made before sleep is still valid after
it. This has not been tried (see the last section).

## How to see which mode a machine is in

    sysctl debug.nvrm_dma_mode
    ioreg -l -w0 | grep nvrm-dma
    log show --last boot --predicate 'eventMessage CONTAINS "NVRM-xnu: DMA"'

The registry property `nvrm-dma` on the GPU's PCI device holds one line, for example
`none (bus address = physical address)`, `system mapper`, or `refused: ...` with the reason. The log has one line per
GPU, `NVRM-xnu: DMA: <the property's text> (system mapper present|absent, device mapper present|absent)` (with
`, nub marked untranslated` before the closing parenthesis when IOKit marked the GPU so), and in modes 1 and 2 the
self-test line `NVRM-xnu: DMA self-test: 2 pages mapped at bus 0x..., 0x... and read back from the mapper`, or, when
it fails, `NVRM-xnu: DMA self-test failed: the mapping gave the pages' physical addresses (...)` or
`NVRM-xnu: DMA self-test failed (status 0x...)`. A refused GPU adds
`NVRM-xnu: NOT STARTING THE GPU: refused: .... Not counted as a failed start.`

On a machine without VT-d (mode 0) the three commands should print `debug.nvrm_dma_mode: 0`, the property
`"nvrm-dma" = "none (bus address = physical address)"`, and, after the header line and the line of the `log` command
itself that `log show` always prints, `NVRM-xnu: DMA: none (bus address = physical address) (system mapper absent,
device mapper absent)`. `log show --last boot` takes about a minute.

`debug.nvrm_dma_mode` is the highest mode of the GPUs that are attached, and stays 3 once a GPU was refused (a
refused GPU is not attached for long, and the refusal is what must show).

## The counters (`sysctl debug | grep nvrm_dma`)

All read-only. In mode 0 every one of them except `nvrm_dma_mode` stays 0 (`nvrm_dma_refused` too, unless the RM
asks for a peer or device-register mapping, which is refused in every mode).

| Name | Meaning | Healthy |
|---|---|---|
| `nvrm_dma_mode` | 0 none, 1 system mapper, 2 device mapper, 3 refused | 0 without VT-d |
| `nvrm_dma_maps`, `nvrm_dma_unmaps`, `nvrm_dma_live` | mappings made, released, live now | mode 1: thousands after start, `live` > 0 |
| `nvrm_dma_live_bytes`, `nvrm_dma_peak_bytes` | bytes mapped now, most at once | |
| `nvrm_dma_verified` | pages read back from the mapper | grows with `maps` |
| `nvrm_dma_verify_bad` | pages whose bus address did not lead back to them | always 0 |
| `nvrm_dma_unaddressable`, `nvrm_dma_not_contig`, `nvrm_dma_fail`, `nvrm_dma_bad_priv` | mappings refused, by cause | 0 |
| `nvrm_dma_budget_refused` | mappings refused to keep the mapper's space from filling | 0 |
| `nvrm_dma_refused` | peer-to-peer and device-register mappings (none exist on macOS; always refused) | 0 with one GPU |
| `nvrm_dma_user_maps` | an app's own pages mapped for the GPU (Vulkan host-pointer import) | grows with such apps |
| `nvrm_dma_surf_maps` | staging buffers of the accelerator mapped | small |
| `nvrm_dma_pageoff_refused` | surface copies refused because their pages were not mapped | 0 |
| `nvrm_dma_max_us` | slowest single mapping, microseconds | well under a frame |
| `nvrm_dma_lo`, `nvrm_dma_hi` | lowest and highest bus address handed out | mode 1: below 137438953472 (128 GB) |
| `nvrm_dma_leaked` | mappings still live when their GPU stopped | 0 |

`nvrm_dma_live` must read the same before and after a sleep with nothing else running.

## The accelerator's copies (`sysctl debug | grep nvaccel_dma`)

Each accelerator asks NVRM, at its first page-off, hybrid frame or two-plane grant check, how the GPU's addresses
reach the bus, and keeps the answer until NVRM refuses one of its page lists (then it asks again at the next one)
(`debug.nvaccel_dma_mode`: -1 until then, then the same numbers as above). Without a mapper it copies as 2.9 did and
every other `nvaccel_dma_*` counter stays 0. With a mapper its copies of GPU memory to the CPU go through a staging buffer that NVRM mapped for the
GPU, instead of raw physical addresses that the mapper would send elsewhere.

| Name | Meaning | Healthy |
|---|---|---|
| `nvaccel_dma_mode` | -1 before the first page-off; then 0 no mapper, 1 system mapper, 2 device mapper, 3 a mapper the driver may not use | 0 without VT-d |
| `nvaccel_dma_staged` | copies through the mapped staging buffer | grows with screenshots, with VT-d on |
| `nvaccel_dma_maps`, `nvaccel_dma_map_fail`, `nvaccel_dma_unmaps` | staging buffers mapped, mappings that failed, mappings given back | `maps` small and stable; `map_fail` 0 |
| `nvaccel_dma_abandoned` | buffers left mapped after a copy-engine timeout | 0 |
| `nvaccel_dma_refused` | page-offs NVRM refused as unmapped | 0 |
| `nvaccel_dma_family`, `nvaccel_dma_hybrid` | page-offs and hybrid frames left to the older path because the GPU is translated | 0 or small |

When an accelerator stops, the staging buffer mapped through its GPU's NVRM is unmapped and freed at once (after any
copy still using it; `nvaccel_dma_unmaps` grows by one), so `debug.nvrm_dma_leaked` does not count it when that GPU
stops later.

## The boot-arg `nvrmdma=0`

It is read when each GPU starts (the boot arguments cannot change while the machine runs, so every GPU gets the same
answer).

- **Without a mapper** it changes nothing: mode 0, the 2.9 behaviour.
- **With a mapper** it selects mode 3: the GPU is not started. It can never select "physical addresses on a
  translated bus", because that is what broke 2.9 on such machines. Its use is diagnostic: if a boot with VT-d on
  fails in modes 1 or 2, `nvrmdma=0` gives a clean "GPU not started" with VT-d still on, which separates the mapping
  code from everything else VT-d changes.

The way back to a working desktop on a VT-d machine where modes 1 or 2 fail is to turn VT-d off (or keep the
installer's dropped DMAR table), which gives mode 0.

## What was never run on hardware

Everything in modes 1, 2 and 3, and mode 0 too: nothing of this branch has been loaded on a Mac yet. The DMA layer as
it stands at commit 3933ff6, the self-test's check for an untranslated address included, was compiled and linked for
macOS 15 and later on one Intel Mac (12 threads, macOS 26.7, Xcode 26.5) and on GitHub's Intel runner
(macos-15-intel: 4 processors, macOS 15, Xcode 26.3 with the 26.2 SDK). Its kernel imports were checked against the
export lists in the published source of the macOS 15 and 26 kernels, and on that Mac `kmutil libraries` found every
symbol NVRM imported at an earlier state of the branch; the decisions were tested on a host. Nothing of it has run
with a real mapper:

- the NVIDIA RM's own bus-address path (it is compiled into the driver and is the path Linux always uses, but it was
  never executed on macOS);
- AppleVTD's behaviour on macOS 15 and 26 (one segment per mapping, the read-back call), as read from IOPCIFamily's
  published source;
- surface copies through a mapped staging buffer, and the cost of mapping under load;
- sleep and wake with live mappings.

Mode 0 is the one that runs on every machine the driver supports today. There, the only new work is at GPU start:
two questions to IOKit about the mapper (is there a system mapper, does the GPU have one of its own), one property
read, one lock, the read-only counters, one log line and the `nvrm-dma` property. Allocation
options, addresses and the surface copies are those of 2.9.

## Checking a VT-d machine (optional, with the rollback at hand)

1. Enable VT-d in the firmware, keep the DMAR table, boot. Expect mode 1.
2. After a few minutes of normal use (a browser with video, a Metal app, a Vulkan app):
   `nvrm_dma_maps` in the thousands, `nvrm_dma_verified` > 0, every "refused" and "bad" counter 0,
   `nvrm_dma_hi` below 128 GB.
3. Sleep and wake once: `nvrm_dma_live` unchanged, the desktop comes back.
4. Look for AppleVTD fault lines naming the GPU (`log show --last boot | grep -i "vtd"`). Any such line is a path that
   still sends an untranslated address; the whole line belongs in a report.
