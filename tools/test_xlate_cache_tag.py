#!/usr/bin/env python3
"""test_xlate_cache_tag.py: the shader cache tag the plugin builds when it opens the translator names the translator's
way back from the whole-object views of pull request 524 of the original project. With NVMTL_WHOLE_VIEWS=0 the tag
ends in -wv0, after every other mode mark, so a module translated with the views on is never served to a process that
has them off, nor the other way; any other value, or none, leaves the tag as it was. The plugin and the translator
read the same byte: a value whose first byte is 0, even one that is not UTF-8, adds the mark (the translator reads it in
translator/translator/src/native/view_switches.rs). When the tag with its mark does not fit, the tag is "untagged":
the shader cache is off in that process. Without a translator there is no tag to mark.

tools/test_app_profile.c holds the rules of nvmtl_spvcache_escape_tag (plugin/nvmtl_spvcache_name.h); this test holds
the call. It compiles the tag statements of nvmtl_xlate_ready in plugin/NVMTLLibraryLoad.m as they are, from the build
tag to the line that gives up when the translator did not open (the first use of the tag comes after that line), with
stand-ins for the translator's build identity, the typed dummy slots and the log, and runs them once per case.

  python3 tools/test_xlate_cache_tag.py [--baseline [REF]]
      --baseline REF   the sources of that revision (default HEAD); on 3.0.4 (e3ea9d6b) this test fails

Needs a C compiler (CC, default cc). Nothing here needs a Mac or a GPU."""
import argparse
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


loader = source('plugin/NVMTLLibraryLoad.m')
header = source('plugin/nvmtl_spvcache_name.h')
start = loader.index('static int nvmtl_xlate_ready(void) {')
first = loader.index('if (h) nvmtl_translator_cache_tag(', start)
first = loader.rindex('\n', 0, first) + 1
last = loader.index('if (!h) {', first)
last = loader.rindex('\n', 0, last) + 1
statements = loader[first:last]
tags = '\n'.join(re.findall(r'(?m)^static char gXlate(?:Build)?Tag\[[^\n]*;$', loader[:start]))
assert tags.count('static char') == 2, 'the two tag buffers of plugin/NVMTLLibraryLoad.m were not found'
cap = int(re.search(r'#define NVMTL_SPVCACHE_TAG_MAX (\d+)', header).group(1))

HARNESS = r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
/* strlcpy and strlcat as the Mac's libc has them (not every C library has them). */
static size_t test_strlcpy(char *d, const char *s, size_t n)
{
    size_t k = strlen(s);
    if (n) { size_t c = k < n - 1 ? k : n - 1; memcpy(d, s, c); d[c] = 0; }
    return k;
}
static size_t test_strlcat(char *d, const char *s, size_t n)
{
    size_t m = strnlen(d, n);
    return m == n ? n + strlen(s) : m + test_strlcpy(d + m, s, n - m);
}
/* The Mac's string.h may define them as fortified macros: the stand-ins replace those. */
#undef strlcpy
#undef strlcat
#define strlcpy test_strlcpy
#define strlcat test_strlcat
#include "nvmtl_spvcache_name.h"
#define nvlog(...) (fprintf(stderr, __VA_ARGS__), fputc('\n', stderr))
/*TAGS*/
/* The translator's build identity, as nvmtl_translator_cache_tag writes it, comes from the test. */
static void nvmtl_translator_cache_tag(const char *path, char *tag, size_t capacity)
{
    (void)path;
    strlcpy(tag, getenv("TEST_BUILD"), capacity);
}
int nvmtl_typed_dummies_wanted(void) { return getenv("TEST_TYPED_DUMMIES") != NULL; }
static void tag_statements(void *h, const char *xpath)
{
/*STATEMENTS*/
}
int main(int argc, char **argv)
{
    tag_statements(argc > 1 && !strcmp(argv[1], "opened") ? (void *)&argc : NULL, "libnvmtl_translate.dylib");
    printf("%s\n", gXlateTag);
    return 0;
}
'''

fails = 0


def arm(name, ok, detail):
    global fails
    print(('ok   ' if ok else 'FAIL ') + name + f' ({detail})')
    fails += not ok


with tempfile.TemporaryDirectory(prefix='xlate_cache_tag-') as d:
    d = pathlib.Path(d)
    (d / 'nvmtl_spvcache_name.h').write_text(header)
    c, exe = d / 'tag.c', d / 'tag'
    c.write_text(HARNESS.replace('/*TAGS*/', tags).replace('/*STATEMENTS*/', statements))
    cc = os.environ.get('CC', 'cc').split()
    r = subprocess.run(cc + ['-std=gnu11', '-I', str(d), '-o', str(exe), str(c)], capture_output=True, text=True)
    if r.returncode:
        print(r.stderr[-2000:])
        sys.exit('FAIL the tag statements of nvmtl_xlate_ready do not build')
    # No setting of the tester's reaches the statements: each case names the ones it wants.
    clean = {k: v for k, v in os.environ.items() if not k.startswith('NVMTL_') and not k.startswith('TEST_')}
    build = 'sha256-' + '5a' * 32

    def tag(opened=True, whole_views=None, typed_dummies=False, build=build):
        env = dict(clean, TEST_BUILD=build)
        if whole_views is not None:
            env['NVMTL_WHOLE_VIEWS'] = whole_views
        if typed_dummies:
            env['TEST_TYPED_DUMMIES'] = '1'
        out = subprocess.run([str(exe)] + (['opened'] if opened else []), env=env, capture_output=True, check=True)
        return out.stdout.decode().strip(), out.stderr.decode()

    on, _ = tag()
    arm('with NVMTL_WHOLE_VIEWS unset the tag is the build and its mode marks, without -wv0',
        on.startswith(build) and on != build and '-wv0' not in on, on)
    off, _ = tag(whole_views='0')
    arm('NVMTL_WHOLE_VIEWS=0 adds -wv0 to that tag, so the two modes never share a cache entry', off == on + '-wv0', off)
    for value in ('1', '', 'off', ' 0'):
        got, _ = tag(whole_views=value)
        arm(f'NVMTL_WHOLE_VIEWS={value!r} leaves the tag as with the views on', got == on, got)
    got, _ = tag(whole_views='00')
    arm("NVMTL_WHOLE_VIEWS='00' starts with 0, so it is the way back", got == on + '-wv0', got)
    got, _ = tag(whole_views=os.fsdecode(b'0\xff'))
    arm('a value that is not UTF-8 but starts with the byte 0 is the way back, as the translator reads it',
        got == on + '-wv0', got)
    typed, _ = tag(typed_dummies=True)
    got, _ = tag(whole_views='0', typed_dummies=True)
    arm('the mark comes after the typed dummy slots mark (-td1)', typed.endswith('-td1') and got == typed + '-wv0', got)
    got, _ = tag(opened=False, whole_views='0')
    arm('without a translator the tag stays untagged', got == 'untagged', got)
    # A build tag that leaves room for the mode marks but not for -wv0: the tag must not be cut.
    marks = len(on) - len(build)
    longest = 'sha256-' + 'b' * (cap - 3 - marks - len('sha256-'))
    fits, _ = tag(build=longest)
    arm('a long build tag that fits with its mode marks keeps the cache on', fits == longest + on[len(build):], fits)
    got, log = tag(build=longest, whole_views='0')
    arm('when the tag with -wv0 does not fit, the cache is off in that process (untagged) and the log says so',
        got == 'untagged' and 'the shader cache is OFF' in log, got)

print(f'{fails} failed' if fails else 'ok')
sys.exit(1 if fails else 0)
