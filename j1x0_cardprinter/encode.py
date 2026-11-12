"""Pixel data -> printer payloads (docs/image-data.md).

Colour planes carry a 5-bit dye density per dot, packed three to a 16-bit word. The black resin
panel is a 1-bit bitmap. Both are streamed as 1024 head lines of 640 dots.

Only Pillow and the standard library are used. A card is a Pillow image 1024 wide and 640 tall (CARD_W x
CARD_H); a colour plane is a mode "L" image of 5-bit densities; the resin mask is a mode "L" image that is
255 where there is a resin dot and 0 elsewhere. "Stream order" is the transposed image, 640 wide and 1024
tall, one row per head line.
"""

from __future__ import annotations

import random
import sys
from array import array

from PIL import Image, ImageChops, ImageFilter

LINES = 1024        # head lines per card (long edge)
DOTS = 640          # dots per head line (short edge)
LEVELS = 32         # density levels named in the plane header


def _lut(f) -> list[int]:
    return [f(v) for v in range(256)]


def _point(image: Image.Image, table: list[int]) -> Image.Image:
    """Apply a 256-entry lookup table to every band."""
    return image.point(table * len(image.getbands()))


def stream_order(image: Image.Image) -> Image.Image:
    """A card-sized image (LINES wide, DOTS tall) -> stream order (DOTS wide, LINES tall): row i is image column i.

    This is how the Windows driver sends an upright card; decoding a captured job back into an image
    gives the card the right way up (docs/captured-job.md).
    """
    if image.size != (LINES, DOTS):
        raise ValueError(f"expected a {LINES} x {DOTS} image, got {image.width} x {image.height}")
    return image.transpose(Image.Transpose.TRANSPOSE)


# ── Colour preparation ───────────────────────────────────────────────────────

def sharpen(image: Image.Image, amount: int = 1) -> Image.Image:
    """The driver's 3x3 sharpen (interior pixels only): v += clamp((12c - 2*N4 - D4)*5*amount/16, +-70).

    `amount` 1 is what the Windows driver does; 2, 3, ... scale the correction up. Sharpening makes a thin
    light/dark halo at every hard edge, which shows as a faint line between the steps of a gradient strip.

    Pillow rounds where the driver truncates, so a pixel can differ from the driver's by one level in 255;
    the 16-level quantising that follows hides it.
    """
    kernel = ImageFilter.Kernel((3, 3), [-1, -2, -1, -2, 12, -2, -1, -2, -1],
                                scale=16 / (5 * amount), offset=128)
    correction = _point(image.filter(kernel), _lut(lambda v: 128 + max(-70, min(70, v - 128))))
    sharpened = ImageChops.add(image, correction, offset=-128)           # c + clamp(t), clipped to 0..255
    out = image.copy()
    out.paste(sharpened.crop((1, 1, image.width - 1, image.height - 1)), (1, 1))   # the 1-pixel border is left alone
    return out


def quantise16(channel: Image.Image, rng: random.Random) -> Image.Image:
    """Stochastic rounding of a mode "L" channel to the 16 levels 0, 16, ... 240, as the driver does."""
    base = channel.point(_lut(lambda v: (v // 17) * 16))
    remainder = channel.point(_lut(lambda v: v % 17))
    noise = Image.frombytes("L", channel.size, rng.randbytes(channel.width * channel.height))
    noise = noise.point(_lut(lambda v: min(v // 15, 16)))                # uniform 0..16
    up = ImageChops.subtract(remainder, noise).point(_lut(lambda d: 16 if d else 0))   # round up when noise < v % 17
    return ImageChops.add(base, up)


def prepare_colour(
    rgb: Image.Image, *, sharpening: int = 0, rng: random.Random | None = None
) -> Image.Image:
    """An RGB image -> an RGB image of the same size with values in {0, 16, ... 240}.

    `sharpening` 0 (the default) leaves edges alone; 1 is the Windows driver's 3x3 sharpen (`sharpen`), and
    larger values sharpen more. Brightness/contrast adjustment is left to the artwork; the driver's
    gain/gamma stage is not reproduced.
    """
    rng = rng or random.Random()
    rgb = rgb.convert("RGB")
    if sharpening > 0:
        rgb = sharpen(rgb, sharpening)
    return Image.merge("RGB", [quantise16(channel, rng) for channel in rgb.split()])


def colour_planes(prepared: Image.Image) -> list[Image.Image]:
    """Planes 0, 1, 2 (= Y, M, C) as mode "L" images of 5-bit densities.

    The driver reads DIB bytes B, G, R as planes 0, 1, 2 and sends (255 - byte) >> 3.
    """
    red, green, blue = prepared.convert("RGB").split()
    table = _lut(lambda v: (255 - v) >> 3)
    return [channel.point(table) for channel in (blue, green, red)]


# ── Payload encoders ─────────────────────────────────────────────────────────

WORDS_PER_LINE = -(-DOTS // 3)          # 214: three samples per word, the last word holds one
LINE_BYTES = WORDS_PER_LINE * 2 + 4     # 432: the words, then four zero bytes
_WORD_BYTES = WORDS_PER_LINE * 2        # 428


def pack_plane(samples: Image.Image | bytes) -> bytes:
    """Pack 5-bit samples in stream order into the plane payload.

    `samples` is a mode "L" image DOTS wide (one row per head line), or the same samples as bytes, row by row.

    As the Windows driver does it (`FUN_180014540`, confirmed against a captured job): each line is
    214 big-endian 16-bit words `0 | s0 (5 bits) | s1 | s2`, the last holding only sample 639 in the
    top field, then four zero bytes: 432 bytes a line, 442368 a plane. No run-length coding.
    """
    if isinstance(samples, Image.Image):
        if samples.width != DOTS:
            raise ValueError(f"a stream-order plane is {DOTS} wide, not {samples.width}")
        data = samples.convert("L").tobytes()
    else:
        data = bytes(samples)
    if len(data) % DOTS:
        raise ValueError(f"{len(data)} samples is not a whole number of {DOTS}-dot lines")
    pad = bytes(WORDS_PER_LINE * 3 - DOTS)                              # two zero samples complete the last word
    padded = b"".join(data[i:i + DOTS] + pad for i in range(0, len(data), DOTS))
    words = array("H", [(a << 10) | (b << 5) | c
                        for a, b, c in zip(padded[0::3], padded[1::3], padded[2::3])])
    if sys.byteorder == "little":
        words.byteswap()                                                # the printer wants big-endian words
    packed = words.tobytes()
    tail = bytes(LINE_BYTES - _WORD_BYTES)
    return b"".join(packed[i:i + _WORD_BYTES] + tail for i in range(0, len(packed), _WORD_BYTES))


def unpack_plane(payload: bytes) -> Image.Image:
    """The inverse of `pack_plane`: payload -> a mode "L" image DOTS wide, one row per head line."""
    if len(payload) % LINE_BYTES:
        raise ValueError(f"{len(payload)} bytes is not a whole number of {LINE_BYTES}-byte lines")
    rows = bytearray()
    for start in range(0, len(payload), LINE_BYTES):
        words = array("H")
        words.frombytes(payload[start:start + _WORD_BYTES])
        if sys.byteorder == "little":
            words.byteswap()
        line = bytearray(WORDS_PER_LINE * 3)
        line[0::3] = bytes((w >> 10) & 31 for w in words)
        line[1::3] = bytes((w >> 5) & 31 for w in words)
        line[2::3] = bytes(w & 31 for w in words)
        rows += line[:DOTS]
    return Image.frombytes("L", (DOTS, len(payload) // LINE_BYTES), bytes(rows))


def encode_plane(plane: Image.Image) -> bytes:
    return pack_plane(stream_order(plane))


def pack_mono(mask: Image.Image) -> bytes:
    """The resin mask (255 = dot) -> 1024 lines x 80 bytes, MSB first, with 0x5B doubled.

    The lines go out in the reverse order to the colour planes: a printed test card showed the resin panel
    mirrored along the card's long edge when it was in the same order, so the head-line order of the K
    bitmap is the opposite of the dye planes' (the dots within a line are not reversed).
    """
    lines = stream_order(mask).transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    bits = lines.convert("1", dither=Image.Dither.NONE)                  # one bit a dot, leftmost dot = MSB
    return bits.tobytes().replace(b"\x5b", b"\x5b\x5b")
