/* plugin_managed_tiled.m: a shader-writable Managed wrapper of an IOSurface in the family's VRAM, drawn through a tiled
 * image of its own, and what other wrappers of the same surface and the CPU see of it, on the installed Metal driver.
 * For RGBA8Unorm ('RGBA', 128x64, 512-byte rows) and RGBA16Float ('RGhA', 128x64, 1024-byte rows) it makes the surface
 * and, before any other wrapper of it, the wrapper M (Managed, ShaderRead|ShaderWrite|RenderTarget), so that M is not
 * an alias of a cached image. It reads M's fields _parent, _vramOn, _stex and the first word of _surfaceLinear by name:
 * with NVMTL_MANAGED_SURFACE_TILED unset M must be a tiled image of its own over the VRAM import (_vramOn 1,
 * _surfaceLinear set, _stex 1); with NVMTL_MANAGED_SURFACE_TILED=0 it must be drawn straight into the import (_vramOn
 * 1, _surfaceLinear empty). The driver's log of this process must say which (the switch is read once per process). Then
 * a wrapper S (Shared, ShaderRead|RenderTarget) and four steps, each read back exactly (RGBA8) or within 1e-3 (16F):
 *   a  red is drawn into M: S, a new Shared wrapper and the CPU (IOSurfaceLock) read red
 *   b  green is drawn into S: M reads green (another writer moved the VRAM's generation)
 *   c  the CPU writes blue through IOSurfaceLock: M reads blue (the surface's seed moved)
 *   d  a kernel writes yellow through M: S and the CPU read yellow
 * M is read only through blits, never with getBytes. When M got a private image instead of the VRAM, its case FAILs as
 * "backing" with the driver's reason line ("item 7: surface ... -> a private image ... (<reason>)"), and the steps
 * still run, since what other wrappers see must hold for every backing.
 *   /usr/bin/xcrun clang -arch x86_64 -mmacosx-version-min=15.0 -fobjc-arc -O2 tools/card/plugin_managed_tiled.m \
 *     -framework Foundation -framework Metal -framework IOSurface -o <run>/bin/<id>/nvmtltest
 * It needs NVMTL_PROCESS_ENV=1 (a release plugin reads no NVMTL_ switch and writes no log without it) and
 * NVMTL_LOG_PATH; without them it exits 3. It writes no file and opens no window; the surfaces are its own, in memory.
 * Exit 0 pass, 1 a case failed, 2 setup only, 3 skip, 70 crash. Not yet run on a Mac. */
#import "plugin_common.h"
#import <IOSurface/IOSurface.h>
#include <math.h>

enum { W = 128, H = 64 };

typedef struct {
    const char *name;
    MTLPixelFormat format;
    uint32_t fourcc;
    size_t bpp;
} format_t;

static const format_t FORMATS[] = {
    {"RGBA8", MTLPixelFormatRGBA8Unorm, 0x52474241, 4},    // 'RGBA'
    {"RGBA16F", MTLPixelFormatRGBA16Float, 0x52476841, 8}, // 'RGhA'
};

static const float RED[4] = {1, 0, 0, 1}, GREEN[4] = {0, 1, 0, 1}, BLUE[4] = {0, 0, 1, 1}, YELLOW[4] = {1, 1, 0, 1};

static float component(const format_t *f, const uint8_t *pixel, unsigned c)
{
    if (f->bpp == 4) return pixel[c] / 255.0f;
    return (float)((const __fp16 *)pixel)[c];
}

// counts the pixels of a W x H image (rows of `row` bytes) that differ from `colour`; prints the case line
static void compare(const format_t *f, const char *step, const char *reader, const uint8_t *base, size_t row,
                    const float colour[4])
{
    const float tolerance = f->bpp == 4 ? 0.5f / 255.0f : 1e-3f;
    unsigned bad = 0, fx = 0, fy = 0;
    float got[4] = {0, 0, 0, 0};
    for (unsigned y = 0; y < H; y++)
        for (unsigned x = 0; x < W; x++) {
            const uint8_t *p = base + y * row + x * f->bpp;
            BOOL wrong = NO;
            for (unsigned c = 0; c < 4; c++) {
                float v = component(f, p, c);
                if (!isfinite(v) || fabsf(v - colour[c]) > tolerance) wrong = YES;
            }
            if (wrong && !bad++) {
                fx = x, fy = y;
                for (unsigned c = 0; c < 4; c++) got[c] = component(f, p, c);
            }
        }
    char name[96];
    snprintf(name, sizeof name, "%s-%s-%s", f->name, step, reader);
    if (!bad)
        nm_pass(name, "%u pixels as expected", W * H);
    else
        nm_fail(name, "%u of %u pixels wrong, first (%u,%u) got=(%.4g,%.4g,%.4g,%.4g) expected=(%g,%g,%g,%g)", bad,
                W * H, fx, fy, got[0], got[1], got[2], got[3], colour[0], colour[1], colour[2], colour[3]);
}

static void fill_cpu(const format_t *f, uint8_t *base, size_t row, const float colour[4])
{
    for (unsigned y = 0; y < H; y++)
        for (unsigned x = 0; x < W; x++) {
            uint8_t *p = base + y * row + x * f->bpp;
            for (unsigned c = 0; c < 4; c++) {
                if (f->bpp == 4)
                    p[c] = (uint8_t)lrintf(colour[c] * 255.0f);
                else
                    ((__fp16 *)p)[c] = (__fp16)colour[c];
            }
        }
}

static id<MTLDevice> device;
static id<MTLCommandQueue> queue;
static id<MTLComputePipelineState> fill_pipeline;
static id<MTLRenderPipelineState> draw_pipeline[2]; // RGBA8Unorm, RGBA16Float

// a texture read back through a blit into a Shared buffer
static void read_blit(const format_t *f, const char *step, const char *reader, id<MTLTexture> texture,
                      const float colour[4])
{
    char name[96];
    snprintf(name, sizeof name, "%s-%s-%s", f->name, step, reader);
    if (!texture) {
        nm_setup(name, "no texture to read");
        return;
    }
    const size_t row = W * f->bpp;
    id<MTLBuffer> buffer = [device newBufferWithLength:row * H options:MTLResourceStorageModeShared];
    if (!buffer) {
        nm_setup(name, "readback buffer");
        return;
    }
    memset(buffer.contents, 0x33, row * H);
    id<MTLCommandBuffer> cb = [queue commandBuffer];
    id<MTLBlitCommandEncoder> blit = [cb blitCommandEncoder];
    [blit copyFromTexture:texture
                     sourceSlice:0
                     sourceLevel:0
                    sourceOrigin:MTLOriginMake(0, 0, 0)
                      sourceSize:MTLSizeMake(W, H, 1)
                        toBuffer:buffer
               destinationOffset:0
          destinationBytesPerRow:row
        destinationBytesPerImage:row * H];
    [blit endEncoding];
    if (!nm_run(cb, name)) return;
    compare(f, step, reader, buffer.contents, row, colour);
}

static void read_cpu(const format_t *f, const char *step, IOSurfaceRef surface, const float colour[4])
{
    char name[96];
    snprintf(name, sizeof name, "%s-%s-cpu", f->name, step);
    if (IOSurfaceLock(surface, kIOSurfaceLockReadOnly, NULL) != kIOReturnSuccess) {
        nm_fail(name, "IOSurfaceLock failed");
        return;
    }
    compare(f, step, "cpu", IOSurfaceGetBaseAddress(surface), IOSurfaceGetBytesPerRow(surface), colour);
    IOSurfaceUnlock(surface, kIOSurfaceLockReadOnly, NULL);
}

// a pass that clears the texture to transparent black and draws one full-screen triangle of `colour` (the draw goes
// through the texture's colour attachment view, the clear alone would not)
static BOOL draw_to(const format_t *f, const char *step, id<MTLTexture> texture, const float colour[4])
{
    char name[96];
    snprintf(name, sizeof name, "%s-%s-draw", f->name, step);
    id<MTLRenderPipelineState> pipeline = f->bpp == 4 ? draw_pipeline[0] : draw_pipeline[1];
    if (!pipeline) {
        nm_setup(name, "no render pipeline");
        return NO;
    }
    MTLRenderPassDescriptor *pass = [MTLRenderPassDescriptor renderPassDescriptor];
    pass.colorAttachments[0].texture = texture;
    pass.colorAttachments[0].loadAction = MTLLoadActionClear;
    pass.colorAttachments[0].storeAction = MTLStoreActionStore;
    pass.colorAttachments[0].clearColor = MTLClearColorMake(0, 0, 0, 0);
    id<MTLCommandBuffer> cb = [queue commandBuffer];
    id<MTLRenderCommandEncoder> encoder = [cb renderCommandEncoderWithDescriptor:pass];
    if (!encoder) {
        nm_fail(name, "no render encoder");
        return NO;
    }
    [encoder setRenderPipelineState:pipeline];
    [encoder setFragmentBytes:colour length:4 * sizeof(float) atIndex:0];
    [encoder drawPrimitives:MTLPrimitiveTypeTriangle vertexStart:0 vertexCount:3];
    [encoder endEncoding];
    return nm_run(cb, name);
}

static BOOL kernel_write(const format_t *f, const char *step, id<MTLTexture> texture, const float colour[4])
{
    char name[96];
    snprintf(name, sizeof name, "%s-%s-kernel", f->name, step);
    if (!fill_pipeline) {
        nm_setup(name, "no compute pipeline");
        return NO;
    }
    id<MTLCommandBuffer> cb = [queue commandBuffer];
    id<MTLComputeCommandEncoder> encoder = [cb computeCommandEncoder];
    [encoder setComputePipelineState:fill_pipeline];
    [encoder setTexture:texture atIndex:0];
    [encoder setBytes:colour length:4 * sizeof(float) atIndex:0];
    [encoder dispatchThreads:MTLSizeMake(W, H, 1) threadsPerThreadgroup:MTLSizeMake(8, 8, 1)];
    [encoder endEncoding];
    return nm_run(cb, name);
}

static id<MTLTexture> wrapper(IOSurfaceRef surface, const format_t *f, MTLStorageMode storage, MTLTextureUsage usage)
{
    MTLTextureDescriptor *d = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:f->format
                                                                                 width:W
                                                                                height:H
                                                                             mipmapped:NO];
    d.storageMode = storage;
    d.usage = usage;
    return [device newTextureWithDescriptor:d iosurface:surface plane:0];
}

int main(void)
{
    @autoreleasepool {
        nm_start(60);
        const char *process_env = getenv("NVMTL_PROCESS_ENV"), *log_path = getenv("NVMTL_LOG_PATH");
        if (!process_env || strcmp(process_env, "1") || !log_path || !*log_path)
            nm_skip(
                "needs NVMTL_PROCESS_ENV=1 and NVMTL_LOG_PATH: the driver reads the switch and writes its log "
                "only then");
        // the plugin's own reading of the switch: on unless the value starts with 0
        const char *e = getenv("NVMTL_MANAGED_SURFACE_TILED");
        const BOOL tiled = !(e && e[0] == '0');
        device = nm_device();
        queue = [device newCommandQueue];
        nm_info("NVMTL_MANAGED_SURFACE_TILED=%s: Managed wrappers are expected %s", e ? e : "unset",
                tiled ? "to be drawn through a tiled image of their own" : "to be drawn straight into the VRAM import");
        id<MTLLibrary> library = nm_library(
            device,
            @"#include <metal_stdlib>\nusing namespace metal;\n"
             "kernel void fill(texture2d<float, access::write> t [[texture(0)]], constant float4 &c [[buffer(0)]],"
             " uint2 p [[thread_position_in_grid]]) {"
             " if (p.x < t.get_width() && p.y < t.get_height()) t.write(c, p); }\n"
             "vertex float4 v(uint i [[vertex_id]]) { float2 p = float2((i << 1) & 2, i & 2);"
             " return float4(p * 2.0 - 1.0, 0, 1); }\n"
             "fragment float4 colour(constant float4 &c [[buffer(0)]]) { return c; }\n",
            "library");
        if (library) {
            fill_pipeline = nm_kernel(device, library, @"fill", "kernel");
            for (unsigned k = 0; k < 2; k++) {
                MTLRenderPipelineDescriptor *pd = [MTLRenderPipelineDescriptor new];
                pd.vertexFunction = [library newFunctionWithName:@"v"];
                pd.fragmentFunction = [library newFunctionWithName:@"colour"];
                pd.colorAttachments[0].pixelFormat = k ? MTLPixelFormatRGBA16Float : MTLPixelFormatRGBA8Unorm;
                NSError *error = nil;
                draw_pipeline[k] = [device newRenderPipelineStateWithDescriptor:pd error:&error];
                if (!draw_pipeline[k])
                    nm_setup(k ? "RGBA16F-pipeline" : "RGBA8-pipeline", "render pipeline: %.300s",
                             error ? error.localizedDescription.UTF8String : "no error text");
            }
        }

        // every surface and its wrapper M first, before any other wrapper of these surfaces: M is then made by the
        // driver itself, not taken as an alias of a cached image, and its log lines come first
        const unsigned nf = sizeof FORMATS / sizeof FORMATS[0];
        IOSurfaceRef surfaces[2] = {NULL, NULL};
        id<MTLTexture> managed[2] = {nil, nil};
        for (unsigned i = 0; i < nf; i++) {
            const format_t *f = &FORMATS[i];
            const size_t row = W * f->bpp;
            surfaces[i] = IOSurfaceCreate((__bridge CFDictionaryRef) @{
                (id)kIOSurfaceWidth : @(W),
                (id)kIOSurfaceHeight : @(H),
                (id)kIOSurfaceBytesPerElement : @(f->bpp),
                (id)kIOSurfaceBytesPerRow : @(row),
                (id)kIOSurfaceAllocSize : @(row * H),
                (id)kIOSurfacePixelFormat : @(f->fourcc)
            });
            if (!surfaces[i] || IOSurfaceGetBytesPerRow(surfaces[i]) != row) {
                nm_setup(f->name, "IOSurfaceCreate of %ux%u with %zu-byte rows", W, H, row);
                continue;
            }
            IOSurfaceLock(surfaces[i], 0, NULL);
            memset(IOSurfaceGetBaseAddress(surfaces[i]), 0x55, row * H);
            IOSurfaceUnlock(surfaces[i], 0, NULL);
            managed[i] = wrapper(surfaces[i], f, MTLStorageModeManaged,
                                 MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite | MTLTextureUsageRenderTarget);
            if (!managed[i]) nm_setup(f->name, "the Managed wrapper is nil");
        }

        NSArray<NSString *> *log = nm_log_lines();
        BOOL any_vram = NO;
        for (unsigned i = 0; i < nf; i++) {
            const format_t *f = &FORMATS[i];
            if (!managed[i]) continue;
            char name[64];
            snprintf(name, sizeof name, "%s-backing", f->name);
            BOOL has_parent = NO, has_linear = NO;
            const void *parent = nm_pointer_field(managed[i], "_parent", &has_parent);
            const void *linear = nm_pointer_field(managed[i], "_surfaceLinear", &has_linear);
            const int vram = nm_bool_field(managed[i], "_vramOn"), stex = nm_bool_field(managed[i], "_stex");
            nm_info("%s M: _parent=%s _vramOn=%d _surfaceLinear=%s _stex=%d usage=0x%lx", f->name,
                    has_parent ? (parent ? "set" : "nil") : "absent", vram,
                    has_linear ? (linear ? "set" : "empty") : "absent", stex, (unsigned long)managed[i].usage);
            if (!has_parent || !has_linear || vram < 0 || stex < 0) {
                nm_fail(
                    name,
                    "the driver's texture lacks a field this check reads (_parent, _surfaceLinear, _vramOn, _stex)");
                continue;
            }
            if (parent) {
                nm_fail(name, "M is an alias of another image (_parent set): the check cannot judge its backing");
                continue;
            }
            if (vram != 1) {
                char id_text[48];
                snprintf(id_text, sizeof id_text, "item 7: surface %u ", (unsigned)IOSurfaceGetID(surfaces[i]));
                NSString *why = nm_log_find(log, id_text, "-> a private image", NULL);
                nm_fail(name, "M got a private image, not the family's VRAM (driver: %s)",
                        why ? why.UTF8String : "no 'item 7' line for this surface in the driver's log");
                continue;
            }
            any_vram = YES;
            if (stex != 1)
                nm_fail(name, "M has no storage view (_stex=0) although it asked for ShaderWrite");
            else if (tiled && !linear)
                nm_fail(name, "M is drawn straight into the VRAM import, expected a tiled image of its own");
            else if (!tiled && linear)
                nm_fail(name, "M has a tiled image of its own, expected none with NVMTL_MANAGED_SURFACE_TILED=0");
            else if (tiled)
                nm_pass(name, "backing: a tiled image of its own over the VRAM import");
            else
                nm_pass(name, "backing: straight into the VRAM import");
        }
        if (any_vram) {
            NSString *line = nm_log_find(log, "shader-writable Managed surfaces: ", NULL);
            const char *want = tiled ? "shader-writable Managed surfaces: a tiled image of their own"
                                     : "shader-writable Managed surfaces: drawn straight into the VRAM import";
            if (line) nm_info("driver: %s", line.UTF8String);
            if (line && [line hasPrefix:[NSString stringWithUTF8String:want]])
                nm_pass("switch-log", "the driver read the switch as expected");
            else
                nm_fail("switch-log", "the driver's log %s, expected '%s'",
                        line ? "says something else" : "has no 'shader-writable Managed surfaces' line", want);
            if (tiled)
                for (unsigned i = 0; i < nf; i++) {
                    if (!managed[i] || nm_bool_field(managed[i], "_vramOn") != 1) continue;
                    char id_text[48], name[64];
                    snprintf(id_text, sizeof id_text, "item 7: surface %u %ux%u ",
                             (unsigned)IOSurfaceGetID(surfaces[i]), W, H);
                    snprintf(name, sizeof name, "%s-backing-log", FORMATS[i].name);
                    NSString *seen = nm_log_find(log, id_text, "drawn through a tiled image of its own", NULL);
                    if (seen)
                        nm_pass(name, "the driver logged it: %s", seen.UTF8String);
                    else
                        nm_fail(name, "no '%s... drawn through a tiled image of its own' line in the driver's log",
                                id_text);
                }
        }

        for (unsigned i = 0; i < nf; i++) {
            @autoreleasepool {
                const format_t *f = &FORMATS[i];
                if (!managed[i]) continue;
                IOSurfaceRef surface = surfaces[i];
                id<MTLTexture> m = managed[i];
                id<MTLTexture> s =
                    wrapper(surface, f, MTLStorageModeShared, MTLTextureUsageShaderRead | MTLTextureUsageRenderTarget);
                if (!s) {
                    nm_setup(f->name, "the Shared wrapper S is nil");
                    continue;
                }
                // a: red drawn into M; the other wrappers and the CPU see it once M has published it into the surface
                if (draw_to(f, "a", m, RED)) {
                    read_blit(f, "a", "S", s, RED);
                    read_cpu(f, "a", surface, RED);
                    read_blit(f, "a", "new-shared",
                              wrapper(surface, f, MTLStorageModeShared, MTLTextureUsageShaderRead), RED);
                }
                // b: green drawn into S; M takes the VRAM's newer pixels into its own image
                if (draw_to(f, "b", s, GREEN)) read_blit(f, "b", "M", m, GREEN);
                // c: the CPU writes blue; M takes the surface's newer seed
                {
                    char name[64];
                    snprintf(name, sizeof name, "%s-c-cpu-write", f->name);
                    if (IOSurfaceLock(surface, 0, NULL) != kIOReturnSuccess)
                        nm_fail(name, "IOSurfaceLock failed");
                    else {
                        fill_cpu(f, IOSurfaceGetBaseAddress(surface), IOSurfaceGetBytesPerRow(surface), BLUE);
                        IOSurfaceUnlock(surface, 0, NULL);
                        read_blit(f, "c", "M", m, BLUE);
                    }
                }
                // d: a kernel writes yellow through M; S and the CPU see it
                if (kernel_write(f, "d", m, YELLOW)) {
                    read_blit(f, "d", "S", s, YELLOW);
                    read_cpu(f, "d", surface, YELLOW);
                }
            }
        }
        for (unsigned i = 0; i < nf; i++) {
            managed[i] = nil;
            if (surfaces[i]) CFRelease(surfaces[i]);
        }
        return nm_finish();
    }
}
