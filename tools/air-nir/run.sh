#!/bin/bash
# usage: tools/air-nir/run.sh [FILE.ll|FILE.bc ...]
#
# Runs Metal AIR through NVK's AIR-to-NIR frontend (nvk/air, docs/AIR-TO-NIR.md) and NAK, on Linux: the NVK built
# for the host on Mesa's nouveau drm-shim as an RTX 5050 (shader model 120), as tools/nak-corpus does. With no
# files: the fixtures here and every kernel among the translator's LLVM fixtures (translator/**/*.ll with
# !air.kernel). Text IR is assembled with llvm-as; bitcode is read with LLVM 18's libLLVM.
#
# Needs: NVK_HOST_W, the host build of this tree's NVK (src/build/src/nouveau/vulkan/libvulkan_nouveau.so under
# it), with nvk/air copied into its src/nouveau/vulkan and nvk/nvk-air-nir.patch applied; llvm-as (18); a C
# compiler with the Vulkan headers and loader. Exit 2 if a pipeline failed, 1 if the run could not start.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd); ROOT=$(cd "$HERE/../.." && pwd)
: "${NVK_HOST_W:?run.sh: set NVK_HOST_W to the host NVK build}"
B=$NVK_HOST_W/src/build
export LD_PRELOAD=$B/src/nouveau/drm-shim/libnouveau_noop_drm_shim.so NOUVEAU_CHIPSET=1b7
export VK_DRIVER_FILES=$B/src/nouveau/vulkan/nouveau_devenv_icd.x86_64.json VK_ICD_FILENAMES=$B/src/nouveau/vulkan/nouveau_devenv_icd.x86_64.json
export MESA_SHADER_CACHE_DISABLE=true
AS=$(command -v llvm-as-18 || command -v llvm-as) || { echo "run.sh: llvm-as not found" >&2; exit 1; }
W=$(mktemp -d); trap 'rm -rf "$W"' EXIT
"${CC:-cc}" -O2 -I"$ROOT/nvk/air" -o "$W/airtest" "$HERE/airtest.c" -lvulkan -ldl || { echo "run.sh: airtest does not build" >&2; exit 1; }
if [ $# -eq 0 ]; then
  set -- "$HERE"/fixtures/*.ll $(grep -rl --include='*.ll' 'air.kernel' "$ROOT/translator" | sort)
fi
files=()
for f in "$@"; do
  case "$f" in
  *.ll) o="$W/$(echo "${f#$ROOT/}" | tr / _).bc"; "$AS" "$f" -o "$o" 2>/dev/null && files+=("$o") || echo "$(basename "$f"): llvm-as refuses it (skipped)" ;;
  *) files+=("$f") ;;
  esac
done
"$W/airtest" "${files[@]}" 2>&1 | grep -v '^nvk: '
