#!/usr/bin/env python3
"""The upstream Mesa commits in the NVK patch chain, the releases build_nvk.sh can build, and the two build variables
that change the chain.

2.9 kept the upstream NAK fixes in one opt-in file (NVK_EXTRA_PATCHES=nvk-upstream-nak) that nobody built by default
and that does not apply on the 26.3.0-rc3 base (rc3 already has two of its commits). They are now three patches at
the end of the default chain (release 3.0), each unchanged upstream text: nvk-mesa-26.3.0, nvk-nak-fixes-main,
nvk-nak-sched-main. This test holds:

  the chain    the three follow nvk-diag-nofollow in that order; the "case $REL in ... esac" block of build_nvk.sh,
               evaluated alone (a check that wants the chain without running the build reads it that way), gives the
               whole default chain; every name in it is a file
  2.9          release 2.9 can be built again: its chain is the one of tag v2.9's script, on 2.9's Mesa commit, and
               the script holds the SHA-256 of every file of nvk/ that 2.9's build reads; each equals the file in
               this tree (and tag v2.9's, where the history is here), and a changed one stops the 2.9 build by name
  the record   each patch names its upstream commits in its header, and NOTICE names the same ones
  the files    the three touch only compiler sources that no later patch of the chain touches, so the rest applies
               whichever of them is left out
  the switches NVK_SKIP_PATCHES leaves out exactly the named upstream-fix patches and refuses any other name;
               a name the chain asked for does not have (2.9 and 2.8 have none of the three) stops the build;
               NVK_EXTRA_PATCHES=nvk-upstream-nak (a file of 2.9 that is gone) stops every release with what to do
  the record   the one line the script keeps in BUILD.txt, which is all the release build shows of it, names the
               release and what was left out or added; for the default chain it ends after the export count

The switches are tested on the real script: it is run with a Mesa path that does not exist, so it selects its chain,
prints it, and stops at its first check before it writes anything. The record line is printed after the build, which
cannot run here: its text is checked, and the lines of the script that select the chain are evaluated alone for what
they put at its end."""
import hashlib, os, pathlib, re, shutil, subprocess, sys, tempfile

R = pathlib.Path(__file__).resolve().parents[1]
NVK = R / "nvk"
SCRIPT = R / "build/build_nvk.sh"
bld = SCRIPT.read_text()
notice = (R / "NOTICE").read_text()
FIXES = ["nvk-mesa-26.3.0", "nvk-nak-fixes-main", "nvk-nak-sched-main"]
# release 2.9's chain, as tag v2.9's build/build_nvk.sh has it
CHAIN29 = ("nvk-macos @FIXES nvk-620-kernel nvk-recycle nvk-slab nvk-slabdef nvk-hubfix nvk-perf-boost-burst nvk-rated-tdp "
           "nvk-gps-maxpower nvk-metal-wsi nvk-card-select hwrt/ttu-nak hwrt/ttu-rq curves/curves hwrt/wide nvk-warnings "
           "nvk-unbound-stage nvk-diag-nofollow").split()
V29 = "7412cdd"   # tag v2.9
fails = 0


def arm(name, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + name + (f" ({str(detail).strip()[-300:]})" if detail and not ok else ""))
    fails += not ok


# --- the chain ------------------------------------------------------------------------------------------------------
default_rel = re.search(r"\bREL=\$\{4:-([\w.]+)\}", bld)
arm("the release built when none is asked for is 3.0", default_rel is not None and default_rel.group(1) == "3.0",
    default_rel and default_rel.group(0))
arm("the 2.8 chain is unchanged",
    '       CHAIN="nvk-macos @FIXES nvk-620-kernel nvk-recycle nvk-slab nvk-slabdef" ;;' in bld.splitlines())


def block_chain(rel, text=bld, var="CHAIN"):
    """CHAIN (or another variable) as the release table alone sets it: the lines from 'case $REL in' to 'esac',
    evaluated by bash."""
    m = re.search(r"(?ms)^case \$REL in\n.*?^esac$", text)
    if not m:
        return None
    r = subprocess.run(["bash", "-c", 'REL=$1; NVK=$2; FIXES=; CHAIN=; BASE=; eval "$3" >/dev/null; eval "v=\\${$4}"; printf "%s" "$v"',
                        "_", rel, str(NVK), m.group(0), var], capture_output=True, text=True)
    return r.stdout.split() if r.returncode == 0 else None


def run(rel="3.0", script=SCRIPT, **env):
    """The script up to its first check -> (exit status, output lines, the chain it printed or None)."""
    with tempfile.TemporaryDirectory(prefix="nvkchain-") as t:
        e = {k: v for k, v in os.environ.items() if not k.startswith("NVK_")}
        e.update(env)
        r = subprocess.run(["bash", str(script), t + "/no-mesa", t + "/no-ogkm", t + "/out", rel],
                           capture_output=True, text=True, env=e)
        wrote = os.path.exists(t + "/out")
    lines = r.stdout.splitlines()
    chain = next((l.split(": ", 1)[1].split() for l in lines if l.startswith(f"patch chain ({rel}): ")), None)
    return r.returncode, lines, chain, wrote


default = block_chain("3.0")
at = default.index("nvk-diag-nofollow") + 1 if default and "nvk-diag-nofollow" in default else 0
arm("the release table of build_nvk.sh evaluates on its own and gives the 3.0 chain: 2.9's, then the three patches",
    default is not None and default[:at] == CHAIN29 and default[at:at + 3] == FIXES, default)
arm("control: the same evaluation selects by release (2.8 gives the 2.8 chain)",
    block_chain("2.8") == "nvk-macos @FIXES nvk-620-kernel nvk-recycle nvk-slab nvk-slabdef".split())
arm("control: a table that does not evaluate is seen", block_chain("3.0", bld.replace("\nesac\n", "\nesac_\n", 1)) is None)
missing = [p for p in (default or []) if p != "@FIXES" and not (NVK / (p + ".patch")).is_file()]
arm("every name in the default chain is a patch file in nvk/", default is not None and not missing, missing)
arm("the opt-in file the three replace is gone", not (NVK / "nvk-upstream-nak.patch").exists())

# --- release 2.9, built again ---------------------------------------------------------------------------------------


def git_show(rev, path):
    """A file of an older commit, or None where the history is not here (a shallow clone, a tarball)."""
    try:
        r = subprocess.run(["git", "-C", str(R), "show", f"{rev}:{path}"], capture_output=True)
    except OSError:
        return None
    return r.stdout if r.returncode == 0 else None


arm("the 2.9 chain is release 2.9's, on its own Mesa commit (2.9 and 3.0 share it today; a later nvk/MESA_BASE must not move 2.9)",
    block_chain("2.9") == CHAIN29 and block_chain("2.9", var="BASE") == ["90a959eb"]
    and block_chain("2.9", var="FIXES") == [str(NVK / "nvk-fixes-after-def16.patch")], block_chain("2.9"))
old_script = git_show(V29, "build/build_nvk.sh")
if old_script is None:
    print("note: tag v2.9 is not in this clone's history; the 2.9 chain and files are compared with this test's copy only")
else:
    arm("this test's copy of the 2.9 chain is the one in tag v2.9's build_nvk.sh",
        block_chain("2.9", old_script.decode()) == CHAIN29, block_chain("2.9", old_script.decode()))
m = re.search(r'(?ms)^R29_FILES="\n(.*?)^"$', bld)
sums = dict((f, s) for s, f in re.findall(r"(?m)^([0-9a-f]{64})  (\S+)$", m.group(1))) if m else {}
# what 2.9's build reads from nvk/: its chain and fixes patch, the two kernel-header patches, and every file the script
# copies or runs from there after the chain (WARNINGS is a ceiling and MESA_BASE is replaced by the commit in the table)
after = bld[bld.index("for p in $CHAIN; do\n  f=$NVK/"):] if "for p in $CHAIN; do\n  f=$NVK/" in bld else ""
reads = {c + ".patch" for c in CHAIN29 if c != "@FIXES"} | {"nvk-fixes-after-def16.patch", "ogkm-nvos-620.patch", "ogkm-warnings.patch"}
reads |= {f for f in re.findall(r'\$NVK/([\w./-]+)', after) if f not in ("WARNINGS",)}
arm("the script holds a SHA-256 for every file of nvk/ that the 2.9 build reads, and for nothing else",
    m is not None and set(sums) == reads and len(reads) > 25, sorted(set(sums) ^ reads))
now = [f for f, s in sums.items() if not (NVK / f).is_file() or hashlib.sha256((NVK / f).read_bytes()).hexdigest() != s]
arm("each of them is the file in this tree: 2.9 can be built from it today", sums and not now, now)
if old_script is not None:
    old = [f for f, s in sums.items() if hashlib.sha256(git_show(V29, "nvk/" + f) or b"").hexdigest() != s]
    arm("each SHA-256 is that of the file in tag v2.9", sums and not old, old)
rc, out, chain, wrote = run("2.9")
arm("2.9 builds its own chain and passes the file check (it reaches the Mesa check, for 2.9's commit)",
    rc == 2 and chain == CHAIN29 and any("has no commit 90a959eb" in l for l in out) and not wrote, out)
rc, out, chain, wrote = run("2.9", NVK_EXTRA_PATCHES="one")
arm("an extra patch is still appended to 2.9 (and is named in its record, below)", chain == CHAIN29 + ["one"], chain)


def changed_tree(change, rel="2.9"):
    """A copy of the script and the files of nvk/ it reads for 2.9, one of them changed -> run(rel) of the copy."""
    with tempfile.TemporaryDirectory(prefix="nvkchain-tree-") as d:
        d = pathlib.Path(d)
        (d / "build").mkdir()
        shutil.copy(SCRIPT, d / "build/build_nvk.sh")
        for f in list(sums) + ["MESA_BASE"]:
            (d / "nvk" / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(NVK / f, d / "nvk" / f)
        change(d / "nvk")
        return run(rel, script=d / "build/build_nvk.sh")


rc, out, chain, wrote = changed_tree(lambda n: None)
arm("control: the copied tree alone passes the file check", rc == 2 and any("has no commit 90a959eb" in l for l in out), out)
for what, change, name in (
        ("a patch of the chain changed by one line", lambda n: (n / "nvk-hubfix.patch").write_text((n / "nvk-hubfix.patch").read_text() + "\n"), "nvk-hubfix.patch"),
        ("a header the build copies in, changed", lambda n: (n / "hwrt/nvk_hwrt.h").write_text("/* other */\n"), "hwrt/nvk_hwrt.h"),
        ("the fixes patch removed", lambda n: (n / "nvk-fixes-after-def16.patch").unlink(), "nvk-fixes-after-def16.patch")):
    rc, out, chain, wrote = changed_tree(change)
    arm(f"2.9 with {what} stops by name before it writes anything",
        rc == 2 and not wrote and any(l.startswith(f"STOP: nvk/{name} is not the file release 2.9 was built from") for l in out)
        and not any("has no commit" in l for l in out), out)
rc, out, chain, wrote = changed_tree(lambda n: (n / "nvk-hubfix.patch").write_text("changed\n"), "3.0")
arm("control: 3.0 does not read the list (its own chain may change; only 2.9 is fixed)",
    rc == 2 and any("has no commit" in l for l in out) and not any("release 2.9 was built from" in l for l in out), out)
arm("control: a list without one of the files is seen", set(list(sums)[1:]) != reads)

# --- the record: patch headers and NOTICE ---------------------------------------------------------------------------
COMMIT = re.compile(r"^ +commit ([0-9a-f]{8})(?: \(main ([0-9a-f]{8})\))? (\S.*)$")


def commits(text):
    return [m.groups() for m in map(COMMIT.match, text.splitlines()) if m]


def notice_part(name, text=notice):
    """The lines of NOTICE that belong to one patch: from the line that names it to the next patch name or blank line."""
    out, on = [], False
    for l in text.splitlines():
        if on and (not l.strip() or "nvk/nvk-" in l):
            break
        if f"nvk/{name}.patch" in l:
            on = True
            continue
        if on:
            out.append(l)
    return "\n".join(out)


files = {}
for name in FIXES:
    text = (NVK / (name + ".patch")).read_text()
    head, _, body = text.partition("\ndiff --git ")
    listed = commits(head)
    arm(f"{name}: the header names the upstream commits it carries", len(listed) >= 4 and "MIT" in head, len(listed))
    arm(f"{name}: NOTICE names the same commits, hash and subject", listed and commits(notice_part(name)) == listed,
        [c for c in listed if c not in commits(notice_part(name))])
    # patch(1) skips leading text, but not text that looks like a diff. It strips indentation (blanks, tabs, X) first;
    # the BSD patch of macOS then reads any line that starts with digits and a, c or d as an ed or "normal" diff
    # command (GNU patch only when nothing follows the letter), so no header line may start with a digit.
    risky = [l for l in head.splitlines() if re.match(r"[ \tX]*(--- |\+\+\+ |@@|\*\*\* |Index:|Prereq:|diff |[<>] |\.$|\d)", l)]
    arm(f"{name}: no header line can be taken for part of a patch", not risky, risky)
    files[name] = re.findall(r"(?m)^diff --git a/(\S+) b/\1$", "diff --git " + body)
    arm(f"{name}: touches only compiler and utility sources, nothing of the NVRM back end",
        files[name] and all(f.startswith(("src/compiler/nir/", "src/util/", "src/nouveau/compiler/")) or
                            f == "src/nouveau/vulkan/nvk_shader.c" for f in files[name]), files[name])
some = commits((NVK / "nvk-nak-sched-main.patch").read_text().partition("\ndiff --git ")[0])
cut = "\n".join(l for l in notice.splitlines() if some[0][0] not in l)
arm("control: a commit missing from NOTICE is seen", commits(notice_part("nvk-nak-sched-main", cut)) != some)
arm("control: a header line that starts with a commit hash would be refused",
    re.match(r"[ \tX]*(--- |\+\+\+ |@@|\*\*\* |Index:|Prereq:|diff |[<>] |\.$|\d)", "  83a732aa nak: Increase sm120 imma/hmma latencies") is not None)
arm("the Blackwell scoreboard fix and the scheduler fix are where the build comments say",
    ("2351a39d", "801952de", "nak: Fix missing scoreboard for uniform Ldcg on SM120") in
    commits((NVK / "nvk-mesa-26.3.0.patch").read_text().partition("\ndiff --git ")[0])
    and ("b6b973d0", None, "nak: Fix ready_cycle calculation in prepass sched") in some)

# --- the files: what makes leaving one out safe for the rest of the chain -------------------------------------------
shared = {(a, b): sorted(set(files[a]) & set(files[b])) for i, a in enumerate(FIXES) for b in FIXES[i + 1:]}
arm("the scheduling patch shares no file with the other two",
    not shared[("nvk-mesa-26.3.0", "nvk-nak-sched-main")] and not shared[("nvk-nak-fixes-main", "nvk-nak-sched-main")], shared)
one = "src/nouveau/compiler/nak/sm120_instr_latencies.rs"


def old_ranges(name, path):
    """(first, last) old line of every hunk of one file section."""
    text = (NVK / (name + ".patch")).read_text()
    sec = text.split(f"diff --git a/{path} b/{path}\n", 1)[1].split("\ndiff --git ", 1)[0]
    return [(int(a), int(a) + int(n or 1) - 1) for a, n in re.findall(r"(?m)^@@ -(\d+)(?:,(\d+))? \+", sec)]


if shared[("nvk-mesa-26.3.0", "nvk-nak-fixes-main")] == [one]:
    a, b = old_ranges("nvk-mesa-26.3.0", one), old_ranges("nvk-nak-fixes-main", one)
    text26 = (NVK / "nvk-mesa-26.3.0.patch").read_text().split(f"b/{one}\n", 1)[1]
    same_len = re.search(r"(?m)^@@ -\d+,(\d+) \+\d+,(\d+) @@", text26)
    arm("the one file the other two share is changed in hunks that do not meet, and the first keeps its length",
        all(x[1] < y[0] or y[1] < x[0] for x in a for y in b) and same_len.group(1) == same_len.group(2), (a, b))
else:
    arm("the other two share exactly one file (the SM120 latency table)", False, shared)
later = default[at + 3:] if default else []
mine = set(sum(files.values(), []))
clash = {}
for p in later:
    hit = sorted(mine & set(re.findall(r"(?m)^(?:diff --git a/\S+ b/|\+\+\+ b/)(\S+)", (NVK / (p + ".patch")).read_text())))
    if hit:
        clash[p] = hit
arm("no patch after the three touches a file they change (it would not apply with one of them left out)", not clash, clash)

# --- the switches, on the real script -------------------------------------------------------------------------------
rc, out, chain, wrote = run()
arm("by default the script builds the whole chain and says so in one line",
    rc == 2 and chain == default and not any(l.startswith(("skipped", "note")) for l in out)
    and any(l.startswith("STOP: ") and "has no commit" in l for l in out) and not wrote, out)
rc, out, chain, wrote = run(NVK_SKIP_PATCHES="nvk-nak-sched-main")
arm("NVK_SKIP_PATCHES=nvk-nak-sched-main leaves out that patch alone and reports it",
    chain == [p for p in default if p != "nvk-nak-sched-main"]
    and out.count("skipped nvk-nak-sched-main.patch (NVK_SKIP_PATCHES)") == 1
    and sum(l.startswith("skipped") for l in out) == 1, out)
rc, out, chain, wrote = run(NVK_SKIP_PATCHES="nvk-nak-sched-main\tnvk-mesa-26.3.0\n nvk-nak-fixes-main")
arm("all three can be left out, however the names are separated",
    chain == [p for p in default if p not in FIXES] and sum(l.startswith("skipped") for l in out) == 3, out)
rc, out, chain, wrote = run(NVK_SKIP_PATCHES="nvk-nak-sched-mian")
arm("a misspelt name stops the build instead of being ignored",
    rc == 2 and chain is None and not wrote and any(l.startswith("STOP: NVK_SKIP_PATCHES can only name") and
                                                    all(f in l for f in FIXES) and "nvk-nak-sched-mian" in l for l in out), out)
rc, out, chain, wrote = run(NVK_SKIP_PATCHES="nvk-nak-sched-main nvk-macos")
arm("a patch the rest of the chain is built on cannot be left out", rc == 2 and chain is None and not wrote, out)
for rel, says in (("3.0", "its upstream fixes are already in the 3.0 chain"), ("2.9", "the tree of tag v2.9 has the file"),
                  ("2.8", "a 2.8 library built with it cannot be built from this tree")):
    rc, out, chain, wrote = run(rel, NVK_EXTRA_PATCHES="one nvk-upstream-nak two")
    arm(f"{rel}: the 2.9 setting NVK_EXTRA_PATCHES=nvk-upstream-nak (the file is gone) stops the build and says what to do",
        rc == 2 and chain is None and not wrote
        and any(l.startswith("STOP: NVK_EXTRA_PATCHES names nvk-upstream-nak") and says in l for l in out), out)
rc, out, chain, wrote = run(NVK_EXTRA_PATCHES="one two")
arm("other extra patches are still appended, in order", chain == default + ["one", "two"], chain)
for rel in ("2.9", "2.8"):
    rc, out, chain, wrote = run(rel, NVK_SKIP_PATCHES="nvk-nak-sched-main")
    arm(f"the {rel} chain has none of the three, and a skip there stops the build instead of building the whole chain",
        rc == 2 and chain is None and not wrote
        and any(l.startswith("STOP: NVK_SKIP_PATCHES names nvk-nak-sched-main") and f"which the {rel} chain does not have" in l
                for l in out), out)

# --- the record of a build whose chain is not the default one --------------------------------------------------------
RECORD = r'(?m)^echo "release \$REL  sha256 [^\n]*  exports \$\(wc -l < "\$OUT/exports\.txt"\)\$OTHER_CHAIN" \| tee "\$OUT/BUILD\.txt"$'
writers = [l for l in bld.splitlines() if re.search(r'(tee|>>?) *"?\$OUT/BUILD\.txt', l)]
arm("the one line that writes BUILD.txt ends with what the chain differs by",
    len(writers) == 1 and re.search(RECORD, writers[0]) is not None, writers)
arm("control: release 2.9's line, which ended after the export count, is not taken for it",
    not re.search(RECORD, bld.replace('$OTHER_CHAIN" | tee', '" | tee')))


def record_end(rel="3.0", **env):
    """What the script puts at the end of its BUILD.txt line: its lines from UPSTREAM_FIXES= to OTHER_CHAIN=, alone."""
    m = re.search(r"(?ms)^UPSTREAM_FIXES=.*?^OTHER_CHAIN=[^\n]*$", bld)
    if not m:
        return None
    e = {k: v for k, v in os.environ.items() if not k.startswith("NVK_")}
    e.update(env)
    r = subprocess.run(["bash", "-c", 'set -euo pipefail; REL=$1; NVK=$2; BASE=; eval "$3" >/dev/null; printf "%s" "$OTHER_CHAIN"',
                        "_", rel, str(NVK), m.group(0)], capture_output=True, text=True, env=e)
    return r.stdout if r.returncode == 0 else None


arm("the default chain adds nothing to the record line", record_end() == "", record_end())
arm("a patch left out is named in the record",
    record_end(NVK_SKIP_PATCHES="nvk-nak-sched-main") == "  WITHOUT nvk-nak-sched-main (NVK_SKIP_PATCHES)",
    record_end(NVK_SKIP_PATCHES="nvk-nak-sched-main"))
arm("all three left out are named, in chain order",
    record_end(NVK_SKIP_PATCHES="nvk-nak-sched-main nvk-mesa-26.3.0 nvk-nak-fixes-main")
    == "  WITHOUT " + " ".join(FIXES) + " (NVK_SKIP_PATCHES)")
arm("an added patch is named in the record, on 3.0 and on 2.9",
    record_end(NVK_EXTRA_PATCHES="one two") == "  PLUS one two (NVK_EXTRA_PATCHES)"
    and record_end("2.9", NVK_EXTRA_PATCHES="one") == "  PLUS one (NVK_EXTRA_PATCHES)")
arm("left out and added together are both named",
    record_end(NVK_SKIP_PATCHES="nvk-mesa-26.3.0", NVK_EXTRA_PATCHES="one")
    == "  WITHOUT nvk-mesa-26.3.0 (NVK_SKIP_PATCHES)  PLUS one (NVK_EXTRA_PATCHES)")
arm("2.9's and 2.8's own chains add nothing to the record line", record_end("2.9") == "" and record_end("2.8") == "")
arm("control: a refused name gives no record at all", record_end(NVK_SKIP_PATCHES="nvk-macos") is None
    and record_end("2.8", NVK_SKIP_PATCHES="nvk-nak-sched-main") is None and record_end(NVK_EXTRA_PATCHES="nvk-upstream-nak") is None)
print("FAILED" if fails else "PASS"); sys.exit(1 if fails else 0)
