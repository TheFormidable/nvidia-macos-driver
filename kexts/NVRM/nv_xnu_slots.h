/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

/* nv_xnu_slots.h - a small fixed table of slots, each owned by one object (a GPU, its interrupt thread), claimed and
 * given back without a lock. Used for the per-GPU interrupt-thread slots behind os_is_isr() (os-xnu.cpp) and the
 * per-GPU RC timer slots (nv_xnu_timers.cpp). No kernel type and no kernel call, only compiler atomics, so the host
 * test (tools/test_quiesce_gates.cpp) runs the same code the kext does.
 *
 * Rules: an owner is never NULL; one owner claims from one thread at a time (a GPU claims from its own start, which is
 * serialised); a slot is given back only by its owner. Readers that scan the table (nv_slot_find) see either the old or
 * the new owner of a slot, never a torn value: the entries are single pointers written atomically. */
#ifndef NV_XNU_SLOTS_H
#define NV_XNU_SLOTS_H

/* The slot `owner` already holds, or -1. */
static inline int nv_slot_find(void *const volatile *slots, unsigned n, const void *owner)
{
    if (!owner) return -1;
    for (unsigned i = 0; i < n; i++)
        if (__atomic_load_n(&slots[i], __ATOMIC_SEQ_CST) == owner) return (int)i;
    return -1;
}

/* The slot `owner` holds, claiming the first free one if it holds none; -1 when every slot is taken (or owner is
 * NULL). Claiming twice gives the same slot. */
static inline int nv_slot_claim(void *volatile *slots, unsigned n, void *owner)
{
    const int have = nv_slot_find((void *const volatile *)slots, n, owner);
    if (have >= 0 || !owner) return have;
    for (unsigned i = 0; i < n; i++) {
        void *expected = 0;
        if (__atomic_compare_exchange_n(&slots[i], &expected, owner, 0, __ATOMIC_SEQ_CST, __ATOMIC_SEQ_CST)) return (int)i;
    }
    return -1;
}

/* Gives a slot back. Does nothing for -1, for a slot out of range, or for a slot another owner holds; returns whether
 * the slot was freed. */
static inline int nv_slot_release(void *volatile *slots, unsigned n, int slot, void *owner)
{
    if (slot < 0 || (unsigned)slot >= n || !owner) return 0;
    void *expected = owner;
    return __atomic_compare_exchange_n(&slots[slot], &expected, (void *)0, 0, __ATOMIC_SEQ_CST, __ATOMIC_SEQ_CST) ? 1 : 0;
}

#endif
