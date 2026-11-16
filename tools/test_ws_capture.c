/* test_ws_capture.c - the rules of NVMTL_SURFIN_ZERO and NVMTL_WS_FREE_OFFMAIN (plugin/nvmtl_ws_capture.h): on in
   WindowServer only, NAME=0 the one way back, and which buffer frees may leave the releasing thread.
   Build and run anywhere:  cc -O2 -Wall -I plugin tools/test_ws_capture.c -o /tmp/test_ws_capture && /tmp/test_ws_capture */
#include <stdio.h>
#include "nvmtl_ws_capture.h"

static int fails;
#define ARM(name, cond) do { int ok_ = (cond); printf("%s %s\n", ok_ ? "ok  " : "FAIL", name); if (!ok_) fails++; } while (0)

#define CAP (64ull << 20)
/* control: a copy of the rule that forgets the CPU map, i.e. would free a host-pointer import off its thread */
static int bad_mapped(int on, int imported, int mapped, int pooled, int placed, int lost, uint64_t pend, uint64_t sz,
                      uint64_t cap)
{
    (void)mapped;
    if (!on || !imported || pooled || placed || lost) return 0;
    return sz > cap || pend > cap - sz ? -1 : 1;
}
/* control: a copy whose bound wraps around (pending + size compared, overflowing) */
static int bad_cap(int on, int imported, int mapped, int pooled, int placed, int lost, uint64_t pend, uint64_t sz,
                   uint64_t cap)
{
    if (!on || !imported || mapped || pooled || placed || lost) return 0;
    return pend + sz > cap ? -1 : 1;
}
typedef int (*rule_fn)(int, int, int, int, int, int, uint64_t, uint64_t, uint64_t);
static int rule_arms(rule_fn f)
{
    int bad = 0;
    bad += f(1, 1, 0, 0, 0, 0, 0, 9043968, CAP) != 1;               /* a dead capture surface's VRAM import moves */
    bad += f(1, 1, 1, 0, 0, 0, 0, 9043968, CAP) != 0;               /* the host-pointer import stays */
    bad += f(1, 0, 0, 0, 0, 0, 0, 4096, CAP) != 0;                  /* an allocation of its own stays */
    bad += f(1, 1, 0, 1, 0, 0, 0, 4096, CAP) != 0;                  /* pooled */
    bad += f(1, 1, 0, 0, 1, 0, 0, 4096, CAP) != 0;                  /* placed */
    bad += f(1, 1, 0, 0, 0, 1, 0, 4096, CAP) != 0;                  /* bind lost */
    bad += f(0, 1, 0, 0, 0, 0, 0, 4096, CAP) != 0;                  /* switch off */
    bad += f(1, 1, 0, 0, 0, 0, CAP - 4096, 4096, CAP) != 1;         /* exactly at the cap */
    bad += f(1, 1, 0, 0, 0, 0, CAP - 4095, 4096, CAP) != -1;        /* one byte past it */
    bad += f(1, 1, 0, 0, 0, 0, 0, CAP + 1, CAP) != -1;              /* one buffer larger than the cap */
    bad += f(1, 1, 0, 0, 0, 0, UINT64_MAX - 10, 4096, CAP) != -1;   /* a backlog that would wrap */
    return bad;
}

int main(void)
{
    ARM("both switches are on in WindowServer by default", nvmtl_ws_capture_switch(NULL, "WindowServer"));
    ARM("NAME=0 is the way back in WindowServer", !nvmtl_ws_capture_switch("0", "WindowServer"));
    ARM("any value not starting with 0 leaves it on there (only =0 is documented)",
        nvmtl_ws_capture_switch("1", "WindowServer") && nvmtl_ws_capture_switch("", "WindowServer"));
    ARM("an application never has it on, set or not",
        !nvmtl_ws_capture_switch(NULL, "java") && !nvmtl_ws_capture_switch("1", "Safari")
        && !nvmtl_ws_capture_switch(NULL, NULL));
    ARM("a name that only starts like WindowServer is an application",
        !nvmtl_ws_capture_switch(NULL, "WindowServerX") && !nvmtl_ws_capture_switch(NULL, "Window"));
    ARM("NVMTL_SURFIN_ZERO: on in WindowServer with the prologue on, set or unset",
        nvmtl_surfin_zero_switch(NULL, NULL, "WindowServer") && nvmtl_surfin_zero_switch(NULL, "1", "WindowServer"));
    ARM("NVMTL_SURFIN_ZERO: off with NVMTL_PRESUBMIT_PROLOGUE=0 (its hook is then outside the submit mutex)",
        !nvmtl_surfin_zero_switch(NULL, "0", "WindowServer"));
    ARM("NVMTL_SURFIN_ZERO: =0 and an application still off",
        !nvmtl_surfin_zero_switch("0", NULL, "WindowServer") && !nvmtl_surfin_zero_switch(NULL, NULL, "java"));

    ARM("off-main free: every arm of the rule", rule_arms(nvmtl_free_offmain_ok) == 0);
    ARM("control: a rule that forgets the CPU map is caught", rule_arms(bad_mapped) > 0);
    ARM("control: a bound that wraps around is caught", rule_arms(bad_cap) > 0);
    printf("%s: %d failed\n", fails ? "FAIL" : "PASS", fails);
    return fails ? 1 : 0;
}
