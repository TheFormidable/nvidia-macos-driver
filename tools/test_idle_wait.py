#!/usr/bin/env python3
"""test_idle_wait.py: the wiring of the woken resource wait (plugin/nvmtl_idle_wait.h; its threads are in
tools/test_idle_wait.c). Checks in the sources that every change of a buffer's or texture's command-buffer count goes
through nvmtl_idle_add (the order the no-lost-wake proof needs covers them all), that the release which brings either
count to zero tells the queue for that count's address, that NVMTL_GL_WAIT_EVENT is read once into a static the way the
other escape switches are, and that both -waitUntilComplete methods of NVMTLGL.m wait on the queue when it is on and keep
the 50 us polling, the 10 s limit and its log lines when it is 0.

  python3 tools/test_idle_wait.py"""
import pathlib
import re
import sys

root = pathlib.Path(__file__).resolve().parents[1]
objects = (root / 'plugin/NVMTLObjects.m').read_text()
gl = (root / 'plugin/NVMTLGL.m').read_text()
fails = 0


def arm(what, ok):
    global fails
    print(('ok    ' if ok else 'FAIL  ') + what)
    if not ok:
        fails += 1


def body(text, signature):
    if signature not in text:
        return ''
    start = text.index(signature)
    opening = text.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[start:end]


resref = body(objects, 'static inline void nvmtl_resref(id o, int d)')
arm('nvmtl_resref is there', bool(resref))
arm('the buffer count changes through nvmtl_idle_add', 'nvmtl_idle_add(&b->_resRefs, d)' in resref)
arm('the texture count (the parent\'s) changes through nvmtl_idle_add', 'nvmtl_idle_add(&t->_resRefs, d)' in resref)
for who in ('b', 't'):
    add = resref.find(f'const int v = nvmtl_idle_add(&{who}->_resRefs, d);')
    tell = resref.find(f'if (v == 0) nvmtl_idle_settled(&gNVMTLIdleQ, &{who}->_resRefs);', add)
    branch = resref.find('if (d > 0)', add)
    arm(f'every arrival of the {"buffer" if who == "b" else "texture"} count at zero tells the queue for its address, '
        'whichever way the count moved', add >= 0 and tell > add and branch > tell)
arm('the residency kick at zero is kept for both, as it was',
    resref.count('else if (v == 0 && __atomic_load_n(&b->_wantOn, __ATOMIC_ACQUIRE)) nvmtl_res2_kick(1);') == 1
    and resref.count('else if (v == 0 && __atomic_load_n(&t->_wantOn, __ATOMIC_ACQUIRE)) nvmtl_res2_kick(1);') == 1)

writers = []
for path in sorted((root / 'plugin').glob('*.[mch]')):
    text = path.read_text(errors='replace')
    for m in re.finditer(r'__atomic_(?:add_fetch|sub_fetch|fetch_add|fetch_sub|store_n|exchange_n|compare_exchange_n)\s*\(\s*&[^,;]*_resRefs'
                         r'|_resRefs\s*(?:\+\+|--|[-+]?=(?!=))|(?:\+\+|--)\s*[\w>.-]*_resRefs', text):
        writers.append(f'{path.name}: {m.group(0)}')
arm('nothing else changes a _resRefs count' + (' (' + '; '.join(writers) + ')' if writers else ''), not writers)

switch = body(objects, 'static BOOL nvmtl_gl_wait_event_on(void)')
arm('NVMTL_GL_WAIT_EVENT is read once into a static with nvmtl_idle_wait_on',
    'static int on = -1;' in switch and 'getenv("NVMTL_GL_WAIT_EVENT")' in switch and 'nvmtl_idle_wait_on(e)' in switch)
arm('nvmtl_idle_wait.h is included where nvmtl_resref lives',
    objects.index('#include "nvmtl_idle_wait.h"') < objects.index('static inline void nvmtl_resref'))

tex = body(gl, '@implementation NVMTLTexture (NVMTLGL)\n- (void)waitUntilComplete')
sel1 = gl[gl.index('@implementation NVMTLBuffer (NVMTLSel1)'):] if '@implementation NVMTLBuffer (NVMTLSel1)' in gl else ''
buf = body(sel1[:sel1.find('@end')], '- (void)waitUntilComplete')
for name, text, rule, line in (
        ('texture', tex, 'YES', 'GL: waitUntilComplete gave up after 10 s - %d command buffer(s) still name this texture'),
        ('buffer', buf, 'NO', 'waitUntilComplete (buffer) gave up after 10 s - %d command buffer(s) still name it')):
    arm(f'the {name} wait is there', bool(text))
    on = text.find('if (nvmtl_gl_wait_event_on())')
    poll = text.find('usleep(50);')
    arm(f'the {name} wait sleeps on the queue when NVMTL_GL_WAIT_EVENT is on (with the {name} rule)',
        on >= 0 and f'nvmtl_gl_wait_idle(&' in text[on:poll] and f'_resRefs, {rule}, t0)' in text[on:poll])
    arm(f'the {name} wait keeps the 50 us polling and its 10 s limit for NVMTL_GL_WAIT_EVENT=0',
        poll > on and '10000000000ull' in text[text.find('while', on):poll])
    arm(f'the {name} wait logs the same give-up line either way', text.count(line) == 2)
helper = body(gl, 'static BOOL nvmtl_gl_wait_idle(')
arm('the woken wait uses the queue nvmtl_resref tells, with the 10 s limit',
    'nvmtl_idle_wait_for(&gNVMTLIdleQ, refs, textureRule, nvmtl_idle_now_ns(), 10000000000ull, &slept)' in helper)

print('FAILED %d' % fails if fails else 'ok')
sys.exit(1 if fails else 0)
