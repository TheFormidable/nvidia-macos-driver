#!/usr/bin/env python3
"""The plugin glue of the reaper of finished asynchronous texture uploads (plugin/nvmtl_up_reap.h), where only the source
can be checked here. tools/test_up_reap.c holds the rules of the header; this holds the calls in plugin/nvmtl_vk.c:

  the switch     nvmtl_asyncup_reap_on reads NVMTL_ASYNCUP_REAP once and decides with nvmtl_up_reap_default(e, getprogname())
  the list       the store puts the record's header on the list only when the reaper is on, and claims the handle it
                 replaced under the lock; the take claims under the lock; what a claim leaves is handled after the unlock
  the poll       the sweep's poll waits with a zero timeout and tells a dropped copy apart; only the collect runs under
                 the lock
  the release    the sweep finishes the submission, counts the upload out of both counts, destroys the staging and frees
                 the record outside the lock, then marks the header released under it and frees an orphan
  the place      the upload sweeps after its finish-first and before it reads the counts for the room
  the header     every path that frees a record frees its header too, except the sweep, which leaves it to the image

Each check also runs on damaged copies of the text, which must fail it (the controls).
Run: python3 tools/test_up_reap.py [root]"""
import pathlib
import re
import sys

R = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1])
VK = (R / "plugin/nvmtl_vk.c").read_text()
fails = 0


def arm(name, ok):
    global fails
    print(("ok   " if ok else "FAIL ") + name)
    fails += not ok


def compact(text):
    """The code without comments (whole-line and trailing) and without any whitespace."""
    return re.sub(r"\s+", "", re.sub(r"(?<![:\"])//[^\n]*", "", text))


def body(text, head):
    """Compacted, from the line that starts with `head` to the first lone closing brace, or '' when absent."""
    pat = "".join(r"\s*" + re.escape(c) + r"\s*" if c in "(),*:" else r"\s+" if c == " " else re.escape(c)
                  for c in head)
    found = re.search(pat, text)
    if not found:
        return ""
    j = text.find("\n}\n", found.start())
    return compact(text[found.start():j + 2]) if j >= 0 else ""


def locked(b):
    """The parts of a compacted body between each lock and the next unlock of g_asyncup_mutex."""
    return re.findall(r"pthread_mutex_lock\(&g_asyncup_mutex\);(.*?)pthread_mutex_unlock\(&g_asyncup_mutex\);", b)


def switch(vk):
    b = body(vk, "static int nvmtl_asyncup_reap_on(void)")
    return ("staticinton=-1;if(on<0){" in b and 'constchar*e=getenv("NVMTL_ASYNCUP_REAP");' in b
            and "on=nvmtl_up_reap_default(e,getprogname());" in b)


def the_list(vk):
    st = body(vk, "static uint64_t nvmtl_asyncup_store(nvk_image *img, uint64_t token)")
    tk = body(vk, "static uint64_t nvmtl_asyncup_take(nvk_image *img)")
    ls, lt = locked(st), locked(tk)
    return (len(ls) == 1 and len(lt) == 1
            and "if(reap)nvmtl_up_fifo_push(&g_asyncup_fifo,au->rec);" in ls[0]
            and "nvmtl_up_claim(&g_asyncup_fifo,(nvmtl_up_rec*)(uintptr_t)old)" in ls[0]
            and "nvmtl_up_claim(&g_asyncup_fifo,(nvmtl_up_rec*)(uintptr_t)token)" in lt[0]
            and st.endswith("pthread_mutex_unlock(&g_asyncup_mutex);returnnvmtl_asyncup_claimed(old,kind);}")
            and tk.endswith("pthread_mutex_unlock(&g_asyncup_mutex);returnnvmtl_asyncup_claimed(token,kind);}")
            and vk.count("nvmtl_up_fifo_push(") == 1)


def the_poll(vk):
    pr = body(vk, "static int nvmtl_asyncup_probe(void *ctx, void *body)")
    sw = body(vk, "static void nvmtl_asyncup_sweep(void)")
    ls = locked(sw)
    return ("pvkWaitForFences(g_dev,1,&fence,VK_TRUE,0)!=VK_SUCCESS)returnNVMTL_UP_PROBE_PENDING;" in pr
            and "g_state!=1||" in pr
            and "nvmtl_drop_seen(pnvk_fence_dropped(g_dev,fence))?NVMTL_UP_PROBE_DROPPED:NVMTL_UP_PROBE_RAN" in pr
            and len(ls) == 2
            and ls[0] == "constunsignedn=nvmtl_up_sweep_collect(&g_asyncup_fifo,NVMTL_UP_SWEEP_MAX,nvmtl_asyncup_probe,NULL,got);"
            and ls[1] == "constintorphan=nvmtl_up_release_done(got[i]);"
            and "if(!nvmtl_asyncup_reap_on()||!atomic_load(&g_asyncup_held))return;" in sw)


def the_release(vk):
    sw = body(vk, "static void nvmtl_asyncup_sweep(void)")
    rel = ("(void)nvmtl_vk_submit_finish(&b->infl);atomic_fetch_sub(&g_asyncup_held,1);"
           "atomic_fetch_sub(&g_asyncup_held_bytes,b->bytes);nvmtl_vk_buffer_destroy(&b->stage);free(b);"
           "pthread_mutex_lock(&g_asyncup_mutex);constintorphan=nvmtl_up_release_done(got[i]);"
           "pthread_mutex_unlock(&g_asyncup_mutex);if(orphan)free(got[i]);")
    return rel in sw and "nvmtl_asyncup_redo" not in sw and sw.count("free(") == 2


WRITE = "int nvmtl_vk_image_write_region_level_layer(nvk_queue *q, nvk_image *img"


def the_place(vk):
    wr = body(vk, WRITE)
    first = wr.find("if(finish_first)nvmtl_asyncup_finish(img,nvmtl_asyncup_take(img));")
    sweep = wr.find("nvmtl_asyncup_sweep();")
    loads = wr.find("constuint64_theld=atomic_load(&g_asyncup_held)")
    return 0 <= first < sweep < loads and wr.count("nvmtl_asyncup_sweep(") == 1 and compact(vk).count("nvmtl_asyncup_sweep()") == 1


def the_header(vk):
    wr = body(vk, WRITE)
    fi = body(vk, "static void nvmtl_asyncup_finish(nvk_image *img, uint64_t token)")
    cl = body(vk, "static uint64_t nvmtl_asyncup_claimed(uint64_t handle, nvmtl_up_claim_kind kind)")
    return (wr.count("free(au->rec);free(au);") == 3 and "if(au)free(au->rec);free(au);au=NULL;" in wr
            and "if(!au||!(au->rec=nvmtl_asyncup_rec_new(au))||nvmtl_vk_buffer_create(bytes,1,&au->stage))" in wr
            and fi.find("free(au->rec);") >= 0 and fi.find("free(au->rec);") < fi.find("nvmtl_vk_submit_finish(")
            and "if(kind==NVMTL_UP_CLAIM_FREE){free(r);return0;}" in cl
            and "if(kind==NVMTL_UP_CLAIM_LEFT)return0;" in cl and cl.endswith("return(uint64_t)(uintptr_t)r->body;}"))


CHECKS = [
    ("NVMTL_ASYNCUP_REAP is read once and decided by nvmtl_up_reap_default(e, getprogname())", switch, [
        ("on = nvmtl_up_reap_default(e, getprogname());", "on = 1;"),
        ("on = nvmtl_up_reap_default(e, getprogname());", "on = nvmtl_up_reap_default(e, NULL);"),
    ]),
    ("the store lists only with the reaper on, the store and the take claim under the lock and act after it", the_list, [
        ("    if (reap) nvmtl_up_fifo_push(&g_asyncup_fifo, au->rec);\n", ""),
        ("    if (reap) nvmtl_up_fifo_push(&g_asyncup_fifo, au->rec);", "    nvmtl_up_fifo_push(&g_asyncup_fifo, au->rec);"),
        ("    pthread_mutex_unlock(&g_asyncup_mutex);\n    return nvmtl_asyncup_claimed(token, kind);",
         "    const uint64_t back = nvmtl_asyncup_claimed(token, kind);\n    pthread_mutex_unlock(&g_asyncup_mutex);\n    return back;"),
        ("const nvmtl_up_claim_kind kind = token ? nvmtl_up_claim(&g_asyncup_fifo, (nvmtl_up_rec *)(uintptr_t)token) : NVMTL_UP_CLAIM_FINISH;",
         "const nvmtl_up_claim_kind kind = NVMTL_UP_CLAIM_FINISH;"),
    ]),
    ("the sweep polls with a zero timeout, tells a dropped copy apart, and holds the lock only to collect and mark", the_poll, [
        ("pvkWaitForFences(g_dev, 1, &fence, VK_TRUE, 0) != VK_SUCCESS", "pvkWaitForFences(g_dev, 1, &fence, VK_TRUE, UINT64_MAX) != VK_SUCCESS"),
        ("pnvk_fence_dropped && nvmtl_drop_seen(pnvk_fence_dropped(g_dev, fence)) ? NVMTL_UP_PROBE_DROPPED : NVMTL_UP_PROBE_RAN",
         "NVMTL_UP_PROBE_RAN"),
        ("        (void)nvmtl_vk_submit_finish(&b->infl);   //",
         "        pthread_mutex_lock(&g_asyncup_mutex); (void)nvmtl_vk_submit_finish(&b->infl); pthread_mutex_unlock(&g_asyncup_mutex);   //"),
        ("    if (!nvmtl_asyncup_reap_on() || !atomic_load(&g_asyncup_held)) return;\n", ""),
    ]),
    ("the sweep releases as a take would, outside the lock, and leaves the header to its image", the_release, [
        ("        atomic_fetch_sub(&g_asyncup_held_bytes, b->bytes);\n", ""),
        ("        nvmtl_vk_buffer_destroy(&b->stage);\n", ""),
        ("        if (orphan) free(got[i]);", "        free(got[i]);"),
        ("        if (orphan) free(got[i]);", "        (void)orphan;"),
    ]),
    ("the upload sweeps after its finish-first and before it reads the counts for the room", the_place, [
        ("        nvmtl_asyncup_sweep();\n", ""),
    ]),
    ("a record's header is freed with it everywhere but in the sweep", the_header, [
        ("    free(au->rec);   // no image names it any more", "    // no image names it any more"),
        ("{ nvmtl_vk_buffer_destroy(&au->stage); free(au->rec); free(au); return -1; }",
         "{ nvmtl_vk_buffer_destroy(&au->stage); free(au); return -1; }"),
        ("    if (kind == NVMTL_UP_CLAIM_FREE) { free(r); return 0; }", "    if (kind == NVMTL_UP_CLAIM_FREE) return 0;"),
        ("    if (kind == NVMTL_UP_CLAIM_LEFT) return 0;\n", ""),
    ]),
]

for name, check, mutations in CHECKS:
    arm(name, check(VK))
    for old, new in mutations:
        if VK.count(old) != 1:
            arm("  control text is still in the source exactly once: " + old.strip()[:60], False)
            continue
        arm("  control fails it: " + old.strip()[:50] + " -> " + (new.strip()[:40] or "(removed)"),
            not check(VK.replace(old, new)))

spaced = re.sub(r"\(", "( ", VK)
arm("every check tolerates changed spacing", all(check(spaced) for _, check, _ in CHECKS))
print(f"{fails} failed")
sys.exit(1 if fails else 0)
