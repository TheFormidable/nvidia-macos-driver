#!/usr/bin/env python3
"""The plugin glue around 3.0.1's two speed defaults, where only the source can be checked here.

tools/test_switch_defaults.c holds the rules of nvmtl_switch_defaults.h, tools/test_tex_upload.c the rules of
nvmtl_tex_upload.h and tools/test_pretranslate.c the pool. This test holds the calls those cannot reach:

  async reader   nvmtl_async_tex_upload in plugin/nvmtl_vk.c decides with
                 nvmtl_async_tex_upload_default(e, getprogname())
  pretranslate   nvmtl_pretranslate_on in plugin/NVMTLLibraryLoad.m decides with nvmtl_pretranslate_default(...) given
                 nvmtl_process_is_game(); nvmtl_pretranslate_library queues through the bounded submit (512 jobs) and
                 counts a refused job as skipped after releasing what it retained
  the pool       its workers run at QOS_CLASS_UTILITY, never QOS_CLASS_BACKGROUND
  surface-out    both replaceRegion methods of plugin/NVMTLObjects.m finish the upload of an IOSurface texture
                 (nvmtl_vk_image_upload_barrier) right before [self nvmtlSurfaceOut]
  the room       nvmtl_vk_image_write_region_level_layer asks nvmtl_up_room after the finish-first and before it makes
                 the asynchronous record
  the counts     the store counts an upload in before it publishes the handle, and every finish counts it out
                 unconditionally
  one way out    every stored record is finished by nvmtl_asyncup_finish (barrier, drain, finish-first, hand-back)
                 or released by the reaper's sweep (nvmtl_asyncup_sweep, held by tools/test_up_reap.py), and the
                 failure paths before the store free their record without finishing (it was never counted)
  the hand-back  nvmtl_asyncup_store claims the handle nvmtl_up_set replaced, under the lock, and the upload
                 finishes what the claim hands back outside it: nothing is finished or waited for while
                 g_asyncup_mutex is held

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
    pat = "".join(r"\s*" + re.escape(c) + r"\s*" if c in "(),*:" else r"\s+" if c == " " else re.escape(c)
                  for c in head)
    found = re.search(pat, text)
    if not found:
        return ""
    i = found.start()
    j = text.find("\n}\n", i)
    return text[i:j + 2] if j >= 0 else ""


WS = r"\s*"


def seq(*tokens):
    """A regular expression for these tokens in this order, with any whitespace between them."""
    return WS.join(re.escape(t) for t in tokens)


def async_reader(s):
    b = code(body(s["vk"], "static int nvmtl_async_tex_upload(void)"))
    return re.search(r"\b" + seq("on", "=", "nvmtl_async_tex_upload_default", "(", "e", ",", "getprogname", "(", ")",
                                 ")", ";"), b) is not None


def pretranslate_reader(s):
    b = code(body(s["lib"], "static int nvmtl_pretranslate_on(void)"))
    return re.search(r"\bon" + WS + "=" + WS + r"nvmtl_pretranslate_default" + WS + r"\([^;]*"
                     r"\bnvmtl_process_is_game" + WS + r"\(" + WS + r"\)[^;]*\)" + WS + ";", b) is not None


def pretranslate_bounded(s):
    lib = code(s["lib"])
    b = code(body(s["lib"], "void nvmtl_pretranslate_library(NVMTLLibrary *lib)"))
    m = re.search(seq("nvmtl_pool_submit_bounded", "(", "nvmtl_pre_pool", "(", ")", ",", "nvmtl_pre_run", ",", "j", ",",
                      "NVMTL_PRE_QUEUED_MAX", ")"), b)
    # a refused job releases both of its retains and frees itself, is counted as skipped, and the loop goes on
    refused = re.search(seq("CFRelease", "(", "j->lib", ")", ";", "CFRelease", "(", "j->name", ")", ";",
                            "free", "(", "j", ")") + r"[^}]*NVMTL_POOL_FULL[^}]*gPreSkipped[^}]*;"
                        + WS + seq("continue", ";"), b, re.S)
    return (m is not None and refused is not None and re.search(r"nvmtl_pool_submit" + WS + r"\(", b) is None
            and re.search(r"#define" + WS + r"NVMTL_PRE_QUEUED_MAX" + WS + r"512\b", lib) is not None)


def pool_qos(s):
    b = code(body(s["pool"], "static inline void *nvmtl_pool_main(void *arg)"))
    return (re.search(r"pthread_set_qos_class_self_np" + WS + r"\(" + WS + r"QOS_CLASS_UTILITY\b", b) is not None
            and "QOS_CLASS_BACKGROUND" not in b)


BARRIER = (seq("if", "(", "(", "_parent", "?", "_parent", ":", "self", ")", "->", "_surf", ")",
               "nvmtl_vk_image_upload_barrier", "(") + WS + "(" + seq("[", "self", "nvi", "]") + "|im)" + WS
           + seq(")", ";", "[", "self", "nvmtlSurfaceOut", "]"))


def barrier_in(s, head):
    b = code(body(s["obj"], head))
    w = b.find("nvmtl_vk_image_write_region_level_layer")
    m = re.search(BARRIER, b)
    # the barrier is the statement right before the only surface-out, which comes after the upload
    outs = re.findall(seq("[", "self", "nvmtlSurfaceOut", "]"), b)
    return w >= 0 and m is not None and m.start() > w and len(outs) == 1


REPLACE = "- (void)replaceRegion:(MTLRegion)region mipmapLevel:(NSUInteger)level withBytes:"
REPLACE_SLICE = "- (void)replaceRegion:(MTLRegion)region mipmapLevel:(NSUInteger)level slice:"


def barrier_replace(s):
    return barrier_in(s, REPLACE)


def barrier_replace_slice(s):
    return barrier_in(s, REPLACE_SLICE)


WRITE = "int nvmtl_vk_image_write_region_level_layer(nvk_queue *q, nvk_image *img"


def room(s):
    b = code(body(s["vk"], WRITE))
    first = re.search(seq("if", "(", "finish_first", ")", "nvmtl_asyncup_finish", "(", "img", ",", "nvmtl_asyncup_take",
                          "(", "img", ")"), b)
    r = re.search(seq("if", "(", "nvmtl_up_room", "(", "held", ",", "held_bytes", ",", "bytes", ")", ")", "{"), b)
    alloc = re.search(seq("au", "=", "(", "nvmtl_asyncup", "*", ")", "calloc"), b)
    loads = re.search(seq("held", "=", "atomic_load", "(", "&", "g_asyncup_held", ")", ",", "held_bytes", "=",
                          "atomic_load", "(", "&", "g_asyncup_held_bytes", ")"), b)
    return (first is not None and r is not None and alloc is not None and loads is not None
            and first.start() < loads.start() < r.start() < alloc.start() and len(re.findall(r"calloc", b)) == 1)


def compact(text):
    """The code without comments (whole-line and trailing) and without any whitespace."""
    return re.sub(r"\s+", "", re.sub(r"(?<![:\"])//[^\n]*", "", text))


STORE = "static uint64_t nvmtl_asyncup_store(nvk_image *img, uint64_t token)"
FINISH = "static void nvmtl_asyncup_finish(nvk_image *img, uint64_t token)"


def counting(s):
    st = compact(body(s["vk"], STORE))
    fi = compact(body(s["vk"], FINISH))
    # the count is raised before the handle is published under the lock, so a finish on another thread that takes it
    # can never lower the count first (a wrapped, huge count would turn every upload synchronous)
    add = st.find("atomic_fetch_add(&g_asyncup_held,1);atomic_fetch_add(&g_asyncup_held_bytes,au->bytes);")
    lock = st.find("pthread_mutex_lock(&g_asyncup_mutex);")
    # the finish lowers it on every path, the drain of a dying image (img NULL) and a redone copy included:
    # unconditional statements right after the redo decision and right before the record is freed
    out = ("nvmtl_asyncup_redo(img,au);atomic_fetch_sub(&g_asyncup_held,1);"
           "atomic_fetch_sub(&g_asyncup_held_bytes,au->bytes);nvmtl_vk_buffer_destroy(&au->stage);free(au);}")
    return (0 <= add < lock and fi.endswith(out) and fi.count("atomic_fetch_sub(") == 2
            and "au->bytes=bytes;" in compact(body(s["vk"], WRITE)))


def one_way_out(s):
    """Every stored record leaves through nvmtl_asyncup_finish, and nothing that was never stored does."""
    vk = compact(s["vk"])
    wr = compact(body(s["vk"], WRITE))
    callers = ("voidnvmtl_vk_image_upload_barrier(nvk_image*img){"
               "nvmtl_asyncup_finish(img,nvmtl_asyncup_take(img));}",
               "staticvoidnvmtl_vk_image_upload_drain(nvk_image*img){"
               "nvmtl_asyncup_finish(NULL,nvmtl_asyncup_take(img));}")
    # the upload itself finishes only what it took (finish-first) or what the store handed back; its failure paths
    # before the store free their own record and must not finish it (it was never counted in)
    sw = compact(body(s["vk"], "static void nvmtl_asyncup_sweep(void)"))
    return (all(c in vk for c in callers) and vk.count("nvmtl_asyncup_finish(") == 5
            and wr.count("nvmtl_asyncup_finish(") == 2 and vk.count("nvmtl_vk_submit_finish(&au->infl)") == 1
            and vk.count("nvmtl_vk_submit_finish(&b->infl)") == 1 and sw.count("nvmtl_vk_submit_finish(&b->infl)") == 1
            and wr.count("free(au);") == 3)


def no_wait_under_lock(s):
    """Neither the store nor the take finishes or waits for anything while g_asyncup_mutex is held."""
    ok = True
    for head in (STORE, "static uint64_t nvmtl_asyncup_take(nvk_image *img)"):
        b = compact(body(s["vk"], head))
        lk, un = b.find("pthread_mutex_lock(&g_asyncup_mutex);"), b.find("pthread_mutex_unlock(&g_asyncup_mutex);")
        inside = b[lk:un] if 0 <= lk < un else ""
        ok = ok and bool(inside) and not re.search(r"finish|wait|redo|free\(|claimed\(", inside)
    return ok


def hand_back(s):
    st = compact(body(s["vk"], STORE))
    lk = st.find("pthread_mutex_lock(&g_asyncup_mutex);")
    st_set = st.find("constuint64_told=nvmtl_up_set(&s,(uint64_t)(uintptr_t)au->rec);")
    claim = st.find("nvmtl_up_claim(&g_asyncup_fifo,(nvmtl_up_rec*)(uintptr_t)old)")
    un = st.find("pthread_mutex_unlock(&g_asyncup_mutex);")
    wr = compact(body(s["vk"], WRITE))
    return (0 <= lk < st_set < claim < un and "returnnvmtl_asyncup_claimed(old,kind);}" in st
            and "nvmtl_asyncup_finish(img,nvmtl_asyncup_store(img,(uint64_t)(uintptr_t)au));" in wr
            and wr.count("nvmtl_asyncup_store(") == 1)


CHECKS = [
    ("nvmtl_async_tex_upload decides with nvmtl_async_tex_upload_default(e, getprogname())", async_reader, [
        ("vk", "on = nvmtl_async_tex_upload_default(e, getprogname());", "on = e && e[0] == '1';"),
        ("vk", "on = nvmtl_async_tex_upload_default(e, getprogname());",
         "on = nvmtl_async_tex_upload_default(e, NULL);"),
    ]),
    ("nvmtl_pretranslate_on decides with nvmtl_pretranslate_default(...) and nvmtl_process_is_game()",
     pretranslate_reader, [
        ("lib", "on = nvmtl_pretranslate_default(e, e ? 0 : nvmtl_process_is_game());",
         "on = e && !strcmp(e, \"1\");"),
        ("lib", "on = nvmtl_pretranslate_default(e, e ? 0 : nvmtl_process_is_game());",
         "on = nvmtl_pretranslate_default(e, 0);"),
    ]),
    ("nvmtl_pretranslate_library queues through the 512-job bound and counts a refused job as skipped",
     pretranslate_bounded, [
        ("lib", "nvmtl_pool_submit_bounded(nvmtl_pre_pool(), nvmtl_pre_run, j, NVMTL_PRE_QUEUED_MAX)",
         "nvmtl_pool_submit(nvmtl_pre_pool(), nvmtl_pre_run, j)"),
        ("lib", "#define NVMTL_PRE_QUEUED_MAX 512", "#define NVMTL_PRE_QUEUED_MAX 100000"),
        ("lib", "atomic_fetch_add(&gPreSkipped, 1); refused++;", "refused++;"),
        ("lib", "CFRelease(j->lib); CFRelease(j->name); free(j);\n            if (rc",
         "CFRelease(j->lib); free(j);\n            if (rc"),
        ("lib", "refused++;\n            continue;", "refused++;\n            break;"),
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
        ("vk", "    atomic_fetch_sub(&g_asyncup_held, 1);   // every handle finished here was counted in by nvmtl_asyncup_store", ""),
        ("vk", "    atomic_fetch_add(&g_asyncup_held_bytes, au->bytes);\n", ""),
        ("vk", "            else au->bytes = bytes;", ""),
        ("vk", "    atomic_fetch_sub(&g_asyncup_held, 1);   //",
         "    if (img) atomic_fetch_sub(&g_asyncup_held, 1);   //"),
        ("vk", "    atomic_fetch_add(&g_asyncup_held, 1);\n    atomic_fetch_add(&g_asyncup_held_bytes, au->bytes);\n"
               "    pthread_mutex_lock(&g_asyncup_mutex);",
         "    pthread_mutex_lock(&g_asyncup_mutex);\n    atomic_fetch_add(&g_asyncup_held, 1);\n"
         "    atomic_fetch_add(&g_asyncup_held_bytes, au->bytes);"),
    ]),
    ("every stored upload leaves through nvmtl_asyncup_finish, and a record never stored does not", one_way_out, [
        ("vk", "{ nvmtl_vk_buffer_destroy(&au->stage); free(au->rec); free(au); return -1; }",
         "{ nvmtl_asyncup_finish(img, (uint64_t)(uintptr_t)au); return -1; }"),
        ("vk", "        (void)nvmtl_vk_submit_finish(&b->infl);", "        (void)b;"),
        ("vk", "{ nvmtl_asyncup_finish(NULL, nvmtl_asyncup_take(img)); }",
         "{ uint64_t t = nvmtl_asyncup_take(img); if (t) { nvmtl_asyncup *au = (nvmtl_asyncup *)(uintptr_t)t; "
         "nvmtl_vk_submit_finish(&au->infl); nvmtl_vk_buffer_destroy(&au->stage); free(au); } }"),
        ("vk", "        if (finish_first) nvmtl_asyncup_finish(img, nvmtl_asyncup_take(img));\n",
         "        if (finish_first) nvmtl_asyncup_finish(img, nvmtl_asyncup_take(img));\n"
         "        nvmtl_asyncup_finish(img, nvmtl_asyncup_take(img));\n"),
    ]),
    ("the store and the take neither finish nor wait while they hold g_asyncup_mutex", no_wait_under_lock, [
        ("vk", "    const uint64_t old = nvmtl_up_set(&s, (uint64_t)(uintptr_t)au->rec);",
         "    const uint64_t old = nvmtl_up_set(&s, (uint64_t)(uintptr_t)au->rec);\n"
         "    if (old) nvmtl_vk_submit_finish(&((nvmtl_asyncup *)(uintptr_t)old)->infl);"),
        ("vk", "    img->up_token = 0;\n    const nvmtl_up_claim_kind kind",
         "    img->up_token = 0;\n    nvmtl_asyncup_finish(img, token);\n    const nvmtl_up_claim_kind kind"),
        ("vk", "    pthread_mutex_unlock(&g_asyncup_mutex);\n    return nvmtl_asyncup_claimed(old, kind);",
         "    const uint64_t back = nvmtl_asyncup_claimed(old, kind);\n    pthread_mutex_unlock(&g_asyncup_mutex);\n    return back;"),
    ]),
    ("the store hands back the replaced handle under the lock and the upload finishes it", hand_back, [
        ("vk", "    const uint64_t old = nvmtl_up_set(&s, (uint64_t)(uintptr_t)au->rec);",
         "    nvmtl_up_set(&s, (uint64_t)(uintptr_t)au->rec); const uint64_t old = 0;"),
        ("vk", "nvmtl_asyncup_finish(img, nvmtl_asyncup_store(img, (uint64_t)(uintptr_t)au));",
         "(void)nvmtl_asyncup_store(img, (uint64_t)(uintptr_t)au);"),
        ("vk", "    return nvmtl_asyncup_claimed(old, kind);\n}", "    return 0;\n}"),
        ("vk", "    const nvmtl_up_claim_kind kind = old ? nvmtl_up_claim(&g_asyncup_fifo, (nvmtl_up_rec *)(uintptr_t)old) : NVMTL_UP_CLAIM_FINISH;\n", "    const nvmtl_up_claim_kind kind = NVMTL_UP_CLAIM_FINISH;\n"),
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
        arm("  control fails it: " + (old.strip()[:50] + " -> " + (new.strip()[:40] or "(removed)")),
            not check(damaged))

# whitespace is not the code: the same text with its spacing changed still passes
spaced = {k: re.sub(r"\(", "( ", v) for k, v in SRC.items()}
arm("every check tolerates changed spacing", all(check(spaced) for _, check, _ in CHECKS))

print(f"{fails} failed")
sys.exit(1 if fails else 0)
