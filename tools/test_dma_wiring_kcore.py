#!/usr/bin/env python3
"""How NVRM.kext's start, stop, platform interface and page allocator use the DMA layer (kexts/NVRM/nvrm_dma.h).

The layer itself (nvrm_dma.cpp) has its own test. This one reads the callers:

- startGPU decides the DMA mode before the boot guard counts the start, declines (returns false, no strike) when the
  decision is "refuse" or could not be made, logs why before it detaches, and detaches on every later false return,
  because a start that returns false gets no stop();
- stop() detaches after rm_free_private_state;
- "nvSurfDma" is answered before the power-management admission of callPlatformFunction;
- the OS layer (os-xnu2.cpp) no longer defines the RM's three DMA functions, says so with NV_XNU_DMA_LAYER, and gives
  the layer its two helpers; host-contiguous memory is asked for only while a GPU remaps; registered user pages are
  held to the GPU's address width only when the GPU does not remap;
- the weak fallbacks in nv_xnu_stubs.cpp give the answers of 2.9 (no remapping, identity), so a kext without the layer
  behaves as 2.9 did.

Each rule has a control: the source changed so that it breaks that rule, and the rule must catch it.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NV = ROOT / "kexts/NVRM"
FILES = {k: (NV / k).read_text() for k in ("NVRM.cpp", "os-xnu2.cpp", "nv-xnu.h", "nv_xnu_stubs.cpp")}


def strip_comments(s):
    s = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), s, flags=re.S)
    return re.sub(r"//[^\n]*", "", s)


def body(src, sig):
    """From the line that starts with sig to the first closing brace at column 0."""
    at = src.find("\n" + sig)
    if at < 0:
        return ""
    end = src.find("\n}\n", at)
    return src[at:end + 3] if end >= 0 else ""


def rules(files):
    out = []
    nvrm = strip_comments(files["NVRM.cpp"])
    os2 = strip_comments(files["os-xnu2.cpp"])
    hdr = strip_comments(files["nv-xnu.h"])
    stubs = strip_comments(files["nv_xnu_stubs.cpp"])

    # ---- startGPU ----
    st = body(nvrm, "bool NVRM::startGPU(")
    att = st.find("nvrm_dma_attach(")
    guard = st.find("nvrm_guard_count_start();")
    layout = st.find("if (gNvLayoutFb == kNvLayoutRefused)")
    sup = st.find("super::start(provider)", max(guard, 0))
    if not (0 <= layout < att < guard):
        out.append("startGPU decides the DMA mode after the layout decline and before the boot guard counts the start")
    if "if (!fNv.dma_dev) fNv.dma_dev = nvrm_dma_attach(&fNv, OSDynamicCast(IOPCIDevice, provider));" not in st:
        out.append("the mode is decided once, for the provider's PCI device, and kept in nv->dma_dev")
    dec = st[att:guard] if 0 <= att < guard else ""
    m = re.search(r"if \(!fNv\.dma_dev \|\| nvrm_dma_mode_of\(&fNv\) == NVRM_DMA_REFUSE\) \{(.*?)\n    \}", dec, re.S)
    if not m:
        out.append("a refusal, or no record (out of memory), declines the start")
    else:
        d = m.group(1)
        why, det, ret = d.find("nvrm_dma_why(&fNv)"), d.find("nvrm_dma_detach(&fNv);"), d.find("return false;")
        if not (0 <= why < det < ret):
            out.append("the decline logs why before it detaches, then returns false")
        if "nvrm_guard" in d:
            out.append("the decline is not a boot-guard strike")
    after = st[att:] if att >= 0 else ""
    falses = [mm.start() for mm in re.finditer(r"return false;", after)]
    for f in falses:
        line_start = after.rfind("\n", 0, f)
        stmt = after[max(0, line_start - 400):f]
        last = stmt.rfind("nvrm_dma_detach(&fNv);")
        if last < 0 or "return" in stmt[last + len("nvrm_dma_detach(&fNv);"):]:
            out.append("every false return of startGPU after the decision detaches first (line: %s)"
                       % after[line_start + 1:f + 13].strip()[:90])
            break
    if not (0 <= sup):
        out.append("startGPU still calls super::start after the guard count")

    # ---- stop ----
    sp = body(nvrm, "void NVRM::stop(")
    if not (0 <= sp.find("rm_free_private_state(") < sp.find("nvrm_dma_detach(&fNv);")):
        out.append("stop() detaches after rm_free_private_state")

    # ---- callPlatformFunction ----
    cp = body(nvrm, "IOReturn NVRM::callPlatformFunction(")
    dma = cp.find("fn->isEqualTo(NVRM_SS_FN_DMA)")
    pm = cp.find("NvXnuPmUse pmUse(rmCall);")
    if not (0 <= dma < pm):
        out.append("nvSurfDma is answered before the power-management admission")
    elif "nvrm_dma_surf((struct NVRMSurfDma *)p1, &fNv) ? kIOReturnIOError : kIOReturnSuccess" not in cp[dma:pm]:
        out.append("nvSurfDma passes the request and this GPU's state to nvrm_dma_surf")
    elif "NvXnuPmUse" in cp[dma:pm] or "nv_xnu_pm_try_enter" in cp[dma:pm]:
        out.append("nvSurfDma does not take the power-management admission")

    # ---- OS layer ----
    if not re.search(r"#define NV_XNU_DMA_LAYER 1\b", hdr):
        out.append("nv-xnu.h says the OS layer has handed the RM's DMA interface to nvrm_dma.cpp")
    for fn in ("nv_requires_dma_remap", "nv_dma_map_alloc", "nv_dma_unmap_alloc"):
        if re.search(r"NV_API_CALL %s\s*\([^;]*\)\s*\{" % fn, os2):
            out.append("os-xnu2.cpp does not define " + fn)
    if not re.search(r'extern "C" unsigned nv_xnu_dma_bits_now\(void\) \{ return nv_xnu_dma_bits; \}', os2):
        out.append("os-xnu2.cpp gives nv_xnu_dma_bits_now the width the RM set")
    src = body(os2, "IOMemoryDescriptor *nv_xnu_dma_source(")
    lk, walk, unl = src.find("IOSimpleLockLock(nv_pm_lock);"), src.find("for (nv_xnu_alloc *x = nv_pm_head;"), src.find("IOSimpleLockUnlock(nv_pm_lock);")
    use = src.find("md = a->md;")
    if not (0 <= lk < walk < use < unl) or "listed && !a->freed && a->kind == kNvAllocKindRm" not in src or "md->retain();" not in src[use:unl]:
        out.append("nv_xnu_dma_source finds an RM allocation on the live list, under its lock, before it uses it, and retains it")
    if "r->pin->magic != kNvUpinMagic" not in src or "r->a.kind != kNvAllocKindUser" not in src:
        out.append("nv_xnu_dma_source takes registered pages only with both their marks")
    ap = body(os2, "NV_STATUS NV_API_CALL nv_alloc_pages(")
    if "if ((opts & kIOMemoryPhysicallyContiguous) && nvrm_dma_any_remap()) opts |= kIOMemoryHostPhysicallyContiguous;" not in ap \
            or ap.count("kIOMemoryHostPhysicallyContiguous") != 1:
        out.append("nv_alloc_pages asks for host-contiguous memory only while a GPU remaps")
    if "a->kind = kNvAllocKindRm;" not in ap or "a->dma32 = (nv && nv->force_dma32_alloc)" not in ap:
        out.append("nv_alloc_pages marks its allocations and records the RM's below-4 GB request")
    rp = body(os2, "NV_STATUS NV_API_CALL nv_register_user_pages(")
    if "const bool remap = nv_requires_dma_remap(nv);" not in rp or "(!remap && ph + PAGE_SIZE - 1 > lim)" not in rp:
        out.append("nv_register_user_pages holds the pages to the address width only when the GPU does not remap")
    if "r->a.kind = kNvAllocKindUser;" not in rp:
        out.append("nv_register_user_pages marks its records")
    am = body(os2, 'extern "C" IOMemoryDescriptor *nv_xnu_alloc_md(')
    if not ("nv_pm_is_live(a)" in am and "md = a->md; md->retain();" in am):
        out.append("nv_xnu_alloc_md still hands out a retained descriptor of a live allocation (its pages live while it does)")

    # ---- weak fallbacks: the 2.9 answers ----
    def weak(fn):
        mm = re.search(r"__attribute__\(\(weak\)\)[^\n]*\b%s\([^)]*\)\s*\{(.*?)\n\}" % fn, stubs, re.S)
        return mm.group(1) if mm else None
    w = weak("nv_requires_dma_remap")
    if w is None or "return NV_FALSE;" not in w:
        out.append("without the layer, nv_requires_dma_remap answers false")
    w = weak("nv_dma_map_alloc")
    if w is None or "if (priv) *priv = NULL;" not in w or "return NV_OK;" not in w:
        out.append("without the layer, nv_dma_map_alloc gives the identity answer")
    w = weak("nv_dma_unmap_alloc")
    if w is None or "return NV_OK;" not in w:
        out.append("without the layer, nv_dma_unmap_alloc succeeds")
    return out


RULES = [
    "startGPU decides the DMA mode after the layout decline and before the boot guard counts the start",
    "the mode is decided once, for the provider's PCI device, and kept in nv->dma_dev",
    "a refusal, or no record (out of memory), declines the start",
    "the decline logs why before it detaches, then returns false",
    "the decline is not a boot-guard strike",
    "every false return of startGPU after the decision detaches first",
    "stop() detaches after rm_free_private_state",
    "nvSurfDma is answered before the power-management admission",
    "nvSurfDma passes the request and this GPU's state to nvrm_dma_surf",
    "nvSurfDma does not take the power-management admission",
    "nv-xnu.h says the OS layer has handed the RM's DMA interface to nvrm_dma.cpp",
    "os-xnu2.cpp does not define nv_requires_dma_remap",
    "os-xnu2.cpp does not define nv_dma_map_alloc",
    "os-xnu2.cpp does not define nv_dma_unmap_alloc",
    "os-xnu2.cpp gives nv_xnu_dma_bits_now the width the RM set",
    "nv_xnu_dma_source finds an RM allocation on the live list, under its lock, before it uses it, and retains it",
    "nv_xnu_dma_source takes registered pages only with both their marks",
    "nv_alloc_pages asks for host-contiguous memory only while a GPU remaps",
    "nv_alloc_pages marks its allocations and records the RM's below-4 GB request",
    "nv_register_user_pages holds the pages to the address width only when the GPU does not remap",
    "nv_register_user_pages marks its records",
    "nv_xnu_alloc_md still hands out a retained descriptor of a live allocation (its pages live while it does)",
    "without the layer, nv_requires_dma_remap answers false",
    "without the layer, nv_dma_map_alloc gives the identity answer",
    "without the layer, nv_dma_unmap_alloc succeeds",
]

failures = []


def arm(name, ok):
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        failures.append(name)


broken = rules(FILES)
for r in RULES:
    arm(r, not any(b.startswith(r) for b in broken))
for b in broken:
    if not any(b.startswith(r) for r in RULES):
        arm(b, False)

CONTROLS = [
    ("control: the mode decided after the guard count", "NVRM.cpp",
     lambda s: s.replace("    nvrm_guard_count_start();\n", "", 1).replace(
         "    if (!fNv.dma_dev) fNv.dma_dev = nvrm_dma_attach(", "    nvrm_guard_count_start();\n    if (!fNv.dma_dev) fNv.dma_dev = nvrm_dma_attach(", 1),
     "startGPU decides the DMA mode after the layout decline and before the boot guard counts the start"),
    ("control: out of memory carries on", "NVRM.cpp",
     lambda s: s.replace("if (!fNv.dma_dev || nvrm_dma_mode_of(&fNv) == NVRM_DMA_REFUSE) {", "if (nvrm_dma_mode_of(&fNv) == NVRM_DMA_REFUSE) {", 1),
     "a refusal, or no record (out of memory), declines the start"),
    ("control: the reason read after the detach", "NVRM.cpp",
     lambda s: re.sub(r'(        LOG\("NOT STARTING THE GPU: %s\. [^\n]*\n)(        nvrm_dma_detach\(&fNv\);\n)', r"\2\1", s, count=1),
     "the decline logs why before it detaches, then returns false"),
    ("control: a false return that leaves the record behind", "NVRM.cpp",
     lambda s: s.replace('if (!gNvClientsLock) { LOG("IOLockAlloc FAILED"); nvrm_dma_detach(&fNv); return false; }',
                         'if (!gNvClientsLock) { LOG("IOLockAlloc FAILED"); return false; }', 1),
     "every false return of startGPU after the decision detaches first"),
    ("control: nvSurfDma behind the admission", "NVRM.cpp",
     lambda s: s.replace("    if (fn && fn->isEqualTo(NVRM_SS_FN_DMA)) {", "    if (false) {", 1).replace(
         "        if (fn->isEqualTo(NVRM_SS_FN_RM))\n", "        if (fn->isEqualTo(NVRM_SS_FN_DMA)) return nvrm_dma_surf((struct NVRMSurfDma *)p1, &fNv) ? kIOReturnIOError : kIOReturnSuccess;\n        if (fn->isEqualTo(NVRM_SS_FN_RM))\n", 1),
     "nvSurfDma is answered before the power-management admission"),
    ("control: the OS layer still answers nv_requires_dma_remap", "os-xnu2.cpp",
     lambda s: s.replace("static NvU32 nv_xnu_dma_bits = 64;", "NvBool NV_API_CALL nv_requires_dma_remap(nv_state_t *nv) { TRC; return NV_FALSE; }\nstatic NvU32 nv_xnu_dma_bits = 64;", 1),
     "os-xnu2.cpp does not define nv_requires_dma_remap"),
    ("control: host-contiguous memory always", "os-xnu2.cpp",
     lambda s: s.replace("&& nvrm_dma_any_remap()) opts |= kIOMemoryHostPhysicallyContiguous;", ") opts |= kIOMemoryHostPhysicallyContiguous;", 1),
     "nv_alloc_pages asks for host-contiguous memory only while a GPU remaps"),
    ("control: the address width dropped for every GPU", "os-xnu2.cpp",
     lambda s: s.replace("(!remap && ph + PAGE_SIZE - 1 > lim)", "(false)", 1),
     "nv_register_user_pages holds the pages to the address width only when the GPU does not remap"),
    ("control: the allocation used before it is found on the list", "os-xnu2.cpp",
     lambda s: s.replace("if (listed && !a->freed && a->kind == kNvAllocKindRm && a->md)", "if (a->md)", 1),
     "nv_xnu_dma_source finds an RM allocation on the live list, under its lock, before it uses it, and retains it"),
    ("control: the weak map refuses (as a stub)", "nv_xnu_stubs.cpp",
     lambda s: s.replace("    NV_XNU_STUB_HIT(\"nv_dma_map_alloc\");\n    if (priv) *priv = NULL;\n    return NV_OK;", "    NV_XNU_STUB_HIT(\"nv_dma_map_alloc\");\n    return NV_ERR_NOT_SUPPORTED;", 1),
     "without the layer, nv_dma_map_alloc gives the identity answer"),
    ("control: the hand-over not announced", "nv-xnu.h",
     lambda s: s.replace("#define NV_XNU_DMA_LAYER 1", "", 1),
     "nv-xnu.h says the OS layer has handed the RM's DMA interface to nvrm_dma.cpp"),
]
for name, fname, mut, rule in CONTROLS:
    m = dict(FILES)
    m[fname] = mut(FILES[fname])
    changed = m[fname] != FILES[fname]
    arm(name, changed and any(b.startswith(rule) for b in rules(m)))

if failures:
    print("FAILED: " + "; ".join(failures))
    sys.exit(1)
print("PASS")
