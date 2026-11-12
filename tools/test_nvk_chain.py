#!/usr/bin/env python3
"""The upstream Mesa commits in the NVK patch chain, and the two build variables that change the chain.

2.9 kept the upstream NAK fixes in one opt-in file (NVK_EXTRA_PATCHES=nvk-upstream-nak) that nobody built by default
and that does not apply on the 26.3.0-rc3 base (rc3 already has two of its commits). They are now three patches at
the end of the default chain, each unchanged upstream text: nvk-mesa-26.3.0, nvk-nak-fixes-main, nvk-nak-sched-main.
This test holds:

  the chain    the three follow nvk-diag-nofollow in that order; the "case $REL in ... esac" block of build_nvk.sh,
               evaluated alone (a check that wants the chain without running the build reads it that way), gives the
               whole default chain; every name in it is a file
  the record   each patch names its upstream commits in its header, and NOTICE names the same ones
  the files    the three touch only compiler sources that no later patch of the chain touches, so the rest applies
               whichever of them is left out
  the switches NVK_SKIP_PATCHES leaves out exactly the named upstream-fix patches and refuses any other name;
               NVK_EXTRA_PATCHES=nvk-upstream-nak (an old setting) is dropped with one note and the build goes on

The switches are tested on the real script: it is run with a Mesa path that does not exist, so it selects its chain,
prints it, and stops at its first check before it writes anything."""
import os, pathlib, re, subprocess, sys, tempfile

R = pathlib.Path(__file__).resolve().parents[1]
NVK = R / "nvk"
SCRIPT = R / "build/build_nvk.sh"
bld = SCRIPT.read_text()
notice = (R / "NOTICE").read_text()
FIXES = ["nvk-mesa-26.3.0", "nvk-nak-fixes-main", "nvk-nak-sched-main"]
fails = 0


def arm(name, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + name + (f" ({str(detail).strip()[-300:]})" if detail and not ok else ""))
    fails += not ok


# --- the chain ------------------------------------------------------------------------------------------------------
line29 = next((l for l in bld.splitlines() if l.strip().startswith('CHAIN="nvk-macos') and "nvk-card-select" in l), "")
arm("the three upstream-fix patches follow nvk-diag-nofollow, in this order",
    " nvk-diag-nofollow " + " ".join(FIXES) in line29, line29[-120:])
arm("the 2.8 chain is unchanged",
    '       CHAIN="nvk-macos @FIXES nvk-620-kernel nvk-recycle nvk-slab nvk-slabdef" ;;' in bld.splitlines())


def block_chain(rel, text=bld):
    """CHAIN as the release table alone sets it: the lines from 'case $REL in' to 'esac', evaluated by bash."""
    m = re.search(r"(?ms)^case \$REL in\n.*?^esac$", text)
    if not m:
        return None
    r = subprocess.run(["bash", "-c", 'REL=$1; NVK=$2; FIXES=; CHAIN=; BASE=; eval "$3" >/dev/null; printf "%s" "$CHAIN"',
                        "_", rel, str(NVK), m.group(0)], capture_output=True, text=True)
    return r.stdout.split() if r.returncode == 0 else None


def run(rel="2.9", **env):
    """The script up to its first check -> (exit status, output lines, the chain it printed or None)."""
    with tempfile.TemporaryDirectory(prefix="nvkchain-") as t:
        e = {k: v for k, v in os.environ.items() if not k.startswith("NVK_")}
        e.update(env)
        r = subprocess.run(["bash", str(SCRIPT), t + "/no-mesa", t + "/no-ogkm", t + "/out", rel],
                           capture_output=True, text=True, env=e)
        wrote = os.path.exists(t + "/out")
    lines = r.stdout.splitlines()
    chain = next((l.split(": ", 1)[1].split() for l in lines if l.startswith(f"patch chain ({rel}): ")), None)
    return r.returncode, lines, chain, wrote


default = block_chain("2.9")
at = default.index("nvk-diag-nofollow") + 1 if default and "nvk-diag-nofollow" in default else 0
arm("the release table of build_nvk.sh evaluates on its own and gives the 2.9 chain with the three patches",
    default is not None and default[at:at + 3] == FIXES and default[0] == "nvk-macos" and "@FIXES" in default, default)
arm("control: the same evaluation selects by release (2.8 gives the 2.8 chain)",
    block_chain("2.8") == "nvk-macos @FIXES nvk-620-kernel nvk-recycle nvk-slab nvk-slabdef".split())
arm("control: a table that does not evaluate is seen", block_chain("2.9", bld.replace("\nesac\n", "\nesac_\n", 1)) is None)
missing = [p for p in (default or []) if p != "@FIXES" and not (NVK / (p + ".patch")).is_file()]
arm("every name in the default chain is a patch file in nvk/", default is not None and not missing, missing)
arm("the opt-in file the three replace is gone", not (NVK / "nvk-upstream-nak.patch").exists())

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
rc, out, chain, wrote = run(NVK_EXTRA_PATCHES="nvk-upstream-nak")
arm("the 2.9 setting NVK_EXTRA_PATCHES=nvk-upstream-nak is dropped with one note and the build goes on",
    chain == default and sum(l.startswith("note: NVK_EXTRA_PATCHES=nvk-upstream-nak is ignored") for l in out) == 1
    and any("has no commit" in l for l in out), out)
rc, out, chain, wrote = run(NVK_EXTRA_PATCHES="one nvk-upstream-nak two")
arm("other extra patches are still appended, in order", chain == default + ["one", "two"], chain)
rc, out, chain, wrote = run("2.8", NVK_SKIP_PATCHES="nvk-nak-sched-main")
arm("the 2.8 chain has none of the three, and a skip there says so",
    chain == "nvk-macos @FIXES nvk-620-kernel nvk-recycle nvk-slab nvk-slabdef".split()
    and any("which the 2.8 chain does not have" in l for l in out), out)
print("FAILED" if fails else "PASS"); sys.exit(1 if fails else 0)
