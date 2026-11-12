/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

// One plane of a two-plane IOSurface (NV12: luma, then interleaved chroma) backed by its own VRAM.
//
// The accelerator family keeps one device cache, and so one NVResource and one NVVidMemory, per (surface, plane). The
// plane's VRAM holds that plane alone, from offset 0. The surface's system memory is one block holding both planes at
// offsets the kernel cannot read for itself (no plane-geometry accessor of IOSurface is linked, and a new import cannot
// be checked before the owner boots), so the plugin names the plane's byte range when it asks for the VRAM
// (NVRM_SS_PLANE_RANGED, nvrm_surfshare_abi.h). A page-off of such a resource copies the plane's VRAM into that range
// only, and marks the plane's own validity bit. A resource granted without a range is paged off exactly as before
// (offset 0, the smaller of the surface and the VRAM, bit 0).
//
// Pure: plain C, also valid C++, no kernel header. tools/test_plane_range.c holds every rule here.
#pragma once
#ifndef NVRM_SS_BAD_RANGE
#error "include nvrm_surfshare_abi.h (in a kernel unit: with NVRM_SS_KERNEL_ABI defined) before nvaccel_plane_range.h"
#endif

// size [in] of a ranged grant: the plane's byte offset in the surface's memory in the high 32 bits, its length (bytes
// per row times rows) in the low 32 bits.
static inline void nvaccel_range_decode(unsigned long long sizeWord, unsigned long long *off, unsigned long long *len)
{
    *off = sizeWord >> 32;
    *len = sizeWord & 0xffffffffull;
}

// The range against the plane's VRAM and the surface's memory: 0, NVRM_SS_TOO_SMALL (empty, or more than the VRAM
// holds), NVRM_SS_BAD_RANGE (it leaves the surface's memory). Subtraction only: off + len is never formed, so no input
// wraps past the test.
static inline unsigned nvaccel_range_check(unsigned long long off, unsigned long long len, unsigned long long mdLen,
                                           unsigned long long vramBytes)
{
    if (!len || len > vramBytes) return NVRM_SS_TOO_SMALL;
    if (off > mdLen || len > mdLen - off) return NVRM_SS_BAD_RANGE;
    return 0;
}

// A resource that already carries a range keeps it: the layout of an IOSurface never changes, so a second importer
// names the same bytes. A different range would move where another process's page-offs write: refused.
static inline int nvaccel_range_same(unsigned long long haveOff, unsigned long long haveLen, unsigned long long off,
                                     unsigned long long len)
{
    return !haveLen || (haveOff == off && haveLen == len);
}

// The bit of the family's validity words (valid[0]: system memory current, valid[6]: VRAM current) that this resource's
// only level uses: the one bit set in either word. Neither set (a fresh resource): bit 0, the bit the single-plane path
// has always used. More than one bit: not understood, 0.
static inline unsigned short nvaccel_valid_bit(unsigned short sysValid, unsigned short vramValid)
{
    const unsigned short m = (unsigned short)(sysValid | vramValid);
    if (!m) return 1;
    return (unsigned short)((m & (m - 1)) ? 0 : m);
}

// The bit a grant records: what the words say now, or 0 when they are not understood or name another bit than the one
// an earlier grant of the same resource recorded.
static inline unsigned short nvaccel_grant_bit(unsigned short haveBit, unsigned short sysValid, unsigned short vramValid)
{
    const unsigned short b = nvaccel_valid_bit(sysValid, vramValid);
    return (haveBit && b != haveBit) ? (unsigned short)0 : b;
}

// The bit a dirty mark sets (and a page-off clears or sets): the recorded bit of a ranged resource; bit 0 for plane 0 of
// any other, as it always was. 0 = refuse the mark: a plane above 0 without a range would be paged off over the start
// of the surface.
static inline unsigned short nvaccel_dirty_bit(unsigned long long rangeLen, unsigned short validBit, unsigned plane)
{
    if (rangeLen) return validBit;
    return (unsigned short)(plane == 0 ? 1 : 0);
}

// Whether a ranged grant may be made at all. on: not switched off by the boot-arg. unlockedWait: the page-off waits for
// the GPU without the accelerator lock (a plane is written by WindowServer's GPU and read by a CPU lock right after;
// with the locked wait every such lock would hold the accelerator). dmaMode: how this GPU's addresses reach the bus
// (nvrm_surfshare_abi.h). stageMapped: the shared staging buffer is mapped for this GPU. Without a mapper (mode 0) a
// plane can always be staged; behind one, only once a mapped buffer exists; a mapper the driver may not use (mode 3),
// never: the family's page-off is the only other copy, and it would put the wrong bytes in the wrong place.
static inline int nvaccel_planes_allowed(int on, int unlockedWait, unsigned dmaMode, int stageMapped)
{
    if (!on || !unlockedWait) return 0;
    if (dmaMode == 0) return 1;
    return (dmaMode == 1 || dmaMode == 2) && stageMapped;
}

// What a page-off copies: VRAM [0, *bytes) to the surface's memory at *dstOff. Not ranged: offset 0 and the smaller of
// the surface and the VRAM, as the page-off has always copied (the answer is then only *bytes != 0; the caller keeps
// its own handling of an empty copy). Ranged: the plane's range, or nothing (0) when it no longer fits the surface or
// the VRAM.
static inline int nvaccel_pageoff_span(int ranged, unsigned long long off, unsigned long long len,
                                       unsigned long long mdLen, unsigned long long vramBytes,
                                       unsigned long long *dstOff, unsigned long long *bytes)
{
    if (!ranged) {
        *dstOff = 0;
        *bytes = mdLen < vramBytes ? mdLen : vramBytes;
        return *bytes != 0;
    }
    if (nvaccel_range_check(off, len, mdLen, vramBytes) != 0) { *dstOff = 0; *bytes = 0; return 0; }
    *dstOff = off;
    *bytes = len;
    return 1;
}
