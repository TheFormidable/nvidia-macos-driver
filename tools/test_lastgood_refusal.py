#!/usr/bin/env python3
"""The fallback job puts the previous driver back when the new one's framebuffer refuses this macOS's classes.

NVRMFB compares the sizes of Apple's framebuffer classes on the running macOS with the ones it was built for. When they
differ NVRM declines the card before the kernel's boot guard counts the start, and every framebuffer of the driver
refuses for the same reason, the one that shows the desktop with the driver off included. Where the OpenCore setup
excludes Apple's own firmware framebuffer, no desktop comes up at that start or at any later one; where it does not,
Apple's firmware framebuffer takes the card and shows a slow desktop without acceleration. Either way the guard, which
only counts starts that began, never turns anything off: the job that restores the previous driver only looked at the
guard's count and never ran its restore. NVRM writes the refusal on the PCI device (nvrm-layout = "declined ..."), and the job now
reads it there, every five seconds for four minutes, because the kexts can start after the job does.

The job is lifted out of install.sh, filled in the way the installer fills it, and run against fake ioreg, nvram, ditto,
sleep and shutdown with every path of it moved into a folder of the test's. The job removes kexts and restarts the Mac,
so nothing of it runs unless every path it names lies in that folder, its PATH holds that folder only, and the real rm,
cp and mkdir it reaches there refuse an argument that is not under the folder. The negative control is the same job
without its look at the registry, which must leave the refused driver in place.

Two conditions belong to the refusal alone. The previous driver is put back only on the macOS build the install was
made on: it was running there, so it fits; after a macOS update the refusal may be the update's doing, and the driver
that was replaced has no such check. And the Mac is restarted at once unless someone may be working: a display is
connected to another GPU's framebuffer and a user owns the console. Then the files are put back and the restart is
left to the user. A display under the declined card itself (Apple's firmware framebuffer, where OpenCore does not
exclude IONDRVSupport) does not hold the restart back: the job subtracts, by registry id, the displays that
`ioreg -r -k nvrm-layout` prints under the card from all of them. The job's older rule (two starts in a row without a
display) is compared with 2.9's job and must not change."""
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
fails = 0


def arm(name, ok, detail=""):
    global fails
    fails += not ok
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f" ({str(detail).strip()[-600:]})"))


HEAD = 'cat > "$LG.tmp" <<EOF || die "write the fallback job"\n'
# what the installer has in these when it writes the job (install.sh, its first lines and step 4)
INSTALLER = {
    "BK": "/Library/NullMoth/backup-20261113-000000.TESTED",
    "KEXTS": "NVRM NVAccel NVRMFB NVRMAGDC",
    "EXT": "/Library/Extensions",
    "GB": "/Library/GPUBundles",
    "FW": "/Library/NullMoth/nvfw",
    "FWOLD": "/Users/Shared/nvfw",
    "KC": "/Library/KernelCollections/AuxiliaryKernelExtensions.kc",
    "BUILD": "25G342",
    # the fallback the job restores: this run's backup, or the one an install still on trial kept, and its macOS build
    "FB": "/Library/NullMoth/backup-20261113-000000.TESTED",
    "FBOSB": "25G342",
}
PATH_LINE = "PATH=/usr/bin:/bin:/usr/sbin:/sbin"
LOOK = "refused() { ioreg -l -w0 2>/dev/null | grep -q '\"nvrm-layout\" = \"declined'; }"


class Unsafe(Exception):
    pass


def template(src):
    """The job as install.sh holds it: the here-document it writes to lastgood.sh."""
    if src.count(HEAD) != 1:
        raise Unsafe(f"the line that writes the fallback job occurs {src.count(HEAD)} times in install.sh")
    body = src[src.index(HEAD) + len(HEAD):]
    return body[:body.index("\nEOF\n") + 1]


def fill(tmpl, **other):
    """The job as the installer writes it to disk. Only the installer's own variables may be filled in here: a command
    substitution that is not escaped would run now, in the test, with whatever it names. `other` replaces one of the
    installer's values (an install that could not read its macOS build, for instance)."""
    if re.search(r"(?<!\\)\$\(|`", tmpl):
        raise Unsafe("the job's template runs a command while it is written")
    names = set(re.findall(r"(?<!\\)\$\{?([A-Za-z_][A-Za-z0-9_]*)", tmpl))
    if not names <= set(INSTALLER):
        raise Unsafe(f"the job's template fills in {sorted(names - set(INSTALLER))}, which this test does not know")
    if not set(other) <= set(INSTALLER):
        raise Unsafe(f"{sorted(set(other) - set(INSTALLER))} is not one of the installer's variables")
    script = "".join(f"{k}='{v}'\n" for k, v in {**INSTALLER, **other}.items()) + "cat <<EOF\n" + tmpl + "EOF\n"
    p = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, timeout=60,
                       env={"PATH": "/usr/bin:/bin"})
    if p.returncode != 0 or not p.stdout.startswith("#!/bin/bash\n"):
        raise Unsafe(f"the job could not be filled in: {p.stderr[-300:]}")
    return p.stdout


ABS = re.compile(r"/(?:Library|Users|System|Volumes|Applications|Network|private|usr|opt|etc|var|tmp|bin|sbin|dev|cores)"
                 r"(?![A-Za-z0-9_])[^\s\"';)]*")


def redirect(job, fake, t):
    """The job with every path it names moved under t and a PATH that holds nothing but the test's folder: the fakes,
    and links to the few real tools in REAL. Any other program the job calls is not found. Raises instead of returning
    a job that could reach this machine: one that names a tool by its full path, or a path this function did not
    move."""
    if job.count(PATH_LINE) != 1:
        raise Unsafe(f"the job's PATH line occurs {job.count(PATH_LINE)} times")
    for prog in FAKES:
        if re.search(rf"(^|[^\w.-])/(usr/)?s?bin/{prog}\b", job, re.M):
            raise Unsafe(f"the job names /…/{prog} by its full path, past the fakes")
    if re.search(r"\bcommand\s+-[A-Za-z]*p", job):
        raise Unsafe("the job asks for the system's own PATH (command -p), past the fakes")
    text = re.sub(r"/(Library|Users)/", lambda m: f"{t}/{m.group(1)}/", job)   # one pass: t itself is not looked at
    mine = f"PATH={fake}"
    text = text.replace(PATH_LINE, mine)
    for n, line in enumerate(text.split("\n"), 1):
        if line == mine or (n == 1 and line == "#!/bin/bash"):
            continue
        for m in ABS.finditer(line):
            if m.start() and line[m.start() - 1] not in " \t\"'=(>;|&:":
                continue   # inside a longer path, which the match that started it has judged
            if m.group(0) == "/dev/null" or m.group(0).startswith(f"{t}/"):
                continue
            if m.group(0) == "/dev/console" and line[:m.start()].endswith("stat -f %Su "):
                continue   # only asked who owns it, of stat, which is a fake here; any other use is refused
            raise Unsafe(f"line {n} of the job names {m.group(0)!r}, outside the test's folder")
    return text


# The real tools the job and the fakes may use. The three that change files are reached through a guard that refuses
# an argument outside the test's folder (a path the job builds while it runs is not seen by redirect()).
REAL = ("cut", "grep", "date", "cat")
GUARDED = ("rm", "cp", "mkdir", "mv")
GUARD = r'''#!/bin/bash
for a in "$@"; do
  case "$a" in
    */../*|*/..) echo "REFUSED a path with ..: @NAME@ $*" >> "$T/calls"; exit 97;;
    -*|"$T"/*) ;;
    *) echo "REFUSED outside the test's folder: @NAME@ $*" >> "$T/calls"; exit 97;;
  esac
done
exec "@REAL@" "$@"'''

FAKES = {
    # what `ioreg -l -w0` prints: the file the test wrote (and may write again between two looks)
    "ioreg": r'''#!/bin/bash
# `ioreg -r -c IODisplayConnect`: the displays connected to this Mac's framebuffers, `-r -c IOFramebuffer` the
# framebuffers themselves, and `-r -k nvrm-layout` the subtree of the card NVRM declined (its PCI device and everything
# under it); none unless the test wrote some (none is one of the looks, and each is written down in the calls).
# Without -r, -c and -k only pick whose properties are shown: the whole tree is printed, as the real ioreg does.
case " $* " in *" -r "*) echo "ioreg $*" >> "$T/calls";; esac
case " $* " in
  *" -c "*|*" -k "*)
    case " $* " in *" -r "*) ;; *)
      echo "+-o Root  <class IORegistryEntry, id 0x100000100, retain 30>"
      cat "$T/card.txt" "$T/fbs.txt" "$T/displays.txt" "$T/ioreg.txt" 2>/dev/null; exit 0;;
    esac;;
esac
case " $* " in
  *" -c IODisplayConnect "*) [ ! -f "$T/displays.txt" ] || cat "$T/displays.txt"; exit 0;;
  *" -c IOFramebuffer "*) [ ! -f "$T/fbs.txt" ] || cat "$T/fbs.txt"; exit 0;;
  *" -k nvrm-layout "*) [ ! -f "$T/card.txt" ] || cat "$T/card.txt"; exit 0;;
  *" -k "*|*" -c "*) exit 0;;
esac
n=$(cat "$T/ioreg.n" 2>/dev/null || echo 0); echo $((n + 1)) > "$T/ioreg.n"
[ ! -f "$T/ioreg.txt" ] || cat "$T/ioreg.txt"
exit 0''',
    "nvram": r'''#!/bin/bash
echo "nvram $*" >> "$T/calls"
case "$1" in
  -d) rm -f "$T/nv.${2##*:}";;
  *=*) k=${1%%=*}; echo "${1#*=}" > "$T/nv.${k##*:}";;
  *) [ -f "$T/nv.${1##*:}" ] || exit 1; printf '%s\t%s\n' "$1" "$(cat "$T/nv.${1##*:}")";;
esac''',
    # ditto copies a file, or the contents of a folder into the target folder
    "ditto": r'''#!/bin/bash
echo "ditto $*" >> "$T/calls"
if [ -d "$1" ]; then mkdir -p "$2" && cp -R "$1"/. "$2"/; else cp "$1" "$2"; fi''',
    # the wait between two looks: no time passes, and what the test wants to happen meanwhile happens (during the n-th
    # wait: registry.<n> is added to what ioreg prints, nv.<n> is a shell line that changes the fake NVRAM)
    "sleep": r'''#!/bin/bash
n=$(cat "$T/sleep.n" 2>/dev/null || echo 0); n=$((n + 1)); echo $n > "$T/sleep.n"
[ "$1" = 5 ] || echo "sleep $*" >> "$T/calls"
[ ! -f "$T/registry.$n" ] || cat "$T/registry.$n" >> "$T/ioreg.txt"
[ ! -f "$T/nvram.$n" ] || . "$T/nvram.$n"
exit 0''',
    # this boot's name: the file the test wrote; no file, no answer. Any other question is refused.
    "sysctl": r'''#!/bin/bash
[ "$*" = "-n kern.bootsessionuuid" ] || { echo "sysctl $*" >> "$T/calls"; exit 1; }
[ -f "$T/boot.txt" ] || exit 1
cat "$T/boot.txt"''',
    "shutdown": r'''#!/bin/bash
echo "shutdown $*" >> "$T/calls"''',
    # the running macOS build: the file the test wrote; no file, no answer (the tool failed). osbuild.fail holds how
    # many questions fail before it answers.
    "sw_vers": r'''#!/bin/bash
[ "$*" = "-buildVersion" ] || { echo "sw_vers $*" >> "$T/calls"; exit 1; }
echo "sw_vers $*" >> "$T/asked"
f=$(cat "$T/osbuild.fail" 2>/dev/null || echo 0)
if [ "$f" -gt 0 ]; then echo $((f - 1)) > "$T/osbuild.fail"; exit 1; fi
[ -f "$T/osbuild.txt" ] || exit 1
cat "$T/osbuild.txt"''',
    # who owns the console: the file the test wrote; no file, no answer (stat failed). Any other question is refused.
    "stat": r'''#!/bin/bash
[ "$*" = "-f %Su /dev/console" ] || { echo "stat $*" >> "$T/calls"; exit 1; }
[ -f "$T/console.txt" ] || exit 1
cat "$T/console.txt"''',
}

def ioreg_line(text):
    return f'    | |   "nvrm-layout" = "{text}"\n'


FB_REFUSED = ('    | |   "nvrmfb-layout" = "refused: IOFramebuffer is 0x1e0 bytes in this macOS, 0x1d0 in the driver '
              '(1 size, 0 vtables differ of 3, 0); macOS 25G342 (Darwin 25.7.0)"\n')
FB_OK = '    | |   "nvrmfb-layout" = "ok: 3 classes, vtables not compared; macOS 25G342 (Darwin 25.7.0)"\n'
# what `ioreg -r -c IOFramebuffer -w0` prints on a Mac with a second GPU, and `ioreg -r -c IODisplayConnect -w0` when a
# display is connected to it
OTHER_FB = ('+-o AppleIntelFramebuffer@0  <class AppleIntelFramebuffer, id 0x1000004d2, registered, matched, active, '
            'busy 0 (9 ms), retain 14>\n')
OTHER_DISPLAY = ('+-o display0  <class IODisplayConnect, id 0x1000004e0, registered, matched, active, busy 0 (2 ms), '
                 'retain 6>\n')
# the same two on a Mac whose OpenCore does not exclude IONDRVSupport: once the driver's framebuffer has refused, Apple's
# firmware framebuffer holds the NVIDIA card itself and gets a display once WindowServer opens it
APPLE_FB = ('+-o .Display_boot  <class IONDRVFramebuffer, id 0x1000004f1, registered, matched, active, busy 0 (3 ms), '
            'retain 12>\n')
APPLE_DISPLAY = ('+-o display0  <class IODisplayConnect, id 0x1000004f8, registered, matched, active, busy 0 (1 ms), '
                 'retain 6>\n')
# what `ioreg -r -k nvrm-layout -w0` prints after a refusal: the declined card's PCI device and its subtree, with
# Apple's framebuffer in it, and its display once WindowServer has opened that framebuffer (as a Mac's read of the same
# command for nvrm-claimed showed: one subtree, rooted at the IOPCIDevice, the display under the framebuffer)
CARD_PCI = ('+-o GFX0@0  <class IOPCIDevice, id 0x1000002a0, registered, matched, active, busy 0 (120 ms), '
            'retain 18>\n')
CARD = CARD_PCI + APPLE_FB
CARD_LIT = CARD + APPLE_DISPLAY
# a second NVIDIA card, declined as well, with Apple's framebuffer drawing on it too
APPLE_DISPLAY2 = ('+-o display0  <class IODisplayConnect, id 0x100000a18, registered, matched, active, busy 0 (1 ms), '
                  'retain 6>\n')
CARD2_LIT = ('+-o GFX1@0  <class IOPCIDevice, id 0x100000a00, registered, matched, active, busy 0 (98 ms), retain 17>\n'
             '+-o .Display_boot  <class IONDRVFramebuffer, id 0x100000a11, registered, matched, active, busy 0 (2 ms), '
             'retain 12>\n' + APPLE_DISPLAY2)
NAMES = ("NVRM", "NVAccel", "NVRMFB", "NVRMAGDC")


class Result:
    pass


def run(job, *, registry="", nv=None, later=None, nv_later=None, backup=True, keep=False, osbuild=INSTALLER["BUILD"],
        osbuild_fail=0, fbs="", displays="", console="root", card="", kc_fails=None, boot="BOOT-THIS", seen=None,
        seen_dir=False):
    """One start of the Mac with the job installed. registry is what ioreg prints at the first look and nv the NVRAM
    variables at the job's start; later[n] is added to the registry during the n-th wait, and nv_later[n] is a shell
    line that changes the fake NVRAM during it. osbuild is the macOS build that runs (None: it cannot be read) and
    osbuild_fail how many times sw_vers fails before it says so, fbs the framebuffers the registry holds, displays
    what ioreg prints for the displays connected to them (none: nothing shows a picture), card what it prints for the
    subtree of the PCI device that carries nvrm-layout (none: no such device, or ioreg found nothing) and console the
    owner of /dev/console (None: stat fails)."""
    t = pathlib.Path(tempfile.mkdtemp(prefix="lgr-"))
    r = Result()
    try:
        fake = t / "bin"
        fake.mkdir()
        for name, body in FAKES.items():
            p = fake / name
            p.write_text(body + "\n")
            p.chmod(p.stat().st_mode | stat.S_IXUSR)
        for name in REAL + GUARDED:
            real = shutil.which(name, path="/usr/bin:/bin")
            if not real:
                raise Unsafe(f"no {name} in /usr/bin or /bin")
            if name in GUARDED:
                (fake / name).write_text(GUARD.replace("@NAME@", name).replace("@REAL@", real) + "\n")
                (fake / name).chmod(0o755)
            else:
                (fake / name).symlink_to(real)
        if kc_fails:   # the copy (cp) or the rename (mv) of the kernel collection fails; a cut cp leaves a short file
            tool = kc_fails
            (fake / tool).rename(fake / (tool + ".guard"))
            (fake / tool).write_text("#!/bin/bash\n"
                'case "$*" in *AuxiliaryKernelExtensions.kc*Library/KernelCollections/AuxiliaryKernelExtensions.kc*)\n'
                '  echo "' + tool + ' FAILED $*" >> "$T/calls"\n'
                + ('  printf partial > "${@: -1}"\n' if tool == "cp" else "")
                + '  exit 1;; esac\n'
                'exec "' + str(fake) + '/' + tool + '.guard" "$@"\n')
            (fake / tool).chmod(0o755)
        text = redirect(job, fake, t)
        lib = t / "Library"
        bk = t / INSTALLER["BK"].lstrip("/")
        for d in ("Extensions", "GPUBundles", "NullMoth/kexts/26", "NullMoth/nvfw", "KernelCollections", "LaunchDaemons"):
            (lib / d).mkdir(parents=True)
        for k in NAMES:   # the driver that refused, installed
            (lib / "Extensions" / f"{k}.kext").mkdir()
            (lib / "Extensions" / f"{k}.kext" / "which").write_text("new\n")
        (lib / "GPUBundles/NVMTLDriver.bundle").mkdir()
        (lib / "GPUBundles/NVMTLDriver.bundle/which").write_text("new\n")
        (lib / "KernelCollections/AuxiliaryKernelExtensions.kc").write_text("new\n")
        (lib / "NullMoth/nvfw/which").write_text("new\n")
        (lib / "NullMoth/driver-version").write_text("3.0\n")
        plist = lib / "LaunchDaemons/com.nullmoth.lastgood.plist"
        script = lib / "NullMoth/lastgood.sh"
        plist.write_text("plist\n")
        script.write_text(text)
        if backup:   # the driver the install replaced
            for k in NAMES:
                (bk / f"{k}.kext").mkdir(parents=True)
                (bk / f"{k}.kext" / "which").write_text("previous\n")
            (bk / "NVMTLDriver.bundle").mkdir()
            (bk / "NVMTLDriver.bundle/which").write_text("previous\n")
            (bk / "AuxiliaryKernelExtensions.kc").write_text("previous\n")
            (bk / "nvfw").mkdir()
            (bk / "nvfw/which").write_text("previous\n")
            (bk / "driver-version").write_text("2.9\n")
        (t / "ioreg.txt").write_text(registry)
        if osbuild is not None:
            (t / "osbuild.txt").write_text(osbuild + "\n")
        (t / "osbuild.fail").write_text(f"{osbuild_fail}\n")
        if fbs:
            (t / "fbs.txt").write_text(fbs)
        if displays:
            (t / "displays.txt").write_text(displays)
        if card:
            (t / "card.txt").write_text(card)
        if console is not None:
            (t / "console.txt").write_text(console + "\n")
        if boot is not None:
            (t / "boot.txt").write_text(boot + "\n")
        if seen is not None:
            (lib / "NullMoth/lastgood.seen").write_text(seen + "\n")
        if seen_dir:   # a note that cannot be written
            (lib / "NullMoth/lastgood.seen").mkdir()
        for name, value in (nv or {}).items():
            (t / f"nv.{name}").write_text(value + "\n")
        for n, more in (later or {}).items():
            (t / f"registry.{n}").write_text(more)
        for n, line in (nv_later or {}).items():
            (t / f"nvram.{n}").write_text(line + "\n")
        # JOB_BASH: the shell the job runs under (macOS runs it with bash 3.2)
        p = subprocess.run([os.environ.get("JOB_BASH", "/bin/bash"), str(script)],
                           env={"T": str(t), "PATH": "/usr/bin:/bin"}, capture_output=True, text=True, timeout=300)

        def read(path):
            return path.read_text() if path.exists() else ""
        r.rc, r.err = p.returncode, p.stderr
        r.troot = str(t)
        r.fake = fake if keep else None
        r.log = read(lib / "NullMoth/lastgood.log")
        r.calls = read(t / "calls")
        r.asked = read(t / "asked").count("sw_vers")
        r.sleeps = int(read(t / "sleep.n") or 0)
        r.looks = int(read(t / "ioreg.n") or 0)
        r.kexts = [read(lib / "Extensions" / f"{k}.kext" / "which").strip() for k in NAMES]
        r.bundle = read(lib / "GPUBundles/NVMTLDriver.bundle/which").strip()
        r.kc = read(lib / "KernelCollections/AuxiliaryKernelExtensions.kc").strip()
        r.kc_tmp = sorted(x.name for x in (lib / "KernelCollections").iterdir() if x.name != "AuxiliaryKernelExtensions.kc")
        r.fw = read(lib / "NullMoth/nvfw/which").strip()
        r.version = read(lib / "NullMoth/driver-version").strip()
        r.marker = read(lib / "NullMoth/driver-restored").strip()
        seenp = lib / "NullMoth/lastgood.seen"
        r.seen = read(seenp).strip() if seenp.is_file() else ""
        r.job_left = plist.exists() and script.exists()
        r.job_gone = not plist.exists() and not script.exists()
        r.restarted = "shutdown -r now" in r.calls
    finally:
        if not keep:
            shutil.rmtree(t, ignore_errors=True)
    return r


def put_back(r):
    """The previous driver is on disk again and the job took itself away (the restart is a separate question)."""
    return (r.kexts == ["previous"] * 4 and r.bundle == "previous" and r.kc == "previous" and r.fw == "previous"
            and r.version == "2.9" and r.marker == "previous" and r.job_gone)


def restored(r):
    return put_back(r) and r.restarted


def untouched(r):
    return (r.kexts == ["new"] * 4 and r.bundle == "new" and r.kc == "new" and r.fw == "new" and r.version == "3.0"
            and not r.marker and not r.restarted)


# What `date` prints at the start of each log line. The zone is a name where the zone has one (UTC, CET) and a
# number where it has none: "-05" in Bogota, "+0530" in Kolkata.
DATE = re.compile(r"\w{3} \w{3} +\d+ \d{2}:\d{2}:\d{2} [\w+-]+ \d{4}")


def dedate(text):
    """The log with the timestamp of each line replaced, so two runs a second apart compare equal."""
    return DATE.sub("<date>", text)


arm("the differential's date pattern knows a zone written as a name and one written as a number",
    all(dedate(f"{stamp} previous driver restored; restarting") == "<date> previous driver restored; restarting"
        for stamp in ("Fri Nov 13 07:46:02 UTC 2026", "Fri Nov 13 02:46:02 -05 2026", "Fri Nov 13 13:16:02 +0530 2026",
                      "Sun Nov  1 02:46:02 CET 2026")))
arm("control: a pattern that takes the zone for a word leaves Bogota's stamp in the log (two runs a second apart "
    "then differ)",
    re.sub(r"\w{3} \w{3} +\d+ \d{2}:\d{2}:\d{2} \w+ \d{4}", "<date>", "Fri Nov 13 02:46:02 -05 2026 x") != "<date> x")


def changing(r):
    """The calls that change the Mac: NVRAM writes and deletes, file copies, the restart. A plain read of a variable
    (both jobs read nullmoth-nv-ok and nullmoth-nv-starts, the new one on every look) is left out: it changes nothing."""
    return [ln for ln in r.calls.splitlines()
            if ln.startswith(("nvram -d ", "ditto ", "shutdown")) or (ln.startswith("nvram ") and "=" in ln)]


def filestate(r):
    """What the start left on disk and whether it restarted: the four kexts, the bundle, the kernel collection, the
    firmware, the recorded version, the restore marker, whether the job removed itself, and the restart."""
    return (r.kexts, r.bundle, r.kc, r.fw, r.version, r.marker, r.job_gone, r.restarted)


def canon(r):
    """Everything about a run that two jobs must share, with this run's own temporary folder written out so that two
    runs in different folders compare equal: the state-changing calls, the log, and the files left behind."""
    out = [ln.replace(r.troot, "<t>") for ln in changing(r)]
    return out, dedate(r.log).replace(r.troot, "<t>"), filestate(r)


def show(r):
    return f"rc {r.rc} looks {r.looks} sleeps {r.sleeps} kexts {r.kexts} kc {r.kc} job_left {r.job_left}\n{r.log}\n{r.calls}\n{r.err}"


src = (ROOT / "package/install.sh").read_text()
un = (ROOT / "package/uninstall.sh").read_text()
nvrm = (ROOT / "kexts/NVRM/NVRM.cpp").read_text()

# ---- the two ends of the wire: what NVRM writes, and what the two jobs look for
written = re.findall(r'setProperty\("nvrm-layout",\s*"([^"]*)"\)', nvrm)
arm("NVRM writes nvrm-layout on the PCI device when it declines for the framebuffer, early and late",
    len(written) == 2 and all(w.startswith("declined") for w in written), written)
EARLY, LATE = (written + ["", ""])[:2]
try:
    job = fill(template(src))
except Unsafe as e:
    arm("the fallback job can be lifted out of install.sh", False, e)
    print(f"{fails} failed")
    sys.exit(1)
arm("the fallback job looks for exactly that word in the registry", job.count(LOOK) == 1, job[:900])
arm("  and so does the boot-guard job, before it calls a start good",
    src.count("if ioreg -l -w0 2>/dev/null | grep -q '\"nvrm-layout\" = \"declined'; then") == 1)
syntax = subprocess.run(["/bin/bash", "-n"], input=job, capture_output=True, text=True)
arm("the job as written to disk is a valid script", syntax.returncode == 0, syntax.stderr)

# ---- the refusal
r = run(job, registry=FB_REFUSED + ioreg_line(EARLY))
arm("a framebuffer that refused this macOS: the previous driver is put back and the Mac restarts", restored(r), show(r))
arm("  at the first look, without waiting", r.sleeps == 0 and r.looks >= 1, show(r))
arm("  and the log says why and keeps what the framebuffer found (the class and both sizes)",
    "framebuffer refused Apple's classes" in r.log and "IOFramebuffer is 0x1e0 bytes in this macOS, 0x1d0 in the driver" in r.log
    and "declined: " in r.log, r.log)
arm("  and the guard's count is cleared for the driver that comes back",
    "nvram -d 7C436110-AB2A-4BBB-A880-FE41995C9F82:nullmoth-nv-starts" in r.calls, r.calls)

r = run(job, registry=FB_REFUSED, later={3: ioreg_line(EARLY)})
arm("kexts that start after the job: the refusal is found at a later look", restored(r) and r.sleeps == 3, show(r))

r = run(job, registry=ioreg_line(LATE))
arm("the refusal NVRM reads only after it started (declined late) counts the same", restored(r), show(r))

r = run(job, registry=FB_REFUSED + ioreg_line(EARLY), nv={"nullmoth-nv-ok": "1"})
arm("a refusal wins over a display-came-up word left from before", restored(r), show(r))

r = run(job, registry=FB_REFUSED + ioreg_line(EARLY), backup=False)
arm("a refusal with the backup gone: nothing is removed, the job says so and takes itself away",
    untouched(r) and r.job_gone and "its backup" in r.log and "is gone" in r.log, show(r))

# ---- never onto another macOS: the previous driver ran on the build the install was made on, and only there is it
# known to fit. After a macOS update the refusal may be the update's doing, and that driver has no such check.
r = run(job, registry=FB_REFUSED + ioreg_line(EARLY), osbuild="25H100")
arm("a refusal on another macOS build than the install was made on: the previous driver is not put back",
    untouched(r) and r.job_left and r.rc == 0, show(r))
arm("  and the log names both builds and keeps what the framebuffer found",
    "macOS build 25H100" in r.log and "made on 25G342" in r.log and "IOFramebuffer is 0x1e0 bytes" in r.log
    and "restoring" not in r.log, r.log)
r = run(job, registry=FB_REFUSED + ioreg_line(EARLY), osbuild=None)
arm("  nor when the running build cannot be read", untouched(r) and r.job_left and "build unknown" in r.log, show(r))
arm("    after asking three times", r.asked == 3, show(r))
r = run(job, registry=FB_REFUSED + ioreg_line(EARLY), osbuild_fail=1)
arm("  a build that cannot be read once is asked again: the previous driver is put back and the Mac restarts",
    restored(r) and r.asked == 2 and "unknown" not in r.log, show(r))
r = run(job, registry=FB_REFUSED + ioreg_line(EARLY), osbuild_fail=2)
arm("    and so it is at the third question", restored(r) and r.asked == 3, show(r))
try:
    r = run(fill(template(src), FBOSB=""), registry=FB_REFUSED + ioreg_line(EARLY))
    arm("  nor when the install recorded no build", untouched(r) and r.job_left and "an unknown build" in r.log,
        show(r))
    r = run(fill(template(src), FBOSB=""), registry=FB_REFUSED + ioreg_line(EARLY), osbuild=None)
    arm("  nor when neither build is known (two unknowns are not the same build)", untouched(r) and r.job_left, show(r))
except Unsafe as e:
    arm("  nor when the install recorded no build", False, e)
r = run(job, registry=FB_OK, nv={"nullmoth-nv-starts": "%02"}, osbuild="25H100")
arm("  (two starts in a row without a display restore on any build, as before: that rule is 2.9's and is not changed)",
    restored(r), show(r))

# ---- the restart after a refusal: at once, unless someone may be working at a desktop (a lit display, a user)
r = run(job, registry=FB_REFUSED + ioreg_line(EARLY), fbs=OTHER_FB, displays=OTHER_DISPLAY, console="tester")
arm("a refusal while a user is logged in and another GPU shows a display: the previous driver is put back and the "
    "Mac is not restarted", put_back(r) and not r.restarted and r.rc == 0, show(r))
arm("  and the log says that the restart is left to the user, without naming whose display it is",
    "not restarted now" in r.log and "starts at the next restart" in r.log and "restarting" not in r.log
    and "A display is lit and a user is logged in" in r.log and "another GPU" not in r.log, r.log)
r = run(job, registry=FB_REFUSED + ioreg_line(EARLY), fbs=OTHER_FB, displays=OTHER_DISPLAY, console="root")
arm("  at the login window of such a Mac it restarts", restored(r), show(r))
r = run(job, registry=FB_REFUSED + ioreg_line(EARLY), fbs=OTHER_FB, displays="", console="tester")
arm("  a second GPU with nothing plugged into it shows nothing: the Mac restarts whoever owns the console",
    restored(r), show(r))
r = run(job, registry=FB_REFUSED + ioreg_line(EARLY), fbs="", displays="", console="tester")
arm("  and so it does with no framebuffer at all", restored(r), show(r))
r = run(job, registry=FB_OK, nv={"nullmoth-nv-starts": "%02"}, fbs=OTHER_FB, displays=OTHER_DISPLAY, console="tester")
arm("  (two starts in a row without a display restart whoever is logged in, as before)", restored(r), show(r))
r = run(job, registry=FB_REFUSED + ioreg_line(EARLY), fbs=OTHER_FB, displays=OTHER_DISPLAY, console=None)
arm("  a display on another GPU and a console whose owner cannot be read: put back, the restart left to the user",
    put_back(r) and not r.restarted, show(r))
r = run(job, registry=FB_REFUSED + ioreg_line(EARLY), displays="ioreg: could not be reached\n", console="tester")
arm("  any other text from ioreg is not a display: the Mac restarts", restored(r), show(r))

# ---- the usual Mac: one display, on the NVIDIA card, and an OpenCore setup that excludes IONDRVSupport, so nothing
# can show a picture after a refusal. In every order of the job, the kexts and the login it gets the previous driver
# back and restarts.
for label, kw in (
        ("the refusal at the job's first look, at the login window", dict(registry=FB_REFUSED + ioreg_line(EARLY))),
        ("the refusal at the first look, a user logged in by itself", dict(registry=FB_REFUSED + ioreg_line(EARLY),
                                                                             console="tester")),
        ("the kexts after the job, a user logged in", dict(registry="", later={3: FB_REFUSED + ioreg_line(EARLY)},
                                                           console="tester")),
        ("the refusal read late, after a long settle, a user logged in", dict(registry=FB_OK,
                                                                               later={30: ioreg_line(LATE)},
                                                                               console="tester")),
        ("the owner of the console cannot be read", dict(registry=FB_REFUSED + ioreg_line(EARLY), console=None)),
        ("the build answers at the second question, a user logged in",
         dict(registry=FB_REFUSED + ioreg_line(EARLY), osbuild_fail=1, console="tester")),
        ("a display-came-up word from before, a user logged in",
         dict(registry=FB_REFUSED + ioreg_line(EARLY), nv={"nullmoth-nv-ok": "1"}, console="tester"))):
    r = run(job, **kw)
    arm(f"a Mac with its one display on the card gets the previous driver back and restarts: {label}", restored(r),
        show(r))

# ---- the same Mac where OpenCore does not exclude IONDRVSupport: Apple's firmware framebuffer takes the card after the
# refusal and WindowServer brings the login window up on it. That display hangs under the declined card (its PCI device
# carries nvrm-layout), so it is not someone's desktop of another GPU: the Mac restarts, at the login window and after
# an automatic login alike, and says why in its log.
ONLY_CARD = ("previous driver restored. The only lit display is on the card whose driver was refused, where macOS's "
             "firmware framebuffer draws it without acceleration; the previous driver is back; restarting")
PLAIN = "previous driver restored; restarting"
LEFT = "A display is lit and a user is logged in, so the Mac is not restarted now"


def asked(r):
    """The subtree questions the job put to ioreg (the fake writes each one down), in order."""
    return [ln for ln in r.calls.splitlines() if ln.startswith("ioreg -r ")]


def after_restore(r):
    """Every subtree question came after the last file was put back: the display is asked about only once the restore
    is done, where it was asked before."""
    lines = r.calls.splitlines()
    last = max((i for i, ln in enumerate(lines) if ln.startswith("ditto ")), default=-1)
    first = min((i for i, ln in enumerate(lines) if ln.startswith("ioreg -r ")), default=len(lines))
    return last >= 0 and first > last


REF = dict(registry=FB_REFUSED + ioreg_line(EARLY))
r = run(job, **REF, fbs=APPLE_FB, displays=APPLE_DISPLAY, card=CARD_LIT, console="root")
arm("Apple's firmware framebuffer on the card shows the login window: the previous driver is put back and the Mac "
    "restarts", restored(r) and PLAIN in r.log and ONLY_CARD not in r.log, show(r))
r = run(job, **REF, fbs=APPLE_FB, displays="", card=CARD, console="root")
arm("  before WindowServer has opened it (no display yet): the same", restored(r) and PLAIN in r.log, show(r))
r = run(job, **REF, fbs=APPLE_FB, displays="", card=CARD, console="tester")
arm("  no display anywhere, a user logged in: the Mac restarts, with the usual line",
    restored(r) and PLAIN in r.log and ONLY_CARD not in r.log, show(r))
r = run(job, **REF, fbs=APPLE_FB, displays=APPLE_DISPLAY, card=CARD_LIT, console="tester")
arm("  a user logged in by itself on that desktop: the previous driver is put back and the Mac restarts",
    restored(r) and r.rc == 0, show(r))
arm("    and the log says why: the only display is on the refused card, drawn by the firmware framebuffer; restarting",
    ONLY_CARD in r.log and LEFT not in r.log and "another GPU" not in r.log, r.log)
arm("    and the display was asked about after the restore, the card's subtree after all the displays",
    after_restore(r) and asked(r) == ["ioreg -r -c IODisplayConnect -w0", "ioreg -r -k nvrm-layout -w0"], r.calls)
r = run(job, **REF, fbs=APPLE_FB, displays=APPLE_DISPLAY, card=CARD_LIT, console=None)
arm("    and so when the console's owner cannot be read (taken, as before, for someone who may be there)",
    restored(r) and ONLY_CARD in r.log, show(r))
r = run(job, **REF, fbs=APPLE_FB, displays=APPLE_DISPLAY + APPLE_DISPLAY, card=CARD_LIT + CARD_LIT, console="tester")
arm("    and so when ioreg prints the display and the card's subtree twice (ids, not counts, are compared)",
    restored(r) and ONLY_CARD in r.log, show(r))
CARD_BLIND = "card=\\$(ioreg -r -k nvrm-layout -w0 2>/dev/null | ids)"
old_rule = job.replace(CARD_BLIND.replace("\\$", "$"), "card=")
if old_rule == job:
    arm("control: the job with the old rule can be made", False, "the line that asks for the card's subtree is gone")
else:
    r = run(old_rule, **REF, fbs=APPLE_FB, displays=APPLE_DISPLAY, card=CARD_LIT, console="tester")
    arm("control: with every display counting (the old rule) that Mac is left on the slow desktop, not restarted",
        put_back(r) and not r.restarted and LEFT in r.log, show(r))
r = run(job, **REF, fbs=APPLE_FB, displays=APPLE_DISPLAY, card="", console="tester")
arm("  ioreg finds nothing under a declined card: every display counts, as before (put back, not restarted)",
    put_back(r) and not r.restarted and LEFT in r.log, show(r))
r = run(job, **REF, fbs=APPLE_FB, displays=APPLE_DISPLAY, card="ioreg: could not be reached\n", console="tester")
arm("  nor when it prints something that is not a display's line", put_back(r) and not r.restarted, show(r))
r = run(job, **REF, fbs=APPLE_FB, displays=APPLE_DISPLAY,
        card=CARD + APPLE_DISPLAY.replace("0x1000004f8", "0x1000004f80"), console="tester")
arm("  a display id under the card that only begins like the lit one does not take it away (whole ids compared)",
    put_back(r) and not r.restarted, show(r))
r = run(job, **REF, fbs=APPLE_FB, displays="ioreg: could not be reached\n", card=CARD_LIT, console="tester")
arm("  the displays' question answered with other text: no display, the Mac restarts as before",
    restored(r) and PLAIN in r.log, show(r))
r = run(job, **REF, fbs=APPLE_FB, displays=APPLE_DISPLAY.replace(", id 0x1000004f8", ""), card=CARD, console="tester")
arm("    and so when a display's line carries no id", restored(r), show(r))

# a display on another GPU still holds the restart back, whatever the declined card shows
r = run(job, **REF, fbs=OTHER_FB + APPLE_FB, displays=OTHER_DISPLAY + APPLE_DISPLAY, card=CARD_LIT, console="tester")
arm("a display on another GPU beside Apple's on the card, a user logged in: put back, not restarted, the usual words",
    put_back(r) and not r.restarted and LEFT in r.log and "starts at the next restart" in r.log
    and ONLY_CARD not in r.log, show(r))
r = run(job, **REF, fbs=OTHER_FB + APPLE_FB, displays=OTHER_DISPLAY + APPLE_DISPLAY, card=CARD_LIT + CARD_LIT,
        console="tester")
arm("  and so when ioreg prints the card's subtree twice (a count of displays would come out even and restart)",
    put_back(r) and not r.restarted and LEFT in r.log, show(r))
r = run(job, **REF, fbs=OTHER_FB + APPLE_FB, displays=OTHER_DISPLAY, card=CARD, console="tester")
arm("  another GPU's display and nothing lit on the card: the same", put_back(r) and not r.restarted, show(r))
r = run(job, **REF, fbs=OTHER_FB + APPLE_FB, displays=OTHER_DISPLAY + APPLE_DISPLAY, card=CARD_LIT, console="root")
arm("  at the login window of that Mac it restarts", restored(r) and PLAIN in r.log, show(r))

# two NVIDIA cards, both declined, each with Apple's framebuffer drawing: both subtrees are subtracted
r = run(job, **REF, fbs=APPLE_FB, displays=APPLE_DISPLAY + APPLE_DISPLAY2, card=CARD_LIT + CARD2_LIT,
        console="tester")
arm("two declined cards, each lit by Apple's framebuffer, a user logged in: put back and restarted",
    restored(r) and ONLY_CARD in r.log, show(r))
r = run(job, **REF, fbs=APPLE_FB, displays=APPLE_DISPLAY + APPLE_DISPLAY2, card=CARD_LIT, console="tester")
arm("  control: a lit display outside the declined subtrees (the second card not declined) holds the restart back",
    put_back(r) and not r.restarted and LEFT in r.log, show(r))
r = run(job, **REF, fbs=APPLE_FB, displays=APPLE_DISPLAY + APPLE_DISPLAY2, card=CARD_LIT + CARD2_LIT, console="root")
arm("  at the login window: restarted", restored(r) and PLAIN in r.log, show(r))

# the older rule never asks about displays
r = run(job, registry=FB_OK, nv={"nullmoth-nv-starts": "%02"}, fbs=APPLE_FB, displays=APPLE_DISPLAY, card=CARD_LIT,
        console="tester")
arm("two starts in a row without a display: restored and restarted without a question about displays, as before",
    restored(r) and asked(r) == [] and PLAIN in r.log, show(r))

# ---- what must not look like a refusal
r = run(job, registry=FB_REFUSED)
arm("the framebuffer's own refusal without NVRM's word (nvlayout=0 would read \"overridden\") restores nothing",
    untouched(r) and r.job_left and r.rc == 0, show(r))
arm("  after looking for four minutes, every five seconds", r.looks == 48 and r.sleeps == 47, show(r))

r = run(job, registry=FB_OK)
arm("a driver whose classes fit is left alone and the job stays for the next start", untouched(r) and r.job_left, show(r))

# ---- the two looks the job had before, unchanged
r = run(job, registry=FB_OK, nv={"nullmoth-nv-ok": "1"}, seen="BOOT-EARLIER")
arm("the display came up at an earlier start: the job takes itself away, as before",
    untouched(r) and r.job_gone and "keeping it" in r.log and r.sleeps == 0, show(r))

r = run(job, registry=FB_OK, nv={"nullmoth-nv-starts": "%02"})
arm("two starts in a row without a display: the previous driver is put back, as before",
    restored(r) and r.sleeps == 1 and "did not bring the display up twice; restoring the previous driver from" in r.log
    and "refused" not in r.log, show(r))

r = run(job, registry=FB_OK, nv={"nullmoth-nv-starts": "%01"},
        nv_later={2: 'echo %02 > "$T/nv.nullmoth-nv-starts"'})
arm("a second start that NVRM counts while the job is looking is not two failed starts (the count is read once, "
    "at the job's start, as before)", untouched(r) and r.job_left and r.looks == 48, show(r))

r = run(job, registry=FB_OK, nv_later={2: 'echo 1 > "$T/nv.nullmoth-nv-ok"'})
arm("a display that comes up while the job is looking ends the looking", untouched(r) and r.sleeps == 2, show(r))
arm("  and leaves the job in place: WindowServer can still keep dying on that display, and the next start needs it",
    r.job_left and "keeping it" not in r.log, show(r))

# ---- the order of the kexts and the job is not fixed
CLAIMED = '    | |   "nvrm-claimed" = Yes\n'
STAYS = "the job stays until the next one"
r = run(job, registry=FB_OK, nv={"nullmoth-nv-ok": "1"})
arm("the display came up in this start before the job's first look: the job stays and notes this boot",
    untouched(r) and r.job_left and r.seen == "BOOT-THIS" and STAYS in r.log and "keeping it" not in r.log
    and r.rc == 0, show(r))
r = run(job, registry=FB_OK, nv={"nullmoth-nv-ok": "1"}, boot=None)
arm("  control: a boot whose name cannot be read keeps the older rule (the job takes itself away)",
    untouched(r) and r.job_gone and "keeping it" in r.log and not r.seen, show(r))
r = run(job, registry=FB_OK, nv={"nullmoth-nv-ok": "1"}, seen_dir=True)
arm("  a note that cannot be written keeps the older rule too: the job never stays for good",
    untouched(r) and r.job_gone and "keeping it" in r.log, show(r))
r = run(job, registry=FB_OK, nv_later={2: 'echo 1 > "$T/nv.nullmoth-nv-ok"'}, seen_dir=True)
arm("  and a note that cannot be written at a later look keeps the job for the next start, as the older rule did",
    untouched(r) and r.job_left and "keeping it" not in r.log and r.sleeps == 2, show(r))
r = run(job, registry=FB_OK, nv={"nullmoth-nv-ok": "1"}, seen="BOOT-THIS")
arm("  found again in the same boot: still kept, nothing more in the log", untouched(r) and r.job_left
    and r.log == "", show(r))
r = run(job, registry=FB_OK, nv={"nullmoth-nv-ok": "1"}, seen="BOOT-EARLIER")
arm("  found at a later start: the job and its note are gone", untouched(r) and r.job_gone and not r.seen
    and "keeping it" in r.log, show(r))
r = run(job, registry=FB_OK, nv_later={2: 'echo 1 > "$T/nv.nullmoth-nv-ok"'})
arm("a display that comes up while the job is looking notes this boot as well", untouched(r) and r.job_left
    and r.seen == "BOOT-THIS" and r.sleeps == 2, show(r))
# the start after one in which the display lit before the first look and WindowServer then kept dying (6c wrote %02
# and deleted nullmoth-nv-ok): the driver is off, NVRM claims nothing, the previous driver comes back
r1 = run(job, registry=FB_OK, nv={"nullmoth-nv-ok": "1"}, boot="BOOT-LIT")
r = run(job, registry=FB_OK, nv={"nullmoth-nv-starts": "%02"}, seen=r1.seen or None, boot="BOOT-NEXT")
arm("after a WindowServer loop on a display lit before the job looked, the next start puts the previous driver back",
    r1.job_left and r1.seen == "BOOT-LIT" and restored(r) and not r.seen, show(r1) + show(r))

arm("  and the note goes with the job: an install and the uninstaller remove one left from an earlier trial",
    src.count('"$KC.nullmoth-restore" /Library/NullMoth/lastgood.seen\n') == 1
    and job.count("done_job() { rm -f /Library/LaunchDaemons/com.nullmoth.lastgood.plist /Library/NullMoth/lastgood.sh "
                  "/Library/NullMoth/lastgood.seen; }") == 1 and un.count(" /Library/NullMoth/lastgood.seen ") == 1)

r = run(job, registry=FB_OK + CLAIMED, nv={"nullmoth-nv-starts": "%02"})
arm("a count of two that includes this start (NVRM claimed the card) restores nothing and goes on looking",
    untouched(r) and r.job_left and r.looks >= 48, show(r))
r = run(job, registry=FB_OK, nv={"nullmoth-nv-starts": "%02"}, later={1: CLAIMED})
arm("  also when the claim is published during the job's first wait", untouched(r) and r.job_left, show(r))
r = run(job, registry=FB_OK + CLAIMED, nv={"nullmoth-nv-starts": "%02"},
        nv_later={3: 'echo 1 > "$T/nv.nullmoth-nv-ok"'})
arm("  and when that start then lights the display, the job stays for the next start", untouched(r) and r.job_left
    and r.seen == "BOOT-THIS", show(r))
r = run(job, registry=FB_OK + CLAIMED, nv={"nullmoth-nv-starts": "%03"})
arm("a count of three is two failed starts whatever is claimed: restored at once", restored(r) and r.sleeps == 0,
    show(r))
try:
    HOLD = ("    case \"$v\" in %02) sleep 5; if ioreg -l -w0 2>/dev/null | grep -q '\"nvrm-claimed\" = Yes'; then v=;"
            " fi;; esac\n")
    blind = job.replace(HOLD, "")
    if blind == job:
        raise AssertionError("the control's edit found nothing to change")
    r = run(blind, registry=FB_OK + CLAIMED, nv={"nullmoth-nv-starts": "%02"})
    arm("  control: without the look at the claim, that count of two restores during the start (the bug)",
        restored(r), show(r))
except (Unsafe, AssertionError) as e:
    arm("  control: the job without its look at the claim can be run", False, e)

# ---- differential against 2.9's job (v2.9 = 7412cdd), which had no look at the registry and ran once.
# Whenever NVRM has NOT declined the card, the new job must do at its first pass exactly what 2.9's did: restore or
# leave the same files, make the same NVRAM writes and deletes, write the same log, and restart (or not) the same.
# Only a refusal, which 2.9 cannot see, is allowed to differ. 2.9's job is read from git; when it cannot be read the
# arm says so and fails, as test_bootguard_ws_loop.py does for its tag.
job29 = None
try:
    src29 = subprocess.run(["git", "-C", str(ROOT), "show", "v2.9:package/install.sh"], capture_output=True, text=True)
    if src29.returncode != 0:
        raise Unsafe(f"v2.9:package/install.sh could not be read ({src29.stderr.strip()[-200:]})")
    job29 = fill(template(src29.stdout))
    arm("2.9's fallback job can be lifted from git for the differential", True)
except Unsafe as e:
    arm("2.9's fallback job can be lifted from git for the differential", False, e)

if job29 is not None:
    # every no-refusal case the two jobs can meet at the first pass: the display came up before, two failed starts
    # (with and without a backup), one failed start (below the count), and a quiet start with nothing set.
    for label, kw in (
        ("the display came up at an earlier start",
         dict(registry=FB_OK, nv={"nullmoth-nv-ok": "1"}, seen="BOOT-EARLIER")),
        ("two starts in a row without a display", dict(registry=FB_OK, nv={"nullmoth-nv-starts": "%02"})),
        ("two starts without a display and the backup gone",
         dict(registry=FB_OK, nv={"nullmoth-nv-starts": "%02"}, backup=False)),
        ("one start below the count", dict(registry=FB_OK, nv={"nullmoth-nv-starts": "%01"})),
        ("a quiet first start, nothing set and an empty registry", dict(registry="", nv={})),
        ("two starts without a display on another macOS build, a user logged in at another GPU's desktop",
         dict(registry=FB_OK, nv={"nullmoth-nv-starts": "%02"}, osbuild="25H100", fbs=OTHER_FB,
              displays=OTHER_DISPLAY, console="tester")),
    ):
        a, b = run(job, **kw), run(job29, **kw)
        ca, cb = canon(a), canon(b)
        arm(f"with no refusal the new job does at its first pass what 2.9's did: {label}", ca == cb,
            f"\n   new state-changing: {ca[0]}\n   2.9 state-changing: {cb[0]}"
            f"\n   new files/restart:  {ca[2]}\n   2.9 files/restart:  {cb[2]}"
            f"\n   new log: {ca[1]!r}\n   2.9 log: {cb[1]!r}")

# ---- controls
try:
    blind = job.replace(LOOK, "refused() { false; }")
    if blind == job:
        raise AssertionError("the control's edit found nothing to change")
    r = run(blind, registry=FB_REFUSED + ioreg_line(EARLY))
    arm("negative control: without its look at the registry the job leaves the refused driver in place (the bug)",
        untouched(r) and r.job_left, show(r))
except (Unsafe, AssertionError) as e:
    arm("negative control: the job without its look at the registry can be run", False, e)

for what, bad in (("its PATH line changed", job.replace(PATH_LINE, "PATH=/bin:/usr/bin:/usr/sbin:/sbin")),
                  ("it calls /sbin/shutdown", job.replace("shutdown -r now", "/sbin/shutdown -r now")),
                  ("it asks for the system's own PATH", job.replace("shutdown -r now", "command -p shutdown -r now")),
                  ("it names a path outside the installer's folders", job.replace('rm -rf /Library/NullMoth/kexts',
                                                                               'rm -rf /System/Library/Extensions')),
                  ("it writes to the console", job.replace('shutdown -r now', 'echo x > /dev/console; shutdown -r now'))):
    try:
        if bad == job:
            raise AssertionError("the control's edit found nothing to change")
        redirect(bad, pathlib.Path("/nonexistent/bin"), pathlib.Path("/nonexistent"))
        arm(f"control: a job that would reach this machine is not run ({what})", False, "it would have run")
    except Unsafe:
        arm(f"control: a job that would reach this machine is not run ({what})", True)
    except AssertionError as e:
        arm(f"control: a job that would reach this machine is not run ({what})", False, e)
# the guard in front of the real rm, cp and mkdir: a path outside the test's folder is refused, one inside is done
r = run(job, registry=FB_OK, nv={"nullmoth-nv-ok": "1"}, seen="BOOT-EARLIER", keep=True)
try:
    t = r.fake.parent
    env = {"T": str(t), "PATH": str(r.fake)}
    (t / "inside").mkdir()
    out = subprocess.run([str(r.fake / "rm"), "-rf", "/nonexistent-outside-the-test"], env=env, capture_output=True)
    arm("control: the real rm is not handed a path outside the test's folder", out.returncode == 97
        and "REFUSED outside the test's folder: rm" in (t / "calls").read_text(), out)
    out = subprocess.run([str(r.fake / "mkdir"), "-p", f"{t}/../outside"], env=env, capture_output=True)
    arm("  nor mkdir one that only starts there by name", out.returncode == 97
        and not (t.parent / "outside").exists(), out)
    out = subprocess.run([str(r.fake / "rm"), "-rf", str(t / "inside")], env=env, capture_output=True)
    arm("  and a path inside it is removed", out.returncode == 0 and not (t / "inside").exists(), out)
finally:
    shutil.rmtree(r.fake.parent, ignore_errors=True)

try:
    live = template(src).replace("\\$(date)", "$(date)", 1)
    if live == template(src):
        raise AssertionError("the control's edit found nothing to change")
    fill(live)
    arm("control: a template that runs a command while the job is written is not filled in", False, "it was")
except Unsafe:
    arm("control: a template that runs a command while the job is written is not filled in", True)
except AssertionError as e:
    arm("control: a template that runs a command while the job is written is not filled in", False, e)

# ---- the kernel collection's copy or rename fails (a full volume, an I/O error)
r = run(job, registry=FB_REFUSED + ioreg_line(EARLY), kc_fails="cp")
arm("refusal, the collection's copy fails: nothing changed, the job stays, no restart, no file left, logged",
    untouched(r) and r.job_left and r.kc_tmp == [] and r.rc == 0 and "could not be copied" in r.log, show(r) + str(r.kc_tmp))
arm("  control: the failure was injected into the collection's copy (at ee7f332 this start left a cut collection in"
    " place, the job gone and the Mac restarting)", "cp FAILED -p " in r.calls and ".kc.nullmoth-restore" in r.calls, show(r))
r = run(job, nv={"nullmoth-nv-starts": "%02"}, kc_fails="cp")
arm("twice, the collection's copy fails: nothing changed, the job stays, no restart",
    untouched(r) and r.job_left and r.kc_tmp == [] and "could not be copied" in r.log, show(r) + str(r.kc_tmp))
r = run(job, registry=FB_REFUSED + ioreg_line(EARLY), kc_fails="mv")
arm("refusal, the rename fails: the collection in place is the new one whole, the job stays, no restart",
    r.kc == "new" and r.job_left and not r.restarted and "could not be put in place" in r.log, show(r) + str(r.kc_tmp))
r = run(job, registry=FB_REFUSED + ioreg_line(EARLY))
arm("the ordinary refusal leaves no staging file", restored(r) and r.kc_tmp == [], show(r) + str(r.kc_tmp))
arm("  and one a job left behind (cut off in the middle) is removed by the next install and by the uninstaller",
    src.count('rm -f "$LG" "$LGP" "$KC.nullmoth-restore"') == 1 and un.count('"$KC.nullmoth-restore"') == 1)

print(f"{fails} failed")
sys.exit(1 if fails else 0)
