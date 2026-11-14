#!/bin/bash
# build/toolchain.sh: build the tools NVK's build needs from pinned sources, into one folder, without Homebrew.
#
#   build/toolchain.sh list                 the tool names in build order, one per line
#   build/toolchain.sh id <name>            one line that changes whenever that tool's recipe changes
#   build/toolchain.sh build <name>...      build, install and check each, in the order given; stop at the first failure
#   build/toolchain.sh check <name>...      only the checks of what is installed
#
# When homebrew-core was read on 2026-11-13 it had no ready-made builds for Intel Macs of the formulas build.sh would
# install: there "brew install" compiles LLVM with every target, two Pythons and OpenSSL into /usr/local. This script
# builds only what NVK's build uses, for this Mac's own processor, with Apple's clang, from source trees that build.sh
# has fetched and verified by commit.
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
#   MACOSX_DEPLOYMENT_TARGET (on a Mac; default 15.0, as in build.sh's steps): the oldest macOS the tools must run on.
#   It is part of every id, and every Mach-O file a tool installs is checked against it.
# Nothing else of the caller's environment reaches a build command, and nothing is written outside the four folders.
#
# What this script removes or overwrites is its own only. A prefix, work or log folder that already exists, is not
# empty and was not made by this script (the prefix has .nm-toolchain/, the work folder .nm-toolchain-work, the log
# folder .nm-toolchain-logs) is refused before anything is written; the folders are judged by where they really are
# (pwd -P), so a link does not get past the rules; a record of installed files with a line that is not a file of the
# prefix reached through its real folders stops the build before anything is removed. xcrun and security are run from
# /usr/bin, never through the caller's PATH.
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
    ninja) echo 2 ;; cmake) echo 2 ;; openssl) echo 2 ;; zstd) echo 2 ;; xz) echo 2 ;; python) echo 2 ;;
    pkgconf) echo 2 ;; spirv-tools) echo 2 ;; glslang) echo 2 ;; llvm) echo 2 ;;
    *) return 1 ;;
  esac
}
known_tool() { case " $TOOLS " in *" $1 "*) return 0 ;; esac; return 1; }
# The version of the pinned source each tool is built from (build/pins.sh pins the sources; tools/test_build_sh.py holds
# these equal to those pins): the checks compare it with what the installed program prints, so that a build from a
# source tree of another version (a by-hand run with another NM_TC_SRC) stops instead of being stamped. Only the tools
# whose real --version line is known; the others print nothing to compare.
pinned_version() {
  case "$1" in
    ninja) echo 1.13.2 ;;
    cmake) echo 4.4.5 ;;
    python) echo 3.14.10 ;;
    pkgconf) echo 2.5.1 ;;
    glslang) echo 16.6.0 ;;
    llvm) echo 22.1.8 ;;
    *) return 1 ;;
  esac
}
# On a Mac the oldest macOS the tools are built for is part of the id too.
tool_id() {
  local dt=""
  if is_darwin; then dt=" macos$(macos_min)"; fi
  printf '%s recipe %s %s-%s%s\n' "$1" "$(recipe_number "$1")" "$(uname -s)" "$(uname -m)" "$dt"
}

is_darwin() { [ "$(uname -s)" = Darwin ]; }
macos_min() {
  local v=${MACOSX_DEPLOYMENT_TARGET:-15.0}
  case "$v" in [0-9]*) ;; *) die "MACOSX_DEPLOYMENT_TARGET must be a macOS version such as 15.0 (got $v)" ;; esac
  case "$v" in *[!0-9.]*|*..*|*.) die "MACOSX_DEPLOYMENT_TARGET must be a macOS version such as 15.0 (got $v)" ;; esac
  printf '%s\n' "$v"
}
# The fixed place of the macOS helpers xcrun and security: never looked up through the caller's PATH.
SYSBIN=/usr/bin
ncpu() { sysctl -n hw.ncpu 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2; }
# the extension of a shared library on this system (LLVM's layout check)
shlib_ext() { if is_darwin; then echo dylib; else echo so; fi; }

# ---------------------------------------------------------------------------------------------------------- settings
# real_path <path>: where a path really is, also before it exists: the nearest existing folder above it, resolved with
# pwd -P, plus the rest
real_path() {
  local p=$1 rest=""
  while [ ! -d "$p" ]; do
    rest="/${p##*/}$rest"; p=${p%/*}
    [ -n "$p" ] || p=/
  done
  p=$(cd "$p" && pwd -P) || return 1
  [ "$p" != / ] || p=""
  printf '%s%s\n' "$p" "$rest"
}
# need_dir <variable>: an absolute folder without spaces, the characters below or "." parts, set to where it really is
# (nothing is created). A colon splits PATH and pkgconf's search path, a semicolon CMake's lists, and pkgconf reads
# quotes and backslashes in a .pc file's paths as shell quoting: a prefix with one of them gives a pkg-config that finds
# nothing or answers -I without the folder.
need_dir() {
  local v=$1 p r ws
  eval "p=\${$v:-}"
  [ -n "$p" ] || die "$v is not set (build.sh sets it; see the head of build/toolchain.sh)"
  ws=$(printf ' \t\n')
  case "$p" in /) die "$v may not be /" ;; /*) ;; *) die "$v must be an absolute path (got $p)" ;; esac
  case "$p" in *[$ws]*) die "$v contains a space ($p): the build systems of these tools split their commands on it" ;; esac
  case "$p" in *[:\;\'\"\\\$\`]*) die "$v contains one of : ; ' \" \\ \$ \` ($p): search paths split on them and pkgconf reads them as quoting" ;; esac
  case "$p/" in */./*|*/../*) die "$v may not contain . or .. parts (got $p)" ;; esac
  r=$(real_path "$p") || die "cannot find where $v ($p) really is"
  case "$r" in ''|/) die "$v ($p) is really /" ;; *[$ws]*) die "$v ($p) is really $r, which contains a space" ;; esac
  case "$r" in *[:\;\'\"\\\$\`]*) die "$v ($p) is really $r, which contains one of : ; ' \" \\ \$ \`" ;; esac
  eval "$v=\$r"
}
# own_dir <variable> <marker>: a folder this script may write into, overwrite in and remove from. It may be missing or
# empty; one that holds something must carry the marker this script leaves in the folders it makes (a home folder, a
# shared prefix such as /usr/local or a log folder with other files is refused before anything is written).
own_dir() {
  local v=$1 m=$2 p c
  eval "p=\$$v"
  if [ -e "$p" ] && [ ! -d "$p" ]; then die "$v ($p) is not a folder"; fi
  # the marker is written through (the stamps and records in the prefix's, an empty file in the others), so it must be
  # what this script makes, never a link: a link would carry those writes to wherever it points. And of its kind: the
  # prefix's a folder, the others a plain file
  if [ -L "$p/$m" ]; then die "$v ($p): $m is a link, which build/toolchain.sh never makes: name a new or empty folder"; fi
  if [ -e "$p/$m" ]; then
    case "$m" in
      .nm-toolchain) [ -d "$p/$m" ] || die "$v ($p): $m is not a folder, which build/toolchain.sh never makes: name a new or empty folder" ;;
      *) [ -f "$p/$m" ] || die "$v ($p): $m is not a plain file, which build/toolchain.sh never makes: name a new or empty folder" ;;
    esac
  fi
  if [ -d "$p" ] && [ ! -e "$p/$m" ]; then
    c=$(ls -A "$p") || die "cannot read $v ($p)"
    [ -z "$c" ] || die "$v ($p) holds files and was not made by build/toolchain.sh (it has no $m): name a new or empty folder"
  fi
  mkdir -p "$p" || die "cannot create $v ($p)"
}
setup_build() {
  need_dir NM_TC_PREFIX; need_dir NM_TC_SRC; need_dir NM_TC_WORK; need_dir NM_TC_LOGS
  P=$NM_TC_PREFIX; SRC=$NM_TC_SRC; WORK=$NM_TC_WORK; LOGS=$NM_TC_LOGS
  local a b
  for a in P SRC WORK LOGS; do for b in P SRC WORK LOGS; do
    [ "$a" = "$b" ] && continue
    eval "case \"\$$a/\" in \"\$$b/\"*) die \"NM_TC_ folders may not lie inside one another: \$$a is inside \$$b\" ;; esac"
  done; done
  [ -d "$SRC" ] || die "NM_TC_SRC names $SRC, which is not a folder"
  JOBS=${NM_TC_JOBS:-$(ncpu)}
  case "$JOBS" in ''|*[!0-9]*|0) die "NM_TC_JOBS must be a positive number (got $JOBS)" ;; esac
  own_dir NM_TC_PREFIX .nm-toolchain; own_dir NM_TC_WORK .nm-toolchain-work; own_dir NM_TC_LOGS .nm-toolchain-logs
  mkdir -p "$P/.nm-toolchain"; : > "$WORK/.nm-toolchain-work"; : > "$LOGS/.nm-toolchain-logs"
  W_HOME=$WORK/home; W_TMP=$WORK/tmp
  mkdir -p "$W_HOME" "$W_TMP"
  # this script's own helpers (sort may spill to a temporary file) write there too, not to the caller's TMPDIR
  TMPDIR=$W_TMP; export TMPDIR
  SDK=""; DT=""
  if is_darwin; then
    DT=$(macos_min)
    # Apple's clang, when the /usr/bin shim picks the SDK itself, also searches /usr/local/include and /usr/local/lib:
    # on an Intel Mac that is Homebrew's prefix, so a build could take Homebrew's headers and libraries without anyone
    # asking. With SDKROOT given explicitly it does not.
    SDK=$(clean_run xcrun --sdk macosx --show-sdk-path 2>/dev/null) || SDK=""
    [ -n "$SDK" ] && [ -d "$SDK" ] || die "xcrun --sdk macosx --show-sdk-path gives no SDK: install Xcode and select it (xcode-select), or set DEVELOPER_DIR"
  fi
}
setup_check() {
  need_dir NM_TC_PREFIX
  P=$NM_TC_PREFIX; SDK=""; DT=""
  [ -d "$P" ] || die "NM_TC_PREFIX names $P, which is not a folder"
  if is_darwin; then DT=$(macos_min); fi
}

# ---------------------------------------------------------------------------------------------------------- clean env
# Every build command runs as "env -i" plus this list: a home folder and a temporary folder in the work folder (so that
# caches, pip's configuration and what a tool writes "to the home folder" stay there), the toolchain's bin first on a
# PATH of the system's own folders, the SDK, the oldest macOS to build for, and the Xcode the caller chose. Nothing else: no CC, CFLAGS, LDFLAGS,
# CPATH, LIBRARY_PATH, PKG_CONFIG_PATH, PYTHONPATH, no Homebrew variables. Python writes no bytecode, so that the
# scripts the builds run from the source trees leave those trees as they are.
clean_env() {   # [extra PATH folder]: sets CLEAN
  local path="$P/bin"
  [ $# -gt 0 ] && [ -n "$1" ] && path="$path:$1"
  path="$path:/usr/bin:/bin:/usr/sbin:/sbin"
  CLEAN=(env -i "HOME=$W_HOME" "USER=${USER:-$(id -un)}" LANG=C "TMPDIR=$W_TMP" "PATH=$path" PYTHONDONTWRITEBYTECODE=1)
  [ -z "$SDK" ] || CLEAN+=("SDKROOT=$SDK")
  [ -z "$DT" ] || CLEAN+=("MACOSX_DEPLOYMENT_TARGET=$DT")
  [ -z "${DEVELOPER_DIR:-}" ] || CLEAN+=("DEVELOPER_DIR=$DEVELOPER_DIR")
}
# a helper of macOS (xcrun, security) run the same way, before any tool exists, from its fixed place
clean_run() {
  local e=(env -i "HOME=$W_HOME" "USER=${USER:-$(id -un)}" LANG=C "TMPDIR=$W_TMP" PATH=/usr/bin:/bin:/usr/sbin:/sbin)
  [ -z "${DEVELOPER_DIR:-}" ] || e+=("DEVELOPER_DIR=$DEVELOPER_DIR")
  "${e[@]}" "$SYSBIN/$1" "${@:2}"
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
# A record names files under the prefix by relative paths, and every folder on the way is a real folder (the listing
# that wrote it does not follow links). A line that names anything else (an absolute path, a "." or ".." part, the
# stamps, a file reached through a link, even one that points back into the prefix) means the record was changed or
# broken: nothing is removed, and the build stops.
record_damaged() {   # <record> <line number> <line>
  die "the record is damaged: $1 line $2 names \"$3\", which is not a file of the prefix $P. Nothing was removed; remove that prefix (or name another NM_TC_PREFIX) and build again"
}
check_record() {   # <record>
  local rec=$1 rel n=0 d r
  while IFS= read -r rel || [ -n "$rel" ]; do
    n=$((n + 1))
    case "/$rel/" in //|*/../*|*/./*|/.nm-toolchain/*|///*) record_damaged "$rec" "$n" "$rel" ;; esac
    case "$rel" in /*) record_damaged "$rec" "$n" "$rel" ;; esac
    d=$P/$rel; d=${d%/*}
    if [ -d "$d" ]; then
      r=$(cd "$d" && pwd -P) || record_damaged "$rec" "$n" "$rel"
      [ "$r" = "$d" ] || record_damaged "$rec" "$n" "$rel"
    elif [ -e "$d" ] || [ -L "$d" ]; then
      record_damaged "$rec" "$n" "$rel"
    fi
  done < "$rec"
}
forget_tool_files() {   # <tool>: remove what its record lists, then empty folders
  local rec=$STATE/$1.files rel d
  [ -f "$rec" ] || return 0
  check_record "$rec"
  while IFS= read -r rel || [ -n "$rel" ]; do
    rm -f "${P:?}/${rel:?}"
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
# copy_tree <source tree> <folder>: a copy to build in, writable by its owner whatever the source tree's modes (cp keeps
# them: a read-only tree would give a copy the build cannot write into, and one the next build could not remove)
copy_tree() { x cp -RP "$1" "$2"; x chmod -R u+w "$2"; }
# Each recipe builds in $B (an empty folder in the work folder) and installs into the prefix. What each project could
# find on the machine by itself is either given or turned off, and the comments say which and why.

# ninja 1.13.2: its own bootstrap (a Python script and the C++ compiler), run in a copy of the tree as on the Mac (the
# bootstrap writes its objects and the program next to configure.py). It reads CXX, CFLAGS, CXXFLAGS and LDFLAGS from
# the environment, which the clean environment does not have.
recipe_ninja() {
  pick_python ninja; clean_env "$PYDIR"
  copy_tree "$SRC/ninja" "$B/ninja"
  xin "$B/ninja" "${CLEAN[@]}" "$PY" configure.py --bootstrap
  x mkdir -p "$P/bin"
  x cp "$B/ninja/ninja" "$P/bin/ninja"
  x chmod 755 "$P/bin/ninja"
}
check_ninja() { need_file bin/ninja; answers_pin ninja bin/ninja --version; }

# cmake 4.4.5: its bootstrap and make, in a copy of the tree, as on the Mac. The bootstrap takes the system's curl, zlib
# and iconv on macOS (it links /usr/lib/libcurl.4.dylib, libz and libiconv: the system's own, which the checks accept)
# and CMake's bundled copies of the rest. No Qt dialog (it would look for Qt), no tests. The Mac also passed
# CMAKE_USE_OPENSSL=ON with its own OpenSSL, which macOS's curl does not use; here OpenSSL is built after cmake, so
# CMake's default on macOS (off) stays. Off a Mac, the bundled curl gets no TLS library (CMAKE_USE_OPENSSL=OFF), so
# that a trial on Linux needs no OpenSSL headers.
recipe_cmake() {
  local tls=()
  is_darwin || tls=(-DCMAKE_USE_OPENSSL=OFF)
  clean_env; cmake_ignore
  copy_tree "$SRC/cmake" "$B/cmake"
  xin "$B/cmake" "${CLEAN[@]}" ./bootstrap "--prefix=$P" "--parallel=$JOBS" --no-qt-gui \
    -- "${CMAKE_COMMON[@]}" -DBUILD_TESTING=OFF ${tls[@]+"${tls[@]}"}
  xin "$B/cmake" "${CLEAN[@]}" make -j "$JOBS"
  xin "$B/cmake" "${CLEAN[@]}" make install
}
check_cmake() {
  need_file bin/cmake; need_file bin/ctest; need_file bin/cpack; need_glob share 'cmake-*'
  answers_pin cmake bin/cmake --version; answers bin/ctest --version; answers bin/cpack --version
}

# OpenSSL 3.5.9, static, only so that Python has ssl and hashlib (pip and meson's downloads need them). In a copy of the
# tree, as on the Mac. The Configure target is this Mac's processor (darwin64-x86_64-cc or darwin64-arm64-cc). Turned
# off: shared libraries and the tests; install_sw installs no documentation. zlib, brotli and zstd compression and kTLS
# are off by default.
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
  copy_tree "$SRC/openssl" "$B/openssl"
  xin "$B/openssl" "${CLEAN[@]}" perl ./Configure ${target[@]+"${target[@]}"} no-shared no-tests \
    "--prefix=$P/build-deps" "--openssldir=$P/ssl" --libdir=lib
  xin "$B/openssl" "${CLEAN[@]}" make -j "$JOBS"
  xin "$B/openssl" "${CLEAN[@]}" make install_sw
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

# zstd 1.5.7, static, only for Python's compression.zstd (a test uses it where present). Its own Makefile in lib/, in a
# copy of the tree, as on the Mac: the static library and the headers only (no programs, which would look for zlib, lzma
# and lz4).
recipe_zstd() {
  clean_env
  copy_tree "$SRC/zstd" "$B/zstd"
  xin "$B/zstd" "${CLEAN[@]}" make -C lib -j "$JOBS" libzstd.a
  xin "$B/zstd" "${CLEAN[@]}" make -C lib install-static install-includes "PREFIX=$P/build-deps"
}
check_zstd() { need_file build-deps/lib/libzstd.a; need_file build-deps/include/zstd.h; }

# xz 5.8.4, static liblzma only, for Python's lzma (tools/test_igfx_trace.py imports it; the macOS SDK has no lzma.h).
# No translations (they would look for libintl), none of the programs or scripts, no documentation, no tests.
recipe_xz() {
  cmake_project "$SRC/xz" "$P/build-deps" \
    -DBUILD_SHARED_LIBS=OFF -DBUILD_TESTING=OFF -DXZ_NLS=OFF -DXZ_TOOL_XZ=OFF -DXZ_TOOL_XZDEC=OFF -DXZ_TOOL_LZMADEC=OFF \
    -DXZ_TOOL_LZMAINFO=OFF -DXZ_TOOL_SCRIPTS=OFF -DXZ_DOC=OFF
}
check_xz() { need_file build-deps/lib/liblzma.a; need_file build-deps/include/lzma.h; }

# Python 3.14.10 into $P/python. CPython's build imports modules from its source tree with -E (bytecode would be
# written there), so it is built in a copy of the tree in the work folder. Static libpython, no framework.
# The Mac's configure line. Given: OpenSSL, zstd and liblzma from build-deps (static, so nothing outside the toolchain
# is loaded at run time). From the SDK (with SDKROOT set, the compiler sees nothing in /usr/local): zlib, bz2, sqlite3,
# libffi, uuid, readline (macOS's libedit), curses and dbm. Bundled: expat and mpdecimal. tkinter and gdbm are not built
# (the SDK has neither Tcl/Tk headers nor gdbm), which nobody here needs. pip is installed from the wheel inside the
# source.
recipe_python() {
  local bd=$P/build-deps f
  for f in lib/libssl.a lib/libzstd.a lib/liblzma.a include/zstd.h include/lzma.h; do
    [ -f "$bd/$f" ] || die "python is built with OpenSSL, zstd and xz from $bd, and $bd/$f is missing: build openssl, zstd and xz first"
  done
  rm -rf "${P:?}/python"
  clean_env
  copy_tree "$SRC/python" "$B/cpython"
  xin "$B/cpython" "${CLEAN[@]}" ./configure "--prefix=$P/python" "--with-openssl=$bd" --with-ensurepip=install \
    "LIBZSTD_CFLAGS=-I$bd/include" "LIBZSTD_LIBS=$bd/lib/libzstd.a" "LIBLZMA_CFLAGS=-I$bd/include" "LIBLZMA_LIBS=$bd/lib/liblzma.a"
  xin "$B/cpython" "${CLEAN[@]}" make -j "$JOBS"
  xin "$B/cpython" "${CLEAN[@]}" make install
}
check_python() {
  local py=python/bin/python3.14 out
  need_file "$py"; [ -e "$P/$py" ] || return 0
  answers_pin python "$py" --version
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
  local v out d
  need_file bin/pkgconf; need_file bin/pkg-config; [ -e "$P/bin/pkgconf" ] || return 0
  answers_pin pkgconf bin/pkgconf --version; answers_pin pkgconf bin/pkg-config --version
  # the folders whose -I and -L pkgconf leaves out of its answers must not be the toolchain's own
  for v in pc_system_includedirs pc_system_libdirs; do
    out=$(cd / && env -i "$P/bin/pkgconf" --variable "$v" pkg-config 2>&1) || { bad "pkgconf --variable $v pkg-config fails: $out"; continue; }
    [ -n "$out" ] || { bad "pkgconf --variable $v pkg-config prints nothing"; continue; }
    for d in $(printf '%s\n' "$out" | tr ':' ' '); do
      case "$d/" in "$P/"*) bad "pkgconf's $v holds $d, inside the toolchain: it would leave the toolchain's own -I and -L out of every answer" ;; esac
    done
    cnote "pkgconf $v: $out"
  done
}

# SPIRV-Headers at the commit SPIRV-Tools' DEPS names, installed into the prefix, then SPIRV-Tools with its default
# libraries, as on the Mac: static libraries (SPIRV-Tools.pc names them, so Mesa's mesa_clc links them in) and the one
# shared libSPIRV-Tools-shared, with an absolute install name. No tests (they would download googletest, effcee and
# re2), no warnings as errors (a newer clang than theirs adds some).
recipe_spirv_tools() {
  pick_python spirv-tools
  [ -f "$SRC/spirv-headers/include/spirv/unified1/spirv.h" ] || die "spirv-tools needs SPIRV-Headers in $SRC/spirv-headers"
  B_SUB=headers
  cmake_project "$SRC/spirv-headers" "$P" -DSPIRV_HEADERS_ENABLE_TESTS=OFF -DSPIRV_HEADERS_ENABLE_INSTALL=ON
  B_SUB=tools
  cmake_project "$SRC/spirv-tools" "$P" "-DSPIRV-Headers_SOURCE_DIR=$SRC/spirv-headers" -DSPIRV_SKIP_TESTS=ON \
    -DSPIRV_WERROR=OFF "-DPython3_EXECUTABLE=$PY"
}
check_spirv_tools() {
  local p
  need_file lib/pkgconfig/SPIRV-Tools.pc; need_file include/spirv-tools/libspirv.h; need_file include/spirv/unified1/spirv.h
  grep -q '^Version: *[0-9]' "$P/lib/pkgconfig/SPIRV-Tools.pc" 2>/dev/null || bad "lib/pkgconfig/SPIRV-Tools.pc has no Version line"
  for p in spirv-as spirv-dis spirv-val spirv-opt spirv-link; do need_file "bin/$p"; [ ! -e "$P/bin/$p" ] || answers "bin/$p" --version; done
  # what Mesa's build asks: the toolchain's pkg-config, from an empty environment, must give the toolchain's folders
  if [ -x "$P/bin/pkg-config" ] && [ -f "$P/lib/pkgconfig/SPIRV-Tools.pc" ]; then
    pc_answer "--cflags SPIRV-Tools" "-I$P/include"
    pc_answer "--libs SPIRV-Tools" "-L$P/lib -lSPIRV-Tools-opt -lSPIRV-Tools -lSPIRV-Tools-link"
  else
    cnote "pkg-config --cflags SPIRV-Tools not asked: the toolchain has no pkg-config"
  fi
}
pc_answer() {   # <arguments> <expected answer>: what the toolchain's pkg-config prints, blanks folded
  local out
  # shellcheck disable=SC2086
  out=$(cd / && env -i "$P/bin/pkg-config" $1 2>&1) || { bad "pkg-config $1 fails: $out"; return 0; }
  out=$(printf '%s\n' "$out" | tr -s ' \t\n' '   ' | sed 's/^ //; s/ $//')
  if [ "$out" = "$2" ]; then cnote "pkg-config $1: $out"; else bad "pkg-config $1 prints \"$out\", not \"$2\""; fi
}

# glslang 16.6.0 against the SPIRV-Tools above (ALLOW_EXTERNAL_SPIRV_TOOLS, ENABLE_OPT), as on the Mac; the exported
# tree has no External/spirv-tools, so nothing else is found. Static libraries: only the program is used. No tests.
recipe_glslang() {
  pick_python glslang
  [ -f "$P/lib/pkgconfig/SPIRV-Tools.pc" ] || die "glslang is built against the toolchain's SPIRV-Tools: build spirv-tools first"
  cmake_project "$SRC/glslang" "$P" -DALLOW_EXTERNAL_SPIRV_TOOLS=ON -DENABLE_OPT=ON -DGLSLANG_TESTS=OFF \
    "-DPython3_EXECUTABLE=$PY"
  [ -e "$P/bin/glslangValidator" ] || x ln -s glslang "$P/bin/glslangValidator"
}
check_glslang() {
  local first
  need_file bin/glslang; need_file bin/glslangValidator; [ -e "$P/bin/glslangValidator" ] || return 0
  answers_pin glslang bin/glslangValidator --version
  first=$(cd / && env -i "$P/bin/glslangValidator" --version 2>/dev/null | sed -n 1p) || true   # answers_pin counted a failure
  case "$first" in "Glslang Version: "*:*) ;; *) bad "glslangValidator --version starts with \"$first\", not \"Glslang Version: N:x.y.z\" (build.sh reads it)" ;; esac
}

# LLVM 22.1.8 and clang into $P/llvm. Shared libLLVM and libclang-cpp (build.sh builds the SPIRV-LLVM-Translator as a
# shared library against this LLVM and Mesa's mesa_clc links both: with a static LLVM there would be two copies in one
# program), and libclang for bindgen. Absolute install names (CMAKE_INSTALL_NAME_DIR; LLVM leaves @rpath out once
# CMAKE_INSTALL_RPATH is set): the Translator and mesa_clc are built by other build systems that add no rpath.
# Targets: this Mac's own (host: mesa_clc only turns OpenCL C into SPIR-V and bindgen parses headers for this Mac; on
# an Intel Mac host is X86, the target the Mac built with) and SPIRV (Mesa compiles for spirv64-unknown-unknown).
# "host" instead of X86 so that the same line serves Apple silicon. RTTI and exceptions on, as on the Mac whose NVK
# build proved this LLVM with build.sh's Translator and Mesa's mesa_clc (Homebrew's LLVM has exceptions off).
# Turned off, each would be looked up on the machine: zstd, libxml2, libedit, libffi, z3, libcurl, cpp-httplib, libpfm;
# zlib comes from the SDK. No static analyzer, tests, examples, benchmarks, documentation or language bindings.
recipe_llvm() {
  pick_python llvm
  rm -rf "${P:?}/llvm"
  cmake_project "$SRC/llvm/llvm" "$P/llvm" \
    -DLLVM_ENABLE_PROJECTS=clang "-DLLVM_TARGETS_TO_BUILD=host;SPIRV" \
    -DLLVM_BUILD_LLVM_DYLIB=ON -DLLVM_LINK_LLVM_DYLIB=ON -DCLANG_LINK_CLANG_DYLIB=ON -DLLVM_ENABLE_RTTI=ON -DLLVM_ENABLE_EH=ON \
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
  answers_pin llvm llvm/bin/llvm-config --version; answers llvm/bin/clang --version
  major=$(cd / && env -i "$P/llvm/bin/llvm-config" --version 2>/dev/null | sed 's/\..*//') || true   # answers_pin counted a failure
  need_file "llvm/lib/clang/$major/include/opencl-c.h"; need_file "llvm/lib/clang/$major/include/opencl-c-base.h"
  [ "$(cd / && env -i "$P/llvm/bin/llvm-config" --shared-mode 2>/dev/null)" = shared ] || bad "llvm-config --shared-mode does not say shared"
  comps=" $(cd / && env -i "$P/llvm/bin/llvm-config" --components 2>/dev/null | tr '\n' ' ') " || true   # the list below says what lacks
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
# answers_pin <tool> <program, relative> [arguments]: it answers, and its first line holds the pinned version of <tool>
# as a whole word (1.13.20 or 21.13.2 is not 1.13.2)
answers_pin() {
  local t=$1 p=$2 want out rc=0 first; shift 2
  want=$(pinned_version "$t")
  out=$(cd / && env -i "$P/$p" "$@" 2>&1) || rc=$?
  if [ "$rc" != 0 ] || [ -z "$out" ]; then bad "$p $* does not answer from an empty environment (exit $rc): $(printf '%s' "$out" | head -n 2 | tr '\n' ' ')"; return 0; fi
  first=$(printf '%s\n' "$out" | sed -n 1p)
  case " $first " in
    *[!0-9.]"$want"[!0-9.]*) cnote "$p $*: $first" ;;
    *) bad "$p $* prints \"$first\", not $t $want, the version this script builds: is NM_TC_SRC/$t the pinned source tree?" ;;
  esac
}
# On macOS, every Mach-O file the tool installed may name only the system's libraries and the toolchain's own: a
# library of a Homebrew (or of any other folder) that a build picked up would tie the toolchain to it. And each must run
# on the oldest macOS asked for: its minimum version (LC_BUILD_VERSION's minos, or LC_VERSION_MIN_MACOSX) not above it.
# A static library is judged by its first member that has one (what a program linked from it would be told by ld).
macho_minos() {   # <file>: the first minimum macOS version otool -l shows, or nothing
  # awk stops at the first answer; otool, writing an archive's other members into a closed pipe, is stopped then too,
  # which is not a failure
  { otool -l "$1" 2>/dev/null || true; } | awk '
    $1 == "cmd" { c = $2 }
    c == "LC_BUILD_VERSION" && $1 == "minos" { print $2; exit }
    c == "LC_VERSION_MIN_MACOSX" && $1 == "version" { print $2; exit }'
}
version_above() {   # <a> <b>: a is a later version than b
  awk -v a="$1" -v b="$2" 'BEGIN { n = split(a, x, "."); m = split(b, y, "."); if (m > n) n = m
    for (i = 1; i <= n; i++) { if ((x[i] + 0) > (y[i] + 0)) exit 0; if ((x[i] + 0) < (y[i] + 0)) exit 1 } exit 1 }'
}
macho_files() {   # <record file>: "m <file>" for each Mach-O file, "a <file>" for each static library it lists
  perl -e '
    my $root = shift;
    while (my $rel = <STDIN>) {
      chomp $rel; my $f = "$root/$rel";
      next if -l $f || ! -f $f;
      open(my $h, "<", $f) or next; binmode $h; my $m = ""; read($h, $m, 8); close $h;
      if (substr($m, 0, 4) =~ /^(\xfe\xed\xfa[\xce\xcf]|[\xce\xcf]\xfa\xed\xfe|\xca\xfe\xba[\xbe\xbf])$/) { print "m $rel\n" }
      elsif ($m eq "!<arch>\n") { print "a $rel\n" }
    }' "$P" < "$1"
}
# A link the tool installed is not a Mach-O file and is skipped above, but whatever loads or runs it reaches its target:
# links_out lists "<file> <target>" for each link of the record whose target, after every link on the way, lies
# outside the prefix, /usr/lib and /System/Library (a link that leads nowhere is judged by where it points)
links_out() {   # <record file>
  perl -e '
    use Cwd qw(abs_path);
    my $root = shift;
    sub lexical { my @o; for my $c (split m{/}, $_[0]) { next if $c eq "" || $c eq "."; if ($c eq "..") { pop @o } else { push @o, $c } } return "/" . join("/", @o) }
    while (my $rel = <STDIN>) {
      chomp $rel; my $f = "$root/$rel";
      next unless -l $f;
      my ($p, $n) = ($f, 0);
      while (-l $p && $n++ < 40) {
        my $t = readlink $p; last unless defined $t;
        (my $d = $p) =~ s{/[^/]*$}{};
        $p = lexical($t =~ m{^/} ? $t : "$d/$t");
      }
      my $r = (-e $p && abs_path($p)) || $p;
      next if index("$r/", "$root/") == 0 || index($r, "/usr/lib/") == 0 || index($r, "/System/Library/") == 0;
      print "$rel $r\n";
    }' "$P" < "$1"
}
check_libraries() {   # <tool>
  local rec=$P/.nm-toolchain/$1.files kind rel lib n=0 na=0 mo to
  is_darwin || return 0
  [ -f "$rec" ] || { bad "no record of the files $1 installed (build it with this script)"; return 0; }
  while read -r rel to; do
    bad "$P/$rel is a link to $to, which is neither the system's nor the toolchain's"
  done < <(links_out "$rec")
  while read -r kind rel; do
    mo=$(macho_minos "$P/$rel")
    if [ "$kind" = a ]; then
      # a static library names no libraries; one whose members carry no version (assembler output) is only noted
      na=$((na + 1))
      if [ -z "$mo" ]; then cnote "$P/$rel: otool -l shows no minimum macOS version in its members"
      elif version_above "$mo" "$DT"; then bad "$P/$rel is built for macOS $mo and later, above MACOSX_DEPLOYMENT_TARGET $DT"; fi
      continue
    fi
    n=$((n + 1))
    while IFS= read -r lib; do
      case "$lib" in /usr/lib/*|/System/Library/*|"$P"/*) ;; *) bad "$P/$rel links $lib, which is neither the system's nor the toolchain's" ;; esac
    done < <(otool -L "$P/$rel" 2>/dev/null | sed -n 's/^[[:space:]][[:space:]]*\([^ ][^ ]*\) (.*/\1/p')
    if [ -z "$mo" ]; then bad "$P/$rel: otool -l shows no minimum macOS version"
    elif version_above "$mo" "$DT"; then bad "$P/$rel is built for macOS $mo and later, above MACOSX_DEPLOYMENT_TARGET $DT"; fi
  done < <(macho_files "$rec")
  cnote "$n Mach-O files of $1 name only /usr/lib, /System/Library and $P, and they and its $na static libraries run on macOS $DT; its links lead nowhere else"
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
  rm -f "$LOG"; : > "$LOG"
  logline "build/toolchain.sh build $t: $(tool_id "$t"), $(date -u +%Y-%m-%dT%H:%M:%SZ), $(uname -s) $(uname -m), $JOBS jobs"
  logline "prefix $P, sources $SRC/$t, build folder $B${SDK:+, SDK $SDK}"
  [ -d "$SRC/$t" ] || die "$t: its source tree $SRC/$t is missing (build.sh fetches it)"
  say "toolchain: building $t (log: $LOG)"
  t0=$(date +%s)
  forget_tool_files "$t"
  rm -rf "${B:?}"; mkdir -p "$B"
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
  if [ "${NM_TC_KEEP:-0}" = 1 ]; then logline "build folder kept: $B"; else rm -rf "${B:?}"; fi
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
