/* xlate_paths.m: compute checks, on the Metal device the system gives (it must be NVMTLDevice), of shader translator
 * repairs that no other program checks on the card. tools/card/checks_xlate.py builds it as {BIN}/<id>/nvmtltest and
 * runs it with the cases of one row (no case named: every case):
 *
 *   abarray-a abarray-c abarray-s  a texture picked by a run-time index from an argument buffer's array of eight:
 *                                  array<texture2d<float>, 8> and the C array texture2d<float> t [[id(0)]] [8] (both
 *                                  read), and a nearest sample through the first form; each index 0-7 in its own
 *                                  dispatch
 *   tbuf                           a single texture_buffer field of an argument buffer, read by 16 threads
 *   phi8 phi9 phi16                a sample through a sampler that a switch picks among 8, 9 and 16 samplers, each
 *                                  pick compared with the direct sample through that sampler
 *   walk                           a loop that picks its next buffer through a pointer phi on its back edge
 *   esc                            a device pointer read from a buffer and passed to a helper marked noinline
 *   pack10 pack565                 pack_float_to_unorm10a2 near every half-way value (and their float neighbours),
 *                                  RGB565 packing of every half value in [0, 1] (half3 forms first, float3 last), and
 *                                  controls (NaN, clamps)
 *   splat                          a helper marked noinline called with a vector splat constant
 *   localfield                     textures and samplers kept in a local array of structs and read by a helper
 *   lanes                          an int2 read through a float4 pointer at a run-time index
 *
 * Each case compiles its own library. When Apple's Metal compiler refuses a case's source (its error text names
 * "program_source:"), that case is a SKIP with the compiler's first error line: the shape cannot be written that way
 * for this compiler, which says nothing about the driver. A refusal by the driver (the library, the function or the
 * pipeline) is that case's FAIL. When NVMTL_AIR_DUMP names a folder, the plugin writes there the AIR text of each
 * function the program asks for (plugin/NVMTLObjects.m, nvmtl_air_dump), and the program reports as INFO lines
 * whether the shape a repair handles is in it, also for a function the driver then refuses: Apple's compiler may emit
 * another shape, and a PASS then says less.
 *
 * Output: one line per case, "PASS <case> ...", "FAIL <case>: ...", "SKIP <case>: ...", INFO lines, then
 * "INFO translator: ..." (the plugin's own line that names the translator it opened in this process, by its LC_UUID
 * and path: the plugin opens the libnvmtl_translate.dylib beside it in NVMTLDriver.bundle/Contents/MacOS when that file
 * is there, else nvmtl/libnvmtl_translate.dylib, so the results are that translator's), and last
 * "RESULT pass|fail|skip pass=N fail=N skip=N". Exit 0 every case that ran passed, 1 a case failed, 2 usage or a setup
 * error only (the program could not make its own objects), 3 skip (no device of this driver: first line "SKIP: ...",
 * or every case skipped). It has its own alarm (ALARM_SECONDS); a command buffer that does not complete within
 * GPU_WAIT_SECONDS ends the program by SIGALRM, as the alarm would, so that the runner stops every later GPU check.
 * A crash prints "CRASH <signal>" and a backtrace on stderr and exits 70. The program writes no file and changes no
 * setting; it reads only the AIR text and the plugin log of its own process, in the folders the runner named.
 * It has run once, on one Mac, on macOS 26.7.3 (25G342) with release 3.0.1's package, before abarray-c's declaration
 * was respelled and the INFO translator line was added.
 *
 * Never run here: an index past 0 of an array of texture buffers, and anything under NVMTL_NO_TIER2_FIXED, whose old
 * path declares one descriptor for a whole argument-buffer texture array (an index past 0 would read past it); the
 * abarray cases are skipped when this process has that switch on (nvrm610.conf can set it).
 */
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <dispatch/dispatch.h>
#include <execinfo.h>
#include <math.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define ALARM_SECONDS 75
#define GPU_WAIT_SECONDS 20
#define GUARD 0xa5a5a5a5u

static unsigned g_pass, g_fail, g_skip, g_setup;
static id<MTLDevice> g_device;
static id<MTLCommandQueue> g_queue;

/* ---- crashes, output --------------------------------------------------------------------------------------------- */

static void crashed(int sig)
{
    const char *name = sig == SIGSEGV ? "SIGSEGV" : sig == SIGBUS ? "SIGBUS" : sig == SIGILL ? "SIGILL"
                     : sig == SIGFPE ? "SIGFPE" : "SIGABRT";
    ssize_t r = write(2, "CRASH ", 6);
    r = write(2, name, strlen(name));
    r = write(2, "\n", 1);
    (void)r;
    void *frames[64];
    int n = backtrace(frames, 64);
    backtrace_symbols_fd(frames, n, 2);
    _exit(70);
}

__attribute__((format(printf, 3, 0))) static void emit(const char *head, const char *name, const char *fmt, va_list ap)
{
    char text[2048];
    int n = snprintf(text, sizeof text, "%s%s", head, name ? name : "");
    if (n < 0) n = 0;
    if ((size_t)n >= sizeof text) n = (int)sizeof text - 1;
    vsnprintf(text + n, sizeof text - (size_t)n, fmt, ap);
    for (char *p = text; *p; p++) if (*p == '\n' || *p == '\r' || *p == '\t') *p = ' ';
    printf("%s\n", text);
    fflush(stdout);
}

__attribute__((format(printf, 2, 3))) static void pass(const char *c, const char *fmt, ...)
{
    va_list ap; va_start(ap, fmt);
    char head[96]; snprintf(head, sizeof head, "PASS %s ", c);
    emit(head, NULL, fmt, ap); va_end(ap); g_pass++;
}

__attribute__((format(printf, 2, 3))) static void fail(const char *c, const char *fmt, ...)
{
    va_list ap; va_start(ap, fmt);
    char head[96]; snprintf(head, sizeof head, "FAIL %s: ", c);
    emit(head, NULL, fmt, ap); va_end(ap); g_fail++;
}

/* a FAIL because the program could not make its own objects (a buffer, a texture): exit 2 when nothing else failed */
__attribute__((format(printf, 2, 3))) static void setup_fail(const char *c, const char *fmt, ...)
{
    va_list ap; va_start(ap, fmt);
    char head[96]; snprintf(head, sizeof head, "FAIL %s: setup: ", c);
    emit(head, NULL, fmt, ap); va_end(ap); g_fail++; g_setup++;
}

__attribute__((format(printf, 2, 3))) static void skip(const char *c, const char *fmt, ...)
{
    va_list ap; va_start(ap, fmt);
    char head[96]; snprintf(head, sizeof head, "SKIP %s: ", c);
    emit(head, NULL, fmt, ap); va_end(ap); g_skip++;
}

__attribute__((format(printf, 1, 2))) static void info(const char *fmt, ...)
{
    va_list ap; va_start(ap, fmt);
    emit("INFO ", NULL, fmt, ap); va_end(ap);
}

static void result_line(void)
{
    const char *word = g_fail ? "fail" : g_pass ? "pass" : "skip";
    printf("RESULT %s pass=%u fail=%u skip=%u%s\n", word, g_pass, g_fail, g_skip,
           !g_fail && !g_pass ? " (every case skipped; its SKIP line says why)" : "");
    fflush(stdout);
}

/* the first line of a compiler error text that says "error:", else its first line */
static void first_error(NSString *text, char *out, size_t size)
{
    NSString *pick = nil;
    for (NSString *l in [text componentsSeparatedByString:@"\n"]) {
        if (!pick && l.length) pick = l;
        if ([l rangeOfString:@"error:"].location != NSNotFound) { pick = l; break; }
    }
    snprintf(out, size, "%s", (pick ?: @"(no text)").UTF8String);
}

/* ---- objects ----------------------------------------------------------------------------------------------------- */

static id<MTLBuffer> buffer(NSUInteger length, uint8_t fill)
{
    id<MTLBuffer> b = [g_device newBufferWithLength:length options:MTLResourceStorageModeShared];
    if (b) memset(b.contents, fill, length);
    return b;
}

static id<MTLTexture> texture_rgba32f(NSUInteger w, NSUInteger h, const float *rgba)
{
    MTLTextureDescriptor *d = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA32Float
                                                                                 width:w height:h mipmapped:NO];
    d.storageMode = MTLStorageModeShared;
    d.usage = MTLTextureUsageShaderRead;
    id<MTLTexture> t = [g_device newTextureWithDescriptor:d];
    if (t) [t replaceRegion:MTLRegionMake2D(0, 0, w, h) mipmapLevel:0 withBytes:rgba bytesPerRow:w * 16];
    return t;
}

static id<MTLSamplerState> sampler_state(MTLSamplerAddressMode s, MTLSamplerAddressMode t, MTLSamplerMinMagFilter f)
{
    MTLSamplerDescriptor *d = [MTLSamplerDescriptor new];
    d.sAddressMode = s;
    d.tAddressMode = t;
    d.minFilter = d.magFilter = f;
    d.normalizedCoordinates = YES;
    return [g_device newSamplerStateWithDescriptor:d];
}

/* what the last shape() call found: 1 every marker in the AIR text, 0 one or more absent, -1 not known (no dump) */
static int g_shape = -1;

/* Reports, for each marker, whether the AIR text of `name` holds it (only when the row set NVMTL_AIR_DUMP). */
static void shape(const char *c, NSString *name, NSArray<NSString *> *markers)
{
    g_shape = -1;
    const char *dir = getenv("NVMTL_AIR_DUMP");
    if (!markers.count || !dir || !*dir) return;
    NSString *path = [[@(dir) stringByAppendingPathComponent:name] stringByAppendingPathExtension:@"ll"];
    NSString *air = [NSString stringWithContentsOfFile:path encoding:NSUTF8StringEncoding error:nil];
    if (!air) { info("%s shape: no AIR text of %s in the dump folder", c, name.UTF8String); return; }
    g_shape = 1;
    for (NSString *m in markers) {
        const BOOL here = [air rangeOfString:m].location != NSNotFound;
        if (!here) g_shape = 0;
        info("%s shape: '%s' %s in the AIR of %s", c, m.UTF8String, here ? "present" : "absent", name.UTF8String);
    }
}

/* The plugin's log of this process (NVMTL_LOG_PATH, which the runner sets inside the run folder), opened for reading,
   or NULL: *set says whether the variable names a file at all. Both readers below read it with stdio, one whole line
   at a time (getline, whatever its length), never as one UTF-8 string: a log line the plugin cut at its buffer's end
   (nvlog's vsnprintf) may end inside a UTF-8 character, which a whole-file UTF-8 read refuses, and a fixed buffer
   would take the rest of a long line for a line of its own. */
static FILE *plugin_log(BOOL *set)
{
    const char *path = getenv("NVMTL_LOG_PATH");
    *set = path && *path;
    return *set ? fopen(path, "r") : NULL;
}

/* Reports whether a line of the plugin's log of this process holds `needle`. */
static void log_line_info(const char *c, NSString *needle)
{
    BOOL set;
    FILE *f = plugin_log(&set);
    if (!set) return;
    if (!f) { info("%s: the plugin log could not be read", c); return; }
    const char *want = needle.UTF8String;
    char *line = NULL;
    size_t cap = 0;
    BOOL has = NO;
    while (!has && getline(&line, &cap, f) >= 0)
        has = strstr(line, want) != NULL;
    const BOOL bad = !has && ferror(f);
    free(line);
    fclose(f);
    if (bad) info("%s: the plugin log could not be read", c);
    else info("%s: the plugin log %s '%s'", c, has ? "has" : "has no", want);
}

/* Repeats, as "INFO translator: ...", the plugin's line of this process ("pid <pid> NVMTL: " in NVMTL_LOG_PATH) that
   names the translator it opened, "translate: opened <LC_UUID> <path> (cache tag ...)", or the line of a dlopen that
   failed, "translate: dlopen(<path>): <dlerror>". The plugin writes it once, at its first translation
   (plugin/NVMTLLibraryLoad.m, nvmtl_xlate_ready). The dlopen line names nvmtl/'s path (NVMTL_XLATE_LIB), the
   fallback, whichever copy the plugin tried: when the copy beside the plugin failed to open, only dlerror's text after
   it names that copy. */
static void translator_info(void)
{
    BOOL set;
    FILE *f = plugin_log(&set);
    if (!set) return;
    if (!f) { info("translator: the plugin log could not be read"); return; }
    char mine[64];
    snprintf(mine, sizeof mine, "pid %d NVMTL: ", (int)getpid());
    const size_t n = strlen(mine);
    char *line = NULL;
    size_t cap = 0;
    BOOL found = NO;
    while (!found && getline(&line, &cap, f) >= 0) {
        if (strncmp(line, mine, n)) continue;
        const char *t = line + n;
        if (strncmp(t, "translate: opened ", 18) && strncmp(t, "translate: dlopen(", 18)) continue;
        line[strcspn(line, "\r\n")] = 0;
        info("translator: %s", t);
        found = YES;
    }
    const BOOL bad = !found && ferror(f);
    free(line);
    fclose(f);
    if (bad)
        info("translator: the plugin log could not be read");
    else if (!found)
        info("translator: no 'translate: opened' line of this process in the plugin log (the plugin opens its "
             "translator at its first translation)");
}

typedef enum { BUILD_OK, BUILD_SKIP, BUILD_FAIL } build_status;

/* Compiles one case's source and makes the compute pipeline of `name`; on SKIP or FAIL, `why` says why. */
static build_status build(const char *c, NSString *body, NSString *name, NSArray<NSString *> *markers,
                          id<MTLComputePipelineState> __strong *pso, id<MTLFunction> __strong *fn, char *why,
                          size_t size)
{
    NSString *src = [@"#include <metal_stdlib>\nusing namespace metal;\n" stringByAppendingString:body];
    NSError *e = nil;
    id<MTLLibrary> lib = [g_device newLibraryWithSource:src options:nil error:&e];
    if (!lib) {
        NSString *d = e.localizedDescription ?: @"(no reason given)";
        char first[1024];
        first_error(d, first, sizeof first);
        if ([d rangeOfString:@"program_source:"].location != NSNotFound) {
            snprintf(why, size, "Apple's Metal compiler refused this case's source: %s", first);
            return BUILD_SKIP;
        }
        snprintf(why, size, "the driver refused the library: %s", first);
        return BUILD_FAIL;
    }
    id<MTLFunction> f = [lib newFunctionWithName:name];
    // the plugin writes a function's AIR text when it is asked for, before it translates it (nvmtl_air_dump at the
    // top of newFunctionWithName), so the shape of a function it then refuses is reported too
    shape(c, name, markers);
    if (!f) {
        snprintf(why, size, "the driver gave no function %s (it could not translate it; the plugin log says why)",
                 name.UTF8String);
        return BUILD_FAIL;
    }
    id<MTLComputePipelineState> p = [g_device newComputePipelineStateWithFunction:f error:&e];
    if (!p) {
        char first[1024];
        first_error(e.localizedDescription ?: @"(no reason given)", first, sizeof first);
        snprintf(why, size, "no compute pipeline for %s: %s", name.UTF8String, first);
        return BUILD_FAIL;
    }
    *pso = p;
    if (fn) *fn = f;
    return BUILD_OK;
}

/* build() that reports its own SKIP or FAIL line: -> the pipeline, or nil when the case ends here */
static id<MTLComputePipelineState> build_case(const char *c, NSString *body, NSString *name,
                                              NSArray<NSString *> *markers, id<MTLFunction> __strong *fn)
{
    id<MTLComputePipelineState> p = nil;
    char why[1400];
    build_status st = build(c, body, name, markers, &p, fn, why, sizeof why);
    if (st == BUILD_SKIP) skip(c, "%s", why);
    else if (st == BUILD_FAIL) fail(c, "%s", why);
    return st == BUILD_OK ? p : nil;
}

/* Commits and waits at most GPU_WAIT_SECONDS. A command buffer that never completes means a GPU that may be hung:
 * the program then ends by SIGALRM, as at its alarm, and the runner stops every later GPU check. */
static BOOL finish(const char *c, id<MTLCommandBuffer> cb)
{
    dispatch_semaphore_t done = dispatch_semaphore_create(0);
    [cb addCompletedHandler:^(id<MTLCommandBuffer> b) { (void)b; dispatch_semaphore_signal(done); }];
    [cb commit];
    const dispatch_time_t limit = dispatch_time(DISPATCH_TIME_NOW, (int64_t)GPU_WAIT_SECONDS * (int64_t)NSEC_PER_SEC);
    if (dispatch_semaphore_wait(done, limit)) {
        fail(c, "the command buffer did not complete within %d s; ending as at the alarm", GPU_WAIT_SECONDS);
        result_line();
        signal(SIGALRM, SIG_DFL);
        raise(SIGALRM);
        _exit(1);
    }
    if (cb.status != MTLCommandBufferStatusCompleted) {
        char first[1024];
        first_error(cb.error.localizedDescription ?: @"(no error object)", first, sizeof first);
        fail(c, "command buffer status %ld: %s", (long)cb.status, first);
        return NO;
    }
    return YES;
}

static void one_thread(id<MTLComputeCommandEncoder> enc)
{
    [enc dispatchThreadgroups:MTLSizeMake(1, 1, 1) threadsPerThreadgroup:MTLSizeMake(1, 1, 1)];
}

static BOOL guards_intact(const void *base, size_t from_word, size_t to_word)
{
    const uint32_t *w = base;
    for (size_t i = from_word; i < to_word; i++) if (w[i] != GUARD) return NO;
    return YES;
}

/* ---- argument-buffer texture arrays by a run-time index ---------------------------------------------------------- */

/* stage_input/mod.rs routes each element of an argument-buffer texture array through the bindless image heap by its
   own handle. NVMTL_NO_TIER2_FIXED keeps the old single-binding path, where an index past 0 reads past the array's
   one descriptor; with that switch on in this process (from its environment or nvrm610.conf) the case is not run. */
static BOOL old_texture_array_path(const char *c)
{
    const char *v = getenv("NVMTL_NO_TIER2_FIXED");
    if (v && *v && strcmp(v, "0")) {
        skip(c, "NVMTL_NO_TIER2_FIXED=%s in this process: its old path has one descriptor for the whole array, so an "
             "index past 0 would read past it; not run", v);
        return YES;
    }
    return NO;
}

static void case_abarray(const char *c)
{
    if (old_texture_array_path(c)) return;
    NSString *name, *body;
    if (!strcmp(c, "abarray-a")) {
        name = @"pick_a";
        body = @"struct A { array<texture2d<float>, 8> t [[id(0)]]; };\n"
                "kernel void pick_a(constant A& g [[buffer(0)]], constant uint& k [[buffer(1)]],\n"
                "                   device float4* o [[buffer(2)]]) { o[0] = g.t[k].read(uint2(0)); }\n";
    } else if (!strcmp(c, "abarray-c")) {
        name = @"pick_c";
        // the attribute goes right after the name: after "[8]" it would belong to the array type, which Apple's
        // compiler refuses ("'id' attribute cannot be applied to types")
        body = @"struct C { texture2d<float> t [[id(0)]] [8]; };\n"
                "kernel void pick_c(constant C& g [[buffer(0)]], constant uint& k [[buffer(1)]],\n"
                "                   device float4* o [[buffer(2)]]) { o[0] = g.t[k].read(uint2(0)); }\n";
    } else {
        name = @"pick_s";
        body = @"struct S { array<texture2d<float>, 8> t [[id(0)]]; };\n"
                "kernel void pick_s(constant S& g [[buffer(0)]], constant uint& k [[buffer(1)]],\n"
                "                   device float4* o [[buffer(2)]]) {\n"
                "  constexpr sampler s(coord::normalized, address::clamp_to_edge, filter::nearest);\n"
                "  o[0] = g.t[k].sample(s, float2(0.5f), level(0)); }\n";
    }
    id<MTLFunction> fn = nil;
    id<MTLComputePipelineState> pso = build_case(c, body, name, @[], &fn);
    if (!pso) return;
    float texel[8][4];
    id<MTLTexture> tex[8];
    for (unsigned k = 0; k < 8; k++) {
        texel[k][0] = (float)k; texel[k][1] = (float)k + 0.25f; texel[k][2] = 100.f + (float)k; texel[k][3] = 1.f;
        tex[k] = texture_rgba32f(1, 1, texel[k]);
        if (!tex[k]) { setup_fail(c, "texture %u", k); return; }
    }
    id<MTLArgumentEncoder> ae = [fn newArgumentEncoderWithBufferIndex:0];
    if (!ae) { fail(c, "the driver gave no argument encoder for buffer 0 of %s", name.UTF8String); return; }
    // at least the eight handles' 64 bytes, whatever encodedLength says, so that the shader reads this buffer only
    id<MTLBuffer> args = buffer(ae.encodedLength > 64 ? ae.encodedLength : 64, 0);
    id<MTLBuffer> ks = buffer(8 * 256, 0), out = buffer(8 * 256, 0xa5);
    if (!args || !ks || !out) { setup_fail(c, "buffers"); return; }
    [ae setArgumentBuffer:args offset:0];
    for (unsigned k = 0; k < 8; k++) {
        [ae setTexture:tex[k] atIndex:k];
        *(uint32_t *)((char *)ks.contents + 256 * k) = k;
    }
    // This driver's encoder writes a texture as one 8-byte handle word, element k at byte 8k, where the shader reads
    // it; a word left 0 is an element the encoder did not write (the plugin log says why, "REFUSED").
    const uint64_t *handles = args.contents;
    unsigned written = 0;
    for (unsigned k = 0; k < 8; k++) written += handles[k] != 0;
    info("%s encoder: encodedLength %lu, %u of 8 handle words written", c, (unsigned long)ae.encodedLength, written);
    if (ae.encodedLength < 64)
        info("%s encoder: encodedLength %lu is below the 64 bytes of eight handles", c,
             (unsigned long)ae.encodedLength);
    id<MTLCommandBuffer> cb = [g_queue commandBuffer];
    id<MTLComputeCommandEncoder> enc = [cb computeCommandEncoder];
    [enc setComputePipelineState:pso];
    [enc setBuffer:args offset:0 atIndex:0];
    for (unsigned k = 0; k < 8; k++) [enc useResource:tex[k] usage:MTLResourceUsageRead];
    for (unsigned k = 0; k < 8; k++) {
        // a uniform index per dispatch: a divergent one would also need NonUniform, which is not this repair
        [enc setBuffer:ks offset:256 * k atIndex:1];
        [enc setBuffer:out offset:256 * k atIndex:2];
        one_thread(enc);
    }
    [enc endEncoding];
    if (!finish(c, cb)) return;
    // the plugin logs "tier2: <function> reads N argument-buffer texture(s) through the bindless heap" when the module
    // reads them by their handles (plugin/NVMTLEmbedded.m, nvmtl_embedded_plan). It counts only the reflection's
    // argument-buffer textures (entries with an embedded_source) whose binding the module does not declare, so the line
    // is missing both when the module declares that binding and when the reflection names no such texture
    log_line_info(c, [NSString stringWithFormat:@"tier2: %@ reads", name]);
    static const float zero4[4] = {0};
    unsigned bad = 0, texel0 = 0, zeros = 0, first = 8;
    float got[4] = {0};
    for (unsigned k = 0; k < 8; k++) {
        const char *slot = (const char *)out.contents + 256 * k;
        BOOL ok = !memcmp(slot, texel[k], 16) && guards_intact(slot, 4, 64);
        if (k && !memcmp(slot, texel[0], 16)) texel0++;
        if (k && !memcmp(slot, zero4, 16)) zeros++;
        if (!ok && first == 8) { first = k; memcpy(got, slot, 16); }
        bad += !ok;
    }
    if (!bad) { pass(c, "indices 0-7 each read their own texture (8 dispatches)"); return; }
    // Past one binding's one descriptor (plugin/nvmtl_vk.c gives each binding one) an index reads the next bindings'
    // descriptors, which this program leaves empty, so zeros; bounds-checked compute (NVMTL_COMPUTE_ROBUST=1, or no
    // VK_EXT_pipeline_robustness) holds the index at element 0 instead.
    fail(c, "%u of 8 indices wrong; first k=%u got=(%g,%g,%g,%g) expected=(%g,%g,%g,%g)%s", bad, first, got[0], got[1],
         got[2], got[3], texel[first][0], texel[first][1], texel[first][2], texel[first][3],
         texel0 == 7 ? "; every index read texture 0: the index did not reach the read (one binding for the whole "
                       "array, its index held at element 0 by bounds-checked compute)"
         : zeros == 7 ? "; every index past 0 read zeros: handle words the encoder left 0 (its INFO line above), or an "
                        "index past the one descriptor of a binding the module declares for the array (no 'tier2:' "
                        "line above: the module declares the array's binding, or the reflection names no "
                        "argument-buffer texture)"
         : "");
}

/* ---- a single texture_buffer field ------------------------------------------------------------------------------- */

static void case_tbuf(const char *c)
{
    NSString *body = @"struct T { texture_buffer<float> tb [[id(0)]]; };\n"
                      "kernel void tb_read(constant T& g [[buffer(0)]], device float4* o [[buffer(1)]],\n"
                      "                    uint i [[thread_position_in_grid]]) { o[i] = g.tb.read(i); }\n";
    id<MTLFunction> fn = nil;
    id<MTLComputePipelineState> pso = build_case(c, body, @"tb_read", @[], &fn);
    if (!pso) return;
    id<MTLBuffer> data = buffer(256, 0), out = buffer(20 * 16, 0xa5);
    if (!data || !out) { setup_fail(c, "buffers"); return; }
    float *v = data.contents;
    for (unsigned i = 0; i < 16; i++) {
        v[4 * i] = (float)i; v[4 * i + 1] = -(float)(i + 1); v[4 * i + 2] = 0.5f * (float)i + 0.25f; v[4 * i + 3] = 1.f;
    }
    MTLTextureDescriptor *td = [MTLTextureDescriptor textureBufferDescriptorWithPixelFormat:MTLPixelFormatRGBA32Float
        width:16 resourceOptions:MTLResourceStorageModeShared usage:MTLTextureUsageShaderRead];
    id<MTLTexture> tb = [data newTextureWithDescriptor:td offset:0 bytesPerRow:256];
    if (!tb) { fail(c, "the driver made no texture buffer over a shared buffer (RGBA32Float, 16 texels)"); return; }
    id<MTLArgumentEncoder> ae = [fn newArgumentEncoderWithBufferIndex:0];
    if (!ae) { fail(c, "the driver gave no argument encoder for buffer 0 of tb_read"); return; }
    id<MTLBuffer> args = buffer(ae.encodedLength ? ae.encodedLength : 16, 0);
    if (!args) { setup_fail(c, "argument buffer"); return; }
    [ae setArgumentBuffer:args offset:0];
    [ae setTexture:tb atIndex:0];
    id<MTLCommandBuffer> cb = [g_queue commandBuffer];
    id<MTLComputeCommandEncoder> enc = [cb computeCommandEncoder];
    [enc setComputePipelineState:pso];
    [enc setBuffer:args offset:0 atIndex:0];
    [enc setBuffer:out offset:0 atIndex:1];
    [enc useResource:tb usage:MTLResourceUsageRead];
    [enc useResource:data usage:MTLResourceUsageRead];
    [enc dispatchThreadgroups:MTLSizeMake(1, 1, 1) threadsPerThreadgroup:MTLSizeMake(16, 1, 1)];
    [enc endEncoding];
    if (!finish(c, cb)) return;
    const float *o = out.contents;
    unsigned bad = 0, zeros = 0, first = 16;
    for (unsigned i = 0; i < 16; i++) {
        if (memcmp(&o[4 * i], &v[4 * i], 16)) { bad++; if (first == 16) first = i; }
        if (o[4 * i] == 0.f && o[4 * i + 1] == 0.f && o[4 * i + 2] == 0.f && o[4 * i + 3] == 0.f) zeros++;
    }
    BOOL guards = guards_intact(o, 64, 80);
    if (!bad && guards) { pass(c, "16 texels read bit for bit, guards intact"); return; }
    if (bad)
        fail(c, "%u of 16 texels wrong; first i=%u got=(%g,%g,%g,%g) expected=(%g,%g,%g,%g)%s%s", bad, first,
             o[4 * first], o[4 * first + 1], o[4 * first + 2], o[4 * first + 3], v[4 * first], v[4 * first + 1],
             v[4 * first + 2], v[4 * first + 3], zeros == 16 ? "; all zero, the dummy texel view's signature" : "",
             guards ? "" : "; guards overwritten");
    else fail(c, "the texels are right but o[16..19] were written got=%08x expected=%08x",
              ((const uint32_t *)o)[64], GUARD);
}

/* ---- a sampler picked among 8, 9 or 16 --------------------------------------------------------------------------- */

static NSString *phi_source(unsigned n, BOOL pick, BOOL ternary)
{
    NSMutableString *s = [NSMutableString new];
    [s appendFormat:@"kernel void %@%u(texture2d<float> t [[texture(0)]]", pick ? @"pick" : @"direct", n];
    for (unsigned j = 0; j < n; j++) [s appendFormat:@", sampler s%u [[sampler(%u)]]", j, j];
    [s appendString:@", constant uint& k [[buffer(0)]], device float4* o [[buffer(1)]]) {\n"];
    if (!pick) {
        for (unsigned j = 0; j < n; j++) [s appendFormat:@"  o[%u] = t.sample(s%u, float2(1.3f), level(0));\n", j, j];
    } else if (!ternary) {
        [s appendString:@"  sampler s = s0;\n  switch (k) {\n"];
        for (unsigned j = 1; j < n; j++) [s appendFormat:@"  case %u: s = s%u; break;\n", j, j];
        [s appendString:@"  default: break;\n  }\n  o[0] = t.sample(s, float2(1.3f), level(0));\n"];
    } else {
        [s appendString:@"  sampler s ="];
        for (unsigned j = 1; j < n; j++) [s appendFormat:@" k == %uu ? s%u :", j, j];
        [s appendString:@" s0;\n  o[0] = t.sample(s, float2(1.3f), level(0));\n"];
    }
    [s appendString:@"}\n"];
    return s;
}

static void case_phi(const char *c)
{
    const unsigned n = (unsigned)strtoul(c + 3, NULL, 10);   // phi8, phi9, phi16
    // 16 distinct (s address, t address, filter) states at the coordinate 1.3 on a 4x4 texture: per axis, nearest
    // gives texel 3 (clamp), 1 (repeat) or 2 (mirror), linear gives texel 3, a 0/1 or a 3/2 mix; (clamp, clamp,
    // linear) is left out because it equals (clamp, clamp, nearest) there
    const MTLSamplerAddressMode C = MTLSamplerAddressModeClampToEdge, R = MTLSamplerAddressModeRepeat,
                                M = MTLSamplerAddressModeMirrorRepeat;
    const MTLSamplerMinMagFilter N = MTLSamplerMinMagFilterNearest, L = MTLSamplerMinMagFilterLinear;
    const struct { MTLSamplerAddressMode s, t; MTLSamplerMinMagFilter f; } states[16] = {
        {C, C, N}, {C, R, L}, {R, R, N}, {R, C, L}, {M, M, N}, {M, R, L}, {C, R, N}, {R, M, L},
        {R, C, N}, {M, C, L}, {C, M, N}, {C, M, L}, {M, C, N}, {R, R, L}, {R, M, N}, {M, M, L}};
    char pick_name[16], direct_name[16];
    snprintf(pick_name, sizeof pick_name, "pick%u", n);
    snprintf(direct_name, sizeof direct_name, "direct%u", n);
    NSArray<NSString *> *markers = @[@"phi ptr addrspace(2)", @"select i1"];
    id<MTLComputePipelineState> direct = build_case(c, phi_source(n, NO, NO), @(direct_name), @[], NULL);
    if (!direct) return;
    id<MTLComputePipelineState> pick = nil;
    char why[1400];
    build_status st = build(c, phi_source(n, YES, NO), @(pick_name), markers, &pick, NULL, why, sizeof why);
    if (st == BUILD_SKIP) {
        info("%s: the switch form was refused by Apple's compiler (%s); trying a chain of ?:", c, why);
        st = build(c, phi_source(n, YES, YES), @(pick_name), markers, &pick, NULL, why, sizeof why);
    }
    if (st == BUILD_SKIP) { skip(c, "%s", why); return; }
    if (st == BUILD_FAIL) { fail(c, "%s", why); return; }
    float pixels[16 * 4];
    for (unsigned y = 0; y < 4; y++) for (unsigned x = 0; x < 4; x++) {
        float *p = &pixels[4 * (4 * y + x)];
        p[0] = 0.25f * (float)x; p[1] = 0.25f * (float)y; p[2] = (float)(4 * y + x) / 16.f; p[3] = 1.f;
    }
    id<MTLTexture> tex = texture_rgba32f(4, 4, pixels);
    id<MTLSamplerState> samplers[16];
    for (unsigned j = 0; j < n; j++) {
        samplers[j] = sampler_state(states[j].s, states[j].t, states[j].f);
        if (!samplers[j]) { setup_fail(c, "sampler %u", j); return; }
    }
    id<MTLBuffer> ks = buffer(16 * 256, 0), picks = buffer(16 * 256, 0xa5), directs = buffer(512, 0xa5);
    if (!tex || !ks || !picks || !directs) { setup_fail(c, "texture or buffers"); return; }
    for (unsigned k = 0; k < n; k++) *(uint32_t *)((char *)ks.contents + 256 * k) = k;
    id<MTLCommandBuffer> cb = [g_queue commandBuffer];
    id<MTLComputeCommandEncoder> enc = [cb computeCommandEncoder];
    [enc setTexture:tex atIndex:0];
    for (unsigned j = 0; j < n; j++) [enc setSamplerState:samplers[j] atIndex:j];
    [enc setComputePipelineState:direct];
    [enc setBuffer:ks offset:0 atIndex:0];
    [enc setBuffer:directs offset:0 atIndex:1];
    one_thread(enc);
    [enc setComputePipelineState:pick];
    for (unsigned k = 0; k < n; k++) {
        [enc setBuffer:ks offset:256 * k atIndex:0];
        [enc setBuffer:picks offset:256 * k atIndex:1];
        one_thread(enc);
    }
    [enc endEncoding];
    if (!finish(c, cb)) return;
    const float *d = directs.contents;
    if (!guards_intact(d, 4 * n, 128)) { fail(c, "direct%u wrote past its %u results", n, n); return; }
    for (unsigned i = 0; i < n; i++) for (unsigned j = i + 1; j < n; j++)
        if (!memcmp(&d[4 * i], &d[4 * j], 16)) {
            fail(c, "the direct samples through samplers %u and %u are equal (%g,%g,%g,%g): their states are not "
                 "applied, or the texture cannot tell them apart", i, j, d[4 * i], d[4 * i + 1], d[4 * i + 2],
                 d[4 * i + 3]);
            return;
        }
    unsigned bad = 0, first = n;
    for (unsigned k = 0; k < n; k++) {
        const char *slot = (const char *)picks.contents + 256 * k;
        BOOL ok = !memcmp(slot, &d[4 * k], 16) && guards_intact(slot, 4, 64);
        if (!ok && first == n) first = k;
        bad += !ok;
    }
    if (!bad) {
        pass(c, "every pick among %u samplers equals the direct sample through it (%u dispatches)", n, n + 1);
        return;
    }
    const float *g = (const float *)((const char *)picks.contents + 256 * first);
    fail(c, "%u of %u picks wrong; first k=%u got=(%g,%g,%g,%g) expected=(%g,%g,%g,%g)", bad, n, first, g[0], g[1],
         g[2], g[3], d[4 * first], d[4 * first + 1], d[4 * first + 2], d[4 * first + 3]);
}

/* ---- buffer-address lowering: a back-edge pointer phi, an escaping pointer --------------------------------------- */

static void case_walk(const char *c)
{
    NSString *body = @"kernel void walk(device const uint* a [[buffer(0)]], device const uint* b [[buffer(1)]],\n"
                      "                 device uint* o [[buffer(2)]], constant uint& n [[buffer(3)]]) {\n"
                      "  device const uint* p = a; uint s = 0;\n"
                      "  for (uint i = 0; i < n && i < 16u; ++i) { s = s * 31u + p[i]; p = (s & 1u) ? b : a; }\n"
                      "  o[0] = s;\n}\n";
    id<MTLComputePipelineState> pso = build_case(c, body, @"walk", @[@"phi ptr addrspace(1)"], NULL);
    if (!pso) return;
    id<MTLBuffer> a = buffer(256, 0), b = buffer(256, 0), out = buffer(256, 0xa5), n = buffer(256, 0);
    if (!a || !b || !out || !n) { setup_fail(c, "buffers"); return; }
    uint32_t *av = a.contents, *bv = b.contents;
    for (unsigned i = 0; i < 64; i++) { av[i] = 7 * i + 3; bv[i] = 11 * i + 5; }
    *(uint32_t *)n.contents = 16;
    uint32_t s = 0;
    const uint32_t *p = av;
    for (unsigned i = 0; i < 16; i++) { s = s * 31u + p[i]; p = (s & 1u) ? bv : av; }
    id<MTLCommandBuffer> cb = [g_queue commandBuffer];
    id<MTLComputeCommandEncoder> enc = [cb computeCommandEncoder];
    [enc setComputePipelineState:pso];
    [enc setBuffer:a offset:0 atIndex:0];
    [enc setBuffer:b offset:0 atIndex:1];
    [enc setBuffer:out offset:0 atIndex:2];
    [enc setBuffer:n offset:0 atIndex:3];
    one_thread(enc);
    [enc endEncoding];
    if (!finish(c, cb)) return;
    const uint32_t *o = out.contents;
    if (o[0] == s && guards_intact(o, 1, 64)) {
        pass(c, "16 steps through two buffers, sum %08x as on the CPU", s);
        return;
    }
    fail(c, "got=%08x expected=%08x%s", o[0], s, guards_intact(o, 1, 64) ? "" : "; guards overwritten");
}

static void case_esc(const char *c)
{
    NSString *body = @"struct P { device const uint* q; };\n"
                      "__attribute__((noinline)) static uint get_at(device const uint* q, uint i) { return q[i]; }\n"
                      "kernel void esc(constant P& t [[buffer(0)]], device uint* o [[buffer(1)]],\n"
                      "                uint i [[thread_position_in_grid]]) { o[i] = get_at(t.q, i) + 1u; }\n";
    id<MTLComputePipelineState> pso = build_case(c, body, @"esc", @[@"get_at"], NULL);
    if (!pso) return;
    id<MTLBuffer> data = buffer(256, 0), table = buffer(256, 0), out = buffer(256, 0xa5);
    if (!data || !table || !out) { setup_fail(c, "buffers"); return; }
    uint32_t *dv = data.contents;
    for (unsigned i = 0; i < 64; i++) dv[i] = 1000 + 37 * i;
    *(uint64_t *)table.contents = data.gpuAddress;
    id<MTLCommandBuffer> cb = [g_queue commandBuffer];
    id<MTLComputeCommandEncoder> enc = [cb computeCommandEncoder];
    [enc setComputePipelineState:pso];
    [enc setBuffer:table offset:0 atIndex:0];
    [enc setBuffer:out offset:0 atIndex:1];
    [enc useResource:data usage:MTLResourceUsageRead];
    [enc dispatchThreadgroups:MTLSizeMake(1, 1, 1) threadsPerThreadgroup:MTLSizeMake(16, 1, 1)];
    [enc endEncoding];
    if (!finish(c, cb)) return;
    log_line_info(c, @"address lowering REFUSED, shader kept as-is: address-table pointer escapes through");
    const uint32_t *o = out.contents;
    unsigned bad = 0, first = 16;
    for (unsigned i = 0; i < 16; i++) if (o[i] != dv[i] + 1) { bad++; if (first == 16) first = i; }
    BOOL guards = guards_intact(o, 16, 64);
    if (!bad && guards) { pass(c, "16 threads read through the pointer the helper was given"); return; }
    if (bad) fail(c, "%u of 16 wrong; first i=%u got=%u expected=%u%s", bad, first, o[first], dv[first] + 1,
                  guards ? "" : "; guards overwritten");
    else fail(c, "the values are right but o[16..] were written got=%08x expected=%08x", o[16], GUARD);
}

/* ---- RGB10A2 and RGB565 packing ---------------------------------------------------------------------------------- */

/* the reference of tools/translator_readback.cpp: round half to even of the exact product, NaN 0, clamped to [0, 1] */
static uint32_t round_even(double v)
{
    double f = floor(v), rest = v - f;
    return (uint32_t)f + (rest > 0.5 || (rest == 0.5 && ((uint32_t)f & 1)));
}

static uint32_t unorm(float v, uint32_t maximum)
{
    if (isnan(v)) v = 0;
    return round_even((double)fminf(1.f, fmaxf(0.f, v)) * maximum);
}

/* For information only: the product computed in float (rounded once), then rounded half to even. The translator's
   lowering of air.pack.unorm.rgb10a2 aims at the exact product and needs its residual FMA kept whole (NoContraction)
   for that, so a word that matches this and not unorm() says that the residual was lost. */
static uint32_t unorm_float_product(float v, uint32_t maximum)
{
    if (isnan(v)) v = 0;
    volatile float product = fminf(1.f, fmaxf(0.f, v)) * (float)maximum;
    return (uint32_t)rintf(product);
}

static float half_value(uint16_t bits)
{
    unsigned exponent = (bits >> 10) & 31, fraction = bits & 1023;
    float value = exponent == 31 ? (fraction ? NAN : INFINITY)
                : ldexpf((float)(fraction + (exponent ? 1024 : 0)), exponent ? (int)exponent - 25 : -24);
    return bits & 0x8000 ? -value : value;
}

static float nudge(float v, int d) { return d < 0 ? nextafterf(v, 0.f) : d > 0 ? nextafterf(v, 2.f) : v; }

static void case_pack10(const char *c)
{
    NSString *body = @"kernel void p10(device const float4* in [[buffer(0)]], device uint* o [[buffer(1)]],\n"
                      "                uint i [[thread_position_in_grid]]) {\n"
                      "  o[i] = pack_float_to_unorm10a2(in[i]);\n}\n";
    id<MTLComputePipelineState> pso = build_case(c, body, @"p10", @[@"air.pack.unorm.rgb10a2"], NULL);
    if (!pso) return;
    const int intrinsic = g_shape;   // 0: the AIR has no air.pack.unorm.rgb10a2 call, the packing is plain arithmetic
    // every 10-bit half-way value (2j+1)/2046 and its two float neighbours, in all three channels in different
    // orders, the 2-bit ones (2j+1)/6 in alpha, then the controls of translator_readback.cpp and infinities
    enum { HALFWAY = 1023 * 3, CONTROLS = 6, COUNT = (HALFWAY + CONTROLS + 63) / 64 * 64 };
    id<MTLBuffer> in = buffer(COUNT * 16, 0), out = buffer(COUNT * 4 + 256, 0xa5);
    if (!in || !out) { setup_fail(c, "buffers"); return; }
    float *v = in.contents;
    unsigned e = 0;
    for (unsigned j = 0; j < 1023; j++) for (int d = -1; d <= 1; d++, e++) {
        v[4 * e] = nudge((float)(2 * j + 1) / 2046.f, d);
        v[4 * e + 1] = nudge((float)(2 * (1022 - j) + 1) / 2046.f, d);
        v[4 * e + 2] = nudge((float)(2 * ((j * 7) % 1023) + 1) / 2046.f, -d);
        v[4 * e + 3] = nudge((float)(2 * (j % 3) + 1) / 6.f, d);
    }
    const float controls[CONTROLS][4] = {{0.f, 0.5f, 1.f, 0.5f}, {-1.f, 2.f, 0.25f, 1.f}, {NAN, 0.75f, 0.5f, 0.f},
        {1.f / 2046.f, 3.f / 2046.f, 5.f / 2046.f, 1.f / 6.f}, {INFINITY, -INFINITY, 1e-30f, -0.f},
        {1.f / 1023.f, 0.5f / 1023.f, 1.5f / 1023.f, 0.5f / 3.f}};
    for (unsigned k = 0; k < CONTROLS; k++, e++) memcpy(&v[4 * e], controls[k], 16);
    id<MTLCommandBuffer> cb = [g_queue commandBuffer];
    id<MTLComputeCommandEncoder> enc = [cb computeCommandEncoder];
    [enc setComputePipelineState:pso];
    [enc setBuffer:in offset:0 atIndex:0];
    [enc setBuffer:out offset:0 atIndex:1];
    [enc dispatchThreadgroups:MTLSizeMake(COUNT / 64, 1, 1) threadsPerThreadgroup:MTLSizeMake(64, 1, 1)];
    [enc endEncoding];
    if (!finish(c, cb)) return;
    const uint32_t *o = out.contents;
    unsigned bad = 0, first = COUNT, float_product = 0;
    uint32_t want_first = 0;
    for (unsigned i = 0; i < COUNT; i++) {
        const float *x = &v[4 * i];
        uint32_t want = unorm(x[0], 1023) | unorm(x[1], 1023) << 10 | unorm(x[2], 1023) << 20 | unorm(x[3], 3) << 30;
        if (o[i] == want) continue;
        if (!bad) { first = i; want_first = want; }
        bad++;
        float_product += o[i] == (unorm_float_product(x[0], 1023) | unorm_float_product(x[1], 1023) << 10
                                  | unorm_float_product(x[2], 1023) << 20 | unorm_float_product(x[3], 3) << 30);
    }
    BOOL guards = guards_intact(o, COUNT, COUNT + 64);
    if (bad)
        info("pack10 rounding: %u of the %u wrong words equal the float product rounded half to even%s", float_product,
             bad, float_product == bad && intrinsic == 1 ? " (a rounding the lowering's exact product excludes): the "
             "residual FMA was split, by a translator without its NoContraction (release 2.9's) or by a backend that "
             "ignores it" : "");
    if (!bad && guards) {
        pass(c, "%u inputs packed as the exact product rounds half to even", (unsigned)COUNT);
        return;
    }
    if (bad && guards && intrinsic == 0) {
        // Apple's compiler wrote the packing as arithmetic on the float product, whose rounding at these half-way
        // inputs is not the exact product's: the reference describes only the intrinsic the translator lowers
        skip(c, "the AIR has no air.pack.unorm.rgb10a2 call, so the repair's lowering is not reached; %u of %u differ "
             "from the exact product's rounding (first input (%.9g,%.9g,%.9g,%.9g) got=%08x expected=%08x)", bad,
             (unsigned)COUNT, v[4 * first], v[4 * first + 1], v[4 * first + 2], v[4 * first + 3], o[first], want_first);
        return;
    }
    if (bad) fail(c, "%u of %u wrong; first input (%.9g,%.9g,%.9g,%.9g) got=%08x expected=%08x%s", bad, (unsigned)COUNT,
                  v[4 * first], v[4 * first + 1], v[4 * first + 2], v[4 * first + 3], o[first], want_first,
                  guards ? "" : "; guards overwritten");
    else fail(c, "the values are right but the words after them were written");
}

static void case_pack565(const char *c)
{
    // The inputs are half values, as in the AIR form air.pack.unorm.rgb565.v3f16: a half times 31 or 63 is exact in
    // float, so the translator's rounding of the float product (dispatch_texture.rs, lower_pack_rgb565) is the exact
    // rounding. The forms on half3 come first (the MSL name of the half3 overload is not known here, so both are
    // tried); if Apple's compiler refuses both, the same values go as floats to the float3 form.
    static const struct { const char *name, *call, *type; BOOL as_float; } forms[] = {
        {"p565h", "pack_half_to_unorm565", "half4", NO}, {"p565", "pack_float_to_unorm565", "half4", NO},
        {"p565f", "pack_float_to_unorm565", "float4", YES}};
    id<MTLComputePipelineState> pso = nil;
    char why[1400];
    BOOL as_float = NO;
    build_status st = BUILD_SKIP;
    for (unsigned f = 0; f < sizeof forms / sizeof forms[0] && st == BUILD_SKIP; f++) {
        if (f) info("%s: the %s form was not compiled (%s); trying %s on %s", c, forms[f - 1].call, why,
                    forms[f].call, forms[f].type);
        NSString *body = [NSString stringWithFormat:@"kernel void %s(device const %s* in [[buffer(0)]],\n"
                          "  device ushort* o [[buffer(1)]], uint i [[thread_position_in_grid]]) {\n"
                          "  o[i] = %s(in[i].xyz);\n}\n", forms[f].name, forms[f].type, forms[f].call];
        as_float = forms[f].as_float;
        NSArray<NSString *> *markers = as_float ? @[@"air.pack.unorm.rgb565"] : @[@"air.pack.unorm.rgb565.v3f16"];
        st = build(c, body, @(forms[f].name), markers, &pso, NULL, why, sizeof why);
    }
    if (st == BUILD_SKIP) { skip(c, "%s", why); return; }
    if (st == BUILD_FAIL) { fail(c, "%s", why); return; }
    // every half value from 0 to 1 (bits 0x0000-0x3c00) in red, in another order in green and blue, then the half
    // controls of translator_readback.cpp
    enum { SWEEP = 0x3c01, CONTROLS = 5, COUNT = (SWEEP + CONTROLS + 63) / 64 * 64 };
    id<MTLBuffer> in = buffer(COUNT * (as_float ? 16 : 8), 0), out = buffer(COUNT * 2 + 256, 0xa5);
    uint16_t *h = calloc(COUNT * 4, sizeof *h);
    if (!in || !out || !h) { free(h); setup_fail(c, "buffers"); return; }
    unsigned e = 0;
    for (unsigned b = 0; b < SWEEP; b++, e++) {
        h[4 * e] = (uint16_t)b;
        h[4 * e + 1] = (uint16_t)((b * 7u) % SWEEP);
        h[4 * e + 2] = (uint16_t)(0x3c00 - b);
        h[4 * e + 3] = 0;
    }
    const uint16_t controls[CONTROLS][3] = {{0x3c00, 0x3800, 0}, {0x7e00, 0x3c00, 0x3800}, {0xbc00, 0x4000, 0x3400},
                                            {0x3800, 0x3800, 0x3800}, {0x7c00, 0xfc00, 1}};
    for (unsigned k = 0; k < CONTROLS; k++, e++) { memcpy(&h[4 * e], controls[k], 6); h[4 * e + 3] = 0; }
    if (as_float) for (unsigned i = 0; i < COUNT * 4; i++) ((float *)in.contents)[i] = half_value(h[i]);
    else memcpy(in.contents, h, COUNT * 8);
    id<MTLCommandBuffer> cb = [g_queue commandBuffer];
    id<MTLComputeCommandEncoder> enc = [cb computeCommandEncoder];
    [enc setComputePipelineState:pso];
    [enc setBuffer:in offset:0 atIndex:0];
    [enc setBuffer:out offset:0 atIndex:1];
    [enc dispatchThreadgroups:MTLSizeMake(COUNT / 64, 1, 1) threadsPerThreadgroup:MTLSizeMake(64, 1, 1)];
    [enc endEncoding];
    if (!finish(c, cb)) { free(h); return; }
    const uint16_t *o = out.contents;
    unsigned bad = 0, first = COUNT;
    uint16_t want_first = 0;
    for (unsigned i = 0; i < COUNT; i++) {
        const uint16_t *x = &h[4 * i];
        uint16_t want = (uint16_t)(unorm(half_value(x[0]), 31) | unorm(half_value(x[1]), 63) << 5
                                   | unorm(half_value(x[2]), 31) << 11);
        if (o[i] != want) { if (!bad) { first = i; want_first = want; } bad++; }
    }
    BOOL guards = guards_intact(o + COUNT, 0, 32);
    if (!bad && guards)
        pass(c, "%u %s inputs packed as the exact product rounds half to even", (unsigned)COUNT,
             as_float ? "float3" : "half3");
    else if (bad)
        fail(c, "%u of %u wrong; first input bits (%04x,%04x,%04x) got=%04x expected=%04x%s", bad, (unsigned)COUNT,
             h[4 * first], h[4 * first + 1], h[4 * first + 2], o[first], want_first,
             guards ? "" : "; guards overwritten");
    else fail(c, "the values are right but the words after them were written");
    free(h);
}

/* ---- inlining: a splat argument; textures in a local object's fields --------------------------------------------- */

static void case_splat(const char *c)
{
    NSString *body = @"__attribute__((noinline)) static float4 clearcoat(float4 c, float4 f0) { return c * f0; }\n"
                      "kernel void splat_k(device const float4* in [[buffer(0)]], device float4* o [[buffer(1)]],\n"
                      "                    uint i [[thread_position_in_grid]]) {\n"
                      "  o[i] = clearcoat(in[i], float4(0.04f));\n}\n";
    id<MTLComputePipelineState> pso = build_case(c, body, @"splat_k", @[@"splat (float", @"clearcoat"], NULL);
    if (!pso) return;
    id<MTLBuffer> in = buffer(16 * 16, 0), out = buffer(16 * 16 + 256, 0xa5);
    if (!in || !out) { setup_fail(c, "buffers"); return; }
    float *v = in.contents;
    for (unsigned i = 0; i < 16; i++) {
        // no lane is zero: the library is built with fast math, under which -0 times 0.04f may come back as +0
        v[4 * i] = 1.f + (float)i; v[4 * i + 1] = -2.5f * (float)(i + 1); v[4 * i + 2] = 0.5f + (float)i / 16.f;
        v[4 * i + 3] = 1000.f - 3.25f * (float)i;
    }
    id<MTLCommandBuffer> cb = [g_queue commandBuffer];
    id<MTLComputeCommandEncoder> enc = [cb computeCommandEncoder];
    [enc setComputePipelineState:pso];
    [enc setBuffer:in offset:0 atIndex:0];
    [enc setBuffer:out offset:0 atIndex:1];
    [enc dispatchThreadgroups:MTLSizeMake(1, 1, 1) threadsPerThreadgroup:MTLSizeMake(16, 1, 1)];
    [enc endEncoding];
    if (!finish(c, cb)) return;
    const float *o = out.contents;
    unsigned bad = 0, first = 64, zeros = 0;
    for (unsigned i = 0; i < 64; i++) {
        const float want = v[i] * 0.04f;
        if (memcmp(&o[i], &want, 4)) { bad++; if (first == 64) first = i; }
        zeros += o[i] == 0.f;
    }
    BOOL guards = guards_intact(o, 64, 128);
    if (!bad && guards) { pass(c, "64 lanes equal the CPU product by 0.04f bit for bit"); return; }
    if (bad) fail(c, "%u of 64 lanes wrong; first lane %u got=%.9g expected=%.9g%s%s", bad, first, o[first],
                  v[first] * 0.04f, zeros >= 60 ? "; the lanes are zero" : "", guards ? "" : "; guards overwritten");
    else fail(c, "the lanes are right but the words after them were written");
}

static void case_localfield(const char *c)
{
    NSString *body = @"struct Env { texture2d<float> t; sampler s; };\n"
                      "__attribute__((noinline)) static float4 read_env(thread const Env* e, uint p) {\n"
                      "  return e[p].t.sample(e[p].s, float2(0.5f), level(0)); }\n"
                      "kernel void env_k(texture2d<float> t0 [[texture(0)]], texture2d<float> t1 [[texture(1)]],\n"
                      "                  sampler s0 [[sampler(0)]], sampler s1 [[sampler(1)]],\n"
                      "                  constant uint& page [[buffer(0)]], device float4* o [[buffer(1)]]) {\n"
                      "  Env e[2] = {{t0, s0}, {t1, s1}};\n"
                      "  o[0] = read_env(e, page);\n}\n";
    id<MTLComputePipelineState> pso = build_case(c, body, @"env_k", @[@"alloca", @"read_env"], NULL);
    if (!pso) return;
    const float texels[2][4] = {{0.125f, 0.25f, 0.5f, 1.f}, {0.75f, 0.5f, 0.25f, 0.f}};
    id<MTLTexture> t0 = texture_rgba32f(1, 1, texels[0]), t1 = texture_rgba32f(1, 1, texels[1]);
    id<MTLSamplerState> s0 = sampler_state(MTLSamplerAddressModeClampToEdge, MTLSamplerAddressModeClampToEdge,
                                           MTLSamplerMinMagFilterNearest);
    id<MTLSamplerState> s1 = sampler_state(MTLSamplerAddressModeRepeat, MTLSamplerAddressModeRepeat,
                                           MTLSamplerMinMagFilterNearest);
    id<MTLBuffer> pages = buffer(512, 0), out = buffer(512, 0xa5);
    if (!t0 || !t1 || !s0 || !s1 || !pages || !out) { setup_fail(c, "textures, samplers or buffers"); return; }
    *(uint32_t *)((char *)pages.contents + 256) = 1;
    id<MTLCommandBuffer> cb = [g_queue commandBuffer];
    id<MTLComputeCommandEncoder> enc = [cb computeCommandEncoder];
    [enc setComputePipelineState:pso];
    [enc setTexture:t0 atIndex:0];
    [enc setTexture:t1 atIndex:1];
    [enc setSamplerState:s0 atIndex:0];
    [enc setSamplerState:s1 atIndex:1];
    for (unsigned p = 0; p < 2; p++) {
        [enc setBuffer:pages offset:256 * p atIndex:0];
        [enc setBuffer:out offset:256 * p atIndex:1];
        one_thread(enc);
    }
    [enc endEncoding];
    if (!finish(c, cb)) return;
    for (unsigned p = 0; p < 2; p++) {
        const float *o = (const float *)((const char *)out.contents + 256 * p);
        if (memcmp(o, texels[p], 16) || !guards_intact(o, 4, 64)) {
            fail(c, "page %u got=(%g,%g,%g,%g) expected=(%g,%g,%g,%g)%s", p, o[0], o[1], o[2], o[3], texels[p][0],
                 texels[p][1], texels[p][2], texels[p][3],
                 o[0] == 0.f && o[1] == 0.f && o[2] == 0.f && o[3] == 0.f ? "; zero, the old signature" : "");
            return;
        }
    }
    pass(c, "pages 0 and 1 sample their own texture through the local object");
}

/* ---- an int2 through a float4 pointer ---------------------------------------------------------------------------- */

static void case_lanes(const char *c)
{
    NSString *body = @"kernel void lanes(constant float4* s [[buffer(0)]], constant uint& i [[buffer(1)]],\n"
                      "                  device int2* o [[buffer(2)]]) {\n"
                      "  o[0] = *reinterpret_cast<constant int2*>(s + i);\n}\n";
    id<MTLComputePipelineState> pso = build_case(c, body, @"lanes",
                                                 @[@"getelementptr inbounds <4 x float>", @"load <2 x i32>"], NULL);
    if (!pso) return;
    id<MTLBuffer> slots = buffer(256, 0), index = buffer(256, 0), out = buffer(256, 0xa5);
    if (!slots || !index || !out) { setup_fail(c, "buffers"); return; }
    uint32_t *w = slots.contents;
    for (unsigned i = 0; i < 64; i++) w[i] = 0x11110000u + i;   // any other slot read shows as a wrong word
    const uint32_t slot1[4] = {(uint32_t)-7, 123456789u, 0x7fc00001u, 5u};
    memcpy(&w[4], slot1, 16);
    *(uint32_t *)index.contents = 1;
    id<MTLCommandBuffer> cb = [g_queue commandBuffer];
    id<MTLComputeCommandEncoder> enc = [cb computeCommandEncoder];
    [enc setComputePipelineState:pso];
    [enc setBuffer:slots offset:0 atIndex:0];
    [enc setBuffer:index offset:0 atIndex:1];
    [enc setBuffer:out offset:0 atIndex:2];
    one_thread(enc);
    [enc endEncoding];
    if (!finish(c, cb)) return;
    const uint32_t *o = out.contents;
    if (o[0] == slot1[0] && o[1] == slot1[1] && guards_intact(o, 2, 64)) {
        pass(c, "slot 1's first two words, -7 and 123456789, and nothing after them");
        return;
    }
    fail(c, "got=(%d,%d) expected=(-7,123456789)%s", (int)o[0], (int)o[1],
         guards_intact(o, 2, 64) ? "" : "; guards overwritten");
}

/* ---- main -------------------------------------------------------------------------------------------------------- */

static const struct { const char *name; void (*run)(const char *); } cases[] = {
    {"abarray-a", case_abarray}, {"abarray-c", case_abarray}, {"abarray-s", case_abarray}, {"tbuf", case_tbuf},
    {"phi8", case_phi}, {"phi9", case_phi}, {"phi16", case_phi}, {"walk", case_walk}, {"esc", case_esc},
    {"pack10", case_pack10}, {"pack565", case_pack565}, {"splat", case_splat}, {"localfield", case_localfield},
    {"lanes", case_lanes},
};
enum { NCASES = sizeof cases / sizeof cases[0] };

int main(int argc, char **argv)
{
    @autoreleasepool {
        const int sigs[] = {SIGSEGV, SIGBUS, SIGILL, SIGFPE, SIGABRT};
        for (unsigned i = 0; i < sizeof sigs / sizeof sigs[0]; i++) signal(sigs[i], crashed);
        alarm(ALARM_SECONDS);
        BOOL chosen[NCASES] = {NO};
        for (int a = 1; a < argc; a++) {
            unsigned i = 0;
            while (i < NCASES && strcmp(argv[a], cases[i].name)) i++;
            if (i == NCASES) {
                fprintf(stderr, "usage: %s [case ...]; the cases:", argv[0]);
                for (unsigned j = 0; j < NCASES; j++) fprintf(stderr, " %s", cases[j].name);
                fprintf(stderr, "\n");
                return 2;
            }
            chosen[i] = YES;
        }
        if (argc < 2) for (unsigned i = 0; i < NCASES; i++) chosen[i] = YES;
        g_device = MTLCreateSystemDefaultDevice();
        NSString *cls = g_device ? NSStringFromClass([g_device class]) : @"none";
        if (![cls isEqualToString:@"NVMTLDevice"]) {
            printf("SKIP: no device of this driver (%s)\n", cls.UTF8String);
            printf("RESULT skip pass=0 fail=0 skip=0\n");
            return 3;
        }
        g_queue = [g_device newCommandQueue];
        if (!g_queue) { printf("FAIL setup: no command queue\nRESULT fail pass=0 fail=1 skip=0\n"); return 2; }
        info("device %s", g_device.name.UTF8String);
        for (unsigned i = 0; i < NCASES; i++) {
            if (!chosen[i]) continue;
            @autoreleasepool { cases[i].run(cases[i].name); }
        }
        translator_info();
        result_line();
        if (g_fail) return g_fail > g_setup ? 1 : 2;
        return g_pass ? 0 : 3;
    }
}
