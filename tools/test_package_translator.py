#!/usr/bin/env python3
"""The Metal plugin opens the shader translator beside it (NVMTLDriver.bundle/Contents/MacOS/libnvmtl_translate.dylib)
whenever it can read one, and nvmtl/'s only when there is none (plugin/NVMTLLibraryLoad.m, nvmtl_xlate_ready). Release
2.9, the base build/package_local.sh stages onto, carries its translator only there, and the packages made before this
fix (from the 3.0 branch, and from 3.0.1 up to b80056ef) put this build's in nvmtl/ alone: every Metal app translated
with 2.9's. The package step now puts this build's in both places, and package/check_package.sh stops a package whose
two copies are not the same build. Their bytes may differ (each copy is signed on its own, and the bundle's again by
the reseal), so the guard compares their LC_UUIDs.

The package step runs whole here, on a base laid out like 2.9's, with stand-ins for the build's outputs and for Apple's
tools (codesign, lipo, nm, otool, plutil, ditto, and a tar that drops the options only bsdtar has), so it runs on Linux
too. The translators are small Mach-O files with an LC_UUID; the stand-in codesign appends a mark that names the file it
signed, so two copies signed apart always differ here (real ad hoc signatures may not), and the guard must pass them.
Where a real otool is found (the Mac's, or LLVM's llvm-otool), the guard is run once more with it, which checks that it
reads what that otool prints.
usage: test_package_translator.py"""
import hashlib, os, plistlib, re, shutil, struct, subprocess, sys, tempfile, uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUNDLED = "Library/GPUBundles/NVMTLDriver.bundle/Contents/MacOS/libnvmtl_translate.dylib"
NVMTL = "Library/GPUBundles/nvmtl/libnvmtl_translate.dylib"
OLD = "29292929-0000-4000-8000-000000000029"   # the base's translator (release 2.9's)
NEW = "30130130-0000-4000-8000-000000000301"   # this build's
STOP_LINE = "STOP: the translator beside the plugin"
fails = 0


def check(what, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + what + ("" if ok else f"\n     {detail}"))
    fails += 0 if ok else 1


def macho(u, tail=b""):
    """A 64-bit x86_64 dylib: its header, LC_ID_DYLIB and LC_UUID <u>, then <tail> (enough for otool -l, -L, -D)."""
    name = b"@rpath/libnvmtl_translate.dylib\0"
    name += b"\0" * (-(24 + len(name)) % 8)
    cmds = struct.pack("<IIIIII", 0xd, 24 + len(name), 24, 2, 0x10000, 0x10000) + name
    cmds += struct.pack("<II", 0x1b, 24) + uuid.UUID(u).bytes
    return struct.pack("<IiiIIIII", 0xfeedfacf, 0x01000007, 3, 6, 2, len(cmds), 0, 0) + cmds + tail


def uuid_of(path):
    """The LC_UUID of a thin 64-bit Mach-O (what macho() makes), upper case as otool prints it; None if it has none."""
    d = Path(path).read_bytes()
    if d[:4] != b"\xcf\xfa\xed\xfe":
        return None
    off = 32
    for _ in range(struct.unpack_from("<I", d, 16)[0]):
        cmd, size = struct.unpack_from("<II", d, off)
        if cmd == 0x1b:
            return str(uuid.UUID(bytes=d[off + 8:off + 24])).upper()
        off += size
    return None


OTOOL = '''import struct, sys, uuid
# otool stand-in: -l prints the load commands as otool does (LC_UUID with its uuid), -L and -D the library's own name
path = sys.argv[-1]
try:
    d = open(path, "rb").read()
except OSError as e:
    sys.exit(f"otool: {e}")
if d[:4] != b"\\xcf\\xfa\\xed\\xfe":
    print(f"{path}: is not an object file"); sys.exit(1)
print(f"{path}:")
NAMES = {0x1b: "LC_UUID", 0xd: "LC_ID_DYLIB"}
off = 32
for i in range(struct.unpack_from("<I", d, 16)[0]):
    cmd, size = struct.unpack_from("<II", d, off)
    if "-l" in sys.argv:
        print(f"Load command {i}\\n     cmd {NAMES.get(cmd, hex(cmd))}\\n cmdsize {size}")
        if cmd == 0x1b:
            print("    uuid " + str(uuid.UUID(bytes=d[off + 8:off + 24])).upper())
    elif cmd == 0xd:
        name = d[off + struct.unpack_from("<I", d, off + 8)[0]:off + size].split(b"\\0")[0].decode()
        print(name if "-D" in sys.argv else f"\\t{name} (compatibility version 1.0.0, current version 1.0.0)")
    off += size
'''
PLUTIL = '''import plistlib, sys
# plutil stand-in: -extract <key> raw -o - <file>
try:
    v = plistlib.load(open(sys.argv[-1], "rb"))[sys.argv[sys.argv.index("-extract") + 1]]
except Exception as e:
    sys.exit(f"plutil: {e}")
print(v)
'''
CODESIGN = r'''#!/bin/bash
# codesign stand-in: signing a file appends a mark that names it (two copies signed apart differ, as real ones may); a
# bundle gets a seal file, and with --deep each Mach-O in its Contents/MacOS is signed too; checks pass; -d prints an
# ad hoc identity
sign=0; deep=0; t=
for a in "$@"; do
  case "$a" in
    -s|--sign) sign=1;;
    --deep) deep=1;;
    -d*|--display) printf 'Identifier=standin\nSignature=adhoc\n' >&2; exit 0;;
  esac
  t=$a
done
[ "$sign" = 1 ] || exit 0
mark() { printf '\0standin signature of %s' "$1" >> "$1"; }
if [ -d "$t" ]; then
  mkdir -p "$t/Contents/_CodeSignature" || exit 1
  printf 'standin seal\n' > "$t/Contents/_CodeSignature/CodeResources" || exit 1
  if [ "$deep" = 1 ] && [ -d "$t/Contents/MacOS" ]; then
    while IFS= read -r f; do
      [ "$(head -c 4 "$f" | od -An -tx1 | tr -d ' \n')" != cffaedfe ] || mark "$f"
    done < <(find "$t/Contents/MacOS" -type f)
  fi
elif [ -f "$t" ]; then mark "$t"
else exit 1; fi
'''
TAR = r'''#!/bin/bash
# tar stand-in: the system's tar without the options only bsdtar has (GNU tar has no --uid or --no-mac-metadata)
a=()
while [ $# -gt 0 ]; do
  case "$1" in
    --uid|--gid|--uname|--gname) shift 2; continue;;
    --no-xattrs|--no-acls|--no-fflags|--no-mac-metadata) shift; continue;;
  esac
  a+=("$1"); shift
done
for t in /usr/bin/tar /bin/tar; do if [ -x "$t" ]; then exec "$t" "${a[@]}"; fi; done
exit 1
'''


def standins(d, otool=None):
    """Apple's tools as the package step uses them, in a new folder <d>; otool: a real otool to link instead of the
    stand-in. The folder is made anew each time, so no write lands through a link made by an earlier call."""
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    py = f"#!{sys.executable}\n"
    scripts = {"codesign": CODESIGN, "tar": TAR, "plutil": py + PLUTIL, "otool": py + OTOOL,
               "lipo": "#!/bin/bash\necho x86_64\n",   # lipo -archs
               "nm": "#!/bin/bash\nfor s in _NVEncVT_Register _NVEncVT_RegisterDecoders _NVEncVT_CreateInstance "
                     "_NVDecVT_CreateInstance _NVEncVT_BlackwellSelfTest; do echo \"0000000000001000 T $s\"; done\n",
               "ditto": "#!/bin/bash\nexec cp -pR \"$1\" \"$2\"\n"}
    for name, text in scripts.items():
        (d / name).write_text(text)
        os.chmod(d / name, 0o755)
    if otool:
        (d / "otool").unlink()
        os.symlink(otool, d / "otool")
    return d


def write(root, files):
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)


def base_tree(d, bundled=True):
    """A base like release 2.9: the plugin bundle with its translator, no translator in nvmtl/, and a SHA256SUMS
    written as 2.9's is (no ./ before the paths)."""
    files = {"install.sh": b"#!/bin/bash\n", "nullmoth-runtime-check.sh": b"#!/bin/bash\n", "VERSION": b"2.9\n",
             "Library/GPUBundles/nvmtl-allow.txt": b"x\n",
             "Library/GPUBundles/NVMTLDriver.bundle/Contents/Info.plist": b"<plist/>\n",
             "Library/GPUBundles/NVMTLDriver.bundle/Contents/MacOS/NVMTLDriver": b"the 2.9 plugin\n",
             "Library/GPUBundles/NVMTLDriver.bundle/Contents/_CodeSignature/CodeResources": b"the 2.9 seal\n",
             "Library/GPUBundles/nvmtl/libvulkan.1.4.362.dylib": b"lib\n"}
    if bundled:
        files[BUNDLED] = macho(OLD, b"release 2.9's translator")
    write(d, files)
    (d / "SHA256SUMS").write_text("".join(f"{hashlib.sha256(v).hexdigest()}  {r}\n" for r, v in files.items()))
    os.symlink("libvulkan.1.4.362.dylib", d / "Library/GPUBundles/nvmtl/libvulkan.1.dylib")
    os.symlink("libvulkan.1.dylib", d / "Library/GPUBundles/nvmtl/libvulkan.dylib")


def build_out(o, xlate=None):
    """What ./build.sh's components leave in out/, as far as the package step reads it."""
    files = {f: f"stand-in {f}\n".encode() for f in (
        "dlss-pkg/dlss_fetch.sh", "dlss-pkg/dlss/dlss_cache", "dlss-pkg/dlss/nm_unzstd",
        "dlss-pkg/dlss/nm_unzstd.LICENSE", "dlss-pkg/dlss/assertfail_trap.ptx",
        "kexts/nvrm/NVRM.kext/Contents/Info.plist", "kexts/fb/NVRMFB.kext/Contents/Info.plist",
        "kexts/agdc/NVRMAGDC.kext/Contents/Info.plist", "kexts/accel/NVAccel.kext/Contents/Info.plist",
        "kexts/accel26/NVAccel.kext/Contents/Info.plist", "nvk/libvulkan_nouveau.dylib",
        "nvkbridge/lib/libnvk_minecraft.dylib", "nvkbridge/lib/libnvk_renderer.dylib", "nvkbridge/nvk_bridge_icd.json",
        "nvkbridge/nvk-bridge-runtime.properties", "plugin/NVMTLDriver", "zink/zink/libnvk_zink.dylib",
        "zink/zink/libnvk_cgl.dylib", "zink/zink/zink_sync_probe", "zink/zink/gld_bundle_probe",
        "zink/zink/cgl-dispatch-map.json", "zink/zink/MESA-SOURCE.json",
        "zink/NVKZinkGLDriver.bundle/Contents/Info.plist", "ryzenadj/ryzenadj", "nvencvt/libnvencvt.dylib",
        "userland/nvmtl-air-dis",
        "kexts/nmsmn/NMSMN.kext/Contents/MacOS/NMSMN", "boost/nvmtl-boost", "vulkan-loader/libvulkan.1.dylib")}
    files["kexts/nmsmn/NMSMN.kext/Contents/Info.plist"] = plistlib.dumps({"CFBundleIdentifier": "com.nullmoth.NMSMN"})
    files["xlate/libnvmtl_translate.dylib"] = xlate if xlate is not None else macho(NEW, b"this build's translator")
    write(o, files)


def package(base, t, tree=ROOT, otool=None):
    """Runs <tree>'s build/package_local.sh on <base> with the stand-ins; returns (exit, output, pkgroot)."""
    out = t / "out"
    if not out.exists():
        build_out(out)
    tools = standins(t / "standins", otool)
    env = {"PATH": f"{tools}:/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(t), "TMPDIR": str(t), "LANG": "C",
           "NM_PACKAGE_VERSION": "3.0.1"}
    r = subprocess.run(["bash", str(tree / "build/package_local.sh"), str(base), str(out), str(t / "pkg")],
                       capture_output=True, text=True, env=env, timeout=300)
    return r.returncode, r.stdout + r.stderr, t / "pkg" / "pkgroot"


def sums_verify(root):
    r = subprocess.run(["shasum", "-a", "256", "--strict", "-c", "SHA256SUMS", "--quiet"], cwd=root,
                       capture_output=True, text=True)
    return r.returncode == 0, r.stdout + r.stderr


def lines_naming(root, rel):
    return [l for l in (root / "SHA256SUMS").read_text().splitlines() if l[66:] in (rel, "./" + rel)]


def changed_tree(t, edits):
    """A copy of what the package step reads (build/package_local.sh, package/, licenses/, the two pages it copies from
    tools/) with each (file, pattern) of <edits> taken out of its file; None when a pattern finds nothing."""
    tree = t / "tree"
    (tree / "build").mkdir(parents=True)
    shutil.copy(ROOT / "build/package_local.sh", tree / "build/package_local.sh")
    shutil.copytree(ROOT / "package", tree / "package")
    shutil.copytree(ROOT / "licenses", tree / "licenses")
    (tree / "tools/nvk-bridge").mkdir(parents=True)
    for f in ("INSTALL.md", "APPLICATIONS.md"):
        shutil.copy(ROOT / "tools/nvk-bridge" / f, tree / "tools/nvk-bridge" / f)
    for f, pattern in edits:
        src = (tree / f).read_text()
        m = re.search(pattern, src, re.S)
        if not m:
            return None
        (tree / f).write_text(src.replace(m.group(0), "\n"))
    return tree


# the copy into the bundle (build/package_local.sh) and the guard (check_package.sh), as the controls take them out
FIX = ("build/package_local.sh",
       r"\nXB=Library/GPUBundles/NVMTLDriver\.bundle/Contents/MacOS/libnvmtl_translate\.dylib\n"
       r"if \[ -f \"\$R/\$XB\" \]; then put_signed [^\n]*\n")
GUARD = ("package/check_package.sh", r"\nXB=\"\$PB/Contents/MacOS/libnvmtl_translate\.dylib\".*?\nfi\n")


def in_temp(fn):
    t = Path(tempfile.mkdtemp(prefix="pkgxlate-"))
    try:
        return fn(t)
    finally:
        shutil.rmtree(t, ignore_errors=True)


def arm_package_with_the_bundled_copy(t):
    base = t / "dl" / "pkgroot"; base.mkdir(parents=True); base_tree(base)
    rc, out, r = package(base, t)
    check("the package step on a base like 2.9 ends without a stop", rc == 0, out[-1500:])
    if rc != 0:
        return
    check("the translator beside the plugin is this build's (the base's was 2.9's)",
          uuid_of(base / BUNDLED) == OLD and uuid_of(r / BUNDLED) == NEW,
          (uuid_of(base / BUNDLED), uuid_of(r / BUNDLED)))
    check("nvmtl/ still has this build's translator too", uuid_of(r / NVMTL) == NEW, uuid_of(r / NVMTL))
    check("the guard let the two copies through", STOP_LINE not in out, out[-1500:])
    check("the step names the copy it replaced in the bundle", f"replaced {BUNDLED} (signed ad hoc)" in out,
          out[-1500:])
    ok, why = sums_verify(r)
    check("SHA256SUMS verifies (shasum --strict -c, as install.sh checks it)", ok, why)
    lines = lines_naming(r, BUNDLED)
    check("one SHA256SUMS line names the bundle's translator, with its hash after the reseal",
          len(lines) == 1 and lines[0][:64] == hashlib.sha256((r / BUNDLED).read_bytes()).hexdigest(), lines)
    check("the tarball is written", (t / "pkg" / "nullmoth-nvidia-local.tar.gz").is_file(), out[-600:])


def arm_package_without_the_bundled_copy(t):
    base = t / "dl" / "pkgroot"; base.mkdir(parents=True); base_tree(base, bundled=False)
    rc, out, r = package(base, t)
    check("a base without a translator in the bundle is packaged too", rc == 0, out[-1500:])
    if rc == 0:
        check("and gets none there (the plugin then opens nvmtl/'s, this build's)",
              not (r / BUNDLED).exists() and not lines_naming(r, BUNDLED) and uuid_of(r / NVMTL) == NEW)


def arm_the_package_of_3_0_1(t):
    """Negative controls: the package step of 3.0.1 (no copy into the bundle), with the guard and without it."""
    tree = changed_tree(t / "a", [FIX])
    check("the package step has its copy into the bundle", tree is not None)
    if tree:
        base = t / "a" / "dl" / "pkgroot"; base.mkdir(parents=True); base_tree(base)
        rc, out, r = package(base, t / "a", tree)
        stop = [l for l in out.splitlines() if l.startswith(STOP_LINE)]
        named = len(stop) == 1 and all(w in stop[0] for w in (BUNDLED, NVMTL, OLD, NEW))
        check("negative control: without the copy into the bundle the guard stops the package step, naming both "
              "copies and both LC_UUIDs", rc != 0 and named, out[-1500:])
    tree = changed_tree(t / "b", [FIX, GUARD])
    check("check_package.sh has the guard", tree is not None)
    if tree:
        base = t / "b" / "dl" / "pkgroot"; base.mkdir(parents=True); base_tree(base)
        rc, out, r = package(base, t / "b", tree)
        check("negative control: without both, the package is made and carries 2.9's translator beside the plugin "
              "(the fault of the packages made before this fix)", rc == 0 and uuid_of(r / BUNDLED) == OLD, out[-1500:])


def gate(t, files, otool=None):
    """check_package.sh on a small tree holding <files>: the guard's lines (the tree fails other checks too)."""
    root = t / "pkgroot"
    shutil.rmtree(root, ignore_errors=True)
    files = dict(files, **{"Library/GPUBundles/NVMTLDriver.bundle/Contents/MacOS/libnvencvt.dylib": b"v"})
    write(root, files)
    (root / "SHA256SUMS").write_text("".join(f"{hashlib.sha256(v).hexdigest()}  ./{r}\n" for r, v in files.items()))
    tools = standins(t / "gate-standins", otool)
    env = {"PATH": f"{tools}:/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(t), "TMPDIR": str(t), "LANG": "C"}
    r = subprocess.run(["bash", str(ROOT / "package/check_package.sh"), str(root)], capture_output=True, text=True,
                       env=env, timeout=120)
    return [l for l in r.stdout.splitlines() if l.startswith(STOP_LINE)], r.stdout


def arm_the_guard(t, otool=None, who="stand-in otool"):
    lines, out = gate(t, {BUNDLED: macho(OLD), NVMTL: macho(NEW)}, otool)
    named = len(lines) == 1 and all(w in lines[0] for w in (BUNDLED, NVMTL, f"LC_UUID {OLD}", f"LC_UUID {NEW}"))
    check(f"{who}: two builds stop the package, naming both copies and both LC_UUIDs", named, out)
    lines, out = gate(t, {BUNDLED: macho(NEW, b"signed here"), NVMTL: macho(NEW, b"signed there")}, otool)
    check(f"{who}: one build signed apart (other bytes, the same LC_UUID) passes", not lines, out)
    if otool:
        return
    lines, out = gate(t, {BUNDLED: macho(NEW)})
    check("a translator in the bundle and none in nvmtl/ stops the package",
          len(lines) == 1 and NVMTL in lines[0] and "none read" in lines[0], out)
    lines, out = gate(t, {BUNDLED: b"not a Mach-O file\n", NVMTL: macho(NEW)})
    check("a file in the bundle whose LC_UUID cannot be read stops the package",
          len(lines) == 1 and "none read" in lines[0], out)
    lines, out = gate(t, {BUNDLED: b"not a Mach-O file\n", NVMTL: b"not a Mach-O file\n"})
    check("two copies whose LC_UUIDs cannot be read stop the package (an empty one is not a match)",
          len(lines) == 1 and lines[0].count("none read") == 2, out)
    silent = t / "silent-otool"
    silent.write_text("#!/bin/bash\nexit 1\n")
    os.chmod(silent, 0o755)
    lines, out = gate(t, {BUNDLED: macho(OLD), NVMTL: macho(NEW)}, str(silent))
    check("an otool that prints nothing (missing or failing) stops the package instead of passing two builds",
          len(lines) == 1 and lines[0].count("none read") == 2, out)
    lines, out = gate(t, {NVMTL: macho(NEW)})
    check("no translator in the bundle: the guard has nothing to say", not lines, out)


def real_otool():
    """The Mac's otool, or LLVM's (llvm-otool, also named with its version): what the guard reads on a Mac."""
    for name in ["otool", "llvm-otool"] + [f"llvm-otool-{v}" for v in range(40, 13, -1)]:
        p = shutil.which(name)
        if p:
            return p
    return None


def main():
    in_temp(arm_package_with_the_bundled_copy)
    in_temp(arm_package_without_the_bundled_copy)
    in_temp(arm_the_package_of_3_0_1)
    in_temp(arm_the_guard)
    o = real_otool()
    if o:
        in_temp(lambda t: arm_the_guard(t, o, f"real otool ({o})"))
    else:
        print("skip the guard with a real otool: none here (neither otool nor llvm-otool)")
    print(f"{'all passed' if not fails else str(fails) + ' failed'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
