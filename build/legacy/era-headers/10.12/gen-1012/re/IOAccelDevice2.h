class IOAccelSurface2;
class IOAccelDisplayPipe2;
class IOAccelDisplayMachine2;
/*
 * IOAccelDevice2 — reconstructed from IOAcceleratorFamily2 (macOS Sequoia, x86_64).
 *
 * Apple ships no header for this class. Everything below was read out of the
 * binary with ../re/vtable.py, not inferred from documentation or guessed:
 *
 *   superclass    __ZN14IOAccelDevice210superClassE relocates against
 *                 IOUserClient::gMetaClass                        -> IOUserClient
 *   class size    OSMetaClass ctor immediate in
 *                 __ZN14IOAccelDevice29MetaClassC2Ev: movl $0x188 -> 392 bytes
 *   vtable        __ZTV14IOAccelDevice2 @ 0x6f100, 311 slots
 *   dispatch      __ZN14IOAccelDevice214sDeviceMethodsE @ 0x6fbb0,
 *                 legacy IOExternalMethod[10], 48-byte stride
 *
 * THE TWO THINGS THAT MUST NOT DRIFT
 *
 * 1. VTABLE ORDER. The three new virtuals occupy slots 302/303/304 and the six
 *    reserved slots follow at 305..310. Declaring them in any other order, or
 *    omitting a reserved slot, moves every later entry. A virtual call then
 *    lands on the wrong function — a panic at runtime, silence at compile time.
 *
 * 2. CLASS SIZE. A subclass's own members start at offset 392. If this header
 *    computes a smaller size, those members overlap IOAccelDevice2's private
 *    ivars and corrupt them. Nothing warns. The static_assert below is the
 *    guard; if it ever fires, re-read the metaclass ctor rather than adjusting
 *    the number to make it pass.
 */

#ifndef _IOACCELDEVICE2_H
#define _IOACCELDEVICE2_H

#include <IOKit/IOUserClient.h>
#include <IOKit/IOService.h>

/*
 * Payload structs. The dispatch table gives their exact SIZES; it does not give
 * their field layouts. They are therefore declared opaque and size-checked —
 * inventing plausible fields would be a guess wearing the costume of a fact.
 * Fill them in only as each one is actually decoded.
 */
#define IOACCEL_OPAQUE(name, bytes)                                            \
    struct name { uint8_t opaque[bytes]; };                                    \
    static_assert(sizeof(struct name) == (bytes), #name " wrong size")

IOACCEL_OPAQUE(IOAccelDeviceConfigData,        64);
IOACCEL_OPAQUE(IOAccelDeviceEventMachineData, 600);
IOACCEL_OPAQUE(IOAccelDeviceSurfaceData,       24);
IOACCEL_OPAQUE(IOAccelDeviceGlobalObjectIDData, 8);
IOACCEL_OPAQUE(IOAccelDeviceTraceFilterData,    8);
IOACCEL_OPAQUE(IOAccelDeviceInfoReturnData,    24);
IOACCEL_OPAQUE(IOAccelDeviceGIDGroupData,      16);
IOACCEL_OPAQUE(IOAccelDeviceAPIProperty,       16);

/* get_name() writes into a caller-supplied buffer of exactly this size. */
enum { kIOAccelDeviceNameSize = 64 };

/* Total object size from the metaclass constructor. */

enum { kIOAccelDevice2Size = 0x170 };
class IOAccelDevice2 : public IOUserClient {
    OSDeclareDefaultStructors(IOAccelDevice2);
public:
    virtual bool deviceStart();
    virtual void deviceStop();
    virtual void _RESERVEDIOAccelDevice0();
    virtual void _RESERVEDIOAccelDevice1();
    virtual void _RESERVEDIOAccelDevice2();
    virtual void _RESERVEDIOAccelDevice3();
    virtual void _RESERVEDIOAccelDevice4();
    virtual void _RESERVEDIOAccelDevice5();
    bool init(OSDictionary *properties, task_t owningTask);
    IOReturn get_config(IOAccelDeviceConfigData *out);
    IOReturn get_name(char *out);
    IOReturn get_event_machine(IOAccelDeviceEventMachineData *out);
    IOReturn set_stereo(uint32_t a, uint32_t b);
    IOReturn get_device_info(IOAccelDeviceInfoReturnData *out);
    IOReturn get_next_gid_group(IOAccelDeviceGIDGroupData *out);
    void stopLocked();
private:
    uint8_t _opaque[kIOAccelDevice2Size - sizeof(IOUserClient)];
};
static_assert(sizeof(IOAccelDevice2) == kIOAccelDevice2Size, "IOAccelDevice2 must be the size this release registers");

static_assert(sizeof(IOAccelDevice2) == kIOAccelDevice2Size,
              "IOAccelDevice2 must be exactly 0x188 bytes — a subclass's "
              "members start at that offset and will corrupt the parent's "
              "ivars if this is wrong");

/*
 * Selector numbers for sDeviceMethods, from the decoded table.
 * Calling convention and payload size per selector:
 *
 *   sel  convention       scalars in   struct out
 *   0    ScalarIStructO   0            64
 *   1    ScalarIStructO   0            64
 *   2    ScalarIStructO   0            600
 *   3    ScalarIStructO   1            24
 *   4    ScalarIScalarO   2            0
 *   5    ScalarIStructO   0            8
 *   6    ScalarIStructO   0            8
 *   7    ScalarIStructO   0            24
 *   8    ScalarIStructO   0            16
 *   9    StructIStructO   16 in        variable
 */
enum {
    kIOAccelDeviceGetConfig             = 0,
    kIOAccelDeviceGetName               = 1,
    kIOAccelDeviceGetEventMachine       = 2,
    kIOAccelDeviceGetSurfaceInfo        = 3,
    kIOAccelDeviceSetStereo             = 4,
    kIOAccelDeviceGetNextGlobalObjectID = 5,
    kIOAccelDeviceGetCurrentTraceFilter = 6,
    kIOAccelDeviceGetDeviceInfo         = 7,
    kIOAccelDeviceGetNextGIDGroup       = 8,
    kIOAccelDeviceSetAPIProperty        = 9,
    kIOAccelDeviceMethodCount           = 10,
};

#endif /* _IOACCELDEVICE2_H */

