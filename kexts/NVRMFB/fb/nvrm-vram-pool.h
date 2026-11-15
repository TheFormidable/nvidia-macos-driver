/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

/* nvrm-vram-pool.h - blocks of VRAM of a size that is asked for and handed back again and again are made ahead of
   time, away from the thread that asks; and when the fill with zeros may be left out.

   WAS: every grant ran on the caller's thread: wait for the display gate, ask NVKMS for memory, map it, fill it with
   zeros from the processor through BAR1 (about 330 MB/s). A screen share makes WindowServer create one 9 MB surface
   per captured frame and destroy it in the same frame: 27 ms of fill and 6 ms of gate (a flip holds it while it waits
   for its latch) on WindowServer's main thread, every frame. Measured on an RTX 5050: 100 frames a second without the
   share, 20 with it, and the game behind it at twice that.

   NOW, two things, both in vramGrant (nvrm-fb.cpp):
   - The fill. RM clears video memory when it is freed and refuses to allocate while its scrubber is not valid, and it
     says so (NV0080_CTRL_FB_CAPS_VIDMEM_ALLOCS_ARE_CLEARED). While that is believed (nvrm_vp_trusted) the framebuffer
     does not fill a new block a second time; it reads NVRM_VP_PROBES places of it and its end instead. The word is
     believed only until the first of: a block that does not read as zero there (every block is filled again until
     restart), or a standby of the GPU - a system sleep, or an idle power-down where boot argument nvrtd3=1 allows
     them (the slot loses power, RM brings back what was allocated and nothing in its sources clears the rest: from
     then on every block is filled again, and what the probes read before the fill is counted, so that a card can
     show whether its memory comes back clean).
   - The gate. A size is in churn when it was granted NVRM_VP_HOT times in a row and handed back NVRM_VP_BACK times in
     a row, each within NVRM_VP_WINDOW_NS of the one before. For such a size a worker keeps up to NVRM_VP_DEPTH blocks
     ready; a grant takes one without the display gate. Every block is still a new NVKMS allocation with its own
     cookie, made by the same code with the gate held, and a block that must be filled is filled by the worker, after
     it let go of the gate. A block is never given to a second surface, and releases are not changed at all.
     The worker calls NVKMS only while the GPU is up: a block it holds when a standby begins waits here, apart from
     the ready ones (nvrm_vp_park), and goes back first thing after the wake.

   This file is the bookkeeping only, without a lock and without a kernel call, so tools/test_vram_pool.c can run it.
   The caller holds the pool's lock around every call and does the real grants and releases itself. */
#ifndef NVRM_VRAM_POOL_H
#define NVRM_VRAM_POOL_H

#define NVRM_VP_SIZES       4u                   /* sizes followed at one time */
#define NVRM_VP_DEPTH       3u                   /* blocks kept ready for one size in churn */
#define NVRM_VP_HOT         3u                   /* grants in a row ... */
#define NVRM_VP_BACK        2u                   /* ... and releases in a row that make a size one in churn */
#define NVRM_VP_WINDOW_NS   1000000000ull        /* "in a row": no more than this apart; also how long the churn lasts */
#define NVRM_VP_MAX_BYTES   (64ull << 20)        /* ready blocks, all sizes together (nvrm_vp_cap) */
#define NVRM_VP_PROBES      64u                  /* places read in a new block, and its last 8 bytes */
#define NVRM_VP_LEFT        8u                   /* blocks that wait to go back (nvrm_vp_park): one per head of a card */

#define NVRM_VP_WHY_STALE   1                    /* made before the last power event */
#define NVRM_VP_WHY_IDLE    2                    /* its size is no longer in churn, or everything goes */
#define NVRM_VP_WHY_LEFT    3                    /* it could not go back when the worker had it in hand */

typedef struct {
    void *handle, *kva;                          /* NVRMVramRequest.handle, .kva */
    unsigned long long phys, bytes, cookie;      /* .phys, .actualSize, .allocationCookie */
    unsigned long long gen;                      /* the power generation it was made in */
} nvrm_vp_block;

typedef struct {
    unsigned long long bytes;                    /* 0: the slot is free */
    unsigned long long last, lastBack;           /* when this size was granted / handed back last, ns */
    unsigned long long nofill;                   /* no block is made for it before this time */
    unsigned heat, back;                         /* grants in a row, releases in a row */
    unsigned n;                                  /* ready blocks, oldest first */
    nvrm_vp_block ready[NVRM_VP_DEPTH];
} nvrm_vp_size;

typedef struct {
    nvrm_vp_size s[NVRM_VP_SIZES];
    nvrm_vp_block left[NVRM_VP_LEFT];            /* made, not ready, never handed out: they only wait to go back */
    unsigned nleft;
    unsigned long long gen;                      /* rises with every power event, and with the first dirty block */
    unsigned long long ready_bytes;              /* the ready blocks of every size, together */
    unsigned long long nofill;                   /* no block of any size is made before this time (nvrm_vp_refused_all) */
} nvrm_vp;

/* 1 while a new block may go without the fill from the processor: RM said it clears video memory (`rmClears`), the
   GPU has not been through a standby since the system started (`powerEvents` messages from NVRM so far), and no block
   read as not zero (`dirty`). */
static inline int nvrm_vp_trusted(int rmClears, long long powerEvents, long long dirty)
{
    return rmClears && !powerEvents && !dirty;
}
/* Which 8 bytes of a new block of `bytes` the framebuffer reads as probe `i` (0 .. NVRM_VP_PROBES; the last one is the
   block's end): an index in units of 8 bytes. The places are spread evenly, each a little further into its stretch
   than the one before, so that they do not all fall on one column of an image whose rows divide the block. The first
   one is the block's second 8 bytes, not its first: those were just written by the test that the mapping is live
   (they are zero whatever the RM left there, and reading them back would prove nothing). The probes are a tripwire
   for a card or a firmware that does not keep RM's word; they prove nothing about the bytes between. */
static inline unsigned long long nvrm_vp_probe_at(unsigned long long bytes, unsigned i)
{
    const unsigned long long q = bytes >> 3, stride = q / NVRM_VP_PROBES;
    if (!q) return 0;
    if (i >= NVRM_VP_PROBES || !stride) return q - 1;
    if (!i) return stride > 1 ? 1 : 0;
    return stride * i + ((unsigned long long)i * 1021ull) % stride;
}
/* The ceiling for the ready blocks of all sizes together when one of `bytes` is added, on a framebuffer whose surfaces
   may map `budget` bytes of BAR1: 64 MB, or room for two of this size when that is more (one ready block of a 4K
   surface would make every late run of the worker a wait for the gate), and never more than an eighth of the budget. */
static inline unsigned long long nvrm_vp_cap(unsigned long long budget, unsigned long long bytes)
{
    const unsigned long long eighth = budget / 8;
    unsigned long long c = NVRM_VP_MAX_BYTES;
    if (bytes > c / 2) c = bytes > eighth / 2 ? eighth : 2 * bytes;
    return c < eighth ? c : eighth;
}

static inline int nvrm_vp_within(unsigned long long now, unsigned long long then)
{
    return now <= then || now - then <= NVRM_VP_WINDOW_NS;
}
static inline nvrm_vp_size *nvrm_vp_find(nvrm_vp *p, unsigned long long bytes)
{
    if (!bytes) return 0;
    for (unsigned i = 0; i < NVRM_VP_SIZES; i++) if (p->s[i].bytes == bytes) return &p->s[i];
    return 0;
}
/* In churn: asked for again and again AND handed back again and again. Grants alone (an application that opens, a
   window that is made larger) are surfaces that stay; nothing is made ahead of time for them. */
static inline int nvrm_vp_is_hot(const nvrm_vp_size *s, unsigned long long now)
{
    return s->bytes && s->heat >= NVRM_VP_HOT && nvrm_vp_within(now, s->last) &&
           s->back >= NVRM_VP_BACK && nvrm_vp_within(now, s->lastBack);
}
/* 1 when `bytes` is a size in churn at `now`. */
static inline int nvrm_vp_hot(nvrm_vp *p, unsigned long long bytes, unsigned long long now)
{
    const nvrm_vp_size *s = nvrm_vp_find(p, bytes);
    return s && nvrm_vp_is_hot(s, now);
}
static inline void nvrm_vp_pop(nvrm_vp *p, nvrm_vp_size *s, unsigned i, nvrm_vp_block *out)
{
    *out = s->ready[i];
    for (unsigned k = i + 1; k < s->n; k++) s->ready[k - 1] = s->ready[k];
    s->n--;
    s->ready[s->n].handle = 0; s->ready[s->n].kva = 0;
    s->ready[s->n].phys = s->ready[s->n].bytes = s->ready[s->n].cookie = s->ready[s->n].gen = 0;
    p->ready_bytes -= out->bytes;
}

/* A grant of `bytes` arrives at `now`. 1 when the size is in churn with it. A size not followed yet takes a free
   slot, or the slot of the size that was granted longest ago among those that went cold and hold no ready block; when
   there is none, the size is not followed (0). */
static inline int nvrm_vp_note(nvrm_vp *p, unsigned long long bytes, unsigned long long now)
{
    nvrm_vp_size *s = nvrm_vp_find(p, bytes);
    if (!bytes) return 0;
    if (!s) {
        for (unsigned i = 0; i < NVRM_VP_SIZES && !s; i++) if (!p->s[i].bytes) s = &p->s[i];
        if (!s)
            for (unsigned i = 0; i < NVRM_VP_SIZES; i++) {
                nvrm_vp_size *c = &p->s[i];
                if (!c->n && !nvrm_vp_within(now, c->last) && (!s || c->last < s->last)) s = c;
            }
        if (!s) return 0;
        s->bytes = bytes; s->heat = 0; s->back = 0; s->lastBack = 0; s->nofill = 0; s->last = now;
    }
    if (s->heat && nvrm_vp_within(now, s->last)) { if (s->heat < 0xffffu) s->heat++; }
    else s->heat = 1;
    s->last = now;
    return nvrm_vp_is_hot(s, now);
}
/* The accelerator handed a block of `bytes` back at `now` (the caller released it, as always). Only a size that is
   followed already counts it: a release never takes a slot. */
static inline void nvrm_vp_note_back(nvrm_vp *p, unsigned long long bytes, unsigned long long now)
{
    nvrm_vp_size *s = nvrm_vp_find(p, bytes);
    if (!s) return;
    if (s->back && nvrm_vp_within(now, s->lastBack)) { if (s->back < 0xffffu) s->back++; }
    else s->back = 1;
    s->lastBack = now;
}
/* The oldest ready block of exactly `bytes` that was made in this power generation, removed (1); else 0. */
static inline int nvrm_vp_take(nvrm_vp *p, unsigned long long bytes, nvrm_vp_block *out)
{
    nvrm_vp_size *s = nvrm_vp_find(p, bytes);
    if (!s) return 0;
    for (unsigned i = 0; i < s->n; i++)
        if (s->ready[i].gen == p->gen) { nvrm_vp_pop(p, s, i, out); return 1; }
    return 0;
}
/* Which block goes back to the framebuffer next: 0 when none, else why (NVRM_VP_WHY_*) and, for a ready block, its
   size slot and its place there. The blocks that wait to go back come first (nvrm_vp_park); then the ready blocks
   made before the last power event; then those of a size that is not in churn any more (`all`: of every size). */
static inline int nvrm_vp_release_find(const nvrm_vp *p, unsigned long long now, int all, unsigned *slot, unsigned *at)
{
    if (p->nleft) return NVRM_VP_WHY_LEFT;
    for (unsigned i = 0; i < NVRM_VP_SIZES; i++)
        for (unsigned k = 0; k < p->s[i].n; k++)
            if (p->s[i].ready[k].gen != p->gen) { *slot = i; *at = k; return NVRM_VP_WHY_STALE; }
    for (unsigned i = 0; i < NVRM_VP_SIZES; i++)
        if (p->s[i].n && (all || !nvrm_vp_is_hot(&p->s[i], now))) { *slot = i; *at = 0; return NVRM_VP_WHY_IDLE; }
    return 0;
}
/* Is there a block to give back? The worker asks this first and takes the block out (nvrm_vp_next_release) only once
   it holds the display gate and knows the GPU is up, so that a block it cannot release stays where it is. */
static inline int nvrm_vp_release_due(const nvrm_vp *p, unsigned long long now, int all)
{
    unsigned slot = 0, at = 0;
    return nvrm_vp_release_find(p, now, all, &slot, &at);
}
/* The next block to give back to the framebuffer, removed: 0 when there is none, else why. */
static inline int nvrm_vp_next_release(nvrm_vp *p, unsigned long long now, int all, nvrm_vp_block *out)
{
    unsigned slot = 0, at = 0;
    const int why = nvrm_vp_release_find(p, now, all, &slot, &at);
    if (why == NVRM_VP_WHY_LEFT) {
        *out = p->left[0];
        for (unsigned k = 1; k < p->nleft; k++) p->left[k - 1] = p->left[k];
        p->nleft--;
        p->left[p->nleft].handle = 0; p->left[p->nleft].kva = 0;
        p->left[p->nleft].phys = p->left[p->nleft].bytes = p->left[p->nleft].cookie = p->left[p->nleft].gen = 0;
    } else if (why) {
        nvrm_vp_pop(p, &p->s[slot], at, out);
    }
    return why;
}
/* The worker has *b in hand, the pool does not want it (nvrm_vp_store said 0) and it cannot go back now, because the
   GPU went to standby meanwhile: it waits here and goes back before anything else is done (nvrm_vp_next_release). It
   is never handed out. 0: no place left. A worker has at most one block in hand and none is made while one waits
   (nvrm_vp_next_fill). The workers that run are the one of the framebuffer that served the accelerator and, after a
   wake, at most one more for blocks a stopped framebuffer left: never more than one per framebuffer, and the places
   are as many as a card can have heads. */
static inline int nvrm_vp_park(nvrm_vp *p, const nvrm_vp_block *b)
{
    if (!b->handle || p->nleft >= NVRM_VP_LEFT) return 0;
    p->left[p->nleft++] = *b;
    return 1;
}
/* The size the worker makes a block for next, 0 when none: a size in churn with fewer than NVRM_VP_DEPTH ready
   blocks, not put off (nvrm_vp_refused), that fits under the ceiling for `budget`; the one with the fewest ready
   blocks first. None while a block waits to go back. *gen is the power generation the new block belongs to
   (nvrm_vp_store refuses it when that has moved on). */
static inline unsigned long long nvrm_vp_next_fill(const nvrm_vp *p, unsigned long long now, unsigned long long budget,
                                                   unsigned long long *gen)
{
    const nvrm_vp_size *best = 0;
    *gen = p->gen;
    if (now < p->nofill || p->nleft) return 0;
    for (unsigned i = 0; i < NVRM_VP_SIZES; i++) {
        const nvrm_vp_size *s = &p->s[i];
        const unsigned long long cap = nvrm_vp_cap(budget, s->bytes);
        if (!nvrm_vp_is_hot(s, now) || s->n >= NVRM_VP_DEPTH || now < s->nofill) continue;
        if (s->bytes > cap || p->ready_bytes > cap - s->bytes) continue;
        if (!best || s->n < best->n || (s->n == best->n && s->last > best->last)) best = s;
    }
    return best ? best->bytes : 0;
}
/* The worker made *b (b->gen: what nvrm_vp_next_fill said). 1: it is ready now; 0: it is not wanted any more (the size
   is no longer in churn or is full, the ceiling was reached, a power event came in between, or everything went back
   for want of room while it was made) and the caller releases it. The worker of every framebuffer of the card stores
   here, and other events come between its choice of a size and this call (the block is made and filled without the
   pool's lock), so nothing that nvrm_vp_next_fill looked at is taken for granted. */
static inline int nvrm_vp_store(nvrm_vp *p, const nvrm_vp_block *b, unsigned long long now, unsigned long long budget)
{
    nvrm_vp_size *s = nvrm_vp_find(p, b->bytes);
    const unsigned long long cap = nvrm_vp_cap(budget, b->bytes);
    if (!s || !b->handle || !b->kva || !b->cookie || b->gen != p->gen) return 0;
    if (now < p->nofill || now < s->nofill) return 0;
    if (!nvrm_vp_is_hot(s, now) || s->n >= NVRM_VP_DEPTH) return 0;
    if (b->bytes > cap || p->ready_bytes > cap - b->bytes) return 0;
    s->ready[s->n++] = *b;
    p->ready_bytes += b->bytes;
    return 1;
}
/* The framebuffer refused a block of `bytes` to the worker at `now`: none is asked for again for a while. */
static inline void nvrm_vp_refused(nvrm_vp *p, unsigned long long bytes, unsigned long long now)
{
    nvrm_vp_size *s = nvrm_vp_find(p, bytes);
    if (s) s->nofill = now + NVRM_VP_WINDOW_NS;
}
/* A grant of the accelerator found no room and every ready block went back for it at `now`: no size is made ahead of
   time for a while (not one that comes into churn meanwhile either), or the worker and the accelerator would take
   the same room from each other again and again. */
static inline void nvrm_vp_refused_all(nvrm_vp *p, unsigned long long now) { p->nofill = now + NVRM_VP_WINDOW_NS; }
/* The GPU went to standby, came back or was lost, or the first block that the RM should have cleared read as not
   zero: no block made before this is handed out. */
static inline void nvrm_vp_power_event(nvrm_vp *p) { p->gen++; }
/* Ready blocks, of every size and generation, and the ones that wait to go back: what the worker still has to look
   at later (ready blocks go back when their size is no longer in churn). */
static inline unsigned nvrm_vp_held(const nvrm_vp *p)
{
    unsigned n = p->nleft;
    for (unsigned i = 0; i < NVRM_VP_SIZES; i++) n += p->s[i].n;
    return n;
}
#endif
