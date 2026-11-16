"""The shader translator rows of tools/card_selftest.py: the translator repairs, checked on the installed plugin
({P}) and translator ({X}) of this Mac.

  xlate.sampler-array      tools/test_sampler_array_gpu.m: array<sampler, 1>, per-element run-time sampler state, 15
                           offscreen renders through the system's Metal device; its AIR is dumped
  xlate.int-handle         tools/test_integer_texture_handle_gpu.m with tools/fixtures/integer_texture_handle.metal:
                           a texture copied through a 64-bit integer and a uniform nested in an argument group; its AIR
                           is dumped and the row says whether Apple's compiler emitted the integer copy
  xlate.msaa               tools/test_webgpu_msaa_gpu.m with tools/fixtures/webgpu_msaa_resolve.metal: a multisampled
                           argument-buffer texture keeps its samples, query-only textures get their descriptor; its
                           AIR is dumped
  xlate.paths-*            tools/card/xlate_paths.m, one row per group of its cases: argument-buffer texture arrays
                           by a run-time index, a texture_buffer field, sampler picks among 8, 9 and 16, a back-edge
                           pointer phi, an escaping pointer, RGB10A2 and RGB565 packing, a splat argument, textures in
                           a local object, an int2 through a float4 pointer
  xlate.readback-prepare   tools/translator_readback.py --prepare-only: every readback case translated by {X} in
                           Python and checked by spirv-val (no GPU)
  xlate.readback-<case>    one readback case per row: translated by {X}, checked by spirv-val, dispatched by
                           tools/translator_readback.cpp through {P}'s Vulkan entry points, its words compared

The two programs that load a plugin by path are given {P} only, the file Metal itself loads: a second copy of
NVMTLDriver in a process defines its classes twice. translator_readback.py runs as /usr/bin/python3 -B -I, never with
-O (it checks with assert), and gets spirv-val and spirv-dis only through --tools {TOOLCHAIN}, never on PATH. No row
sets NVMTL_NO_TIER2_FIXED, and no case reads an array of texture buffers past element 0: on that old path an index
past 0 is a descriptor read out of range. Every row builds into {BIN}/<id>/ and writes under {OUT} only (logs and
caches by the runner's environment, AIR text into {OUT}/airdump/<id>, readback files into {OUT}/readback/<case>).

{X} need not be the translator the plugin uses: the plugin opens the libnvmtl_translate.dylib beside it in
NVMTLDriver.bundle/Contents/MacOS when that file is there, else nvmtl/libnvmtl_translate.dylib
(plugin/NVMTLLibraryLoad.m, nvmtl_xlate_ready). The rows of xlate_paths.m end with an "INFO translator: " line that
repeats the plugin's own line naming the translator it opened (its LC_UUID and path), and the readback rows translate
with {X} itself. These rows have run once, on one Mac, on macOS 26.7.3 (25G342) with release 3.0.1's package."""

_CC = ["/usr/bin/xcrun", "clang", "-arch", "x86_64", "-mmacosx-version-min=15.0"]
_PY = ["/usr/bin/python3", "-B", "-I"]

# translator_readback.py's kernels, in its order (the 27 cases it had, then ivec2-gep, splat-inline, object-fields)
_READBACK = ("control", "phi", "table", "helper", "i64", "alloca", "callback", "pack565", "pack10", "pack565dyn",
             "forward0", "forward1", "texture5", "texture127", "texture1d", "storage40", "texture-array0",
             "texture-array1", "texture-array-dynamic", "sampler-array0", "sampler-array1", "wrapped-nested",
             "wrapped-single", "wrapped-trailing", "wrapped-array", "wrapped-float", "wrapped-bitcopy", "ivec2-gep",
             "splat-inline", "object-fields")
_READBACK_SOURCES = ["tools/translator_readback.py", "tools/translator_readback.cpp", "plugin/nvmtl_vk.h"]


def _airdump(cid):
    return {"NVMTL_AIR_DUMP": "{OUT}/airdump/" + cid}


def _existing(cid, order, title, source, frameworks, opt, args, stdout, info, extra_sources=(), argv=None):
    """a row of one of the existing GPU programs, built as their header comment says, into {BIN}/<id>/nvmtltest"""
    build = _CC + ["-fobjc-arc"] + list(opt) + ["{SRC}/" + source, "-o", "{BIN}/%s/nvmtltest" % cid]
    for f in frameworks:
        build += ["-framework", f]
    row = {"id": cid, "part": "xlate", "title": title, "kind": "gpu", "order": order,
           "sources": [source] + list(extra_sources), "build": [{"argv": build, "limit": 300}],
           "argv": argv or ["{BIN}/%s/nvmtltest" % cid] + list(args),
           # none of the three has a time limit of its own (their waitUntilCompleted), so the runner's is the limit
           "limit": 60, "expect": {"rc": 0, "stdout": list(stdout)},
           # exit 2 (no device, setup) and 3 (no library) are failures of these programs, not skips
           "rc_map": {}, "info": list(info)}
    return row


# The integer-handle program runs under /bin/sh, which then says whether the AIR of fs, dumped by the plugin into
# {OUT}/airdump/xlate.int-handle, holds the shape the repair handles (load i64 then inttoptr); Apple's compiler may
# emit another. A signal that ended the program becomes the convention of tools/card ("CRASH SIG<name>" on stderr,
# exit 70), so the runner reports a crash; the shell does not raise it again, which would add a crash report of its own.
_HANDLE_SH = (
    '"$1" "$2" "$3"; rc=$?\n'
    'f="$4/fs.ll"\n'
    'if [ ! -f "$f" ]; then echo "INFO no AIR text of fs in the dump folder"\n'
    'elif grep -q "load i64" "$f" && grep -q "inttoptr" "$f"; then\n'
    '  echo "INFO shape: the AIR of fs holds load i64 and inttoptr (the integer-copied texture is reached)"\n'
    'else echo "INFO shape: the AIR of fs lacks load i64 or inttoptr (Apple\'s compiler kept the texture as is)"; fi\n'
    'if [ "$rc" -gt 128 ] && [ "$rc" -lt 160 ]; then echo "CRASH SIG$(kill -l "$((rc - 128))")" >&2; exit 70; fi\n'
    'exit "$rc"\n')

CHECKS = [
    _existing("xlate.sampler-array", 300, "array<sampler, 1> and per-element run-time sampler state: 15 offscreen "
              "renders read back", "tools/test_sampler_array_gpu.m", ("Foundation", "Metal"), (), (),
              [r"^passed=15 failed=0$"], [r"^passed="]),
    _existing("xlate.int-handle", 301, "WebGPU texture copied through a 64-bit integer, nested uniform: 4 frames read "
              "back", "tools/test_integer_texture_handle_gpu.m", ("Foundation", "Metal", "IOKit"), ("-O2",), (),
              [r"^frame=%d swap=%d .* errors=0$" % (f, f % 2) for f in range(4)], [r"^frame=", r"^INFO "],
              extra_sources=("tools/fixtures/integer_texture_handle.metal",),
              argv=["/bin/sh", "-c", _HANDLE_SH, "xlate.int-handle", "{BIN}/xlate.int-handle/nvmtltest", "{P}",
                    "{SRC}/tools/fixtures/integer_texture_handle.metal", "{OUT}/airdump/xlate.int-handle"]),
    _existing("xlate.msaa", 302, "WebGPU multisampled argument-buffer texture and query-only textures: 6 readbacks",
              "tools/test_webgpu_msaa_gpu.m", ("Foundation", "Metal", "IOKit"), ("-O2",),
              ("{P}", "{SRC}/tools/fixtures/webgpu_msaa_resolve.metal"),
              [r"^%s encoder=%s pixel=.* errors=0$" % (n, e) for e in ("reflected", "descriptor")
               for n in ("msaa_resolve", "msaa_query", "mip_query")], [r"^(msaa_resolve|msaa_query|mip_query) "],
              extra_sources=("tools/fixtures/webgpu_msaa_resolve.metal",)),
]
# The three dump the AIR text of every function they ask for into {OUT}/airdump/<id> (plugin/NVMTLObjects.m,
# nvmtl_air_dump, before the translation, so a function the driver refuses is dumped too): which form Apple's compiler
# gave a shape is then on file beside the row's verdict.
for _c in CHECKS:   # the three rows above
    _c["env"] = _airdump(_c["id"])

_PATHS_ALARM = 75   # xlate_paths.m's ALARM_SECONDS
for _i, (_s, _cases, _t) in enumerate((
        ("abarray", ("abarray-a", "abarray-c", "abarray-s"),
         "argument-buffer texture arrays of 8 by a run-time index, both declarations, read and sampled"),
        ("tbuf", ("tbuf",), "a single texture_buffer field of an argument buffer, 16 texels"),
        ("samplerphi", ("phi8", "phi9", "phi16"), "a sampler picked among 8, 9 and 16 equals the direct sample"),
        ("walk", ("walk",), "a loop that picks its buffer through a back-edge pointer phi"),
        ("esc", ("esc",), "a device pointer from a buffer passed to a helper that is not inlined"),
        ("pack", ("pack10", "pack565"),
         "RGB10A2 and RGB565 packing at the half-way values, as the exact product rounds"),
        ("splat", ("splat",), "a helper called with a vector splat constant"),
        ("localfield", ("localfield",), "textures and samplers in a local object's fields reach the sample"),
        ("lanes", ("lanes",), "an int2 read through a float4 pointer at a run-time index"))):
    _cid = "xlate.paths-" + _s
    CHECKS.append({
        # abarray goes last but one: on a translator that misses the heap route it reads past one descriptor, and a
        # GPU fault there stops every later GPU row
        "id": _cid, "part": "xlate", "title": _t, "kind": "gpu", "order": 890 if _s == "abarray" else 310 + _i,
        "sources": ["tools/card/xlate_paths.m"],
        "build": [{"argv": _CC + ["-fobjc-arc", "-O1", "-g", "-Wall", "-Wextra", "-Wno-unused-parameter",
                                  "{SRC}/tools/card/xlate_paths.m", "-o", "{BIN}/%s/nvmtltest" % _cid,
                                  "-framework", "Foundation", "-framework", "Metal"], "limit": 300}],
        "argv": ["{BIN}/%s/nvmtltest" % _cid] + list(_cases), "env": _airdump(_cid), "limit": _PATHS_ALARM + 30,
        "expect": {"rc": 0, "stdout": [r"^RESULT pass "] + [r"^(PASS|SKIP) %s[ :]" % c for c in _cases]},
        "rc_map": {3: "skip"}, "info": [r"^INFO ", r"^SKIP "]})

CHECKS.append({
    "id": "xlate.readback-prepare", "part": "xlate", "title": "every readback case translated by the installed "
    "translator in Python and checked by spirv-val (no GPU)", "kind": "cpu", "order": 250,
    "sources": list(_READBACK_SOURCES),
    "argv": _PY + ["{SRC}/tools/translator_readback.py", "--plugin", "{P}", "--translator", "{X}", "--tools",
                   "{TOOLCHAIN}", "--out", "{OUT}/readback/prepare", "--prepare-only"],
    "limit": 180, "needs": ["spirv"],
    "expect": {"rc": 0, "stdout": [r"^Prepared and validated %s$" % c for c in _READBACK]}, "rc_map": {}})

for _i, _c in enumerate(_READBACK):
    _cid = "xlate.readback-" + _c
    CHECKS.append({
        "id": _cid, "part": "xlate", "title": "readback case %s: translated, validated, dispatched through the "
        "plugin's Vulkan entry points, words compared" % _c, "kind": "gpu", "order": 400 + _i,
        "sources": list(_READBACK_SOURCES),
        # the script builds its C++ child into --out with xcrun clang++ and gives it 30 s; this limit covers the
        # build, the translation and spirv-val too
        "argv": _PY + ["{SRC}/tools/translator_readback.py", "--plugin", "{P}", "--translator", "{X}", "--tools",
                       "{TOOLCHAIN}", "--headers", "{VKINC}", "--out", "{OUT}/readback/" + _c, "--cases", _c],
        "limit": 180, "needs": ["spirv", "vkinc"],
        "expect": {"rc": 0, "stdout": [r"^GPU ", r"^PASS %s \(\d+ dispatches\)$" % _c]}, "rc_map": {},
        "info": [r"^GPU "]})

del _i, _s, _cases, _t, _cid, _c
