"""The rows of tools/card_selftest.py for release 3.0.1's own changes, checked on the card with the installed driver.

Each row needs a file that release 3.0.1 added to the checkout ("needs": file:{SRC}/...), so on a checkout without
3.0.1 every row is a skip that names that file: plugin/nvmtl_switch_defaults.h for the two switches that are on by
default from 3.0.1, nvk/nvk-local-map-vram.patch for where NVK keeps its own mapped memory, nvk/nvk-nvrm-fixes.patch
for the NVRM back end's fixes, kexts/NVRM/nvrm_boost_policy_abi.h for the boost policy. The runner's gates have
already seen that the installed package is the checkout's (run it with --expect of the 3.0.1 package's commit) and
that the loaded NVRM.kext is a 3.0.1 one. Every row builds one program into {BIN}/<id>/nvmtltest and runs it once,
offscreen, with the runner's environment (NVMTL_PROCESS_ENV=1, the row's own NVMTL_LOG_PATH, caches in the run
folder); the programs follow the card conventions (0 pass, 1 a case failed, 2 only setup failed, 3 skip with
"SKIP: <why>" first, 70 a crash signal; PASS, FAIL and INFO lines and a RESULT line last). A row that checks a
default skips when the settings file sets that switch, since the default is then not what runs.

  v301.boost-policy          the boost policy read through NVRM's user client as NVK reads it equals
                             debug.nvrm_boost_policy; sets without root, of a value past the last and an unknown
                             operation are refused; nothing changed (no Metal device, no GPU work)
  v301.async-upload          replaceRegion's asynchronous upload, on by default: 300 textures written one after
                             another and read by blits, the count limit of 256 pending uploads and its one log line,
                             overwrites, two threads on one texture, textures freed with their upload queued, an
                             IOSurface texture and a view of it read by the CPU at once; NVMTL_SURFIN_ZERO=1 stays
                             off outside WindowServer (a surface brought in for a blit says so), and so does
                             NVMTL_WS_FREE_OFFMAIN=1 when a free asked it (otherwise INFO)
  v301.async-upload-off      the same cases with NVMTL_ASYNC_TEX_UPLOAD=0 (the way back: every upload waits)
  v301.half-mips-sync        plugin.half-mips (its replaceRegion uploads) with NVMTL_ASYNC_TEX_UPLOAD=0; that the switch
                             reaches the driver is v301.async-upload-off's to show (the older program does not say)
  v301.pretranslate-game     background translation of a new library's shaders in a game (the program's own
                             Info.plist has the games category): on by default, 32 of 32 processed ahead, pipelines
                             made while it runs, every kernel right
  v301.pretranslate-off      the same game with NVMTL_PRETRANSLATE=0: nothing queued, every kernel right
  v301.pretranslate-app      the program without an Info.plist (not a game): nothing queued, every kernel right
  v301.localmap-default      new kernels and new textures and samplers used at once in the command buffer that made
                             them, with NVK's own mapped memory where 3.0.1 puts it by default (behind BAR1 where BAR1
                             covers the video memory)
  v301.localmap-sysmem       the same with NVK_LOCAL_MAP_SYSMEM=1 (the way back: system RAM, as in 3.0)
  v301.vk-nvrm               the native Vulkan runtime's renderer: VK_EXT_map_memory_placed withdrawn on the NVRM back
                             end, and 4 threads mapping and freeing memory at once with MallocScribble=1; the heaps,
                             and whether BAR1 covers the video memory, are information
The pretranslate rows set NVMTL_GAME_GPU_PRIORITY=0 and NVMTL_GAME_LOWER_BACKGROUND=0, so that a program that is a
game never changes its channel group's priority. Not yet run on a Mac."""

_FLAGS = ["-arch", "x86_64", "-mmacosx-version-min=15.0", "-fobjc-arc", "-O2"]
_CXX = ["/usr/bin/xcrun", "clang++", "-arch", "x86_64", "-mmacosx-version-min=15.0", "-std=c++17", "-fobjc-arc", "-O2"]
_RESULT = r"^RESULT pass \d+ passed, 0 failed$"
_COMMON = "tools/card/plugin_common.h"
# the files of release 3.0.1 that the rows need
_DEFAULTS = "file:{SRC}/plugin/nvmtl_switch_defaults.h"
_LOCAL_MAP = "file:{SRC}/nvk/nvk-local-map-vram.patch"
_NVRM_FIXES = "file:{SRC}/nvk/nvk-nvrm-fixes.patch"
_BOOST = "file:{SRC}/kexts/NVRM/nvrm_boost_policy_abi.h"
_RENDERER = "{GB}/nvmtl/minecraft/lib/libnvk_renderer.dylib"
_PLIST = "tools/card/v301_game_info.plist"
_NO_GAME_MODE = {"NVMTL_GAME_GPU_PRIORITY": "0", "NVMTL_GAME_LOWER_BACKGROUND": "0"}


def _build(cid, source, frameworks, extra=()):
    argv = ["/usr/bin/xcrun", "clang"] + _FLAGS + ["{SRC}/" + source] + list(extra)
    for f in frameworks:
        argv += ["-framework", f]
    return [{"argv": argv + ["-o", "{BIN}/" + cid + "/nvmtltest"], "limit": 120}]


def _row(cid, order, title, program, frameworks, args, stdout, needs, env=None, limit=90, extra=(), sources=(),
         info=(r"^INFO ",)):
    row = {"id": cid, "part": "v301", "kind": "gpu", "order": order, "title": title,
           "sources": ["tools/card/" + program, _COMMON] + list(sources),
           "build": _build(cid, "tools/card/" + program, frameworks, extra),
           "argv": ["{BIN}/" + cid + "/nvmtltest"] + list(args), "limit": limit, "needs": list(needs),
           "expect": {"rc": 0, "stdout": list(stdout) + [_RESULT]}, "rc_map": {3: "skip"}, "info": list(info)}
    if env:
        row["env"] = dict(env)
    return row


_ASYNC_BOTH = [r"^PASS many 300 textures of 128x128 written one after another, then read by blits and every tenth "
               r"by getBytes$", r"^PASS overwrite ", r"^PASS race ", r"^PASS destroy ", r"^PASS iosurface "]
_ASYNC_ON = _ASYNC_BOTH + [
    r"^PASS limit the driver said once: vk: tex upload async: 256 pending uploads hold \d+ bytes of staging "
    r"\(limits 256 and 268435456\): ",
    r"^PASS switch the driver says vk: async texture upload ON \(NVMTL_ASYNC_TEX_UPLOAD=unset\)$",
    r"^PASS gating NVMTL_SURFIN_ZERO=1 is off outside WindowServer: a surface the CPU zeroed was uploaded as any "
    r"other$"]
_ASYNC_OFF = _ASYNC_BOTH + [
    r"^PASS limit no line of pending uploads at a limit: every upload waited for its copy$",
    r"^PASS switch the driver says vk: async texture upload OFF \(NVMTL_ASYNC_TEX_UPLOAD=0\)$"]

_PRE_BOTH = [r"^PASS kernels all 64 kernels of both libraries wrote what their text says$"]
_PRE_GAME = _PRE_BOTH + [
    r"^PASS switch the driver says pretranslate: ON \(NVMTL_PRETRANSLATE=unset, a game\)$",
    r"^PASS ahead the driver says pretranslate: 32 of 32 queued shaders processed - ",
    r"^PASS during 32 pipelines made and run while their background translation ran; the driver says pretranslate: "
    r"64 of 64 queued shaders processed - ",
    r"^PASS queued the driver says twice: pretranslate: library of 32 entry points queued for background "
    r"translation$"]
_PRE_NONE = _PRE_BOTH + [r"^PASS during 32 pipelines of a second library made and run$",
                         r"^PASS queued the driver queued no library for background translation$"]

_LOCAL = [r"^PASS kernels 96 new kernels in 24 new libraries, each run as soon as its pipeline was made$",
          r"^PASS textures 200 new textures and samplers, each read and sampled in the command buffer that filled it$"]

CHECKS = [
    _row("v301.boost-policy", 295,
         "the boost policy read as NVK reads it; a set without privilege and bad values refused; nothing changed",
         "v301_boost_policy.m", ["Foundation", "Metal", "IOKit"], [],
         [r"^PASS read ", r"^PASS refused ", r"^PASS unchanged "], [_BOOST], limit=40),
    _row("v301.async-upload", 360,
         "replaceRegion's asynchronous upload, on by default: limits, overwrites, threads, frees and IOSurfaces",
         "v301_async_upload.m", ["Foundation", "Metal", "IOSurface"], ["on"], _ASYNC_ON, [_DEFAULTS],
         env={"NVMTL_SURFIN_ZERO": "1", "NVMTL_WS_FREE_OFFMAIN": "1"}),
    _row("v301.async-upload-off", 361,
         "the same uploads with NVMTL_ASYNC_TEX_UPLOAD=0 (the way back: replaceRegion waits for its copy)",
         "v301_async_upload.m", ["Foundation", "Metal", "IOSurface"], ["off"], _ASYNC_OFF, [_DEFAULTS],
         env={"NVMTL_ASYNC_TEX_UPLOAD": "0"}),
    {"id": "v301.half-mips-sync", "part": "v301", "kind": "gpu", "order": 362,
     "title": "Core Animation's half-float mip levels with NVMTL_ASYNC_TEX_UPLOAD=0 (its uploads wait, as in 3.0)",
     "sources": ["tools/test_ca_half_float_mips_gpu.m"],
     "build": _build("v301.half-mips-sync", "tools/test_ca_half_float_mips_gpu.m", ["Foundation", "Metal"]),
     "argv": ["{BIN}/v301.half-mips-sync/nvmtltest"], "limit": 90, "needs": [_DEFAULTS],
     "env": {"NVMTL_ASYNC_TEX_UPLOAD": "0"},
     # the older program: exit 2 (no device of this driver, or a setup step) is a failure, as in plugin.half-mips. It
     # prints nothing of the switch, and a row has no key for a line of the driver's log: that NVMTL_ASYNC_TEX_UPLOAD=0
     # reaches the driver in this environment is held by v301.async-upload-off just before, whose switch case requires
     # the driver's "OFF (NVMTL_ASYNC_TEX_UPLOAD=0)" line; the runner copies no such line into this row's information.
     # It has no crash handlers either: a crash ends it by the signal, and macOS may write a crash report for it, which
     # the runner's audit names, as for plugin.half-mips
     "expect": {"rc": 0, "stdout": [r"^PASS: half-float mip levels, 0 wrong values$"]}, "rc_map": {2: "fail"}},
    _row("v301.pretranslate-game", 365,
         "a game's new shaders translated ahead by default; pipelines made while it runs; every kernel right",
         "v301_pretranslate.m", ["Foundation", "Metal"], ["game"], _PRE_GAME, [_DEFAULTS], env=_NO_GAME_MODE,
         extra=["-Wl,-sectcreate,__TEXT,__info_plist,{SRC}/" + _PLIST], sources=[_PLIST]),
    _row("v301.pretranslate-off", 366, "the same game with NVMTL_PRETRANSLATE=0: nothing translated ahead",
         "v301_pretranslate.m", ["Foundation", "Metal"], ["off"],
         _PRE_NONE + [r"^PASS switch the driver says pretranslate: OFF \(NVMTL_PRETRANSLATE=0\)$"], [_DEFAULTS],
         env=dict(_NO_GAME_MODE, NVMTL_PRETRANSLATE="0"),
         extra=["-Wl,-sectcreate,__TEXT,__info_plist,{SRC}/" + _PLIST], sources=[_PLIST]),
    _row("v301.pretranslate-app", 367, "a program that is not a game: nothing translated ahead by default",
         "v301_pretranslate.m", ["Foundation", "Metal"], ["app"],
         _PRE_NONE + [r"^PASS switch the driver says pretranslate: OFF \(NVMTL_PRETRANSLATE=unset, not a game\)$"],
         [_DEFAULTS], env=_NO_GAME_MODE),
    _row("v301.localmap-default", 370,
         "new kernels, textures and samplers used at once, with NVK's own mapped memory where 3.0.1 puts it",
         "v301_local_map.m", ["Foundation", "Metal"], ["default"], _LOCAL, [_LOCAL_MAP]),
    _row("v301.localmap-sysmem", 371,
         "the same with NVK_LOCAL_MAP_SYSMEM=1 (the way back: NVK's own mapped memory in system RAM)",
         "v301_local_map.m", ["Foundation", "Metal"], ["sysmem"], _LOCAL, [_LOCAL_MAP],
         env={"NVK_LOCAL_MAP_SYSMEM": "1"}),
    {"id": "v301.vk-nvrm", "part": "v301", "kind": "gpu", "order": 530,
     "title": "the renderer: VK_EXT_map_memory_placed withdrawn, and 4 threads mapping and freeing memory at once",
     "sources": ["tools/card/v301_vk_nvrm.mm"],
     "build": [{"argv": _CXX + ["-Wall", "-Wextra", "-Wno-missing-field-initializers", "-I{VKINC}",
                                "{SRC}/tools/card/v301_vk_nvrm.mm", "-framework", "Foundation", "-framework", "Metal",
                                "-o", "{BIN}/v301.vk-nvrm/nvmtltest"], "limit": 180}],
     "argv": ["{BIN}/v301.vk-nvrm/nvmtltest", _RENDERER], "limit": 90,
     "needs": ["vkinc", "file:" + _RENDERER, _NVRM_FIXES], "env": {"MallocScribble": "1"},
     # NVK's own lines of a mapping table that lost an entry, and of an RM allocation, map or unmap refused (the same
     # text for each, nvkmd_nvrm_mem.c), from any NVK in the process; the program fails a refused allocation as well
     "expect": {"rc": 0, "stdout": [r"^PASS placed ", r"^PASS map-threads ", _RESULT],
                "stderr_forbid": [r"^\[!\] ent == NULL", r"^\[!\] nvRes:"]},
     "rc_map": {3: "skip"}, "info": [r"^INFO "]},
]
