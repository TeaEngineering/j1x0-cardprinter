"""Pixel data -> printer payloads (docs/image-data.md).

Colour planes carry a 5-bit dye density per dot, packed three to a 16-bit word. The black resin
panel is a 1-bit bitmap. Both are streamed as 1024 head lines of 640 dots.
"""

from __future__ import annotations

import numpy as np

LINES = 1024        # head lines per card (long edge)
DOTS = 640          # dots per head line (short edge)
LEVELS = 32         # density levels named in the plane header


def stream_order(image: np.ndarray) -> np.ndarray:
    """(DOTS, LINES) image array -> (LINES, DOTS) array in stream order: line i is image column i.

    This is how the Windows driver sends an upright card; decoding a captured job back into an image
    gives the card the right way up (docs/captured-job.md).
    """
    if image.shape[:2] != (DOTS, LINES):
        raise ValueError(f"expected {(DOTS, LINES)} image, got {image.shape[:2]}")
    return np.ascontiguousarray(image.T)


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

WORDS_PER_LINE = -(-DOTS // 3)          # 214: three samples per word, the last word holds one
LINE_BYTES = WORDS_PER_LINE * 2 + 4     # 432: the words, then four zero bytes


def pack_plane(samples: np.ndarray) -> bytes:
    """Pack 5-bit samples in stream order (LINES x DOTS) into the plane payload.

    As the Windows driver does it (`FUN_180014540`, confirmed against a captured job): each line is
    214 big-endian 16-bit words `0 | s0 (5 bits) | s1 | s2`, the last holding only sample 639 in the
    top field, then four zero bytes: 432 bytes a line, 442368 a plane. No run-length coding.
    """
    s = np.asarray(samples, dtype=np.uint16).reshape(-1, DOTS)
    lines = s.shape[0]
    padded = np.zeros((lines, WORDS_PER_LINE * 3), np.uint16)
    padded[:, :DOTS] = s
    triples = padded.reshape(lines, WORDS_PER_LINE, 3)
    words = (triples[..., 0] << 10) | (triples[..., 1] << 5) | triples[..., 2]
    out = np.zeros((lines, LINE_BYTES), np.uint8)
    out[:, 0:WORDS_PER_LINE * 2:2] = words >> 8
    out[:, 1:WORDS_PER_LINE * 2:2] = words & 0xFF
    return out.tobytes()


def unpack_plane(payload: bytes) -> np.ndarray:
    """The inverse of `pack_plane`: payload -> (lines, DOTS) array of 5-bit samples."""
    rows = np.frombuffer(payload, np.uint8).reshape(-1, LINE_BYTES)
    words = (rows[:, 0:WORDS_PER_LINE * 2:2].astype(np.uint16) << 8) | rows[:, 1:WORDS_PER_LINE * 2:2]
    samples = np.stack(((words >> 10) & 31, (words >> 5) & 31, words & 31), axis=-1)
    return samples.reshape(len(rows), -1)[:, :DOTS].astype(np.uint8)


def encode_plane(plane: np.ndarray) -> bytes:
    return pack_plane(stream_order(plane))


def pack_mono(mask: np.ndarray) -> bytes:
    """True = resin dot -> 1024 lines x 80 bytes, MSB first, with 0x5B doubled.

    The lines go out in the reverse order to the colour planes: a printed test card showed the resin panel
    mirrored along the card's long edge when it was in the same order, so the head-line order of the K
    bitmap is the opposite of the dye planes' (the dots within a line are not reversed).
    """
    lines = stream_order(mask.astype(np.uint8))[::-1]
    return np.packbits(lines, axis=1).tobytes().replace(b"\x5b", b"\x5b\x5b")
