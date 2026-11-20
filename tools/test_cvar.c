/* test_cvar.c - a compute kernel's threadgroup-size variant table (plugin/nvmtl_cvar.h) under threads, with a stub compiler.
   Build and run anywhere (no GPU, no Metal):  cc -std=c11 -O1 -pthread -Wall -Wextra -I plugin tools/test_cvar.c -o /tmp/test_cvar && /tmp/test_cvar
   variant() below is nvmtl_compute_variant's locking with the Vulkan calls replaced by counters (tools/test_cvar.py holds
   the plugin's function to that shape). Each arm is a sentence about what the table must do; exit status 0 when all hold. */
#define _DEFAULT_SOURCE
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
struct nvmtl_cvar { uint32_t l[3]; void *pipe; };   /* as declared in plugin/nvmtl_vk.h */
#include "nvmtl_cvar.h"

static int fails;
static void arm(const char *what, int ok) { printf("%s  %s\n", ok ? "ok  " : "FAIL", what); if (!ok) fails++; }

typedef struct { struct nvmtl_cvar *var; uint32_t nvar, capvar; } kernel;
static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;
static atomic_ulong compiled, destroyed, leaked;
static atomic_uintptr_t next_handle = 1;
static int compile_us = 200;            /* how long the stub compiler takes */
static atomic_int slow_size_compiling;  /* set while the slow size of the stall arm is compiling */
static uint32_t first_cap = 16, max_cap = 4096;

static void *stub_compile(const uint32_t l[3])
{
    if (l[0] == 999) { atomic_store(&slow_size_compiling, 1); usleep(300000); atomic_store(&slow_size_compiling, 0); }
    else usleep((useconds_t)compile_us);
    atomic_fetch_add(&compiled, 1);
    return (void *)atomic_fetch_add(&next_handle, 1);
}

static void *variant(kernel *p, const uint32_t l[3], int parallel)
{
    void *found, *winner = NULL; int put = -1; uint32_t grown = 0;
    pthread_mutex_lock(&g_lock);
    found = nvmtl_cvar_find(p->var, p->nvar, l);
    if (!found) {
        if (parallel) pthread_mutex_unlock(&g_lock);
        found = stub_compile(l);
        if (parallel) pthread_mutex_lock(&g_lock);
        put = nvmtl_cvar_insert(&p->var, &p->nvar, &p->capvar, l, found, parallel, first_cap, max_cap, &winner, &grown);
        if (put == NVMTL_CVAR_FULL) atomic_fetch_add(&leaked, 1);
    }
    pthread_mutex_unlock(&g_lock);
    if (put == NVMTL_CVAR_TAKEN) { atomic_fetch_add(&destroyed, 1); found = winner; }
    return found;
}

/* ---- many threads, a mix of sizes ---- */
#define NSIZE 40
#define NTHREAD 16
#define NREQ 1000
static kernel g_k;
static int g_parallel;
static atomic_int mismatch;
static void size_of(int i, uint32_t l[3]) { l[0] = 1u + (uint32_t)i; l[1] = 1u + (uint32_t)(i % 3); l[2] = 1; }
static void *worker(void *arg)
{
    unsigned seed = (unsigned)(uintptr_t)arg * 2654435761u + 1;
    for (int r = 0; r < NREQ; r++) {
        seed = seed * 1103515245u + 12345u;
        const int i = (int)((seed >> 8) % NSIZE);
        uint32_t l[3]; size_of(i, l);
        void *got = variant(&g_k, l, g_parallel);
        pthread_mutex_lock(&g_lock);
        void *kept = nvmtl_cvar_find(g_k.var, g_k.nvar, l);
        pthread_mutex_unlock(&g_lock);
        if (!got || got != kept) atomic_fetch_add(&mismatch, 1);
    }
    return NULL;
}
static void run_mix(int parallel)
{
    free(g_k.var); memset(&g_k, 0, sizeof g_k);
    atomic_store(&compiled, 0); atomic_store(&destroyed, 0); atomic_store(&leaked, 0); atomic_store(&mismatch, 0);
    g_parallel = parallel;
    pthread_t t[NTHREAD];
    for (uintptr_t i = 0; i < NTHREAD; i++) pthread_create(&t[i], NULL, worker, (void *)i);
    for (int i = 0; i < NTHREAD; i++) pthread_join(t[i], NULL);
}
static int each_size_once(void)
{
    if (g_k.nvar != NSIZE) return 0;
    for (int i = 0; i < NSIZE; i++) {
        uint32_t l[3]; size_of(i, l); int n = 0;
        for (uint32_t j = 0; j < g_k.nvar; j++) n += !memcmp(g_k.var[j].l, l, sizeof l);
        if (n != 1) return 0;
    }
    return 1;
}

/* ---- the stall: one thread compiles a slow size while another looks up a size already kept ---- */
static void *slow_worker(void *arg) { uint32_t l[3] = { 999, 1, 1 }; variant(&g_k, l, (int)(intptr_t)arg); return NULL; }
static double lookup_ms_during_slow_compile(int parallel)
{
    free(g_k.var); memset(&g_k, 0, sizeof g_k);
    uint32_t kept[3] = { 64, 1, 1 };
    variant(&g_k, kept, parallel);
    pthread_t t; pthread_create(&t, NULL, slow_worker, (void *)(intptr_t)parallel);
    while (!atomic_load(&slow_size_compiling)) usleep(100);
    struct timespec a, b; clock_gettime(CLOCK_MONOTONIC, &a);
    variant(&g_k, kept, parallel);
    clock_gettime(CLOCK_MONOTONIC, &b);
    pthread_join(t, NULL);
    return (double)(b.tv_sec - a.tv_sec) * 1e3 + (double)(b.tv_nsec - a.tv_nsec) / 1e6;
}

int main(void)
{
    /* single-threaded rules of the insert */
    {
        struct nvmtl_cvar *var = NULL; uint32_t nvar = 0, cap = 0, grown = 0; void *w = NULL;
        const uint32_t a[3] = { 64, 1, 1 }, b[3] = { 32, 2, 1 };
        arm("the first insert makes the table at its first capacity", nvmtl_cvar_insert(&var, &nvar, &cap, a, (void *)1, 1, 16, 4096, &w, &grown) == NVMTL_CVAR_ADDED && cap == 16 && nvar == 1);
        arm("a kept size is found with its pipeline", nvmtl_cvar_find(var, nvar, a) == (void *)1);
        arm("a size never put in is not found", nvmtl_cvar_find(var, nvar, b) == NULL);
        arm("a size another thread put in first is TAKEN and hands back that thread's pipeline", nvmtl_cvar_insert(&var, &nvar, &cap, a, (void *)2, 1, 16, 4096, &w, &grown) == NVMTL_CVAR_TAKEN && w == (void *)1 && nvar == 1);
        arm("without the recheck (compiled under the lock) the insert appends, as 3.0.5 did", nvmtl_cvar_insert(&var, &nvar, &cap, b, (void *)3, 0, 16, 4096, &w, &grown) == NVMTL_CVAR_ADDED && nvar == 2);
        free(var);
    }
    {
        struct nvmtl_cvar *var = NULL; uint32_t nvar = 0, cap = 0, grown = 0, g[9]; void *w = NULL; int r[9];
        for (uint32_t i = 0; i < 9; i++) { uint32_t l[3] = { i + 1, 1, 1 }; grown = 0; r[i] = nvmtl_cvar_insert(&var, &nvar, &cap, l, (void *)(uintptr_t)(i + 1), 1, 2, 8, &w, &grown); g[i] = grown; }
        arm("the table doubles 2 -> 4 -> 8 and says so when it grows past its first capacity", r[0] == NVMTL_CVAR_ADDED && r[1] == NVMTL_CVAR_ADDED && r[2] == NVMTL_CVAR_GREW && r[4] == NVMTL_CVAR_GREW && cap == 8
            && g[2] == 4 && g[4] == 8 && r[3] == NVMTL_CVAR_ADDED && r[5] == NVMTL_CVAR_ADDED);
        int all = 1;
        for (uint32_t i = 0; i < 8; i++) { uint32_t l[3] = { i + 1, 1, 1 }; all &= nvmtl_cvar_find(var, nvar, l) == (void *)(uintptr_t)(i + 1); }
        arm("growing loses no entry", all);
        arm("at its limit the table keeps no more (FULL: the caller's pipeline leaks, as before)", r[8] == NVMTL_CVAR_FULL && nvar == 8);
        free(var);
    }

    /* threads */
    run_mix(1);
    arm("16 threads, 40 sizes, compile outside the lock: each size is kept exactly once", each_size_once());
    arm("every caller got the pipeline the table keeps for its size", atomic_load(&mismatch) == 0);
    arm("every pipeline compiled is either kept or destroyed (none leaks)", atomic_load(&compiled) == g_k.nvar + atomic_load(&destroyed) && atomic_load(&leaked) == 0);
    printf("      (outside the lock: %lu compiles for %u sizes, %lu duplicates destroyed)\n", atomic_load(&compiled), g_k.nvar, atomic_load(&destroyed));
    first_cap = 4; max_cap = 64;
    run_mix(1);
    arm("the same with a table that grows 4 -> 8 -> 16 -> 32 -> 64 while threads insert", each_size_once() && atomic_load(&mismatch) == 0 && atomic_load(&compiled) == g_k.nvar + atomic_load(&destroyed));
    first_cap = 16; max_cap = 4096;
    run_mix(0);
    arm("NVMTL_CVAR_PARALLEL=0 (compile under the lock): each size compiled exactly once, nothing destroyed", each_size_once() && atomic_load(&compiled) == NSIZE && atomic_load(&destroyed) == 0 && atomic_load(&mismatch) == 0);

    const double par = lookup_ms_during_slow_compile(1), ser = lookup_ms_during_slow_compile(0);
    printf("      (a kept size looked up while another thread compiles for 300 ms: %.1f ms outside the lock, %.1f ms under it)\n", par, ser);
    arm("compiling outside the lock, another thread's lookup of a kept size does not wait for the compile", par < 100.0);
    arm("compiling under the lock (the way out), that lookup waits for it, as in 3.0.5", ser >= 100.0);
    free(g_k.var);

    printf(fails ? "FAILED %d\n" : "ok\n", fails);
    return fails != 0;
}
