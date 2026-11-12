#!/usr/bin/env python3
"""The accelerator's side of two-plane surfaces in VRAM: a plane's VRAM is granted with the plane's byte range in the
surface's memory (NVRM_SS_PLANE_RANGED), and a page-off of that plane copies it into that range and nowhere else.

What this checks:
  - on the host, with the code lifted from kexts/NVRM/accel/nvrm-accel.cpp (the staging buffer's code, nvAccelPlanesOn,
    the plane's page-off and its own staging buffer, and nvAccelPlaneGrant) run against a stand-in NVRM that answers
    "nvSurfDma" and "nvSurfPageoff" as NVRM does (in modes 1 and 2 a page list must lie in one live mapping of the same
    NVRM), stand-in buffers whose pages have physical and bus addresses, a surface and a plane's VRAM full of known
    bytes:
      * the plane's VRAM lands in the surface at the plane's offset, for exactly the plane's length, and no other byte of
        the surface changes; success sets the plane's own validity bit and clears the surface's pending mark;
      * without a mapper through the shared physically contiguous buffer or, when that one cannot serve, a buffer of the
        page-off's own listed page by page; behind a mapper through the shared mapped buffer or a buffer of its own
        mapped through the NVRM the page-off goes to, given back (UNMAP) before it is completed and released;
      * a mapping that cannot be made, a mapper the driver may not use, a range that no longer fits: nothing copied,
        nothing written, the page-off fails (there is no family page-off in the program at all: a call to it would not
        compile);
      * a copy-engine timeout abandons the buffer with its mapping (no UNMAP, not released); NVRM refusing the pages
        makes the accelerator ask for the mode again;
      * the staging lock is held across MAP, the copy and UNMAP; the surface's pages are wired across the copy and
        given back after it;
      * the grant: the range against the surface and the VRAM, a second grant that moves the range, plane 0 sharing the
        resource or the VRAM, validity words with two bits, a resource that is not a surface's; the plane-0 cache it
        looks up is given back; plane 0's grant looks up nothing else;
      * whether a grant may be made: switched off (the mode is not even asked), the locked page-off wait, behind a
        mapper only with the shared buffer mapped through this GPU's NVRM, never in mode 3;
  - on the source text: the flag is taken off the plane only for a VRAM grant; the switch is checked before any device
    cache is looked up; the plane checks come after the existing cache and VRAM checks and before the size check; a
    dirty mark uses the plane's bit and refuses a plane above 0 without a range; the page-off sends a plane to its own
    copy right after the mode is read, before the mode-3 family line; neither family fallback of the page-off's head
    takes a plane; boot-arg and property; read-only counters registered once;
  - the page-off of a resource without a range is the one of the step before (2714281) byte for byte, once the lines
    that only a plane takes are taken out.
Each group has negative controls on altered copies of the source.
   python3 tools/test_surf_planes_accel.py"""
import pathlib
import re
import subprocess
import sys
import tempfile

R = pathlib.Path(__file__).resolve().parents[1]
SRC = R / "kexts/NVRM/accel/nvrm-accel.cpp"
BEFORE = "2714281"   # the accelerator's page-off before plane ranges
FAILS = []


def arm(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (f"  [{str(detail).strip()[-700:]}]" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def strip_comments(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def git_show(rev, path):
    r = subprocess.run(["git", "-C", str(R), "show", f"{rev}:{path}"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def func(src, start, end_marker="\n}\n"):
    a = src.find(start)
    if a < 0:
        return ""
    b = src.find(end_marker, a)
    return src[a:b + len(end_marker)] if b >= 0 else ""


def staging(src):
    a = src.find("static IOLock *volatile gNVSVStageLock;")
    s = src.find("static IOBufferMemoryDescriptor *nvSVStage(", a)
    e = src.find("\n    return b;\n}\n", s)
    return src[a:e + len("\n    return b;\n}\n")] if 0 <= a < s < e else ""


def ranged(src):
    """from nvAccelPlanesOn to the end of nvSVPageoffRanged"""
    a = src.find("static bool nvAccelPlanesOn(NVAccel *a)\n{")
    s = src.find("static void nvSVPageoffRanged(", a)
    e = src.find("\n    if (out) *out = ok;\n}\n", s)
    return src[a:e + len("\n    if (out) *out = ok;\n}\n")] if 0 <= a < s < e else ""


def grant(src):
    return func(src, "static unsigned nvAccelPlaneGrant(NVAccel *a, void *surf, NVResource *res, NVVidMemory *vm, struct NVRMSurfShareEsc *p)\n{")


HARNESS = r'''
#include <cassert>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <map>
#include <string>
#include <vector>
typedef int IOReturn;
typedef unsigned IOOptionBits;
typedef unsigned long vm_size_t;
typedef unsigned long long IOByteCount;
typedef unsigned long long addr64_t;
typedef long long SInt64;
typedef unsigned UInt32;
enum : int { kIOReturnSuccess = 0, kIOReturnNoMemory = (int)0xe00002bd, kIOReturnUnsupported = (int)0xe00002c7,
             kIOReturnNotAttached = (int)0xe00002d9 };
static const unsigned kIODirectionNone = 0, kIODirectionInOut = 3, kIOMemoryPhysicallyContiguous = 0x10, kIOMemoryMapperNone = 0x800;
#define PAGE_SIZE 4096
static std::vector<std::string> T;
static void tr(const char *f, ...) __attribute__((format(printf, 1, 2)));
static void tr(const char *f, ...) { char b[400]; va_list ap; va_start(ap, f); vsnprintf(b, sizeof b, f, ap); va_end(ap); T.push_back(b); }
#define ALOG(fmt, ...) tr("log " fmt, ##__VA_ARGS__)
static SInt64 OSAddAtomic64(SInt64 v, volatile SInt64 *p) { SInt64 o = *p; *p += v; return o; }
static UInt32 OSBitOrAtomic(UInt32 m, volatile UInt32 *p) { UInt32 o = *p; *p |= m; return o; }
struct IOLock { int held; int takes; };
static IOLock *IOLockAlloc() { return new IOLock(); }
static void IOLockLock(IOLock *l) { assert(!l->held); l->held = 1; l->takes++; }
static void IOLockUnlock(IOLock *l) { assert(l->held); l->held = 0; }
static void IOLockFree(IOLock *l) { delete l; }
static bool OSCompareAndSwapPtr(void *o, void *n, void *volatile *p) { if (*p != o) return false; *p = n; return true; }
static void nvSVZero(void *p, unsigned long n) { memset(p, 0, n); }
static int gMallocs;
static void *IOMalloc(unsigned long n) { gMallocs++; return malloc(n); }
static void IOFree(void *p, unsigned long) { gMallocs--; free(p); }
struct OSSymbol {
    const char *name; static int live;
    static const OSSymbol *withCStringNoCopy(const char *s) { live++; return new OSSymbol{ s }; }
    void release() const { live--; delete this; }
};
int OSSymbol::live = 0;
// ---- objects with a kind, so that OSDynamicCast can be what it is in the kernel: a checked cast ----------------------
struct OSObject { int kind = 0; int rc = 1; void retain() { rc++; } void release() { assert(rc > 0); rc--; } };
#define OSDynamicCast(T, o) ((o) && ((OSObject *)(o))->kind == T::kKind ? (T *)(o) : (T *)nullptr)
// ---- memory: the surface's descriptor and the staging buffers, with physical and bus addresses ------------------------
struct IOMemoryDescriptor : OSObject {
    std::vector<unsigned char> mem; int prepared = 0, prepares = 0, writes = 0;
    virtual ~IOMemoryDescriptor() {}
    virtual IOByteCount getLength() const { return mem.size(); }
    virtual IOReturn prepare(unsigned = 0) { prepared++; prepares++; return 0; }
    virtual IOReturn complete(unsigned = 0) { assert(prepared > 0); prepared--; return 0; }
    IOByteCount writeBytes(IOByteCount off, const void *src, IOByteCount len) {
        assert(prepared > 0); writes++;
        if (off > mem.size()) return 0;
        const IOByteCount n = len < mem.size() - off ? len : mem.size() - off;
        memcpy(mem.data() + off, src, n); tr("surface write at %llu len %llu", off, n); return n;
    }
};
static int gNextBuf = 1, gFailAlloc = 0;
struct IOBufferMemoryDescriptor;
static std::map<int, IOBufferMemoryDescriptor *> gBufs;
struct IOBufferMemoryDescriptor : IOMemoryDescriptor {
    int id; unsigned opts; bool dead = false;
    static IOBufferMemoryDescriptor *withOptions(IOOptionBits o, vm_size_t size, vm_size_t align) {
        tr("alloc opts %#x size %lu align %lu%s", o, (unsigned long)size, (unsigned long)align, gFailAlloc ? " -> fails" : "");
        if (gFailAlloc) { gFailAlloc--; return nullptr; }
        IOBufferMemoryDescriptor *b = new IOBufferMemoryDescriptor(); b->id = gNextBuf++; b->opts = o; b->mem.assign(size, 0xEE);
        gBufs[b->id] = b; return b;
    }
    IOByteCount getLength() const override { assert(!dead); return mem.size(); }
    IOReturn prepare(unsigned = 0) override { assert(!dead); tr("prepare %d", id); prepared++; return 0; }
    IOReturn complete(unsigned = 0) override { assert(!dead && prepared > 0); tr("complete %d", id); prepared--; return 0; }
    void release() { assert(!dead); tr("release %d", id); dead = true; }
    void *getBytesNoCopy() { assert(!dead); return mem.data(); }
    // physical: a contiguous buffer is one run; any other buffer has its pages scattered (a gap after each)
    addr64_t phys(IOByteCount off) const { return (opts & kIOMemoryPhysicallyContiguous) ? base() + off : base() + (off / 4096) * 3 * 4096 + off % 4096; }
    addr64_t base() const { return 0x100000000000ull * (unsigned long long)id; }
    addr64_t getPhysicalSegment(IOByteCount off, IOByteCount *len, IOOptionBits o) {
        assert(!dead && prepared > 0 && o == kIOMemoryMapperNone);
        if (off >= mem.size()) { *len = 0; return 0; }
        *len = (opts & kIOMemoryPhysicallyContiguous) ? mem.size() - off : 4096 - off % 4096;
        return phys(off);
    }
};
// the byte a physical address names, in mode 0
static unsigned char *physByte(addr64_t a) {
    const int id = (int)(a / 0x100000000000ull); auto it = gBufs.find(id);
    if (it == gBufs.end() || it->second->dead || !it->second->prepared) return nullptr;
    IOBufferMemoryDescriptor *b = it->second; const addr64_t rel = a - b->base();
    IOByteCount off;
    if (b->opts & kIOMemoryPhysicallyContiguous) off = rel;
    else { if ((rel / 4096) % 3) return nullptr; off = (rel / 4096 / 3) * 4096 + rel % 4096; }
    return off < b->mem.size() ? b->mem.data() + off : nullptr;
}
// ---- the plane's VRAM and NVRM ----------------------------------------------------------------------------------------
struct NVVidMemory : OSObject { enum { kKind = 2 }; void *fRmHandle; unsigned long long fBytes; bool fScanout = false; std::vector<unsigned char> data; };
struct NVRMSurfDma;
static IOLock *volatile *gStageLockPtr;
struct IOService : OSObject {
    explicit IOService(const char *n) : name(n) {}
    const char *name; int mode = 0; unsigned pageoffStatus = 0; bool mapFails = false;
    unsigned long long nextSerial = 1, nextBus = 0x10000000ull;
    struct Map { IOBufferMemoryDescriptor *b; unsigned long long bus, len; };
    std::map<unsigned long long, Map> live;
    int queries = 0, maps = 0, unmaps = 0, pageoffs = 0, unlockedCalls = 0;
    IOReturn callPlatformFunction(const OSSymbol *sym, bool, void *p1, void *, void *, void *);
};
static std::map<void *, NVVidMemory *> gVram;
#define NVRM_SS_KERNEL_ABI 1
#include "nvrm_surfshare_abi.h"
#include "nvaccel_plane_range.h"
#include "nvaccel_pageoff_recheck.h"
IOReturn IOService::callPlatformFunction(const OSSymbol *sym, bool, void *p1, void *, void *, void *)
{
    const bool unlocked = !*gStageLockPtr || !(*gStageLockPtr)->held;
    if (!strcmp(sym->name, NVRM_SS_FN_PAGEOFF)) {
        if (unlocked) unlockedCalls++;
        NVRMSurfPageoff *q = (NVRMSurfPageoff *)p1; pageoffs++;
        assert(q->version == NVRM_SS_RM_VERSION);
        NVVidMemory *vm = gVram[q->kapiMemory]; assert(vm);
        assert(q->length <= vm->fBytes && q->pageCount == (q->length + 4095) / 4096);
        std::vector<unsigned char *> dst;
        for (unsigned long long k = 0; k < q->pageCount; k++) {
            const unsigned long long a = q->pages[k]; unsigned char *p = nullptr;
            if (a & 4095) { q->status = NVRM_SS_NO_DMA; tr("%s: PAGEOFF page %llu not aligned", name, k); return 0; }
            if (mode == 1 || mode == 2) {      // nvrm_dma_pages_ok: every page inside ONE live mapping of THIS NVRM
                for (auto &m : live) if (a >= m.second.bus && a + 4096 <= m.second.bus + m.second.len) {
                    if (!dst.empty() && k && !(q->pages[0] >= m.second.bus && q->pages[0] < m.second.bus + m.second.len)) break;
                    p = m.second.b->mem.data() + (a - m.second.bus); }
            } else p = physByte(a);
            if (!p) { q->status = NVRM_SS_NO_DMA; tr("%s: PAGEOFF page %llu (%#llx) refused", name, k, a); return 0; }
            dst.push_back(p);
        }
        q->status = pageoffStatus; q->usec = 7;
        tr("%s: PAGEOFF %llu pages len %llu -> %#x", name, (unsigned long long)q->pageCount, (unsigned long long)q->length, pageoffStatus);
        if (pageoffStatus == 0)
            for (unsigned long long i = 0; i < q->length; i++) dst[i / 4096][i % 4096] = vm->data[i];
        return 0;
    }
    assert(!strcmp(sym->name, NVRM_SS_FN_DMA));
    NVRMSurfDma *q = (NVRMSurfDma *)p1;
    assert(q->version == NVRM_SS_DMA_VERSION);
    q->mode = (unsigned)mode; q->status = NVRM_SS_OFF;
    if (q->op != NVRM_SS_DMA_QUERY && unlocked) unlockedCalls++;     // MAP and UNMAP under the staging lock
    if (q->op == NVRM_SS_DMA_QUERY) { queries++; q->status = 0; tr("%s: QUERY -> mode %d", name, mode); return 0; }
    if (q->op == NVRM_SS_DMA_UNMAP) {
        unmaps++;
        auto it = live.find((unsigned long long)(uintptr_t)q->token);
        if (it != live.end()) {
            assert(!it->second.b->dead && it->second.b->prepared > 0);   // given back while the buffer is alive and wired
            tr("%s: UNMAP %llu (buffer %d)", name, it->first, it->second.b->id); live.erase(it); q->status = 0;
        } else tr("%s: UNMAP not live", name);
        q->token = nullptr; return 0;
    }
    assert(q->op == NVRM_SS_DMA_MAP);
    if (mode != 1 && mode != 2) return 0;
    IOBufferMemoryDescriptor *md = (IOBufferMemoryDescriptor *)q->md;
    assert(md && !md->dead && md->prepared > 0 && q->length && q->length <= md->getLength());
    maps++;
    if (mapFails) { q->status = NVRM_SS_NO_DMA; tr("%s: MAP buffer %d -> refused", name, md->id); return 0; }
    const unsigned long long s = nextSerial++;
    q->token = (void *)(uintptr_t)s; q->busBase = nextBus; nextBus += 0x10000000ull; q->status = 0;
    live[s] = Map{ md, q->busBase, (q->length + 4095) & ~4095ull };
    tr("%s: MAP buffer %d len %llu -> token %llu", name, md->id, (unsigned long long)q->length, s);
    return 0;
}
struct NVAccel { unsigned fDmaAnswer = 0; IOService *fNVRM = nullptr; IOService *locateNVRM() { return fNVRM; } static NVAccel *resolve(NVAccel *o) { return o; } };
static IOService *nvAccelNVRM(NVAccel *a) { return a ? a->fNVRM : nullptr; }
struct NVResource : OSObject {
    enum { kKind = 1 };
    unsigned char body[0x100];
    NVAccel *fAccel = nullptr;
    nvaccel_pageoff_copies fPoCopies = { 0, 0 };
    unsigned long long fRangeOff = 0, fRangeLen = 0;
    unsigned short fValidBit = 0;
    unsigned fRangeSid = 0;
};
template <class X> static X &raw(void *o, unsigned off) { return *(X *)((unsigned char *)o + off); }
static NVVidMemory *nvres_vidmem(NVResource *r) { return r ? OSDynamicCast(NVVidMemory, raw<OSObject *>(r, 0x88)) : nullptr; }
struct NVRMRmHandles { void *kapiDevice; };
struct Surf : OSObject { unsigned id; IOMemoryDescriptor *md; };
static unsigned _ZNK9IOSurface12getSurfaceIDEv(void *s) { return ((Surf *)s)->id; }
static void *_ZNK9IOSurface19getMemoryDescriptorEv(void *s) { return ((Surf *)s)->md; }
struct Cache : OSObject { unsigned char body[0x40]; };
static std::map<unsigned, Cache *> gCacheOfPlane;      // what deviceCacheForIOSurface answers for each plane (retained)
static int gCacheLookups;
static void *_ZN22IOGraphicsAccelerator223deviceCacheForIOSurfaceEP9IOSurfacej(void *, void *, unsigned plane) {
    gCacheLookups++; Cache *c = gCacheOfPlane.count(plane) ? gCacheOfPlane[plane] : nullptr; if (c) c->retain(); return c; }
static unsigned gPendCleared;
static bool nvSVPend(unsigned id, int op, NVResource * = nullptr) { if (op == 0) { gPendCleared = id; } return true; }
static int gNvAccelDmaMode = -1;
static SInt64 gCntDmaStaged, gCntDmaMaps, gCntDmaMapFail, gCntDmaUnmaps, gCntDmaAbandoned, gCntDmaRefused, gCntDmaFamily, gCntDmaHybrid;
static unsigned gNVSVPageoff, gNVSVPageoffFail;
static int gSurfPlanes = 1, gPoUnlocked = 1, gSurfPlaneBits;
static unsigned gSurfPlaneLogs;
static SInt64 gCntSvRanged, gCntSvRangedOff, gCntSvRangedSmall, gCntSvRangedRange, gCntSvRangedShared, gCntSvRangedBit;
static SInt64 gCntPoRanged, gCntPoRangedFail, gCntPoRangedTemp;
@@STAGING@@
@@GRANT@@
@@RANGED@@
// ---- the world of one page-off ----------------------------------------------------------------------------------------
static IOService A("A"), B("B");
static NVAccel ACC;
static NVResource *RES; static NVVidMemory VM; static Surf SURF; static IOMemoryDescriptor MD; static unsigned short VALID[8];
static const unsigned long long OFF = 6000, LEN = 9000;    // a plane that starts and ends inside pages
static void world(int mode, unsigned bit = 2)
{
    if (gNVSVStageLock) { assert(!gNVSVStageLock->held); }
    nvSVStageFree(); gNVSVStageBackoff = 0; A.live.clear(); B.live.clear();
    A.mode = mode; A.pageoffStatus = 0; A.mapFails = false; A.queries = A.maps = A.unmaps = A.pageoffs = A.unlockedCalls = 0;
    B.mode = mode;
    ACC = NVAccel(); ACC.fNVRM = &A;
    RES = new NVResource(); RES->fAccel = &ACC; RES->fRangeOff = OFF; RES->fRangeLen = LEN; RES->fValidBit = bit; RES->fRangeSid = 42;
    memset(VALID, 0, sizeof VALID); raw<unsigned short *>(RES, 0x30) = VALID; raw<unsigned char>(RES, 0x14) = 0xc0;
    VM.kind = 2; VM.fScanout = false; VM.fRmHandle = (void *)0x77; VM.fBytes = 16384; VM.data.resize(16384);
    for (unsigned i = 0; i < 16384; i++) VM.data[i] = (unsigned char)(i * 7 + 3);
    gVram[VM.fRmHandle] = &VM; raw<OSObject *>(RES, 0x88) = &VM;
    MD.mem.assign(32768, 0x5A); MD.prepared = 0; MD.prepares = 0; MD.writes = 0;
    SURF.id = 42; SURF.md = &MD; gPendCleared = 0; T.clear();
    gNVSVPageoff = gNVSVPageoffFail = 0; gCntPoRanged = gCntPoRangedFail = gCntPoRangedTemp = 0;
}
// the surface holds the plane's VRAM at OFF for LEN and its old bytes everywhere else
static bool landed()
{
    for (unsigned long long i = 0; i < MD.mem.size(); i++) {
        const unsigned char want = (i >= OFF && i < OFF + LEN) ? VM.data[i - OFF] : 0x5A;
        if (MD.mem[i] != want) { tr("byte %llu is %#x, not %#x", i, MD.mem[i], want); return false; }
    }
    return true;
}
static bool untouched() { for (unsigned char c : MD.mem) if (c != 0x5A) return false; return MD.writes == 0; }
static bool run(unsigned mode, bool *outp)
{
    struct NVRMRmHandles h = { (void *)1 };
    bool out = true;
    nvSVPageoffRanged(RES, &MD, &SURF, &VM, &h, nvAccelDmaMode(&ACC), &out);
    *outp = out;
    (void)mode;
    return !gNVSVStageLock || !gNVSVStageLock->held;
}
static int failed;
static void check(const char *n, bool ok)
{
    printf("%s %s\n", ok ? "ok  " : "FAIL", n);
    if (!ok) { failed++; for (auto &l : T) printf("     | %s\n", l.c_str()); }
}
static bool traceOrder(const char *a, const char *b)
{
    int ia = -1, ib = -1;
    for (int i = 0; i < (int)T.size(); i++) { if (ia < 0 && T[i].find(a) != std::string::npos) ia = i; if (T[i].find(b) != std::string::npos) ib = i; }
    return ia >= 0 && ib > ia;
}
static bool traceHas(const char *a) { for (auto &l : T) if (l.find(a) != std::string::npos) return true; return false; }
int main()
{
    gStageLockPtr = &gNVSVStageLock;
    bool out = false;
    // ---- the page-off of a plane ----
    world(0); { const bool unl = run(0, &out);
        check("mode 0, shared buffer: the plane's VRAM lands at its offset, for its length, nothing else written; its bit set, the mark cleared",
              unl && out && landed() && (VALID[0] & 2) && !(VALID[0] & 1) && gPendCleared == 42 && MD.prepared == 0 && MD.prepares == 1
              && RES->fPoCopies.started == 1 && RES->fPoCopies.finished == 1 && gCntPoRanged == 1 && gNVSVPageoff == 1 && !gCntPoRangedTemp
              && A.pageoffs == 1 && A.unlockedCalls == 0 && gNVSVStageBuf && (gNVSVStageBuf->opts & kIOMemoryPhysicallyContiguous) && gMallocs == 0); }
    world(0); gNVSVStageBackoff = 5; { const bool unl = run(0, &out);
        check("mode 0, the shared buffer cannot serve: a buffer of its own, listed page by page, completed and released after the copy",
              unl && out && landed() && gCntPoRangedTemp == 1 && A.pageoffs == 1 && gNVSVStageBackoff == 4 && traceOrder("PAGEOFF", "complete")
              && traceOrder("complete", "release") && !gNVSVStageBuf && gMallocs == 0); }
    world(1); { const bool unl = run(1, &out);
        check("mode 1, shared buffer: mapped through the NVRM the page-off goes to, the copy through its bus addresses",
              unl && out && landed() && A.maps == 1 && A.live.size() == 1 && A.pageoffs == 1 && A.unlockedCalls == 0 && gNVSVStageTok && B.maps == 0); }
    world(2); gNVSVStageBackoff = 5; { const bool unl = run(2, &out);
        check("mode 2, the shared buffer cannot serve: its own buffer is mapped through the same NVRM, used, given back (UNMAP) before it is completed and released",
              unl && out && landed() && gCntPoRangedTemp == 1 && A.maps == 1 && A.unmaps == 1 && A.live.empty() && traceOrder("MAP buffer", "PAGEOFF")
              && traceOrder("PAGEOFF", "UNMAP") && traceOrder("UNMAP", "complete") && traceOrder("complete", "release") && A.unlockedCalls == 0 && gMallocs == 0); }
    world(1); gNVSVStageBackoff = 5; A.mapFails = true; { const bool unl = run(1, &out);
        check("mode 1, no buffer can be mapped: nothing copied or written, the page-off fails, no physical page is ever offered",
              unl && !out && untouched() && A.pageoffs == 0 && gCntPoRangedFail == 1 && gNVSVPageoffFail == 1 && !(VALID[0] & 2) && MD.prepared == 0
              && RES->fPoCopies.finished == 0 && gMallocs == 0 && traceHas("no staging buffer could be mapped")); }
    world(3); { const bool unl = run(3, &out);
        check("mode 3: nothing copied, nothing wired, no copy counted, the page-off fails",
              unl && !out && untouched() && A.pageoffs == 0 && MD.prepares == 0 && RES->fPoCopies.started == 0 && gCntPoRangedFail == 1); }
    world(0); VM.fBytes = 4096; { const bool unl = run(0, &out);
        check("a range that no longer fits the VRAM: nothing copied or written, the page-off fails",
              unl && !out && untouched() && A.pageoffs == 0 && MD.prepares == 0 && gCntPoRangedFail == 1); }
    world(0); MD.mem.assign(OFF + LEN - 1, 0x5A); { const bool unl = run(0, &out);
        check("a range that no longer fits the surface: nothing copied or written", unl && !out && MD.writes == 0 && A.pageoffs == 0); }
    world(1); gNVSVStageBackoff = 5; A.pageoffStatus = 0x65; { const bool unl = run(1, &out);
        IOBufferMemoryDescriptor *own = gBufs.rbegin()->second;
        check("a copy-engine timeout into its own buffer: the buffer is abandoned wired and mapped (no UNMAP, not released), nothing written",
              unl && !out && untouched() && A.unmaps == 0 && A.live.size() == 1 && !own->dead && own->prepared == 1 && gCntDmaAbandoned >= 1
              && MD.prepared == 0 && gMallocs == 0); }
    world(1); A.pageoffStatus = 0x65; { const SInt64 ab = gCntDmaAbandoned; const bool unl = run(1, &out);
        check("a copy-engine timeout into the shared buffer: it is abandoned with its mapping, as for any page-off",
              unl && !out && untouched() && A.unmaps == 0 && !gNVSVStageBuf && !gNVSVStageTok && gCntDmaAbandoned == ab + 1); }
    world(1); { bool o1; run(1, &o1); A.pageoffStatus = NVRM_SS_NO_DMA; MD.mem.assign(32768, 0x5A); MD.writes = 0; const bool unl = run(1, &out);
        check("NVRM refuses the pages: nothing written, the mode is asked again, the shared buffer's mapping given back",
              o1 && unl && !out && untouched() && ACC.fDmaAnswer == 0 && A.unmaps == 1 && !gNVSVStageBuf && gCntDmaRefused >= 1); }
    world(0, 1); { const bool unl = run(0, &out);
        check("a plane whose grant recorded bit 0 sets bit 0", unl && out && landed() && VALID[0] == 1); }
    world(0); SURF.id = 43; { const bool unl = run(0, &out);
        check("the resource now belongs to another surface than its range: nothing copied, wired or counted, the page-off fails",
              unl && !out && untouched() && A.pageoffs == 0 && MD.prepares == 0 && RES->fPoCopies.started == 0 && gCntPoRangedFail == 1
              && !(VALID[0] & 2)); }
    SURF.id = 42;
    // ---- whether a grant may be made ----
    world(0); gSurfPlanes = 0; { const int q = A.queries; NVAccel fresh; fresh.fNVRM = &A; const SInt64 off = gCntSvRangedOff;
        check("switched off: refused and counted, and the mode is not even asked", !nvAccelPlanesOn(&fresh) && A.queries == q && gCntSvRangedOff == off + 1); }
    gSurfPlanes = 1;
    world(0); gPoUnlocked = 0; { NVAccel fresh; fresh.fNVRM = &A;
        check("the page-off waits with the accelerator lock held: refused, the mode not asked", !nvAccelPlanesOn(&fresh) && A.queries == 0); }
    gPoUnlocked = 1;
    world(0); { NVAccel fresh; fresh.fNVRM = &A; check("no mapper: allowed", nvAccelPlanesOn(&fresh)); }
    world(1); { NVAccel fresh; fresh.fNVRM = &A;
        const bool before = nvAccelPlanesOn(&fresh);
        bool o; run(1, &o);                                     // a page-off maps the shared buffer through A
        NVAccel other; other.fNVRM = &B; B.mode = 1;
        check("behind a mapper: refused until the shared buffer is mapped through this GPU's NVRM, then allowed; another GPU's mapping does not count",
              !before && o && nvAccelPlanesOn(&fresh) && !nvAccelPlanesOn(&other)); }
    world(3); { NVAccel fresh; fresh.fNVRM = &A; check("a mapper the driver may not use: refused", !nvAccelPlanesOn(&fresh)); }
    // ---- the grant ----
    {   static NVResource r0, r1; static NVVidMemory v0, v1; static Cache c0, c1;
        auto reset = [&]() {
            r0 = NVResource(); r1 = NVResource(); r0.kind = r1.kind = 1; v0 = NVVidMemory(); v1 = NVVidMemory(); v0.kind = v1.kind = 2;
            v0.fBytes = 2073600; v1.fBytes = 1036800; v0.fRmHandle = (void *)1; v1.fRmHandle = (void *)2;
            raw<unsigned char>(&r0, 0x14) = 0xc0; raw<unsigned char>(&r1, 0x14) = 0xc0;
            static unsigned short w0[8], w1[8]; memset(w0, 0, sizeof w0); memset(w1, 0, sizeof w1);
            raw<unsigned short *>(&r0, 0x30) = w0; raw<unsigned short *>(&r1, 0x30) = w1;
            raw<OSObject *>(&r0, 0x88) = &v0; raw<OSObject *>(&r1, 0x88) = &v1;
            c0 = Cache(); c1 = Cache(); raw<OSObject *>(&c0, 0x20) = &r0; raw<OSObject *>(&c1, 0x20) = &r1;
            gCacheOfPlane.clear(); gCacheOfPlane[0] = &c0; gCacheOfPlane[1] = &c1; gCacheLookups = 0;
            MD.mem.assign(2073600 + 1036800, 0); SURF.md = &MD;
            gCntSvRanged = gCntSvRangedSmall = gCntSvRangedRange = gCntSvRangedShared = gCntSvRangedBit = 0; gSurfPlaneBits = 0;
        };
        auto ask = [&](unsigned plane, unsigned long long off, unsigned long long len, NVResource *res, NVVidMemory *vm) {
            struct NVRMSurfShareEsc p; memset(&p, 0, sizeof p); p.surfaceID = 42; p.plane = plane; p.size = (off << 32) | len;
            const unsigned st = nvAccelPlaneGrant(&ACC, &SURF, res, vm, &p);
            return std::make_pair(st, p.size);
        };
        reset(); raw<unsigned short *>(&r1, 0x30)[6] = 2;
        auto g = ask(1, 2073600, 1036800, &r1, &v1);
        check("plane 1 with its own resource and VRAM: granted; range and bit recorded; size is the plane's length; the plane-0 cache given back",
              g.first == 0 && g.second == 1036800 && r1.fRangeOff == 2073600 && r1.fRangeLen == 1036800 && r1.fValidBit == 2 && gSurfPlaneBits == 2
              && gCntSvRanged == 1 && gCacheLookups == 1 && c0.rc == 1);
        g = ask(1, 2073600, 1036800, &r1, &v1);
        check("the same range again: granted", g.first == 0 && gCntSvRanged == 2);
        g = ask(1, 2073600 - 64, 1036800, &r1, &v1);
        check("another range for the same resource: refused, the recorded range kept", g.first == NVRM_SS_BAD_RANGE && r1.fRangeOff == 2073600 && gCntSvRangedRange == 1);
        reset(); g = ask(0, 0, 2073600, &r0, &v0);
        check("plane 0: granted, no other cache looked up, bit 0 for a fresh resource", g.first == 0 && gCacheLookups == 0 && r0.fValidBit == 1 && gSurfPlaneBits == 0);
        reset(); gCacheOfPlane[0] = &c1;
        g = ask(1, 2073600, 1036800, &r1, &v1);
        check("plane 0 has the same resource: refused as shared, nothing recorded, the cache given back",
              g.first == NVRM_SS_BAD_RANGE && gCntSvRangedShared == 1 && !r1.fRangeLen && c1.rc == 1);
        reset(); raw<OSObject *>(&r0, 0x88) = &v1;
        g = ask(1, 2073600, 1036800, &r1, &v1);
        check("plane 0 has the same VRAM: refused as shared", g.first == NVRM_SS_BAD_RANGE && gCntSvRangedShared == 1 && !r1.fRangeLen);
        reset(); g = ask(1, 2073601, 1036800, &r1, &v1);
        check("a range that runs past the surface: refused, nothing looked up", g.first == NVRM_SS_BAD_RANGE && gCntSvRangedRange == 1 && gCacheLookups == 0 && !r1.fRangeLen);
        reset(); g = ask(1, 0xffffffffull, 0xffffffffull, &r1, &v1);
        check("the largest offset and length: refused, no wrap", g.first != 0 && !r1.fRangeLen);
        reset(); g = ask(1, 0, 1036801, &r1, &v1);
        check("a plane larger than its VRAM: too small", g.first == NVRM_SS_TOO_SMALL && gCntSvRangedSmall == 1 && !r1.fRangeLen);
        reset(); g = ask(1, 0, 0, &r1, &v1);
        check("an empty plane: too small", g.first == NVRM_SS_TOO_SMALL && !r1.fRangeLen);
        reset(); raw<unsigned short *>(&r1, 0x30)[0] = 1; raw<unsigned short *>(&r1, 0x30)[6] = 2;
        g = ask(1, 2073600, 1036800, &r1, &v1);
        check("validity words with two bits: refused, nothing recorded", g.first == NVRM_SS_BAD_RANGE && gCntSvRangedBit == 1 && !r1.fRangeLen && c0.rc == 1);
        reset(); raw<unsigned char>(&r1, 0x14) = 0x40;
        g = ask(1, 2073600, 1036800, &r1, &v1);
        check("a resource that is not a surface's: no cache", g.first == NVRM_SS_NO_CACHE && !r1.fRangeLen);
        reset(); g = ask(1, 2073600, 1036800, &r1, &v1); raw<unsigned short *>(&r1, 0x30)[6] = 2;
        const auto g2 = ask(1, 2073600, 1036800, &r1, &v1);
        check("a later grant whose words name another bit than the recorded one: refused", g.first == 0 && r1.fValidBit == 1 && g2.first == NVRM_SS_BAD_RANGE && gCntSvRangedBit == 1);
        reset(); raw<unsigned short *>(&r0, 0x30) = raw<unsigned short *>(&r1, 0x30);
        g = ask(1, 2073600, 1036800, &r1, &v1);
        check("plane 0 has the same validity words: refused as shared, nothing recorded, the cache given back",
              g.first == NVRM_SS_BAD_RANGE && gCntSvRangedShared == 1 && !r1.fRangeLen && c0.rc == 1);
        reset(); v1.fScanout = true;
        g = ask(1, 2073600, 1036800, &r1, &v1);
        check("scanout VRAM: refused, nothing recorded, nothing looked up", g.first == NVRM_SS_NO_VRAM && !r1.fRangeLen && gCacheLookups == 0);
        reset(); g = ask(1, 2073600, 1036800, &r1, &v1); SURF.id = 77;
        const auto g3 = ask(1, 2073600, 1036800, &r1, &v1); SURF.id = 42;
        check("the surface is recorded with the range; the same range for another surface is refused",
              g.first == 0 && r1.fRangeSid == 42 && g3.first == NVRM_SS_BAD_RANGE && gCntSvRangedRange == 1 && r1.fRangeSid == 42);
    }
    nvSVStageFree();
    check("every symbol given back", OSSymbol::live == 0);
    return failed ? 1 : 0;
}
'''


def build_run(d, name, st, gr, rg):
    src = HARNESS.replace("@@STAGING@@", st).replace("@@GRANT@@", gr).replace("@@RANGED@@", rg)
    (d / f"{name}.cpp").write_text(src)
    b = subprocess.run(["c++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-Wno-unused-function", "-Wno-unused-parameter",
                        "-Wno-unused-variable", "-Wno-unused-but-set-variable", "-Wno-format-extra-args", "-Wno-class-memaccess",
                        f"-I{R / 'kexts/NVRM'}", f"-I{R / 'kexts/NVRM/accel'}", str(d / f"{name}.cpp"), "-o", str(d / name)],
                       capture_output=True, text=True)
    if b.returncode != 0:
        return None, b.stderr[-1500:]
    try:
        r = subprocess.run([str(d / name)], capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return -1, "did not finish within 60 s"
    return r.returncode, r.stdout + r.stderr[-800:]


def pageoff(src):
    return func(src, "void NVResource::pageoff(IOAccelEvent *ev, bool a1, bool *out, unsigned long long a3)\n{")


PLANE_LINES = [
    '        if (fRangeLen) { nvSVRangedFail(this, out, NVRM_SS_NO_VRAM, "no surface, VRAM or RM handles"); return; }   // never the family\'s copy\n',
    '                if (fRangeLen) { nvSVRangedFail(this, out, NVRM_SS_NO_VRAM, "no surface, VRAM or RM handles after the wait"); return; }\n',
    "    if (fRangeLen) { nvSVPageoffRanged(this, md, surf, vm, &h, dmaMode, out); return; }\n",
]


def unranged(po):
    """the page-off as a resource without a range runs it: the plane-only lines out, the validity bit as it is then (1)"""
    for l in PLANE_LINES:
        po = po.replace(l, "")
    po = po.replace("(v0[0] & validBit())", "(v0[0] & 1u)").replace("(v1[0] & validBit())", "(v1[0] & 1u)")
    # the one statement split over lines and the comment widened for the bit
    po = po.replace("            if (!surf || !vm || !vm->fRmHandle || vm->fScanout || !vm->fBytes || !nvAccelRmHandles(fAccel, &h)) {\n"
                    "                IOAccelResource2::pageoff(ev, a1, out, a3); return;\n            }",
                    "            if (!surf || !vm || !vm->fRmHandle || vm->fScanout || !vm->fBytes || !nvAccelRmHandles(fAccel, &h)) { IOAccelResource2::pageoff(ev, a1, out, a3); return; }")
    return norm(po)


def norm(text):
    """comments out, trailing blanks and empty lines out: what the compiler reads"""
    lines = [l.rstrip() for l in strip_comments(text).splitlines()]
    return "\n".join(l for l in lines if l.strip())


def text_problems(src):
    out = []
    s = strip_comments(src)
    sv = func(s, "static IOReturn nvAccelSurfVram(NVAccel *owner, struct NVRMSurfShareEsc *p, task_t task, unsigned cmd)\n{")
    rg = sv.find("const bool ranged = cmd == NVRM_ESC_SURF_VRAM && (p->plane & NVRM_SS_PLANE_RANGED);")
    mask = sv.find("p->plane &= ~(NVRM_SS_PLANE_TRYLOCK | NVRM_SS_PLANE_GPU_MARK | (ranged ? NVRM_SS_PLANE_RANGED : 0u));")
    cap = sv.find("if (p->plane > 15)")
    k1 = sv.find("if (ranged && !nvAccelPlanesOn(a)) { p->status = NVRM_SS_OFF; return kIOReturnSuccess; }")
    look = sv.find("_ZN22IOGraphicsAccelerator223deviceCacheForIOSurfaceEP9IOSurfacej((void *)a, surf, p->plane)")
    if not (0 <= rg < mask < cap < k1 < look):
        out.append("the flag is not taken off a VRAM grant's plane only, or the switch is not checked before the cache lookup")
    nc = sv.find("if (!cache || !res) p->status = NVRM_SS_NO_CACHE;")
    nv = sv.find("else if (!vm || !vm->fRmHandle || !vm->fBytes) p->status = NVRM_SS_NO_VRAM;")
    pg = sv.find("else if (ranged && (p->status = nvAccelPlaneGrant(a, surf, res, vm, p)) != 0) {}")
    sz = sv.find("else if (p->size > vm->fBytes) p->status = NVRM_SS_TOO_SMALL;")
    if not (look < nc < nv < pg < sz) or sv.count("nvAccelPlaneGrant(") != 1:
        out.append("the plane checks are not between the VRAM check and the size check")
    d = func(s, "static unsigned nvAccelSurfDirty(void *cache, NVResource *res, unsigned plane)\n{")
    if ("const unsigned short bit = nvaccel_dirty_bit(res->fRangeLen, res->fValidBit, plane);" not in d
            or "if (!bit) return NVRM_SS_NO_CACHE;" not in d or "valid[6] |= bit;" not in d
            or "valid[0] &= (unsigned short)~bit;" not in d or "1u << plane" in d
            or d.find("if (!bit) return NVRM_SS_NO_CACHE;") > d.find("_ZN20IOSurfaceDeviceCache10setCurrentEv(cache);")):
        out.append("the dirty mark does not use the plane's bit, or marks before it refuses a plane without a range")
    po = pageoff(s)
    mode = po.find("const unsigned dmaMode = nvAccelDmaMode(fAccel);")
    br = po.find("if (fRangeLen) { nvSVPageoffRanged(this, md, surf, vm, &h, dmaMode, out); return; }")
    fam3 = po.find("if (dmaMode >= NVRM_DMA_REFUSE) {")
    if not (0 <= mode < br < fam3) or po.count("nvSVPageoffRanged(") != 1:
        out.append("a plane is not sent to its own page-off right after the mode is read, before the mode-3 family line")
    fams = [m.start() for m in re.finditer(r"IOAccelResource2::pageoff\(", po)]
    guards = [m.start() for m in re.finditer(r"if \(fRangeLen\) \{ nvSVRangedFail\(this, out, NVRM_SS_NO_VRAM, ", po)]
    head = [f for f in fams if f < br]
    # each family call of the head has the plane's refusal as the statement right before it
    if len(guards) != 2 or len(head) != 2 or any(not any(g < f and ";" not in po[po.find("return; }", g) + 9:f] for g in guards) for f in head):
        out.append("a family fallback in the page-off's head takes a plane")
    rgd = func(s, "static void nvSVPageoffRanged(")
    if "IOAccelResource2::pageoff(" in rgd or "md->getPhysicalSegment(" in rgd:
        out.append("the plane's page-off calls the family's page-off or hands the surface's own pages to the copy engine")
    h = s.find("virtual bool start(IOService *provider) APPLE_KEXT_OVERRIDE {")
    st = s[h:h + 20000] if h >= 0 else ""
    o = st.find("nvSurfPlanesReadOptOut();")
    f = st.find("bool r = IOGraphicsAccelerator2::start(provider);")
    pr = st.find('setProperty("NVAccelSurfPlanes", nvSurfPlanesOffered() ? 1ULL : 0ULL, 32);')
    g = st.rfind("#ifndef NMX_ACCEL\n", 0, pr)
    if not (0 <= o < f < g < pr) or "#endif" in st[g:pr] or "#else" in st[g:pr]:
        out.append("the boot-arg is not read before the family starts, or the property is not the NVIDIA accelerator's")
    opt = func(s, "static void nvSurfPlanesReadOptOut(void)\n{")
    if ('PE_parse_boot_argn("nvaccel_surf_planes", &v, sizeof v) && !v' not in opt or "OSCompareAndSwap(0, 1, &once)" not in opt
            or "gSurfPlanes = 0;" not in opt or s.replace(opt, "").count("gSurfPlanes = 0") or "static int gSurfPlanes = 1;" not in s):
        out.append("the switch is not on by default with one boot-arg read once")
    return out


SYSCTLS = ("nvaccel_surf_planes", "nvaccel_surf_plane_validbits", "nvaccel_surfvram_ranged", "nvaccel_surfvram_ranged_off",
           "nvaccel_surfvram_ranged_small", "nvaccel_surfvram_ranged_range", "nvaccel_surfvram_ranged_shared",
           "nvaccel_surfvram_ranged_validbit", "nvaccel_pageoff_ranged", "nvaccel_pageoff_ranged_fail", "nvaccel_pageoff_ranged_temp")


def sysctl_problems(src):
    out = []
    s = strip_comments(src)
    inst = func(s, "static void nvaccelInstallSysctl(void)\n{")
    nv = inst[inst.find("#else"):] if "#else" in inst else ""
    for name in SYSCTLS:
        m = re.search(r"(SYSCTL_INT\s*\(\s*_debug\s*,\s*OID_AUTO\s*,\s*" + name + r"\s*,([^,]*),|NVACCEL_CNT\s*\(\s*" + name + r"\s*,)", s)
        if not m:
            out.append(f"debug.{name} is not declared")
        elif m.group(2) is not None and (re.search(r"CTLFLAG_(RW|WR|ANYBODY)", m.group(2)) or "CTLFLAG_RD" not in m.group(2)):
            out.append(f"debug.{name} is not read-only")
        reg = f"sysctl_register_oid(&sysctl__debug_{name});"
        if nv.count(reg) != 1 or s.count(reg) != 1:
            out.append(f"debug.{name} is not registered once, by the NVIDIA accelerator")
    return out


def main():
    src = SRC.read_text()
    st, gr, rg = staging(src), grant(src), ranged(src)
    arm("the staging code, the plane grant and the plane page-off are found in the source", bool(st and gr and rg))

    pr = text_problems(src)
    arm("grant, dirty mark, page-off routing, boot-arg and property are wired as designed", not pr, "; ".join(pr))
    pr = sysctl_problems(src)
    arm("the plane counters are read-only and registered once", not pr, "; ".join(pr))

    before = git_show(BEFORE, "kexts/NVRM/accel/nvrm-accel.cpp")
    if before is None:
        arm(f"the page-off before plane ranges ({BEFORE}) is readable", False)
    else:
        a, b = unranged(pageoff(src)), norm(pageoff(before))
        arm("a resource without a range runs the page-off of the step before, byte for byte", a == b and bool(a),
            next((f"first difference at {i}: {a[i-80:i+80]!r} vs {b[i-80:i+80]!r}" for i in range(min(len(a), len(b))) if a[i] != b[i]), "lengths differ"))
        alt = src.replace("    if (dmaMode >= NVRM_DMA_REFUSE) { OSAddAtomic64(1, &gCntDmaFamily);", "    if (dmaMode >= NVRM_DMA_REFUSE && !fRangeLen) { OSAddAtomic64(1, &gCntDmaFamily);", 1)
        arm("control: a change on the path of a resource without a range is caught", unranged(pageoff(alt)) != b)

    # text controls
    arm("control: the switch checked after the cache lookup is caught", any("before the cache lookup" in x for x in text_problems(
        src.replace("    if (ranged && !nvAccelPlanesOn(a)) { p->status = NVRM_SS_OFF; return kIOReturnSuccess; }   // no plane cache is looked up\n", "", 1).replace(
            "    NVResource *res = OSDynamicCast(NVResource, ctx);\n",
            "    NVResource *res = OSDynamicCast(NVResource, ctx);\n    if (ranged && !nvAccelPlanesOn(a)) { p->status = NVRM_SS_OFF; return kIOReturnSuccess; }\n", 1))))
    arm("control: the flag taken off every command's plane is caught", any("VRAM grant's plane only" in x for x in text_problems(
        src.replace("const bool ranged = cmd == NVRM_ESC_SURF_VRAM && (p->plane & NVRM_SS_PLANE_RANGED);", "const bool ranged = (p->plane & NVRM_SS_PLANE_RANGED);"))))
    arm("control: a dirty mark of 1 << plane is caught", any("dirty mark" in x for x in text_problems(
        src.replace("    valid[6] |= bit;\n", "    valid[6] |= (unsigned short)(1u << plane);\n"))))
    arm("control: a plane sent to the family in mode 3 is caught", any("mode-3 family line" in x for x in text_problems(
        src.replace("    if (fRangeLen) { nvSVPageoffRanged(this, md, surf, vm, &h, dmaMode, out); return; }\n", "").replace(
            "    if (!md || md->prepare(kIODirectionNone) != kIOReturnSuccess) {\n",
            "    if (fRangeLen) { nvSVPageoffRanged(this, md, surf, vm, &h, dmaMode, out); return; }\n    if (!md || md->prepare(kIODirectionNone) != kIOReturnSuccess) {\n", 1))))
    arm("control: a head fallback that takes a plane is caught", any("head" in x for x in text_problems(src.replace(PLANE_LINES[0], ""))))
    arm("control: the property published for every accelerator is caught", any("property" in x for x in text_problems(
        src.replace('#ifndef NMX_ACCEL\n            // Tells the plugin that a plane', '            // Tells the plugin that a plane', 1))))
    arm("control: a writable plane counter is caught", any("read-only" in x for x in sysctl_problems(
        src.replace("SYSCTL_INT(_debug, OID_AUTO, nvaccel_surf_planes, CTLFLAG_RD", "SYSCTL_INT(_debug, OID_AUTO, nvaccel_surf_planes, CTLFLAG_RW"))))
    arm("control: a plane counter never registered is caught", any("nvaccel_pageoff_ranged_temp" in x for x in sysctl_problems(
        src.replace("    sysctl_register_oid(&sysctl__debug_nvaccel_pageoff_ranged_temp);\n", ""))))

    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        code, out = build_run(d, "real", st, gr, rg)
        lines = [l for l in (out or "").splitlines() if l[:4] in ("ok  ", "FAIL")]
        arm(f"the lifted code compiles and runs on the host ({len(lines)} arms)", code == 0 and len(lines) == 35, out)
        for l in lines:
            arm("host: " + l[5:], l.startswith("ok"))

        def changed(tag, piece, old, new):
            texts = {"st": st, "gr": gr, "rg": rg}
            if texts[piece].count(old) != 1:
                return False, f"the text to alter is not in the {piece} piece once: {old[:80]}"
            texts[piece] = texts[piece].replace(old, new)
            c, o = build_run(d, "c" + tag, texts["st"], texts["gr"], texts["rg"])
            return c is not None and c != 0, o

        for tag, piece, what, old, new in [
            ("a", "rg", "the plane written at offset 0", "md->writeBytes((IOByteCount)dstOff,", "md->writeBytes((IOByteCount)0,"),
            ("b", "rg", "bit 0 set whatever the plane", "if (valid) valid[0] |= res->fValidBit;", "if (valid) valid[0] |= 1;"),
            ("c", "rg", "its own buffer's UNMAP after it is released",
             "    if (t->tok) {\n", "    if (t->b) { t->b->complete(); t->b->release(); t->b = nullptr; }\n    if (t->tok) {\n"),
            ("d", "rg", "its own buffer freed after a timeout", "else { if (t.tok) OSAddAtomic64(1, &gCntDmaAbandoned); t.b = nullptr; t.tok = nullptr; }", "else {}"),
            ("e", "rg", "physical pages offered when the mapping failed", "        if (!nvSVDmaMap(nvrm, b, want, &t->tok, &bus)) { nvSVTempFree(t, nvrm); return false; }\n", "        (void)nvSVDmaMap(nvrm, b, want, &t->tok, &bus); dmaMode = 0;\n"),
            ("f", "rg", "the mode-3 refusal taken out", "    if (dmaMode >= NVRM_DMA_REFUSE) { nvSVRangedFail(", "    if (false) { nvSVRangedFail("),
            ("g", "gr", "plane 0's resource not compared", "if (r0 == res || (m0 && m0 == vm) || (w0 && w0 == valid))", "if ((w0 && w0 == valid))"),
            ("h", "gr", "the range checked with an addition", "else if ((st = nvaccel_range_check(off, len, mdLen, vm->fBytes)) != 0) {",
             "else if ((st = (len && len <= vm->fBytes && off + len <= mdLen) ? 0u : (unsigned)NVRM_SS_BAD_RANGE) != 0) {"),
            ("i", "gr", "the plane-0 cache not given back", "        if (c0) ((OSObject *)c0)->release();\n", ""),
            ("j", "rg", "a mapping of another GPU's NVRM accepted", "const bool mapped = mode && nvrm && gNVSVStageTok && gNVSVStageNvrm == nvrm;",
             "const bool mapped = mode && nvrm && gNVSVStageTok;"),
            ("l", "rg", "the surface of the range not compared", "    if (_ZNK9IOSurface12getSurfaceIDEv(surf) != res->fRangeSid) {\n",
             "    if (false) {\n"),
            ("m", "gr", "plane 0's validity words not compared", "|| (w0 && w0 == valid)) {", ") {"),
            ("n", "gr", "scanout VRAM granted", "    else if (vm->fScanout) { st = NVRM_SS_NO_VRAM;", "    else if (false) { st = NVRM_SS_NO_VRAM;"),
            ("k", "rg", "the staging lock let go before the copy", "        IOBufferMemoryDescriptor *buf = nullptr;\n",
             "        IOBufferMemoryDescriptor *buf = nullptr; IOLockUnlock(lk); IOLockLock(lk);\n        IOLockUnlock(lk);\n"),
        ]:
            ok, det = changed(tag, piece, old, new)
            arm(f"control: {what} fails the host program", ok, det)

    print("PASS" if not FAILS else f"FAILED ({len(FAILS)})")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
