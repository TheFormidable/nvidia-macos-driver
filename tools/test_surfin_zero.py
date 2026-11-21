#!/usr/bin/env python3
"""NVMTL_SURFIN_ZERO driven through the plugin's own code: nvmtlSurfaceIn, nvmtl_surfin_zero_try, nvmtl_surf_zero_flush
and nvmtl_pre_redo are lifted from plugin/NVMTLObjects.m into a fixture with stub surfaces, uploads and prologue.

Builds the way tools/test_surface_publication.py does (clang -fobjc-arc -framework Foundation): on a Mac, or on Linux
with the GNUstep wrapper first on PATH. Four runs: WindowServer, an application, WindowServer with
NVMTL_SURFIN_ZERO=0, and WindowServer with NVMTL_PRESUBMIT_PROLOGUE=0 (S1b is off without the prologue)."""
import os
import pathlib
import subprocess
import sys
import tempfile

root = pathlib.Path(__file__).resolve().parents[1]
source = (root / 'plugin/NVMTLObjects.m').read_text()


def body(head, end='\n}\n'):
    """The text from `head` to the first `end` after it (end included)."""
    i = source.index(head)
    return source[i:source.index(end, i) + len(end)]


redo = body('static void nvmtl_pre_redo(void *held) {')
release = body('static void nvmtl_pre_release(void *held) {', '}\n')
z0 = source.index('#include "nvmtl_zero_scan.h"')
zero = source[z0:source.index('static void nvmtl_presubmit(void) {', z0)]
s0 = source.index('- (void)nvmtlSurfaceIn {')
surfin = source[s0:source.index('- (BOOL)nvmtlSurfaceOut {', s0)]
for name, text in (('nvmtl_pre_redo', redo), ('the zero block', zero), ('nvmtlSurfaceIn', surfin)):
    assert len(text) > 200, f'{name} not found in plugin/NVMTLObjects.m'

fixture = r'''
#import <Foundation/Foundation.h>
#include <assert.h>
#include <os/lock.h>
#include <pthread.h>
#include <stdatomic.h>
#include <dispatch/dispatch.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#import <objc/runtime.h>
typedef struct __IOSurface { unsigned seed, planes; size_t row; unsigned char cpu[64]; } Surface;
typedef int IOReturn;
enum { kIOSurfaceLockReadOnly = 1 };
static const char *progname = "WindowServer";
#define getprogname() progname
#define nvlog(...) ((void)0)
static uint32_t IOSurfaceGetSeed(IOSurfaceRef s) { return s->seed; }
static unsigned IOSurfaceGetID(IOSurfaceRef s) { (void)s; return 7; }
static unsigned locks;
static IOReturn IOSurfaceLock(IOSurfaceRef s, unsigned o, uint32_t *seed) { (void)o; locks++; if (seed) *seed = s->seed; return 0; }
static IOReturn IOSurfaceUnlock(IOSurfaceRef s, unsigned o, uint32_t *seed) { (void)s; (void)o; (void)seed; return 0; }
static void *IOSurfaceGetBaseAddressOfPlane(IOSurfaceRef s, uint32_t p) { (void)p; return s->cpu; }
static void *IOSurfaceGetBaseAddress(IOSurfaceRef s) { return s->cpu; }
static size_t IOSurfaceGetBytesPerRowOfPlane(IOSurfaceRef s, uint32_t p) { (void)p; return s->row; }
static size_t IOSurfaceGetBytesPerRow(IOSurfaceRef s) { return s->row; }
static size_t IOSurfaceGetAllocSize(IOSurfaceRef s) { return sizeof s->cpu; }
static size_t IOSurfaceGetPlaneCount(IOSurfaceRef s) { return s->planes; }
typedef struct { unsigned w, h, bpp; void *img; } Image;
typedef struct { void *buf; size_t size; void *map; } nvk_buffer;
typedef struct { int open; } nvk_cmdbuf;
typedef void nvk_queue;
static nvk_queue *nvmtl_device_queue(void) { return NULL; }
// uploads: every upload the surface-in made (both kinds); tags: every VRAM tag it published
static unsigned uploads, tagSets, gpu_tag;
static int nvmtl_vk_image_write_host(void *q, Image *i, void *b, size_t row, size_t n, void *c) { uploads++; return 0; }
static int nvmtl_vk_image_write(void *q, Image *i, void *b, size_t row) { uploads++; return 0; }
static int nvmtl_vram_tag_current(IOSurfaceRef s, NSUInteger p, unsigned seed) { return gpu_tag && gpu_tag == seed; }
static void nvmtl_vram_tag_set(IOSurfaceRef s, NSUInteger p, unsigned seed) { tagSets++; gpu_tag = seed; }
static unsigned nvmtl_vram_gen(IOSurfaceRef s, NSUInteger p) { return 1; }
static int nvmtl_busy_foreign(IOSurfaceRef s, NSUInteger p) { return 0; }
static unsigned busyWaits;
static void nvmtl_busy_wait(IOSurfaceRef s, NSUInteger p) { busyWaits++; }
static void nvmtl_vk_fence_wait_compositor(uint32_t sid) { (void)sid; }
static int nvmtl_vk_copy_image(void *q, Image *a, uint32_t ax, uint32_t ay, Image *b, uint32_t bx, uint32_t by, uint32_t w,
                               uint32_t h) { return 0; }
static void nvmtl_frame_dump(const char *t, void *b, size_t r, unsigned w, unsigned h) {}
static void nvmtl_vk_host_pages_key(uint32_t s, uint64_t c) {}
static uint64_t nvmtl_host_pages_cookie(IOSurfaceRef s) { return 0; }
static bool nvmtl_surfin_import(void) { return true; }
static BOOL nvmtl_plane_seed_on(void) { return NO; }
static os_unfair_lock g_ps_lock = OS_UNFAIR_LOCK_INIT; static int g_ps;
static int nvmtl_ps_covers(int *p, ...) { return 0; }
static uint64_t nvmtl_plane_cookie(IOSurfaceRef s, BOOL c) { return 0; }
static unsigned long long g_surfin_plane_skip, g_surfin_skip, g_surfin_copy;
// the pre-submit prologue: open answers preOpen; each fill is recorded; holds are kept (retained) for the test
static int preOpen = 1, cmdAnswer = 0, waitAnswer = 0;
static unsigned fills, cmdFills; static nvk_buffer *fillBuf[16]; static size_t fillOff[16], fillLen[16];
static void *holds[16]; static unsigned nholds;
static void (*nvmtl_pre_submit_hook)(void);
static void (*nvmtl_pre_release_hook)(void *);
static void (*nvmtl_pre_redo_hook)(void *);
static int nvmtl_vk_pre_open(void) { return preOpen; }
static int nvmtl_vk_pre_fill(nvk_buffer *b, size_t off, size_t n, uint32_t w) {
    if ((off | n) & 3 || n > b->size) return -1;
    fillBuf[fills & 15] = b; fillOff[fills & 15] = off; fillLen[fills & 15] = n; fills++; return 0; }
static void nvmtl_vk_pre_hold(void *r) { holds[nholds++ & 15] = r; }
// keepUntilFlush: the test's own reference to a root, dropped once the flush has taken its set, so that the flush's
// reference is the last one
static id keepUntilFlush;
static int nvmtl_vk_cmd_begin(nvk_queue *q, nvk_cmdbuf *c) { c->open = 1; keepUntilFlush = nil; return cmdAnswer; }
static int nvmtl_vk_cmd_fill_buffer(nvk_cmdbuf *c, nvk_buffer *b, size_t off, size_t n, uint8_t v) {
    if (n > b->size) return -1; cmdFills++; return 0; }
static int nvmtl_vk_submit_wait(nvk_cmdbuf *c) { return waitAnswer; }
static void nvmtl_presubmit(void) {}
@interface NVMTLBuffer : NSObject { @public nvk_buffer _b, _shadow; } @end
@implementation NVMTLBuffer @end
static unsigned marks;
static void nvmtl_managed_mark(NVMTLBuffer *b, NSUInteger lo, NSUInteger hi) { marks++; }
static pthread_t mainThread; static _Atomic int deallocs, deallocsOnMain; static void *watch;
@interface NVMTLTexture : NSObject {
@public NVMTLTexture *_parent; IOSurfaceRef _surf; BOOL _vramOn, _surfSynced, _clientWritten; void *_hostPages;
    uint32_t _surfSeed, _surfGen; NSUInteger _plane; Image _i, _surfaceLinear; void *_q; IOSurfaceRef _y422Surf;
    nvk_buffer _vramBuf; int _zeroLost; BOOL _zeroTried;
}
- (BOOL)nvmtlSurfaceCopyable;
- (void)nvmtlY422SurfaceIn;
- (void)nvmtlSurfaceIn;
@end
static NSObject *monitor;
static id nvmtl_surface_sync(NVMTLTexture *t) { return monitor; }
'''

checks = r'''
@implementation NVMTLTexture
- (BOOL)nvmtlSurfaceCopyable { return YES; }
- (void)nvmtlY422SurfaceIn { assert(!"4:2:2"); }
- (void)dealloc { if (_surf == watch) { deallocs++; if (pthread_equal(pthread_self(), mainThread)) deallocsOnMain++; } }
'''

main = r'''
@end
static Surface *surface(unsigned seed) {
    Surface *s = calloc(1, sizeof *s); s->seed = seed; s->planes = 1; s->row = 16; return s;   // 4 rows of 16 bytes
}
static NVMTLTexture *root(Surface *s) {
    NVMTLTexture *t = [NVMTLTexture new]; t->_surf = s; t->_vramOn = YES; t->_i = (Image){ .w = 4, .h = 4, .bpp = 4 };
    t->_vramBuf = (nvk_buffer){ .buf = (void *)1, .size = 64 }; return t;
}
static unsigned long long scans(void) { return atomic_load(&gSurfZeroScans); }
static unsigned owed(void) { return (unsigned)gSurfZeroOwed.allObjects.count; }
int main(int argc, char **argv) { @autoreleasepool {
    mainThread = pthread_self(); monitor = [NSObject new];
    const int app = argc > 1 && !strcmp(argv[1], "app"), off = argc > 1 && !strcmp(argv[1], "off");
    if (app) progname = "java";
    if (app || off) {
        // an application, or WindowServer with NVMTL_SURFIN_ZERO=0 or the prologue off: a zero surface is uploaded and
    // tagged, as in 3.0
        Surface *s = surface(3); NVMTLTexture *t = root(s);
        [t nvmtlSurfaceIn];
        assert(uploads == 1 && tagSets == 1 && !scans() && !owed() && t->_surfSynced);
        printf("PASS: %s uploads a zero surface as before\n", app ? "an application"
               : getenv("NVMTL_PRESUBMIT_PROLOGUE") ? "NVMTL_PRESUBMIT_PROLOGUE=0" : "NVMTL_SURFIN_ZERO=0");
        return 0;
    }
    // a new surface nobody wrote (the capture surface): no upload, one owed root, synced at the lock's seed, no tag
    Surface *s = surface(5); NVMTLTexture *t = root(s);
    [t nvmtlSurfaceIn];
    assert(uploads == 0 && scans() == 1 && owed() == 1 && t->_surfSynced && t->_surfSeed == 5 && tagSets == 0);
    assert(nvmtl_pre_submit_hook == nvmtl_presubmit);
    [t nvmtlSurfaceIn]; assert(uploads == 0 && scans() == 1);   // current: nothing again
    // the flush: one fill of row * h at offset 0 into the root's VRAM import, in the prologue, which holds the root
    nvmtl_surf_zero_flush();
    assert(fills == 1 && fillBuf[0] == &t->_vramBuf && fillOff[0] == 0 && fillLen[0] == 64 && nholds == 1 && !owed());
    assert(nvmtl_pre_release_hook == nvmtl_pre_release && nvmtl_pre_redo_hook == nvmtl_pre_redo);
    // a re-sync after a CPU write (the seed moved; pages still zero): uploaded at once, no second scan (attack S1b-3)
    s->seed++; [t nvmtlSurfaceIn];
    assert(uploads == 1 && scans() == 1 && tagSets == 1 && t->_surfSeed == 6);
    // the prologue's End failed (or its submit was dropped): its hold goes to the redo hook before its release, as
    // nvmtl_vk.c now does on both paths (attack S1b-1). The root decides again at its next surface-in, though its seed
    // has not moved: scanned again, zero again, owed again; then it uploads when a byte is set by the time it decides
    { Surface *e = surface(9); NVMTLTexture *u = root(e); const unsigned h0 = nholds, f0 = fills;
      [u nvmtlSurfaceIn]; nvmtl_surf_zero_flush(); assert(fills == f0 + 1 && nholds == h0 + 1);
      void *held = holds[h0 & 15];
      nvmtl_pre_redo_hook(held); nvmtl_pre_release_hook(held);
      assert(u->_zeroLost == 1);
      const unsigned long long sc = scans(); const unsigned up = uploads;
      [u nvmtlSurfaceIn];
      assert(u->_zeroLost == 0 && scans() == sc + 1 && uploads == up && owed() == 1 && u->_surfSynced);
      nvmtl_surf_zero_flush(); held = holds[(nholds - 1) & 15]; nvmtl_pre_redo_hook(held); nvmtl_pre_release_hook(held);
      e->cpu[63] = 1; [u nvmtlSurfaceIn];
      assert(scans() == sc + 2 && uploads == up + 1 && !owed() && tagSets >= 2);
      // and a hold of the managed flush on the same path still has its ranges marked again
      NVMTLBuffer *b = [NVMTLBuffer new]; b->_b.size = 64; b->_shadow.map = (void *)1; const unsigned m0 = marks;
      nvmtl_pre_redo((__bridge void *)@[@[b, @0, @64], b]); assert(marks == m0 + 1);
    }
    // one non-zero byte at the last position: uploaded as before, nothing owed
    { Surface *n = surface(11); n->cpu[63] = 0x40; NVMTLTexture *u = root(n); const unsigned up = uploads;
      [u nvmtlSurfaceIn]; assert(uploads == up + 1 && !owed() && u->_surfSynced); }
    // a surface another process has drawn into (_clientWritten): today's synchronous upload (attack S1b-2)
    { Surface *c = surface(12); NVMTLTexture *u = root(c); u->_clientWritten = YES; const unsigned up = uploads;
      const unsigned long long sc = scans();
      [u nvmtlSurfaceIn]; assert(uploads == up + 1 && !owed() && scans() == sc); }
    // the tiled image of a Managed wrapper, a two-plane surface, a private image: as before
    { Surface *m = surface(13); NVMTLTexture *u = root(m); u->_surfaceLinear.img = (void *)1; unsigned up = uploads;
      [u nvmtlSurfaceIn]; assert(uploads == up + 1 && !owed());
      Surface *p = surface(14); p->planes = 2; u = root(p); up = uploads;
      [u nvmtlSurfaceIn]; assert(uploads == up + 1 && !owed());
      Surface *q = surface(15); u = root(q); u->_vramOn = NO; up = uploads;
      [u nvmtlSurfaceIn]; assert(uploads == up + 1 && !owed()); }
    // a root released before the flush gets no fill
    { const unsigned f0 = fills;
      @autoreleasepool { Surface *g = surface(16); NVMTLTexture *u = root(g); [u nvmtlSurfaceIn]; assert(owed() == 1); }
      nvmtl_surf_zero_flush(); assert(fills == f0); }
    // the prologue full or off: the fills are their own submit, waited for; the roots are released on another
    // thread, never here inside a submit (attack S1b-7); a failed submit marks every root to decide again
    { preOpen = 0; const unsigned c0 = cmdFills; Surface *g = surface(17); watch = g;
      @autoreleasepool { NVMTLTexture *u = root(g); [u nvmtlSurfaceIn]; assert(owed() == 1); keepUntilFlush = u; }
      // in a pool, as nvmtl_presubmit calls it, popped only after the worker has released (or 1 s): a reference the
      // flush left in the caller's pool is then the root's last release, here (deallocsOnMain), not a silent leak
      @autoreleasepool { nvmtl_surf_zero_flush(); assert(cmdFills == c0 + 1 && !keepUntilFlush);
        for (int i = 0; i < 500 && !atomic_load(&deallocs); i++) [NSThread sleepForTimeInterval:0.002]; }
      fprintf(stderr, "S1b-7: the watched root was freed %d time(s), %d of them on the submitting thread (wanted 1 and 0)\n",
              atomic_load(&deallocs), atomic_load(&deallocsOnMain));
      assert(atomic_load(&deallocs) == 1 && atomic_load(&deallocsOnMain) == 0);
      Surface *h = surface(18); NVMTLTexture *u = root(h); [u nvmtlSurfaceIn];
      waitAnswer = -1; nvmtl_surf_zero_flush(); assert(u->_zeroLost == 1);
      const unsigned up = uploads; h->cpu[0] = 1; [u nvmtlSurfaceIn]; assert(uploads == up + 1 && !u->_zeroLost);
      waitAnswer = 0; preOpen = 1; }
    puts("PASS: a zero surface owes one GPU fill and no upload or tag; a re-sync uploads without a scan; a fill whose "
         "prologue failed makes the surface decide again; a non-zero byte, a client-written, tiled, two-plane or private "
         "surface uploads as before; a released root gets no fill; the fallback submit releases its roots off the "
         "submitting thread and marks them on failure");
} return 0; }
'''

with tempfile.TemporaryDirectory() as directory:
    path = pathlib.Path(directory)
    (path / 'test.m').write_text(fixture + release + redo + zero + checks + surfin + main)
    r = subprocess.run(['clang', '-fobjc-arc', '-I', str(root / 'plugin'), '-framework', 'Foundation',
                        str(path / 'test.m'), '-o', str(path / 'test')])
    if r.returncode:
        sys.exit(1)
    # the switches unset unless a run sets one, whatever the caller's environment holds
    clean = {k: v for k, v in os.environ.items() if k not in ('NVMTL_SURFIN_ZERO', 'NVMTL_PRESUBMIT_PROLOGUE')}
    subprocess.run([str(path / 'test')], check=True, env=clean)
    subprocess.run([str(path / 'test'), 'app'], check=True, env=clean)
    subprocess.run([str(path / 'test'), 'off'], check=True, env={**clean, 'NVMTL_SURFIN_ZERO': '0'})
    subprocess.run([str(path / 'test'), 'off'], check=True, env={**clean, 'NVMTL_PRESUBMIT_PROLOGUE': '0'})
