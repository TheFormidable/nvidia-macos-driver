/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

// nvrm_dma.cpp: the bus address of system memory, for a GPU that a DMA mapper translates.
//
// Until 2.9 every address the driver gave the GPU was a CPU physical address. That is right when nothing sits between
// the GPU and memory, and wrong under VT-d: AppleVTD points every PCI function at one translated address space, so a
// physical address on the bus is either a DMA fault or, worse, some other device's mapping. This file follows the Linux
// driver instead (kernel-open/nvidia/nv-dma.c): when the GPU has a mapper, nv_requires_dma_remap() is true, the RM
// keeps physical addresses for the CPU and calls nv_dma_map_alloc() for the address the GPU must use, and each mapping
// is an IODMACommand on the allocation's memory descriptor, read back from the mapper's own tables before the RM sees
// it. With no mapper (mode NVRM_DMA_NONE, every machine 2.9 works on) each entry point below returns before doing
// anything 2.9 did not do; the comment at each one says where.
//
// Locks: gDmaLock (an IOLock) guards the device table, the list of live mappings and the users count of a device. It
// is a leaf: held for list walks and pointer updates only, never across an IOKit call, an allocation, a log line or a
// call into the RM. Callers may hold the RM's API and GPU locks; this file never calls into the RM. IOKit's own locks
// (a descriptor's prepare lock, AppleVTD's space lock) are taken inside prepare/complete with none of ours held.
//
// Lifetime: device records live in a static table and are never freed, so a reader that races a GPU's stop reads a
// stale record, never freed memory. A mapping holds its own reference and its own wire count on the descriptor it
// maps, so the pages stay alive until its unmap whatever the owner of the descriptor does meanwhile (the RM unpins a
// process's pages before it unmaps them). Every mapping is released exactly once: by the RM's unmap, by the
// accelerator's UNMAP, or by nvrm_dma_detach() for what is left when the GPU stops; each of those first unlinks the
// mapping under the lock, and only the one that unlinked it releases it.
//
// Sleep and wake: nothing to do. The translations live in AppleVTD's page tables in memory and AppleVTD restores its
// units before device drivers are told to power up, so a mapping made before sleep is valid after wake and the RM
// resumes with the same bus addresses (debug.nvrm_dma_live reads the same before and after).

#include "nv-xnu.h"
#include <IOKit/IOMapper.h>
#include <IOKit/IODMACommand.h>
#include <IOKit/IOMemoryDescriptor.h>
#include <IOKit/IOBufferMemoryDescriptor.h>
#include <libkern/OSAtomic.h>
#include <sys/sysctl.h>
#include <pexpert/pexpert.h>
#include <mach/mach_time.h>
#include <kern/clock.h>
#include "nvrm_dma.h"
extern "C" {
#include "nvrm_dma_policy.h"
}

// NV_XNU_DMA_LAYER (nv-xnu.h) says that the OS layer (os-xnu2.cpp) has left the RM's DMA interface
// (nv_requires_dma_remap, nv_dma_map_alloc, nv_dma_unmap_alloc) to this file and provides nv_xnu_dma_bits_now() and
// nv_xnu_dma_source(). In a tree where it does not, the OS layer still answers the RM with physical addresses; this
// file then defines none of the three (two definitions would not link) and refuses any GPU behind a mapper instead of
// using it, which is the only safe answer there.
#ifdef NV_XNU_DMA_LAYER
#define DMA_RM_ROUTED 1
#else
#define DMA_RM_ROUTED 0
#endif

// Every line here is rate limited at its call site; like NVRM.cpp's LOG it goes to the serial log and the system log.
#define DLOG(fmt, ...) do { kprintf("NVRM-xnu: " fmt "\n", ##__VA_ARGS__); IOLog("NVRM-xnu: " fmt "\n", ##__VA_ARGS__); } while (0)

#ifndef kIOMemoryDescriptorOptionsKey
#define kIOMemoryDescriptorOptionsKey "IOMemoryDescriptorOptions"   // IOPCIFamily sets it on a nub it leaves untranslated
#endif

enum : UInt32 { kDevFree = 0, kDevLive = 0x4e444431u /* NDD1 */, kDevBusy = 0x4e444442u /* NDDB: being attached */,
                kDevRetired = 0x4e444458u /* NDDX: a stop could not drain it; never reused */ };
enum : NvU32 { kMapMagic = 0x4e444d31u /* NDM1 */, kMapDead = 0x4e444d58u /* NDMX */ };
enum : NvU32 { kKindRm = 0, kKindUser = 1, kKindSurf = 2, kKindTest = 3 };
enum { kDmaMaxDev = 8, kDmaMaxSeg = 16, kDmaDrainMs = 2000 };

struct nv_dma_device {             // the RM's opaque nv_dma_device_t (nv.h); nv->dma_dev points at one of gDmaDevs
    volatile UInt32 state;         // kDev*; only kDevLive is ever handed out
    enum nvrm_dma_mode mode;
    enum nvrm_dma_why whyCode;
    const char *why;               // static text
    nv_state_t *nv;
    IOPCIDevice *pci;              // the provider; outlives this record's use
    IOMapper *mapper;              // DEVICE mode: retained. SYSTEM mode: NULL, and IOMapper::gSystem is used
    volatile SInt32 users;         // map / unmap calls in progress on this device (taken under gDmaLock)
    volatile SInt32 dead;          // set under gDmaLock when the GPU stops: no new user, no new mapping
};
struct nv_xnu_dmamap {             // one per live mapping; what the RM keeps as its mapping's private data
    NvU32 magic, kind;
    nv_dma_device *dev;
    IOMapper *mapper;              // whose address space holds the range; compared, never called
    IOMemoryDescriptor *md;        // retained AND prepared by this mapping
    IODMACommand *cmd;             // prepared
    NvU64 base, bytes, serial;     // base is the bus address of byte 0 (one run when runs == 1); serial: the token
    NvU32 runs;
    nv_xnu_dmamap *prev, *next;    // gDmaHead list, under gDmaLock
};

static IOLock *gDmaLock;                           // created by the first attach, never freed (see the file comment)
static nv_dma_device gDmaDevs[kDmaMaxDev];
static nv_xnu_dmamap *gDmaHead;
static UInt64 gDmaSerial;                          // under gDmaLock; never 0 for a live mapping
static volatile SInt32 gDmaSysctl, gDmaEverRefused;
static int gDmaMode;
static volatile SInt64 gDmaMaps, gDmaUnmaps, gDmaLive, gDmaLiveBytes, gDmaPeakBytes, gDmaFail, gDmaVerifyBad, gDmaVerified,
                       gDmaUnaddressable, gDmaNotContig, gDmaBudget, gDmaBadPriv, gDmaRefused, gDmaUserMaps, gDmaSurfMaps,
                       gDmaPageoffRefused, gDmaMaxUs, gDmaLo, gDmaHi, gDmaLeaked;

SYSCTL_INT(_debug, OID_AUTO, nvrm_dma_mode, CTLFLAG_RD | CTLFLAG_LOCKED, &gDmaMode, 0, "GPU DMA: 0 untranslated (as 2.9), 1 system mapper, 2 device mapper, 3 a GPU was refused (highest of the GPUs)");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_dma_maps, CTLFLAG_RD | CTLFLAG_LOCKED, (long long *)&gDmaMaps, "allocations mapped through the DMA mapper");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_dma_unmaps, CTLFLAG_RD | CTLFLAG_LOCKED, (long long *)&gDmaUnmaps, "DMA mappings released");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_dma_live, CTLFLAG_RD | CTLFLAG_LOCKED, (long long *)&gDmaLive, "DMA mappings live now");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_dma_live_bytes, CTLFLAG_RD | CTLFLAG_LOCKED, (long long *)&gDmaLiveBytes, "bytes mapped now");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_dma_peak_bytes, CTLFLAG_RD | CTLFLAG_LOCKED, (long long *)&gDmaPeakBytes, "most bytes mapped at once");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_dma_fail, CTLFLAG_RD | CTLFLAG_LOCKED, (long long *)&gDmaFail, "mappings the mapper refused or could not serve");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_dma_verified, CTLFLAG_RD | CTLFLAG_LOCKED, (long long *)&gDmaVerified, "pages read back from the mapper's tables");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_dma_verify_bad, CTLFLAG_RD | CTLFLAG_LOCKED, (long long *)&gDmaVerifyBad, "pages whose bus address did not lead back to the page (must stay 0)");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_dma_unaddressable, CTLFLAG_RD | CTLFLAG_LOCKED, (long long *)&gDmaUnaddressable, "mappings above the GPU's address width (must stay 0)");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_dma_not_contig, CTLFLAG_RD | CTLFLAG_LOCKED, (long long *)&gDmaNotContig, "contiguous allocations the mapper split (must stay 0)");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_dma_budget_refused, CTLFLAG_RD | CTLFLAG_LOCKED, (long long *)&gDmaBudget, "mappings refused to keep the mapper's space from filling");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_dma_bad_priv, CTLFLAG_RD | CTLFLAG_LOCKED, (long long *)&gDmaBadPriv, "map or unmap calls with private data that is not this driver's");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_dma_refused, CTLFLAG_RD | CTLFLAG_LOCKED, (long long *)&gDmaRefused, "peer and MMIO mappings refused (no such mapping on macOS)");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_dma_user_maps, CTLFLAG_RD | CTLFLAG_LOCKED, (long long *)&gDmaUserMaps, "mappings of a process's pinned pages");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_dma_surf_maps, CTLFLAG_RD | CTLFLAG_LOCKED, (long long *)&gDmaSurfMaps, "mappings made for the accelerator's page-off buffers");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_dma_pageoff_refused, CTLFLAG_RD | CTLFLAG_LOCKED, (long long *)&gDmaPageoffRefused, "page-off copies refused: pages not mapped through the mapper");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_dma_max_us, CTLFLAG_RD | CTLFLAG_LOCKED, (long long *)&gDmaMaxUs, "slowest mapping call, microseconds");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_dma_lo, CTLFLAG_RD | CTLFLAG_LOCKED, (long long *)&gDmaLo, "lowest bus address handed out through the mapper");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_dma_hi, CTLFLAG_RD | CTLFLAG_LOCKED, (long long *)&gDmaHi, "highest bus address handed out through the mapper");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_dma_leaked, CTLFLAG_RD | CTLFLAG_LOCKED, (long long *)&gDmaLeaked, "mappings still live when their GPU stopped");

static void dmaSysctls(void)
{
    if (!OSCompareAndSwap(0, 1, &gDmaSysctl)) return;
    sysctl_register_oid(&sysctl__debug_nvrm_dma_mode); sysctl_register_oid(&sysctl__debug_nvrm_dma_maps);
    sysctl_register_oid(&sysctl__debug_nvrm_dma_unmaps); sysctl_register_oid(&sysctl__debug_nvrm_dma_live);
    sysctl_register_oid(&sysctl__debug_nvrm_dma_live_bytes); sysctl_register_oid(&sysctl__debug_nvrm_dma_peak_bytes);
    sysctl_register_oid(&sysctl__debug_nvrm_dma_fail); sysctl_register_oid(&sysctl__debug_nvrm_dma_verified);
    sysctl_register_oid(&sysctl__debug_nvrm_dma_verify_bad); sysctl_register_oid(&sysctl__debug_nvrm_dma_unaddressable);
    sysctl_register_oid(&sysctl__debug_nvrm_dma_not_contig); sysctl_register_oid(&sysctl__debug_nvrm_dma_budget_refused);
    sysctl_register_oid(&sysctl__debug_nvrm_dma_bad_priv); sysctl_register_oid(&sysctl__debug_nvrm_dma_refused);
    sysctl_register_oid(&sysctl__debug_nvrm_dma_user_maps); sysctl_register_oid(&sysctl__debug_nvrm_dma_surf_maps);
    sysctl_register_oid(&sysctl__debug_nvrm_dma_pageoff_refused); sysctl_register_oid(&sysctl__debug_nvrm_dma_max_us);
    sysctl_register_oid(&sysctl__debug_nvrm_dma_lo); sysctl_register_oid(&sysctl__debug_nvrm_dma_hi);
    sysctl_register_oid(&sysctl__debug_nvrm_dma_leaked);
}

static void dmaZero(void *p, unsigned long n) { volatile unsigned char *b = (volatile unsigned char *)p; while (n--) *b++ = 0; }
// A device record from the RM's opaque pointer: only an address inside the table, and only a live record. Nothing at
// the pointer is read before it is known to be one of ours. No lock: records are never freed.
static nv_dma_device *dmaDev(const void *p)
{
    const uintptr_t a = (uintptr_t)p, lo = (uintptr_t)&gDmaDevs[0], hi = (uintptr_t)&gDmaDevs[kDmaMaxDev];
    if (a < lo || a >= hi || (a - lo) % sizeof(nv_dma_device)) return NULL;
    nv_dma_device *d = (nv_dma_device *)p;
    return d->state == kDevLive ? d : NULL;
}
static nv_dma_device *dmaDevOf(const nv_state_t *nv) { return nv ? dmaDev(nv->dma_dev) : NULL; }
static IOMapper *dmaMapper(const nv_dma_device *d) { return d->mapper ? d->mapper : IOMapper::gSystem; }
static void dmaMaxOf(volatile SInt64 *v, SInt64 x) { for (SInt64 o = *v; x > o && !OSCompareAndSwap64((UInt64)o, (UInt64)x, v); o = *v) {} }
static void dmaMinOf(volatile SInt64 *v, SInt64 x) { for (SInt64 o = *v; (o == 0 || x < o) && !OSCompareAndSwap64((UInt64)o, (UInt64)x, v); o = *v) {} }
static unsigned dmaBitsNow(void)
{
#if DMA_RM_ROUTED
    return nv_xnu_dma_bits_now();
#else
    return 64;
#endif
}
// debug.nvrm_dma_mode: the highest mode of the GPUs attached now, and 3 for good once a GPU was refused (a declined
// start detaches at once, and the refusal is what the owner must see).
static void dmaPublishMode(void)
{
    int m = gDmaEverRefused ? (int)NVRM_DMA_REFUSE : (int)NVRM_DMA_NONE;
    for (int i = 0; i < kDmaMaxDev; i++) if (gDmaDevs[i].state == kDevLive && (int)gDmaDevs[i].mode > m) m = (int)gDmaDevs[i].mode;
    gDmaMode = m;
}

// Count a user of a device that maps or unmaps, so that the GPU's stop waits for it. NULL when the device is not live
// or is stopping: the caller then does nothing.
static nv_dma_device *dmaGet(nv_dma_device *d)
{
    IOLock *l = gDmaLock;
    if (!d || !l) return NULL;
    IOLockLock(l);
    if (d->state != kDevLive || d->dead) d = NULL; else OSIncrementAtomic(&d->users);
    IOLockUnlock(l);
    return d;
}
static void dmaPut(nv_dma_device *d) { if (d) OSDecrementAtomic(&d->users); }

// Read a mapping back from the mapper's own tables: the bus address of a page must lead to that page. Returns the
// number of pages that did not. The first and last page always; every page (up to 4096) when full is set.
static NvU64 dmaVerify(nv_dma_device *d, IOMemoryDescriptor *md, const NvU64 *seg_addr, const NvU64 *seg_len, unsigned nseg,
                       NvU64 pages, bool full)
{
    IOMapper *mp = dmaMapper(d);
    if (!mp) return pages;
    NvU64 bad = 0, page = 0;
    for (unsigned s = 0; s < nseg; s++) {
        for (NvU64 k = 0; k < seg_len[s] && page < pages; k += PAGE_SIZE, page++) {
            if (!nvrm_dma_verify_page(page, pages, full)) continue;
            IOByteCount len = 0;
            const addr64_t phys = md->getPhysicalSegment((IOByteCount)(page * PAGE_SIZE), &len, kIOMemoryMapperNone);
            const uint64_t back = mp->mapToPhysicalAddress(seg_addr[s] + k);
            OSIncrementAtomic64(&gDmaVerified);
            if (!phys || back != phys) {
                if (OSIncrementAtomic64(&gDmaVerifyBad) < 8)
                    DLOG("DMA VERIFY FAILED: page %llu bus 0x%llx leads to 0x%llx, the page is at 0x%llx",
                         page, seg_addr[s] + k, (unsigned long long)back, (unsigned long long)phys);
                bad++;
            }
        }
    }
    return bad;
}

static void dmaRelease(nv_xnu_dmamap *m)
{
    if (m->cmd) { m->cmd->complete(false, false); m->cmd->clearMemoryDescriptor(false); m->cmd->release(); m->cmd = NULL; }
    if (m->md) { m->md->complete(); m->md->release(); m->md = NULL; }
}
static void dmaFree(nv_xnu_dmamap *m)
{
    dmaRelease(m);
    m->magic = kMapDead;
    IOFree(m, sizeof *m);
}

// Map [0, bytes) of md for the GPU of d. `out` gets the base (contig) or one bus address per page (`pages` of them).
// The caller holds a user count on d (dmaGet). On success *pm is the mapping, linked and counted.
static NV_STATUS dmaMap(nv_dma_device *d, IOMemoryDescriptor *md, NvU64 bytes, unsigned bits, bool contig, NvU64 *out,
                        NvU64 pages, NvU32 kind, nv_xnu_dmamap **pm)
{
    const uint64_t t0 = mach_absolute_time();
    if (!nvrm_dma_budget_ok(d->mode, (unsigned long long)gDmaLiveBytes, bytes)) {
        if (OSIncrementAtomic64(&gDmaBudget) < 4)
            DLOG("DMA map of %llu KB refused: %lld MB are mapped already (the mapper's space is finite)",
                 bytes >> 10, (long long)(gDmaLiveBytes >> 20));
        return NV_ERR_NO_MEMORY;
    }
    nv_xnu_dmamap *m = (nv_xnu_dmamap *)IOMalloc(sizeof *m);
    if (!m) return NV_ERR_NO_MEMORY;
    dmaZero(m, sizeof *m);
    m->magic = kMapMagic; m->kind = kind; m->dev = d; m->mapper = dmaMapper(d); m->bytes = bytes;
    NV_STATUS st = NV_ERR_OPERATING_SYSTEM;
    NvU64 sa[kDmaMaxSeg], sl[kDmaMaxSeg]; unsigned nseg = 0;
    do {
        // The mapping's own reference and wire count: the pages stay alive until dmaRelease, whatever the
        // descriptor's owner does meanwhile.
        md->retain();
        if (md->prepare() != kIOReturnSuccess) { md->release(); st = NV_ERR_NO_MEMORY; OSIncrementAtomic64(&gDmaFail); break; }
        m->md = md;
        m->cmd = IODMACommand::withSpecification(kIODMACommandOutputHost64, (UInt8)bits, 0, IODMACommand::kMapped, 0, 1,
                                                 d->mapper, NULL);
        if (!m->cmd) { st = NV_ERR_NO_MEMORY; OSIncrementAtomic64(&gDmaFail); break; }
        IOReturn r = m->cmd->setMemoryDescriptor(md, false);
        if (r == kIOReturnSuccess) {
            r = m->cmd->prepare(0, bytes, false, false);
            if (r != kIOReturnSuccess) m->cmd->clearMemoryDescriptor(false);
        }
        if (r != kIOReturnSuccess) {
            if (OSIncrementAtomic64(&gDmaFail) < 8) DLOG("DMA map of %llu KB (%u bits) failed: 0x%x", bytes >> 10, bits, r);
            m->cmd->release(); m->cmd = NULL;
            st = NV_ERR_NO_MEMORY; break;
        }
        UInt64 off = 0; bool segOk = true;
        while (off < bytes) {
            IODMACommand::Segment64 seg; UInt32 n = 1;
            if (nseg == kDmaMaxSeg || m->cmd->gen64IOVMSegments(&off, &seg, &n) != kIOReturnSuccess || n != 1 || !seg.fLength) {
                segOk = false; break;
            }
            sa[nseg] = seg.fIOVMAddr; sl[nseg] = seg.fLength; nseg++;
        }
        if (!segOk) {
            if (OSIncrementAtomic64(&gDmaFail) < 8) DLOG("DMA map of %llu KB: the mapper gave more than %d segments or none", bytes >> 10, kDmaMaxSeg);
            break;
        }
        const int x = nvrm_dma_expand(sa, sl, nseg, pages, contig, out);
        if (x != NVRM_DMA_X_OK) {
            if (x == NVRM_DMA_X_NOT_CONTIG) OSIncrementAtomic64(&gDmaNotContig); else OSIncrementAtomic64(&gDmaFail);
            DLOG("DMA map of %llu pages: the mapper's segments cannot serve it (%d, %u segments, contig %d)", pages, x, nseg, (int)contig);
            break;
        }
        bool addr = true;
        for (unsigned s = 0; s < nseg; s++) addr = addr && nvrm_dma_addressable(sa[s], sl[s], bits);
        if (!addr) {
            if (OSIncrementAtomic64(&gDmaUnaddressable) < 8) DLOG("DMA map at 0x%llx is above the GPU's %u address bits", sa[0], bits);
            st = NV_ERR_INVALID_ADDRESS; break;
        }
        if (dmaVerify(d, md, sa, sl, nseg, pages, nvrm_dma_verify_full((unsigned long long)gDmaMaps) != 0)) break;
        st = NV_OK;
    } while (0);
    if (st == NV_OK) {
        m->runs = nvrm_dma_runs(sa, sl, nseg);
        m->base = sa[0];
        // Linked only while the GPU is not stopping: a stop that already drained the list must not miss this one.
        IOLockLock(gDmaLock);
        if (d->dead) st = NV_ERR_INVALID_STATE;
        else {
            m->serial = ++gDmaSerial;
            m->next = gDmaHead; if (gDmaHead) gDmaHead->prev = m; gDmaHead = m;
        }
        IOLockUnlock(gDmaLock);
    }
    if (st != NV_OK) { dmaFree(m); return st; }
    for (unsigned s = 0; s < nseg; s++) { dmaMinOf(&gDmaLo, (SInt64)sa[s]); dmaMaxOf(&gDmaHi, (SInt64)(sa[s] + sl[s] - 1)); }
    OSIncrementAtomic64(&gDmaMaps); OSIncrementAtomic64(&gDmaLive);
    dmaMaxOf(&gDmaPeakBytes, OSAddAtomic64((SInt64)bytes, &gDmaLiveBytes) + (SInt64)bytes);
    uint64_t ns = 0; absolutetime_to_nanoseconds(mach_absolute_time() - t0, &ns);
    dmaMaxOf(&gDmaMaxUs, (SInt64)(ns / 1000));
    *pm = m;
    return NV_OK;
}

// Unlink a live mapping: the one at `ptr` when ptr is given (it is compared, never read, until found), else the one
// with this serial; it must belong to `dev` when dev is given, and be of a kind in `kinds` (bit per kind). NULL when
// none matches, which is also what a second unmap of the same mapping finds.
static nv_xnu_dmamap *dmaTake(const void *ptr, UInt64 serial, const nv_dma_device *dev, unsigned kinds)
{
    nv_xnu_dmamap *m = NULL;
    IOLock *l = gDmaLock;
    if (!l || (!ptr && !serial)) return NULL;
    IOLockLock(l);
    for (nv_xnu_dmamap *p = gDmaHead; p; p = p->next) {
        if (ptr ? (const void *)p != ptr : p->serial != serial) continue;
        if ((!dev || p->dev == dev) && (kinds & (1u << p->kind))) m = p;
        break;
    }
    if (m) {
        if (m->prev) m->prev->next = m->next; else gDmaHead = m->next;
        if (m->next) m->next->prev = m->prev;
        m->prev = m->next = NULL;
    }
    IOLockUnlock(l);
    return m;
}
// Release a mapping that dmaTake unlinked.
static void dmaUnmap(nv_xnu_dmamap *m)
{
    const NvU64 bytes = m->bytes;
    dmaFree(m);
    OSIncrementAtomic64(&gDmaUnmaps); OSDecrementAtomic64(&gDmaLive); OSAddAtomic64(-(SInt64)bytes, &gDmaLiveBytes);
}

// One mapping made and read back before the RM is started: if the mapper cannot give this GPU a working translation,
// the driver must know now, not when the GPU's firmware reads garbage.
static bool dmaSelfTest(nv_dma_device *d)
{
    IOBufferMemoryDescriptor *b = IOBufferMemoryDescriptor::inTaskWithOptions(kernel_task, kIODirectionInOut, 2 * PAGE_SIZE, PAGE_SIZE);
    if (!b) return false;
    NvU64 va[2] = { 0, 0 }; nv_xnu_dmamap *m = NULL;
    const NV_STATUS st = dmaMap(d, b, 2 * PAGE_SIZE, nvrm_dma_bits(dmaBitsNow(), 0), false, va, 2, kKindTest, &m);
    const bool ok = st == NV_OK && m && va[0] && va[1] && va[0] != va[1];
    if (ok) DLOG("DMA self-test: 2 pages mapped at bus 0x%llx, 0x%llx and read back from the mapper", va[0], va[1]);
    else DLOG("DMA self-test failed (status 0x%x)", (unsigned)st);
    if (m) { nv_xnu_dmamap *t = dmaTake(m, 0, d, 1u << kKindTest); if (t) dmaUnmap(t); }
    b->release();
    return ok;
}

// ---- nvrm_dma.h ----

extern "C" struct nv_dma_device *nvrm_dma_attach(nv_state_t *nv, IOPCIDevice *pci)
{
    if (!nv) return NULL;
    if (nv_dma_device *again = dmaDevOf(nv)) return again;          // attached already: the record stays as it is
    if (!gDmaLock) {
        IOLock *l = IOLockAlloc();
        if (!l) return NULL;
        if (!OSCompareAndSwapPtr(NULL, l, (void *volatile *)&gDmaLock)) IOLockFree(l);
    }
    dmaSysctls();
    // Read on every attach, not once into a global: two GPUs start on their own threads, and a "read once" flag lets
    // the second one decide before the first has stored the value (with a mapper that would map instead of refusing).
    // The boot arguments do not change while the machine runs, so every GPU gets the same answer.
    uint32_t optArg = 1;
    const int optOut = (PE_parse_boot_argn("nvrmdma", &optArg, sizeof optArg) && optArg == 0) ? 1 : 0;
    nv_dma_device *d = NULL;
    IOLockLock(gDmaLock);
    for (int i = 0; i < kDmaMaxDev && !d; i++) if (gDmaDevs[i].state == kDevFree) { d = &gDmaDevs[i]; d->state = kDevBusy; }
    IOLockUnlock(gDmaLock);
    if (!d) { DLOG("DMA: no free record for another GPU (%d in use)", kDmaMaxDev); return NULL; }
    d->mode = NVRM_DMA_NONE; d->whyCode = NVRM_DMA_WHY_MODE; d->nv = nv; d->pci = pci; d->mapper = NULL; d->users = 0; d->dead = 0;

    // The questions IOKit itself asks before it maps a buffer for a device. The answer is settled long before this
    // driver starts (AppleVTD is installed from the PCI root bridge's start).
    IOMapper::checkForSystemMapper();
    IOMapper *dev = pci ? IOMapper::copyMapperForDevice(pci) : NULL;
    if (dev && dev == IOMapper::gSystem) { dev->release(); dev = NULL; }     // the shared space is the system mode
    const bool hadDev = dev != NULL;
    bool bypass = false;
    if (pci) if (OSNumber *n = OSDynamicCast(OSNumber, pci->getProperty(kIOMemoryDescriptorOptionsKey)))
        bypass = (n->unsigned32BitValue() & kIOMemoryMapperNone) != 0;
    enum nvrm_dma_mode mode = nvrm_dma_decide(optOut, dev != NULL, IOMapper::gSystem != NULL, bypass);
    if (mode == NVRM_DMA_DEVICE) d->mapper = dev; else if (dev) dev->release();
    d->mode = mode;
    // Modes 0 and 3 stop here. Modes 1 and 2 need the RM's calls to reach this file and a mapping that reads back.
    bool selftest = true;
    if (nvrm_dma_remaps(mode) && DMA_RM_ROUTED) selftest = dmaSelfTest(d);
    enum nvrm_dma_why why = NVRM_DMA_WHY_MODE;
    d->mode = nvrm_dma_settle(mode, DMA_RM_ROUTED, selftest ? 1 : 0, &why);
    d->whyCode = why;
    d->why = nvrm_dma_why_text(d->mode, why);
    if (d->mode != NVRM_DMA_DEVICE && d->mapper) { d->mapper->release(); d->mapper = NULL; }
    IOLockLock(gDmaLock);
    d->state = kDevLive;
    IOLockUnlock(gDmaLock);
    if (d->mode == NVRM_DMA_REFUSE) gDmaEverRefused = 1;
    dmaPublishMode();
    DLOG("DMA: %s (system mapper %s, device mapper %s%s)", d->why, IOMapper::gSystem ? "present" : "absent",
         hadDev ? "present" : "absent", bypass ? ", nub marked untranslated" : "");
    if (pci) pci->setProperty("nvrm-dma", d->why);
    return d;
}

// Any context: no lock, no sleep. NVRM_DMA_NONE for NULL, the control state and a GPU never attached or detached.
extern "C" int nvrm_dma_mode_of(const nv_state_t *nv)
{
    const nv_dma_device *d = dmaDevOf(nv);
    return d ? (int)d->mode : (int)NVRM_DMA_NONE;
}
extern "C" const char *nvrm_dma_why(const nv_state_t *nv)
{
    const nv_dma_device *d = dmaDevOf(nv);
    return d ? d->why : nvrm_dma_why_text(NVRM_DMA_NONE, NVRM_DMA_WHY_MODE);
}
// Asked by the page allocator before every allocation. Mode 0: always 0 (no GPU in table remaps), so the allocation
// options stay 2.9's. No lock: a stale answer during a start or stop is the answer of a moment before.
extern "C" int nvrm_dma_any_remap(void)
{
    for (int i = 0; i < kDmaMaxDev; i++) if (gDmaDevs[i].state == kDevLive && nvrm_dma_remaps(gDmaDevs[i].mode)) return 1;
    return 0;
}
extern "C" void nvrm_dma_detach(nv_state_t *nv)
{
    nv_dma_device *d = dmaDevOf(nv);
    if (!d) return;                                                   // never attached, or detached already
    IOLockLock(gDmaLock);
    d->dead = 1;                                                      // no new user, no new mapping from here on
    IOLockUnlock(gDmaLock);
    nv->dma_dev = NULL;
    // Users in progress (an accelerator MAP racing the stop) finish within one mapping call; a bounded wait.
    int waited = 0;
    while (d->users > 0 && waited < kDmaDrainMs) { IOSleep(1); waited++; }
    const bool drained = d->users <= 0;
    // What is still mapped: the RM has unmapped what it was going to (this runs after rm_free_private_state), and the
    // GPU is stopped, so the translations can go. Each one is unlinked first, so an unmap that still arrives finds
    // nothing listed and does nothing.
    for (;;) {
        nv_xnu_dmamap *m = NULL;
        IOLockLock(gDmaLock);
        for (nv_xnu_dmamap *p = gDmaHead; p; p = p->next) if (p->dev == d) { m = p; break; }
        IOLockUnlock(gDmaLock);
        if (!m) break;
        nv_xnu_dmamap *t = dmaTake(m, 0, d, ~0u);
        if (!t) continue;                                             // someone else took it meanwhile
        if (OSIncrementAtomic64(&gDmaLeaked) < 8) DLOG("DMA mapping of %llu KB (kind %u) was still live when its GPU stopped", t->bytes >> 10, t->kind);
        dmaUnmap(t);
    }
    if (!drained) {
        // A caller is still inside a mapping call on this record and may still read its mapper: keep both for good.
        DLOG("DMA: a mapping call was still running %d ms after the GPU stopped; its record is kept", kDmaDrainMs);
        IOLockLock(gDmaLock);
        d->state = kDevRetired;
        IOLockUnlock(gDmaLock);
        dmaPublishMode();
        return;
    }
    IOMapper *mp = d->mapper;
    IOLockLock(gDmaLock);
    d->mapper = NULL;
    d->state = kDevFree;
    IOLockUnlock(gDmaLock);
    if (mp) mp->release();
    dmaPublishMode();
}

// ---- the RM's own DMA interface (nv.h) ----

extern "C" {

#if DMA_RM_ROUTED
// Mode 0: false, so the RM creates no IO address space and never calls nv_dma_map_alloc (2.9).
NvBool NV_API_CALL nv_requires_dma_remap(nv_state_t *nv)
{
    return nvrm_dma_remaps((enum nvrm_dma_mode)nvrm_dma_mode_of(nv)) ? NV_TRUE : NV_FALSE;
}

NV_STATUS NV_API_CALL nv_dma_map_alloc(nv_dma_device_t *dev, NvU64 page_count, NvU64 *va_array, NvBool contig,
                                       NvBool bReadOnlyDeviceMap, void **priv)
{
    nv_dma_device *d = dmaDev(dev);
    if (!d || !nvrm_dma_remaps(d->mode)) {
        // Mode 0: the RM does not come here (nv_requires_dma_remap is false). The 2.9 answer, for a caller that does.
        if (priv) *priv = NULL;
        return NV_OK;
    }
    if (!priv || !*priv || !va_array || !page_count || page_count > (~0ULL >> PAGE_SHIFT)) {
        OSIncrementAtomic64(&gDmaBadPriv);
        return NV_ERR_INVALID_ARGUMENT;
    }
    if (!(d = dmaGet(d))) return NV_ERR_INVALID_STATE;                // the GPU is stopping
    NvU64 bytes = 0; int f32 = 0, user = 0;
    IOMemoryDescriptor *md = nv_xnu_dma_source(*priv, &bytes, &f32, &user);
    if (!md) {
        if (OSIncrementAtomic64(&gDmaBadPriv) < 8)
            DLOG("nv_dma_map_alloc(%llu pages): private data %p is not a live allocation of this driver -- refused", page_count, *priv);
        dmaPut(d);
        return NV_ERR_INVALID_STATE;
    }
    // bReadOnlyDeviceMap is not passed on: the mapping is made with the direction the descriptor already carries (a
    // read-only pin is a kIODirectionOut descriptor), as IOKit maps any descriptor.
    NV_STATUS st = NV_ERR_INVALID_ARGUMENT;
    nv_xnu_dmamap *m = NULL;
    if (page_count * PAGE_SIZE <= bytes)
        st = dmaMap(d, md, page_count * PAGE_SIZE, nvrm_dma_bits(dmaBitsNow(), f32), contig != NV_FALSE, va_array, page_count,
                    user ? kKindUser : kKindRm, &m);
    md->release();                                                    // the mapping holds its own reference
    dmaPut(d);
    if (st != NV_OK) return st;                                       // *priv is left as it came, as Linux leaves it
    if (user) OSIncrementAtomic64(&gDmaUserMaps);
    *priv = m;
    return NV_OK;
}

NV_STATUS NV_API_CALL nv_dma_unmap_alloc(nv_dma_device_t *dev, NvU64 page_count, NvU64 *va_array, void **priv)
{
    void *p = priv ? *priv : NULL;
    if (!p) return NV_OK;
    nv_dma_device *d = dmaDev(dev);
    if (!d || !nvrm_dma_remaps(d->mode)) return NV_OK;                // mode 0: nothing was ever mapped, the 2.9 answer
    nv_xnu_dmamap *m = dmaTake(p, 0, d, (1u << kKindRm) | (1u << kKindUser));
    if (!m) {
        // Not listed: already released by a stop of this GPU, or not ours. Never dereferenced.
        if (OSIncrementAtomic64(&gDmaBadPriv) < 8) DLOG("nv_dma_unmap_alloc(%llu pages): %p is not a live mapping", page_count, p);
        return NV_ERR_INVALID_ARGUMENT;
    }
    dmaUnmap(m);
    *priv = NULL;
    return NV_OK;
}
#endif

// With a mapper these would put an address on the bus that nothing mapped: a peer GPU's BAR or another device's MMIO
// has no mapping path here. Refused in every mode, as the weak stubs refuse them in 2.9, and counted.
NV_STATUS NV_API_CALL nv_dma_map_peer(nv_dma_device_t *dev, nv_dma_device_t *peer, NvU8 bar, NvU64 page_count, NvU64 *va)
{
    if (OSIncrementAtomic64(&gDmaRefused) < 4) DLOG("nv_dma_map_peer refused (no peer mapping on macOS)");
    return NV_ERR_NOT_SUPPORTED;
}
void NV_API_CALL nv_dma_unmap_peer(nv_dma_device_t *dev, NvU64 page_count, NvU64 va) { }
NV_STATUS NV_API_CALL nv_dma_map_mmio(nv_dma_device_t *dev, NvU64 page_count, NvU64 *va)
{
    if (OSIncrementAtomic64(&gDmaRefused) < 4) DLOG("nv_dma_map_mmio refused (no MMIO mapping on macOS)");
    return NV_ERR_NOT_SUPPORTED;
}
void NV_API_CALL nv_dma_unmap_mmio(nv_dma_device_t *dev, NvU64 page_count, NvU64 va) { }
// x86 DMA is cache coherent: nothing to invalidate (the Linux body is ARM-only).
void NV_API_CALL nv_dma_cache_invalidate(nv_dma_device_t *dev, void *priv) { }

// "nvSurfDma" (nvrm_surfshare_abi.h). Mode 0: QUERY answers mode 0, MAP answers NVRM_SS_OFF before anything is
// allocated (the accelerator then keeps its 2.9 page lists), UNMAP finds nothing to take.
int nvrm_dma_surf(struct NVRMSurfDma *q, nv_state_t *nv)
{
    if (!q) return 1;
    if (q->version != NVRM_SS_DMA_VERSION) { q->status = NVRM_SS_BAD_VERSION; return 0; }
    nv_dma_device *d = dmaDevOf(nv);
    q->mode = d ? (unsigned)d->mode : (unsigned)NVRM_DMA_NONE;
    q->status = NVRM_SS_OFF;
    if (q->op == NVRM_SS_DMA_QUERY) { q->status = 0; return 0; }
    if (q->op == NVRM_SS_DMA_UNMAP) {
        // Only a mapping this GPU made: a token handed to another GPU's NVRM (or made up) unmaps nothing. A GPU that
        // has stopped has no mappings left (its stop removed them), so there is nothing to look for then.
        const UInt64 serial = (UInt64)(uintptr_t)q->token;
        nv_xnu_dmamap *m = (serial && d) ? dmaTake(NULL, serial, d, 1u << kKindSurf) : NULL;
        if (m) { dmaUnmap(m); q->status = 0; }
        q->token = NULL;
        return 0;
    }
    if (q->op != NVRM_SS_DMA_MAP || !d || !nvrm_dma_remaps(d->mode) || !q->md || !q->length) return 0;
    IOMemoryDescriptor *md = OSDynamicCast(IOMemoryDescriptor, (OSObject *)q->md);
    if (!md) return 0;
    const NvU64 bytes = (q->length + PAGE_MASK) & ~(NvU64)PAGE_MASK;
    if (bytes < q->length || bytes > md->getLength()) { q->status = NVRM_SS_TOO_SMALL; return 0; }
    // The GPU is stopping: nothing mapped. NVRM_SS_NO_DMA, not NVRM_SS_OFF, because OFF tells the accelerator that
    // physical addresses are the bus addresses, which is false with a mapper.
    if (!(d = dmaGet(d))) { q->status = NVRM_SS_NO_DMA; return 0; }
    NvU64 base = 0; nv_xnu_dmamap *m = NULL;
    // One run on the bus: the caller gets the base and adds page offsets itself.
    const NV_STATUS st = dmaMap(d, md, bytes, nvrm_dma_bits(dmaBitsNow(), 0), true, &base, bytes >> PAGE_SHIFT, kKindSurf, &m);
    dmaPut(d);
    if (st != NV_OK) { q->status = NVRM_SS_NO_DMA; return 0; }
    OSIncrementAtomic64(&gDmaSurfMaps);
    q->busBase = base; q->token = (void *)(uintptr_t)m->serial; q->status = 0;
    return 0;
}

// Mode 0 answers 1 before any lock or list is touched: the page-off copy runs exactly as in 2.9.
int nvrm_dma_pages_ok(nv_state_t *nv, const NvU64 *pages, NvU64 n)
{
    nv_dma_device *d = dmaDevOf(nv);
    if (!d || d->mode == NVRM_DMA_NONE) return 1;
    int inside = 0;
    IOLock *l = gDmaLock;
    if (nvrm_dma_remaps(d->mode) && l) {
        IOLockLock(l);
        for (nv_xnu_dmamap *m = gDmaHead; m && !inside; m = m->next)
            inside = m->kind == kKindSurf && m->dev == d && m->runs == 1 && nvrm_dma_pages_inside(pages, n, m->base, m->bytes);
        IOLockUnlock(l);
    }
    const int ok = nvrm_dma_pageoff_allowed(d->mode, inside);
    if (!ok && OSIncrementAtomic64(&gDmaPageoffRefused) < 4)
        DLOG("page-off copy refused: %llu pages starting 0x%llx are not mapped through the DMA mapper", n, (pages && n) ? pages[0] : 0);
    return ok;
}

}
