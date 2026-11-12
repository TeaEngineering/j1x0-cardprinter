from j1x0_cardprinter.protocol import (
    JobOptions, NO_RESIN, Overlay, Tuning, build_job, cmd, mono_block, plane_block,
)
from j1x0_cardprinter.usb import J1x0MockUSB


def test_cmd_framing():
    assert cmd("MASTER") == b"\x1bMASTER\r"


def test_plane_block_matches_driver_format():
    # only plane 0 is preceded by ESC $F CR (docs/captured-job.md)
    assert plane_block(0, b"XY") == b"\x1b$F\r\x1bGS 0 32 0 0 1024 640 XY\r\x1bIS 0\r"
    assert plane_block(1, b"XY") == b"\x1bGS 1 32 0 0 1024 640 XY\r\x1bIS 1\r"


def test_mono_block_matches_driver_format():
    assert mono_block(b"ZZ") == b"\x1bF\r\x1bG 0 0 10 640 1024 1\r\x1bZZZ\r\x1bI 10\r"


def test_job_without_resin_uses_blank_marker():
    job = build_job([b"a", b"b", b"c"], None)
    # MASTER twice (monitor and driver), then JOB, SXY and the default tuning of 5
    assert job.startswith(b"\x1bMASTER\r\x1bMASTER\r\x1bJOB 0\r\x1bSXY 1\r\x1b+$C 0 5\r")
    assert NO_RESIN == b"\x1bF\r\x1b!FF 1\r" and NO_RESIN in job
    assert job.endswith(b"\x1bvF\r\x1bIV 30\r")


def test_end_of_card_is_mo_alone():
    from j1x0_cardprinter.protocol import end_of_card
    assert end_of_card() == b"\x1bMO\r"


def test_job_order_and_options():
    opts = JobOptions(tuning=Tuning(c=(1, 2, 3), common=9), overlay=Overlay.NONE, trailer=cmd("MO"))
    job = build_job([b"a", b"b", b"c"], b"k", opts)
    order = [job.index(m) for m in (b"MASTER", b"+$C 0 1", b"+C 9", b"GS 0", b"GS 1", b"GS 2",
                                    b"G 0 0 10", b"\x1bMO\r")]
    assert order == sorted(order)
    assert b"vF" not in job


def test_mock_link_records_writes():
    link = J1x0MockUSB(responses={b"q": b"reply"})
    assert link.write(b"abc") == 3
    assert link.query(b"q") == b"reply"
    assert link.stream == b"abc"            # queries are recorded separately from job bytes
    assert link.queries == [b"q"]
