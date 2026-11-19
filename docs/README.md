# Documentation index

Start with [current status](STATUS.md) and the [main README](../README.md).
The documents below cover the current source tree and its historical design
records. A historical test applies to its named revision, not all later code.

## Installation, builds and validation

- [Build guide](BUILDING.md) / [Guía de compilación](BUILDING.es.md)
- [Hardware test checklist](TESTING-ON-HARDWARE.md) / [Pruebas en hardware](TESTING-ON-HARDWARE.es.md)
- [Supported cards](CARD-SUPPORT.md), [troubleshooting](TROUBLESHOOTING.md), [switches](SWITCHES.md) (generated from source)
- [Architecture](HOW-IT-WORKS.md), [changelog](../CHANGELOG.md), [provenance](PROVENANCE.md), [security](../SECURITY.md), [contributing](../CONTRIBUTING.md)

## Telemetry, power and GPU selection

- [NVMetrics integration](NVMETRICS.md), [GPU Caps](GPU-CAPS.md), [Phase 1 evidence](GPU-CAPS-PHASE1-VALIDACION.md), [Phase 2 discovery](GPU-CAPS-PHASE2.md)
- [GPU utilisation](GPU-UTILIZATION.md), [sleep/wake](SLEEP-WAKE.md)
- [Automatic primary GPU](AUTO-PRIMARY-GPU.md), [multi-GPU selection](MULTI-GPU.md), [multi-GPU design](MULTI-GPU-DESIGN.md)
- [Hybrid laptops](HYBRID-LAPTOPS.md), [hybrid validation plan](HYBRID-TESTER-PLAN.md)

## Rendering, memory and synchronization

- [NVK base and headers](NVK-BASE-AND-HEADERS.md), [translator validation](TRANSLATOR-VALIDATION.md), [translator architecture](../translator/translator/docs/ARCHITECTURE.md)
- [Surface fences](SURFACE-FENCE.md), [event waits](EVENT-WAIT.md), [channel recovery](CHANNEL-RECOVERY.md), [interrupts and RM timers](INTERRUPT-PATH-AND-RC-TIMER.md)
- [DMA/IOMMU](DMA-IOMMU.md), [two-plane IOSurfaces and deferred uploads](STUDIO-475-476.md), [historical host-copy coherence review](host-copy-coherence-review.md)
- [Hardware ray queries](HWRT.md), [curve geometry](CURVES.md), [ray-tracing gate](RT-WORKING-SET-GATE.md), [DLSS](../dlss/README.md)
- [Zink OpenGL](ZINK-OPENGL.md), [Vulkan application runtime](../tools/nvk-bridge/README.md), [runtime installation](../tools/nvk-bridge/INSTALL.md), [application launch examples](../tools/nvk-bridge/APPLICATIONS.md)

## Display, audio and browser/media

- [Backlight](NVRMFB-BACKLIGHT-POWER.md), [hot-plug lifecycle](NVRMFB-HOTPLUG-LIFECYCLE.md)
- [Display audio](DISPLAY-AUDIO-MAPPING.md), [volume](DISPLAY-AUDIO-VOLUME.md), [framebuffer audio mapping](NVRMFB-DISPLAY-AUDIO-MAPPING.md)
- [Safari WebGL](SAFARI-WEBGL.md), [Safari WebGPU](SAFARI-WEBGPU-TEXTURES.md), [Photo Booth](PHOTOBOOTH-PREVIEW.md)
- [Windows parity](WINDOWS-PARITY.md), [media/power parity](WINDOWS-PARITY-MEDIA.md), [Mac parity](MAC-PARITY.md)

Component README files under `kexts/`, `nvk/`, `tools/` and `translator/`
document local implementation and test details. Preserve their attribution and
historical measurements; consult the status ledger before treating any as a
current hardware guarantee. License texts and test fixtures are not user guides.
