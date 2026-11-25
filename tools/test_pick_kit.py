#!/usr/bin/env python3
"""One package for every release: package/pick_kit.sh names the part of the package that installs on a macOS version,
and install.sh's legacy_install runs the matching kit for 10.14 to 14 from a private copy checked against SHA256SUMS.

pick_kit.sh is run with every release the driver knows and with the versions around them. legacy_install is lifted out
of install.sh and run in a scratch folder: mktemp's /var/tmp is moved into that folder, ditto and chown are stand-ins,
and the kit's install-legacy.sh is a stand-in that records its arguments. Controls: a kit file changed after the package
was made must stop it before the kit runs, and so must a package whose SHA256SUMS lists none of the kit. The order of
install.sh is checked too: the dispatch comes after the root check and before the macOS 15 and 26 gate."""
import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
PICK = ROOT / "package" / "pick_kit.sh"
INSTALL = ROOT / "package" / "install.sh"
RELEASES = ["10.14", "10.15", "11", "12", "13", "14"]
fails = 0


def arm(name, ok, detail=""):
    global fails
    fails += not ok
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f" ({str(detail).strip()[-500:]})"))


def pick(version, pkg):
    p = subprocess.run(["/bin/bash", str(PICK), version, str(pkg)], capture_output=True, text=True)
    return p.returncode, p.stdout.strip()


def make_kit(pkg, rel):
    k = pkg / "legacy" / rel
    (k / "kexts" / "NVRM.kext" / "Contents" / "MacOS").mkdir(parents=True)
    (k / "kexts" / "NVRM.kext" / "Contents" / "MacOS" / "NVRM").write_text("kext " + rel)
    (k / "RELEASE").write_text(rel + "\n")
    (k / "install-legacy.sh").write_text('#!/bin/bash\necho "KIT $(cat "$(dirname "$0")/RELEASE") ARGS:$*: DIR:$(dirname "$0")" >> "$NM_TEST_LOG"\nexit 7\n')
    return k


def sums(pkg):
    lines = []
    for f in sorted(p for p in pkg.rglob("*") if p.is_file() and p.name != "SHA256SUMS"):
        h = subprocess.run(["shasum", "-a", "256", str(f)], capture_output=True, text=True).stdout.split()[0]
        lines.append(f"{h}  ./{f.relative_to(pkg)}")
    (pkg / "SHA256SUMS").write_text("\n".join(lines) + "\n")


with tempfile.TemporaryDirectory() as td:
    T = pathlib.Path(td)
    full = T / "full"
    for r in RELEASES:
        make_kit(full, r)
    empty = T / "empty"; empty.mkdir()

    # 1. pick_kit.sh
    for v, want in [("15.7.2", "modern"), ("26.7.3", "modern"), ("26", "modern"), ("15.0", "modern")]:
        rc, out = pick(v, full)
        arm(f"pick {v} -> {want}", rc == 0 and out == want, out)
    for v, rel in [("10.14.6", "10.14"), ("10.14", "10.14"), ("10.15.7", "10.15"),
                   ("11.7.10", "11"), ("12.7.6", "12"), ("13.7.8", "13"), ("14.8.3", "14")]:
        rc, out = pick(v, full)
        arm(f"pick {v} -> legacy {rel}", rc == 0 and out == f"legacy {rel}", out)
        rc, out = pick(v, empty)
        arm(f"pick {v} without kits -> none", rc == 0 and out.startswith("none ") and "no kit for macOS " + rel in out, out)
    for v, word in [("10.13.6", "older"), ("10.12.6", "older"), ("10.7.5", "older"), ("10.6.8", "older"), ("10.5", "older"),
                    ("9.2", "older"), ("16.0", "newer"), ("27.1", "newer"),
                    ("", "cannot read"), ("10", "cannot read"), ("x.y", "cannot read"), ("10.13;rm", "cannot read")]:
        rc, out = pick(v, full)
        arm(f"pick '{v}' -> none ({word})", rc == 0 and out.startswith("none ") and word in out, out)
    wrong = T / "wrong"; make_kit(wrong, "10.14"); (wrong / "legacy" / "10.14" / "RELEASE").write_text("10.13\n")
    rc, out = pick("10.14.6", wrong)
    arm("pick refuses a kit whose RELEASE names another release", out.startswith("none ") and "not the one" in out, out)

    # 2. install.sh's order
    src = INSTALL.read_text()
    i_root = src.index('[ "$(id -u)" -eq 0 ] || die "run with sudo"')
    i_disp = src.index('case "${v%%.*}" in 15|26) ;; *) legacy_install "$v" ;; esac')
    i_gate = src.index('case $MAJ in 15|26) ;;')
    arm("install.sh: root check, then the dispatch, then the 15/26 gate", i_root < i_disp < i_gate)

    # 3. legacy_install, lifted out and run
    a = src.index("legacy_install() {"); b = src.index("\n}\n", a) + 3
    fn = src[a:b]
    arm("legacy_install names /var/tmp once (the test moves it)", fn.count("/var/tmp/") == 1, fn.count("/var/tmp/"))
    vt = T / "vartmp"; vt.mkdir()
    fn = fn.replace("/var/tmp/", str(vt) + "/")
    stub = T / "bin"; stub.mkdir()
    (stub / "ditto").write_text('#!/bin/bash\nexec cp -R "$1" "$2"\n')
    (stub / "chown").write_text('#!/bin/bash\nexit 0\n')
    for s in ("ditto", "chown"):
        os.chmod(stub / s, 0o755)
    harness = ('set -u\nSTAGE=""\ndie() { echo "DIE $*"; exit 1; }\nstep() { echo "STEP $*"; }\nok() { echo "OK $*"; }\n'
               + fn + '\nlegacy_install "$1"\n')
    (T / "h.sh").write_text(harness)

    def run(pkg, version, check=None):
        log = T / "log"; log.write_text("")
        env = dict(os.environ, PATH=f"{stub}:/usr/bin:/bin", HERE=str(pkg), NM_TEST_LOG=str(log))
        env.pop("CHECK", None)
        if check:
            env["CHECK"] = check
        p = subprocess.run(["/bin/bash", "-c", f'HERE="$HERE"; source "{T}/h.sh" "$0"', version],
                                     env=env, capture_output=True, text=True)
        return p.returncode, p.stdout + p.stderr, log.read_text()

    pkg = T / "pkg"
    for r in RELEASES:
        make_kit(pkg, r)
    (pkg / "pick_kit.sh").write_text(PICK.read_text())
    sums(pkg)
    rc, out, log = run(pkg, "10.14.6")
    arm("10.14.6: the 10.14 kit runs, from the private copy, with no arguments",
        rc == 7 and log.startswith("KIT 10.14 ARGS:: DIR:" + str(vt) + "/nullmoth-legacy."), out + log)
    rc, out, log = run(pkg, "14.8.3", check="1")
    arm("14.8.3 with CHECK=1: the 14 kit runs with --check", rc == 7 and log.startswith("KIT 14 ARGS:--check: DIR:" + str(vt)), out + log)
    for v in ("10.13.6", "10.7.5"):
        rc, out, log = run(pkg, v)
        arm(f"{v}: stopped as unsupported, no kit runs", rc == 1 and "DIE" in out and "older" in out and "10.14" in out and log == "", out + log)

    # controls
    (pkg / "legacy" / "10.14" / "kexts" / "NVRM.kext" / "Contents" / "MacOS" / "NVRM").write_text("changed")
    rc, out, log = run(pkg, "10.14.6")
    arm("control: a kit file changed after packaging stops it before the kit runs", rc == 1 and "does not match SHA256SUMS" in out
        and log == "", out + log)
    sums(pkg)
    lines = [l for l in (pkg / "SHA256SUMS").read_text().splitlines() if "/legacy/12/" not in l]
    (pkg / "SHA256SUMS").write_text("\n".join(lines) + "\n")
    rc, out, log = run(pkg, "12.7.6")
    arm("control: SHA256SUMS that lists none of the kit stops it", rc == 1 and "lists no file of the kit" in out and log == "", out + log)
    sums(pkg)
    (pkg / "legacy" / "13" / "extra.dylib").write_text("not listed")
    rc, out, log = run(pkg, "13.7.8")
    arm("control: a kit file SHA256SUMS does not list stops it", rc == 1 and "does not list" in out and log == "", out + log)
    (pkg / "legacy" / "13" / "extra.dylib").unlink(); sums(pkg)
    bad = (pkg / "SHA256SUMS").read_text().replace("  ./legacy/11/RELEASE", " ./legacy/11/RELEASE")
    (pkg / "SHA256SUMS").write_text(bad)
    rc, out, log = run(pkg, "11.7.10")
    arm("control: a malformed line of the kit stops it (what --strict does elsewhere)", rc == 1 and ("malformed" in out or "does not list" in out) and log == "", out + log)
    nopick = T / "nopick"; make_kit(nopick, "11"); sums(nopick)
    rc, out, log = run(nopick, "11.7.10")
    arm("a package without pick_kit.sh keeps the old refusal", rc == 1 and "built for macOS 15 and 26" in out and log == "", out + log)

print("result:", "PASS" if not fails else f"FAIL ({fails})")
sys.exit(1 if fails else 0)
