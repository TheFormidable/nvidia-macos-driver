/* test_cmd_life.c - which command buffers a dead image waits for (plugin/nvmtl_cmd_life.h), proven on the host.
   Build and run anywhere:
     cc -std=c11 -O2 -Wall -Wextra -Werror -I plugin tools/test_cmd_life.c -o /tmp/test_cmd_life && /tmp/test_cmd_life

   Every kind of command-buffer life the plugin has is driven through the header, in long random interleavings:
   own submit, riding another's submit (the pre-submit prologue, the counter reset), dropped before the queue (abandon,
   a submit that fails before vkQueueSubmit, an empty or failed prologue, a counter reset whose owner is dropped), a
   second end of an ended record, the end of a record never counted, a record moved to another (a submission part
   taking the command buffer's recording). After every step: the open count is the model's, and is 0 whenever nothing
   is open; a dead image is never released while a command buffer open at its death can still be submitted, or has
   been submitted and not completed, or while a submit in flight at its death has not completed; and once everything
   is ended and completed, every dead image is released (the horizon does not fall behind for ever).
   Controls: the 2.9 counters with a prologue's begin and no end drift away for good; a double end lowers the 2.9
   horizon below a command buffer still open; a command buffer begun after a death and submitted first overtakes the
   2.9 horizon; and the random checker itself reports a release rule that ignores open command buffers, and one that
   uses the 2.9 count. */
#include "nvmtl_cmd_life.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int g_fail, g_pass;
static void check(int ok, const char *what)
{
    if (ok) g_pass++; else { g_fail++; printf("FAIL %s\n", what); }
}

/* ---- the random world ---------------------------------------------------------------------------------------- */
#define MAXL 400000
#define MAXD 200000
enum { L_FREE, L_OPEN, L_ENDED };
typedef struct { nvmtl_cl_tag tag; int state, rider, owner, nriders; uint64_t seq; } life;
typedef struct { nvmtl_cl_mark m; uint64_t sub_at_death; int first_open, nopen; int *open; int live; } dead;
typedef int (*due_fn)(nvmtl_cl_state *, nvmtl_cl_mark *, uint64_t);

static life L[MAXL]; static int nl;
static dead D[MAXD]; static int nd, dhead;
static int openv[MAXL], nopen;          /* indices of open lives */
static nvmtl_cl_state S; static uint64_t finished;
static int violations;
static unsigned long long rng = 88172645463325252ull;
static unsigned rnd(unsigned n) { rng ^= rng << 13; rng ^= rng >> 7; rng ^= rng << 17; return (unsigned)(rng % n); }

static void open_add(int i) { openv[nopen++] = i; }
static void open_del(int i) { for (int k = 0; k < nopen; k++) if (openv[k] == i) { openv[k] = openv[--nopen]; return; } }
static int begin(int rider, int owner)
{
    if (nl >= MAXL) return -1;
    const int i = nl++; memset(&L[i], 0, sizeof L[i]);
    nvmtl_cl_begin(&S, &L[i].tag); L[i].state = L_OPEN; L[i].rider = rider; L[i].owner = owner;
    if (rider) L[owner].nriders++;
    open_add(i); return i;
}
static void end_model(int i, uint64_t seq) { L[i].state = L_ENDED; L[i].seq = seq; open_del(i); }
static void drop(int i)
{
    if (!L[i].rider)   /* dropping an owner retires its counter reset with it (nvmtl_counter_retire) */
        for (int k = nopen - 1; k >= 0; k--) { const int r = openv[k]; if (L[r].rider && L[r].owner == i) { nvmtl_cl_drop(&S, &L[r].tag); end_model(r, 0); } }
    nvmtl_cl_drop(&S, &L[i].tag); end_model(i, 0);
}
static void submit(int i)
{
    int riders[8], n = 0;
    for (int k = 0; k < nopen; k++) { const int r = openv[k]; if (L[r].rider && L[r].owner == i && n < 8) riders[n++] = r; }
    for (int k = 0; k < n; k++) nvmtl_cl_ride(&S, &L[riders[k]].tag);
    const uint64_t seq = nvmtl_cl_submit(&S, &L[i].tag);
    for (int k = 0; k < n; k++) end_model(riders[k], seq);
    end_model(i, seq);
}
/* a release is safe when everything that could read the dead thing has completed */
static int release_safe(const dead *d)
{
    if (d->sub_at_death > finished) return 0;
    for (int k = 0; k < d->nopen; k++) {
        const life *x = &L[d->open[k]];
        if (x->state == L_OPEN) return 0;
        if (x->seq > finished) return 0;
    }
    return 1;
}
static void release(dead *d, const char *where)
{
    if (!release_safe(d)) { if (violations++ < 3) printf("  unsafe release (%s): death with %d open, finished %llu\n", where, d->nopen, (unsigned long long)finished); }
    d->live = 0; free(d->open); d->open = NULL;
}
static void death(due_fn due)
{
    if (nd >= MAXD) return;
    dead *d = &D[nd]; memset(d, 0, sizeof *d);
    d->sub_at_death = S.submitted; d->nopen = nopen; d->open = malloc(sizeof(int) * (size_t)(nopen ? nopen : 1));
    memcpy(d->open, openv, sizeof(int) * (size_t)nopen);
    nvmtl_cl_death(&S, &d->m);
    d->m.pad = (uint32_t)nvmtl_cl_open(&S);   /* only the 2.9-style rule below reads it */
    d->live = 1;
    if (due(&S, &d->m, finished)) release(d, "at death");   /* released at once, never queued (nvmtl_grace_hold) */
    nd++;
}
static void reap(due_fn due)
{
    while (dhead < nd && (!D[dhead].live || due(&S, &D[dhead].m, finished))) { if (D[dhead].live) release(&D[dhead], "reap"); dhead++; }
}
static int open_rand(int want_rider)
{
    int c[64], n = 0;
    for (int k = 0; k < nopen && n < 64; k++) if (L[openv[k]].rider == want_rider) c[n++] = openv[k];
    return n ? c[rnd((unsigned)n)] : -1;
}

/* One random run. Returns the number of unsafe releases; *stuck counts dead things left after a full drain. */
static int run(due_fn due, unsigned steps, unsigned seed, int *stuck, int *bad_counts)
{
    memset(&S, 0, sizeof S); nl = 0; nd = 0; dhead = 0; nopen = 0; finished = 0; violations = 0;
    rng = 88172645463325252ull ^ ((unsigned long long)seed * 0x9E3779B97F4A7C15ull);
    *stuck = 0; *bad_counts = 0;
    for (unsigned t = 0; t < steps && nl < MAXL - 16 && nd < MAXD - 4; t++) {
        const unsigned ev = rnd(100);
        if (ev < 18) { if (nopen < 48) begin(0, 0); }
        else if (ev < 26) { const int o = open_rand(0); if (o >= 0 && L[o].nriders < 2) begin(1, o); }   /* prologue / counter reset */
        else if (ev < 44) { const int o = open_rand(0); if (o >= 0) submit(o); }
        else if (ev < 50) { const int o = open_rand(0); if (o >= 0) { drop(o); reap(due); } }                 /* abandon, failed before the queue */
        else if (ev < 53) { const int r = open_rand(1); if (r >= 0) drop(r); }                                 /* empty or failed prologue */
        else if (ev < 56) { if (nl) { const int i = (int)rnd((unsigned)nl); if (L[i].state == L_ENDED) {     /* a second end */
                     const nvmtl_cl_state before = S; nvmtl_cl_drop(&S, &L[i].tag); nvmtl_cl_ride(&S, &L[i].tag);
                     if (memcmp(&before, &S, sizeof S)) (*bad_counts)++; } } }
        else if (ev < 58) { nvmtl_cl_tag z; memset(&z, 0, sizeof z); const nvmtl_cl_state before = S;         /* never counted */
                     nvmtl_cl_drop(&S, &z); nvmtl_cl_ride(&S, &z); if (memcmp(&before, &S, sizeof S)) (*bad_counts)++; }
        else if (ev < 60) { const int o = open_rand(0); if (o >= 0) { nvmtl_cl_tag moved = L[o].tag; memset(&L[o].tag, 0, sizeof L[o].tag);
                     nvmtl_cl_drop(&S, &L[o].tag); L[o].tag = moved; } }                                        /* part->commands = _c; memset(&_c) */
        else if (ev < 78) death(due);
        else { if (finished < S.submitted && rnd(4)) { finished++; reap(due); } }
        if (nvmtl_cl_open(&S) != (uint64_t)nopen || S.strays || S.uncounted) (*bad_counts)++;
        if ((t & 4095) == 4095 || t + 1 == steps) {   /* drain: end everything, complete everything */
            while (nopen) { const int o = openv[rnd((unsigned)nopen)]; if (L[o].rider) drop(o); else if (rnd(3)) submit(o); else drop(o); }
            finished = S.submitted; reap(due);
            if (nvmtl_cl_open(&S) != 0) (*bad_counts)++;
            for (int k = dhead; k < nd; k++) if (D[k].live) (*stuck)++;
            if (*stuck) break;
        }
    }
    for (int k = 0; k < nd; k++) if (D[k].open) { free(D[k].open); D[k].open = NULL; }
    return violations;
}

/* the release rules the checker must catch */
static int due_inflight_only(nvmtl_cl_state *s, nvmtl_cl_mark *m, uint64_t f) { (void)s; return m->seq <= f; }
static int due_count_29(nvmtl_cl_state *s, nvmtl_cl_mark *m, uint64_t f) { (void)s; return m->seq + m->pad <= f; }
static int due_real(nvmtl_cl_state *s, nvmtl_cl_mark *m, uint64_t f) { return nvmtl_cl_due(s, m, f); }

/* ---- 2.9's three counters, for the controls ---------------------------------------------------------------- */
typedef struct { uint64_t begun, submitted, abandoned; } c29;
static uint64_t h29(const c29 *c) { const uint64_t p = c->begun > c->submitted + c->abandoned ? c->begun - c->submitted - c->abandoned : 0; return c->submitted + p; }

int main(void)
{
    /* 1. the scripted lives, one by one */
    {
        nvmtl_cl_state s; memset(&s, 0, sizeof s); nvmtl_cl_tag a, b, p; nvmtl_cl_mark m;
        nvmtl_cl_begin(&s, &a); nvmtl_cl_death(&s, &m);
        check(!nvmtl_cl_due(&s, &m, 0), "a death with a command buffer open waits");
        const uint64_t q = nvmtl_cl_submit(&s, &a);
        check(q == 1 && !nvmtl_cl_due(&s, &m, 0), "after its submit it waits for that submit to complete");
        check(nvmtl_cl_due(&s, &m, 1), "and is released once it has");
        check(nvmtl_cl_open(&s) == 0, "nothing open: the open count is 0");
        memset(&s, 0, sizeof s); nvmtl_cl_begin(&s, &a); nvmtl_cl_begin(&s, &p); nvmtl_cl_death(&s, &m);
        nvmtl_cl_ride(&s, &p); const uint64_t q2 = nvmtl_cl_submit(&s, &a);
        check(nvmtl_cl_open(&s) == 0 && s.rode == 1 && s.own == 1, "a prologue that rides its owner's submit ends with it");
        check(!nvmtl_cl_due(&s, &m, q2 - 1) && nvmtl_cl_due(&s, &m, q2), "the dead image waits for that one submit, not one more");
        memset(&s, 0, sizeof s); nvmtl_cl_begin(&s, &a); nvmtl_cl_death(&s, &m); nvmtl_cl_drop(&s, &a);
        check(nvmtl_cl_due(&s, &m, 0), "a command buffer dropped unsubmitted releases at once when nothing is in flight");
        nvmtl_cl_drop(&s, &a); nvmtl_cl_ride(&s, &a);
        check(s.dropped == 1 && s.rode == 0 && s.strays == 0, "a second end of the same record counts nothing");
        memset(&b, 0, sizeof b); nvmtl_cl_drop(&s, &b);
        check(s.dropped == 1, "the end of a record never counted counts nothing");
        memset(&s, 0, sizeof s); nvmtl_cl_death(&s, &m);
        check(nvmtl_cl_due(&s, &m, 0), "an idle queue: a death is released at once");
        memset(&s, 0, sizeof s); nvmtl_cl_submit(&s, &b);
        check(s.uncounted == 1 && s.submitted == 1, "a submit of an uncounted record still takes a sequence, counted apart");
        /* a command buffer begun after the death does not hold the dead image */
        memset(&s, 0, sizeof s); nvmtl_cl_death(&s, &m); nvmtl_cl_begin(&s, &a);
        check(nvmtl_cl_due(&s, &m, 0), "a command buffer begun after the death is not waited for");
        /* a full cohort ring only holds longer */
        memset(&s, 0, sizeof s); nvmtl_cl_tag t[NVMTL_CL_COHORTS + 4]; nvmtl_cl_mark mm[NVMTL_CL_COHORTS + 4];
        for (unsigned k = 0; k < NVMTL_CL_COHORTS + 4; k++) { nvmtl_cl_begin(&s, &t[k]); nvmtl_cl_death(&s, &mm[k]); }
        check(s.merged > 0, "more open cohorts than the ring holds merge into the current one");
        int any = 0; for (unsigned k = 0; k < NVMTL_CL_COHORTS + 4; k++) any |= nvmtl_cl_due(&s, &mm[k], 0);
        check(!any, "while all are open nothing is released");
        for (unsigned k = 0; k < NVMTL_CL_COHORTS + 4; k++) nvmtl_cl_drop(&s, &t[k]);
        int all = 1; for (unsigned k = 0; k < NVMTL_CL_COHORTS + 4; k++) all &= nvmtl_cl_due(&s, &mm[k], 0);
        check(all && nvmtl_cl_open(&s) == 0 && s.strays == 0, "once all have ended everything is released");
        /* a record that is never ended (lost): later dead images are released after NVMTL_CL_PATIENCE completed submits */
        memset(&s, 0, sizeof s); nvmtl_cl_tag lost; nvmtl_cl_begin(&s, &lost);
        nvmtl_cl_mark lm[300]; uint64_t fin = 0; unsigned released = 0, early = 0;
        for (unsigned k = 0; k < 300; k++) {
            nvmtl_cl_tag a2; nvmtl_cl_begin(&s, &a2); nvmtl_cl_death(&s, &lm[k]);
            for (unsigned j = 0; j < 10; j++) { nvmtl_cl_tag b2; nvmtl_cl_begin(&s, &b2); fin = nvmtl_cl_submit(&s, &b2); }
            fin = nvmtl_cl_submit(&s, &a2);
            for (unsigned j = 0; j <= k; j++) if (lm[j].pad != 7 && nvmtl_cl_due(&s, &lm[j], fin)) {
                if (fin < lm[j].seq + NVMTL_CL_PATIENCE) early++;
                lm[j].pad = 7; released++; }
        }
        check(early == 0, "a lost record: nothing is released before NVMTL_CL_PATIENCE submits after its death have completed");
        check(released > 150 && s.forced == released, "a lost record: every dead image after it is released, each counted as forced");
        check(s.merged > 0 && s.strays == 0, "a lost record fills the cohort ring; later deaths share a cohort and nothing goes astray");
    }
    /* 2. long random interleavings with the header's rule */
    {
        int unsafe = 0, stuck = 0, bad = 0; uint64_t forced = 0;
        for (unsigned seed = 1; seed <= 24; seed++) { int st, bc; unsafe += run(due_real, 60000, seed, &st, &bc); stuck += st; bad += bc; forced += S.forced; }
        printf("  random: %d runs of 60000 steps (the last: %d lives, %d deaths, %llu submits, %llu rode, %llu dropped), %d unsafe releases, "
               "%d left after a drain, %d count errors\n", 24, nl, nd, (unsigned long long)S.submitted, (unsigned long long)S.rode,
               (unsigned long long)S.dropped, unsafe, stuck, bad);
        check(unsafe == 0, "random: no dead image is released while something open at its death can still read it");
        check(stuck == 0, "random: once everything is ended and completed, every dead image is released");
        check(bad == 0, "random: the open count is the model's, a second end and an uncounted end count nothing");
        check(forced == 0, "random: no release was forced (no command buffer stayed open that long), so every release above was judged");
    }
    /* 3. the checker catches wrong rules (negative controls) */
    {
        int unsafe = 0, st, bc;
        for (unsigned seed = 1; seed <= 4; seed++) unsafe += run(due_inflight_only, 60000, seed, &st, &bc);
        check(unsafe > 0, "control: a rule that waits only for the submits in flight is reported unsafe");
        unsafe = 0;
        for (unsigned seed = 1; seed <= 8; seed++) unsafe += run(due_count_29, 60000, seed, &st, &bc);
        check(unsafe > 0, "control: the 2.9 count (in flight + open at the death) is reported unsafe: a later command buffer overtakes");
    }
    /* 4. 2.9's counters: the defects this replaces */
    {
        /* a frame = owner + prologue begun, one submit, finished; a texture dies in every frame */
        c29 c = { 0, 0, 0 }; uint64_t fin = 0, held = 0, h = 0;
        for (int f = 0; f < 1000; f++) { c.begun += 2; h = h29(&c); c.submitted++; fin = c.submitted; if (h > fin) held++; }
        check(h29(&c) - c.submitted == 1000, "control: with a prologue begun and never ended, the 2.9 horizon runs 1000 submits ahead after 1000 frames");
        check(held == 1000, "control: and no texture that died in those frames is ever released, although the queue is idle");
        nvmtl_cl_state s; memset(&s, 0, sizeof s); unsigned rel = 0;
        for (int f = 0; f < 1000; f++) { nvmtl_cl_tag o, p; nvmtl_cl_mark m; nvmtl_cl_begin(&s, &o); nvmtl_cl_begin(&s, &p); nvmtl_cl_death(&s, &m);
            nvmtl_cl_ride(&s, &p); const uint64_t q = nvmtl_cl_submit(&s, &o); rel += nvmtl_cl_due(&s, &m, q); }
        check(rel == 1000, "the same frames with the header: every texture is released when its frame completes");
        /* a double end: A is dropped and dropped again (counted twice) while B is open */
        c29 d = { 0, 0, 0 }; d.begun = 2; d.abandoned = 2;   /* A begun, B begun, A abandoned twice */
        const uint64_t hd = h29(&d); d.submitted++; const uint64_t bseq = d.submitted;
        check(bseq > hd, "control: a double end lowers the 2.9 horizon below the open command buffer's submit");
        nvmtl_cl_tag A, B; nvmtl_cl_mark m; memset(&s, 0, sizeof s);
        nvmtl_cl_begin(&s, &A); nvmtl_cl_begin(&s, &B); nvmtl_cl_drop(&s, &A); nvmtl_cl_drop(&s, &A); nvmtl_cl_death(&s, &m);
        check(!nvmtl_cl_due(&s, &m, 0), "with the header the second end counts nothing and B is still waited for");
        /* overtaking: X open at the death, Y begun after it and submitted first */
        c29 o = { 1, 0, 0 }; const uint64_t ho = h29(&o); o.begun++; o.submitted++; o.submitted++; const uint64_t xseq = o.submitted;
        check(xseq > ho, "control: a command buffer begun after the death and submitted first overtakes the 2.9 horizon");
        nvmtl_cl_tag X, Y; memset(&s, 0, sizeof s);
        nvmtl_cl_begin(&s, &X); nvmtl_cl_death(&s, &m); nvmtl_cl_begin(&s, &Y); const uint64_t ys = nvmtl_cl_submit(&s, &Y);
        check(!nvmtl_cl_due(&s, &m, ys), "with the header the dead image is not released when Y completes");
        const uint64_t xs = nvmtl_cl_submit(&s, &X);
        check(!nvmtl_cl_due(&s, &m, xs - 1) && nvmtl_cl_due(&s, &m, xs), "but when X does");
    }
    printf("%s: %d passed, %d failed\n", g_fail ? "FAIL" : "OK", g_pass, g_fail);
    return g_fail ? 1 : 0;
}
