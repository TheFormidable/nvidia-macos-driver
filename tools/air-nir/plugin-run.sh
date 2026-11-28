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
  [ "$n" = r_handlechoice ] && flags=-O0   # the choice of handles stays a switch on a literal, as Apple's GL leaves it
  xcrun -sdk macosx metal $flags -c -o "$OUT/air/$n.air" "$f" || { echo "plugin-run.sh: $n.metal does not compile" >&2; exit 2; }
  xcrun -sdk macosx metallib -o "$OUT/air/$n.metallib" "$OUT/air/$n.air" || { echo "plugin-run.sh: $n.metallib not made" >&2; exit 2; }
done
# r_nospace: r_buffers with air.address_space taken out of every buffer's metadata, as the GL-to-Metal compiler emits
# its buffers (the pointer type says the address space)
xcrun -sdk macosx metal -S -emit-llvm -o "$OUT/air/r_nospace.ll" "$HERE/metal-render/r_buffers.metal" &&
  sed -E -i '' '/air\.buffer/s/!"air\.address_space", i32 [0-9]+, //' "$OUT/air/r_nospace.ll" &&
  ! grep -q 'air\.address_space' "$OUT/air/r_nospace.ll" &&
  xcrun -sdk macosx metal -c -o "$OUT/air/r_nospace.air" "$OUT/air/r_nospace.ll" &&
  xcrun -sdk macosx metallib -o "$OUT/air/r_nospace.metallib" "$OUT/air/r_nospace.air" ||
  { echo "plugin-run.sh: r_nospace not made" >&2; exit 2; }
# r_oob_nospace: r_oob so made (the bounds-checked descriptor with the address space taken from the pointer type)
xcrun -sdk macosx metal -S -emit-llvm -o "$OUT/air/r_oob_nospace.ll" "$HERE/metal-render/r_oob.metal" &&
  sed -E -i '' '/air\.buffer/s/!"air\.address_space", i32 [0-9]+, //' "$OUT/air/r_oob_nospace.ll" &&
  ! grep -q 'air\.address_space' "$OUT/air/r_oob_nospace.ll" &&
  xcrun -sdk macosx metal -c -o "$OUT/air/r_oob_nospace.air" "$OUT/air/r_oob_nospace.ll" &&
  xcrun -sdk macosx metallib -o "$OUT/air/r_oob_nospace.metallib" "$OUT/air/r_oob_nospace.air" ||
  { echo "plugin-run.sh: r_oob_nospace not made" >&2; exit 2; }
# tex_pixwrap: its pixel-coordinate samplers' state words rewritten, clamp_to_edge to repeat and clamp_to_zero to
# mirrored_repeat (Metal's compiler allows neither with coord::pixel; the translator emulates both)
xcrun -sdk macosx metal -S -emit-llvm -o "$OUT/air/tex_pixwrap.ll" "$HERE/metal/tex_pixwrap.metal" &&
  perl -i -pe 's/(\@__air_sampler_state\S* = internal addrspace\(2\) constant \[2 x i64\] \[i64 )(\d+)/$1 . ((($2 & 0x1ff) == 0x49) ? (($2 & ~0x1ff) | 0x92) : (($2 & 0x1ff) == 0) ? (($2 & ~0x1ff) | 0xdb) : $2)/e' "$OUT/air/tex_pixwrap.ll" &&
  [ "$(perl -ne 'print "$1\n" if /\@__air_sampler_state\S* = internal addrspace\(2\) constant \[2 x i64\] \[i64 (\d+)/' "$OUT/air/tex_pixwrap.ll" |
       perl -ne '$n++ if (($_ & 0x1ff) == 0x92 || ($_ & 0x1ff) == 0xdb); END { print $n + 0 }')" = 4 ] &&
  xcrun -sdk macosx metal -c -o "$OUT/air/tex_pixwrap.air" "$OUT/air/tex_pixwrap.ll" &&
  xcrun -sdk macosx metallib -o "$OUT/air/tex_pixwrap.metallib" "$OUT/air/tex_pixwrap.air" ||
  { echo "plugin-run.sh: tex_pixwrap not made" >&2; exit 2; }
# PLUGIN_RUN_EXTRA_AIR: a folder of more .metallib files for the tests that name them (r_qcblit: qc_blit.metallib, made
# from QuartzCore's own functions, which are not in this tree)
if [ -n "${PLUGIN_RUN_EXTRA_AIR:-}" ]; then cp "$PLUGIN_RUN_EXTRA_AIR"/*.metallib "$OUT/air/" || exit 2; fi
clang -O1 -fno-objc-arc -Wall -Wextra -Wno-unused-parameter -Wno-unused-function -Wno-missing-field-initializers \
  -I"$ROOT/nvk/air" -I"$VKINC" -framework Foundation -framework Metal -framework IOKit \
  -o "$OUT/plugin_run" "$HERE/plugin_run.m" || { echo "plugin-run.sh: plugin_run does not build" >&2; exit 2; }
# every pipeline is compiled (plugin_run turns Mesa's disk cache off), and the caches are OUTDIR's
"$OUT/plugin_run" "$B/MacOS/NVMTLDriver" "$NVK" "$OUT/air" "$OUT" "$@"
