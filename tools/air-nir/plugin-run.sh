#!/bin/bash
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# usage: tools/air-nir/plugin-run.sh NVMTLDriver NVK.dylib OUTDIR [TEST...]
#
# Runs the kernels of tools/air-nir/metal through the Metal plugin's own compute path, and the vertex and fragment pairs
# of tools/air-nir/metal-render through its render path (plugin_run.m, render_tests.h), without installing anything
# (TEST names one; "compute" or "render" all of one kind): NVMTLDriver is a development build of the plugin (build/build_plugin_dev.sh), NVK.dylib a build of NVK
# (build_nvk.sh's out dir). OUTDIR gets a bundle folder for the plugin (OUTDIR/bundle/NVMTLDriver.bundle: the plugin
# copied into Contents/MacOS, and links to the installed bundle's Info.plist, Resources, air-runtime, translator and AIR
# decoder, which are only read), the kernels as .air and .metallib (Apple's Metal compiler, as run-mac.sh compiles
# them), the harness, and what one run writes (driver.log, results.tsv, the AIR and shader caches, the plugin's
# per-dispatch GPU times, render.tsv). OUTDIR must not exist yet: every run starts with empty caches.
#
# NVMTL_AIR_NIR in the environment picks the road (unset: AIR to NIR first; 0: the SPIR-V road only);
# PLUGIN_RUN_CORRUPT=1 checks the checks. PLUGIN_RUN_VULKAN_INCLUDE: the Vulkan headers (default: the Mesa tree next
# to NVK.dylib). PLUGIN_RUN_INSTALLED: the installed bundle (default /Library/GPUBundles/NVMTLDriver.bundle). Exit as
# plugin_run's (3: GPU work stopped).
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd); ROOT=$(cd "$HERE/../.." && pwd)
PLUGIN=${1:?usage: plugin-run.sh NVMTLDriver NVK.dylib OUTDIR [TEST...]}; NVK=${2:?}; OUT=${3:?}; shift 3
INST=${PLUGIN_RUN_INSTALLED:-/Library/GPUBundles/NVMTLDriver.bundle}
[ -e "$OUT" ] && { echo "plugin-run.sh: $OUT exists; each run needs a new folder" >&2; exit 2; }
VKINC=${PLUGIN_RUN_VULKAN_INCLUDE:-$(cd "$(dirname "$NVK")" && pwd)/mesa/include}
[ -f "$VKINC/vulkan/vulkan_core.h" ] || { echo "plugin-run.sh: no Vulkan headers in $VKINC" >&2; exit 2; }
[ -d "$INST/Contents/MacOS/air-runtime" ] || { echo "plugin-run.sh: no air-runtime in $INST" >&2; exit 2; }
B=$OUT/bundle/NVMTLDriver.bundle/Contents
mkdir -p "$B/MacOS" "$OUT/air" || exit 2
cp "$PLUGIN" "$B/MacOS/NVMTLDriver" || exit 2
ln -s "$INST/Contents/Info.plist" "$B/Info.plist"
ln -s "$INST/Contents/Resources" "$B/Resources"
for f in "$INST"/Contents/MacOS/*; do
  n=$(basename "$f"); [ "$n" = NVMTLDriver ] || ln -s "$f" "$B/MacOS/$n"
done
for f in "$HERE"/metal/*.metal "$HERE"/metal-render/*.metal; do
  n=$(basename "$f" .metal); flags=
  [ "$n" = fmath_precise ] && flags=-fno-fast-math
  xcrun -sdk macosx metal $flags -c -o "$OUT/air/$n.air" "$f" || { echo "plugin-run.sh: $n.metal does not compile" >&2; exit 2; }
  xcrun -sdk macosx metallib -o "$OUT/air/$n.metallib" "$OUT/air/$n.air" || { echo "plugin-run.sh: $n.metallib not made" >&2; exit 2; }
done
clang -O1 -fno-objc-arc -Wall -Wextra -Wno-unused-parameter -Wno-unused-function -Wno-missing-field-initializers \
  -I"$ROOT/nvk/air" -I"$VKINC" -framework Foundation -framework Metal -framework IOKit \
  -o "$OUT/plugin_run" "$HERE/plugin_run.m" || { echo "plugin-run.sh: plugin_run does not build" >&2; exit 2; }
# every pipeline is compiled (plugin_run turns Mesa's disk cache off), and the caches are OUTDIR's
"$OUT/plugin_run" "$B/MacOS/NVMTLDriver" "$NVK" "$OUT/air" "$OUT" "$@"
