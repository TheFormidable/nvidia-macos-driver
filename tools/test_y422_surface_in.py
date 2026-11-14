#!/usr/bin/env python3
"""A 4:2:2 texture over an IOSurface plane (a camera frame) copies the plane's rows only when they end inside the
surface's allocation, and waits for another process's GPU write of the plane, or WindowServer's, before it reads.
Compiles the production -[NVMTLTexture nvmtlY422SurfaceIn] body against stand-in IOSurface and wait functions and
checks a 1080p 2vuy frame, padded rows that end exactly at the allocation's end, rows that run past it (by 4 KB, and
a last row by one byte), a plane that starts past it, and the order of the waits. Run against the 2.9 source (a path
as the argument), the arms for rows past the allocation and for the waits fail."""
import pathlib
import subprocess
import sys
import tempfile

root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1])
src = (root / 'plugin/NVMTLObjects.m').read_text()
head = '- (void)nvmtlY422SurfaceIn {'
body = src[src.index(head) + len(head):]
depth, i = 1, 0
while depth:
    depth += {'{': 1, '}': -1}.get(body[i], 0)
    i += 1
body = body[:i - 1].replace('@synchronized (self) {', '{', 1).replace('[self nvmtlY422Upload:0 rows:_i.h]', 'upload()')

HARNESS = r'''
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include "nvmtl_yuv422.h"
#define BOOL int
#define YES 1
#define NO 0
#define kIOSurfaceLockReadOnly 1u
#define kIOReturnSuccess 0
typedef struct { size_t alloc, bpr, h, off; uint32_t seed; char *base; int foreign; } surf_t;
static char ev[64]; static int locks, uploads;
static void note(char c) { size_t n = strlen(ev); if (n + 1 < sizeof ev) { ev[n] = c; ev[n + 1] = 0; } }
static uint32_t IOSurfaceGetSeed(surf_t *s) { return s->seed; }
static uint32_t IOSurfaceGetID(surf_t *s) { return 7; }
static int IOSurfaceLock(surf_t *s, uint32_t o, uint32_t *seed) { note('L'); locks++; return kIOReturnSuccess; }
static int IOSurfaceUnlock(surf_t *s, uint32_t o, uint32_t *seed) { note('U'); locks--; return kIOReturnSuccess; }
static size_t IOSurfaceGetAllocSize(surf_t *s) { return s->alloc; }
static void *IOSurfaceGetBaseAddress(surf_t *s) { return s->base; }
static void *IOSurfaceGetBaseAddressOfPlane(surf_t *s, uint32_t p) { return s->base + s->off; }
static size_t IOSurfaceGetBytesPerRowOfPlane(surf_t *s, uint32_t p) { return s->bpr; }
static size_t IOSurfaceGetHeightOfPlane(surf_t *s, uint32_t p) { return s->h; }
static int nvmtl_busy_foreign(surf_t *s, unsigned long p) { note('F'); return s->foreign; }
static void nvmtl_busy_wait(surf_t *s, unsigned long p) { note('W'); }
static void nvmtl_vk_fence_wait_compositor(uint32_t sid) { note('C'); }
#define nvlog(...) ((void)0)
static struct { uint32_t w, h; } _i;
static struct { void *mutableBytes; } _y422Raw;
static surf_t *_y422Surf; static unsigned long _y422Plane; static uint32_t _y422Seed;
static BOOL _y422Synced, _clientWritten;
static void upload(void) { note('P'); uploads++; }
static void surface_in(void) {''' + body + r'''}
static char *mem;
static void fresh(surf_t *s, size_t alloc, size_t bpr, size_t h, size_t off, int foreign) {
    memset(s, 0, sizeof *s); s->alloc = alloc; s->bpr = bpr; s->h = h; s->off = off; s->seed = 1; s->base = mem;
    s->foreign = foreign;
    _y422Surf = s; _y422Plane = 0; _y422Seed = 0; _y422Synced = NO; _clientWritten = NO; _i.w = 1920; _i.h = 1080;
    memset(_y422Raw.mutableBytes, 0, nvmtl_y422_row_bytes(_i.w) * _i.h); ev[0] = 0; locks = uploads = 0;
}
int main(void) {
    const size_t span = 1920 * 2;
    mem = calloc(64 << 20, 1); _y422Raw.mutableBytes = malloc(span * 1080); surf_t s;
    mem[0] = 11; mem[span * 1079 + span - 1] = 22;
    fresh(&s, span * 1080, span, 1080, 0, 0); surface_in();
    const char *raw = _y422Raw.mutableBytes;
    printf("%d\n", uploads == 1 && _y422Synced && raw[0] == 11 && raw[span * 1080 - 1] == 22 && !locks);
    fresh(&s, 4096 * 1079 + span, 4096, 1080, 0, 0); surface_in();
    printf("%d\n", uploads == 1 && _y422Synced && !locks);
    fresh(&s, span * 1080 - 4096, span, 1080, 0, 0); surface_in();
    printf("%d\n", uploads == 0 && !_y422Synced && !locks && raw[0] == 0);
    fresh(&s, span * 1080 - 1, span, 1080, 0, 0); surface_in();
    printf("%d\n", uploads == 0 && !_y422Synced && !locks && raw[0] == 0);
    fresh(&s, span * 1080, span, 1080, span * 1080 + 64, 0); surface_in();
    printf("%d\n", uploads == 0 && !_y422Synced && !locks);
    fresh(&s, span * 1080, span, 1080, 0, 1); surface_in();
    printf("%d\n", strcmp(ev, "FWLUP") == 0 && _clientWritten);
    ev[0] = 0; uploads = 0; surface_in();
    printf("%d\n", strcmp(ev, "W") == 0 && uploads == 0);
    fresh(&s, span * 1080, span, 1080, 0, 0); surface_in();
    printf("%d\n", strcmp(ev, "FCLUP") == 0);
    ev[0] = 0; uploads = 0; surface_in();
    printf("%d\n", ev[0] == 0 && uploads == 0);
    return 0;
}
'''

with tempfile.TemporaryDirectory() as d:
    c, exe = pathlib.Path(d) / 'y422.c', pathlib.Path(d) / 'y422'
    c.write_text(HARNESS)
    r = subprocess.run(['cc', '-w', '-I' + str(root / 'plugin'), '-o', str(exe), str(c)], capture_output=True,
                       text=True)
    if r.returncode:
        print(r.stderr[-1500:])
        sys.exit('could not compile nvmtlY422SurfaceIn - check is UNKNOWN, not clean')
    got = [int(x) for x in subprocess.check_output([str(exe)], text=True).split()]
names = ['a 1080p 2vuy frame that fits is copied whole and uploaded, the surface unlocked',
         'padded rows whose last row ends exactly at the allocation\'s end are copied',
         'rows that run 4 KB past the allocation are skipped: nothing copied, no upload, the surface unlocked',
         'a last row that ends one byte past the allocation is skipped',
         'a plane that starts past the allocation is skipped',
         'another process\'s busy tag: its write is waited for before the surface is locked',
         'then the next use waits for that writer even though the seed has not moved',
         'no tag: WindowServer\'s hold is waited for before the surface is locked',
         'control: an unchanged seed after a copy does nothing']
want = [1] * len(names)
fails = 0
for n, g, w in zip(names, got, want):
    ok = g == w
    print(('ok   ' if ok else 'FAIL ') + n)
    fails += not ok
if len(got) != len(names):
    print(f'FAIL the harness printed {len(got)} results for {len(names)} arms')
    fails += 1
print('FAILED' if fails else 'all ok')
sys.exit(1 if fails else 0)
