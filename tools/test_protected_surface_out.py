#!/usr/bin/env python3
"""test_protected_surface_out.py: a protected IOSurface (DRM video), which gives the process no CPU mapping, is published
on the GPU side when its pixels live in the surface's own VRAM, instead of failing the command buffer; drawn through a
private image, or with its VRAM mark refused, it still fails, and says that the surface is a protected one.

Compiles the production stretch of plugin/NVMTLObjects.m from -[NVMTLTexture nvmtlSurfaceCopyable] to -width (the
surface-in and surface-out methods; the 4:2:2 reader is left out, as tools/test_surface_publication.py does) against
stand-in IOSurface and kernel calls, with the real nvmtlSurfaceCopyable. The idea is from pull request 524 of the
original project (P3); this test is written for this tree.

  python3 tools/test_protected_surface_out.py [--baseline [REF]]
      --baseline REF   the sources of that revision (default HEAD); on 3.0.4 the GPU-side arms fail

Builds the way tools/test_surface_publication.py does (clang -fobjc-arc -framework Foundation): on a Mac, or on Linux
with the GNUstep wrapper first on PATH."""
import argparse
import pathlib
import subprocess
import tempfile

root = pathlib.Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument('--baseline', nargs='?', const='HEAD', metavar='REF')
args = parser.parse_args()


def source(path):
    if args.baseline:
        return subprocess.check_output(['git', 'show', f'{args.baseline}:{path}'], cwd=root, text=True)
    return (root / path).read_text()


objects = source('plugin/NVMTLObjects.m')
start = objects.index('- (BOOL)nvmtlSurfaceCopyable {')
methods = objects[start:objects.index('- (NSUInteger)width ', start)]
y0 = methods.index('- (void)nvmtlY422SurfaceIn {'); y1 = methods.index('\n}\n', y0) + 3
assert methods[y1:].startswith('- (void)nvmtlSurfaceIn {'), 'the 4:2:2 reader is no longer right before nvmtlSurfaceIn'
methods = methods[:y0] + methods[y1:]

fixture = r'''
#import <Foundation/Foundation.h>
#include <assert.h>
#include <os/lock.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <dispatch/dispatch.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#import <objc/runtime.h>
// noCpu: a protected surface, which the system maps nowhere in this process
typedef struct __IOSurface { unsigned seed; unsigned char cpu[4]; int noCpu; } Surface;
typedef int IOReturn;
enum { kIOSurfaceLockReadOnly = 1 };
typedef struct { unsigned w,h,bpp; unsigned char gpu[4]; void *img; } Image;
static unsigned locks, reads, gpuCopies, blockingDirties, tries;
static int dirtyAnswer=1, tryAnswer=-1;
static unsigned vram_generation=1, vram_tag;
static unsigned long long g_surfin_skip, g_surfin_copy;
// every log line is kept: the protected arms check what the driver says
static char lastLog[512]; static unsigned protectedLogs;
static void nvlog(const char *f, ...) { va_list a; va_start(a, f); vsnprintf(lastLog, sizeof lastLog, f, a); va_end(a);
    if (strstr(lastLog, "protected")) protectedLogs++; }
static uint32_t IOSurfaceGetSeed(IOSurfaceRef s) { return s->seed; }
static unsigned IOSurfaceGetID(IOSurfaceRef s) { return 7; }
static IOReturn IOSurfaceLock(IOSurfaceRef s, unsigned opts, uint32_t *seed) { locks++; if (!opts) s->seed++; if (seed) *seed=s->seed; return 0; }
static IOReturn IOSurfaceUnlock(IOSurfaceRef s, unsigned opts, uint32_t *seed) { if (seed) *seed=s->seed; return 0; }
static void *IOSurfaceGetBaseAddress(IOSurfaceRef s) { return s->noCpu ? NULL : s->cpu; }
static void *IOSurfaceGetBaseAddressOfPlane(IOSurfaceRef s, uint32_t p) { return s->noCpu ? NULL : s->cpu; }
static size_t IOSurfaceGetBytesPerRowOfPlane(IOSurfaceRef s, uint32_t p) { return 4; }
static size_t IOSurfaceGetWidthOfPlane(IOSurfaceRef s, uint32_t p) { return 1; }
static size_t IOSurfaceGetHeightOfPlane(IOSurfaceRef s, uint32_t p) { return 1; }
static size_t IOSurfaceGetBytesPerElementOfPlane(IOSurfaceRef s, uint32_t p) { return 4; }
static size_t IOSurfaceGetAllocSize(IOSurfaceRef s) { return sizeof s->cpu; }
static size_t IOSurfaceGetPlaneCount(IOSurfaceRef s) { return 1; }
static CFTypeRef IOSurfaceCopyValue(IOSurfaceRef s, CFStringRef k) { (void)s; (void)k; return NULL; }
static void IOSurfaceSetValue(IOSurfaceRef s, CFStringRef k, CFTypeRef v) { (void)s; (void)k; (void)v; }
static int nvmtl_vk_image_write_host(void *q, Image *img, void *base, size_t row, size_t bytes, void *cache) { return -1; }
static int nvmtl_vk_image_read_host(void *q, Image *img, void *base, size_t row, size_t bytes, void *cache) { return -1; }
typedef Image nvk_image; typedef void *nvk_buffer;
static int nvmtl_vk_image_read_host_planes(void *q, int n, nvk_image *const *img, void *const *base, const size_t *row,
                                           const size_t *bytes, nvk_buffer *const *cache) { return -1; }
static uint32_t nvmtl_vram_gen(IOSurfaceRef s, NSUInteger p) { return vram_generation; }
static os_unfair_lock g_vram_tag_lock = OS_UNFAIR_LOCK_INIT;
static int nvmtl_vram_tag_current(IOSurfaceRef s, NSUInteger p, unsigned seed) { return vram_tag == seed; }
static void nvmtl_vram_tag_write(IOSurfaceRef s, NSUInteger p, unsigned seed, unsigned gen) { vram_tag=seed; }
static void nvmtl_vram_tag_set(IOSurfaceRef s, NSUInteger p, unsigned seed) { vram_tag=seed; }
static unsigned nvmtl_vram_tag_bump(IOSurfaceRef s, NSUInteger p, unsigned seed) { vram_tag=seed; return ++vram_generation; }
static int nvmtl_vk_plane_mark_moves_seed(void) { return 1; }
// the kernel's VRAM mark: a mark that takes moves the seed, as the kernel's does
static int nvmtl_vk_surface_dirty(unsigned s, unsigned p) { blockingDirties++; return dirtyAnswer; }
static int nvmtl_vk_surface_dirty_try(unsigned s, unsigned p) { tries++; return tryAnswer; }
static int nvmtl_noseed(void) { return 0; }
static void nvmtl_vk_host_pages_key(uint32_t surface, uint64_t cookie) { (void)surface; (void)cookie; }
static int nvmtl_busy_foreign(IOSurfaceRef s, NSUInteger p) { return 0; }
static void nvmtl_busy_wait(IOSurfaceRef s, NSUInteger p) {}
static void nvmtl_vk_fence_wait_compositor(uint32_t sid) {}
static void nvmtl_frame_dump(const char *tag, void *base, size_t row, unsigned w, unsigned h) {}
static BOOL nvmtl_ws_kernel_waits(void){return NO;}
static void nvmtl_ws_mark(uint32_t sid,BOOL pending){}
static void nvtrace(const char *f,...){(void)f;}
static int nvmtl_vk_copy_image(void *q, Image *src, uint32_t sx, uint32_t sy, Image *dst, uint32_t dx, uint32_t dy, uint32_t w, uint32_t h) {
    gpuCopies++; memcpy(dst->gpu, src->gpu, 4); return 0;
}
static int nvmtl_vk_image_write(void *q, Image *i, void *base, size_t row) { memcpy(i->gpu,base,4); return 0; }
static int nvmtl_vk_image_read(void *q, Image *i, void *base, size_t row) { if (!base) return -1; reads++; memcpy(base,i->gpu,4); return 0; }
@interface NVMTLTexture : NSObject {
@public NVMTLTexture *_parent; IOSurfaceRef _surf; BOOL _vramOn, _surfSynced, _clientWritten; void *_hostPages;
    int _zeroLost; BOOL _zeroTried;
    uint32_t _surfSeed, _surfGen; NSUInteger _plane; Image _i, _surfaceLinear; void *_q;
    IOSurfaceRef _y422Surf;
}
- (void)nvmtlY422SurfaceIn;
- (BOOL)nvmtlSurfaceCopyable;
- (void)nvmtlSurfaceIn;
- (BOOL)nvmtlSurfaceOut;
- (BOOL)nvmtlSurfaceOutDirtied:(BOOL)dirtied;
@end
@class NVMTLTexture;
static int nvmtl_surfin_zero_try(NVMTLTexture *r, const void *b, size_t row) { return 0; }
// a bind passing a copy-out (NVMTL_SURFIN_SKIP_OWN_OUT, nvmtl_surf_private.h) is tools/test_surf_private's: off here, so
// every copy-out holds its monitor as these arms expect
static BOOL nvmtl_surface_out_pass_begin(NVMTLTexture *r) { (void)r; return NO; }
static void nvmtl_surface_out_pass_end(NVMTLTexture *r, BOOL passable) { (void)r; (void)passable; }
@implementation NVMTLTexture
- (void)nvmtlY422SurfaceIn { assert(!"a non-4:2:2 texture took the 4:2:2 path"); }
'''
checks = r'''
@end
static NVMTLTexture *texture(Surface *s, BOOL vram) {
    NVMTLTexture *t=[NVMTLTexture new]; t->_surf=s; t->_vramOn=vram; t->_i=(Image){.w=1,.h=1,.bpp=4,.gpu={9,8,7,6}}; return t;
}
#define ARM(name, cond) do { const int ok_ = !!(cond); printf("%s  %s\n", ok_ ? "ok  " : "FAIL", name); if (!ok_) fails++; } while (0)
int main(void) { @autoreleasepool {
    int fails=0;
    // control: a surface with a CPU mapping, drawn through a private image, is copied out under its lock, as before
    { Surface s={.seed=1}; NVMTLTexture *t=texture(&s,NO);
      ARM("control: a mapped private surface is copied out under its lock", [t nvmtlSurfaceOut] && locks==1 && reads==1 && !memcmp(s.cpu,t->_i.gpu,4)); }
    // a protected surface in its own VRAM: the blocking mark takes, nothing is locked or read on the CPU
    { Surface s={.seed=3,.noCpu=1}; NVMTLTexture *t=texture(&s,YES);
      dirtyAnswer=0; const unsigned l=locks, b=blockingDirties, g=vram_generation;
      const BOOL out=[t nvmtlSurfaceOut];
      ARM("a protected surface in VRAM is published by its kernel mark (WAS: the command buffer failed)",
          out && blockingDirties==b+1 && locks==l && t->_surfSynced && t->_surfSeed==3 && vram_generation==g+1);
      ARM("... and the log says it was a protected surface published on the GPU side",
          protectedLogs>=1 && strstr(lastLog,"protected") && strstr(lastLog,"GPU side"));
      // after the commit's mark (WindowServer): a try that takes, and one that finds the lock busy, both stand
      tryAnswer=0; ARM("... after the commit's mark too (a try that takes)", [t nvmtlSurfaceOutDirtied:YES] && locks==l);
      tryAnswer=-1; ARM("... and when the try is refused, the commit's mark stands", [t nvmtlSurfaceOutDirtied:YES] && locks==l);
      // the blocking mark refused: there is no CPU copy to fall back to, so the publication fails, without a lock
      dirtyAnswer=1; const unsigned before=protectedLogs;
      ARM("a protected surface whose VRAM mark is refused still fails, without trying a CPU copy",
          ![t nvmtlSurfaceOut] && locks==l && protectedLogs==before+1 && strstr(lastLog,"refused"));
      dirtyAnswer=0; }
    // a tiled image over a protected surface's VRAM: copied into the VRAM on the GPU before the mark
    { Surface s={.seed=5,.noCpu=1}; NVMTLTexture *t=texture(&s,YES); t->_surfaceLinear=(Image){.w=1,.h=1,.bpp=4,.img=(void *)1};
      const unsigned c=gpuCopies, l=locks;
      ARM("a protected surface's tiled image is copied into its VRAM on the GPU, then marked",
          [t nvmtlSurfaceOut] && gpuCopies==c+1 && !memcmp(t->_surfaceLinear.gpu,t->_i.gpu,4) && locks==l); }
    // a protected surface drawn through a private image: only the CPU could publish it, and it has no mapping
    { Surface s={.seed=7,.noCpu=1}; NVMTLTexture *t=texture(&s,NO);
      const unsigned l=locks, r=reads, before=protectedLogs;
      const BOOL out=[t nvmtlSurfaceOut];
      ARM("a protected surface drawn through a private image still fails (nothing could publish it)", !out && reads==r);
      ARM("... without locking it, and the log names it a protected surface",
          locks==l && protectedLogs==before+1 && strstr(lastLog,"private image")); }
    printf("%s\n", fails ? "FAIL" : "PASS: a protected surface in VRAM is published by its kernel mark; drawn through a private image or with its mark refused it fails and says why");
    return fails ? 1 : 0;
} }
'''
with tempfile.TemporaryDirectory() as directory:
    path = pathlib.Path(directory)
    (path / 'test.m').write_text(fixture + methods + checks)
    subprocess.run(['clang', '-fobjc-arc', '-I', str(root / 'plugin'), '-framework', 'Foundation',
                    str(path / 'test.m'), '-o', str(path / 'test')], check=True)
    raise SystemExit(subprocess.run([str(path / 'test')]).returncode)
