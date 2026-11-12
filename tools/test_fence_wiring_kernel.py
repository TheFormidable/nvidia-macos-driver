#!/usr/bin/env python3
"""What kexts/NVRM/nvrm_fence.cpp, the kernel side of the surface fence, may and may not do.

The decisions of the fence are in kexts/NVRM/nvrm_fence_table.h and are run by tools/test_fence_table.c and
tools/test_fence_model.c. This file is about the kernel code around them, which no host test can run: the lock, the
sleep and the wakeup, the pages of another process, the boot-arg and the counters. A mistake in any of these is a
panic, a hang or a lost wakeup on the owner's machine, so each rule is asserted on the source text:

- every function nvrm_fence.h declares is defined with the declared parameters; the file makes no RM call;
- one opt-out (nvrmfence=0), read once, and nothing that switches the layer on; off means no lock and no page;
- every debug.nvrm_fence_* sysctl is read-only, registered once, and every counter of the table has one;
- the kick is three statements: test, move the generation, wake only when somebody sleeps; no lock, no log line;
- the sleep counts itself in, queues itself, reads both generations AGAIN and only then blocks; it is bounded, and
  only a sleep that a signal can end is refused for the number of sleepers;
- one unlock: it moves the change generation and wakes the sleepers after the lock is released, and logs after that;
- between the lock and the unlock: no log line, no allocation, no wakeup, no page pinned or let go, no return;
- a page of another process is pinned (wired) before the lock and let go after it, is never mapped into the kernel,
  and is read only through the one function that zeroes its buffer first;
- a channel is registered only after the kernel read the probe word the process stored, and verified only after it
  read a different one later; a rebuilt channel's new notifier is pinned before the lock, swapped under it only when
  the table agreed, and the old one is let go after it;
- every request but QUERY and CHAN_DEL is answered "off" before a page is touched unless the layer is on; a compositor
  hold is WindowServer's; a request racing the close of its connection adds nothing;
- user waits can be interrupted; NVAccel's cannot, look at compositor holds only, and take no lock when there is none;
- start, stop, sleep, wake and a closing connection are safe in any order; the lock and the table are never freed;
- the lock order is written down where the lock is declared; every log line is rate-limited and says "fence";
- the table header includes nothing but the ABI header and defines only static inline functions.

Each arm has a negative control: the same check on the text with that one thing broken must fail.
Usage: test_fence_wiring_kernel.py [driver checkout]"""
import pathlib
import re
import sys

root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1])

LEX = re.compile(r"""//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])'""", re.S)
PROTOTYPES = ["nvrm_fence_start", "nvrm_fence_stop", "nvrm_fence_kick", "nvrm_fence_pm", "nvrm_fence_user",
              "nvrm_fence_owner_gone", "nvrm_fence_kernel_ops"]
# what may not happen between nvfLock() and nvfUnlock()
UNDER_LOCK = (r"\bIOLog\s*\(|->\s*release\s*\(|->\s*complete\s*\(|->\s*prepare\s*\(|\bwithAddressRange\s*\(|\bnvfPin\s*\(|"
              r"\bnvfUnpin\s*\(|\bnvfMapFailed\s*\(|\bnvfCount\s*\(|\bnvfLog\s*\(|\bIOLockAlloc\s*\(|\bIOMalloc\w*\s*\(|"
              r"\bthread_wakeup\w*\s*\(|\bthread_block\s*\(|\bassert_wait\w*\s*\(|\bnvf_wait\s*\(|\bnvf_wait_seq\s*\(|"
              r"\bnvfCallerIsWindowServer\s*\(|\bproc_selfname\s*\(|\bnvfBoot\s*\(|\bnvfSysctls\s*\(|\bnvfOwner\s*\(|\breturn\b")


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
    """name -> body of every function defined at the top level of the file."""
    out, depth, start = {}, 0, 0
    for i, c in enumerate(code):
        if c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                m = re.search(r"(\w+)\s*\(([^(){};]*)\)\s*$", code[:start])
                if m:
                    out[m.group(1)] = code[start + 1:i]
    return out


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


def order(text, patterns):
    """True when each pattern occurs, each after the end of the previous one."""
    pos = 0
    for p in patterns:
        m = re.compile(p).search(text, pos)
        if not m:
            return False
        pos = m.end()
    return True


def count(text, pattern):
    return len(re.findall(pattern, text))


def lock_regions(b):
    """The pieces of a function body between nvfLock() and nvfUnlock(), or None when they do not pair up."""
    out, start = [], None
    for m in re.finditer(r"\b(nvfLock|nvfUnlock)\s*\(\s*\)\s*;", b):
        if m.group(1) == "nvfLock":
            if start is not None:
                return None
            start = m.end()
        else:
            if start is None:
                return None
            out.append(b[start:m.start()])
            start = None
    return out if start is None else None


def prototypes(header_code):
    return {m.group(2): squash(m.group(1) + " (" + m.group(3) + ")")
            for m in re.finditer(r"^(void|bool)\s+(nvrm_fence_\w+)\s*\(([^)]*)\)\s*;", header_code, re.M)}


def definitions(code):
    return {m.group(2): squash(m.group(1) + " (" + m.group(3) + ")")
            for m in re.finditer(r"^(void|bool)\s+(nvrm_fence_\w+)\s*\(([^)]*)\)\s*\{", code, re.M)}


def arms(src, header, table):
    code = code_only(src)
    fn = functions(code)
    out = []

    def arm(name, ok):
        out.append((name, bool(ok)))

    def b(name):
        return fn.get(name, "")

    # ---- the interface ----------------------------------------------------------------------------------------------
    want, have = prototypes(code_only(header)), definitions(code)
    arm("every function nvrm_fence.h declares is defined here with the declared parameters, and no other is exported",
        sorted(want) == sorted(PROTOTYPES) and want == have)
    arm("the fence makes no RM call, takes no client gate and reaches into no other part of the driver",
        not re.search(r"\b(?:rm|nv|os|nvkms|nvrm_power|nvrm_dma|nvrm_chan)_\w+\s*\(|\bfGate\b|\bGate\s+\w+\s*\(|IOCommandGate|IOAccel|IOSurface", code)
        and re.findall(r'#include\s+"([^"]+)"', src) == ["nv-xnu.h", "nvrm_fence.h", "nvrm_fence_table.h"])

    # ---- the opt-out ------------------------------------------------------------------------------------------------
    boot = b("nvfBoot")
    arm("one opt-out, read once: nvrmfence=0 switches the layer off, and no boot-arg switches it on",
        count(src, r"PE_parse_boot_arg\w*\s*\(") == 1
        and re.search(r'\(\s*PE_parse_boot_argn\s*\(\s*"nvrmfence"\s*,\s*&v\s*,\s*sizeof\s*\(\s*v\s*\)\s*\)\s*&&\s*v\s*==\s*0\s*\)\s*\?\s*2\s*:\s*1\s*;', src) is not None
        and "PE_parse_boot_argn" in boot and re.search(r"if\s*\(\s*boot\s*==\s*2\s*\)\s*gFState\s*=\s*0\s*;", boot) is not None
        and not re.search(r"PE_i_can_has|\bgetenv\b|IORegistryEntry|getProperty", code))
    start = b("nvrm_fence_start")
    arm("switched off: the start registers the counters and returns before it makes the lock",
        order(start, [r"=\s*nvfBoot\s*\(\s*\)\s*;", r"\bnvfSysctls\s*\(\s*\)\s*;", r"if\s*\(\s*boot\s*==\s*2\s*\)\s*return\s*;", r"\bIOLockAlloc\s*\("]))
    ops = b("nvrm_fence_kernel_ops")
    arm("NVAccel is handed the wait only when version and size match and the layer is not switched off",
        statements(ops) == ["if (!o || o->version != NVRM_FENCE_ABI_VERSION || o->size != sizeof(*o)) return false;",
                            "if (nvfBoot() == 2) return false;", "o->wait = nvfKernelWait;", "gFAccel = 1;", "return true;"])

    # ---- the counters -----------------------------------------------------------------------------------------------
    decl = re.findall(r"\bSYSCTL_(\w+)\s*\(\s*(\w+)\s*,\s*(\w+)\s*,\s*(\w+)\s*,\s*([^,]+),\s*([^,]+),", code)
    names = [d[3] for d in decl]
    arm("every sysctl is a plain read-only declaration under debug, named nvrm_fence_*, each name once",
        len(decl) >= 3 and len(decl) == count(code, r"\bSYSCTL_\w+\s*\(") and len(set(names)) == len(names)
        and all(d[0] in ("INT", "QUAD") and d[1] == "_debug" and d[2] == "OID_AUTO" and d[3].startswith("nvrm_fence_")
                and squash(d[4]) == "CTLFLAG_RD | CTLFLAG_LOCKED" for d in decl)
        and not re.search(r"CTLFLAG_RW|CTLFLAG_WR|CTLFLAG_ANYBODY|CTLFLAG_SECURE|sysctl_handle_|sysctl_unregister_oid|\bsysctl_oid\b", code))
    reg = re.findall(r"\bsysctl_register_oid\s*\(\s*&\s*sysctl__debug_(\w+)\s*\)\s*;", code)
    sysctls = b("nvfSysctls")
    arm("each is registered exactly once, in the one function that runs once per boot, and never unregistered",
        sorted(reg) == sorted(names) and count(sysctls, r"\bsysctl_register_oid\s*\(") == len(reg)
        and re.match(r"\s*if\s*\(\s*!\s*OSCompareAndSwap\s*\(\s*0\s*,\s*1\s*,\s*\(\s*volatile\s+UInt32\s*\*\s*\)\s*&gFSysctls\s*\)\s*\)\s*return\s*;", sysctls) is not None
        and count(code, r"\bnvfSysctls\s*\(\s*\)\s*;") == 1)
    enum = re.search(r"enum\s*\{\s*(NVF_STAT_OPENS\b.*?)\bNVF_STAT_N\b", code_only(table), re.S)
    stats = re.findall(r"\bNVF_STAT_(\w+)", enum.group(1)) if enum else []
    by_stat = {m.group(2): m.group(1) for m in re.finditer(r"\bSYSCTL_QUAD\s*\(\s*_debug\s*,\s*OID_AUTO\s*,\s*(\w+)\s*,[^;]*?\bNVF_STAT\s*\(\s*(\w+)\s*\)", code)}
    arm("every counter of the table has its sysctl, named after it",
        len(stats) > 40 and sorted(by_stat) == sorted(stats) and all(by_stat[s] == "nvrm_fence_" + s.lower() for s in stats)
        and len(names) == len(stats) + 3 and {"nvrm_fence_state", "nvrm_fence_accel", "nvrm_fence_kicks"} <= set(names))

    # ---- the kick ---------------------------------------------------------------------------------------------------
    arm("the kick is three statements: on?, move the interrupt generation, wake only when somebody sleeps",
        statements(b("nvrm_fence_kick")) == ["if (gFState != 1) return;", "OSIncrementAtomic64(&gFKick);",
                                             "if (gFSleepers > 0) thread_wakeup((event_t)&gFKick);"])

    # ---- the sleep --------------------------------------------------------------------------------------------------
    sleep = b("nvfOpSleep")
    arm("the sleep counts itself in, queues itself, reads both generations again and only then blocks",
        order(sleep, [r"OSIncrementAtomic\s*\(\s*&gFSleepers\s*\)", r"\bassert_wait_timeout\s*\(\s*\(\s*event_t\s*\)\s*&gFKick\b",
                      r"if\s*\(\s*\(\s*unsigned\s*\)\s*gFKick\s*!=\s*kickSeen\s*\|\|\s*\(\s*unsigned\s*\)\s*gFGen\s*!=\s*genSeen\s*\)\s*thread_wakeup\s*\(\s*\(\s*event_t\s*\)\s*&gFKick\s*\)\s*;",
                      r"\bthread_block\s*\(\s*THREAD_CONTINUE_NULL\s*\)", r"OSDecrementAtomic\s*\(\s*&gFSleepers\s*\)\s*;\s*return\b"])
        and count(sleep, r"\bgFKick\b") == 3 and count(sleep, r"\bgFGen\b") == 1)
    arm("it is the only sleep of the file, and nothing here needs a call the kernel does not export to kexts",
        count(code, r"\bassert_wait\w*\s*\(") == 1 and count(code, r"\bthread_block\w*\s*\(") == 1
        and not re.search(r"\bclear_wait\s*\(|thread_call_cancel_wait|\bIOSleep\s*\(|\bIODelay\s*\(|\bIOLockSleep\w*\s*\(|\bmsleep\s*\(|\bdelay\s*\(|\bIOPause\s*\(|thread_call_|IOTimerEventSource", code))
    arm("every sleep is bounded by the longest wait of the interface",
        order(sleep, [r"if\s*\(\s*us\s*>\s*NVRM_FENCE_WAIT_MAX_MS\s*\*\s*1000ull\s*\)\s*us\s*=\s*NVRM_FENCE_WAIT_MAX_MS\s*\*\s*1000ull\s*;",
                      r"\bassert_wait_timeout\s*\([^;]*,\s*\(\s*uint32_t\s*\)\s*us\s*,\s*NSEC_PER_USEC\s*\)\s*;"]))
    arm("only a sleep that a signal can end is refused for the number of sleepers, and only such a sleep can be interrupted",
        re.match(r"\s*if\s*\(\s*OSIncrementAtomic\s*\(\s*&gFSleepers\s*\)\s*>=\s*kNvfMaxSleepers\s*&&\s*abortable\s*\)\s*\{\s*OSDecrementAtomic\s*\(\s*&gFSleepers\s*\)\s*;\s*return\s+NVF_SLEEP_FULL\s*;\s*\}", sleep) is not None
        and re.search(r"\babortable\s*\?\s*THREAD_ABORTSAFE\s*:\s*THREAD_UNINT\b", sleep) is not None
        and re.search(r"return\s+wr\s*==\s*THREAD_INTERRUPTED\s*\?\s*NVF_SLEEP_ABORTED\s*:\s*NVF_SLEPT\s*;", sleep) is not None
        and re.search(r"\bkNvfMaxSleepers\s*=\s*256\s*;", code) is not None)

    # ---- the lock ---------------------------------------------------------------------------------------------------
    unlock = b("nvfUnlock")
    arm("the one unlock moves the change generation and wakes the sleepers after the lock is released, then logs",
        order(unlock, [r"\bchanged\s*=\s*gT\.changes\s*!=\s*gFChangesSeen\s*;", r"gFChangesSeen\s*=\s*gT\.changes\s*;", r"\bnvf_events_take\s*\(",
                       r"\bIOLockUnlock\s*\(\s*gFLock\s*\)\s*;",
                       r"if\s*\(\s*changed\s*\)\s*\{\s*OSIncrementAtomic\s*\(\s*&gFGen\s*\)\s*;\s*if\s*\(\s*gFSleepers\s*>\s*0\s*\)\s*thread_wakeup\s*\(\s*\(\s*event_t\s*\)\s*&gFKick\s*\)\s*;\s*\}",
                       r"\bnvfLog\s*\("])
        and count(code, r"\bIOLockUnlock\s*\(") == 1 and count(code, r"\bIOLockLock\s*\(") == 1
        and statements(b("nvfLock")) == ["IOLockLock(gFLock);"]
        and count(code, r"\bthread_wakeup\w*\s*\(") == 3 and count(code, r"&\s*gFGen\b") == 1 and count(code, r"\bnvfLog\s*\(") == 2)
    regions, paired = [], True
    for name, body_ in fn.items():
        if name in ("nvfLock", "nvfUnlock", "nvfOpLock", "nvfOpUnlock"):
            continue
        r = lock_regions(body_)
        if r is None:
            paired = False
        else:
            regions += [(name, x) for x in r]
    arm("every function that takes the lock lets go of it itself, and the lock is taken in 12 places",
        paired and len(regions) == 12 and statements(b("nvfOpLock")) == ["nvfLock();"] and statements(b("nvfOpUnlock")) == ["nvfUnlock();"])
    arm("under the lock: no log line, no allocation, no wakeup, no sleep, no page pinned or let go, no return",
        paired and regions and not [n for n, x in regions if re.search(UNDER_LOCK, x)])

    # ---- the pages of another process -------------------------------------------------------------------------------
    arm("a page is pinned in the caller's task for reading, and a page that cannot be wired is given back at once",
        statements(b("nvfPin")) == ["IOMemoryDescriptor *md = IOMemoryDescriptor::withAddressRange((mach_vm_address_t)pageVA, NVF_PAGE, kIODirectionOut, current_task());",
                                    "if (!md) return NULL;", "if (md->prepare() != kIOReturnSuccess) { md->release(); return NULL; }", "return md;"]
        and count(code, r"\bwithAddressRange\s*\(") == 1 and count(code, r"->\s*prepare\s*\(") == 1)
    arm("letting go is complete, then release, in one place",
        statements(b("nvfUnpin")) == ["if (!md) return;", "md->complete();", "md->release();"]
        and count(code, r"->\s*complete\s*\(") == 1 and count(code, r"->\s*release\s*\(") == 2)
    arm("no page of a process is ever mapped into the kernel, and nothing but the fence's own lock is allocated",
        not re.search(r"createMappingInTask|IOMemoryMap|->\s*map\s*\(|getVirtualAddress|getBytesNoCopy|\bkernel_task\b|ml_phys_|\bcopyin\w*\s*\(|\bcopyout\w*\s*\(|"
                      r"\bbcopy\s*\(|\bmemcpy\s*\(|IOBufferMemoryDescriptor|\bIOMalloc\w*\s*\(|\bIONew\w*\s*\(|kern_os_malloc|\bkalloc\w*\s*\(|\bnew\s+\w", code)
        and count(code, r"\bIOLockAlloc\s*\(") == 1 and re.search(r"^static\s+struct\s+nvf_table\s+gT\s*;", code, re.M) is not None)
    arm("a pinned page is read in one place, 8 bytes into a buffer that was zeroed first",
        statements(b("nvfRead64")) == ["*out = 0;", "return md->readBytes(off, out, sizeof(*out)) == sizeof(*out);"]
        and count(code, r"\breadBytes\s*\(") == 1
        and sorted(n for n, x in fn.items() if re.search(r"\bnvfRead64\s*\(", x)) == ["nvfChanAdd", "nvfChanVerify", "nvfRead"])
    rd = b("nvfRead")
    arm("the table's view of a channel: the completion value before the flush value, a fault from the notifier's status",
        order(rd, [r"!\s*gFMap\[ci\]\.sem\s*\|\|\s*!\s*gFMap\[ci\]\.err\s*\)\s*return\s+0\s*;", r"nvfRead64\s*\(\s*m->sem\s*,\s*0\s*,\s*&d\s*\)",
                   r"nvfRead64\s*\(\s*m->sem\s*,\s*NVRM_FENCE_SEM_FLUSHED_OFF\s*,\s*&f\s*\)", r"nvfRead64\s*\(\s*m->err\s*,\s*m->errOff\s*\+\s*8\s*,\s*&note\s*\)",
                   r"\*faulted\s*=\s*\(\s*UInt32\s*\)\s*\(\s*note\s*>>\s*48\s*\)\s*!=\s*m->errBase\s*;"]))

    # ---- registering a channel --------------------------------------------------------------------------------------
    add = b("nvfChanAdd")
    arm("CHAN_ADD: addresses checked, both pages pinned, the probe word read back, all before the lock",
        order(add, [r"!\s*nvf_sem_va_ok\s*\(\s*c->semVA\s*\)\s*\|\|\s*!\s*nvf_err_va_ok\s*\(\s*c->errVA\s*,\s*c->errBase\s*\)\s*\|\|\s*!\s*c->probe\s*\)\s*\{\s*r->status\s*=\s*NVRM_FENCE_ST_BAD_ARG\s*;\s*return\s*;",
                    r"\bnvfPin\s*\(\s*c->semVA\s*\)", r"\bnvfPin\s*\(\s*c->errVA\s*&\s*~\s*\(\s*NVF_PAGE\s*-\s*1\s*\)\s*\)",
                    r"if\s*\(\s*!\s*nvfRead64\s*\(\s*sem\s*,\s*NVRM_FENCE_SEM_PROBE_OFF\s*,\s*&seen\s*\)\s*\|\|\s*seen\s*!=\s*c->probe\s*\)\s*\{",
                    r"r->status\s*=\s*NVRM_FENCE_ST_MAP\s*;\s*return\s*;", r"\bnvfLock\s*\(\s*\)\s*;"]))
    arm("CHAN_ADD: the owner is named before the lock, a closing connection adds nothing, a refused channel's pages are let go after it",
        order(add, [r"\bowner\s*=\s*nvfOwner\s*\(\s*ownerId\s*\)\s*;", r"\bnvfLock\s*\(\s*\)\s*;",
                    r"st\s*=\s*\*closing\s*\?\s*NVRM_FENCE_ST_OFF\s*:\s*nvf_chan_add\s*\(", r"if\s*\(\s*st\s*==\s*NVRM_FENCE_ST_OK\s*\)\s*\{",
                    r"m->probe0\s*=\s*seen\s*;", r"sem\s*=\s*NULL\s*;\s*err\s*=\s*NULL\s*;", r"\bnvfUnlock\s*\(\s*\)\s*;",
                    r"\bnvfUnpin\s*\(\s*sem\s*\)\s*;\s*nvfUnpin\s*\(\s*err\s*\)\s*;"]))
    ver = b("nvfChanVerify")
    arm("CHAN_VERIFY: the view must show a value written after the registration: the one named, not zero, not the first one",
        order(ver, [r"\bnvfLock\s*\(\s*\)\s*;",
                    r"matches\s*=\s*nvfRead64\s*\(\s*gFMap\[ci\]\.sem\s*,\s*NVRM_FENCE_SEM_PROBE_OFF\s*,\s*&seen\s*\)\s*&&\s*c->probe\s*!=\s*0\s*&&\s*seen\s*==\s*c->probe\s*&&\s*seen\s*!=\s*gFMap\[ci\]\.probe0\s*;",
                    r"r->status\s*=\s*nvf_chan_verify\s*\(\s*&gT\s*,\s*owner\s*,\s*c->chan\s*,\s*matches\s*,\s*&freed\s*\)\s*;",
                    r"\bnvfUnlock\s*\(\s*\)\s*;", r"\bnvfUnpin\s*\(\s*sem\s*\)\s*;\s*nvfUnpin\s*\(\s*err\s*\)\s*;"])
        and count(ver, r"\bmatches\s*=") == 2)
    reb = b("nvfChanRebind")
    arm("CHAN_REBIND: the new notifier is pinned before the lock and swapped under it only when the table agreed",
        order(reb, [r"if\s*\(\s*!\s*c->chan\s*\|\|\s*!\s*nvf_err_va_ok\s*\(\s*c->errVA\s*,\s*c->errBase\s*\)\s*\)\s*\{\s*r->status\s*=\s*NVRM_FENCE_ST_BAD_ARG\s*;\s*return\s*;",
                    r"\berr\s*=\s*nvfPin\s*\(\s*c->errVA\s*&\s*~\s*\(\s*NVF_PAGE\s*-\s*1\s*\)\s*\)\s*;", r"\bnvfLock\s*\(\s*\)\s*;",
                    r"r->status\s*=\s*nvf_chan_rebind\s*\(\s*&gT\s*,\s*owner\s*,\s*c->chan\s*,\s*&ci\s*\)\s*;",
                    r"if\s*\(\s*r->status\s*==\s*NVRM_FENCE_ST_OK\s*&&\s*ci\s*<\s*NVF_CHANS\s*\)\s*\{", r"\bold\s*=\s*gFMap\[ci\]\.err\s*;",
                    r"gFMap\[ci\]\.err\s*=\s*err\s*;", r"\berr\s*=\s*old\s*;", r"\bnvfUnlock\s*\(\s*\)\s*;"]))
    arm("CHAN_REBIND: the old notifier (or the unused new one) is let go after the lock, and nothing else of the channel is touched",
        statements(reb)[-2:] == ["nvfUnlock();", "nvfUnpin(err);"] and count(reb, r"\bnvfUnpin\s*\(") == 1
        and not re.search(r"\.sem\b|\bprobe0\b|\bnvf_chan_add\b|\bnvf_chan_del\b|\bnvf_chan_verify\b|\bnvfTake\b", reb))
    dele = b("nvfChanDel")
    arm("CHAN_DEL is served in every state once the lock exists, and lets the pages go after the lock",
        order(dele, [r"if\s*\(\s*!\s*gFLock\s*\)\s*\{\s*r->status\s*=\s*NVRM_FENCE_ST_OFF\s*;\s*return\s*;\s*\}", r"\bnvfLock\s*\(\s*\)\s*;", r"\bnvf_chan_del\s*\(",
                     r"if\s*\(\s*freed\s*!=\s*NVF_NIL\s*\)\s*nvfTake\s*\(\s*freed\s*,\s*&sem\s*,\s*&err\s*\)\s*;", r"\bnvfUnlock\s*\(\s*\)\s*;",
                     r"\bnvfUnpin\s*\(\s*sem\s*\)\s*;\s*nvfUnpin\s*\(\s*err\s*\)\s*;"]))

    # ---- requests ---------------------------------------------------------------------------------------------------
    user = b("nvrm_fence_user")
    gate = re.search(r"if\s*\(\s*gFState\s*!=\s*1\s*\)\s*\{\s*r->status\s*=\s*NVRM_FENCE_ST_OFF\s*;\s*return\s*;\s*\}", user)
    handlers = [r"\bnvfChanAdd\s*\(", r"\bnvfChanVerify\s*\(", r"\bnvfChanRebind\s*\(", r"\bnvfOpen\s*\(", r"\bnvfArm\s*\(", r"\bnvfClose\s*\(",
                r"\bnvf_wait\s*\(", r"\bnvf_wait_seq\s*\("]
    arm("a request is answered \"off\" before a page is touched or the lock taken, unless the layer is on; QUERY and CHAN_DEL are answered always",
        gate is not None
        and order(user, [r"r->version\s*!=\s*NVRM_FENCE_ABI_VERSION", r"r->op\s*==\s*NVRM_FENCE_OP_QUERY", r"r->op\s*==\s*NVRM_FENCE_OP_CHAN_DEL\s*\)\s*\{\s*nvfChanDel\s*\(",
                         r"if\s*\(\s*gFState\s*!=\s*1\s*\)", r"\bswitch\s*\(\s*r->op\s*\)"])
        and all(re.search(h, user[gate.end():]) and not re.search(h, user[:gate.start()]) for h in handlers)
        and all(count(code, h) == 2 for h in handlers[:6]) and not re.search(r"\bnvfLock\b|\bnvfPin\b", user[:gate.start()].replace("nvfChanDel", "")))
    opn = b("nvfOpen")
    arm("OPEN: a compositor hold is WindowServer's, AUTO needs COMPOSITOR, and a closing connection opens nothing",
        order(opn, [r"\bflags\s*=\s*r->flags\s*&\s*\(\s*NVRM_FENCE_F_COMPOSITOR\s*\|\s*NVRM_FENCE_F_AUTO\s*\)\s*;", r"\|\|\s*flags\s*==\s*NVRM_FENCE_F_AUTO\s*\)\s*\{\s*r->status\s*=\s*NVRM_FENCE_ST_BAD_ARG\s*;",
                    r"if\s*\(\s*\(\s*flags\s*&\s*NVRM_FENCE_F_COMPOSITOR\s*\)\s*&&\s*!\s*nvfCallerIsWindowServer\s*\(\s*\)\s*\)\s*\{", r"r->status\s*=\s*NVRM_FENCE_ST_DENIED\s*;\s*return\s*;",
                    r"\bowner\s*=\s*nvfOwner\s*\(\s*ownerId\s*\)\s*;", r"\bnvfLock\s*\(\s*\)\s*;",
                    r"r->status\s*=\s*\*closing\s*\?\s*NVRM_FENCE_ST_OFF\s*:\s*nvf_open\s*\(\s*&gT\s*,\s*owner\s*,\s*pid\s*,\s*h->cookie\s*,\s*h->count\s*,\s*h->ids\s*,\s*h->planes\s*,\s*flags\s*,\s*now\s*\)\s*;",
                    r"\bnvfUnlock\s*\(\s*\)\s*;"]))
    arm("only CHAN_ADD and OPEN give a connection its owner number, and nothing else writes the client's word",
        sorted(n for n, x in fn.items() if re.search(r"\bnvfOwner\s*\(", x)) == ["nvfChanAdd", "nvfOpen"]
        and count(code, r"\bOSCompareAndSwap64\s*\(\s*0\s*,\s*id\s*,\s*ownerId\s*\)") == 1 and not re.search(r"\*\s*ownerId\s*(?:=(?!=)|\+\+|--)", code))
    kw = b("nvfKernelWait")
    arm("user waits can be interrupted; NVAccel's cannot",
        re.search(r"nvf_wait\s*\(\s*&gT\s*,\s*&gFOps\s*,\s*NULL\s*,\s*&q\s*,\s*w->timeoutMs\s*,\s*\(\s*r->flags\s*&\s*NVRM_FENCE_F_LOOK\s*\)\s*!=\s*0\s*,\s*1\s*,\s*w\s*\)", user) is not None
        and re.search(r"nvf_wait_seq\s*\(\s*&gT\s*,\s*&gFOps\s*,\s*NULL\s*,\s*\*ownerId\s*,\s*w->id\s*,\s*w->seq\s*,\s*w->timeoutMs\s*,\s*1\s*,\s*w\s*\)", user) is not None
        and re.search(r"return\s+nvf_wait\s*\(\s*&gT\s*,\s*&gFOps\s*,\s*NULL\s*,\s*&q\s*,\s*w->timeoutMs\s*,\s*\(\s*flags\s*&\s*NVRM_FENCE_F_LOOK\s*\)\s*!=\s*0\s*,\s*0\s*,\s*w\s*\)\s*;", kw) is not None
        and count(code, r"\bnvf_wait\s*\(") == 2 and count(code, r"\bnvf_wait_seq\s*\(") == 1)
    arm("NVAccel's wait answers \"nothing pending\" without the lock when the layer is not on or no compositor hold is alive, and looks at compositor holds only",
        order(kw, [r"w->result\s*=\s*NVRM_FENCE_R_NONE\s*;", r"if\s*\(\s*gFState\s*!=\s*1\s*\|\|\s*!\s*gT\.liveCompositor\s*\|\|\s*!\s*w->id\s*\)\s*return\s+NVRM_FENCE_ST_OK\s*;",
                   r"\bq\s*=\s*\{\s*w->id\s*,\s*w->planes\s*&\s*0xFFu\s*,\s*w->pid\s*,\s*NVRM_FENCE_F_COMPOSITOR\s*,\s*0\s*,\s*0\s*\}\s*;", r"\bnvf_wait\s*\("])
        and "nvfLock" not in kw)

    # ---- start, stop, sleep, wake, a closing connection -------------------------------------------------------------
    stop, pm, gone = b("nvrm_fence_stop"), b("nvrm_fence_pm"), b("nvrm_fence_owner_gone")
    arm("before the first start nothing here touches the lock: stop, sleep and wake, and a closing connection return at once",
        statements(stop)[:1] == ["if (!gFLock) return;"] and statements(pm)[:1] == ["if (!gFLock) return;"]
        and statements(gone)[:1] == ["if (!ownerId || !gFLock) return;"])
    arm("the lock is made once, by whichever start comes first, and neither it nor the table is ever freed",
        order(start, [r"if\s*\(\s*!\s*gFLock\s*\)\s*\{", r"\block\s*=\s*IOLockAlloc\s*\(\s*\)\s*;", r"if\s*\(\s*!\s*lock\s*\)\s*return\s*;",
                      r"if\s*\(\s*!\s*OSCompareAndSwapPtr\s*\(\s*NULL\s*,\s*lock\s*,\s*\(\s*void\s*\*\s*volatile\s*\*\s*\)\s*&gFLock\s*\)\s*\)\s*IOLockFree\s*\(\s*lock\s*\)\s*;",
                      r"\bnvfLock\s*\(\s*\)\s*;", r"if\s*\(\s*!\s*gFInited\s*\)\s*\{\s*nvf_init\s*\(\s*&gT\s*\)\s*;\s*gT\.off\s*=\s*1\s*;"])
        and count(code, r"\bIOLockFree\s*\(") == 1 and count(code, r"\bnvf_init\s*\(") == 1 and not re.search(r"\bgFLock\s*=(?!=)", code))
    arm("the layer goes on in one place: the first GPU's start, and not after a failed resume or after it switched itself off",
        count(code, r"\bgFState\s*=\s*1\s*;") == 1
        and re.search(r"if\s*\(\s*\+\+gFCards\s*==\s*1\s*&&\s*!\s*gFLost\s*&&\s*!\s*gT\.fused\s*\)\s*\{\s*gT\.off\s*=\s*0\s*;\s*gT\.paused\s*=\s*0\s*;\s*gFSuspended\s*=\s*0\s*;\s*gFState\s*=\s*1\s*;\s*\}", start) is not None
        and re.search(r"if\s*\(\s*gT\.fused\s*&&\s*gFState\s*==\s*1\s*\)\s*gFState\s*=\s*2\s*;", unlock) is not None)
    arm("the last GPU's stop ends every hold, which wakes every sleeper, and a stop without its start does nothing",
        order(stop, [r"\bnvfLock\s*\(\s*\)\s*;", r"if\s*\(\s*gFCards\s*>\s*0\s*&&\s*--gFCards\s*==\s*0\s*\)\s*\{", r"gT\.off\s*=\s*1\s*;",
                     r"nvf_end_all\s*\(\s*&gT\s*,\s*NVF_END_SHUTDOWN\s*\)\s*;", r"if\s*\(\s*gFState\s*==\s*1\s*\)\s*gFState\s*=\s*3\s*;", r"\bnvfUnlock\s*\(\s*\)\s*;"]))
    arm("going to sleep ends every hold and refuses new ones until each suspend has had its answer; a failed resume keeps the layer off",
        order(pm, [r"if\s*\(\s*gFCards\s*>\s*0\s*\)\s*\{", r"if\s*\(\s*what\s*==\s*NVRM_FENCE_PM_SUSPENDING\s*\)\s*\{", r"gFSuspended\+\+\s*;", r"gT\.paused\s*=\s*1\s*;",
                   r"nvf_end_all\s*\(\s*&gT\s*,\s*NVF_END_PM\s*\)\s*;", r"else\s+if\s*\(\s*what\s*==\s*NVRM_FENCE_PM_RESUMED\s*\|\|\s*what\s*==\s*NVRM_FENCE_PM_LOST\s*\)\s*\{",
                   r"if\s*\(\s*gFSuspended\s*>\s*0\s*&&\s*--gFSuspended\s*==\s*0\s*\)\s*gT\.paused\s*=\s*0\s*;", r"if\s*\(\s*what\s*==\s*NVRM_FENCE_PM_LOST\s*\)\s*\{",
                   r"gFLost\s*=\s*true\s*;", r"gT\.off\s*=\s*1\s*;", r"nvf_end_all\s*\(\s*&gT\s*,\s*NVF_END_SHUTDOWN\s*\)\s*;"])
        and count(code, r"\bgFLost\s*=(?!=)") == 1 and count(code, r"\bgT\.paused\s*=\s*0\s*;") == 2)
    arm("a closing connection: its holds and channels end under the lock, its pages are let go after it, in every state",
        order(gone, [r"\bnvfLock\s*\(\s*\)\s*;", r"\bn\s*=\s*nvf_owner_gone\s*\(\s*&gT\s*,\s*ownerId\s*,\s*idx\s*,\s*NVF_OWNER_CHANS\s*\)\s*;", r"\bnvfTake\s*\(\s*idx\[i\]",
                     r"\bnvfUnlock\s*\(\s*\)\s*;", r"\bnvfUnpin\s*\(\s*gone\[i\]\s*\)\s*;", r"if\s*\(\s*n\s*<\s*NVF_OWNER_CHANS\s*\)\s*break\s*;"])
        and "gFState" not in gone)

    # ---- what is written down, and what is said -----------------------------------------------------------------------
    m = re.search(r"((?:^//[^\n]*\n)+)static\s+IOLock\s*\*\s*gFLock\s*;", src, re.M)
    note = squash(m.group(1).replace("//", " ")) if m else ""
    arm("the lock order is written down where the lock is declared",
        all(s in note for s in ("a leaf", "gate -> gFLock", "accelerator lock -> gFLock", "never the other way round", "Under it:", "Before it:", "After it:",
                                "takes no lock at all")))
    logs = re.findall(r'\bIOLog\s*\(\s*"([^"]*)"', src)
    arm("every log line names the fence, and each kind is rate-limited: by the table, or the first 8 and every 256th here",
        len(logs) == count(code, r"\bIOLog\s*\(") == 5 and all(s.startswith("NVRM-xnu: fence: ") and s.endswith("\\n") for s in logs)
        and sorted(n for n, x in fn.items() if re.search(r"\bIOLog\s*\(", x)) == ["nvfLog", "nvfMapFailed"]
        and re.search(r"\bn\s*=\s*OSIncrementAtomic\s*\(\s*&gFMapLogs\s*\)\s*;\s*if\s*\(\s*n\s*<\s*8\s*\|\|\s*\(\s*\(\s*n\s*\+\s*1\s*\)\s*&\s*0xFF\s*\)\s*==\s*0\s*\)\s*IOLog\s*\(", b("nvfMapFailed")) is not None)

    # ---- the table header -------------------------------------------------------------------------------------------
    tcode = code_only(table)
    defs = re.findall(r"^(?!\s)([^\n#/{}]*?)\b(nvf_\w+)\s*\([^;{]*\)\s*\n?\{", tcode, re.M)
    arm("the table header includes only the ABI header, defines nothing but static inline functions, and calls nothing of a kernel or a C library",
        re.findall(r"#\s*include\s*([<\"][^>\"]+[>\"])", table) == ['"nvrm_fence_abi.h"'] and len(defs) > 30
        and all(squash(d[0]).startswith("static inline ") for d in defs)
        and not re.search(r"\b(?:IOLog|printf|kprintf|malloc|free|IOMalloc\w*|memcpy|memset|memcmp|strlen|IOLock\w+|OS\w+Atomic\w*|mach_absolute_time|thread_\w+|assert_wait\w*)\s*\(", tcode))
    return out


def broken(src, old, new):
    assert src.count(old) >= 1, "control text not found: " + old
    return src.replace(old, new, 1)


def moved(src, line, after):
    """`line` taken out and put back right after `after`."""
    return broken(broken(src, line, ""), after, after + line)


KICK = "    OSIncrementAtomic64(&gFKick);\n    if (gFSleepers > 0) thread_wakeup((event_t)&gFKick);\n"
RECHECK = "        if ((unsigned)gFKick != kickSeen || (unsigned)gFGen != genSeen) thread_wakeup((event_t)&gFKick);\n"
ASSERT_WAIT = "    wait_result_t wr = assert_wait_timeout((event_t)&gFKick, abortable ? THREAD_ABORTSAFE : THREAD_UNINT, (uint32_t)us, NSEC_PER_USEC);\n"
CLAMP = "    if (us > NVRM_FENCE_WAIT_MAX_MS * 1000ull) us = NVRM_FENCE_WAIT_MAX_MS * 1000ull;\n"
UNLOCK = "    IOLockUnlock(gFLock);\n"
WAKE = "    if (changed) {\n        OSIncrementAtomic(&gFGen);\n        if (gFSleepers > 0) thread_wakeup((event_t)&gFKick);\n    }\n"
LOGS = "    for (unsigned i = 0; i < n; i++) nvfLog(&ev[i]);\n"
PREPARE = "    if (md->prepare() != kIOReturnSuccess) { md->release(); return NULL; }\n"
PROBE1 = "    if (!nvfRead64(sem, NVRM_FENCE_SEM_PROBE_OFF, &seen) || seen != c->probe) {\n"
ADD = "    st = *closing ? NVRM_FENCE_ST_OFF : nvf_chan_add(&gT, owner, (unsigned)proc_selfpid(), r->flags, &handle);\n"
ADD_OWNER = "    const UInt64 owner = nvfOwner(ownerId);\n    unsigned handle = 0, st;\n    nvfLock();\n"
ADD_TAIL = "    nvfUnlock();\n    nvfUnpin(sem); nvfUnpin(err);\n    c->chan = handle;\n"
PROBE2 = " &&\n                  seen != gFMap[ci].probe0;\n"
REBIND_PIN = "    IOMemoryDescriptor *err = nvfPin(c->errVA & ~(NVF_PAGE - 1));\n"
REBIND_IF = "    if (r->status == NVRM_FENCE_ST_OK && ci < NVF_CHANS) {\n        IOMemoryDescriptor *old = gFMap[ci].err;\n"
REBIND_TAIL = "    nvfUnlock();\n    nvfUnpin(err);          // the old page, or the new one when the request was refused\n"
DEL_TAIL = "    if (freed != NVF_NIL) nvfTake(freed, &sem, &err);\n    nvfUnlock();\n    nvfUnpin(sem); nvfUnpin(err);\n}\n\n// CHAN_REBIND"
GATE = "    if (gFState != 1) { r->status = NVRM_FENCE_ST_OFF; return; }\n"
CASE_ADD = "    case NVRM_FENCE_OP_CHAN_ADD:    nvfChanAdd(ownerId, closing, r); break;\n"
DENIED = "    if ((flags & NVRM_FENCE_F_COMPOSITOR) && !nvfCallerIsWindowServer()) {\n"
OPEN = "    r->status = *closing ? NVRM_FENCE_ST_OFF : nvf_open(&gT, owner, pid, h->cookie, h->count, h->ids, h->planes, flags, now);\n"
SHORTCUT = "    if (gFState != 1 || !gT.liveCompositor || !w->id) return NVRM_FENCE_ST_OK;\n"
KWAIT = "    return nvf_wait(&gT, &gFOps, NULL, &q, w->timeoutMs, (flags & NVRM_FENCE_F_LOOK) != 0, 0, w);\n"
OPS_CHECK = "    if (!o || o->version != NVRM_FENCE_ABI_VERSION || o->size != sizeof(*o)) return false;\n"
OPS_OFF = "    if (nvfBoot() == 2) return false;\n"
START_OFF = "    if (boot == 2) return;\n"
START_ON = "    if (++gFCards == 1 && !gFLost && !gT.fused) {\n"
STOP_END = "        nvf_end_all(&gT, NVF_END_SHUTDOWN);              // sleepers wake, find nothing pending and leave with DEAD\n"
PM_GUARD = "void nvrm_fence_pm(int what)\n{\n    if (!gFLock) return;\n"
PM_END = "            nvf_end_all(&gT, NVF_END_PM);                // and every reader leaves now, before the interrupts stop\n"
PM_LOST = "                gFLost = true;\n"
GONE_TAIL = "        nvfUnlock();\n        for (unsigned i = 0; i < 2 * n; i++) nvfUnpin(gone[i]);\n"
ONE_DECL = 'SYSCTL_QUAD(_debug, OID_AUTO, nvrm_fence_strikes, CTLFLAG_RD | CTLFLAG_LOCKED, NVF_STAT(STRIKES), "strikes against connections whose holds were given up");\n'
ONE_REG = "    sysctl_register_oid(&sysctl__debug_nvrm_fence_strikes);\n"
ONCE = "    if (!OSCompareAndSwap(0, 1, (volatile UInt32 *)&gFSysctls)) return;\n"
ORDER_NOTE = "//   Before it: pinning a page (that can fault and block). After it: unpinning, every wakeup, every log line.\n"

# (what is broken, a piece of the name of the arm that must fail, which text, how)
CONTROLS = [
    ("a function defined with another parameter", "every function nvrm_fence.h declares", "cpp",
     lambda s: broken(s, "void nvrm_fence_pm(int what)\n{", "void nvrm_fence_pm(unsigned what)\n{")),
    ("a declared function that is not defined", "every function nvrm_fence.h declares", "cpp",
     lambda s: broken(s, "void nvrm_fence_kick(void)\n{", "static void nvrmFenceKick(void)\n{")),
    ("an RM call in the fence", "makes no RM call", "cpp", lambda s: broken(s, GATE, GATE + "    rm_isr(NULL, NULL, NULL);\n")),
    ("a second boot-arg", "one opt-out", "cpp",
     lambda s: broken(s, START_OFF, START_OFF + '    UInt32 on = 0; if (PE_parse_boot_argn("nvrmfenceon", &on, sizeof(on)) && !on) return;\n')),
    ("the boot-arg as an opt-in", "one opt-out", "cpp", lambda s: broken(s, "&& v == 0) ? 2 : 1;", "&& v == 1) ? 1 : 2;")),
    ("switched off, the start still makes the lock", "switched off: the start", "cpp", lambda s: broken(s, START_OFF, "")),
    ("NVAccel gets the wait although the layer is switched off", "NVAccel is handed the wait", "cpp", lambda s: broken(s, OPS_OFF, "")),
    ("the wait is written before version and size are checked", "NVAccel is handed the wait", "cpp",
     lambda s: broken(broken(s, OPS_CHECK, ""), "    o->wait = nvfKernelWait;\n", "    o->wait = nvfKernelWait;\n" + OPS_CHECK)),
    ("a writable sysctl", "every sysctl is a plain read-only declaration", "cpp",
     lambda s: broken(s, "nvrm_fence_strikes, CTLFLAG_RD | CTLFLAG_LOCKED", "nvrm_fence_strikes, CTLFLAG_RW | CTLFLAG_LOCKED")),
    ("a sysctl outside the fence's names", "every sysctl is a plain read-only declaration", "cpp",
     lambda s: broken(broken(s, "OID_AUTO, nvrm_fence_strikes,", "OID_AUTO, nvrm_strikes,"), "sysctl__debug_nvrm_fence_strikes", "sysctl__debug_nvrm_strikes")),
    ("a sysctl that is declared and never registered", "each is registered exactly once", "cpp", lambda s: broken(s, ONE_REG, "")),
    ("a sysctl registered twice", "each is registered exactly once", "cpp", lambda s: broken(s, ONE_REG, ONE_REG + ONE_REG)),
    ("the registration can run twice", "each is registered exactly once", "cpp", lambda s: broken(s, ONCE, "")),
    ("a counter of the table without a sysctl", "every counter of the table", "cpp", lambda s: broken(broken(s, ONE_DECL, ""), ONE_REG, "")),
    ("a sysctl that shows another counter than its name says", "every counter of the table", "cpp",
     lambda s: broken(broken(s, "NVF_STAT(STRIKES)", "NVF_STAT(@)"), "NVF_STAT(OPENS)", "NVF_STAT(STRIKES)").replace("NVF_STAT(@)", "NVF_STAT(OPENS)")),
    ("the kick looks for sleepers before it moves its generation", "the kick is three statements", "cpp",
     lambda s: broken(s, KICK, "    if (gFSleepers > 0) thread_wakeup((event_t)&gFKick);\n    OSIncrementAtomic64(&gFKick);\n")),
    ("the kick takes the lock", "the kick is three statements", "cpp", lambda s: broken(s, KICK, "    nvfLock();\n" + KICK + "    nvfUnlock();\n")),
    ("the kick wakes whether or not anybody sleeps", "the kick is three statements", "cpp",
     lambda s: broken(s, KICK, "    OSIncrementAtomic64(&gFKick);\n    thread_wakeup((event_t)&gFKick);\n")),
    ("the sleep does not read the generations again", "the sleep counts itself in", "cpp", lambda s: broken(s, RECHECK, "")),
    ("the sleep reads them again before it is queued", "the sleep counts itself in", "cpp",
     lambda s: broken(broken(s, RECHECK, ""), ASSERT_WAIT, RECHECK + ASSERT_WAIT)),
    ("the sleep counts itself in after it is queued", "the sleep counts itself in", "cpp",
     lambda s: broken(broken(s, "    if (OSIncrementAtomic(&gFSleepers) >= kNvfMaxSleepers && abortable) {\n        OSDecrementAtomic(&gFSleepers);\n        return NVF_SLEEP_FULL;\n    }\n", ""),
                      ASSERT_WAIT, ASSERT_WAIT + "    OSIncrementAtomic(&gFSleepers);\n")),
    ("a second sleep, without a bound", "it is the only sleep of the file", "cpp",
     lambda s: broken(s, GATE, GATE + "    assert_wait((event_t)&gFKick, THREAD_UNINT); (void)thread_block(THREAD_CONTINUE_NULL);\n")),
    ("a call the kernel does not export", "it is the only sleep of the file", "cpp", lambda s: broken(s, RECHECK, "        clear_wait(current_thread(), THREAD_AWAKENED);\n" + RECHECK)),
    ("the sleep is not clamped", "every sleep is bounded", "cpp", lambda s: broken(s, CLAMP, "")),
    ("NVAccel's sleep can be refused for the number of sleepers", "only a sleep that a signal can end", "cpp",
     lambda s: broken(s, ">= kNvfMaxSleepers && abortable)", ">= kNvfMaxSleepers)")),
    ("every sleep can be interrupted", "only a sleep that a signal can end", "cpp",
     lambda s: broken(s, "abortable ? THREAD_ABORTSAFE : THREAD_UNINT", "THREAD_ABORTSAFE")),
    ("the sleepers are woken before the lock is released", "the one unlock", "cpp", lambda s: moved(s, UNLOCK, WAKE)),
    ("the log lines are written under the lock", "the one unlock", "cpp", lambda s: moved(s, UNLOCK, LOGS)),
    ("a second unlock that wakes nobody", "the one unlock", "cpp", lambda s: broken(s, ADD_TAIL, ADD_TAIL.replace("nvfUnlock();", "IOLockUnlock(gFLock);"))),
    ("a function leaves with the lock held", "every function that takes the lock", "cpp",
     lambda s: broken(s, "    r->status = st;\n}\n\nstatic void nvfClose", "    nvfLock();\n    r->status = st;\n}\n\nstatic void nvfClose")),
    ("a page let go under the lock", "under the lock: no log line", "cpp",
     lambda s: broken(s, DEL_TAIL, DEL_TAIL.replace("    nvfUnlock();\n    nvfUnpin(sem); nvfUnpin(err);\n", "    nvfUnpin(sem); nvfUnpin(err);\n    nvfUnlock();\n"))),
    ("a return between the lock and the unlock", "under the lock: no log line", "cpp", lambda s: broken(s, ADD, ADD + "    if (st == NVRM_FENCE_ST_FULL) return;\n")),
    ("a log line under the lock", "under the lock: no log line", "cpp", lambda s: broken(s, STOP_END, STOP_END + '        IOLog("NVRM-xnu: fence: stopped\\n");\n')),
    ("a counter helper that takes the lock again under the lock", "under the lock: no log line", "cpp", lambda s: broken(s, ADD, ADD + "    nvfCount(NVF_STAT_CHAN_ADDS);\n")),
    ("a page read without being wired", "a page is pinned in the caller's task", "cpp", lambda s: broken(s, PREPARE, "")),
    ("a page pinned for writing", "a page is pinned in the caller's task", "cpp", lambda s: broken(s, "NVF_PAGE, kIODirectionOut, current_task()", "NVF_PAGE, kIODirectionInOut, current_task()")),
    ("released before completed", "letting go is complete, then release", "cpp", lambda s: broken(s, "    md->complete();\n    md->release();\n", "    md->release();\n    md->complete();\n")),
    ("a page mapped into the kernel", "no page of a process is ever mapped", "cpp",
     lambda s: broken(s, "    return md;\n", "    (void)md->createMappingInTask(kernel_task, 0, kIOMapAnywhere | kIOMapReadOnly);\n    return md;\n")),
    ("the table allocated at start", "no page of a process is ever mapped", "cpp", lambda s: broken(s, START_OFF, START_OFF + "    void *t = IOMallocZero(sizeof(struct nvf_table)); (void)t;\n")),
    ("the read buffer is not zeroed", "a pinned page is read in one place", "cpp", lambda s: broken(s, "    *out = 0;\n    return md->readBytes", "    return md->readBytes")),
    ("the flush value read before the completion value", "the table's view of a channel", "cpp",
     lambda s: moved(s, "    if (!nvfRead64(m->sem, 0, &d)) return 0;\n", "    if (flushed && !nvfRead64(m->sem, NVRM_FENCE_SEM_FLUSHED_OFF, &f)) return 0;\n")),
    ("a channel registered without the probe", "CHAN_ADD: addresses checked", "cpp", lambda s: broken(s, PROBE1, "    if (!nvfRead64(sem, NVRM_FENCE_SEM_PROBE_OFF, &seen)) {\n")),
    ("a channel's pages pinned under the lock", "CHAN_ADD: addresses checked", "cpp",
     lambda s: broken(broken(s, "    IOMemoryDescriptor *sem = nvfPin(c->semVA);\n", "    IOMemoryDescriptor *sem = NULL;\n"), ADD_OWNER, ADD_OWNER + "    sem = nvfPin(c->semVA);\n")),
    ("CHAN_ADD does not look at the closing word", "CHAN_ADD: the owner is named", "cpp", lambda s: broken(s, "st = *closing ? NVRM_FENCE_ST_OFF : nvf_chan_add(", "st = nvf_chan_add(")),
    ("CHAN_ADD names the owner under the lock", "CHAN_ADD: the owner is named", "cpp",
     lambda s: broken(s, ADD_OWNER, "    unsigned handle = 0, st;\n    nvfLock();\n    const UInt64 owner = nvfOwner(ownerId);\n")),
    ("CHAN_VERIFY accepts the value of the registration", "CHAN_VERIFY: the view must show", "cpp", lambda s: broken(s, PROBE2, ";\n")),
    ("CHAN_VERIFY believes the caller", "CHAN_VERIFY: the view must show", "cpp", lambda s: broken(s, "    int matches = 0;\n", "    int matches = 0;\n    matches = 1;\n")),
    ("CHAN_REBIND pins under the lock", "CHAN_REBIND: the new notifier is pinned", "cpp",
     lambda s: broken(broken(s, REBIND_PIN, "    IOMemoryDescriptor *err = NULL;\n"), "    unsigned ci = NVF_NIL;\n    nvfLock();\n", "    unsigned ci = NVF_NIL;\n    nvfLock();\n    err = nvfPin(c->errVA & ~(NVF_PAGE - 1));\n")),
    ("CHAN_REBIND swaps whatever the table answered", "CHAN_REBIND: the new notifier is pinned", "cpp", lambda s: broken(s, REBIND_IF, REBIND_IF.replace("r->status == NVRM_FENCE_ST_OK && ", ""))),
    ("CHAN_REBIND lets the old notifier go under the lock", "CHAN_REBIND: the old notifier", "cpp",
     lambda s: broken(s, REBIND_TAIL, "    nvfUnpin(err);\n    nvfUnlock();\n")),
    ("CHAN_REBIND forgets the semaphore page", "CHAN_REBIND: the old notifier", "cpp", lambda s: broken(s, REBIND_IF, REBIND_IF + "        gFMap[ci].sem = NULL;\n")),
    ("CHAN_DEL refused when the layer is off", "CHAN_DEL is served in every state", "cpp",
     lambda s: broken(s, "    if (!gFLock) { r->status = NVRM_FENCE_ST_OFF; return; }\n    const unsigned long long now = nvfNowNs();", "    if (gFState != 1) { r->status = NVRM_FENCE_ST_OFF; return; }\n    const unsigned long long now = nvfNowNs();")),
    ("requests are served while the layer is off", "a request is answered \"off\"", "cpp", lambda s: broken(s, GATE, "")),
    ("CHAN_ADD is served before the layer is asked", "a request is answered \"off\"", "cpp",
     lambda s: broken(broken(s, CASE_ADD, ""), GATE, "    if (r->op == NVRM_FENCE_OP_CHAN_ADD) { nvfChanAdd(ownerId, closing, r); return; }\n" + GATE)),
    ("any process may take a compositor hold", "OPEN: a compositor hold", "cpp", lambda s: broken(s, DENIED, "    if (0) {\n")),
    ("OPEN does not look at the closing word", "OPEN: a compositor hold", "cpp", lambda s: broken(s, OPEN, OPEN.replace("*closing ? NVRM_FENCE_ST_OFF : ", ""))),
    ("a wait gives a connection an owner number", "only CHAN_ADD and OPEN", "cpp", lambda s: broken(s, "nvf_wait_seq(&gT, &gFOps, NULL, *ownerId,", "nvf_wait_seq(&gT, &gFOps, NULL, nvfOwner(ownerId),")),
    ("NVAccel's wait can be interrupted", "user waits can be interrupted", "cpp", lambda s: broken(s, KWAIT, KWAIT.replace("!= 0, 0, w)", "!= 0, 1, w)"))),
    ("a user wait cannot be interrupted", "user waits can be interrupted", "cpp", lambda s: broken(s, "w->timeoutMs, 1, w);\n        break;\n    }\n    default", "w->timeoutMs, 0, w);\n        break;\n    }\n    default")),
    ("NVAccel's wait always takes the lock", "NVAccel's wait answers", "cpp", lambda s: broken(s, SHORTCUT, "    if (!w->id) return NVRM_FENCE_ST_OK;\n")),
    ("NVAccel's wait looks at every hold", "NVAccel's wait answers", "cpp", lambda s: broken(s, "w->pid, NVRM_FENCE_F_COMPOSITOR, 0, 0 };", "w->pid, flags & NVRM_FENCE_F_COMPOSITOR, 0, 0 };")),
    ("sleep and wake touch the lock before the first start", "before the first start nothing here touches the lock", "cpp", lambda s: broken(s, PM_GUARD, "void nvrm_fence_pm(int what)\n{\n")),
    ("the lock is freed at the last stop", "the lock is made once", "cpp", lambda s: broken(s, STOP_END, STOP_END + "        IOLockFree(gFLock);\n")),
    ("the table is cleared at every start", "the lock is made once", "cpp", lambda s: broken(s, "    if (!gFInited) { nvf_init(&gT);", "    { nvf_init(&gT);")),
    ("a start revives a layer that a failed resume switched off", "the layer goes on in one place", "cpp", lambda s: broken(s, START_ON, "    if (++gFCards == 1 && !gT.fused) {\n")),
    ("the last stop leaves the holds", "the last GPU's stop", "cpp", lambda s: broken(s, STOP_END, "")),
    ("going to sleep leaves the holds", "going to sleep ends every hold", "cpp", lambda s: broken(s, PM_END, "")),
    ("a failed resume is forgotten", "going to sleep ends every hold", "cpp", lambda s: broken(s, PM_LOST, "")),
    ("a closing connection's pages are let go under the lock", "a closing connection: its holds", "cpp",
     lambda s: broken(s, GONE_TAIL, "        for (unsigned i = 0; i < 2 * n; i++) nvfUnpin(gone[i]);\n        nvfUnlock();\n")),
    ("a closing connection is ignored when the layer is off", "a closing connection: its holds", "cpp",
     lambda s: broken(s, "    if (!ownerId || !gFLock) return;\n", "    if (!ownerId || !gFLock) return;\n    if (gFState != 1) return;\n")),
    ("the lock order is not written down", "the lock order is written down", "cpp", lambda s: broken(s, ORDER_NOTE, "")),
    ("a log line that is not rate-limited", "every log line names the fence", "cpp", lambda s: broken(s, PM_LOST, PM_LOST + '                IOLog("NVRM-xnu: fence: the GPU did not come back\\n");\n')),
    ("a log line that does not say where it comes from", "every log line names the fence", "cpp", lambda s: broken(s, '"NVRM-xnu: fence: pid %d: %s - this channel', '"NVRM-xnu: pid %d: %s - this channel')),
    ("the table header takes a system header", "the table header includes only", "table", lambda s: broken(s, '#include "nvrm_fence_abi.h"\n', '#include "nvrm_fence_abi.h"\n#include <string.h>\n')),
    ("a function of the table header that is not inline", "the table header includes only", "table",
     lambda s: broken(s, "static inline unsigned nvf_bucket(unsigned sid)", "unsigned nvf_bucket(unsigned sid)")),
]


def main():
    src = (root / "kexts/NVRM/nvrm_fence.cpp").read_text()
    header = (root / "kexts/NVRM/nvrm_fence.h").read_text()
    table = (root / "kexts/NVRM/nvrm_fence_table.h").read_text()
    fails = 0
    for name, ok in arms(src, header, table):
        print(("ok   " if ok else "FAIL ") + name)
        fails += 0 if ok else 1
    for name, arm_piece, which, mutate in CONTROLS:
        try:
            res = arms(mutate(src), header, table) if which == "cpp" else arms(src, header, mutate(table))
        except AssertionError as e:
            print("FAIL control '%s': %s" % (name, e))
            fails += 1
            continue
        hit = [a for a, ok in res if not ok and arm_piece in a]
        print(("ok   " if hit else "FAIL ") + "control: %s is caught" % name)
        fails += 0 if hit else 1
    print("%d failed" % fails)
    return 1 if fails else 0


sys.exit(main())
