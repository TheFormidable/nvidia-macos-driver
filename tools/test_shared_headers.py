#!/usr/bin/env python3
"""The headers NVRM.kext and NVK both compile are the same bytes on both sides.

Three headers describe an interface between the kext and NVK: nvrm_evwait.h (the event wait: selector numbers, the
reason bits, the wait policy), nvrm_fence_abi.h (the surface fence: selector 12, its request structures) and
nvrm_chan_note_abi.h (the channel note: escape 0xE8). Each lives in kexts/NVRM, where the kext includes it. NVK is
built from a Mesa tree that the patches in nvk/ are applied to, so its copy is a "new file" part of the patch that
first needs it, at src/nouveau/vulkan/nvkmd/nvrm/<name>.

Two copies of a structure layout drift apart with one edit, and nothing compiles the two together: the kext and NVK
would each build, and then disagree about a size or a number at run time. So the copy inside the patch is taken out
here and compared with the kext's file, byte for byte. Also checked: the patch is one the default build applies (a
copy in a patch nobody applies proves nothing), and the header reaches the Mesa tree by no second route (a copy step in
build/build_nvk.sh, or a loose second file, would be a copy this test does not compare).

A patch that is not in the tree yet fails its arms, with a line saying which piece of work brings it.
Usage: test_shared_headers.py [driver checkout]"""
import pathlib
import re
import sys

ROOT = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1]
MESA_DIR = "src/nouveau/vulkan/nvkmd/nvrm"
# header in kexts/NVRM, the patch in nvk/ that adds NVK's copy, and the work that patch arrives with
SHARED = [
    ("nvrm_evwait.h", "nvk-evwait", "NVK's use of the kernel event wait (its wait loop sleeps on the kext's event)"),
    ("nvrm_chan_note_abi.h", "nvk-channel-recover", "NVK's rebuild of a channel after a GPU fault"),
    ("nvrm_fence_abi.h", "nvk-fence", "NVK's use of the kernel surface fence"),
]
fails = []


def arm(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (f"\n       {detail}" if detail and not ok else ""))
    if not ok:
        fails.append(name)


def created_file(patch, path):
    """-> (the bytes of the file the patch creates at path, '') or (None, why not). patch: the patch file's bytes;
    path: the file's place in the tree the patch is applied to with -p1. Both spellings in nvk/ are read: git's
    ("--- /dev/null") and diff -ruN's ("--- a/<path>" with the epoch as its date). What makes it a new file is the same
    in both: one hunk, whose old side is empty (@@ -0,0 +1,N @@) and whose N lines are all added."""
    lines = patch.split(b"\n")
    starts = [i for i, ln in enumerate(lines)
              if ln.startswith(b"+++ ") and ln[4:].split(b"\t")[0].strip().split(b"/", 1)[-1] == path.encode()]
    if not starts:
        return None, f"the patch has no part for {path}"
    if len(starts) > 1:
        return None, f"the patch has {len(starts)} parts for {path}"
    i = starts[0] + 1
    hunk = re.match(rb"@@ -0(?:,0)? \+1(?:,(\d+))? @@", lines[i]) if i < len(lines) else None
    if not hunk:
        return None, f"the patch changes {path} instead of creating it (its first hunk is not @@ -0,0 +1,N @@)"
    n = int(hunk.group(1)) if hunk.group(1) else 1
    body = lines[i + 1:i + 1 + n]
    if len(body) != n or not all(ln.startswith(b"+") for ln in body):
        return None, f"the new-file hunk for {path} does not hold the {n} added lines its header promises"
    rest = lines[i + 1 + n:]
    no_newline = bool(rest) and rest[0].startswith(b"\\ No newline")
    after = rest[1:] if no_newline else rest
    # anything that follows belongs to the next file of the patch (or is the end): a second hunk here would mean the
    # file is not simply created
    if after and after[0].startswith(b"@@"):
        return None, f"the patch has more than one hunk for {path}"
    return b"\n".join(ln[1:] for ln in body) + (b"" if no_newline else b"\n"), ""


def first_difference(a, b):
    """Where two files part, said so that the line can be found."""
    la, lb = a.split(b"\n"), b.split(b"\n")
    for n, (x, y) in enumerate(zip(la, lb), 1):
        if x != y:
            return f"line {n}: the kext has {x[:90]!r}, the patch has {y[:90]!r}"
    return f"the kext's file has {len(la) - 1} lines and the patch's copy {len(lb) - 1}" if len(la) != len(lb) else "no difference"


def chain(script):
    """The patch names build_nvk.sh applies when no release is asked for: the words of the CHAIN="..." line in that
    release's arm of its `case $REL in` block."""
    default = re.search(r"\bREL=\$\{4:-([\w.]+)\}", script)
    arm_text = re.search(r"^[ \t]*" + re.escape(default.group(1)) + r"\)(.*?);;", script, re.S | re.M) if default else None
    line = re.search(r'^[ \t]*CHAIN="([^"\n]*)"', arm_text.group(1), re.M) if arm_text else None
    return line.group(1).split() if line else []


def as_new_file(path, data, style="git"):
    """A patch that creates path with these bytes (for the controls)."""
    body = data.split(b"\n")
    tail = b"" if data.endswith(b"\n") else b"\n\\ No newline at end of file"
    if data.endswith(b"\n"):
        body = body[:-1]
    p = path.encode()
    head = (b"diff --git a/" + p + b" b/" + p + b"\nnew file mode 100644\nindex 0000000..1234567\n--- /dev/null\n+++ b/" + p + b"\n"
            if style == "git" else
            b"diff -ruN a/" + p + b" b/" + p + b"\n--- a/" + p + b"\t1969-12-31 16:00:00\n+++ b/" + p + b"\t2026-10-05 07:44:54\n")
    return head + b"@@ -0,0 +1," + str(len(body)).encode() + b" @@\n" + b"\n".join(b"+" + ln for ln in body) + tail + b"\n"


build = (ROOT / "build/build_nvk.sh").read_text()
applied = chain(build)
arm("build/build_nvk.sh has a default release with a patch chain", len(applied) > 5, f"read: {' '.join(applied) or 'nothing'}")
for name, patch_name, work in SHARED:
    ours, patch = ROOT / "kexts/NVRM" / name, ROOT / "nvk" / (patch_name + ".patch")
    there = f"{MESA_DIR}/{name}"
    arm(f"{name}: the kext's file exists", ours.is_file())
    if not patch.is_file():
        why = (f"nvk/{patch_name}.patch is not in the tree. It is delivered with {work}, which adds the patch to nvk/ and its "
               f"name to the chain in build/build_nvk.sh; the patch carries {there} as a new file. Until then NVK has no copy "
               "of this header and this test fails.")
        arm(f"{name}: nvk/{patch_name}.patch carries NVK's copy", False, why)
        arm(f"{name}: NVK's copy is byte for byte the kext's file", False, "nothing to compare: see the line above")
    else:
        copy, why = created_file(patch.read_bytes(), there)
        arm(f"{name}: nvk/{patch_name}.patch carries NVK's copy, as the new file {there}", copy is not None, why)
        same = copy is not None and ours.is_file() and copy == ours.read_bytes()
        arm(f"{name}: NVK's copy is byte for byte the kext's file", same,
            first_difference(ours.read_bytes(), copy) if copy is not None and ours.is_file() else "nothing to compare")
    arm(f"{name}: the default NVK build applies {patch_name}", patch_name in applied,
        f"{patch_name} is not a word of the default CHAIN= line of build/build_nvk.sh")
    # one file in kexts/NVRM, one copy inside the patch, and no third: a loose copy or a copy step is not compared here
    live = "\n".join(ln for ln in build.split("\n") if not ln.lstrip().startswith("#"))
    loose = sorted(str(p.relative_to(ROOT)) for top in ("kexts", "nvk", "plugin", "build") for p in (ROOT / top).rglob(name)
                   if p != ours)
    arm(f"{name}: it reaches NVK only inside the patch (no copy step in build_nvk.sh, no second file)", name not in live and not loose,
        ("build/build_nvk.sh names it; " if name in live else "") + ", ".join(loose))

# controls: the reading of a patch, on patches made here from the kext's own header
sample = (ROOT / "kexts/NVRM/nvrm_evwait.h").read_bytes()
there = f"{MESA_DIR}/nvrm_evwait.h"
before = b"diff --git a/src/x.c b/src/x.c\n--- a/src/x.c\n+++ b/src/x.c\n@@ -1,2 +1,3 @@\n a\n+b\n c\n"
after = b"diff --git a/src/y.c b/src/y.c\n--- a/src/y.c\n+++ b/src/y.c\n@@ -4,2 +4,3 @@\n d\n+e\n f\n"
arm("control: a header written into a patch the way git does comes back byte for byte, between other files' parts",
    created_file(before + as_new_file(there, sample) + after, there) == (sample, ""))
arm("control: the same for the diff -ruN spelling, which nvk-macos.patch uses",
    created_file(before + as_new_file(there, sample, "ruN") + after, there) == (sample, ""))
arm("control: a file without a final newline comes back without one",
    created_file(as_new_file(there, sample[:-1]), there) == (sample[:-1], "") and sample.endswith(b"\n"))
changed = sample.replace(b"NVRM_EVW_SEL_NOTE", b"NVRM_EVW_SEL_NOTE ", 1)
got = created_file(as_new_file(there, changed), there)[0]
arm("control: one added space in the copy is a difference, and the line is named",
    changed != sample and got is not None and got != sample and first_difference(sample, got).startswith("line "))
arm("control: a copy with Windows line ends, or with its last line cut off, is a difference",
    created_file(as_new_file(there, sample.replace(b"\n", b"\r\n")), there)[0] != sample
    and created_file(as_new_file(there, sample[:sample.rstrip(b"\n").rfind(b"\n") + 1]), there)[0] != sample)
arm("control: a patch that edits the header in place does not count as carrying it",
    created_file(b"--- a/" + there.encode() + b"\n+++ b/" + there.encode() + b"\n@@ -1,2 +1,3 @@\n a\n+b\n c\n", there)[0] is None)
arm("control: the header created in another folder, or under a longer name, is not NVK's copy",
    created_file(as_new_file("src/nouveau/vulkan/nvrm_evwait.h", sample), there)[0] is None
    and created_file(as_new_file(there + ".in", sample), there)[0] is None)
arm("control: a new-file hunk shorter than its header says is refused",
    created_file(as_new_file(there, sample).replace(b"@@ -0,0 +1,", b"@@ -0,0 +1,9", 1), there)[0] is None)
if fails:
    print(f"\nFAILED ({len(fails)}):")
    for f in fails:
        print("  " + f)
    missing = [p for _, p, _ in SHARED if not (ROOT / "nvk" / (p + ".patch")).is_file()]
    if missing:
        print("Not in the tree yet: " + ", ".join(f"nvk/{p}.patch" for p in missing) + ". They arrive with the NVK work named above; "
              "the failures about them are expected until then, and they are failures, not skips.")
else:
    print("PASS")
sys.exit(1 if fails else 0)
