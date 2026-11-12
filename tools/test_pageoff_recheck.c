/* test_pageoff_recheck.c - what a page-off does when it has the accelerator lock back after waiting without it, proven
   on the host against every short order of events.
   Build and run anywhere:  cc -I kexts/NVRM/accel tools/test_pageoff_recheck.c -o /tmp/t && /tmp/t

   The model. One resource: the GPU's memory, system memory, the family's bit "system memory is current", and the
   driver's numbered copies (nvaccel_pageoff_recheck.h, the same record and functions the kext uses). One page-off, P,
   has let go of the lock and waits for the GPU to finish frame 1. While it waits, in any order:
     G  the GPU finishes frame 1                      C  another page-off copies (reads the GPU's memory, writes system
     D  the plugin marks a new GPU write                 memory, sets the bit; lock held from its number to its end)
     F  the family sets the bit by a path of its own  X  a CPU writer writes system memory (no lock)
     W  P's wait ends (once): done when G came before it, at its bound otherwise
   Then P has the lock back, decides, and copies or reports done.

   The measure is 2.9, whose page-off held the lock through its wait and copied the moment the wait ended. What must
   hold, and which other rule breaks it:
   - What P leaves in system memory is never older than what 2.9 would have copied: the GPU's memory as it was when
     P's wait ended (when only the driver's copies set the bit). So a P that waited until the frame was finished never
     reports done over a half-drawn one. The rule before this header ("the bit was clear and is set") breaks it: a
     page-off that gave up early sets the bit, and P handed its unfinished frame back.
   - A CPU write made after a copy that began when P's wait was over is still there when P returns, however the wait
     ended. In 2.9 no page-off could run between P's copy and such a write. The same earlier rule breaks it when the
     bit was already set before P waited: P copied the GPU's pixels over the write.
   - The simpler rule "any copy that finished meanwhile is P's copy" breaks the first again. */
#include "nvaccel_pageoff_recheck.h"
#include <stdio.h>
#include <string.h>

static int fails;
#define ARM(name, cond) do { const int ok = !!(cond); printf("%s  %s\n", ok ? "ok  " : "FAIL", name); if (!ok) fails++; } while (0)

typedef int (*rule_fn)(const nvaccel_pageoff_copies *c, int was, int now, unsigned long long atUnlock, unsigned long long atWaitEnd);
static int rule_header(const nvaccel_pageoff_copies *c, int was, int now, unsigned long long u, unsigned long long w)
{
    return nvaccel_pageoff_already_done(c, was, now, u, w);
}
/* negative controls */
static int rule_bit_only(const nvaccel_pageoff_copies *c, int was, int now, unsigned long long u, unsigned long long w)
{
    (void)c; (void)u; (void)w; return !was && now;
}
static int rule_any_copy(const nvaccel_pageoff_copies *c, int was, int now, unsigned long long u, unsigned long long w)
{
    (void)w; return now && (!was || nvaccel_pageoff_copied_after(c, u));
}

enum { FRAME = 1 };   /* the frame P waits for */
typedef struct {
    int gpu;       /* the newest frame complete in the GPU's memory: what a copy made now reads */
    int sys;       /* the frame system memory holds */
    int cpu;       /* CPU writes in system memory that no copy has overwritten */
    int current;   /* the family's bit */
    nvaccel_pageoff_copies copies;
} world;
static void copy_now(world *w)
{
    const unsigned long long n = nvaccel_pageoff_copy_begin(&w->copies);
    w->sys = w->gpu; w->cpu = 0; w->current = 1;
    nvaccel_pageoff_copy_end(&w->copies, n);
}

typedef struct { long runs, older_than_29, early_frame, lost_write, differs_without_copies, no_copy_when_vram_newest, done_after_early_copy; } tally;

/* One order of events; `wasCurrent` is the bit when P let go of the lock. */
static void run(const char *ev, int wasCurrent, rule_fn rule, tally *t)
{
    world w; memset(&w, 0, sizeof w);
    w.current = wasCurrent; w.sys = wasCurrent ? 0 : -1;   /* bit clear: the GPU's memory already holds more than system memory */
    const unsigned long long atUnlock = nvaccel_pageoff_copies_started(&w.copies);
    unsigned long long atWaitEnd = 0;
    int waitDone = 0, sawG = 0, family = 0, copies = 0, copyBeganAfterWait = 0, copyAfterWait = 0, waited = 0, writesAfterThatCopy = 0;
    int gpuAtWaitEnd = 0;   /* what 2.9's page-off, with the lock held through its wait, would have copied */
    for (const char *e = ev; *e; e++) switch (*e) {
        case 'G': if (w.gpu < FRAME) w.gpu = FRAME; sawG = 1; break;
        case 'W': atWaitEnd = nvaccel_pageoff_copies_started(&w.copies); waitDone = sawG; gpuAtWaitEnd = w.gpu; waited = 1; break;
        case 'C': copy_now(&w); copies++; if (waited) { copyBeganAfterWait = copyAfterWait = 1; writesAfterThatCopy = 0; } break;
        case 'D': w.gpu += 10; w.current = 0; copyAfterWait = 0; break;   /* newer pixels on the GPU: a page-off may copy over anything again */
        case 'F': w.current = 1; family = 1; break;
        case 'X': w.cpu++; if (copyAfterWait) writesAfterThatCopy++; break;
    }
    const int now = w.current;
    const int done = rule(&w.copies, wasCurrent, now, atUnlock, atWaitEnd);
    if (!done) copy_now(&w);
    t->runs++;
    if (!now && done) t->no_copy_when_vram_newest++;
    /* nothing but the driver's copies set the bit: system memory holds at least what 2.9 would have copied */
    if (!family && w.sys < gpuAtWaitEnd) t->older_than_29++;
    /* and so, when P waited until the frame was finished, the frame */
    if (waitDone && !family && w.sys < FRAME) t->early_frame++;
    /* a copy that began after P's wait was over, and no GPU write marked since: CPU writes after it stay */
    if (copyAfterWait && w.cpu < writesAfterThatCopy) t->lost_write++;
    /* nothing of the driver's finished meanwhile: exactly the earlier rule, the bit before and after */
    if (!copies && done != (!wasCurrent && now)) t->differs_without_copies++;
    /* copies finished meanwhile but none began after P's wait ended: P never takes one of them for its own */
    if (copies && !copyBeganAfterWait && done) t->done_after_early_copy++;
}
/* every order of up to `depth` events G C D F X, with W at every position, for both values of the bit */
static void each(char *buf, int len, int depth, rule_fn rule, tally *t)
{
    char withW[16];
    for (int at = 0; at <= len; at++) {
        memcpy(withW, buf, (size_t)at); withW[at] = 'W'; memcpy(withW + at + 1, buf + at, (size_t)(len - at)); withW[len + 1] = 0;
        run(withW, 0, rule, t); run(withW, 1, rule, t);
    }
    if (len == depth) return;
    for (const char *e = "GCDFX"; *e; e++) { buf[len] = *e; each(buf, len + 1, depth, rule, t); }
}

int main(void)
{
    /* the numbering */
    nvaccel_pageoff_copies c; memset(&c, 0, sizeof c);
    ARM("no copy yet: none has started, none has finished after anything", nvaccel_pageoff_copies_started(&c) == 0 && !nvaccel_pageoff_copied_after(&c, 0));
    const unsigned long long n1 = nvaccel_pageoff_copy_begin(&c);
    ARM("a copy that has begun but not finished is not a finished copy", n1 == 1 && nvaccel_pageoff_copies_started(&c) == 1 && !nvaccel_pageoff_copied_after(&c, 0));
    nvaccel_pageoff_copy_end(&c, n1);
    ARM("once finished it counts for a waiter that looked before it began, not for one that looked after",
        nvaccel_pageoff_copied_after(&c, 0) && !nvaccel_pageoff_copied_after(&c, 1));
    const unsigned long long n2 = nvaccel_pageoff_copy_begin(&c);   /* this one fails: it never leaves its number */
    const unsigned long long n3 = nvaccel_pageoff_copy_begin(&c);
    ARM("a copy that failed leaves nothing behind", n2 == 2 && n3 == 3 && !nvaccel_pageoff_copied_after(&c, 1));
    nvaccel_pageoff_copy_end(&c, n3);
    ARM("the next good copy counts for waiters that looked before it began", nvaccel_pageoff_copied_after(&c, 1) && nvaccel_pageoff_copied_after(&c, 2) && !nvaccel_pageoff_copied_after(&c, 3));

    /* the rule, case by case: (c holds copies 1 and 3 finished, 3 the last) */
    ARM("the bit is clear: copy, whatever else happened", !nvaccel_pageoff_already_done(&c, 0, 0, 0, 0) && !nvaccel_pageoff_already_done(&c, 1, 0, 0, 0));
    ARM("a copy began after the wait ended and finished: that was this copy", nvaccel_pageoff_already_done(&c, 0, 1, 1, 2) && nvaccel_pageoff_already_done(&c, 1, 1, 1, 2));
    ARM("a copy finished meanwhile but began before the wait ended: copy", !nvaccel_pageoff_already_done(&c, 0, 1, 2, 3) && !nvaccel_pageoff_already_done(&c, 1, 1, 2, 3));
    ARM("no copy meanwhile, the bit was clear and is set: the family did it, done", nvaccel_pageoff_already_done(&c, 0, 1, 3, 3));
    ARM("no copy meanwhile, the bit was set all along: copy, as 2.9", !nvaccel_pageoff_already_done(&c, 1, 1, 3, 3));

    /* every order of up to six events while the lock was free */
    char buf[16]; tally t; memset(&t, 0, sizeof t);
    each(buf, 0, 6, rule_header, &t);
    printf("      %ld orders of events\n", t.runs);
    ARM("the walk ran every order (5^0 + ... + 5^6 sequences, W at each place, both values of the bit)", t.runs == 2L * (1 * 1 + 5 * 2 + 25 * 3 + 125 * 4 + 625 * 5 + 3125 * 6 + 15625 * 7));
    ARM("a clear bit always leads to a copy", t.no_copy_when_vram_newest == 0);
    ARM("system memory never ends up older than what 2.9 would have copied when the wait ended", t.older_than_29 == 0);
    ARM("a page-off that waited for the frame never reports done over an unfinished one", t.early_frame == 0);
    ARM("a CPU write made after this page-off's copy was made by another thread is never overwritten", t.lost_write == 0);
    ARM("with no copy of the driver's meanwhile the decision is the bit before and after, as before", t.differs_without_copies == 0);
    ARM("a copy that began before the wait ended is never taken for this page-off's copy", t.done_after_early_copy == 0);

    /* negative controls: the same walk with the earlier rule, and with the simpler one */
    tally o; memset(&o, 0, sizeof o); each(buf, 0, 6, rule_bit_only, &o);
    ARM("control: the earlier rule (bit clear before, set now) leaves system memory older than 2.9 would have", o.older_than_29 > 0);
    ARM("control: the earlier rule reports done over an unfinished frame after a wait that ended done", o.early_frame > 0);
    ARM("control: the earlier rule overwrites a CPU write when the bit was set before the wait", o.lost_write > 0);
    tally s; memset(&s, 0, sizeof s); each(buf, 0, 6, rule_any_copy, &s);
    ARM("control: taking any copy that finished meanwhile for this one reports done over an unfinished frame", s.early_frame > 0 && s.done_after_early_copy > 0);
    ARM("(the simpler rule does keep the CPU write: the unfinished frame is all it gets wrong)", s.lost_write == 0);
    printf("%s\n", fails ? "FAILED" : "PASS");
    return fails ? 1 : 0;
}
