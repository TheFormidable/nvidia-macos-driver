/* test_fence_table.c - the surface fence's table and wait loop (kexts/NVRM/nvrm_fence_table.h, the file the kext
   compiles) against a made-up clock and made-up channels. Proves, one arm at a time: every state a hold can be in and
   every way it ends; that an OPEN is all or nothing and never evicts a hold that still delays a reader; the snapshot
   rule; the bounds, strikes, distrust and the fuse; what a rebuilt channel keeps; the wait loop's answers, its budget
   and that it samples the generations before it looks; and, with 200,000 random operations, that the table agrees at
   every step with a second, naive implementation. The controls at the end show the checks can fail.
   The threaded run of the same loop is tools/test_fence_model.c.
   Build and run from the repository root:
     cc -std=c11 -O1 -Wall -Wextra -Werror -I kexts/NVRM tools/test_fence_table.c -o /tmp/test_fence_table && /tmp/test_fence_table */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "nvrm_fence_table.h"

static int fails;
#define T(name, cond) do { if (!(cond)) { printf("FAIL %s\n", name); fails++; } else printf("ok   %s\n", name); } while (0)
#define MS(x) ((unsigned long long)(x) * 1000000ull)
#define OK    NVRM_FENCE_ST_OK
#define COMP  NVRM_FENCE_F_COMPOSITOR
#define AUTO  NVRM_FENCE_F_AUTO
#define E_OPEN  NVRM_FENCE_E_OPEN
#define E_ARMED NVRM_FENCE_E_ARMED
#define E_DONE  NVRM_FENCE_E_GPU_DONE

static struct nvf_table Tb;                    /* the table under test */
static unsigned long long now;                 /* the clock, ns */

/* the channels' pages: what the kernel would read through its pinned views */
static struct hwchan { unsigned long long done, flushed; int faulted, mapped; unsigned reads; } hw[NVF_CHANS];
static int hw_read(void *ctx, unsigned i, unsigned long long *done, unsigned long long *flushed, int *faulted)
{
    (void)ctx;
    if (!hw[i].mapped) return 0;
    hw[i].reads++;
    *done = hw[i].done;
    if (flushed) *flushed = hw[i].flushed;
    *faulted = hw[i].faulted;
    return 1;
}
#define IDX(h) ((h) & 0xFFFu)

static void fresh(void) { nvf_init(&Tb); memset(hw, 0, sizeof(hw)); now = MS(1000); }
static unsigned chan_new(unsigned long long owner, unsigned pid)      /* CHAN_ADD + CHAN_VERIFY; 0 = refused */
{
    unsigned h = 0, fi = 0;
    if (nvf_chan_add(&Tb, owner, pid, 0, &h) != OK) return 0;
    memset(&hw[IDX(h)], 0, sizeof(hw[0]));
    hw[IDX(h)].mapped = 1;
    return nvf_chan_verify(&Tb, owner, h, 1, &fi) == OK ? h : 0;
}
static unsigned open1(unsigned long long owner, unsigned pid, unsigned long long cookie, unsigned sid, unsigned flags)
{
    const unsigned char pl = 0;
    return nvf_open(&Tb, owner, pid, cookie, 1, &sid, &pl, flags, now);
}
static unsigned arm1(unsigned long long owner, unsigned long long cookie, unsigned sid, unsigned h, unsigned long long seq, unsigned flags)
{
    return nvf_arm(&Tb, owner, cookie, sid, h, seq, flags, now, hw_read, NULL);
}
static unsigned close1(unsigned long long owner, unsigned long long cookie, unsigned sid)
{
    unsigned found = 0;
    (void)nvf_close(&Tb, owner, cookie, sid, hw_read, NULL, &found);
    return found;
}
/* one look by a reader that has just arrived */
static unsigned look(unsigned sid, unsigned planes, unsigned pid, unsigned flags, struct nvf_view *out)
{
    struct nvf_query q = { sid, planes, pid, flags, 0, 0 };
    struct nvf_view v;
    const unsigned n = nvf_eval(&Tb, &q, now, NVF_WHY_FIRST, hw_read, NULL, &v);
    if (out) *out = v;
    return n;
}
static const struct nvf_entry *hold_of(unsigned long long owner, unsigned long long cookie, unsigned sid)
{
    const int oi = nvf_owner_find(&Tb, owner);
    const int ei = oi < 0 ? -1 : nvf_find(&Tb, (unsigned)oi, cookie, sid);
    return ei < 0 ? NULL : &Tb.e[ei];
}

/* Every list and every count of the table agrees with every other. Returns NULL, or what is wrong. */
static const char *structure(const struct nvf_table *t)
{
    static unsigned char seen[NVF_ENTRIES];
    unsigned i, n, used = 0, live = 0, liveC = 0, flagged = 0, chans = 0, noirq = 0, ownersEntries = 0;
    memset(seen, 0, sizeof(seen));
    for (n = 0, i = t->freeHead; i != NVF_NIL; i = t->e[i].next) {
        if (i >= NVF_ENTRIES || ++n > NVF_ENTRIES) return "the free list is broken";
        if (t->e[i].state) return "a hold in use is on the free list";
        if (seen[i]++) return "a slot is on the free list twice";
    }
    if (n != NVF_ENTRIES - t->used) return "the free list and the used count disagree";
    for (unsigned b = 0; b < NVF_BUCKETS; b++)
        for (n = 0, i = t->bucket[b]; i != NVF_NIL; i = t->e[i].next) {
            if (i >= NVF_ENTRIES || ++n > NVF_ENTRIES) return "a bucket chain is broken";
            if (!t->e[i].state) return "a free slot is on a bucket chain";
            if (nvf_bucket(t->e[i].sid) != b) return "a hold is on the wrong bucket chain";
            if (seen[i]++) return "a hold is on two chains";
            used++;
            if (t->e[i].expired) flagged++; else { live++; if (t->e[i].flags & COMP) liveC++; }
        }
    if (used != t->used) return "holds are missing from the bucket chains";
    if (live != t->live || liveC != t->liveCompositor || flagged != t->flagged) return "live / flagged counts are wrong";
    if (t->stat[NVF_STAT_LIVE] != t->live || t->liveMax < t->live) return "the live gauge is wrong";
    for (unsigned o = 0; o < NVF_OWNERS; o++) {
        unsigned prev = NVF_NIL, mine = 0;
        if (!t->o[o].owner) continue;
        for (n = 0, i = t->o[o].head; i != NVF_NIL; prev = i, i = t->e[i].onext) {
            if (i >= NVF_ENTRIES || ++n > NVF_ENTRIES) return "an owner's list is broken";
            if (!t->e[i].state || t->e[i].oidx != o || t->e[i].owner != t->o[o].owner) return "an owner's list holds a stranger";
            if (t->e[i].oprev != prev) return "an owner's list has a bad back link";
        }
        if (n != t->o[o].entries) return "an owner's hold count is wrong";
        ownersEntries += n;
        for (i = 0; i < NVF_CHANS; i++) if (t->c[i].used && t->c[i].oidx == o) { mine++; if (t->c[i].owner != t->o[o].owner) return "a channel names the wrong owner"; }
        if (mine != t->o[o].chans) return "an owner's channel count is wrong";
    }
    if (ownersEntries != t->used) return "a hold is on no owner's list";
    for (i = 0; i < NVF_CHANS; i++) if (t->c[i].used) {
        chans++; if (t->c[i].noirq) noirq++;
        if (i >= t->chanHigh) return "a channel is beyond chanHigh";
        if (!t->c[i].gen || !t->o[t->c[i].oidx].owner) return "a channel has no generation or no owner";
    }
    if (t->chanHigh && !t->c[t->chanHigh - 1].used) return "chanHigh is too high";
    if (chans != t->stat[NVF_STAT_CHANS] || noirq != t->stat[NVF_STAT_CHAN_NOIRQ]) return "the channel gauges are wrong";
    return NULL;
}

/* ---- a scripted world for nvf_wait: nothing runs but the caller, and "sleeping" lets a script move the world ---- */
static struct {
    unsigned kick, gen, changesSeen;
    int locked, sleptLocked, sleeps, result;
    unsigned long long dl[64], at[64];            /* the deadline of each sleep and the time it began */
    unsigned kSeen[64], kAtSleep[64];
    void (*during)(int nth);                      /* what happens while the reader sleeps; may move `now` (not past dl) */
    void (*inLock)(void);                         /* runs once, the first time the lock is taken */
} W;
static void w_lock(void *c) { (void)c; W.locked++; if (W.inLock) { void (*f)(void) = W.inLock; W.inLock = NULL; f(); } }
static void w_unlock(void *c) { (void)c; W.locked--; if (Tb.changes != W.changesSeen) { W.changesSeen = Tb.changes; W.gen++; } }
static unsigned long long w_now(void *c) { (void)c; return now; }
static unsigned w_kick(void *c) { (void)c; return W.kick; }
static unsigned w_gen(void *c) { (void)c; return W.gen; }
static int w_sleep(void *c, unsigned k, unsigned g, unsigned long long dl, int abortable)
{
    const int nth = W.sleeps++;
    (void)c; (void)abortable;
    if (W.locked) W.sleptLocked++;
    if (nth < 64) { W.dl[nth] = dl; W.at[nth] = now; W.kSeen[nth] = k; W.kAtSleep[nth] = W.kick; }
    if (W.result != NVF_SLEPT) return W.result;
    if (W.kick != k || W.gen != g) return NVF_SLEPT;          /* the re-check after arming: do not block */
    if (W.during) W.during(nth);
    if (W.kick == k && W.gen == g && now < dl) now = dl;       /* nothing woke it: the deadline did */
    return NVF_SLEPT;
}
static const struct nvf_wait_ops WOPS = { w_lock, w_unlock, w_now, w_kick, w_gen, w_sleep, hw_read };
static void w_reset(void) { memset(&W, 0, sizeof(W)); W.changesSeen = Tb.changes; }
static unsigned wait_for(unsigned sid, unsigned pid, unsigned flags, unsigned timeoutMs, int lookOnly, struct NVRMFenceWait *w)
{
    struct nvf_query q = { sid, 0, pid, flags, 0, 0 };
    memset(w, 0xAA, sizeof(*w));
    return nvf_wait(&Tb, &WOPS, NULL, &q, timeoutMs, lookOnly, 1, w);
}
static unsigned waits_ended(void)
{
    return (unsigned)(Tb.stat[NVF_STAT_WAIT_READY] + Tb.stat[NVF_STAT_WAIT_DEAD] + Tb.stat[NVF_STAT_WAIT_TIMEOUT] +
                      Tb.stat[NVF_STAT_WAIT_BUDGET] + Tb.stat[NVF_STAT_WAIT_ABORTED] + Tb.stat[NVF_STAT_WAIT_FULL]);
}

/* scripts */
static unsigned sH;                                   /* the channel a script works on */
static void s_gpu_done_kick(int nth) { (void)nth; now += MS(1); hw[IDX(sH)].done = 100; W.kick++; }
static void s_gpu_done_quiet(int nth) { (void)nth; }  /* the GPU finishes without an interrupt: only the slice sees it */
static void s_close(int nth) { (void)nth; now += MS(3); (void)close1(1, 1, 7); w_lock(NULL); w_unlock(NULL); }
static void s_owner_gone(int nth) { unsigned fi[NVF_OWNER_CHANS]; (void)nth; now += MS(2); (void)nvf_owner_gone(&Tb, 1, fi, NVF_OWNER_CHANS); w_lock(NULL); w_unlock(NULL); }
static void s_pid_gone(int nth) { unsigned fi[NVF_OWNER_CHANS]; (void)nth; now += MS(2); (void)nvf_pid_gone(&Tb, 11, fi, NVF_OWNER_CHANS); w_lock(NULL); w_unlock(NULL); }
static void s_fault(int nth) { (void)nth; now += MS(2); hw[IDX(sH)].faulted = 1; W.kick++; }
static void s_open_more(int nth) { if (nth == 0) { now += MS(1); (void)open1(2, 22, 9, 7, 0); } else { now += MS(1); (void)close1(1, 1, 7); w_lock(NULL); w_unlock(NULL); } }
static void s_kick_in_look(void) { W.kick++; }
static unsigned long long sT0;
static void s_arm_late(int nth)
{
    (void)nth;
    if (now - sT0 >= MS(1500) && hold_of(1, 1, 7) && hold_of(1, 1, 7)->state == E_OPEN) (void)arm1(1, 1, 7, sH, 100, 0);
}
static void s_third_strike(int nth) { if (nth) return; now += MS(1); (void)look(7, 0, 0, 0, NULL); w_lock(NULL); w_unlock(NULL); }
static void s_two_ends(int nth) { if (nth) return; now += MS(1); (void)close1(1, 1, 7); hw[IDX(sH)].faulted = 1; w_lock(NULL); w_unlock(NULL); }

/* ---- the arms ---------------------------------------------------------------------------------------------------- */

static void arm_sizes(void)
{
    T("a hold is 64 bytes, a request 96", sizeof(struct nvf_entry) == 64 && sizeof(struct NVRMFenceReq) == 96);
    T("the table stays small enough to be static data (under 160 KB)", sizeof(struct nvf_table) < 160u * 1024u);
    T("a channel index fits its 12 bits and a generation is never 0", NVF_CHANS <= 4096 && NVF_CHAN_GEN_MAX == 0xFFFFF);
}

static void arm_open_close(void)
{
    fresh();
    T("an empty table has nothing pending", look(7, 0, 0, 0, NULL) == 0);
    T("OPEN takes a hold", open1(1, 11, 1, 7, 0) == OK && look(7, 0, 0, 0, NULL) == 1 && Tb.live == 1);
    T("another surface is not held", look(8, 0, 0, 0, NULL) == 0);
    T("a second command buffer holds the same surface", open1(1, 11, 2, 7, 0) == OK && look(7, 0, 0, 0, NULL) == 2);
    T("the first CLOSE does not free the surface", close1(1, 1, 7) == 1 && look(7, 0, 0, 0, NULL) == 1);
    T("the second does", close1(1, 2, 7) == 1 && look(7, 0, 0, 0, NULL) == 0 && Tb.live == 0 && Tb.used == 0);
    T("a CLOSE that finds nothing is not an error and is counted", close1(1, 2, 7) == 0 && Tb.stat[NVF_STAT_CLOSE_GONE] == 1);
    {
        unsigned ids[3] = { 3, 4, 5 }; unsigned char pl[3] = { 0, 0, 0 };
        T("one OPEN, three surfaces", nvf_open(&Tb, 1, 11, 5, 3, ids, pl, 0, now) == OK && Tb.live == 3);
        T("CLOSE with surface 0 closes every hold of the command buffer", close1(1, 5, 0) == 3 && Tb.live == 0);
    }
    {
        unsigned ids[3] = { 3, 3, 4 }; unsigned char pl[3] = { 1, 2, 1 };
        T("a surface named twice in one OPEN is one hold", nvf_open(&Tb, 1, 11, 6, 3, ids, pl, 0, now) == OK && Tb.live == 2);
        T("and its planes are the union", hold_of(1, 6, 3) && hold_of(1, 6, 3)->planes == 3);
        T("opening it again only adds planes", nvf_open(&Tb, 1, 11, 6, 1, ids, pl + 2, 0, now) == OK && Tb.live == 2 &&
          hold_of(1, 6, 3)->planes == 3);
        pl[0] = 0;
        T("plane mask 0 means every plane and stays that", nvf_open(&Tb, 1, 11, 6, 1, ids, pl, 0, now) == OK && hold_of(1, 6, 3)->planes == 0);
    }
    T("refused: no cookie, no surface, too many, AUTO without COMPOSITOR, owner 0",
      open1(1, 11, 0, 7, 0) == NVRM_FENCE_ST_BAD_ARG && open1(1, 11, 1, 0, 0) == NVRM_FENCE_ST_BAD_ARG &&
      nvf_open(&Tb, 1, 11, 1, 9, (unsigned[9]){ 1, 2, 3, 4, 5, 6, 7, 8, 9 }, NULL, 0, now) == NVRM_FENCE_ST_BAD_ARG &&
      nvf_open(&Tb, 1, 11, 1, 0, (unsigned[1]){ 1 }, NULL, 0, now) == NVRM_FENCE_ST_BAD_ARG &&
      open1(1, 11, 1, 7, AUTO) == NVRM_FENCE_ST_BAD_ARG && open1(0, 11, 1, 7, 0) == NVRM_FENCE_ST_BAD_ARG);
    T("the lists agree", structure(&Tb) == NULL);
}

static void arm_full(void)
{
    unsigned ids[8]; unsigned char pl[8] = { 0 };
    unsigned i, k, st = OK;
    fresh();
    for (k = 0; k < 15 && st == OK; k++) { for (i = 0; i < 8; i++) ids[i] = 100 + k * 8 + i; st = nvf_open(&Tb, 1, 11, 50 + k, 8, ids, pl, 0, now); }
    for (i = 0; i < 4; i++) ids[i] = 900 + i;
    st = st == OK ? nvf_open(&Tb, 1, 11, 99, 4, ids, pl, 0, now) : st;
    T("one connection can take 124 holds", st == OK && Tb.o[nvf_owner_find(&Tb, 1)].entries == 124);
    for (i = 0; i < 8; i++) ids[i] = 950 + i;
    T("an OPEN of 8 with room for 4 is refused whole", nvf_open(&Tb, 1, 11, 98, 8, ids, pl, 0, now) == NVRM_FENCE_ST_FULL &&
      Tb.o[nvf_owner_find(&Tb, 1)].entries == 124 && Tb.live == 124 && look(950, 0, 0, 0, NULL) == 0);
    T("and counted", Tb.stat[NVF_STAT_REFUSED_FULL] == 1);
    T("room for exactly 4 more", nvf_open(&Tb, 1, 11, 98, 4, ids, pl, 0, now) == OK && Tb.live == 128);
    T("the 129th hold of a connection is refused", open1(1, 11, 97, 999, 0) == NVRM_FENCE_ST_FULL && Tb.live == 128);

    /* fill the table: eight connections, two per process */
    for (unsigned o = 2; o <= 8 && st == OK; o++)
        for (k = 0; k < 16 && st == OK; k++) { for (i = 0; i < 8; i++) ids[i] = o * 1000 + k * 8 + i; st = nvf_open(&Tb, o, 10 + o / 2, 50 + k, 8, ids, pl, 0, now); }
    T("eight connections fill the table", st == OK && Tb.used == NVF_ENTRIES && Tb.live == NVF_ENTRIES);
    T("a full table refuses", open1(9, 14, 1, 5, 0) == NVRM_FENCE_ST_FULL && Tb.used == NVF_ENTRIES);
    T("and no hold that still delays a reader was evicted", Tb.live == NVF_ENTRIES && look(2000, 0, 0, 0, NULL) == 1);
    T("the lists agree when full", structure(&Tb) == NULL);

    /* two holds of connection 3 time out (a reader looks after their bound); they are flagged, not freed */
    now += MS(150);
    T("a reader flags a hold past its bound", look(3000, 0, 0, 0, NULL) == 0 && Tb.flagged == 1 && Tb.used == NVF_ENTRIES);
    T("its slot is taken first when room is needed", open1(9, 14, 1, 5, 0) == OK && Tb.flagged == 0 && Tb.used == NVF_ENTRIES &&
      Tb.stat[NVF_STAT_RECLAIMED] == 1 && hold_of(3, 50, 3000) == NULL);
    T("still full: the next one is refused", open1(9, 14, 2, 6, 0) == NVRM_FENCE_ST_FULL);
    /* connection 1 is at its quota: only its own flagged holds help it */
    T("a connection at its quota is not helped by another's flagged hold", look(4000, 0, 0, 0, NULL) == 0 && Tb.flagged == 1 &&
      open1(1, 11, 96, 998, 0) == NVRM_FENCE_ST_FULL && Tb.flagged == 1);
    (void)close1(4, 50, 4000);
    now = MS(1000) + MS(2000);         /* every hold opened at the start is now exactly at the longest bound */
    T("a hold exactly at the longest bound is not taken", open1(1, 11, 96, 998, 0) == NVRM_FENCE_ST_FULL && Tb.stat[NVF_STAT_RECLAIMED] == 1);
    now += 1;
    T("past the longest bound, unread holds make room (the connection's own first)", open1(1, 11, 96, 998, 0) == OK &&
      Tb.stat[NVF_STAT_RECLAIMED] == 2 && Tb.o[nvf_owner_find(&Tb, 1)].entries == 128);
    T("the lists agree after reclaiming", structure(&Tb) == NULL);

    fresh();
    T("four connections of one process may own holds", open1(1, 5, 1, 1, 0) == OK && open1(2, 5, 1, 1, 0) == OK &&
      open1(3, 5, 1, 1, 0) == OK && open1(4, 5, 1, 1, 0) == OK);
    T("the fifth is refused, for holds and for channels", open1(5, 5, 1, 1, 0) == NVRM_FENCE_ST_FULL && chan_new(5, 5) == 0);
    T("another process is not affected", open1(6, 6, 1, 1, 0) == OK);
    fresh();
    for (i = 0, k = 1; i < NVF_OWNER_CHANS && k; i++) k = chan_new(1, 11);
    T("a connection can watch 32 channels and no more", k != 0 && chan_new(1, 11) == 0 && Tb.o[nvf_owner_find(&Tb, 1)].chans == 32);
    for (unsigned o = 2; o <= 32 && k; o++) for (i = 0; i < NVF_OWNER_CHANS && k; i++) k = chan_new(o, 100 + o);
    T("1024 channels fill the table, the next is refused", k != 0 && Tb.stat[NVF_STAT_CHANS] == NVF_CHANS && chan_new(33, 133) == 0);
    T("the lists agree with every channel in use", structure(&Tb) == NULL);
}

static void arm_states(void)
{
    unsigned h, h2, other, fi = 0, raw = 0;
    fresh();
    h = chan_new(1, 11); h2 = chan_new(1, 11); other = chan_new(2, 22);
    T("channels are registered", h && h2 && other && h != h2);

    /* a compositor hold with AUTO */
    T("the compositor opens an AUTO hold", open1(1, 11, 1, 7, COMP | AUTO) == OK && Tb.liveCompositor == 1);
    T("armed on value 10", arm1(1, 1, 7, h, 10, 0) == OK && hold_of(1, 1, 7)->state == E_ARMED);
    hw[IDX(h)].done = 9;
    T("at 9 it is still in the way", look(7, 0, 0, COMP, NULL) == 1);
    hw[IDX(h)].done = 10;
    T("at 10 it ends in the kernel", look(7, 0, 0, COMP, NULL) == 0 && Tb.live == 0 && Tb.stat[NVF_STAT_AUTO] == 1 && hold_of(1, 1, 7) == NULL);
    T("its late CLOSE finds nothing", close1(1, 1, 7) == 0);

    /* an application hold */
    T("an application hold", open1(1, 11, 2, 7, 0) == OK && arm1(1, 2, 7, h, 20, 0) == OK);
    hw[IDX(h)].done = 20;
    T("the GPU is done: it still blocks until its CLOSE", look(7, 0, 0, 0, NULL) == 1 && hold_of(1, 2, 7)->state == E_DONE && Tb.live == 1);
    T("CLOSE frees it", close1(1, 2, 7) == 1 && look(7, 0, 0, 0, NULL) == 0);

    /* ARM raises, never lowers */
    (void)open1(1, 11, 3, 7, 0);
    T("ARM 30, then 25: stays 30", arm1(1, 3, 7, h, 30, 0) == OK && arm1(1, 3, 7, h, 25, 0) == OK && hold_of(1, 3, 7)->seq == 30);
    T("ARM 40 raises it", arm1(1, 3, 7, h, 40, 0) == OK && hold_of(1, 3, 7)->seq == 40);
    T("ARM on another channel of the same connection moves it", arm1(1, 3, 7, h2, 5, 0) == OK && hold_of(1, 3, 7)->chan == h2 && hold_of(1, 3, 7)->seq == 5);
    (void)close1(1, 3, 7);

    /* refused ARMs leave the hold OPEN, and it ends at CLOSE */
    (void)open1(1, 11, 4, 7, 0);
    T("ARM on another connection's channel is STALE", arm1(1, 4, 7, other, 1, 0) == NVRM_FENCE_ST_STALE && hold_of(1, 4, 7)->state == E_OPEN);
    T("ARM on a handle that was never given is STALE", arm1(1, 4, 7, h ^ 0x1000, 1, 0) == NVRM_FENCE_ST_STALE && arm1(1, 4, 7, 0, 1, 0) == NVRM_FENCE_ST_STALE);
    T("ARM on a channel that is not verified is STALE", nvf_chan_add(&Tb, 1, 11, 0, &raw) == OK && arm1(1, 4, 7, raw, 1, 0) == NVRM_FENCE_ST_STALE);
    T("ARM for an unknown command buffer or surface is STALE", arm1(1, 77, 7, h, 1, 0) == NVRM_FENCE_ST_STALE && arm1(1, 4, 8, h, 1, 0) == NVRM_FENCE_ST_STALE &&
      arm1(3, 4, 7, h, 1, 0) == NVRM_FENCE_ST_STALE);
    T("each refusal is counted", Tb.stat[NVF_STAT_REFUSED_STALE] == 7);
    T("the hold is still OPEN and blocks", look(7, 0, 0, 0, NULL) == 1 && hold_of(1, 4, 7)->state == E_OPEN);
    T("and ends at its CLOSE", close1(1, 4, 7) == 1 && look(7, 0, 0, 0, NULL) == 0);
    T("a failed verification frees the channel and hands its index back", nvf_chan_verify(&Tb, 1, raw, 0, &fi) == NVRM_FENCE_ST_MAP &&
      fi == IDX(raw) && Tb.stat[NVF_STAT_MAP_MISMATCH] == 1 && nvf_chan_verify(&Tb, 1, raw, 1, &fi) == NVRM_FENCE_ST_STALE);
    T("verifying a verified channel again changes nothing, whatever it says", nvf_chan_verify(&Tb, 1, h, 0, &fi) == OK && fi == NVF_NIL &&
      Tb.c[IDX(h)].verified == 1);

    /* ARM_DONE and a value already reached */
    (void)open1(1, 11, 5, 7, COMP | AUTO);
    T("ARM_DONE ends an AUTO hold, with no channel named", arm1(1, 5, 7, 0, 0, NVRM_FENCE_F_ARM_DONE) == OK && hold_of(1, 5, 7) == NULL && Tb.live == 0);
    (void)open1(1, 11, 6, 7, 0);
    T("ARM_DONE turns an application hold GPU_DONE", arm1(1, 6, 7, 0, 0, NVRM_FENCE_F_ARM_DONE) == OK && hold_of(1, 6, 7)->state == E_DONE);
    (void)close1(1, 6, 7);
    (void)open1(1, 11, 7, 7, COMP | AUTO);
    T("ARM on a value the GPU has already reached ends an AUTO hold at once", arm1(1, 7, 7, h, 20, 0) == OK && hold_of(1, 7, 7) == NULL &&
      Tb.stat[NVF_STAT_ARM_DONE] == 3);
    T("the lists agree", structure(&Tb) == NULL);

    /* re-arming cannot stretch a hold: the short bound keeps counting from the first completion */
    fresh();
    h = chan_new(1, 11);
    (void)open1(1, 11, 1, 7, 0); (void)arm1(1, 1, 7, h, 0, NVRM_FENCE_F_ARM_DONE);
    for (int i = 0; i < 3; i++) { now += MS(40); (void)arm1(1, 1, 7, h, 0, NVRM_FENCE_F_ARM_DONE); }
    T("a hold armed again and again still times out 100 ms after it first completed", look(7, 0, 0, 0, NULL) == 0 && Tb.stat[NVF_STAT_EXPIRED_CPU] == 1);
}

static void arm_filters(void)
{
    unsigned char pl;
    unsigned sid = 7;
    fresh();
    (void)open1(1, 11, 1, 7, 0);                    /* application, pid 11 */
    (void)open1(2, 22, 1, 7, COMP);                 /* compositor, pid 22 */
    T("a plain reader sees both", look(7, 0, 0, 0, NULL) == 2);
    T("a COMPOSITOR reader ignores the application hold", look(7, 0, 0, COMP, NULL) == 1);
    T("a reader for pid 11 ignores pid 22", look(7, 0, 11, 0, NULL) == 1 && look(7, 0, 22, 0, NULL) == 1 && look(7, 0, 33, 0, NULL) == 0);
    T("both filters together", look(7, 0, 11, COMP, NULL) == 0 && look(7, 0, 22, COMP, NULL) == 1);
    (void)close1(1, 1, 7); (void)close1(2, 1, 7);
    pl = 1; (void)nvf_open(&Tb, 1, 11, 2, 1, &sid, &pl, 0, now);       /* plane 0 only */
    T("a reader of plane 1 is not delayed by a hold on plane 0", look(7, 2, 0, 0, NULL) == 0);
    T("a reader of plane 0, or of any plane, is", look(7, 1, 0, 0, NULL) == 1 && look(7, 0, 0, 0, NULL) == 1 && look(7, 3, 0, 0, NULL) == 1);
    pl = 0; (void)nvf_open(&Tb, 1, 11, 3, 1, &sid, &pl, 0, now);       /* every plane */
    T("a hold on every plane delays a reader of any", look(7, 2, 0, 0, NULL) == 1 && look(7, 1, 0, 0, NULL) == 2);
}

static void arm_snapshot(void)
{
    struct nvf_query q = { 7, 0, 0, 0, 0, 0 };
    struct nvf_view v;
    fresh();
    (void)open1(1, 11, 1, 7, 0);
    T("the reader arrives: one hold in front of it", nvf_eval(&Tb, &q, now, NVF_WHY_FIRST, hw_read, NULL, &v) == 1 && q.snap != 0);
    (void)open1(1, 11, 2, 7, 0);                    /* the next frame is committed while the reader waits */
    T("a hold opened after its first look is not in its way", nvf_eval(&Tb, &q, now, NVF_WHY_SLICE, hw_read, NULL, &v) == 1 && v.blockers == 1);
    (void)close1(1, 1, 7);
    T("the write it arrived on ends: the reader is free although the surface is held again",
      nvf_eval(&Tb, &q, now, NVF_WHY_STATE, hw_read, NULL, &v) == 0 && Tb.live == 1);
    T("and its answer is READY", nvf_result_from_recent(&Tb, &q, 1) == NVRM_FENCE_R_READY);
    T("a reader arriving now waits for the second write", look(7, 0, 0, 0, NULL) == 1);
}

static void arm_endings(void)
{
    unsigned h, fi[NVF_OWNER_CHANS], n, one = 0;
    struct NVRMFenceWait w;
    fresh(); w_reset();
    h = chan_new(1, 11); sH = h;
    (void)open1(1, 11, 1, 7, 0); (void)arm1(1, 1, 7, h, 100, 0);
    W.during = s_owner_gone;
    T("the writer's connection closes while a reader sleeps: DEAD", wait_for(7, 0, 0, 2000, 0, &w) == OK && w.result == NVRM_FENCE_R_DEAD &&
      w.ownerPid == 11 && w.state == E_ARMED && w.blockers == 1 && w.waitedUs == 2000);
    T("its holds are gone, its channel index was handed back, its record is free", Tb.live == 0 && Tb.used == 0 &&
      nvf_owner_find(&Tb, 1) < 0 && Tb.stat[NVF_STAT_END_OWNER] == 1 && Tb.stat[NVF_STAT_CHANS] == 0);
    T("its handle is refused afterwards", nvf_chan_del(&Tb, 1, h, now, hw_read, NULL, &one) == NVRM_FENCE_ST_STALE);
    T("a connection that is unknown, or 0, is ignored", nvf_owner_gone(&Tb, 5, fi, NVF_OWNER_CHANS) == 0 && nvf_owner_gone(&Tb, 0, fi, NVF_OWNER_CHANS) == 0);

    fresh();
    for (n = 0; n < 5; n++) (void)chan_new(1, 11);
    T("a closing connection's channels come back a few at a time when asked so", nvf_owner_gone(&Tb, 1, fi, 2) == 2 && nvf_owner_find(&Tb, 1) >= 0 &&
      nvf_owner_gone(&Tb, 1, fi, 2) == 2 && nvf_owner_gone(&Tb, 1, fi, 2) == 1 && nvf_owner_find(&Tb, 1) < 0 && Tb.stat[NVF_STAT_CHANS] == 0);

    fresh(); w_reset();
    h = chan_new(1, 11); sH = h;
    (void)open1(1, 11, 1, 7, 0); (void)arm1(1, 1, 7, h, 100, 0);
    W.during = s_fault;
    T("the channel faults: DEAD", wait_for(7, 0, 0, 2000, 0, &w) == OK && w.result == NVRM_FENCE_R_DEAD && Tb.stat[NVF_STAT_END_CHAN_ERR] == 1 && Tb.live == 0);
    hw[IDX(h)].done = 200;
    (void)open1(1, 11, 2, 7, 0); (void)arm1(1, 2, 7, h, 150, 0);
    T("work the GPU finished before the fault counts as finished", hold_of(1, 2, 7) && hold_of(1, 2, 7)->state == E_DONE);
    (void)open1(1, 11, 3, 8, 0);
    T("a hold armed on a faulted channel ends at once", arm1(1, 3, 8, h, 300, 0) == OK && hold_of(1, 3, 8) == NULL && Tb.stat[NVF_STAT_END_CHAN_ERR] == 2);

    fresh();
    h = chan_new(1, 11);
    (void)open1(1, 11, 1, 7, COMP | AUTO); (void)arm1(1, 1, 7, h, 10, 0);
    (void)open1(1, 11, 2, 8, 0); (void)arm1(1, 2, 8, h, 10, 0);
    (void)open1(1, 11, 3, 9, 0); (void)arm1(1, 3, 9, h, 50, 0);
    (void)open1(1, 11, 4, 6, 0);
    hw[IDX(h)].done = 10;
    {
        struct nvf_query q7 = { 7, 0, 0, 0, 0, 0 }, q9 = { 9, 0, 0, 0, 0, 0 };
        struct nvf_view v;
        q7.snap = q9.snap = Tb.serial; q7.recent0 = q9.recent0 = Tb.recentSeq;
        T("CHAN_DEL hands the index back", nvf_chan_del(&Tb, 1, h, now, hw_read, NULL, &one) == OK && one == IDX(h));
        T("deleted after the value: the AUTO hold ended as complete (READY)", nvf_eval(&Tb, &q7, now, NVF_WHY_STATE, hw_read, NULL, &v) == 0 &&
          nvf_result_from_recent(&Tb, &q7, 1) == NVRM_FENCE_R_READY);
        T("deleted before the value: DEAD", nvf_eval(&Tb, &q9, now, NVF_WHY_STATE, hw_read, NULL, &v) == 0 &&
          nvf_result_from_recent(&Tb, &q9, 1) == NVRM_FENCE_R_DEAD && Tb.stat[NVF_STAT_END_CHAN_DEL] == 1);
    }
    T("the application hold whose value was reached waits for its CLOSE", hold_of(1, 2, 8) && hold_of(1, 2, 8)->state == E_DONE);
    T("a hold that was never armed is untouched", hold_of(1, 4, 6) && hold_of(1, 4, 6)->state == E_OPEN);
    T("the deleted handle is refused everywhere", arm1(1, 4, 6, h, 1, 0) == NVRM_FENCE_ST_STALE && nvf_chan_del(&Tb, 1, h, now, hw_read, NULL, &one) == NVRM_FENCE_ST_STALE &&
      nvf_chan_rebind(&Tb, 1, h, &one) == NVRM_FENCE_ST_STALE && nvf_chan_verify(&Tb, 1, h, 1, &one) == NVRM_FENCE_ST_STALE);
    n = chan_new(1, 11);
    T("the slot is used again under a new handle", n && IDX(n) == IDX(h) && n != h && arm1(1, 4, 6, h, 1, 0) == NVRM_FENCE_ST_STALE && arm1(1, 4, 6, n, 1, 0) == OK);
    T("the lists agree", structure(&Tb) == NULL);

    fresh();
    h = chan_new(1, 11);
    hw[IDX(h)].mapped = 0;
    (void)open1(1, 11, 1, 7, 0); (void)arm1(1, 1, 7, h, 10, 0);
    T("a channel whose pages cannot be read counts as faulted: nobody waits on it", look(7, 0, 0, 0, NULL) == 0 && Tb.stat[NVF_STAT_END_CHAN_ERR] == 1);
}

/* A process exits. NVK's fence requests (its holds and channel registrations) go on a connection of their own, owner 1
   here, while its RM objects live on another connection, which may close first and free the channels. Whichever
   connection of the dying process closes first ends the fence state of all of them. */
static void arm_pid_gone(void)
{
    unsigned h, h2, other, fi[NVF_OWNER_CHANS], one = 0, n, changes;
    struct NVRMFenceWait w;
    int fast = 0;
    fresh(); w_reset();
    h = chan_new(1, 11); sH = h;
    h2 = chan_new(2, 11);                            /* a second fence connection of the same process */
    other = chan_new(3, 22);                         /* another process */
    (void)open1(1, 11, 1, 7, 0); (void)arm1(1, 1, 7, h, 100, 0);
    (void)open1(2, 11, 1, 8, 0);
    (void)open1(3, 22, 1, 9, COMP | AUTO); (void)arm1(3, 1, 9, other, 50, 0);
    W.during = s_pid_gone;
    T("the process exits while a reader sleeps on its armed hold: the reader leaves at once with DEAD", wait_for(7, 0, 0, 2000, 0, &w) == OK &&
      w.result == NVRM_FENCE_R_DEAD && w.waitedUs == 2000 && w.ownerPid == 11);
    T("every hold of every connection of that process ended, as at a close: no strike", Tb.live == 1 && Tb.used == 1 && look(8, 0, 0, 0, NULL) == 0 &&
      Tb.stat[NVF_STAT_END_OWNER] == 2 && Tb.stat[NVF_STAT_STRIKES] == 0 && Tb.stat[NVF_STAT_EXPIRED_GPU] == 0);
    T("and all its channels are out of the table", Tb.stat[NVF_STAT_CHANS] == 1 && !Tb.c[IDX(h)].used && !Tb.c[IDX(h2)].used &&
      Tb.o[nvf_owner_find(&Tb, 1)].chans == 0 && Tb.o[nvf_owner_find(&Tb, 2)].chans == 0);
    T("its two connections are counted once each", Tb.stat[NVF_STAT_PID_GONE] == 2);
    T("another process keeps its hold and channel", look(9, 0, 0, COMP, NULL) == 1 && Tb.c[IDX(other)].used);
    T("each record stays, empty and closed, until its own connection closes",
      nvf_owner_find(&Tb, 1) >= 0 && Tb.o[nvf_owner_find(&Tb, 1)].closed && Tb.o[nvf_owner_find(&Tb, 2)].closed &&
      !Tb.o[nvf_owner_find(&Tb, 3)].closed && Tb.o[nvf_owner_find(&Tb, 1)].entries == 0);
    {
        unsigned hh = 0;
        T("a request still in flight on it adds nothing: OPEN and CHAN_ADD are OFF", open1(1, 11, 2, 7, 0) == NVRM_FENCE_ST_OFF &&
          nvf_chan_add(&Tb, 2, 11, 0, &hh) == NVRM_FENCE_ST_OFF && hh == 0 && Tb.used == 1 && Tb.stat[NVF_STAT_CHANS] == 1);
    }
    T("the rest finds nothing", arm1(1, 1, 7, h, 100, 0) == NVRM_FENCE_ST_STALE && close1(1, 1, 7) == 0 &&
      nvf_chan_del(&Tb, 1, h, now, hw_read, NULL, &one) == NVRM_FENCE_ST_STALE && nvf_chan_rebind(&Tb, 1, h, &one) == NVRM_FENCE_ST_STALE &&
      nvf_chan_verify(&Tb, 2, h2, 1, &one) == NVRM_FENCE_ST_STALE && nvf_seq_eval(&Tb, 1, h, 1, NVF_WHY_FIRST, hw_read, NULL, &fast) == -1);
    changes = Tb.changes;
    T("a second call for the same process does nothing", nvf_pid_gone(&Tb, 11, fi, NVF_OWNER_CHANS) == 0 && Tb.stat[NVF_STAT_PID_GONE] == 2 &&
      Tb.changes == changes && Tb.live == 1);
    T("pid 0, or a pid with nothing, does nothing", nvf_pid_gone(&Tb, 0, fi, NVF_OWNER_CHANS) == 0 && nvf_pid_gone(&Tb, 33, fi, NVF_OWNER_CHANS) == 0 &&
      Tb.stat[NVF_STAT_PID_GONE] == 2);
    T("each connection's own close frees its record", nvf_owner_gone(&Tb, 1, fi, NVF_OWNER_CHANS) == 0 && nvf_owner_find(&Tb, 1) < 0 &&
      nvf_owner_gone(&Tb, 2, fi, NVF_OWNER_CHANS) == 0 && nvf_owner_find(&Tb, 2) < 0);
    T("a later process with the same pid has records of its own and is served", open1(4, 11, 1, 7, 0) == OK && chan_new(4, 11) != 0 &&
      !Tb.o[nvf_owner_find(&Tb, 4)].closed);
    T("the lists agree", structure(&Tb) == NULL);

    /* many channels: they come back a batch at a time, but every hold ends in the first call */
    fresh();
    for (n = 0; n < NVF_OWNER_CHANS; n++) { (void)chan_new(1, 11); (void)chan_new(2, 11); }
    (void)open1(1, 11, 1, 7, 0); (void)open1(2, 11, 1, 8, 0);
    changes = Tb.changes;
    T("the first call ends every hold of the process and hands back one full batch", nvf_pid_gone(&Tb, 11, fi, NVF_OWNER_CHANS) == NVF_OWNER_CHANS &&
      Tb.live == 0 && Tb.o[nvf_owner_find(&Tb, 1)].closed && Tb.o[nvf_owner_find(&Tb, 2)].closed && Tb.changes != changes);
    T("the next hands back the rest, then nothing", nvf_pid_gone(&Tb, 11, fi, NVF_OWNER_CHANS) == NVF_OWNER_CHANS &&
      nvf_pid_gone(&Tb, 11, fi, NVF_OWNER_CHANS) == 0 && Tb.stat[NVF_STAT_CHANS] == 0 && Tb.stat[NVF_STAT_PID_GONE] == 2);
    fresh();
    (void)chan_new(1, 11);
    changes = Tb.changes;
    T("channels alone: the table still counts a change, so a producer asleep on one of them looks again",
      nvf_pid_gone(&Tb, 11, fi, NVF_OWNER_CHANS) == 1 && Tb.changes != changes);
    T("the lists agree after the batches", structure(&Tb) == NULL);
}

static void arm_rebind(void)
{
    unsigned h, raw = 0, idx = 0, other;
    struct nvf_chan before;
    fresh();
    h = chan_new(1, 11); other = chan_new(2, 22);
    (void)open1(1, 11, 1, 7, 0); (void)arm1(1, 1, 7, h, 10, 0);          /* will die with the fault */
    (void)open1(1, 11, 2, 8, 0);                                         /* not armed yet */
    Tb.c[IDX(h)].found = 20; Tb.c[IDX(h)].foundSlice = 3;
    hw[IDX(h)].done = 5; hw[IDX(h)].faulted = 1;
    T("the fault ends what was armed", look(7, 0, 0, 0, NULL) == 0 && Tb.c[IDX(h)].faulted == 1 && Tb.c[IDX(h)].done == 5);
    before = Tb.c[IDX(h)];
    T("a rebind of an unknown handle, another connection's channel or an unverified one is STALE",
      nvf_chan_rebind(&Tb, 1, h ^ 0x1000, &idx) == NVRM_FENCE_ST_STALE && idx == NVF_NIL &&
      nvf_chan_rebind(&Tb, 1, other, &idx) == NVRM_FENCE_ST_STALE && nvf_chan_rebind(&Tb, 2, h, &idx) == NVRM_FENCE_ST_STALE &&
      nvf_chan_add(&Tb, 1, 11, 0, &raw) == OK && nvf_chan_rebind(&Tb, 1, raw, &idx) == NVRM_FENCE_ST_STALE);
    T("and leaves the channel as it was: still faulted", Tb.c[IDX(h)].faulted == 1 && Tb.c[IDX(h)].done == before.done &&
      Tb.c[IDX(h)].gen == before.gen && Tb.stat[NVF_STAT_CHAN_REBINDS] == 0);
    {
        const unsigned live = Tb.live, used = Tb.used, changes = Tb.changes;
        T("the rebind is accepted and names the channel", nvf_chan_rebind(&Tb, 1, h, &idx) == OK && idx == IDX(h) && Tb.stat[NVF_STAT_CHAN_REBINDS] == 1);
        T("the fault is forgotten", Tb.c[IDX(h)].faulted == 0);
        T("nothing else changed: handle, owner, verified, value seen, interrupt record, holds",
          Tb.c[IDX(h)].gen == before.gen && Tb.c[IDX(h)].owner == before.owner && Tb.c[IDX(h)].verified == 1 &&
          Tb.c[IDX(h)].done == before.done && Tb.c[IDX(h)].found == 20 && Tb.c[IDX(h)].foundSlice == 3 &&
          Tb.live == live && Tb.used == used && Tb.changes == changes && hold_of(1, 2, 8) && hold_of(1, 2, 8)->state == E_OPEN);
    }
    hw[IDX(h)].faulted = 0;                          /* the kernel now reads the new notifier */
    T("the same handle arms again", arm1(1, 2, 8, h, 9, 0) == OK && look(8, 0, 0, 0, NULL) == 1);
    hw[IDX(h)].done = 9;
    T("and completion is seen through it", look(8, 0, 0, 0, NULL) == 1 && hold_of(1, 2, 8)->state == E_DONE);
    hw[IDX(h)].faulted = 1;
    (void)open1(1, 11, 3, 9, 0); (void)arm1(1, 3, 9, h, 99, 0);
    T("a fault on the new notifier is seen as well", hold_of(1, 3, 9) == NULL && Tb.c[IDX(h)].faulted == 1);
    Tb.off = 1;
    T("a rebind while the layer is off is refused and changes nothing", nvf_chan_rebind(&Tb, 1, h, &idx) == NVRM_FENCE_ST_OFF && Tb.c[IDX(h)].faulted == 1);
}

static void arm_bounds(void)
{
    unsigned h, h2;
    fresh();
    h = chan_new(1, 11);
    (void)open1(1, 11, 1, 7, 0); (void)arm1(1, 1, 7, h, 10, 0);
    now += MS(1999);
    T("an armed hold is in the way at 1999 ms", look(7, 0, 0, 0, NULL) == 1 && Tb.flagged == 0);
    now += MS(1);
    T("and is given up at 2000 ms, with a strike", look(7, 0, 0, 0, NULL) == 0 && Tb.flagged == 1 && Tb.stat[NVF_STAT_EXPIRED_GPU] == 1 &&
      Tb.stat[NVF_STAT_STRIKES] == 1 && Tb.o[nvf_owner_find(&Tb, 1)].strikes == 1);
    T("the given-up hold is in nobody's way", look(7, 0, 0, 0, NULL) == 0 && Tb.live == 0 && Tb.used == 1);
    T("the kernel wants a log line for it", Tb.evN == 1 && Tb.ev[0].kind == NVF_EV_STRIKE && Tb.ev[0].sid == 7 && Tb.ev[0].pid == 11 &&
      Tb.ev[0].ms == 2000 && Tb.ev[0].state == E_ARMED && Tb.ev[0].bound == NVF_BOUND_GPU && Tb.ev[0].strikes == 1);
    T("its CLOSE frees the slot", close1(1, 1, 7) == 1 && Tb.used == 0 && Tb.flagged == 0);

    /* the penalty window: 10 s of 20 ms bounds, and its expiries are not strikes */
    (void)open1(1, 11, 2, 7, 0); (void)arm1(1, 2, 7, h, 10, 0);
    now += MS(19);
    T("in the penalty window a hold is in the way at 19 ms", look(7, 0, 0, 0, NULL) == 1);
    now += MS(1);
    T("and given up at 20 ms, whatever its state, without a strike", look(7, 0, 0, 0, NULL) == 0 && Tb.stat[NVF_STAT_EXPIRED_PENALTY] == 1 &&
      Tb.stat[NVF_STAT_STRIKES] == 1);
    (void)close1(1, 2, 7);
    now = MS(1000) + MS(2000) + MS(10000) - MS(30);
    (void)open1(1, 11, 3, 7, 0);
    now += MS(29);
    T("the window lasts until 10 s after the strike", look(7, 0, 0, 0, NULL) == 0 && Tb.stat[NVF_STAT_EXPIRED_PENALTY] == 2);
    (void)close1(1, 3, 7);
    now += MS(1);
    (void)open1(1, 11, 4, 7, 0);
    now += MS(99);
    T("after it the usual bounds are back: an unarmed hold is in the way at 99 ms", look(7, 0, 0, 0, NULL) == 1);
    now += MS(1);
    T("and given up at 100 ms when its writer's GPU is idle: the second strike", look(7, 0, 0, 0, NULL) == 0 && Tb.stat[NVF_STAT_EXPIRED_CPU] == 1 &&
      Tb.stat[NVF_STAT_STRIKES] == 2);
    (void)close1(1, 4, 7);
    {
        const unsigned long long second = now;
        now = second + MS(20000) - MS(21);
        (void)open1(1, 11, 5, 7, 0);
        now += MS(20);
        T("the second window is 20 s", look(7, 0, 0, 0, NULL) == 0 && Tb.stat[NVF_STAT_EXPIRED_PENALTY] == 3 && Tb.stat[NVF_STAT_STRIKES] == 2);
        (void)close1(1, 5, 7);
        /* forgiveness: one strike per 10 minutes without one */
        now = second + MS(600000) + MS(5);
        (void)open1(1, 11, 6, 7, 0);
        now += MS(100);
        T("ten minutes later one strike is forgiven: this one is the second again, not the third", look(7, 0, 0, 0, NULL) == 0 &&
          Tb.o[nvf_owner_find(&Tb, 1)].strikes == 2 && Tb.o[nvf_owner_find(&Tb, 1)].distrusted == 0 && Tb.stat[NVF_STAT_STRIKES] == 3);
        (void)close1(1, 6, 7);
    }
    {
        /* forgiveness counts whole periods of 10 minutes since the last strike: a connection with two strikes stalls again */
        static const unsigned long long gapMs[3] = { 599999, 600000, 1200000 };
        unsigned strikes[3], distrusted[3];
        for (unsigned k = 0; k < 3; k++) {
            fresh();
            now = MS(5000000);
            (void)open1(1, 11, 1, 7, 0);
            Tb.o[nvf_owner_find(&Tb, 1)].strikes = 2; Tb.o[nvf_owner_find(&Tb, 1)].lastStrikeNs = now + MS(100) - MS(gapMs[k]);
            now += MS(100);
            (void)look(7, 0, 0, 0, NULL);
            strikes[k] = Tb.o[nvf_owner_find(&Tb, 1)].strikes; distrusted[k] = Tb.o[nvf_owner_find(&Tb, 1)].distrusted;
        }
        T("a stall 599,999 ms after the second strike is the third: distrusted", strikes[0] == 3 && distrusted[0] == 1);
        T("at exactly 600,000 ms one strike was forgiven: it is the second again", strikes[1] == 2 && distrusted[1] == 0);
        T("after 20 minutes both were: it is the first", strikes[2] == 1 && distrusted[2] == 0);
    }

    /* a writer whose GPU still has work in flight gets the long bound for unarmed and finished holds */
    fresh();
    h = chan_new(1, 11); h2 = chan_new(1, 11);
    (void)open1(1, 11, 1, 7, 0);
    hw[IDX(h2)].done = 4; hw[IDX(h2)].flushed = 5;
    now += MS(1999);
    T("an unarmed hold lasts while one of its connection's channels has work in flight", look(7, 0, 0, 0, NULL) == 1);
    hw[IDX(h2)].done = 5;
    T("and is given up as soon as that GPU is idle", look(7, 0, 0, 0, NULL) == 0 && Tb.stat[NVF_STAT_EXPIRED_CPU] == 1);
    fresh();
    h = chan_new(1, 11); h2 = chan_new(1, 11);
    (void)open1(1, 11, 1, 7, 0); (void)arm1(1, 1, 7, h, 3, 0);
    hw[IDX(h)].done = 3; hw[IDX(h2)].flushed = 9;
    T("GPU_DONE", look(7, 0, 0, 0, NULL) == 1 && hold_of(1, 1, 7)->state == E_DONE);
    now += MS(1999);
    T("a finished hold also lasts while the GPU is busy", look(7, 0, 0, 0, NULL) == 1);
    now += MS(1);
    T("but not beyond 2000 ms", look(7, 0, 0, 0, NULL) == 0 && Tb.stat[NVF_STAT_EXPIRED_GPU] == 1);
    fresh();
    h = chan_new(1, 11); h2 = chan_new(2, 22);
    (void)open1(1, 11, 1, 7, 0);
    hw[IDX(h2)].flushed = 9;                         /* somebody else's GPU work */
    hw[IDX(h)].flushed = 9; hw[IDX(h)].faulted = 1;  /* its own channel, but faulted */
    now += MS(100);
    T("another connection's work, or a faulted channel, does not extend it", look(7, 0, 0, 0, NULL) == 0 && Tb.stat[NVF_STAT_EXPIRED_CPU] == 1);

    /* a penalty window does not shorten a compositor hold below the CPU bound: NVAccel's flips and page-offs wait on
       those, and 2.9 waited 100 ms on the mark there */
    fresh();
    h = chan_new(1, 11);
    (void)open1(1, 11, 1, 7, 0);
    now += MS(100);
    (void)look(7, 0, 0, 0, NULL);                    /* the first strike: a 10 s penalty window */
    (void)close1(1, 1, 7);
    {
        const unsigned long long t0 = now;
        struct nvf_view v;
        (void)open1(1, 11, 2, 7, COMP); (void)arm1(1, 2, 7, h, 10, 0);
        (void)open1(1, 11, 3, 8, 0); (void)arm1(1, 3, 8, h, 10, 0);
        T("in a penalty window a compositor hold is told the 100 ms bound", look(7, 0, 0, COMP, &v) == 1 && v.deadlineNs == t0 + MS(100));
        now += MS(20);
        T("the application hold of the same connection is given up at 20 ms", look(8, 0, 0, 0, NULL) == 0 && Tb.stat[NVF_STAT_EXPIRED_PENALTY] == 1);
        T("the compositor hold is still in the way at 20 ms", look(7, 0, 0, COMP, NULL) == 1);
        now += MS(79);
        T("and at 99 ms", look(7, 0, 0, 0, NULL) == 1);
        now += MS(1);
        T("it is given up at 100 ms as a penalty expiry: no strike", look(7, 0, 0, COMP, NULL) == 0 && Tb.stat[NVF_STAT_EXPIRED_PENALTY] == 2 &&
          Tb.stat[NVF_STAT_STRIKES] == 1 && Tb.stat[NVF_STAT_EXPIRED_GPU] == 0);
    }

    /* the sleep deadline a reader is given */
    fresh();
    h = chan_new(1, 11);
    (void)open1(1, 11, 1, 7, 0); (void)open1(1, 11, 2, 7, 0); (void)arm1(1, 2, 7, h, 10, 0);
    {
        struct nvf_view v;
        T("the reader is told the earliest bound, the first writer and its state", look(7, 0, 0, 0, &v) == 2 && v.deadlineNs == now + MS(100) &&
          v.firstPid == 11 && v.firstState == E_ARMED && v.fast == 1);
    }
}

static void arm_distrust(void)
{
    unsigned i, h;
    fresh();
    h = chan_new(1, 11);
    for (i = 0; i < 3; i++) {
        (void)open1(1, 11, 10 + i, 7, 0);
        now += MS(100);
        if (i == 2) { (void)open1(1, 11, 20, 8, 0); (void)open1(1, 11, 21, 9, COMP | AUTO); (void)arm1(1, 21, 9, h, 5, 0); }   /* in flight */
        (void)look(7, 0, 0, 0, NULL);
        if (i < 2) { (void)close1(1, 10 + i, 7); now += MS(40000); }   /* past every penalty window, inside the forgiveness window */
    }
    T("the third strike distrusts the connection", Tb.stat[NVF_STAT_STRIKES] == 3 && Tb.o[nvf_owner_find(&Tb, 1)].distrusted == 1 &&
      Tb.distrustedOwners == 1 && Tb.stat[NVF_STAT_OWNERS_DISTRUSTED] == 1);
    /* its producer took those surfaces out of the 2.9 marks when it opened them: letting their readers go now would
       let them read work the GPU is still writing */
    T("its holds in flight are not let go with it: they still delay readers", look(8, 0, 0, 0, NULL) == 1 && look(9, 0, 0, COMP, NULL) == 1 &&
      Tb.live == 2 && Tb.liveCompositor == 1 && Tb.flagged == 1 && Tb.stat[NVF_STAT_RECLAIMED] == 0);
    T("its OPENs are refused from now on (that process keeps the older mechanism)", open1(1, 11, 30, 7, 0) == NVRM_FENCE_ST_DISTRUST &&
      Tb.stat[NVF_STAT_REFUSED_DISTRUST] == 1);
    T("but it may still arm and close what it has", arm1(1, 20, 8, h, 9, 0) == OK && hold_of(1, 20, 8)->state == E_ARMED);
    hw[IDX(h)].done = 5;
    T("a hold in flight ends by its GPU work", look(9, 0, 0, COMP, NULL) == 0 && Tb.stat[NVF_STAT_AUTO] == 1 && Tb.liveCompositor == 0);
    now += MS(19);
    T("one that does not complete is in the way until the penalty bound the strike set", look(8, 0, 0, 0, NULL) == 1);
    now += MS(1);
    T("and is given up there, without another strike", look(8, 0, 0, 0, NULL) == 0 && Tb.stat[NVF_STAT_EXPIRED_PENALTY] == 1 &&
      Tb.stat[NVF_STAT_STRIKES] == 3 && Tb.live == 0);
    T("other connections are not affected", open1(2, 22, 1, 7, 0) == OK && look(7, 0, 0, 0, NULL) == 1);
    T("its CLOSE still frees its slots", close1(1, 12, 7) == 1 && close1(1, 20, 8) == 1 && Tb.flagged == 0);
    T("the layer is still on", Tb.fused == 0 && Tb.off == 0);
    T("the lists agree", structure(&Tb) == NULL);

    /* a hold of the same connection that a look counted before the strike that distrusted it stays counted */
    fresh();
    (void)open1(1, 11, 1, 7, 0);                     /* oldest: will give the third strike */
    Tb.o[nvf_owner_find(&Tb, 1)].strikes = 2; Tb.o[nvf_owner_find(&Tb, 1)].lastStrikeNs = now;
    now += MS(50);
    (void)open1(1, 11, 2, 7, 0);                     /* newest: walked first, 50 ms younger */
    now += MS(50);
    T("a hold counted in the look that distrusted its connection is still counted", look(7, 0, 0, 0, NULL) == 1 && Tb.flagged == 1 &&
      Tb.o[nvf_owner_find(&Tb, 1)].distrusted == 1);
    T("the next look gives it the penalty bound: 50 ms old, it is given up", look(7, 0, 0, 0, NULL) == 0 && Tb.flagged == 2 &&
      Tb.stat[NVF_STAT_EXPIRED_PENALTY] == 1 && Tb.stat[NVF_STAT_STRIKES] == 1);

    /* a reader asleep on one hold of a connection whose other hold brings the third strike */
    {
        struct NVRMFenceWait w;
        unsigned st;
        fresh(); w_reset();
        h = chan_new(1, 11);
        Tb.c[IDX(h)].found = 20;                     /* interrupts known to arrive: the reader's slice is 2 ms */
        (void)open1(1, 11, 1, 7, 0);                 /* never armed: given up 100 ms from now */
        Tb.o[nvf_owner_find(&Tb, 1)].strikes = 2; Tb.o[nvf_owner_find(&Tb, 1)].lastStrikeNs = now;
        now += MS(99);
        (void)open1(1, 11, 2, 8, 0); (void)arm1(1, 2, 8, h, 100, 0);   /* fresh, armed: good for 2000 ms */
        W.during = s_third_strike;
        st = wait_for(8, 0, 0, 2000, 0, &w);
        T("wakes at the strike, and keeps waiting for its own hold, now only until the penalty bound: TIMEOUT at 20 ms, no new strike",
          st == OK && w.result == NVRM_FENCE_R_TIMEOUT && w.waitedUs == 20000 && Tb.stat[NVF_STAT_WAKE_STATE] == 1 &&
          Tb.stat[NVF_STAT_EXPIRED_PENALTY] == 1 && Tb.stat[NVF_STAT_STRIKES] == 1 && Tb.o[nvf_owner_find(&Tb, 1)].distrusted == 1);
    }

    /* the fuse */
    fresh();
    for (unsigned long long o = 1; o <= NVF_FUSE_OWNERS; o++) {
        (void)open1(o, (unsigned)(100 + o), 1, (unsigned)(40 + o), 0);
        (void)open1(o, (unsigned)(100 + o), 2, 77, 0);
        Tb.o[nvf_owner_find(&Tb, o)].strikes = 2; Tb.o[nvf_owner_find(&Tb, o)].lastStrikeNs = now;
    }
    now += MS(100);
    (void)open1(20, 200, 1, 78, COMP);               /* a well-behaved connection */
    (void)chan_new(20, 200);
    for (unsigned o = 1; o < NVF_FUSE_OWNERS; o++) (void)look(40 + o, 0, 0, 0, NULL);
    T("seven distrusted connections: the layer is still on", Tb.distrustedOwners == 7 && !Tb.fused && look(78, 0, 0, 0, NULL) == 1);
    T("the eighth switches it off and ends everything", look(40 + NVF_FUSE_OWNERS, 0, 0, 0, NULL) == 0 && Tb.fused == 1 && Tb.off == 1 &&
      Tb.live == 0 && Tb.used == 0 && look(78, 0, 0, 0, NULL) == 0);
    T("everything is refused from then on", open1(20, 200, 2, 78, 0) == NVRM_FENCE_ST_OFF && chan_new(21, 201) == 0);
    {
        unsigned one = 0, hh = 0;
        for (unsigned i2 = 0; i2 < NVF_CHANS; i2++) if (Tb.c[i2].used) hh = (Tb.c[i2].gen << 12) | i2;
        T("but a channel can still be deleted, so its pages are let go", hh && nvf_chan_del(&Tb, 20, hh, now, hw_read, NULL, &one) == OK && one == IDX(hh));
    }
    T("the lists agree after the fuse", structure(&Tb) == NULL);
}

static void arm_expired(void)
{
    struct NVRMFenceWait w;
    fresh(); w_reset();
    (void)open1(1, 11, 1, 7, 0);
    T("a reader waits out a hold that never ends: TIMEOUT after exactly its bound", wait_for(7, 0, 0, 2000, 0, &w) == OK &&
      w.result == NVRM_FENCE_R_TIMEOUT && w.waitedUs == 100000 && Tb.stat[NVF_STAT_WAIT_TIMEOUT] == 1);
    T("the next reader does not wait at all", wait_for(7, 0, 0, 2000, 0, &w) == OK && w.result == NVRM_FENCE_R_NONE && w.waitedUs == 0 && W.sleeps > 0);
    T("the hold kept its slot", Tb.used == 1 && Tb.flagged == 1 && Tb.live == 0);
    T("arming it afterwards is accepted and changes nothing", arm1(1, 1, 7, 0, 0, NVRM_FENCE_F_ARM_DONE) == OK && Tb.used == 1 && Tb.flagged == 1);
    T("its CLOSE frees it", close1(1, 1, 7) == 1 && Tb.used == 0 && Tb.flagged == 0);
    T("the lists agree", structure(&Tb) == NULL);
}

/* one piece of work on connection 1's channel, drawn into surface 7, found complete by a reader that `why` woke */
static void work_found(unsigned h, unsigned long long cookie, unsigned long long seq, int why)
{
    struct nvf_query q = { 7, 0, 0, 0, 0, 0 };
    struct nvf_view v;
    (void)open1(1, 11, cookie, 7, COMP | AUTO); (void)arm1(1, cookie, 7, h, seq, 0);
    (void)nvf_eval(&Tb, &q, now, NVF_WHY_FIRST, hw_read, NULL, &v);
    hw[IDX(h)].done = seq;
    (void)nvf_eval(&Tb, &q, now, why, hw_read, NULL, &v);
}

static void arm_trust(void)
{
    struct nvf_view v;
    unsigned h, i, lost, back;
    int fast = 0;
    fresh();
    h = chan_new(1, 11);
    T("a new channel is not trusted", !nvf_chan_trusted(&Tb.c[IDX(h)]));
    for (i = 0; i < 8; i++) {
        struct nvf_query q = { 7, 0, 0, 0, 0, 0 };
        (void)open1(1, 11, 100 + i, 7, COMP | AUTO); (void)arm1(1, 100 + i, 7, h, i + 1, 0);
        if (i == 7) T("until 8 completions were seen its holds are looked at every 250 us", nvf_eval(&Tb, &q, now, NVF_WHY_FIRST, hw_read, NULL, &v) == 1 &&
                      v.fast == 1 && nvf_slice_us(0, NVF_WHY_FIRST, v.fast) == 250);
        else (void)nvf_eval(&Tb, &q, now, NVF_WHY_FIRST, hw_read, NULL, &v);
        hw[IDX(h)].done = i + 1;
        (void)nvf_eval(&Tb, &q, now, NVF_WHY_IRQ, hw_read, NULL, &v);        /* woken by an interrupt, found it complete */
    }
    T("8 completions found on interrupt wakes: trusted", Tb.c[IDX(h)].found == 8 && Tb.c[IDX(h)].foundSlice == 0 && nvf_chan_trusted(&Tb.c[IDX(h)]));
    (void)open1(1, 11, 200, 7, COMP | AUTO); (void)arm1(1, 200, 7, h, 100, 0);
    T("holds on it are no longer looked at fast", look(7, 0, 0, 0, &v) == 1 && v.fast == 0);
    T("slices: 2 ms after a look, an interrupt or a table change", nvf_slice_us(0, NVF_WHY_FIRST, 0) == 2000 && nvf_slice_us(32000, NVF_WHY_IRQ, 0) == 2000 &&
      nvf_slice_us(8000, NVF_WHY_STATE, 0) == 2000);
    T("doubling to 32 ms after slices that found nothing", nvf_slice_us(2000, NVF_WHY_SLICE, 0) == 4000 && nvf_slice_us(4000, NVF_WHY_SLICE, 0) == 8000 &&
      nvf_slice_us(16000, NVF_WHY_SLICE, 0) == 32000 && nvf_slice_us(32000, NVF_WHY_SLICE, 0) == 32000 && nvf_slice_us(250, NVF_WHY_SLICE, 0) == 2000);
    T("a completion found at a first look, or after a table change, says nothing about interrupts", Tb.c[IDX(h)].found == 8);
    T("WAIT_SEQ may sleep on a trusted channel", nvf_seq_eval(&Tb, 1, h, 100, NVF_WHY_FIRST, hw_read, NULL, &fast) == 0 && fast == 0);
    (void)close1(1, 200, 7);

    /* the interrupts stop arriving: completions are found by the slice */
    for (i = 0; i < 12; i++) {
        struct nvf_query q = { 7, 0, 0, 0, 0, 0 };
        (void)open1(1, 11, 300 + i, 7, COMP | AUTO); (void)arm1(1, 300 + i, 7, h, 200 + i, 0);
        (void)nvf_eval(&Tb, &q, now, NVF_WHY_FIRST, hw_read, NULL, &v);
        hw[IDX(h)].done = 200 + i;
        (void)nvf_eval(&Tb, &q, now, NVF_WHY_SLICE, hw_read, NULL, &v);
    }
    T("completions found only by the slice: judged \"no interrupt\"", !nvf_chan_trusted(&Tb.c[IDX(h)]) && Tb.c[IDX(h)].noirq == 1 &&
      Tb.stat[NVF_STAT_CHAN_NOIRQ] == 1);
    (void)open1(1, 11, 400, 7, COMP | AUTO); (void)arm1(1, 400, 7, h, 900, 0);
    T("its holds are looked at every 250 us again", look(7, 0, 0, 0, &v) == 1 && v.fast == 1 && nvf_slice_us(32000, NVF_WHY_SLICE, v.fast) == 250);
    T("and WAIT_SEQ will not sleep on it", nvf_seq_eval(&Tb, 1, h, 900, NVF_WHY_FIRST, hw_read, NULL, &fast) == -2);
    (void)close1(1, 400, 7);
    for (i = 0; i < 40; i++) {
        struct nvf_query q = { 7, 0, 0, 0, 0, 0 };
        (void)open1(1, 11, 500 + i, 7, COMP | AUTO); (void)arm1(1, 500 + i, 7, h, 1000 + i, 0);
        (void)nvf_eval(&Tb, &q, now, NVF_WHY_FIRST, hw_read, NULL, &v);
        hw[IDX(h)].done = 1000 + i;
        (void)nvf_eval(&Tb, &q, now, NVF_WHY_IRQ, hw_read, NULL, &v);
    }
    T("when the interrupts come back the channel is trusted again", nvf_chan_trusted(&Tb.c[IDX(h)]) && Tb.c[IDX(h)].noirq == 0 &&
      Tb.stat[NVF_STAT_CHAN_NOIRQ] == 0 && Tb.c[IDX(h)].found < NVF_TRUST_HALVE);
    T("the kernel wanted one log line for the channel", Tb.evSeen[NVF_EV_NOIRQ] == 1);

    /* the record follows the recent past: however long a channel behaved one way, the change shows within 40 completions */
    fresh();
    h = chan_new(1, 11);
    for (i = 0; i < 200; i++) work_found(h, 1 + i, 1 + i, NVF_WHY_IRQ);
    T("200 completions on interrupts: trusted, and the record stays short", nvf_chan_trusted(&Tb.c[IDX(h)]) && Tb.c[IDX(h)].found < NVF_TRUST_HALVE &&
      Tb.c[IDX(h)].foundSlice == 0);
    for (lost = 0; lost < 40 && nvf_chan_trusted(&Tb.c[IDX(h)]); lost++) work_found(h, 1000 + lost, 1000 + lost, NVF_WHY_SLICE);
    T("then the interrupts stop: judged \"no interrupt\" within 40 completions", lost < 40 && Tb.c[IDX(h)].noirq == 1 && Tb.stat[NVF_STAT_CHAN_NOIRQ] == 1);
    for (i = 0; i < 200; i++) work_found(h, 2000 + i, 2000 + i, NVF_WHY_SLICE);
    T("200 completions without one: still \"no interrupt\", the record still short", Tb.c[IDX(h)].noirq == 1 && Tb.c[IDX(h)].found < NVF_TRUST_HALVE &&
      Tb.c[IDX(h)].foundSlice <= Tb.c[IDX(h)].found);
    for (back = 0; back < 40 && !nvf_chan_trusted(&Tb.c[IDX(h)]); back++) work_found(h, 3000 + back, 3000 + back, NVF_WHY_IRQ);
    T("then they arrive again: trusted within 40 completions", back < 40 && Tb.c[IDX(h)].noirq == 0 && Tb.stat[NVF_STAT_CHAN_NOIRQ] == 0);
    T("every one of those holds ended at its completion", Tb.used == 0 && Tb.stat[NVF_STAT_AUTO] == 400 + lost + back && structure(&Tb) == NULL);
    T("the kernel wanted one log line for this channel too", Tb.evSeen[NVF_EV_NOIRQ] == 1);
}

static void arm_close_agree(void)
{
    unsigned h;
    fresh();
    h = chan_new(1, 11);
    (void)open1(1, 11, 1, 7, 0); (void)arm1(1, 1, 7, h, 10, 0);
    hw[IDX(h)].done = 10;
    (void)close1(1, 1, 7);
    T("CLOSE of an armed hold whose value the kernel sees reached: agree", Tb.stat[NVF_STAT_CLOSE_GPU_AGREE] == 1 && Tb.stat[NVF_STAT_CLOSE_GPU_BEHIND] == 0);
    (void)open1(1, 11, 2, 7, 0); (void)arm1(1, 2, 7, h, 20, 0);
    (void)close1(1, 2, 7);
    T("CLOSE while the kernel's view is short of the value: behind", Tb.stat[NVF_STAT_CLOSE_GPU_AGREE] == 1 && Tb.stat[NVF_STAT_CLOSE_GPU_BEHIND] == 1);
    (void)open1(1, 11, 3, 7, 0);
    (void)close1(1, 3, 7);
    T("CLOSE of a hold that was never armed counts as neither", Tb.stat[NVF_STAT_CLOSE_GPU_AGREE] == 1 && Tb.stat[NVF_STAT_CLOSE_GPU_BEHIND] == 1 &&
      Tb.stat[NVF_STAT_CLOSES] == 3);
}

static void arm_results(void)
{
    struct NVRMFenceWait w;
    unsigned h, i;
    fresh(); w_reset();
    h = chan_new(1, 11); sH = h;
    (void)open1(1, 11, 1, 7, 0); (void)open1(2, 22, 1, 7, 0); (void)open1(3, 33, 1, 8, COMP);
    {
        struct nvf_query q7 = { 7, 0, 0, 0, 0, 0 }, q8 = { 8, 0, 0, 0, 0, 0 }, q9 = { 9, 0, 0, 0, 0, 0 };
        struct nvf_view v;
        (void)nvf_eval(&Tb, &q7, now, NVF_WHY_FIRST, hw_read, NULL, &v); (void)nvf_eval(&Tb, &q8, now, NVF_WHY_FIRST, hw_read, NULL, &v);
        (void)nvf_eval(&Tb, &q9, now, NVF_WHY_FIRST, hw_read, NULL, &v);
        Tb.paused = 1;
        nvf_end_all(&Tb, NVF_END_PM);
        T("the GPU goes to sleep: every hold ends", Tb.live == 0 && Tb.used == 0 && Tb.stat[NVF_STAT_END_PM] == 3 && Tb.liveCompositor == 0);
        T("their readers see DEAD", nvf_eval(&Tb, &q7, now, NVF_WHY_STATE, hw_read, NULL, &v) == 0 && nvf_result_from_recent(&Tb, &q7, 1) == NVRM_FENCE_R_DEAD &&
          nvf_result_from_recent(&Tb, &q8, 1) == NVRM_FENCE_R_DEAD);
        T("a reader that had nothing in front of it sees NONE", nvf_result_from_recent(&Tb, &q9, 0) == NVRM_FENCE_R_NONE);
        T("holds are refused while the GPU sleeps, channels are not", open1(1, 11, 2, 7, 0) == NVRM_FENCE_ST_OFF && chan_new(1, 11) != 0);
        Tb.paused = 0;
        T("and accepted again after it", open1(1, 11, 2, 7, 0) == OK);
        (void)close1(1, 2, 7);
    }
    /* precedence: TIMEOUT > DEAD > READY > NONE */
    fresh(); w_reset();
    h = chan_new(1, 11); sH = h;
    (void)open1(1, 11, 1, 7, 0);                               /* will be closed: READY */
    (void)open1(1, 11, 2, 7, 0); (void)arm1(1, 2, 7, h, 100, 0);   /* will die with the channel: DEAD */
    W.during = s_two_ends;
    T("one hold closed and one killed: DEAD wins over READY", wait_for(7, 0, 0, 2000, 0, &w) == OK && w.result == NVRM_FENCE_R_DEAD && w.blockers == 2);
    fresh(); w_reset();
    (void)open1(1, 11, 1, 7, 0);
    now += MS(60);
    (void)open1(2, 22, 1, 7, 0);
    W.during = NULL;
    {
        struct nvf_query q = { 7, 0, 0, 0, 0, 0 };
        struct nvf_view v;
        (void)nvf_eval(&Tb, &q, now, NVF_WHY_FIRST, hw_read, NULL, &v);
        (void)close1(2, 22, 7); (void)close1(2, 1, 7);
        now += MS(40);
        T("one hold closed and one given up: TIMEOUT wins", nvf_eval(&Tb, &q, now, NVF_WHY_SLICE, hw_read, NULL, &v) == 0 &&
          nvf_result_from_recent(&Tb, &q, 1) == NVRM_FENCE_R_TIMEOUT);
    }
    /* the ring remembers 64 endings */
    fresh();
    {
        struct nvf_query q = { 7, 0, 0, 0, 0, 0 };
        struct nvf_view v;
        (void)open1(1, 11, 1, 7, 0); (void)arm1(1, 1, 7, chan_new(1, 11), 5, 0);
        (void)nvf_eval(&Tb, &q, now, NVF_WHY_FIRST, hw_read, NULL, &v);
        hw[0].faulted = 1;
        (void)look(7, 0, 0, 0, NULL);                         /* another reader sees the fault: the hold ends DEAD */
        for (i = 0; i < NVF_RECENT - 1; i++) { (void)open1(2, 22, 5, 100 + i, 0); (void)close1(2, 5, 100 + i); }
        T("63 other endings later the reader still learns DEAD", nvf_eval(&Tb, &q, now, NVF_WHY_STATE, hw_read, NULL, &v) == 0 &&
          nvf_result_from_recent(&Tb, &q, 1) == NVRM_FENCE_R_DEAD);
        (void)open1(2, 22, 5, 100, 0); (void)close1(2, 5, 100);
        T("after 64 the ring has forgotten: the answer degrades to READY", nvf_result_from_recent(&Tb, &q, 1) == NVRM_FENCE_R_READY);
    }
    /* ...but not when everything ended at once: that is more endings than the ring holds exactly when the work is not done */
    {
        static const unsigned whys[3] = { NVF_END_PM, NVF_END_SHUTDOWN, NVF_END_DISTRUST };
        static const unsigned want[3] = { NVRM_FENCE_R_DEAD, NVRM_FENCE_R_DEAD, NVRM_FENCE_R_TIMEOUT };
        unsigned k, good = 0, few = 0;
        for (k = 0; k < 3; k++) {
            struct nvf_query q = { 7, 0, 0, 0, 0, 0 }, q2 = { 7, 0, 0, 0, 0, 0 };
            struct nvf_view v;
            fresh();
            (void)open1(1, 11, 1, 7, 0);
            (void)nvf_eval(&Tb, &q2, now, NVF_WHY_FIRST, hw_read, NULL, &v);
            nvf_end_all(&Tb, whys[k]);                        /* one hold: the ring knows how it ended */
            if (nvf_eval(&Tb, &q2, now, NVF_WHY_STATE, hw_read, NULL, &v) == 0 && nvf_result_from_recent(&Tb, &q2, 1) == want[k]) few++;
            fresh();
            (void)open1(1, 11, 1, 7, 0);
            for (i = 0; i < NVF_RECENT + 6; i++) (void)open1(2, 22, 5, 100 + i, 0);
            if (nvf_eval(&Tb, &q, now, NVF_WHY_FIRST, hw_read, NULL, &v) != 1) continue;
            nvf_end_all(&Tb, whys[k]);                        /* 71 holds: the reader's own is no longer in the ring */
            if (Tb.recentSeq - q.recent0 > NVF_RECENT && nvf_eval(&Tb, &q, now, NVF_WHY_STATE, hw_read, NULL, &v) == 0 &&
                nvf_result_from_recent(&Tb, &q, 1) == want[k]) good++;
        }
        T("sleep, the last stop, the switch-off with one hold alive: its reader learns DEAD, DEAD, TIMEOUT from the ring", few == 3);
        T("the same with 71 holds alive, more than the ring remembers: still DEAD, DEAD, TIMEOUT, not READY", good == 3);
    }
    fresh();
    {
        struct nvf_query q = { 7, 0, 0, 0, 0, 0 };
        struct nvf_view v;
        (void)open1(1, 11, 9, 9, 0);
        nvf_end_all(&Tb, NVF_END_PM);                         /* a sleep that ended something, before this reader arrived */
        (void)open1(1, 11, 1, 7, 0);
        (void)nvf_eval(&Tb, &q, now, NVF_WHY_FIRST, hw_read, NULL, &v);
        (void)close1(1, 1, 7);
        for (i = 0; i < NVF_RECENT; i++) { (void)open1(2, 22, 5, 100 + i, 0); (void)close1(2, 5, 100 + i); }
        T("a sleep before the reader arrived says nothing about its holds: past the ring its answer is READY, as before",
          Tb.recentSeq - q.recent0 > NVF_RECENT && nvf_eval(&Tb, &q, now, NVF_WHY_STATE, hw_read, NULL, &v) == 0 &&
          nvf_result_from_recent(&Tb, &q, 1) == NVRM_FENCE_R_READY);
    }
    fresh();
    nvf_end_all(&Tb, NVF_END_SHUTDOWN);
    T("ending everything on an empty table still wakes sleepers", Tb.changes == 1 && Tb.live == 0);
}

static void arm_wait(void)
{
    struct NVRMFenceWait w;
    unsigned h, st;
    fresh(); w_reset();
    T("nothing pending: NONE, no sleep", wait_for(7, 0, 0, 2000, 0, &w) == OK && w.result == NVRM_FENCE_R_NONE && W.sleeps == 0 && w.waitedUs == 0 &&
      w.ownerPid == 0 && w.state == 0 && w.blockers == 0 && Tb.stat[NVF_STAT_WAIT_NONE] == 1 && Tb.stat[NVF_STAT_WAITS] == 0);
    h = chan_new(1, 11); sH = h;
    (void)open1(1, 11, 1, 7, COMP | AUTO); (void)arm1(1, 1, 7, h, 100, 0);
    T("LOOK: PENDING without sleeping", wait_for(7, 0, 0, 2000, 1, &w) == OK && w.result == NVRM_FENCE_R_PENDING && W.sleeps == 0 && w.ownerPid == 11 &&
      w.state == E_ARMED && w.blockers == 1 && Tb.stat[NVF_STAT_LOOK_PENDING] == 1 && Tb.stat[NVF_STAT_WAITS] == 0);
    T("a wait with no time: BUDGET without sleeping, the hold untouched", wait_for(7, 0, 0, 0, 0, &w) == OK && w.result == NVRM_FENCE_R_BUDGET &&
      W.sleeps == 0 && Tb.live == 1 && Tb.stat[NVF_STAT_WAIT_BUDGET] == 1);

    /* the interrupt arrives 1 ms into the sleep */
    Tb.c[IDX(h)].found = 8;                          /* a channel whose interrupts are known to arrive */
    w_reset(); W.during = s_gpu_done_kick;
    st = wait_for(7, 0, 0, 2000, 0, &w);
    T("woken by the interrupt: READY after 1 ms, one sleep", st == OK && w.result == NVRM_FENCE_R_READY && w.waitedUs == 1000 && W.sleeps == 1 &&
      Tb.stat[NVF_STAT_WAKE_IRQ] == 1 && Tb.stat[NVF_STAT_DONE_ON_IRQ] == 1 && Tb.stat[NVF_STAT_AUTO] == 1);
    T("the first slice on a trusted channel is 2 ms", W.dl[0] - W.at[0] == MS(2));
    T("it never slept holding the lock, and let go of it", W.sleptLocked == 0 && W.locked == 0);
    T("the wait took 1 to 10 ms: counted there", Tb.stat[NVF_STAT_WAIT_LT10MS] == 1 && Tb.stat[NVF_STAT_WAIT_MAX_US] == 1000);

    /* no interrupt: the slices find it, doubling */
    (void)open1(1, 11, 2, 7, COMP | AUTO); (void)arm1(1, 2, 7, h, 200, 0);
    w_reset(); W.during = s_gpu_done_quiet;
    st = wait_for(7, 0, 0, 100, 0, &w);
    T("no interrupt and no completion: BUDGET exactly at the caller's 100 ms", st == OK && w.result == NVRM_FENCE_R_BUDGET && w.waitedUs == 100000 && Tb.live == 1);
    T("the slices doubled: 2, 4, 8, 16, 32, 32 ms, the last cut at the budget", W.sleeps == 7 && W.dl[0] - W.at[0] == MS(2) && W.dl[1] - W.at[1] == MS(4) &&
      W.dl[2] - W.at[2] == MS(8) && W.dl[3] - W.at[3] == MS(16) && W.dl[4] - W.at[4] == MS(32) && W.dl[5] - W.at[5] == MS(32) && W.dl[6] - W.at[6] == MS(6));
    T("the wake reasons were counted", Tb.stat[NVF_STAT_WAKE_SLICE] == 7);
    {
        int late = 0;
        const unsigned long long t0 = W.at[0];
        for (int i = 0; i < W.sleeps && i < 64; i++) if (W.dl[i] > t0 + MS(100)) late++;
        T("no sleep was asked to last beyond the budget", late == 0);
    }
    (void)close1(1, 2, 7);

    /* a timeout larger than the maximum is clamped */
    (void)open1(1, 11, 3, 7, COMP | AUTO); (void)arm1(1, 3, 7, h, 300, 0);
    Tb.o[nvf_owner_find(&Tb, 1)].penaltyUntilNs = 0;
    w_reset(); W.during = NULL;
    st = wait_for(7, 0, 0, 100000, 0, &w);
    T("a hold that never completes ends the wait at its own 2000 ms bound: TIMEOUT", st == OK && w.result == NVRM_FENCE_R_TIMEOUT && w.waitedUs == 2000000);
    (void)close1(1, 3, 7);
    fresh(); w_reset();
    h = chan_new(1, 11); sH = h;
    (void)open1(1, 11, 1, 7, 0); (void)arm1(1, 1, 7, h, 100, 0);
    now += MS(1500);
    st = wait_for(7, 0, 0, 100000, 0, &w);
    T("a reader arriving 1500 ms into a hold waits the remaining 500 ms", st == OK && w.result == NVRM_FENCE_R_TIMEOUT && w.waitedUs == 500000);
    fresh(); w_reset();
    (void)open1(1, 11, 1, 7, 0); (void)open1(2, 22, 1, 7, 0); (void)open1(3, 33, 1, 7, 0);
    hw[IDX(chan_new(1, 11))].flushed = 1; hw[IDX(chan_new(2, 22))].flushed = 1; hw[IDX(chan_new(3, 33))].flushed = 1;
    now += MS(500);
    st = wait_for(7, 0, 0, 100000, 0, &w);
    T("three unarmed holds of writers whose GPUs are busy, 500 ms old: TIMEOUT after their remaining 1500 ms", st == OK &&
      w.result == NVRM_FENCE_R_TIMEOUT && w.waitedUs == 1500000 && w.blockers == 3);
    fresh(); w_reset();
    h = chan_new(1, 11); sH = h;
    hw[IDX(h)].flushed = 1;                          /* the writer's GPU has work in flight: its unarmed hold may last */
    (void)open1(1, 11, 1, 7, 0);
    sT0 = now; W.during = s_arm_late;                /* and 1500 ms later it is armed: 2000 ms more */
    st = wait_for(7, 0, 0, 100000, 0, &w);
    T("a hold that outlasts the reader: the timeout is clamped to 2000 ms: BUDGET, the hold untouched", st == OK &&
      w.result == NVRM_FENCE_R_BUDGET && w.waitedUs == 2000000 && hold_of(1, 1, 7) && hold_of(1, 1, 7)->state == E_ARMED &&
      !hold_of(1, 1, 7)->expired && Tb.live == 1);
    {
        struct NVRMFenceWait w2;
        fresh(); w_reset();
        (void)open1(1, 11, 1, 7, 0); hw[IDX(chan_new(1, 11))].flushed = 1;
        st = wait_for(7, 0, 0, 5000, 0, &w2);
        T("a hold's bound and the reader's budget end at the same instant: the hold is given up first: TIMEOUT", st == OK &&
          w2.waitedUs == 2000000 && w2.result == NVRM_FENCE_R_TIMEOUT);
    }

    /* interrupted, and refused for the number of sleepers */
    fresh(); w_reset();
    (void)open1(1, 11, 1, 7, 0);
    W.result = NVF_SLEEP_ABORTED;
    T("the sleeping thread is interrupted: ABORTED", wait_for(7, 0, 0, 2000, 0, &w) == OK && w.result == NVRM_FENCE_R_ABORTED && Tb.stat[NVF_STAT_WAIT_ABORTED] == 1 &&
      Tb.live == 1 && W.locked == 0);
    W.result = NVF_SLEEP_FULL;
    T("too many sleepers: FULL, with PENDING as the answer", wait_for(7, 0, 0, 2000, 0, &w) == NVRM_FENCE_ST_FULL && w.result == NVRM_FENCE_R_PENDING &&
      Tb.stat[NVF_STAT_WAIT_FULL] == 1 && W.locked == 0);
    W.result = NVF_SLEPT;

    /* the application publishes (CLOSE) while the reader sleeps */
    W.during = s_close;
    T("the writer's CLOSE wakes the reader: READY", wait_for(7, 0, 0, 2000, 0, &w) == OK && w.result == NVRM_FENCE_R_READY && w.waitedUs == 3000 &&
      Tb.stat[NVF_STAT_WAKE_STATE] == 1 && Tb.stat[NVF_STAT_DONE_ON_STATE] == 1);

    /* snapshot inside the loop */
    (void)open1(1, 11, 1, 7, 0);
    w_reset(); W.during = s_open_more;
    T("a hold opened while the reader sleeps is not waited for", wait_for(7, 0, 0, 2000, 0, &w) == OK && w.result == NVRM_FENCE_R_READY &&
      w.blockers == 1 && Tb.live == 1 && W.sleeps == 2);
    (void)close1(2, 9, 7);

    /* the generations are sampled before the look */
    fresh(); w_reset();
    h = chan_new(1, 11); sH = h;
    (void)open1(1, 11, 1, 7, COMP | AUTO); (void)arm1(1, 1, 7, h, 100, 0);
    W.inLock = s_kick_in_look;                       /* an interrupt lands after the reader sampled, while it looks */
    W.during = NULL;
    st = wait_for(7, 0, 0, 20, 0, &w);
    T("an interrupt that lands during the look is seen by the sleep (the generation was sampled before it)",
      W.sleeps >= 1 && W.kSeen[0] == 0 && W.kAtSleep[0] == 1);
    T("so that sleep does not block and the wake is counted as an interrupt", W.at[1] == W.at[0] && Tb.stat[NVF_STAT_WAKE_IRQ] == 1 && st == OK);
    (void)close1(1, 1, 7);

    T("every wait that found something is accounted for exactly once", Tb.stat[NVF_STAT_WAITS] == waits_ended());
    fresh(); w_reset();
    (void)open1(1, 11, 1, 7, COMP | AUTO); h = chan_new(1, 11); (void)arm1(1, 1, 7, h, 5, 0);
    hw[IDX(h)].done = 5;
    T("a hold that this very look completes: READY without a sleep, and it counts as a wait", wait_for(7, 0, 0, 2000, 0, &w) == OK &&
      w.result == NVRM_FENCE_R_READY && W.sleeps == 0 && Tb.stat[NVF_STAT_WAITS] == 1 && Tb.stat[NVF_STAT_WAIT_READY] == 1);
    (void)open1(1, 11, 2, 7, COMP | AUTO); (void)arm1(1, 2, 7, h, 6, 0);
    hw[IDX(h)].done = 6;
    T("the same for a LOOK: NONE (a LOOK answers NONE or PENDING only)", wait_for(7, 0, 0, 2000, 1, &w) == OK && w.result == NVRM_FENCE_R_NONE);
}

static void arm_wait_seq(void)
{
    struct NVRMFenceWait w;
    unsigned h, other, st;
    fresh(); w_reset();
    h = chan_new(1, 11); other = chan_new(2, 22); sH = h;
    hw[IDX(h)].done = 100;
    memset(&w, 0xAA, sizeof(w));
    T("a value already reached: READY at once", nvf_wait_seq(&Tb, &WOPS, NULL, 1, h, 100, 50, 1, &w) == OK && w.result == NVRM_FENCE_R_READY &&
      W.sleeps == 0 && w.waitedUs == 0 && Tb.stat[NVF_STAT_SEQ_WAITS] == 1);
    T("another connection's channel, an unknown handle, no connection: DEAD", nvf_wait_seq(&Tb, &WOPS, NULL, 1, other, 1, 50, 1, &w) == OK &&
      w.result == NVRM_FENCE_R_DEAD && nvf_wait_seq(&Tb, &WOPS, NULL, 1, 0x5005, 1, 50, 1, &w) == OK && w.result == NVRM_FENCE_R_DEAD &&
      nvf_wait_seq(&Tb, &WOPS, NULL, 0, h, 1, 50, 1, &w) == OK && w.result == NVRM_FENCE_R_DEAD && W.sleeps == 0);
    /* probation: fewer than 8 judged completions, so sleep in 250 us slices */
    hw[IDX(h)].done = 100;
    w_reset(); W.during = NULL;
    st = nvf_wait_seq(&Tb, &WOPS, NULL, 1, h, 101, 1, 1, &w);
    T("a channel still being judged: the kernel sleeps, in 250 us slices, up to the timeout: BUDGET", st == OK && w.result == NVRM_FENCE_R_BUDGET &&
      w.waitedUs == 1000 && W.sleeps == 4 && W.dl[0] - W.at[0] == 250000 && Tb.stat[NVF_STAT_SEQ_SLEPT] == 1);
    for (unsigned i = 0; i < 8; i++) {                /* the slices find 8 completions: no interrupt arrives for it */
        int fast = 0;
        (void)nvf_seq_eval(&Tb, 1, h, 101 + i, NVF_WHY_FIRST, hw_read, NULL, &fast);
        hw[IDX(h)].done = 101 + i;
        (void)nvf_seq_eval(&Tb, 1, h, 101 + i, NVF_WHY_SLICE, hw_read, NULL, &fast);
    }
    w_reset();
    T("a channel judged \"no interrupt\": PENDING, the kernel will not sleep", nvf_wait_seq(&Tb, &WOPS, NULL, 1, h, 500, 50, 1, &w) == OK &&
      w.result == NVRM_FENCE_R_PENDING && W.sleeps == 0 && Tb.stat[NVF_STAT_SEQ_REFUSED] == 1 && Tb.stat[NVF_STAT_CHAN_NOIRQ] == 1);
    /* a trusted channel */
    Tb.c[IDX(h)].found = 20; Tb.c[IDX(h)].foundSlice = 0;
    hw[IDX(h)].done = 0; Tb.c[IDX(h)].done = 0;
    w_reset(); W.during = s_gpu_done_kick;
    T("a trusted channel: the kernel sleeps and the interrupt wakes it: READY", nvf_wait_seq(&Tb, &WOPS, NULL, 1, h, 100, 50, 1, &w) == OK &&
      w.result == NVRM_FENCE_R_READY && w.waitedUs == 1000 && W.sleeps == 1 && W.dl[0] - W.at[0] == MS(2) && W.sleptLocked == 0 && W.locked == 0);
    w_reset(); W.during = NULL;
    T("its timeout is clamped to 100 ms: BUDGET", nvf_wait_seq(&Tb, &WOPS, NULL, 1, h, 900, 5000, 1, &w) == OK && w.result == NVRM_FENCE_R_BUDGET && w.waitedUs == 100000);
    w_reset(); W.during = s_fault;
    T("the channel faults during the sleep: DEAD", nvf_wait_seq(&Tb, &WOPS, NULL, 1, h, 900, 50, 1, &w) == OK && w.result == NVRM_FENCE_R_DEAD);
    hw[IDX(h)].faulted = 0; { unsigned idx; (void)nvf_chan_rebind(&Tb, 1, h, &idx); }
    w_reset(); W.result = NVF_SLEEP_ABORTED;
    T("interrupted: ABORTED", nvf_wait_seq(&Tb, &WOPS, NULL, 1, h, 900, 50, 1, &w) == OK && w.result == NVRM_FENCE_R_ABORTED);
    W.result = NVF_SLEEP_FULL;
    T("too many sleepers: FULL", nvf_wait_seq(&Tb, &WOPS, NULL, 1, h, 900, 50, 1, &w) == NVRM_FENCE_ST_FULL && w.result == NVRM_FENCE_R_PENDING);
    w_reset();
    Tb.paused = 1;
    T("while the GPU sleeps: DEAD, no sleep", nvf_wait_seq(&Tb, &WOPS, NULL, 1, h, 900, 50, 1, &w) == OK && w.result == NVRM_FENCE_R_DEAD && W.sleeps == 0);
    Tb.paused = 0; Tb.off = 1;
    T("when the layer is off: DEAD, no sleep", nvf_wait_seq(&Tb, &WOPS, NULL, 1, h, 900, 50, 1, &w) == OK && w.result == NVRM_FENCE_R_DEAD && W.sleeps == 0 && W.locked == 0);
}

static void arm_value_only_rises(void)
{
    struct NVRMFenceWait w;
    unsigned h;
    fresh(); w_reset();
    h = chan_new(1, 11);
    (void)open1(1, 11, 1, 7, 0); (void)arm1(1, 1, 7, h, 10, 0);
    hw[IDX(h)].done = 10;
    T("the GPU reaches 10: the hold is complete", look(7, 0, 0, 0, NULL) == 1 && hold_of(1, 1, 7)->state == E_DONE && Tb.c[IDX(h)].done == 10);
    hw[IDX(h)].done = 3;                             /* the page reads lower afterwards: rewritten, or gone and zero */
    (void)open1(1, 11, 2, 8, 0);
    T("a completion value the kernel has seen is never taken back: work armed at or below it is complete at once",
      arm1(1, 2, 8, h, 9, 0) == OK && hold_of(1, 2, 8)->state == E_DONE && Tb.c[IDX(h)].done == 10 && Tb.stat[NVF_STAT_ARM_DONE] == 1);
    T("and the producer's own wait for it is READY", nvf_wait_seq(&Tb, &WOPS, NULL, 1, h, 10, 50, 1, &w) == OK && w.result == NVRM_FENCE_R_READY && W.sleeps == 0);
    (void)open1(1, 11, 3, 9, 0);
    T("work beyond it still waits", arm1(1, 3, 9, h, 11, 0) == OK && hold_of(1, 3, 9)->state == E_ARMED && look(9, 0, 0, 0, NULL) == 1);
}

static void arm_log(void)
{
    struct nvf_event ev[NVF_EVENTS + 4];
    unsigned i, lines = 0, most = 0;
    fresh();
    for (i = 0; i < 600; i++) {                       /* a process that stalls once in a long while, for days */
        unsigned n;
        (void)open1(1, 11, 1 + i, 7, 0);
        now += MS(100);
        (void)look(7, 0, 0, 0, NULL);                /* given up: a strike, and the kernel is told */
        (void)close1(1, 1 + i, 7);
        n = nvf_events_take(&Tb, ev, NVF_EVENTS + 4);
        if (n > most) most = n;
        for (unsigned j = 0; j < n; j++) if (ev[j].kind == NVF_EV_STRIKE) lines++;
        now += MS(1300000);                          /* long enough for every strike to be forgiven */
    }
    T("600 stalls, each forgiven before the next: 600 strikes counted, nobody distrusted", Tb.stat[NVF_STAT_STRIKES] == 600 &&
      Tb.stat[NVF_STAT_OWNERS_DISTRUSTED] == 0 && Tb.evSeen[NVF_EV_STRIKE] == 600);
    T("but 10 log lines: the first 8, the 256th and the 512th", lines == 10 && most == 1);
    T("taking the events forgets them", Tb.evN == 0 && nvf_events_take(&Tb, ev, NVF_EVENTS) == 0);
    fresh();
    for (i = 0; i < NVF_EVENTS + 4; i++) (void)open1(1 + i, 100 + i, 1, 7, 0);
    now += MS(100);
    (void)look(7, 0, 0, 0, NULL);                    /* one look gives up twelve holds of twelve connections */
    T("no more than 8 are kept for one release of the lock", Tb.stat[NVF_STAT_STRIKES] == NVF_EVENTS + 4 && Tb.evN == NVF_EVENTS &&
      nvf_events_take(&Tb, ev, 3) == 3 && Tb.evN == 0);
}

static void arm_addresses(void)
{
    T("a semaphore page: page aligned, in user space", nvf_sem_va_ok(0x100000000ull) && nvf_sem_va_ok(0x1000) && nvf_sem_va_ok(0x00007FFFFFFFF000ull));
    T("refused: 0, not aligned, the kernel's half, wrapping", !nvf_sem_va_ok(0) && !nvf_sem_va_ok(0x100000800ull) && !nvf_sem_va_ok(0x100000001ull) &&
      !nvf_sem_va_ok(0x0000800000000000ull) && !nvf_sem_va_ok(0xFFFFFF8000001000ull) && !nvf_sem_va_ok(0xFFFFFFFFFFFFF000ull));
    T("a notifier: 16 bytes, 16-aligned, so never across a page boundary", nvf_err_va_ok(0x100000000ull, 0) && nvf_err_va_ok(0x100000FF0ull, 0xFFFF) && nvf_err_va_ok(0x100000010ull, 7));
    T("refused: not 16-aligned, in page zero, in the kernel's half", !nvf_err_va_ok(0x100000008ull, 0) && !nvf_err_va_ok(0x100000FF8ull, 0) &&
      !nvf_err_va_ok(0x10, 0) && !nvf_err_va_ok(0, 0) && !nvf_err_va_ok(0x0000800000000000ull, 0) && !nvf_err_va_ok(0xFFFFFFFFFFFFFFF0ull, 0));
    T("refused: a registration status that is not 16 bits", !nvf_err_va_ok(0x100000000ull, 0x10000));
    {
        /* where a wired page may be read: below installed memory + 4 GB. 32 GB of memory here: the limit is 36 GB. */
        const unsigned long long G = 1ull << 30, top = 36 * G;
        T("a page of system memory is read, up to the last whole page below the end of the physical map",
          nvf_phys_ok(0x1000, NVF_PAGE, top) && nvf_phys_ok(20 * G, NVF_PAGE, top) && nvf_phys_ok(top - NVF_PAGE, NVF_PAGE, top) &&
          nvf_phys_ok(0x7FFFF000ull, 2 * NVF_PAGE, top));
        T("refused: the GPU's memory window at 256 GB and up, a page that crosses the end, a page past it",
          !nvf_phys_ok(256 * G, NVF_PAGE, top) && !nvf_phys_ok(0x3FFA0000000ull, NVF_PAGE, top) &&
          !nvf_phys_ok(top, NVF_PAGE, top) && !nvf_phys_ok(top - 0x800, NVF_PAGE, top) && !nvf_phys_ok(top + NVF_PAGE, NVF_PAGE, top));
        T("refused: no address, an address not on a page boundary, a segment shorter than the page",
          !nvf_phys_ok(0, NVF_PAGE, top) && !nvf_phys_ok(20 * G + 8, NVF_PAGE, top) && !nvf_phys_ok(20 * G, NVF_PAGE - 1, top) &&
          !nvf_phys_ok(20 * G, 0, top));
        T("refused: every page while the end of the map is not known, and no wrap at the top of the address space",
          !nvf_phys_ok(0x1000, NVF_PAGE, 0) && !nvf_phys_ok(0xFFFFFFFFFFFFF000ull, NVF_PAGE, ~0ull - 0x800) &&
          !nvf_phys_ok(0x1000, NVF_PAGE, NVF_PAGE));
    }
}

static void arm_off(void)
{
    static struct nvf_table Z;                       /* never initialised: what the kext has before its first start */
    unsigned fi[4], one = 0, h = 0;
    T("a table that was never started refuses holds and channels", nvf_open(&Z, 1, 11, 1, 1, (unsigned[1]){ 7 }, NULL, 0, 5) == NVRM_FENCE_ST_OFF &&
      nvf_chan_add(&Z, 1, 11, 0, &h) == NVRM_FENCE_ST_OFF && h == 0);
    T("and everything else on it does nothing", nvf_owner_gone(&Z, 1, fi, 4) == 0 && nvf_chan_del(&Z, 1, 0x1000, 5, hw_read, NULL, &one) == NVRM_FENCE_ST_STALE &&
      nvf_chan_rebind(&Z, 1, 0x1000, &one) == NVRM_FENCE_ST_STALE && nvf_arm(&Z, 1, 1, 7, 0x1000, 1, 0, 5, hw_read, NULL) == NVRM_FENCE_ST_STALE);
    {
        struct nvf_query q = { 7, 0, 0, 0, 0, 0 };
        struct nvf_view v;
        int fast = 0;
        unsigned found = 9;
        nvf_end_all(&Z, NVF_END_SHUTDOWN);
        T("a look at it finds nothing and ends", nvf_eval(&Z, &q, 5, NVF_WHY_FIRST, hw_read, NULL, &v) == 0 &&
          nvf_seq_eval(&Z, 1, 0x1000, 1, NVF_WHY_FIRST, hw_read, NULL, &fast) == -1 &&
          nvf_close(&Z, 1, 1, 0, hw_read, NULL, &found) == OK && found == 0 && Z.live == 0 && Z.used == 0);
    }
    fresh();
    Tb.off = 1;
    T("a table switched off refuses holds and channels", open1(1, 11, 1, 7, 0) == NVRM_FENCE_ST_OFF && chan_new(1, 11) == 0 && Tb.used == 0);
}

/* ---- the same rules, written again the slow way, and 200,000 random operations on both --------------------------- */

#define MH 4096
static struct mhold { int used; unsigned long long owner, cookie, seq, since, serial; unsigned sid, pid, chan; int state, flags, planes, expired; } mh[MH];
static int mhN;
static struct mchan { int used; unsigned long long owner, done; unsigned handle; int verified, faulted, found, foundSlice, noirq; } mc[NVF_CHANS];
static struct mowner { int used; unsigned long long owner, penaltyUntil, lastStrike; unsigned pid; int strikes, distrusted, closed; } mo[64];
static struct mrec { unsigned long long serial; unsigned sid, pid; int why, flags, planes; } mring[NVF_RECENT];
static unsigned long long mserial, mrecent, m_allAt;   /* m_allAt, m_allWhy: the last time everything was ended at once */
static int m_allWhy;
static int m_live, m_liveC, m_used, m_flagged, m_off, m_paused, m_fused, m_distrusted, m_noirq;
static unsigned long long m_stat[NVF_STAT_N];
static int m_bug;                                    /* a control: 1 = an armed hold is given up 1 ms early, 2 = no snapshot,
                                                        3 = a compositor hold has the 20 ms penalty bound,
                                                        4 = distrust flags the owner's live holds,
                                                        5 = a process's exit ends only the closing connection,
                                                        6 = past the ring every answer is READY, also after everything
                                                            was ended at once */

static void m_reset(void) { memset(mh, 0, sizeof(mh)); mhN = 0; memset(mc, 0, sizeof(mc)); memset(mo, 0, sizeof(mo)); mserial = 1; mrecent = 0;
    m_allAt = 0; m_allWhy = 0;
    m_live = m_liveC = m_used = m_flagged = m_off = m_paused = m_fused = m_distrusted = m_noirq = 0; memset(m_stat, 0, sizeof(m_stat)); }
static struct mowner *mo_find(unsigned long long owner) { for (int i = 0; i < 64; i++) if (mo[i].used && mo[i].owner == owner) return &mo[i]; return NULL; }
static struct mowner *mo_get(unsigned long long owner, unsigned pid)
{
    struct mowner *o = mo_find(owner), *spare = NULL;
    int same = 0;
    if (o || !owner) return o;
    for (int i = 0; i < 64; i++) { if (!mo[i].used) { if (!spare) spare = &mo[i]; } else if (mo[i].pid == pid) same++; }
    if (!spare || same >= (int)NVF_PID_OWNERS) return NULL;
    memset(spare, 0, sizeof(*spare)); spare->used = 1; spare->owner = owner; spare->pid = pid;
    return spare;
}
static void m_rec(const struct mhold *h, int why)
{
    struct mrec *r = &mring[mrecent % NVF_RECENT];
    r->serial = h->serial; r->sid = h->sid; r->pid = h->pid; r->why = why; r->flags = h->flags; r->planes = h->planes;
    mrecent++;
}
static void m_drop(const struct mhold *h) { m_live--; if (h->flags & (int)COMP) m_liveC--; }
static void m_flag(struct mhold *h, int why) { if (!h->used || h->expired) return; h->expired = why; m_rec(h, why); m_drop(h); m_flagged++; }
static void m_end(struct mhold *h, int why)
{
    if (!h->used) return;
    if (h->expired) m_flagged--;
    else {
        m_rec(h, why); m_drop(h);
        if (why == NVF_END_GPU) m_stat[NVF_STAT_AUTO]++; else if (why == NVF_END_OWNER) m_stat[NVF_STAT_END_OWNER]++;
        else if (why == NVF_END_CHAN_ERR) m_stat[NVF_STAT_END_CHAN_ERR]++; else if (why == NVF_END_CHAN_DEL) m_stat[NVF_STAT_END_CHAN_DEL]++;
        else if (why == NVF_END_PM) m_stat[NVF_STAT_END_PM]++;
    }
    h->used = 0; m_used--;
}
static void m_end_all(int why) { for (int i = 0; i < mhN; i++) m_end(&mh[i], why); m_allAt = mrecent; m_allWhy = why; }
static struct mhold *m_find(unsigned long long owner, unsigned long long cookie, unsigned sid)
{
    for (int i = 0; i < mhN; i++) if (mh[i].used && mh[i].owner == owner && mh[i].cookie == cookie && mh[i].sid == sid) return &mh[i];
    return NULL;
}
static int m_entries(unsigned long long owner) { int n = 0; for (int i = 0; i < mhN; i++) if (mh[i].used && mh[i].owner == owner) n++; return n; }
static int m_chans(unsigned long long owner) { int n = 0; for (unsigned i = 0; i < NVF_CHANS; i++) if (mc[i].used && mc[i].owner == owner) n++; return n; }
static struct mchan *mc_find(unsigned long long owner, unsigned handle)
{
    const unsigned i = IDX(handle);
    if (!owner || !(handle >> 12) || i >= NVF_CHANS || !mc[i].used || mc[i].handle != handle || mc[i].owner != owner) return NULL;
    return &mc[i];
}
static void mc_refresh(struct mchan *c)
{
    const struct hwchan *x = &hw[c - mc];
    if (!x->mapped) { c->faulted = 1; return; }
    if (x->done > c->done) c->done = x->done;
    if (x->faulted) c->faulted = 1;
}
static int mc_trusted(const struct mchan *c) { return c->found >= (int)NVF_TRUST_MIN && c->foundSlice * 2 <= c->found; }
static void mc_judge(struct mchan *c, int why)
{
    int noirq;
    if (why != NVF_WHY_IRQ && why != NVF_WHY_SLICE) return;
    c->found++; if (why == NVF_WHY_SLICE) c->foundSlice++;
    if (c->found >= (int)NVF_TRUST_HALVE) { c->found /= 2; c->foundSlice /= 2; }
    noirq = c->found >= (int)NVF_TRUST_MIN && !mc_trusted(c);
    if (noirq != c->noirq) { c->noirq = noirq; m_noirq += noirq ? 1 : -1; }
}
static void mc_free(struct mchan *c) { if (c->noirq) m_noirq--; memset(c, 0, sizeof(*c)); }
static void m_complete(struct mhold *h)
{
    if (h->flags & (int)AUTO) { m_end(h, NVF_END_GPU); return; }
    h->state = (int)E_DONE;
    if (!(h->flags & (int)NVF_EF_REARMED)) h->since = now;
}
static int m_step(struct mhold *h, int why)
{
    struct mchan *c = mc_find(h->owner, h->chan);
    if (!c) { m_end(h, NVF_END_CHAN_DEL); return 0; }
    mc_refresh(c);
    if (c->done >= h->seq) { mc_judge(c, why); m_complete(h); return 0; }
    if (c->faulted) { m_end(h, NVF_END_CHAN_ERR); return 0; }
    return 1;
}
static int m_busy(unsigned long long owner)
{
    for (unsigned i = 0; i < NVF_CHANS; i++) {
        struct mchan *c = &mc[i];
        if (!c->used || c->owner != owner || !c->verified || c->faulted || !hw[i].mapped) continue;
        if (hw[i].done > c->done) c->done = hw[i].done;
        if (hw[i].faulted) { c->faulted = 1; continue; }
        if (hw[i].flushed > c->done) return 1;
    }
    return 0;
}
static int m_strike(struct mowner *o)
{
    if (o->strikes && now > o->lastStrike) {
        const unsigned long long f = (now - o->lastStrike) / MS(NVF_FORGIVE_MS);
        o->strikes = f >= (unsigned long long)o->strikes ? 0 : o->strikes - (int)f;
    }
    o->strikes++;
    o->lastStrike = now;
    o->penaltyUntil = now + MS(NVF_PENALTY_MS) * (1ull << (o->strikes - 1));
    m_stat[NVF_STAT_STRIKES]++;
    if (o->strikes < (int)NVF_STRIKES || o->distrusted) return 0;
    o->distrusted = 1; m_distrusted++; m_stat[NVF_STAT_OWNERS_DISTRUSTED]++;
    if (m_bug == 4) for (int i = 0; i < mhN; i++) if (mh[i].used && mh[i].owner == o->owner) m_flag(&mh[i], NVF_END_DISTRUST);
    if (m_distrusted < (int)NVF_FUSE_OWNERS) return 1;
    m_fused = 1; m_off = 1;
    return 2;
}
struct mq { unsigned sid, planes, pid, flags; unsigned long long snap, recent0; };
static int m_matches(const struct mq *q, unsigned sid, unsigned long long serial, int planes, unsigned pid, int flags)
{
    if (sid != q->sid) return 0;
    if (m_bug != 2 && serial > q->snap) return 0;
    if (q->planes && planes && !(q->planes & (unsigned)planes)) return 0;
    if (q->pid && q->pid != pid) return 0;
    if ((q->flags & COMP) && !(flags & (int)COMP)) return 0;
    return 1;
}
static unsigned m_eval(struct mq *q, int why, struct nvf_view *v)
{
    int again;
    if (!q->snap) { q->snap = mserial; q->recent0 = mrecent; }
    do {                                                        /* again: only for the control m_bug 4 */
        again = 0;
        memset(v, 0, sizeof(*v)); v->deadlineNs = ~0ull;
        for (int i = mhN - 1; i >= 0 && !again; i--) {         /* newest first, as a bucket chain is */
            struct mhold *h = &mh[i];
            struct mowner *o;
            unsigned long long age, bound;
            int kind;
            if (!h->used || h->expired || !m_matches(q, h->sid, h->serial, h->planes, h->pid, h->flags)) continue;
            if (h->state == (int)E_ARMED && !m_step(h, why) && !h->used) continue;
            o = mo_find(h->owner);
            age = now > h->since ? now - h->since : 0;
            if (now < o->penaltyUntil) {                /* a compositor hold's penalty bound is the CPU bound (m_bug 3: as it was) */
                kind = NVF_BOUND_PENALTY; bound = MS((h->flags & (int)COMP) && m_bug != 3 ? NVF_CAP_CPU_MS : NVF_CAP_PEN_MS); }
            else if (h->state == (int)E_ARMED) { kind = NVF_BOUND_GPU; bound = MS(NVF_CAP_GPU_MS) - (m_bug == 1 ? MS(1) : 0); }
            else if (age >= MS(NVF_CAP_CPU_MS) && m_busy(h->owner)) { kind = NVF_BOUND_GPU; bound = MS(NVF_CAP_GPU_MS); }
            else { kind = NVF_BOUND_CPU; bound = MS(NVF_CAP_CPU_MS); }
            if (age >= bound) {
                int struck;
                m_flag(h, NVF_END_EXPIRED);
                if (kind == NVF_BOUND_PENALTY) { m_stat[NVF_STAT_EXPIRED_PENALTY]++; continue; }
                m_stat[kind == NVF_BOUND_GPU ? NVF_STAT_EXPIRED_GPU : NVF_STAT_EXPIRED_CPU]++;
                struck = m_strike(o);
                if (struck == 2) {
                    m_end_all(NVF_END_DISTRUST);
                    memset(v, 0, sizeof(*v)); v->deadlineNs = ~0ull;
                    return 0;
                }
                if (struck == 1 && m_bug == 4) again = 1;
                continue;
            }
            if (!v->blockers) { v->firstPid = h->pid; v->firstState = (unsigned)h->state; }
            v->blockers++;
            if (h->since + bound < v->deadlineNs) v->deadlineNs = h->since + bound;
            if (h->state == (int)E_ARMED) { const struct mchan *c = mc_find(h->owner, h->chan); if (c && !mc_trusted(c)) v->fast = 1; }
        }
    } while (again);
    return v->blockers;
}
static unsigned m_result(const struct mq *q, int saw)
{
    unsigned res = saw ? NVRM_FENCE_R_READY : NVRM_FENCE_R_NONE;
    if (mrecent - q->recent0 > NVF_RECENT) {            /* the ring has forgotten; an ending of everything since then is known */
        if (m_bug != 6 && m_allWhy && m_allAt > q->recent0) return m_allWhy == NVF_END_DISTRUST ? NVRM_FENCE_R_TIMEOUT : NVRM_FENCE_R_DEAD;
        return NVRM_FENCE_R_READY;
    }
    for (unsigned long long s = q->recent0; s != mrecent; s++) {
        const struct mrec *r = &mring[s % NVF_RECENT];
        unsigned one;
        if (!m_matches(q, r->sid, r->serial, r->planes, r->pid, r->flags)) continue;
        one = (r->why == NVF_END_CLOSE || r->why == NVF_END_GPU) ? NVRM_FENCE_R_READY :
              (r->why == NVF_END_EXPIRED || r->why == NVF_END_DISTRUST) ? NVRM_FENCE_R_TIMEOUT : NVRM_FENCE_R_DEAD;
        if (one > res) res = one;
    }
    return res;
}
static struct mhold *m_new(void)
{
    if (mhN == MH || mhN > 2 * m_used + 64) { int k = 0; for (int i = 0; i < mhN; i++) if (mh[i].used) mh[k++] = mh[i]; mhN = k; }   /* keeps their order */
    memset(&mh[mhN], 0, sizeof(mh[0]));
    return &mh[mhN++];
}
static int m_stale(const struct mhold *h) { return !h->expired && now >= h->since && now - h->since > MS(NVF_CAP_GPU_MS); }

static unsigned long long rs = 0x9E3779B97F4A7C15ull;
static unsigned rnd(unsigned n) { rs ^= rs << 13; rs ^= rs >> 7; rs ^= rs << 17; return (unsigned)((rs >> 11) % n); }

/* Three kinds of day. BUSY: few connections drawing into few surfaces while the clock moves in small steps, so most
   holds live their whole life (opened, armed, completed by the GPU, closed). CROWDED: many connections that seldom
   close and a clock that jumps, so holds are given up, connections are struck and distrusted and the layer switches
   itself off. LEAKY: connections that open and almost never close, on so many surfaces that readers seldom meet a
   hold, so the table and the per-connection quota fill and slots have to be reclaimed. The numbers are how often each
   operation is drawn, in 1/1000; pmOneIn thins the last one (sleep) further. */
enum { O_OPEN, O_ARM, O_CLOSE, O_LOOK, O_GPU, O_TIME, O_CHAN_ADD, O_CHAN_DEL, O_GONE, O_FAULT, O_SEQ, O_PM, O_N };
struct profile { unsigned owners, cookies, sids, jumpy, pmOneIn; unsigned short mix[O_N]; };
static const struct profile BUSY    = {  6,  4,   6, 0,    2, { 160, 240, 100, 220, 181,  50, 25, 4, 3,  8,  8, 1 } };
static const struct profile CROWDED = { 14, 16,  10, 1,   10, { 330, 120,  60, 250,  60, 110, 40, 4, 4, 10, 10, 2 } };
static const struct profile LEAKY   = { 14, 16, 100, 1, 1000, { 600,  60,  20,  30,  20, 250, 10, 2, 1,  2,  4, 1 } };

static unsigned long long cov[NVF_STAT_N];          /* what a run exercised: the table's counters, summed over its boots */
static unsigned covFuses, covEdges;

/* Returns 0 when table and model agreed on all `ops` operations, else the number of the first operation they did not. */
static unsigned long differential(const struct profile *P, unsigned long ops, unsigned long long seed, const char **what)
{
#define NOWN 14
#define NRD 6
    static struct { unsigned long long id; unsigned pid; unsigned h[3]; } own[NOWN];
    static struct { int on, saw; struct nvf_query q; struct mq m; } rd[NRD];
    unsigned long long nextOwner = 1;
    unsigned edgeSid = 0;                             /* a surface whose hold the clock was just moved to the edge of */
    unsigned warm = 6 * P->owners;                    /* a boot begins with the connections registering their channels */
    unsigned long n;
#define DIFF(c, s) do { if (!(c)) { *what = s; return n ? n : 1; } } while (0)
    rs = seed; *what = "";
    memset(cov, 0, sizeof(cov)); covFuses = covEdges = 0;
    fresh(); m_reset(); memset(rd, 0, sizeof(rd));
    for (unsigned i = 0; i < NOWN; i++) { own[i].id = nextOwner++; own[i].pid = 100 + i % 3; memset(own[i].h, 0, sizeof(own[i].h)); }
    for (n = 1; n <= ops; n++) {
        unsigned oi = rnd(P->owners);
        unsigned long long owner = own[oi].id;
        const unsigned pid = own[oi].pid;
        unsigned op = 0, draw = rnd(1000);
        while (op < O_N - 1 && draw >= P->mix[op]) draw -= P->mix[op++];
        if (warm) { warm--; op = O_CHAN_ADD; }
        if (edgeSid) op = O_LOOK;
        if (op == O_OPEN) {
            unsigned ids[8], cnt = 1 + rnd(3), need = 0, st, want;
            const unsigned flags = rnd(4) == 0 ? COMP | AUTO : rnd(8) == 0 ? COMP : rnd(60) == 0 ? AUTO : 0;
            unsigned char pl[8];
            const unsigned long long cookie = 1 + rnd(P->cookies);
            struct mowner *o;
            for (unsigned i = 0; i < cnt; i++) { ids[i] = rnd(40) == 0 ? 0 : 1 + rnd(P->sids); pl[i] = (unsigned char)rnd(4); }
            st = nvf_open(&Tb, owner, pid, cookie, cnt, ids, pl, flags, now);
            if (m_off || m_paused) want = NVRM_FENCE_ST_OFF;
            else {
                int bad = (flags & AUTO) && !(flags & COMP);
                for (unsigned i = 0; i < cnt; i++) if (!ids[i]) bad = 1;
                if (bad) want = NVRM_FENCE_ST_BAD_ARG;
                else if (!(o = mo_get(owner, pid))) want = NVRM_FENCE_ST_FULL;
                else if (o->closed) want = NVRM_FENCE_ST_OFF;
                else if (o->distrusted) want = NVRM_FENCE_ST_DISTRUST;
                else {
                    int mine, ownLeft = 0, anyLeft = 0;
                    for (unsigned i = 0; i < cnt; i++) { unsigned j; for (j = 0; j < i && ids[j] != ids[i]; j++) { } if (j == i && !m_find(owner, cookie, ids[i])) need++; }
                    /* which holds made room is the table's choice: take from the model what the table no longer has,
                       and require that each was one that delays nobody and not of this command buffer */
                    for (int i = 0; i < mhN; i++) {
                        struct mhold *h = &mh[i];
                        const int toi = h->used ? nvf_owner_find(&Tb, h->owner) : -1;
                        if (!h->used || (toi >= 0 && nvf_find(&Tb, (unsigned)toi, h->cookie, h->sid) >= 0)) continue;
                        DIFF((h->expired || m_stale(h)) && !(h->owner == owner && h->cookie == cookie), "OPEN evicted a hold that still delays a reader");
                        m_end(h, NVF_END_EXPIRED); m_stat[NVF_STAT_RECLAIMED]++;
                    }
                    mine = m_entries(owner);
                    for (int i = 0; i < mhN; i++) if (mh[i].used && (mh[i].expired || m_stale(&mh[i])) && !(mh[i].owner == owner && mh[i].cookie == cookie)) {
                        anyLeft++; if (mh[i].owner == owner) ownLeft++; }
                    if (mine + (int)need > (int)NVF_OWNER_ENTRIES) { want = NVRM_FENCE_ST_FULL; DIFF(ownLeft == 0, "OPEN refused a connection that had reclaimable holds"); }
                    else if (m_used + (int)need > (int)NVF_ENTRIES) { want = NVRM_FENCE_ST_FULL; DIFF(anyLeft == 0, "OPEN refused while reclaimable holds were left"); }
                    else {
                        want = OK;
                        mserial++;
                        for (unsigned i = 0; i < cnt; i++) {
                            struct mhold *h = m_find(owner, cookie, ids[i]);
                            if (h) { h->planes = (!h->planes || !pl[i]) ? 0 : (h->planes | pl[i]); continue; }
                            h = m_new();
                            h->used = 1; h->owner = owner; h->cookie = cookie; h->since = now; h->serial = mserial; h->sid = ids[i]; h->pid = pid;
                            h->state = (int)E_OPEN; h->flags = (int)(flags & (COMP | AUTO)); h->planes = pl[i];
                            m_used++; m_live++; if (flags & COMP) m_liveC++; m_stat[NVF_STAT_OPENS]++;
                        }
                    }
                }
            }
            DIFF(st == want, "OPEN answered differently");
        } else if (op == O_ARM) {
            unsigned long long cookie = 1 + rnd(P->cookies), seq;
            unsigned sid = 1 + rnd(P->sids), handle, st, want = OK;
            const unsigned flags = rnd(10) == 0 ? NVRM_FENCE_F_ARM_DONE : 0;
            struct mhold *h = mhN ? &mh[rnd((unsigned)mhN)] : NULL;
            if (h && h->used && rnd(4)) {                 /* three times in four: a hold that exists, by its own connection */
                unsigned k;
                for (k = 0; k < P->owners && own[k].id != h->owner; k++) { }
                if (k < P->owners) { oi = k; owner = own[k].id; cookie = h->cookie; sid = h->sid; }
            }
            handle = rnd(12) == 0 ? own[rnd(P->owners)].h[rnd(3)] : own[oi].h[rnd(3)];
            seq = hw[IDX(handle)].done + rnd(6);
            st = nvf_arm(&Tb, owner, cookie, sid, handle, seq, flags, now, hw_read, NULL);
            h = m_find(owner, cookie, sid);
            if (!h) want = NVRM_FENCE_ST_STALE;
            else if (!h->expired) {
                if (h->state == (int)E_DONE) h->flags |= (int)NVF_EF_REARMED;
                if (flags & NVRM_FENCE_F_ARM_DONE) m_complete(h);
                else {
                    struct mchan *c = mc_find(owner, handle);
                    if (!c || !c->verified) want = NVRM_FENCE_ST_STALE;
                    else {
                        if (h->state == (int)E_ARMED && h->chan == handle) { if (seq > h->seq) h->seq = seq; }
                        else { if (h->state == (int)E_OPEN) h->since = now; h->chan = handle; h->seq = seq; h->state = (int)E_ARMED; }
                        (void)m_step(h, NVF_WHY_FIRST);
                    }
                }
            }
            DIFF(st == want, "ARM answered differently");
        } else if (op == O_CLOSE) {
            const unsigned long long cookie = 1 + rnd(P->cookies);
            const unsigned sid = rnd(4) == 0 ? 0 : 1 + rnd(P->sids);
            unsigned found = 0, want = 0;
            (void)nvf_close(&Tb, owner, cookie, sid, hw_read, NULL, &found);
            for (int i = 0; i < mhN; i++) {
                struct mhold *h = &mh[i];
                if (!h->used || h->owner != owner || h->cookie != cookie || (sid && h->sid != sid)) continue;
                if (h->state == (int)E_ARMED && !h->expired) { struct mchan *c = mc_find(owner, h->chan); if (c) mc_refresh(c); }
                m_end(h, NVF_END_CLOSE); want++;
            }
            DIFF(found == want, "CLOSE found a different number of holds");
        } else if (op == O_LOOK) {
            const unsigned r = rnd(NRD);
            int why;
            struct nvf_view v, mv;
            unsigned a, b;
            if (edgeSid) rd[r].on = 0;                    /* a reader arrives at exactly that instant */
            why = rd[r].on ? 1 + (int)rnd(3) : NVF_WHY_FIRST;
            if (!rd[r].on) {
                const unsigned sid = edgeSid ? edgeSid : 1 + rnd(P->sids), planes = (edgeSid || rnd(3)) ? 0 : 1 + rnd(3);
                const unsigned qpid = (edgeSid || rnd(4)) ? 0 : 100 + rnd(3), flags = (edgeSid || rnd(4)) ? 0 : COMP;
                rd[r].on = 1; rd[r].saw = 0;
                rd[r].q = (struct nvf_query){ sid, planes, qpid, flags, 0, 0 };
                rd[r].m = (struct mq){ sid, planes, qpid, flags, 0, 0 };
                edgeSid = 0;
            }
            a = nvf_eval(&Tb, &rd[r].q, now, why, hw_read, NULL, &v);
            b = m_eval(&rd[r].m, why, &mv);
            DIFF(a == b, "a reader has a different number of holds in front of it");
            DIFF(v.blockers == mv.blockers && v.firstPid == mv.firstPid && v.firstState == mv.firstState && v.deadlineNs == mv.deadlineNs && v.fast == mv.fast,
                 "a reader is told a different writer, state, deadline or slice");
            DIFF(nvf_result_from_recent(&Tb, &rd[r].q, rd[r].saw) == m_result(&rd[r].m, rd[r].saw), "a reader is given a different result");
            if (a) rd[r].saw = 1;
            if (!a || rnd(30) == 0) rd[r].on = 0;
        } else if (op == O_GPU) {                         /* the GPU works */
            const unsigned h = own[rnd(P->owners)].h[rnd(3)];
            if (h) { hw[IDX(h)].done += 1 + rnd(6); hw[IDX(h)].flushed = hw[IDX(h)].done + (rnd(3) ? 0 : 1 + rnd(3)); }
        } else if (op == O_TIME) {                        /* time passes */
            const unsigned k = rnd(1000);
            if (k < (P->jumpy ? 150u : 30u) && mhN) {     /* ...up to the edge of a hold's bound: its last nanosecond, or the bound itself */
                for (int tries = 0; tries < 16 && !edgeSid; tries++) {
                    const struct mhold *h = &mh[rnd((unsigned)mhN)];
                    const struct mowner *o = h->used ? mo_find(h->owner) : NULL;
                    const unsigned edge = (o && now < o->penaltyUntil) ? ((h->flags & (int)COMP) ? NVF_CAP_CPU_MS : NVF_CAP_PEN_MS) :
                                          h->state == (int)E_ARMED ? NVF_CAP_GPU_MS :
                                          rnd(4) ? NVF_CAP_CPU_MS : NVF_CAP_GPU_MS;
                    const unsigned long long at = h->since + MS(edge) - rnd(2);
                    if (h->used && !h->expired && at > now) { now = at; edgeSid = h->sid; covEdges++; }
                }
            } else if (!P->jumpy) now += k < 950 ? rnd(100000) : k < 995 ? MS(1 + rnd(5)) : k < 999 ? MS(100 + rnd(300)) : MS(1500 + rnd(1100));
            else now += k < 600 ? rnd(2000000) : k < 900 ? MS(5 + rnd(55)) : k < 980 ? MS(100 + rnd(300)) : k < 999 ? MS(1500 + rnd(1100)) : MS(660000);
        } else if (op == O_CHAN_ADD) {                    /* CHAN_ADD (+ CHAN_VERIFY) */
            const unsigned slot = rnd(3), flags = rnd(4) ? 0 : NVRM_FENCE_F_CHAN_CE, verify = rnd(8);
            unsigned h = 0, fi = 0, st, want;
            struct mowner *o;
            if (own[oi].h[slot]) continue;
            st = nvf_chan_add(&Tb, owner, pid, flags, &h);
            want = m_off ? NVRM_FENCE_ST_OFF : !(o = mo_get(owner, pid)) ? NVRM_FENCE_ST_FULL : o->closed ? NVRM_FENCE_ST_OFF :
                   m_chans(owner) >= (int)NVF_OWNER_CHANS ? NVRM_FENCE_ST_FULL : OK;
            DIFF(st == want && (st == OK) == (h != 0), "CHAN_ADD answered differently");
            if (st != OK) continue;
            DIFF(!mc[IDX(h)].used, "CHAN_ADD gave out a channel slot that is in use");
            memset(&hw[IDX(h)], 0, sizeof(hw[0])); hw[IDX(h)].mapped = 1;
            mc[IDX(h)] = (struct mchan){ 1, owner, 0, h, 0, 0, 0, 0, 0 };
            if (verify == 0) {                            /* the view did not follow: the channel is dropped */
                DIFF(nvf_chan_verify(&Tb, owner, h, 0, &fi) == NVRM_FENCE_ST_MAP && fi == IDX(h), "a failed CHAN_VERIFY did not hand the channel back");
                mc_free(&mc[IDX(h)]);
            } else if (verify != 1) {                     /* 1: left unverified */
                DIFF(nvf_chan_verify(&Tb, owner, h, 1, &fi) == OK && fi == NVF_NIL, "CHAN_VERIFY refused");
                mc[IDX(h)].verified = 1;
                own[oi].h[slot] = h;
            } else own[oi].h[slot] = h;
        } else if (op == O_CHAN_DEL) {
            const unsigned slot = rnd(3), h = own[oi].h[slot];
            unsigned fi = 0, st;
            struct mchan *c = mc_find(owner, h);
            st = nvf_chan_del(&Tb, owner, h, now, hw_read, NULL, &fi);
            DIFF((st == OK) == (c != NULL) && (c ? fi == IDX(h) : st == NVRM_FENCE_ST_STALE), "CHAN_DEL answered differently");
            if (c) {
                mc_refresh(c);
                for (int i = 0; i < mhN; i++) {
                    struct mhold *x = &mh[i];
                    if (!x->used || x->owner != owner || x->state != (int)E_ARMED || x->expired || x->chan != h) continue;
                    if (c->done >= x->seq) m_complete(x); else m_end(x, NVF_END_CHAN_DEL);
                }
                mc_free(c);
            }
            own[oi].h[slot] = 0;
        } else if (op == O_GONE) {                        /* the connection closes; a new one takes its place */
            unsigned fi[NVF_OWNER_CHANS], got, want;
            if (rnd(3) == 0) {                            /* ...because its process exits: every connection of it ends first */
                unsigned b;
                want = 0; got = 0;
                for (int i = 0; i < 64; i++) if (mo[i].used && mo[i].pid == pid) want += (unsigned)m_chans(mo[i].owner);
                do { b = nvf_pid_gone(&Tb, pid, fi, NVF_OWNER_CHANS); got += b; } while (b == NVF_OWNER_CHANS);
                DIFF(got == want || m_bug == 5, "an exiting process handed back a different number of channels");
                for (int i = 0; i < 64 && m_bug != 5; i++) {
                    struct mowner *o = &mo[i];
                    if (!o->used || o->pid != pid) continue;
                    if (!o->closed) { o->closed = 1; m_stat[NVF_STAT_PID_GONE]++; }
                    for (int k = 0; k < mhN; k++) if (mh[k].used && mh[k].owner == o->owner) m_end(&mh[k], NVF_END_OWNER);
                    for (unsigned k = 0; k < NVF_CHANS; k++) if (mc[k].used && mc[k].owner == o->owner) mc_free(&mc[k]);
                }
                for (unsigned k = 0; k < P->owners; k++) if (own[k].pid == pid && m_bug != 5) memset(own[k].h, 0, sizeof(own[k].h));
            }
            want = (unsigned)m_chans(owner);
            got = nvf_owner_gone(&Tb, owner, fi, NVF_OWNER_CHANS);
            DIFF(got == want, "a closing connection handed back a different number of channels");
            for (int i = 0; i < mhN; i++) if (mh[i].used && mh[i].owner == owner) m_end(&mh[i], NVF_END_OWNER);
            for (unsigned i = 0; i < NVF_CHANS; i++) if (mc[i].used && mc[i].owner == owner) mc_free(&mc[i]);
            if (mo_find(owner)) mo_find(owner)->used = 0;
            own[oi].id = nextOwner++; memset(own[oi].h, 0, sizeof(own[oi].h));
        } else if (op == O_FAULT) {                       /* a channel faults, or is rebuilt */
            const unsigned h = own[oi].h[rnd(3)];
            if (!h) continue;
            if (rnd(8) == 0) hw[IDX(h)].faulted = 1;
            else {
                unsigned idx = 0;
                const unsigned st = nvf_chan_rebind(&Tb, owner, h, &idx);
                struct mchan *c = mc_find(owner, h);
                const unsigned want = m_off ? NVRM_FENCE_ST_OFF : (!c || !c->verified) ? NVRM_FENCE_ST_STALE : OK;
                DIFF(st == want, "CHAN_REBIND answered differently");
                if (st == OK) { DIFF(idx == IDX(h), "CHAN_REBIND named another channel"); c->faulted = 0; hw[IDX(h)].faulted = 0; }
            }
        } else if (op == O_SEQ) {                         /* WAIT_SEQ's look */
            const unsigned h = own[oi].h[rnd(3)];
            const unsigned long long seq = hw[IDX(h)].done + rnd(3);
            const int why = (int)rnd(4);
            int fast = 0, mfast = 0, a, b;
            struct mchan *c = mc_find(owner, h);
            a = nvf_seq_eval(&Tb, owner, h, seq, why, hw_read, NULL, &fast);
            if (m_off || m_paused || !c || !c->verified) b = -1;
            else {
                mc_refresh(c);
                if (c->done >= seq) { mc_judge(c, why); b = 1; }
                else if (c->faulted) b = -1;
                else if (c->found < (int)NVF_TRUST_MIN) { mfast = 1; b = 0; }
                else b = mc_trusted(c) ? 0 : -2;
            }
            DIFF(a == b && fast == mfast, "WAIT_SEQ's look answered differently");
        } else if (m_paused || rnd(P->pmOneIn) == 0) {    /* the GPU sleeps and wakes */
            if (!m_paused) { Tb.paused = 1; nvf_end_all(&Tb, NVF_END_PM); m_paused = 1; m_end_all(NVF_END_PM); }
            else { Tb.paused = 0; m_paused = 0; }
        }
        DIFF(Tb.live == (unsigned)m_live && Tb.liveCompositor == (unsigned)m_liveC, "the count of holds that delay readers differs");
        DIFF(Tb.used == (unsigned)m_used && Tb.flagged == (unsigned)m_flagged, "the count of slots in use differs");
        DIFF(Tb.fused == (unsigned)m_fused && Tb.distrustedOwners == (unsigned)m_distrusted && Tb.off == (unsigned)m_off, "distrust or the fuse differs");
        Tb.evN = 0;
        if (n % 1000 == 0 || m_fused || n == ops) {
            static const int cmp[] = { NVF_STAT_OPENS, NVF_STAT_AUTO, NVF_STAT_END_OWNER, NVF_STAT_END_CHAN_ERR, NVF_STAT_END_CHAN_DEL, NVF_STAT_END_PM, NVF_STAT_PID_GONE,
                                       NVF_STAT_EXPIRED_GPU, NVF_STAT_EXPIRED_CPU, NVF_STAT_EXPIRED_PENALTY, NVF_STAT_STRIKES,
                                       NVF_STAT_OWNERS_DISTRUSTED, NVF_STAT_RECLAIMED };
            const char *bad = structure(&Tb);
            DIFF(bad == NULL, bad);
            DIFF(Tb.stat[NVF_STAT_CHAN_NOIRQ] == (unsigned long long)m_noirq, "the count of channels judged \"no interrupt\" differs");
            for (unsigned i = 0; i < sizeof(cmp) / sizeof(cmp[0]); i++) DIFF(Tb.stat[cmp[i]] == m_stat[cmp[i]], "a counter differs");
            for (unsigned i = 0; i < NVF_CHANS; i++) {
                DIFF(Tb.c[i].used == mc[i].used, "a channel slot differs");
                if (mc[i].used) DIFF(Tb.c[i].done == mc[i].done && Tb.c[i].faulted == mc[i].faulted && Tb.c[i].verified == mc[i].verified &&
                                     Tb.c[i].found == mc[i].found && Tb.c[i].foundSlice == mc[i].foundSlice, "a channel's state differs");
            }
        }
        if (m_fused || n == ops) { for (unsigned i = 0; i < NVF_STAT_N; i++) cov[i] += Tb.stat[i]; covFuses += (unsigned)m_fused; }
        if (m_fused) {                                    /* the layer switched itself off: start a new boot */
            const unsigned long long keep = now;
            fresh(); m_reset(); now = keep; memset(rd, 0, sizeof(rd)); edgeSid = 0; warm = 6 * P->owners;
            for (unsigned i = 0; i < NOWN; i++) { own[i].id = nextOwner++; memset(own[i].h, 0, sizeof(own[i].h)); }
        }
    }
    return 0;
#undef DIFF
}

static void cov_print(void)
{
    printf("     that run: %llu holds opened; %llu ended by the GPU, %llu became GPU-done or were closed, %llu were given up\n"
           "     (GPU bound %llu, CPU bound %llu, penalty bound %llu, %u of them looked at on the very edge of the bound); %llu strikes,\n"
           "     %llu connections distrusted, %u fuses, %llu slots reclaimed; ended by a fault %llu, a deleted channel %llu,\n"
           "     a closed connection %llu (%llu connections emptied at their process's exit), sleep %llu; %llu channels, %llu rebinds;\n"
           "     %llu requests refused for room\n",
           cov[NVF_STAT_OPENS], cov[NVF_STAT_AUTO], cov[NVF_STAT_CLOSES],
           cov[NVF_STAT_EXPIRED_GPU] + cov[NVF_STAT_EXPIRED_CPU] + cov[NVF_STAT_EXPIRED_PENALTY],
           cov[NVF_STAT_EXPIRED_GPU], cov[NVF_STAT_EXPIRED_CPU], cov[NVF_STAT_EXPIRED_PENALTY], covEdges, cov[NVF_STAT_STRIKES],
           cov[NVF_STAT_OWNERS_DISTRUSTED], covFuses, cov[NVF_STAT_RECLAIMED], cov[NVF_STAT_END_CHAN_ERR], cov[NVF_STAT_END_CHAN_DEL],
           cov[NVF_STAT_END_OWNER], cov[NVF_STAT_PID_GONE], cov[NVF_STAT_END_PM], cov[NVF_STAT_CHAN_ADDS], cov[NVF_STAT_CHAN_REBINDS], cov[NVF_STAT_REFUSED_FULL]);
}

static void arm_differential(void)
{
    const char *what = "";
    unsigned long at;
    m_bug = 0;
    at = differential(&BUSY, 200000, 0x9E3779B97F4A7C15ull, &what);
    if (at) printf("     operation %lu: %s\n", at, what);
    T("200,000 random operations of a busy desktop: the table and a naive second implementation agree at every step", at == 0);
    cov_print();
    T("and that run was the ordinary life of holds: most closed by their writers, hundreds ended by the GPU, by faults, by sleep, by exits", at == 0 &&
      cov[NVF_STAT_CLOSES] * 2 > cov[NVF_STAT_OPENS] && cov[NVF_STAT_AUTO] > 500 && cov[NVF_STAT_END_CHAN_ERR] > 100 &&
      cov[NVF_STAT_END_CHAN_DEL] > 20 && cov[NVF_STAT_CHAN_REBINDS] > 100 && cov[NVF_STAT_END_PM] > 100 && cov[NVF_STAT_END_OWNER] > 100 &&
      cov[NVF_STAT_PID_GONE] > 100);
    at = differential(&CROWDED, 200000, 0xD1B54A32D192ED03ull, &what);
    if (at) printf("     operation %lu: %s\n", at, what);
    T("200,000 more of a crowded, misbehaving one: they agree", at == 0);
    cov_print();
    T("and that run was the hard one: holds given up on every bound, strikes, distrust, the fuse", at == 0 &&
      cov[NVF_STAT_EXPIRED_GPU] > 100 && cov[NVF_STAT_EXPIRED_CPU] > 500 && cov[NVF_STAT_EXPIRED_PENALTY] > 5000 && covEdges > 100 &&
      cov[NVF_STAT_STRIKES] > 500 && cov[NVF_STAT_OWNERS_DISTRUSTED] >= 3 * NVF_FUSE_OWNERS && covFuses >= 3);
    at = differential(&LEAKY, 60000, 0xA0761D6478BD642Full, &what);
    if (at) printf("     operation %lu: %s\n", at, what);
    T("60,000 of connections that leak their holds: they agree", at == 0);
    cov_print();
    T("and that run filled the table: slots reclaimed by the thousand, requests refused when nothing could be", at == 0 &&
      cov[NVF_STAT_RECLAIMED] > 1000 && cov[NVF_STAT_REFUSED_FULL] > 100);
}

/* ---- controls: each check above can fail ------------------------------------------------------------------------- */

static void arm_controls(void)
{
    const char *what = "";
    unsigned ei;
    m_bug = 1;
    T("control: a second implementation that gives an armed hold up 1 ms early is caught", differential(&CROWDED, 200000, 0xD1B54A32D192ED03ull, &what) != 0);
    m_bug = 2;
    T("control: one that forgets the snapshot rule is caught", differential(&CROWDED, 200000, 0xD1B54A32D192ED03ull, &what) != 0);
    m_bug = 3;
    T("control: one that gives a compositor hold the 20 ms penalty bound is caught", differential(&CROWDED, 200000, 0xD1B54A32D192ED03ull, &what) != 0);
    m_bug = 4;
    T("control: one where distrust lets go of the owner's holds in flight is caught", differential(&CROWDED, 200000, 0xD1B54A32D192ED03ull, &what) != 0);
    m_bug = 5;
    T("control: one where an exiting process's other connections keep their holds is caught", differential(&BUSY, 200000, 0x9E3779B97F4A7C15ull, &what) != 0);
    m_bug = 6;
    T("control: one that answers READY past the ring even after everything was ended at once is caught", differential(&BUSY, 200000, 0x9E3779B97F4A7C15ull, &what) != 0);
    m_bug = 0;

    fresh();
    (void)open1(1, 11, 1, 7, 0); (void)open1(1, 11, 2, 8, 0); (void)open1(2, 22, 1, 7, COMP);
    T("control: the list check passes on a sound table", structure(&Tb) == NULL);
    Tb.o[nvf_owner_find(&Tb, 1)].entries++;
    T("control: a wrong hold count is seen", structure(&Tb) != NULL);
    Tb.o[nvf_owner_find(&Tb, 1)].entries--;
    ei = Tb.bucket[nvf_bucket(8)];
    Tb.bucket[nvf_bucket(8)] = (unsigned short)NVF_NIL;
    T("control: a hold cut out of its bucket chain is seen", structure(&Tb) != NULL);
    Tb.bucket[nvf_bucket(8)] = (unsigned short)ei;
    Tb.liveCompositor = 0;
    T("control: a wrong compositor count is seen", structure(&Tb) != NULL);
    Tb.liveCompositor = 1;
    Tb.e[Tb.freeHead].state = 1;
    T("control: a hold in use on the free list is seen", structure(&Tb) != NULL);
    Tb.e[Tb.freeHead].state = 0;
    T("control: and the table is sound again", structure(&Tb) == NULL);

    /* the scripted world can show a sleep that would have blocked through an interrupt */
    {
        struct NVRMFenceWait w;
        unsigned h;
        fresh(); w_reset();
        h = chan_new(1, 11); sH = h;
        (void)open1(1, 11, 1, 7, COMP | AUTO); (void)arm1(1, 1, 7, h, 100, 0);
        (void)wait_for(7, 0, 0, 20, 0, &w);
        T("control: without an interrupt during the look the first sleep does block (time moves)", W.sleeps >= 2 && W.at[1] > W.at[0] && W.kSeen[0] == W.kAtSleep[0]);
    }
}

int main(void)
{
    arm_sizes();
    arm_open_close();
    arm_full();
    arm_states();
    arm_filters();
    arm_snapshot();
    arm_endings();
    arm_pid_gone();
    arm_rebind();
    arm_bounds();
    arm_distrust();
    arm_expired();
    arm_trust();
    arm_close_agree();
    arm_results();
    arm_wait();
    arm_wait_seq();
    arm_value_only_rises();
    arm_log();
    arm_addresses();
    arm_off();
    arm_differential();
    arm_controls();
    printf(fails ? "\n%d FAILED\n" : "\nall passed\n", fails);
    return fails ? 1 : 0;
}
