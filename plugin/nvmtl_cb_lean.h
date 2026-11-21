/* nvmtl_cb_lean.h - what a command buffer makes up front and what it makes when first needed.
   Apple's OpenGL renderer makes on the order of a hundred command buffers a frame, so what each one costs before any
   command is recorded is paid that often on the game's render thread. Two of those costs are only needed sometimes:
     - the scheduled-handler and completed-handler dispatch groups. A group is entered only when handlers are dispatched
       (nvmtlDispatchHandlers, always under the command buffer's _completionCondition lock) and waited for only in
       -waitUntilScheduled / -waitUntilCompleted. A group nobody entered has nothing to wait for, so it is made under
       that lock just before its first enter, and a waiter reads it under the same lock: nil means no handler of that
       kind was ever dispatched, which is exactly the case where waiting on a group made up front returned at once.
     - the queue's capacity semaphore, made at the first -commandBuffer under the command-queue class lock, which every
       later -commandBuffer took again only to read it. It is made with the queue instead, before the queue is handed
       out, so -commandBuffer reads it without that lock; the semaphore and its count are the same.
   NVMTL_CB_LEAN=0: both groups are made with every command buffer, and every -commandBuffer takes the class lock to
   find the semaphore, as in 3.0.5. The rules are pure so tools/test_cb_lean.c proves them without Metal. */
#ifndef NVMTL_CB_LEAN_H
#define NVMTL_CB_LEAN_H

/* the way out: on unless the variable starts with 0, as the neighbouring commit switches read theirs */
static inline int nvmtl_cb_lean_env_on(const char *value)
{
    return !(value && value[0] == '0');
}

/* -init makes the two dispatch groups only with the saving off */
static inline int nvmtl_cb_groups_at_init(int lean)
{
    return !lean;
}

/* nvmtlDispatchHandlers makes a group right before its first enter: when there is a handler to dispatch and no group
   yet. With no handler it returns before touching any group, as it always did. */
static inline int nvmtl_cb_group_make_now(int haveGroup, unsigned long handlers)
{
    return !haveGroup && handlers != 0;
}

/* A waiter waits on the group it read under the lock, if there is one. A group made up front and never entered
   returns from dispatch_group_wait at once, so skipping the call for a nil group waits for the same handlers. */
static inline int nvmtl_cb_group_wait_due(int haveGroup)
{
    return haveGroup;
}

/* The capacity semaphore is made with the queue when the queue enforces capacity (NVMTL_ASYNC_COMMIT) and the saving
   is on. -commandBuffer then reads it without the class lock; if it finds none (the saving off, or a queue made some
   other way) it takes the lock and makes it as before. */
static inline int nvmtl_cb_capacity_with_queue(int lean, int asyncCommit)
{
    return lean && asyncCommit;
}
static inline int nvmtl_cb_capacity_read_unlocked(int lean)
{
    return lean;
}

#endif
