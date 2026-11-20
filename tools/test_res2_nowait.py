#!/usr/bin/env python3
"""test_res2_nowait.py: the VRAM pager's two waits on the allocating thread are taken from plugin/nvmtl_res2_nowait.h.

tools/test_res2_nowait.c proves the decisions; this checks that plugin/NVMTLObjects.m is wired to them:
NVMTL_RES2_NOWAIT is read once into a static through nvmtl_res2_nowait_default (so WindowServer keeps the waits),
nvmtl_res2_pass_guarded returns after a kick before its 30 ms poll whenever nvmtl_res2_busy_action says so, and
nvmtl_res2_evict's non-blocking branch reaps once with no sleep and no loop while the 16 ms sleep and its blind window
stay, unchanged, in the branch NVMTL_RES2_NOWAIT=0 takes.

  python3 tools/test_res2_nowait.py"""
import pathlib
import re
import sys

root = pathlib.Path(__file__).resolve().parents[1]
objects = (root / 'plugin' / 'NVMTLObjects.m').read_text()
fails = []


def arm(what, ok):
    print(('ok    ' if ok else 'FAIL  ') + what)
    if not ok:
        fails.append(what)


def function(text, signature):
    if signature not in text:
        return ''
    start = text.index(signature)
    opening = text.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[start:end]


on = function(objects, 'static int nvmtl_res2_nowait_on(void) {')
guarded = function(objects, 'static void nvmtl_res2_pass_guarded(int from_worker) {')
evict = function(objects, 'static uint64_t nvmtl_res2_evict(uint64_t want) {')

arm('NVMTLObjects.m includes nvmtl_res2_nowait.h', '#include "nvmtl_res2_nowait.h"' in objects)
arm('the switch is read in one place', objects.count('getenv("NVMTL_RES2_NOWAIT")') == 1 and 'getenv("NVMTL_RES2_NOWAIT")' in on)
arm('it is read once into a static', 'static int on = -1;' in on and 'if (on < 0)' in on)
arm('its value comes from the header rule with the process name (WindowServer keeps the waits)',
    re.search(r'on = nvmtl_res2_nowait_default\(e, getprogname\(\)\);', on) is not None)

busy = guarded.find('nvmtl_res2_busy_action(from_worker, nvmtl_res2_nowait_on())')
spin = guarded.find('usleep(200)')
arm('pass_guarded asks the header what a taken lock means', busy >= 0)
arm('the kick-and-return comes before the 30 ms poll',
    busy >= 0 and spin > busy and re.search(r'== NVMTL_RES2_BUSY_KICK\) \{ nvmtl_res2_kick\(1\); return; \}', guarded) is not None)
arm('pass_guarded keeps the 30 ms poll for NVMTL_RES2_NOWAIT=0', '30000000ull' in guarded and spin >= 0)

arm('evict asks the header what to do after the pass',
    re.search(r'nvmtl_res2_drain_action\(nvmtl_res2_nowait_on\(\), after <= before, gGraveVram,', evict) is not None)
m = re.search(r'if \(drain == NVMTL_RES2_DRAIN_REAP\) \{(.*?)\} else if \(drain == NVMTL_RES2_DRAIN_SLEEP\) \{', evict, re.S)
reap = m.group(1) if m else ''
arm('the non-blocking branch exists and is followed by the sleeping one', m is not None)
arm('the non-blocking branch reaps once and recounts', reap.count('nvmtl_res2_reap();') == 1 and 'after = nvmtl_vk_vram_free_now();' in reap)
arm('the non-blocking branch never sleeps or loops', not re.search(r'usleep|sleep\(|while|for \(', reap))
arm('the non-blocking branch kicks the worker when nothing came back', 'if (after <= before) { nvmtl_res2_kick(0);' in reap)
arm('the non-blocking branch neither reads nor sets the blind window', 'blindUntil' not in reap)
sleep = evict[m.end():] if m else ''
arm('the sleeping branch keeps the 16 ms loop and the 250 ms blind window',
    '16000000ull' in sleep and 'usleep(1000)' in sleep and '250000000ull' in sleep)
arm('the old unconditional condition is gone', '>= atomic_load(&blindUntil)) {' not in evict)
arm('evict sleeps nowhere else', evict.count('usleep(') == 1)

# the Private buffers with no fallback of their own retry in system memory, but only while the waits are off
arm('the visibility scratch retries in system memory only while the waits are off',
    'if (nvmtl_vk_buffer_create(_visRecords.count * 8, 0, &scratch->_b) &&\n'
    '            (!nvmtl_res2_nowait_on() || nvmtl_vk_buffer_create(_visRecords.count * 8, 1, &scratch->_b))) {' in objects)
arm('the build userID side table retries in system memory only while the waits are off',
    'if (nvmtl_vk_buffer_create(uidBytes, 0, &dst->_uid) && (!nvmtl_res2_nowait_on() || '
    'nvmtl_vk_buffer_create(uidBytes, 1, &dst->_uid))) {' in objects)
arm('the copied side table retries in system memory only while the waits are off',
    'if (nvmtl_vk_buffer_create(s->_uid.size, 0, &d->_uid) && (!nvmtl_res2_nowait_on() || '
    'nvmtl_vk_buffer_create(s->_uid.size, 1, &d->_uid))) {' in objects)
arm('no other Private buffer of the encoders is refused without a fallback',
    len(re.findall(r'nvmtl_vk_buffer_create\([^;]*, 0, &(?:scratch->_b|dst->_uid|d->_uid)\)\)', objects)) == 0)

print('FAILED %d' % len(fails) if fails else 'ok')
sys.exit(1 if fails else 0)
