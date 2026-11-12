#!/usr/bin/env python3
"""The streams and answers of libnvencvt's H.264 decode self-test (vendor/nvenc/src/h264kat_vectors.h) are what their
generator says they are, and the answers are those of a decoder that is not this library.

The card passes the self-test only when its pictures hash to the embedded answers, so a wrong answer would keep a
working decoder off for good, and an answer nobody can reproduce would be a number to take on trust. This checks:

  * the header is exactly what tools/gen_h264_kat.py writes from the header's own streams with this machine's ffmpeg
    decoder (control: with one bit of a stream changed it is not);
  * every frame ffmpeg decodes from each stream is one of the embedded answers, each exactly once, through the same
    hash and matching code the self-test uses (tools/test_h264_kat.c);
  * the streams hold no encoder banner or other text, only parameter sets and slices;
  * when this machine's ffmpeg and x264 are the versions the header records, encoding again gives the same bytes, and
    the pictures x264 itself reconstructed while encoding (its dump-yuv output: an encoder's, not a decoder's) are the
    embedded answers as well, so the answers do not rest on one decoder alone.

Without ffmpeg the test says so and exits 2 (not a pass).

usage: test_h264_kat_vectors.py [repo root]"""
import pathlib, re, shlex, shutil, subprocess, sys, tempfile

root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1])
header = root / 'vendor/nvenc/src/h264kat_vectors.h'
gen = root / 'tools/gen_h264_kat.py'
ctest = root / 'tools/test_h264_kat.c'
fails = 0


def check(name, ok, detail=''):
    global fails
    print(('ok   ' if ok else 'FAIL ') + name + (f' ({detail})' if detail else ''))
    fails += 0 if ok else 1


if not shutil.which('ffmpeg'):
    print('SKIP: ffmpeg not found (it is the independent decoder the answers are checked against)'); sys.exit(2)
cc = shutil.which('cc') or shutil.which('clang') or shutil.which('gcc')
if not cc:
    print('SKIP: no C compiler'); sys.exit(2)

text = header.read_text()
streams = {m.group(1): bytes(int(x) for x in m.group(2).replace('\n', '').split(',') if x.strip())
           for m in re.finditer(r'static const uint8_t kat_(\w+)_bytes\[\d+\] = \{([^}]*)\};', text)}
names = re.findall(r'(?m)^    \{ "(\w+)", ', text)
check('the header holds a stream for each entry of its table', bool(names) and all(n in streams for n in names), ' '.join(names))

with tempfile.TemporaryDirectory(prefix='nmkatv-') as d:
    d = pathlib.Path(d)
    run = lambda *a: subprocess.run([sys.executable, str(gen), *map(str, a)], capture_output=True, text=True)
    r = run('--from', header, '--dump', d, d / 'again.h')
    check('the generator rebuilds the header from the header\'s own streams', r.returncode == 0, (r.stderr or r.stdout).strip()[-300:])
    again = (d / 'again.h').read_text() if (d / 'again.h').exists() else ''
    check('byte for byte: streams, answers and the recorded command lines are the generator\'s', again == text)

    # control: one bit of the first stream's last slice
    first = names[0] if names else ''
    if first:
        bad = bytearray(streams[first]); bad[-20] ^= 0x10
        body = ''.join(','.join(map(str, bad[i:i + 32])) + (',' if i + 32 < len(bad) else '') + '\n' for i in range(0, len(bad), 32))
        changed = re.sub(r'(static const uint8_t kat_%s_bytes\[\d+\] = \{\n)[^}]*(\};)' % first, lambda m: m.group(1) + body + m.group(2), text)
        (d / 'bad.h').write_text(changed)
        r = run('--from', d / 'bad.h', d / 'bad-out.h')
        out = (d / 'bad-out.h').read_text() if r.returncode == 0 else ''
        answers = lambda t: re.findall(r'\{ (0x[0-9a-f]{16})ull, (0x[0-9a-f]{16})ull \}', t)
        check('control: with one bit of a stream changed the answers are no longer the header\'s',
              changed != text and (r.returncode != 0 or answers(out) != answers(text)))

    # the same hash and match code the self-test uses, over ffmpeg's frames
    m = re.search(r'(?m)^\s*cc (.*?) -o \S+ && \S+\s*$', ctest.read_text())
    check('the C test names its build line', m is not None)
    if m and all((d / f'{n}.nv12').exists() for n in names):
        b = subprocess.run([cc, *shlex.split(m.group(1)), '-o', str(d / 't')], cwd=root, capture_output=True, text=True)
        check('the C test builds', b.returncode == 0, b.stderr.strip()[-400:])
        if b.returncode == 0:
            t = subprocess.run([str(d / 't'), *[str(d / f'{n}.nv12') for n in names]], capture_output=True, text=True)
            lines = [l for l in t.stdout.split('\n') if 'the other decoder' in l or 'control: that decoder' in l]
            for l in lines:
                print('  ' + l)
            check('every frame ffmpeg decodes is an embedded answer, each once, and a changed frame is none',
                  t.returncode == 0 and len(lines) == 2 * len(names) and all(l.startswith('ok') for l in lines),
                  '' if t.returncode == 0 else t.stdout.strip()[-300:])

    for n in names:
        s = streams[n]
        types = set()
        for i in range(len(s) - 3):
            if s[i:i + 3] == b'\x00\x00\x01':
                types.add(s[i + 3] & 31)
        check(f'{n}: parameter sets and slices only, and no encoder banner', types == {7, 8, 5, 1} and b'x264' not in s and b'Lavc' not in s,
              ' '.join(map(str, sorted(types))))

    # the encoder: comparable only with the versions the header records
    recorded = [l for l in text.split('\n') if l.startswith('// encoder:')]
    check('the header records the encoder that made the streams', len(recorded) == 1 and 'x264 - core' in recorded[0], ' '.join(recorded))
    r = run(d / 'fresh.h')
    fresh = (d / 'fresh.h').read_text() if r.returncode == 0 and (d / 'fresh.h').exists() else ''
    here = [l for l in fresh.split('\n') if l.startswith('// encoder:')]
    if not fresh:
        print('not compared: this ffmpeg cannot encode the streams again (no libx264?): ' + (r.stderr or r.stdout).strip()[-200:])
    elif here != recorded:
        print(f'not compared: the streams were made with "{recorded[0][12:] if recorded else "?"}", this machine has "{here[0][12:] if here else "?"}"')
    else:
        check('encoding again with the recorded encoder gives the same header', fresh == text)
        # A second opinion on the answers, from something that is not a decoder. x264 predicts every picture from its own
        # reconstruction of the pictures before it, which by the standard is what a decoder must output, and writes
        # those reconstructions to a file when asked (dump-yuv, frames in display order). It can only be asked while
        # it encodes, so this is comparable where the encode above gave the embedded bytes.
        recorded_cmds = re.findall(r'(?m)^// (\w+) \((\d+)x(\d+)\): ffmpeg (.*)$', text)
        files, why = [], '' if (d / 't').exists() else 'the C test was not built'
        for name, w, h, args in recorded_cmds if not why else []:
            w, h, a, yuv = int(w), int(h), args.split(), d / f'{name}-x264.yuv'
            if '-x264-params' not in a:
                why = f'{name}: the recorded command line has no -x264-params'
                break
            a[a.index('-x264-params') + 1] += f':dump-yuv={yuv}'
            e = subprocess.run(['ffmpeg', '-v', 'error', *a, '-'], capture_output=True)
            raw, fs = yuv.read_bytes() if yuv.exists() else b'', w * h * 3 // 2
            if e.returncode or not raw or len(raw) % fs:
                why = f'{name}: this x264 wrote no reconstruction ({len(raw)} bytes)'
                break
            check(f'{name}: the encode that wrote its reconstruction gave the embedded stream', e.stdout == streams.get(name))
            nv12 = bytearray()
            for o in range(0, len(raw), fs):                     # planar 4:2:0 as x264 writes it -> NV12
                uv = bytearray(w * h // 2)
                uv[0::2], uv[1::2] = raw[o + w * h:o + w * h * 5 // 4], raw[o + w * h * 5 // 4:o + fs]
                nv12 += raw[o:o + w * h] + uv
            (d / f'{name}-x264.nv12').write_bytes(nv12)
            files.append(str(d / f'{name}-x264.nv12'))
        if why or [n for n, *_ in recorded_cmds] != names:
            print('not compared: the encoder\'s own reconstruction (' + (why or 'the recorded command lines are not the table\'s streams') + ')')
        else:
            t = subprocess.run([str(d / 't'), *files], capture_output=True, text=True)
            lines = [l for l in t.stdout.split('\n') if 'the other decoder' in l or 'control: that decoder' in l]
            check('the encoder\'s own reconstruction of every frame is an embedded answer too, each once (x264, not a decoder)',
                  t.returncode == 0 and len(lines) == 2 * len(names) and all(l.startswith('ok') for l in lines),
                  '' if t.returncode == 0 else t.stdout.strip()[-300:])

print(f'{fails} failed' if fails else 'all passed')
sys.exit(1 if fails else 0)
