#!/bin/bash
# build_plugin_dev.sh <out-dir>: the Metal plugin (NVMTLDriver) with its diagnostics compiled in, built on this Mac from
# the working tree's plugin/ (uncommitted edits included). It is build_plugin_release.sh's compile line without
# -DNVMTL_RELEASE=1, so everything else is what a release gets: macOS 15.0 deployment target, IOKit and CoreFoundation
# linked, an API newer than macOS 15 without an availability check is an error, the plugin's warning ceiling, and the
# Vulkan headers of the Mesa clone the release uses. A plugin built here logs and honours the diagnostic switches that
# a release ignores; it is for finding a fault, not for handing to others.
#   NULLMOTH_BUILD_ROOT  the build folder (./build.sh sets it); the source copy goes to <root>/build/plugin-dev
#   NM_VULKAN_INCLUDE    Vulkan headers (default: <root>/build/gpu-src/mesa/include, as build_plugin_release.sh)
# The two diagnostic markers that build_plugin.sh requires only in a non-release build must be in the result.
set -euo pipefail
OUT=${1:?output directory}
HERE=$(cd "$(dirname "$0")/.." && pwd)
BASE=${NULLMOTH_BUILD_ROOT:?set NULLMOTH_BUILD_ROOT to the build folder}
INC=${NM_VULKAN_INCLUDE:-$BASE/build/gpu-src/mesa/include}
[ -f "$INC/vulkan/vulkan_core.h" ] || { echo "STOP: no Vulkan headers at $INC (vulkan/vulkan_core.h)"; exit 2; }
R=$BASE/build/plugin-dev
rm -rf "$R/src" "$R/out"; mkdir -p "$R/src" "$R/out"
cp -pR "$HERE/plugin/". "$R/src/"
( cd "$R/src" && xcrun clang -arch x86_64 -mmacosx-version-min=15.0 -bundle -fobjc-arc -O1 \
  -Wno-protocol -Wno-incomplete-implementation -Werror=unguarded-availability-new \
  -I"$INC" -o ../out/NVMTLDriver NVMTLDevice.m nvmtl_dlss.c dlss/elfhost.c dlss/lnxlibc.c \
  -framework Foundation -framework Metal -framework IOSurface -framework IOKit -framework CoreFoundation -framework MetalFX -liconv 2>../out/build.err ) \
  || { grep ' error:' "$R/out/build.err" | head -20; exit 1; }
mkdir -p "$OUT"
cp -p "$R/out/NVMTLDriver" "$R/out/build.err" "$OUT/"
bash "$HERE/build/warn_ceiling.sh" plugin "$(grep -c ': warning:' "$OUT/build.err" || true)" "$OUT/build.err" || exit 4
minos=$(vtool -show-build "$OUT/NVMTLDriver" | awk '/minos/{print $2; exit}')
[ "$minos" = 15.0 ] || { echo "STOP: minos $minos, not 15.0"; exit 1; }
for m in nvmtl-fps- nvmtl-passtime-request; do
  n=$(strings -a "$OUT/NVMTLDriver" | grep -c -- "$m" || true)   # grep -q would stop strings early: pipefail
  [ "${n:-0}" -ge 1 ] || { echo "STOP: the diagnostic marker $m is missing: this is not a development build"; exit 6; }
done
echo "NVMTLDriver (development, diagnostics on) sha $(shasum -a 256 "$OUT/NVMTLDriver" | cut -c1-8) minos $minos warnings $(grep -c ': warning:' "$OUT/build.err" || true)"
