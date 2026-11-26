#!/usr/bin/env python3
"""Import final, independently signed graphics payloads into a new NullMoth package.

This tool never generates a trust anchor, handles a private key, installs a driver,
modifies EFI or changes host trust policy. Run without sudo on a build Mac.
"""
import argparse
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile

GPU = Path("Library/GPUBundles")
UNITS = ("NVMTLDriver.bundle", "NVKZinkGLDriver.bundle", "nvmtl")
MAGICS = {bytes.fromhex(x) for x in
          ("cffaedfe", "cefaedfe", "cafebabe", "bebafeca", "feedfacf",
           "cffaedfe", "cafebabf", "bfbafeca")}
SEAL = "_CodeSignature"

def digest(path):
    with path.open("rb") as f:
        h = hashlib.sha256()
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
        return h.hexdigest()

def inventory(root):
    """Reject special files, external links and dangerous path names."""
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            p = Path(directory) / name
            rel = p.relative_to(root).as_posix()
            if any(c in rel for c in ("\n", "\r", "\\")):
                raise ValueError("unsupported path: " + repr(rel))
            mode = p.lstat().st_mode
            if mode & (stat.S_ISUID | stat.S_ISGID):
                raise ValueError("set-id entry: " + rel)
            if p.is_symlink():
                target = os.readlink(p)
                if os.path.isabs(target) or not p.resolve(strict=True).is_relative_to(root):
                    raise ValueError("external symlink: " + rel)
                if not p.resolve().is_file():
                    raise ValueError("directory symlink: " + rel)
                result[rel] = ("link", target)
            elif stat.S_ISREG(mode):
                result[rel] = ("file", digest(p))
            elif not stat.S_ISDIR(mode):
                raise ValueError("special file: " + rel)
    return result

def manifest(base, inv):
    rows = {}
    for line in (base / "SHA256SUMS").read_text().splitlines():
        m = re.fullmatch(r"([a-f0-9]{64}) [ *](?:\./)?(.+)", line)
        if not m or m[2] in rows:
            raise ValueError("invalid/duplicate SHA256SUMS line")
        rows[m[2]] = m[1]
    actual = {p: v[1] for p, v in inv.items()
              if v[0] == "file" and p != "SHA256SUMS"}
    if rows != actual:
        raise ValueError("base package manifest is not exact")
    return rows

def is_macho(path):
    with path.open("rb") as f:
        return f.read(4) in MAGICS

def run(*args):
    return subprocess.run([str(a) for a in args], check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=120).stdout.decode("utf-8", "replace")

def uuids(path):
    s = run("/usr/bin/otool", "-l", path)
    return sorted(re.findall(r"(?m)^\s*uuid\s+([0-9A-Fa-f-]+)\s*$", s))

def verify_unit(base, signed, signer, leaf):
    before, after = inventory(base), inventory(signed)
    def payload(x):
        return {k for k in x if SEAL not in Path(k).parts}
    if payload(before) != payload(after):
        raise ValueError("payload path set changed: " + base.name)
    # Only seal files and signed Mach-O bytes may differ. Resource data,
    # symlink targets and build UUIDs remain those of the original package.
    for rel in sorted(payload(before)):
        old, new = before[rel], after[rel]
        if old[0] != new[0]:
            raise ValueError("entry type changed: " + rel)
        if old[0] == "link":
            if old != new:
                raise ValueError("link changed: " + rel)
            continue
        a, b = base / rel, signed / rel
        if not is_macho(a):
            if old != new:
                raise ValueError("non-code resource changed: " + rel)
            continue
        if not is_macho(b) or not uuids(a) or uuids(a) != uuids(b):
            raise ValueError("Mach-O UUID mismatch: " + rel)
        # All Mach-O code in these units must be independently verified.
        # The expected leaf identity is an explicit local input, never WEX's.
        run(signer, "verify", b)
        info = run(signer, "print-signature-info", b).lower()
        if "signature_verifies: true" not in info or leaf not in info:
            raise ValueError("signature or signer mismatch: " + rel)
    if base.suffix == ".bundle":
        # A private chain can be cryptographically valid but not trusted by
        # the build host. Do not treat that as platform authorization.
        p = subprocess.run(["/usr/bin/codesign", "--verify", "--deep",
                            "--strict", str(signed)], capture_output=True,
                           timeout=120)
        if p.returncode and b"CSSMERR_TP_NOT_TRUSTED" not in p.stderr:
            raise ValueError("bundle seal rejected: " + signed.name)

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", type=Path, required=True, help="finished pkgroot")
    ap.add_argument("--signed-gpu", type=Path,
                    help="directory containing final signed copies of all three units")
    ap.add_argument("--output", type=Path, help="new pkgroot; must not exist")
    ap.add_argument("--leaf-fingerprint", help="expected certificate fingerprint as hex")
    ap.add_argument("--rcodesign", default="rcodesign")
    args = ap.parse_args()
    if os.geteuid() == 0:
        ap.error("do not run as root")
    base = args.base.resolve(strict=True)
    inv = inventory(base)
    manifest(base, inv)
    for unit in UNITS:
        p = base / GPU / unit
        if not p.is_dir() or p.is_symlink():
            raise ValueError("missing regular graphics unit: " + str(p))
    if not args.signed_gpu:
        for unit in UNITS:
            p = base / GPU / unit
            for rel, v in sorted(inventory(p).items()):
                if v[0] == "file" and is_macho(p / rel):
                    print(str(Path(unit) / rel))
        return
    if not args.output or not args.leaf_fingerprint:
        ap.error("--signed-gpu requires --output and --leaf-fingerprint")
    leaf = args.leaf_fingerprint.replace(":", "").lower()
    if not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", leaf):
        ap.error("expected a SHA-1 or SHA-256 certificate fingerprint")
    signed = args.signed_gpu.resolve(strict=True)
    out = args.output.absolute()
    out_parent = out.parent.resolve(strict=True)
    out = out_parent / out.name
    if out.exists() or out.is_symlink() or out.is_relative_to(base) or out.is_relative_to(signed):
        ap.error("output must be a new directory outside both inputs")
    for unit in UNITS:
        p = signed / unit
        if not p.is_dir() or p.is_symlink():
            raise ValueError("signed unit missing: " + unit)
        verify_unit(base / GPU / unit, p, args.rcodesign, leaf)
    with tempfile.TemporaryDirectory(prefix=".nullmoth-signed-", dir=out_parent) as temp:
        stage = Path(temp) / "pkgroot"
        shutil.copytree(base, stage, symlinks=True)
        for unit in UNITS:
            dst = stage / GPU / unit
            shutil.rmtree(dst)
            shutil.copytree(signed / unit, dst, symlinks=True)
        after = inventory(stage)
        def scoped(p):
            return any(p.startswith((GPU / unit).as_posix() + "/") for unit in UNITS)
        if ({p: v for p, v in inv.items() if not scoped(p)} !=
                {p: v for p, v in after.items() if not scoped(p)}):
            raise ValueError("unrelated package data changed")
        with (stage / "SHA256SUMS").open("w") as f:
            for p, v in sorted(after.items()):
                if v[0] == "file" and p != "SHA256SUMS":
                    f.write(v[1] + "  ./" + p + "\n")
        manifest(stage, inventory(stage))
        os.rename(stage, out)
    print("Staged separately signed graphics package:", out)
    print("Host authorization and hardware compatibility are NOT established.")

if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise SystemExit("STOP: " + str(exc))
