/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

/* nvrm_fence.h - what the rest of NVRM.kext calls in the surface fence (nvrm_fence.cpp). The interface to user space
 * and to NVAccel is nvrm_fence_abi.h; NVAccel is another kext and reaches the fence only through the function table
 * that nvrm_fence_kernel_ops() fills.
 *
 * One lock, the fence lock (an IOLock), and it is a leaf: nothing here allocates, sleeps on anything but the fence's
 * own wakeup, or calls IOKit or the RM while holding it. Callers may hold the user client's gate (clientClose) or the
 * accelerator lock (NVAccel's waits) when they call in; the fence never takes either. It makes no RM call and touches
 * no hardware, so no call here needs the GPU to be awake.
 *
 * Every function is safe, and does nothing, before the first nvrm_fence_start() and after the last nvrm_fence_stop():
 * a GPU that sleeps or stops half-way through its start calls them in any order. */
#pragma once
#include <libkern/OSTypes.h>
#ifndef __cplusplus
#include <stdbool.h>
#endif
#include "nvrm_fence_abi.h"

#ifdef __cplusplus
extern "C" {
#endif

/* nvrm_fence_pm(what) */
enum { NVRM_FENCE_PM_RESUMED = 0, NVRM_FENCE_PM_SUSPENDING = 1, NVRM_FENCE_PM_LOST = 2 };

/* A GPU finished starting. Thread context, may sleep; call it once per GPU as the last step of a successful start,
 * after the adapter is initialised. The first call reads the boot-arg nvrmfence (0 = off for this boot), allocates the
 * table and registers the debug.nvrm_fence_* sysctls. When the allocation fails the fence stays off: every later call
 * is refused or does nothing and user space keeps the older mechanism. Nothing to check, nothing to undo. */
void nvrm_fence_start(void);

/* A GPU is stopping (kext stop or shutdown). Thread context; call it once for each GPU whose start called
 * nvrm_fence_start(), before any RM teardown: it makes no RM call. When the last GPU stops, every hold ends, every
 * sleeper in a wait is woken and returns, and the fence refuses everything from then on. It does not wait for the
 * sleepers to leave: they hold no fence memory that is freed (the table is never freed). */
void nvrm_fence_stop(void);

/* The GPU may have completed work. Any thread context that may take no lock and must not sleep, including the
 * interrupt work loop and the bottom-half thread: two atomic operations and, only when somebody sleeps in a wait, one
 * thread_wakeup. Call it after every handled interrupt (whatever the RM answered) and after every bottom-half run,
 * because a channel error reaches its notifier in the bottom half. Not for the primary interrupt filter. */
void nvrm_fence_kick(void);

/* Sleep and wake of a GPU. Thread context (the power-management call).
 *   NVRM_FENCE_PM_SUSPENDING (1)  before user channels are stopped: every hold ends, readers leave at once, and new
 *                                 holds are refused until the matching RESUMED, so work committed across the
 *                                 transition uses the older mechanism.
 *   NVRM_FENCE_PM_RESUMED (0)     the GPU is back. Pairs with one SUSPENDING.
 *   NVRM_FENCE_PM_LOST (2)        the resume failed: as the last stop, the fence stays off.
 * Call exactly one of RESUMED or LOST for each SUSPENDING. */
void nvrm_fence_pm(int what);

/* The body of user-client selector NVRM_FENCE_SELECTOR. The caller's thread, WITHOUT the user client's gate (a WAIT
 * sleeps up to NVRM_FENCE_WAIT_MAX_MS and must not hold up the client's other calls).
 *   ownerId  the client's fence owner word: zero when the client is allocated, never written by the client again.
 *            The fence assigns it (compare-and-swap) on the client's first CHAN_ADD or OPEN.
 *   closing  the client's "cleanup has begun" word, non-zero from the moment clientClose commits to cleaning up. The
 *            fence re-reads it under its lock, so a request racing the close cannot add state after
 *            nvrm_fence_owner_gone() ran.
 *   r        the request, already copied in and of exactly sizeof(struct NVRMFenceReq); the answer is written in place.
 * The answer is r->status (NVRM_FENCE_ST_*); the selector returns kIOReturnSuccess whenever it got this far. */
void nvrm_fence_user(volatile UInt64 *ownerId, const volatile SInt32 *closing, struct NVRMFenceReq *r);

/* The client that owned `ownerId` is closing (its process exited or closed the connection). Thread context; the
 * caller may hold the client's gate. Call it after `closing` is set and BEFORE the RM frees the client's objects: the
 * client's channels and holds end at once (readers see DEAD instead of waiting out a bound on a channel that no longer
 * advances), and the kernel's views of its pages are released after the lock. ownerId 0 (the client never used the
 * fence) does nothing. */
void nvrm_fence_owner_gone(UInt64 ownerId);

/* The platform function NVRM_FENCE_FN_OPS: fill the table NVAccel keeps. Thread context, no RM call, no power
 * reference needed. Returns true with o->wait set when o->version and o->size match and the fence is not switched off
 * by its boot-arg; false otherwise (answer kIOReturnUnsupported: NVAccel then keeps its own waits). The pointer stays
 * valid until NVRM.kext unloads, which cannot happen while NVAccel is matched on it. o->wait() takes only the fence
 * lock, cannot be interrupted, is never refused for the number of sleepers, and may sleep up to w->timeoutMs. */
bool nvrm_fence_kernel_ops(struct NVRMFenceKernelOps *o);

#ifdef __cplusplus
}
#endif
