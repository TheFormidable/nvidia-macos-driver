/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

// nv_xnu_kcore.h: what the rest of NVRM.kext calls in nv_xnu_kcore.cpp: the answer to "may this context block?", the
// serialized work queues that replace one thread per RM work item, and two small note helpers.
//
// The RM calls its OS layer from contexts that must not sleep (under its spinlocks, with preemption off). On Linux
// every OS-layer function asks the kernel which kind of context it is in and takes a non-blocking branch when it has
// to. nvx_can_block() is that question here. The work queues give the RM what a Linux kthread queue gives it: one
// worker per queue, items run in the order they were queued, and a flush that returns when everything queued before
// it has run.
//
// Lock order, complete: RM locks (API lock, GPU locks, RM spinlocks) -> a queue's own simple lock (interrupts off;
// only assert_wait and thread_wakeup happen inside). The queue administration lock (slot allocation, start, stop) is
// an IOLock taken in thread context only and never while a queue's simple lock is held.

#pragma once
#include "nv-xnu.h"

extern "C" {

// Once, in thread context, from the first start of the kext, before the RM is entered for the first time. Reads the
// boot-args nvrmatomic and nvrmworkq (only the value 0 switches a feature off), tests the context predicate against a
// lock it takes itself, allocates what the features need and starts the global work queue. Each thing that cannot be
// had switches only its own feature back to the older behaviour; there is nothing to check and nothing to undo. A
// second call does nothing.
void nv_xnu_kcore_init(void);

// Registers this file's read-only sysctls. Thread context; call it once, from the block that registers the kext's
// other sysctls.
void nv_xnu_kcore_sysctls_register(void);

// May the calling context block: sleep, wait for an IOLock, allocate with waiting? Any context; no lock.
// It is the kernel's own answer (preemption and interrupts enabled) EXCEPT when the non-blocking branches are switched
// off, by the boot-arg nvrmatomic=0 or because the start-up test of the predicate failed: then it is true in every
// context, so that every OS-layer function runs its older body. Use it for exactly that choice, between the older
// body and the non-blocking branch of a function the RM calls.
// Do NOT use it to decide whether a wait this driver adds may sleep (a cancel that waits for a callback, a quiesce):
// with the branches switched off it says yes with preemption off, and sleeping there panics. Such code asks
// preemption_enabled() itself; nv_xnu_quiesce.h has the helper.
bool nvx_can_block(void);

// One of the OS-layer functions was entered in a context that cannot block. Any context: a counter per site and, on
// the first hit of a site, one kprintf line with the caller as an offset into the kext. No IOLog, no allocation, no
// lock. `site` is an NVX_SITE_* value (others are ignored), `ra` the return address of the OS-layer function.
void nvx_note(unsigned site, void *ra);

// A queue of its own for one GPU. Thread context (takes the administration lock, starts a thread). Call it while
// starting a GPU, before the adapter is initialised, and store the result in nv->queue.
// Returns NULL when there is no free slot, the worker could not be started, or the queues are switched off
// (nvrmworkq=0): that is NOT a failure. With nv->queue NULL the GPU's items go to the global queue, which is
// serialized too (or, with the queues off, run as they did before). Never fail a start because of it.
// Every non-NULL result is handed to nv_xnu_wq_gpu_down() exactly once.
struct os_work_queue *nv_xnu_wq_gpu_start(void);

// Stop a GPU's own queue: flush it, tell the worker to exit, wait for it, release the slot. Thread context, and never
// from a work item (a call from the queue's own worker is refused and logged). Set nv->queue to NULL BEFORE calling,
// so that items queued from then on go to the global queue. There are two waits inside, the flush and the wait for the
// worker to leave, and cap_ms bounds each of them (0 is no bound to ask for here and is taken as 30000). The kext's
// stop, which nothing else bounds, uses 30000; the system shutdown path has 8 s for everything it does and passes at
// most 2000, as it does to its flushes. When a wait runs out, or the queue was already abandoned by a capped flush,
// the slot is marked abandoned and never used again, the worker is left alone and the function returns: the teardown
// goes on, as it always did before there were queues. NULL does nothing.
void nv_xnu_wq_gpu_down(struct os_work_queue *q, unsigned cap_ms = 30000);

// Wait until every item that was queued before the call has run. q NULL is the global queue. `unload` marks the flush
// that belongs to a GPU's teardown (the RM's is_unload): while it runs, os_is_queue_flush_ongoing() answers true for
// that queue. cap_ms 0 waits without a bound, as the RM's own flushes do (each 5 s of waiting logs the item that is
// running); the kext's teardown, sleep and shutdown paths always pass a cap. With the queues switched off
// (nvrmworkq=0, or the global worker could not be started) this is the older flush, which waits for the per-item
// threads.
//   NV_OK                    everything queued before the call has run. Also: the caller is the queue's own worker
//                            (waiting for itself cannot end; returns at once, counted).
//   NV_ERR_ILLEGAL_ACTION    the caller cannot block (nvx_can_block() is false). Nothing was waited for; counted.
//   NV_ERR_TIMEOUT           only with a cap: it ran out with work still queued or running. If q is a GPU's own
//                            queue it is now ABANDONED: every later flush of it, capped or not, returns
//                            NV_ERR_TIMEOUT at once without waiting. Without this rule the RM's own unbounded
//                            flushes during the adapter teardown would hang on the queue the caller has just given
//                            up on. The global queue is never abandoned: its next flush waits again.
// On every result the caller carries on with what it was doing; a teardown must not stop half-way because work is
// stuck. After NV_ERR_TIMEOUT an item may still be running: free nothing it could use that the normal order would
// not already free.
NV_STATUS nv_xnu_wq_flush(struct os_work_queue *q, bool unload, unsigned cap_ms);

// A function of the OS layer that has no real body was called. Any context (no lock: its place in the buffer is
// reserved with one atomic add; no allocation). The name is copied into a fixed buffer that a read-only sysctl
// publishes; call it once per function, from the once-only branch of the stub macro.
void nv_xnu_stub_note(const char *name);

// The non-blocking branches of the OS layer (os-xnu.cpp, os-xnu2.cpp). Any context; no IOLog, no heap call, no
// IOLock. Each is used only where nvx_can_block() is false, except nvx_atomic_free(), which every free goes through
// because a block from the pool has to go back to the pool whatever the context of its free.
//
// A zeroed block of `size` bytes from the kext's own fixed pool (256 KB, requests up to 64 KB), or NULL: the caller
// then makes its older call, which is counted as a miss.
void *nvx_atomic_alloc(NvU64 size);
// 1: `p` has been dealt with (a pool block, freed at once; or, where the heap may not be called, a heap block handed
// to a thread call that frees it); 0: free it the older way.
int nvx_atomic_free(void *p);
// Try to take a lock without blocking, again every 20 us, for at most 20 ms. 1: taken; 0: not taken, and the caller
// makes its older, blocking call (what it always did). `kind` is an NVX_LOCK_* value.
int nvx_atomic_lock(int kind, void *lock);
// os_semaphore_may_sleep() answered no: a counter.
void nvx_atomic_maysleep_false(void);

// ---- os-xnu.cpp: which thread is inside rm_isr, one slot per GPU (what os_is_isr() answers from) ----
#define NV_XNU_ISR_SLOTS 8   // NVRM_DEVTAB_MAX: a ninth card is not started
// Written only by the owning GPU's interrupt handler: its own thread just before rm_isr, NULL right after.
extern volatile thread_t nv_xnu_isr_threads[NV_XNU_ISR_SLOTS];
// The slot of a starting GPU (`owner` is its NVRM object; the same slot again when it already holds one), or -1 when
// all are taken. Thread context; no lock (compare-and-swap).
int nv_xnu_isr_slot_claim(void *owner);
// Gives the slot back, once the GPU's interrupt source is removed. Does nothing for -1 or a slot `owner` does not hold.
void nv_xnu_isr_slot_release(int slot, void *owner);

// ---- nv_xnu_timers.cpp: the RM's RC timer (1 Hz) and nano timers ----
// The RC timer is on unless the boot-arg nvrmrctimer=0. These are for the kext's own paths; the RM starts and stops the
// timer itself through nv_start_rc_timer / nv_stop_rc_timer. All thread context; each may wait for a tick that is
// running, bounded (10 s, 2 s for a suspend), and does nothing for a GPU whose timer was never started.
void nv_xnu_rc_suspend(nv_state_t *nv);   // before RM standby or shutdown: no tick runs from now on
void nv_xnu_rc_resume(nv_state_t *nv);    // after a resume that worked: ticks again if the RM had started the timer
void nv_xnu_rc_detach(nv_state_t *nv);    // after rm_shutdown_adapter: no tick will carry this nv again; idempotent
// After rm_shutdown_adapter: every nano timer of this GPU that the RM did not destroy is stopped, counted and never
// reused; no callback will carry this nv again. Thread context; at most 2 s in all.
void nv_xnu_nano_retire(nv_state_t *nv);
// Registers the timers' read-only sysctls. Thread context; once (a second call does nothing).
void nv_xnu_timers_sysctls(void);

}

enum { NVX_LOCK_MUTEX, NVX_LOCK_RW_READ, NVX_LOCK_RW_WRITE, NVX_LOCK_SEMA };

// Where nvx_note() is called from
enum { NVX_SITE_ALLOC, NVX_SITE_FREE, NVX_SITE_DELAY, NVX_SITE_SCHEDULE, NVX_SITE_MUTEX, NVX_SITE_RWLOCK,
       NVX_SITE_SEMA, NVX_SITE_LOCKALLOC, NVX_SITE_BLOCKING_ENTRY, NVX_SITE_FLUSH, NVX_SITE_MAX };
