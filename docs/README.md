# Documentation index

Start with the [main README](../README.md) (install, OpenCore settings and
[what is on by default and its ways back](../README.md#what-is-on-by-default-and-the-ways-back)), then the
[release 2 notes](releases/RELEASE-2.md) and the [status ledger](STATUS.md). The pages below describe the current
source tree (3.0.20). The design records keep their measurements with the version they were taken on; a historical
result applies to the revision it names, not to all later code.

## Releases and status

- [Release 2 notes](releases/RELEASE-2.md): what this fork added after release 1 (3.0.2), up to 3.0.20
- [Changelog](../CHANGELOG.md): every version in full, with its ways back and what ran where
- [Status and evidence](STATUS.md), version by version
- [Hardware test checklist](TESTING-ON-HARDWARE.md) / [Pruebas en hardware](TESTING-ON-HARDWARE.es.md)

## Installing, building and developing

- [Build guide](BUILDING.md) / [Guía de compilación](BUILDING.es.md), including the kits for macOS 10.7 to 14 and
  signing the graphics stack
- [Troubleshooting](TROUBLESHOOTING.md), [every switch](SWITCHES.md) (generated from the source),
  [supported cards](CARD-SUPPORT.md)
- AMFI left on: [signed graphics with AMFIAnchor](SIGNED-GRAPHICS-EXPERIMENT.md),
  [compatibility and recovery notes](AMFI-COMPATIBILITY.md) (experimental)
- [Architecture](HOW-IT-WORKS.md), [provenance and licences](PROVENANCE.md), [developing](../DEVELOPING.md),
  [contributing](../CONTRIBUTING.md), [security](../SECURITY.md)

## Telemetry, power and GPU selection

- [NVMetrics integration](NVMETRICS.md), [GPU Caps](GPU-CAPS.md), [phase 1 evidence](GPU-CAPS-PHASE1-VALIDACION.md),
  [phase 2 discovery](GPU-CAPS-PHASE2.md)
- [GPU utilisation](GPU-UTILIZATION.md), [sleep and wake](SLEEP-WAKE.md)
- [Automatic primary GPU](AUTO-PRIMARY-GPU.md), [multi-GPU selection](MULTI-GPU.md),
  [multi-GPU design](MULTI-GPU-DESIGN.md)
- [Hybrid laptops](HYBRID-LAPTOPS.md), [hybrid validation plan](HYBRID-TESTER-PLAN.md)

## Rendering, memory and synchronization

- [NVK base and headers](NVK-BASE-AND-HEADERS.md) (Vulkan 1.4.363 until Mesa 27),
  [translator validation](TRANSLATOR-VALIDATION.md),
  [translator architecture](../translator/translator/docs/ARCHITECTURE.md)
- [Zink OpenGL](ZINK-OPENGL.md), [Vulkan application runtime](../tools/nvk-bridge/README.md),
  [runtime installation](../tools/nvk-bridge/INSTALL.md), [application launch examples](../tools/nvk-bridge/APPLICATIONS.md),
  [NAK shader corpus](../tools/nak-corpus/README.md)
- [Surface fences](SURFACE-FENCE.md), [event waits](EVENT-WAIT.md), [channel recovery](CHANNEL-RECOVERY.md),
  [interrupts and RM timers](INTERRUPT-PATH-AND-RC-TIMER.md)
- [DMA/IOMMU](DMA-IOMMU.md), [two-plane IOSurfaces and deferred uploads](STUDIO-475-476.md),
  [historical host-copy coherence review](host-copy-coherence-review.md)
- [Hardware ray queries](HWRT.md), [curve geometry](CURVES.md), [ray-tracing gate](RT-WORKING-SET-GATE.md),
  [DLSS](../dlss/README.md)
- [OpenGL for older macOS](../gl/README.md) (the kits of 10.7 to 10.13)

## Display, audio, browsers and media

- [Backlight](NVRMFB-BACKLIGHT-POWER.md), [hot-plug lifecycle](NVRMFB-HOTPLUG-LIFECYCLE.md)
- [Display audio](DISPLAY-AUDIO-MAPPING.md), [volume](DISPLAY-AUDIO-VOLUME.md),
  [framebuffer audio mapping](NVRMFB-DISPLAY-AUDIO-MAPPING.md)
- [Safari WebGL](SAFARI-WEBGL.md), [Safari WebGPU](SAFARI-WEBGPU-TEXTURES.md), [Photo Booth](PHOTOBOOTH-PREVIEW.md)
- [Windows parity](WINDOWS-PARITY.md), [media and power parity](WINDOWS-PARITY-MEDIA.md), [Mac parity](MAC-PARITY.md)

The README files under `kexts/`, `nvk/`, `tools/` and `translator/` document local implementation and test details.
Keep their attribution and historical measurements, and check the status ledger before treating any of them as a
current hardware guarantee. Licence texts and test fixtures are not user guides.
