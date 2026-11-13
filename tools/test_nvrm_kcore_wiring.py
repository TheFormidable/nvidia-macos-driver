#!/usr/bin/env python3
"""The serialized work queues, the answers by context and the stub bodies (kexts/NVRM/nv_xnu_kcore.cpp) are wired the
way they have to be, read from the source text.

The pure logic is tested on the host (test_nvrm_workq_logic.c, test_nvrm_atomic_pool.c, test_nvrm_itree.c). What can
go wrong around it is the wiring: a GPU start that fails because it got no queue of its own, an adapter shut down while
its items still run, a flush that a teardown waits on for ever, a log line or an allocation under a lock taken with
interrupts off, a stub left in front of the real function, a switch that is not on by default, a check of the GPUs'
memory windows that sees only the first card. Each arm below reads the
files as they are; the controls at the end change one thing in a copy and require the arm to fail.
Usage: test_nvrm_kcore_wiring.py [driver checkout]"""
import pathlib
import re
import sys

ROOT = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1]
FILES = {k: "kexts/NVRM/" + k for k in ("nv_xnu_kcore.cpp", "os-xnu.cpp", "os-xnu2.cpp", "NVRM.cpp", "nv_xnu_stubs.cpp",
                                        "nv-xnu.h", "nv_xnu_quiesce.h")}
FILES["build_nvrm.sh"] = "build/build_nvrm.sh"
FAILS = []


def strip_comments(s):
    s = re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group(0)), s, flags=re.S)
    return re.sub(r"//[^\n]*", "", s)


def body(text, head):
    """The body of the function whose definition contains `head`, from its opening brace to the matching one."""
    i = text.find(head)
    if i < 0:
        return None
    j = text.find("{", i)
    depth = 0
    for k in range(j, len(text)):
        if text[k] == "{":
            depth += 1
        elif text[k] == "}":
            depth -= 1
            if depth == 0:
                return text[j:k + 1]
    return None


def locked_regions(text, take, drop):
    out, at = [], 0
    while True:
        a = text.find(take, at)
        if a < 0:
            return out
        b = text.find(drop, a)
        if b < 0:
            return out + [text[a:]]
        out.append(text[a:b])
        at = b


UNDER_SPIN = ("IOLog", "KLOG", "kprintf", "kern_os_malloc", "kern_os_free", "IOLockLock", "rm_execute_work_item",
              "IOSleep", "thread_deallocate", "kernel_thread_start", "nvx_note")


def problems(src):
    """[why] for every rule the sources do not meet. `src` maps a key of FILES to the file's text."""
    out = []
    k = strip_comments(src["nv_xnu_kcore.cpp"])
    osx = strip_comments(src["os-xnu.cpp"])
    osx2 = strip_comments(src["os-xnu2.cpp"])
    nvrm = strip_comments(src["NVRM.cpp"])
    stubs = strip_comments(src["nv_xnu_stubs.cpp"])

    if not re.search(r"for s in [^\n]*\bnv_xnu_kcore\.cpp\b", src["build_nvrm.sh"]):
        out.append("build_nvrm.sh does not compile nv_xnu_kcore.cpp")
    # the work queue lives in one place
    for fn in ("os_queue_work_item", "os_flush_work_queue", "os_is_queue_flush_ongoing"):
        if re.search(r"\b%s\s*\([^;]*\)\s*\{" % fn, osx2):
            out.append("os-xnu2.cpp still defines " + fn)
        if not re.search(r"NV_API_CALL %s\s*\([^;]*\)\s*\{" % fn, k):
            out.append("nv_xnu_kcore.cpp does not define " + fn)
    if "kernel_thread_start(nv_xnu_work_thread" in osx2:
        out.append("os-xnu2.cpp still starts a thread per work item")
    # the context question
    if not re.search(r"bool nvx_can_block\(void\)\s*\{\s*return !nvx_atomic_on \|\| preemption_enabled\(\);\s*\}", k):
        out.append("nvx_can_block is not the kernel's answer behind the switch")
    q = strip_comments(src["nv_xnu_quiesce.h"])
    if "nvx_can_block" in q or "preemption_enabled()" not in q:
        out.append("the quiesce gates do not ask the kernel itself")
    msl = body(osx, "NvBool NV_API_CALL os_semaphore_may_sleep(")
    if not msl or "nvx_can_block()" not in msl or "NV_FALSE" not in msl:
        out.append("os_semaphore_may_sleep does not answer by context")
    for fn, kind in (("os_acquire_mutex", "NVX_LOCK_MUTEX"), ("os_acquire_rwlock_read", "NVX_LOCK_RW_READ"),
                     ("os_acquire_rwlock_write", "NVX_LOCK_RW_WRITE"), ("os_acquire_semaphore", "NVX_LOCK_SEMA")):
        b = body(osx, "NV_STATUS NV_API_CALL %s(" % fn)
        if not b or "nvx_atomic_lock(%s" % kind not in b:
            out.append(fn + " has no branch for a context that cannot block")
    a = body(osx, "NV_STATUS NV_API_CALL os_alloc_mem(")
    if not a or "nvx_atomic_alloc(" not in a or a.find("nvx_atomic_alloc(") > a.find("kern_os_malloc("):
        out.append("os_alloc_mem does not try the pool first where it cannot block")
    f = body(osx, "void  NV_API_CALL os_free_mem(")
    if not f or "nvx_atomic_free(" not in f:
        out.append("os_free_mem does not hand pool blocks back to the pool")
    sch = body(osx2, "NV_STATUS NV_API_CALL os_schedule(")
    if not sch or "NV_ERR_ILLEGAL_ACTION" not in sch:
        out.append("os_schedule does not refuse where it cannot block")
    wu = body(osx2, "void NV_API_CALL os_wait_uninterruptible(")
    if not wu or "while (!wq->done)" not in wu:
        out.append("os_wait_uninterruptible lost its condition loop")
    # the switches: read once, on unless 0
    for arg, var in (("nvrmatomic", "atomic_arg"), ("nvrmworkq", "workq_arg")):
        if not re.search(r'PE_parse_boot_argn\("%s", &%s' % (arg, var), k) or not re.search(r"\b%s = 1\b" % var, k) \
                or ("if (%s == 0)" % var) not in k:
            out.append("boot-arg %s is not read as on unless it is 0" % arg)
    # sysctls: read-only
    if re.search(r"CTLFLAG_(RW|WR)\b", k) or "SYSCTL_PROC" in k:
        out.append("nv_xnu_kcore.cpp declares a writable sysctl")
    # nothing that can block, allocate or log under a lock taken with interrupts off
    for take, drop in (("= wq_lock(q)", "wq_unlock(q, s)"), ("IOSimpleLockLockDisableInterrupt(nvx_pool_lock)",
                                                            "IOSimpleLockUnlockEnableInterrupt(nvx_pool_lock")):
        for r in locked_regions(k, take, drop):
            for bad in UNDER_SPIN:
                if bad in r:
                    out.append("%s is called with a simple lock held (%s)" % (bad, take))
    if "thread_call_cancel_wait" in k:
        out.append("nv_xnu_kcore.cpp waits for a thread call")
    # a flush of a queue given up on returns at once
    fl = body(k, "NV_STATUS nv_xnu_wq_flush(")
    if not fl or not re.search(r"if \(q->abandoned\) \{ wq_unlock\(q, s\); return NV_ERR_TIMEOUT; \}", fl) \
            or fl.find("q->abandoned) {") > fl.find("assert_wait_timeout("):
        out.append("a flush of an abandoned queue waits")
    # The glue of the default-on work queue: the worker's wakes and the flush and stop bounds. Each of these is the
    # only thing that keeps a boot, a sleep or a shutdown from waiting for ever, and none of them is read by any other
    # test. A later edit that drops one would leave the owner's machine hanging with every test still green.
    worker = body(k, "static void wq_worker(")
    if not worker:
        out.append("wq_worker not found")
    else:
        # after completing an item, under the lock, the worker must wake a flush that is waiting on done_seq
        c = worker.find("nvwq_complete(")
        fw = worker.find("thread_wakeup(&q->core.done_seq)")
        cu = worker.find("wq_unlock(q, s)", c) if c >= 0 else -1
        if not (0 <= c < fw < cu):
            out.append("wq_worker does not wake a flush waiter after completing an item")
        # when it leaves, the worker must wake a stop that is waiting on alive, before its last unlock
        aw = worker.rfind("thread_wakeup(&q->alive)")
        lu = worker.rfind("wq_unlock(q, s)")
        if not (0 <= aw < lu):
            out.append("wq_worker does not wake a stop waiter before it leaves")
    # every locked region of os_queue_work_item that enqueues an item must then wake the parked worker (the first
    # region's wake is `if (q->idle) ...`, the spare-node region's is `if (r == 0 && q->idle) ...`): the arm asks only
    # that a wake of &q->core.head follow the enqueue in that region, so both forms pass
    oqwi = body(k, "NV_STATUS NV_API_CALL os_queue_work_item(")
    enq_regions = [r for r in locked_regions(oqwi or "", "= wq_lock(q)", "wq_unlock(q, s)") if "nvwq_enqueue(" in r]
    if not enq_regions:
        out.append("os_queue_work_item has no locked region that enqueues an item")
    for r in enq_regions:
        e = r.find("nvwq_enqueue(")
        w = r.find("thread_wakeup(&q->core.head)")
        if not (0 <= e < w):
            out.append("os_queue_work_item does not wake the parked worker after enqueuing an item")
    # nv_xnu_wq_flush must test the cap and break out before it sleeps again, or every bound (stop 30 s, shutdown 2 s,
    # sleep 5 s) disappears
    if fl:
        cap = fl.find("if (cap_ms && waited_ms >= cap_ms)")
        awt = fl.find("assert_wait_timeout(&q->core.done_seq")
        if not (0 <= cap < awt):
            out.append("nv_xnu_wq_flush does not bound its wait by cap_ms before sleeping")
    # nv_xnu_wq_gpu_down must set the queue stopping and wake the worker before it waits for the worker to leave
    down = body(k, "void nv_xnu_wq_gpu_down(")
    if down:
        st = down.find("q->core.state = NVWQ_STOPPING")
        wk = down.find("thread_wakeup(&q->core.head)")
        loop = down.find("while (!gave_up && q->alive)")
        if not (0 <= st < wk < loop):
            out.append("nv_xnu_wq_gpu_down does not ask the worker to stop and wake it before it waits")
    # nvx_atomic_free, when it cannot block, must schedule the thread call that drains the deferred-free list, once
    af = body(k, "int nvx_atomic_free(")
    if af:
        pe = af.find("__atomic_exchange_n(&nvx_dfree_pending, 1, __ATOMIC_ACQ_REL)")
        tc = af.find("thread_call_enter(nvx_dfree_call)")
        if not (0 <= pe < tc):
            out.append("nvx_atomic_free does not schedule the deferred-free drain")
    # a slot that is started again keeps its numbers and its flusher counts: a flush of its previous owner may still
    # be on its way out. The bookkeeping is initialised only where the slot gets its lock, and the run state changes
    # only under that lock
    ss = body(k, "static bool wq_start_slot(")
    first = body(ss, "if (!q->lock)") if ss else None
    if not first or "nvwq_core_init(&q->core)" not in first or k.count("nvwq_core_init(") != 1:
        out.append("a work queue slot is initialised again when it is started again")
    held = locked_regions(ss or "", "= wq_lock(q)", "wq_unlock(q, s)")
    if not any("nvwq_restart(&q->core)" in r for r in held):
        out.append("a work queue is not started through nvwq_restart() under its lock")
    rest = ss or ""
    for r in held:
        rest = rest.replace(r, "")
    if re.search(r"q->(core\.state|alive|idle|running) =[^=]", rest):
        out.append("wq_start_slot changes a queue's run state without its lock")
    # stub bodies: real, and no weak stub in front of them
    st = body(k, "NvU32 NV_API_CALL os_strtoul(")
    if not st or "strtoul(str" not in st:
        out.append("os_strtoul does not parse")
    for fn in ("os_strtoul", "os_bug_check", "os_dump_stack", "os_nv_cap_validate_and_dup_fd"):
        if re.search(r"\b%s\s*\([^;]*\)\s*\{" % fn, stubs):
            out.append("nv_xnu_stubs.cpp still defines " + fn)
        if not re.search(r"NV_API_CALL %s\s*\([^;]*\)\s*\{" % fn, k):
            out.append("nv_xnu_kcore.cpp does not define " + fn)
    if "nv_xnu_stub_note(name)" not in src["nv-xnu.h"]:
        out.append("NV_XNU_STUB_HIT does not record the name")
    # the lifecycle
    go = body(nvrm, "IOReturn NVRM::go(")
    if not go:
        out.append("NVRM::go not found")
    else:
        i = go.find("fNv.queue = nv_xnu_wq_gpu_start()")
        if i < 0 or i > go.find("rm_init_adapter(") or i > go.find("addEventSource("):
            out.append("go() does not give the GPU its queue before the event source and rm_init_adapter")
        line = go[go.rfind("\n", 0, i) + 1:go.find("\n", i)] if i >= 0 else ""
        if "return" in line or re.search(r"if \(!fNv\.queue\)[^;\n]*return", go):
            out.append("go() fails when the GPU gets no queue of its own")
        if "nvregdwords: %s = %u" not in go:
            out.append("go() does not log each nvregdwords key with its value")
        # the log says what the RM did: a second '=' makes the RM skip the entry, and the RM keeps 32 bits
        if "(NvU32)strtoul(eq + 1, NULL, 0)" not in go or "!strchr(eq + 1, '=')" not in go:
            out.append("the nvregdwords log does not read the entries the way rm_parse_option_string does")
    for fn, flush, cap in (("void NVRM::stop(", "nv_xnu_wq_flush(fNv.queue, true, 30000)", "workQueueDown(30000)"),
                           ("void NVRM::shutdownFull(", "nv_xnu_wq_flush(s->fNv.queue, true, 2000)", "workQueueDown(2000)")):
        b = body(nvrm, fn)
        if not b:
            out.append(fn + " not found")
            continue
        sd = b.find("rm_shutdown_adapter(")
        if not (0 <= b.find(flush) < b.find(cap) < sd):
            out.append("%s) does not flush and stop the GPU's queue, bounded, before rm_shutdown_adapter" % fn[5:-1])
    stop = body(nvrm, "void NVRM::stop(") or ""
    if not (0 <= stop.find("nv_xnu_wq_flush(NULL, false, 30000)") < stop.find("rm_free_private_state(")):
        out.append("stop() does not flush the global queue before the RM's private state goes")
    sus = body(nvrm, "void NVRM::gpuSuspend(") or ""
    if not (0 <= sus.find("nv_xnu_wq_flush(NULL, false, 5000)") < sus.find("rm_power_management(")):
        out.append("gpuSuspend() does not flush the global queue, at most 5 s, before RM standby")
    # The question "is this range inside a GPU's windows" covers every started card (the surface fence refuses such a
    # page; with nvmultigpu=1 a second card's windows were not seen), the primary first, through the device table.
    isg = body(osx2, 'bool nv_pm_is_gpu(NvU64 start, NvU64 size)') or ""
    if not (re.search(r"nv_state_t\s*\*\s*nv\s*=\s*nv_xnu_gpu_state\s*\(\s*\)\s*;", isg)
            and re.search(r"for\s*\(\s*unsigned\s+g\s*=\s*0\s*;\s*nv\s*&&\s*g\s*<=\s*8\s*;\s*nv\s*=\s*nv_xnu_gpu_state_at\s*\(\s*g\s*\+\+\s*\)\s*\)", isg)
            and re.search(r"nv_state_t\s*\*\s*nv_xnu_gpu_state_at\s*\(\s*unsigned\s+index\s*\)\s*;", osx2)
            and re.search(r"extern\s+\"C\"\s+nv_state_t\s*\*\s*nv_xnu_gpu_state_at\s*\(\s*unsigned\s+index\s*\)\s*\{\s*NVRM\s*\*\s*s\s*=\s*gNVRMTab\.at\s*\(\s*index\s*\)", nvrm)):
        out.append("nv_pm_is_gpu does not look at the windows of every started card")
    return out


def load(root):
    return {k: (root / v).read_text() for k, v in FILES.items()}


def arm(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def main():
    src = load(ROOT)
    found = problems(src)
    arm("the work queues, the context answers and the stub bodies are wired as they must be", not found, "; ".join(found))

    def control(name, key, old, new, want):
        assert src[key].count(old) >= 1, (name, old)
        changed = dict(src)
        changed[key] = src[key].replace(old, new, 1)
        got = problems(changed)
        arm("control: " + name, any(want in g for g in got), "; ".join(got) or "nothing reported")

    control("a log line under a queue's lock", "nv_xnu_kcore.cpp", "        q->running = n->item;\n",
            "        q->running = n->item; IOLog(\"x\");\n", "IOLog is called with a simple lock held")
    control("go() fails without a queue of its own", "NVRM.cpp", "if (!fNv.queue) fNv.queue = nv_xnu_wq_gpu_start();",
            "if (!fNv.queue) fNv.queue = nv_xnu_wq_gpu_start(); if (!fNv.queue) return kIOReturnError;",
            "go() fails when the GPU gets no queue of its own")
    control("the GPU's queue made after the adapter", "NVRM.cpp", "if (!fNv.queue) fNv.queue = nv_xnu_wq_gpu_start();", "",
            "go() does not give the GPU its queue")
    control("shutdownFull() stops the queue after rm_shutdown_adapter", "NVRM.cpp",
            "        s->workQueueDown(2000);\n        if (s->fIntSrc) s->fIntSrc->disable();\n        s->irqSettle(2000);\n"
            "        rm_shutdown_adapter(NULL, &s->fNv); s->fAdapter = false;\n",
            "        if (s->fIntSrc) s->fIntSrc->disable();\n        s->irqSettle(2000);\n"
            "        rm_shutdown_adapter(NULL, &s->fNv); s->fAdapter = false;\n        s->workQueueDown(2000);\n",
            "shutdownFull) does not flush and stop")
    control("stop() with the global flush after the private state", "NVRM.cpp", "    nv_xnu_wq_flush(NULL, false, 30000);\n", "",
            "stop() does not flush the global queue")
    control("gpuSuspend() with the 30 s bound", "NVRM.cpp", "nv_xnu_wq_flush(NULL, false, 5000);",
            "nv_xnu_wq_flush(NULL, false, 30000);", "gpuSuspend() does not flush")
    control("a flush that waits on an abandoned queue", "nv_xnu_kcore.cpp",
            "    if (q->abandoned) { wq_unlock(q, s); return NV_ERR_TIMEOUT; }\n", "", "a flush of an abandoned queue waits")
    # K1: the worker drops the flush wake. Then every flush sleeps out its 5 s slices instead of being woken when the
    # queue drains.
    control("the worker without its flush wake", "nv_xnu_kcore.cpp",
            "        if (q->core.flush_waiters) thread_wakeup(&q->core.done_seq);\n", "",
            "wq_worker does not wake a flush waiter after completing an item")
    # K2: os_queue_work_item drops the first region's wake. An item queued while the worker is parked never runs, and
    # the next unbounded flush never returns.
    control("the enqueue without its wake", "nv_xnu_kcore.cpp",
            "        if (q->idle) thread_wakeup(&q->core.head);\n", "",
            "os_queue_work_item does not wake the parked worker after enqueuing an item")
    # K3: the worker leaves without waking a stop waiter. nv_xnu_wq_gpu_down then loses up to about 5 s per stop (its
    # slices are 5 s), not the whole cap.
    control("the worker without its stop wake", "nv_xnu_kcore.cpp", "    thread_wakeup(&q->alive);\n", "",
            "wq_worker does not wake a stop waiter before it leaves")
    # K5: the flush drops the cap test, so the 30 s / 2 s / 5 s bounds of stop, shutdown and sleep all disappear.
    control("the flush without its cap test", "nv_xnu_kcore.cpp",
            "            if (cap_ms && waited_ms >= cap_ms) { st = NV_ERR_TIMEOUT; break; }\n", "",
            "nv_xnu_wq_flush does not bound its wait by cap_ms before sleeping")
    # K4: gpu_down never sets the queue stopping, so the worker never leaves and gpu_down runs to its cap.
    control("the stop that never asks the worker to stop", "nv_xnu_kcore.cpp",
            "    if (q->core.state == NVWQ_RUN) q->core.state = NVWQ_STOPPING;\n", "",
            "nv_xnu_wq_gpu_down does not ask the worker to stop and wake it before it waits")
    # A2: heap frees made where blocking is not allowed pile on the deferred list and are never freed.
    control("the deferred free never scheduled", "nv_xnu_kcore.cpp",
            "    if (!__atomic_exchange_n(&nvx_dfree_pending, 1, __ATOMIC_ACQ_REL)) thread_call_enter(nvx_dfree_call);\n",
            "    (void)__atomic_exchange_n(&nvx_dfree_pending, 1, __ATOMIC_ACQ_REL);\n",
            "nvx_atomic_free does not schedule the deferred-free drain")
    control("a slot initialised again each time it is started", "nv_xnu_kcore.cpp",
            "    IOInterruptState s = wq_lock(q);\n    const bool ok = !q->alive && nvwq_restart(&q->core) == 0;",
            "    nvwq_core_init(&q->core);\n    IOInterruptState s = wq_lock(q);\n"
            "    const bool ok = !q->alive && nvwq_restart(&q->core) == 0;",
            "a work queue slot is initialised again when it is started again")
    control("a queue started without nvwq_restart()", "nv_xnu_kcore.cpp",
            "const bool ok = !q->alive && nvwq_restart(&q->core) == 0;",
            "const bool ok = !q->alive; q->core.state = NVWQ_RUN;", "not started through nvwq_restart() under its lock")
    control("a failed start that resets the run state without the lock", "nv_xnu_kcore.cpp",
            "        s = wq_lock(q);\n        q->core.state = NVWQ_OFF;\n        q->alive = false;\n        wq_unlock(q, s);\n",
            "        q->core.state = NVWQ_OFF;\n        q->alive = false;\n", "changes a queue's run state without its lock")
    control("the atomic answers on with nvrmatomic=0", "nv_xnu_kcore.cpp", "if (atomic_arg == 0)", "if (atomic_arg == 2)",
            "boot-arg nvrmatomic is not read")
    control("the work queue switched off by default", "nv_xnu_kcore.cpp", "workq_arg = 1;", "workq_arg = 0;",
            "boot-arg nvrmworkq is not read")
    control("a writable sysctl", "nv_xnu_kcore.cpp", "nvrm_wq_queued, CTLFLAG_RD", "nvrm_wq_queued, CTLFLAG_RW",
            "declares a writable sysctl")
    control("the nvregdwords log counts an entry with a second '=' that the RM skips", "NVRM.cpp",
            "if (eq && eq != key && eq[1] && !strchr(eq + 1, '='))", "if (eq && eq != key && eq[1])",
            "the nvregdwords log does not read the entries the way rm_parse_option_string does")
    control("the weak os_strtoul stub back", "nv_xnu_stubs.cpp", "extern \"C\" {\n",
            "extern \"C\" {\n__attribute__((weak)) NvU32 NV_API_CALL os_strtoul(const char *, char **, NvU32)\n{\n    return 0;\n}\n",
            "nv_xnu_stubs.cpp still defines os_strtoul")
    control("os_semaphore_may_sleep always yes", "os-xnu.cpp",
            "if (nvx_can_block()) return NV_TRUE; nvx_atomic_maysleep_false(); return NV_FALSE;", "return NV_TRUE;",
            "os_semaphore_may_sleep does not answer by context")
    control("the condition loop of os_wait_uninterruptible lost", "os-xnu2.cpp",
            "    while (!wq->done) IOLockSleep(wq->lock, wq, THREAD_UNINT);",
            "    if (!wq->done) IOLockSleep(wq->lock, wq, THREAD_UNINT);", "lost its condition loop")
    control("the quiesce gates on nvx_can_block()", "nv_xnu_quiesce.h", "return preemption_enabled() ? 1 : 0;",
            "return nvx_can_block() ? 1 : 0;", "the quiesce gates do not ask the kernel itself")
    control("a thread per work item again", "os-xnu2.cpp", "// os_queue_work_item, os_flush_work_queue",
            "NV_STATUS NV_API_CALL os_queue_work_item(struct os_work_queue *q, void *i) { return 0; }\n// x",
            "os-xnu2.cpp still defines os_queue_work_item")
    control("a window check that sees only the primary card", "os-xnu2.cpp",
            "for (unsigned g = 0; nv && g <= 8; nv = nv_xnu_gpu_state_at(g++))", "for (unsigned g = 0; nv && g < 1; g++)",
            "nv_pm_is_gpu does not look at the windows of every started card")
    print(("\n%d failed" % len(FAILS)) if FAILS else "\nall passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
