#!/usr/bin/env python3
"""Startup timing: headless deadline after the settle delay, nvrmsettle override, early exit when no connector has a display.
Compiles kexts/NVRM/nvrm_startup_policy.h on the host and checks the kernel sources use it."""
import os, re, shutil, subprocess, sys, tempfile
from pathlib import Path

# Comments blanked, string and character literals kept, offsets unchanged (the LEX of test_lifecycle_order.py, with the
# strings kept so a structural check can read both code and the string literals it names).
LEX = re.compile(r"""//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])'""", re.S)


def code_keep_strings(text):
    return LEX.sub(lambda m: m.group(0) if m.group(0)[0] in "\"'" else re.sub(r"[^\n]", " ", m.group(0)), text)


root = Path(__file__).resolve().parents[1]
nvrm = (root / "kexts/NVRM/NVRM.cpp").read_text(encoding="utf-8")
accel = (root / "kexts/NVRM/accel/nvrm-accel.cpp").read_text(encoding="utf-8")
failures = []

def arm(name, ok):
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        failures.append(name)

header = root / "kexts/NVRM/nvrm_startup_policy.h"
arm("the policy header exists", header.exists())

if header.exists():
    harness = r'''
#include <cstdio>
#include <string>
#include "nvrm_startup_policy.h"
#define CHECK(name, cond) do { std::printf("%s %s\n", (cond) ? "ok  " : "FAIL", name); if (!(cond)) bad++; } while (0)
int main() {
    int bad = 0;
    CHECK("a 100 s settle moves the 60 s headless deadline to 160 s", nvrmHeadlessDeadlineSec(60, 100000) == 160);
    CHECK("a card that settles in 0.5 s waits 61 s (partial seconds round up)", nvrmHeadlessDeadlineSec(60, 500) == 61);
    CHECK("no auto-go (settle 0) keeps the plain 60 s deadline", nvrmHeadlessDeadlineSec(60, 0) == 60);
    CHECK("a typo of nvrmsettle=99999999 cannot withhold Metal beyond 5 minutes of settle", nvrmHeadlessDeadlineSec(60, 99999999u) == 360);
    {int c[] = {0, 0}; CHECK("two empty connector lists mean no display anywhere", nvrmNoDisplayOnAnyConnector(c, 2));}
    {int c[] = {0, 1}; CHECK("one connected display means a head can arm", !nvrmNoDisplayOnAnyConnector(c, 2));}
    {int c[] = {0, -1}; CHECK("a nub that has not published yet keeps the wait going", !nvrmNoDisplayOnAnyConnector(c, 2));}
    CHECK("no nub at all is not evidence of an empty connector list", !nvrmNoDisplayOnAnyConnector(nullptr, 0));
    CHECK("unplaced BAR1 waits 100 s by default", nvrmAutoGoSettleMs(false, false, false, 0) == 100000);
    CHECK("placed BAR1 on Turing waits 10 s, on others 0.5 s", nvrmAutoGoSettleMs(true, true, false, 0) == 10000 && nvrmAutoGoSettleMs(true, false, false, 0) == 500);
    CHECK("nvrmsettle=10000 overrides the 100 s default on an unplaced BAR1", nvrmAutoGoSettleMs(false, false, true, 10000) == 10000);
    CHECK("nvrmsettle=0 is honoured", nvrmAutoGoSettleMs(false, true, true, 0) == 0);
    CHECK("a 16 GB BAR1 on a card offering 8 GB is resized down to 8 GB", nvrmBar1ShrinkTarget(16ULL << 30, 0x3fe0, false) == (8ULL << 30));
    CHECK("a 32 GB BAR1 is resized down to 8 GB as well", nvrmBar1ShrinkTarget(32ULL << 30, 0x7fe0, false) == (8ULL << 30));
    CHECK("an 8 GB BAR1 is left alone", nvrmBar1ShrinkTarget(8ULL << 30, 0x3fe0, false) == 0);
    CHECK("a 256 MB BAR1 is not touched by the shrink rule", nvrmBar1ShrinkTarget(256ULL << 20, 0x3fe0, false) == 0);
    CHECK("a card that does not offer 8 GB keeps its size", nvrmBar1ShrinkTarget(16ULL << 30, 0x2000 >> 1, false) == 0);
    CHECK("-nvrmkeepbar1 keeps the firmware size", nvrmBar1ShrinkTarget(16ULL << 30, 0x3fe0, true) == 0);
    {char b[64]; int n = nvrmFallbackModelName(b, sizeof b, "Turing", "TU106", 0x1F08);
     CHECK("an unnamed RTX 20 id reads as its generation, chip and PCI id", n > 0 && std::string(b) == "NVIDIA Turing GPU (TU106, 10DE:1F08)");}
    CHECK("a 64 MB laptop BAR1 that must grow to 8 GB uses the fallback range when devices already sit above 4 GB", nvrmFallbackWindowAllowed(64ULL << 20, 8ULL << 30, true));
    CHECK("a BAR1 left below 4 GB on a host that routes nothing higher stays put", !nvrmFallbackWindowAllowed(512ULL << 20, 8ULL << 30, false));
    CHECK("a same-size move of a BAR1 below 4 GB with no window stays refused", !nvrmFallbackWindowAllowed(512ULL << 20, 0, true));
    CHECK("a resize that stays under 4 GB with no window stays refused", !nvrmFallbackWindowAllowed(64ULL << 20, 256ULL << 20, true));
    CHECK("a BAR1 the firmware put above 4 GB keeps the old behaviour", nvrmFallbackWindowAllowed(8ULL << 30, 0, false));
    CHECK("order A: a head published before arming keeps arm-then-release", !nvrmReleaseBeforeArming(1, false) && !nvrmReleaseBeforeArming(1, true));
    CHECK("order B: no head and no display anywhere releases first", nvrmReleaseBeforeArming(0, true));
    CHECK("no head yet but connectors not enumerated keeps the hold", !nvrmReleaseBeforeArming(0, false));
    CHECK("a crash report fails the RPC at once, slow or not", nvrmGspTimeoutFatal(true, false, true, 0, 3) && nvrmGspTimeoutFatal(true, true, true, 0, 3));
    CHECK("a dead heartbeat with classification on is fatal at the first timeout", nvrmGspTimeoutFatal(false, true, false, 1, 3));
    CHECK("a dead heartbeat with classification off waits for the third timeout (the 6 s x 3 stall)", !nvrmGspTimeoutFatal(false, false, false, 1, 3) && nvrmGspTimeoutFatal(false, false, false, 3, 3));
    CHECK("a slow but healthy GSP keeps today's count with classification on", !nvrmGspTimeoutFatal(false, true, true, 1, 3) && !nvrmGspTimeoutFatal(false, true, true, 2, 3));
    CHECK("a slow but healthy GSP that keeps timing out is still failed at the threshold", nvrmGspTimeoutFatal(false, true, true, 3, 3));
    return bad;
}
'''
    cxx = os.environ.get("CXX") or shutil.which("clang++") or shutil.which("g++")
    with tempfile.TemporaryDirectory(prefix="startup-policy-") as t:
        t = Path(t)
        (t / "t.cpp").write_text(harness)
        subprocess.run([cxx, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-I", str(header.parent), str(t / "t.cpp"), "-o", str(t / "t")], check=True)
        r = subprocess.run([str(t / "t")], capture_output=True, text=True)
        print(r.stdout, end="")
        if r.returncode:
            failures.append("policy harness")

# The waits of auto-go (nvrm_startup_policy.h): the stepper against a transcription of the 2.9 loop, the 50 ms stepper
# against the conditions themselves, and the small helpers. Each group has a control that a wrong rule fails.
waits = r"""
#include <cstdio>
#include <vector>
#include "nvrm_startup_policy.h"
#define CHECK(name, cond) do { std::printf("%s %s\n", (cond) ? "ok  " : "FAIL", name); if (!(cond)) bad++; } while (0)
struct Sample { int mask; unsigned connected; bool noDisp; };
static unsigned pop(unsigned m) { unsigned c = 0; for (; m; m &= m - 1) c++; return c; }
// The 2.9 loop of NVRM::autoGo, as it was written (samples[i] is what the i-th poll, at i seconds, reads). stillAt is
// 3 in 2.9; the control passes 2. Returns the poll index it left at (20 = after the twentieth sleep) and why.
static int loop29(const Sample *smp, int want, bool hy, int stillAt, int *why)
{
    int mask = 0, last = -1, still = 0, i;
    *why = 7;
    for (i = 0; i < 20; i++) {
        mask = smp[i].mask;
        if (hy) { if (mask & 0x10) { *why = 5; break; } continue; }
        if ((int)pop((unsigned)mask) >= want) { *why = 1; break; }
        still = (mask && mask == last) ? still + 1 : 0; last = mask;
        if (still >= stillAt) { *why = 2; break; }
        if (smp[i].connected > 0 && (int)pop((unsigned)mask) >= (int)smp[i].connected) { *why = 3; break; }
        if (!mask && smp[i].noDisp) { *why = 4; break; }
    }
    return i;
}
// The stepper as auto-go drives it: poll, sleep, count. tick(k) is what the k-th poll reads; returns the poll it left at
// (or the poll count at the bound) and why. pollEvery is the real time between polls, stepMs what the stepper is told.
template <class F> static unsigned runStepper(F tick, unsigned stepMs, unsigned pollEvery, int want, bool hy, int *why, unsigned *ms)
{
    NvrmHeadsWait w; nvrmHeadsWaitInit(&w, stepMs, want, hy);
    for (unsigned k = 0;; k++) {
        const Sample s = tick(k);
        NvrmHeadsWhy y = nvrmHeadsWaitPoll(&w, (unsigned)s.mask, s.connected, s.noDisp, false);
        if (y != kNvrmHeadsKeepWaiting) { *why = y; *ms = k * pollEvery; return k; }
        if (!nvrmHeadsWaitSlept(&w)) { *why = kNvrmHeadsTimeout; *ms = (k + 1) * pollEvery; return k + 1; }
        if (k > 100000) { *why = -1; *ms = ~0u; return k; }
    }
}
static unsigned rng = 12345u;
static unsigned rnd(unsigned n) { rng = rng * 1103515245u + 12345u; return (rng >> 8) % n; }
// A world on 50 ms ticks: heads appear one by one and stay (the accelerator publishes, it never takes a head back),
// the connector count appears at some moment, the "no display anywhere" walk turns true at some moment.
struct World { std::vector<Sample> t; int want; bool hy; };
static World makeWorld(bool monotone)
{
    World w; w.want = 1 + (int)rnd(3); w.hy = rnd(8) == 0;
    const unsigned n = 420; w.t.resize(n);
    int mask = 0; unsigned conn = 0; bool nd = false;
    const unsigned connAt = rnd(500), ndAt = rnd(600), c = rnd(4);
    for (unsigned k = 0; k < n; k++) {
        if (monotone) { if (rnd(40) == 0) mask |= 1 << rnd(5); }
        else if (rnd(25) == 0) mask = (int)rnd(32);
        if (k == connAt) conn = c;
        if (k == ndAt) nd = true;
        w.t[k] = Sample{ mask, conn, nd && !mask };
    }
    return w;
}
// Does the reason the stepper gave hold at tick k of the world, read the way 2.9 reads it?
static bool holds(const World &w, unsigned k, int why)
{
    const Sample s = w.t[k];
    const unsigned heads = pop((unsigned)s.mask);
    switch (why) {
    case kNvrmHeadsEnough: return (int)heads >= w.want;
    case kNvrmHeadsStable: {                       // non-zero and the same for the last 3 s
        if (!s.mask || k < 60) return false;
        for (unsigned j = k - 60; j <= k; j++) if (w.t[j].mask != s.mask) return false;
        return true; }
    case kNvrmHeadsAllConnected: return s.connected > 0 && heads >= s.connected;
    case kNvrmHeadsNoDisplay: return !s.mask && s.noDisp;
    case kNvrmHeadsHybridPanel: return (s.mask & 0x10) != 0;
    case kNvrmHeadsTimeout: return k == 400;
    }
    return false;
}
int main()
{
    int bad = 0;
    // 1. With 1 s steps the stepper is the 2.9 loop: same poll, same reason, on any sequence, monotone or not.
    {
        int same = 0, total = 0, ctlDiff = 0;
        for (int n = 0; n < 20000; n++) {
            Sample smp[20]; const int want = 1 + (int)rnd(3); const bool hy = rnd(8) == 0;
            for (int i = 0; i < 20; i++) smp[i] = Sample{ (int)rnd(4) == 0 ? (int)rnd(32) : (i ? smp[i - 1].mask : 0), rnd(3) ? 0u : rnd(4), rnd(5) == 0 };
            int why29 = 0, whySt = 0, whyCtl = 0; unsigned ms = 0;
            const int i29 = loop29(smp, want, hy, 3, &why29);
            const unsigned k = runStepper([&](unsigned j) { return smp[j < 20 ? j : 19]; }, 1000, 1000, want, hy, &whySt, &ms);
            total++; if ((int)k == i29 && whySt == why29) same++;
            const int iCtl = loop29(smp, want, hy, 2, &whyCtl);
            if (iCtl != i29 || whyCtl != why29) ctlDiff++;
            if ((int)k != i29 || whySt != why29) { if (same + 1 == total) std::printf("     first difference: poll %d/%u why %d/%d\n", i29, k, why29, whySt); }
        }
        std::printf("     %d of %d sequences identical\n", same, total);
        CHECK("with 1 s steps the stepper leaves at the same poll for the same reason as the 2.9 loop", same == total);
        CHECK("control: a loop that calls the mask stable after two equal polls is told apart", ctlDiff > 0);
        int why = 0; unsigned ms = 0;
        const unsigned k = runStepper([](unsigned) { return Sample{ 0, 0, false }; }, 1000, 1000, 1, false, &why, &ms);
        CHECK("nothing ever published: 20 polls, the bound after the twentieth sleep (20 s), as 2.9", k == 20 && why == kNvrmHeadsTimeout && ms == 20000);
    }
    // 2. With 50 ms steps, in worlds where heads only appear: it leaves no later than 2.9 would, and only when the
    //    reason it gives is true at that moment.
    {
        int late = 0, untrue = 0, ctlUntrue = 0, earlier = 0;
        for (int n = 0; n < 20000; n++) {
            World w = makeWorld(true);
            int why29 = 0; Sample smp[20];
            for (int i = 0; i < 20; i++) smp[i] = w.t[i * 20];
            const int i29 = loop29(smp, w.want, w.hy, 3, &why29);
            int why = 0; unsigned ms = 0;
            const unsigned k = runStepper([&](unsigned j) { return w.t[j < 419 ? j : 419]; }, 50, 50, w.want, w.hy, &why, &ms);
            if (k > (unsigned)i29 * 20) late++;
            if (k < (unsigned)i29 * 20) earlier++;
            if (!holds(w, k, why)) untrue++;
            // control: a stepper that counts polls, not time, for "unchanged" (it is told 1000 ms per 50 ms poll)
            int whyC = 0; unsigned msC = 0;
            const unsigned kc = runStepper([&](unsigned j) { return w.t[j < 419 ? j : 419]; }, 1000, 50, w.want, w.hy, &whyC, &msC);
            if (whyC != kNvrmHeadsTimeout && !holds(w, kc, whyC)) ctlUntrue++;
        }
        std::printf("     %d worlds left earlier than 2.9, %d later, %d with a reason that did not hold\n", earlier, late, untrue);
        CHECK("with 50 ms steps the wait never ends after 2.9's would have", late == 0);
        CHECK("with 50 ms steps the wait ends only when the reason it gives is true at that moment", untrue == 0);
        CHECK("with 50 ms steps most waits end before 2.9's (the point of the change)", earlier > 10000);
        CHECK("control: a stepper that counts 3 polls as 3 s is caught calling a changing mask stable", ctlUntrue > 0);
    }
    // 3. The accelerator declined: the wait ends at once whatever the mask says.
    {
        NvrmHeadsWait w; nvrmHeadsWaitInit(&w, 50, 1, false);
        CHECK("an accelerator that declined ends the heads wait at the first poll", nvrmHeadsWaitPoll(&w, 0, 0, false, true) == kNvrmHeadsNoAccel);
        CHECK("... also in the hybrid wait", (nvrmHeadsWaitInit(&w, 50, 1, true), nvrmHeadsWaitPoll(&w, 0, 0, false, true) == kNvrmHeadsNoAccel));
        CHECK("its wait for the display path is at most 20 s", nvrmAccelRefusedWaitMs(0) == 20000);
        CHECK("... and never ends after 2.9's heads wait plus its ten arm tries (30 s)", nvrmAccelRefusedWaitMs(15000) == 15000 && nvrmAccelRefusedWaitMs(19950) == 10050 && nvrmAccelRefusedWaitMs(30000) == 0 && nvrmAccelRefusedWaitMs(40000) == 0);
        unsigned worst = 0;
        for (unsigned e = 0; e <= 20000; e += 50) if (e + nvrmAccelRefusedWaitMs(e) > worst) worst = e + nvrmAccelRefusedWaitMs(e);
        CHECK("heads wait plus that wait: at most 30 s for every moment the refusal can be seen", worst == 30000);
    }
    // 4. Raster: a head is left early only when its counter has not moved at all for 50 ms.
    CHECK("raster: a counter that never moved is left after 50 ms", nvrmRasterHeadIdle(50000000ull, 0) && nvrmRasterHeadIdle(51000000ull, 0));
    CHECK("raster: not before 50 ms", !nvrmRasterHeadIdle(49999999ull, 0));
    CHECK("raster: a counter that moved once keeps the full window", !nvrmRasterHeadIdle(60000000ull, 1) && !nvrmRasterHeadIdle(250000000ull, 1));
    CHECK("raster: 20 Hz (the slowest accepted) moves the counter many times in 50 ms, so it is never left early",
          !nvrmRasterHeadIdle(50000000ull, 100));
    // 5. Arm retry: early only on the edge that the write needs.
    CHECK("arm: head 0 not published when the write failed: retry as soon as it is", nvrmArmRetryMayPoll(true, 0) && nvrmArmRetryMayPoll(true, 0x2));
    CHECK("arm: head 0 already published: the full second, as 2.9", !nvrmArmRetryMayPoll(true, 1) && !nvrmArmRetryMayPoll(true, 0x3));
    CHECK("arm: mask unreadable: the full second, as 2.9", !nvrmArmRetryMayPoll(false, 0));
    // 6. The bounds 2.9 had, and the start gate.
    CHECK("bounds: 20 s of heads, 3 s unchanged, ten 1 s arm tries", kNvrmHeadsWaitMs == 20000 && kNvrmHeadsStableMs == 3000 && kNvrmArmTries == 10 && kNvrmArmStepMs == 1000);
    CHECK("start gate: at most 5 s, in 10 ms steps", kNvrmStartGateMaxMs == 5000 && kNvrmStartGateStepMs == 10);
    return bad;
}
"""
with tempfile.TemporaryDirectory(prefix="startup-waits-") as t:
    t = Path(t)
    (t / "w.cpp").write_text(waits)
    cxx = os.environ.get("CXX") or shutil.which("clang++") or shutil.which("g++")
    b = subprocess.run([cxx, "-std=c++17", "-O1", "-Wall", "-Wextra", "-Werror", "-I", str(header.parent), str(t / "w.cpp"), "-o", str(t / "w")],
                       capture_output=True, text=True)
    if b.returncode:
        print(b.stdout + b.stderr)
        failures.append("waits harness does not build")
    else:
        r = subprocess.run([str(t / "w")], capture_output=True, text=True)
        print(r.stdout, end="")
        if r.returncode:
            failures.append("waits harness")


def body(src, name):
    """The text of a function definition `name(` in src, from its signature to its closing brace at column 0."""
    at = src.find("\n" + name + "(")
    if at < 0:
        return ""
    end = src.find("\n}\n", at)
    return src[at:end + 3] if end >= 0 else ""


def wiring(src):
    """The start-up wiring in NVRM.cpp. Returns the names of the rules src breaks."""
    broken = []
    start = body(src, "bool NVRM::startGPU")
    auto = body(src, "void NVRM::autoGo")
    raster = body(src, "void NVRM::measureBootRaster")
    gate = start.find("nvrm_guard_count_start();")
    fb = start.find('nvrmPeerLayout(provider, "nvrmfb-layout"')
    refuse = start.find("if (gNvLayoutFb == kNvLayoutRefused)")
    if not (0 <= fb < refuse < gate and "return false;" in start[refuse:gate]):
        broken.append("startGPU reads nvrmfb-layout and declines on a refusal before nvrm_guard_count_start()")
    tail = start.rstrip()[:-1].rstrip().splitlines()
    if not (len(tail) >= 2 and tail[-1].strip() == "return true;" and "&fStartReturned" in tail[-2]):
        broken.append("the last statement of startGPU before its final return true writes fStartReturned")
    # These two read the structure, not just a name: start() runs on past the thread it started, so auto-go must spin on
    # fStartReturned until it returns before it touches the card; and a framebuffer-layout refusal seen late must step
    # aside (release the hold and return) before go(2). A later edit that drops the wait or disables the late refusal
    # leaves a log line or the string behind, which a find() of the name would still accept.
    autoc = code_keep_strings(auto)
    go = autoc.find("s->go(2)")
    m = re.search(r"while\s*\(\s*!\s*OSAddAtomic\s*\(\s*0\s*,\s*&s->fStartReturned\s*\)\s*&&\s*waited\s*<\s*"
                  r"kNvrmStartGateMaxMs\s*\)\s*\{\s*IOSleep\s*\(", autoc)
    if not (m and 0 <= m.start() < go):
        broken.append("autoGo waits for fStartReturned before go(2)")
    ref = autoc.find("if (gNvLayoutFb == kNvLayoutRefused) {")
    refuse_ok = False
    if 0 <= ref < go:
        ob = autoc.find("{", ref)
        depth, end = 0, -1
        for i in range(ob, len(autoc)):
            if autoc[i] == "{":
                depth += 1
            elif autoc[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end >= 0:
            block = autoc[ob:end]
            refuse_ok = "releaseBootHold(" in block and "return;" in block and end < go
    if not refuse_ok:
        broken.append("autoGo reads nvrmfb-layout again before go(2)")
    if src.count('"-nvrmoldwaits"') != 1 or '"-nvrmoldwaits"' not in start:
        broken.append("-nvrmoldwaits is read once, in startGPU")
    if not ("nvrmHeadsWaitPoll(" in auto and "nvrmHeadsWaitSlept(" in auto and "old ? 1000u" in auto):
        broken.append("the heads wait is the stepper, with 1 s steps under -nvrmoldwaits")
    if "for (i = 0; i < 20; i++)" in auto:
        broken.append("the 2.9 one-second heads loop is gone")
    if not ("nvrmArmRetryMayPoll(" in auto and "!old &&" in auto[auto.find("nvrmArmRetryMayPoll(") - 200:auto.find("nvrmArmRetryMayPoll(")]):
        broken.append("the arm retry polls only outside -nvrmoldwaits")
    if not ('"nvaccel-layout"' in auto and "nvrmAccelRefusedWaitMs(" in auto and '"debug.nvrmfb_guard_cleared"' in auto):
        broken.append("an accelerator refusal waits, bounded, for the framebuffer's display path")
    if "!fOldWaits && nvrmRasterHeadIdle(" not in raster:
        broken.append("the raster early exit is skipped under -nvrmoldwaits")
    return broken


broken = wiring(nvrm)
for rule in ["startGPU reads nvrmfb-layout and declines on a refusal before nvrm_guard_count_start()",
             "the last statement of startGPU before its final return true writes fStartReturned",
             "autoGo waits for fStartReturned before go(2)", "autoGo reads nvrmfb-layout again before go(2)",
             "-nvrmoldwaits is read once, in startGPU", "the heads wait is the stepper, with 1 s steps under -nvrmoldwaits",
             "the 2.9 one-second heads loop is gone", "the arm retry polls only outside -nvrmoldwaits",
             "an accelerator refusal waits, bounded, for the framebuffer's display path",
             "the raster early exit is skipped under -nvrmoldwaits"]:
    arm(rule, rule not in broken)
# controls: each change breaks the rule it should
def mutated(old, new):
    return nvrm.replace(old, new, 1) if old in nvrm else None
for name, m, rule in [
    ("control: the guard counted before the framebuffer's verdict is read",
     mutated("    nvrm_guard_count_start();\n", "").replace(
         "    // NVRMFB's verdict on the Apple framebuffer classes", "    nvrm_guard_count_start();\n    // NVRMFB's verdict on the Apple framebuffer classes", 1)
     if mutated("    nvrm_guard_count_start();\n", "") else None,
     "startGPU reads nvrmfb-layout and declines on a refusal before nvrm_guard_count_start()"),
    ("control: the mark written before registerService", (nvrm.replace("    OSCompareAndSwap(0, 1, &fStartReturned);\n    return true;", "    return true;", 1)
          .replace("    registerService();\n", "    OSCompareAndSwap(0, 1, &fStartReturned);\n    registerService();\n", 1)
          if "    OSCompareAndSwap(0, 1, &fStartReturned);\n    return true;" in nvrm else None),
     "the last statement of startGPU before its final return true writes fStartReturned"),
    ("control: -nvrmoldwaits read a second time in autoGo", mutated("    const bool old = s->fOldWaits;", '    const bool old = PE_parse_boot_argn("-nvrmoldwaits", nb, sizeof nb);'),
     "-nvrmoldwaits is read once, in startGPU"),
    ("control: the raster early exit taken under -nvrmoldwaits too", mutated("!fOldWaits && nvrmRasterHeadIdle(", "nvrmRasterHeadIdle("),
     "the raster early exit is skipped under -nvrmoldwaits"),
    # The wait for start() to return is deleted; the LOG line after it still names fStartReturned before go(2).
    ("control: the wait for start() to return removed from autoGo",
     mutated("        while (!OSAddAtomic(0, &s->fStartReturned) && waited < kNvrmStartGateMaxMs) "
             "{ IOSleep(kNvrmStartGateStepMs); waited += kNvrmStartGateStepMs; }\n", ""),
     "autoGo waits for fStartReturned before go(2)"),
    # The late framebuffer-layout refusal is disabled; the string "nvrmfb-layout" stays in the nvrmPeerLayout call.
    ("control: the late framebuffer-layout refusal disabled",
     mutated('if (gNvLayoutFb == kNvLayoutRefused) {\n            LOG("auto-go: NOT STARTING THE GPU',
             'if (0 && gNvLayoutFb == kNvLayoutRefused) {\n            LOG("auto-go: NOT STARTING THE GPU'),
     "autoGo reads nvrmfb-layout again before go(2)"),
]:
    arm(name, m is not None and rule in wiring(m))

arm("NVAccel places the headless deadline after the published settle delay", "nvrmHeadlessDeadlineSec(" in accel and "me->fHeadlessWaited < me->fHeadlessDeadlineSec" in accel)
arm("NVAccel no longer compares against the fixed 60 s constant", "me->fHeadlessWaited < kHeadlessDeadlineSec" not in accel)
arm("NVRM publishes its settle delay on the PCI device for NVAccel", 'fPCI->setProperty("nvrm-autogo-settle-ms"' in nvrm)
arm("NVRM takes its settle delay from the policy (nvrmsettle override included)", "nvrmAutoGoSettleMs(" in nvrm)
arm("auto-go stops waiting for heads when no connector carries a display", "nvrmNoDisplayAnywhere()" in nvrm and "not waiting for heads" in nvrm)
arm("an early release still runs the arming that serves a later hotplug", "if (earlyRelease) s->releaseBootHold(" in nvrm and nvrm.index("if (earlyRelease) s->releaseBootHold(") < nvrm.index('nvrmSysW("debug.nvaccelfb", 1)') and "if (!earlyRelease) s->releaseBootHold(" in nvrm)
arm("bus mastering from firmware is switched off until pass 2 enables it", "off until the RM starts the card" in nvrm and nvrm.index("off until the RM starts the card") < nvrm.index("fPCI->setBusMasterEnable(true)"))
arm("GSP timeout classification is on by default and a boot-arg override still wins", 'rm_write_registry_dword(NULL, NULL, "RmGspTimeoutClassification", 1)' in nvrm and nvrm.index('"RmGspTimeoutClassification"') < nvrm.index('rm_parse_option_string(NULL, regdw)'))
arm("a card the RM cannot name gets a model from the GPU profile", "nvrmFallbackModelName(" in nvrm)
arm("a BAR1 that must grow past 4 GB is no longer refused outright when nothing but the window is missing", "nvrmFallbackWindowAllowed(fwBar1, newSize" in nvrm)
arm("placeLargeBar1 resizes a firmware BAR1 above 8 GB through the proven path", "nvrmBar1ShrinkTarget(bar1Size, rbSupported, nvrmKeepBar1Size())" in nvrm)

if failures:
    print("FAILED:", ", ".join(failures))
    sys.exit(1)
print("PASS")
