#!/usr/bin/env python3
"""The draw cache's wiring in the plugin (plugin/nvmtl_draw_cache.h; the decisions are proved by tools/test_draw_cache.c).

The cache may skip a bind only while it knows everything recorded into the VkCommandBuffer. This holds, from the
sources:
  every index buffer, vertex buffer, topology and restart call of the plugin is in nvmtl_vk.c, and each one either
  asks the cache or is the patch draw's, which tells the cache it set the topology behind it;
  a new VkCommandBuffer starts from a cleared nvk_cmdbuf (so a cleared cache), and the plugin records no secondary
  command buffers;
  NGX's feature setup and evaluation, which record into the same command buffer, are followed by an invalidation;
  a pipeline with a dynamic vertex stride still binds every vertex buffer, since only that bind sets the stride;
  every graphics pipeline takes topology and restart as dynamic state, so binding one leaves them as they were;
  NVMTL_BIND_CACHE=0 is read once into a static, and WindowServer never uses the cache.
Usage: test_draw_cache.py [driver checkout]"""
import pathlib, re, sys

R = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1]
P = R / "plugin"
vk = (P / "nvmtl_vk.c").read_text()
vkh = (P / "nvmtl_vk.h").read_text()
fails = 0


def arm(name, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + name + (f" ({str(detail).strip()[-300:]})" if detail and not ok else ""))
    fails += not ok


def func_of(src, pos):
    """name of the top-level function a position is in (the last line before it that opens one at column 0)"""
    m = None
    for m in re.finditer(r"^(?:static\s+)?(?:inline\s+)?[A-Za-z_][\w \*]*?\b(\w+)\s*\([^;{]*\)\s*\{", src[:pos], re.M):
        pass
    return m.group(1) if m else None


def body(src, name):
    m = re.search(r"^[^\n;]*\b" + re.escape(name) + r"\s*\([^;{]*\)\s*\{", src, re.M)
    if not m:
        return ""
    i, depth = m.end(), 1
    while depth and i < len(src):
        depth += {"{": 1, "}": -1}.get(src[i], 0); i += 1
    return src[m.start():i]


CALLS = ["pvkCmdBindIndexBuffer(", "pvkCmdBindVertexBuffers(", "pvkCmdBindVertexBuffers2(", "pvkCmdSetPrimitiveTopology(",
         "pvkCmdSetPrimitiveRestartEnable("]
where = {}
for c in CALLS:
    for m in re.finditer(re.escape(c), vk):
        where.setdefault(c, []).append(func_of(vk, m.start()))
arm("the plugin's binds are where the cache expects them",
    sorted(where.get("pvkCmdBindIndexBuffer(", [])) == ["nvmtl_vk_cmd_draw_indexed", "nvmtl_vk_cmd_draw_indexed_indirect"]
    and where.get("pvkCmdBindVertexBuffers(") == ["nvmtl_vk_cmd_vertex_inputs"]
    and where.get("pvkCmdBindVertexBuffers2(") == ["nvmtl_vk_cmd_vertex_inputs"]
    and sorted(where.get("pvkCmdSetPrimitiveTopology(", [])) == ["nvmtl_vk_cmd_draw_patches", "nvmtl_vk_cmd_set_topology"]
    and sorted(where.get("pvkCmdSetPrimitiveRestartEnable(", [])) == ["nvmtl_vk_cmd_draw_patches", "nvmtl_vk_cmd_set_topology"],
    where)

others = [f.name for f in sorted(P.rglob("*")) if f.is_file() and f.suffix in (".c", ".m", ".h", ".mm", ".cpp", ".inc")
          and f.name not in ("nvmtl_vk.c", "nvmtl_draw_cache.h")
          and re.search(r"CmdBindIndexBuffer|CmdBindVertexBuffers|CmdSetPrimitiveTopology|CmdSetPrimitiveRestartEnable|CmdExecuteCommands",
                        f.read_text(errors="replace"))]
arm("no other plugin source records these binds or executes secondary command buffers", not others, others)
arm("nvmtl_vk.c records no secondary command buffers", "vkCmdExecuteCommands" not in vk and "COMMAND_BUFFER_LEVEL_SECONDARY" not in vk)

ix = body(vk, "nvmtl_vk_cmd_draw_indexed"); ixi = body(vk, "nvmtl_vk_cmd_draw_indexed_indirect")
for name, b in (("draw_indexed", ix), ("draw_indexed_indirect", ixi)):
    m = re.search(r"if \(nvmtl_dc_ib\(&c->dc, nvmtl_bind_cache_on\(\), [^\n]*\)\)\n\s*pvkCmdBindIndexBuffer\(", b)
    arm(f"{name}: the index buffer is bound when the cache asks, still before the nobind check",
        m is not None and b.index("pvkCmdBindIndexBuffer(") < b.index("if (c->nobind) return;"), b[:400])

vi = body(vk, "nvmtl_vk_cmd_vertex_inputs")
arm("vertex inputs: a static-stride binding is bound when the cache asks",
    "if (nvmtl_dc_vb(&c->dc, cache, i, (uint64_t)(uintptr_t)b, off, 0)) pvkCmdBindVertexBuffers(c->cb, i, 1, &b, &off); continue;" in vi)
dyn_line = next((l for l in vi.splitlines() if "pvkCmdBindVertexBuffers2(" in l), "")
arm("vertex inputs: a dynamic-stride binding is always bound, and remembered",
    dyn_line.strip().startswith("pvkCmdBindVertexBuffers2(") and "nvmtl_dc_vb(&c->dc, cache, i, (uint64_t)(uintptr_t)b, off, 1);" in vi,
    dyn_line)
arm("vertex inputs: every NOT-drawn check stays", vi.count("NOT drawn") == 3, vi.count("NOT drawn"))

st = body(vk, "nvmtl_vk_cmd_set_topology")
arm("set_topology: c->topo is still set on every call (the fetch split reads it)", "c->topo = mtlType;" in st
    and st.index("c->topo = mtlType;") < st.index("nvmtl_dc_topo("))
arm("set_topology: topology and restart are each recorded when the cache asks",
    re.search(r"if \(nvmtl_dc_topo\(&c->dc, nvmtl_bind_cache_on\(\), [^\n]*&emitRestart\)\)\n\s*pvkCmdSetPrimitiveTopology\(", st) is not None
    and "if (emitRestart) pvkCmdSetPrimitiveRestartEnable(c->cb, restart);" in st)
dp = body(vk, "nvmtl_vk_cmd_draw_patches")
arm("draw_patches tells the cache it set the topology and restart behind it",
    "nvmtl_dc_topo_unknown(&c->dc);" in dp and dp.index("pvkCmdSetPrimitiveRestartEnable(") < dp.index("nvmtl_dc_topo_unknown(&c->dc);"))

for fn, ev, cr in (("nvmtl_vk_cmd_dlss", "nvmtl_dlss_evaluate", "nvmtl_dlss_feature_create"),
                   ("nvmtl_vk_cmd_dlss_rr", "nvmtl_dlss_rr_evaluate", "nvmtl_dlss_rr_feature_create"),
                   ("nvmtl_vk_cmd_dlss_fg", "nvmtl_dlss_fg_evaluate", "nvmtl_dlss_fg_feature_create")):
    b = body(vk, fn)
    arm(f"{fn}: NGX's setup and evaluation are each followed by an invalidation",
        f"= {cr}(c->cb, &want); nvmtl_dc_invalidate(&c->dc); if (!f) return -1;" in b
        and re.search(re.escape(f"const int rc = {ev}(c->cb, f, &e);") + r"\n\s*nvmtl_dc_invalidate\(&c->dc\);", b) is not None)

arm("nvk_cmdbuf carries the cache", "                 nvmtl_draw_cache dc;" in vkh and '#include "nvmtl_draw_cache.h"' in vkh)
beg = body(vk, "nvmtl_vk_cmd_begin")
arm("a new VkCommandBuffer starts from a cleared nvk_cmdbuf",
    beg.index("memset(out, 0, sizeof *out);\n    VkCommandPool pool") < beg.index("pvkBeginCommandBuffer(out->cb"))
arm("only nvmtl_vk_cmd_begin begins a command buffer that draws (the other begin is the dummy zero-fill)",
    [func_of(vk, m.start()) for m in re.finditer(r"pvkBeginCommandBuffer\(", vk)] == ["nvmtl_vk_cmd_begin", "nvmtl_vk_zero_dummies"],
    [func_of(vk, m.start()) for m in re.finditer(r"pvkBeginCommandBuffer\(", vk)])
arm("every graphics pipeline takes topology and restart as dynamic state (one creation site)",
    vk.count("nvmtl_opaque_CreateGraphicsPipelines(g_dev") == 1
    and "VK_DYNAMIC_STATE_PRIMITIVE_TOPOLOGY, VK_DYNAMIC_STATE_PRIMITIVE_RESTART_ENABLE, VK_DYNAMIC_STATE_FRONT_FACE," in vk
    and re.search(r"uint32_t ndyn = hasDepth \? \(out->has_stencil \? 16u : 12u\) : 9u;", vk) is not None)

on = body(vk, "nvmtl_bind_cache_on")
arm("NVMTL_BIND_CACHE is read once into a static; only 0 turns the cache off; WindowServer never uses it",
    "static int on = -1;" in on and on.count('getenv("NVMTL_BIND_CACHE")') == 1 and "!(e && e[0] == '0') && !ws" in on
    and 'strcmp(me, "WindowServer")' in on)
arm("nothing else reads NVMTL_BIND_CACHE", sum(f.read_text(errors="replace").count('"NVMTL_BIND_CACHE"')
                                               for f in P.rglob("*") if f.is_file() and f.suffix in (".c", ".m", ".h")) == 1)
print("FAILED" if fails else "PASS"); sys.exit(1 if fails else 0)
