#!/usr/bin/env python3
"""kext_imports.py: what the kext objects import, and whether the kernel will resolve it when the kext loads.

A kext that calls a kernel function its KPIs do not export compiles and links, and then never loads: the kernel linker
refuses it on every machine, and the first anybody hears of it is a black screen. This reads the undefined symbols of
compiled kext objects and puts each one in a class, once per supported kernel. Two mistakes it exists for:

  thread_call_cancel_wait   is in xnu's Private.exports only. Apple's own kexts may link it; a third-party kext may not.
  PE_boot_args              is exported by macOS 26 and not by macOS 15: the kext would load on one and not the other.

Classes:
  kernel      exported to kexts by a KPI the kext's Info.plist names (xnu config/<KPI>.exports and the x86_64 ones)
  family      a symbol of a class of an IOKit family the Info.plist names (not provable without the family's binary)
  sibling     defined by another kext of this tree whose bundle the Info.plist names
  nvidia      defined, with external linkage, by the NVIDIA RM / NVKMS sources the kext links (libnvkernel.a,
              libnvkms.a), as far as their text shows: the archives are not built for this check. NVRM only
  linktime    provided by the compiler's kext runtime at link time
  allowed     listed in build/kext_imports_allow.txt with a reason
  UNRESOLVED  none of the above, or exported by the Private KPI only, or by a KPI the Info.plist does not name:
              the run fails

Two classes are read from text and say so. "family" goes by the class a C++ symbol belongs to (the first name of its
mangled form), never by a class among its parameters: NVRMThing::attach(IOPCIDevice *) is this tree's method, and one
that nothing defines is unresolved. "nvidia" needs a definition: `name(...) {` that is not static, a file-scope
variable, or NVIDIA's generated `#define name(...) name_IMPL(...)` over one. A name their sources only declare (the
os_* and nv_* functions the RM expects its OS layer, this kext, to define), call, or mention in a comment is not
theirs, and neither is floor() or free(): those are in the kernel's headers, in NVIDIA's text, and in no kernel.

Every kernel given is checked and an import has to resolve on each. The export lists come from --exports (a folder
holding xnu's config/*.exports; give it once per kernel), or from <work>/src/<tag>/config for each --xnu-tag, which is
fetched from apple-oss-distributions/xnu (config/ only, the tag checked against its pinned commit) when it is not there
and --offline is not given. With neither, the two kernels of XNU_TAGS are used.

  kext_imports.py --objects <dir> --repo <driver checkout> --ogkm <patched NVIDIA tree>
                  [--exports <xnu config dir>]... | [--xnu-tag <tag>]... --work <dir> [--offline]
                  [--nm "<tool>"] [--toolchain-atexit] [--verbose]
  kext_imports.py --selftest

build/kext_check.sh compiles the objects and calls this. It also takes objects compiled some other way, as long as
their file names say which kext they belong to (KEXTS below).
"""
import argparse
import plistlib
import re
import subprocess
import sys
from pathlib import Path

# macOS 15.0, the oldest system the kexts are built for, and macOS 26.0, the newest they run on
XNU_TAGS = ("xnu-11215.1.10", "xnu-12377.1.9")
XNU_SHA = {"xnu-11215.1.10": "8d741a5de7ff4191bf97d57b9f54c2f6d4a15585", "xnu-12377.1.9": "f6217f891ac0bb64f3d375211650a4c1ff8ca1ea"}
XNU_URL = "https://github.com/apple-oss-distributions/xnu"
# OSBundleLibraries name -> the export list. A third-party kext cannot link the Private KPI, whatever its plist says.
KPI = {"com.apple.kpi.bsd": "BSDKernel", "com.apple.kpi.iokit": "IOKit", "com.apple.kpi.libkern": "Libkern",
       "com.apple.kpi.mach": "Mach", "com.apple.kpi.unsupported": "Unsupported", "com.apple.kpi.dsep": "MACFramework"}
PRIVATE = "Private"
# class-name prefixes of the IOKit families, as build/check_kext_libraries.sh lists them, plus IOSurface
FAMILY = [("IOHIDDevice|IOHIDInterface|IOHIDEvent", ["com.apple.iokit.IOHIDFamily"]),
          ("IOAudio", ["com.apple.iokit.IOAudioFamily"]),
          ("IONDRV", ["com.apple.iokit.IONDRVSupport"]),
          ("IOACPIPlatform", ["com.apple.iokit.IOACPIFamily"]),
          ("IOPCIDevice|IOPCIBridge", ["com.apple.iokit.IOPCIFamily"]),
          ("IOFramebuffer|IODisplay|IOBacklight|IOI2C", ["com.apple.iokit.IOGraphicsFamily"]),
          ("IOAccel|IOGraphicsAccelerator", ["com.apple.iokit.IOAcceleratorFamily2", "com.apple.iokit.IOGraphicsFamily"]),
          ("IOSurface", ["com.apple.iokit.IOSurface"]),
          ("AppleGraphicsDeviceControl", ["com.apple.AppleGraphicsDeviceControl"])]
# C symbols (not classes) a family exports: name prefix -> the family
FAMILY_C = [("_gIODisplay", "com.apple.iokit.IOGraphicsFamily"), ("_IODisplay", "com.apple.iokit.IOGraphicsFamily"),
            ("_IOFramebuffer", "com.apple.iokit.IOGraphicsFamily"), ("_gIOFB", "com.apple.iokit.IOGraphicsFamily")]
# kext -> (object file names, Info.plist). build/kext_check.sh names every NVRM object nvrm-<source>.o; a harness that
# keeps the source's own name (nvrm_pageoff.o) is read as well.
KEXTS = {
    "NVRM": (r"^nvrm[-_].*\.o$", "kexts/NVRM/Info.plist"),
    "NVAccel": (r"^accel\.o$", "kexts/NVRM/accel/Info.plist"),
    "NVAccel (macOS 26)": (r"^accel-tahoe\.o$", "kexts/NVRM/accel/Info.plist"),
    "NVRMFB": (r"^fb\.o$", "kexts/NVRMFB/fb/Info.plist"),
    "NVRMAGDC": (r"^agdc\.o$", "kexts/NVRMAGDC/Info.plist"),
    "NMGopFB": (r"^gopfb\.o$", "kexts/NMGopFB/Info.plist"),
    "NMIntelFB": (r"^intelfb\.o$", "kexts/NMIntelFB/Info.plist"),
    "NMIntelFBVirtual": (r"^intelfb-virtual\.o$", "kexts/NMIntelFB/Info-virtual.plist"),
    "NMSMN": (r"^nmsmn\.o$", "kexts/NMSMN/Info.plist"),
    "NMDRM": (r"^nmdrm\.o$", "kexts/NMDRM/Info.plist"),
    "NVPrimarySelector": (r"^primary\.o$", "kexts/NVPrimarySelector/Info.plist"),
}
# symbols the compiler or the kext start-up objects (libkmod, libkmodc++, libcc_kext) provide at link time
LINKTIME = re.compile(r"^(___stack_chk_(fail|guard)|_kmod_info|__start|__stop|___cxa_pure_virtual|__realmain|__antimain|"
                      r"__kext_apple_cc|___bzero|___udivti3|___umodti3|___divti3|___modti3|_OSKextGetCurrentIdentifier|"
                      r"_OSKextGetCurrentLoadTag|_OSKextGetCurrentVersionString)$")
ATEXIT = ("___cxa_atexit", "___dso_handle")


def nm(tool, obj, flag):
    """-u: what the object leaves undefined. -gU: what it defines for other objects (a static function of one object
    does not resolve a call from another, so local symbols are left out)."""
    r = subprocess.run(tool.split() + [flag, str(obj)], capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"STOP: {tool} {flag} {obj}: {r.stderr.strip()[-300:]}")
    return {l.split()[-1] for l in r.stdout.splitlines() if l.strip() and not l.endswith(":")}


class Exports:
    """The export lists of one kernel: which KPI exports a symbol, exactly or by a name* prefix."""

    def __init__(self, label, exact=None, prefix=None):
        self.label, self.exact, self.prefix = label, exact or {}, prefix or []

    @classmethod
    def load(cls, cfg, label):
        e = cls(label)
        for name in list(KPI.values()) + [PRIVATE]:
            for suffix in ("exports", "x86_64.exports", "x86_64.MacOSX.exports"):
                p = cfg / f"{name}.{suffix}"
                if not p.is_file():
                    continue
                for line in p.read_text(errors="replace").splitlines():
                    s = re.sub(r"#.*", "", line).split(":")[0].strip()
                    if not s:
                        continue
                    if s.endswith("*"):
                        e.prefix.append((s[:-1], name))
                    else:
                        e.exact.setdefault(s, set()).add(name)
        return e

    def kpis(self, sym):
        return set(self.exact.get(sym, ())) | {name for pre, name in self.prefix if sym.startswith(pre)}


NV_SOURCES = ("src/nvidia", "src/nvidia-modeset", "src/common")
NV_COMMENT = re.compile(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'', re.S)
# name ( parameters ) {  - the parameters may hold two levels of parentheses (a function-pointer parameter)
NV_FUNC = re.compile(r"\b([A-Za-z_]\w*)\s*\((?:[^(){};]|\((?:[^(){};]|\([^(){};]*\))*\))*\)\s*\{")
NV_DATA = re.compile(r"(?m)^(?!extern\b|static\b|typedef\b|return\b|goto\b|case\b|else\b)[A-Za-z_][\w \t*]*?[ \t*]([A-Za-z_]\w*)[ \t]*(?:\[[^\]\n]*\][ \t]*)*[=;]")
NV_ALIAS = re.compile(r"(?m)^[ \t]*#[ \t]*define[ \t]+(\w+)\([^)\n]*\)[ \t]+(\w+)\(")
NV_NOT_A_NAME = {"if", "for", "while", "switch", "do", "else", "return", "sizeof"}


def nvidia_defined_in(text):
    """-> (names this source text defines with external linkage, [(macro, what it stands for)]).
    The RM writes `type\nname\n(\n  parameters\n)\n{`, so a definition is found by its shape, not by its line."""
    t = NV_COMMENT.sub(lambda m: " " if m.group(0)[0] == "/" else m.group(0), text)
    names = set()
    for m in NV_FUNC.finditer(t):
        n = m.group(1)
        if n in NV_NOT_A_NAME or n in names:
            continue
        line = t.rfind("\n", 0, m.start()) + 1
        before = t[t.rfind("\n", 0, line - 1) + 1:line - 1] if line else ""
        if t[line:m.start()].lstrip().startswith("#") or before.rstrip().endswith("\\"):
            continue                                        # a macro, or the continued line of one
        # the declaration specifiers: what stands between the previous ; { } and the name, preprocessor lines left out
        start = max(t.rfind(c, 0, m.start()) for c in ";{}") + 1
        spec, skip = [], False
        for l in t[start:m.start()].split("\n"):
            if skip or l.lstrip().startswith("#"):
                skip = l.rstrip().endswith("\\"); continue
            spec.append(l)
        spec = "\n".join(spec)
        if re.search(r"[A-Za-z_]\w*[\s*]*$", spec) and not re.search(r"\bstatic\b", spec):
            names.add(n)                                    # a return type stands before it, and it is not file-local
    names.update(m.group(1) for m in NV_DATA.finditer(t))
    return names, [(a, b) for a, b in NV_ALIAS.findall(t) if b.startswith(a + "_")]


def nvidia_defined(ogkm):
    """The functions and variables the RM / NVKMS sources define for other objects, by their text."""
    names, aliases = set(), []
    for sub in NV_SOURCES:
        for p in (ogkm / sub).rglob("*"):
            if p.suffix in (".c", ".cpp", ".h") and p.is_file():
                n, a = nvidia_defined_in(p.read_text(errors="replace"))
                names |= n; aliases += a
    # NVIDIA's generator writes `#define vidmemGetHeap(...) vidmemGetHeap_IMPL(...)` and the source defines the short name
    return names | {b for a, b in aliases if a in names}


def leading_class(sym):
    """The class a mangled C++ symbol belongs to: the first name of N...E, after a vtable / typeinfo / thunk / guard
    prefix. For a free function it is the function's own name. None when the symbol does not start with a name."""
    m = re.match(r"__Z(?:T[VTISC]|Th\w+?_|Tv\w+?_|GV|GR)?N?[rVK]*(\d+)", sym)
    if not m:
        return None
    n = int(m.group(1)); name = sym[m.end():m.end() + n]
    return name if len(name) == n else None


def classify(sym, exports, libs, nvidia, siblings, allow, atexit=False):
    """-> the class of one undefined symbol. libs is the kext's OSBundleLibraries (bundle identifiers), nvidia the
    names the RM / NVKMS sources define (empty for a kext that does not link them)."""
    named = {KPI[l] for l in libs if l in KPI}
    has = exports.kpis(sym)
    if has & named:
        return "kernel"
    if sym in ATEXIT:
        if atexit:
            return "toolchain"   # this clang's form of a global destructor; Apple's clang does not emit it for a kext
        return ("UNRESOLVED (the compiler lowered a global destructor to __cxa_atexit, which no kext may import: compile with "
                "-mllvm -disable-atexit-based-global-dtor-lowering as build/kext_check.sh does, or pass --toolchain-atexit)")
    if LINKTIME.match(sym):
        return "linktime"
    if sym in siblings:
        return "sibling"
    if sym in allow:
        return "allowed"
    if has == {PRIVATE}:
        return "UNRESOLVED (exported by the Private KPI only: Apple's own kexts may link it, this one would be refused at load)"
    if has - {PRIVATE}:
        return "UNRESOLVED (exported by %s, which the Info.plist does not name in OSBundleLibraries)" % ", ".join(sorted(has - {PRIVATE}))
    if sym.startswith("__Z"):
        cls = leading_class(sym)
        for pat, ids in FAMILY:
            if cls and re.match(r"(?:%s)" % pat, cls):
                return "family" if any(i in libs for i in ids) else "UNRESOLVED (family %s is not in OSBundleLibraries)" % ids[0]
    for pre, fam in FAMILY_C:
        if sym.startswith(pre):
            return "family" if fam in libs else "UNRESOLVED (family %s is not in OSBundleLibraries)" % fam
    if sym.startswith("_") and not sym.startswith("__Z") and sym[1:] in nvidia:
        return "nvidia"
    return "UNRESOLVED"


NVIDIA_HINT = ("  (NVRM links the RM and NVKMS. A C function counts as theirs when their sources define it: one they only declare, "
               "as they do the os_* and nv_* functions of the OS layer, is this kext's to define. One they do define, in a way "
               "the text does not show, goes in build/kext_imports_allow.txt with the file that defines it.)")


def check(kernels, groups, nvidia, allow, atexit=False, verbose=False, out=print):
    """groups: {kext: (undefined symbols, OSBundleLibraries, sibling symbols)}. -> 1 when any import is unresolved on any kernel."""
    rc = 0
    for ex in kernels:
        out(f"--- imports against {ex.label}")
        for k, (und, libs, siblings) in groups.items():
            tally, bad = {}, []
            for s in sorted(und):
                c = classify(s, ex, libs, nvidia if k == "NVRM" else set(), siblings, allow, atexit)
                tally[c.split()[0]] = tally.get(c.split()[0], 0) + 1
                if c.startswith("UNRESOLVED"):
                    others = [o.label for o in kernels if o is not ex and
                              classify(s, o, libs, nvidia if k == "NVRM" else set(), siblings, allow, atexit) == "kernel"]
                    bad.append((s, c + (f" [exported by {', '.join(others)}, so the kext would load there and not here]" if others else "")))
                elif verbose:
                    out(f"  {c}: {s}")
            out(f"imports {k}: {len(und)} symbols: " + ", ".join(f"{n} {c}" for c, n in sorted(tally.items())))
            for s, c in bad:
                out(f"  {c}: {s}")
            if k == "NVRM" and any(c == "UNRESOLVED" and not s.startswith("__Z") for s, c in bad):
                out(NVIDIA_HINT)
            rc |= bool(bad)
    return rc


def fetch_exports(dest, tag):
    """xnu's config/ folder at one tag, and nothing else of the tree."""
    part = dest.parent / (dest.name + ".part")
    subprocess.run(["rm", "-rf", str(part)])
    for c in (["git", "init", "-q", str(part)], ["git", "-C", str(part), "remote", "add", "origin", XNU_URL],
              ["git", "-C", str(part), "sparse-checkout", "set", "config"],
              ["git", "-C", str(part), "fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", "tag", tag],
              ["git", "-C", str(part), "checkout", "-q", "FETCH_HEAD"]):
        if subprocess.run(c).returncode:
            print("STOP: cannot fetch xnu's export lists:", " ".join(c)); return False
    head = subprocess.run(["git", "-C", str(part), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    if tag in XNU_SHA and head != XNU_SHA[tag]:
        print(f"STOP: {XNU_URL} tag {tag} is commit {head}, not the pinned {XNU_SHA[tag]}"); return False
    if not (part / "config" / "IOKit.exports").is_file():
        print(f"STOP: {XNU_URL} at {tag} has no config/IOKit.exports"); return False
    part.rename(dest)
    return True


NV_SAMPLE = """
NV_STATUS NV_API_CALL os_alloc_mem(void **address, NvU64 size);   /* the OS layer defines it, not the RM */
extern NvU32 g_only_declared;
RsServer          g_resServ;
// rm_in_a_comment(x) { is not code
#define rm_macro_only(x) { (x)++; }
#define vidmemGetHeap(pGpu, pDevice) vidmemGetHeap_IMPL(pGpu, pDevice)
#define portMemFree(p) free(p)
NV_STATUS NV_API_CALL rm_isr_bh(
    nvidia_stack_t *sp,
    nv_state_t *nv
)
{
    if (rm_called_only(nv)) {
        return floor(1.0) ? NV_OK : NV_ERR_GENERIC;
    }
    return NV_OK;
}
static NV_STATUS
_helperOnly
(
    OBJGPU *pGpu
)
{
    return NV_OK;
}
Heap*
vidmemGetHeap
(
    OBJGPU  *pGpu,
    void   (*pCallback)(OBJGPU *, void *)
)
{
    return NULL;
}
NvBool nvKmsOpen(NvU32 pid) { return NV_TRUE; }
"""


def selftest():
    old = Exports("macOS 15", {"_IOLog": {"IOKit"}, "_thread_call_cancel": {"Mach"}, "_thread_call_cancel_wait": {PRIVATE}, "_real_ncpus": {"Unsupported"},
                               "_PE_parse_boot_argn": {"IOKit"}}, [("_OSKext", "Libkern"), ("__ZN16IOPlatformExpert", PRIVATE)])
    new = Exports("macOS 26", dict(old.exact, _PE_boot_args={"IOKit"}), list(old.prefix))
    libs = {"com.apple.kpi.iokit", "com.apple.kpi.libkern", "com.apple.kpi.mach", "com.apple.iokit.IOPCIFamily"}
    C = lambda sym, ex=old, l=libs, nvidia=(), sib=(), allow=(), atexit=False: classify(sym, ex, l, set(nvidia), set(sib), set(allow), atexit)
    lines = []
    rc_both = check([old, new], {"NVRM": ({"_IOLog", "_PE_boot_args"}, libs, set())}, set(), set(), out=lines.append)
    rc_good = check([old, new], {"NVRM": ({"_IOLog", "_PE_parse_boot_argn"}, libs, set())}, set(), set(), out=lambda s: None)
    rc_new = check([new], {"NVRM": ({"_IOLog", "_PE_boot_args"}, libs, set())}, set(), set(), out=lambda s: None)
    names, aliases = nvidia_defined_in(NV_SAMPLE)
    nv = names | {b for a, b in aliases if a in names}
    hint = []
    rc_floor = check([old], {"NVRM": ({"_IOLog", "_floor"}, libs, set())}, nv, set(), out=hint.append)
    arms = [
        ("an exported kernel function is resolved", C("_IOLog") == "kernel"),
        ("a symbol in no export list is caught", C("_no_such_function") == "UNRESOLVED"),
        ("thread_call_cancel_wait, which only the Private KPI exports, is caught and the message says why",
         C("_thread_call_cancel_wait").startswith("UNRESOLVED") and "Private KPI only" in C("_thread_call_cancel_wait")),
        ("naming com.apple.kpi.private in the plist does not make a Private symbol loadable",
         C("_thread_call_cancel_wait", l=libs | {"com.apple.kpi.private"}).startswith("UNRESOLVED")),
        ("a Private name* prefix is caught as well", C("__ZN16IOPlatformExpert9someThingEv").startswith("UNRESOLVED (exported by the Private")),
        ("a symbol of a KPI the plist does not name is caught, and the KPI is named", "exported by Unsupported, which the Info.plist does not name" in C("_real_ncpus")),
        ("the same symbol resolves once the plist names the KPI", C("_real_ncpus", l=libs | {"com.apple.kpi.unsupported"}) == "kernel"),
        ("a prefix export (name*) resolves", C("_OSKextRetainKextWithLoadTag") == "kernel"),
        ("a family class the plist names is accepted", C("__ZN11IOPCIDevice12configRead32Eh") == "family"),
        ("a family class the plist does not name is caught", C("__ZN13IOFramebuffer4openEv").startswith("UNRESOLVED (family")),
        ("a family vtable, a const method and a thunk belong to the family",
         all(C(s) == "family" for s in ("__ZTV11IOPCIDevice", "__ZNK11IOPCIDevice9getVendorEv", "__ZThn16_N11IOPCIDevice5startEP9IOService"))),
        ("a method of a class of this tree is not the family's because a family class is among its parameters",
         C("__ZN9NVRMOther6attachEP11IOPCIDevice") == "UNRESOLVED" and C("__ZN4NVRM5probeEP9IOServiceP11IOPCIDevice") == "UNRESOLVED"),
        ("a function the NVIDIA sources define is theirs", C("_rm_isr_bh", nvidia={"rm_isr_bh"}) == "nvidia"),
        ("a function they do not define is not excused: floor(), or an os_* function the kext forgot to define",
         C("_floor", nvidia=nv) == "UNRESOLVED" and C("_os_alloc_mem", nvidia=nv) == "UNRESOLVED" and rc_floor == 1 and any("build/kext_imports_allow.txt" in l for l in hint)),
        ("a Private kernel function is not excused by the NVIDIA sources defining its name",
         C("_thread_call_cancel_wait", nvidia={"thread_call_cancel_wait"}).startswith("UNRESOLVED")),
        ("NVIDIA's text: a definition spread over lines is found, and so is a file-scope variable",
         {"rm_isr_bh", "vidmemGetHeap", "g_resServ", "nvKmsOpen"} <= nv),
        ("NVIDIA's text: the generated NAME -> NAME_IMPL macro over a definition counts", "vidmemGetHeap_IMPL" in nv),
        ("NVIDIA's text: a declaration, an extern variable, a static function, a call, a macro and a comment define nothing",
         not nv & {"os_alloc_mem", "g_only_declared", "_helperOnly", "rm_called_only", "rm_macro_only", "rm_in_a_comment", "floor", "free", "portMemFree"}),
        ("a symbol another kext of the tree defines is a sibling's", C("_nv_xnu_helper", sib={"_nv_xnu_helper"}) == "sibling"),
        ("an allow-listed symbol passes", C("_x", allow={"_x"}) == "allowed"),
        ("a global destructor lowered to __cxa_atexit is caught unless the toolchain is known to do that",
         C("___cxa_atexit").startswith("UNRESOLVED") and C("___cxa_atexit", atexit=True) == "toolchain"),
        ("PE_boot_args resolves on macOS 26 alone", C("_PE_boot_args", ex=new) == "kernel" and C("_PE_boot_args", ex=old) == "UNRESOLVED" and rc_new == 0),
        ("checked against both kernels the same object fails, and the line says where it would load",
         rc_both == 1 and any("_PE_boot_args" in l and "exported by macOS 26, so the kext would load there and not here" in l for l in lines)),
        ("an object whose imports both kernels export passes", rc_good == 0),
    ]
    bad = [n for n, ok in arms if not ok]
    for n, ok in arms:
        print(("ok   " if ok else "FAIL ") + n)
    print(f"{len(bad)} failed of {len(arms)}")
    return 1 if bad else 0


def main():
    if "--selftest" in sys.argv:
        return selftest()
    ap = argparse.ArgumentParser(description="classify the kernel symbols compiled kext objects import")
    ap.add_argument("--objects", required=True); ap.add_argument("--ogkm", required=True); ap.add_argument("--repo", required=True)
    ap.add_argument("--exports", action="append", default=[]); ap.add_argument("--xnu-tag", action="append", default=[])
    ap.add_argument("--work", default=""); ap.add_argument("--offline", action="store_true")
    ap.add_argument("--nm", default="llvm-nm"); ap.add_argument("--toolchain-atexit", action="store_true"); ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    repo, objs = Path(a.repo), Path(a.objects)
    kernels = []
    for d in a.exports:
        cfg = Path(d)
        if not (cfg / "IOKit.exports").is_file():
            print(f"STOP: no xnu export lists at {cfg} (expected IOKit.exports and its neighbours)"); return 2
        tag = subprocess.run(["git", "-C", str(cfg), "describe", "--tags", "--exact-match"], capture_output=True, text=True).stdout.strip()
        kernels.append(Exports.load(cfg, tag or str(cfg)))
    if not a.exports:
        if not a.work:
            print("STOP: give --exports <xnu config dir> once per kernel, or --work <dir> to keep fetched export lists in"); return 2
        for tag in a.xnu_tag or XNU_TAGS:
            dest = Path(a.work) / "src" / tag
            if not (dest / "config" / "IOKit.exports").is_file():
                if a.offline:
                    print(f"STOP: no xnu export lists at {dest}/config and --offline was given"); return 2
                dest.parent.mkdir(parents=True, exist_ok=True)
                if not fetch_exports(dest, tag):
                    return 2
            kernels.append(Exports.load(dest / "config", tag))
    allow = set()
    ap_ = repo / "build/kext_imports_allow.txt"
    if ap_.is_file():
        allow = {l.split()[0] for l in ap_.read_text().splitlines() if l.strip() and not l.startswith("#")}
    files = {k: [p for p in sorted(objs.glob("*.o")) if re.search(pat, p.name)] for k, (pat, _) in KEXTS.items()}
    defined = {k: set().union(*[nm(a.nm, p, "-gU") for p in fs]) if fs else set() for k, fs in files.items()}
    groups = {}
    for k, (pat, plist) in KEXTS.items():
        if not files[k]:
            print(f"imports {k}: no object (not compiled in this run)"); continue
        libs = set(plistlib.load(open(repo / plist, "rb")).get("OSBundleLibraries", {}))
        und = set().union(*[nm(a.nm, p, "-u") for p in files[k]]) - defined[k]
        groups[k] = (und, libs, defined["NVRM"] if "com.nullmoth.NVRM" in libs else set())
    if not groups:
        print(f"STOP: no kext object in {objs}"); return 2
    rc = check(kernels, groups, nvidia_defined(Path(a.ogkm)), allow, a.toolchain_atexit, a.verbose)
    names = " and ".join(e.label for e in kernels)
    print("import check: " + (f"FAILED (an UNRESOLVED symbol means the kext would not load) against {names}" if rc else f"every import has a provider on {names}"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
