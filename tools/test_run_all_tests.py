#!/usr/bin/env python3
"""tools/run_all_tests.py itself: every test it runs gets a temporary folder of its own as TMPDIR, deleted when the test
ends, and no Python bytecode is written, anywhere.

The runner is copied into a scratch tree whose only test makes a temporary folder with Python's tempfile and leaves it
behind, as several tests of this tree do, and imports a module of the tree. Negative controls: the same runner without
its own TMPDIR leaves the folder; without PYTHONDONTWRITEBYTECODE the module's bytecode is written where this Python
puts it (importlib's cache_from_source): under a pycache prefix (Apple's python3 always has one, in
~/Library/Caches/com.apple.python; the arms set one inside the scratch tree, so nothing reaches the tester's caches),
and, on a Python without a prefix of its own, beside the module as well. A Python that keeps its own prefix even when
PYTHONPYCACHEPREFIX names another would write that bytecode into the tester's caches: there the test imports nothing,
and the arms hold the switch that every write of bytecode obeys, sys.dont_write_bytecode, as the test sees it.

The tester's environment with the driver's settings (NVMTL_X, NVENCVT_LOG, VK_ICD_FILENAMES, ...) and the inputs a test
may take from the tester (CC, NVENCVT_HEADERS, NM_OGKM620, ZSTD, ...): the first do not reach a test, the second do.
Negative control: the runner that passes its environment on as it is. The prefixes it leaves out are the plugin's own.

A Python a test starts in isolated mode ignores that variable: every such start must also give -B (a text arm).

A test that makes a folder beside the temporary folder it was given (a bare mktemp on macOS does) has it listed at the
end of the run. Negative control: the runner without that list.

Then tests that leave a program running in the background, that pass their time limit, and that try to reach the
terminal: the runner starts each test as a session of its own, without a terminal and with nothing on its input, and
stops what it started. Negative control: the same runner with the test in the runner's own session.

A test stopped at its limit is reported with the end of what it had printed, also what a Python test printed without
flushing, and the tests that took long are named with their times. Negative controls: the runner that drops the
output, the runner that leaves Python's output buffered, the runner with its real threshold for "long".

A test that prints bytes that are not UTF-8 fails with its output and the run reaches its summary (control: the output
read strictly ends the run with Python's error). An entry a test removes and a later one makes again names the later
test (control: a runner that remembers every entry it saw). A test that runs long is named on standard error while the
run goes on (control: a runner that says nothing until the end)."""
import sys
sys.dont_write_bytecode = True   # run alone, this test writes none for its own imports either (python3 -B, from inside)
import importlib.util  # noqa: E402
import json  # noqa: E402
import os
import pathlib
import re
import shutil
import signal
import subprocess
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNNER = (ROOT / "tools/run_all_tests.py").read_text()
fails = 0


def arm(name, ok, detail=""):
    global fails
    fails += not ok
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f" ({str(detail)[-400:]})"))


LEAK = """import os, sys
flag = sys.dont_write_bytecode
sys.dont_write_bytecode = True   # the standard library's bytecode would land outside the tree, even in a control
import pathlib, tempfile
here = pathlib.Path(__file__).resolve().parents[1]
here.joinpath("flag.txt").write_text(str(flag))
sys.path.insert(0, str(pathlib.Path(__file__).parent))
if not here.joinpath("no-import").exists():
    sys.dont_write_bytecode = flag
    import helper_mod  # noqa: F401  (Python would write its bytecode for it: beside it, or under a pycache prefix)
    sys.dont_write_bytecode = True
d = tempfile.mkdtemp(prefix="leak-")
pathlib.Path(d, "x").write_text("left behind")
pathlib.Path(__file__).resolve().parents[1].joinpath("leak.txt").write_text(d)
"""
# the outer runner sets the first and the last
CLEAN = {k: v for k, v in os.environ.items() if k not in ("PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX", "PYTHONUNBUFFERED")}


def py(env, code, *args):
    return subprocess.run([sys.executable, "-B", "-c", code, *args], env=env, capture_output=True, text=True).stdout.strip()


# Apple's python3 has a pycache prefix of its own (~/Library/Caches/com.apple.python) with no variable set; then the case
# without a prefix cannot occur, and a control run without one would write into the tester's caches.
BUILTIN_PREFIX = py(CLEAN, "import sys; print(sys.pycache_prefix or '')")


def leak_run(runner_text, prefix):
    """-> (runner exit status, the folder the test made, whether it still exists, where Python writes the bytecode of the
    module the test imports ("" when that is outside the tree: the test then imports nothing), whether it is there, whether
    any bytecode was written in the tree or there, sys.dont_write_bytecode as the test saw it, output). prefix: run with
    PYTHONPYCACHEPREFIX set to a folder of the scratch tree (sys.pycache_prefix, as Apple's python3 has one), else with none."""
    with tempfile.TemporaryDirectory() as d:
        # the real path: the runner starts its tests by their real paths, and Python names the bytecode after the path a
        # module was imported by (on a Mac the temporary folder is under /var, a link to /private/var)
        root = pathlib.Path(d).resolve()
        (root / "tools").mkdir()
        (root / "tools/run_all_tests.py").write_text(runner_text)
        (root / "tools/test_leak.py").write_text(LEAK)
        (root / "tools/helper_mod.py").write_text("X = 1\n")
        env = dict(CLEAN, PYTHONPYCACHEPREFIX=str(root / "pycache-prefix")) if prefix else CLEAN
        # where this Python, in the environment the test gets, puts the bytecode of the module: the arms look there, not
        # at one folder that only some Pythons use
        target = py(env, "import importlib.util, sys; print(importlib.util.cache_from_source(sys.argv[1]))",
                    str(root / "tools/helper_mod.py"))
        if not target.startswith(str(root)):   # a control would write it there: outside the tree, into the tester's caches
            (root / "no-import").write_text(target)
            target = ""
        # -B: the runner itself writes nothing; what it starts gets only what the runner puts in its environment
        r = subprocess.run([sys.executable, "-B", str(root / "tools/run_all_tests.py")], env=env, capture_output=True,
                           text=True, timeout=120)
        made = (root / "leak.txt").read_text() if (root / "leak.txt").exists() else ""
        left = bool(made) and os.path.exists(made)
        if left:
            shutil.rmtree(made, ignore_errors=True)
        at_target = bool(target) and os.path.exists(target)
        written = at_target or any(root.rglob("*.pyc"))
        flag = (root / "flag.txt").read_text() if (root / "flag.txt").exists() else ""
        return r.returncode, made, left, target, at_target, written, flag, r.stdout + r.stderr


rc, made, left, target, at, pyc, flag, out = leak_run(RUNNER, prefix=True)
arm("a test's temporary folder is made inside one the runner gave it", rc == 0 and pathlib.Path(made).parent.name.startswith("nmtest-"),
    (rc, made, out))
arm("and is gone when the runner is done", made and not left, (made, out))
if target:
    arm("with a pycache prefix, a module the test imports leaves no bytecode, under the prefix or anywhere in the tree",
        rc == 0 and "pycache-prefix" in target and not pyc and flag == "True", (target, flag, out))
else:
    print(f"(this python3 keeps its own pycache prefix whatever PYTHONPYCACHEPREFIX says: {BUILTIN_PREFIX})")
    arm("the test runs with the writing of bytecode off, which every import obeys", rc == 0 and flag == "True" and not pyc, (flag, out))
if BUILTIN_PREFIX:
    print(f"not run: the case without a pycache prefix (this python3 always has one: {BUILTIN_PREFIX})")
else:
    rc, made, left, target, at, pyc, flag, out = leak_run(RUNNER, prefix=False)
    arm("without one, it leaves none beside the module either", rc == 0 and "__pycache__" in target and not pyc and flag == "True",
        (target, flag, out))

old_line = 'env.update(TMPDIR=tmp, PYTHONDONTWRITEBYTECODE="1")'
arm("the runner sets TMPDIR (and no bytecode) in one place", RUNNER.count(old_line) == 1)
rc, made, left, target, at, pyc, flag, out = leak_run(RUNNER.replace(old_line, 'env.update(PYTHONDONTWRITEBYTECODE="1")'), prefix=True)
arm("control: without its own TMPDIR the test's folder is left behind", made and left, (rc, made, out))
rc, made, left, target, at, pyc, flag, out = leak_run(RUNNER.replace(old_line, "env.update(TMPDIR=tmp)"), prefix=True)
if target:
    arm("control: without PYTHONDONTWRITEBYTECODE the bytecode is written under the prefix", rc == 0 and at and flag == "False", (target, flag, out))
else:
    arm("control: without PYTHONDONTWRITEBYTECODE the test runs with the writing of bytecode on", rc == 0 and flag == "False", (flag, out))
if not BUILTIN_PREFIX:
    rc, made, left, target, at, pyc, flag, out = leak_run(RUNNER.replace(old_line, "env.update(TMPDIR=tmp)"), prefix=False)
    arm("control: and without a prefix it is written beside the module", rc == 0 and at and target and flag == "False", (target, flag, out))

# isolated mode (-I, or -E) ignores PYTHONDONTWRITEBYTECODE: a Python a test starts that way writes the bytecode of its imports
# wherever that Python keeps it (on a Mac, into the tester's ~/Library/Caches), unless it is given -B as well
def isolated_without_b(text):
    found = []
    for m in re.finditer(r"\bpython3?((?:[ \t]+-[A-Za-z]+)+)|sys\.executable((?:\s*,\s*[\"']-[A-Za-z]+[\"'])+)", text):
        flags = "".join(re.findall(r"-([A-Za-z]+)", m.group(1) or m.group(2)))
        if re.search("[IE]", flags) and "B" not in flags:
            found.append(m.group(0).strip())
    return found


tests = sorted(p for p in (ROOT / "tools").glob("test_*") if p.suffix in (".py", ".sh") and p.name != "test_run_all_tests.py")  # its controls
bad = {p.name: isolated_without_b(p.read_text(errors="replace")) for p in tests}
bad = {k: v for k, v in bad.items() if v}
arm("no test starts Python in isolated mode without -B", len(tests) > 100 and not bad, bad)
arm("control: a Python started with -I alone is caught, in a script and from Python",
    isolated_without_b('python3 -I - "$F" <<PY') and isolated_without_b('subprocess.run([sys.executable, "-I", str(x)])')
    and not isolated_without_b('python3 -I -B - <<PY') and not isolated_without_b('[sys.executable, "-I", "-B", s]'))


# ---- the tester's driver settings do not reach a test; what a test takes from the tester does
ENV = """import json, os, pathlib
pathlib.Path(__file__).resolve().parents[1].joinpath("env.json").write_text(json.dumps(sorted(os.environ)))
"""
DRIVER = {"NVMTL_X": "1", "NVK_Y": "1", "NVRM_Z": "1", "NAK_DEBUG": "print", "ELFHOST_PATH": "/x", "METAL2VULKAN_DUMP": "1",
          "MESA_LOG_FILE": "/x", "NVENCVT_LOG": "1", "NVDEC_SYNC": "1", "RR_FLAGS": "1", "DLSS_TRACE": "1",
          "VK_ICD_FILENAMES": "/x", "NVK_PATH": "/x"}
# DYLD_INSERT_LIBRARIES is a driver name as well, but no program may be started with it naming a stand-in: macOS's dyld
# stops every program that is not protected (Xcode's python3 is not) before it runs. The arm below asks the runner's own
# function, in this process, instead.
TESTERS = {"NVENCVT_HEADERS": "/h", "NVDEC_HEADERS": "/d", "NM_OGKM620": "/o", "NM_RMCC_CC": "cc", "CC": "cc", "CXX": "c++",
           "ZSTD": "/z", "BUILD_SH_BASH": "/bin/bash", "NVRM_PRISTINE_OGKM": "/p", "NVMTLX_NOT_OURS": "1"}


def env_run(runner_text):
    """-> (the names the test saw, output): the runner started with the driver's settings and the tester's own inputs"""
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        (root / "tools").mkdir()
        (root / "tools/run_all_tests.py").write_text(runner_text)
        (root / "tools/switches.tsv").write_text((ROOT / "tools/switches.tsv").read_text())
        (root / "tools/test_env.py").write_text(ENV)
        r = subprocess.run([sys.executable, "-B", str(root / "tools/run_all_tests.py")], env=dict(CLEAN, **DRIVER, **TESTERS),
                           capture_output=True, text=True, timeout=120)
        seen = (root / "env.json").read_text() if (root / "env.json").exists() else "[]"
        return set(json.loads(seen)), r.stdout + r.stderr


seen, out = env_run(RUNNER)
arm("none of the driver's settings in the tester's environment reaches a test", seen and not seen & set(DRIVER), (sorted(seen & set(DRIVER)), out))
arm("what a test takes from the tester does (compilers, header folders, trees, zstd, bash), and HOME and PATH",
    set(TESTERS) | {"HOME", "PATH", "TMPDIR"} <= seen, (sorted(set(TESTERS) - seen), out))
scrub = "env = test_env(tmp, names)"
arm("the runner builds each test's environment in one place", RUNNER.count(scrub) == 1)
spec = importlib.util.spec_from_file_location("nm_runner_under_test", ROOT / "tools/run_all_tests.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)   # defines only: the runner's main() runs only as a script
names = frozenset(runner.driver_env_names())
saved = os.environ.get("DYLD_INSERT_LIBRARIES")
os.environ["DYLD_INSERT_LIBRARIES"] = "/x"   # this process is already running; only what it starts would read it
try:
    given, unscrubbed = runner.test_env("/x-tmp", names), runner.test_env("/x-tmp", frozenset())
finally:
    if saved is None:
        del os.environ["DYLD_INSERT_LIBRARIES"]
    else:
        os.environ["DYLD_INSERT_LIBRARIES"] = saved
arm("DYLD_INSERT_LIBRARIES, a driver name, is left out of a test's environment too",
    "DYLD_INSERT_LIBRARIES" in names and "DYLD_INSERT_LIBRARIES" not in given, sorted(k for k in given if k.startswith("DYLD_")))
arm("control: without the registry's names the runner would pass it on", unscrubbed.get("DYLD_INSERT_LIBRARIES") == "/x")
# ./build.sh test exports NM_VULKAN_INCLUDE for the four tests that compile against vulkan/vulkan.h; were the registry to
# list it (a build script reads it), the runner must still pass it on, or those tests skip and the run passes
os.environ["NM_VULKAN_INCLUDE"] = "/vk"
try:
    kept = runner.test_env("/x-tmp", names | {"NM_VULKAN_INCLUDE"}).get("NM_VULKAN_INCLUDE")
    saved_keep = runner.KEEP_ENV
    runner.KEEP_ENV = saved_keep - {"NM_VULKAN_INCLUDE"}
    try:
        lost = runner.test_env("/x-tmp", names | {"NM_VULKAN_INCLUDE"}).get("NM_VULKAN_INCLUDE")
    finally:
        runner.KEEP_ENV = saved_keep
finally:
    del os.environ["NM_VULKAN_INCLUDE"]
arm("NM_VULKAN_INCLUDE reaches a test even when the registry lists it", kept == "/vk", kept)
arm("control: a runner that does not keep it drops it once the registry lists it", lost is None, lost)
seen, out = env_run(RUNNER.replace(scrub, 'env = dict(os.environ, TMPDIR=tmp, PYTHONDONTWRITEBYTECODE="1")'))
arm("control: with the tester's environment as it is, the driver's settings reach the test", set(DRIVER) <= seen, (sorted(set(DRIVER) - seen), out))
knobs = (ROOT / "plugin/nvmtl_env_knobs.h").read_text()
body = knobs[knobs.index("static inline int nvmtl_env_knob_prefixed"):]
body = body[:body.index("\n}\n")]
header_prefixes = re.findall(r'strncmp\(kv, "([A-Z0-9_]+)", \d+\)', body)
runner_prefixes = re.search(r"KNOB_PREFIXES = \(([^)]*)\)", RUNNER)
runner_prefixes = re.findall(r'"([A-Z0-9_]+)"', runner_prefixes.group(1)) if runner_prefixes else []
arm("the runner leaves out exactly the prefixes the plugin forgets", len(header_prefixes) >= 10 and sorted(header_prefixes) == sorted(runner_prefixes),
    (header_prefixes, runner_prefixes))
grown = body.replace('!strncmp(kv, "NVMTL_", 6)', '!strncmp(kv, "NVMTL_", 6) || !strncmp(kv, "NVZZ_", 5)')
arm("control: a prefix the plugin adds is caught", sorted(re.findall(r'strncmp\(kv, "([A-Z0-9_]+)", \d+\)', grown)) != sorted(runner_prefixes))


# ---- what a test leaves in the system's temporary folder, outside the one the runner gave it, is listed
ESCAPE = """import os, pathlib
# as a bare mktemp on macOS does: the folder lands in the system's temporary folder, not in the TMPDIR given
pathlib.Path(os.environ["TMPDIR"]).parent.joinpath("escaped-%d" % os.getpid()).mkdir()
# and one named like the runner's own folders, which only the runner's own may hide
pathlib.Path(os.environ["TMPDIR"]).parent.joinpath("nmtest-escaped-%d" % os.getpid()).mkdir()
"""


def escape_run(runner_text):
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        (root / "tools").mkdir()
        (root / "systmp").mkdir()
        (root / "tools/run_all_tests.py").write_text(runner_text)
        (root / "tools/test_escape.py").write_text(ESCAPE)
        # two tests that leave nothing, one before and one after it (the runner takes them in the order of their names)
        (root / "tools/test_a_quiet.py").write_text("pass\n")
        (root / "tools/test_z_quiet.py").write_text("pass\n")
        r = subprocess.run([sys.executable, "-B", str(root / "tools/run_all_tests.py"), "-j", "1"], env=dict(CLEAN, TMPDIR=str(root / "systmp")),
                           capture_output=True, text=True, timeout=120)
        return r.returncode, r.stdout + r.stderr, sorted(os.listdir(root / "systmp"))


rc, out, there = escape_run(RUNNER)
# only what appeared in the scratch tree's own temporary folder: /tmp is shared, and while this test runs other programs
# (a runner beside it among them) make and remove folders there
lines = [l for l in out.splitlines() if l.startswith("left in ") and "systmp during the run" in l]
arm("a folder a test made beside its own temporary folder is listed at the end, and the run is not failed for it",
    rc == 0 and any("systmp during the run" in l and l.endswith(tuple(there)) and "escaped-" in l for l in lines), (there, out))
arm("a folder it made with a name like the runner's own is listed too", any(l.rsplit(": ", 1)[-1].startswith("nmtest-escaped-") for l in lines), out)
arm("the test's own temporary folder is not listed (it is gone)",
    not any(re.fullmatch(r"nmtest-(?!escaped-).*", l.rsplit(": ", 1)[-1]) for l in lines)
    and not any(re.fullmatch(r"nmtest-(?!escaped-).*", n) for n in there), out)
escaped = [l for l in lines if "escaped-" in l]
arm("each entry is listed with the test that was running when it was first seen, and with no other",
    len(escaped) == 2 and all("; first seen while these ran: test_escape.py): " in l for l in escaped), out)
report = ('            print(f"left in {root} during the run (by a test, or by another program meanwhile; '
          'first seen while these ran: {who}): {n}")\n')
arm("the runner lists what appeared in one place", RUNNER.count(report) == 1)
own = "        if os.path.join(root, n) not in OWN_TMPS:"
arm("the runner leaves out of the list only the folders it made itself", RUNNER.count(own) == 1)
rc, out, there = escape_run(RUNNER.replace(own, '        if not n.startswith("nmtest-"):'))
arm("control: leaving out every name like the runner's own hides the second folder",
    not any("nmtest-escaped-" in l for l in out.splitlines()) and any(n.startswith("nmtest-escaped-") for n in there), out)
rc, out, there = escape_run(RUNNER.replace(report, "            pass\n"))
arm("control: without the list the escaped folder goes unnoticed", rc == 0 and any(n.startswith("escaped-") for n in there)
    and not any("escaped-" in l for l in out.splitlines()), out)
per_test = "            note_new(p.name)\n"
arm("the runner lists the temporary folders again when a test ends, in one place", RUNNER.count(per_test) == 1)
rc, out, there = escape_run(RUNNER.replace(per_test, "            pass\n"))
arm("control: a runner that lists the folders only at the end cannot say which tests were running",
    rc == 0 and any("escaped-" in l and "first seen while these ran: none: it appeared after the last test had ended" in l for l in out.splitlines()), out)


# an entry that a test removes and a later test makes again is laid at the door of the tests running when the one that is
# left appeared, not of those that made the first
AGAIN = {"test_1_makes.py": "import os\nos.mkdir(os.path.join(os.environ['NMT_SYSTMP'], 'again'))\n",
         "test_2_removes.py": "import os\nos.rmdir(os.path.join(os.environ['NMT_SYSTMP'], 'again'))\n",
         "test_3_makes.py": "import os\nos.mkdir(os.path.join(os.environ['NMT_SYSTMP'], 'again'))\n"}


def again_run(runner_text):
    """-> the runner's line for the entry `again`, which test_1 makes, test_2 removes and test_3 makes again (-j 1)"""
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d).resolve()
        (root / "tools").mkdir()
        (root / "systmp").mkdir()
        (root / "tools/run_all_tests.py").write_text(runner_text)
        for name, source in AGAIN.items():
            (root / f"tools/{name}").write_text(source)
        r = subprocess.run([sys.executable, "-B", str(root / "tools/run_all_tests.py"), "-j", "1"],
                           env=dict(CLEAN, TMPDIR=str(root / "systmp"), NMT_SYSTMP=str(root / "systmp")), capture_output=True, text=True, timeout=120)
        return [l for l in (r.stdout + r.stderr).splitlines() if l.startswith("left in ") and "systmp during the run" in l and l.endswith(": again")]


forget = "        KNOWN.clear()\n"
arm("the runner forgets an entry that went away, in one place", RUNNER.count(forget) == 1)
got = again_run(RUNNER)
arm("an entry removed and made again names the test that made the one that is left",
    len(got) == 1 and got[0].endswith("first seen while these ran: test_3_makes.py): again"), got)
got = again_run(RUNNER.replace(forget, ""))
arm("control: a runner that remembers every entry it ever saw names the test that made the first one",
    len(got) == 1 and "first seen while these ran: test_1_makes.py): again" in got[0], got)


# ---- what a test leaves running, its time limit, and the terminal
BG = '''import os, pathlib, subprocess, sys, time
here = pathlib.Path(__file__).resolve().parents[1]
p = subprocess.Popen(["sleep", "300"])          # a child it never waits for (a daemon, a stand-in left behind)
(here / "pid.txt").write_text(str(p.pid))
if "hang" in pathlib.Path(__file__).name:
    time.sleep(300)
try:
    os.close(os.open("/dev/tty", os.O_RDWR))
    tty = "yes"
except OSError:
    tty = "no"
(here / "tty.txt").write_text(tty + " " + str(os.isatty(0)))
'''
SESSION = "text=True, start_new_session=True)"
STDIN = "stdin=subprocess.DEVNULL, stdout=subprocess.PIPE"


def alive(pid):
    """running and not a zombie (ps works the same on macOS and Linux)"""
    r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return bool(r.stdout.strip()) and not r.stdout.strip().startswith("Z")


def bg_run(runner_text, name):
    """-> (pid of the test's background child, whether it still runs, what the test saw of the terminal, output). The
    runner is started from a shell that has a terminal of its own (a pseudo-terminal as controlling terminal), as in
    Terminal.app; the shell stays while the child is looked at, so that the end of the session does not end it."""
    import fcntl, pty, termios
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        (root / "tools").mkdir()
        (root / "tools/run_all_tests.py").write_text(runner_text.replace("TIMEOUT = 600", "TIMEOUT = 5"))
        (root / f"tools/{name}").write_text(BG)
        log, done = root / "log.txt", root / "done"
        master, slave = pty.openpty()

        def ctty():
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
        sh = subprocess.Popen(["/bin/sh", "-c", '"$1" "$2" > "$3" 2>&1; : > "$4"; sleep 60', "sh", sys.executable,
                               str(root / "tools/run_all_tests.py"), str(log), str(done)],
                              stdin=slave, stdout=slave, stderr=slave, preexec_fn=ctty)
        t0 = time.time()
        while not done.exists() and time.time() - t0 < 120:
            time.sleep(0.1)
        pid = int((root / "pid.txt").read_text()) if (root / "pid.txt").exists() else 0
        time.sleep(0.5)
        running = bool(pid) and alive(pid)
        if running:
            os.kill(pid, signal.SIGKILL)
        sh.kill()
        sh.wait()
        os.close(slave)
        os.close(master)
        tty = (root / "tty.txt").read_text() if (root / "tty.txt").exists() else ""
        return pid, running, tty, log.read_text() if log.exists() else ""


arm("the runner starts each test in a session of its own with nothing on its input", RUNNER.count(SESSION) == 1 and RUNNER.count(STDIN) == 1)
loose = RUNNER.replace(SESSION, "text=True, start_new_session=False)").replace(STDIN, "stdout=subprocess.PIPE")
pid, running, tty, out = bg_run(RUNNER, "test_bg.py")
arm("a program a passing test leaves running is stopped when the test ends", pid and not running, (pid, out))
arm("a test cannot open the terminal and its input is not the terminal", tty == "no False", (tty, out))
pid, running, tty, out = bg_run(loose, "test_bg.py")
arm("control: in the runner's session the program outlives the test", pid and running, (pid, out))
arm("control: in the runner's session the test reaches the terminal", tty.startswith("yes"), (tty, out))
pid, running, tty, out = bg_run(RUNNER, "test_hang.py")
arm("a test past its time limit is stopped with what it started", pid and not running and "no result after 5 s" in out, (pid, out))
pid, running, tty, out = bg_run(loose, "test_hang.py")
arm("control: in the runner's session what it started outlives it", pid and running, (pid, out))


# ---- a per-test time limit: a named test may run past the general limit, others may not
def limit_run(runner_text, test_name, sleep_s):
    """Run a scratch test <test_name> that sleeps sleep_s then passes, under a runner whose general limit is 2 s and
    whose table gives test_build_sh.py 8 s. -> the runner's output."""
    text = runner_text.replace("TIMEOUT = 600", "TIMEOUT = 2").replace('"test_build_sh.py": 3600', '"test_build_sh.py": 8')
    assert text.count("TIMEOUT = 2\n") == 1, "the runner's general limit is not where this test changes it"
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        (root / "tools").mkdir()
        (root / "tools/run_all_tests.py").write_text(text)
        (root / f"tools/{test_name}").write_text(f"import time\ntime.sleep({sleep_s})\nprint('PASS')\n")
        r = subprocess.run([sys.executable, "-B", str(root / "tools/run_all_tests.py"), test_name],
                           env=CLEAN, capture_output=True, text=True, timeout=120)
        return r.stdout + r.stderr


arm("the runner reads a per-test limit from a table, not just the general one", "PER_TEST_TIMEOUT" in RUNNER
    and RUNNER.count("PER_TEST_TIMEOUT.get(name, TIMEOUT)") == 1 and 'timeout=timeout' in RUNNER)
out = limit_run(RUNNER, "test_build_sh.py", 4)
arm("a test in the table runs past the general limit (test_build_sh.py: 4 s under a 2 s general limit, 8 s table limit)",
    "1 passed, 0 failed" in out and "no result" not in out, out)
out = limit_run(RUNNER, "test_other.py", 4)
arm("a test not in the table is held to the general limit", "no result after 2 s" in out, out)
no_table = RUNNER.replace('PER_TEST_TIMEOUT = {"test_build_sh.py": 3600}', "PER_TEST_TIMEOUT = {}")
arm("control precondition: the table has exactly the one entry", no_table != RUNNER)
out = limit_run(no_table, "test_build_sh.py", 4)
arm("control: without its own limit test_build_sh.py is held to the general limit and stopped", "no result after 2 s" in out, out)


# ---- a test stopped at its limit: the end of what it had printed is shown, and the slow tests are named
def scratch_run(runner_text, tests, *opts, limit=2):
    """Run the scratch tests {name: source} under the runner with a general limit of 2 s (or `limit`). -> (exit status,
    output)."""
    text = runner_text.replace("TIMEOUT = 600", f"TIMEOUT = {limit}")
    assert text.count(f"TIMEOUT = {limit}\n") == 1
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        (root / "tools").mkdir()
        (root / "tools/run_all_tests.py").write_text(text)
        for name, source in tests.items():
            (root / f"tools/{name}").write_text(source)
        r = subprocess.run([sys.executable, "-B", str(root / "tools/run_all_tests.py"), *opts], env=CLEAN, capture_output=True, text=True, timeout=120)
        return r.returncode, r.stdout + r.stderr


# print() without a flush: into a pipe Python keeps it in its buffer, and a test stopped at its limit takes it along
STUCK = {"test_stuck.py": "import time\nprint('x' * 3000)\nprint('ARM-BEFORE-THE-HANG')\ntime.sleep(60)\n",
         "test_stuck.sh": "echo SHELL-BEFORE-THE-HANG\nsleep 60\n",
         "test_mute.py": "import time\ntime.sleep(60)\n"}
rc, out = scratch_run(RUNNER, STUCK)
stuck = out.split("FAIL test_stuck.py: ", 1)[-1].split("\n\n", 1)[0]
arm("a test stopped at its limit is a failure that ends with what it had printed (a Python test's unflushed lines too)",
    rc == 1 and stuck.startswith("no result after 2 s") and stuck.rstrip().endswith("ARM-BEFORE-THE-HANG") and "3 failed" in out, out[-900:])
arm("of that output only the end is shown, as for any failed test", 1000 < stuck.count("x") < 1300, stuck.count("x"))
arm("a shell test stopped at its limit shows its last line as well",
    "SHELL-BEFORE-THE-HANG" in out.split("FAIL test_stuck.sh: ", 1)[-1].split("\n\n", 1)[0], out[-900:])
arm("a test that had printed nothing says so",
    out.split("FAIL test_mute.py: ", 1)[-1].split("\n\n", 1)[0].strip() == "no result after 2 s, stopped; it had printed nothing", out[-900:])
keep = "        raise TimedOut(as_text(out) + as_text(err)) from None\n"
unbuf = '    env["PYTHONUNBUFFERED"] = "1"'
arm("the runner hands on what a stopped test had printed in one place, and starts Python tests unbuffered in one place",
    RUNNER.count(keep) == 1 and RUNNER.count(unbuf) == 1)
rc, out = scratch_run(RUNNER.replace(keep, '        raise TimedOut("") from None\n'), STUCK)
arm("control: a runner that drops the output shows nothing of the stopped tests",
    rc == 1 and "BEFORE-THE-HANG" not in out and out.count("it had printed nothing") == 3, out[-900:])
rc, out = scratch_run(RUNNER.replace(unbuf, "    pass"), STUCK)
arm("control: with Python's output left buffered the stopped Python test's lines are lost, the shell test's are not",
    rc == 1 and "ARM-BEFORE-THE-HANG" not in out and "SHELL-BEFORE-THE-HANG" in out, out[-900:])

SLOWS = {"test_quick.py": "print('PASS')\n", "test_slowish.py": "import time\ntime.sleep(1.5)\nprint('PASS')\n"}
slow_at = "SLOW = 30 "
arm("the runner names its threshold for a slow test in one place", RUNNER.count(slow_at) == 1)
rc, out = scratch_run(RUNNER.replace(slow_at, "SLOW = 1 "), SLOWS)
named = [l for l in out.splitlines() if l.startswith("took 1 s or more: ")]
arm("the tests that took long are named with their times before the last line, the others are not",
    rc == 0 and len(named) == 1 and re.fullmatch(r"took 1 s or more: test_slowish\.py [12] s", named[0])
    and out.strip().splitlines()[-1] == "2 passed, 0 failed, 0 skipped, 0 not run, of 2", out[-600:])
rc, out = scratch_run(RUNNER, SLOWS)
arm("control: under the real threshold a test of a second and a half is not named", rc == 0 and "or more:" not in out, out[-600:])

# a skipped test's reason is its last line: enough of it is printed to hold a path and a tool's own message
LONG_SKIP = {"test_skips.py": "import sys\nprint('SKIP: cannot do it here: ' + 'p' * 300 + ' THE-TOOLS-OWN-WORDS')\nsys.exit(2)\n"}
rc, out = scratch_run(RUNNER, LONG_SKIP)
arm("a skipped test's reason is printed with its end, 330 characters on", rc == 0 and "THE-TOOLS-OWN-WORDS" in out and "1 skipped" in out, out[-600:])
cut = "[:SKIP_REASON]"
rc, out = scratch_run(RUNNER.replace(cut, "[:200]"), LONG_SKIP)
arm("control: cut at 200 characters the reason loses its end", RUNNER.count(cut) == 1 and rc == 0 and "THE-TOOLS-OWN-WORDS" not in out and "1 skipped" in out, out[-600:])


# ---- how much of a failed test's output is printed: the last 1200 characters, or the last N with --tail N
def tail_run(runner_text, *opts):
    """Run a scratch test that prints a first line, 5000 characters and a last line, then fails. -> the runner's output."""
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        (root / "tools").mkdir()
        (root / "tools/run_all_tests.py").write_text(runner_text)
        (root / "tools/test_long.py").write_text("import sys\nprint('FIRST-LINE')\nprint('x' * 5000)\nprint('LAST-LINE')\nsys.exit(1)\n")
        r = subprocess.run([sys.executable, "-B", str(root / "tools/run_all_tests.py"), *opts, "test_long"],
                           env=CLEAN, capture_output=True, text=True, timeout=120)
        return r.returncode, r.stdout + r.stderr


rc, out = tail_run(RUNNER)
arm("a failed test's output is cut to its last 1200 characters by default", rc == 1 and "LAST-LINE" in out and "FIRST-LINE" not in out
    and out.count("x") < 1300, out[-400:])
rc, out = tail_run(RUNNER, "-j", "2", "--tail", "20000")
arm("--tail 20000 (beside -j) prints the whole of it", rc == 1 and "FIRST-LINE" in out and "LAST-LINE" in out and "1 failed" in out, out[-400:])
rc, out = tail_run(RUNNER, "--tail", "300")
arm("--tail 300 prints less than the default", rc == 1 and "LAST-LINE" in out and out.count("x") < 300, out[-400:])
rc, out = tail_run(RUNNER.replace("out[-TAIL:]", "out[-1200:]"), "--tail", "20000")
arm("control: a runner that ignores the option still cuts the output", rc == 1 and "FIRST-LINE" not in out, out[-400:])


# ---- bytes that are not UTF-8 in a test's output: the test fails with its output shown, the run goes on to its summary
RAW = {"test_raw.py": "import sys\nsys.stdout.buffer.write(b'BEFORE \\xff\\xfe AFTER\\n')\nsys.exit(1)\n", "test_fine.py": "print('PASS')\n"}
replace = 'errors="replace", text=True'
arm("the runner reads a test's output with undecodable bytes replaced, in one place", RUNNER.count(replace) == 1)
rc, out = scratch_run(RUNNER, RAW)
arm("a test that prints bytes that are not UTF-8 is a failure with its output, and the run ends with its summary",
    rc == 1 and "FAIL test_raw.py: exit 1" in out and "BEFORE \ufffd\ufffd AFTER" in out
    and out.strip().splitlines()[-1] == "1 passed, 1 failed, 0 skipped, 0 not run, of 2", out[-600:])
rc, out = scratch_run(RUNNER.replace(replace, "text=True"), RAW)
arm("control: read strictly, those bytes end the whole run with Python's error and no summary",
    rc == 1 and "UnicodeDecodeError" in out and "passed" not in out, out[-600:])

# ---- a test that runs long is named while the run goes on, not only in the summary at its end
# (an interval of one second and a test of four: a loaded machine may be late with a line, and may take a second over
# the short test, which is then named as well and rightly; what is held is what the lines that are there say)
LONG = {"test_long_one.py": "import time\ntime.sleep(4)\nprint('PASS')\n", "test_short_one.py": "print('PASS')\n"}
still_at = "STILL = 300 "
arm("the runner names its interval for a long test in one place", RUNNER.count(still_at) == 1)
rc, out = scratch_run(RUNNER.replace(still_at, "STILL = 1 "), LONG, limit=60)
during = [l for l in out.splitlines() if l.startswith("still running: ")]
named = [re.findall(r"(\S+) \((\d+) s\)", l) for l in during]
arm("every interval the tests that have run that long are named with their times, and none that has run for less",
    rc == 0 and sum(1 for l in named if any(n == "test_long_one.py" for n, _ in l)) >= 2 and all(named)
    and all(int(s) >= 1 for l in named for _, s in l)
    and "2 passed, 0 failed, 0 skipped, 0 not run, of 2" in out.splitlines(), out[-600:])
rc, out = scratch_run(RUNNER.replace(still_at, "STILL = 1 ").replace("long_ones = sorted(", "long_ones = [] and sorted("), LONG, limit=60)
arm("control: a runner that waits silently says nothing until the end", rc == 0 and "still running" not in out, out[-600:])


# ---- a checkout without the history the tests' controls read: one note before the summary, only after a failure
# Real git scenes in a scratch folder: a small repository with the three release tags, cloned with a file:// URL (no
# network) shallow, whole, and whole without tags, and a copy without .git. Every git here reads no configuration of the
# tester's (a ~/.gitconfig that signs every commit would fail the scene) and is handed no SDKROOT, TOOLCHAINS or
# DEVELOPER_DIR of this test's.
GIT_ENV = {k: v for k, v in CLEAN.items() if not k.startswith("GIT_")}
GIT_ENV.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main", "-c", "core.hooksPath=/dev/null",
       "-c", "commit.gpgSign=false", "-c", "tag.gpgSign=false"]
HISTORY = {"test_fails.py": "import sys\nprint('FAILS')\nsys.exit(1)\n", "test_passes.py": "print('PASS')\n"}
note_call = "        note = history_note()\n"


def git_scene(*args):
    subprocess.run(GIT + list(args), env=GIT_ENV, check=True, capture_output=True, timeout=60)


def history_scenes(d, runner_text):
    """-> {scene: tree}: the repository with the tags (origin), its shallow clone, its whole clone, its whole clone
    without tags, and a copy of it without .git"""
    o = d / "origin"
    (o / "tools").mkdir(parents=True)
    git_scene("init", "-q", str(o))
    for i, tag in enumerate(("v1.15.0", "v2.8.1", "v2.9")):
        (o / "release.txt").write_text(tag + "\n")
        git_scene("-C", str(o), "add", "-A")
        git_scene("-C", str(o), "commit", "-q", "-m", tag)
        git_scene("-C", str(o), "tag", tag)
    (o / "tools/run_all_tests.py").write_text(runner_text)
    for name, source in HISTORY.items():
        (o / f"tools/{name}").write_text(source)
    git_scene("-C", str(o), "add", "-A")
    git_scene("-C", str(o), "commit", "-q", "-m", "the tests")
    url = "file://" + str(o)
    git_scene("clone", "-q", "--depth", "1", url, str(d / "shallow"))
    git_scene("clone", "-q", url, str(d / "whole"))
    git_scene("clone", "-q", "--no-tags", url, str(d / "notags"))
    shutil.copytree(o, d / "archive", ignore=shutil.ignore_patterns(".git"))
    return {n: d / n for n in ("shallow", "whole", "notags", "archive")}


def history_run(tree, *names):
    """-> (exit status, output lines) of the scene's runner on the named tests"""
    r = subprocess.run([sys.executable, "-B", str(tree / "tools/run_all_tests.py"), *names], env=GIT_ENV, capture_output=True,
                       text=True, timeout=120)
    return r.returncode, (r.stdout + r.stderr).strip().splitlines()


def notes(lines):
    return [l for l in lines if l.startswith("note: ")]


arm("the runner asks for its note in one place", RUNNER.count(note_call) == 1)
with tempfile.TemporaryDirectory() as d:
    sc = history_scenes(pathlib.Path(d).resolve(), RUNNER)
    for scene, cause in (("shallow", "is a shallow clone"), ("notags", "lacks the tags v2.9, v2.8.1, v1.15.0"),
                         ("archive", "is not a git checkout")):
        rc, out = history_run(sc[scene], "test_fails", "test_passes")
        arm(f"{scene}: after a failure, one note just before the summary names the cause ({cause}), and the exit status is 1",
            rc == 1 and len(notes(out)) == 1 and out[-2] == notes(out)[0] and cause in out[-2]
            and out[-1] == "1 passed, 1 failed, 0 skipped, 0 not run, of 2", (rc, out[-3:]))
        rc, out = history_run(sc[scene], "test_passes")
        arm(f"{scene}: without a failure, no note and exit status 0", rc == 0 and not notes(out), (rc, out[-3:]))
    rc, out = history_run(sc["whole"], "test_fails", "test_passes")
    arm("whole clone with the tags: no note after a failure", rc == 1 and not notes(out) and out[-1].startswith("1 passed, 1 failed"), out[-3:])
    # the command a note names brings what the tests read
    for scene, want in (("shallow", "git fetch --unshallow --tags"), ("notags", "git fetch --tags")):
        rc, out = history_run(sc[scene], "test_fails")
        cmd = re.findall(r"`([^`]+)`", notes(out)[0]) if notes(out) else []
        arm(f"{scene}: the note names `{want}`", cmd == [want], out[-2:])
        if cmd == [want]:
            git_scene("-C", str(sc[scene]), *want.split()[1:])
            rc, out = history_run(sc[scene], "test_fails")
            arm(f"{scene}: after `{want}` the tags are there and no note is printed", rc == 1 and not notes(out), out[-3:])
    rc, out = history_run(sc["archive"], "test_fails")
    arm("the note in a tree without .git says to clone the whole repository", bool(notes(out)) and "git clone without --depth" in notes(out)[0], out[-2:])
with tempfile.TemporaryDirectory() as d:
    sc = history_scenes(pathlib.Path(d).resolve(), RUNNER.replace(note_call, "        note = ''\n"))
    rc, out = history_run(sc["shallow"], "test_fails", "test_passes")
    arm("control: the runner without the note says nothing in a shallow clone", rc == 1 and not notes(out), out[-3:])
# with no git to ask, a checkout gives no note and the run ends as always
with tempfile.TemporaryDirectory() as d:
    sc = history_scenes(pathlib.Path(d).resolve(), RUNNER)
    nogit = pathlib.Path(d) / "nogit-bin"
    nogit.mkdir()
    r = subprocess.run([sys.executable, "-B", str(sc["shallow"] / "tools/run_all_tests.py"), "test_fails", "test_passes"],
                       env=dict(GIT_ENV, PATH=str(nogit)), capture_output=True, text=True, timeout=120)
    out = (r.stdout + r.stderr).strip().splitlines()
    arm("with no git on PATH a shallow clone gives no note, and the run ends with its summary and exit status 1",
        r.returncode == 1 and not notes(out) and out[-1] == "1 passed, 1 failed, 0 skipped, 0 not run, of 2", out[-3:])


print(f"{fails} failed")
sys.exit(1 if fails else 0)
