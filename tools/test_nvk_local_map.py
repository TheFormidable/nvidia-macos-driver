#!/usr/bin/env python3
"""Where NVK's own mapped device-local memory goes by default (nvk/nvk-local-map-vram.patch, 3.0.1).

3.0 kept the shader heap, the descriptor tables and sets and NVK's other mapped device-local memory in system RAM
unless NVK_LOCAL_MAP_SYSMEM=0 asked for video memory. 3.0.1 places it in video memory by default in every process but
WindowServer, for memory that is not shared and that the CPU does not read back (NVKMD_MEM_HOST_READ: USERD, the event
heap, descriptor pools), with NVK_LOCAL_MAP_SYSMEM=1 as the way back. This test takes the patch's own code (the patch
carries it with 12 lines of context, so no Mesa tree is needed), compiles it with stand-ins and holds:

  the rule      for every combination of NVK_LOCAL_MAP_SYSMEM (unset, 1, 0, empty, another word), NVK_BAR_VRAM (unset,
                0, 1), the process (an app, WindowServer, a name that only starts like it) and a BAR1 that does or does
                not cover the VRAM: the default is video memory for unshared memory the CPU does not read back, in an
                app, and system RAM in WindowServer; =1 gives system RAM everywhere; =0 gives video memory everywhere,
                shared and read-back memory included, exactly where 3.0's expression did; NVK_BAR_VRAM=0 keeps the new
                default off; nothing moves when BAR1 is smaller than the VRAM
  read once     a thousand allocations read each setting once
  the placement the block of nvkmd_nvrm_alloc_tiled_mem that applies the rule, compiled as it is in the patch, for each
                mode and every combination of LOCAL, CAN_MAP, BAR, SHARED, HOST_READ, a DEFAULT_BAR passed in and
                NVK_DEBUG=force_gart: the flags that come out (BAR, DEFAULT_BAR only on what the default placed,
                GART/LOCAL/VRAM), the request kept for the retry, and whether the default placed it
  the map       nvkmd_dev_alloc_mapped_mem compiled from the patch: a refused map of memory the default placed is
                made again once in system RAM, the first memory let go exactly once; any other refused map fails as
                before
  the order     with USERD in system RAM the read of GPPut before a doorbell no longer goes through BAR1, so the
  page          device keeps one page that the default places behind BAR1 and reads it before every doorbell.
                nvkmd_nvrm_dev_bar_order_init and the read are compiled from the patch: the page is asked for as
                plain LOCAL memory (a HOST_READ or SHARED request would be left in system RAM) only in the default
                mode, kept only when it carries the default's mark, let go otherwise; the read really loads from the
                page (the page is made unreadable and the load faults) and does nothing without a page or a device
  the text      the three allocations the CPU reads back say NVKMD_MEM_HOST_READ, the two flags are bits of their own,
                an allocation the default placed is made in system RAM when the RM has no video memory for it, each
                of the three doorbells the patch shows reads GPPut, then the order page, then rings; the device makes
                the page last and lets it go first; a refused BAR1 map of memory the default placed is not reported
                as an error; and the patch touches the nine files it names
  the chain     the patch is in the 3.0 chain after nvk-nvrm-fixes (it is made on top of it) and is not an
                upstream-fix patch that NVK_SKIP_PATCHES may leave out; its header is prose patch(1) cannot misread
  the way back  CHANGELOG.md has NVK_LOCAL_MAP_SYSMEM=1 in 3.0.1's table of ways back, the registry row is a setting
                and the settings tool shows the registry's words
  controls      the same checks fail on a copy of the rule that forgets WindowServer, shared memory, read-back memory
                or NVK_BAR_VRAM=0, and on copies of the placement, of the map and of the order page that are wrong in
                the ways listed

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


def hunks(path):
    """[(the @@ line, the text of the hunk after the patch)] of one file of the patch."""
    out, cur, infile = [], None, False
    for l in ("diff --git " + body).split("\n"):
        if l.startswith("diff --git "):
            infile = l == f"diff --git a/{path} b/{path}"
            cur = None
        elif infile and l.startswith("@@"):
            cur = [l, []]
            out.append(cur)
        elif infile and cur is not None and l[:1] in (" ", "+") and not l.startswith("+++"):
            cur[1].append(l[1:])
    return [(h, "\n".join(t) + "\n") for h, t in out]


def hunk_of(path, func, has=""):
    """The one hunk of the file whose @@ line names the function (and whose text has `has`), or ""."""
    hs = [t for h, t in hunks(path) if func in h and has in t]
    return hs[0] if len(hs) == 1 else ""


MEMC = "src/nouveau/vulkan/nvkmd/nvrm/nvkmd_nvrm_mem.c"
NVRMH = "src/nouveau/vulkan/nvkmd/nvrm/nvkmd_nvrm.h"
CTXC = "src/nouveau/vulkan/nvkmd/nvrm/nvkmd_nvrm_ctx.c"
DEVC = "src/nouveau/vulkan/nvkmd/nvrm/nvkmd_nvrm_dev.c"
RECC = "src/nouveau/vulkan/nvkmd/nvrm/nvkmd_nvrm_recover.c"
FILES = ["src/nouveau/vulkan/nvk_descriptor_set.c", "src/nouveau/vulkan/nvk_device.c", "src/nouveau/vulkan/nvkmd/nvkmd.c",
         "src/nouveau/vulkan/nvkmd/nvkmd.h", NVRMH, CTXC, DEVC, MEMC, RECC]

# --- the text -------------------------------------------------------------------------------------------------------
arm("the patch touches the nine files of the change and nothing else",
    sorted(re.findall(r"(?m)^diff --git a/(\S+) b/\1$", "diff --git " + body)) == FILES)
arm("3.0's opt-in function is removed and nothing still calls it",
    "nvrm_local_map_stays_vram(struct nvkmd_nvrm_pdev *pdev)" in minus and "nvrm_local_map_stays_vram(" not in after)
defs = re.findall(r"(?m)^#define (NVKMD_MEM_HOST_READ|NVKMD_MEM_DEFAULT_BAR) +\(\(enum nvkmd_mem_flags\)\(1u << (\d+)\)\)$",
                  "\n".join(t for _, t in hunks("src/nouveau/vulkan/nvkmd/nvkmd.h")))
bits = dict((n, int(b)) for n, b in defs)
# nvkmd.h's enum uses bits 0-5 and 7, nvkmd_nvrm_mem.c's NVK69_MEM_GPU_CACHED bit 6
taken = [int(b) for b in re.findall(r"(?m)^\s*NVKMD_MEM_\w+\s*= 1 << (\d+),", after)] + \
        [int(b) for b in re.findall(r"#define NVK69_MEM_GPU_CACHED \(\(enum nvkmd_mem_flags\)\(1u << (\d+)\)\)", after)]
arm("nvkmd.h defines NVKMD_MEM_HOST_READ and NVKMD_MEM_DEFAULT_BAR as macros, on bits no other flag uses",
    len(defs) == 2 and len(bits) == 2 and bits["NVKMD_MEM_HOST_READ"] != bits["NVKMD_MEM_DEFAULT_BAR"]
    and set(bits.values()).isdisjoint(set(taken) | set(range(0, 6))) and 7 in taken and 6 in taken, (defs, taken))
DEFINES = "\n".join(f"#define {n} ((enum nvkmd_mem_flags)(1u << {b}))" for n, b in defs)

sites = [
    ("USERD (nvkmd_nvrm_ctx.c)", "src/nouveau/vulkan/nvkmd/nvrm/nvkmd_nvrm_ctx.c",
     r"nvkmd_dev_alloc_mapped_mem\(_dev, log_obj, 0x80000, 0x10000, ([^;]*?)&ctx->userD\)\);"),
    ("the event heap (nvk_device.c)", "src/nouveau/vulkan/nvk_device.c",
     r"nvk_heap_init\(dev, &dev->event_heap,([^;]*?)\);"),
    ("descriptor pools (nvk_descriptor_set.c)", "src/nouveau/vulkan/nvk_descriptor_set.c",
     r"nvkmd_dev_alloc_mapped_mem\(dev->nvkmd, &dev->vk\.base,\s*mem_size, 0,([^;]*?)&pool->mem\);"),
]
for what, path, rx in sites:
    text = re.sub(r"/\*.*?\*/", " ", "".join(t for _, t in hunks(path)), flags=re.S)
    ms = re.findall(rx, text, re.S)
    flags_arg = ms[0].split(",")[0] if len(ms) == 1 else ""
    arm(f"the allocation of {what} passes NVKMD_MEM_LOCAL | NVKMD_MEM_HOST_READ (the CPU reads it back)",
        set(re.findall(r"NVKMD_MEM_\w+", flags_arg)) >= {"NVKMD_MEM_LOCAL", "NVKMD_MEM_HOST_READ"}, ms)

# the way out when the VRAM is full: only for what the default placed, with the request as it came, demoted the way
# the rule for mappable memory demotes it (so the retry cannot be placed again and cannot recurse a second time)
alloc_hunks = "".join(t for h, t in hunks(MEMC) if "nvkmd_nvrm_alloc_tiled_mem(" in h)
retry = re.search(r"NV_STATUS nvRes = nvRmApiAlloc\(&rm, pdev->hDevice, &hMemoryPhys, hClass, &params\);\s*"
                  r"if \(nvRes != NV_OK && placed_by_default\) \{.*?return nvkmd_nvrm_alloc_tiled_mem\(_dev, log_obj, size_in_B, "
                  r"align_B, pte_kind, tile_mode,\s*\(flags_in & ~\(NVKMD_MEM_LOCAL \| NVKMD_MEM_VRAM\)\) \| NVKMD_MEM_GART, "
                  r"mem_out\);\s*\}\s*if \(nvRes != NV_OK\) \{", alloc_hunks, re.S)
arm("an allocation the default placed and the RM refused is made again in system RAM, as 3.0 made it; others fail as before",
    retry is not None and alloc_hunks.count("placed_by_default = ") == 2)
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
# the placement: from the line after the API handle in nvkmd_nvrm_alloc_tiled_mem up to its alignment
ahunk = hunk_of(MEMC, "nvkmd_nvrm_alloc_tiled_mem(", "nvkmd_nvrm_dev_api_ctl(pdev, &rm);\n")
a = ahunk.find("nvkmd_nvrm_dev_api_ctl(pdev, &rm);\n")
b = ahunk.find("const uint32_t mem_align_B", a)
block = ahunk[a + len("nvkmd_nvrm_dev_api_ctl(pdev, &rm);\n"):b] if a >= 0 and b > a else ""
arm("the placement block of nvkmd_nvrm_alloc_tiled_mem is whole in the patch's text",
    "NVK_DEBUG_FORCE_GART" in block and "nvrm_local_map_wants_bar(" in block and "NVMTL batch 36" in block)
mhunk = hunk_of("src/nouveau/vulkan/nvkmd/nvkmd.c", "nvkmd_dev_alloc_mapped_mem(")
a = mhunk.find("\n{\n")
b = mhunk.find("\n}\n", a)
mapped = mhunk[a + 1:b + 3] if a >= 0 and b > a else ""
arm("the body of nvkmd_dev_alloc_mapped_mem is whole in the patch's text",
    mhunk.startswith("                           struct nvkmd_mem **mem_out)\n{") and "*mem_out = mem;" in mapped)

# --- the order page, in the text -------------------------------------------------------------------------------------
# Every doorbell the patch shows: the write-combining buffers drained, GPPut read, the order page read, then the ring.
RING = (r"__atomic_thread_fence\(__ATOMIC_SEQ_CST\);[^\n]*\n(?:\s*//[^\n]*\n)*\s*\(void\)\*\(volatile NvU32 \*\)&userD->GPPut;\n"
        r"(?:\s*//[^\n]*\n)*\s*nvkmd_nvrm_dev_bar_order\(ctx->base\.dev\);\n"
        r"\s*volatile NvU32 \*doorbell = [^;\n]*;\n\s*\*doorbell = ")
for what, path, n in (("a submission and the flush that waits (nvkmd_nvrm_ctx.c)", CTXC, 2),
                      ("the doorbell a stalled or rebuilt channel is rung with (nvkmd_nvrm_recover.c)", RECC, 1)):
    text = "".join(t for _, t in hunks(path))
    arm(f"{what}: GPPut is read, then the order page, then the doorbell is rung, at each of the {n}",
        len(re.findall(RING, text)) == n and len(re.findall(r"(?m)^\s*\*doorbell = ", text)) == n,
        (len(re.findall(RING, text)), len(re.findall(r"(?m)^\s*\*doorbell = ", text))))
devt = "".join(t for _, t in hunks(DEVC))
arm("the device makes the order page last, when it can allocate, and before it is handed out",
    re.search(r"nvkmd_nvrm_dev_bar_order_init\(dev, log_obj\);\n\s*\*dev_out = &dev->base;\n\s*return VK_SUCCESS;", devt) is not None
    and devt.count("nvkmd_nvrm_dev_bar_order_init(") == 1)
arm("the device lets the order page go first, while the mapping table its unmap needs is still there",
    re.search(r"dev->barOrder = NULL;\n\s*if \(dev->barOrderMem != NULL\)\n\s*nvkmd_mem_unref\(dev->barOrderMem\);\n"
              r"\s*if \(dev->mappings != NULL\)\n\s*_mesa_hash_table_destroy\(dev->mappings, NULL\);", devt) is not None)
hdrt = "".join(t for _, t in hunks(NVRMH))
arm("the device keeps the page and the address read before a doorbell, and the read is declared where both files see it",
    "   struct nvkmd_mem *barOrderMem;\n   const volatile NvU32 *barOrder;\n};" in hdrt
    and "void nvkmd_nvrm_dev_bar_order_init(struct nvkmd_nvrm_dev *dev, struct vk_object_base *log_obj);" in hdrt)
order_read = c_slice(hdrt, "static inline void\nnvkmd_nvrm_dev_bar_order(struct nvkmd_dev *dev)\n{", "\n}\n")
order_init = c_slice(after, "void\nnvkmd_nvrm_dev_bar_order_init(struct nvkmd_nvrm_dev *dev, struct vk_object_base *log_obj)\n{",
                     "\n}\n")
arm("the order page's two functions are whole in the patch's text",
    "(void)*p;" in order_read and "nvkmd_dev_alloc_mapped_mem(" in order_init and "dev->barOrder = mem->map;" in order_init)
# a refused BAR1 map of memory the default placed: the caller takes system RAM, so no error is reported for it
mapt = re.sub(r"/\*.*?\*/", " ", "".join(t for h, t in hunks(MEMC) if "nvkmd_nvrm_mem_map(" in h), flags=re.S)
quiet = re.search(r"NV_STATUS nvRes = nvRmApiMapMemory\([^;]*\);\s*"
                  r"if \(nvRes != NV_OK && \(mem->base\.flags & NVKMD_MEM_DEFAULT_BAR\)\) \{(.*?)\n   \}\s*"
                  r"if \(nvRes != NV_OK\) \{\s*fprintf\(stderr, \"\[!\] nvRes", mapt, re.S)
qb = quiet.group(1) if quiet else ""
arm("a refused BAR1 map of memory the default placed frees its record and returns the error without reporting one "
    "(a line under the diagnostics only); any other refused map is reported as before",
    quiet is not None and "vk_errorf" not in qb and "[!]" not in qb and qb.count("free(mapping);") == 1
    and re.search(r"if \(nvkmd_nvrm_diag_on\(\)\)\s*fprintf\(stderr,", qb) is not None
    and qb.rstrip().endswith("return VK_ERROR_OUT_OF_DEVICE_MEMORY;"), qb[-200:])

# --- the rule, compiled ---------------------------------------------------------------------------------------------
FLAGS = r'''
enum nvkmd_mem_flags { NVKMD_MEM_LOCAL = 1 << 0, NVKMD_MEM_GART = 1 << 1, NVKMD_MEM_VRAM = 1 << 2, NVKMD_MEM_CAN_MAP = 1 << 3,
                       NVKMD_MEM_SHARED = 1 << 4, NVKMD_MEM_COHERENT = 1 << 5, NVKMD_MEM_BAR = 1 << 7 };
'''
HARNESS = r'''
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
''' + FLAGS + r'''
%s
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
   const enum nvkmd_mem_flags lm = NVKMD_MEM_LOCAL | NVKMD_MEM_CAN_MAP;
   printf("%%d %%d %%d %%d %%d %%d %%d %%d %%d\n", mode, same, reads_lms, reads_bar, reads_other,
          (int)nvrm_local_map_wants_bar(mode, lm),
          (int)nvrm_local_map_wants_bar(mode, lm | NVKMD_MEM_SHARED),
          (int)nvrm_local_map_wants_bar(mode, lm | NVKMD_MEM_HOST_READ),
          (int)nvrm_local_map_wants_bar(mode, lm | NVKMD_MEM_SHARED | NVKMD_MEM_HOST_READ));
   return 0;
}
'''
LMS = ["(unset)", "1", "0", "", "yes", "01"]
BARV = ["(unset)", "0", "1"]
PROG = ["java", "WindowServer", "WindowServer2", "(unset)"]
SIZES = [("0x200000000", "0x200000000"), ("0x400000000", "0x200000000"), ("0x10000000", "0x200000000")]
GB8 = ("0x200000000", "0x200000000")
CFLAGS = ["-O1", "-Wall", "-Wno-unused-function"]


def build(src, d, tag):
    c, exe = d / f"{tag}.c", d / tag
    c.write_text(src)
    r = subprocess.run([cc] + CFLAGS + ["-o", str(exe), str(c)], capture_output=True, text=True)
    return ("does not compile: " + r.stderr[-400:]) if r.returncode else None, exe


def run_matrix(src, d, tag):
    """{(lms, barv, prog, sizes): (mode, same, reads_lms, reads_bar, reads_other, private, shared, hostread, both)},
    or a string."""
    err, exe = build(HARNESS % (DEFINES, src), d, tag)
    if err:
        return err
    out = {}
    for key in itertools.product(LMS, BARV, PROG, SIZES):
        lms, barv, prog, (bar, vram) = key
        o = subprocess.run([str(exe), lms, barv, prog, bar, vram], capture_output=True, text=True, timeout=30)
        if o.returncode or len(o.stdout.split()) != 9:
            return f"run {key}: exit {o.returncode} {o.stdout!r}"
        out[key] = tuple(int(x) for x in o.stdout.split())
    return out


def problems(res):
    """The cases where the compiled code does not do what the release notes say."""
    if isinstance(res, str):
        return [res]
    bad = []
    for (lms, barv, prog, (bar, vram)), (mode, same, r_lms, r_bar, r_other, *placed) in res.items():
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
        # placed: (unshared, shared, read back, shared and read back)
        if tuple(placed) != {0: (0, 0, 0, 0), 1: (1, 0, 0, 0), 2: (1, 1, 1, 1)}[want]:
            bad.append(f"{lms}/{barv}/{prog}/{bar}: unshared/shared/read back/both placed {placed} under mode {want}")
        if r_lms != 1 or r_bar != 1 or r_other:
            bad.append(f"{lms}/{barv}/{prog}/{bar}: read {r_lms}+{r_bar}+{r_other} times in 1000 allocations")
    return bad


# --- the placement, compiled ----------------------------------------------------------------------------------------
PLACE = r'''
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
''' + FLAGS + r'''
%s
#define NVK_DEBUG_FORCE_GART (1ull << 7)
struct nvk_pdev_stub { unsigned long long debug_flags; };
struct nvkmd_nvrm_pdev { int unused; };
struct nvkmd_nvrm_dev { struct { struct nvk_pdev_stub *pdev; } base; };
static int g_mode;
%s
%s
static int nvrm_local_map_mode(struct nvkmd_nvrm_pdev *pdev) { (void)pdev; return g_mode; }
static unsigned place(unsigned in, int force_gart, uint64_t size_B, int *placed, unsigned *fin, uint64_t *sin)
{
   struct nvk_pdev_stub nvp = { force_gart ? NVK_DEBUG_FORCE_GART : 0 };
   struct nvkmd_nvrm_dev dv = { { &nvp } }, *dev = &dv;
   struct nvkmd_nvrm_pdev pd = { 0 }, *pdev = &pd;
   enum nvkmd_mem_flags flags = (enum nvkmd_mem_flags)in;
   /* ---- the patch's text ---- */
%s
   /* ---- end ---- */
   *placed = placed_by_default; *fin = (unsigned)flags_in; *sin = size_in_B;
   return (unsigned)flags;
}
int main(void)
{
   const int modes[3] = { NVRM_LOCAL_MAP_SYSMEM, NVRM_LOCAL_MAP_VRAM_PRIVATE, NVRM_LOCAL_MAP_VRAM_ALL };
   const unsigned bit[6] = { NVKMD_MEM_LOCAL, NVKMD_MEM_CAN_MAP, NVKMD_MEM_BAR, NVKMD_MEM_SHARED, NVKMD_MEM_HOST_READ,
                             NVKMD_MEM_DEFAULT_BAR };
   for (int m = 0; m < 3; m++)
      for (int c = 0; c < 128; c++) {
         unsigned in = NVKMD_MEM_COHERENT;
         for (int i = 0; i < 6; i++) if (c & (1 << i)) in |= bit[i];
         g_mode = modes[m];
         int placed = -1; unsigned fin = 0; uint64_t sin = 0;
         unsigned out = place(in, (c >> 6) & 1, 0x12345, &placed, &fin, &sin);
         printf("%%d %%d %%u %%u %%d %%u %%d\n", m, c, in, out, placed, fin, sin == 0x12345);
      }
   return 0;
}
'''
LOCAL, GART, VRAM, CAN_MAP, SHARED, COHERENT, BAR = 1, 2, 4, 8, 16, 32, 128
HREAD, DBAR = 1 << bits.get("NVKMD_MEM_HOST_READ", 8), 1 << bits.get("NVKMD_MEM_DEFAULT_BAR", 9)


def want_place(mode, f, force):
    """What the commit message and nvkmd.h say comes out: (flags, placed_by_default, flags_in)."""
    f &= ~DBAR                                             # DEFAULT_BAR is never passed in
    if force:
        f = (f & ~(LOCAL | VRAM)) | GART
    fin, placed = f, 0
    if f & LOCAL and f & CAN_MAP and not f & BAR:
        if mode == 2 or (mode == 1 and not f & (SHARED | HREAD)):
            placed = int(mode == 1)                        # only the 3.0.1 default, not =0 and not the caller
            f |= BAR | (DBAR if placed else 0)
    if f & CAN_MAP and not f & BAR:                        # batch 36: mapped memory that is not BAR is system RAM
        f = (f & ~(LOCAL | VRAM)) | GART
    return f, placed, fin


def run_place(blk, d, tag):
    """{(mode, combination): (in, out, placed, flags_in, size kept)}, or a string."""
    rules = c_slice(code, "enum { NVRM_LOCAL_MAP_SYSMEM = 0", "};")
    wants = c_slice(code, "static bool\nnvrm_local_map_wants_bar(", "\n}\n")
    err, exe = build(PLACE % (DEFINES, rules, wants, blk), d, tag)
    if err:
        return err
    o = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    rows = [tuple(int(x) for x in l.split()) for l in o.stdout.splitlines()]
    if o.returncode or len(rows) != 384 or any(len(r) != 7 for r in rows):
        return f"exit {o.returncode}, {len(rows)} rows"
    return {(r[0], r[1]): r[2:] for r in rows}


def place_problems(res):
    if isinstance(res, str):
        return [res]
    bad = []
    for (m, c), (fl_in, out, placed, fin, size_kept) in res.items():
        want = want_place(m, fl_in, (c >> 6) & 1)
        if (out, placed, fin) != want or not size_kept:
            bad.append(f"mode {m} in {fl_in:#x} force_gart {(c >> 6) & 1}: out {out:#x} placed {placed} flags_in {fin:#x}, "
                       f"want {want[0]:#x} {want[1]} {want[2]:#x}")
    return bad


def placed_of(res, m, flags, force=0):
    """(out, placed) for one request."""
    c = sum(1 << i for i, b in enumerate((LOCAL, CAN_MAP, BAR, SHARED, HREAD, DBAR)) if flags & b) | (force << 6)
    r = res[(m, c)]
    return r[1], r[2]


# --- the map, compiled ----------------------------------------------------------------------------------------------
MAP = r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
typedef int VkResult;
#define VK_SUCCESS 0
''' + FLAGS + r'''
%s
enum nvkmd_mem_map_flags { NVKMD_MEM_MAP_RD = 1 << 0, NVKMD_MEM_MAP_WR = 1 << 1, NVKMD_MEM_MAP_FIXED = 1 << 3 };
struct nvkmd_dev { int unused; };
struct vk_object_base { int unused; };
struct nvkmd_mem { enum nvkmd_mem_flags flags; int id; };
static struct nvkmd_mem mems[4];
static int n_alloc, n_map, n_unref, alloc_res[4], map_res[4], map_ids[8], unref_ids[8], bad_args;
static unsigned alloc_mark, alloc_flags[4];
/* The backend marks what its default placed: the first allocation gets alloc_mark (DEFAULT_BAR or nothing). */
static VkResult nvkmd_dev_alloc_mem(struct nvkmd_dev *dev, struct vk_object_base *log_obj, uint64_t size_B, uint64_t align_B,
                                    enum nvkmd_mem_flags flags, struct nvkmd_mem **mem_out)
{
   (void)dev; (void)log_obj;
   if (n_alloc >= 4) abort();
   if (size_B != 0x80000 || align_B != 0x10000) bad_args++;
   const int i = n_alloc++;
   alloc_flags[i] = (unsigned)flags;
   if (alloc_res[i] != VK_SUCCESS) return alloc_res[i];
   mems[i].flags = (enum nvkmd_mem_flags)(flags | (i == 0 ? alloc_mark : 0));
   mems[i].id = i + 1;
   *mem_out = &mems[i];
   return VK_SUCCESS;
}
static VkResult nvkmd_mem_map(struct nvkmd_mem *mem, struct vk_object_base *log_obj, enum nvkmd_mem_map_flags map_flags,
                              void *fixed_addr, void **map_out)
{
   (void)log_obj; (void)fixed_addr; (void)map_out;
   if (n_map >= 8) abort();
   if (map_flags != NVKMD_MEM_MAP_WR) bad_args++;
   map_ids[n_map] = mem->id;
   return map_res[n_map++];
}
static void nvkmd_mem_unref(struct nvkmd_mem *mem) { if (n_unref >= 8) abort(); unref_ids[n_unref++] = mem->id; }
static VkResult
nvkmd_dev_alloc_mapped_mem(struct nvkmd_dev *dev, struct vk_object_base *log_obj, uint64_t size_B, uint64_t align_B,
                           enum nvkmd_mem_flags flags, enum nvkmd_mem_map_flags map_flags, struct nvkmd_mem **mem_out)
/* ---- the patch's text ---- */
%s
/* ---- end ---- */
static void list(const char *what, const int *v, int n)
{
   printf(" %%s=", what);
   for (int i = 0; i < n; i++) printf("%%s%%d", i ? "," : "", v[i]);
}
int main(int argc, char **argv)
{
   /* request flags, the first allocation's mark, then the results of the allocations and of the maps */
   if (argc != 7) return 2;
   const unsigned req = (unsigned)strtoul(argv[1], NULL, 0);
   alloc_mark = (unsigned)strtoul(argv[2], NULL, 0);
   alloc_res[0] = atoi(argv[3]); alloc_res[1] = atoi(argv[4]); map_res[0] = atoi(argv[5]); map_res[1] = atoi(argv[6]);
   struct nvkmd_dev dev = { 0 }; struct vk_object_base log = { 0 };
   struct nvkmd_mem *out = NULL;
   const VkResult r = nvkmd_dev_alloc_mapped_mem(&dev, &log, 0x80000, 0x10000, (enum nvkmd_mem_flags)req, NVKMD_MEM_MAP_WR, &out);
   printf("ret=%%d out=%%d allocs=%%d", r, out ? out->id : 0, n_alloc);
   printf(" flags=");
   for (int i = 0; i < n_alloc; i++) printf("%%s%%#x", i ? "," : "", alloc_flags[i]);
   list("maps", map_ids, n_map);
   list("unrefs", unref_ids, n_unref);
   printf(" args=%%s\n", bad_args ? "bad" : "ok");
   return 0;
}
'''
REQ = LOCAL | VRAM | COHERENT | HREAD          # a request with VRAM and other flags in it, to see what the retry keeps
E_MAP, E_OOM = -5, -2
GART_RETRY = (REQ & ~(LOCAL | VRAM)) | GART | CAN_MAP
MAP_CASES = [
    # (what the arm holds, the first allocation's mark, allocation results, map results, what comes out)
    ("a good map allocates once and hands that memory out",
     DBAR, (0, 0), (0, 0), f"ret=0 out=1 allocs=1 flags={REQ | CAN_MAP:#x} maps=1 unrefs= args=ok"),
    ("a refused map of memory the default placed is made again once in system RAM (GART and CAN_MAP, without LOCAL "
     "and VRAM), the first memory let go once, and the second is handed out",
     DBAR, (0, 0), (E_MAP, 0), f"ret=0 out=2 allocs=2 flags={REQ | CAN_MAP:#x},{GART_RETRY:#x} maps=1,2 unrefs=1 args=ok"),
    ("a second refused map returns the error, with both memories let go once each and nothing handed out",
     DBAR, (0, 0), (E_MAP, E_MAP),
     f"ret={E_MAP} out=0 allocs=2 flags={REQ | CAN_MAP:#x},{GART_RETRY:#x} maps=1,2 unrefs=1,2 args=ok"),
    ("a refused allocation in system RAM after a refused map returns that error, the first memory let go once",
     DBAR, (0, E_OOM), (E_MAP, 0), f"ret={E_OOM} out=0 allocs=2 flags={REQ | CAN_MAP:#x},{GART_RETRY:#x} maps=1 unrefs=1 args=ok"),
    ("a refused map of memory the default did not place fails as before: one allocation, let go once",
     0, (0, 0), (E_MAP, 0), f"ret={E_MAP} out=0 allocs=1 flags={REQ | CAN_MAP:#x} maps=1 unrefs=1 args=ok"),
    ("a refused allocation fails at once, nothing mapped or let go",
     DBAR, (E_OOM, 0), (0, 0), f"ret={E_OOM} out=0 allocs=1 flags={REQ | CAN_MAP:#x} maps= unrefs= args=ok"),
]


def run_map(body_text, d, tag):
    """[(what, got, want)] of the cases that fail, or a string."""
    err, exe = build(MAP % (DEFINES, body_text), d, tag)
    if err:
        return err
    out = []
    for what, mark, ar, mr, want in MAP_CASES:
        o = subprocess.run([str(exe), hex(REQ), hex(mark), str(ar[0]), str(ar[1]), str(mr[0]), str(mr[1])],
                           capture_output=True, text=True, timeout=30)
        out.append((what, f"exit {o.returncode} " + o.stdout.strip() if o.returncode else o.stdout.strip(), want))
    return out


def map_problems(res):
    if isinstance(res, str):
        return [res]
    return [f"{w}: {g} != {x}" for w, g, x in res if g != x]


def caught(res, check):
    """A control holds when the wrong copy compiles and runs, and the checks then find something wrong with it."""
    return not isinstance(res, str) and bool(check(res))


# --- the order page, compiled ---------------------------------------------------------------------------------------
ORDER = r'''
#include <setjmp.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <unistd.h>
typedef int VkResult;
typedef uint32_t NvU32;
#define VK_SUCCESS 0
''' + FLAGS + r'''
%s
enum nvkmd_mem_map_flags { NVKMD_MEM_MAP_RD = 1 << 0, NVKMD_MEM_MAP_WR = 1 << 1, NVKMD_MEM_MAP_RDWR = 3 };
enum { NVRM_LOCAL_MAP_SYSMEM = 0, NVRM_LOCAL_MAP_VRAM_PRIVATE = 1, NVRM_LOCAL_MAP_VRAM_ALL = 2 };
struct vk_object_base { int unused; };
struct nvkmd_pdev { int unused; };
struct nvkmd_dev { struct nvkmd_pdev *pdev; };
struct nvkmd_mem { enum nvkmd_mem_flags flags; void *map; };
struct nvkmd_nvrm_pdev { struct nvkmd_pdev base; };
struct nvkmd_nvrm_dev { struct nvkmd_dev base; struct nvkmd_mem *barOrderMem; const volatile NvU32 *barOrder; };
static struct nvkmd_nvrm_pdev *nvkmd_nvrm_pdev(struct nvkmd_pdev *p) { return (struct nvkmd_nvrm_pdev *)p; }
static struct nvkmd_nvrm_dev *nvkmd_nvrm_dev(struct nvkmd_dev *d) { return (struct nvkmd_nvrm_dev *)d; }
static int mode, alloc_res, n_mode, n_alloc, n_unref, bad_args;
static unsigned mark, alloc_req;
static void *page;
static struct nvkmd_mem the_mem;
static int nvrm_local_map_mode(struct nvkmd_nvrm_pdev *pdev) { (void)pdev; n_mode++; return mode; }
/* The backend's allocation: what comes back carries `mark` (DEFAULT_BAR when the default placed it behind BAR1). */
static VkResult nvkmd_dev_alloc_mapped_mem(struct nvkmd_dev *dev, struct vk_object_base *log_obj, uint64_t size_B, uint64_t align_B,
                                           enum nvkmd_mem_flags flags, enum nvkmd_mem_map_flags map_flags, struct nvkmd_mem **mem_out)
{
   (void)dev; (void)log_obj; (void)align_B;
   n_alloc++;
   alloc_req = (unsigned)flags;
   if (size_B == 0 || size_B > 0x10000 || !(map_flags & NVKMD_MEM_MAP_RD)) bad_args++;
   if (alloc_res != VK_SUCCESS) return alloc_res;
   the_mem.flags = (enum nvkmd_mem_flags)(flags | NVKMD_MEM_CAN_MAP | mark);
   the_mem.map = page;
   *mem_out = &the_mem;
   return VK_SUCCESS;
}
static void nvkmd_mem_unref(struct nvkmd_mem *mem) { if (mem == &the_mem) n_unref++; else bad_args++; }
/* ---- the patch's text ---- */
%s
%s
/* ---- end ---- */
static sigjmp_buf jb;
static void on_fault(int sig) { (void)sig; siglongjmp(jb, 1); }
int main(int argc, char **argv)
{
   /* the mode, the mark the allocation comes back with, the allocation's result */
   if (argc != 4) return 2;
   mode = atoi(argv[1]); mark = (unsigned)strtoul(argv[2], NULL, 0); alloc_res = atoi(argv[3]);
   const long ps = sysconf(_SC_PAGESIZE);
   page = mmap(NULL, (size_t)ps, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
   if (page == MAP_FAILED) return 3;
   struct nvkmd_nvrm_pdev pdev = { { 0 } };
   struct nvkmd_nvrm_dev dev = { { &pdev.base }, NULL, NULL };
   struct vk_object_base log = { 0 };
   nvkmd_nvrm_dev_bar_order_init(&dev, &log);
   const int has = dev.barOrder != NULL, right = !has || ((const void *)dev.barOrder == page && dev.barOrderMem == &the_mem);
   const int kept = dev.barOrderMem != NULL;
   /* Does the read before a doorbell load from the page? Make the page unreadable: a load faults, nothing else does. */
   if (mprotect(page, (size_t)ps, PROT_NONE) != 0) return 3;
   signal(SIGSEGV, on_fault); signal(SIGBUS, on_fault);
   int touched = 0, null_ok = 0;
   if (sigsetjmp(jb, 1) == 0) nvkmd_nvrm_dev_bar_order(&dev.base); else touched = 1;
   if (sigsetjmp(jb, 1) == 0) { nvkmd_nvrm_dev_bar_order(NULL); null_ok = 1; }
   printf("page=%%d right=%%d kept=%%d modes=%%d allocs=%%d req=%%#x unrefs=%%d touched=%%d null=%%d args=%%s\n", has, right, kept, n_mode,
          n_alloc, alloc_req, n_unref, touched, null_ok, bad_args ? "bad" : "ok");
   return 0;
}
'''
E_OOM_O = -2
ORDER_CASES = [
    # (what the arm holds, the mode, the mark of what comes back, the allocation's result, what comes out)
    ("in the default mode the device asks once for plain LOCAL memory (not marked read-back or shared, which the default "
     "would leave in system RAM), keeps the page the default placed behind BAR1, and the read before a doorbell loads from it",
     1, "DBAR", 0, f"page=1 right=1 kept=1 modes=1 allocs=1 req={{LOCAL:#x}} unrefs=0 touched=1 null=1 args=ok"),
    ("a page that came back without the default's mark (no room in the VRAM or in BAR1: it is in system RAM) is let go "
     "once, and there is no read",
     1, "0", 0, f"page=0 right=1 kept=0 modes=1 allocs=1 req={{LOCAL:#x}} unrefs=1 touched=0 null=1 args=ok"),
    ("a refused allocation leaves the device without a page and without a read, nothing let go",
     1, "DBAR", E_OOM_O, f"page=0 right=1 kept=0 modes=1 allocs=1 req={{LOCAL:#x}} unrefs=0 touched=0 null=1 args=ok"),
    ("with NVK_LOCAL_MAP_SYSMEM=1, in WindowServer or with a small BAR1 (system RAM) nothing is allocated and nothing read",
     0, "DBAR", 0, "page=0 right=1 kept=0 modes=1 allocs=0 req=0 unrefs=0 touched=0 null=1 args=ok"),
    ("with NVK_LOCAL_MAP_SYSMEM=0 (USERD itself is behind BAR1, as in 3.0) nothing is allocated and nothing read",
     2, "DBAR", 0, "page=0 right=1 kept=0 modes=1 allocs=0 req=0 unrefs=0 touched=0 null=1 args=ok"),
]


def run_order(init_text, read_text, d, tag):
    """[(what, got, want)] of every case, or a string."""
    err, exe = build(ORDER % (DEFINES, read_text, init_text), d, tag)
    if err:
        return err
    out = []
    for what, m, mk, ar, want in ORDER_CASES:
        o = subprocess.run([str(exe), str(m), hex(DBAR) if mk == "DBAR" else "0", str(ar)], capture_output=True, text=True, timeout=30)
        out.append((what, f"exit {o.returncode} " + o.stdout.strip() if o.returncode else o.stdout.strip(), want.format(LOCAL=LOCAL)))
    return out


cc = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
if not cc or not code or not block or not mapped or not order_init or not order_read or len(defs) != 2:
    found = bool(code and block and mapped and order_init and order_read and len(defs) == 2)
    arm("the patch's code to compile was found", found)
    print("SKIP: no C compiler on PATH: the rule, the placement, the map and the order page were not compiled" if found else "FAILED")
    sys.exit(2 if found and not fails else 1)
with tempfile.TemporaryDirectory() as tmp:
    d = pathlib.Path(tmp)
    res = run_matrix(code, d, "rule")
    bad = problems(res)
    arm(f"the rule holds in all {len(LMS) * len(BARV) * len(PROG) * len(SIZES)} combinations, each setting read once", not bad, bad[:3])
    if not isinstance(res, str):
        arm("default in an app: video memory for unshared memory the CPU does not read back, system RAM for shared",
            res[("(unset)", "(unset)", "java", GB8)][0] == 1 and res[("(unset)", "(unset)", "java", GB8)][5:] == (1, 0, 0, 0))
        arm("default in an app: memory the CPU reads back (NVKMD_MEM_HOST_READ) keeps system RAM",
            res[("(unset)", "(unset)", "java", GB8)][7] == 0)
        arm("default in WindowServer: system RAM", res[("(unset)", "(unset)", "WindowServer", GB8)][0] == 0)
        arm("a process with no name counts as an app", res[("(unset)", "(unset)", "(unset)", GB8)][0] == 1)
        arm("NVK_LOCAL_MAP_SYSMEM=1 is the way back in every process",
            all(res[("1", b, p, GB8)][0] == 0 for b in BARV for p in PROG))
        arm("NVK_LOCAL_MAP_SYSMEM=0 still means video memory everywhere, WindowServer, shared and read-back memory included",
            all(res[("0", b, p, GB8)][0] == 2 and res[("0", b, p, GB8)][5:] == (1, 1, 1, 1) for b in BARV for p in PROG))
        arm("NVK_BAR_VRAM=0 keeps the new default off", res[("(unset)", "0", "java", GB8)][0] == 0)
        arm("a BAR1 smaller than the VRAM moves nothing, whatever is set",
            all(res[(l, b, p, SIZES[2])][0] == 0 for l in LMS for b in BARV for p in PROG))
    muts = [("forgets WindowServer", "(window_server || bar_vram_off)", "(bar_vram_off)"),
            ("forgets shared memory", "(NVKMD_MEM_SHARED | NVKMD_MEM_HOST_READ)", "(NVKMD_MEM_HOST_READ)"),
            ("forgets memory the CPU reads back", "(NVKMD_MEM_SHARED | NVKMD_MEM_HOST_READ)", "(NVKMD_MEM_SHARED)"),
            ("forgets NVK_BAR_VRAM=0", "(window_server || bar_vram_off)", "(window_server)")]
    for i, (what, old, new) in enumerate(muts):
        arm(f"control: a rule that {what} fails", code.count(old) == 1 and caught(run_matrix(code.replace(old, new), d, f"mut{i}"), problems))

    pres = run_place(block, d, "place")
    bad = place_problems(pres)
    arm("the placement gives the flags and the mark the change describes, for each mode and all 128 combinations of "
        "LOCAL, CAN_MAP, BAR, SHARED, HOST_READ, DEFAULT_BAR passed in and force_gart", not bad, bad[:3])
    if not isinstance(pres, str):
        lcm = LOCAL | CAN_MAP
        arm("the default puts unshared LOCAL mapped memory behind BAR1 in video memory and marks it DEFAULT_BAR",
            placed_of(pres, 1, lcm) == (lcm | COHERENT | BAR | DBAR, 1))
        arm("under the default, shared or read-back LOCAL mapped memory goes to system RAM, unmarked",
            all(placed_of(pres, 1, lcm | x) == ((x | CAN_MAP | COHERENT | GART), 0) for x in (SHARED, HREAD, SHARED | HREAD)))
        arm("NVK_LOCAL_MAP_SYSMEM=0 puts it behind BAR1 without the mark, read-back and shared memory too",
            all(placed_of(pres, 2, lcm | x) == ((lcm | x | COHERENT | BAR), 0) for x in (0, SHARED, HREAD, SHARED | HREAD)))
        arm("what the caller put behind BAR1 itself is never marked DEFAULT_BAR, in any mode",
            all(placed_of(pres, m, lcm | BAR | x) == ((lcm | BAR | (x & ~DBAR) | COHERENT), 0) for m in range(3) for x in (0, DBAR, HREAD)))
        arm("a DEFAULT_BAR passed in is taken off (only the default sets it)",
            all(not (r[1] & DBAR) for (m, c), r in pres.items() if not r[2]))
        arm("with force_gart nothing is placed and mapped memory goes to system RAM",
            all(r[1] & GART and not r[1] & (LOCAL | VRAM) and not r[2] for (m, c), r in pres.items() if c >> 6))
    lm4 = [("undoes BAR on what the default placed (LM-4)", "   }\n   /* NVMTL batch 36",
            "   }\n   if (placed_by_default) flags &= ~NVKMD_MEM_BAR;\n   /* NVMTL batch 36"),
           ("takes LOCAL off before the rule (LM-4)", "bool placed_by_default = false;\n",
            "bool placed_by_default = false;\n   flags &= ~NVKMD_MEM_LOCAL;\n"),
           ("does not take an incoming DEFAULT_BAR off", "flags &= ~NVKMD_MEM_DEFAULT_BAR;\n", "\n"),
           ("marks memory NVK_LOCAL_MAP_SYSMEM=0 placed", "placed_by_default ? NVKMD_MEM_DEFAULT_BAR : 0",
            "NVKMD_MEM_DEFAULT_BAR"),
           ("keeps for the retry the request as the rule changed it", None, "flags_in"),
           ("applies the rule before force_gart", None, "force_gart")]
    for i, (what, old, new) in enumerate(lm4):
        if new == "flags_in":     # the request remembered after the rule's block instead of before it
            fi = "   const enum nvkmd_mem_flags flags_in = flags;\n"
            ok = block.count(fi) == 1 and block.count("   /* NVMTL batch 36") == 1
            mutated = block.replace(fi, "").replace("   /* NVMTL batch 36", fi + "   /* NVMTL batch 36")
        elif new == "force_gart":  # the FORCE_GART block moved below the rule's block
            fg = c_slice(block, "   if (dev->base.pdev->debug_flags & NVK_DEBUG_FORCE_GART) {", "\n   }\n")
            rb = c_slice(block, "   if ((flags & NVKMD_MEM_LOCAL) && (flags & NVKMD_MEM_CAN_MAP) && !(flags & NVKMD_MEM_BAR)) {",
                         "\n      }\n   }\n")
            ok = bool(fg) and bool(rb)
            mutated = block.replace(fg, "").replace(rb, rb + fg) if ok else block
        else:
            ok = block.count(old) == 1
            mutated = block.replace(old, new)
        arm(f"control: a placement that {what} fails", ok and caught(run_place(mutated, d, f"pmut{i}"), place_problems))

    mres = run_map(mapped, d, "map")
    for (what, got, want) in (mres if not isinstance(mres, str) else [("the map harness", mres, "")]):
        arm(what, got == want, f"{got} != {want}")
    mmuts = [("retries memory the default did not place", " && (mem->flags & NVKMD_MEM_DEFAULT_BAR)", ""),
             ("does not let the first memory go", "      nvkmd_mem_unref(mem);\n      result = nvkmd_dev_alloc_mem(",
              "      result = nvkmd_dev_alloc_mem("),
             ("keeps VRAM in the retry", "(flags & ~(NVKMD_MEM_LOCAL | NVKMD_MEM_VRAM))", "(flags & ~NVKMD_MEM_LOCAL)"),
             ("retries without CAN_MAP", "NVKMD_MEM_GART | NVKMD_MEM_CAN_MAP, &mem);", "NVKMD_MEM_GART, &mem);"),
             ("does not map the retried memory", "         return result;\n      result = nvkmd_mem_map(mem, log_obj, map_flags, NULL, NULL);\n",
              "         return result;\n"),
             ("lets the second memory go twice when its allocation fails",
              "      if (result != VK_SUCCESS)\n         return result;\n      result = nvkmd_mem_map(",
              "      if (result != VK_SUCCESS) {\n         nvkmd_mem_unref(mem);\n         return result;\n      }\n"
              "      result = nvkmd_mem_map(")]
    for i, (what, old, new) in enumerate(mmuts):
        arm(f"control: a map path that {what} fails",
            mapped.count(old) == 1 and caught(run_map(mapped.replace(old, new), d, f"mmut{i}"), map_problems))

    ores = run_order(order_init, order_read, d, "order")
    for (what, got, want) in (ores if not isinstance(ores, str) else [("the order page harness", ores, "")]):
        arm(what, got == want, f"{got} != {want}")
    omuts = [("is made in every mode", order_init, "if (nvrm_local_map_mode(pdev) != NVRM_LOCAL_MAP_VRAM_PRIVATE)",
              "if (nvrm_local_map_mode(pdev) < 0)", None),
             ("keeps a page that is not behind BAR1", order_init, "if (!(mem->flags & NVKMD_MEM_DEFAULT_BAR)) {",
              "if (mem == NULL) {", None),
             ("asks for the page as memory the CPU reads back", order_init, "0x1000, 0, NVKMD_MEM_LOCAL,",
              "0x1000, 0, NVKMD_MEM_LOCAL | NVKMD_MEM_HOST_READ,", None),
             ("keeps the memory but not its address", order_init, "   dev->barOrder = mem->map;\n", "", None),
             ("does not load from the page before a doorbell", None, "      (void)*p;\n", "      (void)p;\n", order_read)]
    for i, (what, init, old, new, read) in enumerate(omuts):
        src = read if read is not None else init
        ok = src.count(old) == 1
        res = run_order(init.replace(old, new) if init is not None else order_init,
                        read.replace(old, new) if read is not None else order_read, d, f"omut{i}")
        arm(f"control: an order page that {what} fails", ok and caught(res, map_problems))

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
arm("CHANGELOG.md has NVK_LOCAL_MAP_SYSMEM=1 in a row of 3.0.1's table of ways back, as a line of nvrm610.conf",
    re.search(r"(?m)^\|[^\n]*\| `NVK_LOCAL_MAP_SYSMEM=1` \|[^\n]*nvrm610\.conf[^\n]*\|\s*$", sec) is not None)
row = [l.split("\t") for l in (R / "tools/switches.tsv").read_text().split("\n") if l.startswith("env\tNVK_LOCAL_MAP_SYSMEM\t")]
arm("the registry has one row for it, a setting", len(row) == 1 and len(row[0]) == 7 and row[0][2] == "setting", row)
tool = (R / "tools/nullmoth_settings.py").read_text()
offer = re.findall(r'S\("[^"]+", "[^"]+", "((?:[^"\\]|\\.)*)", "conf", "NVK_LOCAL_MAP_SYSMEM", "[^"]+",\s*"((?:[^"\\]|\\.)*)"', tool)
arm("the settings tool offers it once, its name and text the registry's purpose",
    len(row) == 1 and len(offer) == 1 and f"{offer[0][0]}: {offer[0][1]}" == row[0][-1], offer)

print("FAILED" if fails else "PASS")
sys.exit(1 if fails else 0)
