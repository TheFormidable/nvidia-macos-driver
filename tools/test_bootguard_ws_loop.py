#!/usr/bin/env python3
"""The boot-guard job tells a desktop that stayed up from a WindowServer that keeps dying.

When WindowServer cannot get a Metal device for a display this driver drives, CoreDisplay asserts "Failed to create
MetalDevice" and launchd restarts WindowServer every ten seconds: a black screen with only the cursor. The head is lit,
so the kernel's guard was already cleared, and the job saw *a* WindowServer at both of its looks and called the start
good, so nothing ever turned the driver off. The job is lifted out of install.sh and run against fake pgrep, sysctl,
nvram, stat, sleep, ioreg and shutdown. The negative control is the job as 2.8.1 shipped it (tag v2.8.1), which must
call the looping WindowServer good.

A WindowServer that stays up is not this driver's desktop either when the driver's framebuffer refused this macOS's
classes: NVRM then declined the card and wrote that on the PCI device (nvrm-layout = "declined ..."), and nothing of the
driver shows a picture. Calling that start good would end the install's trial, and the fallback job
(tools/test_lastgood_refusal.py) would take itself away at the next start. The job reads the registry before it calls
a start good; the control is the job without that look.

Nor is a desktop with no NVIDIA driver running (no kext loaded yet, or the driver off), or one where NVRM claimed the
card and nothing lit: the job ends the trial (nullmoth-nv-ok) only when a card carries nvrm-claimed and its
framebuffer reported the display path up (debug.nvrmfb_guard_cleared) or a display hangs off another GPU. The controls
are the job with the two lines it had before that rule.

A WindowServer that keeps dying is this driver's loop when its accelerator is armed (debug.nvaccelfb), or when the
accelerator refused this macOS's classes and every display hangs off the card NVRM claimed; the control is the job
with the armed-only rule."""
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
fails = 0


def arm(name, ok, detail=""):
    global fails
    fails += not ok
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f" ({detail.strip()[-400:]})"))


def job_from(src):
    start = src.index("cat > \"$BG.tmp\" <<'EOF' || die \"write the boot-guard job\"\n")
    body = src[src.index("\n", start) + 1:]
    return body[:body.index("\nEOF\n") + 1]


FAKES = {
    # one WindowServer pid per call, from $WS_PIDS (the last one repeats); none when the list is empty
    "pgrep": r'''#!/bin/bash
n=$(cat "$T/pgrep.n" 2>/dev/null || echo 0); echo $((n + 1)) > "$T/pgrep.n"
set -- $WS_PIDS; [ $# -gt 0 ] || exit 1
i=$((n + 1)); [ $i -le $# ] || i=$#
eval "echo \${$i}"''',
    "sysctl": r'''#!/bin/bash
case "$2" in
  kern.boottime) echo "{ sec = $BOOT_SEC, usec = 912475 } Sat Nov  7 22:00:00 2026";;
  debug.nvaccelfb) [ -n "$ARMED" ] && echo "$ARMED" || exit 1;;
  debug.nvrmfb_guard_cleared) [ -n "$CLEARED" ] && echo "$CLEARED" || exit 1;;
  *) exit 1;;
esac''',
    "nvram": r'''#!/bin/bash
echo "nvram $*" >> "$T/calls"
case "$1" in
  -d) rm -f "$T/nv.${2##*:}";;
  *=*) k=${1%%=*}; echo "${1#*=}" > "$T/nv.${k##*:}";;
  *) [ -f "$T/nv.${1##*:}" ] || exit 1; printf '%s\t%s\n' "$1" "$(cat "$T/nv.${1##*:}")";;
esac''',
    # stat -f %Su /dev/console is the console user; anything else goes to the real stat
    "stat": r'''#!/bin/bash
if [ "$3" = /dev/console ]; then echo "$CONSOLE"; exit 0; fi
exec /usr/bin/stat "$@"''',
    # the 45 s wait: a WindowServer that dies meanwhile leaves its report
    "sleep": r'''#!/bin/bash
n=$(cat "$T/sleep.n" 2>/dev/null || echo 0); echo $((n + 1)) > "$T/sleep.n"
if [ "$1" = 45 ] && [ "$n" -lt "${CRASHES:-0}" ]; then : > "$DRDIR/WindowServer-2026-11-07-22$n.ips"; fi
exit 0''',
    "shutdown": r'''#!/bin/bash
echo "shutdown $*" >> "$T/calls"''',
    # what `ioreg -l -w0` prints: the properties the test gave it; `ioreg -r -k nvrm-claimed` the claimed card's
    # subtree and `ioreg -r -c IODisplayConnect` every display, as the test gave them
    "ioreg": r'''#!/bin/bash
case " $* " in
  *" -k nvrm-claimed "*) [ -z "$CARD" ] || printf '%s\n' "$CARD";;
  *" -c IODisplayConnect "*) [ -z "$DISPLAYS" ] || printf '%s\n' "$DISPLAYS";;
  *" -r "*) ;;
  *) [ -z "$REGISTRY" ] || printf '%s\n' "$REGISTRY";;
esac
exit 0''',
}


# a start of this driver: NVRM claimed the card and the framebuffer reported its display path up
CLAIMED = '    | |   "nvrm-claimed" = Yes'
GOOD = dict(registry=CLAIMED, cleared="1")


def run(job, *, pids, armed="1", crashes=0, console="root", prior_loop=None, registry="", cleared="", card="",
        displays="", starts=None):
    t = pathlib.Path(tempfile.mkdtemp(prefix="bgws-"))
    fake = t / "bin"
    fake.mkdir()
    for name, body in FAKES.items():
        p = fake / name
        p.write_text(body + "\n")
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
    dr = t / "DiagnosticReports"
    dr.mkdir()
    # a report from an earlier boot must not count
    old = dr / "WindowServer-2026-11-01-000000.ips"
    old.write_text("")
    os.utime(old, (time.time() - 86400, time.time() - 86400))
    if prior_loop is not None:
        (t / "nv.nullmoth-ws-loop").write_text(prior_loop + "\n")
    if starts is not None:
        (t / "nv.nullmoth-nv-starts").write_text(starts + "\n")
    text = redirect(job, fake, t, dr)
    script = t / "bootguard.sh"
    script.write_text(text)
    env = dict(os.environ, T=str(t), WS_PIDS=" ".join(map(str, pids)), ARMED=armed or "", CRASHES=str(crashes),
               CONSOLE=console, DRDIR=str(dr), BOOT_SEC=str(int(time.time()) - 120), REGISTRY=registry,
               CLEARED=cleared, CARD=card, DISPLAYS=displays)
    try:
        # JOB_BASH: the shell the job runs under (macOS runs it with bash 3.2)
        p = subprocess.run([os.environ.get("JOB_BASH", "/bin/bash"), str(script)], env=env, capture_output=True,
                           text=True, timeout=120)
        log = (t / "bootguard.log").read_text() if (t / "bootguard.log").exists() else ""
        calls = (t / "calls").read_text() if (t / "calls").exists() else ""
    finally:
        shutil.rmtree(t, ignore_errors=True)
    return p, log, calls


class Unredirected(Exception):
    pass


# Every absolute path that names a place this machine has, so that one the job did not move stands out. The fakes come
# first on PATH, but a file operation (rm, cp, ...) is given its path as an argument and does not go through PATH: a job
# line that named a real /Library or /System path would act on it with the real tool, whatever PATH held. The three
# lines below are redirected into the test's folder; any other such path left in the job is refused rather than run.
ABS = re.compile(r"/(?:Library|Users|System|Volumes|Applications|Network|private|usr|opt|etc|var|tmp|bin|sbin|dev|cores)"
                 r"(?![A-Za-z0-9_])[^\s\"';)]*")


def redirect(job, fake, t, dr):
    """The job with its PATH, log and report folder pointed at the test's. The job runs nvram and shutdown -r now: when
    a line moved and the fakes would not come first, nothing runs (the real ones would reach this Mac's NVRAM and restart
    it when the tests run as root). Any absolute path the job names past these redirects (a file operation does not go
    through PATH) is refused, so a job changed to touch a real /Library or /System path is not run."""
    text = job
    mine = f"PATH={fake}:/usr/bin:/bin:/usr/sbin:/sbin"
    for old, new in (("PATH=/usr/bin:/bin:/usr/sbin:/sbin", mine),
                     ("LOG=/Library/NullMoth/bootguard.log", f"LOG={t}/bootguard.log"),
                     ("DR=/Library/Logs/DiagnosticReports", f"DR={dr}")):
        if text.count(old) != 1:
            if old.startswith("DR=") and text.count("DiagnosticReports") == 0:
                continue   # 2.8.1's job reads no reports
            raise Unredirected(f"{old!r} occurs {text.count(old)} times in the job")
        text = text.replace(old, new)
    for prog in FAKES:
        if re.search(rf"(^|[^\w.-])/(usr/)?s?bin/{prog}\b", text):
            raise Unredirected(f"the job names /…/{prog} by its full path, past the fakes")
    if re.search(r"\bcommand\s+-[A-Za-z]*p", text):
        raise Unredirected("the job asks for the system's own PATH (command -p), past the fakes")
    # the PATH line (holds /usr/bin etc. by design, after the fakes) and the shebang are the job's only system paths
    # that are not a bug; everything else must have been moved under the test's folder (or be /dev/null, or the one
    # read of /dev/console's owner through the fake stat).
    for n, line in enumerate(text.split("\n"), 1):
        if line == mine or (n == 1 and line == "#!/bin/bash"):
            continue
        for m in ABS.finditer(line):
            if m.start() and line[m.start() - 1] not in " \t\"'=(>;|&:":
                continue   # inside a longer path, which the match that started it has judged
            if m.group(0) == "/dev/null" or m.group(0).startswith(f"{t}/") or m.group(0).startswith(f"{dr}/"):
                continue
            if m.group(0) == "/dev/console" and line[:m.start()].endswith("stat -f %Su "):
                continue   # only asked who owns it, of the fake stat; any other use is refused
            raise Unredirected(f"the job names {m.group(0)!r}, a path this test did not move under its folder")
    return text


src = (ROOT / "package/install.sh").read_text()
job = job_from(src)
arm("the job still says when a start is good", "this start counts as good" in job)
arm("and reads WindowServer reports from this boot", "DiagnosticReports" in job and "kern.boottime" in job)

loop = list(range(500, 900, 10))   # a new WindowServer at every look

p, log, calls = run(job, pids=[777], **GOOD)
arm("a WindowServer that stays up for the wait counts as good", "counts as good" in log, log + p.stderr)
arm("  and clears the start count and sets nv-ok", "nvram -d 7C436110-AB2A-4BBB-A880-FE41995C9F82:nullmoth-nv-starts" in calls
    and "nullmoth-nv-ok=1" in calls, calls)
arm("  and turns nothing off", "nv-starts=%02" not in calls and "shutdown" not in calls, calls)

p, log, calls = run(job, pids=loop, crashes=99)
arm("a WindowServer that keeps dying on the NVIDIA display turns the driver off for the next boot",
    "nullmoth-nv-starts=%02" in calls and "nvram -d 7C436110-AB2A-4BBB-A880-FE41995C9F82:nullmoth-nv-ok" in calls, log + calls)
arm("  and never calls that start good", "counts as good" not in log and "nullmoth-nv-ok=1" not in calls, log)
arm("  and restarts once from the login window", "shutdown -r now" in calls, calls)
arm("  and leaves the reason for the desktop report", "nullmoth-ws-loop=" in calls, calls)

p, log, calls = run(job, pids=loop, crashes=99, console="user1")
arm("with someone logged in it turns the driver off but does not restart", "nv-starts=%02" in calls
    and "shutdown" not in calls, calls)

p, log, calls = run(job, pids=loop, crashes=99, prior_loop="5")
arm("a second loop in a row does not restart again", "nv-starts=%02" in calls and "shutdown" not in calls, calls)

p, log, calls = run(job, pids=loop, crashes=99, armed=None)
arm("a WindowServer loop on a display this driver does not drive is left alone", "nv-starts=%02" not in calls
    and "shutdown" not in calls and "four minutes" in log, log + calls)

p, log, calls = run(job, pids=[600, 610, 620, 620, 620, 620, 620, 620, 620, 620], crashes=2, **GOOD)
arm("two crashes at boot and then a steady WindowServer still counts as good", "counts as good" in log
    and "nv-starts=%02" not in calls, log + calls)

p, log, calls = run(job, pids=loop, crashes=0)
arm("a WindowServer seen as four processes counts as a loop even with no reports", "nv-starts=%02" in calls, log + calls)

# a framebuffer that refused this macOS's classes: NVRM declined the card, and a WindowServer that stays up (with no
# display, on another GPU's, or on Apple's firmware framebuffer where OpenCore does not exclude IONDRVSupport) is not a
# good start of this driver. The texts are the ones NVRM writes.
LOOK = "      if ioreg -l -w0 2>/dev/null | grep -q '\"nvrm-layout\" = \"declined'; then\n"
written = re.findall(r'setProperty\("nvrm-layout",\s*"([^"]*)"\)', (ROOT / "kexts/NVRM/NVRM.cpp").read_text())
arm("NVRM writes its refusal on the PCI device, early and late", len(written) == 2, written)
arm("the job looks for it before it calls a start good", job.count(LOOK) == 1
    and job.index(LOOK) < job.index("nvram $V:nullmoth-nv-ok=1"))
for text in written:
    p, log, calls = run(job, pids=[777], registry=f'    | |   "nvrm-layout" = "{text}"')
    arm(f"a WindowServer that stays up after NVRM {text.split(':')[0]} the card is not a good start",
        "counts as good" not in log and "does not count as good" in log and "nullmoth-nv-ok=1" not in calls,
        log + calls)
    arm("  and the start count and the driver are left as they are", "nullmoth-nv-starts" not in calls
        and "shutdown" not in calls, calls)
p, log, calls = run(job, pids=[777], cleared="1",
                    registry='    | |   "nvrmfb-layout" = "ok: 3 classes, vtables not compared; macOS 25G342"\n'
                             '    | |   "nvaccel-layout" = "refused: IOAccelDevice2 is 0x90 bytes in this macOS"\n'
                             + CLAIMED)
arm("the other layout lines are not NVRM's refusal: the desktop that stayed up counts as good", "counts as good" in log
    and "nullmoth-nv-ok=1" in calls, log + calls)
PCI = "+-o GFX0@0  <class IOPCIDevice, id 0x1000002a0, registered, matched, active, busy 0 (120 ms), retain 18>"
ON_CARD = "+-o display0  <class IODisplayConnect, id 0x1000004f8, registered, matched, active, busy 0 (1 ms), retain 6>"
ELSEWHERE = ON_CARD.replace("0x1000004f8", "0x1000004e0")
if written and job.count(LOOK) == 1:
    # the late decline: NVRM had claimed the card and its framebuffer then refused, so nothing clears the guard in that
    # start. With a display on another GPU the look is all that keeps the job from calling it good.
    late = dict(pids=[777], registry=f'    | |   "nvrm-layout" = "{written[-1]}"\n' + CLAIMED, card=PCI,
                displays=ELSEWHERE)
    p, log, calls = run(job, **late)
    arm("a late decline with a display on another GPU is not a good start", "does not count as good" in log
        and "nullmoth-nv-ok=1" not in calls and "nullmoth-nv-starts" not in calls, log + calls)
    p, log, calls = run(job.replace(LOOK, "      if false; then\n"), **late)
    arm("control: without the look the job calls the refused start good (what ends the install's trial)",
        "counts as good" in log and "nullmoth-nv-ok=1" in calls, log + calls)
else:
    arm("control: the job without its look at the registry can be made", False)

# A desktop that stayed up ends the install's trial (nullmoth-nv-ok) only when it is this driver's: NVRM claimed a card
# and either its framebuffer reported the display path up (debug.nvrmfb_guard_cleared) or a display hangs off another
# GPU. With no NVIDIA driver running the count is cleared and the trial goes on; with the card claimed and nothing lit
# the start keeps its count.
NVOK = "nullmoth-nv-ok=1"
DEL_STARTS = "nvram -d 7C436110-AB2A-4BBB-A880-FE41995C9F82:nullmoth-nv-starts"
CASES = {
    "a": dict(registry=CLAIMED, cleared="1", card=PCI + "\n" + ON_CARD, displays=ON_CARD, starts="%01"),
    "b": dict(registry="", starts="%01"),
    "c": dict(registry=CLAIMED, starts="%01", card=PCI),
    "d": dict(registry=CLAIMED, starts="%01", card=PCI, displays=ELSEWHERE),
    "e": dict(registry=CLAIMED, starts="%01", card=PCI + "\n" + ON_CARD, displays=ON_CARD),
    "f": dict(registry="", starts="%02"),
}
p, log, calls = run(job, pids=[777], **CASES["a"])
arm("(a) the card claimed and its display path up: the start counts as good", "counts as good" in log
    and DEL_STARTS in calls and NVOK in calls, log + calls)
p, log, calls = run(job, pids=[777], **CASES["b"])
arm("(b) no NVIDIA driver running (no kext loaded yet): the count is cleared, the trial goes on, no nv-ok",
    DEL_STARTS in calls and NVOK not in calls and "with no NVIDIA driver running" in log
    and "the install's trial goes on" in log and "counts as good" not in log, log + calls)
p, log, calls = run(job, pids=[777], **CASES["c"])
arm("(c) the card claimed and nothing lit yet (the display armed late): the start keeps its count, nothing written",
    "this start keeps its count" in log and "nvram -d" not in calls and "=" not in calls, log + calls)
p, log, calls = run(job, pids=[777], **CASES["d"])
arm("(d) the card claimed, not lit, a display on another GPU (a laptop's panel): the start counts as good",
    "counts as good" in log and NVOK in calls, log + calls)
p, log, calls = run(job, pids=[777], **CASES["e"])
arm("(e) the card claimed, not lit, the only display under the card: the start keeps its count",
    "this start keeps its count" in log and NVOK not in calls and "nullmoth-nv-starts" not in calls, log + calls)
p, log, calls = run(job, pids=[777], **CASES["f"])
arm("(f) the driver off after two dark starts: the count is cleared as before, no nv-ok", DEL_STARTS in calls
    and NVOK not in calls and "with no NVIDIA driver running" in log, log + calls)
# the controls: the job with the two lines it had before this rule calls (b), (c) and (e) good and ends the trial
NEW_START = "      # The driver's desktop: NVRM runs a card"
NEW_END = "      echo \"$(date) the desktop came up and stayed up (WindowServer $p for 45 s); this"
NEW_RULE = job[job.index(NEW_START):job.index(NEW_END)]
OLD_RULE = ("      nvram -d $V:nullmoth-nv-starts 2>/dev/null\n      nvram $V:nullmoth-nv-ok=1 2>/dev/null\n"
            "      nvram -d $V:nullmoth-ws-loop 2>/dev/null\n")
old_job = job.replace(NEW_RULE, OLD_RULE)
arm("control: the job with the older rule can be made", old_job != job and job.count(NEW_RULE) == 1)
for c in "bce":
    p, log, calls = run(old_job, pids=[777], **CASES[c])
    arm(f"control: with the older rule ({c}) writes nv-ok and counts as good",
        NVOK in calls and "counts as good" in log, log + calls)
# the registry ids are compared whole: a display whose id only begins like one under the card is another display
p, log, calls = run(job, pids=[777], **dict(CASES["e"], displays=ON_CARD.replace("0x1000004f8", "0x1000004f80")))
arm("  a display whose id only begins like the card's is another GPU's: the start counts as good",
    "counts as good" in log and NVOK in calls, log + calls)

# an accelerator that refused this macOS registers no debug.nvaccelfb, but NVRMFB lights the display: a WindowServer
# that keeps dying there is this driver's loop when every display hangs off the card NVRM drives, and not when a
# display is on another GPU or there is none
REF = '    | |   "nvaccel-layout" = "refused: IOAccelDevice2 is 0x90 bytes in this macOS"'
RULE = '    if { [ "$r1" -ge 3 ] || [ "$#" -ge 4 ]; } && [ "$ours" = 1 ]; then\n'
OLD_LOOP = '    if { [ "$r1" -ge 3 ] || [ "$#" -ge 4 ]; } && [ "$(sysctl -n debug.nvaccelfb 2>/dev/null)" = 1 ]; then\n'
p, log, calls = run(job, pids=loop, crashes=99, armed=None, registry=REF + "\n" + CLAIMED, card=PCI + "\n" + ON_CARD,
                    displays=ON_CARD)
arm("a loop on the only display, with the accelerator refused, turns the driver off and restarts once",
    "nullmoth-nv-starts=%02" in calls and "shutdown -r now" in calls, log + calls)
for what, card, disp in (("on another GPU only", PCI, ELSEWHERE), ("on both GPUs", PCI + "\n" + ON_CARD,
                                                                     ON_CARD + "\n" + ELSEWHERE), ("nowhere", PCI, "")):
    p, log, calls = run(job, pids=loop, crashes=99, armed=None, registry=REF + "\n" + CLAIMED, card=card, displays=disp)
    arm(f"a loop with the accelerator refused and a display {what} is left alone",
        "nv-starts=%02" not in calls and "shutdown" not in calls, log + calls)
p, log, calls = run(job, pids=loop, crashes=99, armed=None, registry=REF, card="", displays=ON_CARD)
arm("  and so is one with no card claimed (the driver off: Apple's or the firmware's framebuffer draws)",
    "nv-starts=%02" not in calls and "shutdown" not in calls, log + calls)
if job.count(RULE) == 1:
    p, log, calls = run(job.replace(RULE, OLD_LOOP), pids=loop, crashes=99, armed=None, registry=REF + "\n" + CLAIMED,
                        card=PCI + "\n" + ON_CARD, displays=ON_CARD)
    arm("control: with the armed-only rule the same loop is left alone (what left a black screen at every start)",
        "nv-starts=%02" not in calls and "four minutes" in log, log + calls)
else:
    arm("control: the job's loop rule can be found", False)

# a job whose PATH line moved, or that names nvram by its full path, is not run at all
for what, bad in (("its PATH line changed", job.replace("PATH=/usr/bin:/bin:/usr/sbin:/sbin", "PATH=/bin:/usr/bin:/usr/sbin:/sbin")),
                  ("it calls /usr/sbin/nvram", job.replace("nvram -d", "/usr/sbin/nvram -d", 1)),
                  ("it names a real /Library path for a file operation", job.replace("shutdown -r now", "rm -rf /Library/NullMoth; shutdown -r now")),
                  ("it asks for the system's own PATH", job.replace("shutdown -r now", "command -p shutdown -r now"))):
    try:
        redirect(bad, pathlib.Path("/nonexistent/bin"), pathlib.Path("/nonexistent"), pathlib.Path("/nonexistent/dr"))
        arm(f"control: a job that would reach the real nvram and shutdown is refused ({what})", False, "it would have run")
    except Unredirected:
        arm(f"control: a job that would reach the real nvram and shutdown is refused ({what})", True)

# negative control: the job as 2.8.1 shipped it (tag v2.8.1) calls the looping WindowServer good
base = subprocess.run(["git", "-C", str(ROOT), "show", "v2.8.1:package/install.sh"], capture_output=True, text=True)
if base.returncode == 0:
    p, log, calls = run(job_from(base.stdout), pids=loop, crashes=99)
    arm("negative control: the 2.8.1 job calls a WindowServer that keeps dying good (the bug)",
        "counts as good" in log and "nv-starts=%02" not in calls, log + calls)
else:
    arm("negative control: 2.8.1's install.sh is readable", False, base.stderr)

print(f"{fails} failed")
sys.exit(1 if fails else 0)
