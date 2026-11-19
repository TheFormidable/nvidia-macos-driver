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
build/pins.sh), which `./build.sh fetch` gets by commit beside NVK's Mesa. NVK is not built a second time for Zink:
its Vulkan driver is a second file copy of the nvkbridge component's frontend and of the NVK renderer copy that
component carries, so `nvmtl/zink` adds about the same 19.5 MB again as `nvmtl/minecraft` (measured on the Linux
cross-build).

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
3. Loading the bundle loads the adapter, which then replaces AppKit's `NSOpenGLContext` `setView:`, `update` and
   `clearDrawable` in that process (they marshal LWJGL's view calls to the main thread and tell the adapter a view's
   size), and in OBS also ScreenCaptureKit's `setPixelFormat:` (below). This happens in every process that loads
   OpenGL on this driver, also in one whose contexts all stay with Apple's renderer; only `nvzink=0` leaves the bundle
   unloaded.
4. The adapter leaves a context with Apple's renderer when Zink cannot start in this process (no NVK device, for
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
  `malloc_zone_from_ptr` and `malloc_size`; where a pointer that starts no block lies, this task's VM map says:
  `mach_vm_region_recurse`, which gives the region's tag and protection and reads no memory);
- the profile byte is legacy (0) or core (1);
- the CGL context derived from the recorded dispatch pointer is the start of a heap block of its own, not the GLI
  context, that holds a whole `struct _CGLContextObject`. This ties the recorded pointer to the public layout: a
  pointer read from a wrong offset, minus 8, almost never lands on the start of a block that size. On 26.7.3 (25G342)
  the derived context started a heap block of at least 0x1e90 bytes in every OpenGL test process of card-test round
  11; whether it is the GLI context was not checked there;
- the context's first member (`rend`) is null, this GLI context, a value that is no aligned user address, or one that
  starts no heap block, points into neither context and lies in heap memory or in no mapped region. gldCreateContext
  runs inside GLEngine's context creation, before CGL can store the new GLI context in `rend`; until then `rend` holds
  whatever the new block holds (zero from a cleared block; a free-list link or MallocPreScribble's 0xaa bytes from one
  that is not). A `rend` that is another GLI engine of the same CGL context, another heap block, a pointer into either
  context, or a pointer into an image, a stack or other memory malloc did not map (the vtable or class pointer of a
  foreign object) is refused. The adapter compares `rend` with the GLI context again on every call through the current
  context;
- the saved dispatch table, the one CGL restores the context's table from, is CGL's memory, and prepare writes the
  adapters into it. It is accepted as its own heap block spanning a whole `GLIFunctionDispatch`, as a span of the CGL
  context's block past the public struct, or as a span of readable, writable heap memory (a region malloc made) that
  starts at no block start and holds a whole `GLIFunctionDispatch` before the region ends: a table inside a CGL heap
  object the guard cannot see, such as the CGL private object's engine records, which are 0x1e90 bytes apart, room for
  a 0x18-byte head and a `GLIFunctionDispatch`. It is refused as a smaller block, the GLI context or a span inside it,
  another GLI engine, the CGL context's public part, a span running past the context's block or its heap region, a
  span that would reach the start of the GLI context or of the CGL context before a `GLIFunctionDispatch` ends, an
  address in an image, a stack, read-only or unmapped memory, or a value that is no aligned user address.

`nvk_cgl_prepare_gld` also adopts one GLI engine per CGL context: a further engine of a context already adopted (another
renderer of its pixel format) keeps Apple's renderer, having written nothing, and `nvk_cgl_last_error` says "The CGL
context already has a GLI engine served by Zink".

The first context `nvk_cgl_prepare_gld` refuses in a process is written once to standard error, as "NVK Zink: OpenGL
stays Apple's; GLEngine's private layout does not match the record (...)". The text in parentheses names the check
that refused and what the memory held, word by word: null, this GLI engine, another GLI engine of this CGL context, a
heap block of a given size, a pointer into a named block at an offset, or the value itself with the VM tag, protection
and remaining bytes of the region that holds it. Past the context check, it gives the sizes of the GLI engine's and
the CGL context's blocks too, so a report needs that one line and `sw_vers`.

`integer_dispatch_slot` runs later, on a context Zink already serves, to find the GLI integer callback vector that
CGLTexImageIOSurface2D goes through. The CGL private object and its engine list are memory the guard above did not
prove, so it reads the private object only as a heap block of its own, or as a span of the CGL context's block past
the public struct, that holds the recorded engine count and list pointer; and the engine list only as a heap block of
its own, or as a span of the private object's block past those two members, that holds the records the count says
(`nvk_cgl_room` in `zink/nvk_cgl_guard.h`). The vector is then cloned only when it is a heap block whose entries
`dladdr` names as GLEngine's callbacks. When the private object or the list is anywhere else, the context stays
Zink's, CGLTexImageIOSurface2D keeps Apple's handler in it, and the first such refusal in a process is written once
as "NVK Zink: CGLTexImageIOSurface2D in a Zink context stays Apple's; GLEngine's private layout does not match the
record (...)".

What the checks do not prove: a `rend` that is null, no aligned user address, or no heap object in heap or unmapped
memory is accepted on the strength of the context check alone, and so is a saved table that is a span of heap memory
at no block start. The context check covers the word at 0xb2c8; nothing ties the word at 0xb2d0 to it but the record,
which puts the saved table's pointer in the next word. A wrong layout passes the context check only with a pointer
exactly 8 bytes into a heap block of at least 0x1e90 bytes. A build that changes only the word at 0xb2d0 is refused
when that word is a smaller block, the GLI context, another GLI engine, the CGL context's public part, or an address
outside writable heap memory, and is written into when it points at least 0x1e78 bytes before the end of a writable
heap region: a span inside a foreign heap object, whose memory the adapters' pointers then overwrite.

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
The installer and uninstaller of release 2.9 and of earlier 3.0 packages do not know the bundle: going back with one of
them leaves `/Library/GPUBundles/NVKZinkGLDriver.bundle` in place (their accelerator does not name it, so it is not
loaded). Run this package's `sudo ./uninstall.sh` first, or remove the bundle with
`sudo rm -rf /Library/GPUBundles/NVKZinkGLDriver.bundle`.

Once the bundle is in place, the installer asks macOS's trusted GPU-bundle lookup (`gpu_bundle_find_trusted`, the one
that finds `NVMTLDriver.bundle` for Metal) for it with `nvmtl/zink/gld_bundle_probe` (tools/gld_bundle_probe.c, from
the HighDelay/nvidia-macos-driver-nvk fork). If the lookup finds `NVMTLDriver.bundle` but not `NVKZinkGLDriver.bundle`,
the install stops and rolls back, since every OpenGL application would otherwise find no driver. If the lookup does
not find `NVMTLDriver.bundle` either, the probe cannot judge, and the installer says so and goes on.

## What was checked

- The darwin cross-build on Linux: Mesa's 834 objects of libnvk_zink.dylib and zink_sync_probe with 0 errors and 0
  warnings, a link against empty framework stubs, the dispatch table generated from the 11.3 SDK's OpenGL headers,
  and `nvk_cgl.c` and `gld_bridge.c` compiled with `-Wall -Wextra -Werror` (`-fsyntax-only`).
- `tools/test_cgl_dispatch.py`: the generator's rules on synthetic headers.
- `tools/test_build_zink_tools.py`: `build/build_zink.sh`'s check of the bison and flex on its PATH, with stand-ins.
  Mesa 26.3.0's grammars need Bison 3.0 or newer: a bison of 3.0 or newer passes, and macOS's 2.3, a bison that
  prints no version, a missing bison and a missing flex each stop the script before meson, naming `./build.sh deps`
  where a bison is the reason.
- `tools/test_cgl_gld_guard.py`: the layout guard's checks, compiled for the Linux host from `zink/nvk_cgl_guard.h`
  and fed synthetic heap blocks and VM regions. It accepts the layouts the record allows and refuses each way the
  layout can differ, with controls: a GLI context that is not a block or is too small, a profile byte out of range, a
  null, unaligned or interior dispatch pointer, a CGL context that is the GLI context; a `rend` that is null, stale,
  not a heap pointer, or inside heap memory at no block start (accepted) or another GLI engine, another heap block, a
  pointer into either context, or a pointer into an image's data or a stack (refused); a saved table past the
  context's public part or a span of writable heap memory at no block start (accepted) or too small, inside the GLI
  context, in the CGL context's public part, past its block, another engine, unaligned, unmapped, in an image's data,
  read-only, under a tag malloc does not use, running past its heap region, or reaching the start of the GLI context
  or of the CGL context (refused); each refusal's text; and `nvk_cgl_room`, the question `integer_dispatch_slot` asks
  of the CGL private object and its engine list. It loads no Apple framework and touches no GPU; `malloc_size` has no
  Linux name, so a shim behind the header's macros uses `malloc_usable_size`, and a table of synthetic regions stands
  in for the VM map.
- `nvk_cgl.c` and `appkit.m` with clang 18 `-fsyntax-only` on Linux against the public macOS 11.3 SDK's own headers
  (`-isysroot`, target `x86_64-apple-macos15.0`, the flags of `build/build_zink.sh`): no diagnostic. This covers
  `nvk_cgl_vm_region`'s call of `mach_vm_region_recurse` and the VM tag names; the 11.3 SDK names tag 13
  `VM_MEMORY_MALLOC_PGUARD`.
- Not checked: `native_drawable.m` (it names `kIOMainPortDefault`, which the 11.3 SDK does not have), a compile with
  Xcode against a current SDK, the real link, running anything; where the recorded builds keep the CGL private object
  and its engine list (`integer_dispatch_slot` has not run on a Mac); when CGL stores `rend` relative to
  gldCreateContext and how it allocates the context, on any build (25G342 refused at the earlier `rend == engine`
  check: card-test round 11); where the saved dispatch table is on any build (the saved-table check has not run on a
  Mac); and the VM tags malloc gives its regions on macOS 26.7.3.
