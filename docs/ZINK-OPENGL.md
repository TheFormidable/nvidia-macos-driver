# Native OpenGL through Zink on NVK

On macOS 15.5 or later the accelerator names `NVKZinkGLDriver.bundle` as its OpenGL driver, with no setting. OpenGL
applications then are routed to NVK through Mesa's Zink when the guard accepts the context, instead of on Apple's
OpenGL over Metal. Boot argument `nvzink=0` keeps Apple's renderer for every application, as in 2.9.

**Status at 3.0.20.** On the fork's one test machine (RTX 5050, macOS 26.7.3, build 25G342) OpenGL goes through Zink
since 3.0.6, the first package installed there with the 3.0.3 guard: the accelerator reports `IOGLBundleName`
`NVKZinkGLDriver`, the OpenGL rows of the 3.0.7 card run went through Zink (none inactive), and the games measured
there draw through it. A card run there before 3.0.3 had 39 Zink rows inactive, 38 of them refused by the older guard.
That is one card on one macOS build: on another, a selected bundle is not proof that Zink serves a context, so check
GL_RENDERER in the application. See [STATUS.md](STATUS.md).

What later versions changed; from 3.0.7 on each change is on by default and has one way back ("The way out"):

| Version | What changed | Section |
|---|---|---|
| 3.0.3 | The layout guard accepts a CGL context whose `rend` does not yet hold the GLI engine, which is what 26.7.3 hands over | "The layout guard" |
| 3.0.7 | A swap runs one frame ahead of the GPU and keeps its import of the window's video memory (kernel, NVK and Zink) | "Presentation" |
| 3.0.8 | A swap asks WindowServer for the window's opacity at most once and keeps the answer 100 ms | "Presentation" |
| 3.0.11 | With the swap interval at 0 a swap leaves its frame to a sender thread and does not wait for the display: the newest frame is shown | "Presentation" |
| 3.0.16 | `glBufferSubData` writes through a staging buffer where Mesa's threaded context would wait for the GPU | "Buffer uploads" |
| 3.0.17 | NVK uses the GPU's hierarchical Z (ZCULL); under Zink it acts in the render passes that clear depth at load | "Hierarchical Z" |

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
   GLEngine's dispatch table and puts its own entries in the context's table. From then on nothing gives the context
   back to Apple's renderer: a call the adapter cannot serve is dropped (next section, "What the checks do not
   prove").

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
differs would have these reads and writes land in the wrong memory of every OpenGL application. On 26.7.3 (25G342)
the guard of 3.0.3 and later accepts the contexts and Zink serves them (Status, above).
Before it reads past any recorded pointer or writes through one, `nvk_cgl_prepare_gld` checks that the memory is
shaped as the record says, using only facts the process already has and the sizes the public SDK headers give
(`zink/nvk_cgl_guard.h`). It runs before the process's Zink screen (a Vulkan instance and an NVK device) is made. On
any mismatch it leaves the engine it was handed unadapted, having written nothing and opened no device. For the first
GLI engine of a CGL context, this leaves that context on Apple's renderer; what happens to a later engine of the same
context is described below. Below, the GLI engine is GLEngine's per-context object, the pointer `gldCreateContext`
hands over (the sections above call it GLEngine's context; Apple's headers type it `GLIContext`). The checks are:

- the GLI engine is a heap block that spans every recorded read (the process's allocator reports a block's start and
  size: `malloc_zone_from_ptr` and `malloc_size`; where a pointer that starts no block lies, this task's VM map says:
  `mach_vm_region_recurse`, which gives the region's tag and protection and reads no memory);
- the profile byte is legacy (0) or core (1);
- the CGL context derived from the recorded dispatch pointer is the start of a heap block of its own, not the GLI
  engine, that holds a whole `struct _CGLContextObject`. This ties the recorded pointer to the public layout: a
  pointer read from a wrong offset, minus 8, almost never lands on the start of a block that size. On 26.7.3 (25G342)
  the guard of 3.0.1, which asked here only for a heap block of at least 0x1e90 bytes, found that held in each of the
  38 card rows it then refused at the older `rend` check. Whether that block is the CGL context, and that it is not
  the GLI engine itself, was not checked there;
- the context's first member (`rend`) is null, this GLI engine, a value that is no aligned user address, or one that
  starts no heap block, points into neither the engine nor the context and lies in heap memory or in no mapped
  region. This check rests on an assumption no record confirms: that CGL stores the new GLI engine in `rend` only
  after GLEngine's context creation, which calls `gldCreateContext`, has returned, so that `rend` holds at this point
  whatever the new block holds (zero from a cleared block; a free-list link, MallocPreScribble's 0xaa bytes or a word
  of the block's previous owner from one that is not). The source fork's adapter never reads `rend` there. A `rend`
  that is another GLI engine of the same CGL context, another heap block, a pointer into the engine or the context,
  or a pointer into an image, a stack or other memory malloc did not map (the vtable or class pointer of a foreign
  object) is refused;
- the saved dispatch table, the one CGL restores the context's table from, is CGL's memory, and prepare writes the
  adapters into it. It is accepted as its own heap block spanning a whole `GLIFunctionDispatch`, as a pointer-aligned
  span of the CGL context's block past the public struct, or as a span of readable, writable heap memory (a region
  malloc made) that starts at no block start and holds a whole `GLIFunctionDispatch` before the region ends: a table
  inside a CGL heap object the guard cannot see, such as the CGL private object's engine records, which are 0x1e90
  bytes apart, room for a 0x18-byte head and a `GLIFunctionDispatch`. It is refused as a smaller block, the GLI
  engine or a span inside it, another GLI engine of the same CGL context, the CGL context's public part, a span
  running past the context's block or its heap region, a span that would reach the start of the GLI engine or of the
  CGL context before a `GLIFunctionDispatch` ends, an address in an image, a stack, read-only or unmapped memory, or a
  value that is no aligned user address.

An engine that passes the checks is adopted, or left unadopted where the Zink screen is not ready (it could not be
made, or it was marked uncertain after GPU work whose completion could not be confirmed, which `nvk_zink.c` reports
with "NVK Zink: completion uncertain; share group quarantined") or memory ran out. `nvk_cgl_prepare_gld` remembers
both outcomes for the CGL context: the first by the context's bridge, the second in one slot, which holds the last
such context. A later engine of such a context (another renderer of its pixel format) is a further engine. It is left
unadapted and prints no line, whatever its own check says: the line would say that OpenGL stays Apple's for a layout
that differs, and that is not what decided the context. In a context Zink serves, `nvk_cgl_last_error` then says "The
CGL context already has a GLI engine served by Zink", and leaving the engine unadapted does not hand its calls to
Apple's renderer: a call that reaches the context's adapters with it is dropped, with the third line below. In a
context left unadopted prepare sets no last error. Three limits:

- A later engine whose check stops before the context's (its own block, its profile byte, its dispatch pointers) names
  no context, so prepare cannot tell that it is a further engine and refuses it like a first engine, with the line.
- A refusal is not remembered. A later engine of a context whose earlier engine was refused is checked like a first
  engine: it is refused again when its check refuses, and adopted when its own check passes (the saved table is a
  word of each engine, so one engine's can be refused and another's accepted). Zink then serves, through that engine,
  a context the line said stays Apple's.
- The slot holds one context, until the GLD context of the engine left unadopted is destroyed. After an engine of
  another context is left unadopted in between, a later engine of the earlier context is checked like a first engine
  too: it is refused, with the line, when its check refuses, and adopted when its check passes and its bridge can be
  allocated. That can happen only where the earlier engine was left for want of memory, since a Zink screen that is
  not ready does not become ready later in the process.

The adapter prints three kinds of line on standard error, each once per process, the first time it happens:

- "NVK Zink: OpenGL stays Apple's; GLEngine's private layout does not match the record (...)" for the first context
  `nvk_cgl_prepare_gld` refuses. The text in parentheses names the check that refused and what the memory held, word
  by word: null, this GLI engine, another GLI engine of this CGL context, a heap block of a given size, a pointer into
  a named block at an offset, or the value itself with the VM tag, protection and remaining bytes of the region that
  holds it. Past the context check, it gives the sizes of the GLI engine's and the CGL context's blocks too. A later
  context refused for another reason prints nothing. The line means what it says for the context it names, with the
  two exceptions above: it can come from a later engine of a context Zink serves, whose check stopped before the
  context's, and a later engine of the context it names can pass its own check and be adopted.
- "NVK Zink: CGLTexImageIOSurface2D in a Zink context stays Apple's; GLEngine's private layout does not match the
  record (...)". When Zink first serves a context (at its first GL call), in every process and whether or not the
  application ever calls CGLTexImageIOSurface2D, `install_integer_dispatch` places a hook in the GLI integer callback
  vector that call goes through; `integer_dispatch_slot` finds the vector in the CGL private object's record of the
  engine. The CGL private object and its engine list are memory the guard above did not check, so it reads the private
  object only as a heap block of its own, or as a pointer-aligned span of the CGL context's block past the public
  struct, that holds the recorded engine count and list pointer; and the engine list only as a heap block of its own,
  or as a pointer-aligned span of the private object's block past those two members, that holds the records the count
  says (`nvk_cgl_room` in `zink/nvk_cgl_guard.h`). The vector is cloned only when it is a heap block of 16 to 512
  pointers whose entries 4, 6, 9 and 15 `dladdr` names gliCreateContext, gliDestroyContext, gliSetInteger and
  gliGetVersion. In every other case (the private object or the list is anywhere else, the list pointer is null or the
  count is outside 1 to 16, no record names the engine, the vector is null, starts no heap block or is a block of
  another size, `dladdr` does not give those four names) the context stays Zink's, only CGLTexImageIOSurface2D keeps
  Apple's handler in it, and the text in parentheses says which case it was. On a Mac where that happens the line
  appears in every OpenGL process Zink serves, and every Zink row of the card checks fails its "messages" check. The
  one case that prints nothing is a failed allocation of the clone.
- "NVK Zink: a call through a Zink context was dropped; it did not come with the GLI engine Zink adopted for the current
  CGL context (...)". Every adapted call compares the GLI engine it comes with (CGL passes the context's `rend`) with
  the engine adopted for the current context. When they differ the call is dropped: it returns 0 or nothing, and neither
  Zink nor Apple's renderer runs it. The text in parentheses says what the call came with, what `rend` holds and whether
  the two are one value. The line is printed for the first such call in the process; calls that come with the adopted
  engine, and the process's other contexts, are served. The source fork drops such a call the same way, without a line.

What the checks do not prove:

- Accepting `rend` at creation says nothing about what CGL stores there afterwards. If the assumption above is wrong
  in a way the guard cannot see (`rend` never becomes the adopted engine), prepare has already put the adapters in
  the context's table and nothing gives the context back to Apple's renderer: every GL call of that context is
  dropped and returns 0 or nothing, `glGetString` included. The context draws nothing, and an application that
  checks what `glGetString` gives may report that OpenGL is unavailable or stop at start. The third line above says
  so once. Boot argument `nvzink=0` is the way out.
- The verdict on `rend` is per context. Where CGL does not clear the context's block, a word left by the block's
  previous owner (a pointer to a live heap block, say) is refused in one context while another context of the same
  process is adopted. Contexts of one share group can then be split between Apple's renderer and Zink, which cannot
  see each other's objects, and only the first refusal in the process prints.
- The context check covers the word at 0xb2c8; nothing ties the word at 0xb2d0 to it but the record, which puts the
  saved table's pointer in the next word. A wrong layout passes the context check only with a pointer exactly 8 bytes
  into a heap block of at least 0x1e90 bytes. A build that changes only the word at 0xb2d0 is refused when that word
  is a smaller block, the GLI engine, another GLI engine of the same CGL context, the CGL context's public part, or an
  address outside writable heap memory. It is accepted, and the adapters' pointers are written over it, when that
  word starts any other heap block of at least 0x1e78 bytes (the allocator cannot say whose block it is; a pointer
  read from a wrong offset is rarely the start of a block that size), or points at least 0x1e78 bytes before the end
  of a writable heap region, inside a foreign heap object.

## Presentation

For a context with an AppKit view, frames are copied on the GPU into the IOSurface of the window's CAMetalLayer
drawable, through the same IOSurface import, completion fence and dirty notification NVK already offers the plugin
(`nvmtl_nvk_stage_import`, `nvmtl_nvk_surface_dirty`). A context without a view, or one whose layer cannot be used,
presents through an IOSurface that NVK imports. No frame goes through a CPU copy.

Through 3.0.6 the thread that swapped did all of it for every frame: it imported the surface's video memory, sent the
copy, slept until the GPU had finished the frame, marked and tagged the surface and presented the drawable. Measured
in a Minecraft modpack at 55 to 64 frames per second on an RTX 5050, that thread spent 30 to 44 % of its time asleep
in the swap and 6 to 8 % importing, for a layer that hands out the same three surfaces in turn.

Since 3.0.7 a swap is one frame ahead (`zink/nvk_present_queue.h`, the second half of `zink/nvk_present.c`):

- The swap draws the frame into the scratch image, flushes, sends the copy to the same queue right behind it and
  returns. One thread of the process, the presentation thread, waits for the copy's fence and then does what the
  swap did, in the same order: the mark, the tag, the drawable.
- Before a context starts its next frame, and before it asks the layer for the next drawable, it waits for that to
  have happened. A context never has more than one frame on its way, so the scratch image is drawn into again only
  when nothing reads it, and what is on screen is at most one frame behind what was swapped.
- A front-buffer flush, and a swap while a diagnostic observer is set, wait for their own frame as before.
- A frame the presentation thread could not publish (the mark of an import made for that frame was refused, the
  channel was rebuilt under the copy) is released unpresented, and the next swap of that context says so on standard
  error, once and then at every doubling of the count. The swap that sent it had already returned 1.
- The import of a surface's video memory is kept, for up to four surfaces per context, and let go when the context has
  presented 64 frames without it, when a swap finds no drawable, when the context goes back to 3.0.6's path, and with
  the context; a context that stops swapping keeps what it has until then. The kernel names the block it shared with
  each import, and a mark for a kept import names that block: when the surface has another one by then (or none), the
  kernel refuses the mark with `NVRM_SS_MOVED` and counts it in `debug.nvaccel_surf_mark_moved`. The frame is still
  whole in the scratch image, so the presentation thread lets that import go and sends the frame again at once, the
  way 3.0.6 sent every frame (a new import, the copy, the wait, a mark that names no block, the tag), before it
  answers; it does the same for any other refusal of such a mark. Whether a frame reaches the screen does not depend
  on the application swapping again. It is not sent again once the share group is quarantined: nothing is sent then,
  as in 3.0.6. After four refused marks in a context's life the context lets its kept imports go and imports for every
  frame from then on, and says so once on standard error.
- Anything this path does not take (NVK refuses the import, the surface is not one the copy can fill) goes through
  3.0.6's path for that frame, unchanged; after three such frames in a row the context stays with it and lets its kept
  imports go.
- A GPU that does not finish a copy in 5 s, or a presentation thread that stays at one frame for 12 s (5 s for the
  copy, 5 s more for a frame it had to send again), quarantines the share group as before: nothing is freed, nothing
  more is presented. The 12 s are that thread's and not the waiting swap's, because it works the frames of every
  window of the process in turn: a frame behind another window's slow one is not given up while that one is within its
  time, and a frame whose work is already past it is given up at once.
- The presentation thread takes none of the application's signals, except the ones a fault of its own raises (SIGSEGV,
  SIGBUS, SIGILL, SIGFPE, SIGTRAP, SIGSYS), which stay open so that a crash there is reported as one.

What this rests on, and no test isolates: a duplicate of the RM object keeps a surface's video memory alive in
this process after the surface gave it up (the Metal plugin's imports rely on the same); a drawable may be presented
and released from a thread that is not the one that asked for it (the card check `zink.win-thread` does that with
the swap itself); and the waits of an application that reads back or queries its own frame move into the next
frame. Every package the test machine has run since 3.0.7 presents this way by default. On 3.0.7 a capture of the
modded pack gave 90 frames a second with the GPU 83 % busy, where a comparable capture on 3.0.6 gave 61 and 51 % (not
the identical scene; CHANGELOG.md, 3.0.7, What ran where). No card row runs a swap one frame ahead (the rows set the
diagnostic observer), and `zink.win-thread` failed on 3.0.7 for the time of its own pixel reader.

Whether the drawable is opaque decides the layer's `opaque` and whether the copy writes alpha 1. It is the context's
`kCGLCPSurfaceOpacity`, and CGL answers it with a message to WindowServer and its reply. Through 3.0.7
`present_drawable` asked twice per swap, once for the layer and once for the copy (on the IOSurface path, there instead
of for the copy); in a capture of the Minecraft pack on 3.0.7 that was 5.7 % of the game's render thread. Since 3.0.8 a
swap asks at most once and gives the layer, the copy and the IOSurface path that one answer, and a context keeps it
(`zink/nvk_opacity.h`): it asks again on the first swap at least 100 ms after it last asked, and at once on its first
swap, after an ask that failed (which answers opaque and is not kept), and after the adapter has seen the context's
output change: a size it applies (`bridge_current`, `nvk_cgl_resize`), a view bound or unbound (`nvk_native_bind_view`,
which AppKit's `setView:` and `update` reach on the main thread), the native target removed (`nvk_native_remove`, which
`clearDrawable` reaches), and a native acquire that gave no drawable, except a swap that again finds no view. So after
an application changes `kCGLCPSurfaceOpacity` of a context that has swapped, the swaps of up to the next 100 ms are
composed with the old value, where 3.0.7 used the new one from the next swap. Where `CGLSetParameter` of that parameter
(236) passes the GLI integer callback the adapter replaces for `CGLTexImageIOSurface2D`, the next swap asks at once;
whether it does has not been seen, and nothing depends on it. The gain has not been measured: 3.0.8 was put in place on
the test machine without a capture.

Since 3.0.11 a swap with the swap interval at 0 does not ask the layer either (`zink/nvk_newest.h`). It copies the
frame into the scratch image, flushes, waits for the GPU to have finished the frame parked before it, and returns; the
frame stays parked in the scratch image. A sender thread waits for the frame in flight, asks the layer for a drawable,
and hands the frame that is parked by then to the presentation thread. The application is one frame ahead of the GPU
as before and is not held to the display; a frame replaced before the layer had room is never copied, and the last
frame drawn always is. A front-buffer flush parks the front buffer the same way. Only a frame after one that was presented
by its own swap or flush goes this way: the first frame, a resize, a missing view, an observer and every failure are
presented by the swap, after the sender has been stopped. `tools/test_zink_newest.py` runs the agreement between the two threads.
On the test machine Minecraft without mods on OpenGL through Zink gave 490 frames a second on 3.0.11, read by its
owner on the game's own counter (no capture, and no figure on Zink from before); a resize, a minimize and a second
display under the sender have not been tried.

## Buffer uploads (3.0.16)

Mesa's threaded context (`u_threaded_context.c`) normally writes a busy buffer through a staging buffer and a copy
queued on the GPU. It maps the buffer itself, and waits for the buffer's whole batch, in two cases: a write of more
than 320 bytes into a buffer the application has mapped, and a write it infers to be unsynchronized that overlaps the
range of a staging upload not executed yet (Mesa never resets its record of that range, so the overlap is often with
an upload long done). In ATM10 with a shader pack on the test machine (2026-11-23, 3.0.12 with NVK of 3.0.15), the
game's render thread spent 19 to 25 % of its time asleep in that wait, inside `glBufferSubData`.

Since 3.0.16 `zink/mesa-zink-nvk.patch` gives the threaded context an option, `subdata_never_wait`, which Zink sets on
NVK on macOS; `tc_buffer_subdata` then writes those two cases through the threaded context's own staging path
(`tc_subdata_stage_instead` in `u_threaded_context.h`). A subdata call writes every byte of its range and the copy is
queued after every earlier call, so the buffer ends up as the mapping would have left it. Excluded, as before: a buffer
of user memory, with CPU storage, in a client pointer heap, sparse or unmappable; a persistent mapping; a write the
threaded context already stages; a write of 320 bytes or fewer that is not unsynchronized. What an application can
see: in a buffer it keeps mapped, the bytes reach the mapping when the GPU runs the copy, not when the call returns.
The GL specification asks for a fence before such a read; `NVK_ZINK_SUBDATA_STAGE=0` is for a program that reads
without one.

On the card the change has been seen only where the game did not wait: two ATM10 captures on 2026-11-24, as installed
and with `NVK_ZINK_SUBDATA_STAGE=0`, both without the wait (GPU at 96 to 98 %). A capture taken while the wait is there
has not been recorded.

## Hierarchical Z (3.0.17)

NVK's ZCULL code (a ZCULL plane for a depth image, the unit programmed at the start of a render pass) runs only when
the device reports the unit's geometry, and the NVRM backend reported none before 3.0.17; `nvk/nvk-zcull.patch` asks
RM for it (`NV2080_CTRL_CMD_GR_GET_ZCULL_INFO`). NVK keeps a ZCULL plane only for a depth image that cannot be a copy
destination. Zink creates its depth images as copy destinations, so for OpenGL applications ZCULL acts in the render
passes that clear depth at load, which is how a frame usually starts. It stays off in WindowServer. `NVK_ZCULL=0` turns
it off for NVK in every process, Zink's included; `NVK_ZCULL_LOG=1` prints NVK's decision once per device. Whether RM
answers the query on the RTX 5050, and any gain or artefact in a game, are not recorded.

## The way out

Native OpenGL has one way out, boot argument `nvzink=0` (a row in tools/switches.tsv, docs/SWITCHES.md): the
accelerator names AppleMetalOpenGLRenderer again, read once when it starts, so every application keeps Apple's
OpenGL. Nothing else switches native OpenGL off.

Each change from 3.0.7 on has a switch of its own, a line of the settings file
(`/Library/GPUBundles/nvmtl/nvrm610.conf`). The Zink frontend reads its four once per process, together, at the
process's first Zink context:
`NVK_ZINK_PRESENT_AHEAD=0` (3.0.7) makes every swap wait for the GPU and publish its own frame, through the code of
3.0.6; `NVK_ZINK_SURFACE_KEEP=0` (3.0.7) leaves swaps one frame ahead and imports the drawable's video memory for every
frame; `NVK_ZINK_PRESENT_NEWEST=0` (3.0.11) makes a swap with the interval at 0 ask the layer itself again, as in
3.0.9; `NVK_ZINK_SUBDATA_STAGE=0` (3.0.16) leaves the threaded context's option off, so `glBufferSubData` maps and
waits as in 3.0.14. `NVK_ZINK_OPACITY_KEEP=0` (3.0.8), read the same way by the CGL adapter (`libnvk_cgl.dylib`) when
its first context presents, makes every swap ask WindowServer for the drawable's opacity again, once.
A line may have blanks before the name, around the `=` and before the value, so every spelling that turns a switch
of the Metal plugin off in that file turns one of these off (the plugin itself keeps a blank after the `=` as part
of the value); the last line that names a switch decides. As for those settings, a variable of that name in a process's own
environment counts only when that process also sets `NVMTL_PROCESS_ENV=1`.
`NVK_ZCULL=0` (3.0.17) is NVK's: it turns hierarchical Z off in every process that runs on NVK, not only under Zink.

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

On the optional route that keeps AMFI enforcing, `tools/sign_graphics.sh` signs `NVKZinkGLDriver.bundle` with
`NVMTLDriver.bundle` and the `nvmtl` libraries, with a CA of your own that OpenCore registers through AMFIAnchor
([SIGNED-GRAPHICS-EXPERIMENT.md](SIGNED-GRAPHICS-EXPERIMENT.md)). 3.0.20 ran that way on the test machine; it is
experimental, and the documented install keeps the AMFI boot arguments.

## What was checked

- On the card (RTX 5050, macOS 26.7.3): every package installed since 3.0.6, built with Xcode on that Mac; the card
  checks of 3.0.7 (133 of 134 rows ran, the OpenGL rows through Zink, no Xid; `zink.win-thread` and `zink.migrate`
  failed, described in CHANGELOG.md, 3.0.7); the captures and game readings above. [STATUS.md](STATUS.md) has each
  version.
- The darwin cross-build on Linux: Mesa's 834 objects of libnvk_zink.dylib and zink_sync_probe with 0 errors and 0
  warnings, a link against empty framework stubs, the dispatch table generated from the 11.3 SDK's OpenGL headers,
  and `nvk_cgl.c` and `gld_bridge.c` compiled with `-Wall -Wextra -Werror` (`-fsyntax-only`).
- `tools/test_cgl_dispatch.py`: the generator's rules on synthetic headers.
- `tools/test_build_zink_tools.py`: `build/build_zink.sh`'s check of the bison and flex on its PATH, with stand-ins.
  Mesa 26.3.0's grammars need Bison 3.0 or newer: a bison of 3.0 or newer passes, and macOS's 2.3, a bison that
  prints no version, a missing bison and a missing flex each stop the script before meson, naming `./build.sh deps`
  where a bison is the reason.
- `tools/test_cgl_gld_guard.py` (3.0.3): `zink/nvk_cgl_guard.h` compiled for the host and fed synthetic heap blocks
  and VM regions. It accepts the layouts the record allows and refuses each way the layout can differ, at the
  boundaries and with controls: the GLI engine's block, the profile byte, the dispatch pointers, the CGL context, each
  kind of `rend` and of saved table listed under "The layout guard", and a span of memory at no block start under each
  VM tag from 0 to 31 and each protection (a saved table only under the tags malloc gives, readable and writable); each
  refusal's text with the sizes it prints, the text of the dropped-call line, which engines count as further engines,
  and `nvk_cgl_room`. Its other checks read `zink/nvk_cgl.c`, which the host cannot compile: that prepare reads
  through no recorded offset before the check, returns from every refusal before the screen and any write, and holds
  no other return and no other write to the refusal's reason before it allocates the bridge, and that the three lines
  are printed where this document says and are quoted here, in TESTING-ON-HARDWARE and in the CHANGELOG as the code
  builds them. It loads no Apple framework and touches no GPU; `malloc_size` has no Linux name, so a shim behind the
  header's macros uses `malloc_usable_size` on Linux (`malloc_size` on a Mac), and a table of synthetic regions stands
  in for the VM map.
- `nvk_cgl.c` and `appkit.m` with clang 18 `-fsyntax-only` on Linux against the public macOS 11.3 SDK's own headers
  (`-isysroot`, target `x86_64-apple-macos15.0`, the flags of `build/build_zink.sh`): no diagnostic. This covers
  `nvk_cgl_vm_region`'s call of `mach_vm_region_recurse` and the VM tag names; the 11.3 SDK names tag 13
  `VM_MEMORY_MALLOC_PGUARD`.
- The card checks' two programs, `tools/card/zink_gl_check.m` and `tools/card/zink_win_check.m` (both include
  `tools/card/zink_common.h`), with clang 18 `-fsyntax-only` on Linux against the same SDK, with the flags of their
  rows and Mesa's Vulkan headers: no error, and no warning but three that say `NSOpenGLContext`'s `view` is
  deprecated, in lines 3.0.3 did not touch. The shell fragment of the glbench row under sh and bash, with a
  first line that names Zink, "(null)", another renderer and none.
- `tools/test_zink_present_queue.py` (3.0.7): the presentation queue as it is, with real threads (who waits for whom,
  the limit as the presentation thread's time at one frame and not a swap's wait, teardown, two threads presenting one
  context, a fork, which signals the thread takes), and the half of `nvk_present.c` that presents one frame ahead, cut
  out of the file as it is, against a scripted GPU and a scripted kernel that remembers which block each surface has:
  the order fence, mark, tag, drawable; a fence reset before it is used again; kept imports, their eviction and their
  age to the frame; a block that moved under a kept import and the frame sent again without another swap; a context
  that stops keeping; a GPU that does not finish, and no frame sent
  again once the share group is quarantined; every refusal; both switches and how their lines may be written; an NVK
  and a kernel from before 3.0.7. A third program runs the end of `present_drawable` and `native_frame_done`, cut out
  of `nvk_cgl.c`, for who releases the drawable in each answer of the frontend. Parts run again under ThreadSanitizer
  and AddressSanitizer, and all of it against 31 copies of the sources with one rule broken each, every one of which
  has to fail; the text of 3.0.6's path, which every way back leads to, is held unchanged. `nvk_zink_present_frame`
  itself and the 12 s limit are read, not run (the programs run with 0.4 s). Its recorded runs are on Linux, with
  gcc 13 and clang 18. `tools/test_surf_keep.py` runs the kernel's side of a kept import the same way: the two rules
  and the lines of the escape that apply them.
- `tools/test_zink_opacity.py` (3.0.8): `zink/nvk_opacity.h`, as it is, with a clock and a reader of its own: the
  first swap asks, nothing is asked within 100 ms, one ask at 100 ms, a change of the output asks at once, with the
  switch off every swap asks once, a failed ask answers opaque and is not kept, a clock that goes back or wraps keeps
  no answer for good; plain and under AddressSanitizer and UndefinedBehaviorSanitizer, and against 13 copies with one
  rule broken each, every one of which has to fail. It reads `nvk_cgl.c` and `native_drawable.m` for one ask per swap,
  the switch, and each place that makes the next swap ask, and against 3 copies of them with one such place broken
  each, every one of which has to fail that reading. Its recorded runs are on Linux, with gcc 13 and clang 18.
- `tools/test_zink_newest.py` (3.0.11): `zink/nvk_newest.h` as it is, with real threads (a frame parked while another
  is sent goes with the same sender, a stop waits for the frame being sent, 20000 frames at an uneven pace against a
  slow layer end with the last one sent), plain and under ThreadSanitizer, against six copies with one rule broken
  each, and a reading of the adapter and the frontend for every path that has to stop the sender.
- `tools/test_zink_subdata_stage.py` (3.0.16): the decision over every case of "Buffer uploads", the code around it,
  the switch, and eight broken copies, each of which has to fail. `tools/test_nvk_zcull.py` (3.0.17): the patch's
  place in the chain, each of RM's figures in its field, and the decision compiled out of the patch over 20 cases.
- The frontend with the queue (`nvk_zink.c`, `nvk_present.c`, `nvk_present_queue.c`, `nvk_iosurface.c`) in the
  darwin cross-build on Linux, 0 warnings, and `nvk_cgl.c`, `appkit.m` and `native_drawable.m` with `-fsyntax-only`
  (the last with the 11.3 SDK's older name standing in for `kIOMainPortDefault`).
  The NVK patch `nvk-surface-keep` applies at the end of the chain without fuzz; NVK itself was not compiled here.
- Not checked: anything on the two recorded builds, 24H32 and 25G241 (the fork has run only 26.7.3, and macOS 15 not
  at all); on any build, when CGL stores `rend` relative to gldCreateContext, what `rend` holds afterwards and how CGL
  allocates the context (25G342 refused at the earlier `rend == engine` check before 3.0.3); where the saved dispatch
  table, the CGL private object and its engine list lie (on 26.7.3 the guard accepts the saved table and
  `integer_dispatch_slot` runs in every process Zink serves, but no reading of where they were is recorded); and the
  VM tags malloc gives its regions on macOS 26.7.3.
