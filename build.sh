#!/bin/bash
# build.sh: build the whole driver on this Mac with one command.
#
#   ./build.sh [options] [command] [arguments]
#
# Commands (none = all):
#   all            examine the Mac, install what is missing (after asking), fetch the pinned sources, build every
#                  component in order, stage a package (when a base package is given), print a summary
#   doctor         examine only: what is present, missing or too old; changes nothing; exit 1 if something required is missing
#   deps           install what is missing (Homebrew formulas, a Python venv, Rust), all under the working directory
#                  except the Homebrew formulas
#   fetch          download the pinned sources and verify them (commit ids and SHA-256 from build/pins.sh)
#   build [component...]   build the named components (and what they need), or all of them; "./build.sh help" lists them
#   package        stage a package directory that package/install.sh accepts (needs a release package as the base)
#   test           the host tests (tools/run_all_tests.py) and the pin check
#   env            print export lines for every variable, to run one of the build/ scripts by hand the same way
#   clean [outputs|sources|tools|all]   delete inside the working directory only (default: outputs)
#   help           this text and the options
#
# Options (each also an environment variable and a line of the configuration file; see build/build.conf.example):
#   --workdir DIR (NM_WORK)  -j N (NM_JOBS)  -y|--yes (NM_ASSUME_YES)  --no-install (NM_NO_INSTALL)  --offline (NM_OFFLINE)
#   -n|--dry-run (NM_DRY_RUN)  --flavour release|dev (NM_FLAVOUR)  --accel 15|26|both (NM_ACCEL)  -v|--verbose (NM_VERBOSE)
#   --base-package FILE (NM_BASE_PACKAGE)  --force (NM_FORCE)  --config FILE (NM_CONFIG)
# docs/BUILDING.md explains everything; docs/BUILDING.es.md has the quick start in Spanish.
#
# Written for the /bin/bash 3.2 of macOS and its BSD tools, and runs on Linux for doctor, fetch, test, env and --dry-run.
# It never calls sudo and never installs the driver.
set -Eeu -o pipefail
umask 022

case "$0" in */*) NM_HERE_DIR=${0%/*} ;; *) NM_HERE_DIR=. ;; esac
HERE=$(cd "$NM_HERE_DIR" && pwd -P)
SELF="$HERE/build.sh"
PINS_FILE="$HERE/build/pins.sh"
MARKER=.nullmoth-build-workdir
LOCAL_HOST=nullmoth-local-build

# ---------------------------------------------------------------------------------------------------------- messages
say()  { printf '%s\n' "$*"; }
note() { printf 'note: %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'STOP: %s\n' "$*" >&2; exit 2; }
hr()   { say "---------------------------------------------------------------------------------------------------"; }
q() {   # a value quoted for a shell
  case "$1" in
    '') printf "''" ;;
    *[!A-Za-z0-9_./:=@%+,-]*) printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")" ;;
    *) printf '%s' "$1" ;;
  esac
}
qcmd() { local a out=""; for a in "$@"; do out="$out $(q "$a")"; done; printf '%s\n' "${out# }"; }
tilde() {   # the home folder written as ~ (report files and summaries a tester may post)
  # both spellings: $HOME as set, and the same folder with its symbolic links resolved, which is how the working
  # directory is stored (a home under /var or /tmp is /private/var or /private/tmp once resolved)
  local ph=""
  if [ -n "${HOME:-}" ] && [ "$HOME" != / ]; then
    ph=$(cd "$HOME" 2>/dev/null && pwd -P) || ph=""
    awk -v h="$HOME" -v p="$ph" 'function rep(s, x,   i, o) { o = ""; while ((i = index(s, x)) > 0) { o = o substr(s, 1, i - 1) "~"; s = substr(s, i + length(x)) } return o s }
      { if (p != "" && p != "/" && p != h) $0 = rep($0, p); print rep($0, h) }'
  else cat; fi
}

# ---------------------------------------------------------------------------------------------------------- small tools
is_mac() { [ "$(uname -s)" = Darwin ]; }
# Xcode 26 (the macOS 26 SDK the plugin needs) does not run on a macOS older than NM_PIN_TOOL_MACOS_MIN
mac_too_old() { is_mac && ! ver_ge "$(sw_vers -productVersion 2>/dev/null || echo 0)" "$NM_PIN_TOOL_MACOS_MIN"; }
have() { command -v "$1" >/dev/null 2>&1; }
sha256_file() { shasum -a 256 "$1" | cut -c1-64; }   # shasum: macOS has it; on Linux it comes with perl
sha256_stdin() { shasum -a 256 | cut -c1-64; }
ncpu() { sysctl -n hw.ncpu 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2; }
first_line() { sed -n 1p; }
# ver_ge A B: A >= B for dotted versions (digits only per field; text after the digits of a field is ignored)
ver_ge() {
  local a=$1 b=$2 x y
  while [ -n "$a" ] || [ -n "$b" ]; do
    x=${a%%.*}; y=${b%%.*}
    x=$(printf '%s' "$x" | sed 's/[^0-9].*//'); y=$(printf '%s' "$y" | sed 's/[^0-9].*//')
    x=${x:-0}; y=${y:-0}
    if [ "$((10#$x))" -gt "$((10#$y))" ]; then return 0; fi
    if [ "$((10#$x))" -lt "$((10#$y))" ]; then return 1; fi
    case "$a" in *.*) a=${a#*.} ;; *) a= ;; esac
    case "$b" in *.*) b=${b#*.} ;; *) b= ;; esac
  done
  return 0
}
version_of() {   # first dotted number in a text
  sed -n 's/[^0-9]*\([0-9][0-9]*\(\.[0-9][0-9]*\)*\).*/\1/p' | first_line
}

# ---------------------------------------------------------------------------------------------------------- pins
load_pins() {
  [ -f "$PINS_FILE" ] || die "build/pins.sh is missing"
  local line name value n=0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    case "$line" in
      NM_PIN_[A-Z0-9_]*=*) ;;
      *) die "build/pins.sh: only NM_PIN_*=value lines and comments are allowed here: $line" ;;
    esac
    name=${line%%=*}; value=${line#*=}
    case "$value" in \"*\") value=${value#\"}; value=${value%\"} ;; esac
    eval "$name=\$value"
    n=$((n + 1))
  done < "$PINS_FILE"
  [ "$n" -gt 20 ] || die "build/pins.sh holds only $n pins"
}
pin() { eval "printf '%s' \"\${NM_PIN_$1:-}\""; }

# The pins must say what the scripts that use the same source say; a difference stops the build.
PIN_PROBLEMS=0
pin_agree() {   # <what> <pin value> <file> <fixed text that must occur in it>
  if [ ! -f "$HERE/$3" ]; then say "  pin FAIL  $1: $3 is missing"; PIN_PROBLEMS=$((PIN_PROBLEMS + 1)); return; fi
  if grep -qF -- "$4" "$HERE/$3"; then say "  pin ok    $1 = $2 ($3)"
  else say "  pin FAIL  $1 = $2, but $3 does not contain: $4"; PIN_PROBLEMS=$((PIN_PROBLEMS + 1)); fi
}
check_pins() {
  PIN_PROBLEMS=0
  local base
  pin_agree "open-gpu-kernel-modules (kexts)" "$NM_PIN_OGKM620_REF" build/build_rm620.sh "V=\${NV_VERSION:-$NM_PIN_OGKM620_REF}"
  pin_agree "open-gpu-kernel-modules (kexts)" "$NM_PIN_OGKM620_REF" build/build_kms620.sh "V=\${NV_VERSION:-$NM_PIN_OGKM620_REF}"
  pin_agree "open-gpu-kernel-modules (NVK, libnvencvt)" "$NM_PIN_OGKM610_REF" build/build_nvencvt.sh "open-gpu-kernel-modules.git $NM_PIN_OGKM610_REF "
  base=$(tr -d ' \n' < "$HERE/nvk/MESA_BASE" 2>/dev/null || true)
  case "$NM_PIN_MESA_COMMIT" in
    "$base"*) if [ -n "$base" ]; then say "  pin ok    Mesa = $NM_PIN_MESA_COMMIT (nvk/MESA_BASE $base)"; else say "  pin FAIL  nvk/MESA_BASE is empty"; PIN_PROBLEMS=$((PIN_PROBLEMS + 1)); fi ;;
    *) say "  pin FAIL  Mesa = $NM_PIN_MESA_COMMIT, but nvk/MESA_BASE says $base"; PIN_PROBLEMS=$((PIN_PROBLEMS + 1)) ;;
  esac
  pin_agree "Vulkan loader tag" "$NM_PIN_VKLOADER_REF" build/build_vulkan_loader.sh "TAG=$NM_PIN_VKLOADER_REF"
  pin_agree "Vulkan-Headers tag" "$NM_PIN_VKHEADERS_REF" build/build_vulkan_loader.sh "TAG=$NM_PIN_VKHEADERS_REF"
  pin_agree "Vulkan-Loader commit" "$NM_PIN_VKLOADER_COMMIT" build/build_vulkan_loader.sh "LOADER_SHA=$NM_PIN_VKLOADER_COMMIT"
  pin_agree "Vulkan-Headers commit" "$NM_PIN_VKHEADERS_COMMIT" build/build_vulkan_loader.sh "HEADERS_SHA=$NM_PIN_VKHEADERS_COMMIT"
  pin_agree "open-gpu-doc" "$NM_PIN_OPENGPUDOC_COMMIT" build/build_nvencvt.sh "open-gpu-doc.git $NM_PIN_OPENGPUDOC_COMMIT "
  pin_agree "WebKit SPI headers" "$NM_PIN_WEBKIT_COMMIT" build/build_nvencvt.sh "WebKit/WebKit/$NM_PIN_WEBKIT_COMMIT/"
  case "$NM_PIN_WEBKIT_CMBASE_URL$NM_PIN_WEBKIT_VTDEC_URL" in
    *"$NM_PIN_WEBKIT_COMMIT"*"$NM_PIN_WEBKIT_COMMIT"*) ;;
    *) say "  pin FAIL  the WebKit header URLs do not name commit $NM_PIN_WEBKIT_COMMIT"; PIN_PROBLEMS=$((PIN_PROBLEMS + 1)) ;;
  esac
  pin_agree "RyzenAdj tag" "$NM_PIN_RYZENADJ_REF" tools/ryzenadj-macos/build.sh "TAG=$NM_PIN_RYZENADJ_REF"
  pin_agree "RyzenAdj commit" "$NM_PIN_RYZENADJ_COMMIT" tools/ryzenadj-macos/build.sh "COMMIT=$NM_PIN_RYZENADJ_COMMIT"
  pin_agree "Rust (translator)" "$NM_PIN_RUST_VERSION" build/build_xlate_release.sh "RV=\${NULLMOTH_RUST_VERSION:-$NM_PIN_RUST_VERSION}"
  return 0
}

# ---------------------------------------------------------------------------------------------------------- settings
# Every setting: name, default, meaning. Precedence: built-in default < configuration file < environment < option.
OPTION_VARS="NM_WORK NM_JOBS NM_ASSUME_YES NM_NO_INSTALL NM_OFFLINE NM_DRY_RUN NM_FLAVOUR NM_ACCEL NM_VERBOSE NM_FORCE
NM_BASE_PACKAGE NM_BASE_PACKAGE_SHA256 NM_PACKAGE_VERSION"
# where outside things come from
LOCATION_VARS="NM_OGKM_URL NM_MESA_URL NM_KHRONOS_URL NM_OPEN_GPU_DOC_URL NM_RYZENADJ_URL NM_WEBKIT_SPI_URL NM_RUSTUP_URL
RUSTUP_DIST_SERVER DEVELOPER_DIR NM_BREW NM_PYTHON NM_LLVM_PREFIX NM_OGKM620_PRISTINE NM_OGKM620 NM_OGKM610 NM_MESA_GIT
NM_VULKAN_INCLUDE NM_LILU_DEBUG_KEXT NM_VIRTUALSMC_SRC NM_MACKERNELSDK NULLMOTH_BUILD_ROOT NULLMOTH_BUILD_HOST NM_LOCAL_HOST_PATH"
# read by the existing build scripts and passed through when set (their defaults are the scripts' own)
SCRIPT_VARS="NV_VERSION KMS_ARCHIVE RE MINVER FB_DEFS KHDR KLIB MACOSX_DEPLOYMENT_TARGET NVK_PATH NVK_SYSROOT NVK_JOBS
NVK_EXTRA_PATCHES NVK_SKIP_PATCHES NVK_REF_EXPORTS PKG_CONFIG_PATH LIBCLANG_PATH JOBS
NULLMOTH_RUST_VERSION NULLMOTH_RUST_BIN NULLMOTH_CARGO_HOME"
ALL_VARS="$OPTION_VARS $LOCATION_VARS $SCRIPT_VARS NM_CONFIG"
known_var() { case " $(printf '%s' "$ALL_VARS" | tr '\n' ' ') " in *" $1 "*) return 0 ;; esac; return 1; }

ENV_SET=" "
remember_environment() { local v; for v in $ALL_VARS; do eval "[ -n \"\${$v+x}\" ]" && ENV_SET="$ENV_SET$v " || true; done; }
from_env() { case "$ENV_SET" in *" $1 "*) return 0 ;; esac; return 1; }

# absolute_path <path>: no "." or ".." parts and no symbolic links in the part that exists (macOS's readlink cannot do it)
absolute_path() {
  local p=$1 rest="" d
  case "$p" in /*) ;; *) p=$PWD/$p ;; esac
  while [ ! -d "$p" ]; do
    case "$p" in */*) ;; *) break ;; esac
    rest=${p##*/}${rest:+/$rest}; p=${p%/*}; [ -n "$p" ] || p=/
  done
  d=$(cd "$p" 2>/dev/null && pwd -P) || d=$p
  case "/$rest/" in */./*|*/../*) printf '%s' "$1"; return 0 ;; esac   # left for check_paths to refuse
  d=${d%/}; printf '%s' "$d${rest:+/$rest}"
}
# shellcheck disable=SC2088
expand_tilde() { case "$1" in "~") printf '%s' "$HOME" ;; "~/"*) printf '%s/%s' "$HOME" "${1#\~/}" ;; *) printf '%s' "$1" ;; esac; }

load_config() {   # NAME=value lines; read, never run; unknown names stop
  local f=$1 line name value n=0
  [ -f "$f" ] || die "configuration file $f does not exist"
  while IFS= read -r line || [ -n "$line" ]; do
    n=$((n + 1))
    line=$(printf '%s' "$line" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
    case "$line" in ''|'#'*) continue ;; esac
    case "$line" in export\ *) line=${line#export } ;; esac
    case "$line" in [A-Za-z_]*=*) ;; *) die "$f:$n: expected NAME=value, got: $line" ;; esac
    name=${line%%=*}; value=${line#*=}
    case "$name" in *[!A-Za-z0-9_]*) die "$f:$n: bad name $name" ;; esac
    known_var "$name" || die "$f:$n: unknown setting $name (\"./build.sh help\" lists them)"
    case "$value" in \"*\") value=${value#\"}; value=${value%\"} ;; \'*\') value=${value#\'}; value=${value%\'} ;; esac
    value=$(expand_tilde "$value")
    if from_env "$name"; then continue; fi   # the environment wins over the file
    eval "$name=\$value"
  done < "$f"
}

set_defaults() {
  : "${NM_WORK:=$HOME/nullmoth-build}"
  NM_WORK=$(expand_tilde "$NM_WORK")
  NM_WORK=$(absolute_path "$NM_WORK")
  NM_WORK=${NM_WORK%/}
  : "${NM_JOBS:=$(ncpu)}"
  : "${NM_ASSUME_YES:=0}" "${NM_NO_INSTALL:=0}" "${NM_OFFLINE:=0}" "${NM_DRY_RUN:=0}" "${NM_VERBOSE:=0}" "${NM_FORCE:=0}"
  : "${NM_FLAVOUR:=release}" "${NM_ACCEL:=both}"
  case "$NM_JOBS" in ''|*[!0-9]*|0) die "the job count must be a positive number (got '$NM_JOBS')" ;; esac
  case "$NM_FLAVOUR" in release|dev) ;; *) die "--flavour must be release or dev (got '$NM_FLAVOUR')" ;; esac
  case "$NM_ACCEL" in 15|26|both) ;; *) die "--accel must be 15, 26 or both (got '$NM_ACCEL')" ;; esac
  local b; for b in NM_ASSUME_YES NM_NO_INSTALL NM_OFFLINE NM_DRY_RUN NM_VERBOSE NM_FORCE; do
    eval "case \"\$$b\" in 0|1) ;; yes|true) $b=1 ;; no|false|'') $b=0 ;; *) die \"$b must be 0 or 1\" ;; esac"
  done
  if [ -n "${NULLMOTH_RUST_VERSION:-}" ] && [ "$NULLMOTH_RUST_VERSION" != "$NM_PIN_RUST_VERSION" ]; then
    warn "NULLMOTH_RUST_VERSION=$NULLMOTH_RUST_VERSION is not the pinned $NM_PIN_RUST_VERSION: the translator will not be the release's"
  fi
  if [ -n "${NV_VERSION:-}" ] && [ "$NV_VERSION" != "$NM_PIN_OGKM620_REF" ]; then
    die "NV_VERSION=$NV_VERSION, but the pinned NVIDIA tree is $NM_PIN_OGKM620_REF (change build/pins.sh and the build scripts together)"
  fi
}

# Paths everything else uses. NULLMOTH_BUILD_ROOT is the folder the existing scripts default their trees under.
derive_paths() {
  W=$NM_WORK
  R=${NULLMOTH_BUILD_ROOT:-$W/build}
  O=$W/out
  SRC=$W/src
  TOOLS=$W/tools
  LOGS=$W/logs
  STAMPS=$W/stamps
  REPORTS=$W/reports
  PKGDIR=$W/package
  GITDIR=$SRC/git
  PRISTINE=${NM_OGKM620_PRISTINE:-$R/build/ogkm620/open-gpu-kernel-modules-$NM_PIN_OGKM620_REF}
  RMWORK=$R/build/rm620
  OGKM620=${NM_OGKM620:-$RMWORK/ogkm-$NM_PIN_OGKM620_REF}
  OGKM610=${NM_OGKM610:-$R/build/ogkm620/open-gpu-kernel-modules-$NM_PIN_OGKM610_REF}
  MESA=${NM_MESA_GIT:-$R/build/gpu-src/mesa}
  KHRONOS=$SRC/khronos
  RYZENSRC=$SRC/RyzenAdj
  WEBKITSRC=$SRC/webkit-spi
  NVHDR=$R/build/nvencvt-headers
  VENV=$TOOLS/venv
  TBIN=$TOOLS/bin
  THOST=$TOOLS/host
  # always under the working directory: a RUSTUP_HOME or CARGO_HOME from your shell (~/.rustup, ~/.cargo) is not used
  RUSTUP_DIR=$TOOLS/rustup
  CARGO_DIR=$TOOLS/cargo
  BUILD_HOST=${NULLMOTH_BUILD_HOST:-$LOCAL_HOST}
  case "$(uname -m)" in arm64|aarch64) HOST_TRIPLE=aarch64-apple-darwin ;; *) HOST_TRIPLE=x86_64-apple-darwin ;; esac
  RUST_BIN=${NULLMOTH_RUST_BIN:-$RUSTUP_DIR/toolchains/$NM_PIN_RUST_VERSION-$HOST_TRIPLE/bin}
  STEP_PATH="$TBIN:/usr/bin:/bin:/usr/sbin:/sbin"
}

# Refusals that hold for every command that builds or writes (doctor reports them instead of stopping).
PATH_PROBLEMS=""
path_problem() { PATH_PROBLEMS="$PATH_PROBLEMS$1
"; }
check_paths() {
  PATH_PROBLEMS=""
  local ws
  ws=$(printf ' \t')
  case "$HERE" in *[$ws]*) path_problem "the checkout path contains a space or tab ($HERE): the release scripts' commands and build_nvk.sh's compiler flags split on spaces; move the checkout" ;; esac
  case "${HOME:-}" in ''|/) path_problem "HOME is not set to a home folder" ;; *[$ws]*) path_problem "the home folder path contains a space ($HOME): build_nvk.sh and the release scripts put it into space-separated flags" ;; esac
  case "$NM_WORK" in /*) ;; *) path_problem "the working directory must be an absolute path (got $NM_WORK)" ;; esac
  case "$NM_WORK/" in */./*|*/../*) path_problem "the working directory may not contain . or .. parts (got $NM_WORK)" ;; esac
  case "$NM_WORK" in *[$ws]*) path_problem "the working directory contains a space ($NM_WORK): the release scripts' commands split on it" ;; esac
  case "$NM_WORK/" in "$HERE/"*) path_problem "the working directory $NM_WORK is inside the checkout; choose one outside it (default ~/nullmoth-build)" ;; esac
  case "$HERE/" in "$NM_WORK/"*) path_problem "the checkout is inside the working directory $NM_WORK; choose another working directory" ;; esac
  local rh; rh=$(absolute_path "${HOME:-/}")
  case "${NM_WORK%/}" in /|"${HOME%/}"|"${HOME%/}/nvmtl-build"|"$rh"|"$rh/nvmtl-build") path_problem "the working directory may not be / , the home folder or ~/nvmtl-build (the original author's folder); default is ~/nullmoth-build" ;; esac
  if [ -n "${NULLMOTH_BUILD_ROOT:-}" ]; then
    case "$NULLMOTH_BUILD_ROOT" in *[$ws]*) path_problem "NULLMOTH_BUILD_ROOT contains a space" ;; esac
  fi
  git -C "$HERE" rev-parse --is-inside-work-tree >/dev/null 2>&1 || path_problem "$HERE is not a git checkout (the release scripts build from git revisions; clone the repository instead of downloading an archive)"
  return 0
}
require_paths_ok() { check_paths; [ -z "$PATH_PROBLEMS" ] || { printf '%s' "$PATH_PROBLEMS" | sed 's/^/STOP: /' >&2; exit 2; }; }

# ---------------------------------------------------------------------------------------------------------- working dir, lock
in_workdir() { case "$1" in "$W"/?*) return 0 ;; esac; return 1; }
safe_rm() {   # delete only inside the working directory that carries our marker
  local p
  for p in "$@"; do
    [ -n "$p" ] || die "internal: an empty path was given to delete"
    [ -n "$W" ] && [ "$W" != / ] || die "internal: the working directory is empty"
    in_workdir "$p" || die "refusing to delete $p: it is not inside the working directory $W"
    [ -f "$W/$MARKER" ] || die "refusing to delete inside $W: it has no $MARKER marker (not made by build.sh)"
    case "$p" in *"/../"*|*"/.."|*"/./"*) die "refusing to delete $p (relative components)" ;; esac
    if [ "$NM_DRY_RUN" = 1 ]; then say "+ rm -rf $(q "$p")"; else rm -rf "$p"; fi
  done
}
ensure_workdir() {
  [ "$NM_DRY_RUN" = 1 ] && return 0
  if [ -d "$W" ] && [ ! -f "$W/$MARKER" ]; then
    [ -z "$(ls -A "$W" 2>/dev/null)" ] || die "$W exists, is not empty and was not made by build.sh (no $MARKER); choose another --workdir"
  fi
  mkdir -p "$W" "$LOGS" "$STAMPS" "$REPORTS"
  [ -f "$W/$MARKER" ] || printf 'made by build.sh of the NullMoth driver; everything below can be deleted with ./build.sh clean all\n' > "$W/$MARKER"
}
LOCK_HELD=0
take_lock() {
  [ "$NM_DRY_RUN" = 1 ] && return 0
  local l=$W/build.lock pid tries=0
  while ! mkdir "$l" 2>/dev/null; do
    pid=$(sed -n 's/^pid=//p' "$l/owner" 2>/dev/null | first_line)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      die "another build.sh run (pid $pid) is using $W; wait for it or stop it ($(tr '\n' ' ' < "$l/owner" 2>/dev/null))"
    fi
    tries=$((tries + 1)); [ "$tries" -le 2 ] || die "cannot take the lock $l"
    note "taking over the lock of a run that is gone (${pid:-unknown pid})"
    rm -f "$l/owner"; rmdir "$l" 2>/dev/null || true
  done
  printf 'pid=%s\nstarted=%s\ncommand=%s\n' "$$" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$CMD" > "$l/owner"
  LOCK_HELD=1
}
release_lock() {
  if [ "$LOCK_HELD" = 1 ]; then rm -f "$W/build.lock/owner"; rmdir "$W/build.lock" 2>/dev/null || true; LOCK_HELD=0; fi
}

# ---------------------------------------------------------------------------------------------------------- failure report
CUR_STEP=""; CUR_LOG=""
write_report() {   # <step> <exit status> <log or ''>
  local step=$1 rc=$2 log=$3 f
  [ -f "$W/$MARKER" ] || return 0   # only inside a working directory build.sh made
  [ -d "$REPORTS" ] || mkdir -p "$REPORTS" 2>/dev/null || return 0
  f=$REPORTS/failure-$step-$(date -u +%Y%m%dT%H%M%SZ).txt
  {
    say "NullMoth driver build: failure report"
    say "date (UTC):   $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    say "step:         $step (exit status $rc)"
    say "command:      ./build.sh $CMDLINE"
    say "build.sh:     $(sha256_file "$SELF" | cut -c1-16)   pins: $(sha256_file "$PINS_FILE" | cut -c1-16)"
    say "settings:     flavour=$NM_FLAVOUR accel=$NM_ACCEL jobs=$NM_JOBS offline=$NM_OFFLINE workdir=$W"
    hr; say "machine"
    if is_mac; then
      say "macOS $(sw_vers -productVersion 2>/dev/null) ($(sw_vers -buildVersion 2>/dev/null)), $(uname -m), $(sysctl -n hw.model 2>/dev/null)"
      say "cpu: $(sysctl -n machdep.cpu.brand_string 2>/dev/null), $(ncpu) cores, memory $(( $(sysctl -n hw.memsize 2>/dev/null || echo 0) / 1073741824 )) GB"
    else
      say "$(uname -srm)"
    fi
    hr; say "tools"; tool_versions
    hr; say "checkout"
    say "commit: $(git -C "$HERE" rev-parse HEAD 2>/dev/null) ($(git -C "$HERE" log -1 --format=%s 2>/dev/null | cut -c1-80))"
    say "branch: $(git -C "$HERE" rev-parse --abbrev-ref HEAD 2>/dev/null)"
    local ch; ch=$(git -C "$HERE" status --porcelain 2>/dev/null | wc -l | tr -d ' ')
    if [ "$ch" = 0 ]; then say "local changes: none"; else say "local changes: $ch file(s):"; git -C "$HERE" status --porcelain 2>/dev/null | sed -n 1,40p; fi
    hr; say "pins (build/pins.sh)"
    grep -E '^NM_PIN_[A-Z0-9_]*(_REF|_COMMIT|_SHA256|_VERSION|_MIN|_MAJOR)=' "$PINS_FILE" || true
    hr; say "variables the build passes to the scripts"; print_env 2>/dev/null | grep '^export ' || true
    if [ -n "$log" ] && [ -f "$log" ]; then hr; say "last 80 lines of $log"; tail -n 80 "$log"; fi
  } 2>&1 | tilde > "$f"
  printf '%s\n' "$f"
}
fail_step() {   # <step> <rc>
  local step=$1 rc=$2 rep
  say ""
  say "STOP: step '$step' failed (exit status $rc)."
  if [ -n "$CUR_LOG" ] && [ -f "$CUR_LOG" ]; then
    say "last lines of its log:"; tail -n 25 "$CUR_LOG" | tilde | sed 's/^/    /'
    say "full log:    $(printf '%s' "$CUR_LOG" | tilde)"
  fi
  rep=$(write_report "$step" "$rc" "$CUR_LOG" || true)
  [ -n "$rep" ] && say "report file: $(printf '%s' "$rep" | tilde)   (attach it to a bug report; it names no account, the home folder is written as ~)"
  say "docs/BUILDING.md, section \"When a build fails\", lists what each common failure means."
  CUR_STEP=""
  exit 1
}
on_err() {
  local rc=$? line=$1 cmd=$2
  # in a $(...) or ( ) the failure is the caller's to judge: leave quietly, the main shell reports what stops it
  if [ "${BASH_SUBSHELL:-0}" -gt 0 ]; then exit "$rc"; fi
  trap - ERR
  if [ -n "$CUR_STEP" ]; then fail_step "$CUR_STEP" "$rc"; fi
  say "STOP: build.sh stopped at line $line (exit status $rc): $cmd" >&2
  local rep; rep=$(write_report build.sh "$rc" "" 2>/dev/null || true)
  [ -n "$rep" ] && say "report file: $(printf '%s' "$rep" | tilde)" >&2
  exit "$rc"
}
on_exit() { release_lock; [ -z "${TMPF:-}" ] || rm -f "$TMPF" "$TMPF".* 2>/dev/null || true; }
on_signal() { trap - INT TERM; say "build.sh: interrupted${CUR_STEP:+ during step $CUR_STEP}" >&2; release_lock; exit 130; }

# ---------------------------------------------------------------------------------------------------------- tools
BREW=""; BREW_PREFIX=""
find_brew() {
  BREW=""; BREW_PREFIX=""
  local c
  for c in "${NM_BREW:-}" /opt/homebrew/bin/brew /usr/local/bin/brew "$(command -v brew 2>/dev/null || true)"; do
    [ -n "$c" ] && [ -x "$c" ] && { BREW=$c; break; }
  done
  [ -n "$BREW" ] && BREW_PREFIX=$(cd "$(dirname "$BREW")/.." && pwd -P)
  return 0
}
brew_opt() { printf '%s/opt/%s' "${BREW_PREFIX:-/nonexistent-homebrew}" "$1"; }   # a path that never exists without Homebrew

# Each tool: the path the build will use ("" when none fits) and a status line. Homebrew's bin folder is never put on
# the build's PATH: tools/bin gets a link to each chosen tool, so nothing else from Homebrew can leak into the build.
T_PYTHON=""; T_PATCH=""; T_NINJA=""; T_CMAKE=""; T_PKGCONFIG=""; T_GLSLANG=""; T_LLVMCONFIG=""; T_LLVMSPIRV=""
LLVM_PREFIX=""; PC_SPIRV_LLVM=""; PC_SPIRV_TOOLS=""; LIBCLANG_DIR=""
pick() {   # <min version or ''> <version command arg> <candidates...>: the first executable at least that new
  local min=$1 arg=$2 c v; shift 2
  for c in "$@"; do
    [ -n "$c" ] && [ -x "$c" ] || continue
    if [ -z "$min" ]; then printf '%s' "$c"; return 0; fi
    v=$("$c" $arg 2>&1 | version_of)
    [ -n "$v" ] && ver_ge "$v" "$min" && { printf '%s' "$c"; return 0; }
  done
  return 0
}
# glslangValidator --version says "Glslang Version: 11:16.0.0": 11 is its SPIR-V generator number and the release
# follows the second colon. Mesa's meson.build reads it the same way (split(':')[2]).
glslang_version() { "$1" --version 2>/dev/null | first_line | sed -n 's/^[^:]*:[^:]*:[[:space:]]*//p' | version_of; }
pc_version() { [ ! -f "$1" ] || sed -n 's/^Version:[[:space:]]*//p' "$1" | first_line; }
find_tools() {
  find_brew
  local cands=() p v
  # Python 3.10 or newer: NM_PYTHON, Homebrew python@3.14 and older, then PATH
  for p in "${NM_PYTHON:-}" "$(brew_opt python@3.14)/bin/python3.14" "$(brew_opt python@3.13)/bin/python3.13" \
           "$(brew_opt python@3.12)/bin/python3.12" "$(brew_opt python@3.11)/bin/python3.11" "$(brew_opt python@3.10)/bin/python3.10" \
           "$(command -v python3 2>/dev/null || true)"; do cands+=( "$p" ); done
  T_PYTHON=$(pick "$NM_PIN_TOOL_PYTHON_MIN" --version "${cands[@]}")
  # GNU patch: the scripts pass --no-backup-if-mismatch
  T_PATCH=""
  for p in "$(brew_opt gpatch)/bin/gpatch" "$(brew_opt gpatch)/libexec/gnubin/patch" "$(command -v gpatch 2>/dev/null || true)" "$(command -v patch 2>/dev/null || true)"; do
    [ -n "$p" ] && [ -x "$p" ] && case "$("$p" --version 2>/dev/null | first_line)" in *'GNU patch'*) true ;; *) false ;; esac && { T_PATCH=$p; break; }
  done
  T_NINJA=$(pick "$NM_PIN_TOOL_NINJA_MIN" --version "$(brew_opt ninja)/bin/ninja" "$(command -v ninja 2>/dev/null || true)")
  T_CMAKE=$(pick "$NM_PIN_TOOL_CMAKE_MIN" --version "$(brew_opt cmake)/bin/cmake" "$(command -v cmake 2>/dev/null || true)")
  T_PKGCONFIG=$(pick "" "" "$(brew_opt pkgconf)/bin/pkg-config" "$(command -v pkg-config 2>/dev/null || true)")
  T_GLSLANG=""
  for p in "$(brew_opt glslang)/bin/glslangValidator" "$(command -v glslangValidator 2>/dev/null || true)"; do
    [ -n "$p" ] && [ -x "$p" ] || continue
    v=$(glslang_version "$p" || true)
    [ -n "$v" ] && ver_ge "$v" "$NM_PIN_TOOL_GLSLANG_MIN" && { T_GLSLANG=$p; break; }
  done
  # LLVM 22 (its libraries build the native mesa_clc; NVK itself is compiled by Xcode's clang)
  LLVM_PREFIX=""; T_LLVMCONFIG=""; T_LLVMSPIRV=""; LIBCLANG_DIR=""
  for p in "${NM_LLVM_PREFIX:-}" "$(brew_opt "llvm@$NM_PIN_TOOL_LLVM_MAJOR")" "$(brew_opt llvm)"; do
    [ -n "$p" ] && [ -x "$p/bin/llvm-config" ] || continue
    case "$("$p/bin/llvm-config" --version 2>/dev/null)" in "$NM_PIN_TOOL_LLVM_MAJOR".*) LLVM_PREFIX=$p; break ;; esac
  done
  if [ -n "$LLVM_PREFIX" ]; then
    T_LLVMCONFIG=$LLVM_PREFIX/bin/llvm-config
    LIBCLANG_DIR=$LLVM_PREFIX/lib
  fi
  # SPIRV-LLVM-Translator: its .pc must have LLVM's major.minor
  PC_SPIRV_LLVM=""; T_LLVMSPIRV=""
  for p in "$(brew_opt spirv-llvm-translator)" "$LLVM_PREFIX"; do
    [ -n "$p" ] && [ -f "$p/lib/pkgconfig/LLVMSPIRVLib.pc" ] || continue
    PC_SPIRV_LLVM=$p/lib/pkgconfig
    [ -x "$p/bin/llvm-spirv" ] && T_LLVMSPIRV=$p/bin/llvm-spirv
    break
  done
  PC_SPIRV_TOOLS=""
  p=$(brew_opt spirv-tools); [ -n "$p" ] && [ -f "$p/lib/pkgconfig/SPIRV-Tools.pc" ] && PC_SPIRV_TOOLS=$p/lib/pkgconfig
  return 0
}
llvm_version() { [ -z "$T_LLVMCONFIG" ] || "$T_LLVMCONFIG" --version 2>/dev/null || true; }
venv_python() { [ -x "$VENV/bin/python3" ] && printf '%s' "$VENV/bin/python3"; return 0; }

tool_versions() {   # one line per tool, for doctor and the report
  local x
  if is_mac; then
    say "xcode-select: $(xcode-select -p 2>/dev/null || echo none)${DEVELOPER_DIR:+ (DEVELOPER_DIR=$DEVELOPER_DIR)}"
    say "xcodebuild:   $(xcodebuild -version 2>/dev/null | tr '\n' ' ')"
    say "SDK:          $(xcrun --show-sdk-version 2>/dev/null) $(xcrun --show-sdk-path 2>/dev/null)"
    say "clang:        $(xcrun clang --version 2>/dev/null | first_line)"
    say "ld:           $(xcrun ld -v 2>&1 | first_line)"
  fi
  say "git:          $(git --version 2>/dev/null)"
  say "make:         $(make --version 2>/dev/null | first_line)"
  say "patch:        ${T_PATCH:-none} $([ -n "$T_PATCH" ] && "$T_PATCH" --version 2>/dev/null | first_line)"
  say "python:       ${T_PYTHON:-none} $([ -n "$T_PYTHON" ] && "$T_PYTHON" --version 2>&1)"
  x=$(venv_python); say "venv:         ${x:-none} $([ -n "$x" ] && "$VENV/bin/meson" --version 2>/dev/null | sed 's/^/meson /')"
  say "ninja:        ${T_NINJA:-none} $([ -n "$T_NINJA" ] && "$T_NINJA" --version 2>/dev/null)"
  say "cmake:        ${T_CMAKE:-none} $([ -n "$T_CMAKE" ] && "$T_CMAKE" --version 2>/dev/null | first_line)"
  say "pkg-config:   ${T_PKGCONFIG:-none} $([ -n "$T_PKGCONFIG" ] && "$T_PKGCONFIG" --version 2>/dev/null)"
  say "glslang:      ${T_GLSLANG:-none} $([ -n "$T_GLSLANG" ] && "$T_GLSLANG" --version 2>/dev/null | first_line)"
  say "llvm:         ${LLVM_PREFIX:-none} $([ -n "$T_LLVMCONFIG" ] && "$T_LLVMCONFIG" --version 2>/dev/null)"
  say "spirv-llvm:   ${PC_SPIRV_LLVM:-none} $(pc_version "$PC_SPIRV_LLVM/LLVMSPIRVLib.pc" 2>/dev/null)"
  say "spirv-tools:  ${PC_SPIRV_TOOLS:-none} $(pc_version "$PC_SPIRV_TOOLS/SPIRV-Tools.pc" 2>/dev/null)"
  say "rustc:        $([ -x "$RUST_BIN/rustc" ] && "$RUST_BIN/rustc" --version 2>/dev/null || echo "none at $RUST_BIN")"
  say "bindgen:      $([ -x "$CARGO_DIR/bin/bindgen" ] && "$CARGO_DIR/bin/bindgen" --version 2>/dev/null || echo none)"
  say "cbindgen:     $([ -x "$CARGO_DIR/bin/cbindgen" ] && "$CARGO_DIR/bin/cbindgen" --version 2>/dev/null || echo none)"
  say "brew:         ${BREW:-none} $([ -n "$BREW" ] && "$BREW" --version 2>/dev/null | first_line)"
  return 0
}

# ---------------------------------------------------------------------------------------------------------- doctor
D_MISSING=0; D_LINES=""
d_ok()      { say "  ok       $1"; }
d_missing() { say "  MISSING  $1"; D_MISSING=$((D_MISSING + 1)); D_LINES="$D_LINES  - $1
"; }
d_old()     { say "  TOO OLD  $1"; D_MISSING=$((D_MISSING + 1)); D_LINES="$D_LINES  - $1
"; }
d_note()    { say "  note     $1"; }
d_warn()    { say "  WARNING  $1"; }

doctor() {   # prints; sets D_MISSING
  D_MISSING=0; D_LINES=""
  local v x sdk
  say "== host"
  if is_mac; then
    v=$(sw_vers -productVersion 2>/dev/null || echo 0)
    d_ok "macOS $v ($(uname -m))"
    ver_ge "$v" "$NM_PIN_TOOL_MACOS_MIN" || d_old "macOS $v: the build needs Xcode $NM_PIN_TOOL_XCODE_MIN, which runs on macOS $NM_PIN_TOOL_MACOS_MIN or later; deps, build and package refuse this Mac"
    case "$(uname -m)" in
      arm64) d_note "Apple silicon: supported (the release build Mac is one); every part is built for x86_64" ;;
      x86_64) d_note "Intel: supported. NVIDIA's make -n probes the host compiler for x86-only hardening flags, so the RM archive may carry flags a release built on Apple silicon does not" ;;
    esac
  else
    d_missing "a Mac: this is $(uname -s) $(uname -m). Here only doctor, fetch, test, env and --dry-run work; building needs macOS with Xcode"
  fi
  say "== checkout and working directory"
  check_paths
  if [ -n "$PATH_PROBLEMS" ]; then printf '%s' "$PATH_PROBLEMS" | while IFS= read -r x; do [ -n "$x" ] && say "  REFUSED  $x"; done; D_MISSING=$((D_MISSING + 1)); D_LINES="$D_LINES  - a path problem (above)
"; fi
  if git -C "$HERE" rev-parse HEAD >/dev/null 2>&1; then
    x=$(git -C "$HERE" status --porcelain | wc -l | tr -d ' ')
    d_ok "checkout $HERE at $(git -C "$HERE" rev-parse --short HEAD)$([ "$x" != 0 ] && echo ", $x changed file(s): the build uses them")"
  fi
  d_note "working directory $W$([ -f "$W/$MARKER" ] && echo ' (exists)' || echo ' (created on the first fetch or build)')"
  say "== pins (build/pins.sh against the scripts that use them)"
  check_pins
  [ "$PIN_PROBLEMS" = 0 ] || { D_MISSING=$((D_MISSING + 1)); D_LINES="$D_LINES  - pins disagree with the scripts (above)
"; }
  say "== Xcode (cannot be installed by a script)"
  if is_mac; then
    if ! xcode-select -p >/dev/null 2>&1; then
      d_missing "Xcode $NM_PIN_TOOL_XCODE_MIN or later (none selected)"
    else
      x=$(xcodebuild -version 2>/dev/null | sed -n 's/^Xcode //p' | first_line)
      if [ -z "$x" ]; then d_missing "full Xcode $NM_PIN_TOOL_XCODE_MIN or later: only the command line tools are selected ($(xcode-select -p))"
      elif ver_ge "$x" "$NM_PIN_TOOL_XCODE_MIN"; then d_ok "Xcode $x at $(xcode-select -p)"
      else d_old "Xcode $x: $NM_PIN_TOOL_XCODE_MIN or later is needed (the plugin uses the macOS 26 SDK)"; fi
      sdk=$(xcrun --show-sdk-path 2>/dev/null || true); v=$(xcrun --show-sdk-version 2>/dev/null || true)
      if [ -z "$sdk" ]; then d_missing "a macOS SDK (xcrun --show-sdk-path fails)"
      else
        ver_ge "${v:-0}" "$NM_PIN_TOOL_SDK_MIN" && d_ok "macOS SDK $v" || d_old "macOS SDK $v: $NM_PIN_TOOL_SDK_MIN or later"
        [ -f "$sdk/System/Library/Frameworks/Kernel.framework/Headers/IOKit/IOLib.h" ] && d_ok "kernel headers in the SDK" || d_missing "Kernel.framework headers in $sdk (every kext compiles against them)"
        [ -f "$sdk/usr/lib/libkmod.a" ] && d_ok "libkmod.a in the SDK" || d_missing "usr/lib/libkmod.a in $sdk (every kext links it)"
        [ -f "$sdk/System/Library/Frameworks/Metal.framework/Headers/MTL4CommandQueue.h" ] && d_ok "Metal 4 headers (plugin)" || d_missing "Metal/MTL4CommandQueue.h in $sdk (the plugin needs the macOS 26 SDK)"
      fi
      for x in clang clang++ libtool lipo vtool otool nm strip install_name_tool dwarfdump codesign; do
        xcrun -f "$x" >/dev/null 2>&1 || have "$x" || d_missing "$x (Xcode)"
      done
      d_note "the build stops on any new compiler or linker warning (build/warnings.txt ceilings are 0): an Xcode other than the release's may add one; the release's Xcode version is not recorded anywhere"
    fi
  else
    d_note "not checked: not a Mac"
  fi
  d_note "no Kernel Debug Kit is needed: no build step reads one (install.sh passes --allow-missing-kdk)"
  say "== tools from macOS and Xcode"
  for x in git make curl shasum strings plutil ditto md5 xattr tar; do
    if have "$x"; then :; elif is_mac; then d_missing "$x"; else d_note "$x not found (only matters on the Mac)"; fi
  done
  if have make; then
    v=$(make --version 2>/dev/null | first_line)
    case "$v" in *"GNU Make"*) ver_ge "$(printf '%s' "$v" | version_of)" "$NM_PIN_TOOL_MAKE_MIN" && d_ok "$v" || d_old "$v (GNU make $NM_PIN_TOOL_MAKE_MIN or later)" ;; *) d_missing "GNU make (found: $v)" ;; esac
  fi
  say "== build tools (\"./build.sh deps\" installs the missing ones)"
  find_tools
  if [ -n "$BREW" ]; then d_ok "Homebrew $BREW"; elif is_mac; then d_missing "Homebrew (https://brew.sh): build.sh offers to run its official installer"; fi
  [ -n "$T_PATCH" ] && d_ok "GNU patch $T_PATCH" || d_missing "GNU patch (Homebrew gpatch): the scripts pass --no-backup-if-mismatch"
  if [ -n "$T_PYTHON" ]; then
    v=$("$T_PYTHON" --version 2>&1 | version_of)
    d_ok "Python $v ($T_PYTHON)"
    ver_ge "$v" "$NM_PIN_TOOL_PYTHON_TESTS_MIN" || d_note "Python $NM_PIN_TOOL_PYTHON_TESTS_MIN is needed by one host test (tools/test_fw_fetch.py); the build needs $NM_PIN_TOOL_PYTHON_MIN"
  else d_missing "Python $NM_PIN_TOOL_PYTHON_MIN or later (Homebrew $NM_PIN_TOOL_PYTHON_FORMULA); macOS's python3 is 3.9"; fi
  x=$(venv_python)
  if [ -n "$x" ] && [ -x "$VENV/bin/meson" ]; then
    v=$("$VENV/bin/meson" --version 2>/dev/null)
    ver_ge "$v" "$NM_PIN_TOOL_MESON_MIN" && d_ok "meson $v and Mesa's Python modules in $VENV" || d_old "meson $v in $VENV"
  else d_missing "the Python environment $VENV (meson, mako, PyYAML, packaging)"; fi
  [ -n "$T_NINJA" ] && d_ok "ninja $("$T_NINJA" --version)" || d_missing "ninja $NM_PIN_TOOL_NINJA_MIN or later"
  [ -n "$T_CMAKE" ] && d_ok "cmake $("$T_CMAKE" --version | first_line | version_of)" || d_missing "cmake $NM_PIN_TOOL_CMAKE_MIN or later"
  [ -n "$T_PKGCONFIG" ] && d_ok "pkg-config $T_PKGCONFIG" || d_missing "pkg-config (Homebrew pkgconf)"
  [ -n "$T_GLSLANG" ] && d_ok "glslangValidator $(glslang_version "$T_GLSLANG")" || d_missing "glslangValidator $NM_PIN_TOOL_GLSLANG_MIN or later (Homebrew glslang)"
  if [ -n "$LLVM_PREFIX" ]; then d_ok "LLVM $("$T_LLVMCONFIG" --version) at $LLVM_PREFIX"
  else d_missing "LLVM $NM_PIN_TOOL_LLVM_MAJOR.x with clang libraries (Homebrew llvm@$NM_PIN_TOOL_LLVM_MAJOR, or NM_LLVM_PREFIX)"; fi
  if [ -n "$PC_SPIRV_LLVM" ]; then
    v=$(pc_version "$PC_SPIRV_LLVM/LLVMSPIRVLib.pc")
    if [ -n "$LLVM_PREFIX" ]; then
      x=$("$T_LLVMCONFIG" --version | cut -d. -f1-2)
      case "$v" in "$x"|"$x".*) d_ok "SPIRV-LLVM-Translator $v" ;; *) d_old "SPIRV-LLVM-Translator $v does not match LLVM $x (Mesa needs the same major.minor)" ;; esac
    else d_note "SPIRV-LLVM-Translator $v (LLVM $NM_PIN_TOOL_LLVM_MAJOR missing)"; fi
  else d_missing "SPIRV-LLVM-Translator for LLVM $NM_PIN_TOOL_LLVM_MAJOR (Homebrew spirv-llvm-translator)"; fi
  if [ -n "$PC_SPIRV_TOOLS" ]; then
    v=$(pc_version "$PC_SPIRV_TOOLS/SPIRV-Tools.pc")
    ver_ge "$v" "$NM_PIN_TOOL_SPIRV_TOOLS_MIN" && d_ok "SPIRV-Tools $v" || d_old "SPIRV-Tools $v ($NM_PIN_TOOL_SPIRV_TOOLS_MIN or later)"
  else d_missing "SPIRV-Tools $NM_PIN_TOOL_SPIRV_TOOLS_MIN or later (Homebrew spirv-tools)"; fi
  if [ -x "$RUST_BIN/rustc" ]; then
    v=$("$RUST_BIN/rustc" --version | version_of)
    [ "$v" = "$NM_PIN_RUST_VERSION" ] && d_ok "Rust $v ($RUST_BIN)" || d_old "Rust $v at $RUST_BIN, pinned $NM_PIN_RUST_VERSION"
    [ -d "$("$RUST_BIN/rustc" --print target-libdir --target x86_64-apple-darwin 2>/dev/null)" ] && d_ok "Rust target x86_64-apple-darwin" || d_missing "Rust target x86_64-apple-darwin"
  else d_missing "Rust $NM_PIN_RUST_VERSION in $RUSTUP_DIR (installed by rustup into the working directory)"; fi
  for x in bindgen:"$NM_PIN_BINDGEN_VERSION" cbindgen:"$NM_PIN_CBINDGEN_VERSION"; do
    if [ -x "$CARGO_DIR/bin/${x%%:*}" ]; then
      v=$("$CARGO_DIR/bin/${x%%:*}" --version 2>/dev/null | version_of)
      [ "$v" = "${x#*:}" ] && d_ok "${x%%:*} $v" || d_old "${x%%:*} $v, pinned ${x#*:}"
    else d_missing "${x%%:*} ${x#*:} (cargo install into $CARGO_DIR)"; fi
  done
  say "== sources (\"./build.sh fetch\")"
  source_status
  say "== things that can stop a build on another Mac"
  x=$(id -un 2>/dev/null || true)
  [ "${#x}" -ge 5 ] || d_warn "your account name '$x' is short: build_rm620.sh and build_kms620.sh stop (exit 6) if it appears anywhere in NVIDIA's archive, case-insensitive"
  x=$(hostname -s 2>/dev/null || true)
  [ "${#x}" -ge 5 ] || d_warn "the computer's short name '$x' is short: the same check also looks for it (System Settings > General > Sharing > Local hostname)"
  for x in CC CXX CFLAGS CPPFLAGS LDFLAGS; do eval "[ -z \"\${$x:-}\" ]" || d_note "$x is set in your environment; build.sh removes it for every step (an exported CC breaks build_rm620.sh)"; done
  if is_mac && [ "$(uname -m)" = x86_64 ] && { [ -e /usr/local/lib/libelf.dylib ] || [ -e /usr/local/lib/libelf.a ]; }; then
    d_warn "/usr/local/lib holds a libelf: the NVK link may pick it up; build.sh stops if the result loads anything from /usr/local"
  fi
  if [ -z "${NM_BASE_PACKAGE:-}" ]; then d_note "no base package (NM_BASE_PACKAGE): everything builds, but \"package\" needs a release tarball (docs/BUILDING.md)"
  elif [ -e "$NM_BASE_PACKAGE" ]; then d_ok "base package $NM_BASE_PACKAGE"; else d_missing "the base package $NM_BASE_PACKAGE"; fi
  [ "$NM_OFFLINE" = 1 ] && d_note "offline: NVK cannot be built (meson downloads six Rust crates into each fresh Mesa tree)"
  say ""
  if [ "$D_MISSING" = 0 ]; then say "doctor: everything required is present"
  else say "doctor: $D_MISSING thing(s) missing or too old:"; printf '%s' "$D_LINES"; fi
  return 0
}

# ---------------------------------------------------------------------------------------------------------- run helpers
TMPF=""
tmpfile() { [ -n "$TMPF" ] || die "internal: no temporary file"; printf '%s' "$TMPF"; }   # made once in main; removed on exit
# x <command...>: run one command of a step; its output goes to the step's log (and the terminal with --verbose).
x() {
  if [ "$NM_DRY_RUN" = 1 ]; then say "    $(qcmd "$@")"; return 0; fi
  printf '+ %s\n' "$(qcmd "$@")" >> "$CUR_LOG"
  if [ "$NM_VERBOSE" = 1 ]; then "$@" 2>&1 | tee -a "$CUR_LOG"; else "$@" >> "$CUR_LOG" 2>&1; fi
}
# The environment every step's script gets: a fixed PATH (tools/bin first, Homebrew never), no compiler variables.
step_env() {
  printf '%s\n' env -u CC -u CXX -u OBJC -u CFLAGS -u CXXFLAGS -u OBJCFLAGS -u CPPFLAGS -u LDFLAGS -u NM_TAHOE -u SDKROOT \
    "PATH=$STEP_PATH" "NULLMOTH_BUILD_ROOT=$R" "MACOSX_DEPLOYMENT_TARGET=${MACOSX_DEPLOYMENT_TARGET:-15.0}" PYTHONDONTWRITEBYTECODE=1 \
    "JOBS=${JOBS:-$NM_JOBS}"
}
STEP_ENV=()
make_step_env() { STEP_ENV=(); local l; while IFS= read -r l; do STEP_ENV+=( "$l" ); done < <(step_env); }
# variables passed through only when set
passthru() { local v; for v in "$@"; do eval "[ -n \"\${$v+x}\" ] && printf '%s\n' \"$v=\${$v}\"" || true; done; }

# ---------------------------------------------------------------------------------------------------------- fetch
git_q() { git -c advice.detachedHead=false -c init.defaultBranch=nm "$@"; }
mirror_url() {   # <pin id>: the mirror variable's value, or the pinned URL
  local mv url
  mv=$(pin "$1_MIRROR"); url=$(pin "$1_URL")
  case "$mv" in
    NM_KHRONOS_URL) if [ -n "${NM_KHRONOS_URL:-}" ]; then printf '%s/%s' "${NM_KHRONOS_URL%/}" "${url##*/}"; else printf '%s' "$url"; fi ;;   # a prefix
    *) eval "printf '%s' \"\${$mv:-$url}\"" ;;
  esac
}
# fetch_bare <id> <bare repo>: the pinned ref or commit into a bare repository; verifies the commit id
fetch_bare() {
  local id=$1 repo=$2 ref commit url got
  ref=$(pin "${id}_REF"); commit=$(pin "${id}_COMMIT"); url=$(mirror_url "$id")
  if [ -d "$repo" ] && git_q -C "$repo" cat-file -e "$commit^{commit}" 2>/dev/null; then
    got=$( { [ -n "$ref" ] && git_q -C "$repo" rev-parse -q --verify "refs/tags/$ref^{commit}"; } 2>/dev/null || true)
    if [ -z "$ref" ] || [ "$got" = "$commit" ]; then say "  ok       $(pin "${id}_NAME"): $commit (already here)"; return 0; fi
  fi
  [ "$NM_OFFLINE" = 0 ] || die "offline, and $(pin "${id}_NAME") is not in $repo yet (run ./build.sh fetch with the network)"
  say "  fetch    $(pin "${id}_NAME") from $url"
  if [ "$NM_DRY_RUN" = 1 ]; then
    say "    git init --bare $(q "$repo"); git fetch --depth 1 $(q "$url") $([ -n "$ref" ] && echo "refs/tags/$ref" || echo "$commit")"; return 0
  fi
  [ -d "$repo" ] || git_q init -q --bare "$repo"
  if [ -n "$ref" ]; then
    git_q -C "$repo" fetch -q --depth 1 "$url" "+refs/tags/$ref:refs/tags/$ref" || die "could not fetch $ref from $url"
    got=$(git_q -C "$repo" rev-parse "refs/tags/$ref^{commit}")
  else
    git_q -C "$repo" fetch -q --depth 1 "$url" "$commit" || die "could not fetch $commit from $url"
    got=$(git_q -C "$repo" rev-parse FETCH_HEAD)
  fi
  if [ "$got" != "$commit" ]; then
    [ -z "$ref" ] || git_q -C "$repo" tag -d "$ref" >/dev/null 2>&1 || true
    die "$(pin "${id}_NAME"): $url gave commit $got for ${ref:-$commit}, the pin is $commit (build/pins.sh); nothing was used"
  fi
  # a ref for a commit-only pin, so that a clone of this repository gets it
  [ -n "$ref" ] || { git_q -C "$repo" update-ref "refs/heads/nm-pinned" "$commit"; git_q -C "$repo" symbolic-ref HEAD refs/heads/nm-pinned; }
  say "  verified $(pin "${id}_NAME"): commit $got"
}
# export_tree <id> <bare repo> <dir>: the pinned commit as a plain tree (what the build scripts copy)
export_tree() {
  local id=$1 repo=$2 dir=$3 commit rec
  commit=$(pin "${id}_COMMIT"); rec=$SRC/exports/$(printf '%s' "$dir" | sha256_stdin | cut -c1-16)
  if [ -d "$dir" ] && [ "$(cat "$rec" 2>/dev/null)" = "$commit $dir" ]; then say "  ok       $dir"; return 0; fi
  say "  export   $commit -> $dir"
  if [ -e "$dir" ]; then in_workdir "$dir" || die "$dir exists and is outside the working directory: remove it or point the variable at it"; safe_rm "$dir"; fi
  [ "$NM_DRY_RUN" = 1 ] && { say "    git archive $commit | tar -x -C $(q "$dir")"; return 0; }
  mkdir -p "$dir" "$SRC/exports"
  git_q -C "$repo" archive "$commit" | tar -x -C "$dir"
  printf '%s %s\n' "$commit" "$dir" > "$rec"
}
# checkout_clone <id> <bare repo> <dir> [sparse paths...]: a work tree at the pinned commit whose origin is the bare repo
checkout_clone() {
  local id=$1 repo=$2 dir=$3 commit; shift 3
  commit=$(pin "${id}_COMMIT")
  if [ -d "$dir/.git" ] && [ "$(git_q -C "$dir" rev-parse HEAD 2>/dev/null)" = "$commit" ] && [ -z "$(git_q -C "$dir" status --porcelain 2>/dev/null)" ]; then
    say "  ok       $dir"; return 0
  fi
  say "  checkout $commit -> $dir"
  if [ -e "$dir" ]; then in_workdir "$dir" || die "$dir exists, is outside the working directory and is not a clean checkout at $commit"; safe_rm "$dir"; fi
  [ "$NM_DRY_RUN" = 1 ] && { say "    git clone --no-checkout file://$repo $(q "$dir"); git checkout $commit"; return 0; }
  git_q clone -q --no-checkout "file://$repo" "$dir"
  if [ $# -gt 0 ]; then git_q -C "$dir" sparse-checkout set "$@"; fi
  git_q -C "$dir" checkout -q --detach "$commit"
}
fetch_file() {   # <id> <dest>: one file, SHA-256 checked
  local id=$1 dest=$2 sha url mv tmp
  sha=$(pin "${id}_SHA256"); mv=$(pin "${id}_MIRROR"); url=$(pin "${id}_URL")
  eval "[ -z \"\${$mv:-}\" ] || url=\"\${$mv%/}/${url##*/}\""
  if [ -f "$dest" ] && [ "$(sha256_file "$dest")" = "$sha" ]; then say "  ok       $(pin "${id}_NAME")"; return 0; fi
  [ "$NM_OFFLINE" = 0 ] || die "offline, and $(pin "${id}_NAME") is not in $dest yet"
  say "  fetch    $(pin "${id}_NAME") from $url"
  [ "$NM_DRY_RUN" = 1 ] && { say "    curl -fsSL $(q "$url") -o $(q "$dest")"; return 0; }
  mkdir -p "$(dirname "$dest")"; tmp=$dest.part
  curl -fsSL --proto '=https,file' --retry 3 -o "$tmp" "$url" || { rm -f "$tmp"; die "could not download $url"; }
  [ "$(sha256_file "$tmp")" = "$sha" ] || { rm -f "$tmp"; die "$url has SHA-256 $(sha256_file "$tmp" 2>/dev/null), the pin is $sha (build/pins.sh)"; }
  mv -f "$tmp" "$dest"; say "  verified $(pin "${id}_NAME"): sha256 $sha"
}
verify_given_tree() {   # <what> <dir> <file with the version> <expected text>
  [ -f "$2/$3" ] && grep -q "$4" "$2/$3" || die "$1 at $2 is not the pinned one ($3 does not contain $4)"
  say "  ok       $1: $2 (given; $3 says $4)"
}
cmd_fetch() {
  say "== fetching the pinned sources into $W"
  local og=$GITDIR/open-gpu-kernel-modules.git
  have git || die "git is needed"
  have curl || die "curl is needed"
  if [ -n "${NM_OGKM620_PRISTINE:-}" ]; then verify_given_tree "NVIDIA $NM_PIN_OGKM620_REF tree" "$PRISTINE" version.mk "NVIDIA_VERSION = $NM_PIN_OGKM620_REF"
  else fetch_bare OGKM620 "$og"; export_tree OGKM620 "$og" "$PRISTINE"; fi
  fetch_bare OGKM610 "$og"
  if [ -n "${NM_OGKM610:-}" ]; then verify_given_tree "NVIDIA $NM_PIN_OGKM610_REF tree" "$OGKM610" version.mk "NVIDIA_VERSION = $NM_PIN_OGKM610_REF"
  else export_tree OGKM610 "$og" "$OGKM610"; fi
  # Mesa: a clone with a work tree (build_nvk.sh exports the pinned commit from it; the plugin reads its include/)
  if [ -n "${NM_MESA_GIT:-}" ]; then
    git -C "$MESA" cat-file -e "$NM_PIN_MESA_COMMIT^{commit}" 2>/dev/null || die "NM_MESA_GIT=$MESA has no commit $NM_PIN_MESA_COMMIT"
    [ "$(git -C "$MESA" rev-parse HEAD)" = "$NM_PIN_MESA_COMMIT" ] || warn "$MESA has $(git -C "$MESA" rev-parse --short HEAD) checked out: the release plugin takes its Vulkan headers from that work tree's include/, which is then not mesa-26.3.0-rc3's"
    say "  ok       Mesa: $MESA (given)"
  else
    fetch_bare MESA "$GITDIR/mesa.git"
    checkout_clone MESA "$GITDIR/mesa.git" "$MESA"
  fi
  fetch_bare VKHEADERS "$KHRONOS/Vulkan-Headers"
  fetch_bare VKLOADER "$KHRONOS/Vulkan-Loader"
  fetch_bare OPENGPUDOC "$GITDIR/open-gpu-doc.git"
  fetch_bare RYZENADJ "$GITDIR/RyzenAdj.git"
  checkout_clone RYZENADJ "$GITDIR/RyzenAdj.git" "$RYZENSRC"
  fetch_file WEBKIT_CMBASE "$WEBKITSRC/CMBaseObjectSPI.h"
  fetch_file WEBKIT_VTDEC "$WEBKITSRC/VTVideoDecoderSPI.h"
  # build_nvencvt.sh's header folder, laid out as it makes it, with origins on this Mac (its fetch then stays local)
  checkout_clone OGKM610 "$og" "$NVHDR/ogkm" src/common/sdk/nvidia/inc src/nvidia/arch/nvalloc/unix/include
  checkout_clone OPENGPUDOC "$GITDIR/open-gpu-doc.git" "$NVHDR/ogd" classes/video
  if [ "$NM_DRY_RUN" = 1 ]; then say "    cp $(q "$WEBKITSRC")/*.h $(q "$NVHDR/webkit-spi")/"
  else mkdir -p "$NVHDR/webkit-spi"; cp -p "$WEBKITSRC/CMBaseObjectSPI.h" "$WEBKITSRC/VTVideoDecoderSPI.h" "$NVHDR/webkit-spi/"; fi
  check_mesa_requirements
  # the translator's crates, so that its build runs offline; checked against translator/wrapper/Cargo.lock by cargo
  if [ -x "$RUST_BIN/cargo" ]; then
    if [ "$NM_OFFLINE" = 1 ]; then note "offline: the translator's crates are not fetched (the build uses what $CARGO_DIR holds)"
    elif [ "$NM_DRY_RUN" = 1 ]; then say "    cargo fetch --locked --manifest-path translator/wrapper/Cargo.toml"
    else
      say "  fetch    the translator's crates (cargo fetch --locked)"
      env "CARGO_HOME=$CARGO_DIR" "RUSTUP_HOME=$RUSTUP_DIR" "PATH=$RUST_BIN:/usr/bin:/bin" \
        "$RUST_BIN/cargo" fetch --locked --manifest-path "$HERE/translator/wrapper/Cargo.toml" >/dev/null || die "cargo fetch failed"
    fi
  else
    note "the translator's crates are fetched once Rust is installed (./build.sh deps); its build downloads them otherwise"
  fi
  say "fetch: every source is at its pinned commit or SHA-256"
}
source_status() {
  local c
  [ -f "$PRISTINE/version.mk" ] && d_ok "NVIDIA $NM_PIN_OGKM620_REF: $PRISTINE" || d_note "NVIDIA $NM_PIN_OGKM620_REF not fetched yet"
  [ -f "$OGKM610/version.mk" ] && d_ok "NVIDIA $NM_PIN_OGKM610_REF: $OGKM610" || d_note "NVIDIA $NM_PIN_OGKM610_REF not fetched yet"
  c=$(git -C "$MESA" rev-parse HEAD 2>/dev/null || true)
  if [ "$c" = "$NM_PIN_MESA_COMMIT" ]; then d_ok "Mesa $NM_PIN_MESA_REF: $MESA"; elif [ -n "$c" ]; then d_warn "Mesa at $MESA has $c checked out, not $NM_PIN_MESA_COMMIT"; else d_note "Mesa not fetched yet"; fi
  [ -d "$KHRONOS/Vulkan-Loader" ] && d_ok "Vulkan-Headers and Vulkan-Loader mirrors: $KHRONOS" || d_note "Vulkan loader sources not fetched yet"
  [ -d "$RYZENSRC/.git" ] && d_ok "RyzenAdj: $RYZENSRC" || d_note "RyzenAdj not fetched yet"
  [ -d "$NVHDR/ogd" ] && d_ok "libnvencvt headers: $NVHDR" || d_note "libnvencvt headers not fetched yet"
  return 0
}
# The minimum versions above come from Mesa's meson.build at the pinned commit: once it is here, compare.
has_text() { case "$1" in *"$2"*) return 0 ;; esac; return 1; }   # (printf | grep -q fails under pipefail when grep exits first)
check_mesa_requirements() {
  local mb
  [ -d "$MESA/.git" ] || [ -d "$MESA" ] || return 0
  [ "$NM_DRY_RUN" = 1 ] && return 0
  mb=$(git -C "$MESA" show "$NM_PIN_MESA_COMMIT:meson.build" 2>/dev/null) || return 0
  has_text "$mb" "python_version_req = '>= $NM_PIN_TOOL_PYTHON_MIN'" || die "Mesa's meson.build no longer asks Python >= $NM_PIN_TOOL_PYTHON_MIN: update build/pins.sh"
  has_text "$mb" "version_compare('< $NM_PIN_TOOL_MESON_MIN')" || die "Mesa's meson.build no longer asks meson $NM_PIN_TOOL_MESON_MIN: update build/pins.sh"
  has_text "$mb" "_glslang_preamble_version = '$NM_PIN_TOOL_GLSLANG_MIN'" || die "Mesa's meson.build no longer asks glslang $NM_PIN_TOOL_GLSLANG_MIN: update build/pins.sh"
  local b; for b in $NM_PIN_BINDGEN_BAD; do has_text "$mb" "'$b'" || die "Mesa's meson.build no longer lists bindgen $b as bad: update build/pins.sh"; done
  say "  ok       Mesa's meson.build asks what build/pins.sh says (Python $NM_PIN_TOOL_PYTHON_MIN, meson $NM_PIN_TOOL_MESON_MIN, glslang $NM_PIN_TOOL_GLSLANG_MIN, bindgen not $NM_PIN_BINDGEN_BAD)"
}

# ---------------------------------------------------------------------------------------------------------- deps
ask() {   # <question>: yes with --yes; otherwise a y/N answer from the terminal; no terminal = no
  [ "$NM_ASSUME_YES" = 1 ] && { say "$1 [y/N] yes (--yes)"; return 0; }
  if [ -t 0 ] && [ -r /dev/tty ]; then
    local a; printf '%s [y/N] ' "$1"; read -r a < /dev/tty || a=
    case "$a" in y|Y|yes|YES|Yes) return 0 ;; esac
  else say "$1 [y/N] no (no terminal; run with --yes to agree)"; fi
  return 1
}
xcode_help() {
  cat <<EOF
Xcode cannot be installed by a script. Do this once:
  1. Install Xcode $NM_PIN_TOOL_XCODE_MIN or later from the App Store (or developer.apple.com/download, "Xcode 26").
  2. In Terminal:  sudo xcode-select --switch /Applications/Xcode.app/Contents/Developer
  3.               sudo xcodebuild -license accept
  4.               xcodebuild -runFirstLaunch
  Several Xcodes: set DEVELOPER_DIR=/Applications/Xcode-26.app/Contents/Developer (or in the configuration file)
  instead of step 2. The command line tools alone are not enough: the plugin needs the macOS 26 SDK's Metal 4 headers.
Then run ./build.sh doctor again.
EOF
}
brew_llvm_is_pinned() { local j; j=$("$BREW" info --json=v2 llvm 2>/dev/null || true); printf '%s\n' "$j" | grep -c "\"stable\": *\"$NM_PIN_TOOL_LLVM_MAJOR\." >/dev/null; }
brew_formula_exists() { "$BREW" info --json=v2 "$1" >/dev/null 2>&1; }
brew_installed() { [ -d "$BREW_PREFIX/opt/$1" ]; }
cmd_deps() {
  is_mac || die "deps installs Mac tools; this is $(uname -s). On this machine only doctor, fetch, test, env and --dry-run work"
  find_tools
  local missing_xcode=0 f want=() llvmf nobrew=0
  xcode-select -p >/dev/null 2>&1 && xcodebuild -version >/dev/null 2>&1 || missing_xcode=1
  if [ "$missing_xcode" = 1 ]; then warn "Xcode is missing or not selected"; xcode_help; fi
  # Homebrew
  if [ -z "$BREW" ]; then
    say "Homebrew is not installed. build.sh uses it for: $NM_PIN_BREW_FORMULAS."
    say "Its official installer is: /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
    say "It asks for your password itself and installs into $([ "$(uname -m)" = arm64 ] && echo /opt/homebrew || echo /usr/local)."
    if [ "$NM_DRY_RUN" = 1 ]; then
      say "    (dry run: build.sh would ask you to type yes, then run that installer)"; nobrew=1
    elif [ "$NM_NO_INSTALL" = 1 ] || [ "$NM_OFFLINE" = 1 ]; then die "Homebrew is missing (not installed: --no-install or --offline)"
    elif [ -t 0 ] && [ -r /dev/tty ]; then
      local a; printf 'Type yes to run Homebrew'"'"'s official installer now (--yes does not count for this): '; read -r a < /dev/tty || a=
      [ "$a" = yes ] || die "Homebrew is needed; install it from https://brew.sh and run ./build.sh deps again"
      /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" || die "Homebrew's installer failed"
      find_tools; [ -n "$BREW" ] || die "Homebrew is still not found after its installer ran"
    else die "Homebrew is missing; install it from https://brew.sh (build.sh runs its installer only after a typed yes at a terminal)"; fi
  fi
  # formulas for what is missing
  [ -n "$T_PATCH" ] || want+=( gpatch )
  [ -n "$T_NINJA" ] || want+=( ninja )
  [ -n "$T_CMAKE" ] || want+=( cmake )
  [ -n "$T_PKGCONFIG" ] || want+=( pkgconf )
  [ -n "$T_GLSLANG" ] || want+=( glslang )
  [ -n "$PC_SPIRV_TOOLS" ] || want+=( spirv-tools )
  [ -n "$T_PYTHON" ] && "$T_PYTHON" --version 2>&1 | version_of | { read -r v; ver_ge "$v" "$NM_PIN_TOOL_PYTHON_TESTS_MIN"; } || want+=( "$NM_PIN_TOOL_PYTHON_FORMULA" )
  if [ -z "$LLVM_PREFIX" ] && [ "$nobrew" = 1 ]; then want+=( "llvm@$NM_PIN_TOOL_LLVM_MAJOR" )
  elif [ -z "$LLVM_PREFIX" ]; then
    llvmf=""
    if brew_formula_exists "llvm@$NM_PIN_TOOL_LLVM_MAJOR"; then llvmf=llvm@$NM_PIN_TOOL_LLVM_MAJOR
    elif brew_llvm_is_pinned; then llvmf=llvm
    else warn "Homebrew has no LLVM $NM_PIN_TOOL_LLVM_MAJOR formula today: install LLVM $NM_PIN_TOOL_LLVM_MAJOR.x yourself (with its clang libraries) and set NM_LLVM_PREFIX to it"; fi
    [ -z "$llvmf" ] || want+=( "$llvmf" )
  fi
  if [ -z "$PC_SPIRV_LLVM" ] && [ "$nobrew" = 1 ]; then want+=( spirv-llvm-translator )
  elif [ -z "$PC_SPIRV_LLVM" ]; then
    # Homebrew builds spirv-llvm-translator against its current llvm; only then is it the matching version
    if brew_llvm_is_pinned; then want+=( spirv-llvm-translator )
    else warn "Homebrew's spirv-llvm-translator follows its current llvm, which is not $NM_PIN_TOOL_LLVM_MAJOR: build SPIRV-LLVM-Translator (branch llvm_release_${NM_PIN_TOOL_LLVM_MAJOR}0) against LLVM $NM_PIN_TOOL_LLVM_MAJOR and set PKG_CONFIG_PATH to its lib/pkgconfig"; fi
  fi
  if [ ${#want[@]} -gt 0 ]; then
    say "Homebrew formulas to install (only these; nothing of yours is upgraded or removed by build.sh): ${want[*]}"
    say "Homebrew itself may upgrade an outdated dependency of a formula it installs."
    if [ "$NM_NO_INSTALL" = 1 ]; then say "not installing (--no-install)"
    elif [ "$NM_OFFLINE" = 1 ]; then say "not installing (--offline)"
    elif [ "$NM_DRY_RUN" = 1 ]; then for f in "${want[@]}"; do say "    HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_UPGRADE=1 brew install $f"; done
    elif ask "Install them now?"; then
      for f in "${want[@]}"; do
        say "brew install $f"
        HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_UPGRADE=1 HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK=1 HOMEBREW_NO_ENV_HINTS=1 \
          "$BREW" install "$f" || die "brew install $f failed"
      done
    else say "not installed; the build needs them"; fi
    find_tools
  fi
  deps_python
  deps_rust
  if [ "$NM_DRY_RUN" = 1 ]; then say "deps: dry run; the commands above would run, nothing was installed"; return 0; fi
  make_tool_links
  say ""
  doctor >/dev/null 2>&1 || true
  if [ "$missing_xcode" = 1 ]; then die "Xcode is still missing (see above)"; fi
  if [ "$D_MISSING" != 0 ]; then say "deps: still missing:"; printf '%s' "$D_LINES"; exit 1; fi
  say "deps: everything required is present"
}
write_requirements() {   # pip --require-hashes file from build/pins.sh
  local p spec h
  for p in $NM_PIN_PY_PACKAGES; do
    spec=$(pin "PY_$p"); printf '%s' "$spec"
    for h in $(pin "PY_${p}_HASHES"); do printf ' --hash=sha256:%s' "$h"; done
    printf '\n'
  done
}
deps_python() {
  local py want_new=0
  if [ -x "$VENV/bin/python3" ] && [ -x "$VENV/bin/meson" ] && [ "$(cat "$VENV/.nm-requirements" 2>/dev/null)" = "$(write_requirements | sha256_stdin)" ]; then
    say "Python environment: $VENV (up to date)"; return 0
  fi
  if [ -z "$T_PYTHON" ] && [ "$NM_DRY_RUN" = 1 ]; then
    say "    <Python from Homebrew $NM_PIN_TOOL_PYTHON_FORMULA> -m venv $(q "$VENV"); pip install --require-hashes --no-deps -r <requirements>"; return 0
  fi
  [ -n "$T_PYTHON" ] || { warn "no Python $NM_PIN_TOOL_PYTHON_MIN or later: the Python environment cannot be made yet"; return 0; }
  if [ "$NM_NO_INSTALL" = 1 ]; then say "not creating $VENV (--no-install)"; return 0; fi
  [ "$NM_OFFLINE" = 0 ] || { warn "offline: $VENV cannot be filled"; return 0; }
  say "Python environment: $VENV from $T_PYTHON, packages pinned with their SHA-256 in build/pins.sh"
  if [ "$NM_DRY_RUN" = 1 ]; then say "    $(q "$T_PYTHON") -m venv $(q "$VENV"); pip install --require-hashes --no-deps -r <requirements>"; return 0; fi
  [ ! -d "$VENV" ] || safe_rm "$VENV"
  mkdir -p "$TOOLS"
  "$T_PYTHON" -m venv "$VENV" || die "could not create $VENV"
  write_requirements > "$TOOLS/requirements.txt"
  PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1 "$VENV/bin/python3" -m pip install -q --require-hashes --no-deps -r "$TOOLS/requirements.txt" \
    || die "pip could not install the pinned packages (see above)"
  write_requirements | sha256_stdin > "$VENV/.nm-requirements"
  : "$want_new"
}
deps_rust() {
  local init sha url
  if [ -x "$RUST_BIN/rustc" ] && [ -d "$("$RUST_BIN/rustc" --print target-libdir --target x86_64-apple-darwin 2>/dev/null)" ] \
     && [ -x "$CARGO_DIR/bin/bindgen" ] && [ -x "$CARGO_DIR/bin/cbindgen" ]; then say "Rust: $RUST_BIN (present)"; return 0; fi
  if [ "$NM_NO_INSTALL" = 1 ]; then say "not installing Rust (--no-install)"; return 0; fi
  [ "$NM_OFFLINE" = 0 ] || { warn "offline: Rust cannot be installed"; return 0; }
  case "$HOST_TRIPLE" in aarch64-*) sha=$NM_PIN_RUSTUP_SHA256_AARCH64 ;; *) sha=$NM_PIN_RUSTUP_SHA256_X86_64 ;; esac
  url="${NM_RUSTUP_URL:-$NM_PIN_RUSTUP_URL}/$HOST_TRIPLE/rustup-init"
  init=$TOOLS/downloads/rustup-init-$NM_PIN_RUSTUP_VERSION-$HOST_TRIPLE
  say "Rust $NM_PIN_RUST_VERSION into $RUSTUP_DIR and $CARGO_DIR (rustup-init $NM_PIN_RUSTUP_VERSION, --no-modify-path: nothing is written to your shell files)"
  if [ "$NM_DRY_RUN" = 1 ]; then
    say "    curl -fsSL $(q "$url") -o $(q "$init")   (SHA-256 $sha)"
    say "    RUSTUP_HOME=$(q "$RUSTUP_DIR") CARGO_HOME=$(q "$CARGO_DIR") $(q "$init") -y --no-modify-path --profile minimal --default-toolchain none"
    say "    rustup toolchain install $NM_PIN_RUST_VERSION --profile minimal; rustup target add --toolchain $NM_PIN_RUST_VERSION x86_64-apple-darwin"
    say "    cargo install --locked bindgen-cli@$NM_PIN_BINDGEN_VERSION cbindgen@$NM_PIN_CBINDGEN_VERSION"
    return 0
  fi
  ask "Install Rust $NM_PIN_RUST_VERSION, bindgen $NM_PIN_BINDGEN_VERSION and cbindgen $NM_PIN_CBINDGEN_VERSION into $TOOLS?" || { say "Rust not installed; the build needs it"; return 0; }
  mkdir -p "$TOOLS/downloads"
  if [ ! -f "$init" ] || [ "$(sha256_file "$init")" != "$sha" ]; then
    curl -fsSL --proto '=https' --tlsv1.2 --retry 3 -o "$init.part" "$url" || die "could not download $url"
    [ "$(sha256_file "$init.part")" = "$sha" ] || { rm -f "$init.part"; die "$url does not have the pinned SHA-256 $sha"; }
    mv -f "$init.part" "$init"; chmod 755 "$init"
  fi
  local renv=( "RUSTUP_HOME=$RUSTUP_DIR" "CARGO_HOME=$CARGO_DIR" "PATH=$CARGO_DIR/bin:/usr/bin:/bin:/usr/sbin:/sbin" )
  [ -z "${RUSTUP_DIST_SERVER:-}" ] || renv+=( "RUSTUP_DIST_SERVER=$RUSTUP_DIST_SERVER" )
  env "${renv[@]}" "$init" -y -q --no-modify-path --profile minimal --default-toolchain none || die "rustup-init failed"
  env "${renv[@]}" "$CARGO_DIR/bin/rustup" toolchain install "$NM_PIN_RUST_VERSION" --profile minimal || die "rustup could not install $NM_PIN_RUST_VERSION"
  env "${renv[@]}" "$CARGO_DIR/bin/rustup" target add --toolchain "$NM_PIN_RUST_VERSION" x86_64-apple-darwin || die "rustup could not add the x86_64-apple-darwin target"
  env "${renv[@]}" "RUSTUP_TOOLCHAIN=$NM_PIN_RUST_VERSION" "$CARGO_DIR/bin/cargo" install --locked --root "$CARGO_DIR" --version "$NM_PIN_BINDGEN_VERSION" bindgen-cli || die "cargo install bindgen-cli failed"
  env "${renv[@]}" "RUSTUP_TOOLCHAIN=$NM_PIN_RUST_VERSION" "$CARGO_DIR/bin/cargo" install --locked --root "$CARGO_DIR" --version "$NM_PIN_CBINDGEN_VERSION" cbindgen || die "cargo install cbindgen failed"
}
# tools/bin: one link per tool the build may take from outside Xcode; it is the first folder on every step's PATH.
make_tool_links() {
  mkdir -p "$TBIN" "$THOST"
  local l
  for l in "$TBIN"/*; do [ -L "$l" ] && rm -f "$l"; done
  [ -n "$T_PATCH" ] && ln -s "$T_PATCH" "$TBIN/patch"
  [ -n "$T_NINJA" ] && ln -s "$T_NINJA" "$TBIN/ninja"
  [ -n "$T_CMAKE" ] && ln -s "$T_CMAKE" "$TBIN/cmake"
  [ -n "$T_PKGCONFIG" ] && ln -s "$T_PKGCONFIG" "$TBIN/pkg-config"
  [ -n "$T_GLSLANG" ] && ln -s "$T_GLSLANG" "$TBIN/glslangValidator"
  [ -n "$T_LLVMCONFIG" ] && ln -s "$T_LLVMCONFIG" "$TBIN/llvm-config"
  [ -n "$T_LLVMSPIRV" ] && ln -s "$T_LLVMSPIRV" "$TBIN/llvm-spirv"
  if [ -x "$VENV/bin/python3" ]; then
    ln -s "$VENV/bin/python3" "$TBIN/python3"
    for l in meson mako-render; do [ -x "$VENV/bin/$l" ] && ln -s "$VENV/bin/$l" "$TBIN/$l"; done
  fi
  for l in rustc cargo rustdoc; do [ -x "$RUST_BIN/$l" ] && ln -s "$RUST_BIN/$l" "$TBIN/$l"; done
  for l in bindgen cbindgen; do [ -x "$CARGO_DIR/bin/$l" ] && ln -s "$CARGO_DIR/bin/$l" "$TBIN/$l"; done
  for l in ssh rsync scp; do ln -sfn "$HERE/build/local_build_host.sh" "$THOST/$l"; done
  return 0
}

# ---------------------------------------------------------------------------------------------------------- components
ALL_COMPONENTS="rm kms nvrm fb accel agdc nmsmn ryzenadj airdis dlss osver nvk boost vulkan plugin xlate nvencvt"
EXTRA_COMPONENTS="selector nmkbl nmhda"
# comp_info <name>: C_DEPS (components), C_SRC (checkout paths whose contents it is built from, scripts included),
# C_VARS (variables it reads), C_PINS (the build/pins.sh entries it is built from, by name prefix), C_OUT (a file that
# exists when it was built), C_TOOLS (tool groups), C_DESC
comp_info() {
  C_DEPS=""; C_SRC=""; C_VARS=""; C_PINS=""; C_OUT=""; C_TOOLS="xcode"; C_DESC=""
  case "$1" in
    rm)    C_DESC="NVIDIA's RM core for macOS (libnvkernel.a), patched with kexts/NVRM/patches"
           C_SRC="build/build_rm620.sh kexts/NVRM/patches"; C_VARS="NV_VERSION NM_OGKM620_PRISTINE"; C_PINS="OGKM620_"; C_TOOLS="xcode patch python"
           C_OUT="$OGKM620/src/nvidia/_out/Darwin_x86_64/libnvkernel.a" ;;
    kms)   C_DESC="NVIDIA's modeset core for macOS (libnvkms.a)"; C_DEPS="rm"
           C_SRC="build/build_kms620.sh"; C_VARS="NV_VERSION"; C_TOOLS="xcode python"
           C_OUT="$OGKM620/src/nvidia-modeset/_out/Darwin_x86_64/libnvkms.a" ;;
    nvrm)  C_DESC="NVRM.kext"; C_DEPS="rm kms"
           C_SRC="build/build_nvrm.sh build/warn_ceiling.sh build/warnings.txt kexts/NVRM tools/test_patch_chain.sh"; C_VARS="KMS_ARCHIVE RE"; C_PINS="OGKM620_"; C_TOOLS="xcode python patch"
           C_OUT="$O/kexts/nvrm/NVRM.kext/Contents/MacOS/NVRM" ;;
    fb)    C_DESC="NVRMFB.kext"; C_DEPS="rm"
           C_SRC="build/fb_build.sh build/warn_ceiling.sh build/warnings.txt kexts/NVRMFB"; C_VARS="MINVER FB_DEFS"
           C_OUT="$O/kexts/fb/NVRMFB.kext/Contents/MacOS/NVRMFB" ;;
    accel) C_DESC="NVAccel.kext for macOS $(accel_majors | sed 's/ / and /')"; C_DEPS="rm"
           C_SRC="build/accel_build.sh build/accel_build_all.sh build/warn_ceiling.sh build/warnings.txt kexts/NVRM"; C_VARS="RE NM_ACCEL MACOSX_DEPLOYMENT_TARGET"
           C_OUT="$O/accel-all/$(accel_majors | sed 's/ .*//')/NVAccel.kext/Contents/MacOS/NVAccel" ;;
    agdc)  C_DESC="NVRMAGDC.kext"; C_SRC="build/build_agdc.sh build/warn_ceiling.sh build/warnings.txt kexts/NVRMAGDC"
           C_OUT="$O/kexts/agdc/NVRMAGDC.kext/Contents/MacOS/NVRMAGDC" ;;
    nmsmn) C_DESC="NMSMN.kext (carried in the package)"; C_SRC="kexts/NMSMN build/warn_ceiling.sh build/warnings.txt"
           C_OUT="$O/kexts/nmsmn/NMSMN.kext/Contents/MacOS/NMSMN" ;;
    ryzenadj) C_DESC="ryzenadj (carried in the package)"; C_SRC="tools/ryzenadj-macos"; C_PINS="RYZENADJ_"; C_OUT="$O/ryzenadj/ryzenadj" ;;
    airdis) C_DESC="nvmtl-air-dis (the plugin's AIR decoder)"; C_SRC="build/build_air_dis.sh plugin/air_dis.c"; C_OUT="$O/userland/nvmtl-air-dis" ;;
    dlss)  C_DESC="dlss_cache and nm_unzstd"; C_SRC="build/build_dlss_cache.sh dlss package/dlss_fetch.sh"; C_OUT="$O/dlss-pkg/dlss/nm_unzstd" ;;
    osver) C_DESC="libnvmtl_osver.dylib"; C_SRC="build/build_osver.sh plugin"; C_OUT="$O/osver/libnvmtl_osver.dylib" ;;
    nvk)   C_DESC="NVK, the Vulkan driver (libvulkan_nouveau.dylib)"
           C_SRC="build/build_nvk.sh build/gen_nvk_rust_crates.py nvk licenses/nvk-rust-crates.txt"
           C_VARS="NVK_SYSROOT NVK_EXTRA_PATCHES NVK_SKIP_PATCHES NVK_REF_EXPORTS NVK_JOBS NVK_PATH PKG_CONFIG_PATH LIBCLANG_PATH NM_MESA_GIT NM_OGKM610"
           C_PINS="MESA_ OGKM610_ RUST_VERSION= BINDGEN_ CBINDGEN_ PY_ TOOL_LLVM_MAJOR="
           C_TOOLS="xcode patch python nvk rust"; C_OUT="$O/nvk/libvulkan_nouveau.dylib" ;;
    boost) C_DESC="nvmtl-boost"; C_DEPS="nvk"; C_SRC="tools/nvmtl-boost"; C_OUT="$O/boost/nvmtl-boost" ;;
    vulkan) C_DESC="the Khronos Vulkan loader"; C_SRC="build/build_vulkan_loader.sh"; C_VARS="NM_KHRONOS_URL"; C_PINS="VKHEADERS_ VKLOADER_"; C_TOOLS="xcode cmake"
           C_OUT="$O/vulkan-loader/libvulkan.1.dylib" ;;
    plugin) C_DESC="the Metal plugin (NVMTLDriver, $NM_FLAVOUR)"; C_SRC="build/build_plugin_release.sh build/build_plugin_dev.sh build/local_build_host.sh build/warn_ceiling.sh build/warnings.txt plugin"
           C_VARS="NM_FLAVOUR NULLMOTH_BUILD_HOST NM_VULKAN_INCLUDE NM_MESA_GIT"; C_PINS="MESA_"; C_OUT="$O/plugin/NVMTLDriver" ;;
    xlate) C_DESC="the shader translator (libnvmtl_translate.dylib)"; C_SRC="build/build_xlate_release.sh build/local_build_host.sh build/warn_ceiling.sh build/warnings.txt translator"
           C_VARS="NULLMOTH_BUILD_HOST NULLMOTH_RUST_VERSION NULLMOTH_RUST_BIN NULLMOTH_CARGO_HOME"; C_PINS="RUST_VERSION="; C_TOOLS="xcode rust"; C_OUT="$O/xlate/libnvmtl_translate.dylib" ;;
    nvencvt) C_DESC="libnvencvt.dylib (video)"; C_DEPS="rm"
           C_SRC="build/build_nvencvt.sh build/local_build_host.sh build/warn_ceiling.sh build/warnings.txt vendor/nvenc tools/nvencvt_header_abi.py tools/nvk_header_abi.py"
           C_VARS="NULLMOTH_BUILD_HOST"; C_PINS="OGKM610_ OGKM620_ OPENGPUDOC_ WEBKIT_"; C_OUT="$O/nvencvt/libnvencvt.dylib" ;;
    selector) C_DESC="NVPrimarySelector.kext (optional)"; C_SRC="build/build_primary_selector.sh kexts/NVPrimarySelector build/warn_ceiling.sh build/warnings.txt"
           C_VARS="KHDR KLIB"; C_OUT="$O/selector/NVPrimarySelector.kext/Contents/MacOS/NVPrimarySelector" ;;
    nmkbl) C_DESC="NMKbdBacklight.kext (optional; needs Lilu, VirtualSMC, MacKernelSDK)"; C_SRC="kexts/NMKbdBacklight build/check_kext_libraries.sh"
           C_VARS="NM_LILU_DEBUG_KEXT NM_VIRTUALSMC_SRC NM_MACKERNELSDK"; C_OUT="$O/kexts/nmkbl/NMKbdBacklight.kext/Contents/MacOS/NMKbdBacklight" ;;
    nmhda) C_DESC="NMHDA.kext (optional; needs Lilu, MacKernelSDK)"; C_SRC="kexts/NMHDA build/check_kext_libraries.sh"
           C_VARS="NM_LILU_DEBUG_KEXT NM_MACKERNELSDK"; C_OUT="$O/kexts/nmhda/NMHDA.kext/Contents/MacOS/NMHDA" ;;
    package) C_DESC="the package directory"; C_DEPS="$ALL_COMPONENTS"
           C_SRC="build/package_local.sh package licenses"; C_VARS="NM_BASE_PACKAGE NM_BASE_PACKAGE_SHA256 NM_PACKAGE_VERSION"; C_TOOLS="xcode"
           C_OUT="$PKGDIR/pkgroot/SHA256SUMS" ;;
    *) return 1 ;;
  esac
  # NM_OGKM620 names a tree whose archives someone built already: the two archive steps then have nothing to do
  return 0
}
accel_majors() { case "$NM_ACCEL" in 15) echo 15 ;; 26) echo 26 ;; *) echo "15 26" ;; esac; }

tree_hash() {   # contents of the checkout's files under the given paths (tracked and untracked, as they are now)
  local t; t=$(tmpfile).paths
  ( cd "$HERE" && git ls-files -z -c -o --exclude-standard -- "$@" | tr '\0' '\n' | LC_ALL=C sort -u |
      while IFS= read -r f; do [ -f "$f" ] && [ ! -L "$f" ] && printf '%s\n' "$f"; done > "$t"
    if [ -s "$t" ]; then git hash-object --stdin-paths < "$t" | paste -d' ' - "$t"; fi ) | sha256_stdin
  rm -f "$t"
}
TOOLS_ID=""
tools_id() {   # what the compilers are: part of every stamp
  [ -n "$TOOLS_ID" ] && { printf '%s' "$TOOLS_ID"; return; }
  TOOLS_ID="host $(uname -m)"
  if is_mac; then
    TOOLS_ID="$TOOLS_ID; xcode $( { xcodebuild -version 2>/dev/null || true; } | tr '\n' ' '); sdk $(xcrun --show-sdk-version 2>/dev/null || true)"
    TOOLS_ID="$TOOLS_ID; clang $( { xcrun clang --version 2>/dev/null || true; } | first_line); ld $( { xcrun ld -v 2>&1 || true; } | first_line)"
  fi
  printf '%s' "$TOOLS_ID"
}
recipe() {   # <component>: everything its output depends on, as text
  local n=$1 v d
  comp_info "$n"
  say "component $n"
  say "build.sh $(sha256_file "$SELF")"
  # only the pins of the sources this component is built from: another pin, or a comment, changes nothing here
  local pre; for pre in $C_PINS; do grep -E "^NM_PIN_$pre" "$PINS_FILE" | sed 's/^/pin /' || true; done
  say "tools $(tools_id)"
  case " $C_TOOLS " in *" nvk "*)
    say "llvm $([ -n "$T_LLVMCONFIG" ] && "$T_LLVMCONFIG" --version 2>/dev/null) spirv-llvm $(pc_version "$PC_SPIRV_LLVM/LLVMSPIRVLib.pc" 2>/dev/null) spirv-tools $(pc_version "$PC_SPIRV_TOOLS/SPIRV-Tools.pc" 2>/dev/null)"
    say "glslang $([ -n "$T_GLSLANG" ] && "$T_GLSLANG" --version 2>/dev/null | first_line) meson $([ -x "$VENV/bin/meson" ] && "$VENV/bin/meson" --version)" ;;
  esac
  case " $C_TOOLS " in *" rust "*) say "rust $([ -x "$RUST_BIN/rustc" ] && "$RUST_BIN/rustc" --version)" ;; esac
  for v in $C_VARS; do eval "say \"var $v=\${$v:-}\""; done
  say "src $(tree_hash $C_SRC)"
  for d in $C_DEPS; do say "dep $d $(sha256_file "$STAMPS/$d" 2>/dev/null || echo none)"; done
  return 0
}

STATUS_LINES=""
status_line() { STATUS_LINES="$STATUS_LINES$(printf '  %-9s %-11s %s' "$1" "$2" "$3")
"; }
DONE=" "
# build_component <name>: its dependencies first, then itself unless its stamp says it is up to date
build_component() {
  local n=$1 d r
  case "$DONE" in *" $n "*) return 0 ;; esac
  comp_info "$n" || die "unknown component '$n' (\"./build.sh help\" lists them)"
  for d in $C_DEPS; do build_component "$d"; done
  comp_info "$n"
  if skip_reason=$(component_skip "$n"); then
    say "== $n: skipped ($skip_reason)"; status_line "$n" skipped "$skip_reason"; DONE="$DONE$n "; return 0
  fi
  r=$(tmpfile).recipe
  recipe "$n" > "$r"
  if [ "$NM_FORCE" = 0 ] && [ -f "$STAMPS/$n" ] && cmp -s "$r" "$STAMPS/$n" && [ -e "$C_OUT" ]; then
    say "== $n: up to date ($C_DESC)"; status_line "$n" "up to date" "$(printf '%s' "$C_OUT" | tilde)"; DONE="$DONE$n "; return 0
  fi
  say "== $n: building $C_DESC"
  if [ "$NM_DRY_RUN" = 1 ]; then
    [ -f "$STAMPS/$n" ] && say "   (its stamp differs: $(diff "$STAMPS/$n" "$r" 2>/dev/null | grep '^>' | sed 's/^> //' | cut -c1-60 | tr '\n' ';' | cut -c1-200))"
    CUR_LOG=/dev/null; run_component "$n"; status_line "$n" "would build" "$C_DESC"; DONE="$DONE$n "; return 0
  fi
  rm -f "$STAMPS/$n"
  CUR_STEP=$n; CUR_LOG=$LOGS/$n.log
  { say "build.sh step $n, $(date -u +%Y-%m-%dT%H:%M:%SZ)"; cat "$r"; say "----"; } > "$CUR_LOG"
  local t0 rc=0; t0=$(date +%s)
  run_component "$n" || rc=$?
  [ "$rc" = 0 ] || fail_step "$n" "$rc"
  [ -e "$C_OUT" ] || { say "the step ended without $C_OUT" >> "$CUR_LOG"; fail_step "$n" 1; }
  mv -f "$r" "$STAMPS/$n"
  say "   done in $(( $(date +%s) - t0 )) s: $(grep -v '^+ ' "$CUR_LOG" | tail -n 1 | cut -c1-150 | tilde)"
  status_line "$n" built "$(printf '%s' "$C_OUT" | tilde)"
  CUR_STEP=""; DONE="$DONE$n "
}
component_skip() {   # prints a reason and succeeds when the component is not built in this configuration
  case "$1" in
    rm|kms) [ -z "${NM_OGKM620:-}" ] || { printf 'NM_OGKM620 names a tree whose archives are built: %s' "$OGKM620"; return 0; } ;;
    nmkbl) [ -n "${NM_LILU_DEBUG_KEXT:-}" ] && [ -n "${NM_VIRTUALSMC_SRC:-}" ] && [ -n "${NM_MACKERNELSDK:-}" ] || { printf 'NM_LILU_DEBUG_KEXT, NM_VIRTUALSMC_SRC and NM_MACKERNELSDK are not all set'; return 0; } ;;
    nmhda) [ -n "${NM_LILU_DEBUG_KEXT:-}" ] && [ -n "${NM_MACKERNELSDK:-}" ] || { printf 'NM_LILU_DEBUG_KEXT and NM_MACKERNELSDK are not both set'; return 0; } ;;
  esac
  return 1
}

# The commands. Each runs the existing script as the release does; "x" logs it.
host_env() {   # the release scripts' build Mac is this Mac, unless NULLMOTH_BUILD_HOST names another one
  printf '%s\n' "NULLMOTH_BUILD_HOST=$BUILD_HOST"
  if [ "$BUILD_HOST" = "$LOCAL_HOST" ]; then printf '%s\n' "PATH=$THOST:$STEP_PATH" "NM_LOCAL_BUILD_HOST=$LOCAL_HOST"; fi
  [ "$NM_OFFLINE" = 1 ] && printf '%s\n' CARGO_NET_OFFLINE=true
  [ -z "${DEVELOPER_DIR:-}" ] || printf '%s\n' "DEVELOPER_DIR=$DEVELOPER_DIR"
}
lines_to_array() { ARR=(); local l; while IFS= read -r l; do [ -n "$l" ] && ARR+=( "$l" ); done; }
REV=""; REV_NOTE=""
source_rev() {   # the revision the git-archive based scripts build: HEAD, or the working tree as a commit object
  [ -n "$REV" ] && return 0
  if [ -z "$(git -C "$HERE" status --porcelain --untracked-files=no)" ]; then REV=$(git -C "$HERE" rev-parse HEAD); REV_NOTE="commit $REV"
  else
    REV=$(GIT_AUTHOR_NAME=build.sh GIT_AUTHOR_EMAIL=build@localhost GIT_COMMITTER_NAME=build.sh GIT_COMMITTER_EMAIL=build@localhost \
          git -C "$HERE" stash create "build.sh: working tree" 2>/dev/null || true)
    [ -n "$REV" ] || die "could not record the working tree's changes for the plugin, translator and video builds (git stash create)"
    REV_NOTE="the working tree of $(git -C "$HERE" rev-parse --short HEAD) with its uncommitted changes (as commit object $REV; untracked files are not part of it)"
  fi
}
run_component() {
  local n=$1 m
  make_step_env
  case "$n" in
    rm)
      [ ! -e "$RMWORK" ] || { in_workdir "$RMWORK" || die "$RMWORK is outside the working directory; build.sh rebuilds it from scratch only inside"; safe_rm "$RMWORK"; }
      x mkdir -p "$RMWORK"
      x "${STEP_ENV[@]}" "NV_VERSION=$NM_PIN_OGKM620_REF" bash "$HERE/build/build_rm620.sh" "$RMWORK" "$PRISTINE"
      # build_rm620.sh archives whatever objects exist and exits 0 when compiles fail; its "failed:" line says so
      [ "$NM_DRY_RUN" = 1 ] || grep -q '^failed: 0 of ' "$CUR_LOG" || { say "build_rm620.sh reported compile failures (it does not stop on them): see 'failed:' above" | tee -a "$CUR_LOG"; return 3; }
      ;;
    kms)  x "${STEP_ENV[@]}" "NV_VERSION=$NM_PIN_OGKM620_REF" bash "$HERE/build/build_kms620.sh" "$RMWORK" ;;
    nvrm)
      lines_to_array < <(passthru KMS_ARCHIVE RE)
      x "${STEP_ENV[@]}" "NVRM_PRISTINE_OGKM=$PRISTINE" ${ARR[@]+"${ARR[@]}"} bash "$HERE/build/build_nvrm.sh" "$HERE/kexts/NVRM" "$OGKM620" "$O/kexts/nvrm" ;;
    fb)
      [ ! -e "$O/kexts/fb" ] || safe_rm "$O/kexts/fb"
      lines_to_array < <(passthru MINVER FB_DEFS)
      x "${STEP_ENV[@]}" "OGKM=$OGKM620" ${ARR[@]+"${ARR[@]}"} bash "$HERE/build/fb_build.sh" "$HERE/kexts" "$O/kexts/fb" ;;
    accel)
      lines_to_array < <(passthru RE)
      [ ! -e "$O/accel-all" ] || safe_rm "$O/accel-all"
      if [ "$NM_ACCEL" = both ]; then
        x "${STEP_ENV[@]}" "OGKM=$OGKM620" ${ARR[@]+"${ARR[@]}"} bash "$HERE/build/accel_build_all.sh" "$HERE/kexts/NVRM" "$O/accel-all"
      elif [ "$NM_ACCEL" = 26 ]; then
        x "${STEP_ENV[@]}" "OGKM=$OGKM620" NM_TAHOE=1 ${ARR[@]+"${ARR[@]}"} bash "$HERE/build/accel_build.sh" "$HERE/kexts/NVRM" "$O/accel-all/26"
      else
        x "${STEP_ENV[@]}" "OGKM=$OGKM620" ${ARR[@]+"${ARR[@]}"} bash "$HERE/build/accel_build.sh" "$HERE/kexts/NVRM" "$O/accel-all/15"
      fi
      for m in 15 26; do
        local d=accel; [ "$m" = 26 ] && d=accel26
        [ ! -e "$O/kexts/$d" ] || safe_rm "$O/kexts/$d"
        case " $(accel_majors) " in *" $m "*) x mkdir -p "$O/kexts/$d"; x cp -R "$O/accel-all/$m/NVAccel.kext" "$O/kexts/$d/" ;; esac
      done ;;
    agdc)  x "${STEP_ENV[@]}" bash "$HERE/build/build_agdc.sh" "$HERE/kexts/NVRMAGDC" "$O/kexts/agdc" ;;
    nmsmn)
      [ ! -e "$O/kexts/nmsmn" ] || safe_rm "$O/kexts/nmsmn"
      x "${STEP_ENV[@]}" bash "$HERE/kexts/NMSMN/build.sh" "$O/kexts/nmsmn" 15.0 ;;
    ryzenadj)
      [ ! -e "$O/ryzenadj" ] || safe_rm "$O/ryzenadj"
      x "${STEP_ENV[@]}" bash "$HERE/tools/ryzenadj-macos/build.sh" "$RYZENSRC" "$O/ryzenadj" ;;
    airdis) x "${STEP_ENV[@]}" bash "$HERE/build/build_air_dis.sh" "$O/userland" ;;
    dlss)
      [ ! -e "$O/dlss-pkg" ] || safe_rm "$O/dlss-pkg"
      x "${STEP_ENV[@]}" bash "$HERE/build/build_dlss_cache.sh" "$O/dlss-pkg" ;;
    osver) x "${STEP_ENV[@]}" bash "$HERE/build/build_osver.sh" "$O/osver" ;;
    nvk)
      [ "$NM_OFFLINE" = 0 ] || { say "offline: NVK needs the network (meson downloads six Rust crates, checked against Mesa's wrap files, into every fresh Mesa tree build_nvk.sh exports)" | tee -a "$CUR_LOG"; return 2; }
      # mesa_clc and vtn_bindgen2 are reused by build_nvk.sh whenever they exist; they must come from this Mesa and LLVM
      local hostid; hostid="$NM_PIN_MESA_COMMIT $(llvm_version)"
      if [ -d "$O/nvk-build/host" ] && [ "$(cat "$O/nvk-build/host/.nm-host-id" 2>/dev/null)" != "$hostid" ]; then safe_rm "$O/nvk-build/host" "$O/nvk-build/clc-native"; fi
      lines_to_array < <(passthru NVK_SYSROOT NVK_EXTRA_PATCHES NVK_SKIP_PATCHES NVK_REF_EXPORTS)
      x "${STEP_ENV[@]}" "NVK_PATH=${NVK_PATH:-$TBIN}" "PKG_CONFIG_PATH=$(nvk_pkg_config_path)" "LIBCLANG_PATH=${LIBCLANG_PATH:-$LIBCLANG_DIR}" \
        "NVK_JOBS=${NVK_JOBS:-$NM_JOBS}" "RUSTUP_HOME=$RUSTUP_DIR" "CARGO_HOME=$CARGO_DIR" "RUSTUP_TOOLCHAIN=$NM_PIN_RUST_VERSION" \
        ${ARR[@]+"${ARR[@]}"} bash "$HERE/build/build_nvk.sh" "$MESA" "$OGKM610" "$O/nvk-build" 2.9
      [ "$NM_DRY_RUN" = 1 ] || { [ ! -d "$O/nvk-build/host" ] || printf '%s\n' "$hostid" > "$O/nvk-build/host/.nm-host-id"; }
      x mkdir -p "$O/nvk"
      x cp "$O/nvk-build/libvulkan_nouveau.dylib" "$O/nvk-build/BUILD.txt" "$O/nvk/"
      # a library from Homebrew or the build folder would load whatever is there on the user's Mac
      if [ "$NM_DRY_RUN" = 0 ] && otool -L "$O/nvk/libvulkan_nouveau.dylib" | awk 'NR>1{print $1}' | grep -E '^(/usr/local/|/opt/homebrew/|/Users/)'; then
        say "STOP: libvulkan_nouveau.dylib loads a library from outside macOS (above): a Homebrew library leaked into the build" | tee -a "$CUR_LOG"; return 5
      fi ;;
    boost) x mkdir -p "$O/boost"; x "${STEP_ENV[@]}" bash "$HERE/tools/nvmtl-boost/build.sh" "$O/nvk-build/mesa" "$O/boost/nvmtl-boost" ;;
    vulkan)
      x "${STEP_ENV[@]}" "NM_KHRONOS_URL=${NM_KHRONOS_URL:-file://$KHRONOS}" bash "$HERE/build/build_vulkan_loader.sh" "$O/vulkan-loader" ;;
    plugin)
      if [ "$NM_FLAVOUR" = dev ]; then
        lines_to_array < <(passthru NM_VULKAN_INCLUDE)
        x "${STEP_ENV[@]}" ${ARR[@]+"${ARR[@]}"} bash "$HERE/build/build_plugin_dev.sh" "$O/plugin"
      else
        source_rev; lines_to_array < <(host_env)
        x "${STEP_ENV[@]}" "${ARR[@]}" bash "$HERE/build/build_plugin_release.sh" "$REV" "$O/plugin"
      fi ;;
    xlate)
      source_rev; lines_to_array < <(host_env)
      x "${STEP_ENV[@]}" "${ARR[@]}" "NULLMOTH_RUST_VERSION=${NULLMOTH_RUST_VERSION:-$NM_PIN_RUST_VERSION}" "NULLMOTH_RUST_BIN=$RUST_BIN" \
        "NULLMOTH_CARGO_HOME=${NULLMOTH_CARGO_HOME:-$CARGO_DIR}" bash "$HERE/build/build_xlate_release.sh" "$REV" "$O/xlate" ;;
    nvencvt)
      source_rev; lines_to_array < <(host_env)
      x "${STEP_ENV[@]}" "${ARR[@]}" "NM_OGKM620=$OGKM620" "NM_OGKM620_PRISTINE=$PRISTINE" bash "$HERE/build/build_nvencvt.sh" "$REV" "$O/nvencvt" ;;
    selector)
      [ ! -e "$O/selector" ] || safe_rm "$O/selector"
      lines_to_array < <(passthru KHDR KLIB)
      x "${STEP_ENV[@]}" ${ARR[@]+"${ARR[@]}"} bash "$HERE/build/build_primary_selector.sh" "$O/selector" ;;
    nmkbl) x "${STEP_ENV[@]}" bash "$HERE/kexts/NMKbdBacklight/build.sh" "$NM_LILU_DEBUG_KEXT" "$NM_VIRTUALSMC_SRC" "$NM_MACKERNELSDK" "$O/kexts/nmkbl" ;;
    nmhda) x "${STEP_ENV[@]}" bash "$HERE/kexts/NMHDA/build.sh" "$NM_LILU_DEBUG_KEXT" "$NM_MACKERNELSDK" "$O/kexts/nmhda" ;;
    package)
      write_build_info
      lines_to_array < <(passthru NM_BASE_PACKAGE_SHA256 NM_PACKAGE_VERSION)
      x "${STEP_ENV[@]}" "NM_BUILD_INFO=$PKGDIR/build-info.txt" "NM_PACKAGE_NAME=$(package_name)" ${ARR[@]+"${ARR[@]}"} \
        bash "$HERE/build/package_local.sh" "$NM_BASE_PACKAGE" "$O" "$PKGDIR" ;;
  esac
}
nvk_pkg_config_path() {
  local p=""
  [ -z "$PC_SPIRV_LLVM" ] || p=$PC_SPIRV_LLVM
  [ -z "$PC_SPIRV_TOOLS" ] || p="${p:+$p:}$PC_SPIRV_TOOLS"
  [ -z "$LLVM_PREFIX" ] || p="${p:+$p:}$LLVM_PREFIX/lib/pkgconfig"
  [ -z "${PKG_CONFIG_PATH:-}" ] || p="$PKG_CONFIG_PATH${p:+:$p}"
  printf '%s' "$p"
}
package_name() { printf 'nullmoth-nvidia-local-%s.tar.gz' "$(git -C "$HERE" rev-parse --short HEAD 2>/dev/null || echo unknown)"; }
write_build_info() {
  [ "$NM_DRY_RUN" = 1 ] && return 0
  mkdir -p "$PKGDIR"
  source_rev
  {
    say "This package was built with ./build.sh from a checkout of the NullMoth driver, not by its release process."
    say "built:      $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    say "commit:     $(git -C "$HERE" rev-parse HEAD)"
    say "sources:    $REV_NOTE"
    say "flavour:    $NM_FLAVOUR (plugin $([ "$NM_FLAVOUR" = dev ] && echo 'with diagnostics' || echo 'as released'))"
    say "base:       $(basename "$NM_BASE_PACKAGE")"
    say "host:       $(uname -m) $(is_mac && sw_vers -productVersion 2>/dev/null)"
    say "tools:      $(tools_id)"
    say "pins:"; grep -E '^NM_PIN_[A-Z0-9_]*(_REF|_COMMIT|_SHA256)=' "$PINS_FILE" | sed 's/^/  /'
  } | tilde > "$PKGDIR/build-info.txt"
}

# ---------------------------------------------------------------------------------------------------------- build
prepare_build() {   # what every build command needs before its first step
  is_mac || [ "$NM_DRY_RUN" = 1 ] || die "building needs a Mac with Xcode; this is $(uname -s). Here only doctor, fetch, test, env and --dry-run work"
  find_tools
  if [ "$NM_DRY_RUN" = 0 ]; then make_tool_links; fi
  derive_paths
}
need_tools_for() {   # stop early, naming what a component needs and does not have
  local n=$1 miss=""
  [ "$NM_DRY_RUN" = 1 ] && return 0
  comp_info "$n"
  case " $C_TOOLS " in *" xcode "*) xcrun --show-sdk-path >/dev/null 2>&1 || miss="$miss Xcode" ;; esac
  case " $C_TOOLS " in *" patch "*) [ -n "$T_PATCH" ] || miss="$miss GNU-patch" ;; esac
  case " $C_TOOLS " in *" python "*) [ -n "$T_PYTHON" ] || miss="$miss Python-3.10" ;; esac
  case " $C_TOOLS " in *" cmake "*) { [ -n "$T_CMAKE" ] && [ -n "$T_NINJA" ]; } || miss="$miss cmake/ninja" ;; esac
  case " $C_TOOLS " in *" rust "*) [ -x "$RUST_BIN/rustc" ] || miss="$miss Rust-$NM_PIN_RUST_VERSION" ;; esac
  case " $C_TOOLS " in *" nvk "*)
    [ -x "$VENV/bin/meson" ] || miss="$miss meson(venv)"; [ -n "$LLVM_PREFIX" ] || miss="$miss LLVM-$NM_PIN_TOOL_LLVM_MAJOR"
    [ -n "$PC_SPIRV_LLVM" ] || miss="$miss SPIRV-LLVM-Translator"; [ -n "$PC_SPIRV_TOOLS" ] || miss="$miss SPIRV-Tools"
    [ -n "$T_GLSLANG" ] || miss="$miss glslang"; [ -x "$CARGO_DIR/bin/bindgen" ] || miss="$miss bindgen"; [ -x "$CARGO_DIR/bin/cbindgen" ] || miss="$miss cbindgen" ;;
  esac
  [ -z "$miss" ] || die "$n needs:$miss (./build.sh doctor shows details; ./build.sh deps installs them)"
}
need_sources_for() {
  local n=$1
  [ "$NM_DRY_RUN" = 1 ] && return 0
  case "$n" in
    rm|nvrm|nvencvt) [ -f "$PRISTINE/version.mk" ] || die "$n needs NVIDIA's $NM_PIN_OGKM620_REF tree at $PRISTINE: run ./build.sh fetch" ;;
  esac
  case "$n" in
    nvk) [ -f "$OGKM610/version.mk" ] && git -C "$MESA" cat-file -e "$NM_PIN_MESA_COMMIT^{commit}" 2>/dev/null || die "nvk needs Mesa and NVIDIA $NM_PIN_OGKM610_REF: run ./build.sh fetch" ;;
    plugin) [ -f "${NM_VULKAN_INCLUDE:-$MESA/include}/vulkan/vulkan_core.h" ] || die "the plugin needs Mesa's Vulkan headers: run ./build.sh fetch" ;;
    vulkan) [ -n "${NM_KHRONOS_URL:-}" ] || [ -d "$KHRONOS/Vulkan-Loader" ] || die "the loader needs its sources: run ./build.sh fetch" ;;
    ryzenadj) [ -d "$RYZENSRC/.git" ] || die "ryzenadj needs RyzenAdj: run ./build.sh fetch" ;;
    nvencvt) [ -d "$NVHDR/ogd/.git" ] && [ -s "$NVHDR/webkit-spi/VTVideoDecoderSPI.h" ] || die "libnvencvt needs its headers: run ./build.sh fetch" ;;
  esac
  return 0
}
expand_components() {   # names and groups -> component names in build order
  local a out=" " c
  for a in "$@"; do
    case "$a" in
      all) out="$out$ALL_COMPONENTS " ;;
      kexts) out="$out rm kms nvrm fb accel agdc nmsmn " ;;
      userland) out="$out airdis dlss osver nvk boost vulkan plugin xlate nvencvt " ;;
      *) comp_info "$a" >/dev/null 2>&1 || die "unknown component '$a'. Components: $ALL_COMPONENTS $EXTRA_COMPONENTS (groups: all kexts userland)"
         [ "$a" != package ] || die "use ./build.sh package"
         out="$out$a " ;;
    esac
  done
  for c in $ALL_COMPONENTS $EXTRA_COMPONENTS; do case "$out" in *" $c "*) printf '%s ' "$c" ;; esac; done
}
cmd_build() {
  local list c
  prepare_build
  [ $# -gt 0 ] || set -- all
  list=$(expand_components "$@")
  say "== building: $list($NM_FLAVOUR flavour, NVAccel for macOS $(accel_majors | sed 's/ / and /'), $NM_JOBS jobs, working directory $W)"
  for c in $list; do need_tools_for "$c"; need_sources_for "$c"; done
  for c in $list; do build_component "$c"; done
}
cmd_package() {
  [ -n "${NM_BASE_PACKAGE:-}" ] || die "package needs a release package as its base: --base-package nullmoth-nvidia-<version>.tar.gz (or NM_BASE_PACKAGE). Why: install.sh needs files that have no source in this repository (nullmoth-runtime-check, nvmtl-allow.txt, the plugin bundle's Info.plist, air-runtime, ...); docs/BUILDING.md explains"
  [ -e "$NM_BASE_PACKAGE" ] || [ "$NM_DRY_RUN" = 1 ] || die "the base package $NM_BASE_PACKAGE does not exist"
  [ "$NM_ACCEL" = both ] || die "a package must carry NVAccel for macOS 15 and 26 (install.sh checks both); build with --accel both"
  prepare_build
  local c; for c in $ALL_COMPONENTS; do need_tools_for "$c"; need_sources_for "$c"; done
  build_component package
}

# ---------------------------------------------------------------------------------------------------------- test
cmd_test() {
  local py rc=0 last
  py=$(venv_python); [ -n "$py" ] || { find_tools; py=${T_PYTHON:-$(command -v python3 || true)}; }
  [ -n "$py" ] || die "no python3"
  say "== pins"; check_pins
  say "== host tests: $(qcmd "$py" tools/run_all_tests.py -j "$NM_JOBS")"
  if [ "$NM_DRY_RUN" = 1 ]; then return 0; fi
  CUR_LOG=$LOGS/test.log
  mkdir -p "$LOGS"
  ( cd "$HERE" && env PYTHONDONTWRITEBYTECODE=1 "$py" tools/run_all_tests.py -j "$NM_JOBS" ) 2>&1 | tee "$CUR_LOG" || rc=$?
  last=$(grep -E '^[0-9]+ passed, [0-9]+ failed' "$CUR_LOG" | tail -n 1 || true)
  case "$last" in *" 0 failed,"*) ;; *) rc=1 ;; esac
  # the runner leaves this one to build_nvrm.sh; with the fetched pristine tree it can run here
  local pc="not run (no pristine $NM_PIN_OGKM620_REF tree yet: ./build.sh fetch)"
  if [ -f "$PRISTINE/version.mk" ]; then
    say "== the kexts/NVRM patch chain against $PRISTINE"
    if ( cd "$HERE" && env "PATH=$TBIN:$PATH" bash tools/test_patch_chain.sh "$PRISTINE" ) > "$LOGS/test-patch-chain.log" 2>&1; then
      pc="every patch applies with --fuzz=0"
    else pc="FAILED (see $(printf '%s' "$LOGS/test-patch-chain.log" | tilde))"; rc=1; fi
  fi
  say ""; say "tests: ${last:-no summary line}   (log: $(printf '%s' "$CUR_LOG" | tilde))"
  say "patch chain: $pc"
  [ "$PIN_PROBLEMS" = 0 ] || rc=1
  if is_mac; then :; else note "on $(uname -s) many tests fail or skip for lack of macOS tools (codesign, plutil, xcrun, cc -arch); run them on the Mac"; fi
  return "$rc"
}

# ---------------------------------------------------------------------------------------------------------- env
print_env() {
  local v
  find_tools >/dev/null 2>&1 || true
  say "# ./build.sh env: the variables build.sh gives the build scripts. Paste into a Terminal, then run one script"
  say "# by hand, for example:  bash build/build_nvrm.sh kexts/NVRM \"\$OGKM\" \"\$NM_WORK/out/kexts/nvrm\""
  say "# (each script also gets CC, CXX, CFLAGS, CPPFLAGS, LDFLAGS, NM_TAHOE removed; build.sh shows its exact"
  say "#  command lines with: ./build.sh --dry-run --force build <component>)"
  say "# -- settings"
  for v in $OPTION_VARS; do eval "say \"export $v=\$(q \"\${$v:-}\")\""; done
  say "# -- where the build keeps and finds things"
  say "export NULLMOTH_BUILD_ROOT=$(q "$R")"
  say "export NM_OGKM620_PRISTINE=$(q "$PRISTINE")"
  say "export NM_OGKM620=$(q "$OGKM620")"
  say "export OGKM=$(q "$OGKM620")"
  say "export NVRM_PRISTINE_OGKM=$(q "$PRISTINE")"
  say "export NM_OGKM610=$(q "$OGKM610")"
  say "export NM_MESA_GIT=$(q "$MESA")"
  say "export NM_KHRONOS_URL=$(q "${NM_KHRONOS_URL:-file://$KHRONOS}")"
  say "export NULLMOTH_BUILD_HOST=$(q "$BUILD_HOST")"
  if [ "$BUILD_HOST" = "$LOCAL_HOST" ]; then
    say "export NM_LOCAL_BUILD_HOST=$(q "$LOCAL_HOST")   # build/local_build_host.sh serves it; PATH starts with tools/host"
    say "# NM_LOCAL_HOST_PATH: the PATH of a command run on that host (default /usr/bin:/bin:/usr/sbin:/sbin)"
  fi
  if [ "$NM_OFFLINE" = 1 ]; then say "export CARGO_NET_OFFLINE=true"; else say "# CARGO_NET_OFFLINE=true with --offline"; fi
  say "export NULLMOTH_RUST_VERSION=$(q "${NULLMOTH_RUST_VERSION:-$NM_PIN_RUST_VERSION}")"
  say "export NULLMOTH_RUST_BIN=$(q "$RUST_BIN")"
  say "export NULLMOTH_CARGO_HOME=$(q "${NULLMOTH_CARGO_HOME:-$CARGO_DIR}")"
  say "export RUSTUP_HOME=$(q "$RUSTUP_DIR")"
  say "export CARGO_HOME=$(q "$CARGO_DIR")"
  say "export RUSTUP_TOOLCHAIN=$(q "$NM_PIN_RUST_VERSION")"
  say "export NVK_PATH=$(q "${NVK_PATH:-$TBIN}")"
  say "export PKG_CONFIG_PATH=$(q "$(nvk_pkg_config_path)")"
  say "export LIBCLANG_PATH=$(q "${LIBCLANG_PATH:-$LIBCLANG_DIR}")"
  say "export NVK_JOBS=$(q "${NVK_JOBS:-$NM_JOBS}")"
  say "export JOBS=$(q "${JOBS:-$NM_JOBS}")"
  say "export NV_VERSION=$(q "$NM_PIN_OGKM620_REF")"
  say "export MACOSX_DEPLOYMENT_TARGET=$(q "${MACOSX_DEPLOYMENT_TARGET:-15.0}")"
  say "export PYTHONDONTWRITEBYTECODE=1"
  if [ "$BUILD_HOST" = "$LOCAL_HOST" ]; then say "export PATH=$(q "$THOST:$STEP_PATH")"; else say "export PATH=$(q "$STEP_PATH")"; fi
  say "# -- read by the build scripts; passed on only when you set them (empty = the script's own default)"
  for v in KMS_ARCHIVE RE MINVER FB_DEFS KHDR KLIB NVK_SYSROOT NVK_EXTRA_PATCHES NVK_SKIP_PATCHES NVK_REF_EXPORTS DEVELOPER_DIR \
           NM_VULKAN_INCLUDE NM_LILU_DEBUG_KEXT NM_VIRTUALSMC_SRC NM_MACKERNELSDK; do
    eval "if [ -n \"\${$v+x}\" ]; then say \"export $v=\$(q \"\${$v}\")\"; else say \"# $v (not set)\"; fi"
  done
  say "# -- package (build/package_local.sh)"
  say "export NM_PACKAGE_NAME=$(q "$(package_name)")"
  say "export NM_BUILD_INFO=$(q "$PKGDIR/build-info.txt")"
  say "# -- mirrors (empty = the URL in build/pins.sh)"
  for v in NM_OGKM_URL NM_MESA_URL NM_OPEN_GPU_DOC_URL NM_RYZENADJ_URL NM_WEBKIT_SPI_URL NM_RUSTUP_URL RUSTUP_DIST_SERVER; do
    eval "if [ -n \"\${$v:-}\" ]; then say \"export $v=\$(q \"\${$v}\")\"; else say \"# $v (not set)\"; fi"
  done
  say "# -- not used by build.sh: STEPS and NM_REV (build_release_inputs.sh, which build.sh replaces), X and RELEASE"
  say "#    (build_xlate.sh and build_plugin.sh, the original author's development scripts for trees in ~/nvmtl-build)"
}

# ---------------------------------------------------------------------------------------------------------- clean
cmd_clean() {
  local what=${1:-outputs}
  [ -d "$W" ] || { say "nothing to clean: $W does not exist"; return 0; }
  [ -f "$W/$MARKER" ] || die "$W has no $MARKER marker; build.sh deletes nothing there"
  case "$what" in
    outputs) safe_rm "$O" "$STAMPS" "$LOGS" "$PKGDIR" "$RMWORK" "$R/build/plugin-release" "$R/build/plugin-dev" "$R/build/xlate-release" "$R/build/nvencvt" ;;
    sources) safe_rm "$SRC" "$R/build/ogkm620" "$R/build/gpu-src" "$NVHDR" "$RMWORK" "$STAMPS" ;;
    tools)   safe_rm "$TOOLS" "$STAMPS" ;;
    all)     ask "Delete everything in $W (sources, tools, outputs)?" || { say "nothing deleted"; return 0; }
             local p; for p in "$W"/* "$W"/.[!.]*; do [ -e "$p" ] || continue; [ "$p" = "$W/$MARKER" ] || [ "$p" = "$W/build.lock" ] || safe_rm "$p"; done ;;
    *) die "clean takes outputs, sources, tools or all" ;;
  esac
  say "clean $what: done"
}

# ---------------------------------------------------------------------------------------------------------- summary, all
install_hint() {
  say "Nothing was installed. To install what was built (needs your password; read README.md, \"Install — by hand\", first):"
  say "  cd $(q "$PKGDIR/pkgroot" | tilde)"
  say "  sudo env CHECK=1 ./install.sh        # a dry run: checks the package and this Mac, changes nothing"
  say "  sudo ./install.sh && sudo shutdown -r now"
  say "Going back: docs/BUILDING.md, \"Installing the result and going back\"."
}
summary() {
  hr
  say "summary"
  printf '%s' "$STATUS_LINES" | tilde
  [ -z "$REV_NOTE" ] || say "plugin, translator and libnvencvt $([ "$NM_DRY_RUN" = 1 ] && echo 'would be' || echo were) built from $REV_NOTE"
  say "outputs:     $(printf '%s' "$O" | tilde)"
  say "logs:        $(printf '%s' "$LOGS" | tilde)"
}
cmd_all() {
  say "== doctor"
  doctor
  if [ "$D_MISSING" != 0 ]; then
    if [ "$NM_DRY_RUN" = 1 ]; then
      if is_mac && ! mac_too_old; then hr; say "== deps"; cmd_deps; else note "dry run: going on although something is missing"; fi
    elif [ "$NM_NO_INSTALL" = 1 ]; then die "something required is missing (above) and --no-install was given"
    else hr; say "== deps"; cmd_deps; fi
  fi
  hr; cmd_fetch
  hr; cmd_build all
  if [ -n "${NM_BASE_PACKAGE:-}" ]; then
    hr; cmd_package
    summary; say "package:     $(printf '%s' "$PKGDIR/pkgroot" | tilde)"; [ "$NM_DRY_RUN" = 1 ] || install_hint
  else
    summary
    say "package:     not staged: no base package. A package needs a release tarball as its base (docs/BUILDING.md):"
    say "             ./build.sh --base-package ~/Downloads/nullmoth-nvidia-<version>.tar.gz package"
  fi
}

# ---------------------------------------------------------------------------------------------------------- help
cmd_help() {
  sed -n '2,/^set -Eeu/p' "$SELF" | sed '$d' | sed 's/^# \{0,1\}//'
  say ""
  say "Components, in build order: $ALL_COMPONENTS"
  say "  optional: $EXTRA_COMPONENTS     groups: all kexts userland"
  local c; for c in $ALL_COMPONENTS $EXTRA_COMPONENTS; do comp_info "$c"; printf '  %-9s %s\n' "$c" "$C_DESC"; done
  say ""
  say "Settings (option, environment variable or configuration file line; precedence: option > environment > file > default):"
  say "  $(printf '%s' "$OPTION_VARS $LOCATION_VARS" | tr '\n' ' ')"
  say "Passed to the build scripts when set: $(printf '%s' "$SCRIPT_VARS" | tr '\n' ' ')"
  say "Configuration file: --config FILE, NM_CONFIG, or ~/.config/nullmoth/build.conf when it exists (example: build/build.conf.example)"
}

# ---------------------------------------------------------------------------------------------------------- main
main() {
  CMDLINE="$*"
  load_pins
  remember_environment
  # options and the command
  local args=() a config=${NM_CONFIG:-}
  local o_work="" o_jobs="" o_flavour="" o_accel="" o_base="" setlist=""
  while [ $# -gt 0 ]; do
    a=$1
    case "$a" in
      --workdir|--jobs|-j|--flavour|--flavor|--accel|--base-package|--config)
        [ $# -ge 2 ] || die "$a needs a value"
        case "$a" in
          --workdir) o_work=$2 ;; --jobs|-j) o_jobs=$2 ;; --flavour|--flavor) o_flavour=$2 ;; --accel) o_accel=$2 ;;
          --base-package) o_base=$2 ;; --config) config=$2 ;;
        esac; shift 2; continue ;;
      --workdir=*) o_work=${a#*=} ;; --jobs=*) o_jobs=${a#*=} ;; -j[0-9]*) o_jobs=${a#-j} ;;
      --flavour=*|--flavor=*) o_flavour=${a#*=} ;; --accel=*) o_accel=${a#*=} ;; --base-package=*) o_base=${a#*=} ;; --config=*) config=${a#*=} ;;
      -y|--yes) setlist="$setlist NM_ASSUME_YES" ;;
      --no-install) setlist="$setlist NM_NO_INSTALL" ;;
      --offline) setlist="$setlist NM_OFFLINE" ;;
      -n|--dry-run) setlist="$setlist NM_DRY_RUN" ;;
      -v|--verbose) setlist="$setlist NM_VERBOSE" ;;
      --force) setlist="$setlist NM_FORCE" ;;
      -h|--help) args+=( help ) ;;
      -*) die "unknown option $a (./build.sh help)" ;;
      *) args+=( "$a" ) ;;
    esac
    shift
  done
  if [ -z "$config" ] && [ -f "$HOME/.config/nullmoth/build.conf" ]; then config=$HOME/.config/nullmoth/build.conf; fi
  [ -z "$config" ] || load_config "$(expand_tilde "$config")"
  NM_CONFIG=$config
  [ -z "$o_work" ] || NM_WORK=$o_work
  [ -z "$o_jobs" ] || NM_JOBS=$o_jobs
  [ -z "$o_flavour" ] || NM_FLAVOUR=$o_flavour
  [ -z "$o_accel" ] || NM_ACCEL=$o_accel
  [ -z "$o_base" ] || NM_BASE_PACKAGE=$(expand_tilde "$o_base")
  for a in $setlist; do eval "$a=1"; done
  set_defaults
  # a setting from the configuration file reaches the scripts as an environment variable does
  for a in $ALL_VARS; do eval "[ -z \"\${$a+x}\" ] || export $a"; done
  derive_paths
  CMD=${args[0]:-all}
  case "$CMD" in all|doctor|deps|fetch|build|package|test|env|clean|help) ;; *) die "unknown command '$CMD' (./build.sh help)" ;; esac
  if [ ${#args[@]} -gt 1 ] && [ "$CMD" != build ] && [ "$CMD" != clean ]; then die "$CMD takes no arguments"; fi
  trap 'on_err "$LINENO" "$BASH_COMMAND"' ERR
  trap on_exit EXIT
  trap on_signal INT TERM
  TMPF=$(mktemp "${TMPDIR:-/tmp}/nullmoth-build.XXXXXX")
  case "$CMD" in
    help) cmd_help; return 0 ;;
    env) check_paths; print_env; return 0 ;;
    doctor) doctor; trap - ERR; if [ "$D_MISSING" = 0 ]; then exit 0; fi; exit 1 ;;
  esac
  require_paths_ok
  case "$CMD" in
    deps|build|package|all)
      if mac_too_old; then
        die "this Mac runs macOS $(sw_vers -productVersion 2>/dev/null): the build needs Xcode $NM_PIN_TOOL_XCODE_MIN, which runs on macOS $NM_PIN_TOOL_MACOS_MIN or later. Update macOS (the driver supports macOS 15 and 26), or build on another Mac"
      fi ;;
  esac
  [ "$NM_DRY_RUN" = 0 ] || say "(dry run: nothing is changed; the commands are printed)"
  ensure_workdir
  take_lock
  case "$CMD" in
    deps) cmd_deps ;;
    fetch) check_pins; [ "$PIN_PROBLEMS" = 0 ] || die "the pins and the scripts disagree (above)"; cmd_fetch ;;
    build) check_pins >/dev/null; [ "$PIN_PROBLEMS" = 0 ] || die "the pins and the scripts disagree (./build.sh doctor)"
           cmd_build ${args[@]+"${args[@]:1}"}; summary ;;
    package) check_pins >/dev/null; [ "$PIN_PROBLEMS" = 0 ] || die "the pins and the scripts disagree (./build.sh doctor)"
             cmd_package; summary; [ "$NM_DRY_RUN" = 1 ] || install_hint ;;
    test) cmd_test || { trap - ERR; exit 1; } ;;
    clean) cmd_clean ${args[@]+"${args[@]:1}"} ;;
    all) check_pins >/dev/null; [ "$PIN_PROBLEMS" = 0 ] || die "the pins and the scripts disagree (./build.sh doctor)"; cmd_all ;;
  esac
}
main "$@"
