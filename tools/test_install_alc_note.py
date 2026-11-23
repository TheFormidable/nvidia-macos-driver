#!/usr/bin/env python3
"""The installer names AppleALC's alcid/alcverbs on macOS 26. There is no AppleHDA there, and NMXAudio leaves an
onboard controller alone while either is set ("another codec stack is switched on (alcid/alcverbs)"), so the Mac had
only the NVIDIA display outputs (a user's RTX 5050 on macOS 26.7.3 with alcid=66, 2026-11-23)."""
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
src = (ROOT / "package/install.sh").read_text()
fails = 0


def arm(name, ok, detail=""):
    global fails
    fails += not ok
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f" ({detail.strip()[-300:]})"))


arm("install.sh defines alc_note once", src.count("alc_note() {") == 1, str(src.count("alc_note() {")))
arm("and calls it only when NMXAudio is installed", 'case " $KEXTS " in *" NMXAudio "*) alc_note "$ba";; esac' in src)
arm("after the audio driver is chosen", src.index('KEXTS="$KEXTS NMXAudio"') < src.index('alc_note "$ba"'))
start = src.index("alc_note() {")
fn = src[start:src.index("\n}\n", start) + 3]


def note(ba):
    p = subprocess.run(["/bin/bash", "-c", fn + 'alc_note "$1"', "x", ba], capture_output=True, text=True, timeout=30)
    return p.stdout + p.stderr


field = "keepsyms=1 nvfb=1 nvaccel=1 nvfbheads=4 -nvkmsnosmooth amfi_get_out_of_my_way=0x1 amfi=0x80 alcid=66 revpatch=sbvmm"
for ba in (field, "alcid=1", "nvfb=1 alcverbs=1", "-alcverbs nvfb=1"):
    out = note(ba)
    arm(f"'{ba}' gets the note", "alcid" in out and "config.plist" in out, out)
for ba in ("", "nvfb=1 nvaccel=1 amfi=0x80", "revpatch=sbvmm -nvkmsnosmooth", "xalcid=1 nvfb=1"):
    out = note(ba)
    arm(f"negative control: '{ba}' gets no note", out.strip() == "", out)
sys.exit(1 if fails else 0)
