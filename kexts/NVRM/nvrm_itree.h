/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

/* nvrm_itree.h - an index of address ranges: "which range contains this address" and "does any range overlap this
 * one" in O(log n), for the table of every kernel mapping the RM makes (os-xnu.cpp). The table used to be a fixed
 * array searched from the start: it filled up on machines with a large BAR1 (a mapping that did not fit was handed out
 * untracked and never released), and freeing an allocation of S pieces with N mappings live cost S x N steps.
 *
 * A balanced (AVL) binary tree of half-open ranges [lo, end), ordered by lo, where every node also keeps the largest
 * `end` in its subtree. Ranges with the same lo are kept in the order they were inserted. A range of length 0
 * (end == lo) is stored and can be found by its start (nvit_first_lo) but contains no address. The two queries answer
 * exactly what the linear scans they replace answered:
 *     contains:  lo <= x && x < end
 *     overlaps:  lo < qend && qlo < end      (the same formula, also for a query of length 0)
 * and when several ranges qualify they return the first in order (lowest lo, earliest inserted).
 *
 * Nodes live inside the caller's own records; the tree never allocates. Every call is made with the caller's lock
 * held; nothing here sleeps or calls anything. tools/test_nvrm_itree.c compares this code, operation by operation,
 * with a copy of the old linear table. Plain C, compiles as C99 and as C++. */
#ifndef NVRM_ITREE_H
#define NVRM_ITREE_H

struct nvit_node {
    struct nvit_node *l, *r, *p;
    unsigned long long lo, end;      /* the range [lo, end); set by the caller before nvit_insert */
    unsigned long long max_end;      /* the largest end in this subtree */
    int h;                           /* height of this subtree, a leaf is 1 */
};
struct nvit_tree { struct nvit_node *root; unsigned count; };

static inline int nvit_height(const struct nvit_node *n) { return n ? n->h : 0; }

/* height and max_end of `n` from its children */
static inline void nvit_fix(struct nvit_node *n)
{
    int hl = nvit_height(n->l), hr = nvit_height(n->r);
    unsigned long long m = n->end;
    n->h = 1 + (hl > hr ? hl : hr);
    if (n->l && n->l->max_end > m) m = n->l->max_end;
    if (n->r && n->r->max_end > m) m = n->r->max_end;
    n->max_end = m;
}

static inline void nvit_replace_child(struct nvit_tree *t, struct nvit_node *parent, struct nvit_node *old,
                                      struct nvit_node *n)
{
    if (!parent) t->root = n;
    else if (parent->l == old) parent->l = n;
    else parent->r = n;
    if (n) n->p = parent;
}

static inline struct nvit_node *nvit_rotate_left(struct nvit_tree *t, struct nvit_node *x)
{
    struct nvit_node *y = x->r;
    x->r = y->l;
    if (y->l) y->l->p = x;
    nvit_replace_child(t, x->p, x, y);
    y->l = x;
    x->p = y;
    nvit_fix(x);
    nvit_fix(y);
    return y;
}

static inline struct nvit_node *nvit_rotate_right(struct nvit_tree *t, struct nvit_node *x)
{
    struct nvit_node *y = x->l;
    x->l = y->r;
    if (y->r) y->r->p = x;
    nvit_replace_child(t, x->p, x, y);
    y->r = x;
    x->p = y;
    nvit_fix(x);
    nvit_fix(y);
    return y;
}

/* From `n` up to the root: recompute, and rotate where the two sides differ in height by more than one. Always walks
 * to the root, because max_end can change on every ancestor even where the heights do not. */
static inline void nvit_rebalance(struct nvit_tree *t, struct nvit_node *n)
{
    while (n) {
        int bal;
        nvit_fix(n);
        bal = nvit_height(n->l) - nvit_height(n->r);
        if (bal > 1) {
            if (nvit_height(n->l->l) < nvit_height(n->l->r)) (void)nvit_rotate_left(t, n->l);
            n = nvit_rotate_right(t, n);
        } else if (bal < -1) {
            if (nvit_height(n->r->r) < nvit_height(n->r->l)) (void)nvit_rotate_right(t, n->r);
            n = nvit_rotate_left(t, n);
        }
        n = n->p;
    }
}

/* The caller has set n->lo and n->end. A range with the same lo as others goes after them. */
static inline void nvit_insert(struct nvit_tree *t, struct nvit_node *n)
{
    struct nvit_node *cur = t->root, *parent = 0;
    int left = 0;
    n->l = n->r = 0;
    n->h = 1;
    n->max_end = n->end;
    while (cur) {
        parent = cur;
        left = n->lo < cur->lo;
        cur = left ? cur->l : cur->r;
    }
    n->p = parent;
    if (!parent) t->root = n;
    else if (left) parent->l = n;
    else parent->r = n;
    t->count++;
    nvit_rebalance(t, parent);
}

/* `n` must be in `t`. Its neighbours keep their order. */
static inline void nvit_remove(struct nvit_tree *t, struct nvit_node *n)
{
    struct nvit_node *from;
    if (n->l && n->r) {
        /* the next range in order takes n's place */
        struct nvit_node *s = n->r;
        while (s->l) s = s->l;
        if (s == n->r) {
            from = s;
        } else {
            from = s->p;
            from->l = s->r;
            if (s->r) s->r->p = from;
            s->r = n->r;
            n->r->p = s;
        }
        s->l = n->l;
        n->l->p = s;
        nvit_replace_child(t, n->p, n, s);
    } else {
        struct nvit_node *c = n->l ? n->l : n->r;
        from = n->p;
        nvit_replace_child(t, n->p, n, c);
    }
    n->l = n->r = n->p = 0;
    t->count--;
    nvit_rebalance(t, from);
}

/* The first range (in order) that contains x, or NULL. */
static inline struct nvit_node *nvit_stab(const struct nvit_tree *t, unsigned long long x)
{
    struct nvit_node *n = t->root;
    while (n) {
        /* A range with end > x in the left subtree either contains x or starts after x; in the second case so does
         * everything at and right of n, so nothing contains x at all. Either way the answer is on the left. */
        if (n->l && n->l->max_end > x) { n = n->l; continue; }
        if (n->lo > x) return 0;
        if (x < n->end) return n;
        n = n->r;
    }
    return 0;
}

/* The first range (in order) with lo < qend && qlo < end, or NULL. */
static inline struct nvit_node *nvit_overlap(const struct nvit_tree *t, unsigned long long qlo, unsigned long long qend)
{
    struct nvit_node *n = t->root;
    while (n) {
        if (n->l && n->l->max_end > qlo) { n = n->l; continue; }   /* the same argument as nvit_stab */
        if (!(n->lo < qend)) return 0;
        if (qlo < n->end) return n;
        n = n->r;
    }
    return 0;
}

/* The first range (in order) whose lo is exactly `lo`, or NULL. */
static inline struct nvit_node *nvit_first_lo(const struct nvit_tree *t, unsigned long long lo)
{
    struct nvit_node *n = t->root, *found = 0;
    while (n) {
        if (n->lo < lo) n = n->r;
        else { if (n->lo == lo) found = n; n = n->l; }
    }
    return found;
}

/* The range after `n` in order, or NULL. */
static inline struct nvit_node *nvit_next(struct nvit_node *n)
{
    if (n->r) { n = n->r; while (n->l) n = n->l; return n; }
    while (n->p && n->p->r == n) n = n->p;
    return n->p;
}

/* Test hook: 0 when every invariant holds (order, heights and balance, max_end, parent links, count); a negative
 * number naming the first one that does not. Recursive; for host tests only. */
static inline int nvit_check_sub(const struct nvit_node *n, const struct nvit_node *parent, unsigned *count,
                                 const struct nvit_node **prev)
{
    int hl, hr, e;
    unsigned long long m;
    if (!n) return 0;
    if (n->p != parent) return -1;
    if ((e = nvit_check_sub(n->l, n, count, prev)) != 0) return e;
    if (*prev && (*prev)->lo > n->lo) return -2;
    *prev = n;
    (*count)++;
    if ((e = nvit_check_sub(n->r, n, count, prev)) != 0) return e;
    hl = nvit_height(n->l); hr = nvit_height(n->r);
    if (n->h != 1 + (hl > hr ? hl : hr)) return -3;
    if (hl - hr > 1 || hr - hl > 1) return -4;
    m = n->end;
    if (n->l && n->l->max_end > m) m = n->l->max_end;
    if (n->r && n->r->max_end > m) m = n->r->max_end;
    if (n->max_end != m) return -5;
    return 0;
}
static inline int nvit_check(const struct nvit_tree *t)
{
    unsigned count = 0;
    const struct nvit_node *prev = 0;
    int e = nvit_check_sub(t->root, 0, &count, &prev);
    if (e) return e;
    return count == t->count ? 0 : -6;
}

#endif
