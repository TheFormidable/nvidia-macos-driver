#!/usr/bin/env python3
"""NVK's settings-file reader (nvmtl_cfg_getenv): the LAST line that begins with "NAME=" wins, as the Metal plugin's
own reader does. This matters only in a process that loads NVK without the plugin (a Vulkan program): in a Metal
process the plugin has already set the variable, so getenv wins before this reader runs. The reader is in
src/nouveau/vulkan/nvmtl_cfg.h, whose change this branch carries in nvk/nvk-channel-recover.patch.

The test reconstructs the header from the patch (one contiguous hunk), redirects its file path to one the test
controls WITHOUT changing the path the driver uses, builds a tiny C program around it, and feeds it conf files:

  - a shipped line above the user's own line          -> the user's line (the last) wins
  - a comment naming the variable above the user line  -> the comment does not count; the user's line wins
  - the name only as the tail of a longer name         -> not matched
  - a value at the very end of the file, no newline     -> matched
  - an empty file / no file at all                      -> no value

The negative control is the reader BEFORE this change (reconstructed from the same hunk): it takes the first match
and gives up at a comment, so it must get the first two cases wrong."""
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

R = pathlib.Path(__file__).resolve().parents[1]
PATCH = R / "nvk/nvk-channel-recover.patch"
CFG = "src/nouveau/vulkan/nvmtl_cfg.h"
REAL_PATH = '"/Library/GPUBundles/nvmtl/nvrm610.conf"'
fails = 0


def arm(name, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + name + (f" ({str(detail).strip()[-300:]})" if detail and not ok else ""))
    fails += not ok


def hunk_images(text, path):
    """The (before, after) image of `path` from its single hunk in the patch (context+'-' and context+'+')."""
    before, after, cur, inhunk = [], [], None, False
    for line in text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            cur, inhunk = line.rstrip("\n").split(" b/", 1)[1], False
        elif cur != path:
            continue
        elif line.startswith("@@"):
            inhunk = True
        elif not inhunk or line.startswith(("--- ", "+++ ", "index ", "new file mode", "\\")):
            continue
        elif line.startswith(" "):
            before.append(line[1:]); after.append(line[1:])
        elif line.startswith("-"):
            before.append(line[1:])
        elif line.startswith("+"):
            after.append(line[1:])
    return "".join(before), "".join(after)


HARNESS = r"""
#include <stdio.h>
#include <stdlib.h>
/* The reader's conf path is redirected here so the driver's own path is never touched. */
static const char *cfg_test_path(void) { return getenv("NVMTL_CFG_TEST_PATH"); }
#include "nvmtl_cfg.h"
int main(int argc, char **argv)
{
    if (argc < 2) return 2;
    const char *v = nvmtl_cfg_getenv(argv[1]);
    printf("%s\n", v ? v : "NULL");
    return 0;
}
"""


def build(header_text, d):
    """Write the header (its real path replaced by the test's) and the harness, compile, return the exe path or None."""
    cc = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
    if cc is None:
        return None
    (d / "nvmtl_cfg.h").write_text(header_text.replace(REAL_PATH, "cfg_test_path()"))
    (d / "h.c").write_text(HARNESS)
    exe = d / "h"
    b = subprocess.run([cc, "-std=c11", "-O1", "-Wall", "-Wextra", "-Werror", "-I", str(d), str(d / "h.c"),
                        "-o", str(exe)], capture_output=True, text=True)
    return exe if b.returncode == 0 else None


def lookup(exe, name, conf_text, d, write_file=True):
    """Run the built reader for `name` against a conf file (or no file when write_file is False)."""
    conf = d / "nvrm610.conf"
    if write_file:
        conf.write_text(conf_text)
    elif conf.exists():
        conf.unlink()
    env = {k: v for k, v in os.environ.items() if k not in ("NVK_CHANNEL_RECOVER", "NVMTL_CFG_TEST_PATH")}
    env["NVMTL_CFG_TEST_PATH"] = str(conf)
    r = subprocess.run([str(exe), name], capture_output=True, text=True, env=env)
    return r.stdout.strip()


CASES = [
    # name, conf text (None = no file), expected driver answer, is one of "the first two" the old reader must miss
    ("shipped line above the user's line -> the user's (last) line wins",
     "NVK_CHANNEL_RECOVER", "NVK_CHANNEL_RECOVER=1\n# --- my settings ---\nNVK_CHANNEL_RECOVER=0\n", "0", True),
    ("a comment naming the variable above the user's line does not count",
     "NVK_CHANNEL_RECOVER", "# set NVK_CHANNEL_RECOVER=9 to disable\nNVK_CHANNEL_RECOVER=0\n", "0", True),
    ("the name only as the tail of a longer name is not matched",
     "NVK_CHANNEL_RECOVER", "XNVK_CHANNEL_RECOVER=1\n", "NULL", False),
    ("a value at the very end of the file without a newline is read",
     "NVK_CHANNEL_RECOVER", "NVK_CHANNEL_RECOVER=7", "7", False),
    ("an empty file yields no value",
     "NVK_CHANNEL_RECOVER", "", "NULL", False),
    ("no file at all yields no value",
     "NVK_CHANNEL_RECOVER", None, "NULL", False),
]


def main():
    patch = PATCH.read_text() if PATCH.exists() else ""
    arm("nvk/nvk-channel-recover.patch exists", bool(patch))
    before, after = hunk_images(patch, CFG)
    arm("the patch reconstructs a single whole-file hunk of nvmtl_cfg.h (reader present in both images)",
        "nvmtl_cfg_getenv" in after and "nvmtl_cfg_getenv" in before and after != before)
    arm("the new reader keeps the last match that starts a line; the old reader took the first",
        "const char *last = NULL;" in after and "last = h;" in after and "const char *last" not in before)
    arm("the new reader documents the 8191-byte limit it keeps (differs from the plugin, which reads the file whole)",
        "first 8191 bytes" in after and "reads the file whole" in after)

    with tempfile.TemporaryDirectory() as td:
        d = pathlib.Path(td)
        exe = build(after, d)
        if exe is None:
            print("SKIP the host reader test: no C compiler")
            return fails
        for desc, name, conf, expect, _ in CASES:
            got = lookup(exe, name, conf, d, write_file=conf is not None)
            arm(f"driver reader: {desc}", got == expect, f"got {got!r}, expected {expect!r}")

        # negative control: the reader before this change must get the first two cases wrong.
        oldd = d / "old"
        oldd.mkdir()
        oldexe = build(before, oldd)
        arm("the old reader still builds (negative control)", oldexe is not None)
        if oldexe is not None:
            for desc, name, conf, expect, is_first_two in CASES:
                if not is_first_two:
                    continue
                got = lookup(oldexe, name, conf, oldd, write_file=conf is not None)
                arm(f"control: the old reader gets this wrong: {desc}", got != expect, f"old gave {got!r} (driver wants {expect!r})")
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
