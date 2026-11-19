#!/usr/bin/env python3
"""Phase 2 read-only discovery from nvcaps_probe JSONL (never writes to RM).
This is an evidence report, NOT an authorization for GPU fan writes or an
in-kernel producer. Inputs are untrusted, status/validity are checked per row.
"""
import argparse
import json
import pathlib
import sys

MAX_INPUT = 8 * 1024 * 1024
MAX_LINES = 256
MAX_RPM = 30000

def ok_row(row):
    return (isinstance(row, dict) and row.get("transport") == 0
            and row.get("status") == "0x0")

def nonzero_timestamp(row):
    try:
        return int(row.get("ts", "0"), 0) > 0
    except (ValueError, TypeError):
        return False

def fresh_section(row):
    return isinstance(row, dict) and row.get("ok") == 1 and nonzero_timestamp(row)

def report(records):
    header = next((x for x in records if x.get("probe") == "nvcaps"), None)
    if header is None or header.get("rev") != 1 or header.get("mode") != "card":
        raise ValueError("missing real-hardware nvcaps rev=1 header (mock data refused)")
    # Probe JSONL uses published command names, not ROW_LIST enum tags.
    # Match exact command IDs; never infer capability from an arbitrary label.
    cmd_alias = {
        "0x2080183c": "PCI_IDS_V2", "0x20801801": "PCI_IDS",
        "0x20801701": "ARCH", "0x20800110": "NAME",
        "0x20800111": "SHORT_NAME", "0x20800808": "SKU",
        "0x20800810": "VBIOS", "0x20800156": "INFOROM",
        "0x20803601": "GSP_FEATURES", "0x20800513": "THERMAL",
        "0x20808530": "FAN_BLOCK",
    }
    rows = {}
    for x in records:
        name = x.get("q")
        if not isinstance(name, str):
            continue
        cmd = x.get("cmd")
        if cmd is not None:
            name = cmd_alias.get(cmd)
            if name is None:
                continue
        if isinstance(name, str):
            rows.setdefault(name, []).append(x)
    def selected(name, pred=lambda x: True):
        return next((x for x in rows.get(name, ()) if ok_row(x) and pred(x)), None)
    result = {
        "schema": "gpu-caps-phase2-evidence/1", "transport": "read_only",
        "manual_authorized": False, "integration": "userspace_evidence_only",
        "hardware": {}, "firmware": {}, "thermal": {}, "fan": {}, "power": {},
        "warnings": []
    }
    pci = selected("PCI_IDS_V2") or selected("PCI_IDS")
    if pci:
        result["hardware"]["device_id"] = pci.get("device_id")
        result["hardware"]["subsystem_id"] = pci.get("subsystem_id")
        result["hardware"]["revision_id"] = pci.get("revision_id")
    for name, fields in (
        ("ARCH", ("architecture", "implementation", "revision")),
        ("NAME", ("name",)), ("SHORT_NAME", ("short_name",)),
        ("SKU", ("board_id", "project", "project_sku", "chip_sku")),
        ("VBIOS", ("revision", "oem_revision")),
        ("INFOROM", ("inforom_image_version",)),
    ):
        row = selected(name)
        if row:
            dest = result["firmware"] if name in ("VBIOS", "INFOROM") else result["hardware"]
            dest[name.lower()] = {k: row[k] for k in fields if k in row}
    gsp = selected("GSP_FEATURES", lambda x: x.get("valid") == 1)
    if gsp:
        result["firmware"]["gsp"] = {
            "version": gsp.get("firmware_version"), "features": gsp.get("gsp_features"),
            "source": "published_RM"
        }
    else:
        result["warnings"].append("GSP firmware version not independently verified")
    th = [x for x in rows.get("THERMAL", ()) if ok_row(x)]
    if th:
        result["thermal"]["published_instructions"] = [
            {"note": x.get("note"), "successful": x.get("successful"),
             "instructions": [
                 {"op": i.get("op"), "result": i.get("result"), "executed": i.get("executed"),
                  "w": i.get("w")}
                 for i in x.get("instructions", []) if isinstance(i, dict)
             ]} for x in th
        ]
    samples = [x for x in records if isinstance(x.get("rusd_sample"), int)]
    # Require at least two distinct successful timestamps. A single sampled,
    # nonzero field is not evidence of continuous hardware telemetry.
    def active(xs):
        valid = [x for x in xs if fresh_section(x)]
        return valid if len({x["ts"] for x in valid}) >= 2 else []
    fan_samples = active([s.get("fan") for s in samples])
    tach = []
    if fan_samples:
        for slot in range(16):
            vals = [s["rpm"][slot] for s in fan_samples
                    if isinstance(s.get("rpm"), list) and len(s["rpm"]) > slot
                    and type(s["rpm"][slot]) is int]
            if len(vals) >= 2 and all(0 < v < MAX_RPM for v in vals):
                tach.append({"slot": slot, "min_observed_rpm": min(vals),
                             "max_observed_rpm": max(vals), "source": "RUSD",
                             "status": "verified_readback"})
    result["fan"]["tach_slots"] = tach
    result["fan"]["tach_slot_count"] = len(tach)
    result["fan"]["cooler_count"] = None  # one tach slot is not necessarily one cooler
    result["fan"]["manual_control"] = "unknown_not_authorized"
    result["fan"]["fan_stop"] = "unknown"
    # Private records: describe only an observed layout, not a write capability
    private = [x for x in rows.get("FAN_BLOCK", ()) if ok_row(x)
               and x.get("matches_two_records") is True and x.get("is_auto") is True]
    result["fan"]["private_auto_layout_observed"] = bool(private)
    if private:
        result["fan"]["private_layout_confidence"] = "experimental"
        result["fan"]["private_min_max_are_proven"] = False
    temp = {}
    for index, label in enumerate(("gpu", "memory")):
        items = active([s.get("temp", [])[index] for s in samples
                        if isinstance(s.get("temp"), list) and len(s["temp"]) > index])
        values = [x.get("raw") for x in items if type(x.get("raw")) is int]
        if len(values) >= 2:
            temp[label] = {"source": "RUSD", "raw_min": min(values),
                           "raw_max": max(values), "raw_scale": "1/256 C",
                           "status": "verified_readback",
                           "published_thermal_target_confirmed": label == "gpu" and bool(th)}
    result["thermal"]["temperatures"] = temp
    for key, fields in (("limit", ("requested_mw", "enforced_mw")),
                        ("tgp", ("mw",)), ("avg", ("gpu_mw",)),
                        ("inst", ("gpu_mw",))):
        items = active([s.get(key) for s in samples])
        values = [{f: x[f] for f in fields if type(x.get(f)) is int and x[f] >= 0}
                  for x in items]
        if values and all(len(v) == len(fields) for v in values):
            result["power"][key] = {"source": "RUSD", "last": values[-1],
                                    "status": "verified_readback"}
    result["warnings"].append(
        "Thermal limits, manual fan writes, fan-stop thresholds and min/max RPM remain unknown")
    return result

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("capture", type=pathlib.Path, help="real nvcaps_probe JSONL")
    p.add_argument("--output", type=pathlib.Path, help="write evidence report to file")
    args = p.parse_args(argv)
    try:
        if args.capture.stat().st_size > MAX_INPUT:
            raise ValueError("capture exceeds 8 MiB")
        records = []
        with args.capture.open(encoding="utf-8") as f:
            for line in f:
                if len(records) >= MAX_LINES:
                    raise ValueError("too many JSONL records")
                if line.strip():
                    x = json.loads(line)
                    if not isinstance(x, dict):
                        raise ValueError("each JSONL line must be an object")
                    records.append(x)
        result = report(records)
        data = json.dumps(result, indent=2, sort_keys=True) + "\n"
        if args.output:
            args.output.write_text(data, encoding="utf-8")
        else:
            sys.stdout.write(data)
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError) as e:
        print(f"GPU Caps phase2: {e}", file=sys.stderr)
        return 2
    return 0

if __name__ == "__main__":
    sys.exit(main())
