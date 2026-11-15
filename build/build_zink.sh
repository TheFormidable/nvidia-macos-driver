#!/bin/bash
# SPDX-License-Identifier: MIT
# build_zink.sh <Mesa git> <Mesa commit> <out dir>: native OpenGL through Mesa's Zink on this build's NVK
# (docs/ZINK-OPENGL.md). ./build.sh runs it as the zink component; <Mesa commit> is its pin (build/pins.sh, ZINKMESA),
# which must be the commit build/prepare_zink.py checks the tree against.
#   <out>/zink/libnvk_zink.dylib       Mesa 26.3.0's Zink with zink/mesa-zink-nvk.patch and the nvk-zink frontend
#                                      (zink/nvk_zink.c, nvk_present.c, nvk_iosurface.c); install name @rpath/..., rpath
#                                      @loader_path, where the package puts the private Vulkan runtime beside it
#   <out>/zink/libnvk_cgl.dylib        the CGL adapter (zink/nvk_cgl.c, appkit.m, native_drawable.m)
#   <out>/zink/cgl-dispatch-map.json   what build/generate_cgl_dispatch.py made of this SDK's OpenGL headers
#   <out>/zink/cgl-api-requirements.json, MESA-SOURCE.json   the registry contract and the Mesa tree's identity
#   <out>/zink/zink_sync_probe          Mesa's barrier and queue check (tools/zink_sync_probe.c), run here once
#   <out>/NVKZinkGLDriver.bundle       the GL driver bundle the accelerator names (zink/gld_bridge.c)
# Everything is x86_64, minos 15.0 like the rest of the package's userland (install.sh's runtime check refuses a package
# whose binaries ask for a newer macOS than the Mac runs, and 15.0 to 15.4 install the driver), with any call to an API
# newer than that an error (-Werror=unguarded-availability-new), signed ad hoc. The accelerator selects the bundle on
# macOS 15.5 or later only. The private
# Vulkan runtime (a frontend and a copy of NVK) is not built here: build/package_local.sh copies the nvkbridge
# component's, which is built from this build's NVK, into the same folder.
set -euo pipefail
MESA_GIT=${1:?Mesa git}; COMMIT=${2:?Mesa commit}; OUT=${3:?out dir}
H=$(cd "$(dirname "$0")/.." && pwd)
stop() { echo "STOP: $*"; exit 1; }
MIN=15.0
AV=-Werror=unguarded-availability-new
want=$(cd "$H/build" && python3 -c 'import prepare_zink; print(prepare_zink.MESA_COMMIT)')
[ "$COMMIT" = "$want" ] || stop "the pin says Mesa $COMMIT for Zink, build/prepare_zink.py checks against $want"
git -C "$MESA_GIT" cat-file -e "$COMMIT^{commit}" 2>/dev/null || stop "$MESA_GIT has no commit $COMMIT (./build.sh fetch)"
for t in meson ninja bison flex xcrun codesign; do command -v "$t" >/dev/null || stop "$t not found"; done
mkdir -p "$OUT"; OUT=$(cd "$OUT" && pwd)
Z=$OUT/zink; S=$OUT/mesa; B=$OUT/build
rm -rf "$Z" "$S" "$OUT/NVKZinkGLDriver.bundle" "$OUT/cgl" "$OUT/gld"; mkdir -p "$Z" "$S"

# 1. the pinned Mesa tree and the overlay
git -C "$MESA_GIT" archive "$COMMIT" | tar -x -C "$S"
python3 "$H/build/prepare_zink.py" "$S" --git "$MESA_GIT"

# 2. Zink and its frontend. Gallium's GL front end needs bison and flex: Xcode's, since Mesa's grammars keep to what
# bison 2.3 reads (meson.build). No LLVM, no Vulkan driver, no window system: Zink reaches the GPU through the private
# Vulkan runtime only.
args=(-Dbuildtype=release -Db_ndebug=true "-Dc_args=-mmacosx-version-min=$MIN $AV" "-Dcpp_args=-mmacosx-version-min=$MIN $AV"
      -Dc_link_args=-mmacosx-version-min=$MIN -Dcpp_link_args=-mmacosx-version-min=$MIN
      -Dgallium-drivers=zink -Dvulkan-drivers= -Dplatforms= -Degl=disabled -Dglx=disabled -Dgbm=disabled -Dglvnd=disabled
      -Dllvm=disabled -Dshared-glapi=disabled -Dvulkan-layers= -Dtools= -Dbuild-tests=false -Dexpat=disabled
      -Dzstd=disabled -Dlibunwind=disabled -Dlmsensors=disabled -Dvalgrind=disabled -Dxlib-lease=disabled
      -Dgallium-rusticl=false -Dspirv-tools=disabled -Dmoltenvk-dir=)
rm -rf "$B"
meson setup "$B" "$S" "${args[@]}"
ninja -C "$B" -j "${JOBS:-$(sysctl -n hw.ncpu)}" src/gallium/targets/nvk-zink/libnvk_zink.dylib src/gallium/targets/nvk-zink/zink_sync_probe
T=$S/src/gallium/targets/nvk-zink
cp "$B/src/gallium/targets/nvk-zink/libnvk_zink.dylib" "$B/src/gallium/targets/nvk-zink/zink_sync_probe" "$Z/"
cp "$T/cgl-api-requirements.json" "$T/MESA-SOURCE.json" "$Z/"
install_name_tool -id @rpath/libnvk_zink.dylib "$Z/libnvk_zink.dylib"
"$Z/zink_sync_probe" || stop "Mesa's barrier and queue check failed on this Mac"

# 3. the CGL adapter, its dispatch table generated from this SDK's public OpenGL headers
SDK=$(xcrun --show-sdk-path)
GLH=$SDK/System/Library/Frameworks/OpenGL.framework/Headers
C=$OUT/cgl; mkdir -p "$C"
python3 "$H/build/generate_cgl_dispatch.py" "$GLH" "$Z/cgl-api-requirements.json" "$C"
cp "$C/cgl-dispatch-map.json" "$Z/"
CF=(-arch x86_64 -mmacosx-version-min=$MIN "$AV" -O2 -Wall -Wextra -Werror -Wno-deprecated-declarations -fvisibility=hidden)
for m in appkit native_drawable; do
  xcrun clang "${CF[@]}" -fobjc-arc -fblocks -c "$H/zink/$m.m" -I"$H/zink" -o "$C/$m.o"
done
xcrun clang "${CF[@]}" -std=c11 -I"$H/zink" -I"$C" "$H/zink/nvk_cgl.c" "$C/appkit.o" "$C/native_drawable.o" \
  -dynamiclib -L"$Z" -lnvk_zink -framework Cocoa -framework OpenGL -framework IOSurface -framework CoreFoundation \
  -framework QuartzCore -framework Metal -framework IOKit -Wl,-rpath,@loader_path \
  -install_name @rpath/libnvk_cgl.dylib -o "$Z/libnvk_cgl.dylib"

# 4. the GL driver bundle: every GLD export of the build Mac's AppleMetalOpenGLRenderer forwarded by name
G=$OUT/gld; mkdir -p "$G"
python3 "$H/build/generate_gld_forward.py" "$G"
BUNDLE=$OUT/NVKZinkGLDriver.bundle
mkdir -p "$BUNDLE/Contents/MacOS"
xcrun clang -arch x86_64 -mmacosx-version-min=$MIN "$AV" -bundle -std=c11 -O2 -Wall -Wextra -Werror -fvisibility=hidden \
  -I"$H/zink" -I"$G" "$H/zink/gld_bridge.c" "$G/gld_forward.S" \
  -Wl,-rpath,@loader_path/../../../nvmtl/zink -o "$BUNDLE/Contents/MacOS/NVKZinkGLDriver"
cat > "$BUNDLE/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>NVKZinkGLDriver</string>
<key>CFBundleIdentifier</key><string>com.nullmoth.NVKZinkGLDriver</string>
<key>CFBundleName</key><string>NVKZinkGLDriver</string>
<key>CFBundlePackageType</key><string>BNDL</string>
<key>CFBundleVersion</key><string>1</string>
</dict></plist>
PLIST

# 5. signatures and the checks a package makes
for f in "$Z/libnvk_zink.dylib" "$Z/libnvk_cgl.dylib" "$Z/zink_sync_probe"; do codesign -f -s - "$f" >/dev/null; done
codesign -f -s - "$BUNDLE" >/dev/null
for f in "$Z/libnvk_zink.dylib" "$Z/libnvk_cgl.dylib" "$BUNDLE/Contents/MacOS/NVKZinkGLDriver"; do
  lipo "$f" -verify_arch x86_64 || stop "${f##*/} is not x86_64"
  codesign --verify --strict "$f" || stop "${f##*/} does not verify"
  # nothing from outside macOS and the runtime folder: a Homebrew library would follow the package to the user's Mac
  bad=$(otool -L "$f" | awk 'NR>1 {print $1}' | grep -vE '^(/usr/lib/|/System/Library/|@rpath/)' || true)
  [ -z "$bad" ] || stop "${f##*/} loads $bad"
done
codesign --verify --strict "$BUNDLE" || stop "NVKZinkGLDriver.bundle does not verify"
echo "zink: libnvk_zink $(shasum -a 256 "$Z/libnvk_zink.dylib" | cut -c1-8), libnvk_cgl $(shasum -a 256 "$Z/libnvk_cgl.dylib" | cut -c1-8), GL bundle $(shasum -a 256 "$BUNDLE/Contents/MacOS/NVKZinkGLDriver" | cut -c1-8) (Mesa ${COMMIT:0:8}, macOS $MIN)"
