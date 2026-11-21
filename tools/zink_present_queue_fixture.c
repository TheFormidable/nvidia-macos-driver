/* zink_present_queue_fixture.c: drives zink/nvk_present_queue.c, as it is, with real threads.
 * Built and run by tools/test_zink_present_queue.py (also under ThreadSanitizer and AddressSanitizer where the
 * compiler has them, and against copies of the source with one rule broken, which must fail here):
 *   cc -std=gnu11 -Wall -Wextra -Werror -g -pthread -DNVK_PQ_WAIT_MS=400 -I zink \
 *      tools/zink_present_queue_fixture.c zink/nvk_present_queue.c -o /tmp/pq && /tmp/pq
 * -DPQ_NO_FORK leaves the fork arm out (a sanitizer's runtime does not follow a fork of a threaded process). */
#include "nvk_present_queue.h"
#include <signal.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static int fails;
#define ARM(name, cond) do { const int ok_ = !!(cond); printf("%s  %s\n", ok_ ? "ok  " : "FAIL", name); fflush(stdout); if (!ok_) fails++; } while (0)

static long long now_ms(void)
{
   struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
   return (long long)t.tv_sec * 1000 + t.tv_nsec / 1000000;
}
static void sleep_ms(int ms) { struct timespec t = { ms / 1000, (ms % 1000) * 1000000L }; nanosleep(&t, NULL); }

/* a frame: what its work does is scripted */
struct frame {
   atomic_int ran;          /* times the work ran */
   atomic_int finished;     /* the work returned */
   int sleep_ms;            /* how long the "GPU" takes */
   atomic_int *gate;        /* the work waits until *gate is 1 */
   int answer;
   pthread_t on;            /* the thread the work ran on */
   atomic_int *order, slot; /* *order is taken and incremented when the work starts */
   int signals_blocked, faults_open;
};
static int work(void *p)
{
   struct frame *f = p;
   f->on = pthread_self();
   if (f->order) f->slot = atomic_fetch_add(f->order, 1);
   sigset_t mask;
   pthread_sigmask(SIG_SETMASK, NULL, &mask);
   f->signals_blocked = sigismember(&mask, SIGINT) == 1 && sigismember(&mask, SIGTERM) == 1 && sigismember(&mask, SIGUSR1) == 1 &&
                        sigismember(&mask, SIGQUIT) == 1 && sigismember(&mask, SIGPROF) == 1 && sigismember(&mask, SIGALRM) == 1;
   f->faults_open = sigismember(&mask, SIGSEGV) == 0 && sigismember(&mask, SIGBUS) == 0 && sigismember(&mask, SIGILL) == 0 &&
                    sigismember(&mask, SIGFPE) == 0 && sigismember(&mask, SIGTRAP) == 0 && sigismember(&mask, SIGSYS) == 0;
   atomic_fetch_add(&f->ran, 1);
   if (f->sleep_ms) sleep_ms(f->sleep_ms);
   while (f->gate && !atomic_load(f->gate)) sleep_ms(1);
   atomic_store(&f->finished, 1);
   return f->answer;
}

/* another thread tries the queue while the main thread is inside */
struct other { struct nvk_pq *q; int wait; int claimed; long long took_ms; atomic_int started; };
static void *other_claim(void *p)
{
   struct other *o = p;
   void *frame; int result;
   atomic_store(&o->started, 1);
   const long long t0 = now_ms();
   o->claimed = nvk_pq_claim(o->q, o->wait, &frame, &result);
   o->took_ms = now_ms() - t0;
   if (o->claimed == NVK_PQ_TAKEN) nvk_pq_release(o->q);
   return NULL;
}

/* two threads present one context: every frame is worked once and comes back once */
struct pair { struct nvk_pq *q; atomic_int *submitted, *back, *busy; struct frame *frames; int base, count; };
static void *presenter(void *p)
{
   struct pair *x = p;
   for (int i = 0; i < x->count; ) {
      void *frame; int result;
      const int claimed = nvk_pq_claim(x->q, 0, &frame, &result);
      if (claimed == NVK_PQ_BUSY) { atomic_fetch_add(x->busy, 1); sched_yield(); continue; }
      if (claimed != NVK_PQ_TAKEN) return (void *)1;
      if (frame) atomic_fetch_add(x->back, 1);
      if (!nvk_pq_submit(x->q, &x->frames[x->base + i], work)) return (void *)1;
      atomic_fetch_add(x->submitted, 1);
      i++;
   }
   return NULL;
}

int main(void)
{
   void *frame; int result;
   sigset_t before, after;
   pthread_sigmask(SIG_SETMASK, NULL, &before);

   /* --- a queue nobody used --- */
   struct nvk_pq q; memset(&q, 0, sizeof q);
   ARM("a zeroed queue is free: a claim takes it and hands back no frame",
       nvk_pq_claim(&q, 0, &frame, &result) == NVK_PQ_TAKEN && frame == NULL && q.state == NVK_PQ_CLAIMED);
   ARM("the thread inside is not let in a second time, waiting or not (no deadlock on itself)",
       nvk_pq_claim(&q, 0, &frame, &result) == NVK_PQ_BUSY && nvk_pq_claim(&q, 1, &frame, &result) == NVK_PQ_BUSY);
   {
      struct other o = { .q = &q, .wait = 0 };
      pthread_t t; pthread_create(&t, NULL, other_claim, &o); pthread_join(t, NULL);
      ARM("another thread finds the queue busy and takes nothing", o.claimed == NVK_PQ_BUSY && q.state == NVK_PQ_CLAIMED);
      struct other w = { .q = &q, .wait = 1 };
      pthread_create(&t, NULL, other_claim, &w);
      while (!atomic_load(&w.started)) sleep_ms(1);
      sleep_ms(60);
      nvk_pq_release(&q);
      pthread_join(t, NULL);
      ARM("a teardown on another thread waits for the thread inside to leave, then gets the queue",
          w.claimed == NVK_PQ_TAKEN && w.took_ms >= 50 && q.state == NVK_PQ_FREE);
   }

   /* --- one frame ahead --- */
   ARM("the process has a presentation thread", nvk_pq_available() == 1);
   pthread_sigmask(SIG_SETMASK, NULL, &after);
   int same_mask = 1;
   for (int sig = 1; sig < 32; sig++) if (sigismember(&before, sig) != sigismember(&after, sig)) same_mask = 0;
   ARM("starting it leaves the caller's signal mask as it was", same_mask && sigismember(&after, SIGINT) == 0);
   struct frame f1 = { .sleep_ms = 80, .answer = 7 };
   nvk_pq_claim(&q, 0, &frame, &result);
   long long t0 = now_ms();
   ARM("a frame is handed over", nvk_pq_submit(&q, &f1, work) == 1);
   ARM("handing it over does not wait for its work", now_ms() - t0 < 60 && !atomic_load(&f1.finished));
   const int claimed = nvk_pq_claim(&q, 0, &frame, &result);
   ARM("the next claim waits for the frame in flight and gets it back with its answer",
       claimed == NVK_PQ_TAKEN && frame == &f1 && result == 7 && atomic_load(&f1.finished) == 1 && now_ms() - t0 >= 70);
   ARM("the work ran once, on another thread, with the signals sent to a process blocked",
       atomic_load(&f1.ran) == 1 && !pthread_equal(f1.on, pthread_self()) && f1.signals_blocked);
   ARM("and the signals of a fault open: a fault there ends the process as on any thread, it does not fault for ever", f1.faults_open);
   nvk_pq_release(&q);
   ARM("after the frame came back the queue is free and holds no frame", q.state == NVK_PQ_FREE && q.frame == NULL);

   /* --- frames of several contexts are worked in the order they were handed over --- */
   {
      struct nvk_pq a, b, c; memset(&a, 0, sizeof a); memset(&b, 0, sizeof b); memset(&c, 0, sizeof c);
      atomic_int order = 0, gate = 0;
      struct frame fa = { .order = &order, .gate = &gate, .answer = 1 }, fb = { .order = &order, .answer = 1 },
                   fc = { .order = &order, .answer = 1 };
      nvk_pq_claim(&a, 0, &frame, &result); nvk_pq_submit(&a, &fa, work);
      nvk_pq_claim(&b, 0, &frame, &result); nvk_pq_submit(&b, &fb, work);
      nvk_pq_claim(&c, 0, &frame, &result); nvk_pq_submit(&c, &fc, work);
      sleep_ms(40);
      ARM("a frame behind one whose GPU work has not finished is not started", atomic_load(&fb.ran) == 0 && atomic_load(&fc.ran) == 0);
      atomic_store(&gate, 1);
      const int ca = nvk_pq_claim(&a, 0, &frame, &result); nvk_pq_release(&a);
      const int cb = nvk_pq_claim(&b, 0, &frame, &result); nvk_pq_release(&b);
      const int cc = nvk_pq_claim(&c, 0, &frame, &result); nvk_pq_release(&c);
      ARM("three contexts' frames are worked first in, first out", ca == NVK_PQ_TAKEN && cb == NVK_PQ_TAKEN && cc == NVK_PQ_TAKEN &&
          fa.slot == 0 && fb.slot == 1 && fc.slot == 2);
   }

   /* --- uncertain --- */
   {
      struct nvk_pq u; memset(&u, 0, sizeof u);
      struct frame fu = { .answer = NVK_PQ_WORK_UNCERTAIN };
      nvk_pq_claim(&u, 0, &frame, &result); nvk_pq_submit(&u, &fu, work);
      frame = (void *)1;
      const int first = nvk_pq_claim(&u, 0, &frame, &result), again = nvk_pq_claim(&u, 1, &frame, &result);
      ARM("a frame whose work answers uncertain is never handed back: every later claim is refused",
          first == NVK_PQ_LOST && again == NVK_PQ_LOST && frame == NULL && u.state == NVK_PQ_UNCERTAIN && u.frame == &fu);
   }

   /* --- the wait runs out --- */
   {
      struct nvk_pq s; memset(&s, 0, sizeof s);
      atomic_int gate = 0;
      struct frame fs = { .gate = &gate, .answer = 3 };
      nvk_pq_claim(&s, 0, &frame, &result); nvk_pq_submit(&s, &fs, work);
      t0 = now_ms();
      const int lost = nvk_pq_claim(&s, 0, &frame, &result);
      const long long waited = now_ms() - t0;
      ARM("a claim gives up when the presentation thread does not answer within the limit, and takes nothing",
          lost == NVK_PQ_LOST && frame == NULL && waited >= NVK_PQ_WAIT_MS - 20 && waited < NVK_PQ_WAIT_MS + 2000 && s.state == NVK_PQ_QUEUED);
      /* the work has been stuck for longer than the limit by now: whoever comes for the frame is told at once */
      t0 = now_ms();
      const int lost_again = nvk_pq_claim(&s, 0, &frame, &result);
      ARM("a frame whose work is past the limit is given up at once by a later claim, without another wait",
          lost_again == NVK_PQ_LOST && frame == NULL && now_ms() - t0 < NVK_PQ_WAIT_MS / 2);
      atomic_store(&gate, 1);
      while (!atomic_load(&fs.finished)) sleep_ms(1);
      sleep_ms(50);
      const int late = nvk_pq_claim(&s, 0, &frame, &result);
      ARM("the frame is still that thread's until it answers; then it comes back", late == NVK_PQ_TAKEN && frame == &fs && result == 3);
      nvk_pq_release(&s);
   }

   /* --- the limit is the presentation thread's, not the claimer's --- */
   {
      struct nvk_pq q3[3]; memset(q3, 0, sizeof q3);
      struct frame f3[3] = { { .sleep_ms = NVK_PQ_WAIT_MS / 2, .answer = 1 }, { .sleep_ms = NVK_PQ_WAIT_MS / 2, .answer = 2 },
                             { .sleep_ms = NVK_PQ_WAIT_MS / 2, .answer = 3 } };
      /* the thread has had nothing to do for longer than the limit: that is not time a frame is charged with */
      sleep_ms(NVK_PQ_WAIT_MS + 50);
      for (int i = 0; i < 3; ++i) { nvk_pq_claim(&q3[i], 0, &frame, &result); nvk_pq_submit(&q3[i], &f3[i], work); }
      t0 = now_ms();
      const int last = nvk_pq_claim(&q3[2], 0, &frame, &result);
      const long long waited = now_ms() - t0;
      ARM("a frame behind two other contexts' frames, each within the limit, is waited for longer than the limit and comes back",
          last == NVK_PQ_TAKEN && frame == &f3[2] && result == 3 && waited > NVK_PQ_WAIT_MS + NVK_PQ_WAIT_MS / 4);
      const int first = nvk_pq_claim(&q3[0], 0, &frame, &result), second = nvk_pq_claim(&q3[1], 0, &frame, &result);
      ARM("and so do the two ahead of it", first == NVK_PQ_TAKEN && second == NVK_PQ_TAKEN);
      for (int i = 0; i < 3; ++i) nvk_pq_release(&q3[i]);
   }

   /* --- a queue freed as soon as its frame came back --- */
   {
      int bad = 0;
      struct frame *fr = calloc(3000, sizeof *fr);
      for (int i = 0; i < 3000 && !bad; i++) {
         struct nvk_pq *h = calloc(1, sizeof *h);
         fr[i].answer = 1;
         if (nvk_pq_claim(h, 0, &frame, &result) != NVK_PQ_TAKEN || !nvk_pq_submit(h, &fr[i], work)) bad = 1;
         if (nvk_pq_claim(h, 1, &frame, &result) != NVK_PQ_TAKEN || frame != &fr[i]) bad = 1;
         nvk_pq_release(h);
         memset(h, 0xa5, sizeof *h);   /* whoever still reads it reads nonsense */
         free(h);
      }
      ARM("3000 queues each freed right after their frame came back: the presentation thread touches none afterwards", !bad);
      free(fr);
   }

   /* --- two threads present one context --- */
   {
      enum { EACH = 4000 };
      struct nvk_pq p; memset(&p, 0, sizeof p);
      atomic_int submitted = 0, back = 0, busy = 0;
      struct frame *fr = calloc(2 * EACH, sizeof *fr);
      for (int i = 0; i < 2 * EACH; i++) fr[i].answer = 1;
      struct pair x = { &p, &submitted, &back, &busy, fr, 0, EACH }, y = { &p, &submitted, &back, &busy, fr, EACH, EACH };
      pthread_t tx, ty; void *rx, *ry;
      pthread_create(&tx, NULL, presenter, &x); pthread_create(&ty, NULL, presenter, &y);
      pthread_join(tx, &rx); pthread_join(ty, &ry);
      const int last = nvk_pq_claim(&p, 0, &frame, &result);
      if (last == NVK_PQ_TAKEN && frame) atomic_fetch_add(&back, 1);
      nvk_pq_release(&p);
      int once = 1;
      for (int i = 0; i < 2 * EACH; i++) if (atomic_load(&fr[i].ran) != 1) once = 0;
      ARM("two threads presenting one context: every frame is worked exactly once and comes back exactly once",
          !rx && !ry && once && atomic_load(&submitted) == 2 * EACH && atomic_load(&back) == 2 * EACH);
      free(fr);
   }

#ifndef PQ_NO_FORK
   /* --- the child of a fork has no presentation thread --- */
   {
      fflush(stdout);
      const pid_t pid = fork();
      if (pid == 0) {
         struct nvk_pq k; memset(&k, 0, sizeof k);
         struct frame fk = { .answer = 1 };
         int ok = nvk_pq_available() == 0;
         ok = ok && nvk_pq_claim(&k, 0, &frame, &result) == NVK_PQ_TAKEN;
         ok = ok && nvk_pq_submit(&k, &fk, work) == 0 && k.state == NVK_PQ_CLAIMED && atomic_load(&fk.ran) == 0;
         nvk_pq_release(&k);
         _exit(ok ? 0 : 1);
      }
      int status = -1;
      waitpid(pid, &status, 0);
      ARM("in a fork's child there is no presentation thread: nothing is queued and the caller stays inside",
          pid > 0 && WIFEXITED(status) && WEXITSTATUS(status) == 0);
      struct frame fp = { .answer = 5 };
      nvk_pq_claim(&q, 0, &frame, &result); nvk_pq_submit(&q, &fp, work);
      ARM("the parent's presentation thread is unaffected by the fork",
          nvk_pq_claim(&q, 0, &frame, &result) == NVK_PQ_TAKEN && frame == &fp && result == 5);
      nvk_pq_release(&q);
   }
#endif
   printf("%s\n", fails ? "FAILED" : "all arms passed");
   return fails ? 1 : 0;
}
