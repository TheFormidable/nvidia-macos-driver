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
 * comparison itself is nvrm_layout.h's; this file names the classes and decides what NVRMFB does with the verdict.
 *
 * Where each expected value comes from:
 *   size      sizeof() of the class in the SDK headers this kext is compiled against (IOGraphicsFamily's public
 *             IOFramebuffer.h, IOGraphicsDevice.h, IODisplay.h, and IOService.h). Apple's own build of IOGraphicsFamily
 *             adds no data member and no virtual function to them (its private part has neither), and these classes keep
 *             their layout across releases with ExpansionData and reserved slots because third-party framebuffers
 *             subclass them: a size difference is a real difference.
 *   vtable    the number of slots the compiler laid out for the class (NVL_BUILT), against the kernel's own table, which
 *             this kext imports already. Compared only when the same comparison on IOService comes out equal.
 *
 * Who asks: NVRMFBClaim::probe first (it probes the PCI device before NVRM starts on it, so the verdict is on the device
 * when NVRM looks), then NVRMBootFB::probe and NVRMNVDAFramebuffer::start, which read the same evaluation. The verdict
 * text goes to the PCI device as "nvrmfb-layout"; NVRM reads it before it counts a start and declines a refused one.
 * Opt-out: boot-arg nvlayout=0 (read in nvrm_layout.h) turns a refusal into "overridden" and everything runs as before. */
#ifndef NVRMFB_LAYOUT_H
#define NVRMFB_LAYOUT_H

#include "nvrm_layout.h"
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
/* what debug.nvrmfb_layout* read: written once, by the first caller after the evaluation */
static int gNvrmfbLayoutState, gNvrmfbLayoutClasses, gNvrmfbLayoutVtables;
SYSCTL_INT(_debug, OID_AUTO, nvrmfb_layout, CTLFLAG_RD | CTLFLAG_LOCKED, &gNvrmfbLayoutState, 0,
           "Apple class layout seen by NVRMFB: 0 not run, 1 ok, 2 refused, 3 overridden (nvlayout=0), 4 unknown");
SYSCTL_INT(_debug, OID_AUTO, nvrmfb_layout_classes, CTLFLAG_RD | CTLFLAG_LOCKED, &gNvrmfbLayoutClasses, 0,
           "Apple classes NVRMFB compared by size");
SYSCTL_INT(_debug, OID_AUTO, nvrmfb_layout_vtables, CTLFLAG_RD | CTLFLAG_LOCKED, &gNvrmfbLayoutVtables, 0,
           "Apple vtables NVRMFB compared by length (0: not compared on this kernel)");

/* Registered only by an instance that started and stays (the claim, a framebuffer), never by a kext that declined:
   a kext with no instance may be unloaded, and a registered sysctl must not outlive its code. */
static void nvrmfbLayoutSysctls(void)
{
    static volatile UInt32 once;
    if (!OSCompareAndSwap(0, 1, &once)) return;
    sysctl_register_oid(&sysctl__debug_nvrmfb_layout);
    sysctl_register_oid(&sysctl__debug_nvrmfb_layout_classes);
    sysctl_register_oid(&sysctl__debug_nvrmfb_layout_vtables);
}

/* The PCI device under `from` (itself, or up to eight providers up): where NVRM and NVAccel look for the verdict. */
static IOService *nvrmfbLayoutPci(IOService *from)
{
    for (int hops = 0; from && hops < 8; hops++, from = from->getProvider())
        if (from->metaCast("IOPCIDevice")) return from;
    return nullptr;
}

/* false: this macOS lays out IOFramebuffer or IODisplayParameterHandler differently from this build, and the caller
   must not construct or start a framebuffer. Thread context with no lock held (a probe or a start). */
static bool nvrmfbLayoutAllows(IOService *provider, const char *who)
{
    const NvLayoutRow rows[] = {
        NVL_ROW(IOFramebuffer, IOGraphicsDevice),
        NVL_ROW(IOGraphicsDevice, IOService),
        NVL_ROW(IODisplayParameterHandler, IOService),
    };
    const NvLayoutVt vts[] = { NVL_VT(IOFramebuffer), NVL_VT(IODisplayParameterHandler) };
    const NvLayoutState st = nvLayoutEvaluateOnce(&gNvrmfbLayout, "NVRMFB", rows, sizeof rows / sizeof rows[0],
                                                  vts, sizeof vts / sizeof vts[0], nvLayoutVt_IOService + 2, NVL_BUILT(IOService));
    if (st == kNvLayoutNotRun) return true;      // the first evaluation never finished: carry on as before
    gNvrmfbLayoutState = (int)st;
    gNvrmfbLayoutClasses = (int)gNvrmfbLayout.run.tally.classes;
    gNvrmfbLayoutVtables = (int)gNvrmfbLayout.run.tally.vtables;
    if (IOService *pci = nvrmfbLayoutPci(provider)) {
        OSObject *had = pci->copyProperty("nvrmfb-layout");
        if (!had) {
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
        IOLog("NVRMFB: %s does not start: the Apple framebuffer classes differ from this build (boot-arg nvlayout=0 "
              "starts it anyway)\n", who ? who : "?");
    return st != kNvLayoutRefused;
}

#endif
