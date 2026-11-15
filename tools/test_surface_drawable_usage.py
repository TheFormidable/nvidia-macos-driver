#!/usr/bin/env python3
"""The usage a surface texture reports and the backing it gets, run from the plugin's own source with stubs.

nvmtl_surface_widen_usage, nvmtl_surface_usage, nvmtl_managed_surface_tiled_on and nvmtl_surface_image_create are
lifted from plugin/NVMTLDevice.m and compiled with stand-ins for IOSurface, the import cache and the Vulkan image calls, so each case below can say which
backing a wrapper got and what was released on the way. The cases:
  - an application's drawable that is not framebuffer-only reports ShaderRead and RenderTarget, and ShaderWrite only
    when its backing has a storage view; WindowServer and framebuffer-only textures keep the usage they asked for;
  - an Unknown usage stays Unknown when the backing has no storage view (the optimal-tiled storage twin covers its
    writes) and becomes the explicit bits when it has one (Core Image refuses a destination without ShaderWrite);
  - a writable wrapper whose linear import of the family's VRAM lost its storage view gets a private image instead,
    and the dropped image and the import go back exactly once; a read-only wrapper keeps the import;
  - a wrapper that may be drawn into whose import is not a colour attachment gets a private image; one that is only
    sampled keeps the import;
  - a shader-writable wrapper in Managed storage keeps the import to publish through and is drawn through a tiled image
    of its own, which starts out not current whatever the surface's tag says; NVMTL_MANAGED_SURFACE_TILED=0 draws it
    straight into the import; a tiled image that cannot be made sends it to a private image, the import released once.
Each case runs once as "Photo Booth" and once as "WindowServer". Three controls must fail: the function with the storage
fallback taken out, the usage rule that keeps Unknown over a real storage view, and the function without its tiled
image for Managed wrappers.

The source of the policy is the HighDelay/nvidia-macos-driver-nvk fork (Photo Booth's shared drawables, and stale
layers of application icons drawn into shader-writable Managed surfaces)."""
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'plugin/NVMTLDevice.m').read_text()


def function(signature):
    start = SOURCE.index(signature)
    cursor = SOURCE.index('{', start) + 1
    depth = 1
    while depth:
        depth += (SOURCE[cursor] == '{') - (SOURCE[cursor] == '}')
        cursor += 1
    return SOURCE[start:cursor] + '\n'


POLICY = function('static BOOL nvmtl_surface_widen_usage(') + function('static NSUInteger nvmtl_surface_usage(')
IMAGE = function('static int nvmtl_managed_surface_tiled_on(') + function('static int nvmtl_surface_image_create(')
FALLBACK = '''                    if ((stex && !t->_i.storage) || (rt && !(t->_i.usage & VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT))) {'''
TILED = '''== MTLStorageModeManaged && nvmtl_managed_surface_tiled_on()) {'''
if FALLBACK not in IMAGE or TILED not in IMAGE:
    print('FAIL: nvmtl_surface_image_create no longer has the fallback or the Managed branch this test drives')
    sys.exit(1)

FIXTURE = r'''
#define _POSIX_C_SOURCE 200809L   /* setenv and unsetenv under -std=c11 */
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef signed char BOOL;
typedef unsigned long NSUInteger;
#define YES 1
#define NO 0
#define MTLTextureUsageUnknown 0ul
#define MTLTextureUsageShaderRead 1ul
#define MTLTextureUsageShaderWrite 2ul
#define MTLTextureUsageRenderTarget 4ul
#define MTLResourceStorageModeShift 4
#define MTLStorageModeShared 0ul
#define MTLStorageModeManaged 1ul
#define VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT 0x10u
static const char *program;
#define getprogname() program
#define nvlog(...) ((void)0)
@@POLICY@@
typedef struct { void *img; int storage; unsigned usage; } nvk_image;
typedef struct { int live; } nvk_buffer;
typedef struct { nvk_image _i; nvk_buffer _vramBuf; BOOL _vramOn; nvk_image _surfaceLinear; unsigned long _ropt; uint32_t _surfSeed; BOOL _surfSynced; } NVMTLTexture;
typedef void *IOSurfaceRef;
typedef struct { int planes; struct { unsigned row; } p[2]; } nvmtl_pa_surface;
static unsigned long long g_surf_vram, g_surf_private;
static unsigned alias_calls, private_calls, image_drops, buffer_puts, buffer_drops, tag_current;
static int drop_storage, drop_render, alias_fails, cached, typed_fails;
static size_t IOSurfaceGetPlaneCount(IOSurfaceRef s) { (void)s; return 1; }
static size_t IOSurfaceGetBytesPerRow(IOSurfaceRef s) { (void)s; return 2048; }
static size_t IOSurfaceGetHeight(IOSurfaceRef s) { (void)s; return 64; }
static uint32_t IOSurfaceGetID(IOSurfaceRef s) { (void)s; return 1; }
static uint32_t IOSurfaceGetSeed(IOSurfaceRef s) { (void)s; return 7; }
static int nvmtl_vk_surface_share_on(void) { return 1; }
static int nvmtl_vk_format_is_storage(uint32_t f) { (void)f; return 1; }
static int nvmtl_vram_tag_current(IOSurfaceRef s, NSUInteger p, uint32_t seed) { (void)s; (void)p; (void)seed; return (int)tag_current; }
static int nvmtl_ic_get(IOSurfaceRef s, size_t need, nvk_buffer *b) { (void)s; (void)need; b->live = 1; return 0; }
static int nvmtl_ic_put(nvk_buffer *b) { assert(b->live); buffer_puts++; if (cached) { b->live = 0; return 1; } return 0; }
static void nvmtl_vk_buffer_destroy(nvk_buffer *b) { assert(b->live); buffer_drops++; b->live = 0; }
static void nvmtl_vk_image_destroy(nvk_image *i) { assert(i->img); image_drops++; memset(i, 0, sizeof *i); }
static int nvmtl_vk_image_create_buffer_alias(uint32_t w, uint32_t h, uint32_t f, uint32_t bpp, int a8, int storage, int render,
                                              nvk_buffer *b, size_t off, size_t row, nvk_image *out) {
    (void)w; (void)h; (void)f; (void)bpp; (void)a8; (void)render; (void)off; (void)row; assert(b->live);
    alias_calls++; if (alias_fails) { memset(out, 0, sizeof *out); return -1; }
    out->img = (void *)2; out->storage = storage && !drop_storage; out->usage = render && !drop_render ? VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT : 0;
    return 0;
}
static int nvmtl_vk_image_create_typed(uint32_t w, uint32_t h, uint32_t f, uint32_t bpp, int a8, uint32_t mips, int storage,
                                       uint32_t type, uint32_t layers, nvk_image *out) {
    (void)w; (void)h; (void)f; (void)bpp; (void)a8; (void)mips; (void)type; (void)layers;
    private_calls++; if (typed_fails > 0) { typed_fails--; memset(out, 0, sizeof *out); return -1; }
    out->img = (void *)(uintptr_t)(2 + private_calls); out->storage = storage; out->usage = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT; return 0;
}
/* the two-plane branch is not reached with one plane; these only let it compile */
static void nvmtl_pa_read(IOSurfaceRef s, nvmtl_pa_surface *o) { (void)s; memset(o, 0, sizeof *o); }
static int nvmtl_pa_eligible(const nvmtl_pa_surface *s, uint32_t p, uint32_t w, uint32_t h, uint32_t bpp) { (void)s; (void)p; (void)w; (void)h; (void)bpp; return 1; }
static const char *nvmtl_pa_reason(int why) { (void)why; return "stub"; }
static int nvmtl_plane_vram_ready(void) { return 0; }
static int nvmtl_vk_plane_image_create(uint32_t id, const nvmtl_pa_surface *s, uint32_t p, uint32_t w, uint32_t h, uint32_t f, uint32_t bpp,
                                       int a8, int st, nvk_buffer *b, nvk_image *o) { (void)id; (void)s; (void)p; (void)w; (void)h; (void)f; (void)bpp; (void)a8; (void)st; (void)b; (void)o; return -1; }
@@IMAGE@@
static void reset(NVMTLTexture *t) {
    memset(t, 0, sizeof *t); alias_calls = private_calls = image_drops = buffer_puts = buffer_drops = tag_current = 0;
    drop_storage = drop_render = alias_fails = cached = typed_fails = 0;
}
static int cases(void) {
    const int ws = !strcmp(program, "WindowServer");
    /* the reported usage */
    assert(nvmtl_surface_usage(1, NO, NO) == (ws ? 1ul : 5ul));
    assert(nvmtl_surface_usage(1, NO, YES) == (ws ? 1ul : 7ul));
    assert(nvmtl_surface_usage(1, YES, YES) == 1ul);
    assert(nvmtl_surface_usage(3, NO, NO) == (ws ? 3ul : 7ul));
    assert(nvmtl_surface_usage(3, YES, YES) == 3ul);
    for (int fbo = 0; fbo < 2; fbo++) for (int storage = 0; storage < 2; storage++)
        assert(nvmtl_surface_usage(MTLTextureUsageUnknown, (BOOL)fbo, (BOOL)storage) == (!ws && !fbo && storage ? 7ul : MTLTextureUsageUnknown));
    assert(nvmtl_surface_widen_usage(NO) == !ws && !nvmtl_surface_widen_usage(YES));
    NVMTLTexture t;
    /* a writable wrapper whose import keeps its storage view: the family's VRAM */
    reset(&t);
    assert(!nvmtl_surface_image_create(&t, (void *)1, 0, 512, 64, 37, 4, 0, 1, 1));
    assert(t._vramOn && t._i.storage && alias_calls == 1 && !private_calls && !image_drops && !buffer_puts && !buffer_drops);
    /* the import lost its storage view: a writable private image; the import and its image go back once */
    for (int c = 0; c < 2; c++) {
        reset(&t); drop_storage = 1; cached = c;
        assert(!nvmtl_surface_image_create(&t, (void *)1, 0, 512, 64, 37, 4, 0, 1, 1));
        assert(!t._vramOn && t._i.storage && t._i.img == (void *)3 && private_calls == 1 && image_drops == 1 && buffer_puts == 1);
        assert(buffer_drops == (c ? 0u : 1u));
    }
    /* a read-only wrapper keeps the import even without a storage view */
    reset(&t); drop_storage = 1; tag_current = 1;
    assert(!nvmtl_surface_image_create(&t, (void *)1, 0, 512, 64, 37, 4, 0, 0, 0));
    assert(t._vramOn && !t._i.storage && t._surfSynced && t._surfSeed == 7 && !private_calls && !image_drops && !buffer_puts);
    /* the import failed outright: a private image, writable because the format is */
    reset(&t); alias_fails = 1;
    assert(!nvmtl_surface_image_create(&t, (void *)1, 0, 512, 64, 37, 4, 0, 0, 0));
    assert(!t._vramOn && t._i.storage && private_calls == 1 && buffer_puts == 1 && buffer_drops == 1 && !image_drops);
    /* a wrapper that may be drawn into needs a colour attachment: without one the import goes back, a private image instead */
    reset(&t); drop_render = 1;
    assert(!nvmtl_surface_image_create(&t, (void *)1, 0, 512, 64, 37, 4, 0, 0, 1));
    assert(!t._vramOn && (t._i.usage & VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT) && private_calls == 1 && image_drops == 1 && buffer_puts == 1);
    /* one that is only sampled keeps it */
    reset(&t); drop_render = 1;
    assert(!nvmtl_surface_image_create(&t, (void *)1, 0, 512, 64, 37, 4, 0, 0, 0));
    assert(t._vramOn && !private_calls && !image_drops && !buffer_puts);
    /* a shader-writable Managed wrapper: a tiled image of its own over the kept import, not current whatever the tag says */
    unsetenv("NVMTL_MANAGED_SURFACE_TILED");
    reset(&t); t._ropt = MTLStorageModeManaged << MTLResourceStorageModeShift; tag_current = 1;
    assert(!nvmtl_surface_image_create(&t, (void *)1, 0, 512, 64, 37, 4, 0, 1, 1));
    assert(t._vramOn && t._surfaceLinear.img == (void *)2 && t._i.img == (void *)3 && t._i.storage && !t._surfSynced);
    assert(alias_calls == 1 && private_calls == 1 && !image_drops && !buffer_puts && !buffer_drops);
    /* Shared storage, or a read-only Managed wrapper: drawn straight into the import */
    reset(&t); tag_current = 1;
    assert(!nvmtl_surface_image_create(&t, (void *)1, 0, 512, 64, 37, 4, 0, 1, 1));
    assert(t._vramOn && !t._surfaceLinear.img && t._i.img == (void *)2 && t._surfSynced && !private_calls);
    reset(&t); t._ropt = MTLStorageModeManaged << MTLResourceStorageModeShift;
    assert(!nvmtl_surface_image_create(&t, (void *)1, 0, 512, 64, 37, 4, 0, 0, 1));
    assert(t._vramOn && !t._surfaceLinear.img && !private_calls);
    /* the tiled image cannot be made: a private image, the import and its image released once */
    for (int c = 0; c < 2; c++) {
        reset(&t); t._ropt = MTLStorageModeManaged << MTLResourceStorageModeShift; typed_fails = 1; cached = c;
        assert(!nvmtl_surface_image_create(&t, (void *)1, 0, 512, 64, 37, 4, 0, 1, 1));
        assert(!t._vramOn && !t._surfaceLinear.img && t._i.img == (void *)4 && t._i.storage && private_calls == 2);
        assert(image_drops == 1 && buffer_puts == 1 && buffer_drops == (c ? 0u : 1u));
    }
    printf("PASS (%s): reported usage, Unknown with and without a storage view, linear storage fallback and its cleanup, "
           "colour-attachment fallback, tiled image of a Managed wrapper\n", program);
    return 0;
}
/* the switch is read once per process, so its off state runs in a process of its own */
static int switch_off(void) {
    setenv("NVMTL_MANAGED_SURFACE_TILED", "0", 1);
    NVMTLTexture t;
    reset(&t); t._ropt = MTLStorageModeManaged << MTLResourceStorageModeShift; tag_current = 1;
    assert(!nvmtl_surface_image_create(&t, (void *)1, 0, 512, 64, 37, 4, 0, 1, 1));
    assert(t._vramOn && !t._surfaceLinear.img && t._i.img == (void *)2 && t._surfSynced && !private_calls);
    printf("PASS (%s, NVMTL_MANAGED_SURFACE_TILED=0): a shader-writable Managed wrapper is drawn straight into the import\n", program);
    return 0;
}
int main(int argc, char **argv) {
    assert(argc == 2 || argc == 3); program = argv[1];
    return argc == 3 ? switch_off() : cases();
}
'''


def compiler():
    if shutil.which('xcrun'):
        return ['xcrun', 'clang']
    for cc in ('cc', 'clang', 'gcc'):
        if shutil.which(cc):
            return [cc]
    return None


def build_and_run(directory, image, name):
    src = pathlib.Path(directory) / (name + '.c')
    exe = pathlib.Path(directory) / name
    src.write_text(FIXTURE.replace('@@POLICY@@', POLICY).replace('@@IMAGE@@', image))
    r = subprocess.run(compiler() + ['-std=c11', '-O1', '-g', '-Wall', '-Wextra', '-Wno-unused-function', '-Wno-unused-label',
                                      '-fsanitize=address,undefined', '-fno-sanitize-recover=all', str(src), '-o', str(exe)],
                       capture_output=True, text=True)
    if r.returncode:
        return None, r.stderr[-2000:]
    out = []
    for args in (['Photo Booth'], ['WindowServer'], ['Photo Booth', 'off']):
        p = subprocess.run([str(exe)] + args, capture_output=True, text=True, timeout=60)
        out.append((p.returncode, (p.stdout + p.stderr)[-1500:]))
    return out, ''


def main():
    if not compiler():
        print('SKIP: no C compiler')
        return 2
    with tempfile.TemporaryDirectory(prefix='nvmtl-drawable-usage-') as d:
        runs, err = build_and_run(d, IMAGE, 'current')
        if runs is None:
            print('FAIL: the fixture does not compile\n' + err)
            return 1
        bad = [o for rc, o in runs if rc]
        for rc, o in runs:
            print(o.strip())
        if bad:
            print('FAIL: the production policy failed a case')
            return 1
        broken = IMAGE.replace(FALLBACK, '                    if (0) {', 1)
        runs, err = build_and_run(d, broken, 'control')
        if runs is None or not any(rc for rc, _ in runs):
            print('FAIL: control: the source without the storage fallback was not refused' + (('\n' + err) if err else ''))
            return 1
        print('ok   control: the source without the storage fallback fails')
        global POLICY
        kept = POLICY
        POLICY = POLICY.replace('if (usage == MTLTextureUsageUnknown && !storage) return usage;',
                                'if (usage == MTLTextureUsageUnknown) return usage;', 1)
        runs, err = build_and_run(d, IMAGE, 'control2') if POLICY != kept else (None, 'the Unknown rule was not found')
        POLICY = kept
        if runs is None or not runs[0][0]:
            print('FAIL: control: Unknown kept over a real storage view was not refused' + (('\n' + err) if err else ''))
            return 1
        print('ok   control: Unknown kept over a real storage view fails as Photo Booth')
        runs, err = build_and_run(d, IMAGE.replace(TILED, '== MTLStorageModeManaged && 0) {', 1), 'control3')
        if runs is None or not runs[0][0]:
            print('FAIL: control: a Managed wrapper drawn straight into the import was not refused' + (('\n' + err) if err else ''))
            return 1
        print('ok   control: the source without the tiled image for Managed wrappers fails')
    return 0


if __name__ == '__main__':
    sys.exit(main())
