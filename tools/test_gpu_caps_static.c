#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "../kexts/NVRM/nvrm_gpu_caps_static.h"

int main(void)
{
    uint8_t a[NVRM_GC_BUF_BYTES], b[NVRM_GC_BUF_BYTES], reply[NVRM_GC_BUF_BYTES];
    uint32_t v[4], size;
    const struct NvrmGcIdentity one = {0x10de, 0x2d83, 0x1043, 0x8a3f, 0x030000a1, 0, 2, 0, 123};
    const struct NvrmGcIdentity two = {0x10de, 0x2684, 0x1234, 0x5678, 0x030000a2, 0, 3, 0, 456};
    nvrm_gc_begin(a, 0, NVRM_GC_F_PENDING, 0, NVRM_GC_POL_PUBLISHED_COUNTS, 0, 0);
    size = nvrm_gc_static_copy(a, reply, sizeof reply, 1, 0, 0);
    assert(size == 64 && nvrm_gc_verdict(0, 1, 4, reply, size) == NVRM_GC_V_PENDING);
    nvrm_gc_begin(a, 0, NVRM_GC_F_OFF, 0, NVRM_GC_POL_PUBLISHED_COUNTS, 0, 0);
    size = nvrm_gc_static_copy(a, reply, sizeof reply, 1, 0, 0);
    assert(size == 64 && nvrm_gc_verdict(0, 1, 4, reply, size) == NVRM_GC_V_OFF);
    nvrm_gc_static_publish(a, &one, 1, 0, "620.70.06");
    nvrm_gc_static_publish(b, &two, 1, 0, "620.70.06");
    size = nvrm_gc_static_copy(a, reply, sizeof reply, 1, 0, 0);
    assert(nvrm_gc_verdict(0, 1, 4, reply, size) == NVRM_GC_V_USABLE);
    assert(nvrm_gc_get(reply, size, NVRM_GC_CAP_PCI_DEVICE, 0, v) == NVRM_GC_ST_VERIFIED && v[0] == one.device);
    assert(nvrm_gc_get(reply, size, NVRM_GC_CAP_FAN_INTERFACE, 0, v) == NVRM_GC_ST_UNKNOWN);
    assert(nvrm_gc_get(reply, size, NVRM_GC_CAP_FAN_STOP, 0, v) == NVRM_GC_ST_UNKNOWN);
    char version[64];
    assert(nvrm_gc_get_string(reply, size, NVRM_GC_CAP_RM_VERSION, 0, version, sizeof version) == NVRM_GC_ST_VERIFIED);
    assert(!strcmp(version, "620.70.06"));
    size = nvrm_gc_static_copy(b, reply, sizeof reply, 1, 0, 0);
    assert(nvrm_gc_get(reply, size, NVRM_GC_CAP_PCI_DEVICE, 0, v) == NVRM_GC_ST_VERIFIED && v[0] == two.device);
    assert(nvrm_gc_get(reply, size, NVRM_GC_CAP_RM_GPU_ID, 0, v) == NVRM_GC_ST_VERIFIED && v[0] == two.gpu_id);
    size = nvrm_gc_static_copy(a, reply, sizeof reply, 1, NVRM_GC_PM_SUSPENDED, 1);
    assert(nvrm_gc_verdict(0, 1, 4, reply, size) == NVRM_GC_V_STALE);
    assert(nvrm_gc_get(reply, size, NVRM_GC_CAP_PCI_DEVICE, 0, v) == NVRM_GC_ST_VERIFIED);
    size = nvrm_gc_static_copy(a, reply, sizeof reply, 1, NVRM_GC_PM_LOST, 1);
    assert(nvrm_gc_verdict(0, 1, 4, reply, size) == NVRM_GC_V_LOST);
    nvrm_gc_static_publish(a, &one, 2, 1, "620.70.06");
    size = nvrm_gc_static_copy(a, reply, sizeof reply, 1, NVRM_GC_PM_RUNNING, 1);
    assert(nvrm_gc_verdict(0, 1, 4, reply, size) == NVRM_GC_V_USABLE);
    assert(nvrm_gc_rd32(reply, NVRM_GC_HO_GENERATION) == 2);
    size = nvrm_gc_static_copy(a, reply, 64, UINT64_C(0x100000001), 0, 1);
    assert(size == 64 && nvrm_gc_rd16(reply, NVRM_GC_HO_RECORD_COUNT) == 0);
    memset(reply, 0x55, sizeof reply);
    assert(nvrm_gc_static_copy(a, reply, 63, 1, 0, 1) == 0 && reply[0] == 0x55);
    assert(nvrm_gc_static_copy(a, reply, 4097, 1, 0, 1) == 0 && reply[0] == 0x55);
    puts("PASS: static producer/cache (pending, off, per-GPU, unknown, suspend, lost, resume, major, capacity)");
    return 0;
}
