/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

// nv_xnu_timers.cpp: the two kinds of timer the RM asks its OS layer for.
//
// Nano timers (nv_create_nano_timer and friends): one-shot timers with a nanosecond deadline, used by the RM's timer
// service (tmrEventScheduleRel). On Linux they are hrtimers; here each is a thread call.
// RC timer (nv_start_rc_timer, nv_stop_rc_timer): one per GPU, 1 Hz, whose callback rm_run_rc_callback runs the RM's
// 1 Hz list (osRun1HzCallbacksNow): the check that the GPU still answers on the bus, the NVENC session statistics, the
// GSP log poll, and the channel watchdog once a client turns it on. The GSP periodic health check is NOT on it: the RM
// registers that one only with the registry key RmGspPeriodicHealthcheck=1, which NVIDIA ships off and this driver
// does not set.
//
// The problem both share: a thread call that has already been taken off its queue by a thread-call thread cannot be
// stopped, and thread_call_cancel_wait is not exported to kexts. So a callback can start after the cancel returned. It
// used to then read a timer that had been freed (nano) or a GPU that had been torn down (RC). Here:
//   - the timer storage is never freed: nano timers live in slabs of 32 that are kept for good and reused, the RC slots
//     are a static table; a callback that comes late always reads valid memory;
//   - each arm carries a generation token (the thread call's second parameter); cancel, destroy, stop, suspend and
//     detach move the generation on, so a late callback finds a token that no longer matches and returns without
//     touching the RM (nv_xnu_quiesce.h, NvXnuGen);
//   - after moving the generation on, they wait until no callback is inside, bounded: 2 s for a nano timer (50 ms when
//     the caller may not sleep), 10 s for the RC timer (2 s on the way into sleep). A wait that runs out is counted.
//     A nano timer that is destroyed while a callback may still be inside (its wait ran out, or was not made: asked
//     from inside the callback, or nvrmnanowait=0) is never reused, and that is counted too: each one makes the pool
//     one slot smaller for the rest of the boot.
//
// Opt-outs, each read once: nvrmrctimer=0 (the RC timer is never started: nv->rc_timer_enabled stays 0, as with the old
// stubs) and nvrmnanowait=0 (cancel and destroy of a nano timer do not wait for a running callback). The old opt-in
// spelling nvrmrctimer=1 is accepted and means the default.
//
// Locks: the nano pool's simple lock (interrupts off) is a leaf; nothing is called while it is held but list and
// counter updates. No other lock is taken here. The RM calls nv_stop_rc_timer with its API and GPU locks held; the tick
// only tries the GPU lock (rm_run_rc_callback, GPU_LOCK_FLAGS_COND_ACQUIRE), so the wait cannot be for the caller.
//
// Counters (all read-only sysctls, registered by nv_xnu_timers_sysctls()):
//   debug.nvrm_rc_armed           GPUs whose RC timer runs
//   debug.nvrm_rc_ticks           ticks that called the RM
//   debug.nvrm_rc_runs            ticks the RM ran its list on
//   debug.nvrm_rc_refused         ticks the RM refused (GPU not at full power, in reset or lost): the timer keeps going
//   debug.nvrm_rc_stale           late ticks turned away by their generation
//   debug.nvrm_rc_wait_timeouts   waits for a running tick that ran out (should stay 0)
//   debug.nvrm_nano_slots         nano timer slots made (never freed)
//   debug.nvrm_nano_live          nano timers created and not yet destroyed
//   debug.nvrm_nano_fires         nano timer callbacks run
//   debug.nvrm_nano_stale         late nano callbacks turned away by their generation
//   debug.nvrm_nano_waits         cancels and destroys that had to wait for a running callback
//   debug.nvrm_nano_wait_timeouts such waits that ran out (should stay 0); at a destroy the slot is then retired
//   debug.nvrm_nano_leaked        nano timers the RM had not destroyed when its GPU stopped
//   debug.nvrm_nano_retired       slots taken out of use for good: the leaked ones, and each timer destroyed while a
//                                 callback could still be inside
//   debug.nvrm_nano_bad_handle    calls with a handle that was not a live nano timer (a second destroy, for example)

#include "nv-xnu.h"
#include "nv_xnu_kcore.h"
#include "nv_xnu_quiesce.h"
#include "nv_xnu_slots.h"
#include "nvrm_devtab.h"
#include <IOKit/IOLib.h>
#include <IOKit/IOLocks.h>
#include <kern/thread.h>
#include <kern/thread_call.h>
#include <kern/clock.h>
#include <kern/sched_prim.h>
#include <mach/mach_time.h>
#include <sys/sysctl.h>
#include <pexpert/pexpert.h>

#define KLOG(fmt, ...) do { kprintf("NVRM-xnu: " fmt "\n", ##__VA_ARGS__); IOLog("NVRM-xnu: " fmt "\n", ##__VA_ARGS__); } while (0)

extern "C" {

static int64_t gRcArmed, gRcTicks, gRcRuns, gRcRefused, gRcStale, gRcWaitTimeouts;
static int64_t gNanoSlots, gNanoLive, gNanoFires, gNanoStale, gNanoWaits, gNanoWaitTimeouts, gNanoLeaked, gNanoBadHandle;
static int64_t gNanoRetired;
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_rc_armed, CTLFLAG_RD | CTLFLAG_LOCKED, &gRcArmed, "GPUs whose RC timer runs");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_rc_ticks, CTLFLAG_RD | CTLFLAG_LOCKED, &gRcTicks, "RC timer ticks");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_rc_runs, CTLFLAG_RD | CTLFLAG_LOCKED, &gRcRuns, "RC timer ticks the RM accepted");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_rc_refused, CTLFLAG_RD | CTLFLAG_LOCKED, &gRcRefused, "RC timer ticks the RM refused");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_rc_stale, CTLFLAG_RD | CTLFLAG_LOCKED, &gRcStale, "late RC timer ticks turned away");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_rc_wait_timeouts, CTLFLAG_RD | CTLFLAG_LOCKED, &gRcWaitTimeouts, "waits for a running RC tick that ran out");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_nano_slots, CTLFLAG_RD | CTLFLAG_LOCKED, &gNanoSlots, "nano timer slots made");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_nano_live, CTLFLAG_RD | CTLFLAG_LOCKED, &gNanoLive, "nano timers in use");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_nano_fires, CTLFLAG_RD | CTLFLAG_LOCKED, &gNanoFires, "nano timer callbacks run");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_nano_stale, CTLFLAG_RD | CTLFLAG_LOCKED, &gNanoStale, "late nano timer callbacks turned away");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_nano_waits, CTLFLAG_RD | CTLFLAG_LOCKED, &gNanoWaits, "nano timer cancels that waited for a callback");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_nano_wait_timeouts, CTLFLAG_RD | CTLFLAG_LOCKED, &gNanoWaitTimeouts, "nano timer waits that ran out");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_nano_leaked, CTLFLAG_RD | CTLFLAG_LOCKED, &gNanoLeaked, "nano timers left when their GPU stopped");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_nano_retired, CTLFLAG_RD | CTLFLAG_LOCKED, &gNanoRetired, "nano timer slots taken out of use for good");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_nano_bad_handle, CTLFLAG_RD | CTLFLAG_LOCKED, &gNanoBadHandle, "nano timer calls with a handle not in use");

static inline void cnt(int64_t *c, int64_t d = 1) { __atomic_add_fetch(c, d, __ATOMIC_RELAXED); }

void nv_xnu_timers_sysctls(void)
{
    static int up;
    if (__atomic_exchange_n(&up, 1, __ATOMIC_SEQ_CST)) return;
    sysctl_register_oid(&sysctl__debug_nvrm_rc_armed); sysctl_register_oid(&sysctl__debug_nvrm_rc_ticks);
    sysctl_register_oid(&sysctl__debug_nvrm_rc_runs); sysctl_register_oid(&sysctl__debug_nvrm_rc_refused);
    sysctl_register_oid(&sysctl__debug_nvrm_rc_stale); sysctl_register_oid(&sysctl__debug_nvrm_rc_wait_timeouts);
    sysctl_register_oid(&sysctl__debug_nvrm_nano_slots); sysctl_register_oid(&sysctl__debug_nvrm_nano_live);
    sysctl_register_oid(&sysctl__debug_nvrm_nano_fires); sysctl_register_oid(&sysctl__debug_nvrm_nano_stale);
    sysctl_register_oid(&sysctl__debug_nvrm_nano_waits); sysctl_register_oid(&sysctl__debug_nvrm_nano_wait_timeouts);
    sysctl_register_oid(&sysctl__debug_nvrm_nano_leaked); sysctl_register_oid(&sysctl__debug_nvrm_nano_bad_handle);
    sysctl_register_oid(&sysctl__debug_nvrm_nano_retired);
}

// 1 when the boot-arg `name` is present with the value 0 (the opt-out); any other value, or none, is the default.
static int nv_xnu_opted_out(const char *name)
{
    uint32_t v = 1;
    return (PE_parse_boot_argn(name, &v, sizeof v) && v == 0) ? 1 : 0;
}

static uint64_t nv_xnu_deadline_ms(uint32_t ms)
{
    uint64_t abs = 0;
    nanoseconds_to_absolutetime((uint64_t)ms * 1000000ULL, &abs);
    return mach_absolute_time() + abs;
}

// Poll until no callback is inside `g`, at most until `deadline` (mach_absolute_time) when the caller may sleep, or
// spinMs when it may not. Whether it may sleep is asked of the kernel itself (nv_quiesce_may_sleep, raw
// preemption_enabled()), never of nvx_can_block(): that one answers yes in every context when the non-blocking
// branches are switched off, and a sleep with preemption off panics. Returns whether it became idle.
static bool nv_xnu_wait_idle_until(const NvXnuGen *g, uint64_t deadline, uint32_t spinMs)
{
    if (nv_gen_idle(g)) return true;
    if (nv_quiesce_may_sleep()) {
        while (!nv_gen_idle(g)) {
            if (mach_absolute_time() >= deadline) return false;
            IOSleep(1);
        }
        return true;
    }
    for (uint32_t us = 0; us < spinMs * 1000u; us += 10) {
        IODelay(10);
        if (nv_gen_idle(g)) return true;
    }
    return false;
}
static bool nv_xnu_wait_idle(const NvXnuGen *g, uint32_t sleepMs, uint32_t spinMs)
{
    return nv_xnu_wait_idle_until(g, nv_xnu_deadline_ms(sleepMs), spinMs);
}

// ---- nano timers ------------------------------------------------------------------------------------------------------
#define NV_NANO_MAGIC   0x4e564e54u   // 'NVNT'
#define NV_NANO_SLAB    32
#define NV_NANO_CAP     4096          // slots in all; beyond it create answers NULL, as a failed allocation did before

struct nv_nano_timer {                   // the RM's opaque handle. Type-stable: its memory is never freed
    uint32_t              magic;
    NvXnuGen              gate;          // generation, and how many callbacks are inside
    volatile uint32_t     live;          // 1 from create to destroy
    volatile thread_t     firing;        // the thread inside the callback (a cancel from there must not wait for itself)
    nv_state_t *volatile  nv;            // written by create; a callback reads it only after its generation matched
    void *volatile        event;
    thread_call_t         call;          // made with the slab and kept; param0 is this slot, param1 the generation
    struct nv_nano_timer *next;          // free list, FIFO: the longest time before a slot is used again
};
struct nv_nano_slab {
    struct nv_nano_slab *next;           // chain of all slabs, newest first; published under the pool lock, never freed
    nv_nano_timer_t      t[NV_NANO_SLAB];
};

static IOSimpleLock *volatile gNanoLock;   // made once, on first use
static nv_nano_timer_t *gNanoFreeHead, *gNanoFreeTail;
static struct nv_nano_slab *volatile gNanoSlabs;
static int gNanoNoWait = -1;              // nvrmnanowait=0

static void nv_xnu_nano_fire(thread_call_param_t p0, thread_call_param_t p1);

static IOSimpleLock *nv_xnu_nano_lock(void)
{
    IOSimpleLock *l = __atomic_load_n(&gNanoLock, __ATOMIC_ACQUIRE);
    if (l || !nv_quiesce_may_sleep()) return l;
    IOSimpleLock *mine = IOSimpleLockAlloc();
    if (!mine) return NULL;
    IOSimpleLock *expected = NULL;
    if (!__atomic_compare_exchange_n(&gNanoLock, &expected, mine, false, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) {
        IOSimpleLockFree(mine);
        return expected;
    }
    return mine;
}

// Appends a slot to the free list. Called with the pool lock held.
static void nv_xnu_nano_push_locked(nv_nano_timer_t *t)
{
    t->next = NULL;
    if (gNanoFreeTail) gNanoFreeTail->next = t; else gNanoFreeHead = t;
    gNanoFreeTail = t;
}

// One more slab of slots. Thread context only: it allocates. Everything is made outside the lock and spliced in under
// it; when the cap was reached meanwhile the new slab is undone.
static void nv_xnu_nano_grow(IOSimpleLock *lock)
{
    if (!nv_quiesce_may_sleep() || __atomic_load_n(&gNanoSlots, __ATOMIC_RELAXED) + NV_NANO_SLAB > NV_NANO_CAP) return;
    struct nv_nano_slab *s = (struct nv_nano_slab *)IOMallocZero(sizeof *s);
    if (!s) return;
    unsigned made = 0;
    for (; made < NV_NANO_SLAB; made++) {
        nv_nano_timer_t *t = &s->t[made];
        t->magic = NV_NANO_MAGIC;
        t->call = thread_call_allocate_with_options(nv_xnu_nano_fire, t, THREAD_CALL_PRIORITY_HIGH, THREAD_CALL_OPTIONS_ONCE);
        if (!t->call) break;
    }
    bool kept = false;
    if (made == NV_NANO_SLAB) {
        IOInterruptState st = IOSimpleLockLockDisableInterrupt(lock);
        if (gNanoSlots + NV_NANO_SLAB <= NV_NANO_CAP) {
            for (unsigned i = 0; i < NV_NANO_SLAB; i++) nv_xnu_nano_push_locked(&s->t[i]);
            s->next = gNanoSlabs;
            __atomic_store_n(&gNanoSlabs, s, __ATOMIC_RELEASE);
            gNanoSlots += NV_NANO_SLAB;
            kept = true;
        }
        IOSimpleLockUnlockEnableInterrupt(lock, st);
    }
    if (!kept) {
        for (unsigned i = 0; i < made; i++) thread_call_free(s->t[i].call);   // never entered: free at once
        IOFree(s, sizeof *s);
    }
}

static nv_nano_timer_t *nv_xnu_nano_pop(void)
{
    IOSimpleLock *lock = nv_xnu_nano_lock();
    if (!lock) return NULL;
    for (int attempt = 0; attempt < 2; attempt++) {
        IOInterruptState st = IOSimpleLockLockDisableInterrupt(lock);
        nv_nano_timer_t *t = gNanoFreeHead;
        if (t) { gNanoFreeHead = t->next; if (!gNanoFreeHead) gNanoFreeTail = NULL; t->next = NULL; }
        IOSimpleLockUnlockEnableInterrupt(lock, st);
        if (t) return t;
        if (attempt == 0) nv_xnu_nano_grow(lock);
    }
    return NULL;
}

static void nv_xnu_nano_free(nv_nano_timer_t *t)
{
    IOSimpleLock *lock = __atomic_load_n(&gNanoLock, __ATOMIC_ACQUIRE);   // a slot exists, so the lock does
    IOInterruptState st = IOSimpleLockLockDisableInterrupt(lock);
    nv_xnu_nano_push_locked(t);
    IOSimpleLockUnlockEnableInterrupt(lock, st);
}

static inline bool nv_xnu_nano_valid(const nv_nano_timer_t *t) { return t && t->magic == NV_NANO_MAGIC; }

static void nv_xnu_nano_fire(thread_call_param_t p0, thread_call_param_t p1)
{
    nv_nano_timer_t *t = (nv_nano_timer_t *)p0;
    const unsigned long long token = (unsigned long long)(uintptr_t)p1;
    if (!nv_gen_enter(&t->gate, token)) { cnt(&gNanoStale); return; }
    // Read what the callback needs, then check the generation once more: a slot destroyed and created again after this
    // call was armed has moved its generation on before it wrote anything new, so a match here means these values are
    // the ones this arm was made for.
    const uint32_t live = __atomic_load_n(&t->live, __ATOMIC_SEQ_CST);
    nv_state_t *nv = t->nv;
    void *event = t->event;
    if (live && nv_gen_current(&t->gate) == token) {
        t->firing = current_thread();
        cnt(&gNanoFires);
        rm_run_nano_timer_callback(NULL, nv, event);
        t->firing = NULL;
    } else cnt(&gNanoStale);
    nv_gen_leave(&t->gate);
}

void NV_API_CALL nv_create_nano_timer(nv_state_t *nv, void *pTmrEvent, nv_nano_timer_t **pt)
{
    if (!pt) return;
    nv_nano_timer_t *t = nv_xnu_nano_pop();
    if (!t) { *pt = NULL; return; }
    t->nv = nv;
    t->event = pTmrEvent;
    t->firing = NULL;
    nv_gen_invalidate(&t->gate);                    // whatever was still armed for this slot is now stale
    __atomic_store_n(&t->live, 1, __ATOMIC_SEQ_CST);
    cnt(&gNanoLive);
    *pt = t;
}

// Linux: hrtimer_start with a relative time. Re-arming a pending timer replaces its deadline.
void NV_API_CALL nv_start_nano_timer(nv_state_t *nv, nv_nano_timer_t *t, NvU64 timens)
{
    if (!nv_xnu_nano_valid(t) || !__atomic_load_n(&t->live, __ATOMIC_SEQ_CST)) { cnt(&gNanoBadHandle); return; }
    uint64_t abs = 0, deadline = 0;
    nanoseconds_to_absolutetime(timens, &abs);   // the whole 64-bit range: a microsecond count in 32 bits ran out at 71 minutes
    clock_absolutetime_interval_to_deadline(abs, &deadline);
    thread_call_enter1_delayed(t->call, (thread_call_param_t)(uintptr_t)nv_gen_current(&t->gate), deadline);
}

// Stops the timer and waits for a callback that is running (not when asked from inside that callback, or with
// nvrmnanowait=0). Returns whether no callback is inside any more.
static bool nv_xnu_nano_quiesce(nv_nano_timer_t *t, uint64_t deadline)
{
    if (gNanoNoWait < 0) gNanoNoWait = nv_xnu_opted_out("nvrmnanowait");
    nv_gen_invalidate(&t->gate);
    thread_call_cancel(t->call);
    if (t->firing == current_thread() || gNanoNoWait) return nv_gen_idle(&t->gate);
    if (nv_gen_idle(&t->gate)) return true;
    cnt(&gNanoWaits);
    const bool idle = nv_xnu_wait_idle_until(&t->gate, deadline, 50);
    if (!idle) cnt(&gNanoWaitTimeouts);
    thread_call_cancel(t->call);   // what the callback armed again before it saw the new generation
    return idle;
}

void NV_API_CALL nv_cancel_nano_timer(nv_state_t *nv, nv_nano_timer_t *t)
{
    if (!nv_xnu_nano_valid(t) || !__atomic_load_n(&t->live, __ATOMIC_SEQ_CST)) { cnt(&gNanoBadHandle); return; }
    nv_xnu_nano_quiesce(t, nv_xnu_deadline_ms(2000));
}

// Ends a timer that is live: stops it and puts its slot back, or keeps the slot out of use for good when a callback
// may still be inside (the wait ran out or was not made) or the caller asks for it. A slot that is not put back is
// counted: nothing else tells a pool that got smaller from one that is in use. Returns false when the timer was not
// live (already ended).
static bool nv_xnu_nano_end(nv_nano_timer_t *t, uint64_t deadline, bool quarantine)
{
    uint32_t one = 1;
    if (!__atomic_compare_exchange_n(&t->live, &one, 0, false, __ATOMIC_SEQ_CST, __ATOMIC_SEQ_CST)) return false;
    cnt(&gNanoLive, -1);
    const bool idle = nv_xnu_nano_quiesce(t, deadline);
    if (idle && !quarantine) nv_xnu_nano_free(t);
    else cnt(&gNanoRetired);
    return true;
}

void NV_API_CALL nv_destroy_nano_timer(nv_state_t *nv, nv_nano_timer_t *t)
{
    // A second destroy of the same handle, or one after its GPU stopped, used to free memory twice
    if (!nv_xnu_nano_valid(t) || !nv_xnu_nano_end(t, nv_xnu_deadline_ms(2000), false)) cnt(&gNanoBadHandle);
}

void nv_xnu_nano_retire(nv_state_t *nv)
{
    if (!nv) return;
    const uint64_t deadline = nv_xnu_deadline_ms(2000);   // for all of them together
    unsigned left = 0;
    for (struct nv_nano_slab *s = __atomic_load_n(&gNanoSlabs, __ATOMIC_ACQUIRE); s; s = s->next) {
        for (unsigned i = 0; i < NV_NANO_SLAB; i++) {
            nv_nano_timer_t *t = &s->t[i];
            if (!__atomic_load_n(&t->live, __ATOMIC_SEQ_CST) || t->nv != nv) continue;
            if (nv_xnu_nano_end(t, deadline, true)) left++;   // kept out of use: the RM may still hold the handle
        }
    }
    if (left) {
        cnt(&gNanoLeaked, left);
        KLOG("timers: %u nano timer(s) were still in use when the GPU stopped; stopped, and their slots are not reused", left);
    }
}

// ---- RC timer ---------------------------------------------------------------------------------------------------------
// Linux (kernel-open/nvidia/nv.c): nv_start_rc_timer arms a 1 s kernel timer whose callback is rm_run_rc_callback.
// Differences, on purpose: the tick re-arms whatever rm_run_rc_callback returns (Linux lets the timer die on the first
// refusal, such as a tick that lands during a power transition), and the driver pauses the timer itself around sleep,
// RTD3 and shutdown (nv_xnu_rc_suspend / nv_xnu_rc_resume) instead of leaving it to fire at a GPU without power.
#define NV_XNU_RC_SLOTS 8   // one per GPU
static_assert(NV_XNU_RC_SLOTS == NVRM_DEVTAB_MAX, "one RC timer slot for every card the device table can hold");
struct nv_xnu_rc {
    thread_call_t     call;     // made on the first start in this slot and kept: a late tick always finds it
    NvXnuGen          gate;
    volatile int      paused;
    volatile int      armed;    // counted in debug.nvrm_rc_armed
    volatile thread_t owner;    // the thread inside the tick (a stop from the 1 Hz list must not wait for itself)
};
static nv_xnu_rc gNvRc[NV_XNU_RC_SLOTS];
static void *volatile gNvRcOwner[NV_XNU_RC_SLOTS];   // the nv_state_t of each slot
static int gRcOff = -1;                              // nvrmrctimer=0

static void nv_xnu_rc_tick(thread_call_param_t p0, thread_call_param_t p1);

static nv_xnu_rc *nv_xnu_rc_find(nv_state_t *nv)
{
    const int i = nv_slot_find(gNvRcOwner, NV_XNU_RC_SLOTS, nv);
    return i < 0 ? NULL : &gNvRc[i];
}
static void nv_xnu_rc_arm(nv_xnu_rc *rc, unsigned long long token)
{
    uint64_t deadline; clock_interval_to_deadline(1000, kMillisecondScale, &deadline);
    thread_call_enter1_delayed(rc->call, (thread_call_param_t)(uintptr_t)token, deadline);
}
static void nv_xnu_rc_set_armed(nv_xnu_rc *rc, int on)
{
    if (__atomic_exchange_n(&rc->armed, on, __ATOMIC_SEQ_CST) != on) cnt(&gRcArmed, on ? 1 : -1);
}
static void nv_xnu_rc_tick(thread_call_param_t p0, thread_call_param_t p1)
{
    nv_xnu_rc *rc = (nv_xnu_rc *)p0;
    const unsigned long long token = (unsigned long long)(uintptr_t)p1;
    if (!nv_gen_enter(&rc->gate, token)) { cnt(&gRcStale); return; }
    rc->owner = current_thread();
    nv_state_t *nv = (nv_state_t *)gNvRcOwner[rc - gNvRc];
    if (nv && !rc->paused && nv->rc_timer_enabled) {
        cnt(&gRcTicks);
        // rm_run_rc_callback only tries the GPU lock: a busy RM costs no wait (the RM runs the list when the lock is released).
        if (rm_run_rc_callback(NULL, nv) == NV_OK) cnt(&gRcRuns); else cnt(&gRcRefused);
        if (nv_gen_current(&rc->gate) == token) nv_xnu_rc_arm(rc, token);
    }
    rc->owner = NULL;
    nv_gen_leave(&rc->gate);
}
// No tick runs once this returns (unless the wait ran out, which is counted). Asked from inside a tick it does not
// wait for itself: that tick sees the new generation and does not re-arm.
static void nv_xnu_rc_quiesce(nv_xnu_rc *rc, uint32_t waitMs)
{
    nv_gen_invalidate(&rc->gate);
    thread_call_cancel(rc->call);
    if (rc->owner == current_thread()) return;
    if (!nv_xnu_wait_idle(&rc->gate, waitMs, 50)) {
        cnt(&gRcWaitTimeouts);
        kprintf("NVRM-xnu: RC timer: a tick was still running after %u ms\n", waitMs);
    }
    thread_call_cancel(rc->call);   // a tick armed again just before it saw the new generation
}

NvS32 NV_API_CALL nv_start_rc_timer(nv_state_t *nv)
{
    if (!nv) return -1;
    if (gRcOff < 0) gRcOff = nv_xnu_opted_out("nvrmrctimer");
    if (gRcOff) return 0;   // as the old stubs: rc_timer_enabled stays 0 and nothing ever calls rm_run_rc_callback
    if (nv->rc_timer_enabled) return -1;
    const int i = nv_slot_claim(gNvRcOwner, NV_XNU_RC_SLOTS, nv);
    if (i < 0) { kprintf("NVRM-xnu: RC timer: no free slot, the 1 Hz callbacks stay off for this GPU\n"); return -1; }
    nv_xnu_rc *rc = &gNvRc[i];
    if (!rc->call && nv_quiesce_may_sleep())
        rc->call = thread_call_allocate_with_options(nv_xnu_rc_tick, rc, THREAD_CALL_PRIORITY_KERNEL, THREAD_CALL_OPTIONS_ONCE);
    if (!rc->call) {
        nv_slot_release(gNvRcOwner, NV_XNU_RC_SLOTS, i, nv);
        kprintf("NVRM-xnu: RC timer: no thread call, the 1 Hz callbacks stay off for this GPU\n");
        return -1;
    }
    rc->paused = 0;
    nv->rc_timer_enabled = 1;
    nv_xnu_rc_set_armed(rc, 1);
    nv_xnu_rc_arm(rc, nv_gen_invalidate(&rc->gate));
    kprintf("NVRM-xnu: RC timer armed: the RM's 1 Hz callbacks run (boot-arg nvrmrctimer=0 turns them off)\n");
    return 0;
}

// Called by the RM (from rm_disable_adapter and its own teardown) with its API and GPU locks held.
NvS32 NV_API_CALL nv_stop_rc_timer(nv_state_t *nv)
{
    if (!nv || !nv->rc_timer_enabled) return -1;
    nv->rc_timer_enabled = 0;
    nv_xnu_rc *rc = nv_xnu_rc_find(nv);
    if (!rc || !rc->call) return 0;
    nv_xnu_rc_set_armed(rc, 0);
    nv_xnu_rc_quiesce(rc, 10000);
    return 0;
}

// System sleep, RTD3 and shutdown (NVRM::gpuSuspend, NVRM::shutdownFull): no tick may reach a GPU in standby or
// without power. 2 s: this runs inside the system's power change.
void nv_xnu_rc_suspend(nv_state_t *nv)
{
    nv_xnu_rc *rc = nv ? nv_xnu_rc_find(nv) : NULL;
    if (!rc || !rc->call) return;
    rc->paused = 1;
    nv_xnu_rc_quiesce(rc, 2000);
}
void nv_xnu_rc_resume(nv_state_t *nv)
{
    nv_xnu_rc *rc = nv ? nv_xnu_rc_find(nv) : NULL;
    if (!rc || !rc->call) return;
    rc->paused = 0;
    if (nv->rc_timer_enabled) nv_xnu_rc_arm(rc, nv_gen_invalidate(&rc->gate));
}
// After rm_shutdown_adapter, before the GPU's private state goes: the slot is let go, the thread call stays with it.
void nv_xnu_rc_detach(nv_state_t *nv)
{
    if (!nv) return;
    const int i = nv_slot_find(gNvRcOwner, NV_XNU_RC_SLOTS, nv);
    if (i < 0) return;
    nv_xnu_rc *rc = &gNvRc[i];
    nv->rc_timer_enabled = 0;
    rc->paused = 1;
    nv_xnu_rc_set_armed(rc, 0);
    if (rc->call) nv_xnu_rc_quiesce(rc, 10000);
    nv_slot_release(gNvRcOwner, NV_XNU_RC_SLOTS, i, nv);
}

}
