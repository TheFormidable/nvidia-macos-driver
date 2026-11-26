#!/usr/bin/env python3
"""The page-off's copy without the accelerator lock (nvPoCopyUnlocked, 3.0.20), run on the host.

The function is lifted from kexts/NVRM/accel/nvrm-accel.cpp and run against a stand-in: an accelerator lock and the
copy's own buffer lock that know which thread holds them, a resource laid out at the offsets the driver reads, a
surface whose system memory is one number (the frame it holds), the GPU's memory (another number), and NVRM's copy
engine, which fills the buffer with the GPU's frame and, while it reads, lets other threads act: the plugin marks a new
GPU write, another page-off copies with the lock held and a CPU writer writes after it, the VRAM is allocated again,
the copy engine fails, the lock goes away.

What must hold, in every arm: the accelerator lock is free while the copy engine reads and held whenever the surface's
pages, the validity bit or the copy's numbers are written; the function comes back with the lock held by its caller's
thread; the buffer lock is never taken with the accelerator lock held; the VRAM is marked busy for exactly the copy
engine's read; every reference it takes is given back. And what reaches the surface is a frame the GPU's memory held
after the wait, and never over a CPU write made after another page-off's copy.

Negative controls: four altered copies of the function (no check of the plugin's marks, the copy's number taken after
the read, the buffer written into the surface without the lock, the VRAM not marked busy) each fail their arm.
   python3 tools/test_pageoff_copy_unlocked.py"""
import pathlib, subprocess, sys, tempfile

R = pathlib.Path(__file__).resolve().parents[1]
SRC = (R / "kexts/NVRM/accel/nvrm-accel.cpp").read_text()
fails = 0


def arm(name, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        fails += 1
        if detail: print("     " + detail.strip().replace("\n", "\n     ")[:3000])


def lift(src):
    a = src.find("static int nvPoCopyUnlocked(NVResource *res, NVAccel *a, void *surf, NVVidMemory *vm, const struct NVRMRmHandles *h,")
    e = src.find("\n}\n", a)
    return src[a:e + 3] if 0 <= a < e else ""


FIXTURE = r'''
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
typedef int64_t SInt64; typedef int32_t SInt32; typedef uint32_t UInt32; typedef uint64_t addr64_t; typedef uint64_t IOByteCount;
typedef int IOReturn;
enum { kIOReturnSuccess = 0, kIODirectionNone = 0, kIOMemoryMapperNone = 0 };
#define ALOG(...) ((void)0)
#include "nvaccel_pageoff_recheck.h"
#include "nvres_fields.h"
#define NVRM_SS_KERNEL_ABI 1
#include "nvrm_surfshare_abi.h"
enum { kPoCuNotTaken = 0, kPoCuDone, kPoCuLocked };
#define NVRM_FENCE_WAIT_MAX_MS 2000u
static void *const T1 = (void *)0x1001;
static void *gThread = T1;
static int gBad; static const char *gBadWhy = "";
static void bad(const char *why) { if (!gBad++) gBadWhy = why; }
struct IOLock { void *holder; int locks; };
static IOLock L, U;                                       // the accelerator lock, the copy's buffer lock
static void IOLockLock(IOLock *l) {
    if (l == &U && L.holder == gThread) bad("the buffer lock is taken with the accelerator lock held");
    if (l->holder) bad("a lock is taken twice");
    l->holder = gThread; l->locks++;
}
static void IOLockUnlock(IOLock *l) { if (l->holder != gThread) bad("a lock is given back by a thread that does not hold it"); l->holder = nullptr; }
static IOLock *nvPoCuLock(void) { return &U; }
static SInt64 OSAddAtomic64(SInt64 v, SInt64 *p) { SInt64 o = *p; *p += v; return o; }
static SInt32 OSDecrementAtomic(volatile SInt32 *p) { return (*p)--; }
static uint64_t gClock = 1000;
static uint64_t mach_absolute_time() { return gClock; }
static void absolutetime_to_nanoseconds(uint64_t a, uint64_t *ns) { *ns = a; }
static void nvSVZero(void *p, unsigned long n) { memset(p, 0, n); }
static void *IOMalloc(size_t n) { return malloc(n); }
static void IOFree(void *p, size_t) { free(p); }
static int gFreed;
struct OSObject { int rc = 1; int getRetainCount() const { return rc; } void retain() { rc++; } void release() { if (--rc <= 0) gFreed++; } };
struct OSSymbol { static const OSSymbol *withCStringNoCopy(const char *) { static OSSymbol s; return &s; } void release() const {} };
// ---- the accelerator and its lock -------------------------------------------------------------------------------------
struct NVAccel : OSObject {
    bool may = true; int pins = 0;
    bool mayRelease() { return may; }
    void unpinLock() { pins--; }
};
static bool gLockGone;
static void nvFamUnlock(NVAccel *) { IOLockUnlock(&L); }
static bool nvFamLock(NVAccel *a, bool) { if (gLockGone) return false; IOLockLock(&L); a->pins++; return true; }
static int gStoodDown;
static void nvPoStandDown(const char *) { gStoodDown++; }
static int gPoUnlocked = 1, gPoCopyUnlocked = 1, gPoCuMaxUs;
static volatile SInt32 gPoCuBackoff;
static SInt64 gCntPoUnlocked, gCntPoCuOk, gCntPoCuChanged, gCntPoCuAlready, gCntPoCuNoBuf, gCntPoCuFailed;
static unsigned gNVSVPageoff, gNVSVPageoffFail;
static unsigned gDmaMode; static int gDmaForgot;
static unsigned nvAccelDmaMode(NVAccel *) { return gDmaMode; }
static void nvAccelDmaForget(NVAccel *) { gDmaForgot++; }
static int gWaits, gWaitsLocked;
static bool nvSurfWaitReadyFor(unsigned, unsigned) { gWaits++; if (L.holder) gWaitsLocked++; return true; }
static int gPendCleared;
static bool nvSVPend(unsigned, int op, void * = nullptr) { if (!op) gPendCleared++; return true; }
// ---- the resource, its VRAM and the surface ---------------------------------------------------------------------------
struct NVVidMemory : OSObject { void *fRmHandle; unsigned long long fAllocationCookie; bool fScanout; unsigned long long fBytes; volatile unsigned fPoCopyBusy; };
struct NVResource : OSObject {
    unsigned char body[0x100];
    NVAccel *fAccel; nvaccel_pageoff_copies fPoCopies; unsigned long long fMarks = 0, fRangeLen = 0; unsigned short fValidBit = 0;
    unsigned short validBit() const { return fRangeLen ? fValidBit : (unsigned short)1; }
};
static NVVidMemory *nvres_vidmem(NVResource *r) { return *(NVVidMemory **)((unsigned char *)r + kNvResVidMem); }
struct NVRMRmHandles { void *kapiDevice; };
static int gGpu, gSys, gCpuWrite;                         // the GPU's frame, the frame in system memory, a CPU write still there
struct IOMemoryDescriptor {
    unsigned long long len = 8192; int prepared = 0;
    unsigned long long getLength() const { return len; }
    IOReturn prepare(int) { prepared++; return 0; }
    IOReturn complete(int) { prepared--; return 0; }
    IOByteCount writeBytes(IOByteCount, const void *src, IOByteCount n) {
        if (L.holder != gThread) bad("the surface's pages are written without the accelerator lock");
        if (!prepared) bad("the surface's pages are written unwired");
        gSys = *(const int *)src; gCpuWrite = 0; return n;
    }
};
struct IOBufferMemoryDescriptor {
    int frame = -1; unsigned long long len = 1 << 20;
    unsigned long long getLength() const { return len; }
    addr64_t getPhysicalSegment(unsigned long long, IOByteCount *l, int) { *l = len; return 0x100000; }
    void *getBytesNoCopy() { return &frame; }
};
static IOBufferMemoryDescriptor BUF; static bool gNoBuf;
static IOBufferMemoryDescriptor *gPoCuBuf;
static IOBufferMemoryDescriptor *nvPoCuBuffer(unsigned long long) { if (U.holder != gThread) bad("the buffer is taken without its lock"); return gNoBuf ? nullptr : &BUF; }
struct Surf { unsigned id; IOMemoryDescriptor md; };
struct Cache { unsigned char pad[0x10]; void *surf; };
static unsigned _ZNK9IOSurface12getSurfaceIDEv(void *s) { return ((Surf *)s)->id; }
static IOMemoryDescriptor *_ZNK9IOSurface19getMemoryDescriptorEv(void *s) { return &((Surf *)s)->md; }
static NVAccel *A; static NVResource *RES; static NVVidMemory VM, VM2; static Cache CACHE; static Surf SURF; static unsigned short VALID[8];
template <class T> static T &raw(void *o, unsigned off) { return *(T *)((unsigned char *)o + off); }
// ---- NVRM's copy engine: other threads act while it reads ---------------------------------------------------------------
static void (*gDuringRead)();
static unsigned gCeStatus; static int gReads, gReadsLocked, gReadsNotBusy;
struct IOService {
    void callPlatformFunction(const OSSymbol *, bool, void *p, void *, void *, void *) {
        NVRMSurfPageoff *q = (NVRMSurfPageoff *)p;
        gReads++; if (L.holder) gReadsLocked++; if (!VM.fPoCopyBusy) gReadsNotBusy++;
        if (void (*f)() = gDuringRead) { gDuringRead = nullptr; f(); }
        q->status = gCeStatus; q->usec = 1000;
        if (!gCeStatus) BUF.frame = gGpu;
    }
};
static IOService NVRM_;
static IOService *nvAccelNVRM(NVAccel *) { return &NVRM_; }
@@FN@@
// ---- the other threads -------------------------------------------------------------------------------------------------
static void markWrite() { RES->fMarks++; VALID[0] &= (unsigned short)~1; gGpu++; }        // the plugin, with the lock (taken and given back)
static void otherPageoffThenCpuWrite() {   // another page-off copies with the lock held, then a CPU writer writes
    const unsigned long long n = nvaccel_pageoff_copy_begin(&RES->fPoCopies);
    gSys = gGpu; VALID[0] |= 1; nvaccel_pageoff_copy_end(&RES->fPoCopies, n);
    gSys = 99; gCpuWrite = 1;
}
static void reallocVram() { VM.fAllocationCookie = 77; }
static void swapVm() { raw<NVVidMemory *>(RES, kNvResVidMem) = &VM2; }
static void dropOthers() { RES->rc = 1; }
static void setup() {
    A = new NVAccel(); A->rc = 3;
    RES = new NVResource(); RES->rc = 2; RES->fAccel = A;
    L = IOLock(); U = IOLock();
    VM = NVVidMemory(); VM.fRmHandle = (void *)0x10; VM.fAllocationCookie = 5; VM.fBytes = 8192; VM.rc = 1;
    VM2 = VM; VM2.fAllocationCookie = 6;
    SURF = Surf(); SURF.id = 7; memset(&CACHE, 0, sizeof CACHE); CACHE.surf = &SURF; memset(VALID, 0, sizeof VALID);
    memset(RES->body, 0, sizeof RES->body);
    raw<unsigned char>(RES, kNvResType) = 0xc0; raw<unsigned short *>(RES, kNvResValid) = VALID; raw<NVVidMemory *>(RES, kNvResVidMem) = &VM; raw<Cache *>(RES, kNvResCache) = &CACHE;
    gBad = 0; gBadWhy = ""; gFreed = 0; gLockGone = false; gStoodDown = 0; gPoUnlocked = gPoCopyUnlocked = 1; gPoCuBackoff = 0; gDmaMode = 0; gDmaForgot = 0;
    gCntPoUnlocked = gCntPoCuOk = gCntPoCuChanged = gCntPoCuAlready = gCntPoCuNoBuf = gCntPoCuFailed = 0; gNVSVPageoff = gNVSVPageoffFail = 0;
    gWaits = gWaitsLocked = 0; gPendCleared = 0; gGpu = 1; gSys = 0; gCpuWrite = 0; BUF = IOBufferMemoryDescriptor(); gNoBuf = false; gPoCuBuf = &BUF;
    gDuringRead = nullptr; gCeStatus = 0; gReads = gReadsLocked = gReadsNotBusy = 0;
}
struct Res { int ret; bool out; bool lockHeld; };
static OSObject *holdRes, *holdAccel;
static Res run() {
    IOLockLock(&L);   // the family's caller
    holdRes = holdAccel = nullptr; bool out = false; NVRMRmHandles h = { (void *)1 };
    const int ret = nvPoCopyUnlocked(RES, A, &SURF, &VM, &h, true, &holdRes, &holdAccel, &out);
    Res r = { ret, out, L.holder == gThread };
    if (r.lockHeld) IOLockUnlock(&L);
    if (holdRes) holdRes->release();      // NVResource::pageoff's holds, released when it returns
    if (holdAccel) holdAccel->release();
    return r;
}
// after every arm that came back with the lock
static bool sane(const Res &r) {
    return !gBad && r.lockHeld && !U.holder && !VM.fPoCopyBusy && VM.rc == 1 && A->rc == 3 && A->pins == 0 && gFreed == 0
        && SURF.md.prepared == 0 && gWaitsLocked == 0 && gReadsLocked == 0 && gReadsNotBusy == 0;
}
static int failed;
static void check(const char *n, bool ok) {
    printf("%s %s\n", ok ? "ok  " : "FAIL", n);
    if (!ok) { failed++; printf("     bad %d (%s) sys %d gpu %d cpu %d valid %#x started %llu finished %llu ok %lld changed %lld already %lld failed %lld vm.rc %d a.rc %d res.rc %d pins %d reads %d/%d/%d\n",
        gBad, gBadWhy, gSys, gGpu, gCpuWrite, VALID[0], RES->fPoCopies.started, RES->fPoCopies.finished, (long long)gCntPoCuOk, (long long)gCntPoCuChanged,
        (long long)gCntPoCuAlready, (long long)gCntPoCuFailed, VM.rc, A->rc, RES->rc, A->pins, gReads, gReadsLocked, gReadsNotBusy); }
}
int main() {
    {   setup(); Res r = run();
        check("quiet: the copy engine reads without the lock, the surface gets the GPU's frame with it, the bit is set, the copy ends",
              sane(r) && r.ret == kPoCuDone && r.out && gSys == 1 && (VALID[0] & 1) && RES->fPoCopies.started == 1 && RES->fPoCopies.finished == 1
              && gCntPoCuOk == 1 && gPendCleared == 1 && gWaits == 1 && gReads == 1 && RES->rc == 2); }
    {   setup(); gDuringRead = markWrite; Res r = run();
        check("the plugin marks a GPU write while the engine reads: nothing reaches the surface, copied again with the lock held",
              sane(r) && r.ret == kPoCuLocked && gSys == 0 && !(VALID[0] & 1) && RES->fPoCopies.finished == 0 && gCntPoCuChanged == 1); }
    {   setup(); gDuringRead = otherPageoffThenCpuWrite; Res r = run();
        check("another page-off copies while the engine reads and a CPU writer writes after it: done, the CPU write is still there",
              sane(r) && r.ret == kPoCuDone && r.out && gSys == 99 && gCpuWrite == 1 && gCntPoCuAlready == 1 && RES->fPoCopies.finished == 2); }
    {   setup(); gDuringRead = [] { otherPageoffThenCpuWrite(); reallocVram(); }; Res r = run();
        check("another page-off copies, a CPU writer writes, then the VRAM is allocated again: done, the CPU write is still there",
              sane(r) && r.ret == kPoCuDone && r.out && gSys == 99 && gCntPoCuAlready == 1 && gCntPoCuChanged == 0); }
    {   setup(); gDuringRead = reallocVram; Res r = run();
        check("the VRAM is allocated again while the engine reads (another cookie): nothing reaches the surface",
              sane(r) && r.ret == kPoCuLocked && gSys == 0 && gCntPoCuChanged == 1); }
    {   setup(); gDuringRead = swapVm; Res r = run();
        check("the resource points at other VRAM after the read: nothing reaches the surface, the held VRAM is given back",
              sane(r) && r.ret == kPoCuLocked && gSys == 0 && gCntPoCuChanged == 1); }
    {   setup(); gCeStatus = 0x65u; Res r = run();
        check("the copy engine times out: the buffer is left to it, nothing reaches the surface, copied again with the lock",
              sane(r) && r.ret == kPoCuLocked && gSys == 0 && gPoCuBuf == nullptr && gCntPoCuFailed == 1); }
    {   setup(); gCeStatus = NVRM_SS_NO_DMA; Res r = run();
        check("NVRM refuses the buffer's pages (its GPU translates now): the mode is asked again, copied with the lock",
              sane(r) && r.ret == kPoCuLocked && gSys == 0 && gDmaForgot == 1); }
    {   setup(); gNoBuf = true; Res r = run();
        check("no buffer: copied with the lock, nothing read", sane(r) && r.ret == kPoCuLocked && gReads == 0 && gSys == 0); }
    {   setup(); gDuringRead = dropOthers; Res r = run();
        check("everybody else lets go of the resource meanwhile: nothing read through it, the page-off answers no and keeps its hold",
              !gBad && r.lockHeld && r.ret == kPoCuDone && !r.out && gSys == 0 && !U.holder && VM.rc == 1); }
    {   setup(); gDuringRead = [] { gLockGone = true; }; Res r = run();
        check("the lock cannot be taken back: it stands down, answers no, comes back without the lock and with nothing written",
              !gBad && !r.lockHeld && r.ret == kPoCuDone && !r.out && gStoodDown == 1 && gSys == 0 && !U.holder && !VM.fPoCopyBusy); }
    {   setup(); A->may = false; Res r = run();
        check("the thread may not let go of the lock (not the owner, or pinned): not taken, the lock never let go",
              !gBad && r.lockHeld && r.ret == kPoCuNotTaken && gReads == 0 && L.locks == 1); }
    {   setup(); gDmaMode = 1; Res r = run();
        check("a GPU behind a DMA mapper: not taken", !gBad && r.ret == kPoCuNotTaken && gReads == 0 && L.locks == 1); }
    {   setup(); VALID[0] = 1; Res r = run();
        check("system memory already current: not taken (copied as before)", !gBad && r.ret == kPoCuNotTaken && gReads == 0); }
    {   setup(); gPoCopyUnlocked = 0; Res r = run();
        check("boot-arg nvaccel_pageoff_copy_locked=1: not taken", !gBad && r.ret == kPoCuNotTaken && gReads == 0); }
    {   setup(); RES->fRangeLen = 4096; RES->fValidBit = 2; Res r = run();
        check("a plane of a two-plane surface: not taken", !gBad && r.ret == kPoCuNotTaken && gReads == 0); }
    printf(failed ? "%d failed\n" : "all passed\n", failed);
    return failed ? 1 : 0;
}
'''


def build_run(fn, tmp, tag):
    c = pathlib.Path(tmp) / (tag + ".cpp")
    c.write_text(FIXTURE.replace("@@FN@@", fn))
    exe = pathlib.Path(tmp) / tag
    inc = ["-I", str(R / "kexts/NVRM/accel"), "-I", str(R / "kexts/NVRM")]
    p = subprocess.run(["c++", "-std=c++17", "-w", *inc, str(c), "-o", str(exe)], capture_output=True, text=True)
    if p.returncode:
        return None, p.stderr
    q = subprocess.run([str(exe)], capture_output=True, text=True)
    return q.stdout, q.stdout + q.stderr


fn = lift(SRC)
arm("nvPoCopyUnlocked is found in the source", bool(fn))
CONTROLS = [
    ("the plugin's marks not compared", "res->fMarks == marks0 &&", "",
     "the plugin marks a GPU write while the engine reads"),
    ("the copy's number taken after the read",
     "            copyNumber = nvaccel_pageoff_copy_begin(&res->fPoCopies);   // before the GPU's memory is read\n", "",
     "another page-off copies while the engine reads"),
    ("the buffer written into the surface before the lock is taken back",
     "    __atomic_store_n(&vm->fPoCopyBusy, 0u, __ATOMIC_RELEASE);   // NVRM is done with the VRAM\n",
     "    __atomic_store_n(&vm->fPoCopyBusy, 0u, __ATOMIC_RELEASE);   // NVRM is done with the VRAM\n"
     "    if (sb && !st) { md0->prepare(kIODirectionNone); md0->writeBytes(0, sb->getBytesNoCopy(), (IOByteCount)len); md0->complete(kIODirectionNone); }\n",
     "quiet"),
    ("the VRAM not marked busy", "    __atomic_store_n(&vm->fPoCopyBusy, 1u, __ATOMIC_RELEASE);\n", "", "quiet"),
]
with tempfile.TemporaryDirectory() as tmp:
    if fn:
        out, log = build_run(fn, tmp, "real")
        arm("the lifted function compiles and every arm passes", out is not None and "all passed" in out, log)
        if out:
            print("     " + "\n     ".join(l for l in out.splitlines() if l.startswith(("ok", "FAIL"))))
        for name, old, new, armname in CONTROLS:
            if old not in fn:
                arm("control (%s) can be made" % name, False, "text not found: " + old[:80]); continue
            # the third control needs the copy number taken: it replaces nothing else
            if name.startswith("the copy's number"):
                bad = fn.replace(old, "", 1).replace("    if (!nvFamLock(a, false)) {\n",
                    "    if (sb && !st) copyNumber = nvaccel_pageoff_copy_begin(&res->fPoCopies);\n    if (!nvFamLock(a, false)) {\n", 1)
            else:
                bad = fn.replace(old, new, 1)
            o2, l2 = build_run(bad, tmp, "ctl")
            failed_arm = o2 is not None and any(l.startswith("FAIL " + armname) for l in o2.splitlines())
            arm("control (%s) fails its arm" % name, failed_arm, l2 if not failed_arm else "")
print("PASS" if not fails else "%d failed" % fails)
sys.exit(1 if fails else 0)
