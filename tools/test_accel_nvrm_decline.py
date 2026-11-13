#!/usr/bin/env python3
"""NVAccel stops waiting for NVRM as soon as NVRM has declined the card, and only then.

NVAccel::start (kexts/NVRM/accel/nvrm-accel.cpp, the NVIDIA build) waits up to 30 s, with the PCI device's matching
held, for NVRM to publish nvrm-claimed. When NVRM declines the card early (the framebuffer refused this macOS's Apple
classes), NVRM::startGPU writes "nvrm-layout" = "declined: ..." on the PCI device and returns false without ever
claiming, so the accelerator used to sit out the whole 30 s. Asserted on the source text (comments blanked):
  - the loop still tests nvrm-claimed first and now also ends when the early decline is there;
  - after the loop the nvrm-claimed test still comes first; only when it fails is the early decline told apart from
    "never claimed in 30 s", each with its own line, and both return false;
  - the decline test reads the property with copyProperty and OSDynamicCast(OSString, ...), releases it, compares a
    prefix "declined:" character by character (so "declined late ...", written after the claim, never matches), and
    does not look at nvrm-dma;
  - all of it is in the non-NMX branch only;
  - NVRM writes "nvrm-layout" in exactly two places: the early decline, inside the refused branch and before the line
    that publishes nvrm-claimed, with a text that starts "declined:"; and the late one, after the claim, with a text
    that starts "declined late". So where NVRM starts normally the property does not exist and the loop is 2.9's.
Negative controls: copies without the decline test in the loop, with the decline told before the claim after the loop,
with a prefix that also matches the late text, with getProperty in place of copyProperty, keyed on nvrm-dma, or with a
third writer of the property in NVRM must fail.
Usage: test_accel_nvrm_decline.py [driver checkout]"""
import pathlib
import re
import sys

root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1])
LEX = re.compile(r"""//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])'""", re.S)


def no_comments(text):
    return LEX.sub(lambda m: m.group(0) if m.group(0)[0] in "\"'" else re.sub(r"[^\n]", " ", m.group(0)), text)


def block(code, start_pat):
    m = re.search(start_pat, code)
    if not m:
        return ""
    i = code.index("{", m.end() - 1)
    depth = 0
    for j in range(i, len(code)):
        if code[j] == "{":
            depth += 1
        elif code[j] == "}":
            depth -= 1
            if depth == 0:
                return code[i + 1:j]
    return ""


def order(text, patterns):
    pos = 0
    for p in patterns:
        m = re.compile(p).search(text, pos)
        if not m:
            return False
        pos = m.end()
    return True


CLAIMED = r'pci->getProperty\s*\(\s*"nvrm-claimed"\s*\)\s*!=\s*kOSBooleanTrue'


def arms(accel, nvrm):
    out = []
    arm = lambda name, ok: out.append((name, bool(ok)))
    a, n = no_comments(accel), no_comments(nvrm)

    start = block(a, r"virtual\s+bool\s+start\s*\(\s*IOService\s*\*\s*provider\s*\)\s*APPLE_KEXT_OVERRIDE\s*\{")
    i_else, i_end = start.find("#else"), start.find("#endif")
    nvidia = start[i_else:i_end] if 0 <= i_else < i_end else ""
    arm("the wait loop tests the claim first and also ends at NVRM's early decline",
        re.search(r"while\s*\(\s*" + CLAIMED + r"\s*&&\s*!\s*nvrmDeclinedEarly\s*\(\s*pci\s*,\s*nullptr\s*,\s*0\s*\)\s*&&\s*waited\s*<\s*300\s*\)\s*\{\s*IOSleep\s*\(\s*100\s*\)\s*;\s*waited\+\+\s*;\s*\}", nvidia) is not None)
    arm("after the loop the claim is tested first; only without it is the decline told apart from never claimed",
        order(nvidia, [r"while\s*\(", r"if\s*\(\s*" + CLAIMED + r"\s*\)\s*\{", r"if\s*\(\s*nvrmDeclinedEarly\s*\(\s*pci\s*,\s*declined\s*,\s*sizeof\s+declined\s*\)\s*\)",
                       r'ALOG\s*\(\s*"NVRM declined this card %04x:%04x \(nvrm-layout: %s\) - NOT starting the accelerator"', r"\belse\b",
                       r'ALOG\s*\(\s*"NVRM never claimed %04x:%04x in 30 s', r"return\s+false\s*;", r'ALOG\s*\(\s*"NVRM owns'])
        and len(re.findall(r"\bnvrmDeclinedEarly\s*\(", nvidia)) == 2 and "nvrmDeclinedEarly" not in start[:max(i_else, 0)])
    helper = block(a, r"static\s+bool\s+nvrmDeclinedEarly\s*\(\s*IOPCIDevice\s*\*\s*pci\s*,\s*char\s*\*\s*text\s*,\s*size_t\s+cap\s*\)\s*\{")
    hpos = a.find("static bool nvrmDeclinedEarly(")
    guard = a.rfind("#ifndef NMX_ACCEL", 0, hpos) if hpos >= 0 else -1
    arm("the decline test reads one string safely and matches only the early text",
        re.search(r'static\s+const\s+char\s+kEarly\[\]\s*=\s*"declined:"\s*;', helper) is not None
        and re.search(r'OSObject\s*\*\s*o\s*=\s*pci->copyProperty\s*\(\s*"nvrm-layout"\s*\)\s*;', helper) is not None
        and re.search(r"OSDynamicCast\s*\(\s*OSString\s*,\s*o\s*\)", helper) is not None and "OSSafeReleaseNULL(o);" in helper
        and re.search(r"for\s*\(\s*unsigned\s+i\s*=\s*0\s*;\s*early\s*&&\s*i\s*<\s*sizeof\s+kEarly\s*-\s*1\s*;\s*i\+\+\s*\)\s*early\s*=\s*t\[i\]\s*==\s*kEarly\[i\]\s*;", helper) is not None
        and "getProperty" not in helper and "nvrm-dma" not in helper and "nvrm-dma" not in nvidia
        and guard >= 0 and "#endif" not in a[guard:hpos] and "#if" not in a[guard + 17:hpos])

    writes = [(m.start(), m.group(1)) for m in re.finditer(r'setProperty\s*\(\s*"nvrm-layout"\s*,\s*"([^"]*)"', n)]
    claim = n.find('fPCI->setProperty("nvrm-claimed", kOSBooleanTrue);')
    refused = re.search(r"if\s*\(\s*gNvLayoutFb\s*==\s*kNvLayoutRefused\s*\)\s*\{\s*provider->setProperty\s*\(\s*\"nvrm-layout\"", n)
    arm("NVRM writes nvrm-layout only when it declines: early (before the claim, \"declined:\") and late (after it, \"declined late\")",
        len(writes) == 2 and claim > 0 and refused is not None
        and writes[0][0] < claim < writes[1][0] and writes[0][1].startswith("declined:") and writes[1][1].startswith("declined late")
        and not writes[1][1].startswith("declined:") and count_props(n) == 2)
    return out


def count_props(n):
    return len(re.findall(r'"nvrm-layout"', n))


def broken(src, old, new):
    assert src.count(old) == 1, "control text not found once: " + old
    return src.replace(old, new)


LOOP = 'while (pci->getProperty("nvrm-claimed") != kOSBooleanTrue && !nvrmDeclinedEarly(pci, nullptr, 0) && waited < 300)'
AFTER = '''            if (pci->getProperty("nvrm-claimed") != kOSBooleanTrue) {
                char declined[200];
'''
CONTROLS = [
    ("the loop without the decline test", "the wait loop tests", "accel",
     lambda s: broken(s, LOOP, 'while (pci->getProperty("nvrm-claimed") != kOSBooleanTrue && waited < 300)')),
    ("the loop testing the decline before the claim", "the wait loop tests", "accel",
     lambda s: broken(s, LOOP, 'while (!nvrmDeclinedEarly(pci, nullptr, 0) && pci->getProperty("nvrm-claimed") != kOSBooleanTrue && waited < 300)')),
    ("the decline told before the claim after the loop", "after the loop the claim", "accel",
     lambda s: broken(s, AFTER, '''            char declined[200];
            if (nvrmDeclinedEarly(pci, declined, sizeof declined)) { ALOG("NVRM declined this card %04x:%04x (nvrm-layout: %s) - NOT starting the accelerator", 0, 0, declined); return false; }
            if (pci->getProperty("nvrm-claimed") != kOSBooleanTrue) {
''')),
    ("a prefix that also matches the late decline", "the decline test reads", "accel",
     lambda s: broken(s, 'kEarly[] = "declined:";', 'kEarly[] = "declined";')),
    ("getProperty in place of copyProperty", "the decline test reads", "accel",
     lambda s: broken(s, 'OSObject *o = pci->copyProperty("nvrm-layout");', 'OSObject *o = pci->getProperty("nvrm-layout");')),
    ("keyed on nvrm-dma", "the decline test reads", "accel",
     lambda s: broken(s, 'OSObject *o = pci->copyProperty("nvrm-layout");', 'OSObject *o = pci->copyProperty("nvrm-dma");')),
    ("the property not released", "the decline test reads", "accel",
     lambda s: broken(s, "        OSSafeReleaseNULL(o);\n        return early;", "        return early;")),
    ("a third writer of nvrm-layout in NVRM", "NVRM writes nvrm-layout only", "nvrm",
     lambda s: broken(s, '    fPCI->setProperty("nvrm-claimed", kOSBooleanTrue);\n',
                      '    fPCI->setProperty("nvrm-claimed", kOSBooleanTrue);\n    fPCI->setProperty("nvrm-layout", "declined: x");\n')),
    ("the early decline written after the claim", "NVRM writes nvrm-layout only", "nvrm",
     lambda s: broken(s, "declined: the framebuffer refused", "declined late: the framebuffer refused")),
]


def main():
    accel = (root / "kexts/NVRM/accel/nvrm-accel.cpp").read_text()
    nvrm = (root / "kexts/NVRM/NVRM.cpp").read_text()
    fails = 0
    for name, ok in arms(accel, nvrm):
        print(("ok   " if ok else "FAIL ") + name)
        fails += 0 if ok else 1
    for name, piece, which, mutate in CONTROLS:
        try:
            res = arms(mutate(accel), nvrm) if which == "accel" else arms(accel, mutate(nvrm))
        except AssertionError as e:
            print("FAIL control '%s': %s" % (name, e))
            fails += 1
            continue
        hit = [a for a, ok in res if not ok and piece in a]
        print(("ok   " if hit else "FAIL ") + "control: %s is caught" % name)
        fails += 0 if hit else 1
    print("%d failed" % fails)
    return 1 if fails else 0


sys.exit(main())
