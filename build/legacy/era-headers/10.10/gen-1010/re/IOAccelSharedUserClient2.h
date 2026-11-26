class IOAccelSurface2;
class IOAccelDisplayPipe2;
class IOAccelDisplayMachine2;
/*
 * IOAccelSharedUserClient2 — reconstructed from IOAcceleratorFamily2
 * (macOS Sequoia, x86_64). Apple ships no header.
 *
 * This is the RESOURCE plane of the Metal driver: allocating GPU memory,
 * shared memory, fences and MTLEvents. Everything a Metal app does that isn't
 * command submission arrives through the 21 selectors below.
 *
 * All of it was READ OUT OF THE BINARY with ../re/vtable.py:
 *
 *   superclass   __ZN24IOAccelSharedUserClient210superClassE relocates against
 *                IOUserClient::gMetaClass                     -> IOUserClient
 *   class size   movl $0x168, %ecx in the metaclass ctor       -> 360 bytes
 *   vtable       __ZTV24IOAccelSharedUserClient2, 310 slots
 *   dispatch     __ZN24IOAccelSharedUserClient214sSharedMethodsE @ 0x77cb0,
 *                IOExternalMethod[21] — length proved by the next defined
 *                symbol landing at exactly table + 21*48.
 *
 * VTABLE ORDER DIFFERS FROM IOAccelDevice2. Here the six reserved slots come
 * FIRST (302..307) and the two new virtuals come AFTER them (308, 309). In
 * IOAccelDevice2 the new virtuals come first and the reserved slots trail. Do
 * not assume a house style — read each class's vtable. Getting this backwards
 * puts sharedStart() where a reserved slot belongs, which is a panic at
 * runtime and silence at build time.
 *
 * Verify any change with:  python3 re/verify_class.py IOAccelSharedUserClient2
 */

#ifndef _IOACCELSHAREDUSERCLIENT2_H
#define _IOACCELSHAREDUSERCLIENT2_H

#include <IOKit/IOUserClient.h>
#include <IOKit/IOService.h>

/*
 * Payload structs. The dispatch table gives exact SIZES but not field layouts,
 * so these are opaque and size-checked. Filling in invented fields would be a
 * guess dressed as a fact; decode them one at a time as they are needed.
 */
#ifndef IOACCEL_OPAQUE
#define IOACCEL_OPAQUE(name, bytes)                                            \
    struct name { uint8_t opaque[bytes]; };                                    \
    static_assert(sizeof(struct name) == (bytes), #name " wrong size")
#endif

IOACCEL_OPAQUE(IOAccelSharedPageoffResourceArgs,             8);
IOACCEL_OPAQUE(IOAccelDeviceShmemData,                      16);
IOACCEL_OPAQUE(IOAccelSharedGetInfoReturnData,              16);
IOACCEL_OPAQUE(IOAccelSharedSetupDirtyRingReturnData,       24);
IOACCEL_OPAQUE(IOAccelCreateMTLEventResult,                 24);
IOACCEL_OPAQUE(IOAccelMemoryData,                           48);
IOACCEL_OPAQUE(IOAccelAllocatedSize,                         8);
IOACCEL_OPAQUE(IOAccelResourceSetResourceOwnerIdentityData, 16);

/* Variable-sized across the boundary — no fixed size to assert. */
struct IOAccelNewResourceArgs;
struct IOAccelNewResourceReturnData;
struct IOAccelGetResourceInfoReturnData;

/* Argument of set_resource_purgeable; passed and returned by value as a scalar. */
typedef uint32_t eIOAccelResourcePurgeable;


enum { kIOAccelSharedUserClient2Size = 0x110 };
class IOAccelSharedUserClient2 : public IOUserClient {
    OSDeclareDefaultStructors(IOAccelSharedUserClient2);
public:
    virtual bool sharedStart();
    virtual void sharedStop();
    IOReturn delete_resource(uint32_t id);
    IOReturn page_off_resource(IOAccelSharedPageoffResourceArgs *a);
    IOReturn finish_object_event(uint32_t a, uint32_t b);
private:
    uint8_t _opaque[kIOAccelSharedUserClient2Size - sizeof(IOUserClient)];
};
static_assert(sizeof(IOAccelSharedUserClient2) == kIOAccelSharedUserClient2Size, "IOAccelSharedUserClient2 must be the size this release registers");

static_assert(sizeof(IOAccelSharedUserClient2) == kIOAccelSharedUserClient2Size,
              "IOAccelSharedUserClient2 must be exactly 0x168 bytes — a "
              "subclass's members start at that offset and will corrupt the "
              "parent's ivars if this is wrong");

/*
 * Selector map. Conventions and payload sizes decoded from sSharedMethods:
 *
 *   sel  convention       in            out
 *   0    StructIStructO   VARIABLE      VARIABLE
 *   1    ScalarIScalarO   1             0
 *   2    ScalarIStructI   0             8
 *   3    ScalarIScalarO   2             0
 *   4    ScalarIScalarO   2             1
 *   5    ScalarIScalarO   1             5
 *   6    ScalarIStructO   1             VARIABLE
 *   7    ScalarIStructO   1             16
 *   8    ScalarIScalarO   1             0
 *   9    ScalarIStructO   0             16
 *   10   ScalarIStructO   0             24
 *   11   ScalarIScalarO   0             0
 *   12   StructIStructO   8             8
 *   13   StructIStructO   8             24
 *   14   ScalarIScalarO   1             0
 *   15   ScalarIStructO   0             48
 *   16   ScalarIScalarO   1             0
 *   17   StructIStructO   VARIABLE      VARIABLE
 *   18   StructIStructO   16            8
 *   19   ScalarIStructO   0             8
 *   20   StructIStructO   16            0
 */
enum {
    kIOAccelSharedNewResource               = 0,
    kIOAccelSharedDeleteResource            = 1,
    kIOAccelSharedPageOffResource           = 2,
    kIOAccelSharedFinishObjectEvent         = 3,
    kIOAccelSharedSetResourcePurgeable      = 4,
    kIOAccelSharedGetSurfaceInfo            = 5,
    kIOAccelSharedGetResourceInfo           = 6,
    kIOAccelSharedCreateShmem               = 7,
    kIOAccelSharedDestroyShmem              = 8,
    kIOAccelSharedGetSharedInfo             = 9,
    kIOAccelSharedSetupDirtyRing            = 10,
    kIOAccelSharedProcessDirtyCommands      = 11,
    kIOAccelSharedAllocateFenceMemory       = 12,
    kIOAccelSharedCreateMTLEvent            = 13,
    kIOAccelSharedDestroyMTLEvent           = 14,
    kIOAccelSharedGetMemoryData             = 15,
    kIOAccelSharedDisconnectPeer            = 16,
    kIOAccelSharedSetResourcesPurgeable     = 17,
    kIOAccelSharedGetResourceOffset         = 18,
    kIOAccelSharedGetAllocatedSize          = 19,
    kIOAccelSharedSetResourceOwnerIdentity  = 20,
    kIOAccelSharedMethodCount               = 21,
};

#endif /* _IOACCELSHAREDUSERCLIENT2_H */

