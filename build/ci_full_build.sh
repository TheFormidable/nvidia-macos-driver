#!/bin/bash
# ci_full_build.sh: the checks of the full build from an empty working directory (.github/workflows/full-build.yml),
# which runs on an Intel Mac of GitHub's exactly what docs/BUILDING.md tells a tester to type. Every command also runs
# by hand on a Mac, but watch-start leaves a process of its own running until watch-stop or prove, and the read of
# Apple's make with an empty developer folder is made only in GitHub's job (GITHUB_ACTIONS=true):
#
#   ci_full_build.sh basepkg <dir>
#       the 2.9 release package that "./build.sh package" starts from, in <dir>: downloaded once from the URL pinned in
#       build/pins.sh (NM_PIN_CI_BASEPKG_*) and checked against the pinned size and SHA-256 before it is used, also when
#       it is already there. Prints its path and nothing else on stdout.
#   ci_full_build.sh brew-state <file> [prefix]
#       what Homebrew has before the build: the names and link targets in <prefix>/bin and <prefix>/opt and the formula
#       and version folders of <prefix>/Cellar (prefix: /usr/local, Homebrew's place on an Intel Mac). The file's time
#       is also the moment from which "prove" looks for files Homebrew keeps that changed.
#   ci_full_build.sh watch-start <dir> [prefix ...]   /   ci_full_build.sh watch-stop <dir>
#       a watch in the background that lists the running processes twice a second and keeps every one that is
#       Homebrew's brew: a process whose program, or whose script when the program is a shell, is <prefix>/bin/brew
#       or lies in <prefix>/Homebrew or <prefix>/Library/Homebrew (prefixes: /usr/local and /opt/homebrew, Homebrew's
#       two places, when none is given). Its record and pid file are in <dir>. watch-stop and prove stop only the
#       watch that was started for <dir>, written as watch-start was given it: a pid file that names any other
#       process (the watch ended and its number was given to another program) is removed and nothing is stopped.
#   ci_full_build.sh deps-plan <file>
#       reads the output of "./build.sh --dry-run deps" in <file>: exit 1 when deps would call Homebrew.
#   ci_full_build.sh toolchain-id <workdir>
#       one line that changes whenever a tool of the working directory's toolchain was finished, or nothing when there
#       is no toolchain (the full build saves the toolchain's cache under it, after a failed deps too).
#   ci_full_build.sh prove <workdir> <brew-state file> <watch dir> [prefix]
#       the proofs, one line each with what was checked and the number; stops the watch; exit 1 when a check failed or
#       could not be made (its input is missing because an earlier step failed).
#   ci_full_build.sh logs <workdir>
#       for a failed run: the end of the newest logs, the newest failure report, and every line of every log that
#       says "error:" or "STOP:".
#   ci_full_build.sh reads <workdir>
#       what only a Mac can show, printed as labelled blocks for a reader of the log (no check: it always exits 0,
#       and a read that cannot be made says "could not be read: <why>"): how the toolchain's tools were built, what
#       Apple's own make does with an empty developer folder (in GitHub's job only: on a Mac with a desktop that
#       may be Apple's offer to install the command line tools), what the package's tarball carries (owners,
#       AppleDouble entries, extended attributes, the account name, signatures), and how often the build machine's
#       home folder and working directory appear in the compiled files. It writes only a scratch folder under the
#       working directory, which it removes.
#
# Written for the /bin/bash 3.2 of macOS and its BSD tools. Mach-O files are read with otool (Xcode's; elsewhere any
# otool that prints the same, such as llvm-otool, put on PATH under that name). It writes only the files and folders
# it is given, never runs brew, installs nothing and never uses sudo.
set -u -o pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd -P)
SELF="$HERE/build/ci_full_build.sh"
PINS="$HERE/build/pins.sh"

say()  { printf '%s\n' "$*"; }
stop() { say "STOP: $*" >&2; exit 2; }
usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$SELF" >&2; exit 2; }

# Folders of build.sh's working directory (its derive_paths): tools/bin holds a link to every tool a step uses, the
# Python environment is tools/venv, Rust tools/rustup and tools/cargo, the SPIRV-LLVM-Translator
# tools/spirv-llvm-translator, the step logs logs/<component>.log, the failure reports reports/, the package package/.
# The tools deps builds from pinned sources (build/toolchain.sh) are installed in tools/toolchain, with one stamp per
# finished tool in tools/toolchain/.nm-toolchain/, and each tool's log is logs/toolchain/<tool>.log; their sources are
# src/git/tc-<tool>.git (fetched) and src/toolchain/<tool> (exported from it), their build trees tools/toolchain-build.
# When build.sh asks Homebrew anything it keeps Homebrew's cache in tools/homebrew-cache.
tc_prefix() { printf '%s/tools/toolchain' "$1"; }
tc_logs() { printf '%s/logs/toolchain' "$1"; }
# the links build.sh makes in tools/bin (make_tool_links) once deps has every tool: a step that finds one of these
# missing there would look for it on its PATH, which leads to macOS's own folders only, and stop
TOOL_LINKS="bindgen cargo cbindgen cmake glslangValidator llvm-config llvm-spirv ninja patch pkg-config rustc"

# ---------------------------------------------------------------------------------------------------------- small tools
sha256_file() { shasum -a 256 "$1" | cut -c1-64; }
sha256_stdin() { shasum -a 256 | cut -c1-64; }
size_of() { wc -c < "$1" | tr -d ' '; }
pin() { sed -n "s/^$1=\"\{0,1\}\([^\"]*\)\"\{0,1\}\$/\1/p" "$PINS" | sed -n 1p; }
# ver_le A B: dotted version A is at most B
ver_le() {
  local a=$1 b=$2 x y
  while [ -n "$a" ] || [ -n "$b" ]; do
    x=${a%%.*}; y=${b%%.*}; x=${x:-0}; y=${y:-0}
    case "$x$y" in *[!0-9]*) return 1 ;; esac
    if [ "$((10#$x))" -lt "$((10#$y))" ]; then return 0; fi
    if [ "$((10#$x))" -gt "$((10#$y))" ]; then return 1; fi
    case "$a" in *.*) a=${a#*.} ;; *) a= ;; esac
    case "$b" in *.*) b=${b#*.} ;; *) b= ;; esac
  done
  return 0
}
# resolve <path>: the file a path leads to through its symbolic links (macOS's readlink has no -f)
resolve() {
  local p=$1 t n=0 d
  while [ -L "$p" ] && [ "$n" -lt 40 ]; do
    t=$(readlink "$p") || break
    case "$t" in /*) p=$t ;; *) p=$(dirname "$p")/$t ;; esac
    n=$((n + 1))
  done
  d=$(cd "$(dirname "$p")" 2>/dev/null && pwd -P) || { printf '%s' "$p"; return 0; }
  printf '%s/%s' "$d" "$(basename "$p")"
}

# ---------------------------------------------------------------------------------------------------------- basepkg
file_is() { [ -f "$1" ] && [ "$(size_of "$1")" = "$2" ] && [ "$(sha256_file "$1")" = "$3" ]; }
cmd_basepkg() {
  local dir=${1:-} url sha size f
  [ -n "$dir" ] || usage
  url=$(pin NM_PIN_CI_BASEPKG_URL); sha=$(pin NM_PIN_CI_BASEPKG_SHA256); size=$(pin NM_PIN_CI_BASEPKG_SIZE)
  case "$url" in https://*/*.tar.gz) ;; *) stop "build/pins.sh names no NM_PIN_CI_BASEPKG_URL (https://.../<name>.tar.gz)" ;; esac
  printf '%s' "$sha" | grep -qx '[0-9a-f]\{64\}' || stop "build/pins.sh names no NM_PIN_CI_BASEPKG_SHA256 (64 hex digits)"
  case "$size" in ''|*[!0-9]*) stop "build/pins.sh names no NM_PIN_CI_BASEPKG_SIZE (bytes)" ;; esac
  mkdir -p "$dir" || stop "cannot make $dir"
  f=$(cd "$dir" && pwd -P)/${url##*/}
  if file_is "$f" "$size" "$sha"; then
    say "base package: $f is here already, $size bytes, SHA-256 $sha as pinned" >&2
  else
    rm -f "$f" "$f.part"
    say "base package: downloading $url" >&2
    curl -fsSL --proto '=https' --tlsv1.2 --retry 3 -o "$f.part" "$url" || { rm -f "$f.part"; stop "could not download $url"; }
    if ! file_is "$f.part" "$size" "$sha"; then
      say "STOP: $url gave $(size_of "$f.part") bytes with SHA-256 $(sha256_file "$f.part"); the pin is $size bytes, $sha (build/pins.sh)" >&2
      rm -f "$f.part"; exit 1
    fi
    mv -f "$f.part" "$f" || stop "cannot move the download to $f"
    say "base package: $f, $size bytes, SHA-256 $sha as pinned" >&2
  fi
  printf '%s\n' "$f"
}

# ---------------------------------------------------------------------------------------------------------- Homebrew
brew_listing() {   # <prefix>: one line per entry of bin/ and opt/ (with its link target) and per Cellar/<formula>/<version>
  local p=$1 d e
  for d in bin opt; do
    if [ ! -d "$p/$d" ]; then say "$d: (no such folder)"; continue; fi
    find "$p/$d" -mindepth 1 -maxdepth 1 2>/dev/null | LC_ALL=C sort | while IFS= read -r e; do
      if [ -L "$e" ]; then say "$d/${e##*/} -> $(readlink "$e")"; else say "$d/${e##*/}"; fi
    done
  done
  if [ -d "$p/Cellar" ]; then
    find "$p/Cellar" -mindepth 2 -maxdepth 2 2>/dev/null | LC_ALL=C sort | while IFS= read -r e; do say "Cellar/${e#"$p/Cellar/"}"; done
  else say "Cellar: (no such folder)"; fi
}
cmd_brew_state() {
  local out=${1:-} p=${2:-/usr/local}
  [ -n "$out" ] || usage
  mkdir -p "$(dirname "$out")" || stop "cannot make the folder of $out"
  brew_listing "$p" > "$out" || stop "cannot write $out"
  touch "$out"
  say "Homebrew in $p before the build: $(wc -l < "$out" | tr -d ' ') entries in bin, opt and Cellar, listing sha256 $(sha256_file "$out" | cut -c1-16)"
}
# Homebrew's own folders, where any brew command that ran would leave a changed file: its repository and formula folders
# under the prefix, its state under var/homebrew, and its cache and logs in the home folder.
brew_own_folders() {
  local p=$1 d
  for d in "$p/Homebrew" "$p/Cellar" "$p/Caskroom" "$p/var/homebrew" "${HOME:-/nonexistent}/Library/Caches/Homebrew" "${HOME:-/nonexistent}/Library/Logs/Homebrew"; do
    [ -d "$d" ] && say "$d"
  done
  return 0
}

# The watch. A brew command that runs Homebrew's Ruby lives for well over a second, so a list of the processes taken
# every half second sees it; a process of the watch itself (ps, awk) is told apart by its parent.
# Which process is Homebrew's brew: the list shows a command line, and brew is a shell script that goes on as
# Homebrew's own brew.sh and then as its own Ruby. So the first word decides, or, while the words are programs that
# run another (a shell, env, ruby and the like), the first word after them that is neither an option nor an
# assignment: it must be <prefix>/bin/brew (not <prefix>/bin/brew-gem or another program whose name begins so) or
# start with <prefix>/Homebrew/ or <prefix>/Library/Homebrew/. A path that only
# holds such a name further in is not Homebrew's: the stand-in brews of this tree's tests live in folders named
# <a test's folder>/usr/local/bin and <a test's folder>/opt/homebrew/bin, and the full build runs those tests under
# this watch. Nor is a program that merely names brew's path as an argument, nor another program of that Homebrew
# (the Python of /opt/homebrew runs the tests themselves on many Macs). Those that are named brew are counted apart.
# It must outlive the step that starts it and be read by a later step. GitHub's runner stops what a job left running
# when the job ends (it finds those processes by a variable of the job's environment); whether it also stops a step's
# background processes when that step ends is not shown here. So the loop is started in a session of its own (perl's
# setsid: macOS has no setsid command) where perl is there, and prove checks rather than assumes: a watch that is not
# running at the end, that made no list, or that stood still for more than WATCH_GAP seconds at one time (between two
# lists, or between its last list and prove) is "not checked" (which fails the job), never "0 seen".
# A shorter pause is the machine's and not the watch's: a runner with four slow processors, busy with a whole build,
# makes its lists 0.7 s apart on average and now and then some seconds apart (eleven, once in forty minutes). prove
# prints the longest pause and the time it ended, so that the reader sees how fine the net was and which step ran then.
# Being outside every process group, it is not ended with whatever started it: it ends when watch-stop or prove
# removes its pid file, when the folder of that file is gone, and by itself after NMCI_WATCH_MAX seconds (eight hours
# when unset, more than any job runs), so that one whose starter was killed does not list processes for ever.
WATCH_GAP=60
cmd_watch_start() {
  local dir=${1:-} p pid i
  [ -n "$dir" ] || usage
  shift
  [ $# -gt 0 ] || set -- /usr/local /opt/homebrew
  for p in "$@"; do
    case "$p" in /*) ;; *) stop "watch-start: a Homebrew prefix is an absolute path, not '$p'" ;; esac
    case "$p" in *[[:space:]]*) stop "watch-start: a prefix with a blank cannot be told apart in a list of processes: '$p'" ;; esac
  done
  mkdir -p "$dir" || stop "cannot make $dir"
  : > "$dir/brew-watch.txt"; printf '0 0 0 0 -\n' > "$dir/brew-watch.count"
  printf 'starting\n' > "$dir/brew-watch.pid"   # the loop runs while this file exists
  if [ -x /usr/bin/perl ]; then
    nohup /usr/bin/perl -MPOSIX -e 'POSIX::setsid(); exec @ARGV or exit 127' /bin/bash "$SELF" watch-loop "$dir" "$@" > /dev/null 2>&1 < /dev/null &
  else
    nohup /bin/bash "$SELF" watch-loop "$dir" "$@" > /dev/null 2>&1 < /dev/null &
  fi
  pid=$!
  printf '%s\n' "$pid" > "$dir/brew-watch.pid"
  # the first list is made before this returns, so that what the next step starts is already watched
  i=0
  while [ "$(sed -n 's/ .*//p' "$dir/brew-watch.count" 2>/dev/null)" = 0 ] && [ "$i" -lt 100 ]; do sleep 0.1; i=$((i + 1)); done
  if [ "$(sed -n 's/ .*//p' "$dir/brew-watch.count" 2>/dev/null)" = 0 ]; then
    # the build goes on; prove says that the watch was not checked
    say "the watch for brew processes (pid $pid) made no list within 10 s"; return 0
  fi
  say "watching for brew processes (pid $pid), twice a second, until watch-stop"
}
cmd_watch_loop() {
  local dir=$1 n=0 t0 t prev gap=0 gapend=-
  shift
  local max=${NMCI_WATCH_MAX:-28800}
  case "$max" in ''|*[!0-9]*) max=28800 ;; esac
  t0=$(date +%s); prev=$t0
  while [ -f "$dir/brew-watch.pid" ] && [ $((prev - t0)) -lt "$max" ]; do
    # the prefixes reach awk through its environment: a value given with -v would have its backslashes read as escapes
    ps -axo pid=,ppid=,command= 2>/dev/null | NMCI_WATCH_PREFIXES="$*" awk -v me="$$" '
      function is_brew(word,   k) { for (k = 1; k <= np; k++) if (index(word, bp[k]) == 1 && (word == bp[k] || bp[k] ~ /\/$/)) return 1; return 0 }
      BEGIN { m = split(ENVIRON["NMCI_WATCH_PREFIXES"], pre, " ")
              for (k = 1; k <= m; k++) { bp[++np] = pre[k] "/bin/brew"; bp[++np] = pre[k] "/Homebrew/"; bp[++np] = pre[k] "/Library/Homebrew/" } }
      $1 == me || $2 == me { next }
      { c = $0; sub(/^ *[0-9]+ +[0-9]+ +/, "", c)
        nw = split(c, w, " "); hit = 0
        for (i = 1; i <= nw; i++) {
          if (i > 1 && (w[i] ~ /^-/ || w[i] ~ /^[A-Za-z_][A-Za-z0-9_]*=/)) continue
          if (is_brew(w[i])) { hit = 1; break }
          b = w[i]; sub(/^.*\//, "", b)
          if (b !~ /^(bash|sh|zsh|dash|ksh|env|ruby|nohup|nice|time|timeout|caffeinate)$/) break
        }
        if (hit) print "homebrew " c
        else if (c ~ /(^|\/)brew( |$)/) print "other " c }' >> "$dir/brew-watch.txt"
    n=$((n + 1)); t=$(date +%s)
    if [ $((t - prev)) -gt "$gap" ]; then gap=$((t - prev)); gapend=$(date -u +%H:%M:%SZ); fi
    prev=$t
    # lists made, seconds since the start, the longest time between two lists, the time of the last list, and the
    # time of day (UTC) at which that longest pause ended
    printf '%s %s %s %s %s\n' "$n" "$((t - t0))" "$gap" "$t" "$gapend" > "$dir/brew-watch.count.new" && mv -f "$dir/brew-watch.count.new" "$dir/brew-watch.count"
    sleep 0.5
  done
}
WATCH_SUMMARY=""; WATCH_HITS=-1; WATCH_OTHER=0
watch_stop() {   # <dir>: stops the watch; sets WATCH_HITS (-1: it did not watch to the end) and WATCH_SUMMARY
  local dir=$1 pid alive=0 n s g last ge now
  pid=$(sed -n 1p "$dir/brew-watch.pid" 2>/dev/null || true)
  # a process that ended and was not yet reaped (state Z) still answers kill -0. Only this folder's watch is taken
  # for alive and stopped: after a watch that ended without removing its file (killed with its step, a restart of the
  # Mac), the number in the file may be another program's by now
  case "$pid" in ''|*[!0-9]*) ;; *) case "$(ps -o stat= -p "$pid" 2>/dev/null)" in ''|Z*|*Z*) ;; *)
    case "$(ps -o command= -p "$pid" 2>/dev/null)" in *"watch-loop $dir "*) kill -0 "$pid" 2>/dev/null && alive=1 ;; esac ;; esac ;; esac
  rm -f "$dir/brew-watch.pid"
  if [ "$alive" = 1 ]; then kill "$pid" 2>/dev/null || true; fi
  read -r n s g last ge < "$dir/brew-watch.count" 2>/dev/null || true
  n=${n:-0}; s=${s:-0}; g=${g:-0}; last=${last:-0}; ge=${ge:--}; now=$(date +%s)
  WATCH_OTHER=$(grep '^other ' "$dir/brew-watch.txt" 2>/dev/null | sort -u | wc -l | tr -d ' ')
  WATCH_HITS=$(grep '^homebrew ' "$dir/brew-watch.txt" 2>/dev/null | sort -u | wc -l | tr -d ' ')
  if [ "$alive" = 0 ]; then
    WATCH_HITS=-1; WATCH_SUMMARY="the watch was not running at the end ($n lists over $s s)"
  elif [ "$n" = 0 ]; then
    WATCH_HITS=-1; WATCH_SUMMARY="the watch made no list"
  elif [ "$g" -gt "$WATCH_GAP" ]; then
    WATCH_HITS=-1; WATCH_SUMMARY="the watch made no list for $g s at one time, until $ge ($n lists over $s s)"
  elif [ $((now - last)) -gt "$WATCH_GAP" ]; then
    WATCH_HITS=-1; WATCH_SUMMARY="the watch's last list is $((now - last)) s old ($n lists over $s s)"
  else
    # the time since the last list is a pause like the others
    if [ $((now - last)) -gt "$g" ]; then g=$((now - last)); ge=$(date -u +%H:%M:%SZ); fi
    WATCH_SUMMARY="$n lists of the processes over $s s, at most $g s apart"
    # two lists a second or two apart are the loop's own pace on a busy machine; a longer pause is named with its time
    if [ "$g" -gt 2 ] && [ "$ge" != - ]; then WATCH_SUMMARY="$WATCH_SUMMARY (the longest pause ended $ge)"; fi
  fi
}
cmd_watch_stop() {
  local dir=${1:-}
  [ -n "$dir" ] || usage
  watch_stop "$dir"
  say "brew processes seen: $WATCH_HITS ($WATCH_SUMMARY); other processes named brew: $WATCH_OTHER"
  grep '^homebrew ' "$dir/brew-watch.txt" 2>/dev/null | sort -u | sed -n 1,20p
  [ "$WATCH_HITS" = 0 ]
}

# deps-plan: the dry run of deps lists every command deps would run. With tools from source (the default on an Intel
# Mac) it starts with build.sh's sentence "missing build tools are built by ./build.sh deps from pinned sources into
# <folder>, without Homebrew"; the Homebrew way prints no such sentence, and before an install it prints the "Homebrew
# formulas to install" line and each brew command. The Homebrew way is refused even when it would install nothing (a
# Mac whose Homebrew has every formula): this job is about the default, which takes nothing from Homebrew.
cmd_deps_plan() {
  local f=${1:-} hits src
  [ -n "$f" ] && [ -f "$f" ] || usage
  hits=$(grep -nE 'Homebrew formulas to install|(^|[ /])brew (install|info|unlink|upgrade|link|tap|update)( |$)|run that installer|Type yes to run Homebrew' "$f" || true)
  src=$(grep -cE '^missing build tools are built by \./build\.sh deps from pinned sources into .*, without Homebrew' "$f" || true)
  if [ -n "$hits" ]; then
    say "deps would call Homebrew on this Mac ($(printf '%s\n' "$hits" | wc -l | tr -d ' ') line(s) of its dry run):"
    printf '%s\n' "$hits" | sed -n 1,20p | sed 's/^/    /'
    return 1
  fi
  if [ "$src" = 0 ]; then
    say "deps's dry run does not say that the tools are built from pinned sources without Homebrew (the Homebrew way: NM_TOOLS_FROM, or not an Intel Mac); its first lines:"
    sed -n 1,5p "$f" | sed 's/^/    /'
    return 1
  fi
  say "deps's dry run builds the tools from pinned sources and names no Homebrew command ($(wc -l < "$f" | tr -d ' ') lines read)"
}

cmd_toolchain_id() {
  local w=${1:-} st
  [ -n "$w" ] || usage
  st=$(tc_prefix "$w")/.nm-toolchain
  [ -d "$st" ] || return 0
  ( cd "$st" && find . -type f | LC_ALL=C sort | while IFS= read -r f; do printf '%s\n' "$f"; cat "$f"; done ) | sha256_stdin | cut -c1-16
}

# ---------------------------------------------------------------------------------------------------------- Mach-O
# macho_kind <file>: "thin <filetype byte>" or "fat" for a Mach-O file, nothing for anything else. A fat header shares
# its magic number with Java's class files; a fat file names a handful of architectures where a class file has its
# version (45 or more).
macho_kind() {
  local h n
  h=$(head -c 16 "$1" 2>/dev/null | od -An -tx1 | tr -d ' \n')
  case "${h:0:8}" in
    cffaedfe|cefaedfe) printf 'thin %s\n' "${h:24:2}" ;;
    feedfacf|feedface) printf 'thin %s\n' "${h:30:2}" ;;
    cafebabe) n=$((16#${h:8:8})); [ "$n" -gt 0 ] && [ "$n" -lt 20 ] && printf 'fat\n' ;;
  esac
  return 0
}
# machos <dir>...: the Mach-O programs and libraries (not object files: they load nothing) under the folders, one path
# per line. Only files with an execute bit or a library's name are opened: a linker gives its output the execute bit.
machos() {
  local d f k
  for d in "$@"; do
    [ -d "$d" ] || continue
    find "$d" -type f \( -perm -u+x -o -name '*.dylib' -o -name '*.so' \) -print 2>/dev/null | LC_ALL=C sort | while IFS= read -r f; do
      k=$(macho_kind "$f")
      case "$k" in ''|"thin 01"|"thin 0a") ;; *) printf '%s\n' "$f" ;; esac
    done
  done
}
loads_of() { otool -L "$1" 2>/dev/null | awk 'NR > 1 && !/:$/ { print $1 }'; }
own_names() { otool -D "$1" 2>/dev/null | awk 'NR > 1 && !/:$/ { print $1 }'; }
rpaths_of() { otool -l "$1" 2>/dev/null | awk '$1 == "cmd" { r = ($2 == "LC_RPATH") } r && $1 == "path" { print $2; r = 0 }'; }
minos_of() {
  otool -l "$1" 2>/dev/null | awk '$1 == "cmd" { c = $2 }
    c == "LC_BUILD_VERSION" && $1 == "minos" { print $2; exit }
    c == "LC_VERSION_MIN_MACOSX" && $1 == "version" { print $2; exit }'
}
# allowed <path> <working directory> <real working directory> <package folders too: 0|1>
allowed() {
  case "$1" in
    /usr/lib/*|/System/*|@rpath/*|@loader_path|@loader_path/*|@executable_path|@executable_path/*) return 0 ;;
    "$2"/*|"$3"/*) return 0 ;;
  esac
  if [ "$4" = 1 ]; then case "$1" in /Library/GPUBundles/*|/Library/NullMoth/*) return 0 ;; esac; fi
  return 1
}
# Rust's distribution ships a search path of the machine Rust itself was built on in programs of its own (rust-lld has
# $RUST_DIST_BUILD/<host>/llvm/lib beside @loader_path/../lib). No step of this build put it there.
RUST_DIST_BUILD=/Users/runner/work/rust/rust/build
# rust_dist_rpaths <workdir> <real workdir> <scratch>: moves those search paths out of bad-tools.txt into rust-dist.txt,
# for the files of the working directory's Rust toolchains only and only while no folder of that name is on this Mac: it
# then searches nothing. The same path in any other tool, or one that is there, stays a finding.
rust_dist_rpaths() {
  local w=$1 wr=$2 t=$3 l d
  : > "$t/rust-dist.txt"; : > "$t/bad-tools.kept"
  while IFS= read -r l; do
    d=${l##*: rpath }
    case "$l" in
      "$w/tools/rustup/toolchains/"*": rpath $RUST_DIST_BUILD/"*|"$wr/tools/rustup/toolchains/"*": rpath $RUST_DIST_BUILD/"*)
        if [ ! -e "$d" ]; then printf '%s\n' "$l" >> "$t/rust-dist.txt"; continue; fi ;;
    esac
    printf '%s\n' "$l" >> "$t/bad-tools.kept"
  done < "$t/bad-tools.txt"
  mv "$t/bad-tools.kept" "$t/bad-tools.txt"
}
# check_loads <list file> <workdir> <real workdir> <package folders 0|1> <bad lines file>: every library each file
# loads and every folder it searches (LC_RPATH); appends "<file>: loads|rpath <path>" for each that is not allowed
check_loads() {
  local list=$1 w=$2 wr=$3 pk=$4 bad=$5 f d own
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    if ! otool -L "$f" > /dev/null 2>&1; then printf '%s: otool cannot read it\n' "$f" >> "$bad"; continue; fi
    own=$(own_names "$f")
    while IFS= read -r d; do
      [ -n "$d" ] || continue
      case "
$own
" in *"
$d
"*) continue ;; esac
      allowed "$d" "$w" "$wr" "$pk" || printf '%s: loads %s\n' "$f" "$d" >> "$bad"
    done < <(loads_of "$f")
    while IFS= read -r d; do
      [ -n "$d" ] || continue
      allowed "$d" "$w" "$wr" "$pk" || printf '%s: rpath %s\n' "$f" "$d" >> "$bad"
    done < <(rpaths_of "$f")
  done < "$list"
}

# ---------------------------------------------------------------------------------------------------------- prove
NOK=0; NFAIL=0; NSKIP=0
ok()      { say "ok    $*"; NOK=$((NOK + 1)); }
fail()    { say "FAIL  $*"; NFAIL=$((NFAIL + 1)); [ "${GITHUB_ACTIONS:-}" != true ] || say "::error::$*"; }
skipped() { say "NOT CHECKED  $*"; NSKIP=$((NSKIP + 1)); [ "${GITHUB_ACTIONS:-}" != true ] || say "::error::not checked: $*"; }
indent()  { sed -n "1,${1}p" | sed 's/^/        /'; }

cmd_prove() {
  local w=${1:-} before=${2:-} wd=${3:-} p=${4:-/usr/local} wr t
  [ -n "$w" ] && [ -n "$before" ] && [ -n "$wd" ] || usage
  mkdir -p "$wd" || stop "cannot make $wd"
  wr=$(cd "$w" 2>/dev/null && pwd -P) || wr=$w
  t=$wd/prove; rm -rf "$t"; mkdir -p "$t" || stop "cannot make $t"
  say "== what the build from an empty working directory did and did not do (working directory $w)"
  prove_homebrew "$w" "$before" "$wd" "$p" "$t"
  prove_tools_used "$w" "$wr"
  prove_loads "$w" "$wr" "$t"
  prove_minos "$w" "$t"
  prove_logs "$w" "$p"
  prove_checkout
  prove_package "$w" "$t"
  say "== $((NOK + NFAIL + NSKIP)) checks: $NOK ok, $NFAIL failed, $NSKIP not checked"
  [ "$NFAIL" = 0 ] && [ "$NSKIP" = 0 ]
}

prove_homebrew() {
  local w=$1 before=$2 wd=$3 p=$4 t=$5 n changed folders
  if [ ! -f "$before" ]; then
    skipped "Homebrew's $p/bin, opt and Cellar: no listing from before the build ($before)"
  else
    brew_listing "$p" > "$t/brew-after.txt"
    n=$(wc -l < "$before" | tr -d ' ')
    if cmp -s "$before" "$t/brew-after.txt"; then
      ok "Homebrew's $p/bin, opt and Cellar, names and link targets: $n entries, sha256 $(sha256_file "$before" | cut -c1-16) before and after the build: unchanged"
    else
      fail "Homebrew's $p/bin, opt and Cellar changed during the build: $n entries before, $(wc -l < "$t/brew-after.txt" | tr -d ' ') after; the first differences:"
      diff "$before" "$t/brew-after.txt" | grep '^[<>]' | indent 30
    fi
    folders=$(brew_own_folders "$p")
    if [ -z "$folders" ]; then
      ok "files Homebrew keeps that changed during the build: 0 (none of its folders exists: $p/Homebrew, Cellar, Caskroom, var/homebrew, ~/Library/Caches/Homebrew, ~/Library/Logs/Homebrew)"
    else
      changed=$(printf '%s\n' "$folders" | while IFS= read -r d; do find "$d" -newer "$before" -print 2>/dev/null; done)
      n=$(printf '%s' "$changed" | grep -c . || true)
      if [ "$n" = 0 ]; then
        ok "files Homebrew keeps that changed during the build: 0, in $(printf '%s\n' "$folders" | wc -l | tr -d ' ') folders ($(printf '%s\n' "$folders" | sed "s|^${HOME:-/nonexistent}/|~/|" | tr '\n' ' ' | sed 's/ $//'))"
      else
        fail "files Homebrew keeps that changed during the build: $n; the first of them:"
        printf '%s\n' "$changed" | indent 20
      fi
    fi
  fi
  if [ -e "$w/tools/homebrew-cache" ]; then
    fail "build.sh asked Homebrew something: $w/tools/homebrew-cache, where it keeps Homebrew's cache when it calls brew, exists"
  else
    ok "build.sh's folder for Homebrew's cache, which it makes whenever it calls brew ($w/tools/homebrew-cache): absent"
  fi
  watch_stop "$wd"
  if [ "$WATCH_HITS" = -1 ]; then
    skipped "brew processes: $WATCH_SUMMARY"
  elif [ "$WATCH_HITS" = 0 ]; then
    ok "brew processes seen: 0 ($WATCH_SUMMARY; $WATCH_OTHER other process(es) named brew, which are not Homebrew's: the tests' stand-ins)"
  else
    fail "brew processes seen: $WATCH_HITS ($WATCH_SUMMARY):"
    grep '^homebrew ' "$wd/brew-watch.txt" | sort -u | indent 20
  fi
  say "      (not shown, and not showable without root: a brew command that changes no file and is over between two lists"
  say "      of the watch leaves nothing behind: brew --prefix, which ends within half a second, or any within a pause)"
}

# The tools the steps run: tools/bin (build.sh links each chosen tool there and puts it first on every step's PATH; its
# patch is a two-line script that runs the chosen patch) and the Python environment's interpreter.
prove_tools_used() {
  local w=$1 wr=$2 e name tg n=0 bad="" sys=""
  if [ ! -d "$w/tools/bin" ]; then skipped "the tools the steps use: $w/tools/bin does not exist"; return 0; fi
  while IFS= read -r e; do
    name=${e##*/}
    if [ -L "$e" ]; then tg=$(resolve "$e")
    elif [ "$name" = patch ]; then tg=$(sed -n 's/^exec \(.*\) -f "\$@"$/\1/p' "$e" | sed "s/^'\(.*\)'\$/\1/")
    else tg=$e; fi
    n=$((n + 1))
    case "$tg" in
      "$w"/*|"$wr"/*) ;;
      /usr/bin/*|/bin/*) sys="$sys $name=$tg" ;;
      *) bad="$bad
$name -> ${tg:-?}" ;;
    esac
  done < <(find "$w/tools/bin" -mindepth 1 -maxdepth 1 2>/dev/null | LC_ALL=C sort)
  if [ -e "$w/tools/venv/bin/python3" ] || [ -L "$w/tools/venv/bin/python3" ]; then
    n=$((n + 1)); tg=$(resolve "$w/tools/venv/bin/python3")
    case "$tg" in "$w"/*|"$wr"/*) ;; *) bad="$bad
python3 of the Python environment -> $tg" ;; esac
  else bad="$bad
python3 of the Python environment: not in tools/venv/bin"
  fi
  for name in $TOOL_LINKS; do
    [ -e "$w/tools/bin/$name" ] || [ -L "$w/tools/bin/$name" ] || bad="$bad
$name: not in tools/bin"
  done
  if [ -z "$bad" ]; then
    ok "the tools the steps use (tools/bin and the Python environment): $n, every one inside the working directory${sys:+; from macOS itself:$sys}"
  else
    fail "the tools the steps use: $(printf '%s' "$bad" | grep -c .) of $n lie outside the working directory and macOS, or are not there:"
    printf '%s\n' "$bad" | grep . | indent 20
  fi
}

prove_loads() {
  local w=$1 wr=$2 t=$3 n b pr named missing f g rd
  : > "$t/bad-out.txt"; : > "$t/bad-tools.txt"
  if [ ! -d "$w/out" ]; then skipped "the Mach-O files under $w/out: no such folder"
  else
    machos "$w/out" > "$t/out.txt"
    n=$(grep -c . "$t/out.txt" || true)
    check_loads "$t/out.txt" "$w" "$wr" 1 "$t/bad-out.txt"
    b=$(grep -c . "$t/bad-out.txt" || true)
    if [ "$n" = 0 ]; then fail "Mach-O programs and libraries under $w/out: none found"
    elif [ "$b" = 0 ]; then ok "Mach-O programs and libraries under $w/out: $n, every library they load and every folder they search lies in /usr/lib, /System, the working directory or the package's own /Library folders"
    else fail "Mach-O programs and libraries under $w/out: $n, $b load(s) or search path(s) outside /usr/lib, /System, the working directory and the package's folders:"; indent 30 < "$t/bad-out.txt"; fi
  fi
  pr=$(tc_prefix "$w")
  machos "$pr" "$w/tools/spirv-llvm-translator" "$w/tools/cargo/bin" "$w/tools/rustup/toolchains" > "$t/tools.txt"
  # the tools NVK's build runs, by name: each must be there, in the working directory
  named=""; missing=""
  for f in "$pr/bin/ninja" "$pr/bin/cmake" "$pr/python/bin/python3.14" "$pr/bin/pkgconf" "$pr/bin/spirv-val" "$pr/bin/spirv-opt" \
           "$pr/bin/glslang" "$pr/llvm/bin/llvm-config" "$pr/llvm/lib/libLLVM.dylib" "$pr/llvm/lib/libclang-cpp.dylib"; do
    if [ -e "$f" ]; then
      g=$(resolve "$f"); named="$named ${f#"$pr"/}"
      # a link of the toolchain that leads out of the working directory is a tool taken from elsewhere
      case "$g" in "$w"/*|"$wr"/*) ;; *) printf '%s: leads to %s, outside the working directory\n' "$f" "$g" >> "$t/bad-tools.txt" ;; esac
      grep -qxF "$g" "$t/tools.txt" || printf '%s\n' "$g" >> "$t/tools.txt"
    else missing="$missing ${f#"$w"/}"; fi
  done
  n=$(find "$pr/python/lib" -path '*/lib-dynload/*.so' -type f 2>/dev/null | wc -l | tr -d ' ')
  [ "$n" -gt 0 ] && named="$named python3.14's $n extension modules" || missing="$missing python3.14's extension modules (lib-dynload/*.so)"
  g=$(find "$w/out" -type f -name mesa_clc 2>/dev/null | sed -n 1p)
  if [ -n "$g" ]; then named="$named mesa_clc"; grep -qxF "$g" "$t/tools.txt" || printf '%s\n' "$g" >> "$t/tools.txt"; else missing="$missing mesa_clc (under out/)"; fi
  n=$(grep -c . "$t/tools.txt" || true)
  check_loads "$t/tools.txt" "$w" "$wr" 0 "$t/bad-tools.txt"
  rust_dist_rpaths "$w" "$wr" "$t"
  b=$(grep -c . "$t/bad-tools.txt" || true)
  rd=$(grep -c . "$t/rust-dist.txt" || true)
  if [ "$rd" = 0 ]; then rd=""
  else rd="; not counted: $rd search path(s) under $RUST_DIST_BUILD, the machine Rust was built on, which Rust's distribution ships in its own$(sed -e 's/: rpath .*//' -e 's|.*/| |' "$t/rust-dist.txt" | LC_ALL=C sort -u | tr -d '\n') and which name no folder of this Mac"; fi
  if [ -n "$missing" ]; then fail "tools of the working directory that are not there:$missing"; fi
  if [ "$n" = 0 ]; then fail "Mach-O files of the working directory's tools (toolchain, Translator, Rust): none found"
  elif [ "$b" = 0 ]; then ok "Mach-O files of the working directory's tools (toolchain, Translator, Rust, mesa_clc): $n, every library they load and every folder they search lies in /usr/lib, /System or the working directory; among them$named$rd"
  else fail "Mach-O files of the working directory's tools: $n, $b load(s) or search path(s) outside /usr/lib, /System and the working directory:"; indent 30 < "$t/bad-tools.txt"; fi
}

# What the package ships and this build made (a file whose SHA256SUMS line differs from the base package's, or that the
# base package does not have) must run on macOS 15.0: its LC_BUILD_VERSION minos at most 15.0. The files carried over
# unchanged from the base package are counted apart.
sums_hash() { awk -v r="$2" '{ q = substr($0, 67); if (q == r || q == "./" r) { print substr($0, 1, 64); exit } }' "$1" 2>/dev/null; }
base_dir() { local b; b=$(find "$1/package/base" -maxdepth 3 -type f -name install.sh 2>/dev/null | sed -n 1p); [ -n "$b" ] && dirname "$b"; return 0; }
prove_minos() {
  local w=$1 t=$2 r=$1/package/pkgroot b f rel hn hb m built=0 kept=0 bad="" dist="" kdist=""
  if [ ! -f "$r/SHA256SUMS" ]; then skipped "the macOS version of what the package ships: no staged package ($r)"; return 0; fi
  b=$(base_dir "$w")
  machos "$r" > "$t/pkg.txt"
  : > "$t/minos-built.txt"; : > "$t/minos-kept.txt"
  while IFS= read -r f; do
    rel=${f#"$r"/}; m=$(minos_of "$f")
    hn=$(sums_hash "$r/SHA256SUMS" "$rel"); hb=""
    [ -z "$b" ] || hb=$(sums_hash "$b/SHA256SUMS" "$rel")
    if [ -n "$hb" ] && [ "$hb" = "$hn" ]; then kept=$((kept + 1)); printf '%s\n' "${m:-none}" >> "$t/minos-kept.txt"; continue; fi
    built=$((built + 1)); printf '%s\n' "${m:-none}" >> "$t/minos-built.txt"
    if [ -z "$m" ]; then bad="$bad
$rel: no minimum macOS version"
    elif ! ver_le "$m" 15.0; then bad="$bad
$rel: minos $m"; fi
  done < "$t/pkg.txt"
  dist=$(sort "$t/minos-built.txt" | uniq -c | awk '{ printf "%s%s x%s", (NR > 1 ? ", " : ""), $2, $1 }')
  kdist=$(sort "$t/minos-kept.txt" | uniq -c | awk '{ printf "%s%s x%s", (NR > 1 ? ", " : ""), $2, $1 }')
  if [ "$built" = 0 ]; then fail "Mach-O files the package ships that this build made: none found"
  elif [ -z "$bad" ]; then ok "Mach-O files the package ships that this build made: $built, minos $dist: none needs a macOS newer than 15.0 (carried over unchanged from the base package: $kept${kdist:+, minos $kdist})"
  else fail "Mach-O files the package ships that this build made: $built, minos $dist; $(printf '%s' "$bad" | grep -c .) need a macOS newer than 15.0 or name none:"; printf '%s\n' "$bad" | grep . | indent 30; fi
}

# The logs of the build steps: a line that names Homebrew's prefix is a tool, header or library a step found there.
COMPONENT_LOGS="rm kms nvrm fb accel agdc nmsmn ryzenadj airdis dlss osver nvk boost vulkan plugin xlate nvencvt package spirv-llvm-translator"
prove_logs() {
  local w=$1 p=$2 c f n=0 hits=""
  for c in $COMPONENT_LOGS; do
    f=$w/logs/$c.log; [ -f "$f" ] || continue
    n=$((n + 1))
    hits="$hits$(grep -nE "(^|[^A-Za-z0-9_.])($p/|/opt/homebrew/)" "$f" 2>/dev/null | sed "s|^|$c.log:|" | sed -n 1,10p)
"
  done
  hits=$(printf '%s' "$hits" | grep . || true)
  if [ "$n" = 0 ]; then skipped "the logs of the build steps: none in $w/logs"
  elif [ -z "$hits" ]; then ok "lines of the $n build step logs that name $p/ or /opt/homebrew/: 0"
  else fail "lines of the $n build step logs that name $p/ or /opt/homebrew/: $(printf '%s\n' "$hits" | grep -c .) (a tool, header or library found there); the first:"; printf '%s\n' "$hits" | indent 30; fi
}

prove_checkout() {
  local s n
  if ! s=$(git -C "$HERE" status --porcelain 2>&1); then skipped "the checkout: git status failed: $s"; return 0; fi
  n=$(printf '%s' "$s" | grep -c . || true)
  if [ "$n" = 0 ]; then ok "the checkout after the build ($(git -C "$HERE" rev-parse --short HEAD)): git status --porcelain prints nothing (0 lines)"
  else fail "the checkout after the build: git status --porcelain prints $n line(s):"; printf '%s\n' "$s" | indent 30; fi
}

prove_package() {
  local w=$1 t=$2 r=$1/package/pkgroot b tb cnt v nsum
  if [ ! -f "$r/SHA256SUMS" ]; then skipped "the package: no staged package ($r)"; return 0; fi
  v=$(sed -n 1p "$r/VERSION" 2>/dev/null)
  tb=$(ls -t "$w"/package/*.tar.gz 2>/dev/null | sed -n 1p)
  b=$(base_dir "$w")
  if [ -n "$b" ] && [ -f "$b/SHA256SUMS" ]; then
    cnt=$(awk 'FNR == 1 { f++ } { q = substr($0, 67); sub(/^\.\//, "", q); h = substr($0, 1, 64) }
               f == 1 { base[q] = h; next }
               { seen[q] = 1; if (!(q in base)) add++; else if (base[q] != h) rep++; else same++ }
               END { for (q in base) if (!(q in seen)) rem++; printf "%d replaced, %d added, %d removed, %d unchanged", rep, add, rem, same }' \
          "$b/SHA256SUMS" "$r/SHA256SUMS")
  else cnt="(no base package found under $w/package/base to compare with)"; fi
  if [ -z "$tb" ]; then fail "the package: VERSION ${v:-none}; no tarball in $w/package"
  else ok "the package: VERSION ${v:-none}; $(basename "$tb"), $(size_of "$tb") bytes, sha256 $(sha256_file "$tb"); files against the base package's SHA256SUMS: $cnt (not uploaded: it stays on this machine)"; fi
  [ -n "$v" ] || fail "the package has no VERSION"
  nsum=$(grep -c . "$r/SHA256SUMS" || true)
  if ( cd "$r" && shasum -a 256 --strict -c SHA256SUMS --quiet ) > "$t/sums.txt" 2>&1; then
    ok "shasum -a 256 --strict -c SHA256SUMS in the package: $nsum files, every one matches"
  else
    fail "shasum -a 256 --strict -c SHA256SUMS in the package fails ($nsum lines):"; indent 20 < "$t/sums.txt"
  fi
}

# ---------------------------------------------------------------------------------------------------------- logs
cmd_logs() {
  local w=${1:-} f tl rep
  [ -n "$w" ] || usage
  say "== the newest logs of $w/logs, last 300 lines each"
  find "$w/logs" -type f -name '*.log' -exec ls -t {} + 2>/dev/null | sed -n 1,3p > "${TMPDIR:-/tmp}/nm-ci-logs.$$"
  tl=$(tc_logs "$w")
  # the toolchain's own newest log, when it is not among them
  f=$(find "$tl" -maxdepth 1 -type f -name '*.log' -exec ls -t {} + 2>/dev/null | sed -n 1p)
  [ -z "$f" ] || grep -qxF "$f" "${TMPDIR:-/tmp}/nm-ci-logs.$$" || printf '%s\n' "$f" >> "${TMPDIR:-/tmp}/nm-ci-logs.$$"
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    say ""; say "==== $f (last 300 of $(wc -l < "$f" | tr -d ' ') lines)"; tail -n 300 "$f"
  done < "${TMPDIR:-/tmp}/nm-ci-logs.$$"
  rm -f "${TMPDIR:-/tmp}/nm-ci-logs.$$"
  rep=$(ls -t "$w"/reports/failure-*.txt 2>/dev/null | sed -n 1p)
  if [ -n "$rep" ]; then say ""; say "==== the newest failure report: $rep"; cat "$rep"; fi
  say ""; say "== every line that says error: or STOP: (at most 60 per file)"
  find "$w/logs" "$w/reports" -type f \( -name '*.log' -o -name '*.txt' \) 2>/dev/null | LC_ALL=C sort | while IFS= read -r f; do
    if grep -qE 'error:|STOP:' "$f" 2>/dev/null; then
      say "---- $f: $(grep -cE 'error:|STOP:' "$f") line(s)"
      grep -nE 'error:|STOP:' "$f" | sed -n 1,60p
    fi
  done
  return 0
}

# ---------------------------------------------------------------------------------------------------------- reads
rhead() { say ""; say "---- $*"; }
cannot() { say "could not be read: $*"; }
# rrun <label> <program> <arguments...>: the program's output, or why it could not run
rrun() {
  local l=$1; shift
  rhead "$l"
  if [ ! -x "$1" ] && ! command -v "$1" > /dev/null 2>&1; then cannot "$1 is not there"; return 0; fi
  "$@" 2>&1 | sed -n 1,60p
  say "(exit ${PIPESTATUS[0]})"
}
cmd_reads() {
  local w=${1:-} pr r pkg f g u n home wr scratch v
  [ -n "$w" ] || usage
  pr=$(tc_prefix "$w"); r=$w/package/pkgroot
  wr=$(cd "$w" 2>/dev/null && pwd -P) || wr=$w
  home=${HOME:-}
  say "== reads of the build in $w (a record, not a check)"
  # a. the toolchain, as built here
  rrun "otool -L <toolchain>/bin/cmake (expected: /usr/lib/libcurl.4.dylib, nothing under /usr/local or /opt)" otool -L "$pr/bin/cmake"
  rrun "the toolchain's Python: ssl.OPENSSL_VERSION" "$pr/python/bin/python3.14" -c 'import ssl, sys; print(ssl.OPENSSL_VERSION); print(sys.version)'
  rrun "llvm-config --targets-built" "$pr/llvm/bin/llvm-config" --targets-built
  rhead "glslangValidator --version (first 3 lines)"
  if [ -x "$pr/bin/glslangValidator" ]; then "$pr/bin/glslangValidator" --version 2>&1 | sed -n 1,3p; else cannot "$pr/bin/glslangValidator is not there"; fi
  rrun "the toolchain's pkg-config --variable pc_path pkg-config" "$pr/bin/pkg-config" --variable pc_path pkg-config
  rhead "the toolchain's pkg-config --cflags --libs SPIRV-Tools (empty environment)"
  if [ -x "$pr/bin/pkg-config" ]; then env -i PATH=/usr/bin:/bin "$pr/bin/pkg-config" --cflags --libs SPIRV-Tools 2>&1; say "(exit $?)"
  else cannot "$pr/bin/pkg-config is not there"; fi
  rhead "ls -l <workdir>/tools/bin"
  if [ -d "$w/tools/bin" ]; then ls -l "$w/tools/bin" 2>&1 | sed "s|$wr|<workdir>|g; s|$w|<workdir>|g"; else cannot "$w/tools/bin is not there"; fi
  f=$(find "$w/out" -type f -name mesa_clc 2>/dev/null | LC_ALL=C sort | sed -n 1p)
  if [ -n "$f" ]; then rrun "otool -L ${f#"$w"/}" otool -L "$f"; else rhead "otool -L mesa_clc"; cannot "no mesa_clc under $w/out"; fi
  f=$(find "$w/out" -type f -name libvulkan_nouveau.dylib 2>/dev/null | LC_ALL=C sort | sed -n 1p)
  if [ -n "$f" ]; then rrun "otool -L ${f#"$w"/}" otool -L "$f"; else rhead "otool -L libvulkan_nouveau.dylib"; cannot "no libvulkan_nouveau.dylib under $w/out"; fi
  rhead "the Python environment's pyvenv.cfg: first line and home"
  if [ -f "$w/tools/venv/pyvenv.cfg" ]; then sed -n 1p "$w/tools/venv/pyvenv.cfg"; grep '^home' "$w/tools/venv/pyvenv.cfg" || say "(no home line)"
  else cannot "$w/tools/venv/pyvenv.cfg is not there"; fi
  rhead "otool -l <toolchain>/bin/ninja: LC_BUILD_VERSION"
  if [ -f "$pr/bin/ninja" ]; then otool -l "$pr/bin/ninja" 2>&1 | grep -A4 LC_BUILD_VERSION || say "(no LC_BUILD_VERSION)"; else cannot "$pr/bin/ninja is not there"; fi
  # b. Apple's stand-in programs in /usr/bin with a developer folder that holds nothing
  rhead "env -i DEVELOPER_DIR=<an empty folder> /usr/bin/make --version"
  scratch=$w/reads-scratch; rm -rf "$scratch"
  if [ ! -d "$w" ]; then cannot "$w is not there"
  elif [ "${GITHUB_ACTIONS:-}" != true ]; then cannot "made only in GitHub's job (on a Mac with a desktop it may be Apple's offer to install the command line tools)"
  elif ! mkdir -p "$scratch/empty-developer-dir"; then cannot "cannot make $scratch"
  else env -i DEVELOPER_DIR="$scratch/empty-developer-dir" /usr/bin/make --version < /dev/null 2>&1 | sed -n 1,10p; say "exit ${PIPESTATUS[0]}"; fi
  rm -rf "$scratch"
  rrun "xcode-select -p" xcode-select -p
  # c. the tarball ./build.sh package wrote
  pkg=$(ls -t "$w"/package/*.tar.gz 2>/dev/null | sed -n 1p)
  rrun "tar --version" tar --version
  rhead "the tarball: size and SHA-256"
  if [ -z "$pkg" ]; then
    cannot "no tarball in $w/package"
    for v in "owner/group of every header (tar tvzf)" "AppleDouble (._) entries" "LIBARCHIVE.xattr / SCHILY.xattr records" \
             "how often the account name occurs in the archive"; do rhead "$v"; cannot "no tarball in $w/package"; done
  else
    say "$(basename "$pkg"): $(size_of "$pkg") bytes, sha256 $(sha256_file "$pkg")"
    rhead "owner/group of every header (tar tvzf)"
    # bsdtar prints "mode links owner group size ...", GNU tar "mode owner/group size ..."
    tar tvzf "$pkg" 2>&1 | awk '{ if ($2 ~ /\//) print $2; else print $3 "/" $4 }' | sort | uniq -c
    rhead "AppleDouble (._) entries"
    tar tzf "$pkg" 2>/dev/null | grep -c -E '(^|/)\._' || true
    rhead "LIBARCHIVE.xattr / SCHILY.xattr records"
    gzip -dc "$pkg" 2>/dev/null | strings -a | grep -c -E 'LIBARCHIVE\.xattr|SCHILY\.xattr' || true
    u=$(id -un 2>/dev/null || true)
    rhead "how often the account name ($u) occurs in the archive"
    if [ -n "$u" ]; then gzip -dc "$pkg" 2>/dev/null | strings -a | grep -c -F "$u" || true; else cannot "id -un printed nothing"; fi
  fi
  if [ ! -d "$r" ]; then
    for v in "text files of pkgroot that hold the home folder" VERSION NULLMOTH-LOCAL-BUILD.txt "links in pkgroot" "set-uid and set-gid files in pkgroot" \
             "codesign -dv of each kext and of the four user-space products" "xattr -lr pkgroot: attributes and how many files carry each"; do
      rhead "$v"; cannot "no staged package ($r)"
    done
  else
    rhead "text files of pkgroot that hold the home folder"
    if [ -n "$home" ]; then grep -rlI -F -e "$home" "$r" 2>/dev/null | sed "s|^$r/||" | sed -n 1,20p; say "(end of list)"; else cannot "HOME is not set"; fi
    rhead "VERSION"; cat "$r/VERSION" 2>&1
    rhead "NULLMOTH-LOCAL-BUILD.txt"; sed "s|$home|~|g" "$r/NULLMOTH-LOCAL-BUILD.txt" 2>&1
    rhead "links in pkgroot"; find "$r" -type l -exec ls -l {} \; 2>&1 | sed "s|$r/||g"; say "(end of list)"
    rhead "set-uid and set-gid files in pkgroot"; find "$r" \( -perm -4000 -o -perm -2000 \) -print 2>&1 | sed "s|$r/||"; say "(end of list)"
    rhead "codesign -dv of each kext and of the four user-space products"
    if ! command -v codesign > /dev/null 2>&1; then cannot "codesign is not there"
    else
      for f in $(cd "$r" && find Library -name '*.kext' -type d -prune 2>/dev/null | LC_ALL=C sort) Library/GPUBundles/nvmtl/libvulkan_nouveau.dylib \
               Library/GPUBundles/nvmtl/libnvmtl_translate.dylib Library/GPUBundles/NVMTLDriver.bundle Library/NullMoth/boost/nvmtl-boost; do
        say "== $f"
        if [ -e "$r/$f" ]; then codesign -dv "$r/$f" 2>&1 | grep -E '^(Identifier|Signature|TeamIdentifier)|not signed' || say "(none of those lines)"
        else cannot "$f is not in the package"; fi
      done
    fi
    rhead "xattr -lr pkgroot: attributes and how many files carry each"
    if command -v xattr > /dev/null 2>&1; then xattr -lr "$r" 2>/dev/null | cut -d: -f2 | sort | uniq -c; say "(end of list)"; else cannot "xattr is not there"; fi
  fi
  # d. how much of the build machine's paths the compiled files carry (a measurement). Each file is handed to strings
  # on its standard input: given a Mach-O file by name, Apple's strings reads only its sections, also with -a, and
  # the names in its load commands (the libraries it loads, its run paths, its own name) are in none
  rhead "the home folder and the working directory in the compiled files (strings over every byte, load commands too), per Mach-O file with a count above 0"
  if [ -z "$home" ]; then cannot "HOME is not set"
  elif [ ! -d "$w/out" ] && [ ! -d "$r" ]; then cannot "neither $w/out nor a staged package is there"
  elif ! command -v strings > /dev/null 2>&1; then cannot "strings is not there"
  else
    scratch=$w/reads-scratch; rm -rf "$scratch"; mkdir -p "$scratch" 2>/dev/null || { cannot "cannot make $scratch"; return 0; }
    { machos "$w/out"; [ ! -d "$r" ] || machos "$r"; } > "$scratch/machos.txt"
    : > "$scratch/counts.txt"
    while IFS= read -r f; do
      n=$(strings -a < "$f" 2>/dev/null | grep -c -F "$home" || true)
      g=$(strings -a < "$f" 2>/dev/null | grep -c -F -e "$w" -e "$wr" || true)
      [ "${n:-0}" = 0 ] && [ "${g:-0}" = 0 ] || printf '%s %s %s\n' "${n:-0}" "${g:-0}" "$f" >> "$scratch/counts.txt"
    done < "$scratch/machos.txt"
    say "$(grep -c . "$scratch/machos.txt" || true) Mach-O files read, $(grep -c . "$scratch/counts.txt" || true) with a count above 0 (home folder, working directory, file):"
    sort -rn "$scratch/counts.txt" | sed "s|$wr|<workdir>|; s|$w|<workdir>|; s|$home|~|" | sed -n 1,200p
    sort -rn "$scratch/counts.txt" | sed -n 1,3p | while read -r n g f; do
      say "== ${f#"$w"/}: five lines"
      strings -a < "$f" 2>/dev/null | grep -F "$home" | sed -n 1,5p | sed "s|$home|~|g"
    done
    rm -rf "$scratch"
  fi
  say ""
  say "== end of the reads"
  return 0
}

case "${1:-}" in
  basepkg) shift; cmd_basepkg "$@" ;;
  brew-state) shift; cmd_brew_state "$@" ;;
  watch-start) shift; cmd_watch_start "$@" ;;
  watch-loop) shift; cmd_watch_loop "$@" ;;
  watch-stop) shift; cmd_watch_stop "$@" ;;
  deps-plan) shift; cmd_deps_plan "$@" ;;
  toolchain-id) shift; cmd_toolchain_id "$@" ;;
  prove) shift; cmd_prove "$@" ;;
  logs) shift; cmd_logs "$@" ;;
  reads) shift; cmd_reads "$@" ;;
  *) usage ;;
esac
