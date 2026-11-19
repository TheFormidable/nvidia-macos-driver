#!/usr/bin/env python3
"""test_nvencvt_dec_hold.py: libnvencvt's decoders keep the pictures they emitted for a while in Firefox's processes,
and the VP9 and AV1 decoders log what NVDEC said about each frame.

Firefox decodes in one process and draws in another: its media process (plugin-container) passes a picture's IOSurface
on by number, and its GPU process takes the surface a few milliseconds after the decoder released it. A pixel buffer
released at once could go back to its pool in between and be written with the next picture. From pull request 487 of
the original project (dreamelite96), which kept the last NVDEC_HOLD (default 8) pictures in every process; here the 8
are kept in plugin-container only, where the race was seen, and NVENCVT_DEC_HOLD=0 keeps none (as in 3.0.4).

Compiles from vendor/nvenc/vt/nvencvt_dec.c, as they are, dec_hold_pictures, hold_picture, hold_release_all and
out_emit, with stand-ins for CoreVideo's reference counts and VideoToolbox's emit, and checks:
  * in plugin-container a picture goes back to its pool exactly when the eighth later one is emitted, and every held
    one when the session is invalidated, each once; with NVENCVT_DEC_HOLD=0, and in every other process (the decode
    service, a browser's GPU process, ffmpeg), at its emit, as in 3.0.4;
  * two threads emitting into one session (the output thread and a synchronous frame) release every picture once
    (also under ThreadSanitizer where the compiler has it);
  * the wiring in the source: the switch is read once, in NVDecVT_Register, before any decoder registers; a session
    takes the count when it is made; invalidate lets the held pictures go after the output thread has ended and before
    the session's own pool, and finalize destroys the lock after that;
  * the VP9 and AV1 decoders keep NVDEC's count of blocks in error (err_blocks), their log lines print it, and
    NVENCVT_LOG=2 logs every frame; every log line starts with the time of day.

  python3 tools/test_nvencvt_dec_hold.py [--baseline [REF]]
      --baseline REF   the sources of that revision (default HEAD); there this test fails

Needs a C compiler and POSIX threads."""
import argparse
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

root = pathlib.Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument('--baseline', nargs='?', const='HEAD', metavar='REF')
args = parser.parse_args()
fails = 0


def check(name, ok, detail=''):
    global fails
    print(('ok   ' if ok else 'FAIL ') + name + (f' ({detail})' if detail else ''))
    fails += 0 if ok else 1


def source(path):
    if args.baseline is not None:
        return subprocess.check_output(['git', 'show', f'{args.baseline}:{path}'], cwd=root, text=True)
    return (root / path).read_text()


def function(text, signature):
    """the definition (not a declaration) that starts with signature, or ''"""
    start = text.find(signature)
    while start >= 0 and not text[start + len(signature):].lstrip().startswith('{'):
        start = text.find(signature, start + 1)
    if start < 0:
        return ''
    opening = text.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[start:end]


def macro(text, name):
    """a #define with its continuation lines"""
    start = text.find('#define ' + name)
    if start < 0:
        return ''
    end = start
    while True:
        nl = text.index('\n', end)
        if text[nl - 1] != '\\':
            return text[start:nl + 1]
        end = nl + 1


dec = source('vendor/nvenc/vt/nvencvt_dec.c')
compiler = shutil.which('cc') or shutil.which('clang')
if not compiler:
    sys.exit('A C compiler is required.')


def build_and_run(directory, name, text, extra=()):
    c = directory / (name + '.c')
    c.write_text(text)
    b = subprocess.run([compiler, '-std=gnu11', '-Wall', '-Wno-unused-function', '-g', '-pthread', *extra, str(c), '-o', str(directory / name)],
                       capture_output=True, text=True)
    if b.returncode:
        return None, b.stderr[-2000:]
    r = subprocess.run([str(directory / name)], capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout + r.stderr


# ---- the hold, compiled ----
hold_parts = [macro(dec, 'NVDEC_HOLD_PICTURES'),
              function(dec, 'static unsigned dec_hold_pictures(const char *env, const char *me)'),
              function(dec, 'static void hold_picture(DState *s, CVPixelBufferRef pb)'),
              function(dec, 'static void hold_release_all(DState *s)')]
have_hold = all(hold_parts)
emit = function(dec, 'static void out_emit(DState *s, OutJob *j)')

hold_fixture = r'''
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef int32_t OSStatus; typedef uint32_t VTDecodeInfoFlags;
typedef void *VTVideoDecoderSession, *VTVideoDecoderFrame;
typedef struct PB { _Atomic int refs; _Atomic int returned; int id; } *CVPixelBufferRef;
static _Atomic int returns, overreleased;
static void CVPixelBufferRelease(CVPixelBufferRef p) {
    const int r = --p->refs;
    if (r == 0) { p->returned++; returns++; }
    if (r < 0) overreleased++;
}
/* Firefox's media process: it passes the surface on by number and keeps no reference of its own */
static _Atomic int emitted;
static OSStatus VTDecoderSessionEmitDecodedFrame(VTVideoDecoderSession s, VTVideoDecoderFrame f, OSStatus r, VTDecodeInfoFlags fl,
                                                 CVPixelBufferRef pb) { (void)s; (void)f; (void)r; (void)fl; (void)pb; emitted++; return 0; }
static double nowms(void) { return 0; }
#define HAVE_HOLD /*HAVE_HOLD*/
/*HOLD_MACRO*/
#ifndef NVDEC_HOLD_PICTURES
#define NVDEC_HOLD_PICTURES 8
#endif
typedef struct { VTVideoDecoderSession session; CVPixelBufferRef held[NVDEC_HOLD_PICTURES]; unsigned heldNext, hold; pthread_mutex_t heldLock; } DState;
typedef struct OutJob { VTVideoDecoderFrame frame; OSStatus res; VTDecodeInfoFlags fl; CVPixelBufferRef pb; int surf, rel; unsigned usEmit; } OutJob;
/*HOLD_FUNCTIONS*/
/*OUT_EMIT*/
static unsigned hold_for(const char *env, const char *me) {
#if HAVE_HOLD
    return dec_hold_pictures(env, me);
#else
    (void)env; (void)me; return 0;
#endif
}
static void release_all(DState *s) {
#if HAVE_HOLD
    hold_release_all(s);
#else
    (void)s;
#endif
}
static int bad;
#define T(c) do { if (!(c)) { printf("FAIL %s (line %d)\n", #c, __LINE__); bad++; } } while (0)
static CVPixelBufferRef make(int id) { CVPixelBufferRef p = calloc(1, sizeof *p); p->refs = 1; p->id = id; return p; }
/* emit n pictures in a session of process `me`; want = how many later pictures each one waits for */
static void run(const char *env, const char *me, unsigned want) {
    DState s; memset(&s, 0, sizeof s); pthread_mutex_init(&s.heldLock, NULL);
    s.hold = hold_for(env, me);
    const int n = 40; CVPixelBufferRef p[40];
    int early = 0, late = 0;
    for (int i = 0; i < n; i++) {
        p[i] = make(i);
        OutJob j = { .pb = p[i] };
        out_emit(&s, &j);
        T(j.pb == NULL);
        for (int k = 0; k <= i; k++) {
            const int should = (unsigned)(i - k) >= want;      /* back in the pool once `want` later ones were emitted */
            if (p[k]->returned && !should) early++;
            if (!p[k]->returned && should) late++;
        }
    }
    release_all(&s);
    int once = 0; for (int i = 0; i < n; i++) once += p[i]->returned == 1;
    printf("%-24s NVENCVT_DEC_HOLD=%-5s: %u kept, %d returned early, %d late, %d of %d returned once\n", me ? me : "(no name)",
           env ? env : "unset", s.hold, early, late, once, n);
    T(s.hold == want && early == 0 && late == 0 && once == n);
    for (int i = 0; i < n; i++) free(p[i]);
    pthread_mutex_destroy(&s.heldLock);
}
static DState shared;
static void *emitter(void *arg) {
    CVPixelBufferRef *p = arg;
    for (int i = 0; i < 20000; i++) { OutJob j = { .pb = p[i] }; out_emit(&shared, &j); }
    return NULL;
}
int main(void) {
    run(NULL, "plugin-container", 8);
    run("1", "plugin-container", 8);
    run("0", "plugin-container", 0);
    run(NULL, "VTDecoderXPCService", 0);
    run(NULL, "com.apple.WebKit.GPU", 0);
    run(NULL, "Google Chrome Helper (GPU)", 0);
    run(NULL, "ffmpeg", 0);
    run(NULL, NULL, 0);
    /* two emitters into one session, then invalidate */
    memset(&shared, 0, sizeof shared); pthread_mutex_init(&shared.heldLock, NULL);
    shared.hold = hold_for(NULL, "plugin-container");
    CVPixelBufferRef *a = malloc(20000 * sizeof *a), *b = malloc(20000 * sizeof *b);
    for (int i = 0; i < 20000; i++) { a[i] = make(i); b[i] = make(20000 + i); }
    returns = 0;
    pthread_t t1, t2;
    pthread_create(&t1, NULL, emitter, a); pthread_create(&t2, NULL, emitter, b);
    pthread_join(t1, NULL); pthread_join(t2, NULL);
    const int before = returns;
    release_all(&shared);
    int once = 0; for (int i = 0; i < 20000; i++) once += (a[i]->returned == 1) + (b[i]->returned == 1);
    printf("two threads: %d returned while emitting, %d once in all, %d released too often\n", before, once, (int)overreleased);
    T(once == 40000 && overreleased == 0 && before == 40000 - (int)shared.hold);
    for (int i = 0; i < 20000; i++) { free(a[i]); free(b[i]); }
    free(a); free(b);
    return bad != 0;
}
'''
hold_fixture = (hold_fixture.replace('/*HAVE_HOLD*/', '1' if have_hold else '0')
                .replace('/*HOLD_MACRO*/', hold_parts[0]).replace('/*HOLD_FUNCTIONS*/', '\n'.join(hold_parts[1:]))
                .replace('/*OUT_EMIT*/', emit))

with tempfile.TemporaryDirectory() as directory:
    d = pathlib.Path(directory)
    check('the retention functions are in the source', have_hold, 'dec_hold_pictures, hold_picture, hold_release_all')
    rc, out = build_and_run(d, 'hold', hold_fixture)
    print(out, end='' if out.endswith('\n') else '\n')
    check('in plugin-container a picture is released when the eighth later one is emitted, and at invalidate; '
          'elsewhere and with NVENCVT_DEC_HOLD=0 at its emit', rc == 0)
    probe = subprocess.run([compiler, '-fsanitize=thread', '-x', 'c', '-', '-o', str(d / 'probe')], input='int main(void){return 0;}',
                           capture_output=True, text=True)
    if probe.returncode == 0 and subprocess.run([str(d / 'probe')], capture_output=True).returncode == 0:
        rc, out = build_and_run(d, 'hold_tsan', hold_fixture, ('-fsanitize=thread', '-O1'))
        check('two emitters into one session: no data race (ThreadSanitizer)', rc == 0 and 'ThreadSanitizer' not in out,
              out.strip().splitlines()[-1] if out.strip() else '')
    else:
        print('not run: ThreadSanitizer (the compiler or the system cannot run it here)')

    # ---- the log line's time of day, compiled ----
    dlog = macro(dec, 'DLOG(...)')
    log_fixture = r'''
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include <sys/time.h>
#include <time.h>
static bool gDLog = true; static FILE *gDLogF;
/*DLOG*/
int main(void) { gDLogF = stdout; DLOG("decodeFrame %d", 5); return 0; }
'''.replace('/*DLOG*/', dlog)
    rc, out = build_and_run(d, 'dlog', log_fixture)
    check('a log line starts with the time of day', rc == 0 and re.fullmatch(r'\d\d:\d\d:\d\d\.\d{3} nvdecvt: decodeFrame 5\n', out or '') is not None,
          (out or '').strip())

    # ---- err_blocks: the status read of each decoder, compiled ----
    for name in ('vp9dec', 'av1dec'):
        c = source(f'vendor/nvenc/src/{name}.c')
        h = source(f'vendor/nvenc/src/{name}.h')
        line = next((l for l in c.splitlines() if 'd->err_status = st->error_status;' in l), '')
        status_fixture = r'''
#include <stdio.h>
typedef struct { unsigned error_status, mbs_correctly_decoded, mbs_in_error; } nvdec_status_s;
struct dec { unsigned err_status, ok_blocks, err_blocks; };
static void status(struct dec *d, const nvdec_status_s *st) { /*LINE*/ }
int main(void) {
    struct dec d = { 0, 0, 0 }; const nvdec_status_s st = { 0x7, 8100, 60 };
    status(&d, &st);
    printf("status 0x%x ok %u err %u\n", d.err_status, d.ok_blocks, d.err_blocks);
    return !(d.err_status == 7 && d.ok_blocks == 8100 && d.err_blocks == 60);
}
'''.replace('/*LINE*/', line.strip())
        rc, out = build_and_run(d, name, status_fixture)
        check(f'{name} keeps NVDEC\'s count of blocks in error', bool(line) and rc == 0 and 'err_blocks' in h, (out or '').strip())

# ---- the VP9 and AV1 log lines, compiled with their arguments' types (as the headers declare them) ----
def log_statement(fn):
    """the `if (gDLog && ...) DLOG(...);` statement of a decode function"""
    i = fn.find('if (gDLog && (s->frames <= 3 || res')
    if i < 0:
        return ''
    j = fn.index('DLOG(', i) + 4
    depth, k = 0, j
    while True:
        depth += (fn[k] == '(') - (fn[k] == ')')
        k += 1
        if not depth:
            break
    return fn[i:fn.index(';', k) + 1]


vp9_fn = function(dec, 'static OSStatus decode_vp9(DState *s, VTVideoDecoderFrame frame, const uint8_t *p, size_t len)')
av1_fn = function(dec, 'static OSStatus decode_av1(DState *s, VTVideoDecoderFrame frame, const uint8_t *p, size_t len)')
vh, ah = source('vendor/nvenc/src/vp9dec.h'), source('vendor/nvenc/src/av1dec.h')
types = all(t in h for h in (vh, ah) for t in ('unsigned err_status, ok_blocks, err_blocks;', 'double engine_ms;', 'int dec_surf;')) \
    and 'int twin[' in ah and 'int ntiles;' in ah and 'unsigned frames, errors;' in dec
lines_fixture = r'''
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/time.h>
#include <time.h>
typedef int32_t OSStatus; typedef void *VTVideoDecoderFrame;
static bool gDLog = true, gDLogAll; static FILE *gDLogF;
/*DLOG*/
typedef struct { int dec_surf; unsigned err_status, ok_blocks, err_blocks; double engine_ms; } Vp9Dec;
typedef struct { int twin[8]; int ntiles; int dec_surf; unsigned err_status, ok_blocks, err_blocks; double engine_ms; } Av1Dec;
typedef struct { unsigned frames, errors; Vp9Dec *vp9; Av1Dec *av1; } DState;
static void vp9(DState *s, VTVideoDecoderFrame frame, size_t len, int nf, OSStatus res, int shown) { /*VP9*/ }
static void av1(DState *s, VTVideoDecoderFrame frame, size_t len, OSStatus res, int shown) { /*AV1*/ }
int main(void) {
    Vp9Dec v = { 3, 0, 8100, 60, 1.5 }; Av1Dec a = { { 5, 4, -1, -1, -1, -1, -1, -1 }, 2, 1, 0, 8100, 60, 2.5 };
    DState s = { 10, 0, &v, &a };
    gDLogF = stdout;
    vp9(&s, NULL, 1000, 2, 0, 3); av1(&s, NULL, 900, 0, 4);      /* frame 10, no error: silent unless NVENCVT_LOG=2 */
    gDLogAll = true;
    vp9(&s, NULL, 1000, 2, 0, 3); av1(&s, NULL, 900, 0, 4);
    return 0;
}
'''.replace('/*DLOG*/', macro(dec, 'DLOG(...)')).replace('/*VP9*/', log_statement(vp9_fn)).replace('/*AV1*/', log_statement(av1_fn))
with tempfile.TemporaryDirectory() as directory:
    rc, out = build_and_run(pathlib.Path(directory), 'lines', lines_fixture, ('-Wformat=2', '-Werror'))
    lines = (out or '').splitlines()
    check('the VP9 and AV1 log lines compile with -Wformat=2 -Werror against the headers\' types, print the blocks in error, '
          'and print frame 10 only with NVENCVT_LOG=2', types and rc == 0 and len(lines) == 2 and all('ok 8100 err 60' in l for l in lines)
          and 'twin 4' in lines[1], ' | '.join(lines)[-400:])

# ---- the wiring, in the source ----
register = function(dec, 'OSStatus NVDecVT_Register(int rating)')
create = function(dec, 'OSStatus NVDecVT_CreateInstance(FourCharCode codec, CFAllocatorRef al, VTVideoDecoderRef *out)')
inval = function(dec, 'static OSStatus b_invalidate(CMBaseObjectRef o)')
final = function(dec, 'static void b_finalize(CMBaseObjectRef o)')
read_at = register.find('gDecHold = dec_hold_pictures(getenv("NVENCVT_DEC_HOLD"), getprogname());')
check('NVENCVT_DEC_HOLD is read once, in NVDecVT_Register, before any decoder registers',
      dec.count('"NVENCVT_DEC_HOLD"') == 1 and 0 <= read_at < register.find('reg(kCMVideoCodecType_H264'))
check('a session takes the count when it is made, and its lock', 's->hold = gDecHold' in create and 'pthread_mutex_init(&s->heldLock, NULL)' in create)
join, rel, pool = inval.find('pthread_join(s->thr, NULL)'), inval.find('hold_release_all(s);'), inval.find('CVPixelBufferPoolRelease(s->ownPool)')
check('invalidate lets the held pictures go after the output thread ended and before the session\'s pool', 0 <= join < rel < pool)
check('finalize destroys the hold\'s lock after invalidate', 0 <= final.find('b_invalidate(o)') < final.find('pthread_mutex_destroy(&s->heldLock)'))
check('out_emit hands its reference to the hold instead of releasing it', 'hold_picture(s, j->pb)' in emit and 'CVPixelBufferRelease(j->pb)' not in emit)
vp9 = function(dec, 'static OSStatus decode_vp9(DState *s, VTVideoDecoderFrame frame, const uint8_t *p, size_t len)')
av1 = function(dec, 'static OSStatus decode_av1(DState *s, VTVideoDecoderFrame frame, const uint8_t *p, size_t len)')
check('NVENCVT_LOG=2 logs every VP9 and AV1 frame with the blocks in error',
      all('|| gDLogAll)' in f and 'err_blocks' in f for f in (vp9, av1)) and 'gDLogAll = lv && !strcmp(lv, "2");' in register)

print('all passed' if not fails else f'{fails} FAILED')
sys.exit(1 if fails else 0)
