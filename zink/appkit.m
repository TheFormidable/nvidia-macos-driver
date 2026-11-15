/* SPDX-License-Identifier: MIT */
#import <Cocoa/Cocoa.h>
#import <objc/runtime.h>
#import <dispatch/dispatch.h>
#import <CoreVideo/CoreVideo.h>
#include <dlfcn.h>
#include <pthread.h>
#include <string.h>
#include "native_drawable.h"

/* LWJGL 2 invokes these view operations from its render thread. AppKit may
 * simultaneously build the same layer tree on main, causing blank windows or
 * a CALayerArray mutation exception. Keep GL current-context and swap calls on
 * the render thread; only the Cocoa view operations are marshalled here. */
static void (*original_set_view)(id, SEL, NSView *);
static void (*original_update)(id, SEL);
static void (*original_clear)(id, SEL);
static pthread_once_t installed = PTHREAD_ONCE_INIT;
void nvk_cgl_lwjgl_drawable_size(CGLContextObj, unsigned, unsigned);

/* OBS 33 requests RGBA16F screen streams on macOS 15+, which can fail before
 * producing any frames on this driver. Its source handles BGRA8 dynamically.
 * This default compatibility mode affects only the OBS application. */
static void (*original_capture_format)(id, SEL, OSType);
static pthread_once_t obs_policy_once = PTHREAD_ONCE_INIT;
static BOOL obs_compatibility;

static void read_obs_policy(void)
{
   if (![[NSBundle mainBundle].bundleIdentifier isEqualToString:@"com.obsproject.obs-studio"]) return;
   const char *override = getenv("NVKZINK_OBS_COMPAT");
   if (override && (!strcmp(override, "0") || !strcmp(override, "1"))) {
      obs_compatibility = !strcmp(override, "1"); return;
   }
   Boolean valid = false;
   Boolean enabled = CFPreferencesGetAppBooleanValue(CFSTR("NVKZinkCaptureCompatibility"),
      CFSTR("com.obsproject.obs-studio"), &valid);
   obs_compatibility = valid ? enabled : YES;
}

int nvk_cgl_obs_compatibility_enabled(void)
{
   pthread_once(&obs_policy_once, read_obs_policy);
   return obs_compatibility;
}

static void obs_capture_format(id configuration, SEL selector, OSType format)
{
   original_capture_format(configuration, selector,
      format == kCVPixelFormatType_64RGBAHalf ? kCVPixelFormatType_32BGRA : format);
}

static void install_obs_capture_compatibility(void)
{
   if (!nvk_cgl_obs_compatibility_enabled()) return;
   void *framework = dlopen("/System/Library/Frameworks/ScreenCaptureKit.framework/ScreenCaptureKit", RTLD_NOW | RTLD_LOCAL);
   if (!framework) { fprintf(stderr,"NVK Zink: OBS capture compatibility could not load ScreenCaptureKit\n"); return; }
   Class cls = NSClassFromString(@"SCStreamConfiguration");
   SEL selector = NSSelectorFromString(@"setPixelFormat:");
   Method method = class_getInstanceMethod(cls, selector);
   NSMethodSignature *signature = method ? [NSMethodSignature signatureWithObjCTypes:method_getTypeEncoding(method)] : nil;
   if (signature.numberOfArguments != 3 || strcmp(signature.methodReturnType, @encode(void)) ||
       strcmp([signature getArgumentTypeAtIndex:2], @encode(OSType))) {
      fprintf(stderr,"NVK Zink: OBS capture compatibility found an unsupported pixel-format setter\n"); return;
   }
   Dl_info image = {0};
   if (!dladdr((void *)install_obs_capture_compatibility, &image) || !image.dli_fname ||
       !dlopen(image.dli_fname, RTLD_LAZY | RTLD_LOCAL | RTLD_NODELETE)) return;
   original_capture_format = (void *)method_getImplementation(method);
   method_setImplementation(method, (IMP)obs_capture_format);
   fprintf(stderr,"NVK Zink: OBS compatibility uses BGRA8 ScreenCaptureKit frames\n");
}

static void publish_view_size(NSOpenGLContext *context)
{
   NSView *view = context.view;
   nvk_native_bind_view(context.CGLContextObj, (__bridge void *)view);
   if (!view) return;
   NSRect rect = view.bounds;
   if (view.wantsBestResolutionOpenGLSurface) rect = [view convertRectToBacking:rect];
   if (rect.size.width > 0 && rect.size.height > 0 &&
       rect.size.width <= INT_MAX && rect.size.height <= INT_MAX)
      nvk_cgl_lwjgl_drawable_size(context.CGLContextObj,
                                  (unsigned)rect.size.width, (unsigned)rect.size.height);
}

static BOOL lwjgl_background_caller(void *caller)
{
   if ([NSThread isMainThread]) return NO;
   Dl_info info = {0};
   if (!dladdr(caller, &info) || !info.dli_fname) return NO;
   const char *name = strrchr(info.dli_fname, '/');
   return !strcmp(name ? name + 1 : info.dli_fname, "liblwjgl.dylib");
}

static void lwjgl_set_view(id context, SEL selector, NSView *view)
{
   if (!lwjgl_background_caller(__builtin_return_address(0))) {
      original_set_view(context, selector, view);
      if ([NSThread isMainThread]) publish_view_size(context);
      return;
   }
   dispatch_sync(dispatch_get_main_queue(), ^{
      @autoreleasepool { original_set_view(context, selector, view); publish_view_size(context); }
   });
}

static void lwjgl_update(id context, SEL selector)
{
   if (!lwjgl_background_caller(__builtin_return_address(0))) {
      original_update(context, selector);
      if ([NSThread isMainThread]) publish_view_size(context);
      return;
   }
   dispatch_sync(dispatch_get_main_queue(), ^{
      @autoreleasepool {
         original_update(context, selector);
         /* LWJGL's MacOSXOpenGLView is a bare NSView: it does not emit the
          * NSOpenGLView viewport update used by the other resize hooks. */
         publish_view_size(context);
      }
   });
}

static void lwjgl_clear_drawable(id context, SEL selector)
{
   if (!lwjgl_background_caller(__builtin_return_address(0))) {
      original_clear(context, selector);
      nvk_native_remove([context CGLContextObj]);
      return;
   }
   dispatch_sync(dispatch_get_main_queue(), ^{
      @autoreleasepool { original_clear(context, selector); nvk_native_remove([context CGLContextObj]); }
   });
}

static void install_hooks(void)
{
   install_obs_capture_compatibility();
   Class cls = [NSOpenGLContext class];
   Method set_view = class_getInstanceMethod(cls, @selector(setView:));
   Method update = class_getInstanceMethod(cls, @selector(update));
   Method clear = class_getInstanceMethod(cls, @selector(clearDrawable));
   if (!set_view || !update || !clear) return;

   /* Objective-C retains the IMPs after an explicit CGL client dlcloses the
    * library. Pin this image before publishing them to avoid dangling calls. */
   Dl_info image = {0};
   if (!dladdr((void *)install_hooks, &image) || !image.dli_fname ||
       !dlopen(image.dli_fname, RTLD_LAZY | RTLD_LOCAL | RTLD_NODELETE)) return;
   original_set_view = (void *)method_getImplementation(set_view);
   original_update = (void *)method_getImplementation(update);
   original_clear = (void *)method_getImplementation(clear);
   method_setImplementation(set_view, (IMP)lwjgl_set_view);
   method_setImplementation(update, (IMP)lwjgl_update);
   method_setImplementation(clear, (IMP)lwjgl_clear_drawable);
}

void nvk_cgl_install_lwjgl_appkit(void)
{
   pthread_once(&installed, install_hooks);
}
