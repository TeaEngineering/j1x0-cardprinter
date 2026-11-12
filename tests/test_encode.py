import numpy as np
import pytest

from j1x0_cardprinter.encode import (
    DOTS, LINE_BYTES, LINES, colour_planes, pack_mono, pack_plane, prepare_colour, quantise16, stream_order,
    unpack_plane,
)


def test_pack_plane_layout_for_a_blank_line():
    line = pack_plane(np.full((1, DOTS), 1, np.uint8))
    assert len(line) == LINE_BYTES == 432
    assert line == b"\x04\x21" * 213 + b"\x04\x00" + b"\x00" * 4     # 213 full words, 1 lone sample, 4 zero bytes


def test_pack_plane_field_order_is_msb_first():
    samples = np.zeros((1, DOTS), np.uint8)
    samples[0, :3] = (31, 17, 3)
    assert pack_plane(samples)[:2] == ((31 << 10) | (17 << 5) | 3).to_bytes(2, "big")


@pytest.mark.parametrize("seed", range(3))
def test_pack_plane_roundtrips(seed):
    samples = np.random.default_rng(seed).integers(0, 32, (LINES, DOTS)).astype(np.uint8)
    packed = pack_plane(samples)
    assert len(packed) == LINES * LINE_BYTES == 442368
    assert (unpack_plane(packed) == samples).all()


def test_white_page_encodes_to_value_one():
    white = np.full((DOTS, LINES, 3), 255, np.uint8)
    planes = colour_planes(prepare_colour(white, rng=np.random.default_rng(0)))
    for plane in planes:
        assert plane.shape == (DOTS, LINES)
        assert (plane == 1).all()


def test_plane_channel_mapping_is_ymc_from_bgr():
    px = np.zeros((DOTS, LINES, 3), np.uint8)
    px[..., 0] = 255       # R full, G and B zero -> only plane 2 (cyan) is blank
    prepared = prepare_colour(px, do_sharpen=False, rng=np.random.default_rng(0))
    y, m, c = colour_planes(prepared)
    assert (y == 31).all() and (m == 31).all() and (c == 1).all()


def test_quantise_levels_are_multiples_of_16():
    rng = np.random.default_rng(1)
    out = quantise16(rng.integers(0, 256, (50, 50)), rng)
    assert set(np.unique(out)) <= set(range(0, 241, 16))


def test_pack_mono_shape_and_bit_order():
    mask = np.zeros((DOTS, LINES), bool)
    mask[0, 0] = True                       # dot 0 of line 0 -> MSB of byte 0
    data = pack_mono(mask)
    assert len(data) == LINES * 80
    assert data[0] == 0x80 and not any(data[1:])


def test_pack_mono_stuffs_0x5b():
    mask = np.zeros((DOTS, LINES), bool)
    for dot in (1, 3, 4, 6, 7):             # 0b01011011 = 0x5B
        mask[dot, 0] = True
    data = pack_mono(mask)
    assert data[:2] == b"\x5b\x5b" and len(data) == LINES * 80 + 1


def test_stream_order_line_i_is_image_column_i():
    # Proven from a captured job: decoding it gives an upright card with this mapping (docs/captured-job.md).
    img = np.arange(DOTS * LINES).reshape(DOTS, LINES)
    lines = stream_order(img)
    assert lines.shape == (LINES, DOTS)
    assert lines[0, 0] == img[0, 0] and lines[0, 1] == img[1, 0] and lines[1, 0] == img[0, 1]
    with pytest.raises(ValueError):
        stream_order(np.zeros((3, 3)))
