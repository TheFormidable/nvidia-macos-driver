#!/usr/bin/env python3
"""test_dlss_views.py - the wiring of nvmtl_dlss_views.h (tools/test_dlss_views.c proves the header itself).

Every path that takes a VkImage away from a Metal texture must take DLSS's views of it out of the cache, or a cached
view outlives its image and a later image with the same handle value is handed the dead one: the image's destroy
(nvmtl_vk_image_destroy_now), the grave-reaped backing (nvmtl_vk_image_free_backing) and the re-back
(nvmtl_vk_image_reback, through the grace). The cache is the growable table, never a fixed array, and Frame
Generation decides its priming evaluation through nvmtl_dlss_fg_prime and remembers the image it handed the core.

    python3 tools/test_dlss_views.py     # exit 1 on a missing call
"""
import os, re, sys
here = os.path.dirname(os.path.abspath(__file__))
root = os.path.join(here, "..")
vk = open(os.path.join(root, "plugin", "nvmtl_vk.c")).read()
fx = open(os.path.join(root, "plugin", "NVMTLMetalFXDLSS.m")).read()
fails = 0

def body(src, name):
    m = re.search(r"^[^\n;]*\b%s\s*\([^;{]*\)\s*\{" % re.escape(name), src, re.M)
    if not m: return ""
    i = m.end(); depth = 1
    while depth and i < len(src):
        depth += {"{": 1, "}": -1}.get(src[i], 0); i += 1
    return src[m.start():i]

def arm(what, ok):
    global fails
    print("%s  %s" % ("ok  " if ok else "FAIL", what))
    if not ok: fails += 1

def before(b, first, second):
    i, j = b.find(first), b.find(second)
    return i >= 0 and j >= 0 and i < j

d = body(vk, "nvmtl_vk_image_destroy_now"); f = body(vk, "nvmtl_vk_image_free_backing"); r = body(vk, "nvmtl_vk_image_reback")
arm("the image's destroy drops its DLSS views before the VkImage goes", before(d, "nvmtl_dlss_views_drop(", "pvkDestroyImage("))
arm("the grave-reaped backing drops its DLSS views before the VkImage goes", before(f, "nvmtl_dlss_views_drop(", "pvkDestroyImage("))
arm("the re-back retires the old image's DLSS views before the old VkImage goes",
    before(r, "nvmtl_dlss_views_retire((VkImage)i->img)", "pvkDestroyImage(g_dev, (VkImage)i->img"))
t = body(vk, "nvmtl_dlss_views_retire")
arm("the retire destroys through the grace, outside the cache's mutex",
    "nvmtl_vk_image_view_destroy(" in t and "pvkDestroyImageView" not in t
    and t.find("pthread_mutex_unlock(&g_dlss_vc_mu)") < t.find("nvmtl_vk_image_view_destroy("))
v = body(vk, "dlss_view")
arm("dlss_view looks up and keeps views in the growable table", "nvmtl_dvc_find(&g_dlss_vc" in v and "nvmtl_dvc_add(&g_dlss_vc" in v)
arm("no fixed-size DLSS view table is left", "DLSS_VIEW_CACHE" not in vk and "static nvmtl_dvc g_dlss_vc;" in vk)
arm("nvmtl_vk.c includes the header", '#include "nvmtl_dlss_views.h"' in vk)
e = body(fx, "nvmtl_fg_src")
arm("Frame Generation reads the image DLSS uses for a texture (its nvi)", "[t nvi]" in e)
arm("Frame Generation decides the priming through nvmtl_dlss_fg_prime", "nvmtl_dlss_fg_prime(reset, prev != nil, _haveHistory, (id)_prevColorTexture == (id)_lastColor" in fx)
arm("Frame Generation remembers the image it handed the core with the last colour",
    "_lastColor = _colorTexture; _lastSeen = nvmtl_fg_src(color);" in fx)
arm("a failed encode forgets the remembered image", "_haveHistory = NO; _lastSeen = (nvmtl_dlss_src){ 0 }; return;" in fx)
print(fails and "FAILED %d" % fails or "ok")
sys.exit(1 if fails else 0)
