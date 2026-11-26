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
enum { kIOAccelSysMemorySize = 0xf8 };
class IOAccelSysMemory : public IOAccelMemory {
    OSDeclareDefaultStructors(IOAccelSysMemory);
public:
private:
    uint8_t _opaque[kIOAccelSysMemorySize - sizeof(IOAccelMemory)];
};
static_assert(sizeof(IOAccelSysMemory) == kIOAccelSysMemorySize, "IOAccelSysMemory must be the size this release registers");

