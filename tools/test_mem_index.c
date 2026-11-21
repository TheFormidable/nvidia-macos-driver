/* test_mem_index.c - the memory table's hash index (nvmtl_mem.h), proven on the host against the backward scan it replaced.
   Build and run anywhere (no GPU):  cc -O2 -I plugin tools/test_mem_index.c -o /tmp/test_mem_index && /tmp/test_mem_index
   Each arm is a sentence about what must hold. */
#include "nvmtl_mem.h"
#include <signal.h>
#include <unistd.h>

static int fails;
#define ARM(name, cond) do { const int ok = !!(cond); printf("%s  %s\n", ok ? "ok  " : "FAIL", name); if (!ok) fails++; } while (0)

static uint64_t rng = 0x9e3779b97f4a7c15ull;
static uint32_t rnd(uint32_t n) { rng ^= rng << 13; rng ^= rng >> 7; rng ^= rng << 17; return (uint32_t)(rng % n); }

/* the table before the index: a backward scan of every record */
static const nvmtl_mm_entry *mm_scan(const nvmtl_mm_state *s, const void *mem) { if (!mem) return NULL; for (uint32_t i = s->n; i-- > 0;) if (s->e[i].mem == mem) return &s->e[i]; return NULL; }
/* nvmtl_mm_died as it was, over the scan: the reference the indexed table must track step by step */
static void mm_died_scan(nvmtl_mm_state *s, const void *mem, uint64_t seq) { nvmtl_mm_entry *e = (nvmtl_mm_entry *)mm_scan(s, mem); if (e && !e->died) { e->died = seq ? seq : 1; s->live--; } }

#define H(x) ((const void *)(uintptr_t)(x))

int main(void)
{
    alarm(5);   /* a chain walk that never ends fails the run instead of hanging it */

    /* 1. the index beside the scan, with handles the driver reissues */
    {   nvmtl_mm_state *m = calloc(1, sizeof *m), *r = calloc(1, sizeof *r); int good = 1, same_tables = 1; uint64_t seq = 1;
        for (int i = 0; i < 40000; i++) {
            const void *h = H(0x1000 + 16 * (uintptr_t)rnd(3000));   /* a small pool: handles come back, live or dead */
            if (rnd(3)) { nvmtl_mm_add(m, h, 0x100000000ull + (uint64_t)i * 65536, 65536, 0, "t", seq); nvmtl_mm_add(r, h, 0x100000000ull + (uint64_t)i * 65536, 65536, 0, "t", seq); }
            else { nvmtl_mm_died(m, h, seq); mm_died_scan(r, h, seq); }
            seq++;
            if ((i & 255) == 0) for (int k = 0; k < 8; k++) { const void *q = H(0x1000 + 16 * (uintptr_t)rnd(3100)); if (nvmtl_mm_by_mem(m, q) != mm_scan(m, q)) good = 0; }
        }
        for (uint32_t k = 0; k < 3100; k++) { const void *q = H(0x1000 + 16 * (uintptr_t)k); if (nvmtl_mm_by_mem(m, q) != mm_scan(m, q)) good = 0; }
        if (m->n != r->n || m->live != r->live || m->unbased != r->unbased) same_tables = 0;
        for (uint32_t i = 0; same_tables && i < m->n; i++) if (m->e[i].mem != r->e[i].mem || m->e[i].died != r->e[i].died || m->e[i].seq != r->e[i].seq) same_tables = 0;
        ARM("the memory index returns the very record the backward scan returns, for every handle, reused or never seen", good);
        ARM("  a table that marks deaths through the index ends record for record as the scanning table does", same_tables);
        ARM("  a NULL handle is never found", nvmtl_mm_by_mem(m, NULL) == NULL);
        ARM("  the entry is still 88 bytes (the chain fits the padding)", sizeof(nvmtl_mm_entry) == 88);
        free(m->e); free(m); free(r->e); free(r); }

    /* 2. an old heap memory made first and looked up after 100000 others: found, and the address lookup is unchanged */
    {   nvmtl_mm_state *m = calloc(1, sizeof *m); uint64_t seq = 1;
        nvmtl_mm_add(m, H(0xaa0), 0x2ef000000ull, 1ull << 30, 0, "heap", seq++);
        for (uint32_t i = 0; i < 100000; i++) { nvmtl_mm_add(m, H(0x40000000 + 16 * (uintptr_t)i), 0x400000000ull + (uint64_t)i * 65536, 65536, 0, "own", seq++); nvmtl_mm_died(m, H(0x40000000 + 16 * (uintptr_t)i), seq++); }
        const nvmtl_mm_entry *e = nvmtl_mm_by_mem(m, H(0xaa0));
        ARM("an old heap memory is found alive after 100000 younger memories died", e && e == &m->e[0] && !e->died && m->live == 1);
        char l[300]; nvmtl_mm_describe(m, 0x2ef000000ull + 77, l, sizeof l);
        ARM("  and an address in it is described as before", strstr(l, "= heap memory 0xaa0") && strstr(l, "+ offset 77"));
        free(m->e); free(m); }

    /* 3. a chain link that does not point to an older entry (an append half seen by a reader without the lock) ends the walk */
    {   nvmtl_mm_state *m = calloc(1, sizeof *m);
        nvmtl_mm_add(m, H(0x1000), 0, 4096, 0, "a", 1);
        const uint32_t b = nvmtl_mm_bucket(H(0x1000)); uint32_t other = 0x2000; while (nvmtl_mm_bucket(H(other)) != b) other += 16;
        m->e[0].chain = 1;   /* a link to itself */
        ARM("a walk over a link to itself ends without a match", nvmtl_mm_by_mem(m, H(other)) == NULL && nvmtl_mm_by_mem(m, H(0x1000)) == &m->e[0]);
        free(m->e); free(m); }

    printf("test_mem_index: %d failed\n", fails);
    return fails ? 1 : 0;
}
