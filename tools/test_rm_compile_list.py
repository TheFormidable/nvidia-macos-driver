#!/usr/bin/env python3
"""test_rm_compile_list.py: build/build_rm620.sh and build_kms620.sh run again in a folder they built, and what the
kext builds take from them.

build_rm620.sh writes compile_cmds.sh from NVIDIA's "make -n", which lists only what is out of date. Run a second time
in the same work folder it listed one command, and compile_cmds.sh lost the rest: build_nvrm.sh then stopped ("0 osapi.c
lines") and accel_build.sh took its -D flags from the one line left, a file with defines of its own, without a word.

This runs the real scripts on a small stand-in NVIDIA tree (real make, real patch, python3; stand-ins for xcrun, clang,
libtool, strings, stat, md5 and for the account and computer names) and checks:
  * the list is complete after a first build, a second run that changed nothing, a second run after one source was
    touched, and a fresh work folder (what build.sh --force does), and accel_build.sh gets the same -D flags each time;
  * a list that lacks the osapi.c command, or whose command lacks the RM's defines, stops build/rm_defines.sh and
    accel_build.sh with a message, and nothing is compiled;
Negative controls: the scripts as they were at 19279ba (read with git show) must fail the same checks."""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OLD = "19279ba"   # the scripts before the fix
ACCOUNT, HOST = "ike", "mybox"
fails = 0


def check(name, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + name + (f"  [{str(detail)[:1200]}]" if detail and not ok else ""), flush=True)
    fails += 0 if ok else 1


def negative(name, problems):
    check(f"control: {name}", bool(problems), "the check found nothing wrong with the old script")


STANDIN = r'''#!/bin/bash
# the stand-in tools: <role> <args...>
role=${0##*/}
case "$role" in
  id) [ "${1:-}" = -un ] && echo "$NMT_ACCOUNT" || /usr/bin/id "$@" ;;
  whoami) echo "$NMT_ACCOUNT" ;;
  hostname) echo "$NMT_HOST" ;;
  strings) shift; cat "$@" 2>/dev/null || cat "$1" ;;
  stat) f=${@: -1}; wc -c < "$f" | tr -d ' ' ;;
  md5) f=${@: -1}; cksum "$f" | cut -d' ' -f1 ;;
  clang|clang++)
    [ "${1:-}" = --version ] && { echo "stand-in clang 0"; exit 0; }
    o=""; prev=""; for a in "$@"; do [ "$prev" = -o ] && o=$a; prev=$a; done
    printf '%s\n' "$@" > "$o"
    if [ -n "${NMT_FLAGLOG:-}" ]; then printf '%s\n' "$@" > "$NMT_FLAGLOG.$(basename "$o")"; fi ;;
  xcrun)
    case "$1" in
      --show-sdk-path) echo "$NMT_SDK" ;;
      libtool) out=""; list=""; shift
               while [ $# -gt 0 ]; do case "$1" in -o) out=$2; shift ;; -filelist) list=$2; shift ;; esac; shift; done
               { while read -r f; do cat "$f"; done < "$list"; printf '%s\n' "${NMT_ARCHIVE_EXTRA:-}"; } > "$out" ;;
      *) exec "$@" ;;
    esac ;;
esac
'''

BASE_D = "-DNVRM -D_LANGUAGE_C -DNV_X86_64 -DPORT_IS_KERNEL_BUILD=1 -D\"MBEDTLS_USER_CONFIG_FILE=<cfg.h>\""
# (source, compiler, defines of its own): the first line is not osapi.c's, and one file has a define the others lack, as
# NVIDIA's libspdm files do
RM_SOURCES = [("generated/g_first.c", "cc", ""), ("arch/nvalloc/unix/src/osapi.c", "cc", ""),
              ("src/libraries/libspdm/spdm.c", "cc", " -DLIBSPDM_CONFIG")]
KMS_SOURCES = [("src/nvkms.c", "cc", ""), ("lib/dp.cpp", "c++", "")]   # the DisplayPort library is C++


def makefile(sources):
    """GNU make 3.81 compatible: explicit rules, so that "make -n" lists only what is out of date, as NVIDIA's does"""
    objs = " ".join(f"$(OUTPUTDIR)/{Path(s).stem}.o" for s, _, _ in sources)
    t = ["OUTPUTDIR ?= _out/$(TARGET_OS)_$(TARGET_ARCH)", "NV_BUILD_USER ?= $(shell whoami)", "NV_BUILD_HOST ?= $(shell hostname)",
         f"all: $(OUTPUTDIR)/version.h {objs} $(OUTPUTDIR)/g_nvid_string.c", "",
         "$(OUTPUTDIR)/version.h:", "\techo '#define NVIDIA_VERSION \"620.70.06\"' > $@", "",
         f"$(OUTPUTDIR)/g_nvid_string.c: {objs}",
         "\techo 'const char NVRM_ID[] = \"nvidia id: x  ($(NV_BUILD_USER)@$(NV_BUILD_HOST))\";' > $@", ""]
    for s, cc, extra in sources:
        t += [f"$(OUTPUTDIR)/{Path(s).stem}.o: {s}", f"\t{cc} {BASE_D}{extra} -O2 -mcmodel=kernel -fno-pic -c {s} -o $@", ""]
    return "\n".join(t) + "\n"


def pristine(root):
    """A pristine 620.70.06 tree with the anchors build_rm620.sh edits, and the two makefiles."""
    files = {
        "version.mk": "NVIDIA_VERSION = 620.70.06\n",
        "src/common/sdk/nvidia/inc/cpuopsys.h": "#if (defined(__INTEGRITY) ) && !defined(nvmacosx)\n#endif\n",
        "src/common/inc/nvVer.h": "#if defined(NV_QNX) || defined(NV_INTEGRITY) || \\\n    0\n#endif\n",
        "src/common/inc/nvUnixVersion.h": "#if defined(NV_QNX) || defined(NV_INTEGRITY) || \\\n    0\n#endif\n",
        "src/common/inc/nvBldVer.h": "#if defined(NV_LINUX) || defined(NV_BSD) || defined(NV_SUNOS)\n#endif\n",
        "src/nvidia/src/kernel/gpu/timer/timer_ostimer.c": "void f(void) {\n    int x = 0;\n}\n",
        "src/nvidia-modeset/src/kapi.c": "int kapi(void) {\n    return 0;\n}\n",
        "src/nvidia/Makefile": makefile(RM_SOURCES),
        "src/nvidia-modeset/Makefile": makefile(KMS_SOURCES),
    }
    for s, _, _ in RM_SOURCES:
        files[f"src/nvidia/{s}"] = "int a;\n"
    for s, _, _ in KMS_SOURCES:
        files[f"src/nvidia-modeset/{s}"] = "int b;\n"
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


PATCH_ROOT = """--- a/src/nvidia/src/kernel/gpu/timer/timer_ostimer.c
+++ b/src/nvidia/src/kernel/gpu/timer/timer_ostimer.c
@@ -1,3 +1,4 @@
 void f(void) {
     int x = 0;
+    pTmrEvent->super.pOSTmrCBdata = NULL;
 }
"""
PATCH_MODESET = """--- a/src/kapi.c
+++ b/src/kapi.c
@@ -1,3 +1,3 @@
 int kapi(void) {
-    return 0;
+    return 1;
 }
"""


def repo(base, old):
    """A copy of the scripts under test: build/ and kexts/NVRM (patches, the accel source) as build_rm620.sh expects."""
    r = base / ("repo-old" if old else "repo")
    for rel in ("build/build_rm620.sh", "build/build_kms620.sh", "build/accel_build.sh", "build/rm_defines.sh",
                "build/warn_ceiling.sh", "build/warnings.txt"):
        p = r / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if old and rel != "build/rm_defines.sh":
            text = subprocess.run(["git", "-C", str(ROOT), "show", f"{OLD}:{rel}"], capture_output=True, text=True)
            if text.returncode != 0:
                print(f"SKIP: the old scripts ({OLD}) are not in this clone: run from a full clone")
                sys.exit(2)
            p.write_text(text.stdout)
        elif not (old and rel == "build/rm_defines.sh"):
            shutil.copy2(ROOT / rel, p)
    (r / "kexts/NVRM/patches").mkdir(parents=True, exist_ok=True)
    (r / "kexts/NVRM/patches/600-root.patch").write_text(PATCH_ROOT)
    (r / "kexts/NVRM/patches/610-modeset.patch").write_text(PATCH_MODESET)
    (r / "kexts/NVRM/accel/re").mkdir(parents=True, exist_ok=True)
    (r / "kexts/NVRM/accel/nvrm-accel.cpp").write_text("int accel;\n")
    (r / "kexts/NVRM/accel/Info.plist").write_text("<plist/>\n")
    return r


class Env:
    def __init__(self, base, extra=""):
        self.base = base
        self.bin = base / "bin"
        self.bin.mkdir(parents=True, exist_ok=True)
        tool = self.bin / "standin"
        tool.write_text(STANDIN)
        tool.chmod(0o755)
        for n in ("id", "whoami", "hostname", "strings", "stat", "md5", "clang", "clang++", "xcrun"):
            (self.bin / n).unlink(missing_ok=True)
            os.symlink(tool, self.bin / n)
        (base / "sdk").mkdir(exist_ok=True)
        self.extra = extra

    def run(self, argv, flaglog=None, cwd=None):
        env = {"PATH": f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(self.base), "LC_ALL": "C",
               "NMT_ACCOUNT": ACCOUNT, "NMT_HOST": HOST, "NMT_SDK": str(self.base / "sdk"), "NMT_ARCHIVE_EXTRA": self.extra,
               "JOBS": "2", "NV_VERSION": "620.70.06", "TMPDIR": os.environ.get("TMPDIR", "/tmp")}
        if flaglog:
            env["NMT_FLAGLOG"] = str(flaglog)
        r = subprocess.run(argv, capture_output=True, text=True, env=env, cwd=cwd, timeout=300)
        return r.returncode, r.stdout + r.stderr


def rm_lines(work):
    p = work / "ogkm-620.70.06/src/nvidia/_out/Darwin_x86_64/compile_cmds.sh"
    return p.read_text().splitlines() if p.exists() else []


def kms_lines(work):
    p = work / "ogkm-620.70.06/src/nvidia-modeset/_out/Darwin_x86_64/compile_cmds.sh"
    return p.read_text().splitlines() if p.exists() else []


def rebuild_problems(base, old):
    """first build, again untouched, again with one source touched, and a fresh folder: complete lists, same flags"""
    r = repo(base, old)
    env = Env(base)
    src = base / "pristine"
    if not src.exists():
        pristine(src)
    p = []
    seen = {}
    for case in ("first", "again", "touched", "fresh"):
        work = base / ("work-fresh" if case == "fresh" else "work")
        work.mkdir(exist_ok=True)
        if case == "touched":
            time_bump(work / "ogkm-620.70.06/src/nvidia/src/libraries/libspdm/spdm.c")
            time_bump(work / "ogkm-620.70.06/src/nvidia-modeset/lib/dp.cpp")
        rc, out = env.run(["bash", str(r / "build/build_rm620.sh"), str(work), str(src)])
        if rc != 0:
            p.append(f"{case}: build_rm620.sh exit {rc}: {out[-400:]}")
            continue
        rc, out = env.run(["bash", str(r / "build/build_kms620.sh"), str(work)])
        if rc != 0:
            p.append(f"{case}: build_kms620.sh exit {rc}: {out[-400:]}")
        lines, klines = rm_lines(work), kms_lines(work)
        if len(lines) != len(RM_SOURCES) or not any(" -c arch/nvalloc/unix/src/osapi.c " in l for l in lines):
            p.append(f"{case}: compile_cmds.sh has {len(lines)} of {len(RM_SOURCES)} commands")
        if len(klines) != len(KMS_SOURCES):
            p.append(f"{case}: the modeset compile_cmds.sh has {len(klines)} of {len(KMS_SOURCES)} commands")
        d, rc, text = accel_defines_ogkm(env, r, work / "ogkm-620.70.06", f"{'old' if old else 'new'}-{case}")
        if d is None:
            p.append(f"{case}: accel_build.sh exit {rc}: {text[-300:]}")
        seen[case] = d
    want = seen.get("first")
    for case, d in seen.items():
        if d is not None and want is not None and d != want:
            p.append(f"{case}: accel_build.sh got other -D flags than after the first build: +{sorted(set(d) - set(want))} -{sorted(set(want) - set(d))}")
    return p, seen


def accel_defines_ogkm(env, r, ogkm, tag):
    log = env.base / f"flags-{tag}"
    out = env.base / f"accel-{tag}"
    argv = ["env", f"OGKM={ogkm}", "bash", str(r / "build/accel_build.sh"), str(r / "kexts/NVRM"), str(out)]
    rc, text = env.run(argv, flaglog=log)
    f = Path(f"{log}.nvrm-accel.o")
    if rc != 0 or not f.exists():
        return None, rc, text
    return [a for a in f.read_text().splitlines() if a.startswith("-D")], rc, text


def time_bump(path):
    st = path.stat()
    os.utime(path, (st.st_atime + 3600, st.st_mtime + 3600))


def truncated_problems(base, old):
    """a list without the osapi.c command, and one whose command lacks the RM's defines: the readers stop"""
    r = repo(base, old)
    env = Env(base)
    src = base / "pristine"
    if not src.exists():
        pristine(src)
    work = base / "work-cut"
    work.mkdir(exist_ok=True)
    rc, out = env.run(["bash", str(r / "build/build_rm620.sh"), str(work), str(src)])
    if rc != 0:
        return [f"build_rm620.sh exit {rc}: {out[-300:]}"]
    cc = work / "ogkm-620.70.06/src/nvidia/_out/Darwin_x86_64/compile_cmds.sh"
    full = cc.read_text().splitlines()
    p = []
    for what, text in (("only the libspdm command", [l for l in full if "spdm.c" in l]),
                       ("an osapi.c command without -DNV_DARWIN", [l.replace(" -DNV_DARWIN", "") for l in full if "osapi.c" in l])):
        cc.write_text("\n".join(text) + "\n")
        d, rc, out = accel_defines_ogkm(env, r, work / "ogkm-620.70.06", f"cut-{'old' if old else 'new'}-{len(p)}-{what[:5]}")
        if d is not None or rc == 0:
            p.append(f"{what}: accel_build.sh compiled (exit {rc}) with {d}")
        elif "STOP:" not in out:
            p.append(f"{what}: accel_build.sh stopped without saying why: {out[-200:]}")
        if not old:
            rc2, out2 = env.run(["bash", str(r / "build/rm_defines.sh"), str(work / "ogkm-620.70.06/src/nvidia")])
            if rc2 == 0 or "STOP:" not in out2:
                p.append(f"{what}: rm_defines.sh exit {rc2}: {out2[-200:]}")
    cc.write_text("\n".join(full) + "\n")
    return p


def main():
    for t in ("make", "patch", "git"):
        if not shutil.which(t):
            print(f"SKIP: {t} is needed")
            return 2
    base = Path(tempfile.mkdtemp(prefix="rmlist-"))
    try:
        p, seen = rebuild_problems(base / "new", old=False)
        check("build_rm620.sh and build_kms620.sh again (untouched, one source touched) and in a fresh folder: complete lists, "
              "accel_build.sh gets the first build's -D flags", not p, p)
        first = seen.get("first") or []
        check("the -D flags are the osapi.c command's: no -DLIBSPDM_CONFIG, with -DNV_DARWIN and the PORT defines",
              "-DLIBSPDM_CONFIG" not in first and "-DNV_DARWIN" in first and "-DPORT_IS_KERNEL_BUILD=1" in first, first)
        negative("the scripts of " + OLD + " (an incremental make -n rewrites the list)", rebuild_problems(base / "old", old=True)[0])
        p = truncated_problems(base / "cut", old=False)
        check("a list without the osapi.c command or without its defines: rm_defines.sh and accel_build.sh stop with a message", not p, p)
        negative("accel_build.sh of " + OLD + " (head -1)", truncated_problems(base / "cut-old", old=True))
    finally:
        if os.environ.get("NMT_KEEP"):
            print(f"kept: {base}")
        else:
            shutil.rmtree(base, ignore_errors=True)
    print(f"{fails} failed" if fails else "all passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
