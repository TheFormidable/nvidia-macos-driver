/* test_nvrm_atomic_pool.c - the allocator NVRM uses for the RM's allocations made where the kernel heap may not be
   called (kexts/NVRM/nvrm_atomic_pool.h), on the host: random allocate/free against a shadow map (no two live blocks
   overlap, every payload is 16-byte aligned and wholly inside the pool, every byte keeps what was written to it), a
   pool emptied in any order is one block again, and pointers that are not live blocks are refused.
   Build and run anywhere:
     cc -std=c11 -O2 -Wall -Wextra -Werror -I kexts/NVRM tools/test_nvrm_atomic_pool.c -o /tmp/test_nvrm_atomic_pool && /tmp/test_nvrm_atomic_pool */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include "nvrm_atomic_pool.h"

static int fails;
#define T(name, cond) do { if (!(cond)) { printf("FAIL %s\n", name); fails++; } else printf("ok   %s\n", name); } while (0)

static unsigned long long rng = 0x2545f4914f6cdd1dull;
static unsigned rnd(unsigned n) { rng ^= rng << 13; rng ^= rng >> 7; rng ^= rng << 17; return (unsigned)(rng % n); }

#define POOL (256u * 1024u)
static _Alignas(4096) unsigned char backing[POOL + 8192];   /* the pool sits inside, with room on both sides */
static unsigned char *const mem = backing + 4096;
static unsigned char owner[POOL];          /* which live slot (1..) owns each byte; 0 = none */
struct live { unsigned char *p; unsigned size; unsigned char tag; };
static struct live L[4096];
static unsigned nl;

static unsigned count_free_blocks(const struct nvap_pool *p, unsigned *largest)
{
    unsigned n = 0;
    *largest = 0;
    for (struct nvap_free *f = p->free; f; f = f->next) { n++; if (f->size > *largest) *largest = f->size; }
    return n;
}
static int free_list_ordered_and_apart(const struct nvap_pool *p)
{
    for (struct nvap_free *f = p->free; f && f->next; f = f->next)
        if (!((unsigned char *)f + f->size < (unsigned char *)f->next)) return 0;   /* ordered, and never touching */
    return 1;
}

/* One random run. `size_cap` bounds requests. Returns disagreements. */
static int run(struct nvap_pool *p, unsigned steps, unsigned size_cap, unsigned *allocs, unsigned *misses)
{
    int bad = 0;
    nvap_init(p, mem, POOL);
    memset(owner, 0, sizeof owner);
    nl = 0;
    *allocs = *misses = 0;
    for (unsigned s = 0; s < steps; s++) {
        if (nl < 4000 && (nl == 0 || rnd(100) < 55)) {
            unsigned size = 1 + rnd(rnd(8) == 0 ? size_cap : 512);
            unsigned char *q = (unsigned char *)nvap_alloc(p, size);
            if (!q) { (*misses)++; continue; }
            (*allocs)++;
            if (((uintptr_t)q & 15u) != 0) bad++;
            if (q < mem || q + size > mem + POOL) { bad++; continue; }
            for (unsigned i = 0; i < size; i++) if (owner[q - mem + i]) { bad++; break; }
            unsigned char tag = (unsigned char)(1 + rnd(250));
            for (unsigned i = 0; i < size; i++) owner[q - mem + i] = 1;
            memset(q, tag, size);
            L[nl].p = q; L[nl].size = size; L[nl].tag = tag; nl++;
        } else {
            unsigned k = rnd(nl);
            struct live v = L[k];
            for (unsigned i = 0; i < v.size; i++) if (v.p[i] != v.tag) { bad++; break; }
            if (nvap_free(p, v.p) != 0) bad++;
            for (unsigned i = 0; i < v.size; i++) owner[v.p - mem + i] = 0;
            L[k] = L[--nl];
        }
        if (s % 997 == 0 && !free_list_ordered_and_apart(p)) bad++;
    }
    return bad;
}

int main(void)
{
    struct nvap_pool p;
    unsigned largest, allocs, misses;

    nvap_init(&p, mem, POOL);
    T("a new pool is one free block of the whole size", count_free_blocks(&p, &largest) == 1 && largest == POOL && p.used == 0);
    T("size 0 is refused", nvap_alloc(&p, 0) == NULL);
    T("more than the pool is refused", nvap_alloc(&p, POOL) == NULL && nvap_alloc(&p, POOL - 16) != NULL);
    nvap_init(&p, mem, POOL);
    void *a = nvap_alloc(&p, 1), *b = nvap_alloc(&p, 16), *c = nvap_alloc(&p, 17);
    T("blocks are 32-byte multiples with a 16-byte header", (unsigned char *)b - (unsigned char *)a == 32
      && (unsigned char *)c - (unsigned char *)b == 32 && p.used == 32 + 32 + 64 && p.live == 3);
    T("the first payload is 16 bytes into the pool", (unsigned char *)a == mem + 16);
    T("nvap_owns is exact at both ends", nvap_owns(&p, mem) && nvap_owns(&p, mem + POOL - 1) && !nvap_owns(&p, mem + POOL)
      && !nvap_owns(&p, mem - 1));
    T("a pointer outside the pool is refused", nvap_free(&p, mem + POOL) == -1 && nvap_free(&p, (void *)&p) == -1);
    T("a pointer inside a block but not its start is refused", nvap_free(&p, (unsigned char *)b + 16) == -1
      && nvap_free(&p, (unsigned char *)b + 1) == -1 && nvap_free(&p, mem) == -1);
    T("a live block frees once", nvap_free(&p, b) == 0 && p.live == 2);
    T("the same pointer a second time is refused and changes nothing", nvap_free(&p, b) == -1 && p.live == 2 && p.used == 32 + 64);
    T("first fit gives the freed hole back to a request that fits", nvap_alloc(&p, 8) == b);
    T("free the neighbours: merging leaves one block", nvap_free(&p, a) == 0 && nvap_free(&p, c) == 0 && nvap_free(&p, b) == 0
      && count_free_blocks(&p, &largest) == 1 && largest == POOL && p.used == 0 && p.live == 0);
    T("a pointer merged away is refused", nvap_free(&p, c) == -1 && nvap_free(&p, a) == -1);
    /* exhaustion */
    {
        unsigned n = 0;
        while (nvap_alloc(&p, 4000)) n++;
        T("an exhausted pool answers NULL, it does not hand out what it does not have", n == POOL / 4032
          && nvap_alloc(&p, 4000) == NULL);
        T("the peak is kept", p.peak == n * 4032 && p.peak <= POOL);
    }
    /* random */
    {
        int bad = run(&p, 400000, 70000, &allocs, &misses);
        T("400000 random steps: no overlap, aligned, inside, contents kept, list ordered and merged", bad == 0);
        T("the run filled the pool and missed some large requests", allocs > 100000 && misses > 0);
        while (nl) { if (nvap_free(&p, L[nl - 1].p) != 0) bad++; nl--; }
        T("everything freed in random order: one block of the original size again", bad == 0
          && count_free_blocks(&p, &largest) == 1 && largest == POOL && p.used == 0 && p.live == 0);
    }
    /* control: a defect in the allocator must show. Two blocks handed out over each other (the split forgotten) is the
       failure this test exists for; simulate it by putting a live block's header back on the free list. */
    {
        unsigned bad_seen = 0;
        nvap_init(&p, mem, POOL);
        unsigned char *x = (unsigned char *)nvap_alloc(&p, 100);
        memset(x, 0x5a, 100);
        struct nvap_free *h = (struct nvap_free *)(void *)(x - 16);
        h->magic = NVAP_MAGIC_FREE; h->next = p.free; p.free = h;   /* the defect */
        unsigned char *y = (unsigned char *)nvap_alloc(&p, 50);
        memset(y, 0xa5, 50);
        for (unsigned i = 0; i < 100; i++) if (x[i] != 0x5a) { bad_seen = 1; break; }
        T("control: a block handed out twice is seen through the contents check", bad_seen && y == x);
    }
    printf(fails ? "\n%d failed\n" : "\nall passed\n", fails);
    return fails ? 1 : 0;
}
