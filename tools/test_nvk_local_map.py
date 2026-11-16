#!/usr/bin/env python3
"""Where NVK's own mapped device-local memory goes by default (nvk/nvk-local-map-vram.patch, 3.0.1).

3.0 kept the shader heap, the descriptor tables and sets and NVK's other mapped device-local memory in system RAM
unless NVK_LOCAL_MAP_SYSMEM=0 asked for video memory. 3.0.1 places it in video memory by default in every process but
WindowServer, for memory that is not shared, with NVK_LOCAL_MAP_SYSMEM=1 as the way back. This test takes the patch's
own functions (the patch carries them whole, so no Mesa tree is needed), compiles them with stand-ins for the
settings reader, the process name and the device, and holds:

  the rule      for every combination of NVK_LOCAL_MAP_SYSMEM (unset, 1, 0, empty, another word), NVK_BAR_VRAM (unset,
                0, 1), the process (an app, WindowServer, a name that only starts like it) and a BAR1 that does or does
                not cover the VRAM: the default is video memory for unshared memory in an app and system RAM in
                WindowServer; =1 gives system RAM everywhere; =0 gives video memory everywhere, shared memory included,
                exactly where 3.0's expression did; NVK_BAR_VRAM=0 keeps the new default off; nothing moves when BAR1
                is smaller than the VRAM
  read once     a thousand allocations read each setting once
  the text      the old function is gone, the allocation path asks the new ones under the same three flags, an
                allocation the default placed is made in system RAM when the RM has no video memory for it, and the
                patch touches the one file
  the chain     the patch is in the 3.0 chain after nvk-nvrm-fixes (it is made on top of it) and is not an
                upstream-fix patch that NVK_SKIP_PATCHES may leave out; its header is prose patch(1) cannot misread
  the way back  CHANGELOG.md names NVK_LOCAL_MAP_SYSMEM=1 under 3.0.1, and the registry row and the settings tool say
                what the default now is
  controls      the same checks fail on a copy of the rule that forgets WindowServer, one that forgets shared memory
                and one that forgets NVK_BAR_VRAM=0

Linux or macOS, a C compiler, no GPU. usage: test_nvk_local_map.py [driver checkout]"""
import itertools, pathlib, re, shutil, subprocess, sys, tempfile

R = pathlib.Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1]
PATCH = R / "nvk/nvk-local-map-vram.patch"
fails = 0


def arm(name, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + name + (f" ({str(detail).strip()[-300:]})" if detail and not ok else ""))
    fails += not ok


if not PATCH.is_file():
    print("SKIP: nvk/nvk-local-map-vram.patch is not in this checkout")
    sys.exit(2)
ptext = PATCH.read_text()
head, _, body = ptext.partition("\ndiff --git ")
lines = body.split("\n")
after = "\n".join(l[1:] for l in lines if l[:1] in (" ", "+") and not l.startswith("+++"))
plus = "\n".join(l[1:] for l in lines if l.startswith("+") and not l.startswith("+++"))
minus = "\n".join(l[1:] for l in lines if l.startswith("-") and not l.startswith("---"))

# --- the text -------------------------------------------------------------------------------------------------------
arm("the patch touches nvkmd_nvrm_mem.c and nothing else",
    re.findall(r"(?m)^diff --git a/(\S+) b/\1$", "diff --git " + body) == ["src/nouveau/vulkan/nvkmd/nvrm/nvkmd_nvrm_mem.c"])
arm("3.0's opt-in function is removed and nothing still calls it",
    "nvrm_local_map_stays_vram(struct nvkmd_nvrm_pdev *pdev)" in minus and "nvrm_local_map_stays_vram(" not in after)
site = re.search(r"if \(\(flags & NVKMD_MEM_LOCAL\) && \(flags & NVKMD_MEM_CAN_MAP\) && !\(flags & NVKMD_MEM_BAR\)\) \{\s*"
                 r"const int lm = nvrm_local_map_mode\(pdev\);\s*if \(nvrm_local_map_wants_bar\(lm, flags\)\) \{\s*"
                 r"flags \|= NVKMD_MEM_BAR;\s*placed_by_default = lm == NVRM_LOCAL_MAP_VRAM_PRIVATE;", after)
arm("the allocation path asks the rule for LOCAL memory that can be mapped and is not behind BAR1 yet", site is not None)
# the way out when the VRAM is full: only for what the default placed, with the request as it came, demoted the way
# the rule for mappable memory demotes it (so the retry cannot be placed again and cannot recurse a second time)
retry = re.search(r"if \(nvRes != NV_OK && placed_by_default\) \{.*?return nvkmd_nvrm_alloc_tiled_mem\(_dev, log_obj, size_in_B, "
                  r"align_B, pte_kind, tile_mode,\s*\(flags_in & ~\(NVKMD_MEM_LOCAL \| NVKMD_MEM_VRAM\)\) \| NVKMD_MEM_GART, "
                  r"mem_out\);\s*\}\s*if \(nvRes != NV_OK\) \{", after, re.S)
arm("an allocation the default placed and the RM refused is made in system RAM, as 3.0 made it; others fail as before",
    retry is not None and after.count("placed_by_default = ") == 2 and "bool placed_by_default = false;" in after
    and after.index("const enum nvkmd_mem_flags flags_in = flags;") < after.index("flags |= NVKMD_MEM_BAR;")
    and after.index("const uint64_t size_in_B = size_B;") < after.index("flags |= NVKMD_MEM_BAR;"))
arm("each of the two settings is read in the once-run reader and nowhere else",
    plus.count('nvmtl_cfg_getenv("NVK_LOCAL_MAP_SYSMEM")') == 1 and plus.count('nvmtl_cfg_getenv("NVK_BAR_VRAM")') == 1
    and "util_call_once_data(&nvrm_local_map_mode_once, nvrm_local_map_mode_read, pdev);" in plus)
arm("WindowServer is told by its exact process name", 'strcmp(pn, "WindowServer") == 0' in plus)


def c_slice(text, start, end):
    a = text.find(start)
    b = text.find(end, a)
    return text[a:b + len(end)] if a >= 0 and b >= 0 else ""


# context and added lines together: git may keep a brace or a blank line of the old function as context
code = c_slice(after, "enum { NVRM_LOCAL_MAP_SYSMEM = 0", "return nvrm_local_map_mode_value;\n}")
arm("the rule, the per-allocation test and the reader are whole in the patch's text",
    all(s in code for s in ("nvrm_local_map_rule(const char *e, bool bar_covers_vram, bool window_server, bool bar_vram_off)",
                            "nvrm_local_map_wants_bar(int mode, enum nvkmd_mem_flags flags)",
                            "nvrm_local_map_mode_read(const void *data)")))

# --- the rule, compiled ---------------------------------------------------------------------------------------------
HARNESS = r'''
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
enum nvkmd_mem_flags { NVKMD_MEM_LOCAL = 1 << 0, NVKMD_MEM_CAN_MAP = 1 << 3, NVKMD_MEM_SHARED = 1 << 4, NVKMD_MEM_BAR = 1 << 7 };
struct nvkmd_nvrm_pdev { struct { struct { unsigned long long bar_size_B, vram_size_B; } dev_info; } base; };
typedef struct { int done; } util_once_flag;
#define UTIL_ONCE_FLAG_INIT { 0 }
static void util_call_once_data(util_once_flag *f, void (*fn)(const void *), const void *d) { if (!f->done) { f->done = 1; fn(d); } }
static const char *v_lms, *v_bar, *v_prog; static int reads_lms, reads_bar, reads_other;
static const char *nvmtl_cfg_getenv(const char *name)
{
   if (!strcmp(name, "NVK_LOCAL_MAP_SYSMEM")) { reads_lms++; return v_lms; }
   if (!strcmp(name, "NVK_BAR_VRAM")) { reads_bar++; return v_bar; }
   reads_other++; return NULL;
}
static int nvkmd_nvrm_diag_on(void) { return 0; }
static const char *test_progname(void) { return v_prog; }
#define getprogname test_progname
%s
static const char *arg(const char *a) { return strcmp(a, "(unset)") ? a : NULL; }
int main(int argc, char **argv)
{
   if (argc != 6) return 2;
   v_lms = arg(argv[1]); v_bar = arg(argv[2]); v_prog = arg(argv[3]);
   struct nvkmd_nvrm_pdev pdev = { { { strtoull(argv[4], NULL, 0), strtoull(argv[5], NULL, 0) } } };
   int mode = -1, same = 1;
   for (int i = 0; i < 1000; i++) { int m = nvrm_local_map_mode(&pdev); if (i && m != mode) same = 0; mode = m; }
   printf("%%d %%d %%d %%d %%d %%d %%d\n", mode, same, reads_lms, reads_bar, reads_other,
          (int)nvrm_local_map_wants_bar(mode, NVKMD_MEM_LOCAL | NVKMD_MEM_CAN_MAP),
          (int)nvrm_local_map_wants_bar(mode, NVKMD_MEM_LOCAL | NVKMD_MEM_CAN_MAP | NVKMD_MEM_SHARED));
   return 0;
}
'''
LMS = ["(unset)", "1", "0", "", "yes", "01"]
BARV = ["(unset)", "0", "1"]
PROG = ["java", "WindowServer", "WindowServer2", "(unset)"]
SIZES = [("0x200000000", "0x200000000"), ("0x400000000", "0x200000000"), ("0x10000000", "0x200000000")]
GB8 = ("0x200000000", "0x200000000")


def run_matrix(src, d, tag):
    """{(lms, barv, prog, sizes): (mode, same, reads_lms, reads_bar, reads_other, private, shared)}, or a string."""
    c, exe = d / f"{tag}.c", d / tag
    c.write_text(HARNESS % src)
    r = subprocess.run([cc, "-O1", "-Wall", "-Wno-unused-function", "-o", str(exe), str(c)], capture_output=True, text=True)
    if r.returncode:
        return "does not compile: " + r.stderr[-400:]
    out = {}
    for key in itertools.product(LMS, BARV, PROG, SIZES):
        lms, barv, prog, (bar, vram) = key
        o = subprocess.run([str(exe), lms, barv, prog, bar, vram], capture_output=True, text=True, timeout=30)
        if o.returncode or len(o.stdout.split()) != 7:
            return f"run {key}: exit {o.returncode} {o.stdout!r}"
        out[key] = tuple(int(x) for x in o.stdout.split())
    return out


def problems(res):
    """The cases where the compiled code does not do what the release notes say."""
    if isinstance(res, str):
        return [res]
    bad = []
    for (lms, barv, prog, (bar, vram)), (mode, same, r_lms, r_bar, r_other, private, shared) in res.items():
        covers = int(bar, 0) >= int(vram, 0)
        old_on = lms != "(unset)" and lms[:1] == "0" and covers          # 3.0: on = (e && e[0] == '0') && bar_covers
        if lms != "(unset)":
            want = 2 if old_on else 0                                    # a set value means what it meant in 3.0
        elif not covers or prog == "WindowServer" or barv[:1] == "0":
            want = 0                                                     # the three places the new default stays out of
        else:
            want = 1
        if mode != want or not same:
            bad.append(f"{lms}/{barv}/{prog}/{bar}: mode {mode}, want {want}")
        if (private, shared) != {0: (0, 0), 1: (1, 0), 2: (1, 1)}[want]:
            bad.append(f"{lms}/{barv}/{prog}/{bar}: unshared {private} shared {shared} under mode {want}")
        if r_lms != 1 or r_bar != 1 or r_other:
            bad.append(f"{lms}/{barv}/{prog}/{bar}: read {r_lms}+{r_bar}+{r_other} times in 1000 allocations")
    return bad


cc = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
if not cc or not code:
    arm("a C compiler is on PATH and the patch's functions were found (the rule was not run)", bool(code), "no compiler" if code else "")
    print("SKIP: no C compiler on PATH: the rule was not compiled" if code else "FAILED")
    sys.exit(2 if code and not fails else 1)
with tempfile.TemporaryDirectory() as tmp:
    d = pathlib.Path(tmp)
    res = run_matrix(code, d, "rule")
    bad = problems(res)
    arm(f"the rule holds in all {len(LMS) * len(BARV) * len(PROG) * len(SIZES)} combinations, each setting read once", not bad, bad[:3])
    if not isinstance(res, str):
        arm("default in an app: video memory for unshared memory, system RAM for shared", res[("(unset)", "(unset)", "java", GB8)][0] == 1)
        arm("default in WindowServer: system RAM", res[("(unset)", "(unset)", "WindowServer", GB8)][0] == 0)
        arm("a process with no name counts as an app", res[("(unset)", "(unset)", "(unset)", GB8)][0] == 1)
        arm("NVK_LOCAL_MAP_SYSMEM=1 is the way back in every process",
            all(res[("1", b, p, GB8)][0] == 0 for b in BARV for p in PROG))
        arm("NVK_LOCAL_MAP_SYSMEM=0 still means video memory everywhere, WindowServer and shared memory included",
            all(res[("0", b, p, GB8)][0] == 2 and res[("0", b, p, GB8)][6] == 1 for b in BARV for p in PROG))
        arm("NVK_BAR_VRAM=0 keeps the new default off", res[("(unset)", "0", "java", GB8)][0] == 0)
        arm("a BAR1 smaller than the VRAM moves nothing, whatever is set",
            all(res[(l, b, p, SIZES[2])][0] == 0 for l in LMS for b in BARV for p in PROG))
    muts = [("forgets WindowServer", "(window_server || bar_vram_off)", "(bar_vram_off)"),
            ("forgets shared memory", " && !(flags & NVKMD_MEM_SHARED)", ""),
            ("forgets NVK_BAR_VRAM=0", "(window_server || bar_vram_off)", "(window_server)")]
    for i, (what, old, new) in enumerate(muts):
        arm(f"control: a rule that {what} fails", code.count(old) == 1 and bool(problems(run_matrix(code.replace(old, new), d, f"mut{i}"))))

# --- the chain ------------------------------------------------------------------------------------------------------
bld = (R / "build/build_nvk.sh").read_text()
m = re.search(r'^\s*3\.0\)\s*FIXES=\S+\s*\n(?:\s*#[^\n]*\n)*\s*CHAIN="([^"]+)"', bld, re.M)
chain = m.group(1).split() if m else []
arm("the 3.0 chain applies nvk-local-map-vram after nvk-nvrm-fixes",
    "nvk-local-map-vram" in chain and "nvk-nvrm-fixes" in chain
    and chain.index("nvk-nvrm-fixes") < chain.index("nvk-local-map-vram"), " ".join(chain[-4:]))
arm("only the 3.0 chain has it (2.9 and 2.8 are built as they were)", len(re.findall(r'CHAIN="[^"]*nvk-local-map-vram', bld)) == 1)
up = re.search(r'^UPSTREAM_FIXES="([^"]*)"', bld, re.M)
arm("it is not an upstream-fix patch NVK_SKIP_PATCHES can leave out", up is not None and "nvk-local-map-vram" not in up.group(1).split())
risky = [l for l in head.splitlines() if re.match(r"[ \tX]*(--- |\+\+\+ |@@|\*\*\* |Index:|Prereq:|diff |[<>] |\.$|\d)", l)]
arm("a prose header that says whose change it is, no line of it can be taken for part of a patch",
    len(head) > 400 and not risky and "NullMoth's own change" in head, risky)

# --- the way back, where a user reads it ----------------------------------------------------------------------------
log = (R / "CHANGELOG.md").read_text()
sec = log.partition("\n## 3.0.1\n")[2].partition("\n## 3.0\n")[0]
arm("CHANGELOG.md names NVK_LOCAL_MAP_SYSMEM=1 as the way back under 3.0.1",
    re.search(r"(?m)^\|[^|\n]*\| `NVK_LOCAL_MAP_SYSMEM=1` \| `nvrm610\.conf` \|$", sec) is not None)
row = [l.split("\t") for l in (R / "tools/switches.tsv").read_text().split("\n") if l.startswith("env\tNVK_LOCAL_MAP_SYSMEM\t")]
arm("the registry row is a setting whose text says the desktop keeps system RAM and what On and off do",
    len(row) == 1 and row[0][2] == "setting" and "WindowServer" in row[0][-1] and "as before 3.0.1" in row[0][-1])
tool = (R / "tools/nullmoth_settings.py").read_text()
arm("the settings tool offers it with the same words", len(row) == 1 and row[0][-1].split(": ", 1)[-1] in tool)

print("FAILED" if fails else "PASS")
sys.exit(1 if fails else 0)
