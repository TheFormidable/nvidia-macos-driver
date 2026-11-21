#!/usr/bin/env python3
"""test_cb_lean.py: a command buffer makes its handler groups on first use, its queue's capacity semaphore is made with
the queue, and -commandBuffer no longer makes a second resource set.

Text checks of plugin/NVMTLObjects.m, plugin/NVMTLEventSync.m and plugin/NVMTLDevice.m that pin the wiring
tools/test_cb_lean.c cannot see: -init makes the groups only with NVMTL_CB_LEAN=0; nvmtlDispatchHandlers makes a group
before its first enter and is only called with _completionCondition held; the waiters read the group under that lock
and skip only a nil one; the switch is read once into a static; the semaphore is made in the one place queues are made,
before the queue is returned, with the count -commandBuffer used; -commandBuffer reads it without the class lock only
with the saving on and keeps the locked path; -init still makes the resource set and -commandBuffer no longer replaces it.

  python3 tools/test_cb_lean.py [--baseline [REF]]
      --baseline REF   the sources of that revision (default HEAD); on 3.0.5 the changes are missing and the test fails"""
import argparse
import pathlib
import re
import subprocess
import sys

root = pathlib.Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument('--baseline', nargs='?', const='HEAD', metavar='REF')
args = parser.parse_args()


def source(path):
    if args.baseline:
        return subprocess.check_output(['git', 'show', f'{args.baseline}:{path}'], cwd=root, text=True)
    return (root / path).read_text()


def function(text, signature, after=0):
    start = text.find(signature, after)
    if start < 0:
        return ''
    opening = text.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[start:end]


objects = source('plugin/NVMTLObjects.m')
events = source('plugin/NVMTLEventSync.m')
device = source('plugin/NVMTLDevice.m')
fails = 0


def arm(what, ok):
    global fails
    print(('ok    ' if ok else 'FAIL  ') + what)
    if not ok:
        fails += 1


cbimpl = objects.find('@implementation NVMTLCommandBuffer\n')
init = function(objects, '- (instancetype)init {', cbimpl)
knob = function(events, 'static BOOL nvmtl_cb_lean_on(void) {')
handlers = function(events, '- (void)nvmtlDispatchHandlers:(NSArray *)handlers scheduled:(BOOL)scheduled {')
completed = function(objects, '- (void)waitUntilCompleted {')
scheduled = function(objects, '- (void)waitUntilScheduled {')
make = function(objects, 'static void nvmtl_queue_make_capacity(NVMTLCommandQueue *q) {')
newq = function(device, '- (id<MTLCommandQueue>)newCommandQueueWithMaxCommandBufferCount:(NSUInteger)n {')
qimpl = objects.find('@implementation NVMTLCommandQueue\n')
cbuf = function(objects, '- (id<MTLCommandBuffer>)commandBuffer {', qimpl)

arm('NVMTL_CB_LEAN is read once into a static, with the shared on-unless-0 rule',
    'static int on = -1;' in knob and 'if (on < 0)' in knob and 'getenv("NVMTL_CB_LEAN")' in knob
    and 'nvmtl_cb_lean_env_on(e)' in knob)
arm('the switch and the header come before the command buffer that uses them',
    0 <= events.find('#include "nvmtl_cb_lean.h"') and objects.find('#import "NVMTLEventSync.m"') < cbimpl)

arm('-init makes both groups only when the pure rule says so (NVMTL_CB_LEAN=0)',
    re.search(r'if \(nvmtl_cb_groups_at_init\(nvmtl_cb_lean_on\(\)\)\) \{ _callbackGroup = dispatch_group_create\(\); '
              r'_scheduledGroup = dispatch_group_create\(\); \}', init) is not None
    and init.count('dispatch_group_create') == 2)
arm('-init still makes the resource set', '_resources = [NVMTLResSet new];' in init)
arm('-commandBuffer no longer replaces -init\'s resource set',
    cbuf and re.search(r'^\s*cb->_resources = ', cbuf, re.M) is None)

i_early = handlers.find('if (!handlers.count) return;')
i_mk_cb = handlers.find('if (nvmtl_cb_group_make_now(_callbackGroup != nil, handlers.count)) _callbackGroup = dispatch_group_create();')
i_mk_sc = handlers.find('if (scheduled && nvmtl_cb_group_make_now(_scheduledGroup != nil, handlers.count)) _scheduledGroup = dispatch_group_create();')
i_enter = handlers.find('dispatch_group_enter(_callbackGroup);')
arm('nvmtlDispatchHandlers makes each group after its no-handler return and before the first enter',
    0 <= i_early < i_mk_cb < i_enter and i_mk_cb < i_mk_sc < i_enter)
arm('the groups are created nowhere else', objects.count('_callbackGroup = dispatch_group_create()') == 1
    and events.count('_callbackGroup = dispatch_group_create()') == 1
    and objects.count('_scheduledGroup = dispatch_group_create()') == 1
    and events.count('_scheduledGroup = dispatch_group_create()') == 1)
calls = [m.start() for m in re.finditer(r'\[self nvmtlDispatchHandlers:', events + objects)]
both = events + objects


def locked_at(text, i):
    before = text[:i]
    return before.rfind('[_completionCondition lock]') > before.rfind('[_completionCondition unlock]')


arm('every call of nvmtlDispatchHandlers holds _completionCondition (where the waiters read the groups)',
    len(calls) == 2 and all(locked_at(both, i) for i in calls))

for name, body, ivar, loop in (('waitUntilCompleted', completed, '_callbackGroup', 'while (!_completionReady) [_completionCondition wait];'),
                               ('waitUntilScheduled', scheduled, '_scheduledGroup', 'while (!_wasScheduled && !_executionDone) [_completionCondition wait];')):
    i_loop = body.find(loop)
    i_read = body.find(f'const BOOL handlersGroup = {ivar} != nil;')
    i_unlock = body.find('[_completionCondition unlock];')
    i_wait = body.find(f'if (nvmtl_cb_group_wait_due(handlersGroup)) dispatch_group_wait({ivar}, DISPATCH_TIME_FOREVER);')
    arm(f'{name} reads its group under the lock after its wait and skips only a nil one',
        0 <= i_loop < i_read < i_unlock < i_wait and body.count('dispatch_group_wait') == 1)

arm('the semaphore is made with the queue through the pure rule, with the count -commandBuffer used',
    'nvmtl_cb_capacity_with_queue(nvmtl_cb_lean_on(), nvmtl_async_commit_on())' in make
    and 'q->_capacity = dispatch_semaphore_create(q->_capacityCount ? q->_capacityCount : 64);' in make
    and 'dispatch_semaphore_create(_capacityCount ? _capacityCount : 64)' in cbuf)
i_count = newq.find('q->_capacityCount = n ? n : 64;')
i_make = newq.find('nvmtl_queue_make_capacity(q);')
i_ret = newq.find('return q;')
arm('newCommandQueue makes it after the count is set and before the queue is returned',
    0 <= i_count < i_make < i_ret)
queues_made = len(re.findall(r'\[NVMTLCommandQueue (new|alloc)\]', objects + events + device))
arm('that is the only place a command queue is made (nothing else could hand out a queue without it)', queues_made == 1)
arm('nothing else writes the semaphore', (objects + events + device).count('_capacity = dispatch_semaphore_create') == 2)

i_fast = cbuf.find('if (nvmtl_cb_capacity_read_unlocked(nvmtl_cb_lean_on())) cap = _capacity;')
i_slow = cbuf.find('if (!cap) @synchronized ([NVMTLCommandQueue class]) { if (!_capacity) _capacity = '
                   'dispatch_semaphore_create(_capacityCount ? _capacityCount : 64); cap = _capacity; }')
i_sem = cbuf.find('dispatch_semaphore_wait(cap, DISPATCH_TIME_FOREVER);')
arm('-commandBuffer reads it unlocked only with the saving on, else takes the class lock as before, then waits on it',
    0 <= i_fast < i_slow < i_sem)

print('FAILED %d' % fails if fails else 'ok')
sys.exit(1 if fails else 0)
