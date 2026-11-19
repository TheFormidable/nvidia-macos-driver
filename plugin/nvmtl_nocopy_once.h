/* nvmtl_nocopy_once.h - which newBufferWithBytesNoCopy ranges are copied once, when the buffer is made, instead of
   before every submission and back after every part that may have written them.

   A range the GPU cannot import (an unaligned pointer or length) is copied into memory of the driver's own. In 3.0.4
   that copy is refreshed from the caller's pages before every submission, and written back into them after every part
   that may have written the buffer. Pull request 524 of the original project (its commit "fix 4K 240 Hz WindowServer
   crash") stops both for every such range of WindowServer, and hands WindowServer the driver's copy in place of its own
   pages. Nothing in this tree shows how WindowServer fills those pages: if it wrote them through its own pointer after
   making the buffer, the GPU would draw the bytes of the first copy and nothing would say so. So this takes the pull
   request only where nothing can write those pages behind the driver's back, which the driver can see in the mapping:
   every page of the range is mapped in this process without write access, privately (not shared with another task and
   not mapped a second time in this one) and not backed by a file or a device pager. Such pages hold the bytes they held
   when the buffer was made for as long as that stays so, and what this relies on is that WindowServer does not make
   them writable again (mprotect, vm_protect) and write them while the buffer lives. -contents still returns the
   caller's pointer, as Metal says it does. Writing the copy back into such pages, as 3.0.4 does after a part that may
   have written the buffer, faults: in WindowServer, a crash.
   NVMTL_WS_NOCOPY_ONCE=0: every such range is copied before each submission and written back, as in 3.0.4.

   Pure, so tools/test_nocopy_once.py proves it on the host; the plugin feeds it what mach_vm_region answers
   (VM_REGION_BASIC_INFO_64 first, VM_REGION_EXTENDED_INFO only for a region that answer leaves possible). The constants below are the values of <mach/vm_prot.h> and <mach/vm_region.h>, and the
   plugin checks them against those headers when it is built. */
#ifndef NVMTL_NOCOPY_ONCE_H
#define NVMTL_NOCOPY_ONCE_H
#include <stdint.h>
#include <string.h>

enum { NVMTL_NCO_PROT_WRITE = 0x02 };
enum { NVMTL_NCO_SM_COW = 1, NVMTL_NCO_SM_PRIVATE = 2, NVMTL_NCO_SM_EMPTY = 3, NVMTL_NCO_SM_SHARED = 4,
       NVMTL_NCO_SM_TRUESHARED = 5, NVMTL_NCO_SM_PRIVATE_ALIASED = 6, NVMTL_NCO_SM_SHARED_ALIASED = 7,
       NVMTL_NCO_SM_LARGE_PAGE = 8 };
/* A range made of more mapping regions than this is not looked at further (and is copied as before). */
enum { NVMTL_NCO_MAX_REGIONS = 64 };

/* One mapping region as mach_vm_region describes it. */
typedef struct { uint64_t start, size; int protection, share_mode, external_pager; } nvmtl_nco_region;
/* The region that holds addr, or the first one above it: 0 and *out filled, or nonzero when there is none. */
typedef int (*nvmtl_nco_query)(void *context, uint64_t addr, nvmtl_nco_region *out);

/* Whether nothing but a change of its protection can change what this region holds. A copy-on-write region qualifies:
   a write by the other side of the copy goes to a page of its own. A region mapped twice in this task (the aliased
   modes) does not, since the other mapping may be writable; nor does one shared with another task, nor a mode this
   does not know. Nor does an "empty" one: the kernel answers that for a submap, for memory with no object behind it
   yet and for physically contiguous memory (memory a driver maps in), and cannot tell those apart here. */
static inline int nvmtl_nco_region_sealed(const nvmtl_nco_region *r)
{
    if (r->protection & NVMTL_NCO_PROT_WRITE) return 0;
    if (r->external_pager) return 0;
    return r->share_mode == NVMTL_NCO_SM_COW || r->share_mode == NVMTL_NCO_SM_PRIVATE;
}

/* Whether every page of [p, p + n) lies in regions that qualify, with no hole between them. */
static inline int nvmtl_nco_range_sealed(uint64_t p, uint64_t n, nvmtl_nco_query query, void *context)
{
    if (!n || !query || p + n < p) return 0;
    const uint64_t end = p + n;
    uint64_t at = p;
    for (unsigned k = 0; at < end; k++) {
        nvmtl_nco_region r;
        if (k >= NVMTL_NCO_MAX_REGIONS || query(context, at, &r)) return 0;
        if (r.start > at || r.start + r.size <= at || r.start + r.size < r.start) return 0;   /* a hole, or no answer */
        if (!nvmtl_nco_region_sealed(&r)) return 0;
        at = r.start + r.size;
    }
    return 1;
}

/* Whether this process copies such ranges once at all: WindowServer, unless `setting` (NVMTL_WS_NOCOPY_ONCE, may be
   NULL) starts with '0'. */
static inline int nvmtl_nco_applies(const char *process, const char *setting)
{
    return process && !strcmp(process, "WindowServer") && !(setting && setting[0] == '0');
}
#endif
