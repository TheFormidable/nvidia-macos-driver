/* nvmtl_fast_flush.h - when -commitAndWaitUntilSubmitted submits its own command buffer on the committing thread.
   Apple's OpenGL renderer flushes its context with -commitAndWaitUntilSubmitted, on the order of a hundred times a
   frame (every glBufferSubData on a buffer the pending command buffer uses flushes it). The commit only queues the
   command buffer and asks the nvmtl.ahead queue (or the worker) for a submit-ahead pass; the caller then slept until
   that other thread had taken the command-buffer class lock, submitted the part and woken it. Since the caller sleeps
   until its command buffer is scheduled anyway, it runs that same submit-ahead step itself, for its own queue, in queue
   order, up to and including its own command buffer, with the class lock it held a moment before. Nothing the caller
   sees changes: it still returns only once the command buffer is scheduled (the wait after the step is the one it had),
   with the same status and result; a command buffer the step cannot submit (the in-flight cap, a caller copy, an earlier
   command buffer of the queue not committed, an event wait, a hold until a surface publication) waits as before.
   Not in WindowServer: its commit-time pass on the committing thread stalled window drags, which is why the pass runs on
   nvmtl.ahead at all, and WindowServer's flush waits for completion (or keeps its scheduled wait) as before.
   NVMTL_GL_FAST_FLUSH=0: the committing thread only waits, as in 3.0.5. The rule is pure so tools/test_fast_flush.c
   proves it without Metal. */
#ifndef NVMTL_FAST_FLUSH_H
#define NVMTL_FAST_FLUSH_H

/* the way out: on unless the variable starts with 0, as the neighbouring commit switches read theirs */
static inline int nvmtl_fast_flush_env_on(const char *value)
{
    return !(value && value[0] == '0');
}

/* Whether the committing thread runs the submit-ahead step for its command buffer. Only where the asynchronous
   submit-ahead exists at all and may run at commit (NVMTL_ASYNC_COMMIT, the G-SUB submit-ahead and NVMTL_COMMIT_AHEAD
   are all on), only while the worker is pumping (as nvmtl.ahead's pass: the worker is what finishes a part submitted
   ahead), and only for a command buffer that is committed and not yet finished. */
static inline int nvmtl_fast_flush_inline(int on, int windowServer, int submitAheadOn, int commitAheadOn,
                                          int pumping, int committed, int executionDone)
{
    return on && !windowServer && submitAheadOn && commitAheadOn && pumping && committed && !executionDone;
}

/* The walk over the queue's reservations, in order: a command buffer that could not submit all its parts stops it,
   since nothing later in the queue may go before it, and so does the committing thread's own command buffer, since
   what comes after it is not what this caller waits for (nvmtl.ahead's pass still submits it). */
static inline int nvmtl_fast_flush_walk_stops(int submittedAll, int isOwn)
{
    return !submittedAll || isOwn;
}

/* the census line is written at the first and then at every power of two flushes */
static inline int nvmtl_fast_flush_census_due(unsigned long long count)
{
    return count && !(count & (count - 1));
}

#endif
