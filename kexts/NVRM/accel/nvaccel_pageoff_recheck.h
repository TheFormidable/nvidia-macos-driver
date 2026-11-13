/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

/* nvaccel_pageoff_recheck.h - after a page-off waited without the accelerator lock: is its copy still to be made?

   NVResource::pageoff copies a surface's pixels from the GPU's memory to system memory. By default it waits for the GPU
   without the accelerator lock. While the lock is free other threads can page the same resource off, and a CPU writer
   can write system memory after one of them. With the lock back, the page-off has to decide whether it still copies.

   It has two things to go by. The family's bit "system memory holds the newest pixels" (a page-off sets it, the plugin's
   mark of a GPU write clears it), as it was before the wait and as it is now. And the driver's own copies of this
   resource: each takes a number when it starts and leaves the number behind when it has finished well, both with the
   lock held, so a waiter can tell which copies began after it let go of the lock and which after its own wait ended.

   - The bit is clear: the GPU's memory is the newest. Copy, as with the lock held.
   - A copy that started after this wait ended has finished: it read the GPU's memory no earlier than this page-off
     would have with the lock held all along (2.9 copied the moment its wait ended, whether the GPU was done or the
     wait ran into its bound), so it is the copy this page-off was about to make. Report done and copy nothing; a
     second copy would put the GPU's pixels over whatever a CPU writer has written to system memory since.
   - Some other copy finished while the lock was free, but it started before this wait ended: it may have read the
     GPU's memory before the GPU was done (its own wait was for less, or ran into its bound). Copy, as 2.9 did after
     its wait. Reporting done here would hand back a frame the wait was there to complete.
   - No copy of the driver's finished while the lock was free, so the bit is the family's own doing. It was clear and is
     set: the family made system memory current by a path of its own, report done. It was set all along: nothing
     changed, and 2.9 copied.

   Pure: tools/test_pageoff_recheck.c runs these rules and the numbering against every short order of events. */
#ifndef NVACCEL_PAGEOFF_RECHECK_H
#define NVACCEL_PAGEOFF_RECHECK_H
/* One per resource. `started` goes up by one when a copy begins, with the accelerator lock held; a waiter reads it
   without the lock when its wait ends. `finished` is the number of the last copy that finished well, written and read
   with the lock held only. A copy holds the lock from its number to its end, so numbers finish in order. */
typedef struct { unsigned long long started; unsigned long long finished; } nvaccel_pageoff_copies;

static inline unsigned long long nvaccel_pageoff_copy_begin(nvaccel_pageoff_copies *c)
{
    return __atomic_add_fetch(&c->started, 1ull, __ATOMIC_RELEASE);
}
static inline void nvaccel_pageoff_copy_end(nvaccel_pageoff_copies *c, unsigned long long number) { c->finished = number; }
/* How many copies have begun. A copy that begins after this call gets a larger number. */
static inline unsigned long long nvaccel_pageoff_copies_started(const nvaccel_pageoff_copies *c)
{
    return __atomic_load_n(&c->started, __ATOMIC_ACQUIRE);
}
/* 1 when a copy that began after `seen` copies had begun has finished well. */
static inline int nvaccel_pageoff_copied_after(const nvaccel_pageoff_copies *c, unsigned long long seen) { return c->finished > seen; }
/* 1 = report the page-off done and copy nothing, 0 = copy. Called with the lock held again.
   wasCurrent, currentNow   the family's bit before the lock was let go, and now
   atUnlock, atWaitEnd      nvaccel_pageoff_copies_started() just before the lock was let go, and when the wait ended */
static inline int nvaccel_pageoff_already_done(const nvaccel_pageoff_copies *c, int wasCurrent, int currentNow,
                                               unsigned long long atUnlock, unsigned long long atWaitEnd)
{
    if (!currentNow) return 0;
    if (nvaccel_pageoff_copied_after(c, atWaitEnd)) return 1;
    if (nvaccel_pageoff_copied_after(c, atUnlock)) return 0;
    return !wasCurrent;
}
#endif
