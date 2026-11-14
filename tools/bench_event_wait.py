#!/usr/bin/env python3
"""Compare NVRM_EVENT_WAIT in disposable processes; never edit driver settings."""

import argparse
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import tempfile


def summarize(runs):
    result = {}
    for mode in ("compute", "image"):
        groups = {}
        for policy in ("poll", "event"):
            selected = [r for r in runs if r["mode"] == mode and r["policy"] == policy]
            if not selected:
                raise ValueError("missing " + mode + "/" + policy + " samples")
            for run in selected:
                for field in ("samples_ms", "samples_cpu_ms"):
                    values = run[field]
                    if len(values) != 25 or not all(math.isfinite(v) and v >= 0 for v in values):
                        raise ValueError("invalid measurement samples")
            groups[policy] = {
                "runs": len(selected),
                "samples": sum(len(r["samples_ms"]) for r in selected),
                "median_wall_ms": statistics.median(statistics.median(r["samples_ms"]) for r in selected),
                "median_cpu_ms": statistics.median(statistics.median(r["samples_cpu_ms"]) for r in selected),
            }
        result[mode] = groups
    return result


def windowserver():
    return subprocess.check_output(["pgrep", "-x", "WindowServer"], text=True).strip()


# The driver counts, in debug.nvrm_evt_clients, the kernel connections that have slept on the GPU completion
# event at least once. The count is sticky per connection (NVRM.cpp, fEvtEverSlept 0 -> 1), so a connection that
# already slept once never increments it again: WindowServer and any other long-running GPU client were counted
# soon after boot and do not move it during a probe. A freshly launched probe opens its own device connection, so
# the count moving across that probe's lifetime is taken as the probe's own. That is how a run proves the wait
# policy it asked for actually took effect, without depending on any line the driver prints. The count is one for
# the whole machine, though: another process whose connection sleeps for the first time during the probe (one that
# starts then, or an older one whose waits were all short until then) moves it too, so a refused poll probe on a
# desktop in use is worth one more run before it is believed.
EVT_CLIENTS_SYSCTL = "debug.nvrm_evt_clients"


def read_evt_clients():
    """The current debug.nvrm_evt_clients, or None when the sysctl is absent (an older kext without it)."""
    try:
        out = subprocess.run(["sysctl", "-n", EVT_CLIENTS_SYSCTL], capture_output=True, text=True)
    except OSError:
        return None
    if out.returncode != 0:
        return None
    text = out.stdout.strip()
    try:
        return int(text)
    except ValueError:
        return None


def verify_policy(policy, before, after):
    """Decide whether a compute/image probe ran under the policy it was asked to. Returns (ok, reason).

    before/after are debug.nvrm_evt_clients around the probe, or None when it could not be read. An `event`
    probe must have made its own connection sleep (the count moves up by at least one); a `poll` probe must not
    have slept at all (the count does not move). When the counter is unavailable the policy cannot be proven,
    which is a failure to verify, not a pass."""
    if before is None or after is None:
        return False, "cannot verify: " + EVT_CLIENTS_SYSCTL + " is not available (needs this driver's kext)"
    delta = after - before
    if delta < 0:
        return False, EVT_CLIENTS_SYSCTL + " went backwards by " + str(-delta) + " (counter reset mid-run)"
    if policy == "event":
        if delta < 1:
            return False, "event-wait probe never slept on the GPU event (" + EVT_CLIENTS_SYSCTL + " did not move)"
        return True, "slept (" + EVT_CLIENTS_SYSCTL + " +" + str(delta) + ")"
    if policy == "poll":
        if delta != 0:
            return False, ("poll probe slept on the GPU event (" + EVT_CLIENTS_SYSCTL + " moved by " + str(delta)
                           + "; another process's first sleep during the probe also moves it: run again on a quiet"
                           + " desktop)")
        return True, "did not sleep (" + EVT_CLIENTS_SYSCTL + " unchanged)"
    return False, "unknown policy " + repr(policy)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="new directory for samples and local logs")
    parser.add_argument("--rounds", type=int, default=5)
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error("--rounds must be positive")
    args.output.mkdir(parents=True, exist_ok=False)
    environment = dict(os.environ)
    for key in list(environment):
        if key.startswith(("DYLD_", "GPU_PROBE_")):
            environment.pop(key)
    runs = []
    image_hash = None
    initial_windowserver = windowserver()

    with tempfile.TemporaryDirectory(prefix="nvmtl-eventwait-") as temporary:
        binary = str(Path(temporary) / "probe")
        subprocess.run([
            "xcrun", "clang", "-O2", "-fobjc-arc",
            str(Path(__file__).with_suffix(".m")),
            "-framework", "Foundation", "-framework", "Metal",
            "-framework", "CoreGraphics", "-framework", "CoreImage", "-o", binary,
        ], check=True, env=environment)

        def probe(label, mode, policy=None):
            nonlocal image_hash
            if windowserver() != initial_windowserver:
                raise RuntimeError("WindowServer restarted; stop before more GPU work")
            command = [binary, mode]
            if policy is not None:
                command.append("NVRM_EVENT_WAIT=" + ("1" if policy == "event" else "0"))
            clients_before = read_evt_clients() if mode in ("compute", "image") else None
            try:
                proc = subprocess.run(command, capture_output=True, text=True, timeout=55, env=environment)
            except subprocess.TimeoutExpired as error:
                (args.output / (label + ".timeout.txt")).write_text(str(error))
                raise RuntimeError("probe timed out; stop before more GPU work") from error
            clients_after = read_evt_clients() if mode in ("compute", "image") else None
            (args.output / (label + ".stderr.txt")).write_text(proc.stderr)
            if proc.returncode or "implemented in both" in proc.stderr or windowserver() != initial_windowserver:
                raise RuntimeError("probe failed or WindowServer restarted; inspect " + label)
            data = json.loads(proc.stdout)
            if mode in ("compute", "image"):
                ok, reason = verify_policy(policy, clients_before, clients_after)
                data.update(evt_clients_before=clients_before, evt_clients_after=clients_after,
                            policy_verified=ok, policy_verification=reason)
                if not ok:
                    raise RuntimeError("requested wait policy was not verified: " + label + ": " + reason)
            if mode in ("compute", "events") and data.get("verified") is not True:
                raise RuntimeError("data verification failed: " + label)
            if mode == "transfer" and not all(t["verified"] for t in data["tests"]):
                raise RuntimeError("transfer verification failed: " + label)
            if mode == "image":
                if not data["alpha_verified"]:
                    raise RuntimeError("image alpha verification failed")
                if image_hash is not None and data["pixel_hash"] != image_hash:
                    raise RuntimeError("image pixels changed between runs")
                image_hash = data["pixel_hash"]
            data.update(policy=policy, windowserver_unchanged=True)
            (args.output / (label + ".json")).write_text(json.dumps(data, indent=2) + "\n")
            print(label + ": passed", flush=True)
            return data

        probe("inventory", "inventory")
        for round_number in range(1, args.rounds + 1):
            policies = ("poll", "event") if round_number % 2 else ("event", "poll")
            for mode in ("compute", "image"):
                for policy in policies:
                    data = probe("{}-{}-{}".format(mode, policy, round_number), mode, policy)
                    data["round"] = round_number
                    runs.append(data)
        for mode in ("events", "transfer"):
            for policy in ("poll", "event"):
                probe(mode + "-" + policy, mode, policy)
        result = {"summary": summarize(runs), "runs": runs}
        (args.output / "comparison.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result["summary"], indent=2))


if __name__ == "__main__":
    main()
