"""The native OpenGL rows of tools/card_selftest.py: OpenGL contexts on the installed NVKZinkGLDriver.bundle, which
forwards to Apple's renderer and hands each hardware context to the Zink adapter in /Library/GPUBundles/nvmtl/zink.

Two programs, one subcommand per process, so that one crash or hang ends one row only:
  tools/card/zink_gl_check.m   offscreen CGL contexts: the path a context takes, renderer information, twelve
                               profile and pixel-format rows, offscreen rendering, legacy contexts, IOSurface textures,
                               shared contexts, threads, destroy orders (also under MallocScribble) and, last of all,
                               a context that migrates between threads
  tools/card/zink_win_check.m  AppKit windows: presentation through the adapter's CAMetalLayer, checked by glReadPixels
                               before each flush and by the adapter's present observer on every presented pixel;
                               resize, swap interval and opacity, flushes of a context that is not current, front-buffer
                               presentation, a render thread, clearDrawable, LWJGL's off-main calls (with
                               tools/card/zink_lwjgl_stub.m built as liblwjgl.dylib) and window formats (information)
Both include tools/card/zink_common.h. Every program follows the conventions of tools/card: exit 0 pass, 1 fail, 2
setup error, 3 skip, 4 inactive (the adapter's layout guard kept a hardware context Apple's; any other reason is a
failure), one line per case and a RESULT line. Rows that need Zink say "zink"; the software-renderer controls (Apple's
software renderer in the same program, which never reaches the bundle) do not. glbench (tools/glbench/glbench.c) runs
as a smoke row and a number. The migration probe may crash by design of the code it probes, so it runs last, in its
own process.

Every row builds its own copy of its program into {BIN}/<id>/nvmtltest, the name the plugin always grants a device.
These rows have run once, on one Mac, on macOS 26.7.3 (25G342) with release 3.0.1's package."""

_CC = ["/usr/bin/xcrun", "clang", "-arch", "x86_64", "-mmacosx-version-min=15.0"]
_WARN = ["-fobjc-arc", "-O1", "-g", "-Wall", "-Wextra", "-Wno-unused-parameter"]
_MAP = "{GB}/nvmtl/zink/cgl-dispatch-map.json"
_GL_SOURCES = ("tools/card/zink_gl_check.m", "tools/card/zink_common.h")
_WIN_SOURCES = ("tools/card/zink_win_check.m", "tools/card/zink_common.h", "zink/nvk_zink.h")
_GL_FRAMEWORKS = ("Foundation", "OpenGL", "IOSurface", "Metal", "CoreGraphics")
_WIN_FRAMEWORKS = ("Cocoa", "OpenGL", "QuartzCore", "Metal", "IOSurface", "CoreGraphics", "Foundation")
# the driver's own lines: zink/gld_bridge.c, zink/nvk_cgl.c, zink/nvk_zink.c, zink/native_drawable.m, and Mesa's
# errors, which Mesa 26.3.0 src/util/log.c:139-141 and :265-268 write as "MESA: error: ..."
_FORBID = (r"^NVKZinkGLDriver: ", r"^NVK Zink: ", r"^MESA: error: ")


def _compile(cid, source, flags, frameworks, output="nvmtltest"):
    argv = _CC + _WARN + list(flags) + ["{SRC}/" + source, "-o", "{BIN}/%s/%s" % (cid, output)]
    for f in frameworks:
        argv += ["-framework", f]
    return {"argv": argv, "limit": 300}


def _row(cid, order, title, program, args, alarm, needs, window=False, env=None, extra_build=(), extra_sources=()):
    """a row of one of the two programs; alarm is the program's own (zink_gl_check.m and zink_win_check.m main)"""
    if program == "gl":
        sources, build = list(_GL_SOURCES), [_compile(cid, "tools/card/zink_gl_check.m", (), _GL_FRAMEWORKS)]
    else:
        sources = list(_WIN_SOURCES) + list(extra_sources)
        build = list(extra_build) + [_compile(cid, "tools/card/zink_win_check.m",
                                              ("-I", "{VKINC}", "-iquote", "{SRC}/zink"), _WIN_FRAMEWORKS)]
    row = {"id": cid, "part": "zink", "title": title, "kind": "gpu", "order": order, "sources": sources,
           "build": build, "argv": ["{BIN}/%s/nvmtltest" % cid] + list(args), "limit": alarm + 30,
           "needs": list(needs), "expect": {"rc": 0, "stdout": [r"^RESULT pass "], "stderr_forbid": list(_FORBID)},
           "rc_map": {3: "skip", 4: "inactive"}, "info": [r"^INFO "]}
    if window:
        row["window"] = True
    if env:
        row["env"] = dict(env)
    return row


def _gl(cid, order, title, args, alarm=20, zink=True, env=None):
    needs = ["zink"] if zink else []
    if "--map" in args:
        needs.append("file:" + _MAP)
    return _row(cid, order, title, "gl", args, alarm, needs, env=env)


def _win(cid, order, title, args, zink=True, extra_build=(), extra_sources=()):
    needs = ["gui", "zink", "vkinc"] if zink else ["gui", "vkinc"]
    return _row(cid, order, title, "win", args, 30, needs, window=True, extra_build=extra_build,
                extra_sources=extra_sources)


_SOFT = ("--renderer", "software")
_SCRIBBLE = {"MallocScribble": "1", "MallocPreScribble": "1"}   # no MallocStackLogging: it writes to /private/tmp

CHECKS = [
    _gl("zink.path", 600, "a hardware context is Zink's: strings, adapter dispatch and the images that serve it",
        ["path", "--map", _MAP]),
    _gl("zink.path-software", 601, "control: a software-renderer context in the same program stays Apple's",
        ["path"] + list(_SOFT), zink=False),
    _gl("zink.rinfo", 602, "renderer information through the forwarding bundle: one accelerated online renderer",
        ["rinfo"]),
]

_order = 604
for _p in ("legacy", "gl3", "gl4"):
    for _b in ("db", "sb"):
        for _a in ("alpha", "noalpha"):
            CHECKS.append(_gl("zink.profile-%s-%s-%s" % (_p, _b, _a), _order,
                              "%s profile, %s, %s: strings, default framebuffer, FBO draw, extensions against the "
                              "installed map" % (_p, {"db": "double-buffered", "sb": "single-buffered"}[_b],
                                                  {"alpha": "with alpha", "noalpha": "without alpha"}[_a]),
                              ["profile", _p, _b, _a, "--map", _MAP]))
            _order += 1

CHECKS += [
    _gl("zink.offscreen", 620, "offscreen: depth test, readback, PBO, fence, texture, CGLFlushDrawable without a "
        "drawable", ["offscreen"]),
    _gl("zink.offscreen-software", 621, "control: the offscreen check on Apple's software renderer",
        ["offscreen"] + list(_SOFT), zink=False),
    _gl("zink.legacy", 622, "legacy context: display list, GLSL 1.20, GL_MAJOR_VERSION, Apple's VAO names",
        ["legacy"]),
    _gl("zink.iosurface", 630, "CGLTexImageIOSurface2D: read, write-back, refresh on rebind, RGBA16F and R8, errors",
        ["iosurface"], alarm=30),
    _gl("zink.iosurface-software", 631, "control: the IOSurface check on Apple's software renderer",
        ["iosurface"] + list(_SOFT), alarm=30, zink=False),
]
for _i, (_s, _t) in enumerate((("a", "a shared context sees a texture and a buffer of its partner"),
                               ("b", "an unshared context sees nothing of another, in either direction"),
                               ("c", "a shared context made current first still shares with its partner"),
                               ("d", "a shared context first used after its partner was destroyed (known "
                                     "difference by code reading)"))):
    CHECKS.append(_gl("zink.share-" + _s, 640 + _i, "share (%s): %s" % (_s, _t), ["share", _s]))
    CHECKS.append(_gl("zink.share-%s-software" % _s, 644 + _i, "control: share (%s) on Apple's software renderer" % _s,
                      ["share", _s] + list(_SOFT), zink=False))
CHECKS += [
    _gl("zink.threads-a", 650, "four threads, one context each, 200 frames, no cross-talk", ["threads", "a"],
        alarm=30),
    _gl("zink.threads-b", 651, "50 fence handoffs between shared contexts on two threads", ["threads", "b"],
        alarm=30),
]
for _i, (_s, _t) in enumerate((("a", "a context released on one thread and destroyed on another"),
                               ("b", "a context destroyed while current"),
                               ("c", "a context whose thread exited with it current, and the process's exit"))):
    CHECKS.append(_gl("zink.destroy-" + _s, 660 + _i, "destroy (%s): %s" % (_s, _t), ["destroy", _s]))
for _i, (_s, _t) in enumerate((("a", "released on one thread, destroyed on another"), ("b", "destroyed while current"),
                               ("c", "thread exited with it current"))):
    CHECKS.append(_gl("zink.destroy-%s-scribble" % _s, 663 + _i,
                      "destroy (%s) under MallocScribble and MallocPreScribble: %s" % (_s, _t), ["destroy", _s],
                      env=_SCRIBBLE))

# glbench as it is: a smoke row and a number. A context that is not Zink's makes the row inactive; zink.path says why.
# A GL_RENDERER of (null) is not that case: glGetString gave 0, as the adapter's calls do when it drops them in a
# context it took, so the row keeps glbench's own exit status and fails.
# glbench has no crash handlers of its own; zink_crash_guard.h, included by the build, turns a crash into exit 70
# and a CRASH line on standard error, so that macOS writes no crash report for it.
CHECKS.append({
    "id": "zink.glbench", "part": "zink", "title": "glbench 4000 draws x 20 frames on Zink: no GL error (smoke, and a "
    "number)", "kind": "gpu", "order": 690, "sources": ["tools/glbench/glbench.c", "tools/card/zink_crash_guard.h"],
    "build": [{"argv": _CC + ["-O2", "-Wall", "-include", "{SRC}/tools/card/zink_crash_guard.h",
                              "{SRC}/tools/glbench/glbench.c", "-o", "{BIN}/zink.glbench/nvmtltest",
                              "-framework", "OpenGL"], "limit": 300}],
    "argv": ["/bin/sh", "-c",
             'out=$("$1" 4000 20); rc=$?\n'
             'case "$out" in\n'
             '"GL_RENDERER zink Vulkan "*) printf "%s\\n" "$out"; exit "$rc" ;;\n'
             '"GL_RENDERER (null)"*) printf "%s\\n" "$out"; exit "$rc" ;;\n'
             '"GL_RENDERER "*) r=$(printf "%s\\n" "$out" | sed -n "1s/^GL_RENDERER //p")\n'
             '   printf "INACTIVE: the context is not Zink\'s (GL_RENDERER %s); zink.path says why\\n" "$r"\n'
             '   printf "%s\\n" "$out"; exit 4 ;;\n'
             'esac\n'
             'printf "%s\\n" "$out"; exit "$rc"\n',
             "glbench", "{BIN}/zink.glbench/nvmtltest"],
    "limit": 120, "needs": ["zink"],
    "expect": {"rc": 0, "stdout": [r"^GL_RENDERER zink Vulkan .*\(MESA_NVK\)", r"glGetError 0x0  image "],
               "stderr_forbid": list(_FORBID)},
    "rc_map": {4: "inactive"}, "info": [r"^GL_RENDERER ", r"^GL_VERSION ", r"^draws/frame "]})

_STUB = [_compile("zink.win-lwjgl", "tools/card/zink_lwjgl_stub.m",
                  ("-dynamiclib", "-install_name", "@rpath/liblwjgl.dylib"), ("AppKit",), output="liblwjgl.dylib")]
CHECKS += [
    _win("zink.win-basic", 700, "an NSOpenGLView presents 60 frames through the adapter's CAMetalLayer, every pixel "
         "checked", ["basic"]),
    _win("zink.win-resize", 701, "the drawable and the layer follow two window resizes", ["resize"]),
    _win("zink.win-ctl", 702, "swap interval and opacity reach the layer; alpha forced where it must be", ["ctl"]),
    _win("zink.win-noncurrent", 703, "CGLFlushDrawable of a window context that is not current restores the binding",
         ["noncurrent"]),
    _win("zink.win-front", 704, "a single-buffered window presents on glFlush", ["front"]),
    _win("zink.win-thread", 705, "a render thread other than main presents 120 frames", ["thread"]),
    _win("zink.win-clear", 706, "clearDrawable and the context's destruction remove the layer", ["clear"]),
    _win("zink.win-lwjgl", 707, "setView:, update and clearDrawable from liblwjgl.dylib off main run on main",
         ["lwjgl", "--lwjgl", "{BIN}/zink.win-lwjgl/liblwjgl.dylib"], extra_build=_STUB,
         extra_sources=("tools/card/zink_lwjgl_stub.m",)),
    _win("zink.win-formats", 708, "multisample, float and depth-32 window formats: what Zink's default framebuffer "
         "gets (information)", ["formats"]),
    _win("zink.win-formats-software", 709, "control: the same window formats on Apple's software renderer "
         "(information)", ["formats"] + list(_SOFT), zink=False),
]
CHECKS.append(_gl("zink.migrate", 900, "probe, last: a context moves to a second thread while its first thread binds "
                  "another (3 runs)", ["migrate"], alarm=45))

del _order, _p, _b, _a, _i, _s, _t
