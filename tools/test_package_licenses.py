#!/usr/bin/env python3
"""The package carries the licence and notice of everything in it, and installs them beside the driver.

The package shipped NVIDIA's GSP firmware, the LGPL shader translator and the driver itself with none of their licence
texts. The release tooling now copies the files below into the package's licenses/ folder and refuses a release when
one is missing; install.sh copies that folder to /Library/NullMoth/licenses and uninstall.sh removes it. A package that
./build.sh makes from a release package puts this checkout's NOTICE and LICENSE in it, not the base release's.
NVK is Mesa's (MIT), but three headers its patches add are the driver's own, under PolyForm: NOTICE and
docs/PROVENANCE.md must name every such file the patches add."""
import pathlib
import re
import sys

root = pathlib.Path(__file__).resolve().parents[1]
fails = 0


def check(name, ok, detail=''):
    global fails
    print(('ok   ' if ok else 'FAIL ') + name + (f' ({detail})' if detail else ''))
    fails += not ok


texts = ['LICENSE', 'NOTICE', 'translator/LICENSE', 'translator/COPYING', 'licenses/NVIDIA-firmware-LICENCE.txt',
         'licenses/NVIDIA-open-gpu-kernel-modules-COPYING.txt', 'licenses/Mesa-license.rst']
missing = [t for t in texts if not (root / t).is_file() or not (root / t).stat().st_size]
check('every licence text the package ships is in the repository', not missing, ', '.join(missing))
firmware = (root / 'licenses/NVIDIA-firmware-LICENCE.txt').read_text(errors='replace') if not missing else ''
check('the firmware licence is NVIDIA\'s', 'License For Customer Use of NVIDIA Software' in firmware)
notice = (root / 'NOTICE').read_text()
check('NOTICE names the NVIDIA release the driver is built on, not an older one',
      '620.70.06' in notice and '615.78.08' not in notice and 'r610' not in notice)
check('NOTICE points at each licence text', all(f in notice for f in ('NVIDIA-firmware-LICENCE.txt',
      'NVIDIA-open-gpu-kernel-modules-COPYING.txt', 'Mesa-license.rst', 'translator/LICENSE')))


def polyform_in_nvk(patches):
    """The files the NVK patches add to Mesa's tree under PolyForm (the driver's own licence), by their Mesa path."""
    out = []
    for text in patches:
        new = None
        for line in text.splitlines():
            if line.startswith('+++ b/'):
                new = line[6:]
            elif line.startswith('+') and 'SPDX-License-Identifier: PolyForm' in line and new:
                out.append(new)
    return sorted(out)


def notice_problems(notice_text, prov_text, files):
    p = []
    nvk = notice_text.split('\nNVK ', 1)[1].split('\n\n', 1)[0] if '\nNVK ' in notice_text else ''
    row = next((l for l in prov_text.splitlines() if l.startswith('| Vulkan driver NVK')), '')
    for f in files:
        name = f.rsplit('/', 1)[1]
        if name not in nvk:
            p.append(f'NOTICE does not name {name} under NVK')
        if name not in row:
            p.append(f'docs/PROVENANCE.md does not name {name} in the NVK row')
    if files and 'PolyForm' not in nvk:
        p.append('NOTICE does not say under which licence they are')
    if files and 'PolyForm' not in row:
        p.append('docs/PROVENANCE.md does not say under which licence they are')
    return p


patches = [f.read_text(errors='replace') for f in sorted((root / 'nvk').glob('*.patch'))]
own = polyform_in_nvk(patches)
prov = (root / 'docs/PROVENANCE.md').read_text()
check('the NVK patches add Mesa files under the driver\'s own licence (the kernel headers NVK shares)', len(own) >= 3, own)
p = notice_problems(notice, prov, own)
check('NOTICE and docs/PROVENANCE.md name each of them and their licence (NVK is not all MIT)', not p, '; '.join(p))
bad = notice_problems(notice.replace('nvrm_fence_abi.h', 'nvrm_fence.h'), prov, own)
check('control: a NOTICE that misses one of them is seen', bool(bad))
extra = own + ['src/nouveau/vulkan/nvkmd/nvrm/nvrm_new_abi.h']
check('control: a new PolyForm file in a patch that the documents do not name is seen', bool(notice_problems(notice, prov, extra)))
local = (root / 'build/package_local.sh').read_text()
check('a package built from this checkout carries its NOTICE and LICENSE in licenses/ (install.sh installs that folder)',
      'put_file "$HERE/NOTICE" licenses/NOTICE' in local and 'put_file "$HERE/LICENSE" licenses/LICENSE' in local)
install = (root / 'package/install.sh').read_text()
check('install.sh installs the licences beside the driver',
      'ditto "$HERE/licenses" /Library/NullMoth/licenses' in install)
uninstall = (root / 'package/uninstall.sh').read_text()
check('uninstall.sh removes them with the driver', re.search(r'rm -rf [^\n]*/Library/NullMoth/licenses', uninstall) is not None)
print(f'{fails} failed')
sys.exit(1 if fails else 0)
