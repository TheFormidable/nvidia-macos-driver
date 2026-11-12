/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

// nvrm_dma_policy.h: the decisions of the DMA layer (nvrm_dma.cpp), with no kernel header, so that a host test can
// prove them (tools/test_dma_policy.c).
//
// Two worlds. With no DMA mapper for the GPU (no VT-d, VT-d off, or the DMAR table dropped) a bus address IS the
// physical address and the driver behaves exactly as 2.9: nv_requires_dma_remap() is false and nothing here runs after
// the mode is decided. With a mapper (AppleVTD) every address the GPU puts on the bus is translated, so the driver
// follows the Linux driver: the RM keeps physical addresses for the CPU and asks the OS layer (nv_dma_map_alloc) for
// the bus address of each allocation. A third state covers what cannot be made safe: a mapper is there and the driver
// may not or cannot use it. Then the GPU is not started, because starting it would send untranslated addresses
// through VT-d (2.9 did exactly that).
//
// enum nvrm_dma_mode is the one in the kernel half of nvrm_surfshare_abi.h (the accelerator reads the same numbers);
// that header is plain C and has no include of its own.

#ifndef NVRM_DMA_POLICY_H
#define NVRM_DMA_POLICY_H

#ifndef NVRM_SS_KERNEL_ABI
#define NVRM_SS_KERNEL_ABI 1
#endif
#include "nvrm_surfshare_abi.h"

#define NVRM_DMA_PAGE        4096ull
#define NVRM_DMA_PAGE_MASK   (NVRM_DMA_PAGE - 1)

/* optout          boot-arg nvrmdma=0 (the one diagnostic switch)
 * device_mapper   IOMapper::copyMapperForDevice(nub) returned a mapper of its own (not the system mapper itself)
 * system_mapper   IOMapper::gSystem is a live mapper
 * nub_bypass      the nub carries IOMemoryDescriptorOptions with kIOMemoryMapperNone: IOKit leaves it untranslated
 * Without a mapper the switch changes nothing: it can only ever turn "use the mapper" into "do not start". */
static inline enum nvrm_dma_mode nvrm_dma_decide(int optout, int device_mapper, int system_mapper, int nub_bypass)
{
    if (nub_bypass && !device_mapper) return NVRM_DMA_NONE;
    if (!device_mapper && !system_mapper) return NVRM_DMA_NONE;
    if (optout) return NVRM_DMA_REFUSE;
    return device_mapper ? NVRM_DMA_DEVICE : NVRM_DMA_SYSTEM;
}

static inline int nvrm_dma_remaps(enum nvrm_dma_mode m) { return m == NVRM_DMA_SYSTEM || m == NVRM_DMA_DEVICE; }

/* Why a GPU ended in its mode: the line for the log and the "nvrm-dma" registry property. */
enum nvrm_dma_why {
    NVRM_DMA_WHY_MODE = 0,        /* the mode itself says it all (none, system mapper, device mapper)            */
    NVRM_DMA_WHY_OPTOUT = 1,      /* a mapper translates the GPU and boot-arg nvrmdma=0 forbids using it          */
    NVRM_DMA_WHY_NOT_ROUTED = 2,  /* a mapper translates the GPU and this build does not route the RM's DMA calls
                                     through the DMA layer (the OS layer still answers them with physical addresses) */
    NVRM_DMA_WHY_SELFTEST = 3     /* the self-test mapping did not read back from the mapper                      */
};

/* After the decision: a mode that would use the mapper still needs the RM's DMA calls to come to this layer
 * (rm_routed) and the self-test mapping to read back (selftest_ok; not asked when not routed). Either missing turns it
 * into a refusal; there is no "carry on with physical addresses" with a mapper. Modes 0 and 3 pass through. */
static inline enum nvrm_dma_mode nvrm_dma_settle(enum nvrm_dma_mode m, int rm_routed, int selftest_ok, enum nvrm_dma_why *why)
{
    enum nvrm_dma_why w = (m == NVRM_DMA_REFUSE) ? NVRM_DMA_WHY_OPTOUT : NVRM_DMA_WHY_MODE;
    if (nvrm_dma_remaps(m) && !rm_routed) { m = NVRM_DMA_REFUSE; w = NVRM_DMA_WHY_NOT_ROUTED; }
    else if (nvrm_dma_remaps(m) && !selftest_ok) { m = NVRM_DMA_REFUSE; w = NVRM_DMA_WHY_SELFTEST; }
    if (why) *why = w;
    return m;
}

/* The address width a mapping is asked for: the width the RM set for the device, or 32 for an allocation the RM wanted
 * below 4 GB (MEMDESC_FLAGS_ALLOC_32BIT_ADDRESSABLE). Never above 64, never below 32. */
static inline unsigned nvrm_dma_bits(unsigned device_bits, int force32)
{
    unsigned b = (device_bits == 0 || device_bits > 64) ? 64 : device_bits;
    if (b < 32) b = 32;
    return force32 ? 32 : b;
}

/* Linux nv_dma_is_addressable: [base, base + bytes) lies below 2^bits and does not wrap. */
static inline int nvrm_dma_addressable(unsigned long long base, unsigned long long bytes, unsigned bits)
{
    unsigned long long last;
    if (!bytes) return 0;
    last = base + bytes - 1;
    if (last < base) return 0;
    if (bits >= 64) return 1;
    return last < (1ull << bits);
}

enum { NVRM_DMA_X_OK = 0, NVRM_DMA_X_SHORT = 1, NVRM_DMA_X_UNALIGNED = 2, NVRM_DMA_X_NOT_CONTIG = 3, NVRM_DMA_X_BAD = 4 };

/* Turn the segments a DMA command produced into what the RM wants in va_array: one entry (the base) for a contiguous
 * allocation, which is only right when the whole range is one run on the bus, otherwise one entry per 4 KiB page.
 * Segments are in order of offset; adjacent segments that continue each other count as one run. Only the first
 * page_count pages are written. */
static inline int nvrm_dma_expand(const unsigned long long *seg_addr, const unsigned long long *seg_len, unsigned nseg,
                                  unsigned long long page_count, int contig, unsigned long long *out)
{
    unsigned long long done = 0, run_end = 0;
    unsigned s;
    if (!seg_addr || !seg_len || !out || !nseg || !page_count) return NVRM_DMA_X_BAD;
    for (s = 0; s < nseg; s++) {
        unsigned long long a = seg_addr[s], l = seg_len[s], k;
        if (!l) return NVRM_DMA_X_BAD;
        if ((a & NVRM_DMA_PAGE_MASK) || ((l & NVRM_DMA_PAGE_MASK) && s + 1 != nseg)) return NVRM_DMA_X_UNALIGNED;
        if (a + l < a) return NVRM_DMA_X_BAD;
        if (contig) {
            if (s == 0) out[0] = a;
            else if (a != run_end) return NVRM_DMA_X_NOT_CONTIG;
            run_end = a + l;
            done += (l + NVRM_DMA_PAGE_MASK) / NVRM_DMA_PAGE;
            continue;
        }
        for (k = 0; k < l && done < page_count; k += NVRM_DMA_PAGE) out[done++] = a + k;
    }
    return done >= page_count ? NVRM_DMA_X_OK : NVRM_DMA_X_SHORT;
}

/* Number of runs on the bus: segments that do not continue the one before start a new run. */
static inline unsigned nvrm_dma_runs(const unsigned long long *seg_addr, const unsigned long long *seg_len, unsigned nseg)
{
    unsigned s, runs = nseg ? 1u : 0u;
    for (s = 1; s < nseg; s++) if (seg_addr[s] != seg_addr[s - 1] + seg_len[s - 1]) runs++;
    return runs;
}

/* How much the driver lets itself keep mapped at once. A mapper's address space is finite and AppleVTD's allocator
 * waits without a timeout when it is full, so the driver stops well before: half of the shared 128 GB space, or 6 GB
 * of the 8 GB a per-device space has. */
static inline unsigned long long nvrm_dma_budget(enum nvrm_dma_mode m)
{
    return m == NVRM_DMA_DEVICE ? (6ull << 30) : (64ull << 30);
}
static inline int nvrm_dma_budget_ok(enum nvrm_dma_mode m, unsigned long long live_bytes, unsigned long long add_bytes)
{
    const unsigned long long cap = nvrm_dma_budget(m);
    return add_bytes <= cap && live_bytes <= cap - add_bytes;
}

/* Which mappings are read back from the mapper's own tables page by page: the first 64 completely, then every 64th.
 * Every mapping has its first and last page read back regardless. */
static inline int nvrm_dma_verify_full(unsigned long long nth) { return nth < 64 || (nth & 63) == 0; }
/* Is page `page` of a mapping of `pages` pages read back? (full: nvrm_dma_verify_full said so; at most 4096 pages) */
static inline int nvrm_dma_verify_page(unsigned long long page, unsigned long long pages, int full)
{
    return page == 0 || page + 1 == pages || (full && page < 4096);
}

/* A page list handed to the copy engine (page-off) while a mapper is active must lie inside one range the driver
 * itself mapped for that purpose: every entry page aligned and a whole page inside [base, base + bytes). */
static inline int nvrm_dma_pages_inside(const unsigned long long *pages, unsigned long long n, unsigned long long base,
                                        unsigned long long bytes)
{
    unsigned long long i;
    if (!pages || !n || !bytes || base + bytes < base) return 0;
    for (i = 0; i < n; i++) {
        const unsigned long long p = pages[i];
        if ((p & NVRM_DMA_PAGE_MASK) || p < base || p - base >= bytes || bytes - (p - base) < NVRM_DMA_PAGE) return 0;
    }
    return 1;
}

/* The answer of the page-off check (nvrm_dma_pages_ok): mode 0 is always yes (physical is the bus), a refused GPU is
 * always no, and with a mapper only pages inside a mapping made for the page-off (inside_mapping) pass. */
static inline int nvrm_dma_pageoff_allowed(enum nvrm_dma_mode m, int inside_mapping)
{
    if (m == NVRM_DMA_NONE) return 1;
    return nvrm_dma_remaps(m) && inside_mapping;
}

static inline const char *nvrm_dma_why_text(enum nvrm_dma_mode m, enum nvrm_dma_why w)
{
    switch (m) {
    case NVRM_DMA_NONE:   return "none (bus address = physical address)";
    case NVRM_DMA_SYSTEM: return "system mapper";
    case NVRM_DMA_DEVICE: return "device mapper";
    case NVRM_DMA_REFUSE: break;
    }
    switch (w) {
    case NVRM_DMA_WHY_NOT_ROUTED:
        return "refused: a DMA mapper (VT-d) translates this GPU and this build does not send the RM's DMA requests to the mapper";
    case NVRM_DMA_WHY_SELFTEST:
        return "refused: a DMA mapper (VT-d) translates this GPU and the self-test mapping did not read back";
    case NVRM_DMA_WHY_OPTOUT:
    case NVRM_DMA_WHY_MODE:
        break;
    }
    return "refused: a DMA mapper (VT-d) translates this GPU and boot-arg nvrmdma=0 forbids using it";
}

#endif
