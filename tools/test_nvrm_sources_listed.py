#!/usr/bin/env python3
"""Every source file in kexts/NVRM is one that build/build_nvrm.sh compiles into NVRM.kext.

The script compiles the C++ files named in its CXX_SOURCES list, not whatever lies in the folder, and stops when a
.cpp in the folder is not on the list. That stop happens on the build Mac, after the NVIDIA core has been built. A new
source file that was compiled and tested everywhere else, and never added to the list, is found only there. The C
files are worse off: the script compiles them one by one by name and checks nothing, so a new .c that is not named is
simply missing from the kext, and the functions it defines are then missing when the kext loads.

The list is not read here as text. The script's own lines, from "CXX_SOURCES=(" to its first compiler call (the list,
the sources it adds only when they exist, and its own check), are run by bash on the real folder and on stand-in
folders, so what is tested is what the build does.
Usage: test_nvrm_sources_listed.py [driver checkout]"""
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "build/build_nvrm.sh"
SRC = ROOT / "kexts/NVRM"
fails = 0


def arm(name, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + name + (f"  [{str(detail).strip()[-300:]}]" if detail and not ok else ""))
    fails += not ok


def list_block(script):
    """The lines of build_nvrm.sh that make and check the list: from the line that starts CXX_SOURCES=( to the line
    before the next one that calls the compiler (xcrun). '' when the script has no such part."""
    lines = script.split("\n")
    a = next((i for i, ln in enumerate(lines) if re.match(r"\s*CXX_SOURCES=\(", ln)), None)
    b = next((i for i, ln in enumerate(lines) if a is not None and i > a and "xcrun" in ln), None)
    return "\n".join(lines[a:b]) if a is not None and b is not None else ""


def run_block(block, src, stop_exits):
    """Run the lines on one source folder. stop_exits: the script's `exit` ends the run, as in the build (the answer is
    then its exit status and what it printed); otherwise `exit` does nothing and the list is printed whatever the
    script thought of it. -> (status, output, list)"""
    prelude = "set -uo pipefail\n" + ("" if stop_exits else "exit() { :; }\n")
    r = subprocess.run(["bash", "-c", prelude + block + '\nprintf "LISTED %s\\n" "${CXX_SOURCES[@]}"\n'],
                       capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin:/usr/local/bin", "SRC": str(src), "OGKM": "/nonexistent", "OUT": "/nonexistent",
                            "HERE": str(ROOT / "build"), "NV": "/nonexistent", "KMS": "/nonexistent", "HOME": "/nonexistent"})
    out = r.stdout + r.stderr
    return r.returncode, out, [ln[7:] for ln in out.split("\n") if ln.startswith("LISTED ")]


def judge(block, src):
    """-> (the .cpp files of src the list does not name, the listed names with no file, the names listed twice,
    whether the script's own check let the folder pass)."""
    _, _, listed = run_block(block, src, stop_exits=False)
    have = sorted(p.name for p in src.glob("*.cpp"))
    status, out, _ = run_block(block, src, stop_exits=True)
    return ([n for n in have if n not in listed], [n for n in listed if n not in have],
            sorted({n for n in listed if listed.count(n) > 1}), status == 0 and "STOP" not in out)


def c_unnamed(script, names):
    """The C files of the folder the script never names. It compiles a C file by writing "$SRC/<name>" on a command
    line; a comment does not compile anything."""
    live = "\n".join(ln for ln in script.split("\n") if not ln.lstrip().startswith("#"))
    return [n for n in names if not re.search(r'\$SRC/' + re.escape(n) + r'(?![\w.+-])', live)]


if not shutil.which("bash"):
    print("SKIP: needs bash"); sys.exit(2)
script = SCRIPT.read_text()
block = list_block(script)
arm("build_nvrm.sh makes its list of C++ sources before its first compiler call, and checks the folder against it",
    bool(block) and "not in CXX_SOURCES" in block and "is listed but missing" in block)
unlisted, missing, twice, passes = judge(block, SRC) if block else ([], [], [], False)
arm("every kexts/NVRM/*.cpp is on the list the build compiles", block and not unlisted, ", ".join(unlisted))
arm("every name on the list is a file in kexts/NVRM", block and not missing, ", ".join(missing))
arm("no name is on the list twice (its object would be linked twice)", block and not twice, ", ".join(twice))
arm("the script's own check lets the folder through", passes)
c_files = sorted(p.name for p in SRC.glob("*.c"))
arm("every kexts/NVRM/*.c is compiled by name in build_nvrm.sh", c_files and not c_unnamed(script, c_files), ", ".join(c_unnamed(script, c_files)))
# controls: stand-in folders holding the names the list has for the real folder (empty files are enough: nothing is
# compiled here), so that they say the same whatever state the real folder is in
names = sorted(set(run_block(block, SRC, stop_exits=False)[2])) if block else []
with tempfile.TemporaryDirectory() as d:
    d = pathlib.Path(d)

    def folder(name, drop=(), add=()):
        f = d / name; f.mkdir()
        for n in [n for n in names if n not in drop] + list(add):
            (f / n).write_text("")
        return f

    same = judge(block, folder("same")) if names else None
    arm("control: a stand-in folder with exactly the listed names passes", same is not None and same[:2] == ([], []) and same[3] is True, same)
    extra = judge(block, folder("extra", add=["zz_new_source.cpp"])) if names else None
    arm("control: a .cpp that is not on the list is named here, and the script's own check stops the build on it",
        extra is not None and extra[0] == ["zz_new_source.cpp"] and extra[3] is False, extra)
    gone = judge(block, folder("gone", drop=["NVRM.cpp"])) if names else None
    arm("control: a listed file that is missing is named here, and the script's own check stops the build on it",
        gone is not None and gone[1] == ["NVRM.cpp"] and gone[3] is False, gone)
    status, out, _ = run_block(block, folder("extra2", add=["zz_new_source.cpp"]), stop_exits=True) if names else (0, "", [])
    arm("control: the build's message names the file", status == 1 and "STOP: zz_new_source.cpp" in out, out)
arm("control: a C file named only in a comment, or only as the start of a longer name, counts as not compiled",
    c_unnamed('# "$SRC/a.c" was compiled here once\nrmcc.py "$SRC/ab.c" "$SRC/b.c.in" "$OUT/b.o"\n', ["a.c", "b.c", "ab.c"]) == ["a.c", "b.c"])
print("FAILED" if fails else "PASS"); sys.exit(1 if fails else 0)
