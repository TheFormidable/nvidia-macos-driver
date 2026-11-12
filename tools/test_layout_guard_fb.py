#!/usr/bin/env python3
"""NVRMFB and NVRMAGDC compare the Apple classes they subclass with the running kernel, and step aside on a difference.

A false refusal in NVRMFB means no picture from the card, a missed one means a kext whose fields sit on Apple's. What
this checks, on the source text and with the host compiler:
  - the three copies of nvrm_layout.h are one file;
  - every vtable NVRMFB reads is named exactly ("__ZTV" + length + name) and is the base of one of its own classes, so
    the kext imports it already (a new or mistyped name is an import that stops the kext from loading);
  - every Apple base of an NVRMFB class, and IOFramebuffer's own base, has a size row; AGDC has its one row;
  - the check runs where the design puts it: first in NVRMFBClaim::probe (before IOService::probe), in NVRMBootFB::probe
    once the driver is known to be off, at the top of NVRMNVDAFramebuffer::start before anything is allocated, and in
    the AGDC worker before the object is allocated, whose refusal is state -6 with the framebuffer reference dropped;
  - the sysctls are read-only, registered only by code that stays (never from a probe), and AGDC's is in both of its
    register and unregister lists;
  - on the pure part, the verdicts NVRMFB can produce with its three rows: equal -> ok, an uncalibrated kernel or a
    differing base name -> still ok, only a size or a vtable length refuses, and nvlayout=0 overrides.
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
    for lst in ("static void nvdaAgdcInstallSysctl(void)", "static void nvdaAgdcRemoveSysctl(void)"):
        if "sysctl__debug_nvrmfb_agdc_layout" not in block(ag, lst):
            out.append(f"debug.nvrmfb_agdc_layout missing from {lst}")
    return out


def sysctl_problems(layout_h, agdc_src):
    out = []
    want = {"nvrmfb_layout": layout_h, "nvrmfb_layout_classes": layout_h, "nvrmfb_layout_vtables": layout_h,
            "nvrmfb_agdc_layout": agdc_src}
    for name, text in want.items():
        m = re.search(r"SYSCTL_INT\s*\(\s*_debug\s*,\s*OID_AUTO\s*,\s*" + name + r"\s*,([^,]*),", text)
        if not m:
            out.append(f"debug.{name} is not declared as SYSCTL_INT")
        elif re.search(r"CTLFLAG_(RW|WR|ANYBODY)", m.group(1)) or "CTLFLAG_RD" not in m.group(1):
            out.append(f"debug.{name} is not read-only")
    return out


PURE = r'''
#include <cassert>
#include <cstdio>
#include <cstring>
#include "nvrm_layout.h"
// NVRMFB's three rows with the sizes the SDK headers give (IOFramebuffer 0x1d0, the other two 0x88)
static const NvLayoutRow rows[3] = { { "IOFramebuffer", "IOGraphicsDevice", 0x1d0 }, { "IOGraphicsDevice", "IOService", 0x88 },
                                     { "IODisplayParameterHandler", "IOService", 0x88 } };
static NvLayoutTally run(const NvLayoutSeen *seen, int calibrated, const int *vtEnd)
{
    NvLayoutTally t; nvLayoutTallyInit(&t);
    for (int i = 0; i < 3; i++) nvLayoutRow(&rows[i], &seen[i], &t);
    t.calibrated = (unsigned)calibrated;
    static const void *slots[2][400];
    const int built[2] = { 350, 280 };
    for (int v = 0; calibrated && v < 2; v++) {
        for (int i = 0; i < 400; i++) slots[v][i] = i < vtEnd[v] ? (const void *)&slots : nullptr;
        NvLayoutVt vt = { v ? "IODisplayParameterHandler" : "IOFramebuffer", slots[v], built[v] };
        nvLayoutVtRow(&vt, &t);
    }
    return t;
}
int main()
{
    const NvLayoutSeen same[3] = { { true, 0x1d0, "IOGraphicsDevice" }, { true, 0x88, "IOService" }, { true, 0x88, "IOService" } };
    const int ends[2] = { 350, 280 };
    NvLayoutTally t = run(same, 1, ends);
    char text[200];
    assert(nvLayoutVerdict(&t, false) == kNvLayoutOk);
    nvLayoutText(text, sizeof text, kNvLayoutOk, &t, "25B78", "25.1.0");
    assert(!strcmp(text, "ok: 3 classes, 2 vtables; macOS 25B78 (Darwin 25.1.0)"));
    assert(nvLayoutTextState(text) == kNvLayoutOk);
    // a kernel where the IOService calibration did not come out equal: vtables are not compared, sizes still decide
    const int longer[2] = { 352, 280 };
    t = run(same, 0, longer);
    assert(nvLayoutVerdict(&t, false) == kNvLayoutOk && t.vtables == 0);
    // a base name that differs (a renamed intermediate class) is counted, never a refusal
    const NvLayoutSeen renamed[3] = { { true, 0x1d0, "IOGraphicsDevice2" }, { true, 0x88, "IOService" }, { true, 0x88, "IOService" } };
    t = run(renamed, 1, ends);
    assert(nvLayoutVerdict(&t, false) == kNvLayoutOk && t.superDiff == 1);
    // a class not registered (IOGraphicsDevice under another name) is counted, never a refusal
    const NvLayoutSeen absent[3] = { { true, 0x1d0, "IOGraphicsDevice" }, { false, 0, nullptr }, { true, 0x88, "IOService" } };
    t = run(absent, 1, ends);
    assert(nvLayoutVerdict(&t, false) == kNvLayoutOk && t.absent == 1 && t.classes == 2);
    // controls: a grown IOFramebuffer and a longer IOFramebuffer vtable refuse, and nvlayout=0 overrides
    const NvLayoutSeen grown[3] = { { true, 0x1e0, "IOGraphicsDevice" }, { true, 0x88, "IOService" }, { true, 0x88, "IOService" } };
    t = run(grown, 1, ends);
    assert(nvLayoutVerdict(&t, false) == kNvLayoutRefused && nvLayoutVerdict(&t, true) == kNvLayoutOverridden);
    nvLayoutText(text, sizeof text, kNvLayoutRefused, &t, "25B78", "25.1.0");
    assert(nvLayoutTextRefused(text) && strstr(text, "IOFramebuffer is 0x1e0 bytes in this macOS, 0x1d0 in the driver"));
    t = run(same, 1, longer);
    assert(nvLayoutVerdict(&t, false) == kNvLayoutRefused && t.vtBad == 1);
    const int shorter[2] = { 350, 279 };
    t = run(same, 1, shorter);
    assert(nvLayoutVerdict(&t, false) == kNvLayoutRefused);
    puts("pure verdicts for NVRMFB's rows: ok, uncalibrated, renamed base, absent class, and the refusing controls");
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
    pr = sysctl_problems(fbl, agdc)
    arm("the four new sysctls are read-only", not pr, "; ".join(pr))

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
    arm("control: a writable layout sysctl is caught",
        any("read-only" in x for x in sysctl_problems(fbl.replace("nvrmfb_layout, CTLFLAG_RD", "nvrmfb_layout, CTLFLAG_RW"), agdc)))

    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        (d / "t.cpp").write_text(PURE)
        build = subprocess.run(["c++", "-std=c++17", "-Wall", "-Wextra", "-Werror", f"-I{ROOT}/kexts/NVRMFB",
                                str(d / "t.cpp"), "-o", str(d / "t")], capture_output=True, text=True)
        res = subprocess.run([str(d / "t")], capture_output=True, text=True) if build.returncode == 0 else None
        arm("pure verdicts for NVRMFB's rows", res is not None and res.returncode == 0,
            build.stderr[-400:] if res is None else res.stderr[-400:])
        # control: the same program against a verdict that also refuses on a differing base name must fail
        (d / "bad").mkdir()
        (d / "bad/nvrm_layout.h").write_text((ROOT / "kexts/NVRMFB/nvrm_layout.h").read_text().replace(
            "if (t->sizeBad || t->vtBad)", "if (t->sizeBad || t->vtBad || t->superDiff)"))
        build = subprocess.run(["c++", "-std=c++17", "-Wall", "-Wextra", "-Werror", f"-I{d}/bad",
                                str(d / "t.cpp"), "-o", str(d / "tb")], capture_output=True, text=True)
        res = subprocess.run([str(d / "tb")], capture_output=True, text=True) if build.returncode == 0 else None
        arm("control: a verdict that refuses on a renamed base fails the pure arm", res is not None and res.returncode != 0)

    print("PASS" if not FAILS else f"FAILED ({len(FAILS)})")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
