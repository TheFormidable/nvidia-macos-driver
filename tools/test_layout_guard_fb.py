#!/usr/bin/env python3
"""NVRMFB and NVRMAGDC compare the Apple classes they subclass with the running kernel, and step aside on a difference.

A false refusal in NVRMFB means no picture from the card, a missed one means a kext whose fields sit on Apple's. So
only what the kernel states through an interface may refuse: the size of a class. The length of a kernel vtable is read
off the table (the entries up to the first null word), and whether IOGraphicsFamily's tables end that way in the
running kernel collection is not something the kext can ask; NVRMFB reports those lengths and never refuses on them.
What this checks, on the source text and with the host compiler:
  - the three copies of nvrm_layout.h are one file;
  - every vtable NVRMFB reads is named exactly ("__ZTV" + length + name) and is the base of one of its own classes, so
    the kext imports it already (a new or mistyped name is an import that stops the kext from loading);
  - every Apple base of an NVRMFB class, and IOFramebuffer's own base, has a size row; AGDC has its one row;
  - the check runs where the design puts it: first in NVRMFBClaim::probe (before IOService::probe), in NVRMBootFB::probe
    once the driver is known to be off, at the top of NVRMNVDAFramebuffer::start before anything is allocated, and in
    the AGDC worker before the object is allocated, whose refusal is state -6 with the framebuffer reference dropped;
  - the sysctls are read-only, each registered once and only by code that stays (never from a probe), and AGDC's is
    in both of its register and unregister lists;
  - a vtable length cannot refuse: the evaluation that returns NVRMFB's verdict is handed no vtable and no calibration
    table, nothing else in the file computes a verdict, the function returns on that verdict alone, and the lengths go
    to their own property, their own sysctls and one log line;
  - no class of NVRMFB adds a virtual function to its Apple base (its tables are exactly as long as the bases', which
    is what lets the kernel collection builder catch a base that grew);
  - the AGDC worker compares into a result of its own, not one that two overlapping runs would share;
  - on the pure part, compiled from the kext's own headers: with NVRMFB's three rows, equal -> ok, a differing base name
    or an unregistered class -> still ok, a size refuses, nvlayout=0 overrides; and the report of the lengths says
    "same", "differ" (longer and shorter) or "not compared", reads no table when IOService does not come out equal,
    reads no word past the expected end, and is never a text another kext would take for "refused".
Each group has a negative control on a changed copy of the text."""
import filecmp
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
FB = ROOT / "kexts/NVRMFB/fb/nvrm-fb.cpp"
FBL = ROOT / "kexts/NVRMFB/fb/nvrmfb_layout.h"
AGDC = ROOT / "kexts/NVRMAGDC/nvrm-agdc.cpp"
FAILS = []


def arm(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def strip_comments(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def block(text, start, end_marker="\n}\n"):
    """text from `start` to the end of that function or class (the first end_marker after it); '' when absent."""
    a = text.find(start)
    if a < 0:
        return ""
    b = text.find(end_marker, a)
    return text[a:b if b >= 0 else len(text)]


def call_args(text, name):
    """the argument lists of every call of name( in text, split at the commas outside brackets"""
    calls = []
    for m in re.finditer(r"\b" + re.escape(name) + r"\s*\(", text):
        depth, i, args, cur = 1, m.end(), [], ""
        while i < len(text) and depth:
            c = text[i]
            if c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
                if not depth:
                    break
            if c == "," and depth == 1:
                args.append(cur.strip())
                cur = ""
            else:
                cur += c
            i += 1
        args.append(cur.strip())
        calls.append(args)
    return calls


def vtable_problems(layout_h, fb_src):
    out = []
    labels = re.findall(r'NVL_VTABLE\(\s*(\w+)\s*,\s*"([^"]*)"\s*\)', strip_comments(layout_h))
    if not labels:
        out.append("no NVL_VTABLE at all")
    bases = set(re.findall(r"class\s+\w+\s*:\s*public\s+(\w+)", strip_comments(fb_src)))
    for cls, sym in labels:
        if sym != f"__ZTV{len(cls)}{cls}":
            out.append(f"{cls}: label {sym} is not __ZTV{len(cls)}{cls}")
        if cls not in bases:
            out.append(f"{cls}: not the base of an NVRMFB class, so its vtable would be a new import")
    used = set(re.findall(r"NVL_VT\(\s*(\w+)\s*\)", strip_comments(layout_h)))
    declared = {c for c, _ in labels}
    for c in used - declared:
        out.append(f"NVL_VT({c}) without NVL_VTABLE")
    return out


APPLE_FB_BASES = {"IOFramebuffer", "IODisplayParameterHandler"}


def row_problems(layout_h, fb_src):
    out = []
    rows = dict(re.findall(r"NVL_ROW\(\s*(\w+)\s*,\s*(\w+)\s*\)", strip_comments(layout_h)))
    bases = set(re.findall(r"class\s+\w+\s*:\s*public\s+(\w+)", strip_comments(fb_src))) & APPLE_FB_BASES
    for b in sorted(bases):
        if b not in rows:
            out.append(f"{b} (base of an NVRMFB class) has no row")
    if "IOFramebuffer" in rows and rows["IOFramebuffer"] not in rows:
        out.append(f"IOFramebuffer's base {rows['IOFramebuffer']} has no row")
    for cls, sup in rows.items():
        if f"__is_base_of({sup}, {cls})" not in layout_h:
            out.append(f"no static_assert that {sup} is the base of {cls}")
    return out


def wiring_problems(fb_src, agdc_src):
    out = []
    src = strip_comments(fb_src)
    claim = block(src, "class NVRMFBClaim", "\n};\n")
    p = claim.find("nvrmfbLayoutAllows(")
    q = claim.find("IOService::probe(")
    if p < 0 or q < 0 or p > q:
        out.append("NVRMFBClaim::probe does not check the layout before IOService::probe")
    s = claim.find("bool start(")
    if s < 0 or claim.find("nvrmfbLayoutSysctls();", s) < claim.find("IOService::start(", s):
        out.append("NVRMFBClaim::start does not register the layout sysctls after IOService::start")
    boot = block(src, "class NVRMBootFB", "\n};\n")
    probe = boot[boot.find("IOService *probe("):]
    a = probe.find("if (!nvrm_driver_off() || !provider) return NULL;")
    b = probe.find('nvrmfbLayoutAllows(provider, "NVRMBootFB")')
    c = probe.find("getConsoleInfo(")
    if not (0 <= a < b < c):
        out.append("NVRMBootFB::probe does not check the layout after the driver-off test and before it uses the console")
    start = block(src, "bool NVRMNVDAFramebuffer::start(IOService *provider)")
    a = start.find("if (nvrm_driver_off()) return false;")
    b = start.find('if (!nvrmfbLayoutAllows(provider, "NVRMNVDAFramebuffer")) return false;')
    c = start.find("IOSimpleLockAlloc(")
    if not (0 <= a < b < c):
        out.append("NVRMNVDAFramebuffer::start does not refuse before its first allocation")
    for name, body in (("NVRMFBClaim::probe", claim[:s] if s > 0 else claim), ("NVRMBootFB::probe", probe)):
        if "nvrmfbLayoutSysctls" in body:
            out.append(f"{name} registers sysctls: a kext that declines may be unloaded under them")
    guard = block(src, "static void fbGuardCleared(const char *why)\n{")
    if "nvrmfbLayoutSysctls();" not in guard:
        out.append("fbGuardCleared does not register the layout sysctls")
    ag = strip_comments(agdc_src)
    work = block(ag, "static void nvdaAgdcWorkBody(void)\n{")
    e = work.find("nvLayoutEvaluate(")
    r = work.find("gAgdcLayout == kNvLayoutRefused")
    al = work.find("OSTypeAlloc(NVDADeviceControl)")
    if not (0 <= e < r < al):
        out.append("the AGDC worker does not evaluate and decide before OSTypeAlloc")
    refused = work[r:work.find("}", r)] if r >= 0 else ""
    for need in ("gAgdcState = -6;", "nvdaAgdcDropFB();", "return;"):
        if need not in refused:
            out.append(f"AGDC refusal lacks {need}")
    if "NVL_ROW(AppleGraphicsDeviceControl, IOService)" not in ag:
        out.append("AGDC has no AppleGraphicsDeviceControl row")
    if '"nvagdc-layout"' not in work:
        out.append("AGDC does not publish nvagdc-layout")
    if "NvLayoutRun run;" not in work[:e if e >= 0 else 0] or re.search(r"static\s+NvLayoutRun\b", ag):
        out.append("the AGDC worker does not compare into a result of its own: two overlapping runs would share it")
    for lst in ("static void nvdaAgdcInstallSysctl(void)", "static void nvdaAgdcRemoveSysctl(void)"):
        if "sysctl__debug_nvrmfb_agdc_layout" not in block(ag, lst):
            out.append(f"debug.nvrmfb_agdc_layout missing from {lst}")
    return out


FB_SYSCTLS = ("nvrmfb_layout", "nvrmfb_layout_classes", "nvrmfb_layout_vtables", "nvrmfb_layout_vtdiff")


def sysctl_problems(layout_h, agdc_src, fb_src):
    out = []
    want = {name: layout_h for name in FB_SYSCTLS}
    want["nvrmfb_agdc_layout"] = agdc_src
    for name, text in want.items():
        m = re.search(r"SYSCTL_INT\s*\(\s*_debug\s*,\s*OID_AUTO\s*,\s*" + name + r"\s*,([^,]*),", text)
        if not m:
            out.append(f"debug.{name} is not declared as SYSCTL_INT")
        elif re.search(r"CTLFLAG_(RW|WR|ANYBODY)", m.group(1)) or "CTLFLAG_RD" not in m.group(1):
            out.append(f"debug.{name} is not read-only")
    # NVRMFB has no list that takes sysctls out again: each of these is registered in the one function that runs once,
    # exactly once, and nowhere else
    src = strip_comments(layout_h)
    declared = set(re.findall(r"SYSCTL_\w+\s*\(\s*_debug\s*,\s*OID_AUTO\s*,\s*(\w+)", src))
    if declared != set(FB_SYSCTLS):
        out.append(f"the header declares {sorted(declared)}, the test knows {sorted(FB_SYSCTLS)}")
    once = block(src, "static void nvrmfbLayoutSysctls(void)")
    if "OSCompareAndSwap(0, 1, &once)" not in once:
        out.append("nvrmfbLayoutSysctls does not run once")
    for name in sorted(declared):
        reg = f"sysctl_register_oid(&sysctl__debug_{name});"
        if once.count(reg) != 1 or src.count(reg) != 1 or f"sysctl__debug_{name}" in strip_comments(fb_src):
            out.append(f"debug.{name} is not registered exactly once, in nvrmfbLayoutSysctls")
    return out


def verdict_problems(layout_h):
    """Only a class size may refuse. A length read off a kernel vtable is reported and must not reach the verdict."""
    out = []
    src = strip_comments(layout_h)
    calls = call_args(src, "nvLayoutEvaluateOnce")
    if len(calls) != 1:
        out.append(f"{len(calls)} calls of nvLayoutEvaluateOnce, expected one")
    elif len(calls[0]) != 8 or calls[0][4:] != ["nullptr", "0", "nullptr", "0"]:
        out.append("the evaluation that can refuse is handed a vtable or a calibration table: " + ", ".join(calls[0][4:]))
    for other in ("nvLayoutEvaluate", "nvLayoutVerdict", "nvLayoutVtRow"):
        if call_args(src, other):
            out.append(f"{other}() is called here: a second place that could turn a table into a verdict")
    allows = block(src, "static bool nvrmfbLayoutAllows(")
    if not re.search(r"const\s+NvLayoutState\s+st\s*=\s*nvLayoutEvaluateOnce\s*\(", allows):
        out.append("st is not the constant result of the size evaluation")
    returns = [r.strip() for r in re.findall(r"\breturn\b([^;]*);", allows)]
    if returns != ["true", "st != kNvLayoutRefused"]:
        out.append("nvrmfbLayoutAllows returns on something other than the size verdict: " + " | ".join(returns))
    if not re.search(r"static\s+inline\s+void\s+nvrmfbVtReport\s*\(", src):
        out.append("nvrmfbVtReport has a result: a caller could refuse on it")
    # where the two results go: the property NVRM reads carries the size verdict, the lengths have their own
    for need, what in (('pci->setProperty("nvrmfb-layout", gNvrmfbLayout.run.text);', "nvrmfb-layout is not the verdict text"),
                       ('pci->setProperty("nvrmfb-layout-vtables", vt.text);', "the lengths are not published as nvrmfb-layout-vtables"),
                       ('IOLog("NVRMFB: Apple vtable lengths %s\\n", vt.text);', "the lengths are not logged"),
                       ("gNvrmfbLayoutVtables = (int)vt.compared;", "debug.nvrmfb_layout_vtables is not the number of tables read"),
                       ("gNvrmfbLayoutVtDiff = (int)vt.differ;", "debug.nvrmfb_layout_vtdiff is not the number that differ")):
        if allows.count(need) != 1:
            out.append(what)
    return out


def virtual_problems(fb_src):
    """A class of this kext that added a virtual function would have a longer table than its base: the kernel collection
    builder then no longer rejects the kext when the base grows by as many, and the kext's function sits on Apple's."""
    out = []
    src = strip_comments(fb_src)
    classes = re.findall(r"class\s+(\w+)\s*:\s*public\s+(\w+)", src)
    if not {b for _, b in classes} >= APPLE_FB_BASES:
        out.append("no NVRMFB class on IOFramebuffer and IODisplayParameterHandler was found")
    for cls, _ in classes:
        body = block(src, f"class {cls} ", "\n};\n")
        if re.search(r"\bvirtual\b|OSMetaClassDeclareReserved", body):
            out.append(f"{cls} declares a virtual function of its own")
    return out


PURE = r'''
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <sys/mman.h>
#include <unistd.h>
#include "nvrmfb_layout.h"
// NVRMFB's three rows with the sizes the SDK headers give (IOFramebuffer 0x1d0, the other two 0x88)
static const NvLayoutRow rows[3] = { { "IOFramebuffer", "IOGraphicsDevice", 0x1d0 }, { "IOGraphicsDevice", "IOService", 0x88 },
                                     { "IODisplayParameterHandler", "IOService", 0x88 } };
// What nvrmfbLayoutAllows hands the evaluation that can refuse: the three rows, no vtable and no calibration table.
static NvLayoutTally sizes(const NvLayoutSeen *seen)
{
    NvLayoutTally t; nvLayoutTallyInit(&t);
    for (int i = 0; i < 3; i++) nvLayoutRow(&rows[i], &seen[i], &t);
    t.calibrated = nvLayoutVtCmp(nullptr, 0, nullptr) == 0 ? 1 : 0;     // what the evaluation makes of (nullptr, 0)
    return t;
}
// The lengths: three tables with that many entries and nulls after them (400 words each, so "longer" is in bounds).
static const void *tab[3][400];
static NvrmfbVtReport report(int ioService, int fb, int handler)
{
    const int ends[3] = { ioService, fb, handler };
    for (int v = 0; v < 3; v++)
        for (int i = 0; i < 400; i++) tab[v][i] = i < ends[v] ? (const void *)&tab : nullptr;
    const NvLayoutVt svc = { "IOService", tab[0], 266 };
    const NvLayoutVt vts[2] = { { "IOFramebuffer", tab[1], 350 }, { "IODisplayParameterHandler", tab[2], 280 } };
    NvrmfbVtReport r;
    nvrmfbVtReport(&r, &svc, vts, 2);
    return r;
}
#define UNITS " (virtual functions: this macOS/the driver)"
int main()
{
    const NvLayoutSeen same[3] = { { true, 0x1d0, "IOGraphicsDevice" }, { true, 0x88, "IOService" }, { true, 0x88, "IOService" } };
    char text[200];

    // ---- the verdict: sizes alone
    NvLayoutTally t = sizes(same);
    assert(nvLayoutVerdict(&t, false) == kNvLayoutOk && t.classes == 3 && t.vtables == 0 && t.vtBad == 0);
    nvLayoutText(text, sizeof text, kNvLayoutOk, &t, "25B78", "25.1.0");
    assert(!strcmp(text, "ok: 3 classes, vtables not compared; macOS 25B78 (Darwin 25.1.0)"));
    assert(nvLayoutTextState(text) == kNvLayoutOk);
    // a base name that differs (a renamed intermediate class) is counted, never a refusal
    const NvLayoutSeen renamed[3] = { { true, 0x1d0, "IOGraphicsDevice2" }, { true, 0x88, "IOService" }, { true, 0x88, "IOService" } };
    t = sizes(renamed);
    assert(nvLayoutVerdict(&t, false) == kNvLayoutOk && t.superDiff == 1);
    // a class not registered (IOGraphicsDevice under another name) is counted, never a refusal
    const NvLayoutSeen absent[3] = { { true, 0x1d0, "IOGraphicsDevice" }, { false, 0, nullptr }, { true, 0x88, "IOService" } };
    t = sizes(absent);
    assert(nvLayoutVerdict(&t, false) == kNvLayoutOk && t.absent == 1 && t.classes == 2);
    // controls: a grown and a shrunk class refuse, and nvlayout=0 overrides
    const NvLayoutSeen grown[3] = { { true, 0x1e0, "IOGraphicsDevice" }, { true, 0x88, "IOService" }, { true, 0x88, "IOService" } };
    t = sizes(grown);
    assert(nvLayoutVerdict(&t, false) == kNvLayoutRefused && nvLayoutVerdict(&t, true) == kNvLayoutOverridden);
    nvLayoutText(text, sizeof text, kNvLayoutRefused, &t, "25B78", "25.1.0");
    assert(nvLayoutTextRefused(text) && strstr(text, "IOFramebuffer is 0x1e0 bytes in this macOS, 0x1d0 in the driver"));
    assert(strstr(text, "(1 size, 0 vtables differ of 3, 0)"));
    const NvLayoutSeen shrunk[3] = { { true, 0x1d0, "IOGraphicsDevice" }, { true, 0x88, "IOService" }, { true, 0x80, "IOService" } };
    t = sizes(shrunk);
    assert(nvLayoutVerdict(&t, false) == kNvLayoutRefused);

    // ---- the lengths: a report, in every case a text that no reader takes for a verdict
    NvrmfbVtReport r = report(266, 350, 280);
    assert(r.compared == 2 && r.differ == 0);
    assert(!strcmp(r.text, "same: IOFramebuffer 350/350, IODisplayParameterHandler 280/280" UNITS));
    assert(nvLayoutTextState(r.text) == kNvLayoutNotRun);
    // a table that goes on where this build expects the end (the kernel has more functions, or no null entry follows)
    r = report(266, 352, 280);
    assert(r.compared == 2 && r.differ == 1);
    assert(!strcmp(r.text, "differ: IOFramebuffer >350/350, IODisplayParameterHandler 280/280" UNITS "; reported only, not a reason to refuse"));
    assert(nvLayoutTextState(r.text) == kNvLayoutNotRun && !nvLayoutTextRefused(r.text));
    // a table that ends early
    r = report(266, 350, 279);
    assert(r.compared == 2 && r.differ == 1);
    assert(!strcmp(r.text, "differ: IOFramebuffer 350/350, IODisplayParameterHandler 279/280" UNITS "; reported only, not a reason to refuse"));
    r = report(266, 400, 1);
    assert(r.compared == 2 && r.differ == 2 && strstr(r.text, "differ: IOFramebuffer >350/350, IODisplayParameterHandler 1/280"));
    assert(nvLayoutTextState(r.text) == kNvLayoutNotRun);
    // IOService itself does not read the way this build expects: the reading is not trusted and nothing else is read
    r = report(270, 352, 279);
    assert(r.compared == 0 && r.differ == 0 && !strcmp(r.text, "not compared: IOService >266/266" UNITS));
    r = report(260, 350, 280);
    assert(r.compared == 0 && r.differ == 0 && !strcmp(r.text, "not compared: IOService 260/266" UNITS));
    assert(nvLayoutTextState(r.text) == kNvLayoutNotRun);
    {   // "nothing else is read": these tables are addresses that fault when touched
        for (int i = 0; i < 400; i++) tab[0][i] = i < 260 ? (const void *)&tab : nullptr;
        const NvLayoutVt svc = { "IOService", tab[0], 266 };
        const NvLayoutVt bad[2] = { { "IOFramebuffer", (const void *const *)(uintptr_t)8, 350 },
                                    { "IODisplayParameterHandler", (const void *const *)(uintptr_t)16, 280 } };
        nvrmfbVtReport(&r, &svc, bad, 2);
        assert(r.compared == 0 && !strncmp(r.text, "not compared: ", 14));
    }
    {   // no table at all, and a slot count that cannot be right
        const NvLayoutVt svc = { "IOService", tab[0], 266 };
        const NvLayoutVt vts[1] = { { "IOFramebuffer", tab[1], 350 } };
        nvrmfbVtReport(&r, nullptr, vts, 1);
        assert(r.compared == 0 && r.differ == 0 && !strcmp(r.text, "not compared: no table"));
        nvrmfbVtReport(&r, &svc, nullptr, 0);
        assert(r.compared == 0 && !strcmp(r.text, "not compared: no table"));
        for (int i = 0; i < 400; i++) tab[0][i] = i < 266 ? (const void *)&tab : nullptr;
        const NvLayoutVt odd[1] = { { "IOFramebuffer", tab[1], -1 } };
        nvrmfbVtReport(&r, &svc, odd, 1);
        assert(r.compared == 0 && r.differ == 0 && !strcmp(r.text, "not compared: IOFramebuffer not readable" UNITS));
    }
    {   // No word past the expected end is read: the 281 words of a table (280 entries and what follows them) are the
        // last words of a page, and the page after it cannot be read. Once with the null entry, once without.
        const long page = sysconf(_SC_PAGESIZE);
        char *m = (char *)mmap(nullptr, (size_t)page * 2, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
        const bool guarded = m != MAP_FAILED && mprotect(m + page, (size_t)page, PROT_NONE) == 0;
        assert(guarded);
        const void **end = (const void **)(m + page);
        for (int i = 1; i <= 281; i++) end[-i] = (const void *)&tab;
        for (int i = 0; i < 400; i++) tab[0][i] = i < 266 ? (const void *)&tab : nullptr;
        const NvLayoutVt svc = { "IOService", tab[0], 266 };
        const NvLayoutVt vts[1] = { { "IODisplayParameterHandler", end - 281, 280 } };
        nvrmfbVtReport(&r, &svc, vts, 1);
        assert(r.compared == 1 && r.differ == 1 && strstr(r.text, "differ: IODisplayParameterHandler >280/280"));
        end[-1] = nullptr;
        nvrmfbVtReport(&r, &svc, vts, 1);
        assert(r.compared == 1 && r.differ == 0 && strstr(r.text, "same: IODisplayParameterHandler 280/280"));
        munmap(m, (size_t)page * 2);
    }
    {   // a line longer than the buffer is cut, not written past it
        struct { NvrmfbVtReport rep; char guard[8]; } g;
        memset(g.guard, 0x5a, sizeof g.guard);
        static const char longName[] = "IOFramebufferIOFramebufferIOFramebufferIOFramebufferIOFramebufferIOFramebufferIOFramebuffer"
                                       "IOFramebufferIOFramebufferIOFramebufferIOFramebufferIOFramebufferIOFramebufferIOFramebuffer"
                                       "IOFramebufferIOFramebufferIOFramebufferIOFramebufferIOFramebufferIOFramebufferIOFramebuffer";
        for (int i = 0; i < 400; i++) { tab[0][i] = i < 266 ? (const void *)&tab : nullptr; tab[1][i] = i < 350 ? (const void *)&tab : nullptr; }
        const NvLayoutVt svc = { "IOService", tab[0], 266 };
        const NvLayoutVt vts[1] = { { longName, tab[1], 350 } };
        nvrmfbVtReport(&g.rep, &svc, vts, 1);
        assert(strlen(g.rep.text) == sizeof g.rep.text - 1 && !strncmp(g.rep.text, "same: IOFramebufferIO", 21));
        for (unsigned i = 0; i < sizeof g.guard; i++) assert(g.guard[i] == 0x5a);
    }
    puts("sizes decide (ok, renamed base, absent class, grown, shrunk, overridden); the vtable lengths are a report");
    return 0;
}
'''


def main():
    copies = [ROOT / f"kexts/{k}/nvrm_layout.h" for k in ("NVRM", "NVRMFB", "NVRMAGDC")]
    arm("the three copies of nvrm_layout.h are identical",
        all(p.exists() for p in copies) and all(filecmp.cmp(copies[0], p, shallow=False) for p in copies[1:]))
    fb, fbl, agdc = FB.read_text(), FBL.read_text(), AGDC.read_text()

    pr = vtable_problems(fbl, fb)
    arm("vtable labels are exact and each is the base of an NVRMFB class", not pr, "; ".join(pr))
    pr = row_problems(fbl, fb)
    arm("every Apple base of an NVRMFB class and IOFramebuffer's base have a row", not pr, "; ".join(pr))
    pr = wiring_problems(fb, agdc)
    arm("the check runs in the claim's probe, the boot framebuffer's probe, the framebuffer's start and the AGDC worker",
        not pr, "; ".join(pr))
    pr = sysctl_problems(fbl, agdc, fb)
    arm("the five layout sysctls are read-only and NVRMFB's four are each registered once", not pr, "; ".join(pr))
    pr = verdict_problems(fbl)
    arm("only a class size can refuse: the vtable lengths go to their own property, sysctls and log line", not pr, "; ".join(pr))
    pr = virtual_problems(fb)
    arm("no NVRMFB class adds a virtual function to its Apple base", not pr, "; ".join(pr))

    # negative controls, on changed copies of the text
    arm("control: a mistyped vtable label is caught",
        any("label" in x for x in vtable_problems(fbl.replace('"__ZTV13IOFramebuffer"', '"__ZTV12IOFramebuffer"'), fb)))
    arm("control: a vtable of a class NVRMFB does not subclass is caught",
        any("new import" in x for x in vtable_problems(fbl + '\nNVL_VTABLE(IODisplay, "__ZTV9IODisplay");\n', fb)))
    arm("control: a missing row is caught",
        any("IODisplayParameterHandler" in x for x in row_problems(fbl.replace("NVL_ROW(IODisplayParameterHandler, IOService),", ""), fb)))
    arm("control: the claim's check after IOService::probe is caught",
        any("NVRMFBClaim::probe" in x for x in wiring_problems(
            fb.replace('        if (!nvrmfbLayoutAllows(provider, "NVRMFBClaim")) return NULL;\n        return IOService::probe(provider, score);',
                       '        IOService *r = IOService::probe(provider, score);\n        if (!nvrmfbLayoutAllows(provider, "NVRMFBClaim")) return NULL;\n        return r;'), agdc)))
    arm("control: the framebuffer start without the check is caught",
        any("NVRMNVDAFramebuffer::start" in x for x in wiring_problems(
            fb.replace('    if (!nvrmfbLayoutAllows(provider, "NVRMNVDAFramebuffer")) return false;\n', ""), agdc)))
    arm("control: sysctls registered from a probe are caught",
        any("registers sysctls" in x for x in wiring_problems(
            fb.replace('if (!nvrmfbLayoutAllows(provider, "NVRMBootFB")) return NULL;',
                       'if (!nvrmfbLayoutAllows(provider, "NVRMBootFB")) return NULL; nvrmfbLayoutSysctls();'), agdc)))
    agdc_late = agdc.replace("        nvdaAgdcScan(pci);\n        NVDADeviceControl *d = OSTypeAlloc(NVDADeviceControl);\n",
                             "        nvdaAgdcScan(pci);\n        NVDADeviceControl *d0 = OSTypeAlloc(NVDADeviceControl); (void)d0;\n        NVDADeviceControl *d = nullptr;\n", 1)
    agdc_late = agdc_late.replace("        if (!gAgdcLayout) {", "        NVDADeviceControl *e = OSTypeAlloc(NVDADeviceControl); (void)e;\n        if (!gAgdcLayout) {", 1)
    arm("control: an AGDC object allocated before the decision is caught",
        any("before OSTypeAlloc" in x for x in wiring_problems(fb, agdc_late)))
    arm("control: an AGDC refusal that keeps the framebuffer reference is caught",
        any("nvdaAgdcDropFB" in x for x in wiring_problems(
            fb, agdc.replace("            nvdaAgdcDropFB(); return;\n        }\n        nvdaAgdcScan(pci);", "            return;\n        }\n        nvdaAgdcScan(pci);"))))
    arm("control: an AGDC result that overlapping runs of the worker share is caught",
        any("result of its own" in x for x in wiring_problems(
            fb, agdc.replace("static int gAgdcLayout = 0;\n", "static int gAgdcLayout = 0;\nstatic NvLayoutRun gAgdcLayoutRun;\n", 1))))
    arm("control: a writable layout sysctl is caught",
        any("read-only" in x for x in sysctl_problems(fbl.replace("nvrmfb_layout, CTLFLAG_RD", "nvrmfb_layout, CTLFLAG_RW"), agdc, fb)))
    arm("control: a layout sysctl that is declared and never registered is caught",
        any("nvrmfb_layout_vtdiff is not registered" in x for x in sysctl_problems(
            fbl.replace("    sysctl_register_oid(&sysctl__debug_nvrmfb_layout_vtdiff);\n", ""), agdc, fb)))
    arm("control: a layout sysctl registered a second time, from the framebuffer source, is caught",
        any("nvrmfb_layout_classes is not registered" in x for x in sysctl_problems(
            fbl, agdc, fb + "\nstatic void again(void) { sysctl_register_oid(&sysctl__debug_nvrmfb_layout_classes); }\n")))
    arm("control: a vtable handed to the evaluation that can refuse is caught",
        any("handed a vtable" in x for x in verdict_problems(fbl.replace(
            "                                                  nullptr, 0, nullptr, 0);",
            "                                                  vts, 2, nvLayoutVt_IOService + 2, NVL_BUILT(IOService));"))))
    arm("control: a refusal on the reported lengths is caught",
        any("other than the size verdict" in x for x in verdict_problems(fbl.replace(
            "    gNvrmfbLayoutState = (int)st;\n", "    if (vt.differ) return false;\n    gNvrmfbLayoutState = (int)st;\n"))))
    arm("control: a second verdict computed from the tables is caught",
        any("nvLayoutVerdict() is called here" in x for x in verdict_problems(fbl.replace(
            "    gNvrmfbLayoutState = (int)st;\n",
            "    NvLayoutTally t2; nvLayoutTallyInit(&t2); nvLayoutVtRow(&vts[0], &t2);\n"
            "    gNvrmfbLayoutState = (int)nvLayoutVerdict(&t2, false);\n"))))
    arm("control: the lengths published under the name NVRM reads is caught",
        any("nvrmfb-layout is not the verdict text" in x for x in verdict_problems(fbl.replace(
            'pci->setProperty("nvrmfb-layout", gNvrmfbLayout.run.text);', 'pci->setProperty("nvrmfb-layout", vt.text);'))))
    arm("control: an NVRMFB class with a virtual function of its own is caught",
        any("NVRMBootFB declares a virtual function" in x for x in virtual_problems(
            fb.replace("    OSDeclareDefaultStructors(NVRMBootFB)\n", "    OSDeclareDefaultStructors(NVRMBootFB)\n    virtual void nvExtra();\n", 1))))

    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        (d / "t.cpp").write_text(PURE)

        def pure(out, *include_dirs):
            """build the host program with these include directories and run it: 0 passed, None did not build"""
            build = subprocess.run(["c++", "-std=c++17", "-Wall", "-Wextra", "-Werror", *[f"-I{i}" for i in include_dirs],
                                    str(d / "t.cpp"), "-o", str(d / out)], capture_output=True, text=True)
            if build.returncode != 0:
                return None, build.stderr[-600:]
            res = subprocess.run([str(d / out)], capture_output=True, text=True)
            return res.returncode, res.stderr[-600:]

        real = (ROOT / "kexts/NVRMFB/fb", ROOT / "kexts/NVRMFB")
        code, err = pure("t", *real)
        arm("the kext's own headers on the host: sizes decide, the vtable lengths are a report", code == 0, err)
        # control: the same program against a verdict that also refuses on a differing base name must fail
        (d / "bad").mkdir()
        (d / "bad/nvrm_layout.h").write_text((ROOT / "kexts/NVRMFB/nvrm_layout.h").read_text().replace(
            "if (t->sizeBad || t->vtBad)", "if (t->sizeBad || t->vtBad || t->superDiff)"))
        code, _ = pure("tb", d / "bad", *real)
        arm("control: a verdict that refuses on a renamed base fails the host program", code is not None and code != 0)
        # control: the same program against a report that counts only a table that ends early must fail
        (d / "bad2").mkdir()
        changed = FBL.read_text().replace("        if (r != 0) rep->differ++;", "        if (r < 0) rep->differ++;")
        (d / "bad2/nvrmfb_layout.h").write_text(changed)
        code, _ = pure("tc", d / "bad2", ROOT / "kexts/NVRMFB")
        arm("control: a report that misses a table longer than expected fails the host program",
            changed != FBL.read_text() and code is not None and code != 0)
        # control: the same program against a report that reads the tables although IOService came out different must fail
        (d / "bad3").mkdir()
        changed = FBL.read_text().replace("    if (cal != 0) {\n", "    if (cal == 2) {\n")
        (d / "bad3/nvrmfb_layout.h").write_text(changed)
        code, _ = pure("td", d / "bad3", ROOT / "kexts/NVRMFB")
        arm("control: a report that reads the tables without the IOService self-check fails the host program",
            changed != FBL.read_text() and code is not None and code != 0)

    print("PASS" if not FAILS else f"FAILED ({len(FAILS)})")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
