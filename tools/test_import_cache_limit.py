#!/usr/bin/env python3
"""test_import_cache_limit.py: the size limit of the shared family-VRAM import cache gives an 8 GB card the large
card's 64 MB.

The cache (NVMTL_SURF_IMPORT_CACHE=1; off by default) keeps surfaces up to 64 MB on a card with 8 GB of VRAM or more
and up to 16 MB on a smaller one. The limit compared the device-local heap with 8 GiB, but an 8 GB card reports a
little less (an RTX 4060 Ti 8188 MB), so it got the small card's 16 MB and the 4K and 5K surfaces WindowServer
composites were left out. The line is now 7 GiB. From pull request 487 of the original project (dreamelite96).

Compiles nvmtl_ic_env_u64 and nvmtl_ic_max_bytes from plugin/NVMTLDevice.m as they are (with clang's blocks and a
stand-in for dispatch_once) and runs them for several heap sizes and for NVMTL_SURF_IMPORT_CACHE_MAX_MB.

  python3 tools/test_import_cache_limit.py [--baseline [REF]]
      --baseline REF   the sources of that revision (default HEAD); there this test fails (8188 MB gets 16)

Needs clang."""
import argparse
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

root = pathlib.Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument('--baseline', nargs='?', const='HEAD', metavar='REF')
args = parser.parse_args()


def source(path):
    if args.baseline is not None:
        return subprocess.check_output(['git', 'show', f'{args.baseline}:{path}'], cwd=root, text=True)
    return (root / path).read_text()


def function(text, signature):
    start = text.index(signature)
    opening = text.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[start:end]


dev = source('plugin/NVMTLDevice.m')
parts = [function(dev, 'static uint64_t nvmtl_ic_env_u64(const char *name, uint64_t def)'),
         function(dev, 'static uint64_t nvmtl_ic_max_bytes(void)')]
fixture = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
void *_NSConcreteGlobalBlock[32], *_NSConcreteStackBlock[32];   /* clang's blocks, without a runtime */
typedef long dispatch_once_t;
static void dispatch_once(dispatch_once_t *o, void (^b)(void)) { if (!*o) { *o = 1; b(); } }
static uint64_t heap;
static uint64_t nvmtl_vk_vram_bytes(void) { return heap; }
/*UNDER_TEST*/
int main(int argc, char **argv) {
    (void)argc; heap = strtoull(argv[1], NULL, 10) << 20;
    printf("%llu\n", (unsigned long long)(nvmtl_ic_max_bytes() >> 20));
    return 0;
}
'''.replace('/*UNDER_TEST*/', '\n'.join(parts))

compiler = shutil.which('clang')
if not compiler:
    sys.exit('clang is required (the function uses a block).')
cases = [  # heap MiB, NVMTL_SURF_IMPORT_CACHE_MAX_MB, limit MB, what it is
    (8188, None, 64, 'an 8 GB card as it reports itself (RTX 4060 Ti)'),
    (8192, None, 64, 'exactly 8 GiB'),
    (12282, None, 64, 'a 12 GB card'),
    (7168, None, 64, '7 GiB, the line'),
    (6144, None, 16, 'a 6 GB card'),
    (4096, None, 16, 'a 4 GB card (GTX 1650)'),
    (8188, '32', 32, 'the variable wins'),
    (4096, '64', 64, 'the variable wins on a small card too'),
]
fails = 0
with tempfile.TemporaryDirectory() as directory:
    d = pathlib.Path(directory)
    (d / 'limit.c').write_text(fixture)
    b = subprocess.run([compiler, '-std=gnu11', '-fblocks', '-Wall', '-Wno-unused-function', str(d / 'limit.c'), '-o', str(d / 'limit')],
                       capture_output=True, text=True)
    if b.returncode:
        print(b.stderr[-2000:])
        sys.exit('FAIL: the fixture did not compile')
    for heap, env, want, what in cases:
        e = {k: v for k, v in os.environ.items() if k != 'NVMTL_SURF_IMPORT_CACHE_MAX_MB'}
        if env is not None:
            e['NVMTL_SURF_IMPORT_CACHE_MAX_MB'] = env
        got = int(subprocess.run([str(d / 'limit'), str(heap)], capture_output=True, text=True, env=e, check=True).stdout)
        ok = got == want
        fails += not ok
        print(('ok   ' if ok else 'FAIL ') + f'{what}: heap {heap} MB -> surfaces up to {got} MB (want {want})')
print('all passed' if not fails else f'{fails} FAILED')
sys.exit(1 if fails else 0)
