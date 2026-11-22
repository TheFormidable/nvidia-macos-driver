#!/usr/bin/env python3
"""With NAK_TEX_UHANDLE=1 a texture whose handle left its uniform register still compiles (nvk/nvk-tex-uhandle-safe.patch).

NAK_TEX_UHANDLE=1 (off by default) keeps the handle of a TEX in a uniform register, and the encoder asserts that it is
one (sm70_encode.rs, set_ureg). Two passes could give that source a warp register: opt_uniform_instrs, for a handle
loaded by an instruction that cannot be uniform, and spill_values, for a spilled uniform value used in a non-uniform
block. The compile then panicked and the pipeline failed. The patch puts such a texture back into the layout from_nir
builds for a handle that is not uniform, in OpTex's legalize and in assign_regs after the uniform registers were
spilled. No Mesa tree is needed here:

  the code     the two pieces of Rust the patch adds (OpTex::demote_handle and the loop in assign_regs) are cut out of
               the patch and built with rustc against stand-ins for the few NAK types they use, then run over every
               shape of sources a TEX can have at that point (one to four coordinates, split or not, up to three
               components after the handle, values repeated the two ways legalize leaves them repeated: in a source
               of one component, and as one whole vector in both sources) with a handle that is Zero, in a UGPR, or
               in a GPR. A Zero or UGPR handle leaves the block as it was, without a second liveness. A GPR handle
               gives from_nir's fallback layout: the handle first in the second source, the .SCR split undone, no
               vector longer than four, no value twice among the sources of more than one component, each copy placed
               just before its TEX, and the liveness and the most values live at once computed again from the
               function as it then is. Then two textures with a GPR handle in one block, and two such blocks with
               one without between them: every one of the textures goes back, and liveness runs once more, not twice.
  controls     the same code with one rule broken each: a repeated value left without its copy, a source of one
               component given copies, the split kept, the coordinates not joined again, the handle put last, the
               handle left in its place as well, a UGPR handle demoted, liveness not computed again, liveness computed
               again every time, the most values live at once left as it was, a block rebuilt although it has no
               uniform handle, only the first such texture put back, only the first such block rebuilt. Every one of
               them has to fail the run.
  the text     the patch only adds lines, to assign_regs.rs and sm70_encode.rs alone; OpTex's legalize is
               demote_handle, then legalize_tex_instr, and nothing after it; the loop in assign_regs
               follows the spill of the uniform files and comes before the GPR limit is taken from the liveness; the
               layout is built with the statements of from_nir's fallback (nvk/nvk-macos.patch); and the patch sits
               in the 3.0 chain once, after every other patch that changes those two files, and not in the chains of
               2.9 and 2.8.

What this does not show: that the encoder's uniform-handle form is right on a card (nothing here runs a GPU), and that
no third pass can move the handle; the two known ones were found by reading NAK and reproduced on a Linux build of
this NVK. Without rustc the code arms are skipped and the test says so (exit 2).
Usage: test_nak_tex_uhandle_safe.py"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.dont_write_bytecode = True
import switch_registry as sr  # noqa: E402  (the chain as build/build_nvk.sh applies it)

NAME = "nvk-tex-uhandle-safe"
NAK = "src/nouveau/compiler/nak/"
FILES = [NAK + "assign_regs.rs", NAK + "sm70_encode.rs"]
RUSTC = shutil.which("rustc")
fails = 0


def arm(name, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + name + (f" ({str(detail).strip()[-600:]})" if detail and not ok else ""))
    fails += not ok


# Stand-ins for the NAK types the patch's code touches. Their rules are NAK's: an SSARef holds one to four values, a
# Src converts from a reference or a value, an instruction is built from an op.
HARNESS = r'''
#![allow(dead_code, unused_variables, unused_assignments, unused_mut)]
use std::cell::Cell;
use std::collections::HashSet;
use std::ops::{Deref, DerefMut};
type FxHashSet<T> = HashSet<T>;
#[derive(Clone, Copy, PartialEq, Eq, Hash, Debug)]
enum RegFile { GPR, UGPR }
#[derive(Clone, Copy, PartialEq, Eq, Hash, Debug)]
struct SSAValue { idx: u32, file: RegFile }
impl SSAValue { fn file(&self) -> RegFile { self.file } }
#[derive(Clone, PartialEq, Eq, Debug)]
struct SSARef(Vec<SSAValue>);
impl SSARef {
    fn comps(&self) -> u8 { self.0.len() as u8 }
    fn file(&self) -> RegFile { assert!(self.0.iter().all(|v| v.file == self.0[0].file)); self.0[0].file }
}
impl Deref for SSARef { type Target = [SSAValue]; fn deref(&self) -> &[SSAValue] { &self.0 } }
impl DerefMut for SSARef { fn deref_mut(&mut self) -> &mut [SSAValue] { &mut self.0 } }
impl TryFrom<&[SSAValue]> for SSARef {
    type Error = &'static str;
    fn try_from(c: &[SSAValue]) -> Result<Self, Self::Error> {
        if c.is_empty() { Err("Empty vector") } else if c.len() > 4 { Err("Too many vector components") } else { Ok(SSARef(c.to_vec())) }
    }
}
#[derive(Clone, PartialEq, Eq, Debug)]
enum SrcRef { Zero, SSA(SSARef) }
#[derive(Clone, PartialEq, Eq, Debug)]
struct Src { src_ref: SrcRef }
impl Src { fn is_zero(&self) -> bool { self.src_ref == SrcRef::Zero } }
impl From<SrcRef> for Src { fn from(r: SrcRef) -> Src { Src { src_ref: r } } }
impl From<SSARef> for Src { fn from(r: SSARef) -> Src { Src { src_ref: SrcRef::SSA(r) } } }
impl From<SSAValue> for Src { fn from(v: SSAValue) -> Src { Src { src_ref: SrcRef::SSA(SSARef(vec![v])) } } }
#[derive(Clone, PartialEq, Eq, Debug)]
enum Dst { None, SSA(SSAValue) }
impl From<SSAValue> for Dst { fn from(v: SSAValue) -> Dst { Dst::SSA(v) } }
#[derive(Clone, PartialEq, Eq, Debug)]
struct OpTex { srcs: [Src; 2], handle: Src, scalar: bool }
#[derive(Clone, PartialEq, Eq, Debug)]
struct OpCopy { dst: Dst, src: Src }
#[derive(Clone, PartialEq, Eq, Debug)]
enum Op { Tex(Box<OpTex>), Copy(Box<OpCopy>), Other(u32) }
impl From<OpCopy> for Op { fn from(o: OpCopy) -> Op { Op::Copy(Box::new(o)) } }
#[derive(Clone, PartialEq, Eq, Debug)]
struct Instr { op: Op }
impl Instr { fn new(op: impl Into<Op>) -> Instr { Instr { op: op.into() } } }
struct Block { instrs: Vec<Instr> }
struct Alloc { next: u32 }
impl Alloc { fn alloc(&mut self, file: RegFile) -> SSAValue { self.next += 1; SSAValue { idx: self.next - 1, file } } }
struct Function { blocks: Vec<Block>, ssa_alloc: Alloc, liveness_runs: Cell<u32> }
// A liveness knows which run it is, and what is asked of it afterwards carries that number: the most values live at
// once that RA sizes its registers by has to come from the liveness taken after the last change.
struct SimpleLiveness { run: u32 }
impl SimpleLiveness {
    fn for_function(f: &Function) -> SimpleLiveness {
        f.liveness_runs.set(f.liveness_runs.get() + 1);
        SimpleLiveness { run: f.liveness_runs.get() }
    }
    fn calc_max_live(&self, f: &Function) -> u32 { self.run }
}

//@DEMOTE@

// -> the run of the liveness that max_live is from when the patch's code is done
fn hook(f: &mut Function) -> u32 {
    let mut live = SimpleLiveness::for_function(f);
    let mut max_live = live.calc_max_live(f);
//@HOOK@
    let _ = &live;
    max_live
}

const FRESH: u32 = 1000;   // values the hook allocates start here
fn val(i: u32) -> SSAValue { SSAValue { idx: i, file: RegFile::GPR } }
fn src(v: &[u32]) -> Src { if v.is_empty() { SrcRef::Zero.into() } else { SSARef(v.iter().map(|i| val(*i)).collect()).into() } }
fn comps(s: &Src) -> Vec<SSAValue> { match &s.src_ref { SrcRef::SSA(v) => v.to_vec(), SrcRef::Zero => vec![] } }

// every list of n values out of 0..pool
fn lists(n: usize, pool: u32) -> Vec<Vec<u32>> {
    let mut out = vec![vec![]];
    for _ in 0..n { out = out.iter().flat_map(|l| (0..pool).map(move |v| { let mut l = l.clone(); l.push(v); l })).collect(); }
    out
}
// what legalize leaves: no value twice among the sources of more than one component, unless the two sources are
// one and the same vector, which it keeps as it is
fn legal(s0: &[u32], s1: &[u32]) -> bool {
    let mut seen = HashSet::new();
    [s0, s1].iter().filter(|s| s.len() > 1).all(|s| s.iter().all(|v| seen.insert(*v)))
        || (s0.len() > 1 && s0 == s1 && s0.iter().all(|v| s0.iter().filter(|w| *w == v).count() == 1))
}

fn main() {
    let (mut cases, mut bad) = (0u32, 0u32);
    let mut say = |what: &str, s0: &[u32], s1: &[u32], scalar: bool, kind: u32| {
        bad += 1;
        if bad <= 12 { println!("bad: {} (srcs {:?} {:?} scalar {} handle {})", what, s0, s1, scalar, kind); }
    };
    // (coordinates, the second source without the handle, whether lower_tex split the coordinates)
    let mut shapes: Vec<(usize, usize, bool)> = vec![(1, 1, true), (2, 1, true), (2, 2, true)];
    for n0 in 1..=4 { for n1 in 0..=3 { shapes.push((n0, n1, false)); } }
    for (n0, n1, scalar) in shapes {
        for s0 in lists(n0, 5) {
            for s1 in lists(n1, 6) {
                if !legal(&s0, &s1) { continue; }
                for kind in 0..3u32 {           // the handle: 0 Zero, 1 in a UGPR, 2 in a GPR
                    cases += 1;
                    let h = SSAValue { idx: 900, file: if kind == 1 { RegFile::UGPR } else { RegFile::GPR } };
                    let handle: Src = if kind == 0 { SrcRef::Zero.into() } else { h.into() };
                    let tex = OpTex { srcs: [src(&s0), src(&s1)], handle, scalar };
                    let plain = OpTex { srcs: [src(&[7]), src(&[8])], handle: SrcRef::Zero.into(), scalar: false };
                    let before = vec![Instr { op: Op::Other(1) }, Instr { op: Op::Tex(Box::new(tex.clone())) }, Instr { op: Op::Other(2) }];
                    let quiet = vec![Instr { op: Op::Other(3) }, Instr { op: Op::Tex(Box::new(plain)) }];
                    let mut f = Function { blocks: vec![Block { instrs: before.clone() }, Block { instrs: quiet.clone() }],
                                           ssa_alloc: Alloc { next: FRESH }, liveness_runs: Cell::new(0) };
                    let quiet_at = f.blocks[1].instrs.as_ptr();
                    let max_live_from = hook(&mut f);
                    let runs = f.liveness_runs.get();
                    if f.blocks[1].instrs != quiet || f.blocks[1].instrs.as_ptr() != quiet_at {
                        say("a block with no uniform handle was rebuilt", &s0, &s1, scalar, kind);
                    }
                    let after = &f.blocks[0].instrs;
                    if kind != 2 {
                        if *after != before { say("a Zero or UGPR handle changed the block", &s0, &s1, scalar, kind); }
                        if runs != 1 || max_live_from != 1 { say("liveness was computed again for nothing", &s0, &s1, scalar, kind); }
                        continue;
                    }
                    if runs != 2 { say("liveness was not computed again", &s0, &s1, scalar, kind); }
                    if max_live_from != runs { say("the most values live at once is from the liveness before the change", &s0, &s1, scalar, kind); }
                    let n = after.len();
                    if n < 3 || after[0] != before[0] || after[n - 1] != before[2] { say("the other instructions moved", &s0, &s1, scalar, kind); continue; }
                    let t = match &after[n - 2].op { Op::Tex(t) => t, _ => { say("the TEX is not where it was", &s0, &s1, scalar, kind); continue; } };
                    // the copies, all just before the TEX: fresh value <- original
                    let mut from = std::collections::HashMap::new();
                    let mut copies_ok = true;
                    for i in &after[1..n - 2] {
                        match &i.op {
                            Op::Copy(c) => match (&c.dst, comps(&c.src).as_slice()) {
                                (Dst::SSA(d), [s]) if d.idx >= FRESH && s.idx < FRESH && d.file == s.file => { copies_ok &= from.insert(*d, *s).is_none(); }
                                _ => copies_ok = false,
                            },
                            _ => copies_ok = false,
                        }
                    }
                    if !copies_ok { say("something other than a copy of an old value stands before the TEX", &s0, &s1, scalar, kind); }
                    if !t.handle.is_zero() || t.scalar { say("the handle or the split was kept", &s0, &s1, scalar, kind); }
                    let (g0, g1) = (comps(&t.srcs[0]), comps(&t.srcs[1]));
                    if g0.len() > 4 || g1.len() > 4 { say("a vector is longer than four", &s0, &s1, scalar, kind); }
                    // from_nir's fallback layout, in the old values
                    let mut want0: Vec<SSAValue> = s0.iter().map(|i| val(*i)).collect();
                    let mut want1 = vec![h];
                    if scalar { want0.extend(s1.iter().map(|i| val(*i))); } else { want1.extend(s1.iter().map(|i| val(*i))); }
                    let old = |v: &SSAValue| *from.get(v).unwrap_or(v);
                    if g0.iter().map(old).collect::<Vec<_>>() != want0 || g1.iter().map(old).collect::<Vec<_>>() != want1 {
                        say("the layout is not from_nir's fallback", &s0, &s1, scalar, kind);
                    }
                    // RA's rule, and no copy that was not needed
                    let mut seen = HashSet::new();
                    let mut dups = 0;
                    let mut wanted = HashSet::new();
                    for (got, want) in [(&g0, &want0), (&g1, &want1)] {
                        if got.len() > 1 {
                            if !got.iter().all(|v| seen.insert(*v)) { say("a value is twice among the vector sources", &s0, &s1, scalar, kind); }
                            dups += want.iter().filter(|v| !wanted.insert(**v)).count();
                        }
                    }
                    let used = g0.iter().chain(g1.iter()).filter(|v| v.idx >= FRESH).count();
                    if from.len() != dups || used != dups { say("the copies are not one for each repeated value", &s0, &s1, scalar, kind); }
                }
            }
        }
    }
    // two textures in one block, the second with nothing to do
    let h = val(900);
    let a = OpTex { srcs: [src(&[1]), src(&[1])], handle: h.into(), scalar: true };
    let b = OpTex { srcs: [src(&[2, 3]), src(&[])], handle: SSAValue { idx: 901, file: RegFile::UGPR }.into(), scalar: false };
    let mut f = Function { blocks: vec![Block { instrs: vec![Instr { op: Op::Tex(Box::new(a)) }, Instr { op: Op::Tex(Box::new(b.clone())) }] }],
                           ssa_alloc: Alloc { next: FRESH }, liveness_runs: Cell::new(0) };
    let max_live_from = hook(&mut f);
    cases += 1;
    let i = &f.blocks[0].instrs;
    if i.len() != 3 || !matches!(&i[0].op, Op::Copy(_)) || i[2].op != Op::Tex(Box::new(b)) || f.liveness_runs.get() != 2 || max_live_from != 2 {
        bad += 1; println!("bad: two textures in one block");
    }
    // two textures in one block, each with its handle in a GPR and an instruction between them: both go back
    let done = |s0: Src, s1: Src| Instr { op: Op::Tex(Box::new(OpTex { srcs: [s0, s1], handle: SrcRef::Zero.into(), scalar: false })) };
    let (h0, h1) = (val(900), val(901));
    let a = OpTex { srcs: [src(&[1]), src(&[1])], handle: h0.into(), scalar: true };
    let b = OpTex { srcs: [src(&[2, 3]), src(&[4])], handle: h1.into(), scalar: false };
    let mut f = Function { blocks: vec![Block { instrs: vec![Instr { op: Op::Tex(Box::new(a)) }, Instr { op: Op::Other(5) }, Instr { op: Op::Tex(Box::new(b)) }] }],
                           ssa_alloc: Alloc { next: FRESH }, liveness_runs: Cell::new(0) };
    let max_live_from = hook(&mut f);
    cases += 1;
    let fresh = SSAValue { idx: FRESH, file: RegFile::GPR };
    let want = vec![Instr::new(OpCopy { dst: fresh.into(), src: val(1).into() }),
                    done(SSARef(vec![val(1), fresh]).into(), h0.into()),
                    Instr { op: Op::Other(5) },
                    done(src(&[2, 3]), SSARef(vec![h1, val(4)]).into())];
    if f.blocks[0].instrs != want || f.liveness_runs.get() != 2 || max_live_from != 2 {
        bad += 1; println!("bad: two textures with a GPR handle in one block: {:?}", f.blocks[0].instrs);
    }
    // two blocks with such a texture and one without between them: each is rebuilt, and liveness runs once more, not twice
    let tex = |c: u32, h: u32| Instr { op: Op::Tex(Box::new(OpTex { srcs: [src(&[c, c + 1]), src(&[])], handle: val(h).into(), scalar: false })) };
    let quiet = vec![Instr { op: Op::Other(3) }];
    let mut f = Function { blocks: vec![Block { instrs: vec![tex(1, 900)] }, Block { instrs: quiet.clone() },
                                        Block { instrs: vec![Instr { op: Op::Other(4) }, tex(5, 901)] }],
                           ssa_alloc: Alloc { next: FRESH }, liveness_runs: Cell::new(0) };
    let quiet_at = f.blocks[1].instrs.as_ptr();
    let max_live_from = hook(&mut f);
    cases += 1;
    if f.blocks[0].instrs != vec![done(src(&[1, 2]), val(900).into())] || f.blocks[1].instrs != quiet || f.blocks[1].instrs.as_ptr() != quiet_at
        || f.blocks[2].instrs != vec![Instr { op: Op::Other(4) }, done(src(&[5, 6]), val(901).into())]
        || f.liveness_runs.get() != 2 || max_live_from != 2 {
        bad += 1; println!("bad: two blocks with a GPR handle: {:?} / {:?}", f.blocks[0].instrs, f.blocks[2].instrs);
    }
    println!("cases {} bad {}", cases, bad);
}
'''

PATCH = ROOT / "nvk" / (NAME + ".patch")
patch = PATCH.read_text() if PATCH.is_file() else ""
bld = (ROOT / "build/build_nvk.sh").read_text()
arm(f"nvk/{NAME}.patch is there", patch)


def section(path, keep):
    """the hunk lines of one file whose first character is in keep, that character stripped"""
    out, cur = [], None
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            cur = None
        elif line.startswith("+++ "):
            cur = re.sub(r"^b/", "", line[4:].split("\t")[0].strip())
        elif cur == path and not line.startswith("--- ") and not line.startswith("@@") and line[:1] in keep:
            out.append(line[1:])
    return out


enc_added = "\n".join(section(NAK + "sm70_encode.rs", "+")) + "\n"
ra_added = section(NAK + "assign_regs.rs", "+")
demote = re.search(r"(?m)^impl OpTex \{\n.*?^\}\n", enc_added, re.S)
start = next((i for i, l in enumerate(ra_added) if l.strip() == "let mut demoted = false;"), None)
hook = "\n".join(ra_added[start:]) + "\n" if start is not None else None
arm("the patch has OpTex::demote_handle and the loop in assign_regs", demote and hook, patch[:300])


def run(d, tag, demote_text, hook_text):
    src = HARNESS.replace("//@DEMOTE@", demote_text).replace("//@HOOK@", hook_text)
    Path(d, f"{tag}.rs").write_text(src)
    b = subprocess.run([RUSTC, "--edition", "2021", "-C", "debuginfo=0", "-o", f"{d}/{tag}", f"{d}/{tag}.rs"],
                       capture_output=True, text=True)
    if b.returncode:
        return "does not build", b.stderr
    r = subprocess.run([f"{d}/{tag}"], capture_output=True, text=True)
    return ("ran" if r.returncode == 0 else f"exit {r.returncode}"), r.stdout + r.stderr[-800:]


if demote and hook and RUSTC:
    with tempfile.TemporaryDirectory() as d:
        status, out = run(d, "real", demote.group(0), hook)
        m = re.search(r"cases (\d+) bad (\d+)", out)
        arm("the patch's code builds against the stand-ins and runs", status == "ran" and m, f"{status}\n{out}")
        arm("every shape of sources, with a Zero, a UGPR and a GPR handle: the block is left alone or rebuilt as from_nir's "
            "fallback, with a copy for each repeated value and no other", m and int(m.group(1)) > 10000 and m.group(2) == "0", out)
        if m:
            print(f"     {m.group(1)} cases")
        D, H = demote.group(0), hook
        controls = [
            ("a repeated value gets no copy", D, H, "if !seen.insert(*c) {", "if !seen.insert(*c) && false {"),
            ("a source of one component is given copies too", D, H, "if vec.comps() == 1 {", "if false {"),
            ("the .SCR split is kept", D, H, "            self.scalar = false;\n", ""),
            ("the coordinates are not joined again", D, H, "            c0.extend(c1.drain(..));\n", ""),
            ("the handle goes last in the second source", D, H, "        let mut n1: Vec<SSAValue> = h.iter().copied().collect();\n        n1.extend(c1);\n",
             "        let mut n1: Vec<SSAValue> = c1;\n        n1.extend(h.iter().copied());\n"),
            ("the handle is left in its place as well", D, H, "        self.handle = SrcRef::Zero.into();\n", ""),
            ("a handle in a UGPR is demoted too", D, H, "SrcRef::SSA(h) if h.file() != RegFile::UGPR => h.clone(),", "SrcRef::SSA(h) => h.clone(),"),
            ("liveness is not computed again", D, H, "        if demoted {\n", "        if demoted && false {\n"),
            ("liveness is computed again every time", D, H, "        if demoted {\n", "        if true {\n"),
            ("the most values live at once is left as it was before the change", D, H, "            max_live = live.calc_max_live(f);\n", ""),
            ("a block with no uniform handle is rebuilt", D, H, "if !tex.handle.is_zero())) {\n                continue;",
             "if !tex.handle.is_zero())) {\n                let _ = 0;"),
            ("only the first texture with such a handle is put back", D, H, "                    if tex.demote_handle() {\n",
             "                    if !demoted && tex.demote_handle() {\n"),
            ("only the first block with such a texture is rebuilt", D, H, "            b.instrs = instrs;\n",
             "            b.instrs = instrs;\n            if demoted {\n                break;\n            }\n"),
        ]
        for i, (what, dm, hk, old, new) in enumerate(controls):
            if (dm + hk).count(old) != 1:
                arm(f"control ({what}): the text it breaks is in the patch once", False, repr(old))
                continue
            status, out = run(d, f"c{i}", dm.replace(old, new), hk.replace(old, new))
            m = re.search(r"cases (\d+) bad (\d+)", out)
            arm(f"control ({what}): the run fails", status != "ran" or (m and m.group(2) != "0"), f"{status}\n{out[-300:]}")

# ---- the text
minus = [l for l in patch.split("\ndiff --git ", 1)[-1].splitlines() if l.startswith("-") and not l.startswith("--- ")]
arm("the patch only adds lines: the encoder, its assertion and every other pass are as they were", not minus, minus[:3])
arm("it changes assign_regs.rs and sm70_encode.rs alone, neither a file of the three upstream-fix patches",
    re.findall(r"(?m)^diff --git a/(\S+)", patch) == FILES, re.findall(r"(?m)^diff --git a/(\S+)", patch))
# as tools/test_nvk_chain.py has it for the upstream patches: patch(1), the BSD one of macOS above all, takes a header
# line that looks like a diff, or starts with a digit, for part of the patch
risky = [l for l in patch.partition("\ndiff --git ")[0].splitlines()
         if re.match(r"[ \tX]*(--- |\+\+\+ |@@|\*\*\* |Index:|Prereq:|diff |[<>] |\.$|\d)", l)]
arm("no line of the patch's header can be taken for part of a patch", not risky, risky)
enc = section(NAK + "sm70_encode.rs", " +")
A_LEGALIZE = "OpTex's legalize is demote_handle, then legalize_tex_instr, and nothing else"
if "impl SM70Op for OpTex {" in enc:
    i = enc.index("impl SM70Op for OpTex {")
    stmts = [l.strip() for l in enc[i + 2:] if l.strip() and not l.strip().startswith("//")]
    arm(A_LEGALIZE, enc[i + 1].strip() == "fn legalize(&mut self, b: &mut LegalizeBuilder) {"
        and stmts[:3] == ["self.demote_handle();", "legalize_tex_instr(self, b);", "}"],
        f"its statements in the patch are {stmts[:4]}")
else:
    arm(A_LEGALIZE, False, "no hunk of the patch shows `impl SM70Op for OpTex {`")
body = demote.group(0) if demote else ""
first = body.split("pub fn demote_handle(&mut self) -> bool {", 1)[-1].strip().split(";")[0]
arm("demote_handle returns false before it writes anything, for a Zero handle and for one in a UGPR",
    re.fullmatch(r"let h = match &self\.handle\.src_ref \{\s*SrcRef::SSA\(h\) if h\.file\(\) != RegFile::UGPR => h\.clone\(\),"
                 r"\s*_ => return false,\s*\}", first) is not None, first)
macos = (ROOT / "nvk/nvk-macos.patch").read_text()
same = ["if let SrcRef::SSA(v) = &{}.src_ref {{ c0.extend(v.iter().copied()); }}", "c0.extend(c1.drain(..));", "n1.extend(c1);"]
arm("the layout is built with the statements of from_nir's fallback (nvk/nvk-macos.patch)",
    all(s.format("self.srcs[0]") in body and s.format("src0") in macos for s in same)
    and "let mut n1 = hs;" in macos and "let mut n1: Vec<SSAValue> = h.iter().copied().collect();" in body,
    [s for s in same if s.format("self.srcs[0]") not in body or s.format("src0") not in macos])
ra = section(NAK + "assign_regs.rs", " +")
try:
    a = next(i for i, l in enumerate(ra) if l.strip().startswith("// " + NAME))
    g = next(i for i, l in enumerate(ra) if "An instruction can have at most 4 vector sources" in l)
    arm("the loop in assign_regs follows the spill of the uniform files and comes before the GPR limit",
        a < g and ra[a - 3:a] == ["            }", "        }", ""] and ra[g - 1] == "" and ra[g - 2] == "        }"
        and "@@ -1444,6 +1444," in patch, ra[max(a - 3, 0):a] + ra[g - 2:g + 1])
except StopIteration:
    arm("the loop in assign_regs follows the spill of the uniform files and comes before the GPR limit", False, "not found")
chain, _copied, problems = sr.nvk_chain(ROOT)
at = [i for i, p in enumerate(chain) if p == PATCH]
changes = re.compile(r"(?m)^\+\+\+ b/(?:" + "|".join(re.escape(f) for f in FILES) + r")(\t.*)?$")
touching = [p.relative_to(ROOT / "nvk").as_posix() for p in (chain[at[0] + 1:] if at else [])
            if p.is_file() and changes.search(p.read_text(errors="replace"))]
arm("the patch is in the 3.0 chain once, after every other patch that changes those two files",
    not problems and len(at) == 1 and not touching,
    f"{', '.join(touching)} changes one of them after it: put {NAME} after it in the chain" if touching else "not in the chain once")
arm("the 2.9 and 2.8 chains do not have it", sum(NAME in l for l in bld.splitlines() if 'CHAIN="' in l) == 1)

if fails:
    print("FAILED")
    sys.exit(1)
if not RUSTC:
    print("SKIP: the text arms pass; the code arms need rustc")
    sys.exit(2)
print("PASS")
