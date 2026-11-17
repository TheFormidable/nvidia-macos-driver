#!/usr/bin/env python3
"""Function-constant warm-up: a recorded variant is translated ahead of the caller into exactly the file a live miss
writes, and the caller never waits for a variant the background has not started.

WindowServer's main thread translated Core Animation's fixed_frag variants one after another after a driver update
(each 0.5-2 s; the watchdog kills it after 40 s without a check-in). Since 2.9 the plugin records slow sets beside the
cache and, the first time a process asks for any variant of that AIR text, hands the recorded siblings to the
background. Up to 3.0.1 each sibling had a block of its own behind a two-slot semaphore, and the main thread waited up
to 60 s for one still queued, so the watchdog killed it again. Now two workers take the siblings from a queue, and a
caller reads a file already on disk at once, takes a pending sibling over and translates it itself, and waits for a
running one for at most NVMTL_FCWARM_JOIN_MS. This compiles the production code (the cache key, nvmtl_translate_with_fc,
nvmtl_translate_with_fc_cached and the warm-up) out of NVMTLLibraryLoad.m against the real libnvmtl_translate.dylib,
and runs "boots":

  boot 1   live misses for five sets: files + records
  update   every cache file deleted (a new translator build sweeps them), records kept
  boot 2   the caller asks for ONE set; the other four must appear in the background, while one never recorded must not
  ref      a fresh cache, each set translated live on its own: names and bytes must equal boot 2's warmed files
  join     with the files gone again, a second set asked for right after the first is read, joined or taken over, and
           translated once in all
  off      NVMTL_FCWARM_OFF=1: boot 2 warms nothing (the control that must fire)
  timing   five sets asked for at once: each translated once in all, and the asking thread waits less than for five
           misses without the warm-up
  bound    the join bound is a named constant far below the watchdog's 40 s
  pending  the background held slow (TEST_BG_MS): a sibling no worker has started is taken over, the caller returns
           after about one translation of its own, and the background never translates it
  running  a sibling a worker is translating is joined: the caller gets the worker's file and translates nothing
  hit      a sibling a worker is translating, whose file another writer has already put on disk: the caller reads it at
           once and does not wait for the worker
  late     a worker slower than the bound: the caller stops waiting after NVMTL_FCWARM_JOIN_MS and translates itself
  workers  the background translates at most two at a time, on at most two threads

The harness wraps the translator: it counts each translation per set and per thread (the asking thread or the
warm-up's), adds --pad-ms to every translation, so that a small shader stands in for the 1.3-2 s uber-shader and its
misses are recorded (100 ms), and adds TEST_BG_MS to the warm-up's translations in the scenes that hold the background
back. The scenes pending, late and workers fail against the warm-up of 44091839 (the caller waits for the queue, or for
as long as the worker takes, up to 60 s; on Linux each queued variant held a thread of its own), and bound fails there
too (60 s). running passes there as well, as 44091839 also waited for a variant that was running; late is the scene
that tells the two apart. hit passes there too, as 44091839 read the file first; it fails when the warm-up is asked
before the file is read.

usage: test_fcwarm.py [--pad-ms N] <libnvmtl_translate.dylib> <fixed_frag .ll> <sets file: idx:hex,... per line, six or
more> [repo root]"""
import hashlib
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time

args = sys.argv[1:]
pad_ms = 0
if args[:1] == ["--pad-ms"]:
    pad_ms, args = int(args[1]), args[2:]
lib, ll, sets_file = args[:3]
root = pathlib.Path(args[3] if len(args) > 3 else pathlib.Path(__file__).resolve().parents[1])
src = (root / "plugin/NVMTLLibraryLoad.m").read_text()
nsets = len([line for line in pathlib.Path(sets_file).read_text().splitlines() if line.strip()])
fails = 0


def arm(name, ok, detail=""):
    global fails
    fails += not ok
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f" ({str(detail).strip()[-500:]})"))


def cut(start, end):
    i = src.index(start)
    return src[i:src.index(end, i)]


parts = [
    # the entry points of the one-module path; this test leaves them unloaded, as NVMTL_FC_SPEC is off by default
    cut("// One translation per function for every set of function constants", "static nvmtl_refl_fn xlate_refl;"),
    cut("static _Atomic unsigned long gSpvHit, gSpvMiss, gSpvStored;", "static _Atomic unsigned long long gSpvAdded;"),
    cut("@interface NVMTLAirDigest : NSObject", "static BOOL nvmtl_air_unroll_wanted(NSData *air);"),
    # the framed cache files (nvmtl_cache_read, nvmtl_cache_write) the warm-up and a live miss write
    cut("// ---- cache files that prove they are whole", "// ---- one translation per module"),
    cut("static __thread int g_fc_expect_link;", "static int nvmtl_reflect_air("),
]
HARNESS = r'''
#import <Foundation/Foundation.h>
#include <CommonCrypto/CommonDigest.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <objc/runtime.h>
#include <os/lock.h>
#include <pthread.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <dirent.h>
#include <stdatomic.h>
#include <time.h>
#include <unistd.h>
#include "nvmtl_cache_file.h"
#define nvlog(...) (fprintf(stderr, __VA_ARGS__), fputc('\n', stderr))
static int nvmtl_vk_lost(void) { return 0; }   // no device in this test, so never lost
typedef int (*nvmtl_fc_fn)(const char *ll, const char *stage, const uint32_t *idx, const uint32_t *sizes,
                           const uint8_t *const *payloads, size_t n, uint8_t **out, size_t *out_len, char *err, size_t err_len);
typedef void (*nvmtl_free_fn)(uint8_t *p, size_t len);
static nvmtl_fc_fn xlate_fc;
static nvmtl_free_fn xlate_free;
static int nvmtl_xlate_ready(void) { return xlate_fc != NULL; }
static char gXlateTag[80] = "testbuild";
// the cache folder and file names: the plugin's naming, in a folder the test chooses
static NSString *nvmtl_spvcache_dir(void) { return @(getenv("TEST_CACHE")); }
static NSString *nvmtl_spvcache_path_digest(const unsigned char h[CC_SHA256_DIGEST_LENGTH], NSString *stage) {
    char hex[2 * CC_SHA256_DIGEST_LENGTH + 1];
    for (int i = 0; i < CC_SHA256_DIGEST_LENGTH; i++) snprintf(hex + 2 * i, 3, "%02x", h[i]);
    return [nvmtl_spvcache_dir() stringByAppendingPathComponent:[NSString stringWithFormat:@"%s.%@.%s.c3.spv", hex, stage, gXlateTag]];
}
BOOL nvmtl_needs_visible_link(NSString *air, NSString *why) { (void)air; (void)why; return NO; }
// declared in NVMTLObjects.m in the plugin's single translation unit
enum { NVMTL_AIRF_KNOWN = 1, NVMTL_AIRF_DYLD_TABLE = 2, NVMTL_AIRF_ISECT_TABLE = 4 };
''' + "\n".join(parts) + r'''
// The translator as the plugin calls it, wrapped: every call is counted for its set and for the thread that made it
// (the test's asking thread, or the warm-up's), and slowed down by TEST_PAD_MS (every call) and TEST_BG_MS (the
// warm-up's calls only). Nothing of the production code is changed: it calls xlate_fc, which points here.
#define TEST_MAX_SETS 64
static nvmtl_fc_fn real_fc;
static __thread int t_asker;
static pthread_mutex_t gTestMu = PTHREAD_MUTEX_INITIALIZER;
static NSArray<NSString *> *gTestKeys;   // each set as "idx:hex,..." sorted by index
static int gAsker[TEST_MAX_SETS], gBg[TEST_MAX_SETS], gBgNow[TEST_MAX_SETS], gBgBusy, gBgPeak, gBgThreadCount;
static pthread_t gBgThreads[TEST_MAX_SETS];
static NSString *test_key(NSDictionary<NSNumber *, NSString *> *byIndex) {
    NSMutableArray *kv = [NSMutableArray array];
    for (NSNumber *i in [byIndex.allKeys sortedArrayUsingSelector:@selector(compare:)])
        [kv addObject:[NSString stringWithFormat:@"%u:%@", i.unsignedIntValue, byIndex[i]]];
    return [kv componentsJoinedByString:@","];
}
static int test_set_of(const uint32_t *idx, const uint32_t *sizes, const uint8_t *const *payloads, size_t n) {
    NSMutableDictionary *d = [NSMutableDictionary dictionary];
    for (size_t k = 0; k < n; k++) {
        NSMutableString *h = [NSMutableString string];
        for (uint32_t j = 0; j < sizes[k]; j++) [h appendFormat:@"%02x", payloads[k][j]];
        d[@(idx[k])] = h;
    }
    NSUInteger i = [gTestKeys indexOfObject:test_key(d)];
    return i == NSNotFound || i >= TEST_MAX_SETS ? -1 : (int)i;
}
static int test_fc(const char *ll, const char *stage, const uint32_t *idx, const uint32_t *sizes,
                   const uint8_t *const *payloads, size_t n, uint8_t **out, size_t *out_len, char *err,
                   size_t err_len) {
    int k;
    @autoreleasepool { k = test_set_of(idx, sizes, payloads, n); }
    const int bg = !t_asker;
    pthread_mutex_lock(&gTestMu);
    if (k >= 0) { if (bg) { gBg[k]++; gBgNow[k]++; } else gAsker[k]++; }
    if (bg) {
        if (++gBgBusy > gBgPeak) gBgPeak = gBgBusy;
        int seen = 0;
        for (int i = 0; i < gBgThreadCount; i++) seen |= pthread_equal(gBgThreads[i], pthread_self());
        if (!seen && gBgThreadCount < TEST_MAX_SETS) gBgThreads[gBgThreadCount++] = pthread_self();
    }
    pthread_mutex_unlock(&gTestMu);
    const char *pad = getenv("TEST_PAD_MS"), *slow = getenv("TEST_BG_MS");
    const long ms = (pad ? atol(pad) : 0) + (bg && slow ? atol(slow) : 0);
    if (ms > 0) usleep((useconds_t)(ms * 1000));
    const int rc = real_fc(ll, stage, idx, sizes, payloads, n, out, out_len, err, err_len);
    pthread_mutex_lock(&gTestMu);
    if (bg) { gBgBusy--; if (k >= 0) gBgNow[k]--; }
    pthread_mutex_unlock(&gTestMu);
    return rc;
}
static NSArray *test_sets(const char *file) {
    NSMutableArray *out = [NSMutableArray array];
    NSMutableArray *keys = [NSMutableArray array];
    for (NSString *line in [[NSString stringWithContentsOfFile:@(file) encoding:NSUTF8StringEncoding error:NULL] componentsSeparatedByString:@"\n"]) {
        if (!line.length) continue;
        NSMutableDictionary *d = [NSMutableDictionary dictionary], *byIndex = [NSMutableDictionary dictionary];
        for (NSString *kv in [line componentsSeparatedByString:@","]) {
            NSArray *p = [kv componentsSeparatedByString:@":"];
            NSMutableArray *b = [NSMutableArray array];
            NSString *h = [p[1] lowercaseString];
            for (NSUInteger i = 0; i + 1 < h.length + 1 && i < h.length; i += 2)
                [b addObject:@((uint8_t)strtoul([h substringWithRange:NSMakeRange(i, 2)].UTF8String, NULL, 16))];
            d[p[0]] = b;
            NSMutableString *hx = [NSMutableString string];
            for (NSNumber *x in b) [hx appendFormat:@"%02x", x.unsignedCharValue];
            byIndex[@((uint32_t)[p[0] integerValue])] = hx;
        }
        [out addObject:d];
        [keys addObject:test_key(byIndex)];
    }
    gTestKeys = keys;
    return out;
}
// what the plugin's newFunctionWithName:constantValues: hands over: the dictionary plus packed index/size/payload arrays
static NSData *test_live(NSString *ll, NSDictionary *c, double *ms) {
    size_t n = c.count, k = 0;
    uint32_t idx[256], sz[256]; const uint8_t *pay[256]; uint8_t bytes[256][16];
    for (NSString *key in c) {
        NSArray *a = c[key]; idx[k] = (uint32_t)key.integerValue; sz[k] = (uint32_t)a.count;
        for (NSUInteger j = 0; j < a.count; j++) bytes[k][j] = [a[j] unsignedCharValue];
        pay[k] = bytes[k]; k++;
    }
    NSString *e = nil;
    uint64_t t0 = clock_gettime_nsec_np(CLOCK_MONOTONIC_RAW);
    NSData *d = nvmtl_translate_with_fc_cached(ll, @"fragment", @"fixed_frag_lph_cpf", c, idx, sz, pay, n, &e);
    if (ms) *ms = (clock_gettime_nsec_np(CLOCK_MONOTONIC_RAW) - t0) / 1e6;
    if (!d) fprintf(stderr, "translate failed: %s\n", e.UTF8String);
    return d;
}
static void test_wait_warm(void) {
    for (int i = 0; i < 1200; i++) {
        os_unfair_lock_lock(&gFcWarmLock);
        const NSUInteger busy = gFcWarmInFlight.count;
        os_unfair_lock_unlock(&gFcWarmLock);
        if (!busy) break;
        usleep(100000);
    }
    // a job taken over, or a wait that ran out, leaves the worker's translation running: wait for those too
    for (int i = 0; i < 1200; i++) {
        pthread_mutex_lock(&gTestMu);
        const int busy = gBgBusy;
        pthread_mutex_unlock(&gTestMu);
        if (!busy) break;
        usleep(100000);
    }
    nvmtl_cache_io_drain();
}
// "p:1,2,3" asks for one of those sets that the warm-up has not started while two of its translations run (pending);
// "r:1,2,3" asks for one the warm-up is translating now (running). Each waits up to 30 s for that state. "h:1,2,3"
// picks a running one as r: does, then writes its cache file as another writer would (test_plant) and asks for it.
static int test_pick(const char *spec, int running) {
    NSArray *cands = [@(spec + 2) componentsSeparatedByString:@","];
    for (int t = 0; t < 3000; t++) {
        int pick = -1;
        pthread_mutex_lock(&gTestMu);
        const int busy = gBgBusy;
        for (NSString *c in cands) {
            const int k = c.intValue;
            if (k < 0 || k >= TEST_MAX_SETS) continue;
            if (running ? gBgNow[k] > 0 : (busy >= 2 && !gBg[k] && !gAsker[k])) { pick = k; break; }
        }
        pthread_mutex_unlock(&gTestMu);
        if (pick >= 0) return pick;
        usleep(10000);
    }
    return -1;
}
// The set's cache file, written as a live miss writes it (the plugin's key, translator and framed file), by a call
// the wrapper does not count.
static void test_plant(NSString *ll, NSDictionary *c) {
    size_t n = c.count, k = 0;
    uint32_t idx[256], sz[256]; const uint8_t *pay[256]; uint8_t bytes[256][16];
    for (NSString *key in c) {
        NSArray *a = c[key]; idx[k] = (uint32_t)key.integerValue; sz[k] = (uint32_t)a.count;
        for (NSUInteger j = 0; j < a.count; j++) bytes[k][j] = [a[j] unsignedCharValue];
        pay[k] = bytes[k]; k++;
    }
    NSDictionary *config = @{ @"stage": @"fragment", @"entry": @"fixed_frag_lph_cpf", @"constants": c };
    NSData *json = [NSJSONSerialization dataWithJSONObject:config options:NSJSONWritingSortedKeys error:NULL];
    NSString *path = nvmtl_spvcache_path_air(nvmtl_air_state(ll), json, @"fc1");
    uint8_t *out = NULL; size_t len = 0; char e[512] = "";
    const int rc = real_fc(ll.UTF8String, "fragment", idx, sz, pay, n, &out, &len, e, sizeof e);
    const int w = rc == 0 && out ? nvmtl_cache_write(path, [NSData dataWithBytes:out length:len]) : -1;
    if (out && xlate_free) xlate_free(out, len);
    printf("PLANTED rc %d write %d\n", rc, w);
}
int main(int argc, char **argv) {
    @autoreleasepool {
        t_asker = 1;
        void *h = dlopen(getenv("TEST_LIB"), RTLD_NOW);
        if (!h) { fprintf(stderr, "dlopen: %s\n", dlerror()); return 2; }
        real_fc = (nvmtl_fc_fn)dlsym(h, "nvmtl_translate_fc");
        xlate_free = (nvmtl_free_fn)dlsym(h, "nvmtl_translate_free");
        xlate_fc = real_fc ? test_fc : NULL;
        NSString *ll = [NSString stringWithContentsOfFile:@(getenv("TEST_LL")) encoding:NSUTF8StringEncoding error:NULL];
        NSArray *sets = test_sets(getenv("TEST_SETS"));
        // argv: a list of set numbers to ask for, in order; "w" waits for the warm-up to finish; p:... and r:... above
        for (int i = 1; i < argc; i++) {
            if (!strcmp(argv[i], "w")) { test_wait_warm(); continue; }
            int k = atoi(argv[i]);
            if (argv[i][0] == 'p' || argv[i][0] == 'r' || argv[i][0] == 'h') {
                k = test_pick(argv[i], argv[i][0] != 'p');
                printf("PICKED %s %d\n", argv[i][0] != 'p' ? "running" : "pending", k);
                if (k < 0) continue;
                if (argv[i][0] == 'h') test_plant(ll, sets[(NSUInteger)k]);
            }
            double ms = 0; NSData *d = test_live(ll, sets[(NSUInteger)k], &ms);
            printf("ASKED %d %lu B %.0f ms\n", k, (unsigned long)d.length, ms);
        }
        test_wait_warm();
        for (NSUInteger k = 0; k < sets.count && k < TEST_MAX_SETS; k++)
            printf("XLATE %lu asker %d background %d\n", (unsigned long)k, gAsker[k], gBg[k]);
        printf("BACKGROUND at most %d at once, on %d thread(s)\n", gBgPeak, gBgThreadCount);
    }
    return 0;
}
'''

work = pathlib.Path(tempfile.mkdtemp(prefix="fcwarm-"))
(work / "t.m").write_text(HARNESS)
cc = subprocess.run(["clang", "-fobjc-arc", "-O1", "-w", "-I", str(root / "plugin"), "-framework", "Foundation",
                     str(work / "t.m"), "-o", str(work / "t")], capture_output=True, text=True)
arm("the production warm-up and cache code compile out of NVMTLLibraryLoad.m", cc.returncode == 0, cc.stderr)
if cc.returncode:
    sys.exit(1)
if nsets < 6:
    arm("the sets file has six sets or more", False, nsets)
    sys.exit(1)


def boot(cache, asks, extra=None):
    env = dict(os.environ, TEST_LIB=lib, TEST_LL=ll, TEST_SETS=sets_file, TEST_CACHE=str(cache),
               TEST_PAD_MS=str(pad_ms), **(extra or {}))
    t = time.time()
    p = subprocess.run([str(work / "t"), *asks], capture_output=True, text=True, env=env, timeout=900)
    return p, time.time() - t


def files(cache):
    return {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in pathlib.Path(cache).glob("*.c3.spv")}


def asked(out):
    """set -> ms the asking thread spent on it"""
    return {int(line.split()[1]): float(line.split()[-2]) for line in out.splitlines() if line.startswith("ASKED")}


def xlate(out):
    """set -> (translations by the asking thread, by the warm-up)"""
    rows = (line.split() for line in out.splitlines())
    return {int(w[1]): (int(w[3]), int(w[5])) for w in rows if w[:1] == ["XLATE"]}


def picked(out):
    m = re.search(r"^PICKED \w+ (-?\d+)$", out, re.M)
    return int(m.group(1)) if m else -1


def total(out):
    return sum(a + b for a, b in xlate(out).values())


A, B, C, D = (work / x for x in "ABCD")
for d in (A, B, C, D):
    d.mkdir()
# sets 1..4 are the recorded siblings (generic and specific), 0 is the one the caller asks for, 5 is never recorded
p, _ = boot(A, ["0", "1", "2", "3", "4"])
boot1 = files(A)
arm("boot 1: five live misses leave five cache files", len(boot1) == 5, p.stdout + p.stderr)
rec = (A / ".fcwarm.v1").read_text().splitlines() if (A / ".fcwarm.v1").exists() else []
arm("  and five records beside them (each took over 100 ms)", len(rec) == 5, rec)
for f in A.glob("*.c3.spv"):
    f.unlink()

p, secs = boot(A, ["0"])
boot2 = files(A)
arm("boot 2: asking for one set brings the four recorded siblings in the background", len(boot2) == 5, p.stdout + p.stderr)
arm("  and says so", "4 recorded variant(s) of this function translating in the background" in p.stderr, p.stderr)

ref, live_ms = {}, []
for k in ("1", "2", "3", "4", "5"):
    shutil.rmtree(B); B.mkdir()
    p, _ = boot(B, [k])
    ref[k] = files(B)
    live_ms += asked(p.stdout).values()
same = all(len(ref[k]) == 1 and next(iter(ref[k].items())) in boot2.items() for k in ("1", "2", "3", "4"))
arm("each warmed file has the live file's name and bytes (sets 1-4)", same, {k: ref[k] for k in ref} | {"boot2": boot2})
arm("negative control: a set nobody recorded (5) is not warmed", not (set(ref["5"]) & set(boot2)), (ref["5"], boot2))
diff_key = len({next(iter(ref[k])) for k in ("1", "2", "3", "4", "5")}) == 5
arm("negative control: a different constant set is a different cache file", diff_key, ref)
t_max = max(live_ms or [0])   # the slowest live miss of one set on this machine, with the pad

# join: records present, no files; ask for 0 then 1 at once - 1 is running in the background, or still queued
for f in A.glob("*.c3.spv"):
    f.unlink()
p, _ = boot(A, ["0", "1"])
took = p.stderr.count("TOOK OVER")
arm("a sibling asked for right after the first set is read, joined or taken over, and translated once in all",
    xlate(p.stdout).get(1, (0, 0)) in ((0, 1), (1, 0)) and p.stderr.count("fc cache: miss") == 1 + took
    and ("fc cache: HIT" in p.stderr or "JOINED" in p.stderr or took == 1), p.stdout + p.stderr[-800:])
arm("  and gets the same bytes", files(A).items() >= {next(iter(ref["1"].items()))}, files(A))

shutil.copy(A / ".fcwarm.v1", C / ".fcwarm.v1")
p, _ = boot(C, ["0"], {"NVMTL_FCWARM_OFF": "1"})
arm("control: NVMTL_FCWARM_OFF=1 warms nothing", len(files(C)) == 1, (files(C), p.stderr[-300:]))

# timing: what the asking thread pays for five sets, cold, with and without warm records
shutil.copy(A / ".fcwarm.v1", D / ".fcwarm.v1")
p_w, _ = boot(D, ["0", "1", "2", "3", "4"])
E = work / "E"; E.mkdir()
p_c, _ = boot(E, ["0", "1", "2", "3", "4"], {"NVMTL_FCWARM_OFF": "1"})
w, c = sum(asked(p_w.stdout).values()), sum(asked(p_c.stdout).values())
took = p_w.stderr.count("TOOK OVER")
arm("asking for all five at once: each set translated once in all, live misses only for 0 and those taken over",
    total(p_w.stdout) == 5 and p_w.stderr.count("fc cache: miss") == 1 + took, p_w.stdout + p_w.stderr[-800:])
arm("control: without the warm-up all five are live misses", p_c.stderr.count("fc cache: miss") == 5, p_c.stderr[-400:])
print(f"     asking thread, five cold sets: {c:.0f} ms without warm-up, {w:.0f} ms with records "
      f"({len(asked(p_w.stdout))} asked, {took} taken over)")
arm("the asking thread waits less with the warm-up", w < c, (w, c))

# bound: the join's limit, from the source, as the plugin compiles it
m = re.search(r"^#define NVMTL_FCWARM_JOIN_(MS|SEC) (\d+) +// chosen:", src, re.M)
bound_ms = (int(m.group(2)) * (1000 if m.group(1) == "SEC" else 1)) if m else 0
arm("the join bound is a chosen constant far below the watchdog's 40 s (at most 10 s)", 0 < bound_ms <= 10000,
    m.group(0) if m else "no NVMTL_FCWARM_JOIN_MS")
bound_ms = bound_ms or 60000

# pending: the background held back, so two siblings run and two wait; the caller asks for a waiting one
F = work / "F"; F.mkdir()
shutil.copy(A / ".fcwarm.v1", F / ".fcwarm.v1")
bg_ms = int(2 * t_max) + 2000
p, _ = boot(F, ["0", "p:1,2,3,4", "w"], {"TEST_BG_MS": str(bg_ms)})
k, ms, x = picked(p.stdout), asked(p.stdout), xlate(p.stdout)
arm(f"a sibling still pending in the background is taken over: the caller returns after about one translation "
    f"(at most {t_max + 1000:.0f} ms; a background translation takes over {bg_ms} ms)",
    k > 0 and ms.get(k, 1e9) <= t_max + 1000, p.stdout + p.stderr[-800:])
arm("  and the background skips it: translated once, by the caller, while each other sibling is the background's",
    k > 0 and x.get(k) == (1, 0) and all(x.get(j) == (0, 1) for j in (1, 2, 3, 4) if j != k) and x.get(0) == (1, 0),
    p.stdout)
arm("  and the take-over is counted",
    "TOOK OVER a recorded variant the background had not started (1 so far)" in p.stderr, p.stderr[-800:])
arm("  and its file is the live file", k > 0 and files(F).items() >= ref.get(str(k), {"none": ""}).items(),
    (k, files(F), ref.get(str(k))))
pend_out = p.stdout

# running: the caller asks for a sibling a worker is translating, which ends well within the bound
G = work / "G"; G.mkdir()
shutil.copy(A / ".fcwarm.v1", G / ".fcwarm.v1")
p, _ = boot(G, ["0", "r:1,2,3,4", "w"], {"TEST_BG_MS": "500"})
k, ms, x = picked(p.stdout), asked(p.stdout), xlate(p.stdout)
arm("a sibling the background is translating is joined: the caller gets its file and translates nothing",
    k > 0 and x.get(k) == (0, 1) and "JOINED the background translation" in p.stderr
    and "did not finish in time" not in p.stderr and ms.get(k, 1e9) <= t_max + 500 + 1000, p.stdout + p.stderr[-800:])

# hit: a worker is translating a sibling whose file is already on disk (another process, or a caller's own miss, wrote
# it); the caller reads the file and does not wait for the worker
G2 = work / "G2"; G2.mkdir()
shutil.copy(A / ".fcwarm.v1", G2 / ".fcwarm.v1")
hold_ms = int(2 * t_max) + 3000
p, _ = boot(G2, ["0", "h:1,2,3,4", "w"], {"TEST_BG_MS": str(hold_ms)})
k, ms, x = picked(p.stdout), asked(p.stdout), xlate(p.stdout)
arm(f"a sibling whose file is on disk is read at once while a worker translates it (within 1000 ms; the worker is "
    f"held {hold_ms} ms): no wait, no translation by the caller",
    k > 0 and "PLANTED rc 0 write 0" in p.stdout and ms.get(k, 1e9) <= 1000 and x.get(k) == (0, 1)
    and "JOINED" not in p.stderr and "fc cache: HIT" in p.stderr,
    p.stderr[-300:] + f" | set {k} asked in {ms.get(k)} ms, translations (caller, background) {x.get(k)}, "
    f"planted: {'PLANTED rc 0 write 0' in p.stdout}")

# late: a worker slower than the bound; the caller stops waiting at the bound and translates the sibling itself
H = work / "H"; H.mkdir()
shutil.copy(A / ".fcwarm.v1", H / ".fcwarm.v1")
p, _ = boot(H, ["0", "r:1,2,3,4", "w"], {"TEST_BG_MS": str(min(bound_ms, 10000) + 3000)})
k, ms, x = picked(p.stdout), asked(p.stdout), xlate(p.stdout)
arm(f"a join waits at most the bound ({bound_ms} ms), then the caller translates it itself",
    k > 0 and bound_ms - 50 <= ms.get(k, -1) <= bound_ms + t_max + 1000 and x.get(k, (0, 0))[0] == 1
    and "did not finish in time, translating here" in p.stderr, (ms.get(k), p.stdout, p.stderr[-600:]))

# workers: what the pending scene's background used (two at a time, at most two threads)
m = re.search(r"^BACKGROUND at most (\d+) at once, on (\d+) thread", pend_out, re.M)
peak, threads = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
arm(f"the background translates at most two at a time, on at most two threads ({peak} at once, {threads} threads)",
    m and 1 <= peak <= 2 and 1 <= threads <= 2, pend_out)
print(f"{fails} failed")
sys.exit(1 if fails else 0)
