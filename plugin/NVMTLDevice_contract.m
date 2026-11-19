/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

#import <Metal/Metal.h>
#import "NVMTLObjects.h"
#import <IOSurface/IOSurfaceRef.h>
#include <mach/mach.h>
#include <mach/mach_vm.h>
#include "nvmtl_nocopy_once.h"
_Static_assert(VM_PROT_WRITE == NVMTL_NCO_PROT_WRITE && SM_COW == NVMTL_NCO_SM_COW && SM_PRIVATE == NVMTL_NCO_SM_PRIVATE &&
               SM_EMPTY == NVMTL_NCO_SM_EMPTY && SM_SHARED == NVMTL_NCO_SM_SHARED && SM_TRUESHARED == NVMTL_NCO_SM_TRUESHARED &&
               SM_PRIVATE_ALIASED == NVMTL_NCO_SM_PRIVATE_ALIASED && SM_SHARED_ALIASED == NVMTL_NCO_SM_SHARED_ALIASED,
               "nvmtl_nocopy_once.h no longer has the values of <mach/vm_prot.h> and <mach/vm_region.h>");
// The mapping region of this task that holds addr, or the first one above it (nvmtl_nocopy_once.h). The basic answer
// is asked first: it reads the region's map entry only. The extended answer, which says how the region is shared and
// whether a pager backs it, walks every page of the entry, so it is asked only of a region the basic answer leaves
// possible (no write access, not shared); the others are refused from the basic answer as they are.
static int nvmtl_nco_query_task(void *context, uint64_t addr, nvmtl_nco_region *out)
{
    (void)context;
    mach_vm_address_t at = (mach_vm_address_t)addr; mach_vm_size_t size = 0;
    mach_port_t object = MACH_PORT_NULL;
    vm_region_basic_info_data_64_t basic; mach_msg_type_number_t bcount = VM_REGION_BASIC_INFO_COUNT_64;
    if (mach_vm_region(mach_task_self(), &at, &size, VM_REGION_BASIC_INFO_64, (vm_region_info_t)&basic, &bcount, &object) != KERN_SUCCESS)
        return -1;
    if (object != MACH_PORT_NULL) { mach_port_deallocate(mach_task_self(), object); object = MACH_PORT_NULL; }
    out->start = (uint64_t)at; out->size = (uint64_t)size;
    out->protection = basic.protection; out->share_mode = basic.shared ? NVMTL_NCO_SM_SHARED : 0; out->external_pager = 0;
    if ((basic.protection & VM_PROT_WRITE) || basic.shared) return 0;
    mach_vm_address_t start = at; mach_vm_size_t extent = 0;
    vm_region_extended_info_data_t info; mach_msg_type_number_t count = VM_REGION_EXTENDED_INFO_COUNT;
    if (mach_vm_region(mach_task_self(), &start, &extent, VM_REGION_EXTENDED_INFO, (vm_region_info_t)&info, &count, &object) != KERN_SUCCESS)
        return -1;
    if (object != MACH_PORT_NULL) mach_port_deallocate(mach_task_self(), object);
    if (start != at) return -1;   // the map changed between the two answers: no answer
    out->size = (uint64_t)extent;
    out->protection = info.protection; out->share_mode = info.share_mode; out->external_pager = info.external_pager;
    return 0;
}
// A no-copy range the GPU cannot import that WindowServer has mapped read only, privately: copied once, when the buffer
// is made (nvmtl_nocopy_once.h says which ranges and why only those). From pull request 524 of the original project,
// adapted. NVMTL_WS_NOCOPY_ONCE=0: copied before every submission and back after each part, as in 3.0.4. Read once.
static BOOL nvmtl_nocopy_once(const void *p, NSUInteger n)
{
    static int applies = -1;
    if (applies < 0) {
        const char *e = getenv("NVMTL_WS_NOCOPY_ONCE");
        applies = nvmtl_nco_applies(getprogname(), e);
        if (nvmtl_nco_applies(getprogname(), NULL))
            nvlog("newBufferWithBytesNoCopy: a range that cannot be imported and is mapped read only %s (NVMTL_WS_NOCOPY_ONCE=%s)",
                  applies ? "is copied once" : "is copied at every submission", e ? e : "unset");
    }
    return applies && nvmtl_nco_range_sealed((uint64_t)(uintptr_t)p, (uint64_t)n, nvmtl_nco_query_task, NULL);
}
@implementation NVMTLDevice (Contract)
- (NSUInteger)doubleFPConfig { return 0; }
- (NSUInteger)featureProfile { return 10002; }
- (NSUInteger)getTimestampFrequency { return 1000000000ULL; }
- (NSUInteger)halfFPConfig { return 0; }
- (BOOL)isDepth24Stencil8PixelFormatSupported { return YES; }
- (BOOL)metalAssertionsEnabled { return NO; }
- (NSUInteger)minimumTextureBufferAlignmentForPixelFormat:(NSUInteger)a0 { return 256; }
static void nvbt(const char *tag, int *count, int max);
- (id)newBufferWithBytesNoCopy:(void *)a0 length:(NSUInteger)a1 options:(NSUInteger)a2 deallocator:(id)a3
{
    const MTLStorageMode sm = (MTLStorageMode)((a2 >> MTLResourceStorageModeShift) & 0xF);
    if (!a1) { nvlog("newBufferWithBytesNoCopy: zero length -> nil (the deallocator is not called)"); return nil; }
    if (sm == MTLStorageModePrivate || sm == MTLStorageModeMemoryless) {
        nvlog("newBufferWithBytesNoCopy: storage mode %d -> nil (a real driver ABORTS: storageModePrivate incompatible with ...WithBytes variant of newBuffer)", (int)sm);
        return nil; }
    if (!a0) {
        NVMTLBuffer *z = (NVMTLBuffer *)[self newBufferWithLength:a1 options:a2];
        if (!z) { nvlog("newBufferWithBytesNoCopy: NULL pointer, %lu bytes: no buffer to stand in -> nil", (unsigned long)a1); return nil; }
        void *zc = [z contents];
        if (zc) { memset(zc, 0, a1); if (z->_shadow.map) [z didModifyRange:NSMakeRange(0, a1)]; }
        if (a3) { void (^user)(void *, NSUInteger) = [a3 copy]; const NSUInteger n = a1;
                  z->_hostDealloc = ^(void *p, NSUInteger l) { (void)p; (void)l; user(NULL, n); }; }
        { static int said; if (!said++) nvlog("newBufferWithBytesNoCopy: NULL pointer, %lu bytes -> a zero-filled buffer of our own; its deallocator gets (NULL, %lu)", (unsigned long)a1, (unsigned long)a1); }
        return z;
    }
    NVMTLBuffer *b = [NVMTLBuffer new];
    b->_storage = sm;
    b->_ropt = a2 & ((MTLResourceOptions)0xF | ((MTLResourceOptions)0x3 << MTLResourceHazardTrackingModeShift));
    b->_hostDealloc = a3 ? [a3 copy] : nil;
    // Managed on a discrete GPU means the GPU works on its own copy, synchronized by didModifyRange (CPU -> GPU) and
    // synchronizeResource (GPU -> CPU). Apple's OpenCL wraps every cl_mem this way; importing the pages zero-copy put
    // every kernel access across PCIe instead (a 5600x4200 byte-image rotate: 6.6 s per dispatch, Geekbench 6 OpenCL
    // Horizon Detection hit the 60 s GPU watchdog). The caller's pages become the managed shadow, so contents is still
    // the caller's pointer, and the GPU side lives in VRAM.
    // This used to apply only from 1 MiB up, on the theory that a small buffer's transfer outweighs the win. It does
    // not: the transfer is one DMA of the dirty range per submission, while a kernel that reads a small buffer reads it
    // once per thread. Geekbench 7 OpenCL keeps exactly its hottest data in small cl_mem objects - Particle Physics'
    // whole state (4 buffers, 0.1 MB, 1000 dispatches per chain), Video Filter's colour LUT (read per pixel), the
    // network weights and biases in Blur / Face / Super Resolution, the path tracer's ray counters (atomics) - and
    // every one of those accesses crossed PCIe: those workloads ran at 0.4-12% of the same card under OpenCL on
    // Windows, while the two whose buffers are all large (Horizon, Fluid) ran at 64-82%.
    // NVMTL_MANAGED_VRAM_MIN=<bytes> sets the floor (1048576 restores the old behaviour).
    static NSUInteger vramMin = NSUIntegerMax;
    if (vramMin == NSUIntegerMax) {
        const char *e = getenv("NVMTL_MANAGED_VRAM_MIN");
        vramMin = e && *e ? (NSUInteger)strtoull(e, NULL, 0) : 0;
        nvlog("newBufferWithBytesNoCopy: managed buffers from %lu bytes get a VRAM copy (NVMTL_MANAGED_VRAM_MIN=%s)",
              (unsigned long)vramMin, e ? e : "unset");
    }
    if (sm == MTLStorageModeManaged && a1 >= vramMin && nvmtl_vk_buffer_import_host(a0, a1, &b->_shadow) == 0) {
        if (nvmtl_vk_buffer_create(a1, 0, &b->_b) == 0) {
            [b didModifyRange:NSMakeRange(0, a1)];
            { static int said; if (!said++) nvlog("newBufferWithBytesNoCopy: managed %lu bytes -> VRAM copy, caller's pages are the shadow", (unsigned long)a1); }
            return b;
        }
        nvlog("newBufferWithBytesNoCopy: managed %lu bytes: VRAM refused - the GPU reads the caller's pages directly", (unsigned long)a1);
        b->_b = b->_shadow; memset(&b->_shadow, 0, sizeof b->_shadow);
        return b;
    }
    // NVMTL_MANAGED_OWNED_VRAM=1: a Managed range the driver cannot import (an unaligned pointer or an unpadded length)
    // used to fall to the copy below, where the GPU reads a system-memory copy over PCIe. It now gets a VRAM working
    // copy like an importable one: owned staging memory carries the transfers and the caller's pages stay what contents
    // returns (nvmtl_shared_wc.h finds the changed pages at each submit).
    if (sm == MTLStorageModeManaged && a1 >= vramMin) {
        static int owned = -1;
        if (owned < 0) { const char *e = getenv("NVMTL_MANAGED_OWNED_VRAM"); owned = e && e[0] == '1';
                         nvlog("newBufferWithBytesNoCopy: unimportable managed ranges %s (NVMTL_MANAGED_OWNED_VRAM=%s)", owned ? "get a VRAM copy with owned staging" : "use the system-memory copy", e ? e : "unset"); }
        if (owned) {
            nvmtl_swc *s = (nvmtl_swc *)calloc(1, sizeof *s);
            memset(&b->_shadow, 0, sizeof b->_shadow);
            if (s && nvmtl_swc_init(s, a1) == 0 && nvmtl_vk_buffer_create(a1, 1, &b->_shadow) == 0 && b->_shadow.map &&
                nvmtl_vk_buffer_create(a1, 0, &b->_b) == 0 && !b->_b.map) {
                b->_hostPtr = a0; b->_hostLen = (size_t)a1; b->_swc = s;
                { static int said; if (!said++) nvlog("newBufferWithBytesNoCopy: managed %lu bytes at %p cannot be imported -> VRAM copy with owned staging", (unsigned long)a1, a0); }
                return b;
            }
            if (s) { nvmtl_swc_free(s); free(s); }
            if (b->_shadow.buf) nvmtl_vk_buffer_destroy(&b->_shadow);
            memset(&b->_shadow, 0, sizeof b->_shadow);
            if (b->_b.buf) nvmtl_vk_buffer_destroy(&b->_b);
            memset(&b->_b, 0, sizeof b->_b);
            nvlog("newBufferWithBytesNoCopy: managed %lu bytes: VRAM copy refused - the system-memory copy", (unsigned long)a1);
        }
    }
    if (nvmtl_vk_buffer_import_host(a0, a1, &b->_b) == 0) {
        static int said; if (!said++) nvlog("newBufferWithBytesNoCopy: IMPORTED the caller's pages (%lu bytes at %p) \u2014 zero copy", (unsigned long)a1, a0);
        return b;
    }
    if (nvmtl_vk_buffer_create(a1, 1, &b->_b) || !b->_b.map) {
        nvmtl_alloc_failed("newBufferWithBytesNoCopy");
        nvlog("newBufferWithBytesNoCopy: could neither import nor allocate %lu bytes -> nil", (unsigned long)a1);
        return nil;
    }
    memcpy(b->_b.map, a0, a1);
    if (nvmtl_nocopy_once(a0, a1)) {
        // no host pointer: nothing copies to or from the caller's pages again. contents still returns them, and the
        // deallocator still gets the caller's pointer and length rather than the copy's.
        b->_callerPages = a0;
        if (b->_hostDealloc) {
            void (^user)(void *, NSUInteger) = b->_hostDealloc; void *const caller = a0; const NSUInteger n = a1;
            b->_hostDealloc = ^(void *p, NSUInteger l) { (void)p; (void)l; user(caller, n); };
        }
        { static int said; if (!said++) nvlog("newBufferWithBytesNoCopy: COPIED ONCE %lu bytes at %p (import alignment %zu): mapped read only and private, never copied again",
                                              (unsigned long)a1, a0, nvmtl_vk_host_import_align()); }
        return b;
    }
    b->_hostPtr = a0; b->_hostLen = (size_t)a1;
    { static int said; if (!said++) nvlog("newBufferWithBytesNoCopy: COPIED %lu bytes at %p (import alignment %zu) \u2014 resynced at every submission",
                                          (unsigned long)a1, a0, nvmtl_vk_host_import_align()); }
    return b;
}
- (id)newBufferWithDescriptor:(id)a0 { nvlog("newBufferWithDescriptor: -> nil (not built yet)"); return nil; }
- (id)newBufferWithIOSurface:(IOSurfaceRef)a0 { nvlog("newBufferWithIOSurface: -> nil (not built yet)"); return nil; }
- (id)newIndirectCommandBufferWithDescriptor:(id)a0 maxCount:(NSUInteger)a1 options:(NSUInteger)a2 { nvlog("newIndirectCommandBufferWithDescriptor:maxCount:options: -> nil (not built yet)"); return nil; }
- (id)newIndirectComputeCommandEncoderWithBuffer:(id)a0 { nvlog("newIndirectComputeCommandEncoderWithBuffer: -> nil (not built yet)"); return nil; }
- (id)newIndirectRenderCommandEncoderWithBuffer:(id)a0 { nvlog("newIndirectRenderCommandEncoderWithBuffer: -> nil (not built yet)"); return nil; }
- (id)newMotionEstimationPipelineWithDescriptor:(id)a0 { nvlog("newMotionEstimationPipelineWithDescriptor: -> nil (not built yet)"); return nil; }
- (id)newVisibleFunctionTableWithDescriptor:(id)a0 { nvlog("newVisibleFunctionTableWithDescriptor: -> nil (not built yet)"); return nil; }
- (void)receive:(id)a0 { nvlog("receive: (ignored)"); }
- (void)reserveResourceIndicesForResourceType:(NSUInteger)a0 indices:(NSUInteger *)a1 indexCount:(NSUInteger)a2 { nvlog("reserveResourceIndicesForResourceType:indices:indexCount: (ignored)"); }
- (NSUInteger)resourcePatchingTypeForResourceType:(NSUInteger)a0 { return 0; }
- (void)setMetalAssertionsEnabled:(BOOL)a0 { nvlog("setMetalAssertionsEnabled: (ignored)"); }
- (NSUInteger)singleFPConfig { return 0; }
// Sparse tile sizes are the device's own sparse block shapes (nvmtl_sparse_query.h); a format the device reports none for has size (0, 0, 0).
static MTLSize nvmtl_sparse_size(NSUInteger type, NSUInteger pixelFormat, NSUInteger samples) {
    uint32_t vk = 0, bpp = 0, g[3] = { 0, 0, 0 }; int a8 = 0;
    if (nvmtl_pixfmt((MTLPixelFormat)pixelFormat, &vk, &bpp, &a8) || a8 || nvmtl_vk_sparse_granularity(vk, type == MTLTextureType3D, (uint32_t)samples, g)) return MTLSizeMake(0, 0, 0);
    return MTLSizeMake(g[0], g[1], g[2]);
}
- (MTLSize)sparseTileSizeWithTextureType:(NSUInteger)a0 pixelFormat:(NSUInteger)a1 sampleCount:(NSUInteger)a2 { return nvmtl_sparse_size(a0, a1, a2); }
- (MTLSize)sparseTileSizeWithTextureType:(NSUInteger)a0 pixelFormat:(NSUInteger)a1 sampleCount:(NSUInteger)a2 sparsePageSize:(NSInteger)a3 { return nvmtl_sparse_size(a0, a1, a2); }
- (MTLSize)tileSizeWithTextureType:(NSUInteger)a0 pixelFormat:(NSUInteger)a1 sampleCount:(NSUInteger)a2 { return nvmtl_sparse_size(a0, a1, a2); }
- (NSUInteger)sparseTileSizeInBytes { uint32_t g[3]; return (NSUInteger)nvmtl_sparse_tile_bytes(nvmtl_vk_sparse_granularity(VK_FORMAT_R8G8B8A8_UNORM, 0, 1, g) == 0); }
- (NSUInteger)sparseTileSizeInBytesForSparsePageSize:(NSInteger)pageSize { return [self sparseTileSizeInBytes]; }
@end
