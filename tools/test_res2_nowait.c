/* test_res2_nowait.c - when a thread that asks the VRAM pager for room may sleep (plugin/nvmtl_res2_nowait.h).
   Build and run anywhere (no GPU, no Metal):  cc -std=c11 -Wall -Wextra -I plugin tools/test_res2_nowait.c -o /tmp/test_res2_nowait && /tmp/test_res2_nowait
   Each arm is a sentence about a decision the rule must make or refuse. tools/test_res2_nowait.py checks that
   NVMTLObjects.m takes its decisions from this header. */
#include <stdio.h>
#include "nvmtl_res2_nowait.h"

static int fails;
static void arm(const char *what, int ok) { printf("%s  %s\n", ok ? "ok  " : "FAIL", what); if (!ok) fails++; }

int main(void)
{
    arm("unset, a game does not wait for the pager", nvmtl_res2_nowait_default(NULL, "java") == 1);
    arm("unset, a process without a name does not wait either", nvmtl_res2_nowait_default(NULL, NULL) == 1);
    arm("NVMTL_RES2_NOWAIT=0 restores the waits", nvmtl_res2_nowait_default("0", "java") == 0);
    arm("NVMTL_RES2_NOWAIT=1 or any other value keeps them off", nvmtl_res2_nowait_default("1", "java") == 1 &&
                                                                 nvmtl_res2_nowait_default("", "java") == 1);
    arm("WindowServer keeps the waits, unset", nvmtl_res2_nowait_default(NULL, "WindowServer") == 0);
    arm("WindowServer keeps the waits with the variable set to 1 too", nvmtl_res2_nowait_default("1", "WindowServer") == 0);
    arm("a name that only starts with WindowServer is not WindowServer", nvmtl_res2_nowait_default(NULL, "WindowServerHelper") == 1);

    /* the lock is taken */
    arm("the worker always kicks itself and returns", nvmtl_res2_busy_action(1, 0) == NVMTL_RES2_BUSY_KICK &&
                                                      nvmtl_res2_busy_action(1, 1) == NVMTL_RES2_BUSY_KICK);
    arm("an allocating thread kicks the worker and returns", nvmtl_res2_busy_action(0, 1) == NVMTL_RES2_BUSY_KICK);
    arm("with the waits restored an allocating thread polls for 30 ms as in 3.0.5", nvmtl_res2_busy_action(0, 0) == NVMTL_RES2_BUSY_SPIN);

    /* after the pass */
    const uint64_t g = 64ull << 20, now = 1000000000ull, later = now + 250000000ull;
    arm("VRAM came back: nothing more to do, either way", nvmtl_res2_drain_action(1, 0, g, now, 0) == NVMTL_RES2_DRAIN_NONE &&
                                                          nvmtl_res2_drain_action(0, 0, g, now, 0) == NVMTL_RES2_DRAIN_NONE);
    arm("an empty graveyard has nothing to drain, either way", nvmtl_res2_drain_action(1, 1, 0, now, 0) == NVMTL_RES2_DRAIN_NONE &&
                                                               nvmtl_res2_drain_action(0, 1, 0, now, 0) == NVMTL_RES2_DRAIN_NONE);
    arm("nothing came back and the graveyard holds VRAM: reap once, do not sleep", nvmtl_res2_drain_action(1, 1, g, now, 0) == NVMTL_RES2_DRAIN_REAP);
    arm("the non-blocking path ignores the blind window (it never sets one)", nvmtl_res2_drain_action(1, 1, g, now, later) == NVMTL_RES2_DRAIN_REAP);
    arm("no setting gives an allocating thread a sleep when the waits are off",
        nvmtl_res2_drain_action(1, 1, g, 0, 0) != NVMTL_RES2_DRAIN_SLEEP && nvmtl_res2_drain_action(1, 1, g, later, now) != NVMTL_RES2_DRAIN_SLEEP);
    arm("with the waits restored it sleeps up to 16 ms as in 3.0.5", nvmtl_res2_drain_action(0, 1, g, now, 0) == NVMTL_RES2_DRAIN_SLEEP);
    arm("with the waits restored it stays blind for 250 ms after a timeout", nvmtl_res2_drain_action(0, 1, g, now, later) == NVMTL_RES2_DRAIN_NONE);
    arm("and sleeps again once the blind window is over", nvmtl_res2_drain_action(0, 1, g, later, later) == NVMTL_RES2_DRAIN_SLEEP);

    printf(fails ? "FAILED %d\n" : "ok\n", fails);
    return fails != 0;
}
