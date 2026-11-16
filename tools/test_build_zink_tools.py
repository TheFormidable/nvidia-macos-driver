#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""build/build_zink.sh stops before meson when the bison on its PATH is older than 3.0 (macOS's /usr/bin/bison is 2.3)
or missing, or when flex is missing, with a line that says why and names ./build.sh deps; Bison 3.0 or newer passes.

The script is run from a scratch copy of the tree whose build/prepare_zink.py is a stand-in: it names the commit of a
small git repository made here (the script compares the two first) and stops with STANDIN-PREPARE, the first thing the
script does after its checks. PATH holds stand-ins for meson, ninja, xcrun, codesign, flex and bison, and links to the
few real tools the script runs before that point, so this machine's own bison and flex are never found."""
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "build/build_zink.sh"
REAL = ["bash", "sh", "env", "git", "python3", "sed", "mkdir", "rm", "tar", "dirname", "cat", "head", "tr", "basename"]
FAILED = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"  [{detail}]"))
    if not ok:
        FAILED.append(name)


def main():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="nmzinktools-"))
    try:
        repo = tmp / "mesa.git"
        env0 = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp), "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env0)
        (repo / "VERSION").write_text("26.3.0\n")
        subprocess.run(["git", "-C", str(repo), "add", "VERSION"], check=True, env=env0)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "stand-in"], check=True, env=env0)
        commit = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, env=env0,
                                capture_output=True, text=True).stdout.strip()

        def tree(script_text):
            h = pathlib.Path(tempfile.mkdtemp(dir=tmp, prefix="tree-"))
            (h / "build").mkdir()
            (h / "build/build_zink.sh").write_text(script_text)
            (h / "build/prepare_zink.py").write_text(
                f"import sys\nMESA_COMMIT = '{commit}'\n"
                "if __name__ == '__main__':\n    print('STANDIN-PREPARE'); sys.exit(3)\n")
            return h

        def run(h, bison=None, flex=True):
            """build_zink.sh with a PATH of stand-ins; bison: the first line its --version prints, '' for a bison that
            prints nothing, None for no bison"""
            b = pathlib.Path(tempfile.mkdtemp(dir=tmp, prefix="bin-"))
            for t in REAL:
                p = shutil.which(t)
                if p:
                    (b / t).symlink_to(p)
            tools = ["meson", "ninja", "xcrun", "codesign"] + (["flex"] if flex else [])
            for t in tools:
                (b / t).write_text("#!/bin/sh\necho stand-in\n")
            if bison is not None:
                (b / "bison").write_text("#!/bin/sh\n" + (f"echo '{bison}'\n" if bison else "") + "exit 0\n")
            for t in tools + (["bison"] if bison is not None else []):
                (b / t).chmod(0o755)
            env = dict(env0, PATH=str(b))
            r = subprocess.run(["bash", str(h / "build/build_zink.sh"), str(repo), commit, str(tmp / "out")],
                               env=env, capture_output=True, text=True, timeout=60)
            return r.returncode, r.stdout + r.stderr

        h = tree(SCRIPT.read_text())
        for bv in ("bison (GNU Bison) 3.8.2", "bison (GNU Bison) 3.0", "bison (GNU Bison) 10.1"):
            rc, out = run(h, bv)
            v = bv.split()[-1]
            check(f"Bison {v} passes the check: build_zink.sh goes on to the tree and names the bison it found",
                  rc == 3 and "STANDIN-PREPARE" in out and f"is Bison {'.'.join(v.split('.')[:2])}; flex" in out, (rc, out))
        for bv, words in (("bison (GNU Bison) 2.3", "is Bison 2.3; Mesa's grammars need 3.0 or newer (./build.sh deps builds one)"),
                          ("bison (GNU Bison) 2.10", "is Bison 2.10; Mesa's grammars need 3.0 or newer"),
                          ("", "does not say which Bison it is"),
                          (None, "bison not found: native OpenGL needs Bison 3.0 or newer (./build.sh deps builds one)")):
            rc, out = run(h, bv)
            what = f"Bison {bv.split()[-1]}" if bv else ("a bison that prints no version" if bv == "" else "no bison")
            check(f"{what}: build_zink.sh stops before the tree and meson, saying why",
                  rc == 1 and "STOP: " in out and words in out and "STANDIN-PREPARE" not in out, (rc, out))
        rc, out = run(h, "bison (GNU Bison) 3.8.2", flex=False)
        check("no flex: build_zink.sh stops before the tree, saying so", rc == 1 and "STOP: flex not found" in out
              and "STANDIN-PREPARE" not in out, (rc, out))

        # control: the check as it was (bison and flex only looked up by name) lets macOS's 2.3 through
        text = SCRIPT.read_text()
        start = text.index('command -v flex >/dev/null || stop "flex not found')
        end = text.index('echo "zink: $bison is Bison $bv; flex $(command -v flex)"\n') + len('echo "zink: $bison is Bison $bv; flex $(command -v flex)"\n')
        old = text[:start] + 'for t in bison flex; do command -v "$t" >/dev/null || stop "$t not found"; done\n' + text[end:]
        rc, out = run(tree(old), "bison (GNU Bison) 2.3")
        check("control: a build_zink.sh that only looks bison up by name lets Bison 2.3 reach the tree",
              rc == 3 and "STANDIN-PREPARE" in out, (rc, out))
        check("build/build_zink.sh's BISON_MIN is 3.0, the NM_PIN_TOOL_BISON_MIN of build/pins.sh",
              "\nBISON_MIN=3.0\n" in text and "\nNM_PIN_TOOL_BISON_MIN=3.0\n" in (ROOT / "build/pins.sh").read_text())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"{len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
