#!/usr/bin/env python3
"""The order of the steps in NVRM's start, stop, shutdown, sleep, wake and client-close paths (kexts/NVRM/NVRM.cpp).

Several changes each add a step to these paths: a gate that keeps the interrupt handler out of the RM while the GPU
is down (irqOpen / irqSettle), work queues with a bounded flush, rm_disable_adapter before rm_shutdown_adapter, the
surface fence (start / stop / pm / owner_gone), the RC timer and the nano timers that must be let go, the DMA layer,
the drain of event posts at client close, and the "start is done" mark the other kexts wait for. Each step is right
in one place only, relative to the others. What a wrong place costs, for example: the interrupt source enabled before
the gate is open drops every interrupt, and the GPU never finishes its start; a flush with a long bound inside the
8 s the system allows a shutdown leaves the GPU half shut down for the next boot; the fence told about a dead process
only after the RM has freed its channels makes WindowServer wait out a timeout for every surface that process drew.

None of this can be run without the card, so the order is asserted on the source text. A step is looked for as the
call it is, not as a line of text, and a call to another function of NVRM.cpp counts as that function's steps in that
place, with its arguments put in, so the code may be laid out with helpers or without. Each rule says in words what
must hold and, when it does not, which step is missing or out of place.

The arms at the end run the same rules on a made-up source that follows them (written out in full, and again with
helper functions) and on that source with one step moved, removed or changed at a time: each such change must fail
the rule it breaks and no other.

Until the reworked paths are in NVRM.cpp the rules fail, and the first and the last line of the output say that this
is why. Not asserted here: that each of these steps does nothing when its own start has not run (a sleep or a stop on
a half-started adapter). That is a property of the steps' own code, for the tests that run that code on the host.
Usage: test_lifecycle_order.py [driver checkout]"""
import pathlib
import re
import sys

# The steps the reworked paths add. While NVRM.cpp has none of them, the rework is not in the tree.
NEW_STEPS = ["irqOpen", "irqSettle", "workQueueDown", "nv_xnu_wq_flush", "nv_xnu_wq_gpu_start", "rm_disable_adapter",
             "nvrm_fence_start", "nvrm_fence_stop", "nvrm_fence_pm", "nvrm_fence_owner_gone", "evtDrainPosts", "fStartDone"]

LEX = re.compile(r"""//[^\n]*|/\*.*?\*/|/\*.*\Z|"(?:\\.|[^"\\\n])*"|'(?:\\.[^'\n]{0,8}|[^'\\\n])'""", re.S)
KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "sizeof", "do", "else", "defined", "decltype", "alignof",
            "static_assert", "__attribute__", "new", "delete", "case", "typeof"}
DEFINITION = re.compile(r"(?<![\w~.>])((?:[A-Za-z_]\w*::)*[A-Za-z_]\w*)\s*\(([^(){};]*)\)\s*(?:const\s*)?\{")
CLASS_BODY = re.compile(r"\b(?:class|struct)\s+([A-Za-z_]\w*)\b[^;{()]*\{")
NAME_CALL = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
NOTHING = ("NULL", "nullptr", "0")


def blank(s):
    return re.sub(r"[^\n]", " ", s)


def code_only(text):
    """The text with its comments blanked, and the inside of strings and character constants blanked. Same length."""
    return LEX.sub(lambda m: m.group(0)[0] + blank(m.group(0)[1:-1]) + m.group(0)[-1] if m.group(0)[0] in "\"'" else blank(m.group(0)), text)


def closing(text, at):
    """The offset of the bracket that closes the one at offset at, or None."""
    depth = 0
    for i in range(at, len(text)):
        if text[i] in "([{":
            depth += 1
        elif text[i] in ")]}":
            depth -= 1
            if depth == 0:
                return i
    return None


def arguments(text, open_paren):
    """The arguments of the call whose '(' is at open_paren ([] for f(), and when the brackets do not close)."""
    end = closing(text, open_paren)
    if end is None:
        return []
    out, depth, start = [], 0, open_paren + 1
    for i in range(open_paren, end + 1):
        c = text[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        if (c == "," and depth == 1) or i == end:
            out.append(" ".join(text[start:i].split()))
            start = i + 1
    return [] if out == [""] else out


def receiver(body, at):
    """What the call whose name starts at offset at is made on: ('', None) for f(), ('::', 'Class') for Class::f(),
    ('->', 'obj') for obj->f() and obj.f(), None for anything longer (a->b->f(), g()->f(), A::B::f(), ::f())."""
    lead = body[max(0, at - 200):at].rstrip()
    how = next((op for op in ("::", "->", ".") if lead.endswith(op)), "")
    if not how:
        return "", None
    on = re.search(r"(?<![\w>.:)\]])([A-Za-z_]\w*)$", lead[:-len(how)].rstrip())
    return ("::" if how == "::" else "->", on.group(1)) if on else None


class Source:
    """One C++ file: its functions, and each function as the steps it runs."""

    def __init__(self, text):
        self.code = code_only(text)
        self.functions = {}     # name -> (parameter names, the text between its braces, its parameter list as written)
        self.expanded = {}
        classes = [(m.end(), closing(self.code, m.end() - 1), m.group(1)) for m in CLASS_BODY.finditer(self.code)]
        taken = -1
        for m in DEFINITION.finditer(self.code):
            line = self.code[self.code.rfind("\n", 0, m.start()) + 1:m.start()]
            end = closing(self.code, m.end() - 1)
            # not a definition: a block inside a function already taken (an if, a loop, a macro that opens one), a
            # keyword, a preprocessor line, braces that do not close
            if m.start() < taken or m.group(1).split("::")[-1] in KEYWORDS or line.lstrip().startswith("#") or end is None:
                continue
            taken = end
            # a function written inside its class's braces is that class's
            owner = next((c for a, b, c in classes if b is not None and a <= m.start() < b), None)
            name = m.group(1) if "::" in m.group(1) or not owner else f"{owner}::{m.group(1)}"
            words = [re.findall(r"[A-Za-z_]\w*", p.split("=")[0]) for p in m.group(2).split(",")]
            params = [w[-1] if len(w) > 1 else None for w in words]     # "void", and a type with no name: None
            self.functions.setdefault(name, ([] if params == [None] else params, self.code[m.end():end], m.group(2)))

    def callee(self, caller, m):
        """The function of this file that a call is to, or None. f() in a member function is the same class's f, or
        else a plain function. this->f(), and obj->f() where the caller declares obj as a pointer or reference to a
        class, are that class's f. Class::f() is that one. Everything else is somebody else's and is not followed:
        fIntSrc->disable(), a->b->f(), super::stop()."""
        _, body, written = self.functions[caller]
        owner = caller.rsplit("::", 1)[0] if "::" in caller else ""
        name, on = m.group(1), receiver(body, m.start())
        if on is None:
            return None
        how, what = on
        if how == "":
            wanted = ([f"{owner}::{name}"] if owner else []) + [name]
        elif how == "::":
            wanted = [f"{what}::{name}"]
        elif what == "this":
            wanted = [f"{owner}::{name}"]
        else:
            kind = re.search(r"\b([A-Za-z_]\w*)\s*[*&]\s*(?:const\s+)?" + re.escape(what) + r"\b", written + " ; " + body)
            wanted = [f"{kind.group(1)}::{name}"] if kind else []
        return next((w for w in wanted if w in self.functions), None)

    def steps(self, name, inside=()):
        """The body of a function, with every call to another function of this file followed by that function's own
        steps in braces, its parameters replaced by the arguments of the call."""
        if name not in self.functions:
            return None
        if not inside and name in self.expanded:
            return self.expanded[name]
        body = self.functions[name][1]
        out, done = [], 0
        for m in NAME_CALL.finditer(body):
            if m.start() < done or m.group(1) in KEYWORDS:
                continue
            callee, end = self.callee(name, m), closing(body, m.end() - 1)
            if not callee or callee == name or callee in inside or len(inside) >= 4 or end is None:
                continue
            inner, params, args = self.steps(callee, inside + (name,)), self.functions[callee][0], arguments(body, m.end() - 1)
            put = {p: a for p, a in zip(params, args) if p} if len(params) == len(args) else {}
            if put:
                inner = re.sub(r"\b(" + "|".join(map(re.escape, put)) + r")\b", lambda k: put[k.group(1)], inner)
            out += [body[done:end + 1], " { ", inner, " } "]
            done = end + 1
        out = "".join(out) + body[done:]
        if not inside:
            self.expanded[name] = out
        return out

    def number(self, expr, scope="", depth=0):
        """An argument as a number: a literal, a product of two, `true ? a : b`, or a name given exactly one value in
        the steps being read (scope) or else in this file. None when it cannot be read that way."""
        e = expr.strip()
        while e.startswith("(") and closing(e, 0) == len(e) - 1:
            e = e[1:-1].strip()
        if re.fullmatch(r"\d+[uUlL]*", e):
            return int(re.match(r"\d+", e).group(0))
        product = re.fullmatch(r"(\d+)[uUlL]*\s*\*\s*(\d+)[uUlL]*", e)
        if product:
            return int(product.group(1)) * int(product.group(2))
        pick = re.fullmatch(r"(true|false)\s*\?\s*([^?:]+?)\s*:\s*([^?:]+)", e)
        if pick and depth < 4:
            return self.number(pick.group(2) if pick.group(1) == "true" else pick.group(3), scope, depth + 1)
        if re.fullmatch(r"[A-Za-z_]\w*", e) and depth < 4:
            for where in (scope, self.code):
                given = {" ".join(v.split()) for v in re.findall(r"\b" + e + r"\s*=(?!=)\s*([^;,{}]+?)\s*[;,}]", where)}
                given |= {" ".join(v.split()) for v in re.findall(r"^[ \t]*#[ \t]*define[ \t]+" + e + r"[ \t]+(\S[^\n]*?)[ \t]*$", where, re.M)}
                if given:
                    return self.number(given.pop(), scope, depth + 1) if len(given) == 1 else None
        return None


# ---- steps ----------------------------------------------------------------------------------------------------------
class Step:
    """Something to find in a function: a call (optionally only with certain arguments) or a piece of text. A step
    that is not needed may be absent; where it is present it has to be in its place."""

    def __init__(self, said, pattern, when=None, needed=True):
        self.said, self.pattern, self.when, self.needed = said, re.compile(pattern), when, needed

    def found(self, text):
        """-> [(offset, arguments)] for every place the step is at."""
        out = []
        for m in self.pattern.finditer(text):
            args = arguments(text, m.end() - 1) if text[m.end() - 1:m.end()] == "(" else []
            if self.when is None or self.when(args):
                out.append((m.start(), args))
        return out


def call(name, said=None, when=None, needed=True):
    return Step(said or name + "()", r"(?<![\w])" + re.escape(name) + r"\s*\(", when, needed)


def text(pattern, said):
    return Step(said, pattern)


OWN_FLUSH = call("nv_xnu_wq_flush", "the flush of the GPU's own work queue (nv_xnu_wq_flush(fNv.queue, true, bound))",
                 lambda a: len(a) == 3 and a[0] not in NOTHING)
GLOBAL_FLUSH = call("nv_xnu_wq_flush", "the flush of the global work queue (nv_xnu_wq_flush(NULL, false, bound))",
                    lambda a: len(a) == 3 and a[0] in NOTHING)
ANY_FLUSH = call("nv_xnu_wq_flush")
SETTLE = call("irqSettle")
IRQ_OFF = text(r"fIntSrc\s*->\s*disable\s*\(", "fIntSrc->disable()")
IRQ_ON = text(r"fIntSrc\s*->\s*enable\s*\(", "fIntSrc->enable()")
STANDBY = call("rm_power_management", "rm_power_management(STANDBY)", lambda a: any("NV_PM_ACTION_STANDBY" in x for x in a))
RESUME = call("rm_power_management", "rm_power_management(RESUME)", lambda a: any("NV_PM_ACTION_RESUME" in x for x in a))
FENCE_SLEEPS = call("nvrm_fence_pm", "nvrm_fence_pm(1), which tells the fence the GPU is going to sleep",
                    lambda a: a in (["1"], ["NVRM_FENCE_PM_SUSPENDING"]))
FENCE_WAKES = call("nvrm_fence_pm", "nvrm_fence_pm(ok ? 0 : 2), which tells the fence whether the GPU came back",
                   lambda a: len(a) == 1 and re.fullmatch(r"ok\s*\?\s*(0|NVRM_FENCE_PM_RESUMED)\s*:\s*(2|NVRM_FENCE_PM_LOST)", a[0]) is not None)
START_DONE = text(r"\bfStartDone\s*=(?!=)|(?<!&)&\s*fStartDone\b", "the write of fStartDone")
ADAPTER_DOWN = [OWN_FLUSH, call("rm_disable_adapter"), call("workQueueDown"), IRQ_OFF, SETTLE, call("rm_shutdown_adapter"),
                call("nv_xnu_rc_detach"), call("nv_xnu_nano_retire")]
RESUME_LINES = ("if (ok) nvrm_power_resume();", "if (ok) nv_xnu_rc_resume(&fNv);")


def in_order(src, function, steps):
    """'' when each step is in the function, its first place after the first place of the step before it (a step that
    is not needed and not there is passed over). Otherwise what is missing or out of place."""
    body = src.steps(function)
    if body is None:
        return f"{function} is not in the file"
    wrong, before = [], None
    for s in steps:
        at = min((p for p, _ in s.found(body)), default=None)
        if at is None:
            if s.needed:
                wrong.append(f"{s.said} is not there")
            continue
        if before is not None and at <= before[0]:
            wrong.append(f"{s.said} comes before {before[1]}, and has to come after it")
        before = (at, s.said)
    return "; ".join(wrong[:4]) + (f"; and {len(wrong) - 4} more" if len(wrong) > 4 else "")


def bounded(src, function, step, arg, most, least=1):
    """'' when the step is in the function and every such call waits between least and most milliseconds (0 would mean
    no bound at all). Otherwise which call does not."""
    body = src.steps(function)
    if body is None:
        return f"{function} is not in the file"
    places = step.found(body)
    if not places:
        return f"{step.said} is not there"
    wrong = []
    for _, args in places:
        shown = args[arg] if arg < len(args) else ""
        n = src.number(shown, body) if shown else None
        if n is None:
            wrong.append(f"{step.said}: its wait '{shown}' is not a number that can be read here")
        elif not least <= n <= most:
            wrong.append(f"{step.said} waits {n} ms" + (", which is no bound at all" if n == 0 else "")
                         + "; it has to wait " + (f"{most} ms" if least == most else f"at most {most} ms"))
    return "; ".join(dict.fromkeys(wrong))


def both(*whys):
    return "; ".join(w for w in whys if w)


def stop_starts_right(src):
    if not re.search(r"void NVRM::stop\(IOService \*provider\)\s*\{\s*nvrm_power_stop\(\);", src.code):
        return "nvrm_power_stop(); is not the first statement of NVRM::stop(IOService *provider)"
    return in_order(src, "NVRM::stop", [call("nvrm_power_stop"), call("nvrm_fence_stop"),
                                        Step("the teardown of the utilization state (fUtilizationLock)", r"\bfUtilizationLock\b", needed=False),
                                        call("PMstop")])


def resume_lines(src):
    body = src.functions.get("NVRM::gpuResume", (None, None, None))[1]
    if body is None:
        return "NVRM::gpuResume is not in the file"
    at = [body.find(line) for line in RESUME_LINES]
    if min(at) < 0:
        return "gpuResume has no line " + " and no line ".join(f"'{line}'" for line, a in zip(RESUME_LINES, at) if a < 0)
    return "" if at[0] < at[1] else "the RC timer's line comes before the power manager's"


def start_done_last(src):
    body = src.steps("NVRM::go")
    if body is None:
        return "NVRM::go is not in the file"
    writes = START_DONE.found(body)
    if not writes:
        return "go() does not write fStartDone"
    end = body.find(";", writes[-1][0])
    rest = re.findall(r"[A-Za-z_]\w*", body[end + 1:]) if end >= 0 else ["(the statement does not end)"]
    return "" if rest == ["return", "kIOReturnSuccess"] else "after the write of fStartDone go() still runs: " + " ".join(rest[:8])


def enable_only_with_open_gate(src):
    wrong, seen = [], 0
    for name, (_, own, _) in src.functions.items():
        if not IRQ_ON.found(own):
            continue
        body = src.steps(name)
        for at, _ in IRQ_ON.found(body):
            seen += 1
            gate = re.findall(r"(?<![\w])(irqOpen|irqSettle)\s*\(", body[:at])
            if not gate:
                wrong.append(f"{name} enables the interrupt source with no irqOpen() before it")
            elif gate[-1] != "irqOpen":
                wrong.append(f"{name} enables the interrupt source after irqSettle() closed the gate, with no irqOpen() in between")
    return "; ".join(dict.fromkeys(wrong)) if seen else "no fIntSrc->enable() was found in the file"


# ---- the rules ------------------------------------------------------------------------------------------------------
RULES = [
    ("stop(): nvrm_power_stop() is the first statement and the fence is stopped right after it (whoever sleeps in it "
     "is woken before anything is torn down), before the utilization state goes and the driver leaves power management",
     stop_starts_right),
    ("stop(): once the driver has left power management the adapter goes down in one order: the GPU's own work queue "
     "flushed with a bound, rm_disable_adapter, the queue taken away, the interrupt off and the gate settled, "
     "rm_shutdown_adapter, then the RC timer and the nano timers let go",
     lambda s: in_order(s, "NVRM::stop", [call("PMstop")] + ADAPTER_DOWN)),
    ("stop(): after the adapter, the event source is removed, the bottom-half thread stopped, the interrupt-thread slot "
     "released and the global work queue flushed; only then is the RM's private state freed (an item still running "
     "would use it), and the DMA layer is detached after that",
     lambda s: in_order(s, "NVRM::stop", [call("rm_shutdown_adapter"), call("removeEventSource"), call("bottomHalfStop"),
                                          call("nv_xnu_isr_slot_release"), GLOBAL_FLUSH, call("rm_free_private_state"),
                                          call("nvrm_dma_detach")])),
    ("stop(): every work-queue flush is bounded, at 30 s (nothing else bounds an unload, and a shorter wait would give "
     "up on a queue that is only slow)",
     lambda s: bounded(s, "NVRM::stop", ANY_FLUSH, 2, 30000, 30000)),
    ("shutdownFull(): the fence is stopped first, before the RM gate closes and the user channels stop (and after the "
     "power manager, if that is stopped here too); then the adapter goes down in the same order as in stop()",
     lambda s: in_order(s, "NVRM::shutdownFull", [call("nvrm_power_stop", needed=False), call("nvrm_fence_stop"), call("nv_xnu_pm_close"),
                                                  call("rm_stop_user_channels")] + ADAPTER_DOWN)),
    ("shutdownFull(): each flush and the settle wait at most 2 s (the system gives the whole shutdown 8 s, and an "
     "adapter left half shut down can fail the next boot)",
     lambda s: both(bounded(s, "NVRM::shutdownFull", ANY_FLUSH, 2, 2000), bounded(s, "NVRM::shutdownFull", SETTLE, 0, 2000))),
    ("systemWillShutdown(): when its wait for the shutdown thread is over, the interrupt is turned off and the gate "
     "settled (not only the bottom half drained), before the BAR layout is put back",
     lambda s: in_order(s, "NVRM::systemWillShutdown", [call("kernel_thread_start"), IRQ_OFF, SETTLE, call("nvrmBuildShutdownPlan")])),
    ("gpuSuspend(): the power manager, the fence and the RC timer are told first; the RM gate is closed and the user "
     "channels stopped; the global work queue is flushed; then RM standby; and only then the interrupt off and the "
     "gate settled",
     lambda s: in_order(s, "NVRM::gpuSuspend", [call("nvrm_power_suspend"), FENCE_SLEEPS, call("nv_xnu_rc_suspend"), call("nv_xnu_pm_close"),
                                                call("rm_stop_user_channels"), GLOBAL_FLUSH, STANDBY, IRQ_OFF, SETTLE])),
    ("gpuSuspend(): the flush waits 5 s and the settle at most 2 s (all of it runs inside the system's power change, "
     "behind a 10 s wait already; the unload's 30 s here would trip the sleep watchdog)",
     lambda s: both(bounded(s, "NVRM::gpuSuspend", ANY_FLUSH, 2, 5000, 5000), bounded(s, "NVRM::gpuSuspend", SETTLE, 0, 2000))),
    ("gpuResume(): the gate is opened, then the interrupt enabled, then RM resume; after that the power manager and the "
     "RC timer are resumed, and after them the fence is told whether the GPU came back",
     lambda s: in_order(s, "NVRM::gpuResume", [call("irqOpen"), IRQ_ON, RESUME, call("nvrm_power_resume"), call("nv_xnu_rc_resume"), FENCE_WAKES])),
    ("gpuResume(): the power manager and the RC timer are each resumed by a statement of their own, "
     f"'{RESUME_LINES[0]}' and then '{RESUME_LINES[1]}' (tools/test_notebook_power_wiring.py reads the first as text)",
     resume_lines),
    ("clientClose(): one caller wins; the client leaves the table; the fence forgets the process BEFORE the RM frees its "
     "channels (who waits on its surfaces is released at once, not after a timeout); event posts are drained AFTER the "
     "RM cleanup; then the mappings are marked dead and the client terminated",
     lambda s: in_order(s, "NVRMUserClient::clientClose",
                        [text(r"OSCompareAndSwap\s*\(\s*0\s*,\s*1\s*,\s*&\s*fCleaned\s*\)", "the compare-and-swap on fCleaned"),
                         text(r"gNvClients\s*\[\s*fFd\s*\]\s*=\s*NULL", "gNvClients[fFd] = NULL"), call("nvrm_fence_owner_gone"),
                         call("rm_cleanup_file_private"), call("evtDrainPosts"),
                         text(r"context\s*\.\s*valid\s*=\s*NV_FALSE", "context.valid = NV_FALSE"), call("terminate")])),
    ("go(), second pass: the GPU's work queue first, then the event source on the work loop, the bottom-half thread, the "
     "gate opened, the interrupt enabled, rm_init_adapter, the power manager started, the fence started",
     lambda s: in_order(s, "NVRM::go", [call("nv_xnu_wq_gpu_start"), call("addEventSource"), call("bottomHalfStart"), call("irqOpen"), IRQ_ON,
                                        call("rm_init_adapter"), call("nvrm_power_start"), call("nvrm_fence_start")])),
    ("go(): fStartDone is written after everything else the start does, as the last statement before the successful "
     "return (the other kexts wait for it and then use what the start set up)",
     start_done_last),
    ("everywhere: the interrupt source is enabled only with the gate open, irqOpen() before every fIntSrc->enable() "
     "(an interrupt that arrives at a closed gate is dropped, and the GPU's start then times out)",
     enable_only_with_open_gate),
]


def broken(source_text):
    """The rules a source does not meet: [(rule, why)]."""
    src = Source(source_text)
    return [(said, why) for said, rule in RULES for why in [rule(src)] if why]


# ---- a made-up source that follows every rule, for the controls --------------------------------------------------------
SAMPLE = r'''
IOReturn NVRM::go(uint32_t pass)
{
    if (pass >= 2 && fPassDone < 2) {
        fNv.queue = nv_xnu_wq_gpu_start();   // NULL: this GPU uses the global queue
        fWorkLoop = IOWorkLoop::workLoop();
        fIntSrc = IOFilterInterruptEventSource::filterInterruptEventSource(this, a, b, fPCI, msi);
        if (!fWorkLoop || !fIntSrc || fWorkLoop->addEventSource(fIntSrc) != kIOReturnSuccess) { LOG("setup FAILED"); return kIOReturnError; }
        bottomHalfStart();
        irqOpen();
        fIntSrc->enable();
        NvBool ok = rm_init_adapter(NULL, nv);
        if (!ok) return kIOReturnError;
        fAdapter = true;
        enableAspmL1();
        nvrm_power_start(&fNv);
        nvrm_fence_start(); fFenceUp = true;
        fStartDone = 1;
    }
    return kIOReturnSuccess;
}
void NVRM::irqOpen() { gNvIsrFilterAt = 0; nv_gate_open(&fIrqGate); }
void NVRM::irqSettle(uint32_t waitMs) { nv_gate_close(&fIrqGate); bottomHalfDrain(waitMs); }
void NVRM::workQueueDown() { struct os_work_queue *q = fNv.queue; fNv.queue = NULL; if (q) nv_xnu_wq_gpu_down(q); }
void NVRM::stop(IOService *provider)
{
    nvrm_power_stop();
    if (fFenceUp) { nvrm_fence_stop(); fFenceUp = false; }
    if (fUtilizationLock) { IOLockLock(fUtilizationLock); fUtilizationStopping = true; IOLockUnlock(fUtilizationLock); }
    if (fPmJoined) { PMstop(); fPmJoined = false; }
    if (fAdapter) {
        nv_xnu_wq_flush(fNv.queue, true, 30000);
        rm_disable_adapter(NULL, &fNv);
        workQueueDown();
        if (fIntSrc) fIntSrc->disable();
        irqSettle(2000);
        rm_shutdown_adapter(NULL, &fNv); fAdapter = false;
        nv_xnu_rc_detach(&fNv); nv_xnu_nano_retire(&fNv);
    }
    if (fIntSrc) { fIntSrc->disable(); if (fWorkLoop) fWorkLoop->removeEventSource(fIntSrc); fIntSrc->release(); fIntSrc = NULL; }
    bottomHalfStop();
    nv_xnu_isr_slot_release(fIsrSlot, this);
    nv_xnu_wq_flush(NULL, false, 30000);
    if (fPrivateState) { rm_free_private_state(NULL, &fNv); fPrivateState = false; }
    nvrm_dma_detach(&fNv);
    super::stop(provider);
}
void NVRM::shutdownFull(void *p0, wait_result_t)
{
    NVRM *s = (NVRM *)p0;
    if (s->fFenceUp) { nvrm_fence_stop(); s->fFenceUp = false; }
    s->notifyFramebuffers(0);
    const int busy = nv_xnu_pm_close(3000);
    rm_stop_user_channels(NULL, &s->fNv);
    if (s->fAdapter) {
        nv_xnu_wq_flush(s->fNv.queue, true, 2000);
        rm_disable_adapter(NULL, &s->fNv);
        s->workQueueDown();
        if (s->fIntSrc) s->fIntSrc->disable();
        s->irqSettle(2000);
        rm_shutdown_adapter(NULL, &s->fNv); s->fAdapter = false;
        nv_xnu_rc_detach(&s->fNv); nv_xnu_nano_retire(&s->fNv);
    }
    OSCompareAndSwap(0, 1, &s->fShutdownDone);
}
void NVRM::systemWillShutdown(IOOptionBits specifier)
{
    if (kernel_thread_start(full ? &NVRM::shutdownFull : &NVRM::shutdownQuiesce, this, &th) == KERN_SUCCESS) { wait(); }
    if (fIntSrc) fIntSrc->disable();
    irqSettle(1000);
    const unsigned n = nvrmBuildShutdownPlan(&fBarUndo, plan, 16);
}
void NVRM::gpuSuspend()
{
    nvrm_power_suspend();
    nvrm_fence_pm(1);
    nv_xnu_rc_suspend(&fNv);
    const int busy = nv_xnu_pm_close(10000);
    NV_STATUS cst = rm_stop_user_channels(NULL, &fNv);
    nv_xnu_wq_flush(NULL, false, 5000);
    NV_STATUS st = rm_power_management(NULL, &fNv, NV_PM_ACTION_STANDBY);
    if (fIntSrc) fIntSrc->disable();
    irqSettle(2000);
}
void NVRM::gpuResume()
{
    bool ok = pciRestoreAfterWake() && gpuAnswers();
    irqOpen();
    if (fIntSrc) fIntSrc->enable();
    NV_STATUS st = ok ? rm_power_management(NULL, &fNv, NV_PM_ACTION_RESUME) : NV_ERR_GPU_IS_LOST;
    ok = st == NV_OK;
    nv_xnu_pm_open();
    if (ok) nvrm_power_resume();
    if (ok) nv_xnu_rc_resume(&fNv);
    nvrm_fence_pm(ok ? 0 : 2);
}
bool NVRMUserClient::evtDrainPosts() { return nv_event_posting_refcount_read(&fNvfp) == 0; }
IOReturn NVRMUserClient::clientClose(void)
{
    if (OSCompareAndSwap(0, 1, &fCleaned)) {
        Gate g(fGate);
        if (gNvClientsLock && fFd >= 0) { IOLockLock(gNvClientsLock); gNvClients[fFd] = NULL; nv_pm_close(fPm); IOLockUnlock(gNvClientsLock); }
        nvrm_fence_owner_gone(fFenceOwner);
        if (fOwner) { NvXnuPmUse pmUse; rm_cleanup_file_private(NULL, nvState(), &fNvfp); }
        (void)evtDrainPosts();
        IOLockLock(fMapLock);
        if (fMapList) fMapList->context.valid = NV_FALSE;
        IOLockUnlock(fMapLock);
    }
    terminate();
    return kIOReturnSuccess;
}
'''
STOP_BLOCK = ("    if (fAdapter) {\n        nv_xnu_wq_flush(fNv.queue, true, 30000);\n", "    if (fIntSrc) { fIntSrc->disable(); if (fWorkLoop)")
SHUTDOWN_BLOCK = ("    if (s->fAdapter) {\n", "    OSCompareAndSwap(0, 1, &s->fShutdownDone);")


def with_helpers(sample, in_class=False):
    """The same steps laid out differently. The adapter teardown is one function both callers use, and the two
    interrupt lines of a start are another. Written outside the class, the teardown takes its bound as a parameter and
    the callers pass a named constant or a number; written inside the class's braces, it takes a flag and works the
    bound out itself."""
    block = sample[sample.index(STOP_BLOCK[0]):sample.index(STOP_BLOCK[1])]
    block_s = sample[sample.index(SHUTDOWN_BLOCK[0]):sample.index(SHUTDOWN_BLOCK[1])]
    out = sample.replace(block, "    adapterDown(true);\n" if in_class else "    adapterDown(kStopFlushMs);\n")
    out = out.replace(block_s, "    s->adapterDown(false);\n" if in_class else "    s->adapterDown(2000);\n")
    out = out.replace("        irqOpen();\n        fIntSrc->enable();\n", "        interruptsOn();\n")
    out = out.replace("    irqOpen();\n    if (fIntSrc) fIntSrc->enable();\n", "    interruptsOn();\n")
    out = out.replace("nv_xnu_wq_flush(NULL, false, 5000);", "nv_xnu_wq_flush(NULL, false, kSleepFlushMs);")
    steps = block.replace("    if (fAdapter) {\n", "").replace("30000", "capMs").rsplit("    }\n", 1)[0]
    q = "" if in_class else "NVRM::"
    helpers = (f"void {q}interruptsOn() {{ irqOpen(); if (fIntSrc) fIntSrc->enable(); }}\n"
               f"void {q}adapterDown({'bool unload' if in_class else 'uint32_t capMs'})\n{{\n    if (!fAdapter) return;\n"
               + ("    const uint32_t capMs = unload ? kStopFlushMs : 2 * 1000;\n" if in_class else "") + steps + "}\n")
    if in_class:
        helpers = "class NVRM : public IOService {\n    IOFilterInterruptEventSource *fIntSrc = nullptr;\n" + helpers + "};\n"
    return "enum { kStopFlushMs = 30000, kSleepFlushMs = 5000 };\n" + helpers + out


def swapped(sample, first, second):
    """The sample with two of its pieces exchanged."""
    a, b = sample.index(first), sample.index(second)
    assert 0 <= a < b and sample.count(first) == 1 and sample.count(second) == 1, (first, second)
    return sample[:a] + second + sample[a + len(first):b] + first + sample[b + len(second):]


def changed(sample, old, new):
    """The sample with one piece replaced."""
    assert sample.count(old) == 1, (sample.count(old), old)
    return sample.replace(old, new)


# what each control changes, the numbers of the rules that have to fail for it, and the changed source
FENCE_STOP = "    if (fFenceUp) { nvrm_fence_stop(); fFenceUp = false; }\n"
FENCE_STOP_S = "    if (s->fFenceUp) { nvrm_fence_stop(); s->fFenceUp = false; }\n"
UTILIZATION = "    if (fUtilizationLock) { IOLockLock(fUtilizationLock); fUtilizationStopping = true; IOLockUnlock(fUtilizationLock); }\n"
PM_STOP = "    if (fPmJoined) { PMstop(); fPmJoined = false; }\n"
FREE_STATE = "    if (fPrivateState) { rm_free_private_state(NULL, &fNv); fPrivateState = false; }\n"
RM_CLEANUP = "        if (fOwner) { NvXnuPmUse pmUse; rm_cleanup_file_private(NULL, nvState(), &fNvfp); }\n"
SUSPEND_END = "    if (fIntSrc) fIntSrc->disable();\n    irqSettle(2000);\n}\nvoid NVRM::gpuResume()"
CONTROLS = [
    ("stop(): the fence stopped only after the utilization state went", [1], lambda m: swapped(m, FENCE_STOP, UTILIZATION)),
    ("stop() without the fence stop", [1], lambda m: changed(m, FENCE_STOP, "")),
    ("stop(): the adapter taken down before the driver left power management", [2],
     lambda m: changed(changed(m, PM_STOP, ""), "    bottomHalfStop();\n", PM_STOP + "    bottomHalfStop();\n")),
    ("stop(): something else done before the power manager is stopped", [1],
     lambda m: changed(m, "{\n    nvrm_power_stop();\n", "{\n    LOG(\"stop\");\n    nvrm_power_stop();\n")),
    ("stop() without rm_disable_adapter", [2], lambda m: changed(m, "        rm_disable_adapter(NULL, &fNv);\n", "")),
    ("stop(): the queue flushed after rm_disable_adapter instead of before", [2],
     lambda m: swapped(m, "        nv_xnu_wq_flush(fNv.queue, true, 30000);\n", "        rm_disable_adapter(NULL, &fNv);\n")),
    ("stop(): rm_shutdown_adapter with the interrupt still on", [2],
     lambda m: swapped(m, "        if (fIntSrc) fIntSrc->disable();\n        irqSettle(2000);\n", "        rm_shutdown_adapter(NULL, &fNv); fAdapter = false;\n")),
    ("stop(): the timers let go before the adapter is shut down", [2],
     lambda m: swapped(m, "        rm_shutdown_adapter(NULL, &fNv); fAdapter = false;\n", "        nv_xnu_rc_detach(&fNv); nv_xnu_nano_retire(&fNv);\n")),
    ("stop(): the global queue flushed after the private state is freed", [3],
     lambda m: swapped(m, "    nv_xnu_wq_flush(NULL, false, 30000);\n", FREE_STATE)),
    ("stop(): the DMA layer detached before the private state is freed", [3], lambda m: swapped(m, FREE_STATE, "    nvrm_dma_detach(&fNv);\n")),
    ("stop(): the interrupt-thread slot never released", [3], lambda m: changed(m, "    nv_xnu_isr_slot_release(fIsrSlot, this);\n", "")),
    ("stop(): a flush with no bound", [4], lambda m: changed(m, "nv_xnu_wq_flush(NULL, false, 30000);", "nv_xnu_wq_flush(NULL, false, 0);")),
    ("stop(): a queue given up on after 5 s", [4], lambda m: changed(m, "nv_xnu_wq_flush(fNv.queue, true, 30000);", "nv_xnu_wq_flush(fNv.queue, true, 5000);")),
    ("shutdownFull() without the fence stop", [5], lambda m: changed(m, FENCE_STOP_S, "")),
    ("shutdownFull(): the fence stopped after the user channels", [5], lambda m: swapped(m, FENCE_STOP_S, "    rm_stop_user_channels(NULL, &s->fNv);\n")),
    ("shutdownFull(): the power manager stopped after the fence", [5], lambda m: changed(m, FENCE_STOP_S, FENCE_STOP_S + "    nvrm_power_stop();\n")),
    ("shutdownFull(): the 30 s bound of stop()", [6],
     lambda m: changed(m, "nv_xnu_wq_flush(s->fNv.queue, true, 2000);", "nv_xnu_wq_flush(s->fNv.queue, true, 30000);")),
    ("shutdownFull(): a 5 s settle", [6], lambda m: changed(m, "s->irqSettle(2000);", "s->irqSettle(5000);")),
    ("systemWillShutdown() only drains the bottom half", [7], lambda m: changed(m, "    irqSettle(1000);\n", "    bottomHalfDrain(1000);\n")),
    ("gpuSuspend(): the fence is not told", [8], lambda m: changed(m, "    nvrm_fence_pm(1);\n", "")),
    ("gpuSuspend(): the fence told the GPU is back", [8], lambda m: changed(m, "    nvrm_fence_pm(1);\n", "    nvrm_fence_pm(0);\n")),
    ("gpuSuspend(): the gate settled before the interrupt is off", [8],
     lambda m: changed(m, SUSPEND_END, "    irqSettle(2000);\n    if (fIntSrc) fIntSrc->disable();\n}\nvoid NVRM::gpuResume()")),
    ("gpuSuspend(): the flush after RM standby", [8],
     lambda m: swapped(m, "    nv_xnu_wq_flush(NULL, false, 5000);\n", "    NV_STATUS st = rm_power_management(NULL, &fNv, NV_PM_ACTION_STANDBY);\n")),
    ("gpuSuspend(): the 30 s flush bound of an unload", [9], lambda m: changed(m, "nv_xnu_wq_flush(NULL, false, 5000);", "nv_xnu_wq_flush(NULL, false, 30000);")),
    ("gpuSuspend(): a bound that is not a number", [9], lambda m: changed(m, "nv_xnu_wq_flush(NULL, false, 5000);", "nv_xnu_wq_flush(NULL, false, flushMs());")),
    ("gpuResume(): the interrupt enabled with the gate still closed", [10, 15],
     lambda m: changed(m, "    irqOpen();\n    if (fIntSrc) fIntSrc->enable();\n", "    if (fIntSrc) fIntSrc->enable();\n")),
    ("gpuResume(): the fence told the GPU is back whether or not it is", [10], lambda m: changed(m, "nvrm_fence_pm(ok ? 0 : 2);", "nvrm_fence_pm(0);")),
    ("gpuResume(): the fence told before the RC timer is resumed", [10],
     lambda m: swapped(m, "    if (ok) nv_xnu_rc_resume(&fNv);\n", "    nvrm_fence_pm(ok ? 0 : 2);\n")),
    ("gpuResume(): the two resumes in one statement", [11],
     lambda m: changed(m, "    if (ok) nvrm_power_resume();\n    if (ok) nv_xnu_rc_resume(&fNv);\n", "    if (ok) { nvrm_power_resume(); nv_xnu_rc_resume(&fNv); }\n")),
    ("clientClose(): the fence told after the RM cleanup", [12], lambda m: swapped(m, "        nvrm_fence_owner_gone(fFenceOwner);\n", RM_CLEANUP)),
    ("clientClose(): event posts drained before the RM cleanup", [12], lambda m: swapped(m, RM_CLEANUP, "        (void)evtDrainPosts();\n")),
    ("clientClose(): the fence told only by a method of another object, which is not followed", [12],
     lambda m: changed(m, "        nvrm_fence_owner_gone(fFenceOwner);\n", "        fPeer->forget();\n")
     + "void NVRMUserClient::forget() { nvrm_fence_owner_gone(fFenceOwner); }\n"),
    ("go(): the interrupt enabled before the gate is open", [13, 15], lambda m: swapped(m, "        irqOpen();\n", "        fIntSrc->enable();\n")),
    ("go(): the GPU's work queue made after the interrupt is on", [13],
     lambda m: changed(changed(m, "        fNv.queue = nv_xnu_wq_gpu_start();   // NULL: this GPU uses the global queue\n", ""),
                       "        fIntSrc->enable();\n", "        fIntSrc->enable();\n        fNv.queue = nv_xnu_wq_gpu_start();\n")),
    ("go(): the fence started before the adapter is up", [13],
     lambda m: changed(changed(m, "        nvrm_fence_start(); fFenceUp = true;\n", ""), "        bottomHalfStart();\n",
                       "        bottomHalfStart();\n        nvrm_fence_start(); fFenceUp = true;\n")),
    ("go(): the start marked done before the fence is started", [14],
     lambda m: swapped(m, "        nvrm_fence_start(); fFenceUp = true;\n", "        fStartDone = 1;\n")),
    ("go(): work after the start is marked done", [14], lambda m: changed(m, "        fStartDone = 1;\n", "        fStartDone = 1;\n        publishGpuProfile();\n")),
    ("go(): the start never marked done", [14], lambda m: changed(m, "        fStartDone = 1;\n", "")),
    ("an interrupt enable in one more function, with no gate opened", [15], lambda m: m + "void NVRM::rearm() { if (fIntSrc) fIntSrc->enable(); }\n"),
    ("an interrupt enable after the gate was closed again", [15],
     lambda m: m + "void NVRM::rearm() { irqOpen(); irqSettle(100); if (fIntSrc) fIntSrc->enable(); }\n"),
]


def main():
    root = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1]
    fails = []

    def arm(name, ok, detail=""):
        print(("ok   " if ok else "FAIL ") + name + (f"\n       {detail}" if detail and not ok else ""))
        if not ok:
            fails.append(name)

    def rules_hit(source_text):
        order = [said for said, _ in RULES]
        return [order.index(said) + 1 for said, _ in broken(source_text)]

    nvrm = (root / "kexts/NVRM/NVRM.cpp").read_text(errors="replace")
    found = broken(nvrm)
    code = code_only(nvrm)
    present = [s for s in NEW_STEPS if re.search(r"\b" + s + r"\b", code)]
    if not found:
        banner = f"All {len(RULES)} rules of the start, stop, shutdown, sleep, wake and client-close order hold in kexts/NVRM/NVRM.cpp."
    elif len(present) < len(NEW_STEPS):
        banner = ("EXPECTED TO FAIL in this tree: the reworked start, stop, shutdown, sleep, wake and client-close paths are not "
                  f"{'in' if not present else 'all in'} kexts/NVRM/NVRM.cpp yet ({len(present)} of the {len(NEW_STEPS)} steps they add are there). "
                  f"{len(found)} of {len(RULES)} rules fail. This is a failure, not a skip; the test passes once that code is merged.")
    else:
        banner = (f"{len(found)} of {len(RULES)} rules FAIL although every step the reworked paths add is in kexts/NVRM/NVRM.cpp: "
                  "a step is in the wrong place, or has the wrong bound.")
    print(banner)
    why = dict(found)
    for n, (said, _) in enumerate(RULES, 1):
        arm(f"{n:2}. {said}", said not in why, why.get(said, ""))

    # controls: the rules can be met, in each layout, and each is broken by the change that should break it
    layouts = [("written out in full", SAMPLE), ("with helper functions that take the bound", with_helpers(SAMPLE)),
               ("with helper functions written inside the class", with_helpers(SAMPLE, in_class=True))]
    for how, source_text in layouts:
        arm(f"control: a made-up source that follows the order, {how}, meets every rule", rules_hit(source_text) == [], broken(source_text))
    arm("control: so does one whose shutdownFull() stops the power manager first, as stop() does",
        rules_hit(changed(SAMPLE, FENCE_STOP_S, "    nvrm_power_stop();\n" + FENCE_STOP_S)) == [])
    for what, want, change in CONTROLS:
        got = rules_hit(change(SAMPLE))
        arm(f"control: {what}: fails rule{'s' if len(want) > 1 else ''} {' and '.join(map(str, want))} and no other", got == want, f"failed: {got or 'none'}")
    for how, flag, plain in (("that takes the bound", "    s->adapterDown(2000);\n", "    s->adapterDown(kStopFlushMs);\n"),
                             ("written inside the class", "    s->adapterDown(false);\n", "    s->adapterDown(true);\n")):
        got = rules_hit(changed(with_helpers(SAMPLE, in_class="inside" in how), flag, plain))
        arm(f"control: shutdownFull() with the 30 s bound of stop(), reached through the shared helper {how}: fails rule 6 and no other", got == [6], f"failed: {got or 'none'}")
    covered = sorted({r for _, want, _ in CONTROLS for r in want})
    arm("control: every rule is broken by at least one of the changes above", covered == list(range(1, len(RULES) + 1)), covered)
    print(f"\n{len(fails)} failed" if fails else "\nPASS")
    if found:
        print(banner)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
