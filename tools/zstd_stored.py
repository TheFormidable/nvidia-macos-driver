#!/usr/bin/env python3
"""zstd_stored.py: standard input as one Zstandard frame of stored (raw) blocks, on standard output.

A frame of raw blocks is Zstandard that every decoder reads, and making one needs no compressor. macOS brings none (no
zstd command, and no Zstandard in a Python before 3.14), and the tests that build a stand-in for NVIDIA's installer,
whose payload is Zstandard, must run on a tester's Mac as it is. frame(data) is the same for a test written in Python.

The format (RFC 8878): the magic number; the frame header, which is a descriptor byte (0xC0: a content size of 8
bytes, a window descriptor, no checksum, no dictionary), the window descriptor (0x38: 128 KiB, the size of the
largest block there is) and the content size; then the blocks, each a little-endian header of 3 bytes (bit 0: the last
block; bits 1 and 2: the type, 0 for raw; the rest: the size) followed by its bytes as they are."""
import struct
import sys

BLOCK = 128 * 1024


def frame(data):
    out = [b"\x28\xb5\x2f\xfd", b"\xc0\x38", struct.pack("<Q", len(data))]
    blocks = max(1, -(-len(data) // BLOCK))          # an empty input is one empty last block
    for i in range(blocks):
        chunk = data[i * BLOCK:(i + 1) * BLOCK]
        out.append(struct.pack("<I", len(chunk) << 3 | (i == blocks - 1))[:3])
        out.append(chunk)
    return b"".join(out)


if __name__ == "__main__":
    sys.stdout.buffer.write(frame(sys.stdin.buffer.read()))
