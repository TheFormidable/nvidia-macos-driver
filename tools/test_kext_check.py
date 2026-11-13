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
# what decides which code is compiled, what it warns about and what it imports: -f, -D, -U, -std, the kernel mode, the
# warning set (-Werror in a build script and not in the check: the Mac build stops where the check passed) and -O
FLAG = re.compile(r"(?<![\w-])(-(?:f[a-z][a-z-]+|mkernel|mno-[a-z-]+|std=[\w+]+|nostdinc|[DU][A-Za-z_]\w*|W[a-z][a-z0-9=+-]*|O[0-3sz]?))(?![\w-])")
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

# Apple's clang adds these warning groups to every kext compile (-mkernel / -fapple-kext); this machine's clang does
# not, so the check must carry them itself or it passes what stops the build on a Mac at its zero ceiling. missing()
# does not require them (a build script does not write them; Apple's driver adds them), so a dedicated arm asks that
# each is in KEXT_FLAGS and in the two compile lines that do not use KEXT_FLAGS (NMDRM, NVPrimarySelector).
MACWARN = ["-Werror=implicit-function-declaration", "-Warray-bounds", "-Warray-bounds-pointer-arithmetic",
           "-Wbuiltin-memcpy-chk-size", "-Wconditional-uninitialized", "-Wempty-body", "-Wimplicit-int-conversion",
           "-Wmemset-transposed-args", "-Wreturn-stack-address", "-Wshadow", "-Wshorten-64-to-32", "-Wsign-conversion",
           "-Wsizeof-pointer-memaccess", "-Wvla"]


def macwarn_missing(block):
    return [f for f in MACWARN if not re.search(r"(?<![\w=-])" + re.escape(f) + r"(?![\w=-])", block)]


mac_def = re.search(r"MACWARN=\(([^)]*)\)", chk)
kf = re.search(r"KEXT_FLAGS=\(([^)]*)\)", chk, re.S)
nmdrm_line = re.search(r'one "NMDRM/nmdrm\.cpp".*?-c "\$K/NMDRM/nmdrm\.cpp"', chk, re.S)
prim_line = re.search(r'one "NVPrimarySelector/.*?-c "\$K/NVPrimarySelector/NVPrimarySelector\.cpp"', chk, re.S)
arm("the check carries every warning group Apple's clang adds, in KEXT_FLAGS and the hand-written NMDRM and NVPrimarySelector lines",
    bool(mac_def) and not macwarn_missing(mac_def.group(1))
    and bool(kf) and '"${MACWARN[@]}"' in kf.group(1)
    and bool(nmdrm_line) and '"${MACWARN[@]}"' in nmdrm_line.group(0)
    and bool(prim_line) and '"${MACWARN[@]}"' in prim_line.group(0),
    " ".join(macwarn_missing(mac_def.group(1)) if mac_def else ["no MACWARN"]))
arm("control: dropping -Wshadow from the warning groups is caught",
    bool(mac_def) and macwarn_missing(mac_def.group(1).replace("-Wshadow", "", 1)) == ["-Wshadow"])
arm("control: a flag in a comment is not a flag", missing('# clang++ -DNEW_THING\nclang++ -fapple-kext -c x.cpp', chk) == [])
arm("control: a warning flag, an -U and an optimisation level the check does not have are caught",
    missing('clang++ -Wall -Wfloat-equal -UNEW_THING -Oz -O2 -c x.cpp', chk) == ["-Oz", "-UNEW_THING", "-Wfloat-equal"])

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
                             bn_edit("for s in nv_rtd3.cpp nvrm_ddc.cpp ", "for s in nv_rtd3.cpp nvrm_ddc.cpp nvrm_new_thing.cpp ")(d)))
arm("a source added to the when-present list is listed", rc == 0, out[-300:])


def present_loop_split(d):
    """The when-present loop with its body on the next line (the list and the test of each name apart)."""
    p = d / "build/build_nvrm.sh"; s = p.read_text()
    m = re.search(r'(?m)^(for s in [^;\n]*nv_rtd3\.cpp[^;\n]*; do)[ \t]*\n?[ \t]*(\[ -f "\$SRC/\$s" \] && CXX_SOURCES\+=\( "\$s" \))[ \t]*;?[ \t]*\n?[ \t]*done', s)
    assert m, "the when-present loop of build_nvrm.sh"
    p.write_text(s[:m.start()] + m.group(1) + "\n    " + m.group(2) + "\ndone" + s[m.end():])


rc, out = sources(present_loop_split)
arm("the when-present loop is read however its lines are broken (it is run, not parsed)", rc == 0, out[-300:])
rc, out = sources(lambda d: (present_loop_split(d), (d / "kexts/NVRM/nvrm_new_thing.cpp").write_text("")))
arm("control: with the loop split, a .cpp it does not name is still caught", rc != 0 and "nvrm_new_thing.cpp is not in CXX_SOURCES" in out, out[-300:])


def one_line_and_older_tree(d):
    """The list written on one line, in a tree that has not got the two when-present sources."""
    p = d / "build/build_nvrm.sh"; s = p.read_text()
    joined = re.sub(r"(?m)^CXX_SOURCES=\([^)]*\)", lambda m: " ".join(m.group(0).split()), s, count=1)
    assert joined != s and re.search(r"(?m)^CXX_SOURCES=\( .*\)$", joined)
    p.write_text(joined)
    for n in ("nv_rtd3.cpp", "nvrm_ddc.cpp"):
        (d / "kexts/NVRM" / n).unlink()


rc, out = sources(one_line_and_older_tree)
arm("the always-compiled list ends at its own ')': written on one line it does not take in the when-present sources of the next", rc == 0, out[-300:])

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
# the check runs under pipefail. A pipeline whose last command leaves at its first match (grep -q) fails now and then:
# the command before it is still writing, dies of SIGPIPE, and pipefail reports that as "no match"
EARLY = re.compile(r"\|\s*grep\b[^|;&]*\s(-[A-Za-z]*q[A-Za-z]*|--quiet|--silent)(?![\w-])")
early = [l.strip()[:90] for l in chk.splitlines() if not l.lstrip().startswith("#") and EARLY.search(l)]
arm("under pipefail, no pipeline of the check ends in a grep that leaves at its first match", "pipefail" in chk and not early, " | ".join(early))
arm("control: such a pipeline is seen, however its options are written",
    all(EARLY.search(l) for l in ('say "$code" | grep -q -F "x" || bad=1', "a | grep -F x | grep -Fq 'y'", "a | grep --quiet y"))
    and not EARLY.search("a | grep -F x | grep 'y' > /dev/null") and not EARLY.search('grep -q x "$file"'))
arm("nothing the check needs is written for a newer bash than a Mac has", not re.search(r"\bdeclare -A\b|\bmapfile\b|\breadarray\b|\$\{\w+,,\}|\$\{\w+\^\^\}|&>>|\|&", chk))
r = subprocess.run([sys.executable, "-B", str(ROOT / "tools/kext_imports.py"), "--selftest"], capture_output=True, text=True)
arm("the import classifier fires (kext_imports.py --selftest)", r.returncode == 0 and "0 failed" in r.stdout, r.stdout[-300:])
for want in ("thread_call_cancel_wait", "only the Private KPI exports", "PE_boot_args resolves on macOS 26 alone", "checked against both kernels the same object fails",
             "a function they do not define is not excused: floor()", "a declaration, an extern variable, a static function, a call, a macro and a comment define nothing",
             "is not the family's because a family class is among its parameters"):
    arm(f"its self-test covers: {want}", want in r.stdout)
# what the classifier is given: a mention of a name in NVIDIA's sources proves nothing (they mention free and floor),
# and a static function of one object does not resolve a call from another
arm("the NVIDIA class goes by definitions and the kext's own symbols by what is external",
    "nvidia_defined(Path(a.ogkm))" in imp and 'nm(a.nm, p, "-gU")' in imp and "nvidia_words" not in imp and 'nm(a.nm, p, "-U")' not in imp)
print(f"{len(FAILS)} failed")
sys.exit(1 if FAILS else 0)
