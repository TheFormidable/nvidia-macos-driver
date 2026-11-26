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
enum { kIOAccelGLContext2Size = 0x1100 };
class IOAccelGLContext2 : public IOAccelContext2 {
    OSDeclareDefaultStructors(IOAccelGLContext2);
public:
    virtual void _pure341() = 0;
    virtual void _pure342() = 0;
    virtual void _pure343() = 0;
    virtual void sleepForSwapCompleteNoLock(unsigned int);
    virtual void addVendorSurfaceRequiredBits(unsigned long long);
    virtual void _pure346() = 0;
    virtual void processSwap(eDoSwap);
private:
    uint8_t _opaque[kIOAccelGLContext2Size - sizeof(IOAccelContext2)];
};
static_assert(sizeof(IOAccelGLContext2) == kIOAccelGLContext2Size, "IOAccelGLContext2 must be the size this release registers");

