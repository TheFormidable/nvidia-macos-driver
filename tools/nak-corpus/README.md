# NAK corpus: the shaders every compiler change is measured on

A change to the shader compiler (NAK) is judged by the same figures over the same shaders: instructions (all, main
pipe, uniform), registers, warps per SM, spills and static cycles. A change is accepted with the diff of those figures
against the run before it, or reverted.

| file | what |
|---|---|
| `cases.txt` | the 30 hand-written cases, one per line: `name|pipetest arguments|vertex shader` |
| `shaders/` | their GLSL sources (`quad.vert` is the vertex shader of most) |
| `gen.py <dir>` | writes the other 120 cases into `<dir>/sh` and `<dir>/cases.txt` |
| `pipetest.c` | builds one graphics pipeline from a vertex and a fragment SPIR-V file (its header lists the options) |
| `run.sh <out.jsonl> [NAME=value ...]` | compiles every case once with the shader caches off and `NAK_STATS=1` |
| `baseline-sm120-host.jsonl` | the run of 3.0.14's NAK on Linux, Mesa's nouveau drm-shim as a GB207 (shader model 120) |

`NAK_STATS=1` is read by `nvk/nvk-nak-stats.patch`: the compiler writes one JSON line per compiled shader stage on
stderr, and `run.sh` keeps those lines with the case's name added. Compare two runs with
`tools/nak_stats_diff.py BASE NEW`; `--gate-instrs PCT` and `--gate-spills` turn a phase's limits into an exit status.

```sh
# point the loader at the NVK to measure, then:
tools/nak-corpus/run.sh /tmp/before.jsonl
tools/nak-corpus/run.sh /tmp/after.jsonl NVMTL_NAK_NO_PREPASS=1      # a switch, or a rebuilt NVK
tools/nak_stats_diff.py /tmp/before.jsonl /tmp/after.jsonl --gate-instrs 5 --gate-spills
```

On Linux the NVK is the fork's, built on Mesa's nouveau drm-shim: `LD_PRELOAD=<build>/src/nouveau/drm-shim/libnouveau_noop_drm_shim.so`,
`NOUVEAU_CHIPSET=1b7` (GB207, shader model 120) and `VK_DRIVER_FILES=<build>/src/nouveau/vulkan/nouveau_devenv_icd.x86_64.json`.
Nothing runs on a GPU there: only compilation is meaningful. Figures from the host say what the compiler emits, not
how fast it runs; a change that moves them still has to be measured on a card (for the same frame rate, lower
graphics utilisation or fewer watts).

Not here, because they cannot be put in the repository: the shaders of a game's shader pack and Apple's Core
Animation kernels. Shaders captured from an application can be run the same way from a local folder, and compared
with `nak_stats_diff.py` by stage and SPIR-V hash.
