#!/usr/bin/env python3
# Compiles and runs tools/test_argbuf_elems.c against plugin/nvmtl_argbuf_elems.h (the [[id]]s and handle offsets of an
# argument-buffer array of handles), then checks the wiring in plugin/NVMTLLibraryLoad.m: the AIR walk asks the header
# only for texture, sampler and buffer leaves, keeps element 0 as the field it always added, and the way out
# NVMTL_ARGBUF_ARRAYS=0 is read once and leaves one field per member.
import pathlib, re, subprocess, sys, tempfile

root = pathlib.Path(__file__).resolve().parent.parent
fails = 0
def check(what, ok):
    global fails
    print(('ok    ' if ok else 'FAIL  ') + what)
    if not ok: fails += 1

with tempfile.TemporaryDirectory() as d:
    exe = pathlib.Path(d) / 'test_argbuf_elems'
    b = subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-I', str(root / 'plugin'),
                        str(root / 'tools' / 'test_argbuf_elems.c'), '-o', str(exe)], capture_output=True, text=True)
    check('test_argbuf_elems.c builds without warnings', b.returncode == 0)
    if b.returncode: print(b.stderr)
    else:
        r = subprocess.run([str(exe)], capture_output=True, text=True)
        print(r.stdout, end='')
        check('test_argbuf_elems.c passes', r.returncode == 0)

src = (root / 'plugin' / 'NVMTLLibraryLoad.m').read_text()
check('NVMTLLibraryLoad.m includes the header', '#include "nvmtl_argbuf_elems.h"' in src)
m = re.search(r'static BOOL nvmtl_argbuf_arrays_on\(void\) \{(.*?)\n\}', src, re.S)
check('the way out has its accessor', bool(m))
if m:
    body = m.group(1)
    check('NVMTL_ARGBUF_ARRAYS is read into a static once', 'static int on = -1;' in body and 'if (on < 0)' in body
          and 'getenv("NVMTL_ARGBUF_ARRAYS")' in body)
    check('only the value 0 turns it off', "on = !(e && e[0] == '0');" in body)
w = re.search(r'static void nvmtl_air_argbuf_walk\(.*?\n\}\n', src, re.S)
check('the AIR walk is there', bool(w))
if w:
    walk = w.group(0)
    add0 = walk.find('[out addObject:fld];')
    elems = walk.find('nvmtl_argbuf_handle_elements(')
    check('element 0 is added as before, the other elements after it', 0 <= add0 < elems)
    check('the switch is asked before the header', re.search(r'handle && nvmtl_argbuf_arrays_on\(\) && nvmtl_air_location_pair\(', walk) is not None)
    check('only texture, sampler and buffer leaves are handle arrays',
          '[kind isEqualToString:@"texture"] || [kind isEqualToString:@"sampler"]' in walk and '!\\"air.buffer\\"' in walk)
    check('elements 1..n-1 take consecutive ids at the header\'s stride',
          'for (unsigned k = 1; k < n; k++)' in walk and '@(baseId + (NSUInteger)rel + k)' in walk
          and '@(baseOff + off + (NSUInteger)k * stride)' in walk)
print('FAILED' if fails else 'all ok')
sys.exit(1 if fails else 0)
