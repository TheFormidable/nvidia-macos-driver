#!/usr/bin/env python3
"""A release plugin never writes through a link.

Its default log is /tmp/nvmtl.log, a fixed name in a folder every local user can write. fopen() follows a link, so a
link planted there by one user sent the log lines of another user's process (one that had opted into logging) into any
file that user could write. The release wrapper nvmtl_rel_fopen is lifted from plugin/NVMTLDevice.m and run; the
control is plain fopen, which writes through the link."""
import pathlib, re, shutil, subprocess, sys, tempfile

R = pathlib.Path(__file__).resolve().parents[1]
src = (R / "plugin/NVMTLDevice.m").read_text()
a = src.index("static inline int nvmtl_rel_devpath(const char *p) {")
b = src.index("#define access(p, m) nvmtl_rel_access(p, m)")
lifted = src[a:b]
fails = 0


def arm(name, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + name + (f" ({str(detail).strip()[-200:]})" if detail and not ok else ""))
    fails += not ok


HARNESS = '#include <fcntl.h>\n#include <stdio.h>\n#include <string.h>\n#include <unistd.h>\n' + lifted + r'''
int main(int argc, char **argv) {
    FILE *f = !strcmp(argv[1], "plain") ? fopen(argv[2], argv[3]) : nvmtl_rel_fopen(argv[2], argv[3]);
    if (!f) { puts("refused"); return 0; }
    if (argv[3][0] != 'r') fputs("line\n", f);
    fclose(f); puts("opened"); return 0;
}
'''
cc = shutil.which("cc") or shutil.which("clang")
if not cc:
    print("SKIP: needs a C compiler"); sys.exit(2)
with tempfile.TemporaryDirectory() as d:
    d = pathlib.Path(d); (d / "h.c").write_text(HARNESS)
    subprocess.run([cc, "-Wall", "-Werror", "-Wno-unused-function", "-o", str(d / "h"), str(d / "h.c")], check=True)
    run = lambda *x: subprocess.run([str(d / "h"), *map(str, x)], capture_output=True, text=True).stdout.strip()  # noqa: E731
    victim = d / "victim"; victim.write_text("keep\n")
    link = d / "nvmtl.log"; link.symlink_to(victim)
    arm("an append through a planted link is refused and the target is untouched", run("rel", link, "a") == "refused" and victim.read_text() == "keep\n")
    arm("a truncating write through a planted link is refused too", run("rel", link, "w") == "refused" and victim.read_text() == "keep\n")
    arm("control: plain fopen writes through the link", run("plain", link, "a") == "opened" and victim.read_text() == "keep\nline\n")
    log = d / "real.log"
    arm("an ordinary log file is created and appended to", run("rel", log, "a") == "opened" and run("rel", log, "a") == "opened" and log.read_text() == "line\nline\n")
    arm("reading is unchanged (a link may be read)", run("rel", link, "r") == "opened")
    arm("a developer request path stays refused in a release build", run("rel", "/tmp/nvmtl-dump-request", "a") == "refused")
dl = (R / "plugin/nvmtl_dlss.c").read_text()
arm("the DLSS trace file is opened without following a link", "O_NOFOLLOW" in dl and 'g_trf = fopen(path, "a")' not in dl)
nd = (R / "nvk/nvk-diag-nofollow.patch").read_text()
bld = (R / "build/build_nvk.sh").read_text()


def chain(script, release=None):
    """The patch names build_nvk.sh applies: the words of the CHAIN="..." line in one release's arm of its
    `case $REL in` block; without a release, the one the script builds when none is asked for (2.9 today).
    A name in a comment, in another release's arm or inside a longer name is not one."""
    if release is None:
        default = re.search(r"\bREL=\$\{4:-([\w.]+)\}", script)
        release = default.group(1) if default else None
    arm_text = re.search(r"^[ \t]*" + re.escape(release) + r"\)(.*?);;", script, re.S | re.M) if release else None
    line = re.search(r'^[ \t]*CHAIN="([^"\n]*)"', arm_text.group(1), re.M) if arm_text else None
    return line.group(1).split() if line else []


# The patch has to be one the default build applies. It was the last name of the chain when this arm was written, and the
# arm asked for exactly that text; patches are appended after it now, so the arm asks for the name as a word of the line.
arm("NVK's diagnostic log (NVK_DIAG=1) is created without following a link, for its owner alone",
    "+         int fd = open(path, O_WRONLY | O_CREAT | O_APPEND | O_NOFOLLOW, 0600);" in nd
    and "-         int fd = open(path, O_WRONLY | O_CREAT | O_APPEND, 0644);" in nd and "nvk-diag-nofollow" in chain(bld))
CASE = ('REL=${4:-2.9}\ncase $REL in\n  2.9) FIXES=x\n       CHAIN="nvk-macos @FIXES %s" ;;\n'
        '  2.8) FIXES=y\n       CHAIN="nvk-macos @FIXES %s" ;;\nesac\n')
arm("control: the name counts with patches appended after it",
    "nvk-diag-nofollow" in chain(CASE % ("nvk-warnings nvk-diag-nofollow nvk-later nvk-last", "nvk-slab")))
arm("control: a comment, the chain of a release that is not the default, or a longer name does not count",
    "nvk-diag-nofollow" not in chain("#   2.9  nvk-diag-nofollow (the log is created without following a link)\n"
                                     + CASE % ("nvk-warnings", "nvk-diag-nofollow"))
    and "nvk-diag-nofollow" in chain(CASE % ("nvk-warnings", "nvk-diag-nofollow"), "2.8")
    and "nvk-diag-nofollow" not in chain(CASE % ("nvk-diag-nofollow-old xnvk-diag-nofollow", "nvk-slab"))
    and "nvk-diag-nofollow" not in chain('CHAIN="nvk-diag-nofollow"\n'))
print("FAILED" if fails else "PASS"); sys.exit(1 if fails else 0)
