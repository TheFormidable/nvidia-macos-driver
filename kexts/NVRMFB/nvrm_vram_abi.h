/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

#pragma once

#define NVRM_VRAM_ABI_VERSION 2u
#define NVRM_VRAM_FN_ALLOC    "nvAllocVram"
#define NVRM_VRAM_FN_FREE     "nvFreeVram"

#define NVRM_VRAM_FN_SCANOUT  "nvScanoutInfo"
// Reply flag of nvScanoutInfo (NVRMVramRequest::flags): the scanout is 30-bit X2B10G10R10, not 8-bit X8R8G8B8. A framebuffer that
// never sets it leaves the 8-bit layout, which is what every accelerator assumed before.
#define NVRM_VRAM_SCANOUT_30BIT 0x1u

#define NVRM_VRAM_BAR1_BUDGET (192ull * 1024ull * 1024ull)

// Asked of NVRM by a framebuffer: does the RM clear video memory before a client gets it (its scrub-on-free; see
// nvrm_query_vidmem_cleared)? cleared: 1 yes and the GPU has not been through a standby yet, 0 no (the RM does not, or
// the GPU has: what was free then came back as it was); the call fails while NVRM has no answer from the RM. An NVRM
// that does not know the name passes it up its providers, where nobody answers: the framebuffer keeps its own zero
// fill.
#define NVRM_VRAM_FN_CLEARED  "nvVidmemCleared"
struct NVRMVidmemCleared {
    unsigned int version;
    unsigned int cleared;
};

// Asked of NVRM by a framebuffer (3.0.1): clear the first `bytes` of one of its blocks of video memory with the GPU's
// copy engine, instead of writing zeros through BAR1 from the processor (27 ms for the 9 MB of a captured frame).
// kapiDevice and kapiMemory are the framebuffer's NVKMS device and the block. The call never waits for a standby:
// with the GPU not running it answers kIOReturnNotReady and runs nothing. result says what the block holds after it:
//   NVRM_VRAM_CLEAR_DONE     the engine finished before the call returned: `cleared` bytes from the start are zero
//   NVRM_VRAM_CLEAR_NOT_RUN  nothing was sent to the engine: the block is as it was, fill it as before
//   NVRM_VRAM_CLEAR_LOST     work was sent and its end was not seen: the engine may still write zeros into the block,
//                            so it must never be handed out or freed (keep it until restart)
// rmStatus is the RM's NV_STATUS and usec the time NVRM spent, for the log and the counters. An NVRM that does not
// know the name passes it up its providers, where nobody answers: the framebuffer keeps its own fill.
#define NVRM_VRAM_FN_CLEAR       "nvVidmemClear"
#define NVRM_VRAM_CLEAR_VERSION  1u
#define NVRM_VRAM_CLEAR_DONE     0u
#define NVRM_VRAM_CLEAR_NOT_RUN  1u
#define NVRM_VRAM_CLEAR_LOST     2u
struct NVRMVidmemClear {
    unsigned int       version;
    unsigned int       result;
    void              *kapiDevice;
    void              *kapiMemory;
    unsigned long long bytes;
    unsigned long long cleared;
    unsigned long long usec;
    unsigned int       rmStatus;
    unsigned int       pad;
};

struct NVRMVramRequest {
    unsigned int       version;
    unsigned int       flags;
    unsigned long long size;
    unsigned long long actualSize;
    unsigned long long phys;
    void              *kva;
    void              *handle;
    unsigned long long mappedTotal;
    unsigned int width, height, pitch;
    unsigned long long allocationCookie;
};
