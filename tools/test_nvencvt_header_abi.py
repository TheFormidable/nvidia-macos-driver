#!/usr/bin/env python3
"""tools/nvencvt_header_abi.py finds what it must, and build/build_nvencvt.sh runs it before the compiler and obeys it.

libnvencvt is compiled against one version of NVIDIA's headers and talks to a resource manager built from another. The
comparison of the two is only worth having if it looks at the names the library really hands to RM, fails when one of
them changes, and stops the build when it fails. This checks:

  * the tool's own --selftest (its parsing and comparing, on a made-up header);
  * on the library's real source: the structs and constants it is known to hand to RM are in the set that is compared,
    the ones only a DRF macro spells included, so the comparison cannot be empty by accident;
  * the differences it lets pass are exactly the two that were reviewed;
  * the command line on two made-up trees: 0 when they agree; 1 and the name when a struct the source uses differs,
    whichever of the two header folders holds it and also when the change is in a header that only another header
    includes; 0 for a change in something the source does not use (control: 1 with --all); 0 and an "allowed" line for
    an allowed name; 2 for a tree without the headers;
  * the build script, run here with stand-ins for the build Mac's ssh, rsync and compiler: it compares before it
    compiles; a difference, or a tree the tool cannot read, stops it with exit 3 before the compiler runs; it takes the
    second tree when the first is not there; with no tree, or a revision without the tool, it says so and builds; a
    library whose exports are not the four entry points and the marker of the Blackwell self-test stops it;
  * when the real trees are on this machine ($NVENCVT_HEADERS/ogkm, and $NM_OGKM620 or $NM_OGKM620_PRISTINE), the real
    comparison: nothing differs outside the allowed names, and (control) with every name compared the two do differ.

usage: test_nvencvt_header_abi.py [repo root]"""
import importlib.util, os, pathlib, shutil, subprocess, sys, tempfile

root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1])
tool = root / 'tools/nvencvt_header_abi.py'
script = root / 'build/build_nvencvt.sh'
fails = 0


def check(name, ok, detail=''):
    global fails
    print(('ok   ' if ok else 'FAIL ') + name + (f' ({detail})' if detail and not ok else ''))
    fails += 0 if ok else 1


def run(*args, **kw):
    return subprocess.run([sys.executable, str(tool), *map(str, args)], capture_output=True, text=True, **kw)


r = run('--selftest')
check('the tool\'s own selftest passes', r.returncode == 0 and 'FAIL' not in r.stdout and r.stdout.count('ok ') >= 20, r.stdout[-300:])

spec = importlib.util.spec_from_file_location('nvencvt_header_abi', tool)
abi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(abi)

# ---- the library's real source ----
code = abi.source(str(root / 'vendor/nvenc'))
names = abi.names_in(code)
handed = ['NVOS00_PARAMETERS', 'NVOS21_PARAMETERS', 'NVOS46_PARAMETERS', 'NVOS54_PARAMETERS', 'NV_MEMORY_ALLOCATION_PARAMS',
          'NV_VASPACE_ALLOCATION_PARAMETERS', 'NV_CHANNEL_ALLOC_PARAMS', 'NV0080_CTRL_GPU_GET_CLASSLIST_V2_PARAMS',
          'nv_ioctl_card_info_t', 'nv_ioctl_nvos33_parameters_with_fd', 'NvNotification', 'AmpereAControlGPFifo', 'NV_ESC_RM_MAP_MEMORY']
check('the structs and requests the library hands to RM are among the names compared', all(n in names for n in handed),
      ' '.join(n for n in handed if n not in names))
pasted = ['NVOS32_ATTR_LOCATION', 'NVOS32_ATTR_LOCATION_PCI', 'NVOS46_FLAGS_CACHE_SNOOP_ENABLE', 'NVOS33_FLAGS_CACHING_TYPE']
check('so are the fields and values only a DRF macro spells', all(n in names for n in pasted), ' '.join(n for n in pasted if n not in names))
includes = set(abi.INCLUDE.findall(code))
check('the headers are found from the library\'s own includes', {'nvos.h', 'nv-ioctl.h', 'nv_escape.h', 'class/cl0080.h'} <= includes)
check('the differences let pass are the two reviewed ones, each with its reason',
      set(abi.ALLOWED) == {'NVOS33_FLAGS_CACHING_TYPE', 'NVOS33_FLAGS_CACHING_TYPE_WRITECOMBINED'} and all(len(v) > 20 for v in abi.ALLOWED.values()),
      ' '.join(sorted(abi.ALLOWED)))

# ---- the command line, on made-up trees ----
SDK, UNIX = 'src/common/sdk/nvidia/inc', 'src/nvidia/arch/nvalloc/unix/include'
TREE = {
    SDK + '/nvtypes.h': 'typedef unsigned int NvU32;\ntypedef NvU32 NvHandle;\n',
    SDK + '/nvos.h': '#include "nvtypes.h"\n'
                     'typedef struct { NvHandle hRoot; NvU32 hClass; NvU32 status; } NVOS21_PARAMETERS;\n'
                     '#define NVOS33_FLAGS_CACHING_TYPE 25:23\n#define NVOS33_FLAGS_CACHING_TYPE_WRITECOMBINED 2\n'
                     '#define NVOS32_ATTR_LOCATION 26:25\n#define NVOS32_ATTR_LOCATION_PCI 0x1\n'
                     'typedef struct { NvU32 spare; } NVOS99_NOT_USED;\n',
    SDK + '/class/cl0080.h': '#define NV01_DEVICE_0 (0x80U)\n',
    UNIX + '/nv-ioctl.h': '#include <nvtypes.h>\ntypedef struct nv_ioctl_card_info { NvU32 gpu_id; NvU32 flags; } nv_ioctl_card_info_t;\n',
}
SOURCE = ('#include "nvos.h"\n#include <nv-ioctl.h>\n#include <class/cl0080.h>\n'
          'int f(void) { NVOS21_PARAMETERS p = { 0 }; nv_ioctl_card_info_t c; (void)c;\n'
          '  return p.status + NV01_DEVICE_0 + DRF_DEF(OS32, _ATTR, _LOCATION, _PCI) + DRF_VAL(OS33, _FLAGS, _CACHING_TYPE, 0)\n'
          '         + NVOS33_FLAGS_CACHING_TYPE_WRITECOMBINED; }\n')


def tree(folder, changes=()):
    """the made-up tree, with (file, old, new) applied; a file made empty is left out"""
    for name, text in TREE.items():
        for f, old, new in changes:
            if f == name:
                assert old in text, (f, old)
                text = text.replace(old, new)
        if text:
            p = pathlib.Path(folder) / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
    return folder


GREW = (SDK + '/nvos.h', 'NvU32 status; }', 'NvU32 status; NvU32 more; }')
GONE = [(SDK + '/nvos.h', '#define NVOS33_FLAGS_CACHING_TYPE 25:23\n#define NVOS33_FLAGS_CACHING_TYPE_WRITECOMBINED 2\n', '')]

with tempfile.TemporaryDirectory(prefix='nmabi-') as d:
    d = pathlib.Path(d)
    src = d / 'lib'
    (src / 'src').mkdir(parents=True)
    (src / 'src/x.c').write_text(SOURCE)
    ref = tree(d / 'ref')

    def cmp(name, changes, *flags):
        r = run(*flags, '--source', src, ref, tree(d / name, changes))
        return r.returncode, r.stdout

    rc, out = cmp('same', [])
    check('two trees that agree: exit 0, and the names the source uses were compared', rc == 0 and '== 0 differ' in out and 'compared: 4 structs' in out, out[-300:])
    rc, out = cmp('grew', [GREW])
    check('a struct the source hands over grew: exit 1 and its name', rc == 1 and 'type differs: NVOS21_PARAMETERS' in out, out[-300:])
    rc, out = cmp('unix', [(UNIX + '/nv-ioctl.h', 'NvU32 flags; }', 'NvU32 flags; NvU32 more; }')])
    check('so does one in the other header folder', rc == 1 and 'type differs: nv_ioctl_card_info_t' in out, out[-300:])
    rc, out = cmp('deep', [(SDK + '/nvtypes.h', 'typedef NvU32 NvHandle;', 'typedef unsigned long long NvHandle;')])
    check('and a field\'s type, defined in a header only another header includes', rc == 1 and 'type differs: NvHandle' in out, out[-300:])
    rc, out = cmp('value', [(SDK + '/nvos.h', 'LOCATION_PCI 0x1', 'LOCATION_PCI 0x2')])
    check('a value only a DRF macro spells changed: exit 1 and its name', rc == 1 and 'define differs: NVOS32_ATTR_LOCATION_PCI' in out, out[-300:])
    rc, out = cmp('lost', [(SDK + '/class/cl0080.h', '#define NV01_DEVICE_0 (0x80U)\n', '\n')])
    check('a constant the source uses is gone: exit 1 and its name', rc == 1 and 'define missing: NV01_DEVICE_0' in out, out[-300:])
    spare = [(SDK + '/nvos.h', 'NvU32 spare; }', 'NvU32 spare; NvU32 more; }')]
    rc, out = cmp('spare', spare)
    check('a struct the source never names changed: exit 0', rc == 0 and '== 0 differ' in out, out[-300:])
    rc, out = cmp('spare-all', spare, '--all')
    check('control: with every name compared that change is reported', rc == 1 and 'type differs: NVOS99_NOT_USED' in out, out[-300:])
    rc, out = cmp('allowed', GONE)
    check('the reviewed difference: exit 0, shown as allowed with its reason',
          rc == 0 and 'allowed: define missing: NVOS33_FLAGS_CACHING_TYPE (' in out and '== 2 differ, 2 of them allowed' in out, out[-400:])
    rc, out = cmp('allowed-and-more', GONE + [GREW])
    check('an allowed difference does not hide another one', rc == 1 and 'type differs: NVOS21_PARAMETERS' in out and '== 3 differ, 2 of them allowed' in out, out[-300:])
    (d / 'empty' / SDK).mkdir(parents=True)
    r = run('--source', src, ref, d / 'empty')
    check('a tree without the headers: exit 2, not a pass', r.returncode == 2 and 'holds none of the headers' in r.stdout, r.stdout[-300:])
    r = run('--source', src, d / 'empty', ref)
    check('nor is a reference without them', r.returncode == 2, r.stdout[-300:])
    r = run('--source', d / 'empty', ref, ref)
    check('nor a folder that holds no library source', r.returncode == 2 and 'no library source' in r.stdout, r.stdout[-300:])
    r = run(ref)
    check('one tree is not a comparison: exit 2', r.returncode == 2)

    # ---- the build script, with stand-ins for the build Mac ----
    bash, git = shutil.which('bash'), shutil.which('git')
    if not bash or not git:
        print('not run: the build script arms need bash and git')
    else:
        text = script.read_text()
        repo, mac, stubs = d / 'repo', d / 'mac', d / 'stubs'
        for f in ('build/build_nvencvt.sh', 'build/warn_ceiling.sh', 'build/warnings.txt'):
            (repo / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(root / f, repo / f)
        (repo / 'vendor/nvenc/src').mkdir(parents=True)
        (repo / 'vendor/nvenc/src/x.c').write_text(SOURCE)
        stubs.mkdir()
        STUBS = {
            # the build Mac is this machine: its commands run here
            'ssh': 'shift; exec bash -c "$1"',
            'rsync': 'dst=${3#*:}; mkdir -p "$dst" && cp -a "$2". "$dst"',
            'scp': 'shift; n=$#; for f in "${@:1:$((n-1))}"; do cp "${f#*:}" "${!n}" || exit 1; done',
            # the header fetch would reach the network; everything else is git's
            'git': 'for a in "$@"; do case "$a" in clone|fetch|checkout|sparse-checkout) exit 0;; esac; done; exec "$REAL_GIT" "$@"',
            'curl': 'echo "curl must not run here" >&2; exit 97',
            'xcrun': 'echo "the compiler runs"; : > ../out/libnvencvt.dylib',
            'vtool': 'echo "    minos 15.0"',
            # what the library exports: the five symbols of a good build, or what $STUB_EXPORTS says
            'nm': 'for s in ${STUB_EXPORTS:-NVDecVT_CreateInstance NVEncVT_BlackwellSelfTest NVEncVT_CreateInstance NVEncVT_Register '
                  'NVEncVT_RegisterDecoders}; do echo "0000000000001000 T _$s"; done',
            'shasum': 'echo "0123456789abcdef  $3"',
        }
        for name, body in STUBS.items():
            (stubs / name).write_text('#!/bin/bash\n' + body + '\n')
            (stubs / name).chmod(0o755)
        env = {k: v for k, v in os.environ.items() if not k.startswith(('NM_OGKM620', 'NULLMOTH_', 'GIT_'))}
        env.update(PATH=f'{stubs}{os.pathsep}{env.get("PATH", "")}', REAL_GIT=git, NULLMOTH_BUILD_HOST='buildmac', NULLMOTH_BUILD_ROOT=str(mac))
        g = lambda *a: subprocess.run([git, '-C', str(repo), '-c', 'user.name=test', '-c', 'user.email=test@example.invalid',
                                       '-c', 'commit.gpgsign=false', *a], capture_output=True, text=True, env=env)
        made = g('init', '-q').returncode == 0 and g('add', '-A').returncode == 0 and g('commit', '-q', '-m', 'the library').returncode == 0
        old = g('rev-parse', 'HEAD').stdout.strip()
        for f in ('tools/nvencvt_header_abi.py', 'tools/nvk_header_abi.py'):
            (repo / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(root / f, repo / f)
        made = made and g('add', '-A').returncode == 0 and g('commit', '-q', '-m', 'the comparison').returncode == 0
        check('a small repository with the build script, the library source and the tool was made', made and len(old) == 40)
        K1, K2 = mac / 'build/rm620/ogkm-620.70.06', mac / 'build/ogkm620/open-gpu-kernel-modules-620.70.06'
        HDR = mac / 'build/nvencvt-headers'

        def build(first=None, second=None, rev='HEAD', extra=None, hollow=False):
            """run the script with the made-up trees `first` and `second` (lists of changes, or None for no tree) where
            it looks for the kernel module's; hollow: the first is only its empty header folder -> (exit code, output)"""
            shutil.rmtree(mac, ignore_errors=True)
            tree(HDR / 'ogkm')
            for sub in ('ogkm/.git', 'ogd/.git', 'webkit-spi'):
                (HDR / sub).mkdir(parents=True, exist_ok=True)
            for f in ('CMBaseObjectSPI.h', 'VTVideoDecoderSPI.h'):
                (HDR / 'webkit-spi' / f).write_text('// stand-in\n')
            for where, changes in ((K1, first), (K2, second)):
                if changes is not None:
                    tree(where, changes)
            if hollow:
                (K1 / SDK).mkdir(parents=True)
            r = subprocess.run([bash, str(repo / 'build/build_nvencvt.sh'), rev, str(d / 'out')], capture_output=True, text=True,
                               env=dict(env, **(extra or {})), cwd=str(d))
            return r.returncode, r.stdout + r.stderr

        compiled = lambda out: 'the compiler runs' in out
        rc, out = build(first=[])
        check('the trees agree: the names are compared, then the compiler runs, and the build ends well',
              rc == 0 and '== 0 differ' in out and compiled(out) and out.index('== 0 differ') < out.index('the compiler runs')
              and str(K1) in out and 'libnvencvt ' in out.split('\n')[-2], out[-500:])
        rc, out = build(first=[GREW])
        check('a struct the library hands over differs: exit 3, the name is shown, and the compiler never runs',
              rc == 3 and 'type differs: NVOS21_PARAMETERS' in out and 'STOP: the header comparison failed (exit 1' in out and not compiled(out), out[-500:])
        rc, out = build(first=GONE)
        check('the reviewed difference does not stop the build', rc == 0 and '2 of them allowed' in out and compiled(out), out[-500:])
        rc, out = build(second=[GREW])
        check('without the first tree the second is compared (and stops the build the same way)',
              rc == 3 and str(K2) in out and 'type differs: NVOS21_PARAMETERS' in out and not compiled(out), out[-500:])
        rc, out = build(first=[], second=[GREW])
        check('with both, the tree the kernel module is built from is the one compared', rc == 0 and str(K1) in out and str(K2) not in out and compiled(out), out[-500:])
        rc, out = build()
        check('with neither tree the build says the comparison was skipped, names where it looked, and goes on',
              rc == 0 and 'header comparison SKIPPED: no open-gpu-kernel-modules 620.70.06 tree at' in out and str(K1) in out and str(K2) in out
              and compiled(out) and 'differ' not in out, out[-500:])
        rc, out = build(hollow=True)
        check('a tree that is there but holds none of the headers: exit 3, not a build without a comparison',
              rc == 3 and 'holds none of the headers' in out and 'STOP: the header comparison failed (exit 2' in out and not compiled(out), out[-500:])
        rc, out = build(first=[], rev=old)
        check('a revision without the tool: the build says the comparison was skipped and goes on',
              rc == 0 and f'header comparison SKIPPED: {old} has no tools/nvencvt_header_abi.py' in out and compiled(out) and 'differ' not in out, out[-500:])
        other = d / 'elsewhere'
        tree(other, [GREW])
        rc, out = build(first=[], extra={'NM_OGKM620': str(other)})
        check('$NM_OGKM620 names the tree, as in the release build', rc == 3 and str(other) in out and not compiled(out), out[-500:])
        entry = 'NVDecVT_CreateInstance NVEncVT_CreateInstance NVEncVT_Register NVEncVT_RegisterDecoders'
        rc, out = build(first=[], extra={'STUB_EXPORTS': entry})
        check('a library without the marker of the Blackwell self-test is not a build: the script stops and shows the exports',
              rc == 1 and 'STOP: exports are [' in out and 'libnvencvt ' not in out.split('\n')[-2], out[-300:])
        rc, out = build(first=[], extra={'STUB_EXPORTS': 'NVDecVT_CreateInstance NVEncVT_BlackwellSelfTest NVEncVT_CreateInstance NVEncVT_Other NVEncVT_Register NVEncVT_RegisterDecoders'})
        check('nor is one that lets another symbol out', rc == 1 and 'STOP: exports are [' in out, out[-300:])
        check('the script compares before the compile line and stops with its own exit code',
              0 < text.find('tools/nvencvt_header_abi.py --source') < text.find('xcrun clang') and 'exit 3' in text)

# ---- the real trees, when this machine has them ----
hdr = os.environ.get('NVENCVT_HEADERS') or os.path.expanduser('~/nvencvt-headers')
real = [t for t in (os.environ.get('NM_OGKM620'), os.environ.get('NM_OGKM620_PRISTINE')) if t and os.path.isdir(os.path.join(t, SDK))]
if not os.path.isdir(os.path.join(hdr, 'ogkm', SDK)) or not real:
    print('not run: the comparison of the real headers (set NVENCVT_HEADERS to the folder that holds ogkm/ at 610.57.04, '
          'and NM_OGKM620 or NM_OGKM620_PRISTINE to a 620.70.06 tree)')
else:
    r = run(os.path.join(hdr, 'ogkm'), *real)
    print('\n'.join('  ' + l[:160] for l in r.stdout.split('\n') if l.startswith(('reference', '   compared', '=='))))
    counts = [l.split() for l in r.stdout.split('\n') if l.startswith('   compared:')]
    check('the real headers: nothing the library uses differs outside the allowed names', r.returncode == 0 and len(counts) == len(real), r.stdout[-600:])
    check('and that is a comparison of something: dozens of types and defines each',
          bool(counts) and all(int(c[1]) >= 30 and int(c[-2]) >= 60 for c in counts), str(counts))
    r = run('--all', os.path.join(hdr, 'ogkm'), real[0])
    check('control: with every name compared, the two versions do differ', r.returncode == 1 and 'differs' in r.stdout)

print(f'{fails} failed' if fails else 'all passed')
sys.exit(1 if fails else 0)
