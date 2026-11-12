/* test_fence_model.c - the surface fence's wait loop with real threads. The table and the loop are the kernel's own
   (kexts/NVRM/nvrm_fence_table.h); the kernel around them is rebuilt here from pthreads with the same order of steps
   as kexts/NVRM/nvrm_fence.cpp: one mutex for the fence lock, two atomic generations (interrupt, table change), a
   count of sleepers, and a wait queue that a thread joins BEFORE it reads the generations a second time.
   Part A walks every interleaving of one or two sleepers with one or two interrupts: no wakeup is lost, and four ways
   of building the sleep or the kick wrongly each lose one. Part B makes one reader meet an interrupt at the two
   instants that matter. Part C runs readers, producers, a GPU and its two interrupt contexts as threads: completions
   with and without interrupts, a writer that dies, a reader that is interrupted, budgets, bounds, the ceiling on
   sleepers, the machine going to sleep, and a mixed load with a faulting channel and a connection that is replaced.
   Build and run from the repository root:
     cc -std=c11 -O1 -Wall -Wextra -Werror -pthread -I kexts/NVRM tools/test_fence_model.c -o /tmp/test_fence_model && /tmp/test_fence_model */
#define _POSIX_C_SOURCE 200809L
#ifdef __APPLE__
#define _DARWIN_C_SOURCE                /* pthread_cond_timedwait_relative_np, see q_timedwait */
#endif
#include <errno.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include "nvrm_fence_table.h"

static int fails;
#define T(name, cond) do { if (!(cond)) { printf("FAIL %s\n", name); fails++; } else printf("ok   %s\n", name); } while (0)
#define OK    NVRM_FENCE_ST_OK
#define COMP  NVRM_FENCE_F_COMPOSITOR
#define AUTO  NVRM_FENCE_F_AUTO
#define IDX(h) ((h) & 0xFFFu)
#define SLACK_US 400000ull      /* what a thread may be late by on a busy machine before a time check calls it a failure */

/* ==== Part A: the sleep and the kick as steps, every interleaving ================================================== */

enum { S_ANNOUNCE, S_QUEUE, S_RECHECK, S_BLOCK, K_MOVE, K_READ, K_WAKE };
struct world { int moved, sleepers, queued[2], woken[2], blocked[2], seen[2]; };
struct prog { const int *step; int n, who; };       /* who: which of the two sleepers, or which of the two interrupts */

static void wake_all(struct world *w)               /* thread_wakeup: everything queued on the event, blocked or not yet */
{
    for (int i = 0; i < 2; i++) {
        if (!w->queued[i]) continue;
        w->woken[i] = 1;
        if (w->blocked[i]) { w->blocked[i] = 0; w->queued[i] = 0; w->sleepers--; }
    }
}
static void step(struct world *w, int op, int who)
{
    switch (op) {
    case S_ANNOUNCE: w->sleepers++; break;                              /* "somebody sleeps": the kick looks at this */
    case S_QUEUE:    w->queued[who] = 1; break;                         /* assert_wait: on the wait queue, still running */
    case S_RECHECK:  if (w->moved) wake_all(w); break;                  /* a generation moved since the look: wake (itself too) */
    case S_BLOCK:    if (w->woken[who]) { w->queued[who] = 0; w->sleepers--; } else w->blocked[who] = 1; break;
    case K_MOVE:     w->moved = 1; break;                               /* the interrupt's generation */
    case K_READ:     w->seen[who] = w->sleepers > 0; break;
    default:         if (w->seen[who]) wake_all(w); break;
    }
}
static unsigned long walks, losses;
static void walk(struct world w, const struct prog *p, int np, int *pc)
{
    int any = 0;
    for (int i = 0; i < np; i++) {
        struct world w2 = w;
        if (pc[i] >= p[i].n) continue;
        any = 1;
        step(&w2, p[i].step[pc[i]], p[i].who);
        pc[i]++; walk(w2, p, np, pc); pc[i]--;
    }
    /* everybody has run to the end: a sleeper still blocked was not told about an interrupt that came after its look */
    if (!any) { walks++; if (w.blocked[0] || w.blocked[1]) losses++; }
}
static const int SLEEP_OK[]            = { S_ANNOUNCE, S_QUEUE, S_RECHECK, S_BLOCK };    /* the kernel's order */
static const int SLEEP_NO_RECHECK[]    = { S_ANNOUNCE, S_QUEUE, S_BLOCK };
static const int SLEEP_RECHECK_FIRST[] = { S_ANNOUNCE, S_RECHECK, S_QUEUE, S_BLOCK };
static const int SLEEP_ANNOUNCE_LATE[] = { S_QUEUE, S_RECHECK, S_ANNOUNCE, S_BLOCK };
static const int KICK_OK[]             = { K_MOVE, K_READ, K_WAKE };                     /* the kernel's order */
static const int KICK_READ_FIRST[]     = { K_READ, K_MOVE, K_WAKE };
#define LEN(a) ((int)(sizeof(a) / sizeof((a)[0])))

static unsigned long explore(const int *sl, int nsl, const int *ki, int nki, int sleepers, int kicks)
{
    struct world w;
    struct prog p[4];
    int pc[4] = { 0, 0, 0, 0 }, np = 0;
    memset(&w, 0, sizeof(w));
    for (int i = 0; i < sleepers; i++) p[np++] = (struct prog){ sl, nsl, i };
    for (int i = 0; i < kicks; i++) p[np++] = (struct prog){ ki, nki, i };
    walks = losses = 0;
    walk(w, p, np, pc);
    return losses;
}

static void part_a(void)
{
    T("one sleeper, one interrupt: none of the 35 interleavings loses the wakeup",
      explore(SLEEP_OK, LEN(SLEEP_OK), KICK_OK, LEN(KICK_OK), 1, 1) == 0 && walks == 35);
    T("one sleeper, two interrupts (the interrupt path and the bottom half): none of 4,200",
      explore(SLEEP_OK, LEN(SLEEP_OK), KICK_OK, LEN(KICK_OK), 1, 2) == 0 && walks == 4200);
    T("two sleepers, one interrupt: none of 11,550",
      explore(SLEEP_OK, LEN(SLEEP_OK), KICK_OK, LEN(KICK_OK), 2, 1) == 0 && walks == 11550);
    T("two sleepers, two interrupts: none of 4,204,200",
      explore(SLEEP_OK, LEN(SLEEP_OK), KICK_OK, LEN(KICK_OK), 2, 2) == 0 && walks == 4204200);
    T("control: a sleep that does not read the generations again after queueing itself loses a wakeup",
      explore(SLEEP_NO_RECHECK, LEN(SLEEP_NO_RECHECK), KICK_OK, LEN(KICK_OK), 1, 1) != 0);
    T("control: so does one that reads them again before it is on the queue",
      explore(SLEEP_RECHECK_FIRST, LEN(SLEEP_RECHECK_FIRST), KICK_OK, LEN(KICK_OK), 1, 1) != 0);
    T("control: and one that counts itself as a sleeper only after reading them",
      explore(SLEEP_ANNOUNCE_LATE, LEN(SLEEP_ANNOUNCE_LATE), KICK_OK, LEN(KICK_OK), 1, 1) != 0);
    T("control: a kick that looks for sleepers before it moves its generation loses one",
      explore(SLEEP_OK, LEN(SLEEP_OK), KICK_READ_FIRST, LEN(KICK_READ_FIRST), 1, 1) != 0);
}

/* ==== the kernel around the table, from pthreads =================================================================== */

static struct nvf_table Tb;
static pthread_mutex_t L = PTHREAD_MUTEX_INITIALIZER;        /* the fence lock */
static unsigned changesSeen;                                 /* under L: the table's change count at the last unlock */
static atomic_uint gKick, gGen;                              /* the two generations */
static atomic_int gSleepers, gSleepersPeak;
static int gSleepersMax = 256;
static pthread_mutex_t Q = PTHREAD_MUTEX_INITIALIZER;        /* the wait queue of the one event */
static pthread_cond_t QC;
static unsigned long qEpoch;                                 /* under Q: ++ per wakeup; "woken since I queued" = it moved */

/* what the GPU and the producers write into the channels' pages, without any lock */
#define GPU_CHANS 16u
static atomic_ullong chDone[NVF_CHANS], chFlushed[NVF_CHANS], chTarget[NVF_CHANS], chReadyAt[NVF_CHANS];
static atomic_int chFaulted[NVF_CHANS], chIrq[NVF_CHANS];
static atomic_int gpuRunning, gpuStop;                       /* the GPU thread and its bottom half (Part C) */
static atomic_ulong gpuScans;
static atomic_uint bhPending;

static unsigned long long now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (unsigned long long)ts.tv_sec * 1000000000ull + (unsigned long long)ts.tv_nsec;
}
static void pause_us(unsigned us)
{
    struct timespec ts = { 0, (long)us * 1000L };
    nanosleep(&ts, NULL);
}

static void q_wakeup(void) { pthread_mutex_lock(&Q); qEpoch++; pthread_cond_broadcast(&QC); pthread_mutex_unlock(&Q); }

/* The queue's condition variable waits until a point on the monotonic clock, the clock of every deadline here. Linux
   binds the variable to that clock (pthread_condattr_setclock). macOS has no such call and its absolute waits run on
   the wall clock, so there the wait is given as the time left until the deadline, measured again on every call: an
   early return is then only one more pass of the caller's loop. Called with Q held; 1 = the deadline has passed. */
static void q_init(void)
{
#ifdef __APPLE__
    pthread_cond_init(&QC, NULL);
#else
    pthread_condattr_t ca;
    pthread_condattr_init(&ca);
    pthread_condattr_setclock(&ca, CLOCK_MONOTONIC);
    pthread_cond_init(&QC, &ca);
    pthread_condattr_destroy(&ca);
#endif
}
static int q_timedwait(unsigned long long deadlineNs)
{
#ifdef __APPLE__
    const unsigned long long t = now_ns();
    struct timespec rel;
    if (t >= deadlineNs) return 1;
    rel.tv_sec = (time_t)((deadlineNs - t) / 1000000000ull);
    rel.tv_nsec = (long)((deadlineNs - t) % 1000000000ull);
    return pthread_cond_timedwait_relative_np(&QC, &Q, &rel) == ETIMEDOUT && now_ns() >= deadlineNs;
#else
    const struct timespec ts = { (time_t)(deadlineNs / 1000000000ull), (long)(deadlineNs % 1000000000ull) };
    return pthread_cond_timedwait(&QC, &Q, &ts) == ETIMEDOUT;
#endif
}

/* nvrm_fence_kick: the generation first, then "does anybody sleep", then the wakeup */
static void k_kick(void) { atomic_fetch_add(&gKick, 1); if (atomic_load(&gSleepers) > 0) q_wakeup(); }

static void k_lock(void *c) { (void)c; pthread_mutex_lock(&L); }
/* the unlock of nvrm_fence.cpp: a hold ended or was flagged under the lock -> the change generation, then the wakeup */
static void k_unlock(void *c)
{
    const int changed = Tb.changes != changesSeen;
    (void)c;
    changesSeen = Tb.changes;
    pthread_mutex_unlock(&L);
    if (changed) { atomic_fetch_add(&gGen, 1); if (atomic_load(&gSleepers) > 0) q_wakeup(); }
}
static unsigned long long k_now(void *c) { (void)c; return now_ns(); }
static unsigned k_kickgen(void *c) { (void)c; return atomic_load(&gKick); }
static unsigned k_gen(void *c) { (void)c; return atomic_load(&gGen); }
static int k_read(void *c, unsigned ci, unsigned long long *done, unsigned long long *flushed, int *faulted)
{
    (void)c;
    *done = atomic_load(&chDone[ci]);
    if (flushed) *flushed = atomic_load(&chFlushed[ci]);
    *faulted = atomic_load(&chFaulted[ci]);
    return 1;
}

/* A thread that calls into the fence. `variant` builds its sleep wrongly on purpose (the controls); `hook` lets a test
   make something happen at an exact place inside the sleep. */
enum { V_OK = 0, V_NO_RECHECK, V_ANNOUNCE_LATE };
enum { H_BEFORE_ANNOUNCE = 1, H_AFTER_RECHECK };
struct actor {
    atomic_int abort;                 /* the thread was interrupted (a signal): an abortable sleep ends */
    atomic_ulong sleeps;              /* how often it went to sleep */
    int variant;
    void (*hook)(struct actor *a, int where);
    unsigned long timedOut, lost;     /* sleeps the deadline ended; of those, ones that began with a generation already moved */
};

static int announce(int abortable)
{
    const int before = atomic_fetch_add(&gSleepers, 1);
    int peak = atomic_load(&gSleepersPeak);
    if (abortable && before >= gSleepersMax) { atomic_fetch_sub(&gSleepers, 1); return 0; }
    while (before + 1 > peak && !atomic_compare_exchange_weak(&gSleepersPeak, &peak, before + 1)) { }
    return 1;
}

/* The sleep, in the kernel's order: announce, queue, read both generations again, block. */
static int k_sleep(void *c, unsigned k, unsigned g, unsigned long long deadlineNs, int abortable)
{
    struct actor *a = (struct actor *)c;
    unsigned long epoch;
    int announced = 0, moved, timedOut = 0, r = NVF_SLEPT;

    if (a->hook) a->hook(a, H_BEFORE_ANNOUNCE);
    if (a->variant != V_ANNOUNCE_LATE) { if (!announce(abortable)) return NVF_SLEEP_FULL; announced = 1; }
    pthread_mutex_lock(&Q); epoch = qEpoch; pthread_mutex_unlock(&Q);                          /* assert_wait */
    if (a->variant != V_NO_RECHECK && (atomic_load(&gKick) != k || atomic_load(&gGen) != g)) q_wakeup();
    if (a->hook) a->hook(a, H_AFTER_RECHECK);
    if (!announced && !announce(abortable)) return NVF_SLEEP_FULL;
    moved = atomic_load(&gKick) != k || atomic_load(&gGen) != g;       /* the test's own note; the kernel has no such line */
    atomic_fetch_add(&a->sleeps, 1);
    pthread_mutex_lock(&Q);                                                                     /* thread_block */
    for (;;) {
        if (abortable && atomic_load(&a->abort)) { r = NVF_SLEEP_ABORTED; break; }
        if (qEpoch != epoch) break;
        if (q_timedwait(deadlineNs)) { timedOut = 1; break; }
    }
    pthread_mutex_unlock(&Q);
    if (timedOut) { a->timedOut++; if (moved) a->lost++; }
    atomic_fetch_sub(&gSleepers, 1);
    return r;
}
static void k_abort(struct actor *a)
{
    atomic_store(&a->abort, 1);
    pthread_mutex_lock(&Q); pthread_cond_broadcast(&QC); pthread_mutex_unlock(&Q);
}
static const struct nvf_wait_ops KOPS = { k_lock, k_unlock, k_now, k_kickgen, k_gen, k_sleep, k_read };

static void world_reset(void)
{
    for (unsigned i = 0; i < NVF_CHANS; i++) atomic_store(&chReadyAt[i], 0);
    if (atomic_load(&gpuRunning)) {                   /* no interrupt of the last scene may arrive in the next one */
        const unsigned long scans = atomic_load(&gpuScans);
        while (atomic_load(&gpuScans) < scans + 2) pause_us(50);
        while (atomic_load(&bhPending)) pause_us(50);
    }
    nvf_init(&Tb); changesSeen = 0;
    atomic_store(&gKick, 0); atomic_store(&gGen, 0); atomic_store(&gSleepers, 0); atomic_store(&gSleepersPeak, 0);
    gSleepersMax = 256;
    for (unsigned i = 0; i < NVF_CHANS; i++) {
        atomic_store(&chDone[i], 0); atomic_store(&chFlushed[i], 0); atomic_store(&chTarget[i], 0);
        atomic_store(&chReadyAt[i], 0); atomic_store(&chFaulted[i], 0); atomic_store(&chIrq[i], 1);
    }
}
/* CHAN_ADD + CHAN_VERIFY for a new channel; 0 = refused */
static unsigned chan_new(unsigned long long owner, unsigned pid)
{
    unsigned h = 0, fi = 0;
    k_lock(NULL);
    if (nvf_chan_add(&Tb, owner, pid, 0, &h) == OK) {
        atomic_store(&chDone[IDX(h)], 0); atomic_store(&chFlushed[IDX(h)], 0); atomic_store(&chTarget[IDX(h)], 0);
        atomic_store(&chReadyAt[IDX(h)], 0); atomic_store(&chFaulted[IDX(h)], 0);
        if (nvf_chan_verify(&Tb, owner, h, 1, &fi) != OK) h = 0;
    }
    k_unlock(NULL);
    return h;
}
static unsigned open1(unsigned long long owner, unsigned pid, unsigned long long cookie, unsigned sid, unsigned flags)
{
    unsigned st;
    k_lock(NULL); st = nvf_open(&Tb, owner, pid, cookie, 1, &sid, NULL, flags, now_ns()); k_unlock(NULL);
    return st;
}
static unsigned arm1(unsigned long long owner, unsigned long long cookie, unsigned sid, unsigned h, unsigned long long seq)
{
    unsigned st;
    k_lock(NULL); st = nvf_arm(&Tb, owner, cookie, sid, h, seq, 0, now_ns(), k_read, NULL); k_unlock(NULL);
    return st;
}
static unsigned close1(unsigned long long owner, unsigned long long cookie, unsigned sid)
{
    unsigned found = 0;
    k_lock(NULL); (void)nvf_close(&Tb, owner, cookie, sid, k_read, NULL, &found); k_unlock(NULL);
    return found;
}
static void owner_gone(unsigned long long owner)
{
    unsigned fi[NVF_OWNER_CHANS];
    k_lock(NULL);
    while (nvf_owner_gone(&Tb, owner, fi, NVF_OWNER_CHANS) == NVF_OWNER_CHANS) { }
    k_unlock(NULL);
}
static unsigned long long waits_ended(void)
{
    return Tb.stat[NVF_STAT_WAIT_READY] + Tb.stat[NVF_STAT_WAIT_DEAD] + Tb.stat[NVF_STAT_WAIT_TIMEOUT] +
           Tb.stat[NVF_STAT_WAIT_BUDGET] + Tb.stat[NVF_STAT_WAIT_ABORTED] + Tb.stat[NVF_STAT_WAIT_FULL];
}

/* ==== Part B: one reader, one interrupt, at the two instants that matter ============================================ */

static unsigned hookChan, hookAt, hookFired;
static void hook_complete(struct actor *a, int where)      /* the GPU finishes and its interrupt arrives, right here */
{
    (void)a;
    if (where != (int)hookAt || hookFired) return;
    hookFired = 1;
    atomic_store(&chDone[hookChan], 5);
    k_kick();
}
/* A reader waits for a compositor hold armed on value 5; the completion and its interrupt land at `where` inside the
   reader's first sleep. *a is the reader, for the caller to judge. */
static void one_reader(struct actor *a, int variant, unsigned where, struct NVRMFenceWait *w, unsigned *status)
{
    struct nvf_query q = { 7, 0, 0, 0, 0, 0 };
    unsigned h;
    memset(a, 0, sizeof(*a));
    world_reset();
    h = chan_new(1, 11);
    (void)open1(1, 11, 1, 7, COMP | AUTO); (void)arm1(1, 1, 7, h, 5);
    a->variant = variant; a->hook = hook_complete;
    hookChan = IDX(h); hookAt = where; hookFired = 0;
    *status = nvf_wait(&Tb, &KOPS, a, &q, 2000, 0, 1, w);
}

static void part_b(void)
{
    struct NVRMFenceWait w;
    struct actor a;
    unsigned st;

    one_reader(&a, V_OK, H_BEFORE_ANNOUNCE, &w, &st);
    T("an interrupt between the reader's look and its sleep (nobody is counted as sleeping yet): the sleep does not block",
      st == OK && w.result == NVRM_FENCE_R_READY && hookFired && a.timedOut == 0 && a.lost == 0 && atomic_load(&a.sleeps) == 1);
    T("and the wake is counted as an interrupt's", Tb.stat[NVF_STAT_WAKE_IRQ] == 1 && Tb.stat[NVF_STAT_DONE_ON_IRQ] == 1 &&
      Tb.stat[NVF_STAT_WAIT_READY] == 1 && Tb.live == 0);
    one_reader(&a, V_OK, H_AFTER_RECHECK, &w, &st);
    T("an interrupt after the reader read the generations again, before it blocks: the kick's wakeup ends the sleep",
      st == OK && w.result == NVRM_FENCE_R_READY && hookFired && a.timedOut == 0 && a.lost == 0);
    T("no thread is left counted as a sleeper", atomic_load(&gSleepers) == 0);

    one_reader(&a, V_NO_RECHECK, H_BEFORE_ANNOUNCE, &w, &st);
    T("control: without the second read the first case sleeps through the interrupt until its slice ends",
      st == OK && w.result == NVRM_FENCE_R_READY && a.timedOut == 1 && a.lost == 1);
    one_reader(&a, V_ANNOUNCE_LATE, H_AFTER_RECHECK, &w, &st);
    T("control: counted as a sleeper too late, the second case does too",
      st == OK && w.result == NVRM_FENCE_R_READY && a.timedOut == 1 && a.lost == 1);
}

/* ==== Part C: threads ============================================================================================== */

/* The GPU: finishes the work a producer submitted when its time has come, stores the completion value and raises the
   interrupt. The interrupt is two kicks, from two threads, as in the driver (interrupt path, then the bottom half). */
static void *gpu_main(void *arg)
{
    (void)arg;
    while (!atomic_load(&gpuStop)) {
        const unsigned long long t = now_ns();
        for (unsigned ci = 0; ci < GPU_CHANS; ci++) {
            unsigned long long at = atomic_load(&chReadyAt[ci]);
            if (!at || t < at || atomic_load(&chFaulted[ci])) continue;      /* a faulted channel completes nothing */
            atomic_store(&chDone[ci], atomic_load(&chTarget[ci]));
            (void)atomic_compare_exchange_strong(&chReadyAt[ci], &at, 0);     /* stays set if more was submitted meanwhile */
            if (atomic_load(&chIrq[ci])) { k_kick(); atomic_fetch_add(&bhPending, 1); }
        }
        atomic_fetch_add(&gpuScans, 1);
        pause_us(40);
    }
    return NULL;
}
static void *bh_main(void *arg)
{
    (void)arg;
    while (!atomic_load(&gpuStop)) {
        if (atomic_load(&bhPending)) { k_kick(); atomic_store(&bhPending, 0); } else pause_us(30);
    }
    return NULL;
}

struct producer {
    pthread_t th;
    struct actor a;
    unsigned long long owner, cookie, seq;
    unsigned pid, handle, flags;          /* flags: COMP | AUTO for the compositor's kind, 0 for an application's */
    unsigned sid[4], nsid, iters, workUs;
    atomic_int stop;
    unsigned long opened, refused, seqReady, seqBudget, seqPending, seqDead, rebuilt;
};
/* The producer noticed its channel's error (as NVK does) and rebuilt it: a clean notifier, and the kernel is told. */
static void rebuild(struct producer *p)
{
    unsigned idx = 0;
    atomic_store(&chFaulted[IDX(p->handle)], 0);
    k_lock(NULL); (void)nvf_chan_rebind(&Tb, p->owner, p->handle, &idx); k_unlock(NULL);
    p->rebuilt++;
}
static void *producer_main(void *arg)
{
    struct producer *p = (struct producer *)arg;
    const unsigned ci = IDX(p->handle);
    for (unsigned i = 0; (!p->iters || i < p->iters) && !atomic_load(&p->stop); i++) {
        const unsigned sid = p->sid[i % p->nsid];
        const unsigned long long cookie = ++p->cookie, seq = ++p->seq;
        if (open1(p->owner, p->pid, cookie, sid, p->flags) != OK) { p->refused++; p->seq--; pause_us(200); continue; }
        p->opened++;
        atomic_store(&chFlushed[ci], seq); atomic_store(&chTarget[ci], seq);       /* the flush: work for the GPU */
        atomic_store(&chReadyAt[ci], now_ns() + (unsigned long long)p->workUs * 1000ull);
        (void)arm1(p->owner, cookie, sid, p->handle, seq);
        while (atomic_load(&chDone[ci]) < seq && !atomic_load(&p->stop)) {
            struct NVRMFenceWait w;
            if (atomic_load(&chFaulted[ci])) { rebuild(p); continue; }
            if (p->flags & AUTO) { pause_us(50); continue; }                         /* the compositor does not wait on its fence here */
            (void)nvf_wait_seq(&Tb, &KOPS, &p->a, p->owner, p->handle, seq, 100, 1, &w);
            if (w.result == NVRM_FENCE_R_READY) { p->seqReady++; break; }
            if (w.result == NVRM_FENCE_R_BUDGET) p->seqBudget++;
            else if (w.result == NVRM_FENCE_R_PENDING) { p->seqPending++; pause_us(100); }   /* the kernel will not sleep on it: poll */
            else if (w.result == NVRM_FENCE_R_DEAD) { p->seqDead++; pause_us(50); }
        }
        (void)close1(p->owner, cookie, sid);        /* the application publishes; the compositor's late CLOSE finds nothing */
    }
    return NULL;
}

struct reader {
    pthread_t th;
    struct actor a;
    unsigned sid[6], nsid, flags, timeoutMs, iters, pauseUs, seed;
    atomic_int stop, finished;
    unsigned long res[8], full, badStatus;
    unsigned long long maxWaitedUs, minWaitedUs, maxOverUs;
};
static void *reader_main(void *arg)
{
    struct reader *r = (struct reader *)arg;
    r->minWaitedUs = ~0ull;
    for (unsigned i = 0; (!r->iters || i < r->iters) && !atomic_load(&r->stop); i++) {
        struct nvf_query q = { 0, 0, 0, 0, 0, 0 };
        struct NVRMFenceWait w;
        unsigned st;
        r->seed = r->seed * 1103515245u + 12345u;
        q.sid = r->sid[(r->seed >> 16) % r->nsid]; q.flags = r->flags;
        st = nvf_wait(&Tb, &KOPS, &r->a, &q, r->timeoutMs, 0, 1, &w);
        if (st == NVRM_FENCE_ST_FULL) r->full++; else if (st != OK) r->badStatus++;
        if (w.result < 8) r->res[w.result]++;
        if (w.result != NVRM_FENCE_R_NONE) {
            if (w.waitedUs > r->maxWaitedUs) r->maxWaitedUs = w.waitedUs;
            if (w.waitedUs < r->minWaitedUs) r->minWaitedUs = w.waitedUs;
            if (w.waitedUs > r->timeoutMs * 1000ull && w.waitedUs - r->timeoutMs * 1000ull > r->maxOverUs) r->maxOverUs = w.waitedUs - r->timeoutMs * 1000ull;
        }
        if (w.result == NVRM_FENCE_R_ABORTED) break;
        if (r->pauseUs) pause_us(r->pauseUs);
    }
    atomic_store(&r->finished, 1);
    return NULL;
}

#define NR 8
#define NP 4
static struct reader R[NR];
static struct producer P[NP];
/* n readers of surface sidA; with sidB every second one reads that instead */
static void readers_start(unsigned n, unsigned sidA, unsigned sidB, unsigned flags, unsigned timeoutMs, unsigned iters, unsigned pauseUs)
{
    memset(R, 0, sizeof(R));
    for (unsigned i = 0; i < n; i++) {
        R[i].sid[0] = (sidB && (i & 1)) ? sidB : sidA; R[i].nsid = 1; R[i].flags = flags; R[i].timeoutMs = timeoutMs; R[i].iters = iters;
        R[i].pauseUs = pauseUs; R[i].seed = 77 + i;
        pthread_create(&R[i].th, NULL, reader_main, &R[i]);
    }
}
static void readers_join(unsigned n) { for (unsigned i = 0; i < n; i++) pthread_join(R[i].th, NULL); }
static unsigned long readers_sum(unsigned n, unsigned result) { unsigned long s = 0; for (unsigned i = 0; i < n; i++) s += R[i].res[result]; return s; }
static unsigned long long readers_max(unsigned n) { unsigned long long m = 0; for (unsigned i = 0; i < n; i++) if (R[i].maxWaitedUs > m) m = R[i].maxWaitedUs; return m; }
static unsigned long long readers_min(unsigned n) { unsigned long long m = ~0ull; for (unsigned i = 0; i < n; i++) if (R[i].minWaitedUs < m) m = R[i].minWaitedUs; return m; }
/* waits (bounded) until every one of the first n readers has gone to sleep at least `times` times */
static int readers_asleep(unsigned n, unsigned long times)
{
    const unsigned long long until = now_ns() + 5000000000ull;
    for (;;) {
        unsigned ok = 0;
        for (unsigned i = 0; i < n; i++) if (atomic_load(&R[i].a.sleeps) >= times || atomic_load(&R[i].finished)) ok++;
        if (ok == n) return 1;
        if (now_ns() > until) return 0;
        pause_us(100);
    }
}
static void producer_start(unsigned i, unsigned long long owner, unsigned pid, unsigned flags, unsigned sidA, unsigned sidB,
                           unsigned iters, unsigned workUs)
{
    memset(&P[i], 0, sizeof(P[i]));
    P[i].owner = owner; P[i].pid = pid; P[i].flags = flags; P[i].sid[0] = sidA; P[i].sid[1] = sidB; P[i].nsid = sidB ? 2 : 1;
    P[i].iters = iters; P[i].workUs = workUs;
    P[i].handle = chan_new(owner, pid);
    pthread_create(&P[i].th, NULL, producer_main, &P[i]);
}

static void scen_interrupts(void)
{
    unsigned long ready, bad;
    world_reset();
    producer_start(0, 1, 11, COMP | AUTO, 7, 0, 400, 300);
    readers_start(3, 7, 0, 0, 2000, 0, 20);
    pthread_join(P[0].th, NULL);
    for (unsigned i = 0; i < 3; i++) atomic_store(&R[i].stop, 1);
    readers_join(3);
    ready = readers_sum(3, NVRM_FENCE_R_READY);
    bad = readers_sum(3, NVRM_FENCE_R_TIMEOUT) + readers_sum(3, NVRM_FENCE_R_DEAD) + readers_sum(3, NVRM_FENCE_R_BUDGET) + readers_sum(3, NVRM_FENCE_R_ABORTED);
    printf("     400 frames, GPU work 300 us, interrupts delivered: %lu waits ended READY; completions found on an interrupt %llu,\n"
           "     on a slice %llu; longest wait %llu us\n", ready, Tb.stat[NVF_STAT_DONE_ON_IRQ], Tb.stat[NVF_STAT_DONE_ON_SLICE], readers_max(3));
    T("interrupts delivered: readers that waited got READY, never TIMEOUT, DEAD or BUDGET", ready > 100 && bad == 0);
    T("the interrupt is what woke them: ten times more completions found on an interrupt than on a slice",
      Tb.stat[NVF_STAT_DONE_ON_IRQ] > 50 && Tb.stat[NVF_STAT_DONE_ON_SLICE] * 10 <= Tb.stat[NVF_STAT_DONE_ON_IRQ]);
    T("the channel's interrupts are trusted", nvf_chan_trusted(&Tb.c[IDX(P[0].handle)]) && Tb.stat[NVF_STAT_CHAN_NOIRQ] == 0);
    T("nobody was struck, nothing is left in the table", Tb.stat[NVF_STAT_STRIKES] == 0 && Tb.live == 0 && Tb.used == 0 && atomic_load(&gSleepers) == 0);
    T("every wait is accounted for once", Tb.stat[NVF_STAT_WAITS] == waits_ended() && Tb.stat[NVF_STAT_WAIT_READY] == ready);
    T("no wait lasted much longer than the GPU work", readers_max(3) < 300 + SLACK_US);
}

static void scen_no_interrupts(void)
{
    struct NVRMFenceWait w;
    struct actor a;
    unsigned long ready, bad;
    unsigned st;
    world_reset();
    atomic_store(&chIrq[0], 0);                       /* the first channel's completions raise no interrupt */
    producer_start(0, 1, 11, COMP | AUTO, 7, 0, 150, 300);
    readers_start(3, 7, 0, 0, 2000, 0, 20);
    pthread_join(P[0].th, NULL);
    for (unsigned i = 0; i < 3; i++) atomic_store(&R[i].stop, 1);
    readers_join(3);
    ready = readers_sum(3, NVRM_FENCE_R_READY);
    bad = readers_sum(3, NVRM_FENCE_R_TIMEOUT) + readers_sum(3, NVRM_FENCE_R_DEAD) + readers_sum(3, NVRM_FENCE_R_BUDGET);
    printf("     150 frames, no interrupt: %lu waits ended READY; completions found on an interrupt %llu, on a slice %llu;\n"
           "     longest wait %llu us\n", ready, Tb.stat[NVF_STAT_DONE_ON_IRQ], Tb.stat[NVF_STAT_DONE_ON_SLICE], readers_max(3));
    T("no interrupt at all: the slice still finds every completion (READY, never TIMEOUT)", IDX(P[0].handle) == 0 && ready > 30 && bad == 0);
    T("found on slices, not on interrupts", Tb.stat[NVF_STAT_DONE_ON_IRQ] == 0 && Tb.stat[NVF_STAT_DONE_ON_SLICE] > 20 && atomic_load(&gKick) == 0);
    T("control: so the comparison of the run with interrupts does fail without them",
      !(Tb.stat[NVF_STAT_DONE_ON_IRQ] > 50 && Tb.stat[NVF_STAT_DONE_ON_SLICE] * 10 <= Tb.stat[NVF_STAT_DONE_ON_IRQ]));
    T("the channel is judged \"no interrupt\"", Tb.c[0].noirq == 1 && Tb.stat[NVF_STAT_CHAN_NOIRQ] == 1 && !nvf_chan_trusted(&Tb.c[0]));
    T("and was looked at in short slices throughout: no wait much longer than the GPU work", readers_max(3) < 300 + SLACK_US);
    memset(&a, 0, sizeof(a));
    st = nvf_wait_seq(&Tb, &KOPS, &a, 1, P[0].handle, atomic_load(&chDone[0]) + 1, 50, 1, &w);
    T("a producer asking the kernel to sleep on that channel is told to poll (PENDING, no sleep)", st == OK && w.result == NVRM_FENCE_R_PENDING &&
      atomic_load(&a.sleeps) == 0 && Tb.stat[NVF_STAT_SEQ_REFUSED] == 1);
    T("nothing is left in the table", Tb.live == 0 && Tb.used == 0 && atomic_load(&gSleepers) == 0);
}

static void scen_application(void)
{
    unsigned long ready, bad;
    world_reset();
    producer_start(0, 1, 11, 0, 7, 8, 300, 300);      /* an application: waits for its own fence, then publishes */
    readers_start(4, 7, 8, 0, 2000, 0, 20);
    pthread_join(P[0].th, NULL);
    for (unsigned i = 0; i < 4; i++) atomic_store(&R[i].stop, 1);
    readers_join(4);
    ready = readers_sum(4, NVRM_FENCE_R_READY);
    bad = readers_sum(4, NVRM_FENCE_R_TIMEOUT) + readers_sum(4, NVRM_FENCE_R_DEAD) + readers_sum(4, NVRM_FENCE_R_BUDGET);
    printf("     300 frames of an application: its own fence waits ended READY %lu, BUDGET %lu, told to poll %lu, DEAD %lu;\n"
           "     readers READY %lu; woken by a table change %llu\n", P[0].seqReady, P[0].seqBudget, P[0].seqPending, P[0].seqDead,
           ready, Tb.stat[NVF_STAT_DONE_ON_STATE]);
    T("an application's holds end at its CLOSE: its readers got READY, never TIMEOUT, DEAD or BUDGET", ready > 50 && bad == 0);
    T("the producer slept in the kernel for its own fence and was woken (WAIT_SEQ READY)", P[0].seqReady > 200 && P[0].seqDead == 0 &&
      Tb.stat[NVF_STAT_SEQ_SLEPT] > 100);
    T("the CLOSE woke readers that were asleep", Tb.stat[NVF_STAT_DONE_ON_STATE] > 20 && Tb.stat[NVF_STAT_WAKE_STATE] > 20);
    T("the kernel's view of the completion value agreed at every CLOSE", Tb.stat[NVF_STAT_CLOSE_GPU_BEHIND] == 0);
    T("every wait is accounted for once, nothing is left", Tb.stat[NVF_STAT_WAITS] == waits_ended() && Tb.live == 0 && Tb.used == 0 &&
      Tb.stat[NVF_STAT_STRIKES] == 0 && atomic_load(&gSleepers) == 0);
}

/* a hold on surface 7 that the GPU never completes: armed on a value nobody submits */
static unsigned stuck_hold(unsigned flags, int armed)
{
    unsigned h;
    world_reset();
    h = chan_new(1, 11);
    (void)open1(1, 11, 1, 7, flags);
    if (armed) (void)arm1(1, 1, 7, h, 1000);
    return h;
}

static void scen_death(void)
{
    (void)stuck_hold(0, 1);
    readers_start(3, 7, 0, 0, 2000, 1, 0);
    T("three readers sleep on a hold whose GPU work never ends", readers_asleep(3, 1) && Tb.stat[NVF_STAT_WAITS] == 3);
    owner_gone(1);                                    /* the writer's connection closes (the process died) */
    readers_join(3);
    T("the writer dies: all three are woken with DEAD, long before any bound", readers_sum(3, NVRM_FENCE_R_DEAD) == 3 && readers_max(3) < 1500000);
    T("its holds and channels are gone", Tb.live == 0 && Tb.used == 0 && Tb.stat[NVF_STAT_CHANS] == 0 && Tb.stat[NVF_STAT_END_OWNER] == 1 &&
      nvf_owner_find(&Tb, 1) < 0 && atomic_load(&gSleepers) == 0);

    (void)stuck_hold(0, 1);
    readers_start(3, 7, 0, 0, 2000, 1, 0);
    (void)readers_asleep(3, 1);
    k_lock(NULL); Tb.paused = 1; nvf_end_all(&Tb, NVF_END_PM); k_unlock(NULL);     /* the machine goes to sleep */
    readers_join(3);
    T("the GPU is put to sleep: sleeping readers are woken with DEAD", readers_sum(3, NVRM_FENCE_R_DEAD) == 3 && readers_max(3) < 1500000 &&
      Tb.stat[NVF_STAT_END_PM] == 1 && Tb.live == 0);
    T("and holds are refused until it is back", open1(1, 11, 2, 7, 0) == NVRM_FENCE_ST_OFF);

    (void)stuck_hold(0, 1);
    readers_start(2, 7, 0, 0, 2000, 1, 0);
    (void)readers_asleep(2, 1);
    k_abort(&R[0].a);                                 /* a signal for the first reader only */
    pthread_join(R[0].th, NULL);
    T("a reader that is interrupted leaves with ABORTED; the hold is untouched", R[0].res[NVRM_FENCE_R_ABORTED] == 1 && Tb.live == 1 &&
      Tb.stat[NVF_STAT_WAIT_ABORTED] == 1);
    T("the other one sleeps on", !atomic_load(&R[1].finished));
    (void)close1(1, 1, 7);
    pthread_join(R[1].th, NULL);
    T("until the writer publishes: READY", R[1].res[NVRM_FENCE_R_READY] == 1 && atomic_load(&gSleepers) == 0);
}

static void scen_bounds(void)
{
    (void)stuck_hold(0, 1);
    readers_start(2, 7, 0, 0, 30, 1, 0);
    readers_join(2);
    T("a 30 ms budget against a hold that stays: BUDGET, not before 30 ms and not long after", readers_sum(2, NVRM_FENCE_R_BUDGET) == 2 &&
      readers_min(2) >= 30000 && readers_max(2) <= 30000 + SLACK_US);
    T("the hold was not touched and nobody was struck", Tb.live == 1 && Tb.flagged == 0 && Tb.stat[NVF_STAT_STRIKES] == 0);

    (void)stuck_hold(0, 0);                           /* never armed: its writer's CPU is all it waits for */
    readers_start(2, 7, 0, 0, 2000, 1, 0);
    readers_join(2);
    T("a hold that is never armed is given up 100 ms after it was opened: both readers get TIMEOUT", readers_sum(2, NVRM_FENCE_R_TIMEOUT) == 2 &&
      readers_max(2) <= 100000 + SLACK_US && readers_min(2) >= 10000);
    T("given up once, one strike, and it keeps its slot until its CLOSE", Tb.stat[NVF_STAT_EXPIRED_CPU] == 1 && Tb.stat[NVF_STAT_STRIKES] == 1 &&
      Tb.live == 0 && Tb.flagged == 1 && Tb.used == 1 && close1(1, 1, 7) == 1 && Tb.used == 0);

    (void)stuck_hold(COMP, 1);
    gSleepersMax = 3;                                 /* the ceiling on sleepers, small enough to reach */
    readers_start(6, 7, 0, 0, 2000, 1, 0);
    {
        const unsigned long long until = now_ns() + 5000000000ull;
        unsigned fin;
        do { fin = 0; for (unsigned i = 0; i < 6; i++) fin += (unsigned)atomic_load(&R[i].finished); if (fin < 3) pause_us(200); } while (fin < 3 && now_ns() < until);
        T("six readers, room for three sleepers: at least three are refused at once", fin >= 3);
    }
    (void)close1(1, 1, 7);
    readers_join(6);
    {
        unsigned long full = 0, pend = readers_sum(6, NVRM_FENCE_R_PENDING), ready = readers_sum(6, NVRM_FENCE_R_READY);
        for (unsigned i = 0; i < 6; i++) full += R[i].full;
        T("the refused ones were told FULL with PENDING, the others READY when the hold ended", full >= 3 && pend == full && ready == 6 - full &&
          Tb.stat[NVF_STAT_WAIT_FULL] == full);
        T("never more than three slept at a time", atomic_load(&gSleepersPeak) <= 3 && atomic_load(&gSleepers) == 0);
        T("every wait is accounted for once", Tb.stat[NVF_STAT_WAITS] == waits_ended());
    }
}

static void scen_mixed(void)
{
    static const unsigned sids[6] = { 21, 22, 23, 24, 25, 26 };
    unsigned long long over = 0, opened = 0;
    unsigned long res[8] = { 0 }, full = 0, bad = 0, rebuilt = 0;
    world_reset();
    producer_start(0, 1, 11, COMP | AUTO, 21, 22, 0, 200);
    producer_start(1, 2, 12, COMP | AUTO, 23, 22, 0, 400);
    producer_start(2, 3, 13, 0, 24, 25, 0, 300);
    producer_start(3, 4, 14, 0, 26, 25, 0, 500);
    memset(R, 0, sizeof(R));
    for (unsigned i = 0; i < NR; i++) {
        memcpy(R[i].sid, sids, sizeof(sids)); R[i].nsid = 6; R[i].seed = 1000 + i;
        R[i].flags = i % 4 == 3 ? COMP : 0; R[i].timeoutMs = i % 3 == 0 ? 2000 : i % 3 == 1 ? 100 : 2; R[i].pauseUs = 10 + 10 * i;
        pthread_create(&R[i].th, NULL, reader_main, &R[i]);
    }
    pause_us(250000);
    atomic_store(&chFaulted[IDX(P[2].handle)], 1);    /* a channel faults: its producer notices and rebuilds it */
    pause_us(150000);
    atomic_store(&chFaulted[IDX(P[0].handle)], 1);
    pause_us(150000);
    atomic_store(&P[1].stop, 1); pthread_join(P[1].th, NULL);          /* a connection goes away and a new one takes its place */
    opened += P[1].opened;
    owner_gone(2);
    producer_start(1, 9, 12, COMP | AUTO, 23, 22, 0, 400);
    pause_us(450000);
    for (unsigned i = 0; i < NP; i++) atomic_store(&P[i].stop, 1);
    for (unsigned i = 0; i < NP; i++) { pthread_join(P[i].th, NULL); opened += P[i].opened; rebuilt += P[i].rebuilt; }
    for (unsigned i = 0; i < NR; i++) atomic_store(&R[i].stop, 1);
    for (unsigned i = 0; i < NR; i++) {
        pthread_join(R[i].th, NULL);
        for (unsigned k = 0; k < 8; k++) res[k] += R[i].res[k];
        full += R[i].full; bad += R[i].badStatus;
        if (R[i].maxOverUs > over) over = R[i].maxOverUs;
    }
    printf("     1 s of four producers and eight readers on six surfaces: %llu holds opened, %lu channel rebuilds; waits NONE %lu,\n"
           "     READY %lu, DEAD %lu, TIMEOUT %lu, BUDGET %lu; latest return %llu us past its budget; strikes %llu\n",
           opened, rebuilt, res[NVRM_FENCE_R_NONE], res[NVRM_FENCE_R_READY], res[NVRM_FENCE_R_DEAD], res[NVRM_FENCE_R_TIMEOUT],
           res[NVRM_FENCE_R_BUDGET], over, Tb.stat[NVF_STAT_STRIKES]);
    T("a mixed load ran: holds by the thousand, waits that slept, both faults rebuilt", opened > 1000 && res[NVRM_FENCE_R_READY] > 100 && rebuilt >= 2 &&
      Tb.stat[NVF_STAT_CHAN_REBINDS] == rebuilt);
    T("no wait was refused or answered with anything unknown", full == 0 && bad == 0 && res[NVRM_FENCE_R_PENDING] == 0 && res[NVRM_FENCE_R_ABORTED] == 0 && res[7] == 0);
    T("no reader came back later than its budget plus what a busy machine may add", over <= SLACK_US);
    T("the table's count of each answer is the readers' own", Tb.stat[NVF_STAT_WAIT_NONE] == res[NVRM_FENCE_R_NONE] &&
      Tb.stat[NVF_STAT_WAIT_READY] == res[NVRM_FENCE_R_READY] && Tb.stat[NVF_STAT_WAIT_DEAD] == res[NVRM_FENCE_R_DEAD] &&
      Tb.stat[NVF_STAT_WAIT_TIMEOUT] == res[NVRM_FENCE_R_TIMEOUT] && Tb.stat[NVF_STAT_WAIT_BUDGET] == res[NVRM_FENCE_R_BUDGET] &&
      Tb.stat[NVF_STAT_WAITS] == waits_ended());
    T("every hold that was opened ended: closed, by the GPU, with its channel or with its connection",
      Tb.stat[NVF_STAT_OPENS] == opened && Tb.live == 0 && Tb.used == 0 && Tb.flagged == 0);
    owner_gone(1); owner_gone(3); owner_gone(4); owner_gone(9);
    T("after the last connection closed nothing is left: no channel, no record, no sleeper", Tb.stat[NVF_STAT_CHANS] == 0 && Tb.chanHigh == 0 &&
      nvf_owner_find(&Tb, 1) < 0 && nvf_owner_find(&Tb, 9) < 0 && atomic_load(&gSleepers) == 0 && !Tb.fused);
}

int main(void)
{
    pthread_t gpu, bh;
    alarm(240);                                       /* a hang is a failure, not a test that never ends */
    setvbuf(stdout, NULL, _IOLBF, 0);
    q_init();

    part_a();
    part_b();
    pthread_create(&gpu, NULL, gpu_main, NULL);
    pthread_create(&bh, NULL, bh_main, NULL);
    atomic_store(&gpuRunning, 1);
    scen_interrupts();
    scen_no_interrupts();
    scen_application();
    scen_death();
    scen_bounds();
    scen_mixed();
    atomic_store(&gpuStop, 1);
    pthread_join(gpu, NULL);
    pthread_join(bh, NULL);
    printf(fails ? "\n%d FAILED\n" : "\nall passed\n", fails);
    return fails ? 1 : 0;
}
