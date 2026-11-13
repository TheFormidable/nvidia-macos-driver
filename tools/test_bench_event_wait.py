#!/usr/bin/env python3
"""Stand-in test for the event-wait benchmark's policy check (tools/bench_event_wait.py).

The real benchmark needs a Mac, a GPU and the driver, so it cannot run here. What it decides, however, is pure:
given the driver's sticky "connections that have slept on the event" counter before and after a probe, did the
probe run under the wait policy it was asked for? This test drives that decision with stand-in counter readings
for both answers (verified and not verified) and both policies, plus the controls that keep the check honest."""

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("bench_event_wait", Path(__file__).with_name("bench_event_wait.py"))
_bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bench)
verify_policy = _bench.verify_policy

CASES = [
    # (name, policy, before, after, expect_ok)
    # verified: an event probe's own connection slept, so the counter moved up by one
    ("event probe slept -> verified", "event", 40, 41, True),
    # verified: the counter may jump by more than one (a probe that opens several connections)
    ("event probe, several new sleepers -> verified", "event", 40, 43, True),
    # not verified: an event probe that never slept (e.g. the kext silently fell back to polling)
    ("event probe that never slept -> not verified", "event", 40, 40, False),
    # verified: a poll probe leaves the counter where it was
    ("poll probe did not sleep -> verified", "poll", 40, 40, True),
    # not verified: a poll probe whose connection slept (the policy did not take effect)
    ("poll probe slept -> not verified", "poll", 40, 41, False),
    # control: the counter is unreadable (older kext) -> cannot prove either policy
    ("counter unavailable (event) -> not verified", "event", None, None, False),
    ("counter unavailable (poll) -> not verified", "poll", None, None, False),
    ("counter half-read (event) -> not verified", "event", 40, None, False),
    # control: the counter went backwards (a reset mid-run) -> not trusted for either policy
    ("counter reset (event) -> not verified", "event", 40, 2, False),
    ("counter reset (poll) -> not verified", "poll", 40, 2, False),
]


def main():
    failures = 0
    for name, policy, before, after, expect_ok in CASES:
        ok, reason = verify_policy(policy, before, after)
        status = "ok  " if ok == expect_ok else "FAIL"
        if ok != expect_ok:
            failures += 1
        print("{} {} (ok={}, reason={!r})".format(status, name, ok, reason))

    # Negative control: a check that passed an event probe on an unchanged counter would be wrong; prove the
    # function does not, i.e. the "verified" answers really depend on the counter moving the right way.
    broken_ok, _ = verify_policy("event", 40, 40)
    if broken_ok:
        print("FAIL negative control: an event probe that did not sleep was wrongly accepted")
        failures += 1
    else:
        print("ok   negative control: an event probe that did not sleep is not accepted")

    if failures:
        raise SystemExit("{} case(s) failed".format(failures))
    print("\nall passed")


if __name__ == "__main__":
    main()
