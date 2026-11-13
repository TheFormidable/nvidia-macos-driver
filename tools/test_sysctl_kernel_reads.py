#!/usr/bin/env python3
"""Every sysctl a kext reads or writes from inside the kernel is declared so that the kernel may.

A kext that calls sysctlbyname() reaches xnu's kernel_sysctlbyname, and sysctl_root() answers EPERM to a kernel
caller for an OID declared without CTLFLAG_KERN (bsd/kern/kern_newsysctl.c). Nothing says so at build time and the
call fails silently. NVRM talks to its sibling kexts this way during auto-go: it reads the heads NVAccel published,
arms the accelerator, and asks NVRMFB whether the display came up (debug.nvrmfb_guard_cleared). That last OID was
declared without the flag since 2.9, so NVRM's read of it always failed: after a settle wait a start was counted
again even when the display was already up, and since 3.0, when the accelerator refuses this macOS's classes, auto-go
waited its whole 20 s for a display that was up and then wrote "display not reported up" into the log and the registry.

What is checked: every name a kext's source hands to sysctlbyname (directly, or through NVRM's nvrmSysR / nvrmSysW)
under debug.* has exactly one declaration under kexts/, and that declaration's access flags name CTLFLAG_KERN.
Names outside debug.* are Apple's own (kern.osversion, hw.memsize: both CTLFLAG_KERN in xnu) and are only listed.
A name built at run time cannot be read from the text and is refused.

The arms at the end show that each check fires, on made-up sources.
Usage: test_sysctl_kernel_reads.py [driver checkout]"""
import pathlib
import re
import sys

ROOT = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1]
SUFFIXES = {".c", ".cc", ".cpp", ".h", ".hpp", ".inc", ".m", ".mm"}
# a call with a string literal as its first argument; the wrappers are NVRM's (NVRM.cpp)
CALL = re.compile(r"\b(sysctlbyname|nvrmSysR|nvrmSysW)\s*\(\s*([^,()]*)")
LITERAL = re.compile(r'"([A-Za-z0-9_.]+)"\s*$')
# the declarations the kexts use: SYSCTL_<KIND>(_debug, OID_AUTO, name, access, ...)
DECL = re.compile(r"\bSYSCTL_[A-Z_]+\s*\(\s*_debug\s*,\s*OID_AUTO\s*,\s*(\w+)\s*,([^,]*),")
APPLE = {"kern.osversion", "hw.memsize"}
fails = []


def arm(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (f"\n       {detail}" if detail and not ok else ""))
    if not ok:
        fails.append(name)


def strip_comments(text):
    """C and C++ comments out, string literals kept (a name inside a comment is not a call)."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1]); i = j + 1
        elif text.startswith("//", i):
            j = text.find("\n", i); i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2); i = n if j < 0 else j + 2
        else:
            out.append(c); i += 1
    return "".join(out)


def survey(sources):
    """sources: {path: text}. -> (reads {name: [path]}, decls {name: [(path, access)]}, unreadable [(path, call)])."""
    reads, decls, unreadable = {}, {}, []
    for path, raw in sources.items():
        text = strip_comments(raw)
        for m in CALL.finditer(text):
            arg = m.group(2).strip()
            before = text[max(0, m.start() - 12):m.start()]
            if arg.startswith("const char") or re.search(r"\bint\s+$", before):
                continue   # the function's own declaration or definition, not a call
            if m.group(1) == "sysctlbyname" and arg in ("n", "name"):
                continue   # the two wrappers hand their own parameter on; their callers are read instead
            lit = LITERAL.match(arg)
            if not lit:
                unreadable.append((path, m.group(0).strip()))
                continue
            reads.setdefault(lit.group(1), []).append(path)
        for m in DECL.finditer(text):
            decls.setdefault(m.group(1), []).append((path, " ".join(m.group(2).split())))
    return reads, decls, unreadable


def problems(sources):
    reads, decls, unreadable = survey(sources)
    bad = [f"{p}: a kernel-side sysctl call whose name is not a literal: {c}" for p, c in unreadable]
    for name, where in sorted(reads.items()):
        if not name.startswith("debug."):
            if name not in APPLE:
                bad.append(f"{name} (read in {where[0]}) is neither a debug.* name of these kexts nor one of Apple's "
                           "names this test knows to be readable from the kernel")
            continue
        d = decls.get(name[len("debug."):], [])
        if len(d) != 1:
            bad.append(f"{name} (read in {where[0]}) has {len(d)} declarations under kexts/")
        elif not re.search(r"\bCTLFLAG_KERN\b", d[0][1]):
            bad.append(f"{name} is read from the kernel ({where[0]}) but declared without CTLFLAG_KERN in {d[0][0]} "
                       f"({d[0][1]}): the kernel's read gets EPERM")
    return bad, reads


def tree(root):
    out = {}
    for p in sorted((root / "kexts").rglob("*")):
        if p.is_file() and p.suffix in SUFFIXES:
            out[str(p.relative_to(root))] = p.read_text(errors="replace")
    return out


sources = tree(ROOT)
bad, reads = problems(sources)
ours = sorted(n for n in reads if n.startswith("debug."))
arm(f"the kexts' sources were read ({len(sources)} files) and NVRM's kernel-side sysctl calls were found in them",
    len(sources) > 20 and "debug.nvaccel_heads_published" in reads and "debug.nvrmfb_guard_cleared" in reads,
    f"{len(sources)} files, names: {ours}")
arm(f"each of the {len(ours)} debug.* names a kext reads or writes from the kernel is declared once, with CTLFLAG_KERN",
    not bad, "\n       ".join(bad))
print("     names: " + ", ".join(ours) + "; Apple's: " + ", ".join(sorted(n for n in reads if not n.startswith("debug."))))

# ---- each check fires
GOOD = {"a.cpp": 'static int r(const char *n, int *v) { return sysctlbyname(n, v, 0, 0, 0); }\n'
                 'void f() { int v; nvrmSysR("debug.one", &v); nvrmSysW("debug.two", 1); }\n',
        "b.cpp": 'SYSCTL_INT(_debug, OID_AUTO, one, CTLFLAG_RD | CTLFLAG_LOCKED | CTLFLAG_KERN, &g, 0, "x");\n'
                 'SYSCTL_PROC(_debug, OID_AUTO, two, CTLTYPE_INT | CTLFLAG_RW | CTLFLAG_LOCKED | CTLFLAG_KERN,\n'
                 '            NULL, 0, h, "I", "y");\n'}
arm("control: a made-up pair of sources that is right passes", problems(GOOD)[0] == [], problems(GOOD)[0])
m = dict(GOOD); m["b.cpp"] = m["b.cpp"].replace("CTLFLAG_RD | CTLFLAG_LOCKED | CTLFLAG_KERN", "CTLFLAG_RD | CTLFLAG_LOCKED")
arm("  a read-only OID declared without CTLFLAG_KERN is reported (the 2.9 declaration)",
    any("debug.one" in b and "EPERM" in b for b in problems(m)[0]), problems(m)[0])
m = dict(GOOD); m["b.cpp"] = m["b.cpp"].replace("CTLFLAG_RW | CTLFLAG_LOCKED | CTLFLAG_KERN", "CTLFLAG_RW | CTLFLAG_LOCKED")
arm("  and so is one declared over two lines", any("debug.two" in b and "EPERM" in b for b in problems(m)[0]),
    problems(m)[0])
m = dict(GOOD); m["b.cpp"] = m["b.cpp"].replace("| CTLFLAG_KERN, &g", ", &g") + "// CTLFLAG_KERN one\n"
arm("  the flag in a comment does not count", any("debug.one" in b for b in problems(m)[0]), problems(m)[0])
m = dict(GOOD); m["a.cpp"] += 'void g() { int v; nvrmSysR("debug.three", &v); }\n'
arm("  a name with no declaration is reported", any("debug.three" in b and "0 declarations" in b for b in problems(m)[0]),
    problems(m)[0])
m = dict(GOOD); m["c.cpp"] = 'SYSCTL_INT(_debug, OID_AUTO, one, CTLFLAG_RD | CTLFLAG_KERN, &h, 0, "z");\n'
arm("  a name declared twice is reported", any("debug.one" in b and "2 declarations" in b for b in problems(m)[0]),
    problems(m)[0])
m = dict(GOOD); m["a.cpp"] += 'void g(const char *which) { int v; nvrmSysR(which, &v); }\n'
arm("  a name that is not a literal is refused", any("not a literal" in b for b in problems(m)[0]), problems(m)[0])
m = dict(GOOD); m["a.cpp"] += 'void g() { int v; size_t l = sizeof v; sysctlbyname("vm.swapusage", &v, &l, NULL, 0); }\n'
arm("  a name of Apple's that this test does not know is refused", any("vm.swapusage" in b for b in problems(m)[0]),
    problems(m)[0])
m = dict(GOOD); m["a.cpp"] += '// nvrmSysR("debug.four", &v);\n'
arm("  a call inside a comment is not a call", problems(m)[0] == [], problems(m)[0])
real = dict(sources)
fb = "kexts/NVRMFB/fb/nvrm-fb.cpp"
cut = re.sub(r"(nvrmfb_guard_cleared,\s*CTLFLAG_RD \| CTLFLAG_LOCKED) \| CTLFLAG_KERN", r"\1", real.get(fb, ""))
real[fb] = cut
arm("  the driver's own sources with the flag taken off debug.nvrmfb_guard_cleared are reported",
    cut != sources.get(fb) and any("debug.nvrmfb_guard_cleared" in b and "EPERM" in b for b in problems(real)[0]),
    problems(real)[0])

print(f"{len(fails)} failed")
sys.exit(1 if fails else 0)
