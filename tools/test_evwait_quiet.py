#!/usr/bin/env python3
"""The controls of the event wait's temporary fallback (kexts/NVRM/nvrm_evwait.h, NVRM_EVW_FB_QUIET).

After NVRM_EVW_MISS_LIMIT missed wakes a process polls, but each thread tries the event again after a pause that it
counts in its own waits, and the first answer that proves events arrive puts the process back on them. Only a kernel
wait call that fails keeps the process polling for good. tools/test_evwait_loop.c holds six invariants of that in arms
named "invariant 1:" to "invariant 6:":

  1  a round is one polling step or one sleep of at most NVRM_EVW_MAX_SLEEP_US, and a wait ends only with its work
  2  events that stop and come back (the threaded stand-in kernel): sleeping on events again within the pause and trial
  3  events that never come back: over a million waits 4 trial sleeps per trial with pauses doubling to the cap, and
     one FELL_BACK note
  4  a failed wait call: polling for good, never a trial
  5  two threads: one brings the events back; the other, which tripped the rule, sleeps again and one missed wake does
     not trip it
  6  a count remembered from before the quiet time that moved does not end it: only a trial's answer or a wake does

This test builds that program against the header and requires every arm to pass; then, for each invariant, against a
copy of the header with the rule that keeps it taken out, and requires that invariant's arm to fail. A control whose
damage does not apply (the header's text moved) fails too, so a rewrite of the header must bring its controls along.

The flag is shared by the process's threads without a lock. A text arm holds that the only plain store to it is DEAD
and that the two changes that depend on what it holds (0 to QUIET, QUIET to 0) are compare-and-swaps, so that a DEAD
stored by another thread between the read and the write is never overwritten or cleared; no single-threaded run can
show that interleaving, so its control is a copy of the header with the old read-then-store clear."""
import pathlib, re, shutil, subprocess, sys, tempfile

R = pathlib.Path(__file__).resolve().parents[1]
HDR = R / "kexts/NVRM/nvrm_evwait.h"
LOOP = R / "tools/test_evwait_loop.c"
fails = 0


def arm(name, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + name + (f" ({str(detail).strip()[-1200:]})" if detail and not ok else ""))
    fails += not ok


# (invariant, what is taken out, [(text in the header, replacement)])
CONTROLS = [
    ("invariant 1", "a round that polls takes no polling step while the process is QUIET",
     [("remain_ns, flag)) { ops->backoff(ctx, waited_ns); return; }",
       "remain_ns, flag)) { if (!flag) ops->backoff(ctx, waited_ns); return; }")]),
    ("invariant 2", "no trial: a QUIET thread never tries the event again",
     [("    if (t->quiet_waits < (t->pause ? t->pause : NVRM_EVW_PAUSE_FIRST)) return flag;\n",
       "    return flag;\n")]),
    ("invariant 3", "a failed trial does not double the pause",
     [("t->pause = p >= NVRM_EVW_PAUSE_MAX / 2u ? NVRM_EVW_PAUSE_MAX : 2u * p;", "t->pause = p;")]),
    ("invariant 3", "missed wakes never end a trial",
     [("++t->trial_misses >= NVRM_EVW_TRIAL_MISSES)", "++t->trial_misses >= 0xffffffffu)")]),
    ("invariant 3", "a failed trial is noted to the kernel as another fallback",
     [("nvrm_evw_trial_failed(t);   /* not another FELL_BACK",
       "nvrm_evw_trial_failed(t); ops->stat(ctx, NVRM_EVW_S_FELL_BACK);   /* not another FELL_BACK")]),
    ("invariant 4", "a failed wait call makes the process QUIET instead of DEAD",
     [("    *fell_back = NVRM_EVW_FB_DEAD;\n", "    *fell_back = NVRM_EVW_FB_QUIET;\n")]),
    ("invariant 5", "the run of missed wakes is not restarted when a thread trips the rule or polls while QUIET",
     [("            t->misses = 0;\n            int was = 0;\n", "            int was = 0;\n"),
      ("    t->misses = 0;   /* a polling thread misses nothing", "    /* a polling thread misses nothing")]),
    ("invariant 6", "a MOVED answer outside a trial clears QUIET (a count remembered from before the quiet time)",
     [("    const int now = proof && (t->trial || why == NVRM_EVW_R_WOKEN);\n", "    const int now = proof;\n")]),
    ("invariant 6, capped or aborted", "a capped or aborted answer whose count moved clears QUIET outside a trial",
     [("    const int now = proof && (t->trial || why == NVRM_EVW_R_WOKEN);\n",
       "    const int now = proof && (t->trial || why != NVRM_EVW_R_MOVED);\n")]),
]


def build_and_run(hdr_text, d, tag):
    inc = pathlib.Path(d) / tag
    inc.mkdir()
    (inc / "nvrm_evwait.h").write_text(hdr_text)
    exe = inc / "loop"
    b = subprocess.run([CC, "-std=c11", "-O2", "-pthread", "-w", "-I", str(inc), str(LOOP), "-o", str(exe)],
                       capture_output=True, text=True)
    if b.returncode != 0:
        return None, b.stderr
    r = subprocess.run([str(exe)], capture_output=True, text=True, timeout=300)
    return r, r.stdout


def flag_writes_ok(h):
    """The process flag: one plain store (DEAD), and a compare-and-swap for each of 0 -> QUIET and QUIET -> 0."""
    stores = re.findall(r"\*fell_back\s*=(?!=)\s*([^;]+);", h)
    cas = re.findall(r"__atomic_compare_exchange_n\(\s*fell_back\s*,\s*&was\s*,\s*(\w+)\s*,", h)
    inits = re.findall(r"\bint was = (\w+);", h)
    return stores == ["NVRM_EVW_FB_DEAD"] and sorted(cas) == ["0", "NVRM_EVW_FB_QUIET"] and \
        sorted(inits) == ["0", "NVRM_EVW_FB_QUIET"]


CC = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
if CC is None:
    arm("a C compiler to build tools/test_evwait_loop.c", False, "no cc, clang or gcc on PATH")
    print("FAILED"); sys.exit(1)
hdr = HDR.read_text()
src = LOOP.read_text()
arm("the flag: DEAD is the only plain store; 0 -> QUIET and QUIET -> 0 are compare-and-swaps", flag_writes_ok(hdr))
old_clear = ("        if (*fell_back == NVRM_EVW_FB_QUIET) { *fell_back = 0; ops->stat(ctx, NVRM_EVW_S_BACK); }\n")
new_clear = ("        int was = NVRM_EVW_FB_QUIET;\n        if (*fell_back == NVRM_EVW_FB_QUIET &&\n"
             "            __atomic_compare_exchange_n(fell_back, &was, 0, 0, __ATOMIC_RELAXED, __ATOMIC_RELAXED))\n"
             "            ops->stat(ctx, NVRM_EVW_S_BACK);\n")
arm("control: the flag - a clear that reads QUIET and then stores 0 (a DEAD in between is lost) fails",
    hdr.count(new_clear) == 1 and not flag_writes_ok(hdr.replace(new_clear, old_clear)))
with tempfile.TemporaryDirectory() as d:
    r, out = build_and_run(hdr, d, "good")
    arm("tools/test_evwait_loop.c builds against the header and every arm passes", r is not None and r.returncode == 0
        and "\nall passed" in out, out)
    for n in range(1, 7):
        arm(f"the program has an arm for invariant {n}, and it passes", f'T("invariant {n}:' in src
            and f"ok   invariant {n}:" in out,
            " | ".join(l for l in out.splitlines() if f"invariant {n}:" in l or l.startswith("     ")))
    for i, (inv, what, edits) in enumerate(CONTROLS):
        damaged = hdr
        applied = all(damaged.count(a) == 1 for a, _ in edits)
        for a, b in edits:
            damaged = damaged.replace(a, b)
        if not applied:
            arm(f"control: {inv} - {what}", False, "the damage does not apply: the header's text moved")
            continue
        r, out = build_and_run(damaged, d, f"c{i}")
        arm(f"control: {inv} - {what}: its arm fails", r is not None and f"FAIL {inv}:" in out, out)

print("FAILED" if fails else "PASS"); sys.exit(1 if fails else 0)
