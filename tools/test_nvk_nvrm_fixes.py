#!/usr/bin/env python3
"""The fixes of nvk/nvk-nvrm-fixes.patch and the commit list of nvk/nvk-mesa-main-2.patch (3.0.1).

The NVRM back end's sources (src/nouveau/vulkan/nvkmd/nvrm/) are new files of nvk/nvk-macos.patch, so their whole text
after the chain can be had from the patches alone: this test applies, file by file, every hunk of the default 3.0 chain
of build/build_nvk.sh to those files (exact context, no fuzz) and holds:

  the lock      every insert, search and removal in the device's table of CPU mappings (dev->mappings) is between a
                lock and an unlock of dev->mappings_mutex in the same function; nothing else is locked and no RM call
                is made while it is held (a leaf lock, so it cannot be part of a lock cycle); no function returns with
                it held; the mutex is created before the table and destroyed with it; no other file reaches the table;
                an unmap removes its entry before the RM unmap. Controls: the text with one lock line removed, with an
                RM call moved inside the lock, and the chain without nvk-nvrm-fixes all fail
  placed maps   VK_EXT_map_memory_placed and its two features follow has_map_fixed and has_overmap of the back end,
                which the NVRM back end never sets
  read once     NVRM_FLUSH_TIMEOUT_MS, NVK_SYSMEM_GPU_UNCACHED and NVK_BAR_VRAM are each read in one function run
                through util_call_once and nowhere else; with a C compiler the three readers are compiled from the
                patch text and give, for several values, what the old per-call expressions gave, with one read of the
                setting for a thousand calls
  osEvent       the device's osEvent is -1 before the first way out through nvkmd_dev_destroy
  main-2        the header lists the commits it carries, oldest first, and NOTICE lists the same ones; each commit's
                files and one added line of each are in the patch; with NM_MESA_GIT naming a Mesa clone that has the
                commits, the patch's added and removed lines are exactly those of the listed commits (CI lists left
                out). Control: a header missing a commit, and a patch with a line no commit has, fail
  the chain     both patches follow nvk-boost-policy in the 3.0 chain, neither is an upstream-fix patch that
                NVK_SKIP_PATCHES may leave out, and no header line can be taken for part of a diff

usage: test_nvk_nvrm_fixes.py [driver checkout]      NM_MESA_GIT=<mesa git> for the full commit comparison"""
import os, pathlib, re, shutil, subprocess, sys, tempfile

R = pathlib.Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1]
NVK = R / "nvk"
FIXP, MAIN2 = NVK / "nvk-nvrm-fixes.patch", NVK / "nvk-mesa-main-2.patch"
NVRM = "src/nouveau/vulkan/nvkmd/nvrm/"
fails = 0


def arm(name, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + name + (f" ({str(detail).strip()[-300:]})" if detail and not ok else ""))
    fails += not ok


if not FIXP.is_file() or not MAIN2.is_file():
    print("SKIP: nvk/nvk-nvrm-fixes.patch or nvk/nvk-mesa-main-2.patch is not in this checkout")
    sys.exit(2)
fixtext, main2 = FIXP.read_text(), MAIN2.read_text()
bld = (R / "build/build_nvk.sh").read_text()


# --- the chain, and the post-chain text of the NVRM sources ---------------------------------------------------------
def chain_of(text):
    """CHAIN of the 3.0 arm, as bash gives it from the script's "case $REL in ... esac" block alone."""
    m = re.search(r"(?ms)^case \$REL in\n.*?^esac$", text)
    script = 'REL=3.0; NVK=x; FIXES=; CHAIN=; BASE=; eval "$1" >/dev/null; printf "%s" "$CHAIN"'
    r = subprocess.run(["bash", "-c", script, "_", m.group(0) if m else "false"], capture_output=True, text=True)
    return r.stdout.split() if r.returncode == 0 else []


def sections(text):
    """[(path, [(old_start, [hunk lines])])] of a unified diff; the path is the new name without its b/ prefix."""
    out, lines, i = [], text.split("\n"), 0
    while i < len(lines):
        if lines[i].startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            path = re.sub(r"^b/", "", lines[i + 1][4:].split("\t")[0].strip())
            hunks, i = [], i + 2
            while i < len(lines):
                m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", lines[i])
                if not m:
                    break
                old_n, new_n = int(m.group(2) or 1), int(m.group(4) or 1)
                body, i = [], i + 1
                while (old_n > 0 or new_n > 0) and i < len(lines):
                    l = lines[i]
                    if l.startswith("\\"):
                        i += 1
                        continue
                    body.append(l)
                    if l[:1] in (" ", "-") or l == "":
                        old_n -= 1
                    if l[:1] in (" ", "+") or l == "":
                        new_n -= 1
                    i += 1
                hunks.append((int(m.group(1)), body))
            out.append((path, hunks))
        else:
            i += 1
    return out


def apply(old, hunks):
    """old lines + hunks -> new lines; every hunk's old text must match exactly somewhere (offsets allowed)."""
    cur, shift = list(old), 0
    for start, body in hunks:
        before = [l[1:] for l in body if l[:1] in (" ", "-") or l == ""]
        after = [l[1:] for l in body if l[:1] in (" ", "+") or l == ""]
        want = max(start - 1 + shift, 0) if before else start + shift
        if not before:
            at = min(want, len(cur))
        else:
            spots = [k for k in range(len(cur) - len(before) + 1) if cur[k:k + len(before)] == before]
            if not spots:
                raise ValueError(f"hunk at {start} does not apply")
            at = min(spots, key=lambda k: abs(k - want))
        cur[at:at + len(before)] = after
        shift += len(after) - len(before)
    return cur


def patch_file(name):
    return NVK / ("nvk-fixes-after-def16.patch" if name == "@FIXES" else name + ".patch")


def nvrm_files(names):
    """{path: text} of every file under the NVRM directory after the named patches, in order."""
    files = {}
    for name in names:
        for path, hunks in sections(patch_file(name).read_text(errors="replace")):
            if path.startswith(NVRM):
                if path not in files and not (hunks and hunks[0][0] == 0):
                    continue   # not a new file of the chain: only its hunks are known (checked on the patch text)
                files[path] = "\n".join(apply(files.get(path, "").split("\n") if path in files else [], hunks))
    return files


chain = chain_of(bld)
upstream = re.search(r'^UPSTREAM_FIXES="([^"]*)"', bld, re.M)
# a later patch may follow them (nvk-local-map-vram does); the rules below are held on the text after the whole chain
fix_at = chain.index("nvk-nvrm-fixes") if "nvk-nvrm-fixes" in chain else len(chain)
arm("the 3.0 chain applies nvk-boost-policy, nvk-mesa-main-2 and nvk-nvrm-fixes one after the other",
    chain[max(fix_at - 2, 0):fix_at + 1] == ["nvk-boost-policy", "nvk-mesa-main-2", "nvk-nvrm-fixes"],
    " ".join(chain[-5:]))
arm("neither new patch is an upstream-fix patch NVK_SKIP_PATCHES can leave out (later patches build on them)",
    upstream is not None and not {"nvk-mesa-main-2", "nvk-nvrm-fixes"} & set(upstream.group(1).split()))
RISKY = r"[ \tX]*(--- |\+\+\+ |@@|\*\*\* |Index:|Prereq:|diff |[<>] |\.$|\d)"
for name, text in (("nvk-nvrm-fixes", fixtext), ("nvk-mesa-main-2", main2)):
    head = text.partition("\ndiff --git ")[0]
    risky = [l for l in head.splitlines() if re.match(RISKY, l)]
    arm(f"{name}: a prose header, no line of it can be taken for part of a patch", len(head) > 400 and not risky, risky)

try:
    after = nvrm_files(chain)
    before = nvrm_files(chain[:fix_at])
    built = True
except (ValueError, OSError) as e:
    after = before = {}
    built = False
    arm("the NVRM sources can be rebuilt from the chain", False, e)
mem, dev = after.get(NVRM + "nvkmd_nvrm_mem.c", ""), after.get(NVRM + "nvkmd_nvrm_dev.c", "")
ctx, hdr = after.get(NVRM + "nvkmd_nvrm_ctx.c", ""), after.get(NVRM + "nvkmd_nvrm.h", "")
fix_secs = dict(sections(fixtext))
post = {p: [l[1:] for _, b in h for l in b if l[:1] in (" ", "+")] for p, h in fix_secs.items()}


def joined(lines):
    return "\n".join(lines)


missed = [p for p, h in fix_secs.items() if p.startswith(NVRM)
          and not all(joined([l[1:] for l in b if l[:1] in (" ", "+")]) in after.get(p, "") for _, b in h)]
arm("control: the rebuilt NVRM files carry every hunk of nvk-nvrm-fixes as it reads after the patch",
    built and mem and dev and ctx and not missed, missed)

# --- the lock -------------------------------------------------------------------------------------------------------
TABLE_OP = re.compile(r"_mesa_hash_table_(\w+)\(\s*dev->mappings\b")
LOCK, UNLOCK = "simple_mtx_lock(&dev->mappings_mutex)", "simple_mtx_unlock(&dev->mappings_mutex)"
OTHER = re.compile(r"\b(simple_mtx_lock|simple_mtx_trylock|mtx_lock|mtx_trylock|pthread_mutex_lock|"
                   r"pthread_mutex_trylock|nvrm_rec_lock\w*|nvRmApi\w+|nvrm_xnu_\w+|NVRM_OPEN|NVRM_CLOSE)\s*\(")


def functions(text):
    """[(name, [lines])] of the top-level function bodies: from a line "{" in column 0 to the next "}" in column 0."""
    out, lines, i = [], text.split("\n"), 0
    while i < len(lines):
        if lines[i] == "{":
            name = next((re.match(r"\s*(\w+)\s*\(", lines[k]).group(1) for k in range(i - 1, max(i - 6, -1), -1)
                         if re.match(r"\s*\w+\s*\(", lines[k])), "?")
            j = i + 1
            while j < len(lines) and lines[j] != "}":
                j += 1
            out.append((name, lines[i + 1:j]))
            i = j
        i += 1
    return out


def lock_problems(text, init_ok=("nvkmd_nvrm_create_dev", "nvkmd_nvrm_dev_destroy")):
    p, ops = [], 0
    for name, body in functions(text):
        held = False
        for l in body:
            code = l.split("//")[0]
            if LOCK in code:
                if held:
                    p.append(f"{name}: locked twice")
                held = True
                continue
            if UNLOCK in code:
                if not held:
                    p.append(f"{name}: unlock without lock")
                held = False
                continue
            m = TABLE_OP.search(code)
            if m and m.group(1) not in ("create", "destroy"):
                ops += 1
                if not held:
                    p.append(f"{name}: {m.group(0)} outside the lock")
            elif m and name not in init_ok:
                p.append(f"{name}: {m.group(0)} outside device create/destroy")
            if held and OTHER.search(code):
                p.append(f"{name}: {OTHER.search(code).group(1)} while the mappings lock is held")
            if held and re.search(r"\breturn\b", code):
                p.append(f"{name}: returns with the mappings lock held")
        if held:
            p.append(f"{name}: ends with the mappings lock held")
    return p, ops


p, ops = lock_problems(mem)
arm("every insert, search and removal in dev->mappings is under dev->mappings_mutex, which is a leaf lock",
    built and not p and ops == 3, (p, ops))
arm("the table's mutex is a field of the device next to the table",
    re.search(r"struct hash_table \*mappings;\n\s*simple_mtx_t mappings_mutex;", hdr) is not None)
dp, dops = lock_problems(dev)
c_at = dev.find("simple_mtx_init(&dev->mappings_mutex"), dev.find("dev->mappings = _mesa_hash_table_create")
arm("the mutex is created before the table (and before any way out) and destroyed with it",
    not dp and 0 <= c_at[0] < c_at[1] and "simple_mtx_destroy(&dev->mappings_mutex);" in dev, (dp, c_at))
TABLE_NAME = re.compile(r"->mappings\b|\bmappings_mutex\b")
users = sorted({path for name in chain for path, hunks in sections(patch_file(name).read_text(errors="replace"))
                if any(l.startswith("+") and TABLE_NAME.search(l) for _, b in hunks for l in b)})
arm("no other source of the chain reaches the table (every reader and writer is in nvkmd_nvrm_mem.c)",
    users == [NVRM + "nvkmd_nvrm.h", NVRM + "nvkmd_nvrm_dev.c", NVRM + "nvkmd_nvrm_mem.c"], users)
unmap = dict(functions(mem)).get("nvkmd_nvrm_mem_unmap", [])
ri = next((k for k, l in enumerate(unmap) if "_mesa_hash_table_remove(dev->mappings" in l), -1)
ui = next((k for k, l in enumerate(unmap) if "nvRmApiUnmapMemory(" in l), -1)
arm("an unmap takes its entry out of the table before the RM unmap (a new map of that address keeps its entry)",
    0 <= ri < ui, (ri, ui))
cut = mem.replace(LOCK, "/* lock removed */", 1)
arm("control: the text with one lock line removed fails", bool(lock_problems(cut)[0]))
moved = mem.replace(UNLOCK + ";\n   if (mapping == NULL)", "nvRmApiUnmapMemory(&rm, 0, 0, 0, mapping);\n   " + UNLOCK +
                    ";\n   if (mapping == NULL)", 1)
arm("control: an RM call moved inside the lock fails", moved != mem and bool(lock_problems(moved)[0]))
arm("control: the chain without nvk-nvrm-fixes fails the lock check",
    built and bool(lock_problems(before.get(NVRM + "nvkmd_nvrm_mem.c", ""))[0]))

# --- placed maps ----------------------------------------------------------------------------------------------------
pd = joined(post.get("src/nouveau/vulkan/nvk_physical_device.c", []))
gate = "supported_extensions.EXT_map_memory_placed = nvkmd->kmd_info.has_map_fixed && nvkmd->kmd_info.has_overmap;"
g, e, f = (pd.find(gate), pd.find("supported_extensions.EXT_external_memory_host = "),
           pd.find("nvk_get_device_features(&nvkmd->dev_info"))
arm("VK_EXT_map_memory_placed is offered only where the back end has has_map_fixed and has_overmap, decided before the "
    "features are", 0 <= e < g < f, (e, g, f))
arm("memoryMapPlaced and memoryUnmapReserve follow the extension, memoryMapRangePlaced stays off",
    ".memoryMapPlaced = supported_extensions->EXT_map_memory_placed," in pd
    and ".memoryUnmapReserve = supported_extensions->EXT_map_memory_placed," in pd
    and ".memoryMapRangePlaced = false," in pd and ".memoryMapPlaced = true" not in pd)
pdev = after.get(NVRM + "nvkmd_nvrm_pdev.c", "")
mapfn = dict(functions(mem)).get("nvkmd_nvrm_mem_map", [])
uses = [k for k, l in enumerate(mapfn) if "fixed_addr" in l]
arm("the NVRM back end sets neither has_map_fixed nor has_overmap (its map ignores the address, its overmap fails)",
    pdev and "has_map_fixed" not in pdev and "has_overmap" not in pdev
    and "placed mapping is not implemented on nvrm" in joined(dict(functions(mem)).get("nvkmd_nvrm_mem_overmap", []))
    and len(uses) == 1 and "if (fixed_addr != NULL)" in mapfn[uses[0]] and "if (mem->hostPtr)" in mapfn[uses[0] - 1])

# --- read once ------------------------------------------------------------------------------------------------------
dm = joined(post.get("src/nouveau/vulkan/nvk_device_memory.c", []))
dm_minus = [l[1:] for _, b in fix_secs.get("src/nouveau/vulkan/nvk_device_memory.c", []) for l in b if l[:1] == "-"]
arm("NVRM_FLUSH_TIMEOUT_MS is read in one place of the back end, a function run through util_call_once",
    ctx.count('nvmtl_cfg_getenv("NVRM_FLUSH_TIMEOUT_MS")') == 1
    and 'nvmtl_cfg_getenv("NVRM_FLUSH_TIMEOUT_MS")'
    in joined(dict(functions(ctx)).get("nvkmd_nvrm_flush_timeout_read", []))
    and "util_call_once(&nvkmd_nvrm_flush_timeout_once, nvkmd_nvrm_flush_timeout_read);" in ctx
    and "const long timeoutMs = nvkmd_nvrm_flush_timeout_ms();" in ctx)
arm("NVK_SYSMEM_GPU_UNCACHED and NVK_BAR_VRAM are read once each, not in nvk_memory_type_flags",
    dm.count('nvmtl_cfg_getenv("NVK_SYSMEM_GPU_UNCACHED")') == 1 and dm.count('nvmtl_cfg_getenv("NVK_BAR_VRAM")') == 1
    and "util_call_once(&nvk_sysmem_gpu_uncached_once, nvk_sysmem_gpu_uncached_read);" in dm
    and "util_call_once(&nvk_bar_vram_off_once, nvk_bar_vram_off_read);" in dm
    and all('nvmtl_cfg_getenv("' not in l for n, b in functions(dm) if not n.endswith("_read") for l in b)
    and dm.count('nvmtl_cfg_getenv("') == 2
    and sum("nvmtl_cfg_getenv" in l for l in dm_minus) == 2)


def c_slice(text, start, end):
    a = text.find(start)
    b = text.find(end, a)
    return text[a:b + len(end)] if a >= 0 and b >= 0 else ""


cc = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
readers = (c_slice(ctx, "static long nvkmd_nvrm_flush_timeout_value;", "return nvkmd_nvrm_flush_timeout_value;\n}")
           + "\n" + c_slice(dm, "static bool nvk_sysmem_gpu_uncached_value", "return nvk_bar_vram_off_value;\n}"))
HARNESS = r'''
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef struct { int done; } util_once_flag;
#define UTIL_ONCE_FLAG_INIT { 0 }
static void util_call_once(util_once_flag *f, void (*fn)(void)) { if (!f->done) { f->done = 1; fn(); } }
static const char *value; static int reads;
static const char *nvmtl_cfg_getenv(const char *name) { (void)name; reads++; return value; }
%s
int main(int argc, char **argv)
{
   value = strcmp(argv[1], "(unset)") ? argv[1] : NULL;
   const char *e = value;   /* the expressions the patch replaced, evaluated on the same value */
   long old_ms = e ? atol(e) : 5000; bool old_cached = !(e && e[0] == '1'); bool old_bar = !(e && e[0] == '0');
   int ok = 1;
   for (int i = 0; i < 1000; i++)
      ok &= nvkmd_nvrm_flush_timeout_ms() == old_ms && !nvk_sysmem_gpu_uncached() == old_cached &&
            !nvk_bar_vram_off() == old_bar;
   printf("%%d %%d\n", ok, reads);
   return argc < 2;
}
'''
if not cc:
    print("note: no C compiler on PATH: the read-once readers were not compiled")
elif readers.count("static") < 6:
    arm("the three read-once readers can be cut out of the patched text", False, readers[:200])
else:
    with tempfile.TemporaryDirectory(prefix="nvrmfixes-", dir=os.environ.get("TMPDIR")) as t:
        src, exe = pathlib.Path(t, "readers.c"), pathlib.Path(t, "readers")
        src.write_text(HARNESS % readers)
        r = subprocess.run([cc, "-std=c11", "-Wall", "-Werror", "-Wno-unused-function", str(src), "-o", str(exe)],
                           capture_output=True, text=True)
        arm("the read-once readers compile as the patch has them", r.returncode == 0, r.stderr)
        if r.returncode == 0:
            got = {v: subprocess.run([str(exe), v], capture_output=True, text=True).stdout.split()
                   for v in ("(unset)", "250", "0", "1", "abc", "-1", "")}
            arm("each reader gives what the old per-call expression gave, for unset, 250, 0, 1, abc, -1 and empty",
                all(g[:1] == ["1"] for g in got.values()), got)
            arm("a thousand calls of the three read the settings three times (once each)",
                all(g[1:] == ["3"] for g in got.values()), got)

# --- osEvent --------------------------------------------------------------------------------------------------------
create = joined(dict(functions(dev)).get("nvkmd_nvrm_create_dev", []))
first_out = create.find("nvkmd_dev_destroy(")
arm("osEvent is -1 before the first way out through nvkmd_dev_destroy (CALLOC's 0 is standard input)",
    0 <= create.find("dev->osEvent = -1;") < first_out)

# --- nvk-mesa-main-2: the commits it lists and carries --------------------------------------------------------------
COMMIT = re.compile(r"^ +commit ([0-9a-f]{8}) (\S.*)$")
# (hash, subject, files, one line the commit adds)
EXPECT = [
    ("0f737d4e", "nvk: fix color_write_enable", ["src/nouveau/vulkan/nvk_cmd_draw.c", "src/nouveau/vulkan/nvk_mme.c",
     "src/nouveau/vulkan/nvk_mme.h"], "      struct mme_value temp = mme_and(b, mask, mme_imm(BITFIELD_RANGE(0, 4)));"),
    ("fe4e33ab", "nvk: fix range calculation for map sync", ["src/nouveau/vulkan/nvk_indirect_execution_set.c"],
     "                             (max_idx - min_idx + 1) * (uint64_t)ies->stride_B);"),
    ("617f463e", "nvk: only upload descriptor state if dirty", ["src/nouveau/vulkan/nvk_cmd_buffer.c"],
     "   desc->push_dirty = 0;"),
    ("3e4d14a1", "vulkan: Add vk_image_format_info_2_stencil_usage(..)", ["src/vulkan/runtime/vk_physical_device.h"],
     "vk_image_format_info_2_stencil_usage(const VkPhysicalDeviceImageFormatInfo2 *info)"),
    ("3ff6450e", "nvk: Include stencil usage in image format queries", ["src/nouveau/vulkan/nvk_image.c"],
     "      vk_image_format_info_2_stencil_usage(pImageFormatInfo);"),
    ("f924c7c9", "nvk: limit push descriptor set upload to max_buffer_size", ["src/nouveau/vulkan/nvk_cmd_buffer.c",
     "src/nouveau/vulkan/nvk_descriptor_set.c", "src/nouveau/vulkan/nvk_descriptor_set.h"],
     "   push_set->max_buffer_size = layout->max_buffer_size;"),
    ("db5503a2", "vulkan: initialize swapchain private mutex properly", ["src/vulkan/runtime/vk_device.c"],
     "   mtx_destroy(&device->swapchain_private_mtx);"),
    ("c9ce420e", "vulkan: fix lookup of VkRenderingAttachmentLocationInfo", ["src/vulkan/runtime/vk_render_pass.c"],
     "   return vk_find_struct_const(pBeginInfo->pInheritanceInfo,"),
]


def listed(text):
    return [m.groups() for m in map(COMMIT.match, text.partition("\ndiff --git ")[0].splitlines()) if m]


def carried(text):
    secs = sections(text)
    plus = {l[1:] for _, h in secs for _, b in h for l in b if l.startswith("+")}
    return sorted({p for p, _ in secs}), plus


def main2_problems(text):
    p = []
    if listed(text) != [(c, s) for c, s, _, _ in EXPECT]:
        p.append(f"header lists {listed(text)}")
    files, plus = carried(text)
    if files != sorted({f for _, _, fs, _ in EXPECT for f in fs}):
        p.append(f"files {files}")
    p += [f"{c}: no '{sig.strip()}'" for c, _, _, sig in EXPECT if sig not in plus]
    return p


arm("nvk-mesa-main-2: the header lists, oldest first, the commits whose files and lines it carries",
    not main2_problems(main2), main2_problems(main2))
notice = (R / "NOTICE").read_text()
KEY = "nvk/nvk-mesa-main-2.patch"
part = notice.split(KEY, 1)[1].split("\n\n", 1)[0] if KEY in notice else ""
in_notice = [m.groups() for m in map(re.compile(r"^ +commit ([0-9a-f]{8}) (\S.*)$").match, part.splitlines()[1:]) if m]
arm("NOTICE lists the same commits for nvk/nvk-mesa-main-2.patch", in_notice == listed(main2), in_notice)
arm("control: a header that misses a commit fails",
    bool(main2_problems(main2.replace("  commit db5503a2 vulkan: initialize swapchain private mutex properly\n", ""))))
three = []
for name in ("nvk-mesa-26.3.0", "nvk-nak-fixes-main", "nvk-nak-sched-main"):
    three += [p for p, _ in sections((NVK / (name + ".patch")).read_text())]
arm("nvk-mesa-main-2 touches no file of the three upstream-fix patches and nothing of NAK or NIR",
    not set(carried(main2)[0]) & set(three)
    and not any(f.startswith(("src/nouveau/compiler/", "src/compiler/")) for f in carried(main2)[0]))
MESA = os.environ.get("NM_MESA_GIT")


def changed_lines(text):
    return sorted(l for p, h in sections(text) if not p.startswith("src/nouveau/ci/") for _, b in h for l in b
                  if l[:1] in "+-")


if not MESA:
    print("note: NM_MESA_GIT is not set: the patch's lines were not compared with the upstream commits")
else:
    want, missing = [], []
    for c, _, _, _ in EXPECT:
        r = subprocess.run(["git", "-C", MESA, "format-patch", "-1", "--stdout", c], capture_output=True, text=True)
        (want.extend(changed_lines(r.stdout)) if r.returncode == 0 else missing.append(c))
    arm("the Mesa clone has every listed commit", not missing, missing)
    arm("the patch's added and removed lines are exactly those of the listed commits (without src/nouveau/ci)",
        not missing and changed_lines(main2) == sorted(want))
    arm("control: a patch with a line no listed commit has fails",
        sorted(changed_lines(main2) + ["+   /* not upstream */"]) != sorted(want))

print("FAILED" if fails else "PASS")
sys.exit(1 if fails else 0)
