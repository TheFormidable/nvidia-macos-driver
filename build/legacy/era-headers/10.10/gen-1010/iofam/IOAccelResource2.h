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
enum { kIOAccelResource2Size = 0xf8 };
class IOAccelResource2 : public OSObject {
    OSDeclareDefaultStructors(IOAccelResource2);
public:
    virtual bool initSurfaceBuffer(IOAccelSurface2*, unsigned int, unsigned int, unsigned int, unsigned int, unsigned int);
    virtual bool allocMemory(IOAccelShared2*);
    virtual bool allocDataBufferMemory(IOAccelShared2*, bool, unsigned int);
    virtual bool init(IOGraphicsAccelerator2*, IOAccelShared2*, unsigned int);
    virtual bool initialize(IOAccelNewResourceArgs*, unsigned long long);
    virtual bool initClientShared(IOAccelClientSharedRO*, IOAccelClientSharedRW*);
    virtual void termClientShared(IOAccelClientSharedRO*, IOAccelClientSharedRW*);
    virtual void copyResourcePrivate(IOAccelResourcePrivate*);
    virtual void sharedRelease(IOAccelShared2*);
    virtual void dirtyLevel(unsigned int, unsigned int);
    virtual bool prepare();
    virtual bool complete();
    virtual void load();
    virtual void unload();
    virtual void addToChannel(IOAccelChannel2*, unsigned int);
    virtual void removeFromChannel(IOAccelChannel2*);
    virtual void fallback();
    virtual void _pure52() = 0;
    virtual void _pure53() = 0;
    virtual void _pure54() = 0;
    virtual uint64_t getMemoryAllocParameter() { return 0; }   // inline in Apple's header on this release (body read from the family)
    virtual void _pure56() = 0;
    virtual void _pure57() = 0;
    virtual void _pure58() = 0;
    virtual void _pure59() = 0;
    virtual void _pure60() = 0;
    virtual void _pure61() = 0;
    virtual void pageoffIfNeeded(unsigned int, unsigned int);
    virtual void pageonIfNeeded();
    virtual void gartEvent();
private:
    uint8_t _opaque[kIOAccelResource2Size - sizeof(OSObject)];
};
static_assert(sizeof(IOAccelResource2) == kIOAccelResource2Size, "IOAccelResource2 must be the size this release registers");

