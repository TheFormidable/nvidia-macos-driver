#!/bin/bash
# pick_kit.sh <macOS product version> <package folder>: which part of this package installs on that macOS.
# Prints one line and exits 0:
#   modern             macOS 15 or 26: install.sh's own path
#   legacy <release>   10.14 to 14: the kit at <package folder>/legacy/<release>/, which holds install-legacy.sh
#   none <reason>      anything else (10.13 and older are not supported), or the package lacks the kit for this release
# install.sh runs it before anything else, on every release it may meet, so it is plain bash 3.2 and reads only its
# two arguments and the package folder. It changes nothing.
v=${1:-}; P=${2:-}
case "$v" in ''|*[!0-9.]*|.*) echo "none cannot read the macOS version (\"$v\")"; exit 0;; esac
maj=${v%%.*}; rest=""; case "$v" in *.*) rest=${v#*.};; esac; min=${rest%%.*}
rel=""
case "$maj" in
  15|26) echo modern; exit 0;;
  11|12|13|14) rel=$maj;;
  10) case "$min" in
        14|15) rel=10.$min;;
        '') echo "none cannot read the macOS version (\"$v\")"; exit 0;;
        *) echo "none macOS $v is older than the oldest release this driver supports (10.14)"; exit 0;;
      esac;;
  *) if [ "$maj" -lt 10 ] 2>/dev/null; then echo "none macOS $v is older than the oldest release this driver supports (10.14)"
     else echo "none macOS $v is newer than this driver knows (it supports 10.14 to 14, 15 and 26)"; fi
     exit 0;;
esac
K=$P/legacy/$rel
if [ -z "$P" ] || [ ! -f "$K/install-legacy.sh" ] || [ ! -f "$K/RELEASE" ]; then
  echo "none this package carries no kit for macOS $rel (it was built without the kits for older releases)"; exit 0
fi
[ "$(tr -cd '0-9.' < "$K/RELEASE")" = "$rel" ] || { echo "none the kit in legacy/$rel is not the one for macOS $rel"; exit 0; }
echo "legacy $rel"
