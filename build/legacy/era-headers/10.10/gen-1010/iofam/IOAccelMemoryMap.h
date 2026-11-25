class IOAccelSurface2;
class IOAccelDisplayPipe2;
class IOAccelDisplayMachine2;
/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

#pragma once
#include "iofam_types.h"
#include <libkern/c++/OSObject.h>
enum { kIOAccelMemoryMapSize = 0xc0 };
class IOAccelMemoryMap : public OSObject {
    OSDeclareDefaultStructors(IOAccelMemoryMap);
public:
    virtual bool init(IOGraphicsAccelerator2*, IOAccelTask*, IOAccelMemory*, unsigned int);
    virtual bool matchOptionBits(unsigned int) { return 0x1; }   // inline in Apple's header on this release (body read from the family)
    virtual unsigned long long getGPUVirtualAddress();
    virtual unsigned long long getGPUVirtualAddressForUserProcess();
    virtual bool prepare();
    virtual bool complete();
    virtual bool compatibleWith(IOAccelMemoryMap*);
    virtual bool allocGPUVirtualAddress();
    virtual void freeGPUVirtualAddress();
    virtual uint64_t getLength() const { void *o_ = *(void **)((unsigned char *)this + 0x18); typedef uint64_t (*F_)(void *); return ((F_)(*(void ***)o_)[36])(o_); }   // inline in Apple's header on this release (body read from the family)
    virtual void _pure45() = 0;
    virtual void _pure46() = 0;
    virtual void _pure47() = 0;
private:
    uint8_t _opaque[kIOAccelMemoryMapSize - sizeof(OSObject)];
};
static_assert(sizeof(IOAccelMemoryMap) == kIOAccelMemoryMapSize, "IOAccelMemoryMap must be the size this release registers");

