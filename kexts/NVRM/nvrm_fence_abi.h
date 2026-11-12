/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

/* nvrm_fence_abi.h - the surface fence: who is still drawing into which IOSurface, kept in NVRM.kext.
 *
 * A producer (NVK, for the Metal plugin) registers its channel once, opens a hold on the surfaces a command buffer
 * writes, arms the hold with the channel's completion value when the work is flushed, and closes it when the
 * application is done. A reader (another process, or NVAccel before a flip or a page-off) waits in the kernel until
 * the holds in front of it have ended. The kernel reads the completion value from the producer's own semaphore page.
 *
 * Shared by three builds, so it is plain C with no include: the kext (C++), NVK (C, a byte-identical copy that its
 * patch adds as nvkmd/nvrm/nvrm_fence_abi.h; tools/test_shared_headers.py compares the two) and the host tests. The
 * plugin does not see the structs: NVK builds the request, and plugin/nvmtl_fence.h mirrors only the numbers. */
#ifndef NVRM_FENCE_ABI_H
#define NVRM_FENCE_ABI_H

#define NVRM_FENCE_ABI_VERSION      1u
#define NVRM_FENCE_SELECTOR         12u     /* NVRMUserClient external method; struct NVRMFenceReq in and out. 9 is the
                                               event-wait note, 10 and 11 answer kIOReturnUnsupported. */
#define NVRM_FENCE_MAX_IDS          8u
#define NVRM_FENCE_WAIT_MAX_MS      2000u   /* a WAIT's timeoutMs is clamped to this */
#define NVRM_FENCE_SEQ_MAX_MS       100u    /* a WAIT_SEQ's timeoutMs is clamped to this */
/* The channel's semaphore page (4096 bytes, page-aligned, the producer's own memory):
 *   +0x000  64-bit completion value, written by the GPU
 *   +0xFF0  64-bit value of the last flush, written by the producer BEFORE the work is visible to the GPU
 *   +0xFF8  64-bit mapping probe, written by the producer around CHAN_ADD / CHAN_VERIFY                       */
#define NVRM_FENCE_SEM_FLUSHED_OFF  0xFF0u
#define NVRM_FENCE_SEM_PROBE_OFF    0xFF8u

#define NVRM_FENCE_OP_QUERY         0u
#define NVRM_FENCE_OP_CHAN_ADD      1u
#define NVRM_FENCE_OP_CHAN_VERIFY   2u
#define NVRM_FENCE_OP_CHAN_DEL      3u
#define NVRM_FENCE_OP_OPEN          4u
#define NVRM_FENCE_OP_ARM           5u
#define NVRM_FENCE_OP_CLOSE         6u
#define NVRM_FENCE_OP_WAIT          7u
#define NVRM_FENCE_OP_WAIT_SEQ      8u
/* The producer rebuilt a faulted channel: same semaphore page, new error notifier. u.chan carries the handle CHAN_ADD
 * gave (chan), and the address (errVA) and registration status (errBase) of the NEW notifier; semVA and probe are not
 * read. The kernel maps the new page, reads faults from it from then on and forgets the old fault; the holds and the
 * verified state stay as they are. Without this call the kernel keeps reading the dead notifier and the channel stays
 * faulted for the life of the process. On any status but OK the channel is as it was before the call: the producer
 * then deletes it (CHAN_DEL) and goes on without the fence. */
#define NVRM_FENCE_OP_CHAN_REBIND   9u

#define NVRM_FENCE_F_COMPOSITOR     0x01u   /* OPEN: compositor-class hold. WAIT: look at compositor-class holds only */
#define NVRM_FENCE_F_AUTO           0x02u   /* OPEN, with COMPOSITOR only: the hold ends when its armed value is reached */
#define NVRM_FENCE_F_ARM_DONE       0x04u   /* ARM: the work is already complete */
#define NVRM_FENCE_F_LOOK           0x08u   /* WAIT: do not sleep; answer NONE or PENDING */
#define NVRM_FENCE_F_CHAN_CE        0x10u   /* CHAN_ADD: copy-engine-only channel (a label for the counters) */

#define NVRM_FENCE_ST_OK            0u
#define NVRM_FENCE_ST_BAD_VERSION   0x80000001u
#define NVRM_FENCE_ST_OFF           0x80000002u   /* boot-arg, fused, stopped, suspended, or the client is closing */
#define NVRM_FENCE_ST_BAD_ARG       0x80000003u
#define NVRM_FENCE_ST_FULL          0x80000004u   /* table, quota, or sleeper ceiling */
#define NVRM_FENCE_ST_DENIED        0x80000005u   /* COMPOSITOR from a process that is not WindowServer */
#define NVRM_FENCE_ST_STALE         0x80000006u   /* unknown, foreign or unverified channel; unknown cookie */
#define NVRM_FENCE_ST_MAP           0x80000007u   /* the page could not be mapped, or the probe did not match */
#define NVRM_FENCE_ST_DISTRUST      0x80000008u   /* this owner's holds timed out too often; it keeps the 2.9 mechanism */

#define NVRM_FENCE_R_NONE           0u   /* nothing was pending for this reader */
#define NVRM_FENCE_R_READY          1u   /* something was pending and completed */
#define NVRM_FENCE_R_DEAD           2u   /* what was pending ended because its writer exited, its channel faulted, or the GPU went away */
#define NVRM_FENCE_R_TIMEOUT        3u   /* a hold's safety bound ended it; counted and logged by the kext */
#define NVRM_FENCE_R_BUDGET         4u   /* the caller's own timeoutMs ran out; the holds are untouched */
#define NVRM_FENCE_R_ABORTED        5u   /* the calling thread was interrupted */
#define NVRM_FENCE_R_PENDING        6u   /* LOOK: something is pending. WAIT_SEQ: not reached and the kernel will not sleep on this channel */

#define NVRM_FENCE_E_OPEN           1u   /* no GPU target yet */
#define NVRM_FENCE_E_ARMED          2u   /* target known, not reached */
#define NVRM_FENCE_E_GPU_DONE       3u   /* target reached, application hold not closed yet */

struct NVRMFenceQuery {              /* 24 bytes, out */
    unsigned active;                 /* 1 = holds are taken and waits are served */
    unsigned accel;                  /* 1 = NVAccel's waits read this table (compositor holds are honoured by the flip) */
    unsigned capGpuMs, capCpuMs;     /* 2000, 100 */
    unsigned state;                  /* debug.nvrm_fence_state */
    unsigned pad;
};
struct NVRMFenceChan {               /* 32 bytes */
    unsigned long long semVA;        /* in  ADD: page-aligned address, in the caller, of the semaphore page */
    unsigned long long errVA;        /* in  ADD, REBIND: address, in the caller, of the channel's error NvNotification (16 bytes, 16-aligned) */
    unsigned long long probe;        /* in  ADD: the value now at semVA + 0xFF8 (non-zero); VERIFY: the new value there */
    unsigned errBase;                /* in  ADD, REBIND: NvNotification.status at registration */
    unsigned chan;                   /* out ADD; in VERIFY, DEL, REBIND: channel handle, never 0 */
};
struct NVRMFenceHold {               /* 72 bytes */
    unsigned long long cookie;       /* in: the producer's name for one command buffer's holds; non-zero */
    unsigned long long seq;          /* in  ARM: the completion value that means "done" */
    unsigned chan;                   /* in  ARM */
    unsigned count;                  /* in: ids used, 1..8; CLOSE with 0 = every hold of this cookie */
    unsigned ids[8];                 /* in: IOSurface ids, non-zero */
    unsigned char planes[8];         /* in  OPEN: plane bit mask per id, 0 = all planes */
    unsigned okMask;                 /* out ARM, CLOSE: bit i = ids[i] was found */
    unsigned pad;
};
struct NVRMFenceWait {               /* 48 bytes */
    unsigned long long seq;          /* in  WAIT_SEQ */
    unsigned long long waitedUs;     /* out */
    unsigned id;                     /* in: IOSurface id (WAIT) or channel handle (WAIT_SEQ) */
    unsigned planes;                 /* in  WAIT: plane bit mask, 0 = any */
    unsigned pid;                    /* in  WAIT: 0 = any writer, else only that process's holds */
    unsigned timeoutMs;              /* in */
    unsigned result;                 /* out: NVRM_FENCE_R_* */
    unsigned ownerPid;               /* out: pid of the first writer waited for, 0 = none */
    unsigned state;                  /* out: NVRM_FENCE_E_* of that writer's hold when last seen */
    unsigned blockers;               /* out: most holds this wait had in front of it at once */
};
struct NVRMFenceReq {                /* 96 bytes; the same struct in and out */
    unsigned version, op, status, flags;
    union { struct NVRMFenceQuery query; struct NVRMFenceChan chan; struct NVRMFenceHold hold;
            struct NVRMFenceWait wait; unsigned long long raw[10]; } u;
};
/* Kernel to kernel. NVAccel calls NVRM's callPlatformFunction(NVRM_FENCE_FN_OPS, p1 = struct NVRMFenceKernelOps *) once
 * and keeps the pointer. wait() takes the fence lock only (a leaf) and may sleep up to w->timeoutMs. */
#define NVRM_FENCE_FN_OPS "nvFenceOps"
struct NVRMFenceKernelOps {
    unsigned version;                /* in: NVRM_FENCE_ABI_VERSION */
    unsigned size;                   /* in: sizeof(struct NVRMFenceKernelOps) */
    unsigned (*wait)(struct NVRMFenceWait *w, unsigned flags);   /* out; returns NVRM_FENCE_ST_* */
};

#ifdef __cplusplus
#define NVRM_FENCE_SASSERT(c, n) static_assert(c, #n)
#else
#define NVRM_FENCE_SASSERT(c, n) _Static_assert(c, #n)
#endif
NVRM_FENCE_SASSERT(sizeof(struct NVRMFenceQuery) == 24, fence_query_size);
NVRM_FENCE_SASSERT(sizeof(struct NVRMFenceChan) == 32, fence_chan_size);
NVRM_FENCE_SASSERT(sizeof(struct NVRMFenceHold) == 72, fence_hold_size);
NVRM_FENCE_SASSERT(sizeof(struct NVRMFenceWait) == 48, fence_wait_size);
NVRM_FENCE_SASSERT(sizeof(struct NVRMFenceReq) == 96, fence_req_size);
NVRM_FENCE_SASSERT(sizeof(struct NVRMFenceKernelOps) == 16, fence_kernel_ops_size);
NVRM_FENCE_SASSERT(__builtin_offsetof(struct NVRMFenceReq, u) == 16, fence_req_union);
NVRM_FENCE_SASSERT(__builtin_offsetof(struct NVRMFenceHold, ids) == 24, fence_hold_ids);
NVRM_FENCE_SASSERT(__builtin_offsetof(struct NVRMFenceHold, okMask) == 64, fence_hold_ok);
NVRM_FENCE_SASSERT(__builtin_offsetof(struct NVRMFenceWait, id) == 16, fence_wait_id);
NVRM_FENCE_SASSERT(__builtin_offsetof(struct NVRMFenceWait, result) == 32, fence_wait_result);
NVRM_FENCE_SASSERT(__builtin_offsetof(struct NVRMFenceChan, errBase) == 24, fence_chan_errbase);
#endif
