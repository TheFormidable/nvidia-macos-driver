#!/bin/bash
# usage: tools/air-nir/run-mac.sh NVK.dylib [TEST...]
#
# Runs the Metal kernels in tools/air-nir/metal on the card through NVK's AIR-to-NIR frontend (docs/AIR-TO-NIR.md),
# on macOS: compiles each with Apple's Metal compiler (xcrun -sdk macosx metal -c; fmath_precise with
# -fno-fast-math), builds airrun.c, and runs it on the NVK library named (a build of this tree, opened directly, not
# the installed one). airrun checks every result against a reference computed on the CPU and stops all GPU work if
# one of NVRM's fault counters rises.
#
# AIRRUN_LLVM names the LLVM library (default: the installed plugin's air-runtime/libLLVM.dylib, read only);
# AIRRUN_VULKAN_INCLUDE the Vulkan headers (default: the Mesa tree next to NVK.dylib in build_nvk.sh's out dir);
# AIRRUN_WORK the folder for the .air files and the harness (default: a temporary folder). Exit as airrun's.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd); ROOT=$(cd "$HERE/../.." && pwd)
NVK=${1:?usage: run-mac.sh NVK.dylib [TEST...]}; shift
LLVM=${AIRRUN_LLVM:-/Library/GPUBundles/NVMTLDriver.bundle/Contents/MacOS/air-runtime/libLLVM.dylib}
W=${AIRRUN_WORK:-}
if [ -z "$W" ]; then W=$(mktemp -d); trap 'rm -rf "$W"' EXIT; fi
mkdir -p "$W/air"
VKINC=${AIRRUN_VULKAN_INCLUDE:-$(cd "$(dirname "$NVK")" && pwd)/mesa/include}   # build_nvk.sh's out dir keeps its Mesa tree
[ -f "$VKINC/vulkan/vulkan_core.h" ] || { echo "run-mac.sh: no Vulkan headers in $VKINC (set AIRRUN_VULKAN_INCLUDE)" >&2; exit 2; }
for f in "$HERE"/metal/*.metal; do
  n=$(basename "$f" .metal); flags=
  [ "$n" = fmath_precise ] && flags=-fno-fast-math
  xcrun -sdk macosx metal $flags -c -o "$W/air/$n.air" "$f" || { echo "run-mac.sh: $n.metal does not compile" >&2; exit 2; }
done
clang -O2 -Wall -Wextra -Wno-unused-parameter -Wno-missing-field-initializers -I"$ROOT/nvk/air" -I"$VKINC" \
  -o "$W/airrun" "$HERE/airrun.c" || { echo "run-mac.sh: airrun does not build" >&2; exit 2; }
# every pipeline is compiled, and nothing is added to Mesa's shader cache in the home folder
MESA_SHADER_CACHE_DISABLE=true "$W/airrun" "$NVK" "$LLVM" "$W/air" "$@"
