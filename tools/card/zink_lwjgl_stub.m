/* A stand-in for LWJGL's native library, built as liblwjgl.dylib for the zink.win-lwjgl check of
 * tools/card/checks_zink.py. The adapter's AppKit hooks run -[NSOpenGLContext setView:], -update and -clearDrawable on
 * main when they are called off main from an image whose file is named liblwjgl.dylib: they look at the image of
 * their return address (zink/appkit.m:79-127). So each message is sent from here, from a function that is never
 * inlined and makes no tail call, so that the return address the hook sees lies in this image. zink_win_check.m
 * dlopens this library by its path and calls the three functions from a render thread while main runs its run loop.
 * It has run once, on one Mac, on macOS 26.7.3 (25G342) with release 3.0.1's package. */
#import <AppKit/AppKit.h>

#define ZINK_LWJGL_EXPORT __attribute__((visibility("default"), noinline, disable_tail_calls))

ZINK_LWJGL_EXPORT int zink_lwjgl_set_view(void *context, void *view)
{
   [(__bridge NSOpenGLContext *)context setView:(__bridge NSView *)view];
   return 1;
}

ZINK_LWJGL_EXPORT int zink_lwjgl_update(void *context)
{
   [(__bridge NSOpenGLContext *)context update];
   return 1;
}

ZINK_LWJGL_EXPORT int zink_lwjgl_clear_drawable(void *context)
{
   [(__bridge NSOpenGLContext *)context clearDrawable];
   return 1;
}
