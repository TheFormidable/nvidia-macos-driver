/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

/* nv_xnu_quiesce.h - "stop this, and know when nobody is inside any more", without a lock the hot path would have to
 * take and without an unbounded wait. Three small state machines, used by the interrupt path, the timers and the
 * bottom-half thread. No kernel type and no kernel call in them, only compiler atomics, so the host test
 * (tools/test_quiesce_gates.cpp) compiles the same code the kext runs.
 *
 * Why the two gates are correct. Both sides do a sequentially consistent store and then load the other side's
 * variable: the one entering stores "I am inside" (++inside) and then reads closed / gen; the one closing stores
 * closed / gen and then reads inside. At least one of the two sees the other's store. So once "close (or invalidate),
 * then wait until idle" has returned, nobody is inside and everybody who tries later is refused. That makes the pair
 * a barrier with a bounded wait: the wait is a poll with a deadline, and when the deadline passes the caller knows it
 * and can count it, instead of hanging in a cancel that waits for a callback stuck in the RM.
 *
 * Rules for every user:
 *   - A successful enter is followed by exactly one leave, on every path. A refused enter is not.
 *   - The closing side waits AFTER it closed or invalidated, never before.
 *   - "Idle" only means nobody is inside now. It is final only while the gate stays closed (NvXnuGate) or while no
 *     callback armed with the current generation exists (NvXnuGen).
 *   - What the callbacks touch must stay valid until the wait has returned idle. If the wait ran out, it must stay
 *     valid for good (keep it in storage that is never freed); the caller counts the timeout and goes on. */
#ifndef NV_XNU_QUIESCE_H
#define NV_XNU_QUIESCE_H

/* Entry gate: many enterers, one closer that must know when nobody is inside. Zero-initialised is open and idle.
 * Enter and leave are safe in any context (they only do atomics); so are close, open and idle. */
struct NvXnuGate { volatile int closed; volatile int inside; };
static inline int  nv_gate_enter(struct NvXnuGate *g)       /* 1: inside, caller must leave. 0: closed, not inside */
{
    __atomic_add_fetch(&g->inside, 1, __ATOMIC_SEQ_CST);
    if (__atomic_load_n(&g->closed, __ATOMIC_SEQ_CST)) { __atomic_sub_fetch(&g->inside, 1, __ATOMIC_SEQ_CST); return 0; }
    return 1;
}
static inline void nv_gate_leave(struct NvXnuGate *g) { __atomic_sub_fetch(&g->inside, 1, __ATOMIC_SEQ_CST); }
static inline void nv_gate_close(struct NvXnuGate *g) { __atomic_store_n(&g->closed, 1, __ATOMIC_SEQ_CST); }
static inline void nv_gate_open(struct NvXnuGate *g)  { __atomic_store_n(&g->closed, 0, __ATOMIC_SEQ_CST); }
static inline int  nv_gate_idle(const struct NvXnuGate *g) { return __atomic_load_n(&g->inside, __ATOMIC_SEQ_CST) == 0; }

/* Generation gate: a deferred callback (a thread call, a timer) carries the generation it was armed with, and runs
 * its body only if that is still the current one. Invalidating makes every callback armed earlier a no-op, whenever
 * it arrives, so the object behind it can be reused; it must not be freed while a late callback can still read the
 * gate itself. Zero-initialised is generation 0 and idle. */
struct NvXnuGen { volatile unsigned long long gen; volatile int inside; };
static inline unsigned long long nv_gen_current(const struct NvXnuGen *g) { return __atomic_load_n(&g->gen, __ATOMIC_SEQ_CST); }
/* every callback armed before this call is refused from now on; returns the new generation, to arm the next one with */
static inline unsigned long long nv_gen_invalidate(struct NvXnuGen *g) { return __atomic_add_fetch(&g->gen, 1, __ATOMIC_SEQ_CST); }
static inline int  nv_gen_enter(struct NvXnuGen *g, unsigned long long armed)   /* 1: inside, caller must leave. 0: stale, not inside */
{
    __atomic_add_fetch(&g->inside, 1, __ATOMIC_SEQ_CST);
    if (__atomic_load_n(&g->gen, __ATOMIC_SEQ_CST) != armed) { __atomic_sub_fetch(&g->inside, 1, __ATOMIC_SEQ_CST); return 0; }
    return 1;
}
static inline void nv_gen_leave(struct NvXnuGen *g) { __atomic_sub_fetch(&g->inside, 1, __ATOMIC_SEQ_CST); }
static inline int  nv_gen_idle(const struct NvXnuGen *g) { return __atomic_load_n(&g->inside, __ATOMIC_SEQ_CST) == 0; }

/* Bottom-half hand-off between the interrupt handler and the thread that runs the bottom half. Not atomic: every call
 * is made with the owner's lock held. The handler requests a run; the thread asks what to do next. */
enum { NV_BH_SLEEP, NV_BH_RUN, NV_BH_EXIT };
struct NvXnuBh { int pending, busy, quit, alive; };
static inline void nv_bh_request(struct NvXnuBh *b) { b->pending = 1; }
static inline int  nv_bh_next(struct NvXnuBh *b)     /* a requested run is never dropped: pending is tested before quit */
{
    if (b->pending) { b->pending = 0; b->busy = 1; return NV_BH_RUN; }
    return b->quit ? NV_BH_EXIT : NV_BH_SLEEP;
}
static inline void nv_bh_done(struct NvXnuBh *b) { b->busy = 0; }
static inline int  nv_bh_drained(const struct NvXnuBh *b) { return !b->pending && !b->busy; }

#ifdef KERNEL
#include <kern/sched_prim.h>
#ifdef __cplusplus
extern "C" {
#endif
/* May a wait for one of the gates above sleep between its polls, here and now? 1: yes. 0: preemption or interrupts
 * are off; the wait may only spin, with a short delay per poll and a much smaller bound, and then give up.
 *
 * This asks the kernel itself, on purpose. The OS layer's own question, nvx_can_block() (nv_xnu_kcore.h), is NOT the
 * same thing: it answers "yes" in every context when the non-blocking branches are switched off (boot-arg
 * nvrmatomic=0, or their start-up test failed), because its job is to choose between an older and a newer body of an
 * RM callback. A cancel that believed it and slept with preemption off would panic the machine. Every wait built on
 * these gates decides with this function and never with nvx_can_block(). */
static inline int nv_quiesce_may_sleep(void) { return preemption_enabled() ? 1 : 0; }
#ifdef __cplusplus
}
#endif
#endif

#endif
