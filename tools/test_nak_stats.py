#!/usr/bin/env python3
"""NAK_STATS=1 writes one JSON line per compiled shader stage, and the corpus and the diff tool read them
(nvk/nvk-nak-stats.patch, tools/nak-corpus/, tools/nak_stats_diff.py). No Mesa tree is needed here:

  the chain    the patch follows nvk-tex-uhandle-safe in the 3.0 chain (only nvk-nak-occ-target after it), is in no
               other, changes src/nouveau/compiler/nak/api.rs alone, and only adds lines
  the switch   the added code reads NAK_STATS once, through a OnceLock, and is on only for the value 1; its one
               eprintln! and its one call of nak_stats_line are inside `if nak_stats_on()`; NAK_STATS has a row in
               tools/switches.tsv and is among the switches the cache-key test exempts
  the fields   the names the patch writes, in their order, are the FIELDS tools/nak_stats_diff.py reads
  the code     nak_stats_line is cut out of the patch and built with rustc: its line is JSON, the keys come in the
               order nak_stats, stage, sm, blake3, then the fields; the hash is lowercase hex; a stage name with a quote
               or a backslash still gives JSON
  the diff     on synthetic runs: totals and changes, a repeated key numbered, other lines skipped, --gate-instrs and
               --gate-spills on both sides of their limits, exit 2 for a file without a NAK_STATS line
  the corpus   every case names shaders that exist (gen.py's too), and the baseline has one vertex and one fragment
               line for each of the 150 cases
  controls     the same arms over broken copies: the guard removed, a field renamed, the stage filter dropped from
               nak_stats_line, a baseline with a case missing. Each has to fail.

What this does not show: that the figures are right (they are NAK's own counters, read where NAK_DEBUG=print reads them;
the Linux build of this NVK printed the same instruction and cycle counts as pipeline executable statistics), and
anything about a card. Without rustc the code arms are skipped and the test says so (exit 2).
Usage: test_nak_stats.py"""
import contextlib
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.dont_write_bytecode = True
PATCH = ROOT / "nvk/nvk-nak-stats.patch"
SCRIPT = ROOT / "build/build_nvk.sh"
CORPUS = ROOT / "tools/nak-corpus"
RUSTC = shutil.which("rustc")
fails = 0
skipped = 0


def arm(name, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + name + (f" ({str(detail).strip()[-300:]})" if detail and not ok else ""))
    fails += not ok


def chains():
    """-> {release: [patch names]} from the case block of build_nvk.sh."""
    text = SCRIPT.read_text()
    case = re.search(r"case \$REL in\n(.*?)\n\s*esac", text, re.S).group(1)
    return {m.group(1): m.group(2).split() for m in re.finditer(r"^\s*([\d.]+)\).*?CHAIN=\"([^\"]*)\"", case, re.S | re.M)}


def added(text):
    """the + lines of the patch's hunks, without the +"""
    body = text.partition("\ndiff --git ")[2]
    return [l[1:] for l in body.splitlines() if l.startswith("+") and not l.startswith("+++")]


def guard_problems(text):
    add = "\n".join(added(text))
    probs = []
    reads = re.findall(r'env::var(?:_os)?\("NAK_STATS"\)', add)
    if len(reads) != 1:
        probs.append(f"{len(reads)} reads of NAK_STATS")
    if not re.search(r'OnceLock<bool>.*?get_or_init\(\|\| env::var\("NAK_STATS"\)\.map_or\(false, \|v\| v == "1"\)\)', add, re.S):
        probs.append("NAK_STATS is not read once, on for 1 only")
    m = re.search(r"\n(\s*)if nak_stats_on\(\) \{\n(.*?)\n\1\}\n", add, re.S)
    if not m:
        return probs + ["no `if nak_stats_on() {` block"]
    inside, outside = m.group(2), add[:m.start()] + add[m.end():]
    if inside.count("eprintln!") != 1 or "eprintln!" in outside:
        probs.append("the eprintln! is not alone inside the guard")
    if inside.count("nak_stats_line(") != 1 or re.search(r"[^n] nak_stats_line\(|=\s*nak_stats_line\(|\(nak_stats_line\(", outside):
        probs.append("nak_stats_line is called outside the guard")
    return probs


def patch_fields(text):
    m = re.search(r"&\[\n(.*?)\n\s*\],", "\n".join(added(text)), re.S)
    return re.findall(r'\("(\w+)", ', m.group(1)) if m else []


def line_fn(text):
    add = added(text)
    i = next((n for n, l in enumerate(add) if l.startswith("fn nak_stats_line(")), None)
    if i is None:
        return None
    j = next(n for n in range(i, len(add)) if add[n] == "}")
    return "\n".join(add[i:j + 1])


def run_line_fn(src):
    """-> (ok, detail): build nak_stats_line with a main and check what it prints."""
    main = r'''
fn main() {
    let h: Vec<u8> = (0u8..32).collect();
    println!("{}", nak_stats_line("fragment", 120, &h, &[("instrs", 11), ("gprs", 24)]));
    println!("{}", nak_stats_line("we\"ird\\st age", 75, &[0xab, 0x01], &[]));
}
'''
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "t.rs").write_text(src + "\n" + main)
        r = subprocess.run([RUSTC, "--edition", "2021", "-O", "-o", str(Path(d) / "t"), str(Path(d) / "t.rs")],
                           capture_output=True, text=True)
        if r.returncode:
            return False, r.stderr
        out = subprocess.run([str(Path(d) / "t")], capture_output=True, text=True).stdout.splitlines()
    try:
        a, b = (json.loads(x, object_pairs_hook=list) for x in out)
    except ValueError as e:
        return False, f"not JSON: {e}: {out}"
    want = [("nak_stats", 1), ("stage", "fragment"), ("sm", 120), ("blake3", bytes(range(32)).hex()),
            ("instrs", 11), ("gprs", 24)]
    if a != want:
        return False, f"{a}"
    if b != [("nak_stats", 1), ("stage", "weirdstage"), ("sm", 75), ("blake3", "ab01")]:
        return False, f"{b}"
    return True, ""


def write(d, name, recs, extra=()):
    p = Path(d) / name
    p.write_text("".join(f"{x}\n" for x in extra) + "".join(json.dumps(r) + "\n" for r in recs))
    return str(p)


def rec(case, stage, instrs, spills=0, **kw):
    import nak_stats_diff as nd
    r = {"case": case, "nak_stats": 1, "stage": stage, "sm": 120, "blake3": "00"}
    r.update({k: 0 for k in nd.FIELDS})
    r.update(instrs=instrs, instrs_warp=instrs, gprs=24, warps_per_sm=48, spills_to_reg=spills, **kw)
    return r


def diff_arms():
    import nak_stats_diff as nd
    with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        base = [rec("a", "fragment", 100), rec("b", "fragment", 200, spills=2), rec("c", "vertex", 100)]
        up6 = [rec("a", "fragment", 106), rec("b", "fragment", 212, spills=2), rec("c", "vertex", 106)]
        spill = [rec("a", "fragment", 100), rec("b", "fragment", 200, spills=3), rec("c", "vertex", 100)]
        B = write(d, "b.jsonl", base, extra=["noise", '{"other":1}', "{broken"])
        U = write(d, "u.jsonl", up6)
        S = write(d, "s.jsonl", spill)
        lines, totals, rise = nd.compare(nd.load(B), nd.load(U))
        arm("diff: the totals and the change of instructions", totals["instrs"] == (400, 424), totals["instrs"])
        arm("diff: lines that are not NAK_STATS lines are skipped", len(nd.load(B)) == 3, len(nd.load(B)))
        arm("diff: +6 % fails --gate-instrs 5", nd.main([B, U, "--gate-instrs", "5"]) == 1)
        arm("diff: +6 % passes --gate-instrs 7", nd.main([B, U, "--gate-instrs", "7"]) == 0)
        arm("diff: one more spill fails --gate-spills", nd.main([B, S, "--gate-spills"]) == 1)
        arm("diff: the same spills pass --gate-spills", nd.main([B, U, "--gate-spills"]) == 0)
        no_case = [{k: v for k, v in r.items() if k != "case"} for r in base[:2]]
        no_case[1]["stage"] = "fragment"
        twice = nd.load(write(d, "t.jsonl", no_case))
        arm("diff: a key seen twice gets a running number", sorted(twice) == [("fragment", "00"), ("fragment", "00", 1)],
            sorted(twice))
        arm("diff: a file without a NAK_STATS line is exit 2", nd.main([B, write(d, "e.jsonl", [], extra=["x"])]) == 2)


def corpus_problems(baseline_text):
    import nak_stats_diff as nd
    sh = CORPUS / "shaders"
    probs, cases = [], []
    for line in (CORPUS / "cases.txt").read_text().splitlines():
        name, _, vert = line.split("|")
        cases.append(name)
        for f in (sh / f"{name}.frag", sh / f"{vert}.vert"):
            if not f.is_file():
                probs.append(f"{f.name} missing")
    with tempfile.TemporaryDirectory() as d:
        subprocess.run([sys.executable, str(CORPUS / "gen.py"), d], check=True, capture_output=True)
        for line in (Path(d) / "cases.txt").read_text().splitlines():
            name, _, vert = line.split("|")
            cases.append(name)
            if not (Path(d) / "sh" / f"{name}.frag").is_file():
                probs.append(f"gen.py: {name}.frag missing")
            if vert != "quad" and not (Path(d) / "sh" / f"{vert}.vert").is_file():
                probs.append(f"gen.py: {vert}.vert missing")
        recs = nd.load(write(d, "base.jsonl", [], extra=baseline_text.splitlines()))
    if len(cases) != 150 or len(set(cases)) != 150:
        probs.append(f"{len(cases)} cases, {len(set(cases))} names")
    want = {(c, s) for c in cases for s in ("vertex", "fragment")}
    if set(recs) != want:
        probs.append(f"baseline: {len(want - set(recs))} stages missing, {len(set(recs) - want)} extra")
    return probs


# --- the chain --------------------------------------------------------------------------------------------------------
arm("nvk/nvk-nak-stats.patch exists", PATCH.is_file())
text = PATCH.read_text() if PATCH.is_file() else ""
ch = chains()
c30 = ch.get("3.0", [])
arm("the patch is in the 3.0 chain right after nvk-tex-uhandle-safe, and only nvk-nak-occ-target follows it",
    "nvk-nak-stats" in c30 and c30[c30.index("nvk-nak-stats") - 1:] in (["nvk-tex-uhandle-safe", "nvk-nak-stats"],
    ["nvk-tex-uhandle-safe", "nvk-nak-stats", "nvk-nak-occ-target"]), c30[-3:])
arm("the patch is in no other chain", all("nvk-nak-stats" not in v for k, v in ch.items() if k != "3.0"))
files = re.findall(r"^\+\+\+ b/(\S+)", text, re.M)
arm("the patch changes api.rs alone", files == ["src/nouveau/compiler/nak/api.rs"], files)
body = text.partition("\ndiff --git ")[2]
arm("the patch only adds lines", body != "" and not [l for l in body.splitlines() if l.startswith("-") and not l.startswith("---")])

# --- the switch -------------------------------------------------------------------------------------------------------
probs = guard_problems(text)
arm("NAK_STATS is read once, on for 1, and guards the only output", not probs, probs)
reg = (ROOT / "tools/switches.tsv").read_text()
arm("NAK_STATS has a row in tools/switches.tsv", re.search(r"^env\tNAK_STATS\tdiag\t", reg, re.M) is not None)
key = (ROOT / "tools/test_nvk_nak_switch_key.py").read_text()
arm("the cache-key test exempts NAK_STATS", re.search(r'^\s*"NAK_STATS": "', key, re.M) is not None)

# --- the fields -------------------------------------------------------------------------------------------------------
try:
    import nak_stats_diff as nd
    have_diff = True
except ImportError as e:
    have_diff = False
    arm("tools/nak_stats_diff.py imports", False, e)
if have_diff:
    arm("the patch writes the FIELDS nak_stats_diff.py reads, in order", tuple(patch_fields(text)) == nd.FIELDS,
        patch_fields(text))

# --- the code ---------------------------------------------------------------------------------------------------------
src = line_fn(text)
if not RUSTC:
    skipped += 1
elif src is None:
    arm("nak_stats_line is in the patch", False)
else:
    ok, detail = run_line_fn(src)
    arm("nak_stats_line writes the JSON line", ok, detail)

# --- the diff tool and the corpus -------------------------------------------------------------------------------------
if have_diff:
    diff_arms()
    base_path = CORPUS / "baseline-sm120-host.jsonl"
    probs = corpus_problems(base_path.read_text() if base_path.is_file() else "")
    arm("the corpus: 150 cases, their shaders, a baseline line per stage", not probs, probs[:5])

# --- controls ---------------------------------------------------------------------------------------------------------
if text:
    unguarded = text.replace("+    if nak_stats_on() {\n", "+    {\n", 1)
    arm("control: the guard removed fails", unguarded != text and bool(guard_problems(unguarded)))
    renamed = text.replace('("instrs_uniform", ', '("uniform", ', 1)
    arm("control: a field renamed fails", have_diff and renamed != text and tuple(patch_fields(renamed)) != nd.FIELDS)
    if RUSTC and src:
        broken = src.replace(".filter(|c| c.is_ascii_alphanumeric() || *c == '_')", "", 1)
        arm("control: the stage filter dropped fails", broken != src and not run_line_fn(broken)[0])
    if have_diff:
        lines = (CORPUS / "baseline-sm120-host.jsonl").read_text().splitlines()
        arm("control: a baseline with a case missing fails", bool(corpus_problems("\n".join(lines[2:]))))

if fails:
    print(f"{fails} FAILED")
    sys.exit(1)
if skipped:
    print("SKIP: the other arms pass; the code arms need rustc")
    sys.exit(2)
print("all passed")
