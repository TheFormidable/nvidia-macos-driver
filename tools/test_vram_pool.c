/* test_vram_pool.c - the bookkeeping of ready VRAM blocks (kexts/NVRMFB/fb/nvrm-vram-pool.h), proven on the host
   against every short order of events.
   Build and run anywhere:  cc -O2 -I kexts/NVRMFB/fb tools/test_vram_pool.c -o /tmp/t && /tmp/t

   The model. A framebuffer that makes blocks (each a new one, with a cookie of its own, stamped with the number of
   power events so far) and releases them, an accelerator that asks for blocks of two sizes and hands them back, two
   workers on the one pool (a card with two framebuffers), the GPU that goes to standby and comes back, and the clock.
   Events, in every order up to DEPTH:
     A B  the accelerator asks for a block of size A / B: the pool is asked first (note, take), else the framebuffer
          makes one on the spot, as before this header
     R    the accelerator hands back the block it has held longest: the framebuffer releases it, and the pool is told
     W V  one step of the first / second worker, as nvrm-fb.cpp does them. With a block in hand: it is stored, or else
          released, or (GPU in standby) left to wait in the pool. Else, with the GPU up: one block that is due goes
          back, or one is made and stays in the worker's hand until its next step - in the driver the block is made
          and filled without the pool's lock, so every other event can come in between
     Z U  the GPU goes to standby / comes back (each a power event)
     D    the first framebuffer stops: its worker finishes what it has in hand; then, with the GPU up, everything
          goes back
     F    everything goes back because a grant of the accelerator found no room
     t T  0.4 s / 1.1 s pass
   What must hold after every event, and the broken rule that each one catches:
   - every block is in one place: with the accelerator, ready, in a worker's hand, waiting to go back, or released;
     none is in two, none is lost (control: a take that leaves the block ready)
   - a block the pool hands out was ready, has the size asked for and was made after the last power event
     (controls: a take that does not look at the generation; a store that stamps the block with the current one)
   - the ready blocks never exceed the ceiling, and the pool's own sum is right (control: a pool that never looks at
     the ceiling); and the worker makes no block that the ceiling has no room for (control: a worker that asks
     without looking at the ceiling)
   - the worker makes a block only of a size the accelerator both asked for and handed back (control: a worker that
     counts grants alone)
   - for a second after everything went back for want of room the worker makes nothing (control: a flush that does
     not put it off) and no block becomes ready (control: a store that does not look at the flush)
   - the framebuffer is never called by a worker while the GPU is in standby (the model's worker keeps that rule as
     nvrm-fb.cpp does, by a look at the GPU first; fb_grant and fb_release check it), and a block that could not go
     back has a place to wait
   - a worker that finds nothing to do leaves nothing made before a power event, nothing that waits to go back and
     no ready block of a size that is no longer in churn (controls: a worker that forgets the blocks of an older
     generation; one that forgets the blocks that wait)
   - nothing is released twice, and after F, or D with the GPU up, the pool is empty
   Not in the model, in the unit checks only: a make that the framebuffer refuses (nvrm_vp_refused), a worker that
   gives everything back because its framebuffer no longer keeps blocks (`all`), a budget that changes, more than
   two sizes. */
#include "nvrm-vram-pool.h"
#include <stdio.h>
#include <string.h>

static int fails;
#define ARM(name, cond) do { const int ok = !!(cond); printf("%s  %s\n", ok ? "ok  " : "FAIL", name); if (!ok) fails++; } while (0)

#define M (1ull << 20)
#define SIZE_A 9043968ull            /* the measured capture surface */
#define SIZE_B (2 * M)
#define BUDGET (160 * M)             /* an eighth is 20 MB: two A, or two A and one B: the ceiling binds */
#define CAP    (BUDGET / 8)
#define BIG    (4096 * M)            /* the measured card's budget */
#define SEC    1000000000ull

/* ---- the rules under test, and the broken ones ---- */
typedef struct {
    int (*take)(nvrm_vp *, unsigned long long, nvrm_vp_block *);
    int (*store)(nvrm_vp *, const nvrm_vp_block *, unsigned long long, unsigned long long);
    int (*next_release)(nvrm_vp *, unsigned long long, int, nvrm_vp_block *);
    unsigned long long (*next_fill)(const nvrm_vp *, unsigned long long, unsigned long long, unsigned long long *);
    void (*flushed)(nvrm_vp *, unsigned long long);
} rules;

static int take_any_generation(nvrm_vp *p, unsigned long long bytes, nvrm_vp_block *out)
{
    nvrm_vp_size *s = nvrm_vp_find(p, bytes);
    if (!s || !s->n) return 0;
    nvrm_vp_pop(p, s, 0, out);
    return 1;
}
static int take_and_leave(nvrm_vp *p, unsigned long long bytes, nvrm_vp_block *out)
{
    nvrm_vp_size *s = nvrm_vp_find(p, bytes);
    if (!s) return 0;
    for (unsigned i = 0; i < s->n; i++) if (s->ready[i].gen == p->gen) { *out = s->ready[i]; return 1; }
    return 0;
}
static int store_no_ceiling(nvrm_vp *p, const nvrm_vp_block *b, unsigned long long now, unsigned long long budget)
{
    (void)budget;
    return nvrm_vp_store(p, b, now, ~0ull);
}
static int store_restamps(nvrm_vp *p, const nvrm_vp_block *b, unsigned long long now, unsigned long long budget)
{
    nvrm_vp_block c = *b;
    c.gen = p->gen;
    return nvrm_vp_store(p, &c, now, budget);
}
/* the store before the reviewers: it did not know that everything had just gone back */
static int store_ignores_flush(nvrm_vp *p, const nvrm_vp_block *b, unsigned long long now, unsigned long long budget)
{
    nvrm_vp_size *s = nvrm_vp_find(p, b->bytes);
    const unsigned long long all = p->nofill, one = s ? s->nofill : 0;
    p->nofill = 0; if (s) s->nofill = 0;
    const int ok = nvrm_vp_store(p, b, now, budget);
    p->nofill = all; if (s) s->nofill = one;
    return ok;
}
static int next_release_forgets_stale(nvrm_vp *p, unsigned long long now, int all, nvrm_vp_block *out)
{
    if (p->nleft) return nvrm_vp_next_release(p, now, all, out);
    for (unsigned i = 0; i < NVRM_VP_SIZES; i++)
        if (p->s[i].n && (all || !nvrm_vp_is_hot(&p->s[i], now))) { nvrm_vp_pop(p, &p->s[i], 0, out); return NVRM_VP_WHY_IDLE; }
    return 0;
}
static int next_release_forgets_waiting(nvrm_vp *p, unsigned long long now, int all, nvrm_vp_block *out)
{
    const unsigned n = p->nleft;
    p->nleft = 0;
    const int why = nvrm_vp_next_release(p, now, all, out);
    p->nleft = n;
    return why;
}
static unsigned long long next_fill_no_ceiling(const nvrm_vp *p, unsigned long long now, unsigned long long budget, unsigned long long *gen)
{
    (void)budget;
    return nvrm_vp_next_fill(p, now, ~0ull, gen);
}
/* the rule before the reviewers: grants in a row are enough */
static unsigned long long next_fill_grants_only(const nvrm_vp *p, unsigned long long now, unsigned long long budget, unsigned long long *gen)
{
    *gen = p->gen;
    if (now < p->nofill || p->nleft) return 0;
    for (unsigned i = 0; i < NVRM_VP_SIZES; i++) {
        const nvrm_vp_size *s = &p->s[i];
        const unsigned long long cap = nvrm_vp_cap(budget, s->bytes);
        if (!s->bytes || s->heat < NVRM_VP_HOT || !nvrm_vp_within(now, s->last) || s->n >= NVRM_VP_DEPTH) continue;
        if (s->bytes > cap || p->ready_bytes > cap - s->bytes) continue;
        return s->bytes;
    }
    return 0;
}
static void flushed_and_forgotten(nvrm_vp *p, unsigned long long now) { (void)p; (void)now; }
static const rules kHeader = { nvrm_vp_take, nvrm_vp_store, nvrm_vp_next_release, nvrm_vp_next_fill, nvrm_vp_refused_all };

/* ---- the model ---- */
enum { GONE = 0, ACCEL, READY, HAND, WAITS };
#define MAXB 40
#define WORKERS 2
typedef struct {
    nvrm_vp pool;
    unsigned long long now, power;               /* the clock; power events so far */
    int down;                                    /* the GPU is in standby */
    unsigned long long flushedAt; int flushedOnce;
    unsigned nb;                                 /* blocks ever made */
    unsigned asked[2], backs[2];                 /* the accelerator's grants and releases of size A / B, ever */
    unsigned char where[MAXB];
    unsigned char releases[MAXB];
    unsigned long long madeAt[MAXB], bytes[MAXB];/* the power count when it was made */
    unsigned held[MAXB], nheld;                  /* the accelerator's blocks, oldest first */
    struct { int has; nvrm_vp_block b; } hand[WORKERS];   /* the block a worker made and has not stored yet */
    const char *broke;                           /* the first rule that broke */
} world;

static void broke(world *w, const char *what) { if (!w->broke) w->broke = what; }
static unsigned which(unsigned long long bytes) { return bytes == SIZE_A ? 0u : 1u; }

static int fb_grant(world *w, unsigned long long bytes, nvrm_vp_block *b, int byWorker)
{
    if (byWorker && w->down) broke(w, "a worker called the framebuffer while the GPU was in standby");
    if (w->nb >= MAXB) { broke(w, "the model ran out of blocks"); return 0; }
    const unsigned id = w->nb++;
    w->where[id] = GONE; w->releases[id] = 0; w->madeAt[id] = w->power; w->bytes[id] = bytes;
    b->handle = (void *)(unsigned long)(id + 1); b->kva = (void *)(unsigned long)(0x1000 + id);
    b->phys = 0x100000ull * (id + 1); b->bytes = bytes; b->cookie = id + 1; b->gen = 0;
    return 1;
}
static unsigned id_of(const nvrm_vp_block *b) { return (unsigned)(unsigned long)b->handle - 1; }
static void fb_release(world *w, const nvrm_vp_block *b, int from, int byWorker)
{
    const unsigned id = id_of(b);
    if (byWorker && w->down) broke(w, "a worker called the framebuffer while the GPU was in standby");
    if (id >= w->nb || b->cookie != id + 1) { broke(w, "a release names no block"); return; }
    if (w->where[id] != from) broke(w, "a block was released from a place it was not in");
    if (w->releases[id]++) broke(w, "a block was released twice");
    w->where[id] = GONE;
}
static void check(world *w)
{
    unsigned seen[MAXB]; memset(seen, 0, sizeof seen);
    unsigned long long sum = 0;
    for (unsigned i = 0; i < NVRM_VP_SIZES; i++) {
        const nvrm_vp_size *s = &w->pool.s[i];
        if (s->n > NVRM_VP_DEPTH) broke(w, "more ready blocks than a size may have");
        for (unsigned k = 0; k < s->n && k < NVRM_VP_DEPTH; k++) {
            const unsigned id = id_of(&s->ready[k]);
            if (id >= w->nb) { broke(w, "a ready entry names no block"); continue; }
            if (w->where[id] != READY || seen[id]++) broke(w, "a block is in two places");
            if (s->ready[k].bytes != s->bytes) broke(w, "a ready block of another size");
            sum += s->ready[k].bytes;
        }
    }
    if (w->pool.nleft > NVRM_VP_LEFT) broke(w, "more blocks wait than there are places");
    for (unsigned k = 0; k < w->pool.nleft && k < NVRM_VP_LEFT; k++) {
        const unsigned id = id_of(&w->pool.left[k]);
        if (id >= w->nb) { broke(w, "a waiting entry names no block"); continue; }
        if (w->where[id] != WAITS || seen[id]++) broke(w, "a block is in two places");
    }
    for (unsigned k = 0; k < WORKERS; k++) {
        if (!w->hand[k].has) continue;
        const unsigned id = id_of(&w->hand[k].b);
        if (id >= w->nb || w->where[id] != HAND || seen[id]++) broke(w, "a block is in two places");
    }
    for (unsigned id = 0; id < w->nb; id++)
        if ((w->where[id] == READY || w->where[id] == WAITS || w->where[id] == HAND) && !seen[id]) broke(w, "a block was lost");
    if (sum != w->pool.ready_bytes) broke(w, "the pool's sum of ready bytes is wrong");
    if (sum > CAP) broke(w, "the ready blocks exceed the ceiling");
}
static void ev_grant(world *w, const rules *r, unsigned long long bytes)
{
    nvrm_vp_block b; memset(&b, 0, sizeof b);
    w->asked[which(bytes)]++;
    nvrm_vp_note(&w->pool, bytes, w->now);
    if (r->take(&w->pool, bytes, &b)) {
        const unsigned id = id_of(&b);
        if (id >= w->nb) { broke(w, "a hit names no block"); return; }
        if (w->where[id] != READY) broke(w, "a block was handed out that was not ready");
        if (b.bytes != bytes || w->bytes[id] != bytes) broke(w, "a block of another size was handed out");
        if (w->madeAt[id] != w->power) broke(w, "a block made before a power event was handed out after it");
        w->where[id] = ACCEL; w->held[w->nheld++] = id;
        return;
    }
    if (!fb_grant(w, bytes, &b, 0)) return;
    w->where[id_of(&b)] = ACCEL; w->held[w->nheld++] = id_of(&b);
}
static void ev_release(world *w)
{
    if (!w->nheld) return;
    const unsigned id = w->held[0];
    for (unsigned k = 1; k < w->nheld; k++) w->held[k - 1] = w->held[k];
    w->nheld--;
    nvrm_vp_block b; memset(&b, 0, sizeof b);
    b.handle = (void *)(unsigned long)(id + 1); b.kva = (void *)(unsigned long)(0x1000 + id);
    b.bytes = w->bytes[id]; b.cookie = id + 1;
    fb_release(w, &b, ACCEL, 0);
    w->backs[which(b.bytes)]++;
    nvrm_vp_note_back(&w->pool, b.bytes, w->now);
}
/* One step of worker `k`, as vramPoolStep in nvrm-fb.cpp takes them. */
static void ev_worker(world *w, const rules *r, unsigned k)
{
    nvrm_vp_block b; memset(&b, 0, sizeof b);
    if (w->hand[k].has) {                        /* the block it made: stored, or released, or left to wait */
        b = w->hand[k].b; w->hand[k].has = 0;
        if (r->store(&w->pool, &b, w->now, BUDGET)) {
            w->where[id_of(&b)] = READY;
            if (w->flushedOnce && w->now - w->flushedAt < NVRM_VP_WINDOW_NS) broke(w, "a block became ready within a second of a flush");
            return;
        }
        if (!w->down) { fb_release(w, &b, HAND, 1); return; }
        if (nvrm_vp_park(&w->pool, &b)) w->where[id_of(&b)] = WAITS;
        else broke(w, "a block that could not go back had no place to wait");
        return;
    }
    if (w->down) return;                         /* a GPU in standby is left alone */
    const int due = nvrm_vp_release_due(&w->pool, w->now, 0);
    const int why = r->next_release(&w->pool, w->now, 0, &b);
    if (r->next_release == nvrm_vp_next_release && due != why) broke(w, "the look ahead and the release disagree");
    if (why) { fb_release(w, &b, why == NVRM_VP_WHY_LEFT ? WAITS : READY, 1); return; }
    unsigned long long gen = 0;
    const unsigned long long bytes = r->next_fill(&w->pool, w->now, BUDGET, &gen);
    if (!bytes) {                                /* nothing to do: then nothing is left that should have gone */
        if (w->pool.nleft) broke(w, "a worker with nothing to do left a block that waits to go back");
        for (unsigned i = 0; i < NVRM_VP_SIZES; i++) {
            const nvrm_vp_size *s = &w->pool.s[i];
            if (s->n && !nvrm_vp_is_hot(s, w->now)) broke(w, "a worker with nothing to do left ready blocks of a size that is not in churn");
            for (unsigned n = 0; n < s->n; n++)
                if (s->ready[n].gen != w->pool.gen) broke(w, "a worker with nothing to do left a block made before a power event");
        }
        return;
    }
    if (w->asked[which(bytes)] < NVRM_VP_HOT || w->backs[which(bytes)] < NVRM_VP_BACK)
        broke(w, "the worker made a block of a size that is not handed back");
    if (w->flushedOnce && w->now - w->flushedAt < NVRM_VP_WINDOW_NS)
        broke(w, "the worker made a block within a second of a flush");
    if (w->pool.ready_bytes + bytes > CAP) broke(w, "the worker made a block the ceiling has no room for");
    if (w->pool.nleft) broke(w, "the worker made a block while another waited to go back");
    if (!fb_grant(w, bytes, &b, 1)) return;
    b.gen = gen;
    w->where[id_of(&b)] = HAND;
    w->hand[k].has = 1; w->hand[k].b = b;
}
/* Everything goes back. `stop`: the first framebuffer stops - its worker has finished its run (stop() waits for it),
   and with the GPU in standby nothing is released. Else: a grant found no room, the accelerator is on its way into
   NVKMS anyway. */
static void ev_drain(world *w, const rules *r, int stop)
{
    nvrm_vp_block b;
    if (stop) {
        if (w->hand[0].has) ev_worker(w, r, 0);
        if (w->down) return;
    }
    for (unsigned ops = 0; ops < 64; ops++) {
        memset(&b, 0, sizeof b);
        const int why = r->next_release(&w->pool, w->now, 1, &b);
        if (!why) break;
        fb_release(w, &b, why == NVRM_VP_WHY_LEFT ? WAITS : READY, 0);
    }
    if (r->next_release != next_release_forgets_waiting && nvrm_vp_held(&w->pool))
        broke(w, "the pool is not empty after everything was given back");
    if (!stop) { r->flushed(&w->pool, w->now); w->flushedAt = w->now; w->flushedOnce = 1; }
}
static void step(world *w, const rules *r, char e)
{
    switch (e) {
        case 'A': ev_grant(w, r, SIZE_A); break;
        case 'B': ev_grant(w, r, SIZE_B); break;
        case 'R': ev_release(w); break;
        case 'W': ev_worker(w, r, 0); break;
        case 'V': ev_worker(w, r, 1); break;
        case 'Z': w->power++; nvrm_vp_power_event(&w->pool); w->down = 1; break;
        case 'U': w->power++; nvrm_vp_power_event(&w->pool); w->down = 0; break;
        case 'D': ev_drain(w, r, 1); break;
        case 'F': ev_drain(w, r, 0); break;
        case 't': w->now += 4 * SEC / 10; break;
        case 'T': w->now += 11 * SEC / 10; break;
    }
    check(w);
}
static const char kEvents[] = "ABRWVZUDFtT";
#define DEPTH 7
static unsigned long long tried;
static char firstBad[DEPTH + 1];
static const char *firstWhy;
static void explore(const world *w, const rules *r, char *path, int depth)
{
    for (const char *e = kEvents; *e; e++) {
        world n = *w;
        path[depth] = *e; path[depth + 1] = 0;
        step(&n, r, *e);
        tried++;
        if (n.broke) { if (!firstWhy) { firstWhy = n.broke; strcpy(firstBad, path); } continue; }
        if (depth + 1 < DEPTH) explore(&n, r, path, depth + 1);
    }
}
/* Every order of DEPTH events after `lead` (a share that is already running, so that the short orders reach the
   states where blocks are ready). */
static const char *run_model(const rules *r, const char *lead)
{
    static world w;
    char path[DEPTH + 1];
    memset(&w, 0, sizeof w); w.now = 10 * SEC;
    tried = 0; firstWhy = 0; firstBad[0] = 0;
    for (const char *e = lead; *e; e++) step(&w, r, *e);
    if (w.broke) { firstWhy = w.broke; strcpy(firstBad, "(lead)"); return firstWhy; }
    explore(&w, r, path, 0);
    return firstWhy;
}
static world play(const char *events)
{
    world w; memset(&w, 0, sizeof w); w.now = 10 * SEC;
    for (const char *e = events; *e; e++) step(&w, &kHeader, *e);
    return w;
}
static nvrm_vp_block blk(unsigned id, unsigned long long bytes, unsigned long long gen)
{
    nvrm_vp_block b; memset(&b, 0, sizeof b);
    b.handle = (void *)(unsigned long)(id + 1); b.kva = (void *)(unsigned long)(0x1000 + id);
    b.bytes = bytes; b.cookie = id + 1; b.gen = gen;
    return b;
}
/* `n` grants and `back` releases of `bytes`, all at `t`: the size is in churn after 3 and 2 */
static void churn(nvrm_vp *p, unsigned long long bytes, unsigned long long t, int n, int back)
{
    for (int k = 0; k < n; k++) nvrm_vp_note(p, bytes, t);
    for (int k = 0; k < back; k++) nvrm_vp_note_back(p, bytes, t);
}

int main(void)
{
    /* ---- when the fill may be left out ---- */
    ARM("RM clears, no sleep yet, no dirty block: the fill is left out", nvrm_vp_trusted(1, 0, 0));
    ARM("RM does not clear (or did not say): filled", !nvrm_vp_trusted(0, 0, 0));
    ARM("after the first power event: filled, for the rest of the boot", !nvrm_vp_trusted(1, 1, 0) && !nvrm_vp_trusted(1, 12, 0));
    ARM("after the first block that was not zero: filled, for the rest of the boot", !nvrm_vp_trusted(1, 0, 1) && !nvrm_vp_trusted(1, 0, 500));
    /* ---- the ceiling ---- */
    ARM("a 4 GiB budget keeps at most 64 MB of 9 MB blocks ready", nvrm_vp_cap(BIG, SIZE_A) == 64 * M);
    ARM("and room for two of a larger size (a 40 MB surface: 80 MB)", nvrm_vp_cap(BIG, 40 * M) == 80 * M);
    ARM("two 4K surfaces (31.6 MB each) fit under the 64 MB", 2 * 33177600ull <= nvrm_vp_cap(BIG, 33177600ull));
    ARM("never more than an eighth of the budget", nvrm_vp_cap(192 * M, SIZE_A) == 24 * M && nvrm_vp_cap(192 * M, 40 * M) == 24 * M &&
        nvrm_vp_cap(BIG, 300 * M) == 512 * M && nvrm_vp_cap(BIG, ~0ull) == 512 * M);
    ARM("no budget, nothing ready", nvrm_vp_cap(0, SIZE_A) == 0 && nvrm_vp_cap(0, 0) == 0);
    {
        const unsigned long long sizes[] = { 65536, 512 * 1024, SIZE_A, 16 * M, 33177600ull, 4096 * M };
        int inside = 1, rising = 1, lastIsEnd = 1, notFirst = 1;
        for (unsigned s = 0; s < sizeof sizes / sizeof sizes[0]; s++) {
            const unsigned long long q = sizes[s] >> 3;
            unsigned long long prev = 0;
            for (unsigned i = 0; i <= NVRM_VP_PROBES; i++) {
                const unsigned long long at = nvrm_vp_probe_at(sizes[s], i);
                if (at >= q) inside = 0;
                if (!at) notFirst = 0;
                if (i && at <= prev) rising = 0;
                prev = at;
            }
            if (nvrm_vp_probe_at(sizes[s], NVRM_VP_PROBES) != q - 1) lastIsEnd = 0;
        }
        ARM("every probe lies inside the block", inside);
        ARM("the probes rise through the block, no place twice", rising);
        ARM("the last probe is the block's last 8 bytes", lastIsEnd);
        ARM("no probe reads the first 8 bytes, which the live test of the mapping has just written", notFirst);
        ARM("the first probe is the 8 bytes after them", nvrm_vp_probe_at(SIZE_A, 0) == 1 && nvrm_vp_probe_at(65536, 0) == 1);
        /* an image 2048 pixels wide (rows of 8192 bytes) in a 16 MB block: the probes must not all sit in one column */
        unsigned cols[NVRM_VP_PROBES], distinct = 0;
        for (unsigned i = 0; i < NVRM_VP_PROBES; i++) {
            cols[i] = (unsigned)((nvrm_vp_probe_at(16 * M, i) * 8) % 8192);
            unsigned k = 0; while (k < i && cols[k] != cols[i]) k++;
            if (k == i) distinct++;
        }
        ARM("with rows of 8192 bytes the probes fall on many columns", distinct >= NVRM_VP_PROBES / 2);
        ARM("a block shorter than the probes is read at its end", nvrm_vp_probe_at(256, 5) == 31 && nvrm_vp_probe_at(0, 5) == 0);
        ARM("one with a single 8 bytes per probe is read from its start (512 bytes: 64 places)", nvrm_vp_probe_at(512, 0) == 0 &&
            nvrm_vp_probe_at(512, 1) == 1 && nvrm_vp_probe_at(512, 63) == 63 && nvrm_vp_probe_at(512, 64) == 63);
    }

    /* ---- churn ---- */
    {
        nvrm_vp p; memset(&p, 0, sizeof p);
        const unsigned long long t = 10 * SEC;
        ARM("the first grant of a size does not put it in churn", !nvrm_vp_note(&p, SIZE_A, t) && !nvrm_vp_hot(&p, SIZE_A, t));
        ARM("nor the second", !nvrm_vp_note(&p, SIZE_A, t + SEC / 20));
        ARM("nor the third, while nothing came back: surfaces that stay", !nvrm_vp_note(&p, SIZE_A, t + SEC / 10) && !nvrm_vp_hot(&p, SIZE_A, t + SEC / 10));
        nvrm_vp_note_back(&p, SIZE_A, t + SEC / 10);
        ARM("one release is not enough", !nvrm_vp_hot(&p, SIZE_A, t + SEC / 10));
        nvrm_vp_note_back(&p, SIZE_A, t + SEC / 10);
        ARM("three grants and two releases in a row: in churn", nvrm_vp_hot(&p, SIZE_A, t + SEC / 10) && nvrm_vp_note(&p, SIZE_A, t + SEC / 10));
        ARM("it stays so for a second after the last of each", nvrm_vp_hot(&p, SIZE_A, t + SEC / 10 + SEC));
        ARM("and not after that", !nvrm_vp_hot(&p, SIZE_A, t + SEC / 10 + SEC + 1));
        ARM("grants that go on without releases end it too",
            (nvrm_vp_note(&p, SIZE_A, t + SEC), nvrm_vp_note(&p, SIZE_A, t + 2 * SEC), !nvrm_vp_hot(&p, SIZE_A, t + 2 * SEC)));
        ARM("a grant after a pause starts the count again", !nvrm_vp_note(&p, SIZE_A, t + 5 * SEC) && nvrm_vp_find(&p, SIZE_A)->heat == 1);
        ARM("a release after a pause starts its count again", (nvrm_vp_note_back(&p, SIZE_A, t + 5 * SEC), nvrm_vp_find(&p, SIZE_A)->back == 1));
        ARM("another size has counts of its own", !nvrm_vp_note(&p, SIZE_B, t + 5 * SEC) && !nvrm_vp_hot(&p, SIZE_B, t + 5 * SEC));
        ARM("size 0 is never followed", !nvrm_vp_note(&p, 0, t) && !nvrm_vp_hot(&p, 0, t));
        memset(&p, 0, sizeof p);
        nvrm_vp_note_back(&p, SIZE_A, t); nvrm_vp_note_back(&p, SIZE_A, t);
        ARM("a release of a size nobody asked for takes no slot", !nvrm_vp_find(&p, SIZE_A));
        ARM("grants exactly a second apart are still in a row",
            (memset(&p, 0, sizeof p), nvrm_vp_note(&p, SIZE_A, t), nvrm_vp_note(&p, SIZE_A, t + SEC), nvrm_vp_note(&p, SIZE_A, t + 2 * SEC),
             nvrm_vp_find(&p, SIZE_A)->heat == 3));
        ARM("one nanosecond more and they are not",
            (memset(&p, 0, sizeof p), nvrm_vp_note(&p, SIZE_A, t), nvrm_vp_note(&p, SIZE_A, t + SEC + 1), nvrm_vp_find(&p, SIZE_A)->heat == 1));
        /* the driver reads the clock before it takes the pool's lock, so two notes can arrive the wrong way round */
        ARM("a clock read out of order still counts as in a row", nvrm_vp_within(5, 10) &&
            (memset(&p, 0, sizeof p), nvrm_vp_note(&p, SIZE_A, t + 10), nvrm_vp_note(&p, SIZE_A, t + 5), nvrm_vp_note(&p, SIZE_A, t + 7),
             nvrm_vp_find(&p, SIZE_A)->heat == 3) &&
            (nvrm_vp_note_back(&p, SIZE_A, t + 9), nvrm_vp_note_back(&p, SIZE_A, t + 6), nvrm_vp_find(&p, SIZE_A)->back == 2));
        memset(&p, 0, sizeof p);
        churn(&p, SIZE_A, t, 3, 1);
        nvrm_vp_note_back(&p, SIZE_A, t + 9 * SEC / 10); nvrm_vp_note_back(&p, SIZE_A, t + 18 * SEC / 10);
        ARM("releases that go on after the grants stopped do not keep a size in churn",
            nvrm_vp_find(&p, SIZE_A)->back == 3 && nvrm_vp_hot(&p, SIZE_A, t + SEC) && !nvrm_vp_hot(&p, SIZE_A, t + 18 * SEC / 10));
    }
    /* ---- which sizes are followed ---- */
    {
        nvrm_vp p; memset(&p, 0, sizeof p);
        const unsigned long long t = 10 * SEC;
        for (unsigned i = 0; i < NVRM_VP_SIZES; i++) churn(&p, (i + 1) * M, t + i, 3, 2);
        ARM("four sizes are followed at once", nvrm_vp_hot(&p, 1 * M, t + 9) && nvrm_vp_hot(&p, 4 * M, t + 9));
        ARM("a fifth is not followed while the four are warm", !nvrm_vp_note(&p, 5 * M, t + 9) && !nvrm_vp_find(&p, 5 * M));
        nvrm_vp_block b = blk(0, 1 * M, 0);
        ARM("(a ready block for the oldest size)", nvrm_vp_store(&p, &b, t + 9, BUDGET));
        nvrm_vp_note(&p, 5 * M, t + 2 * SEC);
        ARM("when all went cold, the fifth takes the oldest slot that holds nothing", nvrm_vp_find(&p, 5 * M) && nvrm_vp_find(&p, 1 * M) && !nvrm_vp_find(&p, 2 * M));
        ARM("and starts with no releases to its name", nvrm_vp_find(&p, 5 * M)->back == 0 && nvrm_vp_find(&p, 5 * M)->heat == 1);
        ARM("the size that still has a ready block keeps its slot and the block", nvrm_vp_find(&p, 1 * M)->n == 1 && p.ready_bytes == 1 * M);
        memset(&p, 0, sizeof p);
        for (unsigned i = 0; i < NVRM_VP_SIZES; i++) nvrm_vp_note(&p, (i + 1) * M, t + i);   /* one grant each: warm, not in churn */
        ARM("a size that is only warming up keeps its slot against a new one",
            !nvrm_vp_hot(&p, 1 * M, t + 9) && !nvrm_vp_note(&p, 5 * M, t + 9) && !nvrm_vp_find(&p, 5 * M) && nvrm_vp_find(&p, 1 * M));
        ARM("until a second has passed since its last grant", (nvrm_vp_note(&p, 5 * M, t + SEC + 1), nvrm_vp_find(&p, 5 * M) && !nvrm_vp_find(&p, 1 * M)));
        memset(&p, 0, sizeof p);
        unsigned long long gen = 0;
        for (unsigned i = 0; i < NVRM_VP_SIZES; i++) churn(&p, (i + 1) * M, t + i, 3, 2);
        nvrm_vp_refused(&p, 1 * M, t + 15 * SEC / 10);          /* put off until t + 2.5 s */
        churn(&p, 5 * M, t + 2 * SEC, 3, 2);                   /* takes the slot of the 1 MB size, which went cold */
        ARM("a size that takes the slot of one that was put off is not put off itself",
            !nvrm_vp_find(&p, 1 * M) && nvrm_vp_find(&p, 5 * M)->nofill == 0 && nvrm_vp_next_fill(&p, t + 2 * SEC, BIG, &gen) == 5 * M);
    }
    /* ---- take, the worker's order ---- */
    {
        nvrm_vp p; memset(&p, 0, sizeof p);
        const unsigned long long t = 10 * SEC;
        nvrm_vp_block b0 = blk(0, SIZE_A, 0), b1 = blk(1, SIZE_A, 0), b2 = blk(2, SIZE_A, 0), b3 = blk(3, SIZE_A, 0), out;
        ARM("a block of a size that is not followed is not stored", !nvrm_vp_store(&p, &b0, t, BUDGET));
        churn(&p, SIZE_A, t, 3, 1);
        ARM("nor one of a size that is not in churn yet", !nvrm_vp_store(&p, &b0, t, BUDGET));
        churn(&p, SIZE_A, t, 0, 1);
        ARM("nothing ready, nothing taken", !nvrm_vp_take(&p, SIZE_A, &out));
        ARM("two blocks of 9 MB fit under a 20 MB ceiling", nvrm_vp_store(&p, &b0, t, BUDGET) && nvrm_vp_store(&p, &b1, t, BUDGET));
        ARM("the third does not", !nvrm_vp_store(&p, &b2, t, BUDGET) && p.ready_bytes == 2 * SIZE_A);
        unsigned long long gen = 99;
        ARM("and the worker does not ask for it", nvrm_vp_next_fill(&p, t, BUDGET, &gen) == 0 && gen == 0);
        ARM("under a larger ceiling it asks for the third", nvrm_vp_next_fill(&p, t, BIG, &gen) == SIZE_A);
        ARM("three is the most for one size", nvrm_vp_store(&p, &b2, t, BIG) && nvrm_vp_next_fill(&p, t, BIG, &gen) == 0);
        ARM("a fourth is refused even when another worker made it (the ceiling has room)", !nvrm_vp_store(&p, &b3, t, BIG) &&
            nvrm_vp_find(&p, SIZE_A)->n == 3 && p.ready_bytes == 3 * SIZE_A);
        ARM("a grant of another size takes nothing", !nvrm_vp_take(&p, SIZE_B, &out));
        ARM("the oldest ready block is taken first", nvrm_vp_take(&p, SIZE_A, &out) && out.cookie == 1 && p.ready_bytes == 2 * SIZE_A);
        nvrm_vp_power_event(&p);
        ARM("after a power event the blocks made before it are not handed out", !nvrm_vp_take(&p, SIZE_A, &out) && nvrm_vp_held(&p) == 2);
        ARM("a block granted before the event is not stored after it", !nvrm_vp_store(&p, &b0, t, BIG));
        ARM("the worker gives back the two made before the power event", nvrm_vp_next_release(&p, t, 0, &out) == NVRM_VP_WHY_STALE && out.cookie == 2 &&
            nvrm_vp_next_release(&p, t, 0, &out) == NVRM_VP_WHY_STALE && out.cookie == 3);
        ARM("and then nothing: the pool is empty", !nvrm_vp_next_release(&p, t, 0, &out) && !nvrm_vp_held(&p) && !p.ready_bytes);
        nvrm_vp_block n0 = blk(10, SIZE_A, p.gen);
        ARM("(a block of the new generation is stored)", nvrm_vp_store(&p, &n0, t, BUDGET));
        ARM("while the size is in churn the worker keeps it", !nvrm_vp_next_release(&p, t + SEC, 0, &out));
        ARM("a second after the last grant it gives it back", nvrm_vp_next_release(&p, t + SEC + 1, 0, &out) == NVRM_VP_WHY_IDLE && out.cookie == 11);
        ARM("(stored again)", nvrm_vp_store(&p, &n0, t, BUDGET));
        ARM("when everything goes, the blocks of a size in churn go too", nvrm_vp_next_release(&p, t, 1, &out) == NVRM_VP_WHY_IDLE);
        {
            nvrm_vp_block old = blk(30, SIZE_A, p.gen), young = blk(31, SIZE_A, p.gen + 1);
            ARM("(a block, a power event, a block of the new generation behind it)", nvrm_vp_store(&p, &old, t, BIG) &&
                (nvrm_vp_power_event(&p), nvrm_vp_store(&p, &young, t, BIG)) && nvrm_vp_find(&p, SIZE_A)->n == 2);
            ARM("a grant takes the new one from behind the old one", nvrm_vp_take(&p, SIZE_A, &out) && out.cookie == 32 &&
                nvrm_vp_find(&p, SIZE_A)->n == 1 && nvrm_vp_find(&p, SIZE_A)->ready[0].cookie == 31);
            ARM("and the old one goes back", nvrm_vp_release_due(&p, t, 0) == NVRM_VP_WHY_STALE &&
                nvrm_vp_next_release(&p, t, 0, &out) == NVRM_VP_WHY_STALE && out.cookie == 31 && !nvrm_vp_held(&p));
        }
        nvrm_vp_block bad = blk(20, SIZE_A, p.gen);
        bad.cookie = 0;
        ARM("a block without a cookie is not stored", !nvrm_vp_store(&p, &bad, t, BUDGET));
        bad = blk(20, SIZE_A, p.gen); bad.handle = 0;
        ARM("nor one without a handle", !nvrm_vp_store(&p, &bad, t, BUDGET));
        bad = blk(20, SIZE_A, p.gen); bad.kva = 0;
        ARM("nor one without a mapping", !nvrm_vp_store(&p, &bad, t, BUDGET) && !nvrm_vp_held(&p));
    }
    /* ---- the worker's choice of a size, a refusal, a flush ---- */
    {
        nvrm_vp p; memset(&p, 0, sizeof p);
        const unsigned long long t = 10 * SEC;
        unsigned long long gen = 0;
        churn(&p, SIZE_A, t, 3, 2);
        churn(&p, SIZE_B, t + 1, 3, 2);
        ARM("with nothing ready the size granted last is made first", nvrm_vp_next_fill(&p, t + 2, BIG, &gen) == SIZE_B);
        nvrm_vp_block b = blk(0, SIZE_B, 0);
        nvrm_vp_store(&p, &b, t + 2, BIG);
        ARM("then the size with fewer ready blocks", nvrm_vp_next_fill(&p, t + 2, BIG, &gen) == SIZE_A);
        nvrm_vp_refused(&p, SIZE_A, t + 2);
        ARM("a refused size is left alone, the other is made", nvrm_vp_next_fill(&p, t + 3, BIG, &gen) == SIZE_B);
        churn(&p, SIZE_A, t + SEC, 3, 2);
        ARM("still left alone a second later, to the nanosecond", nvrm_vp_find(&p, SIZE_A)->nofill == t + 2 + SEC &&
            (nvrm_vp_note(&p, SIZE_A, t + SEC + 1), nvrm_vp_find(&p, SIZE_B)->n = NVRM_VP_DEPTH, nvrm_vp_next_fill(&p, t + SEC + 1, BIG, &gen) == 0));
        ARM("and asked for again after that", nvrm_vp_next_fill(&p, t + 2 + SEC, BIG, &gen) == SIZE_A);
        ARM("a size that went cold is not made", nvrm_vp_next_fill(&p, t + 5 * SEC, BIG, &gen) == 0);
        {
            nvrm_vp q; memset(&q, 0, sizeof q);
            unsigned long long g2 = 0;
            nvrm_vp_block e0 = blk(0, CAP / 2, 0), e1 = blk(1, CAP / 2, 0), e2 = blk(2, CAP / 2, 0);
            churn(&q, CAP / 2, t, 3, 2);
            ARM("two blocks that fill the ceiling to the byte are both made and kept", nvrm_vp_next_fill(&q, t, BUDGET, &g2) == CAP / 2 &&
                nvrm_vp_store(&q, &e0, t, BUDGET) && nvrm_vp_next_fill(&q, t, BUDGET, &g2) == CAP / 2 && nvrm_vp_store(&q, &e1, t, BUDGET) &&
                q.ready_bytes == CAP);
            ARM("and a third is neither asked for nor stored", nvrm_vp_next_fill(&q, t, BUDGET, &g2) == 0 && !nvrm_vp_store(&q, &e2, t, BUDGET));
        }
        ARM("a size larger than an eighth of the budget is never made",
            (memset(&p, 0, sizeof p), churn(&p, 32 * M, t, 3, 2), nvrm_vp_hot(&p, 32 * M, t)) && nvrm_vp_next_fill(&p, t, BUDGET, &gen) == 0);
        {
            nvrm_vp_block big = blk(0, 32 * M, 0);
            ARM("and a block of it that was made all the same is not stored", !nvrm_vp_store(&p, &big, t, BUDGET) && !nvrm_vp_held(&p) && !p.ready_bytes);
        }
        memset(&p, 0, sizeof p);
        churn(&p, 40 * M, t, 3, 2);
        nvrm_vp_block h0 = blk(0, 40 * M, 0), h1 = blk(1, 40 * M, 0), h2 = blk(2, 40 * M, 0);
        ARM("two blocks of a 40 MB size are kept on the measured card, not a third",
            nvrm_vp_store(&p, &h0, t, BIG) && nvrm_vp_store(&p, &h1, t, BIG) && !nvrm_vp_store(&p, &h2, t, BIG) &&
            nvrm_vp_next_fill(&p, t, BIG, &gen) == 0);
        memset(&p, 0, sizeof p);
        churn(&p, SIZE_A, t, 3, 2); churn(&p, SIZE_B, t, 3, 2);
        nvrm_vp_refused_all(&p, t);
        ARM("after a flush no size is made for a second", nvrm_vp_next_fill(&p, t, BIG, &gen) == 0 &&
            (churn(&p, SIZE_A, t + SEC - 1, 3, 2), nvrm_vp_next_fill(&p, t + SEC - 1, BIG, &gen) == 0));
        ARM("not even a size that was not followed when it happened",
            (churn(&p, 3 * M, t + 1, 3, 2), nvrm_vp_hot(&p, 3 * M, t + 1)) && nvrm_vp_next_fill(&p, t + 1, BIG, &gen) == 0);
        ARM("then they are made again", (churn(&p, SIZE_A, t + SEC, 3, 2), nvrm_vp_next_fill(&p, t + SEC, BIG, &gen) != 0));
        /* the worker chose a size, made the block and filled it without the pool's lock: a flush can come in between */
        memset(&p, 0, sizeof p);
        churn(&p, SIZE_A, t, 3, 2);
        nvrm_vp_block late = blk(0, SIZE_A, 0);
        nvrm_vp_refused_all(&p, t);
        ARM("a block that was being made when everything went back is not stored", !nvrm_vp_store(&p, &late, t, BIG) &&
            !nvrm_vp_store(&p, &late, t + SEC - 1, BIG) && !nvrm_vp_held(&p));
        ARM("a second later one is", (churn(&p, SIZE_A, t + SEC, 3, 2), nvrm_vp_store(&p, &late, t + SEC, BIG)));
        memset(&p, 0, sizeof p);
        churn(&p, SIZE_A, t, 3, 2);
        nvrm_vp_refused(&p, SIZE_A, t);
        ARM("nor one of a size that was put off meanwhile", !nvrm_vp_store(&p, &late, t + 1, BIG) && !nvrm_vp_held(&p));
    }
    /* ---- blocks that wait to go back ---- */
    {
        nvrm_vp p; memset(&p, 0, sizeof p);
        const unsigned long long t = 10 * SEC;
        unsigned long long gen = 0;
        nvrm_vp_block out, ready = blk(0, SIZE_A, 0), w0 = blk(1, SIZE_A, 0), none = blk(2, SIZE_A, 0);
        churn(&p, SIZE_A, t, 3, 2);
        ARM("nothing ready, nothing waits: nothing is due", !nvrm_vp_release_due(&p, t, 0) && !nvrm_vp_release_due(&p, t, 1));
        nvrm_vp_store(&p, &ready, t, BIG);
        ARM("a ready block of a size in churn is not due, unless everything goes", !nvrm_vp_release_due(&p, t, 0) &&
            nvrm_vp_release_due(&p, t, 1) == NVRM_VP_WHY_IDLE && nvrm_vp_release_due(&p, t + SEC + 1, 0) == NVRM_VP_WHY_IDLE);
        ARM("asking what is due takes nothing out", nvrm_vp_held(&p) == 1 && p.ready_bytes == SIZE_A);
        ARM("a block the worker could not give back waits in the pool", nvrm_vp_park(&p, &w0) && p.nleft == 1 && nvrm_vp_held(&p) == 2);
        ARM("it is not a ready block", p.ready_bytes == SIZE_A && nvrm_vp_find(&p, SIZE_A)->n == 1);
        ARM("a grant does not get it", nvrm_vp_take(&p, SIZE_A, &out) && out.cookie == 1 && !nvrm_vp_take(&p, SIZE_A, &out) && p.nleft == 1);
        ARM("while it waits no block is made", nvrm_vp_next_fill(&p, t, BIG, &gen) == 0);
        nvrm_vp_store(&p, &ready, t, BIG);
        nvrm_vp_power_event(&p);
        ARM("it goes back first, before the ready blocks made before a power event", nvrm_vp_release_due(&p, t, 0) == NVRM_VP_WHY_LEFT &&
            nvrm_vp_next_release(&p, t, 0, &out) == NVRM_VP_WHY_LEFT && out.cookie == 2 && p.nleft == 0 &&
            nvrm_vp_next_release(&p, t, 0, &out) == NVRM_VP_WHY_STALE && out.cookie == 1 && !nvrm_vp_held(&p));
        ARM("then blocks are made again", nvrm_vp_next_fill(&p, t, BIG, &gen) == SIZE_A && gen == 1);
        none.handle = 0;
        ARM("a block without a handle does not wait", !nvrm_vp_park(&p, &none) && p.nleft == 0);
        int places = 1;
        for (unsigned i = 0; i < NVRM_VP_LEFT; i++) { nvrm_vp_block b = blk(10 + i, SIZE_A, 0); places = places && nvrm_vp_park(&p, &b); }
        nvrm_vp_block extra = blk(30, SIZE_A, 0);
        ARM("as many can wait as a card has heads (8), one more has no place", NVRM_VP_LEFT == 8 && places && !nvrm_vp_park(&p, &extra) &&
            p.nleft == NVRM_VP_LEFT);
        int order = 1;
        for (unsigned i = 0; i < NVRM_VP_LEFT; i++) order = order && nvrm_vp_next_release(&p, t, 1, &out) == NVRM_VP_WHY_LEFT && out.cookie == 11 + i;
        ARM("they go back in the order they came", order && !nvrm_vp_held(&p) && !nvrm_vp_next_release(&p, t, 1, &out));
    }
    /* ---- the measured pattern: one surface per captured frame ---- */
    {
        world w = play("ARARA");                 /* frames 1-3: made on the spot */
        ARM("the first three frames are served as before", w.nb == 3 && !w.broke && nvrm_vp_held(&w.pool) == 0);
        w = play("ARARAW");
        ARM("then the worker makes a block, which is not ready while it is in its hand", !w.broke && w.nb == 4 && w.where[3] == HAND && !nvrm_vp_held(&w.pool));
        w = play("ARARAWWWW");
        ARM("two steps a block: two are ready (two fit the test's ceiling)", w.pool.s[0].n == 2 && !w.broke && w.nb == 5);
        w = play("ARARAWWWWRAWWRAWWRAWW");
        ARM("from the fourth frame on every grant is a hit", !w.broke && w.nb == 3 + 2 + 3 && w.nheld == 1);
        w = play("ARARAWWWWRAWWRTWW");
        ARM("a second after the share ends everything went back", !w.broke && nvrm_vp_held(&w.pool) == 0 && w.pool.ready_bytes == 0);
        w = play("AAAAW");
        ARM("four surfaces that stay: nothing is made ahead of time", !w.broke && w.nb == 4 && nvrm_vp_held(&w.pool) == 0);
        w = play("ARARAWWWWZUA");
        ARM("after a sleep the next grant is made on the spot", !w.broke && w.nb == 6 && w.pool.s[0].n == 2);
        w = play("ARARAWWWWZUAWWWWWW");
        ARM("and the worker replaces the blocks made before it", !w.broke && w.pool.s[0].n == 2 && w.pool.s[0].ready[0].gen == 2 && w.where[3] == GONE && w.where[4] == GONE);
        w = play("ARARAWWWWZWVAWV");
        ARM("with the GPU in standby the workers do nothing, and a grant is made on the spot", !w.broke && w.nb == 6 && w.pool.s[0].n == 2 && !w.pool.nleft);
        w = play("ARARAWZUW");
        ARM("a block made across a sleep is given back, not kept", !w.broke && w.where[3] == GONE && w.releases[3] == 1 && !nvrm_vp_held(&w.pool));
        w = play("ARARAWZW");
        ARM("a block in the worker's hand when the standby begins waits in the pool", !w.broke && w.where[3] == WAITS && w.pool.nleft == 1 && !w.releases[3]);
        w = play("ARARAWZWUA");
        ARM("no grant gets it", !w.broke && w.where[3] == WAITS && w.nb == 5);
        w = play("ARARAWZWUAW");
        ARM("and it goes back first thing after the wake", !w.broke && w.where[3] == GONE && w.releases[3] == 1 && !w.pool.nleft);
        w = play("ARARAWWWWRAD");
        ARM("a stop gives everything back", !w.broke && nvrm_vp_held(&w.pool) == 0);
        w = play("ARARAWWWWAWD");
        ARM("with what the worker had in hand", !w.broke && nvrm_vp_held(&w.pool) == 0 && w.where[5] == GONE && w.releases[5] == 1);
        w = play("ARARAWWWWZD");
        ARM("a stop with the GPU in standby releases nothing", !w.broke && nvrm_vp_held(&w.pool) == 2 && !w.releases[3] && !w.releases[4]);
        w = play("ARARAWWWWZDUVV");
        ARM("the other framebuffer's worker gives the blocks back after the wake", !w.broke && nvrm_vp_held(&w.pool) == 0 && w.releases[3] == 1 && w.releases[4] == 1);
        w = play("ARARAWWWWFW");
        ARM("a grant that found no room takes everything back, and the worker waits", !w.broke && nvrm_vp_held(&w.pool) == 0 && w.nb == 5);
        w = play("ARARAWWWWAWFW");
        ARM("a block that was being made meanwhile goes back too", !w.broke && nvrm_vp_held(&w.pool) == 0 && w.where[5] == GONE && w.releases[5] == 1);
        w = play("ARARAWWWWFTARARAWWWW");
        ARM("a second later it makes blocks again", !w.broke && w.pool.s[0].n == 2);
        w = play("ARARAWVWV");
        ARM("two workers that chose the same size both keep their block while there is room", !w.broke && w.nb == 5 && w.pool.s[0].n == 2);
        w = play("ARARAWWWVWV");
        ARM("when both made one for the last place under the ceiling, the second goes back", !w.broke && w.nb == 6 && w.pool.s[0].n == 2 &&
            w.where[5] == GONE && w.releases[5] == 1);
    }

    /* ---- every order of events: from nothing, and from a share that is running ---- */
    static const char *const leads[] = { "", "ARARAWWWW" };
    for (unsigned l = 0; l < sizeof leads / sizeof leads[0]; l++) {
        const char *why = run_model(&kHeader, leads[l]);
        printf("     %llu orders tried, depth %d after \"%s\"\n", tried, DEPTH, leads[l]);
        ARM(l ? "the header's rules hold in every order after a share began" : "the header's rules hold in every order", !why);
        if (why) printf("     first: %s after \"%s\"\n", why, firstBad);
    }

    static const struct { const char *name; rules r; const char *expect; } controls[] = {
        { "control: a take that does not look at the generation is caught",
          { take_any_generation, nvrm_vp_store, nvrm_vp_next_release, nvrm_vp_next_fill, nvrm_vp_refused_all }, "a block made before a power event was handed out after it" },
        { "control: a take that leaves the block ready is caught",
          { take_and_leave, nvrm_vp_store, nvrm_vp_next_release, nvrm_vp_next_fill, nvrm_vp_refused_all }, "a block is in two places" },
        { "control: a pool that never looks at the ceiling is caught",
          { nvrm_vp_take, store_no_ceiling, nvrm_vp_next_release, nvrm_vp_next_fill, nvrm_vp_refused_all }, "the ready blocks exceed the ceiling" },
        { "control: a worker that asks for a block without looking at the ceiling is caught",
          { nvrm_vp_take, nvrm_vp_store, nvrm_vp_next_release, next_fill_no_ceiling, nvrm_vp_refused_all }, "the worker made a block the ceiling has no room for" },
        { "control: a store that stamps the block with the current generation is caught",
          { nvrm_vp_take, store_restamps, nvrm_vp_next_release, nvrm_vp_next_fill, nvrm_vp_refused_all }, "a block made before a power event was handed out after it" },
        { "control: a worker that counts grants alone is caught",
          { nvrm_vp_take, nvrm_vp_store, nvrm_vp_next_release, next_fill_grants_only, nvrm_vp_refused_all }, "the worker made a block of a size that is not handed back" },
        { "control: a flush that does not put the worker off is caught",
          { nvrm_vp_take, nvrm_vp_store, nvrm_vp_next_release, nvrm_vp_next_fill, flushed_and_forgotten }, "the worker made a block within a second of a flush" },
        { "control: a store that does not look at the flush is caught",
          { nvrm_vp_take, store_ignores_flush, nvrm_vp_next_release, nvrm_vp_next_fill, nvrm_vp_refused_all }, "a block became ready within a second of a flush" },
        { "control: a worker that forgets blocks made before a power event is caught",
          { nvrm_vp_take, nvrm_vp_store, next_release_forgets_stale, nvrm_vp_next_fill, nvrm_vp_refused_all }, "a worker with nothing to do left a block made before a power event" },
        { "control: a worker that forgets the blocks that wait to go back is caught",
          { nvrm_vp_take, nvrm_vp_store, next_release_forgets_waiting, nvrm_vp_next_fill, nvrm_vp_refused_all }, "a worker with nothing to do left a block that waits to go back" },
    };
    for (unsigned i = 0; i < sizeof controls / sizeof controls[0]; i++) {
        const char *got = run_model(&controls[i].r, "ARARAWWWW");
        if (!got) got = run_model(&controls[i].r, "");
        ARM(controls[i].name, got && !strcmp(got, controls[i].expect));
        if (!got || strcmp(got, controls[i].expect)) printf("     got: %s (after \"%s\")\n", got ? got : "(nothing broke)", firstBad);
    }

    printf(fails ? "\n%d FAILED\n" : "\nall passed\n", fails);
    return fails ? 1 : 0;
}
