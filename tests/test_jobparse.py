import numpy as np

from j1x0_cardprinter.encode import DOTS, LINES, pack_mono, pack_plane
from j1x0_cardprinter.jobparse import parse_job
from j1x0_cardprinter.protocol import build_job, cmd, mono_block, plane_block


def plane_payload(value=1):
    return pack_plane(np.full((LINES, DOTS), value, np.uint8))


def good_job(resin=True):
    mask = np.zeros((DOTS, LINES), bool)
    mask[0, 0] = True
    k = pack_mono(mask) if resin else None
    return build_job([plane_payload(), plane_payload(3), plane_payload(5)], k)


def test_a_job_from_the_builder_parses_cleanly():
    data = good_job()
    segments = parse_job(data)
    assert all(s.ok for s in segments)
    assert segments[0].start == 0 and segments[-1].end == len(data)
    assert all(a.end == b.start for a, b in zip(segments, segments[1:]))       # no gaps, no overlaps


def test_commands_and_images_are_labelled():
    texts = [s.text for s in parse_job(good_job())]
    assert texts[:4] == ["ESC MASTER", "ESC MASTER", "ESC JOB 0", "ESC SXY 1"]
    plane0 = next(t for t in texts if t.startswith("plane 0"))
    assert plane0.startswith("plane 0 (32 levels, 1024x640)") and "442368 of 442368 B packed" in plane0
    assert "ESC IS 0" in texts and "ESC I 10" in texts and texts[-1] == "ESC IV 30"
    assert any(t.startswith("resin bitmap (1024x640, 1 bit)") and "81920 of 81920" in t for t in texts)


def test_a_job_without_resin_has_the_blank_marker():
    texts = [s.text for s in parse_job(good_job(resin=False))]
    assert "ESC !FF 1" in texts and not any("resin" in t for t in texts)


def test_a_truncated_plane_is_flagged():
    short = pack_plane(np.full((10, DOTS), 1, np.uint8))              # 10 lines instead of 1024
    segments = parse_job(cmd("MASTER") + plane_block(0, short) + cmd("IV 30"))
    plane = next(s for s in segments if s.kind == "image")
    # Once its real data runs out the parser walks on into the next commands, as a printer would.
    assert not plane.ok and "of 442368 B packed" in plane.text


def test_a_bad_line_terminator_is_flagged():
    payload = bytearray(plane_payload())
    payload[431] = 1                                                   # the fourth zero byte of line 0
    plane = next(s for s in parse_job(plane_block(0, bytes(payload))) if s.kind == "image")
    assert not plane.ok and "bad line terminator" in plane.text


def test_stuffed_0x5b_bytes_are_counted_once():
    mask = np.zeros((DOTS, LINES), bool)
    for dot in (1, 3, 4, 6, 7):                          # the byte 0x5B in the first line sent
        mask[dot, LINES - 1] = True
    payload = pack_mono(mask)
    assert payload[:2] == b"\x5b\x5b" and len(payload) == 81921
    bitmap = next(s for s in parse_job(mono_block(payload)) if "resin" in s.text)
    assert bitmap.ok and "81920 of 81920" in bitmap.text


def test_stray_and_unterminated_bytes_are_flagged():
    stray = parse_job(b"junk" + cmd("MASTER"))
    assert not stray[0].ok and stray[0].kind == "unknown" and "4 unexpected bytes" in stray[0].text
    unterminated = parse_job(cmd("MASTER") + b"\x1bMO")
    assert not unterminated[-1].ok and "unterminated" in unterminated[-1].text


def test_an_empty_job_has_no_segments():
    assert parse_job(b"") == []
