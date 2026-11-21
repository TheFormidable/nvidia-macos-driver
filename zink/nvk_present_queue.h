/* SPDX-License-Identifier: MIT */
#ifndef NVKZINK_PRESENT_QUEUE_H
#define NVKZINK_PRESENT_QUEUE_H
#include <pthread.h>

/* One frame ahead.
 *
 * A context that presents hands its finished frame to one thread of the process, the presentation thread, which
 * waits for the GPU and then publishes the frame. The thread that swapped goes on to the next frame; what it may not
 * do is start a second one before the first has been published. This file is that rule and nothing else: who owns
 * the one frame a context may have in flight, and who may wait for whom. It knows no Vulkan, no Mesa and no Apple
 * type, so tools/test_zink_present_queue.py runs it as it is (tools/zink_present_queue_fixture.c), with threads, on
 * any machine.
 *
 * A queue is in one of five states, read and written only under one lock of the process:
 *
 *   FREE       nothing in flight, nobody inside
 *   CLAIMED    one thread (owner) is inside: it is building a frame, or tidying up after one, or tearing down
 *   QUEUED     a frame is with the presentation thread; frame and work are that thread's until it answers
 *   DONE       the presentation thread answered (result); the next claimer takes the frame back to tidy up
 *   UNCERTAIN  the presentation thread answered that the GPU did not finish; nothing of the frame may be freed
 *
 * The lock and its condition variables belong to the process and are never destroyed. The presentation thread reads
 * a queue's frame and work under the lock, runs the work without it, and stores the answer under it; after that
 * store it does not touch the queue again, so the owner of a queue that is not QUEUED may free it. */
enum { NVK_PQ_FREE = 0, NVK_PQ_CLAIMED, NVK_PQ_QUEUED, NVK_PQ_DONE, NVK_PQ_UNCERTAIN };

/* What the work answers on the presentation thread. Any other value is the caller's own and is handed back as it is
 * by the next claim. */
#define NVK_PQ_WORK_UNCERTAIN (-1)

struct nvk_pq {
   int state;
   pthread_t owner;            /* CLAIMED: the thread inside */
   void *frame;                /* QUEUED and DONE */
   int (*work)(void *frame);   /* QUEUED */
   int result;                 /* DONE */
   struct nvk_pq *next;        /* QUEUED and not yet taken by the presentation thread */
};

/* How long the presentation thread may go without taking or finishing a frame before a claim that waits for a frame
 * in flight gives up, and how long a claim waits for another thread's claim. One frame's work waits up to 5 s for
 * the GPU, and up to 5 s more for a frame it has to send again; the rest is the kernel's mark and Apple's present.
 * The thread works the frames of every context in turn, so the time is the thread's and not the claimer's: a frame
 * that stands behind another context's slow one is not given up while that one is within its time, and a frame
 * whose work has been stuck for longer is given up at once by whoever comes for it. The time is measured on a clock
 * that stands still while the machine sleeps. */
#ifndef NVK_PQ_WAIT_MS
#define NVK_PQ_WAIT_MS 12000
#endif

/* 1 when this process has a presentation thread. It is started by the first call. 0 in a process that could not
 * start one, and in the child of a fork (the thread did not come along). */
int nvk_pq_available(void);

/* Take the queue for the calling thread, waiting first for a frame that is in flight.
 *   NVK_PQ_TAKEN  the caller is inside. *frame is the frame that had finished and *result its work's answer, for the
 *                 caller to tidy up; *frame is NULL when there was none.
 *   NVK_PQ_BUSY   another thread is inside (wait_for_owner 0), or the caller itself is: nothing was taken.
 *   NVK_PQ_LOST   the frame in flight is UNCERTAIN, or the wait ran out. Nothing was taken and nothing may be freed.
 * wait_for_owner 1 also waits for another thread's claim to end, for a teardown that must not run inside a present. */
enum { NVK_PQ_TAKEN = 1, NVK_PQ_BUSY = 0, NVK_PQ_LOST = -1 };
int nvk_pq_claim(struct nvk_pq *q, int wait_for_owner, void **frame, int *result);

/* Leave the queue with nothing in flight. Only the thread inside calls it. */
void nvk_pq_release(struct nvk_pq *q);

/* Hand the frame to the presentation thread, which calls work(frame) once, without the lock. Only the thread inside
 * calls it, and it is no longer inside when this returns 1. 0: there is no presentation thread; the caller is still
 * inside and nothing was queued. */
int nvk_pq_submit(struct nvk_pq *q, void *frame, int (*work)(void *frame));
#endif
