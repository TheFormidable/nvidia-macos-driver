#!/usr/bin/env python3
"""test_translator_whole_views.py: the translator retypes a local variable only through a pointer proven to address the
whole variable, and a helper that views a pointer-free local both as its aggregate and as bytes gets one byte array of
the local's size; with NVMTL_WHOLE_VIEWS=0 it infers what 3.0.4 inferred. The rules and their unit tests are from pull
request 524 of the original project (emretascii; its translator parts T3, for League of Legends, and the byte-view
merge of T4, for the iOS Simulator).

Runs the translator's own unit tests of those rules with cargo, on the CPU: native::ir::whole_view_tests (the tests of
the pull request), native::ir::whole_view_way_back_tests (the switch at 0 gives the inference of 3.0.4) and
native::view_switches. Nothing here needs a Mac or a GPU.

  python3 tools/test_translator_whole_views.py [--baseline [REF]] [--target-dir DIR]
      --baseline REF   the translator of that revision (default HEAD) with this tree's whole_view_tests.rs added to
                       it; on 3.0.4 (e3ea9d6b) 8 of its 21 tests fail, so this test fails
      --target-dir DIR cargo's build folder (default: CARGO_TARGET_DIR when it is set, else translator/translator/target
                       of this tree, which cargo would use anyway, so a second run only builds what changed)

A baseline is built in a cargo build folder of its own, DIR/whole-views-baseline, never in DIR itself. Cargo gives the
translator's test binary the same name for every copy of the tree and judges it fresh by file times, so a baseline
built into DIR would be run again, unchanged, by the next plain run and by a plain cargo test of this tree. For the same
reason the files of the baseline get the current time when they are unpacked: git archive gives them the time of
their commit, which is older than a binary of another revision already in the baseline's folder.

Needs cargo and the translator's crates already in cargo's cache (it runs with --offline); without them it is skipped
(exit 2 and SKIP, as tools/run_all_tests.py counts it). A first build of the translator's tests takes some minutes;
cargo is stopped after 540 s, before the runner's own limit, so that Cargo.lock is always put back as it was.

The cargo is the tester's own, found on PATH: it uses the tester's cargo home (its crate cache and lock file there) and
builds into translator/translator/target of this tree. A tester who wants neither sets CARGO_HOME to an empty folder,
and the test is skipped."""
import argparse
import io
import os
import pathlib
import shutil
import subprocess
import sys
import tarfile
import tempfile

root = pathlib.Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument('--baseline', nargs='?', const='HEAD', metavar='REF',
                    help='test the translator of this Git revision (default: HEAD)')
parser.add_argument('--target-dir', metavar='DIR', help="cargo's build folder")
args = parser.parse_args()

if not shutil.which('cargo'):
    print('SKIP: no cargo on PATH')
    sys.exit(2)

TESTS = 'translator/translator/src/native/ir/whole_view_tests.rs'
MOD = 'translator/translator/src/native/ir/mod.rs'
target = args.target_dir or os.environ.get('CARGO_TARGET_DIR') or root / 'translator/translator/target'
target = pathlib.Path(target).resolve()
if args.baseline:
    target = target / 'whole-views-baseline'
# The switch is read from the environment; the tests of the pull request hold with it unset (on).
env = {k: v for k, v in os.environ.items() if k != 'NVMTL_WHOLE_VIEWS'}
env.update(CARGO_TARGET_DIR=str(target))


# tools/run_all_tests.py stops a test with everything it started after 600 s, and a test stopped that way never reaches
# the lines below that put Cargo.lock back. So cargo is stopped here first, and that is a failure. Its compilers end
# with it or are stopped by the runner when this test ends: they are in this test's session.
CARGO_LIMIT = 540


def run(tree, filters):
    cmd = ['cargo', 'test', '--offline', '--lib', '--manifest-path', str(tree / 'translator/translator/Cargo.toml'),
           '--'] + filters
    try:
        return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=CARGO_LIMIT)
    except subprocess.TimeoutExpired as e:
        def text(x):
            return x.decode(errors='replace') if isinstance(x, bytes) else (x or '')
        return subprocess.CompletedProcess(cmd, 124, text(e.stdout),
                                           text(e.stderr) + f'\nno result from cargo after {CARGO_LIMIT} s: stopped')


with tempfile.TemporaryDirectory(prefix='whole_views-') as tmp:
    if args.baseline:
        tree = pathlib.Path(tmp)
        archive = subprocess.run(['git', 'archive', args.baseline, 'translator/translator'], cwd=root,
                                 capture_output=True, check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as t:
            if hasattr(tarfile, 'data_filter'):
                t.extractall(tree, filter='data')
            else:
                t.extractall(tree)
        for path in tree.rglob('*'):
            if not path.is_symlink():
                os.utime(path)
        (tree / TESTS).write_text((root / TESTS).read_text())
        mod = (tree / MOD).read_text()
        if 'mod whole_view_tests;' not in mod:
            anchor = 'mod static_init;\n'
            assert anchor in mod, f'{MOD} of {args.baseline} has no line {anchor!r} to add the tests after'
            (tree / MOD).write_text(mod.replace(anchor, anchor + '#[cfg(test)]\nmod whole_view_tests;\n', 1))
        filters = ['native::ir::whole_view_tests']
    else:
        tree = root
        filters = ['native::ir::whole_view_tests', 'native::ir::whole_view_way_back_tests', 'native::view_switches']
    # cargo rewrites a Cargo.lock that lists more crates than this build needs: the file is put back as it was.
    lock = tree / 'translator/translator/Cargo.lock'
    kept = lock.read_bytes() if lock.exists() else None
    try:
        r = run(tree, filters)
    finally:
        if kept is not None and lock.read_bytes() != kept:
            lock.write_bytes(kept)
    out = r.stdout + r.stderr
    built = 'Compiling metal2vulkan' in out or 'Running ' in out or 'test result' in out
    if r.returncode and not built and '--offline' in out:
        print("SKIP: cargo cannot build the translator offline (its crates are not in cargo's cache): "
              + (out.strip().splitlines() or [''])[-1][:200])
        sys.exit(2)
    passed = [line for line in out.splitlines() if line.startswith('test ') and line.endswith(' ok')]
    failed = [line for line in out.splitlines() if line.startswith('test ') and line.endswith('FAILED')]
    where = f'the translator of {args.baseline}' if args.baseline else 'this tree'
    if r.returncode or failed or not passed:
        print(f'FAIL: {where}: {len(failed)} failed, {len(passed)} passed')
        for line in failed:
            print('  ' + line)
        if not failed:
            print(out[-3000:])
        sys.exit(1)
    print(f'ok: {where}: {len(passed)} tests passed')
