"""The native Vulkan runtime rows of tools/card_selftest.py: the frontend /Library/GPUBundles/nvmtl/minecraft/lib/
libnvk_minecraft.dylib (tools/nvk-bridge/bridge.mm) over its renderer libnvk_renderer.dylib, opened by its path as
Minecraft opens it and through a Vulkan loader with the runtime's nvk_bridge_icd.json.

Two programs, one mode per process, so that one crash or hang ends one row only:
  tools/nvk-bridge/smoke.mm         the existing smoke program: instance, device and the device features Minecraft
                                    needs (direct and through each loader), and its SDL3 window when a libSDL3.dylib
                                    is there
  tools/nvk-bridge/window_check.mm  an AppKit window of its own, without SDL3: what the frontend offers and hides, the
                                    Metal surface, the swapchain's refusals, presents in FIFO, IMMEDIATE sRGB and with 8
                                    images, a resize, a minimize and a restore, every presented copy read back through
                                    the frontend's present observer, a Metal blit while the Vulkan device lives, the
                                    frontend's counters, the full-BAR rule; and the renderer alone as a control
smoke.mm prints its own lines on stdout and the frontend's "NVK Bridge: " lines (and the loader's) go to stderr, which
the runner cannot require; its rows run it through /bin/sh with stderr merged into stdout (exec keeps its exit status
and signal), and smoke.mm line-buffers stdout so that the merged lines stay whole. window_check.mm reads its own
stderr and needs no merge. The loader rows keep the loader to the runtime: VK_DRIVER_FILES names its manifest, the
explicit and implicit layer paths point at an empty folder the row makes, implicit layers are disabled, and
VK_LOADER_DEBUG=driver makes the loader name the manifest it used.

The package's own files (the four files of the runtime's folder, their modes, the manifest, identities and libraries)
are the rows of tools/card/checks_install.py; there is no row here for them. There is no Java row: the probe of
README.md needs a JDK and the game's jars, which no "needs" can find, and a check never runs java.

Every row builds its own copy of its program into {BIN}/<id>/nvmtltest, the name the plugin always grants a device.
These rows have run once, on one Mac, on macOS 26.7.3 (25G342) with release 3.0.1's package."""

_CXX = ["/usr/bin/xcrun", "clang++", "-arch", "x86_64", "-mmacosx-version-min=15.0", "-std=c++17", "-fobjc-arc", "-O2"]
_DIR = "{GB}/nvmtl/minecraft"
_FRONTEND = _DIR + "/lib/libnvk_minecraft.dylib"
_RENDERER = _DIR + "/lib/libnvk_renderer.dylib"
_MANIFEST = _DIR + "/nvk_bridge_icd.json"
_INSTALLED_LOADER = "{GB}/nvmtl/libvulkan.1.dylib"
_BUILT_LOADER = "{B}/out/vulkan-loader/libvulkan.1.dylib"
_SDL = "/usr/local/lib/libSDL3.dylib"   # an x86_64 SDL3 where Homebrew on an Intel Mac links it
# The runtime's folder as the frontend and the loader may spell it: the loader joins the manifest's folder and its
# "./lib/libnvk_minecraft.dylib", and the frontend names its renderer from its own path (bridge.mm:173-188).
_LIB = r"/Library/GPUBundles/nvmtl/minecraft(?:/+\.)*/+lib/"
# the frontend's lines for a failed renderer load, a missing Metal device and an uncertain completion (bridge.mm:181,
# :409, :492, :986, :1028)
_FORBID = (r"^NVK Bridge: renderer load failed", r"^NVK Bridge: a matching NVIDIA Metal device",
           r"^NVK Bridge: presentation completion uncertain", r"^NVK Bridge: uncertain GPU completion")
_MERGED = 'exec "$0" "$@" 2>&1'   # sh -c: stderr into stdout, then the program in sh's place


def _build_smoke(cid):
    return {"argv": _CXX + ["-I{VKINC}", "{SRC}/tools/nvk-bridge/smoke.mm", "-framework", "Foundation",
                            "-o", "{BIN}/%s/nvmtltest" % cid], "limit": 300}


def _build_window(cid):
    argv = _CXX + ["-Wall", "-Wextra", "-Wno-missing-field-initializers", "-I{VKINC}",
                   "{SRC}/tools/nvk-bridge/window_check.mm", "-o", "{BIN}/%s/nvmtltest" % cid]
    for f in ("Foundation", "AppKit", "QuartzCore", "Metal", "IOSurface", "CoreGraphics"):
        argv += ["-framework", f]
    return {"argv": argv, "limit": 300}


def _no_layers(cid):
    """a build step that makes the empty folder the loader rows give as their layer paths"""
    return {"argv": ["/bin/sh", "-c", 'mkdir -p "$1"', "no-layers", "{BIN}/%s/no-layers" % cid], "limit": 10}


def _loader_env(cid):
    empty = "{BIN}/%s/no-layers" % cid
    return {"VK_DRIVER_FILES": _MANIFEST, "VK_LAYER_PATH": empty, "VK_IMPLICIT_LAYER_PATH": empty,
            "VK_LOADER_LAYERS_DISABLE": "~implicit~", "VK_LOADER_DEBUG": "driver"}


def _smoke(cid, order, title, entry, needs, stdout, info, env=None, extra=(), window=False, limit=60, build=()):
    row = {"id": cid, "part": "vulkan", "title": title, "kind": "gpu", "order": order,
           "sources": ["tools/nvk-bridge/smoke.mm"], "build": list(build) + [_build_smoke(cid)],
           "argv": ["/bin/sh", "-c", _MERGED, "{BIN}/%s/nvmtltest" % cid, entry] + list(extra), "limit": limit,
           "needs": list(needs), "expect": {"rc": 0, "stdout": list(stdout), "stderr_forbid": []},
           "rc_map": {}, "info": list(info)}
    if window:
        row["window"] = True
    if env:
        row["env"] = dict(env)
    return row


def _window(cid, order, title, args, needs, env=None, build=()):
    """a row of window_check.mm's windowed modes; its own alarm is 100 s (window_check.mm main)"""
    row = {"id": cid, "part": "vulkan", "title": title, "kind": "gpu", "order": order,
           "sources": ["tools/nvk-bridge/window_check.mm"], "build": list(build) + [_build_window(cid)],
           "argv": ["{BIN}/%s/nvmtltest" % cid] + list(args), "limit": 130, "needs": list(needs), "window": True,
           "expect": {"rc": 0, "stdout": [r"^PASS frames-fifo ", r"^PASS frames-resized ", r"^PASS frames-immediate ",
                                          r"^PASS frames-eight ", r"^PASS stats ", r"^PASS vram-import ",
                                          r"^PASS teardown ", r"^RESULT pass "],
                      "stderr_forbid": list(_FORBID)},
           "rc_map": {3: "skip"}, "info": [r"^INFO "]}
    if env:
        row["env"] = dict(env)
    return row


_SMOKE_DIRECT = [r"^API \d+\.\d+\.\d+$", r"^DEVICE .+ vendor=0x10de device=0x[0-9a-f]+$",
                 r"^PASS Minecraft required device features$",
                 r"^NVK Bridge: renderer=/Library/GPUBundles/nvmtl/minecraft/lib/libnvk_renderer\.dylib; "
                 r"presentation=gpu-copy; ",
                 r"^NVK Bridge: native device=.+; copy family=\d+; Metal registry=[1-9]\d*$",
                 r"^MAPPED /Library/GPUBundles/nvmtl/minecraft/lib/libnvk_minecraft\.dylib$",
                 r"^MAPPED /Library/GPUBundles/nvmtl/minecraft/lib/libnvk_renderer\.dylib$",
                 r"^PASS native device/API probe$"]
_SMOKE_LOADER = [r"^DEVICE .+ vendor=0x10de device=0x[0-9a-f]+$", r"^PASS Minecraft required device features$",
                 r"^NVK Bridge: renderer=" + _LIB + r"libnvk_renderer\.dylib; presentation=gpu-copy; ",
                 r"^NVK Bridge: native device=.+; copy family=\d+; Metal registry=[1-9]\d*$",
                 r"^MAPPED " + _LIB + r"libnvk_minecraft\.dylib$", r"^MAPPED " + _LIB + r"libnvk_renderer\.dylib$",
                 r"nvk_bridge_icd\.json", r"^PASS native device/API probe$"]
_SMOKE_INFO = [r"^API ", r"^DEVICE ", r"^NVK Bridge: (native device|presentation completion)="]


def _smoke_loader(cid, order, title, loader):
    return _smoke(cid, order, title, loader, ["file:" + loader, "file:" + _MANIFEST, "file:" + _FRONTEND],
                  _SMOKE_LOADER, _SMOKE_INFO, env=_loader_env(cid), build=[_no_layers(cid)])


def _window_loader(cid, order, title, loader):
    return _window(cid, order, title, ["loader", loader, _FRONTEND],
                   ["file:" + loader, "file:" + _MANIFEST, "file:" + _FRONTEND], env=_loader_env(cid),
                   build=[_no_layers(cid)])


# Orders: the headless rows before the zink part's (600-709), the windowed ones after them, so that a window row that
# times out (which stops the later GPU rows) leaves the zink rows run.
CHECKS = [
    {"id": "vulkan.renderer-control", "part": "vulkan", "kind": "gpu", "order": 500,
     "title": "the renderer alone, a control: which of the window-system extensions the frontend hides it offers",
     "sources": ["tools/nvk-bridge/window_check.mm"], "build": [_build_window("vulkan.renderer-control")],
     "argv": ["{BIN}/vulkan.renderer-control/nvmtltest", "renderer", _RENDERER], "limit": 50,
     "needs": ["file:" + _RENDERER],
     "expect": {"rc": 0, "stdout": [r"^PASS renderer-extensions ", r"^RESULT pass "], "stderr_forbid": []},
     "rc_map": {3: "skip"}, "info": [r"^INFO "]},
    _smoke("vulkan.smoke-direct", 510,
           "smoke.mm on the frontend by its path: instance, device, Minecraft's features, the renderer it loaded",
           _FRONTEND, ["file:" + _FRONTEND], _SMOKE_DIRECT, _SMOKE_INFO),
    _smoke_loader("vulkan.smoke-loader", 511,
                  "smoke.mm through the installed Vulkan loader with only the runtime's manifest", _INSTALLED_LOADER),
    _smoke_loader("vulkan.smoke-loader-built", 512,
                  "smoke.mm through the Vulkan loader of the build folder with only the runtime's manifest",
                  _BUILT_LOADER),
    _window("vulkan.window-direct", 720,
            "window_check.mm on the frontend by its path: surface, swapchains, presents read back, resize, minimize",
            ["direct", _FRONTEND], ["file:" + _FRONTEND]),
    _window_loader("vulkan.window-loader", 721,
                   "window_check.mm through the installed Vulkan loader: it routes to the frontend and presents",
                   _INSTALLED_LOADER),
    _window_loader("vulkan.window-loader-built", 722,
                   "window_check.mm through the Vulkan loader of the build folder: it routes to the frontend",
                   _BUILT_LOADER),
    # SDL3 makes its process a regular app and activates it unless SDL_MAC_BACKGROUND_APP is set; the two others keep
    # a shown window from being activated. smoke.mm has no alarm of its own: the runner's limit is the only one.
    _smoke("vulkan.smoke-sdl", 723,
           "smoke.mm with an SDL3 window: 90 frames, every copy read back, a resize, a minimize and a restore",
           _FRONTEND, ["file:" + _FRONTEND, "file:" + _SDL],
           [r"^PASS SDL/provider equality$", r"^PASS Minecraft required device features$",
            r"^STATS copies=90 bytes=\d+ import_failures=0 cpu_readback=0 cpu_upload=0$",
            r"^VERIFICATION frames=90 ", r"^PASS native SDL presentation/resize/restore$"],
           _SMOKE_INFO + [r"^SWAPCHAIN ", r"^STATS ", r"^VERIFICATION "],
           env={"NVKBRIDGE_VERIFY_PIXELS": "1", "SDL_MAC_BACKGROUND_APP": "1",
                "SDL_WINDOW_ACTIVATE_WHEN_SHOWN": "0", "SDL_WINDOW_ACTIVATE_WHEN_RAISED": "0"},
           extra=[_SDL, "3", "90"], window=True, limit=120),
]
