#!/usr/bin/env python3
"""test_nv_compile_lines.py: the RM core's and NVKMS's compile commands are the same on an Intel and on an Apple-silicon
Mac.

NVIDIA's Makefiles ask the build computer's compiler (with no -arch) which hardening options it takes. An Intel Mac's
clang took -mfunction-return=thunk-extern for NVKMS, every return became a jump to ___x86_return_thunk, and NVRM.kext,
which no macOS kernel can then link, was built without a word. build/nv_compile_lines.py now makes the commands of
build_rm620.sh and build_kms620.sh.

The recorded lines in tools/fixtures/nv_make_n are real "make -n TARGET_OS=Darwin TARGET_ARCH=x86_64 NV_VERBOSE=1"
output of NVIDIA's two Makefiles (open-gpu-kernel-modules 620.70.06 with kexts/NVRM/patches applied), three lines per
component, recorded on Linux, where the compiler probes could be pointed at each kind of Mac: "intel" with cc = clang 18
for x86_64-apple-macos15.0, "apple-silicon" with cc = clang 18 for arm64-apple-macos15.0 (what an Intel Mac's and an
Apple-silicon Mac's cc answer), "linux-gcc" with the computer's own gcc 13 and GNU ld (what build/kext_check.sh sees on
a Linux runner). GNU make 4.3 names the C++ compiler g++; Apple's make 3.81 names it c++, and the C++ line was
recorded with that name. Over the whole lists (1121 RM and 225 NVKMS commands) the only differences were:
  RM:    intel +-fcf-protection=full; linux-gcc also +-mindirect-branch-register +-mindirect-branch=thunk-extern
  NVKMS: intel +-fcf-protection=full +-mfunction-return=thunk-extern; linux-gcc also the two -mindirect-branch options,
         -Wformat-overflow=2 -Wformat-truncation=1, and -ffunction-sections -fdata-sections (asked of the linker).

Arms: the three recordings give the same commands; they are, byte for byte, what the transform before this change made
of the Apple-silicon recording (an Apple-silicon Mac's build does not change); 2.9's own choices are there; the Intel
recording gets a note naming what was left out, the Apple-silicon one none; an unexpected compiler stops; both scripts
call the one file and stop when it fails.
Controls: the transform as it was at 3a27af8 (read with git show) lets -mfunction-return=thunk-extern through and makes
the Intel and Apple-silicon commands differ; the new transform without its -mfunction-return rule makes them differ."""
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tools/fixtures/nv_make_n"
OLD = "3a27af8"   # the scripts before the change
HOSTS = ("intel", "apple-silicon", "linux-gcc")
PREFIX = "-arch x86_64 -mmacosx-version-min=15.0 -mkernel -DNV_DARWIN -fcf-protection=branch"
fails = 0


def check(name, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + name + (f"  [{str(detail)[:1200]}]" if detail and not ok else ""), flush=True)
    fails += 0 if ok else 1


def negative(name, problems):
    check(f"control: {name}", bool(problems), "the check found nothing wrong")


def load():
    spec = importlib.util.spec_from_file_location("nv_compile_lines", ROOT / "build/nv_compile_lines.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def rec(comp, host):
    return (FIX / f"{comp}-{host}.txt").read_text().splitlines(True)


def old_transform(comp, lines):
    """The inline transform of build_<comp>620.sh at OLD, run as it ran."""
    script = "build/build_rm620.sh" if comp == "rm" else "build/build_kms620.sh"
    r = subprocess.run(["git", "-C", str(ROOT), "show", f"{OLD}:{script}"], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"SKIP: the old scripts ({OLD}) are not in this clone: run from a full clone")
        sys.exit(2)
    m = re.search(r"python3 -c '\n(.*?)' > \"\$O/compile_cmds\.sh\.new\"", r.stdout, re.S)
    assert m, "the old transform was not found in " + script
    p = subprocess.run([sys.executable, "-B", "-c", m.group(1)], input="".join(lines), capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout.splitlines()


def same_problems(mod, comp):
    out = {h: mod.transform(comp, rec(comp, h))[0] for h in HOSTS}
    return [f"{comp}: {h} differs from apple-silicon: {sorted(set(out[h][i].split()) ^ set(out['apple-silicon'][i].split()))}"
            for h in HOSTS for i in range(len(out[h])) if out[h][i] != out["apple-silicon"][i]] + \
           [f"{comp}: {h} has {len(out[h])} commands" for h in HOSTS if len(out[h]) != 3]


def line_problems(comp, lines):
    p = []
    for l in lines:
        t = l.split()
        if t[0] not in ("clang", "clang++") or not l.split(" ", 1)[1].startswith(PREFIX + " "):
            p.append(f"does not start with clang and the scripts' options: {l[:100]}")
        cf = [x for x in t if x.startswith("-fcf-protection")]
        if cf != ["-fcf-protection=branch"]:
            p.append(f"-fcf-protection options {cf}")
        bad = [x for x in t if x.startswith(("-mfunction-return", "-mindirect-branch", "-Wformat-overflow=", "-Wformat-truncation="))
               or x in ("-mcmodel=kernel", "-fno-pic")]
        if bad:
            p.append(f"still has {bad}")
        if "-fno-jump-tables" not in t:
            p.append("lost -fno-jump-tables (both kinds of Mac take it)")
        if comp == "rm" and "-ffunction-sections" not in t:
            p.append("the RM lost -ffunction-sections (its Makefile adds it always)")
    return p


def main():
    mod = load()
    for comp in ("rm", "kms"):
        p = same_problems(mod, comp)
        check(f"{comp}: the Intel, Apple-silicon and Linux recordings give the same commands", not p, p)
        new = mod.transform(comp, rec(comp, "apple-silicon"))[0]
        old = old_transform(comp, rec(comp, "apple-silicon"))
        check(f"{comp}: an Apple-silicon Mac's commands are byte for byte what they were before", new == old,
              [(a[:80], b[:80]) for a, b in zip(new, old) if a != b][:2] or (len(new), len(old)))
        intel = mod.transform(comp, rec(comp, "intel"))[0]
        p = line_problems(comp, intel)
        check(f"{comp}: from the Intel recording: 2.9's own options, one -fcf-protection (branch), no host-probed option", not p, p)
        olds = old_transform(comp, rec(comp, "intel"))
        if comp == "kms":
            negative("the transform of " + OLD + " keeps -mfunction-return=thunk-extern from the Intel recording",
                     [l for l in olds if "-mfunction-return=thunk-extern" in l])
        negative(f"{comp}: the transform of {OLD} makes Intel's and Apple silicon's commands differ",
                 olds != old_transform(comp, rec(comp, "apple-silicon")))
        notes_i = mod.transform(comp, rec(comp, "intel"))[1]
        notes_a = mod.transform(comp, rec(comp, "apple-silicon"))[1]
        want = ["-fcf-protection=full"] + (["-mfunction-return=thunk-extern"] if comp == "kms" else [])
        check(f"{comp}: the Intel build says what it left out ({', '.join(want)}); the Apple-silicon build says nothing",
              all(any(w in n for n in notes_i) for w in want) and not notes_a, (notes_i, notes_a))
    saved = mod.HOST_PROBED
    mod.HOST_PROBED = [r for r in saved if "function-return" not in r[1]]
    negative("the transform without its -mfunction-return rule makes the NVKMS commands differ", same_problems(mod, "kms"))
    mod.HOST_PROBED = saved

    cli = [sys.executable, "-B", str(ROOT / "build/nv_compile_lines.py")]
    r = subprocess.run(cli + ["kms"], input="".join(rec("kms", "intel")), capture_output=True, text=True)
    check("the command line gives the same commands, notes on standard error only",
          r.returncode == 0 and r.stdout.splitlines() == mod.transform("kms", rec("kms", "intel"))[0]
          and "note: left out -mfunction-return=thunk-extern" in r.stderr and "note:" not in r.stdout, (r.returncode, r.stderr[-300:]))
    for comp, line in (("rm", "gcc -O2 -c a.c -o a.o"), ("rm", "c++ -O2 -c a.cpp -o a.o"), ("kms", "g++ -O2 -c a.cpp -o a.o")):
        r = subprocess.run(cli + [comp], input=rec(comp, "apple-silicon")[0] + line + "\n", capture_output=True, text=True)
        check(f"{comp}: a command of {line.split()[0]} stops (exit 2, STOP, nothing written)",
              r.returncode == 2 and r.stderr.startswith("STOP:") and r.stdout == "", (r.returncode, r.stdout[:80], r.stderr))
    for script, comp in (("build/build_rm620.sh", "rm"), ("build/build_kms620.sh", "kms")):
        s = (ROOT / script).read_text()
        calls = [l for l in s.splitlines() if "nv_compile_lines.py" in l and not l.lstrip().startswith("#")]
        check(f"{script} makes its commands with build/nv_compile_lines.py {comp} and stops when that fails",
              len(calls) == 1 and f'nv_compile_lines.py" {comp} > "$O/compile_cmds.sh.new"' in calls[0] and calls[0].rstrip().endswith("\\")
              and "python3 -c" not in s and "STOP:" in s.split(calls[0], 1)[1].splitlines()[1], calls)

    print(f"{fails} failed" if fails else "all passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
