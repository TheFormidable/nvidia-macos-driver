/* The levels Core Animation leaves unwritten in a mipmapped half-float texture, on the installed Metal driver. Mac only,
 * with the NullMoth driver installed and its card as the system's Metal device. It installs nothing and writes no file:
 * for R16Float, RG16Float and RGBA16Float, at 128x128 (a 2x2 checker around -0.5, 0.25, 1.5, 1.0) and at 63x31 (flat),
 * it makes the texture the way Core Animation does (-newTiledTextureWithDescriptor:offset:bytesPerRow: on a buffer,
 * level 0 only) and reads every level back: each level below 0 must hold the average, extended-range values included,
 * within 0.002. A level the application writes itself (0.375 at level 1, then level 0 again) must keep what it wrote.
 *   xcrun clang -fobjc-arc -O2 tools/test_ca_half_float_mips_gpu.m -framework Foundation -framework Metal \
 *     -o /tmp/half-mips && /tmp/half-mips
 * Exit 0: every level as expected; 1: a level was not (its line says errors=N); 2: no device of this driver, or a setup
 * step failed. From the HighDelay/nvidia-macos-driver-nvk fork, which reports it passing on an RTX 2060 SUPER, macOS
 * 26.7.1; here it loads the installed driver through MTLCreateSystemDefaultDevice instead of a plugin path, as an
 * application, not as WindowServer. */
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
@interface NSObject (ClientStorageTexture)
- (id<MTLTexture>)newTiledTextureWithDescriptor:(MTLTextureDescriptor *)d offset:(NSUInteger)offset bytesPerRow:(NSUInteger)row;
@end
static void require(BOOL ok, const char *what) { if (!ok) { fprintf(stderr, "setup: %s\n", what); exit(2); } }
static unsigned check(id<MTLTexture> t, NSUInteger level, unsigned channels, const float value[4]) {
    NSUInteger w = t.width >> level ? t.width >> level : 1, h = t.height >> level ? t.height >> level : 1;
    NSMutableData *data = [NSMutableData dataWithLength:w * h * channels * 2];
    [t getBytes:data.mutableBytes bytesPerRow:w * channels * 2 fromRegion:MTLRegionMake2D(0, 0, w, h) mipmapLevel:level];
    const _Float16 *p = data.bytes; unsigned bad = 0;
    for (NSUInteger y = 0; y < h; y++) for (NSUInteger x = 0; x < w; x++) for (unsigned c = 0; c < channels; c++) {
        // the 128-pixel source is a 2x2 checker; every level below it averages to value
        float expected = value[c]; if (!level && t.width == 128) expected += .125f * ((x & 1 ? 1 : -1) + (y & 1 ? 1 : -1));
        const float actual = (float)p[(y * w + x) * channels + c]; if (!isfinite(actual) || fabsf(actual - expected) > .002f) bad++;
    }
    printf("format=%lu size=%lux%lu level=%lu first=%g expected=%g errors=%u\n", (unsigned long)t.pixelFormat, (unsigned long)t.width,
           (unsigned long)t.height, (unsigned long)level, (float)p[0], value[0], bad);
    return bad;
}
int main(void) { @autoreleasepool {
    alarm(60); setvbuf(stdout, NULL, _IOLBF, 0);
    id<MTLDevice> device = MTLCreateSystemDefaultDevice();
    if (!device || ![NSStringFromClass([device class]) hasPrefix:@"NVMTL"]) {
        fprintf(stderr, "no Metal device of this driver (got %s)\n", device ? NSStringFromClass([device class]).UTF8String : "none");
        return 2;
    }
    const MTLPixelFormat formats[] = { MTLPixelFormatR16Float, MTLPixelFormatRG16Float, MTLPixelFormatRGBA16Float };
    const unsigned components[] = { 1, 2, 4 }; unsigned failures = 0;
    for (unsigned f = 0; f < 3; f++) for (unsigned odd = 0; odd < 2; odd++) { @autoreleasepool {
        const unsigned channels = components[f];
        const NSUInteger w = odd ? 63 : 128, h = odd ? 31 : 128, row = (w * channels * 2 + 63) & ~(NSUInteger)63, offset = 16;
        NSMutableData *data = [NSMutableData dataWithLength:offset + row * h];
        const float values[4] = { -.5f, .25f, 1.5f, 1.f };
        for (NSUInteger y = 0; y < h; y++) for (NSUInteger x = 0; x < w; x++) for (unsigned c = 0; c < channels; c++) {
            float v = values[c]; if (!odd) v += .125f * ((x & 1 ? 1 : -1) + (y & 1 ? 1 : -1));
            *((_Float16 *)((uint8_t *)data.mutableBytes + offset + y * row) + x * channels + c) = (_Float16)v;
        }
        id<MTLBuffer> b = [device newBufferWithBytes:data.bytes length:data.length options:MTLResourceStorageModeShared];
        require(b != nil, "buffer");
        MTLTextureDescriptor *d = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:formats[f] width:w height:h mipmapped:YES];
        d.storageMode = MTLStorageModeShared; d.usage = MTLTextureUsageShaderRead;
        require([(id)b respondsToSelector:@selector(newTiledTextureWithDescriptor:offset:bytesPerRow:)], "the buffer has no tiled-texture method");
        id<MTLTexture> t = [(id)b newTiledTextureWithDescriptor:d offset:offset bytesPerRow:row];
        require(t != nil, "tiled texture");
        for (NSUInteger l = 0; l < t.mipmapLevelCount; l++) failures += check(t, l, channels, values);
        // a level the application writes ends the filling: it keeps its own pixels after level 0 is written again
        const NSUInteger mw = w >> 1 ? w >> 1 : 1, mh = h >> 1 ? h >> 1 : 1;
        NSMutableData *custom = [NSMutableData dataWithLength:mw * mh * channels * 2]; _Float16 *cp = custom.mutableBytes;
        const float customValues[4] = { .375f, .375f, .375f, .375f };
        for (NSUInteger i = 0; i < mw * mh * channels; i++) cp[i] = (_Float16).375f;
        [t replaceRegion:MTLRegionMake2D(0, 0, mw, mh) mipmapLevel:1 withBytes:custom.bytes bytesPerRow:mw * channels * 2];
        [t replaceRegion:MTLRegionMake2D(0, 0, w, h) mipmapLevel:0 withBytes:(uint8_t *)data.bytes + offset bytesPerRow:row];
        failures += check(t, 1, channels, customValues);
    }}
    printf("%s: half-float mip levels, %u wrong values\n", failures ? "FAIL" : "PASS", failures);
    return failures ? 1 : 0;
}}
