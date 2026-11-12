"""Pixel data -> printer payloads (docs/image-data.md).

Colour planes carry a 5-bit dye density per dot, run-length encoded. The black resin panel is
a 1-bit bitmap. Both are streamed as 1024 head lines of 640 dots.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

LINES = 1024        # head lines per card (long edge)
DOTS = 640          # dots per head line (short edge)
LEVELS = 32         # density levels named in the plane header


@dataclass(frozen=True)
class Orientation:
    """How an upright (DOTS rows x LINES columns) card image maps onto the stream.

    UNVERIFIED (docs/open-questions.md Q3). The default makes stream line i the image
    column i and dot j the image row j. The Windows driver's default turns the page 180
    degrees relative to its surface, so `Orientation(flip_lines=True, flip_dots=True)` is the
    other obvious candidate; settle it with an asymmetric test card.
    """

    flip_lines: bool = False
    flip_dots: bool = False

    def apply(self, image: np.ndarray) -> np.ndarray:
        """(DOTS, LINES) array -> (LINES, DOTS) array in stream order."""
        if image.shape[:2] != (DOTS, LINES):
            raise ValueError(f"expected {(DOTS, LINES)} image, got {image.shape[:2]}")
        lines = image.T
        if self.flip_lines:
            lines = lines[::-1]
        if self.flip_dots:
            lines = lines[:, ::-1]
        return np.ascontiguousarray(lines)


# ── Colour preparation ───────────────────────────────────────────────────────

def sharpen(channel: np.ndarray) -> np.ndarray:
    """The driver's 3x3 sharpen (interior pixels only): v += clamp((12c - 2*N4 - D4)*5/16, +-70)."""
    c = channel.astype(np.int32)
    out = c.copy()
    centre = c[1:-1, 1:-1]
    n4 = c[:-2, 1:-1] + c[2:, 1:-1] + c[1:-1, :-2] + c[1:-1, 2:]
    d4 = c[:-2, :-2] + c[:-2, 2:] + c[2:, :-2] + c[2:, 2:]
    t = (12 * centre - 2 * n4 - d4) * 5 + 8
    t = (t + (t < 0) * 15) >> 4                     # divide by 16, rounding toward zero
    out[1:-1, 1:-1] = centre + np.clip(t, -70, 70)
    return out


def quantise16(channel: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Stochastic rounding to the 16 levels 0, 16, ... 240, as the driver does."""
    v = np.clip(channel, 0, 255).astype(np.int32)
    q = v // 17 + (rng.integers(0, 17, size=v.shape) < v % 17)
    return (q << 4).astype(np.uint8)


def prepare_colour(
    rgb: np.ndarray, *, do_sharpen: bool = True, rng: np.random.Generator | None = None
) -> np.ndarray:
    """(H, W, 3) uint8 RGB -> same shape, values in {0, 16, ... 240}.

    Brightness/contrast adjustment is left to the artwork; the driver's gain/gamma stage
    is not reproduced.
    """
    rng = rng or np.random.default_rng()
    out = np.empty_like(rgb)
    for i in range(3):
        channel = sharpen(rgb[..., i]) if do_sharpen else rgb[..., i]
        out[..., i] = quantise16(channel, rng)
    return out


def colour_planes(prepared: np.ndarray) -> list[np.ndarray]:
    """Planes 0, 1, 2 (= Y, M, C) as (H, W) arrays of 5-bit densities.

    The driver reads DIB bytes B, G, R as planes 0, 1, 2 and sends (255 - byte) >> 3.
    """
    return [((255 - prepared[..., ch].astype(np.int32)) >> 3).astype(np.uint8) for ch in (2, 1, 0)]


# ── Payload encoders ─────────────────────────────────────────────────────────

def rle_plane(samples: np.ndarray) -> bytes:
    """Run-length encode 5-bit samples in stream order.

    A lone value is one byte (< 0x80). A run of n in 2..127 is (0x80 + n, value). Runs longer
    than 127 are split into 127-runs (0xFF, value) plus a remainder. State is not reset between
    lines, so call this once for the whole plane.
    """
    s = np.asarray(samples, dtype=np.uint8).ravel()
    if s.size == 0:
        return b""
    starts = np.concatenate(([0], np.flatnonzero(np.diff(s)) + 1))
    lengths = np.diff(np.concatenate((starts, [s.size])))
    out = bytearray()
    for value, count in zip(s[starts].tolist(), lengths.tolist()):
        full, rem = divmod(count, 127)
        out += bytes((0xFF, value)) * full
        if rem == 1:
            out.append(value)
        elif rem:
            out += bytes((0x80 + rem, value))
    return bytes(out)


def encode_plane(plane: np.ndarray, orientation: Orientation) -> bytes:
    return rle_plane(orientation.apply(plane))


def pack_mono(mask: np.ndarray, orientation: Orientation) -> bytes:
    """True = resin dot -> 1024 lines x 80 bytes, MSB first, with 0x5B doubled."""
    lines = orientation.apply(mask.astype(np.uint8))
    return np.packbits(lines, axis=1).tobytes().replace(b"\x5b", b"\x5b\x5b")
