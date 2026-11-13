#!/usr/bin/env python3
"""Only the real WindowServer may delay the compositor's flips.

Two kernel entry points give a caller power over WindowServer's flips, copied frames and page-offs: a compositor-class
hold of the surface fence (nvfCallerIsWindowServer in kexts/NVRM/nvrm_fence.cpp, asked by OPEN with the compositor
flag) and 2.9's pending mark (nvSurfGpuEscape in kexts/NVRM/accel/nvrm-accel.cpp). Both used to test only the process
name, which is the executable's file name: any user could call a program "WindowServer" and hold every flip for up to
the fence's 2 s bound, again and again. Each test must now also require a system account, with the rule NVRM's client
table already uses (kauth_getuid() < 500: root and _windowserver), and must refuse before it answers yes.

What is asserted on the source text (comments and string contents blanked first):
  - each function refuses when kauth_getuid() >= 500, and that refusal comes before its only "yes";
  - the name test is still there (the uid alone would let every root daemon in);
  - NVAccel declares kauth_getuid with C linkage (it declares its BSD calls by hand) and nvrm_fence.cpp includes
    <sys/kauth.h> inside its extern "C" block; NVRM.cpp's client table uses the same threshold;
  - the only caller of nvfCallerIsWindowServer is the compositor branch of nvfOpen.
Negative controls: the same checks on copies with the uid test removed from either function, with the threshold
changed, with the name test removed, or with the declaration dropped must fail.
Usage: test_compositor_identity.py [driver checkout]"""
import pathlib
import re
import sys

root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1])
LEX = re.compile(r"""//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])'""", re.S)


def code_only(text, keep_strings=False):
    def sub(m):
        s = m.group(0)
        if s[0] in "\"'":
            return s if keep_strings else s[0] + re.sub(r"[^\n]", " ", s[1:-1]) + s[-1]
        return re.sub(r"[^\n]", " ", s)
    return LEX.sub(sub, text)


def body(code, name):
    m = re.search(r"\b" + name + r"\s*\([^;{)]*\)\s*\{", code)
    if not m:
        return ""
    depth = 0
    for i in range(m.end() - 1, len(code)):
        if code[i] == "{":
            depth += 1
        elif code[i] == "}":
            depth -= 1
            if depth == 0:
                return code[m.end():i]
    return ""


UID_REFUSE = r"kauth_getuid\s*\(\s*\)\s*>=\s*500"


def arms(fence, accel, nvrm):
    out = []
    arm = lambda name, ok: out.append((name, bool(ok)))
    fc, ac, nc = code_only(fence), code_only(accel), code_only(nvrm)
    fs, as_ = code_only(fence, True), code_only(accel, True)

    f = body(fc, "nvfCallerIsWindowServer")
    m_uid = re.search(r"if\s*\(\s*" + UID_REFUSE + r"\s*\)\s*return\s+false\s*;", f)
    m_yes = re.search(r"return\s+true\s*;", f)
    arm("fence: a compositor hold needs a system account, refused before the only yes",
        m_uid is not None and m_yes is not None and m_uid.start() < m_yes.start() and len(re.findall(r"return\s+true\s*;", f)) == 1)
    arm("fence: the process name is still compared with WindowServer",
        re.search(r"\bproc_selfname\s*\(\s*name\s*,", f) is not None
        and re.search(r'want\[\]\s*=\s*"WindowServer"\s*;', body(fs, "nvfCallerIsWindowServer")) is not None)
    callers = [m.start() for m in re.finditer(r"\bnvfCallerIsWindowServer\s*\(\s*(?:void\s*)?\)", fc)]
    arm("fence: only OPEN's compositor branch asks it",
        len(callers) == 2
        and re.search(r"if\s*\(\s*\(\s*flags\s*&\s*NVRM_FENCE_F_COMPOSITOR\s*\)\s*&&\s*!\s*nvfCallerIsWindowServer\s*\(\s*\)\s*\)", body(fc, "nvfOpen")) is not None)
    ext = re.search(r'extern\s+"C"\s*\{([^}]*)\}', fs)
    arm("fence: <sys/kauth.h> is included with C linkage",
        ext is not None and re.search(r"#\s*include\s*<sys/kauth\.h>", ext.group(1)) is not None)

    g = body(ac, "nvSurfGpuEscape")
    gs = body(as_, "nvSurfGpuEscape")
    m_ref = re.search(r'if\s*\(\s*strcmp\s*\(\s*nm\s*,\s*"WindowServer"\s*\)\s*\|\|\s*' + UID_REFUSE + r"\s*\)\s*return\s+NVRM_SS_OFF\s*;", gs)
    m_mark = re.search(r"\bnvSurfMark\s*\(", g)
    arm("NVAccel: a pending mark needs WindowServer's name and a system account, refused before the mark is taken",
        m_ref is not None and m_mark is not None and m_ref.start() < m_mark.start() and len(re.findall(r"\bnvSurfMark\s*\(", g)) == 1)
    decl = [m for m in re.finditer(r'extern\s+"C"\s*\{([^}]*)\}', as_) if re.search(r"\bproc_selfname\s*\(", m.group(1))]
    arm("NVAccel: kauth_getuid is declared with C linkage beside proc_selfname",
        len(decl) == 1 and re.search(r"\buid_t\s+kauth_getuid\s*\(\s*void\s*\)\s*;", decl[0].group(1)) is not None)

    arm("the threshold is the one NVRM's client table uses for system accounts",
        re.search(r"\bsystemCaller\s*=\s*kauth_getuid\s*\(\s*\)\s*<\s*500\s*;", nc) is not None)
    return out


def broken(src, old, new):
    assert src.count(old) == 1, "control text not found once: " + old
    return src.replace(old, new)


CONTROLS = [
    ("the uid test removed from the fence", "fence: a compositor hold needs", "fence",
     lambda s: broken(s, "    if (kauth_getuid() >= 500) return false;\n", "")),
    ("the fence's uid test moved after its yes", "fence: a compositor hold needs", "fence",
     lambda s: broken(broken(s, "    if (kauth_getuid() >= 500) return false;\n", ""), "return false;\n    return true;\n}",
                      "return false;\n    return true;\n    if (kauth_getuid() >= 500) return false;\n}")),
    ("the fence's threshold raised", "fence: a compositor hold needs", "fence",
     lambda s: broken(s, "kauth_getuid() >= 500) return false;", "kauth_getuid() >= 501) return false;")),
    ("the fence's name test removed", "fence: the process name", "fence",
     lambda s: broken(s, "    proc_selfname(name, (int)sizeof(name));\n", "")),
    ("the fence's include dropped", "fence: <sys/kauth.h>", "fence",
     lambda s: broken(s, "#include <sys/kauth.h>\n", "")),
    ("the uid test removed from NVAccel's mark", "NVAccel: a pending mark needs", "accel",
     lambda s: broken(s, 'if (strcmp(nm, "WindowServer") || kauth_getuid() >= 500) return NVRM_SS_OFF;', 'if (strcmp(nm, "WindowServer")) return NVRM_SS_OFF;')),
    ("NVAccel's test made an and", "NVAccel: a pending mark needs", "accel",
     lambda s: broken(s, 'if (strcmp(nm, "WindowServer") || kauth_getuid() >= 500)', 'if (strcmp(nm, "WindowServer") && kauth_getuid() >= 500)')),
    ("NVAccel's declaration dropped", "NVAccel: kauth_getuid is declared", "accel",
     lambda s: broken(s, "uid_t kauth_getuid(void);\n", "")),
]


def main():
    fence = (root / "kexts/NVRM/nvrm_fence.cpp").read_text()
    accel = (root / "kexts/NVRM/accel/nvrm-accel.cpp").read_text()
    nvrm = (root / "kexts/NVRM/NVRM.cpp").read_text()
    fails = 0
    for name, ok in arms(fence, accel, nvrm):
        print(("ok   " if ok else "FAIL ") + name)
        fails += 0 if ok else 1
    for name, piece, which, mutate in CONTROLS:
        try:
            res = arms(mutate(fence), accel, nvrm) if which == "fence" else arms(fence, mutate(accel), nvrm)
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
