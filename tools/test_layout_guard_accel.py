#!/usr/bin/env python3
"""NVAccel compares the accelerator family classes it subclasses with the running kernel, and declines to attach on a
difference.

A false refusal removes acceleration from a Mac that works; a missed one leaves the driver's fields on Apple's or its
virtual calls one slot off. So only what can be read without doubt may refuse:
  - a class size, which the kernel states through OSMetaClass::getClassSize();
  - a vtable that reads SHORTER than this build expects (the reading stops at a null entry inside the expected count,
    which an Apple table does not have, and only after the same reading came out exactly right on IOService).
A vtable that reads LONGER refuses nothing: whether a null word follows a table of another kext in the running kernel
collection is not something the kext can ask. It is reported (property, read-only sysctl, log line).
What this checks, on the source text and with the host compiler:
  - every vtable the check reads is named exactly ("__ZTV" + length + name), is the base of one of the kext's own
    classes, and is one of the tables each build already imports (the macOS 26 ones only under NM_TAHOE);
  - every Apple base of a class of the kext, and every Apple base of a row, has a size row with a static_assert;
  - the check runs in NVAccel::probe after the device gate and before IOGraphicsAccelerator2::probe and returns NULL
    when it refuses, in both flavours; nothing of the instance is read before it;
  - NVAccel's constructor stores only zeros (it runs before the check);
  - the sysctls are read-only and registered once, from the accelerator's sysctl list (never from probe);
  - the evaluation that returns the verdict is handed only the tables the report let through, the function returns on
    that verdict alone, nothing else computes a verdict, and the lengths have their own property and log line;
  - no class of the kext adds a virtual function (its tables are exactly as long as this build's bases);
  - on the pure part, compiled from the kext's own headers: a table that reads longer never reaches the verdict, one
    that reads shorter does and refuses, nothing is read when IOService does not come out equal, no word past the
    expected end is read, the output array and the text are never overrun, and no line reads as a verdict.
Each group has a negative control on a changed copy of the text."""
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
ACC = ROOT / "kexts/NVRM/accel/nvrm-accel.cpp"
STUBS = ROOT / "kexts/NVRM/accel/nvaccel_stubs.h"
LAY = ROOT / "kexts/NVRM/accel/nvaccel_layout.h"
FAILS = []

# The family tables each build imports already (measured on the objects of both builds with llvm-nm -u): reading any
# other one would be a new import from IOAcceleratorFamily2.
IMPORTED_15 = {"IOService", "IOGraphicsAccelerator2", "IOAccelResource2", "IOAccelVidMemory", "IOAccelMemoryMap",
               "IOAccelLegacyDisplayPipe"}
IMPORTED_26_EXTRA = {"IOAccelSysMemory", "IOAccel2DContext2", "IOAccelCLContext2", "IOAccelGLContext2",
                     "IOAccelLegacySurface", "IOAccelVideoContext2", "IOAccelEventMachineFast2",
                     "IOAccelLegacyDisplayMachine"}
NOT_APPLE_FAMILY = {"IOService", "OSObject", "IOUserClient"}


def arm(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def strip_comments(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def block(text, start, end_marker="\n}\n"):
    a = text.find(start)
    if a < 0:
        return ""
    b = text.find(end_marker, a)
    return text[a:b if b >= 0 else len(text)]


def call_args(text, name):
    calls = []
    for m in re.finditer(r"\b" + re.escape(name) + r"\s*\(", text):
        depth, i, args, cur = 1, m.end(), [], ""
        while i < len(text) and depth:
            c = text[i]
            if c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
                if not depth:
                    break
            if c == "," and depth == 1:
                args.append(cur.strip())
                cur = ""
            else:
                cur += c
            i += 1
        args.append(cur.strip())
        calls.append(args)
    return calls


def own_classes(acc, stubs):
    return re.findall(r"class\s+(\w+)\s*:\s*public\s+(\w+)", strip_comments(acc) + strip_comments(stubs))


def split_tahoe(layout_h):
    """(outside NM_TAHOE, inside NM_TAHOE) parts of the comment-free header"""
    src = strip_comments(layout_h)
    inside, outside, depth = [], [], 0
    for line in src.split("\n"):
        s = line.strip()
        if s.startswith("#ifdef NM_TAHOE"):
            depth = 1
            continue
        if depth and s.startswith("#endif"):
            depth = 0
            continue
        (inside if depth else outside).append(line)
    return "\n".join(outside), "\n".join(inside)


def vtable_problems(layout_h, acc, stubs):
    out = []
    labels = re.findall(r'NVL_VTABLE\(\s*(\w+)\s*,\s*"([^"]*)"\s*\)', strip_comments(layout_h))
    if not labels:
        out.append("no NVL_VTABLE at all")
    bases = {b for _, b in own_classes(acc, stubs)}
    for cls, sym in labels:
        if sym != f"__ZTV{len(cls)}{cls}":
            out.append(f"{cls}: label {sym} is not __ZTV{len(cls)}{cls}")
        if cls not in bases:
            out.append(f"{cls}: not the base of a class of the kext, so its vtable would be a new import")
    outside, inside = split_tahoe(layout_h)
    for cls in re.findall(r"NVL_VTABLE\(\s*(\w+)", outside):
        if cls not in IMPORTED_15:
            out.append(f"{cls}: read by the macOS 15 build, which does not import its vtable")
    for cls in re.findall(r"NVL_VTABLE\(\s*(\w+)", inside):
        if cls not in IMPORTED_15 | IMPORTED_26_EXTRA:
            out.append(f"{cls}: read by the macOS 26 build, which does not import its vtable")
    for c in set(re.findall(r"NVL_VT\(\s*(\w+)\s*\)", outside)) - set(re.findall(r"NVL_VTABLE\(\s*(\w+)", outside)):
        out.append(f"NVL_VT({c}) in the macOS 15 build without its NVL_VTABLE there")
    for c in set(re.findall(r"NVL_VT\(\s*(\w+)\s*\)", inside)) - {c for c, _ in labels}:
        out.append(f"NVL_VT({c}) without NVL_VTABLE")
    return out


def row_problems(layout_h, acc, stubs):
    out = []
    rows = dict(re.findall(r"NVL_ROW\(\s*(\w+)\s*,\s*(\w+)\s*\)", strip_comments(layout_h)))
    for _, b in own_classes(acc, stubs):
        if b not in NOT_APPLE_FAMILY and b not in rows:
            out.append(f"{b} (base of a class of the kext) has no row")
    for cls, sup in rows.items():
        if sup not in NOT_APPLE_FAMILY and sup not in rows:
            out.append(f"{cls}'s base {sup} has no row")
        if f"__is_base_of({sup}, {cls})" not in layout_h:
            out.append(f"no static_assert that {sup} is the base of {cls}")
    return out


def probe_body(acc):
    src = strip_comments(acc)
    cls = block(src, "class NVAccel : public IOGraphicsAccelerator2 {", "\n};\n")
    return block(cls, "virtual IOService *probe(IOService *provider, SInt32 *score)", "\n    }\n")


def wiring_problems(acc):
    out = []
    probe = probe_body(acc)
    gate = probe.find('- claiming"')
    q = probe.find("return IOGraphicsAccelerator2::probe(provider, score);")
    checks = [m.start() for m in re.finditer(r"if \(!nvaccelLayoutAllows\(this, provider, \"(NVAccel|NMXAccel)\"\)\) return NULL;", probe)]
    if len(checks) != 2 or not all(0 <= gate < c < q for c in checks):
        out.append("NVAccel::probe does not check the layout after the device gate and before IOGraphicsAccelerator2::probe")
    if "#ifdef NMX_ACCEL" not in probe[gate:q] if gate >= 0 and q >= 0 else True:
        out.append("NVAccel::probe does not name its flavour")
    first = min(checks) if checks else -1
    between = probe[gate:first] if 0 <= gate < first else ""
    if re.search(r"\b(getProperty|setProperty|copyProperty|f[A-Z]\w*|IOGraphicsAccelerator2::|getProvider|getWorkLoop)\b", between.split("\n", 1)[-1]):
        out.append("NVAccel::probe reads the instance between the device gate and the layout check")
    if "nvaccelLayoutSysctls" in probe:
        out.append("NVAccel::probe registers sysctls: a kext that declines may be unloaded under them")
    return out


ZERO = re.compile(r"^(0|0u|nullptr|false|\{\s*\}|\{\s*(0|nullptr)\s*\})$")


def ctor_problems(acc):
    """in-class initializers of NVAccel: the constructor runs before probe's check, so it may store only zeros"""
    out = []
    src = strip_comments(acc)
    cls = block(src, "class NVAccel : public IOGraphicsAccelerator2 {", "\n};\n")
    found = 0
    for line in cls.split("\n"):
        if not line.startswith("    ") or line.startswith("     "):
            continue
        s = line.strip()
        if re.match(r"(static|return|if|else|for|while|case|default|typedef|using|enum|friend)\b", s):
            continue
        for stmt in s.split(";"):
            if "(" in stmt.split("=")[0] or "=" not in stmt or "==" in stmt:
                continue
            for decl in stmt.split(","):
                if "=" not in decl:
                    continue
                name, val = decl.split("=", 1)
                m = re.search(r"(\w+)\s*(\[[^\]]*\])?\s*$", name)
                if not m:
                    continue
                found += 1
                if not ZERO.match(val.strip()):
                    out.append(f"{m.group(1)} = {val.strip()}")
    if found < 10:
        out.append(f"only {found} in-class initializers found: the scan no longer reads the class")
    return out


LAYOUT_SYSCTLS = ("nvaccel_layout", "nvaccel_layout_classes", "nvaccel_layout_vtables", "nvaccel_layout_bad",
                  "nvaccel_layout_vtlong", "nvaccel_layout_pure")


def sysctl_problems(layout_h, acc):
    out = []
    src = strip_comments(layout_h)
    for name in LAYOUT_SYSCTLS:
        m = re.search(r"SYSCTL_INT\s*\(\s*_debug\s*,\s*OID_AUTO\s*,\s*" + name + r"\s*,([^,]*),", src)
        if not m:
            out.append(f"debug.{name} is not declared as SYSCTL_INT")
        elif re.search(r"CTLFLAG_(RW|WR|ANYBODY)", m.group(1)) or "CTLFLAG_RD" not in m.group(1):
            out.append(f"debug.{name} is not read-only")
    declared = set(re.findall(r"SYSCTL_\w+\s*\(\s*_debug\s*,\s*OID_AUTO\s*,\s*(\w+)", src))
    if declared != set(LAYOUT_SYSCTLS):
        out.append(f"the header declares {sorted(declared)}, the test knows {sorted(LAYOUT_SYSCTLS)}")
    once = block(src, "static void nvaccelLayoutSysctls(void)")
    if "OSCompareAndSwap(0, 1, &once)" not in once:
        out.append("nvaccelLayoutSysctls does not run once")
    a = strip_comments(acc)
    for name in sorted(declared):
        reg = f"sysctl_register_oid(&sysctl__debug_{name});"
        if once.count(reg) != 1 or src.count(reg) != 1 or f"sysctl__debug_{name}" in a:
            out.append(f"debug.{name} is not registered exactly once, in nvaccelLayoutSysctls")
    inst = block(a, "static void nvaccelInstallSysctl(void)\n{")
    nv = inst[inst.find("#else"):] if "#else" in inst else ""
    if nv.count("nvaccelLayoutSysctls();") != 1 or inst.count("nvaccelLayoutSysctls();") != 1:
        out.append("nvaccelLayoutSysctls is not called once, from the NVIDIA part of nvaccelInstallSysctl")
    if a.count("nvaccelLayoutSysctls") != 1:
        out.append("nvaccelLayoutSysctls is called from somewhere else as well")
    return out


def verdict_problems(layout_h):
    """Only a size or a shorter table may refuse; a table that reads longer must not reach the verdict."""
    out = []
    src = strip_comments(layout_h)
    calls = call_args(src, "nvLayoutEvaluateOnce")
    if len(calls) != 1:
        out.append(f"{len(calls)} calls of nvLayoutEvaluateOnce, expected one")
    elif len(calls[0]) != 8 or calls[0][4:] != ["mayRefuse", "nMay", "ioService.slots", "ioService.built"]:
        out.append("the evaluation that can refuse is handed something other than the tables the report let through: "
                   + ", ".join(calls[0][4:]))
    for other in ("nvLayoutEvaluate", "nvLayoutVerdict", "nvLayoutVtRow"):
        if call_args(src, other):
            out.append(f"{other}() is called here: a second place that could turn a table into a verdict")
    allows = block(src, "static bool nvaccelLayoutAllows(")
    if not re.search(r"const\s+unsigned\s+nMay\s*=\s*nvaccelVtReport\s*\(\s*&vt\s*,\s*&ioService\s*,\s*vts\s*,\s*nVts\s*,\s*mayRefuse\s*,\s*nVts\s*\)\s*;", allows):
        out.append("nMay is not the number of tables nvaccelVtReport let through")
    if re.search(r"\bmayRefuse\s*\[[^\]]*\]\s*=", allows) or re.search(r"\bnMay\s*[-+*/]?=[^=]", allows.split("nvaccelVtReport", 1)[-1]):
        out.append("the tables handed to the verdict are changed after the report chose them")
    if not re.search(r"const\s+NvLayoutState\s+st\s*=\s*nvLayoutEvaluateOnce\s*\(", allows):
        out.append("st is not the constant result of the evaluation")
    returns = [r.strip() for r in re.findall(r"\breturn\b([^;]*);", allows)]
    if returns != ["true", "st != kNvLayoutRefused"]:
        out.append("nvaccelLayoutAllows returns on something other than the verdict: " + " | ".join(returns))
    rep = block(src, "static inline unsigned nvaccelVtReport(")
    if rep.count("mayRefuse[n++] = vts[i];") != 1 or "if (r[i] <= 0 && mayRefuse && n < cap) mayRefuse[n++] = vts[i];" not in rep:
        out.append("nvaccelVtReport does not copy exactly the tables that read the same or shorter")
    for need, what in (('provider->setProperty("nvaccel-layout", gNvaccelLayout.run.text);', "nvaccel-layout is not the verdict text"),
                       ('provider->setProperty("nvaccel-layout-vtables", vt.text);', "the lengths are not published as nvaccel-layout-vtables"),
                       ('IOLog("%s: Apple vtable lengths %s\\n", who, vt.text);', "the lengths are not logged"),
                       ("gNvaccelLayoutVtLong = (int)vt.longer;", "debug.nvaccel_layout_vtlong is not the number of longer tables"),
                       ("gNvaccelLayoutVtables = (int)vt.compared;", "debug.nvaccel_layout_vtables is not the number of tables read")):
        if allows.count(need) != 1:
            out.append(what)
    # the pure-slot reading of the instance happens after the verdict, only when it allows
    p = allows.find("*(const void *const *const *)self")
    e = allows.find("nvLayoutEvaluateOnce(")
    if p < 0 or p < e or "st != kNvLayoutRefused && self && vt.calibrated" not in allows:
        out.append("the instance's vtable is read before the verdict or when it refused")
    return out


def virtual_problems(acc, stubs):
    """every virtual declared by a class of the kext overrides a base function: no class adds a slot"""
    out = []
    for name, text in (("nvrm-accel.cpp", acc), ("nvaccel_stubs.h", stubs)):
        src = strip_comments(text)
        src = re.sub(r'"(\\.|[^"\\])*"', '""', src)
        for m in re.finditer(r"\bvirtual\b([^;{]*)", src):
            if not re.search(r"\b(APPLE_KEXT_OVERRIDE|override)\b", m.group(1)):
                out.append(f"{name}: virtual{m.group(1).strip()[:60]} adds a slot")
        if "OSMetaClassDeclareReserved" in src:
            out.append(f"{name}: reserved slots of its own")
    return out


PURE = r'''
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <sys/mman.h>
#include <unistd.h>
#include "nvaccel_layout.h"
static const void *tab[6][400];
static const int built[6] = { 266, 350, 79, 69, 57, 318 };   // IOService and the five tables of the macOS 15 build
static const char *const names[6] = { "IOService", "IOGraphicsAccelerator2", "IOAccelResource2", "IOAccelVidMemory",
                                      "IOAccelMemoryMap", "IOAccelLegacyDisplayPipe" };
static void fill(const int *ends)
{
    for (int v = 0; v < 6; v++)
        for (int i = 0; i < 400; i++) tab[v][i] = i < ends[v] ? (const void *)&tab : nullptr;
}
struct Out { NvaccelVtReport rep; NvLayoutVt may[5]; unsigned n; NvLayoutTally t; };
// What nvaccelLayoutAllows does: report, then the evaluation on what the report let through (the kernel part runs the
// same steps inside nvLayoutEvaluate, with the size rows, which all match here).
static Out run(const int *ends)
{
    fill(ends);
    const NvLayoutVt svc = { names[0], tab[0], built[0] };
    NvLayoutVt vts[5];
    for (int i = 0; i < 5; i++) vts[i] = NvLayoutVt{ names[i + 1], tab[i + 1], built[i + 1] };
    Out o;
    o.n = nvaccelVtReport(&o.rep, &svc, vts, 5, o.may, 5);
    nvLayoutTallyInit(&o.t);
    const NvLayoutRow row = { "IOGraphicsAccelerator2", "IOAccelerator", 0xdd8 };
    const NvLayoutSeen seen = { true, 0xdd8, "IOAccelerator" };
    nvLayoutRow(&row, &seen, &o.t);
    o.t.calibrated = nvLayoutVtCmp(svc.slots, svc.built, nullptr) == 0 ? 1 : 0;
    for (unsigned i = 0; o.t.calibrated && i < o.n; i++) nvLayoutVtRow(&o.may[i], &o.t);
    return o;
}
#define UNITS " (virtual functions: this macOS/the driver)"
int main()
{
    // all as built: every table reaches the verdict, which is ok
    const int same[6] = { 266, 350, 79, 69, 57, 318 };
    Out o = run(same);
    assert(o.rep.calibrated == 1 && o.rep.compared == 5 && o.rep.shorter == 0 && o.rep.longer == 0 && o.n == 5);
    assert(nvLayoutVerdict(&o.t, false) == kNvLayoutOk && o.t.vtables == 5);
    assert(!strcmp(o.rep.text, "same: IOGraphicsAccelerator2 350/350, IOAccelResource2 79/79, IOAccelVidMemory 69/69, "
                               "IOAccelMemoryMap 57/57, IOAccelLegacyDisplayPipe 318/318" UNITS));
    assert(nvLayoutTextState(o.rep.text) == kNvLayoutNotRun);

    // a table that goes on past the expected end: reported, not handed on, the verdict stays ok
    const int longer[6] = { 266, 352, 79, 69, 57, 318 };
    o = run(longer);
    assert(o.rep.compared == 5 && o.rep.longer == 1 && o.rep.shorter == 0 && o.n == 4);
    for (unsigned i = 0; i < o.n; i++) assert(strcmp(o.may[i].name, "IOGraphicsAccelerator2") != 0);
    assert(nvLayoutVerdict(&o.t, false) == kNvLayoutOk && o.t.vtBad == 0 && o.t.vtables == 4);
    assert(strstr(o.rep.text, "differ: IOGraphicsAccelerator2 >350/350, IOAccelResource2 79/79"));
    assert(strstr(o.rep.text, UNITS "; longer: reported only, not a reason to refuse"));
    assert(nvLayoutTextState(o.rep.text) == kNvLayoutNotRun && !nvLayoutTextRefused(o.rep.text));
    // every table longer: nothing handed on, still ok
    const int allLong[6] = { 266, 400, 400, 400, 400, 400 };
    o = run(allLong);
    assert(o.rep.longer == 5 && o.n == 0 && nvLayoutVerdict(&o.t, false) == kNvLayoutOk);

    // a table that ends early: handed on, the verdict refuses, nvlayout=0 overrides
    const int shorter[6] = { 266, 350, 78, 69, 57, 318 };
    o = run(shorter);
    assert(o.rep.shorter == 1 && o.rep.longer == 0 && o.n == 5);
    assert(nvLayoutVerdict(&o.t, false) == kNvLayoutRefused && nvLayoutVerdict(&o.t, true) == kNvLayoutOverridden);
    assert(o.t.firstKind == kNvLayoutDiffVtShort && !strcmp(o.t.firstName, "IOAccelResource2"));
    assert(strstr(o.rep.text, "IOAccelResource2 78/79") && strstr(o.rep.text, "; shorter: refuses (see nvaccel-layout)"));
    char text[200];
    nvLayoutText(text, sizeof text, kNvLayoutRefused, &o.t, "25B78", "25.1.0");
    assert(nvLayoutTextRefused(text) && strstr(text, "IOAccelResource2 has 78 virtual functions in this macOS, 79 in the driver"));
    // one longer and one shorter: the shorter decides, the longer is still not handed on
    const int both[6] = { 266, 351, 79, 1, 57, 318 };
    o = run(both);
    assert(o.rep.shorter == 1 && o.rep.longer == 1 && o.n == 4 && nvLayoutVerdict(&o.t, false) == kNvLayoutRefused);
    assert(!strcmp(o.t.firstName, "IOAccelVidMemory"));

    // IOService does not read as built: nothing else is read, nothing is handed on, sizes alone decide
    const int calLong[6] = { 270, 300, 1, 1, 1, 1 };
    o = run(calLong);
    assert(o.rep.calibrated == 0 && o.rep.compared == 0 && o.n == 0 && !strcmp(o.rep.text, "not compared: IOService >266/266" UNITS));
    assert(nvLayoutVerdict(&o.t, false) == kNvLayoutOk);
    const int calShort[6] = { 200, 1, 1, 1, 1, 1 };
    o = run(calShort);
    assert(o.rep.calibrated == 0 && o.n == 0 && !strcmp(o.rep.text, "not compared: IOService 200/266" UNITS));
    assert(nvLayoutTextState(o.rep.text) == kNvLayoutNotRun);
    {   // "nothing else is read": these tables are addresses that fault when touched
        const int cal[6] = { 200, 0, 0, 0, 0, 0 };
        fill(cal);
        const NvLayoutVt svc = { "IOService", tab[0], 266 };
        const NvLayoutVt bad[2] = { { "IOGraphicsAccelerator2", (const void *const *)(uintptr_t)8, 350 },
                                    { "IOAccelResource2", (const void *const *)(uintptr_t)16, 79 } };
        NvLayoutVt may[2];
        NvaccelVtReport r;
        assert(nvaccelVtReport(&r, &svc, bad, 2, may, 2) == 0 && r.compared == 0);
    }
    {   // no table, a table that cannot be read, a slot count that cannot be right: not compared, not handed on
        fill(same);
        const NvLayoutVt svc = { "IOService", tab[0], 266 };
        const NvLayoutVt odd[2] = { { "IOGraphicsAccelerator2", nullptr, 350 }, { "IOAccelResource2", tab[2], -1 } };
        NvLayoutVt may[2];
        NvaccelVtReport r;
        assert(nvaccelVtReport(&r, nullptr, odd, 2, may, 2) == 0 && !strcmp(r.text, "not compared: no table"));
        assert(nvaccelVtReport(&r, &svc, nullptr, 0, may, 2) == 0 && !strcmp(r.text, "not compared: no table"));
        assert(nvaccelVtReport(&r, &svc, odd, 2, may, 2) == 0 && r.compared == 0);
        assert(!strcmp(r.text, "not compared: IOGraphicsAccelerator2 not readable, IOAccelResource2 not readable" UNITS));
    }
    {   // the output array: never more than cap entries
        fill(same);
        const NvLayoutVt svc = { "IOService", tab[0], 266 };
        NvLayoutVt vts[5];
        for (int i = 0; i < 5; i++) vts[i] = NvLayoutVt{ names[i + 1], tab[i + 1], built[i + 1] };
        struct { NvLayoutVt may[2]; unsigned char guard[64]; } g;
        memset(g.guard, 0x5a, sizeof g.guard);
        NvaccelVtReport r;
        assert(nvaccelVtReport(&r, &svc, vts, 5, g.may, 2) == 2 && r.compared == 5);
        for (unsigned i = 0; i < sizeof g.guard; i++) assert(g.guard[i] == 0x5a);
        assert(nvaccelVtReport(&r, &svc, vts, 5, nullptr, 5) == 0 && r.compared == 5);
    }
    {   // no word past the expected end is read: the 70 words of a table (69 entries and what follows) are the last
        // words of a page, and the page after it cannot be read. Once with the null entry, once without.
        const long page = sysconf(_SC_PAGESIZE);
        char *m = (char *)mmap(nullptr, (size_t)page * 2, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
        const bool guarded = m != MAP_FAILED && mprotect(m + page, (size_t)page, PROT_NONE) == 0;
        assert(guarded);
        const void **end = (const void **)(m + page);
        for (int i = 1; i <= 70; i++) end[-i] = (const void *)&tab;
        fill(same);
        const NvLayoutVt svc = { "IOService", tab[0], 266 };
        const NvLayoutVt vts[1] = { { "IOAccelVidMemory", end - 70, 69 } };
        NvLayoutVt may[1];
        NvaccelVtReport r;
        assert(nvaccelVtReport(&r, &svc, vts, 1, may, 1) == 0 && r.longer == 1);
        end[-1] = nullptr;
        assert(nvaccelVtReport(&r, &svc, vts, 1, may, 1) == 1 && r.longer == 0 && strstr(r.text, "same: IOAccelVidMemory 69/69"));
        munmap(m, (size_t)page * 2);
    }
    {   // a line longer than the buffer is cut, not written past it; thirteen tables (the macOS 26 build) fit
        struct { NvaccelVtReport rep; char guard[8]; } g;
        memset(g.guard, 0x5a, sizeof g.guard);
        fill(same);
        const NvLayoutVt svc = { "IOService", tab[0], 266 };
        static const char *const n26[13] = { "IOGraphicsAccelerator2", "IOAccelResource2", "IOAccelVidMemory", "IOAccelMemoryMap",
            "IOAccelLegacyDisplayPipe", "IOAccelSysMemory", "IOAccel2DContext2", "IOAccelCLContext2", "IOAccelGLContext2",
            "IOAccelLegacySurface", "IOAccelVideoContext2", "IOAccelEventMachineFast2", "IOAccelLegacyDisplayMachine" };
        NvLayoutVt vts[64];
        for (int i = 0; i < 13; i++) vts[i] = NvLayoutVt{ n26[i], tab[1], 350 };
        nvaccelVtReport(&g.rep, &svc, vts, 13, nullptr, 0);
        assert(strlen(g.rep.text) < sizeof g.rep.text - 1 && strstr(g.rep.text, "IOAccelLegacyDisplayMachine 350/350" UNITS));
        for (int i = 0; i < 64; i++) vts[i] = NvLayoutVt{ "IOAccelLegacyDisplayMachineIOAccelLegacyDisplayMachine", tab[1], 350 };
        assert(nvaccelVtReport(&g.rep, &svc, vts, 64, nullptr, 0) == 0 && g.rep.compared == 64);
        assert(strlen(g.rep.text) == sizeof g.rep.text - 1);
        for (unsigned i = 0; i < sizeof g.guard; i++) assert(g.guard[i] == 0x5a);
    }
    puts("longer is a report, shorter refuses, nothing is read without the IOService self-check");
    return 0;
}
'''


def main():
    acc, stubs, lay = ACC.read_text(), STUBS.read_text(), LAY.read_text()

    pr = vtable_problems(lay, acc, stubs)
    arm("vtable labels are exact, each is the base of a class of the kext and imported by the build that reads it", not pr, "; ".join(pr))
    pr = row_problems(lay, acc, stubs)
    arm("every Apple base of a class of the kext, and every Apple base of a row, has a size row", not pr, "; ".join(pr))
    pr = wiring_problems(acc)
    arm("NVAccel::probe checks after the device gate and before IOGraphicsAccelerator2::probe, in both flavours", not pr, "; ".join(pr))
    pr = ctor_problems(acc)
    arm("NVAccel's constructor stores only zeros", not pr, "; ".join(pr))
    pr = sysctl_problems(lay, acc)
    arm("the six layout sysctls are read-only and registered once, from the accelerator's sysctl list", not pr, "; ".join(pr))
    pr = verdict_problems(lay)
    arm("only sizes and tables that read the same or shorter reach the verdict; the lengths have their own outputs", not pr, "; ".join(pr))
    pr = virtual_problems(acc, stubs)
    arm("no class of the kext adds a virtual function", not pr, "; ".join(pr))

    # negative controls, on changed copies of the text
    arm("control: a mistyped vtable label is caught",
        any("label" in x for x in vtable_problems(lay.replace('"__ZTV16IOAccelResource2"', '"__ZTV15IOAccelResource2"'), acc, stubs)))
    arm("control: a macOS 26 table read by the macOS 15 build is caught",
        any("macOS 15 build" in x for x in vtable_problems(lay.replace(
            'NVL_VTABLE(IOAccelLegacyDisplayPipe, "__ZTV24IOAccelLegacyDisplayPipe");\n',
            'NVL_VTABLE(IOAccelLegacyDisplayPipe, "__ZTV24IOAccelLegacyDisplayPipe");\n'
            'NVL_VTABLE(IOAccelSysMemory, "__ZTV16IOAccelSysMemory");\n'), acc, stubs)))
    arm("control: a vtable of a class the kext does not subclass is caught",
        any("new import" in x for x in vtable_problems(lay + '\nNVL_VTABLE(IOAccelShared2, "__ZTV14IOAccelShared2");\n', acc, stubs)))
    arm("control: a missing row is caught",
        any("IOAccelMemory" in x for x in row_problems(lay.replace("        NVL_ROW(IOAccelMemory, OSObject),\n", ""), acc, stubs)))
    arm("control: a row without its static_assert is caught",
        any("static_assert" in x for x in row_problems(lay.replace(
            'static_assert(__is_base_of(IOAccelContext2, IOAccelCLContext2), "IOAccelCLContext2\'s base is IOAccelContext2");\n', ""), acc, stubs)))
    arm("control: the check after IOGraphicsAccelerator2::probe is caught",
        any("before IOGraphicsAccelerator2::probe" in x for x in wiring_problems(acc.replace(
            '        if (!nvaccelLayoutAllows(this, provider, "NVAccel")) return NULL;\n#endif\n        return IOGraphicsAccelerator2::probe(provider, score);',
            '#endif\n        IOService *r = IOGraphicsAccelerator2::probe(provider, score);\n        if (!nvaccelLayoutAllows(this, provider, "NVAccel")) return NULL;\n        return r;'))))
    arm("control: a flavour without the check is caught",
        any("before IOGraphicsAccelerator2::probe" in x for x in wiring_problems(acc.replace(
            '        if (!nvaccelLayoutAllows(this, provider, "NMXAccel")) return NULL;\n', ""))))
    arm("control: an instance field read before the check is caught",
        any("reads the instance" in x for x in wiring_problems(acc.replace(
            "        // Before anything reads or calls Apple's part of this instance",
            "        if (fNVRM) return NULL;\n        // Before anything reads or calls Apple's part of this instance"))))
    arm("control: a constructor that stores 60 is caught",
        any("fHeadlessDeadlineSec" in x for x in ctor_problems(acc.replace(
            "    uint32_t fHeadlessDeadlineSec = 0;\n", "    uint32_t fHeadlessDeadlineSec = kHeadlessDeadlineSec;\n"))))
    arm("control: a writable layout sysctl is caught",
        any("read-only" in x for x in sysctl_problems(lay.replace("nvaccel_layout_vtlong, CTLFLAG_RD", "nvaccel_layout_vtlong, CTLFLAG_RW"), acc)))
    arm("control: a layout sysctl that is never registered is caught",
        any("nvaccel_layout_pure is not registered" in x for x in sysctl_problems(
            lay.replace("    sysctl_register_oid(&sysctl__debug_nvaccel_layout_pure);\n", ""), acc)))
    arm("control: layout sysctls registered from probe are caught",
        any("registers sysctls" in x for x in wiring_problems(acc.replace(
            '        if (!nvaccelLayoutAllows(this, provider, "NVAccel")) return NULL;\n',
            '        if (!nvaccelLayoutAllows(this, provider, "NVAccel")) return NULL;\n        nvaccelLayoutSysctls();\n'))))
    arm("control: layout sysctls not registered with the others are caught",
        any("nvaccelInstallSysctl" in x for x in sysctl_problems(lay, acc.replace("    nvaccelLayoutSysctls();\n", ""))))
    arm("control: every table handed to the verdict is caught",
        any("handed something other" in x for x in verdict_problems(lay.replace(
            "                                                  mayRefuse, nMay, ioService.slots, ioService.built);",
            "                                                  vts, nVts, ioService.slots, ioService.built);"))))
    arm("control: a report that lets a longer table through is caught",
        any("same or shorter" in x for x in verdict_problems(lay.replace(
            "if (r[i] <= 0 && mayRefuse && n < cap)", "if (r[i] != 2 && mayRefuse && n < cap)"))))
    arm("control: a refusal on the longer count is caught",
        any("other than the verdict" in x for x in verdict_problems(lay.replace(
            "    gNvaccelLayoutState = (int)st;\n", "    if (vt.longer) return false;\n    gNvaccelLayoutState = (int)st;\n"))))
    arm("control: a second verdict computed from the tables is caught",
        any("nvLayoutVerdict() is called here" in x for x in verdict_problems(lay.replace(
            "    gNvaccelLayoutState = (int)st;\n",
            "    NvLayoutTally t2; nvLayoutTallyInit(&t2); nvLayoutVtRow(&vts[0], &t2);\n"
            "    gNvaccelLayoutState = (int)nvLayoutVerdict(&t2, false);\n"))))
    arm("control: the lengths published under the verdict's name are caught",
        any("nvaccel-layout is not the verdict text" in x for x in verdict_problems(lay.replace(
            'provider->setProperty("nvaccel-layout", gNvaccelLayout.run.text);', 'provider->setProperty("nvaccel-layout", vt.text);'))))
    arm("control: the instance's vtable read before the verdict is caught",
        any("before the verdict" in x for x in verdict_problems(lay.replace(
            "    const unsigned nVts = sizeof vts / sizeof vts[0];\n",
            "    const unsigned nVts = sizeof vts / sizeof vts[0];\n    const void *const *early = *(const void *const *const *)self; (void)early;\n"))))
    arm("control: a class with a virtual function of its own is caught",
        any("adds a slot" in x for x in virtual_problems(acc.replace(
            "class IOAccelNVRMCarrier : public IOService {\n", "class IOAccelNVRMCarrier : public IOService {\n    virtual void nvExtra();\n", 1), stubs)))

    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        (d / "t.cpp").write_text(PURE)

        def pure(out, *include_dirs):
            build = subprocess.run(["c++", "-std=c++17", "-Wall", "-Wextra", "-Werror", *[f"-I{i}" for i in include_dirs],
                                    str(d / "t.cpp"), "-o", str(d / out)], capture_output=True, text=True)
            if build.returncode != 0:
                return None, build.stderr[-800:]
            res = subprocess.run([str(d / out)], capture_output=True, text=True)
            return res.returncode, res.stderr[-600:]

        code, err = pure("t", ROOT / "kexts/NVRM/accel")
        arm("the kext's own headers on the host: longer is a report, shorter refuses, IOService is the self-check", code == 0, err)
        (d / "nvrm_layout.h").write_text((ROOT / "kexts/NVRM/nvrm_layout.h").read_text())
        for tag, old, new, what in (
                ("a", "if (r[i] <= 0 && mayRefuse && n < cap)", "if (r[i] != 2 && mayRefuse && n < cap)",
                 "a report that hands on a longer table"),
                ("b", "if (r[i] <= 0 && mayRefuse && n < cap)", "if (r[i] == 0 && mayRefuse && n < cap)",
                 "a report that keeps a shorter table from the verdict"),
                ("c", "    if (cal != 0) {\n", "    if (cal == 2) {\n", "a report that reads the tables without the IOService self-check"),
                ("d", "if (r[i] <= 0 && mayRefuse && n < cap)", "if (r[i] <= 0 && mayRefuse && n <= cap)", "a report that writes one entry past its output array")):
            sub = d / ("bad" + tag)
            (sub / "accel").mkdir(parents=True)
            changed = LAY.read_text().replace(old, new)
            (sub / "accel/nvaccel_layout.h").write_text(changed)
            (sub / "nvrm_layout.h").write_text((ROOT / "kexts/NVRM/nvrm_layout.h").read_text())
            code, _ = pure("t" + tag, sub / "accel")
            arm(f"control: {what} fails the host program", changed != LAY.read_text() and code is not None and code != 0)

    print("PASS" if not FAILS else f"FAILED ({len(FAILS)})")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
