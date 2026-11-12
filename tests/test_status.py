import pytest

from j1x0_cardprinter.status import ERRORS, PortStatus, RIBBONS, parse_reply
from j1x0_cardprinter.usb import (
    DeviceSelectionError, PrinterInfo, choose, get_device_id, list_printers, parse_device_id,
)


# ── Port status ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, ok", [
    (0x18, True),       # selected, no error: idle and ready
    (0x38, True),       # + paper-empty
    (0x28, True),       # paper-empty bit alone is enough for the Windows monitor
    (0x20, True),
    (0x10, False),      # selected but in error
    (0x08, False),      # not selected
    (0x00, False),
])
def test_ok_to_open_follows_the_windows_rule(raw, ok):
    assert PortStatus(raw).ok_to_open is ok


def test_status_flags_and_description():
    status = PortStatus(0x18)
    assert status.selected and status.not_error and not status.paper_empty
    assert "selected" in status.describe() and "no error" in status.describe()
    assert "ERROR" in PortStatus(0x10).describe()


# ── Reply parsing ────────────────────────────────────────────────────────────

def test_success_reply_is_text_before_ack():
    reply = parse_reply(b"J110i V1.2 \x06")
    assert reply.ok and reply.text == "J110i V1.2"


def test_reply_number():
    assert parse_reply(b"2 \x06").number == 2
    assert parse_reply(b"abc\x06").number is None


def test_error_reply_maps_digits_through_the_table():
    reply = parse_reply(b"\x1505")
    assert not reply.ok and reply.error_id == 5 and reply.message == "CARD_IN_ENCODER"
    assert parse_reply(b"\x1551").message == "COVER OPEN"
    assert parse_reply(b"\x1599").message == "UNKNOWN ERROR 99"


@pytest.mark.parametrize("raw, message", [
    (b"", "NO REPLY"),
    (b"no terminator", "UNTERMINATED REPLY"),
    (b"\x15zz", "MALFORMED ERROR REPLY"),
    (b"\x15", "MALFORMED ERROR REPLY"),
])
def test_bad_replies(raw, message):
    reply = parse_reply(raw)
    assert not reply.ok and reply.message == message


def test_tables_have_the_known_entries():
    assert ERRORS[1] == "OUT OF RIBBON" and ERRORS[51] == "COVER OPEN" and ERRORS[60] == "FLASH ERROR"
    assert RIBBONS[0] == "No Ribbon" and RIBBONS[2] == "YMCKO"


# ── IEEE-1284 id and printer selection ───────────────────────────────────────

def test_parse_device_id():
    fields = parse_device_id("MFG:ACME;MODEL:Card One;COMMAND SET:ESC;junk;")
    assert fields == {"MFG": "ACME", "MDL": "Card One", "CMD": "ESC"}


def info(device_id="", product="", vid=1, pid=2, address=3):
    return PrinterInfo(vid=vid, pid=pid, bus=1, address=address, interface=0,
                       product=product, device_id=device_id)


def test_choose_the_single_likely_printer():
    printers = [info("MFG:HP;MDL:LaserJet"), info("MFG:NBS;MDL:JAVEL_CARD_PRINTER", pid=9)]
    assert choose(printers).pid == 9


def test_choose_refuses_when_nothing_or_several_match():
    with pytest.raises(DeviceSelectionError, match="no USB printers"):
        choose([])
    with pytest.raises(DeviceSelectionError, match="no USB printer looks like"):
        choose([info("MFG:HP;MDL:LaserJet")])
    with pytest.raises(DeviceSelectionError, match="several"):
        choose([info("MDL:Javelin J110i", pid=1), info("MDL:Javelin J120i", pid=2)])


class FakeIntf:
    def __init__(self, cls, number=0, alt=0):
        self.bInterfaceClass, self.bInterfaceNumber, self.bAlternateSetting = cls, number, alt


class FakeDev:
    idVendor, idProduct, bus, address = 0x1111, 0x2222, 1, 4
    iManufacturer = iProduct = iSerialNumber = 0

    def __init__(self, classes, reply=b""):
        self._cfg = [FakeIntf(c, n) for n, c in enumerate(classes)]
        self._reply = reply

    def __iter__(self):
        return iter([self._cfg])

    def get_active_configuration(self):
        return type("Cfg", (), {"bConfigurationValue": 1})()

    def ctrl_transfer(self, *args, **kwargs):
        return self._reply


def lengthprefixed(text):
    body = text.encode()
    return bytes(((len(body) + 2) >> 8, (len(body) + 2) & 0xFF)) + body


def test_get_device_id_strips_the_length_prefix():
    dev = FakeDev([7], reply=lengthprefixed("MFG:X;MDL:Y;"))
    assert get_device_id(dev, 0) == "MFG:X;MDL:Y;"


def test_list_printers_keeps_only_printer_class_devices():
    printer = FakeDev([7], reply=lengthprefixed("MFG:NBS;MDL:JAVEL_CARD_PRINTER;"))
    other = FakeDev([3, 8])
    found = list_printers(find=lambda find_all: [other, printer])
    assert len(found) == 1
    assert found[0].vid == 0x1111 and found[0].likely
    assert found[0].id_fields["MDL"] == "JAVEL_CARD_PRINTER"
