#!/usr/bin/env python3
"""The utilization sampler after a slow read: it pauses, three slow reads in a row stop it for the boot, a fast read
starts the count again. Compiles kexts/NVRM/nvrm_utilization_pace.h on the host, with a control on two altered copies
of it, and checks that NVRM's utilization read uses it."""
import os, shutil, subprocess, sys, tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
nvrm = (root / "kexts/NVRM/NVRM.cpp").read_text(encoding="utf-8")
failures = []

def arm(name, ok):
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        failures.append(name)

harness = r'''
#include <cstdio>
#include "nvrm_utilization_pace.h"
#define CHECK(name, cond) do { const bool c_ = (cond); std::printf("%s %s\n", c_ ? "ok  " : "FAIL", name); if (!c_) bad++; } while (0)
static const unsigned long long kMs = 1000ull * 1000, kS = 1000 * kMs;
// NVRM's read as NVAccel's timer drives it for ten minutes: the timer is rearmed 1 s after each answer; a read is made
// when the pause is over and nothing stopped the sampler, and the pause starts when the slow read ends.
// took(k) = how long the k-th read takes. Returns the reads made; *locked = the time they held the RM's locks.
template <typename F> static unsigned run(F took, bool *stop, unsigned long long *locked, unsigned long long *lastRead)
{
    unsigned slow = 0, reads = 0; unsigned long long notBefore = 0; *stop = false; *locked = 0; *lastRead = 0;
    for (unsigned long long now = 0; now < 600 * kS; now += kS) {
        if (*stop || !nvrmUtilPauseOver(now, notBefore)) continue;
        const unsigned long long t = took(reads++);
        *locked += t; *lastRead = now; now += t;
        if (nvrmUtilSlowRead(&slow, stop, t)) notBefore = now + kUtilBackoffNs;
    }
    return reads;
}
int main() {
    int bad = 0;
    unsigned n = 0; bool stop = false;
    CHECK("a read of exactly 0.5 s is not slow", !nvrmUtilSlowRead(&n, &stop, 500 * kMs) && n == 0 && !stop);
    CHECK("a read of 0.6 s is slow and counted, and does not stop the sampler", nvrmUtilSlowRead(&n, &stop, 600 * kMs) && n == 1 && !stop);
    CHECK("a second slow read in a row does not stop it", nvrmUtilSlowRead(&n, &stop, 6 * kS) && n == 2 && !stop);
    CHECK("the third slow read in a row stops it for this boot", nvrmUtilSlowRead(&n, &stop, 6 * kS) && n == 3 && stop);
    n = 0; stop = false;
    nvrmUtilSlowRead(&n, &stop, 2 * kS); nvrmUtilSlowRead(&n, &stop, 2 * kS);
    CHECK("a fast read (answered or failed: only its time counts) starts the count again", !nvrmUtilSlowRead(&n, &stop, 3 * kMs) && n == 0);
    nvrmUtilSlowRead(&n, &stop, 2 * kS); nvrmUtilSlowRead(&n, &stop, 2 * kS);
    CHECK("two slow reads, a fast one, two slow ones: still sampling", n == 2 && !stop);
    CHECK("a read inside the pause is not made", !nvrmUtilPauseOver(99, 100));
    CHECK("a read is made once the pause is over, and before any pause", nvrmUtilPauseOver(100, 100) && nvrmUtilPauseOver(0, 0));
    unsigned long long locked = 0, last = 0;
    unsigned reads = run([](unsigned) { return 6 * kS; }, &stop, &locked, &last);
    std::printf("     a firmware that answers every read after 6 s: %u reads, the locks held %llu s of 600\n", reads, locked / kS);
    CHECK("a firmware that answers every read after 6 s is asked three times in a minute and then never again", reads == 3 && stop && last < 60 * kS);
    reads = run([](unsigned k) { return k % 40 == 5 ? 2 * kS : 3 * kMs; }, &stop, &locked, &last);
    std::printf("     one 2 s wait for the locks in every 40 reads: %u reads in 600 s\n", reads);
    CHECK("a 2 s wait for the locks now and then costs 20 s of statistics each time, not the boot", !stop && last >= 590 * kS && reads > 300);
    return bad;
}
'''

header = root / "kexts/NVRM/nvrm_utilization_pace.h"
arm("the pace header exists", header.exists())
cxx = os.environ.get("CXX") or shutil.which("clang++") or shutil.which("g++")
with tempfile.TemporaryDirectory(prefix="utilization-pace-") as t:
    t = Path(t)
    (t / "t.cpp").write_text(harness)

    def build_run(name, text):
        """-> the harness's output against a header with this text, or None when it does not build"""
        d = t / name
        d.mkdir()
        (d / "nvrm_utilization_pace.h").write_text(text)
        b = subprocess.run([cxx, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-I", str(d), str(t / "t.cpp"), "-o", str(d / "t")],
                           capture_output=True, text=True)
        if b.returncode:
            print(b.stdout + b.stderr, end="")
            return None
        return subprocess.run([str(d / "t")], capture_output=True, text=True).stdout

    if header.exists():
        real = header.read_text(encoding="utf-8")
        out = build_run("real", real)
        print(out or "", end="")
        arm("the rule holds every case above", out is not None and "FAIL" not in out)
        # Controls: the same cases tell apart a count that is never set back, and the first form of this guard, in which
        # one slow read stopped the sampler for the boot.
        reset = "{ *slowInRow = 0; return false; }"
        out = build_run("no-reset", real.replace(reset, "{ return false; }", 1)) if reset in real else None
        arm("control: a count that a fast read does not set back is caught",
            out is not None and "FAIL a fast read (answered or failed: only its time counts) starts the count again" in out)
        stop3 = "static const unsigned kUtilSlowStop = 3;"
        out = build_run("first-slow-stops", real.replace(stop3, "static const unsigned kUtilSlowStop = 1;", 1)) if stop3 in real else None
        arm("control: a rule that stops at the first slow read is caught",
            out is not None and "FAIL a 2 s wait for the locks now and then costs 20 s of statistics each time, not the boot" in out)

# The wiring: NVRM's utilization read takes its time around the query, makes no read inside a pause, hands every read it
# made (not only the answered ones) to the rule, and starts the pause when the read ended, all under fUtilizationLock.
s = nvrm.find("if (fn && fn->isEqualTo(NVRM_UTILIZATION_FN)) {")
e = nvrm.find("nv_xnu_pm_exit(counted);", s)
blk = nvrm[s:e] if 0 <= s < e else ""
arm("NVRM.cpp includes the pace header", '#include "nvrm_utilization_pace.h"' in nvrm)
arm("the members are declared with the stop flag", "unsigned     fUtilizationSlow = 0;" in nvrm and "uint64_t     fUtilizationNotBefore = 0;" in nvrm)
steps = ["IOLockLock(fUtilizationLock);", "const uint64_t utilT0 = mach_absolute_time();",
         "&& nvrmUtilPauseOver(utilT0, fUtilizationNotBefore);",
         "const bool ok = ready && nvrm_query_utilization(fNv.gpu_id, &fUtilizationState, sample);",
         "const uint64_t utilT1 = mach_absolute_time();", "absolutetime_to_nanoseconds(utilT1 - utilT0, &utilNs);",
         "if (ready && nvrmUtilSlowRead(&fUtilizationSlow, &fUtilizationStopping, utilNs)) {",
         "fUtilizationNotBefore = utilT1 + backoff;", "IOLockUnlock(fUtilizationLock);"]
at = [blk.find(x) for x in steps]
arm("the read is timed, paced and counted in this order under fUtilizationLock", all(i >= 0 for i in at) and at == sorted(at))
arm("a slow read is logged with its time", '"utilization: a read took %llu ms (limit %llu), %u in a row - ' in blk)

if failures:
    print("FAILED:", ", ".join(failures)); sys.exit(1)
print("PASS")
