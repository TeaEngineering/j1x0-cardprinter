"""Read a job file back into commands, for logging and for checking it.

The job is mostly text commands (`ESC`, ASCII, `CR`), with three kinds of binary payload that are
not CR-terminated: the colour planes (5-bit samples packed three to a word), the 1-bit resin bitmap (with `0x5B`
doubled) and the overlay bitmap. Those are walked the same way the encoder wrote them, so a block
that does not decode to the right number of samples is flagged (docs/image-data.md).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .status import show_bytes

ESC = 0x1B
_PLANE = re.compile(rb"(?:\x1b\$F\r)?\x1bGS (\d) (\d+) 0 0 (\d+) (\d+) ")
_BITMAP = re.compile(rb"\x1bF\r\x1bG 0 0 (\d+) (\d+) (\d+) 1\r\x1bZ|\x1bvG 0 0 (\d+) (\d+) (\d+) 1\r\x1bZ")


@dataclass(frozen=True)
class Segment:
    start: int
    end: int
    kind: str                   # "command", "image" or "unknown"
    text: str
    ok: bool = True

    @property
    def length(self) -> int:
        return self.end - self.start


def _plane_bytes(lines: int, dots: int) -> int:
    """Size of a plane payload: per line, ceil(dots / 3) 16-bit words and four zero bytes."""
    return lines * (-(-dots // 3) * 2 + 4)


def _bitmap_bytes(data: bytes, pos: int, target: int) -> tuple[int, int]:
    """Walk a stuffed 1-bit bitmap (0x5B written twice) until `target` bytes; (end, bytes)."""
    count, n = 0, len(data)
    while count < target and pos < n:
        pos += 2 if data[pos] == 0x5B else 1
        count += 1
    return pos, count


def parse_job(data: bytes) -> list[Segment]:
    segments: list[Segment] = []
    i, n = 0, len(data)
    while i < n:
        if data[i] != ESC:
            j = data.find(b"\x1b", i)
            j = n if j < 0 else j
            segments.append(Segment(i, j, "unknown", f"{j - i} unexpected bytes {show_bytes(data[i:j], 16)}", False))
            i = j
            continue
        plane = _PLANE.match(data, i)
        if plane:
            number, levels, lines, dots = (int(g) for g in plane.groups())
            size = _plane_bytes(lines, dots)
            end = plane.end() + size
            closed = end < n and data[end] == 0x0D
            available = min(size, max(n - plane.end(), 0))
            tails_ok = all(data[plane.end() + (k + 1) * (size // lines) - 4:plane.end() + (k + 1) * (size // lines)]
                           == b"\0\0\0\0" for k in range(available // (size // lines)))
            ok = end <= n and closed and tails_ok
            segments.append(Segment(
                i, min(end + (1 if closed else 0), n), "image",
                f"plane {number} ({levels} levels, {lines}x{dots}): {available} of {size} B packed"
                + ("" if closed else ", no CR after the data") + ("" if tails_ok else ", bad line terminator"), ok))
            i = segments[-1].end
            continue
        bitmap = _BITMAP.match(data, i)
        if bitmap:
            groups = [int(g) for g in bitmap.groups() if g is not None]
            height, width = groups[1], groups[2]
            end, got = _bitmap_bytes(data, bitmap.end(), height * width // 8)
            closed = end < n and data[end] == 0x0D
            ok = got == height * width // 8 and closed
            kind = "overlay" if data.startswith(b"\x1bvG", i) else "resin"
            segments.append(Segment(
                i, end + (1 if closed else 0), "image",
                f"{kind} bitmap ({width}x{height}, 1 bit): {end - bitmap.end()} B -> {got} of "
                f"{height * width // 8} bytes" + ("" if closed else ", no CR after the data"), ok))
            i = segments[-1].end
            continue
        j = data.find(b"\r", i)
        if j < 0:
            segments.append(Segment(i, n, "unknown", f"unterminated command {show_bytes(data[i:], 24)}", False))
            break
        segments.append(Segment(i, j + 1, "command", "ESC " + data[i + 1:j].decode("latin-1")))
        i = j + 1
    return segments
