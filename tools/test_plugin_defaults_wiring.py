#!/usr/bin/env python3
"""The plugin glue around 3.0.1's two speed defaults, where only the source can be checked here.

tools/test_switch_defaults.c holds the rules of nvmtl_switch_defaults.h, tools/test_tex_upload.c the rules of
nvmtl_tex_upload.h and tools/test_pretranslate.c the pool. This test holds the calls those cannot reach:

  async reader   nvmtl_async_tex_upload in plugin/nvmtl_vk.c decides with nvmtl_async_tex_upload_default(e, getprogname())
  pretranslate   nvmtl_pretranslate_on in plugin/NVMTLLibraryLoad.m decides with nvmtl_pretranslate_default(...) given
                 nvmtl_process_is_game(); nvmtl_pretranslate_library queues through the bounded submit (512 jobs) and
                 counts a refused job as skipped after releasing what it retained
  the pool       its workers run at QOS_CLASS_UTILITY, never QOS_CLASS_BACKGROUND
  surface-out    both replaceRegion methods of plugin/NVMTLObjects.m finish the upload of an IOSurface texture
                 (nvmtl_vk_image_upload_barrier) right before [self nvmtlSurfaceOut]
  the room       nvmtl_vk_image_write_region_level_layer asks nvmtl_up_room after the finish-first and before it makes
                 the asynchronous record; the store counts an upload in and the finish counts it out
  the hand-back  nvmtl_asyncup_store returns the handle nvmtl_up_set replaced, under the lock, and the upload finishes it

Each check also runs on damaged copies of the text, which must fail it (the controls).
Run: python3 tools/test_plugin_defaults_wiring.py [root]"""
import pathlib
import re
import sys

R = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1])
SRC = {
    "vk": (R / "plugin/nvmtl_vk.c").read_text(),
    "lib": (R / "plugin/NVMTLLibraryLoad.m").read_text(),
    "obj": (R / "plugin/NVMTLObjects.m").read_text(),
    "pool": (R / "plugin/nvmtl_pretranslate.h").read_text(),
}
fails = 0


def arm(name, ok):
    global fails
    print(("ok   " if ok else "FAIL ") + name)
    fails += not ok


def code(text):
    """The text without whole-line // comments, so that a comment between two statements does not hide them."""
    return re.sub(r"(?m)^[ \t]*//[^\n]*\n", "", text)


def body(text, head):
    """From the line that starts with `head` to the first line that is a lone closing brace, or '' when absent."""
    pat = "".join(r"\s*" + re.escape(c) + r"\s*" if c in "(),*:" else r"\s+" if c == " " else re.escape(c) for c in head)
    found = re.search(pat, text)
    if not found:
        return ""
    i = found.start()
    j = text.find("\n}\n", i)
    return text[i:j + 2] if j >= 0 else ""


WS = r"\s*"


def async_reader(s):
    b = code(body(s["vk"], "static int nvmtl_async_tex_upload(void)"))
    return re.search(r"\bon" + WS + "=" + WS + r"nvmtl_async_tex_upload_default" + WS + r"\(" + WS + r"e" + WS + ","
                     + WS + r"getprogname" + WS + r"\(" + WS + r"\)" + WS + r"\)" + WS + ";", b) is not None


def pretranslate_reader(s):
    b = code(body(s["lib"], "static int nvmtl_pretranslate_on(void)"))
    return re.search(r"\bon" + WS + "=" + WS + r"nvmtl_pretranslate_default" + WS + r"\([^;]*"
                     r"\bnvmtl_process_is_game" + WS + r"\(" + WS + r"\)[^;]*\)" + WS + ";", b) is not None


def pretranslate_bounded(s):
    lib = code(s["lib"])
    b = code(body(s["lib"], "void nvmtl_pretranslate_library(NVMTLLibrary *lib)"))
    m = re.search(r"nvmtl_pool_submit_bounded" + WS + r"\(" + WS + r"nvmtl_pre_pool" + WS + r"\(" + WS + r"\)" + WS + ","
                  + WS + r"nvmtl_pre_run" + WS + "," + WS + r"j" + WS + "," + WS + r"NVMTL_PRE_QUEUED_MAX" + WS + r"\)", b)
    refused = re.search(r"CFRelease" + WS + r"\(" + WS + r"j->lib" + WS + r"\)[^}]*free" + WS + r"\(" + WS + r"j" + WS
                        + r"\)[^}]*NVMTL_POOL_FULL[^}]*gPreSkipped", b, re.S)
    return (m is not None and refused is not None and re.search(r"nvmtl_pool_submit" + WS + r"\(", b) is None
            and re.search(r"#define" + WS + r"NVMTL_PRE_QUEUED_MAX" + WS + r"512\b", lib) is not None)


def pool_qos(s):
    b = code(body(s["pool"], "static inline void *nvmtl_pool_main(void *arg)"))
    return (re.search(r"pthread_set_qos_class_self_np" + WS + r"\(" + WS + r"QOS_CLASS_UTILITY\b", b) is not None
            and "QOS_CLASS_BACKGROUND" not in b)


BARRIER = (r"if" + WS + r"\(" + WS + r"\(" + WS + r"_parent" + WS + r"\?" + WS + r"_parent" + WS + ":" + WS + r"self" + WS
           + r"\)" + WS + r"->" + WS + r"_surf" + WS + r"\)" + WS + r"nvmtl_vk_image_upload_barrier" + WS + r"\(" + WS
           + r"(\[" + WS + r"self" + WS + r"nvi" + WS + r"\]|im)" + WS + r"\)" + WS + ";" + WS + r"\[" + WS + r"self" + WS
           + r"nvmtlSurfaceOut" + WS + r"\]")


def barrier_in(s, head):
    b = code(body(s["obj"], head))
    w = b.find("nvmtl_vk_image_write_region_level_layer")
    m = re.search(BARRIER, b)
    # the barrier is the statement right before the only surface-out, which comes after the upload
    return w >= 0 and m is not None and m.start() > w and len(re.findall(r"\[" + WS + r"self" + WS + r"nvmtlSurfaceOut"
                                                                         + WS + r"\]", b)) == 1


REPLACE = "- (void)replaceRegion:(MTLRegion)region mipmapLevel:(NSUInteger)level withBytes:"
REPLACE_SLICE = "- (void)replaceRegion:(MTLRegion)region mipmapLevel:(NSUInteger)level slice:"


def barrier_replace(s):
    return barrier_in(s, REPLACE)


def barrier_replace_slice(s):
    return barrier_in(s, REPLACE_SLICE)


WRITE = "int nvmtl_vk_image_write_region_level_layer(nvk_queue *q, nvk_image *img"


def room(s):
    b = code(body(s["vk"], WRITE))
    first = re.search(r"if" + WS + r"\(" + WS + r"finish_first" + WS + r"\)" + WS + r"nvmtl_asyncup_finish" + WS + r"\("
                      + WS + r"img" + WS + "," + WS + r"nvmtl_asyncup_take" + WS + r"\(" + WS + r"img" + WS + r"\)", b)
    r = re.search(r"if" + WS + r"\(" + WS + r"nvmtl_up_room" + WS + r"\(" + WS + r"held" + WS + "," + WS + r"held_bytes" + WS
                  + "," + WS + r"bytes" + WS + r"\)" + WS + r"\)" + WS + r"\{", b)
    alloc = re.search(r"au" + WS + "=" + WS + r"\(" + WS + r"nvmtl_asyncup" + WS + r"\*" + WS + r"\)" + WS + r"calloc", b)
    loads = re.search(r"held" + WS + "=" + WS + r"atomic_load" + WS + r"\(" + WS + r"&" + WS + r"g_asyncup_held" + WS
                      + r"\)" + WS + "," + WS + r"held_bytes" + WS + "=" + WS + r"atomic_load" + WS + r"\(" + WS + r"&"
                      + WS + r"g_asyncup_held_bytes" + WS + r"\)", b)
    return (first is not None and r is not None and alloc is not None and loads is not None
            and first.start() < loads.start() < r.start() < alloc.start() and len(re.findall(r"calloc", b)) == 1)


def counting(s):
    st = re.sub(r"\s+", "", code(body(s["vk"], "static uint64_t nvmtl_asyncup_store(nvk_image *img, uint64_t token)")))
    fi = re.sub(r"\s+", "", code(body(s["vk"], "static void nvmtl_asyncup_finish(nvk_image *img, uint64_t token)")))
    return ("atomic_fetch_add(&g_asyncup_held,1);" in st and "atomic_fetch_add(&g_asyncup_held_bytes,au->bytes);" in st
            and "atomic_fetch_sub(&g_asyncup_held,1);" in fi and "atomic_fetch_sub(&g_asyncup_held_bytes,au->bytes);" in fi
            and "au->bytes=bytes;" in re.sub(r"\s+", "", code(body(s["vk"], WRITE))))


def hand_back(s):
    st = re.sub(r"\s+", "", code(body(s["vk"], "static uint64_t nvmtl_asyncup_store(nvk_image *img, uint64_t token)")))
    lk, st_set, un = st.find("pthread_mutex_lock(&g_asyncup_mutex);"), st.find("constuint64_told=nvmtl_up_set(&s,token);"), \
        st.find("pthread_mutex_unlock(&g_asyncup_mutex);")
    wr = re.sub(r"\s+", "", code(body(s["vk"], WRITE)))
    return (0 <= lk < st_set < un and "returnold;}" in st
            and "nvmtl_asyncup_finish(img,nvmtl_asyncup_store(img,(uint64_t)(uintptr_t)au));" in wr
            and wr.count("nvmtl_asyncup_store(") == 1)


CHECKS = [
    ("nvmtl_async_tex_upload decides with nvmtl_async_tex_upload_default(e, getprogname())", async_reader, [
        ("vk", "on = nvmtl_async_tex_upload_default(e, getprogname());", "on = e && e[0] == '1';"),
        ("vk", "on = nvmtl_async_tex_upload_default(e, getprogname());", "on = nvmtl_async_tex_upload_default(e, NULL);"),
    ]),
    ("nvmtl_pretranslate_on decides with nvmtl_pretranslate_default(...) and nvmtl_process_is_game()",
     pretranslate_reader, [
        ("lib", "on = nvmtl_pretranslate_default(e, e ? 0 : nvmtl_process_is_game());", "on = e && !strcmp(e, \"1\");"),
        ("lib", "on = nvmtl_pretranslate_default(e, e ? 0 : nvmtl_process_is_game());",
         "on = nvmtl_pretranslate_default(e, 0);"),
    ]),
    ("nvmtl_pretranslate_library queues through the 512-job bound and counts a refused job as skipped",
     pretranslate_bounded, [
        ("lib", "nvmtl_pool_submit_bounded(nvmtl_pre_pool(), nvmtl_pre_run, j, NVMTL_PRE_QUEUED_MAX)",
         "nvmtl_pool_submit(nvmtl_pre_pool(), nvmtl_pre_run, j)"),
        ("lib", "#define NVMTL_PRE_QUEUED_MAX 512", "#define NVMTL_PRE_QUEUED_MAX 100000"),
        ("lib", "atomic_fetch_add(&gPreSkipped, 1); refused++;", "refused++;"),
    ]),
    ("the pool's workers run at utility QoS, not background", pool_qos, [
        ("pool", "QOS_CLASS_UTILITY, 0);", "QOS_CLASS_BACKGROUND, 0);"),
    ]),
    ("replaceRegion finishes an IOSurface texture's upload right before its surface-out", barrier_replace, [
        ("obj", "if ((_parent ? _parent : self)->_surf) nvmtl_vk_image_upload_barrier([self nvi]);\n", ""),
        ("obj", "if ((_parent ? _parent : self)->_surf) nvmtl_vk_image_upload_barrier([self nvi]);\n"
                "        [self nvmtlSurfaceOut]; nvmtl_mip_autofill(self, bytes, row, region, level);",
         "[self nvmtlSurfaceOut]; nvmtl_mip_autofill(self, bytes, row, region, level);\n"
         "        if ((_parent ? _parent : self)->_surf) nvmtl_vk_image_upload_barrier([self nvi]);"),
    ]),
    ("replaceRegion:slice: does the same", barrier_replace_slice, [
        ("obj", "if ((_parent ? _parent : self)->_surf) nvmtl_vk_image_upload_barrier(im);\n", ""),
        ("obj", "if ((_parent ? _parent : self)->_surf) nvmtl_vk_image_upload_barrier(im);",
         "if ((_parent ? _parent : self)->_surf) (void)im;"),
    ]),
    ("the upload asks nvmtl_up_room after the finish-first and before it makes the asynchronous record", room, [
        ("vk", "if (nvmtl_up_room(held, held_bytes, bytes)) {", "if (1) {"),
        ("vk", "        if (finish_first) nvmtl_asyncup_finish(img, nvmtl_asyncup_take(img));\n", ""),
    ]),
    ("an upload is counted in by the store and out by the finish", counting, [
        ("vk", "    atomic_fetch_sub(&g_asyncup_held, 1);", ""),
        ("vk", "    atomic_fetch_add(&g_asyncup_held_bytes, au->bytes);\n", ""),
        ("vk", "            else au->bytes = bytes;", ""),
    ]),
    ("the store hands back the replaced handle under the lock and the upload finishes it", hand_back, [
        ("vk", "    const uint64_t old = nvmtl_up_set(&s, token);", "    nvmtl_up_set(&s, token); const uint64_t old = 0;"),
        ("vk", "nvmtl_asyncup_finish(img, nvmtl_asyncup_store(img, (uint64_t)(uintptr_t)au));",
         "(void)nvmtl_asyncup_store(img, (uint64_t)(uintptr_t)au);"),
        ("vk", "    return old;\n}", "    return 0;\n}"),
    ]),
]

for name, check, mutations in CHECKS:
    arm(name, check(SRC))
    for key, old, new in mutations:
        if SRC[key].count(old) != 1:
            arm("  control text is still in the source exactly once: " + old.strip()[:60], False)
            continue
        damaged = dict(SRC)
        damaged[key] = SRC[key].replace(old, new)
        arm("  control fails it: " + (old.strip()[:50] + " -> " + (new.strip()[:40] or "(removed)")), not check(damaged))

# whitespace is not the code: the same text with its spacing changed still passes
spaced = {k: re.sub(r"\(", "( ", v) for k, v in SRC.items()}
arm("every check tolerates changed spacing", all(check(spaced) for _, check, _ in CHECKS))

print(f"{fails} failed")
sys.exit(1 if fails else 0)
