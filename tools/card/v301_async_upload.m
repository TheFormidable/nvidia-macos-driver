/* v301_async_upload.m: replaceRegion's asynchronous upload, on by default outside WindowServer from release 3.0.1
 * (plugin/nvmtl_switch_defaults.h, plugin/nvmtl_tex_upload.h), on the installed Metal driver. The argument says how the
 * row runs: "on" with NVMTL_ASYNC_TEX_UPLOAD unset (the default) or "off" with NVMTL_ASYNC_TEX_UPLOAD=0 (the way back:
 * replaceRegion waits for its copy, as in 3.0). Every texture is RGBA8Unorm or BGRA8Unorm without mip levels and is
 * read back exactly. The cases, in this order:
 *   many       300 textures of 128x128 (64 KiB each), each written by replaceRegion with nothing read between, then all
 *              copied into one buffer by blits in one command buffer, and every tenth also read with getBytes
 *   limit      "on": the driver's log says once that 256 pending uploads reached the count limit ("vk: tex upload
 *              async: 256 pending uploads hold <bytes> bytes of staging (limits 256 and 268435456)"), so the 44 after
 *              them waited for their copies; "off": the log never says so
 *   switch     the driver's log says the switch as the row expects it: "vk: async texture upload ON
 *              (NVMTL_ASYNC_TEX_UPLOAD=unset)" or "vk: async texture upload OFF (NVMTL_ASYNC_TEX_UPLOAD=0)"
 *   overwrite  one texture written whole twice and then in part, read by a blit and by getBytes, 50 times
 *   race       two threads write one texture 200 times each at once; then this thread writes it, and reads that
 *   destroy    500 textures of 64x64, each written and freed with no read (the next one is made first); every 50th is
 *              read before it is freed
 *   iosurface  a texture of an IOSurface, and a view of that texture, written by replaceRegion 20 times each; after
 *              each write the CPU reads the surface through IOSurfaceLock with no command buffer between, which holds
 *              only when the copy is finished before the surface is marked newest
 *   gating     with NVMTL_SURFIN_ZERO=1 and NVMTL_WS_FREE_OFFMAIN=1 in its environment (the row sets them): both are
 *              WindowServer's only, so any line the driver writes about them says they are off in this process
 *   /usr/bin/xcrun clang -arch x86_64 -mmacosx-version-min=15.0 -fobjc-arc -O2 tools/card/v301_async_upload.m \
 *     -framework Foundation -framework Metal -framework IOSurface -o <run>/bin/<id>/nvmtltest
 * It skips when NVMTL_PROCESS_ENV=1 or NVMTL_LOG_PATH is missing (a release plugin reads no switch and writes no log
 * without them), on a device of another driver, and when NVMTL_ASYNC_TEX_UPLOAD does not hold what the row asks for
 * after the device is made (the settings file sets it): then the default this row checks is not what runs. It writes no
 * file and opens no window. Exit 0 pass, 1 a case failed, 2 setup only, 3 skip, 70 crash. Not yet run on a Mac. */
#import "plugin_common.h"
#import <IOSurface/IOSurface.h>

enum { MANY = 300, MW = 128, MH = 128, SW = 128, SH = 64 };

static id<MTLDevice> device;
static id<MTLCommandQueue> queue;
static uint32_t salt;

// the value of texel (x, y) of texture n: every texture of the run differs from every other
static uint32_t texel(uint32_t n, uint32_t x, uint32_t y)
{
    uint32_t v = (n + 1u) * 0x9E3779B1u ^ (x * 0x85EBCA77u) ^ (y * 0xC2B2AE3Du) ^ salt;
    v ^= v >> 15;
    v *= 0x2C1B3C6Du;
    v ^= v >> 12;
    return v;
}

static void fill(uint32_t *p, uint32_t n, uint32_t w, uint32_t h, size_t row_words)
{
    for (uint32_t y = 0; y < h; y++)
        for (uint32_t x = 0; x < w; x++) p[y * row_words + x] = texel(n, x, y);
}

// the first texel that differs, or -1
static long differs(const uint32_t *p, uint32_t n, uint32_t w, uint32_t h, size_t row_words, uint32_t *got,
                    uint32_t *want)
{
    for (uint32_t y = 0; y < h; y++)
        for (uint32_t x = 0; x < w; x++)
            if (p[y * row_words + x] != texel(n, x, y)) {
                *got = p[y * row_words + x];
                *want = texel(n, x, y);
                return (long)y * w + x;
            }
    return -1;
}

static id<MTLTexture> texture(MTLPixelFormat format, uint32_t w, uint32_t h)
{
    MTLTextureDescriptor *d = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:format
                                                                                 width:w
                                                                                height:h
                                                                             mipmapped:NO];
    d.usage = MTLTextureUsageShaderRead;
    return [device newTextureWithDescriptor:d];
}

static void upload(id<MTLTexture> t, uint32_t n, uint32_t *scratch)
{
    fill(scratch, n, (uint32_t)t.width, (uint32_t)t.height, t.width);
    [t replaceRegion:MTLRegionMake2D(0, 0, t.width, t.height) mipmapLevel:0 withBytes:scratch bytesPerRow:t.width * 4];
}

// getBytes of the whole texture compared with texture n's values; NO with a FAIL line
static BOOL read_back(id<MTLTexture> t, uint32_t n, const char *name, const char *what, uint32_t *scratch)
{
    memset(scratch, 0, t.width * t.height * 4);
    [t getBytes:scratch bytesPerRow:t.width * 4 fromRegion:MTLRegionMake2D(0, 0, t.width, t.height) mipmapLevel:0];
    uint32_t got = 0, want = 0;
    long at = differs(scratch, n, (uint32_t)t.width, (uint32_t)t.height, t.width, &got, &want);
    if (at >= 0) {
        nm_fail(name, "%s: getBytes texel %ld is %08x, %08x was written", what, at, got, want);
        return NO;
    }
    return YES;
}

static id<MTLBuffer> blit_back(NSArray<id<MTLTexture>> *textures, const char *name)
{
    NSUInteger total = 0;
    for (id<MTLTexture> t in textures) total += t.width * t.height * 4;
    id<MTLBuffer> out = [device newBufferWithLength:total options:MTLResourceStorageModeShared];
    if (!out) {
        nm_setup(name, "a shared buffer of %lu bytes", (unsigned long)total);
        return nil;
    }
    memset(out.contents, 0, total);
    id<MTLCommandBuffer> cb = [queue commandBuffer];
    id<MTLBlitCommandEncoder> blit = [cb blitCommandEncoder];
    NSUInteger at = 0;
    for (id<MTLTexture> t in textures) {
        [blit copyFromTexture:t
                         sourceSlice:0
                         sourceLevel:0
                        sourceOrigin:MTLOriginMake(0, 0, 0)
                          sourceSize:MTLSizeMake(t.width, t.height, 1)
                            toBuffer:out
                   destinationOffset:at
              destinationBytesPerRow:t.width * 4
            destinationBytesPerImage:t.width * t.height * 4];
        at += t.width * t.height * 4;
    }
    [blit endEncoding];
    return nm_run(cb, name) ? out : nil;
}

static void case_many(uint32_t *scratch)
{
    const char *name = "many";
    NSMutableArray<id<MTLTexture>> *all = [NSMutableArray array];
    for (uint32_t n = 0; n < MANY; n++) {
        id<MTLTexture> t = texture(MTLPixelFormatRGBA8Unorm, MW, MH);
        if (!t) {
            nm_setup(name, "texture %u of %u", n, MANY);
            return;
        }
        upload(t, n, scratch);
        [all addObject:t];
    }
    id<MTLBuffer> out = blit_back(all, name);
    if (!out) return;
    const uint32_t *words = out.contents;
    unsigned wrong = 0;
    for (uint32_t n = 0; n < MANY; n++) {
        uint32_t got = 0, want = 0;
        long at = differs(words + (size_t)n * MW * MH, n, MW, MH, MW, &got, &want);
        if (at >= 0 && wrong++ < 4)
            nm_fail(name, "blit of texture %u: texel %ld is %08x, %08x was written", n, at, got, want);
    }
    for (uint32_t n = 0; n < MANY; n += 10)
        if (!read_back(all[n], n, name, "every tenth", scratch)) wrong++;
    if (!wrong)
        nm_pass(name, "%u textures of %ux%u written one after another, then read by blits and every tenth by getBytes",
                MANY, MW, MH);
}

static void case_limit(BOOL on)
{
    const char *name = "limit";
    NSArray<NSString *> *lines = nm_log_lines();
    unsigned said = 0;
    NSString *last = nil;
    for (NSString *line in lines)
        if ([line hasPrefix:@"vk: tex upload async: "] &&
            [line rangeOfString:@" pending uploads hold "].location != NSNotFound) {
            said++;
            last = line;
        }
    if (on) {
        NSString *want = @"vk: tex upload async: 256 pending uploads hold ";
        NSString *tail =
            @" bytes of staging (limits 256 and 268435456): an upload past a limit waits for its copy, as in 3.0";
        if (said == 1 && [last hasPrefix:want] && [last hasSuffix:tail])
            nm_pass(name, "the driver said once: %s", last.UTF8String);
        else
            nm_fail(name,
                    "%u lines of pending uploads at a limit (one with 256 uploads and the limits 256 and 268435456 "
                    "was expected); the last: %s",
                    said, last ? last.UTF8String : "none");
    } else {
        if (!said)
            nm_pass(name, "no line of pending uploads at a limit: every upload waited for its copy");
        else
            nm_fail(name, "%u lines of pending uploads at a limit with the upload off; the last: %s", said,
                    last.UTF8String);
    }
}

static void case_switch(BOOL on)
{
    const char *name = "switch";
    NSString *want = on ? @"vk: async texture upload ON (NVMTL_ASYNC_TEX_UPLOAD=unset)"
                        : @"vk: async texture upload OFF (NVMTL_ASYNC_TEX_UPLOAD=0)";
    NSString *line = nm_log_find(nm_log_lines(), "vk: async texture upload ", NULL);
    if ([line isEqualToString:want])
        nm_pass(name, "the driver says %s", line.UTF8String);
    else
        nm_fail(name, "the driver says \"%s\", not \"%s\"", line ? line.UTF8String : "nothing of the switch",
                want.UTF8String);
}

static void case_overwrite(uint32_t *scratch)
{
    const char *name = "overwrite";
    id<MTLTexture> t = texture(MTLPixelFormatRGBA8Unorm, MW, MH);
    if (!t) {
        nm_setup(name, "texture");
        return;
    }
    uint32_t *part = malloc((size_t)MW * MH * 4);
    if (!part) {
        nm_setup(name, "memory");
        return;
    }
    unsigned wrong = 0;
    for (uint32_t i = 0; i < 50 && wrong < 4; i++) {
        const uint32_t a = 1000 + 3 * i, b = a + 1, c = a + 2;
        upload(t, a, scratch);
        upload(t, b, scratch);
        id<MTLBuffer> out = blit_back(@[ t ], name);
        if (!out) {
            wrong++;
            break;
        }
        uint32_t got = 0, want = 0;
        long at = differs(out.contents, b, MW, MH, MW, &got, &want);
        if (at >= 0) {
            nm_fail(name, "round %u: the blit after two writes: texel %ld is %08x, %08x was written last", i, at, got,
                    want);
            wrong++;
            continue;
        }
        // the lower half written again with texture c's values, the upper half keeps b's
        fill(part, c, MW, MH, MW);
        [t replaceRegion:MTLRegionMake2D(0, MH / 2, MW, MH / 2)
             mipmapLevel:0
               withBytes:part + (size_t)(MH / 2) * MW
             bytesPerRow:MW * 4];
        memset(scratch, 0, (size_t)MW * MH * 4);
        [t getBytes:scratch bytesPerRow:MW * 4 fromRegion:MTLRegionMake2D(0, 0, MW, MH) mipmapLevel:0];
        for (uint32_t y = 0; y < MH && at < 0; y++)
            for (uint32_t x = 0; x < MW; x++) {
                const uint32_t w = texel(y < MH / 2 ? b : c, x, y);
                if (scratch[y * MW + x] != w) {
                    nm_fail(name,
                            "round %u: getBytes after a write of the lower half: texel (%u,%u) is %08x, %08x "
                            "was written",
                            i, x, y, scratch[y * MW + x], w);
                    at = 0;
                    wrong++;
                    break;
                }
            }
    }
    free(part);
    if (!wrong) nm_pass(name, "50 rounds of two whole writes and a write of half, read by a blit and getBytes");
}

typedef struct {
    __unsafe_unretained id<MTLTexture> t;
    uint32_t base;
} race_arg;

static void *racer(void *p)
{
    race_arg *a = p;
    uint32_t *scratch = malloc((size_t)MW * MH * 4);
    if (!scratch) return NULL;
    for (uint32_t i = 0; i < 200; i++) @autoreleasepool {
            upload(a->t, a->base + i, scratch);
        }
    free(scratch);
    return NULL;
}

static void case_race(uint32_t *scratch)
{
    const char *name = "race";
    id<MTLTexture> t = texture(MTLPixelFormatRGBA8Unorm, MW, MH);
    if (!t) {
        nm_setup(name, "texture");
        return;
    }
    race_arg args[2] = {{t, 20000}, {t, 30000}};
    pthread_t th[2];
    unsigned started = 0;
    for (unsigned i = 0; i < 2; i++)
        if (!pthread_create(&th[i], NULL, racer, &args[i])) started++;
    for (unsigned i = 0; i < started; i++) pthread_join(th[i], NULL);
    if (started != 2) {
        nm_setup(name, "%u of 2 threads started", started);
        return;
    }
    upload(t, 40000, scratch);
    id<MTLBuffer> out = blit_back(@[ t ], name);
    if (!out) return;
    uint32_t got = 0, want = 0;
    long at = differs(out.contents, 40000, MW, MH, MW, &got, &want);
    if (at >= 0)
        nm_fail(name, "the blit after the threads and a last write: texel %ld is %08x, %08x was written last", at, got,
                want);
    else if (read_back(t, 40000, name, "after the threads and a last write", scratch))
        nm_pass(name,
                "two threads wrote one texture 200 times each at once; the last write is what a blit and getBytes "
                "read");
}

static void case_destroy(uint32_t *scratch)
{
    const char *name = "destroy";
    NSMutableArray<id<MTLTexture>> *previous = [NSMutableArray array]; // the texture made before this one
    unsigned wrong = 0, read = 0;
    for (uint32_t i = 0; i < 500 && wrong < 4; i++) @autoreleasepool {
            id<MTLTexture> t = texture(MTLPixelFormatRGBA8Unorm, 64, 64);
            if (!t) {
                nm_setup(name, "texture %u", i);
                return;
            }
            upload(t, 50000 + i, scratch);
            [previous removeAllObjects]; // the one before is freed with its upload perhaps still queued
            if (i % 50 == 49) {
                read++;
                if (!read_back(t, 50000 + i, name, "a texture made after others were freed", scratch)) wrong++;
            }
            [previous addObject:t];
        }
    [previous removeAllObjects];
    if (!wrong)
        nm_pass(name,
                "500 textures written and freed with no read; the %u read before they were freed held "
                "what was written",
                read);
}

static IOSurfaceRef make_surface(void)
{
    return IOSurfaceCreate((__bridge CFDictionaryRef) @{
        (id)kIOSurfaceWidth : @(SW),
        (id)kIOSurfaceHeight : @(SH),
        (id)kIOSurfaceBytesPerElement : @4,
        (id)kIOSurfaceBytesPerRow : @(SW * 4),
        (id)kIOSurfaceAllocSize : @(SW * 4 * SH),
        (id)kIOSurfacePixelFormat : @(0x42475241) // 'BGRA'
    });
}

static void case_iosurface(void)
{
    const char *name = "iosurface";
    IOSurfaceRef s = make_surface();
    if (!s || IOSurfaceGetBytesPerRow(s) != SW * 4) {
        nm_setup(name, "IOSurfaceCreate of %ux%u with %u-byte rows", SW, SH, SW * 4);
        if (s) CFRelease(s);
        return;
    }
    IOSurfaceLock(s, 0, NULL);
    memset(IOSurfaceGetBaseAddress(s), 0x55, (size_t)SW * 4 * SH);
    IOSurfaceUnlock(s, 0, NULL);
    MTLTextureDescriptor *d = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatBGRA8Unorm
                                                                                 width:SW
                                                                                height:SH
                                                                             mipmapped:NO];
    d.storageMode = MTLStorageModeShared;
    d.usage = MTLTextureUsageShaderRead | MTLTextureUsagePixelFormatView;
    id<MTLTexture> t = [device newTextureWithDescriptor:d iosurface:s plane:0];
    id<MTLTexture> view = [t newTextureViewWithPixelFormat:MTLPixelFormatBGRA8Unorm];
    uint32_t *scratch = malloc((size_t)SW * SH * 4);
    if (!t || !view || !scratch) {
        nm_setup(name, "%s", !t ? "a texture of the surface" : !view ? "a view of it" : "memory");
        free(scratch);
        CFRelease(s);
        return;
    }
    unsigned wrong = 0;
    for (uint32_t i = 0; i < 40 && wrong < 4; i++) {
        id<MTLTexture> through = i % 2 ? view : t;
        const uint32_t n = 60000 + i;
        upload(through, n, scratch);
        if (IOSurfaceLock(s, kIOSurfaceLockReadOnly, NULL) != kIOReturnSuccess) {
            nm_fail(name, "IOSurfaceLock failed");
            wrong++;
            break;
        }
        uint32_t got = 0, want = 0;
        long at = differs(IOSurfaceGetBaseAddress(s), n, SW, SH, IOSurfaceGetBytesPerRow(s) / 4, &got, &want);
        IOSurfaceUnlock(s, kIOSurfaceLockReadOnly, NULL);
        if (at >= 0) {
            nm_fail(name, "write %u through the %s: the CPU reads texel %ld as %08x, %08x was written", i,
                    i % 2 ? "view" : "texture", at, got, want);
            wrong++;
        }
    }
    free(scratch);
    t = nil;
    view = nil;
    CFRelease(s);
    if (!wrong)
        nm_pass(name, "40 writes through a texture of an IOSurface and a view of it, each read by the CPU at once");
}

static void case_gating(void)
{
    const char *name = "gating";
    const char *zero = getenv("NVMTL_SURFIN_ZERO"), *offmain = getenv("NVMTL_WS_FREE_OFFMAIN");
    if (!zero || !offmain) return; // only the row that sets them has this case
    NSArray<NSString *> *lines = nm_log_lines();
    NSString *z = nm_log_find(lines, "surface-in zero: a surface whose pages are all zero is ", NULL);
    NSString *census = nm_log_find(lines, "surface-in zero census:", NULL);
    NSString *f = nm_log_find(lines, "vk: off-main frees of dead VRAM imports ", NULL);
    unsigned bad = 0;
    if (z && [z rangeOfString:@" is uploaded as any other ("].location == NSNotFound) {
        nm_fail(name, "the driver says \"%s\" outside WindowServer", z.UTF8String);
        bad++;
    }
    if (census) {
        nm_fail(name, "the driver counts zero surfaces outside WindowServer: %s", census.UTF8String);
        bad++;
    }
    if (f && ![f hasPrefix:@"vk: off-main frees of dead VRAM imports OFF ("]) {
        nm_fail(name, "the driver says \"%s\" outside WindowServer", f.UTF8String);
        bad++;
    }
    if (!z) nm_info("gating: the driver wrote no line of NVMTL_SURFIN_ZERO (no surface-in reached it)");
    if (!f) nm_info("gating: the driver wrote no line of NVMTL_WS_FREE_OFFMAIN (no VRAM import was freed)");
    if (!bad)
        nm_pass(name, "NVMTL_SURFIN_ZERO=%s and NVMTL_WS_FREE_OFFMAIN=%s are off outside WindowServer", zero, offmain);
}

int main(int argc, char **argv)
{
    @autoreleasepool {
        nm_start(60);
        const BOOL on = argc > 1 && !strcmp(argv[1], "on");
        if (argc < 2 || (!on && strcmp(argv[1], "off"))) {
            printf("FAIL usage: v301_async_upload on|off\n");
            return 2;
        }
        const char *process_env = getenv("NVMTL_PROCESS_ENV"), *log_path = getenv("NVMTL_LOG_PATH");
        if (!process_env || strcmp(process_env, "1") || !log_path || !*log_path)
            nm_skip("needs NVMTL_PROCESS_ENV=1 and NVMTL_LOG_PATH: the driver reads the switch and writes its log "
                    "only then");
        device = nm_device();
        // after the device is made, the environment holds what the settings file set too (the environment wins)
        const char *e = getenv("NVMTL_ASYNC_TEX_UPLOAD");
        if (on && e)
            nm_skip("NVMTL_ASYNC_TEX_UPLOAD=%s is set (by the settings file or the environment): the default this row "
                    "checks does not run",
                    e);
        if (!on && !(e && !strcmp(e, "0")))
            nm_skip("NVMTL_ASYNC_TEX_UPLOAD is %s, not 0: the way back this row checks does not run", e ? e : "unset");
        salt = (uint32_t)getpid() * 2654435761u;
        queue = [device newCommandQueue];
        uint32_t *scratch = malloc((size_t)MW * MH * 4);
        if (!queue || !scratch) {
            nm_setup("start", "%s", queue ? "memory" : "a command queue");
            return nm_finish();
        }
        nm_info("NVMTL_ASYNC_TEX_UPLOAD=%s: replaceRegion is expected %s", e ? e : "unset",
                on ? "to return before its copy has run (the 3.0.1 default)" : "to wait for its copy (as in 3.0)");
        // many runs first: the pending uploads it counts are then its own
        case_many(scratch);
        case_limit(on);
        case_switch(on);
        case_overwrite(scratch);
        case_race(scratch);
        case_destroy(scratch);
        case_iosurface();
        case_gating();
        free(scratch);
        return nm_finish();
    }
}
