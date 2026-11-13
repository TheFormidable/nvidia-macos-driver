#!/usr/bin/env python3
"""What debug.nvrm_bh_max_us measures, as kexts/NVRM/NVRM.cpp describes it.

The bottom half (rm_isr_bh) runs with the GPU lock that rm_isr took and handed over when it asked for the bottom half
(as on Linux); it never waits for that lock. The counter list above the bottom-half sysctls used to say the longest
bottom half included "its wait for the GPU lock", which sent a reader of a high value looking for lock contention that
cannot be there. Asserted here:
  - the code the counter times is the run itself: nvrmRunBottomHalf takes its start time first, then calls rm_isr_bh,
    and updates gNvBhMaxUs from that start; nothing in it takes a lock;
  - the comment of debug.nvrm_bh_max_us says the run holds the lock rm_isr handed over and names the counter for the
    wait for the thread (debug.nvrm_bh_wake_max_us, which exists), and no longer claims a wait for the GPU lock.
Negative controls: the old comment, and a timer started after rm_isr_bh, must fail.
Usage: test_bh_counter_comment.py [driver checkout]"""
import pathlib
import re
import sys

root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1])


def arms(src):
    out = []
    arm = lambda name, ok: out.append((name, bool(ok)))
    m = re.search(r"static void nvrmRunBottomHalf\s*\(\s*nv_state_t\s*\*\s*nv\s*\)\s*\{(.*?)\n\}", src, re.S)
    run = m.group(1) if m else ""
    t0, bh, mx = run.find("const uint64_t t0 = mach_absolute_time();"), run.find("rm_isr_bh(NULL, nv);"), run.find("if (us > gNvBhMaxUs) gNvBhMaxUs = us;")
    arm("the counter times the bottom half's own run, with no lock taken in it",
        0 <= t0 < bh < mx and "nvrmUsSince(t0)" in run and not re.search(r"\b(?:IOLock|IOSimpleLock|lck_)\w*\s*\(", run))
    lines = re.findall(r"^//   debug\.nvrm_bh_max_us .*(?:\n//    {20,}.*)*", src, re.M)
    text = " ".join(" ".join(l.replace("//", " ").split()) for l in lines)
    arm("the comment says what is measured: the run, with the lock rm_isr handed over, and where the wait for the thread is",
        len(lines) == 1 and "lock rm_isr handed over" in text and "no wait for that lock" in text and "nvrm_bh_wake_max_us" in text
        and "wait for the GPU lock included" not in text and "SYSCTL_QUAD(_debug, OID_AUTO, nvrm_bh_wake_max_us," in src)
    return out


def broken(src, old, new):
    assert src.count(old) == 1, "control text not found once: " + old
    return src.replace(old, new)


OLD = "//   debug.nvrm_bh_max_us           longest bottom half, its wait for the GPU lock included\n"
CONTROLS = [
    ("the old comment", "the comment says", lambda s: re.sub(r"^//   debug\.nvrm_bh_max_us .*(?:\n//    {20,}.*)*\n", lambda m: OLD, s, count=1, flags=re.M)),
    ("a timer started after the bottom half", "the counter times",
     lambda s: broken(broken(s, "    const uint64_t t0 = mach_absolute_time();\n    OSIncrementAtomic(&gNvBhActive);\n", "    OSIncrementAtomic(&gNvBhActive);\n"),
                      "    rm_isr_bh(NULL, nv);\n", "    rm_isr_bh(NULL, nv);\n    const uint64_t t0 = mach_absolute_time();\n")),
]


def main():
    src = (root / "kexts/NVRM/NVRM.cpp").read_text()
    fails = 0
    for name, ok in arms(src):
        print(("ok   " if ok else "FAIL ") + name)
        fails += 0 if ok else 1
    for name, piece, mutate in CONTROLS:
        try:
            res = arms(mutate(src))
        except AssertionError as e:
            print("FAIL control '%s': %s" % (name, e))
            fails += 1
            continue
        hit = [a for a, ok in res if not ok and piece in a]
        print(("ok   " if hit else "FAIL ") + "control: %s is caught" % name)
        fails += 0 if hit else 1
    print("%d failed" % fails)
    return 1 if fails else 0


sys.exit(main())
