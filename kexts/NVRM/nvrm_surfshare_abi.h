/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

#pragma once
#define NVRM_ESC_SURF_VRAM     0xE2u
#define NVRM_ESC_SURF_DIRTY    0xE3u
#define NVRM_SURFSHARE_VERSION 2u
/* NVRM_ESC_SURF_DIRTY with this plane bit is a WindowServer completion mark (bit 0: 1 pending, 0 ready), not a dirty plane */
#define NVRM_SS_PLANE_GPU_MARK 0x80000000u
/* NVRM_ESC_SURF_DIRTY with this plane bit gives up with NVRM_SS_BUSY instead of waiting for the accelerator lock
 * (accelerators that publish NVAccelSurfaceWait) */
#define NVRM_SS_PLANE_TRYLOCK  0x40000000u
/* NVRM_ESC_SURF_VRAM only, with this plane bit: the grant is for one plane of a surface whose planes live in one block
 * of system memory, and `size` carries the plane's place in that block instead of a byte count:
 *   size [in]  = (byte offset of the plane in the surface's memory << 32) | bytes of the plane (bytesPerRow * rows)
 *   size [out] = as without the flag (the bytes of the video memory that was shared)
 * Offset and length are 32 bits each, so a surface of 4 GB or more is not eligible. A kernel that does not know the
 * flag sees a plane above 15 and answers NVRM_SS_NO_CACHE, so the caller needs no version check. The three plane bits
 * are the top three of the word; the plane index is what remains. */
#define NVRM_SS_PLANE_RANGED   0x20000000u
/* Status values. Each is 0x8000000N with its own N: tools/test_surfshare_status.c fails when two of them are given the
 * same number. NVK and the plugin carry their own list of the values they name (NVK's copy of this file ends at
 * NVRM_SS_TOO_SMALL and forwards `plane`, `size` and `status` untouched); the struct and the escape numbers are the
 * same everywhere. */
#define NVRM_SS_BAD_VERSION   0x80000001u
#define NVRM_SS_OFF           0x80000002u
#define NVRM_SS_NO_SURFACE    0x80000003u
#define NVRM_SS_NO_CACHE      0x80000004u
#define NVRM_SS_NO_VRAM       0x80000005u
#define NVRM_SS_TOO_SMALL     0x80000006u
#define NVRM_SS_BUSY          0x80000007u
/* a DMA mapper (VT-d) translates this GPU and the pages offered are not mapped through it: nothing was copied */
#define NVRM_SS_NO_DMA        0x80000008u
/* NVRM_SS_PLANE_RANGED: the range leaves the surface's memory, the planes share one resource, or the resource's
 * validity word is not understood */
#define NVRM_SS_BAD_RANGE     0x80000009u
struct NVRMSurfShareEsc {
    unsigned int version;
    unsigned int surfaceID;
    unsigned int plane;
    unsigned int hClient;
    unsigned int hSrcClient;
    unsigned int hSrcMemory;
    unsigned int status;
    unsigned int pad;
    unsigned long long size;
};

#ifdef NVRM_SS_KERNEL_ABI
#define NVACCEL_SS_FN   "nvSurfVram"
#define NVRM_SS_FN_PAGEOFF "nvSurfPageoff"
#define NVRM_SS_FN_RM   "nvSurfShareRm"
#define NVRM_SS_RM_VERSION 2u
enum { NVRM_SS_RM_SHARE_KAPI = 5 };
struct NVRMSurfShareRm {
    unsigned int version, op;
    void *kapiDevice, *kapiMemory;
    unsigned int target;
    unsigned int hClient, hObject;
    unsigned int status;
};
struct NVRMSurfPageoff {
    unsigned int version;
    unsigned int status;
    void *kapiDevice, *kapiMemory;
    const unsigned long long *pages;
    unsigned long long pageCount;
    unsigned long long length;
    unsigned long long usec;
};

/* "nvSurfDma": the accelerator asks NVRM for the bus address of a buffer it owns, on a machine where a DMA mapper
 * (VT-d) translates the GPU. The copy engine is given bus addresses, so with a mapper a raw physical page list is
 * refused (NVRM_SS_NO_DMA above) and the accelerator's staging buffer has to be mapped here first.
 *
 *   callPlatformFunction(NVRM_SS_FN_DMA, false, (void *)&q, NULL, NULL, NULL) on the NVRM instance of the GPU
 *
 * Thread context only (the call can sleep). It touches no hardware and makes no RM call, so it works while the GPU
 * is suspended or lost. The accelerator may hold its staging lock across it; NVRM takes only its own DMA list lock,
 * which is a leaf, and never calls back. The answer is in `status`; the IOReturn is kIOReturnSuccess whenever the
 * function is known. kIOReturnUnsupported means an NVRM without the function, which never translates: treat it as
 * mode 0.
 *
 *   NVRM_SS_DMA_QUERY  out: mode, status 0. Costs nothing; ask once per GPU start.
 *   NVRM_SS_DMA_MAP    in: md, length. out: busBase, token, status 0. Byte k of the descriptor is at busBase + k on
 *                      the bus (one run), for `length` rounded up to 4 KiB. The descriptor must stay prepared and
 *                      alive until the UNMAP. status NVRM_SS_OFF: mode is 0 or 3, or an argument is missing (in mode
 *                      0 the physical addresses are the bus addresses: use them as before; in mode 3 the GPU was not
 *                      started). NVRM_SS_TOO_SMALL: length is beyond the descriptor. NVRM_SS_NO_DMA: the mapper
 *                      refused. On any status but 0 nothing is mapped and token is unchanged.
 *   NVRM_SS_DMA_UNMAP  in: token from a MAP. out: token NULL, status 0 (NVRM_SS_OFF when the token was not live:
 *                      already unmapped, or dropped when its GPU stopped; nothing to do either way).
 * A wrong `version` answers NVRM_SS_BAD_VERSION and nothing else is read or written.
 * A mapping still live when its GPU stops is removed by NVRM; the token is then dead and an UNMAP of it is harmless. */
#define NVRM_SS_FN_DMA      "nvSurfDma"
#define NVRM_SS_DMA_VERSION 1u
enum { NVRM_SS_DMA_QUERY = 0, NVRM_SS_DMA_MAP = 1, NVRM_SS_DMA_UNMAP = 2 };
/* How a GPU's addresses reach the bus: NVRMSurfDma.mode, and what NVRM's nvrm_dma_mode_of() answers (nvrm_dma.h) */
enum nvrm_dma_mode {
    NVRM_DMA_NONE   = 0,   /* no mapper: a bus address is the physical address, as it always was                  */
    NVRM_DMA_SYSTEM = 1,   /* the system mapper translates this GPU                                               */
    NVRM_DMA_DEVICE = 2,   /* IOKit gave this GPU a mapper of its own                                             */
    NVRM_DMA_REFUSE = 3    /* a mapper translates this GPU and the driver may not or cannot use it: not started   */
};
struct NVRMSurfDma {                /* 48 bytes, LP64 */
    unsigned int version, op;       /*  0, 4 */
    unsigned int status;            /*  8  out: 0 or NVRM_SS_* */
    unsigned int mode;              /* 12  out: enum nvrm_dma_mode: 0 untranslated, 1 system mapper, 2 device mapper, 3 refused */
    void *md;                       /* 16  MAP in: IOMemoryDescriptor *, prepared by the caller, kIODirectionInOut */
    unsigned long long length;      /* 24  MAP in: bytes from offset 0 (rounded up to 4 KiB) */
    unsigned long long busBase;     /* 32  MAP out: bus address of byte 0; the range is one run on the bus */
    void *token;                    /* 40  MAP out, UNMAP in */
};
typedef char NVRMSurfDmaSizeCheck[sizeof(struct NVRMSurfDma) == 48 ? 1 : -1];
#endif
