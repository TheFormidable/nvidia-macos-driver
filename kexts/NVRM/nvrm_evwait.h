/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

/* nvrm_evwait.h - the event wait, shared by NVRM.kext (user client selector 6) and NVK's nvkmd/nvrm backend.
   One file holds both halves so they cannot drift: the bits the kernel returns, and the rule user space follows to
   decide between polling and sleeping. Pure C (stdint only). NVK carries a byte-identical copy that its patch adds as
   nvkmd/nvrm/nvrm_evwait.h (tools/test_shared_headers.py compares the two); tools/test_evwait_policy.c proves the
   rules on the host and tools/test_evwait_loop.c runs the loop against a stand-in for the kernel.

   Selector 6, unchanged shape (2 scalar inputs, 1 scalar output):
     in[0]  the event count the caller saw       in[1]  how long to sleep, in microseconds (0 = just read the count)
     out[0] bits 0..31  the event count now
            bits 32..39 why the call returned (nvrm_evw_reason), 0 on a kext older than this file
   A 2.9 user space reads only the low 32 bits, so it is unaffected. A new user space on a 2.9 kext sees reason 0 and
   behaves as 2.9 did. Selector 7 says which one it is: bit 1 (NVRM_EVW_CAP_REASON).

   Selector 9, new (1 scalar input, no output): user space tells the kernel what it observed, so one sysctl shows it for
   the whole machine. in[0] = NVRM_EVW_NOTE_MISSED (one missed wake) or NVRM_EVW_NOTE_FELL_BACK (the process went back
   to polling: once each time it does, so a process whose events came back and then stopped again says it twice). A 2.9
   kext answers "bad argument", which the caller ignores. */
#ifndef NVRM_EVWAIT_H
#define NVRM_EVWAIT_H
#include <stdint.h>

#define NVRM_EVW_CAP_WAIT   0x1u   /* selector 7 bit 0 (2.9): token mappings and selector 6 exist */
#define NVRM_EVW_CAP_REASON 0x2u   /* selector 7 bit 1: selector 6 reports the reason, selector 9 exists */
#define NVRM_EVW_SEL_WAIT   6u
#define NVRM_EVW_SEL_CAPS   7u
#define NVRM_EVW_SEL_NOTE   9u
enum { NVRM_EVW_NOTE_MISSED = 1, NVRM_EVW_NOTE_FELL_BACK = 2 };

enum nvrm_evw_reason {
    NVRM_EVW_R_LEGACY   = 0,   /* a kext that does not report (2.9) */
    NVRM_EVW_R_COUNT    = 1,   /* timeout 0: the count was read, nothing else */
    NVRM_EVW_R_MOVED    = 2,   /* the count had already moved past `seen`: did not sleep */
    NVRM_EVW_R_WOKEN    = 3,   /* slept, and an event ended the sleep */
    NVRM_EVW_R_TIMEOUT  = 4,   /* slept the whole timeout, the count did not move */
    NVRM_EVW_R_CAPPED   = 5,   /* too many sleepers in the kernel: did not sleep, the caller must poll this round */
    NVRM_EVW_R_ABORTED  = 6,   /* the sleep was interrupted (a signal, the thread is ending) */
    NVRM_EVW_R_SPURIOUS = 7,   /* awakened but the count did not move */
};
static inline uint64_t nvrm_evw_pack(uint32_t seq, unsigned reason) { return (uint64_t)seq | ((uint64_t)(reason & 0xffu) << 32); }
static inline uint32_t nvrm_evw_seq(uint64_t out)    { return (uint32_t)out; }
static inline unsigned nvrm_evw_reason_of(uint64_t out) { return (unsigned)((out >> 32) & 0xffu); }

/* Kernel side. `wr` is what the park returned, in the caller's own terms: 0 awakened, 1 timed out, 2 interrupted. */
enum { NVRM_EVW_WR_AWAKENED = 0, NVRM_EVW_WR_TIMED_OUT = 1, NVRM_EVW_WR_INTERRUPTED = 2 };
static inline unsigned nvrm_evw_classify(int slept, int wr, uint32_t seen, uint32_t now)
{
    if (!slept) return now != seen ? NVRM_EVW_R_MOVED : NVRM_EVW_R_COUNT;
    if (wr == NVRM_EVW_WR_INTERRUPTED) return NVRM_EVW_R_ABORTED;
    if (now != seen) return NVRM_EVW_R_WOKEN;
    return wr == NVRM_EVW_WR_TIMED_OUT ? NVRM_EVW_R_TIMEOUT : NVRM_EVW_R_SPURIOUS;
}

/* User side. */
enum nvrm_evw_mode {
    NVRM_EVW_POLL  = 0,   /* never sleep on the event: what every process except WindowServer did in 2.9 */
    NVRM_EVW_EAGER = 1,   /* sleep after 20 us: what WindowServer did in 2.9, and NVRM_EVENT_WAIT=1 */
    NVRM_EVW_ADAPT = 2,   /* the default everywhere else: poll as 2.9 for a short wait, sleep through a long one */
};
#define NVRM_EVW_EAGER_SPIN_NS   20000ull      /* 2.9: `waited_ns < 20000` polls */
#define NVRM_EVW_SPIN_NS         50000ull      /* the pause-spin phase of nvkmd_nvrm_wait_backoff */
#define NVRM_EVW_YIELD_NS        1000000ull    /* the sched_yield phase ends here; 2.9 goes on with usleep(50) */
#define NVRM_EVW_LONG_NS         1000000ull    /* avg_ns at or above this: skip the yield phase (avg_ns says what it samples) */
#define NVRM_EVW_MAX_SLEEP_US    2000u         /* 2.9's bound: a missing event costs latency, never a hang */
#define NVRM_EVW_MIN_SLEEP_NS    100000ull     /* less time than this left before the caller's deadline: poll */
#define NVRM_EVW_MISS_LIMIT      16u           /* this many missed wakes in a row, the count never moving: the process polls
                                                  (NVRM_EVW_FB_QUIET), and its threads try the event again now and then */
#define NVRM_EVW_PAUSE_FIRST     256u          /* QUIET: waits a thread polls before its first trial of the event */
#define NVRM_EVW_PAUSE_MAX       65536u        /* each trial that fails doubles the thread's pause, up to this */
#define NVRM_EVW_TRIAL_MISSES    4u            /* the missed wake that ends a trial as a failure */

/* The process flag (`fell_back` below). 0: waits may sleep on the event. Every non-zero value means "poll", and a reader
   that only tests it for non-zero needs to know no more. */
enum {
    NVRM_EVW_FB_DEAD  = 1,   /* a kernel wait call failed: the event cannot be used, polling for good (2.9 did the same) */
    NVRM_EVW_FB_QUIET = 2,   /* NVRM_EVW_MISS_LIMIT missed wakes: polling, but each thread now and then tries a few sleeps
                                (a trial), and the first answer that proves events arrive clears the flag again */
};

/* NVRM_EVENT_WAIT: unset or empty -> the default for the process; "0" poll; "2" adaptive; anything else eager (2.9: `e[0] != '0'`). */
static inline int nvrm_evw_mode_parse(const char *env, int is_window_server)
{
    if (!env || !env[0]) return is_window_server ? NVRM_EVW_EAGER : NVRM_EVW_ADAPT;
    if (env[0] == '0') return NVRM_EVW_POLL;
    if (env[0] == '2') return NVRM_EVW_ADAPT;
    return NVRM_EVW_EAGER;
}

/* What one thread remembers between calls. Zero-initialised is the right start. */
struct nvrm_evw_thread {
    uint64_t last_waited_ns;   /* waited_ns of the previous call: a smaller value now means a new wait began */
    uint64_t avg_ns;           /* running mean (1/4 weight), over this thread's finished waits, of the time into each wait
                                  at which its last round began: not the wait's length (see nvrm_evw_begin) */
    uint32_t seq;              /* the count the kernel returned last time, for the next sleep */
    int      seq_fd;           /* the event it belongs to, -1 none */
    int      seq_valid;
    unsigned misses;           /* missed wakes in a row on this thread */
    /* While the process is QUIET (nvrm_evw_quiet, below): */
    unsigned quiet_waits;      /* waits this thread began since the process went QUIET or since its last trial */
    unsigned pause;            /* how many such waits it polls before its next trial; 0 = NVRM_EVW_PAUSE_FIRST */
    int      trial;            /* 1: in a trial: this thread's rounds sleep on the event as if the flag were clear */
    unsigned trial_misses;     /* missed wakes in this trial */
};
/* Call first in every wait round. Returns 1 when a new wait began (the previous one is folded into the mean).
   The sample is last_waited_ns, the time into the previous wait at which its last round began. It falls short of the
   wait's length by what that round took, up to a whole sleep: a wait polled for its first millisecond that then sleeps
   until the GPU is done is a sample of 1 ms or a little more however long it took, and the mean (whose update rounds
   up) reaches NVRM_EVW_LONG_NS on such samples within about fifty waits; a wait that sleeps right after the 50 us spin
   and ends in that first sleep is a sample of 50 us. What follows in the adaptive mode, for steady waits: under 1 ms,
   polled; from 1 ms to about 2.05 ms (one spin and one 2 ms sleep), polled for the first millisecond and then asleep,
   except about one wait in forty that sleeps right after the spin and pulls the mean down again; longer than that,
   asleep right after the spin, from the third wait on when they run longer than 3 ms (the polled millisecond and one
   whole sleep), after some fifty waits when they run less. tools/test_evwait_policy.c checks these numbers. */
static inline int nvrm_evw_begin(struct nvrm_evw_thread *t, uint64_t waited_ns)
{
    int fresh = 0;
    if (waited_ns < t->last_waited_ns) { t->avg_ns = t->avg_ns - (t->avg_ns >> 2) + (t->last_waited_ns >> 2); fresh = 1; }
    t->last_waited_ns = waited_ns;
    return fresh;
}
/* 1 = sleep on the event this round, 0 = one step of the 2.9 polling ladder.
   remain_ns: time left before the caller's own deadline (UINT64_MAX when there is none). fell_back: non-zero when this
   round must poll (the process flag, as nvrm_evw_quiet lets this thread see it). */
static inline int nvrm_evw_should_sleep(int mode, uint64_t waited_ns, uint64_t avg_ns, uint64_t remain_ns, int fell_back)
{
    if (mode == NVRM_EVW_POLL || fell_back) return 0;
    if (mode == NVRM_EVW_EAGER) return waited_ns >= NVRM_EVW_EAGER_SPIN_NS;      /* exactly 2.9, deadline and all */
    if (remain_ns < NVRM_EVW_MIN_SLEEP_NS) return 0;
    if (waited_ns < NVRM_EVW_SPIN_NS) return 0;                                  /* the spin: identical to 2.9 */
    if (avg_ns >= NVRM_EVW_LONG_NS) return 1;                                    /* this thread waits long: do not burn a core for 1 ms */
    return waited_ns >= NVRM_EVW_YIELD_NS;                                       /* where 2.9 would start usleep(50) */
}
static inline uint32_t nvrm_evw_timeout_us(int mode, uint64_t remain_ns)
{
    if (mode == NVRM_EVW_EAGER) return NVRM_EVW_MAX_SLEEP_US;
    const uint64_t us = remain_ns / 1000u;
    return us < NVRM_EVW_MAX_SLEEP_US ? (uint32_t)us : NVRM_EVW_MAX_SLEEP_US;
}
/* The count to pass as `seen`, or 0 with *have = 0 when the thread has none for this event (the caller then reads it). */
static inline uint32_t nvrm_evw_seen(const struct nvrm_evw_thread *t, int fd, int *have)
{
    *have = t->seq_valid && t->seq_fd == fd;
    return *have ? t->seq : 0;
}
static inline void nvrm_evw_remember(struct nvrm_evw_thread *t, int fd, uint32_t seq) { t->seq = seq; t->seq_fd = fd; t->seq_valid = 1; }
static inline void nvrm_evw_forget(struct nvrm_evw_thread *t) { t->seq_valid = 0; }

/* After a sleep (WOKEN, TIMEOUT), and after an answer that did not sleep because the count had moved (MOVED).
   done: after a TIMEOUT, the work this thread waited for was complete when the sleep returned; not looked at otherwise.
   Returns 1 when the process must go back to polling (QUIET); *missed says whether this sleep was a missed wake.
   A missed wake is a full-length sleep that ended on its timeout, with the count where it was, although the work had
   finished. One alone proves nothing: the GPU makes a completion visible first (the semaphore release user space
   reads) and raises the event after it (the kext posts it only once the interrupt is serviced and RM has run), so a
   sleep that runs out between the two is a missed wake although its event is on the way. That event shows at the
   thread's next call as a moved count: WOKEN when it comes during a sleep, MOVED when it came before the call. Either
   proves that events arrive, so either ends the run. What proves a dead event is NVRM_EVW_MISS_LIMIT missed wakes in a
   row with the count standing still all the while. Any other reason leaves the run as it is here (nvrm_evw_round also
   ends it on a capped or aborted answer whose count moved); a kext that gives no reason (2.9) never counts a miss. */
static inline int nvrm_evw_account(struct nvrm_evw_thread *t, unsigned reason, int done, uint32_t slept_us, int *missed)
{
    *missed = 0;
    if (reason == NVRM_EVW_R_WOKEN || reason == NVRM_EVW_R_MOVED) { t->misses = 0; return 0; }
    if (reason == NVRM_EVW_R_TIMEOUT && done && slept_us >= NVRM_EVW_MAX_SLEEP_US) { *missed = 1; return ++t->misses >= NVRM_EVW_MISS_LIMIT; }
    return 0;
}
/* 1 when the caller must take one polling step instead of looping straight back into the kernel. */
static inline int nvrm_evw_must_poll(unsigned reason)
{
    return reason == NVRM_EVW_R_CAPPED || reason == NVRM_EVW_R_ABORTED || reason == NVRM_EVW_R_SPURIOUS;
}

/* Going back to polling after missed wakes is a pause, not the end. NVRM_EVW_MISS_LIMIT missed wakes take only some
   tens of milliseconds, and events that stop for that long while GPU work completes (an interrupt served late or lost;
   when that happens is not measured) may well come back. RM posts the event from rm_isr, without the GPU lock, so a
   long RM call is not what holds it back. So a QUIET process polls, and each of its threads, counting in its own waits
   (no clock), tries the event again once its pause has passed: in a trial its rounds sleep as if the flag were clear.
   The first answer that proves events arrive (WOKEN, MOVED, or a capped or aborted answer whose count moved:
   nvrm_evw_round) clears the flag for the whole process and resets this thread; the NVRM_EVW_TRIAL_MISSES-th missed
   wake of the trial ends it and doubles this thread's pause (256 waits first, NVRM_EVW_PAUSE_MAX at most). A sleep that
   ran out with the work still pending is neither. When events never come back, a thread pays at most
   NVRM_EVW_TRIAL_MISSES missed wakes (each at most one NVRM_EVW_MAX_SLEEP_US sleep of latency) per trial: 22 trials in
   its first million waits after going QUIET (pauses 256 to 32768 are 65280 waits and 8 trials, then one trial per 65536
   waits). A failed kernel call (DEAD) never gets a trial.

   nvrm_evw_quiet: once per round, after nvrm_evw_begin (`fresh`: what it returned), with the flag as read now. Returns
   the flag as this round must use it: 0 while this thread is in a trial of a QUIET process. *began: a trial starts now. */
static inline int nvrm_evw_quiet(struct nvrm_evw_thread *t, int fresh, int flag, int *began)
{
    *began = 0;
    if (flag == 0) {
        if (t->trial || t->pause || t->quiet_waits) {
            /* another thread brought the events back: this thread's trial and pause belonged to the quiet time, and its
               run of missed wakes (those of a trial) starts again from 0 */
            t->trial = 0; t->pause = 0; t->quiet_waits = 0; t->trial_misses = 0; t->misses = 0;
        }
        return 0;
    }
    if (flag != NVRM_EVW_FB_QUIET) { t->trial = 0; return flag; }
    if (t->trial) return 0;
    t->misses = 0;   /* a polling thread misses nothing; it must not leave QUIET one missed wake away from going back */
    if (fresh) t->quiet_waits++;
    if (t->quiet_waits < (t->pause ? t->pause : NVRM_EVW_PAUSE_FIRST)) return flag;
    t->quiet_waits = 0; t->trial = 1; t->trial_misses = 0; *began = 1;
    return 0;
}
/* A trial ended on its NVRM_EVW_TRIAL_MISSES-th missed wake: poll again, for twice as long. */
static inline void nvrm_evw_trial_failed(struct nvrm_evw_thread *t)
{
    const unsigned p = t->pause ? t->pause : NVRM_EVW_PAUSE_FIRST;
    t->pause = p >= NVRM_EVW_PAUSE_MAX / 2u ? NVRM_EVW_PAUSE_MAX : 2u * p;
    t->trial = 0; t->trial_misses = 0; t->quiet_waits = 0; t->misses = 0;
}
/* An answer proved that events arrive: this thread's quiet state goes (the caller clears a QUIET flag). */
static inline void nvrm_evw_events_back(struct nvrm_evw_thread *t)
{
    t->trial = 0; t->pause = 0; t->quiet_waits = 0; t->trial_misses = 0; t->misses = 0;
}

/* One round of a wait loop, with the outside world behind four calls so the same code runs in NVK and in
   tools/test_evwait_loop.c. The caller's loop is unchanged: it checks its own condition, calls this, and checks again. */
enum { NVRM_EVW_S_SLEEP, NVRM_EVW_S_WOKEN, NVRM_EVW_S_TIMEOUT, NVRM_EVW_S_MISSED, NVRM_EVW_S_CAPPED, NVRM_EVW_S_FELL_BACK,
       NVRM_EVW_S_CHAN_SLEEP,   /* a sleep the wait callback found for a channel that has no event (fd < 0, below) */
       NVRM_EVW_S_TRIAL,        /* a thread of a QUIET process began a trial of the event */
       NVRM_EVW_S_BACK,         /* an answer proved that events arrive again: the QUIET flag was cleared */
       NVRM_EVW_S_N };
struct nvrm_evw_ops {
    /* 1 while the GPU work is not complete; *fd = the event to sleep on, -1 when the pending work is on a channel that
       raises no event of its own (a copy-engine-only channel). */
    int  (*pending)(void *ctx, int *fd);
    /* The kernel wait. Returns 0 when it answered, with *out = nvrm_evw_pack(count, reason); non-zero when it could not.
       This callback is the one place where a caller can put a better kernel wait than the GPU-wide event.
       fd >= 0  selector 6 on that event: timeout_us 0 reads the count and never sleeps; otherwise it sleeps until the
                count differs from `seen`, for at most timeout_us. Non-zero means the event cannot be used at all: the
                process polls from then on, which is what 2.9 did when the call failed.
       fd < 0   there is no event. `seen` is 0 and timeout_us is the longest the callback may sleep. A callback that knows
                another kernel wait for the completion of that channel uses it and answers NVRM_EVW_R_WOKEN (the work it
                waited for is complete) or NVRM_EVW_R_TIMEOUT (it slept and the time ran out); the count in *out is not
                read. Non-zero, or any other reason, means it did not sleep: the round takes one polling step and nothing
                else changes, because a channel without an event says nothing about the event. A callback with no such
                wait returns non-zero for every fd < 0: exactly 2.9, which polled these channels. */
    int  (*wait)(void *ctx, int fd, uint32_t seen, uint32_t timeout_us, uint64_t *out);
    void (*backoff)(void *ctx, uint64_t waited_ns);   /* one step of the 2.9 polling ladder */
    void (*stat)(void *ctx, int what);        /* NVRM_EVW_S_*: a counter; FELL_BACK and MISSED are also reported to the kernel.
                                                 FELL_BACK comes once each time the flag leaves 0, never for a failed trial */
};
/* A kernel wait call failed: the event cannot be used, the process polls for good. FELL_BACK only when the flag was 0
   (a process that was already QUIET has said so once). */
static inline void nvrm_evw_dead(struct nvrm_evw_thread *t, volatile int *fell_back, const struct nvrm_evw_ops *ops, void *ctx)
{
    const int was = *fell_back;
    *fell_back = NVRM_EVW_FB_DEAD;
    t->trial = 0;
    if (was == 0) ops->stat(ctx, NVRM_EVW_S_FELL_BACK);
}
/* has_gpu_work: the wait has at least one GPU completion to wait for (2.9: `n != 0`).
   *fell_back: one flag for the process (NVRM_EVW_FB_*). Set to DEAD here when a kernel wait call fails, for good; to
   QUIET when a thread reaches NVRM_EVW_MISS_LIMIT missed wakes; from QUIET back to 0 by an answer that proves events
   arrive. Threads read it without a lock. The two changes that depend on what it holds are compare-and-swaps (0 to
   QUIET, QUIET to 0), so a DEAD stored by another thread in between is never overwritten and never cleared, and of
   two threads that go QUIET or bring the events back together only one says so; DEAD is a plain store, it wins
   over anything. */
static inline void nvrm_evw_round(struct nvrm_evw_thread *t, int mode, uint64_t waited_ns, uint64_t remain_ns, int has_gpu_work,
                                  volatile int *fell_back, const struct nvrm_evw_ops *ops, void *ctx)
{
    const int fresh = nvrm_evw_begin(t, waited_ns);
    int began = 0;
    const int flag = nvrm_evw_quiet(t, fresh, *fell_back, &began);
    if (began) {
        /* a count remembered from before the quiet time could have moved since, and would end the trial at once on
           an event long gone: the trial starts from a count it reads itself */
        nvrm_evw_forget(t);
        ops->stat(ctx, NVRM_EVW_S_TRIAL);
    }
    if (!has_gpu_work || !nvrm_evw_should_sleep(mode, waited_ns, t->avg_ns, remain_ns, flag)) { ops->backoff(ctx, waited_ns); return; }
    int fd = -1, fd2 = -1, have = 0;
    if (!ops->pending(ctx, &fd)) return;                       /* complete: the caller's check sees it */
    uint64_t out = 0;
    if (fd < 0) {
        /* No event to sleep on. The wait callback is asked all the same, because it may know a wait of the channel's
           own. Its sleeps are not the event's: they are not remembered as a count and never count as a missed wake, so
           a channel that cannot be slept on cannot switch the process to polling. */
        if (ops->wait(ctx, -1, 0, nvrm_evw_timeout_us(mode, remain_ns), &out) == 0) {
            const unsigned how = nvrm_evw_reason_of(out);
            if (how == NVRM_EVW_R_WOKEN || how == NVRM_EVW_R_TIMEOUT) { ops->stat(ctx, NVRM_EVW_S_CHAN_SLEEP); return; }
        }
        ops->backoff(ctx, waited_ns);
        return;
    }
    uint32_t seen = nvrm_evw_seen(t, fd, &have);
    if (!have) {
        /* The count is read BEFORE the completion is checked again: an event between the two moves the count, so the
           sleep below returns at once. A count remembered from this thread's last return is older still, so it is safe
           the same way and saves this call. */
        if (ops->wait(ctx, fd, 0, 0, &out) != 0) { nvrm_evw_dead(t, fell_back, ops, ctx); ops->backoff(ctx, waited_ns); return; }
        seen = nvrm_evw_seq(out);
        if (!ops->pending(ctx, &fd2)) { nvrm_evw_remember(t, fd, seen); return; }
        if (fd2 != fd) { nvrm_evw_forget(t); return; }         /* the wait moved to another device: start over next round */
    }
    const uint32_t us = nvrm_evw_timeout_us(mode, remain_ns);
    if (ops->wait(ctx, fd, seen, us, &out) != 0) { nvrm_evw_dead(t, fell_back, ops, ctx); nvrm_evw_forget(t); ops->backoff(ctx, waited_ns); return; }
    const unsigned why = nvrm_evw_reason_of(out);
    nvrm_evw_remember(t, fd, nvrm_evw_seq(out));
    if (why == NVRM_EVW_R_WOKEN || why == NVRM_EVW_R_TIMEOUT || why == NVRM_EVW_R_LEGACY) ops->stat(ctx, NVRM_EVW_S_SLEEP);
    if (why == NVRM_EVW_R_WOKEN) ops->stat(ctx, NVRM_EVW_S_WOKEN);
    if (why == NVRM_EVW_R_TIMEOUT) ops->stat(ctx, NVRM_EVW_S_TIMEOUT);
    if (why == NVRM_EVW_R_CAPPED) ops->stat(ctx, NVRM_EVW_S_CAPPED);
    /* MOVED is not a sleep and takes no polling step, but a moved count is the late event of an earlier missed wake
       (or a newer one): nvrm_evw_account ends the run of missed wakes on it as on WOKEN. */
    int proof = 0;
    if (why == NVRM_EVW_R_TIMEOUT || why == NVRM_EVW_R_WOKEN || why == NVRM_EVW_R_MOVED) {
        int missed = 0;
        const int done = why == NVRM_EVW_R_TIMEOUT ? !ops->pending(ctx, &fd2) : 0;
        const int give_up = nvrm_evw_account(t, why, done, us, &missed);
        if (missed) ops->stat(ctx, NVRM_EVW_S_MISSED);
        if (give_up) {
            /* QUIET; and this thread's run starts again, so that it is not one missed wake from QUIET when events return */
            t->misses = 0;
            int was = 0;
            if (__atomic_compare_exchange_n(fell_back, &was, NVRM_EVW_FB_QUIET, 0, __ATOMIC_RELAXED, __ATOMIC_RELAXED))
                ops->stat(ctx, NVRM_EVW_S_FELL_BACK);
        } else if (missed && t->trial && ++t->trial_misses >= NVRM_EVW_TRIAL_MISSES) {
            nvrm_evw_trial_failed(t);   /* not another FELL_BACK: the process never stopped polling */
        }
        proof = why != NVRM_EVW_R_TIMEOUT;
    } else if ((why == NVRM_EVW_R_CAPPED || why == NVRM_EVW_R_ABORTED) && nvrm_evw_seq(out) != seen) {
        t->misses = 0;   /* did not sleep its time, but the count moved all the same: the same proof that events arrive */
        proof = 1;
    }
    if (proof) {
        nvrm_evw_events_back(t);
        int was = NVRM_EVW_FB_QUIET;
        if (*fell_back == NVRM_EVW_FB_QUIET &&
            __atomic_compare_exchange_n(fell_back, &was, 0, 0, __ATOMIC_RELAXED, __ATOMIC_RELAXED))
            ops->stat(ctx, NVRM_EVW_S_BACK);
    }
    if (nvrm_evw_must_poll(why)) ops->backoff(ctx, waited_ns);
}
#endif
