/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

/* nvrmfb_layout.h - does the running macOS lay out the Apple classes NVRMFB subclasses the way this build expects?
 *
 * NVRMNVDAFramebuffer and NVRMBootFB are IOFramebuffer subclasses and NVRMBacklightHandler an IODisplayParameterHandler
 * subclass. Their own fields follow the base's fields and their vtables extend the base's, so a base that changed size
 * or gained or lost a virtual function would put this kext's fields on Apple's and call the wrong functions. The
 * comparisons are nvrm_layout.h's; this file names the classes and decides what NVRMFB does with the results.
 *
 * Where each expected value comes from:
 *   size      sizeof() of the class in the SDK headers this kext is compiled against (IOGraphicsFamily's public
 *             IOFramebuffer.h, IOGraphicsDevice.h, IODisplay.h, and IOService.h). Apple's own build of IOGraphicsFamily
 *             adds no data member and no virtual function to them (its private part has neither), and these classes keep
 *             their layout across releases with ExpansionData and reserved slots because third-party framebuffers
 *             subclass them: a size difference is a real difference.
 *   vtable    the number of slots the compiler laid out for the class (NVL_BUILT), against the kernel's own table, which
 *             this kext imports already.
 *
 * A size that differs REFUSES. A vtable length that differs is REPORTED and refuses nothing.
 *   Both sizes are well defined: sizeof() here, and OSMetaClass::getClassSize(), a kernel interface, for the class the
 *   running kernel registered.
 *   The length of a kernel vtable is not something the kernel tells anyone. It is read off the table as "the entries up
 *   to the first null word", which is the length only if a null word follows the last slot. clang writes one behind
 *   every vtable it compiles for a kext (-fapple-kext), and the same reading of IOService shows on each boot that xnu's
 *   own table is like that. But IOFramebuffer's and IODisplayParameterHandler's tables are IOGraphicsFamily's: compiled
 *   by Apple, placed by the tool that builds the kernel collection, and nothing this kext can ask says what follows
 *   their last slot in the collection it runs from. If that word is not null, the reading says "longer" on a macOS that
 *   is fine, and a refusal there is a Mac without a picture at boot. So no reading of a table may refuse.
 *   Refusing would also add little. No class of this kext adds a virtual function to its Apple base (the test holds
 *   that), so its tables are exactly as long as this build's idea of the bases'. If a base really grew, they are shorter
 *   than their parent's, and Apple's kernel collection builder (as published with dyld) rejects a kext for that when it
 *   links it into the collection it is loaded from ("Malformed vtable"): this code does not run on such a macOS. What
 *   remains is a base that lost a function, which the reserved slots of these classes exist to prevent.
 *   The lengths are read and published all the same (property "nvrmfb-layout-vtables", debug.nvrmfb_layout_vtables and
 *   debug.nvrmfb_layout_vtdiff, one log line), so that a report from a new macOS shows how its tables read.
 *
 * Who asks: NVRMFBClaim::probe first (it probes the PCI device before NVRM starts on it, so the verdict is on the device
 * when NVRM looks), then NVRMBootFB::probe and NVRMNVDAFramebuffer::start, which read the same evaluation. The verdict
 * text goes to the PCI device as "nvrmfb-layout"; NVRM reads it before it counts a start and declines a refused one.
 * Opt-out: boot-arg nvlayout=0 (read in nvrm_layout.h) turns a refusal into "overridden" and everything runs as before. */
#ifndef NVRMFB_LAYOUT_H
#define NVRMFB_LAYOUT_H

#include "nvrm_layout.h"

/* ---- pure part: what the vtable lengths say. It needs only the pure part of nvrm_layout.h and calls nothing in the
        kernel, so the host test compiles and runs it (tools/test_layout_guard_fb.py). ---- */

struct NvrmfbVtReport {
    unsigned compared, differ;      /* tables read / of those, the ones with another length than this build's */
    char text[200];                 /* the line: the property "nvrmfb-layout-vtables" and the log */
};

/* "IOFramebuffer 350/350": the virtual functions this macOS has / the ones the driver was compiled with. ">350" is
   "more than 350": the reading stops at the first entry past the expected end. */
static inline void nvrmfbVtPair(NvLayoutOut *o, const NvLayoutVt *vt, int r, int endsAt)
{
    nvLayoutPutS(o, vt->name ? vt->name : "?");
    if (r == 2) { nvLayoutPutS(o, " not readable"); return; }
    nvLayoutPutS(o, r == 1 ? " >" : " ");
    nvLayoutPutN(o, (unsigned)(r == 1 ? vt->built : endsAt), 10);
    nvLayoutPutS(o, "/");
    nvLayoutPutN(o, (unsigned)vt->built, 10);
}

/* Read the lengths and write the line. It has no result a caller could refuse on, on purpose (see the top).
 *   ioService   xnu's IOService table with this build's slot count: the self-check of the reading. When it does not
 *               come out equal, no other table is read and the line says "not compared: IOService ...".
 *   vts, nVts   the tables to report
 * The line starts with "same:", "differ:" or "not compared:". None of them is a state word of nvLayoutText(), so a
 * reader that takes this line for a verdict reads "not run" and carries on. */
static inline void nvrmfbVtReport(NvrmfbVtReport *rep, const NvLayoutVt *ioService, const NvLayoutVt *vts, unsigned nVts)
{
    NvLayoutOut o = { rep->text, (unsigned)sizeof rep->text, 0 };
    int endsAt = 0;
    rep->compared = rep->differ = 0;
    rep->text[0] = 0;
    if (!ioService || !vts || !nVts) { nvLayoutPutS(&o, "not compared: no table"); return; }
    const int cal = nvLayoutVtCmp(ioService->slots, ioService->built, &endsAt);
    if (cal != 0) {
        nvLayoutPutS(&o, "not compared: ");
        nvrmfbVtPair(&o, ioService, cal, endsAt);
        nvLayoutPutS(&o, " (virtual functions: this macOS/the driver)");
        return;
    }
    for (unsigned i = 0; i < nVts; i++) {
        const int r = nvLayoutVtCmp(vts[i].slots, vts[i].built, nullptr);
        if (r == 2) continue;
        rep->compared++;
        if (r != 0) rep->differ++;
    }
    nvLayoutPutS(&o, !rep->compared ? "not compared: " : rep->differ ? "differ: " : "same: ");
    for (unsigned i = 0; i < nVts; i++) {
        const int r = nvLayoutVtCmp(vts[i].slots, vts[i].built, &endsAt);
        if (i) nvLayoutPutS(&o, ", ");
        nvrmfbVtPair(&o, &vts[i], r, endsAt);
    }
    nvLayoutPutS(&o, " (virtual functions: this macOS/the driver)");
    if (rep->differ) nvLayoutPutS(&o, "; reported only, not a reason to refuse");
}

/* ---- kernel part ---- */
#ifdef KERNEL
#include <IOKit/IOService.h>
#include <IOKit/graphics/IOFramebuffer.h>
#include <IOKit/graphics/IOGraphicsDevice.h>
#include <IOKit/graphics/IODisplay.h>
#include <libkern/c++/OSString.h>
#include <libkern/OSAtomic.h>
#include <sys/sysctl.h>

/* the base each row names is the base the header gives the class */
static_assert(__is_base_of(IOGraphicsDevice, IOFramebuffer), "IOFramebuffer's base is IOGraphicsDevice");
static_assert(__is_base_of(IOService, IOGraphicsDevice), "IOGraphicsDevice's base is IOService");
static_assert(__is_base_of(IOService, IODisplayParameterHandler), "IODisplayParameterHandler's base is IOService");

NVL_TAIL(IOService);
NVL_TAIL(IOFramebuffer);
NVL_TAIL(IODisplayParameterHandler);
/* all three are imported by this kext already: IOService and IOFramebuffer as bases of its framebuffers,
   IODisplayParameterHandler as the base of NVRMBacklightHandler */
NVL_VTABLE(IOService, "__ZTV9IOService");
NVL_VTABLE(IOFramebuffer, "__ZTV13IOFramebuffer");
NVL_VTABLE(IODisplayParameterHandler, "__ZTV25IODisplayParameterHandler");

static NvLayoutOnce gNvrmfbLayout;
/* what debug.nvrmfb_layout* read: every caller writes the same values after the evaluation */
static int gNvrmfbLayoutState, gNvrmfbLayoutClasses, gNvrmfbLayoutVtables, gNvrmfbLayoutVtDiff;
SYSCTL_INT(_debug, OID_AUTO, nvrmfb_layout, CTLFLAG_RD | CTLFLAG_LOCKED, &gNvrmfbLayoutState, 0,
           "Apple class layout seen by NVRMFB: 0 not run, 1 ok, 2 refused, 3 overridden (nvlayout=0), 4 unknown");
SYSCTL_INT(_debug, OID_AUTO, nvrmfb_layout_classes, CTLFLAG_RD | CTLFLAG_LOCKED, &gNvrmfbLayoutClasses, 0,
           "Apple classes NVRMFB compared by size");
SYSCTL_INT(_debug, OID_AUTO, nvrmfb_layout_vtables, CTLFLAG_RD | CTLFLAG_LOCKED, &gNvrmfbLayoutVtables, 0,
           "Apple vtables NVRMFB compared by length (0: not compared on this kernel); reported, never a reason to refuse");
SYSCTL_INT(_debug, OID_AUTO, nvrmfb_layout_vtdiff, CTLFLAG_RD | CTLFLAG_LOCKED, &gNvrmfbLayoutVtDiff, 0,
           "Apple vtables whose length differs from this build; reported, never a reason to refuse");

/* Registered only by an instance that started (the claim, a framebuffer) after it made the kext hold a reference on
   itself (nvrmfbStayLoaded() in nvrm-fb.cpp), never by a kext that declined: a kext without that reference may be
   unloaded, and a registered sysctl must not outlive its code. */
static void nvrmfbLayoutSysctls(void)
{
    static volatile UInt32 once;
    if (!OSCompareAndSwap(0, 1, &once)) return;
    sysctl_register_oid(&sysctl__debug_nvrmfb_layout);
    sysctl_register_oid(&sysctl__debug_nvrmfb_layout_classes);
    sysctl_register_oid(&sysctl__debug_nvrmfb_layout_vtables);
    sysctl_register_oid(&sysctl__debug_nvrmfb_layout_vtdiff);
}

/* The PCI device under `from` (itself, or up to eight providers up): where NVRM and NVAccel look for the verdict. */
static IOService *nvrmfbLayoutPci(IOService *from)
{
    for (int hops = 0; from && hops < 8; hops++, from = from->getProvider())
        if (from->metaCast("IOPCIDevice")) return from;
    return nullptr;
}

/* false: IOFramebuffer, IOGraphicsDevice or IODisplayParameterHandler has another size in this macOS than in this
   build, and the caller must not construct or start a framebuffer. Thread context with no lock held (a probe or a
   start). */
static bool nvrmfbLayoutAllows(IOService *provider, const char *who)
{
    const NvLayoutRow rows[] = {
        NVL_ROW(IOFramebuffer, IOGraphicsDevice),
        NVL_ROW(IOGraphicsDevice, IOService),
        NVL_ROW(IODisplayParameterHandler, IOService),
    };
    // Sizes only: the evaluation whose verdict can refuse is handed no vtable and no calibration table, so nothing read
    // from a table can reach it. Its text therefore says "vtables not compared" on a macOS that matches.
    const NvLayoutState st = nvLayoutEvaluateOnce(&gNvrmfbLayout, "NVRMFB", rows, sizeof rows / sizeof rows[0],
                                                  nullptr, 0, nullptr, 0);
    if (st == kNvLayoutNotRun) return true;      // the first evaluation never finished: carry on as before

    // The vtable lengths, for the report alone. Every caller reads them again into its own buffer (three constant
    // tables of a few hundred words), so no caller publishes a line that another one is still writing.
    const NvLayoutVt ioService = NVL_VT(IOService);
    const NvLayoutVt vts[] = { NVL_VT(IOFramebuffer), NVL_VT(IODisplayParameterHandler) };
    NvrmfbVtReport vt;
    nvrmfbVtReport(&vt, &ioService, vts, sizeof vts / sizeof vts[0]);
    static volatile UInt32 vtLogged;
    if (OSCompareAndSwap(0, 1, &vtLogged)) IOLog("NVRMFB: Apple vtable lengths %s\n", vt.text);

    gNvrmfbLayoutState = (int)st;
    gNvrmfbLayoutClasses = (int)gNvrmfbLayout.run.tally.classes;
    gNvrmfbLayoutVtables = (int)vt.compared;
    gNvrmfbLayoutVtDiff = (int)vt.differ;
    if (IOService *pci = nvrmfbLayoutPci(provider)) {
        OSObject *had = pci->copyProperty("nvrmfb-layout");
        if (!had) {
            pci->setProperty("nvrmfb-layout-vtables", vt.text);     // first, so whoever finds the verdict finds this too
            pci->setProperty("nvrmfb-layout", gNvrmfbLayout.run.text);
            // NVRM declines a refused framebuffer layout only if it reads this property before it starts the GPU: that
            // holds when this probe runs in the matching pass that also starts NVRM (same device, score 30000 against
            // 1000, probes before starts). If NVRM is already there, it sees the property only before it brings the
            // display up, and that boot is counted by the boot guard.
            const bool nvrmFirst = pci->getProperty("nvrm-claimed") != nullptr;
            IOLog("NVRMFB: %s published nvrmfb-layout on %s %s\n", who ? who : "?", pci->getName(),
                  nvrmFirst ? "AFTER NVRM started on it: NVRM sees it only before the display bring-up"
                            : "before NVRM started on it: NVRM reads it before it counts a start");
        }
        OSSafeReleaseNULL(had);
    }
    if (st == kNvLayoutRefused)
        IOLog("NVRMFB: %s does not start: an Apple framebuffer class has another size than in this build (boot-arg "
              "nvlayout=0 starts it anyway)\n", who ? who : "?");
    return st != kNvLayoutRefused;
}
#endif /* KERNEL */

#endif
