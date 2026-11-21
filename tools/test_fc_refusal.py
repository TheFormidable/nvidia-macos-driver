#!/usr/bin/env python3
# Holds nvmtl_translate_with_fc_cached (plugin/NVMTLLibraryLoad.m) to remembering only the translator's own refusal of
# a function-constant variant (nvmtl_fc_refusal.h): refused is set on that branch alone, never on an early return or
# after the one-module route declined; the refusal is looked up only after memory, disk and the warm-up missed and
# before the translator is asked; the caller gets the same message either way.
import pathlib, re, sys

root = pathlib.Path(__file__).resolve().parent.parent
src = (root / 'plugin' / 'NVMTLLibraryLoad.m').read_text()
fails = 0
def arm(what, ok):
    global fails
    print(('ok    ' if ok else 'FAIL  ') + what)
    if not ok: fails += 1

def body(head):
    i = src.find(head)
    if i < 0: return ''
    j = src.find('{', i); depth = 0
    for k in range(j, len(src)):
        if src[k] == '{': depth += 1
        elif src[k] == '}':
            depth -= 1
            if depth == 0: return src[j + 1:k + 1]   # the body after its opening brace
    return ''

r = body('NSData *nvmtl_translate_with_fc(NSString *ll, NSString *stage, const uint32_t *idx, const uint32_t *sizes,')
arm('the variant translation is found', bool(r))
sets = [m.start() for m in re.finditer(r'g_fc_refused = ', r)]
arm('the refused flag is cleared first and set in exactly one other place', len(sets) == 2 and r.lstrip().startswith('g_fc_refused = 0;'))
arm('the flag is per thread, so a refusal on one thread never marks another thread\'s request',
    'static __thread int g_fc_refused;' in src)
fc = r.find('if (xlate_fc(ll.UTF8String, stage.UTF8String, idx, sizes, payloads, n, &out, &out_len, e, sizeof e) != 0) {')
yes = r.find('g_fc_refused = !viaSpecTried;')
arm('it is set inside the translator\'s own refusal, right before that branch returns',
    0 <= fc < yes < r.find('return nil;', fc) and r.find('return nil;', fc) == r.find('return nil;', yes))
arm('the early returns (translator not loaded, no xlate_fc, no AIR) come before that branch and leave it 0',
    all(0 <= r.find(t) < fc for t in ('if (!nvmtl_xlate_ready())', 'if (!xlate_fc)', 'if (!ll || !stage.length)')))
arm('a declined one-module route is noted, and the route returns only a module',
    'viaSpecTried = YES;' in r and 'if (viaSpec) return viaSpec;' in r)
c = body('NSData *nvmtl_translate_with_fc_cached(')
look = c.find('nvmtl_translate_failure_for(memoKey)')
use = c.find('if (nvmtl_fc_refusal_use(memoOn, refusedBefore != nil)) {')
tr = c.find('NSData *result = nvmtl_translate_with_fc(ll, stage, idx, sizes, payloads, n, &why);')
arm('the refusal is looked up after the memory, the disk file and the warm-up',
    0 <= c.find('[nvmtl_memo() objectForKey:memoKey]') < c.find('nvmtl_fcwarm_take_or_join(path)') < c.find('if (magic == 0x07230203)') < look)
arm('and before the translator is asked', 0 <= look < use < tr)
arm('a remembered refusal hands back the same message and no module',
    re.search(r'if \(nvmtl_fc_refusal_use\(memoOn, refusedBefore != nil\)\) \{\s*if \(err\) \*err = refusedBefore;', c) is not None
    and c.find('return nil;', use) < tr)
arm('the flag is read right after the call, and the caller gets the translator\'s message only on failure, as before',
    c.find('const int refused = !result && g_fc_refused;') == tr + len('NSData *result = nvmtl_translate_with_fc(ll, stage, idx, sizes, payloads, n, &why);') + 5
    and 'if (!result && err) *err = why;' in c)
arm('a refusal is noted only when the translator refused, with the cache on, under the cap',
    'if (!result && nvmtl_fc_refusal_keep(refused, memoOn, memoKey != nil, gFcRefusalsKept))' in c
    and 'nvmtl_fc_refusal_note(memoKey, why ?: @"specialization failed");' in c)
arm('the cached path has no other translate call after the lookup', c.count('nvmtl_translate_with_fc(') == 3 and c.find('nvmtl_translate_with_fc(', look) == tr + len('NSData *result = '))
m = body('static int nvmtl_fc_refusal_memo_on(void) {')
arm('NVMTL_FC_REFUSAL_MEMO is read once into a static, through the header\'s rule, in exactly one function',
    'static int on = -1;' in m and 'getenv("NVMTL_FC_REFUSAL_MEMO")' in m and 'nvmtl_fc_refusal_memo_env_on(e)' in m
    and src.count('getenv("NVMTL_FC_REFUSAL_MEMO")') == 1)
arm('the gate of the lookup, the use and the keep is the cache switch ANDed with it, and nothing else',
    'const BOOL memoOn = fcCacheOn && nvmtl_fc_refusal_memo_on();' in c
    and 'NSString *refusedBefore = memoOn ? nvmtl_translate_failure_for(memoKey) : nil;' in c
    and c.count('nvmtl_translate_failure_for(') == 1
    and re.findall(r'nvmtl_fc_refusal_use\((\w+),', c) == ['memoOn']
    and re.findall(r'nvmtl_fc_refusal_keep\(refused, (\w+),', c) == ['memoOn']
    and 0 <= c.find('const BOOL memoOn') < look)
n = body('static void nvmtl_fc_refusal_note(NSString *key, NSString *why) {')
arm('the note keeps the cap under the same lock as the lookup',
    '@synchronized([NSProcessInfo processInfo])' in n and 'gFcRefusalsKept++' in n and 'nvmtl_fc_refusal_keep(1, 1, key != nil, gFcRefusalsKept)' in n)
arm('fc keys are fc1 cache names, disjoint from entry keys', 'NSString *path = nvmtl_spvcache_path_air(airDigest, json, @"fc1");' in c
    and 'NSString *memoKey = path ?: nvmtl_spvcache_memo_key_air(airDigest, json, @"fc1");' in c)

print('FAILED %d' % fails if fails else 'ok')
sys.exit(1 if fails else 0)
