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
#include <IOKit/IOService.h>
enum { kIOAccelDisplayMachine2Size = 0x170 };
class IOAccelDisplayMachine2 : public IOService {
    OSDeclareDefaultStructors(IOAccelDisplayMachine2);
public:
    virtual bool init(IOGraphicsAccelerator2*);
    virtual bool start(IOPCIDevice*);
    virtual void stop();
    virtual bool displayModeWillChange() = 0;
    virtual bool displayModeDidChange() = 0;
    virtual bool initFramebufferResource(unsigned int, unsigned int, IOFramebuffer*, IOAccelResource2*);
    virtual void destroyFramebufferResource(unsigned int, unsigned int, IOFramebuffer*, IOAccelResource2*);
    virtual bool initScanoutResource(unsigned int, unsigned int, IOFramebuffer*, IOAccelResource2*);
    virtual void destroyScanoutResource(unsigned int, unsigned int, IOFramebuffer*, IOAccelResource2*);
    virtual void setStereo(unsigned int, IOAccelStereoMode);
    virtual void setupFullScreen(unsigned int, IOAccelResource2*, IOAccelResource2*);
    virtual void resetFullScreen(IOAccelEvent*, unsigned int, IOAccelResource2*, IOAccelResource2*);
    virtual void submitFlipBuffer(IOAccelEvent*, unsigned int, unsigned int, IOAccelResource2*, IOAccelResource2*);
    virtual void setupScanout(unsigned int, IOAccelResource2*, IOAccelResource2*);
    virtual void resetScanout(IOAccelEvent*, unsigned int, IOAccelResource2*, IOAccelResource2*);
    virtual void submitScanoutFlipBuffer(IOAccelEvent*, unsigned int, unsigned int, IOAccelResource2*, IOAccelResource2*);
    virtual bool isFramebufferHardwareMirrorOfFramebuffer(unsigned int, unsigned int);
    virtual void needsSecondaryFramebufferResources();
    virtual bool isCurrentFramebufferModeAcceleratorBacked(unsigned int);
    virtual void enableFlipInterrupt(unsigned int);
    virtual void disableFlipInterrupt(unsigned int);
    virtual void _RESERVEDIOAccelDisplayMachine0();
    virtual void _RESERVEDIOAccelDisplayMachine1();
    virtual void _RESERVEDIOAccelDisplayMachine2();
    virtual void _RESERVEDIOAccelDisplayMachine3();
    virtual void _RESERVEDIOAccelDisplayMachine4();
    virtual void _RESERVEDIOAccelDisplayMachine5();
    unsigned int getFramebufferCount();
    void framebuffer_will_power_off(unsigned int);   // a plain member on this release
    void framebuffer_did_power_on(unsigned int);   // a plain member on this release
    void display_mode_will_change(unsigned int);   // a plain member on this release
    void display_mode_did_change(unsigned int);   // a plain member on this release
    void found_framebuffer(IOFramebuffer *);   // a plain member on this release
private:
    uint8_t _opaque[kIOAccelDisplayMachine2Size - sizeof(IOService)];
};
static_assert(sizeof(IOAccelDisplayMachine2) == kIOAccelDisplayMachine2Size, "IOAccelDisplayMachine2 must be the size this release registers");

#define IOAccelDisplayMachine IOAccelDisplayMachine2
#define IOAccelLegacyDisplayMachine IOAccelDisplayMachine2
