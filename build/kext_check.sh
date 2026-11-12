#!/bin/bash
# kext_check.sh [work dir]: compile every kext source of this tree to a Mach-O object, without linking, and report
# what the objects import. It needs no built RM or NVKMS archive and no NVIDIA hardware, so it runs in a few minutes on
# a Mac or on Linux and is what continuous integration runs on every push. It answers three questions:
#   1. does every kext source still compile, with the flags its build script uses, at or below the warning ceiling
#      (build/warnings.txt);
#   2. is every kexts/NVRM/*.cpp in build_nvrm.sh's CXX_SOURCES and every kexts/NVRM/*.c compiled by build_nvrm.sh
#      (kext_check.sh --sources answers this one alone and needs no input);
#   3. does any object import a symbol that nothing will provide when the kext loads, on macOS 15 or on macOS 26: one
#      the kernel does not export to kexts, or exports through the Private KPI only (such a kext links and never loads).
# It does not link, so it cannot see a duplicate or a missing definition between objects; build_nvrm.sh on a Mac does.
#
# Inputs, each found in this order: the environment, an earlier run's copy under <work dir>/src, a pinned download.
#   NM_OGKM620_PRISTINE   NVIDIA open-gpu-kernel-modules 620.70.06, untouched (MIT / GPL-2.0, github.com/NVIDIA)
#   NM_KERNEL_HEADERS     Kernel.framework/Headers. On a Mac the default is Xcode's. Elsewhere MacKernelSDK
#                         (github.com/acidanthera/MacKernelSDK: Apple's kernel headers, APSL-2.0) is downloaded.
#   NM_XNU_EXPORTS        folders holding xnu's config/*.exports, one per kernel, separated by ':'
#                         (apple-oss-distributions/xnu, APSL-2.0). Unset: the tags in XNU_TAGS are downloaded.
#   NM_CHECK_OFFLINE=1    never download; stop if an input is missing
# Nothing downloaded is copied into the repository or into anything that ships.
# Output: one line per unit ("OK <unit> warnings N" / "FAIL <unit>"), then the import report. Exit 0 when all passed.
# Written for the bash 3.2 a Mac has as well as for a current one.
set -uo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd)
say() { printf '%s\n' "$*"; }
stop() { say "STOP: $*"; exit 2; }

# ---- question 2: the sources build_nvrm.sh compiles are the sources that are there
sources() {
  local bn=$HERE/build/build_nvrm.sh src=$HERE/kexts/NVRM code always listed f s bad=0
  code=$(grep -v '^[[:space:]]*#' "$bn") || { say "FAIL build/build_nvrm.sh cannot be read"; return 1; }
  # CXX_SOURCES=( ... ) names what is always compiled; a line with CXX_SOURCES+=( names what is compiled when present
  # (the list ends at the first ')' at or after its first line: written on one line it is still only the list)
  always=$(say "$code" | awk '/^CXX_SOURCES=\(/ { on = 1 } on { print } on && /\)/ { exit }' | tr -s ' ();' '\n' | grep '^[A-Za-z0-9_.+-]*\.cpp$' | tr '\n' ' ')
  listed=" $always$(say "$code" | sed -n '/CXX_SOURCES+=(/p' | tr -s ' ();' '\n' | grep '^[A-Za-z0-9_.+-]*\.cpp$' | tr '\n' ' ')"
  [ -n "$always" ] || { say "FAIL build/build_nvrm.sh has no CXX_SOURCES=( ... ) list this check can read"; bad=1; }
  for f in "$src"/*.cpp; do
    [ -f "$f" ] || continue; s=$(basename "$f")
    case "$listed" in *" $s "*) ;; *) say "FAIL NVRM/$s is not in CXX_SOURCES of build/build_nvrm.sh (the kext would be built without it): add it to the list"; bad=1;; esac
  done
  for s in $always; do
    [ -f "$src/$s" ] || { say "FAIL NVRM/$s is in CXX_SOURCES of build/build_nvrm.sh and not in kexts/NVRM (the build would stop)"; bad=1; }
  done
  for f in "$src"/*.c; do
    [ -f "$f" ] || continue; s=$(basename "$f")
    say "$code" | grep -q -F "\"\$SRC/$s\"" || { say "FAIL NVRM/$s is compiled by nothing in build/build_nvrm.sh (the kext would be built without it): add its compile line"; bad=1; }
  done
  return $bad
}
if [ "${1:-}" = --sources ]; then
  sources && say "every kexts/NVRM source is one build/build_nvrm.sh compiles"; exit $?
fi

WORK=${1:-$HERE/build-out/kext-check}; mkdir -p "$WORK/src" "$WORK/obj"; WORK=$(cd "$WORK" && pwd)
V=620.70.06
OGKM_SHA=e6a2e01579fbbd1ca2ed43fef2ee2589e9bd8bd9          # tag 620.70.06
MKSDK_SHA=3f750085caa17ec3a7880f11c11bf4f48cd6a164         # MacKernelSDK master, 2026-09-23
# An import has to be exported by the oldest kernel the kexts run on and by the newest one: PE_boot_args, for one, is in
# IOKit.exports of macOS 26 and not of macOS 15, so a kext that calls it loads on one and not on the other.
XNU_TAGS=( xnu-11215.1.10 xnu-12377.1.9 )                  # macOS 15.0 (the deployment target) and macOS 26.0
fetch() {   # fetch <url> <ref> <dest> [sha]: a shallow clone of one tag or commit, verified
  local url=$1 ref=$2 dest=$3 sha=${4:-}
  if [ -d "$dest" ]; then   # an earlier run's clone, or one a cache restored: it is used only while it is still the pinned commit
    [ -z "$sha" ] || [ ! -e "$dest/.git" ] || [ "$(git -C "$dest" rev-parse HEAD 2>/dev/null)" = "$sha" ] || stop "$dest is not commit $sha of $url: remove it and run again"
    return 0
  fi
  [ "${NM_CHECK_OFFLINE:-0}" = 1 ] && stop "$dest is missing and NM_CHECK_OFFLINE=1"
  rm -rf "$dest.part"; git init -q "$dest.part" && git -C "$dest.part" remote add origin "$url" \
    && git -C "$dest.part" fetch -q --depth 1 origin "$ref" && git -C "$dest.part" checkout -q FETCH_HEAD || stop "cannot fetch $ref from $url"
  [ -z "$sha" ] || [ "$(git -C "$dest.part" rev-parse HEAD)" = "$sha" ] || stop "$url $ref is not commit $sha"
  mv "$dest.part" "$dest"
}
MAC=0; [ "$(uname -s)" = Darwin ] && MAC=1
# ---- inputs
PRISTINE=${NM_OGKM620_PRISTINE:-$WORK/src/open-gpu-kernel-modules-$V}
[ -n "${NM_OGKM620_PRISTINE:-}" ] || fetch https://github.com/NVIDIA/open-gpu-kernel-modules "$V" "$PRISTINE" "$OGKM_SHA"
[ -d "$PRISTINE/src/nvidia" ] || stop "no NVIDIA open-gpu-kernel-modules $V tree at $PRISTINE"
if [ -n "${NM_KERNEL_HEADERS:-}" ]; then KHDR=$NM_KERNEL_HEADERS
elif [ $MAC = 1 ]; then KHDR="$(xcrun --show-sdk-path)/System/Library/Frameworks/Kernel.framework/Headers"
else fetch https://github.com/acidanthera/MacKernelSDK "$MKSDK_SHA" "$WORK/src/MacKernelSDK" "$MKSDK_SHA"; KHDR=$WORK/src/MacKernelSDK/Headers; fi
[ -f "$KHDR/IOKit/IOService.h" ] || stop "no kernel headers at $KHDR"
if [ $MAC = 1 ]; then CXX=(xcrun clang++ -arch x86_64 -mmacosx-version-min=15.0); CC=(xcrun clang -arch x86_64 -mmacosx-version-min=15.0); NM="xcrun nm"
else
  CXX=(clang++ -target x86_64-apple-macos15.0); CC=(clang -target x86_64-apple-macos15.0)
  command -v clang++ >/dev/null || stop "no clang++ on PATH"
  NM=llvm-nm; command -v $NM >/dev/null || NM=llvm-nm-$(clang++ -dumpversion | cut -d. -f1)   # a distribution may ship only the versioned name
  command -v $NM >/dev/null || stop "no llvm-nm on PATH (it reads the Mach-O objects; the llvm package has it)"
fi
# Apple's clang keeps a kext's global destructors in __mod_term_func. LLVM's own clang turns them into __cxa_atexit
# calls for every Mach-O target, a symbol no kext may import; this option restores the kext form. A clang without the
# option still serves the compile check, and the import check is told to expect the two symbols.
ATEXIT=
if [ $MAC = 0 ]; then
  if echo 'int x;' | "${CXX[@]}" -mllvm -disable-atexit-based-global-dtor-lowering -x c++ -c - -o /dev/null 2>/dev/null; then
    CXX+=( -mllvm -disable-atexit-based-global-dtor-lowering )
  else ATEXIT=--toolchain-atexit; fi
fi
# ---- the patched NVIDIA tree and the RM's compile line, exactly as build_rm620.sh makes them (it stops before compiling)
T=$WORK/ogkm-$V
want=$(cd "$HERE/kexts/NVRM/patches" && ls *.patch | LC_ALL=C sort | tr '\n' ' ')
have=$(LC_ALL=C sort "$T/.nm-patches" 2>/dev/null | tr '\n' ' ')
if [ "$want" != "$have" ] || [ ! -f "$T/src/nvidia/_out/Darwin_x86_64/compile_cmds.sh" ]; then
  rm -rf "$T"
  NM_PREPARE_ONLY=1 bash "$HERE/build/build_rm620.sh" "$WORK" "$PRISTINE" > "$WORK/prepare.log" 2>&1 || { tail -5 "$WORK/prepare.log"; stop "the NVIDIA tree could not be prepared (see $WORK/prepare.log)"; }
fi
NV="$T/src/nvidia"; KMS="$T/src/nvidia-modeset"; SRC=$HERE/kexts/NVRM; O=$WORK/obj; rm -f "$O"/*.o "$O"/*.err "$O"/warn.*
RMINC=( -I"$NV/arch/nvalloc/unix/include" -I"$NV/arch/nvalloc/common/inc" -I"$NV/arch/nvalloc/common/inc/gsp"
  -I"$T/src/common/sdk/nvidia/inc" -I"$T/src/common/sdk/nvidia/inc/hw" -I"$T/src/common/inc"
  -I"$T/src/common/shared/inc" -I"$NV/inc" -I"$NV/inc/os" -I"$NV/inc/kernel" -I"$NV/kernel/inc" -I"$NV/interface"
  -I"$T/src/common/nvlink/interface" -I"$T/src/common/inc/swref" -I"$T/src/common/inc/swref/published"
  -I"$NV/inc/libraries" -I"$NV/src/libraries" -I"$NV/generated" -I"$NV/src/mm/uvm/interface"
  -I"$T/src/common/uproc/os/libos-v2.0.0/include" -I"$T/src/common/uproc/os/common/include"
  -I"$T/src/common/inc/displayport" -I"$T/src/common/nvlink/inband/interface" )
KMSINC=( -I"$KMS/os-interface/include" -I"$KMS/kapi/interface" -I"$KMS/kapi/include" -I"$T/src/common/unix/nvidia-push/interface" -I"$KMS/interface" -I"$KMS/include"
         -I"$T/src/common/unix/common/inc" -I"$T/src/common/modeset" -I"$T/src/common/unix/common/utils/interface" )
RMDEFS=(); while read -r d; do RMDEFS+=("$d"); done < <(head -1 "$NV/_out/Darwin_x86_64/compile_cmds.sh" | tr ' ' '\n' | grep -E '^-D' | sed -e 's/"//g' | grep -v -E '^-D(NVRM|_LANGUAGE_C)$')
[ ${#RMDEFS[@]} -gt 0 ] || stop "the RM's compile line in $NV/_out/Darwin_x86_64/compile_cmds.sh names no -D"
# KEXT_FLAGS is the flag set every build script under build/ and kexts/*/build.sh gives its compiler; tools/test_kext_check.py
# compares the two, so a flag added to a build script and not here fails a host test.
KEXT_FLAGS=( -fapple-kext -mkernel -nostdinc -DKERNEL -DKERNEL_PRIVATE -DDRIVER_PRIVATE -DAPPLE -DNeXT
  -g0 -std=c++17 -fno-rtti -fno-exceptions -fno-builtin -fno-common
  -Wall -Wno-unused-parameter -Wno-inconsistent-missing-override -Wno-unused-function -O2 )
RMFLAGS=( "${KEXT_FLAGS[@]}" -I"$KHDR" -I"$SRC" "${RMINC[@]}" "${KMSINC[@]}" "${RMDEFS[@]}" )
rc=0; units=0
unit() {   # unit <exit status of the compile> <unit> <ceiling component> <object stem>: the warnings are added up per component
  local w; units=$((units + 1))
  if [ "$1" = 0 ]; then
    w=$(grep -c ': warning:' "$O/$4.err" || true); say "OK   $2  warnings $w"; echo "$w" >> "$O/warn.$3"
  else say "FAIL $2"; grep -E 'error' "$O/$4.err" | head -15; rc=1; fi
}
one() {   # one <unit> <ceiling component> <object stem> <compiler and args...>
  local name=$1 comp=$2 stem=$3; shift 3
  "$@" -o "$O/$stem.o" 2>"$O/$stem.err"; unit $? "$name" "$comp" "$stem"
}
# ---- NVRM.kext
sources || rc=1
for f in "$SRC"/*.cpp; do
  s=$(basename "$f"); one "NVRM/$s" nvrm "nvrm-${s%.cpp}" "${CXX[@]}" "${RMFLAGS[@]}" -c "$f"
done
RMCC_CC=; [ $MAC = 1 ] || RMCC_CC="${CC[*]}"
for f in "$SRC"/*.c; do
  s=$(basename "$f")
  if grep -v '^[[:space:]]*#' "$HERE/build/build_nvrm.sh" | grep -F "\"\$SRC/$s\"" | grep -q 'rmcc\.py'; then
    # an RM-internal file: build_nvrm.sh gives it the RM's own command line for osapi.c (kexts/NVRM/rmcc.py), and so does this
    OGKM="$T" NM_RMCC_CC="$RMCC_CC" python3 -B "$SRC/rmcc.py" "$f" "$O/nvrm-${s%.c}.o" 2>"$O/nvrm-${s%.c}.err"
    unit $? "NVRM/$s (the RM's compile line)" nvrm "nvrm-${s%.c}"
  else
    one "NVRM/$s" nvrm "nvrm-${s%.c}" "${CC[@]}" "${RMFLAGS[@]/-std=c++17/-std=gnu11}" -x c -c "$f"
  fi
done
# ---- NVAccel.kext, the macOS 15 and the macOS 26 (NM_TAHOE) builds
ACC=( -I"$SRC/accel" -I"$SRC/accel/iofam" -I"$SRC/accel/re" -c "$SRC/accel/nvrm-accel.cpp" )
one "NVAccel/nvrm-accel.cpp" nvaccel accel "${CXX[@]}" "${RMFLAGS[@]}" "${ACC[@]}"
one "NVAccel/nvrm-accel.cpp (NM_TAHOE)" nvaccel accel-tahoe "${CXX[@]}" "${RMFLAGS[@]}" -DNM_TAHOE "${ACC[@]}"
# ---- NVRMFB.kext
FBINC=( -I"$KHDR" -I"$HERE/kexts/NVRMFB" -I"$HERE/kexts/NVRMFB/fb"
  -I"$T/src/nvidia/arch/nvalloc/unix/include" -I"$T/src/common/sdk/nvidia/inc" -I"$T/src/common/inc" -I"$T/src/common/shared/inc"
  -I"$KMS/os-interface/include" -I"$KMS/kapi/interface" -I"$KMS/kapi/include" -I"$KMS/interface" -I"$KMS/include"
  -I"$T/src/common/unix/common/inc" -I"$T/src/common/modeset" -I"$T/src/common/unix/common/utils/interface" )
# kIORangeSupportsVRR is in Xcode's IOGraphicsTypes.h and not in MacKernelSDK's older one; any value serves a compile check
FBX=(); grep -q kIORangeSupportsVRR "$KHDR/IOKit/graphics/IOGraphicsTypes.h" 2>/dev/null || FBX=( -DkIORangeSupportsVRR=0x00100000 )
one "NVRMFB/nvrm-fb.cpp" nvrmfb fb "${CXX[@]}" "${KEXT_FLAGS[@]}" ${FBX[@]+"${FBX[@]}"} "${FBINC[@]}" -c "$HERE/kexts/NVRMFB/fb/nvrm-fb.cpp"
# ---- the kexts that need only kernel headers
K=$HERE/kexts
one "NVRMAGDC/nvrm-agdc.cpp" nvrmagdc agdc "${CXX[@]}" "${KEXT_FLAGS[@]}" -I"$KHDR" -I"$K/NVRMAGDC" -c "$K/NVRMAGDC/nvrm-agdc.cpp"
one "NMGopFB/gopfb.cpp" nmgopfb gopfb "${CXX[@]}" "${KEXT_FLAGS[@]}" -I"$KHDR" -I"$K/NMGopFB" -I"$K/NMIntelFB" -I"$K/NVRM/accel" -c "$K/NMGopFB/gopfb.cpp"
one "NMIntelFB/intelfb.cpp" nmintelfb intelfb "${CXX[@]}" "${KEXT_FLAGS[@]}" -I"$KHDR" -I"$K/NMIntelFB" -I"$K/NVRM/accel" -I"$K/NVRM" -UNMINTELFB_VIRTUAL -c "$K/NMIntelFB/intelfb.cpp"
one "NMIntelFB/intelfb.cpp (virtual)" nmintelfb intelfb-virtual "${CXX[@]}" "${KEXT_FLAGS[@]}" -I"$KHDR" -I"$K/NMIntelFB" -I"$K/NVRM/accel" -I"$K/NVRM" -DNMINTELFB_VIRTUAL -c "$K/NMIntelFB/intelfb.cpp"
# IOMallocData / IOFreeData (macOS 12) are in Xcode's IOLib.h and not in MacKernelSDK's: declare them for the check
SMNX=(); grep -q IOMallocData "$KHDR/IOKit/IOLib.h" || { printf '%s\n' '#include <IOKit/IOLib.h>' '__BEGIN_DECLS' 'void *IOMallocData(vm_size_t size);' \
  'void IOFreeData(void *address, vm_size_t size);' '__END_DECLS' > "$WORK/iolib_data.h"; SMNX=( -include "$WORK/iolib_data.h" ); }
one "NMSMN/nmsmn.cpp" nmsmn nmsmn "${CXX[@]}" "${KEXT_FLAGS[@]}" ${SMNX[@]+"${SMNX[@]}"} -I"$KHDR" -I"$K/NMSMN" -c "$K/NMSMN/nmsmn.cpp"
one "NMDRM/nmdrm.cpp" nmdrm nmdrm "${CXX[@]}" -fapple-kext -mkernel -nostdinc -I"$KHDR" -DKERNEL -DKERNEL_PRIVATE -DDRIVER_PRIVATE -DAPPLE -DNeXT \
  -std=c++17 -fno-rtti -fno-exceptions -fno-builtin -fno-common -O2 -Wall -Werror -Wno-unused-parameter -g0 -Wno-deprecated-declarations -Wno-inconsistent-missing-override -c "$K/NMDRM/nmdrm.cpp"
one "NVPrimarySelector/NVPrimarySelector.cpp" primary-selector primary "${CXX[@]}" -fapple-kext -mkernel -nostdinc -I"$KHDR" -I"$K/NVPrimarySelector" \
  -DKERNEL -DKERNEL_PRIVATE -DDRIVER_PRIVATE -DAPPLE -DNeXT -std=c++17 -fno-rtti -fno-exceptions -fno-builtin -fno-common -fno-stack-protector -O2 -Wall -Wextra -c "$K/NVPrimarySelector/NVPrimarySelector.cpp"
say "not checked here: NMHDA and NMKbdBacklight (they need the Lilu and VirtualSMC SDKs; kexts/NMHDA/build.sh, kexts/NMKbdBacklight/build.sh)"
for wf in "$O"/warn.*; do
  [ -f "$wf" ] || continue; c=${wf##*/warn.}; n=$(awk '{ s += $1 } END { print s + 0 }' "$wf")
  bash "$HERE/build/warn_ceiling.sh" "$c" "$n" >/dev/null || { say "FAIL $c: $n warnings, above its ceiling in build/warnings.txt"; grep -h ': warning:' "$O"/*.err | head -10; rc=1; }
done
say "compiled: $units units"
# ---- imports: one run, every kernel
EXP=()
if [ -n "${NM_XNU_EXPORTS:-}" ]; then
  IFS=: read -r -a dirs <<< "$NM_XNU_EXPORTS"; for d in "${dirs[@]}"; do EXP+=( --exports "$d" ); done
  [ ${#dirs[@]} -ge ${#XNU_TAGS[@]} ] || { say "FAIL NM_XNU_EXPORTS names ${#dirs[@]} kernel(s); the kexts run on ${#XNU_TAGS[@]} (${XNU_TAGS[*]})"; rc=1; }
else
  for tag in "${XNU_TAGS[@]}"; do EXP+=( --xnu-tag "$tag" ); done
  [ "${NM_CHECK_OFFLINE:-0}" = 1 ] && EXP+=( --offline )
fi
python3 -B "$HERE/tools/kext_imports.py" --objects "$O" --ogkm "$T" --repo "$HERE" --nm "$NM" --work "$WORK" "${EXP[@]}" $ATEXIT || rc=1
say "objects: $O"
[ $rc = 0 ] && say "kext check: passed" || say "kext check: FAILED"
exit $rc
