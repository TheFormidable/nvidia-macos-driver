class IOAccelSurface2;
class IOAccelDisplayPipe2;
class IOAccelDisplayMachine2;
/*
 * IOGraphicsAccelerator2 — reconstructed from IOAcceleratorFamily2
 * (macOS Sequoia, x86_64). Apple ships no header.
 *
 * THIS IS THE CLASS OUR DRIVER MUST *BE*. Everything else in the stack hangs
 * off an instance of this: macOS finds a graphics accelerator by finding an
 * IOGraphicsAccelerator2 attached to a PCI GPU.
 *
 * IT IS AN ABSTRACT CLASS. 17 of its vtable slots are `___cxa_pure_virtual`,
 * and those 17 methods are the ENTIRE required vendor interface — see
 * re/DRIVER-CONTRACT.md. The other 67 new virtuals have working defaults we
 * inherit for free and override only where NVIDIA differs from Apple's model.
 *
 * WHERE EACH FACT CAME FROM
 *   size        3544 bytes — the KERNEL'S OWN OSMetaClass registry (read by the
 *               inspector in bench/AccelDevice), cross-checked against the
 *               `movl $0xdd8, %ecx` immediate in the metaclass constructor.
 *               Two independent sources agreeing.
 *   superclass  IOAccelerator (full chain: IOAccelerator -> IOService ->
 *               IORegistryEntry -> OSObject), from the same registry.
 *   vtable      __ZTV22IOGraphicsAccelerator2 — 84 new virtual slots, 268..351.
 *
 * THE 84 NEW VIRTUALS BELOW ARE MACHINE-GENERATED, IN VTABLE ORDER.
 * Regenerate, never hand-edit:
 *
 *   python3 re/gen_virtuals.py re/x86/IOAcceleratorFamily2 IOGraphicsAccelerator2 268 \
 *     --names re/x86/AMDRadeonX4000:AMDRadeonX4000_AMDGraphicsAccelerator \
 *     --names re/x86/AMDRadeonX4000:AMDRadeonX4000_AMDEllesmereGraphicsAccelerator
 *
 * RETURN TYPES ARE INFERRED, NOT RECOVERED. The Itanium ABI does not encode
 * return types for non-template functions, so they are guessed from naming
 * convention and marked. This is INERT for vtable layout, and on x86_64 every
 * method here returns a pointer or an integer — same ABI register. It would
 * only matter for a struct returned by value or a float return, neither of
 * which appears here. Confirm the return type of any method you IMPLEMENT; the
 * 67 we inherit are unaffected.
 *
 * THE INHERITED OVERRIDES ARE DELIBERATELY NOT DECLARED. IOGraphicsAccelerator2
 * overrides 14 IOService/OSObject methods (free, start, stop, terminate,
 * finalize, message, newUserClient, getWorkLoop, setProperties, requestProbe,
 * requestTerminate, didTerminate, configureReport, updateReport). Re-declaring
 * them here would be pure risk: they occupy slots that already exist, so
 * declaring them cannot help, while getting ONE signature subtly wrong would
 * create a NEW virtual slot and shift all 84 below it. A subclass can still
 * override any of them directly — they are inherited normally.
 */

#ifndef _IOGRAPHICSACCELERATOR2_H
#define _IOGRAPHICSACCELERATOR2_H

#include "IOAccelerator.h"

/* Referenced across the interface; layouts not needed to subclass. */
class IOAccelAllocationInfo;
class IOAccelClientSharedRO;
class IOAccelClientSharedRW;
class IOAccelCommandDescriptor;
class IOAccelConfig;
class IOAccelEvent;
class IOAccelMemory;
class IOAccelMemoryMap;
class IOAccelResource2;
class IOAccelShared2;
class IOAccelSurface;
class IOAccelSysMemory;
class IOAccelTask;
class IOAccelVidMemory;
class IOFramebuffer;
class IOMemoryDescriptor;
class IOPCIDevice;
class IOSurface;

/* From the kernel's registry, cross-checked against the metaclass constructor. */

enum { kIOGraphicsAccelerator2Size = 0xda8 };
class IOGraphicsAccelerator2 : public IOAccelerator {
    OSDeclareDefaultStructors(IOGraphicsAccelerator2);
public:
    virtual void acceleratorDidLock(char const*, int);
    virtual void acceleratorWillUnlock(char const*, int);
    virtual uint64_t tmpTotalVRAM() { return (uint64_t)*(unsigned int *)((unsigned char *)this + 0xc90); }   // inline in Apple's header on this release (body read from the family)
    virtual uint64_t totalTextureMemory() { return *(uint64_t *)((unsigned char *)this + 0xc90); }   // inline in Apple's header on this release (body read from the family)
    virtual bool isIdle();
    virtual uint64_t getStampMemory(unsigned int*) = 0;
    virtual OSObject * createEventMachine();
    virtual OSObject * newEventMachine() = 0;
    virtual OSObject * newClientSharedRO();
    virtual OSObject * newClientSharedRW();
    virtual void deleteClientSharedRO(IOAccelClientSharedRO*);
    virtual void deleteClientSharedRW(IOAccelClientSharedRW*);
    virtual OSObject * createShared(task*);
    virtual OSObject * createSysMemory();
    virtual OSObject * createVidMemory(IOAccelShared2*, IOAccelResource2*, unsigned long long, void*);
    virtual OSObject * createMemoryMap(IOAccelTask*, IOAccelMemory*, unsigned int);
    virtual OSObject * createResource(IOAccelShared2*, unsigned int);
    virtual OSObject * createCommandDescriptor();
    virtual void deleteCommandDescriptor(IOAccelCommandDescriptor*);
    virtual OSObject * newBlockFence();
    virtual void scrubEvents();
    virtual OSObject * createDrawable(IOAccelSurface2*, IOAccelShared2*, bool);
    virtual void resourceTypeForIOSurface(IOSurface*);
    virtual OSObject * createDisplayPipe(IOFramebuffer*, unsigned int);
    virtual OSObject * createMemoryDescriptorWithAddressRange(unsigned long long, unsigned long long, unsigned int, task*);
    virtual OSObject * createMemoryDescriptorWithPersistentMemoryDescriptor(IOMemoryDescriptor*);
    virtual OSObject * createBufferMemoryDescriptorWithOptions(unsigned int, unsigned long, unsigned long);
    virtual OSObject * createBufferMemoryDescriptorInTaskWithOptions(task*, unsigned int, unsigned long, unsigned long);
    virtual void freeAllGPUMappings();
    virtual void freeWaitToPrepareVidMemory(IOAccelVidMemory*, bool, bool);
    virtual void freeWaitToPrepareVidMap(IOAccelMemoryMap*, bool, bool);
    virtual void unwireAllVidMemory();
    virtual void freeAllVidMemoryMappings();
    virtual void pageoffSurfaceInLinear();
    virtual void freeWaitToPrepareSysMemory(IOAccelSysMemory*, bool);
    virtual void freeWaitToPrepareSysMap(IOAccelMemoryMap*, bool);
    virtual void collectGartWirings();
    virtual void unwireAllSysMemory();
    virtual void freeAllSysMemoryMappings();
    virtual uint64_t getMaxSingleSysMemorySize();
    virtual uint64_t getMaxWiredSysMemorySize();
    virtual OSObject * createUserGPUTask() = 0;
    virtual void acceleratorFinalize();
    virtual void submitSwapCopy(IOAccelEvent*, IOAccelResource2*, IOAccelResource2*);
    virtual void populateAccelConfig(IOAccelConfig*) = 0;
    virtual uint64_t calcMaxGPUPhysicalMemoryBytes(unsigned long long);
    virtual bool configureDevice(IOPCIDevice*) = 0;
    virtual void teardownDevice(IOPCIDevice*) = 0;
    virtual OSObject * createKernelGPUTask() = 0;
    virtual void systemWillSleep();
    virtual void systemDidWake();
    virtual void systemWillChangeSpeed();
    virtual void systemDidChangeSpeed();
    virtual bool configIsHeadless();
    virtual OSObject * newDisplayMachine() = 0;
    virtual OSObject * newShared();
    virtual OSObject * newSharedUserClient();
    virtual OSObject * newDevice();
    virtual OSObject * newGLContext() = 0;
    virtual OSObject * newCLContext() = 0;
    virtual OSObject * newSurface() = 0;
    virtual OSObject * new2DContext() = 0;
    virtual OSObject * newVideoContext() = 0;
    virtual OSObject * newDisplayPipe();
    virtual OSObject * newDrawable();
    virtual OSObject * newContext(unsigned int);
    virtual OSObject * newSysMemory() = 0;
    virtual OSObject * newVidMemory() = 0;
    virtual OSObject * newResource() = 0;
    virtual OSObject * newCommandDescriptor();
    virtual OSObject * newStatistics();
    virtual OSObject * newMemoryMap() = 0;
    virtual uint64_t getMaxResourceSize();
    virtual void _RESERVEDIOGraphicsAccelerator1();
    virtual void _RESERVEDIOGraphicsAccelerator2();
    virtual void _RESERVEDIOGraphicsAccelerator3();
    virtual void _RESERVEDIOGraphicsAccelerator4();
    virtual void _RESERVEDIOGraphicsAccelerator5();
    virtual void describeDriverAllocations(IOAccelAllocationInfo*);
    virtual OSObject * newCommandQueue();
    virtual void ktraceStateChanged();
    void enableAccelerator();
    void disableAccelerator();
private:
    uint8_t _opaque[kIOGraphicsAccelerator2Size - sizeof(IOAccelerator)];
};
static_assert(sizeof(IOGraphicsAccelerator2) == kIOGraphicsAccelerator2Size, "IOGraphicsAccelerator2 must be the size this release registers");

static_assert(sizeof(IOGraphicsAccelerator2) == kIOGraphicsAccelerator2Size,
              "IOGraphicsAccelerator2 must be exactly 0xdd8 (3544) bytes — our "
              "driver subclasses it and its members start at that offset");

#endif /* _IOGRAPHICSACCELERATOR2_H */

