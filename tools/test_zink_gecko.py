#!/usr/bin/env python3
"""Since 3.0.20 the OpenGL of Firefox and of the other browsers built on Gecko stays on Apple's renderer over the Metal
plugin instead of Zink (zink/nvk_gecko.h says why: Gecko binds an IOSurface with CGLTexImageIOSurface2D for everything
it draws, and in a Zink context each of those is copied in and out with a wait for the GPU, so the preview of one's own
screen share in a video call crawled). NVK_ZINK_GECKO_APPLE=0 in the settings file gives them Zink again, as in 3.0.19.

  the decision  zink/nvk_gecko.h, as it is, built on this machine: a loaded image whose last path component is exactly
                XUL makes a Gecko process (Firefox's and plugin-container's paths, LibreWolf's, a bare name); a missing
                name, another library, a suffix or a prefix of XUL, and a folder called XUL do not.
  the adapter   nvk_cgl_prepare_gld leaves a Gecko process's context with Apple right after OBS, before anything is
                read or opened; the switch is read with the adapter's other switch, once, and is asked before the
                images are looked at; the registry row, CHANGELOG, README and docs/ZINK-OPENGL.md.
  controls      copies of the header with one rule broken each. Every one has to fail.

It touches no GPU and loads no Apple framework.
usage: test_zink_gecko.py"""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEADER = ROOT / "zink" / "nvk_gecko.h"
FIXTURE = r'''
#include "nvk_gecko.h"
#include <stdio.h>
static const char *const *names;
static const char *name(unsigned i) { return names[i]; }
static int process(const char *const *list, unsigned count) { names = list; return nvk_gecko_process(count, name); }
int main(void)
{
   int bad = 0;
#define EXPECT(c, what) do { if (!(c)) { printf("FAIL %s\n", what); bad = 1; } } while (0)
   EXPECT(nvk_gecko_image("/Applications/Firefox.app/Contents/MacOS/XUL"), "Firefox's XUL");
   EXPECT(nvk_gecko_image("/Applications/LibreWolf.app/Contents/MacOS/XUL"), "LibreWolf's XUL");
   EXPECT(nvk_gecko_image("XUL"), "a bare XUL");
   EXPECT(!nvk_gecko_image(NULL), "no name");
   EXPECT(!nvk_gecko_image(""), "an empty name");
   EXPECT(!nvk_gecko_image("/usr/lib/libSystem.B.dylib"), "another library");
   EXPECT(!nvk_gecko_image("/Applications/X.app/Contents/MacOS/libXUL.dylib"), "a name that ends in XUL.dylib");
   EXPECT(!nvk_gecko_image("/Applications/X.app/Contents/MacOS/XULRunner"), "a name that starts with XUL");
   EXPECT(!nvk_gecko_image("/Applications/X.app/Contents/MacOS/xul"), "lower case");
   EXPECT(!nvk_gecko_image("/opt/XUL/"), "a folder called XUL");
   static const char *const firefox[] = { "/usr/lib/dyld", NULL,
      "/Applications/Firefox.app/Contents/MacOS/plugin-container.app/Contents/MacOS/plugin-container",
      "/Applications/Firefox.app/Contents/MacOS/XUL" };
   static const char *const other[] = { "/usr/lib/dyld", "/Applications/Minecraft.app/Contents/MacOS/launcher", NULL };
   EXPECT(process(firefox, 4), "Firefox's GPU process, an unloaded image on the way");
   EXPECT(!process(firefox, 3), "the images before XUL");
   EXPECT(!process(other, 3), "another application");
   EXPECT(!process(other, 0), "no image");
   if (!bad) printf("ALL CASES PASSED\n");
   return bad;
}
'''

# Each control breaks one rule of the header; the fixture has to fail on every one.
CONTROLS = [
    ("the last component is compared by its prefix", 'return !strcmp(last ? last + 1 : path, "XUL");',
     'return !strncmp(last ? last + 1 : path, "XUL", 3);'),
    ("XUL anywhere in the last component counts", 'return !strcmp(last ? last + 1 : path, "XUL");',
     'return strstr(last ? last + 1 : path, "XUL") != NULL;'),
    ("a missing name counts", "if (!path) return 0;", "if (!path) return 1;"),
    ("only the first image is looked at", "for (unsigned i = 0; i < count; ++i)", "for (unsigned i = 0; i < count && i < 1; ++i)"),
]

failed = 0


def check(what, ok, detail=""):
    global failed
    print(("ok   " if ok else "FAIL ") + what + ("" if ok or not detail else f"\n     {detail}"))
    if not ok:
        failed += 1


def run_fixture(header_text, tmp):
    folder = Path(tempfile.mkdtemp(dir=tmp))
    (folder / "nvk_gecko.h").write_text(header_text)
    (folder / "fixture.c").write_text(FIXTURE)
    cc = os.environ.get("CC", "cc")
    build = subprocess.run([cc, "-std=c11", "-Wall", "-Wextra", "-Werror", "-I", str(folder), str(folder / "fixture.c"),
                            "-o", str(folder / "fixture")], capture_output=True, text=True)
    if build.returncode:
        return "build", build.stderr
    out = subprocess.run([str(folder / "fixture")], capture_output=True, text=True, timeout=60)
    return ("pass" if out.returncode == 0 and "ALL CASES PASSED" in out.stdout else "fail"), out.stdout


def function(text, head):
    start = text.find(head)
    return text[start:text.index("\n}\n", start) + 3] if start >= 0 else ""


def main():
    header = HEADER.read_text()
    with tempfile.TemporaryDirectory() as tmp:
        verdict, out = run_fixture(header, tmp)
        check("the header decides as described (fixture built with -Wall -Wextra -Werror)", verdict == "pass", out)
        for what, old, new in CONTROLS:
            if header.count(old) != 1:
                check(f"control ({what}): its text is in the header once", False, old)
                continue
            verdict, _ = run_fixture(header.replace(old, new), tmp)
            check(f"control, the header with one rule broken ({what}): the fixture fails", verdict == "fail", verdict)

    cgl = (ROOT / "zink" / "nvk_cgl.c").read_text()
    prepare = function(cgl, "EXPORT int nvk_cgl_prepare_gld(void *engine, void *driver)\n{")
    spots = [prepare.find(s) for s in ("if (nvk_cgl_obs_compatibility_enabled()) return 0;",
                                       "if (gecko_stays_apple()) return 0;",
                                       "if (native_surface_copy_context()) return 0;",
                                       "nvk_cgl_check_gld_layout(", "nvk_zink_screen_ready()")]
    check("prepare_gld leaves a Gecko process's context with Apple after OBS and before any check, read or device",
          -1 not in spots and spots == sorted(spots) and prepare.count("gecko_stays_apple()") == 1, str(spots))
    reader = function(cgl, "static void read_cgl_switches(void)\n{")
    check("the switch is read once, with the adapter's other switch, by its name",
          cgl.count('nvk_zink_cfg_on("NVK_ZINK_GECKO_APPLE")') == 1 and 'gecko_apple = nvk_zink_cfg_on("NVK_ZINK_GECKO_APPLE");' in reader
          and 'opacity_keep = nvk_zink_cfg_on("NVK_ZINK_OPACITY_KEEP");' in reader
          and cgl.count("pthread_once(&cgl_switches_once, read_cgl_switches);") == 2)
    asks = function(cgl, "static int gecko_stays_apple(void)\n{")
    order = [asks.find(s) for s in ("pthread_once(&cgl_switches_once, read_cgl_switches);", "if (!gecko_apple) return 0;",
                                    "pthread_once(&gecko_once, find_gecko);", "return gecko;")]
    check("the switch is asked before the loaded images are looked at, and they are looked at once",
          -1 not in order and order == sorted(order) and cgl.count("nvk_gecko_process(") == 1
          and "nvk_gecko_process(_dyld_image_count(), loaded_image)" in cgl, str(order))

    rows = [l.split("\t") for l in (ROOT / "tools" / "switches.tsv").read_text().splitlines()
            if l.startswith("env\tNVK_ZINK_GECKO_APPLE\t")]
    check("tools/switches.tsv has an escape row for NVK_ZINK_GECKO_APPLE since 3.0.20",
          len(rows) == 1 and rows[0][2] == "escape" and rows[0][4] == "3.0.20" and "NVK_ZINK_GECKO_APPLE=0" in rows[0][3])
    top = re.search(r"^## 3\.0\.20\n(.*?)^## ", (ROOT / "CHANGELOG.md").read_text(), re.S | re.M)
    check("CHANGELOG.md's 3.0.20 section names the way back", bool(top) and "NVK_ZINK_GECKO_APPLE=0" in top.group(1))
    check("README.md's table of ways back has the row",
          "| `NVK_ZINK_GECKO_APPLE=0` | settings line |" in (ROOT / "README.md").read_text())
    check("docs/ZINK-OPENGL.md says Gecko browsers stay on Apple's OpenGL and how to change it",
          "NVK_ZINK_GECKO_APPLE=0" in (ROOT / "docs" / "ZINK-OPENGL.md").read_text())

    print(f"{failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
