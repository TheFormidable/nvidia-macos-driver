#!/usr/bin/env python3
"""The kernel half of channel recovery: escape 0xE8 and the Xid hook, as NVRM.kext wires them.

NVK rebuilds a faulted channel inside the process (nvk/nvk-channel-recover.patch, tools/test_channel_recover.py) and
sends the kext a note about it. The kext only counts: kexts/NVRM/nvrm_chan_note.cpp around the pure counting of
kexts/NVRM/nvrm_chan_note_count.h (run by tools/test_chan_note_count.c). What no host test can run is asserted here
on the source text:

- the Xid hook in nv_log_error runs with RM locks held and possibly with preemption off: the call is one statement,
  made once, from nv_log_error only, and everything it reaches is atomics: no lock, allocation, log line or wakeup;
- the escape goes through NVRMUserClient::esc (no new selector), answered first, before the connection's own gate
  and before the power gate (NvXnuPmUse) that holds every RM call during a GPU suspend and resume, so that a note is
  never parked behind either and can report the suspended state; it still refuses an adapter that is not up, the size
  is checked before the record is touched, the power state passed is the adapter's own, and the answer is
  kIOReturnSuccess;
- the note checks version and request before it counts or writes anything, and reads each field of the record once;
- the ten documented sysctls, read-only, each declared and registered once, never unregistered, behind a once
  guard, registered from NVRM's one-time sysctl block; their names are unique across NVRM's three counter modules
  (fence, DMA, channel notes) and NVRM.cpp;
- NVRM's power-state numbers are the ones the note's ABI promises.

Each arm has a negative control: the same check on the text with that one thing broken must fail.
Usage: test_channel_recover_kernel.py [driver checkout]"""
import pathlib
import re
import sys

root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1])

LEX = re.compile(r"""//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])'""", re.S)
NOTE_SYSCTLS = ["nvrm_xid_count", "nvrm_xid_last", "nvrm_chan_kicks", "nvrm_chan_recovered", "nvrm_chan_dropped",
                  "nvrm_chan_gave_up", "nvrm_chan_injected", "nvrm_chan_last_xid", "nvrm_chan_last_kind",
                  "nvrm_chan_last_why"]
# what nothing reachable from the Xid hook or the note may do
FORBIDDEN = (r"\bIOLock\w*\s*\(|\bIOSimpleLock\w*\s*\(|\blck_\w+\s*\(|\bIOLog\s*\(|\bkprintf\s*\(|\bprintf\s*\(|"
             r"\bIOMalloc\w*\s*\(|\bkern_os_malloc\s*\(|\bmalloc\s*\(|\bnew\b|\bthread_wakeup\w*\s*\(|\bwakeup\s*\(|"
             r"\bassert_wait\w*\s*\(|\bIOSleep\s*\(|\bIODelay\s*\(|\bthread_block\s*\(|\brm_\w+\s*\(|\bsysctl_register_oid\s*\(")
MODULES = ["kexts/NVRM/nvrm_fence.cpp", "kexts/NVRM/nvrm_dma.cpp", "kexts/NVRM/nvrm_chan_note.cpp"]


def code_only(text):
    """Comments blanked and string contents blanked; offsets unchanged."""
    def sub(m):
        s = m.group(0)
        if s[0] in "\"'":
            return s[0] + re.sub(r"[^\n]", " ", s[1:-1]) + s[-1]
        return re.sub(r"[^\n]", " ", s)
    return LEX.sub(sub, text)


def squash(s):
    return " ".join(s.split())


def functions(code):
    """name -> (parameters, body) of every function defined at the top level of the text (or of a class body)."""
    out, depth, start = {}, 0, 0
    for i, c in enumerate(code):
        if c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                m = re.search(r"([\w:]+)\s*\(([^(){};]*)\)\s*(?:const\s*)?$", code[:start])
                if m:
                    out[m.group(1)] = (squash(m.group(2)), code[start + 1:i])
    return out


def block_after(code, pos):
    """The text of the braced block that starts at the first '{' at or after pos."""
    i = code.index("{", pos)
    depth = 0
    for j in range(i, len(code)):
        if code[j] == "{":
            depth += 1
        elif code[j] == "}":
            depth -= 1
            if depth == 0:
                return code[i + 1:j]
    return ""


def sysctl_names(code):
    return re.findall(r"\bSYSCTL_(?:INT|UINT|QUAD|LONG|ULONG|PROC|STRING|OPAQUE|NODE)\s*\(\s*_debug\s*,\s*\w+\s*,\s*(\w+)\s*,", code)


def arms(t):
    """t: path -> text. Yields (name, ok)."""
    cpp = code_only(t["kexts/NVRM/nvrm_chan_note.cpp"])
    hdr = code_only(t["kexts/NVRM/nvrm_chan_note_count.h"])
    abi = code_only(t["kexts/NVRM/nvrm_chan_note_abi.h"])
    nvrm = code_only(t["kexts/NVRM/NVRM.cpp"])
    osx = code_only(t["kexts/NVRM/os-xnu.cpp"])
    fc, fh = functions(cpp), functions(hdr)
    out = []

    def arm(name, ok):
        out.append((name, bool(ok)))

    # ---- the three functions of the ABI header ----
    want = {"nvrm_chan_note_xid": "uint32_t xid", "nvrm_chan_note_handle": "struct NVRMChanNoteEsc *e, uint32_t pm",
            "nvrm_chan_note_sysctls": "void"}
    arm("the module defines the three functions of nvrm_chan_note_abi.h with C linkage and their parameters",
        all(f in fc and fc[f][0] == p for f, p in want.items())
        and all(re.search(r'extern\s+"C"\s+void\s+' + f + r"\s*\(", t["kexts/NVRM/nvrm_chan_note.cpp"]) for f in want)
        and all(re.search(r"\bvoid\s+" + f + r"\s*\(" + re.escape(p).replace(r"\ ", r"\s*") + r"\s*\)\s*;", abi)
                for f, p in want.items()))

    # ---- the Xid hook: any context ----
    xb = fc.get("nvrm_chan_note_xid", ("", ""))[1]
    cx = fh.get("nvrm_cn_xid", ("", ""))[1]
    arm("the Xid hook: its body is one call of the header's counter, which is atomics only",
        squash(xb) == "nvrm_cn_xid(&gCn, xid);"
        and re.sub(r"__atomic_(?:add_fetch|store_n)\s*\([^;]*\)\s*;", "", cx).strip() == "")
    arm("the Xid hook and the note: nothing in the module or the counting header locks, allocates, logs, sleeps or wakes",
        not re.search(FORBIDDEN, cpp.replace(fc.get("nvrm_chan_note_sysctls", ("", ""))[1], ""))
        and not re.search(FORBIDDEN, hdr))
    arm("the counting header includes only the ABI header and defines only static inline functions",
        re.findall(r"#\s*include\s*([<\"][^>\"]+[>\"])", t["kexts/NVRM/nvrm_chan_note_count.h"]) == ['"nvrm_chan_note_abi.h"']
        and all(re.search(r"static\s+inline\s+[\w\s\*]+\b" + f + r"\s*\(", hdr) for f in fh))

    nle_at = re.search(r"\bNV_STATUS\s+NV_API_CALL\s+nv_log_error\s*\([^;{]*\)\s*\{", osx)
    nle = block_after(osx, nle_at.end() - 1) if nle_at else ""
    arm("os-xnu.cpp: nv_log_error counts the Xid once, before its log lines, with the RM's number",
        len(re.findall(r"\bnvrm_chan_note_xid\s*\(", nle)) == 1
        and re.search(r"\bnvrm_chan_note_xid\s*\(\s*error_number\s*\)\s*;", nle)
        and nle.find("nvrm_chan_note_xid") < nle.find("kprintf") and nle.find("nvrm_chan_note_xid") < nle.find("IOLog")
        and '#include "nvrm_chan_note_abi.h"' in t["kexts/NVRM/os-xnu.cpp"])
    others = [p for p in t if p.startswith("kexts/") and p not in ("kexts/NVRM/os-xnu.cpp", "kexts/NVRM/nvrm_chan_note.cpp",
                                                                    "kexts/NVRM/nvrm_chan_note_abi.h")]
    arm("nothing else in kexts/ calls the Xid hook",
        len(re.findall(r"\bnvrm_chan_note_xid\s*\(", osx)) == 1
        and not any(re.search(r"\bnvrm_chan_note_xid\s*\(", code_only(t[p])) for p in others))

    # ---- the note ----
    nb = fh.get("nvrm_cn_note", ("", ""))[1]
    first_if = re.search(r"\bif\s*\(\s*version\s*!=\s*NVRM_CHAN_NOTE_VERSION\s*\|\|\s*what\s*>=\s*NVRM_CN_WHAT_COUNT\s*\)\s*return\s+0\s*;", nb)
    first_effect = re.search(r"__atomic_\w+\s*\(|\be->\w+\s*=[^=]", nb)
    arm("the note: version and request are checked before anything is counted or written",
        first_if and first_effect and first_if.start() < first_effect.start())
    reads = re.findall(r"\be->(\w+)\b(?!\s*=[^=])", nb)
    arm("the note: each input field of the record is read once, into a local, before the checks",
        sorted(reads) == sorted(["version", "what", "xid", "dropped", "kind", "why"])
        and all(nb.find("e->" + f) < (first_if.start() if first_if else -1) for f in reads))
    arm("the note: the dropped count is capped, kind and reason are range-checked",
        re.search(r"dropped\s*<\s*NVRM_CN_DROPPED_MAX\s*\?\s*dropped\s*:\s*NVRM_CN_DROPPED_MAX", nb)
        and re.search(r"nvrm_cn_enum\s*\(\s*kind\s*,\s*NVRM_CN_KIND_MAX\s*\)", nb)
        and re.search(r"nvrm_cn_enum\s*\(\s*why\s*,\s*NVRM_CN_WHY_MAX\s*\)", nb)
        and re.search(r"#define\s+NVRM_CN_DROPPED_MAX\s+64u", hdr))
    hb = fc.get("nvrm_chan_note_handle", ("", ""))[1]
    arm("the escape body: a NULL record does nothing, otherwise the header's note",
        squash(hb) == "if (!e) return; (void)nvrm_cn_note(&gCn, e, pm);")

    # ---- the escape case in NVRM.cpp ----
    esc_at = re.search(r"IOReturn\s+NVRMUserClient::esc\s*\(\s*uint32_t\s+cmd\s*,\s*void\s*\*\s*data\s*,\s*uint32_t\s+size\s*\)", nvrm)
    esc = block_after(nvrm, esc_at.end()) if esc_at else ""
    case = re.search(r"\bif\s*\(\s*cmd\s*==\s*NVRM_ESC_CHAN_NOTE\s*\)\s*\{", esc)
    cbody = squash(block_after(esc, case.start())) if case else ""
    cgate = re.search(r"\bGate\s+\w+\s*\(\s*fGate\s*\)\s*;", esc)
    gate = re.search(r"\bNvXnuPmUse\s+\w+\s*;", esc)
    sw = esc.find("switch (cmd)")
    arm("NVRM.cpp: NVRMUserClient::esc answers NVRM_ESC_CHAN_NOTE first, before the connection's gate, the power gate "
        "(NvXnuPmUse) and the escape switch, and nowhere else",
        case and cgate and gate and sw >= 0 and case.start() < cgate.start() < gate.start() < sw
        and not re.search(r"[;{}]", esc[:case.start()])
        and len(re.findall(r"\bNvXnuPmUse\b", esc)) == 1 and len(re.findall(r"\bfGate\b", esc)) == 1
        and not re.search(r"case\s+NVRM_ESC_CHAN_NOTE\b", esc))
    arm("NVRM.cpp: the note refuses an adapter that is not up, checks the exact size first, passes the adapter's "
        "power state, answers success",
        cbody == "if (!fOwner || fOwner->fPassDone < 2) return kIOReturnNotReady; "
                 "if (size != sizeof(struct NVRMChanNoteEsc)) return kIOReturnBadArgument; "
                 "nvrm_chan_note_handle((struct NVRMChanNoteEsc *)data, (uint32_t)fOwner->fGpuPm); return kIOReturnSuccess;")
    arm("NVRM.cpp: the escape number appears only in that case (no selector, no capability bit) and the header is included",
        len(re.findall(r"\bNVRM_ESC_CHAN_NOTE\b", nvrm)) == 1 and len(re.findall(r"\bnvrm_chan_note_handle\s*\(", nvrm)) == 1
        and '#include "nvrm_chan_note_abi.h"' in t["kexts/NVRM/NVRM.cpp"])
    pm = re.search(r"enum\s*\{\s*kGpuPmRunning\s*=\s*(\d+)\s*,\s*kGpuPmSuspended\s*=\s*(\d+)\s*,\s*kGpuPmLost\s*=\s*(\d+)\s*\}", nvrm)
    cn = re.search(r"NVRM_CN_PM_RUNNING\s*=\s*(\d+)\s*,\s*NVRM_CN_PM_SUSPENDED\s*=\s*(\d+)\s*,\s*NVRM_CN_PM_LOST\s*=\s*(\d+)", abi)
    arm("NVRM.cpp: the power states are the numbers the note's ABI promises",
        pm and cn and pm.groups() == cn.groups())

    # ---- sysctls ----
    names = sysctl_names(cpp)
    decls = re.findall(r"\bSYSCTL_\w+\s*\(\s*_debug\s*,\s*\w+\s*,\s*\w+\s*,\s*([^,]+),", cpp)
    arm("the ten documented sysctls, each declared once, all read-only",
        sorted(names) == sorted(NOTE_SYSCTLS) and len(decls) == 10
        and all(re.fullmatch(r"\s*CTLFLAG_RD\s*\|\s*CTLFLAG_LOCKED\s*", d) for d in decls))
    sb = fc.get("nvrm_chan_note_sysctls", ("", ""))[1]
    st = [s for s in re.split(r";", squash(sb)) if s.strip()]
    regs = re.findall(r"sysctl_register_oid\s*\(\s*&\s*sysctl__debug_(\w+)\s*\)", sb)
    arm("the sysctls are registered once each, behind a once guard that is the first statement",
        st and re.fullmatch(r"if \(!OSCompareAndSwap\(0, 1, &gCnSysctls\)\) return", st[0].strip())
        and sorted(regs) == sorted(NOTE_SYSCTLS)
        and len(re.findall(r"\bsysctl_register_oid\s*\(", cpp)) == 10)
    blk = re.search(r"if\s*\(\s*!\s*sysctlsUp\s*\)\s*\{", nvrm)
    once = block_after(nvrm, blk.start()) if blk else ""
    arm("NVRM.cpp registers them from its one-time sysctl block, and nowhere else",
        len(re.findall(r"\bnvrm_chan_note_sysctls\s*\(\s*\)\s*;", nvrm)) == 1
        and re.search(r"\bnvrm_chan_note_sysctls\s*\(\s*\)\s*;", once)
        and once.find("sysctlsUp = true") < once.find("nvrm_chan_note_sysctls"))
    mods = {p: code_only(t[p]) for p in MODULES + ["kexts/NVRM/NVRM.cpp"] if p in t}
    every = [n for p in mods for n in sysctl_names(mods[p])]
    arm("the fence, DMA and channel-note modules and NVRM.cpp: every debug sysctl name is declared once",
        len(MODULES) == len([p for p in MODULES if p in t]) and len(every) == len(set(every)) and len(every) > 10)
    arm("the fence, DMA and channel-note modules: each of their sysctls is registered exactly once and none is ever unregistered",
        all(len(re.findall(r"\bsysctl_register_oid\s*\(\s*&\s*sysctl__debug_" + n + r"\s*\)", mods[p])) == 1
            for p in MODULES for n in sysctl_names(mods[p]))
        and not any(re.search(r"\bsysctl_unregister_oid\s*\(", mods[p]) for p in mods))
    return out


def broken(text, old, new):
    assert text.count(old) == 1, "control anchor not found once: %r" % old[:60]
    return text.replace(old, new)


CPP, HDR, NVRM, OSX = ("kexts/NVRM/nvrm_chan_note.cpp", "kexts/NVRM/nvrm_chan_note_count.h", "kexts/NVRM/NVRM.cpp",
                       "kexts/NVRM/os-xnu.cpp")
XID_HOOK = "    nvrm_chan_note_xid(error_number);\n"
CASE_SIZE = "        if (size != sizeof(struct NVRMChanNoteEsc)) return kIOReturnBadArgument;\n"
CASE_CALL = "        nvrm_chan_note_handle((struct NVRMChanNoteEsc *)data, (uint32_t)fOwner->fGpuPm);\n"
CASE_UP = "        if (!fOwner || fOwner->fPassDone < 2) return kIOReturnNotReady;\n"
NOTE_IF = ("    if (cmd == NVRM_ESC_CHAN_NOTE) {\n" + CASE_UP + CASE_SIZE + CASE_CALL + "        return kIOReturnSuccess;\n    }\n")
CHECK = "   if (version != NVRM_CHAN_NOTE_VERSION || what >= NVRM_CN_WHAT_COUNT)\n      return 0;\n"
CONTROLS = [
    ("an IOLog in the Xid hook", "locks, allocates, logs", CPP,
     lambda s: broken(s, "    nvrm_cn_xid(&gCn, xid);\n", '    nvrm_cn_xid(&gCn, xid);\n    IOLog("xid %u\\n", xid);\n')),
    ("a lock in the counting header", "locks, allocates, logs", HDR,
     lambda s: broken(s, "   e->status = NVRM_CN_OK;\n", "   e->status = NVRM_CN_OK;\n   IOLockLock(gL);\n")),
    ("an Xid counter that is not atomic", "atomics only", HDR,
     lambda s: broken(s, "   __atomic_add_fetch(&c->xidCount, 1, __ATOMIC_RELAXED);\n   __atomic_store_n(&c->xidLast",
                      "   c->xidCount++;\n   __atomic_store_n(&c->xidLast")),
    ("a header that takes a system header", "includes only the ABI header", HDR,
     lambda s: broken(s, '#include "nvrm_chan_note_abi.h"\n', '#include "nvrm_chan_note_abi.h"\n#include <string.h>\n')),
    ("the Xid hook after the log lines", "counts the Xid once, before", OSX,
     lambda s: broken(s, XID_HOOK, "").replace("    return NV_OK;\n}", "    nvrm_chan_note_xid(error_number);\n    return NV_OK;\n}", 1)),
    ("the Xid hook gone", "counts the Xid once, before", OSX, lambda s: broken(s, XID_HOOK, "")),
    ("the Xid hook called a second time elsewhere", "nothing else in kexts/ calls", OSX,
     lambda s: s + "\nstatic void again(void) { nvrm_chan_note_xid(0); }\n"),
    ("the note counts before it checks the version", "checked before anything", HDR,
     lambda s: broken(s, CHECK, "   __atomic_add_fetch(&c->kicks, 1, __ATOMIC_RELAXED);\n" + CHECK)),
    ("the note reads a field of the record a second time", "read once", HDR,
     lambda s: broken(s, "   if (xid != 0)\n", "   if (e->xid != 0)\n")),
    ("the dropped count without its cap", "capped", HDR,
     lambda s: broken(s, "(int64_t)(dropped < NVRM_CN_DROPPED_MAX ? dropped : NVRM_CN_DROPPED_MAX)", "(int64_t)dropped")),
    ("a kind stored without its range check", "range-checked", HDR,
     lambda s: broken(s, "nvrm_cn_enum(kind, NVRM_CN_KIND_MAX)", "(int32_t)kind")),
    ("the size check after the call", "checks the exact size first", NVRM,
     lambda s: broken(s, CASE_SIZE + CASE_CALL, CASE_CALL + CASE_SIZE)),
    ("a size check that accepts a larger record", "checks the exact size first", NVRM,
     lambda s: broken(s, CASE_SIZE, CASE_SIZE.replace("!=", "<"))),
    ("a power state that is not the adapter's", "passes the adapter's power state", NVRM,
     lambda s: broken(s, CASE_CALL, CASE_CALL.replace("(uint32_t)fOwner->fGpuPm", "0"))),
    ("the note answered after the power gate again", "the power gate (NvXnuPmUse) and the escape switch", NVRM,
     lambda s: broken(broken(s, NOTE_IF, ""), "    NvXnuPmUse pmUse;\n    switch (cmd) {\n",
                      "    NvXnuPmUse pmUse;\n" + NOTE_IF + "    switch (cmd) {\n")),
    ("the note back inside the escape switch", "the power gate (NvXnuPmUse) and the escape switch", NVRM,
     lambda s: broken(broken(s, NOTE_IF, ""), "    default: {\n        NV_STATUS st = rm_ioctl(",
                      NOTE_IF.replace("    if (cmd == NVRM_ESC_CHAN_NOTE) {", "    case NVRM_ESC_CHAN_NOTE: {")
                      + "    default: {\n        NV_STATUS st = rm_ioctl(")),
    ("the note answered behind the connection's gate", "before the connection's gate", NVRM,
     lambda s: broken(broken(s, NOTE_IF, ""), "    NvMapPendingScope pendingScope;", NOTE_IF + "    NvMapPendingScope pendingScope;")),
    ("the note without its adapter check", "refuses an adapter that is not up", NVRM, lambda s: broken(s, CASE_UP, "")),
    ("a selector row for the note", "appears only in that case", NVRM,
     lambda s: s + "\nstatic const unsigned kNoteSel = NVRM_ESC_CHAN_NOTE;\n"),
    ("power-state numbers that moved", "the numbers the note's ABI promises", NVRM,
     lambda s: broken(s, "kGpuPmSuspended = 1, kGpuPmLost = 2", "kGpuPmSuspended = 2, kGpuPmLost = 1")),
    ("a writable sysctl", "all read-only", CPP,
     lambda s: broken(s, "nvrm_chan_kicks, CTLFLAG_RD | CTLFLAG_LOCKED", "nvrm_chan_kicks, CTLFLAG_RW | CTLFLAG_LOCKED")),
    ("a sysctl outside the documented ten", "the ten documented sysctls", CPP,
     lambda s: broken(s, "nvrm_chan_injected, CTLFLAG_RD", "nvrm_chan_injects, CTLFLAG_RD")),
    ("a sysctl registered twice", "registered once each", CPP,
     lambda s: broken(s, "    sysctl_register_oid(&sysctl__debug_nvrm_chan_kicks);\n",
                      "    sysctl_register_oid(&sysctl__debug_nvrm_chan_kicks);\n    sysctl_register_oid(&sysctl__debug_nvrm_chan_kicks);\n")),
    ("registration without the once guard", "registered once each", CPP,
     lambda s: broken(s, "    if (!OSCompareAndSwap(0, 1, &gCnSysctls)) return;\n", "")),
    ("registration outside NVRM's one-time block", "one-time sysctl block", NVRM,
     lambda s: broken(s, "            nvrm_chan_note_sysctls();\n", "").replace(
         "    UInt8 bus = fPCI->getBusNumber()", "    nvrm_chan_note_sysctls();\n    UInt8 bus = fPCI->getBusNumber()", 1)),
    ("a name the fence module declares too", "every debug sysctl name is declared once", CPP,
     lambda s: broken(s, "nvrm_chan_injected, CTLFLAG_RD", "nvrm_fence_kicks, CTLFLAG_RD")),
    ("an unregistration at stop", "none is ever unregistered", CPP,
     lambda s: s + "\nextern \"C\" void nvrm_chan_note_gone(void) { sysctl_unregister_oid(&sysctl__debug_nvrm_chan_kicks); }\n"),
]


def main():
    paths = set(MODULES + [CPP, HDR, NVRM, OSX, "kexts/NVRM/nvrm_chan_note_abi.h"])
    paths |= {str(p.relative_to(root)) for p in (root / "kexts").rglob("*")
              if p.is_file() and p.suffix in (".c", ".cpp", ".h", ".m", ".mm", ".inc")}
    t = {p: (root / p).read_text(errors="replace") for p in sorted(paths) if (root / p).is_file()}
    missing = [p for p in [CPP, HDR, NVRM, OSX] + MODULES if p not in t]
    if missing:
        print("FAIL missing: " + ", ".join(missing))
        return 1
    fails = 0
    for name, ok in arms(t):
        print(("ok   " if ok else "FAIL ") + name)
        fails += 0 if ok else 1
    for name, piece, path, mutate in CONTROLS:
        try:
            u = dict(t)
            u[path] = mutate(t[path])
            res = arms(u)
        except AssertionError as e:
            print("FAIL control '%s': %s" % (name, e))
            fails += 1
            continue
        hit = [a for a, ok in res if not ok and piece in a]
        print(("ok   " if hit else "FAIL ") + "control: %s is caught" % name)
        fails += 0 if hit else 1
    print("PASS" if not fails else "%d failed" % fails)
    return 1 if fails else 0


sys.exit(main())
