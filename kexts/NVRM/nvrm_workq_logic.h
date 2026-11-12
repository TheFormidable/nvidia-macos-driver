/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

/* nvrm_workq_logic.h - the bookkeeping of one serialized work queue, without any kernel call: a FIFO of pending items,
 * a fixed pool of list nodes so that queueing never has to allocate, and the two sequence numbers a flush compares.
 * nv_xnu_kcore.cpp wraps it with a lock, a worker thread and the waits; tools/test_nvrm_workq_logic.c runs this same
 * code on the host.
 *
 * Rules for the caller: every function here is called with the queue's lock held. A node handed out by
 * nvwq_dequeue() goes back through nvwq_complete() exactly once, after the item's callback has returned.
 *
 * Why a flush is two snapshots of enq_seq: there is one worker, it runs items in the order they were accepted, so
 * done_seq reaches a value t exactly when every item accepted before enq_seq was t has finished. An item that queues
 * another while it runs is not covered by the first snapshot; the second, taken once the first is reached, covers
 * it, and the flush still ends even when an item re-queues itself for ever (Linux's nv_kthread_q_flush does the
 * same). Plain C, compiles as C99 and as C++. */
#ifndef NVRM_WORKQ_LOGIC_H
#define NVRM_WORKQ_LOGIC_H

#define NVWQ_STATIC_NODES 256

/* OFF: no worker; RUN: accepting and running; STOPPING: refusing, the worker drains what is pending and exits;
 * LEGACY: the queues are switched off, the caller runs the older one-thread-per-item code instead */
enum { NVWQ_OFF = 0, NVWQ_RUN = 1, NVWQ_STOPPING = 2, NVWQ_LEGACY = 3 };

struct nvwq_node { struct nvwq_node *next; void *item; unsigned char heap; };

struct nvwq_core {
    struct nvwq_node *head, *tail;       /* pending items, oldest first */
    struct nvwq_node *freelist;          /* static nodes not in use */
    unsigned long long enq_seq;          /* items accepted so far */
    unsigned long long done_seq;         /* items whose callback has returned */
    unsigned depth, max_depth;           /* pending + running, and the most there ever were */
    unsigned unload_flushers;            /* flushes that belong to a teardown, in progress */
    unsigned flush_waiters;              /* flushes asleep, waiting for done_seq to move */
    unsigned char state;
    struct nvwq_node nodes[NVWQ_STATIC_NODES];
};

/* All static nodes on the free list, counters 0, state OFF. */
static inline void nvwq_core_init(struct nvwq_core *c)
{
    unsigned i;
    c->head = c->tail = 0;
    c->freelist = 0;
    for (i = NVWQ_STATIC_NODES; i-- > 0; ) {
        c->nodes[i].item = 0;
        c->nodes[i].heap = 0;
        c->nodes[i].next = c->freelist;
        c->freelist = &c->nodes[i];
    }
    c->enq_seq = c->done_seq = 0;
    c->depth = c->max_depth = 0;
    c->unload_flushers = c->flush_waiters = 0;
    c->state = NVWQ_OFF;
}

/* Queue `item` at the tail.
 *   0  queued (a static node, or `spare` when none was left: then *spare_used is 1 and the node is the queue's until
 *      nvwq_complete() hands it back)
 *   1  no static node left and no spare given: nothing queued; the caller may retry with a node it allocated
 *   2  the queue is not accepting (any state but RUN): nothing queued
 * `spare_used` may be NULL when `spare` is NULL. */
static inline int nvwq_enqueue(struct nvwq_core *c, void *item, struct nvwq_node *spare, int *spare_used)
{
    struct nvwq_node *n;
    if (spare_used) *spare_used = 0;
    if (c->state != NVWQ_RUN) return 2;
    n = c->freelist;
    if (n) {
        c->freelist = n->next;
        n->heap = 0;
    } else if (spare) {
        n = spare;
        n->heap = 1;
        if (spare_used) *spare_used = 1;
    } else {
        return 1;
    }
    n->item = item;
    n->next = 0;
    if (c->tail) c->tail->next = n; else c->head = n;
    c->tail = n;
    c->enq_seq++;
    c->depth++;
    if (c->depth > c->max_depth) c->max_depth = c->depth;
    return 0;
}

/* The oldest pending item's node, taken off the list (it still counts in depth until completed); NULL when nothing
 * is pending. done_seq is not touched: the item has not run yet. */
static inline struct nvwq_node *nvwq_dequeue(struct nvwq_core *c)
{
    struct nvwq_node *n = c->head;
    if (!n) return 0;
    c->head = n->next;
    if (!c->head) c->tail = 0;
    n->next = 0;
    return n;
}

/* The item of `n` has run: done_seq++, depth--. A static node goes back on the free list and NULL is returned; a node
 * that came in as a spare is returned for the caller to free (after it has dropped the lock). */
static inline struct nvwq_node *nvwq_complete(struct nvwq_core *c, struct nvwq_node *n)
{
    c->done_seq++;
    if (c->depth) c->depth--;
    n->item = 0;
    if (n->heap) return n;
    n->next = c->freelist;
    c->freelist = n;
    return 0;
}

/* A flush takes a target, then waits until it is flushed (see the head comment for why it takes two). */
static inline unsigned long long nvwq_flush_target(const struct nvwq_core *c) { return c->enq_seq; }
static inline int nvwq_flushed(const struct nvwq_core *c, unsigned long long t) { return c->done_seq >= t; }

#endif
