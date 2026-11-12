#!/usr/bin/env python3
"""NVK's half of the surface fence: how nvk/nvk-fence.patch wires kexts/NVRM/nvrm_fence_abi.h into NVK.

The kernel half (nvrm_fence.cpp, the table and its host test) and the plugin half (plugin/nvmtl_fence.h,
tools/test_fence_plugin.c) are tested where they live; the patch's copy of the header by tools/test_shared_headers.py.
This test reads the patch and holds what only NVK's glue can get wrong:

  the chain      nvk-fence is applied by the default 2.9 build, after nvk-evwait and before the channel rebuild
  the seam       the event wait's loop is not edited: only the wait callback's branch for a channel without an event
                 changes, to a WAIT_SEQ on that channel's own semaphore, and only a real sleep or a real completion
                 is reported as one (anything else is a polling step and quiets that channel for a while); the kernel
                 is given the whole milliseconds of the round's time, rounded down, and is not asked for less than
                 one, so the sleep cannot run past the round; and the round's time is cut to what the caller's
                 deadline leaves in every mode, the eager one included (its sleep on the event ignores the deadline
                 as 2.9 did, but 2.9 polled a channel without an event, and polling returned at the deadline)
  registration   every channel that takes a slot is registered: CHAN_ADD, a new probe value, CHAN_VERIFY, and the slot
                 gets its handle only after both; a refused CHAN_VERIFY is followed by CHAN_DEL; a failure leaves the
                 channel running
  teardown       the handle leaves the slot under the mutex, in one helper, and CHAN_DEL is sent after unlocking and
                 before the unregister returns (the caller frees the pages after it)
  flushes        both flush paths publish the flushed value right after counting it, before the doorbell
  the export     nvmtl_nvk_fence_v1 has exactly the signature the plugin calls through, and is the patch's only export
  requests       only QUERY, OPEN, ARM, CLOSE, WAIT pass; every request starts zeroed with version 1; QUERY's active
                 is 0 without asynchronous submission; OPEN is refused while a channel is not watched; ARM resolves the
                 fence under the mutex and arms only a watched channel
  the kext       selector 12 through the header's number, the whole struct back or nothing; one connection opened for
                 the fence alone, at the first request
  the rebuild    the rebind function exists for the channel rebuild to call, is declared in nvkmd_nvrm.h, and on any
                 refusal deletes the registration

Each source check also runs on a damaged copy of the patch text, which must fail it (the controls)."""
import pathlib, re, subprocess, sys

R = pathlib.Path(__file__).resolve().parents[1]
PATCH = R / "nvk/nvk-fence.patch"
SCRIPT = R / "build/build_nvk.sh"
DIR = "src/nouveau/vulkan/nvkmd/nvrm/"
fails = 0


def arm(name, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + name + (f" ({str(detail).strip()[-300:]})" if detail and not ok else ""))
    fails += not ok


def sections(text):
    """{path: (new text, removed text)} of a git diff: the new text is what each hunk leaves (its context and added
    lines; hunks are separated by an empty line), the removed text its '-' lines. The header before the first 'diff '
    is skipped."""
    out, cur = {}, None
    for line in text.splitlines():
        if line.startswith("diff --git "):
            cur = line.split(" b/", 1)[1]
            out[cur] = ([], [])
        elif cur is None or line.startswith(("--- ", "+++ ", "index ", "new file mode", "\\")):
            continue
        elif line.startswith("@@"):
            out[cur][0].append("")
        elif line.startswith(("+", " ")):
            out[cur][0].append(line[1:])
        elif line.startswith("-"):
            out[cur][1].append(line[1:])
        elif line == "":
            out[cur][0].append("")
    return {k: ("\n".join(a), "\n".join(r)) for k, (a, r) in out.items()}


def body(src, name):
    """The text of the C function `name` (its definition): from its name at the start of a line to the first '}'
    alone on a line."""
    m = re.search(r"(?ms)^" + re.escape(name) + r"\(.*?^\}$", src)
    return m.group(0) if m else ""


def fhunks(text, path, func):
    """The new text (context and added lines) of every hunk of `path` that git's hunk header places in function
    `func`, or that contains the line opening its definition, joined in patch order."""
    out, cur, keep = [], None, False
    for line in text.splitlines():
        if line.startswith("diff --git "):
            cur, keep = line.split(" b/", 1)[1], False
        elif cur != path or line.startswith(("--- ", "+++ ", "index ", "new file mode", "\\")):
            continue
        elif line.startswith("@@"):
            keep = line.split("@@")[-1].strip().startswith(func + "(")
            if keep:
                out.append("")
        elif line.startswith(("+", " ")):
            if not keep and line[1:].startswith(func + "("):
                keep = True
                out.append("")
            if keep:
                out.append(line[1:])
    return "\n".join(out).strip("\n")


def order(text, *parts):
    """Every part occurs in text, each after the one before it."""
    at = -1
    for p in parts:
        at = text.find(p, at + 1)
        if at < 0:
            return False
    return True


def ctx_of(t):
    return sections(t).get(DIR + "nvkmd_nvrm_ctx.c", ("", ""))


# --- the checks, each a function of the patch text so that the controls can run them on a damaged copy ----------------
def chk_seam(t):
    s = sections(t)
    add, rem = ctx_of(t)
    w = fhunks(t, DIR + "nvkmd_nvrm_ctx.c", "nvrm_evw_nvk_wait")
    first = body(add, "nvrm_fence_wait_first")
    left = body(add, "nvrm_evw_nvk_left_us")
    # the loop and the waits that call it are not edited: the only ctx.c lines removed are the old branch with its
    # comment, and the line of the registration that now takes the notifier status from a local
    removed = [l.strip() for l in rem.splitlines() if l.strip()]
    loop_untouched = (DIR + "nvrm_evwait.h" not in s and not s.get(DIR + "nvkmd_nvrm_sync.c", ("", ""))[1].strip()
                      and all(l == "return -1;" or l.startswith(("/* No event for the channel", "* channel's own; there"))
                              or l == "s->errBase = (unsigned)errN->status;" for l in removed)
                      and not any(fhunks(t, DIR + p, f) for p, f in (
                          ("nvkmd_nvrm_ctx.c", "nvkmd_nvrm_wait_block_until"), ("nvkmd_nvrm_ctx.c", "nvkmd_nvrm_wait_block"),
                          ("nvkmd_nvrm_ctx.c", "nvrm_gsub_wait_seq"), ("nvkmd_nvrm_sync.c", "nvrm_gsub_wait"))))
    return (loop_untouched
            and order(w, "if (fd < 0)\n      return nvrm_fence_wait_first(c->n, c->slot, c->gen, c->want, "
                         "nvrm_evw_nvk_left_us(c, timeout_us), out);", "nvrm_xnu_wait_event2(fd,")
            and add.count("nvrm_fence_wait_first(c->n,") == 1
            # the time it may sleep: the round's timeout, cut to what the caller's deadline leaves
            and order(left, "const uint64_t left_us = c->remain_ns / 1000;",
                      "return left_us < timeout_us ? (uint32_t)left_us : timeout_us;")
            and left.count("return ") == 1
            and order(first, "nvrm_fence_req(&r, NVRM_FENCE_OP_WAIT_SEQ, 0);", "r.u.wait.id = handle;",
                      "r.u.wait.seq = seq;", "r.u.wait.timeoutMs = timeout_ms;")
            # whole milliseconds of the round's time, rounded down and capped; less than one: no request, no lock
            and order(first, "const uint32_t whole_ms = timeout_us / 1000;",
                      "const uint32_t timeout_ms = whole_ms > NVRM_FENCE_SEQ_MAX_MS ? NVRM_FENCE_SEQ_MAX_MS : whole_ms;",
                      "if (timeout_ms == 0 || __atomic_load_n(&nvrm_fence_have, __ATOMIC_ACQUIRE) <= 0)\n      return -1;",
                      "pthread_mutex_lock(&nvkmd_nvrm_gsub_mtx);")
            and first.count("uint32_t timeout_ms =") == 1 and "999" not in first
            # only READY / NONE (the value reached, confirmed by the semaphore) and BUDGET (slept at least half) count
            and "r.u.wait.result == NVRM_FENCE_R_READY || r.u.wait.result == NVRM_FENCE_R_NONE" in first
            and "__atomic_load_n(s->sem, __ATOMIC_ACQUIRE) >= seq" in first
            and "r.u.wait.result == NVRM_FENCE_R_BUDGET && took >= (int64_t)timeout_ms * 500000" in first
            and first.count("ret = 0;") == 2 and "int ret = -1;" in first
            and "s->kseqQuiet = os_time_get_nano() + NVRM_FENCE_SEQ_QUIET_NS;" in first
            # the kernel is never called with the channel mutex held
            and order(first, "pthread_mutex_unlock(&nvkmd_nvrm_gsub_mtx);", "nvrm_fence_call(&r)",
                      "pthread_mutex_lock(&nvkmd_nvrm_gsub_mtx);"))


def chk_register(t):
    add, _ = ctx_of(t)
    reg = fhunks(t, DIR + "nvkmd_nvrm_ctx.c", "nvrm_gsub_register")
    ca = body(add, "nvrm_fence_chan_add")
    return (order(reg, "sem[NVRM_FENCE_SEM_FLUSHED_OFF / 8] = 0;", "sem[NVRM_FENCE_SEM_PROBE_OFF / 8] = 0;",
                  "s->kchan = 0;", "channel slots in use - this channel stays SYNCHRONOUS", "nvrm_fence_chan_add(ctx, errN, errBase);",
                  "ctx->gsub = true;")
            and order(ca, "__atomic_store_n(&page[NVRM_FENCE_SEM_PROBE_OFF / 8], probe,",
                      "nvrm_fence_req(&r, NVRM_FENCE_OP_CHAN_ADD,", "r.u.chan.semVA = semVA;",
                      "r.u.chan.errBase = errBase;", "const uint32_t handle = r.u.chan.chan;",
                      "__atomic_store_n(&page[NVRM_FENCE_SEM_PROBE_OFF / 8], ~probe,",
                      "nvrm_fence_req(&r, NVRM_FENCE_OP_CHAN_VERIFY, 0);", "r.u.chan.probe = ~probe;",
                      "nvrm_fence_chan_del(handle);", "s->kchan = handle;")
            and ca.count("s->kchan = handle;") == 1 and "NVRM_FENCE_F_CHAN_CE" in ca
            and "(semVA & 0xfff) != 0 || !nvrm_fence_notifier_ok(errVA)" in ca)


def chk_teardown(t):
    add, _ = ctx_of(t)
    un = fhunks(t, DIR + "nvkmd_nvrm_ctx.c", "nvrm_gsub_unregister")
    take = body(add, "nvrm_fence_chan_take")
    # outside the take helper, kchan is only ever set (to a handle at registration, 0 at a new registration), never
    # cleared on its own: a cleared handle always goes to CHAN_DEL
    rest = add.replace(take, "")
    return (order(un, "s->gen++;", "const uint32_t kchan = nvrm_fence_chan_take(s);",
                  "pthread_mutex_unlock(&nvkmd_nvrm_gsub_mtx);", "nvrm_fence_chan_del(kchan);", "ctx->slot1 = 0;")
            and "s->kchan = 0;" in take and "->kchan = 0" not in rest.replace("s->kchan = 0;\n      s->kseqQuiet = 0;\n      ctx->slot1", "")
            and order(body(add, "nvrm_fence_chan_del"), "nvrm_fence_req(&r, NVRM_FENCE_OP_CHAN_DEL, 0);",
                      "r.u.chan.chan = handle;", "nvrm_xnu_fence(fd, &r)"))


def chk_flushed(t):
    add, _ = ctx_of(t)
    hunks = [fhunks(t, DIR + "nvkmd_nvrm_ctx.c", f) for f in ("nvrm_gsub_submit", "nvkmd_nvrm_exec_ctx_flush")]
    a = "ctx->wSeq++;\n   /* surface fence: what this channel owes, published before the GPU can see the work (the doorbell below) */\n"
    sub = a + "   __atomic_store_n(&((uint64_t *)ctx->sem->map)[NVRM_FENCE_SEM_FLUSHED_OFF / 8], ctx->wSeq, __ATOMIC_RELEASE);"
    syn = a + "   __atomic_store_n(&semAdr[NVRM_FENCE_SEM_FLUSHED_OFF / 8], ctx->wSeq, __ATOMIC_RELEASE);"
    return (order(hunks[0], sub, "write_semaphore_release(") and order(hunks[1], syn, "write_semaphore_release(")
            and add.count("NVRM_FENCE_SEM_FLUSHED_OFF / 8], ctx->wSeq") == 2)


SIG = ("uint32_t nvmtl_nvk_fence_v1(VkDevice device, uint32_t op, uint32_t flags, uint64_t cookie, uint32_t count, "
       "const uint32_t *ids, const uint8_t *planes, VkFence fence, uint32_t pid, uint32_t timeout_ms, uint32_t out[6])")


def chk_export(t):
    s = sections(t)
    mem = s.get("src/nouveau/vulkan/nvk_device_memory.c", ("", ""))[0]
    hdr = s.get("src/nouveau/vulkan/nvkmd/nvkmd.h", ("", ""))[0]
    m = re.search(r"__attribute__\(\(visibility\(\"default\"\)\)\) (uint32_t)[^\n]*\n(nvmtl_nvk_fence_v1\([^)]*\))", mem)
    defn = " ".join((m.group(1) + " " + m.group(2)).split()) if m else ""
    d = re.search(r"uint32_t nvmtl_nvk_fence_v1\([^;]*\);", hdr)
    decl = " ".join(d.group(0).split()).rstrip(";") if d else ""
    plus = "\n".join(l for l in t.splitlines() if l.startswith("+"))
    f = body(mem, "nvmtl_nvk_fence_v1")
    return (defn == SIG and decl == SIG
            and plus.count('visibility("default")') == 1
            and "nvmtl_nvk_fence_dropped_v1" not in plus and "nvmtl_nvk_recover_stats_v1" not in plus
            and order(f, "if (op == NVRM_FENCE_OP_ARM && fence != VK_NULL_HANDLE)", "vk_fence_get_active_sync(f);",
                      "return nvkmd_nvrm_fence(dev->nvkmd, op, flags, cookie, count, ids, planes, sync, pid, timeout_ms, out);")
            and "return NVRM_FENCE_ST_OFF;" in f)


def chk_requests(t):
    add, _ = ctx_of(t)
    f = body(add, "nvkmd_nvrm_fence")
    req = body(add, "nvrm_fence_req")
    cases = re.findall(r"case (NVRM_FENCE_OP_\w+):", f)
    sync = sections(t).get(DIR + "nvkmd_nvrm_sync.c", ("", ""))[0]
    tgt = body(sync, "nvkmd_nvrm_sync_fence_target")
    return (sorted(cases) == sorted(["NVRM_FENCE_OP_QUERY", "NVRM_FENCE_OP_OPEN", "NVRM_FENCE_OP_ARM",
                                     "NVRM_FENCE_OP_CLOSE", "NVRM_FENCE_OP_WAIT"])
            and "default:\n      return NVRM_FENCE_ST_BAD_ARG;" in f
            and order(req, "memset(r, 0, sizeof(*r));", "r->version = NVRM_FENCE_ABI_VERSION;")
            and "struct NVRMFenceReq r = " not in add and "struct NVRMFenceReq q = " not in add
            and "out[0] = r.u.query.active && nvkmd_nvrm_async_on() ? 1 : 0;" in f
            and "if (op == NVRM_FENCE_OP_OPEN && !nvrm_fence_all_watched())\n         return NVRM_FENCE_ST_MAP;" in f
            and order(f, "pthread_mutex_lock(&nvkmd_nvrm_gsub_mtx);", "k = nvkmd_nvrm_sync_fence_target(sync, &slot, &gen, &seq);",
                      "s->ctx != NULL && s->gen == gen ? s->kchan : 0;", "pthread_mutex_unlock(&nvkmd_nvrm_gsub_mtx);",
                      "return NVRM_FENCE_ST_STALE;", "armFlags = NVRM_FENCE_F_ARM_DONE;")
            # the kernel's output is read only after OK
            and "nvrm_fence_call(&r) && r.status == NVRM_FENCE_ST_OK && out != NULL" in f
            and f.count("r.status == NVRM_FENCE_ST_OK && out != NULL") == 2
            and "if (s == NULL || !nvrm_gsub_is_binary(s))\n      return -1;" in tgt and "if (s->npend != 1)\n      return -1;" in tgt)


def chk_kext(t):
    s = sections(t)
    xa = s.get(DIR + "nvrm_xnu.c", ("", ""))[0]
    fx = body(xa, "int nvrm_xnu_fence")
    add, _ = ctx_of(t)
    call = body(add, "nvrm_fence_call")
    return ('#include "nvrm_fence_abi.h"' in xa
            and "IOConnectCallStructMethod(conn, NVRM_FENCE_SELECTOR, r, sizeof *r, &out, &outSize);" in fx
            and "if (kr != KERN_SUCCESS || outSize != sizeof out) return -1;" in fx and order(fx, "return -1;", "*r = out;")
            # one connection, opened for the fence at the first request; closed only when the kext has no fence
            and add.count("NVRM_OPEN(") == 1 and order(call, "pthread_mutex_lock(&nvrm_fence_open_mtx);",
                                                       "have = __atomic_load_n(&nvrm_fence_have, __ATOMIC_ACQUIRE);",
                                                       "const int fd = NVRM_OPEN(NVRM_CTL_NODE_NAME);",
                                                       "__atomic_store_n(&nvrm_fence_fd, fd,", "NVRM_CLOSE(fd);",
                                                       "__atomic_store_n(&nvrm_fence_have, have, __ATOMIC_RELEASE);")
            and add.count("NVRM_CLOSE(") == 1 and "ctlFd" not in add and "= nvrm_fence_have;" not in add
            and "r->status = NVRM_FENCE_ST_OFF;\n   return false;" in call)


def chk_rebind(t):
    s = sections(t)
    h = s.get(DIR + "nvkmd_nvrm.h", ("", ""))[0]
    add, _ = ctx_of(t)
    rb = body(add, "nvkmd_nvrm_fence_chan_rebind")
    return ("bool nvkmd_nvrm_fence_chan_rebind(struct nvkmd_nvrm_exec_ctx *ctx);" in h
            and "static bool\nnvkmd_nvrm_fence_chan_rebind(" not in add and "bool\nnvkmd_nvrm_fence_chan_rebind(" in add
            and order(rb, "nvrm_fence_req(&r, NVRM_FENCE_OP_CHAN_REBIND, 0);", "r.u.chan.errVA = errVA;",
                      "r.u.chan.errBase = errBase;", "r.u.chan.chan = handle;",
                      "nvrm_fence_call(&r) && r.status == NVRM_FENCE_ST_OK", "gone = nvrm_fence_chan_take(s);",
                      "pthread_mutex_unlock(&nvkmd_nvrm_gsub_mtx);", "nvrm_fence_chan_del(gone);")
            and "uint32_t kchan;" in h)


patch = PATCH.read_text() if PATCH.exists() else ""
arm("nvk/nvk-fence.patch exists", bool(patch))

# --- the chain -------------------------------------------------------------------------------------------------------
m = re.search(r"(?ms)^case \$REL in\n.*?^esac$", SCRIPT.read_text())
r = subprocess.run(["bash", "-c", 'REL=2.9; NVK=x; FIXES=; CHAIN=; BASE=; eval "$1" >/dev/null; printf "%s" "$CHAIN"', "_",
                    m.group(0)], capture_output=True, text=True) if m else None
chain = r.stdout.split() if r and r.returncode == 0 else []
arm("the default 2.9 chain applies nvk-fence, after nvk-evwait", "nvk-fence" in chain and "nvk-evwait" in chain
    and chain.index("nvk-evwait") < chain.index("nvk-fence"), " ".join(chain[-5:]))
arm("the channel rebuild, which calls the rebind, comes after it", "nvk-channel-recover" not in chain
    or ("nvk-fence" in chain and chain.index("nvk-channel-recover") > chain.index("nvk-fence")))
head = patch.split("\ndiff --git ", 1)[0]
bad = [l for l in head.splitlines() if re.match(r"(\d|--- |\+\+\+ |@@|\*\*\* |Index:|diff |< |> )", l)]
arm("no header line of the patch reads as a diff or an ed command to BSD patch", "\ndiff --git " in patch and not bad, bad)

# --- the source ------------------------------------------------------------------------------------------------------
CHECKS = [
    ("the seam: only the wait callback's branch for a channel without an event changes, to WAIT_SEQ, and only a real "
     "sleep or completion counts", chk_seam, "took >= (int64_t)timeout_ms * 500000", "took >= 0"),
    ("the seam: a hunk inside the wait loops themselves is refused", chk_seam,
     "@@ nvrm_gsub_unregister(struct nvkmd_nvrm_exec_ctx *ctx)", "@@ nvrm_gsub_wait_seq(struct nvkmd_nvrm_exec_ctx *ctx)"),
    ("the seam: a sleep rounded up past the round's time is refused", chk_seam,
     "const uint32_t whole_ms = timeout_us / 1000;", "const uint32_t whole_ms = (timeout_us + 999) / 1000;"),
    ("the seam: a request for less than a millisecond, stretched to one, is refused", chk_seam,
     "if (timeout_ms == 0 || __atomic_load_n(&nvrm_fence_have,", "if (__atomic_load_n(&nvrm_fence_have,"),
    ("the seam: a sleep that is not told the caller's deadline is refused", chk_seam,
     "c->want, nvrm_evw_nvk_left_us(c, timeout_us), out);", "c->want, timeout_us, out);"),
    ("the seam: a cut that keeps the round's timeout whatever the deadline leaves is refused", chk_seam,
     "return left_us < timeout_us ? (uint32_t)left_us : timeout_us;", "return timeout_us;"),
    ("registration: CHAN_ADD, a new probe, CHAN_VERIFY, then the handle; CHAN_DEL after a refused verify", chk_register,
     "+      nvrm_fence_chan_del(handle);\n+      nvrm_fence_say(", "+      nvrm_fence_say("),
    ("teardown: the handle leaves the slot in one helper, CHAN_DEL after unlocking, before the pages go", chk_teardown,
     "+   nvrm_fence_chan_del(kchan);\n    ctx->slot1 = 0;", "    ctx->slot1 = 0;"),
    ("flushes: both paths publish the flushed value before the GPU sees the work", chk_flushed,
     "+   __atomic_store_n(&semAdr[NVRM_FENCE_SEM_FLUSHED_OFF / 8], ctx->wSeq, __ATOMIC_RELEASE);\n", ""),
    ("the export: nvmtl_nvk_fence_v1 has the signature the plugin calls through", chk_export,
     "VkFence fence, uint32_t pid, uint32_t timeout_ms", "VkFence fence, uint32_t timeout_ms, uint32_t pid"),
    ("requests: five ops, zeroed and versioned, output read after OK, OPEN refused while a channel is unwatched",
     chk_requests, "if (op == NVRM_FENCE_OP_OPEN && !nvrm_fence_all_watched())", "if (op == NVRM_FENCE_OP_OPEN && 0)"),
    ("the kext: selector 12 whole or nothing, through one connection opened for the fence", chk_kext,
     "if (kr != KERN_SUCCESS || outSize != sizeof out) return -1;", "if (kr != KERN_SUCCESS) return -1;"),
    ("the rebuild: the rebind is declared for its caller and deletes the registration when refused", chk_rebind,
     "   nvrm_fence_chan_del(gone);", "   (void)gone;"),
]
for name, fn, good, broken in CHECKS:
    more = name.startswith("the seam: a ")      # further controls of the seam check: it runs on the patch itself once
    if not more:
        arm(name, fn(patch))
    damaged = patch.replace(good, broken)
    arm("control: " + (name.split(":")[1].strip() if more else name.split(":")[0])
        + " - a damaged copy fails",
        damaged != patch and not fn(damaged), "the damage did not apply" if damaged == patch else "")

print("FAILED" if fails else "PASS"); sys.exit(1 if fails else 0)
