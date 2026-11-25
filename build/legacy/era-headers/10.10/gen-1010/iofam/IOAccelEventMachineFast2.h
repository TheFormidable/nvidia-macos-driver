class IOAccelSurface2;
class IOAccelDisplayPipe2;
class IOAccelDisplayMachine2;
/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

#pragma once
#include "IOAccelEventMachine2.h"
enum { kIOAccelEventMachineFast2Size = 0xcc8 };
class IOAccelEventMachineFast2 : public IOAccelEventMachine2 {
    OSDeclareDefaultStructors(IOAccelEventMachineFast2);
public:
    virtual void * getClientTestEventFunc() APPLE_KEXT_OVERRIDE;
    virtual void getSharedMemorySize() APPLE_KEXT_OVERRIDE;
    virtual void setSharedMemory(IOAccelEventSharedPool*, unsigned long) APPLE_KEXT_OVERRIDE;
    virtual void initEvent(IOAccelEvent*) APPLE_KEXT_OVERRIDE;
    virtual void termEvent(IOAccelEvent*) APPLE_KEXT_OVERRIDE;
    virtual void cleanEvent(IOAccelEvent*) APPLE_KEXT_OVERRIDE;
    virtual void finishStamp(int) APPLE_KEXT_OVERRIDE;
    virtual void finishAllStamps() APPLE_KEXT_OVERRIDE;
    virtual bool testStamp(int) APPLE_KEXT_OVERRIDE;
    virtual bool testAllStamps() APPLE_KEXT_OVERRIDE;
    virtual bool testAllStampsUnlocked() APPLE_KEXT_OVERRIDE;
    virtual void finishEventUnlocked(IOAccelEvent*) APPLE_KEXT_OVERRIDE;
    virtual bool testEventUnlocked(IOAccelEvent*) APPLE_KEXT_OVERRIDE;
    virtual void finishEvent(IOAccelEvent*) APPLE_KEXT_OVERRIDE;
    virtual bool testEvent(IOAccelEvent*) APPLE_KEXT_OVERRIDE;
    virtual void finishEventExcluding(IOAccelEvent*, int) APPLE_KEXT_OVERRIDE;
    virtual bool testEventExcluding(IOAccelEvent*, int) APPLE_KEXT_OVERRIDE;
    virtual void moveEvent(IOAccelEvent*, IOAccelEvent*) APPLE_KEXT_OVERRIDE;
    virtual void copyEvent(IOAccelEvent*, IOAccelEvent*) APPLE_KEXT_OVERRIDE;
    virtual void mergeEvent(IOAccelEvent*, IOAccelEvent*) APPLE_KEXT_OVERRIDE;
    virtual void copyEventExcluding(IOAccelEvent*, IOAccelEvent*, int) APPLE_KEXT_OVERRIDE;
    virtual void mergeEventExcluding(IOAccelEvent*, IOAccelEvent*, int) APPLE_KEXT_OVERRIDE;
    virtual void setEventStamp(int, IOAccelEvent*) APPLE_KEXT_OVERRIDE;
    virtual unsigned int incrementStamp(int) APPLE_KEXT_OVERRIDE;
    virtual void writeStampCommand(int, IOAccelEventQueue*, vendevtCommandRec*) APPLE_KEXT_OVERRIDE;
    virtual void writeEventStampCommand(int, IOAccelEventQueue*, IOAccelEvent*, vendevtCommandRec*) APPLE_KEXT_OVERRIDE;
    virtual void writeEventBarrierCommand(IOAccelEventQueue*, IOAccelEvent*, vendevtBarrierRec*, int) APPLE_KEXT_OVERRIDE;
    virtual void logCommandSubmission(int) APPLE_KEXT_OVERRIDE;
    virtual bool stampIsIncremented() APPLE_KEXT_OVERRIDE;
    virtual unsigned int getStamp(int) APPLE_KEXT_OVERRIDE;
    virtual bool checkGPUProgress() APPLE_KEXT_OVERRIDE;
    virtual bool checkChannelProgress(int) APPLE_KEXT_OVERRIDE;
    virtual void * eventTimeout(int) APPLE_KEXT_OVERRIDE;
    virtual unsigned int enableEventCollection() APPLE_KEXT_OVERRIDE { return 0x1; }   // inline in Apple's header on this release (body read from the family)
    virtual void _pure83() = 0;
    virtual void _pure84() = 0;
    virtual void _pure85() = 0;
    virtual void _pure86() = 0;
    virtual int waitForAnyStamp(IOAccelEvent*, int*);
private:
    uint8_t _opaque[kIOAccelEventMachineFast2Size - sizeof(IOAccelEventMachine2)];
};
static_assert(sizeof(IOAccelEventMachineFast2) == kIOAccelEventMachineFast2Size, "IOAccelEventMachineFast2 must be the size this release registers");

