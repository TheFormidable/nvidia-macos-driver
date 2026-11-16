#!/usr/bin/env python3
"""Force CPU import to overlap GPU surface publication in production methods."""
import os
import pathlib
import subprocess
import tempfile

root = pathlib.Path(__file__).resolve().parents[1]
source = (root / 'plugin/NVMTLObjects.m').read_text()
start = source.index('static id nvmtl_surface_sync(')
end = source.index('- (NSUInteger)width ', start)
methods = source[start:end]
# The 4:2:2 surface reader (packed video frames, decoded by the CPU) sits in this stretch and has its own tests
# (tools/test_yuv422.c, tools/test_y422_convert.c); the stand-in texture below is not a 4:2:2 one, so the method is left out and its flag stays NULL.
y0 = methods.index('- (void)nvmtlY422SurfaceIn {'); y1 = methods.index('\n}\n', y0) + 3
assert methods[y1:].startswith('- (void)nvmtlSurfaceIn {'), 'the 4:2:2 reader is no longer right before nvmtlSurfaceIn'
methods = methods[:y0] + methods[y1:]
fixture = r'''
#import <Foundation/Foundation.h>
#include <assert.h>
#include <os/lock.h>
#include <pthread.h>
#include <stdatomic.h>
#include <dispatch/dispatch.h>
#include <stdint.h>
#include <string.h>
#include <time.h>
#import <objc/runtime.h>
typedef struct __IOSurface { unsigned seed; unsigned char cpu[4], cpu1[4]; unsigned planes; } Surface;   // cpu1: plane 1's own bytes
typedef int IOReturn;
enum { kIOSurfaceLockReadOnly = 1 };
typedef struct { unsigned w,h,bpp; unsigned char gpu[4]; void *img; } Image;   // img: set on a tiled image's linear import
static dispatch_semaphore_t entered, release_read;
static unsigned uploads, gpu_seed;
static unsigned long long g_surfin_skip, g_surfin_copy;
// the test's own thread: what the driver does on the caller's thread is told apart from what it does off it
static pthread_t mainThread;
static int onMain(void) { return pthread_equal(pthread_self(), mainThread); }
// bumpAtOffCall = N: the Nth read of a seed off the test's thread first moves it, as someone else's write landing there
static int bumpAtOffCall; static _Atomic int offSeedCalls;
static uint32_t IOSurfaceGetSeed(IOSurfaceRef s) { if (bumpAtOffCall && !onMain() && atomic_fetch_add(&offSeedCalls, 1) + 1 == bumpAtOffCall) s->seed++; return s->seed; }
static unsigned IOSurfaceGetID(IOSurfaceRef s) { return 1; }
static IOReturn IOSurfaceLock(IOSurfaceRef s, unsigned opts, uint32_t *seed) {
    if (!opts) s->seed++;
    if (seed) *seed=s->seed;
    return 0;
}
static IOReturn IOSurfaceUnlock(IOSurfaceRef s, unsigned opts, uint32_t *seed) {
    if (seed) *seed=s->seed;
    return 0;
}
static void *IOSurfaceGetBaseAddressOfPlane(IOSurfaceRef s, uint32_t p) { return p ? s->cpu1 : s->cpu; }
static size_t IOSurfaceGetBytesPerRowOfPlane(IOSurfaceRef s, uint32_t p) { return 4; }
static void *IOSurfaceGetBaseAddress(IOSurfaceRef s) { return s->cpu; }
static size_t IOSurfaceGetAllocSize(IOSurfaceRef s) { return sizeof s->cpu; }
// the imported-pages upload is unavailable here, so every upload takes the CPU copy this test checks
// the host-page fast paths (#399) report "not available", so the copies below take the plain path this test checks
static int nvmtl_vk_image_write_host(void *q, Image *img, void *base, size_t row, size_t bytes, void *cache) { return -1; }
static int nvmtl_vk_image_read_host(void *q, Image *img, void *base, size_t row, size_t bytes, void *cache) { return -1; }
// the planes of one surface read back together (NVMTL_SURFOUT_PLANES): each plane's pixels into its own pages, counted.
// planeReadAnswer: 0 = read; -1 = a plane's pages cannot be imported and nothing was recorded (each plane is then read
// on its own); -2 = the submission failed
typedef Image nvk_image; typedef void *nvk_buffer;
static unsigned planeReads; static int planeReadAnswer, planeReadN;
static int nvmtl_vk_image_read_host_planes(void *q, int n, nvk_image *const *img, void *const *base, const size_t *row,
                                           const size_t *bytes, nvk_buffer *const *cache) {
    planeReads++; planeReadN=n; if (planeReadAnswer) return planeReadAnswer;
    for (int i = 0; i < n; i++) memcpy(base[i], img[i]->gpu, 4); return 0;
}
static unsigned vram_generation=1;   // the VRAM's generation: moved by hand where another writer published into it
static uint32_t nvmtl_vram_gen(IOSurfaceRef s, NSUInteger p) { return vram_generation; }
// the VRAM tags: plane 0's is gpu_seed, as before; the planes above it (the two-plane arm) have their own. tagSets
// counts every write of a tag's seed that is not the marked plane's own bump.
static unsigned plane_tag[4], tagSets;
static os_unfair_lock g_vram_tag_lock = OS_UNFAIR_LOCK_INIT;
static int nvmtl_vram_tag_current(IOSurfaceRef s, NSUInteger p, unsigned seed) { return (p ? plane_tag[p & 3] : gpu_seed) == seed; }
static void nvmtl_vram_tag_write(IOSurfaceRef s, NSUInteger p, unsigned seed, unsigned gen) { (void)gen; tagSets++; if (p) plane_tag[p & 3]=seed; else gpu_seed=seed; }
static void nvmtl_vram_tag_set(IOSurfaceRef s, NSUInteger p, unsigned seed) { nvmtl_vram_tag_write(s, p, seed, 1); }
static unsigned nvmtl_vram_tag_bump(IOSurfaceRef s, NSUInteger p, unsigned seed) { if (p) plane_tag[p & 3]=seed; else gpu_seed=seed; return vram_generation; }
// what this process's planar self-test measured: 1 = each of its marks moved the surface's seed by exactly one
static int seedMeasured=1;
static int nvmtl_vk_plane_mark_moves_seed(void) { return seedMeasured; }
// the kernel's VRAM mark: dirtyAnswer for the blocking one (1 = refused, the copy path), tryAnswer for the try that never
// waits for the accelerator lock (1 = the lock was taken, -1 = refused); each call counted. A mark that takes moves the
// seed of markMoves (the surface of the two-plane arm; NULL elsewhere), as the kernel's does.
// tries counts every try, mainTries those made on the caller's (the test's) thread; offAnswer, when not NO_ANSWER, is
// what a try off that thread hears instead of tryAnswer.
enum { NO_ANSWER = 99 };
static int dirtyAnswer=1, tryAnswer=-1, offAnswer=NO_ANSWER; static _Atomic unsigned blockingDirties, tries, mainTries; static Surface *markMoves;
static int nvmtl_vk_surface_dirty(unsigned s, unsigned p) { blockingDirties++; if (!dirtyAnswer && markMoves) markMoves->seed++; return dirtyAnswer; }
static int nvmtl_vk_surface_dirty_try(unsigned s, unsigned p) {
    tries++; if (onMain()) mainTries++;
    const int answer = (!onMain() && offAnswer != NO_ANSWER) ? offAnswer : tryAnswer;
    if (!answer && markMoves) markMoves->seed++;
    return answer;
}
static int nvmtl_noseed(void) { return 0; }
// own-write tracking across planes (nvmtl_plane_seed.h) is off unless NVMTL_PLANE_SEED=1; its surface-property and page-key
// calls are stubs here, so every arm below runs the default path
static size_t IOSurfaceGetPlaneCount(IOSurfaceRef s) { return s->planes ? s->planes : 1; }
static CFTypeRef IOSurfaceCopyValue(IOSurfaceRef s, CFStringRef k) { (void)s; (void)k; return NULL; }
static void IOSurfaceSetValue(IOSurfaceRef s, CFStringRef k, CFTypeRef v) { (void)s; (void)k; (void)v; }
static void nvmtl_vk_host_pages_key(uint32_t surface, uint64_t cookie) { (void)surface; (void)cookie; }
// cross-process busy marks (PR #398): who wrote the surface last, and the waits for another process's write
static int busyForeign=0, busyWaits=0;
static int nvmtl_busy_foreign(IOSurfaceRef s, NSUInteger p) { (void)s; (void)p; return busyForeign; }
static void nvmtl_busy_wait(IOSurfaceRef s, NSUInteger p) { (void)s; (void)p; busyWaits++; }
// the kernel surface fence: a reader's wait for a surface WindowServer may still be drawing (nvmtl_vk.c), counted
static int compositorWaits=0;
static void nvmtl_vk_fence_wait_compositor(uint32_t sid) { (void)sid; compositorWaits++; }
static void nvmtl_frame_dump(const char *tag, void *base, size_t row, unsigned w, unsigned h) {}
#define nvlog(...) ((void)0)
// helpers the capture path calls (NVMTLEventSync.m); WindowServer kernel marks are off here
static BOOL nvmtl_ws_kernel_waits(void){return NO;}
static void nvmtl_ws_mark(uint32_t sid,BOOL pending){(void)sid;(void)pending;}
static void nvtrace(const char *f,...){(void)f;}
// the GPU copy between a Managed wrapper's tiled image and its linear VRAM import, counted; copyFailure makes it fail
static unsigned gpuCopies; static int copyFailure;
static int nvmtl_vk_copy_image(void *q, Image *src, uint32_t sx, uint32_t sy, Image *dst, uint32_t dx, uint32_t dy, uint32_t w, uint32_t h) {
    gpuCopies++; if (copyFailure) return -1; memcpy(dst->gpu, src->gpu, 4); return 0;
}
static int nvmtl_vk_image_write(void *q, Image *i, void *base, size_t row) {
    memcpy(i->gpu,base,4); uploads++; return 0;
}
static Image *failRead;   // the image whose plain readback fails (the planes-together arm)
static int nvmtl_vk_image_read(void *q, Image *i, void *base, size_t row) {
    if (i == failRead) return -1;
    dispatch_semaphore_signal(entered);
    assert(!dispatch_semaphore_wait(release_read,dispatch_time(DISPATCH_TIME_NOW,5*NSEC_PER_SEC)));
    memcpy(base,i->gpu,4); return 0;
}
@interface NVMTLTexture : NSObject {
@public NVMTLTexture *_parent; IOSurfaceRef _surf; BOOL _vramOn, _surfSynced, _clientWritten; void *_hostPages;
    int _zeroLost; BOOL _zeroTried;   // NVMTL_SURFIN_ZERO: tools/test_surfin_zero.py drives it; here it never answers
    uint32_t _surfSeed, _surfGen; NSUInteger _plane; Image _i, _surfaceLinear; void *_q;
    IOSurfaceRef _y422Surf;   // NULL: not a 4:2:2 texture
}
- (void)nvmtlY422SurfaceIn;
- (BOOL)nvmtlSurfaceCopyable;
- (void)nvmtlSurfaceIn;
- (BOOL)nvmtlSurfaceOut;
- (BOOL)nvmtlSurfaceOutDirtied:(BOOL)dirtied;
@end
// the all-zero surface-in (NVMTL_SURFIN_ZERO, outside the lifted stretch) always says "upload", so every arm here runs
// the upload path it checks; tools/test_surfin_zero.py lifts the real one
@class NVMTLTexture;
static int nvmtl_surfin_zero_try(NVMTLTexture *r, const void *b, size_t row) { (void)r; (void)b; (void)row; return 0; }
@implementation NVMTLTexture
- (BOOL)nvmtlSurfaceCopyable { return YES; }
- (void)nvmtlY422SurfaceIn { assert(!"a non-4:2:2 texture took the 4:2:2 path"); }
'''
checks = r'''
@end
int main(int argc, char **argv) { @autoreleasepool {
    mainThread=pthread_self();
    // the third run, with NVMTL_SURFOUT_PLANES=0 and NVMTL_PLANE_FOLLOW=0 in the environment: both ways back answer off,
    // and a mark of one plane leaves the other plane's tag where it was, as before
    if (argc > 1 && !strcmp(argv[1], "off")) {
        assert(!nvmtl_surfout_planes_on() && !nvmtl_plane_follow_on());
        Surface vs={.seed=20,.planes=2}; NVMTLTexture *v0=[NVMTLTexture new], *v1=[NVMTLTexture new];
        v0->_surf=&vs; v1->_surf=&vs; v1->_plane=1; v0->_vramOn=YES; v1->_vramOn=YES; v0->_i=(Image){.w=1,.h=1,.bpp=4}; v1->_i=v0->_i;
        markMoves=&vs; tryAnswer=0;
        assert([v0 nvmtlSurfaceOutDirtied:YES] && [v1 nvmtlSurfaceOutDirtied:YES] && vs.seed==22);
        assert(nvmtl_vram_tag_current(&vs,1,22) && !nvmtl_vram_tag_current(&vs,0,22) && !tagSets);
        puts("PASS: NVMTL_SURFOUT_PLANES=0 and NVMTL_PLANE_FOLLOW=0 turn both off");
        return 0;
    }
    assert(nvmtl_surfout_planes_on() && nvmtl_plane_follow_on());   // on unless asked otherwise
    Surface s={.seed=1};
    NVMTLTexture *t=[NVMTLTexture new]; t->_surf=&s; t->_i=(Image){.w=1,.h=1,.bpp=4,.gpu={0,128,255,255}};
    NVMTLTexture *alias=[NVMTLTexture new];
    if (argc > 1) { alias->_surf=&s; alias->_vramOn=YES; t->_vramOn=YES; }
    else alias->_parent=t;
    entered=dispatch_semaphore_create(0); release_read=dispatch_semaphore_create(0);
    dispatch_group_t group=dispatch_group_create();
    dispatch_queue_t queue=dispatch_get_global_queue(QOS_CLASS_USER_INITIATED,0);
    dispatch_group_async(group,queue,^{ @autoreleasepool { assert([t nvmtlSurfaceOut]); } });
    assert(!dispatch_semaphore_wait(entered,dispatch_time(DISPATCH_TIME_NOW,5*NSEC_PER_SEC)));
    dispatch_semaphore_t importing=dispatch_semaphore_create(0);
    dispatch_group_async(group,queue,^{ @autoreleasepool {
        dispatch_semaphore_signal(importing); [alias nvmtlSurfaceIn];
    } });
    assert(!dispatch_semaphore_wait(importing,dispatch_time(DISPATCH_TIME_NOW,5*NSEC_PER_SEC)));
    // Give an unsynchronized importer time to read the still-old CPU pixels.
    [NSThread sleepForTimeInterval:0.1];
    dispatch_semaphore_signal(release_read);
    assert(!dispatch_group_wait(group,dispatch_time(DISPATCH_TIME_NOW,5*NSEC_PER_SEC)));
    const unsigned char orange[4]={0,128,255,255};
    assert(!memcmp(t->_i.gpu,orange,4)); assert(!memcmp(s.cpu,orange,4)); assert(uploads==0);
    // A later CPU edit must still be imported once publication has finished.
    s.seed++; memset(s.cpu,71,4); [alias nvmtlSurfaceIn];
    assert(uploads==1); assert(!memcmp((argc>1?alias:t)->_i.gpu,s.cpu,4));
    // Publication after the commit's VRAM mark (WindowServer, an NVAccel whose page-off waits) runs on the thread that
    // releases the pending marks, so it never waits for the accelerator lock: busy, the commit's mark stands and nothing
    // is copied (a copy would block in the read stub and fail); free, the surface is marked again.
    Surface ws={.seed=9}; NVMTLTexture *w=[NVMTLTexture new]; w->_surf=&ws; w->_vramOn=YES; w->_i=(Image){.w=1,.h=1,.bpp=4};
    blockingDirties=0; tries=0; mainTries=0; tryAnswer=-1;
    assert([w nvmtlSurfaceOutDirtied:YES] && tries==1 && !blockingDirties && w->_surfSynced && w->_surfSeed==9);
    tryAnswer=0;
    assert([w nvmtlSurfaceOutDirtied:YES] && tries==2 && mainTries==2 && !blockingDirties);
    // the lock was taken: this thread goes on, and the re-mark is retried off it, again only as tries (a mark that
    // waited for the lock could land after a newer CPU write), until one takes; a page-off that slipped in between the
    // commit's marks must not leave its copy current
    tryAnswer=1;
    // exactly one more try on this thread; in all there may be more by now, the retry's first one runs off it at once
    assert([w nvmtlSurfaceOutDirtied:YES] && mainTries==3 && tries>=3);
    for (int i=0; i<200 && tries<6; i++) [NSThread sleepForTimeInterval:0.002];
    assert(tries>=6 && !blockingDirties);
    tryAnswer=0; const unsigned settledTries=tries;
    for (int i=0; i<200 && tries==settledTries; i++) [NSThread sleepForTimeInterval:0.002];
    [NSThread sleepForTimeInterval:0.02];
    const unsigned after=tries; [NSThread sleepForTimeInterval:0.02];
    assert(after>settledTries && tries==after && !blockingDirties);   // it took, and the retries stopped
    tryAnswer=-1; tries=2;
    // any other publication asks with the blocking mark, as before
    dirtyAnswer=0;
    assert([w nvmtlSurfaceOut] && tries==2 && blockingDirties==1);
    dirtyAnswer=1;
    // a surface another process is still drawing (PR #398): this process waits for that write before it imports, and
    // remembers the surface as client-written so every later import waits too; its own writes never wait
    Surface cs={.seed=3}; NVMTLTexture *c=[NVMTLTexture new]; c->_surf=&cs; c->_i=(Image){.w=1,.h=1,.bpp=4};
    busyWaits=0; busyForeign=0; compositorWaits=0; [c nvmtlSurfaceIn];
    assert(busyWaits==0 && !c->_clientWritten);
    // a surface no other application wrote may be one WindowServer is drawing: its kernel holds are waited for, once
    // per import, and not when the surface has not changed
    assert(compositorWaits==1);
    [c nvmtlSurfaceIn]; assert(compositorWaits==1);
    cs.seed++; busyForeign=1; [c nvmtlSurfaceIn];
    assert(busyWaits==1 && c->_clientWritten);
    busyForeign=0; cs.seed++; [c nvmtlSurfaceIn];
    assert(busyWaits==2);
    assert(compositorWaits==1);   // an application's surface is waited for through its busy tag, not as WindowServer's
    // the two planes of a private two-plane surface published together: one read-write lock moves the seed once, both
    // planes follow it, and neither is uploaded again at its next bind. WAS: the second plane's own lock left the first
    // plane one seed behind, and it was uploaded again (a GPU round trip on WindowServer's compositor thread per frame)
    { Surface ps={.seed=5,.planes=2}; NVMTLTexture *p0=[NVMTLTexture new], *p1=[NVMTLTexture new];
      p0->_surf=&ps; p1->_surf=&ps; p1->_plane=1; p0->_i=(Image){.w=1,.h=1,.bpp=4,.gpu={1,2,3,4}}; p1->_i=(Image){.w=1,.h=1,.bpp=4,.gpu={5,6,7,8}};
      p0->_surfSynced=YES; p1->_surfSynced=YES; p0->_surfSeed=5; p1->_surfSeed=5; p0->_surfGen=1; p1->_surfGen=1;
      planeReads=0; assert(nvmtl_surface_out_planes(@[p0,p1])==0 && planeReads==1 && planeReadN==2 && ps.seed==6);
      assert(!memcmp(ps.cpu,p0->_i.gpu,4) && !memcmp(ps.cpu1,p1->_i.gpu,4));   // each plane's pixels in its own bytes
      assert(p0->_surfSeed==6 && p1->_surfSeed==6);
      const unsigned before=uploads; busyForeign=0;
      [p0 nvmtlSurfaceIn]; [p1 nvmtlSurfaceIn]; assert(uploads==before);
      ps.seed++; [p0 nvmtlSurfaceIn]; assert(uploads==before+1);   // a write by anyone else still uploads
      assert(nvmtl_surface_out_planes(@[p0])==-1 && planeReads==1);   // one plane alone takes the plane-by-plane path
      // the planes' pages cannot be imported: each plane is read on its own under the one lock, and each answers for
      // itself. Plane 1's readback fails: plane 0 is published at the new seed, plane 1 keeps the seed it had, and the
      // answer names plane 1 alone (a command buffer fails for the plane it wrote, not for another's)
      p0->_surfSeed=7; p1->_surfSeed=7; planeReadAnswer=-1; failRead=&p1->_i;
      memset(p0->_i.gpu,9,4); memset(ps.cpu1,0x55,4);
      dispatch_semaphore_signal(release_read);          // plane 0's plain readback
      assert(nvmtl_surface_out_planes(@[p0,p1])==(1<<1) && planeReads==2 && ps.seed==8);
      assert(!dispatch_semaphore_wait(entered,dispatch_time(DISPATCH_TIME_NOW,NSEC_PER_SEC)));
      assert(p0->_surfSeed==8 && p1->_surfSeed==7 && ps.cpu[0]==9 && ps.cpu1[0]==0x55);
      // a submission that failed is not tried again plane by plane (a second read would block in the read stub and
      // fail): every plane is reported, and none takes the new seed
      failRead=NULL; planeReadAnswer=-2; p1->_surfSeed=8;
      assert(nvmtl_surface_out_planes(@[p0,p1])==((1<<0)|(1<<1)) && planeReads==3 && ps.seed==9);
      assert(p0->_surfSeed==8 && p1->_surfSeed==8);
      planeReadAnswer=0;
    }
    // two planes in their own VRAM (WindowServer's NV12 capture frames): every mark that takes moves the surface's one
    // seed, so the plane marked first was left one seed behind and was locked and uploaded again at its next use. The
    // other planes' tags now follow a mark that took and moved the seed by exactly one, where the planar self-test
    // measured that every mark does (seedMeasured).
    { Surface vs={.seed=20,.planes=2}; NVMTLTexture *v0=[NVMTLTexture new], *v1=[NVMTLTexture new];
      v0->_surf=&vs; v1->_surf=&vs; v1->_plane=1; v0->_vramOn=YES; v1->_vramOn=YES; v0->_i=(Image){.w=1,.h=1,.bpp=4}; v1->_i=v0->_i;
      markMoves=&vs; tryAnswer=0; busyForeign=0;
      assert([v0 nvmtlSurfaceOutDirtied:YES] && vs.seed==21 && [v1 nvmtlSurfaceOutDirtied:YES] && vs.seed==22);
      assert(nvmtl_vram_tag_current(&vs,0,22) && nvmtl_vram_tag_current(&vs,1,22));   // WAS: plane 0's tag stayed at 21
      unsigned before=uploads; [v0 nvmtlSurfaceIn]; [v1 nvmtlSurfaceIn]; assert(uploads==before);
      assert(v0->_surfSeed==22 && v1->_surfSeed==22);
      // the blocking mark of an application's own planes moves the seed the same way
      dirtyAnswer=0;
      assert([v1 nvmtlSurfaceOut] && vs.seed==23 && [v0 nvmtlSurfaceOut] && vs.seed==24);
      assert(nvmtl_vram_tag_current(&vs,0,24) && nvmtl_vram_tag_current(&vs,1,24));
      dirtyAnswer=1;
      // a tag that was not current before the mark does not follow: plane 1's VRAM is behind a CPU write
      vs.seed++;   // someone else's write: both tags are stale at 25
      assert([v0 nvmtlSurfaceOutDirtied:YES] && vs.seed==26);
      assert(nvmtl_vram_tag_current(&vs,0,26) && !nvmtl_vram_tag_current(&vs,1,26) && !nvmtl_vram_tag_current(&vs,1,25));
      before=uploads; [v1 nvmtlSurfaceIn]; assert(uploads==before+1);   // so it is uploaded, as before
      assert(nvmtl_vram_tag_current(&vs,1,26));
      // a mark that found the lock taken moved nothing; a seed that moved anyway is someone else's write
      markMoves=NULL; tryAnswer=1; const unsigned sets=tagSets;
      vs.seed++; plane_tag[1]=27;                      // plane 1 current at 27, plane 0 about to be marked
      assert([v0 nvmtlSurfaceOutDirtied:YES] && tagSets==sets && nvmtl_vram_tag_current(&vs,1,27));
      // a mark that took without moving the seed leaves the other tags as they are
      tryAnswer=0; assert([v0 nvmtlSurfaceOutDirtied:YES] && tagSets==sets && vs.seed==27);
      [NSThread sleepForTimeInterval:0.05];            // the retry of the busy mark above has given up or taken by now
      // a re-mark that found the lock taken and takes on its retry, off this thread, moves the seed there: the other
      // plane's tag follows that move too
      markMoves=&vs; tryAnswer=1; offAnswer=0; plane_tag[1]=27; unsigned retried=tagSets;
      assert([v0 nvmtlSurfaceOutDirtied:YES] && nvmtl_vram_tag_current(&vs,1,27));
      for (int i=0; i<500 && vs.seed==27; i++) [NSThread sleepForTimeInterval:0.002];
      for (int i=0; i<500 && !nvmtl_vram_tag_current(&vs,0,28); i++) [NSThread sleepForTimeInterval:0.002];
      [NSThread sleepForTimeInterval:0.02];
      assert(vs.seed==28 && nvmtl_vram_tag_current(&vs,0,28) && nvmtl_vram_tag_current(&vs,1,28) && tagSets==retried+1 && v0->_surfSeed==28);
      // someone else's write between that retry's mark and its bookkeeping (the third seed it reads): the marked
      // plane takes the seed it finds, as before, and the other plane's tag does not follow a seed it cannot vouch for
      // (plane 1's tag is current at the seed before each of these: only the rule keeps it from following)
      retried=tagSets; atomic_store(&offSeedCalls,0); bumpAtOffCall=3;
      assert([v0 nvmtlSurfaceOutDirtied:YES]);
      for (int i=0; i<500 && !nvmtl_vram_tag_current(&vs,0,30); i++) [NSThread sleepForTimeInterval:0.002];
      [NSThread sleepForTimeInterval:0.02];
      assert(vs.seed==30 && nvmtl_vram_tag_current(&vs,0,30) && nvmtl_vram_tag_current(&vs,1,28) && tagSets==retried);
      // ... or between the retry's mark and the seed it reads right behind it (the second): a move by two is not the
      // mark's own
      plane_tag[1]=30; retried=tagSets; atomic_store(&offSeedCalls,0); bumpAtOffCall=2;
      assert([v0 nvmtlSurfaceOutDirtied:YES]);
      for (int i=0; i<500 && !nvmtl_vram_tag_current(&vs,0,32); i++) [NSThread sleepForTimeInterval:0.002];
      [NSThread sleepForTimeInterval:0.02];
      assert(vs.seed==32 && nvmtl_vram_tag_current(&vs,0,32) && nvmtl_vram_tag_current(&vs,1,30) && tagSets==retried);
      // a retry whose mark takes and moves nothing, and someone else's write right behind it: the seed moved by one,
      // but not around the mark
      plane_tag[1]=32; retried=tagSets; atomic_store(&offSeedCalls,0); bumpAtOffCall=3; markMoves=NULL;
      assert([v0 nvmtlSurfaceOutDirtied:YES]);
      for (int i=0; i<500 && !nvmtl_vram_tag_current(&vs,0,33); i++) [NSThread sleepForTimeInterval:0.002];
      [NSThread sleepForTimeInterval:0.02];
      assert(vs.seed==33 && nvmtl_vram_tag_current(&vs,0,33) && nvmtl_vram_tag_current(&vs,1,32) && tagSets==retried);
      bumpAtOffCall=0; offAnswer=NO_ANSWER; markMoves=NULL; tryAnswer=0; vs.seed=27; plane_tag[1]=27; gpu_seed=27;
      // nothing follows in a process whose self-test has not seen every mark move the seed by exactly one: a move by
      // one around a mark could then be someone else's write, and plane 1's tag would call older VRAM current
      seedMeasured=0; markMoves=&vs; tryAnswer=0; plane_tag[1]=vs.seed; const unsigned unmeasured=tagSets;
      assert([v0 nvmtlSurfaceOutDirtied:YES] && vs.seed==28 && tagSets==unmeasured);
      assert(nvmtl_vram_tag_current(&vs,0,28) && nvmtl_vram_tag_current(&vs,1,27));
      seedMeasured=1;
      assert([v0 nvmtlSurfaceOutDirtied:YES] && vs.seed==29 && nvmtl_vram_tag_current(&vs,1,27));   // 27 was not current at 28
      plane_tag[1]=29;
      assert([v0 nvmtlSurfaceOutDirtied:YES] && vs.seed==30 && tagSets==unmeasured+1 && nvmtl_vram_tag_current(&vs,1,30));
      tryAnswer=-1; markMoves=NULL;
    }
    // a shader-writable Managed wrapper drawn through a tiled image of its own (_i) over the linear VRAM import
    // (_surfaceLinear): its publication copies the tiled image into the VRAM before the mark, another writer's
    // publication (a new VRAM generation at the same seed) is copied back on the GPU, a CPU write is uploaded into the
    // tiled image and leaves the VRAM's tag alone, and a copy that fails neither marks nor calls the tiled image current
    { Surface ms={.seed=17}; NVMTLTexture *m=[NVMTLTexture new]; m->_surf=&ms; m->_vramOn=YES;
      m->_i=(Image){.w=1,.h=1,.bpp=4,.gpu={3,17,41,255}}; m->_surfaceLinear=(Image){.w=1,.h=1,.bpp=4,.img=(void *)1};
      dirtyAnswer=0; gpuCopies=0; busyForeign=0; const unsigned marks=blockingDirties;
      assert([m nvmtlSurfaceOut] && gpuCopies==1 && !memcmp(m->_surfaceLinear.gpu,m->_i.gpu,4) && blockingDirties==marks+1);
      unsigned before=uploads; [m nvmtlSurfaceIn]; assert(gpuCopies==1 && uploads==before);   // published: current
      memset(m->_surfaceLinear.gpu,82,4); vram_generation++;
      [m nvmtlSurfaceIn]; assert(gpuCopies==2 && m->_i.gpu[0]==82 && m->_surfGen==vram_generation && uploads==before);
      ms.seed++; memset(ms.cpu,93,4); [m nvmtlSurfaceIn];
      assert(uploads==before+1 && m->_i.gpu[0]==93 && !nvmtl_vram_tag_current(&ms,0,ms.seed));
      copyFailure=1; const unsigned marks2=blockingDirties;
      assert(![m nvmtlSurfaceOut] && blockingDirties==marks2);
      m->_surfSynced=NO; gpu_seed=ms.seed; [m nvmtlSurfaceIn]; assert(!m->_surfSynced);
      copyFailure=0; [m nvmtlSurfaceIn]; assert(m->_surfSynced && m->_i.gpu[0]==82);
      dirtyAnswer=1; vram_generation=1;
    }
    puts("PASS: overlapping publication preserves GPU pixels; later CPU edits import; a publication after the commit's "
         "VRAM mark never waits for the accelerator lock and retries a busy re-mark off its thread; another process's "
         "write is waited for before import, a surface no application wrote waits for WindowServer's kernel holds; the "
         "planes of a private two-plane surface are copied out under one lock, each answering for itself; the VRAM tags "
         "of a surface's other planes follow a mark only where the self-test measured the seed; a Managed wrapper's tiled "
         "image is published through its VRAM import by GPU copies");
} return 0; }
'''
# The copy-out of several planes in one submission (nvmtl_vk_image_read_host_planes, plugin/nvmtl_vk.c) needs Vulkan and
# is not built here; the fixture above stands in for it. Its text is held to this much: every plane handed in is
# prepared, recorded and released (each of its loops runs over all n), and there is one submission after the recording.
vk = (root / 'plugin/nvmtl_vk.c').read_text()
i0 = vk.index('\nint nvmtl_vk_image_read_host_planes(')
planes = vk[i0:vk.index('\n}\n', i0)]
record = planes.index('    for (int i = 0; i < n; i++) {\n')
recorded = planes[record:planes.index('\n    }\n', record)]
submit = planes.index('nvmtl_vk_submit_wait_at(')
assert planes.count('for (int i = 0; i < n && !rc; i++) {') == 1 and planes.count('for (int i = 0; i < n; i++)') == 2, \
    'a loop of the grouped readback no longer runs over every plane'
assert 'pvkCmdCopyImageToBuffer(c.cb, img[i]->img, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, hp[i]->buf, 1, &bic);' in recorded, \
    'the grouped readback no longer records a copy for each plane'
assert planes.count('nvmtl_vk_submit_wait_at(') == 1 and record < submit, 'the grouped readback is no longer one submission after the recording'
assert 'for (int i = 0; i < n; i++) nvmtl_host_pages_done(cache[i], hp[i]);' in planes[submit:], 'a plane\'s pages are not released'

with tempfile.TemporaryDirectory() as directory:
    path = pathlib.Path(directory)
    (path / 'test.m').write_text(fixture + methods + checks)
    subprocess.run(['clang', '-fobjc-arc', '-I', str(root / 'plugin'), '-framework', 'Foundation',
                    str(path / 'test.m'), '-o', str(path / 'test')], check=True)
    subprocess.run([str(path / 'test')], check=True)
    subprocess.run([str(path / 'test'), 'shared'], check=True)
    subprocess.run([str(path / 'test'), 'off'], check=True, env={**os.environ, 'NVMTL_SURFOUT_PLANES': '0', 'NVMTL_PLANE_FOLLOW': '0'})
