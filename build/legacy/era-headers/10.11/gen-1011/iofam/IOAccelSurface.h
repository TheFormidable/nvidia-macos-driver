class IOAccelSurface2;
class IOAccelDisplayPipe2;
class IOAccelDisplayMachine2;
/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

#pragma once
#include <IOKit/IOUserClient.h>
#include "iofam_types.h"
enum { kIOAccelSurface2Size = 0x1338 };
class IOAccelSurface2 : public IOUserClient {
    OSDeclareDefaultStructors(IOAccelSurface2);
public:
    virtual bool copyFromBuffer(int, int, int, int, unsigned int, unsigned int, IOAccelEvent*, IOAccelResource2*, IOAccelSysMemory*, unsigned int, unsigned long long, unsigned int) = 0;
    virtual bool copyToBuffer(int, int, int, int, unsigned int, unsigned int, IOAccelEvent*, IOAccelResource2*, IOAccelSysMemory*, unsigned int, unsigned long long, unsigned int) = 0;
    virtual void submitSwapCopy(IOAccelEvent*, IOAccelResource2*, IOAccelResource2*) = 0;
    virtual void submitSwapFlush(IOAccelEvent*, IOAccelResource2*, IOAccelResource2*, IOAccelSwapFlushRec const*, unsigned int, unsigned int, unsigned int, IOAccelBounds const*) = 0;
    virtual void didSubmitSwap(unsigned int, unsigned int);
    virtual bool isBackBufferReady(unsigned int);
    virtual void submitCopyForward(IOAccelEvent*, unsigned int, IOAccelResource2*, IOAccelResource2*, IOAccelBounds const*, unsigned int);
    virtual void submitUpdate(unsigned int, IOAccelBounds*, unsigned int);
    virtual void surfaceStart();
    virtual void surfaceStop();
    virtual void pickPresentType(unsigned int);
    virtual bool isSurfaceSizeSupported(short, short);
    virtual bool canTmpAlloc(unsigned int) = 0;
    virtual void shapeSurface(unsigned int, unsigned short, unsigned short) = 0;
    virtual uint32_t getDirtyBufferBitsFromPrivateModeBits(unsigned long long) = 0;
    void update_shape();   // a plain member on this release
    void prune_buffers();   // a plain member on this release
    void reset_req_bits();   // a plain member on this release
    void set_scaling(unsigned int, IOAccelSurfaceScaling*);   // a plain member on this release
    void add_context_to_list(IOAccelContext2*);   // a plain member on this release
    void remove_context_from_list(IOAccelContext2*);   // a plain member on this release
    void set_id_mode(unsigned int, unsigned int);   // a plain member on this release
    void set_shape(eIOAccelSurfaceShapeBits, unsigned int, IOAccelDeviceRegion*, unsigned long long);   // a plain member on this release
    void set_shape_backing_length_ext(eIOAccelSurfaceShapeBits, unsigned int, unsigned long long, unsigned int, unsigned long long, IOAccelDeviceRegion*, unsigned long long);   // a plain member on this release
    void surface_lock_options(eLockType, unsigned int, IOAccelSurfaceInformation*, unsigned long long);   // a plain member on this release
    void surface_unlock_options(eLockType, unsigned int);   // a plain member on this release
private:
    uint8_t _opaque[kIOAccelSurface2Size - sizeof(IOUserClient)];
};
static_assert(sizeof(IOAccelSurface2) == kIOAccelSurface2Size, "IOAccelSurface2 must be the size this release registers");

#define IOAccelSurface IOAccelSurface2
#define IOAccelLegacySurface IOAccelSurface2
