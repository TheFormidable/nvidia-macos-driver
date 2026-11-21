/* SPDX-License-Identifier: MIT */
#ifndef NVK_OPACITY_H
#define NVK_OPACITY_H
#include <stdatomic.h>
#include <stdint.h>
#include <time.h>

/* Whether a context's drawable is opaque, as a swap needs to know it.
 *
 * The answer is the context's kCGLCPSurfaceOpacity, and CGL asks WindowServer for it: a message and its reply. Through
 * 3.0.7 present_drawable (zink/nvk_cgl.c) asked twice per swap, once for the layer and once for the copy; in a capture
 * of a modded Minecraft pack on an RTX 5050 that was 5.7 % of the game's render thread. Since 3.0.8 a swap asks at most
 * once, and a context keeps the answer for NVK_OPACITY_KEEP_NS. It asks again at once on its first swap, after an ask
 * that failed, and after anything the CGL adapter sees change the context's output (nvk_opacity_changed). What the
 * adapter does not see is an application that sets kCGLCPSurfaceOpacity itself: the swaps of up to the next 100 ms
 * after that are composed with the old answer, where 3.0.7 used the new one at the next swap.
 *
 * This file knows no Apple type, so tools/test_zink_opacity.py runs it as it is (tools/zink_opacity_fixture.c), with
 * a clock and a reader of its own, on any machine. A struct nvk_opacity that is all zero bytes asks at its first swap,
 * so a bridge made with calloc needs nothing more. Only the thread that swaps calls nvk_opacity_get; CGL lets one
 * thread at a time use a context. Any thread may call nvk_opacity_changed. */
#define NVK_OPACITY_KEEP_NS 100000000ull   /* 100 ms */

struct nvk_opacity {
   uint64_t asked_ns;     /* when it was last asked, on the clock the caller passes */
   int opaque;            /* the answer kept: 1 opaque, 0 not */
   int kept;              /* 1 while that answer may be used; 0: the next swap asks */
   atomic_int changed;    /* 1: the context's output changed since the last swap, which therefore asks */
};

/* Asks for the context's opacity: 1 when it was answered, with *opaque set (nonzero is opaque); 0 when it failed. */
typedef int (*nvk_opacity_reader)(void *arg, int *opaque);

/* Nanoseconds on a clock that only goes forward; on macOS CLOCK_UPTIME_RAW, the clock of zink/nvk_present_queue.c. */
static inline uint64_t nvk_opacity_now_ns(void)
{
   struct timespec t;
#ifdef __APPLE__
   clock_gettime(CLOCK_UPTIME_RAW, &t);
#else
   clock_gettime(CLOCK_MONOTONIC, &t);
#endif
   return (uint64_t)t.tv_sec * 1000000000ull + (uint64_t)t.tv_nsec;
}

/* The context's output changed: the next swap asks, whatever the time. */
static inline void nvk_opacity_changed(struct nvk_opacity *o)
{
   atomic_store(&o->changed, 1);
}

/* The answer for the swap at now_ns: 1 opaque, 0 not. keep 0 (NVK_ZINK_OPACITY_KEEP=0) asks at every swap. The age
 * is the difference of two unsigned values, so a clock that wraps still measures it, and a clock that went back gives
 * a difference past any limit and so an ask, never an answer kept for good. An answer is kept only with keep 1. An ask
 * that fails answers opaque, as 3.0.7 did (its variable started at 1), and is not kept: the next swap asks again. */
static inline int nvk_opacity_get(struct nvk_opacity *o, int keep, uint64_t now_ns, nvk_opacity_reader read, void *arg)
{
   if (atomic_load(&o->changed) && atomic_exchange(&o->changed, 0)) o->kept = 0;
   if (o->kept && now_ns - o->asked_ns < NVK_OPACITY_KEEP_NS) return o->opaque;
   int opaque = 1;
   const int answered = read(arg, &opaque);
   o->opaque = !answered || opaque != 0;
   o->kept = answered && keep;
   o->asked_ns = now_ns;
   return o->opaque;
}
#endif
