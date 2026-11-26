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
enum { kIOAccelSysMemorySize = 0x178 };
class IOAccelSysMemory : public IOAccelMemory {
    OSDeclareDefaultStructors(IOAccelSysMemory);
public:
    virtual uint64_t getDirtySize() const APPLE_KEXT_OVERRIDE;
    virtual uint64_t getResidentSize() const APPLE_KEXT_OVERRIDE;
    virtual uint64_t getPhysicalSegment(unsigned long long, unsigned long long*) APPLE_KEXT_OVERRIDE;
    virtual void setPurgeable(unsigned int, unsigned int*) APPLE_KEXT_OVERRIDE;
    virtual uint64_t setTag(unsigned int a0) APPLE_KEXT_OVERRIDE { void *o_ = *(void **)((unsigned char *)this + 0xc0); typedef uint64_t (*F_)(void *, unsigned int); return ((F_)(*(void ***)o_)[58])(o_, a0); }   // inline in Apple's header on this release (body read from the family)
    virtual uint64_t getTag() APPLE_KEXT_OVERRIDE { void *o_ = *(void **)((unsigned char *)this + 0xc0); typedef uint64_t (*F_)(void *); return ((F_)(*(void ***)o_)[59])(o_); }   // inline in Apple's header on this release (body read from the family)
    virtual bool matchForReuse(void*, unsigned long long) APPLE_KEXT_OVERRIDE;
    virtual void wire() APPLE_KEXT_OVERRIDE;
    virtual void unwire() APPLE_KEXT_OVERRIDE;
    virtual void increment_wire_count() APPLE_KEXT_OVERRIDE;
    virtual void decrement_wire_count() APPLE_KEXT_OVERRIDE;
    virtual void _RESERVEDIOAccelSysMemory0();
    virtual void _RESERVEDIOAccelSysMemory1();
    virtual void _RESERVEDIOAccelSysMemory2();
    virtual void _RESERVEDIOAccelSysMemory3();
    virtual void _RESERVEDIOAccelSysMemory4();
    virtual void _RESERVEDIOAccelSysMemory5();
private:
    uint8_t _opaque[kIOAccelSysMemorySize - sizeof(IOAccelMemory)];
};
static_assert(sizeof(IOAccelSysMemory) == kIOAccelSysMemorySize, "IOAccelSysMemory must be the size this release registers");

