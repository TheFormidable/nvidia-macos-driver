#!/usr/bin/env python3
"""The page attribute table code of NVRM (kexts/NVRM/nvrm_pat.h, NVRM.cpp): it reports the table and, since 3.0.13,
puts entry 6 back to write-combining.

tools/test_nvrm_pat.c proves the decode and the change itself (nvrm_pat_with_wc6, nvrm_pat_fix_wanted). This holds
the wiring to what makes the change safe to ship: the table is read with rdmsr_carefully (which returns an error for a
missing MSR instead of faulting), inside the block that registers the sysctls once per boot and before they are
registered; the sysctls are read-only; the one MSR write under kexts/ is the PAT's, of the value nvrm_pat_with_wc6
computes, inside the per-CPU function of a rendezvous with interrupts off, after no-fill cache mode and a cache flush;
the boot argument nvpatwc=0 is read once and keeps every write away; the wake pass runs from setPowerState only for a
power-on and only where the start pass changed something; and the decoder's verdict decides nothing.
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


def func(src, name):
    """The body of a static function defined in src, by brace matching."""
    m = re.search(r"\n(?:static |extern \"C\" )?[\w ]*\b%s\s*\([^;{]*\)\s*\{" % re.escape(name), src)
    if not m:
        return ""
    i, depth = m.end(), 1
    while depth and i < len(src):
        depth += {"{": 1, "}": -1}.get(src[i], 0)
        i += 1
    return src[m.end():i - 1]


t = NVRM.read_text()
code = re.sub(r"//[^\n]*", "", t)   # comments may name the calls; only code counts

names = ("nvrm_pat", "nvrm_pat_wc", "nvrm_pat_why", "nvrm_pat_found", "nvrm_pat_passes", "nvrm_pat_cpus")
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
arm("the table is read (and changed) inside it, before its sysctls are registered", 0 <= read_at < first_reg)
arm("nvrmPatRead is called once, from that block only", len(re.findall(r"\bnvrmPatRead\s*\(\s*\)\s*;", code)) == 1)

reads = re.findall(r"\brdmsr_carefully\s*\(([^;]*?)\)\s*(==|!=)\s*0", code)
arm("every rdmsr_carefully reads the PAT MSR and has its result checked",
    len(re.findall(r"\brdmsr_carefully\s*\(", code)) == len(reads) + 1 and reads and all("NVRM_PAT_MSR" in r[0] for r in reads))
hdr = (ROOT / "kexts/NVRM/nvrm_pat.h").read_text()
arm("NVRM_PAT_MSR is 0x277 (IA32_PAT)", re.search(r"#define NVRM_PAT_MSR 0x277u?\b", hdr) is not None)

# the only MSR write
writers = {}
for p in sorted((ROOT / "kexts").rglob("*")):
    if p.suffix in (".c", ".cpp", ".h", ".hpp", ".s", ".S") and p.is_file():
        src = re.sub(r"//[^\n]*|/\*.*?\*/", "", p.read_text(errors="replace"), flags=re.S)
        n = len(re.findall(r"\bwrmsr\w*\b", src))
        if n:
            writers[str(p.relative_to(ROOT))] = n
arm("the one MSR write under kexts/ is a single wrmsr in NVRM.cpp", writers == {"kexts/NVRM/NVRM.cpp": 1})
cpu = func(code, "nvrmPatFixCpu")
arm("it is inside nvrmPatFixCpu", re.search(r"\bwrmsr\b", cpu) is not None)
arm("it writes the PAT MSR, with the value nvrm_pat_with_wc6 computed from the table this CPU read",
    '"wrmsr" : : "c"(NVRM_PAT_MSR), "a"((uint32_t)want), "d"((uint32_t)(want >> 32)) : "memory"' in cpu
    and re.search(r"const uint64_t want = nvrm_pat_with_wc6\(pat\);", cpu) is not None)
arm("only when nvrm_pat_fix_wanted says so for this CPU's table", re.search(r"if \(nvrm_pat_fix_wanted\(pat, 1\)\) \{", cpu) is not None
    and cpu.find("nvrm_pat_fix_wanted") < cpu.find("wrmsr"))
pos = {k: cpu.find(k) for k in ('"wbinvd"', '"wrmsr"')}
cd_at = cpu.find("(cr0 | cd) & ~nw")
second_wbinvd = cpu.find('"wbinvd"', pos['"wrmsr"'])
restore_cr0 = cpu.rfind('"r"(cr0)')
arm("no-fill cache mode, then a cache flush, before the write; a flush after it; CR0 restored last but CR4",
    0 <= cd_at < pos['"wbinvd"'] < pos['"wrmsr"'] < second_wbinvd < restore_cr0)
arm("CR0.CD is bit 30, CR0.NW bit 29, CR4.PGE bit 7",
    "cd = (uintptr_t)1 << 30" in cpu and "nw = (uintptr_t)1 << 29" in cpu and "pge = (uintptr_t)1 << 7" in cpu)
arm("nvrmPatFixCpu runs only as the action of mp_rendezvous_no_intrs (every CPU, interrupts off)",
    len(re.findall(r"\bnvrmPatFixCpu\b", code)) == 2 and "mp_rendezvous_no_intrs(nvrmPatFixCpu, NULL);" in code)

# the escape and the two callers
arm("nvpatwc is read once, and only nvpatwc=0 turns the change off",
    len(re.findall(r'PE_parse_boot_argn\("nvpatwc"', code)) == 1
    and 'on = !(PE_parse_boot_argn("nvpatwc", &v, sizeof v) && v == 0u);' in code)
fixes = re.findall(r"\bnvrmPatFix\s*\(\s*\"(\w+)\"\s*\)", code)
arm("a pass is started from two places only: start and wake", sorted(fixes) == ["start", "wake"])
rd = func(code, "nvrmPatRead")
arm("the start pass runs only when the table found wants it and nvpatwc=0 is not set",
    re.search(r"if \(nvrm_pat_fix_wanted\(gNvPatFound, ok\)\) \{\s*if \(nvrmPatFixOn\(\)\) \{ nvrmPatFix\(\"start\"\);", rd) is not None)
wk = func(code, "nvrmPatAfterWake")
arm("the wake pass runs only where the start pass changed something", re.search(r"if \(gNvPatPasses > 0\) nvrmPatFix\(\"wake\"\);", wk) is not None)
sps = func(code, "NVRM::setPowerState")
arm("the wake pass is called from setPowerState for a power-on only, before the GPU resumes",
    len(re.findall(r"\bnvrmPatAfterWake\s*\(\s*\)\s*;", code)) == 1 and "if (ordinal != 0) nvrmPatAfterWake();" in sps
    and sps.find("nvrmPatAfterWake") < sps.find("gpuResume"))
fx = func(code, "nvrmPatFix")
arm("two passes never overlap (a second caller leaves it to the first)",
    fx.lstrip().startswith("if (!OSCompareAndSwap(0, 1, &gPatBusy)) return;") and "OSCompareAndSwap(1, 0, &gPatBusy);" in fx)

# the decoder's verdict decides nothing: it is only stored and published
bare = re.sub(r'"(?:[^"\\\n]|\\.)*"', '""', code)
deciders = [ln.strip() for ln in (re.sub(r"^\s*gNvPatWc\s*=", "", x) for x in bare.splitlines())
            if re.search(r"\bgNvPatWc\b", ln) and re.search(r"\b(if|while|switch|return)\b|\?", ln)]
arm("the verdict (gNvPatWc) is only stored and published, never read to decide anything", not deciders)
arm("no other kext file names the PAT state", not [p for p in (ROOT / "kexts").rglob("*.c*") if p != NVRM and "gNvPat" in p.read_text(errors="replace")])

print("FAILED %d" % fails if fails else "ok")
sys.exit(1 if fails else 0)
