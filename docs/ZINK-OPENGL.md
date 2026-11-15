# Native OpenGL through Zink on NVK

On macOS 15.5 or later the accelerator names `NVKZinkGLDriver.bundle` as its OpenGL driver. OpenGL applications then
run on NVK through Mesa's Zink instead of on Apple's OpenGL over Metal. Boot argument `nvzink=0` keeps Apple's
renderer for every application, as in 2.9.

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
   except `gldGetVersion`, `gldCreateContext` and `gldDestroyContext`. The bundle resolves each delegate in Apple's
   renderer when it loads. If Apple's renderer cannot be opened, or `gldGetVersion`, `gldCreateContext` or
   `gldDestroyContext` is absent from it, the bundle reports no OpenGL for the card (`gldGetVersion` answers 0 and
   `gldCreateContext` 10006) and says so on standard error; a start with `nvzink=0` gives Apple's renderer back, since
   the accelerator then names AppleMetalOpenGLRenderer itself. If any other delegate is absent, the bundle forwards the
   ones that resolved and points the missing one at a stub that returns a GLD error, so the card keeps Apple's OpenGL
   through the bundle rather than losing it; GLEngine on the same renderer does not call a delegate that renderer lacks.
2. After Apple's `gldCreateContext` succeeds, the bundle hands the new context to the CGL adapter. The bundle opens the
   adapter at run time; when the adapter or one of its two entry points is missing, the bundle only forwards and
   OpenGL stays Apple's.
3. The adapter leaves a context with Apple's renderer when Zink cannot start in this process (no NVK device, for
   example), when the application asked for a profile Zink does not serve, in OBS (below), and when the GLEngine
   private layout the adapter reads and writes does not match the record (next section). Otherwise it saves
   GLEngine's dispatch table and puts its own entries in the context's table.

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

## The layout guard

On any other macOS build (26.7.3, for example) the private offsets above are not known to hold. A build whose layout
differs would have these reads and writes land in the wrong memory of every OpenGL application.
Before it reads past any recorded pointer or writes through one, `nvk_cgl_prepare_gld` proves the memory is shaped as
the record says, using only facts the process already has and the sizes the public SDK headers give
(`zink/nvk_cgl_guard.h`). It runs before the process's Zink screen (a Vulkan instance and an NVK device) is made. On
any mismatch it keeps Apple's renderer for that context, having written nothing and opened no device. The checks are:

- the GLI context is a heap block that spans every recorded read (its own allocator reports the block's start and size:
  `malloc_zone_from_ptr` and `malloc_size`);
- the profile byte is legacy (0) or core (1);
- the CGL context derived from the recorded dispatch pointer is a heap block holding a whole `struct
  _CGLContextObject`, and its first member (`rend`) is this GLI context, which ties the recorded pointer to the public
  layout: on a mismatched layout that near never holds;
- the saved dispatch table is a heap block that spans a whole `GLIFunctionDispatch`, the table the adapter writes
  into.

`integer_dispatch_slot` applies the same test to the CGL private object and its engine list before reading through
them. The first refusal in a process is written once to standard error, naming the check that refused, so a user's
report can say why OpenGL stayed Apple's. The `malloc_size` of a pointer read from a wrong offset is almost never the
size of a real block, so a wrong layout is refused rather than written into; a layout the checks cannot confirm is
also refused, which keeps Apple's OpenGL working even where it costs the Zink path.

## Presentation

For a context with an AppKit view, frames are copied on the GPU into the IOSurface of the window's CAMetalLayer
drawable, through the same IOSurface import, completion fence and dirty notification NVK already offers the plugin
(`nvmtl_nvk_stage_import`, `nvmtl_nvk_surface_dirty`). A context without a view, or one whose layer cannot be used,
presents through an IOSurface that NVK imports. No frame goes through a CPU copy.

## The way out

Native OpenGL has one way out, boot argument `nvzink=0` (a row in tools/switches.tsv, docs/SWITCHES.md): the
accelerator names AppleMetalOpenGLRenderer again, read once when it starts, so every application keeps Apple's
OpenGL. Nothing else switches native OpenGL off or changes how it presents.

In OBS, OpenGL always stays Apple's and a screen capture asks for BGRA8 instead of RGBA16F, which can fail before the
first frame on this driver.

## Install and removal

`package/install.sh` stops before it changes anything when the package has no `NVKZinkGLDriver.bundle` or no
`nvmtl/zink`. It backs the bundle up with the others, installs it, owns it root:wheel, and puts the previous state back
on a rollback and in the fallback job of step 6b (the runtime goes with `nvmtl`). `package/uninstall.sh` removes both.

## What was checked

- The darwin cross-build on Linux: Mesa's 834 objects of libnvk_zink.dylib and zink_sync_probe with 0 errors and 0
  warnings, a link against empty framework stubs, the dispatch table generated from the 11.3 SDK's OpenGL headers,
  and `nvk_cgl.c` and `gld_bridge.c` compiled with `-Wall -Wextra -Werror` (`-fsyntax-only`).
- `tools/test_cgl_dispatch.py`: the generator's rules on synthetic headers.
- `tools/test_cgl_gld_guard.py`: the layout guard's checks, compiled for the Linux host from `zink/nvk_cgl_guard.h`
  and fed synthetic heap blocks. It accepts the recorded layout and refuses each way the layout can differ (a GLI
  context that is not a block or is too small, a profile byte out of range, a null or interior dispatch pointer, a CGL
  context whose `rend` is not the engine, a saved table that is not a block or is too small), each with its own reason,
  with controls. It loads no Apple framework and touches no GPU; `malloc_size` has no Linux name, so a shim behind the
  header's two macros uses `malloc_usable_size`.
- Not checked: `appkit.m` and `native_drawable.m` (no AppKit headers on Linux), the real link, running anything, and
  whether the recorded builds' saved dispatch table and CGL private object are their own heap blocks (the guard assumes
  so, as the adapter already assumes of the GLI integer callback vector; if one is interior on a Mac, those builds fall
  back to Apple's OpenGL, which is safe).
