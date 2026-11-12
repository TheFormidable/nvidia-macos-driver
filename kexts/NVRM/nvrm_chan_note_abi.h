/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

/* nvrm_chan_note_abi.h - the channel note: NVK tells NVRM.kext what it did about a faulted or stalled channel, and
 * learns the adapter's power state and the RM's Xid count in the same call.
 *
 * When a channel of a process faults, NVK rebuilds it instead of losing the device. The kext takes no part in the
 * rebuild; it only keeps machine-wide read-only counters (debug.nvrm_chan_*, debug.nvrm_xid_*) so one place shows
 * what every process did, and answers whether the GPU is running, asleep or lost, which decides whether a rebuild
 * is worth trying. The escape carries fixed-size counters from an unprivileged process and nothing else: no pointer,
 * bounded increments. A process can make the counters lie; nothing acts on them.
 *
 * Shared by the kext (C++) and NVK (C, a byte-identical copy that its patch adds as nvkmd/nvrm/nvrm_chan_note_abi.h;
 * tools/test_shared_headers.py compares the two). Plain C, stdint only.
 *
 * A kext without the escape passes 0xE8 to the RM, which rejects an unknown command: the call fails, `status` stays 0,
 * and the caller treats the power state as unknown and stops asking. No user-client selector and no capability bit. */
#pragma once
#include <stdint.h>
/* Private escape through user-client selector 1. 0xE2 and 0xE3 are nvrm_surfshare_abi.h; the RM's own escapes are
 * 0x27-0x5F and 0xC8-0xDB. */
#define NVRM_ESC_CHAN_NOTE      0xE8u
#define NVRM_CHAN_NOTE_VERSION  1u
enum { NVRM_CN_QUERY = 0, NVRM_CN_KICK = 1, NVRM_CN_RECOVERED = 2, NVRM_CN_GAVE_UP = 3, NVRM_CN_INJECTED = 4,
       NVRM_CN_WHAT_COUNT = 5 };
enum { NVRM_CN_PM_RUNNING = 0, NVRM_CN_PM_SUSPENDED = 1, NVRM_CN_PM_LOST = 2 };   /* == NVRM::kGpuPm* */
#define NVRM_CN_OK 1u
struct NVRMChanNoteEsc {
   uint32_t version;    /* in   NVRM_CHAN_NOTE_VERSION                                   offset  0 */
   uint32_t what;       /* in   NVRM_CN_*                                                offset  4 */
   uint32_t xid;        /* in   error-notifier info32, 0 = none (stall)                  offset  8 */
   uint32_t dropped;    /* in   flushes not executed (RECOVERED), else 0                 offset 12 */
   uint32_t kind;       /* in   enum nvrm_rec_kind (RECOVERED, GAVE_UP), else 0          offset 16 */
   uint32_t why;        /* in   enum nvrm_rec_why (GAVE_UP), else 0                      offset 20 */
   uint32_t pm;         /* out  NVRM_CN_PM_* of the adapter behind this connection       offset 24 */
   uint32_t status;     /* out  NVRM_CN_OK when the kext handled it; the caller sends 0  offset 28 */
   uint64_t xidCount;   /* out  debug.nvrm_xid_count                                     offset 32 */
   uint32_t xidLast;    /* out  debug.nvrm_xid_last                                      offset 40 */
   uint32_t reserved;   /* 0                                                             offset 44 */
};                      /* 48 bytes, no padding */
#ifdef __cplusplus
static_assert(sizeof(struct NVRMChanNoteEsc) == 48, "NVRMChanNoteEsc is wire ABI");
#else
_Static_assert(sizeof(struct NVRMChanNoteEsc) == 48, "NVRMChanNoteEsc is wire ABI");
#endif
#ifdef KERNEL
#ifdef __cplusplus
extern "C" {
#endif
/* The RM logged an Xid. ANY context: it is called from the RM's error log hook with RM locks held and possibly with
 * preemption off, so it does two atomic stores and nothing else: no IOLock, no allocation, no IOLog, no wakeup.
 * Safe before nvrm_chan_note_sysctls() and at any time until the kext unloads. */
void nvrm_chan_note_xid(uint32_t xid);
/* The escape's body, from the user client's escape switch on the caller's thread. `e` is the kernel's copy of the
 * record and the caller has checked that the escape's size is exactly sizeof(struct NVRMChanNoteEsc); `pm` is the
 * adapter's power state as NVRM_CN_PM_*. For an unknown version or `what` it leaves `status` 0 and writes nothing;
 * otherwise it counts by `what` (bounded), fills pm, xidCount, xidLast and sets status NVRM_CN_OK. Atomics only: no
 * lock, no RM call, never sleeps. The caller answers kIOReturnSuccess either way; the record is copied back as for
 * every escape. */
void nvrm_chan_note_handle(struct NVRMChanNoteEsc *e, uint32_t pm);
/* Registers the read-only sysctls. Thread context, from the kext's sysctl registration block; a second call does
 * nothing. They stay registered until the kext unloads. */
void nvrm_chan_note_sysctls(void);
#ifdef __cplusplus
}
#endif
#endif
