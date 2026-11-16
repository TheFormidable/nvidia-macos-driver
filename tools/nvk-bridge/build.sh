#!/bin/bash
# build.sh <NVK library> <Vulkan include dir> <out dir>: the native Vulkan presentation runtime (README.md), which the
# package installs at /Library/GPUBundles/nvmtl/minecraft. ./build.sh runs it as the nvkbridge component with this
# build's NVK and the Vulkan headers of the Mesa tree NVK was built from.
#   <out>/lib/libnvk_minecraft.dylib       the frontend (bridge.mm): x86_64, macOS 15.0, install name @loader_path/...
#   <out>/lib/libnvk_renderer.dylib        a copy of the NVK library under its own install name, loaded by the frontend
#   <out>/nvk_bridge_icd.json              the Vulkan loader manifest that names the frontend (VK_DRIVER_FILES)
#   <out>/nvk-bridge-runtime.properties    the runtime's identity: the frontend's SHA-256, for the companion mod
# Both libraries are signed ad hoc. Nothing written names a folder of the build Mac.
set -euo pipefail
NVK=${1:?NVK library}; INC=${2:?Vulkan include dir}; OUT=${3:?out dir}
H=$(cd "$(dirname "$0")" && pwd)
stop() { echo "STOP: $*"; exit 1; }
[ -f "$NVK" ] || stop "$NVK does not exist"
CORE=$INC/vulkan/vulkan_core.h
[ -f "$CORE" ] || stop "$INC has no vulkan/vulkan_core.h"
# The manifest's api_version is the one these headers (NVK's) define: 1.4.<VK_HEADER_VERSION>
hv=$(sed -n 's/^#define VK_HEADER_VERSION \([0-9][0-9]*\)$/\1/p' "$CORE")
mm=$(sed -n 's/^#define VK_HEADER_VERSION_COMPLETE VK_MAKE_API_VERSION(0, \([0-9][0-9]*\), \([0-9][0-9]*\), VK_HEADER_VERSION)$/\1.\2/p' "$CORE")
[ -n "$hv" ] && [ -n "$mm" ] || stop "$CORE defines no VK_HEADER_VERSION and VK_HEADER_VERSION_COMPLETE in the form expected"
mkdir -p "$OUT/lib"
F=$OUT/lib/libnvk_minecraft.dylib; R=$OUT/lib/libnvk_renderer.dylib
rm -f "$F" "$R" "$OUT/nvk_bridge_icd.json" "$OUT/nvk-bridge-runtime.properties"
cp "$NVK" "$R"; chmod 755 "$R"
install_name_tool -id @loader_path/libnvk_renderer.dylib "$R"
codesign -f -s - "$R" >/dev/null
xcrun clang++ -arch x86_64 -mmacosx-version-min=15.0 -dynamiclib -std=c++17 -fobjc-arc -O2 -Wall -Wextra \
  -Wno-missing-field-initializers -Werror -Werror=unguarded-availability-new -I"$INC" "$H/bridge.mm" \
  -framework Foundation -framework QuartzCore -framework Metal -framework IOSurface -framework IOKit \
  -Wl,-install_name,@loader_path/libnvk_minecraft.dylib -o "$F"
codesign -f -s - "$F" >/dev/null
for b in "$F" "$R"; do
  codesign --verify --strict "$b" || stop "${b##*/} does not verify"
  lipo "$b" -verify_arch x86_64 || stop "${b##*/} is not x86_64"
done
minos=$(vtool -show-build "$F" | awk '/minos/{print $2; exit}')
[ "$minos" = 15.0 ] || stop "libnvk_minecraft.dylib has minos $minos, not 15.0"
sha=$(shasum -a 256 "$F" | cut -d' ' -f1)
printf '{\n  "file_format_version": "1.0.0",\n  "ICD": {\n    "library_path": "./lib/libnvk_minecraft.dylib",\n    "api_version": "%s.%s"\n  }\n}\n' \
  "$mm" "$hv" > "$OUT/nvk_bridge_icd.json"
printf '%s\n' schema.version=1 target.os=macos target.arch=x86_64 minecraft.version=26.3 \
  "runtime.id=nvk-macos-gpu-copy-$(printf '%s' "$sha" | cut -c1-12)" library.path=lib/libnvk_minecraft.dylib \
  "library.sha256=$sha" presentation=gpu-copy > "$OUT/nvk-bridge-runtime.properties"
echo "nvk bridge: frontend sha $(printf '%s' "$sha" | cut -c1-8) minos $minos, renderer sha $(shasum -a 256 "$R" | cut -c1-8), Vulkan $mm.$hv"
