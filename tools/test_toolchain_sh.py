#!/usr/bin/env python3
"""build/toolchain.sh, the recipes that build NVK's tools from source, run for real against stand-in source trees.

The real builds take hours and need a Mac; here every source tree is a stand-in whose build scripts (configure.py,
bootstrap, Configure, configure, the CMake and meson projects) record how they were called (arguments, folder,
environment) in a journal and leave behind the files the real install would. The programs they "install" (ninja,
cmake, python3.14, llvm-config, ...) are small scripts that answer --version as the real ones do. A Mac is imitated by
stand-ins of uname, xcrun, security, otool and sysctl at the head of the caller's PATH, so the macOS-only steps run
here too; one arm imitates Linux instead.

Arms, each with a control that must fail for the reason the arm names (a scratch copy of the script broken on purpose,
or the arm's own check given a broken result):
  list/order/layout  list names the ten tools in build order; build builds exactly the names given, in that order,
                     and leaves the layout build.sh looks in, with the stamps
  poison             a caller environment with CC, CFLAGS, LDFLAGS, CPATH, LIBRARY_PATH, PKG_CONFIG_PATH, PYTHONPATH,
                     HOMEBREW_PREFIX, SDKROOT, a PATH that starts with a foreign folder and a HOME with pip.conf reaches
                     no build step; DEVELOPER_DIR does; the SDK is xcrun's
  outside            nothing is written outside the four folders (sources included: never written to)
  fail               a failing step: exit status 1, its last lines and its log named, no stamp, nothing after it built
  check              a missing layout file, a foreign library and an @rpath library in otool -L each refuse the stamp
  alone              building one tool again leaves the others' files and stamps alone and removes its own old files
  id                 a raised recipe number is visible in id, for that tool only
  env                a missing setting stops with one line naming it
  linux              on Linux no xcrun, security or otool runs and OpenSSL gets no darwin target; on Apple silicon
                     OpenSSL gets the arm64 target
  bash32             nothing in the script that /bin/bash 3.2 or the BSD tools of macOS lack

The shell is $BUILD_SH_BASH (default /bin/bash, macOS's bash 3.2 on a Mac). usage: test_toolchain_sh.py [arm ...]"""
import hashlib, os, pathlib, re, shutil, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "build" / "toolchain.sh"
BASH = os.environ.get("BUILD_SH_BASH") or "/bin/bash"
TOOLS = ["ninja", "cmake", "openssl", "zstd", "xz", "python", "pkgconf", "spirv-tools", "glslang", "llvm"]
ONLY = sys.argv[1:]
fails = 0


def check(name, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + name + (f"  [{str(detail)[:1200]}]" if detail and not ok else ""), flush=True)
    fails += 0 if ok else 1
    return ok


def control(name, problems):
    """the arm's check, run on a broken script or a broken result, must find something"""
    return check(f"control: {name}", bool(problems), "the check found nothing wrong")


# ------------------------------------------------------------------------------------------------ stand-ins
STEP_SH = r'''#!/bin/sh
# stand-in of every build step: records the call, writes where a real build writes "to the home folder" and to the
# temporary folder, fails when told to, and leaves what the real step would
SB='@SB@'
who=$1; shift
{
  printf '@@ %s\n' "$who"; printf 'C %s\n' "$(pwd)"
  for a in "$@"; do printf 'A %s\n' "$a"; done
  env | LC_ALL=C sort | sed 's/^/E /'
  printf '@@end\n'
} >> "$SB/journal"
if [ -n "${HOME:-}" ]; then mkdir -p "$HOME/.cache" && : > "$HOME/.cache/standin-$$"; fi
if [ -n "${TMPDIR:-}" ]; then : > "$TMPDIR/standin-$$"; fi
if [ -f "$SB/control/fail-$who" ]; then echo "STANDIN-FAILURE-MARKER in $who"; exit 3; fi
argval() { p=$1; shift; for a in "$@"; do case "$a" in "$p"*) printf '%s' "${a#"$p"}"; return 0 ;; esac; done; return 1; }
mkninja() {   # <file> <tool> <prefix>: a build.ninja the stand-in ninja, cmake and meson run with sh
  mkdir -p "$(dirname "$1")"
  printf '#!/bin/sh\ncase "${1:-all}" in install) exec /bin/sh %s/lib/install.sh %s %s ;; esac\n' "$SB" "$2" "$3" > "$1"
}
mkmake() {   # <tool> <prefix> <install target>
  printf 'all:\n\t@true\n%s:\n\t/bin/sh %s/lib/install.sh %s %s\n' "$3" "$SB" "$1" "$2" > Makefile
}
case "$who" in
  ninja-configure) cp "$SB/templates/ninja" ./ninja; chmod 755 ./ninja ;;
  cmake-bootstrap) mkninja ./build.ninja cmake "$(argval --prefix= "$@")" ;;
  cmake-configure:*)
    bld=""; nx=""; for a in "$@"; do [ "$nx" = B ] && bld=$a; nx=""; [ "$a" = -B ] && nx=B; done
    mkninja "$bld/build.ninja" "${who#cmake-configure:}" "$(argval -DCMAKE_INSTALL_PREFIX= "$@")" ;;
  meson-setup) mkninja "$1/build.ninja" pkgconf "$(argval --prefix= "$@")" ;;
  openssl-configure) mkmake openssl "$(argval --prefix= "$@")" install_sw ;;
  python-configure) mkmake python "$(argval --prefix= "$@")" install ;;
esac
exit 0
'''

INSTALL_SH = r'''#!/bin/sh
# stand-in of an install: the files the real one leaves, minus those control/omit names ("<tool>:<relative path>")
SB='@SB@'; t=$1; p=$2
omit() { [ -f "$SB/control/omit" ] && grep -qx "$t:$1" "$SB/control/omit"; }
mk() { omit "$1" && return 0; mkdir -p "$(dirname "$p/$1")"; printf '%s\n' "${2:-standin $t}" > "$p/$1"; }
macho() { omit "$1" && return 0; mkdir -p "$(dirname "$p/$1")"; printf '\317\372\355\376 stand-in Mach-O\n' > "$p/$1"; }
prog() { omit "$1" && return 0; mkdir -p "$(dirname "$p/$1")"
         printf '#!/bin/sh\ncase "${1:-}" in --version|-version) echo %s ;; *) exit 1 ;; esac\n' "'$2'" > "$p/$1"; chmod 755 "$p/$1"; }
tmpl() { omit "$1" && return 0; mkdir -p "$(dirname "$p/$1")"
         sed -e "s|@P@|${3:-}|g" "$SB/templates/$2" > "$p/$1"; chmod 755 "$p/$1"; }
case "$t" in
  cmake) tmpl bin/cmake cmake; prog bin/ctest "ctest version 4.4.5"; prog bin/cpack "cpack version 4.4.5"
         mk share/cmake-4.4/Modules/FindStandin.cmake ;;
  openssl) mk lib/libssl.a; mk lib/libcrypto.a; mk include/openssl/ssl.h ;;
  zstd) mk lib/libzstd.a; mk include/zstd.h ;;
  xz) mk lib/liblzma.a; mk include/lzma.h ;;
  python) tmpl bin/python3.14 python3.14 "${p%/python}"; mk lib/python3.14/os.py
          macho lib/python3.14/lib-dynload/_ssl.cpython-314-darwin.so ;;
  pkgconf) prog bin/pkgconf 2.5.1; mk lib/libpkgconf.a ;;
  spirv-headers) mk include/spirv/unified1/spirv.h ;;
  spirv-tools) for x in spirv-as spirv-dis spirv-val spirv-opt spirv-link; do prog "bin/$x" "SPIRV-Tools v2025.5 standin"; done
               mk lib/pkgconfig/SPIRV-Tools.pc "Version: 2025.5"; mk include/spirv-tools/libspirv.h
               macho lib/libSPIRV-Tools-shared.dylib ;;
  glslang) prog bin/glslang "Glslang Version: 11:16.6.0"
           omit bin/glslangValidator || ln -s glslang "$p/bin/glslangValidator"
           [ ! -f "$SB/control/glslang-extra" ] || mk "share/glslang-standin-$(cat "$SB/control/glslang-extra")" ;;
  llvm) tmpl bin/llvm-config llvm-config; prog bin/clang "clang version 22.1.8"
        for l in libLLVM libclang-cpp libclang; do macho "lib/$l.dylib"; mk "lib/$l.so"; done
        mk lib/cmake/llvm/LLVMConfig.cmake; mk lib/cmake/clang/ClangConfig.cmake
        mk lib/clang/22/include/opencl-c.h; mk lib/clang/22/include/opencl-c-base.h ;;
esac
'''

TEMPLATES = {
    "ninja": r'''#!/bin/sh
case "${1:-}" in --version) echo 1.13.2; exit 0 ;; esac
dir=.; target=all
while [ $# -gt 0 ]; do case "$1" in -C) dir=$2; shift 2 ;; -j) shift 2 ;; -j*) shift ;; *) target=$1; shift ;; esac; done
/bin/sh '@SB@/lib/step.sh' ninja-run "$dir" "$target" || exit $?
exec /bin/sh "$dir/build.ninja" "$target"
''',
    "cmake": r'''#!/bin/sh
case "${1:-}" in
  --version) echo "cmake version 4.4.5"; exit 0 ;;
  --build) exec /bin/sh "$2/build.ninja" all ;;
  --install) exec /bin/sh "$2/build.ninja" install ;;
esac
src=""; nx=""; for a in "$@"; do [ "$nx" = S ] && src=$a; nx=""; [ "$a" = -S ] && nx=S; done
exec /bin/sh '@SB@/lib/step.sh' "cmake-configure:$(cat "$src/standin-tool")" "$@"
''',
    "meson": r'''#!/bin/sh
case "$1" in
  setup) shift; exec /bin/sh '@SB@/lib/step.sh' meson-setup "$@" ;;
  compile) exec /bin/sh "$3/build.ninja" all ;;
  install) exec /bin/sh "$3/build.ninja" install ;;
esac
exit 1
''',
    "python3.14": r'''#!/bin/sh
case "${1:-}" in
  --version) echo "Python 3.14.10"; exit 0 ;;
  -c) echo "OpenSSL 3.5.9 (stand-in)"; echo "@P@/ssl/cert.pem"; exit 0 ;;
  -m) if [ "${2:-}" = pip ]; then echo "pip 25.2 (stand-in)"; exit 0; fi ;;
esac
exec '@REALPY@' "$@"
''',
    "llvm-config": r'''#!/bin/sh
case "${1:-}" in
  --version) echo 22.1.8 ;;
  --shared-mode) echo shared ;;
  --components) echo "aarch64 all bitwriter core coverage engine executionengine instcombine irreader libdriver linker lto mcdisassembler mcjit option scalaropts spirv target transformutils x86" ;;
  *) exit 1 ;;
esac
''',
}

# helpers of the script itself, at the head of the caller's PATH: they journal without touching HOME or TMPDIR
HELPERS = {
    "uname": r'''case "${1:-}" in -s|'') cat '@SB@/control/os' ;; -m|-p) cat '@SB@/control/arch' ;; -r) echo 25.7.0 ;; *) echo "$(cat '@SB@/control/os') standin" ;; esac''',
    "xcrun": r'''rec xcrun "$@"; [ "$*" = "--sdk macosx --show-sdk-path" ] && echo '@SB@/sdk' ''',
    "security": r'''rec security "$@"; printf -- '-----BEGIN CERTIFICATE-----\nc3RhbmQtaW4=\n-----END CERTIFICATE-----\n' ''',
    "sysctl": r'''[ "$*" = "-n hw.ncpu" ] && echo 3''',
    "otool": r'''rec otool "$@"
f=$2; b=${f##*/}; echo "$f:"
case "$f" in *.dylib) printf '\t%s (compatibility version 1.0.0, current version 1.0.0)\n' "$f" ;; esac
printf '\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1351.0.0)\n'
if [ -f '@SB@/control/foreign' ] && grep -qx "$b" '@SB@/control/foreign'; then
  printf '\t/usr/local/opt/zstd/lib/libzstd.1.dylib (compatibility version 1.0.0, current version 1.5.7)\n'; fi
if [ -f '@SB@/control/rpath' ] && grep -qx "$b" '@SB@/control/rpath'; then
  printf '\t@rpath/libLLVM.dylib (compatibility version 1.0.0, current version 22.1.8)\n'; fi''',
}
REC = r'''rec() { { printf '@@ %s\n' "$1"; shift; for a in "$@"; do printf 'A %s\n' "$a"; done; env | LC_ALL=C sort | sed 's/^/E /'; printf '@@end\n'; } >> '@SB@/journal'; }'''
POISON = ["cc", "c++", "clang", "cmake", "ninja", "python3", "pkg-config", "make", "meson", "perl-build"]


def write(path, text, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)


class Sandbox:
    """one imitated machine: stand-in trees, helpers, the four folders, a caller home and temporary folder"""

    def __init__(self, base, os_name="Darwin", arch="x86_64"):
        self.sb = pathlib.Path(base)
        sb = self.sb
        for d in ("control", "home", "tmp", "sdk", "xcode", "scripts"):
            (sb / d).mkdir(parents=True, exist_ok=True)
        (sb / "control/os").write_text(os_name + "\n")
        (sb / "control/arch").write_text(arch + "\n")
        sub = lambda t: t.replace("@SB@", str(sb)).replace("@REALPY@", sys.executable)
        write(sb / "lib/step.sh", sub(STEP_SH), 0o755)
        write(sb / "lib/install.sh", sub(INSTALL_SH), 0o755)
        for n, t in TEMPLATES.items():
            write(sb / "templates" / n, sub(t), 0o755)
        write(sb / "bin-meson/meson", sub(TEMPLATES["meson"]), 0o755)
        for n, t in HELPERS.items():
            write(sb / "bin" / n, "#!/bin/sh\n" + sub(REC) + "\n" + sub(t) + "\n", 0o755)
        for n in POISON:
            write(sb / "poison/bin" / n, "#!/bin/sh\n" + sub(REC) + f"\nrec POISON-{n} \"$@\"\nexit 1\n", 0o755)
        # the caller's own pip settings, which no build may read
        write(sb / "home/.config/pip/pip.conf", "[global]\nindex-url = https://poison.invalid/simple\n")
        write(sb / "home/.pip/pip.conf", "[global]\nindex-url = https://poison.invalid/simple\n")
        # the stand-in source trees
        src = sb / "src"
        write(src / "ninja/configure.py",
              "import subprocess, sys\nsys.exit(subprocess.call(['/bin/sh', %r, 'ninja-configure'] + sys.argv[1:]))\n"
              % str(sb / "lib/step.sh"))
        write(src / "cmake/bootstrap", f"#!/bin/sh\nexec /bin/sh '{sb}/lib/step.sh' cmake-bootstrap \"$@\"\n", 0o755)
        write(src / "openssl/Configure", f"exec '/bin/sh', '{sb}/lib/step.sh', 'openssl-configure', @ARGV;\n")
        write(src / "python/configure", f"#!/bin/sh\nexec /bin/sh '{sb}/lib/step.sh' python-configure \"$@\"\n", 0o755)
        write(src / "python/README.rst", "stand-in CPython\n")
        for tool, rel in (("zstd", "zstd/build/cmake"), ("xz", "xz"), ("spirv-headers", "spirv-headers"),
                          ("spirv-tools", "spirv-tools"), ("glslang", "glslang"), ("llvm", "llvm/llvm")):
            write(src / rel / "standin-tool", tool + "\n")
            write(src / rel / "CMakeLists.txt", "# stand-in\n")
        write(src / "spirv-headers/include/spirv/unified1/spirv.h", "/* stand-in */\n")
        write(src / "pkgconf/meson.build", "# stand-in\n")
        self.prefix, self.work, self.logs, self.src = sb / "prefix", sb / "work", sb / "logs", src
        self.journal = sb / "journal"

    def env(self, **extra):
        e = {"PATH": f"{self.sb}/bin:/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(self.sb / "home"),
             "TMPDIR": str(self.sb / "tmp"), "LANG": "C", "USER": "tester",
             "NM_TC_PREFIX": str(self.prefix), "NM_TC_SRC": str(self.src), "NM_TC_WORK": str(self.work),
             "NM_TC_LOGS": str(self.logs), "NM_TC_JOBS": "2", "NM_TC_MESON": str(self.sb / "bin-meson/meson"),
             "NM_TC_PYTHON": sys.executable}
        for k, v in extra.items():
            if v is None:
                e.pop(k, None)
            else:
                e[k] = v
        return e

    def run(self, args, script=SCRIPT, **extra):
        p = subprocess.run([BASH, str(script)] + list(args), env=self.env(**extra), cwd=str(self.sb / "home"),
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300)
        return p.returncode, p.stdout, p.stderr

    def records(self):
        """the journal: [(who, cwd, args, env)]"""
        out = []
        if not self.journal.exists():
            return out
        cur = None
        for line in self.journal.read_text(errors="replace").splitlines():
            if line.startswith("@@ "):
                cur = [line[3:], "", [], {}]
            elif line == "@@end" and cur is not None:
                out.append(tuple(cur)); cur = None
            elif cur is not None:
                if line.startswith("C "):
                    cur[1] = line[2:]
                elif line.startswith("A "):
                    cur[2].append(line[2:])
                elif line.startswith("E ") and "=" in line:
                    k, v = line[2:].split("=", 1); cur[3][k] = v
        return out

    def steps(self):
        """the build steps only (not the script's own helpers)"""
        return [r for r in self.records() if r[0] not in ("xcrun", "security", "otool") and not r[0].startswith("POISON")]

    def stamp(self, tool):
        f = self.prefix / ".nm-toolchain" / tool
        return f.read_text().strip() if f.exists() else None


def mutate(sb, name, pairs):
    """a scratch copy of toolchain.sh with each (old, new) replaced once; refuses a pair that does not match"""
    text = SCRIPT.read_text()
    for old, new in pairs:
        assert text.count(old) >= 1, f"mutation {name}: {old!r} is not in build/toolchain.sh"
        text = text.replace(old, new, 1)
    p = sb.sb / "scripts" / f"{name}.sh"
    write(p, text, 0o755)
    return p


def tree_state(root, skip=()):
    """{relative path: content hash or link target or 'dir'} of everything under root, skipping the given top-level names"""
    root = pathlib.Path(root); st = {}
    for dp, dns, fns in os.walk(root):
        rel_d = os.path.relpath(dp, root)
        if rel_d == "." :
            dns[:] = [d for d in dns if d not in skip]
        for d in dns:
            p = os.path.join(dp, d)
            st[os.path.relpath(p, root)] = "link:" + os.readlink(p) if os.path.islink(p) else "dir"
        for f in fns:
            p = os.path.join(dp, f); r = os.path.relpath(p, root)
            if rel_d == "." and f in skip:
                continue
            if os.path.islink(p):
                st[r] = "link:" + os.readlink(p)
            else:
                st[r] = hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()
    return st


def diff_states(a, b):
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))


# layout files build.sh looks in, per tool (Darwin names)
LAYOUT = {
    "ninja": ["bin/ninja"], "cmake": ["bin/cmake", "bin/ctest", "bin/cpack"],
    "openssl": ["build-deps/lib/libssl.a", "build-deps/lib/libcrypto.a", "build-deps/include/openssl/ssl.h", "ssl/cert.pem"],
    "zstd": ["build-deps/lib/libzstd.a", "build-deps/include/zstd.h"],
    "xz": ["build-deps/lib/liblzma.a", "build-deps/include/lzma.h"],
    "python": ["python/bin/python3.14"], "pkgconf": ["bin/pkgconf", "bin/pkg-config"],
    "spirv-tools": ["bin/spirv-val", "bin/spirv-opt", "lib/pkgconfig/SPIRV-Tools.pc", "include/spirv/unified1/spirv.h"],
    "glslang": ["bin/glslang", "bin/glslangValidator"],
    "llvm": ["llvm/bin/llvm-config", "llvm/bin/clang", "llvm/lib/libLLVM.dylib", "llvm/lib/libclang-cpp.dylib",
             "llvm/lib/libclang.dylib", "llvm/lib/cmake/llvm/LLVMConfig.cmake", "llvm/lib/cmake/clang/ClangConfig.cmake",
             "llvm/lib/clang/22/include/opencl-c.h", "llvm/lib/clang/22/include/opencl-c-base.h"],
}


def layout_problems(prefix):
    return [f"{t}: {f}" for t, fs in LAYOUT.items() for f in fs if not (pathlib.Path(prefix) / f).exists()]


def configure_order(sb):
    """the tools in the order their first build step ran"""
    first = {"ninja-configure": "ninja", "cmake-bootstrap": "cmake", "openssl-configure": "openssl",
             "python-configure": "python", "meson-setup": "pkgconf"}
    seen = []
    for who, *_ in sb.steps():
        t = first.get(who) or (who.split(":", 1)[1] if who.startswith("cmake-configure:") else None)
        if t == "spirv-headers":
            t = "spirv-tools"
        if t and t not in seen:
            seen.append(t)
    return seen


# what the script gives, what a shell adds, and LC_CTYPE, which Python sets for its children when the locale is C
# (ninja's configure.py runs the stand-in step); its value is checked like every other for the caller's text
ALLOWED_STEP_ENV = {"HOME", "USER", "LANG", "TMPDIR", "PATH", "PYTHONDONTWRITEBYTECODE", "SDKROOT", "DEVELOPER_DIR",
                    "PWD", "OLDPWD", "SHLVL", "_", "LC_CTYPE"}
POISONED = ["CC", "CFLAGS", "LDFLAGS", "CPATH", "LIBRARY_PATH", "PKG_CONFIG_PATH", "PYTHONPATH", "HOMEBREW_PREFIX"]


def poison_env(sb):
    return dict(CC="/poison/bin/cc", CFLAGS="-DPOISON", LDFLAGS="-L/poison/lib", CPATH="/poison/include",
                LIBRARY_PATH="/poison/lib", PKG_CONFIG_PATH="/poison/lib/pkgconfig", PYTHONPATH="/poison/py",
                HOMEBREW_PREFIX="/poison", SDKROOT="/poison/sdk", DEVELOPER_DIR=str(sb.sb / "xcode"),
                PATH=f"{sb.sb}/poison/bin:{sb.sb}/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                NM_TC_PYTHON=None if os.access("/usr/bin/python3", os.X_OK) else sys.executable,
                NM_TC_STAMP="stamp-text-from-caller")


def leak_problems(sb):
    """every way the caller's environment reached a build step"""
    probs = []
    work_home = str(sb.work / "home")
    for who, cwd, args, env in sb.steps():
        extra = set(env) - ALLOWED_STEP_ENV
        if extra:
            probs.append(f"{who}: has {sorted(extra)}")
        for k, v in env.items():
            if "poison" in v:
                probs.append(f"{who}: {k}={v}")
        if not env.get("PATH", "").startswith(str(sb.prefix / "bin") + ":"):
            probs.append(f"{who}: PATH {env.get('PATH')}")
        if env.get("HOME") != work_home:
            probs.append(f"{who}: HOME {env.get('HOME')}")
        if env.get("DEVELOPER_DIR") != str(sb.sb / "xcode"):
            probs.append(f"{who}: DEVELOPER_DIR {env.get('DEVELOPER_DIR')}")
        if env.get("SDKROOT") != str(sb.sb / "sdk"):
            probs.append(f"{who}: SDKROOT {env.get('SDKROOT')}")
    for who, cwd, args, env in sb.records():
        if who.startswith("POISON"):
            probs.append(f"ran {who}")
        if who == "xcrun" and ("SDKROOT" in env or "poison" in env.get("PATH", "")):
            probs.append("xcrun saw the caller's SDKROOT or PATH")
    if (sb.work / "home/.config/pip/pip.conf").exists() or (sb.work / "home/.pip/pip.conf").exists():
        probs.append("a pip.conf reached the builds' home folder")
    return probs


def arm(name):
    return not ONLY or name in ONLY


# ------------------------------------------------------------------------------------------------ arms
def main():
    print(f"shell: {BASH} ({subprocess.run([BASH, '-c', 'echo $BASH_VERSION'], capture_output=True, text=True).stdout.strip()})")
    check("build/toolchain.sh passes bash -n", subprocess.run([BASH, "-n", str(SCRIPT)]).returncode == 0)
    with tempfile.TemporaryDirectory(prefix="tc-test-") as tmp:
        tmp = pathlib.Path(tmp)
        n = [0]

        def sandbox(**kw):
            n[0] += 1
            return Sandbox(tmp / f"sb{n[0]}", **kw)

        if arm("order") or arm("poison") or arm("outside") or arm("alone"):
            sb = sandbox()
            before = tree_state(sb.sb, skip=("prefix", "work", "logs", "journal"))
            rc, out, err = sb.run(["list"])
            check("list names the ten tools in build order", rc == 0 and out.split() == TOOLS, out + err)
            rc, out, err = sb.run(["build"] + TOOLS, **poison_env(sb))
            check("build of all ten tools in a poisoned environment succeeds", rc == 0, out[-1500:] + err[-1500:])
            # order and layout
            check("build ran the tools in the order given", configure_order(sb) == TOOLS, configure_order(sb))
            check("the layout build.sh looks in is there", not layout_problems(sb.prefix), layout_problems(sb.prefix))
            broken = sb.sb / "broken-prefix"
            shutil.copytree(sb.prefix, broken, symlinks=True)
            (broken / "bin/glslangValidator").unlink()
            control("the layout check reports a missing glslangValidator", layout_problems(broken))
            shutil.rmtree(broken)
            check("every tool has its stamp, with the caller's NM_TC_STAMP text",
                  all(sb.stamp(t) == "stamp-text-from-caller" for t in TOOLS), {t: sb.stamp(t) for t in TOOLS})
            check("build folders are removed once the checks pass", not any((sb.work / t).exists() for t in TOOLS),
                  sorted(os.listdir(sb.work)))
            llvm_conf = [r for r in sb.steps() if r[0] == "cmake-configure:llvm"]
            if "NM_TC_PYTHON" not in {k for k, v in poison_env(sb).items() if v is not None}:
                ok = bool(llvm_conf) and f"-DPython3_EXECUTABLE={sb.prefix}/python/bin/python3.14" in llvm_conf[0][2] \
                    and f"{sb.prefix}/python/bin" in llvm_conf[0][3].get("PATH", "")
                check("without NM_TC_PYTHON, LLVM is configured with the toolchain's Python, its folder on PATH", ok,
                      llvm_conf[:1])
            llvm_args = llvm_conf[0][2] if llvm_conf else []
            check("LLVM gets absolute install names and only the host and SPIRV targets",
                  f"-DCMAKE_INSTALL_NAME_DIR={sb.prefix}/llvm/lib" in llvm_args and "-DLLVM_TARGETS_TO_BUILD=host;SPIRV" in llvm_args,
                  llvm_args)
            ossl = [r for r in sb.steps() if r[0] == "openssl-configure"]
            check("OpenSSL is configured for the Intel Mac target", bool(ossl) and "darwin64-x86_64-cc" in ossl[0][2], ossl[:1])
            check("ssl/cert.pem is what the system keychain gave", "BEGIN CERTIFICATE" in (sb.prefix / "ssl/cert.pem").read_text())
            check("cmake builds ignore the Homebrew and MacPorts prefixes",
                  all("-DCMAKE_IGNORE_PREFIX_PATH=/opt/homebrew;/usr/local;/opt/local" in r[2]
                      for r in sb.steps() if r[0].startswith("cmake-configure:") or r[0] == "cmake-bootstrap"))
            # poison
            probs = leak_problems(sb)
            check("no caller variable, foreign PATH folder or pip.conf reaches a build step; DEVELOPER_DIR and xcrun's SDK do",
                  not probs, probs[:6])
            # outside
            after = tree_state(sb.sb, skip=("prefix", "work", "logs", "journal"))
            check("nothing is written outside the four folders, and the source trees are unchanged",
                  not diff_states(before, after), diff_states(before, after)[:10])
            check("what the builds wrote to their home and temporary folders is in the work folder",
                  any((sb.work / "home/.cache").glob("standin-*")) and any((sb.work / "tmp").glob("standin-*")))
            fixed_tmp = re.compile(r"(^|[\s\"'=:(])/(private/)?tmp\b", re.M)
            check("the script names no fixed temporary folder", not fixed_tmp.search(SCRIPT.read_text()))
            control("a script that names /tmp is reported", fixed_tmp.search(SCRIPT.read_text() + "\nmktemp /tmp/x\n"))

            # alone: build glslang again; everything else stays
            others_before = tree_state(sb.prefix)
            glslang_files = set((sb.prefix / ".nm-toolchain/glslang.files").read_text().split())
            (sb.sb / "control/glslang-extra").write_text("v2\n")
            stamps = {t: sb.stamp(t) for t in TOOLS}
            rc, out, err = sb.run(["build", "glslang"], NM_TC_STAMP="second")
            after_p = tree_state(sb.prefix)
            changed = [p for p in diff_states(others_before, after_p)
                       if p not in glslang_files and not p.startswith(".nm-toolchain/glslang")
                       and p != "share/glslang-standin-v2" and p != "share"]
            check("building glslang again leaves the other tools' files and stamps alone",
                  rc == 0 and not changed and all(sb.stamp(t) == stamps[t] for t in TOOLS if t != "glslang")
                  and sb.stamp("glslang") == "second", (rc, err[-500:], changed[:5]))
            (sb.sb / "control/glslang-extra").write_text("v3\n")
            rc, out, err = sb.run(["build", "glslang"])
            check("a new build of a tool removes the files of its previous build",
                  rc == 0 and (sb.prefix / "share/glslang-standin-v3").exists()
                  and not (sb.prefix / "share/glslang-standin-v2").exists(), err[-500:])
            bad = mutate(sb, "all-stamps", [('rm -f "$STATE/$t"\n', 'rm -f "$STATE"/*[!s]\n')])
            rc, out, err = sb.run(["build", "glslang"], script=bad)
            control("a script that removes every stamp before a build is caught",
                    [t for t in TOOLS if t != "glslang" and sb.stamp(t) != stamps[t]])

        if arm("poison"):
            sb = sandbox()
            bad = mutate(sb, "no-env-i", [('CLEAN=(env -i ', 'CLEAN=(env ')])
            sb.run(["build", "ninja", "cmake", "zstd"], script=bad, **poison_env(sb))
            control("with env instead of env -i, the caller's variables are seen in the steps", leak_problems(sb))

        if arm("outside"):
            sb = sandbox()
            before = tree_state(sb.sb, skip=("prefix", "work", "logs", "journal", "scripts"))
            bad = mutate(sb, "caller-home", [('"HOME=$W_HOME" "USER', '"HOME=$HOME" "USER')])
            sb.run(["build", "ninja", "cmake"], script=bad)
            control("builds given the caller's home folder write outside the four folders",
                    diff_states(before, tree_state(sb.sb, skip=("prefix", "work", "logs", "journal", "scripts"))))

        if arm("fail"):
            def fail_case(sb, script=SCRIPT):
                (sb.sb / "control/fail-cmake-configure:xz").write_text("")
                rc, out, err = sb.run(["build", "ninja", "cmake", "zstd", "xz", "pkgconf"], script=script)
                log = sb.logs / "xz.log"
                probs = []
                if rc != 1: probs.append(f"exit status {rc}")
                if "STANDIN-FAILURE-MARKER" not in err: probs.append("the last lines are not on the terminal")
                if str(log) not in err: probs.append("the log is not named")
                if not log.exists() or "+ env -i" not in log.read_text() or "STANDIN-FAILURE-MARKER" not in log.read_text():
                    probs.append("the log lacks the command line or its output")
                if sb.stamp("xz") is not None: probs.append("xz has a stamp")
                if sb.stamp("zstd") is None: probs.append("zstd, built before, has no stamp")
                if any(r[0] == "meson-setup" for r in sb.steps()): probs.append("pkgconf was built after the failure")
                return probs, err
            sb = sandbox()
            probs, err = fail_case(sb)
            check("a failing step: exit 1, its last lines and log named, no stamp, nothing built after it", not probs,
                  (probs, err[-800:]))
            sb = sandbox()
            bad = mutate(sb, "early-stamp", [('rm -f "$STATE/$t"\n', 'printf "early\\n" > "$STATE/$t"\n')])
            probs, err = fail_case(sb, bad)
            control("a script that writes the stamp before building leaves one after a failure", probs)

        if arm("check"):
            def check_case(sb, setup, script=SCRIPT, words=()):
                setup(sb)
                rc, out, err = sb.run(["build", "ninja", "cmake", "spirv-tools"], script=script)
                probs = []
                if rc == 0: probs.append("exit status 0")
                if sb.stamp("spirv-tools") is not None: probs.append("spirv-tools has a stamp")
                for w in words:
                    if w not in err: probs.append(f"the terminal does not name {w}")
                return probs, err
            cases = [
                ("a missing SPIRV-Tools.pc", lambda s: (s.sb / "control/omit").write_text("spirv-tools:lib/pkgconfig/SPIRV-Tools.pc\n"),
                 ["SPIRV-Tools.pc"], [('need_file lib/pkgconfig/SPIRV-Tools.pc; ', ''),
                                      ('grep -q \'^Version: *[0-9]\' "$P/lib/pkgconfig/SPIRV-Tools.pc" 2>/dev/null || bad', 'true || bad')]),
                ("a Homebrew library in otool -L", lambda s: (s.sb / "control/foreign").write_text("libSPIRV-Tools-shared.dylib\n"),
                 ["libSPIRV-Tools-shared.dylib", "/usr/local/opt/zstd/lib/libzstd.1.dylib"], [("  is_darwin || return 0\n  [ -f \"$rec\" ]", "  return 0\n  [ -f \"$rec\" ]")]),
                ("an @rpath library in otool -L", lambda s: (s.sb / "control/rpath").write_text("libSPIRV-Tools-shared.dylib\n"),
                 ["@rpath/libLLVM.dylib"], [("/usr/lib/*|/System/Library/*|\"$P\"/*) ;;", "*) ;;")]),
            ]
            for what, setup, words, muts in cases:
                sb = sandbox()
                probs, err = check_case(sb, setup, words=words)
                check(f"{what}: no stamp, exit status not 0, the terminal names it", not probs, (probs, err[-800:]))
                sb = sandbox()
                bad = mutate(sb, "check", muts)
                probs, err = check_case(sb, setup, script=bad)
                control(f"a script without that check stamps the tool despite {what}", probs)
            sb = sandbox()
            sb.run(["build", "ninja", "cmake", "spirv-tools"])
            rc, out, err = sb.run(["check", "ninja", "spirv-tools"], NM_TC_WORK=None, NM_TC_LOGS=None, NM_TC_SRC=None)
            check("check needs only NM_TC_PREFIX and passes on what was built", rc == 0 and out.count("  ok") == 2, out + err)
            (sb.prefix / "bin/spirv-val").unlink()
            rc, out, err = sb.run(["check", "spirv-tools"])
            check("check fails and names a file removed since", rc == 1 and "spirv-val" in out, out + err)

        if arm("id"):
            sb = sandbox()
            ids = {t: sb.run(["id", t])[1].strip() for t in TOOLS}
            check("id prints one line per tool, naming it", all(v.startswith(t + " ") and "\n" not in v for t, v in ids.items()), ids)
            bumped = mutate(sb, "bump", [("ninja) echo 1 ;;", "ninja) echo 2 ;;")])
            new = {t: sb.run(["id", t], script=bumped)[1].strip() for t in TOOLS}

            def id_problems(new_ids):
                p = [] if new_ids["ninja"] != ids["ninja"] else ["ninja's id did not change"]
                return p + [t for t in TOOLS if t != "ninja" and new_ids[t] != ids[t]]
            check("a raised recipe number changes that tool's id and no other", not id_problems(new), new)
            blind = mutate(sb, "blind", [("ninja) echo 1 ;;", "ninja) echo 2 ;;"),
                                         ("\"$(recipe_number \"$1\")\"", "1")])
            control("an id that ignores the recipe number does not show the raise",
                    id_problems({t: sb.run(["id", t], script=blind)[1].strip() for t in TOOLS}))
            (sb.sb / "control/arch").write_text("arm64\n")
            check("the processor is part of the id", sb.run(["id", "ninja"])[1].strip() != ids["ninja"])

        if arm("env"):
            sb = sandbox()

            def env_problems(script=SCRIPT):
                p = []
                for var in ("NM_TC_PREFIX", "NM_TC_SRC", "NM_TC_WORK", "NM_TC_LOGS"):
                    rc, out, err = sb.run(["build", "ninja"], script=script, **{var: None})
                    if rc == 0 or len(err.strip().splitlines()) != 1 or var not in err:
                        p.append(f"{var}: exit {rc}, {err.strip()[:200]!r}")
                rc, out, err = sb.run(["build", "pkgconf"], script=script, NM_TC_MESON=None)
                if rc == 0 or "NM_TC_MESON" not in err:
                    p.append(f"NM_TC_MESON: exit {rc}, {err.strip()[:200]!r}")
                rc, out, err = sb.run(["build", "nosuchtool"], script=script)
                if rc == 0 or "nosuchtool" not in err:
                    p.append("an unknown tool is not refused")
                return p
            p = env_problems()
            check("a missing setting or an unknown tool stops with one line naming it", not p, p)
            bad = mutate(sb, "no-env-check", [('[ -n "$p" ] || die "$v is not set', '[ -n "$p" ] || p=$PWD/unset; true || die "$v is not set')])
            control("a script that does not ask for its settings goes on without them", env_problems(bad))

        if arm("linux"):
            def linux_problems(sb):
                p = []
                for r in sb.records():
                    if r[0] in ("xcrun", "security", "otool"):
                        p.append(f"{r[0]} ran")
                ossl = [r for r in sb.steps() if r[0] == "openssl-configure"]
                if not ossl or any(a.startswith("darwin64") for a in ossl[0][2]):
                    p.append(f"openssl configure: {ossl[:1]}")
                if any("SDKROOT" in r[3] for r in sb.steps()):
                    p.append("a step has SDKROOT")
                return p
            sb = sandbox(os_name="Linux")
            sb.run(["build", "ninja", "cmake", "zstd", "openssl"])
            p = linux_problems(sb)
            check("on Linux, no xcrun, security or otool runs, no SDKROOT, no darwin target for OpenSSL", not p, p)
            sb = sandbox(os_name="Linux")
            bad = mutate(sb, "always-darwin", [('is_darwin() { [ "$(uname -s)" = Darwin ]; }', 'is_darwin() { true; }')])
            sb.run(["build", "ninja", "cmake", "zstd", "openssl"], script=bad)
            control("a script that takes every system for a Mac runs the Mac steps on Linux", linux_problems(sb))
            sb = sandbox(arch="arm64")
            rc, out, err = sb.run(["build", "openssl"])
            ossl = [r for r in sb.steps() if r[0] == "openssl-configure"]
            check("on Apple silicon OpenSSL gets darwin64-arm64-cc", rc == 0 and bool(ossl) and "darwin64-arm64-cc" in ossl[0][2]
                  and "darwin64-x86_64-cc" not in ossl[0][2], (rc, err[-300:], ossl[:1]))

        if arm("bash32"):
            # what /bin/bash 3.2 and the BSD userland of macOS do not have
            rules = [
                (r"\bdeclare\s+-[aA]*A|\blocal\s+-A", "associative arrays (bash 4)"),
                (r"\bmapfile\b|\breadarray\b|\bcoproc\b", "bash 4 builtins"),
                (r"\$\{[A-Za-z_][A-Za-z0-9_]*(,,|\^\^)", "case conversion (bash 4)"),
                (r"\$\{[A-Za-z_][A-Za-z0-9_]*@[QEPAa]\}", "parameter transformation (bash 4.4)"),
                (r"&>>|\|&", "bash 4 redirections"),
                (r"\[\[\s+-v\b", "[[ -v ]] (bash 4.2)"),
                (r"\breadlink\s+-f\b|\brealpath\b", "GNU readlink -f / realpath"),
                (r"\bsed\s+(-[a-zA-Z]*\s+)*-i\b|\bsed\s+-r\b", "GNU sed -i / -r"),
                (r"\bcp\s+(-[a-zA-Z]*\s+)*-a\b|\bcp\s+--", "GNU cp options"),
                (r"\bstat\s+-c\b|\bdate\s+-d\b|\bmktemp\s+--|\bfind\b[^\n]*-printf\b|\bgrep\s+-P\b|\bxargs\s+-r\b",
                 "GNU stat, date, mktemp, find, grep or xargs options"),
                (r"\$EPOCHSECONDS|\$EPOCHREALTIME|\$SRANDOM", "bash 5 variables"),
            ]

            def scan(text):
                hits = []
                for i, line in enumerate(text.splitlines(), 1):
                    code = line.split(" #", 1)[0] if not line.lstrip().startswith("#") else ""
                    for rx, what in rules:
                        if re.search(rx, code):
                            hits.append(f"line {i}: {what}: {line.strip()[:100]}")
                return hits
            text = SCRIPT.read_text()
            check("nothing that bash 3.2 or the BSD tools lack", not scan(text), scan(text))
            control("a mapfile and a sed -i are reported", scan(text + "\nmapfile -t x < f\nsed -i 's/a/b/' f\n"))
            # an empty array expanded bare stops bash 3.2 under set -u: every possibly empty one is written ${a[@]+...}
            bare = [m.group(0) for m in re.finditer(r'(?<!\+)"\$\{target\[@\]\}"', text)]
            check("the one array that can be empty is expanded with ${a[@]+...}", not bare, bare)

    print("all passed" if not fails else f"FAILED {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
