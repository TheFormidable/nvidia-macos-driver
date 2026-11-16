/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

// NVIDIA's open-gpu-kernel-modules headers (third-party) carry their own integer-conversion warnings under the kernel
// compiler's defaults; the exception is scoped to these includes and our code below stays checked.
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wsign-conversion"
#pragma clang diagnostic ignored "-Wimplicit-int-conversion"
#pragma clang diagnostic ignored "-Wshorten-64-to-32"
#include <nv.h>
#include <nv-priv.h>
#include <os/os.h>
#include <osapi.h>
#include "gpu/gpu.h"
#include <core/thread_state.h>
#include <core/locks.h>
#include <mem_mgr/mem.h>
#include "kernel/gpu/mem_mgr/mem_mgr.h"
#include "kernel/gpu/mem_mgr/ce_utils.h"
#include "ctrl/ctrl0050.h"
#include "rmapi/rs_utils.h"
#include "resserv/rs_server.h"
#include "resserv/rs_client.h"
#pragma clang diagnostic pop

NV_STATUS nvrm_ce_copy_to_pages(nv_state_t *nv, NvU32 hClient, NvU32 hMemory, const NvU64 *pages, NvU64 pageCount,
                                NvU64 length, NvU64 *pUsec);
NV_STATUS nvrm_ce_clear_vidmem(nv_state_t *nv, NvU32 hClient, NvU32 hMemory, NvU64 bytes, NvU64 *pCleared, NvU32 *pSent,
                               NvU64 *pUsec);

volatile NvU64 nvrm_pageoff_last_runs, nvrm_pageoff_last_pages, nvrm_pageoff_coalesced, nvrm_pageoff_perpage;

static NV_STATUS _nvrmCopyRun(OBJGPU *pGpu, struct CeUtils *pCe, MEMORY_DESCRIPTOR *pSrc, NvU64 srcOff, NvU64 phys, NvU64 bytes,
                              NvU64 *pWorkId)
{
    MEMORY_DESCRIPTOR *pDst = NULL;
    NV_STATUS st = memdescCreate(&pDst, pGpu, bytes, 0, NV_TRUE, ADDR_SYSMEM, NV_MEMORY_CACHED, MEMDESC_FLAGS_NONE);
    if (st != NV_OK) return st;
    memdescDescribe(pDst, ADDR_SYSMEM, phys, bytes);
    CEUTILS_MEMCOPY_PARAMS p = {0};
    p.pDstMemDesc = pDst;  p.dstOffset = 0;
    p.pSrcMemDesc = pSrc;  p.srcOffset = srcOff;
    p.length = bytes;
    p.flags = NV0050_CTRL_MEMSET_FLAGS_ASYNC;
    st = ceutilsMemcopy(pCe, &p);
    if (st == NV_OK) *pWorkId = p.submittedWorkId;
    memdescDestroy(pDst);
    return st;
}

static NV_STATUS _nvrmCopyLocked(OBJGPU *pGpu, NvU32 hClient, NvU32 hMemory, const NvU64 *pages, NvU64 pageCount, NvU64 length)
{
    MemoryManager *pMemoryManager = GPU_GET_MEMORY_MANAGER(pGpu);
    RsClient *pClient = NULL;
    Memory *pMemory = NULL;
    MEMORY_DESCRIPTOR *pDst = NULL;
    NV_STATUS st;

    if (pMemoryManager == NULL) return NV_ERR_INVALID_STATE;
    if (pMemoryManager->pCeUtils == NULL) {
        st = memmgrInitCeUtils(pMemoryManager, NV_FALSE, NV_TRUE);
        if (st != NV_OK) return st;
    }
    st = serverGetClientUnderLock(&g_resServ, hClient, &pClient);
    if (st != NV_OK) return st;
    st = memGetByHandle(pClient, hMemory, &pMemory);
    if (st != NV_OK) return st;
    if (length > memdescGetSize(pMemory->pMemDesc) || length > pageCount * RM_PAGE_SIZE) return NV_ERR_INVALID_ARGUMENT;

    NvU64 runs = 1, i;
    for (i = 1; i < pageCount; i++) if (pages[i] != pages[i - 1] + RM_PAGE_SIZE) runs++;
    nvrm_pageoff_last_runs = runs; nvrm_pageoff_last_pages = pageCount;
    if (runs * 2 <= pageCount) {
        NvU64 off = 0, workId = 0, n;
        nvrm_pageoff_coalesced++;
        for (i = 0; off < length && i < pageCount; i += n) {
            for (n = 1; i + n < pageCount && pages[i + n] == pages[i] + n * RM_PAGE_SIZE; n++) {}
            NvU64 bytes = n * RM_PAGE_SIZE;
            if (bytes > length - off) bytes = length - off;
            st = _nvrmCopyRun(pGpu, pMemoryManager->pCeUtils, pMemory->pMemDesc, off, pages[i], bytes, &workId);
            if (st != NV_OK) break;
            off += bytes;
        }
        if (st == NV_OK) {
            NvU32 spins = 0;
            while (ceutilsUpdateProgress(pMemoryManager->pCeUtils) < workId) {
                if (++spins > 200000) { st = NV_ERR_TIMEOUT; break; }
                osDelayUs(10);
            }
        }
        return st;
    }
    nvrm_pageoff_perpage++;

    st = memdescCreate(&pDst, pGpu, pageCount * RM_PAGE_SIZE, 0, NV_FALSE, ADDR_SYSMEM, NV_MEMORY_CACHED, MEMDESC_FLAGS_NONE);
    if (st != NV_OK) return st;
    memdescFillPages(pDst, 0, (NvU64 *)pages, (NvU32)pageCount, RM_PAGE_SIZE);

    CEUTILS_MEMCOPY_PARAMS p = {0};
    p.pDstMemDesc = pDst;  p.dstOffset = 0;
    p.pSrcMemDesc = pMemory->pMemDesc;  p.srcOffset = 0;
    p.length = length;
    st = ceutilsMemcopy(pMemoryManager->pCeUtils, &p);
    if (st == NV_OK) {
        NvU32 spins = 0;
        while (ceutilsUpdateProgress(pMemoryManager->pCeUtils) < p.submittedWorkId) {
            if (++spins > 200000) { st = NV_ERR_TIMEOUT; break; }
            osDelayUs(10);
        }
    }
    memdescDestroy(pDst);
    return st;
}

// A framebuffer's new block of video memory, cleared by the copy engine (nvrm_vram_abi.h NVRM_VRAM_FN_CLEAR). Every
// check that can refuse comes before anything is sent, including ceutilsMemset's own (paused queue, the block's GPU,
// offset and length), so that a refusal leaves the block untouched and *pSent at 0. From the memset on, *pSent is 1:
// an error then may leave work in the engine's queue that still writes zeros into the block. The end is waited for as
// the page-off waits for its copy (at most 200000 x 10 us); the block is only ever cleared from its start, and never
// beyond the RM's own size for it.
static NV_STATUS _nvrmClearLocked(OBJGPU *pGpu, NvU32 hClient, NvU32 hMemory, NvU64 bytes, NvU64 *pCleared, NvU32 *pSent)
{
    MemoryManager *pMemoryManager = GPU_GET_MEMORY_MANAGER(pGpu);
    RsClient *pClient = NULL;
    Memory *pMemory = NULL;
    NV_STATUS st;

    if (pMemoryManager == NULL) return NV_ERR_INVALID_STATE;
    if (pMemoryManager->pCeUtils == NULL) {
        st = memmgrInitCeUtils(pMemoryManager, NV_FALSE, NV_TRUE);
        if (st != NV_OK) return st;
    }
    if (ceutilsIsSubmissionPaused(pMemoryManager->pCeUtils)) return NV_ERR_BUSY_RETRY;
    st = serverGetClientUnderLock(&g_resServ, hClient, &pClient);
    if (st != NV_OK) return st;
    st = memGetByHandle(pClient, hMemory, &pMemory);
    if (st != NV_OK) return st;
    MEMORY_DESCRIPTOR *pMemDesc = pMemory->pMemDesc;
    if (pMemDesc == NULL || pMemDesc->pGpu != pGpu || memdescGetAddressSpace(pMemDesc) != ADDR_FBMEM)
        return NV_ERR_INVALID_ARGUMENT;
    const NvU64 size = memdescGetSize(pMemDesc);
    const NvU64 length = bytes < size ? bytes : size;
    if (length == 0) return NV_ERR_INVALID_ARGUMENT;

    CEUTILS_MEMSET_PARAMS p = {0};
    p.pMemDesc = pMemDesc;  p.offset = 0;
    p.length = length;      p.pattern = 0;
    p.flags = NV0050_CTRL_MEMSET_FLAGS_ASYNC;
    *pSent = 1;
    st = ceutilsMemset(pMemoryManager->pCeUtils, &p);
    if (st != NV_OK) return st;
    NvU32 spins = 0;
    while (ceutilsUpdateProgress(pMemoryManager->pCeUtils) < p.submittedWorkId) {
        if (++spins > 200000) return NV_ERR_TIMEOUT;
        osDelayUs(10);
    }
    *pCleared = length;
    return NV_OK;
}

NV_STATUS nvrm_ce_clear_vidmem(nv_state_t *nv, NvU32 hClient, NvU32 hMemory, NvU64 bytes, NvU64 *pCleared, NvU32 *pSent,
                               NvU64 *pUsec)
{
    THREAD_STATE_NODE threadState;
    NV_STATUS st;
    void *fp;
    nvidia_stack_t *sp = NULL;
    NvU64 t0 = 0, t1 = 0;
    OBJGPU *pGpu = NV_GET_NV_PRIV_PGPU(nv);

    *pCleared = 0; *pSent = 0;
    if (pGpu == NULL || bytes == 0) return NV_ERR_INVALID_ARGUMENT;
    NV_ENTER_RM_RUNTIME(sp, fp);
    threadStateInit(&threadState, THREAD_STATE_FLAGS_NONE);
    t0 = osGetMonotonicTimeNs();
    if ((st = rmapiLockAcquire(API_LOCK_FLAGS_NONE, RM_LOCK_MODULES_OSAPI)) == NV_OK) {
        if ((st = rmDeviceGpuLocksAcquire(pGpu, GPUS_LOCK_FLAGS_NONE, RM_LOCK_MODULES_OSAPI)) == NV_OK) {
            st = _nvrmClearLocked(pGpu, hClient, hMemory, bytes, pCleared, pSent);
            rmDeviceGpuLocksRelease(pGpu, GPUS_LOCK_FLAGS_NONE, NULL);
        }
        rmapiLockRelease();
    }
    t1 = osGetMonotonicTimeNs();
    if (pUsec) *pUsec = (t1 - t0) / 1000;
    threadStateFree(&threadState, THREAD_STATE_FLAGS_NONE);
    NV_EXIT_RM_RUNTIME(sp, fp);
    return st;
}

NV_STATUS nvrm_ce_copy_to_pages(nv_state_t *nv, NvU32 hClient, NvU32 hMemory, const NvU64 *pages, NvU64 pageCount,
                                NvU64 length, NvU64 *pUsec)
{
    THREAD_STATE_NODE threadState;
    NV_STATUS st;
    void *fp;
    nvidia_stack_t *sp = NULL;
    NvU64 t0 = 0, t1 = 0;
    OBJGPU *pGpu = NV_GET_NV_PRIV_PGPU(nv);

    if (pGpu == NULL || pages == NULL || pageCount == 0 || length == 0) return NV_ERR_INVALID_ARGUMENT;
    NV_ENTER_RM_RUNTIME(sp, fp);
    threadStateInit(&threadState, THREAD_STATE_FLAGS_NONE);
    t0 = osGetMonotonicTimeNs();
    if ((st = rmapiLockAcquire(API_LOCK_FLAGS_NONE, RM_LOCK_MODULES_OSAPI)) == NV_OK) {
        if ((st = rmDeviceGpuLocksAcquire(pGpu, GPUS_LOCK_FLAGS_NONE, RM_LOCK_MODULES_OSAPI)) == NV_OK) {
            st = _nvrmCopyLocked(pGpu, hClient, hMemory, pages, pageCount, length);
            rmDeviceGpuLocksRelease(pGpu, GPUS_LOCK_FLAGS_NONE, NULL);
        }
        rmapiLockRelease();
    }
    t1 = osGetMonotonicTimeNs();
    if (pUsec) *pUsec = (t1 - t0) / 1000;
    threadStateFree(&threadState, THREAD_STATE_FLAGS_NONE);
    NV_EXIT_RM_RUNTIME(sp, fp);
    return st;
}
