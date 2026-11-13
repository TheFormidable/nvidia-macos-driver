/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

/* nvaccel_layout.h - does the running macOS lay out the accelerator family's classes the way this build expects?
 *
 * NVAccel is an IOGraphicsAccelerator2 and its helpers subclass more classes of IOAcceleratorFamily2 (resources,
 * memory, surfaces, display pipe and machine, event machine, task, memory map, the four contexts). Apple ships no
 * header for any of them: they are reconstructed in re/ and iofam/. Each driver class puts its own fields right after
 * the base's and extends the base's vtable, so a base that changed size would put the driver's fields on Apple's, and
 * a base that gained or lost a virtual function would make the driver call the wrong function. The comparisons are
 * nvrm_layout.h's; this file names the classes and decides what NVAccel does with the results.
 *
 * Where each expected value comes from:
 *   size      sizeof() of the reconstructed header, which is held to a constant by the header's own static_assert
 *             (k<Class>Size). The constants were read from the running kernel's class registry on macOS 15
 *             (OSMetaClass::getClassSize(), the same call this file makes; re/IOGraphicsAccelerator2.h also names the
 *             metaclass constructor's immediate as a second source).
 *   vtable    the number of slots the compiler laid out for the class from the reconstructed header (NVL_BUILT). The
 *             slots are generated from Apple's binary in vtable order; the macOS 26 build depends on the same count and
 *             order (gen_tahoe_fwd.py checked them against the 26.7.1 kernel debug kit).
 * Where each running value comes from:
 *   size      OSMetaClass::getClassSize() of the class registered under the name: the sizeof() Apple's own build passed
 *             to the metaclass when IOAcceleratorFamily2 (IOGraphicsFamily for IOAccelerator) was loaded.
 *   vtable    the kernel's table itself (the symbol this kext already imports to build its own classes), read as "the
 *             entries up to the first null word", bounded by the expected count plus one.
 *
 * What refuses:
 *   A SIZE that differs REFUSES (unless nvlayout=0): the driver's fields and its raw offsets into Apple objects would
 *   land on Apple's fields. Sizes are well defined on both sides.
 *   A vtable that reads LONGER is REPORTED and refuses nothing. "Longer" means that the word after the last expected
 *   slot is not null. clang writes a null word behind every vtable it compiles for a kext, but these tables were
 *   placed by the tool that builds Apple's kernel collection, and nothing this kext can ask says what follows their
 *   last slot there; a refusal on that reading could remove acceleration from a macOS that is fine. A base that really
 *   gained functions is caught elsewhere anyway: this kext's classes add no virtual function of their own (the test
 *   holds that), so they would be shorter than their parent, and the collection builder rejects a kext for that
 *   ("Malformed vtable") before any of this runs.
 *   A vtable that reads SHORTER is also REPORTED and refuses nothing. The decision (argued here, and the reason this
 *   file differs from the design, which let a length difference refuse): a shorter reading cannot be told apart, on
 *   this machine, from a false alarm, so it must not take acceleration away on its own.
 *     - It would be a real problem if Apple's base truly lost a virtual function: every later slot of the driver's
 *       class is then one off and the driver calls the wrong function, and the kernel-collection builder does NOT
 *       catch it (it accepts a child longer than its parent). A class size does not catch it either: adding or
 *       removing a virtual function does not change sizeof. So nothing fires on a shorter reading.
 *     - But the very same shorter reading appears with nothing wrong when the expected count is one too high for the
 *       macOS in front of us: the family's headers are reconstructed by hand, the counts for macOS 26 were read from
 *       a single kernel debug kit, and a point release of 15 or 26 can lay a reserved slot out differently. A refusal
 *       would then remove acceleration from a Mac that is fine. Nobody here can read a running kernel collection to
 *       rule that out, so the false-alarm cost (no acceleration for every such user) decides: a length, short or
 *       long, is reported only, and only a class size refuses. This matches the framebuffer kext, where no length
 *       refuses, and the rule that a false refusal must not cost a working machine its acceleration.
 *   Both directions are published (property "nvaccel-layout-vtables", debug.nvaccel_layout_vtlong / _vtshort, a log
 *   line), so a first boot still shows whether the family's tables read the way this build expects.
 *   Pure slots left (a function the base leaves pure and NVAccel does not implement) are counted only.
 *
 * The evaluation whose verdict can refuse is handed the sizes, the IOService calibration and only the tables that read
 * the SAME length: a table that reads shorter or longer cannot reach it, so only a class size ever refuses. Its own
 * text therefore counts only the tables that read the same; the property "nvaccel-layout-vtables" and
 * debug.nvaccel_layout_vtlong / _vtshort show the ones that differ.
 *
 * Who asks: NVAccel::probe, after its device gate and before IOGraphicsAccelerator2::probe. On a refusal the probe
 * returns NULL and IOKit drops the instance, the path every accelerator start that returns false takes. Nothing of
 * Apple's part of the instance is read before the verdict; the constructor stores only zeros (see NVAccel).
 * Properties on the PCI device: "nvaccel-layout-vtables" (never a state word) and "nvaccel-layout" (the verdict text of
 * nvrm_layout.h), once per device. Opt-out: boot-arg nvlayout=0 (read in nvrm_layout.h, once per boot). */
#ifndef NVACCEL_LAYOUT_H
#define NVACCEL_LAYOUT_H

#include "../nvrm_layout.h"

/* ---- pure part: what the vtable lengths say, and which tables may reach the verdict. It needs only the pure part of
        nvrm_layout.h and calls nothing in the kernel, so the host test compiles and runs it
        (tools/test_layout_guard_accel.py). ---- */

struct NvaccelVtReport {
    unsigned calibrated;            /* 1: IOService read exactly its expected length, so the other tables were read */
    unsigned compared;              /* tables read (not counting IOService) */
    unsigned shorter, longer;       /* of those: ended before the expected count / went on after it */
    char text[768];                 /* the line: the property "nvaccel-layout-vtables" and the log */
};

/* "IOGraphicsAccelerator2 350/350": the virtual functions this macOS has / the ones the driver was compiled with.
   ">350" is "more than 350": the reading stops at the first entry past the expected end. */
static inline void nvaccelVtPair(NvLayoutOut *o, const NvLayoutVt *vt, int r, int endsAt)
{
    nvLayoutPutS(o, vt->name ? vt->name : "?");
    if (r == 2) { nvLayoutPutS(o, " not readable"); return; }
    nvLayoutPutS(o, r == 1 ? " >" : " ");
    nvLayoutPutN(o, (unsigned)(r == 1 ? vt->built : endsAt), 10);
    nvLayoutPutS(o, "/");
    nvLayoutPutN(o, (unsigned)vt->built, 10);
}

/* Read the lengths, write the line, and copy into mayRefuse (room for cap entries) the tables that may reach the
 * verdict: those that read the SAME length. A table that reads shorter or longer, or that cannot be read, is never
 * copied, so a vtable length never refuses -- only a class size does (see the header comment).
 * When IOService does not read exactly its expected length, no other table is read and nothing is copied.
 * Returns the number of entries copied. Each table is read once here; the evaluation reads the copied ones again
 * (constant data, so it sees what this saw).
 * The line starts with "same:", "differ:" or "not compared:". None of them is a state word of nvLayoutText(), so a
 * reader that takes this line for a verdict reads "not run" and carries on. */
static inline unsigned nvaccelVtReport(NvaccelVtReport *rep, const NvLayoutVt *ioService, const NvLayoutVt *vts,
                                       unsigned nVts, NvLayoutVt *mayRefuse, unsigned cap)
{
    NvLayoutOut o = { rep->text, (unsigned)sizeof rep->text, 0 };
    int endsAt = 0;
    unsigned n = 0;
    rep->calibrated = rep->compared = rep->shorter = rep->longer = 0;
    rep->text[0] = 0;
    if (!ioService || !vts || !nVts) { nvLayoutPutS(&o, "not compared: no table"); return 0; }
    const int cal = nvLayoutVtCmp(ioService->slots, ioService->built, &endsAt);
    if (cal != 0) {
        nvLayoutPutS(&o, "not compared: ");
        nvaccelVtPair(&o, ioService, cal, endsAt);
        nvLayoutPutS(&o, " (virtual functions: this macOS/the driver)");
        return 0;
    }
    rep->calibrated = 1;
    int r[64];
    int ends[64];
    const unsigned m = nVts < 64 ? nVts : 64;
    for (unsigned i = 0; i < m; i++) {
        r[i] = nvLayoutVtCmp(vts[i].slots, vts[i].built, &ends[i]);
        if (r[i] == 2) continue;
        rep->compared++;
        if (r[i] < 0) rep->shorter++;
        if (r[i] > 0) rep->longer++;
        if (r[i] == 0 && mayRefuse && n < cap) mayRefuse[n++] = vts[i];
    }
    nvLayoutPutS(&o, !rep->compared ? "not compared: " : (rep->shorter || rep->longer) ? "differ: " : "same: ");
    for (unsigned i = 0; i < m; i++) {
        if (i) nvLayoutPutS(&o, ", ");
        nvaccelVtPair(&o, &vts[i], r[i], ends[i]);
    }
    nvLayoutPutS(&o, " (virtual functions: this macOS/the driver)");
    if (rep->longer) nvLayoutPutS(&o, "; longer: reported only, not a reason to refuse");
    if (rep->shorter) nvLayoutPutS(&o, "; shorter: reported only, not a reason to refuse");
    return n;
}

/* ---- kernel part ---- */
#ifdef KERNEL
#include <IOKit/IOService.h>
#include <IOKit/IORangeAllocator.h>   // IOAccelTask.h names it without including it
#include <libkern/c++/OSString.h>
#include <libkern/OSAtomic.h>
#include <sys/sysctl.h>
#include "IOAccelerator.h"
#include "IOGraphicsAccelerator2.h"
#include "IOAccelEventMachineFast2.h"
#include "IOAccelLegacyDisplayMachine.h"
#include "IOAccelLegacyDisplayPipe.h"
#include "IOAccelTask.h"
#include "IOAccelMemoryMap.h"
#include "IOAccelResource2.h"
#include "IOAccelSysMemory.h"
#include "IOAccelVidMemory.h"
#include "IOAccelLegacySurface.h"
#include "IOAccelGLContext2.h"
#include "IOAccel2DContext2.h"
#include "IOAccelCLContext2.h"
#include "IOAccelVideoContext2.h"

extern "C" void __cxa_pure_virtual(void);

/* the base each row names is the base the header gives the class */
static_assert(__is_base_of(IOService, IOAccelerator), "IOAccelerator's base is IOService");
static_assert(__is_base_of(IOAccelerator, IOGraphicsAccelerator2), "IOGraphicsAccelerator2's base is IOAccelerator");
static_assert(__is_base_of(OSObject, IOAccelEventMachine2), "IOAccelEventMachine2's base is OSObject");
static_assert(__is_base_of(IOAccelEventMachine2, IOAccelEventMachineFast2), "IOAccelEventMachineFast2's base is IOAccelEventMachine2");
static_assert(__is_base_of(IOService, IOAccelDisplayMachine), "IOAccelDisplayMachine's base is IOService");
static_assert(__is_base_of(IOAccelDisplayMachine, IOAccelLegacyDisplayMachine), "IOAccelLegacyDisplayMachine's base is IOAccelDisplayMachine");
static_assert(__is_base_of(IOService, IOAccelDisplayPipe), "IOAccelDisplayPipe's base is IOService");
static_assert(__is_base_of(IOAccelDisplayPipe, IOAccelLegacyDisplayPipe), "IOAccelLegacyDisplayPipe's base is IOAccelDisplayPipe");
static_assert(__is_base_of(OSObject, IOAccelTask), "IOAccelTask's base is OSObject");
static_assert(__is_base_of(OSObject, IOAccelMemoryMap), "IOAccelMemoryMap's base is OSObject");
static_assert(__is_base_of(OSObject, IOAccelResource2), "IOAccelResource2's base is OSObject");
static_assert(__is_base_of(OSObject, IOAccelMemory), "IOAccelMemory's base is OSObject");
static_assert(__is_base_of(IOAccelMemory, IOAccelSysMemory), "IOAccelSysMemory's base is IOAccelMemory");
static_assert(__is_base_of(IOAccelMemory, IOAccelVidMemory), "IOAccelVidMemory's base is IOAccelMemory");
static_assert(__is_base_of(IOUserClient, IOAccelSurface), "IOAccelSurface's base is IOUserClient");
static_assert(__is_base_of(IOAccelSurface, IOAccelLegacySurface), "IOAccelLegacySurface's base is IOAccelSurface");
static_assert(__is_base_of(IOUserClient, IOAccelSubmitter2), "IOAccelSubmitter2's base is IOUserClient");
static_assert(__is_base_of(IOAccelSubmitter2, IOAccelContext2), "IOAccelContext2's base is IOAccelSubmitter2");
static_assert(__is_base_of(IOAccelContext2, IOAccelGLContext2), "IOAccelGLContext2's base is IOAccelContext2");
static_assert(__is_base_of(IOAccelContext2, IOAccel2DContext2), "IOAccel2DContext2's base is IOAccelContext2");
static_assert(__is_base_of(IOAccelContext2, IOAccelCLContext2), "IOAccelCLContext2's base is IOAccelContext2");
static_assert(__is_base_of(IOAccelContext2, IOAccelVideoContext2), "IOAccelVideoContext2's base is IOAccelContext2");

/* Vtables: only symbols this kext imports already as the bases of its own classes, so reading them adds no import.
   The macOS 15 build references five family tables; the macOS 26 build references eight more, through the forwarders
   of tahoe_fwd.h. IOService is the calibration (the base of the kext's carrier class). */
NVL_TAIL(IOService);
NVL_TAIL(IOGraphicsAccelerator2);
NVL_TAIL(IOAccelResource2);
NVL_TAIL(IOAccelVidMemory);
NVL_TAIL(IOAccelMemoryMap);
NVL_TAIL(IOAccelLegacyDisplayPipe);
NVL_VTABLE(IOService, "__ZTV9IOService");
NVL_VTABLE(IOGraphicsAccelerator2, "__ZTV22IOGraphicsAccelerator2");
NVL_VTABLE(IOAccelResource2, "__ZTV16IOAccelResource2");
NVL_VTABLE(IOAccelVidMemory, "__ZTV16IOAccelVidMemory");
NVL_VTABLE(IOAccelMemoryMap, "__ZTV16IOAccelMemoryMap");
NVL_VTABLE(IOAccelLegacyDisplayPipe, "__ZTV24IOAccelLegacyDisplayPipe");
#ifdef NM_TAHOE
NVL_TAIL(IOAccelSysMemory);
NVL_TAIL(IOAccel2DContext2);
NVL_TAIL(IOAccelCLContext2);
NVL_TAIL(IOAccelGLContext2);
NVL_TAIL(IOAccelLegacySurface);
NVL_TAIL(IOAccelVideoContext2);
NVL_TAIL(IOAccelEventMachineFast2);
NVL_TAIL(IOAccelLegacyDisplayMachine);
NVL_VTABLE(IOAccelSysMemory, "__ZTV16IOAccelSysMemory");
NVL_VTABLE(IOAccel2DContext2, "__ZTV17IOAccel2DContext2");
NVL_VTABLE(IOAccelCLContext2, "__ZTV17IOAccelCLContext2");
NVL_VTABLE(IOAccelGLContext2, "__ZTV17IOAccelGLContext2");
NVL_VTABLE(IOAccelLegacySurface, "__ZTV20IOAccelLegacySurface");
NVL_VTABLE(IOAccelVideoContext2, "__ZTV20IOAccelVideoContext2");
NVL_VTABLE(IOAccelEventMachineFast2, "__ZTV24IOAccelEventMachineFast2");
NVL_VTABLE(IOAccelLegacyDisplayMachine, "__ZTV27IOAccelLegacyDisplayMachine");
#endif

static NvLayoutOnce gNvaccelLayout;
/* what debug.nvaccel_layout* read: every caller writes the same values after the evaluation */
static int gNvaccelLayoutState, gNvaccelLayoutClasses, gNvaccelLayoutVtables, gNvaccelLayoutBad, gNvaccelLayoutVtLong,
           gNvaccelLayoutVtShort, gNvaccelLayoutPure;
SYSCTL_INT(_debug, OID_AUTO, nvaccel_layout, CTLFLAG_RD | CTLFLAG_LOCKED, &gNvaccelLayoutState, 0,
           "Apple class layout seen by NVAccel: 0 not run, 1 ok, 2 refused, 3 overridden (nvlayout=0), 4 unknown");
SYSCTL_INT(_debug, OID_AUTO, nvaccel_layout_classes, CTLFLAG_RD | CTLFLAG_LOCKED, &gNvaccelLayoutClasses, 0,
           "accelerator family classes NVAccel compared by size");
SYSCTL_INT(_debug, OID_AUTO, nvaccel_layout_vtables, CTLFLAG_RD | CTLFLAG_LOCKED, &gNvaccelLayoutVtables, 0,
           "accelerator family vtables NVAccel read by length (0: not compared on this kernel)");
SYSCTL_INT(_debug, OID_AUTO, nvaccel_layout_bad, CTLFLAG_RD | CTLFLAG_LOCKED, &gNvaccelLayoutBad, 0,
           "class sizes that differ from this build (the only difference that refuses; a vtable length never does)");
SYSCTL_INT(_debug, OID_AUTO, nvaccel_layout_vtlong, CTLFLAG_RD | CTLFLAG_LOCKED, &gNvaccelLayoutVtLong, 0,
           "vtables that read longer than this build; reported, never a reason to refuse");
SYSCTL_INT(_debug, OID_AUTO, nvaccel_layout_vtshort, CTLFLAG_RD | CTLFLAG_LOCKED, &gNvaccelLayoutVtShort, 0,
           "vtables that read shorter than this build; reported, never a reason to refuse");
SYSCTL_INT(_debug, OID_AUTO, nvaccel_layout_pure, CTLFLAG_RD | CTLFLAG_LOCKED, &gNvaccelLayoutPure, 0,
           "functions the accelerator base leaves pure that NVAccel does not implement (expected 0; counted only)");

/* Registered with the other debug.nvaccel* names, by an accelerator that started and stays (a kext with no instance
   may be unloaded, and a registered sysctl must not outlive its code). Once per boot; nothing unregisters them, like
   every other name of this kext. */
static void nvaccelLayoutSysctls(void)
{
    static volatile UInt32 once;
    if (!OSCompareAndSwap(0, 1, &once)) return;
    sysctl_register_oid(&sysctl__debug_nvaccel_layout);
    sysctl_register_oid(&sysctl__debug_nvaccel_layout_classes);
    sysctl_register_oid(&sysctl__debug_nvaccel_layout_vtables);
    sysctl_register_oid(&sysctl__debug_nvaccel_layout_bad);
    sysctl_register_oid(&sysctl__debug_nvaccel_layout_vtlong);
    sysctl_register_oid(&sysctl__debug_nvaccel_layout_vtshort);
    sysctl_register_oid(&sysctl__debug_nvaccel_layout_pure);
}

/* false: a class of the accelerator family has another size in this macOS than in this build, or one of its vtables
   reads shorter, and nvlayout=0 is absent: the caller's probe returns NULL. Thread context with no lock held (a probe).
   self: the instance being probed; its own vtable (its first word, which only the constructor wrote) is read for the
   pure-slot count, and only after the verdict allowed it. kext: "NVAccel" or the flavour's name, for the log. */
static bool nvaccelLayoutAllows(IOGraphicsAccelerator2 *self, IOService *provider, const char *kext)
{
    const char *const who = kext ? kext : "NVAccel";
    const NvLayoutRow rows[] = {
        NVL_ROW(IOAccelerator, IOService),
        NVL_ROW(IOGraphicsAccelerator2, IOAccelerator),
        NVL_ROW(IOAccelEventMachine2, OSObject),
        NVL_ROW(IOAccelEventMachineFast2, IOAccelEventMachine2),
        NVL_ROW(IOAccelDisplayMachine, IOService),
        NVL_ROW(IOAccelLegacyDisplayMachine, IOAccelDisplayMachine),
        NVL_ROW(IOAccelDisplayPipe, IOService),
        NVL_ROW(IOAccelLegacyDisplayPipe, IOAccelDisplayPipe),
        NVL_ROW(IOAccelTask, OSObject),
        NVL_ROW(IOAccelMemoryMap, OSObject),
        NVL_ROW(IOAccelResource2, OSObject),
        NVL_ROW(IOAccelMemory, OSObject),
        NVL_ROW(IOAccelSysMemory, IOAccelMemory),
        NVL_ROW(IOAccelVidMemory, IOAccelMemory),
        NVL_ROW(IOAccelSurface, IOUserClient),
        NVL_ROW(IOAccelLegacySurface, IOAccelSurface),
        NVL_ROW(IOAccelSubmitter2, IOUserClient),
        NVL_ROW(IOAccelContext2, IOAccelSubmitter2),
        NVL_ROW(IOAccelGLContext2, IOAccelContext2),
        NVL_ROW(IOAccel2DContext2, IOAccelContext2),
        NVL_ROW(IOAccelCLContext2, IOAccelContext2),
        NVL_ROW(IOAccelVideoContext2, IOAccelContext2),
    };
    const NvLayoutVt ioService = NVL_VT(IOService);
    const NvLayoutVt vts[] = {
        NVL_VT(IOGraphicsAccelerator2),      // first: the pure-slot count below reads its result
        NVL_VT(IOAccelResource2),
        NVL_VT(IOAccelVidMemory),
        NVL_VT(IOAccelMemoryMap),
        NVL_VT(IOAccelLegacyDisplayPipe),
#ifdef NM_TAHOE
        NVL_VT(IOAccelSysMemory),
        NVL_VT(IOAccel2DContext2),
        NVL_VT(IOAccelCLContext2),
        NVL_VT(IOAccelGLContext2),
        NVL_VT(IOAccelLegacySurface),
        NVL_VT(IOAccelVideoContext2),
        NVL_VT(IOAccelEventMachineFast2),
        NVL_VT(IOAccelLegacyDisplayMachine),
#endif
    };
    const unsigned nVts = sizeof vts / sizeof vts[0];
    // The lengths, read for the report; only the tables that read the same or shorter are handed on. Every caller
    // reads them into its own buffer (constant tables of a few hundred words), so no caller publishes a line that
    // another one is still writing.
    NvLayoutVt mayRefuse[sizeof vts / sizeof vts[0]];
    NvaccelVtReport vt;
    const unsigned nMay = nvaccelVtReport(&vt, &ioService, vts, nVts, mayRefuse, nVts);
    const NvLayoutState st = nvLayoutEvaluateOnce(&gNvaccelLayout, who, rows, sizeof rows / sizeof rows[0],
                                                  mayRefuse, nMay, ioService.slots, ioService.built);
    static volatile UInt32 vtLogged;
    if (OSCompareAndSwap(0, 1, &vtLogged)) IOLog("%s: Apple vtable lengths %s\n", who, vt.text);
    if (st == kNvLayoutNotRun) return true;      // the first evaluation never finished: carry on as before

    const NvLayoutTally *t = &gNvaccelLayout.run.tally;
    gNvaccelLayoutState = (int)st;
    gNvaccelLayoutClasses = (int)t->classes;
    gNvaccelLayoutVtables = (int)vt.compared;
    gNvaccelLayoutBad = (int)(t->sizeBad + t->vtBad);   // vtBad is 0: only same-length tables reach the verdict
    gNvaccelLayoutVtLong = (int)vt.longer;
    gNvaccelLayoutVtShort = (int)vt.shorter;
    // Pure slots: only when the verdict lets the instance live and IOGraphicsAccelerator2's table read exactly its
    // expected length, so both tables hold at least that many slots. Never called through, only compared.
    if (st != kNvLayoutRefused && self && vt.calibrated && nvLayoutVtCmp(vts[0].slots, vts[0].built, nullptr) == 0) {
        const void *const *own = *(const void *const *const *)self;
        gNvaccelLayoutPure = (int)nvLayoutPureLeft(own, vts[0].slots, vts[0].built, (const void *)&__cxa_pure_virtual);
    }
    if (provider && provider->metaCast("IOPCIDevice")) {
        OSObject *had = provider->copyProperty("nvaccel-layout");
        if (!had) {
            provider->setProperty("nvaccel-layout-vtables", vt.text);   // first, so whoever finds the verdict finds this too
            provider->setProperty("nvaccel-layout", gNvaccelLayout.run.text);
            // NVRM reads this property only for its own report: a refusal here removes acceleration, not the display.
            // Say which came first on this boot, so a report shows the order the two kexts really started in.
            const bool nvrmFirst = provider->getProperty("nvrm-claimed") != nullptr;
            IOLog("%s: published nvaccel-layout on %s %s\n", who, provider->getName(),
                  nvrmFirst ? "after NVRM started on it" : "before NVRM started on it");
        }
        OSSafeReleaseNULL(had);
    }
    if (st == kNvLayoutRefused)
        IOLog("%s: does not attach: an accelerator family class has another size than in this build "
              "(boot-arg nvlayout=0 attaches anyway); the display runs without acceleration\n", who);
    return st != kNvLayoutRefused;
}
#endif /* KERNEL */

#endif
