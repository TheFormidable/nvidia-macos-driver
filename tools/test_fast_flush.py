#!/usr/bin/env python3
"""test_fast_flush.py: -commitAndWaitUntilSubmitted submits its own command buffer ahead on the committing thread
outside WindowServer, with the decisions nvmtl.ahead's pass makes, and still waits as before.

Text checks of plugin/NVMTLObjects.m and plugin/NVMTLEventSync.m that pin the wiring tools/test_fast_flush.c cannot
see: the step runs after the commit and before the unchanged wait; WindowServer and NVMTL_GL_FAST_FLUSH=0 skip it;
the switch is read once into a static; the step runs under the command-buffer class lock, only while the worker is
pumping, through nvmtlSubmitAhead, over the command buffer's own queue in reservation order, stopping at that command
buffer; and plain -commit, -waitUntilScheduled and the asynchronous request made at commit are untouched.

  python3 tools/test_fast_flush.py [--baseline [REF]]
      --baseline REF   the sources of that revision (default HEAD); on 3.0.5 the step is missing and the test fails"""
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


def function(text, signature):
    if signature not in text:
        return None
    start = text.index(signature)
    opening = text.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[start:end]


objects = source('plugin/NVMTLObjects.m')
events = source('plugin/NVMTLEventSync.m')
fails = 0


def arm(what, ok):
    global fails
    print(('ok    ' if ok else 'FAIL  ') + what)
    if not ok:
        fails += 1


flush = function(objects, '- (BOOL)commitAndWaitUntilSubmitted {') or ''
step = function(events, 'static void nvmtl_fast_flush_submit(NVMTLCommandBuffer *cb, BOOL windowServer) {') or ''
knob = function(events, 'static BOOL nvmtl_gl_fast_flush_on(void) {') or ''

i_commit = flush.find('[self commit];')
i_step = flush.find('nvmtl_fast_flush_submit(self, ws);')
i_wait = flush.find('if (ws && !nvmtl_ws_kernel_waits()) [self waitUntilCompleted]; else [self waitUntilScheduled];')
arm('the flush commits, then runs the step, then waits exactly as before', 0 <= i_commit < i_step < i_wait)
arm('the flush still returns whether the status is not an error',
    'return self.status != MTLCommandBufferStatusError;' in flush)
arm('the step is given whether this process is WindowServer', 'strcmp(getprogname(), "WindowServer")' in flush)

arm('the step does nothing in WindowServer or with the switch off',
    step.lstrip().find('if (windowServer || !nvmtl_gl_fast_flush_on()) return;') >= 0
    and step.find('if (windowServer || !nvmtl_gl_fast_flush_on()) return;') < step.find('@synchronized'))
arm('NVMTL_GL_FAST_FLUSH is read once into a static, with the shared on-unless-0 rule',
    'static int on = -1;' in knob and 'getenv("NVMTL_GL_FAST_FLUSH")' in knob and 'nvmtl_fast_flush_env_on(e)' in knob
    and 'if (on < 0)' in knob)
arm('the step runs under the command-buffer class lock', '@synchronized ([NVMTLCommandBuffer class])' in step)
arm('the step asks the pure rule with the submit-ahead switches, the worker state and the command buffer state',
    re.search(r'nvmtl_fast_flush_inline\(YES, windowServer, nvmtl_gsub_ahead_on\(\), nvmtl_commit_ahead_on\(\), nvmtl_pumping,\s*'
              r'cb->_committed, cb->_executionDone\)', step) is not None)
arm('the step walks its own queue\'s reservations in order and submits through nvmtlSubmitAhead',
    'for (NVMTLQueueReservation *r in [cb->_queue->_reservations copy])' in step
    and 'nvmtl_fast_flush_walk_stops([b nvmtlSubmitAhead], b == cb)' in step and 'break;' in step)
arm('the step never finishes, advances or kicks anything itself',
    not any(s in step for s in ('nvmtlAdvancePart', 'nvmtlFinishExecution', 'nvmtl_ahead_kick', 'nvmtl_pump_buffers',
                                 'nvmtlMarkScheduled', 'waitUntil')))
arm('the step is defined after the switches it reads',
    events.find('static BOOL nvmtl_commit_ahead_on(void)') < events.find('static void nvmtl_fast_flush_submit(')
    and events.find('static BOOL nvmtl_gsub_ahead_on(void)') < events.find('static void nvmtl_fast_flush_submit('))

commit = function(objects, '- (void)commit {') or ''
arm('a plain commit does not run the step', commit and 'nvmtl_fast_flush_submit' not in commit)
sched = function(objects, '- (void)waitUntilScheduled {') or ''
arm('waitUntilScheduled still sleeps until scheduled or finished and for the scheduled handlers',
    'while (!_wasScheduled && !_executionDone) [_completionCondition wait];' in sched
    and 'dispatch_group_wait(_scheduledGroup, DISPATCH_TIME_FOREVER);' in sched)
pump = function(events, 'static void nvmtl_pump_buffers(void) {') or ''
arm('the commit still asks nvmtl.ahead for a pass while the worker pumps',
    'if (nvmtl_commit_ahead_on() && nvmtl_gsub_ahead_on()) nvmtl_ahead_kick();' in pump)
arm('the call sites are this one flush only', objects.count('nvmtl_fast_flush_submit(') == 1
    and events.count('nvmtl_fast_flush_submit(') == 1)

print('FAILED %d' % fails if fails else 'ok')
sys.exit(1 if fails else 0)
