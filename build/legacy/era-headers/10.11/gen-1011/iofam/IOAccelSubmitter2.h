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
enum { kIOAccelSubmitter2Size = 0x4e8 };
class IOAccelSubmitter2 : public IOUserClient {
    OSDeclareDefaultStructors(IOAccelSubmitter2);
public:
    virtual void _pure300() = 0;
    virtual void _pure301() = 0;
    virtual void _pure302() = 0;
    virtual void _pure303() = 0;
    virtual void retireCommandBuffer(IOAccelBlockFence*) { }   // inline in Apple's header on this release (body read from the family)
    virtual void _pure305() = 0;
private:
    uint8_t _opaque[kIOAccelSubmitter2Size - sizeof(IOUserClient)];
};
static_assert(sizeof(IOAccelSubmitter2) == kIOAccelSubmitter2Size, "IOAccelSubmitter2 must be the size this release registers");

