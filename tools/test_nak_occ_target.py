#!/usr/bin/env python3
"""A heavy graphics shader is built again under a lower register cap when that gains warps, on by default, with
NAK_OCC_TARGET=0 as the way back (nvk/nvk-nak-occ-target.patch, 3.0.15). No Mesa tree is needed here:

  the chain    the patch is the last of the 3.0 chain, right after nvk-nak-stats, in no other chain, and changes
               src/nouveau/compiler/nak/api.rs alone
  the switch   NAK_OCC_TARGET is read once, through a OnceLock, and only the value 0 turns the pick off; it is an escape
               row of tools/switches.tsv, on by default, and in the shader cache key table of nvk-nak-switch-key
  the pick     it runs only behind nak_occ_target_on(), never in WindowServer (whose executable's name is tested once,
               because NAK_OCC_TARGET=0 cannot reach it), for the vertex, tessellation, geometry and fragment stages, not
               when the shader has a RAW budget of its own, only below NAK_OCC_TRY_BELOW (32) warps and for a shader
               that samples a texture or reads global memory; it tries at most twice, drops a build that panics, and
               keeps a build only if it fits under the cap, gains warps, and has at most NAK_OCC_MAX_GROWTH_PCT (5)
               percent more instructions than the first build
  the cap      the build closure hands the ceiling to the RA budget only when there is no RAW budget, and checks the
               RAW budget's result only when there is one, as before
  the ceiling  nak_occ_ceiling is cut out of the patch and built with rustc beside NAK's max_warps_per_sm: for every
               register count, the ceiling it gives is the largest one with more warps, never below the reserved
               registers plus 32, and None when no count gains warps
  the docs     CHANGELOG.md has a 3.0.15 section that names NAK_OCC_TARGET=0, and docs/STATUS.md says host-tested only
  controls     the same arms over broken copies: the default turned off, compute added to the stages, the 5 % gate
               raised, the warp check dropped, the key table without the switch, a ceiling that is one step too low,
               the pick run in WindowServer too, WindowServer's name misspelt.
               Each has to fail.

What this does not show: that more warps make a frame faster on a card (that is the card round's question: the same
place in a game, frames per second and GPU utilisation with and without NAK_OCC_TARGET=0), or what the pick does to a
given shader (tools/nak-corpus/run.sh and tools/nak_stats_diff.py measure that on a Linux build). Without rustc the
ceiling arm is skipped and the test says so (exit 2).
Usage: test_nak_occ_target.py"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / "nvk/nvk-nak-occ-target.patch"
KEYPATCH = ROOT / "nvk/nvk-nak-switch-key.patch"
SCRIPT = ROOT / "build/build_nvk.sh"
RUSTC = shutil.which("rustc")
fails = 0
skipped = 0


def arm(name, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + name + (f" ({str(detail).strip()[-300:]})" if detail and not ok else ""))
    fails += not ok


def chains():
    case = re.search(r"case \$REL in\n(.*?)\n\s*esac", SCRIPT.read_text(), re.S).group(1)
    return {m.group(1): m.group(2).split() for m in re.finditer(r"^\s*([\d.]+)\).*?CHAIN=\"([^\"]*)\"", case, re.S | re.M)}


def added(text):
    body = text.partition("\ndiff --git ")[2]
    return "\n".join(l[1:] for l in body.splitlines() if l.startswith("+") and not l.startswith("+++"))


def switch_problems(add):
    probs = []
    if len(re.findall(r'env::var(?:_os)?\("NAK_OCC_TARGET"\)', add)) != 1:
        probs.append("NAK_OCC_TARGET is not read exactly once")
    if not re.search(r'OnceLock<bool>.*?get_or_init\(\|\| env::var\("NAK_OCC_TARGET"\)\.map_or\(true, \|v\| v != "0"\)\)',
                     add, re.S):
        probs.append("NAK_OCC_TARGET is not read once, on unless 0")
    return probs


def pick_problems(add):
    add = re.sub(r"\s+", " ", add)   # rustfmt's line breaks do not matter
    probs = []
    m = re.search(r"fn nak_occ_stage\(stage: mesa_shader_stage\) -> bool \{(.*?)\}", add)
    stages = set(re.findall(r"MESA_SHADER_\w+", m.group(1))) if m else set()
    if stages != {"MESA_SHADER_VERTEX", "MESA_SHADER_TESS_CTRL", "MESA_SHADER_TESS_EVAL", "MESA_SHADER_GEOMETRY",
                  "MESA_SHADER_FRAGMENT"}:
        probs.append(f"the stages are {sorted(stages)}")
    if not re.search(r"const NAK_OCC_TRY_BELOW: u32 = 32;", add):
        probs.append("NAK_OCC_TRY_BELOW is not 32")
    if not re.search(r"const NAK_OCC_MAX_GROWTH_PCT: u64 = 5;", add):
        probs.append("NAK_OCC_MAX_GROWTH_PCT is not 5")
    if not re.search(r'fn nak_occ_in_windowserver\(\) -> bool \{ static WS: OnceLock<bool> = OnceLock::new\(\); '
                     r'\*WS\.get_or_init\(\|\| \{ env::current_exe\(\)\.map_or\(false, \|p\| p\.file_name\(\)'
                     r'\.map_or\(false, \|n\| n == "WindowServer"\)\) \}\) \}', add):
        probs.append("nak_occ_in_windowserver does not test the executable's name once")
    m = re.search(r"let s = if nak_occ_target_on\(\) && !nak_occ_in_windowserver\(\) && nak_occ_stage\(nir\.info\.stage\(\)\) "
                  r"&& !\(nak\.sm == 120 && unsafe \{ nvmtl_raw_budget\(nir\) \} != 0\) \{(.*?)\} else \{ s \};",
                  add)
    if not m:
        return probs + ["no pick behind nak_occ_target_on(), the WindowServer test, the stage filter and the RAW budget"]
    p = m.group(1)
    for what, rx in (("the warp and latency test", r"if warps < NAK_OCC_TRY_BELOW && nak_occ_hides_latency\(&best\)"),
                     ("at most two tries", r"for _ in 0\.\.2 \{"),
                     ("the ceiling set only for the try", r"let _c = NakOccCeilingGuard::new\(ceiling\);"),
                     ("a panic dropped", r"panic::catch_unwind\(.*?\)\.ok\(\)"),
                     ("the cap checked", r"physical > ceiling"),
                     ("the warps checked", r"c\.info\.max_warps_per_sm <= warps"),
                     ("the instruction gate", r"u64::from\(c\.info\.num_instrs\) \* 100 > u64::from\(first_instrs\) "
                                              r"\* \(100 \+ NAK_OCC_MAX_GROWTH_PCT\)")):
        if not re.search(rx, p, re.S):
            probs.append(f"no {what}")
    return probs


def cap_problems(add):
    add = re.sub(r"\s+", " ", add)
    probs = []
    if not re.search(r"let cap = if raw != 0 \{ raw \} else \{ NAK_OCC_CEILING\.with\(\|c\| c\.get\(\)\) \};", add):
        probs.append("the cap is not the RAW budget first, then the try's ceiling")
    if not re.search(r"if raw != 0 \{ guard\.verify\(", add):
        probs.append("the RAW budget's check does not stay with the RAW budget")
    return probs


def ceiling_fn(add):
    m = re.search(r"^fn nak_occ_ceiling\(.*?^\}", add, re.S | re.M)
    return m.group(0) if m else None


def run_ceiling(src):
    """-> (ok, detail): nak_occ_ceiling against NAK's max_warps_per_sm (ir.rs), for SM 120's 48 warps and 2 reserved."""
    stub = r'''
use std::cmp::{max, min};
struct ShaderModelInfo { warps_per_sm: u8 }
impl ShaderModelInfo { fn hw_reserved_gprs(&self) -> u32 { 2 } }
fn prev_multiple_of(x: u32, m: u32) -> u32 { x - x % m }
fn max_warps_per_sm(sm: &ShaderModelInfo, gprs: u32) -> u32 {
    let total_regs: u32 = 65536;
    let gprs = max(gprs, 1).next_multiple_of(8);
    min(prev_multiple_of((total_regs / 32) / gprs, 4), sm.warps_per_sm.into())
}
fn main() {
    let sm = ShaderModelInfo { warps_per_sm: 48 };
    for g in 1..=256u32 {
        let w = max_warps_per_sm(&sm, g);
        let want = (34..=256u32).rev().filter(|c| c % 8 == 0).find(|&c| max_warps_per_sm(&sm, c) > w);
        let got = nak_occ_ceiling(&sm, w);
        if got != want { println!("BAD {} {} {:?} {:?}", g, w, got, want); }
    }
    println!("{:?} {:?} {:?}", nak_occ_ceiling(&sm, 16), nak_occ_ceiling(&sm, 24), nak_occ_ceiling(&sm, 48));
}
'''
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "t.rs").write_text(stub + "\n" + src + "\n")
        r = subprocess.run([RUSTC, "--edition", "2021", "-O", "-A", "dead_code", "-o", str(Path(d) / "t"),
                            str(Path(d) / "t.rs")], capture_output=True, text=True)
        if r.returncode:
            return False, r.stderr
        out = subprocess.run([str(Path(d) / "t")], capture_output=True, text=True).stdout.splitlines()
    bad = [l for l in out if l.startswith("BAD")]
    if bad:
        return False, "; ".join(bad[:4])
    if out[-1:] != ["Some(96) Some(72) None"]:
        return False, f"{out[-1:]}"
    return True, ""


def key_has(text):
    return re.search(r'^\+   "NAK_OCC_TARGET",$', text, re.M) is not None


def check(patch, keypatch, label=""):
    """-> number of arms that failed for these texts (the chain and docs arms are run once, outside)."""
    global fails
    before = fails
    add = added(patch)
    p = switch_problems(add)
    arm(label + "NAK_OCC_TARGET is read once and only 0 turns it off", not p, p)
    arm(label + "the shader cache key table has NAK_OCC_TARGET", key_has(keypatch))
    p = pick_problems(add)
    arm(label + "the pick is guarded and gated as described", not p, p)
    p = cap_problems(add)
    arm(label + "the ceiling goes to the RA budget only without a RAW budget", not p, p)
    src = ceiling_fn(add)
    if src is None:
        arm(label + "nak_occ_ceiling is in the patch", False)
    elif RUSTC:
        ok, d = run_ceiling(src)
        arm(label + "nak_occ_ceiling gives the largest register count that gains warps", ok, d)
    return fails - before


arm("nvk/nvk-nak-occ-target.patch exists", PATCH.is_file())
if not PATCH.is_file():
    print("1 FAILED")
    sys.exit(1)
text, keytext = PATCH.read_text(), KEYPATCH.read_text()
ch = chains()
arm("the patch is the last of the 3.0 chain, right after nvk-nak-stats",
    ch.get("3.0", [])[-2:] == ["nvk-nak-stats", "nvk-nak-occ-target"], ch.get("3.0", [])[-3:])
arm("the patch is in no other chain", all("nvk-nak-occ-target" not in v for k, v in ch.items() if k != "3.0"))
files = re.findall(r"^diff --git a/(\S+) ", text, re.M)
arm("the patch changes api.rs alone", files == ["src/nouveau/compiler/nak/api.rs"], files)
reg = (ROOT / "tools/switches.tsv").read_text()
arm("NAK_OCC_TARGET is an escape row of tools/switches.tsv, on by default",
    re.search(r"^env\tNAK_OCC_TARGET\tescape\ton \(NAK_OCC_TARGET=0", reg, re.M) is not None)
cl = (ROOT / "CHANGELOG.md").read_text()
sec = re.search(r"^## 3\.0\.15\b(.*?)(?=^## )", cl, re.S | re.M)
arm("CHANGELOG.md has a 3.0.15 section that names NAK_OCC_TARGET=0", bool(sec) and "NAK_OCC_TARGET=0" in sec.group(1))
st = (ROOT / "docs/STATUS.md").read_text()
arm("docs/STATUS.md says host-tested only for 3.0.15",
    re.search(r"3\.0\.15.*?host-tested only", st, re.S) is not None)
if not RUSTC:
    print("skip the ceiling arm: no rustc")
    skipped += 1
check(text, keytext)

# --- controls -----------------------------------------------------------------------------------------------------
print("controls (each has to fail):")
CONTROLS = (
    ("the default turned off", 'map_or(true, |v| v != "0")', 'map_or(false, |v| v != "0")', False),
    ("compute added to the stages", "+    stage == MESA_SHADER_VERTEX\n",
     "+    stage == MESA_SHADER_COMPUTE || stage == MESA_SHADER_VERTEX\n", False),
    ("the 5 % gate raised", "const NAK_OCC_MAX_GROWTH_PCT: u64 = 5;", "const NAK_OCC_MAX_GROWTH_PCT: u64 = 50;", False),
    ("the warp check dropped", "|| c.info.max_warps_per_sm <= warps", "", False),
    ("the key table without the switch", '+   "NAK_OCC_TARGET",\n', "", True),
    ("a ceiling one step too low", "return Some(g);", "return Some(g - 8);", False),
    ("the pick run in WindowServer too", "+        && !nak_occ_in_windowserver()\n", "", False),
    ("WindowServer's name misspelt", '|n| n == "WindowServer"', '|n| n == "Windowserver"', False),
)
for name, old, new, in_key in CONTROLS:
    src = keytext if in_key else text
    if old not in src:
        arm(f"control: {name}: the text to break is in the patch", False, old)
        continue
    broken = src.replace(old, new, 1)
    real, with_out = fails, sys.stdout
    sys.stdout = open("/dev/null", "w")
    try:
        n = check(text if in_key else broken, broken if in_key else keytext, "  ")
    finally:
        sys.stdout.close()
        sys.stdout = with_out
    fails = real
    if name == "a ceiling one step too low" and not RUSTC:
        continue
    arm(f"control: {name} fails", n > 0)
print("all passed" if not fails and not skipped else f"{fails} FAILED" if fails else "passed, with arms skipped")
sys.exit(1 if fails else 2 if skipped else 0)
