/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

// nv_xnu_kcore.cpp: the parts of the OS layer that have to know which kind of context they run in, and the work
// queues. What each function promises is in nv_xnu_kcore.h; this file says how.
//
// Lock order (complete for this file):
//   RM locks (API lock, GPU locks, RM spinlocks)
//     -> a work queue's simple lock      interrupts off; only assert_wait / thread_wakeup inside
//     -> nvx_pool_lock                   interrupts off; a leaf
//   nvx_wq_admin (IOLock)                thread context only; slot allocation, worker start, slot release. Never held
//                                        while flushing, waiting for a worker or calling the RM, and never taken with
//                                        a simple lock held.
// No two of the simple locks are ever held together. Nothing that can block, allocate or log runs under any of them.
//
// Opt-outs, each read once by nv_xnu_kcore_init(): nvrmworkq=0 (the work items run as before, each on a thread of its
// own) and nvrmatomic=0 (every OS-layer function answers as before, whatever the context).

#include "nv-xnu.h"
#include "nv_xnu_kcore.h"
#include "nvrm_workq_logic.h"
#include "nvrm_atomic_pool.h"
#include <IOKit/IOLocks.h>
#include <mach/kmod.h>
#include <mach/semaphore.h>
#include <kern/thread.h>
#include <kern/thread_call.h>
#include <kern/sched_prim.h>
#include <kern/clock.h>
#include <kern/locks.h>
#include <sys/sysctl.h>
#include <pexpert/pexpert.h>

extern "C" kmod_info_t kmod_info;   // NVRM.cpp (KMOD_EXPLICIT_DECL)

#define KLOG(fmt, ...) do { kprintf("NVRM-xnu: " fmt "\n", ##__VA_ARGS__); IOLog("NVRM-xnu: " fmt "\n", ##__VA_ARGS__); } while (0)

static inline void nvx_inc(int64_t *c) { __atomic_fetch_add(c, 1, __ATOMIC_RELAXED); }
static inline void nvx_max(int64_t *c, int64_t v)
{
    int64_t cur = __atomic_load_n(c, __ATOMIC_RELAXED);
    while (v > cur && !__atomic_compare_exchange_n(c, &cur, v, true, __ATOMIC_RELAXED, __ATOMIC_RELAXED)) {}
}
static inline uint64_t nvx_now_us(void)
{
    uint64_t ns; absolutetime_to_nanoseconds(mach_absolute_time(), &ns); return ns / 1000;
}
static inline uintptr_t nvx_kext_off(const void *p)
{
    const uintptr_t a = (uintptr_t)p, base = (uintptr_t)kmod_info.address;
    return (a >= base && a - base < (uintptr_t)kmod_info.size) ? a - base : a;
}

// ---- counters (read-only sysctls) -----------------------------------------------------------------------------------
static int     nvx_wq_mode;            // 1 serialized queues, 0 switched off (nvrmworkq=0), -1 could not start
static int64_t nvx_wq_queued, nvx_wq_run, nvx_wq_depth_max, nvx_wq_item_max_us, nvx_wq_enq_atomic, nvx_wq_enq_fail,
               nvx_wq_flushes, nvx_wq_flush_slow, nvx_wq_flush_self, nvx_wq_flush_atomic, nvx_wq_gpu_queues,
               nvx_wq_abandoned;
static int     nvx_atomic_mode;        // 1 the context answers are on, 0 switched off (nvrmatomic=0), -1 self-test failed
static int64_t nvx_atomic_maysleep_n, nvx_atomic_alloc_n, nvx_atomic_alloc_miss, nvx_atomic_pool_peak,
               nvx_atomic_free_deferred, nvx_atomic_lock_try, nvx_atomic_lock_spun, nvx_atomic_lock_fallback;
static int64_t nvx_site_count[NVX_SITE_MAX];

SYSCTL_INT(_debug, OID_AUTO, nvrm_wq_mode, CTLFLAG_RD | CTLFLAG_LOCKED, &nvx_wq_mode, 0, "RM work queues: 1 serialized, 0 off (nvrmworkq=0), -1 could not start");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_wq_queued, CTLFLAG_RD | CTLFLAG_LOCKED, &nvx_wq_queued, "RM work items queued");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_wq_run, CTLFLAG_RD | CTLFLAG_LOCKED, &nvx_wq_run, "RM work items run");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_wq_depth_max, CTLFLAG_RD | CTLFLAG_LOCKED, &nvx_wq_depth_max, "most RM work items pending or running on one queue");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_wq_item_max_us, CTLFLAG_RD | CTLFLAG_LOCKED, &nvx_wq_item_max_us, "longest RM work item, microseconds");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_wq_enq_atomic, CTLFLAG_RD | CTLFLAG_LOCKED, &nvx_wq_enq_atomic, "RM work items queued where blocking is not allowed");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_wq_enq_fail, CTLFLAG_RD | CTLFLAG_LOCKED, &nvx_wq_enq_fail, "RM work items refused (queue stopping, or no node where blocking is not allowed)");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_wq_flushes, CTLFLAG_RD | CTLFLAG_LOCKED, &nvx_wq_flushes, "work queue flushes");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_wq_flush_slow, CTLFLAG_RD | CTLFLAG_LOCKED, &nvx_wq_flush_slow, "5 s periods a flush waited");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_wq_flush_self, CTLFLAG_RD | CTLFLAG_LOCKED, &nvx_wq_flush_self, "flushes asked for by a queue's own worker (answered at once)");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_wq_flush_atomic, CTLFLAG_RD | CTLFLAG_LOCKED, &nvx_wq_flush_atomic, "flushes refused because the caller could not block");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_wq_gpu_queues, CTLFLAG_RD | CTLFLAG_LOCKED, &nvx_wq_gpu_queues, "per-GPU work queues running");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_wq_abandoned, CTLFLAG_RD | CTLFLAG_LOCKED, &nvx_wq_abandoned, "per-GPU work queues given up on after a bounded wait ran out");
SYSCTL_INT(_debug, OID_AUTO, nvrm_atomic_mode, CTLFLAG_RD | CTLFLAG_LOCKED, &nvx_atomic_mode, 0, "OS-layer answers by context: 1 on, 0 off (nvrmatomic=0), -1 self-test failed");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_atomic_maysleep_false, CTLFLAG_RD | CTLFLAG_LOCKED, &nvx_atomic_maysleep_n, "os_semaphore_may_sleep answered no");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_atomic_alloc, CTLFLAG_RD | CTLFLAG_LOCKED, &nvx_atomic_alloc_n, "RM allocations served by the pool");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_atomic_alloc_miss, CTLFLAG_RD | CTLFLAG_LOCKED, &nvx_atomic_alloc_miss, "RM allocations where blocking is not allowed that the pool could not serve");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_atomic_pool_peak, CTLFLAG_RD | CTLFLAG_LOCKED, &nvx_atomic_pool_peak, "most pool bytes in use");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_atomic_free_deferred, CTLFLAG_RD | CTLFLAG_LOCKED, &nvx_atomic_free_deferred, "heap frees handed to a thread call");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_atomic_delay, CTLFLAG_RD | CTLFLAG_LOCKED, &nvx_site_count[NVX_SITE_DELAY], "os_delay where blocking is not allowed");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_atomic_schedule, CTLFLAG_RD | CTLFLAG_LOCKED, &nvx_site_count[NVX_SITE_SCHEDULE], "os_schedule where blocking is not allowed");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_atomic_lock_try, CTLFLAG_RD | CTLFLAG_LOCKED, &nvx_atomic_lock_try, "lock acquires where blocking is not allowed");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_atomic_lock_spun, CTLFLAG_RD | CTLFLAG_LOCKED, &nvx_atomic_lock_spun, "of those, acquires that had to retry");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_atomic_lock_fallback, CTLFLAG_RD | CTLFLAG_LOCKED, &nvx_atomic_lock_fallback, "of those, acquires that took the blocking call after 20 ms");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_atomic_lockalloc, CTLFLAG_RD | CTLFLAG_LOCKED, &nvx_site_count[NVX_SITE_LOCKALLOC], "lock allocations where blocking is not allowed");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_atomic_blocking_entry, CTLFLAG_RD | CTLFLAG_LOCKED, &nvx_site_count[NVX_SITE_BLOCKING_ENTRY], "blocking OS-layer calls entered where blocking is not allowed");

extern "C" void nv_xnu_kcore_sysctls_register(void)
{
    static bool up;
    if (up) return;
    up = true;
    sysctl_register_oid(&sysctl__debug_nvrm_wq_mode); sysctl_register_oid(&sysctl__debug_nvrm_wq_queued);
    sysctl_register_oid(&sysctl__debug_nvrm_wq_run); sysctl_register_oid(&sysctl__debug_nvrm_wq_depth_max);
    sysctl_register_oid(&sysctl__debug_nvrm_wq_item_max_us); sysctl_register_oid(&sysctl__debug_nvrm_wq_enq_atomic);
    sysctl_register_oid(&sysctl__debug_nvrm_wq_enq_fail); sysctl_register_oid(&sysctl__debug_nvrm_wq_flushes);
    sysctl_register_oid(&sysctl__debug_nvrm_wq_flush_slow); sysctl_register_oid(&sysctl__debug_nvrm_wq_flush_self);
    sysctl_register_oid(&sysctl__debug_nvrm_wq_flush_atomic); sysctl_register_oid(&sysctl__debug_nvrm_wq_gpu_queues);
    sysctl_register_oid(&sysctl__debug_nvrm_wq_abandoned);
    sysctl_register_oid(&sysctl__debug_nvrm_atomic_mode); sysctl_register_oid(&sysctl__debug_nvrm_atomic_maysleep_false);
    sysctl_register_oid(&sysctl__debug_nvrm_atomic_alloc); sysctl_register_oid(&sysctl__debug_nvrm_atomic_alloc_miss);
    sysctl_register_oid(&sysctl__debug_nvrm_atomic_pool_peak); sysctl_register_oid(&sysctl__debug_nvrm_atomic_free_deferred);
    sysctl_register_oid(&sysctl__debug_nvrm_atomic_delay); sysctl_register_oid(&sysctl__debug_nvrm_atomic_schedule);
    sysctl_register_oid(&sysctl__debug_nvrm_atomic_lock_try); sysctl_register_oid(&sysctl__debug_nvrm_atomic_lock_spun);
    sysctl_register_oid(&sysctl__debug_nvrm_atomic_lock_fallback); sysctl_register_oid(&sysctl__debug_nvrm_atomic_lockalloc);
    sysctl_register_oid(&sysctl__debug_nvrm_atomic_blocking_entry);
}

// ---- the context question ---------------------------------------------------------------------------------------------
// Written once by nv_xnu_kcore_init() before the RM is entered, read-only afterwards.
static bool nvx_atomic_on;

extern "C" bool nvx_can_block(void) { return !nvx_atomic_on || preemption_enabled(); }

extern "C" void nvx_note(unsigned site, void *ra)
{
    if (site >= NVX_SITE_MAX) return;
    static const char *const names[NVX_SITE_MAX] = { "alloc", "free", "delay", "schedule", "mutex", "rwlock", "semaphore",
                                                     "lock allocation", "blocking call", "flush" };
    if (__atomic_fetch_add(&nvx_site_count[site], 1, __ATOMIC_RELAXED) == 0)
        kprintf("NVRM-xnu: atomic context at %s, caller NVRM+0x%lx\n", names[site], (unsigned long)nvx_kext_off(ra));
}

// ---- the pool and the deferred free -------------------------------------------------------------------------------------
// Kext data is wired: the pool exists from load and nothing can fail to allocate it.
static unsigned char nvx_pool_mem[256 * 1024] __attribute__((aligned(4096)));
static struct nvap_pool nvx_pool;
static IOSimpleLock *nvx_pool_lock;        // NULL: the pool is not used (allocation never tried, nvap_owns false)
static thread_call_t nvx_dfree_call;       // NULL: heap frees take the older call in every context
static void *volatile nvx_dfree_head;
static volatile int nvx_dfree_pending;

static void nvx_dfree_drain(thread_call_param_t, thread_call_param_t)
{
    __atomic_store_n(&nvx_dfree_pending, 0, __ATOMIC_RELEASE);
    void *p = __atomic_exchange_n(&nvx_dfree_head, (void *)NULL, __ATOMIC_ACQUIRE);
    while (p) {
        void *next = *(void **)p;
        kern_os_free(p);
        p = next;
    }
}

extern "C" void *nvx_atomic_alloc(NvU64 size)
{
    if (!nvx_pool_lock || size == 0 || size > 65536) { nvx_inc(&nvx_atomic_alloc_miss); return NULL; }
    IOInterruptState s = IOSimpleLockLockDisableInterrupt(nvx_pool_lock);
    void *p = nvap_alloc(&nvx_pool, (unsigned)size);
    const unsigned peak = nvx_pool.peak;
    IOSimpleLockUnlockEnableInterrupt(nvx_pool_lock, s);
    if (!p) { nvx_inc(&nvx_atomic_alloc_miss); return NULL; }
    nvx_inc(&nvx_atomic_alloc_n);
    nvx_max(&nvx_atomic_pool_peak, (int64_t)peak);
    memset(p, 0, (size_t)size);
    return p;
}

extern "C" int nvx_atomic_free(void *p)
{
    if (!p) return 1;
    if (nvx_pool_lock && nvap_owns(&nvx_pool, p)) {
        IOInterruptState s = IOSimpleLockLockDisableInterrupt(nvx_pool_lock);
        const int r = nvap_free(&nvx_pool, p);
        IOSimpleLockUnlockEnableInterrupt(nvx_pool_lock, s);
        if (r != 0) kprintf("NVRM-xnu: a free of %p inside the atomic pool is not a live block; ignored\n", p);
        return 1;
    }
    if (nvx_can_block() || !nvx_dfree_call) return 0;
    nvx_note(NVX_SITE_FREE, __builtin_return_address(0));
    nvx_inc(&nvx_atomic_free_deferred);
    // Every heap block is at least 16 bytes, so its first 8 hold the link.
    void *head = __atomic_load_n(&nvx_dfree_head, __ATOMIC_RELAXED);
    do { *(void **)p = head; }
    while (!__atomic_compare_exchange_n(&nvx_dfree_head, &head, p, true, __ATOMIC_RELEASE, __ATOMIC_RELAXED));
    if (!__atomic_exchange_n(&nvx_dfree_pending, 1, __ATOMIC_ACQ_REL)) thread_call_enter(nvx_dfree_call);
    return 1;
}

static bool nvx_try_once(int kind, void *lock)
{
    switch (kind) {
    case NVX_LOCK_MUTEX:    return IOLockTryLock((IOLock *)lock);
    case NVX_LOCK_RW_READ:  return lck_rw_try_lock(IORWLockGetMachLock((IORWLock *)lock), LCK_RW_TYPE_SHARED);
    case NVX_LOCK_RW_WRITE: return lck_rw_try_lock(IORWLockGetMachLock((IORWLock *)lock), LCK_RW_TYPE_EXCLUSIVE);
    case NVX_LOCK_SEMA:     return semaphore_wait_noblock(*(semaphore_t *)lock) == KERN_SUCCESS;
    default:                return false;
    }
}

extern "C" int nvx_atomic_lock(int kind, void *lock)
{
    static const unsigned site[] = { NVX_SITE_MUTEX, NVX_SITE_RWLOCK, NVX_SITE_RWLOCK, NVX_SITE_SEMA };
    nvx_inc(&nvx_atomic_lock_try);
    if (kind >= 0 && kind <= NVX_LOCK_SEMA) nvx_note(site[kind], __builtin_return_address(0));
    if (nvx_try_once(kind, lock)) return 1;
    nvx_inc(&nvx_atomic_lock_spun);
    for (unsigned i = 0; i < 1000; i++) {          // 1000 x 20 us = 20 ms
        IODelay(20);
        if (nvx_try_once(kind, lock)) return 1;
    }
    nvx_inc(&nvx_atomic_lock_fallback);
    {
        static bool said;
        if (!said) {
            said = true;
            kprintf("NVRM-xnu: a lock still held after 20 ms where blocking is not allowed; caller NVRM+0x%lx takes the "
                    "blocking call\n", (unsigned long)nvx_kext_off(__builtin_return_address(0)));
        }
    }
    return 0;
}

extern "C" void nvx_atomic_maysleep_false(void) { nvx_inc(&nvx_atomic_maysleep_n); }

// ---- the work queues --------------------------------------------------------------------------------------------------
struct os_work_queue {
    struct nvwq_core core;
    IOSimpleLock *lock;        // allocated on first use, never freed
    thread_t      thread;      // +1 reference while the worker can run
    void         *running;     // the item being run, for the slow-flush line
    uint64_t      run_start;   // when it started, microseconds
    bool          idle;        // the worker is waiting on &core.head
    bool          alive;       // the worker has not left
    bool          in_use;      // the slot belongs to a GPU
    bool          abandoned;   // a bounded wait on it ran out: never waited for or reused again
    unsigned      index;
};
#define NV_XNU_WQ_GPUS 4
// [0] is the global queue (queue NULL), [1..] one per GPU. Slots are never freed, so a stale pointer in a late call
// always lands on a queue that refuses the item.
static os_work_queue nvx_wq[1 + NV_XNU_WQ_GPUS];
static IOLock *nvx_wq_admin;
static bool nvx_wq_on;                // written once by nv_xnu_kcore_init()
static const char *const nvx_wq_names[1 + NV_XNU_WQ_GPUS] = { "nvrm-wq0", "nvrm-wq1", "nvrm-wq2", "nvrm-wq3", "nvrm-wq4" };

static inline IOInterruptState wq_lock(os_work_queue *q) { return IOSimpleLockLockDisableInterrupt(q->lock); }
static inline void wq_unlock(os_work_queue *q, IOInterruptState s) { IOSimpleLockUnlockEnableInterrupt(q->lock, s); }

static void wq_worker(void *arg, wait_result_t)
{
    os_work_queue *q = (os_work_queue *)arg;
    IOInterruptState s = wq_lock(q);
    for (;;) {
        struct nvwq_node *n = nvwq_dequeue(&q->core);
        if (!n) {
            if (q->core.state != NVWQ_RUN) break;                   // stopping and drained
            q->idle = true;
            assert_wait(&q->core.head, THREAD_UNINT);                // under the lock every waker holds: no lost wake
            wq_unlock(q, s);
            thread_block(THREAD_CONTINUE_NULL);
            s = wq_lock(q);
            q->idle = false;
            continue;
        }
        q->running = n->item;
        q->run_start = nvx_now_us();
        wq_unlock(q, s);
        rm_execute_work_item(NULL, n->item);
        const uint64_t took = nvx_now_us() - q->run_start;
        s = wq_lock(q);
        q->running = NULL;
        struct nvwq_node *h = nvwq_complete(&q->core, n);
        if (q->core.flush_waiters) thread_wakeup(&q->core.done_seq);
        wq_unlock(q, s);
        nvx_inc(&nvx_wq_run);
        nvx_max(&nvx_wq_item_max_us, (int64_t)took);
        if (h) kern_os_free(h);
        s = wq_lock(q);
    }
    q->core.state = NVWQ_OFF;
    q->alive = false;
    // A queue given up on is not waited for by anyone: the worker drops the reference its starter held.
    thread_t drop = NULL;
    if (q->abandoned) { drop = q->thread; q->thread = NULL; }
    thread_wakeup(&q->alive);
    wq_unlock(q, s);
    if (drop) thread_deallocate(drop);
    thread_terminate(current_thread());
}

// 2.9's work queue, kept as it was: each item runs on its own thread, and a flush waits until none is running. Used
// with nvrmworkq=0 and when the global worker could not be started.
static IOLock *nv_xnu_work_lock;
static int nv_xnu_work_running;
static bool nv_xnu_work_flushing;
static IOLock *nv_xnu_work_lock_get(void)
{
    if (!nv_xnu_work_lock) {
        IOLock *l = IOLockAlloc();
        if (l && !OSCompareAndSwapPtr(NULL, l, (void * volatile *)&nv_xnu_work_lock)) IOLockFree(l);
    }
    return nv_xnu_work_lock;
}
static void nv_xnu_work_thread(void *item, wait_result_t)
{
    kprintf("NVRM-xnu: work item %p running\n", item);
    rm_execute_work_item(NULL, item);
    IOLockLock(nv_xnu_work_lock);
    if (--nv_xnu_work_running == 0) IOLockWakeup(nv_xnu_work_lock, &nv_xnu_work_running, false);
    IOLockUnlock(nv_xnu_work_lock);
    thread_terminate(current_thread());
}
static NV_STATUS nv_xnu_work_legacy_queue(void *item)
{
    thread_t t;
    if (!nv_xnu_work_lock_get()) return NV_ERR_NO_MEMORY;
    IOLockLock(nv_xnu_work_lock); nv_xnu_work_running++; IOLockUnlock(nv_xnu_work_lock);
    if (kernel_thread_start(nv_xnu_work_thread, item, &t) != KERN_SUCCESS) {
        IOLockLock(nv_xnu_work_lock); nv_xnu_work_running--; IOLockUnlock(nv_xnu_work_lock);
        return NV_ERR_NO_MEMORY;
    }
    thread_deallocate(t); return NV_OK;
}
// cap_ms 0 is 2.9's flush exactly. A cap (the kext's own teardown and sleep calls) ends the wait when it runs out.
static NV_STATUS nv_xnu_work_legacy_flush(unsigned cap_ms)
{
    NV_STATUS st = NV_OK;
    if (!nv_xnu_work_lock) return NV_OK;
    IOLockLock(nv_xnu_work_lock);
    nv_xnu_work_flushing = true;
    if (!cap_ms) {
        while (nv_xnu_work_running > 0) IOLockSleep(nv_xnu_work_lock, &nv_xnu_work_running, THREAD_UNINT);
    } else {
        uint64_t deadline;
        clock_interval_to_deadline(cap_ms, kMillisecondScale, &deadline);
        while (nv_xnu_work_running > 0)
            if (IOLockSleepDeadline(nv_xnu_work_lock, &nv_xnu_work_running, deadline, THREAD_UNINT) == THREAD_TIMED_OUT) {
                if (nv_xnu_work_running > 0) st = NV_ERR_TIMEOUT;
                break;
            }
    }
    nv_xnu_work_flushing = false;
    IOLockUnlock(nv_xnu_work_lock);
    return st;
}

static inline os_work_queue *wq_of(struct os_work_queue *queue) { return queue ? queue : &nvx_wq[0]; }

extern "C" NV_STATUS NV_API_CALL os_queue_work_item(struct os_work_queue *queue, void *item)
{
    if (!nvx_wq_on) return nv_xnu_work_legacy_queue(item);
    os_work_queue *q = wq_of(queue);
    const bool atomic = !nvx_can_block();
    IOInterruptState s = wq_lock(q);
    int r = nvwq_enqueue(&q->core, item, NULL, NULL);
    if (r == 0) {
        const unsigned depth = q->core.max_depth;
        if (q->idle) thread_wakeup(&q->core.head);
        wq_unlock(q, s);
        nvx_inc(&nvx_wq_queued);
        if (atomic) nvx_inc(&nvx_wq_enq_atomic);
        nvx_max(&nvx_wq_depth_max, (int64_t)depth);
        return NV_OK;
    }
    wq_unlock(q, s);
    if (r == 2) { nvx_inc(&nvx_wq_enq_fail); return NV_ERR_NOT_READY; }
    // Every static node is in use (256 items pending). Where blocking is allowed one more node comes from the heap;
    // where it is not, the item is refused and the RM frees it (os.c, osQueueWorkItem), as Linux would.
    if (atomic) { nvx_inc(&nvx_wq_enq_fail); return NV_ERR_NO_MEMORY; }
    struct nvwq_node *spare = (struct nvwq_node *)kern_os_malloc(sizeof *spare);
    if (!spare) { nvx_inc(&nvx_wq_enq_fail); return NV_ERR_NO_MEMORY; }
    int used = 0;
    s = wq_lock(q);
    r = nvwq_enqueue(&q->core, item, spare, &used);
    const unsigned depth = q->core.max_depth;
    if (r == 0 && q->idle) thread_wakeup(&q->core.head);
    wq_unlock(q, s);
    if (!used) kern_os_free(spare);
    if (r != 0) { nvx_inc(&nvx_wq_enq_fail); return NV_ERR_NOT_READY; }
    nvx_inc(&nvx_wq_queued);
    nvx_max(&nvx_wq_depth_max, (int64_t)depth);
    return NV_OK;
}

extern "C" NV_STATUS nv_xnu_wq_flush(struct os_work_queue *queue, bool unload, unsigned cap_ms)
{
    if (!nvx_wq_on) return nv_xnu_work_legacy_flush(cap_ms);
    os_work_queue *q = wq_of(queue);
    if (!nvx_can_block()) {
        nvx_inc(&nvx_wq_flush_atomic);
        nvx_note(NVX_SITE_FLUSH, __builtin_return_address(0));
        return NV_ERR_ILLEGAL_ACTION;
    }
    if (q->thread && current_thread() == q->thread) {
        if (__atomic_fetch_add(&nvx_wq_flush_self, 1, __ATOMIC_RELAXED) == 0)
            KLOG("a work item flushed its own queue (%s); answered at once", nvx_wq_names[q->index]);
        return NV_OK;
    }
    nvx_inc(&nvx_wq_flushes);
    const uint64_t start = nvx_now_us();
    uint64_t next_log_ms = 5000;
    NV_STATUS st = NV_OK;
    IOInterruptState s = wq_lock(q);
    if (q->abandoned) { wq_unlock(q, s); return NV_ERR_TIMEOUT; }
    if (unload) q->core.unload_flushers++;
    // Two snapshots: the second covers what an item queued while the first was being waited for.
    for (int pass = 0; pass < 2 && st == NV_OK; pass++) {
        const unsigned long long t = nvwq_flush_target(&q->core);
        while (!nvwq_flushed(&q->core, t) && q->alive) {
            const uint64_t waited_ms = (nvx_now_us() - start) / 1000;
            if (cap_ms && waited_ms >= cap_ms) { st = NV_ERR_TIMEOUT; break; }
            uint64_t slice = next_log_ms > waited_ms ? next_log_ms - waited_ms : 1;
            if (cap_ms && slice > cap_ms - waited_ms) slice = cap_ms - waited_ms;
            q->core.flush_waiters++;
            assert_wait_timeout(&q->core.done_seq, THREAD_UNINT, (uint32_t)slice, NSEC_PER_MSEC);
            wq_unlock(q, s);
            (void)thread_block(THREAD_CONTINUE_NULL);
            s = wq_lock(q);
            q->core.flush_waiters--;
            if (!nvwq_flushed(&q->core, t) && (nvx_now_us() - start) / 1000 >= next_log_ms) {
                void *item = q->running;
                const uint64_t age_ms = item ? (nvx_now_us() - q->run_start) / 1000 : 0;
                const unsigned depth = q->core.depth;
                next_log_ms += 5000;
                wq_unlock(q, s);
                nvx_inc(&nvx_wq_flush_slow);
                KLOG("flush of %s still waiting after %llu s: item %p running for %llu ms, %u pending or running",
                     nvx_wq_names[q->index], (unsigned long long)((nvx_now_us() - start) / 1000000), item,
                     (unsigned long long)age_ms, depth);
                s = wq_lock(q);
            }
        }
    }
    if (unload) q->core.unload_flushers--;
    if (st == NV_ERR_TIMEOUT && q != &nvx_wq[0] && !q->abandoned) {
        // From now on nobody waits for this queue: the RM's own unbounded flushes during the teardown that follows
        // would otherwise hang on the queue its caller has just given up on.
        q->abandoned = true;
        nvx_inc(&nvx_wq_abandoned);
    }
    wq_unlock(q, s);
    if (st == NV_ERR_TIMEOUT)
        KLOG("flush of %s gave up after %u ms with work still queued or running", nvx_wq_names[q->index], cap_ms);
    return st;
}

extern "C" NV_STATUS NV_API_CALL os_flush_work_queue(struct os_work_queue *queue, NvBool is_unload)
{
    return nv_xnu_wq_flush(queue, is_unload ? true : false, 0);
}

extern "C" NvBool NV_API_CALL os_is_queue_flush_ongoing(struct os_work_queue *queue)
{
    if (!nvx_wq_on) return nv_xnu_work_flushing ? NV_TRUE : NV_FALSE;
    return __atomic_load_n(&wq_of(queue)->core.unload_flushers, __ATOMIC_RELAXED) ? NV_TRUE : NV_FALSE;
}

// Thread context, nvx_wq_admin held. Sets the slot running with a new worker; false when the worker cannot start.
static bool wq_start_slot(os_work_queue *q, unsigned index)
{
    if (!q->lock) q->lock = IOSimpleLockAlloc();
    if (!q->lock) return false;
    nvwq_core_init(&q->core);
    q->index = index;
    q->running = NULL;
    q->idle = false;
    q->abandoned = false;
    q->core.state = NVWQ_RUN;
    q->alive = true;
    thread_t th = NULL;
    if (kernel_thread_start(wq_worker, q, &th) != KERN_SUCCESS) {
        q->core.state = NVWQ_OFF;
        q->alive = false;
        return false;
    }
    q->thread = th;
    thread_set_thread_name(th, nvx_wq_names[index]);
    return true;
}

extern "C" struct os_work_queue *nv_xnu_wq_gpu_start(void)
{
    if (!nvx_wq_on || !nvx_wq_admin) return NULL;
    IOLockLock(nvx_wq_admin);
    os_work_queue *got = NULL;
    for (unsigned i = 1; i <= NV_XNU_WQ_GPUS && !got; i++) {
        os_work_queue *q = &nvx_wq[i];
        if (q->in_use || q->abandoned) continue;
        if (!wq_start_slot(q, i)) break;
        q->in_use = true;
        got = q;
    }
    IOLockUnlock(nvx_wq_admin);
    if (got) { nvx_inc(&nvx_wq_gpu_queues); KLOG("work queue %s started for a GPU", nvx_wq_names[got->index]); }
    else KLOG("no work queue of its own for this GPU (no free slot or no thread): it uses the global queue");
    return got;
}

extern "C" void nv_xnu_wq_gpu_down(struct os_work_queue *queue, unsigned cap_ms)
{
    if (!queue) return;
    os_work_queue *q = queue;
    if (q < &nvx_wq[1] || q > &nvx_wq[NV_XNU_WQ_GPUS]) { KLOG("stop of a work queue that is not a GPU's: ignored"); return; }
    if (q->thread && current_thread() == q->thread) { KLOG("a work item asked to stop its own queue: refused"); return; }
    if (!cap_ms) cap_ms = 30000;
    const NV_STATUS fst = nv_xnu_wq_flush(q, false, cap_ms);
    const uint64_t start = nvx_now_us();
    bool gave_up = (fst != NV_OK);
    thread_t drop = NULL;
    IOInterruptState s = wq_lock(q);
    if (q->core.state == NVWQ_RUN) q->core.state = NVWQ_STOPPING;
    if (q->idle) thread_wakeup(&q->core.head);
    while (!gave_up && q->alive) {
        const uint64_t waited_ms = (nvx_now_us() - start) / 1000;
        if (waited_ms >= cap_ms) { gave_up = true; break; }
        const uint64_t left = cap_ms - waited_ms;
        assert_wait_timeout(&q->alive, THREAD_UNINT, (uint32_t)(left < 5000 ? left : 5000), NSEC_PER_MSEC);
        wq_unlock(q, s);
        (void)thread_block(THREAD_CONTINUE_NULL);
        s = wq_lock(q);
    }
    if (gave_up) {
        if (!q->abandoned) { q->abandoned = true; nvx_inc(&nvx_wq_abandoned); }
        // Whoever sees the worker gone drops the starter's reference: here if it has already left, else the worker.
        if (!q->alive) { drop = q->thread; q->thread = NULL; }
    } else {
        drop = q->thread;
        q->thread = NULL;
    }
    wq_unlock(q, s);
    if (drop) thread_deallocate(drop);
    IOLockLock(nvx_wq_admin);
    q->in_use = false;                 // an abandoned slot is skipped by nv_xnu_wq_gpu_start() all the same
    IOLockUnlock(nvx_wq_admin);
    __atomic_fetch_sub(&nvx_wq_gpu_queues, 1, __ATOMIC_RELAXED);
    if (gave_up) KLOG("work queue %s given up on after %u ms; the teardown goes on", nvx_wq_names[q->index], cap_ms);
}

// ---- start --------------------------------------------------------------------------------------------------------------
extern "C" void nv_xnu_kcore_init(void)
{
    static bool done;
    if (done) return;
    done = true;

    uint32_t atomic_arg = 1, workq_arg = 1;          // on unless the boot-arg says 0
    (void)PE_parse_boot_argn("nvrmatomic", &atomic_arg, sizeof atomic_arg);
    (void)PE_parse_boot_argn("nvrmworkq", &workq_arg, sizeof workq_arg);

    // The context answers. preemption_enabled() has to be true here and false under a simple lock taken with
    // interrupts off; if either reading is wrong, every OS-layer function keeps its older answer.
    if (atomic_arg == 0) {
        nvx_atomic_mode = 0;
        KLOG("nvrmatomic=0: OS-layer answers do not depend on the context");
    } else {
        bool ok = preemption_enabled();
        IOSimpleLock *probe = IOSimpleLockAlloc();
        if (!probe) ok = false;
        else {
            IOInterruptState s = IOSimpleLockLockDisableInterrupt(probe);
            const bool inside = preemption_enabled();
            IOSimpleLockUnlockEnableInterrupt(probe, s);
            IOSimpleLockFree(probe);
            if (inside) ok = false;
        }
        if (!ok) {
            nvx_atomic_mode = -1;
            KLOG("atomic-context self-test FAILED: OS-layer answers do not depend on the context");
        } else {
            nvx_pool_lock = IOSimpleLockAlloc();
            if (nvx_pool_lock) nvap_init(&nvx_pool, nvx_pool_mem, (unsigned)sizeof nvx_pool_mem);
            nvx_dfree_call = thread_call_allocate(nvx_dfree_drain, NULL);
            nvx_atomic_mode = 1;
            __atomic_store_n(&nvx_atomic_on, true, __ATOMIC_RELEASE);
        }
    }

    // The work queues.
    if (workq_arg == 0) {
        nvx_wq_mode = 0;
        KLOG("nvrmworkq=0: RM work items run each on a thread of its own");
    } else {
        nvx_wq_admin = IOLockAlloc();
        if (wq_start_slot(&nvx_wq[0], 0)) {
            nvx_wq_mode = 1;
            __atomic_store_n(&nvx_wq_on, true, __ATOMIC_RELEASE);
        } else {
            nvx_wq_mode = -1;
            KLOG("work queue thread could not be created: RM work items run each on a thread of its own");
        }
    }
}
