#!/usr/bin/env python3
"""How the plugin puts a two-plane surface (NV12) in its planes' own VRAM, checked in its source.

A plane's texture lives over its own VRAM only after the process's self-test on a private surface has passed, and that
self-test runs on a queue of its own, never on the thread that asked for the texture. A plane's VRAM is asked for with
its byte range (the ranged plane word) on import only: a dirty mark names the plain plane. A command buffer that writes
such a surface reports "scheduled" through nvmtl_schedule_after_surface (tools/test_schedule_policy.c holds its table).
Where the accelerator's half is in the tree (kexts/NVRM/accel/nvaccel_plane_range.h), the words the plugin builds are
decoded by the kernel's own functions. Each text rule is also run on a copy of the source with the rule broken, which
it must refuse.

usage: test_surf_planes_plugin.py [repo root]"""
import pathlib, re, shutil, subprocess, sys, tempfile

root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1])
dev = (root / 'plugin/NVMTLDevice.m').read_text()
evs = (root / 'plugin/NVMTLEventSync.m').read_text()
vkc = (root / 'plugin/nvmtl_vk.c').read_text()
fails = 0


def check(name, ok, detail=''):
    global fails
    print(('ok   ' if ok else 'FAIL ') + name + (f' ({detail})' if detail else ''))
    fails += 0 if ok else 1


def function(src, signature):
    i = src.find(signature)
    if i < 0:
        return ''
    return src[i:src.index('\n}\n', i) + 3]


def gated(src):
    """The planar branch of nvmtl_surface_image_create: eligibility, then the self-test's verdict, then the import."""
    f = function(src, 'static int nvmtl_surface_image_create(')
    e, r, c = f.find('nvmtl_pa_eligible('), f.find('nvmtl_plane_vram_ready()'), f.find('nvmtl_vk_plane_image_create(')
    p = f.find('g_surf_private++')
    return 0 <= e < r < c < p


def dispatched(src):
    """The self-test runs only from nvmtl_plane_vram_ready, inside a dispatch_async block, when the state says start."""
    ready = function(src, 'static int nvmtl_plane_vram_ready(')
    calls = [m.start() for m in re.finditer(r'\bnvmtl_plane_selftest\(\)', src)]
    inside = ready and all(src.find(ready) <= c < src.find(ready) + len(ready) for c in calls)
    return bool(ready) and len(calls) == 1 and inside and bool(
        re.search(r'if \(st < 0\) dispatch_async\([^;]*\^\{ nvmtl_plane_selftest\(\); \}\);', ready))


def scheduled(src):
    """The scheduled-after-publish loop asks the policy and no longer holds the old rule."""
    return ('nvmtl_schedule_after_surface(after, ' in src and '#include "nvmtl_schedule_policy.h"' in src
            and '!r->_vramOn && (after != 3' not in src)


def ranged_on_import_only(src):
    """The plane flag and the ranged size word are made in one place, the ranged import; a mark never carries them."""
    words = [m.start() for m in re.finditer(r'\bnvmtl_pa_(plane|size)_word\(', src)]
    imp = function(src, 'int nvmtl_vk_surface_vram_ranged(')
    a = src.find(imp)
    mark = function(src, 'int nvmtl_vk_surface_dirty(') + function(src, 'int nvmtl_vk_surface_dirty_try(')
    return (bool(imp) and len(words) == 2 and all(a <= w < a + len(imp) for w in words)
            and bool(mark) and 'RANGED' not in mark and 'nvmtl_pa_' not in mark)


check('a two-plane surface takes its planes\' VRAM only after eligibility and a passed self-test', gated(dev))
check('control: an import before the self-test\'s verdict is refused',
      not gated(dev.replace('else if (!nvmtl_plane_vram_ready()) pwhy', 'else if (0) pwhy', 1)))
check('the self-test is started once, on a queue of its own', dispatched(dev))
check('control: a self-test run on the caller\'s thread is refused',
      not dispatched(dev.replace('dispatch_async(dispatch_get_global_queue(QOS_CLASS_UTILITY, 0), ^{ nvmtl_plane_selftest(); });',
                                 'nvmtl_plane_selftest();', 1)))
check('a command buffer\'s "scheduled" asks nvmtl_schedule_after_surface', scheduled(evs))
check('control: the old rule in the loop is refused',
      not scheduled(evs.replace('if (nvmtl_schedule_after_surface(after, r->_surf != NULL, r->_vramOn, planes, waits))',
                                'if (after == 1 || (r->_surf && !r->_vramOn && (after != 3 || planes > 1)))', 1)))
check('the ranged plane word is used on import only, never on a dirty mark', ranged_on_import_only(vkc))
check('control: a dirty mark that carries the ranged word is refused',
      not ranged_on_import_only(vkc.replace('return nvmtl_vk_surface_mark_newest(surfaceID, plane, 0); }',
                                            'return nvmtl_vk_surface_mark_newest(surfaceID, nvmtl_pa_plane_word(plane), 0); }', 1)))

# ---- the kernel's decoding of the plugin's words ----
kh = root / 'kexts/NVRM/accel/nvaccel_plane_range.h'
cc = shutil.which('cc') or shutil.which('clang') or shutil.which('gcc')
if not kh.exists():
    check('the accelerator\'s half is in the tree (kexts/NVRM/accel/nvaccel_plane_range.h)', False,
          'the plugin asks for ranged grants that only that kernel half grants')
elif not cc:
    print('not run: the kernel cross-check needs a C compiler')
else:
    prog = r'''
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#define NVRM_SS_KERNEL_ABI 1
#include "nvrm_surfshare_abi.h"
#include "accel/nvaccel_plane_range.h"
#include "nvmtl_plane_alias.h"
static int bad;
#define T(c) do { if (!(c)) { printf("arm failed: %s\n", #c); bad++; } } while (0)
int main(int argc, char **argv) {
    (void)argv;
    T(NVMTL_PA_PLANE_RANGED == NVRM_SS_PLANE_RANGED);
    nvmtl_pa_surface s[3]; memset(s, 0, sizeof s);
    nvmtl_pa_test_layout(&s[0]);
    s[1].planes = 2; s[1].alloc = 1920u * 1088u * 3u / 2u;
    s[1].p[0] = (nvmtl_pa_plane){ 0, 1920, 1920, 1080, 1, 1, 1 };
    s[1].p[1] = (nvmtl_pa_plane){ 1920u * 1088u, 1920, 960, 540, 2, 1, 1 };
    s[2].planes = 2; s[2].alloc = 3840u * 2160u * 3u / 2u + 8192u;
    s[2].p[0] = (nvmtl_pa_plane){ 4096, 3840, 3840, 2160, 1, 1, 1 };
    s[2].p[1] = (nvmtl_pa_plane){ 4096 + 3840u * 2160u + 4096, 3840, 1920, 1080, 2, 1, 1 };
    int checked = 0;
    for (int i = 0; i < 3; i++) for (uint32_t p = 0; p < 2; p++) {
        const nvmtl_pa_plane *q = &s[i].p[p];
        if (nvmtl_pa_eligible(&s[i], p, q->w, q->h, q->bpe)) { printf("layout %d plane %u not eligible\n", i, p); bad++; continue; }
        unsigned long long off = 0, len = 0;
        nvaccel_range_decode(nvmtl_pa_size_word(q->off, nvmtl_pa_len(q)), &off, &len);
        T(off == q->off && len == nvmtl_pa_len(q));
        T(nvaccel_range_check(off, len, s[i].alloc, len) == 0);
        T(nvaccel_range_same(off, len, off, len));
        checked++;
    }
    /* control: a plane the plugin refuses as outside its surface is one the kernel refuses too */
    nvmtl_pa_surface o = s[1]; o.p[1].off = o.alloc - 4096;
    T(nvmtl_pa_eligible(&o, 1, 960, 540, 2) != 0);
    { unsigned long long off, len; nvaccel_range_decode(nvmtl_pa_size_word(o.p[1].off, nvmtl_pa_len(&o.p[1])), &off, &len);
      T(nvaccel_range_check(off, len, o.alloc, len) == NVRM_SS_BAD_RANGE); }
    printf("%d planes\n", checked);
    return bad || argc > 1;
}
'''
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        (d / 'x.c').write_text(prog)
        r = subprocess.run([cc, '-std=c11', '-Wall', '-Wextra', '-Werror', '-I', str(root / 'plugin'), '-I', str(root / 'kexts/NVRM'),
                            str(d / 'x.c'), '-o', str(d / 'x')], capture_output=True, text=True)
        check('the plugin\'s words and the kernel\'s decoding compile together', r.returncode == 0, r.stderr.strip()[-400:])
        if r.returncode == 0:
            a = subprocess.run([str(d / 'x')], capture_output=True, text=True)
            check('the kernel decodes every eligible plane\'s ranged grant to the plane\'s bytes, and refuses one outside',
                  a.returncode == 0 and '6 planes' in a.stdout, a.stdout.strip()[-400:])
            n = subprocess.run([str(d / 'x'), 'control'], capture_output=True, text=True)
            check('control: the harness can fail', n.returncode != 0)

print('all passed' if not fails else f'{fails} FAILED')
sys.exit(1 if fails else 0)
