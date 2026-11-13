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
                 while the image is still in the layout that copy left (nothing used it for writing since), and
                 never for an image that is being freed
  Metal          -1 completes the command buffer with an error and is not quarantined; the error completion closes the
                 surface fence holds first (nvmtl_busy_mark_done), so a hold armed on dropped work cannot stay open

Each source check also runs on a damaged copy of the text, which must fail it (the controls).
Run: python3 tools/test_channel_recover_plugin.py [root]"""
import pathlib, re, sys

R = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1])
VK = (R / "plugin/nvmtl_vk.c").read_text()
EV = (R / "plugin/NVMTLEventSync.m").read_text()
LOSS = (R / "plugin/nvmtl_loss.h").read_text()
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
run("Metal: -1 completes with an error, is not quarantined, and the fence holds are closed first", chk_metal, (EV,), [
    ("the holds closed after the marks", (sub(EV, "    nvmtl_vk_fence_close_all(&cb->_fence);   // every command buffer, every process, also after an error\n    NSDictionary *marks = cb->_busyMarks;\n",
                                               "    NSDictionary *marks = cb->_busyMarks;\n    nvmtl_vk_fence_close_all(&cb->_fence);\n"),)),
    ("every failed part quarantined", (sub(EV, "if (result == -2) {\n            if (!nvmtl_quarantined_parts) nvmtl_quarantined_parts = [NSMutableArray new];\n            [nvmtl_quarantined_parts addObject:part];",
                                           "if (result) {\n            if (!nvmtl_quarantined_parts) nvmtl_quarantined_parts = [NSMutableArray new];\n            [nvmtl_quarantined_parts addObject:part];"),)),
    ("every failed ahead part quarantined at the finish", (sub(EV, "if (result == -2) {\n            if (!nvmtl_quarantined_parts) nvmtl_quarantined_parts = [NSMutableArray new];\n            [nvmtl_quarantined_parts addObject:p];",
                                                               "if (result < 0) {\n            if (!nvmtl_quarantined_parts) nvmtl_quarantined_parts = [NSMutableArray new];\n            [nvmtl_quarantined_parts addObject:p];"),)),
])
print(f"{'FAILED' if fails else 'PASS'}: {fails} failed")
sys.exit(1 if fails else 0)
