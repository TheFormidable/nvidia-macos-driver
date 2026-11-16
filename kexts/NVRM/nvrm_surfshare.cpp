/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

#include "nv-xnu.h"
#include <IOKit/IOLib.h>

extern "C" {
#include "nvtypes.h"
#include "nvos.h"
#include "nvmisc.h"
#include "nv-kernel-rmapi-ops.h"
#include "rs_access.h"
void NV_API_CALL rm_kernel_rmapi_op(nvidia_stack_t *sp, void *ops_cmd);
unsigned int nvrm_kapi_mem_handle(const void *mem);
int nvrm_kapi_dev_handles(const void *dev, unsigned int *c, unsigned int *d, unsigned int *s);
}
#define NVRM_SS_KERNEL_ABI 1
#include "nvrm_surfshare_abi.h"
#include "nvrm_dma.h"
#include "nvrm_vram_abi.h"

static inline void ssZero(void *p, unsigned long n) { volatile unsigned char *b = (volatile unsigned char *)p; while (n--) *b++ = 0; }

extern "C" int nvrm_surfshare_rm(struct NVRMSurfShareRm *r)
{
    if (!r || r->version != NVRM_SS_RM_VERSION || r->op != NVRM_SS_RM_SHARE_KAPI) { if (r) r->status = NVRM_SS_BAD_VERSION; return 0; }
    unsigned int c = 0, d = 0, s = 0;
    if (!r->kapiDevice || !r->kapiMemory || !nvrm_kapi_dev_handles(r->kapiDevice, &c, &d, &s) || !c) { r->status = NVRM_SS_OFF; return 0; }
    unsigned int m = nvrm_kapi_mem_handle(r->kapiMemory);
    if (!m) { r->status = NVRM_SS_NO_VRAM; return 0; }
    nvidia_kernel_rmapi_ops_t ops; ssZero(&ops, sizeof ops);
    ops.op = NV04_SHARE;
    ops.params.share.hClient = c; ops.params.share.hObject = m;
    ops.params.share.sharePolicy.type = RS_SHARE_TYPE_CLIENT; ops.params.share.sharePolicy.target = r->target;
    ops.params.share.sharePolicy.action = 0;
    RS_ACCESS_MASK_ADD(&ops.params.share.sharePolicy.accessMask, RS_ACCESS_DUP_OBJECT);
    rm_kernel_rmapi_op(NULL, &ops);
    r->status = ops.params.share.status; r->hClient = c; r->hObject = m;
    return 0;
}

#include <sys/sysctl.h>
#include <libkern/OSAtomic.h>
extern "C" volatile unsigned long long nvrm_pageoff_last_runs, nvrm_pageoff_last_pages, nvrm_pageoff_coalesced, nvrm_pageoff_perpage;
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_pageoff_runs, CTLFLAG_RD | CTLFLAG_LOCKED, (long long *)&nvrm_pageoff_last_runs, "runs in the last page-off");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_pageoff_pages, CTLFLAG_RD | CTLFLAG_LOCKED, (long long *)&nvrm_pageoff_last_pages, "pages in the last page-off");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_pageoff_coalesced, CTLFLAG_RD | CTLFLAG_LOCKED, (long long *)&nvrm_pageoff_coalesced, "page-offs copied per run");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_pageoff_perpage, CTLFLAG_RD | CTLFLAG_LOCKED, (long long *)&nvrm_pageoff_perpage, "page-offs copied per 4 KB page");
static volatile SInt32 gSsSysctl = 0;
extern "C" NV_STATUS nvrm_ce_copy_to_pages(void *nv, NvU32 hClient, NvU32 hMemory, const NvU64 *pages, NvU64 pageCount,
                                           NvU64 length, NvU64 *pUsec);
extern "C" int nvrm_surfshare_pageoff(struct NVRMSurfPageoff *p, void *nv)
{
    if (!p || p->version != NVRM_SS_RM_VERSION) { if (p) p->status = NVRM_SS_BAD_VERSION; return 0; }
    unsigned int c = 0, d = 0, s = 0;
    if (!p->kapiDevice || !p->kapiMemory || !nvrm_kapi_dev_handles(p->kapiDevice, &c, &d, &s) || !c) { p->status = NVRM_SS_OFF; return 0; }
    unsigned int m = nvrm_kapi_mem_handle(p->kapiMemory);
    if (!m) { p->status = NVRM_SS_NO_VRAM; return 0; }
    if (OSCompareAndSwap(0, 1, &gSsSysctl)) {
        sysctl_register_oid(&sysctl__debug_nvrm_pageoff_runs); sysctl_register_oid(&sysctl__debug_nvrm_pageoff_pages);
        sysctl_register_oid(&sysctl__debug_nvrm_pageoff_coalesced); sysctl_register_oid(&sysctl__debug_nvrm_pageoff_perpage);
    }
    // The copy engine writes to these addresses. With a DMA mapper they must be bus addresses of a buffer mapped
    // through it ("nvSurfDma"); a raw physical list would land wherever the mapper sends it. Without a mapper this is
    // always yes and the copy runs as before. Every caller of the page-off copy comes through here.
    if (!nvrm_dma_pages_ok((nv_state_t *)nv, (const NvU64 *)p->pages, p->pageCount)) { p->status = NVRM_SS_NO_DMA; return 0; }
    NvU64 us = 0;
    p->status = nvrm_ce_copy_to_pages(nv, c, m, (const NvU64 *)p->pages, p->pageCount, p->length, &us);
    p->usec = us;
    return 0;
}

extern "C" NV_STATUS nvrm_ce_clear_vidmem(void *nv, NvU32 hClient, NvU32 hMemory, NvU64 bytes, NvU64 *pCleared, NvU32 *pSent,
                                          NvU64 *pUsec);
// A framebuffer's new block of video memory cleared by the copy engine (nvrm_vram_abi.h NVRM_VRAM_FN_CLEAR): the RM
// handles of the NVKMS block, found as the page-off finds them, then nvrm_pageoff.c does the work. NVRM.cpp calls this
// only with the GPU running and the power gate entered, and has set result to NOT_RUN already.
extern "C" void nvrm_vidmem_clear(struct NVRMVidmemClear *q, void *nv)
{
    unsigned int c = 0, d = 0, s = 0;
    if (!q->kapiDevice || !q->kapiMemory || !q->bytes || !nvrm_kapi_dev_handles(q->kapiDevice, &c, &d, &s) || !c) return;
    unsigned int m = nvrm_kapi_mem_handle(q->kapiMemory);
    if (!m) return;
    NvU64 cleared = 0, us = 0; NvU32 sent = 0;
    const NV_STATUS st = nvrm_ce_clear_vidmem(nv, c, m, q->bytes, &cleared, &sent, &us);
    q->rmStatus = st; q->usec = us; q->cleared = cleared;
    q->result = st == NV_OK ? NVRM_VRAM_CLEAR_DONE : sent ? NVRM_VRAM_CLEAR_LOST : NVRM_VRAM_CLEAR_NOT_RUN;
}
