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
enum { kIOAccelResource2Size = 0x158 };
class IOAccelResource2 : public OSObject {
    OSDeclareDefaultStructors(IOAccelResource2);
public:
    virtual bool initSurfaceBuffer(IOAccelSurface2*, unsigned int, unsigned int, unsigned int, unsigned int, unsigned int);
    virtual bool allocMemory(IOAccelShared2*);
    virtual bool allocDataBufferMemory(IOAccelShared2*, bool, unsigned int);
    virtual bool allocFallbackMemory();
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
    virtual void getResourceInfo(IOAccelGetResourceInfoReturnData*, unsigned int*);
    virtual void pageon(IOAccelEvent*, bool) { }   // inline in Apple's header on this release (body read from the family)
    virtual void pageon(IOAccelEvent* a0, bool a1, unsigned long long a2) { typedef void (*F_)(void *, IOAccelEvent*, bool, unsigned long long); return ((F_)(*(void ***)this)[54])((void *)this, a0, a1, a2); }   // inline in Apple's header on this release (body read from the family)
    virtual void pageoff(IOAccelEvent*, bool, bool*) { }   // inline in Apple's header on this release (body read from the family)
    virtual void pageoff(IOAccelEvent* a0, bool a1, bool* a2, unsigned long long a3) { typedef void (*F_)(void *, IOAccelEvent*, bool, bool*, unsigned long long); return ((F_)(*(void ***)this)[56])((void *)this, a0, a1, a2, a3); }   // inline in Apple's header on this release (body read from the family)
    virtual void rebuildPagingBuffer() = 0;
    virtual uint64_t getMemoryAllocParameter() { return 0; }   // inline in Apple's header on this release (body read from the family)
    virtual uint64_t getLevelOffset(unsigned char, unsigned char, int*) = 0;
    virtual uint64_t getBackingLevelOffset(unsigned char, unsigned char, int*) = 0;
    virtual uint64_t calculateIOSurfaceDeviceCacheVRAMBytes(unsigned long long*, unsigned long long*) = 0;
    virtual bool addToAperture() = 0;
    virtual void removeFromAperture() = 0;
    virtual void * getApertureMemoryDescriptor(unsigned long long*) = 0;
    virtual void dirtyFaceLevels(unsigned int, unsigned int);
    virtual void dirtyBufferRange(unsigned int, unsigned int);
    virtual void zeroVidMemory(IOAccelEvent*, unsigned long long);
    virtual void scrubEvents();
    virtual void _RESERVEDIOAccelResource1();
    virtual void _RESERVEDIOAccelResource2();
    virtual void _RESERVEDIOAccelResource3();
    virtual void _RESERVEDIOAccelResource4();
    virtual void _RESERVEDIOAccelResource5();
    virtual void pageoffIfNeeded(unsigned int, unsigned int);
    virtual void pageonIfNeeded();
    virtual void gartEvent();
private:
    uint8_t _opaque[kIOAccelResource2Size - sizeof(OSObject)];
};
static_assert(sizeof(IOAccelResource2) == kIOAccelResource2Size, "IOAccelResource2 must be the size this release registers");

