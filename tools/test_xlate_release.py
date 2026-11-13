#!/usr/bin/env python3
"""build/build_xlate_release.sh builds the shader translator with the cargo and rustc of the Rust toolchain's own
folder, not through rustup's proxies. A release build makes rustc strip what it links with Rust's rust-objcopy, and
that program finds Rust's libLLVM only when a proxy starts it. On an Intel Mac it aborted for every linked crate:
cargo's log carried "warning: stripping debug info with `rust-objcopy` failed: signal: 6 (SIGABRT)", the script stopped
on the translator's warning ceiling of 0, and macOS wrote a crash report per abort. The script tells cargo to leave that
strip out (CARGO_PROFILE_RELEASE_STRIP=none); its own strip -S removes the debug map from the dylib.

The script's last check is that the dylib names no build machine path. macOS's strings, given a Mach-O file by name,
reads its sections only; a debug map's names are in the symbol table's strings, outside every section, so the check
hands strings the file on its standard input.

This test runs the real script as ./build.sh does, through build/local_build_host.sh, in a checkout of its own in a
temporary folder, with stand-ins for the Rust toolchain and for the Mac's strip, install_name_tool, codesign, strings
and lipo:
  cargo    records what it was started with and writes a "dylib" that carries a debug map; without the setting it
           writes the two warning lines cargo 1.98.1 wrote on that Mac
  strip    removes the debug map, as strip -S does, or leaves it (the check's own case)
  strings  reads the part before the symbol table of a file it is given by name, and every byte of its standard input
Nothing outside the temporary folder is read or written.
usage: test_xlate_release.py"""
import os, shutil, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "build" / "build_xlate_release.sh"
HOST = "nullmoth-local-build"
SETTING = "CARGO_HOME=$CH CARGO_PROFILE_RELEASE_STRIP=none"   # the script's export line on the build host
MAP = "OSO /Users/builder/nmw/build/xlate-release/target/x86_64-apple-darwin/release/deps/nvmtl_translate.o"
fails = 0


def check(what, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + what + ("" if ok else f"\n     {detail}"))
    fails += 0 if ok else 1


def tool(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + text)
    path.chmod(0o755)


GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


class Scene:
    """a checkout with the script, the files it uses and a translator/ folder, a working directory, and the stand-ins"""

    def __init__(self, tmp, script_text=None):
        self.tmp = tmp
        self.co = tmp / "checkout"
        (self.co / "build").mkdir(parents=True)
        for n in ("local_build_host.sh", "warn_ceiling.sh", "warnings.txt"):
            shutil.copy(ROOT / "build" / n, self.co / "build" / n)
        (self.co / "build" / "build_xlate_release.sh").write_text(script_text if script_text is not None else SCRIPT.read_text())
        (self.co / "translator/wrapper/src").mkdir(parents=True)
        (self.co / "translator/wrapper/Cargo.toml").write_text('[package]\nname = "nvmtl_translate"\nversion = "0.1.0"\n')
        (self.co / "translator/wrapper/src/lib.rs").write_text("// stand-in\n")
        env = dict(os.environ, **GIT_ENV)
        for a in (["init", "-q"], ["add", "translator"],
                  ["-c", "user.name=test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "translator"]):
            subprocess.run(["git", "-C", str(self.co)] + a, check=True, capture_output=True, env=env)
        self.rev = subprocess.run(["git", "-C", str(self.co), "rev-parse", "HEAD"], check=True, capture_output=True, text=True, env=env).stdout.strip()
        self.work = tmp / "work"
        self.work.mkdir()
        self.out = self.work / "out/xlate"
        # the Rust toolchain's own folder. The script's command on the build host sets PATH to it and macOS's folders, so
        # the stand-ins of the tools that command runs (strip, install_name_tool, codesign) are in it too.
        self.tb = self.work / "tools/rustup/toolchains/1.98.1-x86_64-apple-darwin/bin"
        (self.tb.parent / "lib/rustlib/x86_64-apple-darwin/lib").mkdir(parents=True)
        tool(self.tb / "rustc", 'd=$(cd "$(dirname "$0")/.." && pwd)\n'
             'case "$*" in\n'
             '  --version) echo "rustc 1.98.1 (48a229cea 2026-09-01)" ;;\n'
             '  "--print target-libdir --target x86_64-apple-darwin") echo "$d/lib/rustlib/x86_64-apple-darwin/lib" ;;\n'
             '  *) echo "stand-in rustc: unexpected: $*" >&2; exit 64 ;;\n'
             'esac\n')
        tool(self.tb / "cargo", 'd=$(cd "$(dirname "$0")" && pwd)\n'
             '{ echo "args=$*"; echo "strip=${CARGO_PROFILE_RELEASE_STRIP-<unset>}"; echo "first_on_path=${PATH%%:*}"; echo "cargo_home=${CARGO_HOME-<unset>}"; } > "$d/cargo-record.txt"\n'
             't=; while [ $# -gt 0 ]; do [ "$1" = --target-dir ] && t=$2; shift; done\n'
             '[ -n "$t" ] || { echo "error: stand-in cargo: no --target-dir" >&2; exit 101; }\n'
             'mkdir -p "$t/x86_64-apple-darwin/release"\n'
             f'printf \'%s\\n\' "the translator\'s code and data" "==SYMBOL TABLE==" "{MAP}" > "$t/x86_64-apple-darwin/release/libnvmtl_translate.dylib"\n'
             'echo "   Compiling nvmtl_translate v0.1.0 (/src/wrapper)" >&2\n'
             'case "${CARGO_PROFILE_RELEASE_STRIP-}" in\n'
             '  none|false) ;;\n'   # rustc is not asked to strip: rust-objcopy is not started
             '  *) echo \'warning: stripping debug info with `rust-objcopy` failed: signal: 6 (SIGABRT)\' >&2\n'
             '     echo \'warning: `nvmtl_translate` (lib) generated 1 warning\' >&2 ;;\n'
             'esac\n'
             'echo \'    Finished `release` profile [optimized] target(s) in 0.01s\' >&2\n')
        tool(self.tb / "strip", 'd=$(cd "$(dirname "$0")" && pwd)\n'
             '[ "$1" = -S ] && [ -f "$2" ] || { echo "stand-in strip: unexpected: $*" >&2; exit 64; }\n'
             '[ -e "$d/strip-leaves-the-map" ] && exit 0\n'
             'grep -v "^OSO " "$2" > "$2.new" && mv "$2.new" "$2"\n')
        tool(self.tb / "install_name_tool", 'exit 0\n')
        tool(self.tb / "codesign", 'exit 0\n')
        # the tools the script itself runs on this side
        self.bin = tmp / "standins"
        tool(self.bin / "strings", '[ "$1" = -a ] && shift\n'
             'if [ $# -gt 0 ]; then sed "/^==SYMBOL TABLE==\\$/,\\$d" "$1"; else cat; fi\n')
        tool(self.bin / "lipo", 'echo x86_64\n')
        self.thost = self.work / "tools/host"
        self.thost.mkdir(parents=True)
        for l in ("ssh", "rsync", "scp"):
            (self.thost / l).symlink_to(self.co / "build/local_build_host.sh")
        (tmp / "home").mkdir()

    def run(self):
        env = dict(GIT_ENV, PATH=f"{self.thost}:{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin", HOME=str(self.tmp / "home"), TMPDIR=str(self.tmp), LANG="C",
                   NULLMOTH_BUILD_HOST=HOST, NM_LOCAL_BUILD_HOST=HOST, NULLMOTH_BUILD_ROOT=str(self.work), NULLMOTH_RUST_VERSION="1.98.1",
                   NULLMOTH_RUST_BIN=str(self.tb), NULLMOTH_CARGO_HOME=str(self.work / "tools/cargo"))
        r = subprocess.run(["bash", str(self.co / "build/build_xlate_release.sh"), self.rev, str(self.out)], capture_output=True, text=True, env=env, timeout=120)
        return r.returncode, r.stdout + r.stderr

    def record(self):
        f = self.tb / "cargo-record.txt"
        return dict(l.split("=", 1) for l in f.read_text().splitlines()) if f.exists() else {}


def scene(script_text=None, strip_leaves=False):
    tmp = Path(tempfile.mkdtemp(prefix="xlate-release-"))
    made.append(tmp)   # before the scene is made, so that a scene that cannot be made is removed too
    s = Scene(tmp, script_text)
    if strip_leaves:
        (s.tb / "strip-leaves-the-map").write_text("")
    return s


src = SCRIPT.read_text()
made = []
try:
    s = scene()
    rc, out = s.run()
    rec = s.record()
    check("the translator's script ends well: no warning in cargo's log, the dylib copied out",
          rc == 0 and "translator warnings 0 (ceiling 0)" in out and f"libnvmtl_translate {s.rev[:7]}" in out and "arch x86_64 rustc 1.98.1" in out
          and (s.out / "libnvmtl_translate.dylib").is_file(), (rc, out[-600:]))
    check("cargo is told to leave rustc's strip out (CARGO_PROFILE_RELEASE_STRIP=none reaches it through the build host's empty environment)",
          rec.get("strip") == "none", rec)
    check("cargo is the toolchain's own, first on the build command's PATH, with the cargo home it was given",
          rec.get("first_on_path") == str(s.tb) and rec.get("cargo_home") == str(s.work / "tools/cargo"), rec)
    d = s.out / "libnvmtl_translate.dylib"
    check("the script's own strip -S took the debug map out of the dylib", d.is_file() and MAP not in d.read_text(), d.read_text() if d.is_file() else "no dylib")

    assert src.count(SETTING) == 1, "control: the export line with the setting is not in the script once"
    s = scene(src.replace(SETTING, "CARGO_HOME=$CH"))
    rc, out = s.run()
    check("negative control: without the setting the script stops on the translator's warning ceiling, as it did on the Intel Mac",
          rc == 4 and "STOP: translator has 2 compiler warnings, more than its ceiling of 0" in out and s.record().get("strip") == "<unset>"
          and "warning: stripping debug info with `rust-objcopy` failed: signal: 6 (SIGABRT)" in out, (rc, out[-600:], s.record()))

    s = scene(strip_leaves=True)
    rc, out = s.run()
    check("a debug map left in the dylib stops the script: its names are read although they are in no section",
          rc == 5 and "STOP: 1 build machine path(s) left in the dylib" in out and MAP in out, (rc, out[-600:]))
    by_name = src.replace('strings -a < "$D"', 'strings -a "$D"')
    assert src.count('strings -a < "$D"') == 1 and by_name != src, "control: the read of the dylib is not in the script"
    s = scene(by_name, strip_leaves=True)
    rc, out = s.run()
    check("negative control: a script that hands strings the file by name does not see the debug map and ends well",
          rc == 0 and "build machine path" not in out, (rc, out[-600:]))

    s = scene()
    tool(s.bin / "strings", 'echo "strings: cannot read" >&2; exit 1\n')
    rc, out = s.run()
    check("a strings that fails stops the script: what it did not print is not taken for a dylib without paths",
          rc == 5 and "STOP: strings could not read the dylib" in out, (rc, out[-600:]))
finally:
    for t in made:
        shutil.rmtree(t, ignore_errors=True)
print(f"{'all passed' if not fails else str(fails) + ' failed'}")
sys.exit(1 if fails else 0)
