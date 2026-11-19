#!/usr/bin/env python3
"""test_host_buffer_release.py: the caller's deallocator of a newBufferWithBytesNoCopy buffer whose pages were imported
gets the caller's pointer and length, and runs only after the import's VkDeviceMemory is freed, so it also waits out
the buffer grace; it runs once, with no lock of the grace held; a copied buffer's deallocator still runs at once; a lost
device (nothing destroys the buffer) gives the pages back in dealloc, as before; a grace record that cannot be allocated
keeps an import and its pages instead of giving them back early.

Compiles NVMTLBuffer's -dealloc and nvmtl_host_buffer_release from plugin/NVMTLObjects.m and nvmtl_vk_buffer_destroy,
nvmtl_buffer_grace_reap and nvmtl_vk_buffer_destroy_body from plugin/nvmtl_vk.c, as they are, with stand-ins for
Vulkan and for the rest of the plugin. The idea and the first test are from pull request 576 of the original project
(luckiest-luk); this one is written for this tree, which counts submits in nvmtl_cmd_life.h, and takes only the
deallocator half of that pull request.

  python3 tools/test_host_buffer_release.py [--baseline [REF]]
      --baseline REF   the sources of that revision (default HEAD); on 3.0.3 (ac9bb6e4) this test fails

Needs clang with Foundation (a Mac with the Xcode command line tools)."""
import argparse
import pathlib
import subprocess
import sys
import tempfile

root = pathlib.Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument('--baseline', nargs='?', const='HEAD', metavar='REF')
args = parser.parse_args()


def source(path):
    if args.baseline:
        return subprocess.check_output(['git', 'show', f'{args.baseline}:{path}'], cwd=root, text=True)
    return (root / path).read_text()


def function(text, signature):
    start = text.index(signature)
    opening = text.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[start:end]


objects = source('plugin/NVMTLObjects.m')
vk = source('plugin/nvmtl_vk.c')
header = source('plugin/nvmtl_vk.h')
buffer_type = header[header.index('typedef struct { void *buf;'):header.index('} nvk_buffer;') + len('} nvk_buffer;')]
grace_type = vk[vk.index('typedef struct nvmtl_bgrace'):vk.index('static int nvmtl_buffer_grace_on')]
implementation = objects[objects.index('@implementation NVMTLBuffer'):]
destructor = function(implementation, '- (void)dealloc {')
helper = 'static void nvmtl_host_buffer_release(void *retained)'
release_helper = function(objects, helper) if helper in objects else ''
backend = '\n'.join(function(vk, signature) for signature in (
    'void nvmtl_vk_buffer_destroy(nvk_buffer *b)\n{',
    'static void nvmtl_buffer_grace_reap(void)\n{',
    'static void nvmtl_vk_buffer_destroy_body(nvk_buffer *b)\n{'))

fixture = r'''
#import <Foundation/Foundation.h>
#include <assert.h>
#include <pthread.h>
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include "nvmtl_page_life.h"
#include "nvmtl_shared_wc.h"
/*BUFFER_TYPE*/
/*GRACE_TYPE*/
typedef void *VkBuffer, *VkDeviceMemory;
static int g_state = 1, grace = 1, failAllocation;
static uint64_t g_cmd_submitted, g_cmd_finished_seq, g_alloc_bytes, g_bar1_bytes, g_bind_lost_bytes, g_bind_lost;
static uint64_t g_buf_gone;
static void *g_dev;
static __thread int t_grace_reaping;
static unsigned destroyed, freed, callbacks;
enum { NVP_BUFDEL, NVMTL_FX_POOLED = 1, NVMTL_FX_PLACED = 2, NVMTL_LV_DYING };
#define nvlog(...) ((void)0)
#define nvtrace(...) ((void)0)
static int nvmtl_trace_on(void) { return 0; }
static uint64_t nvmtl_vk_buffer_address(nvk_buffer *b) { (void)b; return 0; }
static uint64_t nvmtl_perf_now(void) { return 0; }
static void nvmtl_perf_note(int kind, uint64_t time, size_t bytes) { (void)kind; (void)time; (void)bytes; }
static int nvmtl_buffer_grace_on(void) { return grace; }
static void nvmtl_fx_note_buffer_gone(nvk_buffer *b, unsigned flags) { (void)b; (void)flags; }
static void nvmtl_live_relabel(nvk_buffer *b, unsigned kind) { (void)b; (void)kind; }
static void nvmtl_live_remove(nvk_buffer *b) { (void)b; }
static void pool_buffer_release(nvk_buffer *b) { (void)b; }
static void nvmtl_vk_budget_discharge(size_t n) { (void)n; }
static void pvkDestroyBuffer(void *dev, VkBuffer b, void *allocator) { (void)dev; (void)b; (void)allocator; destroyed++; }
static void pvkFreeMemory(void *dev, VkDeviceMemory m, void *allocator) { (void)dev; (void)m; (void)allocator; freed++; }
static void unmap(void *dev, VkDeviceMemory m) { (void)dev; (void)m; }
static void (*pvkUnmapMemory)(void *, VkDeviceMemory) = unmap;
static int nvmtl_ws_free_offmain(nvmtl_bgrace *g) { (void)g; return 0; }   /* an import is never moved off the thread */
static void nvmtl_vk_buffer_destroy_body(nvk_buffer *b);
static void *test_calloc(size_t n, size_t s) { return failAllocation ? NULL : calloc(n, s); }
#define calloc test_calloc
/*BACKEND*/
#undef calloc
/* nvmtl_vk_buffer_retire: an import is never parked, so it is destroyed; nothing is done without a device */
static void nvmtl_vk_buffer_retire(nvk_buffer *b) { if (g_state != 1 || !b || !b->buf) return; nvmtl_vk_buffer_destroy(b); }
typedef struct { int img; } nvk_image;
typedef struct { nvk_image img; } Park;
static void nvmtl_vk_image_destroy(nvk_image *i) { (void)i; }
@interface NVMTLHeap : NSObject
- (void)nvmtlGiveRange:(NSUInteger)o length:(NSUInteger)n;
- (void)nvmtlReleaseSubAllocation:(NSUInteger)n;
@end
@implementation NVMTLHeap
- (void)nvmtlGiveRange:(NSUInteger)o length:(NSUInteger)n { (void)o; (void)n; }
- (void)nvmtlReleaseSubAllocation:(NSUInteger)n { (void)n; }
@end
@interface NVMTLBuffer : NSObject { @public
    nvk_buffer _b, _shadow; void *_hostPtr, *_swc; size_t _hostLen; id _hostDealloc;
    unsigned _nbpk; Park *_bpk; NVMTLHeap *_subHeap;
    NSUInteger _subCharge, _heapOffset; BOOL _subPlaced;
} @end
/*RELEASE_HELPER*/
@implementation NVMTLBuffer
/*DESTRUCTOR*/
@end

static unsigned char cpu[32];
static void reset(void) {
    assert(!g_bg.ncool);
    g_state = 1; grace = 1; failAllocation = 0;
    g_cmd_submitted = 7; g_cmd_finished_seq = 6;   /* submit 7, which may read the buffer, is in flight */
    destroyed = freed = callbacks = 0; g_alloc_bytes = 1 << 20;
}
enum kind { IMPORTED, MANAGED, COPIED };
/* one buffer of 32 bytes over cpu, freed by its last reference; the deallocator checks what it is given and when */
static void drop(enum kind k) {
    @autoreleasepool {
        NVMTLBuffer *b = [NVMTLBuffer new];
        b->_b = (nvk_buffer){ .buf = (void *)1, .mem = (void *)2, .map = cpu, .size = 32, .imported = 1 };
        if (k == MANAGED) {   /* NVMTLDevice_contract.m: the caller's pages imported as the VRAM copy's shadow */
            b->_b = (nvk_buffer){ .buf = (void *)1, .mem = (void *)2, .size = 32, .alloc = 32 };
            b->_shadow = (nvk_buffer){ .buf = (void *)3, .mem = (void *)4, .map = cpu, .size = 32, .imported = 1 };
            b->_hostPtr = cpu; b->_hostLen = 32;
        } else if (k == COPIED) {   /* an allocation of the driver's own, the bytes copied from the caller's */
            b->_b = (nvk_buffer){ .buf = (void *)1, .mem = (void *)2, .map = cpu + 16, .size = 32, .alloc = 32 };
            b->_hostPtr = cpu; b->_hostLen = 32;
        }
        const BOOL import = k != COPIED, lost = g_state != 1;
        b->_hostDealloc = ^(void *ptr, NSUInteger length) {
            assert(ptr == cpu && length == 32);
            /* pages of an import go back only once the GPU memory over them is freed (none is freed without a device) */
            if (import && !lost) assert(freed >= 1 && freed == destroyed);
            /* the caller's deallocator can call back into the driver: no lock of the grace may be held */
            assert(!pthread_mutex_trylock(&g_bg_lock)); pthread_mutex_unlock(&g_bg_lock);
            callbacks++;
        };
        b = nil;
    }
}
static void reap_all(void) { g_cmd_finished_seq = g_cmd_submitted; nvmtl_buffer_grace_reap(); }

int main(void) { @autoreleasepool {
    /* an import with a submit in flight: the pages wait for that submit, and are given back once */
    reset(); drop(IMPORTED);
    assert(callbacks == 0 && freed == 0 && g_bg.ncool == 1);
    nvmtl_buffer_grace_reap(); assert(callbacks == 0);
    reap_all(); assert(callbacks == 1 && freed == 1 && destroyed == 1 && !g_bg.ncool);
    nvmtl_buffer_grace_reap(); assert(callbacks == 1);

    /* the managed path's shadow import: the same, after both buffers are freed */
    reset(); drop(MANAGED);
    assert(callbacks == 0);
    reap_all(); assert(callbacks == 1 && freed == 2 && destroyed == 2);

    /* a copied buffer: the caller's pages were never the GPU's; given back at once, with the caller's pointer */
    reset(); drop(COPIED);
    assert(callbacks == 1);
    reap_all(); assert(callbacks == 1 && freed == 1);

    /* an import with nothing in flight, and one without the grace: given back at once, after vkFreeMemory */
    reset(); g_cmd_finished_seq = 7; drop(IMPORTED); assert(callbacks == 1 && freed == 1);
    reset(); grace = 0; drop(IMPORTED); assert(callbacks == 1 && freed == 1);

    /* a lost device: nothing destroys the buffer, so dealloc gives the pages back itself, once */
    reset(); g_state = 0; drop(IMPORTED); assert(callbacks == 1 && freed == 0);
    g_state = 1; reap_all(); assert(callbacks == 1);

    /* no grace record: the import and its pages are kept rather than given back under a submit in flight */
    reset(); failAllocation = 1; drop(IMPORTED);
    assert(callbacks == 0 && freed == 0 && destroyed == 0);
    failAllocation = 0; reap_all(); assert(callbacks == 0);
    puts("PASS: an imported buffer's deallocator gets the caller's pointer and length, runs once after vkFreeMemory and "
         "after the grace, with no grace lock held; a copied buffer's runs at once; a lost device runs it in dealloc; "
         "no grace record keeps the import and its pages");
} }
'''
for token, value in {'BUFFER_TYPE': buffer_type, 'GRACE_TYPE': grace_type, 'BACKEND': backend,
                     'RELEASE_HELPER': release_helper, 'DESTRUCTOR': destructor}.items():
    fixture = fixture.replace(f'/*{token}*/', value)
with tempfile.TemporaryDirectory(prefix='nvmtl-host-release-') as directory:
    p = pathlib.Path(directory)
    (p / 'release.m').write_text(fixture)
    for label, flags in (('release', []), ('sanitized', ['-fsanitize=address,undefined'])):
        built = subprocess.run(['xcrun', 'clang', '-fobjc-arc', '-O1', '-g', *flags, '-I', str(root / 'plugin'),
                                str(p / 'release.m'), '-framework', 'Foundation', '-o', str(p / 'release')])
        if built.returncode:
            print(f'FAIL: the fixture did not build ({label})'); sys.exit(1)
        ran = subprocess.run([str(p / 'release')], timeout=60)
        if ran.returncode:
            print(f'FAIL: the deallocator ran early, late, twice or with the wrong pointer ({label})'); sys.exit(1)
        print('PASS configuration:', label, flush=True)
