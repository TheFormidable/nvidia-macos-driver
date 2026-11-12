#!/usr/bin/env python3
"""Every sysctl the kexts declare is on a reviewed list, none is writable by an ordinary user, no name is declared
twice, and the diagnostics that read physical memory stay behind the nvrmdiag boot-arg and a root check.

Before 2.0.3 debug.nvrm_winraw returned 256 KB of physical memory to any user, beside other panic-hunt leftovers.
The fix removed or gated them, but nothing stopped the next debug switch from shipping: this list does. A new writable
sysctl fails here until it is added below with the reason it is safe.

The list used to name eight files. A sysctl in a ninth (debug.nvrm_tgp_enable, nvrm_power.cpp), a write-only one
(CTLFLAG_WR: debug.nmintelfb_dmc) and any sysctl declared through a macro were outside what it checked. Now every
source file under kexts/ is read, comments set aside, macros that declare sysctls expanded at each use, and what
cannot be read from the text is refused instead of passed: a sysctl macro of an unknown kind, access flags that are
not spelled out, a macro handed on as an argument, a hand-built sysctl_oid, a registered OID with no declaration.

Two kexts that declare the same name is a mistake no compiler sees (each kext is its own link), and the second one to
register loses: so each name may be declared once across all of kexts/.

The arms at the end show that each check fires, on made-up sources and on a made-up tree.
Usage: test_sysctl_allowlist.py [driver checkout]"""
import collections
import pathlib
import re
import sys
import tempfile

ROOT = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1]
# What is read: every C, C++ and Objective-C source and header under kexts/, the .inc files they include, and the lines
# the patches under kexts/ add to NVIDIA's tree (they are compiled into NVRM.kext too). Any other file under kexts/
# that carries a sysctl declaration is refused, so a new kind of file cannot bring one in unread.
SOURCE_SUFFIXES = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".inc", ".m", ".mm"}
PATCH_SUFFIXES = {".patch", ".diff"}
# The eight files the list named until it walked the tree. Each must still be there and be read: if the walk ever
# looks at the wrong place, this says so instead of "0 problems in 0 files".
ONCE_NAMED = ["kexts/NVRM/NVRM.cpp", "kexts/NVRM/os-xnu2.cpp", "kexts/NVRM/os-nvkms-xnu.cpp", "kexts/NVRM/nvrm_surfshare.cpp",
              "kexts/NVRM/accel/nvrm-accel.cpp", "kexts/NVRMFB/fb/nvrm-fb.cpp", "kexts/NVRMAGDC/nvrm-agdc.cpp",
              "kexts/NMIntelFB/intelfb.cpp"]
# Writable sysctls: root-only (no CTLFLAG_ANYBODY), each a display or WindowServer-path switch support asks users to
# set with sudo. Reviewed 2.6.6. A name stays here only while its sysctl is writable (an arm below checks that).
WRITABLE = {"nvaccel_igpu", "nvaccel_iop", "nvaccelfb", "nvaccel_vm_sticky", "nvaccel_iop_flip", "nvaccel_surface_gpu_on",
            "nvaccel_iop_async", "nvaccel_crc_head", "nvaccel_svdiag", "nvrmfb_agdc", "nvrmfb_vram_display_aware",
            "nvrmfb_vramtest", "nvrmfb_vramtest_mode", "nvrmfb_flip_latch", "nvrmfb_flip_lean", "nvrmfb_flip_interval",
            # Reviewed 2.7.2: the display-link switch the boot guard manages, and the Intel panel's test attach. Both root-only.
            "nvrmfb_agdc_k5", "nmintelfb_attach",
            # Reviewed 2.9: the high-refresh latch switch (an on/off integer) and the output colour controls. The colour
            # handler rejects any value outside the enumerated bpc / format / range sets and -100..100 for vibrance
            # (EINVAL), and all five are root-only.
            "nvrmfb_flip_latch_auto", "nvrmfb_color_bpc", "nvrmfb_color_format", "nvrmfb_color_range", "nvrmfb_color_vibrance",
            # Reviewed 2.9: the opt-in unlocked page-off wait, an on/off integer read only as zero/non-zero; root-only.
            "nvaccel_pageoff_unlocked_wait",
            # Both older than this review and outside the eight files the list read until it walked the tree. The notebook
            # power-budget rollback is an on/off integer the power code reads as zero/non-zero (nvrm_power.cpp). The Intel
            # display firmware upload is write-only, and its handler parses and checks the blob before any of it is
            # used (intelfb.cpp). Both root-only.
            "nvrm_tgp_enable", "nmintelfb_dmc"}
# Read-only diagnostics that print physical addresses or memory: registered only with boot-arg nvrmdiag=1, root only.
PHYSICAL = {"nvrm_watchpage", "nvrm_physmap", "nvrm_pvsentinel", "nvrm_winwhy", "nvrm_pvchain", "nvrm_winraw"}
DECL = re.compile(r"SYSCTL_(?:INT|UINT|QUAD|PROC|STRING|OPAQUE|LONG|ULONG)\s*\(\s*_debug\s*,\s*OID_AUTO\s*,\s*(\w+)\s*,(.*?)\)\s*;", re.S)
WRITE = re.compile(r"\bCTLFLAG_(?:RW|WR)\b")
# The macros of <sys/sysctl.h> that declare an OID all begin (parent, number, name, access, ...). The four below them
# declare nothing. Anything else spelled SYSCTL_x( is refused: this file would not know where its name and access are.
DECLARING = {"INT", "UINT", "LONG", "ULONG", "QUAD", "OPAQUE", "STRUCT", "STRING", "PROC", "NODE", "OID", "COMPAT_INT",
             "COMPAT_UINT", "STRUCT_INIT", "EXTENSIBLE_NODE", "NODE_MANUAL", "OID_MANUAL"}
NOT_DECLARING = {"HANDLER_ARGS", "IN", "OUT", "DECL"}
SYSCTL_CALL = re.compile(r"\bSYSCTL_(\w+)\s*\(")
IDENT = re.compile(r"[A-Za-z_]\w*\Z")
FAILS = []


def arm(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILS.append((name, detail))


def problems(text):
    """The rules one file is held to as plain text, comments included: what this test has checked since 2.0.3."""
    out = []
    for m in DECL.finditer(text):
        name, flags = m.group(1), m.group(2)
        if "CTLFLAG_ANYBODY" in flags:
            out.append(f"{name}: writable by any user (CTLFLAG_ANYBODY)")
        if WRITE.search(flags) and name not in WRITABLE:
            out.append(f"{name}: writable but not on the reviewed list")
    for name in PHYSICAL:
        if f"sysctl__debug_{name}" not in text:
            continue
        regs = re.findall(rf"^.*sysctl_register_oid\(&sysctl__debug_{name}\).*$", text, re.M)
        gated = all("nv_diag_on()" in r for r in regs) or (regs and "nv_pv_start" in text and
                    re.search(r"if \(nv_diag_on\(\)\) nv_pv_start\(\)", text))
        if not gated:
            out.append(f"{name}: registered without the nvrmdiag boot-arg")
    if "nv_wm_raw_sysctl" in text and not re.search(r"nv_wm_raw_sysctl SYSCTL_HANDLER_ARGS\s*\{[^}]*NV_DIAG_ROOT_ONLY", text, re.S):
        out.append("nvrm_winraw: handler has no root check")
    if re.search(r"SYSCTL_PROC\([^;]*nvrm_\w+[^;]*CTLFLAG_RD[^;]*\)", text) and "NV_DIAG_ROOT_ONLY" not in text and "os-xnu2" in text:
        out.append("physical-memory handlers lost their root check")
    return out


# ---- reading C text -------------------------------------------------------------------------------------------------
# A comment, a string or a character constant. Comments come first: an apostrophe or a quote inside one is prose.
LEX = re.compile(r"""//[^\n]*|/\*.*?\*/|/\*.*\Z|"(?:\\.|[^"\\\n])*"|'(?:\\.[^'\n]{0,8}|[^'\\\n])'""", re.S)


def blank(s):
    return re.sub(r"[^\n\f]", " ", s)


def lex(text):
    """-> (code, masked), both as long as text. code: comments blanked. masked: the inside of strings and character
    constants blanked as well, so brackets and commas in it are the program's own. A backslash at the end of a line
    becomes a form feed: the line goes on (a macro is one line to every pattern here) and lines can still be counted."""
    text = text.replace("\\\r\n", "  \f").replace("\\\n", " \f")
    code = LEX.sub(lambda m: m.group(0) if m.group(0)[0] in "\"'" else blank(m.group(0)), text)
    masked = LEX.sub(lambda m: m.group(0)[0] + blank(m.group(0)[1:-1]) + m.group(0)[-1] if m.group(0)[0] in "\"'"
                     else blank(m.group(0)), text)
    return code, masked


def line_of(text, off):
    return 1 + text.count("\n", 0, off) + text.count("\f", 0, off)


def directive(masked, off):
    """The preprocessor word of the line off is in ('define', 'if', ...), or '' for an ordinary line."""
    m = re.match(r"[ \t]*#[ \t]*(\w+)", masked[masked.rfind("\n", 0, off) + 1:off + 1])
    return m.group(1) if m else ""


def call_args(code, masked, open_paren):
    """The arguments of the call whose '(' is at open_paren, or None when the brackets do not close."""
    depth, start, args = 0, open_paren + 1, []
    for i in range(open_paren, len(masked)):
        c = masked[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                args.append(code[start:i].strip())
                return args
        elif c == "," and depth == 1:
            args.append(code[start:i].strip())
            start = i + 1
    return None


Macro = collections.namedtuple("Macro", "name params body rel line")
Decl = collections.namedtuple("Decl", "parent name flags kind rel line via")
MACRO_DEF = re.compile(r"^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)(\(([^)\n]*)\))?([^\n]*)", re.M)
BODY_TOKEN = re.compile(r'"(?:\\.|[^"\\\n])*"|##|#[ \t]*([A-Za-z_]\w*)|\b([A-Za-z_]\w*)\b')


def full_name(parent, name):
    return parent.lstrip("_").replace("_", ".") + "." + name


def expand(macro, args):
    """The macro's replacement text with the arguments put in, or None when this file cannot do that faithfully."""
    if macro.params is None or len(args) != len(macro.params):
        return None
    put = dict(zip(macro.params, args))

    def one(m):
        t = m.group(0)
        if t == "##":
            return "\x00"
        if t[0] == '"':
            return t
        if m.group(1):
            return '"' + put[m.group(1)].replace('"', '\\"') + '"' if m.group(1) in put else t
        return put.get(t, t)
    return re.sub(r"\s*\x00\s*", "", BODY_TOKEN.sub(one, macro.body))


class Scan:
    """Every sysctl declaration in a set of files (path -> text), and everything about them that is not in order."""

    def __init__(self, files):
        self.decls, self.problems, self.duplicates, self.refs = [], [], [], []
        self.text = {rel: lex(t) for rel, t in files.items()}
        self.macros = collections.defaultdict(list)
        for rel, (code, masked) in self.text.items():
            for m in MACRO_DEF.finditer(masked):
                params = None
                if m.group(2) is not None:
                    params = [p.strip() for p in m.group(3).split(",")] if m.group(3).strip() else []
                    if not all(IDENT.match(p) for p in params):
                        params = None           # variadic or otherwise not a plain list: refused where it is used
                self.macros[m.group(1)].append(Macro(m.group(1), params, code[m.start(4):m.end(4)], rel, line_of(masked, m.start())))
        # A macro declares sysctls when its text does, or when it uses a macro that does.
        self.declaring = set()
        grew = True
        while grew:
            grew = False
            for name, defs in self.macros.items():
                if name in self.declaring:
                    continue
                for d in defs:
                    body = lex(d.body)[1]
                    if any(k.group(1) not in NOT_DECLARING for k in SYSCTL_CALL.finditer(body)) or \
                            any(re.search(rf"\b{re.escape(n)}\b", body) for n in self.declaring):
                        self.declaring.add(name)
                        grew = True
                        break
        for name in sorted(self.declaring):
            for d in self.macros[name]:
                if WRITE.search(d.body) or "CTLFLAG_ANYBODY" in d.body:
                    self.problems.append(f"{d.rel}:{d.line}: {name}: a macro that declares writable sysctls "
                                         "(declare each one by itself, so that it is reviewed by name)")
        for rel, (code, masked) in sorted(self.text.items()):
            self.walk(rel, code, masked, None, 0, 0)
            self.loose_ends(rel, masked)
        self.judge()

    def macro_for(self, name, rel, line):
        defs = self.macros[name]
        here = [d for d in defs if d.rel == rel and d.line <= line]
        if here:
            return here[-1]
        if len({(tuple(d.params) if d.params is not None else None, " ".join(d.body.split())) for d in defs}) == 1:
            return defs[0]
        return None

    def walk(self, rel, code, masked, via, base_line, depth):
        """Declarations written in this text, and those the sysctl macros used in it stand for. via: the macro whose
        expansion this text is (then every position is reported as the line of that use)."""
        def where(off):
            return base_line if via else line_of(masked, off)

        for m in SYSCTL_CALL.finditer(masked):
            if m.group(1) in NOT_DECLARING or (not via and directive(masked, m.start()) == "define"):
                continue
            at = f"{rel}:{where(m.start())}"
            if m.group(1) not in DECLARING:
                self.problems.append(f"{at}: SYSCTL_{m.group(1)}: not a sysctl macro this list knows how to read")
                continue
            args = call_args(code, masked, m.end() - 1)
            if not args or len(args) < 4 or not IDENT.match(args[2]) or not IDENT.match(args[0]):
                self.problems.append(f"{at}: SYSCTL_{m.group(1)}: its parent, name and access cannot be read from the text")
                continue
            self.decls.append(Decl(args[0], args[2], args[3], m.group(1), rel, where(m.start()), via))
        for name in sorted(self.declaring):
            for m in re.finditer(rf"\b{re.escape(name)}\b(\s*\()?", masked):
                kind = "" if via else directive(masked, m.start())
                if kind in ("define", "undef", "ifdef", "ifndef", "if", "elif"):
                    continue                    # its own definition, another macro's text (expanded at that one's uses), a test
                at = f"{rel}:{where(m.start())}"
                if not m.group(1):
                    self.problems.append(f"{at}: {name} declares sysctls and is named without arguments here (handed on to "
                                         "another macro, or used as a bare word): the names it then declares cannot be read")
                    continue
                macro, args = self.macro_for(name, rel, where(m.start())), call_args(code, masked, m.end() - 1)
                text = expand(macro, args) if macro and args is not None and depth < 8 else None
                if text is None:
                    self.problems.append(f"{at}: {name}(...): this use of a macro that declares sysctls cannot be read "
                                         "(several definitions, a variable argument list, or unbalanced brackets)")
                    continue
                c2, m2 = lex(text)
                self.walk(rel, c2, m2, via or name, where(m.start()), depth + 1)

    def loose_ends(self, rel, masked):
        for m in re.finditer(r"(\bextern\s+)?\bstruct\s+sysctl_oid\s+(\w+)\s*(=|\{)", masked):
            if not m.group(1):
                self.problems.append(f"{rel}:{line_of(masked, m.start())}: {m.group(2)}: a sysctl_oid built by hand "
                                     "(use the SYSCTL_ macros, which this list reads)")
        for m in re.finditer(r"\bsysctl__\w+", masked):
            if directive(masked, m.start()) != "define":
                self.refs.append((m.group(0), f"{rel}:{line_of(masked, m.start())}"))

    def judge(self):
        seen = collections.defaultdict(list)
        for d in self.decls:
            at, name = f"{d.rel}:{d.line}", full_name(d.parent, d.name)
            seen[name].append(at + (f" (through {d.via})" if d.via else ""))
            flags = lex(d.flags)[1]
            # CTLFLAG_ and CTLTYPE_ names joined by |, nothing else: a constant of the file's own or a number could
            # carry the write bit unseen
            if re.sub(r"\bCTL(?:FLAG|TYPE)_[A-Z0-9_]+\b", "", flags).strip(" \t\n\f|()"):
                self.problems.append(f"{at}: {name}: its access is given as {' '.join(d.flags.split())!r}, not spelled out with "
                                     "CTLFLAG_ names, so whether it is writable cannot be read")
            if "CTLFLAG_ANYBODY" in flags:
                self.problems.append(f"{at}: {name}: writable by any user (CTLFLAG_ANYBODY)")
            if WRITE.search(flags):
                if d.via:
                    self.problems.append(f"{at}: {name}: writable and declared through the macro {d.via} "
                                         "(declare it by itself, so that it is reviewed by name)")
                elif d.parent != "_debug" or d.name not in WRITABLE:
                    self.problems.append(f"{at}: {name}: writable but not on the reviewed list")
        for name, places in sorted(seen.items()):
            if len(places) > 1:
                self.duplicates.append(f"{name} is declared {len(places)} times: {', '.join(places)}")
        known = {f"sysctl_{d.parent}_{d.name}" for d in self.decls}
        for ident, at in self.refs:
            if ident not in known:
                self.problems.append(f"{at}: {ident} is used but no declaration of that sysctl was read")
        self.names = sorted(seen)
        self.writable = {d.name for d in self.decls if d.parent == "_debug" and WRITE.search(d.flags)}


def read_tree(root):
    """-> (files to scan: path -> text, raw text of each source file for the plain-text rules, files not read that
    carry a sysctl declaration all the same)."""
    files, raw, unread = {}, {}, []
    for p in sorted((root / "kexts").rglob("*")):
        if not p.is_file():
            continue
        rel = str(p.relative_to(root))
        text = p.read_text(errors="replace")
        if p.suffix in SOURCE_SUFFIXES:
            files[rel] = raw[rel] = text
        elif p.suffix in PATCH_SUFFIXES:
            # only what the patch adds; removed and context lines are kept as empty lines so line numbers stay true
            files[rel] = "\n".join(ln[1:] if ln.startswith("+") and not ln.startswith("+++") else "" for ln in text.split("\n"))
        elif any(k.group(1) not in NOT_DECLARING for k in SYSCTL_CALL.finditer(text)):
            unread.append(rel)
    return files, raw, unread


Review = collections.namedtuple("Review", "problems duplicates stale files names writable")


def review(root):
    """One checkout judged: what is wrong with its sysctls, the names declared twice, the names on the reviewed list
    that nothing declares writable, and how much was read."""
    files, raw, unread = read_tree(root)
    scan = Scan(files)
    out = list(scan.problems)
    out += [f"{rel}: a sysctl declaration in a kind of file this list does not read" for rel in unread]
    for rel, text in raw.items():
        # The older plain-text pass stays, so nothing it refused is let through: it also sees a declaration that is
        # commented out. What the walk has already reported for the same file and name is not said twice.
        for x in problems(text):
            name, what = x.split(": ", 1)
            if not any(o.startswith(rel + ":") and f".{name}: {what}" in o for o in scan.problems):
                out.append(f"{rel}: {x}")
    stale = [f"{name} is on the reviewed list but no sysctl of that name is writable: take it off the list "
             "(a name left there would let the sysctl turn writable again unreviewed)" for name in sorted(WRITABLE - scan.writable)]
    return Review(out, scan.duplicates, stale, len(files), scan.names, scan.writable)


def caught(files, needle):
    """Whether scanning the made-up files reports a problem that mentions needle."""
    s = Scan(files)
    return any(needle in p for p in s.problems + s.duplicates)


def clean(files):
    """The scan of made-up files when nothing is wrong with them, else None."""
    s = Scan(files)
    return s if not s.problems and not s.duplicates else None


tree = review(ROOT)
# 2.9 declares 158; the floor of 100 is only there so that a walk which finds next to nothing cannot pass as "0 problems"
arm(f"the walk read the sources under kexts/ ({tree.files} files, {len(tree.names)} sysctls declared, {len(tree.writable)} of them writable)",
    tree.files > len(ONCE_NAMED) and len(tree.names) > 100)
missing = [rel for rel in ONCE_NAMED if not (ROOT / rel).is_file()]
arm("the eight files the list once named are still there, and the walk goes beyond them (nvrm_power.cpp)",
    not missing and "debug.nvrm_tgp_enable" in tree.names, ", ".join(missing))
arm("every sysctl is reviewed: none user-writable, writable ones listed, physical-memory ones gated, all of them readable from the text",
    not tree.problems, "; ".join(tree.problems))
arm("no sysctl name is declared twice across the kexts", not tree.duplicates, "; ".join(tree.duplicates))
arm("the reviewed list names only sysctls that are writable today", not tree.stale, "; ".join(tree.stale))

# the checks themselves fire (negative controls on made-up sources): the three the list has always had ...
arm("an unlisted writable sysctl is caught",
    problems("SYSCTL_INT(_debug, OID_AUTO, nv_new_knob, CTLFLAG_RW | CTLFLAG_LOCKED, &x, 0, \"x\");") != [])
arm("CTLFLAG_ANYBODY is caught",
    problems("SYSCTL_INT(_debug, OID_AUTO, nvaccel_igpu, CTLFLAG_RW | CTLFLAG_ANYBODY, &x, 0, \"x\");") != [])
arm("a physical-memory sysctl registered without the boot-arg is caught",
    problems("SYSCTL_PROC(_debug, OID_AUTO, nvrm_physmap, CTLFLAG_RD, 0, 0, h, \"A\", \"\");\n"
             "static void f(void){ sysctl_register_oid(&sysctl__debug_nvrm_physmap); }") != [])
# ... the same three through the walk, which is what the tree is judged by ...
RW = 'SYSCTL_INT(_debug, OID_AUTO, nv_new_knob, CTLFLAG_RW | CTLFLAG_LOCKED, &x, 0, "x");\n'
RD = 'SYSCTL_QUAD(_debug, OID_AUTO, nv_count, CTLFLAG_RD | CTLFLAG_LOCKED, &n, "n (see the log, too)");\n'
arm("the walk catches an unlisted writable sysctl, and a listed one passes",
    caught({"a.cpp": RW}, "nv_new_knob: writable but not on the reviewed list")
    and clean({"a.cpp": RW.replace("nv_new_knob", "nvaccel_igpu")}) is not None)
arm("the walk catches CTLFLAG_ANYBODY on a listed name",
    caught({"a.cpp": RW.replace("nv_new_knob", "nvaccel_igpu").replace("CTLFLAG_LOCKED", "CTLFLAG_ANYBODY")}, "writable by any user"))
arm("a read-only sysctl passes, with brackets and a comma in its description",
    clean({"a.cpp": RD}) is not None and clean({"a.cpp": RD}).names == ["debug.nv_count"])
# ... and the holes the eight-file list had
arm("a write-only sysctl (CTLFLAG_WR) is caught like a read-write one",
    problems("SYSCTL_PROC(_debug, OID_AUTO, nv_new_blob, CTLTYPE_OPAQUE | CTLFLAG_WR | CTLFLAG_LOCKED, 0, 0, h, \"-\", \"x\");") != []
    and caught({"a.cpp": 'SYSCTL_PROC(_debug, OID_AUTO, nv_new_blob, CTLTYPE_OPAQUE | CTLFLAG_WR | CTLFLAG_LOCKED, 0, 0, h, "-", "x");'},
               "nv_new_blob: writable but not on the reviewed list"))
arm("a writable sysctl outside debug.* is caught even under a listed name",
    caught({"a.cpp": RW.replace("_debug", "_kern").replace("nv_new_knob", "nvaccel_igpu")}, "kern.nvaccel_igpu: writable but not"))
CNT = "#define NV_CNT(nm, var, desc) SYSCTL_QUAD(_debug, OID_AUTO, nm, CTLFLAG_RD | CTLFLAG_LOCKED, (SInt64 *)&var, desc)\n"
through = clean({"a.cpp": CNT + 'NV_CNT(nv_a, gA, "a, with (brackets)");\nNV_CNT(nv_b, gB[2], "b");\n'})
arm("sysctls declared through a macro are seen by name", through is not None and through.names == ["debug.nv_a", "debug.nv_b"])
arm("a macro in a header is followed into the file that uses it",
    Scan({"a.h": CNT, "b.cpp": 'NV_CNT(nv_a, gA, "a");\n'}).names == ["debug.nv_a"])
arm("a macro spread over several lines, and one that pastes a prefix onto the name, are followed",
    Scan({"a.cpp": "#define NV_P(nm, var) \\\n    SYSCTL_QUAD(_debug, OID_AUTO, nv_ ## nm, \\\n      CTLFLAG_RD, &var, #nm)\nNV_P(x, gX);\n"}).names == ["debug.nv_x"])
arm("a macro built on a sysctl macro is followed too",
    Scan({"a.cpp": CNT + "#define NV_TWO(a, b) NV_CNT(a, g ## a, \"\"); NV_CNT(b, g ## b, \"\")\nNV_TWO(nv_a, nv_b);\n"}).names == ["debug.nv_a", "debug.nv_b"])
arm("a macro that declares writable sysctls is caught",
    caught({"a.cpp": "#define NV_KNOB(nm, var) SYSCTL_INT(_debug, OID_AUTO, nm, CTLFLAG_RW | CTLFLAG_LOCKED, &var, 0, \"\")\n"},
           "a macro that declares writable sysctls"))
arm("a macro that takes the access as an argument cannot make a sysctl writable, even a listed one",
    caught({"a.cpp": "#define NV_ANY(nm, acc, var) SYSCTL_INT(_debug, OID_AUTO, nm, acc, &var, 0, \"\")\n"
                     "NV_ANY(nvaccel_igpu, CTLFLAG_RW | CTLFLAG_LOCKED, gX);\n"}, "writable and declared through the macro NV_ANY"))
arm("a sysctl macro handed on as an argument (a table of counters) is refused, as its names cannot be read",
    caught({"a.cpp": CNT + "#define NV_ALL(X) X(nv_a, gA, \"\") X(nv_b, gB, \"\")\nNV_ALL(NV_CNT)\n"}, "is named without arguments"))
arm("access that is not spelled out is refused: a constant of the file's own, and a number",
    caught({"a.cpp": "#define NV_RW (CTLFLAG_RW | CTLFLAG_LOCKED)\nSYSCTL_INT(_debug, OID_AUTO, nv_quiet, NV_RW, &x, 0, \"\");\n"},
           "not spelled out")
    and caught({"a.cpp": "SYSCTL_INT(_debug, OID_AUTO, nv_quiet, 0xC0000000 | CTLFLAG_LOCKED, &x, 0, \"\");\n"}, "not spelled out"))
arm("a sysctl macro of a kind the list does not know is refused",
    caught({"a.cpp": "SYSCTL_SCALABLE_COUNTER(_debug, nv_sc, gSc, \"\");\n"}, "not a sysctl macro this list knows"))
arm("a sysctl_oid built by hand is refused, an extern declaration of one is not",
    caught({"a.cpp": "struct sysctl_oid sysctl__debug_nv_hand = { 0 };\n"}, "built by hand")
    and clean({"a.cpp": RD + "void f(void) { extern struct sysctl_oid sysctl__debug_nv_count; sysctl_register_oid(&sysctl__debug_nv_count); }\n"}) is not None)
arm("an OID that is registered but whose declaration was not read is reported",
    caught({"a.cpp": "void f(void) { sysctl_register_oid(&sysctl__debug_nv_ghost); }\n"}, "sysctl__debug_nv_ghost is used but no declaration"))
arm("a name declared in two kexts is caught",
    caught({"kexts/A/a.cpp": RD, "kexts/B/b.cpp": RD}, "debug.nv_count is declared 2 times: kexts/A/a.cpp:1, kexts/B/b.cpp:1"))
arm("a name declared once directly and once through a macro is caught",
    caught({"a.cpp": RD, "b.cpp": CNT + 'NV_CNT(nv_count, gN, "");\n'}, "debug.nv_count is declared 2 times"))
arm("a declaration in a comment, or a name in a string, is not a second declaration",
    clean({"a.cpp": RD + "// " + RD + "/* " + RD + " */\nconst char *s = \"SYSCTL_QUAD(_debug, OID_AUTO, nv_count, CTLFLAG_RW, &n, 0)\";\n"}) is not None)
arm("control: the two declarations above, uncommented, are a duplicate",
    caught({"a.cpp": RD + RD}, "debug.nv_count is declared 2 times"))
with tempfile.TemporaryDirectory() as d:
    d = pathlib.Path(d)
    made = {"kexts/NVNew/deep/er/new_file.cpp": RW,
            "kexts/NVNew/hdr.hpp": RD,
            "kexts/NVNew/patches/0001-x.patch": "--- a/x.c\n+++ b/x.c\n@@ -1,2 +1,3 @@\n context\n-" + RD + "+" + RD.replace("nv_count", "nv_patched"),
            "kexts/NVNew/table.def": RD.replace("nv_count", "nv_in_def"),
            "kexts/NVNew/notes.md": "the sysctl debug.nv_count counts things\n",
            "elsewhere/not_a_kext.cpp": RW.replace("nv_new_knob", "nv_outside")}
    for rel, text in made.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text)
    made_up = review(d)
    arm("on a made-up tree: a writable sysctl in a file no list ever named is caught, with its place",
        any(p.startswith("kexts/NVNew/deep/er/new_file.cpp:1: debug.nv_new_knob: writable but not") for p in made_up.problems),
        "; ".join(made_up.problems)[:300])
    arm("on a made-up tree: a header and the lines a patch adds are read, the line a patch removes is not",
        made_up.names == ["debug.nv_count", "debug.nv_new_knob", "debug.nv_patched"] and not made_up.duplicates, ", ".join(made_up.names))
    arm("on a made-up tree: a declaration in a kind of file the walk does not read is refused, prose about a sysctl is not",
        any(p.startswith("kexts/NVNew/table.def: a sysctl declaration in a kind of file") for p in made_up.problems)
        and not any("notes.md" in p or "nv_outside" in p for p in made_up.problems))
    arm("on a made-up tree: a listed name that no sysctl declares writable any more is reported as stale",
        any(p.startswith("nvaccel_pageoff_unlocked_wait is on the reviewed list but") for p in made_up.stale))
if FAILS:
    print("\nwhat failed:")
    for name, detail in FAILS:
        print(f"  {name}" + (f"\n    {detail[:700]}" if detail else ""))
print(f"{len(FAILS)} failed")
sys.exit(1 if FAILS else 0)
