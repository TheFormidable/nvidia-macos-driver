#!/usr/bin/env python3
"""test_nocopy_once.py: a newBufferWithBytesNoCopy range that the GPU cannot import, made by WindowServer over pages
it has mapped read only and privately, is copied once, when the buffer is made: it keeps no host pointer, so nothing
copies into the caller's pages (which faults) or out of them again; -contents still returns the caller's pointer and
the deallocator still gets it. Writable pages, pages backed by a file, a range that runs into writable pages, another
process, and NVMTL_WS_NOCOPY_ONCE=0 keep the copy that is refreshed at every submission, as in 3.0.4.

Two parts. The rules of plugin/nvmtl_nocopy_once.h, against made-up mapping regions (every share mode, holes, a range of
several regions). Then the production -[NVMTLDevice newBufferWithBytesNoCopy:length:options:deallocator:] and the query
it feeds the header (plugin/NVMTLDevice_contract.m), -contents and -setPurgeableState: (plugin/NVMTLObjects.m) and the
two copies of a submission (nvmtl_buffer_sync_in and nvmtl_buffer_sync_out), over real pages of this process: on a Mac
the query is mach_vm_region itself, elsewhere a stand-in reads /proc/self/maps. The idea is from pull request 524 of
the original project (its 4K 240 Hz WindowServer commit), limited here to the pages the driver can see nothing writes.

  python3 tools/test_nocopy_once.py [--baseline [REF]]
      --baseline REF   the sources of that revision (default HEAD); on 3.0.4 the copy-once arms fail (the copy back
                       into read-only pages is caught as a fault)

Builds the way tools/test_surface_publication.py does (clang -fobjc-arc -framework Foundation): on a Mac, or on Linux
with the GNUstep wrapper first on PATH."""
import argparse
import os
import pathlib
import subprocess
import tempfile

root = pathlib.Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument('--baseline', nargs='?', const='HEAD', metavar='REF')
args = parser.parse_args()


def source(path):
    if args.baseline:
        r = subprocess.run(['git', 'show', f'{args.baseline}:{path}'], cwd=root, capture_output=True, text=True)
        return r.stdout if r.returncode == 0 else None
    p = root / path
    return p.read_text() if p.exists() else None


def method(text, signature, end):
    start = text.index(signature)
    return text[start:text.index(end, start)]


contract = source('plugin/NVMTLDevice_contract.m')
objects = source('plugin/NVMTLObjects.m')
policy = source('plugin/nvmtl_nocopy_once.h')
vkh = source('plugin/nvmtl_vk.h')
buffer_type = vkh[vkh.index('typedef struct { void *buf;'):vkh.index('} nvk_buffer;') + len('} nvk_buffer;')]
nocopy = method(contract, '- (id)newBufferWithBytesNoCopy:', '- (id)newBufferWithDescriptor:')
# what the contract defines ahead of its methods for the copy once (nothing in 3.0.4)
pre = contract[contract.index('#include "nvmtl_nocopy_once.h"'):contract.index('@implementation NVMTLDevice (Contract)')] \
    if '#include "nvmtl_nocopy_once.h"' in contract else 'static BOOL nvmtl_nocopy_once(const void *p, NSUInteger n) { return NO; }\n'
pre = pre.replace('#include "nvmtl_nocopy_once.h"', '')
impl = objects[objects.index('@implementation NVMTLBuffer'):]
purge = method(impl, '- (MTLPurgeableState)setPurgeableState:', '- (void)addDebugMarker:')
contents = method(impl, '- (void *)contents {', '- (id<MTLHeap>)heap {')
s0 = objects.index('static void nvmtl_buffer_sync_in(NVMTLBuffer *b)\n{')
s1 = objects.index('\n}\n', objects.index('static void nvmtl_buffer_sync_out(NVMTLBuffer *b)\n{', s0)) + 3
sync = objects[s0:s1]
# the deallocator's arguments are taken in -dealloc (tools/test_host_buffer_release.py runs it); this test hands the
# wrapper the same arguments, so it holds -dealloc to the expressions it copies
assert 'void *hostPtr = _hostPtr ?: host->map;' in impl and 'NSUInteger hostLen = (NSUInteger)(_hostLen ?: host->size);' in impl, \
    '-dealloc no longer takes the deallocator\'s arguments the way this test copies'

policy_test = r'''
#include <assert.h>
#include <stdio.h>
#include "nvmtl_nocopy_once.h"
typedef struct { nvmtl_nco_region r[8]; int n; } map_t;
static int query(void *c, uint64_t a, nvmtl_nco_region *out) {
    map_t *m = c;
    for (int i = 0; i < m->n; i++) if (m->r[i].start + m->r[i].size > a) { *out = m->r[i]; return 0; }
    return -1;
}
static int fails;
#define ARM(name, cond) do { const int ok_ = !!(cond); printf("%s  %s\n", ok_ ? "ok  " : "FAIL", name); if (!ok_) fails++; } while (0)
int main(void) {
    const nvmtl_nco_region ro = { 0x1000, 0x2000, 1, NVMTL_NCO_SM_PRIVATE, 0 };
    ARM("a private read-only region qualifies", nvmtl_nco_region_sealed(&ro));
    nvmtl_nco_region r = ro; r.share_mode = NVMTL_NCO_SM_COW;
    ARM("a copy-on-write read-only region qualifies", nvmtl_nco_region_sealed(&r));
    r = ro; r.protection = 3; ARM("a writable region does not", !nvmtl_nco_region_sealed(&r));
    r = ro; r.external_pager = 1; ARM("a region backed by a file or device pager does not", !nvmtl_nco_region_sealed(&r));
    int others = 1;
    for (int m = 0; m <= 9; m++) { r = ro; r.share_mode = m;
        if (m != NVMTL_NCO_SM_PRIVATE && m != NVMTL_NCO_SM_COW) others &= !nvmtl_nco_region_sealed(&r); }
    ARM("shared, aliased, empty, large-page and unknown share modes do not", others);
    map_t one = { { ro }, 1 };
    ARM("a range inside one such region qualifies", nvmtl_nco_range_sealed(0x1010, 0x1000, query, &one));
    ARM("a range that runs past the last region does not", !nvmtl_nco_range_sealed(0x2f00, 0x200, query, &one));
    ARM("a range below every region (a hole) does not", !nvmtl_nco_range_sealed(0x0800, 0x100, query, &one));
    map_t two = { { ro, { 0x3000, 0x1000, 1, NVMTL_NCO_SM_COW, 0 } }, 2 };
    ARM("a range over two adjacent qualifying regions qualifies", nvmtl_nco_range_sealed(0x2f00, 0x200, query, &two));
    map_t gap = { { ro, { 0x4000, 0x1000, 1, NVMTL_NCO_SM_PRIVATE, 0 } }, 2 };
    ARM("a hole between two regions does not", !nvmtl_nco_range_sealed(0x2f00, 0x1200, query, &gap));
    map_t mixed = { { ro, { 0x3000, 0x1000, 3, NVMTL_NCO_SM_PRIVATE, 0 } }, 2 };
    ARM("a range that runs into a writable region does not", !nvmtl_nco_range_sealed(0x2f00, 0x200, query, &mixed));
    ARM("an empty range, an overflowing one and no query do not",
        !nvmtl_nco_range_sealed(0x1010, 0, query, &one) && !nvmtl_nco_range_sealed(~0ull - 4, 16, query, &one) &&
        !nvmtl_nco_range_sealed(0x1010, 16, 0, &one));
    map_t many = { { { 0 } }, 8 };
    for (int i = 0; i < 8; i++) many.r[i] = (nvmtl_nco_region){ 0x1000 + (uint64_t)i * 0x1000, 0x1000, 1, NVMTL_NCO_SM_PRIVATE, 0 };
    ARM("eight regions in a row qualify", nvmtl_nco_range_sealed(0x1000, 0x8000, query, &many));
    ARM("WindowServer copies once; NVMTL_WS_NOCOPY_ONCE=0 and every other process do not",
        nvmtl_nco_applies("WindowServer", NULL) && nvmtl_nco_applies("WindowServer", "1") && !nvmtl_nco_applies("WindowServer", "0") &&
        !nvmtl_nco_applies("Safari", NULL) && !nvmtl_nco_applies(NULL, NULL) && !nvmtl_nco_applies("WindowServerX", NULL));
    return fails ? 1 : 0;
}
'''

fixture = r'''
#import <Foundation/Foundation.h>
#include <assert.h>
#include <fcntl.h>
#include <setjmp.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
#ifdef __APPLE__
#include <mach/mach.h>
#include <mach/mach_vm.h>
#else
/* mach_vm_region, answered from /proc/self/maps: protection from the r/w/x columns, a 's' mapping shared, an inode
   (a file) an external pager. The kernel's answer on a Mac is the real one; this is only its shape. */
typedef int kern_return_t; typedef unsigned mach_port_t; typedef unsigned mach_msg_type_number_t; typedef uint64_t mach_vm_address_t, mach_vm_size_t;
typedef int *vm_region_info_t; typedef int vm_prot_t;
enum { KERN_SUCCESS = 0, MACH_PORT_NULL = 0, VM_REGION_BASIC_INFO_64 = 9, VM_REGION_EXTENDED_INFO = 13, VM_PROT_WRITE = 2 };
enum { SM_COW = 1, SM_PRIVATE = 2, SM_EMPTY = 3, SM_SHARED = 4, SM_TRUESHARED = 5, SM_PRIVATE_ALIASED = 6, SM_SHARED_ALIASED = 7 };
typedef struct { vm_prot_t protection; unsigned user_tag, pages_resident, pages_shared_now_private, pages_swapped_out, pages_dirtied, ref_count;
                 unsigned short shadow_depth; unsigned char external_pager, share_mode; unsigned pages_reusable; } vm_region_extended_info_data_t;
#define VM_REGION_EXTENDED_INFO_COUNT ((mach_msg_type_number_t)(sizeof(vm_region_extended_info_data_t) / sizeof(int)))
typedef struct { vm_prot_t protection, max_protection; unsigned inheritance; int shared, reserved; uint64_t offset;
                 int behavior; unsigned short user_wired_count; } vm_region_basic_info_data_64_t;
#define VM_REGION_BASIC_INFO_COUNT_64 ((mach_msg_type_number_t)(sizeof(vm_region_basic_info_data_64_t) / sizeof(int)))
static mach_port_t mach_task_self(void) { return 1; }
static kern_return_t mach_port_deallocate(mach_port_t t, mach_port_t p) { return 0; }
static unsigned basicAsked, extendedAsked;
static kern_return_t mach_vm_region(mach_port_t task, mach_vm_address_t *addr, mach_vm_size_t *size, int flavor, vm_region_info_t info,
                                    mach_msg_type_number_t *count, mach_port_t *object) {
    FILE *f = fopen("/proc/self/maps", "r"); if (!f) return 1;
    char line[512]; kern_return_t kr = 1;
    while (fgets(line, sizeof line, f)) {
        unsigned long lo, hi, off, inode; char perms[8], dev[16];
        if (sscanf(line, "%lx-%lx %7s %lx %15s %lu", &lo, &hi, perms, &off, dev, &inode) != 6) continue;
        if (hi <= *addr) continue;
        const int prot = (perms[0] == 'r' ? 1 : 0) | (perms[1] == 'w' ? 2 : 0) | (perms[2] == 'x' ? 4 : 0);
        if (flavor == VM_REGION_BASIC_INFO_64) {
            vm_region_basic_info_data_64_t *x = (vm_region_basic_info_data_64_t *)info; memset(x, 0, sizeof *x);
            x->protection = prot; x->max_protection = 7; x->shared = perms[3] == 's'; basicAsked++;
        } else {
            vm_region_extended_info_data_t *x = (vm_region_extended_info_data_t *)info; memset(x, 0, sizeof *x);
            x->protection = prot; x->share_mode = perms[3] == 's' ? SM_SHARED : SM_PRIVATE; x->external_pager = inode != 0;
            extendedAsked++;
        }
        *addr = lo; *size = hi - lo; *object = MACH_PORT_NULL; kr = KERN_SUCCESS; break;
    }
    fclose(f); return kr;
}
#endif
#include "nvmtl_nocopy_once.h"
typedef NSUInteger MTLStorageMode, MTLResourceOptions, MTLPurgeableState;
enum { MTLStorageModeShared = 0, MTLStorageModeManaged = 1, MTLStorageModePrivate = 2, MTLStorageModeMemoryless = 3 };
enum { MTLResourceStorageModeShift = 4, MTLResourceHazardTrackingModeShift = 8 };
enum { MTLPurgeableStateKeepCurrent = 1, MTLPurgeableStateNonVolatile = 2, MTLPurgeableStateVolatile = 3, MTLPurgeableStateEmpty = 4 };
@@BUFFER_TYPE@@
static const char *test_progname = "WindowServer";
#define getprogname() test_progname
static void nvlog(const char *f, ...) { (void)f; }
static unsigned syncIn, syncOut, purged;
enum { NVP_SYNCIN, NVP_SYNCOUT };
static uint64_t nvmtl_perf_now(void) { return 0; }
static void nvmtl_perf_note(int kind, uint64_t t, size_t n) { if (kind == NVP_SYNCIN) syncIn++; else syncOut++; }
static int nvmtl_vk_buffer_import_host(void *p, size_t n, nvk_buffer *out) { memset(out, 0, sizeof *out); return -1; }   // never importable here
static int nvmtl_vk_buffer_create(size_t n, int host, nvk_buffer *out) {
    memset(out, 0, sizeof *out); out->size = n; out->buf = (void *)1; if (host) out->map = calloc(1, n); return host && !out->map; }
static void nvmtl_vk_buffer_destroy(nvk_buffer *b) { free(b->map); memset(b, 0, sizeof *b); }
static size_t nvmtl_vk_host_import_align(void) { return 4096; }
static void nvmtl_alloc_failed(const char *w) {}
typedef struct { int unused; } nvmtl_swc;
static int nvmtl_swc_init(nvmtl_swc *s, size_t n) { return -1; }
static void nvmtl_swc_free(nvmtl_swc *s) {}
@interface NVMTLHeap : NSObject @end
@implementation NVMTLHeap @end
@interface NVMTLBuffer : NSObject { @public nvk_buffer _b, _shadow; MTLStorageMode _storage; MTLResourceOptions _ropt;
    void *_hostPtr; size_t _hostLen; id _hostDealloc; void *_swc; NVMTLHeap *_heap, *_subHeap; uint32_t _purge;
    void *_callerPages; int _zshadow, _syncOut; }
- (void *)contents;
- (MTLPurgeableState)setPurgeableState:(MTLPurgeableState)s;
- (void)didModifyRange:(NSRange)r;
@end
static void nvmtl_buf_settle(NVMTLBuffer *b) {}
static MTLPurgeableState nvmtl_purge(uint32_t *p, MTLPurgeableState s) { purged++; return MTLPurgeableStateNonVolatile; }
static void nvmtl_purge_track(NVMTLBuffer *b, MTLPurgeableState s) {}
static void nvmtl_swc_sync_in(NVMTLBuffer *b) { abort(); }
static void nvmtl_swc_sync_out(NVMTLBuffer *b) { abort(); }
static void *nvmtl_device_queue(void) { return NULL; }
static int nvmtl_vk_copy_buffer(void *q, nvk_buffer *a, size_t ao, nvk_buffer *b, size_t bo, size_t n) { abort(); }
@@SYNC@@
@implementation NVMTLBuffer
@@CONTENTS@@
@@PURGE@@
- (void)didModifyRange:(NSRange)r { nvmtl_buffer_sync_in(self); }
@end
@interface NVMTLDevice : NSObject
- (id)newBufferWithLength:(NSUInteger)n options:(NSUInteger)o;
@end
@@PRE@@
@implementation NVMTLDevice
- (id)newBufferWithLength:(NSUInteger)n options:(NSUInteger)o { abort(); }
@@NOCOPY@@
@end
static sigjmp_buf caught; static volatile sig_atomic_t armed;
static void fault(int sig) { if (armed) { armed = 0; siglongjmp(caught, 1); } signal(sig, SIG_DFL); raise(sig); }
static int fails;
#define ARM(name, cond) do { const int ok_ = !!(cond); printf("%s  %s\n", ok_ ? "ok  " : "FAIL", name); if (!ok_) fails++; } while (0)
static void *gotPtr; static NSUInteger gotLen;
// Pages of this process filled with a pattern and then given the protection asked for. off: the buffer starts that far in
// (16: an unaligned pointer, which the GPU cannot import).
static unsigned char *pages(size_t n, int prot, int fd) {
    unsigned char *p = fd < 0 ? mmap(NULL, n, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0)
                              : mmap(NULL, n, PROT_READ, MAP_PRIVATE, fd, 0);
    assert(p != MAP_FAILED);
    if (fd < 0) { for (size_t i = 0; i < n; i++) p[i] = (unsigned char)(i * 7 + 1); assert(!mprotect(p, n, prot)); }
    return p;
}
static NVMTLBuffer *make(NVMTLDevice *d, unsigned char *p, NSUInteger n) {
    gotPtr = NULL; gotLen = 0;
    return [d newBufferWithBytesNoCopy:p length:n options:0 deallocator:^(void *ptr, NSUInteger len) { gotPtr = ptr; gotLen = len; }];
}
// what -dealloc hands the deallocator (the expressions are held to the source above)
static void deallocate(NVMTLBuffer *b) {
    nvk_buffer *host = b->_shadow.imported ? &b->_shadow : &b->_b;
    void *hostPtr = b->_hostPtr ?: host->map; NSUInteger hostLen = (NSUInteger)(b->_hostLen ?: host->size);
    ((void (^)(void *, NSUInteger))b->_hostDealloc)(hostPtr, hostLen);
}
// one submission of a part that may have written the buffer: the copy in, the copy back. 1: the copy back faulted.
static int submit(NVMTLBuffer *b) {
    signal(SIGSEGV, fault); signal(SIGBUS, fault);
    if (sigsetjmp(caught, 1)) return 1;
    armed = 1; nvmtl_buffer_sync_in(b); nvmtl_buffer_sync_out(b); armed = 0;
    return 0;
}
int main(int argc, char **argv) { @autoreleasepool {
    const char *mode = argc > 1 ? argv[1] : "on";
    if (!strcmp(mode, "other")) test_progname = "Safari";
    NVMTLDevice *d = [NVMTLDevice new];
    const size_t page = (size_t)getpagesize();
    unsigned char *ro = pages(4 * page, PROT_READ, -1), *rw = pages(4 * page, PROT_READ | PROT_WRITE, -1);
    const NSUInteger n = 2 * page + 100;
    if (!strcmp(mode, "on")) {
        NVMTLBuffer *b = make(d, ro + 16, n);
        ARM("WindowServer, read-only private pages: copied once, no host pointer", b && !b->_hostPtr && !memcmp(b->_b.map, ro + 16, n));
        ARM("... -contents is still the caller's pointer", [b contents] == ro + 16);
        const unsigned in = syncIn, out = syncOut;
        ARM("... a submission neither copies in nor writes back into the read-only pages (WAS: the copy back faulted)",
            !submit(b) && syncIn == in && syncOut == out);
        ARM("... its purgeable state stays non-volatile, so the only copy is never discarded",
            [b setPurgeableState:MTLPurgeableStateEmpty] == MTLPurgeableStateNonVolatile && !purged);
        if (b->_hostDealloc) deallocate(b);
        ARM("... the deallocator gets the caller's pointer and length", gotPtr == ro + 16 && gotLen == n);
#ifndef __APPLE__
        const unsigned extended0 = extendedAsked, basic0 = basicAsked;
#endif
        NVMTLBuffer *w = make(d, rw + 16, n);
        ARM("writable pages keep the copy refreshed at every submission, as before", w && w->_hostPtr == rw + 16 && [w contents] == rw + 16);
#ifndef __APPLE__
        ARM("... and are refused from the basic answer alone: the page-walking extended answer is not asked",
            basicAsked > basic0 && extendedAsked == extended0);
#endif
        const unsigned in2 = syncIn;
        ARM("... and are copied in and back", !submit(w) && syncIn == in2 + 1);
        // a range that runs from read-only pages into writable ones
        unsigned char *mixed = pages(4 * page, PROT_READ | PROT_WRITE, -1);
        assert(!mprotect(mixed, 2 * page, PROT_READ));
        NVMTLBuffer *m = make(d, mixed + 16, n);
        ARM("a range that runs into writable pages keeps the refreshed copy", m && m->_hostPtr == mixed + 16);
        // read-only pages of a file: whoever writes the file changes them
        char path[] = "/tmp/nvmtl-nco-XXXXXX"; const int fd = mkstemp(path); assert(fd >= 0); unlink(path);
        assert(ftruncate(fd, (off_t)(4 * page)) == 0);
        unsigned char *file = pages(4 * page, PROT_READ, fd);
        NVMTLBuffer *f = make(d, file + 16, n);
        ARM("read-only pages of a file keep the refreshed copy", f && f->_hostPtr == file + 16);
        close(fd);
    } else {
        NVMTLBuffer *b = make(d, ro + 16, n);
        ARM(!strcmp(mode, "off") ? "NVMTL_WS_NOCOPY_ONCE=0: read-only pages keep the refreshed copy, as in 3.0.4"
                                 : "another process: read-only pages keep the refreshed copy",
            b && b->_hostPtr == ro + 16);
    }
    return fails ? 1 : 0;
} }
'''

for key, value in (('BUFFER_TYPE', buffer_type), ('SYNC', sync), ('CONTENTS', contents), ('PURGE', purge), ('PRE', pre), ('NOCOPY', nocopy)):
    fixture = fixture.replace('@@' + key + '@@', value)

status = 0
with tempfile.TemporaryDirectory() as directory:
    path = pathlib.Path(directory)
    include = path / 'include'; include.mkdir()
    if policy is not None:
        (include / 'nvmtl_nocopy_once.h').write_text(policy)
        (path / 'policy.c').write_text(policy_test)
        subprocess.run(['cc', '-I', str(include), str(path / 'policy.c'), '-o', str(path / 'policy')], check=True)
        status |= subprocess.run([str(path / 'policy')]).returncode
    else:
        print('FAIL  plugin/nvmtl_nocopy_once.h is missing')
        status = 1
        (include / 'nvmtl_nocopy_once.h').write_text('')
    (path / 'test.m').write_text(fixture)
    subprocess.run(['clang', '-fobjc-arc', '-I', str(include), '-framework', 'Foundation',
                    str(path / 'test.m'), '-o', str(path / 'test')], check=True)
    env = {k: v for k, v in os.environ.items() if k != 'NVMTL_WS_NOCOPY_ONCE'}
    status |= subprocess.run([str(path / 'test'), 'on'], env=env).returncode
    status |= subprocess.run([str(path / 'test'), 'other'], env=env).returncode
    status |= subprocess.run([str(path / 'test'), 'off'], env={**env, 'NVMTL_WS_NOCOPY_ONCE': '0'}).returncode
print('PASS' if not status else 'FAIL', ': WindowServer\'s read-only private no-copy pages are copied once and never written back; '
      'writable pages, files, other processes and NVMTL_WS_NOCOPY_ONCE=0 keep the refreshed copy')
raise SystemExit(1 if status else 0)
