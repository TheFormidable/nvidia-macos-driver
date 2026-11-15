#!/usr/bin/env python3
"""Framebuffer-fetch reads of A8 colour attachments, remapped in SPIR-V by plugin/nvmtl_a8_fetch.h, run on the host.

A hand-written fragment module reads two input attachments (bindings 192 and 193 of set 3, as the translator binds
[[color(0)]] and [[color(1)]]) and adds them. For each mask of A8 attachments, exactly the reads of those attachments get
an OpVectorShuffle that gives Metal's (0,0,0,r), with fresh result ids and the bound raised to match; a mask of no read
attachment changes nothing; a binding in another set is not an input attachment; the input module is left as it was;
every truncated length and eight corrupted words are refused or handled without reading past the module (built with
AddressSanitizer). The set and binding come from plugin/nvmtl_vk.c and NVMTL_NCOL from plugin/nvmtl_vk.h, so the test
follows the values the plugin uses. A control compiles the header with its reads left uncounted and must fail.

From the HighDelay/nvidia-macos-driver-nvk fork."""
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
HEADER = (ROOT / 'plugin/nvmtl_a8_fetch.h').read_text()
VK = (ROOT / 'plugin/nvmtl_vk.c').read_text()
VKH = (ROOT / 'plugin/nvmtl_vk.h').read_text()


def define(text, name):
    m = re.search(r'^#define ' + name + r' (\d+)\b', text, re.M)
    if not m:
        print('FAIL: ' + name + ' is not defined where this test looks for it')
        sys.exit(1)
    return '#define %s %s' % (name, m.group(1))


DEFINES = '\n'.join((define(VK, 'NVMTL_FETCH_SET'), define(VK, 'NVMTL_FETCH_BINDING'), define(VKH, 'NVMTL_NCOL')))
if '#include "nvmtl_a8_fetch.h"' not in VK:
    print('FAIL: plugin/nvmtl_vk.c does not include nvmtl_a8_fetch.h')
    sys.exit(1)
FIXTURE = r'''
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
@@DEFINES@@
#include "nvmtl_a8_fetch.h"
#define I(op,n) (((n)<<16)|(op))
static const uint32_t module[] = {
    0x07230203,0x00010300,0,27,0,
    I(17,2),1, I(17,2),40, I(14,3),0,1,
    I(15,6),4,20,0x6e69616d,0,12, I(16,3),20,7,
    I(71,4),10,34,3, I(71,4),10,33,192, I(71,4),10,43,0,
    I(71,4),11,34,3, I(71,4),11,33,193, I(71,4),11,43,1,
    I(71,4),12,30,0,
    I(19,2),1, I(33,3),2,1, I(22,3),3,32,
    I(23,4),4,3,4, I(21,4),5,32,1, I(23,4),6,5,2,
    I(25,9),7,3,6,0,0,0,2,0, I(32,4),8,0,7, I(32,4),9,3,4,
    I(59,4),8,10,0, I(59,4),8,11,0, I(59,4),9,12,3,
    I(43,4),5,13,0, I(44,5),6,14,13,13,
    I(54,5),1,20,0,2, I(248,2),21,
    I(61,4),7,22,10, I(61,4),7,23,11,
    I(98,5),4,24,22,14, I(98,5),4,25,23,14,
    I(129,5),4,26,24,25, I(62,3),12,26, I(253,1), I(56,1)
};
static void verify(const uint32_t *input, size_t bytes, unsigned mask, unsigned expected) {
    uint32_t *out = (void *)1; size_t len = 1;
    int rc = nvmtl_spirv_a8_fetch(input,bytes,mask,&out,&len);
    assert(rc == (expected ? 1 : 0));
    if (!expected) { assert(out == NULL && !len); return; }
    assert(len == bytes + 36*expected && out[3] == input[3]+expected);
    unsigned shuffles=0,reads=0;
    for(size_t i=5;i<len/4;i+=out[i]>>16) {
        unsigned op=out[i]&65535;
        if(op==98) reads++;
        if(op==79) {
            assert((out[i]>>16)==9 && out[i+1]==4);
            assert(out[i+2]==24 || out[i+2]==25);
            assert(mask & (1u<<(out[i+2]-24)));
            assert(out[i+3]>=input[3] && out[i+3]==out[i+4]);
            assert(out[i+5]==1 && out[i+6]==1 && out[i+7]==1 && out[i+8]==0);
            shuffles++;
        }
    }
    assert(reads==2 && shuffles==expected); free(out);
}
int main(void) {
    uint32_t copy[sizeof module/4]; memcpy(copy,module,sizeof copy);
    verify(module,sizeof module,0,0); verify(module,sizeof module,1,1);
    verify(module,sizeof module,2,1); verify(module,sizeof module,3,2);
    verify(module,sizeof module,4,0); assert(!memcmp(copy,module,sizeof copy));
    for(size_t i=5;i<sizeof copy/4;i+=copy[i]>>16)
        if((copy[i]&65535)==71 && copy[i+1]==10 && copy[i+2]==34) copy[i+3]=2;
    verify(copy,sizeof copy,1,0); verify(copy,sizeof copy,3,1);
    for(size_t bytes=0;bytes<sizeof module;bytes++) {
        uint32_t *out=NULL;size_t len=0;
        int rc=nvmtl_spirv_a8_fetch(module,bytes,3,&out,&len);
        assert(rc>=-1 && rc<=1);free(out);
    }
    const unsigned positions[]={0,3,5,7,9,17,21,25};
    for(unsigned i=0;i<sizeof positions/sizeof *positions;i++) {
        memcpy(copy,module,sizeof copy);copy[positions[i]]=0xffffffff;
        uint32_t *out=NULL;size_t len=0;
        int rc=nvmtl_spirv_a8_fetch(copy,sizeof copy,3,&out,&len);
        assert(rc>=-1 && rc<=1);free(out);
    }
    puts("PASS: A8 attachment selection, RGBA isolation, immutable input, malformed/truncated modules");
}
'''
COUNT = '                count++;'


def compiler():
    if shutil.which('xcrun'):
        return ['xcrun', 'clang']
    for cc in ('cc', 'clang', 'gcc'):
        if shutil.which(cc):
            return [cc]
    return None


def run(directory, header, name):
    d = pathlib.Path(directory) / name
    d.mkdir()
    (d / 'nvmtl_a8_fetch.h').write_text(header)
    (d / 'test.c').write_text(FIXTURE.replace('@@DEFINES@@', DEFINES))
    r = subprocess.run(compiler() + ['-std=c11', '-O1', '-g', '-fsanitize=address,undefined', '-fno-sanitize-recover=all',
                                      '-I' + str(d), str(d / 'test.c'), '-o', str(d / 'test')], capture_output=True, text=True)
    if r.returncode:
        return None, r.stderr[-2500:]
    p = subprocess.run([str(d / 'test')], capture_output=True, text=True, timeout=60)
    return p.returncode, (p.stdout + p.stderr)[-1500:]


def main():
    if not compiler():
        print('SKIP: no C compiler')
        return 2
    if COUNT not in HEADER:
        print('FAIL: nvmtl_spirv_a8_fetch does not count the reads the way this test\'s control expects')
        return 1
    with tempfile.TemporaryDirectory(prefix='nvmtl-a8-fetch-') as d:
        rc, out = run(d, HEADER, 'current')
        print(out.strip())
        if rc is None or rc:
            print('FAIL: the remap failed a case' if rc else 'FAIL: the fixture does not compile')
            return 1
        rc, out = run(d, HEADER.replace(COUNT, '                (void)0;', 1), 'control')
        if rc is None or rc == 0:
            print('FAIL: control: a remap that counts no read was not refused' + (('\n' + out) if rc is None else ''))
            return 1
        print('ok   control: a remap that counts no read fails')
    return 0


if __name__ == '__main__':
    sys.exit(main())
