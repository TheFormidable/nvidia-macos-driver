#!/usr/bin/env python3
"""Every path of NVResource::pageoff up to its copy, run on the host.

The family calls a page-off with the accelerator lock held. The page-off may let go of that lock while it waits for the
GPU, and has to come back with the lock, the lock-owner record, the references and the resource exactly as the family's
caller left them, whatever other threads did meanwhile. None of that can run off a Mac with the card, so the part of the
function that decides all of it (from its first line to where it asks for the surface's memory) is lifted from
kexts/NVRM/accel/nvrm-accel.cpp, together with nvFamLock, nvFamUnlock and nvPoStandDown, and run against a stand-in
accelerator lock that knows which thread holds it, a resource laid out at the offsets the driver reads, and a wait
during which other threads act: another page-off of the same resource, the family setting its bit, the owner of the
resource letting go of it, the accelerator's lock going away. The copy itself is the fixture's (two lines of pixels and
the copy's number); where the real copy takes and leaves its number is asserted by tools/test_pageoff_hold.py.

A resource that carries a plane's range (a two-plane surface) is run through the same head: it is never handed to the
family's page-off (that one copies a whole surface from offset 0), and the check after the wait reads the plane's own
validity bit.

Each arm is one path and states what the family's caller finds afterwards: who holds the lock and how often it was taken
and given back, the owner record, what was copied from where, what the page-off answered, the counters, the references.
Negative controls: v2.9's page-off (7412cdd, with the unlocked wait switched on as its opt-in did) fails the arms about
the lock that cannot be taken back, the resource everybody let go of, the copy another thread made, the family's bit
and the driver's own lock, and passes the two about paths that did not change; fifteen altered copies of today's
source each fail the arm about them.
   python3 tools/test_pageoff_paths.py"""
import pathlib, subprocess, sys, tempfile

R = pathlib.Path(__file__).resolve().parents[1]
SRC = "kexts/NVRM/accel/nvrm-accel.cpp"
fails = 0


def arm(name, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + name + (f" ({str(detail).strip()[-400:]})" if detail and not ok else ""))
    fails += not ok


def git_show(rev, path):
    r = subprocess.run(["git", "-C", str(R), "show", f"{rev}:{path}"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def head(src):
    """NVResource::pageoff from its first line to where it asks for the surface's memory: every decision before the copy."""
    a = src.find("void NVResource::pageoff(IOAccelEvent *ev, bool a1, bool *out, unsigned long long a3)\n{")
    b = src.find("    IOMemoryDescriptor *md = (IOMemoryDescriptor *)_ZNK9IOSurface19getMemoryDescriptorEv(surf);", a)
    return src[a:b] if 0 <= a < b else ""


def locks(src):
    a = src.find("static bool nvFamLock(NVAccel *a, bool tryOnly)\n{")
    b = src.find("static void nvFamUnlock(NVAccel *a)\n{", a)
    e = src.find("\n}\n", b)
    return src[a:e + 3] if 0 <= a < b < e else ""


def standdown(src):
    a = src.find("static void nvPoStandDown(const char *why)\n{")
    e = src.find("\n}\n", a)
    return src[a:e + 3] if 0 <= a < e else ""   # v2.9 has none, and its page-off calls none


# The accelerator's own one-line methods, which the fixture repeats: they must stay the source's.
ACCEL_LINES = [
    "bool mayRelease() { return nvaccel_owner_may_release(&fLockOwner, current_thread()); }",
    "bool lockPinned() { return nvaccel_owner_pinned_by(&fLockOwner, current_thread()); }",
    "void pinLock()    { nvaccel_owner_pin(&fLockOwner, current_thread()); }",
    "void unpinLock()  { nvaccel_owner_unpin(&fLockOwner, current_thread()); }",
    "nvaccel_owner_did_lock(&fLockOwner, current_thread()); }",
    "{ nvaccel_owner_will_unlock(&fLockOwner, current_thread()); IOGraphicsAccelerator2::acceleratorWillUnlock(f, l); }",
]
# The resource's one-line method the fixture repeats (kexts/NVRM/accel/nvaccel_stubs.h).
RES_LINES = ["unsigned short validBit() const { return fRangeLen ? fValidBit : (unsigned short)1; }"]

FIXTURE = r'''
#include <cstdint>
#include <cstdio>
#include <cstring>
typedef int64_t SInt64; typedef int32_t SInt32; typedef uint32_t UInt32;
#define ALOG(...) ((void)0)
#include "nvaccel_lock_owner.h"
#include "nvaccel_pageoff_recheck.h"
#include "nvrm_fence_abi.h"
#include "nvrm_surfshare_abi.h"
// ---- threads and the accelerator lock ---------------------------------------------------------------------------------
static void *const T1 = (void *)0x1001, *const T2 = (void *)0x1002;
static void *gThread = T1;
static void *current_thread() { return gThread; }
static int gBad; static const char *gBadWhy = "";     // a lock used wrongly: no arm passes after that
static void bad(const char *why) { if (!gBad++) gBadWhy = why; }
struct IOLock { void *holder; int locks, unlocks; };
static IOLock L;
static bool lockMine() { return L.holder == gThread; }
static void (*gBeforeLock)();                         // what other threads do before this one gets the lock (runs once)
static void IOLockLock(IOLock *l) {
    if (!l) { bad("a lock that is gone is taken"); return; }
    if (void (*f)() = gBeforeLock) { gBeforeLock = nullptr; f(); }
    if (l->holder) bad(l->holder == gThread ? "a thread takes the accelerator lock it already holds" : "the accelerator lock is never given back to a thread that waits for it");
    l->holder = gThread; l->locks++;
}
static bool IOLockTryLock(IOLock *l) { if (!l || l->holder) return false; l->holder = gThread; l->locks++; return true; }
static void IOLockUnlock(IOLock *l) {
    if (!l) { bad("a lock that is gone is unlocked"); return; }
    if (l->holder != gThread) bad("a thread unlocks the accelerator lock it does not hold");
    l->holder = nullptr; l->unlocks++;
}
static SInt32 OSIncrementAtomic(volatile SInt32 *p) { return (*p)++; }
static SInt32 OSDecrementAtomic(volatile SInt32 *p) { return (*p)--; }
static SInt64 OSAddAtomic64(SInt64 v, SInt64 *p) { SInt64 o = *p; *p += v; return o; }
static bool OSCompareAndSwap(UInt32 o, UInt32 n, volatile UInt32 *p) { if (*p != o) return false; *p = n; return true; }
static uint64_t gClock = 1000;
static uint64_t mach_absolute_time() { return gClock; }
static void absolutetime_to_nanoseconds(uint64_t a, uint64_t *ns) { *ns = a; }
// ---- the objects, laid out so that the offsets the driver reads fall inside `body` ------------------------------------
static int gFreed;
struct OSObject { int rc; int getRetainCount() const { return rc; } void retain() { rc++; } void release() { if (--rc <= 0) gFreed++; } };
struct IOAccelEvent;
struct NVVidMemory { void *fRmHandle; bool fScanout; unsigned long long fBytes; };
struct NVAccel : OSObject {
    unsigned char body[0x100];                        // +0x88 the family's lock, +0x90 its count of waiters
    nvaccel_lock_owner fLockOwner;
    int hooks, busy;
    bool mayRelease() { return nvaccel_owner_may_release(&fLockOwner, current_thread()); }
    bool lockPinned() { return nvaccel_owner_pinned_by(&fLockOwner, current_thread()); }
    void pinLock()    { nvaccel_owner_pin(&fLockOwner, current_thread()); }
    void unpinLock()  { nvaccel_owner_unpin(&fLockOwner, current_thread()); }
    void acceleratorDidLock(char const *, int) { hooks++; nvaccel_owner_did_lock(&fLockOwner, current_thread()); }
    void acceleratorWillUnlock(char const *, int) { nvaccel_owner_will_unlock(&fLockOwner, current_thread()); hooks--; }
    static NVAccel *resolve(NVAccel *owner) { return owner; }
};
static void _ZN22IOGraphicsAccelerator29lock_busyEv(void *a) { if (!lockMine()) bad("lock_busy without the lock"); ((NVAccel *)a)->busy++; }
static void _ZN22IOGraphicsAccelerator211unlock_busyEv(void *a) { if (!lockMine()) bad("unlock_busy without the lock"); ((NVAccel *)a)->busy--; }
struct IOAccelResource2 : OSObject {
    unsigned char body[0x100];                        // +0x14 type, +0x30 valid bits, +0x88 VRAM, +0xe0 the surface's device cache
    void pageoff(IOAccelEvent *, bool, bool *out, unsigned long long);   // the family's own page-off
};
struct NVResource : IOAccelResource2 {
    NVAccel *fAccel;
    nvaccel_pageoff_copies fPoCopies;
    unsigned long long fRangeOff = 0, fRangeLen = 0;
    unsigned short fValidBit = 0;
    unsigned short validBit() const { return fRangeLen ? fValidBit : (unsigned short)1; }
    void pageoff(IOAccelEvent *ev, bool a1, bool *out, unsigned long long a3);
};
struct Surf { unsigned id; };
struct Cache { unsigned char pad[0x10]; void *surf; };
static unsigned _ZNK9IOSurface12getSurfaceIDEv(void *s) { return ((Surf *)s)->id; }
static NVVidMemory *nvres_vidmem(IOAccelResource2 *r) { return *(NVVidMemory **)((unsigned char *)r + 0x88); }
struct NVRMRmHandles { void *kapiDevice; };
static void nvSVZero(void *p, unsigned long n) { memset(p, 0, n); }
static bool nvSVDiag() { return false; }
static bool gHandlesOk;
static bool nvAccelRmHandles(NVAccel *, struct NVRMRmHandles *h) { h->kapiDevice = (void *)1; return gHandlesOk; }
// ---- the surface waits: what is asked, with which bound, with the lock or without; other threads act meanwhile --------
static bool gPending; static int gPendingAsked;
static bool nvSurfIsPending(unsigned) { gPendingAsked++; return gPending; }
static const unsigned kSurfGpuWaitMs = 100;
static void (*gDuringWait)();                         // what other threads do while this one waits (runs once)
static int gWaits, gWaitsUnlocked; static unsigned gLastCap;
static bool nvSurfWaitReadyFor(unsigned, unsigned capMs) {
    gWaits++; gLastCap = capMs; if (!lockMine()) gWaitsUnlocked++;
    if (void (*f)() = gDuringWait) { gDuringWait = nullptr; f(); }
    gClock += 5000000;
    return true;
}
static bool nvSurfWaitReady(unsigned id) { return nvSurfWaitReadyFor(id, kSurfGpuWaitMs); }   // 2.9's name for it (the control)
static int gPoUnlocked, gPoUnlockedMaxUs;
static SInt64 gCntPoUnlocked, gCntPoNotOwner, gCntPoPinned, gCntPoAlready, gCntPoRecopied, gCntPoChanged, gCntPoStandDown;
@@STANDDOWN@@
@@LOCKS@@
static int gFamilyPageoffs, gFamilyPageoffsUnlocked;
static int gRangedFails, gRangedFailsUnlocked;            // a plane's page-off that copied nothing (never the family's)
static void nvSVRangedFail(NVResource *, bool *out, unsigned, const char *) { gRangedFails++; if (!lockMine()) gRangedFailsUnlocked++; if (out) *out = false; }
void IOAccelResource2::pageoff(IOAccelEvent *, bool, bool *out, unsigned long long) {
    gFamilyPageoffs++; if (!lockMine()) gFamilyPageoffsUnlocked++;
    if (out) *out = true;
}
static int gGpu, gSys, gCpuWrites;                    // the frame in the GPU's memory, the frame in system memory, CPU writes no copy has overwritten
static int gCopies, gCopiesUnlocked; static void *gCopySurf; static NVVidMemory *gCopyVm;
@@HEAD@@
    // The copy, in place of the function's own.
    gCopies++; if (!lockMine()) gCopiesUnlocked++;
    gCopySurf = surf; gCopyVm = vm;
    const unsigned long long modelNumber = nvaccel_pageoff_copy_begin(&fPoCopies);
    gSys = gGpu; gCpuWrites = 0;
    { volatile unsigned short *modelValid = *(unsigned short *volatile *)(r + 0x30); if (modelValid) modelValid[0] |= validBit(); }
    nvaccel_pageoff_copy_end(&fPoCopies, modelNumber);
    if (out) *out = true;
}
// ---- one accelerator, one resource with a surface in VRAM ---------------------------------------------------------------
static NVAccel *A; static NVResource *RES;
static NVVidMemory VM, VM2; static Cache CACHE, CACHE2; static Surf SURF = { 7 }, SURF2 = { 8 }; static unsigned short VALID[8];
template <class T> static T &raw(void *o, unsigned off) { return *(T *)((unsigned char *)o + off); }
static void setup() {
    A = new NVAccel(); A->rc = 3;                     // a service: the registry and its clients hold it
    RES = new NVResource(); RES->rc = 1; RES->fAccel = A;   // one owner: the table of the client that made it
    L = IOLock(); raw<IOLock *>(A, 0x88) = &L;
    VM = NVVidMemory{ (void *)0x10, false, 4096 }; VM2 = NVVidMemory{ (void *)0x20, false, 4096 };
    memset(&CACHE, 0, sizeof CACHE); memset(&CACHE2, 0, sizeof CACHE2); CACHE.surf = &SURF; CACHE2.surf = &SURF2; memset(VALID, 0, sizeof VALID);
    raw<unsigned char>(RES, 0x14) = 0xc0; raw<unsigned short *>(RES, 0x30) = VALID; raw<NVVidMemory *>(RES, 0x88) = &VM; raw<Cache *>(RES, 0xe0) = &CACHE;
    gThread = T1; gBad = 0; gBadWhy = ""; gFreed = 0; gBeforeLock = nullptr; gDuringWait = nullptr;
    gHandlesOk = true; gPending = false; gPendingAsked = 0; gWaits = gWaitsUnlocked = 0; gLastCap = 0;
    gPoUnlocked = 1; gPoUnlockedMaxUs = 0;
    gCntPoUnlocked = gCntPoNotOwner = gCntPoPinned = gCntPoAlready = gCntPoRecopied = gCntPoChanged = gCntPoStandDown = 0;
    gFamilyPageoffs = gFamilyPageoffsUnlocked = 0; gRangedFails = gRangedFailsUnlocked = 0; gGpu = 1; gSys = 0; gCpuWrites = 0; gCopies = gCopiesUnlocked = 0; gCopySurf = nullptr; gCopyVm = nullptr;
}
struct Call { bool out, lockHeld; void *owner; int depth, pinned; };
// The family's caller of a page-off on thread t: Apple's lock sequence with its two calls into the driver (or, for one
// arm, without them), the page-off, and the unlock if the page-off came back with the lock.
static Call familyPageoff(void *t, bool hooks = true) {
    void *was = gThread; gThread = t;
    IOLockLock(&L); _ZN22IOGraphicsAccelerator29lock_busyEv(A); if (hooks) A->acceleratorDidLock("", 0);
    Call c; bool out = false;
    RES->pageoff(nullptr, true, &out, 0);
    c.out = out; c.lockHeld = lockMine(); c.owner = A->fLockOwner.owner; c.depth = A->fLockOwner.depth; c.pinned = A->fLockOwner.pinned;
    if (c.lockHeld) { if (hooks) A->acceleratorWillUnlock("", 0); _ZN22IOGraphicsAccelerator211unlock_busyEv(A); IOLockUnlock(&L); }
    gThread = was;
    return c;
}
// After a page-off that came back with the lock: everything is as the family's caller left it.
static bool sane(const Call &c, void *t) {
    return !gBad && c.lockHeld && c.owner == t && c.depth == 1 && c.pinned == 0 && !L.holder && L.locks == L.unlocks
        && A->busy == 0 && A->hooks == 0 && !A->fLockOwner.owner && gFreed == 0 && gFamilyPageoffsUnlocked == 0 && gCopiesUnlocked == 0
        && gRangedFailsUnlocked == 0 && A->rc == 3;
}
// A second thread's page-off of the same resource; nothing is pending for it, so it copies what the GPU's memory holds now.
static void otherPageoff() { const bool p = gPending; gPending = false; familyPageoff(T2); gPending = p; }
static bool gSawFree;
static int failed;
static void check(const char *n, bool ok) {
    printf("%s %s\n", ok ? "ok  " : "FAIL", n);
    fflush(stdout);
    if (!ok) { failed++;
        printf("     lock misuse %d (%s); waits %d (%d without the lock, last bound %u); copies %d, family %d; sys %d gpu %d cpu %d; rc %d/%d; locks %d/%d; "
               "unlocked %lld already %lld recopied %lld changed %lld notowner %lld pinned %lld standdown %lld on %d\n",
               gBad, gBadWhy, gWaits, gWaitsUnlocked, gLastCap, gCopies, gFamilyPageoffs, gSys, gGpu, gCpuWrites, RES->rc, A->rc, L.locks, L.unlocks,
               (long long)gCntPoUnlocked, (long long)gCntPoAlready, (long long)gCntPoRecopied, (long long)gCntPoChanged, (long long)gCntPoNotOwner,
               (long long)gCntPoPinned, (long long)gCntPoStandDown, gPoUnlocked); }
}
int main() {
    // ---- paths that never let go of the lock ----
    {   setup(); raw<NVVidMemory *>(RES, 0x88) = nullptr;
        Call c = familyPageoff(T1);
        check("a resource the driver cannot copy itself goes to the family's page-off: nothing is asked, no wait, the lock is never let go",
              sane(c, T1) && gFamilyPageoffs == 1 && gPendingAsked == 0 && gWaits == 0 && gCopies == 0 && c.out && L.locks == 1 && RES->rc == 1);
    }
    {   setup();
        Call c = familyPageoff(T1);
        check("nothing pending on the surface: asked once, the wait keeps the lock and 100 ms, then the copy",
              sane(c, T1) && gPendingAsked == 1 && gWaits == 1 && gWaitsUnlocked == 0 && gLastCap == 100 && gCopies == 1 && c.out && gSys == gGpu && (VALID[0] & 1)
              && RES->fPoCopies.started == 1 && RES->fPoCopies.finished == 1 && L.locks == 1 && RES->rc == 1 && !gCntPoUnlocked && !gCntPoNotOwner && !gCntPoPinned);
    }
    {   setup(); gPending = true; gPoUnlocked = 0;
        Call c = familyPageoff(T1);
        check("the unlocked wait off (the boot-arg, or after a stand-down): the fence is not even asked, the wait keeps the lock and 100 ms, as 2.9",
              sane(c, T1) && gPendingAsked == 0 && gWaits == 1 && gWaitsUnlocked == 0 && gLastCap == 100 && gCopies == 1 && c.out && L.locks == 1 && RES->rc == 1
              && !gCntPoUnlocked && !gCntPoNotOwner && !gCntPoPinned);
    }
    {   setup(); gPending = true;
        const bool got = nvFamLock(A, true); bool out = false;
        RES->pageoff(nullptr, true, &out, 0);
        const bool heldAfter = lockMine(); const int pinnedAfter = A->fLockOwner.pinned;
        nvFamUnlock(A);
        check("a page-off reached from the driver's own locked code never lets go of that lock: 100 ms with it held, counted as pinned",
              !gBad && got && heldAfter && pinnedAfter == 1 && gWaits == 1 && gWaitsUnlocked == 0 && gLastCap == 100 && L.locks == 1 && L.unlocks == 1 && !L.holder
              && gCntPoPinned == 1 && !gCntPoNotOwner && !gCntPoUnlocked && gCopies == 1 && gCopiesUnlocked == 0 && out && A->busy == 0 && A->hooks == 0
              && !A->fLockOwner.owner && A->fLockOwner.pinned == 0 && RES->rc == 1 && A->rc == 3);
    }
    {   setup(); gPending = true;
        Call c = familyPageoff(T1, false);
        check("a caller that holds the lock without the family's did-lock call is not the recorded owner: the wait keeps the lock, counted",
              !gBad && c.lockHeld && gWaits == 1 && gWaitsUnlocked == 0 && gLastCap == 100 && gCntPoNotOwner == 1 && !gCntPoPinned && !gCntPoUnlocked
              && L.locks == 1 && L.unlocks == 1 && gCopies == 1 && gCopiesUnlocked == 0 && c.out && RES->rc == 1);
    }
    {   setup(); gPending = true;
        IOLockLock(&L); _ZN22IOGraphicsAccelerator29lock_busyEv(A); A->acceleratorDidLock("", 0); A->acceleratorDidLock("", 0);
        bool out = false; RES->pageoff(nullptr, true, &out, 0);
        const bool held = lockMine() && A->fLockOwner.owner == T1 && A->fLockOwner.depth == 2;
        A->acceleratorWillUnlock("", 0); A->acceleratorWillUnlock("", 0); _ZN22IOGraphicsAccelerator211unlock_busyEv(A); IOLockUnlock(&L);
        check("a caller that holds the lock at two levels keeps it through the wait",
              !gBad && held && gWaitsUnlocked == 0 && gLastCap == 100 && gCntPoNotOwner == 1 && gCopies == 1 && gCopiesUnlocked == 0 && out && L.locks == 1);
    }
    // ---- the unlocked wait ----
    {   setup(); gPending = true; gSawFree = false;
        gDuringWait = [] { gSawFree = !L.holder && !A->fLockOwner.owner && A->busy == 0 && A->hooks == 0 && RES->rc == 2 && A->rc == 4; };
        Call c = familyPageoff(T1);
        check("a pending surface and the family's caller owning the lock at one level: the lock is free during the wait (2 s bound) with the resource "
              "and the accelerator held, it is taken back once, the copy is made with it, and the owner record is the caller's again",
              sane(c, T1) && gSawFree && gPendingAsked == 1 && gWaits == 1 && gWaitsUnlocked == 1 && gLastCap == NVRM_FENCE_WAIT_MAX_MS && L.locks == 2
              && gCopies == 1 && c.out && gSys == gGpu && gCntPoUnlocked == 1 && !gCntPoAlready && !gCntPoRecopied && !gCntPoChanged && !gCntPoStandDown
              && gPoUnlocked == 1 && RES->rc == 1 && gPoUnlockedMaxUs == 5000);
    }
    {   setup(); gPending = true; gGpu = 0;           // the GPU has not finished the frame
        gDuringWait = [] { otherPageoff(); gGpu = 1; };   // another thread copies now, too early; then the GPU finishes and the wait ends
        Call c = familyPageoff(T1);
        check("another thread copies the surface during the wait, before the GPU has finished: this page-off copies again and its caller gets the finished frame (counted)",
              sane(c, T1) && gCopies == 2 && gSys == 1 && c.out && gCntPoRecopied == 1 && !gCntPoAlready && (VALID[0] & 1)
              && RES->fPoCopies.started == 2 && RES->fPoCopies.finished == 2 && RES->rc == 1 && L.locks == 3);
    }
    {   setup(); gPending = true;
        gDuringWait = [] { gBeforeLock = [] { otherPageoff(); gCpuWrites = 1; }; };   // after the wait has ended, while this thread asks for the lock
        Call c = familyPageoff(T1);
        check("another thread copies the surface after this wait has ended, and a CPU write follows: the copy is not made a second time, the write stays, the page-off reports done",
              sane(c, T1) && gCopies == 1 && gCpuWrites == 1 && gSys == 1 && c.out && gCntPoAlready == 1 && !gCntPoRecopied && gFamilyPageoffs == 0 && RES->rc == 1);
    }
    {   setup(); gPending = true;
        gDuringWait = [] { gBeforeLock = [] { otherPageoff(); VALID[0] &= (unsigned short)~1u; gGpu = 2; }; };   // ... and then the plugin marks a new GPU write
        Call c = familyPageoff(T1);
        check("the same, but the plugin marked a new GPU write after that copy: system memory is not current any more and this page-off copies",
              sane(c, T1) && gCopies == 2 && gSys == 2 && c.out && !gCntPoAlready && RES->rc == 1);
    }
    {   setup(); gPending = true;
        gDuringWait = [] { VALID[0] |= 1; };
        Call c = familyPageoff(T1);
        check("the family makes system memory current during the wait (no copy of the driver's): the page-off reports done and copies nothing",
              sane(c, T1) && gCopies == 0 && gFamilyPageoffs == 0 && c.out && gCntPoAlready == 1 && !gCntPoRecopied && RES->rc == 1);
    }
    {   setup(); gPending = true; VALID[0] = 1;
        Call c = familyPageoff(T1);
        check("system memory was current before the wait and nothing happened meanwhile: the copy is made, as 2.9 made it",
              sane(c, T1) && gCopies == 1 && c.out && !gCntPoAlready && !gCntPoRecopied && RES->rc == 1);
    }
    {   setup(); gPending = true;
        gDuringWait = [] { raw<NVVidMemory *>(RES, 0x88) = &VM2; raw<Cache *>(RES, 0xe0) = &CACHE2; };
        Call c = familyPageoff(T1);
        check("the resource points at another surface and VRAM allocation after the wait: counted, and the copy is made from and to what is there now",
              sane(c, T1) && gCntPoChanged == 1 && gCopies == 1 && gCopySurf == &SURF2 && gCopyVm == &VM2 && c.out && RES->rc == 1);
    }
    {   setup(); gPending = true;
        gDuringWait = [] { VM.fRmHandle = nullptr; };
        Call c = familyPageoff(T1);
        check("the driver can no longer copy the resource itself after the wait: the family's page-off, called with the lock held",
              sane(c, T1) && gFamilyPageoffs == 1 && gCopies == 0 && c.out && gCntPoUnlocked == 1 && RES->rc == 1);
    }
    {   setup(); gPending = true;
        gDuringWait = [] { gPoUnlocked = 0; };
        Call c = familyPageoff(T1);
        check("the unlocked wait is switched off for the boot while a page-off waits: that page-off still takes the lock back, copies, and leaves the owner record the caller's",
              sane(c, T1) && gWaitsUnlocked == 1 && L.locks == 2 && gCopies == 1 && c.out && RES->rc == 1);
    }
    // ---- a plane of a two-plane surface (its resource carries the plane's range and bit) ----
    {   setup(); RES->fRangeOff = 4096; RES->fRangeLen = 2048; RES->fValidBit = 2; raw<NVVidMemory *>(RES, 0x88) = nullptr;
        Call c = familyPageoff(T1);
        check("a plane's resource the driver cannot copy itself is never handed to the family's page-off: failure, the lock never let go",
              sane(c, T1) && gFamilyPageoffs == 0 && gRangedFails == 1 && !c.out && gWaits == 0 && gCopies == 0 && L.locks == 1 && RES->rc == 1);
    }
    {   setup(); gPending = true; RES->fRangeOff = 4096; RES->fRangeLen = 2048; RES->fValidBit = 2;
        gDuringWait = [] { VM.fRmHandle = nullptr; };
        Call c = familyPageoff(T1);
        check("a plane's resource the driver can no longer copy after the wait is not handed to the family's page-off either: failure, with the lock held",
              sane(c, T1) && gFamilyPageoffs == 0 && gRangedFails == 1 && !c.out && gCopies == 0 && gCntPoUnlocked == 1 && RES->rc == 1);
    }
    {   setup(); gPending = true; RES->fRangeOff = 4096; RES->fRangeLen = 2048; RES->fValidBit = 2;
        gDuringWait = [] { VALID[0] |= 2; };
        Call c = familyPageoff(T1);
        check("the family makes a plane's own bit current during the wait: the page-off reports done and copies nothing",
              sane(c, T1) && gCopies == 0 && gFamilyPageoffs == 0 && gRangedFails == 0 && c.out && gCntPoAlready == 1 && RES->rc == 1);
    }
    {   setup(); gPending = true; RES->fRangeOff = 4096; RES->fRangeLen = 2048; RES->fValidBit = 2;
        gDuringWait = [] { VALID[0] |= 1; };
        Call c = familyPageoff(T1);
        check("bit 0 set during the wait says nothing about a plane that uses bit 1: the page-off copies, and sets the plane's bit",
              sane(c, T1) && gCopies == 1 && c.out && !gCntPoAlready && (VALID[0] & 2) && RES->rc == 1);
    }
    {   setup(); gPending = true; VALID[0] = 2;      // the family keeps this resource on bit 1, and system memory reads current on it
        gDuringWait = [] { RES->fRangeOff = 4096; RES->fRangeLen = 2048; RES->fValidBit = 2; };   // a plane's first grant, while the lock is free
        Call c = familyPageoff(T1);
        check("a plane's range granted during the wait: the bit read before it (bit 0) and after it (the plane's) are not compared, and the copy is made",
              sane(c, T1) && gCopies == 1 && c.out && !gCntPoAlready && gCntPoChanged == 1 && gFamilyPageoffs == 0 && gRangedFails == 0 && RES->rc == 1);
    }
    // ---- what the unlocked wait must survive ----
    {   setup(); gPending = true;
        gDuringWait = [] { raw<IOLock *>(A, 0x88) = nullptr; };   // the accelerator is torn down: its lock is gone
        Call c = familyPageoff(T1);
        const bool first = !gBad && !c.lockHeld && !c.out && gCopies == 0 && gFamilyPageoffs == 0 && gPoUnlocked == 0 && gCntPoStandDown == 1 && !gCntPoUnlocked
            && RES->rc == 1 && A->rc == 3 && gFreed == 0 && !L.holder && L.locks == 1 && L.unlocks == 1 && A->busy == 0 && A->hooks == 0;
        raw<IOLock *>(A, 0x88) = &L;
        Call d = familyPageoff(T1);
        check("the lock cannot be taken back: nothing is read or copied without it, the page-off reports failure, and the unlocked wait is off from then on "
              "(the next page-off of a pending surface waits with the lock held)",
              first && sane(d, T1) && gWaits == 2 && gWaitsUnlocked == 1 && gLastCap == 100 && gCopies == 1 && d.out && gCntPoStandDown == 1);
    }
    {   setup(); gPending = true;
        gDuringWait = [] { RES->release();            // its owner lets go: only the page-off's hold is left
                           raw<NVVidMemory *>(RES, 0x88) = &VM2; raw<Cache *>(RES, 0xe0) = &CACHE2; };   // what it pointed at went with the owner
        Call c = familyPageoff(T1);
        check("everybody else lets go of the resource during the wait: it is not freed under the caller and nothing is read through it; the page-off reports "
              "failure with the lock held, keeps that last reference, and the unlocked wait stands down",
              !gBad && c.lockHeld && c.owner == T1 && c.depth == 1 && c.pinned == 0 && !c.out && gFreed == 0 && RES->rc == 1 && A->rc == 3 && gCopies == 0
              && gFamilyPageoffs == 0 && !gCntPoChanged && gPoUnlocked == 0 && gCntPoStandDown == 1 && gCntPoUnlocked == 1 && !L.holder && L.locks == 2 && L.unlocks == 2
              && A->busy == 0 && A->hooks == 0);
    }
    {   setup(); gPending = true;
        gDuringWait = [] { A->release(); A->release(); A->release(); };   // everything else lets go of the accelerator: only the page-off's hold is left
        Call c = familyPageoff(T1);
        check("everybody else lets go of the accelerator during the wait: it is not freed under the caller, the page-off finishes with the lock, "
              "keeps that last reference, and the unlocked wait stands down",
              !gBad && c.lockHeld && c.owner == T1 && c.depth == 1 && c.pinned == 0 && c.out && gCopies == 1 && gCopiesUnlocked == 0 && gFreed == 0
              && A->rc == 1 && RES->rc == 1 && gPoUnlocked == 0 && gCntPoStandDown == 1 && !L.holder && L.locks == 2 && L.unlocks == 2 && A->busy == 0 && A->hooks == 0);
    }
    return failed ? 1 : 0;
}
'''


def run_fixture(src_head, src_locks, src_standdown, tmp, tag):
    cpp = pathlib.Path(tmp) / f"pageoff_{tag}.cpp"
    binp = pathlib.Path(tmp) / f"pageoff_{tag}"
    cpp.write_text(FIXTURE.replace("@@STANDDOWN@@", src_standdown).replace("@@LOCKS@@", src_locks).replace("@@HEAD@@", src_head))
    c = subprocess.run(["c++", "-std=c++17", "-O0", "-fno-strict-aliasing", "-Wall", "-Wno-unused-function", "-Wno-unused-variable",
                        "-Wno-unused-parameter", "-Wno-unused-but-set-variable", "-I", str(R / "kexts/NVRM/accel"), "-I", str(R / "kexts/NVRM"),
                        str(cpp), "-o", str(binp)], capture_output=True, text=True)
    if c.returncode != 0:
        return None, c.stderr
    r = subprocess.run([str(binp)], capture_output=True, text=True, timeout=60)
    if r.returncode not in (0, 1):
        return None, f"the program ended with {r.returncode}\n" + r.stdout + r.stderr
    return {l[5:]: l.startswith("ok") for l in r.stdout.splitlines() if l[:4] in ("ok  ", "FAIL")}, r.stdout + r.stderr


# arm names, as the fixture prints them (start of each is enough to find it)
def find(res, start):
    hits = [n for n in res if n.startswith(start)]
    return hits[0] if len(hits) == 1 else None


NOT_ELIGIBLE = "a resource the driver cannot copy itself"
SWITCHED_OFF = "the unlocked wait off (the boot-arg, or after a stand-down)"
PINNED = "a page-off reached from the driver's own locked code"
UNLOCKED = "a pending surface and the family's caller owning the lock at one level"
EARLY = "another thread copies the surface during the wait, before the GPU has finished"
LATE = "another thread copies the surface after this wait has ended, and a CPU write follows"
FAMILY_BIT = "the family makes system memory current during the wait"
PLANE_NO_FAMILY = "a plane's resource the driver cannot copy itself"
PLANE_NO_FAMILY_LATE = "a plane's resource the driver can no longer copy after the wait"
PLANE_BIT = "the family makes a plane's own bit current during the wait"
PLANE_BIT0 = "bit 0 set during the wait says nothing about a plane"
PLANE_LATE_GRANT = "a plane's range granted during the wait"
NO_RELOCK = "the lock cannot be taken back"
ORPHAN = "everybody else lets go of the resource during the wait"
ACCEL_GONE = "everybody else lets go of the accelerator during the wait"

src = (R / SRC).read_text()
arm("the fixture's accelerator has the source's own lock-owner methods, word for word",
    all(src.count(l) == 1 for l in ACCEL_LINES), [l for l in ACCEL_LINES if src.count(l) != 1])
stubs = (R / "kexts/NVRM/accel/nvaccel_stubs.h").read_text()
arm("the fixture's resource has the source's own validity-bit method, word for word",
    all(stubs.count(l) == 1 for l in RES_LINES), [l for l in RES_LINES if stubs.count(l) != 1])
h, lk, sd = head(src), locks(src), standdown(src)
arm("the page-off's head, nvFamLock, nvFamUnlock and nvPoStandDown are found in the source", bool(h and lk and sd))
RELOCK = "            if (!nvFamLock(a, false)) {\n"
READ1 = "            const unsigned long long copies1 = nvaccel_pageoff_copies_started(&fPoCopies);\n"
WAIT = "            nvSurfWaitReadyFor(sid, NVRM_FENCE_WAIT_MAX_MS);"
CONTROLS = [   # what, which piece, [(the text to alter, its replacement), ...], the arm that must then fail
    ("the earlier condition (bit clear before, set now) in place of the rule", "head",
     [("nvaccel_pageoff_already_done(&fPoCopies, wasCurrent, currentNow, copies0, copies1)", "(!wasCurrent && currentNow)")], EARLY),
    ("the copies begun read before the wait instead of when it ends", "head", [(READ1, ""), (WAIT, READ1 + WAIT)], EARLY),
    ("the result of taking the lock back not tested", "head", [(RELOCK, "            nvFamLock(a, false); if (false) {\n")], NO_RELOCK),
    ("a resource only the page-off still holds read through", "head",
     [("            if (getRetainCount() <= 1) { if (out) *out = false; return; }\n", "")], ORPHAN),
    ("the last reference released at return", "head",
     [('if (o->getRetainCount() <= 1) { nvPoStandDown("the page-off held the last reference"); return; } ', "")], ORPHAN),
    ("the resource not held across the wait", "head", [("            retain(); holdRes.o = this;\n", "")], ORPHAN),
    ("the accelerator not held across the wait", "head", [("            a->retain(); holdAccel.o = a;\n", "")], ACCEL_GONE),
    ("no unpin after the lock is back", "head",
     [("            a->unpinLock();   // nvFamLock pins the driver's own locks; this one is the family's caller's again\n", "")], UNLOCKED),
    ("the driver's own lock not pinned", "locks", [("    if (gPoUnlocked) a->pinLock();\n", "")], PINNED),
    ("the wait made before the lock is let go", "head",
     [("            nvFamUnlock(a);\n" + WAIT, WAIT + "\n            nvFamUnlock(a);")], UNLOCKED),
    ("a plane's resource handed to the family's page-off before the wait", "head",
     [('        if (fRangeLen) { nvSVRangedFail(this, out, NVRM_SS_NO_VRAM, "no surface, VRAM or RM handles"); return; }', "")], PLANE_NO_FAMILY),
    ("a plane's resource handed to the family's page-off after the wait", "head",
     [('                if (fRangeLen) { nvSVRangedFail(this, out, NVRM_SS_NO_VRAM, "no surface, VRAM or RM handles after the wait"); return; }', "")],
     PLANE_NO_FAMILY_LATE),
    ("bit 0 read before and after the wait whatever the resource", "head",
     [("(v0[0] & validBit())", "(v0[0] & 1u)"), ("(v1[0] & validBit())", "(v1[0] & 1u)")], PLANE_BIT),
    ("bit 0 read after the wait whatever the resource (a copy skipped on another plane's bit)", "head",
     [("(v1[0] & validBit())", "(v1[0] & 1u)")], PLANE_BIT0),
    ("the two readings compared although the resource's bit changed during the wait", "head",
     [("            if (!bitChanged) {   volatile unsigned short *const v1 =", "            {   volatile unsigned short *const v1 =")], PLANE_LATE_GRANT),
]
with tempfile.TemporaryDirectory() as tmp:
    res, log = run_fixture(h, lk, sd, tmp, "real") if h and lk and sd else (None, "pieces of the source were not found")
    if res is None:
        arm("the lifted page-off compiles and runs on the host", False, log)
        res = {}
    else:
        arm("the lifted page-off compiles and runs on the host (23 paths)", len(res) == 23, log)
        for n, ok in res.items():
            arm("path: " + n, ok, log)
    for what, piece, changes, target in CONTROLS:
        alt = h if piece == "head" else lk
        missing = [o for o, _ in changes if alt.count(o) != 1]
        if missing:
            arm(f"control ({what}): the text to alter is in the source once", False, missing)
            continue
        for old_s, new_s in changes:
            alt = alt.replace(old_s, new_s)
        cres, clog = run_fixture(alt if piece == "head" else h, alt if piece == "locks" else lk, sd, tmp, "ctl")
        name = find(cres, target) if cres else None
        arm(f"control ({what}) fails its arm", bool(name) and cres[name] is False, clog)
    v29 = git_show("7412cdd", SRC)
    if v29 is None or not head(v29) or not locks(v29):
        arm("control: v2.9's page-off (7412cdd) is readable", False)
    else:
        ores, olog = run_fixture(head(v29), locks(v29), "", tmp, "v29")
        if ores is None:
            arm("control: v2.9's page-off compiles against the same fixture", False, olog)
        else:
            must_fail = [NO_RELOCK, ORPHAN, LATE, FAMILY_BIT, PINNED]
            got = {s: ores.get(find(ores, s)) for s in must_fail + [NOT_ELIGIBLE, SWITCHED_OFF]}
            arm("control: v2.9's page-off, with its opt-in on, fails the arms about the lock that cannot be taken back, the resource everybody let "
                "go of, the copy another thread made, the family's bit and the driver's own lock",
                all(got[s] is False for s in must_fail), got)
            arm("control: v2.9's page-off passes the arms about the two paths that did not change (the family's page-off, the wait with the lock held)",
                got[NOT_ELIGIBLE] is True and got[SWITCHED_OFF] is True, got)

print(f"{fails} failed" if fails else "all passed")
sys.exit(1 if fails else 0)
