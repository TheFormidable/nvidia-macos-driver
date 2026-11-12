#!/usr/bin/env python3
"""switch_registry.py: every switch the driver reads, in one table, and the rules a switch has to keep.

A switch is anything that changes what the shipped code does without a source change: an environment variable, a boot
argument, a sysctl, a flag file, a branch on a process name, a variable of a build script that changes what is compiled.
The tree had hundreds and no list of them, so nobody could say which ones an ordinary user needs (none, beyond the
install line), which are diagnostics and which are leftovers. tools/switches.tsv is the list. This tool finds what the
source reads and holds the two against each other; tools/test_switch_registry.py runs it as a host test.

  switch_registry.py check       the registry and the source agree and every rule below holds
  switch_registry.py gen         write docs/SWITCHES.md and kexts/NVRM/nvrm_switch_table.h from the registry
  switch_registry.py sync        bring the registry up to the tree where that needs no judgement (see below), then gen
  switch_registry.py finalize    sync, and close the rows that were written ahead of their code; then check
  switch_registry.py scan [-v]   what the source reads, by kind (needs no registry)
  switch_registry.py doctor      on an installed Mac: which switches this machine has set and what each is
  switch_registry.py bootstrap   print a first registry for a tree that has none (a draft to review, not to trust)
  switch_registry.py --selftest  every rule fires on synthetic input

AFTER MERGING BRANCHES THAT ADD OR REMOVE SWITCHES, ONE COMMAND BRINGS THE REGISTRY UP TO THE TREE:

    python3 tools/switch_registry.py finalize

It adds a row for every new read-only sysctl (a counter is not a choice, so its row is its own description), drops the
row of a read-only counter that left with its code, follows a sysctl that turned read-only, lowers every ceiling to the
present count, removes every "#pending" mark, regenerates the two generated files and runs `check`. What it will not do
is decide for you: a new environment name, boot argument, writable sysctl, flag file or process-name branch without a
row, and a row whose code never arrived, are printed with what to do and the command exits 1. Commit when it exits 0.

A "#pending <kind> <name>" line in the registry marks a row that was written before the code that reads it was merged
(several branches were developed side by side). Such a row may be read nowhere; nothing else is excused. `finalize`
removes the marks, so from then on a row with no code is an error again, and `check` refuses a mark outright once the
tree has a release newer than 2.9 in CHANGELOG.md.

Registry columns (tab separated, one switch per line, sorted by kind then name):
  kind     env | bootarg | sysctl | file | procname | build
  name     the name as the source spells it
  tier     install    part of the documented install line; the driver does not start without it
           setting    a preference with a control (tools/nullmoth_settings.py); not a fix
           hardware   depends on the machine (a laptop, a panel, a second card, firmware): cannot be defaulted blind
           recovery   makes the driver stand down or start in a safer way; a way out, never a way in
           escape     the one opt-out of a change that is on by default; has a review version
           test       a hook that injects a fault or forces a path so a recovery can be exercised; does nothing unset
           diag       observes only: a log, a dump, a counter, a trace
           dev        an experiment, an A/B or a tuning number left from development; may only become fewer
           internal   not a user switch: set by the driver for itself, a standard variable it reads (HOME), a build
                      host path
           retired    a name the source no longer reads; kept so a stale setting can be named to its user
  default  what is in effect when the switch is not set
  since    the release that introduced it ("2.9" = it was there at 2.9)
  review   for an escape, and for a diag that is not a sysctl and was added after 2.9: the release at which it is
           promoted (the switch deleted) or renewed with a reason. "-" otherwise. A tree that has reached it fails.
  purpose  one line a user can read

Rules `check` enforces (each has a control in --selftest or in tools/test_switch_registry.py):
  1. every switch the source reads has a row, and every row that is not retired (or pending) is read by the source;
  2. no row has an empty default or purpose; rows a user may meet say what they do;
  3. a switch added after 2.9 is never an opt-in: a new row is an escape (on by default, with a review version the tree
     has not reached), a test hook, a read-only observer, a setting the settings tool offers, or internal. The tiers
     dev, hardware, recovery and install are closed to new rows. A new switch cannot be filed as an old one either:
     the number of rows that claim to date from 2.9 cannot rise, and where the clone has the 2.9 release (tag v2.9)
     the test scans that tree and every such row has to name a switch it read;
  4. the number of dev rows per component, of test hooks, and of rows without a reviewed purpose stay at or under the
     ceilings in the registry header; a ceiling never rises (the test compares with the previous commit);
  5. no sysctl is writable unless it was at 2.9 and is a setting, hardware, recovery, diag or dev row; none is
     CTLFLAG_ANYBODY; a declaration whose access flags are not spelled out is refused, because it cannot be judged;
  6. a boot argument is read with a literal name: PE_parse_boot_argn(variable, ...) is followed to the function that
     passes its own parameter on, and every call of that function must pass a literal;
  7. tools/nullmoth_settings.py offers exactly the boot arguments and settings-file keys of tier setting;
  8. docs/SWITCHES.md and kexts/NVRM/nvrm_switch_table.h are byte for byte what `gen` writes.
"""
import json
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REG = "tools/switches.tsv"
GEN_MD = "docs/SWITCHES.md"
GEN_H = "kexts/NVRM/nvrm_switch_table.h"
KINDS = ("env", "bootarg", "sysctl", "file", "procname", "build")
# The order is the order of the columns in docs/SWITCHES.md and of the enum in nvrm_switch_table.h.
TIERS = ("install", "setting", "hardware", "recovery", "escape", "test", "diag", "dev", "internal", "retired")
BASE = "2.9"       # rows with this `since` (or older) are the switches the registry found when it was started
NEXT = "3.0"       # the release a row written on top of BASE belongs to, until CHANGELOG.md names a newer one
UNREVIEWED = "unreviewed"
NEW_TIERS = ("escape", "test", "diag", "setting", "internal", "retired")
WRITABLE_TIERS = ("setting", "hardware", "recovery", "diag", "dev")
NONDEFAULT = ("escape", "test", "dev", "retired")   # what debug.nvrm_switches_nondefault and `doctor` count
C_EXT = (".c", ".cpp", ".h", ".hpp", ".m", ".mm", ".inc")
# not kext sources: patches kept as documentation
UNBUILT = ("kexts/NVRM/proposals/",)
# The one function that may hand PE_parse_boot_argn a name it was given through a pointer: the kernel's report of
# which switches are set asks about every name of the generated table, which is this registry.
BOOT_PROBES = ("nvrmSwitchPresent",)
PREFIX = r"(?:NVMTL_|NVK_|NAK_|NVRM_|METAL2VULKAN_|M2V_|ELFHOST_)"
ENV_LIT = re.compile(r"@?\"(" + PREFIX + r"[A-Z0-9_]*[A-Z0-9])\"")
ENV_READ = re.compile(r"\b(?:getenv|secure_getenv|os_get_option|debug_get_option|debug_get_bool_option|debug_get_num_option)\s*\(\s*\"([A-Za-z0-9_]+)\"")
ENV_READ_RS = re.compile(r"\b(?:env::var|env::var_os|present)\s*\(\s*\"([A-Za-z0-9_]+)\"")
ENV_SET = re.compile(r"(?:un)?setenv\s*\(\s*$")
# a name put together at run time ("NVMTL_" + something) cannot be listed; a prefix used to recognise names can
ENV_GLUE = re.compile(r"@?\"(" + PREFIX + r")(?:%[-0-9.]*[a-zA-Z@])?\"")
ENV_GLUE_OK = re.compile(r"(?:strncmp|strncasecmp|memcmp|starts_with|strip_prefix|hasPrefix|strstr)\s*[(:][^;{}]*$")
NVRM610 = re.compile(r"\bnvrm610_(?:on|value)\s*\(\s*\"([A-Z0-9_]+)\"")
BOOT_CALL = re.compile(r"\bPE_parse_boot_argn\s*\(")
BOOT_WHOLE = re.compile(r"\b(PE_boot_args|PE_parse_boot_arg|PE_parse_boot_arg_str)\s*\(")
BOOT_USER = re.compile(r"\"((?:-?n[vm][a-z0-9_]+)=[0-9a-z]+)\"")
DRIVER_BOOT = re.compile(r"-?n[vm]")
SYSCTL = re.compile(r"\bSYSCTL_([A-Z0-9]+)\s*\(")
SYSCTL_NOT_DECL = ("DECL", "IN", "OUT")
DEFINE = re.compile(r"^[ \t]*#[ \t]*define[ \t]+(\w+)\(([^)]*)\)((?:[^\n]*\\\n)*[^\n]*)", re.M)
FUNC_HEAD = re.compile(r"\b([A-Za-z_]\w*)\s*\(([^(){};]*)\)\s*(?:const\s*)?\{")
NOT_FUNC = ("if", "for", "while", "switch", "catch", "return", "sizeof")
FILE_LIT = re.compile(r"\"((?:/private)?/tmp/nv[a-z]+[-.][A-Za-z0-9_.%-]+|/Library/NullMoth/(?:nvfb-small|gamemode-active)|/Library/GPUBundles/nvmtl/no-failed-air|"
                      r"/Library/GPUBundles/nvmtl-gpupass-probe|/Library/GPUBundles/nvmtl-allow\.txt|/Library/GPUBundles/nvmtl-gpu-policy\.txt|"
                      r"/Library/GPUBundles/nvmtl/nvrm610\.conf|/usr/local/etc/nvencvt\.conf)\"")
PROC_CMP = re.compile(r"\b(?:strcmp|strncmp|strstr)\s*\(\s*(?:me|pn|nm|process|exe|getprogname\(\))\s*,\s*\"([^\"]+)\"")
PROC_SRC = re.compile(r"getprogname\s*\(|proc_selfname\s*\(|const char \*process|const char \*exe\b")
PROC_NOT = ("MTL", "GeForce RTX 50", "andler", "eedback")   # compares against something that is not a process name
BUILD_VAR = re.compile(r"\$\{?(" + PREFIX + r"[A-Z0-9_]*[A-Z0-9])")
STD_ENV = re.compile(r"(HOME|TMPDIR|XDG_CACHE_HOME|PATH|USER|VK_[A-Z0-9_]+)$")

HOW_NEW = ("An improvement ships ON and needs no switch. If the change is risky, give it ONE opt-out read once at start "
           "and add a row of tier escape (default 'on (...)', since %s, a review version). A hook that only injects a fault "
           "for a test is tier test. Something that only observes is tier diag with a review version. A switch somebody "
           "has to set to get the fix (an opt-in) is what this check exists to refuse." % NEXT)
HOW_SYSCTL = ("Make it CTLFLAG_RD and prove the path with a counter. If a default path needs a way out, read ONE boot "
              "argument once at start and register it as tier escape; a value that changes while the driver runs is "
              "a second code path nobody tested.")


def strip_comments(text, rust=False):
    """The text with // and /* */ comments blanked (newlines kept, so positions still give the line)."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1]); i = j + 1
        elif c == "'" and not rust:
            j = i + 1
            while j < n and text[j] not in "'\n":
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1]); i = j + 1
        elif text.startswith("//", i):
            j = text.find("\n", i); i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2); j = n if j < 0 else j + 2
            out.append("".join(ch if ch == "\n" else " " for ch in text[i:j])); i = j
        else:
            out.append(c); i += 1
    return "".join(out)


def lineno(t, pos):
    return t.count("\n", 0, pos) + 1


def call_args(t, i):
    """t[i] is '('. -> (the arguments as text, the index after the matching ')'), or (None, i) when it never closes."""
    args, depth, start, j, n = [], 0, i + 1, i, len(t)
    while j < n:
        c = t[j]
        if c == '"':
            j += 1
            while j < n and t[j] != '"':
                j += 2 if t[j] == "\\" else 1
        elif c == "'" and re.match(r"'(?:\\.|[^\\'])'", t[j:j + 4]):
            j += 3 if t[j + 1] == "\\" else 2
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                args.append(t[start:j]); return args, j + 1
        elif c == "," and depth == 1:
            args.append(t[start:j]); start = j + 1
        j += 1
    return None, i


def literal(arg):
    """The text of an argument that is exactly one string literal, else None."""
    m = re.fullmatch(r"\s*@?\"((?:[^\"\\]|\\.)*)\"\s*", arg)
    return m.group(1) if m else None


def strings_of(arg):
    """The text of an argument made of string literals ("a" "b" is one string), '' when it holds none."""
    return "".join(re.findall(r"\"((?:[^\"\\]|\\.)*)\"", arg))


def blank_strings(t):
    """The text with the inside of every string literal blanked, same length: a name in a log line is not a use."""
    out, i, n = [], 0, len(t)
    while i < n:
        if t[i] == '"':
            j = i + 1
            while j < n and t[j] != '"':
                j += 2 if t[j] == "\\" else 1
            out.append('"' + " " * (min(j, n) - i - 1) + '"'); i = j + 1
        else:
            out.append(t[i]); i += 1
    return "".join(out)[:n]


def enclosing_function(t, pos):
    """The function whose body holds t[pos]: (name, parameter names), or None."""
    best = None
    for m in FUNC_HEAD.finditer(t, max(0, pos - 40000), pos):
        if m.group(1) in NOT_FUNC:
            continue
        depth, j = 1, m.end()
        while j < pos and depth > 0:
            c = t[j]
            if c == '"':
                j += 1
                while j < pos and t[j] != '"':
                    j += 2 if t[j] == "\\" else 1
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            j += 1
        if depth > 0:
            best = m
    if not best:
        return None
    params = [(re.findall(r"[A-Za-z_]\w*", p) or [""])[-1] for p in best.group(2).split(",")]
    return best.group(1), params


def tracked(root, sub, exts):
    base = root / sub
    return sorted(p for p in base.rglob("*") if p.is_file() and p.suffix in exts) if base.exists() else []


def nvk_chain(root):
    """-> (patch files build/build_nvk.sh applies by default, other nvk/ files it copies into the tree, problems).
    The chain is the CHAIN= of its default release, then whatever it appends after the case, with the default of
    NVK_EXTRA_PATCHES. A patch a later revision adds to either is scanned without a change here."""
    p = root / "build/build_nvk.sh"
    if not p.is_file():
        return [], [], []
    sh = p.read_text(errors="replace")
    body = "\n".join(l for l in sh.splitlines() if not l.lstrip().startswith("#"))
    rel = re.search(r"REL=\$\{4:-([\w.]+)\}", body)
    case = re.search(r"case \$REL in\n(.*?)\n\s*esac", body, re.S)
    arm = rel and case and re.search(r"^\s*" + re.escape(rel.group(1)) + r"\)(.*?);;", case.group(1), re.S | re.M)
    if not arm:
        return [], [], ["build/build_nvk.sh: the patch chain of its default release was not found (the registry reads the names NVK adds from it): "
                        "keep REL=${4:-<release>} and a `<release>) ... CHAIN=\"...\" ;;` arm, or teach nvk_chain() in tools/switch_registry.py the new form"]
    fixes = re.search(r"FIXES=\$NVK/(\S+)", arm.group(1))
    words = []
    for m in re.finditer(r"CHAIN=\"([^\"]*)\"", arm.group(1) + "\n" + body[case.end():]):
        if re.search(r"[^\w@./\s${}:-]|\$\(", m.group(1)):
            continue                                                    # a command that edits the chain, not a list of names
        text = re.sub(r"\$\{\w+:-([^}]*)\}", r"\1", m.group(1))        # ${NVK_EXTRA_PATCHES:-default} -> default
        words += [w for w in text.split() if not w.startswith("$")]
    files = []
    for w in words:
        if w == "@FIXES":
            if fixes:
                files.append(root / "nvk" / fixes.group(1))
        else:
            files.append(root / "nvk" / (w + ".patch"))
    copied = sorted({root / "nvk" / m for m in re.findall(r"\$NVK/([\w./-]+\.(?:h|c|comp|inc))\b", body)})
    return files, [c for c in copied if c.is_file()], []


def built_kext_sources(root):
    """Every C, C++ and header file under kexts/, found by walking the tree: a new file cannot hide a switch."""
    return [p for p in tracked(root, "kexts", C_EXT) if not str(p.relative_to(root)).startswith(UNBUILT)]


def sysctl_decls(t):
    """Every sysctl a file declares, directly or through a macro of its own -> [(pos, name, access, description)],
    plus the problems (a declaration the scanner cannot read is a problem, never a silent miss)."""
    out, problems = [], []

    def one(pos, args, bind=None):
        bind = bind or {}
        def sub(x):
            return "".join(bind.get(piece.strip(), piece.strip()) for piece in x.split("##")).strip()
        parent, name = args[0].strip().lstrip("_"), sub(args[2])
        access = " ".join(bind.get(w, w) for w in re.findall(r"[A-Za-z_]\w*", args[3]))
        last = args[-1].strip()
        desc = strings_of(bind.get(last, last))
        if not re.fullmatch(r"\w+", parent) or not re.fullmatch(r"\w+", name):
            problems.append((pos, f"a sysctl is declared with a name the registry cannot read ({args[2].strip()}): spell the name out"))
            return
        out.append((pos, parent.replace("_", ".") + "." + name, access, desc))

    macros = {}
    for m in DEFINE.finditer(t):
        inner = SYSCTL.search(m.group(3))
        if not inner or inner.group(1) in SYSCTL_NOT_DECL:
            continue
        args, _ = call_args(m.group(3), inner.end() - 1)
        if args and len(args) >= 5 and args[0].strip().startswith("_"):
            macros[m.group(1)] = ([p.strip() for p in m.group(2).split(",")], args)
    defines = [(m.start(), m.end()) for m in DEFINE.finditer(t)]
    in_define = lambda pos: any(a <= pos < b for a, b in defines)
    for m in SYSCTL.finditer(t):
        if m.group(1) in SYSCTL_NOT_DECL or in_define(m.start()):
            continue
        args, _ = call_args(t, m.end() - 1)
        if args and len(args) >= 5 and args[0].strip().startswith("_"):
            one(m.start(), args)
    for name, (params, inner) in macros.items():
        for m in re.finditer(r"\b%s\s*\(" % re.escape(name), t):
            if in_define(m.start()):
                continue
            args, _ = call_args(t, m.end() - 1)
            if args is None or len(args) != len(params):
                problems.append((m.start(), f"{name}(...) declares a sysctl and the registry cannot read this use of it")); continue
            one(m.start(), inner, {p: a.strip() for p, a in zip(params, args)})
    return out, problems


def scan(root):
    """-> ({kind: {name: {"sites": [file:line], "owner": set, "set_only": bool, ...}}}, problems)"""
    found = {k: defaultdict(lambda: dict(sites=[], owner=set(), set_only=True)) for k in KINDS}
    problems = []

    def add(kind, name, site, owner, read=True):
        e = found[kind][name]
        e["sites"].append(site); e["owner"].add(owner)
        if read:
            e["set_only"] = False

    def env_in(t, r, owner, rs=False):
        for m in (ENV_READ_RS if rs else ENV_READ).finditer(t):
            add("env", m.group(1), f"{r}:{lineno(t, m.start())}", owner)
        for m in ENV_LIT.finditer(t):
            add("env", m.group(1), f"{r}:{lineno(t, m.start())}", owner, read=rs or not ENV_SET.search(t[max(0, m.start() - 12):m.start()]))
        for m in ENV_GLUE.finditer(t):
            if not ENV_GLUE_OK.search(t[max(0, m.start() - 160):m.start()]):
                problems.append(f"{r}:{lineno(t, m.start())}: a switch name is put together at run time from \"{m.group(1)}\": the registry cannot "
                                f"list a name it cannot read. Spell every name out as one string literal")

    # --- environment: C and Objective-C (plugin, DLSS host, vendor libraries)
    for sub, owner in (("plugin", "plugin"), ("dlss", "dlss"), ("vendor", "nvencvt")):
        for p in tracked(root, sub, C_EXT):
            r = str(p.relative_to(root)); t = strip_comments(p.read_text(errors="replace"))
            env_in(t, r, owner)
            for m in FILE_LIT.finditer(t):
                add("file", m.group(1), f"{r}:{lineno(t, m.start())}", owner)
            if PROC_SRC.search(t):
                for m in PROC_CMP.finditer(t):
                    add("procname", m.group(1), f"{r}:{lineno(t, m.start())}", owner)
            if "kern.bootargs" in t:
                for m in BOOT_USER.finditer(t):
                    add("bootarg", m.group(1).split("=")[0], f"{r}:{lineno(t, m.start())}", owner)
    # --- environment: the translator (Rust)
    for p in tracked(root, "translator", (".rs",)):
        r = str(p.relative_to(root))
        if "/tests/" in r or "/target/" in r or r.endswith("build.rs"):
            continue
        env_in(strip_comments(p.read_text(errors="replace"), rust=True), r, "translator", rs=True)
    # --- environment: the NVK patch chain. Names a patch adds and no later patch of the chain takes away again.
    chain, copied, chain_problems = nvk_chain(root)
    problems += chain_problems
    live, where = defaultdict(int), defaultdict(list)
    for pf in chain:
        if not pf.is_file():
            problems.append(f"{pf.relative_to(root)}: named by build/build_nvk.sh and missing"); continue
        for i, line in enumerate(pf.read_text(errors="replace").splitlines(), 1):
            if line.startswith(("+++", "---")) or line[:1] not in ("+", "-"):    # an empty line is neither
                continue
            body = line[1:]
            if re.match(r"\s*(//|/\*|\*( |/|$))", body):
                continue
            names = {m.group(1) for m in ENV_LIT.finditer(body)} | {m.group(1) for m in ENV_READ_RS.finditer(body)}
            names |= {"NVRM_610_" + m.group(1) for m in NVRM610.finditer(body)}
            names |= {m.group(1) for m in PROC_CMP.finditer(body)} if "getprogname" in body or "pn," in body else set()
            for nme in names:
                live[nme] += 1 if line[0] == "+" else -1
                if line[0] == "+":
                    where[nme].append(f"{pf.relative_to(root)}:{i}")
            if line[0] == "+":
                for m in ENV_GLUE.finditer(body):
                    if not ENV_GLUE_OK.search(body[:m.start()]):
                        problems.append(f"{pf.relative_to(root)}:{i}: a switch name is put together at run time from \"{m.group(1)}\": spell every name out as one string literal")
    for nme, cnt in live.items():
        if cnt > 0:
            kind = "env" if re.match(r"[A-Z0-9_]+$", nme) else "procname"
            for s in where[nme][:2]:
                add(kind, nme, s, "nvk")
    for p in copied:
        env_in(strip_comments(p.read_text(errors="replace")), str(p.relative_to(root)), "nvk")
    # --- kexts: boot arguments, sysctls, flag files, process names
    texts = {}
    for p in built_kext_sources(root):
        r = str(p.relative_to(root)); t = strip_comments(p.read_text(errors="replace"))
        owner = "NVAccel" if r.startswith("kexts/NVRM/accel/") else r.split("/")[1]
        texts[r] = (t, owner)
        for m in BOOT_WHOLE.finditer(t):
            problems.append(f"{r}:{lineno(t, m.start())}: {m.group(1)} reads the boot arguments in a way the registry cannot list (and PE_boot_args is not "
                            f"exported to kexts on macOS 15): read each one with PE_parse_boot_argn(\"literal\", ...)")
        decls, bad = sysctl_decls(t)
        problems += [f"{r}:{lineno(t, pos)}: {msg}" for pos, msg in bad]
        for pos, name, access, desc in decls:
            e = found["sysctl"][name]
            e["sites"].append(f"{r}:{lineno(t, pos)}"); e["owner"].add(owner); e["set_only"] = False
            words = access.split()
            e["writable"] = e.get("writable", False) or "CTLFLAG_RW" in words or "CTLFLAG_WR" in words
            e["anybody"] = e.get("anybody", False) or "CTLFLAG_ANYBODY" in words
            e["desc"] = e.get("desc") or desc
            odd = [w for w in words if not w.startswith(("CTLFLAG_", "CTLTYPE_"))]
            if odd:
                problems.append(f"{r}:{lineno(t, pos)}: sysctl {name}: its access flags are not spelled out ({' '.join(odd)}), so the registry cannot tell "
                                f"whether it is writable: write the CTLFLAG_ names in the declaration")
        if PROC_SRC.search(t):
            for m in PROC_CMP.finditer(t):
                add("procname", m.group(1), f"{r}:{lineno(t, m.start())}", owner)
        for m in FILE_LIT.finditer(t):
            add("file", m.group(1), f"{r}:{lineno(t, m.start())}", owner)
    # boot arguments: a literal name, or a function that passes its own parameter on (then its callers are held to it)
    wrappers, queue = {}, []
    blank = {r: blank_strings(t) for r, (t, owner) in texts.items()}
    for r, (t, owner) in texts.items():
        for m in BOOT_CALL.finditer(blank[r]):
            args, _ = call_args(t, m.end() - 1)
            if not args:
                continue
            lit = literal(args[0])
            if lit is not None:
                add("bootarg", lit, f"{r}:{lineno(t, m.start())}", owner); continue
            fn = enclosing_function(t, m.start())
            arg = args[0].strip()
            if fn and arg in fn[1]:
                if fn[0] not in wrappers:
                    wrappers[fn[0]] = fn[1].index(arg); queue.append(fn[0])
            else:
                problems.append(f"{r}:{lineno(t, m.start())}: PE_parse_boot_argn with a name that is not a literal ({arg}): the registry cannot list a "
                                f"boot argument it cannot read. Pass the name as a string literal")
    while queue:
        w = queue.pop(); idx = wrappers[w]
        for r, (t, owner) in texts.items():
            for m in re.finditer(r"\b%s\b" % re.escape(w), blank[r]):
                rest = t[m.end():m.end() + 40].lstrip()
                if not rest.startswith("("):
                    if w not in BOOT_PROBES:
                        problems.append(f"{r}:{lineno(t, m.start())}: {w} passes its argument to PE_parse_boot_argn and is used here without a call, so the "
                                        f"names it will be asked about cannot be listed: call it with a literal name")
                    continue
                args, _ = call_args(t, t.index("(", m.end()))
                if not args or idx >= len(args):
                    continue
                lit, arg = literal(args[idx]), args[idx].strip()
                if lit is not None:
                    add("bootarg", lit, f"{r}:{lineno(t, m.start())}", owner)
                elif re.search(r"\bchar\b", arg):
                    continue                                    # its own declaration or definition
                else:
                    fn = enclosing_function(t, m.start())
                    if fn and arg in fn[1] and fn[0] != w:
                        if fn[0] not in wrappers:
                            wrappers[fn[0]] = fn[1].index(arg); queue.append(fn[0])
                    else:
                        problems.append(f"{r}:{lineno(t, m.start())}: {w}() reads a boot argument and is called with a name that is not a literal ({arg}): "
                                        f"pass the name as a string literal")
    # --- build scripts: a variable with a driver prefix decides what goes into the shipped driver, or where it is built
    for p in sorted((root / "build").glob("*.sh")) if (root / "build").is_dir() else []:
        r = str(p.relative_to(root))
        for i, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            for m in BUILD_VAR.finditer(line):
                add("build", m.group(1), f"{r}:{i}", "build")
    for name in [n for n in found["procname"] if n.startswith("__") or n in PROC_NOT]:
        del found["procname"][name]
    return found, problems


# ---------------------------------------------------------------------------------------------------------------------
# the registry file
class Registry:
    def __init__(self):
        self.head, self.ceil, self.pending, self.rows, self.errs = [], {}, set(), {}, []

    def text(self):
        out = list(self.head)
        out += [f"#ceiling {t} {o} {n}" for (t, o), n in sorted(self.ceil.items())]
        out += [f"#pending {k} {n}" for k, n in sorted(self.pending, key=lambda x: (KINDS.index(x[0]) if x[0] in KINDS else 99, x[1]))]
        for (kind, name), r in sorted(self.rows.items(), key=lambda x: (KINDS.index(x[0][0]), x[0][1])):
            out.append("\t".join((kind, name, r["tier"], r["default"], r["since"], r["review"], r["purpose"])))
        return "\n".join(out) + "\n"


def parse(text):
    reg = Registry()
    for i, line in enumerate(text.splitlines(), 1):
        if line.startswith("#ceiling"):
            f = line.split()
            if len(f) != 4 or not f[3].isdigit():
                reg.errs.append(f"{REG}:{i}: a ceiling line is '#ceiling <what> <whose> <number>'"); continue
            reg.ceil[(f[1], f[2])] = int(f[3]); continue
        if line.startswith("#pending"):
            f = line.split()
            if len(f) != 3 or f[1] not in KINDS:
                reg.errs.append(f"{REG}:{i}: a pending line is '#pending <kind> <name>'"); continue
            reg.pending.add((f[1], f[2])); continue
        if line.startswith("#"):
            if not reg.rows:
                reg.head.append(line)
            continue
        if not line.strip():
            continue
        f = line.split("\t")
        if len(f) != 7:
            reg.errs.append(f"{REG}:{i}: {len(f)} columns, not 7 (kind, name, tier, default, since, review, purpose; tab separated)"); continue
        kind, name, tier, default, since, review, purpose = f
        if kind not in KINDS or tier not in TIERS:
            reg.errs.append(f"{REG}:{i}: unknown kind or tier ({kind}, {tier}); kinds are {', '.join(KINDS)}, tiers {', '.join(TIERS)}"); continue
        if (kind, name) in reg.rows:
            reg.errs.append(f"{REG}:{i}: {kind} {name} is listed twice"); continue
        reg.rows[(kind, name)] = dict(tier=tier, default=default, since=since, review=review, purpose=purpose, line=i)
    return reg


def load(root):
    p = root / REG
    if not p.is_file():
        reg = Registry(); reg.errs.append(f"{REG} does not exist"); return reg
    return parse(p.read_text())


def ver(s):
    return tuple(int(x) for x in re.findall(r"\d+", s)[:3])


def tree_version(root):
    p = root / "CHANGELOG.md"
    m = re.search(r"^## (\d+(?:\.\d+)*)", p.read_text(errors="replace"), re.M) if p.is_file() else None
    return m.group(1) if m else BASE


def next_version(version):
    """The `since` of a row added now: the release in preparation."""
    return version if ver(version) > ver(BASE) else NEXT


def is_new(row):
    return ver(row["since"]) > ver(BASE)


def rules(reg, found, version, catalog_keys=None):
    """-> list of problems. Pure over its arguments, so --selftest can feed it synthetic input."""
    rows, ceil, out = reg.rows, reg.ceil, []
    for kind in KINDS:
        for name, e in sorted(found[kind].items()):
            row, site = rows.get((kind, name)), e["sites"][0]
            if kind == "sysctl" and e.get("anybody"):
                out.append(f"sysctl {name} ({site}) is writable by any user (CTLFLAG_ANYBODY): a driver switch is root's at most. Remove the flag")
            if row is None:
                if kind == "sysctl" and not e.get("writable"):
                    out.append(f"sysctl {name} is declared at {site} and has no row in {REG}: it is read-only, so its row is its description. "
                               f"Run `python3 tools/switch_registry.py sync` and commit the result")
                elif kind == "sysctl":
                    out.append(f"sysctl {name} is declared writable at {site} and has no row in {REG}: a new writable sysctl is refused. {HOW_SYSCTL}")
                else:
                    out.append(f"{kind} {name} is read at {site} and has no row in {REG}. {HOW_NEW}")
                continue
            if row["tier"] == "retired":
                out.append(f"{REG}:{row['line']}: {kind} {name} is marked retired and {site} still reads it: give the row the tier the switch has, or take the read out")
            if kind == "sysctl":
                says_writable = row["default"].startswith("writable")
                if e.get("writable") and (is_new(row) or row["tier"] not in WRITABLE_TIERS):
                    out.append(f"sysctl {name} ({site}) is writable and its row is tier {row['tier']}, since {row['since']}: a default path has no writable "
                               f"switch, and no writable sysctl is added after {BASE}. {HOW_SYSCTL}")
                elif e.get("writable") and not says_writable:
                    out.append(f"{REG}:{row['line']}: sysctl {name} was read-only and {site} now declares it writable: refused. {HOW_SYSCTL}")
                elif not e.get("writable") and says_writable:
                    out.append(f"{REG}:{row['line']}: sysctl {name} is read-only at {site} and its row still says writable: "
                               f"run `python3 tools/switch_registry.py sync` and commit the result")
    dev, tests, base = defaultdict(int), 0, 0
    for key in sorted(reg.pending - set(rows)):
        out.append(f"{REG}: '#pending {key[0]} {key[1]}' names no row: delete the line")
    if reg.pending and ver(version) > ver(BASE):
        out.append(f"{REG}: {len(reg.pending)} row(s) are still marked #pending and the tree is {version}: a release carries no row written ahead of "
                   f"its code. Run `python3 tools/switch_registry.py finalize`")
    for (kind, name), row in sorted(rows.items()):
        e, at, tier, new = found[kind].get(name), f"{REG}:{row['line']}: {kind} {name}", row["tier"], is_new(row)
        if tier != "retired" and e is None and (kind, name) not in reg.pending:
            out.append(f"{at} is read nowhere: delete the row, or set its tier to retired (and say in its purpose what happened to it) "
                       f"so a machine that still sets it can be told")
        if not row["default"].strip() or not row["purpose"].strip():
            out.append(f"{at} has no default or no purpose: say what is in effect when it is not set, and what it is for")
        if not ver(row["since"]):
            out.append(f"{at}: since is {row['since']!r}, not a release")
        if row["review"] not in ("-", "") and not ver(row["review"]):
            out.append(f"{at}: review is {row['review']!r}, not a release or '-'")
        if tier not in ("dev", "diag", "internal", "retired") and (len(row["purpose"]) < 20 or row["purpose"].startswith(UNREVIEWED)):
            out.append(f"{at}: the purpose of a {tier} switch must say what it does for its user, in a sentence")
        if not new:
            base += 1
        if new and tier not in NEW_TIERS:
            what = {"dev": "an experiment or tuning number", "hardware": "a switch a machine needs before a path works",
                    "recovery": "a way out without a review date", "install": "a new required boot argument"}[tier]
            out.append(f"{at} is new (since {row['since']}) and tier {tier}, {what}: refused. {HOW_NEW} "
                       f"A path that depends on the machine checks for it at run time and falls back to the {BASE} behaviour when the check fails")
        if new and kind == "sysctl" and tier not in ("diag", "retired"):
            out.append(f"{at}: a sysctl added after {BASE} is a read-only counter (tier diag). {HOW_SYSCTL}")
        if new and tier == "internal":
            if kind == "env" and e is not None and not e["set_only"] and not STD_ENV.match(name):
                out.append(f"{at} is tier internal and the source reads it: internal is for a variable the driver only sets, or a standard one. {HOW_NEW}")
            if kind == "bootarg" and DRIVER_BOOT.match(name):
                out.append(f"{at} is tier internal and is named like a boot argument of this driver: internal is for another component's argument "
                           f"the driver reads to adapt. {HOW_NEW}")
        if tier == "escape" or (new and tier == "diag" and kind != "sysctl"):
            if row["review"] in ("-", ""):
                out.append(f"{at} needs a review version: the release at which the switch is deleted (the change has proven itself) or renewed with a reason")
            elif ver(version) >= ver(row["review"]):
                out.append(f"{at} was due for review at {row['review']} and the tree is {version}: promote it (delete the switch and its row), "
                           f"or renew it: a later review version and the reason in its purpose")
        if tier == "escape" and not re.match(r"on\b", row["default"]):
            out.append(f"{at} is an escape and its default is {row['default']!r}: the change it guards must be on when the switch is not set. "
                       f"Write the default as 'on (<what the opt-out spelling does>)'")
        if tier == "test":
            tests += 1
            if not row["default"].startswith("off"):
                out.append(f"{at} is a test hook and its default is {row['default']!r}: a test hook does nothing unless it is set ('off (...)'). "
                           f"If it changes a default path, it is not a test hook")
        if tier == "dev" and e is not None:
            for o in e["owner"]:
                dev[o] += 1
    unrev = sum(1 for r in rows.values() if r["purpose"].startswith(UNREVIEWED))
    for what, whose, n, hint in [("unreviewed", "all", unrev, "write the purpose of the row you added; a new row says what it is for"),
                                 ("test", "all", tests, "a test hook is rare: raise the ceiling in the same commit only for a hook a recovery path cannot be exercised without"),
                                 ("base", "all", base, f"a switch added now is since {next_version(version)}, not {BASE}; then rule 3 says what it may be")] + \
                                [("dev", o, n, "delete one before adding one; a ceiling only goes down") for o, n in sorted(dev.items())]:
        c = ceil.get((what, whose))
        if c is None and (n or what in ("unreviewed", "base")):
            out.append(f"{REG}: no '#ceiling {what} {whose} <n>' line ({n} there now)")
        elif c is not None and n > c:
            out.append(f"{n} rows count against '#ceiling {what} {whose} {c}': {hint}")
    if catalog_keys is not None:
        have = {name for (kind, name), r in rows.items() if r["tier"] == "setting" and kind in ("bootarg", "env")}
        for k in sorted(catalog_keys - {name for (kind, name) in rows}):
            out.append(f"tools/nullmoth_settings.py offers {k}, which has no row: add it with tier setting")
        for k in sorted(have - catalog_keys):
            out.append(f"{k} is tier setting and tools/nullmoth_settings.py does not offer it: a setting has a control there, anything else has another tier")
    return out


def catalog(root):
    """The boot arguments and settings-file keys the settings tool offers: {key: one line about it}."""
    p = root / "tools/nullmoth_settings.py"
    if not p.is_file():
        return None
    t = p.read_text()
    out = {k: "" for k in re.findall(r"\"(?:bootarg|conf)\",\s*\"([A-Za-z0-9_-]+)\"", t)}
    for m in re.finditer(r"S\(\"[^\"]+\",\s*\"[^\"]+\",\s*\"((?:[^\"\\]|\\.)*)\",\s*\"(?:bootarg|conf)\",\s*\"([A-Za-z0-9_-]+)\",\s*\"\w+\",\s*\"((?:[^\"\\]|\\.)*)\"", t):
        out[m.group(2)] = f"{m.group(1)}: {m.group(3)}"
    return out


# ---------------------------------------------------------------------------------------------------------------------
# generated views
def files_of(e):
    """Where a switch is read, as file names: line numbers would make the generated page stale on every edit."""
    names = sorted({s.rsplit(":", 1)[0] for s in e["sites"]})
    return ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")


def render_md(reg, found):
    rows, cell = reg.rows, lambda s: s.replace("|", "/").replace("<", "\\<")
    out = ["# Switches", "",
           f"Generated by `tools/switch_registry.py gen` from `{REG}` and the source. Do not edit; `tools/test_switch_registry.py` compares.", "",
           "An ordinary install sets the boot arguments of tier `install` and nothing else. Every other switch is optional, and",
           "`sysctl debug.nvrm_switches_nondefault` is 0 on a machine that runs the default paths. To see what a machine has set, and",
           "what each of those is: `python3 tools/switch_registry.py doctor`.", "",
           "| tier | meaning |", "|---|---|",
           "| install | part of the documented install line; the driver does not start without it |",
           "| setting | a preference with a control in `tools/nullmoth_settings.py` |",
           "| hardware | depends on the machine (a laptop, a panel, a second card, firmware) |",
           "| recovery | makes the driver stand down or start in a safer way |",
           "| escape | the one opt-out of a change that is on by default; reviewed at the release in its `review` column |",
           "| test | a hook that injects a fault so a recovery can be exercised; does nothing unless set |",
           "| diag | observes only: a log, a dump, a counter |",
           "| dev | left from development; none may be added and their number only falls |",
           "| internal | not a user switch |",
           "| retired | a name the source no longer reads |", ""]
    count = defaultdict(int)
    for (kind, name), r in rows.items():
        count[(kind, r["tier"])] += 1
    out += ["| kind | " + " | ".join(TIERS) + " | all |", "|---|" + "---|" * (len(TIERS) + 1)]
    for k in KINDS:
        out.append(f"| {k} | " + " | ".join(str(count[(k, t)] or "") for t in TIERS) + f" | {sum(count[(k, t)] for t in TIERS)} |")
    for tier, title, lead in (("escape", "The ways back", "Each of these turns one change that is on by default back off. None is needed on a machine that works."),
                              ("test", "Test hooks", "Each of these does nothing unless it is set.")):
        pick = [(k, n, r) for (k, n), r in sorted(rows.items(), key=lambda x: (KINDS.index(x[0][0]), x[0][1])) if r["tier"] == tier]
        if pick:
            out += ["", f"## {title}", "", lead, "", "| kind | name | default | review | purpose |", "|---|---|---|---|---|"]
            out += [f"| {k} | `{n}` | {cell(r['default'])} | {r['review']} | {cell(r['purpose'])} |" for k, n, r in pick]
    for k in KINDS:
        out += ["", f"## {k}", "", "| name | tier | default | since | review | purpose | read in |", "|---|---|---|---|---|---|---|"]
        for (kind, name), r in sorted(rows.items()):
            if kind != k:
                continue
            e = found[k].get(name)
            out.append(f"| `{name}` | {r['tier']} | {cell(r['default'])} | {r['since']} | {r['review']} | {cell(r['purpose'])} | {files_of(e) if e else ''} |")
    return "\n".join(out) + "\n"


def render_header(reg):
    names = sorted((name, r["tier"]) for (kind, name), r in reg.rows.items() if kind == "bootarg")
    out = ["/* nvrm_switch_table.h - generated by tools/switch_registry.py gen from tools/switches.tsv. Do not edit.",
           "   The boot arguments this driver reads (and the retired ones), sorted by name, for nvrm_switch_report.h. */",
           "#ifndef NVRM_SWITCH_TABLE_H", "#define NVRM_SWITCH_TABLE_H",
           "enum { " + ", ".join(f"NVRM_SW_{t.upper()} = {i}" for i, t in enumerate(TIERS)) + f", NVRM_SW_TIER_COUNT = {len(TIERS)} }};",
           "struct nvrm_switch_name { const char *name; unsigned char tier; };",
           "static const struct nvrm_switch_name nvrm_switch_names[] = {"]
    out += [f"    {{ \"{n}\", NVRM_SW_{t.upper()} }}," for n, t in names]
    out += ["};", f"#define NVRM_SWITCH_COUNT {len(names)}u", "#endif", ""]
    return "\n".join(out)


def stale(root, reg, found):
    return [rel for rel, text in ((GEN_MD, render_md(reg, found)), (GEN_H, render_header(reg)))
            if not (root / rel).is_file() or (root / rel).read_text() != text]


def cmd_gen(root, reg=None, found=None):
    reg = reg or load(root)
    if found is None:
        found, _ = scan(root)
    for rel, text in ((GEN_MD, render_md(reg, found)), (GEN_H, render_header(reg))):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    print(f"wrote {GEN_MD} and {GEN_H}")
    return 0


def cmd_check(root):
    reg = load(root)
    found, problems = scan(root)
    cat = catalog(root)
    errs = reg.errs + problems + rules(reg, found, tree_version(root), set(cat) if cat is not None else None)
    errs += [f"{rel} is not what `python3 tools/switch_registry.py gen` writes: run it and commit the result" for rel in stale(root, reg, found)]
    for e in errs:
        print("FAIL " + e)
    n = ", ".join(f"{k} {len(found[k])}" for k in KINDS)
    print(f"{len(reg.rows)} rows ({len(reg.pending)} pending); the source reads {n}; {len(errs)} problem(s)")
    return 1 if errs else 0


def sync(reg, found, version, final=False):
    """Bring the registry up to the tree where that needs no judgement. -> what was done, one line each."""
    did, since = [], next_version(version)
    for name, e in sorted(found["sysctl"].items()):
        row = reg.rows.get(("sysctl", name))
        if row is None:
            if not e.get("writable") and e.get("desc"):
                reg.rows[("sysctl", name)] = dict(tier="diag", default="read-only", since=since, review="-", purpose=e["desc"].replace("\t", " "), line=0)
                did.append(f"added sysctl {name} (read-only: {e['desc']})")
        elif not e.get("writable") and row["default"].startswith("writable"):
            row["default"] = "read-only"
            if row["tier"] == "dev":
                row["tier"] = "diag"
            if e.get("desc"):
                row["purpose"] = e["desc"].replace("\t", " ")
            did.append(f"sysctl {name} is read-only now: its row says so")
    for key in sorted(k for k, r in reg.rows.items() if k[0] == "sysctl" and r["tier"] == "diag" and k[1] not in found["sysctl"] and k not in reg.pending):
        del reg.rows[key]
        did.append(f"dropped sysctl {key[1]}: a read-only counter that left with its code")
    if final and reg.pending:
        late = sorted(k for k in reg.pending if k in reg.rows and k[1] not in found[k[0]])
        did.append(f"removed {len(reg.pending)} #pending mark(s)" + (f"; {len(late)} of those rows have no code and are errors now" if late else ""))
        reg.pending = set()
    counts = {("unreviewed", "all"): sum(1 for r in reg.rows.values() if r["purpose"].startswith(UNREVIEWED)),
              ("base", "all"): sum(1 for r in reg.rows.values() if not is_new(r)),
              ("test", "all"): sum(1 for r in reg.rows.values() if r["tier"] == "test")}
    for (kind, name), r in reg.rows.items():
        if r["tier"] == "dev" and name in found[kind]:
            for o in found[kind][name]["owner"]:
                counts[("dev", o)] = counts.get(("dev", o), 0) + 1
    for key, c in sorted(reg.ceil.items()):
        if key[0] in ("dev", "unreviewed", "base", "test") and counts.get(key, 0) < c:
            reg.ceil[key] = counts.get(key, 0)
            did.append(f"ceiling {key[0]} {key[1]} lowered from {c} to {reg.ceil[key]}")
    return did


def cmd_sync(root, final):
    reg = load(root)
    if reg.errs:
        for e in reg.errs:
            print("FAIL " + e)
        return 1
    found, _ = scan(root)
    did = sync(reg, found, tree_version(root), final)
    (root / REG).write_text(reg.text())
    for d in did:
        print(d)
    print(f"{REG}: {len(did)} change(s)")
    cmd_gen(root, load(root), found)
    return cmd_check(root) if final else 0


# ---------------------------------------------------------------------------------------------------------------------
# bootstrap: a first registry for a tree that has none. Tiers come from what the tree itself says (the settings
# catalogue, the install line, the reviewed writable sysctls) and from the name; purposes of dev and diag rows are left
# as "unreviewed" and counted, so the number can only fall. With --base <checkout of the release the registry starts
# from>, a name that tree does not read is filed as since NEXT and has to pass the rules for a new switch.
INSTALL_BOOT = {"nvfb": "absent: the framebuffer kext does not start (boot-loop guard)", "nvaccel": "absent: the accelerator does not start (boot-loop guard)",
                "nvfbheads": "1 head", "-nvkmsnosmooth": "absent: the firmware picture is carried through the first mode set"}
BOOT_TIER = {
    "recovery": "-nmdrmoff -nmgopfboff -nmintelfboff -nvaccelnoheadless -nvoff nvoffbar -nvrmkeepbar1 -nvrmnoaspm -nvrmnobootscreen -nvrmnodpms "
                "-nvrmnoflip -nvrmnogamma -nvrmnogo -nvrmnopm -nvrmnoshutdown -nvrmsmallbar nvrmmapowner nvfbbootretry nvcursor nvhwvbl nvfbsmall",
    "hardware": "nmflatskip nmgopid nmhdavol nmhybrid nmhybrid20 nmhybriddelay nmhybriddmc nmhybridext nmhybridp2p nmhybridresume nmhybridtc nmkbl "
                "nmkblforce nmkblhid nmsmn nvagdcheads nvfbigpu nvfbigpucache nvfbhotplugpoll nvmaxbpc nvrmacpibl nvrmaspm nvrmfbddc nvrmgps nvrmnvpcf "
                "nvrmtgp nvrtd3 nvrtd3res nvselector nvregdwords",
    "diag": "-nvaccelverbose -nvfbsurvey nvfblog nvfbtrace nvhud nvfbsample nvfbscalerprobe nvrmdiag",
    "internal": "-v gfxrst",
}
BOOT_WORDS = {"recovery": "turns one part of the driver off or back to its safest form, for a machine that does not start",
              "hardware": "depends on the machine (laptop, panel, firmware, second card); cannot be defaulted without it",
              "diag": "observes only", "internal": "not a switch of this driver; read to adapt to it"}
SYSCTL_TIER = {
    "setting": "nvrmfb_color_bpc nvrmfb_color_format nvrmfb_color_range nvrmfb_color_vibrance nvrmfb_flip_latch_auto",
    "hardware": "nvrm_tgp_enable nmintelfb_attach nmintelfb_dmc nvaccel_igpu",
    "recovery": "nvaccel_iop nvaccelfb nvaccel_vm_sticky nvaccel_iop_flip nvaccel_surface_gpu_on nvaccel_iop_async nvrmfb_agdc nvrmfb_agdc_k5 "
                "nvrmfb_vram_display_aware nvrmfb_flip_latch nvrmfb_flip_lean nvrmfb_flip_interval",
    "diag": "nvaccel_crc_head nvaccel_svdiag nvrmfb_vramtest nvrmfb_vramtest_mode",
}
DIAG_WORD = re.compile(r"(DEBUG|DUMP|TRACE|_LOG|LOG_|STATS|PROBE|VERBOSE|PRINT|_HUD|TIMING|REPORT|DIAG|WATCH|VALIDATE|ASSERT|_DBG|SELFTEST)")
FILE_TIER = {"/Library/GPUBundles/nvmtl/nvrm610.conf": ("install", "the settings file the installer writes; the user block is kept on update"),
             "/Library/GPUBundles/nvmtl-allow.txt": ("install", "process names the plugin loads into, written by the installer"),
             "/Library/GPUBundles/nvmtl-gpu-policy.txt": ("setting", "per-application card choice on a machine with two NVIDIA cards"),
             "/Library/NullMoth/nvfb-small": ("recovery", "the same as boot-arg nvfbsmall=1 without a restart into recovery"),
             "/Library/NullMoth/gamemode-active": ("internal", "written by the game-priority helper while a game runs")}
BUILD_DEV = {"NVK_EXTRA_PATCHES": "extra patch files from nvk/ applied after the chain when NVK is built"}


def bootstrap(root, base_root=None):
    found, _ = scan(root)
    old = scan(base_root)[0] if base_root else None
    cat = catalog(root) or {}
    boot = {n: t for t, names in BOOT_TIER.items() for n in names.split()}
    sysw = {"debug." + n: t for t, names in SYSCTL_TIER.items() for n in names.split()}
    reg = Registry()
    reg.head = ["# switches.tsv: every switch the driver reads. Columns: kind, name, tier, default, since, review, purpose (see tools/switch_registry.py).",
                "# A ceiling only goes down: delete or review a row, then lower the number (or run `python3 tools/switch_registry.py sync`)."]
    dev = defaultdict(int)
    for kind in KINDS:
        for name, e in sorted(found[kind].items()):
            default, purpose, since = "unset", UNREVIEWED, BASE if old is None or name in old[kind] else NEXT
            if kind == "env":
                if name in cat:
                    tier, purpose = "setting", cat[name] or "offered by tools/nullmoth_settings.py (see its catalogue for the values)"
                elif e["set_only"] or STD_ENV.match(name):
                    tier, purpose = "internal", ("set by the driver for the libraries it loads" if e["set_only"] else "a standard variable the driver reads")
                elif DIAG_WORD.search(name):
                    tier = "diag"
                else:
                    tier = "dev"
                if "_NO_" in name or name.endswith("_OFF") or "_DISABLE" in name:
                    default = "unset (the path is on)"
            elif kind == "bootarg":
                if name in INSTALL_BOOT:
                    tier, default, purpose = "install", INSTALL_BOOT[name], "part of the install line in README.md; set by the installer's instructions"
                elif name in cat:
                    tier, purpose = "setting", cat[name] or "offered by tools/nullmoth_settings.py (see its catalogue for the values)"
                else:
                    tier = boot.get(name, "dev")
                    purpose = BOOT_WORDS.get(tier, UNREVIEWED)
            elif kind == "sysctl":
                w = e.get("writable")
                tier = sysw.get(name, "dev") if w else "diag"
                default = "writable by root" if w else "read-only"
                purpose = e.get("desc") or UNREVIEWED
            elif kind == "file":
                tier, purpose = FILE_TIER.get(name, ("diag", f"{UNREVIEWED}: a flag or log file; the release plugin refuses /tmp paths"))
                default = "absent"
            elif kind == "build":
                tier, purpose = ("dev", BUILD_DEV[name]) if name in BUILD_DEV else ("internal", "a build host setting (a path, a job count); nothing of it reaches the shipped driver")
            else:
                tier, default, purpose = "internal", "-", "a branch on the process name; not a user switch"
            if tier == "dev":
                for o in e["owner"]:
                    dev[o] += 1
            reg.rows[(kind, name)] = dict(tier=tier, default=default, since=since, review="-", purpose=purpose.replace("\t", " "), line=0)
    reg.ceil = {("dev", o): n for o, n in dev.items()}
    reg.ceil[("unreviewed", "all")] = sum(1 for r in reg.rows.values() if r["purpose"].startswith(UNREVIEWED))
    reg.ceil[("base", "all")] = sum(1 for r in reg.rows.values() if not is_new(r))
    return reg.text()


def release_switches(root, version=BASE):
    """-> {(kind, name)} the tree of a release read, or None when this clone does not have that release (no tag)."""
    tag = "v" + version
    git = ["git", "-C", str(root)]
    if subprocess.run(git + ["rev-parse", "-q", "--verify", tag + "^{commit}"], capture_output=True).returncode:
        return None
    with tempfile.TemporaryDirectory() as d:
        a = subprocess.run(git + ["archive", tag], capture_output=True)
        if a.returncode or subprocess.run(["tar", "xf", "-", "-C", d], input=a.stdout).returncode:
            return None
        found, _ = scan(Path(d))
    return {(k, n) for k in found for n in found[k]}


def misdated(reg, base):
    """Rows that claim to date from the base release and name a switch that release did not read: a new switch filed as
    an old one, which would let it be an opt-in, a development switch or a writable sysctl."""
    return [f"{REG}:{r['line']}: {k[0]} {k[1]} is dated {r['since']}, and the {BASE} release did not read it: it is new. "
            f"Set its since to {NEXT}; rule 3 then says what it may be" for k, r in sorted(reg.rows.items())
            if not is_new(r) and r["tier"] != "retired" and k not in base]


def ceiling_rises(old_text, new_text):
    """Ceilings that are higher in new_text than in old_text, or that old_text did not have and new_text sets above
    nothing (a ratchet a later commit may not loosen)."""
    old, new = parse(old_text).ceil, parse(new_text).ceil
    return [f"ceiling {k[0]} {k[1]} rose from {old[k]} to {v}" for k, v in sorted(new.items()) if k in old and v > old[k]]


# ---------------------------------------------------------------------------------------------------------------------
# doctor: on an installed machine, the switches that are set and what each is. Reads only.
FOREIGN_BOOT = re.compile(r"(nvda_drv|nvda_drv_vrl|nv_disable|nv_spanmodepolicy|nvme)")


def doctor(rows, bootargs, conf_text):
    """-> (lines, counts). bootargs is the kern.bootargs string, conf_text the settings file ('' when absent)."""
    out, cnt = [], defaultdict(int)

    def judge(kind, name, shown):
        row = rows.get((kind, name))
        if row is None:
            cnt["unknown"] += 1; out.append(f"unknown   {shown}: not a switch of this driver version (a misspelling, or one a later version dropped)")
        elif row["tier"] in ("install", "internal"):
            cnt["install"] += 1
        else:
            cnt[row["tier"]] += 1; out.append(f"{row['tier']:9s} {shown}: {row['purpose']}" + (f" [default: {row['default']}]" if row["tier"] == "escape" else ""))

    for tok in bootargs.split():
        name = tok.split("=")[0]
        if DRIVER_BOOT.match(name) and not FOREIGN_BOOT.match(name.lstrip("-")):
            judge("bootarg", name, tok)
    seen = {}
    for line in conf_text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1); seen[k.strip()] = v.strip()
    for k, v in sorted(seen.items()):
        if k.startswith(("NVMTL_", "NVK_", "NVRM_")):
            judge("env", k, f"{k}={v}")
        else:
            cnt["ignored"] += 1; out.append(f"ignored   {k}={v}: the plugin reads only NVMTL_, NVK_ and NVRM_ keys from the settings file")
    return out, dict(cnt)


def cmd_doctor(root, argv):
    reg = load(root)
    if "--bootargs" in argv:
        boot = argv[argv.index("--bootargs") + 1]
    else:
        boot = subprocess.run(["sysctl", "-n", "kern.bootargs"], capture_output=True, text=True).stdout
    conf = Path(argv[argv.index("--conf") + 1]) if "--conf" in argv else Path("/Library/GPUBundles/nvmtl/nvrm610.conf")
    lines, cnt = doctor(reg.rows, boot, conf.read_text(errors="replace") if conf.is_file() else "")
    for l in lines:
        print(l)
    needs = sum(cnt.get(k, 0) for k in NONDEFAULT + ("unknown",))
    print(f"switches set on this machine: {sum(v for k, v in cnt.items() if k != 'install')} beside the install line "
          f"({', '.join(f'{v} {k}' for k, v in sorted(cnt.items()) if k != 'install') or 'none'})")
    print("an ordinary install needs none of them" if not needs else
          f"{needs} of them are not needed by an ordinary install ({', '.join(NONDEFAULT)}, unknown): remove them unless support asked for them")
    return 0


# ---------------------------------------------------------------------------------------------------------------------
def selftest():
    F = lambda: {k: {} for k in KINDS}
    PURPOSE = "a purpose long enough to be read by a user"

    def R(rows=None, ceil=None, pending=()):
        reg = Registry(); reg.rows = dict(rows or {}); reg.pending = set(pending)
        reg.ceil = {("unreviewed", "all"): 0, ("base", "all"): 99, **(ceil or {})}
        return reg

    def row(tier, default="unset", since=BASE, review="-", purpose=PURPOSE, line=1):
        return dict(tier=tier, default=default, since=since, review=review, purpose=purpose, line=line)

    def site(**kw):
        return dict(sites=["x.c:1"], owner={"plugin"}, **{"set_only": False, **kw})

    def has(text, problems):
        return any(text in p for p in problems)

    K = ("env", "NVMTL_NEW")
    f = F(); f["env"]["NVMTL_NEW"] = site()
    esc = {K: row("escape", default="on (0 turns it off)", since="3.0", review="3.2")}
    arms = [
        ("a switch the source reads and the registry lacks is caught, and the message says what a new switch may be",
         has("has no row", rules(R(), f, "3.0")) and has("tier escape", rules(R(), f, "3.0")) and has("opt-in", rules(R(), f, "3.0"))),
        ("a row nothing reads is caught", has("read nowhere", rules(R({("env", "NVMTL_GONE"): row("diag")}), F(), "3.0"))),
        ("a retired row nothing reads is accepted", rules(R({("env", "NVMTL_GONE"): row("retired")}), F(), "3.0") == []),
        ("a retired name the source still reads is caught", has("still reads it", rules(R({K: row("retired")}), f, "3.0"))),
        ("a new opt-in (tier dev after 2.9) is refused, and the message says to ship it on with one opt-out",
         has("is new (since 3.0) and tier dev", rules(R({K: row("dev", since="3.0")}, {("dev", "plugin"): 1}), f, "3.0"))
         and has("ships ON", rules(R({K: row("dev", since="3.0")}, {("dev", "plugin"): 1}), f, "3.0"))),
        ("a new hardware switch is refused: a path that depends on the machine checks for it", has("checks for it at run time", rules(R({K: row("hardware", since="3.0")}), f, "3.0"))),
        ("a new recovery switch is refused: a way out is an escape with a review version", has("tier recovery", rules(R({K: row("recovery", since="3.0")}), f, "3.0"))),
        ("a new switch filed as an old one is caught by the count of 2.9 rows", has("#ceiling base all 0", rules(R({K: row("dev")}, {("dev", "plugin"): 1, ("base", "all"): 0}), f, "3.0"))),
        ("an escape that is on by default and not yet due passes", rules(R(esc), f, "3.0") == []),
        ("an escape past its review version is caught", has("was due for review", rules(R(esc), f, "3.2"))),
        ("an escape that defaults to off is caught", has("must be on when the switch is not set", rules(R({K: row("escape", default="off", since="3.0", review="3.2")}), f, "3.0"))),
        ("an escape without a review version is caught", has("needs a review version", rules(R({K: row("escape", default="on", since="3.0")}), f, "3.0"))),
        ("a switch of 2.9 that became an escape is held to the same two rules", has("needs a review version", rules(R({K: row("escape", default="on")}), f, "3.0"))),
        ("a test hook that is off by default passes, within its ceiling",
         rules(R({K: row("test", default="off (set to inject a fault)", since="3.0")}, {("test", "all"): 1}), f, "3.0") == []),
        ("a test hook above the ceiling is caught", has("#ceiling test all 0", rules(R({K: row("test", default="off", since="3.0")}, {("test", "all"): 0}), f, "3.0"))),
        ("a test hook that is on by default is caught", has("does nothing unless it is set", rules(R({K: row("test", default="on", since="3.0")}, {("test", "all"): 1}), f, "3.0"))),
        ("a new diagnostic that is not a sysctl needs a review version", has("needs a review version", rules(R({K: row("diag", since="3.0")}), f, "3.0"))),
        ("a new internal row for a variable the source reads is caught", has("is tier internal and the source reads it", rules(R({K: row("internal", since="3.0")}), f, "3.0"))),
        ("one more dev switch than the ceiling is caught", has("#ceiling dev plugin 0", rules(R({K: row("dev")}, {("dev", "plugin"): 0}), f, "3.0"))),
        ("a row without a reviewed purpose above the ceiling is caught", has("#ceiling unreviewed all 0", rules(R({K: row("dev", purpose=UNREVIEWED)}, {("dev", "plugin"): 1}), f, "3.0"))),
        ("a setting the settings tool does not offer is caught", has("does not offer it", rules(R({K: row("setting")}), f, "3.0", catalog_keys=set()))),
        ("a raised ceiling is caught, a lowered one is not", ceiling_rises("#ceiling dev plugin 5\n", "#ceiling dev plugin 6\n") != [] and
         ceiling_rises("#ceiling dev plugin 5\n", "#ceiling dev plugin 4\n") == []),
    ]
    S = ("sysctl", "debug.nv_new")
    s = F(); s["sysctl"]["debug.nv_new"] = site(writable=True, anybody=False)
    ro = F(); ro["sysctl"]["debug.nv_new"] = site(writable=False, anybody=False)
    arms += [
        ("a new writable sysctl is refused whatever its tier, and the message says what to do instead",
         all(has("is writable and its row is tier", rules(R({S: row(t, default="writable by root", since="3.0", review="3.2")}, {("test", "all"): 1}), s, "3.0")) for t in TIERS)
         and has("CTLFLAG_RD", rules(R({S: row("diag", default="writable by root", since="3.0")}), s, "3.0"))),
        ("a writable sysctl with no row is refused, not offered to sync", has("a new writable sysctl is refused", rules(R(), s, "3.0"))),
        ("a read-only sysctl with no row is sent to sync", has("switch_registry.py sync", rules(R(), ro, "3.0"))),
        ("a 2.9 writable sysctl of a permitted tier passes", rules(R({S: row("recovery", default="writable by root")}), s, "3.0") == []),
        ("a 2.9 writable sysctl on a default path (tier escape) is caught", has("is writable and its row is tier escape", rules(R({S: row("escape", default="on", review="3.2")}), s, "3.0"))),
        ("a sysctl that was read-only and became writable is caught", has("was read-only and", rules(R({S: row("diag", default="read-only")}), s, "3.0"))),
        ("CTLFLAG_ANYBODY is caught", has("ANYBODY", rules(R({S: row("diag", default="writable by root")}), {**F(), "sysctl": {"debug.nv_new": site(writable=True, anybody=True)}}, "3.0"))),
    ]
    P = ("bootarg", "nvrmlater")
    late = {P: row("escape", default="on (nvrmlater=0 restores 2.9)", since="3.0", review="3.2")}
    arms += [
        ("a row written ahead of its code passes while it is marked pending", rules(R(late, pending={P}), F(), "2.9") == []),
        ("the same row without the mark is an error", has("read nowhere", rules(R(late), F(), "2.9"))),
        ("a pending row still has to keep every other rule", has("needs a review version", rules(R({P: row("escape", default="on", since="3.0")}, pending={P}), F(), "2.9"))),
        ("a pending mark that names no row is caught", has("names no row", rules(R(pending={P}), F(), "2.9"))),
        ("a pending mark in a tree newer than 2.9 is caught", has("still marked #pending", rules(R(late, pending={P}), F(), "3.0"))),
    ]
    # sync and finalize on a registry in memory
    reg = R({**late, S: row("dev", default="writable by root"), ("sysctl", "debug.nv_gone"): row("diag", default="read-only")},
            {("dev", "plugin"): 3, ("base", "all"): 9}, pending={P})
    cnt = F(); cnt["sysctl"] = {"debug.nv_new": site(writable=False, anybody=False, desc="was a knob"), "debug.nv_count": site(writable=False, anybody=False, desc="a counter")}
    sync(reg, cnt, "2.9")
    arms.append(("sync adds a counter, drops one that left, follows a sysctl that turned read-only, lowers ceilings and keeps pending marks",
                 reg.rows[("sysctl", "debug.nv_count")]["tier"] == "diag" and reg.rows[("sysctl", "debug.nv_count")]["since"] == NEXT
                 and ("sysctl", "debug.nv_gone") not in reg.rows and reg.rows[S]["tier"] == "diag" and reg.rows[S]["default"] == "read-only"
                 and reg.ceil[("dev", "plugin")] == 0 and reg.ceil[("base", "all")] == 1 and reg.pending == {P}))
    sync(reg, cnt, "2.9", final=True)
    arms.append(("finalize removes the pending marks, and the row whose code never came is an error again",
                 reg.pending == set() and has("nvrmlater is read nowhere", rules(reg, cnt, "2.9"))))
    w = R(); wf = F(); wf["sysctl"]["debug.nv_w"] = site(writable=True, anybody=False, desc="a knob")
    sync(w, wf, "2.9", final=True)
    arms.append(("sync never adds a row for a writable sysctl", w.rows == {}))
    c = strip_comments('a = getenv("NVMTL_A"); // getenv("NVMTL_B")\n/* getenv("NVMTL_C") */ s = "//NVMTL_D";')
    arms.append(("names in comments are not switches, names in strings are", "NVMTL_A" in c and "NVMTL_B" not in c and "NVMTL_C" not in c and "NVMTL_D" in c))
    rows = {("bootarg", "nvfb"): row("install"), ("bootarg", "nvrmdma"): row("escape", default="on"), ("bootarg", "nvold"): row("retired", purpose="on by default since 3.0"),
            ("env", "NVMTL_FPS_LIMIT"): row("setting"), ("env", "NVK_FAULT_INJECT"): row("test", default="off")}
    lines, cnt = doctor(rows, "nvfb=1 nvrmdma=0 nvold=1 nvfbhrd=1 nvda_drv=1 keepsyms=1", "NVMTL_FPS_LIMIT=60\nFOO=1\nNVMTL_TYPO=1\nNVK_FAULT_INJECT=fake:300\n")
    arms.append(("doctor names an escape, a retired name, a test hook, an unknown boot argument, an unknown and an ignored key, and nothing else",
                 cnt == {"install": 1, "escape": 1, "retired": 1, "unknown": 2, "setting": 1, "ignored": 1, "test": 1}))
    t = parse("# head\n#ceiling dev plugin 2\n#pending bootarg nvx\nenv\tB\tdev\tunset\t2.9\t-\tp\nbootarg\tnvx\tescape\ton\t3.0\t3.2\tp\nenv\tA\tdev\tunset\t2.9\t-\tp\n")
    arms.append(("the registry file is written back sorted, with its header, ceilings and pending marks",
                 t.text() == "# head\n#ceiling dev plugin 2\n#pending bootarg nvx\nenv\tA\tdev\tunset\t2.9\t-\tp\nenv\tB\tdev\tunset\t2.9\t-\tp\nbootarg\tnvx\tescape\ton\t3.0\t3.2\tp\n"))
    arms.append(("a malformed line of the registry is an error, not a skipped row", parse("env\tA\tdev\n").errs != [] and parse("env\tA\tnope\tu\t2.9\t-\tp\n").errs != []))
    bad = [n for n, ok in arms if not ok]
    for n, ok in arms:
        print(("ok   " if ok else "FAIL ") + n)
    print(f"{len(bad)} failed of {len(arms)}")
    return 1 if bad else 0


def main(argv):
    root = ROOT
    if "--root" in argv:
        i = argv.index("--root"); root = Path(argv[i + 1]).resolve(); del argv[i:i + 2]
    if "--selftest" in argv:
        return selftest()
    cmd = argv[1] if len(argv) > 1 else "check"
    if cmd == "bootstrap":
        if (root / REG).is_file() and "--again" not in argv:
            print(f"{REG} exists: rows are added by hand (or by `sync` for read-only sysctls). A second bootstrap would file every switch added since "
                  f"as an old one. Pass --again only to compare its draft with the registry."); return 2
        base = Path(argv[argv.index("--base") + 1]).resolve() if "--base" in argv else None
        sys.stdout.write(bootstrap(root, base)); return 0
    if cmd == "doctor":
        return cmd_doctor(root, argv)
    if cmd in ("scan", "json"):
        found, problems = scan(root)
        if cmd == "json":
            print(json.dumps({k: {n: dict(sites=e["sites"], owner=sorted(e["owner"]), **{x: e[x] for x in ("writable", "desc") if x in e})
                                  for n, e in found[k].items()} for k in KINDS}, indent=1, sort_keys=True)); return 0
        for k in KINDS:
            print(f"{k}: {len(found[k])}")
            if "-v" in argv:
                for name, e in sorted(found[k].items()):
                    print(f"  {name}\t{','.join(sorted(e['owner']))}\t{e['sites'][0]}")
        for p in problems:
            print("FAIL " + p)
        return 1 if problems else 0
    if cmd == "check":
        return cmd_check(root)
    if cmd == "gen":
        return cmd_gen(root)
    if cmd in ("sync", "finalize"):
        return cmd_sync(root, final=cmd == "finalize")
    print(__doc__); return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
