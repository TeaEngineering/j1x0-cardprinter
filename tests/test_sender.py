import logging

import pytest

from j1x0_cardprinter import sender
from j1x0_cardprinter.protocol import end_of_card, plane_block, cmd
from j1x0_cardprinter.sender import PrinterError, send_job
from j1x0_cardprinter.usb import J1x0MockUSB


def writes(link):
    return [e[1] for e in link.events if e[0] == "write"]


def test_data_is_sent_in_chunks_and_the_printer_is_asked_after_it():
    link = J1x0MockUSB()
    data = bytes(range(256)) * 40                         # 10240 bytes, no image blocks
    result = send_job(link, data, end=False)
    assert writes(link) == [4096, 4096, 2048]
    assert link.events == [("write", 4096), ("write", 4096), ("write", 2048), ("enquire",)]
    assert link.stream == data
    assert (result.bytes_sent, result.chunks, result.enquiries) == (10240, 3, 1)


def test_the_chunk_size_can_be_changed():
    link = J1x0MockUSB()
    send_job(link, b"x" * 250, end=False, chunk=100)
    assert writes(link) == [100, 100, 50] and link.enquiries == 1


def test_the_card_ends_with_mo_alone_as_in_the_captured_job():
    link = J1x0MockUSB()
    send_job(link, b"data")
    assert link.sent == [b"data", b"\x1bMO\r"] and link.sent[1] == end_of_card()


def test_the_final_mo_is_acknowledged():
    link = J1x0MockUSB()
    send_job(link, b"data")
    assert link.events == [("write", 4), ("enquire",), ("write", 4), ("enquire",)]


def test_the_end_of_card_is_not_acknowledged_or_sent_when_not_wanted():
    link = J1x0MockUSB()
    send_job(link, b"data", end=False)
    assert link.sent == [b"data"] and link.enquiries == 1


def test_silence_is_retried_and_then_succeeds():
    link = J1x0MockUSB(enquiry_replies=[b"", b"", b"\x06"])
    result = send_job(link, b"data", end=False)
    assert link.enquiries == 3 and result.enquiries == 3


def test_a_printer_that_stops_answering_is_reported_with_the_byte_offset():
    link = J1x0MockUSB(enquiry_replies=[b""] * sender.ACK_TRIES)
    with pytest.raises(PrinterError, match="stopped answering") as exc:
        send_job(link, b"x" * 5000, end=False)
    assert exc.value.offset == 5000 and "after 5000 bytes were sent" in str(exc.value)
    assert link.enquiries == sender.ACK_TRIES


def test_a_nak_is_decoded_into_the_error_name():
    link = J1x0MockUSB(enquiry_replies=[b"\x1551"])
    with pytest.raises(PrinterError, match="COVER OPEN") as exc:
        send_job(link, b"data", end=False)
    assert exc.value.code == 51 and exc.value.offset == 4


def test_nothing_more_is_sent_after_an_error():
    link = J1x0MockUSB(enquiry_replies=[b"\x1501"])
    with pytest.raises(PrinterError, match="OUT OF RIBBON"):
        send_job(link, b"x" * 12000)
    assert writes(link) == [4096, 4096, 3808]              # all the data, then no end-of-card


def test_an_unexpected_reply_is_an_error():
    with pytest.raises(PrinterError, match="unexpected reply"):
        send_job(J1x0MockUSB(enquiry_replies=[b"hello"]), b"data", end=False)


def test_a_short_write_is_an_error():
    class Short(J1x0MockUSB):
        def write(self, data):
            super().write(data)
            return len(data) - 1

    with pytest.raises(PrinterError, match="short write"):
        send_job(Short(), b"data", end=False)


def test_progress_is_reported_after_each_chunk():
    seen = []
    send_job(J1x0MockUSB(), b"x" * 9000, end=False, progress=lambda sent, total: seen.append((sent, total)))
    assert seen == [(4096, 9000), (8192, 9000), (9000, 9000)]


def test_the_waits_are_applied(monkeypatch):
    sleeps = []
    monkeypatch.setattr(sender.time, "sleep", sleeps.append)
    monkeypatch.setattr(sender, "SETTLE", 0.05)
    monkeypatch.setattr(sender, "END_DELAY", 2.0)
    send_job(J1x0MockUSB(), b"x" * 5000)
    assert sleeps == [0.05, 0.05, 2.0]                     # after each chunk, then before the end


# ── Logging ──────────────────────────────────────────────────────────────────

JOB = cmd("MASTER") + cmd("JOB 0") + cmd("IV 30") + plane_block(0, bytes([1]) * 5 + bytes([0x80 + 2, 3]))


def test_info_logging_lists_the_commands_as_they_are_sent(caplog):
    with caplog.at_level(logging.INFO, logger="j1x0_cardprinter.sender"):
        send_job(J1x0MockUSB(), JOB, end=True)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "ESC MASTER" in text and "ESC JOB 0" in text and "ESC IV 30" in text
    assert "plane 0 (32 levels, 1024x640)" in text and "<-- PROBLEM" in text      # this plane is far too short
    assert "end of card: <ESC>MO<CR>" in text and "done: " in text


def test_debug_logging_shows_each_acknowledgement(caplog):
    link = J1x0MockUSB(enquiry_replies=[b"", b"\x06"])
    with caplog.at_level(logging.DEBUG, logger="j1x0_cardprinter.sender"):
        send_job(link, b"data", end=False)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "ESC E 2 -> nothing" in text and "ESC E 2 -> <ACK>" in text and "attempt 2" in text
    assert "chunk 1: 4 B written (4 of 4)" in text


def test_quiet_by_default(caplog):
    with caplog.at_level(logging.WARNING, logger="j1x0_cardprinter.sender"):
        send_job(J1x0MockUSB(), JOB)
    assert caplog.records == []


# ── Where the acknowledgement is asked for ───────────────────────────────────

import numpy as np                                          # noqa: E402

from j1x0_cardprinter.encode import DOTS, LINES, pack_plane  # noqa: E402
from j1x0_cardprinter.jobparse import parse_job             # noqa: E402
from j1x0_cardprinter.protocol import build_job             # noqa: E402


def real_job():
    """A whole card as the Windows driver sent it: three planes and a resin bitmap."""
    plane = pack_plane(np.full((LINES, DOTS), 1, np.uint8))
    return build_job([plane, plane, plane], bytes(LINES * DOTS // 8))


def enquiry_positions(link):
    position, out = 0, []
    for event in link.events:
        if event[0] == "write":
            position += event[1]
        else:
            out.append(position)
    return out


def test_it_asks_where_the_windows_monitor_did():
    job = real_job()
    link = J1x0MockUSB()
    send_job(link, job, end=False)
    segments = {seg.start: seg for seg in parse_job(job)}
    asked = [next(s for s in segments.values() if s.end == pos) for pos in enquiry_positions(link)]
    # after each plane's data and its ESC IS, after the resin data and ESC I 10, and after ESC IV 30
    assert [(s.kind, s.text.split(" (")[0]) for s in asked] == [
        ("image", "plane 0"), ("command", "ESC IS 0"), ("image", "plane 1"), ("command", "ESC IS 1"),
        ("image", "plane 2"), ("command", "ESC IS 2"), ("image", "resin bitmap"), ("command", "ESC I 10"),
        ("command", "ESC IV 30")]
    assert link.enquiries == 9


def test_it_never_asks_during_the_header_or_inside_a_block():
    job = real_job()
    link = J1x0MockUSB()
    send_job(link, job, end=False)
    segments = parse_job(job)
    first_image = next(s.start for s in segments if s.kind == "image")
    for pos in enquiry_positions(link):
        assert pos > first_image
        assert not any(s.kind == "image" and s.start < pos < s.end for s in segments)


def test_blocks_are_chunked_but_each_block_is_acknowledged_once():
    link = J1x0MockUSB()
    send_job(link, real_job(), end=False, chunk=1000)
    assert link.enquiries == 9                                  # more chunks, same number of questions
    assert all(n <= 1000 for n in writes(link))


def test_every_chunk_is_followed_by_the_pause(monkeypatch):
    sleeps = []
    monkeypatch.setattr(sender.time, "sleep", sleeps.append)
    monkeypatch.setattr(sender, "SETTLE", 0.05)
    monkeypatch.setattr(sender, "END_DELAY", 0)
    result = send_job(J1x0MockUSB(), real_job(), end=False)
    assert sleeps.count(0.05) == result.chunks and result.chunks > 100
