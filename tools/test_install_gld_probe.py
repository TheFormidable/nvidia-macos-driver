#!/usr/bin/env python3
"""The installer asks macOS's trusted GPU-bundle lookup whether it finds the installed native OpenGL bundle
(tools/gld_bundle_probe.c, installed in nvmtl/zink). A bundle the lookup refuses would leave every OpenGL application
without a driver, so the install stops and rolls back; NVMTLDriver.bundle, which Metal finds through the same lookup,
is the control, and when the lookup does not find it either the installer only notes that it could not check. This
test takes that block out of package/install.sh and runs it with stand-in probes for the three cases. It runs no real
probe and changes nothing on the machine.
usage: test_install_gld_probe.py"""
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
src = (ROOT / "package/install.sh").read_text()
fails = 0


def arm(name, ok, detail=""):
    global fails
    fails += not ok
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f" ({str(detail).strip()[-400:]})"))


arm("the installer requires the probe in the package before it changes anything",
    '[ -f "$HERE/Library/GPUBundles/nvmtl/zink/gld_bundle_probe" ]' in src
    and src.index("nvmtl/zink/gld_bundle_probe") < src.index("INSTALLING=1"))
start = src.find('ZPROBE="$GB/nvmtl/zink/gld_bundle_probe"\n')
end = src.find("\nfi\n", start)
arm("install.sh has the probe block once", start > 0 and end > start and src.count('ZPROBE="$GB/') == 1)
arm("the probe runs while a stop still rolls the install back (after INSTALLING=1, before INSTALLING=0)",
    start > src.find("INSTALLING=1;") > 0 and src.find("\nINSTALLING=0", start) > start)
arm("the probe runs after the bundle is copied and its permissions are set",
    start > src.find('chmod -R go-w,a+rX "$GB/NVMTLDriver.bundle" "$GB/nvmtl" "$GB/$ZINK_GB"') > 0)
block = src[start:end + 4] if start > 0 and end > start else ""
# the block runs below with stand-ins and an empty PATH; anything that could reach past them (a PATH of its own,
# command -p, a program or a system folder named by full path) is refused before it runs
reach = re.search(r'PATH=|\bcommand\s+-[A-Za-z]*p|(?:^|[^\w.$}-])/(?:usr/|opt/)?s?bin/'
                  r'|(?:^|[^\w.$}-])/(?:Library|System|Users|var/db)/', block, re.M)
arm("the probe block reaches nothing past its stand-ins (no PATH=, no command -p, no program or system folder by "
    "full path)", bool(block) and not reach, reach and reach.group(0))
arm("the kexts' new hashes go into macOS's approval record only after the probe block, "
    "so a refused OpenGL bundle leaves it unchanged",
    src.find("update kext_load_history_v3", end) > end > start
    and src.count("update kext_load_history_v3") == 1 and src.find("KPDB=") > end)


def run(control, bundle):
    with tempfile.TemporaryDirectory(prefix="gldprobe-") as t:
        gb = pathlib.Path(t)
        probe = gb / "nvmtl/zink/gld_bundle_probe"
        probe.parent.mkdir(parents=True)
        probe.write_text("#!/bin/bash\ncase \"$1\" in NVMTLDriver) exit %d;; NVKZinkGLDriver) exit %d;; esac\nexit 2\n"
                         % (control, bundle))
        probe.chmod(0o755)
        script = ('die() { echo "STOP: $*"; exit 7; }\nGB="$1"; BK=/backup; ZINK_GB=NVKZinkGLDriver.bundle\n'
                  + block + 'echo "went on"\n')
        p = subprocess.run(["/bin/bash", "-c", script, "x", str(gb)], env={"PATH": str(gb / "no-programs")},
                           capture_output=True, text=True, timeout=30)
        return p.returncode, p.stdout + p.stderr


if block and not reach:
    rc, out = run(0, 0)
    arm("the lookup finds both bundles: the install goes on", rc == 0 and "went on" in out, out)
    rc, out = run(0, 1)
    arm("the lookup finds NVMTLDriver.bundle but refuses the OpenGL bundle: the install stops with the restore note",
        rc == 7 and "macOS does not trust the installed OpenGL bundle" in out and "/backup" in out
        and "went on" not in out, out)
    rc, out = run(1, 1)
    arm("the lookup does not answer for NVMTLDriver.bundle either: a note, and the install goes on",
        rc == 0 and "note:" in out and "was not checked" in out and "went on" in out, out)
    rc, out = run(1, 0)
    arm("control: with NVMTLDriver.bundle unanswered the OpenGL bundle is not judged", rc == 0 and "note:" in out, out)

print(f"{fails} failed")
sys.exit(1 if fails else 0)
