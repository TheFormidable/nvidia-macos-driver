#!/usr/bin/env python3
"""Without the idle power-down monitor, its gate is closed and it has no GPU.

NVRM::rtd3Arm (kexts/NVRM/NVRM.cpp) publishes the first GPU that starts in gNvRtd3Gpu and opens gNvRtd3Gate before it
asks nv_rtd3_arm (kexts/NVRM/nv_rtd3.cpp) to arm the monitor. On the default machine (no nvrtd3=1), and with nvrtd3=1
but no nvrtd3res, nv_rtd3_arm arms nothing, and the gate used to stay open with the GPU published for the whole boot,
although no monitor call could ever come. nv_rtd3_arm now says whether it armed (int: 1 armed, 0 not), and rtd3Arm,
when it did not, closes the gate and takes the GPU back, in that order, the order stop()'s rtd3Detach uses.

Asserted on the source text (comments blanked):
  - nv_rtd3_arm is defined and declared returning int; it returns 1 only after the thread call was entered, and 0 on
    every early return (not enabled, no resource, armed before, no thread call);
  - rtd3Arm calls nv_rtd3_arm once, tests its result, and on 0 closes the gate first and stores NULL second, after
    the store of this GPU and the opening of the gate;
  - rtd3Detach closes the gate before it takes the GPU back (the order copied here).
Negative controls: copies with the close removed, the NULL store removed, the two swapped, the result not tested,
the definition still void, or an early return that answers 1 must fail.
Usage: test_rtd3_arm_declined.py [driver checkout]"""
import pathlib
import re
import sys

root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1])
LEX = re.compile(r"""//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])'""", re.S)


def code_only(text):
    def sub(m):
        s = m.group(0)
        if s[0] in "\"'":
            return s   # kept: extern "C" is matched, and no string here holds code-like text
        return re.sub(r"[^\n]", " ", s)
    return LEX.sub(sub, text)


def body(code, head):
    m = re.search(head, code)
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


CLOSE = r"\bnv_gate_close\s*\(\s*&gNvRtd3Gate\s*\)\s*;"
TAKE = r"__atomic_store_n\s*\(\s*&gNvRtd3Gpu\s*,\s*\(\s*NVRM\s*\*\s*\)\s*NULL\s*,\s*__ATOMIC_SEQ_CST\s*\)\s*;"


def arms(nvrm, rtd3):
    out = []
    arm = lambda name, ok: out.append((name, bool(ok)))
    n, r = code_only(nvrm), code_only(rtd3)

    d = body(r, r'extern\s+"C"\s+int\s+nv_rtd3_arm\s*\(\s*const\s+struct\s+nv_rtd3_ops\s*\*\s*ops\s*\)\s*\{')
    rets = re.findall(r"\breturn\b[^;]*;", d)
    arm("nv_rtd3_arm returns int: 1 only after the poll is entered, 0 on every early return",
        d != "" and [" ".join(x.split()) for x in rets] == ["return 0;", "return 0;", "return 1;"]
        and order(d, [r"if\s*\(\s*!\s*g_rtd3\.enabled\s*\|\|\s*!\s*g_rtd3\.have_resource\s*\|\|\s*g_call\s*\)\s*return\s+0\s*;",
                      r"if\s*\(\s*!\s*g_call\s*\)\s*return\s+0\s*;", r"\bthread_call_enter_delayed\s*\(", r"return\s+1\s*;\s*$"]))
    arm("NVRM declares nv_rtd3_arm returning int, once",
        len(re.findall(r'extern\s+"C"\s+int\s+nv_rtd3_arm\s*\(\s*const\s+struct\s+nv_rtd3_ops\s*\*\s*ops\s*\)\s*;', n)) == 1
        and not re.search(r"\bvoid\s+nv_rtd3_arm\s*\(", n + r))

    a = body(n, r"\bvoid\s+NVRM::rtd3Arm\s*\(\s*\)\s*\{")
    arm("rtd3Arm: when nothing was armed, the gate is closed and then the GPU taken back",
        order(a, [r"__atomic_store_n\s*\(\s*&gNvRtd3Gpu\s*,\s*this\s*,", r"\bnv_gate_open\s*\(\s*&gNvRtd3Gate\s*\)\s*;",
                  r"if\s*\(\s*!\s*nv_rtd3_arm\s*\(\s*&ops\s*\)\s*\)\s*\{\s*" + CLOSE + r"\s*" + TAKE + r"\s*\}"])
        and len(re.findall(r"\bnv_rtd3_arm\s*\(", a)) == 1)
    det = body(n, r"\bvoid\s+NVRM::rtd3Detach\s*\(\s*\)\s*\{")
    arm("rtd3Detach uses the same order: close, then take the GPU back",
        order(det, [CLOSE, TAKE]))
    return out


def broken(src, old, new):
    assert src.count(old) == 1, "control text not found once: " + old
    return src.replace(old, new)


CLOSE_T, TAKE_T = "nv_gate_close(&gNvRtd3Gate);", "__atomic_store_n(&gNvRtd3Gpu, (NVRM *)NULL, __ATOMIC_SEQ_CST);"
CL = "    if (!nv_rtd3_arm(&ops)) { " + CLOSE_T + " " + TAKE_T + " }\n"
CONTROLS = [
    ("the close removed", "rtd3Arm: when nothing", "nvrm", lambda s: broken(s, CL, "    if (!nv_rtd3_arm(&ops)) { " + TAKE_T + " }\n")),
    ("the NULL store removed", "rtd3Arm: when nothing", "nvrm", lambda s: broken(s, CL, "    if (!nv_rtd3_arm(&ops)) { " + CLOSE_T + " }\n")),
    ("the two swapped", "rtd3Arm: when nothing", "nvrm", lambda s: broken(s, CL, "    if (!nv_rtd3_arm(&ops)) { " + TAKE_T + " " + CLOSE_T + " }\n")),
    ("the result not tested", "rtd3Arm: when nothing", "nvrm", lambda s: broken(s, CL, "    nv_rtd3_arm(&ops); { " + CLOSE_T + " " + TAKE_T + " }\n")),
    ("the declaration still void", "NVRM declares", "nvrm",
     lambda s: broken(s, 'extern "C" int  nv_rtd3_arm(', 'extern "C" void nv_rtd3_arm(')),
    ("the definition still void", "nv_rtd3_arm returns int", "rtd3",
     lambda s: broken(s, 'extern "C" int nv_rtd3_arm(', 'extern "C" void nv_rtd3_arm(')),
    ("a failed thread call answers armed", "nv_rtd3_arm returns int", "rtd3",
     lambda s: broken(s, "    if (!g_call) return 0;\n", "    if (!g_call) return 1;\n")),
    ("the early return answers armed", "nv_rtd3_arm returns int", "rtd3",
     lambda s: broken(s, "!g_rtd3.have_resource || g_call) return 0;", "!g_rtd3.have_resource || g_call) return 1;")),
]


def main():
    nvrm = (root / "kexts/NVRM/NVRM.cpp").read_text()
    rtd3 = (root / "kexts/NVRM/nv_rtd3.cpp").read_text()
    fails = 0
    for name, ok in arms(nvrm, rtd3):
        print(("ok   " if ok else "FAIL ") + name)
        fails += 0 if ok else 1
    for name, piece, which, mutate in CONTROLS:
        try:
            res = arms(mutate(nvrm), rtd3) if which == "nvrm" else arms(nvrm, mutate(rtd3))
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
