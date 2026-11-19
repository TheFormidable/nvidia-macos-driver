#!/usr/bin/env python3
"""test_host_reads_ahead.py: a part whose caller copies (newBufferWithBytesNoCopy buffers the driver copies) are all
buffers it only reads, and that no sealed part may have written, is submitted ahead; a written one, one reached through
a texture, a texture alias, or one a part sealed later writes (asked again at the next try) holds it for the worker, as
does every caller copy with NVMTL_GSUB_HOST_READS=0; and "scheduled" is reported once the queue has accepted the part
and its kernel fence is armed, in the submit-ahead pass and in the worker (before the submission with
NVMTL_SCHEDULED_AFTER_SUBMIT=0).

Compiles the production NVMTLResSet (plugin/NVMTLObjects.m), the seal's marking of written caller copies
(nvmtl_note_host_writes), the copy selection, -nvmtlSubmitAhead and -nvmtlAdvancePart of plugin/NVMTLEventSync.m, as
they are, on the stand-ins of tools/test_submission_copies.py (its fixture strings are read from that file), with the
submissions, kernel fence arms and scheduled reports logged in order. The idea is from pull request 524 of the original
project; this test is written for this tree, where the read-only case is asked again at every try.

  python3 tools/test_host_reads_ahead.py [--baseline [REF]]
      --baseline REF   the production sources of that revision (default HEAD; :0 is the staged index); on 3.0.4 the
                       read-only part is held and "scheduled" comes before the submission

Builds the way tools/test_submission_copies.py does: on a Mac, or on Linux with the GNUstep wrapper first on PATH."""
import argparse
import ast
import os
import pathlib
import re
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


def replace_once(text, old, new):
    assert text.count(old) == 1, f'the fixture of tools/test_submission_copies.py no longer has: {old[:70]!r}'
    return text.replace(old, new)


events = source('plugin/NVMTLEventSync.m')
objects = source('plugin/NVMTLObjects.m')
header = source('plugin/NVMTLObjects.h')

# the stand-ins, as tools/test_submission_copies.py (of this checkout) declares them
strings = {node.targets[0].id: node.value.value for node in ast.parse((root / 'tools/test_submission_copies.py').read_text()).body
           if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
           and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)}
fixture, submission_fixture = strings['fixture'], strings['submission_fixture']

# the production sources, cut where tools/test_submission_copies.py cuts them
start = events.index('static NSArray<NVMTLBuffer *> *nvmtl_submission_copies(')
prepare_start = events.index('static void nvmtl_prepare_submission_copies(', start)
helper = events[start:prepare_start]
prepare = events[prepare_start:events.index('- (BOOL)nvmtlSubmitAhead', prepare_start)]
ahead = events[events.index('- (BOOL)nvmtlSubmitAhead'):events.index('static void nvmtl_gsub_ahead_pass1(void);')]
advance = events[events.index('- (BOOL)nvmtlAdvancePart'):events.index('- (void)nvmtlSubmitWithEvents')]
ws = events[events.index('static const void *const nvmtl_ws_marks_key'):events.index('static void nvmtl_surface_finish_writes(')]
sync = objects[objects.index('static os_unfair_lock gSwcLock = OS_UNFAIR_LOCK_INIT;'):]
res_start = objects.index('static NSMutableSet *nvmtl_identity_set(')
res_set = objects[res_start:objects.index('@end', objects.index('@implementation NVMTLResSet')) + 4]
res_interface = re.search(r'@interface NVMTLResSet : NSMutableSet .*?@end', header, re.S).group()

switches = [function(events, f'static BOOL {name}(void) {{') for name in ('nvmtl_gsub_host_reads_on', 'nvmtl_scheduled_after_submit_on')]
note = function(events, 'static void nvmtl_note_host_writes(NVMTLResSet *members) {')
failures = []


def report(ok, what):
    print(('ok    ' if ok else 'FAIL  ') + what)
    if not ok:
        failures.append(what)


report(note is not None and all(switches), 'the seal marks the caller copies a part may write (nvmtl_note_host_writes)')
seal = events[events.index('- (void)nvmtlSealPartWithSignal'):]
take = 'part->resources = [(NVMTLResSet *)_resources nvmtlTakeAll]; part->surfaces = [_surfDirty copy];'
report(take in seal and seal[seal.index(take) + len(take):].lstrip().startswith('nvmtl_note_host_writes((NVMTLResSet *)part->resources);'),
       'the marking is made as the part takes its resources, before the commit that lets anything submit it')

# The production NVMTLResSet in place of the fixture's plain set.
fixture = replace_once(fixture, '''@interface NVMTLResSet : NSSet
- (BOOL)isWrittenObject:(id)object;
@end
@implementation NVMTLResSet
- (BOOL)isWrittenObject:(id)object { return YES; }
@end''', '#include <stdatomic.h>\nstatic _Atomic unsigned long gRes2Tick;\nstatic inline void nvmtl_resref(id o, int d) { (void)o; (void)d; }\n'
         + res_interface + '\n' + res_set)
# The production switches and the seal's marking (3.0.4 has neither: the marking is then nothing).
stubs = '''static BOOL nvmtl_gsub_host_reads_on(void) { return YES; }
static BOOL nvmtl_scheduled_after_submit_on(void) { return YES; }'''
if note and all(switches):
    submission_fixture = replace_once(submission_fixture, stubs, '\n'.join(switches) + '\n' + note)
else:
    submission_fixture = replace_once(submission_fixture, stubs, 'static void nvmtl_note_host_writes(NVMTLResSet *members) { (void)members; }')
# Every submission (B), wait (W), finish of a begun submission (F), fence arm (A<part>) and scheduled report (S), in order.
submission_fixture = replace_once(submission_fixture, '- (void)nvmtlMarkScheduled { scheduled++; }',
                                  '- (void)nvmtlMarkScheduled { scheduled++; if (events) [events addObject:@"S"]; }')
submission_fixture = replace_once(submission_fixture, '    if (!h->cookie || !f) return;\n',
                                  '    if (f && events) [events addObject:[NSString stringWithFormat:@"A%u", part]];\n'
                                  '    if (!h->cookie || !f) return;\n')
submission_fixture = replace_once(submission_fixture, '{ begins++; fakeGPU(); return submitResult; }',
                                  '{ begins++; if (events) [events addObject:@"B"]; fakeGPU(); return submitResult; }')
submission_fixture = replace_once(submission_fixture, '{ waits++; fakeGPU(); return submitResult; }',
                                  '{ waits++; if (events) [events addObject:@"W"]; fakeGPU(); return submitResult; }')
submission_fixture = replace_once(submission_fixture, '{ (void)f; waits++; if (!submitResult)',
                                  '{ (void)f; waits++; if (events) [events addObject:@"F"]; if (!submitResult)')

checks = r'''
@end
static int failed;
static void arm(int ok, const char *what) { printf("%s  %s\n", ok ? "ok  " : "FAIL", what); if (!ok) failed = 1; }
static NSUInteger at(NSString *e) { return [events indexOfObject:e]; }
static BOOL before(NSString *a, NSString *b) { return at(a) != NSNotFound && at(b) != NSNotFound && at(a) < at(b); }
static unsigned char cpu[16], gpu[16];
static NVMTLBuffer *caller(void) {
    NVMTLBuffer *b = [NVMTLBuffer new]; b->_hostPtr = cpu; b->_hostLen = 16; b->_b = (Buffer){gpu, 16}; return b;
}
// a part sealed with these resources: taken from the encoder's set, its writes marked, as nvmtlSealPartWithSignal does
static NSSet *sealed(NVMTLResSet *encoder) { NVMTLResSet *taken = [encoder nvmtlTakeAll]; nvmtl_note_host_writes(taken); return taken; }
static NVMTLCommandBuffer *one(NSSet *resources) {
    NVMTLSubmissionPart *part = [NVMTLSubmissionPart new];
    part->commands.cb = (void *)1; part->resources = resources; part->encoders = 1;
    NVMTLCommandBuffer *cb = [NVMTLCommandBuffer new]; cb->_committed = YES;
    cb->_parts = [NSMutableArray arrayWithObject:part]; return cb;
}
static NVMTLSubmissionPart *first(NVMTLCommandBuffer *cb) { return cb->_parts[0]; }
static BOOL finished(NVMTLCommandBuffer *cb) { return [cb nvmtlAdvancePart] && cb->_executionDone && !cb->_cbError; }
int main(void) { @autoreleasepool {
    testGPU = gpu;
    const char *r = getenv("NVMTL_GSUB_HOST_READS"), *s = getenv("NVMTL_SCHEDULED_AFTER_SUBMIT");
    const BOOL readsOn = !(r && r[0] == '0'), afterOn = !(s && s[0] == '0');
    memset(cpu, 21, 16); expectedPixel = 21; writtenPixel = 21;
    // 1. A caller copy the part only reads (a blit copy's source), which no part has written.
    {
        NVMTLBuffer *b = caller(); NVMTLResSet *encoder = [NVMTLResSet new]; [encoder addReadOnlyObject:b];
        NVMTLCommandBuffer *cb = one(sealed(encoder));
        const unsigned u = uploads, d = downloads;
        const BOOL went = [cb nvmtlSubmitAhead] && cb->_nextSubmit == 1 && first(cb)->inflight;
        if (readsOn) arm(went && uploads == u + 1, "a caller copy that is only read and never written is submitted ahead, its bytes copied in");
        else arm(!went && !cb->_nextSubmit, "NVMTL_GSUB_HOST_READS=0: every caller copy holds the part for the worker, as in 3.0.4");
        arm(finished(cb) && downloads == d && uploads == u + 1, "... it finishes in the worker, copied in once and never back");
        arm(!b->_gpuWritten, "... and reading it marks nothing");
    }
    // 2. Every other caller copy holds the part for the worker.
    {
        NVMTLBuffer *b = caller(); NVMTLResSet *encoder = [NVMTLResSet new]; [encoder addObject:b];
        NVMTLCommandBuffer *cb = one(sealed(encoder));
        const unsigned d = downloads;
        arm(![cb nvmtlSubmitAhead] && !cb->_nextSubmit, "a caller copy the part may write holds it");
        arm(finished(cb) && downloads == d + 1, "... and the worker copies it back");
        b = caller(); b->_texSeen = YES; encoder = [NVMTLResSet new]; [encoder addReadOnlyObject:b];
        cb = one(sealed(encoder));
        arm(![cb nvmtlSubmitAhead] && !cb->_nextSubmit && finished(cb), "a read-only caller copy with a texture alias holds it");
        b = caller(); NVMTLTexture *t = [NVMTLTexture new]; t->_backBuf = b; t->_usage = MTLTextureUsageShaderRead;
        encoder = [NVMTLResSet new]; [encoder addReadOnlyObject:t];
        cb = one(sealed(encoder));
        arm(![cb nvmtlSubmitAhead] && !cb->_nextSubmit && finished(cb), "a caller copy reached only through a texture holds it");
        b = caller(); cb = one([NSSet setWithObject:b]);
        arm(![cb nvmtlSubmitAhead] && !cb->_nextSubmit && finished(cb), "a part whose resources are not a tracked set holds it");
    }
    // 3. A part sealed after the reader was first tried writes the buffer: the reader is asked again, and holds.
    {
        NVMTLBuffer *b = caller(); NVMTLResSet *encoder = [NVMTLResSet new]; [encoder addReadOnlyObject:b];
        NVMTLCommandBuffer *reader = one(sealed(encoder));
        allocationFails = YES;
        const BOOL triedHeld = ![reader nvmtlSubmitAhead] && !reader->_nextSubmit;   // prepared, then no inflight record
        allocationFails = NO;
        NVMTLResSet *writing = [NVMTLResSet new]; [writing addObject:b];
        NVMTLCommandBuffer *writer = one(sealed(writing));
        if (readsOn) arm(triedHeld && b->_gpuWritten, "the writer's seal marks the caller copy");
        else arm(triedHeld && !b->_gpuWritten, "NVMTL_GSUB_HOST_READS=0: nothing is marked (every caller copy holds)");
        arm(![reader nvmtlSubmitAhead] && !reader->_nextSubmit, "a reader prepared before that seal is asked again and holds");
        arm(finished(writer) && finished(reader), "... both finish in the worker");
        encoder = [NVMTLResSet new]; [encoder addReadOnlyObject:b];
        NVMTLCommandBuffer *later = one(sealed(encoder));
        arm(![later nvmtlSubmitAhead] && finished(later), "once written, a caller copy holds every later reader");
    }
    // 4. "scheduled" in the submit-ahead pass: after the queue accepted the part and its fence was armed.
    {
        events = [NSMutableArray new];
        NVMTLCommandBuffer *cb = one([NVMTLResSet new]);
        const BOOL went = [cb nvmtlSubmitAhead] && first(cb)->inflight;
        if (afterOn) arm(went && before(@"B", @"A0") && before(@"A0", @"S"),
                         "submit-ahead: scheduled once the queue has accepted the part and its kernel fence is armed");
        else arm(went && before(@"S", @"B"), "NVMTL_SCHEDULED_AFTER_SUBMIT=0: scheduled before the submission, as in 3.0.4");
        arm(finished(cb), "... and the worker finishes it");
        // a submission the queue refuses in the ahead pass is reported by the worker, which fails the command buffer
        events = [NSMutableArray new]; submitResult = -1;
        cb = one([NVMTLResSet new]);
        [cb nvmtlSubmitAhead];
        const BOOL notYet = at(@"S") == NSNotFound;
        [cb nvmtlAdvancePart];
        submitResult = 0;
        if (afterOn) arm(notYet && at(@"S") != NSNotFound && cb->_cbError,
                         "a part the queue refused ahead is reported scheduled by the worker, which fails the command buffer");
        else arm(!notYet && cb->_cbError, "NVMTL_SCHEDULED_AFTER_SUBMIT=0: a refused part was already reported, as in 3.0.4");
    }
    // 5. "scheduled" in the worker.
    {
        events = [NSMutableArray new];
        NVMTLCommandBuffer *cb = one([NVMTLResSet new]);
        const BOOL done = finished(cb);
        if (afterOn) arm(done && before(@"B", @"A0") && before(@"A0", @"S") && before(@"S", @"F"),
                         "worker: scheduled after the submission and the fence arm, before the wait for it");
        else arm(done && before(@"S", @"B"), "NVMTL_SCHEDULED_AFTER_SUBMIT=0: the worker reports before the submission");
        events = [NSMutableArray new]; allocationFails = YES;
        cb = one([NVMTLResSet new]);
        const BOOL waited = finished(cb);
        allocationFails = NO;
        if (afterOn) arm(waited && before(@"W", @"S"), "worker without an inflight record: scheduled after the submission");
        else arm(waited && before(@"S", @"W"), "NVMTL_SCHEDULED_AFTER_SUBMIT=0: before it");
        events = [NSMutableArray new]; submitResult = -1;
        cb = one([NVMTLResSet new]);
        [cb nvmtlAdvancePart];
        submitResult = 0;
        if (afterOn) arm(cb->_cbError && before(@"B", @"S"), "worker: a refused submission is still reported, then fails");
        else arm(cb->_cbError && before(@"S", @"B"), "NVMTL_SCHEDULED_AFTER_SUBMIT=0: reported before it, as in 3.0.4");
    }
    events = nil;
    return failed;
} }
'''

with tempfile.TemporaryDirectory() as directory:
    path = pathlib.Path(directory)
    (path / 'ahead.m').write_text(fixture + helper + sync + submission_fixture.replace('/*WS*/', ws) + prepare + ahead + advance + checks)
    subprocess.run(['clang', '-fobjc-arc', '-I', str(root / 'plugin'), '-framework', 'Foundation', '-framework', 'IOSurface',
                    '-framework', 'Metal', str(path / 'ahead.m'), '-o', str(path / 'ahead')], check=True)
    for setting in ({}, {'NVMTL_GSUB_HOST_READS': '0'}, {'NVMTL_SCHEDULED_AFTER_SUBMIT': '0'}):
        env = {k: v for k, v in os.environ.items() if k not in ('NVMTL_GSUB_HOST_READS', 'NVMTL_SCHEDULED_AFTER_SUBMIT', 'NVMTL_GSUB_HOSTPTR_LIFT')}
        env.update(setting)
        if setting:
            print('-- ' + ' '.join(f'{k}={v}' for k, v in setting.items()))
        if subprocess.run([str(path / 'ahead')], env=env).returncode:
            failures.append('the program')
if failures:
    sys.exit(1)
print('all passed')
