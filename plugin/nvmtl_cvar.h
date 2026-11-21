/* nvmtl_cvar.h - a compute kernel's threadgroup-size variants.
   Metal names the threadgroup size at dispatch; the translated kernel reads it from specialization constants 0, 1 and 2
   (3 asks for the linearized form), so each size the application dispatches is a pipeline of its own, compiled the first
   time that size is dispatched and kept in a small table on the kernel's pipeline (nvmtl_compute_variant).
   The table is shared by every thread that dispatches the kernel and guarded by one process-wide lock. The compile is
   spirv_to_nir plus the GPU compiler, so holding that lock across it stalled every other thread's dispatch of any kernel
   for as long as one variant took to compile. The compile is made outside the lock instead; the table is looked at again
   when the result is put in, and a size that another thread put in meanwhile keeps that thread's pipeline, the caller's
   own being destroyed before any command buffer saw it. NVMTL_CVAR_PARALLEL=0 compiles under the lock, as before.
   Pure C so tools/test_cvar.c proves the rules on the host with threads and a stub compiler. */
#ifndef NVMTL_CVAR_H
#define NVMTL_CVAR_H
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>

/* struct nvmtl_cvar { uint32_t l[3]; void *pipe; } is the table's entry, declared with nvk_pipeline (nvmtl_vk.h); the
   includer declares it first (tools/test_cvar.py holds the test's copy equal to that one). */

/* the variant kept for threadgroup size l, or NULL. The caller holds the table's lock. */
static inline void *nvmtl_cvar_find(const struct nvmtl_cvar *var, uint32_t nvar, const uint32_t l[3])
{
    for (uint32_t i = 0; i < nvar; i++) if (!memcmp(var[i].l, l, sizeof var[i].l)) return var[i].pipe;
    return NULL;
}

enum {
    NVMTL_CVAR_ADDED = 0,    /* kept in the table */
    NVMTL_CVAR_GREW = 1,     /* kept, after the table grew (*grown_to is its new capacity) */
    NVMTL_CVAR_FULL = 2,     /* the table is at its limit: not kept, the caller's pipeline leaks as before */
    NVMTL_CVAR_TAKEN = 3,    /* another thread put this size in first: *winner is its pipeline, the caller's is not kept */
};

/* Puts the caller's freshly compiled pipe for size l in the table. recheck is set when the compile ran outside the lock:
   another thread may have put the same size in since the caller missed it, and then that one stays (a size is in the
   table at most once, and every caller of that size gets the same pipeline). The growth is the one the table always had:
   first_cap entries, doubled while under max_cap. The caller holds the table's lock. */
static inline int nvmtl_cvar_insert(struct nvmtl_cvar **var, uint32_t *nvar, uint32_t *capvar, const uint32_t l[3], void *pipe,
                                    int recheck, uint32_t first_cap, uint32_t max_cap, void **winner, uint32_t *grown_to)
{
    if (recheck) {
        void *w = nvmtl_cvar_find(*var, *nvar, l);
        if (w) { *winner = w; return NVMTL_CVAR_TAKEN; }
    }
    int grew = 0;
    if (*nvar == *capvar && *capvar < max_cap) {
        uint32_t cap = *capvar ? *capvar * 2 : first_cap;
        struct nvmtl_cvar *g = realloc(*var, cap * sizeof *g);
        if (g) { *var = g; *capvar = cap; grew = cap > first_cap; if (grown_to) *grown_to = cap; }
    }
    if (*nvar >= *capvar) return NVMTL_CVAR_FULL;
    memcpy((*var)[*nvar].l, l, sizeof (*var)[*nvar].l); (*var)[*nvar].pipe = pipe; (*nvar)++;
    return grew ? NVMTL_CVAR_GREW : NVMTL_CVAR_ADDED;
}

#endif
