#!/usr/bin/env python3
"""The fallback job puts the previous driver back when the new one's framebuffer refuses this macOS's classes.

NVRMFB compares the sizes of Apple's framebuffer classes on the running macOS with the ones it was built for. When they
differ NVRM declines the card before the kernel's boot guard counts the start, and every framebuffer of the driver
refuses for the same reason, the one that shows the desktop with the driver off included. The install excludes Apple's
own firmware framebuffer, so no desktop comes up at that start or at any later one, and the guard, which only counts
starts that began, never turns anything off: the job that restores the previous driver only looked at the guard's count
and never ran its restore. NVRM writes the refusal on the PCI device (nvrm-layout = "declined ..."), and the job now
reads it there, every five seconds for four minutes, because the kexts can start after the job does.

The job is lifted out of install.sh, filled in the way the installer fills it, and run against fake ioreg, nvram, ditto,
sleep and shutdown with every path of it moved into a folder of the test's. Nothing of it runs unless every path it
names lies in that folder (the job removes kexts and restarts the Mac). The negative control is the same job without its
look at the registry, which must leave the refused driver in place."""
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


def fill(tmpl):
    """The job as the installer writes it to disk. Only the installer's own variables may be filled in here: a command
    substitution that is not escaped would run now, in the test, with whatever it names."""
    if re.search(r"(?<!\\)\$\(|`", tmpl):
        raise Unsafe("the job's template runs a command while it is written")
    names = set(re.findall(r"(?<!\\)\$\{?([A-Za-z_][A-Za-z0-9_]*)", tmpl))
    if not names <= set(INSTALLER):
        raise Unsafe(f"the job's template fills in {sorted(names - set(INSTALLER))}, which this test does not know")
    script = "".join(f"{k}='{v}'\n" for k, v in INSTALLER.items()) + "cat <<EOF\n" + tmpl + "EOF\n"
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
            raise Unsafe(f"line {n} of the job names {m.group(0)!r}, outside the test's folder")
    return text


# the real tools the job and the fakes may use; they only ever get paths under the test's folder
REAL = ("rm", "cp", "mkdir", "cut", "grep", "date", "cat")

FAKES = {
    # what `ioreg -l -w0` prints: the file the test wrote (and may write again between two looks)
    "ioreg": r'''#!/bin/bash
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
    "shutdown": r'''#!/bin/bash
echo "shutdown $*" >> "$T/calls"''',
}

def ioreg_line(text):
    return f'    | |   "nvrm-layout" = "{text}"\n'


FB_REFUSED = ('    | |   "nvrmfb-layout" = "refused: IOFramebuffer is 0x1e0 bytes in this macOS, 0x1d0 in the driver '
              '(1 size, 0 vtables differ of 3, 0); macOS 25G342 (Darwin 25.7.0)"\n')
FB_OK = '    | |   "nvrmfb-layout" = "ok: 3 classes, vtables not compared; macOS 25G342 (Darwin 25.7.0)"\n'
NAMES = ("NVRM", "NVAccel", "NVRMFB", "NVRMAGDC")


class Result:
    pass


def run(job, *, registry="", nv=None, later=None, nv_later=None, backup=True):
    """One start of the Mac with the job installed. registry is what ioreg prints at the first look and nv the NVRAM
    variables at the job's start; later[n] is added to the registry during the n-th wait, and nv_later[n] is a shell
    line that changes the fake NVRAM during it."""
    t = pathlib.Path(tempfile.mkdtemp(prefix="lgr-"))
    r = Result()
    try:
        fake = t / "bin"
        fake.mkdir()
        for name, body in FAKES.items():
            p = fake / name
            p.write_text(body + "\n")
            p.chmod(p.stat().st_mode | stat.S_IXUSR)
        for name in REAL:
            real = shutil.which(name, path="/usr/bin:/bin")
            if not real:
                raise Unsafe(f"no {name} in /usr/bin or /bin")
            (fake / name).symlink_to(real)
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
        for name, value in (nv or {}).items():
            (t / f"nv.{name}").write_text(value + "\n")
        for n, more in (later or {}).items():
            (t / f"registry.{n}").write_text(more)
        for n, line in (nv_later or {}).items():
            (t / f"nvram.{n}").write_text(line + "\n")
        p = subprocess.run(["/bin/bash", str(script)], env={"T": str(t), "PATH": "/usr/bin:/bin"}, capture_output=True,
                           text=True, timeout=300)

        def read(path):
            return path.read_text() if path.exists() else ""
        r.rc, r.err = p.returncode, p.stderr
        r.log = read(lib / "NullMoth/lastgood.log")
        r.calls = read(t / "calls")
        r.sleeps = int(read(t / "sleep.n") or 0)
        r.looks = int(read(t / "ioreg.n") or 0)
        r.kexts = [read(lib / "Extensions" / f"{k}.kext" / "which").strip() for k in NAMES]
        r.bundle = read(lib / "GPUBundles/NVMTLDriver.bundle/which").strip()
        r.kc = read(lib / "KernelCollections/AuxiliaryKernelExtensions.kc").strip()
        r.fw = read(lib / "NullMoth/nvfw/which").strip()
        r.version = read(lib / "NullMoth/driver-version").strip()
        r.marker = read(lib / "NullMoth/driver-restored").strip()
        r.job_left = plist.exists() and script.exists()
        r.job_gone = not plist.exists() and not script.exists()
        r.restarted = "shutdown -r now" in r.calls
    finally:
        shutil.rmtree(t, ignore_errors=True)
    return r


def restored(r):
    return (r.kexts == ["previous"] * 4 and r.bundle == "previous" and r.kc == "previous" and r.fw == "previous"
            and r.version == "2.9" and r.marker == "previous" and r.job_gone and r.restarted)


def untouched(r):
    return (r.kexts == ["new"] * 4 and r.bundle == "new" and r.kc == "new" and r.fw == "new" and r.version == "3.0"
            and not r.marker and not r.restarted)


def show(r):
    return f"rc {r.rc} looks {r.looks} sleeps {r.sleeps} kexts {r.kexts} kc {r.kc} job_left {r.job_left}\n{r.log}\n{r.calls}\n{r.err}"


src = (ROOT / "package/install.sh").read_text()
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

# ---- what must not look like a refusal
r = run(job, registry=FB_REFUSED)
arm("the framebuffer's own refusal without NVRM's word (nvlayout=0 would read \"overridden\") restores nothing",
    untouched(r) and r.job_left and r.rc == 0, show(r))
arm("  after looking for four minutes, every five seconds", r.looks == 48 and r.sleeps == 47, show(r))

r = run(job, registry=FB_OK)
arm("a driver whose classes fit is left alone and the job stays for the next start", untouched(r) and r.job_left, show(r))

# ---- the two looks the job had before, unchanged
r = run(job, registry=FB_OK, nv={"nullmoth-nv-ok": "1"})
arm("the display came up at an earlier start: the job takes itself away, as before",
    untouched(r) and r.job_gone and "keeping it" in r.log and r.sleeps == 0, show(r))

r = run(job, registry=FB_OK, nv={"nullmoth-nv-starts": "%02"})
arm("two starts in a row without a display: the previous driver is put back, as before",
    restored(r) and r.sleeps == 0 and "did not bring the display up twice; restoring the previous driver from" in r.log
    and "refused" not in r.log, show(r))

r = run(job, registry=FB_OK, nv={"nullmoth-nv-starts": "%01"},
        nv_later={2: 'echo %02 > "$T/nv.nullmoth-nv-starts"'})
arm("a second start that NVRM counts while the job is looking is not two failed starts (the count is read once, "
    "at the job's start, as before)", untouched(r) and r.job_left and r.looks == 48, show(r))

r = run(job, registry=FB_OK, nv_later={2: 'echo 1 > "$T/nv.nullmoth-nv-ok"'})
arm("a display that comes up while the job is looking ends the looking", untouched(r) and r.sleeps == 2, show(r))
arm("  and leaves the job in place: WindowServer can still keep dying on that display, and the next start needs it",
    r.job_left and "keeping it" not in r.log, show(r))

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
                  ("it names a path outside the installer's folders", job.replace('rm -rf /Library/NullMoth/kexts',
                                                                               'rm -rf /System/Library/Extensions'))):
    try:
        if bad == job:
            raise AssertionError("the control's edit found nothing to change")
        redirect(bad, pathlib.Path("/nonexistent/bin"), pathlib.Path("/nonexistent"))
        arm(f"control: a job that would reach this machine is not run ({what})", False, "it would have run")
    except Unsafe:
        arm(f"control: a job that would reach this machine is not run ({what})", True)
    except AssertionError as e:
        arm(f"control: a job that would reach this machine is not run ({what})", False, e)
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

print(f"{fails} failed")
sys.exit(1 if fails else 0)
