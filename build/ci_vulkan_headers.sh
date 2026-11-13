#!/bin/bash
# ci_vulkan_headers.sh [work dir]: prints the folder that holds the Vulkan headers the release compiles the plugin
# against, for the automatic checks (build/ci_plugin.sh and the host tests). Everything else goes to stderr, so a
# caller takes the folder with INC=$(bash build/ci_vulkan_headers.sh).
#
# The release compiles the plugin against the include/ of the pinned Mesa commit (build/pins.sh: NM_PIN_MESA_COMMIT;
# NVK's patches do not touch include/). A runner gets the same bytes from Khronos' Vulkan-Headers at the commit
# NM_PIN_CI_VKHEADERS_COMMIT, from github.com, where the runners are: every C header of that commit's include/vulkan
# and include/vk_video is byte for byte the file of the same name in Mesa's include/ at NM_PIN_CI_VKHEADERS_MESA, and
# Mesa has two files more there that the plugin cannot use (vk_android_native_buffer.h, .editorconfig). A Mesa pin
# that is not NM_PIN_CI_VKHEADERS_MESA stops this script: compare the new Mesa's headers first (--compare below) and
# move both pins together. Only include/vulkan and include/vk_video are fetched (shallow, blobs of those two folders
# only), and the commit is checked after the fetch.
#
#   NM_VULKAN_INCLUDE=<dir>         use that folder (it must hold vulkan/vulkan.h); nothing is fetched
#   --compare <Mesa include dir>    after the fetch, compare the two sets file by file; exit 1 on any difference
# Nothing fetched ships or is copied into the repository (the default work folder is build-out/, which git ignores).
set -uo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd)
say() { printf '%s\n' "$*" >&2; }
stop() { say "STOP: $*"; exit 2; }
CMP=
if [ "${1:-}" = --compare ]; then CMP=${2:-}; [ -n "$CMP" ] || stop "--compare needs Mesa's include folder"; shift 2; fi
pinv() { sed -n "s/^$1=\([0-9a-f]\{40\}\)\$/\1/p" "$HERE/build/pins.sh"; }
SHA=$(pinv NM_PIN_CI_VKHEADERS_COMMIT); FOR=$(pinv NM_PIN_CI_VKHEADERS_MESA); MESA=$(pinv NM_PIN_MESA_COMMIT)
[ -n "$SHA" ] && [ -n "$FOR" ] && [ -n "$MESA" ] \
  || stop "build/pins.sh does not name NM_PIN_CI_VKHEADERS_COMMIT, NM_PIN_CI_VKHEADERS_MESA and NM_PIN_MESA_COMMIT as commits"
[ "$FOR" = "$MESA" ] || stop "the Vulkan-Headers commit $SHA was compared with Mesa $FOR, and the release now uses Mesa $MESA: run
  $0 --compare <that Mesa's include folder> and set NM_PIN_CI_VKHEADERS_* in build/pins.sh again"

if [ -n "${NM_VULKAN_INCLUDE:-}" ] && [ -z "$CMP" ]; then
  [ -f "$NM_VULKAN_INCLUDE/vulkan/vulkan.h" ] || stop "NM_VULKAN_INCLUDE=$NM_VULKAN_INCLUDE holds no vulkan/vulkan.h"
  printf '%s\n' "$NM_VULKAN_INCLUDE"; exit 0
fi

WORK=${1:-$HERE/build-out/ci-vulkan-headers}; mkdir -p "$WORK" || stop "cannot make $WORK"; WORK=$(cd "$WORK" && pwd)
D=$WORK/$SHA
if [ ! -f "$D/include/vulkan/vulkan.h" ] || [ "$(git -C "$D" rev-parse HEAD 2>/dev/null)" != "$SHA" ]; then
  # a folder the cache restored that is not this commit, or a fetch cut off half-way, is fetched again
  rm -rf "$D" "$D.part"
  git init -q "$D.part" && git -C "$D.part" remote add origin https://github.com/KhronosGroup/Vulkan-Headers \
    && git -C "$D.part" sparse-checkout set include/vulkan include/vk_video \
    && git -C "$D.part" fetch -q --depth 1 --filter=blob:none origin "$SHA" \
    && git -C "$D.part" checkout -q FETCH_HEAD || stop "cannot fetch Vulkan-Headers $SHA from github.com"
  [ "$(git -C "$D.part" rev-parse HEAD)" = "$SHA" ] || stop "the fetched Vulkan-Headers is not commit $SHA"
  mv "$D.part" "$D" || stop "cannot move the fetched headers to $D"
fi
[ -f "$D/include/vulkan/vulkan.h" ] || stop "$D has no include/vulkan/vulkan.h"

if [ -n "$CMP" ]; then
  [ -f "$CMP/vulkan/vulkan.h" ] || stop "$CMP holds no vulkan/vulkan.h"
  bad=0; n=0
  for f in "$CMP"/vulkan/* "$CMP"/vulkan/.editorconfig "$CMP"/vk_video/*; do
    [ -f "$f" ] || continue; r=${f#"$CMP"/}
    case "$r" in vulkan/vk_android_native_buffer.h|vulkan/.editorconfig) say "  only in Mesa (not used by the plugin): $r"; continue ;; esac
    n=$((n + 1))
    if [ ! -f "$D/include/$r" ]; then say "  DIFF $r: not in Vulkan-Headers $SHA"; bad=1
    elif ! cmp -s "$f" "$D/include/$r"; then say "  DIFF $r: the files differ"; bad=1; fi
  done
  for f in "$D"/include/vulkan/*.h "$D"/include/vk_video/*.h; do
    r=${f#"$D"/include/}
    [ -f "$CMP/$r" ] || { say "  DIFF $r: a C header Mesa does not have"; bad=1; }
  done
  [ $bad = 0 ] || { say "Vulkan-Headers $SHA is NOT the release's headers"; exit 1; }
  say "the $n headers of $CMP are byte for byte those of Vulkan-Headers $SHA"
fi
printf '%s\n' "$D/include"
