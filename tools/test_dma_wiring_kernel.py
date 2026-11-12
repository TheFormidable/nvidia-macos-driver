#!/usr/bin/env python3
"""The DMA layer (kexts/NVRM/nvrm_dma.cpp, nvrm_dma_policy.h) and the page-off check in nvrm_surfshare.cpp are wired the
way the rest of the driver relies on: without a DMA mapper every entry point returns before doing anything 2.9 did not
do; with one, no raw page list reaches the copy engine, every mapping holds its own reference on the pages it maps and
is released only after it was unlinked, and the RM's DMA interface is defined exactly once. Also builds and runs
tools/test_dma_policy.c (the policy itself).

The calls into this layer from NVRM.cpp and os-xnu2.cpp (attach before the RM's private state, detach after it, the
"nvSurfDma" dispatch, the allocator's host-contiguous option) are the OS layer's and are tested with it.

   python3 tools/test_dma_wiring_kernel.py [tree-root]    (default: this tree)

Each arm also runs on a made-up source that breaks it (the controls at the end) and must fail there."""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
fails = []


def arm(name, ok):
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        fails.append(name)


def rd(p):
    f = ROOT / p
    return f.read_text(encoding="utf-8", errors="replace") if f.exists() else ""


def strip_comments(src):
    """C/C++ text without comments; string literals kept (the arms look for some)."""
    out, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if c == '"' or c == "'":
            j = i + 1
            while j < n and src[j] != c:
                j += 2 if src[j] == "\\" else 1
            out.append(src[i:j + 1]); i = j + 1
        elif src.startswith("//", i):
            j = src.find("\n", i); i = n if j < 0 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2); i = n if j < 0 else j + 2
        else:
            out.append(c); i += 1
    return "".join(out)


def body(src, name):
    """The body of the function DEFINED as `name(` (a line that does not end in ';' before its '{'), braces matched;
    '' when there is none."""
    for m in re.finditer(r"\b" + re.escape(name) + r"\s*\(", src):
        j = m.end(); depth = 1
        while j < len(src) and depth:
            depth += {"(": 1, ")": -1}.get(src[j], 0); j += 1
        k = j
        while k < len(src) and src[k] in " \t\r\n":
            k += 1
        if k < len(src) and src[k] == "{":
            depth, e = 0, k
            while e < len(src):
                depth += {"{": 1, "}": -1}.get(src[e], 0)
                e += 1
                if depth == 0:
                    return src[k:e]
    return ""


def before(text, first, *later):
    """`first` occurs, and before every occurrence of each of `later` that occurs at all."""
    a = text.find(first)
    if a < 0:
        return False
    return all(text.find(x) < 0 or a < text.find(x) for x in later)


# ---------------------------------------------------------------- the checks, each a function of the sources it reads

def chk_pageoff(ss):
    b = body(strip_comments(ss), "nvrm_surfshare_pageoff")
    m = re.search(r"if \(!nvrm_dma_pages_ok\(([^;]*?)\)\)\s*\{\s*p->status = NVRM_SS_NO_DMA;\s*return 0;\s*\}", b)
    return bool(m) and "nvrm_ce_copy_to_pages(" in b and b.find(m.group(0)) < b.find("nvrm_ce_copy_to_pages(") \
        and b.count("nvrm_ce_copy_to_pages(") == 1 and '#include "nvrm_dma.h"' in ss


def chk_sysctls_readonly(dma):
    s = strip_comments(dma)
    decls = re.findall(r"SYSCTL_[A-Z0-9_]+\s*\(([^;]*)\);", s)
    return len(decls) >= 21 and all("CTLFLAG_RD" in d and "CTLFLAG_RW" not in d and "CTLFLAG_WR" not in d
                                    and "CTLFLAG_ANYBODY" not in d for d in decls) \
        and all(x.startswith("nvrm_dma_") for x in re.findall(r"SYSCTL_[A-Z0-9_]+\s*\(\s*_debug\s*,\s*OID_AUTO\s*,\s*(\w+)", s))


def chk_bootarg_once(sources):
    hits = [(p, n) for p, src in sources.items() for n in [len(re.findall(r'PE_parse_boot_argn\s*\(\s*"nvrmdma"', strip_comments(src)))] if n]
    return hits == [("kexts/NVRM/nvrm_dma.cpp", 1)]


# A function's mode-0 return comes before the first thing 2.9 did not do there.
NEW_WORK = ("dmaGet(", "IOLockLock(", "dmaMap(", "nv_xnu_dma_source(", "dmaTake(", "IOMalloc(", "DLOG(")


def chk_mode0_first(dma, fn, guard, work=NEW_WORK):
    b = body(strip_comments(dma), fn)
    return bool(b) and guard in b and before(b, guard, *work)


def chk_selftest_only_with_mapper(dma):
    b = body(strip_comments(dma), "nvrm_dma_attach")
    calls = re.findall(r"[^\n]*dmaSelfTest\(", b)
    return len(calls) == 1 and "nvrm_dma_remaps(mode) && DMA_RM_ROUTED" in calls[0] \
        and before(b, "nvrm_dma_decide(", "dmaSelfTest(") and before(b, "dmaSelfTest(", "nvrm_dma_settle(")


def chk_own_reference(dma):
    s = strip_comments(dma)
    m, r = body(s, "dmaMap"), body(s, "dmaRelease")
    return before(m, "md->retain();", "md->prepare()", "IODMACommand::withSpecification(") \
        and before(m, "md->prepare()", "IODMACommand::withSpecification(") \
        and before(r, "m->cmd->complete(", "m->md->complete();") and before(r, "m->md->complete();", "m->md->release();")


def chk_unlink_before_release(dma):
    """Every function that releases a live mapping (dmaUnmap) took it off the list first (dmaTake), and nothing else
    frees a linked one."""
    s = strip_comments(dma)
    names = set(re.findall(r"^[\w \*:]*?\b(\w+)\s*\([^;{]*\)\s*\{", s, re.M))
    ok = True
    for fn in names:
        b = body(s, fn)
        if fn != "dmaUnmap" and "dmaUnmap(" in b:
            ok = ok and before(b, "dmaTake(", "dmaUnmap(")
    unmap = body(s, "dmaUnmap")
    return ok and "dmaFree(" in unmap and "gDmaHead" not in unmap and body(s, "dmaTake").count("IOLockLock(") == 1


def chk_rm_interface(dma):
    s = strip_comments(dma)
    blocks = list(re.finditer(r"#if DMA_RM_ROUTED\b(.*?)(?:#else(.*?))?#endif", s, re.S))
    if not blocks or "weak" in s:
        return False
    g = "".join(b.group(1) for b in blocks)
    rest = s
    for b in blocks:
        rest = rest.replace(b.group(0), b.group(2) or "")
    three = ("nv_requires_dma_remap", "nv_dma_map_alloc", "nv_dma_unmap_alloc")
    others = ("nv_dma_map_peer", "nv_dma_unmap_peer", "nv_dma_map_mmio", "nv_dma_unmap_mmio", "nv_dma_cache_invalidate")
    return all(body(g, f) and not body(rest, f) for f in three) and all(body(rest, f) for f in others) \
        and "return NV_ERR_NOT_SUPPORTED;" in body(rest, "nv_dma_map_peer") and "return NV_ERR_NOT_SUPPORTED;" in body(rest, "nv_dma_map_mmio") \
        and "#ifdef NV_XNU_DMA_LAYER\n#define DMA_RM_ROUTED 1\n#else\n#define DMA_RM_ROUTED 0\n#endif" in s \
        and not re.search(r"[^\w](nv_xnu_dma_source|nv_xnu_dma_bits_now)\(", rest)


def defines(src, fn):
    """A strong definition of fn: a line that starts with a plain return type (a weak attribute has parentheses and
    does not count) and names fn, its parameter list, then the opening brace."""
    s = strip_comments(src)
    return re.search(r'^[ \t]*(?:extern "C"[ \t]+)?[\w \t*]+?\b' + fn + r'\s*\([^(){};]*\)\s*\{', s, re.M) is not None


def chk_one_definition(xnu_h, os2):
    """The OS layer and this file never both define the RM's DMA interface (the kext would not link), and never
    neither (the RM would get the weak stubs: no remapping with a mapper)."""
    gate = re.search(r"^\s*#define\s+NV_XNU_DMA_LAYER\b", strip_comments(xnu_h), re.M) is not None
    os_defs = [defines(os2, f) for f in ("nv_requires_dma_remap", "nv_dma_map_alloc", "nv_dma_unmap_alloc")]
    helpers = [defines(os2, f) for f in ("nv_xnu_dma_source", "nv_xnu_dma_bits_now")]
    return (gate and not any(os_defs) and all(helpers)) or (not gate and all(os_defs))


def chk_policy_header(pol):
    s = strip_comments(pol)
    return "enum nvrm_dma_mode {" not in s and '#include "nvrm_surfshare_abi.h"' in s \
        and not re.search(r"#include\s*<", s) and "#define NVRM_SS_KERNEL_ABI 1" in s


def chk_pages_ok_rule(dma):
    b = body(strip_comments(dma), "nvrm_dma_pages_ok")
    return "m->kind == kKindSurf && m->dev == d && m->runs == 1 && nvrm_dma_pages_inside(pages, n, m->base, m->bytes)" in b \
        and "nvrm_dma_pageoff_allowed(d->mode, inside)" in b


def chk_token_is_serial(dma):
    b = body(strip_comments(dma), "nvrm_dma_surf")
    return "q->token = (void *)(uintptr_t)m->serial;" in b and "dmaTake(NULL, serial, d, 1u << kKindSurf)" in b \
        and "q->token = m;" not in b


def chk_detach_drains(dma):
    b = body(strip_comments(dma), "nvrm_dma_detach")
    return before(b, "d->dead = 1;", "dmaTake(", "mp->release()") and "IOSleep(1)" in b and "kDmaDrainMs" in b \
        and "IOFree(" not in b and "kDevRetired" in b


# ---------------------------------------------------------------- the tree

dma, ss = rd("kexts/NVRM/nvrm_dma.cpp"), rd("kexts/NVRM/nvrm_surfshare.cpp")
pol, xnu_h, os2 = rd("kexts/NVRM/nvrm_dma_policy.h"), rd("kexts/NVRM/nv-xnu.h"), rd("kexts/NVRM/os-xnu2.cpp")
kext_sources = {str(p.relative_to(ROOT)): p.read_text(encoding="utf-8", errors="replace")
                for p in sorted((ROOT / "kexts").rglob("*")) if p.suffix in (".c", ".cpp", ".h", ".inc", ".m", ".mm")}

arm("the page-off copy asks nvrm_dma_pages_ok first and answers NVRM_SS_NO_DMA without copying", chk_pageoff(ss))
arm("the page-off check takes only pages inside a page-off mapping of the same GPU, in one run", chk_pages_ok_rule(dma))
arm("every debug.nvrm_dma_* sysctl is read-only", chk_sysctls_readonly(dma))
arm("boot-arg nvrmdma is read in exactly one place", chk_bootarg_once(kext_sources))
arm("mode 0: nv_dma_map_alloc returns the 2.9 answer before any new work",
    chk_mode0_first(dma, "nv_dma_map_alloc", "if (!d || !nvrm_dma_remaps(d->mode)) {"))
arm("mode 0: nv_dma_unmap_alloc returns before looking at what it was given",
    chk_mode0_first(dma, "nv_dma_unmap_alloc", "if (!d || !nvrm_dma_remaps(d->mode)) return NV_OK;"))
arm("mode 0: nvrm_dma_pages_ok answers yes before any lock or list",
    chk_mode0_first(dma, "nvrm_dma_pages_ok", "if (!d || d->mode == NVRM_DMA_NONE) return 1;"))
arm("mode 0: nvSurfDma MAP answers before anything is mapped",
    chk_mode0_first(dma, "nvrm_dma_surf", "if (q->op != NVRM_SS_DMA_MAP || !d || !nvrm_dma_remaps(d->mode) || !q->md || !q->length) return 0;",
                    ("dmaGet(", "dmaMap(", "OSDynamicCast(")))
arm("mode 0: nv_requires_dma_remap is false (no IO address space for the RM)",
    "return nvrm_dma_remaps((enum nvrm_dma_mode)nvrm_dma_mode_of(nv)) ? NV_TRUE : NV_FALSE;" in body(strip_comments(dma), "nv_requires_dma_remap"))
arm("the self-test mapping runs only when a mapper will be used, between the decision and the settling",
    chk_selftest_only_with_mapper(dma))
arm("a mapping holds its own reference and wire count, given back after the mapping (pages stay alive)", chk_own_reference(dma))
arm("a mapping is released only after it was taken off the list (exactly one release)", chk_unlink_before_release(dma))
arm("the accelerator's token is a serial number, never a pointer, and unmaps only a mapping of the same GPU",
    chk_token_is_serial(dma))
arm("a GPU's stop shuts new mappings out, waits a bounded time, drains its mappings and never frees a record",
    chk_detach_drains(dma))
arm("the RM's DMA interface: the three remapping calls only with the OS layer's consent, peer and MMIO refused",
    chk_rm_interface(dma))
arm("nv_requires_dma_remap, nv_dma_map_alloc and nv_dma_unmap_alloc are defined by exactly one of os-xnu2.cpp and nvrm_dma.cpp",
    chk_one_definition(xnu_h, os2))
arm("the policy header is pure and takes the modes from the shared interface header", chk_policy_header(pol))

# the policy host test itself
with tempfile.TemporaryDirectory() as td:
    exe = os.path.join(td, "test_dma_policy")
    cc = subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-I", str(ROOT / "kexts/NVRM"),
                         str(ROOT / "tools/test_dma_policy.c"), "-o", exe], capture_output=True, text=True)
    run = subprocess.run([exe], capture_output=True, text=True) if cc.returncode == 0 else None
    arm("tools/test_dma_policy.c builds and passes", cc.returncode == 0 and run is not None and run.returncode == 0
        and run.stdout.rstrip().endswith("PASS"))
    if cc.returncode != 0:
        print(cc.stderr[-2000:])
    elif run.returncode != 0:
        print(run.stdout[-2000:])

# ---------------------------------------------------------------- controls: each check refuses a source that breaks it

ctl = []


def control(name, refused):
    ctl.append(name)
    arm("control: " + name, refused)


control("the copy without the check", not chk_pageoff(ss.replace("if (!nvrm_dma_pages_ok(", "if (!(")))
control("the check after the copy", not chk_pageoff(
    "#include \"nvrm_dma.h\"\nextern \"C\" int nvrm_surfshare_pageoff(struct NVRMSurfPageoff *p, void *nv)\n{\n    p->status = nvrm_ce_copy_to_pages(nv, 0, 0, 0, 0, 0, 0);\n"
    "    if (!nvrm_dma_pages_ok((nv_state_t *)nv, 0, 0)) { p->status = NVRM_SS_NO_DMA; return 0; }\n    return 0;\n}\n"))
control("a raw list let through with a mapper", not chk_pages_ok_rule(dma.replace("m->kind == kKindSurf && ", "")))
control("a writable sysctl", not chk_sysctls_readonly(dma.replace(
    "nvrm_dma_leaked, CTLFLAG_RD | CTLFLAG_LOCKED", "nvrm_dma_leaked, CTLFLAG_RW | CTLFLAG_LOCKED")))
control("the boot-arg read twice", not chk_bootarg_once(dict(kext_sources, **{"kexts/NVRM/x.cpp": 'PE_parse_boot_argn("nvrmdma", &v, 4);'})))
control("new work before the mode-0 return", not chk_mode0_first(dma.replace(
    "    nv_dma_device *d = dmaDev(dev);\n    if (!d || !nvrm_dma_remaps(d->mode)) {",
    "    nv_dma_device *d = dmaDev(dev);\n    DLOG(\"map\");\n    if (!d || !nvrm_dma_remaps(d->mode)) {", 1),
    "nv_dma_map_alloc", "if (!d || !nvrm_dma_remaps(d->mode)) {"))
control("pages_ok locking before its mode-0 answer", not chk_mode0_first(
    "int nvrm_dma_pages_ok(nv_state_t *nv, const NvU64 *pages, NvU64 n)\n{\n    IOLockLock(l);\n    if (!d || d->mode == NVRM_DMA_NONE) return 1;\n}\n",
    "nvrm_dma_pages_ok", "if (!d || d->mode == NVRM_DMA_NONE) return 1;"))
control("a self-test in every mode", not chk_selftest_only_with_mapper(dma.replace(
    "if (nvrm_dma_remaps(mode) && DMA_RM_ROUTED) selftest = dmaSelfTest(d);", "selftest = dmaSelfTest(d);")))
control("a mapping that borrows the descriptor's reference", not chk_own_reference(dma.replace("        md->retain();\n", "")))
control("a release without an unlink", not chk_unlink_before_release(dma + "\nstatic void bad(nv_xnu_dmamap *m)\n{\n    dmaUnmap(m);\n}\n"))
control("a pointer handed out as the token", not chk_token_is_serial(dma.replace(
    "q->token = (void *)(uintptr_t)m->serial;", "q->token = m;")))
control("an UNMAP that takes another GPU's mapping", not chk_token_is_serial(dma.replace(
    "dmaTake(NULL, serial, d, 1u << kKindSurf)", "dmaTake(NULL, serial, NULL, 1u << kKindSurf)")))
control("a stop that frees the record", not chk_detach_drains(dma.replace("    if (mp) mp->release();", "    if (mp) mp->release();\n    IOFree(d, sizeof *d);")))
control("the remapping calls defined without the OS layer's consent", not chk_rm_interface(dma.replace("#if DMA_RM_ROUTED\n", "#if 1\n", 1)))
control("both define the interface (no link)", not chk_one_definition(
    "#define NV_XNU_DMA_LAYER 1\n", "NvBool NV_API_CALL nv_requires_dma_remap(nv_state_t *nv) { return NV_FALSE; }\n"
    "NV_STATUS NV_API_CALL nv_dma_map_alloc(nv_dma_device_t *dev, NvU64 a, NvU64 *b, NvBool c, NvBool d, void **p) { return 0; }\n"
    "NV_STATUS NV_API_CALL nv_dma_unmap_alloc(nv_dma_device_t *dev, NvU64 a, NvU64 *b, void **p) { return 0; }\n"
    "IOMemoryDescriptor *nv_xnu_dma_source(void *priv, NvU64 *b, int *f, int *u) { return 0; }\nunsigned nv_xnu_dma_bits_now(void) { return 64; }\n"))
control("neither defines it (the weak stubs would answer)", not chk_one_definition(
    "", "IOMemoryDescriptor *nv_xnu_dma_source(void *priv, NvU64 *b, int *f, int *u) { return 0; }\n"))
control("the OS layer handed over without its two helpers", not chk_one_definition("#define NV_XNU_DMA_LAYER 1\n", ""))
control("the policy header with its own mode enum", not chk_policy_header(pol + "\nenum nvrm_dma_mode { X };\n"))

print("FAILED: %d" % len(fails) if fails else "PASS")
sys.exit(1 if fails else 0)
