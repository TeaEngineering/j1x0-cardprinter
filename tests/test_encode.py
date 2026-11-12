import hashlib
import random

import pytest
from PIL import Image

from j1x0_cardprinter.encode import (
    DOTS, LINE_BYTES, LINES, colour_planes, encode_plane, pack_mono, pack_plane, prepare_colour, quantise16,
    sharpen, stream_order, unpack_plane,
)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def card(mode="L", colour=0):
    """A blank card-sized image: LINES wide, DOTS tall."""
    return Image.new(mode, (LINES, DOTS), colour)


def five_bit(seed, count):
    return random.Random(seed).randbytes(count).translate(bytes(i & 31 for i in range(256)))


def column_mask(column, rows):
    """A resin mask with dots at the given rows of one image column."""
    mask = card()
    for row in rows:
        mask.putpixel((column, row), 255)
    return mask


# ── Golden checksums, taken from the numpy implementation this one replaced ──────────────────────

def test_pack_plane_matches_the_numpy_version():
    packed = pack_plane(five_bit(123, LINES * DOTS))
    assert sha(packed) == "9427222100dc7d28799434adad53eb28cc653f2ce488f1fa02cc9dc113e07656"


def test_encode_plane_matches_the_numpy_version():
    plane = Image.frombytes("L", (LINES, DOTS), five_bit(123, LINES * DOTS))
    assert sha(encode_plane(plane)) == "1114218cb5168122fc3a9b13e8b03cd592d20d8eebbcc6c51d6735a9dc2b8c31"


def test_pack_mono_matches_the_numpy_version_including_the_stuffing():
    raw = random.Random(7).randbytes(LINES * DOTS).translate(bytes(255 if i > 200 else 0 for i in range(256)))
    packed = pack_mono(Image.frombytes("L", (LINES, DOTS), raw))
    assert sha(packed) == "3d58fe359e12d9dc1da6bbf9892d33e50a49baad9383ebc47d93ff2c55bde8ba"
    assert len(packed) == LINES * 80 + packed.count(b"\x5b\x5b") and packed.count(b"\x5b\x5b") == 16


def test_colour_planes_match_the_numpy_version():
    rgb = Image.frombytes("RGB", (LINES, DOTS), random.Random(9).randbytes(DOTS * LINES * 3))
    planes = colour_planes(rgb)
    assert sha(b"".join(p.tobytes() for p in planes)) == \
        "cfe5da6dd27674a427bc7fb078cae16f25d9e5a6d89632fe99b730676c1ee61d"


# ── Plane packing ────────────────────────────────────────────────────────────

def test_pack_plane_layout_for_a_blank_line():
    line = pack_plane(bytes([1]) * DOTS)
    assert len(line) == LINE_BYTES == 432
    assert line == b"\x04\x21" * 213 + b"\x04\x00" + b"\x00" * 4     # 213 full words, 1 lone sample, 4 zero bytes


def test_pack_plane_field_order_is_msb_first():
    samples = bytearray(DOTS)
    samples[0:3] = (31, 17, 3)
    assert pack_plane(bytes(samples))[:2] == ((31 << 10) | (17 << 5) | 3).to_bytes(2, "big")


def test_pack_plane_takes_an_image_or_bytes():
    samples = five_bit(5, 3 * DOTS)
    assert pack_plane(Image.frombytes("L", (DOTS, 3), samples)) == pack_plane(samples)
    with pytest.raises(ValueError):
        pack_plane(Image.new("L", (DOTS + 1, 2)))
    with pytest.raises(ValueError):
        pack_plane(bytes(DOTS + 1))


@pytest.mark.parametrize("seed", range(3))
def test_pack_plane_roundtrips(seed):
    samples = five_bit(seed, LINES * DOTS)
    packed = pack_plane(samples)
    assert len(packed) == LINES * LINE_BYTES == 442368
    back = unpack_plane(packed)
    assert back.size == (DOTS, LINES) and back.tobytes() == samples


# ── Colour preparation ───────────────────────────────────────────────────────

def test_white_page_encodes_to_value_one():
    planes = colour_planes(prepare_colour(card("RGB", (255, 255, 255)), rng=random.Random(0)))
    for plane in planes:
        assert plane.size == (LINES, DOTS)
        assert plane.getextrema() == (1, 1)


def test_plane_channel_mapping_is_ymc_from_bgr():
    prepared = prepare_colour(card("RGB", (255, 0, 0)), rng=random.Random(0))   # R full, G and B zero
    y, m, c = colour_planes(prepared)
    assert y.getextrema() == (31, 31) and m.getextrema() == (31, 31) and c.getextrema() == (1, 1)


def test_quantise_levels_are_multiples_of_16():
    rng = random.Random(1)
    channel = Image.frombytes("L", (50, 50), rng.randbytes(2500))
    assert set(quantise16(channel, rng).tobytes()) <= set(range(0, 241, 16))


def test_quantise_is_exact_on_multiples_of_17_and_unbiased_elsewhere():
    rng = random.Random(2)
    assert set(quantise16(Image.new("L", (40, 40), 153), rng).tobytes()) == {144}       # 9 * 17 -> 9 * 16
    flat = quantise16(Image.new("L", (200, 200), 100), rng)                              # 100 = 5 * 17 + 15
    mean = sum(flat.tobytes()) / (200 * 200)
    assert abs(mean - 100 * 16 / 17) < 1.0                                               # stochastic rounding keeps the mean


def test_the_same_seed_gives_the_same_dither():
    image = Image.frombytes("RGB", (60, 40), random.Random(3).randbytes(60 * 40 * 3))
    a = prepare_colour(image, rng=random.Random(5)).tobytes()
    assert a == prepare_colour(image, rng=random.Random(5)).tobytes()
    assert a != prepare_colour(image, rng=random.Random(6)).tobytes()


# ── The resin bitmap ─────────────────────────────────────────────────────────

def test_pack_mono_shape_and_bit_order():
    data = pack_mono(column_mask(LINES - 1, [0]))    # image column 1023 is the first line sent; dot 0 is its MSB
    assert len(data) == LINES * 80
    assert data[0] == 0x80 and not any(data[1:])


def test_the_resin_lines_go_out_in_the_reverse_order_to_the_colour_planes():
    # A printed card showed K mirrored along the long edge when it used the dye planes' line order.
    image = column_mask(0, [5])                      # image column 0, row 5
    assert stream_order(image).getpixel((5, 0)) == 255          # the dye planes send column 0 first...
    k = pack_mono(image)
    assert not any(k[:80]) and k[(LINES - 1) * 80] == 0x80 >> 5  # ...the resin bitmap sends it last, dot 5 -> bit 5
    assert sum(k) == 0x80 >> 5                                    # and nothing else is set; dots are not reversed


def test_pack_mono_stuffs_0x5b():
    data = pack_mono(column_mask(LINES - 1, (1, 3, 4, 6, 7)))     # 0b01011011 = 0x5B, in the first line sent
    assert data[:2] == b"\x5b\x5b" and len(data) == LINES * 80 + 1


def test_stream_order_line_i_is_image_column_i():
    # Proven from a captured job: decoding it gives an upright card with this mapping (docs/captured-job.md).
    img = Image.new("I", (LINES, DOTS))
    img.putdata(range(DOTS * LINES))                 # pixel (x, y) = y * LINES + x
    lines = stream_order(img)
    assert lines.size == (DOTS, LINES)
    assert lines.getpixel((0, 0)) == img.getpixel((0, 0))
    assert lines.getpixel((1, 0)) == img.getpixel((0, 1))       # along a line = down an image column
    assert lines.getpixel((0, 1)) == img.getpixel((1, 0))       # next line = next image column
    with pytest.raises(ValueError):
        stream_order(Image.new("L", (3, 3)))


# ── Sharpening ───────────────────────────────────────────────────────────────

def edge_image():
    edge = Image.new("L", (9, 9), 0)
    edge.paste(20, (5, 0, 9, 9))                     # a vertical edge, small enough not to clamp
    return edge


def test_sharpening_is_off_by_default_and_scales_with_the_amount():
    plain = prepare_colour(edge_image().convert("RGB"), rng=random.Random(0))
    assert set(plain.getchannel(0).crop((0, 4, 9, 5)).tobytes()) <= {0, 16, 32}    # just the flat levels, no halo
    edge = edge_image()
    one, two = sharpen(edge, 1), sharpen(edge, 2)
    assert one.getpixel((4, 4)) <= edge.getpixel((4, 4)) and one.getpixel((5, 4)) > edge.getpixel((5, 4))
    assert two.getpixel((5, 4)) - 20 > one.getpixel((5, 4)) - 20      # the bright side of the edge is boosted more


def test_sharpening_leaves_the_one_pixel_border_alone():
    image = Image.frombytes("L", (12, 10), random.Random(4).randbytes(120))
    out = sharpen(image, 1)
    for x in range(12):
        assert out.getpixel((x, 0)) == image.getpixel((x, 0)) and out.getpixel((x, 9)) == image.getpixel((x, 9))
    for y in range(10):
        assert out.getpixel((0, y)) == image.getpixel((0, y)) and out.getpixel((11, y)) == image.getpixel((11, y))
    assert out.tobytes() != image.tobytes()                            # but the inside changed
