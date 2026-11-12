import numpy as np
import pytest

from j1x0_cardprinter.encode import (
    DOTS, LINES, Orientation, colour_planes, pack_mono, prepare_colour, quantise16, rle_plane,
)


def reference_rle(samples):
    """Direct port of the driver's state machine (RlePlane_Portrait)."""
    out, state, val, cnt = bytearray(), 0, 0, 0
    for v in samples:
        if state == 0:
            val, cnt, state = v, 0, 1
        elif state == 1:
            if v == val:
                cnt, state = 2, 2
            else:
                out.append(val)
                val = v
        elif v != val:
            out += bytes((0x80 + cnt, val))
            val, state = v, 1
        else:
            cnt += 1
            if cnt == 127:
                out += bytes((0xFF, val))
                state = 0
    if state == 1:
        out.append(val)
    elif state == 2:
        out += bytes((0x80 + cnt, val))
    return bytes(out)


def decode_rle(data):
    out, i = [], 0
    while i < len(data):
        b = data[i]
        if b < 0x80:
            out.append(b)
            i += 1
        else:
            out += [data[i + 1]] * (b - 0x80)
            i += 2
    return out


def test_rle_known_values():
    assert rle_plane(np.array([5, 5, 5, 7])) == bytes((0x83, 5, 7))
    assert rle_plane(np.array([1])) == bytes((1,))
    assert rle_plane(np.array([3] * 127)) == bytes((0xFF, 3))
    assert rle_plane(np.array([3] * 128)) == bytes((0xFF, 3, 3))
    assert rle_plane(np.array([3] * 130)) == bytes((0xFF, 3, 0x83, 3))
    assert rle_plane(np.array([], dtype=np.uint8)) == b""


@pytest.mark.parametrize("seed", range(5))
def test_rle_matches_driver_state_machine_and_roundtrips(seed):
    rng = np.random.default_rng(seed)
    # Mix of long runs and noise, values like the real planes (odd, 1..31).
    runs = [np.full(int(n), int(v), np.uint8)
            for n, v in zip(rng.integers(1, 400, 200), rng.choice(range(1, 32, 2), 200))]
    samples = np.concatenate(runs)
    encoded = rle_plane(samples)
    assert encoded == reference_rle(samples.tolist())
    assert decode_rle(encoded) == samples.tolist()


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
    data = pack_mono(mask, Orientation())
    assert len(data) == LINES * 80
    assert data[0] == 0x80 and not any(data[1:])


def test_pack_mono_stuffs_0x5b():
    mask = np.zeros((DOTS, LINES), bool)
    for dot in (1, 3, 4, 6, 7):             # 0b01011011 = 0x5B
        mask[dot, 0] = True
    data = pack_mono(mask, Orientation())
    assert data[:2] == b"\x5b\x5b" and len(data) == LINES * 80 + 1


def test_orientation_flags():
    img = np.arange(DOTS * LINES).reshape(DOTS, LINES)
    base = Orientation().apply(img)
    assert base[0, 0] == img[0, 0] and base[1, 0] == img[0, 1]
    both = Orientation(flip_lines=True, flip_dots=True).apply(img)
    assert (both == base[::-1, ::-1]).all()
    with pytest.raises(ValueError):
        Orientation().apply(np.zeros((3, 3)))
