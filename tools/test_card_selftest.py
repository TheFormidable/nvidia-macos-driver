#!/usr/bin/env python3
"""tools/card_selftest.py, the card runner, without a card and without any of the Mac's own programs.

The runner starts its system programs (git, xcrun, ioreg, sysctl, pgrep, ps, sw_vers, getconf, caffeinate, python3, sh)
through its SYSTEM table. Here every entry points at a stand-in in a scratch folder, the installed driver is a scratch
folder in place of /Library/GPUBundles, the home folder is a scratch one, and EXEC_ONLY_UNDER makes the runner refuse to
start anything outside the scratch folder (and this Python). The runner is loaded from its path by a small wrapper
that sets those names and calls main; it runs as a program of its own, as on the Mac. No GPU program, xcrun, ioreg,
sysctl, git or any other of the Mac's programs runs; the stand-in check programs are Python and /bin/sh scripts.

Static arms on the tree as it is: every tools/card/checks_*.py loads with the runner's own loader, with known keys,
unique ids and known placeholders; every sources path exists; no build writes outside {BIN}/<id>/ and nothing names
/tmp; every GPU program the checks build is named nvmtltest; no file in tools/card/ and no program a check names is a
test_* file that run_all_tests.py would run on the card; the check files import only the standard library and do no
I/O when loaded; the runner starts processes in one place, and every program it names is in its table. Controls: rows
with an unknown key, an unknown placeholder, a build into /tmp, an -o outside {BIN}/<id>/, a forbidden command word, a
program outside the allowed ones, a duplicate id, and a file named for no part are each refused.

Runs with stand-ins: a run whose rows pass, fail by exit code, by a missing stdout pattern and by a forbidden stderr
pattern, skip and are inactive by exit code, fail to build, lack a source, a file, the GUI or Zink, are left out with
--no-window, raise the Xid counter, crash through the convention's exit 70 and by a signal (SIGTERM, which leaves no
crash report), leave a program running in their group, print the home folder, the account, the host and the user
temporary folder (all scrubbed in every file of the run), and print information lines; then a check that passes its
limit (its process group is killed, later GPU checks are not run, a later CPU check runs, exit 4), one that its own
alarm ends (TIMEOUT), one that cannot be killed (STUCK: the kill is made a no-op here), the three stop rules (channel
give-up, fence state, WindowServer's pid), the deadline, caffeinate started with -d -i -w <runner pid> for a window
check and stopped after it; the refusals (an --out outside the base or not empty, a dirty checkout, other driver
sources, another package commit, an installed file that differs, a device of another class, an unknown key), --list
and --plan (which start no check program and write nothing). The real tools/card/checks_install.py runs against a
stand-in installation, once whole (every row passes) and once with a defect for each row; install.plugin-translator
also with a copy of the translator beside the plugin of the same build, of an older one (also unreadable by this
account), one that is not Mach-O and one without LC_UUID (nvmtl/'s too), without nvmtl/'s copy, and on a checkout
whose plugin loader shows no preference for the copy beside it (with none, the same build and an older one beside the
plugin); its preference finder also on loader snippets that do and do not name the copy beside the plugin. After every
run: no file appeared outside the run folder and the stand-ins' own folder.

Not a card check: it runs on Linux and on a Mac without the driver. Written and run on Linux only so far."""
import sys
sys.dont_write_bytecode = True   # nor for the runner and check files it loads
import ast  # noqa: E402
import hashlib
import importlib.util
import json
import os
import plistlib
import pwd
import re
import shlex
import shutil
import signal
import struct
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "tools" / "card_selftest.py"
CARD = ROOT / "tools" / "card"
PY = os.path.realpath(sys.executable)
fails = 0


def arm(name, ok, detail=""):
    global fails
    fails += not ok
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f" ({str(detail)[-1500:]})"), flush=True)


def load_runner():
    spec = importlib.util.spec_from_file_location("card_selftest_under_test", str(RUNNER))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


R = load_runner()


# ------------------------------------------------------------------------------------------- the tree as it is
def strings_of(c):
    out = list(c.get("argv", []))
    for st in c.get("build", []):
        out += st["argv"]
    out += [f"{k}={v}" for k, v in c.get("env", {}).items()]
    out += [n[5:] for n in c.get("needs", []) if n.startswith("file:")]
    return out


checks, errors = R.load_checks(CARD)
files = sorted(CARD.glob("checks_*.py"))
arm("tools/card holds check files, checks_install.py among them", any(f.name == "checks_install.py" for f in files), files)
arm("every tools/card/checks_*.py loads with known keys, unique ids and known placeholders", not errors, errors)
arm("the rows were read (the check looked at something)", len(checks) >= 7, len(checks))
missing = [(c["id"], s) for c in checks for s in c["sources"] if not (ROOT / s).exists()]
arm("every sources path of every row exists in the checkout", not missing, missing)
bad_ph = [(c["id"], s) for c in checks for s in strings_of(c) for p in R.PLACEHOLDER.findall(s) if p not in R.PLACEHOLDERS]
arm("every placeholder the rows use is one the runner knows", not bad_ph, bad_ph)
tmp_use = [(c["id"], s) for c in checks for s in strings_of(c) if re.search(r"(?<!\})/tmp\b", s) or "/private/tmp" in s]
arm("no row names /tmp or /private/tmp (builds and runs stay inside the run folder)", not tmp_use, tmp_use)
outs = [(c["id"], st["argv"][i + 1]) for c in checks for st in c["build"] for i, a in enumerate(st["argv"][:-1]) if a == "-o"]
arm("every build writes into {BIN}/<its id>/ only",
    all(o.startswith("{BIN}/" + cid + "/") for cid, o in outs) and
    all("-o" in st["argv"] or st["argv"][0] != "/usr/bin/xcrun" for c in checks for st in c["build"]), outs)
names = [c["id"] for c in checks if c["kind"] == "gpu" and c.get("argv", [""])[0].startswith("{BIN}/")
         and os.path.basename(c["argv"][0]) != "nvmtltest"]
arm("every GPU program a row builds runs as nvmtltest (the name the plugin always grants a device)", not names, names)
host_tests = sorted(p.name for p in CARD.iterdir() if p.name.startswith("test_"))
named = [(c["id"], s) for c in checks for s in strings_of(c) + c["sources"]
         if re.search(r"(?:^|/)tools/test_[^/]*\.(?:c|cpp|sh|py)$", s)]
arm("no file in tools/card is named test_*, and no row names a tools/test_*.c, .cpp, .sh or .py (run_all_tests.py would "
    "run it on the card)", not host_tests and not named and not RUNNER.name.startswith("test_"), (host_tests, named))

probs = {f.name: R.import_problems(f.read_text()) for f in files}
probs = {k: v for k, v in probs.items() if v}
arm("the check files import only pure standard modules and read, write or start nothing when loaded", not probs, probs)
syntax = []
for f in [RUNNER] + files:
    try:
        ast.parse(f.read_text(), feature_version=(3, 9))
    except SyntaxError as e:
        syntax.append(f"{f.name}: {e}")
arm("the runner and the check files parse as Python 3.9 (Apple's /usr/bin/python3 on that Mac)", not syntax, syntax)

src = RUNNER.read_text()
tree = ast.parse(src)
starts = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
          and (n.value.id, n.attr) in {("subprocess", "Popen"), ("subprocess", "run"), ("subprocess", "call"),
                                       ("subprocess", "check_call"), ("subprocess", "check_output"), ("os", "system"),
                                       ("os", "popen"), ("os", "fork"), ("os", "forkpty")}
          or isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "os"
          and re.match(r"^(exec|spawn|posix_spawn)", n.attr)]
arm("the runner starts processes in one place (one subprocess.Popen, nothing else)",
    [(n.value.id, n.attr) for n in starts] == [("subprocess", "Popen")], [(n.value.id, n.attr, n.lineno) for n in starts])
literal = []
for n in ast.walk(tree):
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("quick", "spawn") and n.args:
        a = n.args[0]
        if isinstance(a, ast.List) and a.elts and isinstance(a.elts[0], ast.Constant):
            literal.append(a.elts[0].value)
literal_bad = [p for p in literal if p not in R.SYSTEM and p not in R._DEFAULT_PATHS]
arm("every program the runner names in its own calls is in its SYSTEM table", literal and not literal_bad, (literal, literal_bad))
arm("the SYSTEM table names absolute paths of the system's own folders, and the programs a check may start are among them",
    all(re.match(r"^/(usr/bin|usr/sbin|bin)/[a-z_0-9]+$", v) for v in R.SYSTEM.values())
    and set(R.CHECK_PROGRAMS) <= set(R.SYSTEM.values()), R.SYSTEM)


# --------------------------------------------------------------------------------------------- loader controls
def refused_by_loader(rows_by_file):
    with tempfile.TemporaryDirectory() as d:
        for name, rows in rows_by_file.items():
            Path(d, name).write_text("CHECKS = " + repr(rows) + "\n")
        return R.load_checks(d)


GOOD = {"id": "plugin.good", "part": "plugin", "title": "a row", "kind": "gpu", "order": 300,
        "build": [{"argv": ["/usr/bin/xcrun", "clang", "{SRC}/tools/x.m", "-o", "{BIN}/plugin.good/nvmtltest"], "limit": 60}],
        "argv": ["{BIN}/plugin.good/nvmtltest"], "limit": 90, "expect": {"rc": 0, "stdout": ["^PASS"]}}


def variant(**kw):
    c = json.loads(json.dumps(GOOD))
    for k, v in kw.items():
        if v is None:
            c.pop(k, None)
        else:
            c[k] = v
    return c


rows, errs = refused_by_loader({"checks_plugin.py": [GOOD]})
arm("control: a well-formed row loads", len(rows) == 1 and not errs, errs)
for what, c, word in (
        ("an unknown key", variant(timeout=5), "unknown key 'timeout'"),
        ("an unknown placeholder", variant(argv=["{BIN}/plugin.good/nvmtltest", "{HOME}/x"]), "unknown placeholder {HOME}"),
        ("a build into /tmp", variant(build=[{"argv": ["/usr/bin/xcrun", "clang", "x.m", "-o", "/tmp/x"], "limit": 9}]), "-o must name"),
        ("/tmp in an argument", variant(argv=["{BIN}/plugin.good/nvmtltest", "/tmp/out"]), "'/tmp'"),
        ("a forbidden command word", variant(argv=["/bin/sh", "-c", "defaults read x"]), "'defaults'"),
        ("sysctl -w split over arguments", variant(argv=["/bin/sh", "-c", "sysctl", "-w", "x=1"]), "sysctl -w"),
        ("a program that is neither built, installed nor allowed", variant(argv=["/usr/bin/open", "x"]), "'/usr/bin/open' is neither"),
        ("the runner's own read programs", variant(argv=["/usr/sbin/ioreg", "-l"]), "'/usr/sbin/ioreg' is neither"),
        ("a part that is not the file's", variant(part="zink"), "part must be 'plugin'"),
        ("an id without its part", variant(id="good"), "must start with its part"),
        ("an rc_map word that is not one", variant(rc_map={3: "pass"}), "rc_map must map"),
        ("a func and an argv", variant(func="install_identity"), "either argv"),
        ("an unknown need", variant(needs=["screen"]), "needs 'screen'"),
        ("NVK_DIAG in a row's environment (NVK then writes /private/tmp/nvk-diag-<pid>.log)",
         variant(env={"NVK_DIAG": "1"}), "'NVK_DIAG'"),
        ("NVK_DIAG on a shell's command line", variant(argv=["/bin/sh", "-c", "NVK_DIAG=1 exec x"]), "'NVK_DIAG'")):
    rows, errs = refused_by_loader({"checks_plugin.py": [c]})
    arm(f"control: {what} is refused when the checks are loaded", not rows and any(word in e for e in errs), errs)
ZX = variant(id="zink.x", part="zink", argv=["{BIN}/zink.x/nvmtltest"],
             build=[{"argv": ["/usr/bin/xcrun", "clang", "x.m", "-o", "{BIN}/zink.x/nvmtltest"], "limit": 9}])
rows, errs = refused_by_loader({"checks_zink.py": [ZX]})
arm("control: the row of the next control loads alone", len(rows) == 1 and not errs, errs)
rows, errs = refused_by_loader({"checks_plugin.py": [GOOD], "checks_zink.py": [ZX, ZX]})
arm("control: a duplicate id is refused", any("also in" in e for e in errs), errs)
rows, errs = refused_by_loader({"checks_other.py": [GOOD]})
arm("control: a file named for no part is refused", any("is not a part" in e for e in errs), errs)
VX = variant(id="v301.x", part="v301", argv=["{BIN}/v301.x/nvmtltest"],
             needs=["file:{SRC}/plugin/nvmtl_switch_defaults.h"],
             build=[{"argv": ["/usr/bin/xcrun", "clang", "x.m", "-o", "{BIN}/v301.x/nvmtltest"], "limit": 9}])
rows, errs = refused_by_loader({"checks_v301.py": [VX]})
arm("control: tools/card/checks_v301.py is a part of its own (release 3.0.1's checks) and its rows load",
    len(rows) == 1 and rows[0]["part"] == "v301" and not errs, errs)
# a checkout without release 3.0.1 lacks the files these rows need, so each of them is a skip there that names one
v301_rows = [c for c in checks if c["part"] == "v301"]
no_marker = [c["id"] for c in v301_rows if not any(n.startswith("file:{SRC}/") for n in c.get("needs", []))]
arm("every row of tools/card/checks_v301.py needs a file of the checkout (file:{SRC}/...), so that a checkout without "
    "release 3.0.1 skips it", v301_rows and not no_marker, no_marker)
arm("the driver sources the checkout gate compares include build/build_nvk.sh (the chain of NVK patches)",
    "build/build_nvk.sh" in R.DRIVER_PATHS and "nvk" in R.DRIVER_PATHS, R.DRIVER_PATHS)
with tempfile.TemporaryDirectory() as d:
    mark = Path(d, "mark")
    Path(d, "checks_plugin.py").write_text(f"import subprocess\nopen({str(mark)!r}, 'w').close()\nCHECKS = []\n")
    rows, errs = R.load_checks(d)
    arm("control: a check file that would import or open something is refused before it is loaded (its code never runs)",
        not rows and any("not loaded" in e and "import subprocess" in e and "open()" in e for e in errs)
        and not mark.exists(), errs)


# --------------------------------------------------------------------------------------------------- unit arms
s = R.Scrub("/Users/tester", "tester", "Bench-Mac-7.local", "/var/folders/ab/cd/T", "/var/folders/ab/cd/C")
got = s("/Users/tester/nullmoth-claude /System/Volumes/Data/Users/tester/x /private/var/folders/ab/cd/T/y /var/folders/ab/cd/C "
        "Bench-Mac-7.local BENCH-MAC-7 user tester testers /Users/testerb tester-x")
want = "~/nullmoth-claude ~/x <usertmp>/y <usercache> <host> <host> user <user> testers /Users/testerb <user>-x"
arm("the scrub: home in its three forms, the user folders, the host and its first label, the account as a word", got == want, got)


def macho_bytes(uuid, minos=(15, 0), ident=None, cpu=0x01000007):
    """a thin Mach-O file: LC_UUID (none when uuid is None), LC_BUILD_VERSION and an optional LC_ID_DYLIB"""
    cmds = struct.pack("<II", 0x1B, 24) + bytes.fromhex(uuid) if uuid else b""
    cmds += struct.pack("<IIIIII", 0x32, 24, 1, (minos[0] << 16) | (minos[1] << 8), 26 << 16, 0)
    n = 2 if uuid else 1
    if ident:
        name = ident.encode() + b"\0"
        name += b"\0" * (-(24 + len(name)) % 8)
        cmds += struct.pack("<IIIIII", 0xD, 24 + len(name), 24, 2, 0x10000, 0x10000) + name
        n += 1
    return struct.pack("<IiiIIIII", 0xFEEDFACF, cpu, 3, 6, n, len(cmds), 0, 0) + cmds + b"\0" * 64


U1, U2, U3 = "11" * 16, "22" * 16, "33" * 16
with tempfile.TemporaryDirectory() as d:
    thin = Path(d, "thin")
    thin.write_bytes(macho_bytes(U1, (15, 0), "@loader_path/libx.dylib"))
    fat = Path(d, "fat")
    a, b = macho_bytes(U3, (11, 0), None, 0x0100000C), macho_bytes(U2, (15, 0))
    fat.write_bytes(struct.pack(">II", 0xCAFEBABE, 2) + struct.pack(">iiIII", 0x0100000C, 0, 4096, len(a), 12)
                    + struct.pack(">iiIII", 0x01000007, 3, 8192, len(b), 12) + b"\0" * (4096 - 48) + a
                    + b"\0" * (4096 - len(a)) + b)
    Path(d, "text").write_text("#!/bin/sh\n")
    t, f, x = R.macho(thin), R.macho(fat), R.macho(Path(d, "text"))
arm("the Mach-O reader: a thin x86_64 file's UUID, minos and install name", t == {"arches": ["x86_64"], "uuid": U1,
                                                                                    "minos": "15.0", "id": "@loader_path/libx.dylib"}, t)
arm("the Mach-O reader: a fat file's arches and its x86_64 slice; a script is not Mach-O",
    f and f["arches"] == ["arm64", "x86_64"] and f["uuid"] == U2 and f["minos"] == "15.0" and x is None, (f, x))
sums = R.sums_lines("a" * 64 + "  ./Library/GPUBundles/x\n" + "b" * 64 + " *Library/GPUBundles/y z\n" + "short line\n")
arm("SHA256SUMS lines with and without ./, the path from column 67", sums == [("a" * 64, "Library/GPUBundles/x"),
                                                                              ("b" * 64, "Library/GPUBundles/y z")], sums)


def pref(*body):
    """install.plugin-translator's preference finder on a loader of one function that calls dladdr"""
    return R._xlate_beside("static void f(void) {\n    Dl_info i; dladdr((const void *)&f, &i);\n    "
                           + "\n    ".join(body) + "\n}\n")


got = [pref('snprintf(p, sizeof p, "%s/libnvmtl_translate.dylib", dir);'),
       pref('NSString *s = [NSString stringWithFormat:@"%@/../Resources/libnvmtl_translate.dylib", d];'),
       pref('const char *b = @"Contents/MacOS/libnvmtl_translate.dylib".UTF8String;')]
arm("the plugin-translator row's preference finder takes a literal that is the path beside the plugin, or a format "
    "whose last directive prints the folder before \"/\" and the path",
    got == [("f", "libnvmtl_translate.dylib"), ("f", "../Resources/libnvmtl_translate.dylib"),
            ("f", "Contents/MacOS/libnvmtl_translate.dylib")], got)
got = [pref('nvlog("translate: no copy at Contents/MacOS/libnvmtl_translate.dylib");'),
       pref('nvlog("translate: %s/lib dir/libnvmtl_translate.dylib", d);'),
       pref('nvlog("translate: dlopen(%s/libnvmtl_translate.dylib): %s", p, e);'),
       pref('nvlog("translate: %s: no libnvmtl_translate.dylib", p);'),
       pref('const char *b = "..\\\\x/libnvmtl_translate.dylib";'),
       pref('const char *b = "100%/libnvmtl_translate.dylib";'),
       pref('void *h = dlopen("/Library/GPUBundles/nvmtl/libnvmtl_translate.dylib", RTLD_NOW);'),
       pref('// "libnvmtl_translate.dylib"', '/* "%s/libnvmtl_translate.dylib" */'),
       R._xlate_beside('static void f(void) { const char *b = "libnvmtl_translate.dylib"; }\n')]
arm("and none that names no usable path beside the plugin: a space, a format whose last directive is not followed by "
    "\"/\" and the path, a backslash, a lone %, an absolute path, a comment, or a function without dladdr",
    got == [None] * 9, got)


# ------------------------------------------------------------------------------------------- the stand-in world
WRAP = """import importlib.util, json, os, sys
from pathlib import Path
cfg = json.loads(Path(sys.argv[1]).read_text())
spec = importlib.util.spec_from_file_location("card_selftest_under_test", cfg["runner"])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.SYSTEM = dict(cfg["system"])
m.SRC = Path(cfg["src"])
m.GB = cfg["gb"]
m.AUDIT_TMP_DIRS = tuple(cfg["audit_tmp"])
m.ICD_DIR = cfg["icd"]
m.EXEC_ONLY_UNDER = tuple(cfg["exec_only_under"])
m.MIN_FREE = 0
m.GB_OWNER_UID = os.getuid()
m.KILL_POLL = cfg["kill_poll"]
if cfg["no_kill"]:
    m._killpg = lambda pgid, sig: None
if cfg["unreadable"]:     # files that access(R_OK) refuses, as for another account (this test may run as root)
    _access, _deny = os.access, set(cfg["unreadable"])
    os.access = lambda p, mode, **k: False if mode & os.R_OK and os.fspath(p) in _deny else _access(p, mode, **k)
if cfg["fault"]:
    def fault(self, *a, **k):
        raise RuntimeError("a stand-in fault of the runner")
    m.Run.watch = fault
sys.exit(m.main(cfg["argv"]))
"""

SH_FAKES = {
    "git": """echo "git-env TMPDIR=${TMPDIR-unset}" >> "$S/calls.log"
case " $* " in
  *" status "*) cat "$S/git_status" 2>/dev/null; exit 0 ;;
  *" rev-parse "*) echo ffffffffffffffffffffffffffffffffffffffff; exit 0 ;;
  *" diff "*) exit "$(cat "$S/git_diff_rc" 2>/dev/null || echo 0)" ;;
esac
exit 2""",
    "ioreg": """[ -f "$S/no_accel" ] && exit 0
echo '+-o NVAccel  <class NVAccel, id 0x100000abc, registered, matched, active, busy 0 (0 ms), retain 9>'
echo '    "IOGLBundleName" = "'"$(cat "$S/gl_bundle")"'"'""",
    "sysctl": """[ "$1" = -n ] || exit 2
case "$2" in
  machdep.cpu.brand_string) echo "Stand-in CPU" ;;
  debug.nvrm_switches_nondefault) echo none ;;
  *) [ -f "$S/counters/$2" ] && cat "$S/counters/$2" || { echo "sysctl: unknown oid '$2'" >&2; exit 1; } ;;
esac""",
    "pgrep": """[ -s "$S/ws" ] || exit 1
cat "$S/ws\"""",
    "ps": """[ -f "$S/ps_lines" ] && while read -r pid rest; do kill -0 "$pid" 2>/dev/null && echo "$pid $rest"; done < "$S/ps_lines"
echo "    1     0     1 Ss   00:03 stand-in-init\"""",
    "sw_vers": """case "$1" in -productVersion) echo 26.7 ;; -buildVersion) echo 25X000 ;; *) exit 1 ;; esac""",
    "getconf": """case "$1" in DARWIN_USER_TEMP_DIR) echo "$S/usertmp/" ;; DARWIN_USER_CACHE_DIR) echo "$S/usercache/" ;; *) exit 1 ;; esac""",
    "xcrun": """out=; src=; prev=
for a in "$@"; do
  [ "$prev" = -o ] && out=$a
  case "$a" in *.m|*.mm|*.c|*.cpp) src=$a ;; esac
  prev=$a
done
echo "xcrun-env TMPDIR=$TMPDIR CLANG_MODULE_CACHE_PATH=$CLANG_MODULE_CACHE_PATH NVMTL_PROCESS_ENV=${NVMTL_PROCESS_ENV-unset}" >> "$S/calls.log"
if grep -q BUILD-FAILS "$src"; then echo "$src:1:1: error: this stand-in source does not build" >&2; exit 1; fi
cp "$src" "$out" && chmod 755 "$out\"""",
    "caffeinate": """w=; prev=
for a in "$@"; do [ "$prev" = -w ] && w=$a; prev=$a; done
echo "$$" > "$S/caffeinate.pid"
i=0
while [ $i -lt 300 ] && kill -0 "$w" 2>/dev/null; do echo x >> "$S/caffeinate.beat"; sleep 0.1; i=$((i + 1)); done""",
    "python3": """exec "$PYEXE" "$@\"""",
    "sh": """exec /bin/sh "$@\"""",
}

PROG = """#!{py} -B
# a stand-in card check: what it does is its first argument
import json, os, signal, socket, sys, time
S = {state!r}
mode = sys.argv[1]
me = os.path.basename(os.path.dirname(sys.argv[0]))
with open(os.path.join(S, "ran.log"), "a") as f:
    f.write(me + " " + mode + "\\n")


def bump(name, by=1, to=None):
    p = os.path.join(S, "counters", name)
    with open(p) as f:
        v = int(f.read().strip())
    with open(p, "w") as f:
        f.write(str(to if to is not None else v + by) + "\\n")


def heartbeat_child(tag, ps_line=False):
    if os.fork() == 0:
        with open(os.path.join(S, tag + ".pid"), "w") as f:
            f.write(str(os.getpid()))
        if ps_line:
            with open(os.path.join(S, "ps_lines"), "a") as f:
                f.write(" ".join(str(x) for x in (os.getpid(), os.getppid(), os.getpgid(0))) + " S 00:01 stand-in-orphan\\n")
        for _ in range(200):
            with open(os.path.join(S, tag + ".beat"), "a") as f:
                f.write("x")
            time.sleep(0.1)
        os._exit(0)


if mode == "pass":
    print("INFO home " + os.environ["HOME"] + "; account " + os.environ["USER"] + "; host " + os.uname().nodename
          + " " + socket.gethostname() + "; usertmp " + os.path.join(S, "usertmp") + "/x")
    for k in sorted(os.environ):
        print("ENV " + k + "=" + os.environ[k])
    with open(os.environ["NVMTL_LOG_PATH"], "a") as f:
        f.write("plugin log line from " + os.environ["HOME"] + "\\n")
    print("PASS stand-in case")
    print("RESULT pass 1")
elif mode == "fail":
    print("FAIL stand-in case: got=1 expected=2")
    sys.exit(1)
elif mode == "skip":
    print("SKIP: a precondition is missing")
    sys.exit(3)
elif mode == "inactive":
    print("INACTIVE: the context is Apple's")
    sys.exit(4)
elif mode == "lacks":
    print("nothing of what is expected")
elif mode == "forbid":
    print("PASS stand-in case")
    sys.stderr.write("NVK Zink: no screen\\n")
elif mode in ("xid", "gaveup", "fence", "ws", "recovered", "kicks", "cekept", "power", "ceclear", "xidce"):
    if mode == "xid":
        bump("debug.nvrm_xid_count", 2)
    elif mode == "xidce":
        bump("debug.nvrm_xid_count", 1)
        bump("debug.nvrmfb_vram_ce_cleared", 4)
    elif mode == "gaveup":
        bump("debug.nvrm_chan_gave_up", 1)
    elif mode == "fence":
        bump("debug.nvrm_fence_state", to=3)
    elif mode == "recovered":
        bump("debug.nvrm_chan_recovered", 1)
        bump("debug.nvrm_chan_dropped", 3)
    elif mode == "kicks":
        bump("debug.nvrm_chan_kicks", 2)
    elif mode == "cekept":
        bump("debug.nvrmfb_vram_ce_kept", 1)
    elif mode == "power":
        bump("debug.nvrmfb_vram_power_events", 2)
    elif mode == "ceclear":
        bump("debug.nvrmfb_vram_ce_clear", to=2)
    else:
        with open(os.path.join(S, "ws"), "w") as f:
            f.write("999\\n")
    print("PASS stand-in case")
elif mode == "crash70":
    sys.stderr.write("CRASH SIGBUS\\n")
    sys.exit(70)
elif mode == "sigterm":
    os.kill(os.getpid(), signal.SIGTERM)
    time.sleep(5)
elif mode == "alarm":
    signal.alarm(1)
    time.sleep(10)
elif mode == "bg":
    heartbeat_child(me)
    print("PASS stand-in case")
elif mode == "orphan":
    heartbeat_child(me, ps_line=True)
    time.sleep(0.5)
    print("PASS stand-in case")
elif mode == "slow":
    heartbeat_child(me)
    with open(os.path.join(S, me + ".main.pid"), "w") as f:
        f.write(str(os.getpid()))
    time.sleep(float(sys.argv[2]))
    print("PASS stand-in case")
elif mode == "info":
    print("INFO speed 12 fps")
    print("PASS stand-in case")
elif mode == "upload":
    with open(os.environ["NVMTL_LOG_PATH"], "a") as f:
        f.write("pid 7 NVMTL: vk: async texture upload ON (NVMTL_ASYNC_TEX_UPLOAD=unset)\\n"
                "pid 7 NVMTL: vk: tex upload async: 256 pending uploads hold 67108864 bytes of staging (limits 256 and "
                "268435456): an upload past a limit waits for its copy, as in 3.0\\n")
    print("PASS stand-in case")
elif mode == "leave":
    home = os.environ["HOME"]
    for p in (os.path.join(os.path.dirname(S), "tmp", "nvmtl-left.log"),
              os.path.join(os.path.dirname(S), "tmp", "nvk-diag-4242.log"),
              os.path.join(home, "Library", "Caches", "nvmtl", "entry"),
              os.path.join(home, "Library", "Logs", "DiagnosticReports", "nvmtltest-2026-11-16-030000.ips"),
              os.path.join(home, "Library", "Logs", "DiagnosticReports", "Other-2026-11-16-030000.ips")):
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write("x")
    print("PASS stand-in case")
else:
    sys.exit(2)
"""

PROBE = """#!/bin/sh
echo "device-probe probe" >> "{state}/ran.log"
cls=$(cat "{state}/probe_class"); gui=$(cat "{state}/probe_gui")
[ -f "{state}/probe_xid" ] && echo 5 > "{state}/counters/debug.nvrm_xid_count"
[ -f "{state}/probe_kick" ] && echo 7 > "{state}/counters/debug.nvrm_chan_kicks"
[ "$cls" = NVMTLDevice ] || echo "SKIP: no device of this driver ($cls)"
echo "DEVICE class=$cls name=Stand-in GPU (NVMTL over NVK stand-in)"
echo "ALLDEVICES count=1"
echo "ALLDEVICE 0 class=$cls name=Stand-in GPU (NVMTL over NVK stand-in)"
echo "GUI $gui"
echo "GUI lockkeys=CGSSessionScreenIsLocked"
[ "$cls" = NVMTLDevice ] || exit 3
echo "PASS device"
"""

MESA = "0a6fa60abb737d26ac8a316a4aad7c9ddf64f9e4"
# nvmtl/'s translator, and an older build of it with another LC_UUID (release 2.9's, the copy the Mac of round 11 had
# beside the plugin), each with its LC_UUID as the row prints it, the form of the plugin's "translate: opened" line
XU, XU_OLD = "44" * 16, "cbc0201690e23dbfbfd716087181d77f"
XU_T, XU_OLD_T = "44444444-4444-4444-4444-444444444444", "CBC02016-90E2-3DBF-BFD7-16087181D77F"
XLATE, XLATE_OLD = macho_bytes(XU), macho_bytes(XU_OLD) + b"older"
SCRIPT = b"#!/bin/sh\n"
BESIDE = "NVMTLDriver.bundle/Contents/MacOS/libnvmtl_translate.dylib"
# a plugin loader without the preference for a copy beside the plugin: its function that calls dladdr opens nvmtl/'s
# copy by its absolute path, and the bare name is only in comments, a log line and a function that does not call dladdr
LOADER_NO_BESIDE = """static const char *nvmtl_xlate_name(void) {
    return /* dladdr(&x, &info) is not called here */ "libnvmtl_translate.dylib";
}
static int nvmtl_xlate_ready(void) {
    Dl_info info; // dladdr(...) then @"libnvmtl_translate.dylib" beside the plugin: not in this loader
    if (dladdr((const void *)&nvmtl_xlate_ready, &info))
        nvlog("translate: no libnvmtl_translate.dylib beside %s", info.dli_fname);
    /* { @"libnvmtl_translate.dylib" } */
    void *h = dlopen("/Library/GPUBundles/nvmtl/libnvmtl_translate.dylib", RTLD_NOW | RTLD_LOCAL);
    return h != NULL;
}
"""
EXPECT = R.DEFAULT_EXPECT


class World:
    """a scratch home, checkout, staged package, installation and stand-in programs"""

    def __init__(self, root, name):
        self.root = Path(root, name)
        self.home = self.root / "home"
        self.base = self.home / "nullmoth-claude"
        self.src = self.base / "src"
        self.b = self.base / "build2"
        self.pkgroot = self.b / "package" / "pkgroot"
        self.gb = self.root / "GPUBundles"
        self.state = self.root / "state"
        self.fake = self.root / "fakebin"
        for d in (self.src / "tools" / "card", self.src / "build", self.src / "plugin", self.pkgroot, self.gb,
                  self.state / "counters", self.fake,
                  self.state / "usertmp", self.state / "usercache", self.root / "tmp", self.root / "icd",
                  self.home / "Library" / "Logs" / "DiagnosticReports"):
            d.mkdir(parents=True, exist_ok=True)
        self.system = {}
        for n, body in SH_FAKES.items():
            p = self.fake / n
            p.write_text(f'#!/bin/sh\nS={shlex.quote(str(self.state))}\nPYEXE={shlex.quote(PY)}\n'
                         f'echo "{n} $*" >> "$S/calls.log"\n{body}\n')
            p.chmod(0o755)
            self.system[n] = str(p)
        card = self.src / "tools" / "card"
        (card / "device_probe.m").write_text(PROBE.format(state=self.state))
        (card / "prog.m").write_text(PROG.format(py=PY, state=str(self.state)))
        (card / "broken.m").write_text("BUILD-FAILS\n")
        (self.src / "build" / "prepare_zink.py").write_text(f"MESA_COMMIT = '{MESA}'\n")
        # the checkout's own translator loader, which install.plugin-translator reads for the plugin's preference
        (self.src / "plugin" / "NVMTLLibraryLoad.m").write_text((ROOT / "plugin" / "NVMTLLibraryLoad.m").read_text())
        inc = self.b / "out" / "nvk-build" / "mesa" / "include" / "vulkan"
        inc.mkdir(parents=True)
        (inc / "vulkan_core.h").write_text("#define VK_HEADER_VERSION 363\n"
                                           "#define VK_HEADER_VERSION_COMPLETE VK_MAKE_API_VERSION(0, 1, 4, VK_HEADER_VERSION)\n")
        self.set(git_status="", git_diff_rc="0", gl_bundle="NVKZinkGLDriver", probe_class="NVMTLDevice",
                 probe_gui="session=1 console=1 logindone=1 locked=0 asleep=0", ws="88\n")
        for n, v in (("debug.nvrm_xid_count", 0), ("debug.nvrm_chan_recovered", 0), ("debug.nvrm_chan_gave_up", 0),
                     ("debug.nvrm_fence_state", 1)):
            (self.state / "counters" / n).write_text(f"{v}\n")
        self.install()

    def set(self, **kv):
        for k, v in kv.items():
            p = self.state / k
            if v is None:
                if p.exists():
                    p.unlink()
            else:
                p.write_text(v)

    def put(self, rel, data, mode=0o644):
        p = self.gb / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data if isinstance(data, bytes) else data.encode())
        p.chmod(mode)

    def install(self, defects=()):
        """the stand-in installation, then the staged package's SHA256SUMS and build record that describe it"""
        if self.gb.exists():
            shutil.rmtree(self.gb)
        self.gb.mkdir()
        front = macho_bytes(U2, (15, 0), "@loader_path/libnvk_minecraft.dylib")
        rend = macho_bytes(U1, (15, 0), "@loader_path/libnvk_renderer.dylib")
        self.put("NVMTLDriver.bundle/Contents/MacOS/NVMTLDriver", macho_bytes(U3), 0o755)
        if "nouuid" in defects:          # nvmtl/'s copy and one beside the plugin, both Mach-O without LC_UUID
            self.put("nvmtl/libnvmtl_translate.dylib", macho_bytes(None), 0o755)
            self.put(BESIDE, macho_bytes(None) + b"its own signature", 0o755)
        elif "noxlate" not in defects:
            self.put("nvmtl/libnvmtl_translate.dylib", XLATE, 0o755)
        if "beside-same" in defects:     # the same build signed apart: other bytes, the same LC_UUID
            self.put(BESIDE, XLATE + b"its own signature", 0o755)
        if "beside-old" in defects:      # an older release's copy, kept by a package staged onto that release
            self.put(BESIDE, XLATE_OLD, 0o755)
        if "beside-script" in defects:   # a file there that is not Mach-O: the plugin's dlopen of it fails
            self.put(BESIDE, SCRIPT, 0o755)
        self.put("nvmtl/libvulkan_nouveau.dylib", rend, 0o755)
        self.put("nvmtl/libvulkan.1.4.362.dylib", macho_bytes(U3), 0o755)
        os.symlink("libvulkan.1.4.362.dylib", self.gb / "nvmtl" / "libvulkan.1.dylib")
        self.put("nvmtl/nvrm610.conf", "NVK_CURVES=1\n# --- my settings, kept on update ---\nNVMTL_X=1\n")
        self.put("nvmtl-allow.txt", "*\n")
        self.put("nvmtl/minecraft/lib/libnvk_minecraft.dylib", front, 0o755)
        self.put("nvmtl/minecraft/lib/libnvk_renderer.dylib", rend, 0o755)
        api = "1.4.362" if "manifest" in defects else "1.4.363"
        self.put("nvmtl/minecraft/nvk_bridge_icd.json", json.dumps({"file_format_version": "1.0.0", "ICD": {
            "library_path": "./lib/libnvk_minecraft.dylib", "api_version": api}}, indent=2))
        sha = hashlib.sha256(front).hexdigest()
        self.put("nvmtl/minecraft/nvk-bridge-runtime.properties", "\n".join(
            ["schema.version=1", "target.os=macos", "target.arch=x86_64", "minecraft.version=26.3",
             f"runtime.id=nvk-macos-gpu-copy-{sha[:12]}", "library.path=lib/libnvk_minecraft.dylib",
             f"library.sha256={sha}", "presentation=gpu-copy"]) + "\n")
        if "extra" in defects:
            self.put("nvmtl/minecraft/README.txt", "a file the package does not put there\n")
        for n in ("libnvk_zink.dylib", "libnvk_cgl.dylib"):
            self.put(f"nvmtl/zink/{n}", macho_bytes(U3), 0o755)
        self.put("nvmtl/zink/libnvk_zink_vulkan.dylib", front, 0o755)
        self.put("nvmtl/zink/libnvk_renderer.dylib", rend, 0o755)
        control = "exit 1" if "control" in defects else 'echo "Trusted GPU bundle: /Library/GPUBundles/$1.bundle"'
        if "noprobe" not in defects:
            self.put("nvmtl/zink/gld_bundle_probe", f'#!/bin/sh\ncase "$1" in NVMTLDriver) {control} ;; '
                     f'*) echo "Trusted GPU bundle: /Library/GPUBundles/$1.bundle" ;; esac\n', 0o755)
        sync = "exit 1" if "sync" in defects else ("echo 'PASS: 1000 four-thread barrier generations, one serial return each; "
                                                   "100 queue completion/teardown cycles'")
        self.put("nvmtl/zink/zink_sync_probe", f"#!/bin/sh\n{sync}\n", 0o755)
        self.put("nvmtl/zink/cgl-dispatch-map.json", json.dumps({"capabilities": {"core": {"version": 41},
                                                                                  "compatibility": {"version": 21}}}))
        self.put("nvmtl/zink/MESA-SOURCE.json", json.dumps({"commit": "0" * 40 if "mesa" in defects else MESA}))
        self.put("NVKZinkGLDriver.bundle/Contents/Info.plist", plistlib.dumps({"CFBundleIdentifier": "com.nullmoth.NVKZinkGLDriver"}))
        self.put("NVKZinkGLDriver.bundle/Contents/MacOS/NVKZinkGLDriver", macho_bytes(U3), 0o755)
        lines = []
        for i, p in enumerate(sorted(x for x in self.gb.rglob("*") if x.is_file() and not x.is_symlink())):
            rel = "Library/GPUBundles/" + str(p.relative_to(self.gb))
            h = hashlib.sha256(p.read_bytes()).hexdigest()
            if rel.endswith("nvrm610.conf"):
                h = "0" * 64        # the installer appends the user's block: never compared
            lines.append(f"{h}  {'./' if i % 2 else ''}{rel}")
        lines.append("f" * 64 + "  Library/Extensions/NVRM.kext/Contents/Info.plist")
        (self.pkgroot / "SHA256SUMS").write_text("\n".join(lines) + "\n")
        (self.pkgroot / "NULLMOTH-LOCAL-BUILD.txt").write_text(
            "This package was built with ./build.sh from a checkout of the NullMoth driver, not by its release process.\n"
            f"built:      2026-11-16T03:00:00Z\ncommit:     {EXPECT}\n")

    def checks(self, by_file):
        card = self.src / "tools" / "card"
        for f in card.glob("checks_*.py"):
            f.unlink()
        for name, rows in by_file.items():
            if isinstance(rows, str):
                (card / name).write_text(rows)
            else:
                (card / name).write_text("CHECKS = " + repr(rows) + "\n")

    def listing(self):
        """every path in the scratch world but the stand-ins' own folder and the runs' folders"""
        out = set()
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if Path(dirpath, d) not in (self.state, self.base / "runs")]
            out |= {str(Path(dirpath, n)) for n in dirnames + filenames}
        return out

    def run(self, argv, no_kill=False, kill_poll=3, timeout=180, fault=False, signal_when=None, unreadable=()):
        cfg = {"runner": str(RUNNER), "system": self.system, "src": str(self.src), "gb": str(self.gb),
               "audit_tmp": [str(self.root / "tmp")], "icd": str(self.root / "icd"),
               "exec_only_under": [os.path.realpath(str(self.root)), PY], "kill_poll": kill_poll, "no_kill": no_kill, "fault": fault,
               "unreadable": [str(self.gb / r) for r in unreadable], "argv": argv}
        (self.state / "wrap.py").write_text(WRAP)
        (self.state / "cfg.json").write_text(json.dumps(cfg))
        for f in ("calls.log", "ran.log"):
            if (self.state / f).exists():
                (self.state / f).unlink()
        env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin", "LANG": "C", "TMPDIR": str(self.root / "tmp"),
               "NVK_INHERITED_KNOB": "1", "VK_ICD_FILENAMES": "/nowhere.json", "SDKROOT": "/nowhere",
               "NVK_DIAG": "1"}
        before = self.listing()
        p = subprocess.Popen([PY, "-B", str(self.state / "wrap.py"), str(self.state / "cfg.json")], env=env,
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             start_new_session=True)
        if signal_when:     # (a line of ran.log, a signal): sent to the runner once that check has started
            line, sig = signal_when
            t0 = time.monotonic()
            while time.monotonic() - t0 < 60 and line not in self.ran_lines():
                time.sleep(0.1)
            time.sleep(0.3)
            os.kill(p.pid, sig)
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            out, err = p.communicate(timeout=10)
            out += "\n(the runner under test did not end in time)"
        self.leftover = sorted(self.listing() - before)
        return p.returncode, out + err

    def calls(self):
        p = self.state / "calls.log"
        return p.read_text().splitlines() if p.exists() else []

    def ran(self):
        p = self.state / "ran.log"
        return p.read_text().split() if p.exists() else []

    def ran_lines(self):
        p = self.state / "ran.log"
        return p.read_text().splitlines() if p.exists() else []


def prow(cid, mode, *extra, kind="gpu", order=300, source="prog.m", **kw):
    """a stand-in row of tools/card/checks_plugin.py"""
    c = {"id": cid, "part": cid.split(".")[0], "title": f"stand-in {mode}", "kind": kind, "order": order,
         "build": [{"argv": ["/usr/bin/xcrun", "clang", "-fobjc-arc", "{SRC}/tools/card/" + source, "-o", "{BIN}/" + cid + "/nvmtltest"],
                    "limit": 30}],
         "argv": ["{BIN}/" + cid + "/nvmtltest", mode] + list(extra), "limit": 15, "expect": {"rc": 0, "stdout": ["^PASS"]}}
    c.update(kw)
    return c


def results(w, out):
    try:
        data = json.loads(Path(out, "results.json").read_text())
    except (OSError, ValueError):
        return {}, {}
    return {r["id"]: r for r in data["rows"]}, data


def files_text(out):
    text = {}
    for p in Path(out).rglob("*"):
        if p.is_file() and "bin" not in p.relative_to(out).parts:
            text[str(p.relative_to(out))] = p.read_bytes().decode("utf-8", "replace")
    return text


def beats_stopped(path):
    """a heartbeat file that no longer grows: what wrote it was stopped"""
    if not path.exists():
        return False
    a = path.stat().st_size
    time.sleep(0.6)
    return path.stat().st_size == a


TOP = tempfile.mkdtemp(prefix="card-selftest-")
try:
    user = pwd.getpwuid(os.getuid()).pw_name
except KeyError:
    user = ""
host = os.uname().nodename
try:
    # ------------------------------------------------------------------------------------------ one whole run
    w = World(TOP, "main")
    rows = [
        prow("plugin.pass", "pass", order=300, info=[r"^INFO speed"]),
        prow("plugin.fail", "fail", order=301),
        prow("plugin.skip", "skip", order=302),
        prow("plugin.inactive", "inactive", order=303),
        prow("plugin.lacks", "lacks", order=304),
        prow("plugin.forbid", "forbid", order=305, expect={"rc": 0, "stdout": ["^PASS"], "stderr_forbid": [r"^NVK Zink: "]}),
        prow("plugin.broken", "pass", order=306, source="broken.m"),
        prow("plugin.nosource", "pass", order=307, sources=["tools/card/absent.m"]),
        prow("plugin.nofile", "pass", order=308, needs=["file:{GB}/absent"]),
        prow("plugin.window", "pass", order=700, window=True),
        prow("plugin.zink", "pass", order=600, needs=["zink"]),
        prow("plugin.xid", "xid", order=309),
        prow("plugin.crash", "crash70", order=310),
        prow("plugin.signal", "sigterm", order=311),
        prow("plugin.bg", "bg", order=312),
        prow("plugin.info", "info", order=313, info=[r"^INFO speed"]),
        {"id": "plugin.mapped", "part": "plugin", "title": "a program started by the table's python3", "kind": "cpu",
         "order": 200, "argv": ["/usr/bin/python3", "-B", "{SRC}/tools/card/prog.m", "pass"], "limit": 15,
         "env": {"CARD_EXTRA": "x{OUT}y"}, "expect": {"rc": 0, "stdout": ["^PASS", r"^ENV CARD_EXTRA=x/.*y$"]}},
    ]
    w.checks({"checks_plugin.py": rows})
    w.set(gl_bundle="AppleMetalOpenGLRenderer")
    out = w.base / "runs" / "main"
    rc, text = w.run(["--out", str(out), "--no-window"])
    got, data = results(w, out)
    want = {"plugin.pass": "pass", "plugin.fail": "FAIL", "plugin.skip": "skip", "plugin.inactive": "inactive",
            "plugin.lacks": "FAIL", "plugin.forbid": "FAIL", "plugin.broken": "build failed", "plugin.nosource": "skip",
            "plugin.nofile": "skip", "plugin.window": "skip", "plugin.zink": "inactive", "plugin.xid": "FAIL",
            "plugin.crash": "CRASH", "plugin.signal": "CRASH", "plugin.bg": "pass", "plugin.info": "pass",
            "plugin.mapped": "pass"}
    arm("a whole run with stand-ins ends with exit 1 (a failure) and DONE", rc == 1 and (out / "DONE").read_text().startswith("exit 1"), text)
    arm("each row's result", {k: got.get(k, {}).get("result") for k in want} == want,
        {k: (got.get(k, {}).get("result"), got.get(k, {}).get("why")) for k in want})
    why = {k: got.get(k, {}).get("why", "") for k in want}
    arm("the reasons: rc, stdout lacks, stderr has, the build's error: line, the source, the file, --no-window, Zink, Xid, signals",
        why["plugin.fail"] == "rc 1" and why["plugin.lacks"].startswith("stdout lacks") and "NVK Zink: no screen" in why["plugin.forbid"]
        and "error: this stand-in source does not build" in why["plugin.broken"] and "not in this checkout" in why["plugin.nosource"]
        and why["plugin.nofile"].startswith("needs ") and why["plugin.window"] == "left out with --no-window"
        and "AppleMetalOpenGLRenderer" in why["plugin.zink"] and why["plugin.xid"] == "Xid +2"
        and why["plugin.crash"] == "SIGBUS" and why["plugin.signal"] == "SIGTERM" and why["plugin.skip"] == "a precondition is missing",
        why)
    arm("a check's Xid rise is in results.json", got.get("plugin.xid", {}).get("xid_delta") == 2, got.get("plugin.xid"))
    ran = w.ran()
    arm("the rows that skip before running, and the build that failed, never started their program",
        not any(x in ran for x in ("plugin.nosource", "plugin.nofile", "plugin.window", "plugin.zink", "plugin.broken")), ran)
    arm("what a check left running in its group was stopped with it", beats_stopped(w.state / "plugin.bg.beat"))
    report = (out / "report.md").read_text()
    arm("report.md has the header, the table, the counts, the skips and why, the information and the audit",
        "| # | part | check | result | seconds | Xid | detail |" in report and "IOGLBundleName AppleMetalOpenGLRenderer" in report
        and "## Skipped and why" in report and "plugin.info: INFO speed 12 fps" in report
        and "## Written outside the work folder" in report and re.search(r"\b\d+ ran, \d+ failed, \d+ skipped", report), report[-3000:])
    logs = sorted(p.name for p in (out / "logs").iterdir())
    arm("one log per row that ran, a build log per build, and no raw stdout, stderr or plugin log left beside them",
        all(any(n.endswith("-" + k + ".log") for n in logs) for k in ("plugin.pass", "plugin.fail", "plugin.mapped"))
        and any(n.endswith("plugin.broken.build.log") for n in logs) and not any(n.endswith((".out", ".err", ".nvmtl.log")) for n in logs), logs)
    plog = next(((out / "logs" / n).read_text() for n in logs if n.endswith("-plugin.pass.log")), "")
    env_lines = dict(l[4:].split("=", 1) for l in plog.splitlines() if l.startswith("ENV "))
    arm("a check's environment is built from nothing: no knob, Vulkan or SDK variable of the runner's environment",
        env_lines and not any(k in env_lines for k in ("NVK_INHERITED_KNOB", "VK_ICD_FILENAMES", "SDKROOT"))
        and env_lines.get("PATH") == "/usr/bin:/bin:/usr/sbin:/sbin" and env_lines.get("NVMTL_PROCESS_ENV") == "1"
        and env_lines.get("TMPDIR", "").endswith("runs/main/tmp/plugin.pass/") and env_lines.get("LANG") == "en_US.UTF-8"
        and env_lines.get("NVMTL_LOG_PATH", "").endswith("-plugin.pass.nvmtl.log") and env_lines.get("NVMTL_NO_FAILED_AIR") == "1"
        and "runs/main/cache/" in env_lines.get("NVMTL_AIRCACHE", "") and "runs/main/cache/" in env_lines.get("MESA_SHADER_CACHE_DIR", ""),
        env_lines)
    arm("every check runs with NVK_DIAG=0 (it wins over NVK_DIAG=1 in nvrm610.conf), whatever the runner's own "
        "environment says", env_lines.get("NVK_DIAG") == "0", env_lines.get("NVK_DIAG"))
    arm("the plugin's log of a check is in that check's log", "--- nvmtl.log ---\nplugin log line from ~" in plog, plog[-800:])
    xc = [l for l in w.calls() if l.startswith("xcrun-env")]
    arm("builds get their own TMPDIR and clang module cache in the run folder, and no NVMTL_ variable",
        xc and all("runs/main/tmp/" in l and "runs/main/cache/clang" in l and "NVMTL_PROCESS_ENV=unset" in l for l in xc), xc)
    arm("a row's env is added with its placeholders filled in; /usr/bin/python3 is the table's python3",
        got.get("plugin.mapped", {}).get("result") == "pass" and any(l.startswith("python3 -B ") for l in w.calls()), w.calls())
    texts = files_text(out)
    raw = [w.home.as_posix(), str(w.state / "usertmp")]
    leaks = {n: [r for r in raw if r in t] for n, t in texts.items()}
    word = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(user) + r"(?![A-Za-z0-9_])") if len(user) > 1 else None
    hostw = re.compile(r"(?<![A-Za-z0-9_-])" + re.escape(host) + r"(?![A-Za-z0-9_-])", re.I) if len(host) > 1 else None
    for n, t in texts.items():
        if word and word.search(t):
            leaks.setdefault(n, []).append("the account name")
        if hostw and hostw.search(t):
            leaks.setdefault(n, []).append("the host name")
    leaks = {n: v for n, v in leaks.items() if v}
    arm("no file of the run holds the home folder, the account, the host or the user temporary folder",
        not leaks and "INFO home ~; account <user>; host <host>" in plog and "usertmp <usertmp>/x" in plog, (leaks, plog[:600]))
    arm("nothing was written outside the run folder", not w.leftover, w.leftover)
    arm("the runner's git calls only read (status, rev-parse, diff --quiet with --no-ext-diff and --no-textconv)",
        all(re.match(r"^git -c core\.fsmonitor=false -C \S+ (status --porcelain|rev-parse HEAD|diff --quiet --no-ext-diff --no-textconv )", l)
            for l in w.calls() if l.startswith("git ")) and any(l.startswith("git ") for l in w.calls()),
        [l for l in w.calls() if l.startswith("git ")])
    arm("the checkout gate's git diff compares build/build_nvk.sh with the other driver sources",
        any(" diff --quiet " in l and l.endswith(" build/build_nvk.sh") for l in w.calls()),
        [l for l in w.calls() if " diff " in l])
    genv = [l for l in w.calls() if l.startswith("git-env ")]
    arm("the runner's own reads (git through Apple's shim) get a TMPDIR in the run folder too",
        genv and all(l.endswith("runs/main/tmp/runner/") for l in genv), genv)
    arm("no caffeinate for a run whose windowed rows are left out", not (w.state / "caffeinate.pid").exists())
    arm("stdout carries one line per row and the table", text.count("\n| ") >= len(want) and "plugin.xid: FAIL (Xid +2)" in text, text[-2000:])

    # ------------------------------------------------------------------------------ what a check leaves outside
    w = World(TOP, "audit")
    w.checks({"checks_plugin.py": [prow("plugin.leave", "leave", order=300)]})
    out = w.base / "runs" / "l"
    rc, text = w.run(["--out", str(out)])
    report = (out / "report.md").read_text() if (out / "report.md").exists() else ""
    tail = report.split("## Written outside the work folder", 1)[-1]
    arm("report.md names what appeared outside the run folder (the plugin's /tmp files, its cache entries, the crash "
        "reports of the programs it ran, not of others), by name and with the home folder scrubbed",
        rc == 0 and re.search(r"^- \S*/tmp/nvmtl-left\.log$", tail, re.M) and "~/Library/Caches/nvmtl: 1 entries newer" in tail
        and re.search(r"^- \S*/tmp/nvk-diag-4242\.log$", tail, re.M)
        and "~/Library/Logs/DiagnosticReports/nvmtltest-2026-11-16-030000.ips" in tail and "Other-" not in tail
        and str(w.home) not in report, (rc, tail, text[-600:]))

    # ----------------------------------------------------------------------- a timeout, its own alarm, a deadline
    w = World(TOP, "timeout")
    w.checks({"checks_plugin.py": [prow("plugin.slow", "slow", "20", order=300, limit=2),
                                   prow("plugin.after", "pass", order=301),
                                   prow("plugin.cpu", "pass", kind="cpu", order=302)]})
    out = w.base / "runs" / "t"
    t0 = time.monotonic()
    rc, text = w.run(["--out", str(out)])
    took = time.monotonic() - t0
    got, _ = results(w, out)
    arm("a GPU check at its limit is TIMEOUT, later GPU checks are not run, a later CPU check runs, exit 4",
        rc == 4 and got.get("plugin.slow", {}).get("result") == "TIMEOUT" and got.get("plugin.after", {}).get("result") == "not run"
        and "timed out" in got.get("plugin.after", {}).get("why", "") and got.get("plugin.cpu", {}).get("result") == "pass",
        (rc, {k: (v["result"], v["why"]) for k, v in got.items()}, text[-1500:]))
    arm("its whole process group was killed (its background child stopped too), well before the program's own 20 s",
        beats_stopped(w.state / "plugin.slow.beat") and took < 15, took)
    w = World(TOP, "alarm")
    w.checks({"checks_plugin.py": [prow("plugin.alarm", "alarm", order=300), prow("plugin.after", "pass", order=301)]})
    out = w.base / "runs" / "a"
    rc, text = w.run(["--out", str(out)])
    got, _ = results(w, out)
    arm("a check ended by its own alarm (SIGALRM) is TIMEOUT and stops the later GPU checks",
        rc == 4 and got.get("plugin.alarm", {}).get("result") == "TIMEOUT" and got.get("plugin.after", {}).get("result") == "not run",
        (rc, got, text[-800:]))
    w = World(TOP, "maxsec")
    w.checks({"checks_plugin.py": [prow("plugin.slow", "slow", "20", order=300, limit=60)]})
    out = w.base / "runs" / "m"
    rc, text = w.run(["--out", str(out), "--max-seconds", "1"])
    got, _ = results(w, out)
    arm("--max-seconds caps a row's limit", got.get("plugin.slow", {}).get("result") == "TIMEOUT", (got, text[-800:]))
    w = World(TOP, "deadline")
    w.checks({"checks_plugin.py": [prow("plugin.slow", "slow", "1.5", order=300, kind="cpu"),
                                   prow("plugin.after", "pass", order=301, kind="cpu")]})
    out = w.base / "runs" / "d"
    rc, text = w.run(["--out", str(out), "--deadline", "1"])
    got, _ = results(w, out)
    arm("past the deadline nothing more starts: the rest is not run, exit 4",
        rc == 4 and got.get("plugin.slow", {}).get("result") == "pass" and got.get("plugin.after", {}).get("result") == "not run"
        and "deadline" in got.get("plugin.after", {}).get("why", ""), (rc, got, text[-800:]))

    # -------------------------------------------------------------------------------------------------- STUCK
    w = World(TOP, "stuck")
    w.checks({"checks_plugin.py": [prow("plugin.stuck", "slow", "20", order=300, limit=1),
                                   prow("plugin.after", "pass", order=301),
                                   prow("plugin.cpu", "pass", kind="cpu", order=302)]})
    out = w.base / "runs" / "s"
    rc, text = w.run(["--out", str(out)], no_kill=True, kill_poll=1)
    got, _ = results(w, out)
    log = next((p.read_text() for p in (out / "logs").glob("*-plugin.stuck.log")), "") if (out / "logs").exists() else ""
    arm("a check still alive after its kill is STUCK: the later GPU checks are not run, the report is written, exit 4",
        rc == 4 and got.get("plugin.stuck", {}).get("result") == "STUCK" and got.get("plugin.after", {}).get("result") == "not run"
        and "could not be stopped" in got.get("plugin.after", {}).get("why", "") and got.get("plugin.cpu", {}).get("result") == "pass"
        and (out / "DONE").exists(), (rc, got, text[-1500:]))
    arm("the STUCK check's log holds ps's lines of its process group", "--- ps of its process group ---" in log, log[-600:])
    for tag in ("plugin.stuck.main", "plugin.stuck"):
        try:
            pid = int((w.state / f"{tag}.pid").read_text())
            os.killpg(pid, signal.SIGKILL) if tag.endswith("main") else os.kill(pid, signal.SIGKILL)
        except (OSError, ValueError):
            pass
    w = World(TOP, "orphan")
    w.checks({"checks_plugin.py": [prow("plugin.orphan", "orphan", order=300), prow("plugin.after", "pass", order=301),
                                   prow("plugin.cpu", "pass", kind="cpu", order=302)]})
    out = w.base / "runs" / "o"
    rc, text = w.run(["--out", str(out)], no_kill=True, kill_poll=1)
    got, _ = results(w, out)
    log = next((p.read_text() for p in (out / "logs").glob("*-plugin.orphan.log")), "") if (out / "logs").exists() else ""
    arm("a check that ends while a process it started outlives the group's SIGKILL is STUCK: the later GPU checks are not "
        "run, a later CPU check runs, exit 4",
        rc == 4 and got.get("plugin.orphan", {}).get("result") == "STUCK" and got.get("plugin.after", {}).get("result") == "not run"
        and got.get("plugin.cpu", {}).get("result") == "pass", (rc, got, text[-1500:]))
    arm("its log names the process left in its group and the program's own exit", "stand-in-orphan" in log
        and "rc 0, a process of its group still running" in log, log[-800:])
    try:
        os.kill(int((w.state / "plugin.orphan.pid").read_text()), signal.SIGKILL)
    except (OSError, ValueError):
        pass

    # ---------------------------------------------------------------------------------------------- stop rules
    for mode, word in (("gaveup", "debug.nvrm_chan_gave_up rose by 1"), ("fence", "debug.nvrm_fence_state changed from 1 to 3"),
                       ("ws", "WindowServer's pid changed"),
                       ("cekept", "debug.nvrmfb_vram_ce_kept rose by 1 during plugin.cause: the copy engine's work"),
                       ("power",
                        "debug.nvrmfb_vram_power_events rose by 2 during plugin.cause: the GPU went through a standby"),
                       ("ceclear",
                        "debug.nvrmfb_vram_ce_clear went from 1 to 2 during plugin.cause: the copy engine's clear")):
        w = World(TOP, "stop-" + mode)
        for n, v in (("debug.nvrmfb_vram_ce_kept", 0), ("debug.nvrmfb_vram_power_events", 0),
                     ("debug.nvrmfb_vram_ce_clear", 1)):
            (w.state / "counters" / n).write_text(f"{v}\n")
        w.checks({"checks_plugin.py": [prow("plugin.cause", mode, order=300), prow("plugin.after", "pass", order=301),
                                       prow("plugin.cpu", "pass", kind="cpu", order=302)]})
        out = w.base / "runs" / mode
        rc, text = w.run(["--out", str(out)])
        got, data = results(w, out)
        arm(f"stop rule: {word} stops every later GPU check (exit 4), a later CPU check still runs",
            rc == 4 and got.get("plugin.cause", {}).get("result") == "pass" and got.get("plugin.after", {}).get("result") == "not run"
            and word in (data.get("stopped") or "") and got.get("plugin.cpu", {}).get("result") == "pass"
            and (mode != "ceclear"
                 or "went from 1 to 2 during plugin.cause" in (data.get("facts", {}).get("ce_clear_left") or "")),
            (rc, data.get("stopped"), got, data.get("facts"), text[-800:]))

    # ----------------------------------- a rebuilt channel, a kick, and the second check with a fault stopping the run
    w = World(TOP, "channel")
    for n, v in (("debug.nvrm_chan_dropped", 0), ("debug.nvrm_chan_kicks", 0), ("debug.nvrmfb_vram_ce_clear", 1),
                 ("debug.nvrmfb_vram_ce_cleared", 0)):
        (w.state / "counters" / n).write_text(f"{v}\n")
    w.checks({"checks_plugin.py": [prow("plugin.rebuilt", "recovered", order=300),
                                   prow("plugin.kicked", "kicks", order=301), prow("plugin.after", "pass", order=302),
                                   prow("plugin.xidce", "xidce", order=303), prow("plugin.later", "pass", order=304),
                                   prow("plugin.cpu", "pass", kind="cpu", order=305)]})
    out = w.base / "runs" / "c"
    rc, text = w.run(["--out", str(out)])
    got, data = results(w, out)
    why = {k: got.get(k, {}).get("why", "") for k in got}
    arm("a channel NVK rebuilt during a check fails it with the rebuild and the dropped submissions; a stalled channel "
        "rung again is noted on a check that passes; neither stops the next GPU check",
        got.get("plugin.rebuilt", {}).get("result") == "FAIL"
        and why.get("plugin.rebuilt") == "channel rebuilt +1; dropped +3"
        and got.get("plugin.kicked", {}).get("result") == "pass" and why.get("plugin.kicked") == "kicks +2"
        and got.get("plugin.after", {}).get("result") == "pass", (rc, why, data.get("stopped"), text[-800:]))
    report = (out / "report.md").read_text() if (out / "report.md").exists() else ""
    arm("the second GPU check with a fault (an Xid with copy-engine clears in it, which it says) stops every later GPU "
        "check and names both; a later CPU check runs; exit 4; the summary counts the checks with a kick",
        rc == 4 and got.get("plugin.xidce", {}).get("result") == "FAIL"
        and why.get("plugin.xidce") == "Xid +1; copy-engine clears +4 during it"
        and got.get("plugin.later", {}).get("result") == "not run" and got.get("plugin.cpu", {}).get("result") == "pass"
        and "2 GPU checks with a fault: plugin.rebuilt (channel rebuilt +1; dropped +3), plugin.xidce (Xid +1)"
        in (data.get("stopped") or "")
        and "1 with a stalled channel rung again (debug.nvrm_chan_kicks)" in report,
        (rc, why, data.get("stopped"), report[-1500:]))

    # ------------------------------------------------------------------------ a kick during the probe is no check's
    w = World(TOP, "probe-kick")
    (w.state / "counters" / "debug.nvrm_chan_kicks").write_text("0\n")
    w.checks({"checks_plugin.py": [prow("plugin.after", "pass", order=300)]})
    w.set(probe_kick="")
    out = w.base / "runs" / "pk"
    rc, text = w.run(["--out", str(out)])
    got, data = results(w, out)
    report = (out / "report.md").read_text() if (out / "report.md").exists() else ""
    arm("a stalled channel rung again during the device probe is not counted as a check with a kick, and stops nothing",
        rc == 0 and got.get("plugin.after", {}).get("result") == "pass" and not data.get("stopped")
        and "kicked" not in data.get("facts", {}) and "with a stalled channel rung again" not in report,
        (rc, data.get("stopped"), data.get("facts"), report[-1200:]))

    # ------------------------------------------------------------------ an Xid in the probe, a fault of the runner
    w = World(TOP, "probe-xid")
    w.checks({"checks_plugin.py": [prow("plugin.after", "pass", order=300), prow("plugin.cpu", "pass", kind="cpu", order=301)]})
    w.set(probe_xid="")
    out = w.base / "runs" / "px"
    rc, text = w.run(["--out", str(out)])
    got, data = results(w, out)
    arm("an Xid rise during the device probe stops the run before any check: exit 4, every row not run",
        rc == 4 and "debug.nvrm_xid_count rose by 5 during the device probe" in (data.get("stopped") or "")
        and {r["result"] for r in got.values()} == {"not run"} and "plugin.after" not in w.ran(), (rc, data.get("stopped"), got))
    w = World(TOP, "fault")
    w.checks({"checks_plugin.py": [prow("plugin.after", "pass", order=300)]})
    out = w.base / "runs" / "f"
    rc, text = w.run(["--out", str(out)], fault=True)
    got, data = results(w, out)
    arm("a fault of the runner itself still writes report.md, results.json and DONE (exit 4), with the reason",
        rc == 4 and (out / "DONE").exists() and (out / "DONE").read_text().startswith("exit 4")
        and "RuntimeError: a stand-in fault" in (data.get("stopped") or "")
        and got.get("plugin.after", {}).get("result") == "not run" and "Traceback" in text, (rc, data.get("stopped"), text[-800:]))

    for sig in (signal.SIGTERM, signal.SIGHUP):
        w = World(TOP, "signal-" + signal.Signals(sig).name)
        w.checks({"checks_plugin.py": [prow("plugin.slow", "slow", "20", order=300, limit=30), prow("plugin.after", "pass", order=301)]})
        out = w.base / "runs" / "sig"
        rc, text = w.run(["--out", str(out)], signal_when=("plugin.slow slow", sig))
        got, data = results(w, out)
        arm(f"{signal.Signals(sig).name} to the runner during a check: the check's process group is killed with it, the "
            "rest is not run, report.md and DONE are written, exit 4",
            rc == 4 and (out / "DONE").exists() and "interrupted" in (data.get("stopped") or "")
            and got.get("plugin.slow", {}).get("result") == "not run" and got.get("plugin.after", {}).get("result") == "not run"
            and beats_stopped(w.state / "plugin.slow.beat"), (rc, data.get("stopped"), got, text[-800:]))
        try:
            os.killpg(int((w.state / "plugin.slow.main.pid").read_text()), signal.SIGKILL)
        except (OSError, ValueError):
            pass

    # ------------------------------------------------------------- results.json after every row; the upload limit line
    w = World(TOP, "partial")
    w.checks({"checks_plugin.py": [prow("plugin.first", "upload", order=300),
                                   prow("plugin.slow", "slow", "20", order=301, limit=30),
                                   prow("plugin.after", "pass", order=302)]})
    out = w.base / "runs" / "p"
    rc, text = w.run(["--out", str(out)], signal_when=("plugin.slow slow", signal.SIGKILL))
    got, data = results(w, out)
    arm("a runner killed by SIGKILL during a check leaves results.json, marked partial, with the rows it finished and "
        "the rest null, and no DONE", rc == -signal.SIGKILL and data.get("partial") is True and data.get("exit") is None
        and got.get("plugin.first", {}).get("result") == "pass" and got.get("plugin.slow", {}).get("result") is None
        and got.get("plugin.after", {}).get("result") is None and not (out / "DONE").exists(), (rc, data, text[-600:]))
    arm("the plugin's line of a row's nvmtl.log that says its pending uploads reached a limit is in that row's "
        "information",
        got.get("plugin.first", {}).get("info") == [
            "driver: vk: tex upload async: 256 pending uploads hold 67108864 bytes of staging (limits 256 and "
            "268435456): an upload past a limit waits for its copy, as in 3.0"], got.get("plugin.first"))
    try:
        os.killpg(int((w.state / "plugin.slow.main.pid").read_text()), signal.SIGKILL)
    except (OSError, ValueError):
        pass

    # ---------------------------------------------------------------------------------- a window, caffeinate
    w = World(TOP, "window")
    w.checks({"checks_plugin.py": [prow("plugin.cpu", "pass", kind="cpu", order=200),
                                   prow("plugin.window", "pass", order=700, window=True)]})
    out = w.base / "runs" / "w"
    rc, text = w.run(["--out", str(out)])
    got, _ = results(w, out)
    caff = [l for l in w.calls() if l.startswith("caffeinate ")]
    arm("a window check with a usable GUI runs, under caffeinate -d -i -w <the runner's pid>, and every row passed: exit 0",
        rc == 0 and got.get("plugin.window", {}).get("result") == "pass" and len(caff) == 1
        and re.match(r"^caffeinate -d -i -w \d+$", caff[0]), (rc, caff, got, text[-800:]))
    arm("caffeinate was stopped after the last window check", beats_stopped(w.state / "caffeinate.beat"))
    w.set(probe_gui="session=1 console=1 logindone=1 locked=1 asleep=0")
    out = w.base / "runs" / "w2"
    rc, text = w.run(["--out", str(out)])
    got, _ = results(w, out)
    arm("a locked screen makes a window check a skip (needs gui), not a failure",
        rc == 0 and got.get("plugin.window", {}).get("result") == "skip" and "locked" in got.get("plugin.window", {}).get("why", ""),
        (rc, got.get("plugin.window"), text[-600:]))

    # --------------------------------------------------------------------------------------------- refusals
    w = World(TOP, "refuse")
    w.checks({"checks_plugin.py": [prow("plugin.pass", "pass")]})
    outside = Path(TOP) / "elsewhere"
    rc, text = w.run(["--out", str(outside)])
    arm("an --out outside ~/nullmoth-claude is refused (exit 3) and nothing is made", rc == 3 and not outside.exists()
        and "not inside" in text, text[-600:])
    full = w.base / "runs" / "full"
    full.mkdir(parents=True)
    (full / "x").write_text("")
    rc, text = w.run(["--out", str(full)])
    arm("an --out that is not empty is refused (exit 3)", rc == 3 and "not an empty folder" in text, text[-600:])
    rc, text = w.run(["--out", str(w.src / "out")])
    arm("an --out inside the checkout is refused (exit 3)", rc == 3 and not (w.src / "out").exists(), text[-600:])
    for what, setup, word in (
            ("a dirty checkout", dict(git_status=" M plugin/NVMTLDevice.m\n"), "not clean"),
            ("driver sources that differ from --expect", dict(git_diff_rc="1"), "differ from"),
            ("no accelerator in the IORegistry", dict(no_accel=""), "no NVAccel"),
            ("a device of another class", dict(probe_class="AMDRadeonDevice"), "no device of this driver")):
        w = World(TOP, "refuse-" + re.sub(r"\W+", "-", what))
        w.checks({"checks_plugin.py": [prow("plugin.pass", "pass")]})
        w.set(**setup)
        out = w.base / "runs" / "r"
        rc, text = w.run(["--out", str(out)])
        got, data = results(w, out)
        arm(f"refused: {what} (exit 3, report.md and DONE written, no check ran)",
            rc == 3 and word in (data.get("refused") or "") and (out / "DONE").exists() and "plugin.pass" not in w.ran(),
            (rc, data.get("refused"), w.ran(), text[-600:]))
    w = World(TOP, "refuse-kext")
    w.checks({"checks_plugin.py": [prow("plugin.pass", "pass")]})
    (w.src / "kexts" / "NVRM").mkdir(parents=True)
    (w.src / "kexts" / "NVRM" / "nvrm_boost_policy_abi.h").write_text("/* 3.0.1 */\n")
    out = w.base / "runs" / "r"
    rc, text = w.run(["--out", str(out)])
    got, data = results(w, out)
    arm("refused: a checkout with 3.0.1's NVRM.kext header on a loaded NVRM.kext without debug.nvrm_boost_policy (the "
        "kexts of the package before, until the restart)",
        rc == 3 and "has no debug.nvrm_boost_policy" in (data.get("refused") or "")
        and "restart after installing" in (data.get("refused") or "") and "plugin.pass" not in w.ran(),
        (rc, data.get("refused"), text[-600:]))
    (w.state / "counters" / "debug.nvrm_boost_policy").write_text("0\n")
    (w.state / "counters" / "debug.nvrmfb_vram_ce_clear").write_text("1\n")
    (w.gb / "nvmtl" / "nvrm610.conf").write_text("NVK_CURVES=1\n# --- my settings, kept on update ---\nBOOST=0\n")
    out = w.base / "runs" / "r2"
    rc, text = w.run(["--out", str(out)])
    got, data = results(w, out)
    report = (out / "report.md").read_text() if (out / "report.md").exists() else ""
    ks = data.get("facts", {}).get("kexts_start", {})
    arm("the same checkout on a kext that answers runs; the boost policy and copy-engine state are recorded at the "
        "start and the end, a name the kexts lack as absent; the knobs list 2.9's short BOOST= line, which NVK still "
        "reads",
        rc == 0 and got.get("plugin.pass", {}).get("result") == "pass" and ks.get("debug.nvrm_boost_policy") == "0"
        and ks.get("debug.nvrmfb_vram_ce_clear") == "1" and ks.get("debug.nvrmfb_vram_ce_kept") == "absent"
        and data.get("facts", {}).get("kexts_end", {}).get("debug.nvrm_boost_policy") == "0"
        and re.search(r"^- boost policy and copy-engine clear \(absent before 3\.0\.1\): "
                      r"start boost_policy=0 .*ce_clear=1 .*; end ", report, re.M)
        and re.search(r"^- nvrm610\.conf knobs: NVK_CURVES=1; BOOST=0", report, re.M),
        (rc, ks, report[:2500]))
    w = World(TOP, "refuse-commit")
    w.checks({"checks_plugin.py": [prow("plugin.pass", "pass")]})
    out = w.base / "runs" / "r"
    rc, text = w.run(["--out", str(out), "--expect", "1234567"])
    got, data = results(w, out)
    arm("refused: a staged package of another commit than --expect", rc == 3 and "not 1234567" in (data.get("refused") or ""), text[-600:])
    w = World(TOP, "refuse-hash")
    w.checks({"checks_plugin.py": [prow("plugin.pass", "pass")]})
    (w.gb / "nvmtl" / "zink" / "libnvk_cgl.dylib").write_bytes(b"another build")
    out = w.base / "runs" / "r"
    rc, text = w.run(["--out", str(out)])
    got, data = results(w, out)
    arm("refused: an installed file that differs from the staged package (nvrm610.conf, whose line does not match, is not "
        "compared)", rc == 3 and "1 of" in (data.get("refused") or "") and "libnvk_cgl.dylib" in (data.get("refused") or ""),
        (data.get("refused"), text[-600:]))
    w = World(TOP, "refuse-key")
    w.checks({"checks_plugin.py": [prow("plugin.pass", "pass", timeout=3)]})
    rc, text = w.run(["--list"])
    arm("an unknown key refuses the run when the checks are loaded, naming the file, the row and the key",
        rc == 3 and "tools/card/checks_plugin.py: plugin.pass: unknown key 'timeout'" in text and not w.calls(), text[-600:])

    # ------------------------------------------------------------------------------------------- --list, --plan
    w = World(TOP, "list")
    w.checks({"checks_plugin.py": [prow("plugin.b", "pass", order=301), prow("plugin.a", "pass", order=300, kind="cpu")]})
    rc, text = w.run(["--list"])
    arm("--list prints each row's id, part, kind, order and title in order, and starts nothing",
        rc == 0 and "plugin.a\tplugin\tcpu\t300\tstand-in pass\nplugin.b\tplugin\tgpu\t301\tstand-in pass" in text
        and not w.calls() and not w.leftover, (text, w.calls()))
    out = w.base / "runs" / "plan"
    rc, text = w.run(["--plan", "--out", str(out)])
    calls = [l.split()[0] for l in w.calls()]
    arm("--plan runs the read gates and prints the build and run plan, builds nothing, starts no check program, writes nothing",
        rc == 0 and "plan: no GPU program runs" in text and "run (15 s): " in text and "nvmtltest pass" in text
        and "xcrun" not in calls and not w.ran() and not out.exists() and not w.leftover and "git" in calls, (text[-1500:], calls))

    # ------------------------------------------------------------ the real install rows on a stand-in installation
    w = World(TOP, "install")
    w.checks({"checks_install.py": (CARD / "checks_install.py").read_text()})
    out = w.base / "runs" / "i"
    rc, text = w.run(["--out", str(out)])
    got, data = results(w, out)
    arm("tools/card/checks_install.py on a whole stand-in installation: every row passes, exit 0",
        rc == 0 and len(got) >= 8 and all(r["result"] == "pass" for r in got.values()),
        (rc, {k: (v["result"], v["why"]) for k, v in got.items()}, text[-1500:]))
    pt = got.get("install.plugin-translator", {})
    arm("install.plugin-translator with no copy beside the plugin: it passes, the plugin opens nvmtl/'s, and the "
        "checkout's own loader is read as preferring the copy beside the plugin (nvmtl_xlate_ready)",
        pt.get("result") == "pass" and pt.get("why") == f"the plugin opens {w.gb}/nvmtl/libnvmtl_translate.dylib: no "
        "copy beside it" and any(l.startswith("INFO plugin source: plugin/NVMTLLibraryLoad.m (nvmtl_xlate_ready) calls "
                                              "dladdr") for l in pt.get("info", []))
        and f"INFO translator nvmtl/libnvmtl_translate.dylib: x86_64, LC_UUID {XU_T}, {len(XLATE)} bytes, sha256 "
            f"{hashlib.sha256(XLATE).hexdigest()[:12]}" in pt.get("info", []), pt)
    w.install(defects=("manifest", "extra", "mesa", "control", "sync", "beside-old"))
    w.set(gl_bundle="AppleMetalOpenGLRenderer")
    out = w.base / "runs" / "i2"
    rc, text = w.run(["--out", str(out)])
    got, data = results(w, out)
    want = {"install.identity": "pass", "install.zink-bundle": "inactive", "install.minecraft-files": "FAIL",
            "install.zink-files": "pass", "install.mesa-source": "FAIL", "install.plugin-translator": "FAIL",
            "install.gl-bundle-trusted": "skip", "install.zink-sync-probe": "FAIL"}
    arm("and with a defect for each row: Apple's OpenGL named, an extra file and another api_version, another Mesa "
        "commit, an older translator beside the plugin, a lookup that does not find the control, a failing barrier "
        "probe",
        rc == 1 and {k: got.get(k, {}).get("result") for k in want} == want
        and "README.txt" in got["install.minecraft-files"]["why"] and "1.4.362" in got["install.minecraft-files"]["why"],
        (rc, {k: (v["result"], v["why"]) for k, v in got.items()}))
    pt = got.get("install.plugin-translator", {})
    beside = f"{w.gb}/{BESIDE}"
    # execute() keeps the first 200 characters of each INFO line, so a line naming a path under a long TMPDIR is cut
    arm("install.plugin-translator with an older build beside the plugin (install.identity still passes, as on the Mac "
        "of round 11): one FAIL line naming the file the plugin opens, both LC_UUIDs, the plugin's preference and what "
        "it means for the run",
        got.get("install.identity", {}).get("result") == "pass"
        and pt.get("why") == f"the Metal plugin opens {beside} (LC_UUID {XU_OLD_T}), not the build of "
        f"{w.gb}/nvmtl/libnvmtl_translate.dylib (LC_UUID {XU_T}), because plugin/NVMTLLibraryLoad.m "
        f"(nvmtl_xlate_ready) prefers the copy beside the plugin: this run's Metal rows translate with that copy, "
        f"while its readback rows use {w.gb}/nvmtl/libnvmtl_translate.dylib"
        and f"INFO the plugin opens {beside}"[:200] in pt.get("info", []), pt)
    report = (out / "report.md").read_text()
    old_line = ("- install.plugin-translator: INFO translator "
                f"{BESIDE}: x86_64, LC_UUID {XU_OLD_T}, {len(XLATE_OLD)} bytes, "
                f"sha256 {hashlib.sha256(XLATE_OLD).hexdigest()[:12]}")
    arm("and report.md lists that failing read row's INFO lines (each copy's LC_UUID, size and SHA-256 prefix) under "
        "Information", old_line in report.split("## Information", 1)[-1]
        and f"| install.plugin-translator | FAIL (the Metal plugin opens {beside} " in report, report[-3000:])
    arm("and nothing was written outside the run folder", not w.leftover, w.leftover)
    w.install(defects=("noprobe", "beside-same"))
    w.set(gl_bundle="NVKZinkGLDriver")
    out = w.base / "runs" / "i3"
    rc, text = w.run(["--out", str(out)])
    got, data = results(w, out)
    arm("and with gld_bundle_probe missing: the lookup row fails (it is not a skip) and so does the zink folder's row",
        rc == 1 and got.get("install.gl-bundle-trusted", {}).get("result") == "FAIL"
        and got.get("install.zink-files", {}).get("result") == "FAIL", (rc, {k: (v["result"], v["why"]) for k, v in got.items()}))
    pt = got.get("install.plugin-translator", {})
    arm("and with a copy beside the plugin of the same build, signed apart (other bytes, the same LC_UUID): "
        "install.plugin-translator passes, the plugin opening that copy",
        pt.get("result") == "pass" and pt.get("why") == f"the plugin opens {beside}, the same build as "
        f"{w.gb}/nvmtl/libnvmtl_translate.dylib (LC_UUID {XU_T})", pt)
    left = list(w.leftover)
    only = ["--only", r"^install\.plugin-translator$"]
    w.install(defects=("noxlate",))
    out = w.base / "runs" / "i4"
    rc, text = w.run(only + ["--out", str(out)])
    got, data = results(w, out)
    pt = got.get("install.plugin-translator", {})
    arm("install.plugin-translator with nvmtl/libnvmtl_translate.dylib missing: it fails, exit 1",
        rc == 1 and list(got) == ["install.plugin-translator"] and pt.get("result") == "FAIL"
        and pt.get("why") == f"{w.gb}/nvmtl/libnvmtl_translate.dylib, the translator of the readback rows and the "
        "plugin's fallback, is missing", (rc, pt, text[-800:]))
    left += w.leftover
    x = f"{w.gb}/nvmtl/libnvmtl_translate.dylib"
    w.install(defects=("beside-script",))
    out = w.base / "runs" / "i4s"
    rc, text = w.run(only + ["--out", str(out)])
    got, data = results(w, out)
    pt = got.get("install.plugin-translator", {})
    arm("install.plugin-translator with a file beside the plugin that is not Mach-O: it fails, saying that the "
        "plugin's dlopen fails there without falling back to nvmtl/'s, and keeps its INFO lines",
        rc == 1 and pt.get("result") == "FAIL"
        and pt.get("why") == f"the Metal plugin opens {beside}, which is not Mach-O, because plugin/NVMTLLibraryLoad.m "
        f"(nvmtl_xlate_ready) prefers the copy beside the plugin: its dlopen fails there, and the plugin does not fall "
        f"back to {x}"
        and f"INFO translator {BESIDE}: not Mach-O, {len(SCRIPT)} bytes, sha256 "
            f"{hashlib.sha256(SCRIPT).hexdigest()[:12]}" in pt.get("info", [])
        and f"INFO translator nvmtl/libnvmtl_translate.dylib: x86_64, LC_UUID {XU_T}, {len(XLATE)} bytes, sha256 "
            f"{hashlib.sha256(XLATE).hexdigest()[:12]}" in pt.get("info", []), (rc, pt, text[-800:]))
    left += w.leftover
    w.install(defects=("nouuid",))
    out = w.base / "runs" / "i4n"
    rc, text = w.run(only + ["--out", str(out)])
    got, data = results(w, out)
    pt = got.get("install.plugin-translator", {})
    arm("install.plugin-translator with nvmtl/'s copy and the one beside the plugin both without LC_UUID: it fails, as "
        "two builds it cannot tell apart",
        rc == 1 and pt.get("result") == "FAIL"
        and pt.get("why") == f"the Metal plugin opens {beside} (LC_UUID none), not the build of {x} (LC_UUID none), "
        f"because plugin/NVMTLLibraryLoad.m (nvmtl_xlate_ready) prefers the copy beside the plugin: this run's Metal "
        f"rows translate with that copy, while its readback rows use {x}", (rc, pt, text[-800:]))
    left += w.leftover
    w.install(defects=("beside-old",))
    out = w.base / "runs" / "i4u"
    rc, text = w.run(only + ["--out", str(out)], unreadable=(BESIDE,))
    got, data = results(w, out)
    pt = got.get("install.plugin-translator", {})
    arm("install.plugin-translator with an older build beside the plugin that this account cannot read: it passes, "
        "since the plugin's own access(R_OK) test then opens nvmtl/'s",
        rc == 0 and pt.get("result") == "pass" and pt.get("why") == f"the plugin opens {x}: no readable copy beside it"
        and f"INFO translator {BESIDE}: there but not readable by this account" in pt.get("info", [])
        and f"INFO the plugin opens {x}"[:200] in pt.get("info", []), (rc, pt, text[-800:]))
    left += w.leftover
    w.install(defects=("beside-old",))
    (w.src / "plugin" / "NVMTLLibraryLoad.m").write_text(LOADER_NO_BESIDE)
    out = w.base / "runs" / "i5"
    rc, text = w.run(only + ["--out", str(out)])
    got, data = results(w, out)
    pt = got.get("install.plugin-translator", {})
    no_pref = ("INFO plugin source: the row finds no function of plugin/NVMTLLibraryLoad.m that calls dladdr and names "
               "libnvmtl_translate.dylib by a relative path, so it cannot tell which copy the plugin prefers")
    arm("install.plugin-translator on a checkout whose plugin loader shows no preference for a copy beside it (its "
        "function that calls dladdr opens nvmtl/'s by its absolute path; the bare name only in comments, a log line "
        "and a function without dladdr), with an older copy beside the plugin: it fails closed, since it cannot tell "
        "which copy the plugin opens, and says so",
        rc == 1 and pt.get("result") == "FAIL"
        and pt.get("why") == f"the Metal plugin may open {beside}, of another build (LC_UUID {XU_OLD_T}, not {XU_T}), "
        f"in place of {x}: the row finds no function of plugin/NVMTLLibraryLoad.m that calls dladdr and names "
        f"libnvmtl_translate.dylib by a relative path, so it cannot tell which copy the plugin prefers, and releases "
        f"1.0.0 to 3.0.1 prefer the one beside the plugin whenever it is readable"
        and no_pref in pt.get("info", [])
        and f"INFO the plugin opens {x} or {beside}: its source does not tell which"[:200] in pt.get("info", []),
        (rc, pt, text[-800:]))
    left += w.leftover
    w.install()
    out = w.base / "runs" / "i6"
    rc, text = w.run(only + ["--out", str(out)])
    got, data = results(w, out)
    pt = got.get("install.plugin-translator", {})
    arm("and with no copy beside the plugin: it passes on nvmtl/'s, with the same INFO line",
        rc == 0 and pt.get("result") == "pass" and pt.get("why") == f"the plugin opens {x}: no copy beside it"
        and no_pref in pt.get("info", []), (rc, pt, text[-800:]))
    left += w.leftover
    w.install(defects=("beside-same",))
    out = w.base / "runs" / "i7"
    rc, text = w.run(only + ["--out", str(out)])
    got, data = results(w, out)
    pt = got.get("install.plugin-translator", {})
    arm("and with a copy of the same build beside the plugin: it passes, whichever of the two the plugin opens",
        rc == 0 and pt.get("result") == "pass"
        and pt.get("why") == f"the plugin opens {x} or {beside}, one build (LC_UUID {XU_T})", (rc, pt, text[-800:]))
    left += w.leftover
    arm("and nothing was written outside the run folder by these eight runs either", not left, left)
finally:
    shutil.rmtree(TOP, ignore_errors=True)

if fails:
    print(f"FAILED: {fails}")
    sys.exit(1)
print("PASS")
