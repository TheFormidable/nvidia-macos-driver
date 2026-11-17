/* plugin_unknown_paths.m: shader writes into textures whose usage is Unknown, made by the paths other than
 * -newTextureWithDescriptor: of the device, on the installed Metal driver. Since the fix such a texture is made
 * storage-capable wherever it is made (plugin/NVMTLDevice.m nvmtl_texture_usage_writable and its callers), so writes
 * through it, and through its mip views, land. Cases, each with its own line:
 *   heap-automatic   a Private automatic heap; an RGBA16Float 32x32 mipmapped texture, usage Unknown; a kernel writes
 *                    (0.25, 0.5, 0.75, 1) through a view of level 2; level 2 is blitted to a buffer: exact halves
 *   heap-placement   the same in a placement heap at offset 0
 *   heap-size        heapTextureSizeAndAlignWithDescriptor: of that descriptor with usage Unknown and with
 *                    ShaderRead|ShaderWrite|PixelFormatView: the same size and alignment
 *   buffer-texture   a Shared buffer's RGBA8Unorm 32x32 texture at offset 0 (rows of the reported linear alignment,
 *                    at least 256 bytes), usage Unknown; a kernel writes; the buffer's bytes are 64, 128, 191, 255
 *                    within 1
 *   texel-buffer     a Shared buffer's RGBA32Float texture buffer of 256 texels, usage Unknown; a kernel with
 *                    texture_buffer<float, access::write> writes; the buffer holds exactly the floats written
 * Every kernel is in a library of its own, so that a translator refusal of one shows as that case's setup failure. GPU
 * work is in one command buffer per step, each waited for before the next.
 *   /usr/bin/xcrun clang -arch x86_64 -mmacosx-version-min=15.0 -fobjc-arc -O2 tools/card/plugin_unknown_paths.m \
 *     -framework Foundation -framework Metal -o <run>/bin/<id>/nvmtltest
 * It writes no file and opens no window. Exit 0 pass, 1 a case failed, 2 setup only, 3 skip, 70 crash. It has run once,
 * on one Mac, on macOS 26.7.3 (25G342) with release 3.0.1's package. */
#import "plugin_common.h"

static const float VALUE[4] = {0.25f, 0.5f, 0.75f, 1.0f};
static id<MTLDevice> device;
static id<MTLCommandQueue> queue;

static NSString *const HEADER = @"#include <metal_stdlib>\nusing namespace metal;\n";

static id<MTLComputePipelineState> pipeline_for(NSString *body, NSString *function, const char *name)
{
    id<MTLLibrary> library = nm_library(device, [HEADER stringByAppendingString:body], name);
    return library ? nm_kernel(device, library, function, name) : nil;
}

static BOOL kernel_write(id<MTLComputePipelineState> pipeline, id<MTLTexture> target, NSUInteger w, NSUInteger h,
                         const char *name)
{
    id<MTLCommandBuffer> cb = [queue commandBuffer];
    id<MTLComputeCommandEncoder> encoder = [cb computeCommandEncoder];
    [encoder setComputePipelineState:pipeline];
    [encoder setTexture:target atIndex:0];
    [encoder dispatchThreads:MTLSizeMake(w, h, 1) threadsPerThreadgroup:MTLSizeMake(w < 8 ? w : 8, h < 8 ? h : 8, 1)];
    [encoder endEncoding];
    return nm_run(cb, name);
}

static MTLTextureDescriptor *chain_descriptor(MTLTextureUsage usage)
{
    MTLTextureDescriptor *d = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA16Float
                                                                                 width:32
                                                                                height:32
                                                                             mipmapped:YES];
    d.storageMode = MTLStorageModePrivate;
    d.usage = usage;
    return d;
}

// a kernel write through a view of level 2 of a heap texture, read back from the texture's own level 2
static void heap_case(id<MTLHeap> heap, BOOL placement, id<MTLComputePipelineState> pipeline, const char *name)
{
    if (!heap) {
        nm_setup(name, "the heap is nil");
        return;
    }
    if (!pipeline) return;
    MTLTextureDescriptor *d = chain_descriptor(MTLTextureUsageUnknown);
    id<MTLTexture> texture = placement ? [heap newTextureWithDescriptor:d offset:0] : [heap newTextureWithDescriptor:d];
    if (!texture) {
        nm_fail(name, "the heap gave no texture for usage Unknown");
        return;
    }
    id<MTLTexture> view = [texture newTextureViewWithPixelFormat:MTLPixelFormatRGBA16Float
                                                     textureType:MTLTextureType2D
                                                          levels:NSMakeRange(2, 1)
                                                          slices:NSMakeRange(0, 1)];
    if (!view) {
        nm_fail(name, "no view of level 2");
        return;
    }
    if (!kernel_write(pipeline, view, 8, 8, name)) return;
    id<MTLBuffer> out = [device newBufferWithLength:256 * 8 options:MTLResourceStorageModeShared];
    if (!out) {
        nm_setup(name, "readback buffer");
        return;
    }
    memset(out.contents, 0, 256 * 8);
    id<MTLCommandBuffer> cb = [queue commandBuffer];
    id<MTLBlitCommandEncoder> blit = [cb blitCommandEncoder];
    [blit copyFromTexture:texture
                     sourceSlice:0
                     sourceLevel:2
                    sourceOrigin:MTLOriginMake(0, 0, 0)
                      sourceSize:MTLSizeMake(8, 8, 1)
                        toBuffer:out
               destinationOffset:0
          destinationBytesPerRow:256
        destinationBytesPerImage:256 * 8];
    [blit endEncoding];
    if (!nm_run(cb, name)) return;
    unsigned bad = 0;
    for (unsigned y = 0; y < 8; y++) {
        const __fp16 *row = (const __fp16 *)((const uint8_t *)out.contents + y * 256);
        for (unsigned x = 0; x < 8; x++)
            for (unsigned c = 0; c < 4; c++) bad += (float)row[x * 4 + c] != VALUE[c];
    }
    if (!bad)
        nm_pass(name, "64 texels of level 2 as written through the view");
    else
        nm_fail(name, "%u of 256 values of level 2 wrong (writes through the view lost) got=%g expected=0.25", bad,
                (float)((const __fp16 *)out.contents)[0]);
}

int main(void)
{
    @autoreleasepool {
        nm_start(60);
        device = nm_device();
        queue = [device newCommandQueue];
        id<MTLComputePipelineState> fill2d = pipeline_for(
            @"kernel void fill(texture2d<float, access::write> t [[texture(0)]], uint2 p [[thread_position_in_grid]]) {"
             " if (p.x < t.get_width() && p.y < t.get_height()) t.write(float4(0.25, 0.5, 0.75, 1.0), p); }\n",
            @"fill", "kernel-2d");

        // the heap size query first: the automatic heap below is sized from it
        MTLSizeAndAlign unknown =
            [device heapTextureSizeAndAlignWithDescriptor:chain_descriptor(MTLTextureUsageUnknown)];
        MTLSizeAndAlign known =
            [device heapTextureSizeAndAlignWithDescriptor:chain_descriptor(MTLTextureUsageShaderRead |
                                                                           MTLTextureUsageShaderWrite |
                                                                           MTLTextureUsagePixelFormatView)];
        nm_info(
            "heapTextureSizeAndAlign: Unknown %lu bytes align %lu; "
            "ShaderRead|ShaderWrite|PixelFormatView %lu bytes align %lu",
            (unsigned long)unknown.size, (unsigned long)unknown.align, (unsigned long)known.size,
            (unsigned long)known.align);
        if (!unknown.size || !known.size)
            nm_fail("heap-size", "a size of 0");
        else if (unknown.size != known.size || unknown.align != known.align)
            nm_fail("heap-size",
                    "Unknown gives %lu/%lu, ShaderWrite gives %lu/%lu (size/align): Unknown is not sized as writable",
                    (unsigned long)unknown.size, (unsigned long)unknown.align, (unsigned long)known.size,
                    (unsigned long)known.align);
        else
            nm_pass("heap-size", "Unknown is sized as a writable texture");

        const NSUInteger align = known.align ? known.align : 256;
        const NSUInteger heap_size = ((known.size ? known.size : 65536) + align - 1) / align * align * 2 + 65536;
        for (unsigned placement = 0; placement < 2; placement++) {
            @autoreleasepool {
                MTLHeapDescriptor *hd = [MTLHeapDescriptor new];
                hd.size = heap_size;
                hd.storageMode = MTLStorageModePrivate;
                hd.hazardTrackingMode = MTLHazardTrackingModeTracked;
                if (placement) hd.type = MTLHeapTypePlacement;
                heap_case([device newHeapWithDescriptor:hd], placement, fill2d,
                          placement ? "heap-placement" : "heap-automatic");
            }
        }

        @autoreleasepool {
            const char *name = "buffer-texture";
            NSUInteger row = [device minimumLinearTextureAlignmentForPixelFormat:MTLPixelFormatRGBA8Unorm];
            if (row < 256) row = 256;
            id<MTLBuffer> buffer = [device newBufferWithLength:row * 32 options:MTLResourceStorageModeShared];
            MTLTextureDescriptor *d = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA8Unorm
                                                                                         width:32
                                                                                        height:32
                                                                                     mipmapped:NO];
            d.storageMode = MTLStorageModeShared;
            d.usage = MTLTextureUsageUnknown;
            if (!buffer)
                nm_setup(name, "buffer");
            else if (fill2d) {
                memset(buffer.contents, 0, row * 32);
                id<MTLTexture> texture = [buffer newTextureWithDescriptor:d offset:0 bytesPerRow:row];
                if (!texture)
                    nm_fail(name, "the buffer gave no texture for usage Unknown (rows of %lu bytes)",
                            (unsigned long)row);
                else if (kernel_write(fill2d, texture, 32, 32, name)) {
                    const unsigned expected[4] = {64, 128, 191, 255};
                    unsigned bad = 0;
                    for (unsigned y = 0; y < 32; y++) {
                        const uint8_t *p = (const uint8_t *)buffer.contents + y * row;
                        for (unsigned i = 0; i < 32 * 4; i++) bad += abs((int)p[i] - (int)expected[i % 4]) > 1;
                    }
                    const uint8_t *p = buffer.contents;
                    if (!bad)
                        nm_pass(name, "4096 bytes as written, rows of %lu bytes", (unsigned long)row);
                    else
                        nm_fail(name, "%u of 4096 bytes wrong, first texel got=(%u,%u,%u,%u) expected=(64,128,191,255)",
                                bad, p[0], p[1], p[2], p[3]);
                }
            }
        }

        @autoreleasepool {
            const char *name = "texel-buffer";
            id<MTLComputePipelineState> fill1d = pipeline_for(
                @"kernel void fill(texture_buffer<float, access::write> t [[texture(0)]],"
                 " uint i [[thread_position_in_grid]]) {"
                 " if (i < t.get_width()) t.write(float4(0.25, 0.5, 0.75, 1.0) * float(i + 1), i); }\n",
                @"fill", name);
            id<MTLBuffer> buffer = [device newBufferWithLength:256 * 16 options:MTLResourceStorageModeShared];
            MTLTextureDescriptor *d =
                [MTLTextureDescriptor textureBufferDescriptorWithPixelFormat:MTLPixelFormatRGBA32Float
                                                                       width:256
                                                             resourceOptions:MTLResourceStorageModeShared
                                                                       usage:MTLTextureUsageUnknown];
            if (!buffer)
                nm_setup(name, "buffer");
            else if (fill1d) {
                memset(buffer.contents, 0, 256 * 16);
                id<MTLTexture> texture = [buffer newTextureWithDescriptor:d offset:0 bytesPerRow:256 * 16];
                if (!texture)
                    nm_fail(name, "the buffer gave no texture buffer for usage Unknown");
                else if (kernel_write(fill1d, texture, 256, 1, name)) {
                    const float *f = buffer.contents;
                    unsigned bad = 0, first = 0;
                    for (unsigned i = 0; i < 256; i++)
                        for (unsigned c = 0; c < 4; c++)
                            if (f[i * 4 + c] != VALUE[c] * (float)(i + 1) && !bad++) first = i * 4 + c;
                    if (!bad)
                        nm_pass(name, "256 texels as written");
                    else
                        nm_fail(name, "%u of 1024 floats wrong, first [%u] got=%g expected=%g", bad, first, f[first],
                                VALUE[first % 4] * (float)(first / 4 + 1));
                }
            }
        }
        return nm_finish();
    }
}
