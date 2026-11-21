#!/usr/bin/env python3
"""test_gl_diag.py: the wiring of the OpenGL path's once-per-process lines (plugin/nvmtl_gl_diag.h; the wording and the
vblank rule are checked by tools/test_gl_diag.c).

- nvmtl_gl_caps_patch (plugin/NVMTLGL.m): every return after the renderer's path matched goes through
  nvmtl_gl_caps_say, and so does the end of the function, so a process that loaded Apple's renderer always says which
  outcome it had; the path check itself says nothing (no line in processes without the renderer). Every outcome is
  reported somewhere, the device's feature is not tested there, and no write comes before the tables are compared.
- nvmtl_gl_caps_enable returns at once on a device without last-vertex provoking, as in 3.0.5: no callback is
  registered there, in any process.
- nvglvbl_synchronize (plugin/NVMTLGLVBlank.m): one decision through nvglvbl_decide, reported before either branch,
  and the corrected wait is taken on that decision alone; nothing is reported at install.
- nvlog_notice (plugin/NVMTLDevice.m): a release build sends a line to the system log as a notice only when the failure
  rule does not already send it, then writes it as every other line.
Negative controls: each check must fail on a copy of the source with the wiring broken."""
import re
import sys
from pathlib import Path

R = Path(__file__).resolve().parents[1]
GL = (R / "plugin/NVMTLGL.m").read_text()
VBL = (R / "plugin/NVMTLGLVBlank.m").read_text()
DEV = (R / "plugin/NVMTLDevice.m").read_text()

fails = 0


def arm(what, ok):
    global fails
    print(("ok    " if ok else "FAIL  ") + what)
    if not ok:
        fails += 1


def body(src, head):
    i = src.find(head)
    if i < 0:
        return ""
    j = src.index("{", i + len(head) - 1)
    depth = 0
    for k in range(j, len(src)):
        depth += {"{": 1, "}": -1}.get(src[k], 0)
        if depth == 0:
            return src[j + 1:k]
    return ""


OUTCOMES = ["PATCHED", "SET_BY_APPLE", "DIFFERS", "NO_SYMBOL", "NOT_WRITABLE"]


def chk_caps_patch(src):
    b = body(src, "static void nvmtl_gl_caps_patch(const struct mach_header *mh) {")
    if not b:
        return False
    code = re.sub(r"//[^\n]*", "", b)
    stmts = [s.strip() for s in re.split(r"[;{}]", code) if s.strip()]
    rets = [i for i, s in enumerate(stmts) if re.search(r"\breturn\b", s)]
    if not rets or "kGLRendererSuffix" not in stmts[rets[0]]:
        return False   # the first return is the path check
    path_end = rets[0]
    if any("nvmtl_gl_caps_say" in s for s in stmts[:path_end + 1]):
        return False   # nothing said before the image is known to be the renderer
    for r in rets[1:]:
        if not (stmts[r] == "return" and stmts[r - 1].startswith("nvmtl_gl_caps_say(")):
            return False
    if not stmts[-1].startswith("nvmtl_gl_caps_say(NVMTL_GL_CAPS_PATCHED"):
        return False   # falling off the end is the patched outcome
    said = re.findall(r"nvmtl_gl_caps_say\(NVMTL_GL_CAPS_(\w+)", b)
    if sorted(said) != sorted(OUTCOMES):
        return False   # each outcome exactly once
    write = b.find("caps[0] |=")
    return ("nvmtl_vk_have_provoking_last()" not in b and "NO_FEATURE" not in b
            and b.find("memcmp(caps, kGLNvidiaCaps1581") < b.find("vm_protect(") < write
            and "nvlog(" not in b)


def chk_caps_say(src):
    b = body(src, "static void nvmtl_gl_caps_say(nvmtl_gl_caps_outcome o, const uint8_t *caps, int kr) {")
    return (bool(b) and re.search(r"if \(atomic_exchange_explicit\(&said, 1, memory_order_relaxed\)\) return;", b) is not None
            and "nvmtl_gl_caps_text(" in b and 'nvlog_notice("%s", t);' in b
            and b.find("return;") < b.find("nvlog_notice("))


def chk_caps_enable(src):
    b = body(src, "void nvmtl_gl_caps_enable(void) {")
    gate = 'if (!nvmtl_vk_have_provoking_last()) return;'
    code = re.sub(r"//[^\n]*", "", b)
    return bool(b) and gate in code and code.index(gate) < code.index("_dyld_register_func_for_add_image(nvmtl_gl_image_added)") \
        and code.count("return") == 1 and "getprogname" not in code


def chk_vbl(src):
    b = body(src, "static void nvglvbl_synchronize(id self, SEL cmd)")
    if not b:
        return False
    first = "if (!queue || !gGLVBL.unconsumed(queue, 0, 0)) return;"
    dec = "const nvglvbl_decision decision = nvglvbl_decide(n, NVGLVBL_ENTRIES, period, t0, t1);"
    say = "nvglvbl_say(decision, n, period, t0, t1);"
    br = "if (decision == NVGLVBL_CORRECTED) {"
    fallback = "((void (*)(id, SEL))gGLVBL.synchronize)(self, cmd);"
    if not all(x in b for x in (first, dec, say, br, fallback)):
        return False
    return (b.index(first) < b.index(dec) < b.index(say) < b.index(br) < b.index("mach_wait_until(") < b.index(fallback)
            and b.count("nvglvbl_say(") == 1 and "info[1].host_time > info[0].host_time" not in b)


def chk_vbl_say(src):
    b = body(src, "static void nvglvbl_say(nvglvbl_decision d, uint32_t n, uint64_t period, uint64_t t0, uint64_t t1)")
    inst = body(src, "static void nvglvbl_install(void)")
    return (bool(b) and "atomic_exchange(&said, true)) return;" in b and "nvglvbl_text(" in b
            and 'nvlog_notice("%s", t);' in b and bool(inst) and "nvlog_notice" not in inst and "nvglvbl_say" not in inst)


def chk_notice(src):
    b = body(src, "void nvlog_notice(const char *fmt, ...) {")
    if not b or "#if NVMTL_RELEASE" not in b:
        return False
    rel = b.split("#if NVMTL_RELEASE")[1].split("#endif")[0]
    return ('if (!nvmtl_log_failure(t)) syslog(LOG_NOTICE, "NullMoth: %s", t);' in rel
            and b.strip().endswith("nvlog_line(t);") and "nvlog_dropped" not in b and b.count("syslog(") == 1)


checks = [
    ("every return of nvmtl_gl_caps_patch after the renderer matched reports its outcome, each outcome once", chk_caps_patch, "GL"),
    ("the provoking-vertex report is said once per process", chk_caps_say, "GL"),
    ("no callback is registered on a device without last-vertex provoking, in any process (as in 3.0.5)", chk_caps_enable, "GL"),
    ("the vblank wait decides once, reports before either branch, and branches on that decision", chk_vbl, "VBL"),
    ("the vblank report is said once per process, and not at install", chk_vbl_say, "VBL"),
    ("nvlog_notice sends a release line to the system log once, as a notice", chk_notice, "DEV"),
]
srcs = {"GL": GL, "VBL": VBL, "DEV": DEV}
for what, fn, key in checks:
    arm(what, fn(srcs[key]))

# negative controls: the same checks on sources with the wiring broken
broken = [
    ("a return that says nothing is caught", chk_caps_patch, GL.replace(
        "if (!caps) { nvmtl_gl_caps_say(NVMTL_GL_CAPS_NO_SYMBOL, NULL, 0); return; }", "if (!caps) return;")),
    ("a patched outcome that says nothing is caught", chk_caps_patch, GL.replace(
        "    nvmtl_gl_caps_say(NVMTL_GL_CAPS_PATCHED, kGLNvidiaCaps1581, 0);", "")),
    ("a feature test that says nothing inside the patch is caught", chk_caps_patch, GL.replace(
        "    uint8_t *caps = nvmtl_gl_local_symbol(", "    if (!nvmtl_vk_have_provoking_last()) return;\n    uint8_t *caps = nvmtl_gl_local_symbol(")),
    ("a callback registered without the feature outside WindowServer is caught", chk_caps_enable, GL.replace(
        "    if (!nvmtl_vk_have_provoking_last()) return;\n    static dispatch_once_t once;",
        '    if (!nvmtl_vk_have_provoking_last() && !strcmp(getprogname(), "WindowServer")) return;\n    static dispatch_once_t once;')),
    ("the old inline vblank rule is caught", chk_vbl, VBL.replace(
        "if (decision == NVGLVBL_CORRECTED) {", "if (n >= 2 && info[1].host_time > info[0].host_time) {")),
    ("a notice sent even when the failure rule sends it is caught", chk_notice, DEV.replace(
        'if (!nvmtl_log_failure(t)) syslog(LOG_NOTICE', 'syslog(LOG_NOTICE')),
]
for what, fn, src in broken:
    arm(what, src not in srcs.values() and not fn(src))

print("FAIL" if fails else "PASS")
sys.exit(1 if fails else 0)
