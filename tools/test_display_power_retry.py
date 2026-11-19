#!/usr/bin/env python3
"""Exercise the production display power transitions without a display change.

A power-on whose head does not light queues the GPU wake call 500 ms later, counting its own attempt as the wake's
first (from pull request 597 of the original project; tools/test_gpu_wake_retry.py covers the retries of that call).

  python3 tools/test_display_power_retry.py [--baseline [REF]]
      --baseline REF   the source of that revision (default HEAD); on 3.0.4 the wake call is never queued here"""
import argparse
import pathlib
import subprocess
import tempfile

root = pathlib.Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument('--baseline', nargs='?', const='HEAD', metavar='REF')
args = parser.parse_args()
if args.baseline:
    source = subprocess.check_output(['git', 'show', f'{args.baseline}:kexts/NVRMFB/fb/nvrm-fb.cpp'], cwd=root, text=True)
else:
    source = (root / 'kexts/NVRMFB/fb/nvrm-fb.cpp').read_text()
start = source.index('bool NVRMNVDAFramebuffer::setHeadPower(')
methods = source[start:source.index('IOReturn NVRMNVDAFramebuffer::setAttribute(', start)]
head_off = source[source.index('bool NVRMNVDAFramebuffer::headOff()'):start]
assert 'submitConfig(cfg, rep, NV_TRUE)' in head_off
assert 'fKms->applyModeSetConfig' not in head_off
connection = source[source.index('IOReturn NVRMNVDAFramebuffer::setAttributeForConnection('):]
# per-connection power stays the 1.14.7 no-op success: head power follows setPowerState only
connection_power = connection[:connection.index('switch (attribute)')]
assert 'return kIOReturnSuccess' in connection_power and 'setHeadPower' not in connection_power
fixture = r'''
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <mutex>
using IOReturn=int;
typedef int SInt32;
enum { kMillisecondScale=1000000 };
static unsigned intervalMs, scale;
static void clock_interval_to_deadline(uint32_t interval,uint32_t factor,uint64_t *deadline) { intervalMs=interval; scale=factor; *deadline=1000+(uint64_t)interval*factor; }
struct IOService {};
enum { kIOPMAckImplied=0, kIOFBNotifyDidPowerOn=1, kIOFBNotifyWillPowerOff=2 };
static bool disabled=false, lockAvailable=true;
static std::recursive_mutex displayLock;
struct NVRMDisplayGate {
    NVRMDisplayGate() { displayLock.lock(); }
    ~NVRMDisplayGate() { displayLock.unlock(); }
    bool valid() { return lockAvailable; }
};
static bool PE_parse_boot_argn(const char*,char*,unsigned long) { return disabled; }
static int OSAddAtomic(int n,int *value) { int old=*value;*value+=n;return old; }
#define FBLOG(...) ((void)0)
#define FBLOGV(...) ((void)0)
struct NVRMNVDAFramebuffer {
    bool fHeadSlept=false, fModeSet=true, sleepOK=true, wakeOK=true;
    bool fPowerOn=true, pagingState=true, fSysSlept=false, gpuReady=true;
    unsigned familyWakes=0;
    void *fSurf=this;
    unsigned fHead=0, sleeps=0, wakes=0, notifications=0, audioPublishes=0;
    void publishDisplayAudioProperties() { audioPublishes++; }
    unsigned ddcCalls=0; bool ddcLast=true;
    void ddcSetPowered(bool on) { ddcCalls++; ddcLast=on; }   // DDC/CI follows display power (w29-display)
    int fStopping=0, fReflipRc=0;
    bool headOff() { sleeps++; return sleepOK; }
    bool reflip() { wakes++; return wakeOK; }
    void handleEvent(int event) { if(event==kIOFBNotifyDidPowerOn) notifications++; }
    bool rmGpuReady() { return gpuReady; }
    void familyDidWake() { familyWakes++; notifications++; pagingState=true; }
    void *fGpuWake=(void *)1; unsigned gpuWakeQueued=0; unsigned long long gpuWakeDeadline=~0ull;
    volatile int fGpuWakeTries=0;
    void scheduleCall(void *, unsigned long long deadline) { gpuWakeQueued++; gpuWakeDeadline=deadline; }
    bool setHeadPower(bool awake);
    IOReturn setPowerState(unsigned long ordinal,IOService *device);
};
'''
checks = r'''
int main() {
    NVRMNVDAFramebuffer f;
    assert(f.setHeadPower(true) && !f.wakes);
    f.sleepOK=false; assert(!f.setHeadPower(false) && !f.fHeadSlept);
    f.sleepOK=true; assert(f.setHeadPower(false) && f.fHeadSlept);
    unsigned sleeps=f.sleeps; assert(f.setHeadPower(false) && f.sleeps==sleeps);
    f.fGpuWakeTries=2; f.wakeOK=false; f.setPowerState(1,nullptr);
    // a failed wake stays retryable, and WindowServer is still told the display is on (1.14.7 behaviour)
    assert(f.fHeadSlept && f.wakes==1 && f.notifications==1);
    // ... and it queues the GPU wake call, because after a display sleep (as here) or a system wake nothing else asks
    // for head power (pull request 597): 500 ms later, not straight after the failure, and with this attempt counted
    // as the first of the wake, so that the wake makes kGpuWakeAttempts in all and not one more
    assert(f.gpuWakeQueued==1 && f.gpuWakeDeadline!=0 && f.gpuWakeDeadline!=~0ull);
    assert(intervalMs==500 && scale==kMillisecondScale && f.fGpuWakeTries==1);
    // the display's audio is published again only once the head really is back (PR #366)
    assert(f.audioPublishes==0);
    f.wakeOK=true; f.setPowerState(1,nullptr);
    assert(!f.fHeadSlept && f.wakes==2 && f.notifications==2 && f.audioPublishes==1);
    assert(f.gpuWakeQueued==1);   // a wake that lit the head queues nothing
    f.setPowerState(1,nullptr); assert(f.wakes==2);
    f.setPowerState(0,nullptr); assert(f.fHeadSlept);
    // -nvrmnodpms: display sleep leaves the head on, but a head that is off (GPU standby) is still lit again
    disabled=true; unsigned wakes=f.wakes;
    assert(f.setHeadPower(true) && !f.fHeadSlept && f.wakes==wakes+1);
    sleeps=f.sleeps; assert(f.setHeadPower(false) && !f.fHeadSlept && f.sleeps==sleeps);
    wakes=f.wakes;
    disabled=false; f.fStopping=1; assert(!f.setHeadPower(true)); f.fStopping=0;
    lockAvailable=false; assert(!f.setHeadPower(true)); lockAvailable=true;
    assert(f.setHeadPower(true) && !f.fHeadSlept);
    f.fModeSet=false; assert(f.setHeadPower(false) && !f.fHeadSlept);
    f.fModeSet=true;
    // system sleep: NVRM already put the GPU in standby, so the head counts as off without a push to NVKMS
    f.gpuReady=false; sleeps=f.sleeps; f.setPowerState(0,nullptr);
    assert(f.fHeadSlept && f.sleeps==sleeps && !f.fPowerOn);
    // a display wake before NVRM resumed the GPU pushes nothing and leaves the head for NVRM's resume to light
    wakes=f.wakes; unsigned notes=f.notifications; f.setPowerState(1,nullptr);
    assert(f.fHeadSlept && f.wakes==wakes && f.fPowerOn && f.notifications==notes+1 && !f.familyWakes);
    assert(f.gpuWakeQueued==1);   // NVRM's resume queues it then, not this power-on
    // the first power-on after a system sleep lights the head and does the family's wake work once
    f.setPowerState(0,nullptr); f.gpuReady=true; f.fSysSlept=true; f.pagingState=false;
    notes=f.notifications; f.setPowerState(1,nullptr);
    assert(!f.fHeadSlept && f.wakes==wakes+1 && f.familyWakes==1 && !f.fSysSlept && f.pagingState);
    assert(f.notifications==notes+1);
    f.setPowerState(1,nullptr); assert(f.familyWakes==1 && f.notifications==notes+2);
    // DDC/CI is told about every power transition, awake or asleep, so a sleeping monitor is not sent commands
    unsigned dc=f.ddcCalls; f.setPowerState(0,nullptr); assert(f.ddcCalls==dc+1 && !f.ddcLast);
    f.setPowerState(1,nullptr); assert(f.ddcCalls==dc+2 && f.ddcLast);
    // a system wake whose head does not light still does the family's wake work, and queues the GPU wake call
    f.setPowerState(0,nullptr); f.fSysSlept=true; f.pagingState=false; f.wakeOK=false;
    unsigned queued=f.gpuWakeQueued; wakes=f.wakes; f.setPowerState(1,nullptr);
    assert(f.fHeadSlept && f.wakes==wakes+1 && f.familyWakes==2 && f.pagingState && f.gpuWakeQueued==queued+1);
    // with no wake call allocated the failure is only logged
    f.setPowerState(0,nullptr); f.fGpuWake=nullptr; queued=f.gpuWakeQueued; f.setPowerState(1,nullptr);
    assert(f.fHeadSlept && f.gpuWakeQueued==queued);
    f.fGpuWake=(void *)1; f.wakeOK=true; f.setPowerState(1,nullptr); assert(!f.fHeadSlept && f.gpuWakeQueued==queued);
    puts("PASS: failed sleep, failed wake retry with notification and a queued GPU wake call, audio republished only on a real wake, connection power no-op, repeated requests, opt-out, shutdown, unavailable gate, GPU standby, wake before resume, system wake, failed system wake");
}
'''
with tempfile.TemporaryDirectory() as directory:
    path = pathlib.Path(directory)
    (path / 'test.cpp').write_text(fixture + methods + checks)
    subprocess.run(['clang++', '-std=c++17', '-pthread', str(path / 'test.cpp'),
                    '-o', str(path / 'test')], check=True)
    subprocess.run([str(path / 'test')], check=True)
