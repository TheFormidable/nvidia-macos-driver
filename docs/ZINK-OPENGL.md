# Native OpenGL through Zink on NVK

On macOS 15.5 or later the accelerator names `NVKZinkGLDriver.bundle` as its OpenGL driver. OpenGL applications then
run on NVK through Mesa's Zink instead of on Apple's OpenGL over Metal. Boot argument `nvzink=0` keeps Apple's
renderer for every application, as in 3.0.

This was built and checked on Linux only (the cross-build and the checks below). It has not yet run on a Mac.

## The parts

| Part | Installed at | Built by |
|---|---|---|
| `NVKZinkGLDriver.bundle` | `/Library/GPUBundles/NVKZinkGLDriver.bundle` | `build/build_zink.sh` (zink/gld_bridge.c and a forwarder generated from the build Mac's AppleMetalOpenGLRenderer export names) |
| `libnvk_cgl.dylib`, the CGL adapter | `/Library/GPUBundles/nvmtl/zink` | `build/build_zink.sh` (zink/nvk_cgl.c, appkit.m, native_drawable.m and the dispatch table generated from the SDK) |
| `libnvk_zink.dylib`, Mesa 26.3.0's Zink with zink/mesa-zink-nvk.patch and the frontend in zink/ | `/Library/GPUBundles/nvmtl/zink` | `build/build_zink.sh` |
| `libnvk_zink_vulkan.dylib` and `libnvk_renderer.dylib`, Zink's private Vulkan driver | `/Library/GPUBundles/nvmtl/zink` | the nvkbridge component (its frontend, and a copy of this build's NVK); `build/package_local.sh` copies them |

`./build.sh build zink` builds the first three from Mesa `mesa-26.3.0` (0a6fa60a, pin `NM_PIN_ZINKMESA` in
build/pins.sh), which `./build.sh fetch` gets by commit beside NVK's Mesa. Zink does not get a second NVK: its
Vulkan driver is the copy of this build's NVK that the nvkbridge component carries.

## How a context reaches Zink

1. GLEngine loads the bundle the accelerator names. Every GLD entry point jumps to Apple's AppleMetalOpenGLRenderer,
   except `gldGetVersion`, `gldCreateContext` and `gldDestroyContext`.
2. After Apple's `gldCreateContext` succeeds, the bundle hands the new context to the CGL adapter. The bundle opens the
   adapter at run time; when the adapter or one of its two entry points is missing, the bundle only forwards and
   OpenGL stays Apple's.
3. The adapter leaves a context with Apple's renderer when Zink cannot start in this process (no NVK device, for
   example), when the application asked for a profile Zink does not serve, when `NVKZINK_DISABLE=1` is set, and in
   OBS (below). Otherwise it saves GLEngine's dispatch table and puts its own entries in the context's table.

## Where the layout comes from

The context's dispatch table is public. `CGLContext.h` in the macOS SDK declares
`struct _CGLContextObject { GLIContext rend; GLIFunctionDispatch disp; CGLPrivateObj priv; void *stak; }`, and
`gliDispatch.h` declares each of the 975 members of `GLIFunctionDispatch` with its signature. `CGLMacro.h` calls
through the same table. `build/generate_cgl_dispatch.py` reads those headers from the SDK that `xcrun --show-sdk-path`
names, and nothing else:

- each slot is `offsetof(struct _CGLContextObject, disp.<member>)`, so the compiler places it from the header;
- each member gets the GL names that reach it: CGLMacro.h's pairs first, then the member's own spelling, then the
  Khronos registry's alias groups. A name is kept only when its SDK prototype has the member's signature;
- a member no name reaches keeps Apple's entry. On the 11.3 SDK's headers that is 3 of 975 members, and 6 names are
  refused for their signature. `cgl-dispatch-map.json`, installed beside the adapter, lists every choice.

`zink/nvk_cgl.c` takes the size of the context's public part from the same header (`offsetof(..., priv)`) and the
drawable swap slot from `disp.swap_APPLE`.

Some offsets are not public: where GLEngine keeps the GLD state in its context (0x8e0), the share group (0x5b00), the
CGL and saved dispatch pointers (0xb2c8, 0xb2d0), the profile byte (0xc6a6) and the engine list of the private
context data (+0x78, +0x82, stride 0x1e90, callbacks +0x10). These are data recorded by the source fork this code is
ported from, as its observations of GLEngine on macOS 15.8.1 (24H32) and 26.7.1 (25G241),
x86_64. They are kept as named constants in zink/nvk_cgl.c and zink/gld_bridge.c. No tool in this tree reads a
framework binary, the shared cache or a kernel to find them, and nothing does so at run time.
`build/generate_gld_forward.py` lists the export names of AppleMetalOpenGLRenderer with `nm -gU`: names, not layouts.

## Presentation

For a context with an AppKit view, frames are copied on the GPU into the IOSurface of the window's CAMetalLayer
drawable, through the same IOSurface import, completion fence and dirty notification NVK already offers the plugin
(`nvmtl_nvk_stage_import`, `nvmtl_nvk_surface_dirty`). A context without a view presents through an IOSurface.

## Switches

All are rows in tools/switches.tsv (docs/SWITCHES.md).

| Switch | What it does |
|---|---|
| boot argument `nvzink=0` | the accelerator names AppleMetalOpenGLRenderer again; read once when it starts |
| `NVKZINK_DISABLE=1` | one application's OpenGL stays Apple's |
| `NVKZINK_PRESENT=gpu`, `cpu` or `native` | the IOSurface path for every context, a CPU copy, or the layer path with no fallback (default `auto`) |
| `NVKZINK_OBS_COMPAT=0` | in OBS, OpenGL runs on Zink as well and its screen capture keeps RGBA16F |
| OBS preference `NVKZinkCaptureCompatibility` (`defaults write com.obsproject.obs-studio NVKZinkCaptureCompatibility -bool NO`) | the same as `NVKZINK_OBS_COMPAT=0`; the variable wins when set |

In OBS, by default, OpenGL stays Apple's and a screen capture asks for BGRA8 instead of RGBA16F, which can fail before
the first frame on this driver.

## Install and removal

`package/install.sh` stops before it changes anything when the package has no `NVKZinkGLDriver.bundle` or no
`nvmtl/zink`. It backs the bundle up with the others, installs it, owns it root:wheel, and puts the previous state back
on a rollback and in the fallback job of step 6b (the runtime goes with `nvmtl`). `package/uninstall.sh` removes both.

## What was checked

- The darwin cross-build on Linux: Mesa's 834 objects of libnvk_zink.dylib and zink_sync_probe with 0 errors and 0
  warnings, a link against empty framework stubs, the dispatch table generated from the 11.3 SDK's OpenGL headers,
  and `nvk_cgl.c` and `gld_bridge.c` compiled with `-Wall -Wextra -Werror` (`-fsyntax-only`).
- `tools/test_cgl_dispatch.py`: the generator's rules on synthetic headers.
- Not checked: `appkit.m` and `native_drawable.m` (no AppKit headers on Linux), the real link, and running anything.
