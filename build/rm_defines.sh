#!/bin/bash
# rm_defines.sh <src/nvidia of a tree build_rm620.sh built>: the -D flags of NVIDIA's own compile command for osapi.c,
# one per line, without -DNVRM and -D_LANGUAGE_C: what build_nvrm.sh, accel_build.sh and kext_check.sh give the kext
# code that includes RM headers. The command is the one of a file named here, not the first line of compile_cmds.sh:
# a list written by an incremental "make -n" once held a single, unrelated command, and the kexts took its flags
# without a word. Stops (exit 2, on stderr) when the list has no such command or it lacks the defines every RM file has.
set -u
NV=${1:?usage: rm_defines.sh <open-gpu-kernel-modules tree>/src/nvidia}
L=$NV/_out/Darwin_x86_64/compile_cmds.sh
F=arch/nvalloc/unix/src/osapi.c
stop() { echo "STOP: $*" >&2; exit 2; }
[ -f "$L" ] || stop "$L is missing: build rm first (build/build_rm620.sh)"
n=$(grep -cF " -c $F " "$L")
[ "$n" = 1 ] || stop "$n compile commands for $F in $L, one expected: the list is incomplete (build rm again in a new work dir)"
line=$(grep -F " -c $F " "$L")
for d in -DNVRM -D_LANGUAGE_C -DNV_DARWIN -DNV_X86_64 -DPORT_IS_KERNEL_BUILD=1; do
  case " $line " in *" $d "*) ;; *) stop "the compile command for $F in $L lacks $d" ;; esac
done
printf '%s\n' "$line" | tr ' ' '\n' | grep -E '^-D' | sed -e 's/"//g' | grep -v -E '^-D(NVRM|_LANGUAGE_C)$'
