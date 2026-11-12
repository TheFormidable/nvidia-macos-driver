/* nvmtl_fence.h - the plugin's side of the kernel surface fence: which surfaces a command buffer holds, whether the
   kernel fence can be used, and which of the older completion marks still have to reach the kernel.

   When a command buffer writes IOSurfaces, another process (or the display pipe) must not read them before the GPU
   finished. Release 2.9 does that with a tag in the surface's own bytes and, in WindowServer, a mark in NVAccel. With
   the fence of NVRM.kext (nvrm_fence_abi.h) the kernel holds the surface instead: the plugin opens one hold per written
   surface when the command buffer commits, arms each with the fence of the last part that writes it, and closes it when
   the work is done; a reader asks the kernel to wait. One decision per command buffer says which mechanism covers it,
   and exactly one always does.

   This header is the part of that with no system call in it, so a host test can prove it (tools/test_fence_plugin.c):

     nvmtl_fence_hold     the surfaces of one command buffer and the last part that writes each (at most 16; a command
                          buffer that writes more is never opened in the kernel and keeps the older mechanism whole)
     nvmtl_fence_thread   what the last open on the calling thread put in the kernel: the surfaces the older mark and
                          tag code must leave to the kernel
     nvmtl_fence_state    is the kernel fence usable, and when to ask again
     nvmtl_fence_legacy   surfaces that carry an older pending mark, so only their clears are sent
     nvmtl_fence_wait_handled   which answers of a kernel wait end the reader's wait

   None of it locks: the caller serialises one nvmtl_fence_legacy and one nvmtl_fence_state with a mutex of its own; a
   nvmtl_fence_hold belongs to its command buffer and a nvmtl_fence_thread to its thread. Plain C, stdint only. */
#ifndef NVMTL_FENCE_H
#define NVMTL_FENCE_H
#include <stdint.h>

#define NVMTL_BUSY_KFENCE       0x4B464E43u   /* the pad word of a pending busy tag whose write the kernel also holds */
#define NVMTL_FENCE_HOLD_MAX    16
#define NVMTL_FENCE_WAIT_MS     2000u         /* a reader's budget for one kernel wait */
#define NVMTL_FENCE_POLL_US     10000u        /* how long the older poll of a busy tag lasts (NVMTL_BUSY_WAIT_NS) */
#define NVMTL_FENCE_PROBE_NS    1000000000ull /* how often an unusable fence is asked again */
#define NVMTL_FENCE_LEGACY_MAX  64

/* The kext's numbers the plugin uses. The plugin does not see nvrm_fence_abi.h (NVK builds the request), so they are
   repeated here; tools/test_fence_plugin.c asserts each equals its NVRM_FENCE_*. */
#define NVMTL_FENCE_MAX_IDS      8u            /* surfaces in one OPEN or ARM: a hold of 16 goes in two */
#define NVMTL_FENCE_OP_QUERY     0u
#define NVMTL_FENCE_OP_OPEN      4u
#define NVMTL_FENCE_OP_ARM       5u
#define NVMTL_FENCE_OP_CLOSE     6u
#define NVMTL_FENCE_OP_WAIT      7u
#define NVMTL_FENCE_F_COMPOSITOR 0x01u
#define NVMTL_FENCE_F_AUTO       0x02u
#define NVMTL_FENCE_ST_OK        0u
#define NVMTL_FENCE_ST_OFF       0x80000002u
#define NVMTL_FENCE_R_NONE       0u
#define NVMTL_FENCE_R_READY      1u
#define NVMTL_FENCE_R_DEAD       2u
#define NVMTL_FENCE_R_TIMEOUT    3u
#define NVMTL_FENCE_R_BUDGET     4u
#define NVMTL_FENCE_R_ABORTED    5u
#define NVMTL_FENCE_R_PENDING    6u

typedef struct nvmtl_fence_hold {            /* one command buffer's holds; fixed at commit */
    uint64_t cookie;                         /* 0 = nothing in the kernel */
    uint32_t n, overflow;                    /* overflow: more than 16 surfaces, never opened */
    uint32_t sid[NVMTL_FENCE_HOLD_MAX], last[NVMTL_FENCE_HOLD_MAX];   /* last: index of the last part that writes sid */
    uint8_t  planes[NVMTL_FENCE_HOLD_MAX];   /* bit (plane & 7) for every plane written */
} nvmtl_fence_hold;
typedef struct { uint32_t n; uint32_t sid[NVMTL_FENCE_HOLD_MAX]; } nvmtl_fence_thread;   /* what the last open on this thread put in the kernel */
typedef struct { uint32_t n, overflow; uint32_t sid[NVMTL_FENCE_LEGACY_MAX]; } nvmtl_fence_legacy;   /* surfaces with an older mark outstanding */
typedef struct { int active, accel; uint64_t next_probe_ns; } nvmtl_fence_state;         /* all zero = never asked */

/* ---- one command buffer's holds ---- */

/* Empty the record: no surface, no cookie, no overflow. Before the walk over the command buffer's parts. */
static inline void nvmtl_fence_hold_reset(nvmtl_fence_hold *h)
{
    h->cookie = 0; h->n = 0; h->overflow = 0;
    for (unsigned i = 0; i < NVMTL_FENCE_HOLD_MAX; i++) { h->sid[i] = 0; h->last[i] = 0; h->planes[i] = 0; }
}

/* Part `part` of the command buffer writes plane `plane` of surface `sid`. A surface seen again merges the plane into
   its mask and keeps the highest part; the 17th different surface sets `overflow` and is not recorded. The plane's bit
   is the one a reader passes to the wait, 1 << (plane & 7). Surface 0 is not a surface (the kernel refuses a request
   that names it) and is ignored. */
static inline void nvmtl_fence_hold_note(nvmtl_fence_hold *h, uint32_t sid, uint32_t plane, uint32_t part)
{
    const uint8_t bit = (uint8_t)(1u << (plane & 7u));
    if (!sid) return;
    for (uint32_t i = 0; i < h->n; i++)
        if (h->sid[i] == sid) {
            h->planes[i] |= bit;
            if (part > h->last[i]) h->last[i] = part;
            return;
        }
    if (h->n >= NVMTL_FENCE_HOLD_MAX) { h->overflow = 1; return; }
    h->sid[h->n] = sid; h->last[h->n] = part; h->planes[h->n] = bit; h->n++;
}

/* The surfaces whose last writer is `part`, in the order they were noted: the holds to arm with that part's fence.
   Returns how many it wrote to `out` (0..16). */
static inline unsigned nvmtl_fence_hold_last(const nvmtl_fence_hold *h, uint32_t part, uint32_t out[NVMTL_FENCE_HOLD_MAX])
{
    unsigned k = 0;
    for (uint32_t i = 0; i < h->n && i < NVMTL_FENCE_HOLD_MAX; i++)
        if (h->last[i] == part) out[k++] = h->sid[i];
    return k;
}

/* 1 when the last open on this thread put a hold of `sid` in the kernel. Surface 0 is never in the record. */
static inline int nvmtl_fence_thread_has(const nvmtl_fence_thread *t, uint32_t sid)
{
    if (!t || !sid) return 0;
    for (uint32_t i = 0; i < t->n && i < NVMTL_FENCE_HOLD_MAX; i++)
        if (t->sid[i] == sid) return 1;
    return 0;
}

/* ---- is the kernel fence usable ---- */

/* 1 when a hold of this class can be opened and a wait of this class asked. `compositor` is non-zero for
   WindowServer's holds and for a wait on them: those also need NVAccel to read the kernel's table (`accel`), because
   the display pipe's own waits are what honours them. */
static inline int nvmtl_fence_usable(const nvmtl_fence_state *s, int compositor)
{
    return s->active && (!compositor || s->accel);
}

/* 1 when the caller should send a QUERY now and give the answer to nvmtl_fence_probe_set(): the fence is not usable
   for this class and it was never asked, or the last answer is at least NVMTL_FENCE_PROBE_NS old. `now` is any
   monotonic clock in nanoseconds, the same one every call uses. */
static inline int nvmtl_fence_probe_due(const nvmtl_fence_state *s, int compositor, uint64_t now)
{
    return !nvmtl_fence_usable(s, compositor) && now >= s->next_probe_ns;
}

/* Record an answer and schedule the next probe one period after `now`. After a QUERY: `status` is the call's status
   (anything but NVMTL_FENCE_ST_OK, a failed call included, means off) and `active`, `accel` are the answer's words.
   After any other call that answered NVMTL_FENCE_ST_OFF: pass that status with active = accel = 0, which switches the
   fence off until a later probe finds it on again. `accel` is kept only while active. Returns 1 when `active` or
   `accel` changed (the caller logs one line per change), 0 otherwise. */
static inline int nvmtl_fence_probe_set(nvmtl_fence_state *s, uint64_t now, uint32_t status, uint32_t active, uint32_t accel)
{
    const int a = (status == NVMTL_FENCE_ST_OK && active) ? 1 : 0;
    const int x = (a && accel) ? 1 : 0;
    const int changed = (a != s->active || x != s->accel) ? 1 : 0;
    s->active = a; s->accel = x; s->next_probe_ns = now + NVMTL_FENCE_PROBE_NS;
    return changed;
}

/* ---- the older completion mark (NVAccel's pending mark of a surface) ---- */

/* Should this mark go to the kernel? 1 = send it as before, 0 = do not send (the caller answers 0, "taken").
     surface 0      always sent: it is the once-a-second question whether the marks are still switched on.
     pending != 0   not sent when the surface is in the calling thread's record `t` (the hold the kernel just took
                    covers it); otherwise sent, and the caller gives the kernel's answer to nvmtl_fence_mark_sent().
     pending == 0   a clear. Sent only when the surface is remembered in `L` (and it is forgotten here), or when `L`
                    ever overflowed: then every clear is sent, because a mark that could not be remembered must still
                    be cleared, and clearing a mark that is not set is harmless.
   `t` may be NULL (no record). */
static inline int nvmtl_fence_mark_route(nvmtl_fence_legacy *L, const nvmtl_fence_thread *t, uint32_t sid, int pending)
{
    if (!sid) return 1;
    if (pending) return nvmtl_fence_thread_has(t, sid) ? 0 : 1;
    int found = 0;
    for (uint32_t i = 0; i < L->n && i < NVMTL_FENCE_LEGACY_MAX; i++)
        if (L->sid[i] == sid) { L->sid[i] = L->sid[--L->n]; found = 1; break; }
    return (found || L->overflow) ? 1 : 0;
}

/* The kernel's answer to a PENDING mark that nvmtl_fence_mark_route() said to send. Answer 0 (taken): the surface is
   remembered so that its clear is sent; when 64 are already remembered, `overflow` is set instead and stays set. Any
   other answer (refused): nothing is remembered, so no clear is sent for it. Call it for pending marks only, never
   after a clear; surface 0 is ignored. */
static inline void nvmtl_fence_mark_sent(nvmtl_fence_legacy *L, uint32_t sid, uint32_t answer)
{
    if (!sid || answer) return;
    for (uint32_t i = 0; i < L->n && i < NVMTL_FENCE_LEGACY_MAX; i++)
        if (L->sid[i] == sid) return;
    if (L->n >= NVMTL_FENCE_LEGACY_MAX) { L->overflow = 1; return; }
    L->sid[L->n++] = sid;
}

/* ---- a kernel wait's answer ---- */

/* 1 when the reader goes on at once; 0 when the older wait on the tag decides, as it does without a fence (a tag older
   than 100 ms is passed at once, a newer one is polled for up to 10 ms). `waited_us` is how long the kernel kept this
   reader, the second word of the wait's answer.
     READY    the kernel saw the write complete, or its writer closed the hold: always 1. Nothing else says "complete".
              The kernel also answers READY when more holds ended during the wait than it keeps the reasons of, so
              that it cannot say how this one ended; nothing in the answer tells that case apart, and it ends the
              wait like any READY.
     NONE     the kernel holds nothing for this reader: the writer closed its hold and writes its tag next, or the hold
              was let go before this reader came (the cases of DEAD and TIMEOUT below).
     DEAD     the hold was let go without completion: the writer exited, its channel faulted, the GPU went to sleep or
              the fence stopped. The last two leave a live writer whose GPU work may still run.
     TIMEOUT  the kernel gave the hold up at its bound (or with every other hold, when the fence switched itself off).
     BUDGET   this reader's own two seconds ran out.
   These four end the wait only when the kernel has by then kept the reader at least as long as the older poll would
   have, NVMTL_FENCE_POLL_US: that reader has given the writer what it always got. A reader the kernel sends away
   sooner goes on to the older wait. So READY is the only answer on which a reader can read earlier than it did
   without a fence.
   0 for everything else (ABORTED, PENDING, an unknown result, any error status), whatever the time. */
static inline int nvmtl_fence_wait_handled(uint32_t status, uint32_t result, uint32_t waited_us)
{
    if (status != NVMTL_FENCE_ST_OK) return 0;
    if (result == NVMTL_FENCE_R_READY) return 1;
    if (result == NVMTL_FENCE_R_NONE || result == NVMTL_FENCE_R_DEAD || result == NVMTL_FENCE_R_TIMEOUT || result == NVMTL_FENCE_R_BUDGET)
        return waited_us >= NVMTL_FENCE_POLL_US;
    return 0;
}

#endif
