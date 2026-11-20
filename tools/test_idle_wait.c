/* test_idle_wait.c - the resource wait of plugin/nvmtl_idle_wait.h, with real threads.
   Build and run anywhere (no GPU, no Metal):
     cc -std=c11 -O2 -pthread -Wall -Wextra -Werror -D_DEFAULT_SOURCE -I plugin tools/test_idle_wait.c -o /tmp/test_idle_wait && /tmp/test_idle_wait
   The test's slice is 300 ms: a waiter that a release failed to wake sleeps a whole slice, so every wait that ends in
   less than half of it was woken. Proves: no wake-up is lost while a release races the waiter's registration (tens of
   thousands of rounds, the release at varying offsets from the wait); a release that does not tell the queue leaves the
   waiter asleep for the slice (the control: the measure above can see a lost wake); a release while nobody waits takes
   no lock; a count reaching zero wakes only a waiter listed for it, or one that found no slot; the 10 s limit becomes
   the given limit and is reported; the texture and buffer rules for a negative count are the old ones, and a buffer
   waiting out a negative count is woken by the add that ends it; the switch. */
#define NVMTL_IDLE_SLICE_NS 300000000ull
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "nvmtl_idle_wait.h"

static int fails;
static void arm(const char *what, int ok) { printf("%s  %s\n", ok ? "ok  " : "FAIL", what); if (!ok) fails++; }

static nvmtl_idle_waitq q = NVMTL_IDLE_WAITQ_INIT;

/* a change of the count as nvmtl_resref makes it: the queue told whenever the count arrives at zero */
static void change(int *refs, int d, int tell)
{
    const int v = nvmtl_idle_add(refs, d);
    if (v == 0 && tell) nvmtl_idle_settled(&q, refs);
}
static void release(int *refs, int tell) { change(refs, -1, tell); }
static void spin(unsigned n) { for (volatile unsigned i = 0; i < n; i++) { } }

typedef struct { int *refs; unsigned delay; int tell; int holds; } releaser_arg;
static void *releaser(void *p)
{
    releaser_arg *a = p;
    spin(a->delay);
    for (int i = 0; i < a->holds; i++) release(a->refs, a->tell);
    return NULL;
}

typedef struct { int *refs; int textureRule; uint64_t limit; int late, slept; uint64_t took; } waiter_arg;
static void *waiter(void *p)
{
    waiter_arg *a = p;
    const uint64_t t0 = nvmtl_idle_now_ns();
    a->late = nvmtl_idle_wait_for(&q, a->refs, a->textureRule, t0, a->limit, &a->slept);
    a->took = nvmtl_idle_now_ns() - t0;
    return NULL;
}

static void *settle_unrelated(void *p) { static int other; nvmtl_idle_settled(&q, &other); *(int *)p = 1; return NULL; }

int main(void)
{
    arm("unset NVMTL_GL_WAIT_EVENT: woken", nvmtl_idle_wait_on(NULL));
    arm("NVMTL_GL_WAIT_EVENT=1 or empty: woken", nvmtl_idle_wait_on("1") && nvmtl_idle_wait_on(""));
    arm("NVMTL_GL_WAIT_EVENT=0: polled, as before", !nvmtl_idle_wait_on("0"));

    int n = -1;
    arm("a negative count is not busy for a texture (it waited while above zero)", !nvmtl_idle_busy(&n, 1));
    arm("a negative count is busy for a buffer (it waited while not zero)", nvmtl_idle_busy(&n, 0));
    n = 0;
    arm("a zero count is idle for both", !nvmtl_idle_busy(&n, 1) && !nvmtl_idle_busy(&n, 0));
    { int slept = 1; arm("an idle count returns at once without sleeping",
                         nvmtl_idle_wait_for(&q, &n, 0, nvmtl_idle_now_ns(), 1000000000ull, &slept) == 0 && !slept); }

    /* nobody waits: the release takes no lock (here the lock is held, and the release still returns) */
    {
        int done = 0; pthread_t th;
        pthread_mutex_lock(&q.lock);
        pthread_create(&th, NULL, settle_unrelated, &done);
        usleep(100000);
        const int returned = __atomic_load_n(&done, __ATOMIC_SEQ_CST);
        pthread_mutex_unlock(&q.lock);
        pthread_join(th, NULL);
        arm("with nobody waiting a release at zero takes no lock", returned);
    }

    /* who is woken */
    {
        int a = 1, b = 1; nvmtl_idle_waitq w = NVMTL_IDLE_WAITQ_INIT;
        w.watch[3] = &a;
        arm("a count a waiter is listed for wakes it", nvmtl_idle_wanted(&w, &a));
        arm("an unrelated count reaching zero wakes nobody", !nvmtl_idle_wanted(&w, &b));
        w.unlisted = 1;
        arm("a waiter that found no slot is woken by any count at zero", nvmtl_idle_wanted(&w, &b));
    }

    /* the race: the release lands anywhere around the waiter's registration; every wait must end woken */
    {
        int lost = 0, rounds = 0, slept = 0; uint64_t worst = 0;
        for (int r = 0; r < 30000; r++) {
            int refs = 1 + (r % 3);
            releaser_arg ra = { &refs, (unsigned)((r * 7919u) % 4000u), 1, refs };
            waiter_arg wa = { &refs, r & 1, 10000000000ull, 0, 0, 0 };
            pthread_t tw, tr;
            if (r & 2) { pthread_create(&tw, NULL, waiter, &wa); pthread_create(&tr, NULL, releaser, &ra); }
            else { pthread_create(&tr, NULL, releaser, &ra); pthread_create(&tw, NULL, waiter, &wa); }
            pthread_join(tr, NULL); pthread_join(tw, NULL);
            rounds++; slept += wa.slept;
            if (wa.late || wa.took >= NVMTL_IDLE_SLICE_NS / 2) lost++;
            if (wa.took > worst) worst = wa.took;
        }
        printf("      %d rounds, %d slept, slowest %llu us\n", rounds, slept, (unsigned long long)(worst / 1000));
        arm("no wake-up is lost when the release races the wait (every wait ended well inside one slice)", !lost);
        arm("the race really put waiters to sleep (some rounds slept)", slept > 0);
    }

    /* several waiters on several counts, more than the list holds: each woken by its own release */
    {
        enum { W = NVMTL_IDLE_WATCH + 4 };
        int refs[W]; waiter_arg wa[W]; releaser_arg ra[W]; pthread_t tw[W], tr[W];
        for (int i = 0; i < W; i++) { refs[i] = 1; wa[i] = (waiter_arg){ &refs[i], 0, 10000000000ull, 0, 0, 0 };
                                     pthread_create(&tw[i], NULL, waiter, &wa[i]); }
        usleep(50000);
        for (int i = 0; i < W; i++) { ra[i] = (releaser_arg){ &refs[i], 1000u * (unsigned)i, 1, 1 }; pthread_create(&tr[i], NULL, releaser, &ra[i]); }
        int ok = 1;
        for (int i = 0; i < W; i++) { pthread_join(tr[i], NULL); pthread_join(tw[i], NULL);
                                     if (wa[i].late || !wa[i].slept || wa[i].took >= NVMTL_IDLE_SLICE_NS / 2 + 50000000ull) ok = 0; }
        arm("waiters beyond the list's slots are woken too, each by its own count", ok);
        int clean = q.waiters == 0 && q.unlisted == 0;
        for (int i = 0; i < NVMTL_IDLE_WATCH; i++) if (q.watch[i]) clean = 0;
        arm("every waiter takes its listing with it", clean);
    }

    /* the control: a release that does not tell the queue leaves a sleeping waiter asleep for the whole slice */
    {
        int refs = 1;
        waiter_arg wa = { &refs, 0, 10000000000ull, 0, 0, 0 };
        releaser_arg ra = { &refs, 0, 0, 1 };
        pthread_t tw, tr;
        pthread_create(&tw, NULL, waiter, &wa);
        usleep(50000);
        pthread_create(&tr, NULL, releaser, &ra);
        pthread_join(tr, NULL); pthread_join(tw, NULL);
        arm("control: an untold release is seen only at the end of the slice (the measure above sees a lost wake)",
            !wa.late && wa.slept && wa.took >= NVMTL_IDLE_SLICE_NS / 2);
    }

    /* a buffer waits out a negative count too: the add that brings it back to zero wakes the waiter */
    {
        int refs = -1;
        waiter_arg wa = { &refs, 0, 10000000000ull, 0, 0, 0 };
        pthread_t tw;
        pthread_create(&tw, NULL, waiter, &wa);
        usleep(50000);
        change(&refs, 1, 1);
        pthread_join(tw, NULL);
        arm("a buffer waiting out a negative count is woken by the add that ends it",
            !wa.late && wa.slept && wa.took < NVMTL_IDLE_SLICE_NS / 2 + 50000000ull);
    }

    /* the limit */
    {
        int refs = 1;
        waiter_arg wa = { &refs, 1, 200000000ull, 0, 0, 0 };
        pthread_t tw; pthread_create(&tw, NULL, waiter, &wa); pthread_join(tw, NULL);
        arm("a count that never settles gives up at the limit and says so", wa.late && wa.took >= 200000000ull && wa.took < 2000000000ull);
        arm("and leaves no waiter behind", q.waiters == 0);
    }

    printf(fails ? "FAILED %d\n" : "ok\n", fails);
    return fails != 0;
}
