#!/usr/bin/env python3
"""Since 3.0.8 a swap of the Zink frontend asks WindowServer at most once whether the drawable is opaque, and a context
keeps the answer for 100 ms (zink/nvk_opacity.h, present_drawable in zink/nvk_cgl.c). Through 3.0.7 every swap asked
twice, and in a capture of a modded Minecraft pack on 3.0.7 the two asks were 5.7 % of the game's render thread. A
mistake here composes a window with the wrong opacity for good, or asks as often as before, and neither can be seen
without a card. This test runs what can be run without one:

  the decision tools/zink_opacity_fixture.c drives nvk_opacity.h, as it is, with a clock and a reader of its own: the
               first swap asks; nothing is asked within 100 ms; at 100 ms one ask; a change of the output asks at once;
               with the switch off every swap asks, once; a failed ask answers opaque and is not kept; a clock that
               goes back or wraps cannot keep an answer for good. Again under AddressSanitizer and
               UndefinedBehaviorSanitizer where the compiler has them.
  controls     copies of the header with one rule broken each. Every one of them has to fail the fixture; a control
               that passes means the fixture no longer looks at that rule.
  the text     the CGL adapter asks in one place, once per swap, gives the layer and the copy the same answer, reads
               its switch once by name, and tells the kept answer that the output changed wherever it sees that. Then
               copies of nvk_cgl.c and native_drawable.m with one of those places broken each, every one of which has
               to fail the reading.

It touches no GPU and loads no Apple framework.
usage: test_zink_opacity.py"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ZINK = ROOT / "zink"
TOOLS = ROOT / "tools"
CC = os.environ.get("CC", "cc")
BASE = ["-std=gnu11", "-Wall", "-Wextra", "-Werror", "-g"]
fails = 0


def check(what, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + what + ("" if ok else f"\n     {detail}"))
    fails += 0 if ok else 1


def build(out, include, flags=()):
    done = subprocess.run([CC, *BASE, *flags, "-I", str(include), str(TOOLS / "zink_opacity_fixture.c"), "-o", str(out)],
                          capture_output=True, text=True)
    return done.returncode == 0, (done.stdout + done.stderr)[-3000:]


def run(binary, limit=60):
    try:
        done = subprocess.run([str(binary)], capture_output=True, text=True, timeout=limit)
        return done.returncode, done.stdout + done.stderr
    except subprocess.TimeoutExpired:
        return "timeout", ""


def function(text, head):
    start = text.index(head)
    return text[start:text.index("\n}\n", start) + 3]


def read_adapter(cgl, native, check):
    """The text: check(what, ok, detail="") for each rule of the adapter's code that this test reads."""
    check("the adapter asks for the opacity in one place",
          len(re.findall(r"kCGLCPSurfaceOpacity, &", cgl)) == 1 and "CGLGetParameter(context, kCGLCPSurfaceOpacity, &opacity)"
          in function(cgl, "static int read_opacity(void *context, int *opaque)\n{"))
    reader = function(cgl, "static int read_opacity(void *context, int *opaque)\n{")
    check("its reader starts from opaque and says whether CGL answered",
          "GLint opacity = 1;" in reader and "return error == kCGLNoError;" in reader)
    asks = function(cgl, "static int opaque_drawable(struct nvk_cgl_bridge *b)\n{")
    check("the switch is read once per process, by its name, and the kept answer is what a swap is given",
          cgl.count('nvk_zink_cfg_on("NVK_ZINK_OPACITY_KEEP")') == 1 and "pthread_once(&cgl_switches_once, read_cgl_switches);" in asks
          and "nvk_opacity_get(&b->opacity, opacity_keep, nvk_opacity_now_ns(), read_opacity, b->context)" in asks)
    check("nothing but opaque_drawable asks", cgl.count("read_opacity, ") == 1 and cgl.count("opaque_drawable(") == 3,
          f"{cgl.count('opaque_drawable(')} mentions of opaque_drawable(")
    drawable = function(cgl, "static int present_drawable(CGLContextObj context)\n{")
    spots = [drawable.find(piece) for piece in ("int opaque = -1;", "opaque = opaque_drawable(b);",
                                                "nvk_native_acquire(context, vendor, device, b->width, b->height, opaque, ",
                                                "nvk_zink_present_frame(b->zink, surface, opaque, ",
                                                "return gpu_present(b, opaque);")]
    check("present_drawable asks once and gives the layer, the copy and the IOSurface path that one answer",
          -1 not in spots and spots == sorted(spots) and drawable.count("opaque_drawable(") == 1, str(spots))
    gpu = function(cgl, "static int gpu_present(struct nvk_cgl_bridge *b, int opaque)\n{")
    check("the IOSurface path asks only when the swap has not",
          "opaque >= 0 ? opaque : opaque_drawable(b)" in gpu and gpu.count("opaque_drawable(") == 1)
    check("a native acquire that gives no drawable makes the next swap ask, unless it again finds no view",
          "if (acquired != 1 && (acquired != NVK_NATIVE_NO_VIEW || !b->no_view)) nvk_opacity_changed(&b->opacity);" in drawable
          and drawable.find("nvk_opacity_changed(&b->opacity)") < drawable.find("if (acquired == 2) return 2;"))
    check("the swap then remembers whether it found no view, for the next swap to compare with",
          drawable.count("b->no_view = acquired == NVK_NATIVE_NO_VIEW;") == 1
          and drawable.find("nvk_opacity_changed(&b->opacity)") < drawable.find("b->no_view = acquired == NVK_NATIVE_NO_VIEW;")
          < drawable.find("if (acquired == 2) return 2;"))
    current = function(cgl, "static struct nvk_cgl_bridge *bridge_current(void *engine)\n{")
    check("a size the adapter applies makes the next swap ask",
          re.search(r"thread_bridge->width = width; thread_bridge->height = height;\n.*\n\s*nvk_opacity_changed\(&thread_bridge->opacity\);",
                    current) is not None
          and "nvk_opacity_changed(&b->opacity);" in function(cgl, "EXPORT int nvk_cgl_resize("))
    check("every bridge starts with nothing kept (calloc), so a context's first swap asks",
          cgl.count("struct nvk_cgl_bridge *b = calloc(1, sizeof(*b));") == 2 and "struct nvk_opacity opacity;" in cgl)
    hook = function(cgl, "static int iosurface_integer(void *engine, unsigned parameter, const int *values)\n{")
    check("the integer callback only marks the answer for parameter 236 and passes the call on as before",
          "if (b && parameter==(unsigned)kCGLCPSurfaceOpacity) nvk_opacity_changed(&b->opacity);" in hook
          and hook.find("nvk_opacity_changed") < hook.find("pthread_mutex_unlock(&bridges_lock);")
          < hook.find("if (parameter!=910) return original(engine,parameter,values);"))
    changed = function(cgl, "void nvk_cgl_output_changed(CGLContextObj context)\n{")
    check("the AppKit side's notice finds the bridge under the adapter's lock and marks the answer of the bridge it found",
          -1 < changed.find("pthread_mutex_lock(&bridges_lock);") < changed.find("find_bridge(context)")
          < changed.find("if (b) nvk_opacity_changed(&b->opacity);") < changed.find("pthread_mutex_unlock(&bridges_lock);"))
    bind = function(native, "void nvk_native_bind_view(CGLContextObj context, void *pointer)\n{")
    remove = function(native, "void nvk_native_remove(CGLContextObj context)\n{")
    differs = bind.find("if (target.view != view) {")
    check("a view binding that changes the view and a removed target make the next swap ask, outside the bindings' locks",
          bind.count("changed = YES;") == 1 and differs >= 0
          and "changed = YES;" in bind[differs:bind.find("\n         }\n", differs)]
          and bind.rstrip().endswith("if (changed) nvk_cgl_output_changed(context);\n}")
          and remove.find("nvk_cgl_output_changed(context);") > remove.rfind("@synchronized(table)") and
          remove.find("nvk_cgl_output_changed(context);") < remove.find("void (^remove)(void)"))


def main():
    if not shutil.which(CC):
        print(f"SKIP: no C compiler ({CC})")
        return 0
    header = (ZINK / "nvk_opacity.h").read_text()
    with tempfile.TemporaryDirectory() as folder:
        tmp = Path(folder)
        ok, out = build(tmp / "op", ZINK)
        check("the header and its fixture compile without a warning", ok, out)
        if ok:
            status, out = run(tmp / "op")
            check("the decision: every arm passes", status == 0 and "all arms passed" in out, out[-2500:])
        flags = ["-fsanitize=address,undefined", "-fno-sanitize-recover=all"]
        ok, out = build(tmp / "op_san", ZINK, flags)
        if not ok:
            print("note the decision under -fsanitize=address,undefined: not run, this compiler cannot")
        else:
            status, out = run(tmp / "op_san")
            check("the decision under -fsanitize=address,undefined: every arm passes and nothing is reported",
                  status == 0 and "all arms passed" in out and "runtime error" not in out and "Sanitizer" not in out,
                  out[-2500:])

        controls = [
            ("the answer is kept for good", "now_ns - o->asked_ns < NVK_OPACITY_KEEP_NS", "1"),
            ("the answer is kept one nanosecond too long", "now_ns - o->asked_ns < NVK_OPACITY_KEEP_NS",
             "now_ns - o->asked_ns <= NVK_OPACITY_KEEP_NS"),
            ("the age is measured with a signed difference, so a clock that went back keeps the answer",
             "now_ns - o->asked_ns < NVK_OPACITY_KEEP_NS", "(int64_t)(now_ns - o->asked_ns) < (int64_t)NVK_OPACITY_KEEP_NS"),
            ("the age is compared as two times, so a clock that wraps asks at every swap after the wrap",
             "now_ns - o->asked_ns < NVK_OPACITY_KEEP_NS", "now_ns < o->asked_ns + NVK_OPACITY_KEEP_NS && now_ns >= o->asked_ns"),
            ("a change of the output is not seen", "if (atomic_load(&o->changed) && atomic_exchange(&o->changed, 0)) o->kept = 0;\n", ""),
            ("a change of the output is seen at every swap after it",
             "atomic_exchange(&o->changed, 0)", "atomic_load(&o->changed)"),
            ("the switch is not looked at", "o->kept = answered && keep;", "o->kept = answered + 0 * keep;"),
            ("a failed ask is kept", "o->kept = answered && keep;", "o->kept = keep;"),
            ("a failed ask answers what the reader left", "o->opaque = !answered || opaque != 0;", "o->opaque = opaque != 0;"),
            ("a failed ask answers transparent", "o->opaque = !answered || opaque != 0;", "o->opaque = answered && opaque != 0;"),
            ("the time of the ask is not written", "   o->asked_ns = now_ns;\n", ""),
            ("a swap asks twice", "   const int answered = read(arg, &opaque);\n",
             "   const int answered = read(arg, &opaque) & read(arg, &opaque);\n"),
            ("the keeping lasts a second", "#define NVK_OPACITY_KEEP_NS 100000000ull", "#define NVK_OPACITY_KEEP_NS 1000000000ull"),
        ]
        for what, old, new in controls:
            if header.count(old) != 1:
                check(f"control '{what}': the line it breaks is in the header once", False, repr(old))
                continue
            where = tmp / "broken"
            where.mkdir(exist_ok=True)
            (where / "nvk_opacity.h").write_text(header.replace(old, new))
            ok, out = build(tmp / "op_broken", where)
            status, out = run(tmp / "op_broken") if ok else ("not built", out)
            check(f"control, the header with one rule broken ({what}): the fixture fails",
                  ok and status == 1, f"status {status}\n{out[-1500:]}")

    # ---- the text ------------------------------------------------------------------------------------------------
    cgl = (ZINK / "nvk_cgl.c").read_text()
    native = (ZINK / "native_drawable.m").read_text()
    read_adapter(cgl, native, check)
    # Copies of the adapter's code with one place broken each. Every one has to fail the reading above.
    adapter_controls = [
        ("the AppKit side's notice does not mark the answer", "nvk_cgl.c",
         [("   if (b) nvk_opacity_changed(&b->opacity);\n   pthread_mutex_unlock(&bridges_lock);",
           "   (void)b;\n   pthread_mutex_unlock(&bridges_lock);")]),
        ("a swap does not remember that it found no view", "nvk_cgl.c",
         [("      b->no_view = acquired == NVK_NATIVE_NO_VIEW;\n", "")]),
        ("only a new target makes the view binding ask, not another view or none", "native_drawable.m",
         [("            changed = YES;\n", ""),
          ("table[key] = target; }", "table[key] = target; changed = YES; }")]),
    ]
    for what, name, edits in adapter_controls:
        text = {"nvk_cgl.c": cgl, "native_drawable.m": native}
        missing = [old for old, _ in edits if text[name].count(old) != 1]
        if missing:
            check(f"adapter control '{what}': the code it breaks is in {name} once", False, repr(missing))
            continue
        for old, new in edits:
            text[name] = text[name].replace(old, new)
        failed = []
        read_adapter(text["nvk_cgl.c"], text["native_drawable.m"],
                     lambda rule, ok, detail="": None if ok else failed.append(rule))
        check(f"adapter control, {name} with one place broken ({what}): the reading fails", bool(failed))
    print("all passed" if not fails else f"{fails} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
