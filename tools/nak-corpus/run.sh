#!/bin/bash
# usage: tools/nak-corpus/run.sh <out.jsonl> [NAME=value ...]
#
# Compiles every case of the NAK corpus (tools/nak-corpus/README.md) once, with the shader caches off and NAK_STATS=1,
# and writes one JSON line per compiled shader stage to <out.jsonl>, each with the case's name added. Compare two runs
# with tools/nak_stats_diff.py. The NAME=value arguments are put into the environment of every compile (a compiler
# switch to measure, for example NVMTL_NAK_NO_PREPASS=1).
#
# The Vulkan driver is whatever the loader finds: point VK_DRIVER_FILES at the NVK to measure (on Linux, the build of
# this NVK on Mesa's nouveau drm-shim, with LD_PRELOAD and NOUVEAU_CHIPSET=1b7 for an RTX 5050's shader model 120).
# Needs glslangValidator and a C compiler with the Vulkan headers and loader. Exit 1 if any case did not compile.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=${1:?usage: run.sh <out.jsonl> [NAME=value ...]}; shift
W=$(mktemp -d); trap 'rm -rf "$W"' EXIT
command -v glslangValidator >/dev/null || { echo "run.sh: glslangValidator not found" >&2; exit 1; }
"${CC:-cc}" -O2 -o "$W/pipetest" "$HERE/pipetest.c" -lvulkan || { echo "run.sh: pipetest does not build" >&2; exit 1; }
mkdir -p "$W/sh" && cp "$HERE"/shaders/* "$W/sh/"
python3 "$HERE/gen.py" "$W/gen" >/dev/null && cp "$W"/gen/sh/* "$W/sh/"
cat "$HERE/cases.txt" "$W/gen/cases.txt" > "$W/cases.txt"
for f in "$W"/sh/*.frag "$W"/sh/*.vert; do
  glslangValidator -V --target-env vulkan1.3 -o "$f.spv" "$f" >/dev/null || { echo "run.sh: $f does not compile" >&2; exit 1; }
done
: > "$OUT"; bad=0; n=0
while IFS='|' read -r name args vert; do
  [ -n "$name" ] || continue
  n=$((n + 1))
  env MESA_SHADER_CACHE_DISABLE=true NAK_STATS=1 "$@" "$W/pipetest" -q $args "$W/sh/$vert.vert.spv" "$W/sh/$name.frag.spv" \
    >/dev/null 2> "$W/err" || { bad=$((bad + 1)); echo "run.sh: $name failed: $(tail -1 "$W/err")" >&2; continue; }
  grep '^{"nak_stats":' "$W/err" | sed "s/^{/{\"case\":\"$name\",/" >> "$OUT"
done < "$W/cases.txt"
echo "run.sh: $n cases, $bad failed, $(wc -l < "$OUT") shader lines in $OUT" >&2
[ "$bad" = 0 ]
