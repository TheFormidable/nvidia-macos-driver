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
enum { kIOAccelVideoContext2Size = 0xfb8 };
class IOAccelVideoContext2 : public IOAccelContext2 {
    OSDeclareDefaultStructors(IOAccelVideoContext2);
public:
private:
    uint8_t _opaque[kIOAccelVideoContext2Size - sizeof(IOAccelContext2)];
};
static_assert(sizeof(IOAccelVideoContext2) == kIOAccelVideoContext2Size, "IOAccelVideoContext2 must be the size this release registers");

