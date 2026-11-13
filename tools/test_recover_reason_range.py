#!/usr/bin/env python3
"""NVK's kinds of loss and reasons for giving a recovery up are numbered once and read in three places.

NVK numbers them (enum nvrm_rec_kind and enum nvrm_rec_why in nvk/nvk-channel-recover.patch). When it gives a channel
up it sends the two numbers to NVRM.kext in the channel note, and the Metal plugin reads them from NVK's statistics
for the line it logs about a lost device. The kext records a number it does not know as -1
(NVRM_CN_KIND_MAX and NVRM_CN_WHY_MAX in kexts/NVRM/nvrm_chan_note_count.h), and the plugin prints one it has no name
for as "?" (the two tables in plugin/nvmtl_loss.h). The three are built separately and each wrote its range by hand,
with a test of its own that compared it only with its own number: NVK gained a fifteenth reason and neither of the
others moved, and nothing failed. That reason ("the GPU is suspended": the waiter looks again) is never sent or
stored, so nothing was wrong; the next reason appended would have been logged as "not understood" by a kext and a
plugin of the same release.

What is checked: the kext's two limits and the plugin's two tables cover exactly the kinds, and the reasons that can
be sent or stored, that NVK's enums define; NVK's own name tables cover every value; the values of each enum are
0, 1, 2, ... without a gap; and a reason this test counts as never sent returns from the recovery before anything is
recorded (the same block tools/test_channel_recover.py holds). The arms at the end show that each rule fires.
Usage: test_recover_reason_range.py [driver checkout]"""
import pathlib
import re
import sys

ROOT = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1]
PATCH, KEXT, PLUGIN = "nvk/nvk-channel-recover.patch", "kexts/NVRM/nvrm_chan_note_count.h", "plugin/nvmtl_loss.h"
# Reasons that end a recovery attempt without giving the channel up: nothing is sent to the kext or kept for the plugin.
NEVER_SENT = {"NVRM_WHY_ASLEEP"}
fails = []


def arm(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (f"\n       {detail}" if detail and not ok else ""))
    if not ok:
        fails.append(name)


def added(patch):
    """the lines the patch adds, without their '+', so that an enum or a table reads as it does in NVK's tree"""
    return "\n".join(l[1:] for l in patch.split("\n") if l.startswith("+") and not l.startswith("+++"))


def enum(text, name):
    """{member: value} of `enum <name> { A = 0, B = 1, ... }`, or None when it is not written that way"""
    m = re.search(r"\benum\s+" + name + r"\s*\{([^}]*)\}", text)
    if not m:
        return None
    out = {}
    for part in m.group(1).split(","):
        part = part.strip()
        if not part:
            continue
        mm = re.fullmatch(r"([A-Z][A-Z0-9_]*)\s*=\s*(\d+)", part)
        if not mm:
            return None   # a member without its number: the numbering is no longer written out, so nothing to hold
        out[mm.group(1)] = int(mm.group(2))
    return out


def names(text, func):
    """how many strings the name table of `func` holds, or None"""
    m = re.search(r"\b" + func + r"\s*\([^)]*\)\s*\{(.*?)\n\}", text, re.S)
    if not m:
        return None
    t = re.search(r"\[\]\s*=\s*\{(.*?)\};", m.group(1), re.S)
    if not t:
        return None
    return len(re.findall(r'"(?:[^"\\]|\\.)*"', t.group(1)))


def limit(text, macro):
    m = re.search(r"^\s*#\s*define\s+" + macro + r"\s+(\d+)u?\s*$", text, re.M)
    return int(m.group(1)) if m else None


def early_return(nvk, member):
    """the recovery leaves with VK_SUCCESS for this reason before the first thing it would record after its sequence"""
    i = nvk.find("nvrm_rec_execute(&nvrm_rec_rm_ops")
    j = nvk.find(f"if (why == {member}) {{", i) if i >= 0 else -1
    k = nvk.find("if (why != NVRM_WHY_OK) {", i) if i >= 0 else -1
    if min(i, j, k) < 0 or not j < k:
        return False
    blk = nvk[j:k]
    return "return VK_SUCCESS;" in blk and not re.search(r"\brec_fail\s*\(|\brec_note\s*\(|\brec_stat_", blk)


def problems(patch, kext, plugin):
    """every way the three disagree, as sentences; empty when they agree"""
    nvk, out = added(patch), []
    kind, why = enum(nvk, "nvrm_rec_kind"), enum(nvk, "nvrm_rec_why")
    if kind is None or why is None:
        return ["NVK's enums nvrm_rec_kind and nvrm_rec_why are not found with every member's number written out"]
    for what, e in (("kind", kind), ("reason", why)):
        if sorted(e.values()) != list(range(len(e))):
            out.append(f"NVK's {what} numbers are not 0..{len(e) - 1} without a gap: {sorted(e.values())}")
    unknown = NEVER_SENT - set(why)
    if unknown:
        out.append(f"this test counts {sorted(unknown)} as never sent, and NVK has no such reason")
    sent = {k: v for k, v in why.items() if k not in NEVER_SENT}
    for member in sorted(NEVER_SENT & set(why)):
        if why[member] < max(sent.values()):
            out.append(f"{member} = {why[member]} lies below a reason that is sent: the kext and the plugin would "
                       "need a hole in their ranges")
        if not early_return(nvk, member):
            out.append(f"{member} is counted as never sent, but the recovery does not return with it before it "
                       "records or reports anything")
    kmax, wmax = max(kind.values()), max(sent.values())
    for macro, want, what in (("NVRM_CN_KIND_MAX", kmax, "kind NVK names"),
                              ("NVRM_CN_WHY_MAX", wmax, "reason NVK can send")):
        got = limit(kext, macro)
        if got != want:
            out.append(f"{KEXT}: {macro} is {got}, and the last {what} is {want}")
    for func, want, what in (("nvmtl_rec_kind_name", kmax + 1, "kinds"), ("nvmtl_rec_why_name", wmax + 1,
                                                                             "reasons NVK can store")):
        got = names(plugin, func)
        if got != want:
            out.append(f"{PLUGIN}: {func} has {got} names, and NVK has {want} {what}")
    for func, want in (("nvrm_rec_kind_name", len(kind)), ("nvrm_rec_why_name", len(why))):
        got = names(nvk, func)
        if got != want:
            out.append(f"{PATCH}: NVK's own {func} has {got} names for {want} values")
    return out


patch, kext, plugin = ((ROOT / p).read_text(errors="replace") for p in (PATCH, KEXT, PLUGIN))
found = problems(patch, kext, plugin)
arm("the kext's limits and the plugin's name tables cover exactly the kinds and reasons NVK can send", not found,
    "; ".join(found))

# ---- each rule fires
WHY_END, KIND_END = "NVRM_WHY_ASLEEP = 14 };", "NVRM_LOSS_UNKNOWN = 7 };"


def control(name, p=None, k=None, g=None, expect=""):
    got = problems(patch if p is None else p, kext if k is None else k, plugin if g is None else g)
    changed = (p is not None and p != patch) or (k is not None and k != kext) or (g is not None and g != plugin)
    arm("control: " + name, changed and any(expect in s for s in got),
        "the damaged copy is the same as the tree" if not changed else f"reported: {got}")


control("a reason appended to NVK's enum is reported for the kext and for the plugin",
        p=patch.replace(WHY_END, "NVRM_WHY_ASLEEP = 14, NVRM_WHY_NEW = 15 };"), expect="NVRM_CN_WHY_MAX is 13")
control("a kind appended to NVK's enum is reported",
        p=patch.replace(KIND_END, "NVRM_LOSS_UNKNOWN = 7, NVRM_LOSS_NEW = 8 };"), expect="NVRM_CN_KIND_MAX is 7")
control("a kext limit above what NVK can send is reported",
        k=kext.replace("#define NVRM_CN_WHY_MAX      13u", "#define NVRM_CN_WHY_MAX      14u"),
        expect="NVRM_CN_WHY_MAX is 14")
control("a plugin table that lost a name is reported",
        g=plugin.replace(', "the channel had already been given up" };', " };"),
        expect="nvmtl_rec_why_name has 13 names")
control("a plugin table with a name for a reason that is never stored is reported",
        g=plugin.replace('"the channel had already been given up" };',
                         '"the channel had already been given up", "the GPU was suspended" };'),
        expect="nvmtl_rec_why_name has 15 names")
ASLEEP_BLOCK = "+   if (why == NVRM_WHY_ASLEEP) {\n"
control("a reason that is counted as never sent but is reported like the others is caught",
        p=patch.replace(ASLEEP_BLOCK, ASLEEP_BLOCK + "+      rec_stat_set(REC_ST_LAST_WHY, why);\n"),
        expect="does not return with it")
control("and so is one whose block no longer leaves the recovery",
        p=patch.replace(ASLEEP_BLOCK, "+   if (why == NVRM_WHY_ASLEEP && retried) {\n"),
        expect="does not return with it")
control("a hole in NVK's numbering is reported",
        p=patch.replace("NVRM_WHY_GAVE_UP_BEFORE = 13, NVRM_WHY_ASLEEP = 14 };",
                        "NVRM_WHY_GAVE_UP_BEFORE = 13, NVRM_WHY_ASLEEP = 15 };"), expect="without a gap")
control("NVK's own table short of a name is reported",
        p=patch.replace(',\n+                                    "the GPU is suspended for a sleep or wake" };', " };"),
        expect="NVK's own nvrm_rec_why_name has 14 names")

print(f"{len(fails)} failed")
sys.exit(1 if fails else 0)
