#!/usr/bin/env python3
"""Where the plugin calls the kernel surface fence, read from the source (the Objective-C cannot be compiled or run
here, so its twelve lines are held in place by text).

Each line is present exactly once, in its function, at the place its order matters:
  - the compositor hold is opened after the commit's VRAM marks and before its pending marks (the marks skip what the
    kernel took, so the open has to come first; a pending mark before the VRAM mark is the deadlock the comment above
    nvmtl_ws_mark_pending explains);
  - the application hold is opened before the first busy tag is written (the tag's pad word says what the open took);
  - everything a command buffer holds is closed by the first statement of nvmtl_busy_mark_done, before its early return,
    so every command buffer closes its holds on every path;
  - a part's holds are armed right after its submission and before anything waits for it, on both submit paths;
  - the reader's kernel wait follows the first early return of nvmtl_busy_wait (a process never waits for itself) and
    precedes the older poll, which stays;
  - the compositor wait in nvmtlSurfaceIn follows the busy-tag check and the early return for an unchanged surface.
And: the older mechanisms are all still there; surface 0 is forwarded before the mark routing; NVK's entry is looked up
once under its published name and called through its published prototype, which is the one NVK's patch exports (that arm
needs nvk/nvk-fence.patch, from the NVK side of this change); no NVMTL_FENCE_* number is defined twice.
Negative controls: each check is run again on a copy of the source with its line moved or removed, and must fail.
"""
import pathlib, re, sys

root = pathlib.Path(__file__).resolve().parents[1]
FILES = {k: (root / 'plugin' / k).read_text() for k in
         ('NVMTLEventSync.m', 'NVMTLDevice.m', 'NVMTLObjects.m', 'NVMTLObjects.h', 'nvmtl_vk.c', 'nvmtl_vk.h', 'nvmtl_fence.h')}
PATCH = root / 'nvk' / 'nvk-fence.patch'
PUBLISHED = ('uint32_t nvmtl_nvk_fence_v1(VkDevice device, uint32_t op, uint32_t flags, uint64_t cookie, uint32_t count, '
            'const uint32_t *ids, const uint8_t *planes, VkFence fence, uint32_t pid, uint32_t timeout_ms, uint32_t out[6])')

# the twelve lines, numbered as in the message of the commit that added them
ROW = {
    1: 'nvmtl_fence_hold _fence;',
    2: 'BOOL early; uint64_t fence; }',
    3: 'static void nvmtl_fence_collect(NVMTLCommandBuffer *cb) {',
    4: 'nvmtl_vk_fence_open(&cb->_fence, NVMTL_FENCE_F_COMPOSITOR | (holds->early ? NVMTL_FENCE_F_AUTO : 0));',
    5: 'nvmtl_vk_fence_close(holds->fence, sid);',
    6: 'nvmtl_vk_fence_open(&cb->_fence, 0);',
    7: 'nvmtl_vk_fence_close_all(&cb->_fence);',
    8: 'nvmtl_vk_fence_arm(&self->_fence, (uint32_t)(_nextSubmit - 1), f);',
    9: 'nvmtl_vk_fence_arm(&self->_fence, (uint32_t)_nextPart, f);',
    10: 'done ? 0 : nvmtl_vk_fence_tag_word((uint32_t)IOSurfaceGetID(surf))',
    11: 'if (t.pad == NVMTL_BUSY_KFENCE && nvmtl_vk_fence_wait((uint32_t)IOSurfaceGetID(surf), (uint32_t)1 << (plane & 7), (uint32_t)t.pid)) return;',
    12: 'if (!r->_clientWritten) nvmtl_vk_fence_wait_compositor((uint32_t)IOSurfaceGetID(r->_surf));',
}
WHERE = {1: 'NVMTLObjects.h', 2: 'NVMTLEventSync.m', 3: 'NVMTLEventSync.m', 4: 'NVMTLEventSync.m', 5: 'NVMTLEventSync.m',
         6: 'NVMTLEventSync.m', 7: 'NVMTLEventSync.m', 8: 'NVMTLEventSync.m', 9: 'NVMTLEventSync.m', 10: 'NVMTLDevice.m',
         11: 'NVMTLDevice.m', 12: 'NVMTLObjects.m'}


def body(text, head, end='\n}\n'):
    """The text of the function that starts with `head`, up to its closing brace at column 0."""
    a = text.index(head)
    return text[a:text.index(end, a) + len(end)]


def check(f):
    """Raise AssertionError with a reason for the first thing that is wrong in the file texts `f`."""
    es, dev, obj, objh, vk, vkh, fh = (f[k] for k in ('NVMTLEventSync.m', 'NVMTLDevice.m', 'NVMTLObjects.m',
                                                       'NVMTLObjects.h', 'nvmtl_vk.c', 'nvmtl_vk.h', 'nvmtl_fence.h'))
    for n, line in ROW.items():
        total = sum(t.count(line) for k, t in f.items() if k.endswith(('.m', '.h')) and k != 'nvmtl_fence.h')
        assert total == 1, f'row {n} is in the Objective-C {total} times, not once: {line}'
        assert line in f[WHERE[n]], f'row {n} is not in {WHERE[n]}'
    assert '@interface NVMTLCommandBuffer () { @public NSDictionary *_busyMarks; nvmtl_fence_hold _fence; } @end' in objh, 'row 1: the ivar is not beside _busyMarks'
    assert '@interface NVMTLSurfaceHolds : NSObject { @public NSMutableSet *ids; NSSet *dirtied; BOOL early; uint64_t fence; } @end' in es, 'row 2'

    collect = body(es, ROW[3])
    assert es.index(ROW[3]) < es.index('static void nvmtl_ws_mark_pending('), 'row 3: nvmtl_fence_collect is defined after its first use'
    assert 'nvmtl_fence_hold_reset(&cb->_fence);' in collect and 'nvmtl_fence_hold_note(&cb->_fence, (uint32_t)IOSurfaceGetID(t->_surf), (uint32_t)t->_plane, (uint32_t)i);' in collect, \
        'row 3: the walk does not reset the record and note each surface with its plane and part index'
    assert es.count('nvmtl_fence_collect(cb);') == 2, 'nvmtl_fence_collect is not called by exactly the two commit paths'

    pend = body(es, 'static void nvmtl_ws_mark_pending(')
    p4 = pend.find(ROW[4])
    assert p4 >= 0, 'row 4 is not in nvmtl_ws_mark_pending'
    assert pend.index('if (!ids) return;') < pend.index('nvmtl_fence_collect(cb);') < p4, 'row 4: collect does not come before the open, after the surfaces were found'
    assert pend.index('nvmtl_vk_surface_dirty(') < p4 < pend.index('nvmtl_ws_mark(n.unsignedIntValue, YES)'), \
        'row 4: the compositor hold is not opened between the VRAM marks and the pending marks'
    assert pend.index(ROW[4]) < pend.index('holds->fence = cb->_fence.cookie;') < pend.index('@synchronized (nvmtl_ws_lock())'), \
        'row 4: the cookie is not kept in holds->fence before the pending marks'

    rel = body(es, 'static void nvmtl_ws_release(')
    assert rel.index('if (![holds->ids containsObject:n]) return;') < rel.index(ROW[5]) < rel.index('[holds->ids removeObject:n];'), \
        'row 5: the close is not right after the holder check (before the holder count is looked at)'

    commit = body(es, 'static void nvmtl_busy_mark_commit(')
    assert commit.index('if (!nvmtl_busy_marks_on()) return;') < commit.index('nvmtl_fence_collect(cb);') < commit.index(ROW[6]) < commit.index('nvmtl_busy_write('), \
        'row 6: the application hold is not opened before the first busy tag'

    done = body(es, 'static void nvmtl_busy_mark_done(')
    first = done.split('{', 1)[1].strip().split('\n', 1)[0]
    assert first.startswith(ROW[7]), 'row 7 is not the first statement of nvmtl_busy_mark_done'

    ahead = body(es, '- (BOOL)nvmtlSubmitAhead {')
    assert re.search(r'part->inflight = f;\n\s*' + re.escape(ROW[8]), ahead), 'row 8 does not follow part->inflight = f; in nvmtlSubmitAhead'
    assert ahead.index('_nextSubmit++;') < ahead.index(ROW[8]), 'row 8: _nextSubmit has not moved past the part yet'
    adv = body(es, '- (BOOL)nvmtlAdvancePart {')
    m = re.search(r'result = nvmtl_vk_submit_begin\(&part->commands, f\);\n\s*if \(!result\) \{\n\s*' + re.escape(ROW[9]), adv)
    assert m, 'row 9 is not the first line inside if (!result) { after nvmtl_vk_submit_begin in nvmtlAdvancePart'
    assert adv.index(ROW[9]) < adv.index('result = nvmtl_vk_submit_finish(f);'), 'row 9: armed after the wait for the part'

    write = body(dev, 'static void nvmtl_busy_write(IOSurfaceRef surf, NSUInteger plane, uint64_t seq, int done)')
    assert 'getpid(), ' + ROW[10] + ' };' in write, 'row 10: the pad word is not the last field of the tag'
    wait = body(dev, 'static void nvmtl_busy_wait(IOSurfaceRef surf, NSUInteger plane)')
    early = 'if (!nvmtl_busy_read(surf, plane, &t) || t.done || t.pid == getpid()) return;'
    assert wait.index(early) < wait.index(ROW[11]) < wait.index('usleep(NVMTL_BUSY_POLL_US);'), \
        'row 11: the kernel wait is not between the first early return and the older poll'
    assert re.search(re.escape(early) + r'\n(\s*//[^\n]*\n)?\s*' + re.escape(ROW[11]), wait), 'row 11 does not follow the first early return'

    sin = body(obj, '- (void)nvmtlSurfaceIn {')
    unchanged = 'if (r->_surfSynced && IOSurfaceGetSeed(r->_surf) == r->_surfSeed && (r->_vramOn || vgen == r->_surfGen)) return;'
    foreign = 'if (!r->_clientWritten && nvmtl_busy_foreign(r->_surf, r->_plane)) { r->_clientWritten = YES; nvmtl_busy_wait(r->_surf, r->_plane); }'
    assert sin.index(unchanged) < sin.index(foreign) < sin.index(ROW[12]), 'row 12: not after the unchanged-surface return and the busy-tag check'

    # the older mechanisms stay: every fallback lands on them
    for old, where in (('nvmtl_busy_write(t->_surf, t->_plane, seq, 0);', es), ('nvmtl_busy_write(t->_surf, t->_plane, seq, 1);', es),
                       ('usleep(NVMTL_BUSY_POLL_US);', dev), ('nvmtl_ws_mark(n.unsignedIntValue, YES)', es),
                       ('nvmtl_ws_mark(sid, NO);', es), ('nvmtl_vk_surface_gpu_mark(0, 0) == 0', es)):
        assert old in where, 'an older mechanism is gone: ' + old

    # the C side
    assert re.search(r'#include "nvmtl_slots.h"\n#include "nvmtl_fence.h"\n', vkh), 'nvmtl_vk.h does not include nvmtl_fence.h right after nvmtl_slots.h'
    gm = body(vk, 'uint32_t nvmtl_vk_surface_gpu_mark(uint32_t surfaceID, int pending)')
    assert gm.index('if (!surfaceID) return pnvk_surface_dirty(g_dev, surfaceID, mark);') < gm.index('nvmtl_fence_mark_route('), \
        'nvmtl_vk_surface_gpu_mark does not forward surface 0 before it consults the routing'
    assert vk.count('dlsym(g_lib, "nvmtl_nvk_fence_v1")') == 1, 'NVK\'s fence entry is not looked up exactly once'
    td = re.search(r'^typedef uint32_t nvmtl_nvk_fence_v1_fn(\(.*?\));$', vk, re.M)
    assert td and 'uint32_t nvmtl_nvk_fence_v1' + td.group(1) == PUBLISHED, 'the plugin\'s prototype of nvmtl_nvk_fence_v1 is not the published one'
    assert vk.count('pnvk_fence(') == 2, 'NVK\'s fence entry is called from more than the two places that check it first'
    defs = re.findall(r'^#define (NVMTL_FENCE_\w+|NVMTL_BUSY_KFENCE)\b', '\n'.join(t for k, t in f.items() if k != 'nvmtl_fence.h'), re.M)
    assert not defs, 'a fence number is defined outside nvmtl_fence.h: ' + ', '.join(defs)


def patch_signature():
    """The signature NVK's patch exports, on one line, or None when the patch is not in this tree."""
    if not PATCH.exists():
        return None
    text = PATCH.read_text()
    m = re.search(r'^\+nvmtl_nvk_fence_v1\((.*?)\)\n\+\{', text, re.M | re.S)
    if not m:
        return ''
    args = ' '.join(part.lstrip('+').strip() for part in m.group(1).split('\n'))
    return 'uint32_t nvmtl_nvk_fence_v1(' + args + ')'


fails = 0
try:
    check(FILES)
    print('ok   the twelve lines, their order, the older mechanisms and the C side')
except (AssertionError, ValueError) as e:   # ValueError: a line a check looks for is not there at all
    print('FAIL', e); fails += 1

sig = patch_signature()
if sig is None:
    print('FAIL nvk/nvk-fence.patch is not in this tree: the prototype cannot be compared with what NVK exports '
          '(expected until the NVK side of the fence is merged)'); fails += 1
elif sig != PUBLISHED:
    print('FAIL NVK exports', sig or 'no nvmtl_nvk_fence_v1', 'and the plugin calls', PUBLISHED); fails += 1
else:
    print('ok   the plugin calls nvmtl_nvk_fence_v1 with the signature NVK exports')

# negative controls: each mutation must make check() fail
def mutate(name, old, new, count=1):
    g = dict(FILES)
    assert g[name].count(old) >= 1, 'control anchor missing: ' + old
    g[name] = g[name].replace(old, new, count)
    return g

def move_after(name, line, anchor):
    """`line` (with its indentation) taken out and put again after the first occurrence of `anchor`."""
    t = FILES[name]
    a = t.index(line); s = t.rindex('\n', 0, a) + 1; e = t.index('\n', a) + 1
    piece = t[s:e]; t = t[:s] + t[e:]
    i = t.index(anchor) + len(anchor)
    i = t.index('\n', i) + 1
    g = dict(FILES); g[name] = t[:i] + piece + t[i:]
    return g

controls = [
    ('row 4 after the pending marks', move_after('NVMTLEventSync.m', ROW[4], 'for (NSNumber *n in ids) { [nvmtl_ws_pending_holds addObject:n]; nvmtl_ws_mark(n.unsignedIntValue, YES); }')),
    ('row 4 missing', mutate('NVMTLEventSync.m', ROW[4], '')),
    ('row 5 after the holder count', move_after('NVMTLEventSync.m', ROW[5], 'if ([nvmtl_ws_pending_holds countForObject:n]) return;')),
    ('row 6 after the tags', move_after('NVMTLEventSync.m', ROW[6], 'nvmtl_busy_write(t->_surf, t->_plane, seq, 0);')),
    ('row 7 after the early return', move_after('NVMTLEventSync.m', ROW[7], '    if (!marks) return;')),
    ('row 8 missing', mutate('NVMTLEventSync.m', ROW[8], '')),
    ('row 9 after the wait', move_after('NVMTLEventSync.m', ROW[9], 'result = nvmtl_vk_submit_finish(f);')),
    ('row 10 on the done tag too', mutate('NVMTLDevice.m', ROW[10], 'nvmtl_vk_fence_tag_word((uint32_t)IOSurfaceGetID(surf))')),
    ('row 11 before the early return', mutate('NVMTLDevice.m', 'nvmtl_busy_tag t;\n    if (!nvmtl_busy_read(surf, plane, &t) || t.done || t.pid == getpid()) return;\n    // the writer\'s kernel fence',
                                              'nvmtl_busy_tag t;\n    // the writer\'s kernel fence')),
    ('row 12 twice', mutate('NVMTLObjects.m', ROW[12], ROW[12] + '\n    ' + ROW[12])),
    ('the older poll removed', mutate('NVMTLDevice.m', 'usleep(NVMTL_BUSY_POLL_US);', 'usleep(1);')),
    ('the older clear removed', mutate('NVMTLEventSync.m', 'nvmtl_ws_mark(sid, NO);', '')),
    ('surface 0 routed', mutate('nvmtl_vk.c', '    if (!surfaceID) return pnvk_surface_dirty(g_dev, surfaceID, mark);\n', '')),
    ('a prototype that differs from the published one', mutate('nvmtl_vk.c', 'uint32_t timeout_ms, uint32_t out[6]);', 'uint32_t timeout, uint32_t out[6]);')),
    ('a fence number repeated in nvmtl_vk.c', mutate('nvmtl_vk.c', '/* ---- the kernel surface fence', '#define NVMTL_FENCE_WAIT_MS 100u\n/* ---- the kernel surface fence')),
    ('nvmtl_fence.h not included', mutate('nvmtl_vk.h', '#include "nvmtl_fence.h"\n', '')),
]
for what, g in controls:
    try:
        check(g)
        print('FAIL negative control passed:', what); fails += 1
    except (AssertionError, ValueError) as e:
        print('ok   control fails (' + what + '):', e)

print('\nall passed' if not fails else f'\n{fails} FAILED')
sys.exit(1 if fails else 0)
