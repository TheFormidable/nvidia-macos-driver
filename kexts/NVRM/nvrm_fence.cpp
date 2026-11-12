/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 *
 * The surface fence: the kernel knows which connection is still drawing into which IOSurface, so that a reader
 * (another process, or NVAccel before a flip or a page-off) sleeps until that work is done instead of polling a mark.
 * A producer registers its channel, opens a hold on the surfaces a command buffer writes, arms it with the channel's
 * completion value and closes it; the kernel reads the completion value from the producer's own semaphore page and
 * the channel's error from its notifier. docs/SURFACE-FENCE.md is the description for people.
 *
 * This file is the kernel around the logic: the lock, the clock, the sleep and wakeup, the two pages per channel, the
 * boot-arg, the log lines and the counters. Every decision (what a reader waits for, when a hold is given up, who is
 * refused) is made in nvrm_fence_table.h, which the host tests run as it is.
 *
 * On by default. Boot-arg nvrmfence=0 switches it off for the boot: every request is answered "off" before a page is
 * touched and every caller keeps the older mechanism (marks, tags, polling). Nothing here is writable from user space.
 * All of debug.nvrm_fence_* is read-only: state (0 off by boot-arg, 1 on, 2 switched itself off, 3 stopped or not
 * started), accel, kicks, and the table's counters, listed with their meaning where they are declared below.
 */

#include "nv-xnu.h"
#include <IOKit/IOLib.h>
#include <IOKit/IOMemoryDescriptor.h>
#include <kern/clock.h>
#include <kern/sched_prim.h>
#include <libkern/OSAtomic.h>
#include <mach/mach_time.h>
#include <pexpert/pexpert.h>
extern "C" {
#include <sys/proc.h>
#include <sys/sysctl.h>
}
#include "nvrm_fence.h"
#include "nvrm_fence_table.h"

extern "C" task_t current_task(void);
extern "C" proc_t current_proc(void);
extern "C" int sysctlbyname(const char *, void *, size_t *, void *, size_t);
// The one question this file asks the rest of the driver (os-xnu2.cpp): is this physical range inside one of the GPU's
// memory windows? It is the test that decides which mappings NVRM hands a process as device memory, and it only compares
// against the window addresses recorded when the GPU started: no lock, no RM call, no hardware.
extern "C" bool nv_pm_is_gpu(NvU64 start, NvU64 size);

// ---- state ---------------------------------------------------------------------------------------------------------

// gFLock is the one lock of this file, and a leaf.
//   Order: a caller may already hold its user client's gate (clientClose -> nvrm_fence_owner_gone) or NVAccel's
//   accelerator lock (its waits, through the function table of nvrm_fence_kernel_ops). So: gate -> gFLock and
//   accelerator lock -> gFLock, never the other way round: nothing that runs under gFLock takes another lock or calls
//   the RM, NVAccel, IOAccelerator or IOSurface.
//   Under it: the table functions, the clock, and reads of a channel's pinned pages (IOMemoryDescriptor::readBytes on
//   a descriptor this file prepared, which walks the descriptor's page list and copies from the physical map: it does
//   not block, allocate or take a lock). Nothing else: no allocation, no sleep, no wakeup, no log line, no IOKit
//   object made, completed or released.
//   Before it: pinning a page (that can fault and block). After it: unpinning, every wakeup, every log line.
//   The interrupt path (nvrm_fence_kick) takes no lock at all.
static IOLock *gFLock;                       // made by the first GPU start, never freed

// The table is static data: nothing to allocate at start, so nothing that can fail there, and it is never freed, so
// a sleeper that outlives the last GPU stop still reads valid memory. All zero until the first start; the table's own
// functions treat that as "holds nothing, accepts nothing".
static struct nvf_table gT;                  // under gFLock, except the two lock-free reads noted where they are made

// The kernel's hold on one channel's two pages. Each descriptor is prepared, which wires the page and keeps it alive
// whatever the process or the RM do with their own mappings, until complete(). The slot owns both. They are let go
// (complete and release, after the lock) by CHAN_DEL, by a CHAN_VERIFY that fails, by CHAN_REBIND for the notifier
// it replaces, by nvrm_fence_pid_gone for everything an exiting process has, and by nvrm_fence_owner_gone for
// everything a connection still has; every connection ends there, so nothing stays pinned past its close.
// What a view costs while it is held: the page stays wired, and the memory object it belongs to cannot be taken apart.
// When a producer frees or unmaps a registered page before its CHAN_DEL, the kernel waits for the view to go before it
// destroys that object, and it waits on the one thread that reaps dead memory objects: the objects queued behind this
// one (those that still had paging in flight when they died) are not freed either until the view goes. NVK deletes the
// registration first, and when a process exits its connections close, and the views go, before its address space is
// taken down. So this happens only to a live process that breaks the rule, and it ends with its connection at the
// latest. Any driver that keeps user memory prepared has the same cost; it is the price of never mapping the page.
struct NvfView {
    IOMemoryDescriptor *sem, *err;           // the semaphore page; the page that contains the error notifier
    UInt64 probe0;                           // the probe word as it read at CHAN_ADD: CHAN_VERIFY must read another
    UInt32 errOff, errBase;                  // the notifier's offset in its page; its status when it was registered
};
static NvfView gFMap[NVF_CHANS];             // under gFLock; indexed like gT.c[]

// The interrupt generation in its low 32 bits and, as a whole, debug.nvrm_fence_kicks. Also the event sleepers wait on.
static volatile SInt64 gFKick;
static volatile SInt32 gFGen;                // the table-change generation: ++ after an unlock that ended or flagged a hold
static volatile SInt32 gFSleepers;           // threads between "I am going to sleep" and "I am back"
static volatile SInt32 gFState = 3;          // debug.nvrm_fence_state
static volatile SInt32 gFAccel;              // 1 once NVAccel fetched the function table
static volatile SInt32 gFBoot;               // the boot-arg: 0 not read yet, 1 on, 2 nvrmfence=0
static volatile SInt32 gFSysctls;            // 1 once the sysctls are registered
static volatile SInt32 gFMapLogs;            // mapping failures logged so far (rate limit)
static volatile SInt64 gFNextOwner;          // owner ids handed out
static unsigned gFChangesSeen;               // under gFLock: gT.changes at the last unlock
static unsigned gFCards, gFSuspended;        // under gFLock: GPUs started; of those, GPUs between SUSPENDING and RESUMED
static bool gFInited, gFLost;                // under gFLock: the table was initialised; a resume failed (off until reboot)
static UInt64 gFPhysTop;                     // installed memory + 4 GB: where the physical map ends; 0 = not known,
                                             // no page is taken. Written once by the first start, before the layer is on

static const SInt32 kNvfMaxSleepers = 256;   // user-space waits beyond this are refused (FULL); NVAccel's never are

// ---- counters ------------------------------------------------------------------------------------------------------

#define NVF_STAT(x) ((long long *)&gT.stat[NVF_STAT_##x])
SYSCTL_INT(_debug, OID_AUTO, nvrm_fence_state, CTLFLAG_RD | CTLFLAG_LOCKED, (int *)&gFState, 0, "surface fence: 0 off by nvrmfence=0, 1 on, 2 switched itself off, 3 stopped or not started");
SYSCTL_INT(_debug, OID_AUTO, nvrm_fence_accel, CTLFLAG_RD | CTLFLAG_LOCKED, (int *)&gFAccel, 0, "1 = NVAccel's waits read the fence table");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_kicks, CTLFLAG_RD | CTLFLAG_LOCKED, (long long *)&gFKick, "interrupts the work loop served (whatever the RM answered) plus bottom-half runs, while on");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_opens, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(OPENS), "holds opened");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_arms, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(ARMS), "holds given a GPU target");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_arm_done, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(ARM_DONE), "of those, already complete when armed");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_closes, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(CLOSES), "holds closed by their writer");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_close_gone, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(CLOSE_GONE), "CLOSE calls that found no hold");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_auto, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(AUTO), "compositor holds ended by the kernel at GPU completion");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_end_owner, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(END_OWNER), "holds ended because their writer's connection closed");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_end_chan_err, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(END_CHAN_ERR), "holds ended by a channel fault");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_end_chan_del, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(END_CHAN_DEL), "holds ended because their channel was deleted first");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_end_pm, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(END_PM), "holds ended by GPU sleep");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_pid_gone, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(PID_GONE), "connections whose holds and channels ended because their process was exiting, before the RM freed anything of it");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_refused_full, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(REFUSED_FULL), "requests refused for room: table or quota");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_refused_denied, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(REFUSED_DENIED), "compositor holds asked for by a process that is not WindowServer");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_refused_stale, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(REFUSED_STALE), "requests naming an unknown hold or channel");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_refused_distrust, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(REFUSED_DISTRUST), "holds refused to a distrusted connection");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_chans, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(CHANS), "channels watched now");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_chan_adds, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(CHAN_ADDS), "channels registered since boot");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_chan_noirq, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(CHAN_NOIRQ), "channels whose completions arrive without an interrupt, now");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_chan_rebinds, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(CHAN_REBINDS), "rebuilt channels given a new error notifier");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_map_fail, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(MAP_FAIL), "channel pages that could not be pinned, or that are not system memory");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_map_mismatch, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(MAP_MISMATCH), "channels whose page did not show what the process wrote");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_live, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(LIVE), "holds that delay readers now");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_live_max, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(LIVE_MAX), "most such holds at once");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_reclaimed, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(RECLAIMED), "slots of given-up or forgotten holds taken back for new ones");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_waits, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(WAITS), "waits that found something pending");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_wait_none, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(WAIT_NONE), "waits and looks that found nothing pending");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_look_pending, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(LOOK_PENDING), "looks that found something pending");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_wait_ready, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(WAIT_READY), "waits ended by completion");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_wait_dead, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(WAIT_DEAD), "waits ended by writer exit, channel fault or GPU sleep");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_wait_timeout, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(WAIT_TIMEOUT), "waits ended because a hold was given up");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_wait_budget, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(WAIT_BUDGET), "waits ended by the caller's own timeout");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_wait_aborted, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(WAIT_ABORTED), "waits ended by a signal");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_wait_full, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(WAIT_FULL), "waits refused because too many threads sleep");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_wake_irq, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(WAKE_IRQ), "sleeps ended by an interrupt");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_wake_state, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(WAKE_STATE), "sleeps ended by a change of the table");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_wake_slice, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(WAKE_SLICE), "sleeps ended by their own time slice");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_done_on_irq, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(DONE_ON_IRQ), "waits that found their surface free after an interrupt");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_done_on_state, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(DONE_ON_STATE), "the same after a change of the table");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_done_on_slice, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(DONE_ON_SLICE), "the same after a time slice: no interrupt told them");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_expired_gpu, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(EXPIRED_GPU), "holds given up after 2000 ms");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_expired_cpu, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(EXPIRED_CPU), "holds given up after 100 ms with their writer's GPU idle");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_expired_penalty, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(EXPIRED_PENALTY), "holds given up in a penalty window: after 20 ms, a compositor hold after 100 ms");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_strikes, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(STRIKES), "strikes against connections whose holds were given up");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_owners_distrusted, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(OWNERS_DISTRUSTED), "connections refused after three strikes");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_close_gpu_agree, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(CLOSE_GPU_AGREE), "armed holds closed with the kernel seeing their work complete");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_close_gpu_behind, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(CLOSE_GPU_BEHIND), "armed holds closed before the kernel saw their work complete");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_wait_max_us, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(WAIT_MAX_US), "longest wait that slept, microseconds");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_wait_lt1ms, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(WAIT_LT1MS), "waits that slept under 1 ms");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_wait_lt10ms, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(WAIT_LT10MS), "waits that slept 1 to 10 ms");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_wait_lt100ms, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(WAIT_LT100MS), "waits that slept 10 to 100 ms");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_wait_ge100ms, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(WAIT_GE100MS), "waits that slept 100 ms or more");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_seq_waits, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(SEQ_WAITS), "producer waits for its own channel");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_seq_slept, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(SEQ_SLEPT), "of those, slept in the kernel");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_seq_refused, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(SEQ_REFUSED), "of those, told to poll: the channel raises no interrupt");

// Once per boot, also when the layer is switched off, so that state 0 can be read. Never unregistered: NVRM.kext is not
// unloaded while the system runs, like every other debug.nvrm_* sysctl.
static void nvfSysctls(void)
{
    if (!OSCompareAndSwap(0, 1, (volatile UInt32 *)&gFSysctls)) return;
    sysctl_register_oid(&sysctl__debug_nvrm_fence_state);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_accel);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_kicks);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_opens);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_arms);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_arm_done);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_closes);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_close_gone);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_auto);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_end_owner);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_end_chan_err);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_end_chan_del);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_end_pm);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_pid_gone);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_refused_full);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_refused_denied);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_refused_stale);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_refused_distrust);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_chans);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_chan_adds);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_chan_noirq);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_chan_rebinds);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_map_fail);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_map_mismatch);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_live);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_live_max);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_reclaimed);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_waits);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_wait_none);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_look_pending);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_wait_ready);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_wait_dead);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_wait_timeout);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_wait_budget);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_wait_aborted);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_wait_full);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_wake_irq);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_wake_state);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_wake_slice);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_done_on_irq);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_done_on_state);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_done_on_slice);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_expired_gpu);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_expired_cpu);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_expired_penalty);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_strikes);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_owners_distrusted);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_close_gpu_agree);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_close_gpu_behind);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_wait_max_us);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_wait_lt1ms);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_wait_lt10ms);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_wait_lt100ms);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_wait_ge100ms);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_seq_waits);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_seq_slept);
    sysctl_register_oid(&sysctl__debug_nvrm_fence_seq_refused);
}

// ---- the lock, the clock, the log ----------------------------------------------------------------------------------

static unsigned long long nvfNowNs(void)
{
    uint64_t ns = 0;
    absolutetime_to_nanoseconds(mach_absolute_time(), &ns);
    return ns;
}

static const char *nvfStateName(unsigned state)
{
    return state == NVRM_FENCE_E_OPEN ? "open" : state == NVRM_FENCE_E_ARMED ? "armed" :
           state == NVRM_FENCE_E_GPU_DONE ? "gpu-done" : "?";
}

// What the table found worth a line. Called after the lock is released; the table has already thinned each kind to the
// first 8 and then every 256th.
static void nvfLog(const struct nvf_event *ev)
{
    switch (ev->kind) {
    case NVF_EV_STRIKE:
        IOLog("NVRM-xnu: fence: surface 0x%x writer pid %d state %s for %u ms - given up (%s), strike %u\n", ev->sid, (int)ev->pid,
              nvfStateName(ev->state), ev->ms, ev->bound == NVF_BOUND_GPU ? "2000 ms bound" : "100 ms bound, its GPU was idle",
              (unsigned)ev->strikes);
        break;
    case NVF_EV_DISTRUST:
        IOLog("NVRM-xnu: fence: pid %d: %u holds given up - this connection's new holds are refused from now on (it keeps the older surface sync)\n",
              (int)ev->pid, (unsigned)ev->strikes);
        break;
    case NVF_EV_FUSE:
        IOLog("NVRM-xnu: fence: %u connections refused since boot - the surface fence is off until the next boot (debug.nvrm_fence_state 2)\n",
              NVF_FUSE_OWNERS);
        break;
    case NVF_EV_NOIRQ:
        IOLog("NVRM-xnu: fence: channel %u of pid %d%s completes without an interrupt - it is polled every %u us instead\n", ev->sid,
              (int)ev->pid, ev->strikes ? " (copy engine)" : "", NVF_SLICE_FAST_US);
        break;
    default:
        break;
    }
}

static void nvfLock(void) { IOLockLock(gFLock); }

// The only unlock. When a hold ended or was flagged under the lock, the change generation moves and the sleepers are
// woken: the generation first, then the look for sleepers, as in the kick (a sleeper counts itself in and queues
// itself before it reads the generations again, so one side always sees the other).
static void nvfUnlock(void)
{
    struct nvf_event ev[NVF_EVENTS];
    const bool changed = gT.changes != gFChangesSeen;
    gFChangesSeen = gT.changes;
    if (gT.fused && gFState == 1) gFState = 2;
    const unsigned n = nvf_events_take(&gT, ev, NVF_EVENTS);
    IOLockUnlock(gFLock);
    if (changed) {
        OSIncrementAtomic(&gFGen);
        if (gFSleepers > 0) thread_wakeup((event_t)&gFKick);
    }
    for (unsigned i = 0; i < n; i++) nvfLog(&ev[i]);
}

// A counter the table does not keep itself.
static void nvfCount(unsigned stat)
{
    nvfLock();
    nvf_count(&gT, stat);
    nvfUnlock();
}

// The boot-arg, read once by whoever asks first: the first GPU start, or NVAccel asking for the function table before
// it. 1 = on, 2 = nvrmfence=0.
static SInt32 nvfBoot(void)
{
    SInt32 boot = gFBoot;
    if (!boot) {
        UInt32 v = 1;
        boot = (PE_parse_boot_argn("nvrmfence", &v, sizeof(v)) && v == 0) ? 2 : 1;
        if (!OSCompareAndSwap(0, (UInt32)boot, (volatile UInt32 *)&gFBoot)) boot = gFBoot;
        if (boot == 2) gFState = 0;
    }
    return boot;
}

// ---- a channel's pages ---------------------------------------------------------------------------------------------

// Never under the lock.
static void nvfUnpin(IOMemoryDescriptor *md)
{
    if (!md) return;
    md->complete();
    md->release();
}

// One page of the calling process, wired, for the kernel to read. Called in that process's context, without the lock.
// The descriptor is not mapped into the kernel: the page is read through the physical map (nvfRead64), so no kernel
// address exists that could fault when the process or the RM tears its side down. Direction "out" (memory to device)
// asks for read access only, which also works on a page the process has mapped read-only.
// IOKit prepares device memory as readily as system memory, and a process can hand in a page of a device that it has
// mapped. So the page's physical address is looked at once here, while the page is wired and cannot move, and the page
// is given back when either is true:
//   - it is not below the end of the physical map (nvf_phys_ok, gFPhysTop). The kernel checks every address it
//     converts through that map and stops the machine on one beyond its end, and a GPU memory window can lie there
//     (this driver moves the large one to 256 GB and up where the machine allows it);
//   - it is inside one of the GPU's own memory windows, wherever they are: firmware also leaves them below 4 GB or
//     right above installed memory, inside the map. NVRM maps those windows, the register file included, into the
//     processes the RM grants them to; a read here would touch the GPU without a power reference, also while it
//     sleeps, each time somebody waits on that process's surfaces.
// A page of another device that lies inside the map cannot be told from system memory: nothing a kext may call says
// which of the two a page is. Such a page is only ever read, 8 bytes at the offsets nvfRead names, as the process that
// mapped it can read it itself; and unless it behaves like memory it fails the proof and is dropped.
static IOMemoryDescriptor *nvfPin(UInt64 pageVA)
{
    IOMemoryDescriptor *md = IOMemoryDescriptor::withAddressRange((mach_vm_address_t)pageVA, NVF_PAGE, kIODirectionOut, current_task());
    if (!md) return NULL;
    if (md->prepare() != kIOReturnSuccess) { md->release(); return NULL; }
    IOByteCount len = 0;
    const addr64_t pa = md->getPhysicalSegment(0, &len, kIOMemoryMapperNone);
    if (!nvf_phys_ok(pa, len, gFPhysTop) || nv_pm_is_gpu(pa, NVF_PAGE)) { nvfUnpin(md); return NULL; }
    return md;
}

// 8 bytes of a pinned page; `off` is 8-aligned and inside the page. One 64-bit load from the physical map, so a value
// the GPU or the process is writing is seen whole. Only ever called on a descriptor nvfPin prepared (readBytes finds
// the page through the wiring; a development kernel asserts on a descriptor without it). False when the descriptor
// has no page there. The copy itself has fault recovery but does not report a fault, so *out is zeroed first: a page
// that cannot be read reads as 0, which no probe value is.
static bool nvfRead64(IOMemoryDescriptor *md, UInt32 off, UInt64 *out)
{
    *out = 0;
    return md->readBytes(off, out, sizeof(*out)) == sizeof(*out);
}

// How the table sees a channel (nvf_read_fn), under the lock. The completion value first and the flush value second:
// the producer stores the flush value before the work can run, so "flushed > done" never misses work in flight. A
// fault is the notifier's 16-bit status (the top of its second 8 bytes) no longer being what it was at registration.
// 0 (no pages, or a page that gives nothing) makes the table treat the channel as faulted: nobody waits on it.
static int nvfRead(void *, unsigned ci, unsigned long long *done, unsigned long long *flushed, int *faulted)
{
    if (ci >= NVF_CHANS || !gFMap[ci].sem || !gFMap[ci].err) return 0;
    const NvfView *m = &gFMap[ci];
    UInt64 d = 0, f = 0, note = 0;
    if (!nvfRead64(m->sem, 0, &d)) return 0;
    if (flushed && !nvfRead64(m->sem, NVRM_FENCE_SEM_FLUSHED_OFF, &f)) return 0;
    if (!nvfRead64(m->err, m->errOff + 8, &note)) return 0;
    *done = d;
    if (flushed) *flushed = f;
    *faulted = (UInt32)(note >> 48) != m->errBase;
    return 1;
}

// Takes a channel's views out of the map, under the lock; the caller unpins them after it.
static void nvfTake(unsigned ci, IOMemoryDescriptor **sem, IOMemoryDescriptor **err)
{
    *sem = NULL; *err = NULL;
    if (ci >= NVF_CHANS) return;
    *sem = gFMap[ci].sem; *err = gFMap[ci].err;
    gFMap[ci].sem = NULL; gFMap[ci].err = NULL;
    gFMap[ci].probe0 = 0; gFMap[ci].errOff = 0; gFMap[ci].errBase = 0;
}

static void nvfMapFailed(const char *what)
{
    const SInt32 n = OSIncrementAtomic(&gFMapLogs);
    if (n < 8 || ((n + 1) & 0xFF) == 0)
        IOLog("NVRM-xnu: fence: pid %d: %s - this channel is not watched (its surfaces keep the older surface sync)\n",
              proc_selfpid(), what);
}

// ---- what the wait loop needs --------------------------------------------------------------------------------------

static void nvfOpLock(void *) { nvfLock(); }
static void nvfOpUnlock(void *) { nvfUnlock(); }
static unsigned long long nvfOpNow(void *) { return nvfNowNs(); }
static unsigned nvfOpKick(void *) { return (unsigned)gFKick; }
static unsigned nvfOpGen(void *) { return (unsigned)gFGen; }

// Sleeps until the deadline, or until a kick or a table change that came after the caller's look. The order is what
// makes that sure: count in, queue on the event, THEN read both generations again, and block only if neither moved.
// A kick moves its generation first and looks for sleepers second, so it either is seen here or sees this thread on
// the queue. clear_wait is not exported to kexts, so a thread that must not block wakes the event (itself included).
static int nvfOpSleep(void *, unsigned kickSeen, unsigned genSeen, unsigned long long deadlineNs, int abortable)
{
    if (OSIncrementAtomic(&gFSleepers) >= kNvfMaxSleepers && abortable) {
        OSDecrementAtomic(&gFSleepers);
        return NVF_SLEEP_FULL;
    }
    const unsigned long long now = nvfNowNs();
    unsigned long long us = deadlineNs > now ? (deadlineNs - now + 999ull) / 1000ull : 0;
    if (us < 50) us = 50;                              // a deadline already behind us still yields once
    if (us > NVRM_FENCE_WAIT_MAX_MS * 1000ull) us = NVRM_FENCE_WAIT_MAX_MS * 1000ull;
    wait_result_t wr = assert_wait_timeout((event_t)&gFKick, abortable ? THREAD_ABORTSAFE : THREAD_UNINT, (uint32_t)us, NSEC_PER_USEC);
    if (wr == THREAD_WAITING) {
        if ((unsigned)gFKick != kickSeen || (unsigned)gFGen != genSeen) thread_wakeup((event_t)&gFKick);
        wr = thread_block(THREAD_CONTINUE_NULL);
    }
    OSDecrementAtomic(&gFSleepers);
    return wr == THREAD_INTERRUPTED ? NVF_SLEEP_ABORTED : NVF_SLEPT;
}

static const struct nvf_wait_ops gFOps = { nvfOpLock, nvfOpUnlock, nvfOpNow, nvfOpKick, nvfOpGen, nvfOpSleep, nvfRead };

// ---- requests from user space --------------------------------------------------------------------------------------

// The connection's owner id, given on its first CHAN_ADD or OPEN: a number, never a pointer, and never 0.
static UInt64 nvfOwner(volatile UInt64 *ownerId)
{
    UInt64 id = *ownerId;
    if (!id) {
        id = (UInt64)OSIncrementAtomic64(&gFNextOwner) + 1;
        if (!OSCompareAndSwap64(0, id, ownerId)) id = *ownerId;      // another thread of this connection was first
    }
    return id;
}

// The test NVAccel makes for its mark escape: compositor-class holds delay WindowServer's own flips, so only
// WindowServer may take them.
static bool nvfCallerIsWindowServer(void)
{
    static const char want[] = "WindowServer";
    char name[32];
    name[0] = 0;
    proc_selfname(name, (int)sizeof(name));
    for (unsigned i = 0; i < sizeof(want); i++) if (name[i] != want[i]) return false;
    return true;
}

// Is the calling thread's process exiting? Asked under the lock by the two requests that add state. The exit sets the
// flag (P_LEXIT, under the process lock) before it closes any of the process's connections, and nvrm_fence_pid_gone
// runs from such a close and takes the fence lock: so a request that takes the lock after it sees the flag and adds
// nothing, and one that took the lock before it added state that nvrm_fence_pid_gone then finds and ends. Without
// this, a thread of the dying process still inside a request when its RM connection closed could open a hold that
// nothing ends until its fence connection closes. proc_exiting only reads a word of the process's own structure,
// which cannot go away while one of its threads runs here: no lock, no sleep.
static bool nvfCallerExiting(void)
{
    return proc_exiting(current_proc()) != 0;
}

// CHAN_ADD. The two pages are pinned and the first proof is made before the lock: the probe word the process says it
// stored must be what the kernel reads through its own view. Any failure costs this channel only: it is never
// registered, so nothing is ever armed on it and the process's surfaces keep the older mechanism.
static void nvfChanAdd(volatile UInt64 *ownerId, const volatile SInt32 *closing, struct NVRMFenceReq *r)
{
    struct NVRMFenceChan *c = &r->u.chan;
    c->chan = 0;
    if (!nvf_sem_va_ok(c->semVA) || !nvf_err_va_ok(c->errVA, c->errBase) || !c->probe) { r->status = NVRM_FENCE_ST_BAD_ARG; return; }
    IOMemoryDescriptor *sem = nvfPin(c->semVA);
    IOMemoryDescriptor *err = sem ? nvfPin(c->errVA & ~(NVF_PAGE - 1)) : NULL;
    if (!sem || !err) {
        nvfUnpin(sem);
        nvfCount(NVF_STAT_MAP_FAIL);
        nvfMapFailed("a channel page could not be pinned, or is not system memory");
        r->status = NVRM_FENCE_ST_MAP;
        return;
    }
    UInt64 seen = 0;
    if (!nvfRead64(sem, NVRM_FENCE_SEM_PROBE_OFF, &seen) || seen != c->probe) {
        nvfUnpin(sem); nvfUnpin(err);
        nvfCount(NVF_STAT_MAP_MISMATCH);
        nvfMapFailed("the kernel's view of the semaphore page does not show what the process stored");
        r->status = NVRM_FENCE_ST_MAP;
        return;
    }
    // The id is given before the lock, so that a close racing this request either finds the id (and removes what is
    // added below) or has already set `closing` (and nothing is added). The same for the exit of this process
    // (nvfCallerExiting).
    const UInt64 owner = nvfOwner(ownerId);
    unsigned handle = 0, st;
    nvfLock();
    st = (*closing || nvfCallerExiting()) ? NVRM_FENCE_ST_OFF : nvf_chan_add(&gT, owner, (unsigned)proc_selfpid(), r->flags, &handle);
    if (st == NVRM_FENCE_ST_OK) {
        NvfView *m = &gFMap[handle & ((1u << NVF_CHAN_INDEX_BITS) - 1u)];
        m->sem = sem; m->err = err; m->probe0 = seen;
        m->errOff = (UInt32)(c->errVA & (NVF_PAGE - 1)); m->errBase = c->errBase;
        sem = NULL; err = NULL;
    }
    nvfUnlock();
    nvfUnpin(sem); nvfUnpin(err);
    c->chan = handle;
    r->status = st;
}

// CHAN_VERIFY: the second proof. The process has stored another value in the probe word after CHAN_ADD returned; the
// view pinned then must show it. That it follows a later write is what tells a live view from a snapshot or a
// copy-on-write copy, before any wait trusts it. A channel that fails is dropped.
static void nvfChanVerify(UInt64 owner, struct NVRMFenceReq *r)
{
    struct NVRMFenceChan *c = &r->u.chan;
    IOMemoryDescriptor *sem = NULL, *err = NULL;
    unsigned freed = NVF_NIL;
    nvfLock();
    const int ci = nvf_chan_find(&gT, owner, c->chan);
    int matches = 0;
    if (ci >= 0 && gFMap[ci].sem) {
        UInt64 seen = 0;
        matches = nvfRead64(gFMap[ci].sem, NVRM_FENCE_SEM_PROBE_OFF, &seen) && c->probe != 0 && seen == c->probe &&
                  seen != gFMap[ci].probe0;
    }
    r->status = nvf_chan_verify(&gT, owner, c->chan, matches, &freed);
    if (freed != NVF_NIL) nvfTake(freed, &sem, &err);
    nvfUnlock();
    nvfUnpin(sem); nvfUnpin(err);
    if (r->status == NVRM_FENCE_ST_MAP) nvfMapFailed("the kernel's view of the semaphore page did not follow a later write");
}

// CHAN_DEL, in every state once the lock exists: a process must be able to give its pages back after the layer went
// off, and an unknown handle is merely STALE.
static void nvfChanDel(UInt64 owner, struct NVRMFenceReq *r)
{
    IOMemoryDescriptor *sem = NULL, *err = NULL;
    unsigned freed = NVF_NIL;
    if (!gFLock) { r->status = NVRM_FENCE_ST_OFF; return; }
    const unsigned long long now = nvfNowNs();
    nvfLock();
    r->status = nvf_chan_del(&gT, owner, r->u.chan.chan, now, nvfRead, NULL, &freed);
    if (freed != NVF_NIL) nvfTake(freed, &sem, &err);
    nvfUnlock();
    nvfUnpin(sem); nvfUnpin(err);
}

// CHAN_REBIND: the producer rebuilt a faulted channel and its error notifier is a new one. The new page is pinned
// before the lock, swapped in under it together with the forgetting of the old fault, and the old page is let go after
// it. Nothing else about the channel changes; when the request is refused, nothing at all does.
static void nvfChanRebind(UInt64 owner, struct NVRMFenceReq *r)
{
    struct NVRMFenceChan *c = &r->u.chan;
    if (!c->chan || !nvf_err_va_ok(c->errVA, c->errBase)) { r->status = NVRM_FENCE_ST_BAD_ARG; return; }
    IOMemoryDescriptor *err = nvfPin(c->errVA & ~(NVF_PAGE - 1));
    if (!err) {
        nvfCount(NVF_STAT_MAP_FAIL);
        nvfMapFailed("the new error notifier of a rebuilt channel could not be pinned, or is not system memory");
        r->status = NVRM_FENCE_ST_MAP;
        return;
    }
    unsigned ci = NVF_NIL;
    nvfLock();
    r->status = nvf_chan_rebind(&gT, owner, c->chan, &ci);
    if (r->status == NVRM_FENCE_ST_OK && ci < NVF_CHANS) {
        IOMemoryDescriptor *old = gFMap[ci].err;
        gFMap[ci].err = err;
        gFMap[ci].errOff = (UInt32)(c->errVA & (NVF_PAGE - 1)); gFMap[ci].errBase = c->errBase;
        err = old;
    }
    nvfUnlock();
    nvfUnpin(err);          // the old page, or the new one when the request was refused
}

static void nvfOpen(volatile UInt64 *ownerId, const volatile SInt32 *closing, struct NVRMFenceReq *r)
{
    struct NVRMFenceHold *h = &r->u.hold;
    const unsigned flags = r->flags & (NVRM_FENCE_F_COMPOSITOR | NVRM_FENCE_F_AUTO);
    if (h->count < 1 || h->count > NVRM_FENCE_MAX_IDS || !h->cookie || flags == NVRM_FENCE_F_AUTO) { r->status = NVRM_FENCE_ST_BAD_ARG; return; }
    if ((flags & NVRM_FENCE_F_COMPOSITOR) && !nvfCallerIsWindowServer()) {
        nvfCount(NVF_STAT_REFUSED_DENIED);
        r->status = NVRM_FENCE_ST_DENIED;
        return;
    }
    const UInt64 owner = nvfOwner(ownerId);              // before the lock, for the same reason as in nvfChanAdd
    const unsigned pid = (unsigned)proc_selfpid();
    const unsigned long long now = nvfNowNs();
    nvfLock();
    r->status = (*closing || nvfCallerExiting()) ? NVRM_FENCE_ST_OFF : nvf_open(&gT, owner, pid, h->cookie, h->count, h->ids, h->planes, flags, now);
    nvfUnlock();
}

static void nvfArm(UInt64 owner, struct NVRMFenceReq *r)
{
    struct NVRMFenceHold *h = &r->u.hold;
    unsigned st = NVRM_FENCE_ST_OK;
    h->okMask = 0;
    if (h->count < 1 || h->count > NVRM_FENCE_MAX_IDS || !h->cookie) { r->status = NVRM_FENCE_ST_BAD_ARG; return; }
    const unsigned long long now = nvfNowNs();
    nvfLock();
    for (unsigned i = 0; i < h->count; i++) {
        const unsigned one = nvf_arm(&gT, owner, h->cookie, h->ids[i], h->chan, h->seq, r->flags & NVRM_FENCE_F_ARM_DONE, now, nvfRead, NULL);
        if (one == NVRM_FENCE_ST_OK) h->okMask |= 1u << i;
        else if (st == NVRM_FENCE_ST_OK) st = one;       // the first refusal; okMask says which ids it was not
    }
    nvfUnlock();
    r->status = st;
}

static void nvfClose(UInt64 owner, struct NVRMFenceReq *r)
{
    struct NVRMFenceHold *h = &r->u.hold;
    unsigned found = 0;
    h->okMask = 0;
    if (h->count > NVRM_FENCE_MAX_IDS || !h->cookie) { r->status = NVRM_FENCE_ST_BAD_ARG; return; }
    nvfLock();
    if (!h->count) (void)nvf_close(&gT, owner, h->cookie, 0, nvfRead, NULL, &found);      // every hold of the command buffer
    for (unsigned i = 0; i < h->count; i++) {
        if (!h->ids[i]) continue;                        // 0 would mean "every hold": only count 0 may ask for that
        (void)nvf_close(&gT, owner, h->cookie, h->ids[i], nvfRead, NULL, &found);
        if (found) h->okMask |= 1u << i;
    }
    nvfUnlock();
    r->status = NVRM_FENCE_ST_OK;
}

void nvrm_fence_user(volatile UInt64 *ownerId, const volatile SInt32 *closing, struct NVRMFenceReq *r)
{
    if (!r) return;
    if (!ownerId || !closing) { r->status = NVRM_FENCE_ST_BAD_ARG; return; }
    if (r->version != NVRM_FENCE_ABI_VERSION) { r->status = NVRM_FENCE_ST_BAD_VERSION; return; }
    if (r->op == NVRM_FENCE_OP_QUERY) {
        struct NVRMFenceQuery *q = &r->u.query;
        q->active = gFState == 1; q->accel = gFAccel != 0;
        q->capGpuMs = NVF_CAP_GPU_MS; q->capCpuMs = NVF_CAP_CPU_MS;
        q->state = (unsigned)gFState; q->pad = 0;
        r->status = NVRM_FENCE_ST_OK;
        return;
    }
    if (r->op == NVRM_FENCE_OP_CHAN_DEL) { nvfChanDel(*ownerId, r); return; }
    // Off by boot-arg, switched off, stopped, not started: the answer comes before a page is touched or the lock taken.
    if (gFState != 1) { r->status = NVRM_FENCE_ST_OFF; return; }
    switch (r->op) {
    case NVRM_FENCE_OP_CHAN_ADD:    nvfChanAdd(ownerId, closing, r); break;
    case NVRM_FENCE_OP_CHAN_VERIFY: nvfChanVerify(*ownerId, r); break;
    case NVRM_FENCE_OP_CHAN_REBIND: nvfChanRebind(*ownerId, r); break;
    case NVRM_FENCE_OP_OPEN:        nvfOpen(ownerId, closing, r); break;
    case NVRM_FENCE_OP_ARM:         nvfArm(*ownerId, r); break;
    case NVRM_FENCE_OP_CLOSE:       nvfClose(*ownerId, r); break;
    case NVRM_FENCE_OP_WAIT: {
        struct NVRMFenceWait *w = &r->u.wait;
        if (!w->id) { r->status = NVRM_FENCE_ST_BAD_ARG; break; }
        struct nvf_query q = { w->id, w->planes & 0xFFu, w->pid, r->flags & NVRM_FENCE_F_COMPOSITOR, 0, 0 };
        r->status = nvf_wait(&gT, &gFOps, NULL, &q, w->timeoutMs, (r->flags & NVRM_FENCE_F_LOOK) != 0, 1, w);
        break;
    }
    case NVRM_FENCE_OP_WAIT_SEQ: {
        struct NVRMFenceWait *w = &r->u.wait;
        r->status = nvf_wait_seq(&gT, &gFOps, NULL, *ownerId, w->id, w->seq, w->timeoutMs, 1, w);
        break;
    }
    default: r->status = NVRM_FENCE_ST_BAD_ARG; break;
    }
}

// ---- the rest of NVRM.kext -----------------------------------------------------------------------------------------

void nvrm_fence_kick(void)
{
    if (gFState != 1) return;
    OSIncrementAtomic64(&gFKick);
    if (gFSleepers > 0) thread_wakeup((event_t)&gFKick);
}

// One connection (ownerId) or every connection of one process (pid) gives its fence state back: its holds end and
// its channels leave the table under the lock, a batch at a time, and their pages are let go after it.
static void nvfGiveBack(UInt64 ownerId, unsigned pid)
{
    if (!gFLock) return;
    for (;;) {
        IOMemoryDescriptor *gone[2 * NVF_OWNER_CHANS];
        unsigned idx[NVF_OWNER_CHANS];
        nvfLock();
        const unsigned n = ownerId ? nvf_owner_gone(&gT, ownerId, idx, NVF_OWNER_CHANS) : nvf_pid_gone(&gT, pid, idx, NVF_OWNER_CHANS);
        for (unsigned i = 0; i < n; i++) nvfTake(idx[i], &gone[2 * i], &gone[2 * i + 1]);
        nvfUnlock();
        for (unsigned i = 0; i < 2 * n; i++) nvfUnpin(gone[i]);
        if (n < NVF_OWNER_CHANS) break;                  // a full batch means there may be more
    }
}

void nvrm_fence_owner_gone(UInt64 ownerId)
{
    if (ownerId) nvfGiveBack(ownerId, 0);
}

void nvrm_fence_pid_gone(int pid)
{
    if (pid > 0) nvfGiveBack(0, (unsigned)pid);
}

void nvrm_fence_start(void)
{
    const SInt32 boot = nvfBoot();
    nvfSysctls();
    if (boot == 2) return;
    if (!gFPhysTop) {
        // The physical map of x86-64 macOS covers the memory the booter reported plus 4 GB; hw.memsize is that memory
        // or less (boot-arg maxmem), so this end is never beyond the map's.
        UInt64 mem = 0;
        size_t l = sizeof(mem);
        if (sysctlbyname("hw.memsize", &mem, &l, NULL, 0) == 0 && mem && mem < (1ull << 46)) gFPhysTop = mem + (4ull << 30);
    }
    if (!gFLock) {
        IOLock *lock = IOLockAlloc();
        if (!lock) return;                               // stays "not started": every request is refused, nothing to undo
        if (!OSCompareAndSwapPtr(NULL, lock, (void * volatile *)&gFLock)) IOLockFree(lock);
    }
    nvfLock();
    if (!gFInited) { nvf_init(&gT); gT.off = 1; gFChangesSeen = gT.changes; gFInited = true; }
    // The first GPU switches the layer on, also again after a clean last stop; never after a failed resume or after
    // the layer switched itself off, which both last until the next boot.
    if (++gFCards == 1 && !gFLost && !gT.fused) {
        gT.off = 0; gT.paused = 0;
        gFSuspended = 0;
        gFState = 1;
    }
    nvfUnlock();
}

void nvrm_fence_stop(void)
{
    if (!gFLock) return;
    nvfLock();
    if (gFCards > 0 && --gFCards == 0) {                 // a stop without its start does nothing
        gT.off = 1;
        nvf_end_all(&gT, NVF_END_SHUTDOWN);              // sleepers wake, find nothing pending and leave with DEAD
        if (gFState == 1) gFState = 3;
        gFSuspended = 0;
    }
    nvfUnlock();
}

void nvrm_fence_pm(int what)
{
    if (!gFLock) return;
    nvfLock();
    if (gFCards > 0) {                                   // a GPU that sleeps before any start finished: nothing to do
        if (what == NVRM_FENCE_PM_SUSPENDING) {
            gFSuspended++;
            gT.paused = 1;                               // holds are refused until the matching RESUMED
            nvf_end_all(&gT, NVF_END_PM);                // and every reader leaves now, before the interrupts stop
        } else if (what == NVRM_FENCE_PM_RESUMED || what == NVRM_FENCE_PM_LOST) {
            if (gFSuspended > 0 && --gFSuspended == 0) gT.paused = 0;
            if (what == NVRM_FENCE_PM_LOST) {            // the GPU did not come back: as the last stop, until the next boot
                gFLost = true;
                gT.off = 1;
                nvf_end_all(&gT, NVF_END_SHUTDOWN);
                if (gFState == 1) gFState = 3;
            }
        }
    }
    nvfUnlock();
}

// NVAccel's wait. Compositor-class holds only, whatever `flags` says: that is what the lock-free test below is about,
// and it is the only kind NVAccel asks for. With no such hold alive this costs two loads and takes no lock (both are
// plain reads of words that only change under the lock: a hold opened this instant is one the caller came before).
static unsigned nvfKernelWait(struct NVRMFenceWait *w, unsigned flags)
{
    if (!w) return NVRM_FENCE_ST_BAD_ARG;
    w->result = NVRM_FENCE_R_NONE; w->waitedUs = 0; w->ownerPid = 0; w->state = 0; w->blockers = 0;
    if (gFState != 1 || !gT.liveCompositor || !w->id) return NVRM_FENCE_ST_OK;
    struct nvf_query q = { w->id, w->planes & 0xFFu, w->pid, NVRM_FENCE_F_COMPOSITOR, 0, 0 };
    return nvf_wait(&gT, &gFOps, NULL, &q, w->timeoutMs, (flags & NVRM_FENCE_F_LOOK) != 0, 0, w);
}

bool nvrm_fence_off_by_bootarg(void)
{
    return nvfBoot() == 2;
}

bool nvrm_fence_kernel_ops(struct NVRMFenceKernelOps *o)
{
    if (!o || o->version != NVRM_FENCE_ABI_VERSION || o->size != sizeof(*o)) return false;
    if (nvfBoot() == 2) return false;
    o->wait = nvfKernelWait;
    gFAccel = 1;
    return true;
}
