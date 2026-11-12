#!/usr/bin/env python3
"""build/kext_check.sh checks what the build scripts build, with the flags they use, against both kernels.

The check compiles every kext source without a Mac, so it carries its own copy of the compiler flags. A copy drifts:
a define added to a build script and not to the check would leave the check compiling different code. This holds the
two together. It also holds the one thing a compile cannot show: a new kexts/NVRM source that build/build_nvrm.sh does
not name is left out of the kext (a .c) or stops the build on the Mac (a .cpp), and nothing on any other machine said
so. kext_check.sh --sources answers that without any input, so it is run here on the tree and on copies with a source
added, where it has to fail.

It shows the import classifier fires (tools/kext_imports.py --selftest: a Private-only symbol, a symbol one kernel
exports and the other does not), and keeps the two edits that let the check prepare the NVIDIA tree without compiling it.
Usage: test_kext_check.py [driver checkout]"""
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1]
FAILS = []


def arm(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


chk = (ROOT / "build/kext_check.sh").read_text()
imp = (ROOT / "tools/kext_imports.py").read_text()
FLAG = re.compile(r"(?<![\w-])(-(?:f[a-z][a-z-]+|mkernel|mno-[a-z-]+|std=[\w+]+|nostdinc|D[A-Za-z_]\w*))(?![\w-])")
# flags that only change paths recorded in the object (reproducible builds): no effect on what compiles
PATH_ONLY = {"-fdebug-prefix-map", "-ffile-prefix-map"}
CHECKED = ["build/build_nvrm.sh", "build/accel_build.sh", "build/fb_build.sh", "build/build_agdc.sh", "build/build_primary_selector.sh",
           "kexts/NMDRM/build.sh", "kexts/NMGopFB/build.sh", "kexts/NMIntelFB/build.sh", "kexts/NMSMN/build.sh"]


def missing(script_text, check_text):
    body = "\n".join(l for l in script_text.splitlines() if not l.lstrip().startswith("#"))
    return sorted(f for f in set(FLAG.findall(body)) - PATH_ONLY if f not in check_text)


for rel in CHECKED:
    p = ROOT / rel
    m = missing(p.read_text(), chk) if p.is_file() else ["(the script is gone)"]
    arm(f"every compile flag of {rel} is in the check", not m, " ".join(m))
arm("control: a flag the check does not have is caught", missing('clang++ -fapple-kext -DNEW_THING -c x.cpp', chk) == ["-DNEW_THING"])
arm("control: a flag in a comment is not a flag", missing('# clang++ -DNEW_THING\nclang++ -fapple-kext -c x.cpp', chk) == [])

# every kext with a build script is either compiled by the check or named as not checked
built = {p.parent.name for p in (ROOT / "kexts").glob("*/build.sh")} | {"NVRM", "NVAccel", "NVRMFB", "NVRMAGDC", "NVPrimarySelector"}
named = {k for k in built if re.search(rf"\b{k}\b", chk)}
arm("every kext is compiled by the check or named in it as not checked", built == named, " ".join(sorted(built - named)))
# ... and its objects are read by the import check under a plist that is in the tree
plists = dict(re.findall(r'^    "([^"]+)": \(r"[^"]+", "([^"]+)"\),$', imp, re.M))
arm("the import check knows a kext for every object the check compiles",
    len(plists) >= 11 and all((ROOT / p).is_file() for p in plists.values()), " ".join(p for p in plists.values() if not (ROOT / p).is_file()))
stems = set(re.findall(r'^one "[^"]+" [\w-]+ ([\w-]+) ', chk, re.M))
pats = re.findall(r'^    "[^"]+": \(r"([^"]+)", ', imp, re.M)
orphan = sorted(s for s in stems if not any(re.search(p, s + ".o") for p in pats))
arm("every object the check compiles is one the import check reads", len(stems) >= 10 and not orphan, " ".join(orphan))


# the sources of NVRM.kext: the real script, on the tree and on copies of its three inputs
def sources(edit=None):
    """-> (exit status, output) of kext_check.sh --sources on a copy of the tree's NVRM source names and build_nvrm.sh."""
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        (d / "build").mkdir(); (d / "kexts/NVRM").mkdir(parents=True)
        for f in ("kext_check.sh", "build_nvrm.sh"):
            shutil.copy(ROOT / "build" / f, d / "build" / f)
        for p in list((ROOT / "kexts/NVRM").glob("*.cpp")) + list((ROOT / "kexts/NVRM").glob("*.c")):
            (d / "kexts/NVRM" / p.name).write_text("")     # the names are what is checked
        if edit:
            edit(d)
        r = subprocess.run(["bash", str(d / "build/kext_check.sh"), "--sources"], capture_output=True, text=True)
        return r.returncode, r.stdout + r.stderr


def bn_edit(old, new):
    def f(d):
        p = d / "build/build_nvrm.sh"; s = p.read_text()
        assert old in s, old
        p.write_text(s.replace(old, new, 1))
    return f


r = subprocess.run(["bash", str(ROOT / "build/kext_check.sh"), "--sources"], capture_output=True, text=True)
arm("every kexts/NVRM/*.cpp is in CXX_SOURCES and every *.c is compiled by build/build_nvrm.sh", r.returncode == 0, (r.stdout + r.stderr)[-400:])
rc, out = sources()
arm("the same on a copy of the names (the controls below start from a passing state)", rc == 0, out[-300:])
rc, out = sources(lambda d: (d / "kexts/NVRM/nvrm_new_thing.cpp").write_text(""))
arm("control: a new .cpp that CXX_SOURCES does not name is caught, and the line says what to do",
    rc != 0 and "nvrm_new_thing.cpp is not in CXX_SOURCES" in out and "add it to the list" in out, out[-300:])
rc, out = sources(lambda d: (d / "kexts/NVRM/nvrm_new_thing.c").write_text(""))
arm("control: a new .c that build_nvrm.sh compiles nowhere is caught",
    rc != 0 and "nvrm_new_thing.c is compiled by nothing" in out, out[-300:])
rc, out = sources(lambda d: ((d / "kexts/NVRM/nvrm_new_thing.cpp").write_text(""),
                             (d / "build/build_nvrm.sh").write_text("# CXX_SOURCES+=( nvrm_new_thing.cpp )\n" + (d / "build/build_nvrm.sh").read_text())))
arm("control: a name in a comment of build_nvrm.sh does not count as listed", rc != 0 and "nvrm_new_thing.cpp is not in CXX_SOURCES" in out, out[-300:])
rc, out = sources(lambda d: (d / "kexts/NVRM/os-xnu.cpp").unlink())
arm("control: a listed source that is gone is caught", rc != 0 and "os-xnu.cpp is in CXX_SOURCES" in out, out[-300:])
rc, out = sources(lambda d: ((d / "kexts/NVRM/nvrm_new_thing.cpp").write_text(""),
                             bn_edit("for s in nv_rtd3.cpp nvrm_ddc.cpp; do", "for s in nv_rtd3.cpp nvrm_ddc.cpp nvrm_new_thing.cpp; do")(d)))
arm("a source added to the when-present list is listed", rc == 0, out[-300:])

arm("the check asks build_rm620.sh for the patched tree and stops it before the compile",
    "NM_PREPARE_ONLY=1 bash" in chk and "NM_PREPARE_ONLY" in (ROOT / "build/build_rm620.sh").read_text())
arm("the RM compile line takes the check's compiler (NM_RMCC_CC)", "NM_RMCC_CC" in chk and "NM_RMCC_CC" in (ROOT / "kexts/NVRM/rmcc.py").read_text())
# with neither variable set, the two real builds do what they did: the additions are one guarded statement each
rm620 = [l for l in (ROOT / "build/build_rm620.sh").read_text().splitlines() if "NM_PREPARE_ONLY" in l and not l.startswith("#")]
arm("build_rm620.sh stops early only when NM_PREPARE_ONLY is set", len(rm620) == 1 and rm620[0].startswith('[ -z "${NM_PREPARE_ONLY:-}" ] || {'), " | ".join(rm620))
rmcc = (ROOT / "kexts/NVRM/rmcc.py").read_text()
arm("rmcc.py changes its command line only when NM_RMCC_CC is set",
    rmcc.count("NM_RMCC_CC") >= 2 and 'if os.environ.get("NM_RMCC_CC"):' in rmcc
    and all(l.startswith(("    ", "#", "if os.environ.get")) for l in rmcc.split('if os.environ.get("NM_RMCC_CC"):')[1].split("r = subprocess.run")[0].splitlines() if l.strip()))

tags = re.search(r"^XNU_TAGS=\(([^)]*)\)", chk, re.M).group(1).split()
arm("the import check runs against the oldest and the newest supported kernel", len(tags) >= 2 and all(re.fullmatch(r"xnu-[\d.]+", t) for t in tags), " ".join(tags))
arm("the classifier's own default kernels are the check's",
    re.search(r'^XNU_TAGS = \(([^)]*)\)', imp, re.M) and re.findall(r'"([^"]+)"', re.search(r'^XNU_TAGS = \(([^)]*)\)', imp, re.M).group(1)) == tags)
arm("the import check is one run over every kernel, and a failure of it fails the check",
    len(re.findall(r"^python3 -B \"\$HERE/tools/kext_imports\.py\" .*\|\| rc=1$", chk, re.M)) == 1 and "--xnu-tag \"$tag\"" in chk and "--exports \"$d\"" in chk)
arm("downloads are pinned to a commit", bool(re.search(r"^OGKM_SHA=[0-9a-f]{40}\b", chk, re.M) and re.search(r"^MKSDK_SHA=[0-9a-f]{40}\b", chk, re.M))
    and all(re.search(rf'"{re.escape(t)}": "[0-9a-f]{{40}}"', imp) for t in tags), " ".join(tags))
arm("nothing the check needs is written for a newer bash than a Mac has", not re.search(r"\bdeclare -A\b|\bmapfile\b|\breadarray\b|\$\{\w+,,\}|\$\{\w+\^\^\}|&>>|\|&", chk))
r = subprocess.run([sys.executable, "-B", str(ROOT / "tools/kext_imports.py"), "--selftest"], capture_output=True, text=True)
arm("the import classifier fires (kext_imports.py --selftest)", r.returncode == 0 and "0 failed" in r.stdout, r.stdout[-300:])
for want in ("thread_call_cancel_wait", "only the Private KPI exports", "PE_boot_args resolves on macOS 26 alone", "checked against both kernels the same object fails"):
    arm(f"its self-test covers: {want}", want in r.stdout)
print(f"{len(FAILS)} failed")
sys.exit(1 if FAILS else 0)
