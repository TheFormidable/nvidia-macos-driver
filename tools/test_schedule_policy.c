/* test_schedule_policy.c - which surface writes a command buffer publishes before it reports "scheduled", by mode and per
   surface (two-plane surfaces in their planes' own VRAM included).
   Build and run anywhere:  cc -I plugin tools/test_schedule_policy.c -o /tmp/test_schedule_policy && /tmp/test_schedule_policy */
#include "nvmtl_schedule_policy.h"
#include <stdio.h>

static int fails;
/* The decision as it was written in NVMTLEventSync.m before two-plane surfaces could live in VRAM, with the loop's
   "mode 0 publishes nothing" folded in. */
static int old_rule(int after, int surf, int vram, unsigned long planes)
{ return after && (after == 1 || (surf && !vram && (after != 3 || planes > 1))); }
/* How many inputs a version of the per-surface rule gets wrong, so the controls can show each rule is needed. */
static int surface_rule_wrong(int (*f)(int, int, int, unsigned long, int))
{
    int bad = 0;
    for (int mode = 0; mode <= 3; mode++) for (int surf = 0; surf <= 1; surf++) for (int vram = 0; vram <= 1; vram++)
        for (unsigned long planes = 0; planes <= 3; planes++) for (int kw = 0; kw <= 1; kw++) {
            const int got = f(mode, surf, vram, planes, kw);
            const int want = (planes <= 1 || !vram || !surf) ? old_rule(mode, surf, vram, planes)
                                                            : (mode == 1 || (mode != 0 && !kw));
            bad += got != want;
        }
    return bad;
}
static int ctl_never_planar(int m, int s, int v, unsigned long p, int k) { (void)k; return old_rule(m, s, v, p); }
static int ctl_ignores_wait(int m, int s, int v, unsigned long p, int k) { (void)k; return nvmtl_schedule_after_surface(m, s, v, p, 0); }
static int ctl_mode0(int m, int s, int v, unsigned long p, int k) { return m == 0 ? (s && !v) : nvmtl_schedule_after_surface(m, s, v, p, k); }
#define ARM(name, cond) do { const int ok = !!(cond); printf("%s  %s\n", ok ? "ok  " : "FAIL", name); if (!ok) fails++; } while (0)

int main(void)
{
    ARM("Chrome, its helpers, Chromium and Edge publish every surface write first",
        nvmtl_schedule_after_mode(NULL, "Google Chrome") == 1 && nvmtl_schedule_after_mode(NULL, "Google Chrome Helper (GPU)") == 1 &&
        nvmtl_schedule_after_mode(NULL, "Chromium Helper (Renderer)") == 1 && nvmtl_schedule_after_mode(NULL, "Microsoft Edge Helper (GPU)") == 1);
    ARM("WindowServer, Safari, Electron apps and unnamed processes keep mode 3",
        nvmtl_schedule_after_mode(NULL, "WindowServer") == 3 && nvmtl_schedule_after_mode(NULL, "Safari") == 3 &&
        nvmtl_schedule_after_mode(NULL, "Electron") == 3 && nvmtl_schedule_after_mode(NULL, NULL) == 3 && nvmtl_schedule_after_mode("", "") == 3);
    int all = 1;
    for (char d = '0'; d <= '3'; d++) { const char s[2] = { d, 0 };
        all &= nvmtl_schedule_after_mode(s, "Google Chrome") == d - '0' && nvmtl_schedule_after_mode(s, "WindowServer") == d - '0'; }
    ARM("an explicit 0..3 wins in every process", all);
    ARM("a setting that is not a single digit 0..3 is ignored, not turned into a mode no path knows",
        nvmtl_schedule_after_mode("x", "WindowServer") == 3 && nvmtl_schedule_after_mode("7", "WindowServer") == 3 &&
        nvmtl_schedule_after_mode("12", "Google Chrome") == 1);
    ARM("per surface: for single-plane and private surfaces the rule is exactly the one before, in all four modes",
        surface_rule_wrong(nvmtl_schedule_after_surface) == 0);
    ARM("a two-plane surface in its planes' VRAM is held back only where WindowServer's page-off does not wait for it",
        !nvmtl_schedule_after_surface(3, 1, 1, 2, 1) && nvmtl_schedule_after_surface(3, 1, 1, 2, 0) &&
        !nvmtl_schedule_after_surface(2, 1, 1, 2, 1) && nvmtl_schedule_after_surface(2, 1, 1, 2, 0) &&
        nvmtl_schedule_after_surface(1, 1, 1, 2, 1) && !nvmtl_schedule_after_surface(0, 1, 1, 2, 0));
    ARM("control: a rule that keeps two-plane VRAM surfaces as they were is told apart", surface_rule_wrong(ctl_never_planar) != 0);
    ARM("control: a rule that never asks whether the kernel waits is told apart", surface_rule_wrong(ctl_ignores_wait) != 0);
    ARM("control: a rule where mode 0 still publishes private surfaces is told apart", surface_rule_wrong(ctl_mode0) != 0);
    return fails ? 1 : 0;
}
