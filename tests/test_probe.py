import pytest

from j1x0_cardprinter.probe import check_link, quick_check
from j1x0_cardprinter.status import parse_panels, smart_card_type
from j1x0_cardprinter.usb import J1x0MockUSB

CORE_QUERIES = [b"\x1bV\r", b"\x1b!RIBBON\r", b"\x1b!RIBLEN\r"]
INFO_QUERIES = [b"\x1b%SERIE\r", b"\x1b!V 15\r", b"\x1b!V 20\r", b"\x1b!NL\r"]


def test_idle_printer_is_ready():
    link = J1x0MockUSB()
    report = check_link(link)
    assert report.ready and not report.warnings
    text = "\n".join(report.lines())
    assert "0x18" in text and "J1x0 MOCK 1.0" in text and "2 (YMCKO)" in text
    assert text.endswith("READY")
    assert link.queries == CORE_QUERIES + INFO_QUERIES
    assert link.sent == []                         # a check never writes job data


def test_device_information_fields_in_help_page_order():
    lines = check_link(J1x0MockUSB()).lines()
    labels = [line.split(":")[0] for line in lines if line.split(":")[0] in (
        "model/firmware", "serial number", "head resistance", "magnetic encoder", "smart card",
        "ribbon type", "remaining count", "panels printed")]
    assert labels == ["model/firmware", "serial number", "head resistance", "magnetic encoder",
                      "smart card", "ribbon type", "remaining count", "panels printed"]
    text = "\n".join(lines)
    assert "serial number:    MOCK0001" in text
    assert "head resistance:  26" in text
    assert "magnetic encoder: Yes" in text
    assert "smart card:       No" in text
    assert "remaining count:  100" in text
    assert "panels printed:   1234" in text


@pytest.mark.parametrize("version, expected", [
    ("J110 1.0", "No"),
    ("J110 1.0 0iB", "Contact Encoder"),
    ("J110 0iC", "Contact Encoder and HID iCLASS"),
    ("J110 0iD", "Contact Encoder and MIFARE"),
    ("J110 0iE", "Contact Station"),
    ("J110 0iF", "HID iCLASS"),
    ("J110 0iH", "MIFARE contactless"),
    ("J110 0iH 0iF", "HID iCLASS"),            # first match in the Windows order wins
])
def test_smart_card_type_from_the_version_string(version, expected):
    assert smart_card_type(version) == expected


def test_report_decodes_smart_card_from_the_model_reply():
    report = check_link(J1x0MockUSB(responses={b"\x1bV\r": b"J110i FW 2.1 0iF\x06"}))
    assert report.smart_card == "HID iCLASS" and report.ready
    assert "smart card:       HID iCLASS" in "\n".join(report.lines())


@pytest.mark.parametrize("text, count", [("!1234 ", 1234), ("1234", 1234), ("!0", 0), ("abc", None)])
def test_parse_panels(text, count):
    assert parse_panels(text) == count


def test_magnetic_encoder_yes_no():
    assert check_link(J1x0MockUSB(responses={b"\x1b!V 20\r": b"0\x06"})).magnetic_encoder == "No"
    assert check_link(J1x0MockUSB(responses={b"\x1b!V 20\r": b"1\x06"})).magnetic_encoder == "Yes"


def test_unanswered_info_fields_are_na_and_do_not_block_ready():
    silent = {q: b"" for q in INFO_QUERIES}
    report = check_link(J1x0MockUSB(responses=silent))
    assert report.ready and not report.warnings
    text = "\n".join(report.lines())
    assert "serial number:    N/A (NO REPLY)" in text
    assert "head resistance:  N/A (NO REPLY)" in text
    assert "magnetic encoder: N/A" in text
    assert "panels printed:   N/A" in text


def test_error_reply_on_an_info_field_is_shown_by_name():
    report = check_link(J1x0MockUSB(responses={b"\x1b%SERIE\r": b"\x1551"}))
    assert report.ready
    assert "serial number:    N/A (COVER OPEN, error 51)" in "\n".join(report.lines())


def test_monochrome_ribbon_remaining_count_is_na():
    report = check_link(J1x0MockUSB(responses={b"\x1b!RIBBON\r": b"7\x06", b"\x1b!RIBLEN\r": b"0\x06"}))
    assert "remaining count:  N/A (monochrome ribbon)" in "\n".join(report.lines())


def test_no_model_reply_means_smart_card_na():
    report = check_link(J1x0MockUSB(responses={b"\x1bV\r": b""}))
    assert report.smart_card == "N/A" and not report.ready


def test_offline_printer_is_not_ready():
    report = check_link(J1x0MockUSB(status=0x00))
    assert not report.ready
    assert "offline" in " ".join(report.problems)


def test_no_ribbon_is_a_problem():
    report = check_link(J1x0MockUSB(responses={b"\x1b!RIBBON\r": b"0\x06"}))
    assert not report.ready and "no ribbon" in report.problems[0]
    assert "0 (No Ribbon)" in "\n".join(report.lines())


def test_silent_printer_fails_the_check():
    report = check_link(J1x0MockUSB(responses={b"\x1bV\r": b""}))
    assert not report.ready and "NO REPLY" in report.problems[0]


def test_error_reply_is_reported_by_name():
    report = check_link(J1x0MockUSB(responses={b"\x1bV\r": b"\x1551"}))
    assert not report.ready and "COVER OPEN" in report.problems[0]


def test_ribbon_query_failure_is_only_a_warning():
    report = check_link(J1x0MockUSB(responses={b"\x1b!RIBBON\r": b""}))
    assert report.ready and any("ribbon query failed" in w for w in report.warnings)


def test_low_ribbon_warns():
    report = check_link(J1x0MockUSB(responses={b"\x1b!RIBLEN\r": b"10\x06"}))
    assert report.ready and any("running low" in w for w in report.warnings)


class NoStatusByte(J1x0MockUSB):
    """Like a J110i: it never answers the USB status request."""

    def port_status(self):
        raise OSError("Operation timed out")


def test_a_printer_without_a_status_byte_is_judged_by_its_replies():
    report = check_link(NoStatusByte())
    assert report.ready
    assert any("does not answer the USB status request" in w for w in report.warnings)
    assert "port status:      unavailable (Operation timed out)" in "\n".join(report.lines())


def test_no_status_byte_and_no_replies_is_not_ready():
    report = check_link(NoStatusByte(responses={b"\x1bV\r": b""}))
    assert not report.ready and "version query" in report.problems[0]


def test_quick_check_uses_the_status_byte_when_there_is_one():
    assert quick_check(J1x0MockUSB()) == (True, "")
    ready, reason = quick_check(J1x0MockUSB(status=0x00))
    assert not ready and "error or is offline" in reason


def test_quick_check_falls_back_to_a_version_query():
    link = NoStatusByte()
    assert quick_check(link) == (True, "")
    assert link.queries == [b"\x1bV\r"]
    ready, reason = quick_check(NoStatusByte(responses={b"\x1bV\r": b""}))
    assert not ready and "not answering" in reason and "busy printing" in reason
