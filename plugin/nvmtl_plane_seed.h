/* nvmtl_plane_seed.h - tells our own writes to one plane of an IOSurface apart from anyone else's.
   An IOSurface has one seed for all its planes. Taking its read-write lock moves the seed by exactly one (measured: the
   seed reported at lock time is the new value, and unlock reports the same). Our surface-out of plane 0 of a two-plane
   surface therefore made every other plane's image look stale, and each frame uploaded both planes from the CPU again.
   The ledger records each of our surface-outs as (surface, cookie, plane, seed before, seed after); a wrapper of another
   plane may treat a seed gap as still current when a chain of our own records on other planes covers it exactly.
   Pure: the caller holds its lock and supplies the clock and the surface's cookie (a value stored in the surface, so a
   reused surface id never matches). tools/test_plane_seed.c proves the rules. */
#ifndef NVMTL_PLANE_SEED_H
#define NVMTL_PLANE_SEED_H
#include <stdint.h>
#define NVMTL_PS_SLOTS 64
#define NVMTL_PS_MAX_STEPS 8
typedef struct { uint32_t surf, from, to, plane; uint64_t cookie, at_ns; } nvmtl_ps_rec;
typedef struct { nvmtl_ps_rec r[NVMTL_PS_SLOTS]; unsigned next; } nvmtl_ps;

/* A surface-out took the lock at `lock_seed` (the value the lock reported) and unlocked at `unlock_seed`. Only a lock that
   moved the seed by one and unlocked on that same value is ours alone; anything else is recorded as nothing. */
static inline int nvmtl_ps_record(nvmtl_ps *p, uint32_t surf, uint64_t cookie, uint32_t plane, uint32_t lock_seed, uint32_t unlock_seed, uint64_t now)
{
    if (!cookie || !lock_seed || unlock_seed != lock_seed) return 0;
    p->r[p->next++ % NVMTL_PS_SLOTS] = (nvmtl_ps_rec){ surf, lock_seed - 1, lock_seed, plane, cookie, now };
    return 1;
}
/* 1 when the seed went from `have` to `now` only through our own records on planes other than `plane`, each younger than
   max_age_ns. A record of this same plane ends the chain: those pixels are new to this plane's image. */
static inline int nvmtl_ps_covers(const nvmtl_ps *p, uint32_t surf, uint64_t cookie, uint32_t plane, uint32_t have, uint32_t now_seed,
                                  uint64_t now, uint64_t max_age_ns)
{
    if (!cookie) return 0;
    for (int step = 0; step < NVMTL_PS_MAX_STEPS && have != now_seed; step++) {
        int i = 0;
        for (; i < NVMTL_PS_SLOTS; i++) {
            const nvmtl_ps_rec *e = &p->r[i];
            if (e->surf == surf && e->cookie == cookie && e->to && e->from == have && now >= e->at_ns && now - e->at_ns < max_age_ns) break;
        }
        if (i == NVMTL_PS_SLOTS || p->r[i].plane == plane) return 0;
        have = p->r[i].to;
    }
    return have == now_seed;
}
/* Planes that live in their own VRAM carry a tag in the surface: "this plane's VRAM is current at seed X". Marking one
   plane's VRAM newest in the kernel moves the surface's one seed, so the tags of the other planes fell one seed behind,
   and the plane marked first was locked, paged off and uploaded again at its next use: once per captured frame in
   WindowServer. 1 when the seed went from `before` (read just before the mark) to `after` (read just after it) through
   that mark alone, so that a tag current at `before` may be moved to `after`: the mark took (`marked` 0; 1 is a lock that
   was taken, -1 a refusal, and neither moves the seed) and the seed moved by exactly one. A larger move has someone
   else's write in it, and no move leaves the tags current as they are. */
static inline int nvmtl_ps_mark_moved(int marked, uint32_t before, uint32_t after)
{
    return marked == 0 && (uint32_t)(after - before) == 1u;
}
/* That rule rests on one thing this driver's code cannot show: that every mark that takes moves the seed by exactly
   one. The move is made by the accelerator family (IOSurfaceDeviceCache::setCurrent), not by us. If a mark could take
   without moving the seed, a move by one seen around it would be someone else's write, and following it would call a
   plane's VRAM current over newer CPU pixels. So the planar self-test measures it, in each process, before any tag
   follows: every mark it makes is noted with the seed read just before and just after it (also a mark of VRAM that was
   already newest, as WindowServer's mark at completion is after its mark at commit). The premise holds only when at
   least one mark was noted and every one of them moved the seed by exactly one. The first that did not is kept for
   the log. */
typedef struct { uint32_t marks, by_one, odd_before, odd_after; } nvmtl_ps_premise;
static inline void nvmtl_ps_premise_note(nvmtl_ps_premise *q, uint32_t before, uint32_t after)
{
    q->marks++;
    if ((uint32_t)(after - before) == 1u) { q->by_one++; return; }
    if (q->marks - q->by_one == 1) { q->odd_before = before; q->odd_after = after; }
}
static inline int nvmtl_ps_premise_holds(const nvmtl_ps_premise *q) { return q->marks > 0 && q->by_one == q->marks; }
#endif
