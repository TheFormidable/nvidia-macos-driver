#!/usr/bin/env python3
"""card_selftest.py: the card checks of the installed driver's native Vulkan, native OpenGL, plugin and translator
parts, in one unattended run on a Mac with the driver installed and loaded.

  cd ~/nullmoth-claude/src && /usr/bin/python3 -B -I tools/card_selftest.py [--out DIR] [--expect COMMIT]
      [--only REGEX] [--no-window] [--max-seconds N] [--deadline N] [--plan | --list]

The checks are the rows of every tools/card/checks_<part>.py (part: install, xlate, plugin, vulkan, zink, v301; v301
holds the checks of release 3.0.1's own changes, each of which skips on a checkout without that change). Each file
holds CHECKS, a list of dicts whose keys are listed in KEYS below; a key the runner does not know, an unknown
placeholder, a duplicate id, a program that is not a built check, an installed file or one of the runner's own system
programs, or a forbidden command word (NVK_DIAG among them: it sends NVK's output to /private/tmp) refuses the run
before anything is built. The runner sorts the rows by "order",
refuses the run unless this Mac runs the staged package of --expect (the gates below), builds every check from this
checkout with /usr/bin/xcrun clang, runs each on the installed driver one at a time, and writes, under
~/nullmoth-claude only:

  <out>/report.md      the header (macOS, CPU, Metal device, package, IOGLBundleName, checkout) and the table
  <out>/results.json   one object per row: id, part, result, rc, signal, seconds, xid_delta, detail, log
  <out>/logs/NN-id.log the argv, environment additions, limits, times, exit, verdict and its rule, counters, then the
                       program's stdout, stderr and its plugin log; NN-id.build.log for the build
  <out>/bin, tmp, cache the built programs, every child's TMPDIR, and the shader caches the children are pointed at
  <out>/DONE           written last, with the exit status
The default <out> is ~/nullmoth-claude/cardtest/<UTC stamp>; another must lie inside ~/nullmoth-claude, outside its
src and build2, and be empty or absent. Every byte the runner writes or prints passes one scrub: the home folder
becomes ~, the account name <user>, the host name <host>, the user temporary and cache folders <usertmp> and
<usercache>.

Gates (each refusal exits 3 with its reason and runs nothing on the GPU): the out folder and 2 GB free; a clean
checkout whose driver sources equal --expect (git diff tree to tree, no index write); the staged package's
NULLMOTH-LOCAL-BUILD.txt commit equals --expect; every SHA256SUMS line under Library/GPUBundles matches the installed
file (nvrm610.conf excluded, the installer appends the user's block to it); NVKZinkGLDriver.bundle, nvmtl/zink and
nvmtl/minecraft exist; the accelerator is in the IORegistry (its IOGLBundleName, NVKZinkGLDriver, gives the "zink"
need); and the device probe (tools/card/device_probe.m, 30 s) sees a default Metal device of class NVMTLDevice whose
name contains " (NVMTL over ", and tells whether a console GUI session is usable (the "gui" need). A refusal of the out
folder itself writes nothing; every later refusal writes report.md and DONE.

Each child starts from an environment built from nothing (HOME, USER, LOGNAME, LANG, PATH=/usr/bin:/bin:/usr/sbin:/sbin,
TMPDIR=<out>/tmp/<id>, the plugin's log, AIR cache and Mesa cache pointed into <out>, then the row's env), with
/dev/null as input, its output in files, in a session of its own, in <out>/tmp/<id>. At its limit, and when it ends, its
process group is killed and awaited for KILL_POLL seconds: a check with a process still alive then (the program, or one
it started) is STUCK. A STUCK check, a GPU check that timed out, a rise of debug.nvrm_chan_gave_up, of
debug.nvrmfb_vram_ce_kept or of debug.nvrmfb_vram_power_events, a change of debug.nvrm_fence_state or of WindowServer's
pid stops every later GPU check; a rise of debug.nvrm_xid_count, debug.nvrm_chan_recovered or debug.nvrm_chan_dropped
fails the check it happened in, and one of debug.nvrm_chan_kicks is noted in its reason. caffeinate -d -i -w <this pid>
holds the display awake only while windowed checks run.

Exit: 0 every check that ran passed (skips, inactive and fallback rows listed with their reason); 1 a FAIL, TIMEOUT,
CRASH or build failure; 2 usage; 3 refused; 4 stopped early. --list prints every row (id, part, kind, order, title)
and builds nothing; --plan runs the gates that only read and prints the build and run plan, runs no GPU program and
writes nothing.

It never uses sudo, installs, loads or copies anything outside <out>, changes a setting, reads a system binary, the
kernel collection or the dyld shared cache, opens the network, captures the screen, or kills a process that is not in
its own children's groups. Its system programs come from the SYSTEM table, which a host test points at stand-ins
(tools/test_card_selftest.py). Not yet run on a Mac: it was written and its host test run on Linux only."""
import sys
sys.dont_write_bytecode = True   # nor for the check files it loads (python3 -B, but from inside)
import argparse  # noqa: E402
import ast
import datetime
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
import stat
import struct
import subprocess
import time
import traceback
from pathlib import Path

# the commit of the package installed on the Mac the checks were written for
DEFAULT_EXPECT = "9c2b756663d7208149e498ddf1ec64c2d867593b"
SRC = Path(__file__).resolve().parents[1]                       # the checkout
CARD_DIR = None                                                 # None: SRC/tools/card
GB = "/Library/GPUBundles"
# Every system program the runner starts, by name. A check's argv or build argv that starts with one of these paths
# starts the program this table names instead; the host test points every entry at a stand-in.
SYSTEM = {
    "git": "/usr/bin/git",
    "xcrun": "/usr/bin/xcrun",
    "ioreg": "/usr/sbin/ioreg",
    "sysctl": "/usr/sbin/sysctl",
    "pgrep": "/usr/bin/pgrep",
    "ps": "/bin/ps",
    "sw_vers": "/usr/bin/sw_vers",
    "getconf": "/usr/bin/getconf",
    "caffeinate": "/usr/bin/caffeinate",
    "python3": "/usr/bin/python3",
    "sh": "/bin/sh",
}
_DEFAULT_PATHS = {v: k for k, v in SYSTEM.items()}
EXEC_ONLY_UNDER = None       # None, or folders and programs outside which nothing is started (set by the host test)
AUDIT_TMP_DIRS = ("/tmp", "/private/tmp")
# where the opt-in vulkan-for-apps.sh puts loader manifests (listed, never written)
ICD_DIR = "/usr/local/share/vulkan/icd.d"
KILL_POLL = 15               # seconds a killed check has to end before it is STUCK
MIN_FREE = 2 * 1024 ** 3
GB_OWNER_UID = 0             # the owner of the installed files (install.sh: root:wheel)
DEADLINE = 2400
PROBE_LIMIT = 30
PROBE_BUILD_LIMIT = 120
QUICK_LIMIT = 20             # git, ioreg, sysctl and the other short reads
LINE_CUT = 230
DETAIL_CUT = 120
LOG_KEEP = 4 * 1024 * 1024   # bytes of each stream kept in a check's log (the end of it)

PARTS = ("install", "xlate", "plugin", "vulkan", "zink", "v301")
KINDS = ("read", "cpu", "gpu")
KEYS = ("id", "part", "title", "kind", "order", "sources", "build", "argv", "env", "limit", "needs", "window", "expect",
        "rc_map", "info", "func")
PLACEHOLDERS = ("SRC", "OUT", "BIN", "LOGS", "TMP", "CACHE", "B", "TOOLCHAIN", "VKINC", "P", "X", "GB")
PLACEHOLDER = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")
NEEDS = ("gui", "zink", "vkinc", "spirv")
RC_WORDS = ("skip", "inactive", "fallback", "fail")
DEFAULT_RC_MAP = {3: "skip", 4: "inactive"}
EXPECT_KEYS = ("rc", "stdout", "stderr_forbid")
ID = re.compile(r"^[a-z0-9][a-z0-9.-]*$")
# words no check may put in its command lines or environment (the rules of the Mac the checks run on); NVK_DIAG=1 makes
# NVK copy its standard error into /private/tmp/nvk-diag-<pid>.log (nvk/nvk-diag-nofollow.patch)
FORBIDDEN = re.compile(r"(?<![\w.-])(sudo|nvram|csrutil|bless|kmutil|kextload|kextunload|kextutil|launchctl|defaults|"
                       r"tccutil|osascript|screencapture|spindump|killall|pkill|pmset|dtrace|lldb|java|java_home|NVK_DIAG|"
                       r"install\.sh|uninstall\.sh|vulkan-for-apps\.sh|MallocStackLogging\w*|NM_TEST_PLUGIN)(?![\w-])"
                       r"|/private/tmp|(?<![\w.~}-])/tmp(?![\w.-])|sysctl\s+-w")
# the runner's programs a check may start (the others in SYSTEM are the runner's own reads)
CHECK_PROGRAMS = ("/usr/bin/xcrun", "/usr/bin/python3", "/bin/sh")
# build/build_nvk.sh names the chain of NVK patches the package was built with (its CHAIN line)
DRIVER_PATHS = ("plugin", "zink", "translator", "nvk", "kexts", "dlss", "vendor", "tools/nvk-bridge/bridge.mm",
                "build/build_nvk.sh")
# Read around every GPU check; a name the loaded kexts lack reads as None and its rule does nothing. The channel
# counters are NVRM.kext's (kexts/NVRM/nvrm_chan_note.cpp: kicks, recovered and dropped are NVK's notes of a stalled or
# rebuilt channel); the nvrmfb_vram ones are NVRMFB's (kexts/NVRMFB/fb/nvrm-fb.cpp): power_events counts the GPU's
# standby, resume and lost messages, and the ce_ ones (3.0.1) its copy-engine clear of new video memory blocks.
COUNTERS = ("debug.nvrm_xid_count", "debug.nvrm_chan_recovered", "debug.nvrm_chan_gave_up", "debug.nvrm_chan_dropped",
            "debug.nvrm_fence_state", "debug.nvrm_chan_kicks", "debug.nvrmfb_vram_power_events",
            "debug.nvrmfb_vram_ce_clear", "debug.nvrmfb_vram_ce_cleared", "debug.nvrmfb_vram_ce_kept")
FAILED = ("FAIL", "TIMEOUT", "CRASH", "STUCK", "build failed")
BASE_ENV_NAMES = ("HOME", "USER", "LOGNAME", "LANG", "PATH", "TMPDIR", "PYTHONDONTWRITEBYTECODE")
ZINK_FILES = ("libnvk_zink.dylib", "libnvk_cgl.dylib", "libnvk_zink_vulkan.dylib", "libnvk_renderer.dylib",
              "gld_bundle_probe", "zink_sync_probe", "cgl-dispatch-map.json", "MESA-SOURCE.json")
MINECRAFT_FILES = {"lib/libnvk_minecraft.dylib": 0o755, "lib/libnvk_renderer.dylib": 0o755,
                   "nvk_bridge_icd.json": 0o644, "nvk-bridge-runtime.properties": 0o644}
ZINK_RECORDED_BUILDS = ("24H32", "25G241")   # docs/ZINK-OPENGL.md: the macOS builds the GLEngine layout was recorded on


class Refused(Exception):
    """a gate refused the run; the message says why"""


class Missing(Exception):
    """a placeholder has no value on this Mac (VKINC without Vulkan headers)"""


def utc(t=None):
    return datetime.datetime.fromtimestamp(time.time() if t is None else t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def inside(path, root):
    path, root = os.path.realpath(str(path)), os.path.realpath(str(root))
    return path == root or path.startswith(root.rstrip("/") + "/")


def text_of(data):
    return data.decode("utf-8", "replace") if isinstance(data, bytes) else (data or "")


def tail_of(path):
    """-> the text of a file the child wrote, its last LOG_KEEP bytes, or "" when there is none"""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - LOG_KEEP))
            data = f.read()
    except OSError:
        return ""
    head = f"[the first {size - LOG_KEEP} bytes are left out]\n" if size > LOG_KEEP else ""
    return head + text_of(data)


def signame(n):
    try:
        return signal.Signals(n).name
    except ValueError:
        return f"signal {n}"


# --------------------------------------------------------------------------------------------------------- scrub
class Scrub:
    """One function for every byte printed or written: the home folder (as given, its realpath and its form under
    /System/Volumes/Data) becomes ~, the user temporary and cache folders <usertmp> and <usercache>, the host name and
    its first label <host>, and the account name, as a whole word, <user>."""

    def __init__(self, home, user, host, usertmp="", usercache=""):
        pairs = []

        def forms(p):
            p = (p or "").rstrip("/")
            if len(p) < 2:
                return []
            out = {p, os.path.realpath(p)}
            for q in list(out):
                if q.startswith("/private/"):
                    out.add(q[len("/private"):])
                elif q.startswith(("/var/", "/tmp/")):
                    out.add("/private" + q)
            return out
        for p in forms(usertmp):
            pairs.append((p, "<usertmp>"))
        for p in forms(usercache):
            pairs.append((p, "<usercache>"))
        homes = forms(home)
        homes |= {"/System/Volumes/Data" + h for h in list(homes) if h.startswith("/")}
        for p in homes:
            pairs.append((p, "~"))
        pairs.sort(key=lambda x: -len(x[0]))
        self.rules = [(re.compile(re.escape(p) + r"(?![A-Za-z0-9._-])"), r) for p, r in pairs]
        names = []
        if host:
            names += [host, host.split(".")[0]]
        for h in sorted(set(n for n in names if len(n) > 1), key=len, reverse=True):
            self.rules.append((re.compile(r"(?<![A-Za-z0-9_-])" + re.escape(h) + r"(?![A-Za-z0-9_-])", re.I), "<host>"))
        if user and len(user) > 1:
            self.rules.append((re.compile(r"(?<![A-Za-z0-9_])" + re.escape(user) + r"(?![A-Za-z0-9_])"), "<user>"))

    def __call__(self, text):
        for rx, new in self.rules:
            text = rx.sub(new, text)
        return text


# ------------------------------------------------------------------------------------------------ child processes
def program(argv0):
    """the program a name of the SYSTEM table, or the default path of one, stands for"""
    if argv0 in SYSTEM:
        return SYSTEM[argv0]
    if argv0 in _DEFAULT_PATHS:
        return SYSTEM[_DEFAULT_PATHS[argv0]]
    return argv0


def _killpg(pgid, sig):
    os.killpg(pgid, sig)


def kill_group(pgid):
    try:
        _killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def spawn(argv, env, cwd, stdout, stderr):
    """the one place a process is started: in a session of its own, with /dev/null as input"""
    argv = [program(argv[0])] + [str(a) for a in argv[1:]]
    if EXEC_ONLY_UNDER is not None:
        real = os.path.realpath(argv[0])
        if not any(inside(real, r) for r in EXEC_ONLY_UNDER):
            raise OSError(f"not a program this run may start: {argv[0]}")
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, env=env, cwd=cwd,
                            start_new_session=True, close_fds=True)


def quick(argv, env, limit=QUICK_LIMIT):
    """-> (rc or None, stdout, stderr) of a short system read; its group is killed at the limit"""
    try:
        p = spawn(argv, env, "/", subprocess.PIPE, subprocess.PIPE)
    except OSError as e:
        return None, "", str(e)
    try:
        out, err = p.communicate(timeout=limit)
    except subprocess.TimeoutExpired:
        kill_group(p.pid)
        try:
            out, err = p.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            for f in (p.stdout, p.stderr):
                try:
                    f.close()
                except OSError:
                    pass
            out, err = b"", b""
        return None, text_of(out), text_of(err) + f"\n(no answer within {limit} s)"
    return p.returncode, text_of(out), text_of(err)


PS_ARGV = ["ps", "-A", "-o", "pid=,ppid=,pgid=,stat=,etime=,command="]


def group_lines(pgid):
    """-> ps's lines of the processes of a group that are not zombies, or None when ps gives no answer"""
    rc, out, _ = quick(PS_ARGV, {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C"})
    if rc != 0:
        return None
    return [l for l in out.splitlines() if len(l.split()) > 3 and l.split()[2] == str(pgid)
            and not l.split()[3].startswith("Z")]


def group_ended(pgid, until):
    """True once no process of the group is left. A program can end while a process it started stays in its group:
    the group is polled until the monotonic time until, then ps tells whether what is left is only zombies that their
    new parent has not reaped yet."""
    while True:
        try:
            _killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except OSError:      # a member this account cannot signal: still there
            pass
        if time.monotonic() >= until:
            return group_lines(pgid) == []
        time.sleep(0.1)


def run_limited(argv, env, cwd, out_path, err_path, limit):
    """runs one check or build step with its output in files. -> dict: rc (or None), killed (at the limit), stuck
    (the program or a process of its group still alive KILL_POLL s after the group's SIGKILL), seconds, pid,
    start_error"""
    res = {"rc": None, "killed": False, "stuck": False, "seconds": 0.0, "pid": None, "start_error": None}
    t0 = time.monotonic()
    try:
        with open(out_path, "wb") as o:
            e = open(err_path, "wb") if err_path else o
            try:
                p = spawn(argv, env, cwd, o, e)
            finally:
                if err_path:
                    e.close()
    except OSError as ex:
        res["start_error"] = str(ex)
        return res
    res["pid"] = p.pid
    RUNNING.add(p.pid)
    try:
        try:
            res["rc"] = p.wait(timeout=limit)
        except subprocess.TimeoutExpired:
            res["killed"] = True
        kill_group(p.pid)        # the program at its limit, or what it left running in its group
        until = time.monotonic() + KILL_POLL
        if res["rc"] is None:
            try:
                res["rc"] = p.wait(timeout=KILL_POLL)
            except subprocess.TimeoutExpired:
                res["stuck"] = True
        if not res["stuck"] and not group_ended(p.pid, until):
            res["stuck"] = True
    except BaseException:        # the runner interrupted (SIGTERM, SIGHUP) or failed: the check goes with it
        kill_group(p.pid)
        raise
    finally:
        if not res["stuck"]:
            RUNNING.discard(p.pid)
    res["seconds"] = time.monotonic() - t0
    return res


RUNNING = set()   # process groups of the children running now (stopped when the runner is interrupted)


# --------------------------------------------------------------------------------------------------- check files
def check_errors(c, part):
    """-> the reasons a row of tools/card/checks_<part>.py is refused"""
    if not isinstance(c, dict):
        return [f"a row is a {type(c).__name__}, not a dict"]
    errs = []
    name = c.get("id") if isinstance(c.get("id"), str) else "?"
    for k in c:
        if k not in KEYS:
            errs.append(f"unknown key {k!r} (the keys are: {', '.join(KEYS)})")
    cid = c.get("id")
    if not isinstance(cid, str) or not ID.match(cid):
        errs.append("id must be text of a-z, 0-9, '.' and '-'")
    elif not cid.startswith(part + "."):
        errs.append(f"id must start with its part, '{part}.'")
    if c.get("part") != part:
        errs.append(f"part must be {part!r}, the part its file is named for")
    if not isinstance(c.get("title"), str) or not c.get("title").strip():
        errs.append("title must be text")
    if c.get("kind") not in KINDS:
        errs.append(f"kind must be one of {', '.join(KINDS)}")
    if not isinstance(c.get("order"), int) or isinstance(c.get("order"), bool):
        errs.append("order must be an integer")
    for k in ("sources", "needs", "info"):
        v = c.get(k, [])
        if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
            errs.append(f"{k} must be a list of text")
    for s in c.get("sources", []) if isinstance(c.get("sources", []), list) else []:
        if isinstance(s, str) and (s.startswith("/") or ".." in Path(s).parts or PLACEHOLDER.search(s)):
            errs.append(f"sources {s!r} must be a path inside the checkout, relative, without placeholders")
    for n in c.get("needs", []) if isinstance(c.get("needs", []), list) else []:
        if isinstance(n, str) and n not in NEEDS and not (n.startswith("file:") and len(n) > 5):
            errs.append(f"needs {n!r} is not one of {', '.join(NEEDS)} or file:<path>")
    if "window" in c and not isinstance(c["window"], bool):
        errs.append("window must be true or false")
    has_argv, has_func = "argv" in c, "func" in c
    if has_argv == has_func:
        errs.append("a row has either argv (a program) or func (a read the runner makes), not both and not neither")
    if has_func:
        if c.get("func") not in READ_FUNCS:
            errs.append(f"func {c.get('func')!r} is not one of the runner's: {', '.join(sorted(READ_FUNCS))}")
        if c.get("kind") != "read":
            errs.append("func is for a row of kind read")
        for k in ("build", "env", "limit", "expect", "rc_map"):
            if k in c:
                errs.append(f"{k} has no meaning in a func row")
    lines = []   # (where, argv) of every command line, checked below
    if has_argv:
        if not isinstance(c["argv"], list) or not c["argv"] or not all(isinstance(a, str) for a in c["argv"]):
            errs.append("argv must be a non-empty list of text")
        else:
            lines.append(("argv", c["argv"]))
        lim = c.get("limit")
        if not isinstance(lim, (int, float)) or isinstance(lim, bool) or lim <= 0:
            errs.append("limit must be a number of seconds above 0")
    build = c.get("build", [])
    if not isinstance(build, list):
        errs.append("build must be a list of steps")
        build = []
    for i, step in enumerate(build):
        if not isinstance(step, dict) or set(step) != {"argv", "limit"}:
            errs.append(f"build step {i + 1} must be a dict with exactly argv and limit")
            continue
        if not isinstance(step["argv"], list) or not step["argv"] or not all(isinstance(a, str) for a in step["argv"]):
            errs.append(f"build step {i + 1}: argv must be a non-empty list of text")
        else:
            lines.append((f"build step {i + 1}", step["argv"]))
            av = step["argv"]
            for j, a in enumerate(av):
                if a == "-o" and (j + 1 >= len(av) or not av[j + 1].startswith("{BIN}/" + str(cid) + "/")):
                    errs.append(f"build step {i + 1}: -o must name a file in {{BIN}}/{cid}/")
        lim = step.get("limit")
        if not isinstance(lim, (int, float)) or isinstance(lim, bool) or lim <= 0:
            errs.append(f"build step {i + 1}: limit must be a number of seconds above 0")
    env = c.get("env", {})
    if not isinstance(env, dict) or not all(isinstance(k, str) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k) and isinstance(v, str)
                                            for k, v in env.items()):
        errs.append("env must be a dict of NAME: text")
        env = {}
    for where, av in lines:
        a0 = av[0]
        if not (a0 in CHECK_PROGRAMS or a0.startswith(("{BIN}/", "{GB}/", "{B}/"))):
            errs.append(f"{where}: {a0!r} is neither a program the check builds ({{BIN}}/...), an installed one "
                        f"({{GB}}/..., {{B}}/...) nor one of {', '.join(CHECK_PROGRAMS)}")
    strings = [a for _, av in lines for a in av] + [" ".join(av) for _, av in lines] + \
        [f"{k}={v}" for k, v in env.items()] + [n[5:] for n in c.get("needs", []) if isinstance(n, str) and n.startswith("file:")]
    for s in strings:
        for ph in PLACEHOLDER.findall(s):
            if ph not in PLACEHOLDERS:
                errs.append(f"unknown placeholder {{{ph}}} in {s!r} (known: {', '.join(PLACEHOLDERS)})")
        m = FORBIDDEN.search(s)
        if m:
            errs.append(f"{m.group(0)!r} in {s!r}: not allowed in a check on the Mac it runs on")
    expect = c.get("expect", {})
    if not isinstance(expect, dict) or any(k not in EXPECT_KEYS for k in expect):
        errs.append(f"expect must be a dict of {', '.join(EXPECT_KEYS)}")
        expect = {}
    if "rc" in expect and (not isinstance(expect["rc"], int) or isinstance(expect["rc"], bool)):
        errs.append("expect rc must be an integer")
    regexes = []
    for k in ("stdout", "stderr_forbid"):
        v = expect.get(k, [])
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            errs.append(f"expect {k} must be a list of regular expressions")
        else:
            regexes += v
    if isinstance(c.get("info", []), list):
        regexes += [x for x in c.get("info", []) if isinstance(x, str)]
    for rx in regexes:
        try:
            re.compile(rx, re.M)
        except re.error as e:
            errs.append(f"regular expression {rx!r}: {e}")
    rc_map = c.get("rc_map", DEFAULT_RC_MAP)
    if not isinstance(rc_map, dict) or not all(isinstance(k, int) and not isinstance(k, bool) and k != 0 and v in RC_WORDS
                                               for k, v in rc_map.items()):
        errs.append(f"rc_map must map nonzero exit codes to one of {', '.join(RC_WORDS)}")
    return [f"{name}: {e}" for e in errs]


def normalized(c):
    """the row with every optional key filled in"""
    c = dict(c)
    c.setdefault("sources", [])
    c.setdefault("build", [])
    c.setdefault("env", {})
    c.setdefault("needs", [])
    c.setdefault("window", False)
    c.setdefault("info", [])
    c["rc_map"] = dict(c.get("rc_map", DEFAULT_RC_MAP))
    e = dict(c.get("expect", {}))
    e.setdefault("rc", 0)
    e.setdefault("stdout", [])
    e.setdefault("stderr_forbid", [])
    c["expect"] = e
    return c


# what a check file may import and call: it defines CHECKS and nothing else when it is loaded (no I/O)
PURE_IMPORTS = {"re", "os", "os.path", "json", "itertools", "functools", "string", "textwrap", "copy", "collections",
                "__future__"}
IO_NAMES = {"open", "exec", "eval", "compile", "__import__", "input", "breakpoint", "getattr", "globals", "vars"}
IO_ATTRS = {"read_text", "write_text", "read_bytes", "write_bytes", "open", "mkdir", "unlink", "system", "popen",
            "remove", "rename", "listdir", "walk", "scandir", "glob", "iterdir", "stat", "exists", "getenv"}


def import_problems(text):
    """-> what in a check file could read, write or start something when it is loaded: an import beyond a few pure
    modules of the standard library, or a call that opens, lists or starts"""
    tree = ast.parse(text, feature_version=(3, 9))
    probs = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            probs += [f"import {a.name}" for a in node.names if a.name not in PURE_IMPORTS]
        elif isinstance(node, ast.ImportFrom) and (node.module or "") not in PURE_IMPORTS:
            probs.append(f"from {node.module} import")
        elif isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id in IO_NAMES:
                probs.append(f.id + "()")
            elif isinstance(f, ast.Attribute) and f.attr in IO_ATTRS:
                probs.append("." + f.attr + "()")
        elif isinstance(node, ast.Attribute) and node.attr == "environ":
            probs.append(".environ")
    return probs


def load_checks(card_dir):
    """-> (rows sorted by order then id, errors): every tools/card/checks_*.py, read and vetted (import_problems), then
    loaded from its path"""
    rows, errors = [], []
    card_dir = Path(card_dir)
    for f in sorted(card_dir.glob("checks_*.py")):
        rel = f"tools/card/{f.name}"
        part = f.stem[len("checks_"):]
        if part not in PARTS:
            errors.append(f"{rel}: '{part}' is not a part ({', '.join(PARTS)})")
            continue
        try:
            probs = import_problems(f.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError) as e:
            errors.append(f"{rel}: cannot be read as Python 3.9: {type(e).__name__}: {e}")
            continue
        if probs:
            errors.append(f"{rel}: not loaded, it would do more than define CHECKS: {', '.join(sorted(set(probs)))}")
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"card_checks_{part}", str(f))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as e:   # noqa: BLE001  (any error of a check file is a refusal with its reason)
            errors.append(f"{rel}: does not load: {type(e).__name__}: {e}")
            continue
        got = getattr(mod, "CHECKS", None)
        if not isinstance(got, list):
            errors.append(f"{rel}: holds no CHECKS list")
            continue
        for c in got:
            e = check_errors(c, part)
            if e:
                errors += [f"{rel}: {x}" for x in e]
            else:
                c = normalized(c)
                c["file"] = rel
                rows.append(c)
    seen = {}
    for c in rows:
        if c["id"] in seen:
            errors.append(f"{c['file']}: {c['id']}: the id is also in {seen[c['id']]}")
        seen.setdefault(c["id"], c["file"])
    rows.sort(key=lambda c: (c["order"], c["id"]))
    return rows, errors


# ------------------------------------------------------------------------------------------------------- Mach-O
LC_ID_DYLIB, LC_UUID, LC_VERSION_MIN_MACOSX, LC_BUILD_VERSION = 0xD, 0x1B, 0x24, 0x32
CPU = {0x01000007: "x86_64", 0x0100000C: "arm64", 7: "i386"}


def macho(path):
    """-> {"arches": [...], "uuid", "minos", "id"} of our own Mach-O file (the x86_64 slice of a fat one), read with
    Python; None when it is not Mach-O"""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
            magic = struct.unpack(">I", head[:4])[0] if len(head) >= 4 else 0
            if magic in (0xCAFEBABE, 0xCAFEBABF):
                n = struct.unpack(">I", head[4:8])[0]
                wide = magic == 0xCAFEBABF
                size = 32 if wide else 20
                slices = []
                for i in range(min(n, 16)):
                    at = 8 + i * size
                    if wide:
                        cpu, _, off = struct.unpack(">iiQ", head[at:at + 16])
                    else:
                        cpu, _, off = struct.unpack(">iiI", head[at:at + 12])
                    slices.append((cpu & 0xFFFFFFFF, off))
                arches = [CPU.get(cpu, hex(cpu)) for cpu, _ in slices]
                pick = [off for cpu, off in slices if cpu == 0x01000007] or [slices[0][1]] if slices else []
                if not pick:
                    return None
                info = _thin(f, pick[0])
                if info:
                    info["arches"] = arches
                return info
            return _thin(f, 0)
    except (OSError, struct.error):
        return None


def _thin(f, base):
    f.seek(base)
    hdr = f.read(32)
    if len(hdr) < 28:
        return None
    magic = struct.unpack("<I", hdr[:4])[0]
    if magic == 0xFEEDFACF:
        _, cpu, _, _, ncmds, sizeofcmds, _, _ = struct.unpack("<IiiIIIII", hdr[:32])
        f.seek(base + 32)
    elif magic == 0xFEEDFACE:
        _, cpu, _, _, ncmds, sizeofcmds, _ = struct.unpack("<IiiIIII", hdr[:28])
        f.seek(base + 28)
    else:
        return None
    cmds = f.read(min(sizeofcmds, 1 << 20))
    info = {"arches": [CPU.get(cpu & 0xFFFFFFFF, hex(cpu & 0xFFFFFFFF))], "uuid": None, "minos": None, "id": None}
    at = 0
    for _ in range(min(ncmds, 4096)):
        if at + 8 > len(cmds):
            break
        cmd, size = struct.unpack("<II", cmds[at:at + 8])
        body = cmds[at:at + size]
        if cmd == LC_UUID and size >= 24:
            info["uuid"] = body[8:24].hex()
        elif cmd == LC_BUILD_VERSION and size >= 24:
            info["minos"] = _version(struct.unpack("<I", body[12:16])[0])
        elif cmd == LC_VERSION_MIN_MACOSX and size >= 16 and not info["minos"]:
            info["minos"] = _version(struct.unpack("<I", body[8:12])[0])
        elif cmd == LC_ID_DYLIB and size >= 24:
            off = struct.unpack("<I", body[8:12])[0]
            info["id"] = body[off:].split(b"\0", 1)[0].decode("utf-8", "replace")
        if size < 8:
            break
        at += size
    return info


def _version(v):
    major, minor, patch = v >> 16, (v >> 8) & 0xFF, v & 0xFF
    return f"{major}.{minor}" + (f".{patch}" if patch else "")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sums_lines(text):
    """-> [(sha256, path without ./)] of a SHA256SUMS file (shasum -a 256 output: the path starts at column 67)"""
    out = []
    for line in text.splitlines():
        if len(line) < 67 or not re.match(r"^[0-9a-f]{64}[ ][ *]", line):
            continue
        p = line[66:]
        if p.startswith("./"):
            p = p[2:]
        out.append((line[:64], p))
    return out


# ---------------------------------------------------------------------------------------- reads the runner makes
def _file_rows(ctx, folder, wanted, lines, fails):
    """modes, owner and presence of an installed folder's files"""
    for rel, mode in wanted.items():
        p = os.path.join(folder, rel)
        try:
            st = os.lstat(p)
        except OSError:
            fails.append(f"{rel} is missing")
            continue
        if not stat.S_ISREG(st.st_mode):
            fails.append(f"{rel} is not a regular file")
            continue
        got = st.st_mode & 0o7777
        lines.append(f"INFO {rel} mode {got:o} uid {st.st_uid}")
        if mode is not None and got != mode:
            fails.append(f"{rel} has mode {got:o}, not {mode:o}")
        if st.st_mode & 0o022:
            fails.append(f"{rel} is writable by group or others")
        if st.st_uid != GB_OWNER_UID:
            fails.append(f"{rel} is owned by uid {st.st_uid}, not {GB_OWNER_UID}")


def _verdict_of(fails, ok_text, lines):
    for f in fails:
        lines.append(f"FAIL {f}")
    if fails:
        return "FAIL", "; ".join(fails), lines
    lines.append(f"PASS {ok_text}")
    return "pass", ok_text, lines


def install_identity(ctx, check):
    """the gates' facts as a row of the table: the package commit, the hashes, the driver sources"""
    f = ctx.facts
    lines = [f"INFO package commit {f.get('package_commit', '?')}",
             f"INFO {f.get('sums_equal', 0)} files under {GB} equal the staged package (nvrm610.conf not compared)",
             f"INFO checkout HEAD {f.get('head', '?')}; driver sources equal {ctx.expect}"]
    fails = []
    if not f.get("package_commit", "").startswith(ctx.expect):
        fails.append(f"the staged package is of {f.get('package_commit', 'no commit')}, not {ctx.expect}")
    if f.get("sums_differ"):
        fails.append(f"{len(f['sums_differ'])} installed files differ from the staged package")
    if not f.get("sums_equal"):
        fails.append(f"no SHA256SUMS line under {GB} was compared")
    return _verdict_of(fails, f"package {ctx.expect[:8]}: {f.get('sums_equal', 0)} files under {GB} equal the staged "
                       f"package; driver sources of HEAD equal it", lines)


def install_zink_bundle(ctx, check):
    """the OpenGL bundle the accelerator names, and its Info.plist and executable"""
    lines, fails = [], []
    bundle = os.path.join(GB, "NVKZinkGLDriver.bundle", "Contents")
    try:
        with open(os.path.join(bundle, "Info.plist"), "rb") as f:
            plist = plistlib.load(f)
        ident = plist.get("CFBundleIdentifier")
        lines.append(f"INFO CFBundleIdentifier {ident}")
        if ident != "com.nullmoth.NVKZinkGLDriver":
            fails.append(f"CFBundleIdentifier is {ident}, not com.nullmoth.NVKZinkGLDriver")
    except (OSError, plistlib.InvalidFileException, ValueError) as e:
        fails.append(f"NVKZinkGLDriver.bundle/Contents/Info.plist: {type(e).__name__}")
    exe = os.path.join(bundle, "MacOS", "NVKZinkGLDriver")
    m = macho(exe)
    if not m:
        fails.append("NVKZinkGLDriver.bundle/Contents/MacOS/NVKZinkGLDriver is missing or not Mach-O")
    else:
        lines.append(f"INFO NVKZinkGLDriver {'+'.join(m['arches'])} minos {m['minos']}")
    name = ctx.facts.get("gl_bundle", "none")
    lines.append(f"INFO IOGLBundleName {name}")
    if fails:
        return _verdict_of(fails, "", lines)
    if name != "NVKZinkGLDriver":
        lines.append(f"INACTIVE: IOGLBundleName is {name}")
        return "inactive", f"IOGLBundleName is {name}: Apple's OpenGL serves every application", lines
    return _verdict_of([], "IOGLBundleName is NVKZinkGLDriver and the bundle is in place", lines)


def _vk_api_version(ctx):
    """1.<minor>.<VK_HEADER_VERSION> of the Vulkan headers the runtime was built with (tools/nvk-bridge/build.sh)"""
    if ctx.vkinc:
        try:
            core = Path(ctx.vkinc, "vulkan", "vulkan_core.h").read_text(errors="replace")
            hv = re.search(r"^#define VK_HEADER_VERSION (\d+)$", core, re.M)
            mm = re.search(r"^#define VK_HEADER_VERSION_COMPLETE VK_MAKE_API_VERSION\(0, (\d+), (\d+), VK_HEADER_VERSION\)$", core, re.M)
            if hv and mm:
                return f"{mm.group(1)}.{mm.group(2)}.{hv.group(1)}", "the build's Vulkan headers"
        except OSError:
            pass
    return "1.4.363", "Mesa 26.3's headers (no Vulkan headers found in build2)"


def install_minecraft(ctx, check):
    """the native Vulkan runtime's folder: its four files, modes, manifest, identity and libraries"""
    lines, fails = [], []
    folder = os.path.join(GB, "nvmtl", "minecraft")
    found = set()
    for dirpath, dirnames, filenames in os.walk(folder):
        for n in filenames + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]:
            found.add(os.path.relpath(os.path.join(dirpath, n), folder))
    extra = sorted(found - set(MINECRAFT_FILES))
    if extra:
        fails.append("files beside the four: " + ", ".join(extra[:6]))
    _file_rows(ctx, folder, MINECRAFT_FILES, lines, fails)
    try:
        man = json.loads(Path(folder, "nvk_bridge_icd.json").read_text())
        want, why = _vk_api_version(ctx)
        got = (man.get("file_format_version"), man.get("ICD", {}).get("library_path"), man.get("ICD", {}).get("api_version"))
        lines.append(f"INFO manifest {got[0]} {got[1]} {got[2]} (expected api_version {want}, from {why})")
        if got != ("1.0.0", "./lib/libnvk_minecraft.dylib", want):
            fails.append(f"the manifest reads {got}, not ('1.0.0', './lib/libnvk_minecraft.dylib', {want!r})")
    except (OSError, ValueError, AttributeError) as e:
        fails.append(f"nvk_bridge_icd.json: {type(e).__name__}")
    front = os.path.join(folder, "lib", "libnvk_minecraft.dylib")
    rend = os.path.join(folder, "lib", "libnvk_renderer.dylib")
    try:
        props = dict(l.split("=", 1) for l in Path(folder, "nvk-bridge-runtime.properties").read_text().splitlines() if "=" in l)
        sha = sha256_file(front)
        want = {"library.path": "lib/libnvk_minecraft.dylib", "library.sha256": sha, "target.arch": "x86_64",
                "runtime.id": "nvk-macos-gpu-copy-" + sha[:12], "presentation": "gpu-copy", "schema.version": "1"}
        for k, v in want.items():
            if props.get(k) != v:
                fails.append(f"nvk-bridge-runtime.properties {k}={props.get(k)}, not {v}")
        lines.append(f"INFO runtime.id {props.get('runtime.id')}")
    except (OSError, ValueError) as e:
        fails.append(f"nvk-bridge-runtime.properties: {type(e).__name__}")
    nouveau = macho(os.path.join(GB, "nvmtl", "libvulkan_nouveau.dylib"))
    for path, ident in ((front, "@loader_path/libnvk_minecraft.dylib"), (rend, "@loader_path/libnvk_renderer.dylib")):
        m = macho(path)
        rel = os.path.relpath(path, folder)
        if not m:
            fails.append(f"{rel} is not Mach-O")
            continue
        lines.append(f"INFO {rel} {'+'.join(m['arches'])} minos {m['minos']} id {m['id']} uuid {m['uuid']}")
        if "x86_64" not in m["arches"]:
            fails.append(f"{rel} has no x86_64 code")
        if m["id"] != ident:
            fails.append(f"{rel} has install name {m['id']}, not {ident}")
        if path == front and m["minos"] != "15.0":
            fails.append(f"{rel} has minos {m['minos']}, not 15.0")
        if path == rend and (not nouveau or nouveau["uuid"] != m["uuid"]):
            fails.append(f"{rel}'s LC_UUID is not that of nvmtl/libvulkan_nouveau.dylib (the shader-cache identity)")
    loaders = sorted(n for n in os.listdir(os.path.join(GB, "nvmtl")) if n.startswith("libvulkan") and n != "libvulkan_nouveau.dylib") \
        if os.path.isdir(os.path.join(GB, "nvmtl")) else []
    for n in loaders:
        p = os.path.join(GB, "nvmtl", n)
        lines.append(f"INFO loader {n}" + (f" -> {os.readlink(p)}" if os.path.islink(p) else ""))
    built = os.path.join(ctx.b, "out", "vulkan-loader", "libvulkan.1.dylib")
    lines.append(f"INFO built loader {'present' if os.path.isfile(built) else 'absent'}: {built}")
    lines.append(f"INFO {ICD_DIR}: " + (", ".join(sorted(os.listdir(ICD_DIR))) if os.path.isdir(ICD_DIR) else "absent"))
    return _verdict_of(fails, "the runtime's four files, manifest, identity and libraries are as built", lines)


def install_zink_files(ctx, check):
    """nvmtl/zink: the files the package puts there, their kind, and the copies that must equal the runtime's"""
    lines, fails = [], []
    folder = os.path.join(GB, "nvmtl", "zink")
    _file_rows(ctx, folder, {n: None for n in ZINK_FILES}, lines, fails)
    try:
        extra = sorted(set(os.listdir(folder)) - set(ZINK_FILES))
        if extra:
            lines.append("INFO also in nvmtl/zink: " + ", ".join(extra))
    except OSError:
        pass
    for n in ZINK_FILES:
        p = os.path.join(folder, n)
        if n.endswith(".dylib"):
            m = macho(p)
            if not m:
                fails.append(f"{n} is not Mach-O")
            else:
                lines.append(f"INFO {n} {'+'.join(m['arches'])} minos {m['minos']} uuid {m['uuid']}")
        elif not n.endswith(".json") and os.path.isfile(p) and not os.access(p, os.X_OK):
            fails.append(f"{n} is not executable")   # the two probes; the program rows below run them
    pairs = (("libnvk_zink_vulkan.dylib", os.path.join(GB, "nvmtl", "minecraft", "lib", "libnvk_minecraft.dylib")),
             ("libnvk_renderer.dylib", os.path.join(GB, "nvmtl", "libvulkan_nouveau.dylib")))
    for n, other in pairs:
        a, b = macho(os.path.join(folder, n)), macho(other)
        if a and (not b or a["uuid"] != b["uuid"]):
            fails.append(f"{n}'s LC_UUID differs from {os.path.relpath(other, GB)}'s (it is a copy of it)")
    try:
        caps = json.loads(Path(folder, "cgl-dispatch-map.json").read_text()).get("capabilities", {})
        lines.append(f"INFO GL core {caps.get('core', {}).get('version')} compatibility "
                     f"{caps.get('compatibility', {}).get('version')} (cgl-dispatch-map.json)")
    except (OSError, ValueError, AttributeError) as e:
        fails.append(f"cgl-dispatch-map.json: {type(e).__name__}")
    build = ctx.facts.get("macos_build", "")
    lines.append(f"INFO macOS build {build}" + ("" if build in ZINK_RECORDED_BUILDS else
                 f": not one of the builds the GLEngine layout was recorded on ({', '.join(ZINK_RECORDED_BUILDS)}), so "
                 f"OpenGL may stay Apple's by design"))
    return _verdict_of(fails, "nvmtl/zink holds the package's eight files; its runtime copies match", lines)


def install_mesa_source(ctx, check):
    """the Mesa commit native OpenGL was built from (MESA-SOURCE.json) is the one the checkout pins"""
    lines, fails = [], []
    try:
        pin = re.search(r"^MESA_COMMIT = '([0-9a-f]{40})'", Path(ctx.src, "build", "prepare_zink.py").read_text(), re.M)
    except OSError:
        pin = None
    try:
        got = json.loads(Path(GB, "nvmtl", "zink", "MESA-SOURCE.json").read_text()).get("commit")
    except (OSError, ValueError, AttributeError) as e:
        got = None
        fails.append(f"MESA-SOURCE.json: {type(e).__name__}")
    lines.append(f"INFO MESA-SOURCE.json commit {got}; build/prepare_zink.py pins {pin.group(1) if pin else None}")
    if not pin:
        fails.append("build/prepare_zink.py names no MESA_COMMIT")
    elif got is not None and got != pin.group(1):
        fails.append(f"MESA-SOURCE.json names {got}, not the pinned {pin.group(1)}")
    return _verdict_of(fails, f"Mesa {got} as pinned", lines)


READ_FUNCS = {f.__name__: f for f in (install_identity, install_zink_bundle, install_minecraft, install_zink_files,
                                      install_mesa_source)}


# ------------------------------------------------------------------------------------------------------- the run
class Run:
    def __init__(self, args):
        self.args = args
        self.t0 = time.monotonic()
        self.start = time.time()
        self.home = str(Path.home())
        try:
            self.user = pwd.getpwuid(os.getuid()).pw_name
        except KeyError:
            self.user = os.environ.get("USER", "")
        self.host = os.uname().nodename
        self.base = Path(os.path.realpath(os.path.join(self.home, "nullmoth-claude")))
        self.src = Path(SRC)
        self.b = self.base / "build2"
        self.pkgroot = self.b / "package" / "pkgroot"
        self.toolchain = self.b / "tools" / "toolchain" / "bin"
        self.vkinc = next((str(p) for p in (self.b / "out" / "nvk-build" / "mesa" / "include",
                                            self.b / "build" / "gpu-src" / "mesa" / "include") if p.is_dir()), None)
        self.expect = args.expect
        self.facts = {}
        self.scrub = Scrub(self.home, self.user, self.host)
        self.out = None
        self.rows = []
        self.stopped = None       # why every later GPU check is not run
        self.deadline_hit = False
        self.build_stuck = False
        self.caffeinate = None
        # crash reports are matched by program name; translator_readback is the child the readback script builds
        self.programs = {"nvmtltest", "translator_readback", "python3", "Python"}
        self.gui_ok, self.gui_why = None, "decided by the device probe"

    # -- paths and environments
    def set_out(self, out):
        self.out = Path(out)
        self.bin, self.logs, self.tmp, self.cache = (self.out / n for n in ("bin", "logs", "tmp", "cache"))

    def values(self):
        o = self.out if self.out is not None else Path("<out>")
        return {"SRC": str(self.src), "OUT": str(o), "BIN": str(o / "bin"), "LOGS": str(o / "logs"), "TMP": str(o / "tmp"),
                "CACHE": str(o / "cache"), "B": str(self.b), "TOOLCHAIN": str(self.toolchain), "VKINC": self.vkinc,
                "P": f"{GB}/NVMTLDriver.bundle/Contents/MacOS/NVMTLDriver", "X": f"{GB}/nvmtl/libnvmtl_translate.dylib",
                "GB": GB}

    def subst(self, s):
        vals = self.values()

        def one(m):
            v = vals.get(m.group(1))
            if v is None:
                raise Missing(m.group(1).lower())
            return v
        return PLACEHOLDER.sub(one, s)

    def plain_env(self, cid=None):
        env = {"HOME": self.home, "USER": self.user, "LOGNAME": self.user, "LANG": "en_US.UTF-8",
               "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONDONTWRITEBYTECODE": "1"}
        # TMPDIR in the run folder for every child, the runner's own reads included: Apple's /usr/bin/git is a shim
        # that goes through xcrun, which keeps its cache of tool paths in TMPDIR, else in the user temporary folder.
        # --plan makes no run folder, so its reads go without.
        if cid and self.out is not None:
            env["TMPDIR"] = str(self.tmp / cid) + "/"
        elif self.out is not None and (self.tmp / "runner").is_dir():
            env["TMPDIR"] = str(self.tmp / "runner") + "/"
        return env

    def caches_env(self):
        return {"MESA_SHADER_CACHE_DIR": str(self.cache / "mesa"), "XDG_CACHE_HOME": str(self.cache / "xdg")}

    def run_env(self, cid, nn, check_env):
        env = self.plain_env(cid)
        env.update(NVMTL_PROCESS_ENV="1", NVMTL_LOG_PATH=str(self.logs / f"{nn}-{cid}.nvmtl.log"),
                   NVMTL_AIRCACHE=str(self.cache / "air" / "aircache"), NVMTL_NO_FAILED_AIR="1", NVMTL_NO_FPS="1")
        env.update(self.caches_env())
        env.update({k: self.subst(v) for k, v in check_env.items()})
        return env

    def build_env(self, cid):
        env = self.plain_env(cid)
        env.update(self.caches_env())
        env["CLANG_MODULE_CACHE_PATH"] = str(self.cache / "clang")
        return env

    def git_env(self):
        env = self.plain_env()
        env.update(GIT_OPTIONAL_LOCKS="0", GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
        return env

    # -- output
    def say(self, line=""):
        for l in self.scrub(str(line)).splitlines() or [""]:
            print(l[:LINE_CUT], flush=True)

    def write(self, path, text):
        with open(path, "w", encoding="utf-8", errors="surrogateescape") as f:
            f.write(self.scrub(text))

    def elapsed(self):
        return time.monotonic() - self.t0

    # -- gates
    def gate_out(self, create):
        if not self.base.is_dir():
            raise Refused(f"{self.base} does not exist: the run's folder must lie inside it")
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out = Path(os.path.realpath(os.path.abspath(self.args.out))) if self.args.out else self.base / "cardtest" / stamp
        if not inside(out, self.base) or os.path.realpath(str(out)) == str(self.base):
            raise Refused(f"--out {out} is not inside {self.base}")
        for keep in (self.src, self.b, self.base / "src", self.base / "nvmetrics-work", self.base / "exp-loader"):
            if inside(out, keep):
                raise Refused(f"--out {out} lies inside {keep}, which this run never writes")
        if out.exists() and (not out.is_dir() or any(out.iterdir())):
            raise Refused(f"--out {out} exists and is not an empty folder")
        free = shutil.disk_usage(str(self.base)).free
        self.facts["free"] = free
        if free < MIN_FREE:
            raise Refused(f"{self.base} has {free // 1024 ** 2} MB free, less than {MIN_FREE // 1024 ** 2} MB")
        self.set_out(out)
        if create:
            for d in (self.bin, self.logs, self.tmp, self.cache / "air" / "aircache", self.cache / "mesa", self.cache / "xdg",
                      self.cache / "clang", self.tmp / "runner"):
                d.mkdir(parents=True, exist_ok=True)
            (self.out / "start").write_text("")
            self.start = os.stat(self.out / "start").st_mtime

    def gate_checkout(self):
        env = self.git_env()
        git = ["git", "-c", "core.fsmonitor=false", "-C", str(self.src)]
        rc, out, err = quick(git + ["status", "--porcelain"], env)
        if rc != 0:
            raise Refused(f"git status failed in the checkout: {(err.strip().splitlines() or ['no answer'])[-1]}")
        if out.strip():
            raise Refused("the checkout is not clean: " + "; ".join(out.strip().splitlines()[:3]))
        rc, out, err = quick(git + ["rev-parse", "HEAD"], env)
        self.facts["head"] = out.strip() if rc == 0 else "unknown"
        rc, out, err = quick(git + ["diff", "--quiet", "--no-ext-diff", "--no-textconv", self.expect, "HEAD", "--"]
                             + list(DRIVER_PATHS), env)
        if rc == 1:
            raise Refused(f"the driver sources of HEAD ({', '.join(DRIVER_PATHS)}) differ from {self.expect}")
        if rc != 0:
            raise Refused(f"git diff against {self.expect} failed: {(err.strip().splitlines() or ['no answer'])[-1]}")

    def gate_package(self):
        info = self.pkgroot / "NULLMOTH-LOCAL-BUILD.txt"
        try:
            m = re.search(r"^commit:\s*([0-9a-f]{7,40})\b", info.read_text(errors="replace"), re.M)
        except OSError:
            raise Refused(f"{info} cannot be read: the staged package of {self.expect} is not there") from None
        if not m:
            raise Refused(f"{info} has no commit: line")
        self.facts["package_commit"] = m.group(1)
        if not m.group(1).startswith(self.expect.lower()):
            raise Refused(f"the staged package was built from {m.group(1)}, not {self.expect}")
        try:
            lines = sums_lines((self.pkgroot / "SHA256SUMS").read_text(errors="replace"))
        except OSError:
            raise Refused(f"{self.pkgroot / 'SHA256SUMS'} cannot be read") from None
        equal, differ = 0, []
        for sha, rel in lines:
            if not rel.startswith("Library/GPUBundles/") or rel == "Library/GPUBundles/nvmtl/nvrm610.conf":
                continue
            installed = os.path.join(GB, rel[len("Library/GPUBundles/"):])
            try:
                got = sha256_file(installed)
            except OSError:
                differ.append(f"{installed} (missing)")
                continue
            if got == sha:
                equal += 1
            else:
                differ.append(installed)
        self.facts["sums_equal"], self.facts["sums_differ"] = equal, differ
        if differ:
            raise Refused(f"{len(differ)} of {equal + len(differ)} files under {GB} differ from the staged package: "
                          + ", ".join(differ[:5]))
        if not equal:
            raise Refused("the staged package's SHA256SUMS lists no file under Library/GPUBundles")
        for d in ("NVKZinkGLDriver.bundle", "nvmtl/zink", "nvmtl/minecraft"):
            if not os.path.isdir(os.path.join(GB, d)):
                raise Refused(f"{GB}/{d} does not exist: the installed package has no native OpenGL or Vulkan")

    def gate_accel(self):
        rc, out, err = quick(["ioreg", "-r", "-c", "NVAccel", "-l", "-d", "1", "-w0"], self.plain_env())
        if rc != 0 or "NVAccel" not in out:
            raise Refused("the IORegistry has no NVAccel: the driver's accelerator is not loaded")
        m = re.search(r'"IOGLBundleName"\s*=\s*"([^"]*)"', out)
        self.facts["gl_bundle"] = m.group(1) if m else "none"

    def record_facts(self):
        env = self.plain_env()
        for key, argv in (("macos", ["sw_vers", "-productVersion"]), ("macos_build", ["sw_vers", "-buildVersion"]),
                          ("cpu", ["sysctl", "-n", "machdep.cpu.brand_string"]),
                          ("switches_nondefault", ["sysctl", "-n", "debug.nvrm_switches_nondefault"])):
            rc, out, _ = quick(argv, env)
            self.facts[key] = out.strip() if rc == 0 else "unknown"
        for key in ("DARWIN_USER_TEMP_DIR", "DARWIN_USER_CACHE_DIR"):
            rc, out, _ = quick(["getconf", key], env)
            self.facts[key] = out.strip() if rc == 0 and out.strip().startswith("/") else ""
        self.scrub = Scrub(self.home, self.user, self.host, self.facts["DARWIN_USER_TEMP_DIR"], self.facts["DARWIN_USER_CACHE_DIR"])
        try:
            conf = Path(GB, "nvmtl", "nvrm610.conf").read_text(errors="replace").splitlines()
            self.facts["knobs"] = [l.strip() for l in conf if re.match(r"^\s*(NVMTL|NVK|NVRM)_[A-Z0-9_]+\s*=", l)]
        except OSError:
            self.facts["knobs"] = ["(nvrm610.conf cannot be read)"]
        try:
            allow = Path(GB, "nvmtl-allow.txt").read_text(errors="replace").splitlines()
            self.facts["allow"] = [l.strip() for l in allow if l.strip() and not l.strip().startswith("#")]
        except OSError:
            self.facts["allow"] = ["(nvmtl-allow.txt cannot be read)"]

    def counters(self):
        env = self.plain_env()
        vals = {}
        for n in COUNTERS:
            rc, out, _ = quick(["sysctl", "-n", n], env)
            v = out.strip()
            vals[n] = (int(v) if re.match(r"^-?\d+$", v) else v) if rc == 0 and v else None
        rc, out, _ = quick(["pgrep", "-x", "WindowServer"], env)
        ws = sorted(int(x) for x in out.split() if x.isdigit()) if rc in (0, 1) else None
        return vals, ws

    # -- needs
    def missing(self, c):
        """-> (result word, why) when a row cannot run here, else None"""
        for s in c["sources"]:
            if not (self.src / s).exists():
                return "skip", f"not in this checkout: {s}"
        if self.args.no_window and c["window"]:
            return "skip", "left out with --no-window"
        needs = list(c["needs"])
        if c["window"] and "gui" not in needs:
            needs.append("gui")
        strings = list(c.get("argv", [])) + [a for st in c["build"] for a in st["argv"]] + list(c["env"].values())
        if any("{VKINC}" in s for s in strings) and "vkinc" not in needs:
            needs.append("vkinc")
        for n in needs:
            if n == "gui" and not self.gui_ok:
                return "skip", f"needs gui: {self.gui_why}"
            if n == "zink" and self.facts.get("gl_bundle") != "NVKZinkGLDriver":
                return "inactive", f"needs zink: IOGLBundleName is {self.facts.get('gl_bundle', 'not read')}"
            if n == "vkinc" and not self.vkinc:
                return "skip", f"needs vkinc: no Vulkan headers in {self.b}/out/nvk-build/mesa/include or {self.b}/build/gpu-src/mesa/include"
            if n == "spirv":
                lack = [t for t in ("spirv-val", "spirv-dis") if not (self.toolchain / t).is_file()]
                if lack:
                    return "skip", f"needs spirv: {', '.join(lack)} not in {self.toolchain}"
            if n.startswith("file:"):
                try:
                    p = self.subst(n[5:])
                except Missing as e:
                    return "skip", f"needs {e}"
                if not os.path.exists(p):
                    return "skip", f"needs {p}"
        return None

    # -- one row
    def new_row(self, n, c):
        return {"n": n, "nn": f"{n:02d}", "id": c["id"], "part": c["part"], "kind": c["kind"], "order": c["order"],
                "title": c["title"], "result": None, "why": "", "rc": None, "signal": None, "seconds": 0.0, "xid_delta": None,
                "detail": "", "log": None, "info": [], "check": c}

    def decide(self, row, result, why="", detail=""):
        row["result"], row["why"] = result, why
        row["detail"] = detail or why

    def build(self, row):
        c = row["check"]
        cid, nn = c["id"], row["nn"]
        (self.bin / cid).mkdir(parents=True, exist_ok=True)
        (self.tmp / cid).mkdir(parents=True, exist_ok=True)
        text = []
        for i, step in enumerate(c["build"]):
            if self.elapsed() > self.args.deadline:
                return self.decide(row, "not run", f"the run's deadline of {self.args.deadline} s")
            argv = [self.subst(a) for a in step["argv"]]
            limit = self.cap(step["limit"])
            raw = self.logs / f"{nn}-{cid}.build{i + 1}.out"
            res = run_limited(argv, self.build_env(cid), str(self.tmp / cid), raw, None, limit)
            out = tail_of(raw)
            try:
                raw.unlink()
            except OSError:
                pass
            text.append(f"step {i + 1}: {shlex.join(argv)}\nlimit {limit} s, {res['seconds']:.1f} s, "
                        + (f"could not start: {res['start_error']}" if res["start_error"] else
                           "STUCK" if res["stuck"] else "stopped at the limit" if res["killed"] else f"exit {res['rc']}")
                        + "\n" + out + ("" if out.endswith("\n") or not out else "\n"))
            if res["stuck"]:
                self.build_stuck = True
                self.stop(f"the build of {cid} could not be stopped")
                row["log"] = f"logs/{nn}-{cid}.build.log"
                self.write(self.logs / f"{nn}-{cid}.build.log", "".join(text))
                return self.decide(row, "STUCK", "its build could not be stopped")
            if res["start_error"] or res["killed"] or res["rc"] != 0:
                first = next((l.strip() for l in out.splitlines() if "error:" in l), "")
                why = (f"could not start: {res['start_error']}" if res["start_error"] else
                       f"no result within {limit} s" if res["killed"] else first or f"exit {res['rc']}")
                row["log"] = f"logs/{nn}-{cid}.build.log"
                self.write(self.logs / f"{nn}-{cid}.build.log", "".join(text))
                return self.decide(row, "build failed", why)
        if c["build"]:
            self.write(self.logs / f"{nn}-{cid}.build.log", "".join(text))

    def cap(self, limit):
        return min(limit, self.args.max_seconds) if self.args.max_seconds else limit

    def execute(self, row):
        """runs a row that is not decided yet: the reads the runner makes, or the program with its limits"""
        c = row["check"]
        cid, nn = c["id"], row["nn"]
        log = self.logs / f"{nn}-{cid}.log"
        row["log"] = f"logs/{nn}-{cid}.log"
        row["ran"] = True
        if c["window"]:
            self.start_caffeinate()
        before = self.counters() if c["kind"] == "gpu" else None
        t_start = time.time()
        if "func" in c:
            try:
                result, why, lines = READ_FUNCS[c["func"]](self, c)
            except Exception as e:   # noqa: BLE001  (a read that fails is that row's failure, with its reason)
                result, why, lines = "FAIL", f"the runner's read raised {type(e).__name__}: {e}", []
            row["seconds"] = time.time() - t_start
            out_text, err_text, nvlog = "\n".join(lines) + "\n", "", ""
            self.decide(row, result, why)
            argv_text, env_add, limit, exit_text, rule = f"(the runner's read {c['func']})", {}, None, "-", why
        else:
            argv = [self.subst(a) for a in c["argv"]]
            self.programs.add(os.path.basename(argv[0]))
            env = self.run_env(cid, nn, c["env"])
            env_add = {k: v for k, v in env.items() if k not in BASE_ENV_NAMES}
            limit = self.cap(c["limit"])
            (self.tmp / cid).mkdir(parents=True, exist_ok=True)
            o, e, nv = (self.logs / f"{nn}-{cid}.{x}" for x in ("out", "err", "nvmtl.log"))
            res = run_limited(argv, env, str(self.tmp / cid), o, e, limit)
            row["seconds"] = res["seconds"]
            out_text, err_text, nvlog = tail_of(o), tail_of(e), tail_of(nv)
            for p in (o, e, nv):
                try:
                    p.unlink()
                except OSError:
                    pass
            result, why, detail, rule, exit_text = self.verdict(c, res, out_text, err_text, limit)
            row["rc"] = res["rc"]
            if res["rc"] is not None and res["rc"] < 0:
                row["signal"] = signame(-res["rc"])
            self.decide(row, result, why, detail)
            argv_text = shlex.join(argv)
            if res["stuck"]:
                group = group_lines(res["pid"])
                shown = "(ps gave no answer)" if group is None else "\n".join(group) or "(ps showed none)"
                err_text += "\n--- ps of its process group ---\n" + shown + "\n"
                self.stop(f"{cid} could not be stopped (STUCK): the GPU may be held until a restart")
            elif result == "TIMEOUT" and c["kind"] == "gpu":
                self.stop(f"{cid} timed out: the GPU may be hung")
        for line in out_text.splitlines():
            if any(re.search(rx, line) for rx in c["info"]):
                row["info"].append(line.strip()[:200])
        after = self.counters() if c["kind"] == "gpu" else None
        if before and after:
            self.watch(row, before, after)
        t_end = time.time()
        counters = "" if before is None else (
            "counters before " + ", ".join(f"{k}={v}" for k, v in before[0].items()) + f"; WindowServer {before[1]}\n"
            "counters after  " + ", ".join(f"{k}={v}" for k, v in after[0].items()) + f"; WindowServer {after[1]}\n")
        if limit is None:
            limit_text = "none (a read the runner makes)"
        else:
            limit_text = f"{limit} s (outer)"
            if self.args.max_seconds:
                limit_text += f", capped by --max-seconds {self.args.max_seconds}"
        head = [f"check    {cid} ({c['part']}, {c['kind']}, order {c['order']}): {c['title']}",
                f"file     {c['file']}", f"argv     {argv_text}", f"cwd      {self.tmp / cid}"]
        head += [f"env      {k}={v}" for k, v in env_add.items()]
        head += [f"limit    {limit_text}", f"start    {utc(t_start)}", f"end      {utc(t_end)}",
                 f"seconds  {row['seconds']:.1f}", f"exit     {exit_text}", f"verdict  {self.shown(row)}", f"rule     {rule}"]
        self.write(log, "\n".join(head) + "\n" + counters + f"--- stdout ---\n{out_text}\n--- stderr ---\n{err_text}\n"
                   f"--- nvmtl.log ---\n{nvlog}\n")

    def verdict(self, c, res, out, err, limit):
        """-> (result, why, detail, rule, exit text) of a program's run"""
        rc = res["rc"]
        detail = last_line(out) or last_line(err)
        if res["start_error"]:
            return "FAIL", "could not start", res["start_error"], "the program could not be started", "not started"
        if res["stuck"]:
            return ("STUCK", f"alive {KILL_POLL} s after SIGKILL", detail, "its process group was killed (at the limit, or "
                    f"for what the program left running when it ended) and did not end within {KILL_POLL} s",
                    "still running" if res["rc"] is None else f"rc {res['rc']}, a process of its group still running")
        if res["killed"]:
            return "TIMEOUT", f"no result within {limit} s", detail, "stopped at the outer limit", "killed at the limit"
        exit_text = f"signal {signame(-rc)}" if rc < 0 else f"rc {rc}"
        if rc < 0:
            if -rc == signal.SIGALRM:
                return "TIMEOUT", "its own alarm (SIGALRM)", detail, "the program's own time limit ended it", exit_text
            return "CRASH", signame(-rc), detail, f"ended by {signame(-rc)}", exit_text
        if rc == 70:
            # rows that merge stderr into stdout carry the handler's line on stdout
            m = re.search(r"^CRASH (\S+)", err, re.M) or re.search(r"^CRASH (\S+)", out, re.M)
            if m:
                return "CRASH", m.group(1), detail, f"its crash handler caught {m.group(1)} (exit 70)", exit_text
        e = c["expect"]
        if rc == e["rc"]:
            for rx in e["stdout"]:
                if not re.search(rx, out, re.M):
                    return "FAIL", f"stdout lacks /{rx}/", detail, f"exit {rc} as expected, but stdout lacks /{rx}/", exit_text
            for rx in e["stderr_forbid"]:
                m = re.search(rx, err, re.M)
                if m:
                    line = err[err.rfind("\n", 0, m.start()) + 1:].split("\n", 1)[0].strip()
                    return "FAIL", f"stderr has {line[:80]!r}", detail, f"stderr matches /{rx}/", exit_text
            return "pass", "", detail, f"exit {rc}, every stdout pattern found, no forbidden stderr pattern", exit_text
        word = c["rc_map"].get(rc)
        if word and word != "fail":
            first = (out.strip().splitlines() or [""])[0]
            m = re.match(r"^(?:SKIP|INACTIVE|FALLBACK):\s*(.*)$", first)
            why = m.group(1) if m else detail
            return word, why, why, f"exit {rc} means {word} for this row", exit_text
        return "FAIL", f"rc {rc}", detail, f"exit {rc}, not the expected {e['rc']}", exit_text

    def watch(self, row, before, after):
        """the driver's fault counters around a GPU check. A rise of the Xid count, of the channels NVK rebuilt or of
        the submissions a rebuild dropped fails the check (a rebuilt channel can leave right-looking pixels); a rise of
        the stalled channels NVK rang again, and a copy-engine clear that left its on state (3.0.1), are noted in its
        reason; a give-up, a change of the fence state, a new WindowServer, copy-engine work whose end was not seen
        (3.0.1) and a standby of the GPU stop every later GPU check."""
        (b, _), (a, aws) = before, after

        def rise(name):
            v0, v1 = b.get(name), a.get(name)
            return v1 - v0 if isinstance(v0, int) and isinstance(v1, int) and v1 > v0 else 0
        x0, x1 = b.get("debug.nvrm_xid_count"), a.get("debug.nvrm_xid_count")
        if isinstance(x0, int) and isinstance(x1, int):
            row["xid_delta"] = x1 - x0
        faults = [f"{word} +{n}" for word, n in (("Xid", rise("debug.nvrm_xid_count")),
                                                 ("channel rebuilt", rise("debug.nvrm_chan_recovered")),
                                                 ("dropped", rise("debug.nvrm_chan_dropped"))) if n]
        notes = []
        if rise("debug.nvrm_chan_kicks"):
            notes.append(f"kicks +{rise('debug.nvrm_chan_kicks')}")
            self.facts["kicked"] = self.facts.get("kicked", 0) + 1
        if faults and rise("debug.nvrmfb_vram_ce_cleared"):
            notes.append(f"copy-engine clears +{rise('debug.nvrmfb_vram_ce_cleared')} during it")
        c0, c1 = b.get("debug.nvrmfb_vram_ce_clear"), a.get("debug.nvrmfb_vram_ce_clear")
        if c0 == 1 and isinstance(c1, int) and c1 != 1:
            notes.append(f"copy-engine clear went from 1 to {c1}")
            self.facts.setdefault("ce_clear_left", f"debug.nvrmfb_vram_ce_clear went from 1 to {c1} during {row['id']}")
        if faults:
            if row["result"] in ("pass", "skip", "inactive", "fallback"):
                row["result"], row["why"] = "FAIL", "; ".join(faults)
            else:
                row["why"] = (row["why"] + "; " + "; ".join(faults)).lstrip("; ")
        if notes:
            row["why"] = (row["why"] + "; " + "; ".join(notes)).lstrip("; ")
        g0, g1 = b.get("debug.nvrm_chan_gave_up"), a.get("debug.nvrm_chan_gave_up")
        if isinstance(g0, int) and isinstance(g1, int) and g1 > g0:
            self.stop(f"debug.nvrm_chan_gave_up rose by {g1 - g0} during {row['id']}")
        if rise("debug.nvrmfb_vram_ce_kept"):
            self.stop(f"debug.nvrmfb_vram_ce_kept rose by {rise('debug.nvrmfb_vram_ce_kept')} during {row['id']}: the "
                      f"copy engine's work on a new video memory block was not seen to end")
        if rise("debug.nvrmfb_vram_power_events"):
            self.stop(f"debug.nvrmfb_vram_power_events rose by {rise('debug.nvrmfb_vram_power_events')} during "
                      f"{row['id']}: the GPU went through a standby")
        f1 = a.get("debug.nvrm_fence_state")
        if self.first_fence is not None and f1 is not None and f1 != self.first_fence:
            self.stop(f"debug.nvrm_fence_state changed from {self.first_fence} to {f1} during {row['id']}")
        if self.first_ws is not None and aws is not None and aws != self.first_ws:
            self.stop(f"WindowServer's pid changed from {self.first_ws} to {aws} during {row['id']}")

    def stop(self, why):
        if not self.stopped:
            self.stopped = why
            self.say(f"stop: {why}; every later GPU check is not run")

    def shown(self, row):
        return row["result"] + (f" ({row['why']})" if row["why"] else "")

    # -- caffeinate, only while windowed checks run
    def start_caffeinate(self):
        if self.caffeinate is None:
            try:
                self.caffeinate = spawn(["caffeinate", "-d", "-i", "-w", str(os.getpid())], self.plain_env(), "/",
                                        subprocess.DEVNULL, subprocess.DEVNULL)
            except OSError as e:
                self.caffeinate = False
                self.say(f"note: caffeinate could not start: {e}")

    def stop_caffeinate(self):
        p = self.caffeinate
        if p:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        self.caffeinate = None if p is None else False

    # -- the device probe (the last gate)
    def device_probe(self):
        src = self.src / "tools" / "card" / "device_probe.m"
        if not src.is_file():
            raise Refused("tools/card/device_probe.m is not in this checkout")
        cid = "device-probe"
        (self.bin / cid).mkdir(parents=True, exist_ok=True)
        (self.tmp / cid).mkdir(parents=True, exist_ok=True)
        exe = self.bin / cid / "nvmtltest"
        argv = ["/usr/bin/xcrun", "clang", "-arch", "x86_64", "-mmacosx-version-min=15.0", "-fobjc-arc", "-O1", str(src),
                "-framework", "Foundation", "-framework", "Metal", "-framework", "CoreGraphics", "-o", str(exe)]
        raw = self.logs / "00-device-probe.build.out"
        res = run_limited(argv, self.build_env(cid), str(self.tmp / cid), raw, None, self.cap(PROBE_BUILD_LIMIT))
        out = tail_of(raw)
        try:
            raw.unlink()
        except OSError:
            pass
        self.write(self.logs / "00-device-probe.build.log", f"{shlex.join(argv)}\n{out}")
        if res["stuck"]:
            self.stop("the device probe's build could not be stopped")
            return False
        if res["rc"] != 0:
            first = next((l.strip() for l in out.splitlines() if "error:" in l), res["start_error"] or f"exit {res['rc']}")
            raise Refused(f"the device probe did not build: {first}")
        before = self.counters()
        self.first_fence = before[0].get("debug.nvrm_fence_state")
        self.first_ws = before[1]
        self.facts["counters_start"] = before
        env = self.run_env(cid, "00", {})
        o, e, nv = (self.logs / f"00-device-probe.{x}" for x in ("out", "err", "nvmtl.log"))
        res = run_limited([str(exe)], env, str(self.tmp / cid), o, e, self.cap(PROBE_LIMIT))
        out, err, nvlog = tail_of(o), tail_of(e), tail_of(nv)
        for p in (o, e, nv):
            try:
                p.unlink()
            except OSError:
                pass
        exit_text = ("STUCK" if res["stuck"] else "killed at the limit" if res["killed"] else
                     f"could not start: {res['start_error']}" if res["start_error"] else
                     f"signal {signame(-res['rc'])}" if res["rc"] < 0 else f"rc {res['rc']}")
        self.write(self.logs / "00-device-probe.log", f"argv     {exe}\nlimit    {self.cap(PROBE_LIMIT)} s\n"
                   f"seconds  {res['seconds']:.1f}\nexit     {exit_text}\n"
                   f"--- stdout ---\n{out}\n--- stderr ---\n{err}\n--- nvmtl.log ---\n{nvlog}\n")
        if res["stuck"] or res["killed"]:
            self.stop("the device probe " + ("could not be stopped" if res["stuck"] else f"gave no answer within {PROBE_LIMIT} s"))
            return False
        m = re.search(r"^DEVICE class=(\S+) name=(.*)$", out, re.M)
        self.facts["device_class"], self.facts["device_name"] = (m.group(1), m.group(2).strip()) if m else ("none", "")
        self.facts["all_devices"] = re.findall(r"^ALLDEVICE \d+ (.*)$", out, re.M)
        g = re.search(r"^GUI session=(-?\d) console=(-?\d) logindone=(-?\d) locked=(-?\d) asleep=(-?\d)$", out, re.M)
        if not g:
            self.gui_ok, self.gui_why = False, "the device probe printed no GUI line"
        else:
            session, console, _, locked, asleep = (int(x) for x in g.groups())
            self.facts["gui"] = g.group(0)[4:]
            why = ("no GUI session (CGSessionCopyCurrentDictionary is NULL)" if session != 1 else
                   "the session is not on the console" if console != 1 else "the screen is locked" if locked == 1 else
                   "the main display is asleep" if asleep == 1 else "")
            self.gui_ok, self.gui_why = not why, why or "a console GUI session, unlocked, display awake"
        self.facts["lock_keys"] = re.findall(r"^GUI lockkeys=(.*)$", out, re.M)
        if res["rc"] != 0 or self.facts["device_class"] != "NVMTLDevice" or " (NVMTL over " not in self.facts["device_name"]:
            raise Refused(f"the device probe sees no device of this driver: class {self.facts['device_class']}, name "
                          f"{self.facts['device_name']!r}, exit {res['rc']} ({last_line(out) or last_line(err)})")
        after = self.counters()
        fake = {"id": "device-probe", "result": "pass", "why": "", "xid_delta": None}
        self.watch(fake, before, after)
        if fake["result"] == "FAIL":
            self.stop(f"debug.nvrm_xid_count rose by {fake['xid_delta']} during the device probe" if fake["xid_delta"]
                      else f"{fake['why']} during the device probe")
        return not self.stopped

    # -- the end
    def audit(self):
        """names only: what appeared outside the run folder while it ran"""
        found = []
        start = self.start

        def newer(p):
            try:
                return os.lstat(p).st_mtime >= start - 1
            except OSError:
                return False
        for d in AUDIT_TMP_DIRS:
            try:
                found += [f"{d}/{n}" for n in sorted(os.listdir(d)) if n.startswith("nvmtl") and newer(os.path.join(d, n))]
            except OSError:
                pass

        def count_new(root, depth=3):
            n = 0
            for dirpath, dirnames, filenames in os.walk(root):
                if dirpath[len(str(root)):].count(os.sep) >= depth:
                    dirnames[:] = []
                n += sum(1 for f in filenames + dirnames if newer(os.path.join(dirpath, f)))
            return n
        home = Path(self.home)
        for root in [home / "Library" / "Caches" / "nvmtl"] + sorted((home / ".cache").glob("mesa_shader_cache*")):
            if root.exists():
                n = count_new(root)
                if n:
                    found.append(f"{root}: {n} entries newer than the start")
        reports = home / "Library" / "Logs" / "DiagnosticReports"
        try:
            found += [f"{reports}/{n}" for n in sorted(os.listdir(reports))
                      if any(re.match(re.escape(p) + r"[-_]\d{4}", n) for p in self.programs) and newer(reports / n)]
        except OSError:
            pass
        uc = self.facts.get("DARWIN_USER_CACHE_DIR")
        if uc:
            try:
                found += [f"{uc}/{n}" for n in sorted(os.listdir(uc)) if n.startswith("nvmtl") and newer(os.path.join(uc, n))]
            except OSError:
                pass
            for p in sorted(self.programs):
                q = os.path.join(uc, "com.apple.metal", p)
                if os.path.exists(q) and newer(q):
                    found.append(q)
        return found

    def exit_code(self):
        if self.stopped or self.deadline_hit:
            return 4
        return 1 if any(r["result"] in FAILED for r in self.rows) else 0

    def report(self, code, refused=None):
        end = time.time()
        f = self.facts
        rows = self.rows
        counts = {k: sum(1 for r in rows if r["result"] == k) for k in ("pass", "skip", "inactive", "fallback", "not run")}
        failed = sum(1 for r in rows if r["result"] in FAILED)
        ran = sum(1 for r in rows if r.get("ran") or r["result"] == "build failed")
        audit = self.audit() if self.out is not None else []
        h = ["# Card checks", "",
             f"- UTC start {utc(self.start)}, end {utc(end)}, {end - self.start:.0f} s; exit {code} ({EXIT_WORDS[code]})",
             f"- macOS {f.get('macos', '?')} ({f.get('macos_build', '?')}); CPU {f.get('cpu', '?')}",
             f"- Metal: {f.get('device_class', 'not probed')} {f.get('device_name', '')}".rstrip(),
             f"- package {f.get('package_commit', '?')}: {f.get('sums_equal', 0)} files under {GB} equal the staged package "
             f"(nvrm610.conf not compared)",
             f"- IOGLBundleName {f.get('gl_bundle', 'not read')}",
             f"- checkout HEAD {f.get('head', '?')}: driver sources equal {self.expect}",
             f"- GUI: {f.get('gui', 'not probed')} ({self.gui_why})" + (f"; lock keys seen: {f['lock_keys'][0]}" if f.get("lock_keys") else ""),
             f"- debug.nvrm_switches_nondefault: {f.get('switches_nondefault', '?')}",
             "- nvrm610.conf knobs: " + ("; ".join(f.get("knobs", [])) or "none")
             + ("  (NVMTL_NO_FBFETCH is set: the A8 framebuffer-fetch library cannot compile)"
                if any(k.startswith("NVMTL_NO_FBFETCH") for k in f.get("knobs", [])) else ""),
             "- nvmtl-allow.txt: " + ("; ".join(f.get("allow", [])) or "empty") + " (nvmtltest is always granted a device)",
             f"- counters at the start: {f['counters_start'][0]}; WindowServer {f['counters_start'][1]}" if f.get("counters_start") else "- counters at the start: not read"]
        if refused:
            h.append(f"- REFUSED: {refused}")
        if self.stopped:
            h.append(f"- STOPPED: {self.stopped}")
        t = ["", "| # | part | check | result | seconds | Xid | detail |", "|---|---|---|---|---|---|---|"]
        for r in rows:
            xid = "" if r["xid_delta"] is None else f"+{r['xid_delta']}" if r["xid_delta"] else "0"
            t.append(f"| {r['nn']} | {r['part']} | {r['id']} | {self.shown(r)} | {r['seconds']:.1f} | {xid} | "
                     f"{r['detail'].replace('|', '/')[:DETAIL_CUT]} |")
        t += ["", f"{ran} ran, {failed} failed, {counts['skip']} skipped, {counts['inactive']} inactive, "
                  f"{counts['fallback']} fallback, {counts['not run']} not run"
              + (f"; {f['kicked']} with a stalled channel rung again (debug.nvrm_chan_kicks)" if f.get("kicked") else "")]
        why = [f"- {r['id']}: {self.shown(r)}" for r in rows if r["result"] in ("skip", "inactive", "fallback", "not run")]
        t += ["", "## Skipped and why", ""] + (why or ["none"])
        info = [f"- {r['id']}: {l}" for r in rows for l in r["info"]]
        if info:
            t += ["", "## Information", ""] + info
        t += ["", "## Written outside the work folder", ""] + ([f"- {a}" for a in audit] or ["nothing found"])
        self.write(self.out / "report.md", "\n".join(h + t) + "\n")
        res = [{k: r[k] for k in ("id", "part", "kind", "order", "title", "result", "why", "rc", "signal", "seconds",
                                  "xid_delta", "detail", "log", "info")} for r in rows]
        self.write(self.out / "results.json", json.dumps({"exit": code, "refused": refused, "stopped": self.stopped,
                                                           "facts": {k: v for k, v in f.items() if k != "counters_start"},
                                                           "audit": audit, "rows": res}, indent=1, default=str) + "\n")
        self.say("")
        for line in t[1:]:
            if line.startswith("## Information"):
                break
            self.say(line)
        self.say(f"report: {self.out / 'report.md'}")
        self.scrub_tree()
        self.write(self.out / "DONE", f"exit {code} at {utc(end)}\n")

    def scrub_tree(self):
        """every text file a child wrote into the run folder (outside bin, tmp and cache) passes the scrub too"""
        for dirpath, dirnames, filenames in os.walk(self.out):
            if Path(dirpath) == self.out:
                dirnames[:] = [d for d in dirnames if d not in ("bin", "tmp", "cache")]
            for n in filenames:
                p = Path(dirpath, n)
                try:
                    if p.is_symlink() or p.stat().st_size > 16 * 1024 * 1024:
                        continue
                    data = p.read_bytes()
                except OSError:
                    continue
                if b"\0" in data:
                    continue
                text = data.decode("utf-8", "surrogateescape")
                new = self.scrub(text)
                if new != text:
                    p.write_text(new, encoding="utf-8", errors="surrogateescape")


EXIT_WORDS = {0: "every check that ran passed", 1: "a check failed", 2: "usage", 3: "refused", 4: "stopped early"}


def last_line(text):
    rx = re.compile(r"^(?:PASS|FAIL|RESULT|SKIP|INACTIVE|FALLBACK|CRASH)\b.*$|^.*\berrors=.*$", re.M)
    found = [m.group(0) for m in rx.finditer(text or "")]
    if found:
        return found[-1].strip()
    lines = [l for l in (text or "").splitlines() if l.strip()]
    return lines[-1].strip() if lines else ""


def parse_args(argv):
    ap = argparse.ArgumentParser(prog="card_selftest.py", description="the card checks of the installed driver's new parts")
    ap.add_argument("--out", help="the run's folder, inside ~/nullmoth-claude, empty or absent (default cardtest/<UTC stamp>)")
    ap.add_argument("--expect", default=DEFAULT_EXPECT, help="the commit of the installed package")
    ap.add_argument("--only", help="run only the rows whose id matches this regular expression")
    ap.add_argument("--no-window", action="store_true", help="leave out every row that opens a window")
    ap.add_argument("--max-seconds", type=float, help="cap every limit at this many seconds")
    ap.add_argument("--deadline", type=float, default=DEADLINE, help=f"start nothing after this many seconds (default {DEADLINE})")
    ap.add_argument("--plan", action="store_true", help="the gates that only read, then the build and run plan; runs no GPU program")
    ap.add_argument("--list", action="store_true", help="print every row and build nothing")
    a = ap.parse_args(argv)
    if not re.match(r"^[0-9a-fA-F]{7,40}$", a.expect):
        ap.error("--expect must be a commit (7 to 40 hexadecimal digits)")
    a.expect = a.expect.lower()
    if a.only:
        try:
            a.only_rx = re.compile(a.only)
        except re.error as e:
            ap.error(f"--only: {e}")
    if a.max_seconds is not None and a.max_seconds <= 0:
        ap.error("--max-seconds must be above 0")
    if a.plan and a.list:
        ap.error("--plan and --list exclude each other")
    return a


def main(argv):
    args = parse_args(argv)
    run = Run(args)
    card = Path(CARD_DIR) if CARD_DIR else run.src / "tools" / "card"
    checks, errors = load_checks(card)
    if errors:
        for e in errors:
            run.say(f"refused: {e}")
        return 3
    if args.only:
        checks = [c for c in checks if args.only_rx.search(c["id"])]
    if args.list:
        for c in checks:
            run.say(f"{c['id']}\t{c['part']}\t{c['kind']}\t{c['order']}\t{c['title']}")
        n = len(list(card.glob("checks_*.py")))
        run.say(f"{len(checks)} rows from {n} check file{'' if n == 1 else 's'}")
        return 0
    if args.plan:
        return plan(run, checks)
    try:
        run.gate_out(create=True)
    except Refused as e:
        run.say(f"refused: {e}")
        return 3
    run.rows = [run.new_row(i + 1, c) for i, c in enumerate(checks)]
    signal.signal(signal.SIGTERM, interrupted)
    if signal.getsignal(signal.SIGHUP) is not signal.SIG_IGN:   # the end of the shell that started it, unless nohup
        signal.signal(signal.SIGHUP, interrupted)
    run.first_fence, run.first_ws = None, None
    try:
        return body(run)
    except Exception as e:   # noqa: BLE001  (a fault of the runner itself still ends what it started and writes DONE)
        return fault(run, e)
    except KeyboardInterrupt:
        for pgid in list(RUNNING):
            kill_group(pgid)
        run.stopped = run.stopped or "the runner was interrupted"
        for r in run.rows:
            if r["result"] is None:
                run.decide(r, "not run", "the runner was interrupted")
        run.stop_caffeinate()
        run.report(4)
        return 4


def interrupted(*_):
    raise KeyboardInterrupt


def fault(run, e):
    """a fault of the runner itself (not yet run on a Mac): stop its children and caffeinate, mark the rest not run,
    and write the report and DONE (exit 4), so that a session that waits for DONE does not wait for ever"""
    for pgid in list(RUNNING):
        kill_group(pgid)
    run.stop_caffeinate()
    why = f"the runner itself failed: {type(e).__name__}: {e}"
    run.say(why)
    for line in traceback.format_exc().splitlines():
        run.say(line)
    run.stopped = run.stopped or why
    for r in run.rows:
        if r["result"] is None:
            run.decide(r, "not run", why)
    try:
        run.report(4)
    except Exception:   # noqa: BLE001  (the report itself failed: DONE still says how the run ended)
        try:
            run.write(run.out / "DONE", f"exit 4 at {utc()} ({why})\n")
        except OSError:
            pass
    return 4


def body(run):
    run.record_facts()
    run.say(f"card checks: {len(run.rows)} rows; out {run.out}")
    try:
        run.gate_checkout()
        run.gate_package()
        run.gate_accel()
        run.say(f"gates: checkout clean, driver sources equal {run.expect}; package {run.facts['package_commit'][:8]}, "
                f"{run.facts['sums_equal']} files equal; IOGLBundleName {run.facts['gl_bundle']}")
        ok = run.device_probe()
    except Refused as e:
        run.say(f"refused: {e}")
        for r in run.rows:
            run.decide(r, "not run", "the run was refused")
        run.report(3, refused=str(e))
        return 3
    run.say(f"device: {run.facts.get('device_class')} {run.facts.get('device_name')}; gui: {run.gui_why}")
    if not ok:
        for r in run.rows:
            run.decide(r, "not run", run.stopped)
        run.report(4)
        return 4
    for r in run.rows:
        why = run.missing(r["check"])
        if why:
            run.decide(r, *why)
    for r in run.rows:     # every build before the first check runs: no GPU in it
        if r["result"] is None and r["check"]["build"]:
            if run.build_stuck:
                run.decide(r, "not run", run.stopped)
                continue
            run.build(r)
            if r["result"] == "not run":
                run.deadline_hit = True
    last_window = max((i for i, r in enumerate(run.rows) if r["check"]["window"] and r["result"] is None), default=-1)
    for i, r in enumerate(run.rows):
        if r["result"] is None:
            if run.elapsed() > run.args.deadline:
                run.deadline_hit = True
                run.decide(r, "not run", f"the run's deadline of {run.args.deadline} s")
            elif run.stopped and r["kind"] == "gpu":
                run.decide(r, "not run", run.stopped)
            else:
                run.execute(r)
        else:
            if not r["log"] and r["result"] == "build failed":
                r["log"] = f"logs/{r['nn']}-{r['id']}.build.log"
        if i == last_window:
            run.stop_caffeinate()
        run.say(f"{r['nn']} {r['id']}: {run.shown(r)} ({r['seconds']:.1f} s){' - ' + r['detail'] if r['detail'] and r['detail'] != r['why'] else ''}")
    run.stop_caffeinate()
    code = run.exit_code()
    run.report(code)
    return code


def plan(run, checks):
    """the gates that only read, then what a run would build and start: no GPU program, no build, no file written"""
    refused = []
    run.say("plan: no GPU program runs, nothing is built and nothing is written")
    try:
        run.gate_out(create=False)
        run.say(f"gate out folder: {run.out} (made at the start of a run); {run.facts['free'] // 1024 ** 3} GB free")
    except Refused as e:
        refused.append(str(e))
        run.say(f"refused: {e}")
    if run.out is None:
        run.set_out(run.base / "cardtest" / "<UTC stamp>")
    run.record_facts()
    for name, gate in (("checkout", run.gate_checkout), ("package", run.gate_package), ("accelerator", run.gate_accel)):
        try:
            gate()
            run.say(f"gate {name}: ok")
        except Refused as e:
            refused.append(str(e))
            run.say(f"refused: {e}")
    run.say(f"facts: macOS {run.facts.get('macos')} ({run.facts.get('macos_build')}); IOGLBundleName "
            f"{run.facts.get('gl_bundle', 'not read')}; package {run.facts.get('package_commit', '?')}; "
            f"{run.facts.get('sums_equal', 0)} files equal; HEAD {run.facts.get('head', '?')}")
    run.say(f"then the device probe: build {run.src}/tools/card/device_probe.m into {run.bin}/device-probe/nvmtltest "
            f"({PROBE_BUILD_LIMIT} s), run it ({PROBE_LIMIT} s): class NVMTLDevice, ' (NVMTL over ' in its name, GUI facts")
    for i, c in enumerate(checks):
        nn = f"{i + 1:02d}"
        run.say(f"{nn} {c['id']} ({c['part']}, {c['kind']}, order {c['order']}{', window' if c['window'] else ''}): {c['title']}")
        try:
            why = run.missing(c)
        except Missing as e:
            why = ("skip", f"needs {e}")
        if why and run.gui_ok is None and why[1].startswith("needs gui"):
            run.say("   gui: decided at run time by the device probe")
        elif why:
            run.say(f"   {why[0]}: {why[1]}")
        try:
            for st in c["build"]:
                run.say(f"   build ({run.cap(st['limit'])} s): {shlex.join(run.subst(a) for a in st['argv'])}")
            if "func" in c:
                run.say(f"   read: the runner's {c['func']}")
            else:
                env = {k: run.subst(v) for k, v in c["env"].items()}
                run.say(f"   run ({run.cap(c['limit'])} s): " + " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
                        + (" " if env else "") + shlex.join(run.subst(a) for a in c["argv"]))
        except Missing as e:
            run.say(f"   skip: needs {e}")
    run.say(f"{len(checks)} rows; " + ("refused: " + "; ".join(refused) if refused else "the gates that only read pass"))
    return 3 if refused else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
