class IOAccelSurface2;
class IOAccelDisplayPipe2;
class IOAccelDisplayMachine2;
/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

#pragma once
#include <libkern/c++/OSObject.h>
#include "iofam_types.h"
enum { kIOAccelShared2Size = 0x78 };
class IOAccelShared2 : public OSObject {
    OSDeclareDefaultStructors(IOAccelShared2);
public:
    virtual bool init(IOGraphicsAccelerator2*, task*);
    virtual void describeDriverAllocations(IOAccelAllocationInfo*);
private:
    uint8_t _opaque[kIOAccelShared2Size - sizeof(OSObject)];
};
static_assert(sizeof(IOAccelShared2) == kIOAccelShared2Size, "IOAccelShared2 must be the size this release registers");

