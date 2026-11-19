#!/usr/bin/env python3
"""test_schedule_hold.py: a command buffer held until its surface writes are published (_scheduleAfterPublish) reads
the hold under its completion lock, so a hold set while another thread reports "scheduled" is not passed by; it reports
"scheduled" as soon as its held writes are published when a later part waits for an event (which a command buffer
committed from its scheduled handler may be the one to signal), and keeps the hold to the end when a later part holds
too, when WindowServer still waits to publish one of its textures or a later part writes a surface there, when no
later part waits, and with NVMTL_SCHEDULE_EARLY=0.

Compiles -nvmtlMarkScheduled, -nvmtlReleaseScheduleHold and -nvmtlScheduledAfterPublished of plugin/NVMTLEventSync.m
as they are, with the switch they read and WindowServer's deferred publications, against a stand-in command buffer.
The idea is from pull request 524 of the original project (P5); this test is written for this tree, where the early
report waits for a later event wait and for WindowServer's deferred publications.

  python3 tools/test_schedule_hold.py [--baseline [REF]]
      --baseline REF   the sources of that revision (default HEAD; :0 is the staged index); on 3.0.4 the lock arm
                       fails and the methods that release the hold early are missing

Builds the way tools/test_submission_copies.py does (clang -fobjc-arc -framework Foundation): on a Mac, or on Linux
with the GNUstep wrapper first on PATH."""
import argparse
import os
import pathlib
import subprocess
import sys
import tempfile

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


events = source('plugin/NVMTLEventSync.m')
failures = []


def report(ok, what):
    print(('ok    ' if ok else 'FAIL  ') + what)
    if not ok:
        failures.append(what)


marks_key = events[events.index('static const void *const nvmtl_ws_marks_key'):]
marks_key = marks_key[:marks_key.index('\n') + 1]
ws = events[events.index('@interface NVMTLSurfaceHolds'):events.index('static void nvmtl_ws_defer(')]
mark = function(events, '- (void)nvmtlMarkScheduled {')
release = function(events, '- (BOOL)nvmtlReleaseScheduleHold {')
early = function(events, '- (void)nvmtlScheduledAfterPublished {')
switch = function(events, 'static BOOL nvmtl_schedule_early_on(void) {')
report(release is not None, 'the hold is released under the completion lock (-nvmtlReleaseScheduleHold)')
report(early is not None and switch is not None,
       'a held command buffer can report scheduled once its held writes are published (-nvmtlScheduledAfterPublished)')

# The production wiring of the hold around these methods.
seal = events[events.index('- (void)nvmtlSealPartWithSignal'):] if '- (void)nvmtlSealPartWithSignal' in events else ''
report('part->holdsSchedule = holds;' in seal and
       'if (holds) { [_completionCondition lock]; _scheduleAfterPublish = YES; [_completionCondition unlock]; }' in seal,
       'the seal sets the hold under the completion lock and the part remembers that it holds')
finish = function(events, '- (void)nvmtlFinishExecution:') or ''
report('if ([self nvmtlReleaseScheduleHold]) [self nvmtlMarkScheduled];' in finish,
       'the end of the command buffer releases the hold under the lock before it reports scheduled')
advance = function(events, '- (BOOL)nvmtlAdvancePart {') or ''
sync_out = 'for (NVMTLBuffer *buffer in nvmtl_submission_copies(part->resources, YES)) nvmtl_buffer_sync_out(buffer);'
report(sync_out in advance and
       advance.find('if (published) [self nvmtlScheduledAfterPublished];') > advance.find(sync_out),
       'the worker asks for the early report only after the part is published and its copies are back')

fixture = r'''
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <objc/runtime.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#define nvlog(...) ((void)0)
#ifndef __APPLE__   // the Metal stand-in of the GNUstep build declares no command-buffer status
typedef NS_ENUM(NSUInteger, MTLCommandBufferStatus) { MTLCommandBufferStatusNotEnqueued, MTLCommandBufferStatusEnqueued,
    MTLCommandBufferStatusCommitted, MTLCommandBufferStatusScheduled, MTLCommandBufferStatusCompleted, MTLCommandBufferStatusError };
#endif
@interface NVMTLTexture : NSObject @end
@implementation NVMTLTexture @end
@class NVMTLCommandBuffer;
/*WS*/
@interface NVMTLSubmissionPart : NSObject { @public NSArray *surfaces; id waitEvent; BOOL holdsSchedule; } @end
@implementation NVMTLSubmissionPart @end
/*SWITCH*/
static unsigned reports;
@interface NVMTLCommandBuffer : NSObject { @public
    NSCondition *_completionCondition; BOOL _scheduleAfterPublish, _wasScheduled; MTLCommandBufferStatus _cbstatus;
    NSMutableArray *_scheduledHandlers; NSUInteger _nextPart; NSMutableArray *_parts;
}
- (void)nvmtlMarkScheduled;
@end
@implementation NVMTLCommandBuffer
- (instancetype)init {
    if ((self = [super init])) { _completionCondition = [NSCondition new]; _scheduledHandlers = [NSMutableArray new]; _parts = [NSMutableArray new]; }
    return self;
}
- (void)nvmtlDispatchHandlers:(NSArray *)handlers scheduled:(BOOL)scheduled { (void)handlers; if (scheduled) reports++; }
/*METHODS*/
@end
'''

checks = r'''
static int failed;
static void arm(int ok, const char *what) { printf("%s  %s\n", ok ? "ok  " : "FAIL", what); if (!ok) failed = 1; }
static NVMTLSubmissionPart *part(BOOL holds, BOOL waits, NSArray *surfaces) {
    NVMTLSubmissionPart *p = [NVMTLSubmissionPart new]; p->holdsSchedule = holds; p->waitEvent = waits ? [NSObject new] : nil;
    p->surfaces = surfaces ?: @[]; return p;
}
// a command buffer whose part 0 (holding) has just been published, with these later parts, held as the seal holds it
static NVMTLCommandBuffer *held(NSArray *later) {
    NVMTLCommandBuffer *cb = [NVMTLCommandBuffer new];
    [cb->_parts addObject:part(YES, NO, @[[NVMTLTexture new]])]; [cb->_parts addObjectsFromArray:later];
    cb->_scheduleAfterPublish = YES; return cb;
}
static void *marker(void *cb) { [(__bridge NVMTLCommandBuffer *)cb nvmtlMarkScheduled]; return NULL; }
int main(void) { @autoreleasepool {
    // 1. A hold set while another thread is reporting scheduled: that thread waits for the lock, and finds the hold.
    {
        NVMTLCommandBuffer *cb = [NVMTLCommandBuffer new]; reports = 0;
        [cb->_completionCondition lock];
        pthread_t t; pthread_create(&t, NULL, marker, (__bridge void *)cb);
        usleep(200 * 1000);   // the other thread is in -nvmtlMarkScheduled, waiting for the lock
        cb->_scheduleAfterPublish = YES;   // what the seal does, under the lock
        [cb->_completionCondition unlock];
        pthread_join(t, NULL);
        arm(!cb->_wasScheduled && !reports, "a hold set under the lock while another thread reports scheduled is not passed by");
        reports = 0; NVMTLCommandBuffer *free_ = [NVMTLCommandBuffer new];
        [free_ nvmtlMarkScheduled]; [free_ nvmtlMarkScheduled];
        arm(free_->_wasScheduled && reports == 1 && free_->_cbstatus == MTLCommandBufferStatusScheduled,
            "control: without a hold, scheduled is reported once");
    }
#ifdef HAVE_EARLY
    const BOOL on = !(getenv("NVMTL_SCHEDULE_EARLY") && getenv("NVMTL_SCHEDULE_EARLY")[0] == '0');
    // 2. The release.
    {
        NVMTLCommandBuffer *cb = held(@[]); reports = 0;
        [cb nvmtlMarkScheduled];
        const BOOL kept = !reports;
        const BOOL first = [cb nvmtlReleaseScheduleHold], second = [cb nvmtlReleaseScheduleHold];
        [cb nvmtlMarkScheduled];
        arm(kept && first && !second && !cb->_scheduleAfterPublish && reports == 1,
            "a held command buffer reports nothing until the hold is released, once, and then reports");
    }
    // 3. A later part waits for an event: reported as soon as the held writes are published (with the switch on).
    {
        NVMTLCommandBuffer *cb = held(@[part(NO, YES, nil), part(NO, NO, @[[NVMTLTexture new]])]); reports = 0;
        [cb nvmtlScheduledAfterPublished];
        if (on) arm(cb->_wasScheduled && reports == 1 && !cb->_scheduleAfterPublish,
                    "a later event wait: scheduled is reported once the held writes are published (no deadlock on an event "
                    "signalled from the scheduled handler)");
        else arm(!cb->_wasScheduled && !reports && cb->_scheduleAfterPublish,
                 "NVMTL_SCHEDULE_EARLY=0: held to the end, as in 3.0.4");
        [cb nvmtlScheduledAfterPublished];
        arm(reports == (on ? 1u : 0u), "asking again reports nothing more");
    }
    // 4. Kept to the end in every other case.
    {
        NVMTLCommandBuffer *cb = held(@[part(NO, YES, nil), part(YES, NO, @[[NVMTLTexture new]])]); reports = 0;
        [cb nvmtlScheduledAfterPublished];
        arm(!reports && cb->_scheduleAfterPublish, "a later part that holds too keeps the hold");
        cb = held(@[part(NO, NO, @[[NVMTLTexture new]]), part(NO, NO, nil)]); reports = 0;
        [cb nvmtlScheduledAfterPublished];
        arm(!reports && cb->_scheduleAfterPublish, "no later event wait: the report stays at the end, as before");
        cb = held(@[]); reports = 0;
        [cb nvmtlScheduledAfterPublished];
        arm(!reports && cb->_scheduleAfterPublish, "the last part: the report stays with the end of the command buffer");
        cb = held(@[part(NO, YES, nil)]); cb->_scheduleAfterPublish = NO; cb->_wasScheduled = YES; reports = 0;
        [cb nvmtlScheduledAfterPublished];
        arm(!reports, "a command buffer that holds nothing is not reported again");
    }
    // 5. WindowServer, where a texture is published when the last holder of its surface lets go.
    {
        NVMTLSurfaceHolds *holds = [NVMTLSurfaceHolds new]; holds->early = YES;
        NVMTLCommandBuffer *cb = held(@[part(NO, YES, nil), part(NO, NO, @[[NVMTLTexture new]])]); reports = 0;
        objc_setAssociatedObject(cb, nvmtl_ws_marks_key, holds, OBJC_ASSOCIATION_RETAIN_NONATOMIC);
        [cb nvmtlScheduledAfterPublished];
        arm(!reports && cb->_scheduleAfterPublish, "WindowServer: a later part that writes any surface keeps the hold");
        cb = held(@[part(NO, YES, nil)]); reports = 0;
        objc_setAssociatedObject(cb, nvmtl_ws_marks_key, holds, OBJC_ASSOCIATION_RETAIN_NONATOMIC);
        NVMTLDeferredPublication *mine = [NVMTLDeferredPublication new]; mine->texture = [NVMTLTexture new]; mine->owner = cb;
        @synchronized (nvmtl_ws_lock()) {
            nvmtl_ws_deferred = [NSMutableDictionary new]; nvmtl_ws_deferred[@7] = [NSMutableArray arrayWithObject:mine];
        }
        [cb nvmtlScheduledAfterPublished];
        arm(!reports && cb->_scheduleAfterPublish,
            "WindowServer: a texture of this command buffer still waiting for another holder keeps the hold");
        NVMTLCommandBuffer *other = [NVMTLCommandBuffer new];
        @synchronized (nvmtl_ws_lock()) { mine->owner = other; }
        [cb nvmtlScheduledAfterPublished];
        if (on) arm(reports == 1 && !cb->_scheduleAfterPublish,
                    "WindowServer: another command buffer's waiting texture does not, and a later event wait is reported early");
        else arm(!reports, "NVMTL_SCHEDULE_EARLY=0: WindowServer too is held to the end");
        @synchronized (nvmtl_ws_lock()) { nvmtl_ws_deferred = nil; }
    }
#endif
    return failed;
} }
'''

methods = mark + '\n' + (release or '') + '\n' + (early or '')
program = (fixture.replace('/*WS*/', marks_key + ws).replace('/*SWITCH*/', switch or '')
           .replace('/*METHODS*/', methods) + checks)
defines = ['-DHAVE_EARLY'] if release and early and switch else []
with tempfile.TemporaryDirectory() as directory:
    path = pathlib.Path(directory)
    (path / 'hold.m').write_text(program)
    subprocess.run(['clang', '-fobjc-arc', *defines, '-framework', 'Foundation', '-framework', 'Metal',
                    str(path / 'hold.m'), '-o', str(path / 'hold')], check=True)
    for setting in (None, '0'):
        env = dict(os.environ)
        env.pop('NVMTL_SCHEDULE_EARLY', None)
        if setting is not None:
            env['NVMTL_SCHEDULE_EARLY'] = setting
            if not defines:
                continue
            print('-- NVMTL_SCHEDULE_EARLY=0')
        if subprocess.run([str(path / 'hold')], env=env).returncode:
            failures.append('the program')
if failures:
    sys.exit(1)
print('all passed')
