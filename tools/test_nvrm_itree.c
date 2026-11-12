/* test_nvrm_itree.c - the index of kernel mappings (kexts/NVRM/nvrm_itree.h) against the table it replaces. The index has
   no switch to turn it off and no fallback, so this comparison is its safety net: a copy of the 2.9 table of
   os-xnu.cpp (fixed array, first free slot, linear scans) and the tree-backed table of today's os-xnu.cpp run the same
   random operations, more than a million of them, over ranges that overlap, touch, nest, repeat and have length 0,
   and must give the same answer to every query. The tree's own invariants are checked as it goes. The controls at the
   end show that the comparison catches a broken tree and a wrong boundary.
   Build and run anywhere:
     cc -std=c11 -O2 -Wall -Wextra -Werror -I kexts/NVRM tools/test_nvrm_itree.c -o /tmp/test_nvrm_itree && /tmp/test_nvrm_itree */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stddef.h>
#include "nvrm_itree.h"

static int fails;
#define T(name, cond) do { if (!(cond)) { printf("FAIL %s\n", name); fails++; } else printf("ok   %s\n", name); } while (0)

static unsigned long long rng = 0x853c49e6748fea9bull;
static unsigned long long r64(void) { rng ^= rng << 13; rng ^= rng >> 7; rng ^= rng << 17; return rng; }
static unsigned rnd(unsigned n) { return (unsigned)(r64() % n); }

typedef unsigned long long u64;

/* ---- the 2.9 table, as os-xnu.cpp had it (va, size, phys, alias), without the IOKit parts ---- */
#define MAXMAPS 8192
struct old_map { u64 va, size, phys; int alias; };
static struct old_map oldt[MAXMAPS];
static unsigned old_high;
static int old_closed_end;   /* control: a table whose ranges include their end */

static int old_map(u64 va, u64 size, u64 phys, int alias)
{
    for (unsigned i = 0; i < MAXMAPS; i++) if (oldt[i].va == 0) {
        oldt[i].va = va; oldt[i].size = size; oldt[i].phys = phys; oldt[i].alias = alias;
        if (i + 1 > old_high) old_high = i + 1;
        return 1;
    }
    return 0;   /* full: 2.9 returned the mapping untracked */
}
static int old_phys_for(u64 va, u64 *out)
{
    for (unsigned i = 0; i < old_high; i++) {
        u64 base = oldt[i].va;
        if (base && va >= base && (old_closed_end ? va <= base + oldt[i].size : va < base + oldt[i].size)) {
            *out = oldt[i].phys + (va - base);
            return 1;
        }
    }
    return 0;
}
static int old_unmap(u64 va)
{
    for (unsigned i = 0; i < old_high; i++) if (oldt[i].va == va) { memset(&oldt[i], 0, sizeof oldt[i]); return 1; }
    return 0;
}
static int old_overlap(u64 phys, u64 len)
{
    for (unsigned i = 0; i < old_high; i++) {
        if (!oldt[i].va || oldt[i].alias) continue;
        if (phys < oldt[i].phys + oldt[i].size && oldt[i].phys < phys + len) return 1;
    }
    return 0;
}

/* ---- today's table: two trees over records, as os-xnu.cpp keeps them ---- */
struct new_map { struct nvit_node va_node, pa_node; u64 va, size, phys; int alias, live; };
static struct new_map pool[200000];
static unsigned pool_n;
static struct new_map *freel[200000];
static unsigned freel_n;
static struct nvit_tree by_va, by_pa;
#define PA_OF(n) ((struct new_map *)(void *)((char *)(n) - offsetof(struct new_map, pa_node)))

static struct new_map *new_alloc(void) { return freel_n ? freel[--freel_n] : &pool[pool_n++]; }
static void new_map(u64 va, u64 size, u64 phys, int alias)
{
    struct new_map *m = new_alloc();
    memset(m, 0, sizeof *m);
    m->va = va; m->size = size; m->phys = phys; m->alias = alias; m->live = 1;
    m->va_node.lo = va; m->va_node.end = va + size;
    nvit_insert(&by_va, &m->va_node);
    if (!alias) { m->pa_node.lo = phys; m->pa_node.end = phys + size; nvit_insert(&by_pa, &m->pa_node); }
}
static int new_phys_for(u64 va, u64 *out)
{
    struct nvit_node *n = nvit_stab(&by_va, va);
    if (!n) return 0;
    struct new_map *m = (struct new_map *)(void *)n;
    *out = m->phys + (va - m->va);
    return 1;
}
/* os_unmap_kernel_space: the entry that starts at va, preferring one of the size given */
static struct new_map *new_find_unmap(u64 va, u64 size)
{
    struct nvit_node *n = nvit_first_lo(&by_va, va), *first = n;
    for (; n && n->lo == va; n = nvit_next(n)) if (((struct new_map *)(void *)n)->size == size) return (struct new_map *)(void *)n;
    return first ? (struct new_map *)(void *)first : NULL;
}
static int new_unmap(u64 va, u64 size)
{
    struct new_map *m = new_find_unmap(va, size);
    if (!m) return 0;
    nvit_remove(&by_va, &m->va_node);
    if (!m->alias) nvit_remove(&by_pa, &m->pa_node);
    m->live = 0;
    freel[freel_n++] = m;
    return 1;
}
static int new_overlap(u64 phys, u64 len) { return nvit_overlap(&by_pa, phys, phys + len) != NULL; }

/* ---- the run ---- */
struct rec { u64 va, size, phys; int alias; };
static struct rec live[MAXMAPS];
static unsigned nlive;
static long mismatches, invalid, checks_bad;
static int consistent;    /* phys = va + K for every entry: then the phys answers must be equal, not only both found */
static const u64 K = 0x100000000ull;

static void reset(void)
{
    memset(oldt, 0, sizeof oldt); old_high = 0;
    by_va.root = by_pa.root = NULL; by_va.count = by_pa.count = 0;
    pool_n = freel_n = 0; nlive = 0;
    mismatches = invalid = checks_bad = 0;
}

static u64 pick_addr(void)
{
    if (nlive && rnd(3) == 0) {   /* a boundary of a live range: its start, last byte, end, end + 1 */
        struct rec *r = &live[rnd(nlive)];
        switch (rnd(4)) { case 0: return r->va; case 1: return r->va + r->size - 1; case 2: return r->va + r->size; default: return r->va - 1; }
    }
    return 0x1000 + (u64)rnd(1u << 16);
}

static void op_map(void)
{
    struct rec r;
    if (nlive && rnd(10) == 0) {
        r = live[rnd(nlive)];        /* the same range again: two aliases of one allocation */
    } else {
        unsigned sz = rnd(8) == 0 ? 0 : (rnd(4) == 0 ? 1 + rnd(16) : 16 * (1 + rnd(256)));
        if (nlive && rnd(4) == 0) { struct rec *a = &live[rnd(nlive)]; r.va = a->va + a->size; }   /* touching */
        else if (nlive && rnd(5) == 0) { struct rec *a = &live[rnd(nlive)]; r.va = a->va + (a->size ? rnd((unsigned)a->size) : 0); }  /* nested / overlapping */
        else r.va = 0x1000 + (u64)rnd(1u << 16);
        r.size = sz;
        r.alias = rnd(3) == 0;
        r.phys = consistent ? r.va + K : (u64)0x200000 + (u64)rnd(1u << 16);
    }
    if (nlive >= MAXMAPS - 1) return;
    /* Two different ranges never start at one address here: 2.9's unmap took whichever came first in its array and
       today's takes the one of the size it is given, so the two tables would legitimately go apart. Repeats of one
       range (aliases) do start at one address; that choice is tested on its own above. */
    for (unsigned i = 0; i < nlive; i++)
        if (live[i].va == r.va && (live[i].size != r.size || live[i].phys != r.phys || live[i].alias != r.alias)) return;
    if (!old_map(r.va, r.size, r.phys, r.alias)) return;
    new_map(r.va, r.size, r.phys, r.alias);
    live[nlive++] = r;
}

static void op_unmap(void)
{
    if (!nlive) { u64 va = pick_addr(); if (old_unmap(va) != new_unmap(va, 0)) mismatches++; return; }
    unsigned k = rnd(nlive);
    struct rec r = live[k];
    /* 2.9 removed the first slot with that va; the new one prefers the entry of that size. Entries that share a va in
       this run are copies of one range, so either choice leaves the same set. */
    if (old_unmap(r.va) != new_unmap(r.va, r.size)) mismatches++;
    live[k] = live[--nlive];
}

static void op_query(void)
{
    u64 x = pick_addr(), a = 0, b = 0;
    int fa = old_phys_for(x, &a), fb = new_phys_for(x, &b);
    if (fa != fb) mismatches++;
    else if (fb) {
        /* the tree's answer must come from a range that contains x */
        struct nvit_node *n = nvit_stab(&by_va, x);
        if (!(n->lo <= x && x < n->end)) invalid++;
        if (consistent && a != b) mismatches++;
    }
    u64 p = consistent ? x + K : (u64)0x200000 + (u64)rnd(1u << 16);
    if (nlive && rnd(3) == 0) { struct rec *r = &live[rnd(nlive)]; p = r->phys + (rnd(2) ? r->size : 0) - rnd(2); }
    u64 len = rnd(6) == 0 ? 0 : (u64)rnd(4096);
    if (old_overlap(p, len) != new_overlap(p, len)) mismatches++;
}

static long run(long ops, int check_every, unsigned target_live)
{
    long done = 0;
    for (long i = 0; i < ops; i++, done++) {
        unsigned c = rnd(100);
        if (nlive < target_live ? c < 45 : c < 25) op_map();
        else if (c < 55) op_unmap();
        else op_query();
        if (check_every && i % check_every == 0) {
            if (nvit_check(&by_va) || nvit_check(&by_pa)) checks_bad++;
            if (by_va.count != nlive) checks_bad++;
        }
    }
    return done;
}

int main(void)
{
    long total = 0;
    struct nvit_tree t = { NULL, 0 };
    struct nvit_node a, b, c, z;

    /* small, exact cases */
    a.lo = 100; a.end = 200; b.lo = 200; b.end = 300; c.lo = 150; c.end = 160; z.lo = 250; z.end = 250;
    nvit_insert(&t, &a); nvit_insert(&t, &b); nvit_insert(&t, &c); nvit_insert(&t, &z);
    T("a range contains its start and not its end", nvit_stab(&t, 100) == &a && nvit_stab(&t, 199) == &a && nvit_stab(&t, 200) == &b
      && nvit_stab(&t, 99) == NULL && nvit_stab(&t, 300) == NULL);
    T("of two ranges that contain x, the one that starts first answers", nvit_stab(&t, 155) == &a);
    T("a range of length 0 contains nothing", nvit_stab(&t, 250) == &b);
    T("touching ranges do not overlap; one byte in does", nvit_overlap(&t, 300, 400) == NULL && nvit_overlap(&t, 299, 400) == &b
      && nvit_overlap(&t, 0, 100) == NULL && nvit_overlap(&t, 0, 101) == &a);
    T("a query of length 0 inside a range overlaps it, as the old formula said", nvit_overlap(&t, 120, 120) == &a
      && nvit_overlap(&t, 100, 100) == NULL);
    T("a range of length 0 strictly inside a query overlaps it, as the old formula said", nvit_overlap(&t, 240, 260) == &b);
    T("the first range at a start is found, also one of length 0", nvit_first_lo(&t, 150) == &c && nvit_first_lo(&t, 250) == &z
      && nvit_first_lo(&t, 151) == NULL);
    T("in-order walk visits all four by start", nvit_next(&a) == &c && nvit_next(&c) == &b && nvit_next(&b) == &z && nvit_next(&z) == NULL);
    nvit_remove(&t, &a);
    T("after a removal: invariants hold and the gap is a gap", nvit_check(&t) == 0 && t.count == 3 && nvit_stab(&t, 120) == NULL
      && nvit_stab(&t, 155) == &c);
    nvit_remove(&t, &b); nvit_remove(&t, &c); nvit_remove(&t, &z);
    T("empty again", t.root == NULL && t.count == 0 && nvit_check(&t) == 0);
    /* repeats keep insertion order, and unmap prefers the size it is given */
    {
        reset();
        new_map(0x5000, 0x1000, 0x9000, 1); new_map(0x5000, 0x2000, 0xa000, 1); new_map(0x5000, 0x1000, 0xb000, 1);
        struct new_map *m = new_find_unmap(0x5000, 0x2000);
        T("unmap takes the range of the size it was given", m && m->size == 0x2000);
        m = new_find_unmap(0x5000, 0x3000);
        T("unmap of a size no range has takes the first at that address", m && m->phys == 0x9000);
        T("repeats of one start come out in insertion order", ((struct new_map *)(void *)nvit_first_lo(&by_va, 0x5000))->phys == 0x9000
          && ((struct new_map *)(void *)nvit_next(nvit_first_lo(&by_va, 0x5000)))->phys == 0xa000);
        T("an address nobody mapped is not unmapped", new_unmap(0x6000, 0x1000) == 0 && by_va.count == 3);
    }

    /* phase 1: small tables, invariants checked after every operation */
    reset(); consistent = 1;
    total += run(150000, 1, 64);
    long m1 = mismatches, i1 = invalid, c1 = checks_bad;
    reset(); consistent = 0;
    total += run(150000, 1, 300);
    T("300000 operations on small tables, invariants checked after each: same answers as the 2.9 table",
      m1 == 0 && i1 == 0 && c1 == 0 && mismatches == 0 && invalid == 0 && checks_bad == 0);
    /* phase 2: a million operations on tables of a few thousand ranges */
    reset(); consistent = 1;
    total += run(600000, 4096, 2500);
    long m2 = mismatches, i2 = invalid, c2 = checks_bad;
    reset(); consistent = 0;
    total += run(600000, 4096, 2500);
    T("1200000 operations on tables of about 2500 ranges: same answers as the 2.9 table", m2 == 0 && i2 == 0 && c2 == 0
      && mismatches == 0 && invalid == 0 && checks_bad == 0);
    /* phase 3: 20000 ranges, then removal in random order down to empty */
    {
        reset();
        int ok = 1;
        static struct nvit_node big[20000];
        struct nvit_tree bt = { NULL, 0 };
        for (unsigned i = 0; i < 20000; i++) { big[i].lo = (u64)rnd(1u << 20); big[i].end = big[i].lo + rnd(512); nvit_insert(&bt, &big[i]); }
        if (nvit_check(&bt) || bt.count != 20000 || bt.root->h > 21) ok = 0;   /* AVL: height <= 1.44 log2(n) */
        unsigned order[20000];
        for (unsigned i = 0; i < 20000; i++) order[i] = i;
        for (unsigned i = 19999; i > 0; i--) { unsigned j = rnd(i + 1), tmp = order[i]; order[i] = order[j]; order[j] = tmp; }
        for (unsigned i = 0; i < 20000; i++) {
            nvit_remove(&bt, &big[order[i]]);
            if ((i % 97 == 0 || i > 19900) && nvit_check(&bt)) { ok = 0; break; }
        }
        total += 40000;
        T("20000 ranges: balanced (height at most 21), then removed in random order down to empty", ok && bt.root == NULL && bt.count == 0);
    }
    printf("     (%ld operations in all)\n", total);
    T("more than a million operations were compared", total > 1000000);

    /* controls: the comparison must catch a broken tree and a wrong boundary */
    {
        reset(); consistent = 0;
        run(20000, 0, 2000);
        /* the defect: a subtree's largest end not kept up to date (what a rotation that forgets nvit_fix leaves) */
        struct nvit_node *n = by_va.root;
        n->l->max_end = n->l->end;
        long before = mismatches;
        int chk = nvit_check(&by_va);
        for (int i = 0; i < 20000; i++) op_query();
        T("control: a stale max_end is reported by nvit_check and changes answers", chk == -5 && mismatches > before);
    }
    {
        reset(); consistent = 1;
        run(20000, 0, 500);
        struct nvit_node *n = by_va.root->r ? by_va.root->r : by_va.root->l;
        struct nvit_node *saved = n->p;
        n->p = NULL;
        T("control: a broken parent link is reported", nvit_check(&by_va) == -1);
        n->p = saved;
        T("restored: the check passes again", nvit_check(&by_va) == 0);
    }
    {
        reset(); consistent = 1;
        old_closed_end = 1;
        run(50000, 0, 500);
        old_closed_end = 0;
        T("control: a table whose ranges include their end byte disagrees (touching ranges are exercised)", mismatches > 0);
    }
    printf(fails ? "\n%d failed\n" : "\nall passed\n", fails);
    return fails ? 1 : 0;
}
