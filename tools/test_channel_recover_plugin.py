#!/usr/bin/env python3
"""The plugin's side of NVK's channel recovery: a command buffer whose work NVK dropped is reported as failed, not lost.

NVK (nvk/nvk-channel-recover.patch) can rebuild a GPU channel that faulted or stalled instead of losing the device. The
one submission it did not run has its fence signalled from the CPU, and nvmtl_nvk_fence_dropped_v1 answers 1 for it.
The pure rules (what the answer means, the opt-out, -1 and never -2, the log lines) are host-tested by tools/test_loss.c.
This test holds what only the plugin's glue in plugin/nvmtl_vk.c and plugin/NVMTLEventSync.m can get wrong:

  the exports    both are found with dlsym by their names, through pointer types that are the contract's prototypes
                 (and, once nvk/nvk-channel-recover.patch is in the tree, the patch's definitions)
  the answer     nvmtl_vk_submit_finish asks after the device-loss branch (which stays the only -2) and before the
                 completion witness, only when the export exists, while the fence still exists; a dropped command
                 buffer is marked blind (no witness is waited for or counted missing) and the function's last
                 statement answers nvmtl_drop_result(dropped, nvmtl_drop_report_on())
  the switch     NVMTL_DROP_REPORT is read once, like its neighbours, and only a leading '0' turns the report off
  the loss       a dropped command buffer is not a loss: no g_state change, no nvmtl_vk_note_loss, no loss file; the
                 loss line names NVK's kind of loss when the statistics export exists
  the clock      the fence wait of nvmtl_vk_submit_finish is timed on awake time (CLOCK_UPTIME_RAW)
  the uploads    an async texture upload whose copy was dropped is made again from its staging buffer, but only
                 while the image is still in the layout that copy left (a render pass, clear or storage use since
                 leaves another one; a copy, blit or upload into it leaves the same one and is not seen), and
                 never for an image that is being freed; the managed-buffer uploads of a dropped pre-submit prologue
                 are marked dirty again (through the managed flush's own hook, before the prologue's holds are
                 released), its zero fills are not made again, and the drop is logged
  Metal          -1 completes the command buffer with an error and is not quarantined; the error completion closes the
                 surface fence holds first (nvmtl_busy_mark_done), so a hold armed on dropped work cannot stay open
  NVK's lines    the "nvk-reason:" lines (among them "nvrm channel slot N rebuilt after ..." and "... not rebuilt: ...")
                 go through the plugin's log rule (nvlog_every -> nvlog_line) with NVK's text as an argument, never as
                 the format, and the callback opens no file of its own; the release rule (nvmtl_log_failure.h, built
                 and run here with the host's cc) sends NVK's errors and both channel lines to the system log and no
                 other NVK warning, whatever words it holds

Each source check also runs on a damaged copy of the text, which must fail it (the controls).
Run: python3 tools/test_channel_recover_plugin.py [root]"""
import pathlib, re, shutil, subprocess, sys, tempfile

R = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1])
VK = (R / "plugin/nvmtl_vk.c").read_text()
EV = (R / "plugin/NVMTLEventSync.m").read_text()
OBJ = (R / "plugin/NVMTLObjects.m").read_text()
LOSS = (R / "plugin/nvmtl_loss.h").read_text()
DEV = (R / "plugin/NVMTLDevice.m").read_text()
FAILURE_H = (R / "plugin/nvmtl_log_failure.h").read_text()
PATCH = R / "nvk/nvk-channel-recover.patch"
fails = 0

# The prototypes of the contract between NVK and the plugin, character for character.
CONTRACT = {
    "nvmtl_nvk_fence_dropped_v1": ("int", "VkDevice device, VkFence fence"),
    "nvmtl_nvk_recover_stats_v1": ("unsigned", "uint64_t *out, unsigned n"),
}


def arm(name, ok):
    global fails
    print(("ok   " if ok else "FAIL ") + name)
    fails += not ok


def body(src, head):
    """The text of the function whose definition starts with `head`, up to its closing brace at column 0."""
    i = src.find(head)
    if i < 0:
        return ""
    j = src.find("\n}\n", i)
    return src[i:j + 2] if j >= 0 else ""


def types_only(params):
    """'uint64_t *out, unsigned n' -> 'uint64_t *, unsigned': the parameter list a function pointer type spells."""
    out = []
    for p in params.split(","):
        p = p.strip()
        m = re.match(r"^(.*?[\s*])(\w+)$", p)
        out.append((m.group(1) if m else p).strip())
    return ", ".join(out)


def chk_exports(vk):
    for name, (ret, params) in CONTRACT.items():
        cast = f"({ret} (*)({types_only(params)}))dlsym(g_lib, \"{name}\")"
        if vk.count(f"\"{name}\"") != 1 or cast not in vk:
            return False
    return ("static int (*pnvk_fence_dropped)(VkDevice, VkFence);" in vk
            and "static unsigned (*pnvk_rec_stats)(uint64_t *, unsigned);" in vk
            and "pnvk_fence_dropped = (int (*)(VkDevice, VkFence))dlsym(g_lib, \"nvmtl_nvk_fence_dropped_v1\");" in vk
            and "pnvk_rec_stats = (unsigned (*)(uint64_t *, unsigned))dlsym(g_lib, \"nvmtl_nvk_recover_stats_v1\");" in vk)


def chk_patch(text):
    """The patch defines and declares both exports with the contract's prototypes."""
    for name, (ret, params) in CONTRACT.items():
        if f"+{ret} {name}({params});" not in text or f"+{name}({params})\n+{{" not in text:
            return False
    return True


def chk_answer(vk):
    fin = body(vk, "int nvmtl_vk_submit_finish(nvmtl_inflight *f)")
    if not fin or fin.count("return -2;") != 1:
        return False
    ask = "const int dropped = pnvk_fence_dropped && nvmtl_drop_seen(pnvk_fence_dropped(g_dev, fence));"
    blind = "if (dropped) { c->wseq = NVMTL_WSEQ_BLIND; nvmtl_vk_note_dropped(waited_ms); }"
    if fin.count(ask) != 1 or fin.count(blind) != 1:
        return False
    a, b = fin.index(ask), fin.index(blind)
    return (fin.index("return -2;") < a < b
            and b < fin.index("if (c->wseq && c->wseq != NVMTL_WSEQ_BLIND)")
            and b < fin.index("pvkDestroyFence(g_dev, fence, NULL);")
            and fin.rstrip().endswith("return nvmtl_drop_result(dropped, nvmtl_drop_report_on());\n}")
            and len(re.findall(r"\breturn\b", fin)) == 2)


def chk_switch(vk, loss):
    f = body(vk, "static int nvmtl_drop_report_on(void)")
    return (vk.count("NVMTL_DROP_REPORT") == 1
            and 'if (on < 0) on = nvmtl_drop_report_parse(getenv("NVMTL_DROP_REPORT"));' in f
            and "static int on = -1;" in f
            and "static inline int nvmtl_drop_report_parse(const char *e) { return !(e && e[0] == '0'); }" in loss
            and "static inline int nvmtl_drop_result(int dropped, int report_on) { return dropped && report_on ? -1 : 0; }" in loss
            and "static inline int nvmtl_drop_seen(int answer) { return answer > 0; }" in loss)


def chk_not_loss(vk):
    nd = body(vk, "static void nvmtl_vk_note_dropped(long waited_ms)")
    nl = body(vk, "static void nvmtl_vk_note_loss(")
    return (bool(nd) and "g_state" not in nd and "nvmtl_vk_note_loss" not in nd and "nvmtl_fault_file_write" not in nd
            and "if (n == 1) nvmtl_fx_dump(" in nd and "nvmtl_drop_log_due(n)" in nd
            and vk.count("nvmtl_vk_note_loss(\"") == 2 and vk.count("nvmtl_fault_file_write(c);") == 1
            and "if (pnvk_rec_stats) {" in nl and "nvmtl_loss_recovery_line(cause, sizeof cause, st, n)" in nl
            and nl.index("nvlog(\"vk: %s\", cause);") < nl.index("if (pnvk_rec_stats) {"))


def chk_clock(vk):
    fin = body(vk, "int nvmtl_vk_submit_finish(nvmtl_inflight *f)")
    return "CLOCK_MONOTONIC" not in fin and fin.count("clock_gettime_nsec_np(CLOCK_UPTIME_RAW)") >= 3 \
        and "const uint64_t w0 = clock_gettime_nsec_np(CLOCK_UPTIME_RAW);" in fin


def chk_uploads(vk):
    fin = body(vk, "static void nvmtl_asyncup_finish(nvk_image *img, uint64_t token)")
    redo = body(vk, "static void nvmtl_asyncup_redo(nvk_image *img, nvmtl_asyncup *au)")
    wr = body(vk, "int nvmtl_vk_image_write_region_level_layer(")
    gone = body(vk, "static void nvmtl_vk_image_destroy_now(nvk_image *i)\n{")
    guard = "if (img->layout != VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL) {"
    copy = "pvkCmdCopyBufferToImage(c.cb, (VkBuffer)au->stage.buf, (VkImage)img->img, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &au->bic);"
    return ("if (nvmtl_vk_submit_finish(&au->infl) == -1 && img) nvmtl_asyncup_redo(img, au);" in fin
            and fin.index("nvmtl_asyncup_redo(") < fin.index("nvmtl_vk_buffer_destroy(&au->stage);")
            and copy in redo
            and guard in redo and redo.index(guard) < redo.index("nvmtl_vk_cmd_begin(") < redo.index(copy)
            and "static void nvmtl_vk_image_upload_drain(nvk_image *img) { nvmtl_asyncup_finish(NULL, nvmtl_asyncup_take(img)); }" in vk
            and "nvmtl_vk_image_upload_drain(i);" in gone and "nvmtl_vk_image_upload_barrier(" not in gone
            and "nvmtl_vk_submit_wait_at(&c, " in redo
            and "au->bic = bic;" in wr and wr.index("au->bic = bic;") < wr.index("nvmtl_vk_submit_begin_at(c, &au->infl")
            and "nvmtl_asyncup_finish(nvmtl_asyncup_take(" not in vk)


def chk_prologue(vk, obj):
    fin = body(vk, "int nvmtl_vk_submit_finish(nvmtl_inflight *f)")
    i = fin.find("    if (pre->begun) {\n")
    blk = fin[i:fin.find("\n    }\n", i)] if i >= 0 else ""
    mf = body(obj, "static void nvmtl_managed_flush(void) {")
    zf = body(obj, "static void nvmtl_zero_flush(void) {")
    redo = body(obj, "static void nvmtl_pre_redo(void *held) {")
    return (order(blk, "if (dropped && (pre->copies || pre->fills)) {", "nvmtl_pre_redo_hook(pre->hold[i]);",
                  "if (nvmtl_drop_log_due(n))",
                  'nvlog("vk: after a GPU fault, %u managed uploads and %u zero fills rode a dropped command buffer',
                  "nvmtl_pre_drop(pre);")
            and blk.count("nvmtl_pre_drop(pre);") == 1
            and "nvmtl_pre_release_hook = nvmtl_pre_release; nvmtl_pre_redo_hook = nvmtl_pre_redo;" in mf
            and "nvmtl_pre_redo_hook" not in zf
            # a zero flush's hold lists buffers, which have no `count`: the class is asked before the length
            and order(redo, "if (![e isKindOfClass:[NSArray class]] || [(NSArray *)e count] != 3) continue;",
                      "nvmtl_managed_mark(b, lo, hi);")
            and "fill" not in redo.split("{", 1)[-1])


def order(text, *parts):
    """Every part occurs in text, each after the one before it."""
    at = -1
    for p in parts:
        at = text.find(p, at + 1)
        if at < 0:
            return False
    return True


def chk_metal(ev):
    done = body(ev, "static void nvmtl_busy_mark_done(NVMTLCommandBuffer *cb) {")
    first = done.split("\n")[1].strip() if done.count("\n") > 1 else ""
    # every place a part is quarantined, and the condition it is quarantined under
    conds = re.findall(r"if \(([^)]*)\) \{\s*if \(!nvmtl_quarantined_parts\) nvmtl_quarantined_parts = \[NSMutableArray new\];\s*"
                       r"\[nvmtl_quarantined_parts addObject:\w+\];", ev)
    return (first.startswith("nvmtl_vk_fence_close_all(&cb->_fence);")
            and "nvmtl_busy_mark_done(self);" in ev
            and "code:MTLCommandBufferErrorInternal" in ev
            and len(conds) == ev.count("[nvmtl_quarantined_parts addObject:") == 2
            and all(c == "result == -2" for c in conds))


def run(name, check, args, damaged):
    arm(name, check(*args))
    for what, dargs in damaged:
        ok = not check(*dargs)
        arm(f"  control: {what} is caught", ok)


def sub(text, old, new):
    assert old in text, f"control anchor missing: {old[:60]}"
    return text.replace(old, new, 1)


run("both exports are found by name through the contract's prototypes", chk_exports, (VK,), [
    ("a stats pointer with the wrong argument types", (sub(VK, "(unsigned (*)(uint64_t *, unsigned))dlsym", "(unsigned (*)(uint32_t *, unsigned))dlsym"),)),
    ("a misspelled export name", (sub(VK, "\"nvmtl_nvk_fence_dropped_v1\"", "\"nvmtl_nvk_fence_drop_v1\""),)),
    ("the fence and device swapped", (sub(VK, "static int (*pnvk_fence_dropped)(VkDevice, VkFence);", "static int (*pnvk_fence_dropped)(VkFence, VkDevice);"),)),
])
if PATCH.exists():
    pt = PATCH.read_text()
    run("nvk-channel-recover.patch exports both with the same prototypes", chk_patch, (pt,), [
        ("an export with its arguments swapped", (pt.replace("nvmtl_nvk_fence_dropped_v1(VkDevice device, VkFence fence)", "nvmtl_nvk_fence_dropped_v1(VkFence fence, VkDevice device)"),)),
    ])
else:
    arm("nvk/nvk-channel-recover.patch is not in this tree: the prototypes cannot be compared with what NVK exports "
        "(expected until the NVK side of channel recovery is merged)", False)
run("a dropped command buffer is asked for after the loss branch, marked blind before the witness, and answered last",
    chk_answer, (VK,), [
        ("the question asked before the loss branch", (sub(sub(VK, "    const int dropped = pnvk_fence_dropped && nvmtl_drop_seen(pnvk_fence_dropped(g_dev, fence));\n", ""),
                                                            "    const long give_up_ms = budget_ms * 10;\n",
                                                            "    const long give_up_ms = budget_ms * 10;\n    const int dropped = pnvk_fence_dropped && nvmtl_drop_seen(pnvk_fence_dropped(g_dev, fence));\n"),)),
        ("a call without the NULL check", (sub(VK, "pnvk_fence_dropped && nvmtl_drop_seen(", "nvmtl_drop_seen("),)),
        ("no blind mark", (sub(VK, "c->wseq = NVMTL_WSEQ_BLIND; nvmtl_vk_note_dropped", "nvmtl_vk_note_dropped"),)),
        ("success answered for a drop", (sub(VK, "return nvmtl_drop_result(dropped, nvmtl_drop_report_on());", "return 0;"),)),
        ("an early return for a drop", (sub(VK, "nvmtl_vk_note_dropped(waited_ms); }", "nvmtl_vk_note_dropped(waited_ms); return -1; }"),)),
    ])
run("NVMTL_DROP_REPORT is read once and only a leading '0' turns the report off", chk_switch, (VK, LOSS), [
    ("an opt-in", (VK, sub(LOSS, "return !(e && e[0] == '0');", "return e && e[0] == '1';"))),
    ("a drop answered as a lost device", (VK, sub(LOSS, "return dropped && report_on ? -1 : 0;", "return dropped && report_on ? -2 : 0;"))),
    ("'not NVK's fence' taken as dropped", (VK, sub(LOSS, "return answer > 0;", "return answer != 0;"))),
    ("the switch read on every call", (sub(VK, "if (on < 0) on = nvmtl_drop_report_parse(", "on = nvmtl_drop_report_parse("), LOSS)),
])
run("a dropped command buffer is not a device loss; a loss names NVK's kind", chk_not_loss, (VK,), [
    ("a drop that disables the backend", (sub(VK, "    if (!nvmtl_drop_log_due(n)) return;\n", "    g_state = -1;\n    if (!nvmtl_drop_log_due(n)) return;\n"),)),
    ("a drop that writes the loss file", (sub(VK, "    if (n == 1) nvmtl_fx_dump(", "    nvmtl_fault_file_write(NULL);\n    if (n == 1) nvmtl_fx_dump("),)),
    ("a loss line without NVK's kind", (sub(VK, "if (nvmtl_loss_recovery_line(cause, sizeof cause, st, n)) nvlog(\"vk: %s\", cause);", ""),)),
])
run("the fence wait runs on awake time", chk_clock, (VK,), [
    ("the wall clock back at the start of the wait", (sub(VK, "    const uint64_t w0 = clock_gettime_nsec_np(CLOCK_UPTIME_RAW);\n    VkResult wr;",
                                                         "    const uint64_t w0 = clock_gettime_nsec_np(CLOCK_MONOTONIC_RAW);\n    VkResult wr;"),)),
])
run("an async texture upload whose copy was dropped is made again before its staging buffer goes", chk_uploads, (VK,), [
    ("the answer ignored", (sub(VK, "if (nvmtl_vk_submit_finish(&au->infl) == -1 && img) nvmtl_asyncup_redo(img, au);", "nvmtl_vk_submit_finish(&au->infl);"),)),
    ("the region not kept", (sub(VK, "        au->bic = bic;", "       "),)),
    ("made again over work that came after it", (sub(VK, "    if (img->layout != VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL) {\n", "    if (0) {\n"),)),
    ("made again for an image being freed", (sub(VK, "    nvmtl_vk_image_upload_drain(i);   // an async upload", "    nvmtl_vk_image_upload_barrier(i);   // an async upload"),)),
])
run("a dropped prologue's managed uploads are marked again before its holds go; its zero fills are not redone",
    chk_prologue, (VK, OBJ), [
        ("no mark again", (sub(VK, "if (pre->hold[i]) nvmtl_pre_redo_hook(pre->hold[i]);", ";"), OBJ)),
        ("the holds released before the mark", (sub(sub(VK, "        nvmtl_pre_drop(pre);\n        const unsigned long long k = g_pre_subs++;",
                                                        "        const unsigned long long k = g_pre_subs++;"),
                                                    "        pre->cb.wseq = c->wseq; nvmtl_cmd_retire(&pre->cb);\n",
                                                    "        pre->cb.wseq = c->wseq; nvmtl_cmd_retire(&pre->cb); nvmtl_pre_drop(pre);\n"), OBJ)),
        ("the hook set by the zero flush too", (VK, sub(OBJ, "        nvmtl_pre_release_hook = nvmtl_pre_release;\n        nvmtl_vk_pre_hold((__bridge_retained void *)todo);\n        const unsigned long long k",
                                                        "        nvmtl_pre_release_hook = nvmtl_pre_release; nvmtl_pre_redo_hook = nvmtl_pre_redo;\n        nvmtl_vk_pre_hold((__bridge_retained void *)todo);\n        const unsigned long long k"))),
        ("a zero flush's hold walked as managed ranges", (VK, sub(OBJ, "[(NSArray *)e count] != 3) continue;", "[(NSArray *)e count] < 1) continue;"))),
        ("a zero flush's buffers asked for a count", (VK, sub(OBJ, "if (![e isKindOfClass:[NSArray class]] || [(NSArray *)e count] != 3) continue;",
                                                          "if ([(NSArray *)e count] != 3) continue;"))),
        # without "fault" the release rule (nvmtl_log_failure.h) keeps the line out of the system log
        ("the line without the words a release build keeps", (sub(VK, 'nvlog("vk: after a GPU fault, %u managed', 'nvlog("vk: %u managed'), OBJ)),
    ])
run("Metal: -1 completes with an error, is not quarantined, and the fence holds are closed first", chk_metal, (EV,), [
    ("the holds closed after the marks", (sub(EV, "    nvmtl_vk_fence_close_all(&cb->_fence);   // every command buffer, every process, also after an error\n    NSDictionary *marks = cb->_busyMarks;\n",
                                               "    NSDictionary *marks = cb->_busyMarks;\n    nvmtl_vk_fence_close_all(&cb->_fence);\n"),)),
    ("every failed part quarantined", (sub(EV, "if (result == -2) {\n            if (!nvmtl_quarantined_parts) nvmtl_quarantined_parts = [NSMutableArray new];\n            [nvmtl_quarantined_parts addObject:part];",
                                           "if (result) {\n            if (!nvmtl_quarantined_parts) nvmtl_quarantined_parts = [NSMutableArray new];\n            [nvmtl_quarantined_parts addObject:part];"),)),
    ("every failed ahead part quarantined at the finish", (sub(EV, "if (result == -2) {\n            if (!nvmtl_quarantined_parts) nvmtl_quarantined_parts = [NSMutableArray new];\n            [nvmtl_quarantined_parts addObject:p];",
                                                               "if (result < 0) {\n            if (!nvmtl_quarantined_parts) nvmtl_quarantined_parts = [NSMutableArray new];\n            [nvmtl_quarantined_parts addObject:p];"),)),
])
# NVK's messages and the plugin's log rule.
def chk_vkmsg(vk):
    cb = body(vk, "static VkBool32 VKAPI_PTR nvmtl_vkmsg_cb(")
    calls = re.findall(r"\bnvlog\w*\s*\(", cb)
    return (bool(cb)
            and "fopen(" not in cb and "nvmtl_log_path" not in cb and "fprintf(" not in cb and "FILE" not in cb
            and 'snprintf(t, sizeof t, "nvk-reason: %s %s: %s",' in cb
            and "for (char *c = t; *c; c++) if ((unsigned char)*c < 0x20) *c = ' ';" in cb
            and calls == ["nvlog_every(", "nvlog_every("]
            # the message is the argument t; the format is a literal that holds the 2.9 file text
            and 'if (rep) nvlog_every("%s   [logged %llu times now%s]", t, (unsigned long long)rep, full ? ", table full" : "");' in cb
            and 'else     nvlog_every("%s%s", t, full ? "   [table full]" : "");' in cb
            and not re.search(r"nvlog_every\(\s*[^\s\"]", cb)
            and cb.index("*c = ' ';") < cb.index("nvlog_every("))


def chk_rule(dev):
    """nvlog_every adds no repeat limiter; nvlog_line is the release rule the documents describe."""
    every = body(dev, "void nvlog_every(const char *fmt, ...) {")
    line = body(dev, "static void nvlog_line(const char *t) {")
    rel = line.split("#else")[0] if "#else" in line else ""
    sl = 'if (nvmtl_log_failure(t)) syslog(LOG_ERR, "NullMoth: %s", t);'
    gate = "if (!nvmtl_process_env_on()) return;"
    wr = 'FILE *f = fopen(nvmtl_log_path(), "a"); if (f) { fprintf(f, "pid %d NVMTL: %s\\n", getpid(), t); fclose(f); }'
    return (bool(every) and "nvlog_dropped" not in every and "nvlog_line(t);" in every
            and "#if NVMTL_RELEASE" in line and sl in rel and gate in rel and rel.index(sl) < rel.index(gate)
            and line.count(wr) == 1 and line.index("#endif") < line.index(wr)
            and 'syslog(LOG_NOTICE, "NVMTL: %s", t);' in line.split("#else")[1].split("#endif")[0])


# What the release rule must answer: 1 = the system log ("NullMoth: ..."), 0 = not.
RULE_CASES = [
    ("nvk-reason: W nvkmd_nvrm_queue.c:1723: nvrm channel slot 3 rebuilt after a CHANNEL loss (Xid 31): 1 flush(es) dropped, 0 replayed", 1),
    ("nvk-reason: W nvkmd_nvrm_queue.c:1537: nvrm channel slot 12 not rebuilt: loss kind FIRMWARE (Xid 119), kind", 1),
    ("nvk-reason: W nvkmd_nvrm_queue.c:1537: nvrm channel slot 0 not rebuilt: loss kind STALL (Xid 0), off   [logged 4096 times now]", 1),
    ("nvk-reason: E nvkmd_nvrm_queue.c:308: NVRM channel faulted", 1),
    ("nvk-reason: E ?: (no message)   [table full]", 1),
    ("nvk-reason: W nvk_physical_device.c:900: VK_EXT_foo missing, the feature is unsupported and cannot be used", 0),
    ("nvk-reason: W nvk_queue.c:12: FAILED to keep a FAULT record, refused", 0),
    ("nvk-reason: W nvkmd_nvrm_queue.c:1: nvrm channel slot 3 rebuilt", 0),
    ("nvk-reason: W nvkmd_nvrm_queue.c:1: nvrm channel slot rebuilt after a CHANNEL loss", 0),
    ("nvk-reason: W nvkmd_nvrm_queue.c:1: the nvrm channel slot 3 rebuilt after a CHANNEL loss", 0),
    ("nvk-reason: W nvkmd_nvrm_queue.c:1: nvrm channel slot 3 was rebuilt after", 0),
    ("nvk-reason: X nvkmd_nvrm_queue.c:1: nvrm channel slot 3 rebuilt after a CHANNEL loss", 0),
    ("vk: vkCreateDevice -> -8", 1),
    ("vk: a channel rebuild dropped this command buffer; fault context follows", 1),
    ("vk: after a GPU fault, 3 managed uploads and 1 zero fills rode a dropped command buffer (1 such so far in WindowServer) - "
     "the uploads are marked to be made again at the next submit, the zero-filled buffers hold what VRAM held", 1),
    ("vk: 3 managed uploads and 1 zero fills rode a dropped command buffer (1 such so far in WindowServer) - "
     "the uploads are marked to be made again at the next submit, the zero-filled buffers hold what VRAM held", 0),
    ("vk: initialized successfully", 0),
    ('vk: NVK messages ON: its own warnings and errors land here as "nvk-reason:"', 0),
]
RULE_PROG = r"""
#include "nvmtl_log_failure.h"
#include <stdio.h>
int main(int argc, char **argv)
{
    for (int i = 1; i < argc; i++) printf("%d\n", nvmtl_log_failure(argv[i]) ? 1 : 0);
    printf("%d\n", nvmtl_log_failure(0) ? 1 : 0);
    return 0;
}
"""


def chk_failure_rule(header):
    """Builds a host program against `header` (the text of nvmtl_log_failure.h) and compares every case's answer."""
    cc = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
    if not cc:
        return False
    with tempfile.TemporaryDirectory(prefix="nullmoth-nvkmsg-") as td:
        d = pathlib.Path(td)
        (d / "nvmtl_log_failure.h").write_text(header)
        (d / "t.c").write_text(RULE_PROG)
        b = subprocess.run([cc, "-std=c11", "-Wall", "-Wextra", "-Werror", "-I", str(d), "-o", str(d / "t"), str(d / "t.c")],
                           capture_output=True, text=True)
        if b.returncode:
            return False
        r = subprocess.run([str(d / "t")] + [c for c, _ in RULE_CASES], capture_output=True, text=True, timeout=60)
    got = r.stdout.split()
    return r.returncode == 0 and got == [str(want) for _, want in RULE_CASES] + ["0"]


OLD_CB = """    char t[600]; snprintf(t, sizeof t, "nvk-reason: %s %s: %s", (sev & VK_DEBUG_UTILS_MESSAGE_SEVERITY_ERROR_BIT_EXT) ? "E" : "W", b, m);
    for (char *c = t; *c; c++) if ((unsigned char)*c < 0x20) *c = ' ';
    extern const char *nvmtl_log_path(void);
    FILE *f = fopen(nvmtl_log_path(), "a");
    if (f) {
        if (rep) fprintf(f, "pid %d NVMTL: %s   [logged %llu times now%s]\\n", getpid(), t, (unsigned long long)rep, full ? ", table full" : "");
        else     fprintf(f, "pid %d NVMTL: %s%s\\n", getpid(), t, full ? "   [table full]" : "");
        fclose(f);
    }
    return VK_FALSE;"""
NEW_CB_FROM = '    char t[584]; snprintf(t, sizeof t, "nvk-reason: %s %s: %s"'


def old_callback(vk):
    """The 2.9 body of the callback (it appended to nvmtl_log_path() itself) in place of today's."""
    i = vk.index(NEW_CB_FROM)
    j = vk.index("    return VK_FALSE;", i) + len("    return VK_FALSE;")
    return vk[:i] + OLD_CB + vk[j:]


run("NVK's lines go through the plugin's log rule with the message as an argument; the callback opens no file", chk_vkmsg, (VK,), [
    ("the 2.9 body that appends to nvmtl_log_path() itself", (old_callback(VK),)),
    ("NVK's text used as the format", (sub(VK, 'else     nvlog_every("%s%s", t, full ? "   [table full]" : "");', "else     nvlog_every(t);"),)),
    ("a second repeat limiter (nvlog)", (sub(VK, 'else     nvlog_every("%s%s", t,', 'else     nvlog("%s%s", t,'),)),
    ("control characters kept", (sub(VK, "    for (char *c = t; *c; c++) if ((unsigned char)*c < 0x20) *c = ' ';\n    // The plugin's log rule", "    // The plugin's log rule"),)),
    ("the repeat count dropped from the line", (sub(VK, 'nvlog_every("%s   [logged %llu times now%s]", t,', 'nvlog_every("%s%s", t,'),)),
])
run("nvlog_every adds no repeat limiter and nvlog_line is the documented release rule", chk_rule, (DEV,), [
    ("nvlog_every through the limiter", (sub(DEV, "    char t[640]; va_list a; va_start(a, fmt); vsnprintf(t, sizeof t, fmt, a); va_end(a);\n    nvlog_line(t);\n}",
                                               "    uint64_t rep; if (nvlog_dropped(fmt, &rep)) return;\n    char t[640]; va_list a; va_start(a, fmt); vsnprintf(t, sizeof t, fmt, a); va_end(a);\n    nvlog_line(t);\n}"),)),
    ("a release file for every process", (sub(DEV, "    if (!nvmtl_process_env_on()) return;   // the file below", "    // the file below"),)),
])
NVK_DISPATCH = "    if (text && !strncmp(text, NVMTL_NVK_REASON, sizeof NVMTL_NVK_REASON - 1)) return nvmtl_log_nvk_reason(text);\n"
run("release rule: NVK's errors and both channel lines reach the system log, no other NVK warning (host program)",
    chk_failure_rule, (FAILURE_H,), [
    ("NVK's lines judged by the plugin's words (the rule before this one)", (sub(FAILURE_H, NVK_DISPATCH, ""),)),
    ("every NVK warning kept", (sub(FAILURE_H, "    if (p[0] != 'W' || p[1] != ' ') return 0;\n", "    if (p[0] == 'W') return 1;\n"),)),
    ("the not-rebuilt line not recognised", (sub(FAILURE_H, ' || !strncmp(m, " not rebuilt: ", 14)', ""),)),
    ("the rebuilt line not recognised", (sub(FAILURE_H, '!strncmp(m, " rebuilt after ", 15) || ', ""),)),
    ("an NVK error not kept", (sub(FAILURE_H, "    if (p[0] == 'E' && p[1] == ' ') return 1;\n", ""),)),
    ("a channel line recognised anywhere in the message", (sub(FAILURE_H, "    if (strncmp(m, slot, sizeof slot - 1)) return 0;\n",
                                                               "    const char *at = strstr(m, slot); if (!at) return 0; m = at;\n"),)),
])
print(f"{'FAILED' if fails else 'PASS'}: {fails} failed")
sys.exit(1 if fails else 0)
