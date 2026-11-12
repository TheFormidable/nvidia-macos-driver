#!/usr/bin/env python3
"""tools/primary_install_fixture.py runs the whole package/install.sh on the machine the tests run on. Its mocks reach
the script only through install.sh's PATH line, and the system folders only through a list of prefixes. Here: the
rewritten script starts its PATH at the mocks and names no system folder outside the fixture, and a PATH line that has
changed stops the fixture instead of letting the real kmutil, nvram and launchctl run.

Negative controls: the prefix list the fixture had before (no /var/db/) leaves the kext approval database at its real
place, and an install.sh whose PATH line has changed is refused."""
import importlib.util
import pathlib
import re
import sys

R = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("installer_fixture", R / "tools/primary_install_fixture.py")
m = importlib.util.module_from_spec(spec)
sys.dont_write_bytecode = True
spec.loader.exec_module(m)
SOURCE = (R / "package/install.sh").read_text()
ROOT = pathlib.Path("/nonexistent/fixture-root")
BIN = ROOT / "bin"
fails = 0


def arm(name, ok, detail=""):
    global fails
    fails += not ok
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f" ({str(detail)[-400:]})"))


def outside(text):
    """Lines of install.sh's own code (not the jobs it writes out) that name a system folder outside the fixture."""
    bad, here = [], None
    for n, line in enumerate(text.splitlines(), 1):
        if here:
            here = None if line.strip() == here else here
            continue
        h = re.search(r"<<-?\s*'?\"?(\w+)", line)
        code = line.split("#", 1)[0] if not line.lstrip().startswith("#!") else ""
        for mt in re.finditer(r"(?<![\w.$}/-])/(Library|System|Users/Shared|var/db)/", code):
            bad.append(f"{n}: {line.strip()}")
            break
        if h:
            here = h.group(1)
    return bad


src = m.private_source(SOURCE, ROOT, BIN)
arm("the installer's PATH starts at the mocks", f"PATH={BIN}:/usr/bin:/bin:/usr/sbin:/sbin; export PATH" in src)
arm("install.sh names no system folder outside the fixture", not outside(src), outside(src))
arm("the kext approval database is the fixture's", f"KPDB={ROOT}/var/db/SystemPolicyConfiguration/KextPolicy" in src)
arm("control: the real install.sh does name system folders", len(outside(SOURCE)) > 3, outside(SOURCE)[:3])
print("     (" + str(len(outside(SOURCE))) + " lines of install.sh name one)")

old = m.private_source(SOURCE, ROOT, BIN, prefixes=["/Library/", "/System/", "/Users/Shared/"])
arm("control: without /var/db/ the real kext approval database stays",
    any("/var/db/SystemPolicyConfiguration/KextPolicy" in b for b in outside(old)), outside(old))

for name, changed in (("one more folder", SOURCE.replace("PATH=/usr/bin:/bin:/usr/sbin:/sbin; export PATH",
                                                         "PATH=/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin; export PATH")),
                      ("written as two lines", SOURCE.replace("PATH=/usr/bin:/bin:/usr/sbin:/sbin; export PATH",
                                                              "PATH=/usr/bin:/bin:/usr/sbin:/sbin\nexport PATH"))):
    try:
        m.private_source(changed, ROOT, BIN)
        arm(f"control: a PATH line {name} is refused", False, "accepted")
    except AssertionError as e:
        arm(f"control: a PATH line {name} is refused", "PATH" in str(e), e)

print(f"{fails} failed")
sys.exit(1 if fails else 0)
