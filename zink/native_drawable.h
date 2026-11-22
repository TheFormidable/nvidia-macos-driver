/* SPDX-License-Identifier: MIT */
#ifndef NVK_NATIVE_DRAWABLE_H
#define NVK_NATIVE_DRAWABLE_H
#include <OpenGL/OpenGL.h>
#include <IOSurface/IOSurface.h>
#include <stdint.h>
/* AppKit calls bind on main. Rendering acquires only published layer state;
 * it never synchronously dispatches to main or changes a view's layer tree. */
void nvk_native_bind_view(CGLContextObj, void *view);
void nvk_native_remove(CGLContextObj);
/* zink/nvk_cgl.c: the context's output changed, so its next swap asks for the drawable's opacity at once. Any thread;
 * not with the CGL adapter's lock held. */
void nvk_cgl_output_changed(CGLContextObj);
/* A missing view can become usable after AppKit attaches it. Auto mode may
 * use its GPU fallback meanwhile, then retry native presentation. */
enum { NVK_NATIVE_NO_VIEW = 3 };
/* 1 = drawable, 2 = resize/hidden/setup pending, 3 = no view yet,
 * 0 = unsupported/failed. interval is the context's swap interval, which the caller reads (zink/nvk_cgl.c): since
 * 3.0.11 a thread the context is not current on may ask, and it calls nothing of CGL's. */
int nvk_native_acquire(CGLContextObj, uint32_t vendor, uint32_t device,
                       unsigned width, unsigned height, int opaque, int interval, void **frame, IOSurfaceRef *surface);
int nvk_native_publish(void *frame);
void nvk_native_release(void *frame);
#endif
