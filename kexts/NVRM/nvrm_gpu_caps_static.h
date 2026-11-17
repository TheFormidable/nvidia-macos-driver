#pragma once
#include "nvrm_gpu_capabilities_abi.h"

/* Phase 1: cached PCI identity only. This part has no RM, IOKit or firmware
 * dependencies, and can be tested on a host. Missing capabilities are UNKNOWN.
 * Do not convert a PCI identity or example block into permission to write. */
struct NvrmGcIdentity {
    uint32_t vendor, device, subsystem_vendor, subsystem_device;
    uint32_t class_revision, domain, bus, slot, gpu_id;
};

static inline void nvrm_gc_static_publish(uint8_t *out, const struct NvrmGcIdentity *id,
                                          uint32_t generation, uint32_t cycle, const char *rm_version)
{
    nvrm_gc_begin(out, generation, NVRM_GC_F_PRODUCED | NVRM_GC_F_PARTIAL, cycle,
                  NVRM_GC_POL_PUBLISHED_COUNTS, 0, 0);
    const uint32_t pci = NVRM_GC_SRC_PCI_CONFIG;
    const uint32_t kernel = NVRM_GC_SRC_KERNEL_STATE;
    const uint32_t valid = NVRM_GC_ANS_VALID;
    if (rm_version) nvrm_gc_put_string(out, NVRM_GC_CAP_RM_VERSION, 0, NVRM_GC_SRC_DRIVER_BUILD,
                                      valid, 0, 0, 0, rm_version);
    if (id->vendor == 0x10deu && id->device != 0xffffu && id->device != 0u) {
        nvrm_gc_put(out, NVRM_GC_CAP_PCI_VENDOR, 0, pci, valid, 0, 0, 0, id->vendor, 0, 0, 0);
        nvrm_gc_put(out, NVRM_GC_CAP_PCI_DEVICE, 0, pci, valid, 0, 0, 0, id->device, 0, 0, 0);
        if (id->subsystem_vendor != 0xffffu && id->subsystem_vendor != 0u && id->subsystem_device != 0xffffu) {
            nvrm_gc_put(out, NVRM_GC_CAP_PCI_SUBSYS_VENDOR, 0, pci, valid, 0, 0, 0, id->subsystem_vendor, 0, 0, 0);
            nvrm_gc_put(out, NVRM_GC_CAP_PCI_SUBSYS_DEVICE, 0, pci, valid, 0, 0, 0, id->subsystem_device, 0, 0, 0);
        }
        if (id->class_revision != 0xffffffffu) {
            nvrm_gc_put(out, NVRM_GC_CAP_PCI_REVISION, 0, pci, valid, 0, 0, 0, id->class_revision & 0xffu, 0, 0, 0);
            nvrm_gc_put(out, NVRM_GC_CAP_PCI_CLASS, 0, pci, valid, 0, 0, 0, id->class_revision >> 24,
                        (id->class_revision >> 16) & 0xffu, (id->class_revision >> 8) & 0xffu, 0);
        }
        nvrm_gc_put(out, NVRM_GC_CAP_PCI_LOCATION, 0, kernel, valid, 0, 0, 0, id->domain, id->bus, id->slot, 0);
        if (id->gpu_id != 0xffffffffu)
            nvrm_gc_put(out, NVRM_GC_CAP_RM_GPU_ID, 0, kernel, valid, 0, 0, 0, id->gpu_id, 0, 0, 0);
    }
}

/* Caller holds the cache leaf lock. No allocation, no hardware access. A short
 * buffer is never modified. For an unknown ABI major return only the header. */
static inline uint32_t nvrm_gc_static_copy(const uint8_t *cache, uint8_t *out, uint32_t cap,
                                          uint64_t major, uint32_t pm, uint32_t cycle)
{
    if (!cache || !out || cap > NVRM_GC_BUF_BYTES) return 0;
    const uint32_t total = nvrm_gc_rd32(cache, NVRM_GC_HO_TOTAL);
    if (nvrm_gc_check(cache, total) != NVRM_GC_OK) return 0;
    const uint32_t bytes = major == NVRM_GC_ABI_MAJOR ? total : NVRM_GC_HEADER_BYTES_1_0;
    if (cap < bytes) return 0;
    for (uint32_t i = 0; i < bytes; ++i) out[i] = cache[i];
    nvrm_gc_mark_copy(out, pm, cycle);
    if (major != NVRM_GC_ABI_MAJOR) nvrm_gc_header_only(out);
    return bytes;
}
