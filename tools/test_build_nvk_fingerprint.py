#!/usr/bin/env python3
"""The last lines of the NVK build cannot fail a build that succeeded.

build/build_nvk.sh runs under `set -euo pipefail`. A pipeline whose last command leaves at its first match while the
command before it is still writing kills that command with SIGPIPE; pipefail turns that into a failure; and when the
pipeline is the value of an assignment, errexit stops the script there. The line that reads the built library's
LC_UUID had that shape since 2.9 (`otool -l lib | awk '/uuid/{print $2; exit}'`): after an NVK build of an hour or
more the script could end with status 141 and no message, depending on how much otool still had to write when awk
left. The line now reads the listing to its end.

What is checked: the script still sets errexit and pipefail (the premise of the rest); no assignment in it takes its
value from a pipeline that ends in a reader that leaves early (awk with exit, grep -q or -m, head, sed with q); and
the awk program on the LC_UUID line, run under the script's own shell options against a listing far larger than a
pipe holds, prints the first uuid once and lets the writer finish.
The arms at the end show that the check sees 2.9's line, and that 2.9's program does fail in that harness.
Usage: test_build_nvk_fingerprint.py [driver checkout]"""
import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1]
SCRIPT = "build/build_nvk.sh"
OLD_LINE = '''UUID=$(otool -l "$OUT/libvulkan_nouveau.dylib" | awk '/uuid/{print $2; exit}')'''
OLD_PROG = "/uuid/{print $2; exit}"
fails = []


def arm(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (f"\n       {detail}" if detail and not ok else ""))
    if not ok:
        fails.append(name)


def split(text, seps):
    """text cut at the separators that stand outside quotes; the longest separator wins at each place."""
    out, cur, quote, i = [], "", "", 0
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\" and quote == '"' and i + 1 < len(text):
                cur += text[i:i + 2]; i += 2; continue
            if c == quote:
                quote = ""
        elif c in "'\"":
            quote = c
        else:
            sep = next((s for s in sorted(seps, key=len, reverse=True) if text.startswith(s, i)), "")
            if sep:
                out.append(cur); cur = ""; i += len(sep); continue
        cur += c; i += 1
    return out + [cur]


def stages(command):
    """the commands of one pipeline: cut at | but not at || (a list operator, which split() has taken out already)"""
    return [s.strip() for s in split(command, ["|"])]


def leaves_early(stage):
    words = stage.split()
    if not words:
        return False
    tool, rest = words[0], stage[len(words[0]):]
    if tool == "head":
        return True
    if tool == "grep":
        return bool(re.search(r"\s(-[A-Za-z]*[qm][A-Za-z0-9]*|--quiet|--silent|--max-count)(?![\w-])", rest))
    if tool == "awk":
        return bool(re.search(r"\bexit\b", rest))
    if tool == "sed":      # q as a command of its program: after an address, a brace or a semicolon, and ending one
        programs = [a or b for a, b in re.findall(r"'([^']*)'|\"([^\"]*)\"", rest)] or [rest]
        return any(re.search(r"(^|[;{}\s\d/$])q\s*($|[;}])", p) for p in programs)
    return False


ASSIGN = re.compile(r"\b([A-Za-z_]\w*)=\$\(")


def early_assignments(script):
    """(line number, text) of each assignment whose value is a pipeline that ends in a reader that leaves early"""
    bad = []
    for n, line in enumerate(script.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        for m in ASSIGN.finditer(line):
            depth, j = 1, m.end()
            while j < len(line) and depth:      # the matching ) of this $(
                depth += (line[j] == "(") - (line[j] == ")")
                j += 1
            body = line[m.end():j - 1]
            for command in split(body, ["||", "&&", ";"]):
                st = stages(command)
                if len(st) > 1 and leaves_early(st[-1]):
                    bad.append((n, line.strip()[:110]))
    return bad


script = (ROOT / SCRIPT).read_text()
opts = " ".join(re.findall(r"^set\s+(.*)$", script, re.M))
arm(f"{SCRIPT} runs under errexit and pipefail",
    bool(re.search(r"(^|\s)-[A-Za-z]*e", opts)) and "pipefail" in opts, opts)
bad = early_assignments(script)
arm("no assignment in it takes its value from a pipeline that ends in a reader that leaves early", not bad,
    "; ".join(f"line {n}: {t}" for n, t in bad))

# ---- the LC_UUID line's own program, under the script's options, against a listing no pipe can hold
uuid_lines = [l for l in script.splitlines() if re.match(r"\s*UUID=\$\(otool -l ", l)]
prog = re.search(r"\|\s*awk\s+'([^']*)'\)\s*$", uuid_lines[0]) if len(uuid_lines) == 1 else None
arm("the script reads LC_UUID on one line, from otool's listing through awk", bool(prog), "\n".join(uuid_lines))
WRITER = ('BEGIN { print "     cmd LC_UUID"; print "    uuid FIRST-UUID"; '
          'for (i = 0; i < 400000; i++) print "  sectname __s" i; print "    uuid LAST-UUID" }')
HARNESS = 'set -euo pipefail\nUUID=$(awk "$WRITER" | awk "$PROG")\necho "LC_UUID $UUID"\n'


def fingerprint(program):
    env = dict(os.environ, WRITER=WRITER, PROG=program)
    r = subprocess.run(["bash", "-c", HARNESS], env=env, capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout.strip()


if prog:
    rc, out = fingerprint(prog.group(1))
    arm("its program prints the first uuid once and the assignment succeeds on a listing of several megabytes",
        rc == 0 and out == "LC_UUID FIRST-UUID", f"status {rc}, printed {out!r}")

# ---- each check fires
old = script.replace(uuid_lines[0], OLD_LINE) if len(uuid_lines) == 1 else script
arm("control: with 2.9's line the assignment is reported",
    old != script and [t for _, t in early_assignments(old)] == [OLD_LINE], early_assignments(old))
rc, out = fingerprint(OLD_PROG)
arm("control: 2.9's program stops the same harness before it prints anything", rc != 0 and out == "",
    f"status {rc}, printed {out!r}")
SEEN = ('X=$(a | grep -q y)', 'X=$(a | grep -Fqx "y")', 'X=$(a b | grep -m1 y)', 'X=$(a | sort | head -1)',
        "X=$(a | sed -n '/y/{p;q;}')", "X=$(a | sed 1q)", "X=$(a | sed -n '/y/q')",
        'X=$(a | awk \'/y/{print; exit}\'); Y=1', 'local X=$(a | head -3 || true)')
LEFT = ('X=$(a | grep -c y || true)', "X=$(a | grep 'q' | sort)", "X=$(a | awk '{print $3}')", 'X=$(head -1 file)',
        "X=$(a | sed -n 's/q/r/p')", 'X=$(a || b | c)', 'echo "x $(a | head -1)"', "X=$(a | grep '|' | wc -l)")
arm("control: each early reader is seen as the last command of an assigned pipeline",
    all(early_assignments(s) for s in SEEN), [s for s in SEEN if not early_assignments(s)])
arm("control: a reader that reads everything, a list, and a substitution that is not an assignment are left alone",
    not any(early_assignments(s) for s in LEFT), [s for s in LEFT if early_assignments(s)])

print(f"{len(fails)} failed")
sys.exit(1 if fails else 0)
