#!/bin/bash
# ci_plugin.sh [out dir]: the release compile of the Metal plugin on the Mac this runs on, with the compiler line of
# build_plugin_release.sh (which does the same on the build Mac over ssh; tools/test_ci_workflow.py holds the two lines
# together). For continuous integration and for anybody who wants to know that plugin/ still compiles before a full
# build. It needs:
#   - the macOS 26 SDK (Xcode 26): plugin/NVMTLMetal4.m imports <Metal/MTL4CommandQueue.h>;
#   - the Vulkan headers: NM_VULKAN_INCLUDE=<dir holding vulkan/vulkan.h>, or they are downloaded at the commit
#     build_vulkan_loader.sh pins (KhronosGroup/Vulkan-Headers, Apache-2.0). Nothing downloaded ships.
# It does not build the translator or NVK, so the result is a compile and link check, not a driver to install.
set -uo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd)
[ "$(uname -s)" = Darwin ] || { echo "STOP: the plugin is Objective-C against Apple's frameworks; this needs a Mac"; exit 2; }
OUT=${1:-$HERE/build-out/ci-plugin}; mkdir -p "$OUT"; OUT=$(cd "$OUT" && pwd)
SDK=$(xcrun --show-sdk-path) || exit 2
[ -f "$SDK/System/Library/Frameworks/Metal.framework/Headers/MTL4CommandQueue.h" ] \
  || { echo "STOP: $SDK has no Metal 4 headers; select Xcode 26 (the plugin is built against the macOS 26 SDK for macOS 15)"; exit 3; }
INC=${NM_VULKAN_INCLUDE:-}
if [ -z "$INC" ]; then
  SHA=$(sed -n 's/^HEADERS_SHA=\([0-9a-f]\{40\}\).*/\1/p' "$HERE/build/build_vulkan_loader.sh")
  [ -n "$SHA" ] || { echo "STOP: build_vulkan_loader.sh names no HEADERS_SHA"; exit 2; }
  D=$OUT/Vulkan-Headers
  if [ ! -f "$D/include/vulkan/vulkan.h" ]; then
    rm -rf "$D"; git init -q "$D" && git -C "$D" remote add origin https://github.com/KhronosGroup/Vulkan-Headers \
      && git -C "$D" fetch -q --depth 1 origin "$SHA" && git -C "$D" checkout -q FETCH_HEAD || { echo "STOP: cannot fetch Vulkan-Headers $SHA"; exit 2; }
    [ "$(git -C "$D" rev-parse HEAD)" = "$SHA" ] || { echo "STOP: Vulkan-Headers is not at $SHA"; exit 2; }
  fi
  INC=$D/include
fi
[ -f "$INC/vulkan/vulkan.h" ] || { echo "STOP: no vulkan/vulkan.h under $INC"; exit 2; }
cd "$HERE/plugin" || exit 2
xcrun clang -arch x86_64 -mmacosx-version-min=15.0 -bundle -fobjc-arc -O1 -DNVMTL_RELEASE=1 \
  -Wno-protocol -Wno-incomplete-implementation -Werror=unguarded-availability-new \
  -I"$INC" -o "$OUT/NVMTLDriver" NVMTLDevice.m nvmtl_dlss.c dlss/elfhost.c dlss/lnxlibc.c \
  -framework Foundation -framework Metal -framework IOSurface -framework IOKit -framework CoreFoundation -framework MetalFX -liconv 2>"$OUT/build.err" \
  || { echo "STOP: the plugin does not compile"; grep -A2 ' error:' "$OUT/build.err" | head -30; exit 1; }
w=$(grep -c ': warning:' "$OUT/build.err" || true)
bash "$HERE/build/warn_ceiling.sh" plugin "$w" "$OUT/build.err" || exit 4
minos=$(vtool -show-build "$OUT/NVMTLDriver" | awk '/minos/{print $2; exit}')
[ "$minos" = 15.0 ] || { echo "STOP: minos $minos, not 15.0"; exit 1; }
echo "NVMTLDriver compiled: minos $minos, warnings $w ($OUT/NVMTLDriver; a compile check, not an installable build)"
