#!/usr/bin/env python3
"""gen_h264_kat.py - writes vendor/nvenc/src/h264kat_vectors.h: the streams and the answers of the H.264 decode
self-test that libnvencvt runs on the card before it registers a decoder (vendor/nvenc/src/h264kat.c).

A stream is a few pictures of a synthetic test source, encoded here with ffmpeg's libx264 and reduced to parameter sets
and slices (SEI and access unit delimiters removed, so the embedded bytes carry no encoder banner). The answer is one
pair of FNV-1a 64 hashes per frame, luma and interleaved chroma of the cropped picture, taken from ffmpeg's own H.264
decoder. H.264 decoding is exact by specification: every conforming decoder gives these bytes, so the hashes are the
reference for the card and do not depend on which decoder made them.

usage: gen_h264_kat.py [--from <existing header>] [--dump <dir>] <out.h>
  --from   take the streams from an existing header instead of encoding them. The encoder's output depends on the
           x264 version; the decoder's does not, so this must reproduce the header byte for byte with any ffmpeg.
  --dump   also write <dir>/<name>.h264 (the stream) and <dir>/<name>.nv12 (the decoded frames, NV12, display order)

Adding a vector is one entry in VECTORS; a frame size must be even, a stream at most 32 frames, and no two frames of a
stream may be the same picture (the self-test matches frames as a set)."""
import re
import subprocess
import sys

# name, width, height, the ffmpeg arguments between "ffmpeg" and the output options
VECTORS = [
    # High profile, CABAC, 8x8 transform, two slices a picture, weighted prediction, B pictures of which some are
    # references (display order I B B P B B P P), picture order count type 0; 192 coded rows cropped to 180
    ("a", 320, 180,
     "-f lavfi -i testsrc2=size=320x180:rate=30 -frames:v 8 -pix_fmt yuv420p -c:v libx264 -profile:v high "
     "-preset medium -bf 2 -refs 3 -g 8 -threads 1 -x264-params cabac=1:8x8dct=1:slices=2:keyint=8:scenecut=0"),
    # a full-size picture: I P P (scenecut=0, or the rotating source makes the encoder choose three I pictures),
    # picture order count type 2, 1088 coded rows cropped to 1080. Every colour and coordinate of the source is
    # given: left to their defaults they are random, whatever the seed
    ("b", 1920, 1080,
     "-f lavfi -i gradients=size=1920x1080:rate=30:nb_colors=4:c0=0x203040:c1=0xe0c040:c2=0x40a0e0:c3=0xd04060:"
     "x0=200:y0=100:x1=1700:y1=1000:type=spiral:speed=0.05:seed=1 -frames:v 3 -pix_fmt yuv420p -c:v libx264 "
     "-profile:v high -preset medium -bf 0 -refs 1 -g 3 -crf 30 -threads 1 -x264-params scenecut=0"),
]
# what is left in a stream: sequence and picture parameter sets and slices
STRIP = "-bsf:v filter_units=remove_types=6|9 -f h264"
FNV0, FNVP, M64 = 0xcbf29ce484222325, 0x100000001b3, 0xffffffffffffffff
PER_LINE = 32


def fnv(h, data):
    for x in data:
        h = ((h ^ x) * FNVP) & M64
    return h


def run(args, **kw):
    return subprocess.run(args, capture_output=True, check=True, **kw).stdout


def encode(args):
    return run(["ffmpeg", "-v", "error"] + args.split() + STRIP.split() + ["-"])


def encoder_banner(args):
    """'x264 - core N rM hash' as libx264 writes it into the stream's first SEI (the SEI the vectors do not keep)"""
    raw = run(["ffmpeg", "-v", "error"] + args.split() + ["-f", "h264", "-"])
    m = re.search(rb"x264 - core \d+[ -~]*? - H\.264", raw)
    return m.group(0)[:-len(" - H.264")].decode() if m else "x264 (version not found in the stream)"


def decode_nv12(stream, w, h):
    """the frames ffmpeg's H.264 decoder outputs, as NV12 (the decoder's planar 4:2:0 with the chroma interleaved here,
    so no scaler is involved)"""
    raw = run(["ffmpeg", "-v", "error", "-threads", "1", "-f", "h264", "-i", "-", "-pix_fmt", "yuv420p",
               "-f", "rawvideo", "-"], input=stream)
    fs = w * h * 3 // 2
    if not raw or len(raw) % fs:
        sys.exit("the decoder returned %d bytes, not whole %dx%d frames" % (len(raw), w, h))
    frames = []
    for i in range(0, len(raw), fs):
        y, u, v = raw[i:i + w * h], raw[i + w * h:i + w * h * 5 // 4], raw[i + w * h * 5 // 4:i + fs]
        uv = bytearray(w * h // 2)
        uv[0::2], uv[1::2] = u, v
        frames.append((y, bytes(uv)))
    return frames


def parse_header(text):
    """-> ({name: stream bytes}, [the comment lines that describe the encoder])"""
    streams = {m.group(1): bytes(int(x) for x in m.group(2).replace("\n", "").split(",") if x.strip())
               for m in re.finditer(r"static const uint8_t kat_(\w+)_bytes\[\d+\] = \{([^}]*)\};", text)}
    return streams, [l for l in text.split("\n") if l.startswith("// encoder:")]


def main(argv):
    src = dump = None
    while argv and argv[0] in ("--from", "--dump"):
        if len(argv) < 2:
            sys.exit(__doc__)
        if argv[0] == "--from":
            src = argv[1]
        else:
            dump = argv[1]
        argv = argv[2:]
    if len(argv) != 1:
        sys.exit(__doc__)
    old, enc_lines = parse_header(open(src).read()) if src else ({}, [])
    if not src:
        version = run(["ffmpeg", "-version"]).decode().split("\n")[0].split(" Copyright")[0]
        enc_lines = ["// encoder: %s, %s" % (version, encoder_banner(VECTORS[0][3]))]
    out = [
        "// h264kat_vectors.h: the streams and the answers of the H.264 decode self-test (h264kat.c). Generated by",
        "// tools/gen_h264_kat.py; do not edit, run it again. Each stream is a few pictures of a synthetic test source,",
        "// encoded with ffmpeg's libx264 and reduced to parameter sets and slices. Each frame's answer is the FNV-1a 64 hash",
        "// of its cropped luma rows and of its cropped interleaved chroma rows as ffmpeg's H.264 decoder outputs them, in",
        "// display order. H.264 decoding is exact by specification, so any conforming decoder gives the same bytes.",
    ] + enc_lines
    for name, w, h, args in VECTORS:
        out.append("// %s (%dx%d): ffmpeg %s %s" % (name, w, h, args, STRIP))
    out += ["#pragma once", '#include "h264kat_core.h"']
    table = []
    for name, w, h, args in VECTORS:
        if w % 2 or h % 2:
            sys.exit("%s: the frame size must be even" % name)
        if src and name not in old:
            sys.exit("%s has no stream named %s" % (src, name))
        stream = old[name] if src else encode(args)
        frames = decode_nv12(stream, w, h)
        hashes = [(fnv(FNV0, y), fnv(FNV0, uv)) for y, uv in frames]
        if len(frames) > 32:
            sys.exit("%s: %d frames, the self-test matches at most 32 a stream" % (name, len(frames)))
        if len(set(hashes)) != len(hashes):
            sys.exit("%s: two frames are the same picture, a set match would be ambiguous" % name)
        if dump:
            open("%s/%s.h264" % (dump, name), "wb").write(stream)
            open("%s/%s.nv12" % (dump, name), "wb").write(b"".join(y + uv for y, uv in frames))
        out.append("static const uint8_t kat_%s_bytes[%d] = {" % (name, len(stream)))
        out += [",".join(map(str, stream[i:i + PER_LINE])) + ("," if i + PER_LINE < len(stream) else "")
                for i in range(0, len(stream), PER_LINE)]
        out.append("};")
        out.append("static const KatFrame kat_%s_frames[%d] = {" % (name, len(hashes)))
        out += ["    { 0x%016xull, 0x%016xull }," % f for f in hashes]
        out.append("};")
        table.append('    { "%s", %d, %d, kat_%s_bytes, sizeof kat_%s_bytes, kat_%s_frames, %d },'
                     % (name, w, h, name, name, name, len(hashes)))
    out += ["static const KatVector kat_vectors[%d] = {" % len(table)] + table + ["};"]
    open(argv[0], "w").write("\n".join(out) + "\n")
    print("%s: %s" % (argv[0], ", ".join("%s %dx%d" % v[:3] for v in VECTORS)))


if __name__ == "__main__":
    main(sys.argv[1:])
