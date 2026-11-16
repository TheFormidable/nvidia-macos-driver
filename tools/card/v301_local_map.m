/* v301_local_map.m: what NVK writes through its own maps and the GPU reads at once, on the installed Metal driver. From
 * release 3.0.1 NVK keeps the memory it maps itself and the CPU only writes (the shader heap, the image and sampler
 * tables, the zero page) in video memory behind BAR1 by default outside WindowServer, where BAR1 covers the whole video
 * memory, and reads a word of a page behind BAR1 before every GPPut it writes (nvk/nvk-local-map-vram.patch); before,
 * that memory was in system RAM, which NVK_LOCAL_MAP_SYSMEM=1 brings back. Where BAR1 does not cover the video memory
 * both ways are the same. The argument says how the row runs: "default" (NVK_LOCAL_MAP_SYSMEM and NVK_BAR_VRAM unset)
 * or "sysmem" (NVK_LOCAL_MAP_SYSMEM=1). Cases, each read back exactly:
 *   kernels   24 rounds; each makes a new library of 4 compute kernels whose numbers are new to this process (new
 *             shader code in NVK's heap), makes their pipelines and runs them at once in one command buffer
 *   textures  200 rounds; each makes a new private R32Float texture of 64x64, fills it by a blit from a buffer, makes a
 *             new sampler (its own lodMaxClamp, so no sampler is reused), and in the same command buffer a kernel reads
 *             every texel through the texture (read) and through the sampler (sample, nearest, level 0), with no wait;
 *             the texture and the sampler are freed at the end of the round, so their table slots are used again
 *   /usr/bin/xcrun clang -arch x86_64 -mmacosx-version-min=15.0 -fobjc-arc -O2 tools/card/v301_local_map.m \
 *     -framework Foundation -framework Metal -o <run>/bin/<id>/nvmtltest
 * It skips on a device of another driver and when NVK_LOCAL_MAP_SYSMEM or NVK_BAR_VRAM do not hold what the row asks
 * for after the device is made (the settings file sets them). It writes no file and opens no window. Exit 0 pass, 1 a
 * case failed, 2 setup only, 3 skip, 70 crash. Not yet run on a Mac. */
#import "plugin_common.h"

enum { ROUNDS = 24, PER = 4, THREADS = 1024, TROUNDS = 200, TW = 64 };

static id<MTLDevice> device;
static id<MTLCommandQueue> queue;
static uint32_t salt;

static uint32_t mul_of(unsigned r, unsigned k)
{
    return (salt ^ (r * 7919u + k * 104729u)) | 1u;
}

static uint32_t add_of(unsigned r, unsigned k)
{
    return salt * 17u + r * 100u + k;
}

static void case_kernels(void)
{
    const char *name = "kernels";
    id<MTLBuffer> out = [device newBufferWithLength:(NSUInteger)PER * THREADS * 4 options:MTLResourceStorageModeShared];
    if (!out) {
        nm_setup(name, "a shared buffer");
        return;
    }
    unsigned wrong = 0;
    for (unsigned r = 0; r < ROUNDS && wrong < 4; r++) @autoreleasepool {
            NSMutableString *src =
                [NSMutableString stringWithString:@"#include <metal_stdlib>\nusing namespace metal;\n"];
            for (unsigned k = 0; k < PER; k++)
                [src
                    appendFormat:@"kernel void m%u_%u(device uint *o [[buffer(0)]], uint i [[thread_position_in_grid]])"
                                 @" { o[i] = i * %uu + %uu; }\n",
                                 r, k, mul_of(r, k), add_of(r, k)];
            id<MTLLibrary> lib = nm_library(device, src, name);
            if (!lib) return;
            id<MTLComputePipelineState> ps[PER];
            for (unsigned k = 0; k < PER; k++) {
                ps[k] = nm_kernel(device, lib, [NSString stringWithFormat:@"m%u_%u", r, k], name);
                if (!ps[k]) return;
            }
            memset(out.contents, 0, (size_t)PER * THREADS * 4);
            id<MTLCommandBuffer> cb = [queue commandBuffer];
            id<MTLComputeCommandEncoder> enc = [cb computeCommandEncoder];
            for (unsigned k = 0; k < PER; k++) {
                [enc setComputePipelineState:ps[k]];
                [enc setBuffer:out offset:(NSUInteger)k * THREADS * 4 atIndex:0];
                [enc dispatchThreads:MTLSizeMake(THREADS, 1, 1) threadsPerThreadgroup:MTLSizeMake(64, 1, 1)];
            }
            [enc endEncoding];
            if (!nm_run(cb, name)) return;
            const uint32_t *v = out.contents;
            for (unsigned k = 0; k < PER; k++)
                for (unsigned i = 0; i < THREADS; i++)
                    if (v[k * THREADS + i] != i * mul_of(r, k) + add_of(r, k)) {
                        nm_fail(name, "round %u kernel %u wrote %08x at %u, %08x is what its text says", r, k,
                                v[k * THREADS + i], i, i * mul_of(r, k) + add_of(r, k));
                        wrong++;
                        break;
                    }
        }
    if (!wrong)
        nm_pass(name, "%u new kernels in %u new libraries, each run as soon as its pipeline was made", ROUNDS * PER,
                ROUNDS);
}

static void case_textures(void)
{
    const char *name = "textures";
    id<MTLLibrary> lib =
        nm_library(device,
                   @"#include <metal_stdlib>\nusing namespace metal;\n"
                    "kernel void look(texture2d<float, access::sample> t [[texture(0)]], sampler s [[sampler(0)]],"
                    " device float2 *o [[buffer(0)]], uint2 p [[thread_position_in_grid]]) {"
                    " float2 at = (float2(p) + 0.5) / float2(t.get_width(), t.get_height());"
                    " o[p.y * t.get_width() + p.x] = float2(t.read(p).x, t.sample(s, at, level(0)).x); }\n",
                   name);
    id<MTLComputePipelineState> look = lib ? nm_kernel(device, lib, @"look", name) : nil;
    id<MTLBuffer> in = [device newBufferWithLength:TW * TW * 4 options:MTLResourceStorageModeShared];
    id<MTLBuffer> out = [device newBufferWithLength:TW * TW * 8 options:MTLResourceStorageModeShared];
    if (!look) return;
    if (!in || !out) {
        nm_setup(name, "shared buffers");
        return;
    }
    unsigned wrong = 0;
    for (unsigned r = 0; r < TROUNDS && wrong < 4; r++) @autoreleasepool {
            float *f = in.contents;
            for (unsigned i = 0; i < TW * TW; i++) f[i] = (float)(r * 4096u + i);
            memset(out.contents, 0xff, TW * TW * 8);
            MTLTextureDescriptor *d = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatR32Float
                                                                                         width:TW
                                                                                        height:TW
                                                                                     mipmapped:NO];
            d.storageMode = MTLStorageModePrivate;
            d.usage = MTLTextureUsageShaderRead;
            id<MTLTexture> t = [device newTextureWithDescriptor:d];
            MTLSamplerDescriptor *sd = [MTLSamplerDescriptor new];
            sd.minFilter = MTLSamplerMinMagFilterNearest;
            sd.magFilter = MTLSamplerMinMagFilterNearest;
            sd.sAddressMode = MTLSamplerAddressModeClampToEdge;
            sd.tAddressMode = MTLSamplerAddressModeClampToEdge;
            sd.lodMaxClamp = 1000.0f + (float)r; // a sampler of its own
            id<MTLSamplerState> s = [device newSamplerStateWithDescriptor:sd];
            if (!t || !s) {
                nm_setup(name, "round %u: %s", r, t ? "a sampler" : "a texture");
                return;
            }
            id<MTLCommandBuffer> cb = [queue commandBuffer];
            id<MTLBlitCommandEncoder> blit = [cb blitCommandEncoder];
            [blit copyFromBuffer:in
                       sourceOffset:0
                  sourceBytesPerRow:TW * 4
                sourceBytesPerImage:TW * TW * 4
                         sourceSize:MTLSizeMake(TW, TW, 1)
                          toTexture:t
                   destinationSlice:0
                   destinationLevel:0
                  destinationOrigin:MTLOriginMake(0, 0, 0)];
            [blit endEncoding];
            id<MTLComputeCommandEncoder> enc = [cb computeCommandEncoder];
            [enc setComputePipelineState:look];
            [enc setTexture:t atIndex:0];
            [enc setSamplerState:s atIndex:0];
            [enc setBuffer:out offset:0 atIndex:0];
            [enc dispatchThreads:MTLSizeMake(TW, TW, 1) threadsPerThreadgroup:MTLSizeMake(8, 8, 1)];
            [enc endEncoding];
            if (!nm_run(cb, name)) return;
            const float *o = out.contents;
            for (unsigned i = 0; i < TW * TW; i++) {
                const float want = (float)(r * 4096u + i);
                if (o[2 * i] != want || o[2 * i + 1] != want) {
                    nm_fail(name, "round %u texel %u: read %g, sample %g, %g was written", r, i, (double)o[2 * i],
                            (double)o[2 * i + 1], (double)want);
                    wrong++;
                    break;
                }
            }
        }
    if (!wrong)
        nm_pass(name, "%u new textures and samplers, each read and sampled in the command buffer that filled it",
                TROUNDS);
}

int main(int argc, char **argv)
{
    @autoreleasepool {
        nm_start(60);
        const char *mode = argc > 1 ? argv[1] : "";
        const BOOL sysmem = !strcmp(mode, "sysmem");
        if (!sysmem && strcmp(mode, "default")) {
            printf("FAIL usage: v301_local_map default|sysmem\n");
            return 2;
        }
        device = nm_device();
        // after the device is made, the environment holds what the settings file set too (the environment wins)
        const char *e = getenv("NVK_LOCAL_MAP_SYSMEM"), *b = getenv("NVK_BAR_VRAM");
        if (!sysmem && (e || (b && b[0] == '0')))
            nm_skip("NVK_LOCAL_MAP_SYSMEM is %s and NVK_BAR_VRAM is %s (the settings file or the environment): the "
                    "default this row checks does not run",
                    e ? e : "unset", b ? b : "unset");
        if (sysmem && !(e && !strcmp(e, "1")))
            nm_skip("NVK_LOCAL_MAP_SYSMEM is %s, not 1: the way back this row checks does not run", e ? e : "unset");
        nm_info("NVK_LOCAL_MAP_SYSMEM=%s, NVK_BAR_VRAM=%s: NVK's own mapped memory is expected %s", e ? e : "unset",
                b ? b : "unset",
                sysmem ? "in system RAM (as in 3.0)"
                       : "behind BAR1 where BAR1 covers the video memory, in system RAM where it does not");
        salt = (uint32_t)getpid() * 2654435761u;
        queue = [device newCommandQueue];
        if (!queue) {
            nm_setup("start", "a command queue");
            return nm_finish();
        }
        case_kernels();
        case_textures();
        return nm_finish();
    }
}
