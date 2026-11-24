#!/usr/bin/env python3
"""Hierarchical Z over NVRM (nvk/nvk-zcull.patch, 3.0.17).

NVK 26.3 culls hidden fragments with the GPU's ZCULL unit once the device reports the unit's geometry
(nv_device_info.has_zcull_info and zcull_info). The NVRM backend set has_zcull_info to false; the patch fills both from
NV2080_CTRL_CMD_GR_GET_ZCULL_INFO and the ZCULL engine's context properties, the way Linux's GSP code does
(r570_gr_get_ctxbufs_and_zcull_info), and keeps it off with NVK_ZCULL=0, in WindowServer, with NVRM_610_ZCULL=0, when a
call fails or when a figure would make NIL divide by zero or index past its 16 subregions.

This test needs no Mesa tree. It checks the patch's place in the chain and the files it touches, that each of RM's ten
fields lands in the field of the same meaning, that the decision is the one the device setup uses, and it compiles the
decision function out of the patch and runs it over every case. Usage: test_nvk_zcull.py"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / "nvk/nvk-zcull.patch"
SCRIPT = ROOT / "build/build_nvk.sh"
SWITCHES = ROOT / "tools/switches.tsv"
fails = 0


def arm(name, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + name + (f" ({str(detail).strip()[-300:]})" if detail and not ok else ""))
    fails += not ok


def chains():
    """-> {release: [patch names]} from the case block of build_nvk.sh."""
    text = SCRIPT.read_text()
    case = re.search(r"case \$REL in\n(.*?)\n\s*esac", text, re.S).group(1)
    return {m.group(1): m.group(2).split() for m in re.finditer(r"^\s*([\d.]+)\).*?CHAIN=\"([^\"]*)\"", case, re.S | re.M)}


text = PATCH.read_text() if PATCH.is_file() else ""
arm("nvk/nvk-zcull.patch exists", bool(text))
body = text.partition("\ndiff --git ")[2]
add = "\n".join(l[1:] for l in body.splitlines() if l.startswith("+") and not l.startswith("+++"))
rem = "\n".join(l[1:] for l in body.splitlines() if l.startswith("-") and not l.startswith("---"))

# --- the chain ---------------------------------------------------------------------------------------------------------
ch = chains()
arm("the patch is the last of the 3.0 chain", ch.get("3.0", [])[-1:] == ["nvk-zcull"], ch.get("3.0", [])[-3:])
arm("the patch is in no other chain", all("nvk-zcull" not in v for k, v in ch.items() if k != "3.0"))
files = re.findall(r"^\+\+\+ b/(\S+)", text, re.M)
arm("the patch changes nvkmd_nvrm_pdev.c alone", files == ["src/nouveau/vulkan/nvkmd/nvrm/nvkmd_nvrm_pdev.c"], files)

# --- the device setup --------------------------------------------------------------------------------------------------
arm("the unconditional has_zcull_info = false is gone", "has_zcull_info = false;" in rem and
    "has_zcull_info = false;" not in add)
arm("the device setup calls nvkmd_nvrm_pdev_zcull(pdev, &rm)", "nvkmd_nvrm_pdev_zcull(pdev, &rm);" in add)
arm("has_zcull_info is what nvrm_zcull_decide answered", re.search(
    r"const bool on = nvrm_zcull_decide\(env, ws, bind_on, info_ok, props_ok, zp\.size, &z, &why\);\s*"
    r"pdev->base\.dev_info\.has_zcull_info = on;\s*if \(on\)\s*pdev->base\.dev_info\.zcull_info = z;", add) is not None)
arm("the figures come from GR_GET_ZCULL_INFO on the subdevice",
    "nvRmApiControl(rm, pdev->hSubdevice, NV2080_CTRL_CMD_GR_GET_ZCULL_INFO, &zi, sizeof zi)" in add)
arm("the context buffer from the ZCULL engine's properties on the device",
    "nvRmApiControl(rm, pdev->hDevice, NV0080_CTRL_CMD_FIFO_GET_ENGINE_CONTEXT_PROPERTIES, &zp, sizeof zp)" in add and
    "ENGINE_ID_GRAPHICS_ZCULL" in add)
# Linux: drivers/gpu/drm/nouveau/nvkm/subdev/gsp/rm/r570/gr.c, r570_gr_get_ctxbufs_and_zcull_info
MAP = {"width_align_pixels": "zi.widthAlignPixels", "height_align_pixels": "zi.heightAlignPixels",
       "pixel_squares_by_aliquots": "zi.pixelSquaresByAliquots", "aliquot_total": "zi.aliquotTotal",
       "zcull_region_byte_multiplier": "zi.zcullRegionByteMultiplier",
       "zcull_region_header_size": "zi.zcullRegionHeaderSize",
       "zcull_subregion_header_size": "zi.zcullSubregionHeaderSize", "subregion_count": "zi.subregionCount",
       "subregion_width_align_pixels": "zi.subregionWidthAlignPixels",
       "subregion_height_align_pixels": "zi.subregionHeightAlignPixels",
       "ctxsw_size": "zp.size", "ctxsw_align": "zp.alignment"}
got = dict(re.findall(r"^\s*z\.(\w+) = ([\w.]+);", add, re.M))
arm("each of the twelve fields gets the RM value of the same meaning", got == MAP,
    {k: (got.get(k), v) for k, v in MAP.items() if got.get(k) != v} or set(got) - set(MAP))
arm("RM is not asked when the rule is already off", re.search(
    r"if \(!\(env != NULL && env\[0\] == '0'\) && !ws && bind_on\) \{\s*si = nvRmApiControl", add) is not None)
arm("the channel bind and this read the same item (NVRM_610_ZCULL)", 'nvrm610_on("ZCULL")' in add)
arm("the switch is read from the settings file as well as the environment", 'nvmtl_cfg_getenv("NVK_ZCULL")' in add)

# --- the switches ------------------------------------------------------------------------------------------------------
rows = {l.split("\t")[1]: l.split("\t") for l in SWITCHES.read_text().splitlines() if l and not l.startswith("#")}
r = rows.get("NVK_ZCULL")
arm("NVK_ZCULL has an escape row from 3.0.17", r is not None and r[0] == "env" and r[2] == "escape" and
    r[3].startswith("on (NVK_ZCULL=0") and r[4] == "3.0.17", r)
r = rows.get("NVK_ZCULL_LOG")
arm("NVK_ZCULL_LOG has a diag row", r is not None and r[0] == "env" and r[2] == "diag", r)

# --- the rule, compiled out of the patch -------------------------------------------------------------------------------
m = re.search(r"^#define NVRM_ZCULL_MAX_SUBREGIONS.*?\n(static bool\nnvrm_zcull_decide\(.*?\n\}\n)", add, re.S | re.M)
arm("nvrm_zcull_decide is in the patch", m is not None)
cc = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
if m and cc:
    src = r'''
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
struct nv_zcull_device_info {   /* src/nouveau/headers/nv_device_info.h, Mesa 26.3 */
   uint32_t width_align_pixels, height_align_pixels, pixel_squares_by_aliquots, aliquot_total,
            zcull_region_byte_multiplier, zcull_region_header_size, zcull_subregion_header_size, subregion_count,
            subregion_width_align_pixels, subregion_height_align_pixels, ctxsw_size, ctxsw_align;
};
''' + m.group(0) + r'''
static int fails;
static void one(const char *name, bool want, const char *want_why, const char *env, bool ws, bool bind, bool info,
                bool props, uint32_t ctxsw, struct nv_zcull_device_info z)
{
   const char *why = NULL;
   bool on = nvrm_zcull_decide(env, ws, bind, info, props, ctxsw, &z, &why);
   bool ok = on == want && why && strcmp(why, want_why) == 0;
   printf("%s %s: %s (%s)\n", ok ? "ok  " : "FAIL", name, on ? "on" : "off", why ? why : "-");
   fails += !ok;
}
int main(void)
{
   /* the figures of Mesa's nouveau drm-shim (src/nouveau/drm-shim/nouveau_noop.c) */
   const struct nv_zcull_device_info g = { 224, 32, 3584, 5120, 128, 224, 1344, 16, 224, 64, 657408, 4096 };
   struct nv_zcull_device_info z;
   one("default, sane figures", true, "on", NULL, false, true, true, true, 657408, g);
   one("NVK_ZCULL=1", true, "on", "1", false, true, true, true, 657408, g);
   one("NVK_ZCULL=0", false, "NVK_ZCULL=0", "0", false, true, true, true, 657408, g);
   one("NVK_ZCULL=0 wins over everything", false, "NVK_ZCULL=0", "0", true, false, false, false, 0, g);
   one("WindowServer", false, "WindowServer", NULL, true, true, true, true, 657408, g);
   one("NVRM_610_ZCULL=0", false, "NVRM_610_ZCULL=0", NULL, false, false, true, true, 657408, g);
   one("GR_GET_ZCULL_INFO failed", false, "GR_GET_ZCULL_INFO failed", NULL, false, true, false, true, 657408, g);
   one("context properties failed", false, "no ZCULL context buffer", NULL, false, true, true, false, 657408, g);
   one("context buffer of 0 bytes", false, "no ZCULL context buffer", NULL, false, true, true, true, 0, g);
   z = g; z.width_align_pixels = 0; one("width align 0", false, "figures out of range", NULL, false, true, true, true, 1, z);
   z = g; z.height_align_pixels = 0; one("height align 0", false, "figures out of range", NULL, false, true, true, true, 1, z);
   z = g; z.pixel_squares_by_aliquots = 0; one("pixels per aliquot 0", false, "figures out of range", NULL, false, true, true, true, 1, z);
   z = g; z.aliquot_total = 0; one("no aliquots", false, "figures out of range", NULL, false, true, true, true, 1, z);
   z = g; z.zcull_region_byte_multiplier = 0; one("0 bytes per aliquot", false, "figures out of range", NULL, false, true, true, true, 1, z);
   z = g; z.subregion_count = 0; one("0 subregions", false, "figures out of range", NULL, false, true, true, true, 1, z);
   z = g; z.subregion_count = 17; one("17 subregions", false, "figures out of range", NULL, false, true, true, true, 1, z);
   z = g; z.subregion_count = 1; one("1 subregion", true, "on", NULL, false, true, true, true, 1, z);
   z = g; z.subregion_width_align_pixels = 0; one("subregion width align 0", false, "figures out of range", NULL, false, true, true, true, 1, z);
   z = g; z.subregion_height_align_pixels = 0; one("subregion height align 0", false, "figures out of range", NULL, false, true, true, true, 1, z);
   z = g; z.zcull_region_header_size = 0; z.zcull_subregion_header_size = 0; z.ctxsw_align = 0;
   one("headers and context alignment 0 are allowed", true, "on", NULL, false, true, true, true, 1, z);
   return fails != 0;
}
'''
    with tempfile.TemporaryDirectory() as d:
        c = Path(d) / "zcull.c"
        c.write_text(src)
        b = subprocess.run([cc, "-std=c11", "-Wall", "-Wextra", "-Werror", "-o", str(Path(d) / "zcull"), str(c)],
                           capture_output=True, text=True)
        arm("the rule compiles on its own with -Wall -Wextra -Werror", b.returncode == 0, b.stderr)
        if b.returncode == 0:
            run = subprocess.run([str(Path(d) / "zcull")], capture_output=True, text=True)
            print(run.stdout, end="")
            arm("every case of the rule", run.returncode == 0, run.stdout + run.stderr)
elif m:
    print("skip the compiled rule: no C compiler")

print("FAILED" if fails else "PASSED", f"({fails} failed)")
sys.exit(1 if fails else 0)
