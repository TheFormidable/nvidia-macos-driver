class IOAccelSurface2;
class IOAccelDisplayPipe2;
class IOAccelDisplayMachine2;
/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

#pragma once
#include "IOAccelContext2.h"
#include "iofam_types.h"
enum { kIOAccelGLContext2Size = 0x1118 };
class IOAccelGLContext2 : public IOAccelContext2 {
    OSDeclareDefaultStructors(IOAccelGLContext2);
public:
    virtual void addDrawableToChannel(IOAccelDrawable2*) = 0;
    virtual void removeDrawableFromChannel(IOAccelDrawable2*) = 0;
    virtual void setCompatibleSurfaceMode(unsigned long long*, unsigned long long, int) = 0;
    virtual void sleepForSwapCompleteNoLock(unsigned int);
    virtual void addVendorSurfaceRequiredBits(unsigned long long);
    virtual void get_temp_allocation_info(IOAccelDrawable2*, unsigned int*, unsigned int*) = 0;
    virtual void processSwap(eDoSwap);
private:
    uint8_t _opaque[kIOAccelGLContext2Size - sizeof(IOAccelContext2)];
};
static_assert(sizeof(IOAccelGLContext2) == kIOAccelGLContext2Size, "IOAccelGLContext2 must be the size this release registers");

