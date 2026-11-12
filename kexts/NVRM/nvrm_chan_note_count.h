/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

/* nvrm_chan_note_count.h - what NVRM.kext does with a channel note and with an Xid: it counts, nothing else.
 *
 * nvrm_chan_note.cpp keeps one struct nvrm_cn_counts for the whole machine and publishes its fields as read-only
 * sysctls; tools/test_chan_note_count.c runs these functions as they are. Plain C and C++, the ABI header and the
 * compiler's atomic builtins only: no lock, no allocation, no call, so both functions are safe in any context,
 * including the RM's error path with its locks held and preemption off.
 *
 * The record comes from an unprivileged process. The kernel copied it before the call, so each field is read once
 * and nothing in it is trusted: an unknown version or request is not answered at all (status stays as the caller
 * sent it, 0), the count of dropped flushes is capped per note, and a kind or reason this kext does not know is
 * recorded as -1 instead of as a number that means nothing. A process can still make the counters lie; nothing in
 * the kernel acts on them. */
#pragma once
#include "nvrm_chan_note_abi.h"

/* At most this many dropped flushes are counted from one note: one rebuild drops at most a ring of 64 flushes. */
#define NVRM_CN_DROPPED_MAX  64u
/* The last loss kind and reason NVK's channel recovery names (UNKNOWN = 7, "gave up before" = 13). Larger values are
 * recorded as -1. */
#define NVRM_CN_KIND_MAX     7u
#define NVRM_CN_WHY_MAX      13u

struct nvrm_cn_counts {
   int64_t xidCount;      /* debug.nvrm_xid_count: Xids the RM logged since the kext loaded */
   int64_t kicks;         /* debug.nvrm_chan_kicks: doorbells rung again on a stalled channel */
   int64_t recovered;     /* debug.nvrm_chan_recovered: channels rebuilt */
   int64_t dropped;       /* debug.nvrm_chan_dropped: flushes those rebuilds did not run */
   int64_t gaveUp;        /* debug.nvrm_chan_gave_up: recoveries given up (the device is lost, as before) */
   int64_t injected;      /* debug.nvrm_chan_injected: faults made by NVK's test hook */
   int32_t xidLast;       /* debug.nvrm_xid_last: the last Xid the RM logged */
   int32_t lastXid;       /* debug.nvrm_chan_last_xid: Xid of the last channel event a process reported */
   int32_t lastKind;      /* debug.nvrm_chan_last_kind: kind of loss of the last give-up, -1 not understood */
   int32_t lastWhy;       /* debug.nvrm_chan_last_why: reason of the last give-up, -1 not understood */
};

/* The RM logged Xid `xid`. Two relaxed atomics: callers are any thread, the reader is a sysctl that wants a recent
 * value, not an ordering. */
static inline void
nvrm_cn_xid(struct nvrm_cn_counts *c, uint32_t xid)
{
   __atomic_add_fetch(&c->xidCount, 1, __ATOMIC_RELAXED);
   __atomic_store_n(&c->xidLast, (int32_t)xid, __ATOMIC_RELAXED);
}

static inline int32_t
nvrm_cn_enum(uint32_t v, uint32_t max)
{
   return v <= max ? (int32_t)v : -1;
}

/* One note. Returns 1 when it was understood (counted, the out fields filled, status NVRM_CN_OK), 0 when it was not
 * (nothing counted, nothing written). `pm` is the adapter's power state, already NVRM_CN_PM_*. */
static inline int
nvrm_cn_note(struct nvrm_cn_counts *c, struct NVRMChanNoteEsc *e, uint32_t pm)
{
   const uint32_t version = e->version, what = e->what, xid = e->xid, dropped = e->dropped;
   const uint32_t kind = e->kind, why = e->why;
   if (version != NVRM_CHAN_NOTE_VERSION || what >= NVRM_CN_WHAT_COUNT)
      return 0;
   switch (what) {
   case NVRM_CN_KICK:
      __atomic_add_fetch(&c->kicks, 1, __ATOMIC_RELAXED);
      break;
   case NVRM_CN_RECOVERED:
      __atomic_add_fetch(&c->recovered, 1, __ATOMIC_RELAXED);
      __atomic_add_fetch(&c->dropped, (int64_t)(dropped < NVRM_CN_DROPPED_MAX ? dropped : NVRM_CN_DROPPED_MAX),
                         __ATOMIC_RELAXED);
      break;
   case NVRM_CN_GAVE_UP:
      __atomic_add_fetch(&c->gaveUp, 1, __ATOMIC_RELAXED);
      __atomic_store_n(&c->lastKind, nvrm_cn_enum(kind, NVRM_CN_KIND_MAX), __ATOMIC_RELAXED);
      __atomic_store_n(&c->lastWhy, nvrm_cn_enum(why, NVRM_CN_WHY_MAX), __ATOMIC_RELAXED);
      break;
   case NVRM_CN_INJECTED:
      __atomic_add_fetch(&c->injected, 1, __ATOMIC_RELAXED);
      break;
   default:   /* NVRM_CN_QUERY: only the answer below */
      break;
   }
   if (xid != 0)
      __atomic_store_n(&c->lastXid, (int32_t)xid, __ATOMIC_RELAXED);
   e->pm = pm;
   e->xidCount = (uint64_t)__atomic_load_n(&c->xidCount, __ATOMIC_RELAXED);
   e->xidLast = (uint32_t)__atomic_load_n(&c->xidLast, __ATOMIC_RELAXED);
   e->status = NVRM_CN_OK;
   return 1;
}
