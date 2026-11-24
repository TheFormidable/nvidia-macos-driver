#!/usr/bin/env python3
"""Since 3.0.16 an OpenGL buffer upload on Zink that Mesa's threaded context would write through a synchronized mapping
goes through a staging buffer instead (tc_subdata_stage_instead and the option subdata_never_wait, added by
zink/mesa-zink-nvk.patch to u_threaded_context.h and .c; Zink sets the option on NVK on macOS from the frontend's switch
NVK_ZINK_SUBDATA_STAGE in zink/nvk_present.c). On the RTX 5050 the render thread of a modded Minecraft spent 19 to 25 %
of its time in that wait. None of it needs a card or a Mesa build to be checked, so this test runs without either:

  the decision   tc_subdata_stage_instead, as the patch writes it, built with stand-in flag values: a write of more
                 than 320 bytes that is not unsynchronized and not staged already is staged; an unsynchronized write is
                 staged only over a pending staging upload; a persistent mapping, a write the threaded context stages
                 already, the upload of a CPU storage and an excluded buffer never are; 320 bytes or fewer that are
                 synchronized are left to the queue.
  the call site  tc_buffer_subdata asks it after tc_improve_map_buffer_flags with the buffers it may not stage and the
                 same overlap test tc_buffer_map makes, drops every unsynchronized flag and sets DISCARD_RANGE, sends the
                 write to tc_buffer_map, and does not mark a staged write as a CPU storage upload (its range has to
                 become valid).
  the switch     Zink sets the option only on NVK, only on macOS, from nvk_zink_subdata_stage_enabled(); the frontend
                 reads NVK_ZINK_SUBDATA_STAGE with its other switches, once; the registry row, CHANGELOG, README and
                 STATUS.
  controls       copies of the patch or of the frontend with one rule broken each. Every one has to fail.

It touches no GPU and loads no Apple framework.
usage: test_zink_subdata_stage.py"""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / "zink" / "mesa-zink-nvk.patch"
PRESENT = ROOT / "zink" / "nvk_present.c"
CC = os.environ.get("CC", "cc")
fails = 0

# Stand-in values: any distinct bits do, the function only names them.
FIXTURE = r"""
#include <stdbool.h>
#include <stdio.h>
#define PIPE_MAP_READ (1u << 0)
#define PIPE_MAP_WRITE (1u << 1)
#define PIPE_MAP_DISCARD_RANGE (1u << 8)
#define PIPE_MAP_DIRECTLY (1u << 9)
#define PIPE_MAP_UNSYNCHRONIZED (1u << 10)
#define PIPE_MAP_PERSISTENT (1u << 13)
#define TC_TRANSFER_MAP_UPLOAD_CPU_STORAGE (1u << 28)
#define TC_TRANSFER_MAP_NO_INFER_UNSYNCHRONIZED (1u << 29)
#define TC_TRANSFER_MAP_NO_INVALIDATE (1u << 30)
#define TC_TRANSFER_MAP_THREADED_UNSYNC (1u << 31)
#define TC_MAX_SUBDATA_BYTES 320
@HELPER@
int main(void)
{
   const unsigned improved = PIPE_MAP_WRITE | TC_TRANSFER_MAP_NO_INVALIDATE | TC_TRANSFER_MAP_NO_INFER_UNSYNCHRONIZED;
   const unsigned mapped = improved | PIPE_MAP_DIRECTLY;
   const unsigned unsync = improved | PIPE_MAP_UNSYNCHRONIZED | TC_TRANSFER_MAP_THREADED_UNSYNC;
   struct { const char *what; unsigned usage, size; bool excluded, overlap, want; } c[] = {
      {"a mapped buffer the GPU may use, 4096 bytes", mapped, 4096, false, false, true},
      {"a mapped buffer, 321 bytes", mapped, 321, false, false, true},
      {"a mapped buffer, 320 bytes (queued as it is)", mapped, 320, false, false, false},
      {"an unsynchronized write over a pending staging upload", unsync, 4096, false, true, true},
      {"an unsynchronized write of 64 bytes over a pending staging upload", unsync, 64, false, true, true},
      {"an unsynchronized write with nothing pending over it", unsync, 4096, false, false, false},
      {"a write the threaded context stages already", improved | PIPE_MAP_DISCARD_RANGE, 4096, false, true, false},
      {"a persistent mapping", mapped | PIPE_MAP_PERSISTENT, 4096, false, false, false},
      {"the upload of a CPU storage", unsync | TC_TRANSFER_MAP_UPLOAD_CPU_STORAGE, 4096, false, true, false},
      {"an excluded buffer, synchronized", mapped, 4096, true, false, false},
      {"an excluded buffer, over a pending upload", unsync, 4096, true, true, false},
   };
   int bad = 0;
   for (unsigned i = 0; i < sizeof c / sizeof c[0]; i++) {
      bool got = tc_subdata_stage_instead(c[i].usage, c[i].size, c[i].excluded, c[i].overlap);
      printf("%s %s: staged=%d\n", got == c[i].want ? "ok  " : "FAIL", c[i].what, got);
      bad += got != c[i].want;
   }
   return bad != 0;
}
"""


def added(patch_text, path):
    """The text the patch adds or keeps in the hunks of one file, without the diff markers."""
    m = re.search(r"^\+\+\+ b/" + re.escape(path) + r"\n(.*?)(?=^--- a/|\Z)", patch_text, re.M | re.S)
    if not m:
        return ""
    return "\n".join(l[1:] for l in m.group(1).split("\n") if l[:1] in ("+", " "))


def flat(text):
    return re.sub(r"\s+", " ", text)


def helper_of(header):
    m = re.search(r"static inline bool\ntc_subdata_stage_instead\(.*?\n}\n", header, re.S)
    return m.group(0) if m else ""


def problems(patch_text, present_text, docs=True):
    """Every rule this test holds, as a list of the ones broken (empty = all hold)."""
    out = []
    header = added(patch_text, "src/gallium/auxiliary/util/u_threaded_context.h")
    source = flat(added(patch_text, "src/gallium/auxiliary/util/u_threaded_context.c"))
    zink = added(patch_text, "src/gallium/drivers/zink/zink_context.c")

    helper = helper_of(header)
    if not helper:
        out.append("the patch has no tc_subdata_stage_instead in u_threaded_context.h")
    else:
        with tempfile.TemporaryDirectory(prefix="subdata-") as t:
            src, exe = Path(t) / "f.c", Path(t) / "f"
            src.write_text(FIXTURE.replace("@HELPER@", helper))
            b = subprocess.run([CC, "-std=c11", "-Wall", "-Wextra", "-Wno-unused-parameter", "-Werror", str(src), "-o", str(exe)],
                               capture_output=True, text=True)
            if b.returncode:
                out.append("the decision does not build: " + (b.stdout + b.stderr)[-800:])
            else:
                r = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60)
                if r.returncode:
                    out.append("the decision: " + " | ".join(l for l in r.stdout.splitlines() if l.startswith("FAIL")))
    if "bool subdata_never_wait;" not in flat(header):
        out.append("threaded_context_options has no subdata_never_wait")

    ask = ("usage = tc_improve_map_buffer_flags(tc, tres, usage, offset, size); "
           "/* Write through a staging buffer where the mapping would wait.")
    call = ("bool staged = false; if (tc->options.subdata_never_wait && tc_subdata_stage_instead(usage, size, "
            "tres->is_user_ptr || tres->allow_cpu_storage || tres->cpu_storage || tres->b.clientptr_heap || "
            "(tres->b.flags & (PIPE_RESOURCE_FLAG_SPARSE | PIPE_RESOURCE_FLAG_UNMAPPABLE)), "
            "p_atomic_read(&tres->pending_staging_uploads) && "
            "util_ranges_intersect(&tres->pending_staging_uploads_range, offset, offset + size))) { "
            "usage &= ~(PIPE_MAP_UNSYNCHRONIZED | TC_TRANSFER_MAP_THREADED_UNSYNC | PIPE_MAP_THREAD_SAFE | "
            "PIPE_MAP_SHORT_LIVED); usage |= PIPE_MAP_DISCARD_RANGE; staged = true; }")
    branch = "size > TC_MAX_SUBDATA_BYTES || tres->cpu_storage || staged) {"
    cpu = ("if (!tres->cpu_storage && offset == 0 && size == resource->width0 && !staged) "
           "usage |= TC_TRANSFER_MAP_UPLOAD_CPU_STORAGE;")
    if ask not in source:
        out.append("tc_buffer_subdata does not decide right after tc_improve_map_buffer_flags")
    if call not in source:
        out.append("tc_buffer_subdata does not ask with the excluded buffers and the pending overlap, or does not "
                   "drop every unsynchronized flag and set DISCARD_RANGE")
    if branch not in source:
        out.append("a staged write does not go to tc_buffer_map")
    if cpu not in source:
        out.append("a staged write of the whole buffer is marked as a CPU storage upload")
    # the decision has to come before the mapping it replaces
    if source.find(ask) > source.find("tc_buffer_map(_pipe, resource, 0, usage, &box, &transfer);") > -1:
        out.append("the decision comes after the mapping")

    guard = re.search(r"#if defined\(__APPLE__\) && defined\(NVK_ZINK_NVK\)\n(.*?)#endif", zink, re.S)
    options = [g for g in re.finditer(r"#if defined\(__APPLE__\) && defined\(NVK_ZINK_NVK\)\n(.*?)#endif", zink, re.S)
               if ".max_texture_subdata_bytes" in g.group(1)]
    if not options or (".subdata_never_wait = screen->info.driver_props.driverID == VK_DRIVER_ID_MESA_NVK && "
                       "nvk_zink_subdata_stage_enabled(),") not in flat(options[0].group(1)):
        out.append("Zink does not set subdata_never_wait on NVK on macOS from nvk_zink_subdata_stage_enabled()")
    if not guard or not any("int nvk_zink_subdata_stage_enabled(void);" in g.group(1) for g in
                            re.finditer(r"#if defined\(__APPLE__\) && defined\(NVK_ZINK_NVK\)\n(.*?)#endif", zink, re.S)):
        out.append("zink_context.c does not declare nvk_zink_subdata_stage_enabled for macOS")

    reader = re.search(r"static void ahead_read_switches\(void\)\n\{(.*?)\n\}", present_text, re.S)
    if not reader or 'subdata_on = nvk_zink_cfg_on("NVK_ZINK_SUBDATA_STAGE");' not in reader.group(1):
        out.append("the frontend does not read NVK_ZINK_SUBDATA_STAGE with its other switches")
    if ("int nvk_zink_subdata_stage_enabled(void) { pthread_once(&ahead_once, ahead_read_switches); "
            "return subdata_on; }") not in flat(present_text):
        out.append("nvk_zink_subdata_stage_enabled does not read the switches once and answer it")

    if docs:
        row = [l.split("\t") for l in (ROOT / "tools/switches.tsv").read_text().splitlines()
               if l.startswith("env\tNVK_ZINK_SUBDATA_STAGE\t")]
        if not row or row[0][2] != "escape" or row[0][4] != "3.0.16" or "NVK_ZINK_SUBDATA_STAGE=0" not in row[0][3]:
            out.append("tools/switches.tsv has no escape row for NVK_ZINK_SUBDATA_STAGE since 3.0.16")
        log = (ROOT / "CHANGELOG.md").read_text()
        top = re.search(r"^## 3\.0\.16\n(.*?)(?=^## )", log, re.M | re.S)
        if not top or "NVK_ZINK_SUBDATA_STAGE=0" not in top.group(1):
            out.append("CHANGELOG.md has no 3.0.16 section with its way back")
        status = (ROOT / "docs/STATUS.md").read_text()
        if not re.search(r"OpenGL buffer uploads without a wait \(3\.0\.16\).*?\n\s+On the card only where the game did not wait",
                         status, re.S):
            out.append("docs/STATUS.md does not say 3.0.16 is not yet shown on a card removing the wait")
        if "| `NVK_ZINK_SUBDATA_STAGE=0` | settings line |" not in (ROOT / "README.md").read_text():
            out.append("README.md's table of ways back has no NVK_ZINK_SUBDATA_STAGE row")
    return out


def main():
    global fails
    patch_text, present_text = PATCH.read_text(), PRESENT.read_text()
    found = problems(patch_text, present_text)
    for p in found:
        print("FAIL " + p)
    if not found:
        print("ok   the decision, the call site, the switch and the documents")
    fails += len(found)

    controls = (
        ("the decision ignores the pending overlap", "patch",
         "+   if (usage & PIPE_MAP_UNSYNCHRONIZED)\n+      return pending_overlap;",
         "+   if (usage & PIPE_MAP_UNSYNCHRONIZED)\n+      return true;"),
        ("the decision stages small synchronized writes", "patch",
         "+   return size > TC_MAX_SUBDATA_BYTES;", "+   return true;"),
        ("the decision stages a write that is staged already", "patch",
         "+       usage & (PIPE_MAP_PERSISTENT | PIPE_MAP_DISCARD_RANGE |", "+       usage & (PIPE_MAP_PERSISTENT |"),
        ("a staged write keeps PIPE_MAP_UNSYNCHRONIZED", "patch",
         "+      usage &= ~(PIPE_MAP_UNSYNCHRONIZED | TC_TRANSFER_MAP_THREADED_UNSYNC |",
         "+      usage &= ~(TC_TRANSFER_MAP_THREADED_UNSYNC |"),
        ("a staged write stays out of tc_buffer_map", "patch",
         "+       tres->cpu_storage || staged) {", "+       tres->cpu_storage) {"),
        ("a staged write of the whole buffer counts as a CPU storage upload", "patch",
         "size == resource->width0 && !staged)", "size == resource->width0)"),
        ("Zink never sets the option", "patch",
         "+                                                        .subdata_never_wait = screen->info.driver_props.driverID == VK_DRIVER_ID_MESA_NVK &&\n",
         "+                                                        .subdata_never_wait = false &&\n"),
        ("the frontend does not read the switch", "present",
         '   subdata_on = nvk_zink_cfg_on("NVK_ZINK_SUBDATA_STAGE");\n', ""),
    )
    for what, which, old, new in controls:
        p, q = patch_text, present_text
        if which == "patch":
            ok_old = p.count(old) == 1
            p = p.replace(old, new)
        else:
            ok_old = q.count(old) == 1
            q = q.replace(old, new)
        if not ok_old:
            print(f"FAIL control '{what}': its text is not in the tree once")
            fails += 1
            continue
        broken = problems(p, q, docs=False)
        print(("ok   " if broken else "FAIL ") + f"control: {what}" + (f" -> {broken[0][:100]}" if broken else " passes"))
        fails += 0 if broken else 1

    print(f"{fails} failed" if fails else "all passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
