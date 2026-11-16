#!/usr/bin/env python3
"""nvmtl_vk_buffer_tiling_features, the format features a linear alias of a buffer is built with, run from plugin/nvmtl_vk.c.

The function is lifted from the plugin's source and compiled with stand-in Vulkan types and queries. With
VK_EXT_image_drm_format_modifier (g_linmod) it must take DRM modifier 0's features when the format's modifier list names
modifier 0 of one plane, and plain LINEAR tiling's features otherwise. Pinned NVK lists no modifier at all for
B10G11R11_UFLOAT (VkFormat 122), E5B9G9R9_UFLOAT (123) and every format it cannot render to, and those formats must keep
SAMPLED: without it a buffer texture of them is refused and an IOSurface wrapper of them loses its VRAM import. The
cases: an empty list, a list with modifier 0 (its COLOR_ATTACHMENT is taken), modifier 0 of two planes only, no
vkGetPhysicalDeviceFormatProperties2, plain_linear, no extension, and each answer kept after the first query. A control
compiles the function with an empty list giving 0 and must fail the empty-list case. No GPU or Vulkan SDK is needed."""
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'plugin/nvmtl_vk.c').read_text()
SIGNATURE = 'static VkFormatFeatureFlags nvmtl_vk_buffer_tiling_features('
CACHE = 'static _Atomic uint64_t g_alias_feature_cache['


def function(signature):
    start = SOURCE.index(signature)
    return SOURCE[start:SOURCE.index('\n}\n', start) + 3]


FIXTURE = r'''
#include <assert.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef void *VkInstance, *VkPhysicalDevice;
typedef uint32_t VkFormat, VkFormatFeatureFlags, VkStructureType;
typedef struct { VkFormatFeatureFlags linearTilingFeatures, optimalTilingFeatures, bufferFeatures; } VkFormatProperties;
typedef struct { VkStructureType sType; void *pNext; VkFormatProperties formatProperties; } VkFormatProperties2;
typedef struct { uint64_t drmFormatModifier; uint32_t drmFormatModifierPlaneCount; VkFormatFeatureFlags drmFormatModifierTilingFeatures; } VkDrmFormatModifierPropertiesEXT;
typedef struct { VkStructureType sType; void *pNext; uint32_t drmFormatModifierCount; VkDrmFormatModifierPropertiesEXT *pDrmFormatModifierProperties; } VkDrmFormatModifierPropertiesListEXT;
#define VK_STRUCTURE_TYPE_FORMAT_PROPERTIES_2 1000059002u
#define VK_STRUCTURE_TYPE_DRM_FORMAT_MODIFIER_PROPERTIES_LIST_EXT 1000158000u
#define SAMPLED 0x1u
#define STORAGE 0x2u
#define COLOR 0x80u
typedef void (*PFN_vkVoidFunction)(void);
typedef void (*PFN_vkGetPhysicalDeviceFormatProperties)(VkPhysicalDevice, VkFormat, VkFormatProperties *);
typedef void (*PFN_vkGetPhysicalDeviceFormatProperties2)(VkPhysicalDevice, VkFormat, VkFormatProperties2 *);
static VkInstance g_inst = (void *)1;
static VkPhysicalDevice g_pdev = (void *)2;
static int g_linmod;
static int no_query2;
static unsigned linear_queries, list_queries;
/* 37: modifier 0 adds COLOR. 122: no modifier listed (NVK's R11G11B10_FLOAT). 44: modifier 0 of two planes only. */
static void linear(VkPhysicalDevice device, VkFormat format, VkFormatProperties *out) {
    assert(device == g_pdev); memset(out, 0, sizeof *out); linear_queries++;
    out->linearTilingFeatures = SAMPLED | (format == 37 ? STORAGE : 0);
}
static void listed(VkPhysicalDevice device, VkFormat format, VkFormatProperties2 *out) {
    assert(device == g_pdev && out->sType == VK_STRUCTURE_TYPE_FORMAT_PROPERTIES_2); list_queries++;
    VkDrmFormatModifierPropertiesListEXT *list = out->pNext;
    assert(list && list->sType == VK_STRUCTURE_TYPE_DRM_FORMAT_MODIFIER_PROPERTIES_LIST_EXT);
    VkDrmFormatModifierPropertiesEXT entries[2] = { { 0x0300000000606015ull, 1, SAMPLED | COLOR }, { 0, 1, SAMPLED | STORAGE | COLOR } };
    uint32_t count = format == 37 ? 2 : format == 44 ? 2 : 0;
    if (format == 44) entries[1].drmFormatModifierPlaneCount = 2;
    if (!list->pDrmFormatModifierProperties) { list->drmFormatModifierCount = count; return; }
    uint32_t n = list->drmFormatModifierCount < count ? list->drmFormatModifierCount : count;
    memcpy(list->pDrmFormatModifierProperties, entries, n * sizeof *entries);
    list->drmFormatModifierCount = n;
}
static PFN_vkVoidFunction pvkGetInstanceProcAddr(VkInstance instance, const char *name) {
    assert(instance == g_inst);
    if (!strcmp(name, "vkGetPhysicalDeviceFormatProperties2")) return no_query2 ? NULL : (PFN_vkVoidFunction)listed;
    return NULL;
}
@@FUNCTION@@
#define CHECK(expr, what) do { if (!(expr)) { printf("FAIL: %s\n", what); return 1; } } while (0)
int main(void) {
    g_linmod = 1;
    VkFormatFeatureFlags f = nvmtl_vk_buffer_tiling_features(linear, 122, 0);
    if (CONTROL) { printf("control: an empty modifier list gives 0x%x\n", (unsigned)f); return (f & SAMPLED) ? 0 : 3; }
    CHECK(f == SAMPLED, "an empty modifier list must give LINEAR tiling's features (SAMPLED)");
    CHECK(list_queries == 1 && linear_queries == 1, "an empty modifier list is asked once and LINEAR tiling once");
    CHECK(nvmtl_vk_buffer_tiling_features(linear, 122, 0) == SAMPLED && list_queries == 1 && linear_queries == 1,
          "the answer for a format is kept after the first query");
    CHECK(nvmtl_vk_buffer_tiling_features(linear, 37, 0) == (SAMPLED | STORAGE | COLOR), "modifier 0's features are taken when it is listed");
    CHECK(nvmtl_vk_buffer_tiling_features(linear, 44, 0) == SAMPLED, "modifier 0 of two planes does not count as listed");
    CHECK(nvmtl_vk_buffer_tiling_features(linear, 37, 1) == (SAMPLED | STORAGE), "plain_linear takes LINEAR tiling's features");
    no_query2 = 1;
    CHECK(nvmtl_vk_buffer_tiling_features(linear, 50, 0) == SAMPLED, "no vkGetPhysicalDeviceFormatProperties2 gives LINEAR tiling's features");
    CHECK(nvmtl_vk_buffer_tiling_features(NULL, 51, 0) == 0, "no query at all gives 0");
    no_query2 = 0; g_linmod = 0;
    CHECK(nvmtl_vk_buffer_tiling_features(linear, 38, 0) == SAMPLED, "without the extension LINEAR tiling's features are taken");
    printf("PASS: empty list, modifier 0 listed, two planes, plain_linear, no FormatProperties2, no query, no extension, kept answers\n");
    return 0;
}
'''


def control_body(current):
    """The function with an empty or missing modifier list answering 0, as an earlier version of
    nvmtl_vk_buffer_tiling_features had it."""
    body = current.replace('if (!listed) {', 'if (!listed && !modifier) {', 1)
    return body if body != current else None


def compiler():
    if shutil.which('xcrun'):
        return ['xcrun', 'clang']
    for cc in ('cc', 'clang', 'gcc'):
        if shutil.which(cc):
            return [cc]
    return None


def build(directory, body, control, name):
    src = pathlib.Path(directory) / (name + '.c')
    exe = pathlib.Path(directory) / name
    src.write_text(FIXTURE.replace('@@FUNCTION@@', body).replace('CONTROL', str(control)))
    r = subprocess.run(compiler() + ['-std=gnu11', '-O1', '-g', '-Wall', '-fsanitize=address,undefined', '-fno-sanitize-recover=all',
                                      str(src), '-o', str(exe)], capture_output=True, text=True)
    if r.returncode:
        print('FAIL: the fixture does not compile\n' + r.stderr[-2500:])
        return None
    return exe


def main():
    if SIGNATURE not in SOURCE:
        print('FAIL: plugin/nvmtl_vk.c has no nvmtl_vk_buffer_tiling_features')
        return 1
    if CACHE not in SOURCE:
        print('FAIL: plugin/nvmtl_vk.c keeps no g_alias_feature_cache for this test to declare')
        return 1
    cache = next(line for line in SOURCE.splitlines() if line.startswith(CACHE))
    current = cache + '\n' + function(SIGNATURE)
    control = control_body(current)
    if control is None or not re.search(r'features = properties\.linearTilingFeatures;', current):
        print('FAIL: nvmtl_vk_buffer_tiling_features has no LINEAR fallback this test can turn into its control')
        return 1
    if not compiler():
        print('SKIP: no C compiler')
        return 2
    with tempfile.TemporaryDirectory(prefix='nvmtl-tiling-features-') as d:
        exe = build(d, current, 0, 'current')
        if not exe:
            return 1
        p = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
        print((p.stdout + p.stderr).strip()[-1500:])
        if p.returncode:
            return 1
        exe = build(d, control, 1, 'control')
        if not exe:
            return 1
        p = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
        print((p.stdout + p.stderr).strip()[-500:])
        if p.returncode != 3:
            print('FAIL: control: an empty modifier list answering 0 was not caught')
            return 1
        print('ok   control: an empty modifier list answering 0 is caught')
    return 0


if __name__ == '__main__':
    sys.exit(main())
