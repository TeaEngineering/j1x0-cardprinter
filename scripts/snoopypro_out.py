"""Pull the bulk-OUT writes out of a SnoopyPro (.usblog) capture.

Each outgoing bulk write is stored as `48 00 09 00`, 16 zero bytes, a 32-bit length, `01 00` and the payload.
Usage: python scripts/snoopypro_out.py capture.usblog [out.bin]   (writes a listing, and the joined stream)
"""

import re
import struct
import sys

HEADER = re.compile(rb"\x48\x00\x09\x00" + b"\x00" * 16 + rb"(.{4})\x01\x00", re.S)


def writes(data: bytes):
    """Yield (file offset, payload) for every bulk-OUT write, in capture order."""
    pos = 0
    while True:
        m = HEADER.search(data, pos)
        if not m:
            return
        length = struct.unpack("<I", m.group(1))[0]
        start = m.end()
        if 0 < length <= 1 << 20 and start + length <= len(data):
            yield m.start(), data[start:start + length]
            pos = start + length
        else:
            pos = m.start() + 1


if __name__ == "__main__":
    data = open(sys.argv[1], "rb").read()
    out = b""
    for n, (offset, payload) in enumerate(writes(data)):
        shown = payload[:40].decode("latin-1").encode("unicode_escape").decode()
        print(f"{n:4d} @{offset:8d}  {len(payload):6d} B  {shown}")
        out += payload
    if len(sys.argv) > 2:
        open(sys.argv[2], "wb").write(out)
        print(f"{len(out)} bytes -> {sys.argv[2]}")
