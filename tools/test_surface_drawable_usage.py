#!/usr/bin/env python3
"""The usage a surface texture reports and the backing it gets, run from the plugin's own source with stubs.

nvmtl_surface_widen_usage, nvmtl_surface_usage and nvmtl_surface_image_create are lifted from plugin/NVMTLDevice.m and
compiled with stand-ins for IOSurface, the import cache and the Vulkan image calls, so each case below can say which
backing a wrapper got and what was released on the way. The cases:
  - an application's drawable that is not framebuffer-only reports ShaderRead and RenderTarget, and ShaderWrite only
    when its backing has a storage view; WindowServer and framebuffer-only textures keep the usage they asked for;
  - an Unknown usage stays Unknown when the backing has no storage view (the optimal-tiled storage twin covers its
    writes) and becomes the explicit bits when it has one (Core Image refuses a destination without ShaderWrite);
  - a writable wrapper whose linear import of the family's VRAM lost its storage view gets a private image instead,
    and the dropped image and the import go back exactly once; a read-only wrapper keeps the import.
Each case runs once as "Photo Booth" and once as "WindowServer". Two controls must fail: the function with the storage
fallback taken out, and the usage rule that keeps Unknown over a real storage view.

The source of the policy is the HighDelay/nvidia-macos-driver-nvk fork (Photo Booth's shared drawables)."""
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
IMAGE = function('static int nvmtl_surface_image_create(')
FALLBACK = '''                    if (stex && !t->_i.storage) {'''
if FALLBACK not in IMAGE:
    print('FAIL: nvmtl_surface_image_create no longer has the storage fallback this test drives')
    sys.exit(1)

FIXTURE = r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef signed char BOOL;
typedef unsigned long NSUInteger;
#define YES 1
#define NO 0
#define MTLTextureUsageUnknown 0ul
#define MTLTextureUsageShaderRead 1ul
#define MTLTextureUsageShaderWrite 2ul
#define MTLTextureUsageRenderTarget 4ul
static const char *program;
#define getprogname() program
#define nvlog(...) ((void)0)
@@POLICY@@
typedef struct { void *img; int storage; } nvk_image;
typedef struct { int live; } nvk_buffer;
typedef struct { nvk_image _i; nvk_buffer _vramBuf; BOOL _vramOn; uint32_t _surfSeed; BOOL _surfSynced; } NVMTLTexture;
typedef void *IOSurfaceRef;
typedef struct { int planes; struct { unsigned row; } p[2]; } nvmtl_pa_surface;
static unsigned long long g_surf_vram, g_surf_private;
static unsigned alias_calls, private_calls, image_drops, buffer_puts, buffer_drops, tag_current;
static int drop_storage, alias_fails, cached;
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
    out->img = (void *)2; out->storage = storage && !drop_storage; return 0;
}
static int nvmtl_vk_image_create_typed(uint32_t w, uint32_t h, uint32_t f, uint32_t bpp, int a8, uint32_t mips, int storage,
                                       uint32_t type, uint32_t layers, nvk_image *out) {
    (void)w; (void)h; (void)f; (void)bpp; (void)a8; (void)mips; (void)type; (void)layers;
    private_calls++; out->img = (void *)3; out->storage = storage; return 0;
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
    drop_storage = alias_fails = cached = 0;
}
int main(int argc, char **argv) {
    assert(argc == 2); program = argv[1];
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
    assert(!nvmtl_surface_image_create(&t, (void *)1, 0, 512, 64, 37, 4, 0, 1));
    assert(t._vramOn && t._i.storage && alias_calls == 1 && !private_calls && !image_drops && !buffer_puts && !buffer_drops);
    /* the import lost its storage view: a writable private image; the import and its image go back once */
    for (int c = 0; c < 2; c++) {
        reset(&t); drop_storage = 1; cached = c;
        assert(!nvmtl_surface_image_create(&t, (void *)1, 0, 512, 64, 37, 4, 0, 1));
        assert(!t._vramOn && t._i.storage && t._i.img == (void *)3 && private_calls == 1 && image_drops == 1 && buffer_puts == 1);
        assert(buffer_drops == (c ? 0u : 1u));
    }
    /* a read-only wrapper keeps the import even without a storage view */
    reset(&t); drop_storage = 1; tag_current = 1;
    assert(!nvmtl_surface_image_create(&t, (void *)1, 0, 512, 64, 37, 4, 0, 0));
    assert(t._vramOn && !t._i.storage && t._surfSynced && t._surfSeed == 7 && !private_calls && !image_drops && !buffer_puts);
    /* the import failed outright: a private image, writable because the format is */
    reset(&t); alias_fails = 1;
    assert(!nvmtl_surface_image_create(&t, (void *)1, 0, 512, 64, 37, 4, 0, 0));
    assert(!t._vramOn && t._i.storage && private_calls == 1 && buffer_puts == 1 && buffer_drops == 1 && !image_drops);
    printf("PASS (%s): reported usage, Unknown with and without a storage view, linear storage fallback and its cleanup\n", program);
    return 0;
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
    for program in ('Photo Booth', 'WindowServer'):
        p = subprocess.run([str(exe), program], capture_output=True, text=True, timeout=60)
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
    return 0


if __name__ == '__main__':
    sys.exit(main())
