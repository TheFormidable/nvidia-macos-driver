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

A test that makes a folder beside the temporary folder it was given (a bare mktemp on macOS does) has it listed at the
end of the run. Negative control: the runner without that list.

Then tests that leave a program running in the background, that pass their time limit, and that try to reach the
terminal: the runner starts each test as a session of its own, without a terminal and with nothing on its input, and
stops what it started. Negative control: the same runner with the test in the runner's own session."""
import sys
sys.dont_write_bytecode = True   # run alone, this test writes none for its own imports either (python3 -B, from inside)
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
CLEAN = {k: v for k, v in os.environ.items() if k not in ("PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX")}  # the outer runner sets the first


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
        root = pathlib.Path(d)
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


# ---- the tester's driver settings do not reach a test; what a test takes from the tester does
ENV = """import json, os, pathlib
pathlib.Path(__file__).resolve().parents[1].joinpath("env.json").write_text(json.dumps(sorted(os.environ)))
"""
DRIVER = {"NVMTL_X": "1", "NVK_Y": "1", "NVRM_Z": "1", "NAK_DEBUG": "print", "ELFHOST_PATH": "/x", "METAL2VULKAN_DUMP": "1",
          "MESA_LOG_FILE": "/x", "NVENCVT_LOG": "1", "NVDEC_SYNC": "1", "RR_FLAGS": "1", "DLSS_TRACE": "1",
          "VK_ICD_FILENAMES": "/x", "DYLD_INSERT_LIBRARIES": "/x", "NVK_PATH": "/x"}
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
"""


def escape_run(runner_text):
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        (root / "tools").mkdir()
        (root / "systmp").mkdir()
        (root / "tools/run_all_tests.py").write_text(runner_text)
        (root / "tools/test_escape.py").write_text(ESCAPE)
        r = subprocess.run([sys.executable, "-B", str(root / "tools/run_all_tests.py")], env=dict(CLEAN, TMPDIR=str(root / "systmp")),
                           capture_output=True, text=True, timeout=120)
        return r.returncode, r.stdout + r.stderr, sorted(os.listdir(root / "systmp"))


rc, out, there = escape_run(RUNNER)
lines = [l for l in out.splitlines() if l.startswith("left in ")]
arm("a folder a test made beside its own temporary folder is listed at the end, and the run is not failed for it",
    rc == 0 and any("systmp during the run" in l and l.endswith(tuple(there)) and "escaped-" in l for l in lines), (there, out))
arm("the test's own temporary folder is not listed (it is gone)", not any(l.rsplit(": ", 1)[-1].startswith("nmtest-") for l in lines) and not any(n.startswith("nmtest-") for n in there), out)
report = '            print(f"left in {root} during the run (by a test, or by another program meanwhile): {n}")\n'
arm("the runner lists what appeared in one place", RUNNER.count(report) == 1)
rc, out, there = escape_run(RUNNER.replace(report, "            pass\n"))
arm("control: without the list the escaped folder goes unnoticed", rc == 0 and any(n.startswith("escaped-") for n in there)
    and not any("escaped-" in l for l in out.splitlines()), out)


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


print(f"{fails} failed")
sys.exit(1 if fails else 0)
