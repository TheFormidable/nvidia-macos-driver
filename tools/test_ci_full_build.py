#!/usr/bin/env python3
"""build/ci_full_build.sh, the checks of the full build from an empty folder (.github/workflows/full-build.yml), says
"ok" for a working directory that did what it should, and "FAIL" for each way one can be wrong.

The full build itself runs only on GitHub's Intel Mac. Its checks are logic that runs anywhere, so this test builds a
small working directory as such a build leaves it, with real Mach-O files (made with clang for macOS: on a Mac with
Xcode's, elsewhere with clang, ld64.lld and llvm-otool, put on PATH as otool), a stand-in Homebrew prefix and home
folder, and a scratch git checkout holding the script. Then: everything ok; and one control per check, each breaking
that one thing (a library loaded from Homebrew's prefix, a search path there, a tool taken from Homebrew, a shipped
file for macOS 26, a changed Homebrew listing, a file Homebrew keeps that changed, a brew process the watch sees, a
damaged package, a changed checkout, a step log naming Homebrew's prefix, a missing tool, a missing package). Also:
the download of the base package refuses a file that is not the pinned one, the dry-run reader recognises the lines
build.sh prints before it calls Homebrew, and the failure listing shows the logs' ends and their error lines.

This test runs on testers' Macs too, so what the job may do there and a tester's suite must not is held off: the watch,
a session of its own that would outlive a killed test, ends with this test's process (a guard, with its own arm); the
reads run with a stand-in for Apple's /usr/bin/make, which the job starts with an empty developer folder; and neither
this test's git nor the script's reads any git configuration of the tester's.
Usage: test_ci_full_build.py [driver checkout]. Exit 2 with SKIP when this machine cannot make Mach-O files."""
import hashlib
import os
import pathlib
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time

ROOT = pathlib.Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "build/ci_full_build.sh"
BASH = os.environ.get("BUILD_SH_BASH", "/bin/bash")
FAILS = []


def arm(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (f"  [{str(detail)[-1500:]}]" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def skip(why):
    print(f"SKIP: {why}")
    sys.exit(2)


# ------------------------------------------------------------------------------------------------ making Mach-O files
def toolchain(bindir):
    """-> the compiler command for an x86_64 macOS target, with an otool on PATH (a stand-in made in bindir)."""
    if platform.system() == "Darwin":
        if not shutil.which("xcrun") or subprocess.run(["xcrun", "--find", "otool"], capture_output=True).returncode:
            skip("no Xcode (xcrun, otool)")
        return ["xcrun", "clang", "-arch", "x86_64"], None
    cc = shutil.which("clang")
    lld = next((shutil.which(n) for n in ("ld64.lld", "ld64.lld-18", "ld64.lld-19", "ld64.lld-20") if shutil.which(n)), None)
    otool = next((shutil.which(n) for n in ("llvm-otool", "llvm-otool-18", "llvm-otool-19", "llvm-otool-20") if shutil.which(n)), None)
    if not (cc and lld and otool):
        skip("needs clang, ld64.lld and llvm-otool to make and read Mach-O files on this system")
    (bindir / "otool").write_text(f'#!/bin/sh\nexec {otool} "$@"\n')
    (bindir / "otool").chmod(0o755)
    (bindir / "ld64.lld").symlink_to(lld) if not (bindir / "ld64.lld").exists() else None
    return [cc, "-target", "x86_64-apple-macos"], str(bindir)


class Maker:
    def __init__(self, tmp):
        self.src = tmp / "src"
        self.src.mkdir()
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.cc, self.extra_path = toolchain(self.bin)
        (self.src / "f.c").write_text("int nm_f(void) { return 1; }\n")
        # no call into the libraries: the load commands are what is checked, and a call would need macOS's dyld_stub_binder
        (self.src / "m.c").write_text("int nm_g(void) { return 2; }\n")
        self.stubs = {}

    def _cc(self, minos):
        if self.cc[-1] == "x86_64-apple-macos":
            return self.cc[:-1] + [f"x86_64-apple-macos{minos}", "-fuse-ld=lld", f"-B{self.bin}"]
        return self.cc + [f"-mmacosx-version-min={minos}"]

    def stub(self, name):
        """a library whose install name is `name`, to link against (nothing of it is copied anywhere)"""
        if name not in self.stubs:
            out = self.src / f"stub{len(self.stubs)}.dylib"
            subprocess.run(self._cc("15.0") + ["-nostdlib", "-dynamiclib", "-install_name", name, "-o", str(out), str(self.src / "f.c")], check=True,
                           capture_output=True, env=self.env())
            self.stubs[name] = out
        return self.stubs[name]

    def env(self):
        e = dict(os.environ)
        if self.extra_path:
            e["PATH"] = self.extra_path + os.pathsep + e["PATH"]
        return e

    def make(self, out, kind="dylib", loads=("/usr/lib/libSystem.B.dylib",), rpaths=(), minos="15.0", install_name=None):
        out = pathlib.Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        args = self._cc(minos) + ["-nostdlib"]
        if kind == "dylib":
            args += ["-dynamiclib", "-install_name", install_name or str(out)]
        elif kind == "bundle":
            args += ["-bundle"]
        elif kind == "object":
            subprocess.run(self._cc(minos) + ["-c", "-o", str(out), str(self.src / "f.c")], check=True, capture_output=True, env=self.env())
            return out
        for r in rpaths:
            args += ["-Wl,-rpath," + r]
        libs = [str(self.stub(n)) for n in loads]
        r = subprocess.run(args + ["-o", str(out), str(self.src / "m.c")] + libs, capture_output=True, text=True, env=self.env())
        if r.returncode:
            raise RuntimeError(r.stderr)
        out.chmod(0o755)
        return out


# ------------------------------------------------------------------------------------------------ the scene
def sha(p):
    return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()


def write_sums(root):
    files = sorted(p for p in pathlib.Path(root).rglob("*") if p.is_file() and p.name != "SHA256SUMS")
    (pathlib.Path(root) / "SHA256SUMS").write_text("".join(f"{sha(p)}  {p.relative_to(root)}\n" for p in files))


def run(argv, env, cwd=None, timeout=120):
    r = subprocess.run([BASH, str(argv[0])] + [str(a) for a in argv[1:]], capture_output=True, text=True, env=env, cwd=cwd, timeout=timeout)
    return r.returncode, r.stdout + r.stderr


def git_quiet(env):
    """env for git without the tester's git: no configuration file of theirs (global, $XDG_CONFIG_HOME's, the
    system's) and none of their GIT_* variables (a GIT_DIR would point git at another repository)"""
    e = {k: v for k, v in env.items() if not k.startswith("GIT_")}
    e.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return e


# The watch the script starts (watch-loop) is a session of its own on purpose: it must outlive the job's step that
# starts it. So it also outlives this test when the test is killed (the runner's time limit and Ctrl-C kill the test's
# process group): it lists the machine's processes twice a second for as long as its pid file exists, which is for
# good when the test was run by hand, and a watch the stall controls stopped (SIGSTOP) stays stopped even after the
# runner deletes the folder. The guard is a small process of its own session as well: it notes every watch this test
# starts and, once the test's process is gone, removes the pid file (every watch then ends by itself) and ends each of
# those watches that is still there, also a stopped one. It names the folder only in its environment, so that the
# test's own last check ("no watch is left running") does not see it.
GUARD = r'''seen=" "
# a killed test that its parent has not reaped yet is a zombie, which kill -0 still finds: that one is gone too
while kill -0 "$NMT_PID" 2>/dev/null && case "$(ps -o stat= -p "$NMT_PID" 2>/dev/null)" in *Z*) false ;; *) true ;; esac; do
  p=$(sed -n 1p "$NMT_CI/brew-watch.pid" 2>/dev/null)
  case "$p" in ""|*[!0-9]*) ;; *) case "$seen" in *" $p "*) ;; *) seen="$seen$p " ;; esac ;; esac
  sleep 1
done
rm -f "$NMT_CI/brew-watch.pid"
for p in $seen; do
  case "$(ps -o command= -p "$p" 2>/dev/null)" in *"watch-loop $NMT_CI "*) kill -CONT "$p" 2>/dev/null; kill "$p" 2>/dev/null ;; esac
done
'''


def guard(ci, pid):
    """starts the guard of the watches in <ci> for as long as process <pid> lives"""
    return subprocess.Popen(["/bin/sh", "-c", GUARD], env={"PATH": "/usr/bin:/bin", "NMT_PID": str(pid), "NMT_CI": str(ci)},
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def watches(ci):
    """-> [(pid, state)] of the watch loops running for <ci>"""
    r = subprocess.run(["ps", "-axo", "pid=,stat=,command="], capture_output=True, text=True).stdout
    return [(int(l.split()[0]), l.split()[1]) for l in r.splitlines() if f"watch-loop {ci} " in l]


class Scene:
    def __init__(self, tmp, mk):
        self.tmp, self.mk = tmp, mk
        # a checkout that holds the script, so that its git status is this test's to set
        self.co = tmp / "checkout"
        (self.co / "build").mkdir(parents=True)
        shutil.copy(SCRIPT, self.co / "build/ci_full_build.sh")
        (self.co / "build/pins.sh").write_text("NM_PIN_X=1\n")
        g = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main", "-c", "core.hooksPath=/dev/null"]
        # with no configuration but these settings: a tester's ~/.gitconfig may sign every commit (it would run their
        # signing program, and a failed signature fails the commit), name a template folder or start a file monitor
        genv = git_quiet(os.environ)
        subprocess.run(g + ["init", "-q", str(self.co)], check=True, env=genv)
        subprocess.run(g + ["-C", str(self.co), "add", "-A"], check=True, env=genv)
        subprocess.run(g + ["-C", str(self.co), "commit", "-q", "-m", "x"], check=True, env=genv)
        self.script = self.co / "build/ci_full_build.sh"
        self.home = tmp / "home"
        self.p = tmp / "usrlocal"          # Homebrew's prefix as an Intel Mac has it
        self.w = tmp / "work"              # the working directory
        self.ci = tmp / "ci"               # the job's own files
        # the script's own git (prove's git status and rev-parse in this checkout) reads no configuration of the
        # tester's either: a new HOME hides ~/.gitconfig, not $XDG_CONFIG_HOME/git/config or the system's file, and a
        # core.fsmonitor there names a program that git status starts
        self.env = git_quiet(mk.env())
        # NMCI_WATCH_MAX: a watch this test starts ends by itself within ten minutes, whatever became of the test
        self.env.update(HOME=str(self.home), TMPDIR=str(tmp), LC_ALL="C", NMCI_WATCH_MAX="600")
        self.env.pop("GITHUB_ACTIONS", None)
        self.homebrew()
        self.workdir()

    def homebrew(self):
        p = self.p
        for d in ("bin", "opt", "Cellar/cmake/4.5.0/bin", "Cellar/pkgconf/3.0.10/bin", "Homebrew/Library/Homebrew", "var/homebrew/locks"):
            (p / d).mkdir(parents=True, exist_ok=True)
        # it runs until the watch has listed it (at most 20 s): a loaded machine cannot make it end unseen
        (p / "bin/brew").write_text(seen_then_exit(self.ci))
        (p / "bin/brew").chmod(0o755)
        (p / "Cellar/cmake/4.5.0/bin/cmake").write_text("")
        (p / "bin/cmake").symlink_to("../Cellar/cmake/4.5.0/bin/cmake")
        (p / "opt/cmake").symlink_to("../Cellar/cmake/4.5.0")
        (p / "opt/pkgconf").symlink_to("../Cellar/pkgconf/3.0.10")
        (p / "bin/gtar").write_text("")
        for d in ("Library/Caches/Homebrew/api", "Library/Logs/Homebrew"):
            (self.home / d).mkdir(parents=True, exist_ok=True)
        (self.home / "Library/Caches/Homebrew/api/formula.jws.json").write_text("{}")

    def workdir(self):
        w, mk = self.w, self.mk
        tc = w / "tools/toolchain"
        (w / ".nullmoth-build-workdir").parent.mkdir(parents=True, exist_ok=True)
        (w / ".nullmoth-build-workdir").write_text("made by build.sh\n")
        for b in ("ninja", "cmake", "pkgconf", "spirv-val", "spirv-opt", "glslang"):
            mk.make(tc / "bin" / b, "bundle", ("/usr/lib/libSystem.B.dylib", "/usr/lib/libc++.1.dylib"))
        mk.make(tc / "python/bin/python3.14", "bundle", ("/usr/lib/libSystem.B.dylib", "/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation"))
        mk.make(tc / "python/lib/python3.14/lib-dynload/_ssl.cpython-314-darwin.so", "bundle")
        mk.make(tc / "llvm/lib/libLLVM-22.dylib", "dylib", ("/usr/lib/libz.1.dylib", "/usr/lib/libSystem.B.dylib"))
        (tc / "llvm/lib/libLLVM.dylib").symlink_to("libLLVM-22.dylib")
        mk.make(tc / "llvm/lib/libclang-cpp.dylib", "dylib", (str(tc / "llvm/lib/libLLVM.dylib"), "/usr/lib/libSystem.B.dylib"))
        mk.make(tc / "llvm/bin/llvm-config", "bundle", (str(tc / "llvm/lib/libLLVM.dylib"), "/usr/lib/libSystem.B.dylib"))
        (tc / ".nm-toolchain").mkdir()
        (tc / ".nm-toolchain/ninja").write_text("3441b633 ninja recipe 2 Darwin-x86_64 macos15.0\n")
        mk.make(w / "tools/spirv-llvm-translator/lib/libLLVMSPIRVLib.dylib", "dylib", (str(tc / "llvm/lib/libLLVM.dylib"),))
        mk.make(w / "tools/cargo/bin/bindgen", "bundle", ("/usr/lib/libSystem.B.dylib",), rpaths=("@loader_path/../lib",))
        mk.make(w / "tools/cargo/bin/cbindgen", "bundle", ("/usr/lib/libSystem.B.dylib",))
        mk.make(w / "tools/spirv-llvm-translator/bin/llvm-spirv", "bundle", (str(w / "tools/spirv-llvm-translator/lib/libLLVMSPIRVLib.dylib"),))
        rs = w / "tools/rustup/toolchains/1.98.1-x86_64-apple-darwin/bin"
        for b in ("rustc", "cargo"):
            mk.make(rs / b, "bundle", ("/usr/lib/libSystem.B.dylib",), rpaths=("@loader_path/../lib",))
        (tc / "bin/pkg-config").symlink_to("pkgconf")
        (tc / "bin/glslangValidator").symlink_to("glslang")
        tb = w / "tools/bin"
        tb.mkdir(parents=True)
        # the links build.sh's make_tool_links makes
        for n, t in (("ninja", tc / "bin/ninja"), ("cmake", tc / "bin/cmake"), ("pkg-config", tc / "bin/pkg-config"),
                     ("glslangValidator", tc / "bin/glslangValidator"), ("llvm-config", tc / "llvm/bin/llvm-config"),
                     ("llvm-spirv", w / "tools/spirv-llvm-translator/bin/llvm-spirv"), ("rustc", rs / "rustc"), ("cargo", rs / "cargo"),
                     ("bindgen", w / "tools/cargo/bin/bindgen"), ("cbindgen", w / "tools/cargo/bin/cbindgen")):
            (tb / n).symlink_to(t)
        (tb / "patch").write_text('#!/bin/sh\n# made by build.sh: the patch it chose, with -f so that it never waits for an answer\nexec /usr/bin/patch -f "$@"\n')
        (w / "tools/venv/bin").mkdir(parents=True)
        (w / "tools/venv/bin/python3").symlink_to(tc / "python/bin/python3.14")
        # out/: what the steps built, NVK's build tree with mesa_clc and object files among it
        mk.make(w / "out/plugin/NVMTLDriver", "bundle", ("/usr/lib/libSystem.B.dylib", "/System/Library/Frameworks/Metal.framework/Versions/A/Metal",
                                                          "/Library/GPUBundles/nvmtl/libnvmtl_osver.dylib"))
        mk.make(w / "out/nvk/libvulkan_nouveau.dylib", "dylib", ("/usr/lib/libSystem.B.dylib",), rpaths=("@loader_path",),
                install_name="@rpath/libvulkan_nouveau.dylib")
        mk.make(w / "out/nvk-build/src/compiler/clc/mesa_clc", "bundle", (str(tc / "llvm/lib/libLLVM.dylib"), str(tc / "llvm/lib/libclang-cpp.dylib"),
                                                                          str(w / "tools/spirv-llvm-translator/lib/libLLVMSPIRVLib.dylib")))
        mk.make(w / "out/nvk-build/src/x.o", "object")
        (w / "logs").mkdir()
        (w / "logs/nvk.log").write_text("build.sh step nvk\n----\nProgram glslangValidator found: YES (" + str(tc) + "/bin/glslang)\n")
        (w / "logs/plugin.log").write_text("build.sh step plugin\nNVMTLDriver compiled\n")
        # the package: a base package as 2.9 has it, and the staged one
        base = w / "package/base/pkgroot"
        lib = "Library/GPUBundles/NVMTLDriver.bundle/Contents/MacOS"
        mk.make(base / lib / "NVMTLDriver", "bundle", minos="15.0")
        mk.make(base / "Library/NullMoth/video/libold.dylib", "dylib", minos="13.0")
        (base / "install.sh").write_text("#!/bin/sh\n")
        (base / "VERSION").write_text("2.9\n")
        (base / "Library/NullMoth/gone.txt").write_text("x")
        write_sums(base)
        r = w / "package/pkgroot"
        shutil.copytree(base, r, symlinks=True)
        (r / "Library/NullMoth/gone.txt").unlink()
        shutil.copy(w / "out/plugin/NVMTLDriver", r / lib / "NVMTLDriver")
        mk.make(r / "Library/NullMoth/dlss/nm_unzstd", "bundle", minos="12.0")
        (r / "VERSION").write_text("3.0\n")
        write_sums(r)
        with tarfile.open(w / "package/nullmoth-nvidia-local-abc1234.tar.gz", "w:gz") as t:
            t.add(r, arcname="pkgroot")

    def before(self):
        rc, out = run([self.script, "brew-state", self.ci / "brew-before.txt", self.p], self.env)
        assert rc == 0, out
        time.sleep(1.1)   # a file changed later is newer than the listing also where times have whole seconds
        rc, out = run([self.script, "watch-start", self.ci, self.p], self.env)
        assert rc == 0, out

    def prove(self, watch=True):
        if watch and not (self.ci / "brew-watch.pid").exists():
            run([self.script, "watch-start", self.ci, self.p], self.env)
        return run([self.script, "prove", self.w, self.ci / "brew-before.txt", self.ci, self.p], self.env, timeout=300)


def seen_then_exit(ci):
    """a stand-in brew that ends once the watch's record names it (at most 20 s)"""
    return (f'#!/bin/sh\ni=0\nwhile [ $i -lt 40 ]; do grep -qF "$0" "{ci}/brew-watch.txt" 2>/dev/null && exit 0; sleep 0.5; i=$((i + 1)); done\n'
            'exit 0\n')


def lines(out, start):
    return [l for l in out.splitlines() if l.startswith(start)]


def failures(out):
    """-> the FAIL and NOT CHECKED lines, each with the indented lines that follow it (what was found)"""
    blocks, cur = [], None
    for l in out.splitlines():
        if l.startswith(("FAIL", "NOT CHECKED")):
            cur = [l]; blocks.append(cur)
        elif cur is not None and l.startswith("        "):
            cur.append(l)
        else:
            cur = None
    return ["\n".join(b) for b in blocks]


def main():
    if not SCRIPT.is_file():
        print(f"FAIL {SCRIPT} is missing"); sys.exit(1)
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d).resolve()
        guard(tmp / "ci", os.getpid())   # before the first watch (the scene's job folder is tmp/ci)
        mk = Maker(tmp)
        try:
            sc = Scene(tmp, mk)
        except (RuntimeError, subprocess.CalledProcessError) as e:
            skip(f"cannot make the Mach-O files here: {str(e)[-300:]}")
        sc.before()
        rc, out = sc.prove()
        fails = lines(out, "FAIL") + lines(out, "NOT CHECKED")
        arm("a working directory that did everything right: every check ok, exit 0", rc == 0 and not fails and len(lines(out, "ok")) >= 12,
            "\n".join(failures(out)) or out)
        for want in ("Homebrew's", "unchanged", "files Homebrew keeps that changed during the build: 0", "homebrew-cache): absent",
                     "brew processes seen: 0", "every one inside the working directory", "/usr/bin/patch",
                     "Mach-O programs and libraries under", "Mach-O files of the working directory's tools", "mesa_clc",
                     "minos 12.0 x1, 15.0 x1", "carried over unchanged from the base package: 1", "build step logs that name",
                     "git status --porcelain prints nothing", "VERSION 3.0", "2 replaced, 1 added, 1 removed, 2 unchanged",
                     "--strict -c SHA256SUMS in the package"):
            arm(f"the ok lines say: {want}", want in out, out)
        arm("the object files of NVK's build tree are not counted as programs", "under " + str(sc.w) + "/out: 3," in out, out)

        def control(name, want, prepare, undo):
            prepare()
            try:
                rc, o = sc.prove()
            finally:
                undo()
            f = failures(o)
            arm("control: " + name, rc == 1 and any(want in b for b in f), o)

        w, p, tc = sc.w, sc.p, sc.w / "tools/toolchain"
        control("a formula Homebrew gained is caught", "Homebrew's", lambda: (p / "Cellar/llvm@22/22.1.8").mkdir(parents=True),
                lambda: shutil.rmtree(p / "Cellar/llvm@22"))
        control("a link Homebrew moved is caught", "changed during the build",
                lambda: ((p / "opt/cmake").unlink(), (p / "opt/cmake").symlink_to("../Cellar/cmake/4.4.0")),
                lambda: ((p / "opt/cmake").unlink(), (p / "opt/cmake").symlink_to("../Cellar/cmake/4.5.0")))
        log = sc.home / "Library/Logs/Homebrew/ninja/01.cmake"
        control("a log Homebrew wrote is caught", "files Homebrew keeps that changed", lambda: (log.parent.mkdir(), log.write_text("x")),
                lambda: shutil.rmtree(log.parent))
        api = sc.home / "Library/Caches/Homebrew/api/formula.jws.json"
        control("Homebrew's formula list fetched again is caught", "files Homebrew keeps that changed", lambda: api.write_text("{ }"), lambda: None)
        sc.before()   # a new reference time for the controls after this one
        control("build.sh's Homebrew cache folder is caught", "asked Homebrew", lambda: (w / "tools/homebrew-cache").mkdir(),
                lambda: (w / "tools/homebrew-cache").rmdir())

        def brew_runs():
            run([sc.script, "watch-start", sc.ci, sc.p], sc.env)
            subprocess.run(["/bin/sh", str(p / "bin/brew"), "info", "llvm"], timeout=10)
        control("a brew process is seen by the watch", "brew processes seen: 1", brew_runs, lambda: None)
        fake = tmp / "sandbox/bin/brew"
        fake.parent.mkdir(parents=True)
        fake.write_text(seen_then_exit(sc.ci))
        run([sc.script, "watch-start", sc.ci, sc.p], sc.env)
        subprocess.run(["/bin/sh", str(fake), "install", "x"], timeout=10)
        rc, o = sc.prove(watch=False)
        # one or more: the watch lists every process of the machine, and other tests of the suite run stand-in brews of
        # their own at the same time
        m = re.search(r"; (\d+) other process\(es\) named brew", o)
        arm("a brew of the tests' sandboxes is counted apart, not as Homebrew's", rc == 0 and m is not None and int(m.group(1)) >= 1, o)

        # Which process the watch takes for Homebrew's brew. The full build runs this tree's tests under its watch, and
        # the build script's test keeps its stand-in brews in <its folder>/usr/local/bin and <its folder>/opt/homebrew/bin;
        # on many Macs the Python that runs the tests is Homebrew's own. These arms use prefixes of their own (p2, p3):
        # prove counts files that appear under the scene's prefix as Homebrew's.
        p2, p3, sbx = tmp / "brew2", tmp / "brew3", tmp / "sandbox2"

        def lists():
            try:
                return int((sc.ci / "brew-watch.count").read_text().split()[0])
            except (OSError, ValueError, IndexError):
                return 0

        def named(argv0):
            """a process that the list of processes shows under the name argv0 (bash's exec -a), for five minutes at most"""
            q = subprocess.Popen([BASH, "-c", 'exec -a "$1" sleep 300', "x", str(argv0)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            end = time.time() + 30
            while time.time() < end:
                shown = subprocess.run(["ps", "-o", "command=", "-p", str(q.pid)], capture_output=True, text=True).stdout.strip()
                if shown.startswith(str(argv0)):
                    break
                time.sleep(0.1)
            return q

        def script_run(path, *args, opts=()):
            """a shell that runs the script at `path` (made here: it waits, two minutes at most) with these arguments"""
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("#!/bin/sh\ni=0\nwhile [ $i -lt 600 ]; do sleep 0.2; i=$((i + 1)); done\n")
            return subprocess.Popen([BASH, *opts, str(path), *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def watched(start, prefixes=(p2,), script=None):
            """-> watch-stop's exit status and output after a new watch has listed the processes `start` makes three times"""
            run([script or sc.script, "watch-start", sc.ci, *prefixes], sc.env)
            procs = []
            try:
                procs = start()
                n0, end = lists(), time.time() + 60
                while lists() < n0 + 3 and time.time() < end:
                    time.sleep(0.2)
                return run([sc.script, "watch-stop", sc.ci], sc.env)
            finally:
                for q in procs:
                    q.kill()
                    q.wait()

        def variant(name, *changes):
            """a copy of the script beside it (it finds the checkout from its own place) with these (old, new) changes"""
            v = sc.co / f"build/ci_full_build_{name}.sh"
            text = sc.script.read_text()
            for old_text, new_text in (('SELF="$HERE/build/ci_full_build.sh"', f'SELF="$HERE/build/ci_full_build_{name}.sh"'),) + changes:
                assert old_text in text, old_text
                text = text.replace(old_text, new_text)
            v.write_text(text)
            with open(sc.co / ".git/info/exclude", "a") as ex:
                ex.write(f"/build/ci_full_build_{name}.sh\n")
            return v

        ruby, brewsh = p2 / "Homebrew/Library/Homebrew/vendor/portable-ruby/current/bin/ruby", p2 / "Homebrew/Library/Homebrew/brew.sh"

        def brew_itself():
            return [script_run(p2 / "bin/brew", "info", "--json=v2", "llvm", opts=("-p",)), script_run(brewsh, "info", opts=("-p",)), named(ruby)]
        rc, o = watched(brew_itself)
        seen = lines(o, "homebrew ")
        arm("Homebrew's brew is seen as its command, as its brew.sh under a shell with an option, and as its own Ruby",
            rc == 1 and all(any(str(x) in l for l in seen) for x in (p2 / "bin/brew", brewsh, ruby)), o)
        only_first = variant("first", ("          if (b !~ /^(bash|sh|zsh|dash|ksh|env|ruby|nohup|nice|time|timeout|caffeinate)$/) break\n", "          break\n"))
        rc, o = watched(brew_itself, script=only_first)
        seen = lines(o, "homebrew ")
        arm("control: a watch that looks at a process's own name only misses brew under its shell",
            any(str(ruby) in l for l in seen) and not any(str(x) in l for l in seen for x in (p2 / "bin/brew", brewsh)), o)

        python = p2 / "Cellar/python@3.13/3.13.5/Frameworks/Python.framework/Versions/3.13/Resources/Python.app/Contents/MacOS/Python"
        sandbox_brew = pathlib.Path(str(sbx) + str(p2)) / "bin/brew"   # <a test's folder><the prefix>/bin/brew

        def not_brew():
            return [script_run(sandbox_brew, "install", "x"),
                    script_run(sbx / "probe", str(p2 / "bin/brew")),   # it names brew's path, as ls -l or test -x would
                    named(python), named(p2 / "bin/cmake")]
        rc, o = watched(not_brew)
        m = re.search(r"other processes named brew: (\d+)", o)
        arm("a stand-in brew in a folder whose path holds the prefix further in, a program that names brew's path, and Homebrew's Python "
            "and cmake are not Homebrew's brew", rc == 0 and "brew processes seen: 0 " in o and m is not None and int(m.group(1)) >= 2, o)
        anywhere = variant("anywhere", ("if (index(word, bp[k]) == 1) return 1", "if (index(c, bp[k])) return 1"))
        rc, o = watched(not_brew, script=anywhere)
        seen = lines(o, "homebrew ")
        arm("control: a watch that looks for the path anywhere in a command line takes the stand-in and the program that names it for brew",
            rc == 1 and any(str(sandbox_brew) in l for l in seen) and any(str(sbx / "probe") in l for l in seen), o)
        whole = variant("whole", ('bp[++np] = pre[k] "/bin/brew"; ', 'bp[++np] = pre[k] "/"; '))
        rc, o = watched(not_brew, script=whole)
        seen = lines(o, "homebrew ")
        arm("control: a watch that takes every program under the prefix for brew counts Homebrew's Python and cmake",
            rc == 1 and any(str(python) in l for l in seen) and any(str(p2 / "bin/cmake") in l for l in seen), o)
        for v in (only_first, anywhere, whole):
            v.unlink()

        ruby3 = p3 / "Library/Homebrew/vendor/portable-ruby/current/bin/ruby"   # a Homebrew that is its own repository, as /opt/homebrew
        rc, o = watched(lambda: [named(ruby3)], prefixes=(p2, p3))
        arm("a second prefix is watched as well", rc == 1 and any(str(ruby3) in l for l in lines(o, "homebrew ")), o)
        rc, o = watched(lambda: [named(ruby3)])
        arm("control: a prefix that was not given is not watched", rc == 0 and "brew processes seen: 0 " in o, o)
        run([sc.script, "watch-start", sc.ci], sc.env)
        wpid = (sc.ci / "brew-watch.pid").read_text().split()[0]
        shown = subprocess.run(["ps", "-o", "command=", "-p", wpid], capture_output=True, text=True).stdout.strip()
        run([sc.script, "watch-stop", sc.ci], sc.env)
        arm("with no prefix given the watch looks in Homebrew's two places", shown.endswith(f"watch-loop {sc.ci} /usr/local /opt/homebrew"), shown)
        for bad, why in (("usr/local", "an absolute path"), (str(tmp / "a b"), "a blank")):
            rc, o = run([sc.script, "watch-start", sc.ci, bad], sc.env)
            arm(f"watch-start refuses the prefix '{bad}'", rc == 2 and why in o and not (sc.ci / "brew-watch.pid").exists(), o)

        # a watch whose starter was killed must not list processes for ever
        run([sc.script, "watch-start", sc.ci, p2], dict(sc.env, NMCI_WATCH_MAX="2"))
        wpid = (sc.ci / "brew-watch.pid").read_text().split()[0]
        end = time.time() + 30
        while time.time() < end and subprocess.run(["kill", "-0", wpid], capture_output=True).returncode == 0:
            time.sleep(0.5)
        gone = subprocess.run(["kill", "-0", wpid], capture_output=True).returncode != 0
        rc, o = run([sc.script, "watch-stop", sc.ci], sc.env)
        arm("a watch ends by itself after NMCI_WATCH_MAX seconds, and is then not taken for none seen",
            gone and rc == 1 and "the watch was not running at the end" in o, o)
        run([sc.script, "watch-start", sc.ci, p2], sc.env)
        wpid = (sc.ci / "brew-watch.pid").read_text().split()[0]
        time.sleep(5)
        still = subprocess.run(["kill", "-0", wpid], capture_output=True).returncode == 0
        rc, o = run([sc.script, "watch-stop", sc.ci], sc.env)
        arm("control: with a larger limit it is still running after five seconds", still and rc == 0, o)
        rc, o = sc.prove(watch=False)
        arm("control: a watch that is not running is not taken for none seen", rc == 1 and any("the watch was not running" in l for l in lines(o, "NOT CHECKED")), o)

        # the watch must outlive the step that starts it: a step's process group killed when the step ends leaves it running
        def start_as_step(script):
            st = subprocess.Popen([BASH, str(script), "watch-start", sc.ci, sc.p], env=sc.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  start_new_session=True)
            st.communicate(timeout=60)
            try:
                os.killpg(st.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            time.sleep(1.5)
        start_as_step(sc.script)
        wpid = int((sc.ci / "brew-watch.pid").read_text().split()[0])
        alive = subprocess.run(["kill", "-0", str(wpid)], capture_output=True).returncode == 0
        rc, o = sc.prove(watch=False)
        arm("the watch runs in a session of its own and outlives the step that started it (its process group killed)",
            alive and rc == 0 and "brew processes seen: 0" in o, o)
        nosid = sc.co / "build/ci_full_build_nosid.sh"   # beside the script, which finds the checkout from its own place
        with open(sc.co / ".git/info/exclude", "a") as ex:
            ex.write("/build/ci_full_build_nosid.sh\n")
        nosid.write_text(sc.script.read_text().replace("if [ -x /usr/bin/perl ]; then", "if [ -x /nonexistent/perl ]; then"))
        start_as_step(nosid)
        rc, o = sc.prove(watch=False)
        nosid.unlink()
        arm("control: a watch started in the step's own process group dies with it and is not taken for none seen",
            rc == 1 and any("the watch was not running" in l for l in lines(o, "NOT CHECKED")), o)
        def stall(resume):
            run([sc.script, "watch-start", sc.ci, sc.p], sc.env)
            wp = int((sc.ci / "brew-watch.pid").read_text().split()[0])
            os.kill(wp, signal.SIGSTOP)
            time.sleep(12)
            if resume:
                os.kill(wp, signal.SIGCONT)
                time.sleep(1.5)
            r = sc.prove(watch=False)
            try:
                os.kill(wp, signal.SIGCONT)   # a stopped watch gets prove's SIGTERM only now
            except ProcessLookupError:
                pass
            return r
        rc, o = stall(True)
        arm("control: a watch that made no list for 12 s is not taken for none seen", rc == 1 and any("made no list for" in l for l in lines(o, "NOT CHECKED")), o)
        rc, o = stall(False)
        arm("control: a watch that stopped listing before the end is not taken for none seen",
            rc == 1 and any("last list is" in l for l in lines(o, "NOT CHECKED")), o)

        control("a tool taken from Homebrew is caught", "lie outside the working directory",
                lambda: ((w / "tools/bin/cmake").unlink(), (w / "tools/bin/cmake").symlink_to(p / "bin/cmake")),
                lambda: ((w / "tools/bin/cmake").unlink(), (w / "tools/bin/cmake").symlink_to(tc / "bin/cmake")))
        pt = w / "tools/bin/patch"
        good_patch = pt.read_text()
        control("a patch from Homebrew is caught", "lie outside the working directory",
                lambda: pt.write_text(good_patch.replace("/usr/bin/patch", "/usr/local/opt/gpatch/bin/gpatch")), lambda: pt.write_text(good_patch))
        bad = w / "out/vulkan-loader/libvulkan.1.dylib"
        control("a library under out/ that loads from Homebrew's prefix is caught", "loads /usr/local/lib/libintl.8.dylib",
                lambda: mk.make(bad, "dylib", ("/usr/lib/libSystem.B.dylib", "/usr/local/lib/libintl.8.dylib")), lambda: bad.unlink())
        control("a search path into Homebrew is caught", "rpath /opt/homebrew/lib",
                lambda: mk.make(bad, "dylib", ("/usr/lib/libSystem.B.dylib",), rpaths=("/opt/homebrew/lib",)), lambda: bad.unlink())
        cm = tc / "bin/cmake"
        control("a tool of the toolchain that loads from Homebrew is caught", "loads /usr/local/opt/curl/lib/libcurl.4.dylib",
                lambda: mk.make(cm, "bundle", ("/usr/lib/libSystem.B.dylib", "/usr/local/opt/curl/lib/libcurl.4.dylib")),
                lambda: mk.make(cm, "bundle", ("/usr/lib/libSystem.B.dylib", "/usr/lib/libc++.1.dylib")))
        pk = w / "tools/toolchain/bin/pkgconf"
        control("a tool the toolchain should have and has not is caught", "bin/pkgconf", lambda: pk.rename(pk.with_suffix(".x")), lambda: pk.with_suffix(".x").rename(pk))
        gl = w / "tools/bin/glslangValidator"
        control("a tool missing from tools/bin is caught", "glslangValidator: not in tools/bin", gl.unlink, lambda: gl.symlink_to(tc / "bin/glslangValidator"))
        vp = w / "tools/venv/bin/python3"
        control("a Python environment made from Homebrew's Python is caught", "python3 of the Python environment",
                lambda: (vp.unlink(), vp.symlink_to(p / "opt/python@3.14/bin/python3.14")), lambda: (vp.unlink(), vp.symlink_to(tc / "python/bin/python3.14")))
        ll = tc / "llvm/lib/libLLVM.dylib"
        hb_llvm = tmp / "elsewhere/llvm/lib/libLLVM.dylib"   # not in Homebrew's own folders, whose changes another line counts
        control("a tool of the toolchain that is a link into Homebrew is caught", "outside the working directory",
                lambda: (mk.make(hb_llvm, "dylib", ("/usr/lib/libSystem.B.dylib",)), ll.unlink(), ll.symlink_to(hb_llvm)),
                lambda: (ll.unlink(), ll.symlink_to("libLLVM-22.dylib")))
        bg = w / "tools/cargo/bin/bindgen"
        control("a tool that searches Homebrew's lib folder (LC_RPATH) is caught", "rpath /usr/local/lib",
                lambda: mk.make(bg, "bundle", ("/usr/lib/libSystem.B.dylib",), rpaths=("/usr/local/lib",)),
                lambda: mk.make(bg, "bundle", ("/usr/lib/libSystem.B.dylib",), rpaths=("@loader_path/../lib",)))
        r = w / "package/pkgroot"
        tbz = w / "package/nullmoth-nvidia-local-abc1234.tar.gz"
        control("a staged package without its tarball is caught", "no tarball", lambda: tbz.rename(tmp / "tb.away"), lambda: (tmp / "tb.away").rename(tbz))
        control("a package without VERSION is caught", "has no VERSION", lambda: ((r / "VERSION").unlink(), write_sums(r)),
                lambda: ((r / "VERSION").write_text("3.0\n"), write_sums(r)))
        new = r / "Library/NullMoth/dlss/nm_unzstd"
        control("a shipped file built for macOS 26 is caught", "minos 26.0",
                lambda: (mk.make(new, "bundle", minos="26.0"), write_sums(r)), lambda: (mk.make(new, "bundle", minos="12.0"), write_sums(r)))
        control("a damaged package is caught", "--strict -c SHA256SUMS in the package fails", lambda: (r / "VERSION").write_text("3.1\n"),
                lambda: (r / "VERSION").write_text("3.0\n"))
        control("a changed checkout is caught", "git status --porcelain prints 1", lambda: (sc.co / "build/pins.sh").write_text("NM_PIN_X=2\n"),
                lambda: (sc.co / "build/pins.sh").write_text("NM_PIN_X=1\n"))
        nl = w / "logs/nvk.log"
        good_log = nl.read_text()
        control("a step log that names Homebrew's prefix is caught", "build step logs that name",
                lambda: nl.write_text(good_log + f"Program bison found: YES ({p}/opt/bison/bin/bison)\n"), lambda: nl.write_text(good_log))
        hold = tmp / "pkg-away"
        control("a run without a package is not taken for a good one", "no staged package", lambda: (w / "package").rename(hold), lambda: hold.rename(w / "package"))
        rc, out = sc.prove()
        arm("after the controls the scene is good again (each control undid its change)", rc == 0, "\n".join(failures(out)) or out)

        # toolchain-id
        rc1, a = run([sc.script, "toolchain-id", w], sc.env)
        (tc / ".nm-toolchain/llvm").write_text("ca7933e4 llvm recipe 2 Darwin-x86_64 macos15.0\n")
        rc2, b = run([sc.script, "toolchain-id", w], sc.env)
        rc3, c = run([sc.script, "toolchain-id", tmp / "nowhere"], sc.env)
        arm("toolchain-id changes when a tool is finished, and is empty without a toolchain",
            rc1 == rc2 == rc3 == 0 and len(a.strip()) == 16 and len(b.strip()) == 16 and a != b and c == "", (a, b, c))

        # the folders of the working directory the job and these checks name are build.sh's own (derive_paths, tc_path,
        # make_tool_links), and the workflow's caches name the same
        bsh = (ROOT / "build.sh").read_text()
        fbw = (ROOT / ".github/workflows/full-build.yml").read_text()
        own = (ROOT / "build/ci_full_build.sh").read_text()
        names = ['  SRC=$W/src\n', '  TOOLS=$W/tools\n', '  LOGS=$W/logs\n', '  GITDIR=$SRC/git\n',
                 '  TC_PREFIX=$TOOLS/toolchain; TC_WORK=$TOOLS/toolchain-build; TC_LOGS=$LOGS/toolchain; TC_SRC=$SRC/toolchain\n',
                 'tc_path() { [ -s "$TC_PREFIX/.nm-toolchain/$1" ]', '  VENV=$TOOLS/venv\n', '  RUSTUP_DIR=$TOOLS/rustup\n',
                 '  CARGO_DIR=$TOOLS/cargo\n', '  SPIRVLLVM_PREFIX=$TOOLS/spirv-llvm-translator\n', '  TBIN=$TOOLS/bin\n']
        gone = [n.strip() for n in names if n not in bsh]
        links = set(re.findall(r'ln -s "\$\w+(?:/\$l)?" "\$TBIN/([\w-]+)"', bsh)) | {"patch"}
        loops = re.findall(r'for l in ([\w ]+); do \[ -x "\$(?:RUST_BIN|CARGO_DIR/bin)/\$l" \]', bsh)
        links |= {x for l in loops for x in l.split()} - {"rustdoc"}
        want_links = set(re.search(r'^TOOL_LINKS="([^"]*)"', own, re.M).group(1).split())
        arm("the folders the job names are build.sh's: the toolchain in tools/toolchain with its stamps in .nm-toolchain, its logs in "
            "logs/toolchain, its sources in src/toolchain, the rest of deps in tools/venv, rustup, cargo, spirv-llvm-translator",
            not gone, gone)
        arm("the links prove expects in tools/bin are the ones build.sh makes (rustdoc aside: no step needs it, it is linked when Rust has it)",
            want_links == links, (sorted(want_links), sorted(links)))
        arm("the scripts' toolchain folders are those", "tc_prefix() { printf '%s/tools/toolchain' \"$1\"; }" in own
            and "tc_logs() { printf '%s/logs/toolchain' \"$1\"; }" in own, "")
        cache = {"${{ runner.temp }}/nmw/tools/toolchain", "!${{ runner.temp }}/nmw/src/toolchain", "${{ runner.temp }}/nmw/tools/venv",
                 "${{ runner.temp }}/nmw/tools/rustup", "${{ runner.temp }}/nmw/tools/cargo", "${{ runner.temp }}/nmw/tools/spirv-llvm-translator",
                 "${{ runner.temp }}/nmw/src"}
        arm("the workflow's caches name those folders", all(f"            {c}\n" in fbw for c in cache) and 'NMCI_WORK=$RUNNER_TEMP/nmw' in fbw,
            sorted(c for c in cache if f"            {c}\n" not in fbw))
        # deps-plan: the lines build.sh prints before it calls Homebrew
        arm("build.sh still prints the lines deps-plan looks for before a Homebrew install",
            "Homebrew formulas to install (only these;" in bsh and '$BREW install $f"; done' in bsh, "")
        # the lines of build.sh's own dry run on an imitated Intel Mac whose Homebrew has every formula (tools/test_build_sh.py's
        # nobrew_sandbox), shortened: the sentences that name Homebrew without calling it must not be taken for a call
        src_line = ("missing build tools are built by ./build.sh deps from pinned sources into /w/tools/toolchain, without Homebrew "
                    "(NM_TOOLS_FROM=auto on an Intel Mac: Homebrew has no ready-made builds for Intel Macs).\n")
        rest = ("deps builds from pinned sources: ninja cmake openssl zstd xz python pkgconf spirv-tools glslang llvm\n"
                "  Nothing is installed outside the working directory and Homebrew is not used.\n"
                "  note: NVK's patches need a patch that also creates files: put GNU patch on PATH as gpatch (Homebrew's formula is gpatch); deps does not build patch\n"
                "    env NM_TC_PREFIX=/w/tools/toolchain bash build/toolchain.sh build llvm\n"
                "SPIRV-LLVM-Translator v22.1.8 for LLVM 22: Homebrew has none that matches; it is built from source into /w/tools/spirv-llvm-translator (a few minutes)\n"
                "deps: dry run; the commands above would run, nothing was built or installed\n")
        plan = tmp / "plan.txt"
        plan.write_text(src_line + "Homebrew formulas to install (only these; nothing of yours is upgraded or removed by build.sh): ninja llvm@22\n"
                        "    env -u HOMEBREW_PREFIX PATH=/usr/local/bin:/usr/bin HOMEBREW_NO_AUTO_UPDATE=1 /usr/local/bin/brew install ninja\n")
        rc, o = run([sc.script, "deps-plan", plan], sc.env)
        arm("control: a dry run of deps that would install formulas is refused", rc == 1 and "would call Homebrew" in o, o)
        plan.write_text(src_line + "    /usr/local/bin/brew unlink <formula>   (with the same environment; only if ...)\n")
        rc, o = run([sc.script, "deps-plan", plan], sc.env)
        arm("control: a dry run of deps that would unlink a formula is refused", rc == 1 and "would call Homebrew" in o, o)
        arm("build.sh still says the sentence deps-plan wants from the source way",
            "auto:source) printf 'missing build tools are built by ./build.sh deps from pinned sources into %s, without Homebrew (NM_TOOLS_FROM=auto on an Intel Mac" in bsh, "")
        plan.write_text("(dry run: nothing is changed; the commands are printed)\n" + src_line + rest)
        rc, o = run([sc.script, "deps-plan", plan], sc.env)
        arm("a dry run of deps that builds the tools from source passes", rc == 0 and "names no Homebrew command" in o, o)
        # NM_TOOLS_FROM=homebrew on a Mac whose Homebrew has every formula: nothing to install, and still not the default way
        plan.write_text("(dry run: nothing is changed; the commands are printed)\n" + rest.replace("deps builds from pinned sources: ninja cmake openssl zstd xz python pkgconf spirv-tools glslang llvm\n", ""))
        rc, o = run([sc.script, "deps-plan", plan], sc.env)
        arm("control: the Homebrew way that would install nothing is refused too", rc == 1 and "does not say that the tools are built from pinned sources" in o, o)

        # basepkg, against a pins.sh of this test's own (it never reaches the network: the URL is a closed local port)
        blob = tmp / "dl/nullmoth-nvidia-2.9.tar.gz"
        blob.parent.mkdir()
        blob.write_bytes(b"not a package, but the pinned bytes\n")
        pins = sc.co / "build/pins.sh"
        pins.write_text("NM_PIN_X=1\nNM_PIN_CI_BASEPKG_URL=https://127.0.0.1:9/v2.9/nullmoth-nvidia-2.9.tar.gz\n"
                        f"NM_PIN_CI_BASEPKG_SIZE={blob.stat().st_size}\nNM_PIN_CI_BASEPKG_SHA256={sha(blob)}\n")
        r = subprocess.run([BASH, str(sc.script), "basepkg", str(blob.parent)], capture_output=True, text=True, env=sc.env, timeout=60)
        arm("basepkg takes a file that has the pinned size and SHA-256, and prints only its path", r.returncode == 0 and r.stdout == f"{blob}\n", (r.stdout, r.stderr))
        blob.write_bytes(b"not the pinned bytes!!!!!!!!!!!!!!\n")
        r = subprocess.run([BASH, str(sc.script), "basepkg", str(blob.parent)], capture_output=True, text=True, env=sc.env, timeout=60)
        arm("control: a file that is not the pinned one is removed and fetched again (here: the download fails, exit 2)",
            r.returncode == 2 and r.stdout == "" and not blob.exists() and "could not download" in r.stderr, (r.returncode, r.stdout, r.stderr))
        pins.write_text("NM_PIN_X=1\n")
        r = subprocess.run([BASH, str(sc.script), "basepkg", str(blob.parent)], capture_output=True, text=True, env=sc.env, timeout=60)
        arm("control: no pin, no download", r.returncode == 2 and "NM_PIN_CI_BASEPKG_URL" in r.stderr, r.stderr)
        real = (ROOT / "build/pins.sh").read_text()
        arm("build/pins.sh pins the base package's URL, size and SHA-256",
            "NM_PIN_CI_BASEPKG_URL=https://github.com/nullmoth/nvidia-macos-driver/releases/download/v2.9/nullmoth-nvidia-2.9.tar.gz\n" in real
            and "NM_PIN_CI_BASEPKG_SIZE=68833656\n" in real
            and "NM_PIN_CI_BASEPKG_SHA256=b624bf07c78cea03043e096383d5c8b01e4fb2c804a6ba377a4e66a2566422ef\n" in real, "")
        pins.write_text("NM_PIN_X=1\n")

        # logs: build.sh's step logs in logs/, the toolchain's in logs/toolchain/
        (w / "logs/toolchain").mkdir()
        (w / "logs/toolchain/llvm.log").write_text("".join(f"line {i}\n" for i in range(400)) + "ld: error: undefined symbol: _x\n")
        (w / "reports").mkdir(exist_ok=True)
        (w / "reports/failure-nvk-20261113T000000Z.txt").write_text("NullMoth driver build: failure report\nstep: nvk\n")
        (w / "logs/fetch.log").write_text("STOP: could not fetch x\n")
        rc, o = run([sc.script, "logs", w], sc.env)
        arm("logs shows the end of the newest logs (300 lines), the newest report, and every error: and STOP: line",
            rc == 0 and "\nline 399\n" in o and "\nline 101\n" in o and "\nline 100\n" not in o and "failure report" in o
            and "ld: error: undefined symbol" in o and "STOP: could not fetch x" in o, o[-2000:])
        # the toolchain's newest log is shown also when three step logs are newer
        for n in ("a", "b", "c"):
            (w / f"logs/{n}.log").write_text(n + "\n")
        rc, o = run([sc.script, "logs", w], sc.env)
        arm("logs shows the toolchain's newest log (logs/toolchain/) beside three newer step logs",
            rc == 0 and f"==== {w}/logs/toolchain/llvm.log (last 300" in o, o[:1500])
        # reads: every label printed, exit 0 although many reads cannot be made here (no Mac programs, Mach-O files that do
        # not run), nothing left behind in the working directory
        # reads starts Apple's /usr/bin/make with an empty developer folder: what that small program of Apple's does
        # there is the record GitHub's job wants, and on a tester's Mac it may be Apple's dialog that offers to install
        # the command line tools. This test needs only that the read is made and labelled, so it runs a copy of the
        # script whose make is a stand-in that notes its call (the only change)
        make_read = 'env -i DEVELOPER_DIR="$scratch/empty-developer-dir" /usr/bin/make --version'
        own = sc.script.read_text()
        others = [l.strip() for l in own.splitlines() if "/usr/bin/make" in l and make_read not in l and not l.lstrip().startswith('rhead "')]
        arm("reads starts Apple's make in one line only, the one this test hands a stand-in", own.count(make_read) == 1 and not others, others)
        smake = tmp / "standin-make/make"
        smake.parent.mkdir()
        smake.write_text(f'#!/bin/sh\necho "DEVELOPER_DIR=$DEVELOPER_DIR $*" >> {shlex.quote(str(smake))}.calls\necho "GNU Make (the test\'s stand-in)"\n')
        smake.chmod(0o755)
        reads_script = sc.co / "build/ci_full_build_reads.sh"   # beside the script, which finds the checkout from its own place
        with open(sc.co / ".git/info/exclude", "a") as ex:
            ex.write("/build/ci_full_build_reads.sh\n")
        reads_script.write_text(own.replace(make_read, make_read.replace("/usr/bin/make", shlex.quote(str(smake)))))
        before_w = sorted(str(x) for x in w.rglob("*"))
        rc, o = run([reads_script, "reads", w], sc.env, timeout=300)
        calls = (tmp / "standin-make/make.calls").read_text() if (tmp / "standin-make/make.calls").exists() else ""
        arm("reads ran the stand-in make, with only the empty developer folder as its environment, and not Apple's",
            calls == f"DEVELOPER_DIR={w}/reads-scratch/empty-developer-dir --version\n" and "GNU Make (the test's stand-in)" in o, (calls, o[-800:]))
        labels = ("otool -L <toolchain>/bin/cmake", "ssl.OPENSSL_VERSION", "llvm-config --targets-built", "glslangValidator --version",
                  "--variable pc_path pkg-config", "--cflags --libs SPIRV-Tools", "ls -l <workdir>/tools/bin", "mesa_clc",
                  "libvulkan_nouveau.dylib", "pyvenv.cfg", "LC_BUILD_VERSION", "DEVELOPER_DIR=<an empty folder> /usr/bin/make --version",
                  "xcode-select -p", "tar --version", "the tarball: size and SHA-256", "owner/group of every header", "AppleDouble",
                  "LIBARCHIVE.xattr", "the account name", "text files of pkgroot that hold the home folder", "---- VERSION",
                  "NULLMOTH-LOCAL-BUILD.txt", "links in pkgroot", "set-uid and set-gid", "codesign -dv", "xattr -lr pkgroot",
                  "the home folder and the working directory in the compiled files")
        heads = [l for l in o.splitlines() if l.startswith("---- ")]
        miss = [x for x in labels if not any(x in h for h in heads)]
        arm("reads prints every label, says what it could not read, and exits 0", rc == 0 and not miss and o.count("could not be read: ") >= 3
            and "== end of the reads" in o, (rc, miss, o[-1500:]))
        arm("reads measures the home folder in the compiled files (the stand-in mesa_clc names the working directory under it)",
            "<workdir>/out/nvk-build/src/compiler/clc/mesa_clc" in o, o[-1500:])
        arm("reads writes nothing it leaves in the working directory", sorted(str(x) for x in w.rglob("*")) == before_w, "")
        rc, o = run([reads_script, "reads", tmp / "nowhere"], sc.env, timeout=120)
        arm("reads of a working directory that is not there: every read says why, exit 0", rc == 0 and o.count("could not be read: ") >= 15, o[-1500:])
        reads_script.unlink()

        # the script's git reads none of the tester's configuration: a core.fsmonitor in $XDG_CONFIG_HOME/git/config
        # names a program that git status starts (prove's check of the checkout runs git status)
        xdg = tmp / "xdg"
        fsmon = tmp / "fsmonitor"
        (xdg / "git").mkdir(parents=True)
        fsmon.write_text(f'#!/bin/sh\necho "$*" >> {shlex.quote(str(fsmon))}.calls\n')
        fsmon.chmod(0o755)
        (xdg / "git/config").write_text(f"[core]\n\tfsmonitor = {fsmon}\n")
        run([sc.script, "watch-start", sc.ci, sc.p], sc.env)
        rc_x, o_x = run([sc.script, "prove", w, sc.ci / "brew-before.txt", sc.ci, sc.p], dict(sc.env, XDG_CONFIG_HOME=str(xdg)), timeout=300)
        ran = fsmon.with_name("fsmonitor.calls").exists()
        subprocess.run(["git", "-C", str(sc.co), "status", "--porcelain"], capture_output=True,
                       env=dict({k: v for k, v in sc.env.items() if not k.startswith("GIT_")}, XDG_CONFIG_HOME=str(xdg)))
        arm("prove's git reads no git configuration of the tester's (a program that $XDG_CONFIG_HOME/git/config names for "
            "core.fsmonitor is not started); control: the same git status with the tester's configuration starts it",
            rc_x == 0 and not ran and fsmon.with_name("fsmonitor.calls").exists(), (rc_x, ran, o_x[-600:]))

        # the guard (GUARD): a test that is killed leaves no watch running, also not one a stall control stopped
        ci2 = tmp / "ci-guarded"
        stands_for_test = subprocess.Popen(["sleep", "120"])
        g = guard(ci2, stands_for_test.pid)
        run([sc.script, "watch-start", ci2, sc.p], sc.env)
        time.sleep(2.5)   # the guard reads the pid file once a second
        started = watches(ci2)
        for pid, _ in started:
            os.kill(pid, signal.SIGSTOP)
        stands_for_test.kill()   # not reaped until the guard is done: a killed test whose parent has not waited for it yet
        try:
            g.wait(timeout=15)
        except subprocess.TimeoutExpired:
            g.kill()
        stands_for_test.wait()
        time.sleep(1)
        left = watches(ci2)
        arm("a killed test leaves no watch running: once its process is gone (also while it is an unreaped zombie), its guard ends every watch it started, also a stopped one",
            len(started) == 1 and not left and not (ci2 / "brew-watch.pid").exists(), (started, left))
        for pid, _ in left:   # what the arm found, so that this test itself leaves nothing
            for sg in (signal.SIGCONT, signal.SIGKILL):
                try:
                    os.kill(pid, sg)
                except ProcessLookupError:
                    pass
        # nothing of the watch is left running
        left = subprocess.run(["ps", "-axo", "command="], capture_output=True, text=True).stdout
        arm("no watch is left running", str(sc.ci) not in left, "")
    print(f"{len(FAILS)} failed")
    sys.exit(1 if FAILS else 0)


main()
