#!/usr/bin/env python3
"""NVK's half of the event wait: how nvk/nvk-evwait.patch wires the rules of kexts/NVRM/nvrm_evwait.h into NVK.

The rules themselves are tested by tools/test_evwait_policy.c and tools/test_evwait_loop.c, and the patch's copy of the
header by tools/test_shared_headers.py. This test reads the patch and holds what only the glue can get wrong:

  the chain     nvk-evwait is applied by the default 2.9 build, after the upstream-fix patches, and before the later
                patches that rewrite the same wait
  one round     NVK's wait round is the header's nvrm_evw_round with four callbacks; the fence wait (vkWaitForFences)
                passes the time left before its deadline, the channel's own wait none, and both go through the same
                function, so a later patch changes one place
  the seam      the wait callback declines a channel without an event (fd < 0): exactly the polling of 2.9 until a
                later patch puts a kernel wait of the channel's own there
  the setting   NVRM_EVENT_WAIT is read with nvmtl_cfg_getenv (environment, then nvrm610.conf: a release plugin removes
                inherited NVRM_ variables, so the file is the way to set it), once, through nvrm_evw_mode_parse, with
                WindowServer named
  the kext      selector 6 is read whole (count and reason) through the header's selector number; the 2.9 call still
                returns the count alone for the synchronous flush, whose sleep is not touched; selector 9 carries the
                notes; an old kext's answer that came back at once with nothing posted is not taken as a sleep
  no sleep      asked to sleep, a kext that says why and answers "count read", "moved" or "woken" with the count it
                was given did not sleep and saw no event: the round polls one step instead of asking again at once
  the counters  seven slots appended after the eight of 2.9 (the plugin reads the first nine by position), one per
                NVRM_EVW_S_*, each mapped to its own slot, and printed with the others

Each source check also runs on a damaged copy of the patch text, which must fail it (the controls)."""
import pathlib, re, subprocess, sys

R = pathlib.Path(__file__).resolve().parents[1]
PATCH = R / "nvk/nvk-evwait.patch"
SCRIPT = R / "build/build_nvk.sh"
DIR = "src/nouveau/vulkan/nvkmd/nvrm/"
fails = 0


def arm(name, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + name + (f" ({str(detail).strip()[-300:]})" if detail and not ok else ""))
    fails += not ok


def sections(text):
    """{path: (new text, removed text)} of a git diff: the new text is what each hunk leaves (its context and added
    lines; hunks are separated by an empty line), the removed text its '-' lines. The header text before the first
    'diff ' is skipped."""
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
    """The text of the C function `name` (its definition, not a call): from its name at the start of a line to the
    first '}' alone on a line."""
    m = re.search(r"(?ms)^" + re.escape(name) + r"\(.*?^\}$", src)
    return m.group(0) if m else ""


def order(text, *parts):
    """Every part occurs in text, each after the one before it."""
    at = -1
    for p in parts:
        at = text.find(p, at + 1)
        if at < 0:
            return False
    return True


# --- the checks, each a function of the patch text so that the controls can run them on a damaged copy ----------------
def chk_round(t):
    s = sections(t)
    ctx = s.get(DIR + "nvkmd_nvrm_ctx.c", ("", ""))[0]
    until = body(ctx, "nvkmd_nvrm_wait_block_until")
    old = body(ctx, "nvkmd_nvrm_wait_block")
    return ("nvrm_evw_round(&nvrm_evw_thr," in until and "&nvrm_evw_fell_back, &ops, &c)" in until
            and all(f".{k} = nvrm_evw_nvk_{k}," in until for k in ("pending", "wait", "backoff", "stat"))
            and "nvkmd_nvrm_wait_block_until(waited_ns, UINT64_MAX, n, slot, gen, want);" in old
            and "nvrm_xnu_wait_event(" not in old + until)


def chk_deadline(t):
    s = sections(t)
    add, rem = s.get(DIR + "nvkmd_nvrm_sync.c", ("", ""))
    return ("abs_timeout_ns == UINT64_MAX ? UINT64_MAX : abs_timeout_ns - (uint64_t)t" in add
            and "nvkmd_nvrm_wait_block_until(t - t0, remain, nh, hslot, hgen, hseq);" in add
            and "nvkmd_nvrm_wait_block(t - t0, nh, hslot, hgen, hseq);" in rem)


def chk_seam(t):
    ctx = sections(t).get(DIR + "nvkmd_nvrm_ctx.c", ("", ""))[0]
    w = body(ctx, "nvrm_evw_nvk_wait")
    i, j = w.find("if (fd < 0)\n      return -1;"), w.find("nvrm_xnu_wait_event2(fd,")
    # nvrm_evwait_pending is 2.9's: a pending copy-engine channel makes it return true with fd -1 (it is not skipped)
    return (0 <= i < j and "c->refused = true;" in w
            and "return pending;   /* pending with *fd < 0: only channels without an event (copy engine)" in ctx)


def chk_setting(t):
    ctx = sections(t).get(DIR + "nvkmd_nvrm_ctx.c", ("", ""))[0]
    m = body(ctx, "nvrm_evw_nvk_mode")
    return ('nvrm_evw_mode_parse(nvmtl_cfg_getenv("NVRM_EVENT_WAIT"), pn != NULL && strcmp(pn, "WindowServer") == 0)' in m
            and "__atomic_store_n(&mode, m," in m and 'getenv("NVRM_EVENT_WAIT")' not in m.replace("cfg_getenv", ""))


def chk_kext(t):
    s = sections(t)
    xa, xr = s.get(DIR + "nvrm_xnu.c", ("", ""))
    w2 = body(xa, "int nvrm_xnu_wait_event2")
    w1 = body(xa, "int64_t nvrm_xnu_wait_event")
    note = body(xa, "void nvrm_xnu_evt_note")
    ctx_add, ctx_rem = s.get(DIR + "nvkmd_nvrm_ctx.c", ("", ""))
    w = body(ctx_add, "nvrm_evw_nvk_wait")
    return ('#include "nvrm_evwait.h"' in xa
            and "IOConnectCallScalarMethod(conn, NVRM_EVW_SEL_WAIT, in, 2, &o, &n)" in w2 and "*out = o;" in w2
            and "(int64_t)nvrm_evw_seq(out)" in w1 and "IOConnectCallScalarMethod(conn, NVRM_EVW_SEL_NOTE, &in, 1, NULL, NULL)" in note
            # the synchronous flush keeps its 2.9 sleep: no line of it is removed
            and "nvrm_flush_sleep" not in ctx_rem
            # an old kext's immediate answer with nothing posted is a polling step, and only outside WindowServer
            and "why == NVRM_EVW_R_LEGACY && timeout_us != 0 && c->mode == NVRM_EVW_ADAPT" in w
            and "*out = nvrm_evw_pack(seen, NVRM_EVW_R_SPURIOUS);" in w)


NO_SLEEP = ("} else if (timeout_us != 0 && nvrm_evw_seq(*out) == seen &&\n"
            "              (why == NVRM_EVW_R_COUNT || why == NVRM_EVW_R_MOVED || why == NVRM_EVW_R_WOKEN)) {")


def chk_nosleep(t):
    ctx = sections(t).get(DIR + "nvkmd_nvrm_ctx.c", ("", ""))[0]
    w = body(ctx, "nvrm_evw_nvk_wait")
    # three answers are rewritten to "a wake without an event" (the header then polls one step), in this order: a reason
    # this file does not know, a named reason that is neither a sleep nor a new event, an old kext's immediate answer
    return (order(w, "if (why > NVRM_EVW_R_SPURIOUS) {", "NVRM_EVW_R_SPURIOUS);", NO_SLEEP,
                  "*out = nvrm_evw_pack(seen, NVRM_EVW_R_SPURIOUS);", "} else if (why == NVRM_EVW_R_LEGACY &&",
                  "*out = nvrm_evw_pack(seen, NVRM_EVW_R_SPURIOUS);", "return 0;")
            and w.count("NVRM_EVW_R_SPURIOUS);") == 3 and w.count("return 0;") == 1)


GS = ["SLEEPS", "WOKEN", "TIMEOUT", "MISSED", "CAPPED", "FALLBACK", "CHAN_SLEEP"]
EVW = ["SLEEP", "WOKEN", "TIMEOUT", "MISSED", "CAPPED", "FELL_BACK", "CHAN_SLEEP"]


def chk_counters(t):
    s = sections(t)
    h = s.get(DIR + "nvkmd_nvrm.h", ("", ""))[0]
    names = re.findall(r"NVRM_GS_\w+", re.sub(r"(?s)/\*.*?\*/", "", h))
    ctx = s.get(DIR + "nvkmd_nvrm_ctx.c", ("", ""))[0]
    table = re.search(r"(?s)nvrm_evw_nvk_counter\[NVRM_EVW_S_N\] = \{(.*?)\};", ctx)
    pairs = re.findall(r"\[NVRM_EVW_S_(\w+)\] = NVRM_GS_EVW_(\w+)", table.group(1)) if table else []
    return (names[:8] == ["NVRM_GS_SUBMIT", "NVRM_GS_ACQUIRE", "NVRM_GS_CPUWAIT", "NVRM_GS_SLOTWAIT", "NVRM_GS_FIFOWAIT",
                          "NVRM_GS_DEFER", "NVRM_GS_REAPED", "NVRM_GS_WAITS"]
            and names[8:] == ["NVRM_GS_EVW_" + g for g in GS] + ["NVRM_GS_N"]
            and sorted(pairs) == sorted(zip(EVW, GS))
            and "_Static_assert(NVRM_EVW_S_N == 7," in ctx
            and all(f"st[1 + NVRM_GS_EVW_{g}]" in ctx for g in GS))


patch = PATCH.read_text() if PATCH.exists() else ""
arm("nvk/nvk-evwait.patch exists", bool(patch))

# --- the chain -------------------------------------------------------------------------------------------------------
m = re.search(r"(?ms)^case \$REL in\n.*?^esac$", SCRIPT.read_text())
r = subprocess.run(["bash", "-c", 'REL=2.9; NVK=x; FIXES=; CHAIN=; BASE=; eval "$1" >/dev/null; printf "%s" "$CHAIN"', "_",
                    m.group(0)], capture_output=True, text=True) if m else None
chain = r.stdout.split() if r and r.returncode == 0 else []
arm("the default 2.9 chain applies nvk-evwait, after nvk-nak-sched-main", "nvk-evwait" in chain
    and "nvk-nak-sched-main" in chain and chain.index("nvk-nak-sched-main") < chain.index("nvk-evwait"), " ".join(chain[-5:]))
arm("the patches that rewrite the same wait come after it", all(chain.index(p) > chain.index("nvk-evwait")
    for p in ("nvk-fence", "nvk-channel-recover") if p in chain and "nvk-evwait" in chain))
head = patch.split("\ndiff --git ", 1)[0]
bad = [l for l in head.splitlines() if re.match(r"(\d|--- |\+\+\+ |@@|\*\*\* |Index:|diff |< |> )", l)]
arm("no header line of the patch reads as a diff or an ed command to BSD patch", "\ndiff --git " in patch and not bad, bad)

# --- the source ------------------------------------------------------------------------------------------------------
CHECKS = [
    ("one round: nvkmd_nvrm_wait_block_until runs nvrm_evw_round with NVK's four callbacks; the old name is it without "
     "a deadline", chk_round, "nvrm_evw_round(&nvrm_evw_thr,", "nvrm_evw_round_(&nvrm_evw_thr,"),
    ("vkWaitForFences passes the time left before its deadline (none for an infinite wait)", chk_deadline,
     "nvkmd_nvrm_wait_block_until(t - t0, remain,", "nvkmd_nvrm_wait_block_until(t - t0, UINT64_MAX,"),
    ("the seam: a channel without an event (pending, fd -1) is declined by the wait before any kernel call",
     chk_seam, "+   if (fd < 0)\n+      return -1;", "+   if (fd < -1)\n+      return -1;"),
    ("NVRM_EVENT_WAIT is read through nvrm610.conf, once, with WindowServer named", chk_setting,
     'nvmtl_cfg_getenv("NVRM_EVENT_WAIT")', 'getenv("NVRM_EVENT_WAIT")'),
    ("selector 6 is read whole and selector 9 carries the notes; the flush keeps the 2.9 call; an old kext's immediate "
     "answer polls, outside WindowServer", chk_kext, "c->mode == NVRM_EVW_ADAPT", "c->mode != NVRM_EVW_POLL"),
    ("no sleep: an answer to a request to sleep that names neither a sleep nor a new event polls one step", chk_nosleep,
     "(why == NVRM_EVW_R_COUNT || why == NVRM_EVW_R_MOVED || why == NVRM_EVW_R_WOKEN)", "(why == NVRM_EVW_R_COUNT)"),
    # a second control of the same check: the rule is only for a request to sleep whose count came back unchanged
    ("no sleep, for any count: a rule that also rewrites a count read or a moved count", chk_nosleep,
     "} else if (timeout_us != 0 && nvrm_evw_seq(*out) == seen &&\n+              (why == NVRM_EVW_R_COUNT",
     "} else if (\n+              (why == NVRM_EVW_R_COUNT"),
    ("seven counters after the eight of 2.9, one per NVRM_EVW_S_*, each in its own slot, all printed", chk_counters,
     "[NVRM_EVW_S_MISSED] = NVRM_GS_EVW_MISSED", "[NVRM_EVW_S_MISSED] = NVRM_GS_EVW_TIMEOUT"),
]
checked = set()
for name, fn, good, broken in CHECKS:
    if fn not in checked:      # a check with two controls is run on the patch itself once, under its first name
        arm(name, fn(patch))
        checked.add(fn)
    damaged = patch.replace(good, broken)
    arm("control: " + name.split(":")[0].split(";")[0] + " - a damaged copy fails",
        damaged != patch and not fn(damaged), "the damage did not apply" if damaged == patch else "")

print("FAILED" if fails else "PASS"); sys.exit(1 if fails else 0)
