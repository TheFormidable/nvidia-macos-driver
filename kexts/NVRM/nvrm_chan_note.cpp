/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 *
 * Channel notes: machine-wide counters of GPU faults and of what NVK did about them.
 *
 * When a channel of a process faults or stalls, NVK rebuilds that channel inside the process instead of losing the
 * whole device, and tells the kext through escape 0xE8 (nvrm_chan_note_abi.h) that it rang a doorbell again, rebuilt
 * a channel, gave up, or injected a test fault. The kext takes no part in the rebuild. It counts those notes, counts
 * every Xid the RM logs, and answers the GPU's power state, which NVK uses to tell a failed wake from a channel fault.
 * An NVK that never sends the escape changes nothing here; a kext without it makes NVK stop asking.
 *
 * All of debug.nvrm_xid_* and debug.nvrm_chan_* is read-only and nothing here switches anything. The counting itself
 * is in nvrm_chan_note_count.h, which tools/test_chan_note_count.c runs.
 */

#include <stdint.h>
#include <libkern/OSAtomic.h>
extern "C" {
#include <sys/sysctl.h>
}
#include "nvrm_chan_note_abi.h"
#include "nvrm_chan_note_count.h"

// Static, so there is nothing to allocate, nothing to fail and nothing to free: the Xid hook may run before the first
// GPU start and after the last stop. Written only through the atomics of nvrm_chan_note_count.h.
static struct nvrm_cn_counts gCn;
static volatile UInt32 gCnSysctls;           // 1 once the sysctls are registered

SYSCTL_QUAD(_debug, OID_AUTO, nvrm_xid_count, CTLFLAG_RD | CTLFLAG_LOCKED, (long long *)&gCn.xidCount, "Xids the RM logged since the kext loaded");
SYSCTL_INT(_debug, OID_AUTO, nvrm_xid_last, CTLFLAG_RD | CTLFLAG_LOCKED, (int *)&gCn.xidLast, 0, "the last Xid the RM logged");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_chan_kicks, CTLFLAG_RD | CTLFLAG_LOCKED, (long long *)&gCn.kicks, "stalled channels whose doorbell NVK rang again");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_chan_recovered, CTLFLAG_RD | CTLFLAG_LOCKED, (long long *)&gCn.recovered, "channels NVK rebuilt after a fault or a stall");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_chan_dropped, CTLFLAG_RD | CTLFLAG_LOCKED, (long long *)&gCn.dropped, "submissions those rebuilds did not run");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_chan_gave_up, CTLFLAG_RD | CTLFLAG_LOCKED, (long long *)&gCn.gaveUp, "channel recoveries given up: that process lost its device");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_chan_injected, CTLFLAG_RD | CTLFLAG_LOCKED, (long long *)&gCn.injected, "faults made by NVK's test hook");
SYSCTL_INT(_debug, OID_AUTO, nvrm_chan_last_xid, CTLFLAG_RD | CTLFLAG_LOCKED, (int *)&gCn.lastXid, 0, "Xid of the last channel event a process reported");
SYSCTL_INT(_debug, OID_AUTO, nvrm_chan_last_kind, CTLFLAG_RD | CTLFLAG_LOCKED, (int *)&gCn.lastKind, 0, "kind of loss of the last recovery given up (1 channel, 2 stall, 3 memory, 4 GSP, 5 bus, 6 wake, 7 unknown; -1 not understood)");
SYSCTL_INT(_debug, OID_AUTO, nvrm_chan_last_why, CTLFLAG_RD | CTLFLAG_LOCKED, (int *)&gCn.lastWhy, 0, "why the last recovery was given up, as NVK numbers it (-1 not understood)");

// From the RM's error log hook, nv_log_error: RM locks may be held and preemption may be off, so two atomic stores and
// nothing else (no lock, no allocation, no log line, no wakeup). The caller logs the Xid itself.
extern "C" void nvrm_chan_note_xid(uint32_t xid)
{
    nvrm_cn_xid(&gCn, xid);
}

// The escape's body, on the calling process's thread, before its connection's gate and the power gate: it must
// answer during a GPU suspend and resume, so it can run beside any other escape or the close of the same
// connection, and two notes of one connection can run at once. It touches only the static counters and the
// caller's own copy of the record. The caller checked the size and copied the record into the kernel; the record is
// read once and answered in place. Atomics only, never sleeps.
extern "C" void nvrm_chan_note_handle(struct NVRMChanNoteEsc *e, uint32_t pm)
{
    if (!e) return;
    (void)nvrm_cn_note(&gCn, e, pm);
}

// Once, from NVRM's sysctl registration block (thread context); a second call does nothing. Never unregistered, like
// every other debug.nvrm_* sysctl: the OIDs and the counters they point at are static data of NVRM.kext, which stays
// loaded until shutdown (it holds a reference on itself from the first start that registers anything: NVRM.cpp,
// nvrmStayLoaded()).
extern "C" void nvrm_chan_note_sysctls(void)
{
    if (!OSCompareAndSwap(0, 1, &gCnSysctls)) return;
    sysctl_register_oid(&sysctl__debug_nvrm_xid_count);
    sysctl_register_oid(&sysctl__debug_nvrm_xid_last);
    sysctl_register_oid(&sysctl__debug_nvrm_chan_kicks);
    sysctl_register_oid(&sysctl__debug_nvrm_chan_recovered);
    sysctl_register_oid(&sysctl__debug_nvrm_chan_dropped);
    sysctl_register_oid(&sysctl__debug_nvrm_chan_gave_up);
    sysctl_register_oid(&sysctl__debug_nvrm_chan_injected);
    sysctl_register_oid(&sysctl__debug_nvrm_chan_last_xid);
    sysctl_register_oid(&sysctl__debug_nvrm_chan_last_kind);
    sysctl_register_oid(&sysctl__debug_nvrm_chan_last_why);
}
