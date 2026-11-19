#!/usr/bin/env python3
"""The text of NVMTL_SURFIN_ZERO and NVMTL_WS_FREE_OFFMAIN in the plugin, where only the source can be checked here.

tools/test_surfin_zero.py runs nvmtlSurfaceIn and the zero flush in a fixture; tools/test_zero_scan.c and
tools/test_ws_capture.c hold the pure rules. This test holds the glue those cannot reach:

  surface-in   the _zeroLost exchange is the first statement in the surface monitor; the zero try comes after
               IOSurfaceLock( and before the upload; the VRAM tag is not published for a zero fill
  the try      refuses a client-written surface and a second scan; the owed insert comes before `return 1`; it reads
               no VRAM tag
  the flush    runs first in nvmtl_presubmit; sets both prologue hooks and holds its set; the fallback releases its set
               on another thread (dispatch_async_f), never inline
  the redo     nvmtl_pre_redo marks a texture before its NSArray test and still says nothing of fills
  End failed   nvmtl_vk_submit_begin_at, where NVMTL_SURFIN_ZERO is on, moves a failed prologue's holds aside, and
               after the submit mutex is released calls the redo hook on each before its release; where it is off the
               branch is 3.0's (holds dropped under the mutex, no redo, 3.0's log line)
  the readers  both readers of NVMTL_SURFIN_ZERO use nvmtl_surfin_zero_switch with NVMTL_PRESUBMIT_PROLOGUE
  the free     in nvmtl_vk_buffer_destroy the off-main call is inside the `now` case, after nvmtl_pl_release;
               nvmtl_ws_free_offmain decides with nvmtl_free_offmain_ok over map, pool, placed and bind_lost, records
               the death (nvmtl_live_remove) before the enqueue, and the body counts its frees atomically
  dealloc      the texture's order (surface image, VRAM import, CFRelease of the surface) is unchanged
  the rows     each switch's getenv literal has its row in tools/switches.tsv

Each check also runs on a damaged copy of the text, which must fail it (the controls).
Run: python3 tools/test_surfin_zero_plugin.py [root]"""
import pathlib
import sys

R = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1])
OBJ = (R / "plugin/NVMTLObjects.m").read_text()
VK = (R / "plugin/nvmtl_vk.c").read_text()
TSV = (R / "tools/switches.tsv").read_text()
fails = 0


def arm(name, ok):
    global fails
    print(("ok   " if ok else "FAIL ") + name)
    fails += not ok


def body(src, head, end="\n}\n"):
    i = src.find(head)
    if i < 0:
        return ""
    j = src.find(end, i)
    return src[i:j + len(end)] if j >= 0 else ""


def order(text, *parts):
    at = -1
    for p in parts:
        at = text.find(p, at + 1)
        if at < 0:
            return False
    return True


def sub(text, a, b):
    assert text.count(a) == 1, f"control text not found once: {a!r}"
    return text.replace(a, b)


def chk_surfin(obj):
    m = body(obj, "- (void)nvmtlSurfaceIn {", "\n- (BOOL)nvmtlSurfaceOut {")
    mon = "@synchronized (nvmtl_surface_sync(r)) {\n"
    i = m.find(mon)
    first = m[i + len(mon):].lstrip().split("\n")[0] if i >= 0 else ""
    if first.startswith("//"):
        first = m[i + len(mon):].lstrip().split("\n")[1].strip()
    return (first.startswith("if (__atomic_exchange_n(&r->_zeroLost, 0, __ATOMIC_ACQ_REL)) { r->_surfSynced = NO;")
            and order(m, "IOSurfaceLock(r->_surf, kIOSurfaceLockReadOnly, &seed)", "nvmtl_surfin_zero_try(r, base, row)",
                      "nvmtl_vk_image_write_host(", "IOSurfaceUnlock(")
            and "if (rc && base && row && nvmtl_surfin_import()) {" in m
            and "if (direct && !zeroed) nvmtl_vram_tag_set(r->_surf, r->_plane, seed);" in m
            and m.count("nvmtl_vram_tag_set(") == 1)


def chk_try(obj):
    t = body(obj, "static int nvmtl_surfin_zero_try(")
    return (bool(t) and "r->_clientWritten" in t.split(") return 0;")[0] and "r->_zeroTried ||" in t
            and "nvmtl_vram_tag" not in t
            and order(t, "r->_zeroTried = YES;", "nvmtl_zero_rows_at(", "[gSurfZeroOwed addObject:r];",
                      "nvmtl_pre_submit_hook = nvmtl_presubmit;", "return 1;")
            and t.count("return 1;") == 1)


def chk_flush(obj):
    f = body(obj, "static void nvmtl_surf_zero_flush(void) {")
    pre = body(obj, "static void nvmtl_presubmit(void) {")
    fallback = f[f.find("} else if (todo.count) {"):] if "} else if (todo.count) {" in f else ""
    return (order(pre, "nvmtl_surf_zero_flush();", "nvmtl_zero_flush();", "nvmtl_managed_flush();")
            and "nvmtl_pre_release_hook = nvmtl_pre_release; nvmtl_pre_redo_hook = nvmtl_pre_redo;" in f
            and "nvmtl_vk_pre_hold((__bridge_retained void *)todo);" in f
            and "dispatch_async_f(" in fallback and "nvmtl_pre_release(keep)" not in fallback
            and "[[NSArray alloc] initWithArray:gSurfZeroOwed.allObjects]" in f)


def chk_redo(obj):
    r = body(obj, "static void nvmtl_pre_redo(void *held) {")
    return (order(r, "if ([e isKindOfClass:[NVMTLTexture class]]) {", "->_zeroLost, 1, __ATOMIC_RELEASE); continue; }",
                  "if (![e isKindOfClass:[NSArray class]] || [(NSArray *)e count] != 3) continue;",
                  "nvmtl_managed_mark(b, lo, hi);")
            and "fill" not in r.split("{", 1)[-1] and "@synchronized" not in r)


def chk_endfail(vk):
    b = body(vk, "int nvmtl_vk_submit_begin_at(nvk_cmdbuf *c, nvmtl_inflight *f, const char *who)")
    lock = b.find("pthread_mutex_lock(&nvmtl_submit_mutex);")
    unlock = b.find("pthread_mutex_unlock(&nvmtl_submit_mutex);")
    inside, after = b[lock:unlock], b[unlock:]
    gate = "else if (!nvmtl_vk_surfin_zero_on()) {"
    g = inside.find(gate)
    e = inside.find("} else {", g)
    off, on = (inside[g:e], inside[e:]) if g >= 0 and e > g else ("", "")
    old = ('if (pre->n) { g_pre_fail++; nvlog("vk: pre-submit prologue End -> %d - %u uploads/fills '
           '(%llu bytes) LOST, "\n'
           '                                              "the GPU keeps the old bytes", pr, pre->n, pre->bytes); }\n'
           '            nvmtl_cmd_retire(&pre->cb); nvmtl_pre_drop(pre); pre->begun = 0;\n')
    return (lock >= 0 and unlock > lock
            and order(inside, "if (pre->n && pr == VK_SUCCESS) cbs[ncb++] = pre->cb.cb;", gate, "} else {")
            and old in off and "lost_" not in off and "redo" not in off.replace("no redo", "")
            and order(on, "if (pre->n) {", "g_pre_fail++;", "lost_redo = 1;", "}",
                      "for (unsigned i = 0; i < pre->nhold; i++) lost_hold[nlost++] = pre->hold[i];",
                      "pre->nhold = 0;")
            and "nvmtl_pre_drop(pre)" not in on and "nvmtl_pre_redo_hook(" not in inside
            and order(after, "if (lost_hold[i] && lost_redo && nvmtl_pre_redo_hook) nvmtl_pre_redo_hook(lost_hold[i]);",
                      "if (lost_hold[i] && nvmtl_pre_release_hook) nvmtl_pre_release_hook(lost_hold[i]);")
            and after.index("nvmtl_pre_release_hook(lost_hold[i])") < after.index("if (result != VK_SUCCESS) {"))


def chk_readers(obj, vk):
    o = body(obj, "static int nvmtl_surfin_zero_on(void) {")
    v = body(vk, "static int nvmtl_vk_surfin_zero_on(void)\n{")
    return (order(o, 'getenv("NVMTL_SURFIN_ZERO")', 'getenv("NVMTL_PRESUBMIT_PROLOGUE")',
                  "nvmtl_surfin_zero_switch(e, p, ")
            and ('nvmtl_surfin_zero_switch(getenv("NVMTL_SURFIN_ZERO"), getenv("NVMTL_PRESUBMIT_PROLOGUE"), '
                 'getprogname())') in v)


def chk_free(vk):
    d = body(vk, "void nvmtl_vk_buffer_destroy(nvk_buffer *b)\n{")
    w = body(vk, "static int nvmtl_ws_free_offmain(nvmtl_bgrace *g)")
    run = body(vk, "static void nvmtl_free_offmain_run(void *p)")
    db = body(vk, "static void nvmtl_vk_buffer_destroy_body(nvk_buffer *b)\n{")
    return (order(d, "const int now = nvmtl_pl_release(", "pthread_mutex_unlock(&g_bg_lock);",
                  "if (now && nvmtl_ws_free_offmain(g)) {", "if (now) { const int was = t_grace_reaping; t_grace_reaping = 1;")
            and order(w, "nvmtl_ws_free_offmain_on()", "nvmtl_free_offmain_ok(1, g->b.imported, g->b.map != NULL, "
                      "g->b.pool != NULL, g->b.placed,", "g->b.bind_lost,", "nvmtl_live_remove(&g->b);",
                      "dispatch_async_f(q, g, nvmtl_free_offmain_run);")
            and order(run, "const int was = t_grace_reaping;",
                      "t_grace_reaping = 1; nvmtl_vk_buffer_destroy_body(&g->b); t_grace_reaping = was;", "free(g);")
            and "__atomic_add_fetch(&g_buf_gone, 1, __ATOMIC_RELAXED);" in db and "g_buf_gone++" not in db)


def chk_dealloc(obj):
    d = body(obj, "- (void)dealloc {\n    // the heap slot goes back")
    return order(d, "if (!_parent && _surfaceLinear.img) nvmtl_vk_image_destroy(&_surfaceLinear);",
                 "if (!_parent && _vramOn) { if (!nvmtl_ic_put(&_vramBuf)) nvmtl_vk_buffer_destroy(&_vramBuf);",
                 "if (_surfOwned && _surf) CFRelease(_surf);")


def chk_rows(obj, vk, tsv):
    names = ["NVMTL_SURFIN_ZERO", "NVMTL_WS_FREE_OFFMAIN"]
    rows = {ln.split("\t")[1]: ln.split("\t") for ln in tsv.splitlines() if ln.startswith("env\t")}
    return (f'getenv("{names[0]}")' in obj and f'getenv("{names[1]}")' in vk
            and all(n in rows and rows[n][2] == "escape" and rows[n][3].startswith("on") and rows[n][4] == "3.0.1"
                    and rows[n][5] == "3.2" for n in names)
            and "NVMTL_WS_SURFACE_SHARE" not in rows and "NVMTL_WS_SURFACE_SHARE" not in obj + vk)


def run(name, check, args, controls):
    arm(name, check(*args))
    for what, bad in controls:
        arm(f"  control: {what} is caught", not check(*bad))


run("surface-in: the lost mark first, the zero try under the lock before the upload, no tag for a fill", chk_surfin,
    (OBJ,), [
        ("the tag set for a zero fill too", (sub(OBJ, "if (direct && !zeroed) nvmtl_vram_tag_set(", "if (direct) nvmtl_vram_tag_set("),)),
        ("the lost mark after the busy wait", (sub(sub(OBJ, "    if (__atomic_exchange_n(&r->_zeroLost, 0, __ATOMIC_ACQ_REL)) { r->_surfSynced = NO; r->_zeroTried = NO; }\n", ""),
                                                   "    if (r->_clientWritten) nvmtl_busy_wait(r->_surf, r->_plane);\n    // The GPU",
                                                   "    if (r->_clientWritten) nvmtl_busy_wait(r->_surf, r->_plane);\n    if (__atomic_exchange_n(&r->_zeroLost, 0, __ATOMIC_ACQ_REL)) { r->_surfSynced = NO; r->_zeroTried = NO; }\n    // The GPU"),)),
        ("an upload made over a zero answer", (sub(OBJ, "if (rc && base && row && nvmtl_surfin_import()) {", "if (base && row && nvmtl_surfin_import()) {"),)),
    ])
run("the try: client-written refused, one scan per root, the owed insert before its answer, no tag read", chk_try,
    (OBJ,), [
        ("a client-written surface allowed", (sub(OBJ, " || r->_clientWritten || r->_plane", " || r->_plane"),)),
        ("the insert after the answer", (sub(OBJ, "    @autoreleasepool { [gSurfZeroOwed addObject:r]; }\n", ""),)),
    ])
run("the flush: first in presubmit, both hooks, held; the fallback releases off this thread", chk_flush, (OBJ,), [
    ("the fallback releasing inline", (sub(OBJ, "dispatch_async_f(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), keep, nvmtl_pre_release);",
                                           "nvmtl_pre_release(keep);"),)),
    ("no redo hook", (sub(OBJ, "        nvmtl_pre_release_hook = nvmtl_pre_release; nvmtl_pre_redo_hook = nvmtl_pre_redo;\n        nvmtl_vk_pre_hold((__bridge_retained void *)todo);\n    } else if (todo.count) {",
                          "        nvmtl_pre_release_hook = nvmtl_pre_release;\n        nvmtl_vk_pre_hold((__bridge_retained void *)todo);\n    } else if (todo.count) {"),)),
])
run("the redo: a texture is marked before the NSArray test, no fill named, no monitor", chk_redo, (OBJ,), [
    ("the texture not marked", (sub(OBJ, "__atomic_store_n(&((NVMTLTexture *)e)->_zeroLost, 1, __ATOMIC_RELEASE); continue; }", "continue; }"),)),
])
run("a prologue whose End failed: holds to the redo hook, then released, after the submit mutex", chk_endfail, (VK,), [
    ("the holds dropped inside the mutex", (sub(VK, "            nvmtl_cmd_retire(&pre->cb); pre->begun = 0;\n", "            nvmtl_cmd_retire(&pre->cb); nvmtl_pre_drop(pre); pre->begun = 0;\n"),)),
    ("lost_redo never set", (sub(VK, "                lost_redo = 1;\n", ""),)),
    ("the new path in every process",
     (sub(VK, "        else if (!nvmtl_vk_surfin_zero_on()) {", "        else if (0) {"),)),
    ("the 3.0 path redoing too",
     (sub(VK, "            nvmtl_cmd_retire(&pre->cb); nvmtl_pre_drop(pre); pre->begun = 0;\n",
          "            nvmtl_cmd_retire(&pre->cb); nvmtl_pre_redo_hook(pre->hold[0]); nvmtl_pre_drop(pre); "
          "pre->begun = 0;\n"),)),
    ("released before the redo", (sub(VK, "        if (lost_hold[i] && lost_redo && nvmtl_pre_redo_hook) nvmtl_pre_redo_hook(lost_hold[i]);\n        if (lost_hold[i] && nvmtl_pre_release_hook) nvmtl_pre_release_hook(lost_hold[i]);",
                                      "        if (lost_hold[i] && nvmtl_pre_release_hook) nvmtl_pre_release_hook(lost_hold[i]);\n        if (lost_hold[i] && lost_redo && nvmtl_pre_redo_hook) nvmtl_pre_redo_hook(lost_hold[i]);"),)),
])
run("both readers of NVMTL_SURFIN_ZERO: off with the prologue off", chk_readers, (OBJ, VK), [
    ("the surface-in reader ignoring the prologue", (sub(OBJ, "on = nvmtl_surfin_zero_switch(e, p, getprogname());",
                                                         "on = nvmtl_ws_capture_switch(e, getprogname());"), VK)),
    ("the End-fail reader ignoring the prologue", (OBJ, sub(VK, 'getenv("NVMTL_PRESUBMIT_PROLOGUE"), getprogname());',
                                                            'NULL, getprogname());'))),
])
run("the free: off main only in the `now` case, only a VRAM import, death recorded first, counted atomically", chk_free,
    (VK,), [
        ("off main before the release check", (sub(sub(VK, "            if (now && nvmtl_ws_free_offmain(g)) { nvmtl_perf_note(NVP_BUFDEL, t0, n); return; }\n", ""),
                                                   "            nvmtl_pl_stamp(&g->life, g_cmd_submitted);\n",
                                                   "            nvmtl_pl_stamp(&g->life, g_cmd_submitted);\n            if (nvmtl_ws_free_offmain(g)) { nvmtl_perf_note(NVP_BUFDEL, t0, n); return; }\n"),)),
        ("a host import allowed off main", (sub(VK, "g->b.map != NULL, g->b.pool != NULL", "0, g->b.pool != NULL"),)),
        ("the death recorded on the queue", (sub(VK, "    nvmtl_live_remove(&g->b);\n    uint64_t peak", "    uint64_t peak"),)),
        ("a plain count", (sub(VK, "__atomic_add_fetch(&g_buf_gone, 1, __ATOMIC_RELAXED);", "g_buf_gone++;"),)),
    ])
run("the texture's dealloc order is unchanged", chk_dealloc, (OBJ,), [
    ("the surface released before its import", (sub(sub(OBJ, "    if (_surfOwned && _surf) CFRelease(_surf);\n", ""),
                                                    "    if (!_parent && _surfaceLinear.img) nvmtl_vk_image_destroy(&_surfaceLinear);",
                                                    "    if (_surfOwned && _surf) CFRelease(_surf);\n    if (!_parent && _surfaceLinear.img) nvmtl_vk_image_destroy(&_surfaceLinear);"),)),
])
run("each switch has its row (escape, on, 3.0.1, review 3.2); the dropped share has none", chk_rows, (OBJ, VK, TSV), [
    ("a row missing", (OBJ, VK, "\n".join(ln for ln in TSV.splitlines() if "\tNVMTL_WS_FREE_OFFMAIN\t" not in ln))),
])
print(f"{'PASS' if not fails else 'FAIL'}: {fails} failed")
sys.exit(1 if fails else 0)
