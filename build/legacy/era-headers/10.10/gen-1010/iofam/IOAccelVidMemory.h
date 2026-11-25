class IOAccelSurface2;
class IOAccelDisplayPipe2;
class IOAccelDisplayMachine2;
/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

#pragma once
#include "IOAccelMemory.h"
#include "iofam_types.h"
enum { kIOAccelVidMemorySize = 0x80 };
class IOAccelVidMemory : public IOAccelMemory {
    OSDeclareDefaultStructors(IOAccelVidMemory);
public:
    virtual bool init(IOGraphicsAccelerator2*, IOAccelShared2*, IOAccelResource2*, unsigned long long, void*);
    virtual void _pure54() = 0;
    virtual void _pure55() = 0;
private:
    uint8_t _opaque[kIOAccelVidMemorySize - sizeof(IOAccelMemory)];
};
static_assert(sizeof(IOAccelVidMemory) == kIOAccelVidMemorySize, "IOAccelVidMemory must be the size this release registers");

