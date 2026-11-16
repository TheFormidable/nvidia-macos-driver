/* plugin_format_threads.m: the driver's first format-storage query asked by 32 threads at once, in 10 fresh processes,
 * on the installed Metal driver. Before the fix, threads that asked at the same time could be told "not storage", and
 * their textures were made without the storage view (plugin/nvmtl_vk.c nvmtl_vk_format_is_storage now uses a function
 * that nvmtl_vk_init resolves under its pthread_once). Each child process opens the device, then 32 threads meet at a
 * barrier and each makes a private 16x16 RGBA16Float texture with usage ShaderWrite; every texture's _stex field (read
 * by name) must be set, and four of them get a kernel write that is read back exactly. The parent runs the 10 children
 * one after the other (posix_spawn of this same program with --child N) and passes when every child passed: 320 of 320
 * textures with their storage view and 40 of 40 exact readbacks.
 * This is a smoke check with almost no power to catch the old race: its window was a few nanoseconds, and creating the
 * device may answer the query before the threads start. It shows that the query answers "storage" from many threads.
 *   /usr/bin/xcrun clang -arch x86_64 -mmacosx-version-min=15.0 -fobjc-arc -O2 tools/card/plugin_format_threads.m \
 *     -framework Foundation -framework Metal -o <run>/bin/<id>/nvmtltest
 * A child ended by its own alarm ends the parent by SIGALRM too, so that the runner treats it as a timeout (a hung GPU)
 * and stops. It writes no file and opens no window. Exit 0 pass, 1 a case failed, 2 setup only, 3 skip, 70 crash. It
 * has run once, on one Mac, on macOS 26.7.3 (25G342) with release 3.0.1's package. */
#import "plugin_common.h"
#include <errno.h>
#include <mach-o/dyld.h>
#include <sched.h>
#include <spawn.h>
#include <sys/wait.h>

extern char **environ;

enum { THREADS = 32, PROCESSES = 10, WRITTEN = 4 };

static id<MTLDevice> g_device;
static _Atomic int g_arrived;
static void *g_textures[THREADS]; // each texture retained by its thread, released by main
static int g_storage[THREADS];

static void *make_texture(void *arg)
{
    const int i = (int)(intptr_t)arg;
    atomic_fetch_add(&g_arrived, 1);
    while (atomic_load(&g_arrived) < THREADS) sched_yield(); // every thread asks at once
    @autoreleasepool {
        MTLTextureDescriptor *d = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA16Float
                                                                                     width:16
                                                                                    height:16
                                                                                 mipmapped:NO];
        d.storageMode = MTLStorageModePrivate;
        d.usage = MTLTextureUsageShaderWrite;
        id<MTLTexture> t = [g_device newTextureWithDescriptor:d];
        g_storage[i] = t ? nm_bool_field(t, "_stex") : -2;
        g_textures[i] = t ? (void *)CFBridgingRetain(t) : NULL;
    }
    return NULL;
}

// one child: exit 0 when every texture has its storage view and every readback is exact, 1 otherwise, 2 setup
static int child(int index)
{
    nm_start(20);
    g_device = nm_device();
    pthread_t threads[THREADS];
    for (int i = 0; i < THREADS; i++)
        if (pthread_create(&threads[i], NULL, make_texture, (void *)(intptr_t)i)) {
            nm_info("process %d: pthread_create failed", index);
            return 2;
        }
    for (int i = 0; i < THREADS; i++) pthread_join(threads[i], NULL);
    int storage = 0, bad = 0;
    for (int i = 0; i < THREADS; i++) {
        if (g_storage[i] == 1)
            storage++;
        else
            nm_info("process %d thread %d: %s", index, i,
                    g_storage[i] == -2 ? "no texture"
                    : g_storage[i] < 0 ? "no _stex field"
                                       : "no storage view (_stex=0)");
    }
    id<MTLCommandQueue> queue = [g_device newCommandQueue];
    NSError *error = nil;
    id<MTLLibrary> library = [g_device
        newLibraryWithSource:
            @"#include <metal_stdlib>\nusing namespace metal;\n"
             "kernel void fill(texture2d<half, access::write> t [[texture(0)]], uint2 p [[thread_position_in_grid]]) {"
             " if (p.x < t.get_width() && p.y < t.get_height()) t.write(half4(0.25h, 0.5h, 0.75h, 1.0h), p); }\n"
                     options:nil
                       error:&error];
    id<MTLFunction> fn = [library newFunctionWithName:@"fill"];
    id<MTLComputePipelineState> pipeline = fn ? [g_device newComputePipelineStateWithFunction:fn error:&error] : nil;
    id<MTLBuffer> out = [g_device newBufferWithLength:256 * 16 options:MTLResourceStorageModeShared];
    if (!pipeline || !out) {
        nm_info("process %d: setup: %s", index,
                error ? error.localizedDescription.UTF8String : "kernel or readback buffer");
        return 2;
    }
    int written = 0;
    for (int i = 0; i < WRITTEN; i++) {
        id<MTLTexture> t = (__bridge id<MTLTexture>)g_textures[i];
        if (!t) continue;
        memset(out.contents, 0, 256 * 16);
        id<MTLCommandBuffer> cb = [queue commandBuffer];
        id<MTLComputeCommandEncoder> encoder = [cb computeCommandEncoder];
        [encoder setComputePipelineState:pipeline];
        [encoder setTexture:t atIndex:0];
        [encoder dispatchThreads:MTLSizeMake(16, 16, 1) threadsPerThreadgroup:MTLSizeMake(8, 8, 1)];
        [encoder endEncoding];
        id<MTLBlitCommandEncoder> blit = [cb blitCommandEncoder];
        [blit copyFromTexture:t
                         sourceSlice:0
                         sourceLevel:0
                        sourceOrigin:MTLOriginMake(0, 0, 0)
                          sourceSize:MTLSizeMake(16, 16, 1)
                            toBuffer:out
                   destinationOffset:0
              destinationBytesPerRow:256
            destinationBytesPerImage:256 * 16];
        [blit endEncoding];
        [cb commit];
        [cb waitUntilCompleted];
        if (cb.status != MTLCommandBufferStatusCompleted) {
            nm_info("process %d texture %d: command buffer status %ld", index, i, (long)cb.status);
            continue;
        }
        const float expected[4] = {0.25f, 0.5f, 0.75f, 1.0f};
        unsigned wrong = 0;
        for (unsigned y = 0; y < 16; y++) {
            const __fp16 *row = (const __fp16 *)((const uint8_t *)out.contents + y * 256);
            for (unsigned x = 0; x < 16 * 4; x++) wrong += (float)row[x] != expected[x % 4];
        }
        if (wrong)
            nm_info("process %d texture %d: %u of 1024 values wrong after a kernel write", index, i, wrong);
        else
            written++;
    }
    for (int i = 0; i < THREADS; i++)
        if (g_textures[i]) CFRelease(g_textures[i]);
    bad = (THREADS - storage) + (WRITTEN - written);
    nm_info("process %d: %d of %d textures with a storage view, %d of %d readbacks exact", index, storage, THREADS,
            written, WRITTEN);
    return bad ? 1 : 0;
}

int main(int argc, char **argv)
{
    @autoreleasepool {
        if (argc == 3 && !strcmp(argv[1], "--child")) return child(atoi(argv[2]));
        nm_start(120);
        (void)nm_device(); // the parent's own device, only to skip early on another driver
        char path[4096];
        uint32_t size = sizeof path;
        if (_NSGetExecutablePath(path, &size)) {
            nm_setup("spawn", "the program's own path is too long");
            return nm_finish();
        }
        nm_info("a smoke check: the old race lasted nanoseconds and device creation may answer the query before "
                "the threads start, so a pass says little about it");
        int passed = 0;
        for (int n = 0; n < PROCESSES; n++) {
            char index[16], name[32];
            snprintf(index, sizeof index, "%d", n);
            snprintf(name, sizeof name, "process-%d", n);
            char *child_argv[] = {path, "--child", index, NULL};
            pid_t pid = 0;
            const int rc = posix_spawn(&pid, path, NULL, NULL, child_argv, environ);
            if (rc) {
                nm_setup(name, "posix_spawn: %s", strerror(rc));
                continue;
            }
            int status = 0;
            pid_t got;
            do got = waitpid(pid, &status, 0);
            while (got < 0 && errno == EINTR);
            if (got != pid) {
                nm_fail(name, "waitpid: %s", strerror(errno));
                continue;
            }
            if (WIFSIGNALED(status) && WTERMSIG(status) == SIGALRM) {
                nm_fail(name, "its own alarm ended it (20 s): the GPU may be hung");
                nm_finish();
                signal(SIGALRM, SIG_DFL);
                raise(SIGALRM); // the runner then stops every later GPU check
                return 1;
            }
            if (WIFSIGNALED(status))
                nm_fail(name, "ended by signal %d", WTERMSIG(status));
            else if (WEXITSTATUS(status) == 0) {
                nm_pass(name, "32 textures with a storage view, 4 exact readbacks");
                passed++;
            } else if (WEXITSTATUS(status) == 1)
                nm_fail(name, "a texture without its storage view or a wrong readback (its INFO lines)");
            else if (WEXITSTATUS(status) == 2)
                nm_setup(name, "the child could not make its kernel or buffer (its INFO line)");
            else if (WEXITSTATUS(status) == 3)
                nm_fail(name, "the child got no device of this driver");
            else if (WEXITSTATUS(status) == 70)
                nm_fail(name, "the child crashed (CRASH on stderr)");
            else
                nm_fail(name, "exit %d", WEXITSTATUS(status));
        }
        if (passed == PROCESSES)
            nm_pass("all",
                    "%d of %d textures had a storage view and %d of %d readbacks were exact in %d fresh processes",
                    THREADS * PROCESSES, THREADS * PROCESSES, WRITTEN * PROCESSES, WRITTEN * PROCESSES, PROCESSES);
        return nm_finish();
    }
}
