#!/usr/bin/env python3
"""uninstall.sh removes every kext of this driver, the opt-in hybrid laptop panel drivers included. A panel driver it
did not list was counted as another product's kext: it was copied into the removal collection, and that collection's
com.nullmoth check then stopped the removal on every hybrid laptop that had one. Reads package/uninstall.sh.
   python3 tools/test_uninstall_kexts.py [tree-root]      (pass an older tree to see the arms fail)"""
import re
import sys
from pathlib import Path

root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
un = (root / "package/uninstall.sh").read_text(encoding="utf-8")
fails = []


def arm(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f" ({detail})"))
    if not ok:
        fails.append(name)


def kexts(text):
    m = re.search(r'^KEXTS="([^"]*)"', text, re.M)
    return m.group(1).split() if m else []


PANELS = ("NMIntelFB", "NMIntelFBVirtual", "NMGopFB")
arm("the uninstaller's list still holds the NVIDIA driver, its selector and its audio driver",
    all(k in kexts(un) for k in ("NVRM", "NVAccel", "NVRMFB", "NVRMAGDC", "NVPrimarySelector", "NMXAudio")), kexts(un))
arm("and the hybrid panel drivers NMIntelFB, its virtual test build and NMGopFB",
    all(k in kexts(un) for k in PANELS), kexts(un))
no_virtual = re.sub(r'^(KEXTS="[^"]*) NMIntelFBVirtual\b', r"\1", un, count=1, flags=re.M)
arm("control: a list without the virtual test build is caught",
    no_virtual != un and not all(k in kexts(no_virtual) for k in PANELS))
# a Mac that has none of them: the backup and the rollback skip a kext that is not there, and rm -rf of a missing
# folder succeeds, so a longer list changes nothing on it
arm("the backup skips a listed kext that is not installed",
    'for k in $KEXTS; do [ ! -e "$EXT/$k.kext" ] || ditto "$EXT/$k.kext" "$RB/$k.kext"' in un)
arm("the rollback puts back only what the backup holds",
    'for k in $KEXTS; do [ ! -e "$RB/$k.kext" ] || ditto "$RB/$k.kext" "$EXT/$k.kext"' in un)
arm("the removal collection is built without any listed kext",
    un.count('n=$(basename "$x" .kext); if [[ " $KEXTS " == *" $n "* ]]; then continue; fi') == 2)
print(f"{len(fails)} failed")
sys.exit(1 if fails else 0)
