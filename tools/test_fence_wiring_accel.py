#!/usr/bin/env python3
"""The accelerator's waits for WindowServer's GPU work go through NVRM's surface fence, and fall back to the 2.9 marks.

NVAccel asks NVRM once for the fence's wait function (platform function nvFenceOps) and keeps the pointer. Every wait
for a surface (the flip, the copied frame, the hybrid paths, the page-off) asks the fence first and then the 2.9 mark
table; with no fence (an older NVRM, boot-arg nvrmfence=0, a fence that did not start) the mark wait is the whole wait,
exactly as in 2.9.

Two kinds of arms:
- text arms on kexts/NVRM/accel/nvrm-accel.cpp (where each call sits, which bound each site passes, the counters);
- behaviour arms: the wait functions are lifted from the source and run in a host program against a stand-in NVRM and
  a fake clock: no fence = 2.9 to the nanosecond, the bounds of the locked and the unlocked page-off, a dead writer
  counts as done, a refusing NVRM is asked again at most once a second, a page-off sees a hold the fence knows.
Negative controls: v2.9's source (7412cdd) fails the text arms; three altered copies of the lifted code (a dead writer
treated as a timeout, the mark wait given its own full bound after a fence wait, no rate limit on asking again) each
fail the arm about them.
   python3 tools/test_fence_wiring_accel.py"""
import pathlib, re, subprocess, sys, tempfile

R = pathlib.Path(__file__).resolve().parents[1]
SRC = "kexts/NVRM/accel/nvrm-accel.cpp"
fails = 0


def arm(name, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + name + (f" ({str(detail).strip()[-300:]})" if detail and not ok else ""))
    fails += not ok


def git_show(rev, path):
    r = subprocess.run(["git", "-C", str(R), "show", f"{rev}:{path}"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def func(src, head):
    if head not in src:
        return ""
    a = src.index(head)
    e = src.find("\n}\n", a)
    return src[a:e if e > 0 else len(src)]


def text_arms(src):
    """name -> bool, for one version of the source."""
    out = {}
    out["the fence ABI is included and its wait type matches NVRMFenceKernelOps.wait"] = (
        '#include "../nvrm_fence_abi.h"' in src
        and "typedef unsigned (*NVRMFenceWaitFn)(struct NVRMFenceWait *w, unsigned flags);" in src)
    ask = func(src, "static NVRMFenceWaitFn nvFenceAsk(IOService *nvrm)\n{")
    out["nvFenceOps is asked through callPlatformFunction with version and size; only success with a pointer is kept"] = (
        "OSSymbol::withCStringNoCopy(NVRM_FENCE_FN_OPS)" in ask and "o.version = NVRM_FENCE_ABI_VERSION; o.size = sizeof o;" in ask
        and "nvrm->callPlatformFunction(sym, false, &o," in ask and "if (r != kIOReturnSuccess || !o.wait) {" in ask
        and "__atomic_store_n(&gFenceWait, (NVRMFenceWaitFn)o.wait, __ATOMIC_RELEASE);" in ask
        and ask.find("if (r != kIOReturnSuccess || !o.wait) {") < ask.find("__atomic_store_n(&gFenceWait"))
    ops = func(src, "static NVRMFenceWaitFn nvFenceOps(void)\n{")
    out["a wait never waits for NVRM to match: it asks only an NVRM already found, at most once a second"] = (
        "a ? a->fNVRM : nullptr" in ops and "locateNVRM" not in ops and "waitForMatchingService" not in ops
        and "gFenceAskAgainAt && mach_absolute_time() < gFenceAskAgainAt" in ops
        and "clock_interval_to_deadline(1, kSecondScale, &next);" in ask)
    h = src.find("virtual bool start(IOService *provider) APPLE_KEXT_OVERRIDE {")
    start = src[h:h + 20000] if h >= 0 else ""
    p, q = start.find('setProperty("NVAccelSurfaceWait", 1ULL, 32);'), start.find("nvFenceAsk(locateNVRM());")
    out["start asks NVRM for the fence right after it publishes NVAccelSurfaceWait"] = 0 <= p < q and start.count("nvFenceAsk(") == 1
    wf = func(src, "static bool nvSurfWaitReadyFor(unsigned int id, unsigned capMs)\n{")
    rd, dd, nf = wf.find("w.result == NVRM_FENCE_R_READY"), wf.find("w.result == NVRM_FENCE_R_DEAD"), wf.find("ready = false;")
    out["the fence wait: compositor holds, the caller's bound, a dead writer counts as done, then the 2.9 marks"] = (
        "if (!id || !gSurfGpuOn) return true;" in wf and "w.timeoutMs = capMs;" in wf
        and "fw(&w, NVRM_FENCE_F_COMPOSITOR);" in wf and 0 <= rd < dd < nf
        and wf.rstrip().endswith("return nvSurfWaitLegacy(id, limit) && ready;   // marks of an older plugin, or of a hold the kernel refused"))
    out["nvSurfWaitReady keeps its name and signature and waits up to the fence's maximum"] = (
        "static bool nvSurfWaitReady(unsigned int id) { return nvSurfWaitReadyFor(id, NVRM_FENCE_WAIT_MAX_MS); }" in src
        and src.count("static bool nvSurfWaitReady(unsigned int id);") == 1)
    out["the flip, the async flip, the copied frame and both hybrid paths still call nvSurfWaitReady"] = all(s in src for s in (
        "nvSurfWaitReady(s->getSurfaceID());   // a copied frame is read here",
        "nvSurfWaitReady(s->getSurfaceID());   // WindowServer did not wait for this composite",
        "if (gIopAsync && pipe) return nvAccelIopAsyncStart(pipe, fb, head, s, &f);\n    nvSurfWaitReady(s->getSurfaceID());",
        "nvSurfWaitReady(x->id); return 0; }",
        "nvSurfWaitReady(id);\n    const unsigned long long len"))
    po = func(src, "void NVResource::pageoff(")
    u = po.find("nvFamUnlock(a);")
    rl = po.find("if (!nvFamLock(a, false)) {", u)
    el = po.find("} else {", u)
    unl = po[u:rl] if 0 <= u < rl else ""
    locked = po[el:po.find("\n    }\n", el)] if el > 0 else ""
    out["the unlocked page-off waits up to the fence's maximum, between the unlock and the relock"] = (
        unl.count("nvSurfWaitReadyFor(sid, NVRM_FENCE_WAIT_MAX_MS);") == 1 and "nvSurfWaitReady(" not in unl)
    out["the page-off that holds the accelerator lock keeps 2.9's 100 ms (the only such call in the file)"] = (
        src.count("nvSurfWaitReadyFor(sid, kSurfGpuWaitMs);") == 1 and locked.count("nvSurfWaitReadyFor(sid, kSurfGpuWaitMs);") == 1
        and "static const unsigned kSurfGpuWaitMs = 100;" in src)
    out["no page-off wait goes around the fence (no nvSurfWaitReady or nvSurfWaitLegacy in pageoff)"] = (
        po != "" and "nvSurfWaitReady(" not in po and "nvSurfWaitLegacy(" not in po)
    ip = func(src, "static bool nvSurfIsPending(unsigned int id)\n{")
    out["a hold the fence knows makes a surface pending (a look, no sleep), and so does a 2.9 mark"] = (
        "fw(&w, NVRM_FENCE_F_COMPOSITOR | NVRM_FENCE_F_LOOK) == NVRM_FENCE_ST_OK && w.result == NVRM_FENCE_R_PENDING" in ip
        and "w.timeoutMs = 0;" in ip and "for (unsigned i = 0; i < 32 && !p; i++) p = gSurfPend[i].id == id;" in ip)
    names = ["nvaccel_surface_fence", "nvaccel_surface_fence_waits", "nvaccel_surface_fence_ready", "nvaccel_surface_fence_dead",
             "nvaccel_surface_fence_timeouts", "nvaccel_surface_fence_errors", "nvaccel_surface_fence_pending",
             "nvaccel_surface_fence_wait_max_us"]
    inst = func(src, "static void nvaccelInstallSysctl(void)\n{")
    nmx = inst.find("#else")
    ok = nmx > 0
    for n in names:
        decl = re.findall(rf"(?:SYSCTL_INT\(_debug, OID_AUTO, {n}, (CTLFLAG_[A-Z]+) \| CTLFLAG_LOCKED|NVACCEL_CNT\({n},)", src)
        ok = ok and len(decl) == 1 and decl[0] in ("", "CTLFLAG_RD") and inst[nmx:].count(f"sysctl_register_oid(&sysctl__debug_{n});") == 1
    out["the debug.nvaccel_surface_fence_* sysctls are read-only, declared once and registered once"] = ok
    return out


def lifted(src):
    a = src.find("static int gSurfGpuOn = 1;")
    b = src.find("// Unlocked page-off wait.")
    return src[a:b] if 0 <= a < b else ""


FIXTURE = r'''
#include <cstdint>
#include <cstdio>
#include <cstring>
typedef int64_t SInt64; typedef int32_t SInt32; typedef uint32_t UInt32; typedef int IOReturn;
enum : int { kIOReturnSuccess = 0, kIOReturnUnsupported = (int)0xe00002c7, kIOReturnNoMemory = (int)0xe00002bd };
#define ALOG(...) ((void)0)
#include "nvrm_fence_abi.h"
typedef unsigned (*NVRMFenceWaitFn)(struct NVRMFenceWait *w, unsigned flags);
static uint64_t gClock = 1000000;                       // absolute time = nanoseconds here
static uint64_t mach_absolute_time() { return gClock; }
enum : uint32_t { kMillisecondScale = 1000000u, kSecondScale = 1000000000u, THREAD_UNINT = 0 };
static void clock_interval_to_deadline(uint32_t v, uint32_t sc, uint64_t *out) { *out = gClock + (uint64_t)v * sc; }
static void absolutetime_to_nanoseconds(uint64_t a, uint64_t *ns) { *ns = a; }
static SInt64 OSAddAtomic64(SInt64 v, SInt64 *p) { SInt64 o = *p; *p += v; return o; }
struct IOLock { int held; }; static IOLock gL;
static IOLock *IOLockAlloc() { return &gL; } static void IOLockFree(IOLock *) {}
static bool OSCompareAndSwapPtr(void *o, void *n, void *volatile *p) { if (*p != o) return false; *p = n; return true; }
static int gBad;
static void IOLockLock(IOLock *l) { if (l->held) gBad++; l->held = 1; } static void IOLockUnlock(IOLock *l) { if (!l->held) gBad++; l->held = 0; }
static void IOLockWakeup(IOLock *, void *, bool) {}
static unsigned gClearId; static uint64_t gClearAt;     // the plugin clears this mark at this time
struct NVSurfPending;
static void nvClearMark(unsigned id);
static int IOLockSleepDeadline(IOLock *l, void *, uint64_t deadline, int) {
    if (!l->held) gBad++;
    if (gClearId && gClearAt < deadline) { if (gClearAt > gClock) gClock = gClearAt; nvClearMark(gClearId); gClearId = 0; }
    else if (deadline > gClock) gClock = deadline;
    return 0;
}
struct OSSymbol { static const OSSymbol *withCStringNoCopy(const char *) { static OSSymbol s; return &s; } void release() const {} };
// the stand-in NVRM: answers nvFenceOps as told; its wait() answers as scripted and spends the scripted time
static int gAsks; static IOReturn gAnswer = kIOReturnUnsupported;
static unsigned gFenceStatus = NVRM_FENCE_ST_OK, gFenceResult = NVRM_FENCE_R_NONE, gFenceTakesMs, gLookPending;
static unsigned gLastFlags, gLastTimeout, gFenceCalls;
static unsigned fakeWait(struct NVRMFenceWait *w, unsigned flags) {
    gFenceCalls++; gLastFlags = flags; gLastTimeout = w->timeoutMs;
    if (flags & NVRM_FENCE_F_LOOK) { w->result = gLookPending ? NVRM_FENCE_R_PENDING : NVRM_FENCE_R_NONE; return gFenceStatus; }
    unsigned t = gFenceTakesMs, cap = w->timeoutMs < NVRM_FENCE_WAIT_MAX_MS ? w->timeoutMs : NVRM_FENCE_WAIT_MAX_MS;
    if (gFenceResult != NVRM_FENCE_R_NONE && t >= cap) { gClock += (uint64_t)cap * 1000000; w->result = NVRM_FENCE_R_BUDGET; return gFenceStatus; }
    if (gFenceResult != NVRM_FENCE_R_NONE) gClock += (uint64_t)t * 1000000;
    w->result = gFenceResult; return gFenceStatus;
}
struct IOService { IOReturn callPlatformFunction(const OSSymbol *, bool, void *p1, void *, void *, void *) {
    gAsks++; struct NVRMFenceKernelOps *o = (struct NVRMFenceKernelOps *)p1;
    if (gAnswer == kIOReturnSuccess && o->version == NVRM_FENCE_ABI_VERSION && o->size == sizeof *o) o->wait = fakeWait;
    return gAnswer; } };
static IOService gNVRM;
struct NVAccel { IOService *fNVRM; static NVAccel *resolve(NVAccel *); };
static NVAccel gA = { &gNVRM };
NVAccel *NVAccel::resolve(NVAccel *) { return &gA; }
#define NVACCEL_CNT(nm, var, desc)
#define SYSCTL_INT(...)
@@LIFTED@@
static void nvClearMark(unsigned id) { for (unsigned i = 0; i < 32; i++) if (gSurfPend[i].id == id) gSurfPend[i].id = 0; }
static int failed;
static void check(const char *n, bool ok) { printf("%s %s\n", ok ? "ok  " : "FAIL", n); failed += !ok; }
static uint64_t ms(uint64_t a) { return a / 1000000; }
int main() {
    // A: an NVRM without the fence: the 2.9 mark wait, to the nanosecond
    {
        bool ok = true;
        uint64_t t = gClock; ok &= nvSurfWaitReady(5) && gClock == t;               // nothing marked: no wait at all
        nvSurfMark(5, true); gClearId = 5; gClearAt = gClock + 30 * 1000000ull;
        t = gClock; ok &= nvSurfWaitReady(5) && gClock - t == 30 * 1000000ull && gCntSurfWaits == 1;
        nvSurfMark(6, true); t = gClock;
        ok &= !nvSurfWaitReady(6) && gClock - t == 100 * 1000000ull && gCntSurfTimeouts == 1 && !nvSurfIsPending(6);
        nvSurfMark(7, true); t = gClock;
        ok &= !nvSurfWaitReadyFor(7, kSurfGpuWaitMs) && gClock - t == 100 * 1000000ull;
        ok &= gFenceCalls == 0 && gCntFenceWaits == 0 && gFenceOn == 0;
        check("no fence: every wait is the 2.9 mark wait (none, cleared at 30 ms, given up at 100 ms)", ok);
    }
    // B: asked again at most once a second while NVRM refuses; never again once it answers
    {
        bool ok = true; gAsks = 0; gClock += 2000ull * 1000000;
        nvSurfWaitReady(9); ok &= gAsks == 1;
        gClock += 500ull * 1000000; nvSurfWaitReady(9); nvSurfIsPending(9); ok &= gAsks == 1;
        gClock += 600ull * 1000000; gAnswer = kIOReturnSuccess; nvSurfWaitReady(9); ok &= gAsks == 2 && gFenceOn == 1;
        gClock += 5000ull * 1000000; nvSurfWaitReady(9); nvSurfIsPending(9); ok &= gAsks == 2;
        check("a refusing NVRM is asked again at most once a second, and not at all once it answered", ok);
    }
    // C: READY after 40 ms: compositor holds, the caller's bound
    {
        gFenceResult = NVRM_FENCE_R_READY; gFenceTakesMs = 40; uint64_t t = gClock;
        bool ok = nvSurfWaitReady(11) && ms(gClock - t) == 40 && gCntFenceReady == 1 && gCntFenceWaits == 1
                  && gLastFlags == NVRM_FENCE_F_COMPOSITOR && gLastTimeout == NVRM_FENCE_WAIT_MAX_MS;
        t = gClock; ok &= nvSurfWaitReadyFor(11, kSurfGpuWaitMs) && gLastTimeout == 100 && ms(gClock - t) == 40;
        check("a hold the GPU completes ends the wait (compositor holds only, the caller's bound passed)", ok);
    }
    // D: the writer died: done, at once, no timeout
    {
        gFenceResult = NVRM_FENCE_R_DEAD; gFenceTakesMs = 1; uint64_t t = gClock; SInt64 to = gCntFenceTimeouts;
        bool ok = nvSurfWaitReady(12) && ms(gClock - t) == 1 && gCntFenceDead == 1 && gCntFenceTimeouts == to;
        check("a hold whose writer exited counts as done: the wait returns at once and reports ready", ok);
    }
    // E: TIMEOUT from the hold's own bound: not ready, counted
    {
        gFenceResult = NVRM_FENCE_R_TIMEOUT; gFenceTakesMs = 100; SInt64 to = gCntFenceTimeouts;
        bool ok = !nvSurfWaitReady(13) && gCntFenceTimeouts == to + 1;
        check("a hold ended by its safety bound reports not ready and is counted", ok);
    }
    // F: the page-off that holds the accelerator lock: fence and mark together within 100 ms
    {
        gFenceResult = NVRM_FENCE_R_READY; gFenceTakesMs = 5000; nvSurfMark(14, true); uint64_t t = gClock;
        bool ok = !nvSurfWaitReadyFor(14, kSurfGpuWaitMs) && gClock - t <= 100 * 1000000ull;
        check("with the accelerator lock held the whole wait (fence, then mark) stays within 100 ms", ok);
    }
    // G: the unlocked page-off and the flip: within the fence's maximum
    {
        gFenceResult = NVRM_FENCE_R_READY; gFenceTakesMs = 5000; nvSurfMark(15, true); uint64_t t = gClock;
        bool ok = !nvSurfWaitReadyFor(15, NVRM_FENCE_WAIT_MAX_MS) && gClock - t <= NVRM_FENCE_WAIT_MAX_MS * 1000000ull
                  && gClock - t >= NVRM_FENCE_WAIT_MAX_MS * 1000000ull;
        check("with no lock held the whole wait stays within the fence's maximum (2 s)", ok);
    }
    // H: nothing in the fence, a 2.9 mark from an older plugin: the mark is still waited for
    {
        gFenceResult = NVRM_FENCE_R_NONE; nvSurfMark(16, true); gClearId = 16; gClearAt = gClock + 50 * 1000000ull;
        uint64_t t = gClock; bool ok = nvSurfWaitReady(16) && ms(gClock - t) == 50;
        check("a 2.9 mark is still waited for when the fence has nothing on the surface", ok);
    }
    // I: pending
    {
        bool ok = true; gLookPending = 1; unsigned c = gFenceCalls;
        ok &= nvSurfIsPending(17) && gLastFlags == (NVRM_FENCE_F_COMPOSITOR | NVRM_FENCE_F_LOOK) && gLastTimeout == 0 && gCntFencePending == 1;
        gLookPending = 0; ok &= !nvSurfIsPending(17);
        nvSurfMark(18, true); ok &= nvSurfIsPending(18); nvSurfMark(18, false);
        gSurfGpuOn = 0; c = gFenceCalls; gLookPending = 1;
        ok &= !nvSurfIsPending(17) && nvSurfWaitReady(17) && gFenceCalls == c;
        gSurfGpuOn = 1; gLookPending = 0;
        check("a page-off sees a hold the fence knows, or a mark; the 2.9 switch turns both off", ok);
    }
    // J: NVRM does not serve the wait: counted, the marks do the work
    {
        gFenceStatus = NVRM_FENCE_ST_OFF; gFenceResult = NVRM_FENCE_R_READY; gFenceTakesMs = 10;
        nvSurfMark(19, true); gClearId = 19; gClearAt = gClock + 20 * 1000000ull; uint64_t t = gClock; SInt64 e = gCntFenceErrors;
        bool ok = nvSurfWaitReady(19) && ms(gClock - t) == 20 && gCntFenceErrors == e + 1;
        gFenceStatus = NVRM_FENCE_ST_OK;
        check("a wait NVRM does not serve is counted and the 2.9 mark wait runs", ok);
    }
    check("every lock taken was given back", gBad == 0 && gL.held == 0);
    return failed ? 1 : 0;
}
'''


def run_fixture(region, tmp, tag):
    cpp = pathlib.Path(tmp) / f"fence_{tag}.cpp"
    binp = pathlib.Path(tmp) / f"fence_{tag}"
    cpp.write_text(FIXTURE.replace("@@LIFTED@@", region))
    c = subprocess.run(["c++", "-std=c++17", "-O0", "-Wall", "-Wno-unused-function", "-Wno-unused-variable",
                        "-Wno-unused-parameter", "-Wno-unused-but-set-variable", "-I", str(R / "kexts/NVRM"),
                        str(cpp), "-o", str(binp)], capture_output=True, text=True)
    if c.returncode != 0:
        return None, c.stderr
    r = subprocess.run([str(binp)], capture_output=True, text=True, timeout=60)
    return {l[5:]: l.startswith("ok") for l in r.stdout.splitlines() if l[:4] in ("ok  ", "FAIL")}, r.stdout + r.stderr


src = (R / SRC).read_text()
for name, ok in text_arms(src).items():
    arm(name, ok)
old = git_show("7412cdd", SRC)
if old is None:
    arm("control: v2.9's source is readable", False, "git show 7412cdd failed")
else:
    # the call sites are 2.9's and stay as they were: that arm is the only one v2.9 passes too
    kept = {"the flip, the async flip, the copied frame and both hybrid paths still call nvSurfWaitReady"}
    old_res = text_arms(old)
    bad = [n for n, ok in old_res.items() if ok and n not in kept]
    arm("control: v2.9's source fails every text arm about the fence", not bad, bad)
    arm("control: v2.9's source has the same five call sites", all(old_res[n] for n in kept))
    # the mark wait is 2.9's body, with the one added line that lets a caller pass an earlier deadline
    v29 = func(old, "static bool nvSurfWaitReady(unsigned int id)\n{")
    leg = func(src, "static bool nvSurfWaitLegacy(unsigned int id, uint64_t limit)\n{")
    arm("the mark wait is 2.9's, line for line, plus the earlier-deadline line",
        v29 != "" and leg.replace("static bool nvSurfWaitLegacy(unsigned int id, uint64_t limit)", "static bool nvSurfWaitReady(unsigned int id)")
        .replace("    if (limit && limit < deadline) deadline = limit;\n", "") == v29)

region = lifted(src)
CONTROLS = [
    ("a dead writer treated as a timeout",
     "            else if (w.result == NVRM_FENCE_R_DEAD) OSAddAtomic64(1, &gCntFenceDead);\n", "",
     "a hold whose writer exited counts as done: the wait returns at once and reports ready"),
    ("the mark wait given its own full bound after a fence wait",
     "return nvSurfWaitLegacy(id, limit) && ready;", "return nvSurfWaitLegacy(id, 0) && ready;",
     "with the accelerator lock held the whole wait (fence, then mark) stays within 100 ms"),
    ("no rate limit on asking a refusing NVRM again",
     "    if (gFenceAskAgainAt && mach_absolute_time() < gFenceAskAgainAt) return nullptr;\n", "",
     "a refusing NVRM is asked again at most once a second, and not at all once it answered"),
]
with tempfile.TemporaryDirectory() as tmp:
    res, log = run_fixture(region, tmp, "real") if region else (None, "the wait functions were not found")
    if res is None:
        arm("the lifted wait functions compile and run on the host", False, log)
    else:
        arm("the lifted wait functions compile and run on the host", len(res) >= 11, log)
        for n, ok in res.items():
            arm("behaviour: " + n, ok, log)
    for what, old_s, new_s, target in CONTROLS:
        if region.count(old_s) != 1:
            arm(f"control ({what}): the line to alter is in the source once", False, old_s)
            continue
        cres, clog = run_fixture(region.replace(old_s, new_s), tmp, "ctl")
        arm(f"control ({what}) fails its arm", cres is not None and cres.get(target) is False, clog)

print(f"{fails} failed" if fails else "all passed")
sys.exit(1 if fails else 0)
