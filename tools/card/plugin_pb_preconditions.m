/* plugin_pb_preconditions.m: what the driver's Photo Booth preview adapter needs from this macOS, checked without Photo
 * Booth, without a camera and without claiming Photo Booth's identity. The adapter (plugin/NVMTLPhotoBooth.m) installs
 * only in a process named "Photo Booth" whose main bundle is com.apple.PhotoBooth, and only when -[CAMetalLayer
 * nextDrawable] takes no argument and returns an object, and -[CIContext render:toMTLTexture:commandBuffer:bounds:
 * colorSpace:] takes three objects, a CGRect and a pointer and returns void; otherwise the real Photo Booth logs "does
 * not have the methods this adapter is written for" and its preview stays as Core Image draws it. This program opens
 * the device (class exactly NVMTLDevice), makes a CIContext on it, and checks both signatures the way the adapter does.
 * It also checks that the adapter stayed out of this process: dladdr of both methods' implementations must name an
 * image that is not the plugin (dladdr asks dyld's list of loaded images in this process; it reads no file). Both type
 * encodings and the images are printed. It cannot show that Photo Booth calls this method or that its preview is
 * upright: that needs Photo Booth with a camera, or a screen capture, and neither is allowed here.
 *   /usr/bin/xcrun clang -arch x86_64 -mmacosx-version-min=15.0 -fobjc-arc -O2 tools/card/plugin_pb_preconditions.m \
 *     -framework Foundation -framework Metal -framework QuartzCore -framework CoreImage -o <run>/bin/<id>/nvmtltest
 * It writes no file, opens no window and does no GPU work beyond the device and the context. Exit 0 pass, 1 a case
 * failed, 2 setup only, 3 skip, 70 crash. Not yet run on a Mac. */
#import "plugin_common.h"
#import <CoreImage/CoreImage.h>
#import <QuartzCore/QuartzCore.h>
#include <dlfcn.h>

// the adapter's own test of a method's shape (nvmtl_photobooth_signature), with the reason when it does not hold
static const char *shape_problem(Method method, BOOL render)
{
    if (!method) return "the class has no such method";
    const char *types = method_getTypeEncoding(method);
    NSMethodSignature *sig = types ? [NSMethodSignature signatureWithObjCTypes:types] : nil;
    if (!sig) return "the runtime gives no type encoding the signature parser reads";
    if (!render) {
        if (sig.numberOfArguments != 2) return "it does not take exactly self and _cmd";
        if (sig.methodReturnType[0] != '@') return "it does not return an object";
        return NULL;
    }
    if (sig.numberOfArguments != 7) return "it does not take five arguments";
    if (strcmp(sig.methodReturnType, @encode(void))) return "it does not return void";
    for (unsigned i = 2; i <= 4; i++)
        if ([sig getArgumentTypeAtIndex:i][0] != '@') return "one of its first three arguments is not an object";
    if (strcmp([sig getArgumentTypeAtIndex:5], @encode(CGRect))) return "its fourth argument is not a CGRect";
    if ([sig getArgumentTypeAtIndex:6][0] != '^') return "its fifth argument is not a pointer";
    return NULL;
}

static void method_case(const char *name, const char *class_name, const char *selector, BOOL render)
{
    Class cls = NSClassFromString([NSString stringWithUTF8String:class_name]);
    char shape[96], place[96];
    snprintf(shape, sizeof shape, "%s-signature", name);
    snprintf(place, sizeof place, "%s-implementation", name);
    if (!cls) {
        nm_fail(shape, "NSClassFromString(%s) is nil: the adapter would not find the class at device creation",
                class_name);
        return;
    }
    Method method = class_getInstanceMethod(cls, sel_registerName(selector));
    const char *types = method ? method_getTypeEncoding(method) : NULL;
    nm_info("%s -[%s %s] type encoding %s", name, class_name, selector, types ? types : "(none)");
    const char *problem = shape_problem(method, render);
    if (problem)
        nm_fail(shape, "-[%s %s]: %s, so the adapter would leave Photo Booth's preview as it is", class_name, selector,
                problem);
    else
        nm_pass(shape, "-[%s %s] has the shape the adapter is written for", class_name, selector);
    if (!method) return;
    Dl_info info;
    memset(&info, 0, sizeof info);
    IMP imp = method_getImplementation(method);
    if (!dladdr((const void *)imp, &info) || !info.dli_fname) {
        nm_fail(place, "dladdr finds no loaded image for the implementation of -[%s %s]", class_name, selector);
        return;
    }
    nm_info("%s implementation in %s", name, info.dli_fname);
    const size_t n = strlen(info.dli_fname);
    if (n >= 12 && !strcmp(info.dli_fname + n - 12, "/NVMTLDriver"))
        nm_fail(place, "the driver replaced -[%s %s] in a process that is not Photo Booth", class_name, selector);
    else
        nm_pass(place, "-[%s %s] is the system's own here", class_name, selector);
}

int main(void)
{
    @autoreleasepool {
        nm_start(20);
        id<MTLDevice> device = nm_device();
        NSString *bundle = [NSBundle mainBundle].bundleIdentifier;
        nm_info("process %s, main bundle %s: the adapter installs only in Photo Booth (com.apple.PhotoBooth)",
                getprogname(), bundle ? bundle.UTF8String : "(none)");
        CIContext *context = [CIContext contextWithMTLDevice:device];
        if (context)
            nm_pass("core-image-context", "Core Image makes a context on this driver's device");
        else
            nm_fail("core-image-context", "+[CIContext contextWithMTLDevice:] gave nil");
        method_case("nextDrawable", "CAMetalLayer", "nextDrawable", NO);
        method_case("render", "CIContext", "render:toMTLTexture:commandBuffer:bounds:colorSpace:", YES);
        return nm_finish();
    }
}
