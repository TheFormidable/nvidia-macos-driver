/* test_fast_flush.c - when -commitAndWaitUntilSubmitted submits its own command buffer ahead on the committing thread.
   Build and run anywhere (no GPU, no Metal):  cc -std=c11 -Wall -Wextra -I plugin tools/test_fast_flush.c -o /tmp/test_fast_flush && /tmp/test_fast_flush
   Each arm is a sentence about a failure the rule must produce or refuse. The walk arm runs the same loop the plugin
   runs over a queue's reservations (nvmtl_fast_flush_submit in NVMTLEventSync.m) against a model of nvmtlSubmitAhead. */
#include <stdio.h>
#include "nvmtl_fast_flush.h"

static int fails;
static void arm(const char *what, int ok) { printf("%s  %s\n", ok ? "ok  " : "FAIL", what); if (!ok) fails++; }

/* a queue's reservations in order: whether nvmtlSubmitAhead would submit all of that command buffer's parts */
struct res { int submits; int asked; };
static int walk(struct res *q, int n, int own)
{
    int last = -1;
    for (int i = 0; i < n; i++) {
        q[i].asked = 1; last = i;
        if (nvmtl_fast_flush_walk_stops(q[i].submits, i == own)) break;
    }
    return last;
}

int main(void)
{
    arm("an unset NVMTL_GL_FAST_FLUSH leaves it on", nvmtl_fast_flush_env_on(NULL));
    arm("an empty or 1 value leaves it on", nvmtl_fast_flush_env_on("") && nvmtl_fast_flush_env_on("1"));
    arm("NVMTL_GL_FAST_FLUSH=0 turns it off", !nvmtl_fast_flush_env_on("0"));

    arm("a committed, unfinished command buffer of a game with the submit-ahead on is submitted inline",
        nvmtl_fast_flush_inline(1, 0, 1, 1, 1, 1, 0));
    arm("the switch off means the caller only waits", !nvmtl_fast_flush_inline(0, 0, 1, 1, 1, 1, 0));
    arm("WindowServer never submits on its committing thread", !nvmtl_fast_flush_inline(1, 1, 1, 1, 1, 1, 0));
    arm("without the asynchronous submit-ahead (NVMTL_ASYNC_COMMIT=0 or G-SUB off) nothing runs inline",
        !nvmtl_fast_flush_inline(1, 0, 0, 1, 1, 1, 0));
    arm("NVMTL_COMMIT_AHEAD=0 keeps the submit-ahead on the worker only", !nvmtl_fast_flush_inline(1, 0, 1, 0, 1, 1, 0));
    arm("with the worker not pumping nothing would finish a part submitted ahead, so nothing runs",
        !nvmtl_fast_flush_inline(1, 0, 1, 1, 0, 1, 0));
    arm("a command buffer that is not committed is not submitted", !nvmtl_fast_flush_inline(1, 0, 1, 1, 1, 0, 0));
    arm("a finished command buffer (NVMTL_DEBUG_WAIT_EACH, or a fast GPU) is left alone",
        !nvmtl_fast_flush_inline(1, 0, 1, 1, 1, 1, 1));

    {
        struct res q[4] = { {1, 0}, {1, 0}, {1, 0}, {1, 0} };
        const int last = walk(q, 4, 1);
        arm("earlier command buffers of the queue go first, in order, and the walk stops at the caller's own",
            last == 1 && q[0].asked && q[1].asked && !q[2].asked && !q[3].asked);
    }
    {
        struct res q[3] = { {0, 0}, {1, 0}, {1, 0} };
        const int last = walk(q, 3, 2);
        arm("an earlier command buffer that cannot be submitted stops the walk: nothing overtakes it in its queue",
            last == 0 && !q[1].asked && !q[2].asked);
    }
    {
        struct res q[2] = { {0, 0}, {1, 0} };
        const int last = walk(q, 2, 0);
        arm("the caller's own command buffer refused (cap, caller copy, hold) ends the walk and leaves the wait to it",
            last == 0 && !q[1].asked);
    }

    arm("the census is written at the first flush", nvmtl_fast_flush_census_due(1));
    arm("and at powers of two", nvmtl_fast_flush_census_due(2) && nvmtl_fast_flush_census_due(1024));
    arm("and not in between", !nvmtl_fast_flush_census_due(3) && !nvmtl_fast_flush_census_due(1000) && !nvmtl_fast_flush_census_due(0));

    printf(fails ? "FAILED %d\n" : "ok\n", fails);
    return fails != 0;
}
