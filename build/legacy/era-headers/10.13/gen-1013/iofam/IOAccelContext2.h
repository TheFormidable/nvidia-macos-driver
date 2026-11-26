class IOAccelSurface2;
class IOAccelDisplayPipe2;
class IOAccelDisplayMachine2;
/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

#pragma once
#include "IOAccelSubmitter2.h"
#include "iofam_types.h"
enum { kIOAccelContext2Size = 0x1060 };
class IOAccelContext2 : public IOAccelSubmitter2 {
    OSDeclareDefaultStructors(IOAccelContext2);
public:
    virtual void * getOwningTask() APPLE_KEXT_OVERRIDE { return *(void * *)((unsigned char *)this + 0x538); }   // inline in Apple's header on this release (body read from the family)
    virtual void * getGPUTask() APPLE_KEXT_OVERRIDE { return *(void * *)((unsigned char *)this + 0x558); }   // inline in Apple's header on this release (body read from the family)
    virtual unsigned int getOwningTaskPid() APPLE_KEXT_OVERRIDE { return (unsigned int)*(unsigned int *)((unsigned char *)this + 0x540); }   // inline in Apple's header on this release (body read from the family)
    virtual void affectedByHardwareReset(bool, unsigned int) APPLE_KEXT_OVERRIDE;
    virtual bool shouldHardwareCommandNooped() APPLE_KEXT_OVERRIDE;
    virtual void contextStart();
    virtual void contextStop();
    virtual void getDataBufferLimits();
    virtual void context_finish();
    virtual bool initSidebandBufferHeader(IOAccelSidebandBufferHeader*, unsigned long long, unsigned long long);
    virtual void processSidebandBuffer(IOAccelCommandDescriptor*, bool);
    virtual void processSidebandToken(IOAccelCommandStreamInfo&);
    virtual void discardSidebandToken(IOAccelCommandStreamInfo&);
    virtual void postTokenSanityCheck(IOAccelCommandStreamInfo&) { }   // inline in Apple's header on this release (body read from the family)
    virtual void processDataBuffers(unsigned int);
    virtual void beginCommandStream(IOAccelCommandStreamInfo&);
    virtual void endCommandStream(IOAccelCommandStreamInfo&);
    virtual void bindResource(IOAccelCommandStreamInfo&, IOAccelResource2*, bool, IOAccelChannel2*, unsigned int, bool);
    virtual void unbindResource(IOAccelCommandStreamInfo&, IOAccelResource2*, IOAccelChannel2*);
    virtual void compactCurrentVidMemory();
    virtual bool prepareResources(IOAccelResource2**, int);
    virtual void compactCurrentMappings(IOAccelMemoryMap*);
    virtual void getDataBuffer(IOAccelContextGetDataBufferIn*, IOAccelContextGetDataBufferOut*, IOAccelResourcePrivate*, unsigned long long);
    virtual bool allocOneDataBuffer(bool, unsigned int);
    virtual void getDataBufferPrivate(IOAccelResource2*, IOAccelResourcePrivate*, unsigned long long) { }   // inline in Apple's header on this release (body read from the family)
    virtual unsigned int validateDataBuffer(unsigned long long, IOBufferMemoryDescriptor*, IOAccelResource2*) { return 0; }   // inline in Apple's header on this release (body read from the family)
    virtual void populateContextConfig(IOAccelContextConfig*);
    virtual void addDataBufferToChannel(IOAccelResource2*, unsigned int);
    virtual void removeDataBufferFromChannel(IOAccelResource2*, unsigned int);
    virtual void removeCurrentResourceFromChannel(IOAccelResource2*, unsigned int);
    virtual void invalidate() { }   // inline in Apple's header on this release (body read from the family)
    virtual unsigned int getFrameDelimiter() { return 0; }   // inline in Apple's header on this release (body read from the family)
    virtual bool canSubmitCommandBuffer();
    virtual void pauseSubmitCommandBuffer();
    virtual void getPriorityInfo();
    virtual void _RESERVEDIOAccelContext3();
    virtual void _RESERVEDIOAccelContext4();
    virtual void _RESERVEDIOAccelContext5();
    virtual void getSurfaceReqBits();
    virtual void requiresBackingStore();
    virtual void set_compatible_surface_mode(unsigned long long*, unsigned long long, unsigned int);
    virtual void removeSurface();
    virtual void allowsExclusiveMode();
    virtual void isTripleBuffered();
    virtual void contextModeBits();
    virtual void describeDriverAllocations(IOAccelAllocationInfo*);
private:
    uint8_t _opaque[kIOAccelContext2Size - sizeof(IOAccelSubmitter2)];
};
static_assert(sizeof(IOAccelContext2) == kIOAccelContext2Size, "IOAccelContext2 must be the size this release registers");

