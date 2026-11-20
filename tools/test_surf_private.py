#!/usr/bin/env python3
"""test_surf_private.py - the wiring of plugin/nvmtl_surf_private.h in the plugin's sources (the rules themselves are
proved by tools/test_surf_private.c).

  - every single-plane surface that takes a private image, and every surface in the family's VRAM whose refused mark is
    copied out, goes through nvmtl_spv_note, which says each reason once (nvmtl_spv_first) and never in WindowServer;
  - the import reports why it failed (t_sv_why) on each of its failure returns;
  - a copy-out raises _surfOutBusy only when passable (NVMTL_SURFIN_SKIP_OWN_OUT, not WindowServer), before its
    IOSurface lock (nvmtl_surface_out_pass_begin), and lowers it again before every return after that, while it still
    holds the monitor (nvmtl_surface_out_pass_end);
  - the bind that passes it sits in nvmtl_surface_in_for_cb before nvmtlSurfaceIn, and not in nvmtlSurfaceIn itself (a
    CPU read or write of the texture still waits);
  - a texture without a surface returns from nvmtl_surface_in_for_cb and nvmtl_surface_cpu_dump before any work.
Each check also runs against a broken copy of the source, which it must refuse."""
import pathlib, re, sys

root = pathlib.Path(__file__).resolve().parent.parent
dev = (root / "plugin" / "NVMTLDevice.m").read_text()
obj = (root / "plugin" / "NVMTLObjects.m").read_text()
vk = (root / "plugin" / "nvmtl_vk.c").read_text()
fails = 0


def arm(what, ok):
    global fails
    print(("ok    " if ok else "FAIL  ") + what)
    if not ok:
        fails += 1


def block(src, start):
    """The text of the brace block that opens at or after `start` (a marker string)."""
    i = src.find(start)
    if i < 0:
        return ""
    j = src.index("{", i)
    depth = 0
    for k in range(j, len(src)):
        depth += (src[k] == "{") - (src[k] == "}")
        if depth == 0:
            return src[j:k + 1]
    return ""


def note_wired(d):
    f = block(d, "static int nvmtl_surface_image_create(")
    priv = f[f.find("private_image:"):]
    n = block(d, "static void nvmtl_spv_note(")
    return ("nvmtl_spv_note(nvmtl_spv_reason(" in priv and "nvmtl_spv_window_server()" in n
            and "nvmtl_spv_first(&g_spv_seen, reason)" in n and "nvlog_every(NVMTL_SPV_FORMAT" in n
            and f.count("vstage = NVMTL_SPV_STAGE_") >= 4)


def why_wired(v):
    f = block(v, "static int nvmtl_vk_surface_vram_words(")
    return all(("t_sv_why = %d;" % k) in f for k in (0, 1, 2, 3))


def out_wired(o):
    b = block(o, "static BOOL nvmtl_surface_out_pass_begin(")
    e = block(o, "static void nvmtl_surface_out_pass_end(")
    if ("nvmtl_spv_note(NVMTL_SPV_MARK_REFUSED" not in b or "__atomic_store_n(&r->_surfOutBusy, 1u" not in b
            or "nvmtl_spv_out_passable(ws ? 0 : nvmtl_surfin_skip_own_out_on(), ws," not in b
            or "if (passable) __atomic_store_n(&r->_surfOutBusy, 0u" not in e):
        return False
    m = block(o, "- (BOOL)nvmtlSurfaceOutDirtied:(BOOL)dirtied")
    up = m.find("const BOOL passable = nvmtl_surface_out_pass_begin(r);")
    lock = m.find("IOSurfaceLock(r->_surf, lockopt")
    if up < 0 or lock < up:
        return False
    # every return after the flag went up lowers it first, between the return before it (or the raise) and itself
    tail = m[up:]
    last = 0
    for r in re.finditer(r"\breturn\b", tail):
        if "nvmtl_surface_out_pass_end(r, passable);" not in tail[last:r.start()]:
            return False
        last = r.end()
    return True


def bind_wired(o):
    f = block(o, "static void nvmtl_surface_in_for_cb(")
    s = block(o, "- (void)nvmtlSurfaceIn {")
    a, b, c = f.find("if (!r->_surf && !t->_y422Surf) return;"), f.find("nvmtl_surface_bind_passes_out(r)"), f.find("[t nvmtlSurfaceIn]")
    sw = block(o, "static BOOL nvmtl_surfin_skip_own_out_on(void)")
    return (0 <= a < b < c and "nvmtl_surface_bind_passes_out" not in s and "!t->_y422Surf && nvmtl_surface_bind_passes_out" in f
            and 'getenv("NVMTL_SURFIN_SKIP_OWN_OUT")' in sw and "e[0] == '0'" in sw)


def dump_wired(o):
    f = block(o, "static void nvmtl_surface_cpu_dump(")
    a, b = f.find("if (!rt || !rt->_surf) return;"), f.find("clock_gettime_nsec_np")
    return 0 <= a < b


arm("each private single-plane surface goes through the once-per-reason note, never in WindowServer", note_wired(dev))
arm("a VRAM surface whose mark was refused is noted where it is copied out, and the copy-out's flag is kept balanced", out_wired(obj))
arm("the family VRAM import reports why it failed on each failure", why_wired(vk))
arm("binds pass the copy-out only in nvmtl_surface_in_for_cb, behind NVMTL_SURFIN_SKIP_OWN_OUT, after the no-surface return", bind_wired(obj))
arm("nvmtl_surface_cpu_dump returns for a texture without a surface before it reads the clock", dump_wired(obj))

# negative controls: each check refuses the mistake it is there for
arm("control: a note outside the private branch is refused", not note_wired(dev.replace("nvmtl_spv_note(nvmtl_spv_reason(", "nvmtl_spv_nope(nvmtl_spv_reason(")))
arm("control: a note without the once-per-reason bit is refused",
    not note_wired(dev.replace("nvmtl_spv_first(&g_spv_seen, reason)", "1")))
m_bad = obj.replace("""        nvmtl_surface_out_pass_end(r, passable);
        nvlog("G13 surface-out: readback FAILED""", """        nvlog("G13 surface-out: readback FAILED""")
arm("control: a failure return that leaves the flag up is refused", m_bad != obj and not out_wired(m_bad))
raise_line = "    const BOOL passable = nvmtl_surface_out_pass_begin(r);\n"
lock_fail = 'nvlog("G13 surface-out: IOSurfaceLock FAILED"); return NO; }\n'
late = obj.replace(raise_line, "", 1).replace(lock_fail, lock_fail + raise_line, 1)
arm("control: a flag raised after the IOSurface lock is refused", late != obj and not out_wired(late))
arm("control: WindowServer reading the switch is refused",
    not out_wired(obj.replace("nvmtl_spv_out_passable(ws ? 0 : nvmtl_surfin_skip_own_out_on(), ws,", "nvmtl_spv_out_passable(nvmtl_surfin_skip_own_out_on(), ws,")))
arm("control: an import failure that does not say why is refused", not why_wired(vk.replace("t_sv_why = 2;", "")))
arm("control: the pass check inside nvmtlSurfaceIn is refused",
    not bind_wired(obj.replace("    if (_y422Surf) { [self nvmtlY422SurfaceIn]; return; }",
                               "    if (_y422Surf) { [self nvmtlY422SurfaceIn]; return; }\n    if (nvmtl_surface_bind_passes_out(self)) return;")))
arm("control: the clock read before the no-surface return is refused",
    not dump_wired(obj.replace("    if (!rt || !rt->_surf) return;\n    static uint64_t next; static int on;",
                               "    static uint64_t next; static int on; (void)clock_gettime_nsec_np(CLOCK_MONOTONIC_RAW);\n    if (!rt || !rt->_surf) return;")))

print("FAILED %d" % fails if fails else "ok")
sys.exit(1 if fails else 0)
