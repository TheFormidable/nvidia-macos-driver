#!/usr/bin/env python3
"""NVK's channel recovery: how nvk/nvk-channel-recover.patch rebuilds a faulted or stalled channel.

The decisions (which losses are rebuilt, the budget, the plan of what is dropped and replayed, the order of the steps)
are in the patch's nvkmd/nvrm/nvkmd_nvrm_recover.h and are host-tested by its nvkmd_nvrm_recover_test.c, which this
test extracts from the patch, builds and runs. The patch's copy of nvrm_chan_note_abi.h is compared with the kext's by
tools/test_shared_headers.py. This test reads the patch and holds what only NVK's glue can get wrong:

  the chain      nvk-channel-recover is applied by the default 2.9 build, after nvk-evwait and nvk-fence (the
                 rebuild calls the fence's rebind); the patch header reads as text to BSD patch
  the switches   NVK_CHANNEL_RECOVER is read through nvmtl_cfg_getenv (the environment, or nvrm610.conf, which is
                 all a release build sees) and only "0" turns it off; NVK_FAULT_INJECT the same way
  the waits      nvrm_gsub_wait_seq returns success only when the semaphore reached the sequence (or the channel was
                 unregistered, which idled it): a rebuild means "look again", never "done"; its stall clock and
                 nvrm_gsub_wait's run on the awake clock and restart when a channel was rebuilt; both still sleep
                 through nvkmd_nvrm_wait_block(_until); the clock is CLOCK_UPTIME_RAW on macOS
  the locks      the lock rules are written where the context mutexes are declared; every op takes the mutex first
                 and every return after that goes through nvrm_rec_leave; the four blocking waits release it
  the rebuild    the new channel is built before the old one is freed; a failed free of the old channel gives up
                 (FAIL(FREE)) and changes nothing; the GPFIFO the plan fills is the new channel's own; the prologue
                 buffer is new for every rebuild; the adopt step sets errBase, then calls the fence's rebind without
                 the slot table's mutex, then clears `recovering`; the dropped range's last value is stored at offset 0
                 of the context's own semaphore page, 64 bits, as the kernel fence reads it
  the kext       the channel note goes through the existing escape path with the header's number (no new selector,
                 no kext file in the patch); a kext that does not answer is asked once and never again
  the sleep      no escape is made with a context mutex held by the kick: its note follows the unlock; a stall on a
                 GPU the kext calls suspended asks RM nothing (the inputs make no RM call, the action comes after the
                 suspended test) and gives the claim back and the mutex up without counting, reporting or giving up
  the exports    nvmtl_nvk_fence_dropped_v1 and nvmtl_nvk_recover_stats_v1 with the signatures of the contract,
                 default visibility, declared in nvkmd.h
  the 2.9 path   the 2.9 verdicts are kept as functions and are what the hooks call with recovery off

Each source check also runs on a damaged copy of the patch text, which must fail it (the controls)."""
import os, pathlib, re, shutil, subprocess, sys, tempfile

R = pathlib.Path(__file__).resolve().parents[1]
PATCH = R / "nvk/nvk-channel-recover.patch"
SCRIPT = R / "build/build_nvk.sh"
DIR = "src/nouveau/vulkan/nvkmd/nvrm/"
fails = 0


def arm(name, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + name + (f" ({str(detail).strip()[-300:]})" if detail and not ok else ""))
    fails += not ok


def newfile(text, path):
    """The whole content of `path` when the patch creates it (a new-file hunk), else ''."""
    out, cur, isnew = [], None, False
    for line in text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            cur, isnew = line.rstrip("\n").split(" b/", 1)[1], False
        elif cur != path:
            continue
        elif line.startswith("new file mode"):
            isnew = True
        elif not isnew or line.startswith(("--- ", "+++ ", "index ", "@@", "\\")):
            continue
        elif line.startswith("+"):
            out.append(line[1:])
    return "".join(out)


def added(text, path):
    """The '+' lines of `path`, joined."""
    out, cur = [], None
    for line in text.splitlines():
        if line.startswith("diff --git "):
            cur = line.split(" b/", 1)[1]
        elif cur == path and line.startswith("+") and not line.startswith("+++ "):
            out.append(line[1:])
    return "\n".join(out)


def newtext(text, path):
    """The new text (context and added lines) of `path`'s hunks, each hunk after an empty line."""
    out, cur = [], None
    for line in text.splitlines():
        if line.startswith("diff --git "):
            cur = line.split(" b/", 1)[1]
        elif cur != path or line.startswith(("--- ", "+++ ", "index ", "new file mode", "\\")):
            continue
        elif line.startswith("@@"):
            out.append("")
        elif line.startswith(("+", " ")):
            out.append(line[1:])
        elif line == "":
            out.append("")
    return "\n".join(out)


def body(src, name):
    """The text of the C function `name` (its definition): from its name at the start of a line to the first '}'
    alone on a line."""
    m = re.search(r"(?ms)^" + re.escape(name) + r"\(.*?^\}$", src)
    return m.group(0) if m else ""


def fhunks(text, path, name):
    """The new text (context and added lines) of every hunk of `path` that git's hunk header places in function
    `name`, or that contains the line opening its definition, joined in patch order."""
    out, cur, keep = [], None, False
    for line in text.splitlines():
        if line.startswith("diff --git "):
            cur, keep = line.split(" b/", 1)[1], False
        elif cur != path or line.startswith(("--- ", "+++ ", "index ", "new file mode", "\\")):
            continue
        elif line.startswith("@@"):
            keep = line.split("@@")[-1].strip().startswith(name + "(")
            if keep:
                out.append("")
        elif line.startswith(("+", " ")):
            if not keep and line[1:].startswith(name + "("):
                keep = True
                out.append("")
            if keep:
                out.append(line[1:])
    return "\n".join(out)


def fadded(text, path, name):
    """Only the added lines of the hunks fhunks() keeps."""
    out, cur, keep = [], None, False
    for line in text.splitlines():
        if line.startswith("diff --git "):
            cur, keep = line.split(" b/", 1)[1], False
        elif cur != path or line.startswith(("--- ", "+++ ", "index ", "new file mode", "\\")):
            continue
        elif line.startswith("@@"):
            keep = line.split("@@")[-1].strip().startswith(name + "(")
        elif line.startswith(("+", " ")):
            keep = keep or line[1:].startswith(name + "(")
            if keep and line.startswith("+"):
                out.append(line[1:])
    return "\n".join(out)


def func(text, path, name):
    """What the patch shows of the function `name` of `path` (a diff has only a few lines of context): its hunks, from
    its definition line when they show it, to the first '}' alone on a line."""
    t = fhunks(text, path, name)
    i = t.find("\n" + name + "(")
    t = t[i + 1:] if i >= 0 else t
    j = t.find("\n}")
    return t[:j + 2] if j >= 0 else t


def order(text, *parts):
    """Every part occurs in text, each after the one before it."""
    at = -1
    for p in parts:
        at = text.find(p, at + 1)
        if at < 0:
            return False
    return True


REC_C, REC_H, REC_T = DIR + "nvkmd_nvrm_recover.c", DIR + "nvkmd_nvrm_recover.h", DIR + "nvkmd_nvrm_recover_test.c"
CTX_C, SYNC_C, NVRM_H = DIR + "nvkmd_nvrm_ctx.c", DIR + "nvkmd_nvrm_sync.c", DIR + "nvkmd_nvrm.h"
API_C = DIR + "nvRmApi.c"
NVKMD_H = "src/nouveau/vulkan/nvkmd/nvkmd.h"


# --- the checks: each takes the patch text and answers True when it holds ----------------------------------------------

def chk_switches(p):
    rc = newfile(p, REC_C)
    en = body(rc, "nvrm_rec_enabled")
    return ('nvmtl_cfg_getenv("NVK_CHANNEL_RECOVER")' in en and "e[0] == '0'" in en
            and 'getenv("NVK_CHANNEL_RECOVER")' not in rc.replace('nvmtl_cfg_getenv("NVK_CHANNEL_RECOVER")', "")
            and 'nvmtl_cfg_getenv("NVK_FAULT_INJECT")' in rc
            and 'getenv("NVK_FAULT_INJECT")' not in rc.replace('nvmtl_cfg_getenv("NVK_FAULT_INJECT")', ""))


def chk_wait_seq(p):
    ws = func(p, CTX_C, "nvrm_gsub_wait_seq")
    add = fadded(p, CTX_C, "nvrm_gsub_wait_seq")
    # the 2.9 successes (semaphore at seq) are unchanged context; the one success the patch adds is the 2.9 answer for
    # a channel that was unregistered; every other return passes a failure on
    succ = re.findall(r"(?m)^([^\n]*)\n[^\n]*return VK_SUCCESS;", add)
    rets = set(re.findall(r"return [^;]*;", ws))
    fails_ok = all(re.search(r"if \(r != VK_SUCCESS\)\s*$", ws[:m.start()]) for m in re.finditer(r"return r;", ws))
    return (bool(ws) and "if (v >= seq)" in ws and rets <= {"return VK_SUCCESS;", "return r;"} and fails_ok
            and [x.strip() for x in succ] == ["if (!nvrm_gsub_slot_registered(slot, gen))"]
            and "return nvkmd_nvrm_slot_stalled(" not in ws
            and "tp = ta;                /* rebuilt, or being rebuilt: look again */" in ws
            and "nvkmd_nvrm_wait_block(" in ws and "nvrm_rec_now_ns()" in ws and "nvkmd_nvrm_slot_epoch(slot)" in ws)


def chk_no_stalled_return(p):
    return "return nvkmd_nvrm_slot_stalled(" not in added(p, CTX_C) + added(p, SYNC_C)


def chk_gsub_wait(p):
    gw = func(p, SYNC_C, "nvrm_gsub_wait")
    return (bool(gw) and "int64_t tp = nvrm_rec_now_ns();" in gw and "const int64_t ta = nvrm_rec_now_ns();" in gw
            and "((uint64_t)sl->epoch << 40)" in gw and "nvkmd_nvrm_slot_kick(hslot[0], hgen[0], log_obj);" in gw
            and "tp = ta;" in gw and "tp = t;" not in gw)


def chk_clock(p):
    now = body(newfile(p, REC_C), "nvrm_rec_now_ns")
    return "clock_gettime_nsec_np(CLOCK_UPTIME_RAW)" in now and "os_time_get_nano()" in now


def chk_lock_rules(p):
    rc = newfile(p, REC_C)
    i = rc.find("static pthread_mutex_t nvrm_rec_mtx[NVRM_CTX_MAX]")
    j = rc.find("LOCK RULES")
    rules = rc[j:i] if 0 <= j < i else ""
    return (bool(rules) and "No thread holds two context mutexes" in rules
            and all(w in rules for w in ("nvrm_gsub_gpfifo_reserve", "nvrm_gsub_submit", "nvrm_gsub_ctx_wait",
                                         "the wait of sync", "nvkmd_nvrm_gsub_mtx", "nvrm_rec_lock_bounded",
                                         "nvrm_rec_mark_dying", "nvkmd_nvrm_fence_chan_rebind")))


OPS = ["nvkmd_nvrm_exec_ctx_wait", "nvkmd_nvrm_exec_ctx_flush", "nvkmd_nvrm_exec_ctx_exec",
       "nvkmd_nvrm_exec_ctx_signal", "nvkmd_nvrm_exec_ctx_sync"]


def op_balanced(b):
    """After `r = nvrm_rec_enter(ctx, log_obj); if (r != VK_SUCCESS) return r;` every return of the op is the last
    statement, right after nvrm_rec_leave(ctx)."""
    m = re.search(r"VkResult r = nvrm_rec_enter\(ctx, log_obj\);\n\s*if \(r != VK_SUCCESS\)\n\s*return r;", b)
    if not m:
        return False
    rest = b[m.end():]
    end = rest.find("\n}")
    rest = rest[:end] if end >= 0 else rest
    rets = [x.start() for x in re.finditer(r"\breturn\b", rest)]
    leave = rest.rfind("nvrm_rec_leave(ctx);")
    return len(rets) == 1 and leave >= 0 and leave < rets[0] and rest[leave:rets[0]].count(";") == 1


def chk_ops(p):
    src = newtext(p, CTX_C)
    sr = body(newfile(p, REC_C), "nvkmd_nvrm_set_reinit")
    return (all(op_balanced(func(p, CTX_C, op)) for op in OPS)
            and order(sr, "nvrm_rec_enter(ctx, NULL)", "return;", "nvrm_rec_leave(ctx);")
            and ".set_reinit = nvkmd_nvrm_set_reinit," in src)


def chk_blocking_waits(p):
    res, sub, cw = (func(p, CTX_C, f) for f in ("nvrm_gsub_gpfifo_reserve", "nvrm_gsub_submit", "nvrm_gsub_ctx_wait"))
    syn = func(p, CTX_C, "nvkmd_nvrm_exec_ctx_sync")
    own = func(p, CTX_C, "nvrm_gsub_wait_seq_owner")
    plain = [m.start() for m in re.finditer(r"nvrm_gsub_wait_seq\(ctx", added(p, CTX_C))]
    return ("nvrm_gsub_wait_seq_owner(ctx, ctx->slotSeq[s], log_obj)" in res and "ctx->recEpoch != ep" in res
            and "nvrm_gsub_wait_seq_owner(ctx, ctx->slotSeq[next], log_obj)" in sub
            and order(cw, "nvrm_rec_release(ctx)", "nvkmd_nvrm_sync_cpu_wait(", "nvrm_rec_retake(ctx, log_obj)")
            and "nvrm_gsub_wait_seq_owner(ctx, ctx->wSeq, log_obj)" in syn
            and order(own, "nvrm_rec_release(ctx)", "nvrm_gsub_wait_seq(ctx, seq, log_obj)", "nvrm_rec_retake(ctx, log_obj)")
            and len(plain) == 1)   # the owner helper only (the destroy's idle wait, outside every op, is 2.9's)


def chk_pointers_after_reserve(p):
    sub = func(p, CTX_C, "nvrm_gsub_submit")
    return order(sub, "nvrm_gsub_gpfifo_reserve(ctx, log_obj)", "NvNotification *notifiers = ctx->notifier->map;",
                 "KeplerBControlGPFifo *userD = ctx->userD->map;", "ctx->slotBeg[cur] = ctx->subPut;",
                 "ctx->subPut = ctx->gpPut;")


def chk_build_first(p):
    h = newfile(p, REC_H)
    ex = body(h, "nvrm_rec_execute")
    return order(ex, "ops->build(u)", "ops->free_old(u)", "ops->discard(u);", "return NVRM_WHY_FREE;",
                 "ops->adopt(u,", "ops->start(u,")


def chk_free_fails(p):
    fo = body(newfile(p, REC_C), "rec_op_free_old")
    return order(fo, "nvRmApiFree(&rm, ctx->hChannel)", "if (rc != NV_OK)", "return -1;", "ctx->hChannel = 0;")


def chk_new_buffers(p):
    rc = newfile(p, REC_C)
    ad, bd = body(rc, "rec_op_adopt"), body(rc, "rec_op_build")
    return ("nvrm_rec_fill(ring, p, ctx->gpFifo->map, d->gpFifo->map, j->prologueEntry, &out);" in ad
            and "nvkmd_dev_alloc_mapped_mem(ctx->base.dev, j->log_obj, size, 0x1000, NVKMD_MEM_GART" in bd
            and "ctx->recPrologue = job.prologue;" in rc)


def chk_adopt_order(p):
    ad = body(newfile(p, REC_C), "rec_op_adopt")
    return order(ad, "pthread_mutex_lock(&nvkmd_nvrm_gsub_mtx);", "s->errBase = newBase;",
                 "pthread_mutex_unlock(&nvkmd_nvrm_gsub_mtx);", "nvkmd_nvrm_fence_chan_rebind(ctx);",
                 "pthread_mutex_lock(&nvkmd_nvrm_gsub_mtx);", "s->recovering = false;")


def chk_dropped_store(p):
    ad = body(newfile(p, REC_C), "rec_op_adopt")
    return order(ad, "nvkmd_nvrm_fence_chan_rebind(ctx);", "uint64_t *sem = ctx->sem->map;",
                 "if (__atomic_load_n(sem, __ATOMIC_ACQUIRE) < p->dropHi)",
                 "__atomic_store_n(sem, p->dropHi, __ATOMIC_RELEASE);", "s->recovering = false;")


def chk_kext(p):
    rc = newfile(p, REC_C)
    note = body(rc, "rec_note")
    api = added(p, API_C)
    files = re.findall(r"(?m)^diff --git a/(\S+)", p)
    return ("nvRmIoctl(api->fd, NVRM_ESC_CHAN_NOTE, e, sizeof(*e))" in api
            and "e->status == NVRM_CN_OK" in api
            and order(note, "__atomic_load_n(&nvrm_rec_kext, __ATOMIC_ACQUIRE) == 2", "return -1;",
                      "nvRmApiChanNote(&rm, &e) != 0", "__atomic_compare_exchange_n(&nvrm_rec_kext, &expect, 2")
            and not any(f.startswith("kexts/") for f in files)
            and "IOConnectCall" not in "".join(added(p, f) for f in files))


def chk_kick_note(p):
    k = body(newfile(p, REC_C), "nvkmd_nvrm_slot_kick")
    i, j = k.find("pthread_mutex_trylock(&nvrm_rec_mtx[R])"), k.find("pthread_mutex_unlock(&nvrm_rec_mtx[R]);")
    held = k[i:j] if 0 <= i < j else ""
    return (bool(held) and "rec_doorbell(rctx, (uint32_t)rctx->subPut);" in held
            and "kpdev = nvkmd_nvrm_pdev(rctx->base.dev->pdev);" in held
            and not re.search(r"\brec_note\s*\(|\bnvRm\w*\s*\(", held)
            and k.count("pthread_mutex_unlock(&nvrm_rec_mtx[R]);") == 1
            and order(k[j:], "pthread_mutex_unlock(&nvrm_rec_mtx[R]);", "if (kpdev != NULL) {",
                      "rec_note(kpdev, NVRM_CN_KICK, 0, 0, 0, 0, NULL);"))


def chk_asleep(p):
    rc, h = newfile(p, REC_C), newfile(p, REC_H)
    ex, inp, act = body(h, "nvrm_rec_execute"), body(rc, "rec_op_inputs"), body(rc, "rec_op_action")
    run = body(rc, "nvrm_rec_run")
    i = run.find("if (why == NVRM_WHY_ASLEEP) {")
    blk = run[i:run.find("\n   }\n", i)] if i >= 0 else ""
    fin = body(rc, "rec_why_is_final")
    return (order(ex, "ops->inputs(u, &in);", "if (nvrm_rec_asleep(in.pm, in.changed, st->now, asleepSince))",
                  "return NVRM_WHY_ASLEEP;", "ops->action(u, &in);", "nvrm_rec_classify(&in)")
            and "rec_note(j->pdev, NVRM_CN_QUERY, 0, 0, 0, 0, &pm);" in inp and "nvRm" not in inp
            and "NV2080_CTRL_CMD_GPU_GET_RECOVERY_ACTION" in act and ".action = rec_op_action," in rc
            and "&ctx->recAsleepNs" in run and bool(blk)
            and order(run, "nvrm_rec_execute(", "if (why == NVRM_WHY_ASLEEP) {", "if (why != NVRM_WHY_OK) {")
            and order(blk, "r->recovering = false;", "nvrm_rec_held--;", "pthread_mutex_unlock(&nvrm_rec_mtx[R]);",
                      "return VK_SUCCESS;")
            and not re.search(r"rec_fail|rec_stat_|gaveUp|faulted|rec_note|nvRm", blk)
            and bool(fin) and "NVRM_WHY_ASLEEP" not in fin.split("{", 1)[-1])


def chk_exports(p):
    rc, sy, kh = newfile(p, REC_C), added(p, SYNC_C), added(p, NVKMD_H)
    return ('__attribute__((visibility("default"))) unsigned\nnvmtl_nvk_recover_stats_v1(uint64_t *out, unsigned n)' in rc
            and '__attribute__((visibility("default"))) int\nnvmtl_nvk_fence_dropped_v1(VkDevice device, VkFence fence)' in sy
            and "int nvmtl_nvk_fence_dropped_v1(VkDevice device, VkFence fence);" in kh
            and "unsigned nvmtl_nvk_recover_stats_v1(uint64_t *out, unsigned n);" in kh
            and "out[REC_ST_VERSION] = 1;" in rc and "REC_ST_FENCES_DROPPED, REC_ST_N };" in rc)


def chk_off_is_29(p):
    src = newtext(p, CTX_C)
    h, s = func(p, CTX_C, "nvkmd_nvrm_slot_health"), func(p, CTX_C, "nvkmd_nvrm_slot_stalled")
    return (order(h, "if (!nvrm_rec_enabled())", "return nvrm_slot_health_29(slot, gen, log_obj);")
            and order(s, "if (!nvrm_rec_enabled())", "return nvrm_slot_stalled_29(slot, gen, want, ms, log_obj);")
            and "nvrm_slot_health_29(uint32_t slot" in src and "nvrm_slot_stalled_29(uint32_t slot" in src)


def chk_classes(p):
    cl = body(newfile(p, REC_H), "nvrm_rec_classify")
    return ("case 13: case 31: case 32: case 39: case 40: case 41: case 43: case 44: case 45: case 69: case 109:" in cl
            and "return NVRM_LOSS_UNKNOWN;" in cl
            and "if (k != NVRM_LOSS_CHANNEL && k != NVRM_LOSS_STALL)\n      return NVRM_WHY_KIND;" in newfile(p, REC_H))


# --- run ---------------------------------------------------------------------------------------------------------------
patch = PATCH.read_text() if PATCH.exists() else ""
arm("nvk/nvk-channel-recover.patch exists", bool(patch))

m = re.search(r"(?ms)^case \$REL in\n.*?^esac$", SCRIPT.read_text())
r = subprocess.run(["bash", "-c", 'REL=2.9; NVK=x; FIXES=; CHAIN=; BASE=; eval "$1" >/dev/null; printf "%s" "$CHAIN"', "_",
                    m.group(0)], capture_output=True, text=True) if m else None
chain = r.stdout.split() if r and r.returncode == 0 else []
arm("the default 2.9 chain applies nvk-channel-recover, after nvk-evwait and nvk-fence",
    all(x in chain for x in ("nvk-evwait", "nvk-fence", "nvk-channel-recover"))
    and chain.index("nvk-evwait") < chain.index("nvk-fence") < chain.index("nvk-channel-recover"), " ".join(chain[-5:]))
head = patch.split("\ndiff --git ", 1)[0]
bad = [l for l in head.splitlines() if re.match(r"(\d|--- |\+\+\+ |@@|\*\*\* |Index:|diff |< |> )", l)]
arm("no header line of the patch reads as a diff or an ed command to BSD patch", "\ndiff --git " in patch and not bad, bad)
arm("the patch creates nvkmd_nvrm_recover.h, its host test and nvrm_chan_note_abi.h as new files",
    all(newfile(patch, DIR + f) for f in ("nvkmd_nvrm_recover.h", "nvkmd_nvrm_recover_test.c", "nvrm_chan_note_abi.h")))
mb = added(patch, "src/nouveau/vulkan/meson.build")
arm("meson builds nvkmd_nvrm_recover.c (the host test is not a meson target)",
    "'nvkmd/nvrm/nvkmd_nvrm_recover.c'," in mb and "recover_test" not in mb)
hdr = newfile(patch, REC_H)
arm("the pure header includes only stdbool, stdint and string",
    sorted(re.findall(r"(?m)^#include\s+(\S+)", hdr)) == ["<stdbool.h>", "<stdint.h>", "<string.h>"])
arm("the ring of the plan is the context's command ring (static assert)",
    "_Static_assert(NVRM_REC_RING == NVRM_CMD_SLOTS" in newfile(patch, REC_C))

CHECKS = [
    ("the switches: NVK_CHANNEL_RECOVER and NVK_FAULT_INJECT through nvmtl_cfg_getenv, only '0' turns recovery off",
     chk_switches, "v = !(e != NULL && e[0] == '0')", "v = !(e != NULL && e[0] != '1')"),
    ("the waits: nvrm_gsub_wait_seq succeeds only at the sequence; a rebuild means look again", chk_wait_seq,
     "+         tp = ta;                /* rebuilt, or being rebuilt: look again */",
     "+         return VK_SUCCESS;"),
    ("the waits: no added return of the stall hook's answer as the wait's result", chk_no_stalled_return,
     "         r = nvkmd_nvrm_slot_stalled(slot, gen, seq,", "         return nvkmd_nvrm_slot_stalled(slot, gen, seq,"),
    ("the waits: nvrm_gsub_wait's stall clock is the awake clock and restarts on a rebuild", chk_gsub_wait,
     "+ ((uint64_t)sl->epoch << 40)", ""),
    ("the waits: the awake clock is CLOCK_UPTIME_RAW", chk_clock, "CLOCK_UPTIME_RAW", "CLOCK_MONOTONIC_RAW"),
    ("the locks: the rules are written where the context mutexes are declared", chk_lock_rules,
     "No thread holds two context mutexes", "A thread may hold two context mutexes"),
    ("the locks: every op takes the mutex first and leaves through nvrm_rec_leave", chk_ops,
     "+   r = nvrm_exec_ctx_flush_held(ctx, log_obj);\n    if (r == VK_SUCCESS && ctx->gsub)",
     "+   r = nvrm_exec_ctx_flush_held(ctx, log_obj);\n+   if (r != VK_SUCCESS)\n+      return r;\n    if (r == VK_SUCCESS && ctx->gsub)"),
    ("the locks: the four blocking waits release the mutex", chk_blocking_waits,
     "r = nvrm_gsub_wait_seq_owner(ctx, ctx->slotSeq[next], log_obj);",
     "r = nvrm_gsub_wait_seq(ctx, ctx->slotSeq[next], log_obj);"),
    ("the locks: the submission reads the notifier and USERD after the reserve's wait", chk_pointers_after_reserve,
     "   ctx->slotBeg[cur] = ctx->subPut;", "   ctx->slotBeg[cur] = ctx->gpPut;"),
    ("the rebuild: the new channel is built before the old one is freed", chk_build_first,
     "+   if (ops->build(u) != 0)\n+      return NVRM_WHY_BUILD;\n+   if (ops->free_old(u) != 0) {",
     "+   if (ops->free_old(u) != 0) {"),
    ("the rebuild: a failed free of the old channel gives up", chk_free_fails,
     "   if (rc != NV_OK) {", "   if (0) {"),
    ("the rebuild: the plan fills the new channel's GPFIFO; the prologue is a new buffer", chk_new_buffers,
     "ctx->gpFifo->map, d->gpFifo->map, j->prologueEntry", "ctx->gpFifo->map, ctx->gpFifo->map, j->prologueEntry"),
    ("the rebuild: errBase, then the fence's rebind outside the slot table's mutex, then recovering cleared",
     chk_adopt_order, "   (void)nvkmd_nvrm_fence_chan_rebind(ctx);\n", ""),
    ("the rebuild: the dropped range's last value goes to offset 0 of the semaphore page, 64 bits", chk_dropped_store,
     "         __atomic_store_n(sem, p->dropHi, __ATOMIC_RELEASE);", "         __atomic_store_n(sem, p->dropLo, __ATOMIC_RELEASE);"),
    ("the kext: escape 0xE8 through the existing path, no selector, a kext without it is asked once", chk_kext,
     "   if (pdev == NULL || __atomic_load_n(&nvrm_rec_kext, __ATOMIC_ACQUIRE) == 2)", "   if (pdev == NULL)"),
    ("the sleep: the kick's note follows the unlock of the context mutex", chk_kick_note,
     "+   pthread_mutex_unlock(&nvrm_rec_mtx[R]);\n+   if (kpdev != NULL) {\n+      rec_note(kpdev, NVRM_CN_KICK, 0, 0, 0, 0, NULL);\n",
     "+   rec_note(kpdev, NVRM_CN_KICK, 0, 0, 0, 0, NULL);\n+   pthread_mutex_unlock(&nvrm_rec_mtx[R]);\n+   if (kpdev != NULL) {\n"),
    ("the sleep: a stall on a suspended GPU asks RM nothing and looks again", chk_asleep,
     "+   if (nvrm_rec_asleep(in.pm, in.changed, st->now, asleepSince))\n+      return NVRM_WHY_ASLEEP;\n+   ops->action(u, &in);\n",
     "+   ops->action(u, &in);\n+   if (nvrm_rec_asleep(in.pm, in.changed, st->now, asleepSince))\n+      return NVRM_WHY_ASLEEP;\n"),
    ("the sleep: the look-again path counts nothing as given up", chk_asleep,
     "+      nvrm_rec_held--;\n+      pthread_mutex_unlock(&nvrm_rec_mtx[R]);\n+      return VK_SUCCESS;\n+   }\n",
     "+      rec_stat_add(REC_ST_GAVE_UP, 1);\n+      nvrm_rec_held--;\n+      pthread_mutex_unlock(&nvrm_rec_mtx[R]);\n+      return VK_SUCCESS;\n+   }\n"),
    ("the sleep: the look-again reason is not final", chk_asleep,
     "+   return w == NVRM_WHY_KIND ||", "+   return w == NVRM_WHY_ASLEEP || w == NVRM_WHY_KIND ||"),
    ("the exports: the two functions of the contract, default visibility, declared in nvkmd.h", chk_exports,
     "+nvmtl_nvk_fence_dropped_v1(VkDevice device, VkFence fence)\n+{", "+nvmtl_nvk_fence_dropped_v1(VkFence fence, VkDevice device)\n+{"),
    ("the 2.9 path: with recovery off the hooks run the kept 2.9 verdicts", chk_off_is_29,
     "      return nvrm_slot_health_29(slot, gen, log_obj);", "      return VK_SUCCESS;"),
    ("the kinds: only channel exceptions and stalls are rebuilt", chk_classes,
     "+   if (k != NVRM_LOSS_CHANNEL && k != NVRM_LOSS_STALL)\n+      return NVRM_WHY_KIND;",
     "+   if (k == NVRM_LOSS_NONE)\n+      return NVRM_WHY_KIND;"),
]
for name, fn, good, broken in CHECKS:
    arm(name, fn(patch))
    damaged = patch.replace(good, broken)
    arm("control: " + name.split(":")[0] + " (" + name.split(":")[1].strip()[:60] + ") - a damaged copy fails",
        damaged != patch and not fn(damaged), "the damage did not apply" if damaged == patch else "")

# --- the host test of the pure header, from the patch ------------------------------------------------------------------
cc = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
test_src = newfile(patch, REC_T)
line = next((l for l in test_src.splitlines()[:12] if l.lstrip(" *").startswith("cc ")), "")
arm("the host test names its build line in its head", bool(line))
if cc is None:
    print("SKIP the host test of nvkmd_nvrm_recover.h: no C compiler")
else:
    with tempfile.TemporaryDirectory() as d:
        dd = pathlib.Path(d)
        (dd / "nvkmd_nvrm_recover.h").write_text(hdr)
        (dd / "nvkmd_nvrm_recover_test.c").write_text(test_src)
        b = subprocess.run([cc, "-std=c11", "-O1", "-g", "-Wall", "-Wextra", "-Werror", "-I.", "nvkmd_nvrm_recover_test.c",
                            "-o", "t"], cwd=d, capture_output=True, text=True)
        run = subprocess.run(["./t"], cwd=d, capture_output=True, text=True) if b.returncode == 0 else None
        arm("the host test of nvkmd_nvrm_recover.h builds and passes",
            run is not None and run.returncode == 0, (b.stderr if run is None else run.stdout[-300:]))
        # control: the same test against a header whose plan replays the flush that faulted must fail
        broken = hdr.replace("lv == NVRM_REC_REPLAY ? S + 2", "lv == NVRM_REC_REPLAY ? S + 1")
        (dd / "nvkmd_nvrm_recover.h").write_text(broken)
        b2 = subprocess.run([cc, "-std=c11", "-O1", "-w", "-I.", "nvkmd_nvrm_recover_test.c", "-o", "t2"], cwd=d,
                            capture_output=True, text=True)
        run2 = subprocess.run(["./t2"], cwd=d, capture_output=True, text=True) if b2.returncode == 0 else None
        arm("control: the host test fails a header whose plan replays the flush that was running",
            broken != hdr and run2 is not None and run2.returncode != 0)
        # control: and a header that believes "suspended" for ever (no bound on the looking)
        broken = hdr.replace("return now - *since < NVRM_REC_ASLEEP_NS;", "return true;")
        (dd / "nvkmd_nvrm_recover.h").write_text(broken)
        b3 = subprocess.run([cc, "-std=c11", "-O1", "-w", "-I.", "nvkmd_nvrm_recover_test.c", "-o", "t3"], cwd=d,
                            capture_output=True, text=True)
        run3 = subprocess.run(["./t3"], cwd=d, capture_output=True, text=True) if b3.returncode == 0 else None
        arm("control: the host test fails a header that looks again for as long as the GPU stays suspended",
            broken != hdr and run3 is not None and run3.returncode != 0 and "asleep: once" in run3.stdout)

print("FAILED" if fails else "PASS"); sys.exit(1 if fails else 0)
