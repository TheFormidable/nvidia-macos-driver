#!/usr/bin/env python3
"""./build.sh, the one-command build, run for real in a sandbox that imitates a Mac.

build.sh is written for a tester's Mac, where it installs tools, downloads sources and runs every build script of the
driver. None of that can happen here, so this runs the real build.sh against stand-ins and checks what it does:

  * a sandbox home folder, a scratch git clone of this tree inside it, and a PATH that holds only stand-ins and the
    basic tools (no /usr/bin): sw_vers, uname, sysctl, xcode-select, xcodebuild, xcrun, brew, python3, patch, make,
    curl, sudo, otool, codesign, ... Each stand-in appends its arguments (and the variables that matter) to a journal.
    git is the real one, behind a wrapper that journals; the remotes are local bare repositories reached through git's
    url.<base>.insteadOf, so build.sh asks for the real URLs and gets the stand-ins. The stand-in Homebrew "installs" a
    formula by creating its folder, rustup-init and cargo create toolchains, pip fills the virtual environment.
  * in the clone, every component build script build.sh runs is replaced by a stand-in that records its arguments and
    environment and creates the file build.sh expects; the original text stays below the stand-in's first lines, so the
    pin comparison still reads it. The stand-in can be told to fail or to wait.
  * the pins of the outside sources are rewritten, in the clone only, to the commits of the stand-in remotes.
  * build/toolchain.sh is a stand-in too ("toolchain" in fake.sh): it records each build with its NM_TC_* settings,
    needs the source trees and earlier tools a real build needs, lays out what build.sh looks for and writes the stamp.
    Every sandbox runs with NM_TOOLS_FROM=homebrew unless an arm says otherwise: the arms written before the source way
    are about Homebrew's behaviour. The source-way arms ("source"), NM_LLVM_PREFIX with Homebrew's translator
    ("ownllvm") and SDKROOT ("sdkroot") set what they test.

The imitated Macs never see a Homebrew of the machine the test runs on: build.sh looks in /usr/local and /opt/homebrew
before PATH, so every run names the stand-in through NM_BREW, which makes build.sh look nowhere else (arm "hostbrew";
arm "whichbrew" moves those two places into the sandbox, in its clone of build.sh only).

Arms (each with a negative control: the same check on a scratch copy of build.sh broken on purpose must fail):
  doctor on a fresh Mac, without Xcode (nothing that opens Apple's install dialog runs) and with it; no Homebrew of
  this machine is reached; which of two Homebrews (Intel, Apple silicon,
  Rosetta); no tool of the other architecture's Homebrew taken from PATH ("pathtools"); what an Xcode-only Mac builds, tools tested for what they do, dependencies checked first ("needs");
  doctor, dry runs and env write nothing ("readonly"); no home path in what a tester pastes ("nohome");
  deps --yes; --no-install; fetch, and a tampered remote; fetch and the tester's git hooks, template and filters; --offline; --dry-run; the build order;
  the variables every step receives; skipping and rebuilding by stamp; a failing step and its report file; an
  interrupted run; two runs at once and a stale lock; paths with a space; Apple silicon and an unsupported macOS;
  "env"; clean; docs/BUILDING.md and docs/BUILDING.es.md against the script; build/pins.sh against the scripts and
  patch chain that consume the same sources; a static scan of build.sh for what bash 3.2 and BSD tools lack.

The shell that runs build.sh is $BUILD_SH_BASH (default /bin/bash, which on a Mac is the bash 3.2 build.sh is written
for). On another system set BUILD_SH_BASH to a bash 3.2 to test that; the test prints which one ran.
NMT_KEEP=1 keeps the sandbox folder. usage: test_build_sh.py [arm ...]"""
import hashlib, os, pathlib, re, shlex, shutil, signal, subprocess, sys, tempfile, threading, time, traceback

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASH = os.environ.get("BUILD_SH_BASH") or "/bin/bash"
fails = 0
ONLY = sys.argv[1:]


# A Homebrew of the machine this test runs on, in the two places build.sh looks at before PATH. An imitated Mac must
# never reach it: "deps --yes" would install formulas on the tester's own Mac.
HOST_BREW = [b for b in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew") if os.path.exists(b)]
# On a Mac the system's own make, python3 and git (/usr/bin) are small programs of Apple's that find the real tool
# through xcrun and xcodebuild, in the developer folder and SDK that DEVELOPER_DIR, SDKROOT and TOOLCHAINS name.
APPLE_TOOLS = sys.platform == "darwin"


# The arms run in two threads (see main): each arm's lines are kept and printed together when it ends.
_LOCK = threading.Lock()
_TLS = threading.local()


def emit(line):
    buf = getattr(_TLS, "buf", None)
    if buf is None:
        with _LOCK:
            print(line, flush=True)
    else:
        buf.append(line)


def check(name, ok, detail=""):
    global fails
    if _STOPPING.is_set():   # the test is being stopped: what the stopped runs gave is not a result
        return ok
    emit(("ok   " if ok else "FAIL ") + name + (f"  [{str(detail)[:1500]}]" if detail and not ok else ""))
    with _LOCK:
        fails += 0 if ok else 1
    return ok


def negative(name, problems):
    """A negative control: the broken script must make the arm's check report something."""
    return check(f"control: {name}", bool(problems), "the check found nothing wrong with the broken script")


def negative_without_apple_tools(name, problems_of):
    """A negative control whose broken script lets a caller's SDKROOT, TOOLCHAINS or DEVELOPER_DIR that names nothing
    reach what build.sh itself asks the system's make, python3 and git. On a Mac those are Apple's (APPLE_TOOLS): the
    two runs on GitHub's Macs in which they were handed an SDKROOT that names no SDK were the two that left xcodebuild's
    result bundles and Apple's marker of an install of the command line tools on demand
    (/private/tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress), which on a Mac with a desktop is taken
    to come with Apple's dialog. The other two variables reach the same programs. So such a control runs only where the
    system's tools are not Apple's; on a Mac it is named and left out. problems_of: the runs, not yet made."""
    if APPLE_TOOLS:
        emit(f"left out on a Mac, where it would hand Apple's own tools a setting that names nothing: control: {name}")
        return True
    return negative(name, problems_of())


# ------------------------------------------------------------------------------------------------- stand-in library
FAKE_SH = r'''#!/bin/bash
# fake.sh <role> <path called> <args...>: the stand-in tools of tools/test_build_sh.py
role=$1; self=$2; shift 2
LIB=${0%/*}; SB=$NMT_SB; S=$SB/state; J=$SB/journal
name=${self##*/}
rec() { { printf '%s' "$1"; shift; for a in "$@"; do printf ' %q' "$a"; done; printf '\n'; } >> "$J"; }
rec "$name" "$@"
mkwrap() {   # <path> <role> [version text]
  mkdir -p "${1%/*}"
  printf '#!/bin/bash\nNMT_SB=%q exec /bin/bash %q %q "$0" "$@"\n' "$SB" "$LIB/fake.sh" "$2" > "$1"; chmod 755 "$1"
  if [ $# -ge 3 ]; then printf '%s\n' "$3" > "$1.version"; fi
}
triple() { case "$(cat "$S/arch")" in arm64) echo aarch64-apple-darwin ;; *) echo x86_64-apple-darwin ;; esac; }
# links <name>: what Homebrew links into <prefix>/bin when it installs <name>, the commands of what it installs with it
# included (state/brew-links-<name>: lines "<formula> <command>"). Each command is a program in that formula's keg
# (Cellar/<formula>/1.0/bin), opt/<formula> leads to the keg when the formula has no opt folder yet, and bin/<command>
# becomes a link into the keg, as Homebrew makes it. A dead link is replaced, as Homebrew does with a link that leads
# nowhere; anything else in the way (a file, a link that leads somewhere) is left and the link is refused, as Homebrew's
# Keg#link does: the install goes on and brew ends with exit 1 (conflict=1), the formulas installed all the same. A
# formula all of whose commands were linked gets Homebrew's record of a linked keg, var/homebrew/linked/<formula>
conflict=0
links() {
  local f n k b=$SB/brew/bin bad=" " seen=" "
  [ -f "$S/brew-links-$1" ] || return 0
  while read -r f n; do
    [ -n "$n" ] || continue
    k=$SB/brew/Cellar/$f/1.0; seen="$seen$f "
    [ -e "$k/bin/$n" ] || mkwrap "$k/bin/$n" plain
    [ -e "$SB/brew/opt/$f" ] || { mkdir -p "$SB/brew/opt"; rm -f "$SB/brew/opt/$f"; ln -s "../Cellar/$f/1.0" "$SB/brew/opt/$f"; }
    mkdir -p "$b"
    if [ -L "$b/$n" ] && [ ! -e "$b/$n" ]; then rm -f "$b/$n"; fi
    if [ -L "$b/$n" ] && [ "$(readlink "$b/$n")" = "../Cellar/$f/1.0/bin/$n" ]; then :
    elif [ -e "$b/$n" ] || [ -L "$b/$n" ]; then
      echo "Error: Could not symlink bin/$n" >&2; echo "Target $b/$n already exists." >&2; conflict=1; bad="$bad$f "
    else ln -s "../Cellar/$f/1.0/bin/$n" "$b/$n"; fi
  done < "$S/brew-links-$1"
  for f in $seen; do
    case "$bad" in *" $f "*) continue ;; esac
    mkdir -p "$SB/brew/var/homebrew/linked"; rm -f "$SB/brew/var/homebrew/linked/$f"; ln -s "../../../Cellar/$f/1.0" "$SB/brew/var/homebrew/linked/$f"
  done
}
formula() {   # <name>: what the stand-in Homebrew puts under opt/<name>
  local p=$SB/brew/opt/$1
  case "$1" in
    gpatch) mkwrap "$p/bin/gpatch" patch "GNU patch 2.8" ;;
    ninja) mkwrap "$p/bin/ninja" ver "1.13.1" ;;
    cmake) mkwrap "$p/bin/cmake" cmake "cmake version 4.1.2" ;;
    pkgconf) mkwrap "$p/bin/pkg-config" ver "2.5.1" ;;
    glslang) mkwrap "$p/bin/glslangValidator" ver "Glslang Version: 11:16.0.0" ;;
    spirv-tools) mkdir -p "$p/lib/pkgconfig"; printf 'Name: SPIRV-Tools\nVersion: 2025.4\n' > "$p/lib/pkgconfig/SPIRV-Tools.pc" ;;
    spirv-llvm-translator) mkdir -p "$p/lib/pkgconfig"; printf 'Name: LLVMSPIRVLib\nVersion: 22.1.0\n' > "$p/lib/pkgconfig/LLVMSPIRVLib.pc"
                           mkwrap "$p/bin/llvm-spirv" ver "LLVM version 22.1.0" ;;
    llvm|llvm@22) mkwrap "$p/bin/llvm-config" ver "22.1.3"; mkdir -p "$p/lib"; : > "$p/lib/libclang.dylib" ;;
    python@3.14) mkwrap "$p/bin/python3.14" py "3.14.0" ;;
    *) return 1 ;;
  esac
}
case "$role" in
  ver) case "${1:-}" in --version|-version|-v|version) cat "$self.version" ;; esac ;;
  # a patch that works: build.sh's own test of a patch (a/f.txt in a temporary folder) is applied by the real patch of
  # the machine running the test; the patch chains are not applied to the stand-in NVIDIA trees (accepted, as before).
  # And one that does not take the options the scripts pass, as a patch older than macOS's might not
  patch) case "${1:-}" in --version) cat "$self.version"; exit 0 ;; esac
         in=$(cat); case "$in" in *"diff --git a/a/f.txt"*) printf '%s\n' "$in" | "$(cat "$LIB/realpatch")" "$@" ;; esac ;;
  patchold) case "${1:-}" in --version) cat "$self.version" ;; esac
            case " $* " in *" --no-backup-if-mismatch "*|*" --fuzz=0 "*) echo "patch: unrecognized option" >&2; exit 2 ;; esac
            exec "$(cat "$LIB/realpatch")" "$@" ;;
  patchnonew) case "${1:-}" in --version) cat "$self.version"; exit 0 ;; esac   # changes files, cannot create one
              in=$(cat); case "$in" in *"--- /dev/null"*) echo "patch: cannot create files" >&2; exit 1 ;; esac
              case "$in" in *"diff --git a/a/f.txt"*) printf '%s\n' "$in" | "$(cat "$LIB/realpatch")" "$@" ;; esac ;;
  # macOS's patch as it treats a patch whose file is not there (the patch chains, for the stand-in trees): without -f
  # it asks on /dev/tty, whatever its output goes to, and waits; here it records that it would have, and fails
  patchask) case "${1:-}" in --version) cat "$self.version"; exit 0 ;; esac
            in=$(cat); case "$in" in *"diff --git a/a/f.txt"*) printf '%s\n' "$in" | "$(cat "$LIB/realpatch")" "$@"; exit ;; esac
            case " $* " in *" -f "*|*" --force "*) exit 0 ;; esac
            rec patch-asked "$@"; exit 1 ;;
  # build.sh's wait for a lock that has no owner yet: the run that made it writes its owner meanwhile
  sleepowner) l=$(cat "$S/lockdir"); [ -f "$l/owner" ] || printf 'pid=%s\nstarted=x\ncommand=build\n' "$(cat "$S/livepid")" > "$l/owner" ;;
  uname)
    case "${1:-}" in
      -s|'') cat "$S/os" 2>/dev/null || echo Darwin ;; -m|-p) cat "$S/arch" ;; -r) echo 24.6.0 ;;
      *) echo "Darwin 24.6.0 $(cat "$S/arch")" ;;
    esac ;;
  sw_vers)
    case "${1:-}" in -productVersion) cat "$S/macos" ;; -buildVersion) echo 24G90 ;; *) printf 'ProductName:\tmacOS\nProductVersion:\t%s\n' "$(cat "$S/macos")" ;; esac ;;
  sysctl)
    [ "${1:-}" = -n ] && shift
    case "${1:-}" in
      hw.ncpu) echo 8 ;; hw.model) echo MacPro7,1 ;; hw.memsize) echo 34359738368 ;;
      hw.optional.arm64) [ "$(cat "$S/cpu")" = arm64 ] || { echo "sysctl: unknown oid 'hw.optional.arm64'" >&2; exit 1; }; echo 1 ;;
      machdep.cpu.brand_string) echo "Intel(R) Xeon(R) W-3245 CPU @ 3.20GHz" ;; *) exit 1 ;;
    esac ;;
  xcode-select)
    # like Apple's: DEVELOPER_DIR first, then the selected folder (state/devdir: one that may no longer be there), else
    # the sandbox's Xcode.app once "Xcode is installed"
    if [ -n "${DEVELOPER_DIR:-}" ]; then d=$DEVELOPER_DIR
    elif [ -f "$S/devdir" ]; then d=$(cat "$S/devdir")
    elif [ -f "$S/xcode" ]; then d=$SB/Xcode.app/Contents/Developer
    else echo "xcode-select: error: unable to get active developer directory" >&2; exit 2; fi
    case "${1:-}" in -p|--print-path) echo "$d" ;; *) exit 1 ;; esac ;;
  xcodebuild)
    [ -f "$S/xcode" ] || { echo "xcode-select: error: tool 'xcodebuild' requires Xcode" >&2; exit 1; }
    case "${1:-}" in -version) printf 'Xcode %s\nBuild version 17A400\n' "$(cat "$S/xcode")" ;; *) exit 1 ;; esac ;;
  xcrun)
    [ -f "$S/xcode" ] || { echo "xcrun: error: invalid active developer path" >&2; exit 1; }
    # like Apple's: a set SDKROOT is the SDK it names (build.sh's steps get the one --sdk macosx gave)
    if [ "${1:-}" = --sdk ]; then shift 2
    elif [ -n "${SDKROOT:-}" ] && [ "${1:-}" = --show-sdk-path ]; then echo "$SDKROOT"; exit 0; fi
    case "${1:-}" in
      --show-sdk-path) echo "$SB/sdk" ;;
      --show-sdk-version) cat "$S/sdk" ;;
      -f|--find) echo "/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/$2" ;;
      clang|clang++) echo "Apple clang version 17.0.0 (clang-1700.4.4.1)" ;;
      ld) echo "@(#)PROGRAM:ld PROJECT:ld-1221.4" >&2 ;;
      *) exit 0 ;;
    esac ;;
  brew)
    # every call, whatever it asks: the three variables a login shell exports for a Homebrew, the PATH Homebrew keeps
    # for post-install steps and its "std" builds, and the two that make Homebrew's bin/brew take HOMEBREW_PATH as PATH
    rec brew-callenv "HOMEBREW_PREFIX=${HOMEBREW_PREFIX:-}" "HOMEBREW_CELLAR=${HOMEBREW_CELLAR:-}" "HOMEBREW_REPOSITORY=${HOMEBREW_REPOSITORY:-}" \
        "PATH=$PATH" "HOMEBREW_PATH=${HOMEBREW_PATH:-}" "HOMEBREW_BREW_FILE=${HOMEBREW_BREW_FILE:-}"
    case "${1:-}" in
      --version) echo "Homebrew 4.6.3" ;;
      --prefix) echo "$SB/brew" ;;
      info)
        # like Homebrew: the formula list is downloaded into its cache, ~/Library/Caches/Homebrew unless HOMEBREW_CACHE is set
        c=${HOMEBREW_CACHE:-$HOME/Library/Caches/Homebrew}/api; mkdir -p "$c" && echo "{}" > "$c/formula.jws.json"
        rec brew-info "HOMEBREW_CACHE=${HOMEBREW_CACHE:-}" "HOMEBREW_PREFIX=${HOMEBREW_PREFIX:-}" "HOMEBREW_CELLAR=${HOMEBREW_CELLAR:-}" "HOMEBREW_REPOSITORY=${HOMEBREW_REPOSITORY:-}"
        f=${3:-}
        case "$f" in llvm) v=$(cat "$S/brew-llvm") ;; llvm@22) [ -f "$S/brew-llvm22" ] || exit 1; v=22.1.3 ;;
          gpatch|ninja|cmake|pkgconf|glslang|spirv-tools|spirv-llvm-translator|python@3.14) v=1.0 ;; *) exit 1 ;; esac
        printf '{"formulae":[{"name":"%s","versions":{"stable": "%s"}}]}\n' "$f" "$v" ;;
      install)
        rec brew-env "HOMEBREW_NO_AUTO_UPDATE=${HOMEBREW_NO_AUTO_UPDATE:-}" "HOMEBREW_NO_INSTALL_UPGRADE=${HOMEBREW_NO_INSTALL_UPGRADE:-}" "HOMEBREW_NO_INSTALL_CLEANUP=${HOMEBREW_NO_INSTALL_CLEANUP:-}" "HOMEBREW_PREFIX=${HOMEBREW_PREFIX:-}" "HOMEBREW_CELLAR=${HOMEBREW_CELLAR:-}" "HOMEBREW_REPOSITORY=${HOMEBREW_REPOSITORY:-}"
        [ $# = 2 ] || { echo "stand-in brew: install takes one formula" >&2; exit 1; }
        # state/brew-install-fails-<formula>: the install fails after its dependencies were installed and linked, and
        # before the formula itself is (a build that failed)
        if [ -f "$S/brew-install-fails-$2" ]; then links "$2"; echo "Error: stand-in failure of brew install $2" >&2; exit 1; fi
        # ctl/brew-block-<formula>: the install waits (a long build) until the file is gone, at most two minutes. It
        # waits as the step stand-in does (STEPLIB_SH), with the shell's own timed read on a FIFO, so that a Ctrl-C is
        # never taken for dealt with by a child that ended in the same moment
        if [ -f "$SB/ctl/brew-block-$2" ]; then
          w=0; lim=2400; nap="sleep 0.05"
          if mkfifo "$SB/ctl/brew-nap-$$" 2>/dev/null && { exec 3<>"$SB/ctl/brew-nap-$$"; } 2>/dev/null; then
            if [ "${BASH_VERSINFO[0]}" -ge 4 ]; then nap="read -t 0.05 -u 3 _"; else nap="read -t 1 -u 3 _"; lim=120; fi
          else
            trap 'trap - INT; kill -INT $$' INT
          fi
          rm -f "$SB/ctl/brew-nap-$$"
          echo $$ > "$SB/ctl/brew-pid-$2"; : > "$SB/ctl/brew-started-$2"
          while [ -f "$SB/ctl/brew-block-$2" ]; do
            w=$((w + 1)); [ "$w" -le "$lim" ] || { echo "stand-in brew: install $2 never released" >&2; exit 9; }
            $nap || true
          done
          exec 3<&-
        fi
        formula "$2" || { echo "Error: No available formula with the name \"$2\"" >&2; exit 1; }
        links "$2"
        # like Homebrew: unless HOMEBREW_NO_INSTALL_CLEANUP is set, an install ends with its clean-up, which removes older
        # versions of formulas (every 30 days of all of them), formulas nothing needs and dead links in the prefix
        [ -n "${HOMEBREW_NO_INSTALL_CLEANUP:-}" ] || rm -rf "$SB/brew/Cellar/yours/1.0"
        echo "==> Pouring $2"
        [ "$conflict" = 0 ] || { echo "Error: The \`brew link\` step did not complete successfully" >&2; exit 1; } ;;
      # like Homebrew's Keg#unlink: the links in the prefix that lead into the formula's keg go; the keg and opt/<formula>
      # stay. state/brew-unlink-fails makes it fail
      unlink)
        [ $# = 2 ] || { echo "stand-in brew: unlink takes one formula" >&2; exit 1; }
        [ ! -f "$S/brew-unlink-fails" ] || { echo "Error: stand-in failure of brew unlink $2" >&2; exit 1; }
        n=0; for l in "$SB/brew/bin"/*; do
          [ -L "$l" ] || continue
          case "$(readlink "$l")" in "../Cellar/$2/"*) rm -f "$l"; n=$((n + 1)) ;; esac
        done
        rm -f "$SB/brew/var/homebrew/linked/$2"
        echo "Unlinking $SB/brew/Cellar/$2/1.0... $n symlinks removed." ;;
      *) echo "stand-in brew: '$1' is not something build.sh may do" >&2; exit 1 ;;
    esac ;;
  py)
    case "${1:-}" in
      --version|-V) echo "Python $(cat "$self.version")" ;;
      -m)
        case "${2:-}" in
          venv) rec sdkroot venv "${SDKROOT-UNSET}"; d=$3; mkwrap "$d/bin/python3" pyvenv "$(cat "$self.version")"; printf 'home = %s\n' "${self%/*}" > "$d/pyvenv.cfg" ;;
          pip) rec sdkroot pip "${SDKROOT-UNSET}"; [ "${self##*/}" = python3 ] && [ -f "${self%/bin/*}/pyvenv.cfg" ] || { echo "stand-in: pip outside a virtual environment" >&2; exit 1; }
               # like pip: what it downloads is kept in ~/Library/Caches/pip unless PIP_CACHE_DIR or PIP_NO_CACHE_DIR says otherwise
               [ -n "${PIP_NO_CACHE_DIR:-}" ] || { c=${PIP_CACHE_DIR:-$HOME/Library/Caches/pip}; mkdir -p "$c/http-v2" && : > "$c/http-v2/stand-in"; }
               mkwrap "${self%/*}/meson" ver "1.12.1"; mkwrap "${self%/*}/mako-render" ver "1.4.3" ;;
          *) exit 1 ;;
        esac ;;
      *run_all_tests.py) rec tests-env "NM_VULKAN_INCLUDE=${NM_VULKAN_INCLUDE-UNSET}"
                         if [ -f "$SB/ctl/tests-fail" ]; then echo "269 passed, 1 failed, 0 skipped, 0 not run, of 270"; exit 1; fi
                         echo "270 passed, 0 failed, 0 skipped, 0 not run, of 270" ;;
      *) exit 0 ;;
    esac ;;
  pyvenv) exec /bin/bash "$LIB/fake.sh" py "$self" "$@" ;;
  curl)
    out=""; url=""
    while [ $# -gt 0 ]; do case "$1" in -o) out=$2; shift ;; *://*) url=$1 ;; esac; shift; done
    case "$url" in file://*) src=${url#file://} ;; https://*) src=$SB/www/${url#https://} ;; *) exit 2 ;; esac
    [ -f "$src" ] || { echo "curl: (22) The requested URL returned error: 404" >&2; exit 22; }
    if [ -n "$out" ]; then cp "$src" "$out"; else cat "$src"; fi ;;
  rustup-init)
    case " $* " in *" --no-modify-path "*) ;; *) echo 'source "$HOME/.cargo/env"' >> "$HOME/.profile" ;; esac
    ch=${CARGO_HOME:-$HOME/.cargo}; mkdir -p "${RUSTUP_HOME:-$HOME/.rustup}"
    mkwrap "$ch/bin/rustup" rustup; mkwrap "$ch/bin/cargo" cargo ;;
  rustup)
    rh=${RUSTUP_HOME:-$HOME/.rustup}
    case "$1 $2" in
      "toolchain install") t=$rh/toolchains/$3-$(triple); mkwrap "$t/bin/rustc" rustc; mkwrap "$t/bin/cargo" cargo; mkdir -p "$t/lib/rustlib/$(triple)/lib" ;;
      "target add") mkdir -p "$rh/toolchains/$4-$(triple)/lib/rustlib/$5/lib" ;;
      *) exit 1 ;;
    esac ;;
  rustc)
    t=${self%/bin/rustc}; v=${t##*/}; v=${v%%-*}
    case "$1" in --version) echo "rustc $v (stand-in 2026-10-01)" ;; --print) echo "$t/lib/rustlib/$4/lib" ;; esac ;;
  cargo)
    case "${1:-}" in
      install) root=""; ver=""; what=""
        shift; while [ $# -gt 0 ]; do case "$1" in --root) root=$2; shift ;; --version) ver=$2; shift ;; --locked) ;; *) what=$1 ;; esac; shift; done
        [ -n "$root" ] || root=${CARGO_HOME:-$HOME/.cargo}
        case "$what" in bindgen-cli) mkwrap "$root/bin/bindgen" ver "bindgen $ver" ;; *) mkwrap "$root/bin/$what" ver "$what $ver" ;; esac ;;
      fetch|--version) : ;;
      *) exit 1 ;;
    esac ;;
  cmake)   # configure records the install prefix; --install lays out what SPIRV-LLVM-Translator installs
    case "${1:-}" in -S|--build|--install) rec sdkroot "cmake${1}" "${SDKROOT-UNSET}" ;; esac
    case "${1:-}" in
      --version) cat "$self.version" ;;
      -S) b=""; pre=""; prev=""
          for a in "$@"; do [ "$prev" = -B ] && b=$a; case "$a" in -DCMAKE_INSTALL_PREFIX=*) pre=${a#*=} ;; esac; prev=$a; done
          [ -f "$2/spirv-headers-tag.conf" ] || { echo "stand-in cmake: $2 is not SPIRV-LLVM-Translator" >&2; exit 1; }
          mkdir -p "$b"; printf '%s\n' "$pre" > "$b/.prefix" ;;
      --build) [ -f "$2/.prefix" ] || exit 1 ;;
      --install) pre=$(cat "$2/.prefix"); mkdir -p "$pre/lib/pkgconfig"
                 printf 'Name: LLVMSPIRVLib\nVersion: 22.1.0.0\n' > "$pre/lib/pkgconfig/LLVMSPIRVLib.pc"; mkwrap "$pre/bin/llvm-spirv" ver "LLVM version 22.1.0" ;;
      *) exit 1 ;;
    esac ;;
  strings) cat "${@: -1}" ;;   # the stand-in archives are text
  # macOS's "file": a program with a <path>.arch beside it is a Mach-O of that architecture (two words: universal)
  file) f=${@: -1}
        if [ -f "$f.arch" ]; then a=$(cat "$f.arch")
          case "$a" in *" "*) echo "Mach-O universal binary with 2 architectures: [x86_64:Mach-O 64-bit executable x86_64] [arm64]" ;;
                       *) echo "Mach-O 64-bit executable $a" ;; esac
        else echo "Bourne-Again shell script text executable, ASCII text"; fi ;;
  otool) printf '%s:\n\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)\n' "${2:-x}"; cat "$S/otool-extra" 2>/dev/null ;;
  sudo) echo "stand-in sudo: build.sh must never call sudo" >&2; exit 1 ;;
  df) printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/disk1s1 976490576 1 %s 50%% /\n' "$(cat "$S/df-kb" 2>/dev/null || echo 500000000)" ;;
  toolchain)
    # build/toolchain.sh: list, id (state/tc-recipe-<tool> raises a recipe number), build (records the call and its
    # NM_TC_* settings, needs the source trees and the earlier tools, lays out what build.sh looks for, writes the
    # stamp NM_TC_STAMP; ctl/tc-fail-<tool> makes that tool fail with its log)
    tcs="ninja cmake openssl zstd xz python pkgconf spirv-tools glslang llvm"
    case "${1:-}" in
      list) printf '%s\n' $tcs ;;
      id) echo "$2 recipe $(cat "$S/tc-recipe-$2" 2>/dev/null || echo 2) Darwin-$(cat "$S/arch") macos${MACOSX_DEPLOYMENT_TARGET:-15.0}" ;;
      build) shift
        P=$NM_TC_PREFIX
        for t in "$@"; do
          rec toolchain-build "$t" "NM_TC_PREFIX=$NM_TC_PREFIX" "NM_TC_SRC=$NM_TC_SRC" "NM_TC_WORK=$NM_TC_WORK" "NM_TC_LOGS=$NM_TC_LOGS" \
              "NM_TC_JOBS=$NM_TC_JOBS" "NM_TC_MESON=$NM_TC_MESON" "NM_TC_STAMP=$NM_TC_STAMP" "MACOSX_DEPLOYMENT_TARGET=${MACOSX_DEPLOYMENT_TARGET-UNSET}"
          mkdir -p "$NM_TC_LOGS" "$NM_TC_WORK" "$P/.nm-toolchain"; L=$NM_TC_LOGS/$t.log; echo "build $t" > "$L"
          [ -d "$NM_TC_SRC/$t" ] || { echo "STOP: $t: its source tree $NM_TC_SRC/$t is missing" >&2; exit 2; }
          [ "$t" != spirv-tools ] || [ -d "$NM_TC_SRC/spirv-headers" ] || { echo "STOP: no spirv-headers" >&2; exit 2; }
          case "$t" in cmake|pkgconf|xz|spirv-tools|glslang|llvm) [ -s "$P/.nm-toolchain/ninja" ] || { echo "STOP: $t needs the toolchain's ninja" >&2; exit 2; } ;; esac
          case "$t" in xz|spirv-tools|glslang|llvm) [ -s "$P/.nm-toolchain/cmake" ] || { echo "STOP: $t needs the toolchain's cmake" >&2; exit 2; } ;; esac
          case "$t" in python) for d in openssl zstd xz; do [ -s "$P/.nm-toolchain/$d" ] || { echo "STOP: python needs $d" >&2; exit 2; }; done ;; esac
          case "$t" in pkgconf) [ -x "$NM_TC_MESON" ] || { echo "STOP: pkgconf needs meson ($NM_TC_MESON)" >&2; exit 2; } ;; esac
          rm -f "$P/.nm-toolchain/$t"
          echo "toolchain: building $t (log: $L)"
          if [ -f "$SB/ctl/tc-fail-$t" ]; then echo "error: STANDIN-TC-FAILURE in $t" >> "$L"; echo "toolchain: $t FAILED (log: $L)" >&2; exit 1; fi
          case "$t" in
            ninja) mkwrap "$P/bin/ninja" ver "1.13.2" ;;
            cmake) mkwrap "$P/bin/cmake" cmake "cmake version 4.4.5" ;;
            openssl|zstd|xz) mkdir -p "$P/build-deps/lib"; : > "$P/build-deps/lib/lib$t.a" ;;
            python) mkwrap "$P/python/bin/python3.14" py "3.14.10" ;;
            pkgconf) mkwrap "$P/bin/pkgconf" ver "2.5.1"; mkwrap "$P/bin/pkg-config" ver "2.5.1" ;;
            spirv-tools) mkdir -p "$P/lib/pkgconfig"; printf 'Name: SPIRV-Tools\nVersion: 2026.4.1\n' > "$P/lib/pkgconfig/SPIRV-Tools.pc" ;;
            glslang) mkwrap "$P/bin/glslang" ver "Glslang Version: 11:16.6.0"; mkwrap "$P/bin/glslangValidator" ver "Glslang Version: 11:16.6.0" ;;
            llvm) mkwrap "$P/llvm/bin/llvm-config" ver "22.1.8"; mkdir -p "$P/llvm/lib"; : > "$P/llvm/lib/libclang.dylib" ;;
          esac
          printf '%s\n' "${NM_TC_STAMP:-$t}" > "$P/.nm-toolchain/$t"
          echo "toolchain: $t ok (1 s)"
        done ;;
      *) echo "usage" >&2; exit 2 ;;
    esac ;;
  plain) : ;;
  *) echo "fake.sh: unknown role $role" >&2; exit 1 ;;
esac
'''

# The component scripts build.sh runs, with the key the stand-in uses.
STEP_SCRIPTS = {
    "build/build_rm620.sh": "rm", "build/build_kms620.sh": "kms", "build/build_nvrm.sh": "nvrm", "build/fb_build.sh": "fb",
    "build/accel_build_all.sh": "accel", "build/accel_build.sh": "accel1", "build/build_agdc.sh": "agdc",
    "kexts/NMSMN/build.sh": "nmsmn", "tools/ryzenadj-macos/build.sh": "ryzenadj", "build/build_air_dis.sh": "airdis",
    "build/build_dlss_cache.sh": "dlss", "build/build_osver.sh": "osver", "build/build_nvk.sh": "nvk",
    "tools/nvmtl-boost/build.sh": "boost", "build/build_vulkan_loader.sh": "vulkan", "build/build_plugin_release.sh": "plugin",
    "build/build_plugin_dev.sh": "plugindev", "build/build_xlate_release.sh": "xlate", "build/build_nvencvt.sh": "nvencvt",
    "build/build_primary_selector.sh": "selector", "build/package_local.sh": "package",
}
STEPLIB_SH = r'''#!/bin/bash
# steplib.sh <key> <script> <args...>: a component build script's stand-in (tools/test_build_sh.py)
key=$1; shift 2
SB=$NMT_SB; J=$SB/journal
mkdir -p "$SB/steps"
n=$(ls "$SB/steps" | wc -l); n=$((n / 2 + 1)); f=$(printf '%s/steps/%03d-%s' "$SB" "$n" "$key")
env > "$f.env"
printf '%s\n' "$@" > "$f.args"
{ printf 'step %s' "$key"; for a in "$@"; do printf ' %q' "$a"; done; printf '\n'; } >> "$J"
echo "stand-in for $key: started"
# told to wait: until released, until the test takes the block away (or its sandbox is gone), at most two minutes;
# a stand-in that outlives its run must not wait for ever.
# It waits with the shell's own timed "read" on a FIFO that never delivers, and from "started" to the end of the wait
# nothing but the shell itself runs. A "sleep" every 50 ms instead loses about one Ctrl-C in eighty (a SIGINT to the
# run's process group, as the test sends it) under a bash 3.2 on a busy machine: a bash that is waiting for a child
# takes a SIGINT for dealt with when that child ends by itself in the same moment, so the stand-in naps on and
# build.sh waits for it. (And 2400 forks are slow.) bash 3.2 cannot time out a read in less than a second, so there a
# release is seen up to a second late.
# ctl/sigint-exits-<key>: the stand-in is a tool that deals with the interrupt itself and ends with a status of its
# own, as ninja and cargo do
if [ -f "$SB/ctl/block-$key" ]; then
  w=0; lim=2400; nap="sleep 0.05"
  if mkfifo "$SB/ctl/nap-$$" 2>/dev/null && { exec 3<>"$SB/ctl/nap-$$"; } 2>/dev/null; then
    if [ "${BASH_VERSINFO[0]}" -ge 4 ]; then nap="read -t 0.05 -u 3 _"; else nap="read -t 1 -u 3 _"; lim=120; fi
  else
    # no FIFO to be had: the sleep it is, with a trap, which bash acts on in all but the rarest of those moments
    trap 'trap - INT; kill -INT $$' INT
  fi
  rm -f "$SB/ctl/nap-$$"
  [ ! -f "$SB/ctl/sigint-exits-$key" ] || trap 'echo "stand-in for $key: stopped, interrupted by user"; exit 2' INT
  echo $$ > "$SB/ctl/pid-$key"; : > "$SB/ctl/started-$key"
  while [ ! -f "$SB/ctl/release-$key" ] && [ -f "$SB/ctl/block-$key" ]; do
    w=$((w + 1)); [ "$w" -le "$lim" ] || { echo "stand-in for $key: never released"; exit 9; }
    $nap || true
  done
  exec 3<&-
fi
if [ -f "$SB/ctl/fail-$key" ]; then echo "error: the compiler stopped in $key (stand-in failure)"; echo "make: *** [all] Error 1"; exit 7; fi
# ctl/step-git: a script making the kinds of git call the component scripts make, in stand-in trees (arm stepgit)
[ ! -f "$SB/ctl/step-git" ] || /bin/bash "$SB/ctl/step-git" "$key" || { echo "stand-in for $key: its git calls failed"; exit 8; }
mk() { mkdir -p "${1%/*}"; printf 'built by the stand-in for %s\n' "$key" > "$1"; }
# like NVIDIA's archives (libtool records each member's time): a rebuild of rm or kms is never byte-identical
mknew() { mk "$1"; printf 'build %s-%s\n' "$$" "$RANDOM" >> "$1"; }
case $key in
  rm) mknew "$1/ogkm-$NV_VERSION/src/nvidia/_out/Darwin_x86_64/libnvkernel.a"; echo "failed: 0 of 1234" ;;
  kms) mknew "$1/ogkm-$NV_VERSION/src/nvidia-modeset/_out/Darwin_x86_64/libnvkms.a" ;;
esac
# the end of build_rm620.sh / build_kms620.sh when their whole-word search finds the account or host name: the archive is
# written, then exit 6 (ctl/personal-<key> holds the archive's text)
case $key in
  rm|kms) if [ -f "$SB/ctl/personal-$key" ]; then
            a=$1/ogkm-$NV_VERSION/src/nvidia/_out/Darwin_x86_64/libnvkernel.a
            [ $key = kms ] && a=$1/ogkm-$NV_VERSION/src/nvidia-modeset/_out/Darwin_x86_64/libnvkms.a
            cp "$SB/ctl/personal-$key" "$a"; echo "STOP: 1 string(s) in ${a##*/} name this account or computer as a word"; exit 6
          fi ;;
esac
case $key in
  nvrm) mk "$3/NVRM.kext/Contents/MacOS/NVRM" ;;
  fb) mk "$2/NVRMFB.kext/Contents/MacOS/NVRMFB" ;;
  accel) mk "$2/15/NVAccel.kext/Contents/MacOS/NVAccel"; mk "$2/26/NVAccel.kext/Contents/MacOS/NVAccel" ;;
  accel1) mk "$2/NVAccel.kext/Contents/MacOS/NVAccel" ;;
  agdc) mk "$2/NVRMAGDC.kext/Contents/MacOS/NVRMAGDC" ;;
  nmsmn) mk "$1/NMSMN.kext/Contents/MacOS/NMSMN" ;;
  ryzenadj) mk "$2/ryzenadj" ;;
  airdis) mk "$1/nvmtl-air-dis" ;;
  dlss) mk "$1/dlss/nm_unzstd" ;;
  osver) mk "$1/libnvmtl_osver.dylib" ;;
  nvk) mk "$3/libvulkan_nouveau.dylib"; mk "$3/BUILD.txt"; mkdir -p "$3/mesa" ;;
  boost) mk "$2" ;;
  vulkan) mk "$1/libvulkan.1.dylib" ;;
  plugin) mk "$2/NVMTLDriver" ;;
  plugindev) mk "$1/NVMTLDriver" ;;
  xlate) mk "$2/libnvmtl_translate.dylib" ;;
  nvencvt) mk "$2/libnvencvt.dylib" ;;
  selector) mk "$1/NVPrimarySelector.kext/Contents/MacOS/NVPrimarySelector" ;;
  package) mk "$3/pkgroot/SHA256SUMS" ;;
esac
echo "stand-in for $key: done"
'''

# Real tools the sandbox PATH offers besides the stand-ins (a Mac has each of them in /usr/bin or /bin).
REAL_TOOLS = ["awk", "sed", "grep", "cut", "tr", "sort", "head", "tail", "cat", "cp", "mv", "rm", "mkdir", "rmdir", "ls", "ln",
              "chmod", "date", "mktemp", "dirname", "basename", "wc", "paste", "cmp", "diff", "tee", "env", "kill", "sleep",
              "tar", "find", "getconf", "touch", "expr", "uniq", "od", "xargs", "perl", "shasum", "id", "hostname", "printf",
              "true", "false", "comm", "cksum", "sh", "pwd", "test", "less", "more", "readlink"]
STANDINS = {"df": "df", "uname": "uname", "sw_vers": "sw_vers", "sysctl": "sysctl", "xcode-select": "xcode-select",
            "xcodebuild": "xcodebuild", "xcrun": "xcrun", "curl": "curl", "otool": "otool", "sudo": "sudo",
            "strings": "strings"}
PLAIN = ["codesign", "plutil", "ditto", "md5", "xattr", "lipo", "vtool", "nm", "dwarfdump", "kmutil", "clang",
         "clang++", "cc", "install_name_tool", "libtool", "strip", "ssh", "rsync", "scp"]

TMP = pathlib.Path(os.path.realpath(tempfile.mkdtemp(prefix="nmbuildsh-")))
LIB = TMP / "lib"
REMOTES = TMP / "remotes"
TPL = TMP / "template"
GIT = shutil.which("git")
REALPATH = {t: shutil.which(t) for t in REAL_TOOLS}
GENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(TMP), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}


def stop(pr):
    """Kill a build.sh run started in its own session, and everything it started."""
    try:
        os.killpg(pr.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


# The build.sh runs going on, in both threads. Each runs in its own session, so Ctrl-C at the terminal reaches only
# this test: on Ctrl-C (or a crash) the main thread stops them all with what they started, and no new run starts, so
# that the second thread ends at once instead of going through its remaining arms, and nothing outlives the test.
_LIVE = set()
_STOPPING = threading.Event()


def track(pr):
    with _LOCK:
        _LIVE.add(pr)
    return pr


def stop_all():
    _STOPPING.set()
    with _LOCK:
        live = list(_LIVE)
    for pr in live:
        if pr.poll() is None:   # not yet reaped: its process group is still the one it made
            stop(pr)


def not_stopping():
    if _STOPPING.is_set():
        raise KeyboardInterrupt("the test is being stopped")


def git(*args, cwd=None, check_rc=True):
    r = subprocess.run([GIT, *map(str, args)], cwd=cwd, env=GENV, capture_output=True, text=True)
    if check_rc and r.returncode:
        raise RuntimeError(f"git {args}: {r.stderr}")
    return r.stdout.strip()


def make_repo(path, files, tags=(), bare_at=None):
    """A git repository with one commit of <files>; returns (bare path, commit)."""
    work = TMP / "repo-work" / path.name
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    git("init", "-q", "-b", "main", work)
    for rel, text in files.items():
        p = work / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git("add", "-A", cwd=work)
    git("commit", "-qm", f"stand-in for {path.name}", cwd=work)
    for t in tags:
        git("tag", "-a", "-m", t, t, cwd=work)
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(path, ignore_errors=True)
    git("clone", "-q", "--bare", work, path)
    return path, git("rev-parse", "HEAD", cwd=work)


MESON_BUILD = """project('mesa')
python_version_req = '>= 3.10'
if meson.version().version_compare('< 1.7.0')
_glslang_preamble_version = '12.2'
bad_bindgen = ['0.72.0', '0.73.0', '0.73.1']
"""


# the toolchain's sources (build/pins.sh NM_PIN_TC*): repository under github.com and the pinned tag
TC_REPOS = {"TCNINJA": ("ninja-build/ninja.git", "v1.13.2"), "TCCMAKE": ("Kitware/CMake.git", "v4.4.5"),
            "TCOPENSSL": ("openssl/openssl.git", "openssl-3.5.9"), "TCZSTD": ("facebook/zstd.git", "v1.5.7"),
            "TCXZ": ("tukaani-project/xz.git", "v5.8.4"), "TCPYTHON": ("python/cpython.git", "v3.14.10"),
            "TCPKGCONF": ("pkgconf/pkgconf.git", "pkgconf-2.5.1"),
            "TCSPIRVHEADERS": ("KhronosGroup/SPIRV-Headers.git", "vulkan-sdk-1.4.363.0"),
            "TCSPIRVTOOLS": ("KhronosGroup/SPIRV-Tools.git", "vulkan-sdk-1.4.363.0"),
            "TCGLSLANG": ("KhronosGroup/glslang.git", "16.6.0"), "TCLLVM": ("llvm/llvm-project.git", "llvmorg-22.1.8")}


def make_remotes(root, tamper=False):
    """Stand-ins for every remote build.sh fetches from, laid out by URL; returns {pin id: commit} and the file hashes."""
    gh = root / "github.com"
    commits = {}
    salt = "tampered\n" if tamper else ""
    # one repository holds both NVIDIA tags, as upstream does
    work = TMP / "repo-work" / ("ogkm-t" if tamper else "ogkm")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    git("init", "-q", "-b", "main", work)
    for ver in ("610.57.04", "620.70.06"):
        (work / "version.mk").write_text(f"NVIDIA_VERSION = {ver}\n{salt}")
        for d in ("src/common/sdk/nvidia/inc", "src/nvidia/arch/nvalloc/unix/include"):
            (work / d).mkdir(parents=True, exist_ok=True)
            (work / d / "nvos.h").write_text(f"/* {ver} */\n")
        git("add", "-A", cwd=work)
        git("commit", "-qm", ver, cwd=work)
        git("tag", "-a", "-m", ver, ver, cwd=work)
        commits["OGKM" + ver.split(".")[0]] = git("rev-parse", "HEAD", cwd=work)
    p = gh / "NVIDIA/open-gpu-kernel-modules.git"
    p.parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(p, ignore_errors=True)
    git("clone", "-q", "--bare", work, p)
    _, commits["MESA"] = make_repo(root / "gitlab.freedesktop.org/mesa/mesa.git",
                                   {"meson.build": MESON_BUILD + salt, "include/vulkan/vulkan_core.h": "#define VK_HEADER_VERSION 363\n",
                                    "include/vulkan/vulkan.h": '#include "vulkan_core.h"\n'},
                                   tags=["mesa-26.3.0-rc3"])
    _, commits["VKHEADERS"] = make_repo(gh / "KhronosGroup/Vulkan-Headers", {"README": "headers\n" + salt}, tags=["v1.4.367"])
    _, commits["VKLOADER"] = make_repo(gh / "KhronosGroup/Vulkan-Loader", {"README": "loader\n" + salt}, tags=["v1.4.367"])
    _, commits["OPENGPUDOC"] = make_repo(gh / "NVIDIA/open-gpu-doc.git", {"classes/video/clc9b0.h": "/* doc */\n" + salt})
    _, commits["SPIRVHEADERS"] = make_repo(gh / "KhronosGroup/SPIRV-Headers.git", {"include/spirv/unified1/spirv.hpp": "// headers\n" + salt})
    _, commits["SPIRVLLVM"] = make_repo(gh / "KhronosGroup/SPIRV-LLVM-Translator.git",
                                        {"spirv-headers-tag.conf": commits["SPIRVHEADERS"] + "\n", "CMakeLists.txt": "# stand-in\n" + salt},
                                        tags=["v22.1.8"])
    for pid, (path, tag) in TC_REPOS.items():
        if pid == "TCSPIRVHEADERS":   # the same repository as SPIRVHEADERS, which the translator's pin names by commit
            git("tag", tag, commits["SPIRVHEADERS"], cwd=gh / "KhronosGroup/SPIRV-Headers.git")
            commits[pid] = commits["SPIRVHEADERS"]
            continue
        _, commits[pid] = make_repo(gh / path, {"README": f"stand-in source of {pid}\n" + salt}, tags=[tag])
    _, commits["RYZENADJ"] = make_repo(gh / "FlyGoat/RyzenAdj.git", {"main.c": "int main(void){return 0;}\n" + salt}, tags=["v0.19.0"])
    www = root / "www/raw.githubusercontent.com/WebKit/WebKit"
    sums = {}
    for f, key in (("CMBaseObjectSPI.h", "WEBKIT_CMBASE"), ("VTVideoDecoderSPI.h", "WEBKIT_VTDEC")):
        d = www / "3bfbc200370710495ffa440abdd31b943c83ad7a/Source/ThirdParty/libwebrtc/Source/webrtc/webkit_sdk/WebKit"
        d.mkdir(parents=True, exist_ok=True)
        (d / f).write_text(f"/* {f} */\n" + salt)
        sums[key] = hashlib.sha256((d / f).read_bytes()).hexdigest()
    ri = {}
    for triple in ("x86_64-apple-darwin", "aarch64-apple-darwin"):
        d = root / "www/static.rust-lang.org/rustup/archive/1.29.3" / triple
        d.mkdir(parents=True, exist_ok=True)
        (d / "rustup-init").write_text(f'#!/bin/bash\nexec /bin/bash "$NMT_LIB/fake.sh" rustup-init "$0" "$@"\n# {triple}\n')
        ri[triple] = hashlib.sha256((d / "rustup-init").read_bytes()).hexdigest()
    return commits, sums, ri


def pin_value(text, name):
    m = re.search(rf"^{name}=(.*)$", text, re.M)
    return m.group(1).strip('"') if m else None


def build_template():
    """A scratch copy of this tree: component scripts replaced by stand-ins, pins pointed at the stand-in remotes."""
    LIB.mkdir(parents=True)
    (LIB / "fake.sh").write_text(FAKE_SH)
    (LIB / "steplib.sh").write_text(STEPLIB_SH)
    (LIB / "realpatch").write_text(shutil.which("patch") or "/usr/bin/patch")
    commits, sums, ri = make_remotes(REMOTES)
    names = subprocess.run([GIT, "ls-files", "-z", "-c", "-o", "--exclude-standard"], cwd=ROOT, capture_output=True,
                           env=GENV).stdout.decode().split("\0")
    for n in names:
        src = ROOT / n
        if not n or not src.is_file() or n.startswith("tools/test_build_sh"):
            continue
        dst = TPL / n
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst, follow_symlinks=False)
    pins = (TPL / "build/pins.sh").read_text()
    replace = {}
    for pid in ("OGKM620", "OGKM610", "MESA", "VKHEADERS", "VKLOADER", "OPENGPUDOC", "RYZENADJ", "SPIRVLLVM", "SPIRVHEADERS"):
        replace[pin_value(pins, f"NM_PIN_{pid}_COMMIT")] = commits[pid]
    for pid in ("WEBKIT_CMBASE", "WEBKIT_VTDEC"):
        replace[pin_value(pins, f"NM_PIN_{pid}_SHA256")] = sums[pid]
    replace[pin_value(pins, "NM_PIN_RUSTUP_SHA256_X86_64")] = ri["x86_64-apple-darwin"]
    replace[pin_value(pins, "NM_PIN_RUSTUP_SHA256_AARCH64")] = ri["aarch64-apple-darwin"]
    for rel in ["build/pins.sh", "nvk/MESA_BASE", *STEP_SCRIPTS]:
        p = TPL / rel
        t = p.read_text()
        for old, new in replace.items():
            t = t.replace(old, new)
        if rel == "nvk/MESA_BASE":
            t = commits["MESA"][:8] + "\n"
        p.write_text(t)
    for rel, key in STEP_SCRIPTS.items():
        p = TPL / rel
        p.write_text(f'#!/bin/bash\n# stand-in written by tools/test_build_sh.py; the original text below never runs\n'
                     f'exec /bin/bash "$NMT_LIB/steplib.sh" {key} "$0" "$@"\n' + p.read_text())
    for pid in TC_REPOS:
        replace_pin = pin_value(pins, f"NM_PIN_{pid}_COMMIT")
        p = TPL / "build/pins.sh"
        p.write_text(p.read_text().replace(replace_pin, commits[pid]))
    p = TPL / "build/toolchain.sh"
    p.write_text('#!/bin/bash\n# stand-in written by tools/test_build_sh.py; the original text below never runs\n'
                 'exec /bin/bash "$NMT_LIB/fake.sh" toolchain "$0" "$@"\n' + p.read_text())
    git("init", "-q", "-b", "main", TPL)
    git("add", "-A", cwd=TPL)
    git("commit", "-qm", "scratch copy", cwd=TPL)
    return commits


class Sandbox:
    """One imitated Mac: <dir>/home (HOME, with the checkout in home/src), <dir>/bin (PATH), <dir>/brew (Homebrew)."""

    def __init__(self, name, arch="x86_64", macos="15.7.1", xcode="26.1", brew=True, formulas=(), checkout_dir="src/nvidia-macos-driver",
                 remotes=None, cpu=None, tools_from="homebrew"):
        # tools_from: NM_TOOLS_FROM in every run's environment. The arms written before the source way existed are about
        # Homebrew's behaviour and say so (homebrew); None leaves it unset (auto), as a tester's Mac has it
        self.tools_from = tools_from
        self.host_brew_hidden = False
        self.dir = TMP / "sb" / name
        shutil.rmtree(self.dir, ignore_errors=True)
        self.home = self.dir / "home"
        self.bin = self.dir / "bin"
        self.state = self.dir / "state"
        self.journal = self.dir / "journal"
        self.ctl = self.dir / "ctl"
        for d in (self.home, self.bin, self.state, self.ctl, self.dir / "tmp", self.dir / "steps"):
            d.mkdir(parents=True)
        self.work = self.home / "nullmoth-build"
        self.co = self.home / checkout_dir
        self.co.parent.mkdir(parents=True, exist_ok=True)
        git("clone", "-q", TPL, self.co)
        (self.state / "arch").write_text(arch + "\n")      # what uname -m says (the process)
        (self.state / "cpu").write_text((cpu or arch) + "\n")   # the Mac's processor (sysctl hw.optional.arm64)
        (self.state / "macos").write_text(macos + "\n")
        (self.state / "sdk").write_text("26.1\n")
        (self.state / "brew-llvm").write_text("22.1.3\n")
        if xcode:
            (self.state / "xcode").write_text(xcode + "\n")
        # the developer folder xcode-select names once Xcode is installed, holding what Xcode 26.5 holds of the files
        # build.sh looks for (XCODE_26_5; listed on a Mac). It has no usr/bin/xcrun: xcrun is /usr/bin/xcrun alone
        for f in XCODE_26_5:
            (self.dir / "Xcode.app/Contents/Developer" / f).parent.mkdir(parents=True, exist_ok=True)
            (self.dir / "Xcode.app/Contents/Developer" / f).write_text("")
        sdk = self.dir / "sdk"
        for f in ("System/Library/Frameworks/Kernel.framework/Headers/IOKit/IOLib.h", "usr/lib/libkmod.a",
                  "System/Library/Frameworks/Metal.framework/Headers/MTL4CommandQueue.h"):
            (sdk / f).parent.mkdir(parents=True, exist_ok=True)
            (sdk / f).write_text("")
        # git reaches the stand-in remotes through the real URLs
        rem = remotes or REMOTES
        (self.dir / "gitconfig").write_text(
            f'[url "file://{rem}/github.com/"]\n\tinsteadOf = https://github.com/\n'
            f'[url "file://{rem}/gitlab.freedesktop.org/"]\n\tinsteadOf = https://gitlab.freedesktop.org/\n'
            '[user]\n\tname = tester\n\temail = tester@example.com\n[advice]\n\tdetachedHead = false\n')
        os.symlink(rem / "www", self.dir / "www")
        for t, p in REALPATH.items():
            if p:
                os.symlink(p, self.bin / t)
        os.symlink(BASH, self.bin / "bash")
        # the real git, without the three variables an arm may have set to something that names nothing: where git is
        # Apple's (APPLE_TOOLS) they would reach xcrun; git itself reads none of them
        (self.bin / "git").write_text(f'#!/bin/bash\n{{ printf git; for a in "$@"; do printf " %q" "$a"; done; echo; }} >> {self.journal}\n'
                                      f'unset SDKROOT TOOLCHAINS DEVELOPER_DIR\nexec {GIT} "$@"\n')
        (self.bin / "git").chmod(0o755)
        for name_, role in STANDINS.items():
            self.wrap(self.bin / name_, role)
        for name_ in PLAIN:
            self.wrap(self.bin / name_, "plain")
        self.wrap(self.bin / "python3", "py", "3.9.6")                  # macOS's own python3
        self.wrap(self.bin / "patch", "patch", "patch 2.0-12u11-Apple")   # macOS's patch: not GNU's, and it applies the chains
        self.wrap(self.bin / "make", "ver", "GNU Make 3.81")
        self.has_brew = brew
        if brew:
            self.wrap(self.dir / "brew/bin/brew", "brew")
            for f in formulas:
                self.run_tool(self.dir / "brew/bin/brew", "install", f)
            (self.dir / "brew/Cellar/yours/1.0").mkdir(parents=True, exist_ok=True)   # an older version of a formula of the tester's
        self.journal.write_text("")

    def wrap(self, path, role, version=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():   # a link to a real tool (REAL_TOOLS): replaced, never written through
            path.unlink()
        path.write_text(f'#!/bin/bash\nNMT_SB={self.dir} exec /bin/bash {LIB}/fake.sh {role} "$0" "$@"\n')
        path.chmod(0o755)
        if version:
            pathlib.Path(str(path) + ".version").write_text(version + "\n")

    def run_tool(self, *argv):
        subprocess.run([str(a) for a in argv], env=self.env(), capture_output=True, check=True)

    def env(self, extra=None, home=None):
        path = [str(self.bin)] + ([str(self.dir / "brew/bin")] if self.has_brew else [])
        e = {"HOME": str(home or self.home), "PATH": ":".join(path), "TMPDIR": str(self.dir / "tmp"), "LANG": "C",
             "USER": "tester", "LOGNAME": "tester", "NMT_SB": str(self.dir), "NMT_LIB": str(LIB),
             "GIT_CONFIG_GLOBAL": str(self.dir / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1"}
        # NM_BREW is the only Homebrew build.sh considers when it is set. Without it build.sh on a tester's Mac would
        # find the real one in /opt/homebrew or /usr/local before the stand-in on PATH, and install into it.
        if self.has_brew:
            e["NM_BREW"] = str(self.dir / "brew/bin/brew")
        if self.tools_from:
            e["NM_TOOLS_FROM"] = self.tools_from
        e.update(extra or {})
        if not e.get("NM_BREW") and HOST_BREW and not self.has_brew and not self.host_brew_hidden:
            raise RuntimeError(f"an imitated Mac without Homebrew cannot run here without NM_BREW: build.sh would find {HOST_BREW[0]}")
        return e

    def hide_host_brew(self):
        """For an imitated Mac that has no Homebrew and, like a tester's, no NM_BREW: in this clone's build.sh the two
        places Homebrew is looked for before PATH become two that do not exist, so that on a machine with a Homebrew
        of its own build.sh finds none here either (env() refuses to run such a Mac otherwise)."""
        self.break_script(BREW_PLACES_LINE, f'brew_places() {{ echo "{self.dir}/no-homebrew/bin/brew {self.dir}/no-homebrew-other/bin/brew"; }}')
        self.host_brew_hidden = True

    def run(self, *args, extra=None, cwd=None, home=None, script=None, timeout=300, stdin=subprocess.DEVNULL):
        """./build.sh <args> from the checkout; returns (exit status, stdout+stderr)."""
        argv = [BASH, script or "./build.sh", *args]
        not_stopping()
        # its own session, so that a run that never ends is stopped with everything it started (a step stand-in left
        # behind would keep running after the test)
        pr = track(subprocess.Popen(argv, cwd=cwd or self.co, env=self.env(extra, home), stdin=stdin, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, start_new_session=True))
        try:
            out, err = pr.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            stop(pr)
            out, err = pr.communicate()
            return 124, f"{out}{err}\n[test: no result after {timeout} s; the run and what it started were stopped]"
        except BaseException:   # Ctrl-C in this thread: the run is in a session of its own and would go on
            stop(pr)
            raise
        finally:
            with _LOCK:
                _LIVE.discard(pr)
        return pr.returncode, out + err

    def popen(self, *args, extra=None, default_sigint=True):
        # Ctrl-C reaches build.sh even when this test was started with SIGINT ignored (nohup, "&" in a script), which
        # build.sh would inherit and could not trap
        # (by a small exec wrapper, not preexec_fn, which is not safe while another thread runs)
        pre = [sys.executable, "-c", "import os, signal, sys; signal.signal(signal.SIGINT, signal.SIG_DFL); os.execv(sys.argv[1], sys.argv[1:])"]
        not_stopping()
        return track(subprocess.Popen([*(pre if default_sigint else []), BASH, "./build.sh", *args], cwd=self.co, env=self.env(extra),
                                      stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                      start_new_session=True))

    def jlines(self):
        return self.journal.read_text().splitlines() if self.journal.exists() else []

    def clear(self):
        self.journal.write_text("")
        for p in (self.dir / "steps").iterdir():
            p.unlink()

    def steps(self):
        """[(key, args, env)] of the component stand-ins that ran, in order."""
        out = []
        for p in sorted((self.dir / "steps").glob("*.env")):
            key = p.stem.split("-", 1)[1]
            env = dict(l.split("=", 1) for l in p.read_text().splitlines() if "=" in l)
            args = pathlib.Path(str(p)[:-4] + ".args").read_text().splitlines()
            out.append((key, args, env))
        return out

    def step_keys(self):
        return [k for k, _, _ in self.steps()]

    def break_script(self, old, new, count=1):
        p = self.co / "build.sh"
        t = p.read_text()
        assert t.count(old) >= 1, f"broken-script control: text not found: {old}"
        p.write_text(t.replace(old, new, count))

    def listing(self, root=None, skip=()):
        """Every path under root with its size and content hash (folders too)."""
        root = pathlib.Path(root or self.home)
        out = {}
        for dp, dns, fns in os.walk(root):
            rel = os.path.relpath(dp, root)
            if any(rel == s or rel.startswith(s + "/") for s in skip):
                dns[:] = []
                continue
            out[rel + "/"] = "dir"
            for f in fns:
                p = os.path.join(dp, f)
                r = os.path.relpath(p, root)
                if os.path.islink(p):
                    out[r] = "link " + os.readlink(p)
                else:
                    out[r] = hashlib.sha256(open(p, "rb").read()).hexdigest()
        return out

    class _Scratch:
        def __init__(self, sb):
            self.sb = sb

        def __enter__(self):
            self.bak = pathlib.Path(str(self.sb.dir) + ".bak")
            shutil.rmtree(self.bak, ignore_errors=True)
            shutil.copytree(self.sb.dir, self.bak, symlinks=True)
            return self.sb

        def __exit__(self, *a):
            shutil.rmtree(self.sb.dir)
            os.rename(self.bak, self.sb.dir)
            return False

    def scratch(self):
        """with sb.scratch(): ... runs in the sandbox and puts it back as it was afterwards (for negative controls)."""
        return Sandbox._Scratch(self)


def diff_listing(a, b):
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))


def want(name):
    return not ONLY or name in ONLY


# ------------------------------------------------------------------------------------------------- what is expected
ORDER = ["rm", "kms", "nvrm", "fb", "accel", "agdc", "nmsmn", "ryzenadj", "airdis", "dlss", "osver", "nvk", "boost", "vulkan",
         "plugin", "xlate", "nvencvt"]
FRESH_MISSING = ["Xcode", "Homebrew", "Python 3.10", "the Python environment", "ninja", "cmake", "pkg-config",
                 "glslangValidator", "LLVM 22", "SPIRV-LLVM-Translator", "SPIRV-Tools", "Rust 1.98.1", "bindgen", "cbindgen"]
MUTATING = re.compile(r"^(brew (install|upgrade|uninstall|reinstall|update|link|unlink|tap)|curl |rustup-init|rustup |cargo (install|fetch)"
                      r"|python[0-9.]* -m (venv|pip)|step |sudo|git (init|fetch|clone|checkout|archive|update-ref|tag|stash|config|sparse-checkout|symbolic-ref))")
# variables the build scripts read that name a place; they must point into the working directory (or the checkout)
PLACE_VARS = ["OGKM", "NVRM_PRISTINE_OGKM", "NULLMOTH_BUILD_ROOT", "NM_OGKM620", "NM_OGKM620_PRISTINE", "NVK_PATH", "NVK_SYSROOT",
              "PKG_CONFIG_PATH", "LIBCLANG_PATH", "RUSTUP_HOME", "CARGO_HOME", "NULLMOTH_RUST_BIN", "NULLMOTH_CARGO_HOME",
              "NM_KHRONOS_URL", "NM_VULKAN_INCLUDE", "KMS_ARCHIVE", "RE", "KHDR", "KLIB", "PATH", "NM_BUILD_INFO", "MINVER"]
SYSTEM_DIRS = ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]


def doctor_items(out):
    m = re.search(r"^doctor: \d+ thing\(s\) missing or too old:\n((?:  - .*\n?)*)", out, re.M)
    return [l[4:] for l in m.group(1).splitlines()] if m else []


def mutating(lines):
    return [l for l in lines if MUTATING.match(l)]


def under(path, roots):
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)


# ------------------------------------------------------------------------------------------------- arms
# What a developer folder holds of the files build.sh looks for, as listed on a Mac with Xcode 26.5 and the command line
# tools 26.6 (2026-11-13). Neither has usr/bin/xcrun, the file Apple's own message names
# ("missing xcrun at: <folder>/usr/bin/xcrun"); both have usr/lib/libxcrun.dylib, which /usr/bin/xcrun loads.
XCODE_26_5 = ("usr/lib/libxcrun.dylib", "usr/bin/xcodebuild", "usr/bin/git", "usr/bin/make")
CLT_26_6 = ("usr/lib/libxcrun.dylib", "usr/bin/git", "usr/bin/make", "usr/bin/clang")
# what a stand-in of Apple's /usr/bin shims records when it runs: on a Mac without the command line tools each opens the
# dialog that offers to install them
SHIMS = ("git", "make", "python3", "xcrun", "xcodebuild")


def shim_runs(lines):
    return [l for l in lines if l.split(" ", 1)[0] in SHIMS]


def fresh_brew():
    """On a machine with a Homebrew of its own, build.sh without NM_BREW would find it: there the imitated Mac without
    Homebrew names a brew that is not there, and doctor's "NM_BREW names ..." line stands for its "Homebrew" line."""
    return {"NM_BREW": "<sb>/no-brew/bin/brew"} if HOST_BREW else {}


def arm_fresh():
    """A Mac with nothing installed. Without Xcode (no developer folder): every command stops before it runs git, make,
    python3, xcrun or xcodebuild, doctor naming exactly Xcode. With Xcode and nothing else: doctor names exactly what is
    missing, exits 1, changes nothing; a dry run of everything ends without error and installs nothing."""
    bare = Sandbox("fresh-noxcode", xcode=None, brew=False)

    def bare_problems(more=None, want="xcode-select", cmds=(("env",), ("--dry-run", "all"), ("fetch",), ("--dry-run", "build", "rm"))):
        p = []
        extra = {k: v.replace("<sb>", str(bare.dir)) for k, v in fresh_brew().items()}
        extra.update(more or {})
        bare.clear()
        rc, out = bare.run("doctor", extra=extra)
        items = doctor_items(out)
        if rc != 1 or len(items) != 1 or not items[0].startswith("Xcode"):
            p.append(f"doctor: exit {rc}, missing {items}")
        for args in cmds:
            rc, out = bare.run(*args, extra=extra)
            if rc != 2 or "xcode-select" not in out or want not in out:
                p.append(f"{' '.join(args)}: exit {rc}, not the stop for a missing Xcode ({want}): {out[-300:]}")
        if shim_runs(bare.jlines()):
            p.append(f"ran what opens Apple's install dialog on such a Mac, or fails there: {shim_runs(bare.jlines())[:4]}")
        if mutating(bare.jlines()):
            p.append(f"ran more than probes: {mutating(bare.jlines())[:3]}")
        return p

    def gone_problems():
        # an Xcode selected and then removed or renamed; and DEVELOPER_DIR naming a folder that is not there (two
        # commands each: the stop is the same code for all of them, which the case without Xcode shows for four)
        two = (("env",), ("--dry-run", "build", "rm"))
        (bare.state / "devdir").write_text(f"{bare.dir}/Applications/Xcode-old.app/Contents/Developer\n")
        p = bare_problems(want="does not exist", cmds=two)
        (bare.state / "devdir").unlink()
        p += bare_problems({"DEVELOPER_DIR": f"{bare.dir}/Applications/Xcode-26.app"}, want="DEVELOPER_DIR", cmds=two)
        (bare.dir / "half/usr/bin").mkdir(parents=True, exist_ok=True)
        p += bare_problems({"DEVELOPER_DIR": f"{bare.dir}/half"}, want="holds none of Apple's tools", cmds=two)
        return p

    p = bare_problems()
    check("no Xcode: every command stops before git, make, python3 or xcrun; doctor names exactly Xcode", not p, p)
    p = gone_problems()
    check("a developer folder that is gone or holds none of Apple's tools (xcode-select, DEVELOPER_DIR): the same stop, naming it", not p, p)
    with bare.scratch():
        bare.break_script('    if ! xcode-select -p >/dev/null 2>&1 && [ -z "${DEVELOPER_DIR:-}" ]; then devp="none selected"; else devp=$(developer_dir_problem); fi',
                          '    if ! xcode-select -p >/dev/null 2>&1; then devp="none selected"; fi')
        negative_without_apple_tools("only xcode-select's exit status checked (a folder that is gone passes; check_paths then runs git)", gone_problems)
    with bare.scratch():
        bare.break_script('  if [ "$CMD" != help ] && is_mac; then', '  if false; then')
        negative("no early stop without Xcode (check_paths then runs git)", bare_problems())

    sb = Sandbox("fresh", brew=False)
    extra = {k: v.replace("<sb>", str(sb.dir)) for k, v in fresh_brew().items()}
    want = [k for k in FRESH_MISSING if k != "Xcode"]

    def layout_problems():
        # every developer folder that is whole is taken for whole: the two a Mac has today, the application itself as
        # DEVELOPER_DIR, and a folder of the older layout, whose xcrun is inside it
        p = []
        for name, files in (("Xcode 26.5", XCODE_26_5), ("the command line tools 26.6", CLT_26_6),
                            ("Xcode.app as DEVELOPER_DIR", tuple("Contents/Developer/" + f for f in XCODE_26_5)),
                            ("a folder with its own usr/bin/xcrun", ("usr/bin/xcrun",))):
            d = sb.dir / "layouts" / name.replace(" ", "-")
            for f in files:
                (d / f).parent.mkdir(parents=True, exist_ok=True)
                (d / f).write_text("")
            sb.clear()
            rc, out = sb.run("env", extra=dict(extra, DEVELOPER_DIR=str(d)))
            if rc != 0 or "developer folder" in out:
                p.append(f"{name}: exit {rc}: {out[-300:]}")
        return p

    p = layout_problems()
    check("the developer folders of Xcode 26.5 and of the command line tools 26.6 (no usr/bin/xcrun in either) are taken "
          "for whole, and so is one with its own xcrun", not p, p)
    with sb.scratch():
        sb.break_script('  developer_dir_whole "$d" || developer_dir_whole "$d/Contents/Developer" || \\',
                        '  [ -x "$d/usr/bin/xcrun" ] || [ -x "$d/Contents/Developer/usr/bin/xcrun" ] || \\')
        negative("a test for usr/bin/xcrun alone refuses the Xcode and the command line tools a Mac has", layout_problems())

    def problems(script_note=""):
        before = sb.listing(sb.dir, skip=("tmp",))
        sb.clear()
        rc, out = sb.run("doctor", extra=extra)
        p = []
        if rc != 1:
            p.append(f"exit {rc}, not 1")
        items = doctor_items(out)
        for k in want:
            n = sum(1 for i in items if re.match(re.escape(k) + r"\b", i))
            if n != 1:
                p.append(f"'{k}' named {n} times in the missing list")
        if len(items) != len(want):
            p.append(f"{len(items)} items missing, expected {len(want)}: {items}")
        if mutating(sb.jlines()):
            p.append(f"doctor ran more than probes: {mutating(sb.jlines())}")
        after = sb.listing(sb.dir, skip=("tmp",))
        d = [x for x in diff_listing(before, after) if x not in ("journal",)]
        if d:
            p.append(f"doctor changed files: {d[:5]}")
        if any(l.split()[0] in ("brew",) for l in sb.jlines()):
            p.append("doctor ran brew")
        return p, out

    p, out = problems()
    check("fresh Mac with Xcode: doctor names exactly what is missing, exits 1, runs only probes, writes nothing", not p, p or out[-800:])
    rc, out = sb.run("doctor", extra=dict(extra, DEVELOPER_DIR=f"{sb.dir}/Xcode.app"))
    items = doctor_items(out)
    check("DEVELOPER_DIR naming an installed Xcode.app: doctor goes on and finds Xcode", rc == 1 and items and
          not any(i.startswith("Xcode") for i in items) and "Xcode 26.1 at" in out, (rc, items, out[-400:]))
    with sb.scratch():
        sb.break_script('  else d_missing "Rust $NM_PIN_RUST_VERSION in $RUSTUP_DIR', '  else d_note "Rust $NM_PIN_RUST_VERSION in $RUSTUP_DIR')
        negative("doctor that forgets one missing tool", problems()[0])
    rc, out = sb.run("--dry-run", "all", extra=extra)
    if HOST_BREW:   # deps stops at once when NM_BREW names no brew; that it installs nothing is what is left to check
        ok = rc == 2 and "NM_BREW names" in out and not mutating(sb.jlines())
    else:
        ok = rc == 0 and not mutating(sb.jlines())
    check("fresh Mac with Xcode: a dry run of everything ends as it should and installs nothing", ok,
          f"exit {rc} {mutating(sb.jlines())[:3]} {out[-600:]}")


BREW_PLACES_LINE = ('brew_places() { if [ "$(mac_arch)" = arm64 ]; then echo "/opt/homebrew/bin/brew /usr/local/bin/brew"; '
                    'else echo "/usr/local/bin/brew /opt/homebrew/bin/brew"; fi; }')


def arm_hostbrew():
    """No run of the imitated Mac reaches a Homebrew of the machine the test runs on.

    A second stand-in Homebrew plays the tester's own: in the clone only, the two places build.sh looks at before PATH
    (/opt/homebrew, /usr/local) are replaced by it. It records every call, and a call to it is the failure."""
    sb = Sandbox("hostbrew")
    host = sb.dir / "hostbrew/bin/brew"
    calls = sb.dir / "hostbrew.calls"
    host.parent.mkdir(parents=True)
    host.write_text(f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{calls}"\n'
                    f'case "${{1:-}}" in --version) echo "Homebrew 4.6.3" ;; --prefix) echo "{sb.dir}/hostbrew" ;; *) exit 1 ;; esac\n')
    host.chmod(0o755)
    sb.break_script(BREW_PLACES_LINE, f'brew_places() {{ echo "{host} {host}"; }}')
    standin = str(sb.dir / "brew/bin/brew")

    def reached():
        return calls.read_text().splitlines() if calls.exists() else []

    def problems(extra=None):
        """doctor and deps on this Mac: which Homebrew answers?"""
        if calls.exists():
            calls.unlink()
        sb.clear()
        p = []
        rc, out = sb.run("doctor", extra=extra)
        if f"Homebrew {standin}" not in out:
            p.append(f"doctor does not name the stand-in Homebrew: {[l for l in out.splitlines() if 'Homebrew' in l][:3]}")
        # a deps that installs: with --no-install it asks Homebrew nothing (brew_may_ask), and the arm would prove nothing
        rc, out = sb.run("deps", "-y", extra=extra)
        if not [l for l in sb.jlines() if l.startswith("brew ")]:
            p.append("deps did not ask the stand-in Homebrew anything")
        if reached():
            p.append(f"the machine's own Homebrew was called: {reached()[:4]}")
        return p

    p = problems()
    check("every run names its stand-in Homebrew (NM_BREW): the machine's own is never called", not p, p)
    negative("a run without NM_BREW (build.sh then finds the machine's own Homebrew first)", problems(extra={"NM_BREW": ""}))

    def missing_problems():
        """NM_BREW names a brew that is not there: no Homebrew, and no other one is looked for."""
        if calls.exists():
            calls.unlink()
        sb.clear()
        p = []
        gone = {"NM_BREW": str(sb.dir / "no-such/bin/brew")}
        rc, out = sb.run("doctor", extra=gone)
        items = doctor_items(out)
        if rc != 1 or not [i for i in items if i.startswith("Homebrew: NM_BREW names")]:
            p.append(f"doctor: exit {rc}, Homebrew lines {[i for i in items if 'Homebrew' in i][:2]}")
        rc, out = sb.run("deps", "-y", extra=gone)
        if rc == 0 or "NM_BREW names" not in out:
            p.append(f"deps: exit {rc}: {out[-200:]}")
        if mutating(sb.jlines()):
            p.append(f"deps went on to install: {mutating(sb.jlines())[:3]}")
        if reached():
            p.append(f"the machine's own Homebrew was called: {reached()[:4]}")
        return p

    p = missing_problems()
    check("NM_BREW that names no brew: doctor and deps say so, nothing is installed, no other Homebrew is looked for", not p, p)
    sb.break_script('  if [ -n "${NM_BREW:-}" ]; then\n    if [ -f "$NM_BREW" ]', '  if false; then\n    if [ -f "$NM_BREW" ]')
    negative("a build.sh that falls back to the usual places when NM_BREW names no brew", missing_problems())


def whichbrew_problems(arch, cpu, places, want_brew, want_other, nm_brew="", script_edit=None, path_brew=None):
    """doctor without NM_BREW on a Mac whose Homebrew(s) sit at <places>: which one does build.sh take, and does it name
    the other? In the clone only, build.sh's two fixed places are moved under the sandbox, to recording stand-ins.
    want_brew is the brew (relative to the sandbox) doctor must use, or None when none of this processor is present and
    doctor must report Homebrew missing. path_brew puts a brew only on PATH (not at a fixed place): ("wrapper",) a plain
    script in no Homebrew prefix, ("other",) a symlink that resolves into the other architecture's prefix."""
    sb = Sandbox(f"which-{arch}-{cpu}-{len(places)}" + ("-nm" if nm_brew else "") + ("-x" if script_edit else "")
                 + ("-" + path_brew[0] if path_brew else ""), arch=arch, cpu=cpu)
    (sb.dir / "brew/bin/brew").unlink()   # the usual stand-in on PATH: not here, so PATH offers no third Homebrew
    bs = sb.co / "build.sh"
    t = bs.read_text()
    if script_edit:
        assert script_edit[0] in t, script_edit[0]
        t = t.replace(*script_edit)
    for fixed in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
        t = t.replace(fixed, f"{sb.dir}{fixed}")
    left = re.findall(r"/(?:opt/homebrew|usr/local)/bin/brew", t.replace(str(sb.dir) + "/", "<sb>"))
    if left:
        return [f"the clone's build.sh still names a Homebrew of this machine: {left}"]
    bs.write_text(t)
    for pl in places:
        sb.wrap(sb.dir / pl.lstrip("/"), "brew")
    if path_brew:
        onpath = sb.bin / "brew"   # sb.bin is first on PATH, so this is what "command -v brew" finds
        if path_brew[0] == "wrapper":
            onpath.write_text('#!/bin/bash\necho "stand-in wrapper brew: was run" >&2\nexit 3\n')   # a run of it is a failure
            onpath.chmod(0o755)
        elif path_brew[0] == "other":
            other_prefix = "/opt/homebrew" if arch == "x86_64" else "/usr/local"   # the prefix of the OTHER architecture
            target = sb.dir / other_prefix.lstrip("/") / "Homebrew/bin/brew"       # a real brew deep in that prefix, not at its bin/brew
            sb.wrap(target, "brew")
            onpath.symlink_to(target)
    extra = {"NM_BREW": str(sb.dir) + nm_brew if nm_brew else ""}
    rc, out = sb.run("doctor", extra=extra)
    p = []
    items = doctor_items(out)
    if want_brew is None:
        if re.search(r"\bok\s+Homebrew ", out):
            p.append(f"doctor took a Homebrew where none of this processor is present: {[l.strip() for l in out.splitlines() if 'Homebrew' in l][:3]}")
        if not [i for i in items if "Homebrew" in i]:
            p.append(f"doctor does not report Homebrew as missing: {items}")
    else:
        want = f"ok       Homebrew {sb.dir}{want_brew}"
        if want not in out:
            p.append(f"doctor: {[l.strip() for l in out.splitlines() if 'Homebrew' in l][:3]}, expected {want_brew}")
    note = [l for l in out.splitlines() if "a second Homebrew" in l]
    if want_other and not (note and f"{sb.dir}{want_other}" in note[0] and f"NM_BREW={sb.dir}{want_other}" in note[0]):
        p.append(f"doctor does not name the other Homebrew {want_other} and NM_BREW: {note}")
    if not want_other and note:
        p.append(f"doctor names a second Homebrew where there is none to choose: {note}")
    return p


def arm_whichbrew():
    both = ["/opt/homebrew/bin/brew", "/usr/local/bin/brew"]
    for arch, cpu, places, use, other, what in (
            ("x86_64", "x86_64", both, "/usr/local/bin/brew", "/opt/homebrew/bin/brew", "an Intel Mac with both: /usr/local"),
            ("arm64", "arm64", both, "/opt/homebrew/bin/brew", "/usr/local/bin/brew", "Apple silicon with both: /opt/homebrew"),
            ("x86_64", "arm64", both, "/opt/homebrew/bin/brew", "/usr/local/bin/brew", "Apple silicon, shell under Rosetta: /opt/homebrew")):
        p = whichbrew_problems(arch, cpu, places, use, other)
        check(f"Homebrew of the Mac's processor first, the other named with NM_BREW: {what}", not p, p)
    # The other architecture's Homebrew alone is NOT a Homebrew this build can use: it is reported missing and named.
    p = whichbrew_problems("x86_64", "x86_64", ["/opt/homebrew/bin/brew"], None, "/opt/homebrew/bin/brew")
    check("an Intel Mac with only an Apple-silicon Homebrew: none is used on its own, doctor names it and NM_BREW", not p, p)
    p = whichbrew_problems("arm64", "arm64", ["/usr/local/bin/brew"], None, "/usr/local/bin/brew")
    check("Apple silicon with only an Intel Homebrew: none is used on its own, doctor names it and NM_BREW", not p, p)
    p = whichbrew_problems("x86_64", "x86_64", both, "/opt/homebrew/bin/brew", None, nm_brew="/opt/homebrew/bin/brew")
    check("NM_BREW names the other one: only that one is used", not p, p)
    # A brew reachable only through PATH: taken only when it resolves into this processor's own prefix.
    p = whichbrew_problems("x86_64", "x86_64", [], None, None, path_brew=("wrapper",))
    check("a brew on PATH that is a wrapper in no Homebrew prefix is not taken", not p, p)
    p = whichbrew_problems("x86_64", "x86_64", [], None, "/bin/brew", path_brew=("other",))
    check("a brew on PATH that resolves into the other architecture's prefix is not used on its own, but is named", not p, p)
    negative("the other architecture's Homebrew used when the processor's own place is empty (the old fallback)",
             whichbrew_problems("x86_64", "x86_64", ["/opt/homebrew/bin/brew"], None, "/opt/homebrew/bin/brew",
                                script_edit=('    [ -f "$native" ] && [ -x "$native" ] && BREW=$native',
                                             '    [ -f "$native" ] && [ -x "$native" ] && BREW=$native\n    [ -n "$BREW" ] || { [ -f "$other" ] && [ -x "$other" ] && BREW=$other; }')))
    negative("a PATH brew taken wherever it resolves (no prefix rule)",
             whichbrew_problems("x86_64", "x86_64", [], None, None, path_brew=("wrapper",),
                                script_edit=('        case "$r" in\n          "${native%/bin/brew}"/*) BREW=$c ;;',
                                             '        case "x$r" in\n          x*) BREW=$c ;;\n          "${native%/bin/brew}"/*) BREW=$c ;;')))
    negative("the processor taken from uname -m only (a shell under Rosetta counts as Intel)",
             whichbrew_problems("x86_64", "arm64", both, "/opt/homebrew/bin/brew", "/usr/local/bin/brew",
                                script_edit=('      *) if is_mac && [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = 1 ]; then MAC_ARCH=arm64;',
                                             '      *) if false; then MAC_ARCH=arm64;')))


def arm_brewenv():
    """Every call build.sh makes to Homebrew drops HOMEBREW_PREFIX, HOMEBREW_CELLAR and HOMEBREW_REPOSITORY, whatever
    the caller exported: a tester's login shell exports them for a Homebrew of the other architecture, and inherited
    they would send the chosen brew at the wrong installation. It also runs with a fixed PATH (that Homebrew's bin, then
    the system's folders) and without HOMEBREW_PATH and HOMEBREW_BREW_FILE: Homebrew keeps the caller's PATH for a
    formula's post-install step and its "std" builds, where a login shell's other-architecture bin folder would come
    first. The stand-in brew records them all; a non-empty one, or another PATH, fails."""
    poison = {"HOMEBREW_PREFIX": "/opt/homebrew", "HOMEBREW_CELLAR": "/opt/homebrew/Cellar", "HOMEBREW_REPOSITORY": "/opt/homebrew",
              "HOMEBREW_PATH": "/opt/homebrew/bin:/usr/bin:/bin", "HOMEBREW_BREW_FILE": "/opt/homebrew/bin/brew"}
    keys = ("HOMEBREW_PREFIX", "HOMEBREW_CELLAR", "HOMEBREW_REPOSITORY", "HOMEBREW_PATH", "HOMEBREW_BREW_FILE")

    def problems(sb):
        sb.clear()
        extra = dict(poison, PATH=f"{sb.dir}/opt/homebrew/bin:" + sb.env()["PATH"])
        sb.run("deps", "-y", extra=extra)
        calls = [l for l in sb.jlines() if l.startswith("brew-callenv")]
        want_path = f"{sb.dir}/brew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        p = []
        if not calls:
            p.append("no brew call was recorded at all (the arm proves nothing)")
        for l in calls:
            for tok in l.split():
                k, _, v = tok.partition("=")
                if k in keys and v:
                    p.append(f"{tok} reached brew (in {l.split()[0]})")
                if k == "PATH" and v != want_path:
                    p.append(f"brew ran with PATH={v}, not {want_path}")
        return p

    sb = Sandbox("brewenv")
    check("every brew call runs without HOMEBREW_PREFIX/CELLAR/REPOSITORY/PATH/BREW_FILE and with a fixed PATH, whatever "
          "the caller exported", not (p := problems(sb)), p)
    neg = Sandbox("brewenv-neg")
    neg.break_script('env -u HOMEBREW_PREFIX -u HOMEBREW_CELLAR -u HOMEBREW_REPOSITORY -u HOMEBREW_BREW_FILE -u HOMEBREW_PATH "PATH=$(brew_path)" \\',
                     'env -u HOMEBREW_BREW_FILE -u HOMEBREW_PATH "PATH=$(brew_path)" \\')
    negative("a run_brew that passes the three location variables through", problems(neg))
    neg3 = Sandbox("brewenv-neg3")
    neg3.break_script(' -u HOMEBREW_BREW_FILE -u HOMEBREW_PATH "PATH=$(brew_path)" \\', ' \\')
    negative("a run_brew that gives brew the caller's PATH and HOMEBREW_PATH (as before)", problems(neg3))
    around = ('  # formulas for what is missing\n', '  [ -z "$("$BREW" --prefix)" ] || true\n  # formulas for what is missing\n')
    neg2 = Sandbox("brewenv-neg2")
    neg2.break_script(*around)
    negative("a brew call in deps that goes around run_brew", problems(neg2))
    # and by the text: run_brew's own line is the only one of build.sh that runs $BREW
    def direct_calls(text):
        out = []
        for n, line in enumerate(text.splitlines(), 1):
            code = re.sub(r"(^|\s)#.*$", "", line)
            if re.match(r"\s*say\s", code):   # a sentence that shows a command, as the dry run does, runs nothing
                continue
            if re.search(r'(?<![\w-])"?\$\{?BREW\}?"?\s+(?:-|[a-z])', code) and 'HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_ENV_HINTS=1' not in code:
                out.append(f"{n}: {line.strip()[:120]}")
        return out
    text = (ROOT / "build.sh").read_text()
    check("build.sh runs Homebrew only through run_brew (no other line runs $BREW)", not direct_calls(text), direct_calls(text))
    assert around[0] in text
    negative("a line that runs \"$BREW\" --prefix directly", direct_calls(text.replace(*around)))


def pathtools_problems(arch, script_edit=None):
    """doctor on a Mac whose PATH puts a Homebrew of the other architecture first (an Intel Mac: an Apple-silicon one in
    /opt/homebrew; Apple silicon: an Intel one in /usr/local). No tool of that Homebrew is taken from PATH, by where its
    file resolves to or by the interpreter its #! names, and no program that "file" calls a Mach-O of the other
    processor; a universal program, and on Apple silicon an ordinary /usr/local/bin program, still are. In the clone
    only, build.sh's two fixed Homebrew places are moved into the sandbox."""
    sb = Sandbox(f"pathtools-{arch}" + ("-x" if script_edit else ""), arch=arch)
    bs = sb.co / "build.sh"
    t = bs.read_text()
    if script_edit:
        assert script_edit[0] in t, script_edit[0]
        t = t.replace(*script_edit)
    for fixed in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
        t = t.replace(fixed, f"{sb.dir}{fixed}")
    bs.write_text(t)
    other = sb.dir / ("opt/homebrew" if arch == "x86_64" else "usr/local")
    sb.wrap(other / "bin/brew", "brew")   # that Homebrew itself (never used: NM_BREW names the stand-in of this processor)
    obin = other / "bin"
    cellar = other / "Cellar"
    for tool, role, ver in (("ninja", "ver", "1.13.1"), ("python3.13", "py", "3.13.9")):
        real = cellar / tool / "1" / "bin" / tool
        sb.wrap(real, role, ver)
        (obin / tool).symlink_to(real)
    (obin / "python3").symlink_to(obin / "python3.13")
    elsewhere = sb.dir / "elsewhere/bin"
    elsewhere.mkdir(parents=True)
    # a script run by that Homebrew's python
    (elsewhere / "pkg-config").write_text(f"#!{obin}/python3\nprint('2.5.1')\n")
    (elsewhere / "pkg-config").chmod(0o755)
    # a script that names that python through env (found on PATH, which has that Homebrew first)
    (elsewhere / "cmake").write_text("#! /usr/bin/env python3\nprint('cmake version 4.1.2')\n")
    (elsewhere / "cmake").chmod(0o755)
    # a Mach-O of the other processor, and a universal one (that one is taken)
    sb.wrap(elsewhere / "glslangValidator", "ver", "Glslang Version: 11:16.0.0")
    (elsewhere / "glslangValidator.arch").write_text("arm64\n" if arch == "x86_64" else "x86_64\n")
    sb.wrap(elsewhere / "gpatch", "patch", "GNU patch 2.8")
    (elsewhere / "gpatch.arch").write_text("x86_64 arm64\n")
    sb.wrap(sb.bin / "file", "file")
    keep = []
    if arch == "arm64":   # an ordinary program in /usr/local/bin, not Homebrew's, on Apple silicon: taken
        sb.wrap(obin / "cmake", "cmake", "cmake version 4.1.2")
        keep.append(("cmake", f"{obin}/cmake"))
    path = ":".join([str(obin), str(elsewhere), str(sb.bin), str(sb.dir / "brew/bin")])
    rc, out = sb.run("doctor", extra={"PATH": path})
    p = []
    items = doctor_items(out)
    for want in ("ninja", "pkg-config", "glslangValidator", "Python 3.10"):
        if not [i for i in items if i.startswith(want)]:
            p.append(f"{want} of the other architecture taken: missing list {items}")
    if arch == "x86_64" and not [i for i in items if i.startswith("cmake")]:
        p.append(f"a cmake whose #! names that Homebrew's python through env was taken: {items}")
    if f"ok       patch {elsewhere}/gpatch" not in out:
        p.append(f"the universal gpatch was not taken: {[l.strip() for l in out.splitlines() if 'patch' in l][:3]}")
    for tool, at in keep:
        if [i for i in items if i.startswith(tool)] or f"ok       {tool}" not in out:
            p.append(f"the ordinary {at} was not taken: {[l.strip() for l in out.splitlines() if tool in l][:3]}")
    notes = [l for l in out.splitlines() if "on PATH (" in l and "is not used" in l]
    for tool in ("ninja", "pkg-config", "glslangValidator", "python3"):
        if not [l for l in notes if l.strip().startswith(f"note     {tool} on PATH")]:
            p.append(f"doctor does not say why {tool} on PATH is not used: {notes}")
    return p


def arm_pathtools():
    for arch in ("x86_64", "arm64"):
        p = pathtools_problems(arch)
        check(f"no tool of the other architecture's Homebrew is taken from PATH ({arch})", not p, p)
    negative("PATH tools taken without asking where they belong (the old command -v)",
             pathtools_problems("x86_64", script_edit=('  [ -n "$(foreign_reason "$c")" ] || printf', '  printf')))
    negative("no Mach-O test (a program of the other processor outside any Homebrew is taken)",
             pathtools_problems("x86_64", script_edit=('  if have file && d=$(file -b "$r" 2>/dev/null); then', '  if false; then')))
    negative("no #! test (a script run by that Homebrew's python is taken)",
             pathtools_problems("x86_64", script_edit=("  [ \"$(LC_ALL=C head -c 2 \"$r\" 2>/dev/null)\" = '#!' ] || return 0", "  return 0")))


DEPS_WANT = ["ninja", "pkgconf", "glslang", "spirv-tools", "python@3.14", "llvm", "spirv-llvm-translator"]


def deps_problems(sb, rc, out, before):
    p = []
    if rc != 0:
        p.append(f"exit {rc}")
    j = sb.jlines()
    inst = [l.split()[2] for l in j if l.startswith("brew install ")]
    if sorted(inst) != sorted(DEPS_WANT):
        p.append(f"brew was asked for {inst}, expected exactly {DEPS_WANT}")
    other = [l for l in j if l.startswith("brew ") and l.split()[1] not in ("--version", "info", "install", "--prefix")]
    if other:
        p.append(f"brew was asked for something else: {other}")
    env = [l for l in j if l.startswith("brew-env")]
    if not env or any("HOMEBREW_NO_AUTO_UPDATE=1" not in l or "HOMEBREW_NO_INSTALL_UPGRADE=1" not in l
                      or "HOMEBREW_NO_INSTALL_CLEANUP=1" not in l for l in env):
        p.append(f"brew install ran without HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_UPGRADE=1 HOMEBREW_NO_INSTALL_CLEANUP=1: {env}")
    if not (sb.dir / "brew/Cellar/yours/1.0").is_dir():
        p.append("an older version of a formula of the tester's was removed (Homebrew's clean-up after an install)")
    if any(re.search(r"(^|\s)sudo(\s|$)", l) for l in j):
        p.append("a journal entry contains sudo")
    after = sb.listing(sb.home)
    outside = [x for x in diff_listing(before, after) if not x.startswith("nullmoth-build/") and x != "nullmoth-build/"]
    if outside:
        p.append(f"written outside the working directory: {outside[:8]}")
    for f in (".cargo", ".rustup", ".profile", ".zshrc", ".bash_profile", ".zprofile"):
        if (sb.home / f).exists():
            p.append(f"~/{f} was written")
    if not (sb.work / "tools/rustup/toolchains/1.98.1-x86_64-apple-darwin/bin/rustc").exists():
        p.append("Rust 1.98.1 is not in the working directory")
    if not (sb.work / "tools/venv/bin/meson").exists():
        p.append("the Python environment has no meson")
    if not [l for l in j if l.startswith("python3 -m pip install") and "--require-hashes" in l]:
        p.append("pip did not install with --require-hashes")
    return p


def spirvprefix_problems(script_edit=None):
    """A SPIRV-Tools that is not Homebrew's, named by NM_SPIRV_TOOLS_PREFIX: doctor takes it, deps asks Homebrew for none,
    and a prefix without the .pc file is named and not taken."""
    sb = Sandbox("spirvprefix")
    if script_edit:
        sb.break_script(*script_edit)
    p = []
    pre = sb.home / "spirv-tools-of-mine"
    (pre / "lib/pkgconfig").mkdir(parents=True)
    (pre / "lib/pkgconfig/SPIRV-Tools.pc").write_text("Name: SPIRV-Tools\nVersion: 2025.5\n")

    def formulas(out):
        m = re.search(r"^Homebrew formulas to install \(.*\): (.*)$", out, re.M)
        return m.group(1).split() if m else None

    rc, out = sb.run("doctor")
    if not re.search(r"^  MISSING  SPIRV-Tools .*NM_SPIRV_TOOLS_PREFIX", out, re.M):
        p.append("without the setting, doctor's MISSING line for SPIRV-Tools does not name it")
    rc, out = sb.run("--dry-run", "deps")
    if "spirv-tools" not in (formulas(out) or []):
        p.append(f"without the setting, the dry run of deps does not ask Homebrew for spirv-tools: {formulas(out)}")
    extra = {"NM_SPIRV_TOOLS_PREFIX": str(pre)}
    rc, out = sb.run("doctor", extra=extra)
    if not re.search(r"^  ok       SPIRV-Tools 2025\.5$", out, re.M):
        p.append("with the setting, doctor does not say: ok SPIRV-Tools 2025.5")
    if [i for i in doctor_items(out) if i.startswith("SPIRV-Tools")]:
        p.append("with the setting, doctor still counts SPIRV-Tools as missing")
    rc, out = sb.run("--dry-run", "deps", extra=extra)
    f = formulas(out)
    if f is None or "spirv-tools" in f or "ninja" not in f:
        p.append(f"with the setting, the dry run of deps asks Homebrew for: {f} (expected the others, without spirv-tools)")
    empty = sb.home / "nothing-here"
    rc, out = sb.run("doctor", extra={"NM_SPIRV_TOOLS_PREFIX": str(empty)})
    # build.sh prints a path under the home folder with ~
    if not re.search(r"^  note     NM_SPIRV_TOOLS_PREFIX names ~/nothing-here, which has no lib/pkgconfig/SPIRV-Tools\.pc", out, re.M):
        p.append("a prefix without SPIRV-Tools.pc is not named by doctor")
    if not [i for i in doctor_items(out) if i.startswith("SPIRV-Tools")]:
        p.append("a prefix without SPIRV-Tools.pc is taken as SPIRV-Tools")
    return p


def arm_spirvprefix():
    p = spirvprefix_problems()
    check("NM_SPIRV_TOOLS_PREFIX names a SPIRV-Tools that is not Homebrew's: doctor takes it and deps installs none", not p, p)
    negative("SPIRV-Tools looked for in Homebrew only (the setting is ignored)",
             spirvprefix_problems(script_edit=('  cands=( "${NM_SPIRV_TOOLS_PREFIX:-}" "$(tc_path spirv-tools)" )\n',
                                               '  cands=( )\n')))
    negative("a prefix taken without looking for its SPIRV-Tools.pc",
             spirvprefix_problems(script_edit=('    [ -n "$p" ] && [ -f "$p/lib/pkgconfig/SPIRV-Tools.pc" ] || continue\n    PC_SPIRV_TOOLS=$p/lib/pkgconfig; break',
                                               '    [ -n "$p" ] || continue\n    PC_SPIRV_TOOLS=$p/lib/pkgconfig; break')))


def cargoconf_problems(script_edit=None):
    """doctor names a cargo configuration in a folder above the working directory (here the home folder's own)."""
    sb = Sandbox("cargoconf")
    if script_edit:
        sb.break_script(*script_edit)
    p = []
    rc, out = sb.run("doctor")
    if "~/.cargo/config.toml applies to cargo's fetch and build of the translator" in out:
        p.append("doctor names a ~/.cargo/config.toml that is not there")
    (sb.home / ".cargo").mkdir()
    (sb.home / ".cargo/config.toml").write_text('[profile.release]\ndebug = true\n')
    rc, out = sb.run("doctor")
    lines = [l for l in out.splitlines() if "~/.cargo/config.toml applies to cargo's fetch and build of the translator" in l]
    if len(lines) != 1:
        p.append(f"doctor does not name ~/.cargo/config.toml once: {lines}")
    return p


def arm_cargoconf():
    p = cargoconf_problems()
    check("doctor names a ~/.cargo/config.toml, which cargo reads for the translator under the working directory", not p, p)
    negative("a doctor that does not look above the working directory for cargo's configuration",
             cargoconf_problems(('  for d in "$W" "$HERE"; do\n', '  for d in; do\n')))


# Homebrew's spirv-llvm-translator is linked against Homebrew's llvm and belongs to no other LLVM. With NM_LLVM_PREFIX
# naming an LLVM 22 that is not Homebrew's, every other tool present and Homebrew's translator installed, deps neither
# takes that translator nor asks Homebrew anything: it builds the pinned one against the LLVM in use.
ALL_BUT_LLVM = ("gpatch", "ninja", "cmake", "pkgconf", "glslang", "spirv-tools", "python@3.14")


def ownllvm_problems(script_edit=None, brewtr=True):
    """brewtr: Homebrew's spirv-llvm-translator is installed (then it must not be taken); without it, today's deps asked
    Homebrew about its llvm to decide whether to install it"""
    sb = Sandbox("ownllvm", formulas=ALL_BUT_LLVM + (("spirv-llvm-translator",) if brewtr else ()))
    mine = sb.home / "llvm-22-of-mine"
    sb.wrap(mine / "bin/llvm-config", "ver", "22.1.8")
    (mine / "lib").mkdir(parents=True, exist_ok=True)
    (mine / "lib/libclang.dylib").write_text("")
    if script_edit:
        sb.break_script(*script_edit)
    sb.clear()
    rc, out = sb.run("deps", "-y", extra={"NM_LLVM_PREFIX": str(mine)})
    p = [] if rc == 0 else [f"deps exit {rc}: {out[-500:]}"]
    calls = brew_calls(sb)
    if calls:
        p.append(f"Homebrew was called: {calls[:4]}")
    conf = [l for l in sb.jlines() if l.startswith("cmake -S ")]
    if not conf or f"-DLLVM_DIR={mine}/lib/cmake/llvm" not in conf[0]:
        p.append(f"the pinned translator was not built against the LLVM in use: {conf[:1]}")
    rc, out = sb.run("doctor", extra={"NM_LLVM_PREFIX": str(mine)})
    if "ok       SPIRV-LLVM-Translator 22.1.0.0" not in out:
        p.append("doctor does not take the translator built for the LLVM in use: "
                 + "; ".join(l.strip() for l in out.splitlines() if "SPIRV-LLVM-Translator" in l))
    return p


def arm_ownllvm():
    p = ownllvm_problems() + ownllvm_problems(brewtr=False)
    check("an LLVM 22 that is not Homebrew's (NM_LLVM_PREFIX): deps calls no brew at all and builds the translator against it, "
          "with Homebrew's translator installed or not", not p, p)
    negative("Homebrew's translator taken whatever the LLVM in use",
             ownllvm_problems(('  [ "$tc" = 1 ] || ! brew_llvm_chosen || brewtr=$(brew_opt spirv-llvm-translator)\n',
                               '  brewtr=$(brew_opt spirv-llvm-translator)\n')))
    negative("Homebrew asked for its translator whatever the LLVM in use",
             ownllvm_problems(('[ "$nobrew" = 0 ] && { brew_llvm_chosen || case " ${want[*]-} " in *" llvm "*) true ;; *) false ;; esac; } && brew_may_ask',
                               '[ "$nobrew" = 0 ] && brew_may_ask'), brewtr=False))
    # the other side: Homebrew's llvm (the pinned major) still gets Homebrew's translator (arm deps proves it with the
    # formulas it installs); a rule that never lets Homebrew's translator in fails there
    def brewllvm_problems(script_edit=None):
        sb = Sandbox("brewllvm", formulas=("cmake",))
        if script_edit:
            sb.break_script(*script_edit)
        before = sb.listing(sb.home)
        rc, out = sb.run("deps", "-y")
        p = deps_problems(sb, rc, out, before)
        if [l for l in sb.jlines() if l.startswith("cmake -S ")]:
            p.append("the translator was built from source although Homebrew's fits Homebrew's llvm, the LLVM in use")
        rc, out = sb.run("doctor")
        if not re.search(r"^  ok       SPIRV-LLVM-Translator 22\.1\.0$", out, re.M):
            p.append("doctor does not take Homebrew's translator for Homebrew's llvm")
        return p
    p = brewllvm_problems()
    check("Homebrew's llvm (LLVM 22) still gets Homebrew's spirv-llvm-translator and nothing is built from source", not p, p)
    negative("Homebrew's llvm without Homebrew's translator",
             brewllvm_problems(('  [ -n "$LLVM_PREFIX" ] && [ -n "$BREW_PREFIX" ] && [ -d "$(brew_opt llvm)" ] || return 1\n', '  return 1\n')))


# Every tool already there, none of it Homebrew's: the tester's settings (NM_PYTHON, NM_LLVM_PREFIX,
# NM_SPIRV_TOOLS_PREFIX), the toolchain an earlier deps built (ninja, cmake, pkgconf, glslang), macOS's patch. With
# NM_TOOLS_FROM=homebrew and a Homebrew present, deps calls no brew at all, not even "brew info": each call it can make
# comes after it knows that a tool is missing and that Homebrew could be the one to give it. The stand-in Homebrew
# records every call (brew_calls). deps runs twice: the first builds the pinned translator against the LLVM of the
# settings, the second finds everything.
def settings_sandbox(name, script_edit=None):
    sb = Sandbox(name)
    sb.work.mkdir(parents=True)
    (sb.work / ".nullmoth-build-workdir").write_text("made by the test\n")
    tc = sb.work / "tools/toolchain"
    sb.wrap(tc / "bin/ninja", "ver", "1.13.2")
    sb.wrap(tc / "bin/cmake", "cmake", "cmake version 4.4.5")
    sb.wrap(tc / "bin/pkg-config", "ver", "2.5.1")
    sb.wrap(tc / "bin/glslangValidator", "ver", "Glslang Version: 11:16.6.0")
    (tc / ".nm-toolchain").mkdir(parents=True)
    for tool in ("ninja", "cmake", "pkgconf", "glslang"):
        (tc / ".nm-toolchain" / tool).write_text(f"{tool} built by an earlier deps\n")
    mine = sb.home / "mine"
    sb.wrap(mine / "llvm/bin/llvm-config", "ver", "22.1.8")
    (mine / "llvm/lib").mkdir(parents=True)
    (mine / "llvm/lib/libclang.dylib").write_text("")
    (mine / "spirv-tools/lib/pkgconfig").mkdir(parents=True)
    (mine / "spirv-tools/lib/pkgconfig/SPIRV-Tools.pc").write_text("Name: SPIRV-Tools\nVersion: 2026.4.1\n")
    sb.wrap(mine / "python/bin/python3.14", "py", "3.14.10")
    if script_edit:
        sb.break_script(*script_edit)
    return sb, {"NM_PYTHON": str(mine / "python/bin/python3.14"), "NM_LLVM_PREFIX": str(mine / "llvm"),
                "NM_SPIRV_TOOLS_PREFIX": str(mine / "spirv-tools")}


def settings_problems(script_edit=None):
    sb, extra = settings_sandbox("settings", script_edit)
    p = []
    for n in (1, 2):
        sb.clear()
        rc, out = sb.run("deps", "-y", extra=extra)
        if rc != 0 or "deps: everything required is present" not in out:
            p.append(f"deps run {n}: exit {rc}: {out[-600:]}")
        if brew_calls(sb):
            p.append(f"deps run {n} called Homebrew: {brew_calls(sb)[:4]}")
        if n == 1 and not [l for l in sb.jlines() if l.startswith("cmake -S ")]:
            p.append("the first deps did not build the translator")
        if "Homebrew formulas to install" in out:
            p.append(f"deps run {n} lists Homebrew formulas to install")
    return p


def arm_settings():
    p = settings_problems()
    check("every tool from the tester's settings and the toolchain, none from Homebrew: deps calls no brew at all, "
          "not even brew info, in its first run (it builds the translator) and its second", not p, p)
    negative("deps that asks Homebrew about its llvm while the LLVM in use is not Homebrew's",
             settings_problems(('[ "$nobrew" = 0 ] && { brew_llvm_chosen || case " ${want[*]-} " in *" llvm "*) true ;; *) false ;; esac; } && brew_may_ask',
                                '[ "$nobrew" = 0 ] && brew_may_ask')))
    negative("deps that asks Homebrew before it knows that a tool is missing",
             settings_problems(('  # formulas for what is missing\n', '  brew_llvm_is_pinned || true\n')))


# ------------------------------------------------------------------------------------------- the source way (NM_TOOLS_FROM)
# build/toolchain.sh is a stand-in (FAKE_SH "toolchain"): its calls are journalled as "toolchain-build <tool> <settings>",
# it needs the source trees and the earlier tools a real build needs, and it lays out what build.sh looks for.
TC_ORDER = ["ninja", "cmake", "openssl", "zstd", "xz", "python", "pkgconf", "spirv-tools", "glslang", "llvm"]
TC_PIN = {"ninja": ["TCNINJA"], "cmake": ["TCCMAKE"], "openssl": ["TCOPENSSL"], "zstd": ["TCZSTD"], "xz": ["TCXZ"],
          "python": ["TCPYTHON"], "pkgconf": ["TCPKGCONF"], "spirv-tools": ["TCSPIRVTOOLS", "TCSPIRVHEADERS"],
          "glslang": ["TCGLSLANG"], "llvm": ["TCLLVM"]}


def tc_builds(sb):
    """[(tool, {setting: value})] of the toolchain builds the last runs made, in order"""
    out = []
    for l in sb.jlines():
        if l.startswith("toolchain-build "):
            parts = shlex.split(l)
            out.append((parts[1], dict(a.split("=", 1) for a in parts[2:])))
    return out


# the tools whose code another has linked in: their stamp text follows that tool's own, after "; with "
TC_LINKED = {"python": ["openssl", "zstd", "xz"], "glslang": ["spirv-tools"]}


def tc_stamp_want(sb, tool, recipe=2):
    pins = (sb.co / "build/pins.sh").read_text()
    commits = "+".join(pin_value(pins, f"NM_PIN_{p}_COMMIT") for p in TC_PIN[tool])
    return f"{commits} {tool} recipe {recipe} Darwin-x86_64 macos15.0" + "".join(
        "; with " + tc_stamp_want(sb, n) for n in TC_LINKED.get(tool, []))


def source_sandbox(name, **kw):
    """an Intel Mac without Homebrew, NM_TOOLS_FROM and NM_BREW unset (as on a tester's Mac), with GNU patch on PATH as
    gpatch"""
    kw.setdefault("brew", False)
    sb = Sandbox(name, tools_from=None, **kw)
    if not sb.has_brew:
        sb.hide_host_brew()
    sb.wrap(sb.bin / "gpatch", "patch", "GNU patch 2.8")
    return sb


def source_deps_problems(sb, rc, out, before, want=TC_ORDER):
    p = []
    if rc != 0:
        p.append(f"exit {rc}")
    if brew_calls(sb):
        p.append(f"Homebrew was called: {brew_calls(sb)[:3]}")
    if re.search(r"Homebrew is not installed|official installer|brew\.sh", out):
        p.append("deps speaks of installing Homebrew")
    built = tc_builds(sb)
    if [t for t, _ in built] != want:
        p.append(f"built {[t for t, _ in built]}, expected {want} in that order")
    w = sb.work
    for t, s in built:
        exp = {"NM_TC_PREFIX": f"{w}/tools/toolchain", "NM_TC_SRC": f"{w}/src/toolchain", "NM_TC_WORK": f"{w}/tools/toolchain-build",
               "NM_TC_LOGS": f"{w}/logs/toolchain", "NM_TC_MESON": f"{w}/tools/venv/bin/meson", "NM_TC_STAMP": tc_stamp_want(sb, t),
               "MACOSX_DEPLOYMENT_TARGET": "15.0"}
        bad = {k: s.get(k) for k, v in exp.items() if s.get(k) != v}
        if bad:
            p.append(f"{t} was built with {bad}")
        stamp = w / "tools/toolchain/.nm-toolchain" / t
        if not stamp.exists() or stamp.read_text().strip() != exp["NM_TC_STAMP"]:
            p.append(f"the stamp of {t} is not '{exp['NM_TC_STAMP']}'")
    outside = [x for x in diff_listing(before, sb.listing(sb.home)) if not x.startswith("nullmoth-build/")]
    if outside:
        p.append(f"written outside the working directory: {outside[:6]}")
    for f in (".profile", ".zshrc", ".bash_profile", ".zprofile", ".cargo", ".rustup"):
        if (sb.home / f).exists():
            p.append(f"~/{f} was written")
    if rc == 0 and "deps: everything required is present" not in out:
        p.append("deps does not end with everything present")
    return p


def arm_source():
    """an Intel Mac without Homebrew: deps builds every tool from the pinned sources, in order, into the working
    directory, with no brew call; a second deps builds nothing; doctor is content"""
    def run(edit=None, tag=""):
        sb = source_sandbox("src" + tag)
        if edit:
            sb.break_script(*edit)
        before = sb.listing(sb.home)
        sb.clear()
        rc, out = sb.run("deps", "-y")
        return sb, source_deps_problems(sb, rc, out, before), out
    sb, p, out = run()
    check("source way on an Intel Mac without Homebrew: deps builds the ten tools in order with the interface's settings and "
          "stamps, calls no brew, offers no Homebrew, writes nothing outside the working directory, ends with everything present",
          not p, (p, out[-1500:]))
    # the per-tool times are one measured run on one Mac: deps states their sum as an estimate for this Mac's plan
    def estimate_problems(text):
        return [] if re.search(r"^  On one 12-thread Intel Mac this takes about \d+ minutes", text, re.M) else ["no estimate"]
    check("deps states its time for the tools as an estimate (about N minutes)", not estimate_problems(out), out[:1500])
    negative("a time stated as the time it took", estimate_problems(out.replace("this takes about", "this took about")))
    sb.clear()
    rc, out = sb.run("deps", "-y")
    again = tc_builds(sb) + [(l, {}) for l in mutating(sb.jlines()) if l.startswith("git fetch")]
    check("a second deps builds and fetches nothing", rc == 0 and not again, (rc, again[:4], out[-500:]))
    rc, out = sb.run("doctor")
    check("doctor after it: everything required is present, missing tools would come from the pinned sources",
          rc == 0 and "missing build tools are built by ./build.sh deps from pinned sources" in out and "Homebrew (https://brew.sh)" not in out,
          out[-1200:])
    # one tool's id or pin changes: that tool alone is built again (a changed pin is imitated by the stamp's commit part,
    # which is what deps compares)
    (sb.state / "tc-recipe-glslang").write_text("3\n")
    st = sb.work / "tools/toolchain/.nm-toolchain/pkgconf"
    st.write_text("0" * 40 + st.read_text().split(" ", 1)[1])
    sb.clear()
    rc, out = sb.run("deps", "-y")
    got = [t for t, _ in tc_builds(sb)]
    check("a raised recipe of glslang and a changed pin of pkgconf: deps builds those two again, nothing else",
          rc == 0 and got == ["pkgconf", "glslang"], (got, out[-600:]))
    # a new OpenSSL (its recipe raised) and a new SPIRV-Tools: each is built again with the tool that has it linked in
    # (Python, glslang), and nothing else; the control's build.sh forgets what is linked into what
    def linked_round(s):
        (s.state / "tc-recipe-openssl").write_text("3\n")
        (s.state / "tc-recipe-spirv-tools").write_text("3\n")
        s.clear()
        rc, out = s.run("deps", "-y")
        return rc, [t for t, _ in tc_builds(s)], out
    rc, got, out = linked_round(sb)
    check("a raised recipe of openssl and of spirv-tools: deps builds openssl, python, spirv-tools and glslang again, nothing else",
          rc == 0 and got == ["openssl", "python", "spirv-tools", "glslang"], (got, out[-600:]))
    # a new LLVM of the toolchain (its recipe raised): LLVM is built again, and the translator with it, because the
    # translator's note is the LLVM's stamp
    rec = sb.work / "tools/spirv-llvm-translator/.nm-llvm"
    stamp = sb.work / "tools/toolchain/.nm-toolchain/llvm"
    p = [] if rec.exists() and rec.read_text() == stamp.read_text() else [f"the note {rec.read_text() if rec.exists() else None!r} is not the LLVM stamp {stamp.read_text()!r}"]
    (sb.state / "tc-recipe-llvm").write_text("3\n")
    sb.clear()
    rc, out = sb.run("deps", "-y")
    got = [t for t, _ in tc_builds(sb)]
    conf = [l for l in sb.jlines() if l.startswith("cmake -S ")]
    if rc != 0 or got != ["llvm"] or len(conf) != 1:
        p.append(f"after a raised LLVM recipe: exit {rc}, toolchain builds {got}, translator configured {len(conf)} time(s)")
    if not rec.exists() or rec.read_text() != stamp.read_text():
        p.append("the note was not brought up to the new LLVM stamp")
    check("source way: the translator notes the toolchain LLVM's stamp, and a rebuilt LLVM builds it again", not p, (p, out[-600:]))
    sbr, _, _ = run(edit=('  if tc_mine "$LLVM_PREFIX"; then cat "$TC_PREFIX/.nm-toolchain/llvm" 2>/dev/null || true\n',
                          '  if false; then :\n'), tag="-neg-note")
    (sbr.state / "tc-recipe-llvm").write_text("3\n")
    sbr.clear()
    sbr.run("deps", "-y")
    negative("a translator noted by the LLVM's version and place alone, which a rebuilt toolchain LLVM keeps",
             [] if [l for l in sbr.jlines() if l.startswith("cmake -S ")] else ["not built again"])
    sbl, pl, _ = run(edit=('tc_linked() { case "$1" in python) echo "openssl zstd xz" ;; glslang) echo spirv-tools ;; *) echo "" ;; esac; }',
                           'tc_linked() { echo ""; }'), tag="-neg-linked")
    rc, got, out = linked_round(sbl)
    negative("a toolchain that does not build Python and glslang again after what they link",
             [] if got == ["openssl", "python", "spirv-tools", "glslang"] else [f"built {got}"])
    # a tool whose stamp is missing (half-installed) is not taken
    (sb.work / "tools/toolchain/.nm-toolchain/glslang").unlink()
    rc, out = sb.run("doctor")
    ok = re.search(r"^  MISSING  glslangValidator .*\(\./build\.sh deps builds it\)", out, re.M) is not None
    check("a toolchain glslang without its stamp is not taken: doctor says MISSING, deps builds it", ok, out[-1200:])
    with sb.scratch():
        sb.break_script('tc_path() { [ -s "$TC_PREFIX/.nm-toolchain/$1" ] && printf', 'tc_path() { [ -e "$TC_PREFIX" ] && printf')
        rc, out = sb.run("doctor")
        negative("find_tools that takes a toolchain tool without its stamp",
                 [] if re.search(r"^  MISSING  glslangValidator", out, re.M) else ["taken"])
    sb2, p2, out2 = run(edit=('TC_ORDER="ninja cmake openssl zstd xz python pkgconf spirv-tools glslang llvm"',
                              'TC_ORDER="ninja cmake openssl zstd xz python pkgconf glslang spirv-tools llvm"'), tag="-neg1")
    negative("a toolchain built out of order (glslang before SPIRV-Tools)", p2)
    sb3, p3, out3 = run(edit=('  if [ "$(tools_from)" = source ]; then deps_source "$missing_xcode"; return 0; fi\n', ''), tag="-neg2")
    negative("deps that goes the Homebrew way on an Intel Mac", p3)


# Homebrew's offer on the imitated Intel Mac of arm_source_some: formula -> its command in <prefix>/bin (as Homebrew links
# them; python3 is python@3.14's), the commands a shell then finds on PATH before macOS's own
NOBREW_LINKS = {"gpatch": ("gpatch", "bin/gpatch"), "ninja": ("ninja", "bin/ninja"), "cmake": ("cmake", "bin/cmake"),
                "pkgconf": ("pkg-config", "bin/pkg-config"), "python@3.14": ("python3", "bin/python3.14"),
                "glslang": ("glslangValidator", "bin/glslangValidator")}


def nobrew_sandbox(tag, edits=()):
    """an Intel Mac, NM_TOOLS_FROM unset (auto), whose Homebrew at its fixed place (brew_places, NM_BREW unset) offers
    cmake, ninja, pkgconf, python3, gpatch, glslang, SPIRV-Tools and llvm, with their commands linked into its bin
    folder, which comes first on PATH"""
    sb = Sandbox("nobrew" + tag, tools_from=None,
                 formulas=tuple(NOBREW_LINKS) + ("spirv-tools", "llvm"))
    b = sb.dir / "brew"
    for f, (cmd, rel) in NOBREW_LINKS.items():
        (b / "bin" / cmd).symlink_to(b / "opt" / f / rel)
    sb.break_script(BREW_PLACES_LINE, f'brew_places() {{ echo "{b}/bin/brew /nonexistent/bin/brew"; }}')
    # Homebrew's bash, first on PATH: it notes what it runs, then runs it with the system's bash
    (b / "bin/bash").write_text(f'#!/bin/sh\necho "$*" >> {sb.dir}/brew-bash-runs\nexec /bin/bash "$@"\n')
    (b / "bin/bash").chmod(0o755)
    for old, new in edits:
        sb.break_script(old, new)
    sb.nobrew_env = {"NM_BREW": "", "PATH": f"{b}/bin:{sb.bin}"}
    return sb


def nobrew_problems(sb):
    """the claims of the source way on that Mac, each with its own list of problems: deps builds all ten tools;
    tools/bin leads to none of Homebrew's (patch: the patch on PATH, macOS's); the Python environment is made from the
    toolchain's Python; no brew call in deps, doctor, fetch or build; doctor names the copies it does not use; the NVK
    step's pkg-config is the toolchain's"""
    P = {k: [] for k in ("built", "links", "patch", "venv", "brew", "doctor", "nvkpc", "bash")}
    env = sb.nobrew_env
    brew = str(sb.dir / "brew")
    tc = str(sb.work / "tools/toolchain")
    before = sb.listing(sb.home)
    sb.clear()
    rc, out = sb.run("deps", "-y", extra=env)
    calls = brew_calls(sb)
    runs = sb.dir / "brew-bash-runs"
    tcb = [l for l in runs.read_text().splitlines() if "toolchain.sh" in l] if runs.exists() else []
    if tcb:
        P["bash"].append(f"Homebrew's bash ran build/toolchain.sh: {tcb[:2]}")
    P["built"] = [x for x in source_deps_problems(sb, rc, out, before) if not x.startswith("Homebrew was called")]
    if calls:
        P["brew"].append(f"deps called brew: {calls[:3]}")
    tbin = sb.work / "tools/bin"
    for l in sorted(tbin.iterdir()) if tbin.is_dir() else []:
        r = os.path.realpath(l)
        if r.startswith(brew + "/"):
            P["links"].append(f"tools/bin/{l.name} leads to {r}")
        if l.name in ("ninja", "cmake", "pkg-config", "glslangValidator", "llvm-config") and not r.startswith(tc + "/"):
            P["links"].append(f"tools/bin/{l.name} is not the toolchain's: {r}")
    pt = (tbin / "patch").read_text() if (tbin / "patch").is_file() else ""
    if f"exec {sb.bin}/patch -f" not in pt:
        P["patch"].append(f"tools/bin/patch is not the patch on PATH (macOS's): {pt[-200:]!r}")
    cfg = sb.work / "tools/venv/pyvenv.cfg"
    home = cfg.read_text().split("=", 1)[1].strip() if cfg.exists() else ""
    if not home.startswith(tc + "/"):
        P["venv"].append(f"the Python environment was made from {home or 'nothing'}")
    sb.clear()
    rc, dout = sb.run("doctor", extra=env)
    if brew_calls(sb):
        P["brew"].append(f"doctor called brew: {brew_calls(sb)[:3]}")
    for n in ("ninja", "cmake", "pkg-config", "python3", "glslangValidator", "llvm-config", "SPIRV-Tools"):
        if not re.search(rf"^  note     {re.escape(n)}: .* not used: with tools from source only the toolchain's", dout, re.M):
            P["doctor"].append(f"doctor does not name Homebrew's {n} as not used")
    if not re.search(r"^  note     Homebrew \S+: not used", dout, re.M) or re.search(r"^  ok       Homebrew", dout, re.M):
        P["doctor"].append("doctor does not say that Homebrew is not used")
    if "doctor: everything required is present" not in dout:
        P["doctor"].append("doctor is not content: " + dout[-300:])
    sb.clear()
    rc, fout = sb.run("fetch", extra=env)
    rc2, bout = sb.run("build", "nvk", extra=env)
    if brew_calls(sb):
        P["brew"].append(f"fetch or build called brew: {brew_calls(sb)[:3]}")
    envs = [e for k, _, e in sb.steps() if k == "nvk"]
    if not envs:
        P["nvkpc"].append(f"nvk did not run (fetch {rc}, build {rc2}): {bout[-400:]}")
    else:
        found = None
        for d in envs[0].get("PATH", "").split(":"):
            c = os.path.join(d, "pkg-config")
            if os.path.isfile(c) and os.access(c, os.X_OK):
                found = os.path.realpath(c)
                break
        if found != tc + "/bin/pkg-config":
            P["nvkpc"].append(f"the NVK step's pkg-config is {found}")
    return P, out


def arm_source_some():
    """on an Intel Mac whose Homebrew offers ninja, cmake, pkgconf, python3, gpatch and more, the source way takes
    nothing from it: deps builds all ten tools, tools/bin leads to none of Homebrew's, no brew is called, the NVK step's
    pkg-config is the toolchain's. With NM_TOOLS_FROM=homebrew the same Mac uses Homebrew's as before."""
    sb = nobrew_sandbox("")
    # first the Homebrew way on this Mac: as before, Homebrew's tools are taken and nothing is built from source
    sb.clear()
    rc, out = sb.run("deps", "-y", extra=dict(sb.nobrew_env, NM_TOOLS_FROM="homebrew"))
    tbin = sb.work / "tools/bin"
    viabrew = {l.name for l in tbin.iterdir() if os.path.realpath(l).startswith(str(sb.dir / "brew") + "/")} if tbin.is_dir() else set()
    hb_ok = rc == 0 and not tc_builds(sb) and {"ninja", "cmake", "pkg-config", "glslangValidator", "llvm-config"} <= viabrew
    check("NM_TOOLS_FROM=homebrew on that Mac: Homebrew's tools are taken as before, nothing is built from source",
          hb_ok, (rc, sorted(viabrew), [t for t, _ in tc_builds(sb)], out[-600:]))
    P, out = nobrew_problems(sb)
    names = {"built": "deps builds all ten tools in order (none counts as there because Homebrew has it) and ends with everything present",
             "links": "tools/bin leads to none of Homebrew's tools: ninja, cmake, pkg-config, glslangValidator and llvm-config are the toolchain's",
             "patch": "tools/bin/patch runs the patch on PATH (macOS's), not Homebrew's gpatch",
             "venv": "the Python environment made by the Homebrew way is made again from the toolchain's Python",
             "brew": "no brew call at all in deps, doctor, fetch and build nvk",
             "doctor": "doctor names each copy Homebrew offers as not used, says Homebrew is not used, and is content",
             "nvkpc": "the NVK step's pkg-config is the toolchain's",
             "bash": "build/toolchain.sh runs with the system's PATH, not under Homebrew's bash"}
    for k, n in names.items():
        check("source way on an Intel Mac with a full Homebrew: " + n, not P[k], (P[k], out[-800:]))
    # one control per claim: build.sh changed so that exactly that claim breaks
    controls = [
        ("built", "find_tools that counts Homebrew's tools as there with tools from source",
         [('  [ "$(tools_from)" != source ] || tc=1\n', '')]),
        ("links", "a source way that takes Homebrew's ninja",
         [('    T_NINJA=$(pick "$NM_PIN_TOOL_NINJA_MIN" --version "$(tc_path ninja bin/ninja)")\n',
           '    T_NINJA=$(pick "$NM_PIN_TOOL_NINJA_MIN" --version "$(brew_opt ninja)/bin/ninja" "$(tc_path ninja bin/ninja)")\n')]),
        ("patch", "a source way that prefers Homebrew's gpatch",
         [('  [ "$tc" = 0 ] || cands=( "$(path_tool patch)" "$(brew_opt gpatch)/bin/gpatch"', '  [ "$tc" = 0 ] || cands=( "$(brew_opt gpatch)/bin/gpatch" "$(path_tool patch)"')]),
        ("venv", "a Python environment that counts whatever Python made it",
         [('     && venv_of_python; then', '     ; then')]),
        ("brew", "doctor that asks brew its version with tools from source",
         [('then d_note "Homebrew $BREW: not used, with tools from source build.sh calls no brew"',
           'then run_brew -- --version >/dev/null 2>&1 || true; d_note "Homebrew $BREW: not used, with tools from source build.sh calls no brew"')]),
        ("doctor", "doctor without the notes on unused copies", [('  tc_unused\n', '')]),
        ("bash", "a source way that runs build/toolchain.sh with the caller's PATH",
         [('"MACOSX_DEPLOYMENT_TARGET=${MACOSX_DEPLOYMENT_TARGET:-15.0}" \\\n    "PATH=$TC_SYSPATH"', '"MACOSX_DEPLOYMENT_TARGET=${MACOSX_DEPLOYMENT_TARGET:-15.0}"'),
          ('tc_sh() { env "PATH=$TC_SYSPATH" bash', 'tc_sh() { bash')]),
        ("nvkpc", "a source way that takes Homebrew's pkg-config",
         [('    T_PKGCONFIG=$(pick "" "" "$(tc_path pkgconf bin/pkg-config)")\n',
           '    T_PKGCONFIG=$(pick "" "" "$(brew_opt pkgconf)/bin/pkg-config" "$(tc_path pkgconf bin/pkg-config)")\n')]),
    ]
    for key, name, edits in controls:
        s = nobrew_sandbox("-" + key, edits)
        if key == "venv":   # the Homebrew way first, as above, so that a venv of Homebrew's Python is there
            s.run("deps", "-y", extra=dict(s.nobrew_env, NM_TOOLS_FROM="homebrew"))
        negative(name, nobrew_problems(s)[0][key])
    s = nobrew_sandbox("-hbneg", [('    *) printf \'%s\' "$NM_TOOLS_FROM" ;;', '    *) printf source ;;')])
    s.clear()
    rc, out = s.run("deps", "-y", extra=dict(s.nobrew_env, NM_TOOLS_FROM="homebrew"))
    negative("NM_TOOLS_FROM=homebrew that builds from source", [] if not tc_builds(s) else ["built from source"])


def arm_source_fail():
    """a tool that fails stops deps as a failed step does; the next deps starts at that tool"""
    sb = source_sandbox("srcfail")
    (sb.ctl / "tc-fail-spirv-tools").write_text("")
    sb.clear()
    rc, out = sb.run("deps", "-y")
    got = [t for t, _ in tc_builds(sb)]
    ok = (rc == 1 and "STOP: step 'toolchain-spirv-tools' failed" in out and "logs/toolchain/spirv-tools.log" in out
          and "STANDIN-TC-FAILURE" in out and "report file:" in out and got[-1] == "spirv-tools" and "llvm" not in got)
    check("a failing SPIRV-Tools build: exit 1, its log named and quoted, a report file, nothing after it built", ok, (rc, got, out[-1200:]))
    (sb.ctl / "tc-fail-spirv-tools").unlink()
    sb.clear()
    rc, out = sb.run("deps", "-y")
    got = [t for t, _ in tc_builds(sb)]
    check("the next deps starts at SPIRV-Tools", rc == 0 and got == ["spirv-tools", "glslang", "llvm"], (rc, got, out[-600:]))


def arm_source_ownpy():
    """a Python of the tester's own (NM_PYTHON) that is older than the host tests want: it stays the one used, the
    toolchain's Python is built beside it once, and the next deps builds nothing"""
    def run(edit=None, tag=""):
        sb = source_sandbox("srcownpy" + tag)
        if edit:
            sb.break_script(*edit)
        own = sb.dir / "own/bin/python3.12"
        sb.wrap(own, "py", "3.12.9")
        env = {"NM_PYTHON": str(own)}
        sb.clear()
        rc1, out1 = sb.run("deps", "-y", extra=env)
        first = [t for t, _ in tc_builds(sb)]
        sb.clear()
        rc2, out2 = sb.run("deps", "-y", extra=env)
        again = [t for t, _ in tc_builds(sb)]
        p = []
        if "python" not in first:
            p.append(f"the first deps built {first}, not the toolchain's Python")
        if again:
            p.append(f"the second deps built {again} again (exit {rc2}): {out2[-300:]}")
        return p, out1
    p, out = run()
    check("NM_PYTHON older than the tests want: the toolchain's Python is built once, a second deps builds nothing", not p, (p, out[-600:]))
    p2, _ = run(edit=('{ ver_ge "${pyv:-0}" "$NM_PIN_TOOL_PYTHON_TESTS_MIN" || tc_current python; }',
                      'ver_ge "${pyv:-0}" "$NM_PIN_TOOL_PYTHON_TESTS_MIN"'), tag="-neg")
    negative("a deps that builds the toolchain's Python again whenever NM_PYTHON is older than the tests want", p2)


def arm_source_modes():
    """dry run, --no-install, --offline, too little space, a working directory with a blank, the pins, the modes"""
    sb = source_sandbox("srcmodes")
    sb.clear()
    rc, out = sb.run("--dry-run", "deps")
    ok = (rc == 0 and not tc_builds(sb) and not mutating(sb.jlines())
          and re.search(r"^    env NM_TC_PREFIX=\S+/tools/toolchain NM_TC_SRC=\S+ NM_TC_WORK=\S+ NM_TC_LOGS=\S+ .*NM_TC_STAMP=.* bash build/toolchain.sh build llvm$", out, re.M)
          and re.search(r"^  fetch    LLVM .* from https://github\.com/llvm/llvm-project\.git", out, re.M))
    check("--dry-run deps prints the fetches and the build/toolchain.sh commands with their settings, runs nothing", bool(ok), out[-1500:])
    sb.clear()
    rc, out = sb.run("deps", "--no-install", "-y")
    check("deps --no-install builds nothing and says so", not tc_builds(sb) and "not building (--no-install)" in out
          and not mutating(sb.jlines()), out[-800:])
    sb.clear()
    rc, out = sb.run("deps", "--offline", "-y")
    ok = not tc_builds(sb) and not [l for l in sb.jlines() if l.startswith("git fetch")] and "offline: the sources of ninja cmake" in out
    check("--offline with no sources fetched: names them, builds and fetches nothing", ok, out[-800:])
    (sb.state / "df-kb").write_text(str(3 * 1024 * 1024) + "\n")
    sb.clear()
    rc, out = sb.run("deps", "-y")
    ok = rc == 2 and "needs about 10 GB free" in out and "3 GB are free" in out and not [l for l in sb.jlines() if l.startswith("git ") and " fetch " in l]
    check("too little free space for LLVM: deps stops before any fetch with the numbers", ok, (rc, out[-500:]))
    (sb.state / "df-kb").unlink()
    (sb.ctl / "tc-fail-ninja").write_text("")
    sb.clear()
    sb.run("deps", "-y")   # fetches every source, then ninja fails
    (sb.ctl / "tc-fail-ninja").unlink()
    sb.clear()
    rc, out = sb.run("deps", "--offline", "-y")
    got = [t for t, _ in tc_builds(sb)]
    check("--offline with the sources fetched: builds the tools, fetches nothing (pkgconf waits for the Python environment, "
          "which cannot be filled offline)",
          got == ["ninja", "cmake", "openssl", "zstd", "xz", "python", "spirv-tools", "glslang", "llvm"]
          and "pkgconf is built with meson" in out and not [l for l in sb.jlines() if l.startswith("git fetch")], (got, out[-600:]))
    wd = str(sb.home / "my build")
    sb.clear()
    rc, out = sb.run("--workdir", wd, "deps", "-y")
    check("a working directory with a blank: deps stops at the start with one line naming it, nothing fetched or built",
          rc == 2 and "contains a space" in out and not tc_builds(sb) and not os.path.exists(wd), (rc, out[-400:]))
    # every ASCII character but letters, digits and % + - . / @ _ (and the control characters) is refused: a colon would
    # split every step's PATH, a quote pkg-config's answers, & ; | ( ) < > end the release scripts' commands on the build
    # Mac (an "R&D" folder made "rm -rf <home>/R" of their first one), * ? [ ] { } are patterns there, a comma splits
    # build_nvk.sh's -Wl,-oso_prefix list, and # ! = ^ ~ are read in some places
    REFUSED = (":", ";", "'", '"', "\\", "$", "`", "&", "|", "<", ">", "(", ")", "[", "]", "{", "}", "*", "?", "!", "#",
               "~", "^", "=", ",", "\x1b", "\x7f")
    def chars_problems(chars=REFUSED, var=None):
        p = []
        for ch in chars:
            wd = str(sb.home / f"my{ch}build")
            sb.clear()
            if var:   # a setting of the tester's that the release scripts put into their commands
                rc, out = sb.run("deps", "-y", extra={var: wd})
            else:
                rc, out = sb.run("--workdir", wd, "deps", "-y")
            if rc != 2 or "may hold only letters, digits and % + - . / @ _" not in out or tc_builds(sb) or os.path.exists(wd):
                p.append(f"{ch!r}: exit {rc}, {out[-200:]!r}")
        return p
    p = chars_problems()
    check("a working directory with any of : ; ' \" \\ $ ` & | < > ( ) [ ] { } * ? ! # ~ ^ = , or a control character: "
          "deps stops at the start with one line naming it, nothing fetched or built", not p, p)
    p = [x for v in ("NULLMOTH_BUILD_ROOT", "NULLMOTH_RUST_BIN", "NULLMOTH_CARGO_HOME", "NM_OGKM620", "NM_OGKM620_PRISTINE")
         for x in chars_problems(("&", "("), var=v)]
    check("NULLMOTH_BUILD_ROOT, NULLMOTH_RUST_BIN, NULLMOTH_CARGO_HOME, NM_OGKM620 or NM_OGKM620_PRISTINE with & or (: "
          "deps stops at the start, nothing fetched or built", not p, p)
    # the control: letters of another alphabet and the six characters kept are taken
    wd = str(sb.home / "años" / "nm_build+1@x%y.z-über")
    sb.clear()
    rc, out = sb.run("--workdir", wd, "--dry-run", "deps")
    check("a working directory with accented letters and % + - . @ _ is taken (dry run)",
          rc == 0 and "may hold only" not in out, (rc, out[-400:]))
    with sb.scratch():
        sb.break_script('nm_unsafe_path() { case "$1" in', 'nm_unsafe_path() { return 1; case "$1" in')
        negative("a build.sh that takes a working directory with an ampersand", [x for x in chars_problems() if x.startswith("'&'")])
    # the three release scripts guard a run by hand with build.sh's own pattern: one text in the four files
    def pattern_problems(texts):
        pats = {n: re.findall(r'\*\[\]\\\[[^\n]*?\[:cntrl:\]\]\*\)', t) for n, t in texts.items()}
        return ([f"{n}: {len(v)} copies of the pattern" for n, v in pats.items() if len(v) != 1]
                or [f"{n}: {v[0]} is not build.sh's {pats['build.sh'][0]}" for n, v in pats.items() if v != pats["build.sh"]])
    texts = {n: (ROOT / n).read_text() for n in ("build.sh", "build/build_plugin_release.sh", "build/build_xlate_release.sh",
                                                  "build/build_nvencvt.sh")}
    p = pattern_problems(texts)
    check("build.sh's nm_unsafe_path and the guards of the plugin, translator and video release scripts are one pattern", not p, p)
    texts["build/build_nvencvt.sh"] = texts["build/build_nvencvt.sh"].replace("\\&", "", 1)
    negative("a video release script whose guard takes an ampersand", pattern_problems(texts))
    # NM_TOOLS_FROM on each processor
    def mode(arch, value):
        s = Sandbox(f"mode-{arch}-{value or 'unset'}", cpu=arch, arch=arch, tools_from=value, formulas=("cmake",))
        rc, out = s.run("--dry-run", "deps")
        return ("source" if "deps builds from pinned sources:" in out else
                "homebrew" if "Homebrew formulas to install" in out else f"neither (exit {rc}: {out[-300:]})"), out
    res = {(a, v): mode(a, v) for a in ("x86_64", "arm64") for v in (None, "source", "homebrew")}
    want = {("x86_64", None): "source", ("arm64", None): "homebrew", ("x86_64", "source"): "source",
            ("arm64", "source"): "source", ("x86_64", "homebrew"): "homebrew", ("arm64", "homebrew"): "homebrew"}
    bad = {k: res[k][0] for k in want if res[k][0] != want[k]}
    check("NM_TOOLS_FROM: auto is source on an Intel Mac and homebrew on Apple silicon; source and homebrew forced on each", not bad, bad)
    check("the Homebrew way on an Intel Mac keeps its warning about hours of compiling and points to NM_TOOLS_FROM=source",
          "compiles each formula above" in res[("x86_64", "homebrew")][1] and "NM_TOOLS_FROM=source" in res[("x86_64", "homebrew")][1],
          res[("x86_64", "homebrew")][1][-800:])
    rc, out = sb.run("doctor", extra={"NM_TOOLS_FROM": "brew"})
    check("an unknown NM_TOOLS_FROM stops with one line that names the three", rc == 2 and
          "NM_TOOLS_FROM must be auto, source or homebrew (got 'brew')" in out, (rc, out[-300:]))
    with sb.scratch():
        sb.break_script('    auto) if [ "$(mac_arch)" = x86_64 ]; then printf source; else printf homebrew; fi ;;',
                        '    auto) printf homebrew ;;')
        rc, out = sb.run("--dry-run", "deps")
        negative("auto that means Homebrew on an Intel Mac", [] if "deps builds from pinned sources:" in out else ["homebrew"])
    # the pins: LLVM's tag of NM_PIN_TOOL_LLVM_MAJOR, Python's of NM_PIN_TOOL_PYTHON_TESTS_MIN
    with sb.scratch():
        pins = sb.co / "build/pins.sh"
        pins.write_text(pins.read_text().replace("NM_PIN_TCLLVM_REF=llvmorg-22.1.8", "NM_PIN_TCLLVM_REF=llvmorg-21.1.8")
                        .replace("NM_PIN_TCPYTHON_REF=v3.14.10", "NM_PIN_TCPYTHON_REF=v3.13.9"))
        rc, out = sb.run("doctor")
        check("a toolchain LLVM pin of another major and a Python pin of another minor: two pin FAIL lines",
              "pin FAIL  LLVM (toolchain) = llvmorg-21.1.8" in out and "pin FAIL  Python (toolchain) = v3.13.9" in out, out[-1500:])
        sb.break_script('    "llvmorg-$NM_PIN_TOOL_LLVM_MAJOR".*) say', '    *) say')
        rc, out = sb.run("doctor")
        negative("a pin check that takes any LLVM tag", [] if "pin FAIL  LLVM (toolchain)" in out else ["passed"])


def arm_deps(sb):
    before = sb.listing(sb.home)
    sb.clear()
    rc, out = sb.run("deps", "-y")
    p = deps_problems(sb, rc, out, before)
    check("deps --yes installs exactly the missing formulas, Python and Rust in the working directory, no sudo, nothing outside", not p,
          (p, out[-1500:]))
    sb.clear()
    rc, out = sb.run("deps", "-y")
    again = mutating(sb.jlines())
    check("deps a second time installs nothing", rc == 0 and not again, (rc, again, out[-500:]))
    rc, out = sb.run("doctor")
    check("doctor after deps: everything required is present (exit 0)", rc == 0 and "doctor: everything required is present" in out,
          out[-1500:])


def arm_deps_control():
    sb = Sandbox("deps-neg", formulas=("cmake",))
    sb.break_script('[ -n "$T_CMAKE" ] || want+=( cmake )', 'want+=( cmake )')
    before = sb.listing(sb.home)
    rc, out = sb.run("deps", "-y")
    negative("deps that asks Homebrew for a formula already installed", deps_problems(sb, rc, out, before))
    sb = Sandbox("deps-neg2", formulas=("cmake",))
    sb.break_script('PIP_CACHE_DIR="$TOOLS/pip-cache" ', '')
    before = sb.listing(sb.home)
    rc, out = sb.run("deps", "-y")
    negative("pip with its own cache (~/Library/Caches/pip, outside the working directory)", deps_problems(sb, rc, out, before))
    sb = Sandbox("deps-neg3", formulas=("cmake",))
    sb.break_script('HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK=1 HOMEBREW_NO_INSTALL_CLEANUP=1 -- install', 'HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK=1 -- install')
    before = sb.listing(sb.home)
    rc, out = sb.run("deps", "-y")
    p = deps_problems(sb, rc, out, before)
    negative("brew install followed by Homebrew's clean-up (HOMEBREW_NO_INSTALL_CLEANUP not set)", p)
    check("control: that clean-up removes an older version of a formula of the tester's",
          any("older version of a formula" in x for x in p), p)


def llvm23_problems(sb, rc, out):
    """Homebrew's llvm is 23 (as homebrew-core is today): deps installs llvm@22, not Homebrew's spirv-llvm-translator,
    and builds the pinned SPIRV-LLVM-Translator v22 against llvm@22 into the working directory."""
    p = []
    j = sb.jlines()
    inst = [l.split()[2] for l in j if l.startswith("brew install ")]
    if "spirv-llvm-translator" in inst:
        p.append("Homebrew's spirv-llvm-translator (built for its LLVM 23) was installed")
    if "llvm@22" not in inst:
        p.append(f"llvm@22 was not installed: {inst}")
    if not any(l.startswith("git ") and " fetch " in l and "KhronosGroup/SPIRV-LLVM-Translator.git" in l and "refs/tags/v22.1.8" in l for l in j):
        p.append("SPIRV-LLVM-Translator v22.1.8 was not fetched from its pinned URL")
    conf = [l for l in j if l.startswith("cmake -S ")]
    llvm22 = str(sb.dir / "brew/opt/llvm@22/lib/cmake/llvm")
    if not conf or llvm22 not in conf[0] or "-DLLVM_EXTERNAL_SPIRV_HEADERS_SOURCE_DIR=" not in conf[0]:
        p.append(f"the translator was not configured against llvm@22 and the pinned headers: {conf[:1]}")
    elif str(sb.work) not in conf[0].split("-DCMAKE_INSTALL_PREFIX=")[1].split()[0]:
        p.append("the translator is not installed into the working directory")
    if rc != 0:
        p.append(f"deps exit {rc}")
    return p


def translator_llvm_problems(sb):
    """sb: deps built the translator against Homebrew's llvm@22 (22.1.3)"""
    p = []
    rec = sb.work / "tools/spirv-llvm-translator/.nm-llvm"
    want = f"22.1.3 {sb.dir / 'brew/opt/llvm@22'}\n"
    if not rec.exists() or rec.read_text() != want:
        p.append(f"the note is {rec.read_text() if rec.exists() else 'missing'!r}, expected {want!r}")
    def builds(extra=None):
        sb.clear()
        rc, out = sb.run("deps", "-y", extra=extra)
        return rc, [l for l in sb.jlines() if l.startswith("cmake -S ")], out
    if rec.exists():
        rec.unlink()
    rc, conf, out = builds()
    if rc != 0 or len(conf) != 1 or "built against another LLVM, or before build.sh noted which" not in out:
        p.append(f"a translator without the note was not built again: exit {rc}, {conf}")
    mine = sb.home / "llvm-22-elsewhere"
    sb.wrap(mine / "bin/llvm-config", "ver", "22.1.3")
    (mine / "lib").mkdir(parents=True, exist_ok=True)
    (mine / "lib/libclang.dylib").write_text("")
    extra = {"NM_LLVM_PREFIX": str(mine)}
    rc, out = sb.run("doctor", extra=extra)
    if "was not built against the LLVM at" not in out:
        p.append("doctor does not say that the translator was built against another LLVM")
    rc, conf, out = builds(extra)
    if rc != 0 or len(conf) != 1 or f"-DLLVM_DIR={mine}/lib/cmake/llvm" not in conf[0]:
        p.append(f"another LLVM of the same version: the translator was not built again against it: exit {rc}, {conf}")
    if rec.exists() and rec.read_text() != f"22.1.3 {mine}\n":
        p.append(f"the note after that build is {rec.read_text()!r}")
    rc, conf, out = builds(extra)
    if rc != 0 or conf:
        p.append(f"a third deps with the same LLVM built the translator again: exit {rc}, {conf}")
    return p


# doctor and a base package given as a folder (an unpacked release): its VERSION is read at once, and another release
# is named as a problem then, not by the package step an hour later. A tarball is not unpacked for it.
def basepkg_problems(script_edit=None):
    sb = Sandbox("basepkg")
    if script_edit:
        sb.break_script(*script_edit)
    p = []
    for version, want_ok in (("2.9\n", True), ("2.8\n", False), (None, False)):
        d = sb.home / f"base-{version and version.strip()}"
        d.mkdir()
        if version:
            (d / "VERSION").write_text(version)
        rc, out = sb.run("doctor", extra={"NM_BASE_PACKAGE": str(d)})
        named = [i for i in doctor_items(out) if "base package" in i]
        if want_ok and (named or not [l for l in out.splitlines() if l.strip().startswith("ok") and "base package" in l and "(release 2.9)" in l]):
            p.append(f"release 2.9 as a folder is not ok: {named}")
        if not want_ok:
            what = version.strip() if version else "unknown (it has no VERSION)"
            if not [i for i in named if f"is release {what}, which the package step refuses" in i] or rc == 0:
                p.append(f"a folder of release {what} is not named as a problem (exit {rc}): {named}")
    # a VERSION that is not a plain file (here a pipe nothing writes to) is not read: doctor would wait for ever. The
    # folder is named as of no known release
    d = sb.home / "base-fifo"
    d.mkdir()
    os.mkfifo(d / "VERSION")
    rc, out = sb.run("doctor", extra={"NM_BASE_PACKAGE": str(d)}, timeout=120)
    named = [i for i in doctor_items(out) if "~/base-fifo is release unknown" in i]
    if rc in (0, 124) or not named:
        p.append(f"a base folder whose VERSION is a pipe: exit {rc}, named {named}: {out[-300:]}")
    tgz = sb.home / "nullmoth-nvidia-2.8.tar.gz"
    tgz.write_text("not a tarball: doctor must not open it\n")
    rc, out = sb.run("doctor", extra={"NM_BASE_PACKAGE": str(tgz)})
    said = [l.strip() for l in out.splitlines() if tgz.name in l]
    if [i for i in doctor_items(out) if "base package" in i] or [l for l in said if "release" in l]:
        p.append(f"a tarball was judged by doctor: {said}")
    return p


def arm_basepkg():
    p = basepkg_problems()
    check("doctor reads an unpacked base package's VERSION and names another release (or none) at once; a tarball is left "
          "to the package step", not p, p)
    negative("a doctor that takes any folder as the base package",
             basepkg_problems(('  elif [ -d "$NM_BASE_PACKAGE" ]; then\n', '  elif false; then\n')))
    negative("a doctor that reads a tarball as if it were a folder",
             basepkg_problems(('  elif [ -d "$NM_BASE_PACKAGE" ]; then\n', '  elif [ -e "$NM_BASE_PACKAGE" ]; then\n')))
    negative("a doctor that reads whatever VERSION is",
             basepkg_problems(('v=""; if [ -f "$NM_BASE_PACKAGE/VERSION" ]; then v=$(head -c 256 "$NM_BASE_PACKAGE/VERSION"',
                               'v=""; if true; then v=$(cat "$NM_BASE_PACKAGE/VERSION"')))


def arm_llvm23():
    sb = Sandbox("llvm23", formulas=("cmake",))
    (sb.state / "brew-llvm").write_text("23.1.5\n")
    (sb.state / "brew-llvm22").write_text("")
    sb.clear()
    rc, out = sb.run("deps", "-y")
    p = llvm23_problems(sb, rc, out)
    check("Homebrew's LLVM is 23: deps installs llvm@22 and builds SPIRV-LLVM-Translator v22 into the working directory", not p,
          (p, out[-1500:]))
    rc, out = sb.run("doctor")
    check("doctor then finds SPIRV-LLVM-Translator 22.1 matching LLVM 22 and everything else (exit 0)",
          rc == 0 and "ok       SPIRV-LLVM-Translator 22.1.0.0" in out, out[-1200:])
    sb.clear()
    rc, out = sb.run("deps", "-y")
    check("deps a second time does not build the translator again", not [l for l in sb.jlines() if l.startswith("cmake -S")], out[-400:])
    # the translator belongs to the LLVM it was built against: deps notes which, and builds it again for another LLVM
    # (here one of the same version elsewhere, NM_LLVM_PREFIX) and once for a translator built before it noted that
    p = translator_llvm_problems(sb)
    check("SPIRV-LLVM-Translator: deps notes the LLVM it was built against and builds it again without that note or for "
          "another LLVM of the same version; doctor says why first", not p, p)
    neg = Sandbox("llvm23-rec-neg", formulas=("cmake",))
    (neg.state / "brew-llvm").write_text("23.1.5\n")
    (neg.state / "brew-llvm22").write_text("")
    neg.run("deps", "-y")
    neg.break_script("  ! spirv_llvm_stale\n}\n", "  return 0\n}\n")
    negative("a translator judged by its version alone", translator_llvm_problems(neg))
    bad = Sandbox("llvm23-pin", formulas=("cmake",))
    (bad.state / "brew-llvm").write_text("23.1.5\n")
    (bad.state / "brew-llvm22").write_text("")
    pins = bad.co / "build/pins.sh"
    pins.write_text(re.sub(r"NM_PIN_SPIRVHEADERS_COMMIT=[0-9a-f]{40}", "NM_PIN_SPIRVHEADERS_COMMIT=" + "1" * 40, pins.read_text()))
    rc, out = bad.run("deps", "-y")
    check("a SPIRV-Headers pin that the translator's spirv-headers-tag.conf contradicts stops deps before any build",
          rc != 0 and "spirv-headers-tag.conf" in out and not [l for l in bad.jlines() if l.startswith("cmake -S")], (rc, out[-600:]))
    neg = Sandbox("llvm23-neg", formulas=("cmake",))
    (neg.state / "brew-llvm").write_text("23.1.5\n")
    (neg.state / "brew-llvm22").write_text("")
    neg.break_script("  deps_spirv_llvm\n  deps_python\n", "  deps_python\n")
    neg.clear()
    rc, out = neg.run("deps", "-y")
    negative("deps that leaves SPIRV-LLVM-Translator to the user", llvm23_problems(neg, rc, out))


def noinstall_problems(sb, args, extra=None):
    sb.clear()
    rc, out = sb.run(*args, extra=extra)
    p = []
    if rc == 0:
        p.append("exit 0")
    m = mutating(sb.jlines())
    if m:
        p.append(f"installed or fetched: {m[:4]}")
    return p, out


def arm_noinstall():
    sb = Sandbox("noinst")
    p, out = noinstall_problems(sb, ["deps", "--no-install", "-y"])
    ok = not p and "not installing (--no-install)" in out and "ninja" in out
    check("deps --no-install --yes: says what is missing, installs nothing, exits non-zero", ok, (p, out[-800:]))
    p, out = noinstall_problems(sb, ["deps", "-y"], extra={"NM_NO_INSTALL": "1"})
    check("NM_NO_INSTALL=1 in the environment does the same", not p, (p, out[-500:]))
    (sb.dir / "nm.conf").write_text("# test\nNM_NO_INSTALL=1\n")
    p, out = noinstall_problems(sb, ["--config", str(sb.dir / "nm.conf"), "deps", "-y"])
    check("NM_NO_INSTALL=1 in a configuration file does the same", not p, (p, out[-500:]))
    p, out = noinstall_problems(sb, ["--no-install", "all"])
    check("all --no-install stops after doctor, fetches and builds nothing", not p and "--no-install was given" in out, (p, out[-500:]))
    with sb.scratch():
        sb.break_script('if [ "$NM_NO_INSTALL" = 1 ]; then say "not installing (--no-install)"', 'if false; then say "not installing (--no-install)"')
        negative("deps that ignores --no-install", noinstall_problems(sb, ["deps", "--no-install", "-y"])[0])


# deps and the python3 of the tester's shell. On an Intel Mac /usr/local/bin, Homebrew's bin, comes before /usr/bin in
# macOS's default PATH, and Homebrew links the commands of what it installs there, dependencies included: python3, pip3
# and the rest from homebrew-core's default Python. deps unlinks that formula again when, and only when, the installs
# gave <prefix>/bin a python3 it did not have, as a link into a Python formula these installs added; after the
# installs it names each other command of macOS that such a shell then finds in Homebrew first. The stand-in Homebrew
# links what state/brew-links-<formula> says when it installs <formula> (FAKE_SH: links), and its unlink removes the
# links into a keg and leaves the keg and opt/<formula>.
PY_LINKS = "{f} python3\n{f} pip3\n{f} python3.14\n"


def in_system(name):
    return any(os.path.lexists(os.path.join(d, name)) for d in SYSTEM_DIRS)


def brew_calls(sb):
    """Every call of the stand-in Homebrew, whatever it was asked (it records brew-callenv on each)."""
    return [l for l in sb.jlines() if l.split(" ", 1)[0] in ("brew", "brew-callenv", "brew-info", "brew-env")]


def pylink_sandbox(name, links=None, formulas=("cmake",), dead_python3=False, unlink_fails=False):
    sb = Sandbox(name, formulas=formulas)
    for formula, text in (links or {}).items():
        (sb.state / f"brew-links-{formula}").write_text(text)
    if dead_python3:   # a python3 of the tester's in Homebrew's bin before deps: a link into a keg that is gone
        (sb.dir / "brew/bin/python3").symlink_to("../Cellar/python@3.14/0.9/bin/python3")
    if unlink_fails:
        (sb.state / "brew-unlink-fails").write_text("")
    return sb


def pylink_problems(sb, args=("deps", "-y"), unlinked=None, linked_to=None, out_has=(), out_lacks=(), rc_ok=True):
    """Run deps; unlinked: the one formula deps must unlink, or None for no unlink; linked_to: where brew/bin/python3
    must then lead (None: there is none)."""
    sb.clear()
    rc, out = sb.run(*args)
    p = []
    if rc_ok and rc != 0:
        p.append(f"exit {rc}")
    unl = [l for l in sb.jlines() if l.startswith("brew unlink")]
    want = [f"brew unlink {unlinked}"] if unlinked else []
    if unl != want:
        p.append(f"brew unlink calls {unl}, expected {want}")
    py3 = sb.dir / "brew/bin/python3"
    now = os.readlink(py3) if py3.is_symlink() else None
    if (now is None) != (linked_to is None) or (linked_to and linked_to not in (now or "")):
        p.append(f"brew/bin/python3 leads to {now}, expected {linked_to}")
    for s in out_has:
        if s not in out:
            p.append(f"the output lacks: {s}")
    for s in out_lacks:
        if s in out:
            p.append(f"the output has: {s}")
    if not (sb.dir / "brew/Cellar/yours/1.0").is_dir():
        p.append("an older version of a formula of the tester's was removed")
    return p, out


def arm_pylink():
    # 1. the link is new and ours: unlinked once, opt stays, doctor and the Python environment still find Python
    links = {"python@3.14": PY_LINKS.format(f="python@3.14")}
    sb = pylink_sandbox("pylink-ours", links)
    p, out = pylink_problems(sb, unlinked="python@3.14",
                             out_has=("build.sh unlinked python@3.14 again (brew unlink python@3.14)", "brew link python@3.14",
                                      "brew doctor lists it among the unlinked kegs", "links it again",
                                      "deps: everything required is present"))
    opt = sb.dir / "brew/opt/python@3.14/bin/python3.14"
    if not opt.exists():
        p.append("opt/python@3.14 is gone")
    for n in ("python3", "pip3", "python3.14"):
        if os.path.lexists(sb.dir / "brew/bin" / n):
            p.append(f"brew/bin/{n} is still linked")
    rc, dout = sb.run("doctor")
    if rc != 0 or f"Python 3.14.0 ({opt}) for NVK's Python environment" not in dout:
        p.append(f"doctor does not find Homebrew's Python through opt (exit {rc}): {dout[-600:]}")
    venv = sb.work / "tools/venv"
    cfg = (venv / "pyvenv.cfg").read_text() if (venv / "pyvenv.cfg").exists() else ""
    r = subprocess.run([str(venv / "bin/python3"), "--version"], env=sb.env(), capture_output=True, text=True)
    if r.returncode != 0 or "3.14" not in r.stdout or f"home = {opt.parent}" not in cfg:
        p.append(f"the Python environment does not run through opt: {r.returncode} {r.stdout} {r.stderr} {cfg}")
    record = sb.work / "tools/.nm-brew-unlinked"
    if not record.exists() or record.read_text() != "python@3.14\n":
        p.append(f"the record of unlinked formulas is not 'python@3.14': {record.read_text() if record.exists() else 'missing'}")
    check("deps: python3 that the installs linked into the Python formula they added is unlinked once; opt stays; "
          "doctor and the Python environment still find that Python; the way back is printed", not p, (p, out[-1500:]))
    neg = pylink_sandbox("pylink-ours-neg", links)
    neg.break_script('      deps_python3_back; DEPS_PY3_UNDO=""\n', '      DEPS_PY3_UNDO=""\n')
    negative("deps that leaves the new python3 link", pylink_problems(neg, unlinked="python@3.14")[0])

    # 1b. a later deps whose install links that Python again (Homebrew links a dependency it has to upgrade) unlinks it
    # again: the formula is installed now, but the record in the working directory says it is deps' doing. A formula the
    # tester linked between the two runs is dropped from the record and left linked; clean tools removes the record
    def again_problems(s, user_links=False):
        s.clear()
        rc, out = s.run("deps", "-y")
        q = [] if rc == 0 else [f"first deps: exit {rc}: {out[-400:]}"]
        if user_links:   # brew link python@3.14 by the tester: the links and Homebrew's record of a linked keg
            (s.dir / "brew/bin/python3").symlink_to("../Cellar/python@3.14/1.0/bin/python3")
            (s.dir / "brew/var/homebrew/linked/python@3.14").symlink_to("../../../Cellar/python@3.14/1.0")
        shutil.rmtree(s.dir / "brew/opt/ninja")   # ninja is missing again; its install links python@3.14
        (s.state / "brew-links-ninja").write_text("python@3.14 python3\n")
        if user_links:
            r, o = pylink_problems(s, unlinked=None, linked_to="../Cellar/python@3.14/1.0/bin/python3")
        else:
            r, o = pylink_problems(s, unlinked="python@3.14",
                                   out_has=("which an earlier ./build.sh deps of this working directory had unlinked",))
        q += r
        if not [l for l in s.jlines() if l == "brew install ninja"]:
            q.append("the second deps did not install ninja")
        rec = s.work / "tools/.nm-brew-unlinked"
        if user_links and rec.exists() and "python@3.14" in rec.read_text().split():
            q.append(f"the record still names the formula the tester linked: {rec.read_text()!r}")
        if not user_links and (not rec.exists() or rec.read_text() != "python@3.14\n"):
            q.append(f"the record after the second unlink: {rec.read_text() if rec.exists() else 'missing'}")
        return q, o
    sb = pylink_sandbox("pylink-again", links)
    p, out = again_problems(sb)
    rc, cout = sb.run("clean", "tools")
    if (sb.work / "tools/.nm-brew-unlinked").exists():
        p.append(f"clean tools left the record (exit {rc})")
    check("deps: a later install that links again a Python formula an earlier deps unlinked is followed by its unlink; "
          "clean tools removes the record", not p, (p, out[-800:]))
    neg = pylink_sandbox("pylink-again-neg", links)
    neg.break_script('    brew_unlinked_add "$f"\n', "")
    negative("deps that does not record what it unlinked", again_problems(neg)[0])
    sb = pylink_sandbox("pylink-again-user", links)
    p, out = again_problems(sb, user_links=True)
    check("deps: a formula the tester linked after deps unlinked it is dropped from the record and stays linked", not p, (p, out[-800:]))
    neg = pylink_sandbox("pylink-again-user-neg", links)
    neg.break_script('      for n in $(brew_unlinked_names); do ! brew_linked "$n" || brew_unlinked_drop "$n"; done\n', "")
    negative("deps that keeps a formula the tester linked in the record", again_problems(neg, user_links=True)[0])

    # 1c. the record is build.sh's own file in the working directory: a link in its place is never read or written
    # through (a file elsewhere stays as it was), only names of Python formulas installed now are kept, and a record
    # that cannot be rewritten is reported without stopping deps
    def record_sandbox(name, text=None, link_to=None, user_linked=False):
        s = pylink_sandbox(name, links, formulas=("cmake", "python@3.14") if user_linked else ("cmake",))
        if user_linked:   # brew link python@3.14 by the tester: Homebrew's record of a linked keg
            lk = s.dir / "brew/var/homebrew/linked/python@3.14"
            if not lk.is_symlink():
                lk.parent.mkdir(parents=True, exist_ok=True)
                lk.symlink_to("../../../Cellar/python@3.14/1.0")
        (s.work / "tools").mkdir(parents=True)
        (s.work / ".nullmoth-build-workdir").write_text("made by build.sh\n")
        r = s.work / "tools/.nm-brew-unlinked"
        if link_to is not None:
            link_to.write_text(text)
            r.symlink_to(link_to)
        elif text is not None:
            r.write_text(text)
        return s, r

    def record_link_problems(s, r, outside, before, unlinked, want_record):
        q, o = pylink_problems(s, unlinked=unlinked)
        if outside.read_text() != before:
            q.append(f"a file outside the working directory, linked in the record's place, was changed: {outside.read_text()!r}")
        now = None if not os.path.lexists(r) else ("link" if r.is_symlink() else r.read_text())
        if now != want_record:
            q.append(f"the record is {now!r}, expected {want_record!r}")
        return q, o

    def record_links(name, edits=()):
        q = []
        s, r = record_sandbox(name + "-add", "keep me\n", link_to=pathlib.Path(TMP / "sb" / f"{name}-outside-add.txt"))
        for old, new in edits:
            s.break_script(old, new)
        q += record_link_problems(s, r, TMP / "sb" / f"{name}-outside-add.txt", "keep me\n", "python@3.14", "python@3.14\n")[0]
        s, r = record_sandbox(name + "-drop", "keep me\npython@3.14\n", link_to=pathlib.Path(TMP / "sb" / f"{name}-outside-drop.txt"),
                              user_linked=True)
        for old, new in edits:
            s.break_script(old, new)
        q += record_link_problems(s, r, TMP / "sb" / f"{name}-outside-drop.txt", "keep me\npython@3.14\n", None, "link")[0]
        return q
    p = record_links("pylink-record-link")
    check("deps: a link in the record's place is neither read nor written through; the new record replaces it", not p, p)
    negative("a record read through a link and written in place",
             record_links("pylink-record-link-neg", edits=(
                 ('  [ -f "$f" ] && [ ! -L "$f" ] && [ -r "$f" ] || return 0\n', '  [ -f "$f" ] || return 0\n'),
                 ('printf \'%s\\n\' "$1" > "$t" && mv -f "$t" "$f"; }', 'printf \'%s\\n\' "$1" > "$f"; }'))))

    def record_names_problems(name, edits=()):
        s, r = record_sandbox(name, "x\n*\npython@3.14 x\n../../python@3.14\npython@3.99\n" + "python@3.14\n" * 3 +
                              "".join(f"python@3.{i}\n" for i in range(200, 2200)))
        for old, new in edits:
            s.break_script(old, new)
        q, o = pylink_problems(s, unlinked="python@3.14")
        now = r.read_text() if r.is_file() else None
        if now != "python@3.14\n":
            q.append(f"the record after deps is {(now or '')[:200]!r}, expected only 'python@3.14'")
        return q, o
    p, out = record_names_problems("pylink-record-names")
    check("deps: the record keeps only names of Python formulas installed now, each once", not p, (p, out[-600:]))
    negative("a record whose every line is kept", record_names_problems(
        "pylink-record-names-neg", edits=(('    [ -d "$BREW_PREFIX/opt/$n" ] || continue\n', ''),
                                          ('    case "$n" in *[!a-z0-9@.]*) continue ;; esac\n', ''),
                                          ('    case "$n" in python|python@[0-9]*) ;; *) continue ;; esac\n', '')))[0])

    def record_frozen_problems(name, edits=()):
        # the tester linked the formula, so deps drops it from the record and has to rewrite it, and cannot
        s, r = record_sandbox(name, "python@3.14\n", user_linked=True)
        for old, new in edits:
            s.break_script(old, new)
        # a file that cannot be replaced or removed: immutable (chattr +i, as root on Linux; chflags uchg, its owner on a Mac)
        freeze = (["chflags", "uchg"], ["chflags", "nouchg"]) if sys.platform == "darwin" else (["chattr", "+i"], ["chattr", "-i"])
        if (sys.platform != "darwin" and os.geteuid() != 0) or not shutil.which(freeze[0][0]) or \
                subprocess.run([*freeze[0], str(r)], capture_output=True).returncode != 0:
            return None, ""
        try:
            q, o = pylink_problems(s, unlinked=None, out_has=("WARNING: could not", "deps: everything required is present"))
        finally:
            subprocess.run([*freeze[1], str(r)], capture_output=True)
        return q, o
    p, out = record_frozen_problems("pylink-record-frozen")
    if p is None:
        emit("skip: this machine cannot make a file that its user cannot replace (chattr as root on Linux, chflags on a Mac)")
    else:
        check("deps: a record it cannot rewrite is reported, and deps goes on", not p, (p, out[-600:]))
        negative("a record whose rewrite failure stops deps", record_frozen_problems(
            "pylink-record-frozen-neg", edits=(('    { mkdir -p "$TOOLS" && printf \'%s\\n\' "$1" > "$t" && mv -f "$t" "$f"; } 2>/dev/null || { rm -f "$t" 2>/dev/null || true; warn "could not write $f"; }\n',
                                                '    mkdir -p "$TOOLS" && printf \'%s\\n\' "$1" > "$t" && mv -f "$t" "$f"\n'),
                                               ('    { [ ! -e "$f" ] || rm -f "$f"; } 2>/dev/null || warn "could not remove $f"\n',
                                                '    [ ! -e "$f" ] || rm -f "$f"\n')))[0] or ["skipped"])

    # 2. the same when the Python arrives as a dependency of another formula of the list (llvm@22 needs homebrew-core's
    # default Python, python@3.15 today, to build), not as the formula deps asked for
    links = {"glslang": PY_LINKS.format(f="python@3.15").replace("python3.14", "python3.15")}
    sb = pylink_sandbox("pylink-dep", links)
    p, _ = pylink_problems(sb, unlinked="python@3.15", out_has=("brew link python@3.15",))
    check("deps: a python3 linked by a Python formula that came as a dependency of the list is unlinked as well", not p, p)
    neg = pylink_sandbox("pylink-dep-neg", links)
    neg.break_script('  case "$f" in python|python@*) ;; *) return 0 ;; esac\n', '  case "$f" in python@3.14) ;; *) return 0 ;; esac\n')
    negative("a rule that only knows the Python formula deps asked for", pylink_problems(neg, unlinked="python@3.15")[0])

    # 3. a dead python3 link of the tester's before deps: the shell skipped it, and Homebrew replaces it without asking,
    # so it counts as none and the new link is unlinked; a python3 that leads somewhere is left (5c)
    links = {"python@3.14": PY_LINKS.format(f="python@3.14")}
    sb = pylink_sandbox("pylink-before", links, dead_python3=True)
    p, _ = pylink_problems(sb, unlinked="python@3.14", linked_to=None, out_has=("brew link python@3.14",))
    check("deps: a dead python3 link in Homebrew's bin before the installs counts as none: the link Homebrew puts there is unlinked", not p, p)
    neg = pylink_sandbox("pylink-before-neg", links, dead_python3=True)
    neg.break_script('DEPS_PY3_BEFORE=0; [ ! -e "$BREW_PREFIX/bin/python3" ] || DEPS_PY3_BEFORE=1',
                     'DEPS_PY3_BEFORE=0; [ ! -e "$BREW_PREFIX/bin/python3" ] && [ ! -L "$BREW_PREFIX/bin/python3" ] || DEPS_PY3_BEFORE=1')
    negative("deps that takes a dead python3 link for the tester's python3", pylink_problems(neg, unlinked="python@3.14", linked_to=None)[0])

    # 4. the Python formula was not in this run's list: the tester had it, and an install linked it (Homebrew links a
    # dependency again when it upgrades it): no unlink, and the list names python3 when macOS has one
    links = {"glslang": "python@3.14 python3\n"}
    sb = pylink_sandbox("pylink-notours", links, formulas=("cmake", "python@3.14"))
    line = f"a shell that has {sb.dir / 'brew/bin'} before /usr/bin now finds Homebrew's python3 first; brew unlink python@3.14 puts each back"
    p, out = pylink_problems(sb, unlinked=None, linked_to="../Cellar/python@3.14/1.0/bin/python3",
                             out_has=(line,) if in_system("python3") else ())
    if re.search(r"^Homebrew formulas to install.*python@3\.14", out, re.M):
        p.append("python@3.14 was in the list")
    check("deps: no unlink of a Python formula the tester already had" + ("; the list names its python3" if in_system("python3") else ""),
          not p, (p, out[-800:]))
    neg = pylink_sandbox("pylink-notours-neg", links, formulas=("cmake", "python@3.14"))
    neg.break_script('    *"$NL$f$NL"*) brew_unlinked_has "$f" || return 0;', '    *"$NL$f$NL"*) true;')
    negative("deps that unlinks a Python formula the tester already had",
             pylink_problems(neg, unlinked=None, linked_to="../Cellar/python@3.14/1.0/bin/python3")[0])

    # 5. the unlink fails: deps says so and ends as it would have
    links = {"python@3.14": PY_LINKS.format(f="python@3.14")}
    sb = pylink_sandbox("pylink-fails", links, unlink_fails=True)
    p, out = pylink_problems(sb, unlinked="python@3.14", linked_to="../Cellar/python@3.14/1.0/bin/python3",
                             out_has=("python3: brew unlink python@3.14 failed", "deps goes on", "deps: everything required is present"))
    check("deps: a failed brew unlink is reported and deps ends as it would have", not p, (p, out[-800:]))
    neg = pylink_sandbox("pylink-fails-neg", links, unlink_fails=True)
    neg.break_script('  if run_brew -- unlink "$f"; then\n', '  run_brew -- unlink "$f"\n  if true; then\n')
    negative("deps that stops when brew unlink fails",
             pylink_problems(neg, unlinked="python@3.14", linked_to="../Cellar/python@3.14/1.0/bin/python3",
                             out_has=("deps: everything required is present",))[0])

    # 5b. an install fails after an earlier one of the same run linked a Python: deps stops, and unlinks it before
    links = {"glslang": PY_LINKS.format(f="python@3.15").replace("python3.14", "python3.15")}
    sb = pylink_sandbox("pylink-halfway", links)
    (sb.state / "brew-install-fails-spirv-tools").write_text("")
    p, out = pylink_problems(sb, unlinked="python@3.15", rc_ok=False,
                             out_has=("brew unlink python@3.15", "brew install spirv-tools failed"))
    check("deps: an install that fails after an earlier one linked a Python stops deps and unlinks that Python first", not p, (p, out[-800:]))
    neg = pylink_sandbox("pylink-halfway-neg", links)
    (neg.state / "brew-install-fails-spirv-tools").write_text("")
    neg.break_script('            { deps_python3_back; DEPS_PY3_UNDO=""; die "brew install $f failed"; }',
                     '            die "brew install $f failed"')
    negative("deps that stops at a failed install with the Python of an earlier one still linked",
             pylink_problems(neg, unlinked="python@3.15", rc_ok=False, out_has=("brew install spirv-tools failed",))[0])

    # 5c. Homebrew ends an install with exit 1 when only a link failed: a python3 of the tester's (here a link to
    # python.org's, which resolves) was in the way of the Python that came with glslang. The formula is installed: deps
    # warns and goes on, the tester's python3 untouched and nothing unlinked (it was there before)
    py_links = {"glslang": PY_LINKS.format(f="python@3.15").replace("python3.14", "python3.15")}

    def conflict_sandbox(name, old_keg=False):
        s = pylink_sandbox(name, py_links)
        (s.dir / "pyorg/bin").mkdir(parents=True)
        (s.dir / "pyorg/bin/python3").write_text("")
        (s.dir / "brew/bin/python3").symlink_to(s.dir / "pyorg/bin/python3")
        if old_keg:   # an older glslang in the Cellar, without its opt link: installed before this run
            (s.dir / "brew/Cellar/glslang/0.9").mkdir(parents=True)
        return s
    conflict_out = ("brew install glslang ended with an error, but glslang is installed", "nothing of yours was replaced",
                    "deps: everything required is present")
    sb = conflict_sandbox("pylink-conflict")
    p, out = pylink_problems(sb, unlinked=None, linked_to=str(sb.dir / "pyorg/bin/python3"), out_has=conflict_out)
    if "The `brew link` step did not complete successfully" not in out:
        p.append("the stand-in Homebrew did not refuse the link")
    if not (sb.dir / "brew/opt/glslang").is_dir() or not (sb.dir / "brew/opt/spirv-tools").is_dir():
        p.append("deps did not go on to install the formulas after glslang")
    check("deps: brew install that ends with exit 1 because a file of the tester's was in the way of a link, the formula "
          "installed: a warning, deps goes on, the tester's python3 stays", not p, (p, out[-1000:]))
    neg = conflict_sandbox("pylink-conflict-neg")
    neg.break_script('          case "$NL$DEPS_KEGS_BEFORE$NL" in *"$NL$f$NL"*) false ;; *) brew_installed "$f" ;; esac ||\n', "          false ||\n")
    negative("deps that stops at every exit status of brew install",
             pylink_problems(neg, unlinked=None, linked_to=str(neg.dir / "pyorg/bin/python3"), out_has=conflict_out)[0])
    sb = conflict_sandbox("pylink-conflict-old", old_keg=True)
    p, out = pylink_problems(sb, unlinked=None, rc_ok=False, linked_to=str(sb.dir / "pyorg/bin/python3"),
                             out_has=("STOP: brew install glslang failed",), out_lacks=("ended with an error, but",))
    check("deps: the same failure of a formula that was installed before this run (its install cannot be told apart) stops deps",
          not p, (p, out[-800:]))
    neg = conflict_sandbox("pylink-conflict-old-neg", old_keg=True)
    neg.break_script('          case "$NL$DEPS_KEGS_BEFORE$NL" in *"$NL$f$NL"*) false ;; *) brew_installed "$f" ;; esac ||\n',
                     '          brew_installed "$f" ||\n')
    negative("deps that takes a formula installed before for this install's",
             pylink_problems(neg, unlinked=None, rc_ok=False, linked_to=str(neg.dir / "pyorg/bin/python3"),
                             out_has=("STOP: brew install glslang failed",))[0])

    # 5d. Ctrl-C during the installs, after the first one linked a Python and while the second one runs: deps makes the
    # same unlink before it says "interrupted" and ends with 130
    def interrupted_problems(s):
        (s.state / "brew-links-ninja").write_text(PY_LINKS.format(f="python@3.15").replace("python3.14", "python3.15"))
        (s.ctl / "brew-block-pkgconf").write_text("")
        s.clear()
        pr = s.popen("deps", "-y")
        try:
            if not wait_for(s.ctl / "brew-started-pkgconf"):
                stop(pr)
                o, _ = pr.communicate()
                return [f"the install of pkgconf never started (exit {pr.returncode}): {o[-600:]}"], o
            os.killpg(pr.pid, signal.SIGINT)
            try:
                o, _ = pr.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                stop(pr)
                o, _ = pr.communicate()
                return ["deps did not stop after Ctrl-C"], o
        finally:
            (s.ctl / "brew-block-pkgconf").unlink(missing_ok=True)
        q = []
        if pr.returncode != 130:
            q.append(f"exit {pr.returncode}, expected 130")
        if "build.sh: interrupted" not in o:
            q.append("no message that it was interrupted")
        unl = [l for l in s.jlines() if l.startswith("brew unlink")]
        if unl != ["brew unlink python@3.15"]:
            q.append(f"brew unlink calls {unl}, expected ['brew unlink python@3.15']")
        if os.path.lexists(s.dir / "brew/bin/python3"):
            q.append(f"brew/bin/python3 is still there: {os.readlink(s.dir / 'brew/bin/python3')}")
        u, i = o.find("unlinked python@3.15 again"), o.find("build.sh: interrupted")
        if u < 0 or (i >= 0 and u > i):
            q.append("the unlink's line is missing or comes after the message that it was interrupted")
        if (s.work / "build.lock").exists():
            q.append("the lock is still there")
        return q, o
    sb = pylink_sandbox("pylink-interrupt", {})
    p, out = interrupted_problems(sb)
    check("deps: Ctrl-C during an install after an earlier one linked a Python unlinks it, says interrupted, exits 130", not p, (p, out[-800:]))
    neg = pylink_sandbox("pylink-interrupt-neg", {})
    neg.break_script('  if [ -n "$DEPS_PY3_UNDO" ]; then DEPS_PY3_UNDO=""; deps_python3_back || true; fi\n', "")
    negative("an interrupt handler that leaves the Python linked", interrupted_problems(neg)[0])

    # 6. --dry-run and --no-install: no call of Homebrew at all; the dry run states the rule
    def quiet_problems(s, args, rc_zero):
        s.clear()
        rc, out = s.run(*args)
        q = []
        if brew_calls(s):
            q.append(f"Homebrew was called: {brew_calls(s)[:3]}")
        if (rc == 0) != rc_zero:
            q.append(f"exit {rc}")
        if "--dry-run" in args and "unlink <formula>" not in out:
            q.append("the dry run does not state the unlink")
        return q
    sb = pylink_sandbox("pylink-quiet", {"python@3.14": PY_LINKS.format(f="python@3.14")})
    p = quiet_problems(sb, ("--dry-run", "deps", "-y"), True)
    check("deps --dry-run: no call of Homebrew, the unlink rule stated", not p, p)
    p = quiet_problems(sb, ("deps", "--no-install", "-y"), False)
    check("deps --no-install: no call of Homebrew at all (not even brew info)", not p, p)
    with sb.scratch():
        sb.break_script('brew_may_ask() { [ "$NM_DRY_RUN" = 0 ] && [ "$NM_OFFLINE" = 0 ] && [ "$NM_NO_INSTALL" = 0 ]; }',
                        'brew_may_ask() { [ "$NM_DRY_RUN" = 0 ] && [ "$NM_OFFLINE" = 0 ]; }')
        negative("deps --no-install that asks brew info", quiet_problems(sb, ("deps", "--no-install", "-y"), False))
    with sb.scratch():
        sb.break_script('    elif [ "$NM_DRY_RUN" = 1 ]; then for f in "${want[@]}"', '    elif false; then for f in "${want[@]}"')
        negative("a dry run of deps that installs", quiet_problems(sb, ("--dry-run", "deps", "-y"), True))

    # 7. the list of commands macOS has that Homebrew's bin now shadows, and that it can never fail deps
    links = {"glslang": "glslang nmt-homebrew-only\nglslang env\n"}   # a name no system folder has, and one every system has
    line = "now finds Homebrew's env first; brew unlink glslang puts each back"
    if in_system("env"):
        sb = pylink_sandbox("pylink-list", links)
        p, out = pylink_problems(sb, unlinked=None, out_has=(f"a shell that has {sb.dir / 'brew/bin'} before /usr/bin {line}",),
                                 out_lacks=("nmt-homebrew-only",))
        check("deps names the commands of macOS that Homebrew's bin now comes first with, and the formula to unlink", not p, (p, out[-800:]))
        with sb.scratch():
            sb.break_script("    for d in /usr/bin /bin /usr/sbin /sbin; do\n", "    for d in /nonexistent; do\n")
            negative("deps without the list", pylink_problems(sb, unlinked=None, out_has=(line,))[0])
        sb = pylink_sandbox("pylink-list-err", links)
        sb.break_script("awk '{ print $1 }' | LC_ALL=C sort -u) || return 1", "awk '{ print $1 }' | LC_ALL=C sort -u --no-such-option) || return 1")
        p, out = pylink_problems(sb, unlinked=None, out_has=("deps: everything required is present",), out_lacks=("now finds",))
        check("a failure while making that list prints nothing and does not fail deps", not p, (p, out[-800:]))
        sb = pylink_sandbox("pylink-list-err-neg", links)
        sb.break_script("awk '{ print $1 }' | LC_ALL=C sort -u) || return 1", "awk '{ print $1 }' | LC_ALL=C sort -u --no-such-option) || return 1")
        sb.break_script('      shadowed=$(deps_shadowed "$bin_before" 2>/dev/null) || shadowed=""', '      shadowed=$(deps_shadowed "$bin_before" 2>/dev/null)')
        negative("a list whose failure reaches deps",
                 pylink_problems(sb, unlinked=None, out_has=("deps: everything required is present",), out_lacks=("now finds",))[0])
    else:
        emit("skip: this machine has no env in /usr/bin, /bin, /usr/sbin or /sbin, which the list arm imitates macOS's with")


XCODE_ONLY = ["rm", "kms", "nvrm", "fb", "accel", "agdc", "nmsmn", "ryzenadj", "airdis", "dlss", "osver", "plugin", "nvencvt"]


def needs_problems(sb, args, want_steps, want_stop=None):
    """--no-install build <args> on a Mac with Xcode, macOS's patch and python3, and nothing from Homebrew"""
    sb.clear()
    rc, out = sb.run("--no-install", "build", *args)
    keys = sb.step_keys()
    p = []
    if want_stop:
        if rc == 0 or want_stop not in out:
            p.append(f"build {' '.join(args)}: exit {rc}, expected a stop naming {want_stop!r}: {out[-300:]}")
    elif rc != 0:
        p.append(f"build {' '.join(args)}: exit {rc}: {out[-400:]}")
    if keys != want_steps:
        p.append(f"build {' '.join(args)} ran {keys}, expected {want_steps}")
    return p


def arm_needs():
    sb = Sandbox("xonly")
    rc, out = sb.run("fetch")
    if rc != 0:
        check("fetch for the Xcode-only Mac", False, out[-400:])
        return
    p = needs_problems(sb, XCODE_ONLY, XCODE_ONLY)
    check("Xcode with macOS's patch and python3 builds rm, kms, nvrm, fb, accel, agdc, nmsmn, ryzenadj, airdis, dlss, osver, "
          "plugin and nvencvt", not p, p)
    for comp, need in (("vulkan", "cmake/ninja"), ("xlate", "Rust-"), ("nvk", "LLVM-")):
        p = needs_problems(sb, [comp], [], want_stop=f"{comp} needs:")
        check(f"{comp} stops before any step, naming what it needs ({need})", not p, p)
    with sb.scratch():
        sb.break_script("""    [ -n "$p" ] && [ -x "$p" ] || continue\n""",
                        """    [ -n "$p" ] && [ -x "$p" ] || continue\n    case "$("$p" --version 2>/dev/null | first_line)" in *'GNU patch'*) ;; *) continue ;; esac\n""")
        negative("a patch chosen by its name (GNU patch) instead of by what it does", needs_problems(sb, ["rm"], ["rm"]))
    with sb.scratch():
        sb.wrap(sb.bin / "patch", "patchold", "patch 2.5")
        p = needs_problems(sb, ["kexts"], [], want_stop="rm needs: a-patch-that-applies-the-chains")
        rc, out = sb.run("doctor")
        if "MISSING  a patch that applies the patch chains" not in out:
            p.append("doctor does not say that the patch found does not apply the chains")
        check("a patch that does not take the scripts' options: build stops before rm, doctor says so", not p, p)
    with sb.scratch():
        sb.wrap(sb.bin / "patch", "patchnonew", "patch 2.0-12u11-Apple")
        p = needs_problems(sb, ["--force", "rm", "nvrm"], ["rm", "kms", "nvrm"])
        p += needs_problems(sb, ["nvk"], [], want_stop="a-patch-that-creates-files")
        rc, out = sb.run("--no-install", "deps")
        if "gpatch" not in out:
            p.append("deps does not name gpatch for NVK")
        check("a patch that changes files but cannot create one: the kexts build, nvk stops, deps names gpatch", not p, p)
    with sb.scratch():
        (sb.co / "build/pins.sh").write_text((sb.co / "build/pins.sh").read_text().replace("NM_PIN_TOOL_PYTHON_KEXT_MIN=3.8", "NM_PIN_TOOL_PYTHON_KEXT_MIN=99"))
        p = needs_problems(sb, ["nvencvt"], [], want_stop="rm needs: python3-99")
        check("build nvencvt checks rm, which it pulls in, before the first step (a Python too old for rm stops it)", not p, p)
        sb.break_script("  for c in $(with_deps $list); do need_tools_for", "  for c in $list; do need_tools_for")
        negative("a check of the named components only (rm, pulled in by nvencvt, built with what it lacks)",
                 needs_problems(sb, ["nvencvt"], [], want_stop="rm needs: python3-99"))


FETCH_WANT = [("https://github.com/NVIDIA/open-gpu-kernel-modules.git", "refs/tags/620.70.06"),
              ("https://github.com/NVIDIA/open-gpu-kernel-modules.git", "refs/tags/610.57.04"),
              ("https://gitlab.freedesktop.org/mesa/mesa.git", "refs/tags/mesa-26.3.0-rc3"),
              ("https://github.com/KhronosGroup/Vulkan-Headers", "refs/tags/v1.4.367"),
              ("https://github.com/KhronosGroup/Vulkan-Loader", "refs/tags/v1.4.367"),
              ("https://github.com/NVIDIA/open-gpu-doc.git", "@OPENGPUDOC"),
              ("https://github.com/FlyGoat/RyzenAdj.git", "refs/tags/v0.19.0")]


def fetch_problems(sb, commits):
    sb.clear()
    rc, out = sb.run("fetch")
    j = sb.jlines()
    fetches = [l for l in j if l.startswith("git ") and " fetch " in l]
    p = []
    if rc != 0:
        p.append(f"exit {rc}")
    for url, ref in FETCH_WANT:
        ref = commits["OPENGPUDOC"] if ref == "@OPENGPUDOC" else ref
        if not any(url in l and ref in l for l in fetches):
            p.append(f"no fetch of {ref} from {url}")
    curls = [l for l in j if l.startswith("curl ")]
    for f in ("CMBaseObjectSPI.h", "VTVideoDecoderSPI.h"):
        if not any(f"raw.githubusercontent.com/WebKit/WebKit/3bfbc200370710495ffa440abdd31b943c83ad7a/" in l and f in l for l in curls):
            p.append(f"{f} not downloaded from the pinned URL")
    pristine = sb.work / "build/ogkm620/open-gpu-kernel-modules-620.70.06/version.mk"
    if not pristine.exists() or "620.70.06" not in pristine.read_text():
        p.append("the 620.70.06 tree was not exported")
    mesa = sb.work / "build/gpu-src/mesa"
    if not mesa.is_dir() or git("rev-parse", "HEAD", cwd=mesa, check_rc=False) != commits["MESA"]:
        p.append("the Mesa clone is not at the pinned commit")
    if not any(l.startswith("cargo fetch --locked") for l in j):
        p.append("the translator's crates were not fetched (cargo fetch --locked)")
    if (sb.work / "build/build").exists():
        p.append("the trees are laid out in <work>/build/build (NULLMOTH_BUILD_ROOT one folder too deep)")
    return p, out


def arm_fetch(sb, commits):
    with sb.scratch():
        sb.break_script('fetch -q --depth 1 "$url" "+refs/tags/$ref:refs/tags/$ref"', 'fetch -q --depth 1 "$url" "+refs/heads/main:refs/tags/$ref"')
        negative("fetch that asks for a branch instead of the pinned tag", fetch_problems(sb, commits)[0])
    with sb.scratch():
        sb.break_script("R=${NULLMOTH_BUILD_ROOT:-$W}", "R=${NULLMOTH_BUILD_ROOT:-$W/build}")
        negative("NULLMOTH_BUILD_ROOT=<work>/build (the trees in <work>/build/build, as before)", fetch_problems(sb, commits)[0])
    p, out = fetch_problems(sb, commits)
    check("fetch asks for every pinned ref at its pinned URL, verifies and lays out the sources", not p, (p, out[-1200:]))
    sb.clear()
    rc, out = sb.run("fetch")
    again = [l for l in sb.jlines() if (l.startswith("git ") and " fetch " in l) or l.startswith("curl ")]
    check("fetch a second time downloads nothing", rc == 0 and not again, (rc, again[:3], out[-400:]))


def tamper_problems(sb, extra=None, what="620.70.06"):
    sb.clear()
    rc, out = sb.run("fetch", extra=extra)
    p = []
    if rc == 0:
        p.append("a tampered source was accepted (exit 0)")
    if "the pin is" not in out:
        p.append("no message naming the pin")
    if what == "620.70.06" and (sb.work / "build/ogkm620/open-gpu-kernel-modules-620.70.06").exists():
        p.append("the tampered tree was exported anyway")
    if what == "webkit" and (sb.work / "src/webkit-spi/CMBaseObjectSPI.h").exists():
        p.append("the tampered header was kept")
    return p, out


def githooks_problems(sb):
    """./build.sh fetch on a Mac whose global git set-up has a hooks folder, a template folder and an attributes file
    that sends every file through a filter: none of them may act in what fetch makes (each leaves a mark when it does)."""
    marks = sb.dir / "git-marks"
    shutil.rmtree(marks, ignore_errors=True)
    marks.mkdir()
    hooks, tpl = sb.dir / "git-hooks", sb.dir / "git-template"
    for d, what in ((hooks, "core.hooksPath"), (tpl / "hooks", "init.templateDir")):
        d.mkdir(parents=True, exist_ok=True)
        for h in ("post-checkout", "post-merge", "reference-transaction"):
            (d / h).write_text(f'#!/bin/sh\n: > "{marks}/{h} hook of {what}"\n')
            (d / h).chmod(0o755)
    (tpl / "nm-template-mark").write_text("copied from init.templateDir\n")
    attrs = sb.dir / "git-attributes"
    attrs.write_text("* filter=nmmark\n")
    filt = sb.dir / "git-filter"
    filt.write_text(f'#!/bin/sh\n: > "{marks}/filter"\ncat\n')
    filt.chmod(0o755)
    cfg = sb.dir / "gitconfig"
    base = cfg.read_text().split("# --- githooks\n")[0]
    cfg.write_text(base + "# --- githooks\n" + f"[core]\n\thooksPath = {hooks}\n\tattributesFile = {attrs}\n[init]\n\ttemplateDir = {tpl}\n"
                   f"[filter \"nmmark\"]\n\tsmudge = {filt}\n\tclean = {filt}\n")
    if sb.work.exists():
        shutil.rmtree(sb.work)
    rc, out = sb.run("fetch")
    p = []
    if rc != 0:
        p.append(f"fetch: exit {rc}: {out[-400:]}")
    p += [f"{m.name} acted" for m in sorted(marks.iterdir())]
    p += [f"the template was copied into {str(m.parent.relative_to(sb.work))}" for m in sb.work.rglob("nm-template-mark")]
    cfg.write_text(base)
    return p


def arm_githooks():
    sb = Sandbox("githooks")
    p = githooks_problems(sb)
    check("fetch runs none of the tester's git hooks, template or filters", not p, p)
    with sb.scratch():
        sb.break_script(" -c core.hooksPath=/dev/null -c core.fsmonitor=false \\\n              -c core.attributesFile=/dev/null", "")
        negative("git without the hooks, monitor and attributes settings", githooks_problems(sb))
    with sb.scratch():
        sb.break_script(" --template= ", " ", count=2)
        negative("git init and clone without an empty template", githooks_problems(sb))


# The component scripts' own git calls (build_nvencvt.sh, build_nvk.sh, build_osver.sh, build_nvrm.sh, accel_build_all.sh,
# build_plugin_release.sh, build_xlate_release.sh, build_vulkan_loader.sh, tools/ryzenadj-macos/build.sh): rev-parse,
# status, describe, cat-file and archive of a tree; clone, sparse-checkout, fetch and checkout of one; init. The stand-in
# of each step that has such calls makes them (STEPLIB_SH, ctl/step-git) in trees named nmgit-*, as does a command sent
# through build/local_build_host.sh's ssh, which build_nvencvt.sh's are.
STEP_GIT_KEYS = ("nvrm", "accel", "ryzenadj", "osver", "plugin", "nvencvt")


def step_git_script(tree, clones, keep_sdk=False):
    # the tester's git may be Apple's /usr/bin/git, which goes through xcrun: it runs without the stand-in SDK settings
    # (the sandbox's sdk folder is no SDK); $C.sdk records what a program started the same way sees. The control
    # (keep_sdk) records them and runs no git at all
    unset = "" if keep_sdk else "env -u SDKROOT -u DEVELOPER_DIR -u TOOLCHAINS"
    return f"""set -e
case "$1" in {"|".join(STEP_GIT_KEYS)}|host) ;; *) exit 0 ;; esac
T={tree}; C={clones}/nmgit-clone-$1; U="{unset}"; G="$U {GIT}"
$U /bin/sh -c 'echo "${{SDKROOT-}}${{DEVELOPER_DIR-}}${{TOOLCHAINS-}}"' > "$C.sdk"
{"exit 0" if keep_sdk else ""}
rm -rf "$C" "$C.init"
$G -C "$T" rev-parse --short HEAD >/dev/null
$G -C "$T" status --porcelain >/dev/null
$G -C "$T" describe --tags --always HEAD >/dev/null
$G -C "$T" cat-file -e "HEAD^{{commit}}"
$G -C "$T" archive HEAD | tar -t >/dev/null
$G clone -q --filter=blob:none --no-checkout "file://$T" "$C"
$G -C "$C" sparse-checkout set d
$G -C "$C" fetch -q --tags origin
$G -C "$C" checkout -q "$($G -C "$T" rev-parse HEAD)"
$G -C "$C" status --porcelain >/dev/null
$G init -q "$C.init"
"""


def stepgit_setup(sb, keep_sdk=False):
    """A tester's ~/.gitconfig (on top of the sandbox's own) with a hooks folder, commit signing and a file system
    monitor, each a program that records where it ran; the stand-in tree nmgit-tree. Returns the record file."""
    rec = sb.dir / "git-record"
    hooks = sb.dir / "tester-hooks"
    hooks.mkdir(exist_ok=True)
    for h in ("post-checkout", "post-merge", "post-index-change", "reference-transaction", "pre-commit", "post-rewrite",
              "pre-auto-gc"):
        (hooks / h).write_text(f'#!/bin/sh\necho "hook {h} $PWD" >> "{rec}"\n')
        (hooks / h).chmod(0o755)
    for name_, line in (("tester-gpg", "gpg $PWD"), ("tester-fsmonitor", "fsmonitor $PWD")):
        (sb.dir / name_).write_text(f'#!/bin/sh\necho "{line}" >> "{rec}"\nexit 1\n')
        (sb.dir / name_).chmod(0o755)
    (sb.home / ".gitconfig").write_text(f"[include]\n\tpath = {sb.dir / 'gitconfig'}\n[core]\n\thooksPath = {hooks}\n"
                                        f"\tfsmonitor = {sb.dir / 'tester-fsmonitor'}\n[commit]\n\tgpgsign = true\n"
                                        f"[tag]\n\tgpgsign = true\n[gpg]\n\tprogram = {sb.dir / 'tester-gpg'}\n")
    tree = sb.dir / "nmgit-tree"
    if not tree.exists():
        (tree / "d").mkdir(parents=True)
        (tree / "d/f.txt").write_text("a file of a stand-in tree\n")
        git("init", "-q", tree)
        git("-C", tree, "add", "-A")
        git("-C", tree, "commit", "-q", "-m", "stand-in tree")
        git("-C", tree, "tag", "v1")
    (sb.ctl / "step-git").write_text(step_git_script(tree, sb.dir / "tmp", keep_sdk))
    return rec


def stepgit_record(rec):
    lines = rec.read_text().splitlines() if rec.exists() else []
    return [l for l in lines if "nmgit-" in l or l.startswith("gpg ")]


def stepgit_problems(sb, keep_sdk=False):
    """build (--force) of the steps with git calls, the tester's ~/.gitconfig as their global configuration"""
    rec = stepgit_setup(sb, keep_sdk)
    rec.write_text("")
    sb.clear()
    rc, out = sb.run("--no-install", "--force", "build", *STEP_GIT_KEYS, extra={"GIT_CONFIG_GLOBAL": str(sb.home / ".gitconfig")})
    p = []
    if rc != 0:
        p.append(f"build: exit {rc}: {out[-500:]}")
    if [k for k in STEP_GIT_KEYS if k not in sb.step_keys()]:
        p.append(f"ran {sb.step_keys()}, not each of {list(STEP_GIT_KEYS)}")
    got = stepgit_record(rec)
    if got:
        p.append(f"the tester's git set-up acted in a step: {got[:6]}")
    sdk = {k: (sb.dir / f"tmp/nmgit-clone-{k}.sdk").read_text().strip() for k in STEP_GIT_KEYS
           if (sb.dir / f"tmp/nmgit-clone-{k}.sdk").exists()}
    if [v for v in sdk.values() if v] or len(sdk) != len(STEP_GIT_KEYS):
        p.append(f"git got the steps' SDK settings: {sdk}")
    return p


def host_git_problems(sb, script):
    """the same git calls sent through local_build_host.sh as ssh, HOME holding the tester's ~/.gitconfig"""
    rec = stepgit_setup(sb)
    rec.write_text("")
    d = sb.dir / "hostlink"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir()
    os.symlink(script, d / "ssh")
    r = subprocess.run([str(d / "ssh"), "nullmoth-local-build", f"/bin/bash {sb.ctl / 'step-git'} host"],
                       env={"HOME": str(sb.home), "PATH": "/usr/bin:/bin", "USER": "tester"}, capture_output=True, text=True)
    p = []
    if r.returncode != 0:
        p.append(f"ssh: exit {r.returncode}: {r.stderr[-400:]}")
    got = stepgit_record(rec)
    if got:
        p.append(f"the tester's git set-up acted in a command of the build host: {got[:6]}")
    return p


def host_proxy_problems(sb, script):
    """the proxy variables reach a command of the build host (its git takes no http.proxy of the tester's any more)"""
    d = sb.dir / "hostlink-proxy"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir()
    os.symlink(script, d / "ssh")
    pv = {"https_proxy": "http://proxy.invalid:3128", "no_proxy": "localhost", "HTTPS_PROXY": "http://proxy.invalid:3129"}
    r = subprocess.run([str(d / "ssh"), "nullmoth-local-build", 'echo "$https_proxy|$no_proxy|$HTTPS_PROXY|${http_proxy-unset}"'],
                       env={"HOME": str(sb.home), "PATH": "/usr/bin:/bin", **pv}, capture_output=True, text=True)
    want = "http://proxy.invalid:3128|localhost|http://proxy.invalid:3129|unset"
    return [] if r.stdout.strip() == want else [f"the build host's command saw {r.stdout.strip()!r}, not {want!r} ({r.stderr[-200:]})"]


def host_sdk_problems(sb, script, sdk):
    """the SDKROOT a step gets (step_env) reaches the commands the release scripts send through local_build_host.sh;
    without one in the step, none is invented (an empty SDKROOT is not an SDK)"""
    d = sb.dir / "hostlink-sdk"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir()
    os.symlink(script, d / "ssh")
    env = {"HOME": str(sb.home), "PATH": "/usr/bin:/bin"}
    if sdk is not None:
        env["SDKROOT"] = sdk
    r = subprocess.run([str(d / "ssh"), "nullmoth-local-build", 'echo "${SDKROOT-unset}"'], env=env, capture_output=True, text=True)
    want = sdk if sdk else "unset"
    return [] if r.stdout.strip() == want else [f"the build host's command saw SDKROOT={r.stdout.strip()!r}, not {want!r} ({r.stderr[-200:]})"]


# NM_MESA_GIT names a Mesa clone of the tester's for NVK; the release plugin still compiles against
# <root>/build/gpu-src/mesa/include, which a fetch with NM_MESA_GIT does not make: the plugin step must stop before it
# starts, saying which folder it reads, rather than compile against no headers (or /usr/local's)
def mesagit_problems(name, own_clone, script_edit=None):
    sb = Sandbox(name)
    if script_edit:
        sb.break_script(*script_edit)
    extra = {}
    if own_clone:
        clone = sb.dir / "own-mesa"
        shutil.rmtree(clone, ignore_errors=True)
        git("clone", "-q", REMOTES / "gitlab.freedesktop.org/mesa/mesa.git", clone)
        extra["NM_MESA_GIT"] = str(clone)
    rc, out = sb.run("fetch", extra=extra)
    if rc != 0:
        return [f"fetch exit {rc}: {out[-300:]}"], False
    sb.clear()
    rc, out = sb.run("build", "plugin", extra=extra)
    ran = "plugin" in sb.step_keys() or any("build_plugin_release.sh" in l for l in sb.jlines())
    want = f"the plugin is compiled against {sb.work}/build/gpu-src/mesa/include"
    p = []
    if own_clone and (rc == 0 or ran or want.replace(str(sb.home), "~") not in out and want not in out):
        p.append(f"exit {rc}, the plugin step {'ran' if ran else 'did not run'}: {out[-300:]}")
    return p, ran


def arm_mesagit():
    p, _ = mesagit_problems("mesagit", True)
    check("with NM_MESA_GIT, build plugin stops before its step and names the folder the release plugin reads", not p, p)
    p, ran = mesagit_problems("mesagit-none", False)
    check("control: after a fetch without NM_MESA_GIT the plugin step is reached", ran and not p, p)
    # the old preflight: the headers looked for where NM_MESA_GIT (or NM_VULKAN_INCLUDE) points
    negative("a preflight that looks for the plugin's headers in the NM_MESA_GIT clone",
             mesagit_problems("mesagit-old", True, ('      [ -f "$inc/vulkan/vulkan_core.h" ] || die',
                                                   '      [ -f "${NM_VULKAN_INCLUDE:-$MESA/include}/vulkan/vulkan_core.h" ] || die'))[0])


def arm_stepgit():
    sb = Sandbox("stepgit")
    rc, out = sb.run("fetch")
    if rc != 0:
        check("fetch for the steps' git arm", False, out[-400:])
        return
    p = stepgit_problems(sb)
    check("a step's git reads none of the tester's configuration: no hooks folder, signing or file system monitor of "
          "theirs acts in a step's clone, checkout, status or archive", not p, p)
    with sb.scratch():
        sb.break_script(' GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1\n', '\n')
        p = stepgit_problems(sb)
        negative("steps that get the tester's git configuration", p)
        check("that control fails by the record alone (hooks and the monitor acted)", len(p) == 1 and "acted" in p[0], p)
    p = stepgit_problems(sb, keep_sdk=True)
    negative("a stand-in step that hands the tester's git the sandbox's SDKROOT", p)
    check("that control fails by the SDK settings alone", len(p) == 1 and "SDK settings" in p[0], p)
    p = host_git_problems(sb, sb.co / "build/local_build_host.sh")
    check("local_build_host.sh's ssh gives its command's git none of the tester's configuration", not p, p)
    bad = sb.dir / "local_build_host-without.sh"
    t = (sb.co / "build/local_build_host.sh").read_text()
    old = '\n           GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 )'
    assert old in t, "broken-script control: text not found in local_build_host.sh"
    bad.write_text(t.replace(old, ' )'))
    bad.chmod(0o755)
    p = host_git_problems(sb, bad)
    negative("a build host whose git reads the tester's configuration", p)
    check("that control fails by the record alone", len(p) == 1 and "acted" in p[0], p)
    p = host_proxy_problems(sb, sb.co / "build/local_build_host.sh")
    check("local_build_host.sh passes the proxy variables that are set to its command, and no other", not p, p)
    t2 = t.replace('      [ -n "${!v:-}" ] && envs+=( "$v=${!v}" )\n', '      :\n')
    assert t2 != t, "broken-script control: the proxy loop not found in local_build_host.sh"
    bad.write_text(t2)
    negative("a build host that drops the proxy variables", host_proxy_problems(sb, bad))
    sdk = str(sb.dir / "sdk")
    p = host_sdk_problems(sb, sb.co / "build/local_build_host.sh", sdk) + host_sdk_problems(sb, sb.co / "build/local_build_host.sh", None)
    check("local_build_host.sh passes the step's SDKROOT to its command, and sets none when the step has none", not p, p)
    t3 = t.replace('    [ -n "${SDKROOT:-}" ] && envs+=( "SDKROOT=$SDKROOT" )\n', '')
    assert t3 != t, "broken-script control: the SDKROOT line not found in local_build_host.sh"
    bad.write_text(t3)
    negative("a build host that drops SDKROOT (the plugin, translator and nvencvt compiles then search /usr/local)",
             host_sdk_problems(sb, bad, sdk))


def arm_tamper():
    rt = TMP / "remotes-tampered"
    make_remotes(rt, tamper=True)
    sb = Sandbox("tamper", remotes=rt)
    p, out = tamper_problems(sb)
    check("fetch refuses a remote whose tag points at another commit", not p, (p, out[-600:]))
    bare = sb.work / "src/git/open-gpu-kernel-modules.git"
    left = git("-C", bare, "tag", "-l", check_rc=False) if bare.exists() else ""
    check("the refused tag is not left in the local repository", "620.70.06" not in left, left)
    sb2 = Sandbox("tamperweb")
    p, out = tamper_problems(sb2, extra={"NM_WEBKIT_SPI_URL": f"file://{rt}/www/raw.githubusercontent.com/WebKit/WebKit/"
                                         "3bfbc200370710495ffa440abdd31b943c83ad7a/Source/ThirdParty/libwebrtc/Source/webrtc/webkit_sdk/WebKit"},
                             what="webkit")
    check("fetch refuses a downloaded file whose SHA-256 is not the pin", not p, (p, out[-600:]))
    sb3 = Sandbox("tamper-neg", remotes=rt)
    sb3.break_script('  if [ "$got" != "$commit" ]; then', '  if false; then')
    negative("fetch that does not compare the commit", tamper_problems(sb3)[0])


def offline_problems(sb):
    sb.clear()
    rc, out = sb.run("--offline", "fetch")
    p = []
    if rc == 0:
        p.append("exit 0 with nothing fetched yet")
    if "offline" not in out:
        p.append("the message does not say offline")
    net = [l for l in sb.jlines() if (l.startswith("git ") and " fetch " in l) or l.startswith("curl ")]
    if net:
        p.append(f"went to the network: {net[:2]}")
    return p, out


def arm_offline(mac):
    sb = Sandbox("offline")
    p, out = offline_problems(sb)
    check("--offline fetch on an empty working directory: fetches nothing, says what is missing, exits non-zero", not p, (p, out[-500:]))
    mac.clear()
    rc, out = mac.run("fetch", extra={"NM_OFFLINE": "1"})
    net = [l for l in mac.jlines() if (l.startswith("git ") and " fetch " in l) or l.startswith("curl ") or l.startswith("cargo fetch")]
    check("NM_OFFLINE=1 fetch after a fetch: verifies what is there, no network", rc == 0 and not net, (rc, net, out[-500:]))
    mac.clear()
    rc, out = mac.run("--offline", "build", "nvk")
    check("--offline build nvk stops with the reason (meson downloads NVK's crates)",
          rc != 0 and "NVK needs the network" in out and "nvk" not in mac.step_keys(), (rc, mac.step_keys(), out[-600:]))
    sb.break_script('[ "$NM_OFFLINE" = 0 ] || die "offline, and $(pin', 'true || die "offline, and $(pin')
    negative("fetch that ignores --offline", offline_problems(sb)[0])


def dry_problems(sb, args):
    before = sb.listing(sb.dir, skip=("journal",))
    sb.journal.write_text("")
    rc, out = sb.run(*args)
    after = sb.listing(sb.dir, skip=("journal",))
    p = []
    if rc != 0:
        p.append(f"exit {rc}")
    d = [x for x in diff_listing(before, after) if x != "journal"]
    if d:
        p.append(f"files changed: {d[:6]}")
    if mutating(sb.jlines()):
        p.append(f"ran: {mutating(sb.jlines())[:4]}")
    # "brew info" can download Homebrew's formula list into its cache, outside the working directory
    asked = [l for l in sb.jlines() if l.startswith("brew ") and l not in ("brew --version", "brew --prefix")]
    if asked:
        p.append(f"asked Homebrew: {asked[:3]}")
    return p, out


def arm_dryrun(mac):
    sb = Sandbox("dry")
    p, out = dry_problems(sb, ["--dry-run", "all"])
    ok = not p and "rm -rf" not in out and "build_rm620.sh" in out and "brew install" in out and "git fetch" in out
    check("--dry-run all on a Mac with nothing installed: prints the installs, fetches and builds, changes nothing", ok, (p, out[-800:]))
    p, out = dry_problems(mac, ["-n", "--force", "build"])
    check("-n --force build after a build: prints every step, runs none, stamps untouched", not p and "build_nvk.sh" in out, (p, out[-500:]))
    p, out = dry_problems(mac, ["--dry-run", "-y", "clean", "all"])
    check("--dry-run clean all deletes nothing", not p, (p, out[-300:]))
    with mac.scratch():
        mac.break_script('  if [ "$NM_DRY_RUN" = 1 ]; then say "    $(qcmd "$@")"; return 0; fi\n', '')
        negative("a dry run that runs the commands", dry_problems(mac, ["-n", "--force", "build", "fb"])[0])
    def dirty_problems(s):
        (s.co / "README.md").write_text((s.co / "README.md").read_text() + "a local change\n")
        p, out = dry_problems(s, ["-n", "--force", "build", "plugin", "xlate"])
        if "build_plugin_release.sh" not in out:
            p.append(f"the plugin step was not printed: {out[-300:]}")
        return p
    with mac.scratch():
        p = dirty_problems(mac)
        check("-n build plugin in a checkout with local changes writes nothing into it (no git stash create)", not p, p)
    with mac.scratch():
        mac.break_script('  elif [ "$NM_DRY_RUN" = 1 ]; then   # git stash create', '  elif false; then   # git stash create')
        negative("a dry run that records the working tree with git stash create", dirty_problems(mac))
    with sb.scratch():
        sb.break_script('elif [ -z "$LLVM_PREFIX" ] && ! brew_may_ask; then', 'elif false; then')
        negative("a dry run that asks Homebrew for its LLVM formulas", dry_problems(sb, ["--dry-run", "all"])[0])


def readonly_problems(sb):
    """doctor, the dry runs and env on a Mac with Homebrew and nothing installed: nothing written anywhere (the stand-in
    brew writes Homebrew's cache when "info" runs, as the real one does); deps afterwards: its "brew info" keeps the
    cache inside the working directory."""
    p = []
    for args in (["doctor"], ["--dry-run", "all"], ["-n", "deps"], ["-n", "build", "rm"], ["env"], ["--offline", "-n", "deps"]):
        before = sb.listing(sb.dir, skip=("journal",))
        sb.journal.write_text("")
        rc, out = sb.run(*args)
        after = sb.listing(sb.dir, skip=("journal",))
        d = [x for x in diff_listing(before, after) if x != "journal"]
        if d:
            p.append(f"{' '.join(args)} wrote {d[:4]}")
        asked = [l for l in sb.jlines() if l.startswith("brew-info")]
        if asked:
            p.append(f"{' '.join(args)} asked brew info ({len(asked)} times)")
    sb.clear()
    rc, out = sb.run("-y", "deps")
    cache = sb.home / "Library/Caches/Homebrew"
    if cache.exists():
        p.append("deps let brew info write ~/Library/Caches/Homebrew")
    asked = [l for l in sb.jlines() if l.startswith("brew-info")]
    if not asked:
        p.append("deps never asked brew info (the test does not see the LLVM formula choice)")
    if any(f"HOMEBREW_CACHE={sb.work}/tools/homebrew-cache" not in l for l in asked):
        p.append(f"brew info without a cache in the working directory: {asked[:2]}")
    return p


def arm_readonly():
    p = readonly_problems(Sandbox("ro"))
    check("doctor, --dry-run, -n deps, -n build and env never run brew info; deps gives it a cache in the working directory", not p, p)
    sb = Sandbox("ro-x")
    sb.break_script('brew_may_ask() { [ "$NM_DRY_RUN" = 0 ] && [ "$NM_OFFLINE" = 0 ] && [ "$NM_NO_INSTALL" = 0 ]; }', 'brew_may_ask() { true; }')
    sb.break_script('run_brew "HOMEBREW_CACHE=$TOOLS/homebrew-cache" -- info', 'run_brew -- info')
    p = readonly_problems(sb)
    negative("dry runs that ask brew info, with Homebrew's own cache (as before)",
             [x for x in p if "asked brew info" in x or "Library/Caches/Homebrew" in x or "without a cache" in x])


def order_problems(sb, args, expect):
    sb.clear()
    rc, out = sb.run(*args)
    keys = sb.step_keys()
    p = []
    if rc != 0:
        p.append(f"exit {rc}: {out[-400:]}")
    if keys != expect:
        p.append(f"steps {keys}, expected {expect}")
    return p, out


def arm_order(mac):
    with mac.scratch():
        p, out = order_problems(mac, ["build", "fb"], ["rm", "fb"])
        check("build fb builds what it needs first (rm) and nothing else", not p, p)
    with mac.scratch():
        mac.break_script('    kms)   C_DESC="NVIDIA\'s modeset core for macOS (libnvkms.a)"; C_DEPS="rm"',
                         '    kms)   C_DESC="NVIDIA\'s modeset core for macOS (libnvkms.a)"; C_DEPS=""')
        mac.break_script('ALL_COMPONENTS="rm kms ', 'ALL_COMPONENTS="kms rm ')
        negative("a build order with kms before rm", order_problems(mac, ["build"], ORDER)[0])
    # the real full build, kept for the arms that follow; CC and CFLAGS from the user's shell must not reach a step
    p, out = order_problems(mac, ["build"], ORDER)
    check("build: every component once, in the documented order", not p, (p, out[-800:]))
    return out


def vars_problems(sb, extra_ok=()):
    p = []
    w = str(sb.work)
    co = str(sb.co)
    brew = str(sb.dir / "brew")
    steps = sb.steps()
    if not steps:
        return ["no step ran"]
    for key, args, env in steps:
        for v in ("CC", "CXX", "CFLAGS", "CPPFLAGS", "LDFLAGS", "OBJC"):
            if v in env:
                p.append(f"{key}: {v} reached the step")
        for v in PLACE_VARS:
            if v not in env:
                continue
            for part in (env[v].split(":") if v in ("PATH", "PKG_CONFIG_PATH") else [env[v]]):
                part = part[len("file://"):] if part.startswith("file://") else part
                if not part.startswith("/"):
                    continue
                roots = [w, co] + (SYSTEM_DIRS if v == "PATH" else []) + ([brew] if v in ("PKG_CONFIG_PATH", "LIBCLANG_PATH") else [])
                if not under(part, roots):
                    p.append(f"{key}: {v} names {part}, outside the working directory")
        for v, val in env.items():
            if "nvmtl-build" in val or "ogkm610" in val:
                p.append(f"{key}: {v}={val} names the original author's folders")
        for a in args:
            if a.startswith("/") and not under(a, [w, co]):
                p.append(f"{key}: argument {a} is outside the working directory and the checkout")
    need = {"fb": ["OGKM"], "accel": ["OGKM"], "nvrm": ["NVRM_PRISTINE_OGKM"], "nvk": ["NVK_PATH", "RUSTUP_HOME", "CARGO_HOME"],
            "xlate": ["NULLMOTH_RUST_BIN", "NULLMOTH_CARGO_HOME", "NULLMOTH_BUILD_ROOT", "NULLMOTH_BUILD_HOST"],
            "vulkan": ["NM_KHRONOS_URL"], "nvencvt": ["NM_OGKM620", "NM_OGKM620_PRISTINE"], "rm": ["NV_VERSION"]}
    for key, args, env in steps:
        for v in need.get(key, []):
            if not env.get(v):
                p.append(f"{key}: {v} is not set")
    return p


def arm_vars(mac):
    p = vars_problems(mac)
    check("every place an existing script reads points into the working directory or the checkout, never ~/nvmtl-build",
          not p, p[:10])
    with mac.scratch():
        mac.break_script("  CARGO_DIR=$TOOLS/cargo", "  CARGO_DIR=$HOME/.cargo")
        mac.clear()
        mac.run("build", extra={"CC": "/usr/local/bin/gcc-14"})
        negative("CARGO_HOME pointed at ~/.cargo", vars_problems(mac))
    with mac.scratch():
        mac.break_script("env -u CC -u CXX", "env -u CXX")
        mac.clear()
        mac.run("build", "fb", extra={"CC": "/usr/local/bin/gcc-14"})
        negative("a CC from the user's shell left in the step's environment", vars_problems(mac))
    # A login shell may point the compiler and build-system search paths at a Homebrew of the other architecture; no
    # step may inherit them.
    leak = {"CMAKE_PREFIX_PATH": "/opt/homebrew/lib/cmake", "LIBRARY_PATH": "/opt/homebrew/lib",
            "CPATH": "/opt/homebrew/include", "PKG_CONFIG_PATH": "/opt/homebrew/lib/pkgconfig",
            "C_INCLUDE_PATH": "/opt/homebrew/include", "CPLUS_INCLUDE_PATH": "/opt/homebrew/include",
            "OBJC_INCLUDE_PATH": "/opt/homebrew/include", "PKG_CONFIG_LIBDIR": "/opt/homebrew/lib/pkgconfig",
            "LDFLAGS": "-L/opt/homebrew/lib", "CPPFLAGS": "-I/opt/homebrew/include", "HOMEBREW_PREFIX": "/opt/homebrew",
            "HOMEBREW_CELLAR": "/opt/homebrew/Cellar", "HOMEBREW_REPOSITORY": "/opt/homebrew"}

    def leak_problems(tag):
        mac.clear()
        e = dict(leak); e["FB_DEFS"] = f"-DNM_LEAK_{tag}=1"   # a fresh value each call so fb really re-runs
        rc, out = mac.run("build", "fb", extra=e)
        steps = mac.steps()
        bad = [f"{key}: {v}={env[v]} inherited from the caller" for key, args, env in steps for v in leak if v in env]
        return bad or ([] if steps else [f"no step ran (exit {rc}): {out[-800:]}"])

    with mac.scratch():
        check("a step inherits no compiler search path, LDFLAGS/CPPFLAGS or HOMEBREW_ variable from the caller", not (q := leak_problems("a")), q)
        mac.break_script("-u PKG_CONFIG_PATH -u CMAKE_PREFIX_PATH -u LIBRARY_PATH -u CPATH \\", "\\")
        negative("a step_env that keeps the caller's search-path variables", leak_problems("b"))
    with mac.scratch():
        mac.break_script("    -u C_INCLUDE_PATH -u CPLUS_INCLUDE_PATH -u OBJC_INCLUDE_PATH -u PKG_CONFIG_LIBDIR -u HOMEBREW_PREFIX -u HOMEBREW_CELLAR -u HOMEBREW_REPOSITORY \\\n", "")
        negative("a step_env that keeps clang's include paths and the caller's HOMEBREW_ variables", leak_problems("c"))


# SDKROOT. Every step, the translator's build and pip get the macOS SDK of the Xcode chosen as SDKROOT (without it Apple's
# clang searches /usr/local/include and /usr/local/lib, Homebrew's on an Intel Mac, before the SDK); the caller's own
# SDKROOT (it may name another platform's SDK) never reaches them; without an xcrun nothing is set and nothing fails.
IOS_SDK = "/some/iPhoneOS.sdk"
UNSET_LINE = "unset SDKROOT TOOLCHAINS\n"
STEP_PATH_LINE = '  STEP_PATH="$TBIN:$VENV/bin:/usr/bin:/bin:/usr/sbin:/sbin"\n'
# Apple's /usr/bin/make (and git, python3): a small program that finds the real tool through xcrun, which stops when
# SDKROOT names an SDK that is not there
APPLE_MAKE = ('#!/bin/sh\n'
              'if [ -n "${SDKROOT:-}" ] && [ ! -d "$SDKROOT" ]; then echo "xcrun: error: SDK \\"$SDKROOT\\" cannot be located" >&2; exit 1; fi\n'
              'echo "GNU Make 3.81"\n')


def sdkstep_problems(mac, tag, extra=None):
    mac.clear()
    e = {"FB_DEFS": f"-DNM_SDK_{tag}=1"}   # a fresh value each call so fb really re-runs
    e.update(extra or {})
    rc, out = mac.run("build", "fb", extra=e)
    steps = mac.steps()
    want = str(mac.dir / "sdk")
    bad = [f"{key}: SDKROOT={env.get('SDKROOT', '(not set)')}, expected {want}" for key, args, env in steps if env.get("SDKROOT") != want]
    return bad or ([] if steps else [f"no step ran (exit {rc}): {out[-800:]}"])


def arm_sdkstep(mac):
    with mac.scratch():
        check("a step gets SDKROOT = the SDK xcrun names for macosx", not (q := sdkstep_problems(mac, "a")), q)
        check("a caller's SDKROOT=/some/iPhoneOS.sdk does not reach a step; it gets the macOS SDK",
              not (q := sdkstep_problems(mac, "b", {"SDKROOT": IOS_SDK})), q)
        rc, out = mac.run("env", extra={"SDKROOT": IOS_SDK})
        check("./build.sh env prints the SDKROOT the steps get, not the caller's",
              f"export SDKROOT={mac.dir / 'sdk'}" in out and IOS_SDK not in out, out[-600:])
        rc, out = mac.run("doctor", extra={"SDKROOT": IOS_SDK})
        check("doctor names the SDK every step builds with, and that the caller's SDKROOT is not passed on",
              re.search(r"^  note     every step builds with SDKROOT=\S*/sdk \(the SDK of the Xcode above; your SDKROOT=/some/iPhoneOS\.sdk is not passed on: "
                        r"for another SDK choose its Xcode with DEVELOPER_DIR\)$", out, re.M) is not None, out[-1500:])
        mac.break_script('  [ -z "$STEP_SDK" ] || printf \'%s\\n\' "SDKROOT=$STEP_SDK"\n', "")
        negative("a step_env that only removes SDKROOT (as before)", sdkstep_problems(mac, "c"))
    # a Mac whose xcrun names no SDK: doctor still says what became of the caller's SDKROOT and TOOLCHAINS
    with mac.scratch():
        mac.break_script('  STEP_SDK=$(env -u SDKROOT xcrun --sdk macosx --show-sdk-path 2>/dev/null | first_line || true)\n', '  STEP_SDK=""\n')
        def unnamed():
            rc, out = mac.run("doctor", extra={"SDKROOT": IOS_SDK, "TOOLCHAINS": "swift"})
            p = [] if "a macOS SDK (xcrun --show-sdk-path fails)" in out else ["doctor found an SDK: " + out[-600:]]
            if not re.search(r"^  note     your SDKROOT=/some/iPhoneOS\.sdk is not passed on: .* choose its Xcode with DEVELOPER_DIR\)$", out, re.M):
                p.append("the caller's SDKROOT is not named")
            if "(your TOOLCHAINS=swift is not passed on)" not in out:
                p.append("the caller's TOOLCHAINS is not named")
            return p
        check("doctor on a Mac whose xcrun names no SDK still names the caller's SDKROOT and TOOLCHAINS, and the way to another SDK",
              not (q := unnamed()), q)
        mac.break_script('        [ -z "$CALLER_SDKROOT" ] || d_note "your SDKROOT=$CALLER_SDKROOT is not passed on: every step', '        : || d_note "every step')
        negative("a doctor that names the caller's SDKROOT only beside an SDK it found", unnamed())
    with mac.scratch():
        mac.break_script(UNSET_LINE, "unset TOOLCHAINS\n")
        mac.break_script("env -u SDKROOT xcrun --sdk macosx --show-sdk-path", "xcrun --show-sdk-path")
        negative_without_apple_tools("an SDK asked of xcrun with the caller's SDKROOT in force (it names that SDK)",
                                     lambda: sdkstep_problems(mac, "d", {"SDKROOT": IOS_SDK}))
    # build.sh's own questions to the system's tools, before a step: in the clone the system folders of the step's
    # PATH are preceded by one that holds a make like Apple's, which is asked for its version before rm (which fb is
    # built on) is looked at
    with mac.scratch():
        d = mac.dir / "usrbin"
        d.mkdir()
        (d / "make").write_text(APPLE_MAKE)
        (d / "make").chmod(0o755)
        mac.break_script(STEP_PATH_LINE, STEP_PATH_LINE.replace("/usr/bin:", f"{d}:/usr/bin:"))
        check("a caller's SDKROOT that names no SDK does not reach the make build.sh asks before a step (Apple's stops on it)",
              not (q := sdkstep_problems(mac, "e", {"SDKROOT": IOS_SDK})), q)
        mac.break_script(UNSET_LINE, "unset TOOLCHAINS\n")
        negative_without_apple_tools("a build.sh that keeps the caller's SDKROOT in its own environment (make is taken for missing: no step)",
                                     lambda: [x for x in sdkstep_problems(mac, "f", {"SDKROOT": IOS_SDK}) if "no step ran" in x and "make" in x])
    with mac.scratch():
        def toolchains_problems(tag):
            mac.clear()
            rc, out = mac.run("build", "fb", extra={"FB_DEFS": f"-DNM_TCS_{tag}=1", "TOOLCHAINS": "swift"})
            steps = mac.steps()
            bad = [f"{key}: TOOLCHAINS={env['TOOLCHAINS']}" for key, args, env in steps if "TOOLCHAINS" in env]
            return bad or ([] if steps else [f"no step ran (exit {rc}): {out[-800:]}"])
        check("a caller's TOOLCHAINS does not reach a step", not (q := toolchains_problems("a")), q)
        rc, out = mac.run("doctor", extra={"TOOLCHAINS": "swift"})
        check("doctor says that the caller's TOOLCHAINS is not passed on",
              "default toolchain of the Xcode above (your TOOLCHAINS=swift is not passed on)" in out, out[-1500:])
        rc, out = mac.run("env", extra={"TOOLCHAINS": "swift"})
        check("./build.sh env tells a shell to drop TOOLCHAINS", re.search(r"^unset TOOLCHAINS ", out, re.M) is not None, out[-600:])
        mac.break_script(UNSET_LINE, "unset SDKROOT\n")
        negative_without_apple_tools("a build.sh that leaves the caller's TOOLCHAINS in place", lambda: toolchains_problems("b"))


def sdkdeps_problems(script_edit=None):
    """deps on a Mac whose Homebrew LLVM is 23 (the translator is built) with SDKROOT=/some/iPhoneOS.sdk in the caller's
    environment: the translator's cmake runs and the Python environment's venv and pip all get the macOS SDK"""
    sb = Sandbox("sdkdeps", formulas=("cmake",))
    (sb.state / "brew-llvm").write_text("23.1.5\n")
    (sb.state / "brew-llvm22").write_text("")
    if script_edit:
        sb.break_script(*script_edit)
    sb.clear()
    rc, out = sb.run("deps", "-y", extra={"SDKROOT": IOS_SDK})
    got = [l.split()[1:] for l in sb.jlines() if l.startswith("sdkroot ")]
    want = str(sb.dir / "sdk")
    p = [] if rc == 0 else [f"deps exit {rc}: {out[-400:]}"]
    for who in ("cmake-S", "cmake--build", "cmake--install", "venv", "pip"):
        seen = [g[1] for g in got if g[0] == who]
        if not seen:
            p.append(f"{who} did not run")
        elif any(s != want for s in seen):
            p.append(f"{who} got SDKROOT={seen}, expected {want}")
    return p


def arm_sdkdeps():
    p = sdkdeps_problems()
    check("the translator's cmake, venv and pip get SDKROOT = the macOS SDK, not the caller's", not p, p)
    negative("a translator build that keeps the caller's environment as it is",
             sdkdeps_problems(('  x env -u SDKROOT ${STEP_SDK:+"SDKROOT=$STEP_SDK"} "PATH=$tpath" "$T_CMAKE" -S', '  x env "PATH=$tpath" "$T_CMAKE" -S')))
    negative("a pip without SDKROOT",
             sdkdeps_problems(('  env -u SDKROOT ${STEP_SDK:+"SDKROOT=$STEP_SDK"} PIP_CACHE_DIR=', '  env -u SDKROOT PIP_CACHE_DIR=')))


def nosdk_problems(script_edit=None):
    """not a Mac and no xcrun: ./build.sh env and doctor set no SDKROOT, even with the caller's, and do not fail on it"""
    sb = Sandbox("nosdk")
    (sb.state / "os").write_text("Linux\n")
    (sb.bin / "xcrun").unlink()
    if script_edit:
        sb.break_script(*script_edit)
    p = []
    rc, out = sb.run("env", extra={"SDKROOT": IOS_SDK})
    if rc != 0:
        p.append(f"env exit {rc}: {out[-300:]}")
    if re.search(r"^export SDKROOT=", out, re.M):
        p.append("env exports an SDKROOT without an xcrun: " + re.search(r"^export SDKROOT=.*$", out, re.M).group(0))
    if not re.search(r"^unset SDKROOT ", out, re.M):
        p.append("env does not say that no SDKROOT is set")
    rc, out = sb.run("doctor", extra={"SDKROOT": IOS_SDK})
    if "SDKROOT" in out:
        p.append("doctor speaks of an SDKROOT on a computer without xcrun")
    return p


def arm_nosdk():
    p = nosdk_problems()
    check("no xcrun (not a Mac): no SDKROOT is exported, the caller's included, and nothing fails", not p, p)
    negative("an SDK taken from the caller's SDKROOT when xcrun gives none",
             nosdk_problems(('  STEP_SDK_DONE=1; STEP_SDK=""\n', '  STEP_SDK_DONE=1; STEP_SDK=${CALLER_SDKROOT:-}\n')))


def rebuilt(sb, *args, extra=None, edit=None):
    if edit:
        p = sb.co / edit[0]
        p.write_text(p.read_text() + edit[1])
    sb.clear()
    rc, out = sb.run("build", *args, extra=extra)
    return rc, sb.step_keys(), out


def rm_again_problems(sb):
    rc, keys, out = rebuilt(sb, "--force", "rm")
    if keys != ["rm"]:
        return [f"--force build rm ran {keys}"]
    rc, keys, out = rebuilt(sb)
    want = ["kms", "nvrm", "fb", "accel", "nvencvt"]
    return [] if keys == want else [f"after rm was built again, build ran {keys}, expected {want}"]


def arm_stamps(mac):
    with mac.scratch():
        rc, keys, out = rebuilt(mac)
        check("a second build skips every finished step", rc == 0 and keys == [] and out.count(": up to date") == len(ORDER),
              (rc, keys, out[-400:]))
        rc, keys, _ = rebuilt(mac, edit=("build/fb_build.sh", "# changed\n"))
        check("a changed component script re-runs exactly that step", keys == ["fb"], keys)
        rc, keys, _ = rebuilt(mac, edit=("kexts/NVRMFB/nvrm_layout.h", "/* changed */\n"))
        check("a changed source file re-runs exactly its component", keys == ["fb"], keys)
        rc, keys, _ = rebuilt(mac, extra={"FB_DEFS": "-DNM_TEST=1"})
        check("a changed variable (FB_DEFS) re-runs exactly the step that reads it", keys == ["fb"], keys)
        rc, keys, _ = rebuilt(mac, extra={"FB_DEFS": "-DNM_TEST=1"})
        check("the same variable again re-runs nothing", keys == [], keys)
        rc, keys, _ = rebuilt(mac)
        check("the variable removed again re-runs that step", keys == ["fb"], keys)
        pins = mac.co / "build/pins.sh"
        t = pins.read_text()
        pins.write_text(t.replace("NM_PIN_VKLOADER_NAME=\"Khronos Vulkan-Loader v1.4.367\"",
                                  "NM_PIN_VKLOADER_NAME=\"Khronos Vulkan-Loader v1.4.367 (renamed)\""))
        rc, keys, out = rebuilt(mac)
        check("a changed pin re-runs exactly the steps that use that source", keys == ["vulkan"], (keys, out[-300:]))
        pins.write_text(pins.read_text() + "# a comment\n")
        rc, keys, _ = rebuilt(mac)
        check("a comment added to build/pins.sh re-runs nothing", keys == [], keys)
        rc, keys, _ = rebuilt(mac, edit=("kexts/NVRM/patches/620-color-range.patch", "\n"))
        check("a changed NVRM patch re-runs the RM and everything built from it",
              keys == ["rm", "kms", "nvrm", "fb", "accel", "nvencvt"], keys)
        (mac.work / "out/osver/libnvmtl_osver.dylib").unlink()
        rc, keys, _ = rebuilt(mac)
        check("a missing output re-runs its step", keys == ["osver"], keys)
        rc, keys, _ = rebuilt(mac, "--force", "agdc")
        check("--force re-runs the named step even when it is up to date", keys == ["agdc"], keys)
        p = rm_again_problems(mac)
        check("rm built again (--force build rm): the next build re-runs everything that reads its flags or links its archive", not p, p)
    with mac.scratch():
        mac.break_script('$(sha256_file "$STAMPS/$d" 2>/dev/null || echo none) $(dep_outputs_id "$d")"',
                         '$(sha256_file "$STAMPS/$d" 2>/dev/null || echo none)"')
        rebuilt(mac)
        negative("stamps that follow only the dependency's recipe, not its new archive", rm_again_problems(mac))
    with mac.scratch():
        mac.break_script("""  for v in $C_VARS; do eval "say \\"var $v=\\${$v:-}\\""; done\n""", "")
        rebuilt(mac)
        rc, keys, _ = rebuilt(mac, extra={"FB_DEFS": "-DNM_TEST=1"})
        negative("stamps that do not record the variables", [] if keys == ["fb"] else ["FB_DEFS change not rebuilt"])


def failure_problems(sb, home=None):
    (sb.ctl / "fail-agdc").write_text("")
    sb.clear()
    rc, out = sb.run("--force", "build", home=home)
    (sb.ctl / "fail-agdc").unlink()
    keys = sb.step_keys()
    p = []
    if rc == 0:
        p.append("exit 0")
    if keys != ORDER[:ORDER.index("agdc") + 1]:
        p.append(f"steps {keys}: later steps started, or earlier ones did not run")
    for t in ("step 'agdc' failed", "error: the compiler stopped in agdc", "full log:    ~/nullmoth-build/logs/agdc.log",
              "report file: ~/nullmoth-build/reports/failure-agdc-"):
        if t not in out:
            p.append(f"the message lacks: {t}")
    if (sb.work / "stamps/agdc").exists():
        p.append("agdc has a stamp")
    if (sb.work / "build.lock").exists():
        p.append("the lock was not released")
    return p, out


def report_problems(sb, home_link):
    reps = sorted((sb.work / "reports").glob("failure-agdc-*.txt"))
    if not reps:
        return ["no report file"]
    t = reps[-1].read_text()
    p = []
    head = git("rev-parse", "HEAD", cwd=sb.co)
    for want_ in ("step:         agdc (exit status 7)", "macOS 15.7.1", "x86_64", "xcodebuild:   Xcode 26.1", "clang:        Apple clang",
                  f"commit: {head}", "local changes:", "NM_PIN_OGKM620_COMMIT=", "NM_PIN_RUST_VERSION=1.98.1",
                  "error: the compiler stopped in agdc", "git version", "python:"):
        if want_ not in t:
            p.append(f"the report lacks: {want_}")
    for home_ in {str(sb.home), os.path.realpath(sb.home), str(home_link)}:
        if home_ in t:
            p.append(f"the report names the home folder {home_}")
    if "tester" in t:
        p.append("the report names the account")
    return p


def arm_failure(mac):
    link = mac.dir / "homelink"
    if not link.exists():
        os.symlink(mac.home, link)
    with mac.scratch():
        p, out = failure_problems(mac, home=link)
        check("a failing step stops the run: later steps do not start, the message names the step, its log and the report", not p,
              (p, out[-1200:]))
        p = report_problems(mac, link)
        check("the report file has machine, tools, commit, changes, pins, step and log tail, and no home path or account", not p, p)
        (mac.co / "kexts/NVRMFB/nvrm_layout.h").write_text("/* local change */\n")
        failure_problems(mac, home=link)
        reps = sorted((mac.work / "reports").glob("failure-agdc-*.txt"))
        t = reps[-1].read_text() if reps else ""
        check("the report lists the checkout's local changes", "local changes: 1 file(s)" in t and "kexts/NVRMFB/nvrm_layout.h" in t,
              t[-400:])
    with mac.scratch():
        mac.break_script('  CUR_STEP=""\n  exit 1\n}', '  CUR_STEP=""\n  return 0\n}')
        negative("a failed step that does not stop the run", failure_problems(mac, home=link)[0])
    with mac.scratch():
        mac.break_script('tilde() { home_as "~"; }', 'tilde() { cat; }')
        failure_problems(mac, home=link)
        negative("a report that keeps the home path", report_problems(mac, link))


def stale_problems(sb):
    """build_nvk.sh fails after an earlier good build left its library: the step must fail and keep no stamp."""
    (sb.co / "build/build_nvk.sh").write_text((sb.co / "build/build_nvk.sh").read_text() + "# changed\n")
    (sb.ctl / "fail-nvk").write_text("")
    sb.clear()
    rc, out = sb.run("build", "nvk")
    (sb.ctl / "fail-nvk").unlink()
    p = []
    if rc == 0:
        p.append("exit 0 although build_nvk.sh failed")
    if "step 'nvk' failed" not in out:
        p.append("no message naming the failed step")
    if (sb.work / "stamps/nvk").exists():
        p.append("nvk was stamped as built from the changed script, with the previous build's library")
    return p, out


def personal_problems(sb, text):
    """build_rm620.sh's exit 6 (the archive names the account or computer as a word; the script judges whole words
    itself): the step stops, rm is not stamped, and nothing builds on the archive."""
    (sb.ctl / "personal-rm").write_text(text)
    sb.clear()
    rc, out = sb.run("--force", "build", "rm", "fb")
    (sb.ctl / "personal-rm").unlink()
    p = []
    if rc == 0 or (sb.work / "stamps/rm").exists() or "fb" in sb.step_keys():
        p.append(f"build_rm620.sh's exit 6 was accepted (exit {rc}, steps {sb.step_keys()})")
    if "name this account or computer" not in out:
        p.append("the script's reason is not shown")
    return p, out


def python_problems(sb):
    """the python3 a step finds on its PATH must be the virtual environment's: Python looks for pyvenv.cfg next to the
    folder of the path it was started by, without resolving links."""
    p = []
    steps = [s for s in sb.steps() if s[0] in ("nvk", "rm")]
    if not steps:
        return ["no nvk or rm step ran"]
    for key, _, env in steps:
        found = None
        for d in env.get("PATH", "").split(":"):
            c = pathlib.Path(d) / "python3"
            if os.path.isfile(c) and os.access(c, os.X_OK):
                found = c
                break
        if not found:
            p.append(f"{key}: no python3 on the step's PATH")
        elif not (found.parent.parent / "pyvenv.cfg").exists():
            p.append(f"{key}: python3 on PATH is {found}, which Python does not run as the environment (no pyvenv.cfg one level up)")
    return p


def pkgconfig_problems(sb):
    """a PKG_CONFIG_PATH from the tester's shell comes after build.sh's (pkg-config takes the first LLVMSPIRVLib.pc), and
    without the folders of the other architecture's Homebrew (this imitated Mac is an Intel one: /opt/homebrew)"""
    sb.clear()
    sb.run("--force", "build", "nvk", extra={"PKG_CONFIG_PATH": "/opt/homebrew/lib/pkgconfig:/usr/local/lib/pkgconfig"})
    envs = [e for k, _, e in sb.steps() if k == "nvk"]
    if not envs:
        return ["nvk did not run"]
    parts = envs[0].get("PKG_CONFIG_PATH", "").split(":")
    p = []
    if not os.path.isfile(os.path.join(parts[0], "LLVMSPIRVLib.pc")):
        p.append(f"the first PKG_CONFIG_PATH entry is not the chosen SPIRV-LLVM-Translator: {parts[:2]}")
    if "/usr/local/lib/pkgconfig" not in parts:
        p.append("the tester's PKG_CONFIG_PATH was dropped")
    if "/opt/homebrew/lib/pkgconfig" in parts:
        p.append(f"the other architecture's Homebrew reached the NVK step: {parts}")
    return p


def arm_steps(mac):
    with mac.scratch():
        p = pkgconfig_problems(mac)
        check("a PKG_CONFIG_PATH from the shell is kept after the translator build.sh chose, without the other Homebrew", not p, p)
    with mac.scratch():
        mac.break_script('    p="${p:+$p:}$d"', '    p="$d${p:+:$p}"')
        negative("the shell's PKG_CONFIG_PATH first", pkgconfig_problems(mac))
    with mac.scratch():
        mac.break_script('    [ -n "$d" ] && ! foreign_search_dir "$d" || continue', '    [ -n "$d" ] || continue')
        negative("the shell's PKG_CONFIG_PATH kept whole (the other architecture's Homebrew included)", pkgconfig_problems(mac))
    with mac.scratch():
        p, out = stale_problems(mac)
        check("a build script that fails stops its step, even when a previous build left an output behind", not p, (p, out[-700:]))
    with mac.scratch():
        mac.break_script('  [ "$rc" = 0 ] || fail_step "${CUR_STEP:-unknown}" "$rc"\n}', '  return "$rc"\n}')
        negative("a step that goes on after its build script failed", stale_problems(mac)[0])
    with mac.scratch():
        p, out = personal_problems(mac, "/Users/someone/src/nv.c\n")
        check("build_rm620.sh's exit 6 (a name as a whole word) stops the build", not p, (p, out[-900:]))
    with mac.scratch():
        mac.break_script('      [ "$m" = 0 ] || return "$m"\n      # build_rm620.sh archives', '      [ "$m" = 0 ] || [ "$m" = 6 ] || return "$m"\n      # build_rm620.sh archives')
        negative("a build that accepts build_rm620.sh's exit 6", personal_problems(mac, "/Users/someone/src/nv.c\n")[0])
    with mac.scratch():
        mac.clear()
        mac.run("--force", "build", "rm", "nvk")
        p = python_problems(mac)
        check("the python3 every step finds is the virtual environment's (mako, PyYAML for Mesa)", not p, p)
    with mac.scratch():
        mac.break_script('  STEP_PATH="$TBIN:$VENV/bin:', '  STEP_PATH="$TBIN:')
        mac.break_script('  # python3, meson and mako-render: from $VENV/bin',
                         '  ln -s "$VENV/bin/python3" "$TBIN/python3"\n  #')
        mac.clear()
        mac.run("--force", "build", "rm", "nvk")
        negative("python3 linked into tools/bin (the bare interpreter, not the environment)", python_problems(mac))
    with mac.scratch():
        p = nvk_release_problems(mac)
        check("the NVK step builds build_nvk.sh's current release (its default), and so does build_release_inputs.sh", not p, p)
    with mac.scratch():
        mac.break_script('"$O/nvk-build" 3.0', '"$O/nvk-build" 2.9')
        negative("build.sh that asks for release 2.9's NVK chain", nvk_release_problems(mac))


def nvk_release_problems(sb):
    """build_nvk.sh builds the chain of the release it is given; 2.9 is release 2.9's chain, without this branch's
    patches, and its BUILD.txt says "release 2.9". The library build.sh makes must be the current release's."""
    sb.clear()
    sb.run("--force", "build", "nvk")
    m = re.search(r"\bREL=\$\{4:-([\w.]+)\}", (sb.co / "build/build_nvk.sh").read_text())
    cur = m.group(1) if m else None
    args = [a for k, a, _ in sb.steps() if k == "nvk"]
    p = []
    if not cur:
        p.append("build_nvk.sh has no default release (REL=${4:-...})")
    if not args:
        p.append("no nvk step ran")
    elif len(args[-1]) != 4 or args[-1][3] != cur:
        p.append(f"build.sh runs build_nvk.sh with {args[-1][3:]}, not its current release {cur}")
    rel = (sb.co / "build/build_release_inputs.sh").read_text()
    if not re.search(r'build_nvk\.sh" "\$MESA" "\$OGKM610" "\$OUT/nvk-build" ' + re.escape(cur or "?") + r" \|\|", rel):
        p.append(f"build_release_inputs.sh does not ask build_nvk.sh for release {cur}")
    return p


def wait_for(path, timeout=300):
    """Until <path> exists, at most as long as one run may take (Sandbox.run): a step starts only after build.sh's own
    checks, which take many times longer under macOS's bash 3.2 on a Mac busy with other suites (run_all_tests -j 4)."""
    t0 = time.time()
    while not path.exists():
        if time.time() - t0 > timeout:
            return False
        time.sleep(0.05)
    return True


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def interrupt_problems(sb, wait=60, default_sigint=True, tool_exits=False):
    """tool_exits: the step's tool deals with the interrupt itself and ends with its own failure status."""
    (sb.ctl / "block-fb").write_text("")
    if tool_exits:
        (sb.ctl / "sigint-exits-fb").write_text("")
    sb.clear()
    pr = sb.popen("--force", "build", "fb", default_sigint=default_sigint)
    p = []
    try:
        if not wait_for(sb.ctl / "started-fb"):
            stop(pr)
            pr.communicate()
            return ["the fb step never started"], ""
        os.killpg(pr.pid, signal.SIGINT)
        try:
            out, _ = pr.communicate(timeout=wait)
        except subprocess.TimeoutExpired:
            stop(pr)
            pr.communicate()
            return ["build.sh did not stop after Ctrl-C"], ""
        pid = int((sb.ctl / "pid-fb").read_text())
        t0 = time.time()
        while alive(pid) and time.time() - t0 < 5:
            time.sleep(0.05)
        if alive(pid):
            p.append(f"the step's stand-in (pid {pid}) outlived the interrupted run")
    finally:
        # the stand-in waits while the block is there: never leave it for the arms that follow
        for f in ("block-fb", "started-fb", "pid-fb", "sigint-exits-fb"):
            (sb.ctl / f).unlink(missing_ok=True)
    if pr.returncode == 0:
        p.append("exit 0 after an interrupt")
    if tool_exits and "interrupted by user" not in (sb.work / "logs/fb.log").read_text():
        p.append("the stand-in did not end by itself (its own message is not in the step's log)")
    if "STOP: step" in out:
        p.append("the interrupt is reported as a failed step")
    if (sb.work / "stamps/fb").exists():
        p.append("the interrupted step has a stamp")
    if (sb.work / "build.lock").exists():
        p.append("the lock is still there")
    if "interrupted" not in out:
        p.append("no message that it was interrupted")
    return p, out


def arm_interrupt(mac):
    with mac.scratch():
        p, out = interrupt_problems(mac)
        check("Ctrl-C in a step: non-zero exit, no stamp for that step, lock released", not p, (p, out[-600:]))
        mac.clear()
        rc, out = mac.run("build", "fb")
        check("the next run builds the interrupted step again", rc == 0 and mac.step_keys() == ["fb"], (rc, mac.step_keys()))
    with mac.scratch():
        mac.break_script("release_lock() {\n", "release_lock() { return 0\n")
        negative("an interrupt that leaves the lock", interrupt_problems(mac)[0])
    # ninja and cargo catch the interrupt, stop their jobs and end with a failure status of their own: the shell that
    # waited then sees a child that ended normally, and only its trap tells it that the user pressed Ctrl-C
    with mac.scratch():
        p, out = interrupt_problems(mac, tool_exits=True)
        check("Ctrl-C in a step whose tool ends by itself with a failure status: still an interrupt, not a failed step", not p, (p, out[-600:]))
    with mac.scratch():
        mac.break_script("  trap on_signal INT TERM\n", "  trap on_signal TERM\n")
        negative("a run that takes such a tool's status for a failed step", interrupt_problems(mac, tool_exits=True)[0])
    # started from "nohup ... &" or a script's "&", this test and everything it starts inherit an ignored SIGINT, which
    # build.sh cannot trap: the Ctrl-C must still reach it, or the arm waits a minute and its step is left running
    old = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        with mac.scratch():
            p, out = interrupt_problems(mac)
            check("the same when this test itself was started with SIGINT ignored", not p, (p, out[-600:]))
        with mac.scratch():
            negative("a run that keeps the ignored SIGINT", interrupt_problems(mac, wait=5, default_sigint=False)[0])
    finally:
        signal.signal(signal.SIGINT, old)


def concurrent_problems(sb):
    (sb.ctl / "block-nmsmn").write_text("")
    sb.clear()
    a = sb.popen("--force", "build", "nmsmn")
    p = []
    try:
        if not wait_for(sb.ctl / "started-nmsmn"):
            stop(a)
            a.communicate()
            return ["the first run never started its step"]
        rc, out = sb.run("--force", "build", "airdis")
        (sb.ctl / "release-nmsmn").write_text("")
        try:
            a_out, _ = a.communicate(timeout=300)
        except subprocess.TimeoutExpired:
            stop(a)
            a.communicate()
            return ["the first run did not finish after its step was released"]
    finally:
        for f in ("block-nmsmn", "started-nmsmn", "release-nmsmn"):
            (sb.ctl / f).unlink(missing_ok=True)
    if rc == 0:
        p.append("the second run went ahead")
    if "another build.sh run (pid" not in out:
        p.append("the second run does not name the first")
    if "airdis" in sb.step_keys():
        p.append("the second run started a step")
    if a.returncode != 0:
        p.append(f"the first run failed: {a_out[-300:]}")
    return p


def arm_concurrent(mac):
    with mac.scratch():
        p = concurrent_problems(mac)
        check("two runs at once: the second stops at the lock and names the first; the first finishes", not p, p)
        lock = mac.work / "build.lock"
        lock.mkdir()
        dead = subprocess.Popen(["true"])
        dead.wait()
        (lock / "owner").write_text(f"pid={dead.pid}\nstarted=x\ncommand=build\n")
        rc, out = mac.run("build", "airdis")
        check("a lock left by a run that is gone is taken over with a note", rc == 0 and "taking over the lock" in out
              and not lock.exists(), (rc, out[-400:]))

    live = subprocess.Popen(["sleep", "300"])   # a process that is not build.sh
    try:
        def race_problems(s):
            lk = s.work / "build.lock"
            shutil.rmtree(lk, ignore_errors=True)
            lk.mkdir()
            (s.state / "lockdir").write_text(str(lk))
            (s.state / "livepid").write_text(str(live.pid))
            rc, out = s.run("build", "airdis")
            shutil.rmtree(lk, ignore_errors=True)
            return [] if rc == 2 and f"another build.sh run (pid {live.pid})" in out else [f"exit {rc}: {out[-300:]}"]

        def reuse_problems(s):
            lk = s.work / "build.lock"
            shutil.rmtree(lk, ignore_errors=True)
            lk.mkdir()
            (lk / "owner").write_text(f"pid={live.pid}\nstarted=x\ncommand=build\n")
            rc, out = s.run("build", "airdis")
            ok = rc == 0 and "taking over the lock" in out and not lk.exists()
            shutil.rmtree(lk, ignore_errors=True)
            return [] if ok else [f"exit {rc}: {out[-300:]}"]

        def ownerless_problems(s):
            lk = s.work / "build.lock"
            lk.mkdir()
            rc, out = s.run("build", "airdis")
            ok = rc == 0 and "taking over the lock" in out and not lk.exists()
            shutil.rmtree(lk, ignore_errors=True)
            return [] if ok else [f"exit {rc}: {out[-300:]}"]
        with mac.scratch():
            p = ownerless_problems(mac)
            check("a lock without an owner that stays so (a run stopped before it wrote one) is taken over after a wait", not p, p)
            mac.break_script('2>/dev/null | first_line || true)', '2>/dev/null | first_line)')
            negative("reading the owner of a lock that has none under errexit (build.sh stopped there)", ownerless_problems(mac))
        with mac.scratch():
            mac.wrap(mac.bin / "sleep", "sleepowner")
            p = race_problems(mac)
            check("a lock whose owner appears a moment after (two runs started together): the second waits and stops", not p, p)
            mac.break_script('    if [ -z "$pid" ] && [ "$waited" -lt 3 ]; then waited=$((waited + 1)); sleep 1; continue; fi\n', "")
            negative("a lock without an owner taken over at once", race_problems(mac))
        if shutil.which("ps"):
            with mac.scratch():
                os.symlink(shutil.which("ps"), mac.bin / "ps")
                p = reuse_problems(mac)
                check("a lock whose pid now belongs to another program (after a crash or a restart) is taken over", not p, p)
                mac.break_script('  case "$(ps -p "$1" -o command= 2>/dev/null || true)" in *build.sh*) return 0 ;; esac\n  return 1\n', "")
                negative("a lock kept by any live process with its pid", reuse_problems(mac))
    finally:
        live.kill()
        live.wait()
    with mac.scratch():
        mac.break_script('while ! mkdir "$l" 2>/dev/null; do', 'while ! mkdir -p "$l" 2>/dev/null; do')
        mac.run("build")
        negative("a lock that does not exclude", concurrent_problems(mac))


def space_problems(sb, args, extra=None, created=None):
    sb.clear()
    rc, out = sb.run(*args, extra=extra)
    p = []
    if rc != 2:
        p.append(f"exit {rc}, not 2")
    if "space" not in out:
        p.append("the message does not say why (space)")
    if created and os.path.exists(created):
        p.append(f"{created} was created")
    if sb.step_keys() or mutating(sb.jlines()):
        p.append("something ran")
    return p, out


def arm_space():
    sb = Sandbox("space")
    wd = str(sb.home / "my build")
    p, out = space_problems(sb, ["--workdir", wd, "fetch"], created=wd)
    check("a working directory with a space is refused at the start, nothing created", not p, (p, out[-400:]))
    p, out = space_problems(sb, ["--dry-run", "build"], extra={"NM_WORK": wd})
    check("the same through NM_WORK, also for a dry run", not p, (p, out[-400:]))
    rc, out = sb.run("--workdir", wd, "doctor")
    check("doctor reports it as REFUSED and exits 1", rc == 1 and "REFUSED" in out and "space" in out, out[-400:])
    sb2 = Sandbox("space-co", checkout_dir="my src/nvidia-macos-driver")
    p, out = space_problems(sb2, ["fetch"], created=str(sb2.work))
    check("a checkout path with a space is refused at the start", not p and "checkout path" in out, (p, out[-400:]))
    nl = str(sb.home / "my\nbuild")
    sb.clear()
    rc, out = sb.run("--workdir", nl, "-y", "clean", "all")
    check("a working directory with a line break is refused at the start, nothing created",
          rc == 2 and "line break" in out and not os.path.exists(nl), (rc, out[-300:]))
    for w in ("/", str(sb.home), str(sb.home / "nvmtl-build")):
        sb.clear()
        rc, out = sb.run("--workdir", w, "-y", "clean", "all")
        check(f"--workdir {w.replace(str(sb.home), '~')} is refused with the reason, nothing run",
              rc == 2 and "may not be /" in out and not mutating(sb.jlines()), (rc, out[-300:]))
    fresh = str(sb.home / "never-made")
    rc, out = sb.run("--workdir", fresh, "clean")
    check("clean on a working directory that does not exist creates nothing", rc == 0 and not os.path.exists(fresh), (rc, out[-300:]))
    with sb.scratch():
        sb.break_script('*"$nl"*) path_problem', '*"@@@"*) path_problem')
        sb.clear()
        rc, out = sb.run("--workdir", nl, "-y", "clean", "all")
        negative("a working directory with a line break accepted", [] if rc == 2 and "line break" in out else ["accepted"])
    sb.break_script('  case "$NM_WORK" in *[$ws]*) path_problem', '  case "$NM_WORK" in *[@]*) path_problem')
    negative("a working directory with a space accepted", space_problems(sb, ["--dry-run", "build"], extra={"NM_WORK": wd})[0])


def arm_deps_ok(sb, rc):
    j = sb.jlines()
    return rc == 0 and any("aarch64-apple-darwin/rustup-init" in l for l in j if l.startswith("curl ")) and \
        (sb.work / "tools/rustup/toolchains/1.98.1-aarch64-apple-darwin/bin/rustc").exists()


def arm_hosts(commits):
    sb = Sandbox("arm", arch="arm64", formulas=DEPS_WANT + ["cmake"])
    with sb.scratch():
        sb.break_script("arm64) HOST_TRIPLE=aarch64-apple-darwin", "arm64) HOST_TRIPLE=x86_64-apple-darwin")
        sb.clear()
        rc, out = sb.run("deps", "-y")
        negative("an Apple-silicon Mac taken for an Intel one", [] if arm_deps_ok(sb, rc) else ["no aarch64 rustup"])
    rc, out = sb.run("doctor")
    check("Apple silicon: doctor says where the tools come from and that no whole build has run there",
          "Apple silicon: by default deps takes the missing tools from Homebrew here. No whole build with build.sh has run on an "
          "Apple-silicon Mac yet" in out, out[:600])
    sb.clear()
    rc, out = sb.run("deps", "-y")
    check("Apple silicon: deps installs the aarch64 rustup and toolchain", arm_deps_ok(sb, rc), (rc, out[-500:]))
    sb.run("fetch")
    sb.clear()
    rc, out = sb.run("build", "xlate")
    st = {k: e for k, _, e in sb.steps()}
    check("Apple silicon: the translator build gets the aarch64-hosted 1.98.1 toolchain (it cross-builds x86_64)",
          rc == 0 and st.get("xlate", {}).get("NULLMOTH_RUST_BIN", "").endswith("toolchains/1.98.1-aarch64-apple-darwin/bin"),
          (rc, st.get("xlate", {}).get("NULLMOTH_RUST_BIN"), out[-400:]))

    def old_problems(s):
        p = []
        rc, out = s.run("doctor")
        if rc != 1 or "macOS 14.7.1" not in out or "15.6" not in out:
            p.append(f"doctor: exit {rc}, {out[:300]}")
        for args in (["build"], ["deps", "-y"], ["all"]):
            s.clear()
            rc, out = s.run(*args)
            if rc != 2 or "macOS 14.7.1" not in out:
                p.append(f"{args}: exit {rc}: {out[-200:]}")
            if s.step_keys() or mutating(s.jlines()):
                p.append(f"{args}: something ran")
        return p

    old = Sandbox("old", macos="14.7.1")
    p = old_problems(old)
    check("macOS older than 15.6 (Xcode 26 cannot run): doctor exits 1, build, deps and all stop at the start with the reason", not p, p)
    old.break_script('  if mac_too_old; then', '  if false; then')
    negative("an old macOS accepted", old_problems(old))
    lx = Sandbox("linux")
    (lx.state / "os").write_text("Linux\n")
    rc, out = lx.run("build")
    check("not a Mac: build stops at the start and says what works there", rc == 2 and "building needs a Mac" in out, out[-300:])
    lx.break_script('is_mac || [ "$NM_DRY_RUN" = 1 ] || die "building needs a Mac', 'true || die "building needs a Mac')
    rc, out = lx.run("build")
    negative("a build that goes ahead on another system", [] if rc == 2 and "building needs a Mac" in out else ["went ahead"])


ENV_COMPARE = {"fb": ["OGKM", "NULLMOTH_BUILD_ROOT", "MACOSX_DEPLOYMENT_TARGET", "JOBS", "PYTHONDONTWRITEBYTECODE"],
               "nvrm": ["NVRM_PRISTINE_OGKM"], "rm": ["NV_VERSION"],
               "nvk": ["NVK_PATH", "PKG_CONFIG_PATH", "LIBCLANG_PATH", "NVK_JOBS", "RUSTUP_HOME", "CARGO_HOME", "RUSTUP_TOOLCHAIN"],
               "xlate": ["NULLMOTH_RUST_VERSION", "NULLMOTH_RUST_BIN", "NULLMOTH_CARGO_HOME", "NULLMOTH_BUILD_HOST", "PATH"],
               "vulkan": ["NM_KHRONOS_URL"], "nvencvt": ["NM_OGKM620", "NM_OGKM620_PRISTINE"]}


def env_problems(sb, steps):
    rc, out = sb.run("env")
    p = []
    if rc != 0:
        return [f"env: exit {rc}"]
    f = sb.dir / "env.out"
    f.write_text(out)
    names = sorted(set(re.findall(r"^export ([A-Za-z_][A-Za-z0-9_]*)=", out, re.M)))
    script = "set -eu\n. " + str(f) + "\n" + "".join(f'printf "%s=%s\\n" {n} "${n}"\n' for n in names)
    r = subprocess.run([BASH, "-c", script], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "HOME": str(sb.home)})
    if r.returncode:
        return [f"bash cannot evaluate the env output: {r.stderr[-300:]}"]
    got = dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)
    for key, vs in ENV_COMPARE.items():
        env = steps.get(key)
        if env is None:
            p.append(f"no {key} step to compare with")
            continue
        for v in vs:
            if got.get(v) != env.get(v):
                p.append(f"{v}: env prints {got.get(v)!r}, the {key} step got {env.get(v)!r}")
    return p


def arm_env(mac):
    steps = {k: e for k, _, e in mac.steps()}
    p = env_problems(mac, steps)
    check("env prints export lines bash can evaluate, with the values the steps receive", not p, p)
    with mac.scratch():
        mac.break_script('say "export OGKM=$(qh "$OGKM620")"', 'say "export OGKM=$(qh "$PRISTINE")"')
        negative("env that prints another OGKM than the build uses", env_problems(mac, steps))


def nohome_problems(sb):
    """what a tester pastes names no home folder (it contains the account name): doctor, dry runs, env, progress, the
    summary, a STOP line and the report. Run with HOME spelled through a link, so that both spellings occur."""
    link = sb.dir / "homelink"
    if not link.exists():
        os.symlink(sb.home, link)
    spellings = {str(sb.home), os.path.realpath(sb.home), str(link)}
    p = []
    runs = [("doctor",), ("--dry-run", "all"), ("-n", "--force", "build", "fb"), ("-n", "deps"), ("env",), ("build",), ("--force", "build", "agdc")]
    (sb.ctl / "fail-agdc").write_text("")
    try:
        for args in runs:
            sb.clear()
            rc, out = sb.run(*args, home=link)
            hits = [l for l in out.splitlines() if any(h in l for h in spellings)]
            if hits:
                p.append(f"{' '.join(args)} prints the home folder: {hits[:2]}")
            if args == ("env",):
                if not re.search(r'^export NM_WORK="\$HOME/nullmoth-build"$', out, re.M):
                    p.append(f"env does not write the working directory as \"$HOME/...\": {[l for l in out.splitlines() if 'NM_WORK=' in l]}")
            if args == ("--force", "build", "agdc"):
                reps = sorted((sb.work / "reports").glob("failure-agdc-*.txt"))
                t = reps[-1].read_text() if reps else ""
                if not t or any(h in t for h in spellings):
                    p.append("the failure report is missing or names the home folder")
    finally:
        (sb.ctl / "fail-agdc").unlink()
    return p


def arm_nohome(mac):
    with mac.scratch():
        p = nohome_problems(mac)
        check("doctor, dry runs, env, progress, summary, STOP lines and the report write the home folder as ~ (env: \"$HOME/...\")", not p, p)
    with mac.scratch():
        mac.break_script("""say()  { if has_home "$*"; then printf '%s\\n' "$*" | tilde; else printf '%s\\n' "$*"; fi; }""",
                         """say()  { printf '%s\\n' "$*"; }""")
        negative("output with the home folder written out (only the summary and the report used ~)", nohome_problems(mac))


def arm_test_cmd(mac):
    def problems(s):
        (s.ctl / "tests-fail").write_text("")
        s.clear()
        rc, out = s.run("test")
        (s.ctl / "tests-fail").unlink()
        return [] if rc != 0 and "1 failed" in out else [f"exit {rc} with a failing test"]
    rc, out = mac.run("test")
    check("test runs the pin check and the host tests with the venv's Python", rc == 0 and "pin ok" in out and
          any("tools/run_all_tests.py" in l and l.startswith("python3") for l in mac.jlines()), (rc, out[-400:]))
    check("test exits non-zero when a host test fails", not problems(mac), out[-300:])
    with mac.scratch():
        mac.break_script('  case "$last" in *" 0 failed,"*) ;; *) rc=1 ;; esac\n', "")
        mac.break_script('| tee "$CUR_LOG" || rc=$?', '| tee "$CUR_LOG" || true')
        negative("test that ignores a failing host test", problems(mac))

    # the plugin's tests get Vulkan's headers: NM_VULKAN_INCLUDE as the tester set it, else the fetched Mesa's include/
    # (without NM_MESA_GIT, the folder the release plugin is compiled against), else none, and one line says which
    mesa_inc = str(mac.work / "build/gpu-src/mesa/include")
    shown = "~/" + os.path.relpath(mesa_inc, mac.home)   # build.sh writes a path in the home folder with ~

    def vk_problems(s, want, says, extra=None):
        s.clear()
        rc, out = s.run("test", extra=extra)
        got = [l for l in s.jlines() if l.startswith("tests-env ")]
        q = []
        if got != [f"tests-env NM_VULKAN_INCLUDE={want}"]:
            q.append(f"the tests got {got}, expected NM_VULKAN_INCLUDE={want}")
        if says not in out:
            q.append(f"the output lacks: {says}")
        return q
    p = vk_problems(mac, mesa_inc, f"Vulkan headers for the tests: {shown} (the fetched Mesa's")
    check("test gives the host tests the fetched Mesa's include/ as NM_VULKAN_INCLUDE and says so", not p, p)
    p = vk_problems(mac, "/somewhere/include", "Vulkan headers for the tests: /somewhere/include (NM_VULKAN_INCLUDE)",
                    extra={"NM_VULKAN_INCLUDE": "/somewhere/include"})
    check("test passes a NM_VULKAN_INCLUDE the tester set unchanged", not p, p)
    with mac.scratch():
        (mac.work / "build/gpu-src/mesa/include/vulkan/vulkan.h").unlink()
        p = vk_problems(mac, "UNSET", "./build.sh fetch brings Mesa's")
        check("test without fetched headers sets nothing and says that ./build.sh fetch brings them", not p, p)
    with mac.scratch():
        mac.break_script("    export NM_VULKAN_INCLUDE=$MESA/include\n", "    NM_VULKAN_INCLUDE=$MESA/include\n")
        negative("test that does not export the Mesa headers to the tests",
                 vk_problems(mac, mesa_inc, f"Vulkan headers for the tests: {shown} (the fetched Mesa's"))

    # the patch chain is applied the way build_rm620.sh and build_nvrm.sh apply it, through tools/bin/patch; macOS's
    # patch would wait at the terminal for an answer about every patch whose file is not in the first folder tried
    def asked_problems(s):
        s.clear()
        rc, out = s.run("test")
        p = []
        asked = [l for l in s.jlines() if l.startswith("patch-asked")]
        if asked:
            p.append(f"patch was called without -f and would have waited for an answer: {asked[:2]}")
        calls = [l for l in s.jlines() if l.startswith("patch ") and "--version" not in l and "--dry-run" in l]
        if not calls:
            p.append("the patch chain was not applied")
        if rc != 0 or "patch chain: every patch applies" not in out:
            p.append(f"exit {rc}: {out[-300:]}")
        return p
    with mac.scratch():
        mac.wrap(mac.bin / "patch", "patchask", "patch 2.0-12u11-Apple")
        p = asked_problems(mac)
        check("test applies the patch chain through tools/bin/patch, which never waits for an answer", not p, p)
        mac.break_script('exec %s -f "$@"', 'exec %s "$@"')
        negative("tools/bin/patch without -f (macOS's patch asks at the terminal)", asked_problems(mac))


def arm_package(mac):
    def problems(s):
        p = []
        s.clear()
        rc, out = s.run("--accel", "15", "--base-package", str(s.dir), "package")
        if rc == 0 or "package" in s.step_keys():
            p.append("a package without both NVAccel builds was staged")
        return p
    rc, out = mac.run("package")
    check("package without a base package stops and says why", rc == 2 and "base" in out and "nullmoth-runtime-check" in out, out[-300:])
    check("package with only one NVAccel flavour is refused", not problems(mac))
    with mac.scratch():
        mac.clear()
        rc, out = mac.run("--base-package", str(mac.dir / "brew"), "package")
        st = [(k, a) for k, a, _ in mac.steps()]
        ok = rc == 0 and st and st[-1][0] == "package" and st[-1][1][1:] == [str(mac.work / "out"), str(mac.work / "package")]
        ok = ok and "sudo env CHECK=1 ./install.sh" in out and not any(l.startswith("sudo") for l in mac.jlines())
        check("package stages from a base package and prints the install command without running it", ok, (rc, st[-1:], out[-600:]))
    with mac.scratch():
        mac.break_script('[ "$NM_ACCEL" = both ] || die', 'true || die')
        negative("a package with one NVAccel flavour", problems(mac))
    top = re.search(r"(?m)^## ([0-9]+(?:\.[0-9]+)*)\s*$", (mac.co / "CHANGELOG.md").read_text())
    top = top.group(1) if top else "?"
    with mac.scratch():
        p, out = package_version_problems(mac, top, "CHANGELOG.md")
        check(f"a package records the release this checkout is ({top}, CHANGELOG.md's first heading), not its base's", not p, (p, out[-500:]))
    with mac.scratch():
        p, out = package_version_problems(mac, "2.9.7", "NM_PACKAGE_VERSION", extra={"NM_PACKAGE_VERSION": "2.9.7"})
        check("a version the user sets wins", not p, (p, out[-500:]))
    with mac.scratch():
        p, out = unversioned_changelog_problems(mac)
        check("a CHANGELOG.md whose first heading is not a version stops the package step with a sentence; no base version is taken",
              not p, (p, out[-500:]))
    def odd_version_problems(s):
        s.clear()
        rc, out = s.run("--base-package", str(s.dir / "brew"), "package", extra={"NM_PACKAGE_VERSION": "3.0rc1"})
        q = []
        if rc == 0 or "package" in s.step_keys():
            q.append(f"the package step ran (exit {rc}) with a version install.sh would record as 3.01")
        if "would record '3.01'" not in out:
            q.append("the stop does not say what would be recorded")
        return q, out
    with mac.scratch():
        p, out = odd_version_problems(mac)
        check("a version the user sets that is not digits and dots stops the package step (install.sh would record 3.0rc1 as 3.01)",
              not p, (p, out[-500:]))
    with mac.scratch():
        mac.break_script("    *[!0-9.]*|.*|*.|*..*) die", "    *[!0-9.]*|.*|*.|*..*) true")
        negative("a package step that takes any version the user sets", odd_version_problems(mac)[0])
    with mac.scratch():
        mac.break_script('then NM_PACKAGE_VERSION=$(changelog_version); PKG_VERSION_FROM="CHANGELOG.md"; fi', 'then :; fi')
        negative("build.sh that leaves the package's VERSION to the base package", package_version_problems(mac, top, "CHANGELOG.md")[0])
    with mac.scratch():
        mac.break_script('  [ -n "$NM_PACKAGE_VERSION" ] || die "the package\'s version', '  [ -n "$NM_PACKAGE_VERSION" ] || true "the package\'s version')
        negative("a package step that goes on without a version", unversioned_changelog_problems(mac)[0])


def package_version_problems(s, want, source, extra=None):
    """./build.sh package: the version its package step is given, and where build.sh says it came from."""
    s.clear()
    rc, out = s.run("--base-package", str(s.dir / "brew"), "package", extra=extra)
    st = [e for k, _, e in s.steps() if k == "package"]
    p = []
    if rc != 0 or not st:
        p.append(f"the package step did not run (exit {rc})")
    elif st[-1].get("NM_PACKAGE_VERSION") != want:
        p.append(f"package_local.sh got NM_PACKAGE_VERSION={st[-1].get('NM_PACKAGE_VERSION')!r}, not {want}")
    if f"package version {want} (from {source})" not in out:
        p.append(f"build.sh does not say that the package version is {want}, from {source}")
    return p, out


def unversioned_changelog_problems(s):
    c = s.co / "CHANGELOG.md"
    c.write_text(c.read_text().replace("\n## ", "\n## Unreleased\n\n## ", 1))
    s.clear()
    rc, out = s.run("--base-package", str(s.dir / "brew"), "package")
    p = []
    if rc == 0 or "package" in s.step_keys():
        p.append(f"the package step ran (exit {rc}) without a version to record")
    if "set NM_PACKAGE_VERSION" not in out:
        p.append("the stop does not say how to go on")
    return p, out


def arm_clean(mac):
    def outside_problems(s):
        keep = s.dir / "elsewhere/build/rm620"
        keep.mkdir(parents=True, exist_ok=True)
        (keep / "x").write_text("x")
        s.run("clean", extra={"NULLMOTH_BUILD_ROOT": str(s.dir / "elsewhere")})
        return [] if (keep / "x").exists() else ["a folder outside the working directory was deleted"]
    with mac.scratch():
        rc, out = mac.run("clean")
        w = mac.work
        ok = rc == 0 and not (w / "out").exists() and not (w / "stamps").exists() and (w / "src").exists() and (w / "tools/venv").exists()
        check("clean removes outputs, stamps and logs and keeps sources and tools", ok, (rc, out[-300:]))
        rc, out = mac.run("clean", "all")
        check("clean all without --yes and without a terminal deletes nothing", "nothing deleted" in out and (w / "src").exists(), out[-300:])
        rc, out = mac.run("-y", "clean", "all")
        left = sorted(os.listdir(w))
        check("clean all --yes leaves only the marker", rc == 0 and left == [".nullmoth-build-workdir"], left)
        p = outside_problems(mac)
        check("clean never deletes outside the working directory (NULLMOTH_BUILD_ROOT elsewhere)", not p, p)

        def old_layout_problems(s, extra=None):
            old = s.work / "build/build/ogkm620/x"
            old.parent.mkdir(parents=True, exist_ok=True)
            old.write_text("x")
            rc, out = s.run("clean", extra=extra)
            gone = not (s.work / "build/build").exists()
            if extra:   # a NULLMOTH_BUILD_ROOT the tester set: their folder, kept
                return [] if not gone and rc == 0 else [f"clean deleted <work>/build/build although NULLMOTH_BUILD_ROOT names it (exit {rc})"]
            return [] if gone and rc == 0 else [f"clean left the old <work>/build/build (exit {rc}): {out[-200:]}"]
        p = old_layout_problems(mac)
        check("clean deletes the <work>/build/build an earlier build.sh left", not p, p)
        p = old_layout_problems(mac, {"NULLMOTH_BUILD_ROOT": str(mac.work / "build")})
        check("clean keeps <work>/build/build when NULLMOTH_BUILD_ROOT is <work>/build", not p, p)
        # the scratch above puts build.sh back when it ends
        mac.break_script("             old_layout_rm ;;\n    sources)", "             ;;\n    sources)")   # outputs without old_layout_rm
        negative("clean that leaves the old <work>/build/build", old_layout_problems(mac))
        other = mac.home / "notours"
        other.mkdir()
        (other / "keep").write_text("x")
        rc, out = mac.run("--workdir", str(other), "clean")
        check("clean refuses a folder build.sh did not make", rc != 0 and (other / "keep").exists() and ".nullmoth-build-workdir" in out, out[-300:])
    with mac.scratch():
        mac.break_script('    in_workdir "$p" || die "refusing to delete $p', '    true || die "refusing to delete $p')
        negative("clean that deletes outside the working directory", outside_problems(mac))


# ------------------------------------------------------------------------------------------------- pins against their consumers
def read_pins(text):
    return {m.group(1): m.group(2).strip('"') for m in re.finditer(r"^(NM_PIN_[A-Z0-9_]+)=(.*)$", text, re.M)}


def pin_expectations(pins):
    """(pin, file, text the file must contain): every place in the repository that states the same number."""
    p = pins
    c8 = p["NM_PIN_MESA_COMMIT"][:8]
    wk = f"https://raw.githubusercontent.com/WebKit/WebKit/{p['NM_PIN_WEBKIT_COMMIT']}/Source/ThirdParty/libwebrtc/Source/webrtc/webkit_sdk/WebKit/"
    return [
        ("NM_PIN_OGKM620_REF", "build/build_rm620.sh", "V=${NV_VERSION:-%s}" % p["NM_PIN_OGKM620_REF"]),
        ("NM_PIN_OGKM620_REF", "build/build_kms620.sh", "V=${NV_VERSION:-%s}" % p["NM_PIN_OGKM620_REF"]),
        ("NM_PIN_OGKM620_REF", "build/build_nvrm.sh", "NVRM_PRISTINE_OGKM=<pristine %s tree>" % p["NM_PIN_OGKM620_REF"]),
        ("NM_PIN_OGKM620_REF", "tools/test_patch_chain.sh", "open-gpu-kernel-modules %s tree" % p["NM_PIN_OGKM620_REF"]),
        ("NM_PIN_OGKM610_REF", "build/build_nvk.sh", "<open-gpu-kernel-modules %s tree>" % p["NM_PIN_OGKM610_REF"]),
        ("NM_PIN_OGKM610_REF", "build/build_nvencvt.sh", "open-gpu-kernel-modules.git %s " % p["NM_PIN_OGKM610_REF"]),
        ("NM_PIN_OGKM610_REF", "vendor/nvenc/SOURCE", "open-gpu-kernel-modules %s" % p["NM_PIN_OGKM610_REF"]),
        ("NM_PIN_OGKM620_URL", "build/build_nvencvt.sh", p["NM_PIN_OGKM620_URL"]),
        ("NM_PIN_MESA_COMMIT", "nvk/MESA_BASE", c8),
        ("NM_PIN_MESA_COMMIT", "build/build_nvk.sh", f"2.9: {c8}, {p['NM_PIN_MESA_REF']}"),
        ("NM_PIN_MESA_COMMIT", "nvk/nvk-mesa-26.3.0.patch", f"{p['NM_PIN_MESA_REF'].replace('mesa-', '')} (nvk/MESA_BASE, {c8})"),
        ("NM_PIN_VKLOADER_REF", "build/build_vulkan_loader.sh", "TAG=" + p["NM_PIN_VKLOADER_REF"]),
        ("NM_PIN_VKHEADERS_REF", "build/build_vulkan_loader.sh", "TAG=" + p["NM_PIN_VKHEADERS_REF"]),
        ("NM_PIN_VKLOADER_COMMIT", "build/build_vulkan_loader.sh", "LOADER_SHA=" + p["NM_PIN_VKLOADER_COMMIT"]),
        ("NM_PIN_VKHEADERS_COMMIT", "build/build_vulkan_loader.sh", "HEADERS_SHA=" + p["NM_PIN_VKHEADERS_COMMIT"]),
        ("NM_PIN_VKLOADER_URL", "build/build_vulkan_loader.sh", p["NM_PIN_VKLOADER_URL"].rsplit("/", 1)[0]),
        ("NM_PIN_OPENGPUDOC_COMMIT", "build/build_nvencvt.sh", f"{p['NM_PIN_OPENGPUDOC_URL']} {p['NM_PIN_OPENGPUDOC_COMMIT']} "),
        ("NM_PIN_OPENGPUDOC_COMMIT", "vendor/nvenc/SOURCE", "open-gpu-doc " + p["NM_PIN_OPENGPUDOC_COMMIT"]),
        ("NM_PIN_WEBKIT_COMMIT", "build/build_nvencvt.sh", wk),
        ("NM_PIN_WEBKIT_COMMIT", "vendor/nvenc/SOURCE", "at " + p["NM_PIN_WEBKIT_COMMIT"]),
        ("NM_PIN_RYZENADJ_REF", "tools/ryzenadj-macos/build.sh", "TAG=" + p["NM_PIN_RYZENADJ_REF"]),
        ("NM_PIN_RYZENADJ_COMMIT", "tools/ryzenadj-macos/build.sh", "COMMIT=" + p["NM_PIN_RYZENADJ_COMMIT"]),
        ("NM_PIN_RUST_VERSION", "build/build_xlate_release.sh", "RV=${NULLMOTH_RUST_VERSION:-%s}" % p["NM_PIN_RUST_VERSION"]),
        ("NM_PIN_TOOL_LLVM_MAJOR", "build/build_nvk.sh", "(LLVM %s.1.x)" % p["NM_PIN_TOOL_LLVM_MAJOR"]),
        ("NM_PIN_BASE_RELEASE", "build/package_local.sh", "\nBASE_RELEASE=%s " % p["NM_PIN_BASE_RELEASE"]),
    ] + [
        # the version build/toolchain.sh compares each tool's --version with: the pinned tag without its prefix
        (f"NM_PIN_TC{pid}_REF", "build/toolchain.sh", f"    {tool}) echo {tc_tag_version(p[f'NM_PIN_TC{pid}_REF'])} ;;")
        for tool, pid in (("ninja", "NINJA"), ("cmake", "CMAKE"), ("python", "PYTHON"), ("pkgconf", "PKGCONF"),
                          ("glslang", "GLSLANG"), ("llvm", "LLVM"))
    ]


def tc_tag_version(ref):
    """v1.13.2, pkgconf-2.5.1, llvmorg-22.1.8, 16.6.0 -> the version alone (only these prefixes: another is kept, and
    then compared, whole)"""
    for pre in ("v", "pkgconf-", "llvmorg-"):
        if ref.startswith(pre):
            return ref[len(pre):]
    return ref


def pin_problems(pins, read):
    p = []
    for name, rel, text in pin_expectations(pins):
        if text not in read(rel):
            p.append(f"{name}={pins[name]}: {rel} does not say {text!r}")
    # the base package of the automatic full build is the pinned release's own tarball
    url = pins.get("NM_PIN_CI_BASEPKG_URL")
    rel = pins["NM_PIN_BASE_RELEASE"]
    if url is not None and not url.endswith(f"/v{rel}/nullmoth-nvidia-{rel}.tar.gz"):
        p.append(f"NM_PIN_CI_BASEPKG_URL={url} is not the tarball of release {rel} (NM_PIN_BASE_RELEASE)")
    for key in ("WEBKIT_CMBASE", "WEBKIT_VTDEC"):
        url = pins[f"NM_PIN_{key}_URL"]
        if not url.startswith(f"https://raw.githubusercontent.com/WebKit/WebKit/{pins['NM_PIN_WEBKIT_COMMIT']}/"):
            p.append(f"NM_PIN_{key}_URL does not name the pinned WebKit commit")
    for pid in pins["NM_PIN_SOURCES"].split():
        for part in ("NAME", "URL", "MIRROR"):
            if f"NM_PIN_{pid}_{part}" not in pins:
                p.append(f"source {pid} has no {part}")
        if not (f"NM_PIN_{pid}_COMMIT" in pins or f"NM_PIN_{pid}_SHA256" in pins):
            p.append(f"source {pid} has neither a commit nor a SHA-256 to verify")
        v = pins.get(f"NM_PIN_{pid}_COMMIT") or pins.get(f"NM_PIN_{pid}_SHA256")
        if v and not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", v):
            p.append(f"source {pid}: {v} is not a full commit id or SHA-256")
    return p


def arm_pins():
    text = (ROOT / "build/pins.sh").read_text()
    pins = read_pins(text)
    cache = {}

    def read(rel):
        if rel not in cache:
            cache[rel] = (ROOT / rel).read_text(errors="replace")
        return cache[rel]
    p = pin_problems(pins, read)
    check("every pin in build/pins.sh equals what the scripts, the patch chain and vendor/nvenc/SOURCE that use it state", not p, p)
    caught = 0
    exps = pin_expectations(pins)
    for name in sorted({e[0] for e in exps}):
        bad = dict(pins)
        v = bad[name]
        i = min(3, len(v) - 1)
        bad[name] = v[:i] + ("x" if v[i] != "x" else "y") + v[i + 1:]
        if pin_problems(bad, read):
            caught += 1
        else:
            check(f"control: a changed {name} is caught", False)
    check(f"control: each of the {len({e[0] for e in exps})} compared pins, changed by one character, is caught",
          caught == len({e[0] for e in exps}))
    bad = dict(cache)
    bad["build/build_xlate_release.sh"] = read("build/build_xlate_release.sh").replace(
        "RV=${NULLMOTH_RUST_VERSION:-%s}" % pins["NM_PIN_RUST_VERSION"], "RV=${NULLMOTH_RUST_VERSION:-1.99.0}")
    negative("a consuming script that moved to another Rust", pin_problems(pins, lambda r: bad.get(r) or read(r)))
    if "NM_PIN_CI_BASEPKG_URL" in pins:
        moved = dict(pins, NM_PIN_CI_BASEPKG_URL=pins["NM_PIN_CI_BASEPKG_URL"].replace("2.9", "2.8"))
        negative("a full build's base package of another release than the pinned one", pin_problems(moved, read))
    # build.sh's own sentences that ask for a base package name the pinned release's tarball, from the pin
    def base_name_problems(bs):
        names = [l for l in bs.splitlines() if "nullmoth-nvidia-" in l and "nullmoth-nvidia-local" not in l]
        q = [f"{l.strip()[:100]}" for l in names if "nullmoth-nvidia-$NM_PIN_BASE_RELEASE.tar.gz" not in l]
        return q + ([] if len(names) == 3 else [f"{len(names)} places, expected 3"])
    q = base_name_problems(read("build.sh"))
    check("build.sh names the base package as nullmoth-nvidia-$NM_PIN_BASE_RELEASE.tar.gz wherever it names one (3 places)", not q, q)
    negative("a build.sh that asks for nullmoth-nvidia-<version>.tar.gz",
             base_name_problems(read("build.sh").replace("~/Downloads/nullmoth-nvidia-$NM_PIN_BASE_RELEASE.tar.gz", "~/Downloads/nullmoth-nvidia-<version>.tar.gz")))


def arm_pins_script():
    """build.sh's own comparison (doctor) reports a pin that its consumer contradicts."""
    sb = Sandbox("pins")
    rc, out = sb.run("doctor")
    check("doctor compares the pins and finds them in agreement", "pin FAIL" not in out and out.count("pin ok") >= 13, out[:1500])
    p = sb.co / "build/build_vulkan_loader.sh"
    t = p.read_text()
    p.write_text(re.sub(r"LOADER_SHA=[0-9a-f]{40}", "LOADER_SHA=" + "0" * 40, t))
    rc, out = sb.run("doctor")
    rc2, out2 = sb.run("--dry-run", "build", "vulkan")
    check("a script that disagrees with its pin: doctor says pin FAIL and exits 1, build stops before any step",
          rc == 1 and "pin FAIL  Vulkan-Loader commit" in out and rc2 != 0 and "disagree" in out2, (rc, rc2, out2[-300:]))
    p.write_text(t)
    sb.break_script('  pin_agree "Vulkan-Loader commit"', '  : pin_agree "Vulkan-Loader commit"')
    p.write_text(re.sub(r"LOADER_SHA=[0-9a-f]{40}", "LOADER_SHA=" + "0" * 40, t))
    rc, out = sb.run("doctor")
    negative("a pin check that skips the loader commit", ["doctor did not say pin FAIL"] if "pin FAIL" not in out else [])


# ------------------------------------------------------------------------------------------------- static scan
# (pattern, what, a line that must be caught)
SCAN = [
    (r"\b(declare|local|typeset)\s+-[a-zA-Z]*A", "associative arrays (bash 4)", "declare -A map"),
    (r"\b(declare|local|typeset)\s+-[a-zA-Z]*n\b", "name references (bash 4.3)", "local -n ref=x"),
    (r"\b(declare|typeset)\s+-[a-zA-Z]*g", "declare -g (bash 4.2)", "declare -g X=1"),
    (r"\b(mapfile|readarray)\b", "mapfile/readarray (bash 4)", "mapfile -t lines < f"),
    (r"\$\{[A-Za-z_][A-Za-z0-9_]*(\[[^]]*\])?(,,?|\^\^?)\}", "case conversion (bash 4)", 'y=${x,,}'),
    (r"\$\{[A-Za-z_][A-Za-z0-9_]*@[QEPAaKkUuL]\}", "${x@Q} (bash 4.4)", 'echo ${x@Q}'),
    (r"\$\{[A-Za-z_][A-Za-z0-9_]*\[-[0-9]+\]\}", "negative array index (bash 4.3)", 'echo ${a[-1]}'),
    (r"&>>", "&>> (bash 4)", "cmd &>> log"),
    (r"\|&", "|& (bash 4)", "cmd |& tee x"),
    (r"\bcoproc\b", "coproc (bash 4)", "coproc cat"),
    (r";;&|;&\s*$", "case fall-through (bash 4)", "  a) x ;&"),
    (r"\{[A-Za-z_][A-Za-z0-9_]*\}[<>]", "{fd}> allocation (bash 4.1)", "exec {fd}>file"),
    (r"\$EPOCH(SECONDS|REALTIME)|\bwait -n\b|printf[^|]*%\([^)]*\)T|\bshopt -s globstar", "bash 4/5 builtins", "wait -n"),
    (r"\[\[ -v ", "[[ -v (bash 4.2)", "[[ -v X ]]"),
    (r"\bsed\s+(-[a-zA-Z]*\s+)*-i(\s+[^'\"]|\s*$)", "sed -i without a suffix argument (BSD sed needs one)", "sed -i s/a/b/ f"),
    (r"\bsed\s+(-[a-zA-Z]*\s+)*-r\b", "sed -r (BSD: -E)", "sed -r 's/a+//'"),
    (r"\breadlink\s+-[a-zA-Z]*f", "readlink -f (BSD lacks it)", "readlink -f x"),
    (r"\b(sha256sum|md5sum|sha1sum)\b", "GNU checksum tools (macOS: shasum)", "sha256sum f"),
    (r"\bnproc\b", "nproc (macOS: sysctl -n hw.ncpu)", "n=$(nproc)"),
    (r"(^|[\s;|&(])timeout\s+[0-9]", "timeout (not on macOS)", "timeout 5 cmd"),
    (r"\bgrep\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*P", "grep -P (BSD lacks it)", "grep -P '\\d' f"),
    (r"\bxargs\s+(-[a-zA-Z0-9]*\s+)*-[a-zA-Z]*r", "xargs -r (BSD lacks it)", "xargs -r rm"),
    (r"\bstat\s+(-[a-zA-Z]*\s+)*-c\b|\bstat\s+--", "GNU stat -c", "stat -c %s f"),
    (r"\bdate\s+(-[a-zA-Z]*\s+)*-d\b|\bdate\s+--date", "GNU date -d", "date -d yesterday"),
    (r"\bsort\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*V", "sort -V", "sort -V list"),
    (r"\bfind\b[^|]*-printf", "find -printf", "find . -printf '%p'"),
    (r"\b(cp|mv|ln|rm|mkdir|tar|grep|sed|head|tail|mktemp|cut)\s+--[a-z]", "GNU long options of core tools", "cp --parents a b"),
    (r"\bhead\s+-n\s*-[0-9]|\btail\s+-n\s*\+0", "head -n -N", "head -n -2 f"),
    (r"\bdu\s+(-[a-zA-Z]*\s+)*-b\b", "du -b", "du -b x"),
    (r"\becho\s+-e\b", "echo -e (not portable)", "echo -e 'a\\n'"),
]


# long options that macOS's own tar (bsdtar) has, for writing an archive that names no account and carries none of
# the Mac's attributes (build/package_local.sh): they are not what the long-options rule is after, which still catches
# every other long option of tar
BSDTAR_OPTIONS = r"--(?:uid|gid|uname|gname)\s+\S+|--no-(?:xattrs|acls|fflags|mac-metadata)\b"


def scan_problems(files):
    p = []
    for f, text in files.items():
        for n, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            code = re.sub(r"\s#\s.*$", "", line)
            if re.search(r"\btar\s", code):
                code = re.sub(BSDTAR_OPTIONS, "", code)
            for rx, what, _ in SCAN:
                if re.search(rx, code):
                    p.append(f"{f}:{n}: {what}: {line.strip()[:120]}")
    return p


def arm_scan():
    names = ["build.sh", "build/pins.sh", "build/local_build_host.sh", "build/package_local.sh", "build/build_plugin_dev.sh",
             "build/toolchain.sh"]
    bs = (ROOT / "build.sh").read_text()
    sourced = re.findall(r"^\s*(?:source|\.)\s+(\S+)", bs, re.M)
    check("build.sh sources no other file (build/pins.sh and the configuration file are read as data)", not sourced, sourced)
    files = {n: (ROOT / n).read_text() for n in names}
    p = scan_problems(files)
    check("build.sh and its helpers use nothing that bash 3.2 or the BSD tools of macOS lack", not p, p)
    for rx, what, sample in SCAN:
        if not scan_problems({"control": sample}):
            check(f"control: the scan catches {what}", False, sample)
    check(f"control: each of the {len(SCAN)} scan patterns catches its example", all(scan_problems({"c": s}) for _, _, s in SCAN))
    own = "tar --uid 0 --gid 0 --uname root --gname wheel --no-xattrs --no-acls --no-fflags --no-mac-metadata -czf t d"
    check("the scan lets through the long options macOS's own tar has for an archive without the builder's account",
          not scan_problems({"c": own}), scan_problems({"c": own}))
    check("control: a GNU-only option of tar beside them is caught", bool(scan_problems({"c": own.replace("--gid 0", "--owner=root")})))
    for n in names:
        r = subprocess.run([BASH, "-n", str(ROOT / n)], capture_output=True, text=True)
        check(f"{BASH} -n {n}", r.returncode == 0, r.stderr)
    bad = TMP / "broken.sh"
    bad.write_text("if true; then\n  echo x\nfi fi\n")
    r = subprocess.run([BASH, "-n", str(bad)], capture_output=True, text=True)
    check("control: bash -n rejects a broken script", r.returncode != 0)


def frameworks(text):
    return set(re.findall(r"-framework\s+(\w+)", text))


def plugin_link_problems(texts):
    """build/build_plugin.sh links the plugin with every framework the release and development scripts link it with:
    without IOKit and CoreFoundation its link stops on 13 undefined IOKit symbols (seen with Xcode 26.5)."""
    have = frameworks(texts["build/build_plugin.sh"])
    p = []
    for other in ("build/build_plugin_release.sh", "build/build_plugin_dev.sh"):
        miss = frameworks(texts[other]) - have
        if miss:
            p.append(f"build/build_plugin.sh lacks {sorted(miss)}, which {other} links")
    return p


def arm_plugin_link():
    names = ("build/build_plugin.sh", "build/build_plugin_release.sh", "build/build_plugin_dev.sh")
    texts = {n: (ROOT / n).read_text() for n in names}
    p = plugin_link_problems(texts)
    check("build/build_plugin.sh links IOKit and CoreFoundation like the release and development scripts", not p, p)
    old = dict(texts)
    old["build/build_plugin.sh"] = texts["build/build_plugin.sh"].replace(" -framework IOKit -framework CoreFoundation", "")
    negative("the link line of v2.9 (no -framework IOKit)", plugin_link_problems(old))


# ------------------------------------------------------------------------------------------------- documents
def script_names(text):
    """The commands, options and settable variables build.sh defines (from its text)."""
    cmds = set(re.search(r'case "\$CMD" in ([a-z|]+)\) ;;', text).group(1).split("|"))
    main = text[text.index("main() {"):]
    loop = main[main.index("while [ $# -gt 0 ]"):main.index("  done")]
    opts = set()
    for lab in re.findall(r"^\s*([-\w|=*\[\]0-9]+)\)", loop, re.M):
        for o in lab.split("|"):
            o = o.split("=")[0].rstrip("*")
            if re.fullmatch(r"-{1,2}[a-z][a-z-]*", o):
                opts.add(o)
    vars_ = set()
    for name in ("OPTION_VARS", "LOCATION_VARS", "SCRIPT_VARS"):
        vars_ |= set(re.search(rf'^{name}="([^"]*)"', text, re.M).group(1).split())
    vars_.add("NM_CONFIG")
    # and what "./build.sh env" exports besides them (set by build.sh for the scripts)
    env = text[text.index("print_env() {"):text.index("# ------", text.index("print_env() {"))]
    vars_ |= set(re.findall(r'say "export ([A-Z_][A-Z0-9_]*)=', env))
    comps = set(re.search(r'^ALL_COMPONENTS="([^"]*)"', text, re.M).group(1).split())
    comps |= set(re.search(r'^EXTRA_COMPONENTS="([^"]*)"', text, re.M).group(1).split())
    return cmds, opts, vars_, comps


def doc_names(text):
    """The commands, options and variables a document lists in its reference tables (sections marked <!-- reference -->)."""
    cmds, opts, vars_, comps = set(), set(), set(), set()
    for block in re.findall(r"<!-- reference: (\w+) -->(.*?)<!-- end -->", text, re.S):
        kind, body = block
        for row in re.findall(r"^\|(.*)\|\s*$", body, re.M):
            cells = re.split(r"(?<!\\)\|", row)   # a cell may hold an escaped \|
            first = re.findall(r"`([^`]+)`", cells[0])
            second = re.findall(r"`([^`]+)`", cells[1]) if len(cells) > 1 else []
            if kind == "commands":
                cmds |= {t.split()[0] for t in first}
            elif kind == "components":
                comps |= {t for t in first}
            elif kind == "options":
                for t in first:
                    for o in re.findall(r"(?<![\w-])(-{1,2}[a-z][a-z-]*)", t):
                        opts.add(o)
                vars_ |= {t for t in second if re.fullmatch(r"[A-Z][A-Z0-9_]+", t)}
            elif kind == "variables":
                vars_ |= {t for t in first if re.fullmatch(r"[A-Z][A-Z0-9_]+", t)}
    return cmds, opts, vars_, comps


def docs_problems(script_text, docs):
    want = script_names(script_text)
    p = []
    for name, text in docs.items():
        got = doc_names(text)
        for what, w, g in zip(("command", "option", "variable", "component"), want, got):
            for x in sorted(w - g):
                p.append(f"{name} does not list the {what} {x}")
            for x in sorted(g - w):
                p.append(f"{name} lists the {what} {x}, which build.sh does not have")
    return p


def arm_docs():
    script = (ROOT / "build.sh").read_text()
    docs = {}
    for n in ("docs/BUILDING.md", "docs/BUILDING.es.md"):
        f = ROOT / n
        docs[n] = f.read_text() if f.exists() else ""
    p = docs_problems(script, docs)
    check("docs/BUILDING.md and docs/BUILDING.es.md list exactly build.sh's commands, options, variables and components", not p, p[:20])
    en = docs["docs/BUILDING.md"]
    for t in ("## When a build fails", "## Installing the result and going back"):
        check(f"docs/BUILDING.md has the section build.sh's messages point to: {t}", t in en)
    neg = script.replace('      --offline) setlist="$setlist NM_OFFLINE" ;;\n',
                         '      --offline) setlist="$setlist NM_OFFLINE" ;;\n      --turbo) setlist="$setlist NM_TURBO" ;;\n')
    neg = neg.replace('OPTION_VARS="NM_WORK ', 'OPTION_VARS="NM_TURBO NM_WORK ')
    negative("an option and a variable added to build.sh but not to the documents", docs_problems(neg, docs))
    neg_docs = {k: v.replace("`--offline`", "`--offline`, `--online`") for k, v in docs.items()}
    negative("a document that lists an option build.sh does not have", docs_problems(script, neg_docs))


# ------------------------------------------------------------------------------------------------- main
def timed(name, fn, *args):
    """Run one arm and print its lines together; with NMT_TIMES=1 also how long it took."""
    t0 = time.monotonic()
    _TLS.buf = []
    try:
        return fn(*args)
    finally:
        lines, _TLS.buf = _TLS.buf, None
        if os.environ.get("NMT_TIMES"):
            lines.append(f"time {name}: {time.monotonic() - t0:.1f} s")
        with _LOCK:
            for l in lines:
                print(l, flush=True)


def finish(worker):
    """Wait for the second thread of arms, if it was started (the sandboxes are removed only after it ends); another
    Ctrl-C while waiting stops its runs again instead of leaving it and the sandboxes behind."""
    while worker is not None and worker.ident and worker.is_alive():
        try:
            worker.join(0.5)
        except KeyboardInterrupt:
            stop_all()
    return None


def main():
    v = subprocess.run([BASH, "-c", 'echo "$BASH_VERSION"'], capture_output=True, text=True).stdout.strip()
    print(f"build.sh runs under {BASH} (bash {v})")
    if not v.startswith("3.2"):
        print(f"note: this is not bash 3.2, the /bin/bash of macOS; set BUILD_SH_BASH to a bash 3.2 to test what a Mac runs")
    for t in ("git", "shasum", "perl", "tar", "patch"):
        if not shutil.which(t):
            print(f"SKIP: {t} is needed to imitate a Mac here")
            sys.exit(2)
    worker = None
    errors = []
    try:
        if want("pins"):
            timed("pins", arm_pins)
        if want("scan"):
            timed("scan", arm_scan)
        if want("docs"):
            timed("docs", arm_docs)
        if want("plugin"):
            timed("plugin_link", arm_plugin_link)
        commits = timed("template", build_template)
        if want("pins"):
            timed("pins_script", arm_pins_script)
        # Each arm below makes its own imitated Mac and shares nothing with the others, so they run in a second thread
        # beside the arms that build on one shared Mac (those stay in this thread: the interrupt arm sets the signal
        # handling, which only the main thread may). Most of the time is build.sh's own forks; two threads halve the
        # time the test needs on a loaded machine. NMT_SERIAL=1 runs everything in this thread.

        def standalone():
            try:
                if want("fresh"):
                    timed("fresh", arm_fresh)
                if want("hostbrew"):
                    timed("hostbrew", arm_hostbrew)
                if want("whichbrew"):
                    timed("whichbrew", arm_whichbrew)
                if want("brewenv"):
                    timed("brewenv", arm_brewenv)
                if want("pathtools"):
                    timed("pathtools", arm_pathtools)
                if want("readonly"):
                    timed("readonly", arm_readonly)
                if want("noinstall"):
                    timed("noinstall", arm_noinstall)
                if want("pylink"):
                    timed("pylink", arm_pylink)
                if want("needs"):
                    timed("needs", arm_needs)
                if want("tamper"):
                    timed("tamper", arm_tamper)
                if want("githooks"):
                    timed("githooks", arm_githooks)
                if want("space"):
                    timed("space", arm_space)
                if want("hosts"):
                    timed("hosts", arm_hosts, commits)
                if want("deps"):
                    timed("deps_control", arm_deps_control)
                if want("llvm23"):
                    timed("llvm23", arm_llvm23)
                if want("cargoconf"):
                    timed("cargoconf", arm_cargoconf)
                if want("spirvprefix"):
                    timed("spirvprefix", arm_spirvprefix)
                if want("ownllvm"):
                    timed("ownllvm", arm_ownllvm)
                if want("settings"):
                    timed("settings", arm_settings)
                if want("basepkg"):
                    timed("basepkg", arm_basepkg)
                if want("stepgit"):
                    timed("stepgit", arm_stepgit)
                if want("mesagit"):
                    timed("mesagit", arm_mesagit)
                if want("source"):
                    timed("source", arm_source)
                    timed("source_some", arm_source_some)
                    timed("source_fail", arm_source_fail)
                    timed("source_modes", arm_source_modes)
                    timed("source_ownpy", arm_source_ownpy)
                if want("sdkroot"):
                    timed("sdkdeps", arm_sdkdeps)
                    timed("nosdk", arm_nosdk)
            except BaseException as e:   # reported after the join, as a failure
                errors.append(e)

        worker = threading.Thread(target=standalone, name="standalone arms")
        if os.environ.get("NMT_SERIAL"):
            standalone()
        else:
            worker.start()
        mac_arms = ["deps", "fetch", "offline", "dryrun", "order", "vars", "sdkroot", "env", "stamps", "failure", "steps", "interrupt", "concurrent",
                    "test", "package", "nohome", "clean"]
        if any(want(a) for a in mac_arms):
            mac = Sandbox("mac", formulas=("cmake",))
            timed("deps", arm_deps, mac)     # the arms below build on what deps, fetch and the full build leave in this sandbox
            timed("fetch", arm_fetch, mac, commits)
            if want("offline"):
                timed("offline", arm_offline, mac)
            timed("order", arm_order, mac)
            for name, fn in (("dryrun", arm_dryrun), ("vars", arm_vars), ("sdkroot", arm_sdkstep), ("env", arm_env), ("stamps", arm_stamps),
                             ("failure", arm_failure), ("steps", arm_steps), ("interrupt", arm_interrupt), ("concurrent", arm_concurrent),
                             ("test", arm_test_cmd), ("package", arm_package), ("nohome", arm_nohome), ("clean", arm_clean)):
                if want(name):
                    timed(name, fn, mac)
        worker = finish(worker)
        for e in errors:
            raise e
    except BaseException as e:
        stop_all()
        raise
    finally:
        finish(worker)
        if worker is not None:   # this thread failed first: what the other one raised is not lost
            for e in errors:
                if not isinstance(e, KeyboardInterrupt):
                    traceback.print_exception(type(e), e, e.__traceback__)
        if os.environ.get("NMT_KEEP"):
            print(f"sandbox kept: {TMP}")
        else:
            shutil.rmtree(TMP, ignore_errors=True)
    print(f"{fails} failed" if fails else "all passed")
    return 1 if fails else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("interrupted: the runs were stopped and the sandboxes removed; no result", flush=True)
        sys.exit(130)
