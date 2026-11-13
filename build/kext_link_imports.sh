#!/bin/bash
# kext_link_imports.sh <imports> <kext>: stop when a linked kext imports a name that no macOS kernel and no Apple family
# defines. <imports> is the `nm -u` list of the linked binary, a file or - for standard input; <kext> names the kext in
# the message. A kext is linked with undefined names allowed, so such a kext links, and then the kernel's linker refuses
# to load it, and the installer's kernel collection step refuses the package.
#
# The names here are the helpers of compiler options made for the Linux kernel, which defines them itself; a Linux
# build of NVIDIA's code turns the options on when the build computer's compiler takes them, and an Intel Mac's clang
# takes some (build/nv_compile_lines.py removes them from the RM's and NVKMS's commands). The complete check, every
# import against the export lists of macOS 15 and 26, is build/kext_check.sh; it needs those lists from xnu's sources,
# which a build does not download. This one knows only the names that no macOS has, and costs a moment.
# Exit 0: none of them; 7: one or more, each named on a STOP line; 2: the list cannot be read or is empty.
set -uo pipefail
F=${1:?usage: kext_link_imports.sh <nm -u output, or -> <kext name>}; K=${2:?usage: kext_link_imports.sh <nm -u output, or -> <kext name>}
if [ "$F" = - ]; then L=$(cat) || { echo "STOP: the import list of $K could not be read"; exit 2; }
else [ -f "$F" ] || { echo "STOP: no import list of $K at $F"; exit 2; }; L=$(cat "$F"); fi
# the last word of each line is the name (Apple's nm -u prints the name alone, llvm-nm "U name")
L=$(printf '%s\n' "$L" | awk 'NF { print $NF }')
[ -n "$L" ] || { echo "STOP: the import list of $K is empty: that is not a kext's (it imports at least the kernel's _IOLog or OSObject)"; exit 2; }
# <extended regular expression for the Mach-O name, without |>|<the option that brings it>
TABLE='^___x86_return_thunk$|-mfunction-return=thunk-extern (every return jumps to the Linux kernel'"'"'s return thunk)
^___x86_indirect_|-mindirect-branch=thunk-extern or -mretpoline-external-thunk (indirect calls through the Linux kernel'"'"'s retpoline thunks)
^___x86_|an x86 helper of the Linux kernel
^___fentry__$|-mfentry (the Linux kernel'"'"'s function tracing hook)
^_?_mcount$|-pg (profiling hook)
^___cyg_profile_func_|-finstrument-functions'
bad=0; seen=" "
while IFS='|' read -r rx why; do
  while read -r s; do
    [ -n "$s" ] || continue
    case "$seen" in *" $s "*) continue ;; esac; seen="$seen$s "
    echo "STOP: $K imports $s, which no macOS defines: the kext would not load. It comes from a compiler option made for the Linux kernel, $why. Build it again in a new work folder; if this stays, report it with this line and the build log"
    bad=1
  done <<< "$(printf '%s\n' "$L" | grep -E "$rx" || true)"
done <<< "$TABLE"
[ $bad = 0 ] || exit 7
exit 0
