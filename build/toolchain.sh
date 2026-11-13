#!/bin/bash
# build/toolchain.sh: build the tools NVK's build needs from pinned sources, into one folder, without Homebrew.
#
#   build/toolchain.sh list                 the tool names in build order, one per line
#   build/toolchain.sh id <name>            one line that changes whenever that tool's recipe changes
#   build/toolchain.sh build <name>...      build, install and check each, in the order given; stop at the first failure
#   build/toolchain.sh check <name>...      only the checks of what is installed
#
# Homebrew has no ready-made builds for Intel Macs any more: there "brew install" compiles LLVM with every target, two
# Pythons and OpenSSL for hours into /usr/local. This script builds only what NVK's build uses, for this Mac's own
# processor, with Apple's clang, from source trees that build.sh has fetched and verified by commit.
#
# Input is the environment only (build.sh sets it):
#   NM_TC_PREFIX  where the tools are installed. Fixed layout: bin/ (ninja, cmake, pkgconf and its pkg-config link,
#                 glslang and glslangValidator, the spirv-* programs), lib/, include/, lib/pkgconfig/SPIRV-Tools.pc;
#                 python/ (Python 3.14 with pip); llvm/ (LLVM and clang, shared libraries); ssl/ (OpenSSL's folder with
#                 cert.pem, where the Python built here looks for CA certificates by default); build-deps/ (static
#                 OpenSSL, zstd and liblzma with their headers, used only while Python is built); .nm-toolchain/ (one
#                 stamp per tool, written after its checks pass, and the list of files each tool installed).
#   NM_TC_SRC     one source tree per tool, $NM_TC_SRC/<name>, and $NM_TC_SRC/spirv-headers. Never written to.
#   NM_TC_WORK    build trees, one per tool, removed once that tool's checks pass (NM_TC_KEEP=1 keeps them); also the
#                 home folder and the temporary folder of every build command.
#   NM_TC_LOGS    one log per tool: every command line with its output.
#   NM_TC_JOBS (default: the number of CPUs), NM_TC_MESON (meson, for pkgconf only), NM_TC_PYTHON (a Python 3 for the
#   build scripts of ninja, SPIRV-Tools, glslang and LLVM; default the toolchain's own once it exists, else
#   /usr/bin/python3), NM_TC_STAMP (the text of a finished tool's stamp; default its id), NM_TC_KEEP.
# Nothing else of the caller's environment reaches a build command, and nothing is written outside the four folders.
#
# Written for the /bin/bash 3.2 of macOS and its BSD tools. It also runs on Linux, so that the portable recipes and the
# host test (tools/test_toolchain_sh.py) can run there; the steps that exist only on a Mac (xcrun, security, otool,
# OpenSSL's darwin targets) are taken only when uname -s says Darwin.
set -eu -o pipefail
umask 022

# ---------------------------------------------------------------------------------------------------------- messages
say()  { printf '%s\n' "$*"; }
warn() { say "WARNING: $*" >&2; }
die()  { say "STOP: $*" >&2; exit 2; }

# ---------------------------------------------------------------------------------------------------------- the tools
TOOLS="ninja cmake openssl zstd xz python pkgconf spirv-tools glslang llvm"

# A recipe's number: raise it whenever that tool's recipe or checks change, so that build.sh, which compares the id
# with the stamp, builds the tool again. The processor is part of the id: a toolchain made under Rosetta is not the
# one a native shell needs.
recipe_number() {
  case "$1" in
    ninja) echo 1 ;; cmake) echo 1 ;; openssl) echo 1 ;; zstd) echo 1 ;; xz) echo 1 ;; python) echo 1 ;;
    pkgconf) echo 1 ;; spirv-tools) echo 1 ;; glslang) echo 1 ;; llvm) echo 1 ;;
    *) return 1 ;;
  esac
}
known_tool() { case " $TOOLS " in *" $1 "*) return 0 ;; esac; return 1; }
tool_id() { printf '%s recipe %s %s-%s\n' "$1" "$(recipe_number "$1")" "$(uname -s)" "$(uname -m)"; }

is_darwin() { [ "$(uname -s)" = Darwin ]; }
ncpu() { sysctl -n hw.ncpu 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2; }
# the extension of a shared library on this system (LLVM's layout check)
shlib_ext() { if is_darwin; then echo dylib; else echo so; fi; }

# ---------------------------------------------------------------------------------------------------------- settings
# need_dir <variable> [create]: an absolute folder without spaces or "." parts; with "create", made when missing
need_dir() {
  local v=$1 p ws
  eval "p=\${$v:-}"
  [ -n "$p" ] || die "$v is not set (build.sh sets it; see the head of build/toolchain.sh)"
  ws=$(printf ' \t\n')
  case "$p" in /) die "$v may not be /" ;; /*) ;; *) die "$v must be an absolute path (got $p)" ;; esac
  case "$p" in *[$ws]*) die "$v contains a space ($p): the build systems of these tools split their commands on it" ;; esac
  case "$p/" in */./*|*/../*) die "$v may not contain . or .. parts (got $p)" ;; esac
  if [ "${2:-}" = create ]; then mkdir -p "$p" || die "cannot create $v ($p)"
  else [ -d "$p" ] || die "$v names $p, which is not a folder"; fi
  p=$(cd "$p" && pwd -P)
  eval "$v=\$p"
}
setup_build() {
  need_dir NM_TC_PREFIX create; need_dir NM_TC_SRC; need_dir NM_TC_WORK create; need_dir NM_TC_LOGS create
  P=$NM_TC_PREFIX; SRC=$NM_TC_SRC; WORK=$NM_TC_WORK; LOGS=$NM_TC_LOGS
  local a b
  for a in P SRC WORK LOGS; do for b in P SRC WORK LOGS; do
    [ "$a" = "$b" ] && continue
    eval "case \"\$$a/\" in \"\$$b/\"*) die \"NM_TC_ folders may not lie inside one another: \$$a is inside \$$b\" ;; esac"
  done; done
  JOBS=${NM_TC_JOBS:-$(ncpu)}
  case "$JOBS" in ''|*[!0-9]*|0) die "NM_TC_JOBS must be a positive number (got $JOBS)" ;; esac
  W_HOME=$WORK/home; W_TMP=$WORK/tmp
  mkdir -p "$W_HOME" "$W_TMP" "$P/.nm-toolchain"
  # this script's own helpers (sort may spill to a temporary file) write there too, not to the caller's TMPDIR
  TMPDIR=$W_TMP; export TMPDIR
  SDK=""
  if is_darwin; then
    # Apple's clang, when the /usr/bin shim picks the SDK itself, also searches /usr/local/include and /usr/local/lib:
    # on an Intel Mac that is Homebrew's prefix, so a build could take Homebrew's headers and libraries without anyone
    # asking. With SDKROOT given explicitly it does not.
    SDK=$(clean_run xcrun --sdk macosx --show-sdk-path 2>/dev/null) || SDK=""
    [ -n "$SDK" ] && [ -d "$SDK" ] || die "xcrun --sdk macosx --show-sdk-path gives no SDK: install Xcode and select it (xcode-select), or set DEVELOPER_DIR"
  fi
}
setup_check() {
  need_dir NM_TC_PREFIX
  P=$NM_TC_PREFIX; SDK=""
}

# ---------------------------------------------------------------------------------------------------------- clean env
# Every build command runs as "env -i" plus this list: a home folder and a temporary folder in the work folder (so that
# caches, pip's configuration and what a tool writes "to the home folder" stay there), the toolchain's bin first on a
# PATH of the system's own folders, the SDK, and the Xcode the caller chose. Nothing else: no CC, CFLAGS, LDFLAGS,
# CPATH, LIBRARY_PATH, PKG_CONFIG_PATH, PYTHONPATH, no Homebrew variables. Python writes no bytecode, so that the
# scripts the builds run from the source trees leave those trees as they are.
clean_env() {   # [extra PATH folder]: sets CLEAN
  local path="$P/bin"
  [ $# -gt 0 ] && [ -n "$1" ] && path="$path:$1"
  path="$path:/usr/bin:/bin:/usr/sbin:/sbin"
  CLEAN=(env -i "HOME=$W_HOME" "USER=${USER:-$(id -un)}" LANG=C "TMPDIR=$W_TMP" "PATH=$path" PYTHONDONTWRITEBYTECODE=1)
  [ -z "$SDK" ] || CLEAN+=("SDKROOT=$SDK")
  [ -z "${DEVELOPER_DIR:-}" ] || CLEAN+=("DEVELOPER_DIR=$DEVELOPER_DIR")
}
# a helper (xcrun, security) run the same way, before any tool exists
clean_run() {
  local e=(env -i "HOME=$W_HOME" "USER=${USER:-$(id -un)}" LANG=C "TMPDIR=$W_TMP" PATH=/usr/bin:/bin:/usr/sbin:/sbin)
  [ -z "${DEVELOPER_DIR:-}" ] || e+=("DEVELOPER_DIR=$DEVELOPER_DIR")
  "${e[@]}" "$(command -v "$1")" "${@:2}"
}
# The Python for build scripts. Its own folder joins PATH only when it is the toolchain's: a Python of another prefix
# (Homebrew's /usr/local/bin) would bring that prefix's other programs with it.
pick_python() {
  PY=${NM_TC_PYTHON:-}
  if [ -z "$PY" ]; then
    if [ -x "$P/python/bin/python3.14" ]; then PY=$P/python/bin/python3.14; else PY=/usr/bin/python3; fi
  fi
  [ -x "$PY" ] || die "$1 needs a Python 3 for its build scripts, and $PY is not one (set NM_TC_PYTHON)"
  PYDIR=""
  case "$PY" in "$P/python/bin/"*) PYDIR=$P/python/bin ;; esac
}
cmake_ignore() {   # what every cmake configure gets
  CMAKE_COMMON=(-DCMAKE_BUILD_TYPE=Release "-DCMAKE_IGNORE_PREFIX_PATH=/opt/homebrew;/usr/local;/opt/local" "-DCMAKE_PREFIX_PATH=$P")
}
on_clean_path() { PATH="$P/bin:/usr/bin:/bin:/usr/sbin:/sbin" command -v "$1" >/dev/null 2>&1; }
need_program() {   # <tool> <program> <what to do>
  on_clean_path "$2" || die "$1 is built with $2, which is not in $P/bin nor in the system's folders: $3"
}

# ---------------------------------------------------------------------------------------------------------- logging
LOG=/dev/null
logline() { printf '%s\n' "$*" >> "$LOG"; }
logcmd() {
  local a out="+"
  for a in "$@"; do out="$out $(printf '%q' "$a")"; done
  printf '%s\n' "$out" >> "$LOG"
}
FAILED_TOOL=""
step_failed() {   # <exit status>
  logline "exit status $1"
  FAILED_TOOL=$CUR_TOOL
  say "" >&2
  say "STOP: $CUR_TOOL failed (exit status $1). Last lines of its log:" >&2
  tail -n 25 "$LOG" | sed 's/^/    /' >&2
  say "full log: $LOG" >&2
  exit 1
}
x() {   # a command, logged, its output into the log
  logcmd "$@"
  local rc=0
  "$@" >> "$LOG" 2>&1 < /dev/null || rc=$?
  [ "$rc" = 0 ] || step_failed "$rc"
}
xin() {   # <folder> <command...>: the same, run in that folder
  local d=$1 rc=0; shift
  logline "+ cd $(printf '%q' "$d")"; logcmd "$@"
  ( cd "$d" && "$@" ) >> "$LOG" 2>&1 < /dev/null || rc=$?
  [ "$rc" = 0 ] || step_failed "$rc"
}

# ---------------------------------------------------------------------------------------------------------- the files of a tool
# The prefix is shared, so each tool's files are recorded: the files that appeared while it was installed. A new build
# of a tool first removes the files of its record and nothing else, so the other tools' files and stamps stay.
STATE=""   # $P/.nm-toolchain
prefix_listing() {   # every file and link under the prefix, relative, sorted
  ( cd "$P" && find . \( -type f -o -type l \) ! -path './.nm-toolchain/*' | sed 's|^\./||' | LC_ALL=C sort )
}
forget_tool_files() {   # <tool>: remove what its record lists, then empty folders
  local rec=$STATE/$1.files rel
  [ -f "$rec" ] || return 0
  local d
  while IFS= read -r rel; do
    case "$rel" in ''|/*|..|../*|*/../*|*/..|.nm-toolchain/*) continue ;; esac
    rm -f "$P/$rel"
    # the folders it leaves empty go too; a folder that still holds something (another tool's files) stays
    d=$rel
    while case "$d" in */*) true ;; *) false ;; esac; do
      d=${d%/*}
      rmdir "$P/$d" 2>/dev/null || break
    done
  done < "$rec"
  rm -f "$rec"
}
BEFORE=""
record_tool_files() {   # <tool>: what is new since BEFORE was listed
  [ -n "$BEFORE" ] && [ -f "$BEFORE" ] || return 0
  prefix_listing | LC_ALL=C comm -13 "$BEFORE" - > "$STATE/$1.files"
  rm -f "$BEFORE"; BEFORE=""
}

# ---------------------------------------------------------------------------------------------------------- recipes
# Each recipe builds in $B (an empty folder in the work folder) and installs into the prefix. What each project could
# find on the machine by itself is either given or turned off, and the comments say which and why.

# ninja 1.13.2: its own bootstrap (a Python script and the C++ compiler); configure.py builds out of tree when run from
# another folder. It reads CXX, CFLAGS, CXXFLAGS and LDFLAGS from the environment, which the clean environment does not
# have.
recipe_ninja() {
  pick_python ninja; clean_env "$PYDIR"
  xin "$B" "${CLEAN[@]}" "$PY" "$SRC/ninja/configure.py" --bootstrap
  x mkdir -p "$P/bin"
  x cp "$B/ninja" "$P/bin/ninja"
  x chmod 755 "$P/bin/ninja"
}
check_ninja() { need_file bin/ninja; answers bin/ninja --version; }

# cmake 4.4.5: its bootstrap, out of tree, generating for ninja. Every third-party library is the copy bundled with
# CMake (--no-system-libs: on macOS the bootstrap would otherwise take the system's curl), so nothing is looked up on the
# machine; the bundled curl gets no TLS library (CMAKE_USE_OPENSSL=OFF: this cmake downloads nothing, and OpenSSL is not
# built yet); no curses dialog (ccmake), no Qt dialog, no tests.
recipe_cmake() {
  need_program cmake ninja "build ninja first"
  clean_env; cmake_ignore
  xin "$B" "${CLEAN[@]}" "$SRC/cmake/bootstrap" "--prefix=$P" "--parallel=$JOBS" --generator=Ninja --no-system-libs --no-qt-gui \
    -- "${CMAKE_COMMON[@]}" -DCMAKE_USE_OPENSSL=OFF -DBUILD_CursesDialog=OFF -DBUILD_QtDialog=OFF -DBUILD_TESTING=OFF
  x "${CLEAN[@]}" ninja -C "$B" -j "$JOBS"
  x "${CLEAN[@]}" ninja -C "$B" install
}
check_cmake() {
  need_file bin/cmake; need_file bin/ctest; need_file bin/cpack; need_glob share 'cmake-*'
  answers bin/cmake --version; answers bin/ctest --version; answers bin/cpack --version
}

# OpenSSL 3.5.9, static, only so that Python has ssl and hashlib (pip and meson's downloads need them). Out of tree.
# The Configure target is this Mac's processor (darwin64-x86_64-cc or darwin64-arm64-cc). Turned off: shared libraries
# and loadable modules (the providers are built into libcrypto, so there is no module folder to find at run time), the
# openssl program, the documentation and the tests. zlib, brotli and zstd compression and kTLS are off by default.
# OPENSSLDIR is $P/ssl: that is where this OpenSSL, and so this Python, looks for cert.pem by default.
# cert.pem is the macOS system roots (read from the system keychain with "security", which only reads).
recipe_openssl() {
  local target=()
  if is_darwin; then
    case "$(uname -m)" in
      x86_64) target=(darwin64-x86_64-cc) ;; arm64) target=(darwin64-arm64-cc) ;;
      *) die "openssl: no Configure target for processor $(uname -m)" ;;
    esac
  fi
  clean_env
  xin "$B" "${CLEAN[@]}" perl "$SRC/openssl/Configure" ${target[@]+"${target[@]}"} no-shared no-module no-apps no-docs no-tests \
    "--prefix=$P/build-deps" "--openssldir=$P/ssl" --libdir=lib
  xin "$B" "${CLEAN[@]}" make -j "$JOBS"
  xin "$B" "${CLEAN[@]}" make install_sw
  x mkdir -p "$P/ssl"
  if is_darwin; then
    logline "+ security find-certificate -a -p /System/Library/Keychains/SystemRootCertificates.keychain > $P/ssl/cert.pem"
    clean_run security find-certificate -a -p /System/Library/Keychains/SystemRootCertificates.keychain > "$P/ssl/cert.pem" 2>> "$LOG" \
      || step_failed $?
  else
    # Linux: the distribution's bundle (only so that the recipe can be tried there)
    local f
    for f in /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt; do
      if [ -f "$f" ]; then x cp "$f" "$P/ssl/cert.pem"; break; fi
    done
  fi
  logline "cert.pem: $(grep -c 'BEGIN CERTIFICATE' "$P/ssl/cert.pem" 2>/dev/null || echo 0) certificates"
}
check_openssl() {
  need_file build-deps/lib/libssl.a; need_file build-deps/lib/libcrypto.a; need_file build-deps/include/openssl/ssl.h
  need_file ssl/cert.pem
  grep -q 'BEGIN CERTIFICATE' "$P/ssl/cert.pem" 2>/dev/null || bad "ssl/cert.pem holds no certificate"
}

# zstd 1.5.7, static, only for Python's compression.zstd (the driver's tests import it). Its CMake project, out of tree:
# the library only, no programs (they would look for zlib, lzma and lz4), no tests, no shared library.
recipe_zstd() {
  cmake_project "$SRC/zstd/build/cmake" "$P/build-deps" \
    -DZSTD_BUILD_STATIC=ON -DZSTD_BUILD_SHARED=OFF -DZSTD_BUILD_PROGRAMS=OFF -DZSTD_BUILD_TESTS=OFF
}
check_zstd() { need_file build-deps/lib/libzstd.a; need_file build-deps/include/zstd.h; }

# xz 5.8.4, static liblzma only, for Python's lzma (tools/test_igfx_trace.py imports it; the macOS SDK has no lzma.h).
# No translations (they would look for libintl), none of the programs, no documentation, no tests.
recipe_xz() {
  cmake_project "$SRC/xz" "$P/build-deps" \
    -DBUILD_SHARED_LIBS=OFF -DBUILD_TESTING=OFF -DXZ_NLS=OFF -DXZ_TOOL_XZ=OFF -DXZ_TOOL_XZDEC=OFF -DXZ_TOOL_LZMADEC=OFF \
    -DXZ_TOOL_LZMAINFO=OFF -DXZ_DOC=OFF
}
check_xz() { need_file build-deps/lib/liblzma.a; need_file build-deps/include/lzma.h; }

# Python 3.14.10 into $P/python. CPython's build imports modules from its source tree with -E (bytecode would be
# written there), so it is built in a copy of the tree in the work folder. Static libpython, no framework.
# Given: OpenSSL, zstd and liblzma from build-deps (static, so nothing outside the toolchain is loaded at run time).
# From the SDK: zlib, bz2, sqlite3, libffi (configure looks for it under $SDKROOT), uuid. The bundled expat and the
# bundled mpdecimal (not a Homebrew one). pkg-config is not asked at all (--with-pkg-config=no).
# Turned off, nobody here needs them and each would be looked up on the machine: readline, curses, tkinter, dbm and
# gdbm (py_cv_module_<name>=n/a), libintl, the test modules. pip is installed from the wheel inside the source.
recipe_python() {
  local bd=$P/build-deps f
  for f in lib/libssl.a lib/libzstd.a lib/liblzma.a include/zstd.h include/lzma.h; do
    [ -f "$bd/$f" ] || die "python is built with OpenSSL, zstd and xz from $bd, and $bd/$f is missing: build openssl, zstd and xz first"
  done
  rm -rf "$P/python"
  clean_env
  x cp -RP "$SRC/python" "$B/cpython"
  xin "$B/cpython" "${CLEAN[@]}" ./configure "--prefix=$P/python" "--with-openssl=$bd" --with-ensurepip=install \
    --with-pkg-config=no --without-readline --without-system-libmpdec --disable-test-modules \
    "LIBZSTD_CFLAGS=-I$bd/include" "LIBZSTD_LIBS=$bd/lib/libzstd.a" "LIBLZMA_CFLAGS=-I$bd/include" "LIBLZMA_LIBS=$bd/lib/liblzma.a" \
    py_cv_module_readline=n/a py_cv_module__curses=n/a py_cv_module__curses_panel=n/a py_cv_module__tkinter=n/a \
    py_cv_module__dbm=n/a py_cv_module__gdbm=n/a ac_cv_lib_intl_textdomain=no ac_cv_header_libintl_h=no
  xin "$B/cpython" "${CLEAN[@]}" make -j "$JOBS"
  xin "$B/cpython" "${CLEAN[@]}" make install
}
check_python() {
  local py=python/bin/python3.14 out
  need_file "$py"; [ -e "$P/$py" ] || return 0
  answers "$py" --version
  out=$(cd / && env -i "$P/$py" -c 'import ssl, zlib, ctypes, hashlib, venv, lzma, bz2, sqlite3, compression.zstd
print(ssl.OPENSSL_VERSION)
print(ssl.get_default_verify_paths().openssl_cafile)' 2>&1) \
    || { bad "$py cannot import every module NVK's build and the tests need: $(printf '%s' "$out" | tail -n 1)"; return 0; }
  cnote "python imports ssl, zlib, ctypes, hashlib, venv, lzma, bz2, sqlite3, compression.zstd; $(printf '%s\n' "$out" | sed -n 1p)"
  [ "$(printf '%s\n' "$out" | tail -n 1)" = "$P/ssl/cert.pem" ] \
    || bad "$py looks for CA certificates in $(printf '%s\n' "$out" | tail -n 1), not in $P/ssl/cert.pem"
  answers "$py" -m pip --version
}

# pkgconf 2.5.1 with meson (NM_TC_MESON: build.sh's, from its Python environment), static. Its default search path is
# the toolchain's own lib/pkgconfig and share/pkgconfig, never Homebrew's. The "system" folders whose -I and -L it drops
# from its answers are the system's own (by default they would be the toolchain's include and lib, and SPIRV-Tools'
# -I would vanish). No tests (they need kyua).
recipe_pkgconf() {
  local meson=${NM_TC_MESON:-}
  [ -n "$meson" ] || die "pkgconf is built with meson: set NM_TC_MESON"
  [ -x "$meson" ] || die "NM_TC_MESON names $meson, which is not a program"
  need_program pkgconf ninja "build ninja first"
  clean_env
  x "${CLEAN[@]}" "$meson" setup "$B" "$SRC/pkgconf" "--prefix=$P" --libdir=lib --buildtype=release --default-library=static \
    --wrap-mode=nodownload -Dtests=disabled -Dwith-system-libdir=/usr/lib -Dwith-system-includedir=/usr/include
  x "${CLEAN[@]}" "$meson" compile -C "$B" -j "$JOBS"
  x "${CLEAN[@]}" "$meson" install -C "$B"
  [ -e "$P/bin/pkg-config" ] || x ln -s pkgconf "$P/bin/pkg-config"
}
check_pkgconf() {
  need_file bin/pkgconf; need_file bin/pkg-config
  answers bin/pkgconf --version; answers bin/pkg-config --version
}

# SPIRV-Headers at the commit SPIRV-Tools' DEPS names, installed into the prefix, then SPIRV-Tools, shared like
# Homebrew's (Mesa's mesa_clc links it). Absolute install names, so that whatever links it later needs no rpath.
# No tests (they would download googletest, effcee and re2), no warnings as errors (a newer clang than theirs adds some).
recipe_spirv_tools() {
  pick_python spirv-tools
  [ -f "$SRC/spirv-headers/include/spirv/unified1/spirv.h" ] || die "spirv-tools needs SPIRV-Headers in $SRC/spirv-headers"
  B_SUB=headers
  cmake_project "$SRC/spirv-headers" "$P" -DSPIRV_HEADERS_ENABLE_TESTS=OFF -DSPIRV_HEADERS_ENABLE_INSTALL=ON
  B_SUB=tools
  cmake_project "$SRC/spirv-tools" "$P" "-DSPIRV-Headers_SOURCE_DIR=$SRC/spirv-headers" -DSPIRV_SKIP_TESTS=ON \
    -DSPIRV_WERROR=OFF -DBUILD_SHARED_LIBS=ON -DSPIRV_TOOLS_BUILD_STATIC=OFF "-DPython3_EXECUTABLE=$PY"
}
check_spirv_tools() {
  local p
  need_file lib/pkgconfig/SPIRV-Tools.pc; need_file include/spirv-tools/libspirv.h; need_file include/spirv/unified1/spirv.h
  grep -q '^Version: *[0-9]' "$P/lib/pkgconfig/SPIRV-Tools.pc" 2>/dev/null || bad "lib/pkgconfig/SPIRV-Tools.pc has no Version line"
  for p in spirv-as spirv-dis spirv-val spirv-opt spirv-link; do need_file "bin/$p"; [ ! -e "$P/bin/$p" ] || answers "bin/$p" --version; done
}

# glslang 16.6.0 against the SPIRV-Tools above (ALLOW_EXTERNAL_SPIRV_TOOLS, ENABLE_OPT), not the copy its External
# folder would fetch (BUILD_EXTERNAL=OFF). Static libraries: only the program is used. No tests.
recipe_glslang() {
  pick_python glslang
  [ -f "$P/lib/pkgconfig/SPIRV-Tools.pc" ] || die "glslang is built against the toolchain's SPIRV-Tools: build spirv-tools first"
  cmake_project "$SRC/glslang" "$P" -DBUILD_EXTERNAL=OFF -DALLOW_EXTERNAL_SPIRV_TOOLS=ON -DENABLE_OPT=ON -DGLSLANG_TESTS=OFF \
    "-DPython3_EXECUTABLE=$PY"
  [ -e "$P/bin/glslangValidator" ] || x ln -s glslang "$P/bin/glslangValidator"
}
check_glslang() {
  local first
  need_file bin/glslang; need_file bin/glslangValidator; [ -e "$P/bin/glslangValidator" ] || return 0
  answers bin/glslangValidator --version
  first=$(cd / && env -i "$P/bin/glslangValidator" --version 2>/dev/null | sed -n 1p)
  case "$first" in "Glslang Version: "*:*) ;; *) bad "glslangValidator --version starts with \"$first\", not \"Glslang Version: N:x.y.z\" (build.sh reads it)" ;; esac
}

# LLVM 22.1.8 and clang into $P/llvm. Shared libLLVM and libclang-cpp (build.sh builds the SPIRV-LLVM-Translator as a
# shared library against this LLVM and Mesa's mesa_clc links both: with a static LLVM there would be two copies in one
# program), and libclang for bindgen. Absolute install names (CMAKE_INSTALL_NAME_DIR; LLVM leaves @rpath out once
# CMAKE_INSTALL_RPATH is set): the Translator and mesa_clc are built by other build systems that add no rpath.
# Targets: this Mac's own (host: mesa_clc only turns OpenCL C into SPIR-V and bindgen parses headers for this Mac) and
# SPIRV (Mesa compiles for spirv64-unknown-unknown; Homebrew's LLVM, which the release was built with, has it).
# RTTI on and exceptions off, as in Homebrew's LLVM. Turned off, each would be looked up on the machine: zstd, libxml2,
# libedit, libffi, z3, libcurl, cpp-httplib, libpfm; zlib comes from the SDK. No static analyzer, tests, examples,
# benchmarks, documentation or language bindings.
recipe_llvm() {
  pick_python llvm
  rm -rf "$P/llvm"
  cmake_project "$SRC/llvm/llvm" "$P/llvm" \
    -DLLVM_ENABLE_PROJECTS=clang "-DLLVM_TARGETS_TO_BUILD=host;SPIRV" \
    -DLLVM_BUILD_LLVM_DYLIB=ON -DLLVM_LINK_LLVM_DYLIB=ON -DCLANG_LINK_CLANG_DYLIB=ON -DLLVM_ENABLE_RTTI=ON -DLLVM_ENABLE_EH=OFF \
    -DCLANG_ENABLE_STATIC_ANALYZER=OFF \
    -DLLVM_INCLUDE_TESTS=OFF -DLLVM_INCLUDE_EXAMPLES=OFF -DLLVM_INCLUDE_BENCHMARKS=OFF -DLLVM_INCLUDE_DOCS=OFF \
    -DCLANG_INCLUDE_TESTS=OFF -DCLANG_INCLUDE_DOCS=OFF -DLLVM_ENABLE_BINDINGS=OFF \
    -DLLVM_ENABLE_ZLIB=ON -DLLVM_ENABLE_ZSTD=OFF -DLLVM_ENABLE_LIBXML2=OFF -DLLVM_ENABLE_LIBEDIT=OFF -DLLVM_ENABLE_FFI=OFF \
    -DLLVM_ENABLE_Z3_SOLVER=OFF -DLLVM_ENABLE_CURL=OFF -DLLVM_ENABLE_HTTPLIB=OFF -DLLVM_ENABLE_LIBPFM=OFF \
    "-DPython3_EXECUTABLE=$PY"
}
# what Mesa's meson.build asks llvm-config for
LLVM_COMPONENTS="bitwriter engine mcdisassembler core executionengine scalaropts transformutils instcombine coverage target
linker irreader option libdriver lto mcjit"
check_llvm() {
  local so major comps c missing=""
  so=$(shlib_ext)
  need_file llvm/bin/llvm-config; [ -e "$P/llvm/bin/llvm-config" ] || return 0
  need_file llvm/bin/clang; need_glob llvm/lib "libLLVM*.$so*"; need_glob llvm/lib "libclang-cpp*.$so*"; need_file "llvm/lib/libclang.$so"
  need_file llvm/lib/cmake/llvm/LLVMConfig.cmake; need_file llvm/lib/cmake/clang/ClangConfig.cmake
  answers llvm/bin/llvm-config --version; answers llvm/bin/clang --version
  major=$(cd / && env -i "$P/llvm/bin/llvm-config" --version 2>/dev/null | sed 's/\..*//')
  need_file "llvm/lib/clang/$major/include/opencl-c.h"; need_file "llvm/lib/clang/$major/include/opencl-c-base.h"
  [ "$(cd / && env -i "$P/llvm/bin/llvm-config" --shared-mode 2>/dev/null)" = shared ] || bad "llvm-config --shared-mode does not say shared"
  comps=" $(cd / && env -i "$P/llvm/bin/llvm-config" --components 2>/dev/null | tr '\n' ' ') "
  for c in $LLVM_COMPONENTS; do case "$comps" in *" $c "*) ;; *) missing="$missing $c" ;; esac; done
  [ -z "$missing" ] || bad "llvm-config --components lacks what Mesa asks for:$missing"
}

# cmake_project <source> <install prefix> [-D...]: configure for ninja in $B (or $B/$B_SUB), build, install. Shared
# libraries get absolute install names and an rpath of their own lib folder, so that the programs start from an empty
# environment and whatever links them later needs nothing more.
cmake_project() {
  local src=$1 pfx=$2 bld=$B; shift 2
  [ -z "$B_SUB" ] || bld=$B/$B_SUB
  need_program "$CUR_TOOL" cmake "build cmake first"; need_program "$CUR_TOOL" ninja "build ninja first"
  clean_env "${PYDIR:-}"; cmake_ignore
  x "${CLEAN[@]}" cmake -G Ninja -S "$src" -B "$bld" "${CMAKE_COMMON[@]}" "-DCMAKE_INSTALL_PREFIX=$pfx" -DCMAKE_INSTALL_LIBDIR=lib \
    "-DCMAKE_INSTALL_NAME_DIR=$pfx/lib" "-DCMAKE_INSTALL_RPATH=$pfx/lib" "$@"
  x "${CLEAN[@]}" cmake --build "$bld" -j "$JOBS"
  x "${CLEAN[@]}" cmake --install "$bld"
}

# ---------------------------------------------------------------------------------------------------------- checks
CHECK_BAD=0; CHECK_OUT=/dev/stdout
cnote() { printf '  %s\n' "$*" >> "$CHECK_OUT"; }
bad() { printf '  FAIL %s\n' "$*" >> "$CHECK_OUT"; CHECK_BAD=$((CHECK_BAD + 1)); }
# these record a failure and return 0, so that the checks go on and name everything that is wrong
need_file() { [ -e "$P/$1" ] || bad "$P/$1 is missing"; }
need_glob() {   # <folder> <pattern>
  local f
  for f in "$P/$1"/$2; do [ -e "$f" ] && return 0; done
  bad "nothing matches $P/$1/$2"
}
answers() {   # <program, relative> [arguments]: started from / with an empty environment, it answers
  local p=$1 out rc=0; shift
  out=$(cd / && env -i "$P/$p" "$@" 2>&1) || rc=$?
  if [ "$rc" != 0 ] || [ -z "$out" ]; then bad "$p $* does not answer from an empty environment (exit $rc): $(printf '%s' "$out" | head -n 2 | tr '\n' ' ')"
  else cnote "$p $*: $(printf '%s\n' "$out" | sed -n 1p)"; fi
}
# On macOS, every Mach-O file the tool installed may name only the system's libraries and the toolchain's own: a
# library of a Homebrew (or of any other folder) that a build picked up would tie the toolchain to it.
macho_files() {   # <record file>: the Mach-O files among the regular files it lists
  perl -e '
    my $root = shift;
    while (my $rel = <STDIN>) {
      chomp $rel; my $f = "$root/$rel";
      next if -l $f || ! -f $f;
      open(my $h, "<", $f) or next; binmode $h; my $m = ""; read($h, $m, 4); close $h;
      print "$rel\n" if $m =~ /^(\xfe\xed\xfa[\xce\xcf]|[\xce\xcf]\xfa\xed\xfe|\xca\xfe\xba[\xbe\xbf])$/;
    }' "$P" < "$1"
}
check_libraries() {   # <tool>
  local rec=$P/.nm-toolchain/$1.files rel lib n=0
  is_darwin || return 0
  [ -f "$rec" ] || { bad "no record of the files $1 installed (build it with this script)"; return 0; }
  while IFS= read -r rel; do
    n=$((n + 1))
    while IFS= read -r lib; do
      case "$lib" in /usr/lib/*|/System/Library/*|"$P"/*) ;; *) bad "$P/$rel links $lib, which is neither the system's nor the toolchain's" ;; esac
    done < <(otool -L "$P/$rel" 2>/dev/null | sed -n 's/^[[:space:]][[:space:]]*\([^ ][^ ]*\) (.*/\1/p')
  done < <(macho_files "$rec")
  cnote "$n Mach-O files of $1 name only /usr/lib, /System/Library and $P"
}
run_checks() {   # <tool>: CHECK_BAD counts the failures
  CHECK_BAD=0
  case "$1" in
    ninja) check_ninja ;; cmake) check_cmake ;; openssl) check_openssl ;; zstd) check_zstd ;; xz) check_xz ;;
    python) check_python ;; pkgconf) check_pkgconf ;; spirv-tools) check_spirv_tools ;; glslang) check_glslang ;;
    llvm) check_llvm ;;
  esac
  check_libraries "$1"
}

# ---------------------------------------------------------------------------------------------------------- build
CUR_TOOL=""
on_exit() {
  # an interrupted or failed install still records what it put into the prefix, so that the next build removes it
  if [ -n "$CUR_TOOL" ] && [ -n "$BEFORE" ]; then record_tool_files "$CUR_TOOL" 2>/dev/null || true; fi
}
build_one() {
  local t=$1 t0 t1 stamp
  CUR_TOOL=$t; STATE=$P/.nm-toolchain; LOG=$LOGS/$t.log; B=$WORK/$t; B_SUB=""; PYDIR=""
  rm -f "$STATE/$t"
  : > "$LOG"
  logline "build/toolchain.sh build $t: $(tool_id "$t"), $(date -u +%Y-%m-%dT%H:%M:%SZ), $(uname -s) $(uname -m), $JOBS jobs"
  logline "prefix $P, sources $SRC/$t, build folder $B${SDK:+, SDK $SDK}"
  [ -d "$SRC/$t" ] || die "$t: its source tree $SRC/$t is missing (build.sh fetches it)"
  say "toolchain: building $t (log: $LOG)"
  t0=$(date +%s)
  forget_tool_files "$t"
  rm -rf "$B"; mkdir -p "$B"
  BEFORE=$WORK/.$t.before
  prefix_listing > "$BEFORE"
  case "$t" in
    ninja) recipe_ninja ;; cmake) recipe_cmake ;; openssl) recipe_openssl ;; zstd) recipe_zstd ;; xz) recipe_xz ;;
    python) recipe_python ;; pkgconf) recipe_pkgconf ;; spirv-tools) recipe_spirv_tools ;; glslang) recipe_glslang ;;
    llvm) recipe_llvm ;;
  esac
  record_tool_files "$t"
  logline "---- checks"
  CHECK_OUT=$LOG
  run_checks "$t"
  if [ "$CHECK_BAD" != 0 ]; then
    say "" >&2
    say "STOP: $t was built, but $CHECK_BAD of its checks failed:" >&2
    grep '^  FAIL ' "$LOG" | sed 's/^  /    /' >&2
    say "full log: $LOG" >&2
    FAILED_TOOL=$t
    exit 1
  fi
  stamp=${NM_TC_STAMP:-$(tool_id "$t")}
  printf '%s\n' "$stamp" > "$STATE/$t"
  t1=$(date +%s)
  logline "---- done in $((t1 - t0)) s; stamp: $stamp"
  if [ "${NM_TC_KEEP:-0}" = 1 ]; then logline "build folder kept: $B"; else rm -rf "$B"; fi
  say "toolchain: $t ok ($((t1 - t0)) s)"
  CUR_TOOL=""
}

# ---------------------------------------------------------------------------------------------------------- main
usage() { sed -n '3,7p' "$0" | sed 's/^# \{0,1\}//'; }
cmd=${1:-}
[ $# -gt 0 ] && shift
case "$cmd" in
  list)
    [ $# = 0 ] || die "list takes no argument"
    for t in $TOOLS; do say "$t"; done ;;
  id)
    [ $# = 1 ] || die "id takes one tool name"
    known_tool "$1" || die "unknown tool: $1 (build/toolchain.sh list names them)"
    tool_id "$1" ;;
  build|check)
    [ $# -gt 0 ] || die "$cmd needs at least one tool name"
    for t in "$@"; do known_tool "$t" || die "unknown tool: $t (build/toolchain.sh list names them)"; done
    if [ "$cmd" = build ]; then
      setup_build
      trap on_exit EXIT
      trap 'exit 130' INT
      trap 'exit 143' TERM
      for t in "$@"; do build_one "$t"; done
    else
      setup_check
      fails=0
      for t in "$@"; do
        say "$t:"
        CHECK_OUT=/dev/stdout
        run_checks "$t"
        if [ "$CHECK_BAD" = 0 ]; then say "  ok"; else fails=$((fails + 1)); fi
      done
      [ "$fails" = 0 ] || { say "STOP: $fails of $# tools failed their checks" >&2; exit 1; }
    fi ;;
  ''|help|-h|--help) usage ;;
  *) die "unknown command: $cmd (list, id, build or check)" ;;
esac
