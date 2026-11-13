#!/bin/bash
# build_rm620.sh: NVIDIA's RM core 615.71.10 compiled for macOS (libnvkernel.a), the way the 610 archive was made:
# NVIDIA's own Makefile dry-run with TARGET_OS=Darwin, then cc -> clang -mkernel, -DNV_DARWIN, -fcf-protection=branch,
# without -mcmodel=kernel / -fno-pic, for macOS 15.0 like the kext that links it (built for the SDK's macOS instead, every
# object drew a linker note against NVRM's 15.0 target), and without the options the Makefile adds only when the build
# Mac's own compiler takes them (build/nv_compile_lines.py: the commands are an Apple-silicon Mac's on an Intel Mac too).
# Four headers learn NV_DARWIN; the patch chain is applied first.
# Usage: build_rm620.sh <work dir> <pristine open-gpu-kernel-modules-<version> tree>; the work dir gets ogkm-<version>.
set -uo pipefail
B=$(cd "${1:?work dir}" && pwd); V=${NV_VERSION:-620.70.06}
SRC=${2:?pristine open-gpu-kernel-modules tree}
T=$B/ogkm-$V
JOBS=${JOBS:-6}
HERE=$(cd "$(dirname "$0")/.." && pwd)
# The work tree is the pristine tree plus every patch in kexts/NVRM/patches, applied here, in file-name order, with no
# fuzz. They used to be applied by hand: the tree the archives were built from had five of the seven, and the two newest
# (hardware YUV 4:2:0, the colour-range control) were in the repository but in no binary. The names applied are recorded in
# the tree; a tree without that record was patched by hand and is refused.
if [ ! -d "$T" ]; then
  cp -R "$SRC" "$T" || exit 2
  : > "$T/.nm-patches"
  for f in "$HERE"/kexts/NVRM/patches/*.patch; do
    ok=0
    for root in "" "src/nvidia-modeset"; do
      ( cd "$T/$root" && patch -p1 --fuzz=0 --dry-run < "$f" >/dev/null 2>&1 && patch -p1 --fuzz=0 --no-backup-if-mismatch < "$f" >/dev/null ) && { ok=1; break; }
    done
    [ $ok = 1 ] || { echo "STOP: $(basename "$f") does not apply to $SRC"; rm -rf "$T"; exit 2; }
    basename "$f" >> "$T/.nm-patches"
  done
  find "$T/src" -name '*.orig' -delete
fi
[ -f "$T/.nm-patches" ] || { echo "STOP: $T has no record of the patches applied to it (patched by hand?); use a new work dir"; exit 2; }
want=$(cd "$HERE/kexts/NVRM/patches" && ls *.patch | LC_ALL=C sort | tr '\n' ' ')
have=$(LC_ALL=C sort "$T/.nm-patches" | tr '\n' ' ')
[ "$want" = "$have" ] || { echo "STOP: $T was built with [$have], this tree has [$want]; use a new work dir"; exit 2; }
echo "patch chain applied: $have"
python3 - "$T" <<'PY' || exit 3
import os, sys
t = sys.argv[1]
def edit(rel, old, new):
    p = os.path.join(t, rel); s = open(p).read()
    if new in s: return
    assert s.count(old) == 1, f"{rel}: anchor found {s.count(old)} times"
    s = s.replace(old, new); open(p + ".tmp", "w").write(s); os.replace(p + ".tmp", p)
edit("src/common/sdk/nvidia/inc/cpuopsys.h", "defined(__INTEGRITY) ) && !defined(nvmacosx)", "defined(__INTEGRITY) || defined(NV_DARWIN) ) && !defined(nvmacosx)")
for rel in ("src/common/inc/nvVer.h", "src/common/inc/nvUnixVersion.h"):
    edit(rel, "defined(NV_QNX) || defined(NV_INTEGRITY) || \\", "defined(NV_QNX) || defined(NV_INTEGRITY) || defined(NV_DARWIN) || \\")
edit("src/common/inc/nvBldVer.h", "#if defined(NV_LINUX) || defined(NV_BSD) || defined(NV_SUNOS)\n", "#if defined(NV_LINUX) || defined(NV_BSD) || defined(NV_SUNOS) || defined(NV_DARWIN)\n")
# the OS-timer fix comes from the patch chain (610-fsp-timer-double-destroy.patch); it must be there
assert "pTmrEvent->super.pOSTmrCBdata = NULL;" in open(os.path.join(t, "src/nvidia/src/kernel/gpu/timer/timer_ostimer.c")).read(), "the OS-timer fix is missing"
print("source edits applied")
PY
cd "$T/src/nvidia" || exit 2
O=_out/Darwin_x86_64; mkdir -p "$O"
# "make -n" lists only what is out of date: run again in a folder built before, it listed one command and compile_cmds.sh,
# which the kext builds read their -D flags from, lost the rest. It is asked against an empty output folder, as on a
# first build, and the built one is put back. NVIDIA's makefiles also record the building account and host (whoami,
# hostname) for the identity string, which is replaced below: they are given neutral values, so make-n.txt names neither.
[ ! -d "$O.nm-aside" ] || { rm -rf "$O" && mv "$O.nm-aside" "$O"; } || exit 2   # left by an interrupted run
[ -z "$(ls -A "$O")" ] || { mv "$O" "$O.nm-aside" && mkdir -p "$O"; } || exit 2
make -n TARGET_OS=Darwin TARGET_ARCH=x86_64 NV_VERBOSE=1 NV_BUILD_USER=build NV_BUILD_HOST=localhost 2>/dev/null > "$B/make-n.txt"
[ ! -d "$O.nm-aside" ] || { rm -rf "$O" && mv "$O.nm-aside" "$O"; } || exit 2
# the generated version header and string file are make steps, not compiles: run those first
grep -E "^echo .*version\.h" "$B/make-n.txt" > "$B/pre.sh"; bash "$B/pre.sh"
# NVIDIA's rule bakes user@host and the date into the identity string; the shipped string names no machine or person.
printf '%s\n' 'const char  NVRM_ID[] = "nvidia id: NVIDIA UNIX Open Kernel Module for x86_64 '"$V"' Release Build (NullMoth Systems)";' \
  'const char *const pNVRM_ID =  NVRM_ID + 11;' > "$O/g_nvid_string.c"
# the compile commands for macOS, the same on an Intel and an Apple-silicon Mac (build/nv_compile_lines.py says why)
grep " -c " "$B/make-n.txt" | python3 -B "$HERE/build/nv_compile_lines.py" rm > "$O/compile_cmds.sh.new" \
  || { echo "STOP: the RM's compile commands could not be made from $B/make-n.txt"; rm -f "$O/compile_cmds.sh.new"; exit 2; }
# never a shorter list than the one there: a list that lost commands would give the kexts the wrong -D flags
n=$(wc -l < "$O/compile_cmds.sh.new")
if [ -f "$O/compile_cmds.sh" ] && [ "$n" -lt "$(wc -l < "$O/compile_cmds.sh")" ]; then
  echo "STOP: make -n listed $n compile commands, fewer than the $(wc -l < "$O/compile_cmds.sh" | tr -d ' ') of the list there; kept that list. Use a new work dir"
  rm -f "$O/compile_cmds.sh.new"; exit 2
fi
mv "$O/compile_cmds.sh.new" "$O/compile_cmds.sh"; echo "compile commands: $n"
# NM_PREPARE_ONLY=1 stops here: the patched tree and the RM's compile line are all build/kext_check.sh needs, and
# everything up to this point runs on any host with make, patch and python3 (no Xcode).
[ -z "${NM_PREPARE_ONLY:-}" ] || { echo "prepared $T: $n compile commands, nothing compiled (NM_PREPARE_ONLY)"; exit 0; }
rm -rf "$O/.err"; mkdir -p "$O/.err"
rm -rf "$B/cmds"; mkdir -p "$B/cmds"; awk -v d="$B/cmds" '{ f = d "/" NR ".sh"; print > f; close(f) }' "$O/compile_cmds.sh"
seq 1 "$n" | xargs -P "$JOBS" -I{} bash -c 'bash "'"$B"'/cmds/{}.sh" 2> "'"$O"'/.err/{}.txt" || echo "FAIL {}"' > "$B/fails.txt"
nf=$(grep -c FAIL "$B/fails.txt"); echo "failed: $nf of $n"
[ "$nf" = 0 ] || { head -5 "$B/fails.txt"; for i in $(grep FAIL "$B/fails.txt" | head -3 | awk '{print $2}'); do sed -n "${i}p" "$O/compile_cmds.sh" | grep -oE " -c [^ ]+"; grep -m3 "error:" "$O/.err/$i.txt"; done; }
rm -f "$O/libnvkernel.a"
ls "$O"/*.o > "$B/objs.txt"; xcrun libtool -static -o "$O/libnvkernel.a" -filelist "$B/objs.txt" 2>"$B/libtool.err" || { tail -5 "$B/libtool.err"; exit 4; }
echo "libnvkernel.a $(stat -f%z "$O/libnvkernel.a") B from $(wc -l < "$B/objs.txt") objects"
# the building account or the computer's short name in the forms a build leaks them in: a path in the home folder
# ($HOME, /Users/<account>), <account>@ and @<computer> (NVIDIA's identity string is user@host), <computer>.local. A
# bare word is reported, not stopped at: an account named like an ordinary word (admin, user, test, dev) occurs as a
# word in NVIDIA's own strings, and a short name inside a word ("max" in "maximum") is not one. The names are not printed.
u=$(id -un 2>/dev/null || true); h=$(hostname -s 2>/dev/null || true); pats=()
[ -z "$u" ] || pats+=( -e "/Users/$u" -e "$u@" )
[ -z "$h" ] || pats+=( -e "@$h" -e "$h.local" )
case "${HOME:-/}" in /) ;; *) pats+=( -e "${HOME%/}" ) ;; esac
if [ ${#pats[@]} = 0 ]; then echo "personal strings in libnvkernel.a: not checked (no account or computer name)"; exit 0; fi
left=$(strings -a "$O/libnvkernel.a" | grep -ciF "${pats[@]}" || true)
[ "$left" = 0 ] || { echo "STOP: $left string(s) in libnvkernel.a hold the home folder, the account name with @ or the computer's name; see them with: strings -a $T/src/nvidia/$O/libnvkernel.a | grep -iF -e \"\$HOME\" -e \"\$(id -un)@\" -e \"@\$(hostname -s)\""; exit 6; }
re=$(for x in "$u" "$h"; do [ -z "$x" ] || printf '%s\n' "$x" | sed 's/[][\.*^$+?(){}|]/\\&/g'; done | paste -sd'|' -)
words=0; [ -z "$re" ] || words=$(strings -a "$O/libnvkernel.a" | grep -ciE "(^|[^[:alnum:]])($re)([^[:alnum:]]|$)" || true)
echo "personal strings in libnvkernel.a: 0"
[ "$words" = 0 ] || echo "note: $words string(s) in libnvkernel.a hold the account or computer name as an ordinary word, not as a path or user@host (see them with: strings -a $T/src/nvidia/$O/libnvkernel.a | grep -iw \"\$(id -un)\")"
