#!/usr/bin/env python3
"""test_gpu_wake_retry.py: a GPU wake call (gpuWakeFire) that fails to light the head queues itself again 500 ms
later, a bounded number of times, without waiting under the display gate.

After a system wake nothing but this call asks for head power, so one failed attempt left the display dark while
WindowServer drew. Pull request 597 of the original project slept 500 ms inside the call, with the display gate held,
and tried once more; the gate is the one recursive lock of every framebuffer, which the flips, the cursor, mode changes
and the video memory worker take too. Here a failed attempt asks for the call again through scheduleCall with a
500 ms deadline instead, up to kGpuWakeAttempts (2 or 3) attempts in one wake. A failed setPowerState(1) queues the
call 500 ms later with its own attempt counted as the wake's first (tools/test_display_power_retry.py), so the call
makes the others. The count starts again when the head is lit, when the call gives up or finds nothing to do, and at a
GPU standby ("NVRMGpuPower" 0); "NVRMGpuPower" 1 leaves it as it is.

Compiles gpuWakeFire, its two constants and the two branches of callPlatformFunction that handle "NVRMGpuPower" 0
and 1, from kexts/NVRMFB/fb/nvrm-fb.cpp as they are, with stand-ins for the kernel and for the rest of the
framebuffer. The thread call is modelled as THREAD_CALL_OPTIONS_ONCE makes it: one queued run at most, and the
deadline of the last request is the one that counts.

  python3 tools/test_gpu_wake_retry.py [--baseline [REF]]
      --baseline REF   the source of that revision (default HEAD); on 3.0.4 this test fails (no retry), and on the
                       pull request's own commit (upstream/pr597) too (it sleeps under the display gate)"""
import argparse
import pathlib
import re
import subprocess
import sys
import tempfile

root = pathlib.Path(__file__).resolve().parents[1]
REL = 'kexts/NVRMFB/fb/nvrm-fb.cpp'
parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument('--baseline', nargs='?', const='HEAD', metavar='REF')
args = parser.parse_args()
if args.baseline:
    source = subprocess.check_output(['git', 'show', f'{args.baseline}:{REL}'], cwd=root, text=True)
else:
    source = (root / REL).read_text()


def function(text, signature):
    start = text.index(signature)
    opening = text.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[start:end]


fire = function(source, 'void NVRMNVDAFramebuffer::gpuWakeFire(')
constants = '\n'.join(m.group(0) for m in re.finditer(r'static const \w+ kGpuWake\w+ = \d+;', source))
attempts = re.search(r'static const \w+ kGpuWakeAttempts = (\d+);', source)
resumed = re.search(r'\} else if \(st == 1\) \{\n((?:[^\n]*\n)*?[^\n]*scheduleCall\(fGpuWake, 0\)[^\n]*)\n', source)
standby = re.search(r'\n *if \(st == 0\) \{\n((?:[^\n]*\n)*?) *\} else if \(st == 1\) \{', source)
if not resumed or not standby:
    print('FAIL the branches that handle "NVRMGpuPower" 0 and 1 were not found')
    sys.exit(1)

fixture = r'''
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <vector>
typedef int32_t SInt32;
typedef uint64_t UInt64;
typedef void *thread_call_param_t;
typedef struct opaque_thread_call *thread_call_t;
enum { kMillisecondScale = 1000000 };
static std::recursive_mutex displayLock;
static unsigned gateDepth, sleeps, sleepsUnderGate, intervalMs, scale;
static const char *lastLog = "";
struct NVRMDisplayGate {
    NVRMDisplayGate() { displayLock.lock(); gateDepth++; }
    ~NVRMDisplayGate() { gateDepth--; displayLock.unlock(); }
    bool valid() const { return true; }
};
struct NVRMCallReference { template <class T> explicit NVRMCallReference(T *) {} };
static void IOSleep(unsigned) { sleeps++; if (gateDepth) sleepsUnderGate++; }
static SInt32 OSAddAtomic(SInt32 n, volatile SInt32 *v) { SInt32 old = *v; *v = old + n; return old; }
static SInt32 OSIncrementAtomic(volatile SInt32 *v) { return OSAddAtomic(1, v); }
static void clock_interval_to_deadline(uint32_t interval, uint32_t factor, uint64_t *deadline)
{
    intervalMs = interval; scale = factor; *deadline = 1000 + (uint64_t)interval * factor;
}
#define FBLOG(fmt, ...) do { lastLog = fmt; } while (0)
struct NVRMNVDAFramebuffer {
    bool fHeadSlept = true, fPowerOn = true, fModeSet = true, fSysSlept = false;
    void *fSurf = this;
    unsigned fHead = 0;
    volatile SInt32 fStopping = 0;
    volatile SInt32 fGpuWakeTries = 0;
    thread_call_t fGpuWake = (thread_call_t)this;
    unsigned failures = 0, attempts = 0;      // the head refuses the first `failures` attempts
    bool pending = false; uint64_t pendingDeadline = 0;
    std::vector<uint64_t> asked;              // every deadline scheduleCall was given
    bool setHeadPower(bool) { attempts++; if (attempts <= failures) return false; fHeadSlept = false; return true; }
    void scheduleCall(thread_call_t, uint64_t deadline) { pending = true; pendingDeadline = deadline; asked.push_back(deadline); }
    void gpuResumed()
    {
/*RESUMED*/
    }
    void gpuStandby()
    {
/*STANDBY*/
    }
    static void gpuWakeFire(thread_call_param_t p0, thread_call_param_t);
};
/*CONSTANTS*/
'''

checks = r'''
static int fails;
static void check(const char *name, bool ok) { printf("%s%s\n", ok ? "ok   " : "FAIL ", name); fails += !ok; }
static unsigned runQueued(NVRMNVDAFramebuffer &f)   // runs the call for as long as it is queued; returns the runs
{
    unsigned runs = 0;
    while (f.pending && runs < 20) { f.pending = false; NVRMNVDAFramebuffer::gpuWakeFire(&f, nullptr); runs++; }
    return runs;
}
int main()
{
    {   // the head never lights: a bounded run of attempts, each retry 500 ms after the failure
        NVRMNVDAFramebuffer f; f.failures = 100; f.gpuResumed();
        const unsigned runs = runQueued(f);
        check("a failed attempt queues the call again", f.asked.size() >= 2 && f.attempts >= 2);
        check("the attempts are bounded: 2 or 3 in all, then the call stops asking", f.attempts <= 3 && runs == f.attempts && !f.pending);
        check("the attempts are as many as kGpuWakeAttempts says", ATTEMPTS == 0 || f.attempts == ATTEMPTS);
        bool delayed = f.asked.size() == f.attempts && f.asked[0] == 0;
        for (size_t i = 1; i < f.asked.size(); ++i) delayed = delayed && f.asked[i] != 0;
        check("each retry is queued with a deadline, not at once", delayed && f.asked.size() >= 2);
        check("the deadline is 500 ms (clock_interval_to_deadline, kMillisecondScale)", intervalMs == 500 && scale == kMillisecondScale);
        check("nothing waits inside the call: no IOSleep with the display gate held", sleepsUnderGate == 0 && sleeps == 0);
        check("a head that never lit stays retryable for the next power request", f.fHeadSlept);
        check("the count starts again after the call gives up", f.fGpuWakeTries == 0);
        check("the last attempt says it failed", strstr(lastLog, "FAILED") != nullptr);
    }
    {   // the second attempt lights the head
        NVRMNVDAFramebuffer f; f.failures = 1; f.gpuResumed();
        const unsigned runs = runQueued(f);
        check("one failure, then the retry lights the head and nothing more is queued", runs == 2 && f.attempts == 2 && !f.fHeadSlept && !f.pending);
        check("the count starts again once the head is lit", f.fGpuWakeTries == 0);
    }
    {   // the first attempt lights the head: as before, one attempt and nothing queued
        NVRMNVDAFramebuffer f; f.gpuResumed();
        const unsigned runs = runQueued(f);
        check("a head lit at the first attempt queues nothing", runs == 1 && f.attempts == 1 && f.asked.size() == 1 && f.fGpuWakeTries == 0);
    }
    {   // setPowerState(1) failed: its attempt is the wake's first, and the call it queued 500 ms later makes the rest
        NVRMNVDAFramebuffer f; f.failures = 100;
        f.attempts = 1; f.fGpuWakeTries = 1; f.pending = true; f.pendingDeadline = 1500;   // as setPowerState(1) leaves it
        runQueued(f);
        bool delayed = !f.asked.empty();
        for (uint64_t d : f.asked) delayed = delayed && d != 0;
        check("after a failed setPowerState(1) the wake makes kGpuWakeAttempts attempts in all, that one included",
              f.attempts >= 2 && f.attempts <= 3 && (ATTEMPTS == 0 || f.attempts == ATTEMPTS) && !f.pending && f.fGpuWakeTries == 0);
        check("... and every retry it queues is 500 ms later, none straight after a failure", delayed);
    }
    {   // NVRM tells the framebuffer the GPU is back just after setPowerState(1) of the same wake failed
        NVRMNVDAFramebuffer f; f.failures = 100;
        f.attempts = 1; f.fGpuWakeTries = 1; f.pending = true; f.pendingDeadline = 1500;   // as setPowerState(1) leaves it
        f.gpuResumed();                                                                  // "NVRMGpuPower" 1
        check("\"NVRMGpuPower\" 1 runs the queued retry now and keeps the count of this wake",
              f.pending && f.pendingDeadline == 0 && f.fGpuWakeTries == 1);
        runQueued(f);
        check("... so the wake still makes kGpuWakeAttempts attempts in all, not one more",
              f.attempts >= 2 && f.attempts <= 3 && (ATTEMPTS == 0 || f.attempts == ATTEMPTS) && !f.pending);
    }
    {   // a GPU standby starts the count again for the wake that follows it
        NVRMNVDAFramebuffer f; f.failures = 100; f.fGpuWakeTries = 2;   // left by a run that never ended (no gate)
        f.gpuStandby();
        check("a GPU standby clears the count", f.fGpuWakeTries == 0 && f.fSysSlept && f.fHeadSlept);
        f.gpuResumed(); runQueued(f);
        check("... and the wake after it gets every attempt", f.attempts >= 2 && (ATTEMPTS == 0 || f.attempts == ATTEMPTS) && !f.pending);
    }
    {   // a retry that finds the display asleep, or the head lit by setPowerState(1), ends the run
        NVRMNVDAFramebuffer f; f.failures = 100; f.gpuResumed();
        f.pending = false; NVRMNVDAFramebuffer::gpuWakeFire(&f, nullptr);
        f.fPowerOn = false; const unsigned before = f.attempts; runQueued(f);
        check("a retry with the display asleep tries nothing, queues nothing, and the count starts again",
              f.attempts == before && !f.pending && f.fGpuWakeTries == 0);
        NVRMNVDAFramebuffer g; g.failures = 100; g.gpuResumed();
        g.pending = false; NVRMNVDAFramebuffer::gpuWakeFire(&g, nullptr);
        g.fHeadSlept = false; const unsigned gBefore = g.attempts; runQueued(g);
        check("a retry that finds the head already lit tries nothing and queues nothing", g.attempts == gBefore && !g.pending && g.fGpuWakeTries == 0);
    }
    {   // a framebuffer that is stopping
        NVRMNVDAFramebuffer f; f.failures = 100; f.fStopping = 1; f.gpuResumed();
        runQueued(f);
        check("a stopping framebuffer tries nothing and queues nothing", f.attempts == 0 && !f.pending);
    }
    printf("%d failed\n", fails);
    return fails ? 1 : 0;
}
'''

# 0: the source names no attempt count (3.0.4), and only the bound of 2 or 3 is checked
expected = f'#define ATTEMPTS {int(attempts.group(1)) if attempts else 0}u\n'
program = (expected + fixture.replace('/*RESUMED*/', resumed.group(1)).replace('/*STANDBY*/', standby.group(1))
           .replace('/*CONSTANTS*/', constants) + fire + '\n' + checks)
with tempfile.TemporaryDirectory() as directory:
    path = pathlib.Path(directory)
    (path / 'test.cpp').write_text(program)
    built = subprocess.run(['clang++', '-std=c++17', '-pthread', '-Wall', '-Wno-unused-function', str(path / 'test.cpp'),
                            '-o', str(path / 'test')], capture_output=True, text=True)
    if built.returncode:
        print(built.stderr[-3000:])
        print('FAIL gpuWakeFire does not compile with the stand-ins')
        sys.exit(1)
    sys.exit(subprocess.run([str(path / 'test')]).returncode)
