/* SPDX-License-Identifier: MIT */
/* Drives zink/nvk_newest.h as it is, with a parked frame, a sender and a layer of its own. tools/test_zink_newest.py
 * builds and runs it; it says what each arm is for. */
#include "nvk_newest.h"
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>

static struct nvk_newest newest;
static pthread_mutex_t frame_lock = PTHREAD_MUTEX_INITIALIZER;   /* the owner's lock on the parked frame */
static unsigned parked_seq, next_seq, last_sent;
static atomic_uint senders, in_send, sends, starts;
static atomic_int gate_closed, fail_next, went_back, two_at_once;
static unsigned send_delay_us;

static void nap(unsigned us) { struct timespec t = {0, (long)us * 1000}; nanosleep(&t, NULL); }

static unsigned parked(void *arg)
{
   (void)arg;
   pthread_mutex_lock(&frame_lock);
   const unsigned seq = parked_seq;
   pthread_mutex_unlock(&frame_lock);
   return seq;
}

static int send(void *arg, unsigned seen)
{
   (void)arg; (void)seen;
   if (atomic_fetch_add(&in_send, 1)) atomic_store(&two_at_once, 1);
   if (send_delay_us) nap((unsigned)rand() % send_delay_us);   /* the layer */
   pthread_mutex_lock(&frame_lock);
   const unsigned taken = parked_seq;
   parked_seq = 0;
   if (taken) {
      if (taken <= last_sent) atomic_store(&went_back, 1);
      last_sent = taken;
      atomic_fetch_add(&sends, 1);
   }
   pthread_mutex_unlock(&frame_lock);
   while (atomic_load(&gate_closed)) nap(200);                  /* the GPU */
   atomic_fetch_sub(&in_send, 1);
   return atomic_exchange(&fail_next, 0);
}

static const struct nvk_newest_ops ops;
static void *sender(void *arg)
{
   if (atomic_fetch_add(&senders, 1)) atomic_store(&two_at_once, 1);
   nvk_newest_run(&newest, &ops, arg);
   atomic_fetch_sub(&senders, 1);
   return NULL;
}
static void start(void *arg)
{
   pthread_t thread;
   atomic_fetch_add(&starts, 1);
   if (pthread_create(&thread, NULL, sender, arg)) abort();
   pthread_detach(thread);
}
static const struct nvk_newest_ops ops = { parked, send, start };

static unsigned park(void)
{
   pthread_mutex_lock(&frame_lock);
   const unsigned seq = parked_seq = ++next_seq;
   pthread_mutex_unlock(&frame_lock);
   return seq;
}
static unsigned sent(void)
{
   pthread_mutex_lock(&frame_lock);
   const unsigned seq = last_sent;
   pthread_mutex_unlock(&frame_lock);
   return seq;
}
static int wait_sent(unsigned seq, unsigned ms)
{
   for (unsigned i = 0; i < ms * 5; ++i) { if (sent() == seq) return 1; nap(200); }
   return sent() == seq;
}
static int fails;
static void arm(const char *what, int ok)
{
   printf("%s %s\n", ok ? "ok  " : "FAIL", what);
   fails += !ok;
}

static atomic_int stop_returned;
static int stop_answer;
static void *stopper(void *arg)
{
   (void)arg;
   stop_answer = nvk_newest_stop(&newest);
   atomic_store(&stop_returned, 1);
   return NULL;
}

int main(void)
{
   pthread_t thread;
   arm("a stop of one that was never used returns at once", nvk_newest_stop(&newest) == 0);

   /* the frame parked while another is being sent is sent too, by the same sender */
   atomic_store(&gate_closed, 1);
   const unsigned first = park();
   arm("the first kick starts a sender", nvk_newest_kick(&newest, &ops, NULL) == 1 && wait_sent(first, 2000));
   const unsigned second = park();
   arm("a kick while the sender is at work starts no second one",
       nvk_newest_kick(&newest, &ops, NULL) == 1 && atomic_load(&starts) == 1);
   nap(20000);
   arm("the frame parked meanwhile waits for the one being sent", sent() == first);
   atomic_store(&gate_closed, 0);
   arm("and is sent when that one is done: the last frame drawn is shown", wait_sent(second, 2000));

   /* stop: waits for the frame being sent, and leaves a parked one parked */
   arm("a stop with the sender idle returns no failure", nvk_newest_stop(&newest) == 0);
   atomic_store(&gate_closed, 1);
   const unsigned third = park();
   nvk_newest_kick(&newest, &ops, NULL);
   wait_sent(third, 2000);
   const unsigned fourth = park();
   nvk_newest_kick(&newest, &ops, NULL);
   pthread_create(&thread, NULL, stopper, NULL);
   nap(50000);
   arm("a stop waits while a frame is being sent", !atomic_load(&stop_returned));
   atomic_store(&gate_closed, 0);
   pthread_join(thread, NULL);
   arm("a stop returns when it was sent, with no sender at work", atomic_load(&stop_returned) && !atomic_load(&senders) && !atomic_load(&in_send));
   arm("the frame that was parked behind it is still parked, not sent", parked(NULL) == fourth && sent() == third);
   arm("the next kick sends it", nvk_newest_kick(&newest, &ops, NULL) == 1 && wait_sent(fourth, 2000));

   /* a sender that gives up: the swap is told, until it has stopped it */
   nvk_newest_stop(&newest);
   atomic_store(&fail_next, 7);
   const unsigned fifth = park();
   nvk_newest_kick(&newest, &ops, NULL);
   wait_sent(fifth, 2000);
   for (unsigned i = 0; i < 10000 && atomic_load(&senders); ++i) nap(200);
   const unsigned sixth = park();
   const unsigned before = atomic_load(&starts);
   arm("after a sender gave up a kick starts none and says so", nvk_newest_kick(&newest, &ops, NULL) == 0 && atomic_load(&starts) == before);
   nap(20000);
   arm("the frame stays parked for the swap to present", parked(NULL) == sixth);
   arm("stop answers what the sender gave up with, once", nvk_newest_stop(&newest) == 7 && nvk_newest_stop(&newest) == 0);
   arm("and the next kick has a sender again", nvk_newest_kick(&newest, &ops, NULL) == 1 && wait_sent(sixth, 2000));

   /* many frames at an uneven pace against a slow layer, with stops in between */
   send_delay_us = 300;
   unsigned last = 0;
   int stops_clean = 1;
   for (unsigned i = 0; i < 20000; ++i) {
      last = park();
      if (!nvk_newest_kick(&newest, &ops, NULL)) abort();
      if (i % 7 == 0) nap((unsigned)rand() % 400);
      if (i % 997 == 0) {
         nvk_newest_stop(&newest);
         if (atomic_load(&senders) || atomic_load(&in_send)) stops_clean = 0;
         nvk_newest_kick(&newest, &ops, NULL);
      }
   }
   arm("20000 frames: the last one is sent", wait_sent(last, 5000));
   arm("20000 frames: every stop found no sender at work", stops_clean);
   arm("20000 frames: fewer were sent than drawn, and none out of order", atomic_load(&sends) < next_seq && !atomic_load(&went_back));
   arm("never two senders, never two sends at once", !atomic_load(&two_at_once));
   nvk_newest_stop(&newest);
   nvk_newest_destroy(&newest);
   printf(fails ? "%d arms failed\n" : "all arms passed\n", fails);
   return fails != 0;
}
