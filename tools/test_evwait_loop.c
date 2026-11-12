/* test_evwait_loop.c - nvrm_evw_round (kexts/NVRM/nvrm_evwait.h) against a stand-in for the kernel's selector 6, with real
   threads, and against a scripted world for the parts that need no clock. Proves: no wake is lost when events are
   delivered; a card whose events never arrive costs at most the 2 ms bound per sleep and ends in polling; a full kernel
   does not spin; a channel with no event goes through the same wait callback and can never switch the process to polling.
   Build and run anywhere:
     cc -std=c11 -O2 -pthread -Wall -Wextra -Werror -I kexts/NVRM tools/test_evwait_loop.c -o /tmp/test_evwait_loop && /tmp/test_evwait_loop */
#define _POSIX_C_SOURCE 200809L
#include <pthread.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include "nvrm_evwait.h"
static int fails;
#define T(name, cond) do { if (!(cond)) { printf("FAIL %s\n", name); fails++; } else printf("ok   %s\n", name); } while (0)
static uint64_t now_ns(void) { struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts); return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec; }

/* ---- part 1: a stand-in for the kernel, the GPU as a thread ---- */

/* the kernel: fEvtSeq, nv_post_event and sWaitEvent, with a mutex and a condition where XNU has assert_wait / thread_wakeup */
static pthread_mutex_t klk = PTHREAD_MUTEX_INITIALIZER; static pthread_cond_t kcv = PTHREAD_COND_INITIALIZER;
static uint32_t kseq; static int kdrop, kcapped, klegacy, kfail;
static uint32_t kmax_us;      /* the longest sleep any caller asked for */
static void k_post(void) { pthread_mutex_lock(&klk); kseq++; pthread_cond_broadcast(&kcv); pthread_mutex_unlock(&klk); }
static int k_wait(void *ctx, int fd, uint32_t seen, uint32_t us, uint64_t *out)
{
    (void)ctx; (void)fd;
    if (kfail) return -1;
    if (us > kmax_us) kmax_us = us;
    int slept = 0, wr = NVRM_EVW_WR_AWAKENED;
    pthread_mutex_lock(&klk);
    if (us && kseq == seen && kcapped) { *out = nvrm_evw_pack(kseq, NVRM_EVW_R_CAPPED); pthread_mutex_unlock(&klk); return 0; }
    if (us && kseq == seen) {
        struct timespec ts; clock_gettime(CLOCK_REALTIME, &ts);
        ts.tv_nsec += (long)us * 1000; while (ts.tv_nsec >= 1000000000) { ts.tv_nsec -= 1000000000; ts.tv_sec++; }
        slept = 1;
        while (kseq == seen) if (pthread_cond_timedwait(&kcv, &klk, &ts)) { wr = NVRM_EVW_WR_TIMED_OUT; break; }
    }
    const uint32_t now = kseq;
    pthread_mutex_unlock(&klk);
    *out = klegacy ? (uint64_t)now : nvrm_evw_pack(now, nvrm_evw_classify(slept, wr, seen, now));
    return 0;
}
/* the GPU: work `want` is complete once `done_seq` reaches it; completion posts an event unless kdrop */
static volatile uint64_t done_seq;
struct waiter { uint64_t want; unsigned stats[NVRM_EVW_S_N]; unsigned backoffs; };
static int w_pending(void *ctx, int *fd) { struct waiter *w = ctx; *fd = 3; return done_seq < w->want; }
static void w_backoff(void *ctx, uint64_t waited) { struct waiter *w = ctx; (void)waited; w->backoffs++; struct timespec ts = { 0, 20000 }; nanosleep(&ts, NULL); }
static void w_stat(void *ctx, int what) { struct waiter *w = ctx; w->stats[what]++; }
static const struct nvrm_evw_ops ops = { w_pending, k_wait, w_backoff, w_stat };
static volatile int fell_back;
static int gpu_delay_us, gpu_jobs;
static void *gpu_thread(void *arg)
{
    (void)arg;
    for (int i = 1; i <= gpu_jobs; i++) {
        struct timespec ts = { 0, gpu_delay_us * 1000L }; nanosleep(&ts, NULL);
        done_seq = (uint64_t)i;
        if (!kdrop) k_post();
    }
    return NULL;
}
/* run `jobs` waits in `mode`, each job taking delay_us on the GPU thread */
static void run(int mode, int jobs, int delay_us, struct waiter *w, struct nvrm_evw_thread *t)
{
    pthread_t g;
    memset(w, 0, sizeof *w); done_seq = 0; kseq = 0; gpu_jobs = jobs; gpu_delay_us = delay_us;
    pthread_create(&g, NULL, gpu_thread, NULL);
    for (int i = 1; i <= jobs; i++) {
        w->want = (uint64_t)i;
        const uint64_t t0 = now_ns();
        while (done_seq < w->want) nvrm_evw_round(t, mode, now_ns() - t0, UINT64_MAX, 1, &fell_back, &ops, w);
    }
    pthread_join(g, NULL);
}

/* ---- part 2: a scripted world: every call is counted and answers what the script says, no clock, no thread ---- */

struct script {
    int pending;                  /* what pending() answers */
    int fd, fd_second;            /* the event it names on the first call of a round, and on the later ones */
    int rc; unsigned reason;      /* what wait() answers */
    uint32_t count;               /* the event count wait() reports */
    unsigned pendings, waits, reads, noevent, backoffs, stats[NVRM_EVW_S_N];
    uint32_t last_seen, last_us, max_us; int last_fd;
};
static int s_pending(void *ctx, int *fd) { struct script *s = ctx; *fd = s->pendings++ ? s->fd_second : s->fd; return s->pending; }
static int s_wait(void *ctx, int fd, uint32_t seen, uint32_t us, uint64_t *out)
{
    struct script *s = ctx;
    s->waits++; s->last_fd = fd; s->last_seen = seen; s->last_us = us;
    if (us > s->max_us) s->max_us = us;
    if (fd < 0) s->noevent++;
    if (fd >= 0 && us == 0) { s->reads++; if (s->rc) return s->rc; *out = nvrm_evw_pack(s->count, NVRM_EVW_R_COUNT); return 0; }
    if (s->rc) return s->rc;
    *out = nvrm_evw_pack(s->count, s->reason);
    return 0;
}
static void s_backoff(void *ctx, uint64_t waited) { struct script *s = ctx; (void)waited; s->backoffs++; }
static void s_stat(void *ctx, int what) { struct script *s = ctx; s->stats[what]++; }
static const struct nvrm_evw_ops sops = { s_pending, s_wait, s_backoff, s_stat };
static unsigned s_stats(const struct script *s) { unsigned n = 0; for (int i = 0; i < NVRM_EVW_S_N; i++) n += s->stats[i]; return n; }
/* one round of a wait that has run `waited` ns; the script's counters start from zero */
static void round1(struct script *s, struct nvrm_evw_thread *t, int mode, uint64_t waited, volatile int *fb)
{
    s->pendings = s->waits = s->reads = s->noevent = s->backoffs = 0; s->max_us = 0; memset(s->stats, 0, sizeof s->stats);
    nvrm_evw_round(t, mode, waited, UINT64_MAX, 1, fb, &sops, s);
}
#define LONG_WAIT 5000000ull     /* 5 ms into a wait: every sleeping mode sleeps */

static void scripted(void)
{
    struct script s; struct nvrm_evw_thread t; volatile int fb = 0;

    /* the event protocol, call by call */
    memset(&s, 0, sizeof s); memset(&t, 0, sizeof t); t.seq_fd = -1;
    s.pending = 1; s.fd = s.fd_second = 3; s.count = 40; s.reason = NVRM_EVW_R_WOKEN;
    round1(&s, &t, NVRM_EVW_ADAPT, LONG_WAIT, &fb);
    T("first sleep on an event: the count is read, the work is checked again, then the sleep",
      s.reads == 1 && s.waits == 2 && s.pendings == 2 && s.last_seen == 40 && s.last_us == NVRM_EVW_MAX_SLEEP_US && s.backoffs == 0);
    s.count = 41;
    round1(&s, &t, NVRM_EVW_ADAPT, LONG_WAIT, &fb);
    T("the next sleep reuses the count the last call returned: one system call", s.reads == 0 && s.waits == 1 && s.last_seen == 40);
    s.fd = 3; s.fd_second = 4; nvrm_evw_forget(&t);
    round1(&s, &t, NVRM_EVW_ADAPT, LONG_WAIT, &fb);
    int have = 1; (void)nvrm_evw_seen(&t, 3, &have);
    T("the wait moved to another device between the two checks: no sleep on the old event", s.reads == 1 && s.waits == 1 && !have);
    int never = 1;
    memset(&t, 0, sizeof t); t.seq_fd = -1; s.fd = s.fd_second = 3;
    for (uint64_t w = 0; w < NVRM_EVW_YIELD_NS; w += 997) { round1(&s, &t, NVRM_EVW_ADAPT, w, &fb); if (s.waits || s.backoffs != 1) never = 0; }
    T("adaptive: a wait under 1 ms never enters the kernel and polls once per round", never);
    round1(&s, &t, NVRM_EVW_ADAPT, NVRM_EVW_YIELD_NS, &fb);
    T("adaptive: at 1 ms it does", s.waits == 2 && s.backoffs == 0);
    round1(&s, &t, NVRM_EVW_POLL, LONG_WAIT, &fb);
    T("NVRM_EVENT_WAIT=0: no call into the kernel at any wait length", s.waits == 0 && s.pendings == 0 && s.backoffs == 1);
    nvrm_evw_round(&t, NVRM_EVW_ADAPT, LONG_WAIT, UINT64_MAX, 0, &fb, &sops, &s);
    T("a wait with no GPU completion to wait for polls", s.waits == 0 && s.backoffs == 2);
    T("nothing so far gave up on the event", !fb);

    /* a channel with no event: pending() names fd -1 */
    memset(&s, 0, sizeof s); memset(&t, 0, sizeof t); t.seq_fd = -1; fb = 0;
    s.pending = 1; s.fd = s.fd_second = -1; s.rc = -1;
    round1(&s, &t, NVRM_EVW_ADAPT, LONG_WAIT, &fb);
    T("no event: the wait callback is asked, with fd -1, no count and the 2 ms bound",
      s.waits == 1 && s.noevent == 1 && s.last_fd == -1 && s.last_seen == 0 && s.last_us == NVRM_EVW_MAX_SLEEP_US && s.reads == 0);
    T("no event, the callback has no wait of its own: exactly one polling step, as 2.9", s.backoffs == 1 && s_stats(&s) == 0);
    unsigned polls = 0;
    for (int i = 0; i < 1000; i++) { round1(&s, &t, NVRM_EVW_EAGER, LONG_WAIT, &fb); polls += s.backoffs; }
    T("no event: a thousand such rounds never switch the process to polling", !fb && polls == 1000 && t.misses == 0);
    s.rc = 0; s.reason = NVRM_EVW_R_WOKEN;
    round1(&s, &t, NVRM_EVW_ADAPT, LONG_WAIT, &fb);
    T("no event, the callback slept until the work was complete: no polling step", s.waits == 1 && s.backoffs == 0 && s.stats[NVRM_EVW_S_CHAN_SLEEP] == 1 && s_stats(&s) == 1);
    s.reason = NVRM_EVW_R_TIMEOUT; unsigned sleeps = 0; polls = 0;
    for (int i = 0; i < 1000; i++) { s.pending = i % 2; round1(&s, &t, NVRM_EVW_ADAPT, LONG_WAIT, &fb); sleeps += s.stats[NVRM_EVW_S_CHAN_SLEEP]; polls += s.backoffs; }
    T("no event, the callback slept its time out, work complete or not: never a missed wake, never a fallback",
      !fb && t.misses == 0 && sleeps == 500 && polls == 0);
    s.pending = 1; int stepped = 1;
    static const unsigned others[] = { NVRM_EVW_R_LEGACY, NVRM_EVW_R_COUNT, NVRM_EVW_R_MOVED, NVRM_EVW_R_CAPPED, NVRM_EVW_R_ABORTED, NVRM_EVW_R_SPURIOUS, 99 };
    for (unsigned i = 0; i < sizeof others / sizeof others[0]; i++) {
        s.reason = others[i]; round1(&s, &t, NVRM_EVW_ADAPT, LONG_WAIT, &fb);
        if (s.backoffs != 1 || s_stats(&s) != 0) stepped = 0;
    }
    T("no event, any answer that is not a sleep: one polling step, so the loop cannot spin in the kernel", stepped && !fb);
    s.reason = NVRM_EVW_R_WOKEN;
    round1(&s, &t, NVRM_EVW_POLL, LONG_WAIT, &fb);
    T("no event, NVRM_EVENT_WAIT=0: the callback is not asked", s.waits == 0 && s.backoffs == 1);
    round1(&s, &t, NVRM_EVW_ADAPT, NVRM_EVW_SPIN_NS - 1, &fb);
    T("no event, inside the spin: the callback is not asked", s.waits == 0 && s.backoffs == 1);
    fb = 1;
    round1(&s, &t, NVRM_EVW_ADAPT, LONG_WAIT, &fb);
    T("no event, in a process that went back to polling: the callback is not asked", s.waits == 0 && s.backoffs == 1);
    fb = 0;
    /* the event's remembered count is not disturbed by a round on a channel without one */
    memset(&t, 0, sizeof t); t.seq_fd = -1; nvrm_evw_remember(&t, 3, 77);
    s.fd = s.fd_second = -1; s.reason = NVRM_EVW_R_TIMEOUT; s.count = 5;
    round1(&s, &t, NVRM_EVW_ADAPT, LONG_WAIT, &fb);
    (void)nvrm_evw_seen(&t, 3, &have);
    T("no event: the count remembered for a real event is kept", have && t.seq == 77);

    /* controls: the same script on a real event must behave differently, or the arms above prove nothing */
    memset(&s, 0, sizeof s); memset(&t, 0, sizeof t); t.seq_fd = -1; fb = 0;
    s.pending = 1; s.fd = s.fd_second = 3; s.rc = -1;
    round1(&s, &t, NVRM_EVW_ADAPT, LONG_WAIT, &fb);
    T("control: a failed call on a real event does switch the process to polling", fb == 1 && s.stats[NVRM_EVW_S_FELL_BACK] == 1 && s.noevent == 0);
}

/* the missed-wake control needs pending() to change its answer inside one round: busy before the sleep, complete after */
struct flip { unsigned calls, stats[NVRM_EVW_S_N], backoffs; };
static int f_pending(void *ctx, int *fd) { struct flip *f = ctx; *fd = 3; return f->calls++ == 0; }
static int f_wait(void *ctx, int fd, uint32_t seen, uint32_t us, uint64_t *out) { (void)ctx; (void)fd; (void)us; *out = nvrm_evw_pack(seen, NVRM_EVW_R_TIMEOUT); return 0; }
static void f_backoff(void *ctx, uint64_t waited) { struct flip *f = ctx; (void)waited; f->backoffs++; }
static void f_stat(void *ctx, int what) { struct flip *f = ctx; f->stats[what]++; }
static const struct nvrm_evw_ops fops = { f_pending, f_wait, f_backoff, f_stat };
static void missed_control(void)
{
    struct flip f; struct nvrm_evw_thread t; volatile int fb = 0; unsigned missed = 0, rounds = 0;
    memset(&t, 0, sizeof t); t.seq_fd = -1; nvrm_evw_remember(&t, 3, 0);
    while (!fb && rounds < 100) { memset(&f, 0, sizeof f); nvrm_evw_round(&t, NVRM_EVW_ADAPT, LONG_WAIT, UINT64_MAX, 1, &fb, &fops, &f); missed += f.stats[NVRM_EVW_S_MISSED]; rounds++; }
    T("control: on a real event the same full-length timeouts with the work complete are missed wakes, and sixteen fall back",
      fb == 1 && missed == NVRM_EVW_MISS_LIMIT && rounds == NVRM_EVW_MISS_LIMIT);
}

int main(void)
{
    struct waiter w; struct nvrm_evw_thread t;
    scripted();
    missed_control();
    /* 1. events delivered, jobs of 3 ms: every wait sleeps and is woken; no sleep runs to its timeout with the work done */
    memset(&t, 0, sizeof t); t.seq_fd = -1; fell_back = 0; kdrop = kcapped = klegacy = kfail = 0;
    run(NVRM_EVW_ADAPT, 60, 3000, &w, &t);
    printf("     delivered: sleeps %u woken %u timeouts %u missed %u backoffs %u\n", w.stats[NVRM_EVW_S_SLEEP], w.stats[NVRM_EVW_S_WOKEN], w.stats[NVRM_EVW_S_TIMEOUT], w.stats[NVRM_EVW_S_MISSED], w.backoffs);
    T("delivered events: the waits sleep and are woken", w.stats[NVRM_EVW_S_WOKEN] >= 50);
    T("delivered events: no wake is lost", w.stats[NVRM_EVW_S_MISSED] == 0 && !fell_back);
    /* 2. eager mode (WindowServer): the same, sleeping from 20 us */
    memset(&t, 0, sizeof t); t.seq_fd = -1;
    run(NVRM_EVW_EAGER, 60, 500, &w, &t);
    T("eager: woken, nothing missed, no fallback", w.stats[NVRM_EVW_S_WOKEN] >= 50 && w.stats[NVRM_EVW_S_MISSED] == 0 && !fell_back);
    /* 3. events never arrive: bounded sleeps, then polling for good. This is also the control for arms 1 and 2: the
          same waiter against a kernel that loses every wake is seen to miss them. */
    memset(&t, 0, sizeof t); t.seq_fd = -1; kdrop = 1; kmax_us = 0;
    const uint64_t a = now_ns();
    run(NVRM_EVW_ADAPT, 40, 2500, &w, &t);
    const uint64_t took = now_ns() - a;
    printf("     dropped: sleeps %u timeouts %u missed %u fell_back %u backoffs %u, %.1f ms for 40 jobs of 2.5 ms\n", w.stats[NVRM_EVW_S_SLEEP], w.stats[NVRM_EVW_S_TIMEOUT], w.stats[NVRM_EVW_S_MISSED], w.stats[NVRM_EVW_S_FELL_BACK], w.backoffs, (double)took / 1e6);
    T("dropped events: the process falls back to polling, once", fell_back && w.stats[NVRM_EVW_S_FELL_BACK] == 1 && w.stats[NVRM_EVW_S_MISSED] == NVRM_EVW_MISS_LIMIT);
    T("dropped events: no sleep is asked to last longer than 2 ms", kmax_us == NVRM_EVW_MAX_SLEEP_US);
    T("dropped events: the whole run is not held up (well under a second over the work itself)", took < 40ull * 2500000ull + 1000000000ull);
    const unsigned sleeps_after = w.stats[NVRM_EVW_S_SLEEP];
    run(NVRM_EVW_ADAPT, 10, 2500, &w, &t);
    T("after the fallback nothing sleeps on the event", w.stats[NVRM_EVW_S_SLEEP] == 0 && sleeps_after > 0);
    /* 4. the kernel at its sleeper ceiling: one polling step per round, never a tight loop */
    memset(&t, 0, sizeof t); t.seq_fd = -1; fell_back = 0; kdrop = 0; kcapped = 1;
    run(NVRM_EVW_ADAPT, 10, 3000, &w, &t);
    T("capped: every capped answer is followed by a polling step", w.stats[NVRM_EVW_S_CAPPED] > 0 && w.backoffs >= w.stats[NVRM_EVW_S_CAPPED] && !fell_back);
    /* 5. a 2.9 kext (no reason): sleeps work, nothing is counted as missed, no fallback */
    memset(&t, 0, sizeof t); t.seq_fd = -1; kcapped = 0; klegacy = 1;
    run(NVRM_EVW_ADAPT, 30, 3000, &w, &t);
    T("2.9 kext: sleeps, and never falls back", w.stats[NVRM_EVW_S_SLEEP] > 0 && w.stats[NVRM_EVW_S_MISSED] == 0 && !fell_back);
    /* 6. no event at all (the call fails): polling, as 2.9 */
    memset(&t, 0, sizeof t); t.seq_fd = -1; klegacy = 0; kfail = 1;
    run(NVRM_EVW_ADAPT, 5, 3000, &w, &t);
    T("no usable event: falls back at the first failed call", fell_back && w.stats[NVRM_EVW_S_SLEEP] == 0);
    printf(fails ? "\n%d FAILED\n" : "\nall passed\n", fails);
    return fails != 0;
}
