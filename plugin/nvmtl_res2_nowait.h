/* nvmtl_res2_nowait.h - when a thread that asks the VRAM pager for room may sleep.
   The pager (res2, NVMTLObjects.m) is asked for room by the thread that is creating a texture, a Private buffer or a
   heap when the VRAM gate refuses it, or when RM refused the allocation. That thread used to sleep twice: up to 30 ms
   polling for the pass lock while the pager's own worker held it, and up to 16 ms after the pass waiting for the
   evicted backings to be freed (they are freed only once every command buffer of their epoch has completed, which at
   20 fps can take a 50 ms frame, longer than the wait; how often it timed out has not been measured). A request the
   pager cannot answer at once already ends in the caller's own fallback: the texture or heap is placed in system memory
   and a later worker pass pages it on. The three Private buffers that had no such fallback (visibility scratch and the
   acceleration-structure user-ID tables) retry in system memory while the waits are off.
   So with the pager's worker busy the caller now kicks the worker and goes on, and after the pass it reaps the
   graveyard once and recounts instead of sleeping.
   NVMTL_RES2_NOWAIT=0 restores both waits, as in 3.0.5. WindowServer keeps them as well: its allocations stall the
   whole desktop either way, and whether more of its textures in system memory cost it more than the sleeps do has not
   been measured on the card.
   The decisions are pure so tools/test_res2_nowait.c proves them without Metal. */
#ifndef NVMTL_RES2_NOWAIT_H
#define NVMTL_RES2_NOWAIT_H
#include <stdint.h>
#include <string.h>

/* env: NVMTL_RES2_NOWAIT after the settings file was read (NULL when unset); progname: getprogname().
   On unless the variable starts with 0 or the process is WindowServer. */
static inline int nvmtl_res2_nowait_default(const char *env, const char *progname)
{
    if (env && env[0] == '0') return 0;
    return !(progname && !strcmp(progname, "WindowServer"));
}

/* What nvmtl_res2_pass_guarded does when the pass lock is already taken. */
typedef enum {
    NVMTL_RES2_BUSY_KICK = 0,   /* reschedule the worker (it sees gRes2Want) and return */
    NVMTL_RES2_BUSY_SPIN = 1    /* poll the lock every 200 us for up to 30 ms, then run the pass on this thread */
} nvmtl_res2_busy;

static inline nvmtl_res2_busy nvmtl_res2_busy_action(int from_worker, int nowait)
{
    return (from_worker || nowait) ? NVMTL_RES2_BUSY_KICK : NVMTL_RES2_BUSY_SPIN;
}

/* What nvmtl_res2_evict does after the pass when the card has no more free VRAM than before it. */
typedef enum {
    NVMTL_RES2_DRAIN_NONE = 0,  /* something came back, or nothing is waiting in the graveyard, or still blind */
    NVMTL_RES2_DRAIN_REAP = 1,  /* one non-blocking reap and a recount; kick the worker if nothing came back */
    NVMTL_RES2_DRAIN_SLEEP = 2  /* reap every 1 ms for up to 16 ms; on a timeout go blind for 250 ms */
} nvmtl_res2_drain;

/* freed_nothing: free VRAM after the pass <= before it; grave_vram: VRAM bytes still in the graveyard;
   now_ns / blind_until_ns: the 3.0.5 rule that skips the sleep for 250 ms after one timed out. That bookkeeping only
   serves the sleep, so the non-blocking path neither reads nor sets it. */
static inline nvmtl_res2_drain nvmtl_res2_drain_action(int nowait, int freed_nothing, uint64_t grave_vram,
                                                       uint64_t now_ns, uint64_t blind_until_ns)
{
    if (!freed_nothing || !grave_vram) return NVMTL_RES2_DRAIN_NONE;
    if (nowait) return NVMTL_RES2_DRAIN_REAP;
    return now_ns >= blind_until_ns ? NVMTL_RES2_DRAIN_SLEEP : NVMTL_RES2_DRAIN_NONE;
}

#endif
