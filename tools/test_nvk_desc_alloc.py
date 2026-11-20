#!/usr/bin/env python3
"""A new descriptor set does not walk the bindings of a layout that has no immutable samplers.

nvk_descriptor_set_create pre-writes the immutable samplers of every new set by visiting each binding of its layout,
and binding_count is the highest binding number plus one. The Metal plugin's layout numbers its bindings up to 641, so
each set it allocated walked 642 binding records to write nothing. nvk/nvk-desc-alloc.patch keeps the layout's
immutable sampler count (nvk_CreateDescriptorSetLayout already computes it) and skips the walk when it is zero.

The equivalence is modelled in C with NVK's own rules: which binding gets an immutable_samplers pointer at layout
creation (binding_has_immutable_samplers, the stashed index, a zero descriptorCount skipped) and which elements the
walk writes. For many layouts, sparse and dense, with and without immutable samplers, the patched walk writes exactly
the elements the old one wrote. The patch text is checked for the three places it changes and its place in the chain.
Usage: test_nvk_desc_alloc.py"""
import pathlib, random, shutil, subprocess, sys, tempfile

R = pathlib.Path(__file__).resolve().parents[1]
patch = (R / "nvk/nvk-desc-alloc.patch").read_text()
bld = (R / "build/build_nvk.sh").read_text()
fails = 0


def arm(name, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + name + (f" ({str(detail).strip()[-200:]})" if detail and not ok else ""))
    fails += not ok


MODEL = r'''
#include <stdio.h>
#include <stdint.h>
#include <string.h>
enum { SAMPLER = 0, COMBINED = 1, SAMPLED = 2, STORAGE_BUF = 7, UBO_DYN = 8 };
typedef struct { uint32_t binding, type, count; int has_imm; } in_binding;   /* VkDescriptorSetLayoutBinding */
typedef struct { uint32_t type, array_size; void *imm; } lay_binding;        /* nvk_descriptor_set_binding_layout */
#define MAXB 700
static int has_imm(const in_binding *b) { return (b->type == SAMPLER || b->type == COMBINED) && b->has_imm; }
static void *const SAMPLERS = (void *)0x1000;
/* nvk_CreateDescriptorSetLayout: the parts that decide immutable_samplers and the count */
static uint32_t create(const in_binding *in, uint32_t n, lay_binding *lb, uint32_t *imm_count)
{
    uint32_t num = 0, cnt = 0;
    for (uint32_t j = 0; j < n; j++) { if (in[j].binding + 1 > num) num = in[j].binding + 1; if (has_imm(&in[j])) cnt += in[j].count; }
    memset(lb, 0, sizeof(lay_binding) * MAXB);   /* multizalloc */
    for (uint32_t j = 0; j < n; j++) lb[in[j].binding].imm = (void *)(uintptr_t)(j + 1);
    for (uint32_t b = 0; b < num; b++) {
        if (!lb[b].imm) continue;
        const in_binding *bi = &in[(uintptr_t)lb[b].imm - 1];
        lb[b].imm = NULL;
        if (bi->count == 0) continue;
        lb[b].type = bi->type; lb[b].array_size = bi->count;
        if (has_imm(bi)) lb[b].imm = SAMPLERS;
    }
    *imm_count = cnt; return num;
}
/* nvk_descriptor_set_create's walk: returns how many elements it wrote and a checksum of which */
static uint64_t walk(const lay_binding *lb, uint32_t binding_count, uint32_t limit, uint32_t *visited)
{
    uint64_t sum = 0; *visited = 0;
    for (uint32_t b = 0; b < limit; b++) {
        (*visited)++;
        if (lb[b].type != SAMPLER && lb[b].type != COMBINED) continue;
        if (lb[b].imm == NULL) continue;
        for (uint32_t j = 0; j < lb[b].array_size; j++) sum = sum * 1000003u + (b * 64u + j + 1);
    }
    (void)binding_count; return sum;
}
int main(void)
{
    static in_binding in[MAXB]; static lay_binding lb[MAXB];
    unsigned layouts = 0, mismatches = 0, with_imm = 0, skipped_with_writes = 0;
    unsigned long long old_visits = 0, new_visits = 0;
    char line[64];
    while (fgets(line, sizeof line, stdin)) {
        uint32_t n = 0;
        if (sscanf(line, "L %u", &n) != 1) continue;
        for (uint32_t j = 0; j < n; j++) {
            unsigned b, t, c, h;
            if (!fgets(line, sizeof line, stdin) || sscanf(line, "%u %u %u %u", &b, &t, &c, &h) != 4) return 2;
            in[j] = (in_binding){ b, t, c, (int)h };
        }
        uint32_t cnt, v_old, v_new;
        const uint32_t num = create(in, n, lb, &cnt);
        const uint64_t a = walk(lb, num, num, &v_old);                   /* before the patch */
        const uint64_t p = walk(lb, num, cnt ? num : 0, &v_new);         /* after it */
        layouts++; mismatches += a != p; with_imm += cnt != 0; skipped_with_writes += (!cnt && a != 0);
        old_visits += v_old; new_visits += v_new;
    }
    printf("%u %u %u %u %llu %llu\n", layouts, mismatches, with_imm, skipped_with_writes, old_visits, new_visits);
    return 0;
}
'''


def layouts():
    rnd = random.Random(20261120)
    out = []
    # the plugin's layout: 0-31 storage buffers, 32-159 sampled images, 160-191 samplers, 480-607 storage images,
    # 640 the address table, 641 the GL uniform buffer; no immutable samplers
    plug = [(i, 7, 1, 0) for i in range(32)] + [(32 + i, 2, 1, 0) for i in range(128)] + \
           [(160 + i, 0, 1, 0) for i in range(32)] + [(480 + i, 3, 1, 0) for i in range(128)] + [(640, 8, 1, 0), (641, 8, 1, 0)]
    out.append(plug)
    out.append(plug[:-1])
    # the same with one immutable sampler, and a sampler binding that names samplers with a zero count
    out.append(plug + [(650, 0, 2, 1)])
    out.append(plug + [(650, 0, 0, 1)])
    # pImmutableSamplers on a type that ignores it
    out.append([(3, 2, 4, 1), (9, 7, 1, 1)])
    out.append([])
    for _ in range(400):
        nums = rnd.sample(range(0, 680), rnd.randint(1, 40))
        out.append([(b, rnd.choice([0, 1, 2, 7, 8]), rnd.choice([0, 1, 1, 1, 3]), rnd.random() < 0.15) for b in nums])
    return out


cc = shutil.which("cc") or shutil.which("clang")
if not cc:
    print("SKIP: needs a C compiler"); sys.exit(2)
with tempfile.TemporaryDirectory() as d:
    d = pathlib.Path(d); (d / "m.c").write_text(MODEL)
    subprocess.run([cc, "-Wall", "-Werror", "-O1", "-o", str(d / "m"), str(d / "m.c")], check=True)
    ls = layouts()
    feed = "".join(f"L {len(l)}\n" + "".join(f"{b} {t} {c} {int(h)}\n" for b, t, c, h in l) for l in ls)
    res = subprocess.run([str(d / "m")], input=feed, capture_output=True, text=True, check=True).stdout.split()
    n, bad, withimm, lost, ov, nv = map(int, res)
    one = subprocess.run([str(d / "m")], input=f"L {len(ls[0])}\n" + "".join(f"{b} {t} {c} {int(h)}\n" for b, t, c, h in ls[0]),
                         capture_output=True, text=True, check=True).stdout.split()

arm("every layout modelled", n == len(ls), res)
arm("the patched walk writes exactly the immutable samplers the old walk wrote, for every layout", bad == 0, res)
arm("a layout whose count is zero never had anything to write", lost == 0, res)
arm("control: some layouts have immutable samplers and are still walked", 0 < withimm < n, res)
arm("the plugin's layout: the old walk visited 642 binding records per set, the patched one none",
    one[4] == "642" and one[5] == "0", one)

added = "\n".join(l[1:] for l in patch.splitlines() if l.startswith("+") and not l.startswith("+++"))
arm("the layout keeps the count it computes", "   uint32_t immutable_sampler_count;" in added
    and "   layout->immutable_sampler_count = immutable_sampler_count;" in added)
arm("the walk is bounded by the count, and only the bound changes",
    "layout->immutable_sampler_count ? layout->binding_count : 0;" in added
    and "-   for (uint32_t b = 0; b < layout->binding_count; b++) {" in patch
    and "+   for (uint32_t b = 0; b < prewrite_count; b++) {" in patch)
arm("the writer is still initialised and finished around the walk",
    "    nvk_descriptor_writer_init_set(pdev, &w, set);\n-   for (uint32_t b = 0;" in patch
    and not any(l.startswith("-") and "nvk_descriptor_writer_" in l for l in patch.splitlines()))
arm("the patch touches only the three descriptor files",
    sorted(l.split(" b/")[1] for l in patch.splitlines() if l.startswith("diff --git ")) ==
    ["src/nouveau/vulkan/nvk_descriptor_set.c", "src/nouveau/vulkan/nvk_descriptor_set_layout.c",
     "src/nouveau/vulkan/nvk_descriptor_set_layout.h"])
chain = next((l for l in bld.splitlines() if l.strip().startswith('CHAIN="nvk-macos') and "nvk-card-select" in l
              and "nvk-mesa-26.3.0" in l), "")
arm("the patch ends the 3.0 chain, after every patch that changes these files", chain.rstrip().endswith('nvk-gpu-caps nvk-desc-alloc" ;;'), chain[-80:])
arm("the 2.9 and 2.8 chains do not have it", sum("nvk-desc-alloc" in l for l in bld.splitlines() if 'CHAIN="' in l) == 1)
print("FAILED" if fails else "PASS"); sys.exit(1 if fails else 0)
