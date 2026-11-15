# Native Vulkan presentation runtime

A Vulkan provider for applications that draw into a macOS window (a `CAMetalLayer`) with Vulkan: Minecraft 26.3, MPV
and other applications that use `VK_EXT_metal_surface`. Rendering goes to the driver's NVK. Each presented image is
copied on the GPU into the window's drawable, so no frame passes through MoltenVK, Metal shader translation or the CPU.
It comes from the HighDelay/nvidia-macos-driver-nvk fork (`nvk/native-macos` there).

The package installs it at `/Library/GPUBundles/nvmtl/minecraft`. The name of that folder comes from the source fork,
where Minecraft was the first application. Nothing selects it for an application by itself: each application chooses
it in its own way, see [Minecraft](INSTALL.md) and [MPV and other applications](APPLICATIONS.md). Applications that do
not choose it keep the provider they use today.

## What is installed

| File | What |
|---|---|
| `lib/libnvk_minecraft.dylib` | the frontend (`bridge.mm`), the library an application loads |
| `lib/libnvk_renderer.dylib` | a copy of the package's `libvulkan_nouveau.dylib` with its own install name |
| `nvk_bridge_icd.json` | a Vulkan loader manifest that names the frontend |
| `nvk-bridge-runtime.properties` | the runtime's identity (the frontend's SHA-256), read by the optional companion mod |

The package also carries [INSTALL.md](INSTALL.md) as `MINECRAFT-NVK.md` and [APPLICATIONS.md](APPLICATIONS.md) as
`NVK-APPLICATIONS.md` at its top, beside `install.sh`; they are not installed.

The renderer is a separate copy so that it is a separate image in the process: an application's Vulkan objects never
mix with the NVK instance the Metal plugin runs in the same process. It reads the same settings file as the driver's
NVK (`/Library/GPUBundles/nvmtl/nvrm610.conf`) and keys its shader cache on the same build identity (`LC_UUID`).
The copy adds about 19.5 MB to the installed driver.

## How it is built

`./build.sh` builds it as the `nvkbridge` component after `nvk`, with `build.sh` in this folder:
`build.sh <NVK library> <Vulkan include dir> <out dir>`. It copies the NVK library to `lib/libnvk_renderer.dylib`, gives
it the install name `@loader_path/libnvk_renderer.dylib`, compiles `bridge.mm` for x86_64 and macOS 15.0 with `-Wall
-Wextra -Werror`, signs both libraries ad hoc, and writes the manifest with the Vulkan version of NVK's headers
(1.4.363 for Mesa 26.3). The package step copies the folder to `Library/GPUBundles/nvmtl/minecraft`. A new NVK build
rebuilds the runtime, because the component depends on `nvk`.

## How it works

- The frontend owns `VK_KHR_surface`, `VK_EXT_metal_surface`, `VK_KHR_get_surface_capabilities2` and
  `VK_KHR_swapchain`. Everything else is NVK's: rendering functions, device features and limits.
- The driver's NVK has its own window-system layer, which copies each frame into the drawable on the CPU. The frontend
  replaces it. The renderer's other surface and swapchain extensions (present id and wait, swapchain maintenance,
  present timing, HDR metadata and the others listed in `unsupported_device_extension`) are not offered, an application
  that asks for one gets `VK_ERROR_EXTENSION_NOT_PRESENT`, and their entry points are absent.
- Swapchain images are ordinary NVK images in VRAM. Two to eight images are supported, independent of the two or three
  drawables of the `CAMetalLayer`.
- On present, the frontend waits for the application's semaphores, imports the drawable's IOSurface as Vulkan memory,
  copies the image into it on a private queue, and waits for the copy to complete. It then marks the VRAM newest
  (`nvmtl_nvk_surface_dirty`), writes the plugin's generation tag (`NVMTLVramAt:p0`) and presents the drawable.
  Presentation is synchronous.
- When the GPU's BAR is smaller than its VRAM and the driver refuses the VRAM import, the copy goes to the drawable's
  host pages instead (`VK_EXT_external_memory_host`). With a full BAR this path is never taken.
- When a copy does not complete within five seconds, the device is marked uncertain: its resources are kept, not freed,
  and every later call on that device returns `VK_ERROR_DEVICE_LOST`.
- The frontend reads no environment variable. The plugin's developer switches `NVMTL_NO_SURFACE_PAGEOFF` and
  `NVMTL_NO_SURFACE_SHARE` do not reach it: it always imports the drawable's VRAM and always marks it newest. The way
  out of the runtime is per application: remove the JVM argument or `VK_DRIVER_FILES`.

## Interfaces with the driver

The frontend needs these from the driver, and this tree's NVK and plugin provide each of them:

| What | Where it is defined |
|---|---|
| `vk_icdGetInstanceProcAddr` and the loader interface, exported by NVK | Mesa's NVK |
| `nvmtl_nvk_stage_import`: the IOSurface import record, version 3 (`surfaceID`, `plane`, `size_B`) | `nvk/nvk-macos.patch`, `struct nvmtl_nvk_share` |
| an opaque-fd import with `VK_KHR_external_memory_fd`, which the record turns into the IOSurface's VRAM | `nvk/nvk-macos.patch`, `nvkmd_nvrm_import_dma_buf` |
| `VK_EXT_external_memory_host`, for the small-BAR path only | `nvk/nvk-macos.patch`, `nvkmd_nvrm_import_host_mem`, offered when the kernel supports it |
| `nvmtl_nvk_surface_dirty(device, surfaceID, plane)`, plane 0 without flags: the blocking mark | `nvk/nvk-macos.patch`, used the same way by `plugin/nvmtl_vk.c` |
| the `NVMTLVramAt:p0` tag `{0x564d5634, seed, generation}`, written after the mark | `plugin/NVMTLDevice.m` and `plugin/NVMTLObjects.m` |
| the `NVAccel` service whose parent's `device-id` matches the Vulkan device, and its Metal device | `kexts/NVRM/accel/Info.plist`, the plugin |

## Checks

- `host_test.mm` includes `bridge.mm` and replaces the renderer with fakes: enumeration bounds, the extension filter,
  device creation, the limited-BAR choice, surface formats, image acquisition and its timeouts, present failures and
  exception containment. It needs a Mac: `tools/test_nvk_bridge.py` builds it there with AddressSanitizer and
  UndefinedBehaviorSanitizer and runs it when Vulkan headers are at hand (`NM_VULKAN_INCLUDE`), and its top has the
  build line.
- `smoke.mm` drives an installed runtime: instance, device and the device features Minecraft needs, and with SDL3 a
  window with frames, a resize, a minimize and a restore. `NVKBRIDGE_VERIFY_PIXELS=1` reads every presented copy back
  and checks its pixels. To test the loader route, pass `libvulkan.1.dylib` as the entry with `VK_DRIVER_FILES` set to
  `nvk_bridge_icd.json` and `NVKBRIDGE_TEST_FRONTEND` set to the frontend's absolute path.
- `ModFreeProbe.java` calls Minecraft 26.3's own `NativeLibrariesBootstrap.tryLoadingVulkan` with
  `-Dorg.lwjgl.vulkan.libname` set and checks that the game, LWJGL and SDL all use the frontend. It needs Java 25 and the
  game's and LWJGL 3.4.3's jars; it does not start the game.
- `tools/test_nvk_bridge.py` checks on any system that the build, the package and the installer carry the runtime and
  that the interfaces above match this tree, and runs `build.sh` with stand-ins for Apple's tools.

## What is known

The source fork reports Minecraft 26.3 (Fabric and NeoForge) rendering through this frontend on an RTX 2060 SUPER,
macOS 26.7.1, with its own NVK build and a full 8 GiB BAR. This tree's version was built and checked on Linux only:
`bridge.mm`, `host_test.mm` and `smoke.mm` pass a syntax-only compile for x86_64 macOS 15.0 against a stand-in SDK, with
two declarations the stand-in lacks (`CGSizeMake`, `CALayer.opaque`) added for the check, and its build and package
wiring passes this repository's tests. It has not run on a Mac with this tree's NVK.

Limits: one window per swapchain, formats `B8G8R8A8_SRGB` and `B8G8R8A8_UNORM` only, FIFO and immediate present modes,
and two identical NVIDIA GPUs are refused (the frontend cannot tell their Metal devices apart). Each present allocates
and imports its copy target and waits for it on the CPU. That import and the mark after the copy are two kernel calls
per frame that take the accelerator's lock, and the frontend holds its device's submit lock during both, so the
application's own queue submissions wait as well. During a screen share a captured frame is paged off under that lock,
and a page-off can hold it while it waits for the GPU (the accelerator's `debug.nvaccel_surf_mark_lock_wait_*` counters
measure such waits). The cost of these calls has not been measured on a card. The Metal plugin imports each surface's
VRAM once and keeps it. With a BAR smaller than VRAM, NVK's own memory types are those of this tree's NVK: the source
fork's change of NVK memory types for a small BAR is not part of this runtime.
