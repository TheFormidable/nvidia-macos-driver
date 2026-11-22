/* SPDX-License-Identifier: MIT */
#ifndef NVK_NEWEST_H
#define NVK_NEWEST_H
#include <pthread.h>
#include <stdbool.h>

/* The newest frame, shown by a thread of its own (3.0.11).
 *
 * Through 3.0.9 a swap of an OpenGL context on Zink asked the window's layer for its next drawable itself, and the
 * layer hands one out only when the compositor has given one back: with the swap interval at 0 the application still
 * drew no faster than about two frames for each composition of the display. Since 3.0.11 such a swap copies the frame
 * into the context's scratch image, leaves it there ("parks" it) and returns; a sender thread asks the layer, and what
 * it sends when the layer answers is whatever frame is parked then, the newest. A frame that a newer one replaced
 * before the layer had room was never shown; the last frame an application draws always is, because the sender runs
 * until nothing is parked.
 *
 * This file is the agreement between the thread that swaps and the sender, and nothing else: it knows no Apple, Mesa
 * or Vulkan type, so tools/test_zink_newest.py runs it as it is, under the thread sanitizer, on any machine. The parked
 * frame itself belongs to its owner (struct nvk_zink_context, zink/nvk_zink.c), behind a lock of its own that is
 * never held while this one is asked for; `parked` below reads it.
 *
 * A struct nvk_newest that is all zero bytes is one that was never used: nvk_newest_kick prepares it. */
struct nvk_newest {
   pthread_mutex_t lock;
   pthread_cond_t idle;
   bool ready;     /* lock and idle exist. Written by the thread that swaps, before the first sender is started. */
   bool sending;   /* a sender has been started and has not left */
   bool stop;      /* nvk_newest_stop waits for the sender to leave */
   int failed;     /* what the sender's last frame came to, when it was not sent: the next swap is told */
};

struct nvk_newest_ops {
   /* The sequence number of the frame that is parked now (they count from 1), or 0 when none is. Called with the
    * lock held. */
   unsigned (*parked)(void *arg);
   /* Send the parked frame; `seen` is what parked() answered. It may wait for the layer and for the GPU. 0 = go on
    * (it was sent, or it is no longer wanted); anything else = stop, and the next swap presents by itself. Called
    * with no lock held. */
   int (*send)(void *arg, unsigned seen);
   /* Run nvk_newest_run(n, ops, arg) on another thread. */
   void (*start)(void *arg);
};

/* The thread that swaps, after it parked a frame. 1 = a sender is on its way to it; 0 = the sender gave up (the
 * frame is still parked, and its swap has to present it by itself, after nvk_newest_stop). */
static inline int nvk_newest_kick(struct nvk_newest *n, const struct nvk_newest_ops *ops, void *arg)
{
   if (!n->ready) {
      if (pthread_mutex_init(&n->lock, NULL)) return 0;
      if (pthread_cond_init(&n->idle, NULL)) { pthread_mutex_destroy(&n->lock); return 0; }
      n->ready = true;
   }
   pthread_mutex_lock(&n->lock);
   const int going = !n->failed;
   if (going && !n->sending) {
      /* Marked before the sender exists: a stop that comes now waits for it. */
      n->sending = true;
      ops->start(arg);
   }
   pthread_mutex_unlock(&n->lock);
   return going;
}

/* The sender. It leaves when nothing is parked, when it is told to stop and when a frame could not be sent; a frame
 * parked after it looked is the next kick's, which finds `sending` cleared under the same lock. */
static inline void nvk_newest_run(struct nvk_newest *n, const struct nvk_newest_ops *ops, void *arg)
{
   pthread_mutex_lock(&n->lock);
   for (;;) {
      const unsigned seen = n->stop || n->failed ? 0 : ops->parked(arg);
      if (!seen) break;
      pthread_mutex_unlock(&n->lock);
      const int failed = ops->send(arg, seen);
      pthread_mutex_lock(&n->lock);
      if (failed) n->failed = failed;
   }
   n->sending = false;
   pthread_cond_broadcast(&n->idle);
   pthread_mutex_unlock(&n->lock);
}

/* Any thread that is about to present, resize or destroy the context by itself: wait until no sender is at work.
 * A frame it was sending is sent; one still parked stays parked, for the caller to drop. Returns what the sender
 * gave up with (0 = it did not), and forgets it. */
static inline int nvk_newest_stop(struct nvk_newest *n)
{
   if (!n->ready) return 0;
   pthread_mutex_lock(&n->lock);
   n->stop = true;
   while (n->sending) pthread_cond_wait(&n->idle, &n->lock);
   n->stop = false;
   const int failed = n->failed;
   n->failed = 0;
   pthread_mutex_unlock(&n->lock);
   return failed;
}

/* After nvk_newest_stop, by whoever frees the memory. */
static inline void nvk_newest_destroy(struct nvk_newest *n)
{
   if (!n->ready) return;
   pthread_cond_destroy(&n->idle);
   pthread_mutex_destroy(&n->lock);
   n->ready = false;
}
#endif
