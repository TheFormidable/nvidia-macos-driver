/* SPDX-License-Identifier: MIT */
#ifndef NVK_ZINK_PRIVATE_H
#define NVK_ZINK_PRIVATE_H
#include "nvk_zink.h"
#include "nvk_present_queue.h"
#include "state_tracker/st_context.h"
#include "pipe/p_screen.h"
#include <pthread.h>
#include <stdint.h>
#include <unistd.h>

struct nvk_iosurface_group;
struct nvk_zink_screen {
   struct pipe_frontend_screen fscreen;
   int refs;
   int uncertain;
};
struct nvk_zink_context {
   struct pipe_frontend_drawable drawable;
   struct nvk_zink_screen *screen;
   /* One reference for the owner (CGL, until nvk_zink_destroy), one for each
    * thread on which the context is Mesa-current, and one for each caller that
    * holds it across a bind (nvk_zink_ref). Freed when the last one drops. */
   int refs;
   struct st_visual visual;
   struct st_context *st;
   struct pipe_resource *attachments[ST_ATTACHMENT_COUNT];
   unsigned width, height;
   bool double_buffer;
   bool front_flush_pending, front_flush_active;
   struct pipe_resource *native_image;
   /* One frame ahead (nvk_present.h). The queue says who may touch `ahead` and the frame in flight.
    * present_refusals counts frames in a row that NVK would not import that way; at three, present_old_only sends
    * every later frame through nvk_zink_copy_vram until the context's presentation state is released. */
   struct nvk_pq present_queue;
   struct nvk_present_ahead *ahead;
   unsigned present_refusals, present_late;
   bool present_old_only;
   /* The newest frame, parked for a sender (zink/nvk_newest.h, 3.0.11). `lock` guards image, layout, seq and opaque,
    * and it guards the scratch image against every thread but the one that draws: that thread blits and flushes
    * into the image with the lock held, and a sender holds it from taking the frame until its copy is on the
    * queue. throttle and drawn are the drawing thread's own. pid is the process the lock was made in: the child of
    * a fork leaves it alone (parked_lock, zink/nvk_zink.c). */
   struct {
      pthread_mutex_t lock;
      struct pipe_resource *image;   /* the scratch image that holds the frame, referenced; NULL = nothing parked */
      unsigned layout, seq;
      int opaque;
      /* The frame before this one went out one frame ahead, from a swap that presented by itself, and nothing has
       * said since that the context has no drawable: the next swap may park. Read and written with p_atomic. */
      int armed;
      struct pipe_fence_handle *throttle;   /* the flush of the frame parked before the newest */
      uint64_t drawn, sent;                 /* sent is written with p_atomic_inc */
      pid_t pid;
   } parked;
   struct {
      IOSurfaceRef surface;
      struct pipe_resource *buffer, *image;
      struct pipe_fence_handle *fence;
   } present[2];
   struct nvk_iosurface_group *iosurfaces;
   struct pipe_resource *iosurface_staging;
};
int nvk_iosurface_group_init(struct nvk_zink_context *, struct nvk_zink_context *share);
int nvk_iosurface_group_release(struct nvk_zink_context *);
#endif
