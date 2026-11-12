/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

/* nvrm_atomic_pool.h - a small first-fit allocator over one fixed block of memory, for the RM's allocations made where
 * the kernel heap may not be called (with interrupts or preemption off: under the RM's own spinlocks). The kernel's
 * allocator asserts on such a call and can block when a zone has to grow; this pool never calls anything. The caller
 * serialises every call with one lock; nothing here sleeps or allocates.
 *
 * Layout: the free list is kept in address order; a block is a 16-byte header followed by its payload, and every block
 * is a multiple of 32 bytes, so with a 16-byte aligned base every payload is 16-byte aligned. Allocation is first fit
 * and splits the block it takes; a free merges with both neighbours, so a pool whose blocks have all been freed is one
 * block again. A header carries a magic word: a pointer that is not the start of a live block (outside the pool, not
 * on a block boundary, already freed) is refused instead of corrupting the list.
 * tools/test_nvrm_atomic_pool.c runs this same code on the host. Plain C, compiles as C99 and as C++. */
#ifndef NVRM_ATOMIC_POOL_H
#define NVRM_ATOMIC_POOL_H

#define NVAP_HDR      16u
#define NVAP_GRANULE  32u
#define NVAP_MAGIC_USED 0x4e564150u   /* "NVAP" */
#define NVAP_MAGIC_FREE 0x4e564146u   /* "NVAF" */

/* Every block starts with this header (16 bytes on a 64-bit build); `next` is used only while the block is free. */
struct nvap_free { unsigned size; unsigned magic; struct nvap_free *next; };
struct nvap_pool {
    unsigned char *base;
    unsigned bytes;              /* a multiple of NVAP_GRANULE */
    struct nvap_free *free;      /* free blocks, lowest address first */
    unsigned used, peak, live;   /* bytes in live blocks (headers included), the most there ever were, live blocks */
};

/* `mem` must be 16-byte aligned; its size is rounded down to a multiple of 32. */
static inline void nvap_init(struct nvap_pool *p, void *mem, unsigned bytes)
{
    p->base = (unsigned char *)mem;
    p->bytes = bytes & ~(NVAP_GRANULE - 1u);
    p->used = p->peak = p->live = 0;
    p->free = 0;
    if (p->bytes >= NVAP_GRANULE) {
        struct nvap_free *f = (struct nvap_free *)(void *)p->base;
        f->size = p->bytes;
        f->magic = NVAP_MAGIC_FREE;
        f->next = 0;
        p->free = f;
    }
}

static inline int nvap_owns(const struct nvap_pool *p, const void *ptr)
{
    const unsigned char *c = (const unsigned char *)ptr;
    return p->base && c >= p->base && c < p->base + p->bytes;
}

/* NULL when size is 0 or nothing large enough is free. The payload is not cleared. */
static inline void *nvap_alloc(struct nvap_pool *p, unsigned size)
{
    struct nvap_free **link, *f;
    unsigned need;
    if (size == 0 || size > p->bytes) return 0;
    need = (size + NVAP_HDR + NVAP_GRANULE - 1u) & ~(NVAP_GRANULE - 1u);
    for (link = &p->free; (f = *link) != 0; link = &f->next) {
        if (f->size < need) continue;
        if (f->size - need >= NVAP_GRANULE) {
            /* split: the front is handed out, the rest takes its place in the list (the order is kept) */
            struct nvap_free *rest = (struct nvap_free *)(void *)((unsigned char *)f + need);
            rest->size = f->size - need;
            rest->magic = NVAP_MAGIC_FREE;
            rest->next = f->next;
            *link = rest;
            f->size = need;
        } else {
            *link = f->next;
        }
        f->magic = NVAP_MAGIC_USED;
        f->next = 0;
        p->used += f->size;
        if (p->used > p->peak) p->peak = p->used;
        p->live++;
        return (unsigned char *)f + NVAP_HDR;
    }
    return 0;
}

/* 0 freed; -1 the pointer is not a live block of this pool (nothing changed). */
static inline int nvap_free(struct nvap_pool *p, void *ptr)
{
    unsigned char *c = (unsigned char *)ptr;
    struct nvap_free *b, *prev, *next, **link;
    unsigned long off;
    if (!nvap_owns(p, ptr) || c < p->base + NVAP_HDR) return -1;
    off = (unsigned long)(c - NVAP_HDR - p->base);
    if (off % NVAP_GRANULE) return -1;
    b = (struct nvap_free *)(void *)(c - NVAP_HDR);
    if (b->magic != NVAP_MAGIC_USED || b->size < NVAP_GRANULE || b->size % NVAP_GRANULE || b->size > p->bytes - off)
        return -1;
    p->used -= b->size;
    p->live--;
    /* insert in address order */
    prev = 0;
    for (link = &p->free; (next = *link) != 0 && next < b; link = &next->next) prev = next;
    b->magic = NVAP_MAGIC_FREE;
    b->next = next;
    *link = b;
    /* merge with the block after it, then with the one before it; a merged-away header is cleared, so a second free of
     * the same pointer is refused */
    if (next && (unsigned char *)b + b->size == (unsigned char *)next) {
        b->size += next->size;
        b->next = next->next;
        next->magic = 0;
    }
    if (prev && (unsigned char *)prev + prev->size == (unsigned char *)b) {
        prev->size += b->size;
        prev->next = b->next;
        b->magic = 0;
    }
    return 0;
}

#endif
