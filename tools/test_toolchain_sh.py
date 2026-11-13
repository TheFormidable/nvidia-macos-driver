#!/usr/bin/env python3
"""build/toolchain.sh, the recipes that build NVK's tools from source, run for real against stand-in source trees.

The real builds take hours and need a Mac; here every source tree is a stand-in whose build scripts (configure.py,
bootstrap, Configure, configure, the CMake and meson projects) record how they were called (arguments, folder,
environment) in a journal and leave behind the files the real install would. The programs they "install" (ninja,
cmake, python3.14, llvm-config, pkgconf, ...) are small scripts that answer as the real ones do. A Mac is imitated by
stand-ins of uname, otool and sysctl at the head of the caller's PATH, and of xcrun and security in a folder of the
sandbox: the script runs those two from the fixed folder SYSBIN (/usr/bin), so the tests run a copy of the script whose
one line "SYSBIN=/usr/bin" names the sandbox's folder instead (the "sysbin" arm checks that this is the only change and
that a PATH xcrun or security is never run). One arm imitates Linux instead.

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
  pkgconfig          pkgconf whose "system" folders are inside the prefix is refused; after SPIRV-Tools, the toolchain's
                     pkg-config --cflags / --libs SPIRV-Tools, run from / with an empty environment, give -I<prefix>/include
                     and -L<prefix>/lib -lSPIRV-Tools-opt -lSPIRV-Tools -lSPIRV-Tools-link, or the stamp is refused
  minos              MACOSX_DEPLOYMENT_TARGET (default 15.0) reaches every step and the id; a Mach-O file or a static
                     library (its first member) built for a later macOS refuses the stamp; a static library whose members
                     carry no version is only noted
  foreign            a prefix, work or log folder that holds files and has no marker of the script is refused, untouched
  link               the folders are judged where they really are: a prefix that is a link into the work folder is refused
  markerlink         a marker (.nm-toolchain, .nm-toolchain-work, .nm-toolchain-logs) that is a link is refused, its target untouched
  record             a record line that is not a file of the prefix reached through real folders (.., absolute, through a
                     link out of the prefix or back into it) removes nothing and stops
  sysbin             xcrun and security come from SYSBIN, never from the caller's PATH
  mac                the recipes run what the Mac's by-hand build ran: cmake's bootstrap and make, zstd's Makefile,
                     OpenSSL no-shared no-tests, CPython's plain configure, SPIRV-Tools' default (static) libraries,
                     LLVM with exceptions; ninja, cmake, OpenSSL, zstd and CPython in a copy of their tree
  alone              building one tool again leaves the others' files and stamps alone and removes its own old files
  id                 a raised recipe number is visible in id, for that tool only
  env                a missing setting stops with one line naming it
  linux              on Linux no xcrun, security or otool runs and OpenSSL gets no darwin target; on Apple silicon
                     OpenSSL gets the arm64 target
  bash32             nothing in the script that /bin/bash 3.2 or the BSD tools of macOS lack

The shell is $BUILD_SH_BASH (default /bin/bash, macOS's bash 3.2 on a Mac). usage: test_toolchain_sh.py [arm ...]"""
import hashlib, os, pathlib, platform, re, shutil, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
REAL_SCRIPT = ROOT / "build" / "toolchain.sh"
SYSBIN_LINE = "SYSBIN=/usr/bin\n"
SCRIPT = None   # the sandbox's copy, see Sandbox.script
BASH = os.environ.get("BUILD_SH_BASH") or "/bin/bash"
TOOLS = ["ninja", "cmake", "openssl", "zstd", "xz", "python", "pkgconf", "spirv-tools", "glslang", "llvm"]
ONLY = sys.argv[1:]
fails = 0


def check(name, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + name + (f"  [{str(detail)[:1200]}]" if detail and not ok else ""), flush=True)
    if not ok:   # a failed build: the end of the log its STOP line names, which says why (the detail ends at the command)
        for path, lines in failed_logs(str(detail)):
            print(f"    last {len(lines)} lines of {path}:", flush=True)
            for l in lines:
                print("      " + l, flush=True)
    fails += 0 if ok else 1
    return ok


def failed_logs(text):
    """[(log, its last 40 lines)] of every "full log: <path>" in a failure's text whose file is there"""
    out = []
    for path in dict.fromkeys(re.findall(r"full log: (\S+)", text)):
        try:
            out.append((path, pathlib.Path(path).read_text(errors="replace").splitlines()[-40:]))
        except OSError:
            pass
    return out


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
  cmake-bootstrap) mkmake cmake "$(argval --prefix= "$@")" install ;;
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
mk() { omit "$1" && return 0; mkdir -p "$(dirname "$p/$1")"
       case "$1" in *.a) printf '!<arch>\nstandin %s\n' "$t" > "$p/$1" ;; *) printf '%s\n' "${2:-standin $t}" > "$p/$1" ;; esac; }
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
  pkgconf) tmpl bin/pkgconf pkgconf "$p"; mk lib/libpkgconf.a ;;
  spirv-headers) mk include/spirv/unified1/spirv.h ;;
  spirv-tools) for x in spirv-as spirv-dis spirv-val spirv-opt spirv-link; do prog "bin/$x" "SPIRV-Tools v2025.5 standin"; done
               mk lib/pkgconfig/SPIRV-Tools.pc "Version: 2025.5
Libs: -L$p/lib -lSPIRV-Tools-opt -lSPIRV-Tools -lSPIRV-Tools-link
Cflags: -I$p/include"; mk include/spirv-tools/libspirv.h
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
    # pkgconf 2.5.1 as built with the toolchain's options: its "system" folders are /usr/include and /usr/lib, so
    # it gives the toolchain's -I and -L; control/pc-sysdirs-prefix imitates the default build (they are the prefix's
    # own, and the -I and -L vanish); control/pc-wrong-libs gives a wrong --libs answer
    "pkgconf": r'''#!/bin/sh
P='@P@'; inc=/usr/include; lib=/usr/lib
[ -f '@SB@/control/pc-sysdirs-prefix' ] && { inc=$P/include; lib=$P/lib; }
case "$*" in
  --version) echo 2.5.1 ;;
  "--variable pc_system_includedirs pkg-config") echo "$inc" ;;
  "--variable pc_system_libdirs pkg-config") echo "$lib" ;;
  "--cflags SPIRV-Tools") [ -f "$P/lib/pkgconfig/SPIRV-Tools.pc" ] || exit 1
                          [ "$inc" = "$P/include" ] && echo " " || echo "-I$P/include " ;;
  "--libs SPIRV-Tools") [ -f "$P/lib/pkgconfig/SPIRV-Tools.pc" ] || exit 1
                        if [ -f '@SB@/control/pc-wrong-libs' ]; then echo "-lSPIRV-Tools"
                        elif [ "$lib" = "$P/lib" ]; then echo "-lSPIRV-Tools-opt -lSPIRV-Tools -lSPIRV-Tools-link"
                        else echo "-L$P/lib  -lSPIRV-Tools-opt -lSPIRV-Tools -lSPIRV-Tools-link"; fi ;;
  *) exit 1 ;;
esac
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
# xcrun and security live in the sandbox's SYSBIN; those of the same name on the caller's PATH must never run
SYSBIN_HELPERS = {
    "xcrun": r'''rec xcrun "$@"; [ "$*" = "--sdk macosx --show-sdk-path" ] && echo '@SB@/sdk' ''',
    "security": r'''rec security "$@"; printf -- '-----BEGIN CERTIFICATE-----\nc3RhbmQtaW4=\n-----END CERTIFICATE-----\n' ''',
}
HELPERS = {
    "uname": r'''case "${1:-}" in -s|'') cat '@SB@/control/os' ;; -m|-p) cat '@SB@/control/arch' ;; -r) echo 25.7.0 ;; *) echo "$(cat '@SB@/control/os') standin" ;; esac''',
    "xcrun": r'''rec PATH-xcrun "$@"; echo /path/sdk''',
    "security": r'''rec PATH-security "$@"''',
    "sysctl": r'''[ "$*" = "-n hw.ncpu" ] && echo 3''',
    "otool": r'''rec otool "$@"
f=$2; b=${f##*/}; echo "$f:"
if [ "$1" = -l ]; then
  mo=15.0; if [ -f '@SB@/control/minos-high' ] && grep -qx "$b" '@SB@/control/minos-high'; then mo=26.0; fi
  if [ -f '@SB@/control/minos-none' ] && grep -qx "$b" '@SB@/control/minos-none'; then
    printf 'Load command 0\n      cmd LC_SEGMENT_64\n'; exit 0; fi
  printf 'Load command 9\n      cmd LC_BUILD_VERSION\n  cmdsize 32\n platform 1\n    minos %s\n      sdk 26.5\n' "$mo"
  exit 0
fi
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
        for n, t in SYSBIN_HELPERS.items():
            write(sb / "sysbin" / n, "#!/bin/sh\n" + sub(REC) + "\n" + sub(t) + "\n", 0o755)
        # the script under test: the checkout's, with SYSBIN naming the sandbox's folder (the only change)
        text = REAL_SCRIPT.read_text()
        assert text.count(SYSBIN_LINE) == 1, "build/toolchain.sh no longer has its one SYSBIN=/usr/bin line"
        self.script = sb / "toolchain.sh"
        write(self.script, text.replace(SYSBIN_LINE, f"SYSBIN={sb}/sysbin\n"), 0o755)
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
        write(src / "zstd/lib/Makefile",
              f"libzstd.a:\n\t/bin/sh '{sb}/lib/step.sh' zstd-make libzstd.a\n"
              f"install-static:\n\t/bin/sh '{sb}/lib/install.sh' zstd $(PREFIX)\ninstall-includes:\n\t@true\n")
        for tool, rel in (("xz", "xz"), ("spirv-headers", "spirv-headers"),
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

    def run(self, args, script=None, **extra):
        p = subprocess.run([BASH, str(script or self.script)] + list(args), env=self.env(**extra), cwd=str(self.sb / "home"),
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
        return [r for r in self.records() if r[0] not in ("xcrun", "security", "otool")
                and not r[0].startswith(("POISON", "PATH-"))]

    def stamp(self, tool):
        f = self.prefix / ".nm-toolchain" / tool
        return f.read_text().strip() if f.exists() else None


def mutate(sb, name, pairs):
    """a scratch copy of toolchain.sh with each (old, new) replaced once; refuses a pair that does not match"""
    text = sb.script.read_text()
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
    first = {"ninja-configure": "ninja", "cmake-bootstrap": "cmake", "openssl-configure": "openssl", "zstd-make": "zstd",
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
                    "MACOSX_DEPLOYMENT_TARGET", "PWD", "OLDPWD", "SHLVL", "_", "LC_CTYPE", "MAKEFLAGS", "MAKELEVEL",
                    "MFLAGS"}
POISONED = ["CC", "CFLAGS", "LDFLAGS", "CPATH", "LIBRARY_PATH", "PKG_CONFIG_PATH", "PYTHONPATH", "HOMEBREW_PREFIX"]


def developer_dir(sb):
    """the DEVELOPER_DIR the poisoned build is given, which must reach every step. On a Mac, /usr/bin/python3 and
    /usr/bin/make (which the recipes run before the toolchain has its own Python, and for cmake, OpenSSL, zstd and
    CPython) are Apple's small programs that run the tool of that name in the developer folder DEVELOPER_DIR names, as
    are cc, c++, ar, ranlib, ld, libtool, strip, install_name_tool, otool and lipo: so on a Mac it is the real developer
    folder (xcode-select -p), and the defaults run as on a tester's Mac. Elsewhere a stand-in folder."""
    if platform.system() == "Darwin":
        d = subprocess.run(["/usr/bin/xcode-select", "-p"], capture_output=True, text=True).stdout.strip()
        if d and os.path.isdir(d):
            return d
    return str(sb.sb / "xcode")


def poison_env(sb):
    return dict(CC="/poison/bin/cc", CFLAGS="-DPOISON", LDFLAGS="-L/poison/lib", CPATH="/poison/include",
                LIBRARY_PATH="/poison/lib", PKG_CONFIG_PATH="/poison/lib/pkgconfig", PYTHONPATH="/poison/py",
                HOMEBREW_PREFIX="/poison", SDKROOT="/poison/sdk", DEVELOPER_DIR=developer_dir(sb),
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
        if env.get("DEVELOPER_DIR") != developer_dir(sb):
            probs.append(f"{who}: DEVELOPER_DIR {env.get('DEVELOPER_DIR')}")
        if env.get("SDKROOT") != str(sb.sb / "sdk"):
            probs.append(f"{who}: SDKROOT {env.get('SDKROOT')}")
        if env.get("MACOSX_DEPLOYMENT_TARGET") != "15.0":
            probs.append(f"{who}: MACOSX_DEPLOYMENT_TARGET {env.get('MACOSX_DEPLOYMENT_TARGET')}")
    for who, cwd, args, env in sb.records():
        if who.startswith(("POISON", "PATH-")):
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
    check("build/toolchain.sh passes bash -n", subprocess.run([BASH, "-n", str(REAL_SCRIPT)]).returncode == 0)
    with tempfile.TemporaryDirectory(prefix="tc-test-") as tmp:
        # where it really is: on a Mac the temporary folder is under /var, a link to /private/var, and the script
        # judges and names its folders by their real place
        tmp = pathlib.Path(os.path.realpath(tmp))
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
            if (sb.prefix / "bin/glslangValidator").is_symlink():
                shutil.copytree(sb.prefix, broken, symlinks=True)
                (broken / "bin/glslangValidator").unlink()
                control("the layout check reports a missing glslangValidator", layout_problems(broken))
                shutil.rmtree(broken)
            else:
                check("control: the layout check reports a missing glslangValidator", False,
                      "not run: the build above left no glslangValidator to remove")
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
            check("the script names no fixed temporary folder", not fixed_tmp.search(REAL_SCRIPT.read_text()))
            control("a script that names /tmp is reported", fixed_tmp.search(REAL_SCRIPT.read_text() + "\nmktemp /tmp/x\n"))

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
            def fail_case(sb, script=None):
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
            tails = failed_logs(err)
            check("a failed build's own FAIL line can show the end of the log its STOP line names",
                  len(tails) == 1 and tails[0][0] == str(sb.logs / "xz.log") and any("STANDIN-FAILURE-MARKER" in l for l in tails[0][1]),
                  (tails, err[-400:]))
            control("a failure text that names no log gives no log to show", failed_logs(err.replace("full log:", "log:")) == [] and ["none"])
            sb = sandbox()
            bad = mutate(sb, "early-stamp", [('rm -f "$STATE/$t"\n', 'printf "early\\n" > "$STATE/$t"\n')])
            probs, err = fail_case(sb, bad)
            control("a script that writes the stamp before building leaves one after a failure", probs)

        if arm("check"):
            def check_case(sb, setup, script=None, words=()):
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

        if arm("pkgconfig"):
            def pc_problems(sb, script=None, expect_pkgconf=True):
                rc, out, err = sb.run(["build", "ninja", "cmake", "pkgconf", "spirv-tools"], script=script)
                p = []
                if expect_pkgconf:
                    log = (sb.logs / "spirv-tools.log").read_text() if (sb.logs / "spirv-tools.log").exists() else ""
                    if rc != 0: p.append(f"exit {rc}: {err[-300:]}")
                    if f"pkg-config --cflags SPIRV-Tools: -I{sb.prefix}/include" not in log: p.append("the --cflags answer is not in the log")
                    if f"pkg-config --libs SPIRV-Tools: -L{sb.prefix}/lib -lSPIRV-Tools-opt -lSPIRV-Tools -lSPIRV-Tools-link" not in log:
                        p.append("the --libs answer is not in the log")
                return rc, err, p
            sb = sandbox()
            rc, err, p = pc_problems(sb)
            check("the toolchain's pkg-config, from / with an empty environment, gives -I<prefix>/include and -L<prefix>/lib "
                  "-lSPIRV-Tools-opt -lSPIRV-Tools -lSPIRV-Tools-link", not p, p)
            pk = [r for r in sb.steps() if r[0] == "meson-setup"]
            check("pkgconf is configured with the system's own folders as its system folders",
                  bool(pk) and "-Dwith-system-libdir=/usr/lib" in pk[0][2] and "-Dwith-system-includedir=/usr/include" in pk[0][2], pk[:1])

            def sysdirs_problems(sb, script=None):
                (sb.sb / "control/pc-sysdirs-prefix").write_text("")
                rc, out, err = sb.run(["build", "ninja", "cmake", "pkgconf"], script=script)
                p = []
                if rc == 0: p.append("exit 0")
                if sb.stamp("pkgconf") is not None: p.append("pkgconf has a stamp")
                if "pc_system_includedirs" not in err or "pc_system_libdirs" not in err: p.append("the terminal does not name both")
                return p, err
            sb = sandbox()
            p, err = sysdirs_problems(sb)
            check("a pkgconf whose system folders are the prefix's include and lib gets no stamp, and both are named", not p, (p, err[-600:]))
            sb = sandbox()
            p, err = sysdirs_problems(sb, mutate(sb, "no-sysdirs", [('case "$d/" in "$P/"*) bad', 'case "$d/" in "/nowhere/"*) bad')]))
            control("a check_pkgconf that does not look at the system folders stamps that pkgconf", p)

            def answer_problems(sb, script=None):
                (sb.sb / "control/pc-wrong-libs").write_text("")
                rc, out, err = sb.run(["build", "ninja", "cmake", "pkgconf", "spirv-tools"], script=script)
                p = []
                if rc == 0: p.append("exit 0")
                if sb.stamp("spirv-tools") is not None: p.append("spirv-tools has a stamp")
                if "--libs SPIRV-Tools" not in err: p.append("the terminal does not name the query")
                return p, err
            sb = sandbox()
            p, err = answer_problems(sb)
            check("a wrong pkg-config --libs SPIRV-Tools answer refuses SPIRV-Tools' stamp", not p, (p, err[-600:]))
            sb = sandbox()
            p, err = answer_problems(sb, mutate(sb, "no-pc-answer", [('if [ "$out" = "$2" ]; then', 'if true; then')]))
            control("a check that does not compare the answer stamps it", p)
            sb = sandbox()
            rc, out, err = sb.run(["build", "ninja", "cmake", "spirv-tools"])
            check("without the toolchain's pkg-config, SPIRV-Tools is checked without the query and says so",
                  rc == 0 and "not asked" in (sb.logs / "spirv-tools.log").read_text(), err[-300:])

        if arm("minos"):
            sb = sandbox()
            rc, out, err = sb.run(["build", "ninja", "cmake", "spirv-tools"], MACOSX_DEPLOYMENT_TARGET="16.1")
            dts = {r[3].get("MACOSX_DEPLOYMENT_TARGET") for r in sb.steps()}
            check("the caller's MACOSX_DEPLOYMENT_TARGET reaches every step", rc == 0 and dts == {"16.1"}, (rc, dts, err[-300:]))
            ids = (sb.run(["id", "llvm"])[1].strip(), sb.run(["id", "llvm"], MACOSX_DEPLOYMENT_TARGET="14.2")[1].strip())
            check("the deployment target is part of the id", ids[0].endswith(" macos15.0") and ids[1].endswith(" macos14.2"), ids)
            rc, out, err = sb.run(["id", "llvm"], MACOSX_DEPLOYMENT_TARGET="15;x")
            check("a deployment target that is not a version stops", rc == 2 and "MACOSX_DEPLOYMENT_TARGET" in err, err)

            def minos_problems(sb, script=None):
                (sb.sb / "control/minos-high").write_text("libSPIRV-Tools-shared.dylib\n")
                rc, out, err = sb.run(["build", "ninja", "cmake", "spirv-tools"], script=script)
                p = []
                if rc == 0: p.append("exit 0")
                if sb.stamp("spirv-tools") is not None: p.append("spirv-tools has a stamp")
                if "26.0" not in err: p.append("the terminal does not name the version")
                return p, err
            sb = sandbox()
            p, err = minos_problems(sb)
            check("a Mach-O file built for a later macOS than the target refuses the stamp", not p, (p, err[-600:]))
            sb = sandbox()
            p, err = minos_problems(sb, mutate(sb, "no-minos", [('elif version_above "$mo" "$DT"; then', 'elif false; then')] * 2))
            control("a check without the version comparison stamps it", p)

            def archive_problems(sb, script=None):
                (sb.sb / "control/minos-high").write_text("libzstd.a\n")
                rc, out, err = sb.run(["build", "ninja", "cmake", "zstd"], script=script)
                p = []
                if rc == 0: p.append("exit 0")
                if sb.stamp("zstd") is not None: p.append("zstd has a stamp")
                if "libzstd.a" not in err or "26.0" not in err: p.append("the terminal does not name the library and its version")
                return p, err
            sb = sandbox()
            p, err = archive_problems(sb)
            check("a static library whose members are built for a later macOS refuses the stamp", not p, (p, err[-600:]))
            sb = sandbox()
            p, err = archive_problems(sb, mutate(sb, "no-archives", [('elsif ($m eq "!<arch>\\n") { print "a $rel\\n" }', '')]))
            control("a check that looks only at Mach-O files stamps it", p)
            sb = sandbox()
            (sb.sb / "control/minos-none").write_text("libzstd.a\n")
            rc, out, err = sb.run(["build", "ninja", "cmake", "zstd"])
            log = (sb.logs / "zstd.log").read_text() if (sb.logs / "zstd.log").exists() else ""
            check("a static library whose members carry no version is noted, not refused",
                  rc == 0 and sb.stamp("zstd") is not None and "libzstd.a: otool -l shows no minimum macOS version" in log
                  and "and its 1 static libraries run on macOS 15.0" in log, (rc, err[-300:]))
            sb = sandbox()
            (sb.sb / "control/minos-none").write_text("libSPIRV-Tools-shared.dylib\n")
            rc, out, err = sb.run(["build", "ninja", "cmake", "spirv-tools"])
            check("a Mach-O file without a version refuses the stamp", rc != 0 and sb.stamp("spirv-tools") is None
                  and "shows no minimum macOS version" in err, (rc, err[-300:]))
            vs = subprocess.run([BASH, "-c", 'source <(sed -n "/^version_above()/,/^}/p" "$1"); r=""; '
                                 'for a in "15.1 15.0" "15.0 15" "26.0 15.0" "15.0 15.0" "14.9 15.0" "15.0.1 15.0"; do '
                                 'set -- $a; if version_above "$1" "$2"; then r="$r y"; else r="$r n"; fi; done; echo $r', "-",
                                 str(REAL_SCRIPT)], capture_output=True, text=True).stdout.split()
            check("version_above: 15.1>15.0, 15.0=15, 26.0>15.0, 15.0=15.0, 14.9<15.0, 15.0.1>15.0",
                  vs == ["y", "n", "y", "n", "n", "y"], vs)

        if arm("foreign"):
            def foreign_problems(sb, var, script=None):
                d = sb.sb / "someones-home"
                write(d / "python/keep.txt", "keep\n"); write(d / "llvm/keep.txt", "keep\n")
                write(d / "ninja/keep.txt", "keep\n"); write(d / "ninja.log", "keep\n")
                before = tree_state(d)
                rc, out, err = sb.run(["build", "ninja"], script=script, **{var: str(d)})
                p = []
                if rc != 2: p.append(f"exit {rc}")
                if len(err.strip().splitlines()) != 1 or var not in err or "not made by build/toolchain.sh" not in err:
                    p.append(f"not one line naming {var}: {err.strip()[:300]!r}")
                if diff_states(before, tree_state(d)): p.append(f"the folder changed: {diff_states(before, tree_state(d))[:5]}")
                return p
            for var, marker in (("NM_TC_PREFIX", ".nm-toolchain"), ("NM_TC_WORK", ".nm-toolchain-work"),
                                ("NM_TC_LOGS", ".nm-toolchain-logs")):
                sb = sandbox()
                p = foreign_problems(sb, var)
                check(f"{var} naming a folder that holds files and was not made by the script is refused, untouched", not p, p)
                sb = sandbox()
                p = foreign_problems(sb, var, mutate(sb, "no-owner", [('[ ! -e "$p/$m" ]; then', '[ -z "$p/$m" ]; then')]))
                control(f"a script without the marker test goes on in {var}", p)
            sb = sandbox()
            rc, out, err = sb.run(["build", "ninja"])
            rc2, out2, err2 = sb.run(["build", "ninja"])
            check("the folders the script made (markers .nm-toolchain/, .nm-toolchain-work, .nm-toolchain-logs) are taken again",
                  rc == 0 and rc2 == 0 and (sb.prefix / ".nm-toolchain").is_dir() and (sb.work / ".nm-toolchain-work").is_file()
                  and (sb.logs / ".nm-toolchain-logs").is_file(), (rc, rc2, err2[-300:]))
            # a log file of the script's own folder that is a link: the next build replaces the link, not its target
            outside_log = sb.sb / "elsewhere.txt"; outside_log.write_text("keep\n")
            (sb.logs / "ninja.log").unlink(); os.symlink(outside_log, sb.logs / "ninja.log")
            rc, out, err = sb.run(["build", "ninja"])
            check("a log that is a link is replaced, its target kept", rc == 0 and outside_log.read_text() == "keep\n"
                  and not (sb.logs / "ninja.log").is_symlink(), (rc, err[-300:]))
            sb = sandbox()
            (sb.sb / "empty").mkdir()
            rc, out, err = sb.run(["build", "ninja"], NM_TC_PREFIX=str(sb.sb / "empty"))
            check("an existing empty folder is taken as the prefix", rc == 0, err[-300:])

        if arm("markerlink"):
            # a marker that is a link: the stamps and records (prefix) and the empty marker file (work, logs) are written
            # through it, so a link would carry those writes to wherever it points; it must be refused, the target untouched
            def markerlink_problems(sb, var, marker, script=None):
                d = sb.sb / ("m-" + var); d.mkdir()
                out_dir = sb.sb / "outside"; out_dir.mkdir(exist_ok=True)
                if marker == ".nm-toolchain":
                    target = out_dir / "state"; write(target / "ninja", "not the script's\n")
                else:
                    target = out_dir / (var + ".txt"); target.write_text("not the script's\n")
                before = tree_state(out_dir)
                os.symlink(target, d / marker)
                rc, out, err = sb.run(["build", "ninja"], script=script, **{var: str(d)})
                p = []
                if rc != 2 or "is a link" not in err: p.append(f"exit {rc}: {err.strip()[:300]!r}")
                if diff_states(before, tree_state(out_dir)): p.append(f"outside changed: {diff_states(before, tree_state(out_dir))[:5]}")
                return p
            for var, marker in (("NM_TC_PREFIX", ".nm-toolchain"), ("NM_TC_WORK", ".nm-toolchain-work"),
                                ("NM_TC_LOGS", ".nm-toolchain-logs")):
                sb = sandbox()
                p = markerlink_problems(sb, var, marker)
                check(f"{var} whose {marker} is a link is refused, the link's target untouched", not p, p)
                sb = sandbox()
                p = markerlink_problems(sb, var, marker, mutate(sb, "marker-link", [('if [ -L "$p/$m" ]; then die', 'if false; then die')]))
                control(f"a script without the marker-link test writes through {var}'s {marker}", p)
            sb = sandbox()
            d = sb.sb / "wrongkind"; (d / ".nm-toolchain").parent.mkdir(parents=True)
            (d / ".nm-toolchain").write_text("a file, not the script's folder\n")
            rc, out, err = sb.run(["build", "ninja"], NM_TC_PREFIX=str(d))
            check("a prefix whose .nm-toolchain is a file is refused, the file untouched",
                  rc == 2 and "is not a folder" in err and (d / ".nm-toolchain").read_text().startswith("a file"), (rc, err[-300:]))
            sb = sandbox()
            w = sb.sb / "wrongwork"; (w / ".nm-toolchain-work").mkdir(parents=True)
            rc, out, err = sb.run(["build", "ninja"], NM_TC_WORK=str(w))
            check("a work folder whose .nm-toolchain-work is a folder is refused", rc == 2 and "is not a plain file" in err, (rc, err[-300:]))
            sb = sandbox()
            w = sb.sb / "wrongwork2"; (w / ".nm-toolchain-work").mkdir(parents=True)
            bad = mutate(sb, "marker-kind", [('      *) [ -f "$p/$m" ] || die', '      *) true || die')])
            rc, out, err = sb.run(["build", "ninja"], script=bad, NM_TC_WORK=str(w))
            control("a script without the kind test takes a work folder whose marker is a folder", [] if rc == 0 else ["refused"])

        if arm("link"):
            def link_problems(sb, script=None):
                (sb.work / "inner").mkdir(parents=True)
                (sb.work / ".nm-toolchain-work").write_text("")
                os.symlink(sb.work / "inner", sb.sb / "prefix-link")
                rc, out, err = sb.run(["build", "ninja"], script=script, NM_TC_PREFIX=str(sb.sb / "prefix-link"))
                p = []
                if rc != 2 or "inside one another" not in err: p.append(f"exit {rc}: {err.strip()[:300]!r}")
                return p
            sb = sandbox()
            p = link_problems(sb)
            check("a prefix that is a link into the work folder is refused as lying inside it", not p, p)
            sb = sandbox()
            p = link_problems(sb, mutate(sb, "logical", [('p=$(cd "$p" && pwd -P) || return 1', 'p=$(cd "$p" && pwd -L) || return 1')]))
            control("judged by the link's own name, it passes", p)
            sb = sandbox()
            (sb.sb / "real-prefix").mkdir()
            os.symlink(sb.sb / "real-prefix", sb.sb / "plink")
            rc, out, err = sb.run(["build", "ninja"], NM_TC_PREFIX=str(sb.sb / "plink/tc"))
            check("a prefix below a link is created at its real place and used",
                  rc == 0 and (sb.sb / "real-prefix/tc/bin/ninja").exists(), err[-300:])

        if arm("record"):
            cases = [("..", "../outside.txt", None), ("absolute", "@SB@/outside.txt", None),
                     ("through a link", "away/outside.txt", None),
                     ("through a link back into the prefix", "inside/ctest",
                      [('[ "$r" = "$d" ] || record_damaged "$rec" "$n" "$rel"',
                        'case "$r/" in "$P/"*) ;; *) record_damaged "$rec" "$n" "$rel" ;; esac')])]
            for what, line, mut in cases:
                def rec_problems(sb, script=None):
                    sb.run(["build", "ninja", "cmake"])
                    out_f = sb.sb / "outside.txt"; out_f.write_text("keep\n")
                    os.symlink(sb.sb, sb.prefix / "away"); os.symlink(sb.prefix / "bin", sb.prefix / "inside")
                    rec = sb.prefix / ".nm-toolchain/cmake.files"
                    rec.write_text("bin/ctest\n" + line.replace("@SB@", str(sb.sb)) + "\n" + rec.read_text())
                    rc, out, err = sb.run(["build", "cmake"], script=script)
                    p = []
                    if rc != 2 or "the record is damaged" not in err: p.append(f"exit {rc}: {err.strip()[-300:]!r}")
                    if not out_f.exists(): p.append("the file outside the prefix was removed")
                    if not (sb.prefix / "bin/ctest").exists(): p.append("a file of the record was removed")
                    return p
                sb = sandbox()
                p = rec_problems(sb)
                check(f"a record line {what} removes nothing and stops: the record is damaged", not p, p)
                sb = sandbox()
                p = rec_problems(sb, mutate(sb, "no-record-check", mut or [('  check_record "$rec"\n', '\n')]))
                control(f"without the record check, a line {what} removes files", p)

        if arm("sysbin"):
            text = REAL_SCRIPT.read_text()
            check("the script runs xcrun and security from /usr/bin, set in one line",
                  text.count(SYSBIN_LINE) == 1 and '"$SYSBIN/$1"' in text and '"$(command -v "$1")" "${@:2}"' not in text)

            def sysbin_problems(sb, script=None):
                rc, out, err = sb.run(["build", "openssl"], script=script)
                p = [f"{r[0]} ran" for r in sb.records() if r[0].startswith("PATH-")]
                if not any(r[0] == "xcrun" for r in sb.records()): p.append("SYSBIN's xcrun did not run")
                if not any(r[0] == "security" for r in sb.records()): p.append("SYSBIN's security did not run")
                if rc != 0: p.append(f"exit {rc}")
                return p
            sb = sandbox()
            p = sysbin_problems(sb)
            check("xcrun and security are those of SYSBIN; the ones on the caller's PATH never run", not p, p)
            sb = sandbox()
            p = sysbin_problems(sb, mutate(sb, "path-helpers", [('"${e[@]}" "$SYSBIN/$1"', '"${e[@]}" "$(command -v "$1")"')]))
            control("a script that looks them up on PATH runs the caller's", p)
            diff = [l for l in sb.script.read_text().splitlines() if l not in text.splitlines()]
            check("the tested copy differs from build/toolchain.sh only in the SYSBIN line",
                  diff == [f"SYSBIN={sb.sb}/sysbin"], diff)

        if arm("mac"):
            def mac_problems(sb, script=None):
                rc, out, err = sb.run(["build"] + TOOLS, script=script)
                st = sb.steps(); p = [] if rc == 0 else [f"exit {rc}: {err[-300:]}"]
                first = lambda who: next((r for r in st if r[0] == who or r[0].startswith(who + ":")), None)
                want = {"cmake-bootstrap": ([], ["--generator=Ninja", "--no-system-libs", "-DCMAKE_USE_OPENSSL=OFF"]),
                        "openssl-configure": (["no-shared", "no-tests"], ["no-module", "no-apps", "no-docs"]),
                        "python-configure": ([f"LIBZSTD_LIBS={sb.prefix}/build-deps/lib/libzstd.a",
                                              f"LIBLZMA_LIBS={sb.prefix}/build-deps/lib/liblzma.a"],
                                             ["--without-readline", "--with-pkg-config=no", "--disable-test-modules",
                                              "py_cv_module_readline=n/a", "--without-system-libmpdec"]),
                        "cmake-configure:spirv-tools": ([], ["-DBUILD_SHARED_LIBS=ON", "-DSPIRV_TOOLS_BUILD_STATIC=OFF"]),
                        "cmake-configure:llvm": (["-DLLVM_ENABLE_EH=ON", "-DLLVM_ENABLE_RTTI=ON"], ["-DLLVM_ENABLE_EH=OFF"])}
                for who, (has, hasnt) in want.items():
                    r = first(who)
                    if not r: p.append(f"{who} did not run"); continue
                    p += [f"{who} lacks {a}" for a in has if a not in r[2]]
                    p += [f"{who} has {a}" for a in hasnt if a in r[2]]
                for who, sub in (("ninja-configure", "ninja"), ("cmake-bootstrap", "cmake"), ("openssl-configure", "openssl"),
                                 ("zstd-make", "zstd/lib"), ("python-configure", "cpython")):
                    r = first(who)
                    if r and r[1] != str(sb.work / sub.split("/")[0].replace("cpython", "python") / sub):
                        p.append(f"{who} ran in {r[1]}, not in a copy of the tree")
                if not any(r[0] == "cmake-configure:zstd" for r in st) and first("zstd-make") is None:
                    p.append("zstd was not built with its Makefile")
                return p
            sb = sandbox()
            p = mac_problems(sb)
            check("the recipes run what the Mac ran (bootstrap and make, zstd's Makefile, CPython's plain configure, "
                  "static SPIRV-Tools, LLVM with exceptions; copies of the trees)", not p, p)
            sb = sandbox()
            control("a script with the old CPython line is caught", mac_problems(sb, mutate(sb, "old-python", [(
                '--with-ensurepip=install \\\n', '--with-ensurepip=install --without-readline \\\n')])))
            sb = sandbox()
            control("a script with LLVM's exceptions off is caught", mac_problems(sb, mutate(sb, "eh-off", [(
                "-DLLVM_ENABLE_EH=ON", "-DLLVM_ENABLE_EH=OFF")])))

        if arm("id"):
            sb = sandbox()
            ids = {t: sb.run(["id", t])[1].strip() for t in TOOLS}
            check("id prints one line per tool, naming it", all(v.startswith(t + " ") and "\n" not in v for t, v in ids.items()), ids)
            bumped = mutate(sb, "bump", [("ninja) echo 2 ;;", "ninja) echo 3 ;;")])
            new = {t: sb.run(["id", t], script=bumped)[1].strip() for t in TOOLS}

            def id_problems(new_ids):
                p = [] if new_ids["ninja"] != ids["ninja"] else ["ninja's id did not change"]
                return p + [t for t in TOOLS if t != "ninja" and new_ids[t] != ids[t]]
            check("a raised recipe number changes that tool's id and no other", not id_problems(new), new)
            blind = mutate(sb, "blind", [("ninja) echo 2 ;;", "ninja) echo 3 ;;"),
                                         ("\"$(recipe_number \"$1\")\"", "1")])
            control("an id that ignores the recipe number does not show the raise",
                    id_problems({t: sb.run(["id", t], script=blind)[1].strip() for t in TOOLS}))
            (sb.sb / "control/arch").write_text("arm64\n")
            check("the processor is part of the id", sb.run(["id", "ninja"])[1].strip() != ids["ninja"])

        if arm("env"):
            sb = sandbox()

            def env_problems(script=None):
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
                    if r[0] in ("xcrun", "security", "otool", "PATH-xcrun", "PATH-security"):
                        p.append(f"{r[0]} ran")
                ossl = [r for r in sb.steps() if r[0] == "openssl-configure"]
                if not ossl or any(a.startswith("darwin64") for a in ossl[0][2]):
                    p.append(f"openssl configure: {ossl[:1]}")
                if any("SDKROOT" in r[3] or "MACOSX_DEPLOYMENT_TARGET" in r[3] for r in sb.steps()):
                    p.append("a step has SDKROOT or MACOSX_DEPLOYMENT_TARGET")
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
            text = REAL_SCRIPT.read_text()
            check("nothing that bash 3.2 or the BSD tools lack", not scan(text), scan(text))
            control("a mapfile and a sed -i are reported", scan(text + "\nmapfile -t x < f\nsed -i 's/a/b/' f\n"))
            # an empty array expanded bare stops bash 3.2 under set -u: every possibly empty one is written ${a[@]+...}
            bare = [m.group(0) for m in re.finditer(r'(?<!\+)"\$\{(target|tls)\[@\]\}"', text)]
            check("the arrays that can be empty are expanded with ${a[@]+...}", not bare, bare)

    print("all passed" if not fails else f"FAILED {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # a check that failed may leave a later arm without what it works on: that is one more failure, said with
        # where it happened, never a bare traceback that hides the count
        import traceback
        check("the test ran to its end", False, traceback.format_exc()[-1000:])
        print(f"FAILED {fails}")
        sys.exit(1)
