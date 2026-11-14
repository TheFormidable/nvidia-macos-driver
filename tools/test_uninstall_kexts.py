#!/usr/bin/env python3
"""uninstall.sh removes every kext of this driver, the opt-in hybrid laptop panel drivers included. A panel driver it
did not list was counted as another product's kext: it was copied into the removal collection, and that collection's
com.nullmoth check then stopped the removal on every hybrid laptop that had one. That check now looks for the bundle
identifiers of the kexts uninstall.sh removes only, so another com.nullmoth kext it does not remove (NMDRM, NMHDA,
NMSMN, NMKbdBacklight) left in /Library/Extensions no longer stops the removal, while each kext of this driver still
does. The AMD and Intel GPU driver's accelerator (NMXAccel) names this driver's Metal plugin, which the removal takes
away, so it still stops the removal, at a check of its own that says why.

Reads package/uninstall.sh and install.sh. The checks and the functions they call are lifted from uninstall.sh and run
by bash with PATH holding only the test's own folder, which has grep and nothing else, and with a stand-in die; lifted
text that names a path, sets PATH or asks for `command -p` is refused rather than run.
   python3 tools/test_uninstall_kexts.py [tree-root]      (pass an older tree to see the arms fail)"""
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
un = (root / "package/uninstall.sh").read_text(encoding="utf-8")
inst = (root / "package/install.sh").read_text(encoding="utf-8")
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


# ---- the removal collection's two checks refuse this driver's identifiers, and only those
def function(text, name):
    i = text.find(f"\n{name}() {{\n")
    return text[i + 1:text.index("\n}\n", i) + 3] if i >= 0 else ""


def checks(text):
    return [l.strip() for l in text.split("\n")
            if '&& die "the removal collection still lists' in l or '&& die "the kernel collection still lists' in l]


def accel_checks(text):
    return [l.strip() for l in text.split("\n") if "&& die \"the AMD and Intel GPU driver's accelerator" in l]


arm("uninstall.sh names the kexts' identifiers as install.sh does (primary_bundle_id, the same text)",
    function(un, "primary_bundle_id") != ""
    and function(un, "primary_bundle_id") == function(inst, "primary_bundle_id"))
arm("the removal collection is checked before it is published, and the published one after", len(checks(un)) == 2,
    checks(un))
accel = accel_checks(un)
arm("the removal collection is also checked for the AMD and Intel GPU driver's accelerator, once, before anything is "
    "removed", len(accel) == 1 and len(checks(un)) == 2
    and un.find(checks(un)[0]) < un.find(accel[0]) < un.find("\nREMOVING=1\n"), accel)


class Refused(Exception):
    pass


def run(text, listing, lines, kext_list=None):
    """For each of `lines`, lifted from `text`: the stand-in die's message where that line stops the removal when
    kmutil inspect prints `listing`, None where it lets the removal go on."""
    lifted = function(text, "primary_bundle_id") + function(text, "lists_driver")
    for part in [lifted] + lines:
        if "/" in part or re.search(r"\bPATH=", part) or re.search(r"\bcommand\s+-[A-Za-z]*p", part):
            raise Refused(f"the lifted text names a path or reaches past PATH: {part[:80]!r}")
    head = f'KEXTS="{" ".join(kexts(text) if kext_list is None else kext_list)}"\n{lifted}'
    head += 'die() { echo "DIE: $*"; exit 3; }\nINS=$1; FINAL_INS=$1\n'
    out = []
    with tempfile.TemporaryDirectory() as t:
        (Path(t) / "grep").symlink_to(shutil.which("grep"))
        for c in lines:
            p = subprocess.run(["/bin/bash", "-c", head + c + "\necho kept\n", "x", listing], cwd=t,
                               env={"PATH": t, "LC_ALL": "C"}, capture_output=True, text=True, timeout=30)
            if (p.returncode, p.stdout.startswith("DIE: ")) not in ((0, False), (3, True)):
                raise Refused(f"a check ended in an unexpected way: {p.returncode} {p.stdout}{p.stderr}")
            out.append(p.stdout[len("DIE: "):].strip() if p.returncode == 3 else None)
    return out


def refused(text, listing, kext_list=None):
    """[removal check, published check]: True where that check, lifted from `text`, stops the removal when kmutil
    inspect prints `listing`."""
    return [m is not None for m in run(text, listing, checks(text), kext_list)]


def listed(*bundle_ids):   # a listing with text on both sides of each identifier; the checks read only identifiers
    return "\n".join(f"  {b} (3.0.0) 2B1E4C5A-0000-4000-8000-00000000000{n}" for n, b in enumerate(bundle_ids))


# the identifier install.sh's primary_bundle_id gives each kext of the list (the arm above holds uninstall.sh to it)
own = ["org.hackintosh.experimental.NVPrimarySelector" if k == "NVPrimarySelector" else "com.nullmoth." + k
       for k in kexts(un)]
built = {}
for p in (root / "kexts").rglob("Info*.plist"):
    d = plistlib.loads(p.read_bytes())
    built.setdefault(d.get("CFBundleExecutable"), set()).add(d.get("CFBundleIdentifier"))
arm("each kext of the list that this tree builds has that identifier in its Info.plist (NMXAudio is not built here)",
    all(built.get(k, {i}) == {i} for k, i in zip(kexts(un), own))
    and [k for k in kexts(un) if k not in built] == ["NMXAudio"],
    {k: built.get(k) for k in kexts(un)})
OTHERS = ("com.nullmoth.NMDRM", "com.nullmoth.NMHDA", "com.nullmoth.NMSMN", "com.nullmoth.NMKbdBacklight",
          "com.nullmoth.NMXGPU", "com.nullmoth.NMXNPU", "as.vit9696.Lilu")
try:
    arm("a com.nullmoth kext the uninstaller does not remove (NMDRM, NMHDA, NMSMN, NMKbdBacklight, NMXGPU, NMXNPU) "
        "does not stop the removal", refused(un, listed(*OTHERS)) == [False, False]
        and all(m is None for m in run(un, listed(*OTHERS), accel)))
    nmx = run(un, listed(*OTHERS, "com.nullmoth.NMXAccel"), accel)
    arm("the AMD and Intel GPU driver's accelerator (NMXAccel) in the removal collection stops it, saying why",
        len(nmx) == 1 and nmx[0] is not None and "NMXAccel" in nmx[0] and "Metal plugin" in nmx[0]
        and "Nothing was changed" in nmx[0], nmx)
    arm("and only under its own name: com.nullmoth.NMXAccelHeadless is not com.nullmoth.NMXAccel",
        run(un, listed("com.nullmoth.NMXAccelHeadless"), accel) == [None])
    # control, made from the text: with that line taken out, an NMXAccel listing goes through every check left
    no_accel = "\n".join(l for l in un.split("\n") if l.strip() not in accel)
    arm("control: without that line the checks of this driver's identifiers let an NMXAccel listing through",
        no_accel != un and accel_checks(no_accel) == []
        and refused(no_accel, listed(*OTHERS, "com.nullmoth.NMXAccel")) == [False, False])
    arm("each kext of this driver in the collection still stops it, at both checks",
        all(refused(un, listed(*OTHERS, i)) == [True, True] for i in own), own)
    arm("an identifier counts only as a whole word: com.nullmoth.NVRMFB is not com.nullmoth.NVRM",
        refused(un, listed("com.nullmoth.NVRMFB"), ["NVRM"]) == [False, False]
        and refused(un, listed("com.nullmoth.NVRM"), ["NVRM"]) == [True, True])
    arm("an empty listing stops nothing", refused(un, "") == [False, False])
    # control, made from the text: the removal check put back in its old form, any com.nullmoth identifier
    old = "\n".join(l[:l.index(l.strip())] + "printf '%s\\n' \"$INS\" | grep -Eq 'com\\.nullmoth|org\\.hackintosh"
                    "\\.experimental\\.NVPrimarySelector'" + l[l.index(" && die"):]
                    if '&& die "the removal collection still lists' in l else l for l in un.split("\n"))
    arm("control: the old form of the check stops the removal on the same listing",
        refused(old, listed(*OTHERS))[0] is True)
except Refused as e:
    arm("the lifted checks could be run inside the test's folder", False, str(e))
print(f"{len(fails)} failed")
sys.exit(1 if fails else 0)
