/* plugin_a8_attach.m: A8Unorm colour attachments without framebuffer fetch, on the installed Metal driver. Vulkan
 * forbids swizzled attachment views, so the driver attaches an A8 image, whose sampled view maps (0,0,0,r), through an
 * identity view of its own and keeps Metal's alpha in the image's red lane (plugin/nvmtl_vk.c
 * nvmtl_vk_cmd_attachment_view). Every case draws, so that the pass goes through the attachment view, and reads back
 * with a blit (bytes out of 255, within 1):
 *   blend   a private 38x38 A8Unorm cleared to alpha 0.25; each of four frames draws alpha 0.25 with One/One additive
 *           blending, the first after the clear and the others loading: 128, 191, 255, 255 (saturated)
 *   mip     a private 64x64 A8Unorm with two levels; level 1 (32x32) is the attachment, cleared to 0.25 with 0.25
 *           added: 128
 *   view    a private 38x38 R8Unorm (PixelFormatView) whose A8Unorm view is the attachment, cleared to 0.2 with 0.2
 *           added; the R8 texture itself is read: 102
 * The card runner runs it twice: as any application (framebuffer fetch on) and with NVMTL_NO_FBFETCH=1 (fetch off; the
 * switch is read only with NVMTL_PROCESS_ENV=1). The pixels must be the same in both. When the driver's log is there
 * (NVMTL_LOG_PATH), its "vk: framebuffer fetch" line must say what NVMTL_NO_FBFETCH asks for.
 *   /usr/bin/xcrun clang -arch x86_64 -mmacosx-version-min=15.0 -fobjc-arc -O2 tools/card/plugin_a8_attach.m \
 *     -framework Foundation -framework Metal -o <run>/bin/<id>/nvmtltest
 * It writes no file and opens no window. Exit 0 pass, 1 a case failed, 2 setup only, 3 skip, 70 crash. It has run once,
 * on one Mac, on macOS 26.7.3 (25G342) with release 3.0.1's package. */
#import "plugin_common.h"

static id<MTLDevice> device;
static id<MTLCommandQueue> queue;
static id<MTLRenderPipelineState> add_alpha; // returns (0, 0, 0, 0.25 or the bytes' value), One/One additive into A8

static id<MTLTexture> texture(MTLPixelFormat format, NSUInteger size, NSUInteger levels)
{
    MTLTextureDescriptor *d = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:format
                                                                                 width:size
                                                                                height:size
                                                                             mipmapped:levels > 1];
    if (levels > 1) d.mipmapLevelCount = levels;
    d.storageMode = MTLStorageModePrivate;
    d.usage = MTLTextureUsageShaderRead | MTLTextureUsageRenderTarget | MTLTextureUsagePixelFormatView;
    return [device newTextureWithDescriptor:d];
}

// one pass: load or clear `target` at `level`, draw `alpha` added to it; NO with a FAIL line when it did not run
static BOOL pass(id<MTLTexture> target, NSUInteger level, BOOL clear, float clear_alpha, float alpha, const char *name)
{
    MTLRenderPassDescriptor *p = [MTLRenderPassDescriptor renderPassDescriptor];
    p.colorAttachments[0].texture = target;
    p.colorAttachments[0].level = level;
    p.colorAttachments[0].loadAction = clear ? MTLLoadActionClear : MTLLoadActionLoad;
    p.colorAttachments[0].storeAction = MTLStoreActionStore;
    p.colorAttachments[0].clearColor = MTLClearColorMake(0, 0, 0, clear_alpha);
    id<MTLCommandBuffer> cb = [queue commandBuffer];
    id<MTLRenderCommandEncoder> encoder = [cb renderCommandEncoderWithDescriptor:p];
    if (!encoder) {
        nm_fail(name, "no render encoder");
        return NO;
    }
    [encoder setRenderPipelineState:add_alpha];
    [encoder setFragmentBytes:&alpha length:sizeof alpha atIndex:0];
    [encoder drawPrimitives:MTLPrimitiveTypeTriangle vertexStart:0 vertexCount:3];
    [encoder endEncoding];
    return nm_run(cb, name);
}

// the bytes of `source` at `level` (size x size), each compared with `expected` within 1
static void check_bytes(id<MTLTexture> source, NSUInteger level, NSUInteger size, unsigned expected, const char *name)
{
    id<MTLBuffer> out = [device newBufferWithLength:256 * size options:MTLResourceStorageModeShared];
    if (!out) {
        nm_setup(name, "readback buffer");
        return;
    }
    memset(out.contents, 0x11, 256 * size);
    id<MTLCommandBuffer> cb = [queue commandBuffer];
    id<MTLBlitCommandEncoder> blit = [cb blitCommandEncoder];
    [blit copyFromTexture:source
                     sourceSlice:0
                     sourceLevel:level
                    sourceOrigin:MTLOriginMake(0, 0, 0)
                      sourceSize:MTLSizeMake(size, size, 1)
                        toBuffer:out
               destinationOffset:0
          destinationBytesPerRow:256
        destinationBytesPerImage:256 * size];
    [blit endEncoding];
    if (!nm_run(cb, name)) return;
    const uint8_t *p = out.contents;
    unsigned bad = 0;
    for (NSUInteger y = 0; y < size; y++)
        for (NSUInteger x = 0; x < size; x++) bad += abs((int)p[y * 256 + x] - (int)expected) > 1;
    if (!bad)
        nm_pass(name, "%lu pixels read %u (first %u)", (unsigned long)(size * size), expected, p[0]);
    else
        nm_fail(name, "%u of %lu pixels wrong got=%u expected=%u", bad, (unsigned long)(size * size), p[0], expected);
}

int main(void)
{
    @autoreleasepool {
        nm_start(60);
        device = nm_device();
        queue = [device newCommandQueue];
        const char *off = getenv("NVMTL_NO_FBFETCH");
        const BOOL fetch_off = off && off[0] == '1'; // the driver's own reading of the switch
        id<MTLLibrary> library = nm_library(
            device,
            @"#include <metal_stdlib>\nusing namespace metal;\n"
             "vertex float4 v(uint i [[vertex_id]]) { float2 p = float2((i << 1) & 2, i & 2);"
             " return float4(p * 2.0 - 1.0, 0, 1); }\n"
             "fragment float4 f(constant float &a [[buffer(0)]]) { return float4(0, 0, 0, a); }\n",
            "library");
        if (library) {
            MTLRenderPipelineDescriptor *pd = [MTLRenderPipelineDescriptor new];
            pd.vertexFunction = [library newFunctionWithName:@"v"];
            pd.fragmentFunction = [library newFunctionWithName:@"f"];
            MTLRenderPipelineColorAttachmentDescriptor *ca = pd.colorAttachments[0];
            ca.pixelFormat = MTLPixelFormatA8Unorm;
            ca.blendingEnabled = YES;
            ca.rgbBlendOperation = ca.alphaBlendOperation = MTLBlendOperationAdd;
            ca.sourceRGBBlendFactor = ca.sourceAlphaBlendFactor = MTLBlendFactorOne;
            ca.destinationRGBBlendFactor = ca.destinationAlphaBlendFactor = MTLBlendFactorOne;
            NSError *error = nil;
            add_alpha = [device newRenderPipelineStateWithDescriptor:pd error:&error];
            if (!add_alpha)
                nm_setup("pipeline", "A8 render pipeline: %.300s",
                         error ? error.localizedDescription.UTF8String : "no error text");
        }
        if (add_alpha) {
            @autoreleasepool {
                id<MTLTexture> a8 = texture(MTLPixelFormatA8Unorm, 38, 1);
                if (!a8) nm_setup("blend", "A8Unorm texture");
                const unsigned expected[4] = {128, 191, 255, 255};
                for (unsigned frame = 0; a8 && frame < 4; frame++) {
                    char name[32];
                    snprintf(name, sizeof name, "blend-frame%u", frame);
                    if (!pass(a8, 0, frame == 0, 0.25f, 0.25f, name)) break;
                    check_bytes(a8, 0, 38, expected[frame], name);
                }
            }
            @autoreleasepool {
                id<MTLTexture> mip = texture(MTLPixelFormatA8Unorm, 64, 2);
                if (!mip)
                    nm_setup("mip", "two-level A8Unorm texture");
                else if (pass(mip, 1, YES, 0.25f, 0.25f, "mip"))
                    check_bytes(mip, 1, 32, 128, "mip");
            }
            @autoreleasepool {
                id<MTLTexture> r8 = texture(MTLPixelFormatR8Unorm, 38, 1);
                id<MTLTexture> view = [r8 newTextureViewWithPixelFormat:MTLPixelFormatA8Unorm];
                if (!r8 || !view)
                    nm_setup("view", "R8Unorm texture or its A8Unorm view");
                else if (pass(view, 0, YES, 0.2f, 0.2f, "view"))
                    check_bytes(r8, 0, 38, 102, "view");
            }
        }
        NSArray<NSString *> *log = nm_log_lines();
        if (!log)
            nm_info("NVMTL_LOG_PATH is not set: the driver's framebuffer fetch state is not checked");
        else {
            NSString *line = nm_log_find(log, "vk: framebuffer fetch ", NULL);
            if (line) nm_info("driver: %s", line.UTF8String);
            const BOOL says_on = line && [line hasPrefix:@"vk: framebuffer fetch ON"];
            if (!line)
                nm_fail("fetch-state", "no 'vk: framebuffer fetch' line in the driver's log");
            else if (says_on == !fetch_off)
                nm_pass("fetch-state", "the driver log says framebuffer fetch %s, as NVMTL_NO_FBFETCH asks",
                        says_on ? "ON" : "OFF");
            else
                nm_fail("fetch-state", "the driver log says framebuffer fetch %s, expected %s", says_on ? "ON" : "off",
                        fetch_off ? "OFF" : "ON");
        }
        return nm_finish();
    }
}
