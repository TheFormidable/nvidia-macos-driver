#!/usr/bin/env python3
"""The serialized work queues and the answers by context (kexts/NVRM/nv_xnu_kcore.cpp) are wired the
way they have to be, read from the source text.

The pure logic is tested on the host (test_nvrm_workq_logic.c, test_nvrm_atomic_pool.c, test_nvrm_itree.c). What can
go wrong around it is the wiring: a GPU start that fails because it got no queue of its own, an adapter shut down while
its items still run, a flush that a teardown waits on for ever, a log line or an allocation under a lock taken with
interrupts off, a switch that is not on by default. Each arm below reads the
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
    arm("the work queues and the context answers are wired as they must be", not found, "; ".join(found))

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
            "        s->workQueueDown(2000);\n        rm_shutdown_adapter(NULL, &s->fNv); s->fAdapter = false;\n",
            "        rm_shutdown_adapter(NULL, &s->fNv); s->fAdapter = false;\n        s->workQueueDown(2000);\n",
            "shutdownFull) does not flush and stop")
    control("stop() with the global flush after the private state", "NVRM.cpp", "    nv_xnu_wq_flush(NULL, false, 30000);\n", "",
            "stop() does not flush the global queue")
    control("gpuSuspend() with the 30 s bound", "NVRM.cpp", "nv_xnu_wq_flush(NULL, false, 5000);",
            "nv_xnu_wq_flush(NULL, false, 30000);", "gpuSuspend() does not flush")
    control("a flush that waits on an abandoned queue", "nv_xnu_kcore.cpp",
            "    if (q->abandoned) { wq_unlock(q, s); return NV_ERR_TIMEOUT; }\n", "", "a flush of an abandoned queue waits")
    control("the atomic answers on with nvrmatomic=0", "nv_xnu_kcore.cpp", "if (atomic_arg == 0)", "if (atomic_arg == 2)",
            "boot-arg nvrmatomic is not read")
    control("the work queue switched off by default", "nv_xnu_kcore.cpp", "workq_arg = 1;", "workq_arg = 0;",
            "boot-arg nvrmworkq is not read")
    control("a writable sysctl", "nv_xnu_kcore.cpp", "nvrm_wq_queued, CTLFLAG_RD", "nvrm_wq_queued, CTLFLAG_RW",
            "declares a writable sysctl")
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
    print(("\n%d failed" % len(FAILS)) if FAILS else "\nall passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
