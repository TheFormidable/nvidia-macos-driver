#!/usr/bin/env python3
"""The kernel half of the event wait (kexts/NVRM/NVRM.cpp, the numbers in kexts/NVRM/nvrm_evwait.h).

Every process may now sleep in user-client selector 6 during a long GPU wait, so the kernel side has to say why each
call returned, survive many processes closing while the RM posts events to them, and show with read-only counters
whether the wakes arrive. What is asserted, on the source text:

- selector 6 keeps 2.9's park (the waiter is queued before it re-reads the count, so no wake is lost) and answers the
  count in the low 32 bits, which is all a 2.9 user space reads, with the reason in bits 32..39; the sleeper ceiling
  answers CAPPED instead of looking like a wake; the 100 ms clamp stays;
- selector 7 says 3 (bit 0 as in 2.9, bit 1 = reasons and selector 9); selector 9 only counts, and refuses anything
  else; rows 0..8 are 2.9's, 9 is the note, 10 and 11 are retired placeholders that answer an error;
- a client close waits for event posts in flight AFTER the RM has freed the client's events (no new post can start
  then), and free() waits again and keeps the object rather than freeing it under a post that never finished; the
  wait sleeps only when a post is really in flight, and is bounded;
- the ten counters are read-only and registered.

Each arm has a negative control: the same check on a copy of the text with that one thing broken must fail.
Usage: test_evwait_wiring_kernel.py [driver checkout]"""
import pathlib
import re
import sys

root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1])

LEX = re.compile(r"""//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])'""", re.S)

SYSCTLS = ["nvrm_evt_matched", "nvrm_evt_slept", "nvrm_evt_woken", "nvrm_evt_timeouts", "nvrm_evt_capped",
           "nvrm_evt_clients", "nvrm_evt_post_drain_waits", "nvrm_evt_post_drain_stuck", "nvrm_evt_user_missed",
           "nvrm_evt_user_fallbacks"]
ROWS_29 = ["{ sGo, 1, 0, 0, 0 }", "{ sEsc, 1, kIOUCVariableStructureSize, 0, kIOUCVariableStructureSize }",
           "{ sGetFd, 0, 0, 1, 0 }", "{ sBindFd, 1, 0, 0, 0 }", "{ sRetired, 0, 0, 0, 0 }", "{ sRetired, 0, 0, 0, 0 }",
           "{ sWaitEvent, 2, 0, 1, 0 }", "{ sCaps, 0, 0, 1, 0 }", "{ sForgetToken, 1, 0, 0, 0 }"]


def code_only(text):
    """Comments blanked and string contents blanked; offsets unchanged."""
    def sub(m):
        s = m.group(0)
        if s[0] in "\"'":
            return s[0] + re.sub(r"[^\n]", " ", s[1:-1]) + s[-1]
        return re.sub(r"[^\n]", " ", s)
    return LEX.sub(sub, text)


def body(code, name):
    """The text between the braces of the definition of `name` (e.g. 'NVRMUserClient::free'), or ''."""
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


def table_rows(code):
    """The rows of NVRMUserClient's dispatch table, each squashed to one line."""
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


def before(text, first, second):
    a, b = text.find(first), text.find(second)
    return a >= 0 and b >= 0 and a < b


def arms(src):
    code = code_only(src)
    out = []

    def arm(name, ok):
        out.append((name, bool(ok)))

    arm("NVRM.cpp includes nvrm_evwait.h", re.search(r'#include\s+"nvrm_evwait\.h"', src) is not None)

    rows = table_rows(code)
    arm("rows 0..8 of the selector table are 2.9's, in 2.9's order", rows[:9] == ROWS_29)
    arm("row 9 is the note: one scalar in, nothing out", len(rows) > 9 and rows[9] == "{ sEvtNote, 1, 0, 0, 0 }")
    arm("rows 10 and 11 are retired placeholders (sRetired answers an error)",
        len(rows) > 11 and rows[10] == rows[11] == "{ sRetired, 0, 0, 0, 0 }"
        and re.search(r"NVRMUserClient::sRetired\s*\([^)]*\)\s*\{\s*return\s+kIOReturnUnsupported\s*;\s*\}", code) is not None)

    caps = body(code, "NVRMUserClient::sCaps")
    arm("selector 7 answers both bits (2.9's bit 0, and bit 1: reasons and selector 9)",
        re.search(r"scalarOutput\s*\[\s*0\s*\]\s*=\s*NVRM_EVW_CAP_WAIT\s*\|\s*NVRM_EVW_CAP_REASON\s*;", caps) is not None)

    w = body(code, "NVRMUserClient::sWaitEvent")
    arm("selector 6 keeps the 100 ms clamp", re.search(r"if\s*\(\s*us\s*>\s*100000\s*\)\s*us\s*=\s*100000\s*;", w) is not None)
    park = ["OSIncrementAtomic(&gNvEvtSleepers)", "assert_wait_timeout((event_t)&me->fEvtSeq, THREAD_ABORTSAFE",
            "if (me->fEvtSeq != seen) thread_wakeup((event_t)&me->fEvtSeq);", "thread_block(THREAD_CONTINUE_NULL)",
            "OSDecrementAtomic(&gNvEvtSleepers);"]
    sw = squash(w)
    at = 0
    for p in park:   # each step after the one before it
        at = sw.find(squash(p), at) if at >= 0 else -1
    arm("selector 6 parks as 2.9 did: counted, queued, count re-read, blocked, uncounted on every way out of the block",
        at >= 0
        and re.search(r"thread_block\s*\(\s*THREAD_CONTINUE_NULL\s*\)\s*;\s*OSDecrementAtomic\s*\(\s*&gNvEvtSleepers\s*\)", w) is not None)
    arm("selector 6 keeps what the block returned and classifies it with nvrm_evw_classify",
        re.search(r"=\s*thread_block\s*\(", w) is not None and "nvrm_evw_classify(slept, wr, seen, now)" in sw
        and "THREAD_TIMED_OUT" in w and "NVRM_EVW_WR_INTERRUPTED" in w)
    ceiling = re.search(r"if\s*\(\s*OSIncrementAtomic\s*\(\s*&gNvEvtSleepers\s*\)\s*>=\s*kNvEvtMaxSleepers\s*\)\s*\{(.*?)\}\s*else", w, re.S)
    arm("at the sleeper ceiling selector 6 does not park and answers CAPPED, counted",
        ceiling is not None and "capped = true" in squash(ceiling.group(1)) and "gNvEvtCapped" in ceiling.group(1)
        and "assert_wait" not in ceiling.group(1)
        and re.search(r"capped\s*\?\s*\(unsigned\)\s*NVRM_EVW_R_CAPPED\s*:", w) is not None)
    arm("selector 6 answers the count in bits 0..31 and the reason above (nvrm_evw_pack), the count read after the park",
        re.search(r"const\s+UInt32\s+now\s*=\s*me\s*->\s*fEvtSeq\s*;", w) is not None
        and re.search(r"scalarOutput\s*\[\s*0\s*\]\s*=\s*nvrm_evw_pack\s*\(\s*now\s*,\s*why\s*\)\s*;", w) is not None
        and len(re.findall(r"scalarOutput\s*\[", w)) == 1)
    arm("selector 6 counts woken and timed-out sleeps",
        "gNvEvtWoken" in w and "gNvEvtTimeouts" in w and "NVRM_EVW_R_WOKEN" in w and "NVRM_EVW_R_TIMEOUT" in w)

    n = body(code, "NVRMUserClient::sEvtNote")
    arm("selector 9 counts the two notes and refuses anything else",
        "NVRM_EVW_NOTE_MISSED" in n and "NVRM_EVW_NOTE_FELL_BACK" in n and "gNvEvtUserMissed" in n
        and "gNvEvtUserFellBack" in n and re.search(r"else\s+return\s+kIOReturnBadArgument\s*;", n) is not None)
    arm("selector 9 logs only the first few fall-backs (any process can call it)",
        re.search(r"OSIncrementAtomic64\s*\(\s*&gNvEvtUserFellBack\s*\)\s*<\s*\d+\s*\)", n) is not None
        and len(re.findall(r"\b(?:kprintf|IOLog|LOG)\s*\(", n)) == 1)
    arm("selector 9 takes no lock and makes no RM call",
        not re.search(r"\b(?:Gate|IOLockLock|rm_\w+|NvXnuPmUse)\b", n))

    c = body(code, "NVRMUserClient::clientClose")
    arm("clientClose drains event posts after the RM cleanup of the client, inside the one cleanup",
        before(c, "OSCompareAndSwap(0, 1, &fCleaned)", "rm_cleanup_file_private(") and before(c, "rm_cleanup_file_private(", "evtDrainPosts(")
        and before(c, "evtDrainPosts(", "context.valid = NV_FALSE") and c.count("evtDrainPosts(") == 1)

    d = body(code, "NVRMUserClient::evtDrainPosts")
    first = re.match(r"\s*if\s*\(\s*nv_event_posting_refcount_read\s*\(\s*&fNvfp\s*\)\s*==\s*0\s*\)\s*return\s+true\s*;", d)
    arm("the drain returns at once when no post is in flight (it sleeps only for a post that is)", first is not None)
    loop = re.search(r"for\s*\(\s*unsigned\s+i\s*=\s*0\s*;\s*i\s*<\s*(\d+)\s*&&\s*nv_event_posting_refcount_read\s*\(\s*&fNvfp\s*\)\s*!=\s*0\s*;\s*i\+\+\s*\)\s*IOSleep\s*\(\s*1\s*\)\s*;", d)
    arm("the drain is bounded: 1 ms steps, at most 200 of them, and it says whether the count reached zero",
        loop is not None and int(loop.group(1)) <= 200 and len(re.findall(r"\bIOSleep\s*\(", d)) == 1
        and re.search(r"return\s+nv_event_posting_refcount_read\s*\(\s*&fNvfp\s*\)\s*==\s*0\s*;\s*$", d) is not None)

    f = body(code, "NVRMUserClient::free")
    stuck = re.search(r"if\s*\(\s*!\s*evtDrainPosts\s*\(\s*\)\s*\)\s*\{(.*?)\}", f, re.S)
    arm("free() drains again after releasing what it owns; a post still in flight keeps the object (counted), never super::free()",
        stuck is not None and "gNvEvtDrainStuck" in stuck.group(1) and re.search(r"\breturn\s*;", stuck.group(1)) is not None
        and "super::free" not in stuck.group(1)
        and before(f, "IOLockFree(fMapLock)", "evtDrainPosts(") and before(f, "evtDrainPosts(", "super::free()")
        and f.count("super::free()") == 1)
    arm("free() clears the magic nv_post_event checks before the memory goes",
        re.search(r"fEvtMagic\s*=\s*0\s*;\s*super::free\s*\(\s*\)\s*;", f) is not None)

    for s in SYSCTLS:
        decl = re.search(r"SYSCTL_QUAD\s*\(\s*_debug\s*,\s*OID_AUTO\s*,\s*" + s + r"\s*,\s*([^,]+),", code)
        arm("debug.%s is declared read-only and registered" % s,
            decl is not None and squash(decl.group(1)) == "CTLFLAG_RD"
            and re.search(r"sysctl_register_oid\s*\(\s*&sysctl__debug_" + s + r"\s*\)", code) is not None)
    return out


def broken(src, old, new, count=1):
    assert src.count(old) >= 1, "control text not found: " + old
    return src.replace(old, new, count)


CONTROLS = [
    ("selector 9 row missing", "row 9 is the note", lambda s: broken(s, "{ sEvtNote, 1, 0, 0, 0 },", "")),
    ("row 10 runs the note", "rows 10 and 11 are retired", lambda s: broken(s, "{ sEvtNote, 1, 0, 0, 0 },", "{ sEvtNote, 1, 0, 0, 0 },\n        { sEvtNote, 1, 0, 0, 0 },")),
    ("a 2.9 row moved", "rows 0..8", lambda s: broken(s, "{ sCaps, 0, 0, 1, 0 },\n        { sForgetToken, 1, 0, 0, 0 },", "{ sForgetToken, 1, 0, 0, 0 },\n        { sCaps, 0, 0, 1, 0 },")),
    ("caps back to 1", "selector 7 answers both bits", lambda s: broken(s, "NVRM_EVW_CAP_WAIT | NVRM_EVW_CAP_REASON;", "1;")),
    ("count re-read dropped", "parks as 2.9 did", lambda s: broken(s, "if (me->fEvtSeq != seen) thread_wakeup((event_t)&me->fEvtSeq);", "")),
    ("sleepers not uncounted after the block", "parks as 2.9 did",
     lambda s: re.sub(r"(const wait_result_t r = thread_block\(THREAD_CONTINUE_NULL\);)\s*OSDecrementAtomic\(&gNvEvtSleepers\);", r"\1", s)),
    ("ceiling looks like a wake", "sleeper ceiling", lambda s: broken(s, "capped = true;", "")),
    ("only the count returned", "bits 0..31", lambda s: broken(s, "args->scalarOutput[0] = nvrm_evw_pack(now, why);", "args->scalarOutput[0] = now;")),
    ("clamp removed", "100 ms clamp", lambda s: broken(s, "if (us > 100000) us = 100000;", "")),
    ("note accepts anything", "selector 9 counts", lambda s: broken(s, "    } else return kIOReturnBadArgument;\n", "    }\n")),
    ("note logs every call", "selector 9 logs only", lambda s: broken(s, "if (OSIncrementAtomic64(&gNvEvtUserFellBack) < 8)", "if (OSIncrementAtomic64(&gNvEvtUserFellBack) >= 0)")),
    ("drain before the RM cleanup", "clientClose drains",
     lambda s: broken(broken(s, "        if (!evtDrainPosts()) kprintf(", "        if (0) kprintf("),
                      "        if (fOwner) { NvXnuPmUse pmUse; rm_cleanup_file_private(", "        (void)evtDrainPosts();\n        if (fOwner) { NvXnuPmUse pmUse; rm_cleanup_file_private(")),
    ("drain always sleeps", "returns at once", lambda s: broken(s, "    if (nv_event_posting_refcount_read(&fNvfp) == 0) return true;\n    OSIncrementAtomic64(&gNvEvtDrainWaits);", "    OSIncrementAtomic64(&gNvEvtDrainWaits);")),
    ("drain unbounded", "the drain is bounded", lambda s: broken(s, "i < 200 &&", "i < 200000 &&")),
    ("free() frees under a post", "free() drains again", lambda s: broken(s, "        kprintf(\"NVRM-xnu: client fd %d: an event post never finished - the connection is kept, not freed\\n\", fFd);\n        return;", "        kprintf(\"NVRM-xnu: client fd %d: an event post never finished - the connection is kept, not freed\\n\", fFd);")),
    ("magic left set", "clears the magic", lambda s: broken(s, "    fEvtMagic = 0;\n    super::free();", "    super::free();")),
    ("a counter writable", "debug.nvrm_evt_woken", lambda s: broken(s, "nvrm_evt_woken, CTLFLAG_RD,", "nvrm_evt_woken, CTLFLAG_RW,")),
    ("a counter not registered", "debug.nvrm_evt_user_fallbacks", lambda s: broken(s, "sysctl_register_oid(&sysctl__debug_nvrm_evt_user_fallbacks);", "")),
]


def main():
    src = (root / "kexts/NVRM/NVRM.cpp").read_text()
    fails = 0
    for name, ok in arms(src):
        print(("ok   " if ok else "FAIL ") + name)
        fails += 0 if ok else 1
    for name, arm_prefix, mutate in CONTROLS:
        try:
            res = arms(mutate(src))
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
