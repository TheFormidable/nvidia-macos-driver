#!/bin/bash
# build_vulkan_loader.sh <out dir>: the Khronos Vulkan loader (libvulkan.1.dylib), x86_64, macOS 15 deployment target, built
# from source at a pinned release. Apache-2.0 (licenses/Vulkan-Loader-LICENSE.txt); the tag and commit are recorded in NOTICE.
# The commit is checked after the clone, so a moved tag stops the build instead of changing what ships.
#
# Needs: git, cmake, ninja, Xcode command line tools, network access to github.com.
# NM_KHRONOS_URL replaces https://github.com/KhronosGroup (for example a folder of local clones that hold the tag).
set -euo pipefail
OUT=${1:?output directory}
TAG=v1.4.367
LOADER_SHA=aa6dee8385afe4b2cc0152d086af2184743e16fd
HEADERS_SHA=9fbc2a782ee3f8a4db64e7d991e23da75e893f44
W=$(mktemp -d); trap 'rm -rf "$W"' EXIT
clone() { git clone -q --depth 1 --branch "$TAG" "${NM_KHRONOS_URL:-https://github.com/KhronosGroup}/$1" "$W/$1"
          [ "$(git -C "$W/$1" rev-parse HEAD)" = "$2" ] || { echo "STOP: $1 $TAG is not commit $2"; exit 2; }; }
clone Vulkan-Headers "$HEADERS_SHA"
clone Vulkan-Loader "$LOADER_SHA"
ARCH="-DCMAKE_OSX_ARCHITECTURES=x86_64 -DCMAKE_OSX_DEPLOYMENT_TARGET=15.0 -DCMAKE_BUILD_TYPE=Release"
cmake -S "$W/Vulkan-Headers" -B "$W/hb" -G Ninja $ARCH -DCMAKE_INSTALL_PREFIX="$W/headers" >/dev/null
cmake --install "$W/hb" >/dev/null
# SYSCONFDIR=/etc: left to itself the loader's CMake compiles "<install prefix>/etc" into the library as a folder where
# it looks for manifests and settings, ahead of /etc. The prefix is this build's temporary folder: the library would
# carry that folder's name, look there on every Mac it runs on, and differ from one build to the next.
cmake -S "$W/Vulkan-Loader" -B "$W/lb" -G Ninja $ARCH -DUPDATE_DEPS=OFF -DBUILD_TESTS=OFF -DSYSCONFDIR=/etc \
  -DVULKAN_HEADERS_INSTALL_DIR="$W/headers" -DCMAKE_INSTALL_PREFIX="$W/loader" >/dev/null
cmake --build "$W/lb" >/dev/null
cmake --install "$W/lb" >/dev/null
mkdir -p "$OUT"
L=$(ls "$W"/loader/lib/libvulkan.1.*.*.dylib 2>/dev/null | head -1)
[ -n "$L" ] || { echo "STOP: the loader library was not produced"; exit 3; }
# Before the library is copied out: it does not name this build's temporary folder, under any spelling of the path
# (the folder's own name is looked for). strings reads the whole file on its standard input: given a Mach-O file by
# name, macOS's strings reads its sections only. Its output is kept apart from grep's count, which is 0 also when
# strings itself failed.
sl=$(strings -a < "$L") || { echo "STOP: strings could not read the loader"; exit 3; }
left=$(printf '%s\n' "$sl" | grep -cF "${W##*/}" || true)
[ "$left" = 0 ] || { echo "STOP: the loader names this build's temporary folder ($left place(s))"; printf '%s\n' "$sl" | grep -F "${W##*/}" | sed -n 1,3p; exit 3; }
cp "$L" "$OUT/libvulkan.1.dylib"
cp "$W/Vulkan-Loader/LICENSE.txt" "$OUT/Vulkan-Loader-LICENSE.txt"
lipo -archs "$OUT/libvulkan.1.dylib" | grep -qx x86_64 || { echo "STOP: not x86_64"; exit 3; }
vtool -show-build "$OUT/libvulkan.1.dylib" | awk '/minos/{print "minos " $2; exit}'
echo "Vulkan-Loader $TAG $LOADER_SHA sha256 $(shasum -a 256 "$OUT/libvulkan.1.dylib" | cut -c1-64)"
