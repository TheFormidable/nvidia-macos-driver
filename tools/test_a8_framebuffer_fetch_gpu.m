/* Framebuffer fetch of an A8 colour attachment on the installed Metal driver: a plain A8Unorm texture, and an A8Unorm
 * view of an R8Unorm texture. Mac only, with the NullMoth driver installed and its card as the system's Metal device. It
 * installs nothing and writes no file: each case clears a 38x38 attachment to alpha 0.1 and draws four frames whose
 * fragment returns the alpha it reads through [[color(0)]] plus 0.2, so the pixels must read 77, 128, 179, 230 (out of
 * 255, within 1) after the frames. A read that saw (r,0,0,1) instead of Metal's (0,0,0,r) gives 255 at once.
 *   xcrun clang -fobjc-arc -O2 tools/test_a8_framebuffer_fetch_gpu.m -framework Foundation -framework Metal \
 *     -o /tmp/a8-fetch && /tmp/a8-fetch
 * Exit 0: every frame read as expected; 1: a frame did not (its line says errors=N); 2: no device of this driver, or a
 * setup step failed (the line before says which). Framebuffer fetch is on in applications (NVMTL_NO_FBFETCH=1 turns it
 * off, and then the library does not compile). From the HighDelay/nvidia-macos-driver-nvk fork, which reports it passing
 * on an RTX 2060 SUPER, macOS 26.7.1; here it loads the installed driver through MTLCreateSystemDefaultDevice instead of
 * a plugin path, as an application, not as WindowServer. */
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
        "vertex float4 v(uint i [[vertex_id]]) { float2 p = float2((i << 1) & 2, i & 2); return float4(p * 2. - 1., 0, 1); }\n"
        "fragment float4 f(float4 previous [[color(0)]]) { return float4(0, 0, 0, previous.a + 0.2f); }\n" options:nil error:&error];
    if (!library) fprintf(stderr, "%s\n", error.description.UTF8String);
    require(library != nil, "library");
    id<MTLCommandQueue> queue = [device newCommandQueue]; unsigned failures = 0;
    for (unsigned view = 0; view < 2; view++) { @autoreleasepool {
        MTLTextureDescriptor *descriptor = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:view ? MTLPixelFormatR8Unorm : MTLPixelFormatA8Unorm
                                                                                             width:38 height:38 mipmapped:NO];
        descriptor.storageMode = MTLStorageModePrivate;
        descriptor.usage = MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite | MTLTextureUsageRenderTarget | MTLTextureUsagePixelFormatView;
        id<MTLTexture> backing = [device newTextureWithDescriptor:descriptor];
        id<MTLTexture> texture = view ? [backing newTextureViewWithPixelFormat:MTLPixelFormatA8Unorm] : backing;
        require(texture != nil, "alpha texture");
        MTLRenderPipelineDescriptor *pd = [MTLRenderPipelineDescriptor new];
        pd.vertexFunction = [library newFunctionWithName:@"v"]; pd.fragmentFunction = [library newFunctionWithName:@"f"];
        pd.colorAttachments[0].pixelFormat = MTLPixelFormatA8Unorm; pd.colorAttachments[0].writeMask = MTLColorWriteMaskAlpha;
        id<MTLRenderPipelineState> pipeline = [device newRenderPipelineStateWithDescriptor:pd error:&error];
        if (!pipeline) fprintf(stderr, "%s\n", error.description.UTF8String);
        require(pipeline != nil, "alpha pipeline");
        id<MTLBuffer> output = [device newBufferWithLength:256 * 38 options:MTLResourceStorageModeShared];
        require(output != nil, "readback buffer");
        for (unsigned frame = 0; frame < 4; frame++) {
            id<MTLCommandBuffer> command = [queue commandBuffer];
            MTLRenderPassDescriptor *pass = [MTLRenderPassDescriptor renderPassDescriptor];
            pass.colorAttachments[0].texture = texture; pass.colorAttachments[0].loadAction = frame ? MTLLoadActionLoad : MTLLoadActionClear;
            pass.colorAttachments[0].storeAction = MTLStoreActionStore; pass.colorAttachments[0].clearColor = MTLClearColorMake(0, 0, 0, .1);
            id<MTLRenderCommandEncoder> encoder = [command renderCommandEncoderWithDescriptor:pass];
            [encoder setRenderPipelineState:pipeline]; [encoder drawPrimitives:MTLPrimitiveTypeTriangle vertexStart:0 vertexCount:3]; [encoder endEncoding];
            id<MTLBlitCommandEncoder> blit = [command blitCommandEncoder];
            [blit copyFromTexture:texture sourceSlice:0 sourceLevel:0 sourceOrigin:MTLOriginMake(0, 0, 0) sourceSize:MTLSizeMake(38, 38, 1)
                         toBuffer:output destinationOffset:0 destinationBytesPerRow:256 destinationBytesPerImage:256 * 38];
            [blit endEncoding]; [command commit]; [command waitUntilCompleted];
            require(command.status == MTLCommandBufferStatusCompleted, "command buffer did not complete");
            const unsigned expected = 77 + 51 * frame; unsigned bad = 0; const unsigned char *pixels = output.contents;
            for (unsigned y = 0; y < 38; y++) for (unsigned x = 0; x < 38; x++) {
                const int delta = (int)pixels[y * 256 + x] - (int)expected; if (delta < -1 || delta > 1) bad++; }
            printf("view=%u frame=%u first=%u expected=%u errors=%u\n", view, frame, pixels[0], expected, bad); failures += bad;
        }
    }}
    printf("%s: A8 framebuffer fetch, %u wrong pixels\n", failures ? "FAIL" : "PASS", failures);
    return failures ? 1 : 0;
}}
