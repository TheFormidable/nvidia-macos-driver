/* SPDX-License-Identifier: MIT */
#ifndef NVKZINK_PRESENT_H
#define NVKZINK_PRESENT_H
#include <IOSurface/IOSurface.h>
struct pipe_screen;
struct pipe_resource;
/* Synchronous copy on the Zink device. 1 = published to the surface, 0 = safe
 * failure, -1 = uncertain completion (caller must quarantine its screen). */
int nvk_zink_copy_vram(struct pipe_screen *, struct pipe_resource *, IOSurfaceRef);

/* One frame ahead (nvk_present.c). A context has one queue (zero is its idle state) and, from its first frame, one
 * nvk_present_ahead, which is read and written only by the thread inside the queue. */
struct nvk_pq;
struct nvk_present_ahead;
struct pipe_fence_handle;
/* Called once for a frame that was handed over: present the drawable if publish is 1, release it either way.
 * Returns 1 if it was presented. It runs on the presentation thread; in a process that has none (the child of a
 * fork), on the thread that swapped, before the swap returns. */
typedef int (*nvk_zink_frame_done)(void *arg, int publish);
/* NVK_ZINK_PRESENT_AHEAD is not 0. */
int nvk_present_ahead_enabled(void);
/* Enter the queue for a new frame, after the one in flight has been published. 1 = inside (leave with cancel or
 * submit); 0 = another thread is presenting this context; -1 = uncertain; -2 = not on this screen or in this process:
 * present with nvk_zink_copy_vram. */
int nvk_present_ahead_begin(struct nvk_pq *, struct nvk_present_ahead **, struct pipe_screen *, int *uncertain);
void nvk_present_ahead_cancel(struct nvk_pq *);
/* Send the copy of `image` (whose blit the caller has flushed; zink_fence is that flush's fence) into `surface` and
 * leave the queue.
 *    2  the frame was handed over (and, if synchronous or an observer is set, it has been published)
 *    3  handed over, waited for, and it was not published
 *   -1  uncertain: nothing may be freed
 *   -2  nothing was submitted or kept: finish this frame with nvk_zink_copy_vram
 * The fence and `done`'s frame belong to the queue for 2, 3 and -1, and stay the caller's for -2. */
int nvk_present_ahead_submit(struct nvk_pq *, struct nvk_present_ahead *, struct pipe_resource *image, IOSurfaceRef surface,
                             struct pipe_fence_handle *zink_fence, int synchronous, nvk_zink_frame_done done, void *arg);
/* Wait for the frame in flight. 1 = none, or it was published; 2 = it was not published; 0 = uncertain. */
int nvk_present_ahead_drain(struct nvk_pq *, int *uncertain);
/* Let the kept imports go: the context has no drawable to present into now. */
void nvk_present_ahead_trim(struct nvk_pq *, struct nvk_present_ahead **, int *uncertain);
/* Wait for the frame in flight and for a thread that is presenting, then free everything. 0 = uncertain: kept. */
int nvk_present_ahead_destroy(struct nvk_pq *, struct nvk_present_ahead **, int *uncertain);
#endif
