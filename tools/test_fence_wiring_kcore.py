#!/usr/bin/env python3
"""Where kexts/NVRM/NVRM.cpp calls the surface fence (kexts/NVRM/nvrm_fence.h, body in nvrm_fence.cpp).

The fence keeps, in the kernel, which surfaces a process is still drawing, and reads their completion from the
writing channel's own semaphore page. NVRM.cpp only connects it: the user-client selector, the function table NVAccel
fetches, the start and stop per GPU, sleep and wake, a dying process, and the kick after each interrupt. Each of those
is right in one place only; what is asserted on the source text:

- row 12 is the fence selector with the exact request size in and out, rows 0..8 are 2.9's; its body never takes the
  client's gate (a wait in it sleeps up to 2 s), refuses a request of another size or one in a descriptor, checks the
  version before the fence sees the request, and hands the fence the client's owner word and its "closing" word;
- "nvFenceOps" is answered before the power-management admission of callPlatformFunction, from the fence's table;
- go() starts the fence once per GPU, after the adapter is up and after the power manager; stop() and shutdownFull()
  stop it as their first step after the power manager, only for a GPU whose start reached it, once;
- gpuSuspend tells it right after the power manager, before the user channels stop; gpuResume tells it after the
  power manager and the RC timer are resumed, RESUMED or LOST, and only when the suspend did;
- a closing client's fence owner is handed back after the one-winner test and BEFORE the RM frees the client's
  channels; the owner word is never written by NVRM.cpp;
- the kick follows rm_isr in the interrupt action (whatever it answered) and rm_isr_bh in the bottom half, which both
  bottom-half variants (own thread, inline on the work loop) run; never in the primary interrupt filter.

Each arm has a negative control: the same check on the text with that one thing broken must fail.
Usage: test_fence_wiring_kcore.py [driver checkout]"""
import pathlib
import re
import sys

root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1])

LEX = re.compile(r"""//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])'""", re.S)
ROWS_29 = ["{ sGo, 1, 0, 0, 0 }", "{ sEsc, 1, kIOUCVariableStructureSize, 0, kIOUCVariableStructureSize }",
           "{ sGetFd, 0, 0, 1, 0 }", "{ sBindFd, 1, 0, 0, 0 }", "{ sRetired, 0, 0, 0, 0 }", "{ sRetired, 0, 0, 0, 0 }",
           "{ sWaitEvent, 2, 0, 1, 0 }", "{ sCaps, 0, 0, 1, 0 }", "{ sForgetToken, 1, 0, 0, 0 }"]
STARTED_ON = r"OSCompareAndSwap\s*\(\s*0\s*,\s*1\s*,\s*&\s*fFenceStarted\s*\)"
STARTED_OFF = r"OSCompareAndSwap\s*\(\s*1\s*,\s*0\s*,\s*&\s*(?:s\s*->\s*)?fFenceStarted\s*\)"


def code_only(text):
    """Comments blanked and string contents blanked; offsets unchanged."""
    def sub(m):
        s = m.group(0)
        if s[0] in "\"'":
            return s[0] + re.sub(r"[^\n]", " ", s[1:-1]) + s[-1]
        return re.sub(r"[^\n]", " ", s)
    return LEX.sub(sub, text)


def body(code, name):
    """The text between the braces of the definition of `name`, or ''."""
    m = re.search(r"(?<![\w:])" + re.escape(name) + r"\s*\([^(){};]*\)\s*(?:const\s*)?\{", code)
    if not m:
        return ""
    depth = 0
    for i in range(m.end() - 1, len(code)):
        if code[i] == "{":
            depth += 1
        elif code[i] == "}":
            depth -= 1
            if depth == 0:
                return code[m.end():i]
    return ""


def squash(s):
    return " ".join(s.split())


def statements(b):
    """The top-level statements of a function body, squashed (a braced block counts as part of its statement)."""
    out, depth, start = [], 0, 0
    for i, c in enumerate(b):
        if c in "({[":
            depth += 1
        elif c in ")}]":
            depth -= 1
            if c == "}" and depth == 0:
                rest = b[i + 1:].lstrip()
                if not rest.startswith("else"):
                    out.append(squash(b[start:i + 1]))
                    start = i + 1
        elif c == ";" and depth == 0:
            out.append(squash(b[start:i + 1]))
            start = i + 1
    return [s for s in out if s]


def table_rows(code):
    b = body(code, "NVRMUserClient::externalMethod")
    m = re.search(r"IOExternalMethodDispatch\s+tbl\s*\[\s*\]\s*=\s*\{", b)
    if not m:
        return []
    rows, depth, start = [], 0, None
    for i in range(m.end() - 1, len(b)):
        c = b[i]
        if c == "{":
            depth += 1
            if depth == 2:
                start = i
        elif c == "}":
            depth -= 1
            if depth == 1 and start is not None:
                rows.append(squash(b[start:i + 1]))
                start = None
            if depth == 0:
                break
    return rows


def at(text, pattern, start=0):
    m = re.compile(pattern).search(text, start)
    return m.start() if m else -1


def order(text, patterns):
    """True when each pattern occurs, each after the previous one."""
    pos = 0
    for p in patterns:
        m = re.compile(p).search(text, pos)
        if not m:
            return False
        pos = m.end()
    return True


def guarded(text, call, guard):
    """Every `call(` in text is the statement of an `if (guard)` (and there is at least one)."""
    calls = re.findall(r"\b" + call + r"\s*\(", text)
    good = re.findall(r"if\s*\(\s*" + guard + r"\s*\)\s*" + call + r"\s*\(", text)
    return len(calls) >= 1 and len(calls) == len(good)


def arms(src, header):
    code = code_only(src)
    out = []

    def arm(name, ok):
        out.append((name, bool(ok)))

    arm("NVRM.cpp includes nvrm_fence.h", re.search(r'#include\s+"nvrm_fence\.h"', src) is not None)

    sel = re.search(r"#define\s+NVRM_FENCE_SELECTOR\s+(\d+)u", header)
    rows = table_rows(code)
    want = "{ sFence, 0, sizeof(struct NVRMFenceReq), 0, sizeof(struct NVRMFenceReq) }"
    arm("the fence row sits at NVRM_FENCE_SELECTOR (12) with the request's exact size in and out, once",
        sel is not None and int(sel.group(1)) == 12 and len(rows) > 12 and rows[12] == want and rows.count(want) == 1)
    arm("rows 0..8 of the selector table are 2.9's", rows[:9] == ROWS_29)

    f = body(code, "NVRMUserClient::sFence")
    arm("the fence selector never takes the client's gate or the RM's power admission",
        f != "" and not re.search(r"\b(?:Gate|fGate|IOLockLock|NvXnuPmUse|nv_xnu_pm_enter)\b", f))
    arm("the fence selector refuses a request in a descriptor, or of any other size, before it reads it",
        order(f, [r"structureInputDescriptor\s*\|\|\s*args\s*->\s*structureOutputDescriptor\s*\)\s*return\s+kIOReturnBadArgument",
                  r"structureInputSize\s*!=\s*sizeof\s*\(\s*struct\s+NVRMFenceReq\s*\)\s*\)\s*return\s+kIOReturnBadArgument",
                  r"structureOutputSize\s*!=\s*sizeof\s*\(\s*struct\s+NVRMFenceReq\s*\)\s*\)\s*return\s+kIOReturnBadArgument",
                  r"nvu_memcpy\s*\(\s*&r\s*,\s*args\s*->\s*structureInput\s*,\s*sizeof\s+r\s*\)"])
        and re.search(r"!\s*args\s*->\s*structureInput\b", f) is not None and re.search(r"!\s*args\s*->\s*structureOutput\b", f) is not None)
    arm("the fence selector checks the version on its own copy, then hands the fence the owner and closing words, and copies all of it back",
        order(f, [r"if\s*\(\s*r\.version\s*!=\s*NVRM_FENCE_ABI_VERSION\s*\)\s*r\.status\s*=\s*NVRM_FENCE_ST_BAD_VERSION\s*;",
                  r"else\s+nvrm_fence_user\s*\(\s*&me\s*->\s*fFenceOwner\s*,\s*&me\s*->\s*fCleaned\s*,\s*&r\s*\)\s*;",
                  r"nvu_memcpy\s*\(\s*args\s*->\s*structureOutput\s*,\s*&r\s*,\s*sizeof\s+r\s*\)\s*;",
                  r"return\s+kIOReturnSuccess\s*;"])
        and len(re.findall(r"\bnvrm_fence_user\s*\(", code)) == 1)

    cpf = body(code, "NVRM::callPlatformFunction")
    ops = at(cpf, r"if\s*\(\s*fn\s*&&\s*p1\s*&&\s*fn\s*->\s*isEqualTo\s*\(\s*NVRM_FENCE_FN_OPS\s*\)\s*\)\s*return\s+nvrm_fence_kernel_ops\s*\(\s*\(\s*struct\s+NVRMFenceKernelOps\s*\*\s*\)\s*p1\s*\)\s*\?\s*kIOReturnSuccess\s*:\s*kIOReturnUnsupported\s*;")
    guard = at(cpf, r"\bNvXnuPmUse\b\s+pmUse\s*\(\s*rmCall\s*\)")
    arm("nvFenceOps is answered from the fence's table, before the power-management admission", ops >= 0 and guard >= 0 and ops < guard)

    go = body(code, "NVRM::go")
    arm("go() starts the fence once per GPU, after rm_init_adapter and the power manager, guarded by the per-GPU flag",
        order(go, [r"\brm_init_adapter\s*\(", r"\bnvrm_power_start\s*\(\s*&fNv\s*\)\s*;", r"if\s*\(\s*" + STARTED_ON + r"\s*\)\s*nvrm_fence_start\s*\(\s*\)\s*;"])
        and len(re.findall(r"\bnvrm_fence_start\s*\(", code)) == 1)

    st = statements(body(code, "NVRM::stop"))
    arm("stop(): the fence is stopped by the second statement, right after nvrm_power_stop(), only where it was started",
        len(st) > 1 and st[0] == "nvrm_power_stop();" and re.fullmatch(r"if \( ?" + STARTED_OFF + r" ?\) nvrm_fence_stop\(\);", st[1]) is not None)
    sf = statements(body(code, "NVRM::shutdownFull"))
    arm("shutdownFull(): the fence is stopped first (after taking the object), only where it was started",
        len(sf) > 1 and re.fullmatch(r"NVRM \*s = \(NVRM \*\)p0;", sf[0]) is not None
        and re.fullmatch(r"if \( ?" + STARTED_OFF + r" ?\) nvrm_fence_stop\(\);", sf[1]) is not None)
    arm("every nvrm_fence_stop() is behind the one-winner test of the per-GPU flag (one stop per started GPU)",
        guarded(code, "nvrm_fence_stop", STARTED_OFF)
        and not re.search(r"\bfFenceStarted\s*(?:=(?!=)|\+\+|--|\+=|-=)", code.replace("volatile UInt32 fFenceStarted = 0;", "")))

    sus = body(code, "NVRM::gpuSuspend")
    arm("gpuSuspend: the fence is told right after the power manager and before the RC timer, the RM gate and the user channels",
        order(sus, [r"\bnvrm_power_suspend\s*\(\s*\)\s*;",
                    r"if\s*\(\s*fFenceStarted\s*&&\s*OSCompareAndSwap\s*\(\s*0\s*,\s*1\s*,\s*&\s*fFenceAsleep\s*\)\s*\)\s*nvrm_fence_pm\s*\(\s*NVRM_FENCE_PM_SUSPENDING\s*\)\s*;",
                    r"\bnv_xnu_rc_suspend\s*\(", r"\bnv_xnu_pm_close\s*\(", r"\brm_stop_user_channels\s*\("])
        and len(re.findall(r"\bnvrm_fence_pm\s*\(", sus)) == 1)
    res = body(code, "NVRM::gpuResume")
    arm("gpuResume: after the power manager and the RC timer are resumed, the fence is told RESUMED or LOST, only when the suspend told it",
        order(res, [r"\brm_power_management\s*\(", r"if\s*\(\s*ok\s*\)\s*nvrm_power_resume\s*\(\s*\)\s*;",
                    r"if\s*\(\s*ok\s*\)\s*nv_xnu_rc_resume\s*\(\s*&fNv\s*\)\s*;",
                    r"if\s*\(\s*OSCompareAndSwap\s*\(\s*1\s*,\s*0\s*,\s*&\s*fFenceAsleep\s*\)\s*\)\s*nvrm_fence_pm\s*\(\s*ok\s*\?\s*NVRM_FENCE_PM_RESUMED\s*:\s*NVRM_FENCE_PM_LOST\s*\)\s*;"])
        and len(re.findall(r"\bnvrm_fence_pm\s*\(", res)) == 1 and len(re.findall(r"\bnvrm_fence_pm\s*\(", code)) == 2)

    cc = body(code, "NVRMUserClient::clientClose")
    arm("clientClose: the fence owner is handed back after the one-winner test and BEFORE the RM frees the client's channels",
        order(cc, [r"if\s*\(\s*OSCompareAndSwap\s*\(\s*0\s*,\s*1\s*,\s*&fCleaned\s*\)\s*\)\s*\{", r"gNvClients\s*\[\s*fFd\s*\]\s*=\s*NULL",
                   r"\bnvrm_fence_owner_gone\s*\(\s*fFenceOwner\s*\)\s*;", r"\brm_cleanup_file_private\s*\("])
        and len(re.findall(r"\bnvrm_fence_owner_gone\s*\(", cc)) == 1)
    fr = body(code, "NVRMUserClient::free")
    arm("free(): a client whose close never ran the cleanup still hands its owner back, and only then",
        re.search(r"if\s*\(\s*OSCompareAndSwap\s*\(\s*0\s*,\s*1\s*,\s*&fCleaned\s*\)\s*\)\s*nvrm_fence_owner_gone\s*\(\s*fFenceOwner\s*\)\s*;", fr) is not None
        and len(re.findall(r"\bnvrm_fence_owner_gone\s*\(", code)) == 2)
    arm("the owner word starts at zero and NVRM.cpp never writes it (only the fence assigns it)",
        re.search(r"volatile\s+UInt64\s+fFenceOwner\s*=\s*0\s*;", code) is not None
        and not re.search(r"\bfFenceOwner\s*(?:=(?!=)|\+\+|--|\+=|-=)", code.replace("volatile UInt64 fFenceOwner = 0;", ""))
        and not re.search(r"&\s*(?:me\s*->\s*)?fFenceOwner\b(?![^;]*nvrm_fence_user)", code.replace("nvrm_fence_user(&me->fFenceOwner", "")))

    io = body(code, "NVRM::interruptOccurred")
    arm("the interrupt action kicks the fence right after rm_isr, whatever it answered",
        order(io, [r"\brm_isr\s*\(\s*NULL\s*,\s*&fNv\s*,\s*&needBottomHalf\s*\)\s*;", r"nv_xnu_isr_thread\s*=\s*NULL\s*;\s*nvrm_fence_kick\s*\(\s*\)\s*;",
                   r"if\s*\(\s*handled\s*\)"]))
    bh = body(code, "nvrmRunBottomHalf")
    arm("the bottom half kicks the fence right after rm_isr_bh",
        re.search(r"\brm_isr_bh\s*\(\s*NULL\s*,\s*nv\s*\)\s*;\s*nvrm_fence_kick\s*\(\s*\)\s*;", bh) is not None)
    arm("both bottom-half variants run that body: the thread, and the inline call on the work loop",
        re.search(r"\bnvrmRunBottomHalf\s*\(\s*&\s*s\s*->\s*fNv\s*\)", body(code, "NVRM::bottomHalfThread")) is not None
        and re.search(r"else\s+nvrmRunBottomHalf\s*\(\s*&fNv\s*\)", io) is not None
        and len(re.findall(r"\brm_isr_bh\s*\(", code)) == 1)
    arm("exactly two kicks, and none in the primary interrupt filter",
        len(re.findall(r"\bnvrm_fence_kick\s*\(", code)) == 2 and "nvrm_fence_kick" not in body(code, "NVRM::interruptFilter"))
    return out


def broken(src, old, new):
    assert src.count(old) >= 1, "control text not found: " + old
    return src.replace(old, new, 1)


ROW12 = "        { sFence, 0, sizeof(struct NVRMFenceReq), 0, sizeof(struct NVRMFenceReq) },"
OPS = ("    if (fn && p1 && fn->isEqualTo(NVRM_FENCE_FN_OPS))\n"
       "        return nvrm_fence_kernel_ops((struct NVRMFenceKernelOps *)p1) ? kIOReturnSuccess : kIOReturnUnsupported;\n")
START = "        if (OSCompareAndSwap(0, 1, &fFenceStarted)) nvrm_fence_start();\n"
STOP = "    if (OSCompareAndSwap(1, 0, &fFenceStarted)) nvrm_fence_stop();\n"
STOP_S = "    if (OSCompareAndSwap(1, 0, &s->fFenceStarted)) nvrm_fence_stop();\n"
SUSPEND = "    if (fFenceStarted && OSCompareAndSwap(0, 1, &fFenceAsleep)) nvrm_fence_pm(NVRM_FENCE_PM_SUSPENDING);\n"
RESUME = "    if (OSCompareAndSwap(1, 0, &fFenceAsleep)) nvrm_fence_pm(ok ? NVRM_FENCE_PM_RESUMED : NVRM_FENCE_PM_LOST);\n"
GONE = "        nvrm_fence_owner_gone(fFenceOwner);\n"
CLEANUP = "        if (fOwner) { NvXnuPmUse pmUse; rm_cleanup_file_private(NULL, nvState(), &fNvfp); }\n"

CONTROLS = [
    ("row 12 missing", "the fence row", lambda s: broken(s, ROW12, "")),
    ("row 12 with a variable size", "the fence row", lambda s: broken(s, ROW12, "        { sFence, 0, kIOUCVariableStructureSize, 0, kIOUCVariableStructureSize },")),
    ("the fence selector under the gate", "never takes the client's gate",
     lambda s: broken(s, "    struct NVRMFenceReq r;\n", "    Gate g(me->fGate);\n    struct NVRMFenceReq r;\n")),
    ("any input size accepted", "refuses a request", lambda s: broken(s, "args->structureInputSize != sizeof(struct NVRMFenceReq)", "args->structureInputSize < sizeof(struct NVRMFenceReq)")),
    ("descriptors accepted", "refuses a request", lambda s: broken(s, "    if (args->structureInputDescriptor || args->structureOutputDescriptor) return kIOReturnBadArgument;\n", "")),
    ("no version check", "checks the version", lambda s: broken(s, "    if (r.version != NVRM_FENCE_ABI_VERSION) r.status = NVRM_FENCE_ST_BAD_VERSION;\n    else nvrm_fence_user", "    nvrm_fence_user")),
    ("nvFenceOps behind the power admission", "nvFenceOps is answered",
     lambda s: broken(broken(s, OPS, ""), "            return nvrm_gpuva_alloc(g) ? kIOReturnIOError : kIOReturnSuccess;\n",
                      "            return nvrm_gpuva_alloc(g) ? kIOReturnIOError : kIOReturnSuccess;\n" + OPS)),
    ("fence started before the adapter is up", "go() starts the fence",
     lambda s: broken(broken(s, START, ""), "        bottomHalfStart();\n", "        bottomHalfStart();\n" + START)),
    ("fence started without the flag", "go() starts the fence", lambda s: broken(s, START, "        nvrm_fence_start();\n")),
    ("stop(): fence stopped late", "stop(): the fence is stopped",
     lambda s: broken(broken(s, STOP, ""), "    if (fPmJoined) { PMstop(); fPmJoined = false; }\n", "    if (fPmJoined) { PMstop(); fPmJoined = false; }\n" + STOP)),
    ("shutdownFull(): fence not stopped", "shutdownFull(): the fence is stopped", lambda s: broken(s, STOP_S, "")),
    ("an unguarded stop", "every nvrm_fence_stop()", lambda s: broken(s, STOP_S, "    nvrm_fence_stop();\n")),
    ("the flag written outside the one-winner test", "every nvrm_fence_stop()", lambda s: broken(s, STOP, STOP + "    fFenceStarted = 0;\n")),
    ("suspend tells the fence after the user channels stop", "gpuSuspend: the fence is told",
     lambda s: broken(broken(s, SUSPEND, ""), "    NV_STATUS cst = rm_stop_user_channels(NULL, &fNv);\n",
                      "    NV_STATUS cst = rm_stop_user_channels(NULL, &fNv);\n" + SUSPEND)),
    ("resume always says RESUMED", "gpuResume: after", lambda s: broken(s, "ok ? NVRM_FENCE_PM_RESUMED : NVRM_FENCE_PM_LOST", "NVRM_FENCE_PM_RESUMED")),
    ("resume tells the fence before the RC timer", "gpuResume: after",
     lambda s: broken(broken(s, RESUME, ""), "    if (ok) nv_xnu_rc_resume(&fNv);\n", RESUME + "    if (ok) nv_xnu_rc_resume(&fNv);\n")),
    ("owner handed back after the RM cleanup", "clientClose: the fence owner",
     lambda s: broken(broken(s, GONE, ""), CLEANUP, CLEANUP + GONE)),
    ("free() hands the owner back every time", "free(): a client", lambda s: broken(s, "    if (OSCompareAndSwap(0, 1, &fCleaned)) nvrm_fence_owner_gone(fFenceOwner);\n", "    nvrm_fence_owner_gone(fFenceOwner);\n")),
    ("NVRM.cpp writes the owner word", "the owner word", lambda s: broken(s, GONE, GONE + "        fFenceOwner = 0;\n")),
    ("interrupt kick only when handled", "the interrupt action kicks",
     lambda s: broken(broken(s, "    nvrm_fence_kick();\n    if (handled) gNvIsrHandled++;", "    if (handled) gNvIsrHandled++;"),
                      "    if (handled) gNvIsrHandled++;", "    if (handled) { gNvIsrHandled++; nvrm_fence_kick(); }")),
    ("bottom-half kick missing", "the bottom half kicks",
     lambda s: broken(s, "    nvrm_fence_kick();   // a channel error reaches its notifier here", "    // a channel error reaches its notifier here")),
    ("inline bottom half bypasses the body", "both bottom-half variants",
     lambda s: broken(s, "        } else nvrmRunBottomHalf(&fNv);", "        } else rm_isr_bh(NULL, &fNv);")),
    ("a kick in the primary filter", "exactly two kicks",
     lambda s: broken(s, "    unsigned long n = ++gNvIsrCount;", "    unsigned long n = ++gNvIsrCount;\n    nvrm_fence_kick();")),
]


def main():
    src = (root / "kexts/NVRM/NVRM.cpp").read_text()
    header = (root / "kexts/NVRM/nvrm_fence_abi.h").read_text()
    fails = 0
    for name, ok in arms(src, header):
        print(("ok   " if ok else "FAIL ") + name)
        fails += 0 if ok else 1
    for name, arm_prefix, mutate in CONTROLS:
        try:
            res = arms(mutate(src), header)
        except AssertionError as e:
            print("FAIL control '%s': %s" % (name, e))
            fails += 1
            continue
        hit = [a for a, ok in res if not ok and arm_prefix in a]
        print(("ok   " if hit else "FAIL ") + "control: %s is caught" % name)
        fails += 0 if hit else 1
    print("%d failed" % fails)
    return 1 if fails else 0


sys.exit(main())
