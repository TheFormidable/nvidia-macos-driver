#!/usr/bin/env python3
"""The page attribute table diagnostic of NVRM (kexts/NVRM/nvrm_pat.h, NVRM.cpp) observes and never acts.

tools/test_nvrm_pat.c proves the decode. This holds the wiring to what makes the diagnostic safe to ship without a run
on the card: the table is read once, with rdmsr_carefully (which returns an error for a missing MSR instead of
faulting), inside the block that registers the sysctls once per boot; the three sysctls are read-only; nothing under
kexts/ writes an MSR, so the driver cannot change the table it reports; and no code path reads the verdict back, so a
wrong verdict cannot change what the driver does.
Usage: test_nvrm_pat.py [driver checkout]"""
import pathlib
import re
import sys

ROOT = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1]
NVRM = ROOT / "kexts/NVRM/NVRM.cpp"
fails = 0


def arm(name, ok):
    global fails
    print(("ok   " if ok else "FAIL ") + name)
    fails += not ok


t = NVRM.read_text()
code = re.sub(r"//[^\n]*", "", t)   # comments may name the calls; only code counts

names = ("nvrm_pat", "nvrm_pat_wc", "nvrm_pat_why")
for n in names:
    m = re.search(r"SYSCTL_\w+\(\s*_debug\s*,\s*OID_AUTO\s*,\s*%s\s*,([^,]*)," % n, code)
    arm(f"debug.{n} is declared", m is not None)
    if m:
        arm(f"debug.{n} is read-only (CTLFLAG_RD, no RW/WR/ANYBODY)",
            "CTLFLAG_RD" in m.group(1) and not re.search(r"CTLFLAG_(RW|WR|ANYBODY)", m.group(1)))

block = re.search(r"static bool sysctlsUp;\s*if \(!sysctlsUp\) \{\s*sysctlsUp = true;(.*?)\n        \}\n", code, re.S)
arm("the once-per-boot sysctl block is found", block is not None)
body = block.group(1) if block else ""
for n in names:
    arm(f"debug.{n} is registered inside it", f"sysctl_register_oid(&sysctl__debug_{n})" in body)
read_at = body.find("nvrmPatRead()")
first_reg = min([body.find(f"sysctl__debug_{n})") for n in names if body.find(f"sysctl__debug_{n})") >= 0] or [-1])
arm("the table is read inside it, before its sysctls are registered", 0 <= read_at < first_reg)
arm("nvrmPatRead is called once, from that block only", len(re.findall(r"\bnvrmPatRead\s*\(\s*\)\s*;", code)) == 1)

calls = re.findall(r"\brdmsr_carefully\s*\(([^;]*)\)\s*==\s*0", code)
arm("rdmsr_carefully is called once, with the PAT MSR, and its result is checked",
    len(re.findall(r"\brdmsr_carefully\s*\(", code)) == 2 and len(calls) == 1 and "NVRM_PAT_MSR" in calls[0])
hdr = (ROOT / "kexts/NVRM/nvrm_pat.h").read_text()
arm("NVRM_PAT_MSR is 0x277 (IA32_PAT)", re.search(r"#define NVRM_PAT_MSR 0x277u?\b", hdr) is not None)

writers = []
for p in sorted((ROOT / "kexts").rglob("*")):
    if p.suffix in (".c", ".cpp", ".h", ".hpp", ".s", ".S") and p.is_file():
        src = p.read_text(errors="replace")
        if re.search(r"\bwrmsr\w*\b", src):
            writers.append(str(p.relative_to(ROOT)))
arm("nothing under kexts/ writes an MSR (no wrmsr): the driver cannot change the table it reports", not writers)

# a line that stores into the verdict is not a reader of it: take the target off before looking for a condition
bare = re.sub(r'"(?:[^"\\\n]|\\.)*"', '""', code)   # nor do the words of a description
deciders = [ln.strip() for ln in (re.sub(r"^\s*gNvPat\w*\s*=", "", x) for x in bare.splitlines())
            if re.search(r"\bgNvPat", ln) and re.search(r"\b(if|while|switch|return)\b|\?", ln)]
arm("the verdict is only stored and published, never read to decide anything", not deciders)
arm("no other kext file names the verdict", not [p for p in (ROOT / "kexts").rglob("*.c*") if p != NVRM and "gNvPat" in p.read_text(errors="replace")])

print("FAILED %d" % fails if fails else "ok")
sys.exit(1 if fails else 0)
