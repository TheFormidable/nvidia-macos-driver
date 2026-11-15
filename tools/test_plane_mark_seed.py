#!/usr/bin/env python3
"""The planar self-test measures what a VRAM mark does to an IOSurface's seed, run on the host without a GPU.

The VRAM tags of a surface's other planes follow a mark of one plane only when the mark took and the seed moved by
exactly one around it (nvmtl_ps_mark_moved, tools/test_plane_seed.c). That is the mark's own move only if every mark
that takes moves the seed by exactly one, which is the accelerator family's doing (IOSurfaceDeviceCache::setCurrent)
and not in this driver's code. Were there a mark that takes and moves nothing, a move by one around it would be
someone else's CPU write, and the follow would call a plane's VRAM current over newer CPU pixels. So each process's
planar self-test (nvmtl_vk_plane_selftest, plugin/nvmtl_vk.c) reads the seed around every mark it makes, one of them a
mark of VRAM that is already newest, and nvmtl_vk_plane_mark_moves_seed() says yes only after a self-test that passed
and saw every mark move the seed by exactly one.

This compiles the self-test's own text (lifted from plugin/nvmtl_vk.c, not a copy) against a model of the surface, the
planes' VRAM and the family's page-off, and runs it with families that move the seed in different ways. Two copies of
the source with the rule broken must fail.

usage: test_plane_mark_seed.py [repo root]"""
import os, pathlib, re, subprocess, sys, tempfile

root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1])
vkc = (root / 'plugin/nvmtl_vk.c').read_text()


def function(src, signature):
    i = src.index(signature)
    return src[i:src.index('\n}\n', i) + 3]


def line(src, start):
    i = src.index(start)
    return src[i:src.index('\n', i) + 1]


def lifted(src):
    return (line(src, 'static _Atomic int g_pa_mark_seed;') + line(src, 'int nvmtl_vk_plane_mark_moves_seed(void)')
            + line(src, 'enum { NVMTL_PA_GEN_A = 1,') + function(src, 'static int nvmtl_pa_look(')
            + function(src, 'static int nvmtl_pa_gpu_write(') + function(src, 'static int nvmtl_pa_mark(')
            + function(src, 'int nvmtl_vk_plane_selftest('))


MODEL = r'''
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "nvmtl_plane_alias.h"
#include "nvmtl_plane_seed.h"
#define VK_FORMAT_R8_UNORM 9
#define VK_FORMAT_R8G8_UNORM 16
typedef struct { void *buf; } nvk_buffer;
typedef struct { void *img; uint32_t plane; } nvk_image;
/* macros, not functions: a Mac's own headers declare these two */
#define clock_gettime_nsec_np(c) ((uint64_t)0)
#define getprogname() "test"
#define nvlog(...) ((void)0)
/* the model: the surface's system memory, each plane's VRAM, which planes' VRAM the family holds newest, the seed */
enum { BY_ONE, NOT_WHEN_NEWEST, BY_TWO, NEVER };
static nvmtl_pa_surface L;
static uint8_t *mem, *vram[2];
static int newest[2], family, pageoff_broken, refuse_call;
static uint32_t seed; static unsigned dirties;
static void pageoff(void)
{
    for (int p = 0; p < 2; p++) {
        if (!newest[p]) continue;
        if (!pageoff_broken) memcpy(mem + L.p[p].off, vram[p], (size_t)nvmtl_pa_len(&L.p[p]));
        newest[p] = 0;
    }
}
static uint8_t *host_lock(void *ctx, int read_only) { (void)ctx; pageoff(); if (!read_only) seed++; return mem; }
static void host_unlock(void *ctx, int read_only) { (void)ctx; (void)read_only; }
static uint32_t host_seed(void *ctx) { (void)ctx; return seed; }
static int nvmtl_vk_plane_image_create(uint32_t sid, const nvmtl_pa_surface *s, uint32_t plane, uint32_t w, uint32_t h, uint32_t vkf,
                                       uint32_t bpp, int a8, int storage, nvk_buffer *vb, nvk_image *img)
{
    (void)sid; (void)s; (void)w; (void)h; (void)vkf; (void)bpp; (void)a8; (void)storage;
    vb->buf = vram[plane]; img->img = vram[plane]; img->plane = plane; return 0;
}
static int nvmtl_vk_image_write(void *q, nvk_image *img, const void *src, size_t row)
{
    (void)q; const nvmtl_pa_plane *p = &L.p[img->plane];
    for (uint32_t y = 0; y < p->h; y++) memcpy(vram[img->plane] + (size_t)y * p->row, (const uint8_t *)src + (size_t)y * row, row);
    return 0;
}
static int nvmtl_vk_image_read(void *q, nvk_image *img, void *dst, size_t row)
{
    (void)q; const nvmtl_pa_plane *p = &L.p[img->plane];
    for (uint32_t y = 0; y < p->h; y++) memcpy((uint8_t *)dst + (size_t)y * row, vram[img->plane] + (size_t)y * p->row, row);
    return 0;
}
static void nvmtl_vk_image_upload_barrier(nvk_image *img) { (void)img; }
static void nvmtl_vk_image_destroy(nvk_image *img) { (void)img; }
static void nvmtl_vk_buffer_destroy(nvk_buffer *b) { (void)b; }
/* the family's mark: the plane's VRAM is newest; what it does to the seed is the family under test */
static int nvmtl_vk_surface_dirty(uint32_t sid, uint32_t plane)
{
    (void)sid;
    if (++dirties == (unsigned)refuse_call) return -1;
    const int was = newest[plane]; newest[plane] = 1;
    if (family == BY_ONE || (family == NOT_WHEN_NEWEST && !was)) seed++;
    else if (family == BY_TWO) seed += 2;
    return 0;
}
/*LIFTED*/
static int fails;
#define ARM(name, cond) do { const int ok = !!(cond); printf("%s  %s\n", ok ? "ok  " : "FAIL", name); if (!ok) fails++; } while (0)
static int run(int fam, int with_seed, int broken, int refuse)
{
    nvmtl_pa_test_layout(&L);
    free(mem); free(vram[0]); free(vram[1]);
    mem = calloc(1, (size_t)L.alloc); vram[0] = calloc(1, (size_t)nvmtl_pa_len(&L.p[0])); vram[1] = calloc(1, (size_t)nvmtl_pa_len(&L.p[1]));
    newest[0] = newest[1] = 0; seed = 40; dirties = 0; family = fam; pageoff_broken = broken; refuse_call = refuse;
    atomic_store(&g_pa_mark_seed, 0);
    const nvmtl_pa_host hst = { NULL, host_lock, host_unlock, with_seed ? host_seed : NULL };
    nvmtl_pa_result r;
    return nvmtl_vk_plane_selftest(7, &L, &hst, &r);
}
int main(void)
{
    ARM("a family whose every mark moves the seed by one: the self-test passes and says so",
        run(BY_ONE, 1, 0, 0) == 0 && nvmtl_vk_plane_mark_moves_seed() == 1);
    ARM("the self-test makes six marks, one of them of VRAM that is already newest", dirties == 6);
    ARM("a family whose mark of VRAM already newest moves nothing: the self-test passes, and nothing may follow",
        run(NOT_WHEN_NEWEST, 1, 0, 0) == 0 && nvmtl_vk_plane_mark_moves_seed() == 0);
    ARM("a family whose marks move the seed by two: nothing may follow", run(BY_TWO, 1, 0, 0) == 0 && nvmtl_vk_plane_mark_moves_seed() == 0);
    ARM("a family whose marks never move the seed: nothing may follow", run(NEVER, 1, 0, 0) == 0 && nvmtl_vk_plane_mark_moves_seed() == 0);
    ARM("a host that cannot say the seed: the self-test passes, unmeasured, and nothing may follow",
        run(BY_ONE, 0, 0, 0) == 0 && nvmtl_vk_plane_mark_moves_seed() == 0);
    ARM("a self-test that fails says nothing about the seed", run(BY_ONE, 1, 1, 0) == 4 && nvmtl_vk_plane_mark_moves_seed() == 0);
    ARM("the mark of VRAM already newest refused: the verdict stands (passed), and nothing may follow",
        run(BY_ONE, 1, 0, 3) == 0 && nvmtl_vk_plane_mark_moves_seed() == 0 && dirties == 6);
    ARM("a first mark refused still fails the self-test at step 3, as before", run(BY_ONE, 1, 0, 1) == 3);
    return fails ? 1 : 0;
}
'''


def build_and_run(src, quiet=False):
    with tempfile.TemporaryDirectory() as d:
        c, exe = pathlib.Path(d) / 't.c', pathlib.Path(d) / 't'
        c.write_text(MODEL.replace('/*LIFTED*/', lifted(src)))
        cc = os.environ.get('CC', 'cc')
        b = subprocess.run([cc, '-std=c11', '-Wall', '-Wextra', '-Wno-unused-function', '-I', str(root / 'plugin'), str(c), '-o', str(exe)],
                           capture_output=True, text=True)
        if b.returncode:
            print(b.stdout + b.stderr)
            return None
        r = subprocess.run([str(exe)], capture_output=True, text=True)
        if not quiet:
            print(r.stdout, end='')
        return r.returncode


fails = 0


def check(name, ok):
    global fails
    print(('ok    ' if ok else 'FAIL  ') + name)
    fails += 0 if ok else 1


check('the self-test of the source measures the seed around its marks', build_and_run(vkc) == 0)
always = vkc.replace('atomic_store(&g_pa_mark_seed, holds ? 1 : 2);', 'atomic_store(&g_pa_mark_seed, 1);', 1)
check('control: a self-test that says yes whatever it measured is caught', always != vkc and build_and_run(always, quiet=True) == 1)
no_remark = vkc.replace('    if (nvmtl_pa_mark(surfaceID, 0, hst, &q)) q.marks++;', '', 1)
check('control: a self-test without the mark of VRAM already newest is caught', no_remark != vkc and build_and_run(no_remark, quiet=True) == 1)
unmeasured = vkc.replace('if (hst->seed) nvmtl_ps_premise_note(q, before, hst->seed(hst->ctx));', 'nvmtl_ps_premise_note(q, 0, 1);', 1)
check('control: a self-test that notes a move it did not read is caught', unmeasured != vkc and build_and_run(unmeasured, quiet=True) == 1)
print('PASS' if not fails else f'{fails} FAILED')
sys.exit(1 if fails else 0)
