/* nvmtl_idle_wait.h - waiting until no command buffer names a resource any more, woken instead of polling.
   Every buffer and texture counts the command buffers whose resource sets name it (_resRefs, moved only by nvmtl_resref
   in NVMTLObjects.m). Apple's OpenGL renderer asks -[NVMTLBuffer waitUntilComplete] (and the texture's) to block until
   that count is zero, from GLDBufferRec::restoreData under glDrawElements. The wait used to sleep 50 us and look again,
   up to 10 s. Here the waiter sleeps on one process-wide condition, with the address of the count it waits for listed
   beside it, and the release that brings that count to zero wakes it. The release side costs one load of the waiter
   count, made only on that transition, and nothing else while nobody waits; while somebody waits, an uncontended lock
   and a look at the list, and a wake only for a listed count (a retire brings many unrelated counts to zero).
   NVMTL_GL_WAIT_EVENT=0: the 50 us polling, as before.

   No wake-up is lost. The waiter adds itself to the waiter count (sequentially consistent) and then reads the resource's
   count (sequentially consistent), both under the lock, and stays under the lock until pthread_cond_wait releases it. The
   change that brings the count to zero (a release, or an add that ends a negative count) does so (sequentially
   consistent) and then reads the waiter count (sequentially consistent).
   In the single order of those four operations either the waiter's read of the count comes after the release, and it
   sees zero (or a later reference, whose own release repeats this argument) and does not sleep; or it comes before, so
   the waiter's increment precedes the release's read, which sees a waiter and takes the lock - which it can get only once
   the waiter sleeps - and finds its listed address (written under the lock before the increment), and broadcasts.
   Every change of a count goes through nvmtl_idle_add, so the order covers them all.
   On x86_64 a sequentially consistent read-modify-write is the same locked instruction as the acquire-release one it
   replaces. tools/test_idle_wait.c proves it with threads. Pure: no Metal, no Vulkan. */
#ifndef NVMTL_IDLE_WAIT_H
#define NVMTL_IDLE_WAIT_H
#include <pthread.h>
#include <stdint.h>
#include <time.h>

#define NVMTL_IDLE_WATCH 8
typedef struct {
    pthread_mutex_t lock;
    pthread_cond_t idle;
    int waiters;                            /* threads inside nvmtl_idle_wait_for; read without the lock by releases */
    const int *watch[NVMTL_IDLE_WATCH];     /* the counts they wait for, under the lock */
    int unlisted;                           /* waiters that found no free slot: any count at zero wakes them */
} nvmtl_idle_waitq;
#define NVMTL_IDLE_WAITQ_INIT { PTHREAD_MUTEX_INITIALIZER, PTHREAD_COND_INITIALIZER, 0, { 0 }, 0 }

/* the longest single sleep: the 10 s limit is measured on the monotonic clock, and on macOS each slice is a relative
   timeout, so a step of the wall clock changes neither (the host test sets its own slice, to tell a wake from a timeout) */
#ifndef NVMTL_IDLE_SLICE_NS
#define NVMTL_IDLE_SLICE_NS 100000000ull
#endif

/* NVMTL_GL_WAIT_EVENT: on unless it is set to a value that starts with 0 */
static inline int nvmtl_idle_wait_on(const char *value) { return !(value && value[0] == '0'); }

/* Every change of a reference count; returns the new count. */
static inline int nvmtl_idle_add(int *refs, int d) { return __atomic_add_fetch(refs, d, __ATOMIC_SEQ_CST); }

/* A count is busy while a command buffer names the resource. A texture waits while its count is above zero, a buffer
   while it is not zero, as their waits always did. */
static inline int nvmtl_idle_busy(const int *refs, int textureRule)
{
    const int v = __atomic_load_n(refs, __ATOMIC_SEQ_CST);
    return textureRule ? v > 0 : v != 0;
}

/* Whether a waiter waits for the count at refs; asked under the lock. */
static inline int nvmtl_idle_wanted(const nvmtl_idle_waitq *q, const int *refs)
{
    if (q->unlisted > 0) return 1;
    for (int i = 0; i < NVMTL_IDLE_WATCH; i++) if (q->watch[i] == refs) return 1;
    return 0;
}

/* The release side, after nvmtl_idle_add brought the count at refs to zero (from either side: a buffer also waits out a
   negative count). Nothing but one load while nobody waits. */
static inline void nvmtl_idle_settled(nvmtl_idle_waitq *q, const int *refs)
{
    if (!__atomic_load_n(&q->waiters, __ATOMIC_SEQ_CST)) return;
    pthread_mutex_lock(&q->lock);
    if (nvmtl_idle_wanted(q, refs)) pthread_cond_broadcast(&q->idle);
    pthread_mutex_unlock(&q->lock);
}

static inline uint64_t nvmtl_idle_now_ns(void)
{
    struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

/* Blocks until the count is idle, or until limitNs has passed since startNs (monotonic, nvmtl_idle_now_ns). Returns 0
   when idle, 1 at the limit. *slept is set when the thread had to sleep at all (for the census). */
static inline int nvmtl_idle_wait_for(nvmtl_idle_waitq *q, const int *refs, int textureRule, uint64_t startNs,
                                      uint64_t limitNs, int *slept)
{
    if (slept) *slept = 0;
    if (!nvmtl_idle_busy(refs, textureRule)) return 0;
    int result = 0, slot = -1;
    pthread_mutex_lock(&q->lock);
    for (int i = 0; i < NVMTL_IDLE_WATCH && slot < 0; i++) if (!q->watch[i]) slot = i;
    if (slot >= 0) q->watch[slot] = refs; else q->unlisted++;
    __atomic_add_fetch(&q->waiters, 1, __ATOMIC_SEQ_CST);
    while (nvmtl_idle_busy(refs, textureRule)) {
        const uint64_t now = nvmtl_idle_now_ns();
        if (now - startNs > limitNs) { result = 1; break; }
        uint64_t slice = limitNs - (now - startNs) + 1;
        if (slice > NVMTL_IDLE_SLICE_NS) slice = NVMTL_IDLE_SLICE_NS;
        if (slept) *slept = 1;
#ifdef __APPLE__
        /* a relative timeout: a step of the wall clock cannot stretch a slice, so the limit holds */
        const struct timespec rel = { (time_t)(slice / 1000000000ull), (long)(slice % 1000000000ull) };
        (void)pthread_cond_timedwait_relative_np(&q->idle, &q->lock, &rel);   /* a timeout, a wake or a spurious return: asked again */
#else
        struct timespec until; clock_gettime(CLOCK_REALTIME, &until);
        const uint64_t ns = (uint64_t)until.tv_nsec + slice;
        until.tv_sec += (time_t)(ns / 1000000000ull); until.tv_nsec = (long)(ns % 1000000000ull);
        (void)pthread_cond_timedwait(&q->idle, &q->lock, &until);   /* a timeout, a wake or a spurious return: asked again */
#endif
    }
    __atomic_sub_fetch(&q->waiters, 1, __ATOMIC_SEQ_CST);
    if (slot >= 0) q->watch[slot] = NULL; else q->unlisted--;
    pthread_mutex_unlock(&q->lock);
    return result;
}

#endif
