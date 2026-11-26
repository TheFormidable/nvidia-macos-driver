class IOAccelSurface2;
class IOAccelDisplayPipe2;
class IOAccelDisplayMachine2;
/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

#pragma once
#include <IOKit/IOService.h>
#include "iofam_types.h"
enum { kIOAccelDisplayPipe2Size = 0x428 };
class IOAccelDisplayPipe2 : public IOService {
    OSDeclareDefaultStructors(IOAccelDisplayPipe2);
public:
    virtual bool init(IOGraphicsAccelerator2*, IOAccelDisplayMachine2*, IOFramebuffer*, unsigned int);
    virtual IOAccelMemory * initFramebufferResource(unsigned int, IOAccelResource2*);
    virtual void destroyFramebufferResource(unsigned int, IOAccelResource2*);
    virtual bool initScanoutResource(unsigned int, IOAccelResource2*);
    virtual void destroyScanoutResource(unsigned int, IOAccelResource2*);
    virtual void setStereo(IOAccelStereoMode);
    virtual bool displayModeWillChange();
    virtual bool displayModeDidChange();
    virtual void needsSecondaryFramebufferResources();
    virtual bool isHardwareMirrorOfPipe(IOAccelDisplayPipe2*);
    virtual void submitScanoutFlipBuffer(IOAccelEvent*, unsigned int, IOAccelResource2*, IOAccelResource2*);
    virtual void submitFlipBuffer(IOAccelEvent*, unsigned int, IOAccelResource2*, IOAccelResource2*);
    virtual void setupFullScreen(IOAccelResource2*, IOAccelResource2*);
    virtual void resetFullScreen(IOAccelEvent*, IOAccelResource2*, IOAccelResource2*);
    virtual void setupScanout(IOAccelResource2*, IOAccelResource2*);
    virtual void resetScanout(IOAccelEvent*, IOAccelResource2*, IOAccelResource2*);
    virtual bool isCurrentModeAcceleratorBacked();
    virtual void enableVBLInterrupt();
    virtual void disableVBLInterrupt();
    virtual void enableTransactionInterrupt();
    virtual void disableTransactionInterrupt();
    virtual OSObject * newDisplayPipeTransaction();
    virtual IOReturn validateTransaction(IOAccelDisplayPipeTransaction2*);
    virtual IOReturn performTransaction(IOAccelDisplayPipeTransaction2*);
    virtual bool isTransactionComplete(IOAccelDisplayPipeTransaction2*);
    virtual void getDisplayModePipeScalerSetup(IOAccelDisplayPipeScaler*);
    virtual OSObject * createWorkLoop();
    virtual void copyCapabilities();
    void framebuffer_will_power_off();   // a plain member on this release
    void framebuffer_did_power_on();   // a plain member on this release
private:
    uint8_t _opaque[kIOAccelDisplayPipe2Size - sizeof(IOService)];
};
static_assert(sizeof(IOAccelDisplayPipe2) == kIOAccelDisplayPipe2Size, "IOAccelDisplayPipe2 must be the size this release registers");

#define IOAccelDisplayPipe IOAccelDisplayPipe2
#define IOAccelLegacyDisplayPipe IOAccelDisplayPipe2
