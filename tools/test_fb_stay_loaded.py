#!/usr/bin/env python3
"""NVRMFB.kext and NVRMAGDC.kext stay loaded once they have made what outlives their objects.

The kernel unloads a kext with IOService classes by itself once no object of its classes exists and nothing else
holds a reference on it, and `kmutil unload` asks for the same. NVRMFB never takes back its sysctls, the NVKMS
display device (NVRM.kext keeps it for the boot, with this kext's event callback in it), its hot-plug thread calls
and the first framebuffer it retains; NVRMAGDC's worker can still be running after the loader's stop. So each kext
holds one reference on itself from the point it makes the first of those, and refuses in its module stop function.
What this checks, on the source text with comments left out:
  - each kext's stay-loaded function asks once (compare-and-swap 0 -> 2 first), takes the reference on its own load
    tag (kmod_info.id) and records success (2 -> 1); nothing in the kext gives a reference back;
  - each module stop function refuses (KERN_FAILURE) as soon as the reference was wanted;
  - NVRMNVDAFramebuffer::start takes it after its three declines (driver off, layout, nvfb) and before anything that
    outlives the object (thread calls, the retained first framebuffer, the remembered nub parent, kapiInit, which
    allocates the NVKMS device, sysctls, the guard clear); NVRMFBClaim::start after IOService::start and before its
    sysctls; nvrmfbInstallSysctl and fbGuardCleared before their first registration;
  - every sysctl registration, the NVKMS device allocation and the hot-plug thread calls sit only in functions that
    come after the reference, and no probe takes it (a probe that declines must leave a kext that unloads);
  - NVRMAGDC: the loader's start takes it after its decline and IOService::start, before it registers the sysctls,
    which are the only way to start the worker; the worker's thread call is allocated only in the sysctl handler.
Each arm has a negative control: a copy of the text with the fix taken out (or moved) must fail it.
Usage: test_fb_stay_loaded.py [driver checkout]"""
import pathlib
import re
import sys

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1])
FILES = {"fb": "kexts/NVRMFB/fb/nvrm-fb.cpp", "fbl": "kexts/NVRMFB/fb/nvrmfb_layout.h",
         "agdc": "kexts/NVRMAGDC/nvrm-agdc.cpp"}
LEX = re.compile(r"""//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])'""", re.S)


def no_comments(text):
    """Comments blanked (strings kept); offsets unchanged."""
    return LEX.sub(lambda m: m.group(0) if m.group(0)[0] in "\"'" else re.sub(r"[^\n]", " ", m.group(0)), text)


def body(code, name):
    """The text between the braces of the definition of `name` (a function, or an in-class method), or ''."""
    m = re.search(r"(?<![\w:])" + re.escape(name) + r"\s*\([^(){};]*\)\s*(?:const\s*)?(?:override\s*)?\{", code)
    if not m:
        return ""
    depth, i = 1, m.end()
    while i < len(code) and depth:
        depth += {"{": 1, "}": -1}.get(code[i], 0)
        i += 1
    return code[m.end():i - 1]


def klass(code, name):
    """The text of `class name ... };`, or ''."""
    a = code.find("class " + name)
    if a < 0:
        return ""
    b = code.find("\n};\n", a)
    return code[a:b if b >= 0 else len(code)]


def first(text, pats):
    """Offset of the first match of any pattern in text, or -1."""
    hits = [m.start() for p in pats for m in [re.search(p, text)] if m]
    return min(hits) if hits else -1


def order(text, pats):
    """Each pattern found, in this order."""
    at = 0
    for p in pats:
        m = re.compile(p).search(text, at)
        if not m:
            return False
        at = m.end()
    return True


def stay_shape(code, fn, flag):
    f = body(code, fn)
    return (order(f, [r"if\s*\(\s*!\s*OSCompareAndSwap\s*\(\s*0\s*,\s*2\s*,\s*&" + flag + r"\s*\)\s*\)\s*return\s*;",
                      r"OSKextRetainKextWithLoadTag\s*\(\s*\(\s*OSKextLoadTag\s*\)\s*kmod_info\.id\s*\)",
                      r"==\s*kOSReturnSuccess\s*\)\s*OSCompareAndSwap\s*\(\s*2\s*,\s*1\s*,\s*&" + flag + r"\s*\)"])
            and "OSKextReleaseKextWithLoadTag" not in code
            and len(re.findall(r"\bOSKextRetainKextWithLoadTag\s*\(", code)) == 1
            and re.search(r"#\s*include\s*<libkern/OSKextLib\.h>", code) is not None)


def stop_refuses(code, flag):
    return re.search(r"\b_stop\s*\([^)]*\)\s*\{\s*return\s+OSAddAtomic\s*\(\s*0\s*,\s*&" + flag
                     + r"\s*\)\s*\?\s*KERN_FAILURE\s*:\s*KERN_SUCCESS\s*;\s*\}", code) is not None \
        and len(re.findall(r"\b_stop\s*\([^)]*\)\s*\{", code)) == 1


# what outlives an NVRMFB object, as it is made in the framebuffer's start
FB_OUTLIVES = [r"\bnvrmfbCall\s*\(", r"\bthread_call_allocate", r"\bgFB\s*=", r"\bgHotplug\s*=", r"\bgHotplugPoll\s*=",
               r"\bgNubParent\s*=", r"\bkapiInit\s*\(", r"\bsysctl_register_oid\s*\(", r"\bfbGuardCleared\s*\(",
               r"\bnvrmfbInstallSysctl\s*\(", r"\bnvrmfbLayoutSysctls\s*\(", r"\bretain\s*\(\s*\)"]
DECLINES = [r"if\s*\(\s*nvrm_driver_off\s*\(\s*\)\s*\)\s*return\s+false\s*;",
            r"if\s*\(\s*!\s*nvrmfbLayoutAllows\s*\([^;]*\)\s*\)\s*return\s+false\s*;",
            r"PE_parse_boot_argn\s*\(\s*\"nvfb\"[\s\S]*?return\s+false\s*;\s*\}\s*\}"]
CALL = r"\bnvrmfbStayLoaded\s*\(\s*\)\s*;"


def fb_start_ok(fb):
    s = body(fb, "NVRMNVDAFramebuffer::start")
    m = re.search(CALL, s)
    if not s or not m:
        return False
    before, after = s[:m.start()], s[m.end():]
    return (order(before, DECLINES) and first(before, FB_OUTLIVES) < 0
            and first(after, [r"\bkapiInit\s*\(", r"\bgHotplug\s*=", r"\bgFB\s*="]) >= 0)


def fb_claim_ok(fb):
    claim = klass(fb, "NVRMFBClaim")
    s = body(claim, "start")
    probe = body(claim, "probe")
    return (bool(s) and order(s, [r"if\s*\(\s*nvrm_driver_off\s*\(\s*\)\s*\)\s*return\s+false\s*;",
                                  r"if\s*\(\s*!\s*IOService::start\s*\([^;]*\)\s*\)\s*return\s+false\s*;", CALL,
                                  r"\bnvrmfbLayoutSysctls\s*\(\s*\)\s*;"])
            and first(s[:re.search(CALL, s).start()] if re.search(CALL, s) else s, FB_OUTLIVES) < 0
            and bool(probe) and "nvrmfbStayLoaded" not in probe
            and "nvrmfbStayLoaded" not in body(klass(fb, "NVRMBootFB"), "probe"))


def fb_registrations_ok(fb, fbl):
    """Every registration (and the device, the hot-plug calls) sits where the reference comes first."""
    inst, guard = body(fb, "nvrmfbInstallSysctl"), body(fb, "fbGuardCleared")
    if not (order(inst, [CALL, r"\bsysctl_register_oid\s*\("]) and order(guard, [CALL, r"\bsysctl_register_oid\s*\("])):
        return False
    if first(inst[:re.search(CALL, inst).start()], [r"\bsysctl_register_oid\s*\("]) >= 0:
        return False
    if first(guard[:re.search(CALL, guard).start()], [r"\bsysctl_register_oid\s*\("]) >= 0:
        return False
    layout = body(fbl, "nvrmfbLayoutSysctls")
    # all registrations: the two functions above and the layout header's own
    total = len(re.findall(r"\bsysctl_register_oid\s*\(", fb)) + len(re.findall(r"\bsysctl_register_oid\s*\(", fbl))
    inside = sum(len(re.findall(r"\bsysctl_register_oid\s*\(", t)) for t in (inst, guard, layout))
    if total != inside or not layout:
        return False
    # the layout sysctls are registered from the claim's start and the guard clear only, each after the reference
    callers = len(re.findall(r"\bnvrmfbLayoutSysctls\s*\(\s*\)\s*;", fb))
    claim_s = body(klass(fb, "NVRMFBClaim"), "start")
    if callers != 2 or not order(claim_s, [CALL, r"\bnvrmfbLayoutSysctls\s*\("]) or not order(guard, [CALL, r"\bnvrmfbLayoutSysctls\s*\("]):
        return False
    # the NVKMS device is allocated only in kapiInit, which only the framebuffer's start calls (after the reference)
    alloc = [m.start() for m in re.finditer(r"->\s*allocateDevice\s*\(", fb)]
    kinit = body(fb, "NVRMNVDAFramebuffer::kapiInit")
    if len(alloc) != 1 or not re.search(r"->\s*allocateDevice\s*\(", kinit):
        return False
    # calls only: the declaration in the class is "bool kapiInit();"
    if len(re.findall(r"(?<!bool )\bkapiInit\s*\(\s*\)\s*;", fb)) != 1 or not re.search(r"\bkapiInit\s*\(\s*\)\s*;", body(fb, "NVRMNVDAFramebuffer::start")):
        return False
    # the global thread calls are made only in the framebuffer's start
    start = body(fb, "NVRMNVDAFramebuffer::start")
    for g in ("gHotplug", "gHotplugPoll"):
        sets = re.findall(r"\b" + g + r"\s*=\s*nvrmfbCall\s*\(", fb)
        if len(sets) != 1 or not re.search(r"\b" + g + r"\s*=\s*nvrmfbCall\s*\(", start):
            return False
    return True


def agdc_ok(ag):
    s = body(ag, "NVDAGDCLoader::start")
    m = re.search(r"\bnvdaAgdcStayLoaded\s*\(\s*\)\s*;", s)
    if not s or not m:
        return False
    return (order(s, [r"if\s*\(\s*nvrm_driver_off\s*\(\s*\)\s*\)\s*return\s+false\s*;",
                      r"if\s*\(\s*!\s*IOService::start\s*\([^;]*\)\s*\)\s*return\s+false\s*;",
                      r"\bnvdaAgdcStayLoaded\s*\(\s*\)\s*;", r"\bnvdaAgdcInstallSysctl\s*\(\s*\)\s*;"])
            and first(s[:m.start()], [r"\bnvdaAgdcInstallSysctl\s*\(", r"\bsysctl_register_oid\s*\(", r"\bthread_call_"]) < 0
            and len(re.findall(r"\bnvdaAgdcInstallSysctl\s*\(\s*\)\s*;", ag)) == 1
            and len(re.findall(r"\bthread_call_allocate\w*\s*\(", ag)) == 1
            and re.search(r"\bthread_call_allocate\s*\(", ag[ag.find("nvrmfb_agdc_sysctl SYSCTL_HANDLER_ARGS"):
                                                            ag.find("SYSCTL_PROC(_debug, OID_AUTO, nvrmfb_agdc,")]) is not None)


def check(t):
    fb, fbl, ag = (no_comments(t[k]) for k in ("fb", "fbl", "agdc"))
    return {
        "NVRMFB asks once for a reference on its own load tag and never gives it back":
            stay_shape(fb, "nvrmfbStayLoaded", "gNvrmfbStayLoaded"),
        "NVRMFB's module stop function refuses once the reference was wanted":
            stop_refuses(fb, "gNvrmfbStayLoaded"),
        "the framebuffer's start takes it after its declines and before anything that outlives the object":
            fb_start_ok(fb),
        "the claim takes it after IOService::start and before its sysctls; no probe takes it":
            fb_claim_ok(fb),
        "every NVRMFB registration, the NVKMS device and the hot-plug calls come after the reference":
            fb_registrations_ok(fb, fbl),
        "NVRMAGDC asks once for a reference on its own load tag and never gives it back":
            stay_shape(ag, "nvdaAgdcStayLoaded", "gAgdcStayLoaded"),
        "NVRMAGDC's module stop function refuses once the reference was wanted":
            stop_refuses(ag, "gAgdcStayLoaded"),
        "the AGDC loader takes it after its decline, before the sysctls that start the worker":
            agdc_ok(ag),
    }


def broken(t, key, old, new):
    """A copy of the texts with one change; the change must apply exactly once (else the control is void)."""
    if t[key].count(old) != 1:
        raise SystemExit(f"control text not found exactly once in {FILES[key]}: {old!r}")
    c = dict(t)
    c[key] = t[key].replace(old, new)
    return c


FB_START_CALL = "    nvrmfbStayLoaded();\n    fFlipCacheLock = IOSimpleLockAlloc();"
CONTROLS = [
    ("a framebuffer start without the reference", "the framebuffer's start takes it",
     lambda t: broken(t, "fb", FB_START_CALL, "    fFlipCacheLock = IOSimpleLockAlloc();")),
    ("the reference taken after kapiInit", "the framebuffer's start takes it",
     lambda t: broken(broken(t, "fb", FB_START_CALL, "    fFlipCacheLock = IOSimpleLockAlloc();"), "fb",
                      "    IORecursiveLockLock(sl); bool kapiOk = kapiInit();",
                      "    IORecursiveLockLock(sl); bool kapiOk = kapiInit(); nvrmfbStayLoaded();")),
    ("the reference taken before the driver-off decline", "the framebuffer's start takes it",
     lambda t: broken(broken(t, "fb", FB_START_CALL, "    fFlipCacheLock = IOSimpleLockAlloc();"), "fb",
                      "    { if (nvrm_driver_off()) return false; }\n    if (!nvrmfbLayoutAllows(provider, \"NVRMNVDAFramebuffer\")) return false;",
                      "    nvrmfbStayLoaded();\n    { if (nvrm_driver_off()) return false; }\n    if (!nvrmfbLayoutAllows(provider, \"NVRMNVDAFramebuffer\")) return false;")),
    ("a hot-plug thread call made before the reference", "the framebuffer's start takes it",
     lambda t: broken(t, "fb", FB_START_CALL, "    if (!gHotplug) gHotplug = nvrmfbCall(hotplugFire, nullptr);\n" + FB_START_CALL)),
    ("a claim that registers before it holds the kext", "the claim takes it",
     lambda t: broken(t, "fb", "        nvrmfbStayLoaded();\n        nvrmfbLayoutSysctls();", "        nvrmfbLayoutSysctls();\n        nvrmfbStayLoaded();")),
    ("a claim whose probe takes the reference", "the claim takes it",
     lambda t: broken(t, "fb", "        if (!nvrmfbLayoutAllows(provider, \"NVRMFBClaim\")) return NULL;",
                      "        nvrmfbStayLoaded();\n        if (!nvrmfbLayoutAllows(provider, \"NVRMFBClaim\")) return NULL;")),
    ("a sysctl registration added to a probe", "every NVRMFB registration",
     lambda t: broken(t, "fb", "        if (!nvrm_driver_off() || !provider) return NULL;",
                      "        sysctl_register_oid(&sysctl__debug_nvrmfb_flip_n);\n        if (!nvrm_driver_off() || !provider) return NULL;")),
    ("the first-grant registration without the reference", "every NVRMFB registration",
     lambda t: broken(t, "fb", "    if (gFbSysctl) return; gFbSysctl = true;\n    nvrmfbStayLoaded();", "    if (gFbSysctl) return; gFbSysctl = true;")),
    ("the guard clear registering before the reference", "every NVRMFB registration",
     lambda t: broken(t, "fb", "    nvrmfbStayLoaded();         // held already (the framebuffer's start); never unregistered, so the kext must stay\n    if (OSCompareAndSwap(0, 1, &once))",
                      "    if (OSCompareAndSwap(0, 1, &once))")),
    ("the NVKMS device allocated outside kapiInit", "every NVRMFB registration",
     lambda t: broken(t, "fb", "    if (!sl) { FBLOG(\"start lock alloc failed -- refusing to start\"); return false; }",
                      "    if (!sl) { FBLOG(\"start lock alloc failed -- refusing to start\"); return false; }\n    if (!gDev) gDev = fKms->allocateDevice(nullptr);")),
    ("NVRMFB's reference given back", "NVRMFB asks once",
     lambda t: broken(t, "fb", "    if (r == kOSReturnSuccess) OSCompareAndSwap(2, 1, &gNvrmfbStayLoaded);",
                      "    if (r == kOSReturnSuccess) { OSCompareAndSwap(2, 1, &gNvrmfbStayLoaded); OSKextReleaseKextWithLoadTag((OSKextLoadTag)kmod_info.id); }")),
    ("NVRMFB taking a reference at every call", "NVRMFB asks once",
     lambda t: broken(t, "fb", "    if (!OSCompareAndSwap(0, 2, &gNvrmfbStayLoaded)) return;\n", "")),
    ("NVRMFB's stop function that always agrees", "NVRMFB's module stop function",
     lambda t: broken(t, "fb", "{ return OSAddAtomic(0, &gNvrmfbStayLoaded) ? KERN_FAILURE : KERN_SUCCESS; }", "{ return KERN_SUCCESS; }")),
    ("an AGDC loader without the reference", "the AGDC loader takes it",
     lambda t: broken(t, "agdc", "    nvdaAgdcStayLoaded();   // before the sysctls", "    //")),
    ("an AGDC loader that registers first", "the AGDC loader takes it",
     lambda t: broken(broken(t, "agdc", "    nvdaAgdcStayLoaded();   // before the sysctls", "    //"), "agdc",
                      "    nvdaAgdcInstallSysctl(); FBLOG(", "    nvdaAgdcInstallSysctl(); nvdaAgdcStayLoaded(); FBLOG(")),
    ("an AGDC loader that takes it before its decline", "the AGDC loader takes it",
     lambda t: broken(broken(t, "agdc", "    nvdaAgdcStayLoaded();   // before the sysctls", "    //"), "agdc",
                      "    { if (nvrm_driver_off()) return false; }\n    if (!IOService::start(provider)) return false;",
                      "    nvdaAgdcStayLoaded();\n    { if (nvrm_driver_off()) return false; }\n    if (!IOService::start(provider)) return false;")),
    ("the AGDC worker allocated at the loader's start", "the AGDC loader takes it",
     lambda t: broken(t, "agdc", "    { if (nvrm_driver_off()) return false; }\n    if (!IOService::start(provider)) return false;",
                      "    { if (nvrm_driver_off()) return false; }\n    gAgdcCall = thread_call_allocate(nvdaAgdcWork, nullptr);\n    if (!IOService::start(provider)) return false;")),
    ("NVRMAGDC's reference given back", "NVRMAGDC asks once",
     lambda t: broken(t, "agdc", "    if (r == kOSReturnSuccess) OSCompareAndSwap(2, 1, &gAgdcStayLoaded);",
                      "    if (r == kOSReturnSuccess) { OSCompareAndSwap(2, 1, &gAgdcStayLoaded); OSKextReleaseKextWithLoadTag((OSKextLoadTag)kmod_info.id); }")),
    ("NVRMAGDC's stop function that always agrees", "NVRMAGDC's module stop function",
     lambda t: broken(t, "agdc", "{ return OSAddAtomic(0, &gAgdcStayLoaded) ? KERN_FAILURE : KERN_SUCCESS; }", "{ return KERN_SUCCESS; }")),
]


def main():
    texts = {k: (ROOT / v).read_text() for k, v in FILES.items()}
    fails = 0
    for name, ok in check(texts).items():
        print(("ok   " if ok else "FAIL ") + name)
        fails += not ok
    for name, prefix, make in CONTROLS:
        res = check(make(texts))
        target = [k for k in res if k.startswith(prefix)]
        caught = len(target) == 1 and not res[target[0]]
        print(("ok   " if caught else "FAIL ") + "control: " + name + " is caught")
        fails += not caught
    print("PASS" if not fails else f"FAILED ({fails})")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
