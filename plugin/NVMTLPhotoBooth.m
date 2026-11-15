/*
 * NullMoth NVIDIA driver for macOS
 * Based on the HighDelay/nvidia-macos-driver-nvk fork's Photo Booth preview adapter.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

// The source fork reports Photo Booth's preview upside down on its driver (RTX 2060 SUPER, macOS 26.7.1), and upright
// with this adapter. Photo Booth's legacy Core Image call, -[CIContext render:toMTLTexture:commandBuffer:bounds:
// colorSpace:], writes the image's rows bottom first into the texture of its CAMetalLayer drawable, and the layer shows
// that texture's rows top first. This adapter reflects the image within the bounds it is rendered to, and only when the
// destination is a drawable this driver handed out in Photo Booth itself (com.apple.PhotoBooth): camera buffers,
// offscreen textures, saved photos, other processes and Core Image's own flipped semantics keep their paths. It
// replaces two methods of the process's classes, so it checks their type signatures first, adds its implementation to
// the class itself rather than changing an inherited one, and keeps this image loaded for as long as the process runs
// (the runtime holds the replacements).
//
// NVMTL_PHOTOBOOTH_FLIP=0 in the settings file leaves Photo Booth's preview as Core Image draws it.
//
// Of Core Graphics only CGRect is used, and Core Image's header is not imported (the plugin links neither): a
// CGAffineTransform is six CGFloats (double on every Mac this driver runs on), a CGColorSpaceRef is a pointer, and a
// CIImage is reached through the runtime.
#include <CoreGraphics/CoreGraphics.h>
#include <dlfcn.h>
#include <math.h>

typedef struct { double a, b, c, d, tx, ty; } nvmtl_photobooth_affine;   // the layout of CGAffineTransform
static char nvmtl_photobooth_drawable_key;
static id (*nvmtl_photobooth_next)(id, SEL);
static void (*nvmtl_photobooth_render)(id, SEL, id, id, id, CGRect, void *);

static BOOL nvmtl_photobooth_process(const char *name, NSString *bundle) {
    return name && !strcmp(name, "Photo Booth") && [bundle isEqualToString:@"com.apple.PhotoBooth"];
}

// -[CAMetalLayer nextDrawable]: marks the texture of a drawable that is one of this driver's textures.
static id nvmtl_photobooth_next_drawable(id layer, SEL selector) {
    id drawable = nvmtl_photobooth_next(layer, selector);
    SEL texture_sel = sel_registerName("texture");
    id texture = drawable && [drawable respondsToSelector:texture_sel] ? ((id (*)(id, SEL))objc_msgSend)(drawable, texture_sel) : nil;
    if ([texture isKindOfClass:[NVMTLTexture class]])
        objc_setAssociatedObject(texture, &nvmtl_photobooth_drawable_key, @YES, OBJC_ASSOCIATION_RETAIN_NONATOMIC);
    return drawable;
}

// The reflection of the rows within bounds (y -> 2 * origin.y + height - y), for a marked texture and finite, non-empty
// bounds; NO otherwise.
static BOOL nvmtl_photobooth_reflection(id texture, CGRect bounds, nvmtl_photobooth_affine *transform) {
    if (!texture || !objc_getAssociatedObject(texture, &nvmtl_photobooth_drawable_key) ||
        !isfinite(bounds.origin.x) || !isfinite(bounds.origin.y) ||
        !isfinite(bounds.size.width) || !isfinite(bounds.size.height) ||
        bounds.size.width <= 0 || bounds.size.height <= 0) return NO;
    const double y = 2 * bounds.origin.y + bounds.size.height;
    if (!isfinite(y)) return NO;
    *transform = (nvmtl_photobooth_affine){ 1, 0, 0, -1, 0, y };
    return YES;
}

// -[CIContext render:toMTLTexture:commandBuffer:bounds:colorSpace:]: the same call, with the image reflected when the
// destination is a marked drawable.
static void nvmtl_photobooth_render_preview(id context, SEL selector, id image, id texture, id command, CGRect bounds,
                                            void *colorSpace) {
    nvmtl_photobooth_affine transform;
    SEL apply = sel_registerName("imageByApplyingTransform:");
    if (image && nvmtl_photobooth_reflection(texture, bounds, &transform) && [image respondsToSelector:apply])
        image = ((id (*)(id, SEL, nvmtl_photobooth_affine))objc_msgSend)(image, apply, transform);
    nvmtl_photobooth_render(context, selector, image, texture, command, bounds, colorSpace);
}

// The method has the shape the replacement is written for: -(id)nextDrawable, or
// -(void)render:(id)toMTLTexture:(id)commandBuffer:(id)bounds:(CGRect)colorSpace:(pointer).
static BOOL nvmtl_photobooth_signature(Method method, BOOL render) {
    if (!method) return NO;
    NSMethodSignature *sig = [NSMethodSignature signatureWithObjCTypes:method_getTypeEncoding(method)];
    if (!sig) return NO;
    if (!render) return sig.numberOfArguments == 2 && sig.methodReturnType[0] == '@';
    return sig.numberOfArguments == 7 && !strcmp(sig.methodReturnType, @encode(void)) &&
        [sig getArgumentTypeAtIndex:2][0] == '@' && [sig getArgumentTypeAtIndex:3][0] == '@' &&
        [sig getArgumentTypeAtIndex:4][0] == '@' &&
        !strcmp([sig getArgumentTypeAtIndex:5], @encode(CGRect)) && [sig getArgumentTypeAtIndex:6][0] == '^';
}

// Gives cls its own implementation: a method cls inherits is added to cls, so the superclass and its other subclasses
// keep theirs.
static void nvmtl_photobooth_replace(Class cls, SEL selector, Method method, IMP replacement) {
    if (!class_addMethod(cls, selector, replacement, method_getTypeEncoding(method)))
        method_setImplementation(class_getInstanceMethod(cls, selector), replacement);
}

static BOOL nvmtl_photobooth_flip_on(void) {
    const char *e = getenv("NVMTL_PHOTOBOOTH_FLIP");
    return !(e && e[0] == '0');
}

static void nvmtl_photobooth_install(void) {
    if (!nvmtl_photobooth_process(getprogname(), [NSBundle mainBundle].bundleIdentifier)) return;
    static dispatch_once_t once;
    dispatch_once(&once, ^{
        if (!nvmtl_photobooth_flip_on()) { nvlog("Photo Booth preview: left as Core Image draws it (NVMTL_PHOTOBOOTH_FLIP=0)"); return; }
        Class layer = NSClassFromString(@"CAMetalLayer"), context = NSClassFromString(@"CIContext");
        SEL next = sel_registerName("nextDrawable");
        SEL render = sel_registerName("render:toMTLTexture:commandBuffer:bounds:colorSpace:");
        Method nextMethod = layer ? class_getInstanceMethod(layer, next) : NULL;
        Method renderMethod = context ? class_getInstanceMethod(context, render) : NULL;
        if (!nvmtl_photobooth_signature(nextMethod, NO) || !nvmtl_photobooth_signature(renderMethod, YES)) {
            nvlog("Photo Booth preview: CAMetalLayer or CIContext does not have the methods this adapter is written for - orientation left as it is");
            return;
        }
        // The runtime keeps the replacements and the association key: this image must stay loaded even if a client
        // closes its handle to the plugin.
        Dl_info info = { 0 };
        if (!dladdr((void *)nvmtl_photobooth_install, &info) || !info.dli_fname ||
            !dlopen(info.dli_fname, RTLD_LAZY | RTLD_LOCAL | RTLD_NODELETE)) {
            nvlog("Photo Booth preview: the plugin image could not be pinned - orientation left as it is");
            return;
        }
        nvmtl_photobooth_next = (id (*)(id, SEL))method_getImplementation(nextMethod);
        nvmtl_photobooth_render = (void (*)(id, SEL, id, id, id, CGRect, void *))method_getImplementation(renderMethod);
        nvmtl_photobooth_replace(layer, next, nextMethod, (IMP)nvmtl_photobooth_next_drawable);
        nvmtl_photobooth_replace(context, render, renderMethod, (IMP)nvmtl_photobooth_render_preview);
        nvlog("Photo Booth preview: the image is reflected into this driver's drawables (NVMTL_PHOTOBOOTH_FLIP=0 leaves it)");
    });
}
