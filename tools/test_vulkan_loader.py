#!/usr/bin/env python3
"""build/build_vulkan_loader.sh builds the Khronos Vulkan loader in a temporary folder and installs it there before it
copies the library out. Left to itself the loader's CMake compiles "<install prefix>/etc" into the library as the
folder where it looks for manifests and settings (SYSCONFDIR), before /etc. With the temporary folder as the prefix
the library carried that folder's name, looked there on every Mac it ran on, and two builds of the same source on the
same Mac had different bytes. The script gives CMake -DSYSCONFDIR=/etc, and before it copies the library out it checks
that the library does not name the temporary folder.

This test runs the real script as ./build.sh does (bash, with NM_KHRONOS_URL) against stand-ins, with a TMPDIR of its
own:
  git      clone makes a folder named as the repository; rev-parse prints the commit the test gave it for that name
  cmake    records what it was started with; configuring notes the install prefix and SYSCONFDIR (the prefix's etc
           when none is given, as the loader's CMakeLists.txt does; for one scene with the prefix's real path, as a
           program that resolves links names it); installing the loader writes a "dylib" that carries SYSCONFDIR
  lipo, vtool   print what the real ones print for an x86_64 library built for macOS 15
  strings  prints what it reads
Nothing outside the test's temporary folder is written.
usage: test_vulkan_loader.py"""
import re, shutil, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "build" / "build_vulkan_loader.sh"
OPTION = " -DSYSCONFDIR=/etc"
fails = 0


def check(what, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + what + ("" if ok else f"\n     {detail}"))
    fails += 0 if ok else 1


def tool(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + text)
    path.chmod(0o755)


src = SCRIPT.read_text()
TAG = re.search(r"^TAG=(v[0-9.]+)$", src, re.M).group(1)
SHA = {"Vulkan-Loader": re.search(r"^LOADER_SHA=([0-9a-f]{40})$", src, re.M).group(1),
       "Vulkan-Headers": re.search(r"^HEADERS_SHA=([0-9a-f]{40})$", src, re.M).group(1)}


class Scene:
    """the stand-ins, a TMPDIR for the script's temporary folder, and an output folder for each run"""

    def __init__(self, tmp, script_text=None):
        self.tmp = tmp
        self.script = tmp / "build_vulkan_loader.sh"
        self.script.write_text(script_text if script_text is not None else src)
        self.bin = tmp / "standins"
        self.t = tmp / "t"
        self.t.mkdir()
        self.link = tmp / "t-by-a-link"
        self.link.symlink_to(self.t)
        self.by_link = False   # the script's TMPDIR is the link
        (tmp / "home").mkdir()
        self.runs = 0
        for name, sha in SHA.items():
            (self.bin / f"commit-{name}").parent.mkdir(parents=True, exist_ok=True)
            (self.bin / f"commit-{name}").write_text(sha + "\n")
        tool(self.bin / "git", 'd=$(cd "$(dirname "$0")" && pwd)\n'
             'echo "git $*" >> "$d/calls.txt"\n'
             'case "$1" in\n'
             '  clone) for a in "$@"; do dest=$a; done\n'
             '         mkdir -p "$dest" || exit 1\n'
             '         [ "${dest##*/}" != Vulkan-Loader ] || echo "the loader\'s licence" > "$dest/LICENSE.txt" ;;\n'
             '  -C) [ "$3 $4" = "rev-parse HEAD" ] || { echo "stand-in git: unexpected: $*" >&2; exit 64; }\n'
             '      cat "$d/commit-${2##*/}" ;;\n'
             '  *) echo "stand-in git: unexpected: $*" >&2; exit 64 ;;\n'
             'esac\n')
        tool(self.bin / "cmake", 'd=$(cd "$(dirname "$0")" && pwd)\n'
             'echo "cmake $*" >> "$d/calls.txt"\n'
             'case "$1" in\n'
             '  -S) [ "$3" = -B ] || { echo "stand-in cmake: unexpected: $*" >&2; exit 64; }\n'
             '      what=${2##*/}; b=$4; prefix=; conf=\n'
             '      for a in "$@"; do case "$a" in -DCMAKE_INSTALL_PREFIX=*) prefix=${a#*=} ;; -DSYSCONFDIR=*) conf=${a#*=} ;; esac; done\n'
             '      [ -n "$prefix" ] || { echo "stand-in cmake: no install prefix" >&2; exit 64; }\n'
             '      named=$prefix; [ ! -e "$d/names-the-real-path" ] || named=$(cd "${prefix%/*}" && pwd -P)/${prefix##*/}\n'
             '      mkdir -p "$b" && echo "$what" > "$b/what" && echo "$prefix" > "$b/prefix" && echo "${conf:-$named/etc}" > "$b/sysconfdir" ;;\n'
             '  --build) [ -f "$2/what" ] || { echo "stand-in cmake: $2 was not configured" >&2; exit 64; } ;;\n'
             '  --install) b=$2; prefix=$(cat "$b/prefix") || exit 64\n'
             '      mkdir -p "$prefix/lib"\n'
             '      [ "$(cat "$b/what")" = Vulkan-Loader ] || exit 0\n'
             f'      printf \'%s\\n\' "the loader\'s code" /etc/xdg /usr/local/share:/usr/share "$(cat "$b/sysconfdir")" > "$prefix/lib/libvulkan.{TAG[1:]}.dylib" ;;\n'
             '  *) echo "stand-in cmake: unexpected: $*" >&2; exit 64 ;;\n'
             'esac\n')
        tool(self.bin / "lipo", '[ "$1" = -archs ] && [ -f "$2" ] || { echo "stand-in lipo: unexpected: $*" >&2; exit 64; }\necho x86_64\n')
        tool(self.bin / "vtool", '[ "$1" = -show-build ] && [ -f "$2" ] || { echo "stand-in vtool: unexpected: $*" >&2; exit 64; }\n'
             'printf \'%s\\n\' "$2:" "Load command 10" "      cmd LC_BUILD_VERSION" "  cmdsize 32" " platform MACOS" "    minos 15.0" "      sdk 26.5" "   ntools 1"\n')
        tool(self.bin / "strings", '[ "$1" = -a ] && shift\nif [ $# -gt 0 ]; then cat "$1"; else cat; fi\n')

    def run(self):
        self.runs += 1
        self.out = self.tmp / f"out{self.runs}"
        calls = self.bin / "calls.txt"
        if calls.exists():
            calls.unlink()
        env = {"PATH": f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(self.tmp / "home"), "TMPDIR": str(self.link if self.by_link else self.t), "LANG": "C",
               "NM_KHRONOS_URL": f"file://{self.tmp}/khronos"}
        r = subprocess.run(["bash", str(self.script), str(self.out)], capture_output=True, text=True, env=env, timeout=120)
        self.calls = calls.read_text().splitlines() if calls.exists() else []
        return r.returncode, r.stdout + r.stderr

    def lib(self):
        f = self.out / "libvulkan.1.dylib"
        return f.read_text() if f.is_file() else None

    def temp_names(self):
        """the names of the temporary folders the script's cmake was given as the loader's install prefix"""
        return sorted({m.group(1).rsplit("/", 1)[-1] for c in self.calls for m in [re.search(r"-DCMAKE_INSTALL_PREFIX=(.*)/loader$", c)] if m})


def scene(script_text=None, by_link=False):
    tmp = Path(tempfile.mkdtemp(prefix="vulkan loader-"))   # a blank in every path the script is given
    made.append(tmp)   # before the scene is made, so that a scene that cannot be made is removed too
    s = Scene(tmp, script_text)
    if by_link:   # the temporary folder is reached through a link, and CMake names it by its real path
        s.by_link = True
        (s.bin / "names-the-real-path").write_text("")
    return s


def last(out):
    return out.strip().splitlines()[-1] if out.strip() else ""


made = []
try:
    s = scene()
    rc, out = s.run()
    lib, names = s.lib(), s.temp_names()
    m = re.fullmatch(rf"Vulkan-Loader {re.escape(TAG)} {SHA['Vulkan-Loader']} sha256 ([0-9a-f]{{64}})", last(out))
    check("the loader's script ends well: the library and the licence copied out, the oldest macOS and the SHA-256 printed",
          rc == 0 and m is not None and "minos 15.0" in out and lib is not None and (s.out / "Vulkan-Loader-LICENSE.txt").is_file(), (rc, out[-600:]))
    conf = [c for c in s.calls if c.startswith("cmake -S ") and "/Vulkan-Loader " in c]
    check("the loader's CMake is given -DSYSCONFDIR=/etc, once",
          len(conf) == 1 and conf[0].split().count("-DSYSCONFDIR=/etc") == 1 and sum("SYSCONFDIR" in c for c in s.calls) == 1, s.calls)
    check("the sources are cloned from NM_KHRONOS_URL at the pinned tag",
          sum(c.startswith("git clone ") and f"--branch {TAG} file://{s.tmp}/khronos/{n} " in c for c in s.calls for n in SHA) == 2, s.calls)
    check("the library names /etc and not the temporary folder it was built in",
          lib is not None and len(names) == 1 and names[0] not in lib and "/etc\n" in lib, (names, lib))
    check("the temporary folder is gone when the script ends", list(s.t.iterdir()) == [], list(s.t.iterdir()))
    first = (m.group(1) if m else None, names)
    rc, out = s.run()
    m = re.fullmatch(rf"Vulkan-Loader {re.escape(TAG)} {SHA['Vulkan-Loader']} sha256 ([0-9a-f]{{64}})", last(out))
    check("a second build, in another temporary folder, gives a library of the same bytes",
          rc == 0 and m is not None and first[0] is not None and m.group(1) == first[0] and s.temp_names() != first[1] and len(s.temp_names()) == 1,
          (rc, first, s.temp_names(), out[-300:]))

    assert src.count(OPTION) == 1, "control: the option is not in the script once"
    without = src.replace(OPTION, "")
    s = scene(without)
    rc, out = s.run()
    names = s.temp_names()
    shown = [l for l in out.splitlines() if l.endswith("/loader/etc")]
    check("negative control: without the option the library names the temporary folder, and the script stops before it copies the library out",
          rc == 3 and "STOP: the loader names this build's temporary folder (1 place(s))" in out and len(names) == 1 and len(shown) == 1
          and names[0] in shown[0] and s.lib() is None and list(s.t.iterdir()) == [], (rc, names, out[-600:]))
    s = scene(without, by_link=True)
    rc, out = s.run()
    names = s.temp_names()
    shown = [l for l in out.splitlines() if l.endswith("/loader/etc")]
    check("a temporary folder reached through a link and named by its real path is found too: the folder's own name is looked for",
          rc == 3 and "STOP: the loader names this build's temporary folder (1 place(s))" in out and len(names) == 1 and len(shown) == 1
          and names[0] in shown[0] and s.link.name not in shown[0] and s.lib() is None, (rc, names, out[-600:]))
    own = '"${W##*/}"'
    assert src.count(own) == 2, "control: the folder's own name is not looked for in the script's two places"
    s = scene(without.replace(own, '"$W"'), by_link=True)
    rc, out = s.run()
    names, lib, sha = s.temp_names(), s.lib(), last(out)[-64:]
    rc2, out2 = s.run()
    check("negative control: a script that looks for the path as it was given does not see the real path and copies that library out, "
          "and its second build has other bytes",
          rc == 0 and "STOP" not in out and len(names) == 1 and lib is not None and names[0] in lib
          and rc2 == 0 and re.fullmatch("[0-9a-f]{64}", sha) and re.fullmatch("[0-9a-f]{64}", last(out2)[-64:]) and last(out2)[-64:] != sha,
          (rc, rc2, names, out[-600:], out2[-200:]))

    s = scene()
    tool(s.bin / "strings", 'echo "strings: cannot read" >&2; exit 1\n')
    rc, out = s.run()
    check("a strings that fails stops the script: what it did not print is not taken for a library without the folder's name",
          rc == 3 and "STOP: strings could not read the loader" in out and s.lib() is None, (rc, out[-600:]))

    s = scene()
    (s.bin / "commit-Vulkan-Loader").write_text("0" * 40 + "\n")
    rc, out = s.run()
    check("a tag that is another commit than the pinned one stops the script before CMake is started",
          rc == 2 and f"STOP: Vulkan-Loader {TAG} is not commit {SHA['Vulkan-Loader']}" in out and not any(c.startswith("cmake ") for c in s.calls)
          and s.lib() is None, (rc, out[-600:], s.calls))
finally:
    for t in made:
        shutil.rmtree(t, ignore_errors=True)
print(f"{'all passed' if not fails else str(fails) + ' failed'}")
sys.exit(1 if fails else 0)
