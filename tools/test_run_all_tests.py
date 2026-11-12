#!/usr/bin/env python3
"""tools/run_all_tests.py itself: every test it runs gets a temporary folder of its own as TMPDIR, deleted when the test
ends, and no Python bytecode is written beside the tests.

The runner is copied into a scratch tree whose only test makes a temporary folder with Python's tempfile and leaves it
behind, as several tests of this tree do. Negative control: the same runner without its own TMPDIR leaves the folder.

Then tests that leave a program running in the background, that pass their time limit, and that try to reach the
terminal: the runner starts each test as a session of its own, without a terminal and with nothing on its input, and
stops what it started. Negative control: the same runner with the test in the runner's own session."""
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNNER = (ROOT / "tools/run_all_tests.py").read_text()
fails = 0


def arm(name, ok, detail=""):
    global fails
    fails += not ok
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f" ({str(detail)[-400:]})"))


LEAK = '''import pathlib, sys, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import helper_mod  # noqa: F401  (Python would write its bytecode beside it, into the checkout)
d = tempfile.mkdtemp(prefix="leak-")
pathlib.Path(d, "x").write_text("left behind")
pathlib.Path(__file__).resolve().parents[1].joinpath("leak.txt").write_text(d)
'''


def leak_run(runner_text):
    """-> (runner exit status, the folder the test made, whether it still exists, whether bytecode was written, output)"""
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        (root / "tools").mkdir()
        (root / "tools/run_all_tests.py").write_text(runner_text)
        (root / "tools/test_leak.py").write_text(LEAK)
        (root / "tools/helper_mod.py").write_text("X = 1\n")
        env = {k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"}   # the outer runner sets it
        r = subprocess.run([sys.executable, str(root / "tools/run_all_tests.py")], env=env, capture_output=True, text=True,
                           timeout=120)
        made = (root / "leak.txt").read_text() if (root / "leak.txt").exists() else ""
        left = bool(made) and os.path.exists(made)
        if left:
            shutil.rmtree(made, ignore_errors=True)
        return r.returncode, made, left, (root / "tools/__pycache__").exists(), r.stdout + r.stderr


rc, made, left, pyc, out = leak_run(RUNNER)
arm("a test's temporary folder is made inside one the runner gave it", rc == 0 and pathlib.Path(made).parent.name.startswith("nmtest-"),
    (rc, made, out))
arm("and is gone when the runner is done", made and not left, (made, out))
arm("a module the test imports leaves no bytecode in the checkout", rc == 0 and not pyc, out)

old_line = "env = dict(os.environ, TMPDIR=tmp, PYTHONDONTWRITEBYTECODE=\"1\")"
arm("the runner sets TMPDIR (and no bytecode) in one place", RUNNER.count(old_line) == 1)
rc, made, left, pyc, out = leak_run(RUNNER.replace(old_line, "env = dict(os.environ, PYTHONDONTWRITEBYTECODE=\"1\")"))
arm("control: without its own TMPDIR the test's folder is left behind", made and left, (rc, made, out))
rc, made, left, pyc, out = leak_run(RUNNER.replace(old_line, "env = dict(os.environ, TMPDIR=tmp)"))
arm("control: without PYTHONDONTWRITEBYTECODE the bytecode is written", rc == 0 and pyc, out)


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
