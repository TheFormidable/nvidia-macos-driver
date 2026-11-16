/* v301_pretranslate.m: background translation of a new library's shaders, on by default in a game from release 3.0.1
 * (plugin/nvmtl_switch_defaults.h, nvmtl_pretranslate_library in plugin/NVMTLLibraryLoad.m), on the installed Metal
 * driver. The driver calls a process a game when its Info.plist has the games category or LSSupportsGameMode. The
 * argument says how the row runs:
 *   game   built with tools/card/v301_game_info.plist as its Info.plist (the games category), NVMTL_PRETRANSLATE unset
 *   off    the same program, with NVMTL_PRETRANSLATE=0 (the way back: shaders are translated when first asked for)
 *   app    built without an Info.plist, NVMTL_PRETRANSLATE unset: not a game, so nothing is translated ahead
 * Two libraries of 32 compute kernels each are made from source; every kernel's text carries a number of this process,
 * so no cache entry of an earlier run answers for it. Cases:
 *   switch   the driver's log says "pretranslate: ON (NVMTL_PRETRANSLATE=unset, a game)", "pretranslate: OFF
 *            (NVMTL_PRETRANSLATE=0)" or "pretranslate: OFF (NVMTL_PRETRANSLATE=unset, not a game)"
 *   queued   game: the log says "pretranslate: library of 32 entry points queued for background translation" for each
 *            library; off and app: the log says nothing of a queued library
 *   ahead    game: within 25 s of the first library the log says "pretranslate: 32 of 32 queued shaders processed"
 *            with none refused; its pipelines are then made from what was translated ahead
 *   during   the second library's 32 pipelines are made and run at once, while (game) the background translation of the
 *            same kernels runs: one translation per kernel, whoever asks first; game: then "64 of 64 queued shaders
 *            processed" with none refused
 *   kernels  every kernel of both libraries is run and writes what its text says, in every row
 * Each row sets NVMTL_GAME_GPU_PRIORITY=0 and NVMTL_GAME_LOWER_BACKGROUND=0, so that a game process of this program
 * never changes its channel group's priority (the driver does that for a game when the Game Mode service is installed).
 *   /usr/bin/xcrun clang -arch x86_64 -mmacosx-version-min=15.0 -fobjc-arc -O2 tools/card/v301_pretranslate.m \
 *     [-Wl,-sectcreate,__TEXT,__info_plist,tools/card/v301_game_info.plist] -framework Foundation -framework Metal \
 *     -o <run>/bin/<id>/nvmtltest
 * It skips when NVMTL_PROCESS_ENV=1 or NVMTL_LOG_PATH is missing, on a device of another driver, and when
 * NVMTL_PRETRANSLATE does not hold what the row asks for after the device is made (the settings file sets it). A game
 * row whose Info.plist this macOS did not read is a setup failure. It writes no file but the shader cache entries the
 * driver makes in the folder the runner gives it, and opens no window. Exit 0 pass, 1 a case failed, 2 setup only, 3
 * skip, 70 crash. Not yet run on a Mac. */
#import "plugin_common.h"

enum { KERNELS = 32, THREADS = 256 };

static id<MTLDevice> device;
static id<MTLCommandQueue> queue;
static uint32_t salt;

static uint32_t mul_of(unsigned lib, unsigned k)
{
    return (salt ^ (lib * 7919u + k * 104729u)) | 1u;
}

static uint32_t add_of(unsigned lib, unsigned k)
{
    return salt * 31u + lib * 1000u + k;
}

static NSString *source_of(unsigned lib)
{
    NSMutableString *s = [NSMutableString stringWithString:@"#include <metal_stdlib>\nusing namespace metal;\n"];
    for (unsigned k = 0; k < KERNELS; k++)
        [s appendFormat:@"kernel void k%u_%u(device uint *o [[buffer(0)]], uint i [[thread_position_in_grid]]) "
                        @"{ o[i] = i * %uu + %uu; }\n",
                        lib, k, mul_of(lib, k), add_of(lib, k)];
    return s;
}

// the number in "<prefix><n> of <n> queued shaders processed - ..., <r> refused" of the last such line, or 0
static unsigned processed(NSArray<NSString *> *lines, unsigned want, unsigned *refused, NSString **line)
{
    NSString *prefix = [NSString stringWithFormat:@"pretranslate: %u of %u queued shaders processed - ", want, want];
    for (NSString *l in [lines reverseObjectEnumerator])
        if ([l hasPrefix:prefix]) {
            unsigned long r = 0;
            NSRange at = [l rangeOfString:@", " options:NSBackwardsSearch];
            if (at.location != NSNotFound) r = strtoul([l substringFromIndex:at.location + 2].UTF8String, NULL, 10);
            *refused = (unsigned)r;
            *line = l;
            return want;
        }
    return 0;
}

// waits up to `seconds` for "<want> of <want> queued shaders processed"; YES when it came
static BOOL wait_processed(unsigned want, double seconds, unsigned *refused, NSString **line)
{
    const double step = 0.1;
    for (double waited = 0; waited <= seconds; waited += step) {
        if (processed(nm_log_lines(), want, refused, line)) return YES;
        usleep((useconds_t)(step * 1e6));
    }
    return NO;
}

static NSArray<id<MTLComputePipelineState>> *pipelines(id<MTLLibrary> library, unsigned lib, const char *name)
{
    NSMutableArray<id<MTLComputePipelineState>> *all = [NSMutableArray array];
    for (unsigned k = 0; k < KERNELS; k++) {
        id<MTLComputePipelineState> p = nm_kernel(device, library, [NSString stringWithFormat:@"k%u_%u", lib, k], name);
        if (!p) return nil;
        [all addObject:p];
    }
    return all;
}

// every kernel run once into its own part of one buffer; the number of kernels that wrote what their text says
static unsigned run_all(NSArray<id<MTLComputePipelineState>> *ps, unsigned lib, const char *name)
{
    id<MTLBuffer> out = [device newBufferWithLength:(NSUInteger)KERNELS * THREADS * 4
                                            options:MTLResourceStorageModeShared];
    if (!out) {
        nm_setup(name, "a shared buffer");
        return 0;
    }
    memset(out.contents, 0, (size_t)KERNELS * THREADS * 4);
    id<MTLCommandBuffer> cb = [queue commandBuffer];
    id<MTLComputeCommandEncoder> enc = [cb computeCommandEncoder];
    for (unsigned k = 0; k < KERNELS; k++) {
        [enc setComputePipelineState:ps[k]];
        [enc setBuffer:out offset:(NSUInteger)k * THREADS * 4 atIndex:0];
        [enc dispatchThreads:MTLSizeMake(THREADS, 1, 1) threadsPerThreadgroup:MTLSizeMake(64, 1, 1)];
    }
    [enc endEncoding];
    if (!nm_run(cb, name)) return 0;
    const uint32_t *v = out.contents;
    unsigned right = 0, told = 0;
    for (unsigned k = 0; k < KERNELS; k++) {
        unsigned i = 0;
        for (; i < THREADS; i++)
            if (v[k * THREADS + i] != i * mul_of(lib, k) + add_of(lib, k)) break;
        if (i == THREADS)
            right++;
        else if (told++ < 3)
            nm_fail(name, "kernel k%u_%u wrote %08x at %u, %08x is what its text says", lib, k, v[k * THREADS + i], i,
                    i * mul_of(lib, k) + add_of(lib, k));
    }
    return right;
}

static unsigned queued_lines(NSArray<NSString *> *lines, NSString **any)
{
    unsigned n = 0;
    for (NSString *l in lines)
        if ([l hasPrefix:@"pretranslate: library of "]) {
            n++;
            *any = l;
        }
    return n;
}

int main(int argc, char **argv)
{
    @autoreleasepool {
        nm_start(60);
        const char *mode = argc > 1 ? argv[1] : "";
        const BOOL game = !strcmp(mode, "game"), off = !strcmp(mode, "off"), app = !strcmp(mode, "app");
        if (!game && !off && !app) {
            printf("FAIL usage: v301_pretranslate game|off|app\n");
            return 2;
        }
        const char *process_env = getenv("NVMTL_PROCESS_ENV"), *log_path = getenv("NVMTL_LOG_PATH");
        if (!process_env || strcmp(process_env, "1") || !log_path || !*log_path)
            nm_skip("needs NVMTL_PROCESS_ENV=1 and NVMTL_LOG_PATH: the driver reads the switch and writes its log "
                    "only then");
        device = nm_device();
        const char *e = getenv("NVMTL_PRETRANSLATE");
        if (!off && e)
            nm_skip("NVMTL_PRETRANSLATE=%s is set (by the settings file or the environment): the default this row "
                    "checks does not run",
                    e);
        if (off && !(e && !strcmp(e, "0")))
            nm_skip("NVMTL_PRETRANSLATE is %s, not 0: the way back this row checks does not run", e ? e : "unset");
        id category = [[NSBundle mainBundle] objectForInfoDictionaryKey:@"LSApplicationCategoryType"];
        id gamemode = [[NSBundle mainBundle] objectForInfoDictionaryKey:@"LSSupportsGameMode"];
        const BOOL is_game = [category isEqual:@"public.app-category.games"] ||
                             ([gamemode respondsToSelector:@selector(boolValue)] && [gamemode boolValue]);
        nm_info("Info.plist: LSApplicationCategoryType %s, LSSupportsGameMode %s: %s",
                category ? [category description].UTF8String : "absent",
                gamemode ? [gamemode description].UTF8String : "absent", is_game ? "a game" : "not a game");
        if ((game || off) != is_game) {
            nm_setup("start", "this program is %s, and the row runs it as %s", is_game ? "a game" : "not a game",
                     app ? "an application" : "a game");
            return nm_finish();
        }
        salt = (uint32_t)getpid() * 2654435761u;
        queue = [device newCommandQueue];
        if (!queue) {
            nm_setup("start", "a command queue");
            return nm_finish();
        }

        id<MTLLibrary> first = nm_library(device, source_of(0), "library");
        if (!first) return nm_finish();
        NSArray<NSString *> *lines = nm_log_lines();
        NSString *want = game  ? @"pretranslate: ON (NVMTL_PRETRANSLATE=unset, a game)"
                         : off ? @"pretranslate: OFF (NVMTL_PRETRANSLATE=0)"
                               : @"pretranslate: OFF (NVMTL_PRETRANSLATE=unset, not a game)";
        NSString *said = nm_log_find(lines, "pretranslate: O", NULL);
        if ([said isEqualToString:want])
            nm_pass("switch", "the driver says %s", said.UTF8String);
        else
            nm_fail("switch", "the driver says \"%s\", not \"%s\"", said ? said.UTF8String : "nothing of the switch",
                    want.UTF8String);

        NSArray<id<MTLComputePipelineState>> *ps0 = nil;
        if (game) {
            unsigned refused = 0;
            NSString *line = nil;
            if (!wait_processed(KERNELS, 25, &refused, &line))
                nm_fail("ahead", "the driver did not say \"%u of %u queued shaders processed\" within 25 s", KERNELS,
                        KERNELS);
            else if (refused)
                nm_fail("ahead", "the background translation refused %u of these plain kernels: %s", refused,
                        line.UTF8String);
            else
                nm_pass("ahead", "the driver says %s", line.UTF8String);
        }
        ps0 = pipelines(first, 0, "kernels");

        // the second library: its pipelines are asked for at once, while its background translation runs (game)
        id<MTLLibrary> second = nm_library(device, source_of(1), "library");
        NSArray<id<MTLComputePipelineState>> *ps1 = second ? pipelines(second, 1, "during") : nil;
        unsigned right1 = ps1 ? run_all(ps1, 1, "during") : 0;
        if (ps1 && right1 == KERNELS) {
            if (game) {
                unsigned refused = 0;
                NSString *line = nil;
                if (!wait_processed(2 * KERNELS, 20, &refused, &line))
                    nm_fail("during", "the driver did not say \"%u of %u queued shaders processed\" within 20 s",
                            2 * KERNELS, 2 * KERNELS);
                else if (refused)
                    nm_fail("during", "the background translation refused %u kernels: %s", refused, line.UTF8String);
                else
                    nm_pass("during",
                            "32 pipelines made and run while their background translation ran; the driver "
                            "says %s",
                            line.UTF8String);
            } else {
                nm_pass("during", "32 pipelines of a second library made and run");
            }
        }

        NSString *any = nil;
        const unsigned nq = queued_lines(nm_log_lines(), &any);
        NSString *qline = [NSString stringWithFormat:@"pretranslate: library of %u entry points queued for background "
                                                     @"translation",
                                                     KERNELS];
        if (game) {
            unsigned exact = 0;
            for (NSString *l in nm_log_lines())
                if ([l isEqualToString:qline]) exact++;
            if (nq == 2 && exact == 2)
                nm_pass("queued", "the driver says twice: %s", qline.UTF8String);
            else
                nm_fail("queued", "%u lines of a queued library (%u of them \"%s\"), two were expected; the last: %s",
                        nq, exact, qline.UTF8String, any ? any.UTF8String : "none");
        } else if (!nq) {
            nm_pass("queued", "the driver queued no library for background translation");
        } else {
            nm_fail("queued", "the driver queued %u libraries with pre-translation off; the last: %s", nq,
                    any.UTF8String);
        }

        const unsigned right0 = ps0 ? run_all(ps0, 0, "kernels") : 0;
        if (ps0 && ps1 && right0 == KERNELS && right1 == KERNELS)
            nm_pass("kernels", "all %u kernels of both libraries wrote what their text says", 2 * KERNELS);
        else if (ps0 && ps1)
            nm_fail("kernels", "%u and %u of %u kernels wrote what their text says", right0, right1, KERNELS);
        return nm_finish();
    }
}
