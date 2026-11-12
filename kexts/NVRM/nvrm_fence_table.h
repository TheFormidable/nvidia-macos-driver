/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

/* nvrm_fence_table.h - the surface fence as pure logic: the table of holds, channels and owners, the one function that
 * decides what a reader still has in front of it, and the wait loop. nvrm_fence.cpp is the kernel around it (the lock,
 * the clock, the sleep, the pages); tools/test_fence_table.c and tools/test_fence_model.c run this file as it is.
 *
 * A HOLD says "this connection is still drawing into this IOSurface". It is OPEN (no GPU target yet), ARMED (it ends,
 * or for an application becomes GPU_DONE, when its channel's completion value reaches a number) or GPU_DONE (the GPU
 * is finished, the application has not published yet). A reader waits for the holds that existed when it first looked
 * and for nothing opened later. Every hold is bounded: past its bound it is flagged, delays nobody any more, and its
 * owner gets a strike; three strikes and the owner is refused (its process keeps the older mechanism), eight such
 * owners since boot and the whole layer switches itself off.
 *
 * Rules of this file: plain C that is also C++17 without RTTI and exceptions; no include but the ABI header, nothing
 * from a system header; nothing allocates; every function is static inline; the caller holds the one lock around every
 * call except nvf_wait / nvf_wait_seq (which take it through their ops) and passes monotonic time in nanoseconds. */
#ifndef NVRM_FENCE_TABLE_H
#define NVRM_FENCE_TABLE_H

#include "nvrm_fence_abi.h"

#define NVF_ENTRIES         1024u
#define NVF_BUCKETS          256u    /* bucket = (sid * 2654435761u) >> 24 */
#define NVF_CHANS           1024u
#define NVF_OWNERS           256u
#define NVF_OWNER_ENTRIES    128u    /* one connection cannot fill the table */
#define NVF_OWNER_CHANS       32u    /* as many as NVK has channel slots */
#define NVF_PID_OWNERS         4u    /* connections of one process that may own holds */
#define NVF_CAP_GPU_MS      2000u    /* a hold whose completion depends on the GPU */
#define NVF_CAP_CPU_MS       100u    /* a hold whose completion depends only on its writer's CPU (2.9 abandoned a busy tag after 100 ms) */
#define NVF_CAP_PEN_MS        20u    /* any hold of an owner in a penalty window (2.9 waited 10 ms) */
#define NVF_PENALTY_MS     10000u    /* first penalty window; doubles with each strike */
#define NVF_STRIKES            3u    /* strikes that distrust an owner */
#define NVF_FORGIVE_MS    600000u    /* one strike is forgiven per 10 minutes without one */
#define NVF_FUSE_OWNERS        8u    /* owners distrusted since boot that switch the layer off */
#define NVF_SLICE_MIN_US    2000u
#define NVF_SLICE_MAX_US   32000u
#define NVF_SLICE_FAST_US    250u
#define NVF_TRUST_MIN          8u    /* completions seen on a channel before its interrupts are judged */
#define NVF_TRUST_HALVE       64u    /* the two trust counts are halved here, so the judgement follows the recent past */
#define NVF_RECENT            64u
#define NVF_EVENTS             8u    /* things worth a log line, kept until the caller has let go of the lock */
#define NVF_NIL           0xFFFFu
#define NVF_NS_PER_MS    1000000ull
#define NVF_PAGE            4096ull   /* the semaphore page and the notifier's page, as the ABI header lays them out */
#define NVF_USER_TOP  0x0000800000000000ull   /* no user address is at or above this on x86-64 */
#define NVF_CHAN_INDEX_BITS   12u    /* a channel handle is (generation << 12) | index, generation 1..0xFFFFF */
#define NVF_CHAN_GEN_MAX 0xFFFFFu
#define NVF_EF_REARMED      0x80u    /* nvf_entry.flags, internal: armed again after its GPU work had completed once */

enum { NVF_END_CLOSE = 1, NVF_END_GPU, NVF_END_OWNER, NVF_END_CHAN_ERR, NVF_END_CHAN_DEL, NVF_END_EXPIRED,
       NVF_END_DISTRUST, NVF_END_PM, NVF_END_SHUTDOWN };
enum { NVF_WHY_FIRST = 0, NVF_WHY_IRQ, NVF_WHY_STATE, NVF_WHY_SLICE };
enum { NVF_SLEPT = 0, NVF_SLEEP_ABORTED, NVF_SLEEP_FULL };
enum { NVF_BOUND_GPU = 1, NVF_BOUND_CPU, NVF_BOUND_PENALTY };
enum { NVF_EV_STRIKE = 0, NVF_EV_DISTRUST, NVF_EV_FUSE, NVF_EV_NOIRQ, NVF_EV_KINDS };

/* The counters behind debug.nvrm_fence_*; docs/SURFACE-FENCE.md says how to read each. LIVE, LIVE_MAX, CHANS and
 * CHAN_NOIRQ are levels, WAIT_MAX_US is a maximum, the rest only grow. */
enum {
    NVF_STAT_OPENS = 0, NVF_STAT_ARMS, NVF_STAT_ARM_DONE, NVF_STAT_CLOSES, NVF_STAT_CLOSE_GONE, NVF_STAT_AUTO,
    NVF_STAT_END_OWNER, NVF_STAT_END_CHAN_ERR, NVF_STAT_END_CHAN_DEL, NVF_STAT_END_PM,
    NVF_STAT_REFUSED_FULL, NVF_STAT_REFUSED_DENIED, NVF_STAT_REFUSED_STALE, NVF_STAT_REFUSED_DISTRUST,
    NVF_STAT_CHANS, NVF_STAT_CHAN_ADDS, NVF_STAT_CHAN_NOIRQ, NVF_STAT_CHAN_REBINDS, NVF_STAT_MAP_FAIL, NVF_STAT_MAP_MISMATCH,
    NVF_STAT_LIVE, NVF_STAT_LIVE_MAX, NVF_STAT_RECLAIMED,
    NVF_STAT_WAITS, NVF_STAT_WAIT_NONE, NVF_STAT_LOOK_PENDING, NVF_STAT_WAIT_READY, NVF_STAT_WAIT_DEAD,
    NVF_STAT_WAIT_TIMEOUT, NVF_STAT_WAIT_BUDGET, NVF_STAT_WAIT_ABORTED, NVF_STAT_WAIT_FULL,
    NVF_STAT_WAKE_IRQ, NVF_STAT_WAKE_STATE, NVF_STAT_WAKE_SLICE,
    NVF_STAT_DONE_ON_IRQ, NVF_STAT_DONE_ON_STATE, NVF_STAT_DONE_ON_SLICE,
    NVF_STAT_EXPIRED_GPU, NVF_STAT_EXPIRED_CPU, NVF_STAT_EXPIRED_PENALTY, NVF_STAT_STRIKES, NVF_STAT_OWNERS_DISTRUSTED,
    NVF_STAT_CLOSE_GPU_AGREE, NVF_STAT_CLOSE_GPU_BEHIND,
    NVF_STAT_WAIT_MAX_US, NVF_STAT_WAIT_LT1MS, NVF_STAT_WAIT_LT10MS, NVF_STAT_WAIT_LT100MS, NVF_STAT_WAIT_GE100MS,
    NVF_STAT_SEQ_WAITS, NVF_STAT_SEQ_SLEPT, NVF_STAT_SEQ_REFUSED,
    NVF_STAT_N
};

struct nvf_entry {                       /* 64 bytes */
    unsigned long long owner, cookie, seq, sinceNs, serial;   /* sinceNs: when it entered its state; serial: open order */
    unsigned sid, pid, chan;             /* chan: handle it is armed on */
    unsigned short next;                 /* bucket chain or free list */
    unsigned char state, flags, planes, expired;   /* state NVRM_FENCE_E_*, 0 = free; flags COMPOSITOR | AUTO; expired: the
                                                      NVF_END_* it was flagged with, 0 = it still delays readers */
    unsigned short oidx, onext, oprev;   /* its owner's record, and that owner's list of holds */
};
struct nvf_chan {
    unsigned long long owner, done;      /* done: the highest completion value seen, never lowered */
    unsigned gen, pass;                  /* gen 0 = free slot; pass: the call that last read its pages */
    unsigned short oidx;
    unsigned char used, verified, faulted, ce, found, foundSlice, noirq;
};
struct nvf_owner {
    unsigned long long owner, penaltyUntilNs, lastStrikeNs;   /* owner 0 = free record */
    unsigned pid, entries, chans;
    unsigned short head;                 /* its first hold, NVF_NIL = none */
    unsigned char strikes, distrusted;
};
struct nvf_recent { unsigned long long serial; unsigned sid, pid; unsigned char why, flags, planes; };
struct nvf_event  { unsigned kind, sid, pid, ms; unsigned char state, bound, strikes, pad; };
struct nvf_table {
    struct nvf_entry e[NVF_ENTRIES]; unsigned short bucket[NVF_BUCKETS], freeHead;
    struct nvf_chan  c[NVF_CHANS];   unsigned chanHigh, chanGen, pass;
    struct nvf_owner o[NVF_OWNERS];
    struct nvf_recent recent[NVF_RECENT]; unsigned long long recentSeq;   /* how the last holds ended */
    unsigned long long serial;           /* ++ at every open; never 0 after nvf_init */
    unsigned live, liveCompositor, liveMax;   /* holds that still delay someone (not flagged) */
    unsigned used, flagged;              /* slots taken; of those, flagged */
    unsigned changes;                    /* ++ whenever a hold ends or is flagged: the caller wakes sleepers */
    unsigned distrustedOwners, fused;
    unsigned off, paused;                /* set by the caller: off = refuse channels and holds; paused = refuse holds (GPU asleep) */
    struct nvf_event ev[NVF_EVENTS]; unsigned evN, evSeen[NVF_EV_KINDS];
    unsigned long long stat[NVF_STAT_N];
};
/* How the logic sees hardware. Returns 0 when this channel has no pages to read (the channel then counts as faulted).
 * `flushed` may be null: the value of the last flush is wanted only on the path that asks whether an owner's GPU is busy. */
typedef int (*nvf_read_fn)(void *ctx, unsigned chanIndex, unsigned long long *done, unsigned long long *flushed, int *faulted);

struct nvf_query { unsigned sid, planes, pid, flags; unsigned long long snap, recent0; };   /* snap 0 = not looked yet */
struct nvf_view  { unsigned blockers, firstPid, firstState; unsigned long long deadlineNs; int fast; };
struct nvf_wait_ops {
    void (*lock)(void *ctx); void (*unlock)(void *ctx);
    unsigned long long (*now)(void *ctx);
    unsigned (*kick)(void *ctx); unsigned (*gen)(void *ctx);            /* the two generations: interrupt, table change */
    /* Sleep until deadlineNs, or until either generation is no longer the one given. The implementation announces the
     * sleeper, puts it on the wait queue, reads both generations AGAIN and only then blocks: a kick between the
     * caller's look and the sleep is then never lost. */
    int (*sleep)(void *ctx, unsigned kickSeen, unsigned genSeen, unsigned long long deadlineNs, int abortable);   /* NVF_SLEPT... */
    nvf_read_fn read;
};

NVRM_FENCE_SASSERT(sizeof(struct nvf_entry) == 64, nvf_entry_size);
NVRM_FENCE_SASSERT(NVF_CHANS <= (1u << NVF_CHAN_INDEX_BITS), nvf_chan_index_fits);
NVRM_FENCE_SASSERT(NVF_ENTRIES < NVF_NIL && NVF_OWNERS < NVF_NIL, nvf_links_fit);
/* nvf_result_from_recent takes the largest: the precedence TIMEOUT > DEAD > READY > NONE is their numeric order */
NVRM_FENCE_SASSERT(NVRM_FENCE_R_NONE < NVRM_FENCE_R_READY && NVRM_FENCE_R_READY < NVRM_FENCE_R_DEAD &&
                   NVRM_FENCE_R_DEAD < NVRM_FENCE_R_TIMEOUT, nvf_result_order);
NVRM_FENCE_SASSERT(NVF_WHY_IRQ == 1 && NVF_WHY_STATE == 2 && NVF_WHY_SLICE == 3, nvf_why_order);

/* ---- small pieces ----------------------------------------------------------------------------------------------- */

static inline unsigned nvf_bucket(unsigned sid) { return (sid * 2654435761u) >> 24; }

/* What CHAN_ADD and CHAN_REBIND accept before any page is touched. The semaphore page: page aligned, not page zero, a
 * user address. The notifier: 16 bytes at a 16-aligned user address, which also keeps them inside one page. errBase
 * is a 16-bit status. */
static inline int nvf_sem_va_ok(unsigned long long semVA)
{
    return semVA >= NVF_PAGE && (semVA & (NVF_PAGE - 1)) == 0 && semVA <= NVF_USER_TOP - NVF_PAGE;
}
static inline int nvf_err_va_ok(unsigned long long errVA, unsigned errBase)
{
    return errVA >= NVF_PAGE && (errVA & 15u) == 0 && errVA <= NVF_USER_TOP - 16 && errBase <= 0xFFFFu;
}

/* Where a wired page may be read from. The kernel reads a channel's pages through its map of physical memory, and on
 * x86-64 that map covers installed memory plus 4 GB (for the hole below 4 GB) and nothing above. A process can also
 * hand in a page of device memory it has mapped, such as the GPU's memory window, which this driver moves to 256 GB
 * and up: a read there has no address in the map and stops the machine. So a page is taken only when it is one whole,
 * aligned page that ends at or below `limit` (installed memory + 4 GB); limit 0, not known, takes none. */
static inline int nvf_phys_ok(unsigned long long pa, unsigned long long len, unsigned long long limit)
{
    return pa != 0 && (pa & (NVF_PAGE - 1)) == 0 && len >= NVF_PAGE && pa < limit && limit - pa >= NVF_PAGE;
}

static inline void nvf_init(struct nvf_table *T)
{
    unsigned char *p = (unsigned char *)T;
    unsigned long i;
    for (i = 0; i < sizeof(*T); i++) p[i] = 0;
    for (i = 0; i < NVF_BUCKETS; i++) T->bucket[i] = (unsigned short)NVF_NIL;
    for (i = 0; i < NVF_ENTRIES; i++) T->e[i].next = (unsigned short)(i + 1 < NVF_ENTRIES ? i + 1 : NVF_NIL);
    T->freeHead = 0;
    T->serial = 1;
}

/* A table that nvf_init has not seen (all zero, as static data is before the first GPU start) holds nothing and
 * accepts nothing. Its lists are not built yet, so nothing may walk them: the three functions that would say no first. */
static inline int nvf_ready(const struct nvf_table *T) { return T->serial != 0; }

static inline void nvf_count(struct nvf_table *T, unsigned stat) { if (stat < NVF_STAT_N) T->stat[stat]++; }

/* Something the kernel should say in the log, once the lock is released. The first 8 of a kind, then every 256th. */
static inline void nvf_event(struct nvf_table *T, unsigned kind, unsigned sid, unsigned pid, unsigned ms,
                             unsigned state, unsigned bound, unsigned strikes)
{
    struct nvf_event *ev;
    const unsigned seen = ++T->evSeen[kind];
    if ((seen > 8 && (seen & 0xFFu) != 0) || T->evN >= NVF_EVENTS) return;
    ev = &T->ev[T->evN++];
    ev->kind = kind; ev->sid = sid; ev->pid = pid; ev->ms = ms;
    ev->state = (unsigned char)state; ev->bound = (unsigned char)bound; ev->strikes = (unsigned char)strikes; ev->pad = 0;
}
/* Hands the noted events to the caller (who logs them after unlocking) and forgets them. Returns how many. */
static inline unsigned nvf_events_take(struct nvf_table *T, struct nvf_event *out, unsigned max)
{
    unsigned i, n = T->evN < max ? T->evN : max;
    for (i = 0; i < n; i++) out[i] = T->ev[i];
    T->evN = 0;
    return n;
}

static inline void nvf_live_changed(struct nvf_table *T)
{
    if (T->live > T->liveMax) T->liveMax = T->live;
    T->stat[NVF_STAT_LIVE] = T->live;
    T->stat[NVF_STAT_LIVE_MAX] = T->liveMax;
}

/* ---- owners ----------------------------------------------------------------------------------------------------- */

/* Owner ids are handed out in sequence, so id % NVF_OWNERS is nearly always the record's place; a miss walks them all. */
static inline int nvf_owner_find(const struct nvf_table *T, unsigned long long owner)
{
    unsigned i, at;
    if (!owner) return -1;
    at = (unsigned)(owner % NVF_OWNERS);
    for (i = 0; i < NVF_OWNERS; i++, at = (at + 1) % NVF_OWNERS)
        if (T->o[at].owner == owner) return (int)at;
    return -1;
}

/* The owner's record, made on first use. -1: no record is free, or this process already has NVF_PID_OWNERS of them. */
static inline int nvf_owner_get(struct nvf_table *T, unsigned long long owner, unsigned pid)
{
    struct nvf_owner *o;
    unsigned i, at, same = 0;
    int spare = -1, oi = nvf_owner_find(T, owner);
    if (oi >= 0 || !owner) return oi;
    at = (unsigned)(owner % NVF_OWNERS);
    for (i = 0; i < NVF_OWNERS; i++, at = (at + 1) % NVF_OWNERS) {
        if (!T->o[at].owner) { if (spare < 0) spare = (int)at; }
        else if (T->o[at].pid == pid) same++;
    }
    if (spare < 0 || same >= NVF_PID_OWNERS) return -1;
    o = &T->o[spare];
    o->owner = owner; o->penaltyUntilNs = 0; o->lastStrikeNs = 0;
    o->pid = pid; o->entries = 0; o->chans = 0;
    o->head = (unsigned short)NVF_NIL; o->strikes = 0; o->distrusted = 0;
    return spare;
}

/* ---- holds: lists, ending, flagging ----------------------------------------------------------------------------- */

static inline int nvf_matches(const struct nvf_query *q, unsigned sid, unsigned long long serial, unsigned planes,
                              unsigned pid, unsigned flags)
{
    if (sid != q->sid || serial > q->snap) return 0;
    if (q->planes && planes && !(q->planes & planes)) return 0;        /* 0 on either side means every plane */
    if (q->pid && q->pid != pid) return 0;
    if ((q->flags & NVRM_FENCE_F_COMPOSITOR) && !(flags & NVRM_FENCE_F_COMPOSITOR)) return 0;
    return 1;
}

static inline void nvf_note_end(struct nvf_table *T, const struct nvf_entry *e, unsigned why)
{
    struct nvf_recent *r = &T->recent[T->recentSeq % NVF_RECENT];
    r->serial = e->serial; r->sid = e->sid; r->pid = e->pid;
    r->why = (unsigned char)why; r->flags = e->flags; r->planes = e->planes;
    T->recentSeq++;
}

static inline void nvf_live_drop(struct nvf_table *T, const struct nvf_entry *e)
{
    if (T->live) T->live--;
    if ((e->flags & NVRM_FENCE_F_COMPOSITOR) && T->liveCompositor) T->liveCompositor--;
    nvf_live_changed(T);
}

/* The hold stops delaying readers, and they are told why, but it keeps its slot until its CLOSE, its owner's exit, or
 * until the slot is needed. */
static inline void nvf_flag(struct nvf_table *T, unsigned ei, unsigned why)
{
    struct nvf_entry *e = &T->e[ei];
    if (!e->state || e->expired) return;
    e->expired = (unsigned char)why;
    nvf_note_end(T, e, why);
    nvf_live_drop(T, e);
    T->flagged++;
    T->changes++;
}

/* Frees the hold. One that still delayed readers is recorded for them with `why`; a flagged one was recorded when it
 * was flagged. */
static inline void nvf_end(struct nvf_table *T, unsigned ei, unsigned why)
{
    struct nvf_entry *e = &T->e[ei];
    struct nvf_owner *o = &T->o[e->oidx];
    unsigned short *pp;
    if (!e->state) return;
    if (e->expired) {
        if (T->flagged) T->flagged--;
    } else {
        nvf_note_end(T, e, why);
        nvf_live_drop(T, e);
        T->changes++;
        if (why == NVF_END_GPU) T->stat[NVF_STAT_AUTO]++;
        else if (why == NVF_END_OWNER) T->stat[NVF_STAT_END_OWNER]++;
        else if (why == NVF_END_CHAN_ERR) T->stat[NVF_STAT_END_CHAN_ERR]++;
        else if (why == NVF_END_CHAN_DEL) T->stat[NVF_STAT_END_CHAN_DEL]++;
        else if (why == NVF_END_PM) T->stat[NVF_STAT_END_PM]++;
    }
    for (pp = &T->bucket[nvf_bucket(e->sid)]; *pp != NVF_NIL && *pp != ei; pp = &T->e[*pp].next) { }
    if (*pp == ei) *pp = e->next;
    if (e->oprev != NVF_NIL) T->e[e->oprev].onext = e->onext; else o->head = e->onext;
    if (e->onext != NVF_NIL) T->e[e->onext].oprev = e->oprev;
    if (o->entries) o->entries--;
    if (T->used) T->used--;
    e->state = 0; e->expired = 0;
    e->next = T->freeHead;
    T->freeHead = (unsigned short)ei;
}

/* The hold of (owner record, cookie, surface), or -1. An owner has at most NVF_OWNER_ENTRIES holds. */
static inline int nvf_find(const struct nvf_table *T, unsigned oi, unsigned long long cookie, unsigned sid)
{
    unsigned ei;
    for (ei = T->o[oi].head; ei != NVF_NIL; ei = T->e[ei].onext)
        if (T->e[ei].cookie == cookie && T->e[ei].sid == sid) return (int)ei;
    return -1;
}

static inline void nvf_end_all(struct nvf_table *T, unsigned why)
{
    unsigned i;
    for (i = 0; i < NVF_ENTRIES; i++) if (T->e[i].state) nvf_end(T, i, why);
    T->changes++;       /* also for sleepers that wait for no hold (nvf_wait_seq) */
}

/* ---- channels --------------------------------------------------------------------------------------------------- */

static inline int nvf_chan_trusted(const struct nvf_chan *c)
{
    return c->found >= NVF_TRUST_MIN && (unsigned)c->foundSlice * 2u <= c->found;
}

/* The channel index a handle names for this owner, or -1: unknown, deleted since, or another owner's. */
static inline int nvf_chan_find(const struct nvf_table *T, unsigned long long owner, unsigned handle)
{
    const unsigned i = handle & ((1u << NVF_CHAN_INDEX_BITS) - 1u), gen = handle >> NVF_CHAN_INDEX_BITS;
    if (!owner || !gen || i >= NVF_CHANS) return -1;
    if (!T->c[i].used || T->c[i].gen != gen || T->c[i].owner != owner) return -1;
    return (int)i;
}

static inline void nvf_chan_free(struct nvf_table *T, unsigned ci)
{
    struct nvf_chan *c = &T->c[ci];
    if (!c->used) return;
    if (c->noirq && T->stat[NVF_STAT_CHAN_NOIRQ]) T->stat[NVF_STAT_CHAN_NOIRQ]--;
    if (T->o[c->oidx].chans) T->o[c->oidx].chans--;
    c->used = 0; c->verified = 0; c->faulted = 0; c->noirq = 0; c->gen = 0; c->owner = 0;
    while (T->chanHigh && !T->c[T->chanHigh - 1].used) T->chanHigh--;
    if (T->stat[NVF_STAT_CHANS]) T->stat[NVF_STAT_CHANS]--;
}

/* Reads the channel's pages, at most once per call into this file: done only rises, a fault sticks until a rebind. */
static inline void nvf_chan_refresh(struct nvf_table *T, unsigned ci, nvf_read_fn read, void *ctx)
{
    struct nvf_chan *c = &T->c[ci];
    unsigned long long done = 0;
    int faulted = 0;
    if (c->pass == T->pass) return;
    c->pass = T->pass;
    if (!read || !read(ctx, ci, &done, (unsigned long long *)0, &faulted)) { c->faulted = 1; return; }
    if (done > c->done) c->done = done;
    if (faulted) c->faulted = 1;
}

/* A sleeper woke and found work on this channel complete. Was it an interrupt that woke it, or did only the slice find
 * it? A wake by a table change says nothing either way. */
static inline void nvf_chan_judge(struct nvf_table *T, unsigned ci, int why)
{
    struct nvf_chan *c = &T->c[ci];
    unsigned char noirq;
    if (why != NVF_WHY_IRQ && why != NVF_WHY_SLICE) return;
    c->found++;
    if (why == NVF_WHY_SLICE) c->foundSlice++;
    if (c->found >= NVF_TRUST_HALVE) { c->found = (unsigned char)(c->found / 2); c->foundSlice = (unsigned char)(c->foundSlice / 2); }
    noirq = (unsigned char)(c->found >= NVF_TRUST_MIN && !nvf_chan_trusted(c));
    if (noirq == c->noirq) return;
    c->noirq = noirq;
    if (noirq) {
        T->stat[NVF_STAT_CHAN_NOIRQ]++;
        nvf_event(T, NVF_EV_NOIRQ, ci, T->o[c->oidx].pid, 0, 0, 0, c->ce);
    } else if (T->stat[NVF_STAT_CHAN_NOIRQ]) {
        T->stat[NVF_STAT_CHAN_NOIRQ]--;
    }
}

static inline unsigned nvf_chan_add(struct nvf_table *T, unsigned long long owner, unsigned pid, unsigned flags,
                                    unsigned *handle)
{
    struct nvf_chan *c;
    unsigned i;
    int oi;
    *handle = 0;
    if (!nvf_ready(T) || T->off) return NVRM_FENCE_ST_OFF;
    if (!owner) return NVRM_FENCE_ST_BAD_ARG;
    oi = nvf_owner_get(T, owner, pid);
    if (oi < 0 || T->o[oi].chans >= NVF_OWNER_CHANS) { T->stat[NVF_STAT_REFUSED_FULL]++; return NVRM_FENCE_ST_FULL; }
    for (i = 0; i < NVF_CHANS && T->c[i].used; i++) { }
    if (i >= NVF_CHANS) { T->stat[NVF_STAT_REFUSED_FULL]++; return NVRM_FENCE_ST_FULL; }
    c = &T->c[i];
    T->chanGen = T->chanGen >= NVF_CHAN_GEN_MAX ? 1u : T->chanGen + 1u;
    c->owner = owner; c->done = 0; c->gen = T->chanGen; c->pass = T->pass - 1u;
    c->oidx = (unsigned short)oi;
    c->used = 1; c->verified = 0; c->faulted = 0; c->ce = (flags & NVRM_FENCE_F_CHAN_CE) ? 1 : 0;
    c->found = 0; c->foundSlice = 0; c->noirq = 0;
    T->o[oi].chans++;
    if (i >= T->chanHigh) T->chanHigh = i + 1;
    T->stat[NVF_STAT_CHANS]++;
    T->stat[NVF_STAT_CHAN_ADDS]++;
    *handle = (c->gen << NVF_CHAN_INDEX_BITS) | i;
    return NVRM_FENCE_ST_OK;
}

/* The second half of the registration: `matches` says the kernel's view showed the value the producer stored after
 * CHAN_ADD. If not, the channel is freed (MAP) and *freeIndex is the index whose pages the caller lets go of. */
static inline unsigned nvf_chan_verify(struct nvf_table *T, unsigned long long owner, unsigned handle, int matches,
                                       unsigned *freeIndex)
{
    const int ci = nvf_chan_find(T, owner, handle);
    *freeIndex = NVF_NIL;
    if (ci < 0) { T->stat[NVF_STAT_REFUSED_STALE]++; return NVRM_FENCE_ST_STALE; }
    if (T->c[ci].verified) return NVRM_FENCE_ST_OK;       /* proven once; a second proof changes nothing */
    if (matches) { T->c[ci].verified = 1; return NVRM_FENCE_ST_OK; }
    nvf_chan_free(T, (unsigned)ci);                       /* nothing can be armed on a channel that was never verified */
    *freeIndex = (unsigned)ci;
    T->stat[NVF_STAT_MAP_MISMATCH]++;
    return NVRM_FENCE_ST_MAP;
}

/* The producer rebuilt a faulted channel and gave it a new error notifier: the old fault is forgotten, and nothing
 * else changes (the holds, the completion value seen, the verified state, what is known about its interrupts).
 * *index is the channel whose notifier the caller swaps. Any refusal leaves the channel as it was. */
static inline unsigned nvf_chan_rebind(struct nvf_table *T, unsigned long long owner, unsigned handle, unsigned *index)
{
    const int ci = nvf_chan_find(T, owner, handle);
    *index = NVF_NIL;
    if (T->off) return NVRM_FENCE_ST_OFF;
    if (ci < 0 || !T->c[ci].verified) { T->stat[NVF_STAT_REFUSED_STALE]++; return NVRM_FENCE_ST_STALE; }
    T->c[ci].faulted = 0;
    T->c[ci].pass = T->pass - 1u;        /* the next look reads the new notifier */
    T->stat[NVF_STAT_CHAN_REBINDS]++;
    *index = (unsigned)ci;
    return NVRM_FENCE_ST_OK;
}

/* ---- the life of a hold ----------------------------------------------------------------------------------------- */

/* The GPU reached what the hold was armed on, or its producer said the work was already complete. A compositor hold
 * opened with AUTO ends here; an application's waits for its CLOSE, on the short bound, counted from now (but not
 * counted afresh when it was armed again after completing once: re-arming cannot stretch a hold). */
static inline void nvf_complete(struct nvf_table *T, unsigned ei, unsigned long long now)
{
    struct nvf_entry *e = &T->e[ei];
    if (e->flags & NVRM_FENCE_F_AUTO) { nvf_end(T, ei, NVF_END_GPU); return; }
    e->state = (unsigned char)NVRM_FENCE_E_GPU_DONE;
    if (!(e->flags & NVF_EF_REARMED)) e->sinceNs = now;
}

/* One look at an ARMED hold. 1: still waiting for the GPU. 0: it completed, or ended with its channel. */
static inline int nvf_armed_step(struct nvf_table *T, unsigned ei, unsigned long long now, int why, nvf_read_fn read,
                                 void *ctx)
{
    struct nvf_entry *e = &T->e[ei];
    const int ci = nvf_chan_find(T, e->owner, e->chan);
    if (ci < 0) { nvf_end(T, ei, NVF_END_CHAN_DEL); return 0; }    /* deleting a channel ends what is armed on it; kept for safety */
    nvf_chan_refresh(T, (unsigned)ci, read, ctx);
    if (T->c[ci].done >= e->seq) { nvf_chan_judge(T, (unsigned)ci, why); nvf_complete(T, ei, now); return 0; }
    if (T->c[ci].faulted) { nvf_end(T, ei, NVF_END_CHAN_ERR); return 0; }
    return 1;
}

/* Does this owner still have GPU work in flight: a verified, unfaulted channel whose last flush is beyond what the GPU
 * has completed. Asked only for a hold that has outlived the short bound. */
static inline int nvf_owner_gpu_busy(struct nvf_table *T, unsigned oi, nvf_read_fn read, void *ctx)
{
    unsigned i;
    for (i = 0; i < T->chanHigh; i++) {
        struct nvf_chan *c = &T->c[i];
        unsigned long long done = 0, flushed = 0;
        int faulted = 0;
        if (!c->used || c->oidx != oi || !c->verified || c->faulted) continue;
        if (!read || !read(ctx, i, &done, &flushed, &faulted)) continue;
        if (done > c->done) c->done = done;
        if (faulted) { c->faulted = 1; continue; }
        if (flushed > c->done) return 1;
    }
    return 0;
}

/* The longest the hold may stay in its state, in ms, and which rule gave it. */
static inline unsigned nvf_bound_ms(struct nvf_table *T, const struct nvf_entry *e, unsigned long long now,
                                    nvf_read_fn read, void *ctx, unsigned *kind)
{
    const unsigned long long age = now > e->sinceNs ? now - e->sinceNs : 0;
    if (now < T->o[e->oidx].penaltyUntilNs) { *kind = NVF_BOUND_PENALTY; return NVF_CAP_PEN_MS; }
    if (e->state == NVRM_FENCE_E_ARMED) { *kind = NVF_BOUND_GPU; return NVF_CAP_GPU_MS; }
    if (age >= NVF_CAP_CPU_MS * NVF_NS_PER_MS && nvf_owner_gpu_busy(T, e->oidx, read, ctx)) {
        *kind = NVF_BOUND_GPU;
        return NVF_CAP_GPU_MS;
    }
    *kind = NVF_BOUND_CPU;
    return NVF_CAP_CPU_MS;
}

/* A hold of this owner outlived a bound that was its own fault. One strike is forgiven for every NVF_FORGIVE_MS since
 * the last one; each strike opens a penalty window (10 s, then 20 s) in which every hold of the owner has the short
 * penalty bound and expiries do not count, so strikes are at least that far apart. Returns 0, 1 when the owner is now
 * distrusted (its holds were flagged), 2 when that was the owner that switches the layer off (the caller ends
 * everything). */
static inline int nvf_strike(struct nvf_table *T, unsigned oi, unsigned long long now)
{
    struct nvf_owner *o = &T->o[oi];
    unsigned ei, doublings;
    if (o->strikes && now > o->lastStrikeNs) {
        const unsigned long long forgiven = (now - o->lastStrikeNs) / (NVF_FORGIVE_MS * NVF_NS_PER_MS);
        o->strikes = forgiven >= o->strikes ? 0 : (unsigned char)(o->strikes - forgiven);
    }
    if (o->strikes < 0xFF) o->strikes++;
    doublings = o->strikes > 8 ? 7u : (unsigned)o->strikes - 1u;
    o->lastStrikeNs = now;
    o->penaltyUntilNs = now + ((unsigned long long)NVF_PENALTY_MS << doublings) * NVF_NS_PER_MS;
    T->stat[NVF_STAT_STRIKES]++;
    if (o->strikes < NVF_STRIKES || o->distrusted) return 0;
    o->distrusted = 1;
    T->distrustedOwners++;
    T->stat[NVF_STAT_OWNERS_DISTRUSTED]++;
    for (ei = o->head; ei != NVF_NIL; ei = T->e[ei].onext) nvf_flag(T, ei, NVF_END_DISTRUST);
    if (T->distrustedOwners < NVF_FUSE_OWNERS) return 1;
    T->fused = 1;
    T->off = 1;
    return 2;
}

/* Room for `want` more holds: frees holds that delay nobody. First the flagged ones of this owner, then (unless
 * ownOnly) any flagged one; then holds that have been in their state longer than the longest bound and that no reader
 * has looked at since (a reader would have flagged them), the owner's first. A hold that still delays a reader is
 * never taken, and neither is one of the command buffer being opened. */
static inline void nvf_reclaim(struct nvf_table *T, unsigned oi, unsigned want, unsigned long long keepCookie,
                               unsigned long long now, int ownOnly)
{
    const unsigned long long owner = T->o[oi].owner;
    unsigned pass, i, freed = 0;
    for (pass = 0; pass < 4 && freed < want; pass++) {
        const int own = (pass == 0 || pass == 2), stale = pass >= 2;
        if (!own && ownOnly) continue;
        for (i = 0; i < NVF_ENTRIES && freed < want; i++) {
            struct nvf_entry *e = &T->e[i];
            if (!e->state || (own && e->oidx != oi)) continue;
            if (e->owner == owner && e->cookie == keepCookie) continue;
            if (stale ? (e->expired || now < e->sinceNs || now - e->sinceNs <= NVF_CAP_GPU_MS * NVF_NS_PER_MS) : !e->expired) continue;
            nvf_end(T, i, NVF_END_EXPIRED);
            T->stat[NVF_STAT_RECLAIMED]++;
            freed++;
        }
    }
}

/* All or nothing: a hold per surface in `ids` for the command buffer `cookie`, or none. A surface the cookie already
 * holds only gains planes. */
static inline unsigned nvf_open(struct nvf_table *T, unsigned long long owner, unsigned pid, unsigned long long cookie,
                                unsigned n, const unsigned *ids, const unsigned char *planes, unsigned flags,
                                unsigned long long now)
{
    struct nvf_owner *o;
    unsigned i, j, need = 0, fresh = 0;
    int oi;
    if (!nvf_ready(T) || T->off || T->paused) return NVRM_FENCE_ST_OFF;
    if (!owner || !cookie || n < 1 || n > NVRM_FENCE_MAX_IDS || !ids) return NVRM_FENCE_ST_BAD_ARG;
    for (i = 0; i < n; i++) if (!ids[i]) return NVRM_FENCE_ST_BAD_ARG;
    if ((flags & NVRM_FENCE_F_AUTO) && !(flags & NVRM_FENCE_F_COMPOSITOR)) return NVRM_FENCE_ST_BAD_ARG;
    oi = nvf_owner_get(T, owner, pid);
    if (oi < 0) { T->stat[NVF_STAT_REFUSED_FULL]++; return NVRM_FENCE_ST_FULL; }
    o = &T->o[oi];
    if (o->distrusted) { T->stat[NVF_STAT_REFUSED_DISTRUST]++; return NVRM_FENCE_ST_DISTRUST; }
    for (i = 0; i < n; i++) {
        for (j = 0; j < i && ids[j] != ids[i]; j++) { }
        if (j == i && nvf_find(T, (unsigned)oi, cookie, ids[i]) < 0) { need++; fresh |= 1u << i; }
    }
    if (o->entries + need > NVF_OWNER_ENTRIES)
        nvf_reclaim(T, (unsigned)oi, o->entries + need - NVF_OWNER_ENTRIES, cookie, now, 1);
    if (o->entries + need <= NVF_OWNER_ENTRIES && T->used + need > NVF_ENTRIES)     /* not for an OPEN its own quota refuses */
        nvf_reclaim(T, (unsigned)oi, T->used + need - NVF_ENTRIES, cookie, now, 0);
    if (o->entries + need > NVF_OWNER_ENTRIES || T->used + need > NVF_ENTRIES) {
        T->stat[NVF_STAT_REFUSED_FULL]++;
        return NVRM_FENCE_ST_FULL;
    }
    T->serial++;
    for (i = 0; i < n; i++) {
        const unsigned char p = planes ? planes[i] : 0;
        struct nvf_entry *e;
        unsigned ei, b;
        if (!(fresh & (1u << i))) {
            const int at = nvf_find(T, (unsigned)oi, cookie, ids[i]);
            if (at >= 0) T->e[at].planes = (unsigned char)((!T->e[at].planes || !p) ? 0 : (T->e[at].planes | p));
            continue;
        }
        ei = T->freeHead;
        e = &T->e[ei];
        T->freeHead = e->next;
        e->owner = owner; e->cookie = cookie; e->seq = 0; e->sinceNs = now; e->serial = T->serial;
        e->sid = ids[i]; e->pid = pid; e->chan = 0;
        e->state = (unsigned char)NVRM_FENCE_E_OPEN;
        e->flags = (unsigned char)(flags & (NVRM_FENCE_F_COMPOSITOR | NVRM_FENCE_F_AUTO));
        e->planes = p; e->expired = 0;
        b = nvf_bucket(e->sid);
        e->next = T->bucket[b];
        T->bucket[b] = (unsigned short)ei;
        e->oidx = (unsigned short)oi; e->oprev = (unsigned short)NVF_NIL; e->onext = o->head;
        if (o->head != NVF_NIL) T->e[o->head].oprev = (unsigned short)ei;
        o->head = (unsigned short)ei;
        o->entries++; T->used++; T->live++;
        if (e->flags & NVRM_FENCE_F_COMPOSITOR) T->liveCompositor++;
        T->stat[NVF_STAT_OPENS]++;
    }
    nvf_live_changed(T);
    return NVRM_FENCE_ST_OK;
}

/* Gives the hold its GPU target. A second ARM on the same channel raises the value and never lowers it; on another
 * channel it moves the hold there. ARM_DONE, or a value the channel has already reached, completes it at once. A
 * handle that is unknown, another owner's or not verified: STALE, and the hold stays as it was and ends at its CLOSE.
 * The clock of a bound starts when the hold is first armed and when it first completes, and no later ARM restarts it. */
static inline unsigned nvf_arm(struct nvf_table *T, unsigned long long owner, unsigned long long cookie, unsigned sid,
                               unsigned handle, unsigned long long seq, unsigned flags, unsigned long long now,
                               nvf_read_fn read, void *ctx)
{
    struct nvf_entry *e;
    const int oi = nvf_owner_find(T, owner);
    const int ei = oi < 0 ? -1 : nvf_find(T, (unsigned)oi, cookie, sid);
    int ci;
    if (ei < 0) { T->stat[NVF_STAT_REFUSED_STALE]++; return NVRM_FENCE_ST_STALE; }
    e = &T->e[ei];
    if (e->expired) return NVRM_FENCE_ST_OK;      /* its readers were already let go; its CLOSE frees it */
    T->pass++;
    if (e->state == NVRM_FENCE_E_GPU_DONE) e->flags |= NVF_EF_REARMED;
    if (flags & NVRM_FENCE_F_ARM_DONE) {
        T->stat[NVF_STAT_ARMS]++; T->stat[NVF_STAT_ARM_DONE]++;
        nvf_complete(T, (unsigned)ei, now);
        return NVRM_FENCE_ST_OK;
    }
    ci = nvf_chan_find(T, owner, handle);
    if (ci < 0 || !T->c[ci].verified) { T->stat[NVF_STAT_REFUSED_STALE]++; return NVRM_FENCE_ST_STALE; }
    if (e->state == NVRM_FENCE_E_ARMED && e->chan == handle) {
        if (seq > e->seq) e->seq = seq;
    } else {
        if (e->state == NVRM_FENCE_E_OPEN) e->sinceNs = now;
        e->chan = handle; e->seq = seq;
        e->state = (unsigned char)NVRM_FENCE_E_ARMED;
    }
    T->stat[NVF_STAT_ARMS]++;
    nvf_chan_refresh(T, (unsigned)ci, read, ctx);
    if (T->c[ci].done >= e->seq) T->stat[NVF_STAT_ARM_DONE]++;
    (void)nvf_armed_step(T, (unsigned)ei, now, NVF_WHY_FIRST, read, ctx);
    return NVRM_FENCE_ST_OK;
}

/* The producer has published: frees the hold of (cookie, sid), or with sid 0 every hold of the cookie. Not found is
 * not an error; *found says how many there were. Closing a hold that is still ARMED looks at its channel once more and
 * counts whether the kernel's view of the completion value agrees that the work is done. */
static inline unsigned nvf_close(struct nvf_table *T, unsigned long long owner, unsigned long long cookie, unsigned sid,
                                 nvf_read_fn read, void *ctx, unsigned *found)
{
    const int oi = nvf_owner_find(T, owner);
    unsigned ei, next, n = 0;
    T->pass++;
    for (ei = (oi < 0 || !cookie) ? NVF_NIL : T->o[oi].head; ei != NVF_NIL; ei = next) {
        struct nvf_entry *e = &T->e[ei];
        next = e->onext;
        if (e->cookie != cookie || (sid && e->sid != sid)) continue;
        if (e->state == NVRM_FENCE_E_ARMED && !e->expired) {
            const int ci = nvf_chan_find(T, owner, e->chan);
            if (ci >= 0) {
                nvf_chan_refresh(T, (unsigned)ci, read, ctx);
                if (T->c[ci].done >= e->seq) T->stat[NVF_STAT_CLOSE_GPU_AGREE]++;
                else if (!T->c[ci].faulted) T->stat[NVF_STAT_CLOSE_GPU_BEHIND]++;
            }
        }
        nvf_end(T, ei, NVF_END_CLOSE);
        n++;
    }
    T->stat[NVF_STAT_CLOSES] += n;
    if (!n) T->stat[NVF_STAT_CLOSE_GONE]++;
    if (found) *found = n;
    return NVRM_FENCE_ST_OK;
}

/* The channel goes away. It is read once more; what was armed on it and has been reached completes, the rest ends.
 * *freeIndex is the index whose pages the caller lets go of after the lock. */
static inline unsigned nvf_chan_del(struct nvf_table *T, unsigned long long owner, unsigned handle,
                                    unsigned long long now, nvf_read_fn read, void *ctx, unsigned *freeIndex)
{
    const int ci = nvf_chan_find(T, owner, handle);
    unsigned ei, next;
    *freeIndex = NVF_NIL;
    if (ci < 0) { T->stat[NVF_STAT_REFUSED_STALE]++; return NVRM_FENCE_ST_STALE; }
    T->pass++;
    nvf_chan_refresh(T, (unsigned)ci, read, ctx);
    for (ei = T->o[T->c[ci].oidx].head; ei != NVF_NIL; ei = next) {
        struct nvf_entry *e = &T->e[ei];
        next = e->onext;
        if (e->state != NVRM_FENCE_E_ARMED || e->expired || e->chan != handle) continue;
        if (T->c[ci].done >= e->seq) nvf_complete(T, ei, now);
        else nvf_end(T, ei, NVF_END_CHAN_DEL);
    }
    nvf_chan_free(T, (unsigned)ci);
    *freeIndex = (unsigned)ci;
    return NVRM_FENCE_ST_OK;
}

/* The connection is closing: every hold of it ends, its channels are freed and, once none is left, its record. Writes
 * up to `max` channel indexes whose pages the caller lets go of after the lock and returns how many; a return of `max`
 * means "call again". */
static inline unsigned nvf_owner_gone(struct nvf_table *T, unsigned long long owner, unsigned *freeIndex, unsigned max)
{
    const int oi = nvf_owner_find(T, owner);
    struct nvf_owner *o;
    unsigned i, n = 0;
    if (oi < 0) return 0;
    o = &T->o[oi];
    while (o->head != NVF_NIL) nvf_end(T, o->head, NVF_END_OWNER);
    for (i = 0; i < T->chanHigh && n < max; i++) {
        if (!T->c[i].used || T->c[i].oidx != (unsigned)oi) continue;
        nvf_chan_free(T, i);
        freeIndex[n++] = i;
    }
    if (n < max || !o->chans) {
        o->owner = 0; o->pid = 0; o->entries = 0; o->chans = 0;
        o->strikes = 0; o->distrusted = 0; o->penaltyUntilNs = 0; o->lastStrikeNs = 0;
    }
    return n;
}

/* ---- the one decision function ---------------------------------------------------------------------------------- */

/* What does this reader still have in front of it? Looks at every hold on the surface that was there when the reader
 * first looked: reads the channels of the armed ones, completes what the GPU has reached, ends what a fault killed,
 * flags what outlived its bound. Returns the number of holds still in the way and fills *v for the sleep. */
static inline unsigned nvf_eval(struct nvf_table *T, struct nvf_query *q, unsigned long long now, int why,
                                nvf_read_fn read, void *ctx, struct nvf_view *v)
{
    int again;
    if (!nvf_ready(T)) {
        v->blockers = 0; v->firstPid = 0; v->firstState = 0; v->deadlineNs = ~0ull; v->fast = 0;
        return 0;
    }
    if (!q->snap) { q->snap = T->serial; q->recent0 = T->recentSeq; }
    T->pass++;
    do {
        unsigned ei, next;
        again = 0;
        v->blockers = 0; v->firstPid = 0; v->firstState = 0; v->deadlineNs = ~0ull; v->fast = 0;
        for (ei = T->bucket[nvf_bucket(q->sid)]; ei != NVF_NIL && !again; ei = next) {
            struct nvf_entry *e = &T->e[ei];
            unsigned long long age, boundNs;
            unsigned kind = 0, boundMs;
            next = e->next;
            if (e->expired || !nvf_matches(q, e->sid, e->serial, e->planes, e->pid, e->flags)) continue;
            if (e->state == NVRM_FENCE_E_ARMED && !nvf_armed_step(T, ei, now, why, read, ctx) && !e->state) continue;
            boundMs = nvf_bound_ms(T, e, now, read, ctx, &kind);
            boundNs = boundMs * NVF_NS_PER_MS;
            age = now > e->sinceNs ? now - e->sinceNs : 0;
            if (age >= boundNs) {
                const unsigned oi = e->oidx;
                int struck;
                nvf_flag(T, ei, NVF_END_EXPIRED);
                if (kind == NVF_BOUND_PENALTY) { T->stat[NVF_STAT_EXPIRED_PENALTY]++; continue; }
                T->stat[kind == NVF_BOUND_GPU ? NVF_STAT_EXPIRED_GPU : NVF_STAT_EXPIRED_CPU]++;
                struck = nvf_strike(T, oi, now);
                nvf_event(T, NVF_EV_STRIKE, e->sid, e->pid, (unsigned)(age / NVF_NS_PER_MS), e->state, kind, T->o[oi].strikes);
                if (struck) nvf_event(T, NVF_EV_DISTRUST, 0, T->o[oi].pid, 0, 0, 0, T->o[oi].strikes);
                if (struck == 2) {                       /* the layer switched itself off: nothing is in anyone's way */
                    nvf_event(T, NVF_EV_FUSE, 0, T->o[oi].pid, 0, 0, 0, 0);
                    nvf_end_all(T, NVF_END_DISTRUST);
                    v->blockers = 0; v->firstPid = 0; v->firstState = 0; v->deadlineNs = ~0ull; v->fast = 0;
                    return 0;
                }
                if (struck == 1) again = 1;              /* holds counted above may have been flagged: count afresh */
                continue;
            }
            if (!v->blockers) { v->firstPid = e->pid; v->firstState = e->state; }
            v->blockers++;
            if (e->sinceNs + boundNs < v->deadlineNs) v->deadlineNs = e->sinceNs + boundNs;
            if (e->state == NVRM_FENCE_E_ARMED) {
                const int ci = nvf_chan_find(T, e->owner, e->chan);
                if (ci >= 0 && !nvf_chan_trusted(&T->c[ci])) v->fast = 1;
            }
        }
    } while (again);
    return v->blockers;
}

/* What became of the holds this reader was (or would have been) waiting for: the worst of how they ended. */
static inline unsigned nvf_result_from_recent(const struct nvf_table *T, const struct nvf_query *q, int saw)
{
    unsigned long long s;
    unsigned res = saw ? NVRM_FENCE_R_READY : NVRM_FENCE_R_NONE;
    if (T->recentSeq - q->recent0 > NVF_RECENT) return NVRM_FENCE_R_READY;     /* more ended than the ring remembers */
    for (s = q->recent0; s != T->recentSeq; s++) {
        const struct nvf_recent *r = &T->recent[s % NVF_RECENT];
        unsigned one;
        if (!nvf_matches(q, r->sid, r->serial, r->planes, r->pid, r->flags)) continue;
        if (r->why == NVF_END_CLOSE || r->why == NVF_END_GPU) one = NVRM_FENCE_R_READY;
        else if (r->why == NVF_END_EXPIRED || r->why == NVF_END_DISTRUST) one = NVRM_FENCE_R_TIMEOUT;
        else one = NVRM_FENCE_R_DEAD;
        if (one > res) res = one;
    }
    return res;
}

/* `fast`: the hold waits on a channel whose interrupts are not known to arrive, so look often. Otherwise 2 ms after a
 * first look or a wake by interrupt or table change, doubling to 32 ms after each slice that found nothing. */
static inline unsigned nvf_slice_us(unsigned prevUs, int why, int fast)
{
    if (fast) return NVF_SLICE_FAST_US;
    if (why != NVF_WHY_SLICE || prevUs < NVF_SLICE_MIN_US) return NVF_SLICE_MIN_US;
    return prevUs * 2 > NVF_SLICE_MAX_US ? NVF_SLICE_MAX_US : prevUs * 2;
}

static inline void nvf_wake_count(struct nvf_table *T, int why)
{
    if (why == NVF_WHY_IRQ) T->stat[NVF_STAT_WAKE_IRQ]++;
    else if (why == NVF_WHY_STATE) T->stat[NVF_STAT_WAKE_STATE]++;
    else if (why == NVF_WHY_SLICE) T->stat[NVF_STAT_WAKE_SLICE]++;
}

/* How a wait that had something in front of it ended, and how long it took when it slept. */
static inline void nvf_wait_count(struct nvf_table *T, unsigned result, int slept, int why, unsigned long long ns)
{
    const unsigned long long us = ns / 1000ull;
    if (result == NVRM_FENCE_R_READY) T->stat[NVF_STAT_WAIT_READY]++;
    else if (result == NVRM_FENCE_R_DEAD) T->stat[NVF_STAT_WAIT_DEAD]++;
    else if (result == NVRM_FENCE_R_TIMEOUT) T->stat[NVF_STAT_WAIT_TIMEOUT]++;
    else if (result == NVRM_FENCE_R_BUDGET) T->stat[NVF_STAT_WAIT_BUDGET]++;
    else if (result == NVRM_FENCE_R_ABORTED) T->stat[NVF_STAT_WAIT_ABORTED]++;
    if (!slept) return;
    if (result == NVRM_FENCE_R_READY || result == NVRM_FENCE_R_DEAD || result == NVRM_FENCE_R_TIMEOUT) {
        if (why == NVF_WHY_IRQ) T->stat[NVF_STAT_DONE_ON_IRQ]++;
        else if (why == NVF_WHY_STATE) T->stat[NVF_STAT_DONE_ON_STATE]++;
        else if (why == NVF_WHY_SLICE) T->stat[NVF_STAT_DONE_ON_SLICE]++;
    }
    if (us > T->stat[NVF_STAT_WAIT_MAX_US]) T->stat[NVF_STAT_WAIT_MAX_US] = us;
    if (us < 1000) T->stat[NVF_STAT_WAIT_LT1MS]++;
    else if (us < 10000) T->stat[NVF_STAT_WAIT_LT10MS]++;
    else if (us < 100000) T->stat[NVF_STAT_WAIT_LT100MS]++;
    else T->stat[NVF_STAT_WAIT_GE100MS]++;
}

/* ---- the wait loop ---------------------------------------------------------------------------------------------- */

/* Waits until nothing that was pending on q's surface when the caller arrived is pending any more, for at most
 * timeoutMs (clamped to NVRM_FENCE_WAIT_MAX_MS). `look`: do not sleep, answer NONE or PENDING. The caller sets
 * q->sid, planes, pid, flags and zeroes q->snap. Returns NVRM_FENCE_ST_OK, or NVRM_FENCE_ST_FULL when the sleep was
 * refused (too many sleepers); the answer is w->result.
 *
 * Why no wake is lost: both generations are read BEFORE the look, and the sleep refuses to block when either has moved
 * since. A completion that lands after the look raises an interrupt, the interrupt moves the kick generation, and the
 * sleep sees it. Where no interrupt comes the slice ends the sleep, so the worst case is one slice late. */
static inline unsigned nvf_wait(struct nvf_table *T, const struct nvf_wait_ops *ops, void *ctx, struct nvf_query *q,
                                unsigned timeoutMs, int look, int abortable, struct NVRMFenceWait *w)
{
    const unsigned long long timeoutNs = (timeoutMs > NVRM_FENCE_WAIT_MAX_MS ? NVRM_FENCE_WAIT_MAX_MS : timeoutMs) * NVF_NS_PER_MS;
    unsigned long long t0, now, dl;
    struct nvf_view v;
    unsigned k, g, n, slice = 0, status = NVRM_FENCE_ST_OK;
    int why = NVF_WHY_FIRST, saw = 0, slept = 0, r;

    w->result = NVRM_FENCE_R_NONE; w->waitedUs = 0; w->ownerPid = 0; w->state = 0; w->blockers = 0;
    t0 = ops->now(ctx);
    for (;;) {
        k = ops->kick(ctx); g = ops->gen(ctx);                       /* the generations before the look */
        ops->lock(ctx);
        now = ops->now(ctx);
        nvf_wake_count(T, why);
        n = nvf_eval(T, q, now, why, ops->read, ctx, &v);
        if (!n) {
            w->result = look ? NVRM_FENCE_R_NONE : nvf_result_from_recent(T, q, saw);
            if (w->result == NVRM_FENCE_R_NONE) T->stat[NVF_STAT_WAIT_NONE]++;
            else {
                if (!saw) T->stat[NVF_STAT_WAITS]++;                 /* pending when it arrived, ended by this very look */
                nvf_wait_count(T, w->result, slept, why, now - t0);
            }
            ops->unlock(ctx);
            break;
        }
        if (!saw) { saw = 1; w->ownerPid = v.firstPid; if (!look) T->stat[NVF_STAT_WAITS]++; }
        w->state = v.firstState;
        if (n > w->blockers) w->blockers = n;
        if (look) { w->result = NVRM_FENCE_R_PENDING; T->stat[NVF_STAT_LOOK_PENDING]++; ops->unlock(ctx); break; }
        if (now - t0 >= timeoutNs) {                                 /* the caller's own bound: the holds are untouched */
            w->result = NVRM_FENCE_R_BUDGET;
            nvf_wait_count(T, w->result, slept, why, now - t0);
            ops->unlock(ctx);
            break;
        }
        slice = nvf_slice_us(slice, why, v.fast);
        ops->unlock(ctx);
        dl = t0 + timeoutNs;
        if (v.deadlineNs < dl) dl = v.deadlineNs;
        if (now + slice * 1000ull < dl) dl = now + slice * 1000ull;
        r = ops->sleep(ctx, k, g, dl, abortable);
        if (r != NVF_SLEPT) {
            ops->lock(ctx);
            now = ops->now(ctx);
            if (r == NVF_SLEEP_ABORTED) {
                w->result = NVRM_FENCE_R_ABORTED;
                nvf_wait_count(T, w->result, slept, why, now - t0);
            } else {
                w->result = NVRM_FENCE_R_PENDING;
                status = NVRM_FENCE_ST_FULL;
                T->stat[NVF_STAT_WAIT_FULL]++;
            }
            ops->unlock(ctx);
            break;
        }
        slept = 1;
        why = ops->kick(ctx) != k ? NVF_WHY_IRQ : ops->gen(ctx) != g ? NVF_WHY_STATE : NVF_WHY_SLICE;
    }
    w->waitedUs = (now - t0) / 1000ull;
    return status;
}

/* A producer waits for its own channel to reach a value. 1 reached; 0 not yet (the caller may sleep; *fast says the
 * channel's interrupts are still being judged, so sleep in short slices); -1 the channel faulted, is not this owner's,
 * or the layer is off or paused; -2 not reached and this channel's interrupts do not arrive: the kernel will not sleep
 * on it. */
static inline int nvf_seq_eval(struct nvf_table *T, unsigned long long owner, unsigned handle, unsigned long long seq,
                               int why, nvf_read_fn read, void *ctx, int *fast)
{
    const int ci = nvf_chan_find(T, owner, handle);
    struct nvf_chan *c;
    *fast = 0;
    if (T->off || T->paused || ci < 0 || !T->c[ci].verified) return -1;
    c = &T->c[ci];
    T->pass++;
    nvf_chan_refresh(T, (unsigned)ci, read, ctx);
    if (c->done >= seq) { nvf_chan_judge(T, (unsigned)ci, why); return 1; }
    if (c->faulted) return -1;
    if (c->found < NVF_TRUST_MIN) { *fast = 1; return 0; }
    return nvf_chan_trusted(c) ? 0 : -2;
}

/* The same loop around nvf_seq_eval: READY reached, DEAD faulted or gone, PENDING the kernel will not sleep on this
 * channel, BUDGET the timeout (clamped to NVRM_FENCE_SEQ_MAX_MS) ran out, ABORTED. */
static inline unsigned nvf_wait_seq(struct nvf_table *T, const struct nvf_wait_ops *ops, void *ctx,
                                    unsigned long long owner, unsigned handle, unsigned long long seq,
                                    unsigned timeoutMs, int abortable, struct NVRMFenceWait *w)
{
    const unsigned long long timeoutNs = (timeoutMs > NVRM_FENCE_SEQ_MAX_MS ? NVRM_FENCE_SEQ_MAX_MS : timeoutMs) * NVF_NS_PER_MS;
    unsigned long long t0, now, dl;
    unsigned k, g, slice = 0, status = NVRM_FENCE_ST_OK;
    int why = NVF_WHY_FIRST, slept = 0, fast = 0, r, s;

    w->result = NVRM_FENCE_R_NONE; w->waitedUs = 0; w->ownerPid = 0; w->state = 0; w->blockers = 0;
    t0 = ops->now(ctx);
    for (;;) {
        k = ops->kick(ctx); g = ops->gen(ctx);
        ops->lock(ctx);
        now = ops->now(ctx);
        if (why == NVF_WHY_FIRST) T->stat[NVF_STAT_SEQ_WAITS]++;
        nvf_wake_count(T, why);
        s = nvf_seq_eval(T, owner, handle, seq, why, ops->read, ctx, &fast);
        if (s == 1) w->result = NVRM_FENCE_R_READY;
        else if (s == -1) w->result = NVRM_FENCE_R_DEAD;
        else if (s == -2) { w->result = NVRM_FENCE_R_PENDING; T->stat[NVF_STAT_SEQ_REFUSED]++; }
        else if (now - t0 >= timeoutNs) w->result = NVRM_FENCE_R_BUDGET;
        if (s != 0 || now - t0 >= timeoutNs) { ops->unlock(ctx); break; }
        if (!slept) T->stat[NVF_STAT_SEQ_SLEPT]++;
        slice = nvf_slice_us(slice, why, fast);
        ops->unlock(ctx);
        dl = t0 + timeoutNs;
        if (now + slice * 1000ull < dl) dl = now + slice * 1000ull;
        r = ops->sleep(ctx, k, g, dl, abortable);
        slept = 1;
        if (r != NVF_SLEPT) {
            w->result = r == NVF_SLEEP_ABORTED ? NVRM_FENCE_R_ABORTED : NVRM_FENCE_R_PENDING;
            if (r == NVF_SLEEP_FULL) status = NVRM_FENCE_ST_FULL;
            now = ops->now(ctx);
            break;
        }
        why = ops->kick(ctx) != k ? NVF_WHY_IRQ : ops->gen(ctx) != g ? NVF_WHY_STATE : NVF_WHY_SLICE;
    }
    w->waitedUs = (now - t0) / 1000ull;
    return status;
}

#endif
