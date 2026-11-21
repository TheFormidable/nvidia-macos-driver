/* SPDX-License-Identifier: MIT */
#include "nvk_present_queue.h"
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <time.h>
#ifdef __APPLE__
#include <pthread/qos.h>
#endif

/* The rules are in nvk_present_queue.h. */
static pthread_mutex_t pq_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t pq_changed = PTHREAD_COND_INITIALIZER;   /* a queue changed state: claimers look again */
static pthread_cond_t pq_work = PTHREAD_COND_INITIALIZER;      /* a frame was queued: the presentation thread */
static struct nvk_pq *pq_head, *pq_tail;
static int pq_thread_state;   /* 0 not tried, 1 running, -1 none (it could not start, or this is a fork's child) */
/* One thread works the frames of every context in turn, so how long a claimer has waited says nothing about a frame
 * that stood behind another's. What is timed is that thread: when it last finished a frame (it takes the next one at
 * once) or was handed one while it had none. A frame is given up when that is NVK_PQ_WAIT_MS ago. */
static long long pq_progress_ms;
static int pq_working;        /* the presentation thread is inside a frame's work */

/* Milliseconds on a clock that does not move while the machine sleeps: a wait that began before a sleep must not be
 * found to have run out on wake. */
static long long pq_now_ms(void)
{
   struct timespec t;
#ifdef __APPLE__
   clock_gettime(CLOCK_UPTIME_RAW, &t);
#else
   clock_gettime(CLOCK_MONOTONIC, &t);
#endif
   return (long long)t.tv_sec * 1000 + t.tv_nsec / 1000000;
}

static void *pq_main(void *unused)
{
   (void)unused;
#ifdef __APPLE__
   pthread_setname_np("nvk.zink.present");
#endif
   pthread_mutex_lock(&pq_lock);
   for (;;) {
      while (!pq_head) pthread_cond_wait(&pq_work, &pq_lock);
      struct nvk_pq *q = pq_head;
      pq_head = q->next;
      if (!pq_head) pq_tail = NULL;
      q->next = NULL;
      void *frame = q->frame;
      int (*work)(void *) = q->work;
      pq_working = 1;
      pthread_mutex_unlock(&pq_lock);
      int result = work(frame);
      pthread_mutex_lock(&pq_lock);
      pq_working = 0;
      pq_progress_ms = pq_now_ms();
      q->result = result;
      q->state = result == NVK_PQ_WORK_UNCERTAIN ? NVK_PQ_UNCERTAIN : NVK_PQ_DONE;
      /* Woken with the lock held, so a claimer cannot free the queue between the store and the wake. From here on
       * the queue is its owner's again and is not read here. */
      pthread_cond_broadcast(&pq_changed);
   }
   return NULL;
}

/* The child of a fork has the parent's memory and none of its threads. Its copy of the lock may be held by a thread
 * that does not exist there, so it gets a new one; frames the parent had in flight are not the child's to finish. */
static void pq_child(void)
{
   pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
   pthread_cond_t changed = PTHREAD_COND_INITIALIZER, work = PTHREAD_COND_INITIALIZER;
   pq_lock = lock; pq_changed = changed; pq_work = work;
   pq_head = pq_tail = NULL;
   pq_working = 0;
   pq_thread_state = -1;
}

/* The lock is held. */
static void pq_start(void)
{
   pq_thread_state = -1;
   pthread_attr_t attr;
   if (pthread_attr_init(&attr)) return;
   pthread_attr_setdetachstate(&attr, PTHREAD_CREATE_DETACHED);
#ifdef __APPLE__
   /* The frame is on its way to the screen: this thread's work is what the user is looking at. */
   pthread_attr_set_qos_class_np(&attr, QOS_CLASS_USER_INTERACTIVE, 0);
#endif
   /* The thread belongs to no runtime of the application (a JVM's signal handlers are not written for it), so it
    * starts with every signal blocked that is sent to a process; the mask of the calling thread is put back. The
    * signals of a fault stay open: blocked, a fault on this thread would not end the process but fault again for
    * ever, and what is a crash report on any other thread would look like a GPU that hangs. */
   sigset_t all, saved;
   sigfillset(&all);
   sigdelset(&all, SIGSEGV); sigdelset(&all, SIGBUS); sigdelset(&all, SIGILL);
   sigdelset(&all, SIGFPE); sigdelset(&all, SIGTRAP); sigdelset(&all, SIGSYS);
   pthread_t thread;
   if (pthread_sigmask(SIG_SETMASK, &all, &saved) == 0) {
      if (pthread_create(&thread, &attr, pq_main, NULL) == 0) {
         pq_thread_state = 1;
         pthread_atfork(NULL, NULL, pq_child);
      }
      pthread_sigmask(SIG_SETMASK, &saved, NULL);
   }
   pthread_attr_destroy(&attr);
   if (pq_thread_state != 1) fprintf(stderr, "NVK Zink: no presentation thread; frames are presented by the thread that swaps\n");
}

int nvk_pq_available(void)
{
   pthread_mutex_lock(&pq_lock);
   if (!pq_thread_state) pq_start();
   int available = pq_thread_state == 1;
   pthread_mutex_unlock(&pq_lock);
   return available;
}

/* Sleep on pq_changed for at most `ms` milliseconds. The lock is held. The condition variable's own clock is the
 * wall clock, which a sleep or a time correction moves: each sleep is short, and the caller decides on pq_now_ms. */
static void pq_sleep(long long ms)
{
   struct timespec until;
   clock_gettime(CLOCK_REALTIME, &until);
   if (ms > 250) ms = 250;
   until.tv_nsec += (long)ms * 1000000;
   if (until.tv_nsec >= 1000000000) { until.tv_sec += 1; until.tv_nsec -= 1000000000; }
   pthread_cond_timedwait(&pq_changed, &pq_lock, &until);
}

int nvk_pq_claim(struct nvk_pq *q, int wait_for_owner, void **frame, int *result)
{
   *frame = NULL; *result = 0;
   const long long began = pq_now_ms();
   int claimed = NVK_PQ_TAKEN;
   pthread_mutex_lock(&pq_lock);
   for (;;) {
      if (q->state == NVK_PQ_FREE) break;
      if (q->state == NVK_PQ_DONE) { *frame = q->frame; *result = q->result; break; }
      if (q->state == NVK_PQ_UNCERTAIN) { claimed = NVK_PQ_LOST; break; }
      if (q->state == NVK_PQ_CLAIMED && (!wait_for_owner || pthread_equal(q->owner, pthread_self()))) {
         claimed = NVK_PQ_BUSY; break;
      }
      /* A frame in flight is the presentation thread's: it is given up when that thread has been at one frame for
       * the whole time, however long or short this caller has waited. Another thread's claim is timed from when
       * this caller began to wait for it. */
      const long long since = q->state == NVK_PQ_QUEUED ? pq_progress_ms : began;
      const long long left = since + NVK_PQ_WAIT_MS - pq_now_ms();
      if (left <= 0) { claimed = NVK_PQ_LOST; break; }
      pq_sleep(left);
   }
   if (claimed == NVK_PQ_TAKEN) {
      q->state = NVK_PQ_CLAIMED;
      q->owner = pthread_self();
      q->frame = NULL; q->work = NULL;
   }
   pthread_mutex_unlock(&pq_lock);
   return claimed;
}

void nvk_pq_release(struct nvk_pq *q)
{
   pthread_mutex_lock(&pq_lock);
   q->state = NVK_PQ_FREE;
   pthread_cond_broadcast(&pq_changed);
   pthread_mutex_unlock(&pq_lock);
}

int nvk_pq_submit(struct nvk_pq *q, void *frame, int (*work)(void *frame))
{
   pthread_mutex_lock(&pq_lock);
   if (!pq_thread_state) pq_start();
   const int queued = pq_thread_state == 1;
   if (queued) {
      q->frame = frame; q->work = work; q->next = NULL;
      q->state = NVK_PQ_QUEUED;
      if (!pq_head && !pq_working) pq_progress_ms = pq_now_ms();   /* an idle thread is handed its next frame */
      if (pq_tail) pq_tail->next = q; else pq_head = q;
      pq_tail = q;
      pthread_cond_signal(&pq_work);
   }
   pthread_mutex_unlock(&pq_lock);
   return queued;
}
