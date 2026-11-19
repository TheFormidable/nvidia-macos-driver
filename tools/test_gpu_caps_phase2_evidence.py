#!/usr/bin/env python3
"""Host-only regression checks: evidence never grants fan control."""
import importlib.util
import pathlib
import tempfile
import json
import subprocess
import sys
import unittest

P = pathlib.Path(__file__).with_name("gpu_caps_phase2_evidence.py")
spec = importlib.util.spec_from_file_location("gpu_caps_phase2_evidence", P)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

def sample(i, ts):
    return {"rusd_sample": i, "fan": {"ok": 1, "ts": hex(ts), "rpm": [1150+i, 1149+i]+[0]*14},
            "temp": [{"ok": 1, "ts": hex(ts), "raw": 53*256+i},
                     {"ok": 1, "ts": hex(ts), "raw": 76*256} ],
            "limit": {"ok": 1, "ts": hex(ts), "enforced_mw": 130000, "requested_mw": 130000}}

class Phase2(unittest.TestCase):
    def setUp(self):
        self.rows = [
            {"probe": "nvcaps", "rev": 1, "mode": "card"},
            {"q": "PCI_IDS_V2", "transport": 0, "status": "0x0",
             "device_id": "0x00002d83", "subsystem_id": "0x8a3f1043"},
            {"q": "GSP_FEATURES", "transport": 0, "status": "0x0",
             "valid": 1, "firmware_version": "620.70.06"},
            {"q": "THERMAL", "transport": 0, "status": "0x0", "instructions": []},
            {"q": "FAN_BLOCK", "transport": 0, "status": "0x0",
             "matches_two_records": True, "is_auto": True},
            sample(0, 100), sample(1, 200), sample(2, 300)
        ]
    def test_real_read_only_evidence(self):
        r = mod.report(self.rows)
        self.assertEqual(r["fan"]["tach_slot_count"], 2)
        self.assertEqual(r["fan"]["tach_slots"][0]["slot"], 0)
        self.assertEqual(r["power"]["limit"]["last"]["enforced_mw"], 130000)
        self.assertEqual(r["firmware"]["gsp"]["version"], "620.70.06")
        self.assertFalse(r["manual_authorized"])
        self.assertFalse(r["fan"]["private_min_max_are_proven"])
        self.assertEqual(r["fan"]["fan_stop"], "unknown")
    def test_stale_or_unfilled_rusd_never_verified(self):
        rows = self.rows[:5] + [sample(0, 10), sample(1, 10)]
        r = mod.report(rows)
        self.assertEqual(r["fan"]["tach_slot_count"], 0)
        self.assertNotIn("gpu", r["thermal"]["temperatures"])
        rows[-1]["fan"]["ts"] = "0x0"
        self.assertEqual(mod.report(rows)["fan"]["tach_slot_count"], 0)
    def test_privileged_or_unproved_never_authorized(self):
        self.rows[4]["matches_two_records"] = False
        self.assertFalse(mod.report(self.rows)["fan"]["private_auto_layout_observed"])
        self.assertFalse(mod.report(self.rows)["manual_authorized"])
    def test_failed_control_does_not_verify_firmware(self):
        self.rows[2]["status"] = "0xdead"
        self.assertNotIn("gsp", mod.report(self.rows)["firmware"])
    def test_mock_rejected(self):
        self.rows[0]["mode"] = "mock"
        with self.assertRaises(ValueError):
            mod.report(self.rows)
    def test_cli_rejects_non_json(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d)/"invalid.jsonl"
            path.write_text("not json\n")
            run = subprocess.run([sys.executable, str(P), str(path)], capture_output=True)
            self.assertNotEqual(run.returncode, 0)
    def test_cli_produces_json(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d)/"probe.jsonl"
            path.write_text("".join(json.dumps(r)+"\n" for r in self.rows))
            run = subprocess.run([sys.executable, str(P), str(path)], capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads(run.stdout)["fan"]["tach_slot_count"], 2)

if __name__ == "__main__":
    unittest.main()
