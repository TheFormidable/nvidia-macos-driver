#!/usr/bin/env python3
"""Read-only signature and load-policy diagnostics for NullMoth on macOS.
Never changes boot arguments, NVRAM, system services or the installed driver.
A successful codesign check is not a WindowServer authorization check.
"""
import argparse
import json
import pathlib
import platform
import re
import subprocess
import sys

DEFAULT = "/Library/GPUBundles/NVMTLDriver.bundle"

def run(args):
    try:
        p = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=25)
        return {"rc": p.returncode, "output": (p.stdout + p.stderr).strip()}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"rc": None, "output": str(exc)}

def classify_log(lines):
    # Do not report raw lines: unified logs can contain private identifiers.
    plugin = "nvmtldriver"
    rejection = 0
    metal_failures = 0
    for line in lines:
        low = line.lower()
        if plugin in low and ("library validation failed" in low or
                              "mapping process is a platform binary" in low):
            rejection += 1
        if "failed to create metaldevice" in low:
            metal_failures += 1
    return {"plugin_validation_messages": rejection,
            "metal_device_failure_messages": metal_failures}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", default=DEFAULT)
    parser.add_argument("--log", type=pathlib.Path, help="optional saved unified log")
    args = parser.parse_args()
    if platform.system() != "Darwin":
        parser.error("run on macOS")
    bundle = pathlib.Path(args.bundle).resolve()
    binary = bundle / "Contents/MacOS/NVMTLDriver"
    signature = run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(bundle)])
    details = run(["/usr/bin/codesign", "-d", "--verbose=4", str(binary)])
    boot = run(["/usr/sbin/sysctl", "-n", "kern.bootargs"])
    flags = boot["output"].split() if boot["rc"] == 0 else []
    amfi_flags = [v for v in flags if v.startswith(("amfi=", "amfi_get_out_of_my_way="))]
    deps = run(["/usr/bin/otool", "-L", str(binary)])
    report = {
        "schema": 1,
        "os": run(["/usr/bin/sw_vers", "-buildVersion"]),
        "bundle": str(bundle),
        "signature_integrity": signature,
        "signature_metadata": details,
        "dependencies": deps,
        "amfi_arguments": amfi_flags,
        "amfi_arguments_known": boot["rc"] == 0,
        "amfipass_beta_requested": "-amfipassbeta" in flags,
        "windowserver_authorization": "not established",
        "note": "Ad hoc, Developer ID and notarization alone do not establish platform-binary trust.",
    }
    if args.log:
        with args.log.open(encoding="utf-8", errors="replace") as f:
            report["log_evidence"] = classify_log(f)
    print(json.dumps(report, indent=2))
    return 0 if signature["rc"] == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
