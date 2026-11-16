/* plugin_buffer_alias_fetch.m: a buffer's linear texture as a colour attachment beside a tiled one, with framebuffer
 * fetch, and the formats a buffer's linear texture keeps, on the installed Metal driver.
 *   alias-beside-tiled  an RGBA8Unorm 128x64 texture of a Shared buffer at offset 0, rows of 512 bytes (or of the
 *                       reported linear alignment when that is larger), usage ShaderRead|RenderTarget, is colour 0;
 *                       colour 1 is a private R16Float. Three frames, each cleared, add their colour to what
 *                       [[color(0)]] reads, in the left half (the scissor); after each frame the buffer's bytes must
 *                       hold the frame's colour over the clear, within 0.006. With fetch on (any process but
 *                       WindowServer) the driver draws such a pass through a tiled stand-in of the linear image, loaded
 *                       and copied back (plugin/nvmtl_vk.c nvmtl_vk_cmd_begin_pass), so the fetch reads this pass's
 *                       clear, not the buffer as it was before; the driver's line about the stand-in is printed as
 *                       information.
 *   alias-alone         the same without colour 1 (the control: nothing tiled beside it, no stand-in)
 *   rg11b10 / rgb9e5    RG11B10Float and RGB9E5Float 32x32 textures of a Shared buffer (rows of 256 bytes or the
 *                       reported alignment), usage ShaderRead, filled with the encodings of (1.0, 0.5, 0.25); a kernel
 *                       reads them into an RGBA32Float texture that is read back: (1, 0.5, 0.25, 1) within 1e-3. A
 *                       linear texture takes its format features from DRM modifier 0 where NVK lists it, which keeps
 *                       these two formats accepted (plugin/nvmtl_vk.c nvmtl_vk_buffer_tiling_features).
 * A texture the buffer refuses (nil) is a failure, with the driver's line about it from its log.
 *   /usr/bin/xcrun clang -arch x86_64 -mmacosx-version-min=15.0 -fobjc-arc -O2 tools/card/plugin_buffer_alias_fetch.m \
 *     -framework Foundation -framework Metal -o <run>/bin/<id>/nvmtltest
 * It writes no file and opens no window. Exit 0 pass, 1 a case failed, 2 setup only, 3 skip, 70 crash. Not yet run on
 * a Mac. */
#import "plugin_common.h"
#include <math.h>

static id<MTLDevice> device;
static id<MTLCommandQueue> queue;

// the driver's last line about a buffer texture it refused, for the failure's text
static const char *refusal_line(void)
{
    static char text[400];
    NSString *line = nm_log_find(nm_log_lines(), "buffer newTextureWithDescriptor", NULL);
    snprintf(text, sizeof text, "%s",
             line ? line.UTF8String : "no 'buffer newTextureWithDescriptor' line in the driver's log");
    return text;
}

static NSUInteger linear_row(MTLPixelFormat format, NSUInteger least)
{
    const NSUInteger align = [device minimumLinearTextureAlignmentForPixelFormat:format];
    if (!align) return least;
    return (least + align - 1) / align * align;
}

// the left half holds the frame's colour over a transparent clear; the right half stays cleared (the scissor)
static unsigned verify(const uint8_t *pixels, size_t row, unsigned width, unsigned height, unsigned frame,
                       unsigned *first)
{
    unsigned bad = 0;
    for (unsigned y = 0; y < height; y++)
        for (unsigned x = 0; x < width; x++) {
            float expected[4] = {((x + frame) & 3) * 0.25f, ((y + frame) & 3) * 0.25f, frame * 0.25f, 1.0f};
            if (x >= width / 2) expected[0] = expected[1] = expected[2] = expected[3] = 0;
            for (unsigned c = 0; c < 4; c++)
                if (fabsf(pixels[y * row + x * 4 + c] / 255.0f - expected[c]) > 0.006f && !bad++)
                    *first = y * width + x;
        }
    return bad;
}

static void alias_case(id<MTLLibrary> library, BOOL beside, const char *name)
{
    enum { W = 128, H = 64 };
    const NSUInteger row = linear_row(MTLPixelFormatRGBA8Unorm, W * 4);
    id<MTLBuffer> buffer = [device newBufferWithLength:row * H options:MTLResourceStorageModeShared];
    if (!buffer) {
        nm_setup(name, "buffer");
        return;
    }
    memset(buffer.contents, 0x55, row * H); // a stale fetch shows as these bytes
    MTLTextureDescriptor *d = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA8Unorm
                                                                                 width:W
                                                                                height:H
                                                                             mipmapped:NO];
    d.storageMode = MTLStorageModeShared;
    d.usage = MTLTextureUsageShaderRead | MTLTextureUsageRenderTarget;
    id<MTLTexture> target = [buffer newTextureWithDescriptor:d offset:0 bytesPerRow:row];
    if (!target) {
        nm_fail(name, "refused: the buffer gave no RenderTarget texture (driver: %s)", refusal_line());
        return;
    }
    MTLTextureDescriptor *ad = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatR16Float
                                                                                  width:W
                                                                                 height:H
                                                                              mipmapped:NO];
    ad.storageMode = MTLStorageModePrivate;
    ad.usage = MTLTextureUsageRenderTarget;
    id<MTLTexture> aux = beside ? [device newTextureWithDescriptor:ad] : nil;
    if (beside && !aux) {
        nm_setup(name, "the R16Float attachment");
        return;
    }
    MTLRenderPipelineDescriptor *pd = [MTLRenderPipelineDescriptor new];
    pd.vertexFunction = [library newFunctionWithName:@"v"];
    pd.fragmentFunction = [library newFunctionWithName:@"f"];
    pd.colorAttachments[0].pixelFormat = MTLPixelFormatRGBA8Unorm;
    if (beside) {
        pd.colorAttachments[1].pixelFormat = MTLPixelFormatR16Float;
        pd.colorAttachments[1].writeMask = MTLColorWriteMaskNone;
    }
    NSError *error = nil;
    id<MTLRenderPipelineState> pipeline = [device newRenderPipelineStateWithDescriptor:pd error:&error];
    if (!pipeline) {
        nm_setup(name, "render pipeline: %.300s", error ? error.localizedDescription.UTF8String : "no error text");
        return;
    }
    nm_info("%s: rows of %lu bytes at offset 0%s", name, (unsigned long)row,
            beside ? ", beside a private R16Float attachment" : "");
    for (unsigned frame = 1; frame <= 3; frame++) {
        @autoreleasepool {
            char fname[64];
            snprintf(fname, sizeof fname, "%s-frame%u", name, frame);
            MTLRenderPassDescriptor *pass = [MTLRenderPassDescriptor renderPassDescriptor];
            pass.colorAttachments[0].texture = target;
            pass.colorAttachments[0].loadAction = MTLLoadActionClear;
            pass.colorAttachments[0].storeAction = MTLStoreActionStore;
            pass.colorAttachments[0].clearColor = MTLClearColorMake(0, 0, 0, 0);
            if (beside) {
                pass.colorAttachments[1].texture = aux;
                pass.colorAttachments[1].loadAction = MTLLoadActionClear;
                pass.colorAttachments[1].storeAction = MTLStoreActionDontCare;
            }
            id<MTLCommandBuffer> cb = [queue commandBuffer];
            id<MTLRenderCommandEncoder> encoder = [cb renderCommandEncoderWithDescriptor:pass];
            if (!encoder) {
                nm_fail(fname, "no render encoder");
                return;
            }
            [encoder setRenderPipelineState:pipeline];
            [encoder setFragmentBytes:&frame length:sizeof frame atIndex:0];
            [encoder setScissorRect:(MTLScissorRect){0, 0, W / 2, H}];
            [encoder drawPrimitives:MTLPrimitiveTypeTriangle vertexStart:0 vertexCount:3];
            [encoder endEncoding];
            if (!nm_run(cb, fname)) return;
            unsigned first = 0;
            const unsigned bad = verify(buffer.contents, row, W, H, frame, &first);
            const uint8_t *p = (const uint8_t *)buffer.contents + (first / W) * row + (first % W) * 4;
            if (!bad)
                nm_pass(fname, "8192 pixels as expected");
            else
                nm_fail(fname, "%u values wrong, first at (%u,%u) got=(%u,%u,%u,%u)", bad, first % W, first / W, p[0],
                        p[1], p[2], p[3]);
        }
    }
}

static void packed_case(id<MTLComputePipelineState> copy, MTLPixelFormat format, uint32_t word, const char *name)
{
    enum { N = 32 };
    const NSUInteger row = linear_row(format, 256);
    id<MTLBuffer> buffer = [device newBufferWithLength:row * N options:MTLResourceStorageModeShared];
    if (!buffer) {
        nm_setup(name, "buffer");
        return;
    }
    for (unsigned y = 0; y < N; y++)
        for (unsigned x = 0; x < N; x++) memcpy((uint8_t *)buffer.contents + y * row + x * 4, &word, 4);
    MTLTextureDescriptor *d = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:format
                                                                                 width:N
                                                                                height:N
                                                                             mipmapped:NO];
    d.storageMode = MTLStorageModeShared;
    d.usage = MTLTextureUsageShaderRead;
    id<MTLTexture> source = [buffer newTextureWithDescriptor:d offset:0 bytesPerRow:row];
    if (!source) {
        nm_fail(name, "refused: the buffer gave no texture of this format (driver: %s)", refusal_line());
        return;
    }
    if (!copy) return;
    MTLTextureDescriptor *od = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA32Float
                                                                                  width:N
                                                                                 height:N
                                                                              mipmapped:NO];
    od.storageMode = MTLStorageModePrivate;
    od.usage = MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite;
    id<MTLTexture> out = [device newTextureWithDescriptor:od];
    id<MTLBuffer> readback = [device newBufferWithLength:N * 16 * N options:MTLResourceStorageModeShared];
    if (!out || !readback) {
        nm_setup(name, "RGBA32Float texture or readback buffer");
        return;
    }
    id<MTLCommandBuffer> cb = [queue commandBuffer];
    id<MTLComputeCommandEncoder> encoder = [cb computeCommandEncoder];
    [encoder setComputePipelineState:copy];
    [encoder setTexture:source atIndex:0];
    [encoder setTexture:out atIndex:1];
    [encoder dispatchThreads:MTLSizeMake(N, N, 1) threadsPerThreadgroup:MTLSizeMake(8, 8, 1)];
    [encoder endEncoding];
    id<MTLBlitCommandEncoder> blit = [cb blitCommandEncoder];
    [blit copyFromTexture:out
                     sourceSlice:0
                     sourceLevel:0
                    sourceOrigin:MTLOriginMake(0, 0, 0)
                      sourceSize:MTLSizeMake(N, N, 1)
                        toBuffer:readback
               destinationOffset:0
          destinationBytesPerRow:N * 16
        destinationBytesPerImage:N * 16 * N];
    [blit endEncoding];
    if (!nm_run(cb, name)) return;
    const float expected[4] = {1.0f, 0.5f, 0.25f, 1.0f}, *f = readback.contents;
    unsigned bad = 0;
    for (unsigned i = 0; i < N * N * 4; i++) bad += !isfinite(f[i]) || fabsf(f[i] - expected[i % 4]) > 1e-3f;
    if (!bad)
        nm_pass(name, "1024 texels read as (1, 0.5, 0.25, 1)");
    else
        nm_fail(name, "%u of 4096 values wrong, first texel got=(%g,%g,%g,%g)", bad, f[0], f[1], f[2], f[3]);
}

int main(void)
{
    @autoreleasepool {
        nm_start(60);
        device = nm_device();
        queue = [device newCommandQueue];
        // the fragment adds its colour to the pixel framebuffer fetch reads (as tools/test_surface_render_formats.m)
        id<MTLLibrary> draw = nm_library(
            device,
            @"#include <metal_stdlib>\nusing namespace metal;\n"
             "vertex float4 v(uint i [[vertex_id]]) { float2 p = float2((i << 1) & 2, i & 2);"
             " return float4(p * 2.0 - 1.0, 0, 1); }\n"
             "fragment float4 f(float4 pos [[position]], constant uint &frame [[buffer(0)]],"
             " float4 previous [[color(0)]]) { uint2 p = uint2(pos.xy);"
             " return previous + float4(float((p.x + frame) & 3) * 0.25, float((p.y + frame) & 3) * 0.25,"
             " float(frame) * 0.25, 1); }\n",
            "fetch-library");
        if (draw) {
            alias_case(draw, YES, "alias-beside-tiled");
            alias_case(draw, NO, "alias-alone");
            NSString *line = nm_log_find(nm_log_lines(), "drawn through tiled stand-ins", NULL);
            nm_info("driver: %s", line ? line.UTF8String : "(no line about tiled stand-ins in the driver's log)");
        }
        id<MTLLibrary> copy_library = nm_library(
            device,
            @"#include <metal_stdlib>\nusing namespace metal;\n"
             "kernel void copy(texture2d<float, access::read> s [[texture(0)]],"
             " texture2d<float, access::write> d [[texture(1)]], uint2 p [[thread_position_in_grid]]) {"
             " if (p.x < s.get_width() && p.y < s.get_height()) d.write(s.read(p), p); }\n",
            "copy-library");
        id<MTLComputePipelineState> copy =
            copy_library ? nm_kernel(device, copy_library, @"copy", "copy-library") : nil;
        // RG11B10Float: R in bits 0-10 and G in 11-21 (5-bit exponent, 6-bit mantissa), B in 22-31 (5-bit exponent,
        // 5-bit mantissa), bias 15: 1.0 = 15 << 6, 0.5 = 14 << 6, 0.25 = 13 << 5
        packed_case(copy, MTLPixelFormatRG11B10Float, 0x3C0u | (0x380u << 11) | (0x1A0u << 22), "rg11b10");
        // RGB9E5Float: 9-bit mantissas in bits 0-8, 9-17, 18-26 and a shared exponent in 27-31, value m * 2^(e - 24):
        // e = 16 gives 1.0 = 256, 0.5 = 128, 0.25 = 64
        packed_case(copy, MTLPixelFormatRGB9E5Float, 256u | (128u << 9) | (64u << 18) | (16u << 27), "rgb9e5");
        return nm_finish();
    }
}
