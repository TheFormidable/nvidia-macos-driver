/* plugin_pb_orientation.m: Core Image's legacy render, -[CIContext
 * render:toMTLTexture:commandBuffer:bounds:colorSpace:] (the call Photo Booth's preview uses), into a drawable of this
 * driver's CAMetalLayer in an ordinary process, against the same render into an offscreen texture. The layer belongs to
 * no window: device is this driver's, BGRA8Unorm, framebufferOnly NO, 64x64. One drawable is taken with nextDrawable
 * and released without being presented. The image is 64x64, red in its upper half and green in its lower half (Core
 * Image's origin is the lower left). Outside Photo Booth the driver's adapter is not installed, so the drawable's bytes
 * must equal the offscreen texture's (within 1 per channel). Which colour the texture's first row holds in each is
 * printed as information only: the source fork says this call writes the image's rows bottom first into drawables,
 * which is what the adapter undoes inside Photo Booth. When nextDrawable gives nothing, or a texture that is not one of
 * this driver's, the program says so and skips.
 *   /usr/bin/xcrun clang -arch x86_64 -mmacosx-version-min=15.0 -fobjc-arc -O2 tools/card/plugin_pb_orientation.m \
 *     -framework Foundation -framework Metal -framework QuartzCore -framework CoreImage -framework CoreGraphics \
 *     -o <run>/bin/<id>/nvmtltest
 * It opens no window and no camera and writes no file. Exit 0 pass, 1 a case failed, 2 setup only, 3 skip, 70 crash.
 * Not yet run on a Mac. */
#import "plugin_common.h"
#import <CoreGraphics/CoreGraphics.h>
#import <CoreImage/CoreImage.h>
#import <QuartzCore/QuartzCore.h>
#include <stdlib.h>

enum { N = 64, ROW = N * 4 };

// "red", "green" or "other" for the first pixel of row y of a BGRA8 image, when the whole row is that colour
static const char *row_colour(const uint8_t *bytes, unsigned y)
{
    const uint8_t *p = bytes + y * ROW;
    const char *name = p[2] > 200 && p[1] < 50 && p[0] < 50   ? "red"
                       : p[1] > 200 && p[2] < 50 && p[0] < 50 ? "green"
                                                              : "other";
    for (unsigned x = 1; x < N; x++)
        if (memcmp(p + x * 4, p, 4)) return "mixed";
    return name;
}

int main(void)
{
    @autoreleasepool {
        nm_start(30);
        id<MTLDevice> device = nm_device();
        id<MTLCommandQueue> queue = [device newCommandQueue];
        CIContext *context = [CIContext contextWithMTLDevice:device
                                                     options:@{kCIContextWorkingColorSpace : [NSNull null]}];
        CAMetalLayer *layer = [CAMetalLayer layer];
        layer.device = device;
        layer.pixelFormat = MTLPixelFormatBGRA8Unorm;
        layer.framebufferOnly = NO;
        layer.drawableSize = CGSizeMake(N, N);
        id<CAMetalDrawable> drawable = [layer nextDrawable];
        if (!drawable) nm_skip("nextDrawable gave no drawable for a CAMetalLayer outside any window");
        id<MTLTexture> target = drawable.texture;
        Class ours = NSClassFromString(@"NVMTLTexture");
        if (!target || !ours || ![(id)target isKindOfClass:ours])
            nm_skip("the drawable's texture is %s, not one of this driver's",
                    target ? object_getClassName(target) : "nil");
        if (!context) {
            nm_setup("core-image", "+[CIContext contextWithMTLDevice:options:] gave nil");
            return nm_finish();
        }
        nm_info("drawable texture %lux%lu usage=0x%lx", (unsigned long)target.width, (unsigned long)target.height,
                (unsigned long)target.usage);

        MTLTextureDescriptor *d = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatBGRA8Unorm
                                                                                     width:N
                                                                                    height:N
                                                                                 mipmapped:NO];
        d.storageMode = MTLStorageModePrivate;
        d.usage = MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite | MTLTextureUsageRenderTarget;
        id<MTLTexture> offscreen = [device newTextureWithDescriptor:d];
        id<MTLBuffer> drawn = [device newBufferWithLength:ROW * N options:MTLResourceStorageModeShared];
        id<MTLBuffer> plain = [device newBufferWithLength:ROW * N options:MTLResourceStorageModeShared];
        if (!offscreen || !drawn || !plain) {
            nm_setup("objects", "offscreen texture or readback buffers");
            return nm_finish();
        }
        memset(drawn.contents, 0x33, ROW * N);
        memset(plain.contents, 0x77, ROW * N);

        CIImage *upper =
            [[CIImage imageWithColor:[CIColor colorWithRed:1 green:0 blue:0
                                                     alpha:1]] imageByCroppingToRect:CGRectMake(0, N / 2, N, N / 2)];
        CIImage *lower =
            [[CIImage imageWithColor:[CIColor colorWithRed:0 green:1 blue:0
                                                     alpha:1]] imageByCroppingToRect:CGRectMake(0, 0, N, N / 2)];
        CIImage *image = [upper imageByCompositingOverImage:lower];
        CGColorSpaceRef space = CGColorSpaceCreateDeviceRGB();
        id<MTLCommandBuffer> cb = [queue commandBuffer];
        [context render:image toMTLTexture:target commandBuffer:cb bounds:CGRectMake(0, 0, N, N) colorSpace:space];
        [context render:image toMTLTexture:offscreen commandBuffer:cb bounds:CGRectMake(0, 0, N, N) colorSpace:space];
        id<MTLBlitCommandEncoder> blit = [cb blitCommandEncoder];
        [blit copyFromTexture:target
                         sourceSlice:0
                         sourceLevel:0
                        sourceOrigin:MTLOriginMake(0, 0, 0)
                          sourceSize:MTLSizeMake(N, N, 1)
                            toBuffer:drawn
                   destinationOffset:0
              destinationBytesPerRow:ROW
            destinationBytesPerImage:ROW * N];
        [blit copyFromTexture:offscreen
                         sourceSlice:0
                         sourceLevel:0
                        sourceOrigin:MTLOriginMake(0, 0, 0)
                          sourceSize:MTLSizeMake(N, N, 1)
                            toBuffer:plain
                   destinationOffset:0
              destinationBytesPerRow:ROW
            destinationBytesPerImage:ROW * N];
        [blit endEncoding];
        const BOOL ran = nm_run(cb, "render");
        CGColorSpaceRelease(space);
        if (ran) {
            const uint8_t *a = drawn.contents, *b = plain.contents;
            unsigned bad = 0, first = 0;
            for (unsigned i = 0; i < ROW * N; i++)
                if (abs((int)a[i] - (int)b[i]) > 1 && !bad++) first = i;
            nm_info("drawable: first row %s, last row %s; offscreen: first row %s, last row %s", row_colour(a, 0),
                    row_colour(a, N - 1), row_colour(b, 0), row_colour(b, N - 1));
            if (!bad)
                nm_pass("drawable-equals-offscreen", "%u bytes alike: no reflection outside Photo Booth", ROW * N);
            else
                nm_fail("drawable-equals-offscreen", "%u of %u bytes differ, first at pixel (%u,%u) got=%u expected=%u",
                        bad, ROW * N, (first % ROW) / 4, first / ROW, a[first], b[first]);
        }
        drawable = nil; // released, never presented
        return nm_finish();
    }
}
