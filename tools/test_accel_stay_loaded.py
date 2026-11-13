#!/usr/bin/env python3
"""NVAccel.kext stays loaded once an accelerator has started, as written in kexts/NVRM/accel/nvrm-accel.cpp.

A started accelerator makes things that are never taken back and point into the kext: its sysctls (registered once,
never unregistered), the notifiers for framebuffers that publish later, thread calls kept for the boot. The kernel
unloads a kext with IOService classes by itself once its last object is gone, and `kmutil unload` asks for the same.
What must hold, on the source text (comments left out), for both builds (the NVIDIA one and the NMX_ACCEL one share
these lines):
- nvaccelStayLoaded() takes one reference on the kext itself (OSKextRetainKextWithLoadTag on kmod_info.id) at most
  once, records whether it got it, and never gives it back;
- the module stop function refuses (KERN_FAILURE) from the first call of nvaccelStayLoaded() on;
- NVAccel::start calls it first thing once the family's start succeeded, before the first notifier, sysctl
  registration, fence question, headless check or thread call of start;
- nvaccelInstallSysctl() calls it before its first registration, and every sysctl_register_oid of the kext is in
  nvaccelInstallSysctl() or in nvaccelLayoutSysctls(), which only nvaccelInstallSysctl() calls;
- probe() makes none of those things, so an accelerator declined there leaves a kext that unloads as before.
Negative controls: altered copies of the source each fail the arm about them.
   python3 tools/test_accel_stay_loaded.py"""
import pathlib
import re
import sys

R = pathlib.Path(__file__).resolve().parents[1]
SRC = R / "kexts/NVRM/accel/nvrm-accel.cpp"
LAYOUT = R / "kexts/NVRM/accel/nvaccel_layout.h"
FAILS = []


def arm(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (f"  [{str(detail).strip()[-400:]}]" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def strip(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def block(text, head):
    """the text from `head` to its matching closing brace (comments already out), or ''"""
    a = text.find(head)
    if a < 0:
        return ""
    b = text.find("{", a + len(head) - 1)
    if b < 0:
        return ""
    depth = 0
    for i in range(b, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[a:i + 1]
    return ""


def order(text, pats):
    at = 0
    for p in pats:
        m = re.compile(p).search(text, at)
        if not m:
            return False
        at = m.end()
    return True


def first(text, pats):
    hits = [m.start() for p in pats for m in re.finditer(p, text)]
    return min(hits) if hits else -1


OUTLIVES = [r"\baddMatchingNotification\s*\(", r"\bnvaccelInstallSysctl\s*\(\s*\)\s*;", r"\bnvFenceAsk\s*\(",
            r"\barmHeadlessCheck\s*\(\s*\)\s*;", r"\bthread_call_allocate\s*\(", r"\bsysctl_register_oid\s*\("]


def problems(src, layout):
    s, l = strip(src), strip(layout)
    out = []
    stay = block(s, "static void nvaccelStayLoaded(void)\n{")
    if not (stay and order(stay, [r"if\s*\(\s*!\s*OSCompareAndSwap\s*\(\s*0\s*,\s*2\s*,\s*&gNvAccelStayLoaded\s*\)\s*\)\s*return\s*;",
                                   r"OSKextRetainKextWithLoadTag\s*\(\s*\(OSKextLoadTag\)\s*kmod_info\.id\s*\)\s*==\s*kOSReturnSuccess",
                                   r"OSCompareAndSwap\s*\(\s*2\s*,\s*1\s*,\s*&gNvAccelStayLoaded\s*\)"])
            and "OSKextReleaseKext" not in s):
        out.append("nvaccelStayLoaded: one reference on the kext, taken once, recorded, never given back")
    if not re.search(r"\b_stop\s*\([^)]*\)\s*\{\s*return\s+OSAddAtomic\s*\(\s*0\s*,\s*&gNvAccelStayLoaded\s*\)\s*\?\s*KERN_FAILURE\s*:\s*KERN_SUCCESS\s*;\s*\}", s):
        out.append("the module stop function refuses once the reference was wanted")
    start = block(s, "virtual bool start(IOService *provider) APPLE_KEXT_OVERRIDE {")
    m = re.search(r"if\s*\(\s*r\s*\)\s*\{\s*nvaccelStayLoaded\s*\(\s*\)\s*;", start)
    before = start[:m.start()] if m else ""
    if not m or first(before, OUTLIVES) >= 0 or first(start[m.end():], OUTLIVES) < 0:
        out.append("start: first thing once the family's start succeeded, before anything that outlives the accelerator")
    inst = block(s, "static void nvaccelInstallSysctl(void)\n{")
    if not (inst and order(inst, [r"if\s*\(\s*gSysctlInstalled\s*\)\s*return\s*;", r"\bnvaccelStayLoaded\s*\(\s*\)\s*;"])
            and 0 <= inst.find("nvaccelStayLoaded") < inst.find("sysctl_register_oid")):
        out.append("nvaccelInstallSysctl: the reference before its first registration")
    lay = block(l, "static void nvaccelLayoutSysctls(void)\n{")
    regs_outside = s.replace(inst, "").count("sysctl_register_oid(") + l.replace(lay, "").count("sysctl_register_oid(")
    calls = len(re.findall(r"\bnvaccelLayoutSysctls\s*\(\s*\)\s*;", s + l))
    if not inst or not lay or regs_outside or calls != 1 or not re.search(r"\bnvaccelLayoutSysctls\s*\(\s*\)\s*;", inst):
        out.append("every sysctl registration is reached only through nvaccelInstallSysctl")
    # probe's two device checks open a brace in each branch of an #ifdef: its body ends at the family's probe
    pa = s.find("virtual IOService *probe(IOService *provider, SInt32 *score) APPLE_KEXT_OVERRIDE {")
    pb = s.find("return IOGraphicsAccelerator2::probe(provider, score);", pa)
    probe = s[pa:pb] if 0 <= pa < pb else ""
    if not probe or first(probe, OUTLIVES + [r"\bnvaccelStayLoaded\s*\("]) >= 0:
        out.append("probe makes nothing that outlives it")
    return out


def main():
    src, layout = SRC.read_text(), LAYOUT.read_text()
    pr = problems(src, layout)
    for what in ["nvaccelStayLoaded: one reference", "the module stop function refuses", "start: first thing",
                 "nvaccelInstallSysctl: the reference", "every sysctl registration", "probe makes nothing"]:
        hit = [p for p in pr if p.startswith(what)]
        arm(what if not hit else hit[0], not hit)
    arm("the arms above are all there is to fail", len(pr) == len([p for p in pr if any(p.startswith(w) for w in [
        "nvaccelStayLoaded", "the module stop", "start:", "nvaccelInstallSysctl:", "every sysctl", "probe"])]), pr)

    def ctl(name, old, new, target, text=src, lay=layout):
        if text.count(old) != 1:
            arm(f"control ({name}): the text to alter is in the source once", False, old)
            return
        got = problems(text.replace(old, new), lay)
        arm(f"control ({name}) is caught", any(p.startswith(target) for p in got), got)

    call = "            nvaccelStayLoaded();   // before the first notifier, thread call or sysctl that outlives this accelerator\n"
    ctl("the call in start taken out", call, "", "start: first thing")
    moved = src.replace(call, "", 1).replace("            nvFenceAsk(locateNVRM());\n", "            nvFenceAsk(locateNVRM());\n" + call, 1)
    arm("control (the call in start moved after the fence question) is caught", moved.count(call) == 1
        and any(p.startswith("start: first thing") for p in problems(moved, layout)))
    ctl("the stop function lets the kext go", "{ return OSAddAtomic(0, &gNvAccelStayLoaded) ? KERN_FAILURE : KERN_SUCCESS; }",
        "{ return KERN_SUCCESS; }", "the module stop function refuses")
    ctl("the reference not recorded", " OSCompareAndSwap(2, 1, &gNvAccelStayLoaded);", ";", "nvaccelStayLoaded: one reference")
    ctl("taken every time", "    if (!OSCompareAndSwap(0, 2, &gNvAccelStayLoaded)) return;\n", "", "nvaccelStayLoaded: one reference")
    ctl("the call in nvaccelInstallSysctl taken out",
        "    nvaccelStayLoaded();   // registered sysctls are never taken back: the kext must not unload from here on\n", "",
        "nvaccelInstallSysctl: the reference")
    ctl("a sysctl registered outside nvaccelInstallSysctl", "        nvSurfPlanesReadOptOut();   // before the plugin can ask for a plane's VRAM\n",
        "        nvSurfPlanesReadOptOut();   // before the plugin can ask for a plane's VRAM\n        sysctl_register_oid(&sysctl__debug_nvaccel_igpu);\n",
        "every sysctl registration")
    ctl("probe registering a notifier", "        return IOGraphicsAccelerator2::probe(provider, score);\n",
        "        addMatchingNotification(gIOFirstPublishNotification, nullptr, &NVAccel::fbPublished, this, nullptr, 0);\n"
        "        return IOGraphicsAccelerator2::probe(provider, score);\n", "probe makes nothing")
    print(f"{len(FAILS)} failed" if FAILS else "all passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
