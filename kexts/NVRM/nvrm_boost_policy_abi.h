/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

/* nvrm_boost_policy_abi.h - the boost policy: one machine-wide answer, held by NVRM.kext, to "may NVK ask for full
 * clocks while a process works", that a root process can change while every app keeps running (idle mode, hot).
 *
 * NVK asks the GPU for full clocks with a short lease that each busy process renews at most once a second
 * (nvk/nvk-perf-boost-burst.patch). Which shape it uses comes from NVRM_610_BOOST, read once when the process opens the
 * device; so switching idle mode through /Library/GPUBundles/nvmtl/nvrm610.conf only reached apps started afterwards.
 * The kext now holds a policy of three values that NVK reads through this selector each time it would renew:
 *   NVRM_BP_UNDECIDED  nobody chose since boot: every process follows NVRM_610_BOOST as before (the conf file keeps
 *                      the choice across reboots; the kext starts here at every boot)
 *   NVRM_BP_IDLE       idle mode: no process renews its lease, and one that holds a lease or a hold clears it
 *   NVRM_BP_NORMAL     idle mode off: every process boosts per burst of work (or holds, with NVRM_610_BOOST=hold),
 *                      even one that started with NVRM_610_BOOST=0
 * It changes no RM state: a process's boost is its own RM client's request, and only that process can clear it, so
 * the policy takes effect at each busy process's next renewal (within a second of its work). A process that is not
 * submitting holds no lease past its last 3 s. Other RM clients (NVMetrics' own boost, nvmtl-boost) are not affected.
 *
 * Reading is open to any process that can open the user client; setting needs root (the caller's credential, checked
 * on its own thread). The value also shows in the read-only sysctl debug.nvrm_boost_policy, with the number of
 * changes since boot in debug.nvrm_boost_policy_sets. It survives sleep and a stop of the card; a reboot returns it
 * to NVRM_BP_UNDECIDED.
 *
 * Shared by the kext (C++) and NVK (C, a byte-identical copy that its patch adds as nvkmd/nvrm/nvrm_boost_policy_abi.h;
 * tools/test_shared_headers.py compares the two). Plain C, stdint only.
 *
 * A kext without the selector answers kIOReturnBadArgument (the selector is past the end of its table): the caller
 * treats the policy as undecided and stops asking. */
#pragma once
#include <stdint.h>
/* NVRMUserClient external method: two scalars in (op, value), one out (the policy after the call). 12 is
 * NVRM_FENCE_SELECTOR, 10 and 11 are retired placeholders. */
#define NVRM_BOOST_POLICY_SELECTOR  13u
enum { NVRM_BP_OP_READ = 0, NVRM_BP_OP_SET = 1 };
enum { NVRM_BP_UNDECIDED = 0, NVRM_BP_IDLE = 1, NVRM_BP_NORMAL = 2, NVRM_BP_COUNT = 3 };

/* The shape a process uses once the policy is known. conf: what NVRM_610_BOOST says for this process, as
 * NVRM_BP_SHAPE_*. Pure, so the host test can run every pair. */
enum { NVRM_BP_SHAPE_OFF = 0, NVRM_BP_SHAPE_BURST = 1, NVRM_BP_SHAPE_HOLD = 2 };
static inline int nvrm_bp_shape(uint64_t policy, int conf)
{
   if (policy == NVRM_BP_IDLE) return NVRM_BP_SHAPE_OFF;
   if (policy == NVRM_BP_NORMAL) return conf == NVRM_BP_SHAPE_HOLD ? NVRM_BP_SHAPE_HOLD : NVRM_BP_SHAPE_BURST;
   return conf;   /* undecided, or a value this side does not know: the process's own setting */
}
