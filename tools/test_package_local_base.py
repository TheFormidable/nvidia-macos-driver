#!/usr/bin/env python3
"""build/package_local.sh takes a base package somebody downloaded or was given, and writes into a copy of it by fixed
names (cp, chmod, printf >, mkdir -p, rm -rf). `cp -pR` copies the base's links as links, and the base check reads only
the files its SHA256SUMS lists, so a link the list does not name (SHA256SUMS.new -> ~/.zshrc) carried those writes out
of the package folder: the first refresh of the sums list overwrote the file the link named. A release package holds
folders, plain files and links to a file beside them; the script refuses anything else before its first write, and a
base that is another release than the one this tree is staged onto.

And the tarball it writes is passed on: its headers must not name the account that built it.

Everything happens in a temporary folder: the "outside" files the hostile links name are in it too.
usage: test_package_local_base.py"""
import hashlib, os, re, shutil, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "build" / "package_local.sh"
fails = 0


def check(what, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + what + ("" if ok else f"\n     {detail}"))
    fails += 0 if ok else 1


def base_tree(d, version="2.9\n"):
    """the files the base check asks for, listed in SHA256SUMS as a release lists them"""
    files = {"install.sh": "#!/bin/bash\n", "nullmoth-runtime-check.sh": "#!/bin/bash\n", "VERSION": version,
             "Library/GPUBundles/nvmtl-allow.txt": "x\n", "Library/GPUBundles/NVMTLDriver.bundle/Contents/Info.plist": "<plist/>\n",
             "Library/GPUBundles/nvmtl/libvulkan.1.4.362.dylib": "lib\n"}
    if version is None:
        del files["VERSION"]
    for rel, text in files.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text)
    (d / "SHA256SUMS").write_text("".join(f"{hashlib.sha256((d / r).read_bytes()).hexdigest()}  ./{r}\n" for r in files))
    # the two links of release 2.9: a name beside them
    os.symlink("libvulkan.1.4.362.dylib", d / "Library/GPUBundles/nvmtl/libvulkan.1.dylib")
    os.symlink("libvulkan.1.dylib", d / "Library/GPUBundles/nvmtl/libvulkan.dylib")


def run(script, base, t):
    out = t / "out"; out.mkdir(exist_ok=True)   # empty: the script stops at the first part this checkout did not build
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(t / "home"), "TMPDIR": str(t), "LANG": "C"}
    r = subprocess.run(["bash", str(script), str(base), str(out), str(t / "pkg")], capture_output=True, text=True, env=env, timeout=120)
    return r.returncode, r.stdout + r.stderr


def escapes(script=SCRIPT):
    """[(case, what happened)] for every hostile link that carried a write out of the package folder"""
    p = []
    for name, target in (("SHA256SUMS.new", "abs"), ("SHA256SUMS.new", "rel"), ("uninstall.sh", "abs"), ("uninstall.sh", "rel")):
        t = Path(tempfile.mkdtemp(prefix="pkgbase-"))
        try:
            (t / "home").mkdir()
            victim = t / "home" / ".zshrc"; victim.write_text("the user's own file\n")
            base = t / "dl" / "pkgroot"; base.mkdir(parents=True); base_tree(base)
            # rel: from the copy the script writes into (<pkg>/pkgroot) up to the temporary folder
            os.symlink(str(victim) if target == "abs" else "../../home/.zshrc", base / name)
            rc, out = run(script, base, t)
            if victim.read_text() != "the user's own file\n":
                p.append(f"{name} -> {target} link: the file outside was overwritten (exit {rc})")
            if "replaced install.sh" in out:
                p.append(f"{name} -> {target} link: the script went on writing into the copy")
        finally:
            shutil.rmtree(t, ignore_errors=True)
    return p


def control_base_passes(script=SCRIPT):
    t = Path(tempfile.mkdtemp(prefix="pkgbase-"))
    try:
        (t / "home").mkdir(); base = t / "dl" / "pkgroot"; base.mkdir(parents=True); base_tree(base)
        rc, out = run(script, base, t)
        return "replaced install.sh" in out, out[-400:]
    finally:
        shutil.rmtree(t, ignore_errors=True)


def odd_base(make, script=SCRIPT, version="2.9\n"):
    """Runs the script on a base that <make>(base folder) changed; returns (it went on to its first write, its output)."""
    t = Path(tempfile.mkdtemp(prefix="pkgbase-"))
    try:
        (t / "home").mkdir(); base = t / "dl" / "pkgroot"; base.mkdir(parents=True); base_tree(base, version)
        make(base)
        rc, out = run(script, base, t)
        return "replaced install.sh" in out or (t / "pkg" / "pkgroot").exists(), out[-400:]
    finally:
        shutil.rmtree(t, ignore_errors=True)


def without(pattern, flags=0):
    """A copy of the script (in a tree of its own, beside a copy of package/) with the text <pattern> matches taken out."""
    t = Path(tempfile.mkdtemp(prefix="pkgbase-ctl-"))
    src = SCRIPT.read_text()
    m = re.search(pattern, src, flags)
    if not m:
        shutil.rmtree(t, ignore_errors=True)
        return None, None
    (t / "build").mkdir(); bad = t / "build" / "package_local.sh"
    bad.write_text(src.replace(m.group(0), "\n"))
    shutil.copytree(ROOT / "package", t / "package")
    return bad, t


ok, out = control_base_passes()
check("a base like release 2.9 (folders, files, two links to a name beside them) passes the base check", ok, out)

# another release, or no release at all: its files without source here would ship under this build's version
for what, version, word in (("that is release 2.8", "2.8\n", "is release 2.8, not 2.9"), ("that is a later release", "3.0\n", "is release 3.0, not 2.9"),
                            ("without a VERSION", None, "is release unknown")):
    went_on, out = odd_base(lambda b: None, version=version)
    check(f"a base {what} is refused before the first write, with the release it must be", not went_on and word in out, out)
bad, t = without(r"\n\[ \"\$bv\" = \"\$BASE_RELEASE\" \] \|\| stop [^\n]*\n")
check("the script has its release test", bad is not None)
if bad:
    try:
        check("negative control: without the release test a 2.8 base is staged", odd_base(lambda b: None, bad, "2.8\n")[0])
    finally:
        shutil.rmtree(t, ignore_errors=True)

# a pipe and a set-id file: nothing a release package holds
def fifo(b):
    os.mkfifo(b / "Library" / "pipe")
def setuid(b):
    (b / "Library" / "tool").write_text("x\n"); os.chmod(b / "Library" / "tool", 0o4755)
for what, make, word in (("a pipe", fifo, "holds a device, pipe or socket: Library/pipe"),
                         ("a set-user-id file", setuid, "holds a set-user-id or set-group-id file: Library/tool")):
    went_on, out = odd_base(make)
    check(f"a base that holds {what} is refused before the first write", not went_on and word in out, out)
bad, t = without(r"\nodd=\$\(find \"\$B\" ! -type f.*?not a release package\)\"\n(?=for f in install\.sh)", re.S)
check("the script has its tests for odd files", bad is not None)
if bad:
    try:
        check("negative control: without those tests a base with a set-user-id file is staged", odd_base(setuid, bad)[0])
    finally:
        shutil.rmtree(t, ignore_errors=True)

p = escapes()
check("a base whose link leads out of its folder is refused before the first write; nothing outside changes", not p, p)
# negative control: the same script without its link test lets the writes out
t = Path(tempfile.mkdtemp(prefix="pkgbase-ctl-"))
try:
    src = SCRIPT.read_text()
    m = re.search(r"\nwhile IFS= read -r l; do\n.*?\ndone < <\(find \"\$B\" -type l\)\n", src, re.S)
    check("the script has its link test (a loop over the base's links)", m is not None)
    if m:
        (t / "build").mkdir(); bad = t / "build" / "package_local.sh"
        bad.write_text(src.replace(m.group(0), "\n"))
        shutil.copytree(ROOT / "package", t / "package")
        check("negative control: without the link test a hostile base overwrites a file outside", len(escapes(bad)) > 0)
finally:
    shutil.rmtree(t, ignore_errors=True)

# the tarball's headers: root/wheel, no extended attributes, whoever builds it
line = next((l for l in SCRIPT.read_text().splitlines(True) if "-czf \"$T\" pkgroot" in l), "")
start = SCRIPT.read_text().find("tar --uid 0")
opts = SCRIPT.read_text()[start:SCRIPT.read_text().find("-czf \"$T\" pkgroot")] if start >= 0 else ""
check("the package tarball is written with --uid 0 --gid 0 --uname root --gname wheel and without extended attributes",
      all(o in opts for o in ("--uid 0", "--gid 0", "--uname root", "--gname wheel", "--no-xattrs", "--no-mac-metadata")), opts or line)
ver = subprocess.run(["tar", "--version"], capture_output=True, text=True).stdout
if "bsdtar" in ver:   # the Mac's tar: the same options on a folder of this test's own
    t = Path(tempfile.mkdtemp(prefix="pkgtar-"))
    try:
        (t / "pkgroot").mkdir(); (t / "pkgroot" / "f").write_text("x\n")
        args = opts.replace("\\\n", " ").split()
        r = subprocess.run(["tar"] + args[1:] + ["-czf", str(t / "p.tgz"), "pkgroot"], cwd=t, capture_output=True, text=True,
                           env=dict(os.environ, COPYFILE_DISABLE="1"))
        lst = subprocess.run(["tar", "tvzf", str(t / "p.tgz")], capture_output=True, text=True).stdout
        owners = {l.split()[2] + "/" + l.split()[3] if len(l.split()) > 3 else "" for l in lst.splitlines()}
        check("bsdtar with those options names root/wheel in every header", r.returncode == 0 and owners == {"root/wheel"}, (r.stderr, lst))
    finally:
        shutil.rmtree(t, ignore_errors=True)
else:
    print("skip the run of the tarball options: this tar is not bsdtar")
print(f"{'all passed' if not fails else str(fails) + ' failed'}")
sys.exit(1 if fails else 0)
