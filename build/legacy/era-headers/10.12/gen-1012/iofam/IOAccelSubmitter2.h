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
enum { kIOAccelSubmitter2Size = 0x528 };
class IOAccelSubmitter2 : public IOUserClient {
    OSDeclareDefaultStructors(IOAccelSubmitter2);
public:
    virtual void * getOwningTask() = 0;
    virtual void * getGPUTask() = 0;
    virtual unsigned int getOwningTaskPid() = 0;
    virtual void affectedByHardwareReset(bool, unsigned int) = 0;
    virtual void retireCommandBuffer(IOAccelBlockFence*) { }   // inline in Apple's header on this release (body read from the family)
    virtual bool shouldHardwareCommandNooped() = 0;
    virtual void _RESERVEDIOAccelSubmitter0();
    virtual void _RESERVEDIOAccelSubmitter1();
    virtual void _RESERVEDIOAccelSubmitter2();
    virtual void _RESERVEDIOAccelSubmitter3();
    virtual void _RESERVEDIOAccelSubmitter4();
    virtual void _RESERVEDIOAccelSubmitter5();
private:
    uint8_t _opaque[kIOAccelSubmitter2Size - sizeof(IOUserClient)];
};
static_assert(sizeof(IOAccelSubmitter2) == kIOAccelSubmitter2Size, "IOAccelSubmitter2 must be the size this release registers");

