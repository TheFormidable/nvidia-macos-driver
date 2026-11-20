/* test_cb_lean.c - what a command buffer makes up front and what it makes when first needed (nvmtl_cb_lean.h).
   Build and run anywhere (no GPU, no Metal):  cc -std=c11 -Wall -Wextra -I plugin tools/test_cb_lean.c -o /tmp/test_cb_lean && /tmp/test_cb_lean
   Each arm is a sentence about a failure the rule must produce or refuse. The group arms run a model of one command
   buffer's handler group (the slot nvmtlDispatchHandlers fills and -waitUntilScheduled / -waitUntilCompleted read, all
   under _completionCondition) through every order of dispatches and waits, made up front and made on first use, and
   compare what each wait waits for. */
#include <stdio.h>
#include "nvmtl_cb_lean.h"

static int fails;
static void arm(const char *what, int ok) { printf("%s  %s\n", ok ? "ok  " : "FAIL", what); if (!ok) fails++; }

/* a group: whether it exists, how many handlers entered and have not left yet, and how many were ever made */
struct group { int exists; int pending; int made; };
static void init_group(struct group *g, int lean) { g->exists = 0; g->pending = 0; g->made = 0;
    if (nvmtl_cb_groups_at_init(lean)) { g->exists = 1; g->made++; } }
/* nvmtlDispatchHandlers: returns before anything when there is no handler; otherwise the group, then one enter each */
static int dispatch(struct group *g, unsigned long handlers) {
    if (!handlers) return 1;
    if (nvmtl_cb_group_make_now(g->exists, handlers)) { g->exists = 1; g->made++; }
    if (!g->exists) return 0;   /* an enter on a nil group: the crash the rule must never allow */
    g->pending += (int)handlers; return 1;
}
/* the handler block: one leave each */
static void handlers_ran(struct group *g, int n) { g->pending -= n; }
/* a waiter: how many handlers its dispatch_group_wait waits for (0 when it returns at once or makes no call) */
static int wait_for(const struct group *g) {
    const int have = g->exists;
    if (!nvmtl_cb_group_wait_due(have)) return 0;
    return g->pending;
}

/* a sequence of steps: 'd' dispatches the given count, 'r' runs all pending handlers, 'w' waits */
static int run(const char *steps, const unsigned long *counts, int lean, int *waits, int *made) {
    struct group g; init_group(&g, lean);
    int nw = 0, ci = 0;
    for (const char *s = steps; *s; s++) {
        if (*s == 'd' && !dispatch(&g, counts[ci++])) return 0;
        if (*s == 'r') handlers_ran(&g, g.pending);
        if (*s == 'w') waits[nw++] = wait_for(&g);
    }
    *made = g.made;
    return 1;
}

int main(void)
{
    arm("an unset NVMTL_CB_LEAN leaves it on", nvmtl_cb_lean_env_on(NULL));
    arm("an empty or 1 value leaves it on", nvmtl_cb_lean_env_on("") && nvmtl_cb_lean_env_on("1"));
    arm("NVMTL_CB_LEAN=0 turns it off", !nvmtl_cb_lean_env_on("0"));

    arm("with the saving on, -init makes no group", !nvmtl_cb_groups_at_init(1));
    arm("NVMTL_CB_LEAN=0: -init makes both groups, as in 3.0.5", nvmtl_cb_groups_at_init(0));
    arm("a group is made at the first dispatch that has a handler", nvmtl_cb_group_make_now(0, 1));
    arm("no group is made for a dispatch with no handler", !nvmtl_cb_group_make_now(0, 0));
    arm("an existing group is never replaced (its enters would be lost)", !nvmtl_cb_group_make_now(1, 3));
    arm("a waiter that finds no group makes no wait call", !nvmtl_cb_group_wait_due(0));
    arm("a waiter that finds a group waits on it", nvmtl_cb_group_wait_due(1));

    {
        static const char *seqs[] = { "w", "dw", "dwr", "drw", "wdw", "ddw", "drdw", "dwdrw", "wdrdww", "dd" };
        static const unsigned long counts[][3] = { {0}, {1}, {2}, {1}, {3}, {1, 2}, {1, 1}, {2, 1}, {1, 4}, {0, 1} };
        int same = 1, nilEnter = 0, fewer = 1;
        for (unsigned i = 0; i < sizeof seqs / sizeof *seqs; i++) {
            int we[8] = {0}, wl[8] = {0}, me = 0, ml = 0;
            const int oke = run(seqs[i], counts[i], 0, we, &me), okl = run(seqs[i], counts[i], 1, wl, &ml);
            if (!oke || !okl) nilEnter = 1;
            for (int k = 0; k < 8; k++) if (we[k] != wl[k]) same = 0;
            if (ml > me) fewer = 0;
        }
        arm("in every order of dispatches, handler runs and waits, each wait waits for the same handlers either way", same);
        arm("no handler is ever entered on a group that was not made", !nilEnter);
        arm("made on first use, never more groups than made up front", fewer);
    }
    {
        int w[2] = {0}, made = 0; const unsigned long none[1] = {0};
        run("w", none, 1, w, &made);
        arm("a command buffer whose handlers are never dispatched makes no group at all", made == 0);
        const unsigned long one[1] = {2};
        run("dw", one, 1, w, &made);
        arm("a dispatched handler that has not run yet is waited for", made == 1 && w[0] == 2);
    }

    arm("the queue's capacity semaphore is made with the queue when capacity is enforced", nvmtl_cb_capacity_with_queue(1, 1));
    arm("NVMTL_ASYNC_COMMIT=0 enforces no capacity: no semaphore with the queue", !nvmtl_cb_capacity_with_queue(1, 0));
    arm("NVMTL_CB_LEAN=0: left to the first -commandBuffer, as in 3.0.5", !nvmtl_cb_capacity_with_queue(0, 1));
    arm("-commandBuffer reads it without the class lock only with the saving on",
        nvmtl_cb_capacity_read_unlocked(1) && !nvmtl_cb_capacity_read_unlocked(0));

    printf(fails ? "FAILED %d\n" : "ok\n", fails);
    return fails != 0;
}
