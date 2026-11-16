"""The Metal plugin rows of tools/card_selftest.py: the plugin's fixes checked on the card with the installed driver.

Every row builds one program from the checkout as {BIN}/<id>/nvmtltest (the name the plugin always grants a device)
and runs it once, offscreen, with the runner's environment (NVMTL_PROCESS_ENV=1, the row's own NVMTL_LOG_PATH, caches
in the run folder). They run one at a time, from the least GPU work to the most.

The programs that were there before these rows (tools/test_*.m, tools/photobooth_surface_probe.m) exit 0 on a pass, 1
when a case failed and 2 when they got no device of this driver or could not build their own objects; their rc_map
makes 2 a failure, so that a missing device never reads as a pass (the runner's gates have already seen this driver's
device, so a program that does not get it has failed). tools/test_unknown_iosurface_storage.m also exits 3 to 6 for
its own setup steps, which are failures too, not the skip and inactive of the default map. The new programs
(tools/card/plugin_*.m, with tools/card/plugin_common.h) follow the card conventions: 0 pass, 1 a case failed, 2 only
setup failed, 3 skip with "SKIP: <why>" first, 70 a crash signal; PASS, FAIL and INFO lines and a RESULT line last.

  plugin.pb-preconditions     the Photo Booth adapter's two method signatures hold on this macOS, and the adapter
                              stays out of a process that is not Photo Booth (no camera, no Photo Booth identity)
  plugin.format-threads       the first format-storage query from 32 threads in 10 fresh processes; a smoke check
                              with almost no power to catch the old race
  plugin.half-mips            Core Animation's mipmapped half-float textures get their lower levels on the GPU
  plugin.unknown-usage        shader writes through Unknown-usage textures and their mip views land
  plugin.unknown-paths        the same through an automatic heap, a placement heap, a buffer's texture and a texel
                              buffer, and the heap size query sizes Unknown as writable
  plugin.a8-fetch             framebuffer fetch of an A8 attachment (plain, and an A8 view of R8)
  plugin.a8-attach            A8 attachments through identity views without fetch: blending, a mip level, an A8 view
  plugin.a8-attach-nofetch    the same with NVMTL_NO_FBFETCH=1 (fetch off in the whole process)
  plugin.custom-drawable      the scheduled-present handler for application-made drawables (tools/test_custom_drawable.m
                              now counts its failures and ends a regression with exit 1, never a signal)
  plugin.iosurface-storage    Unknown-usage IOSurface wrappers still take shader writes (the older regression check)
  plugin.buffer-alias-fetch   a buffer's linear texture beside a tiled attachment with fetch (the tiled stand-in), and
                              RG11B10Float and RGB9E5Float linear textures
  plugin.managed-tiled        a shader-writable Managed surface wrapper drawn through a tiled image of its own, and what
                              other wrappers and the CPU see of it
  plugin.managed-tiled-off    the same with NVMTL_MANAGED_SURFACE_TILED=0 (the way back: drawn straight into the import)
  plugin.pb-orientation       Core Image's legacy render into a drawable of a layer outside any window equals the same
                              render offscreen; which way up the rows land is information
  plugin.photobooth-surface   Core Image renders into IOSurface drawables as Photo Booth's preview does; on a card whose
                              linear import has no storage view the 2048-byte-row surfaces correctly take a private
                              image and the probe fails at "aligned pitch must retain shared VRAM": the row then carries
                              the driver's own reason line (from its nvmtl.log) as information
  plugin.surface-formats      framebuffer fetch into IOSurface wrappers, 96 cases, read by other wrappers and the CPU
  plugin.surface-formats-untiled  the same with NVMTL_MANAGED_SURFACE_TILED=0 (the way back)
Not yet run on a Mac."""

_FLAGS = ["-arch", "x86_64", "-mmacosx-version-min=15.0", "-fobjc-arc", "-O2"]


def _build(cid, source, frameworks):
    """one compile of one source into {BIN}/<id>/nvmtltest"""
    argv = ["/usr/bin/xcrun", "clang"] + _FLAGS + ["{SRC}/" + source]
    for f in frameworks:
        argv += ["-framework", f]
    return [{"argv": argv + ["-o", "{BIN}/" + cid + "/nvmtltest"], "limit": 120}]


def _row(cid, order, title, source, frameworks, limit, stdout, rc_map, env=None, info=None, argv=None,
         extra_sources=()):
    row = {"id": cid, "part": "plugin", "kind": "gpu", "order": order, "title": title,
           "sources": [source] + list(extra_sources),
           "build": _build(cid, source, frameworks),
           "argv": argv or ["{BIN}/" + cid + "/nvmtltest"],
           "limit": limit,
           "expect": {"rc": 0, "stdout": stdout},
           "rc_map": rc_map}
    if env:
        row["env"] = env
    if info:
        row["info"] = info
    return row


def _new(cid, order, title, program, frameworks, limit, stdout, env=None, info=None):
    """a row of a new program of tools/card (the card conventions: exit 3 is a skip)"""
    return _row(cid, order, title, "tools/card/" + program, frameworks, limit,
                stdout + [r"^RESULT pass \d+ passed, 0 failed$"], {3: "skip"}, env=env, info=info,
                extra_sources=["tools/card/plugin_common.h"])


# exit 2 of the older programs (no device of this driver, or a setup step) is a failure
_OLD_RC = {2: "fail"}

# photobooth_surface_probe.m is wrapped by /bin/sh: after the program ends, the driver's reason lines for the
# 2048-byte-row (512x64) surfaces that took a private image are copied from the row's own nvmtl.log to stdout as
# "INFO driver: item 7: ..." (none when those surfaces kept the family's VRAM). The program's exit code passes through;
# a program ended by its own alarm ends the shell by SIGALRM too, so the runner still reads a TIMEOUT and stops; another
# signal becomes exit 70 with "CRASH SIG<name>" on stderr, which the runner reads as a crash.
_PB_WRAP = ('"$1"; rc=$?; '
            'if [ -n "$NVMTL_LOG_PATH" ] && [ -r "$NVMTL_LOG_PATH" ]; then '
            '/usr/bin/grep -h "NVMTL: item 7: surface [0-9]* 512x64 plane 0 -> a private image" "$NVMTL_LOG_PATH" '
            '| /usr/bin/head -n 8 | /usr/bin/sed "s/^pid [0-9]* NVMTL: /INFO driver: /"; fi; '
            'if [ "$rc" -eq 142 ]; then kill -ALRM $$; fi; '
            'if [ "$rc" -gt 128 ]; then echo "CRASH SIG$(kill -l $((rc - 128)))" >&2; exit 70; fi; '
            'exit "$rc"')

_PB_TITLE = "Core Image renders into IOSurface drawables of this driver, as Photo Booth's preview does"

CHECKS = [
    _new("plugin.pb-preconditions", 300,
         "the Photo Booth adapter's method signatures hold here, and it stays out of other processes",
         "plugin_pb_preconditions.m", ["Foundation", "Metal", "QuartzCore", "CoreImage"], 50,
         [r"^PASS nextDrawable-signature ", r"^PASS render-signature ", r"^PASS nextDrawable-implementation ",
          r"^PASS render-implementation "],
         info=[r"^INFO (nextDrawable|render) ", r"^INFO process "]),
    _new("plugin.format-threads", 302,
         "the first format query from 32 threads in 10 fresh processes (a smoke check)",
         "plugin_format_threads.m", ["Foundation", "Metal"], 150,
         [r"^PASS all 320 of 320 textures had a storage view and 40 of 40 readbacks were exact in 10 fresh processes$"],
         info=[r"^INFO a smoke check"]),
    _row("plugin.half-mips", 310, "Core Animation's half-float mip levels are made on the GPU",
         "tools/test_ca_half_float_mips_gpu.m", ["Foundation", "Metal"], 90,
         [r"^PASS: half-float mip levels, 0 wrong values$"], _OLD_RC),
    _row("plugin.unknown-usage", 312, "shader writes through Unknown-usage textures and their mip views land",
         "tools/test_unknown_texture_usage_gpu.m", ["Foundation", "Metal"], 90,
         [r"^PASS: shader writes through Unknown and ShaderWrite usage, 0 wrong texels$"], _OLD_RC),
    _new("plugin.unknown-paths", 314,
         "Unknown usage through heaps, a placement heap, buffer and texel-buffer textures; the heap size query",
         "plugin_unknown_paths.m", ["Foundation", "Metal"], 90,
         [r"^PASS heap-automatic ", r"^PASS heap-placement ", r"^PASS heap-size ", r"^PASS buffer-texture ",
          r"^PASS texel-buffer "],
         info=[r"^INFO heapTextureSizeAndAlign"]),
    _row("plugin.a8-fetch", 316, "framebuffer fetch of A8 attachments (plain, and an A8 view of R8)",
         "tools/test_a8_framebuffer_fetch_gpu.m", ["Foundation", "Metal"], 90,
         [r"^PASS: A8 framebuffer fetch, 0 wrong pixels$"], _OLD_RC),
    _new("plugin.a8-attach", 318, "A8 attachments through identity views: blending, a mip level, an A8 view of R8",
         "plugin_a8_attach.m", ["Foundation", "Metal"], 90,
         [r"^PASS blend-frame3 ", r"^PASS mip ", r"^PASS view ",
          r"^PASS fetch-state the driver log says framebuffer fetch ON"],
         info=[r"^INFO driver: vk: framebuffer fetch"]),
    _new("plugin.a8-attach-nofetch", 319, "the same A8 attachments with framebuffer fetch off (NVMTL_NO_FBFETCH=1)",
         "plugin_a8_attach.m", ["Foundation", "Metal"], 90,
         [r"^PASS blend-frame3 ", r"^PASS mip ", r"^PASS view ",
          r"^PASS fetch-state the driver log says framebuffer fetch OFF"],
         env={"NVMTL_NO_FBFETCH": "1"}, info=[r"^INFO driver: vk: framebuffer fetch"]),
    _row("plugin.custom-drawable", 320,
         "application-made drawables get a scheduled-present handler; CAMetalDrawable keeps its path",
         "tools/test_custom_drawable.m", ["Foundation", "Metal", "QuartzCore"], 150,
         [r"^PASS: 11 policy/lifetime cases and 64 concurrent custom presents; all three presentation variants, "
          r"deferred invocation and CAMetalDrawable unchanged$"], _OLD_RC),
    _row("plugin.iosurface-storage", 322, "Unknown-usage IOSurface wrappers still take shader writes",
         "tools/test_unknown_iosurface_storage.m", ["Foundation", "Metal", "IOSurface"], 75,
         [r'"failures"\s*:\s*0\b'], _OLD_RC, info=[r'"(requestedUsage|reportedUsage)"']),
    _new("plugin.buffer-alias-fetch", 325,
         "a buffer's linear texture beside a tiled attachment with fetch; RG11B10 and RGB9E5 linear textures",
         "plugin_buffer_alias_fetch.m", ["Foundation", "Metal"], 90,
         [r"^PASS alias-beside-tiled-frame3 ", r"^PASS alias-alone-frame3 ", r"^PASS rg11b10 ", r"^PASS rgb9e5 "],
         info=[r"^INFO driver: "]),
    _new("plugin.managed-tiled", 330,
         "a shader-writable Managed surface drawn through its own tiled image; other wrappers and the CPU see it",
         "plugin_managed_tiled.m", ["Foundation", "Metal", "IOSurface"], 90,
         [r"^PASS RGBA8-backing backing: a tiled image of its own over the VRAM import$",
          r"^PASS RGBA16F-backing backing: a tiled image of its own over the VRAM import$", r"^PASS switch-log ",
          r"^PASS RGBA8-backing-log ", r"^PASS RGBA16F-backing-log "],
         info=[r"^INFO RGBA(8|16F) M: ", r"^INFO driver: "]),
    _new("plugin.managed-tiled-off", 331,
         "the same with NVMTL_MANAGED_SURFACE_TILED=0: drawn straight into the VRAM import",
         "plugin_managed_tiled.m", ["Foundation", "Metal", "IOSurface"], 90,
         [r"^PASS RGBA8-backing backing: straight into the VRAM import$",
          r"^PASS RGBA16F-backing backing: straight into the VRAM import$", r"^PASS switch-log "],
         env={"NVMTL_MANAGED_SURFACE_TILED": "0"}, info=[r"^INFO RGBA(8|16F) M: ", r"^INFO driver: "]),
    _new("plugin.pb-orientation", 335,
         "Core Image's legacy render into a drawable of a layer outside any window equals the offscreen render",
         "plugin_pb_orientation.m", ["Foundation", "Metal", "QuartzCore", "CoreImage", "CoreGraphics"], 90,
         [r"^PASS drawable-equals-offscreen "],
         info=[r"^INFO drawable"]),
    _row("plugin.photobooth-surface", 340, _PB_TITLE,
         "tools/photobooth_surface_probe.m", ["Foundation", "Metal", "CoreImage", "IOSurface"], 150,
         [r"^PASS: 32 Core Image frames, repeated wrappers, shared/private backing and every readback pixel$"], _OLD_RC,
         argv=["/bin/sh", "-c", _PB_WRAP, "photobooth-surface", "{BIN}/plugin.photobooth-surface/nvmtltest"],
         info=[r"^INFO driver: item 7: ", r"^(read|unknown) (BGRA|RGBA) (shared|private/cache) usage="]),
    _row("plugin.surface-formats", 350,
         "framebuffer fetch into IOSurface wrappers, 96 cases, read by other wrappers and the CPU",
         "tools/test_surface_render_formats.m", ["Foundation", "Metal", "IOSurface"], 150,
         [r"^PASS: 96 cases, 0 failures$"], _OLD_RC),
    _row("plugin.surface-formats-untiled", 351,
         "the same 96 cases with NVMTL_MANAGED_SURFACE_TILED=0 (the way back)",
         "tools/test_surface_render_formats.m", ["Foundation", "Metal", "IOSurface"], 150,
         [r"^PASS: 96 cases, 0 failures$"], _OLD_RC, env={"NVMTL_MANAGED_SURFACE_TILED": "0"}),
]
