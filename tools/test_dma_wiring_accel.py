#!/usr/bin/env python3
"""The accelerator's side of DMA through a mapper (VT-d): the page-off's staging buffer, and nothing else, reaches the copy
engine through a bus mapping NVRM made ("nvSurfDma"); without a mapper every path is the one 2.9 ran.

NVRM tells each accelerator once how its GPU's addresses reach the bus: mode 0 (no mapper, or an NVRM that does not
know the question), 1 or 2 (a mapper translates; only addresses NVRM mapped may be handed to the copy engine), 3 (a
mapper the driver may not use). What this checks:
  - on the host, with the staging code lifted from kexts/NVRM/accel/nvrm-accel.cpp and run against a stand-in NVRM and
    stand-in buffers that record every call:
      * mode 0: the sequence of allocations, prepares, completes, releases and log lines, and every buffer returned, is
        the one v2.9's nvSVStage (7412cdd) gives over the same page-offs, the copy-engine timeout included;
      * the question is asked once per accelerator; an NVRM without it, or one that answers in a way this driver does
        not know, decides once; no NVRM yet decides nothing;
      * modes 1 and 2: the buffer is allocated without the contiguity option, mapped once through the NVRM the page-off
        goes to, reused only for that NVRM, given back (UNMAP) before it is completed and released, never while the
        stand-in's copy is on it; a failed MAP frees it and backs off; after a timeout the mapping is abandoned with
        the buffer (no UNMAP); a mapped buffer is never reused in mode 0; every NVRM reference is given back;
      * an accelerator's stop gives back the mapping made through its own NVRM (UNMAP, then the buffer is freed) under
        the staging lock, leaves another NVRM's alone, and without a mapping does not take the lock;
  - on the source text: the order of the edits in NVResource::pageoff (after the waits, the mode read before any page
    is wired or any copy counted; mode 3 to the family; the staged copy under the staging lock with the bus address;
    the per-page path in mode 0 only), the timeout and refusal branches (a refused per-page copy asks for the mode
    again), the stop of an accelerator, the hybrid frame in mode 0 only, the status
    values (NVRM_SS_NO_DMA is 0x80000008 and not NVRM_SS_BAD_RANGE), and read-only counters registered once.
Each group has negative controls on changed copies of the source.
   python3 tools/test_dma_wiring_accel.py"""
import pathlib
import re
import subprocess
import sys
import tempfile

R = pathlib.Path(__file__).resolve().parents[1]
SRC = R / "kexts/NVRM/accel/nvrm-accel.cpp"
ABI = R / "kexts/NVRM/nvrm_surfshare_abi.h"
V29 = "7412cdd"
FAILS = []


def arm(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (f"  [{str(detail).strip()[-600:]}]" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def strip_comments(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def git_show(rev, path):
    r = subprocess.run(["git", "-C", str(R), "show", f"{rev}:{path}"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def staging(src):
    """from the staging buffer's globals to the end of nvSVStage: everything that allocates, maps and frees it"""
    a = src.find("static IOLock *volatile gNVSVStageLock;")
    s = src.find("static IOBufferMemoryDescriptor *nvSVStage(", a)
    e = src.find("\n    return b;\n}\n", s)
    return src[a:e + len("\n    return b;\n}\n")] if 0 <= a < s < e else ""


def func(src, start, end_marker="\n}\n"):
    a = src.find(start)
    if a < 0:
        return ""
    b = src.find(end_marker, a)
    return src[a:b + len(end_marker)] if b >= 0 else ""


HARNESS = r'''
#include <cassert>
#include <cstdio>
#include <cstring>
#include <cstdint>
#include <string>
#include <vector>
#include <map>
typedef int IOReturn;
typedef unsigned IOOptionBits;
typedef unsigned long vm_size_t;
typedef unsigned char UInt8;
typedef long long SInt64;
enum : int { kIOReturnSuccess = 0, kIOReturnNoMemory = (int)0xe00002bd, kIOReturnUnsupported = (int)0xe00002c7,
             kIOReturnNotAttached = (int)0xe00002d9, kIOReturnNotReady = (int)0xe00002d8 };
static const unsigned kIODirectionInOut = 3, kIOMemoryPhysicallyContiguous = 0x10;
#define PAGE_SIZE 4096
static std::vector<std::string> T;      // the trace
static void tr(const char *f, ...) __attribute__((format(printf, 1, 2)));
#include <cstdarg>
static void tr(const char *f, ...) { char b[300]; va_list ap; va_start(ap, f); vsnprintf(b, sizeof b, f, ap); va_end(ap); T.push_back(b); }
#define ALOG(fmt, ...) tr("log " fmt, ##__VA_ARGS__)
static SInt64 OSAddAtomic64(SInt64 v, volatile SInt64 *p) { SInt64 o = *p; *p += v; return o; }
struct IOLock { int held; int takes; };
static IOLock *IOLockAlloc() { return new IOLock(); }
static void IOLockLock(IOLock *l) { assert(!l->held); l->held = 1; l->takes++; }
static void IOLockUnlock(IOLock *l) { assert(l->held); l->held = 0; }
static void IOLockFree(IOLock *l) { delete l; }
static bool OSCompareAndSwapPtr(void *o, void *n, void *volatile *p) { if (*p != o) return false; *p = n; return true; }
static void nvSVZero(void *p, unsigned long n) { memset(p, 0, n); }
struct OSSymbol {
    const char *name; static int live;
    static const OSSymbol *withCStringNoCopy(const char *s) { live++; return new OSSymbol{ s }; }
    void release() const { live--; delete this; }
};
int OSSymbol::live = 0;
static int gNextBuf = 1, gFailAlloc = 0, gFailPrepare = 0;
struct IOMemoryDescriptor { virtual ~IOMemoryDescriptor() {} };
struct IOBufferMemoryDescriptor : IOMemoryDescriptor {
    int id, rc = 1, prepared = 0; unsigned opts; unsigned long long len; bool dead = false;
    static IOBufferMemoryDescriptor *withOptions(IOOptionBits o, vm_size_t size, vm_size_t align)
    {
        tr("alloc opts %#x size %lu align %lu%s", o, (unsigned long)size, (unsigned long)align, gFailAlloc ? " -> fails" : "");
        if (gFailAlloc) { gFailAlloc--; return nullptr; }
        IOBufferMemoryDescriptor *b = new IOBufferMemoryDescriptor(); b->id = gNextBuf++; b->opts = o; b->len = size; return b;
    }
    unsigned long long getLength() const { assert(!dead); return len; }
    IOReturn prepare() { assert(!dead); tr("prepare %d%s", id, gFailPrepare ? " -> fails" : ""); if (gFailPrepare) { gFailPrepare--; return kIOReturnNoMemory; } prepared++; return 0; }
    IOReturn complete() { assert(!dead && prepared > 0); tr("complete %d", id); prepared--; return 0; }
    void release() { assert(!dead); tr("release %d", id); if (--rc == 0) dead = true; }
};
// NVRM as nvrm_dma.cpp answers "nvSurfDma" (wip/dma), with switches for what can go wrong
struct NVRMSurfDma;
struct IOService {
    explicit IOService(const char *n) : name(n) {}
    const char *name; int rc = 1; int mode = 0; bool knows = true; unsigned status = 0; bool mapFails = false;
    unsigned long long nextSerial = 1, nextBus = 0x10000000ull;
    std::map<unsigned long long, IOBufferMemoryDescriptor *> live;
    int queries = 0, maps = 0, unmaps = 0;
    void retain() { rc++; }
    void release() { assert(rc > 1); rc--; }
    IOReturn callPlatformFunction(const OSSymbol *sym, bool, void *p1, void *, void *, void *);
};
#define NVRM_SS_KERNEL_ABI 1
#include "nvrm_surfshare_abi.h"
IOReturn IOService::callPlatformFunction(const OSSymbol *sym, bool, void *p1, void *, void *, void *)
{
    assert(sym && !strcmp(sym->name, NVRM_SS_FN_DMA));
    if (!knows) { tr("%s: nvSurfDma unknown", name); return kIOReturnUnsupported; }
    NVRMSurfDma *q = (NVRMSurfDma *)p1;
    assert(q->version == NVRM_SS_DMA_VERSION);
    q->mode = (unsigned)mode; q->status = NVRM_SS_OFF;
    if (q->op == NVRM_SS_DMA_QUERY) { queries++; q->status = status; tr("%s: QUERY -> mode %d status %#x", name, mode, status); return 0; }
    if (q->op == NVRM_SS_DMA_UNMAP) {
        unmaps++;
        auto it = live.find((unsigned long long)(uintptr_t)q->token);
        if (it != live.end()) {
            assert(!it->second->dead && it->second->prepared > 0);     // given back while the buffer is still alive and wired
            tr("%s: UNMAP %llu (buffer %d)", name, it->first, it->second->id); live.erase(it); q->status = 0;
        } else tr("%s: UNMAP %llu not live", name, (unsigned long long)(uintptr_t)q->token);
        q->token = nullptr;
        return 0;
    }
    assert(q->op == NVRM_SS_DMA_MAP);
    if (mode != 1 && mode != 2) { tr("%s: MAP in mode %d -> off", name, mode); return 0; }
    IOBufferMemoryDescriptor *md = (IOBufferMemoryDescriptor *)q->md;
    assert(md && !md->dead && md->prepared > 0);                       // prepared by the caller
    assert(q->length && q->length <= md->getLength());
    maps++;
    if (mapFails) { q->status = NVRM_SS_NO_DMA; tr("%s: MAP buffer %d -> refused", name, md->id); return 0; }
    const unsigned long long s = nextSerial++;
    q->token = (void *)(uintptr_t)s; q->busBase = nextBus; nextBus += 0x10000000ull; q->status = 0;
    live[s] = md;
    tr("%s: MAP buffer %d len %llu -> token %llu bus %#llx", name, md->id, q->length, s, q->busBase);
    return 0;
}
struct NVAccel {
    unsigned fDmaAnswer = 0; IOService *nvrm = nullptr;
    IOService *locateNVRM() { return nvrm; }
    static NVAccel *resolve(NVAccel *o) { return o; }
};
static int gNvAccelDmaMode = -1;
static SInt64 gCntDmaStaged, gCntDmaMaps, gCntDmaMapFail, gCntDmaUnmaps, gCntDmaAbandoned, gCntDmaRefused, gCntDmaFamily, gCntDmaHybrid;
@@STAGING@@
#ifdef OLD
#define STAGE(len) nvSVStage(len)
#define TIMEOUT() (gNVSVStageBuf = nullptr)
#else
#define STAGE(len) nvSVStage(len, 0, nullptr)
#define TIMEOUT() nvSVStageAbandon()
#endif
static void ret(IOBufferMemoryDescriptor *b) { if (b) tr("ret %d", b->id); else tr("ret null"); }
// The page-offs of a session in mode 0: growing, shrinking, a timeout, failed allocations with their back-off, too large.
static void mode0()
{
    ret(STAGE(1000)); ret(STAGE(500)); ret(STAGE(1u << 20)); ret(STAGE((1u << 20) + 1)); ret(STAGE(3000));
    TIMEOUT(); ret(STAGE(4096));
    gFailAlloc = 1; ret(STAGE(8u << 20));
    for (int i = 0; i < 1024; i++) { IOBufferMemoryDescriptor *b = STAGE(16u << 20); if (i == 0 || i == 1023) ret(b); }
    ret(STAGE(16u << 20));
    gFailPrepare = 1; ret(STAGE(32u << 20)); for (int i = 0; i < 1024; i++) (void)STAGE(32u << 20); ret(STAGE(32u << 20)); ret(STAGE(1));
    ret(STAGE((64ull << 20) + 1)); ret(STAGE(64ull << 20)); ret(STAGE(10));
}
int main(int argc, char **argv)
{
    mode0();
#ifndef OLD
    if (argc > 1 && !strcmp(argv[1], "modes")) {
        T.clear();
        // ---- the question: once per accelerator
        IOService oldNvrm("old"); oldNvrm.knows = false;
        NVAccel a; a.nvrm = &oldNvrm;
        assert(nvAccelDmaMode(&a) == 0 && nvAccelDmaMode(&a) == 0 && a.fDmaAnswer == 1);
        NVAccel none;                                                        // no NVRM yet: decided nothing
        assert(nvAccelDmaMode(&none) == 0 && none.fDmaAnswer == 0);
        IOService n0("n0"); NVAccel a0; a0.nvrm = &n0;
        assert(nvAccelDmaMode(&a0) == 0 && nvAccelDmaMode(&a0) == 0 && n0.queries == 1 && gNvAccelDmaMode == 0);
        IOService nb("nb"); nb.status = NVRM_SS_BAD_VERSION; NVAccel ab; ab.nvrm = &nb;
        assert(nvAccelDmaMode(&ab) == NVRM_DMA_REFUSE && nb.queries == 1);
        IOService n7("n7"); n7.mode = 7; NVAccel a7; a7.nvrm = &n7;
        assert(nvAccelDmaMode(&a7) == NVRM_DMA_REFUSE);
        IOService A("A"); A.mode = 1; NVAccel aa; aa.nvrm = &A;
        IOService B("B"); B.mode = 2; NVAccel bb; bb.nvrm = &B;
        assert(nvAccelDmaMode(&aa) == 1 && nvAccelDmaMode(&aa) == 1 && A.queries == 1);
        assert(nvAccelDmaMode(&bb) == 2 && B.queries == 1);
        nvAccelDmaForget(&aa); assert(nvAccelDmaMode(&aa) == 1 && A.queries == 2);
        // ---- start from no buffer and no back-off
        TIMEOUT(); gNVSVStageBackoff = 0;
        // mapped through A, not contiguous, prepared before MAP; reused for A only
        IOBufferMemoryDescriptor *b1 = nvSVStage(5000, 1, &A);
        assert(b1 && !(b1->opts & kIOMemoryPhysicallyContiguous) && b1->prepared == 1 && A.maps == 1 && A.live.size() == 1);
        assert(gNVSVStageTok && gNVSVStageBus == 0x10000000ull && gNVSVStageMapLen == (1u << 20) && A.rc == 2);
        assert(nvSVStage(4000, 1, &A) == b1 && A.maps == 1);
        // another GPU: a new buffer through B; A's mapping given back before its buffer is completed and released
        T.clear();
        IOBufferMemoryDescriptor *b2 = nvSVStage(4000, 2, &B);
        assert(b2 && b2 != b1 && A.live.empty() && B.live.size() == 1 && A.rc == 1 && B.rc == 2);
        int unmapAt = -1, completeAt = -1, releaseAt = -1;
        for (int i = 0; i < (int)T.size(); i++) {
            if (T[i].rfind("A: UNMAP", 0) == 0) unmapAt = i;
            if (T[i] == "complete " + std::to_string(b1->id)) completeAt = i;
            if (T[i] == "release " + std::to_string(b1->id)) releaseAt = i;
        }
        assert(0 <= unmapAt && unmapAt < completeAt && completeAt < releaseAt);
        // mode 0 never takes a mapped buffer: a contiguous one replaces it, B's mapping given back first
        IOBufferMemoryDescriptor *b3 = nvSVStage(4000, 0, nullptr);
        assert(b3 && (b3->opts & kIOMemoryPhysicallyContiguous) && B.live.empty() && B.rc == 1 && !gNVSVStageTok && !gNVSVStageBus);
        // and a translated GPU never takes the physical one
        IOBufferMemoryDescriptor *b4 = nvSVStage(4000, 1, &A);
        assert(b4 && b4 != b3 && A.live.size() == 1 && b3->dead);
        // a failed MAP: the new buffer is completed and released, the old one and its mapping stay, back-off as before
        A.mapFails = true; T.clear();
        const SInt64 before = gCntDmaMapFail;
        assert(nvSVStage(2u << 20, 1, &A) == nullptr && gCntDmaMapFail == before + 1 && gNVSVStageBuf == b4 && A.live.size() == 1);
        bool freed = false; for (auto &l : T) if (l.rfind("release ", 0) == 0 && l != "release " + std::to_string(b4->id)) freed = true;
        assert(freed && gNVSVStageBackoff == 1024);
        while (gNVSVStageBackoff) (void)nvSVStage(2u << 20, 1, &A);   // too large for the buffer in hand: counts the back-off down
        A.mapFails = false;
        // a copy-engine timeout: buffer and mapping abandoned together, no UNMAP, the NVRM reference given back
        const int unmapsBefore = A.unmaps;
        TIMEOUT();
        assert(!gNVSVStageBuf && !gNVSVStageTok && !gNVSVStageNvrm && A.unmaps == unmapsBefore && A.live.size() == 1 && A.rc == 1 && !b4->dead);
        assert(gCntDmaAbandoned == 1);
        // a refusal (NVRM dropped the mapping): freed with an UNMAP NVRM answers "not live", mapped anew next time
        IOBufferMemoryDescriptor *b5 = nvSVStage(4000, 1, &A);
        A.live.clear();
        nvSVStageFree();
        assert(!gNVSVStageBuf && !gNVSVStageTok && A.rc == 1 && b5->dead);
        assert(nvSVStage(4000, 1, &A) && A.live.size() == 1);
        nvSVStageFree();
        assert(A.rc == 1 && B.rc == 1 && OSSymbol::live == 0);
        // an accelerator stops: the buffer mapped through its NVRM is given back and freed under the staging lock;
        // another NVRM's stop leaves it alone; with no mapping (mode 0) the lock is not even taken
        IOLock *lk = nvSVStageLock();
        IOBufferMemoryDescriptor *b6 = nvSVStage(4000, 1, &A);
        assert(b6 && A.live.size() == 1 && A.rc == 2);
        nvSVStageDrop(&B);
        assert(gNVSVStageBuf == b6 && A.live.size() == 1 && A.rc == 2);
        const int takes = lk->takes;
        nvSVStageDrop(&A);
        assert(!gNVSVStageBuf && !gNVSVStageTok && !gNVSVStageNvrm && A.live.empty() && A.rc == 1 && b6->dead && lk->takes == takes + 1 && !lk->held);
        IOBufferMemoryDescriptor *b7 = nvSVStage(4000, 0, nullptr);
        nvSVStageDrop(&A); nvSVStageDrop(nullptr);
        assert(gNVSVStageBuf == b7 && !b7->dead && lk->takes == takes + 1);
        nvSVStageFree();
        puts("modes ok");
    }
#endif
    for (auto &l : T) puts(l.c_str());
    return 0;
}
'''


def build_run(d, name, staging_text, defines, args=()):
    src = HARNESS.replace("@@STAGING@@", staging_text)
    (d / f"{name}.cpp").write_text(src)
    b = subprocess.run(["c++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-Wno-unused-function", "-Wno-unused-parameter", "-Wno-unused-variable",
                        *defines, f"-I{R / 'kexts/NVRM'}", str(d / f"{name}.cpp"), "-o", str(d / name)],
                       capture_output=True, text=True)
    if b.returncode != 0:
        return None, b.stderr[-800:]
    try:
        r = subprocess.run([str(d / name), *args], capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return -1, "did not finish within 60 s"
    return r.returncode, r.stdout if r.returncode == 0 else (r.stdout[-300:] + r.stderr[-500:])


def pageoff(src):
    return func(src, "void NVResource::pageoff(IOAccelEvent *ev, bool a1, bool *out, unsigned long long a3)\n{")


def pageoff_problems(src):
    out = []
    po = strip_comments(pageoff(src))
    if not po:
        return ["NVResource::pageoff not found"]
    wait_end = po.find("nvSurfWaitReadyFor(sid, kSurfGpuWaitMs);")
    marker = po.find("IOMemoryDescriptor *md = (IOMemoryDescriptor *)_ZNK9IOSurface19getMemoryDescriptorEv(surf);")
    mode = po.find("const unsigned dmaMode = nvAccelDmaMode(fAccel);")
    refuse = po.find("if (dmaMode >= NVRM_DMA_REFUSE) { OSAddAtomic64(1, &gCntDmaFamily); IOAccelResource2::pageoff(ev, a1, out, a3); return; }")
    prep = po.find("md->prepare(kIODirectionNone)")
    begin = po.find("nvaccel_pageoff_copy_begin(&fPoCopies)")
    if po.count("nvAccelDmaMode(") != 1:
        out.append("the page-off asks for the DMA mode more or less than once")
    if not (0 <= wait_end < marker < mode < refuse < prep < begin):
        out.append("the mode is not read after the waits and the marker line, with mode 3 leaving before any page is wired or any copy counted")
    lock = po.find("IOLockLock(lk);")
    stage = po.find("IOBufferMemoryDescriptor *sb = nvSVStage(len, dmaMode, dmaMode ? nvAccelNVRM(fAccel) : nullptr);")
    bus = po.find("addr64_t sp = !sb ? 0 : dmaMode ? (addr64_t)gNVSVStageBus : sb->getPhysicalSegment(0, &sl, kIOMemoryMapperNone);")
    maplen = po.find("if (sb && dmaMode) sl = (IOByteCount)gNVSVStageMapLen;")
    call1 = po.find("NVRM_SS_FN_PAGEOFF", lock)
    unlock = po.find("IOLockUnlock(lk);")
    if not (begin < lock < stage < bus < maplen < call1 < unlock):
        out.append("the staged copy does not take the buffer, its bus address and the copy under the staging lock")
    staged_block = po[lock:unlock] if 0 <= lock < unlock else ""
    tmo = staged_block.find("} else if (st == 0x65u) {")
    if tmo < 0 or "nvSVStageAbandon();" not in staged_block[tmo:staged_block.find("}", staged_block.find("nvSVStageAbandon();", tmo) if tmo >= 0 else 0) + 1] \
            or "gNVSVStageBuf = nullptr;" in staged_block or "nvSVStageFree();\n            } else if (st == NVRM_SS_NO_DMA)" in staged_block:
        out.append("a copy-engine timeout does not abandon the buffer together with its mapping")
    m = re.search(r"\} else if \(st == NVRM_SS_NO_DMA\) \{(.*?)\n            \}", staged_block, re.S)
    if not m or "nvAccelDmaForget(fAccel);" not in m.group(1) or "nvSVStageFree();" not in m.group(1):
        out.append("a refusal for unmapped pages does not ask again and free the mapping")
    fam = po.find("if (dmaMode && !staged) { md->complete(kIODirectionNone); OSAddAtomic64(1, &gCntDmaFamily); IOAccelResource2::pageoff(ev, a1, out, a3); return; }")
    per = po.find("unsigned long long *pa = (!staged && pages) ? (unsigned long long *)IOMalloc(pages * sizeof *pa) : nullptr;")
    phys = po.find("md->getPhysicalSegment(off, &seglen, kIOMemoryMapperNone)")
    if not (unlock < fam < per < phys):
        out.append("the per-page path (the surface's own physical pages) is not reserved to mode 0")
    if po.count("kIOMemoryMapperNone") != 2:
        out.append("a physical address is read somewhere else in the page-off")
    return out


def stop_problems(src):
    out = []
    s = strip_comments(src)
    stop = func(s, "    virtual void stop(IOService *provider) APPLE_KEXT_OVERRIDE {", "\n    }\n")
    drop = stop.find("nvSVStageDrop(fNVRM);")
    base = stop.find("IOGraphicsAccelerator2::stop(provider);")
    if not (0 <= drop < base) or stop.count("nvSVStageDrop(") != 1:
        out.append("an accelerator's stop does not give back the staging mapping made through its NVRM before the family's stop")
    po = strip_comments(pageoff(src))
    per = po.find("unsigned long long *pa = (!staged && pages)")
    tail = po[per:] if per >= 0 else ""
    if "else if (st == NVRM_SS_NO_DMA) { OSAddAtomic64(1, &gCntDmaRefused); nvAccelDmaForget(fAccel); }" not in tail:
        out.append("a per-page copy refused for unmapped pages does not ask for the mode again")
    return out


def hybrid_problems(src):
    out = []
    f = strip_comments(func(src, "static bool nvAccelHybridDma(NVDisplayPipe *pipe, IOService *fb, unsigned head, IOSurface *s)\n{"))
    pend = f.find("if (!x.accel || !nvSVPend(x.id, 2, nullptr)) return false;")
    gate = f.find("if (nvAccelDmaMode(x.accel) != NVRM_DMA_NONE) { OSAddAtomic64(1, &gCntDmaHybrid); return false; }")
    frame = f.find("nmh_dma_frame(&ops)")
    if not (0 <= pend < gate < frame):
        out.append("the hybrid copy-engine frame is not mode 0 only (gate after the pending check, before the frame)")
    return out


def status_problems(src, abi):
    out = []
    vals = dict(re.findall(r"#define\s+(NVRM_SS_\w+)\s+(0x[0-9a-fA-F]+)u", abi))
    if vals.get("NVRM_SS_NO_DMA", "").lower() != "0x80000008":
        out.append("NVRM_SS_NO_DMA is not 0x80000008")
    if vals.get("NVRM_SS_BAD_RANGE", "").lower() != "0x80000009":
        out.append("NVRM_SS_BAD_RANGE is not 0x80000009")
    if len(set(v.lower() for v in vals.values())) != len(vals):
        out.append("two status values share a number")
    if re.search(r"0x80000008", strip_comments(src)):
        out.append("the accelerator writes the refusal's number instead of NVRM_SS_NO_DMA")
    return out


DMA_SYSCTLS = ("nvaccel_dma_mode", "nvaccel_dma_staged", "nvaccel_dma_maps", "nvaccel_dma_map_fail", "nvaccel_dma_unmaps",
               "nvaccel_dma_abandoned", "nvaccel_dma_refused", "nvaccel_dma_family", "nvaccel_dma_hybrid")


def sysctl_problems(src):
    out = []
    s = strip_comments(src)
    for name in DMA_SYSCTLS:
        m = re.search(r"(SYSCTL_INT\s*\(\s*_debug\s*,\s*OID_AUTO\s*,\s*" + name + r"\s*,([^,]*),|NVACCEL_CNT\s*\(\s*" + name + r"\s*,)", s)
        if not m:
            out.append(f"debug.{name} is not declared")
        elif m.group(2) is not None and (re.search(r"CTLFLAG_(RW|WR|ANYBODY)", m.group(2)) or "CTLFLAG_RD" not in m.group(2)):
            out.append(f"debug.{name} is not read-only")
        inst = func(s, "static void nvaccelInstallSysctl(void)\n{")
        nv = inst[inst.find("#else"):] if "#else" in inst else ""
        reg = f"sysctl_register_oid(&sysctl__debug_{name});"
        if nv.count(reg) != 1 or s.count(reg) != 1:
            out.append(f"debug.{name} is not registered once, by the NVIDIA accelerator")
    if "#define NVACCEL_CNT(nm, var, desc) SYSCTL_QUAD(_debug, OID_AUTO, nm, CTLFLAG_RD | CTLFLAG_LOCKED," not in s:
        out.append("NVACCEL_CNT is no longer read-only")
    return out


def call_problems(src):
    """nvSurfDma is called exactly as the ABI declares, from one helper, and the token is never dereferenced"""
    out = []
    s = strip_comments(src)
    if s.count("NVRM_SS_FN_DMA") != 1 or "OSSymbol::withCStringNoCopy(NVRM_SS_FN_DMA)" not in s:
        out.append("nvSurfDma is called from more than one place")
    helper = func(s, "static IOReturn nvSVDmaCall(IOService *nvrm, struct NVRMSurfDma *d)\n{")
    if "nvrm->callPlatformFunction(sym, false, d, nullptr, nullptr, nullptr)" not in helper:
        out.append("nvSurfDma is not called as callPlatformFunction(NVRM_SS_FN_DMA, false, &q, NULL, NULL, NULL)")
    for op in ("NVRM_SS_DMA_QUERY", "NVRM_SS_DMA_MAP", "NVRM_SS_DMA_UNMAP"):
        for m in re.finditer(r"d\.op = " + op + r";|u\.op = " + op + r";", s):
            line = s[s.rfind("\n", 0, m.start()):s.find("\n", m.end())]
            if "VERSION" not in line or "NVRM_SS_OFF" not in line:
                out.append(f"a {op} request without the version or the preset status")
    if re.search(r"\*\s*\(\s*[\w\s]+\*\s*\)\s*[\w.>-]*\b(token|gNVSVStageTok)\b|\b(token|gNVSVStageTok)\s*(->|\[)", s):
        out.append("a token is dereferenced")
    return out


def main():
    src = SRC.read_text()
    abi = ABI.read_text()
    old = git_show(V29, "kexts/NVRM/accel/nvrm-accel.cpp")

    pr = pageoff_problems(src)
    arm("page-off: mode read after the waits; mode 3 to the family; staged copy on the bus address under the staging lock; per-page path mode 0 only",
        not pr, "; ".join(pr))
    pr = hybrid_problems(src)
    arm("the hybrid copy-engine frame runs in mode 0 only", not pr, "; ".join(pr))
    pr = stop_problems(src)
    arm("an accelerator's stop gives back its NVRM's staging mapping; a refused per-page copy asks for the mode again", not pr, "; ".join(pr))
    pr = status_problems(src, abi)
    arm("NVRM_SS_NO_DMA is 0x80000008, distinct from NVRM_SS_BAD_RANGE, and named, not written as a number", not pr, "; ".join(pr))
    pr = sysctl_problems(src)
    arm("the DMA counters are read-only and registered once", not pr, "; ".join(pr))
    pr = call_problems(src)
    arm("nvSurfDma is called as the ABI declares, from one helper; no token is dereferenced", not pr, "; ".join(pr))

    # negative controls on the text
    arm("control: the mode read before the waits is caught", any("after the waits" in x for x in pageoff_problems(
        src.replace("    const unsigned dmaMode = nvAccelDmaMode(fAccel);\n", "", 1).replace(
            "    struct NVHold {", "    const unsigned dmaMode = nvAccelDmaMode(fAccel);\n    struct NVHold {", 1))))
    arm("control: mode 3 leaving after the pages are wired is caught", any("after the waits" in x for x in pageoff_problems(
        src.replace("    if (dmaMode >= NVRM_DMA_REFUSE) { OSAddAtomic64(1, &gCntDmaFamily); IOAccelResource2::pageoff(ev, a1, out, a3); return; }\n", "", 1).replace(
            "    bool mayUnwire = true;\n", "    if (dmaMode >= NVRM_DMA_REFUSE) { OSAddAtomic64(1, &gCntDmaFamily); IOAccelResource2::pageoff(ev, a1, out, a3); return; }\n    bool mayUnwire = true;\n", 1))))
    arm("control: a physical staging address in every mode is caught", any("staging lock" in x for x in pageoff_problems(
        src.replace("addr64_t sp = !sb ? 0 : dmaMode ? (addr64_t)gNVSVStageBus : sb->getPhysicalSegment(0, &sl, kIOMemoryMapperNone);",
                    "addr64_t sp = sb ? sb->getPhysicalSegment(0, &sl, kIOMemoryMapperNone) : 0;"))))
    arm("control: the per-page path in every mode is caught", any("mode 0" in x for x in pageoff_problems(
        src.replace("    if (dmaMode && !staged) { md->complete(kIODirectionNone); OSAddAtomic64(1, &gCntDmaFamily); IOAccelResource2::pageoff(ev, a1, out, a3); return; }\n", ""))))
    arm("control: a timeout that drops the buffer but keeps its mapping is caught", any("timeout" in x for x in pageoff_problems(
        src.replace("                nvSVStageAbandon();\n            } else if (st == NVRM_SS_NO_DMA) {", "                gNVSVStageBuf = nullptr;\n            } else if (st == NVRM_SS_NO_DMA) {"))))
    arm("control: a refusal that keeps the stale mapping is caught", any("refusal" in x for x in pageoff_problems(
        src.replace("                nvAccelDmaForget(fAccel);\n                nvSVStageFree();\n", ""))))
    arm("control: a stop that keeps the staging mapping is caught", any("stop" in x for x in stop_problems(
        src.replace("        nvSVStageDrop(fNVRM);", "", 1))))
    arm("control: a per-page refusal that keeps the cached mode is caught", any("per-page" in x for x in stop_problems(
        src.replace("            else if (st == NVRM_SS_NO_DMA) { OSAddAtomic64(1, &gCntDmaRefused); nvAccelDmaForget(fAccel); }\n", "", 1))))
    arm("control: the hybrid frame in every mode is caught", any("mode 0 only" in x for x in hybrid_problems(
        src.replace("    if (nvAccelDmaMode(x.accel) != NVRM_DMA_NONE) { OSAddAtomic64(1, &gCntDmaHybrid); return false; }\n", ""))))
    arm("control: the refusal's number written out is caught", any("instead of" in x for x in status_problems(
        src.replace("} else if (st == NVRM_SS_NO_DMA) {", "} else if (st == 0x80000008u) {"), abi)))
    arm("control: NVRM_SS_NO_DMA given NVRM_SS_BAD_RANGE's number is caught", any("share a number" in x or "not 0x80000008" in x for x in status_problems(
        src, abi.replace("#define NVRM_SS_NO_DMA        0x80000008u", "#define NVRM_SS_NO_DMA        0x80000009u"))))
    arm("control: a writable DMA counter is caught", any("read-only" in x for x in sysctl_problems(
        src.replace("SYSCTL_INT(_debug, OID_AUTO, nvaccel_dma_mode, CTLFLAG_RD", "SYSCTL_INT(_debug, OID_AUTO, nvaccel_dma_mode, CTLFLAG_RW"))))
    arm("control: a DMA counter never registered is caught", any("nvaccel_dma_hybrid" in x for x in sysctl_problems(
        src.replace("    sysctl_register_oid(&sysctl__debug_nvaccel_dma_hybrid);\n", ""))))
    arm("control: a MAP without its version is caught", any("MAP request" in x for x in call_problems(
        src.replace("d.version = NVRM_SS_DMA_VERSION; d.op = NVRM_SS_DMA_MAP;", "d.op = NVRM_SS_DMA_MAP;"))))
    arm("control: a dereferenced token is caught", any("dereferenced" in x for x in call_problems(
        src.replace("        *tok = d.token; *bus = d.busBase;\n", "        *tok = d.token; *bus = d.busBase + *(unsigned char *)d.token;\n"))))

    # on the host
    st_new = staging(src)
    st_old = staging(old) if old else ""
    arm("the staging code is found in today's source and in v2.9's", bool(st_new) and bool(st_old))
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        code_old, out_old = build_run(d, "old", st_old, ["-DOLD"])
        code_new, out_new = build_run(d, "new", st_new, [])
        same = code_old == 0 and code_new == 0 and out_old == out_new and out_old.count("\n") > 20
        arm("mode 0: allocations, prepares, completes, releases, log lines and buffers are v2.9's, page-off for page-off",
            same, f"old {code_old} new {code_new}: {(out_old or '')[-300:]} || {(out_new or '')[-300:]}")
        code, out = build_run(d, "modes", st_new, [], ("modes",))
        arm("modes 1 and 2: mapped once per NVRM, given back before it is freed, abandoned on a timeout, never shared with mode 0",
            code == 0 and "modes ok" in (out or ""), out)

        def changed(tag, old_text, new_text, args=("modes",)):
            t = st_new.replace(old_text, new_text)
            if t == st_new:
                return False, "the change did not apply"
            c, o = build_run(d, "c" + tag, t, [], args)
            return c is not None and c != 0, o

        def changed_mode0(tag, old_text, new_text):
            t = st_new.replace(old_text, new_text)
            if t == st_new:
                return False, "the change did not apply"
            c, o = build_run(d, "m" + tag, t, [])
            return c is not None and (c != 0 or o != out_old), o

        ok, det = changed("a", "static void nvSVStageFree(void)\n{\n",
                          "static void nvSVStageFree(void)\n{\n    if (gNVSVStageBuf) { gNVSVStageBuf->complete(); gNVSVStageBuf->release(); }\n")
        arm("control: an UNMAP after the buffer was freed fails the host program", ok, det)
        ok, det = changed("b", "static void nvSVStageAbandon(void)\n{\n", "static void nvSVStageAbandon(void)\n{\n    nvSVStageFree(); return;\n")
        arm("control: a timeout that gives the mapping back fails the host program", ok, det)
        ok, det = changed("c", "(dmaMode ? gNVSVStageTok && gNVSVStageNvrm == nvrm : !gNVSVStageTok)", "(dmaMode ? gNVSVStageTok != nullptr : !gNVSVStageTok)")
        arm("control: a mapping reused for another GPU's NVRM fails the host program", ok, det)
        ok, det = changed_mode0("d", "kIODirectionInOut | (dmaMode ? 0 : kIOMemoryPhysicallyContiguous)", "kIODirectionInOut")
        arm("control: a mode-0 buffer without the contiguity option fails the mode-0 comparison", ok, det)
        ok2, det2 = changed("e2", "(dmaMode ? gNVSVStageTok && gNVSVStageNvrm == nvrm : !gNVSVStageTok)", "(dmaMode ? gNVSVStageTok && gNVSVStageNvrm == nvrm : true)")
        arm("control: a mapped buffer reused in mode 0 fails the host program", ok2, det2)
        ok, det = changed("f", "    if (r != kIOReturnSuccess) mode = NVRM_DMA_NONE;\n", "    if (r != kIOReturnSuccess) return 0;\n")
        arm("control: an NVRM without the question asked at every page-off fails the host program", ok, det)
        ok, det = changed("g", "    if (!nvrm) return 0;\n", "    if (!nvrm) { a->fDmaAnswer = 1; return 0; }\n")
        arm("control: mode 0 decided for good before NVRM is there fails the host program", ok, det)
        ok, det = changed("h", "    if (b && dmaMode && !nvSVDmaMap(nvrm, b, want, &tok, &bus)) { b->complete(); b->release(); b = nullptr; }\n",
                          "    if (b && dmaMode && !nvSVDmaMap(nvrm, b, want, &tok, &bus)) { b = nullptr; }\n")
        arm("control: a buffer kept wired after a failed MAP fails the host program", ok, det)
        ok, det = changed("j", "    if (gNVSVStageTok && gNVSVStageNvrm == nvrm) nvSVStageFree();\n", "    if (gNVSVStageTok) nvSVStageFree();\n")
        arm("control: a stop that frees another NVRM's mapping fails the host program", ok, det)
        ok, det = changed("k", "    if (!nvrm || !lk || !gNVSVStageTok) return;\n", "    if (!nvrm || !lk) return;\n")
        arm("control: a stop that takes the staging lock without a mapping fails the host program", ok, det)
        ok, det = changed_mode0("i", "    nvSVStageFree();\n    gNVSVStageBuf = b;\n", "    gNVSVStageBuf = b;\n")
        arm("control: a replaced mode-0 buffer that is not freed fails the mode-0 comparison", ok, det)

    print("PASS" if not FAILS else f"FAILED ({len(FAILS)})")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
