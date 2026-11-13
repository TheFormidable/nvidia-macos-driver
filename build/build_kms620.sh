#!/bin/bash
# build_kms620.sh: NVIDIA's modeset core compiled for macOS (libnvkms.a) from the work tree build_rm620.sh made, which
# already carries every patch in kexts/NVRM/patches. Same transform as RM, for macOS 15.0.
# Usage: build_kms620.sh <work dir> (the one build_rm620.sh filled).
set -uo pipefail
B=$(cd "${1:?work dir}" && pwd); JOBS=${JOBS:-6}; V=${NV_VERSION:-620.70.06}
[ -f "$B/ogkm-$V/.nm-patches" ] || { echo "STOP: $B/ogkm-$V has no record of the patches applied to it; run build_rm620.sh in a new work dir"; exit 2; }
cd "$B/ogkm-$V/src/nvidia-modeset" || exit 2
O=_out/Darwin_x86_64; mkdir -p "$O"
# against an empty output folder, so that the list is complete when it was built before (see build_rm620.sh)
[ ! -d "$O.nm-aside" ] || { rm -rf "$O" && mv "$O.nm-aside" "$O"; } || exit 2
[ -z "$(ls -A "$O")" ] || { mv "$O" "$O.nm-aside" && mkdir -p "$O"; } || exit 2
make -n TARGET_OS=Darwin TARGET_ARCH=x86_64 NV_VERBOSE=1 NV_BUILD_USER=build NV_BUILD_HOST=localhost 2>/dev/null > "$B/kms-make-n.txt"
[ ! -d "$O.nm-aside" ] || { rm -rf "$O" && mv "$O.nm-aside" "$O"; } || exit 2
grep " -c " "$B/kms-make-n.txt" | python3 -c '
import sys
for l in sys.stdin:
    l = l.rstrip("\n")
    cc, rest = l.split(" ", 1)
    tool = {"cc": "clang", "c++": "clang++"}[cc]   # the DisplayPort library is C++
    print(tool + " -arch x86_64 -mmacosx-version-min=15.0 -mkernel -DNV_DARWIN -fcf-protection=branch " + rest.replace(" -mcmodel=kernel", "").replace(" -fno-pic", ""))' > "$O/compile_cmds.sh.new"
n=$(wc -l < "$O/compile_cmds.sh.new" | tr -d " ")
if [ -f "$O/compile_cmds.sh" ] && [ "$n" -lt "$(wc -l < "$O/compile_cmds.sh")" ]; then
  echo "STOP: make -n listed $n modeset compile commands, fewer than the list there; kept that list. Run build_rm620.sh in a new work dir"
  rm -f "$O/compile_cmds.sh.new"; exit 2
fi
mv "$O/compile_cmds.sh.new" "$O/compile_cmds.sh"; echo "modeset compile commands: $n"
# the identity string is a make step (user@host and the date); write the shipped wording instead
printf '%s\n' 'const char  NV_KMS_ID[] = "nvidia id: NVIDIA UNIX Open Kernel Mode Setting Driver for x86_64 '"$V"' Release Build (NullMoth Systems)";' \
  'const char *const pNV_KMS_ID =  NV_KMS_ID + 11;' > "$O/g_nvid_string.c"
rm -rf "$O/.err" "$B/kcmds"; mkdir -p "$O/.err" "$B/kcmds"
awk -v d="$B/kcmds" '{ f = d "/" NR ".sh"; print > f; close(f) }' "$O/compile_cmds.sh"
seq 1 "$n" | xargs -P "$JOBS" -I{} bash -c 'bash "'"$B"'/kcmds/{}.sh" 2> "'"$O"'/.err/{}.txt" || echo "FAIL {}"' > "$B/kfails.txt"
nf=$(grep -c FAIL "$B/kfails.txt"); echo "failed: $nf of $n"
[ "$nf" = 0 ] || { for i in $(grep FAIL "$B/kfails.txt" | head -3 | awk '{print $2}'); do grep -oE " -c [^ ]+" "$B/kcmds/$i.sh"; grep -m3 "error:" "$O/.err/$i.txt"; done; exit 5; }
rm -f "$O/libnvkms.a"; ls "$O"/*.o > "$B/kobjs.txt"
xcrun libtool -static -o "$O/libnvkms.a" -filelist "$B/kobjs.txt" 2>"$B/klibtool.err" || { tail -5 "$B/klibtool.err"; exit 4; }
echo "libnvkms.a $(stat -f%z "$O/libnvkms.a") B from $(wc -l < "$B/kobjs.txt" | tr -d " ") objects"
# the account or the computer's short name in the forms a build leaks them in (see build_rm620.sh); names not printed
u=$(id -un 2>/dev/null || true); h=$(hostname -s 2>/dev/null || true); pats=()
[ -z "$u" ] || pats+=( -e "/Users/$u" -e "$u@" )
[ -z "$h" ] || pats+=( -e "@$h" -e "$h.local" )
case "${HOME:-/}" in /) ;; *) pats+=( -e "${HOME%/}" ) ;; esac
if [ ${#pats[@]} = 0 ]; then echo "personal strings in libnvkms.a: not checked (no account or computer name)"; exit 0; fi
left=$(strings -a "$O/libnvkms.a" | grep -ciF "${pats[@]}" || true)
[ "$left" = 0 ] || { echo "STOP: $left string(s) in libnvkms.a hold the home folder, the account name with @ or the computer's name; see them with: strings -a $B/ogkm-$V/src/nvidia-modeset/$O/libnvkms.a | grep -iF -e \"\$HOME\" -e \"\$(id -un)@\" -e \"@\$(hostname -s)\""; exit 6; }
re=$(for x in "$u" "$h"; do [ -z "$x" ] || printf '%s\n' "$x" | sed 's/[][\.*^$+?(){}|]/\\&/g'; done | paste -sd'|' -)
words=0; [ -z "$re" ] || words=$(strings -a "$O/libnvkms.a" | grep -ciE "(^|[^[:alnum:]])($re)([^[:alnum:]]|$)" || true)
echo "personal strings in libnvkms.a: 0"
[ "$words" = 0 ] || echo "note: $words string(s) in libnvkms.a hold the account or computer name as an ordinary word, not as a path or user@host (see them with: strings -a $B/ogkm-$V/src/nvidia-modeset/$O/libnvkms.a | grep -iw \"\$(id -un)\")"
