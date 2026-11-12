#!/usr/bin/env python3
"""tools/run_all_tests.py itself: every test it runs gets a temporary folder of its own as TMPDIR, deleted when the test
ends, and no Python bytecode is written beside the tests.

The runner is copied into a scratch tree whose only test makes a temporary folder with Python's tempfile and leaves it
behind, as several tests of this tree do. Negative control: the same runner without its own TMPDIR leaves the folder."""
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

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


print(f"{fails} failed")
sys.exit(1 if fails else 0)
