/* Shader writes into textures whose usage is Unknown, on the installed Metal driver, next to the same writes into
 * textures that ask for ShaderWrite. Mac only, with the NullMoth driver installed and its card as the system's Metal
 * device. It installs nothing and writes no file: a kernel fills a 32x32 RGBA16Float texture (one level or a full chain;
 * the texture itself or a view of level 2) with (0.25, 0.5, 0.75, 1.0), and every texel is read back.
 *   xcrun clang -fobjc-arc -O2 tools/test_unknown_texture_usage_gpu.m -framework Foundation -framework Metal \
 *     -o /tmp/unknown-usage && /tmp/unknown-usage
 * Exit 0: every texel as written; 1: a case lost writes (its line says errors=N); 2: no device of this driver, or a
 * setup step failed. From the HighDelay/nvidia-macos-driver-nvk fork, which reports it passing on an RTX 2060 SUPER,
 * macOS 26.7.1; here it loads the installed driver through MTLCreateSystemDefaultDevice instead of a plugin path, as an
 * application, not as WindowServer. */
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
static void require(BOOL ok, const char *what) { if (!ok) { fprintf(stderr, "setup: %s\n", what); exit(2); } }
int main(void) { @autoreleasepool {
    alarm(60); setvbuf(stdout, NULL, _IOLBF, 0);
    id<MTLDevice> device = MTLCreateSystemDefaultDevice();
    if (!device || ![NSStringFromClass([device class]) hasPrefix:@"NVMTL"]) {
        fprintf(stderr, "no Metal device of this driver (got %s)\n", device ? NSStringFromClass([device class]).UTF8String : "none");
        return 2;
    }
    NSError *error = nil;
    id<MTLLibrary> library = [device newLibraryWithSource:@"#include <metal_stdlib>\nusing namespace metal;\n"
        "kernel void fill(texture2d<half, access::write> dst [[texture(0)]], uint2 p [[thread_position_in_grid]]) {"
        " if (p.x < dst.get_width() && p.y < dst.get_height()) dst.write(half4(.25h, .5h, .75h, 1.h), p); }\n" options:nil error:&error];
    if (!library) fprintf(stderr, "%s\n", error.description.UTF8String);
    require(library != nil, "library");
    id<MTLComputePipelineState> pipeline = [device newComputePipelineStateWithFunction:[library newFunctionWithName:@"fill"] error:&error];
    require(pipeline != nil, "pipeline");
    id<MTLCommandQueue> queue = [device newCommandQueue]; unsigned failures = 0;
    for (unsigned known = 0; known < 2; known++) for (unsigned mip = 0; mip < 2; mip++) for (unsigned view = 0; view < 2; view++) { @autoreleasepool {
        MTLTextureDescriptor *d = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA16Float width:32 height:32 mipmapped:mip];
        d.storageMode = MTLStorageModePrivate;
        d.usage = known ? (MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite | MTLTextureUsagePixelFormatView) : MTLTextureUsageUnknown;
        id<MTLTexture> base = [device newTextureWithDescriptor:d];
        id<MTLTexture> target = view ? [base newTextureViewWithPixelFormat:MTLPixelFormatRGBA16Float textureType:MTLTextureType2D
                                                                     levels:NSMakeRange(mip ? 2 : 0, 1) slices:NSMakeRange(0, 1)] : base;
        require(target != nil, "texture or view");
        id<MTLBuffer> output = [device newBufferWithLength:256 * 32 options:MTLResourceStorageModeShared];
        require(output != nil, "readback buffer");
        id<MTLCommandBuffer> cb = [queue commandBuffer]; id<MTLComputeCommandEncoder> encoder = [cb computeCommandEncoder];
        [encoder setComputePipelineState:pipeline]; [encoder setTexture:target atIndex:0];
        [encoder dispatchThreads:MTLSizeMake(target.width, target.height, 1) threadsPerThreadgroup:MTLSizeMake(8, 8, 1)]; [encoder endEncoding];
        id<MTLBlitCommandEncoder> blit = [cb blitCommandEncoder];
        [blit copyFromTexture:target sourceSlice:0 sourceLevel:0 sourceOrigin:MTLOriginMake(0, 0, 0) sourceSize:MTLSizeMake(target.width, target.height, 1)
                     toBuffer:output destinationOffset:0 destinationBytesPerRow:256 destinationBytesPerImage:256 * target.height];
        [blit endEncoding]; [cb commit]; [cb waitUntilCompleted];
        require(cb.status == MTLCommandBufferStatusCompleted, "command buffer did not complete");
        const float expected[] = { .25f, .5f, .75f, 1.f }; unsigned bad = 0;
        for (NSUInteger y = 0; y < target.height; y++) {
            const _Float16 *row = (const _Float16 *)((const uint8_t *)output.contents + y * 256);
            for (NSUInteger x = 0; x < target.width; x++) for (unsigned c = 0; c < 4; c++) bad += (float)row[x * 4 + c] != expected[c];
        }
        printf("usage=%s mips=%s target=%s errors=%u\n", known ? "ShaderWrite" : "Unknown", mip ? "chain" : "one", view ? "view" : "texture", bad);
        failures += bad;
    }}
    printf("%s: shader writes through Unknown and ShaderWrite usage, %u wrong texels\n", failures ? "FAIL" : "PASS", failures);
    return failures ? 1 : 0;
}}
