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
enum { kIOAccelCLContext2Size = 0xfd0 };
class IOAccelCLContext2 : public IOAccelContext2 {
    OSDeclareDefaultStructors(IOAccelCLContext2);
public:
private:
    uint8_t _opaque[kIOAccelCLContext2Size - sizeof(IOAccelContext2)];
};
static_assert(sizeof(IOAccelCLContext2) == kIOAccelCLContext2Size, "IOAccelCLContext2 must be the size this release registers");

