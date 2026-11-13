#!/usr/bin/env python3
"""run_all_tests.py [-j N] [name ...]: every host test in tools/, in one run, with one answer.

The tests are separate programs (Python scripts, C and C++ files with their build line in the header comment, shell
scripts), and nothing ran them all: seven of them had been failing unnoticed because the code they lift pieces from had
moved on. This runs each one, prints the failures with the end of their output, and exits 1 if any failed.

  Python   tools/test_*.py, run as they are.
  C, C++   tools/test_*.c and .cpp whose header names their build ("cc <flags> <file> -o /tmp/x && /tmp/x"): built into a
           temporary folder with those flags and run. A file without such a line is listed as not run.
  Shell    tools/test_*.sh that take no argument.

Not run here, and listed at the end with the reason: tests that need a built library or an outside source tree. They are
run at the build step, where those exist. Needs a Mac with the Xcode command line tools (the tests compile Objective-C
and x86_64 code). Run it from a full clone: negative controls read older revisions of the source.

Each test gets a temporary folder of its own as TMPDIR, deleted when it ends, so that what a test leaves behind in its
temporary files does not pile up in the system's; and no test writes Python bytecode, neither into the checkout nor
anywhere else (Apple's python3 would otherwise put it under ~/Library/Caches/com.apple.python, its sys.pycache_prefix).
What a test leaves in the system's temporary folders instead is listed at the end. Each test runs in a session of its
own with no terminal and nothing on its standard input, so nothing it starts can ask for a password or an answer there;
at its time limit, or when the run is interrupted, the test is stopped with everything it started.

No test sees the driver's own settings of the tester's environment (a Mac with the driver installed often exports
some): every variable with a prefix of the plugin's knobs and every name tools/switches.tsv lists as read from the
environment is left out, except the standard ones and NVRM_PRISTINE_OGKM (a source tree a test may be pointed at).
What a test is meant to take from the tester stays: CC, CXX, ZSTD, BUILD_SH_BASH, NVENCVT_HEADERS, NVDEC_HEADERS, NM_*."""
import sys
sys.dont_write_bytecode = True   # nor the runner itself, for what it imports from here on (python3 -B, but from inside)
import concurrent.futures  # noqa: E402
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
# name -> why this runner cannot run it
NEEDS_INPUT = {
    "test_fcwarm.py": "needs a built libnvmtl_translate.dylib and a shader file (build step)",
    "test_gl_translation.py": "needs a built libnvmtl_translate.dylib (build step)",
    "test_resource_phi_fold.py": "needs a built libnvmtl_translate.dylib (build step)",
    "test_patch_chain.sh": "needs a pristine NVIDIA open-gpu-kernel-modules 620.70.06 tree (build_nvrm.sh runs it)",
}
BUILD_LINE = re.compile(r"(?:^|[\s*/])(cc|c\+\+|clang|clang\+\+)\s+(.*?)\s+&&\s+\S+")
TIMEOUT = 600
# A few tests legitimately run far longer than the rest; they get their own limit, not a general rise that would let a
# real hang in another test sit for longer. test_build_sh.py builds and exercises the one-command build script: it took
# 376 s alone on an Intel Mac with 12 CPUs and grows under load with -j 4, so the general 600 s is too tight for it.
# The limit is per test name; every other test keeps TIMEOUT.
PER_TEST_TIMEOUT = {"test_build_sh.py": 1200}
# the prefixes of plugin/nvmtl_env_knobs.h's nvmtl_env_knob_prefixed() (tools/test_run_all_tests.py holds them equal)
KNOB_PREFIXES = ("NVMTL_", "NVK_", "NAK_", "NVRM_", "METAL2VULKAN_", "ELFHOST_", "MESA_", "NIR_", "RADV_", "ACO_")
# listed in switches.tsv because the driver reads them too, and still the tester's to give
KEEP_ENV = {"HOME", "PATH", "TMPDIR", "XDG_CACHE_HOME", "NVRM_PRISTINE_OGKM"}
RUNNING = set()          # the tests' process groups, stopped when the run is interrupted
RUNNING_LOCK = threading.Lock()
OWN_TMPS = set()         # the temporary folders this runner made for its tests (realpath), not left by a test


class Result:
    def __init__(self, returncode, stdout, stderr):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def stop(p):
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run(argv, cwd, env, timeout=TIMEOUT):
    """subprocess.run, but the program is a session of its own (no controlling terminal) with /dev/null as its input, and
    at the limit it is stopped with everything it started: killing only the test would leave its children running, still
    writing into the temporary folder that is deleted next."""
    p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, start_new_session=True)
    with RUNNING_LOCK:
        RUNNING.add(p)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        stop(p)
        try:
            p.communicate(timeout=10)
        except subprocess.TimeoutExpired:   # something that left the session still holds the output open
            p.kill()
        raise
    finally:
        with RUNNING_LOCK:
            RUNNING.discard(p)
    stop(p)   # what it left running in the background after it ended
    return Result(p.returncode, out, err)


def driver_env_names():
    """every name tools/switches.tsv lists as an environment variable the driver or its build reads"""
    try:
        rows = (TOOLS / "switches.tsv").read_text().splitlines()
    except OSError:
        return set()
    return {r.split("\t")[1] for r in rows if r.startswith(("env\t", "build\t"))}


def test_env(tmp, names):
    """the tester's environment without the driver's settings, with the test's own TMPDIR and no bytecode"""
    env = {k: v for k, v in os.environ.items() if k in KEEP_ENV or not (k.startswith(KNOB_PREFIXES) or k in names)}
    env.update(TMPDIR=tmp, PYTHONDONTWRITEBYTECODE="1")
    return env


def temp_listing():
    """-> {(folder, name)} of the system's temporary folders: the one this runner was given and /tmp"""
    seen = set()
    for root in {os.path.realpath(tempfile.gettempdir()), os.path.realpath("/tmp")}:
        try:
            seen |= {(root, n) for n in os.listdir(root)}
        except OSError:
            pass
    return seen


def c_command(path, out):
    """-> (argv, cwd) from the header's build line, or None. The line is written for the repository root when it names the
    file as tools/<file>, and for tools/ otherwise."""
    head = "\n".join(path.read_text(errors="replace").splitlines()[:12])
    for m in BUILD_LINE.finditer(head):
        words = shlex.split(m.group(2))
        if path.name not in [Path(w).name for w in words] or "-o" not in words:
            continue
        if any("<" in w or ">" in w for w in words):
            return "placeholder"      # the line names something to fill in (a header folder, a library)
        cwd = ROOT if f"tools/{path.name}" in words else TOOLS
        i = words.index("-o")
        words[i + 1] = str(out)
        return [m.group(1)] + words, cwd
    return None


def run_one(path, names=frozenset()):
    """-> (name, state, detail): state is ok, FAIL, skip or norun."""
    name = path.name
    if name in NEEDS_INPUT:
        return name, "norun", NEEDS_INPUT[name]
    limit = PER_TEST_TIMEOUT.get(name, TIMEOUT)
    tmp = tempfile.mkdtemp(prefix="nmtest-")
    OWN_TMPS.add(os.path.realpath(tmp))
    env = test_env(tmp, names)
    try:
        if path.suffix == ".py":
            r = run([sys.executable, str(path)], ROOT, env, limit)
        elif path.suffix == ".sh":
            r = run(["/bin/bash", str(path)], TOOLS, env, limit)
        else:
            t = Path(tmp) / "build"
            t.mkdir()
            cmd = c_command(path, t / "t")
            if cmd == "placeholder":
                return name, "norun", "its build line needs a path filled in (see its header)"
            if not cmd:
                return name, "norun", "its header names no build line"
            b = run(cmd[0], cmd[1], env, limit)
            if b.returncode:
                return name, "FAIL", "does not build:\n" + (b.stdout + b.stderr)[-1200:]
            r = run([str(t / "t")], cmd[1], env, limit)
    except subprocess.TimeoutExpired:
        return name, "FAIL", f"no result after {limit} s"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    out = (r.stdout + r.stderr)
    if r.returncode == 0:
        return name, "ok", ""
    if r.returncode == 2 and "SKIP" in out:
        return name, "skip", out.strip().splitlines()[-1][:200]
    return name, "FAIL", f"exit {r.returncode}\n" + out[-1200:]


def main(argv):
    jobs = 4
    if argv[:1] == ["-j"]:
        jobs, argv = int(argv[1]), argv[2:]
    tests = sorted(p for p in TOOLS.glob("test_*") if p.suffix in (".py", ".c", ".cpp", ".sh"))
    if argv:
        tests = [p for p in tests if p.name in argv or p.stem in argv]
    if not tests:
        print("no test matched"); return 1
    # the tests are sessions of their own, so a Ctrl-C at the terminal reaches only this runner: it stops them
    def interrupted(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    names = frozenset(driver_env_names())
    before = temp_listing()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=jobs)
    try:
        results = list(pool.map(lambda p: run_one(p, names), tests))
    except KeyboardInterrupt:
        pool.shutdown(wait=False, cancel_futures=True)
        with RUNNING_LOCK:
            for p in list(RUNNING):
                stop(p)
        print("interrupted: the running tests were stopped", file=sys.stderr)
        return 130
    pool.shutdown()
    by = {s: [(n, d) for n, st, d in results if st == s] for s in ("ok", "FAIL", "skip", "norun")}
    for n, d in by["FAIL"]:
        print(f"FAIL {n}: {d}\n")
    for n, d in by["skip"]:
        print(f"skip {n}: {d}")
    for n, d in by["norun"]:
        print(f"not run {n}: {d}")
    # a test's own folder is deleted after it; what appeared beside it escaped it (a bare mktemp on macOS ignores TMPDIR).
    # Which test made it cannot be told when tests run side by side, and another program may have made it meanwhile, so
    # it is listed and does not fail the run.
    for root, n in sorted(temp_listing() - before):
        if os.path.join(root, n) not in OWN_TMPS:   # only the runner's own, by path: a test may name its folder alike
            print(f"left in {root} during the run (by a test, or by another program meanwhile): {n}")
    print(f"{len(by['ok'])} passed, {len(by['FAIL'])} failed, {len(by['skip'])} skipped, {len(by['norun'])} not run, of {len(results)}")
    return 1 if by["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
