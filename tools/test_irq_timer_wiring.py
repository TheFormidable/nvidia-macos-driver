#!/usr/bin/env python3
"""The interrupt path, the RM's timers and the per-GPU interrupt identity, as they are written in NVRM.kext's sources.

None of it can run without the card, so what must hold is asserted on the source text (comments left out):
- the bottom-half thread and the RC timer are on by default: only the value 0 of nvrmbhthread / nvrmrctimer /
  nvrmnanowait switches each off (an old "=1" spelling still means the default);
- interruptOccurred enters the interrupt gate before rm_isr, turns away what finds it closed without calling the RM,
  and requests the bottom half (on the request alone) before it leaves the gate; it marks its thread in its own GPU's
  slot around rm_isr; the count that paces its log line is 64 bits; the primary filter neither logs nor kicks anything;
- the bottom-half thread asks nv_bh_next() what to do, so a pending run is served before it exits;
- every fIntSrc->enable() has irqOpen() before it; gpuSuspend, systemWillShutdown, stop and shutdownFull disable the
  source and then irqSettle();
- go() claims the GPU's interrupt-thread slot before the event source exists and refuses without one; stop() gives it
  back after the event source is gone;
- a second try of go()'s second pass is refused while the first try's interrupt source is there, and neither the
  source nor the work loop is ever made over an existing one (the first stayed registered, with nothing to remove it);
- os_is_isr() answers from the per-GPU slots; the single global is gone from every source; os_get_cpu_count() is the
  kernel's real_ncpus (at least 1) and os_get_cpu_number() stays 0;
- no source names thread_call_cancel_wait (not exported to kexts); nv_destroy_nano_timer frees nothing (the slots are
  type-stable); a nano or RC callback reaches the RM only after its generation matched, and the RC tick re-arms only
  with the generation it ran with;
- the waits of the quiesce (irqSettle, the timer waits) ask nv_quiesce_may_sleep(), the kernel's own
  preemption_enabled(), and never nvx_can_block();
- a nano timer's deadline is computed in 64-bit nanoseconds (the old 32-bit microsecond count ran out at 71 minutes);
- the timers' counters are read-only sysctls, each registered by nv_xnu_timers_sysctls(), which startGPU calls;
- build_nvrm.sh compiles nv_xnu_timers.cpp;
- every hook of these paths does nothing when its start has not run (a sleep, a shutdown or a stop of a GPU whose
  start stopped half-way): the gate starts open and idle, the slot at -1, the bottom-half calls return without a
  thread, the RC calls without a slot, the fence calls without a fence start.

Each arm has a negative control: the same check on the text with that one thing broken must fail.
Usage: test_irq_timer_wiring.py [driver checkout]"""
import pathlib
import re
import sys

root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1])
NV = "kexts/NVRM/"
FILES = {"nvrm": NV + "NVRM.cpp", "os": NV + "os-xnu.cpp", "os2": NV + "os-xnu2.cpp", "timers": NV + "nv_xnu_timers.cpp",
         "quiesce": NV + "nv_xnu_quiesce.h", "build": "build/build_nvrm.sh"}
LEX = re.compile(r"""//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])'""", re.S)


def no_comments(text):
    """Comments blanked (strings kept); offsets unchanged."""
    return LEX.sub(lambda m: m.group(0) if m.group(0)[0] in "\"'" else re.sub(r"[^\n]", " ", m.group(0)), text)


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


def order(text, patterns):
    """True when each pattern occurs, each after the previous one."""
    pos = 0
    for p in patterns:
        m = re.compile(p).search(text, pos)
        if not m:
            return False
        pos = m.end()
    return True


def kext_sources():
    out = {}
    for d in ("kexts/NVRM", "kexts/NVRMFB", "kexts/NVRMAGDC"):
        for p in sorted((root / d).rglob("*")):
            if p.suffix in (".cpp", ".c", ".h", ".mm", ".m") and p.is_file():
                out[str(p.relative_to(root))] = p.read_text(errors="replace")
    return out


CALL_ENABLE = r"fIntSrc\s*->\s*enable\s*\("


def arms(src, others):
    """[(name, ok)] for the given texts (comments already blanked)."""
    n, os_, t, q, b = src["nvrm"], src["os"], src["timers"], src["quiesce"], src["build"]
    out = []

    def arm(name, ok):
        out.append((name, bool(ok)))

    bh_on = body(n, "nvrmBhThreadOn")
    opted = body(t, "nv_xnu_opted_out")
    arm("opt-out, not opt-in: the bottom-half thread is off only with nvrmbhthread=0",
        re.search(r'PE_parse_boot_argn\s*\(\s*"nvrmbhthread"', bh_on) and re.search(r"v\s*==\s*0\s*\)\s*\?\s*0\s*:\s*1", bh_on)
        and "on != 1" not in bh_on + body(n, "NVRM::bottomHalfStart") and "v == 1" not in bh_on)
    arm("opt-out, not opt-in: the RC timer is off only with nvrmrctimer=0, the nano waits only with nvrmnanowait=0",
        re.search(r"PE_parse_boot_argn\s*\(\s*name\b", opted) and re.search(r"v\s*==\s*0\s*\)\s*\?\s*1\s*:\s*0", opted)
        and "v == 1" not in opted and re.search(r'nv_xnu_opted_out\s*\(\s*"nvrmrctimer"\s*\)', t)
        and re.search(r'nv_xnu_opted_out\s*\(\s*"nvrmnanowait"\s*\)', t))

    io = body(n, "NVRM::interruptOccurred")
    refused = re.search(r"if\s*\(\s*!\s*nv_gate_enter\s*\(\s*&fIrqGate\s*\)\s*\)\s*\{(.*?)\}", io, re.S)
    arm("interruptOccurred: the gate is entered before rm_isr, and a closed gate returns without the RM",
        refused and "return;" in refused.group(1) and not re.search(r"\brm_\w+\s*\(", refused.group(1))
        and io.find("nv_gate_enter(&fIrqGate)") < io.find("rm_isr("))
    arm("interruptOccurred: the bottom half is requested on the request alone, before the gate is left",
        order(io, [r"\brm_isr\s*\(", r"if\s*\(\s*needBottomHalf\s*\)", r"\bnv_bh_request\s*\(\s*&fBh\s*\)",
                   r"else\s+nvrmRunBottomHalf\s*\(\s*&fNv\s*\)", r"\bnv_gate_leave\s*\(\s*&fIrqGate\s*\)\s*;\s*$"])
        and "handled && needBottomHalf" not in io)
    arm("interruptOccurred: its thread is marked in its own GPU's slot around rm_isr",
        order(io, [r"nv_xnu_isr_threads\s*\[\s*fIsrSlot\s*\]\s*=\s*current_thread\s*\(\s*\)\s*;",
                   r"\brm_isr\s*\(", r"nv_xnu_isr_threads\s*\[\s*fIsrSlot\s*\]\s*=\s*NULL\s*;"]))
    arm("interruptOccurred: the count behind its log cadence is 64 bits (a 32-bit one turns negative and logs every interrupt)",
        re.search(r"static\s+volatile\s+SInt64\s+gNvIsrServed\b", n)
        and re.search(r"const\s+SInt64\s+n\s*=\s*OSIncrementAtomic64\s*\(\s*&gNvIsrServed\s*\)", io))
    filt = body(n, "NVRM::interruptFilter")
    arm("interruptFilter (primary interrupt context): no log line, no kick, no RM call",
        filt and not re.search(r"\b(kprintf|IOLog|LOG|nvrm_fence_kick|rm_\w+)\s*\(", filt))
    arm("the bottom-half thread asks nv_bh_next() what to do (a pending run is served before it exits)",
        re.search(r"nv_bh_next\s*\(\s*&s->fBh\s*\)", body(n, "NVRM::bottomHalfThread"))
        and not re.search(r"fBh\.quit|fBhQuit", body(n, "NVRM::bottomHalfThread")))

    wrong = []
    for name in re.findall(r"(?:void|IOReturn|bool)\s+(NVRM::\w+)\s*\(", n):
        bd = body(n, name)
        for m in re.finditer(CALL_ENABLE, bd):
            gates = re.findall(r"\b(irqOpen|irqSettle)\s*\(", bd[:m.start()])
            if not gates or gates[-1] != "irqOpen":
                wrong.append(name)
    arm("irqOpen() before every fIntSrc->enable(), and there are exactly two (go and gpuResume)",
        not wrong and len(re.findall(CALL_ENABLE, n)) == 2)
    settles = [("NVRM::gpuSuspend", ""), ("NVRM::systemWillShutdown", ""), ("NVRM::stop", ""), ("NVRM::shutdownFull", r"s\s*->\s*")]
    arm("gpuSuspend, systemWillShutdown, stop and shutdownFull disable the source and then settle the gate",
        all(re.search(p + r"fIntSrc\s*->\s*disable\s*\(\s*\)\s*;\s*" + p + r"irqSettle\s*\(\s*\d+\s*\)\s*;", body(n, f)) for f, p in settles))
    settle = body(n, "NVRM::irqSettle")
    arm("irqSettle closes the gate first, waits for it to be idle, then for the bottom half; it asks the kernel whether it may sleep",
        order(settle, [r"nv_gate_close\s*\(\s*&fIrqGate\s*\)", r"nv_quiesce_may_sleep\s*\(", r"nv_gate_idle", r"bottomHalfDrain\s*\("])
        and "nvx_can_block" not in settle and "fIntSrc" not in settle)
    arm("irqOpen opens the gate", re.search(r"nv_gate_open\s*\(\s*&fIrqGate\s*\)", body(n, "NVRM::irqOpen")))

    go, stop = body(n, "NVRM::go"), body(n, "NVRM::stop")
    arm("go() claims the interrupt-thread slot before the event source and refuses without one; stop() gives it back after the source is gone",
        order(go, [r"fIsrSlot\s*=\s*nv_xnu_isr_slot_claim\s*\(\s*this\s*\)", r"if\s*\(\s*fIsrSlot\s*<\s*0\s*\)\s*\{[^}]*return\s+kIOReturnNoResources",
                   r"addEventSource\s*\("])
        and order(stop, [r"removeEventSource\s*\(", r"bottomHalfStop\s*\(", r"nv_xnu_isr_slot_release\s*\(\s*fIsrSlot\s*,\s*this\s*\)"]))
    arm("go(): a second try of pass 2 is refused while the first try's interrupt source is there; no source or work loop is made over one",
        order(go, [r"if\s*\(\s*fIntSrc\s*\)\s*\{[^}]*return\s+kIOReturnError\s*;", r"nv_xnu_wq_gpu_start\s*\(",
                   r"if\s*\(\s*!\s*fWorkLoop\s*\)\s*fWorkLoop\s*=\s*IOWorkLoop::workLoop\s*\(\s*\)\s*;",
                   r"fIntSrc\s*=\s*IOFilterInterruptEventSource::filterInterruptEventSource\s*\("])
        and len(re.findall(r"\bfIntSrc\s*=\s*IOFilterInterruptEventSource\b", n)) == 1
        and len(re.findall(r"\bfWorkLoop\s*=\s*IOWorkLoop::workLoop\b", n)) == 1)
    arm("stop(): rm_disable_adapter before rm_shutdown_adapter; the timers let go before the private state is freed",
        order(stop, [r"rm_disable_adapter\s*\(\s*NULL\s*,\s*&fNv\s*\)", r"rm_shutdown_adapter\s*\(\s*NULL\s*,\s*&fNv\s*\)",
                     r"nv_xnu_rc_detach\s*\(\s*&fNv\s*\)", r"nv_xnu_nano_retire\s*\(\s*&fNv\s*\)", r"rm_free_private_state\s*\("]))

    isr = body(os_, "os_is_isr")
    arm("os_is_isr() answers from the per-GPU slots, and the single global is gone from every kext source",
        re.search(r"nv_xnu_isr_threads\s*\[\s*i\s*\]\s*==\s*me", isr)
        and not any(re.search(r"\bnv_xnu_isr_thread\b(?!s)", v) for v in others.values()))
    arm("os_get_cpu_count() is real_ncpus, at least 1; os_get_cpu_number() stays 0",
        re.search(r"=\s*real_ncpus\s*;\s*return\s+n\s*\?\s*n\s*:\s*1\s*;", body(os_, "os_get_cpu_count"))
        and re.search(r"return\s+0\s*;", body(os_, "os_get_cpu_number")) and "cpu_number()" not in body(os_, "os_get_cpu_number"))

    arm("no kext source names thread_call_cancel_wait (it is not exported to kexts)",
        not any("thread_call_cancel_wait" in v for v in others.values()))
    destroy = body(t, "nv_destroy_nano_timer") + body(t, "nv_xnu_nano_end") + body(t, "nv_xnu_nano_quiesce")
    arm("nv_destroy_nano_timer frees nothing: no kern_os_free, IOFree or thread_call_free on the way",
        destroy and not re.search(r"\b(kern_os_free|IOFree|thread_call_free)\s*\(", destroy))
    fire = body(t, "nv_xnu_nano_fire")
    arm("a nano timer callback reaches the RM only after its generation matched, and checks it once more after reading the slot",
        order(fire, [r"if\s*\(\s*!\s*nv_gen_enter\s*\(\s*&t->gate\s*,\s*token\s*\)\s*\)\s*\{[^}]*return\s*;", r"nv_gen_current\s*\(\s*&t->gate\s*\)\s*==\s*token",
                     r"rm_run_nano_timer_callback\s*\(", r"nv_gen_leave\s*\("]))
    tick = body(t, "nv_xnu_rc_tick")
    arm("the RC tick reaches the RM only after its generation matched, and re-arms only with the generation it ran with",
        order(tick, [r"if\s*\(\s*!\s*nv_gen_enter\s*\(\s*&rc->gate\s*,\s*token\s*\)\s*\)\s*\{[^}]*return\s*;", r"rm_run_rc_callback\s*\(",
                     r"if\s*\(\s*nv_gen_current\s*\(\s*&rc->gate\s*\)\s*==\s*token\s*\)\s*nv_xnu_rc_arm\s*\(\s*rc\s*,\s*token\s*\)", r"nv_gen_leave\s*\("]))
    arm("the quiesce waits ask nv_quiesce_may_sleep() (the kernel's preemption_enabled()), never nvx_can_block()",
        re.search(r"nv_quiesce_may_sleep\s*\(\s*void\s*\)\s*\{\s*return\s+preemption_enabled\s*\(\s*\)", q)
        and re.search(r"nv_quiesce_may_sleep\s*\(", body(t, "nv_xnu_wait_idle_until")) and "nvx_can_block" not in t)
    start = body(t, "nv_start_nano_timer")
    arm("a nano timer's deadline is computed in 64-bit nanoseconds",
        re.search(r"nanoseconds_to_absolutetime\s*\(\s*timens\s*,", start) and "clock_interval_to_deadline" not in start)
    decl = re.findall(r"SYSCTL_\w+\s*\(\s*_debug\s*,\s*OID_AUTO\s*,\s*(\w+)\s*,\s*([^,]+),", t)
    reg = body(t, "nv_xnu_timers_sysctls")
    arm("the timers' sysctls are read-only, each registered by nv_xnu_timers_sysctls(), which startGPU calls",
        decl and all("CTLFLAG_RD" in f and "CTLFLAG_RW" not in f and "CTLFLAG_WR" not in f for _, f in decl)
        and all(f"sysctl__debug_{nm})" in reg for nm, _ in decl)
        and re.search(r"nv_xnu_timers_sysctls\s*\(\s*\)\s*;", body(n, "NVRM::startGPU")))
    starts = lambda f, pat: re.match(r"\s*" + pat, f) is not None
    arm("every hook does nothing before its start: gate open, no slot, no thread, no RC slot, no fence start",
        re.search(r"NvXnuGate\s+fIrqGate\s*=\s*\{\s*\}\s*;", n) and re.search(r"int\s+fIsrSlot\s*=\s*-1\s*;", n)
        and starts(body(n, "NVRM::bottomHalfDrain"), r"if\s*\(\s*!\s*fBhLock\s*\)\s*return\s+true\s*;")
        and starts(body(n, "NVRM::bottomHalfStop"), r"if\s*\(\s*!\s*fBhLock\s*\)\s*return\s*;")
        and re.search(r"if\s*\(\s*q\s*\)\s*nv_xnu_wq_gpu_down", body(n, "NVRM::workQueueDown"))
        and re.search(r"if\s*\(\s*slot\s*<\s*0\s*\|\|", body(os_, "nv_xnu_isr_slot_release"))
        and all(re.search(r"if\s*\(\s*!rc\s*\|\|\s*!rc->call\s*\)\s*return\s*;", body(t, f)) for f in ("nv_xnu_rc_suspend", "nv_xnu_rc_resume"))
        and re.search(r"if\s*\(\s*i\s*<\s*0\s*\)\s*return\s*;", body(t, "nv_xnu_rc_detach"))
        and re.search(r"if\s*\(\s*fFenceStarted\s*&&", body(n, "NVRM::gpuSuspend"))
        and re.search(r"if\s*\(\s*OSCompareAndSwap\s*\(\s*1\s*,\s*0\s*,\s*&fFenceAsleep\s*\)\s*\)\s*nvrm_fence_pm", body(n, "NVRM::gpuResume")))
    arm("build_nvrm.sh compiles nv_xnu_timers.cpp",
        re.search(r"^CXX_SOURCES=\([^)]*\bnv_xnu_timers\.cpp\b", b, re.M)
        or re.search(r"^for s in [^;\n]*\bnv_xnu_timers\.cpp\b[^;\n]*; do\n\s*\[ -f \"\$SRC/\$s\" \] && CXX_SOURCES\+=\( \"\$s\" \)", b, re.M))
    return out


def broken(texts, key, old, new):
    assert texts[key].count(old) >= 1, f"control text not found in {key}: {old!r}"
    changed = dict(texts)
    changed[key] = texts[key].replace(old, new, 1)
    return changed


CONTROLS = [
    ("the bottom-half thread opt-in again", "opt-out, not opt-in: the bottom-half",
     lambda s: broken(s, "nvrm", "&& v == 0) ? 0 : 1;", "&& v == 1) ? 1 : 0;")),
    ("the RC timer opt-in again", "opt-out, not opt-in: the RC timer",
     lambda s: broken(s, "timers", "&& v == 0) ? 1 : 0;", "&& v == 1) ? 0 : 1;")),
    ("the gate entered after rm_isr", "interruptOccurred: the gate is entered",
     lambda s: broken(broken(s, "nvrm", "    if (!nv_gate_enter(&fIrqGate)) {", "    if (0) {"),
                      "nvrm", "    nv_xnu_isr_threads[fIsrSlot] = NULL;\n", "    nv_xnu_isr_threads[fIsrSlot] = NULL;\n    if (!nv_gate_enter(&fIrqGate)) return;\n")),
    ("the bottom half requested only when handled", "interruptOccurred: the bottom half is requested",
     lambda s: broken(s, "nvrm", "    if (needBottomHalf) {", "    if (handled && needBottomHalf) {")),
    ("the gate left before the request", "interruptOccurred: the bottom half is requested",
     lambda s: broken(broken(s, "nvrm", "    nv_gate_leave(&fIrqGate);\n}", "}"), "nvrm", "    if (needBottomHalf) {", "    nv_gate_leave(&fIrqGate);\n    if (needBottomHalf) {")),
    ("the slot not cleared after rm_isr", "interruptOccurred: its thread is marked",
     lambda s: broken(s, "nvrm", "    nv_xnu_isr_threads[fIsrSlot] = NULL;\n", "")),
    ("a 32-bit interrupt log count", "interruptOccurred: the count behind its log cadence",
     lambda s: broken(broken(s, "nvrm", "static volatile SInt64 gNvIsrServed", "static volatile SInt32 gNvIsrServed"),
                      "nvrm", "const SInt64 n = OSIncrementAtomic64(&gNvIsrServed)", "const SInt32 n = OSIncrementAtomic(&gNvIsrServed)")),
    ("a log line in the filter", "interruptFilter (primary",
     lambda s: broken(s, "nvrm", "    ++gNvIsrCount;", "    ++gNvIsrCount; kprintf(\"x\");")),
    ("the bottom-half thread tests quit itself", "the bottom-half thread asks",
     lambda s: broken(s, "nvrm", "        if (what == NV_BH_EXIT) break;", "        if (what == NV_BH_EXIT || s->fBh.quit) break;")),
    ("gpuResume without irqOpen", "irqOpen() before every",
     lambda s: broken(s, "nvrm", "    irqOpen();     ", "    ")),
    ("go() enabling before irqOpen", "irqOpen() before every",
     lambda s: broken(s, "nvrm", "        irqOpen();\n        fIntSrc->enable();", "        fIntSrc->enable();\n        irqOpen();")),
    ("gpuSuspend only drains the bottom half", "gpuSuspend, systemWillShutdown, stop",
     lambda s: broken(s, "nvrm", "    if (fIntSrc) fIntSrc->disable();\n    irqSettle(2000);", "    if (fIntSrc) fIntSrc->disable();\n    bottomHalfDrain(2000);")),
    ("shutdownFull without the settle", "gpuSuspend, systemWillShutdown, stop",
     lambda s: broken(s, "nvrm", "        s->irqSettle(2000);\n", "")),
    ("irqSettle that does not close the gate", "irqSettle closes the gate",
     lambda s: broken(s, "nvrm", "    nv_gate_close(&fIrqGate);\n    bool quiet", "    bool quiet")),
    ("irqSettle asking nvx_can_block", "irqSettle closes the gate",
     lambda s: broken(s, "nvrm", "    if (nv_quiesce_may_sleep()) {\n        const uint64_t t0", "    if (nv_quiesce_may_sleep() && nvx_can_block()) {\n        const uint64_t t0")),
    ("irqOpen that does not open", "irqOpen opens the gate",
     lambda s: broken(s, "nvrm", "    nv_gate_open(&fIrqGate);", "")),
    ("go() goes on without a slot", "go() claims the interrupt-thread slot",
     lambda s: broken(s, "nvrm", "return kIOReturnNoResources; }", "}")),
    ("stop() keeps the slot", "go() claims the interrupt-thread slot",
     lambda s: broken(s, "nvrm", "    nv_xnu_isr_slot_release(fIsrSlot, this); fIsrSlot = -1;\n", "")),
    ("go() making a second interrupt source over the first", "go(): a second try of pass 2",
     lambda s: broken(s, "nvrm", "        if (fIntSrc) {\n            LOG(\"pass 2 failed before", "        if (0) {\n            LOG(\"pass 2 failed before")),
    ("go() making a second work loop over the first", "go(): a second try of pass 2",
     lambda s: broken(s, "nvrm", "        if (!fWorkLoop) fWorkLoop = IOWorkLoop::workLoop();", "        fWorkLoop = IOWorkLoop::workLoop();")),
    ("stop() without rm_disable_adapter", "stop(): rm_disable_adapter",
     lambda s: broken(s, "nvrm", "rm_disable_adapter(NULL, &fNv);", ";")),
    ("stop() lets the timers go after the private state", "stop(): rm_disable_adapter",
     lambda s: broken(broken(s, "nvrm", "    nv_xnu_nano_retire(&fNv);\n", ""), "nvrm", "    nvrm_dma_detach(&fNv);", "    nv_xnu_nano_retire(&fNv);\n    nvrm_dma_detach(&fNv);")),
    ("os_is_isr on one global again", "os_is_isr() answers",
     lambda s: broken(s, "os", "if (nv_xnu_isr_threads[i] == me)", "if (nv_xnu_isr_thread == me)")),
    ("a CPU count of 4 again", "os_get_cpu_count() is real_ncpus",
     lambda s: broken(s, "os", "return n ? n : 1;", "return 4;")),
    ("the live CPU number", "os_get_cpu_count() is real_ncpus",
     lambda s: broken(s, "os", "os_get_cpu_number(void) { NV_XNU_TRC; return 0; }", "os_get_cpu_number(void) { NV_XNU_TRC; return cpu_number(); }")),
    ("destroy frees the slot", "nv_destroy_nano_timer frees nothing",
     lambda s: broken(s, "timers", "    if (idle && !quarantine) nv_xnu_nano_free(t);", "    if (idle && !quarantine) nv_xnu_nano_free(t); else IOFree(t, sizeof *t);")),
    ("a nano callback without the generation check", "a nano timer callback reaches",
     lambda s: broken(s, "timers", "    if (!nv_gen_enter(&t->gate, token)) { cnt(&gNanoStale); return; }", "    nv_gen_enter(&t->gate, nv_gen_current(&t->gate));")),
    ("a nano callback without the second check", "a nano timer callback reaches",
     lambda s: broken(s, "timers", "if (live && nv_gen_current(&t->gate) == token) {", "if (live) {")),
    ("an RC tick that re-arms with a new generation", "the RC tick reaches",
     lambda s: broken(s, "timers", "if (nv_gen_current(&rc->gate) == token) nv_xnu_rc_arm(rc, token);", "nv_xnu_rc_arm(rc, nv_gen_current(&rc->gate));")),
    ("the timer wait asking nvx_can_block", "the quiesce waits ask",
     lambda s: broken(s, "timers", "    if (nv_quiesce_may_sleep()) {\n        while", "    if (nvx_can_block()) {\n        while")),
    ("nv_quiesce_may_sleep built on nvx_can_block", "the quiesce waits ask",
     lambda s: broken(s, "quiesce", "return preemption_enabled() ? 1 : 0;", "return nvx_can_block() ? 1 : 0;")),
    ("the 32-bit microsecond deadline back", "a nano timer's deadline",
     lambda s: broken(s, "timers", "    nanoseconds_to_absolutetime(timens, &abs);", "    clock_interval_to_deadline((uint32_t)(timens / 1000ULL), kMicrosecondScale, &deadline);")),
    ("a writable timer sysctl", "the timers' sysctls",
     lambda s: broken(s, "timers", "nvrm_rc_armed, CTLFLAG_RD", "nvrm_rc_armed, CTLFLAG_RW")),
    ("a timer sysctl never registered", "the timers' sysctls",
     lambda s: broken(s, "timers", "sysctl_register_oid(&sysctl__debug_nvrm_nano_leaked);", "")),
    ("a bottom-half drain that waits without a thread", "every hook does nothing before its start",
     lambda s: broken(s, "nvrm", "    if (!fBhLock) return true;\n", "")),
    ("the gate closed from the start", "every hook does nothing before its start",
     lambda s: broken(s, "nvrm", "NvXnuGate    fIrqGate = {};", "NvXnuGate    fIrqGate = { 1, 0 };")),
    ("an RC suspend without a slot", "every hook does nothing before its start",
     lambda s: broken(s, "timers", "    if (!rc || !rc->call) return;\n    rc->paused = 1;", "    rc->paused = 1;")),
    ("a fence wake told without a suspend", "every hook does nothing before its start",
     lambda s: broken(s, "nvrm", "if (OSCompareAndSwap(1, 0, &fFenceAsleep)) nvrm_fence_pm(ok ?", "if (1) nvrm_fence_pm(ok ?")),
    ("the build without the timers", "build_nvrm.sh compiles",
     lambda s: broken(s, "build", "nv_xnu_kcore.cpp nv_xnu_timers.cpp", "nv_xnu_kcore.cpp")),
]
GLOBAL_CONTROLS = [
    ("the single global back in a source", "os_is_isr() answers",
     lambda o: dict(o, extra="extern volatile thread_t nv_xnu_isr_thread;")),
    ("thread_call_cancel_wait named in a source", "no kext source names thread_call_cancel_wait",
     lambda o: dict(o, extra="thread_call_cancel_wait(c);")),
]


def main():
    raw = {k: (root / v).read_text() for k, v in FILES.items()}
    src = {k: (v if k == "build" else no_comments(v)) for k, v in raw.items()}
    others = {k: no_comments(v) for k, v in kext_sources().items()}
    fails = 0
    for name, ok in arms(src, others):
        print(("ok   " if ok else "FAIL ") + name)
        fails += 0 if ok else 1
    for name, prefix, mutate in CONTROLS:
        got = dict(arms(mutate(src), others))
        hit = [a for a, ok in got.items() if a.startswith(prefix) and not ok]
        print(("ok   " if hit else "FAIL ") + "control: " + name + ("" if hit else " - not caught"))
        fails += 0 if hit else 1
    for name, prefix, mutate in GLOBAL_CONTROLS:
        got = dict(arms(src, mutate(others)))
        hit = [a for a, ok in got.items() if a.startswith(prefix) and not ok]
        print(("ok   " if hit else "FAIL ") + "control: " + name + ("" if hit else " - not caught"))
        fails += 0 if hit else 1
    print(f"{fails} failed" if fails else "all passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
