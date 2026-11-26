#!/usr/bin/env python3
# Checks the kext pre-approval the installer records so a first install does not need the user to click Allow in
# System Settings > Privacy & Security before the restart. Builds a stand-in KextPolicy database with macOS's schema,
# runs the same SQL package/install.sh runs, and checks: the driver's kexts end up allowed with exactly one row each,
# an already-allowed driver kext and an unrelated third-party row are left untouched, and install.sh still carries the
# step. SQLite (via Python's module) runs the identical statements macOS's /usr/bin/sqlite3 would, so no Mac is needed
# to catch a schema or syntax regression; whether macOS then loads without a prompt is for the hardware test.
import os
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
INSTALL = os.path.join(HERE, "..", "package", "install.sh")

# macOS's KextPolicy schema (only the columns the installer names matter).
SCHEMA = """
CREATE TABLE kext_policy (team_id TEXT, bundle_id TEXT, allowed INTEGER, developer_name TEXT, flags INTEGER, PRIMARY KEY (team_id, bundle_id));
CREATE TABLE kext_load_history_v3 (path TEXT, team_id TEXT, bundle_id TEXT, boot_uuid TEXT, created_at TEXT, last_seen TEXT, flags INTEGER, cdhash TEXT, PRIMARY KEY (cdhash));
"""

# The driver's own kexts (package/install.sh KEXTS; NVPrimarySelector maps to its own id).
DRIVER_IDS = ["com.nullmoth.NVRM", "com.nullmoth.NVAccel", "com.nullmoth.NVRMFB",
              "com.nullmoth.NVRMAGDC", "org.hackintosh.experimental.NVPrimarySelector"]

# The exact statements install.sh runs (kept in sync with the script; the grep at the end guards the insert).
SELECT_ALLOWED = "select allowed from kext_policy where ifnull(team_id,'')='' and bundle_id=?"
PREAPPROVE = ("insert or replace into kext_policy (team_id,bundle_id,allowed,developer_name,flags) "
              "values ('',?,1,'NullMoth',1)")


def fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def main():
    con = sqlite3.connect(":memory:")
    cur = con.cursor()
    cur.executescript(SCHEMA)

    # Pre-existing state: one driver kext the user already allowed, and an unrelated third-party kext with a team id.
    cur.execute("insert into kext_policy values ('', 'com.nullmoth.NVRM', 1, 'you', 1)")
    cur.execute("insert into kext_policy values ('EXAMPLE', 'com.example.other', 1, 'Other', 8)")

    # The installer's loop: pre-approve any driver kext not already allowed.
    for bid in DRIVER_IDS:
        row = cur.execute(SELECT_ALLOWED, (bid,)).fetchone()
        if (row[0] if row else None) != 1:
            cur.execute(PREAPPROVE, (bid,))
        row = cur.execute(SELECT_ALLOWED, (bid,)).fetchone()
        if (row[0] if row else None) != 1:
            fail(f"{bid} is not allowed after pre-approval")

    # Every driver kext allowed exactly once (insert-or-replace must not duplicate the already-allowed NVRM).
    for bid in DRIVER_IDS:
        n = cur.execute("select count(*) from kext_policy where ifnull(team_id,'')='' and bundle_id=?", (bid,)).fetchone()[0]
        if n != 1:
            fail(f"{bid} has {n} rows, expected 1")

    # The unrelated third-party kext is untouched (same allowed state, team and developer).
    row = cur.execute("select allowed, developer_name from kext_policy where team_id='EXAMPLE' and bundle_id='com.example.other'").fetchone()
    if row != (1, "Other"):
        fail("an unrelated kext row was modified")

    # install.sh still carries the pre-approval write against kext_policy.
    with open(INSTALL, encoding="utf-8") as f:
        text = f.read()
    if "insert or replace into kext_policy" not in text:
        fail("package/install.sh no longer records the kext approval")
    if not re.search(r"select allowed from kext_policy where ifnull\(team_id,''\)=''", text):
        fail("package/install.sh no longer reads kext_policy the way this test mirrors")

    print("PASS: the installer records allowed=1 for the driver's own kexts, once each, leaving other rows untouched")


if __name__ == "__main__":
    main()
