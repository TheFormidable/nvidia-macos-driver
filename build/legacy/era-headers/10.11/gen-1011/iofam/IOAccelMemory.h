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
enum { kIOAccelMemorySize = 0x60 };
class IOAccelMemory : public OSObject {
    OSDeclareDefaultStructors(IOAccelMemory);
public:
    virtual bool init(IOGraphicsAccelerator2*);
    virtual void * getLength() { return *(void * *)((unsigned char *)this + 0x40); }   // inline in Apple's header on this release (body read from the family)
    virtual void _pure37() = 0;
    virtual void _pure38() = 0;
    virtual OSObject * createMappingInTask(IOAccelTask*, unsigned int);
    virtual bool prepare();
    virtual bool complete();
    virtual void _pure42() = 0;
    virtual void _pure43() = 0;
    virtual void _pure44() = 0;
    virtual void _pure45() = 0;
    virtual void _pure46() = 0;
    virtual void _pure47() = 0;
    virtual void _pure48() = 0;
    virtual void orphanIt();
    virtual void unorphanIt(IOAccelShared2*);
    virtual void _pure51() = 0;
    virtual void _pure52() = 0;
private:
    uint8_t _opaque[kIOAccelMemorySize - sizeof(OSObject)];
};
static_assert(sizeof(IOAccelMemory) == kIOAccelMemorySize, "IOAccelMemory must be the size this release registers");

