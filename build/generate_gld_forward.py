#!/usr/bin/env python3
"""generate_gld_forward.py <out dir>: gld_forward.inc and gld_forward.S for NVKZinkGLDriver.bundle (zink/gld_bridge.c).

The bundle has to export every gld* function AppleMetalOpenGLRenderer exports, because GLEngine looks each one up by
name in the bundle the accelerator names. The list is the export names of the build Mac's renderer (nm -gU): names
only, nothing about what is behind them. Each export but the three gld_bridge.c defines becomes a jump through
gld_targets[], which the bundle fills with dlsym() of the same name in Apple's renderer when it loads.
"""
from pathlib import Path
import re
import subprocess
import sys

if len(sys.argv) != 2:
    raise SystemExit('usage: generate_gld_forward.py NEW_BUILD_DIRECTORY')
out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
apple = '/System/Library/Extensions/AppleMetalOpenGLRenderer.bundle/Contents/MacOS/AppleMetalOpenGLRenderer'
exports = subprocess.check_output(['nm', '-gU', apple], text=True)
names = sorted(set(re.findall(r'\b_(gld[A-Z]\w*)$', exports, re.M)))
overrides = {'gldGetVersion', 'gldCreateContext', 'gldDestroyContext'}
if not overrides.issubset(names): raise SystemExit('Required lifecycle exports are missing')
header = ['static const char *const gld_names[] = {']
header += ['   "' + name + '",' for name in names]
header += ['};', f'void *gld_targets[{len(names)}];']
header += [f'#define GLD_INDEX_{name} {i}' for i, name in enumerate(names)]
(out / 'gld_forward.inc').write_text('\n'.join(header) + '\n')
asm = ['.text', '.private_extern _gld_targets']
for i, name in enumerate(names):
    if name in overrides: continue
    asm += [f'.globl _{name}', f'_{name}:', f'  jmpq *_gld_targets+{i*8}(%rip)']
(out / 'gld_forward.S').write_text('\n'.join(asm) + '\n')
print(f'Generated {len(names)} GLD exports, including {len(overrides)} lifecycle overrides')
