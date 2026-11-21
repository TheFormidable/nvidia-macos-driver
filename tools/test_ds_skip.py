#!/usr/bin/env python3
"""A repeated setDepthStencilState is skipped only while the command buffer still holds what the encoder applied.

The rule is nvmtl_ds_skip (plugin/nvmtl_repeat_bind.h, proved by tools/test_repeat_bind.c). What makes it safe is in
the sources, and this checks it there:
  the encoder's record (_dsDone, _dsEpoch, _dsCbId, _dsStencil) is cleared when nvmtlApplyDepthStencil starts and set
  only at its end, after the depth, depth bounds and (with a stencil attachment) stencil calls were all made;
  every render pass begin advances the command buffer's epoch, and a new VkCommandBuffer has a new begin ordinal;
  the plugin's only other writers of depth and stencil state are the stencil reference (its own call, which the apply
  does not repeat once set), the depth bounds re-emitted on a pipeline bind (the same values) and the stencil export
  emulation, which restores every value it changes;
  NVMTL_DS_SKIP is read once into a static, only 0 turns the skip off, and WindowServer never skips.
Usage: test_ds_skip.py [driver checkout]"""
import pathlib, re, sys

R = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1]
P = R / "plugin"
objs = (P / "NVMTLObjects.m").read_text()
objh = (P / "NVMTLObjects.h").read_text()
vk = (P / "nvmtl_vk.c").read_text()
vkh = (P / "nvmtl_vk.h").read_text()
fails = 0


def arm(name, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + name + (f" ({str(detail).strip()[-300:]})" if detail and not ok else ""))
    fails += not ok


def block(src, head):
    i = src.find(head)
    if i < 0:
        return ""
    j, depth = src.index("{", i) + 1, 1
    while depth:
        depth += {"{": 1, "}": -1}.get(src[j], 0); j += 1
    return src[i:j]


ap = block(objs, "- (void)nvmtlApplyDepthStencil {")
lines = [l.strip() for l in ap.splitlines()]
arm("the apply clears the record before anything else", len(lines) > 1 and lines[1] == "_dsDone = nil;", lines[:3])
rec = "_dsDone = _ds; _dsEpoch = _cb->_c.gfx_epoch; _dsCbId = _cb->_c.begin_id; _dsStencil = _ps->_p.has_stencil ? YES : NO;"
arm("the record is written once, as the apply's last statement, after the stencil calls",
    ap.count(rec) == 1 and lines[-2] == rec and ap.index("nvmtl_vk_cmd_set_stencil(") < ap.index(rec)
    and ap.index("nvmtl_vk_cmd_set_depth_bounds(") < ap.index(rec), lines[-3:])
arm("the apply returns early only before it records anything", ap.count("return;") == 1
    and ap.index("return;") < ap.index("nvmtl_vk_cmd_set_depth("))
arm("nothing else writes the record", len(re.findall(r"\b_dsDone\s*=", objs)) == 2
    and len(re.findall(r"\b_dsEpoch\s*=", objs)) == 1 and len(re.findall(r"\b_dsCbId\s*=", objs)) == 1)
arm("the record is an unretained address (compared, never messaged)",
    "__unsafe_unretained NVMTLDepthStencilState *_dsDone;" in objh and not re.search(r"\[_dsDone\b|_dsDone->", objs))

sd = block(objs, "- (void)setDepthStencilState:(id<MTLDepthStencilState>)s {\n    // Apple")
arm("setDepthStencilState asks nvmtl_ds_skip with the current pass, pipeline and command buffer, then applies as before",
    "nvmtl_ds_skip(nvmtl_ds_skip_on(), _begun, (__bridge const void *)s, (__bridge const void *)_ds, (__bridge const void *)_dsDone," in sd
    and "_ps && _ps->_p.has_depth, _ps && _ps->_p.has_stencil, _dsStencil, _dsEpoch, _cb->_c.gfx_epoch, _dsCbId, _cb->_c.begin_id))" in sd
    and sd.rstrip().endswith("_ds = (NVMTLDepthStencilState *)s;\n    [self nvmtlApplyDepthStencil];\n}"), sd)

bp = block(vk, "int nvmtl_vk_cmd_begin_pass(nvk_cmdbuf *c, const nvk_pass *ps, nvk_pipeline *p)\n{")
arm("every render pass begin advances the epoch, first thing", bp.split("\n")[2].strip() == "c->gfx_epoch++;", bp[:200])
arm("the epoch lives in nvk_cmdbuf (cleared for every new command buffer)", "uint32_t gfx_epoch;" in vkh)
arm("nothing else changes the epoch", len(re.findall(r"gfx_epoch\s*(\+\+|=|\+=)", vk + objs)) == 1)

# The plugin's writers of depth and stencil dynamic state, by function: anything new here must be argued again.
writers = {}
for m in re.finditer(r"pvkCmdSet(Depth(TestEnable|WriteEnable|CompareOp|BoundsTestEnable|Bounds)|Stencil(TestEnable|Op|CompareMask|WriteMask|Reference))\(", vk):
    head = vk.rfind("\n", 0, m.start())
    fn = None
    for f in re.finditer(r"^(?:static\s+)?[A-Za-z_][\w \*]*?\b(\w+)\s*\([^;{]*\)\s*\{|^#define (\w+)", vk[:m.start()], re.M):
        fn = f.group(1) or f.group(2)
    writers[fn] = writers.get(fn, 0) + 1
arm("depth and stencil state is written only by the apply's setters, the pipeline bind's depth bounds and the stencil emulation",
    set(writers) == {"nvmtl_vk_cmd_set_depth", "nvmtl_vk_cmd_set_stencil", "nvmtl_vk_cmd_set_stencil_ref",
                     "nvmtl_vk_cmd_set_depth_bounds", "nvmtl_vk_cmd_bind_pipeline", "NVMTL_SEMU_DRAW"}, writers)
semu = vk[vk.index("#define NVMTL_SEMU_DRAW"):vk.index("} while (0)", vk.index("#define NVMTL_SEMU_DRAW"))]
arm("the stencil emulation restores the write masks, depth write and reference it changes",
    "(c)->st_wmask[0]);" in semu and "(c)->depth_write ? VK_TRUE : VK_FALSE); }" in semu and "(c)->st_ref[0]);" in semu and "(c)->st_ref[1]);" in semu)
bpl = block(vk, "void nvmtl_vk_cmd_bind_pipeline(nvk_cmdbuf *c, nvk_pipeline *p) {")
arm("a pipeline bind re-emits the depth bounds the apply last set", "c->db_on ? c->db_lo : 0.0f, c->db_on ? c->db_hi : 1.0f" in bpl)
arm("the encoder's other callers of the setters are the stencil reference calls", sorted(
    m.group(0) for m in re.finditer(r"nvmtl_vk_cmd_set_(depth|stencil|stencil_ref|depth_bounds)\(", objs)) ==
    sorted(["nvmtl_vk_cmd_set_depth(", "nvmtl_vk_cmd_set_depth_bounds(", "nvmtl_vk_cmd_set_stencil(", "nvmtl_vk_cmd_set_stencil_ref(",
            "nvmtl_vk_cmd_set_stencil_ref(", "nvmtl_vk_cmd_set_stencil_ref("]))
arm("setDepthTestMinBound applies the state itself", "_dbOn = YES; _dbLo = lo; _dbHi = hi;\n    [self nvmtlApplyDepthStencil];" in objs)

on = block(objs, "static int nvmtl_ds_skip_on(void) {")
arm("NVMTL_DS_SKIP is read once into a static; only 0 turns the skip off; WindowServer never skips",
    "static int on = -1;" in on and on.count('getenv("NVMTL_DS_SKIP")') == 1 and "!(e && e[0] == '0') && !ws" in on
    and 'strcmp(me, "WindowServer")' in on)
arm("nothing else reads NVMTL_DS_SKIP", sum(f.read_text(errors="replace").count('"NVMTL_DS_SKIP"')
                                            for f in P.rglob("*") if f.is_file() and f.suffix in (".c", ".m", ".h")) == 1)
print("FAILED" if fails else "PASS"); sys.exit(1 if fails else 0)
