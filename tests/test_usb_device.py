"""Device behaviours learned from a real J110i (0a5f:005c), reproduced with fake USB objects."""

import pytest
import usb.core
import usb.util

from j1x0_cardprinter import usb as j1usb
from j1x0_cardprinter.usb import J1x0USB, PrinterInfo, describe, get_device_id


def timeout():
    return usb.core.USBTimeoutError("Operation timed out")


def string_descriptor(text: str) -> bytes:
    body = text.encode("utf-16-le")
    return bytes((2 + len(body), 3)) + body


class Dev:
    """A device whose control requests are answered by `handlers[(bmRequestType, bRequest)]`;
    anything not listed times out, which is what a J110i does for the printer-class requests."""

    idVendor, idProduct, bus, address = 0x0A5F, 0x005C, 1, 4
    iManufacturer, iProduct, iSerialNumber = 1, 2, 0

    def __init__(self, handlers=None):
        self.handlers = handlers or {}
        self.calls = []

    def get_active_configuration(self):
        return type("Cfg", (), {"bConfigurationValue": 1})()

    def ctrl_transfer(self, bm, request, value, index, length, timeout=None):
        self.calls.append((bm, request, value, index))
        handler = self.handlers.get((bm, request))
        if handler is None:
            raise usb.core.USBTimeoutError("Operation timed out")
        return handler(value, index)


class Unlisted(Dev):
    """Some other printer: not one of the models known to ignore the status request."""

    idVendor, idProduct = 0x1234, 0x0001


class Intf:
    bInterfaceNumber, bAlternateSetting = 0, 0


# ── Identity ─────────────────────────────────────────────────────────────────

def test_strings_are_read_with_an_explicit_language_id():
    """pyusb asks for the language list first, which a J110i ignores; English works directly."""
    dev = Dev({(0x80, 0x06): lambda value, index: string_descriptor("JAVELIN") if (value, index) == (0x0301, 0x0409)
               else string_descriptor("CARD PRINTER")})
    assert j1usb._string(dev, 1) == "JAVELIN"
    assert j1usb._string(dev, 2) == "CARD PRINTER"
    assert j1usb._string(dev, 0) == ""
    assert all(call[3] == 0x0409 for call in dev.calls)          # never asked for the language list


def test_strings_fall_back_to_pyusb_when_the_direct_request_is_refused(monkeypatch):
    def refuse(value, index):
        raise usb.core.USBError("stall")

    monkeypatch.setattr(usb.util, "get_string", lambda dev, index: f"string {index}")
    assert j1usb._string(Dev({(0x80, 0x06): refuse}), 3) == "string 3"


def test_a_string_that_times_out_is_given_up_on_without_a_second_wait(monkeypatch):
    def not_called(dev, index):
        raise AssertionError("pyusb's slower route should not be tried after a timeout")

    monkeypatch.setattr(usb.util, "get_string", not_called)
    assert j1usb._string(Dev(), 3) == ""


def test_unreadable_strings_are_empty_not_an_error(monkeypatch):
    def broken(dev, index):
        raise ValueError("The device has no langid")

    monkeypatch.setattr(usb.util, "get_string", broken)
    assert j1usb._string(Dev(), 1) == ""


def test_get_device_id_gives_up_after_one_timeout():
    dev = Dev()
    assert get_device_id(dev, 0) == ""
    assert len(dev.calls) == 1                                    # not a second attempt with the other form


def test_get_device_id_tries_the_other_form_after_a_stall():
    answers = iter([usb.core.USBError("stall"), None])

    def handler(value, index):
        outcome = next(answers)
        if isinstance(outcome, Exception):
            raise outcome
        text = b"MFG:JAVELIN;MDL:J110i;"
        return bytes(((len(text) + 2) >> 8, (len(text) + 2) & 0xFF)) + text

    dev = Dev({(0xA1, 0x00): handler})
    assert get_device_id(dev, 0) == "MFG:JAVELIN;MDL:J110i;"
    assert len(dev.calls) == 2


def test_a_known_model_is_not_asked_for_an_id_it_ignores():
    dev = Dev({(0x80, 0x06): lambda value, index: string_descriptor("JAVELIN")})
    dev.iSerialNumber = 3
    info = describe(dev, Intf())
    assert info.manufacturer == "JAVELIN" and info.device_id == "" and info.serial == ""
    assert not any(call[:2] == (0xA1, 0x00) for call in dev.calls)          # no GET_DEVICE_ID
    assert not any(call[2] == 0x0303 for call in dev.calls)                 # no serial string request


def test_the_known_vid_pid_identifies_a_printer_with_no_strings():
    nameless = PrinterInfo(vid=0x0A5F, pid=0x005C, bus=1, address=4, interface=0)
    assert nameless.likely
    assert not PrinterInfo(vid=0x0A5F, pid=0x9999, bus=1, address=4, interface=0).likely


def test_the_manufacturer_string_identifies_an_unlisted_model():
    other = PrinterInfo(vid=0x0A5F, pid=0x0001, bus=1, address=4, interface=0, manufacturer="JAVELIN")
    assert other.likely


# ── Link behaviour ───────────────────────────────────────────────────────────

class Out:
    def __init__(self):
        self.writes = []

    def write(self, data, timeout=None):
        self.writes.append(bytes(data))
        return len(data)


class In:
    wMaxPacketSize = 64

    def __init__(self, *chunks):
        self.chunks = list(chunks)

    def read(self, size, timeout=None):
        if not self.chunks:
            raise usb.core.USBTimeoutError("Operation timed out")
        return self.chunks.pop(0)


def link(dev=None, ep_in=None):
    return J1x0USB(dev or Dev(), 0, Out(), ep_in or In(), 1000)


def test_a_known_model_is_never_asked_for_its_status_byte():
    usb_link = link()                                              # the J110i's own ids
    with pytest.raises(RuntimeError, match="does not answer GET_PORT_STATUS"):
        usb_link.port_status()
    assert usb_link._dev.calls == []                               # no request, so no wait for a timeout


def test_the_status_request_is_abandoned_after_the_first_timeout():
    usb_link = link(Unlisted())
    with pytest.raises(usb.core.USBTimeoutError):
        usb_link.port_status()
    calls = len(usb_link._dev.calls)
    with pytest.raises(RuntimeError, match="does not answer GET_PORT_STATUS"):
        usb_link.port_status()
    assert len(usb_link._dev.calls) == calls                      # no second wait for a reply that never comes


def test_a_working_status_request_is_returned():
    usb_link = link(Unlisted({(0xA1, 0x01): lambda value, index: b"\x18"}))
    assert usb_link.port_status().raw == 0x18 and usb_link.port_status().ok_to_open


def test_a_query_is_two_writes_the_command_then_esc_e_2():
    """What the Windows monitor does, and what a real J110i answered: `ESC V CR` + `ESC E 2 CR`."""
    usb_link = link(ep_in=In(b"J110i V1.18.25\x06"))
    assert usb_link.query(b"\x1bV\r") == b"J110i V1.18.25\x06"
    assert usb_link._ep_out.writes == [b"\x1bV\r", b"\x1bE 2\r"]


def test_a_query_collects_a_reply_split_over_packets():
    usb_link = link(ep_in=In(b"J110i V1.", b"18.25\x06"))
    assert usb_link.query(b"\x1bV\r") == b"J110i V1.18.25\x06"


def test_a_silent_printer_gives_an_empty_reply():
    assert link().query(b"\x1bV\r") == b""


# ── Writes while the printer is busy ─────────────────────────────────────────

class StallingEndpoint:
    """Bulk-OUT that behaves as listed: an int is how many bytes a write takes, "timeout" raises,
    and a partial count models pyusb returning what moved before a timeout."""

    def __init__(self, script):
        self.script = list(script)
        self.taken = bytearray()

    def write(self, data, timeout=None):
        step = self.script.pop(0) if self.script else len(data)
        if step == "timeout":
            raise usb.core.USBTimeoutError("Operation timed out")
        n = min(step, len(data))
        self.taken += bytes(data[:n])
        return n


def link_with(endpoint):
    return J1x0USB(Dev(), 0, endpoint, None, timeout=5000)


def test_a_write_resumes_after_a_partial_transfer():
    ep = StallingEndpoint([256, 0 + 10, "timeout", "timeout"])
    data = bytes(range(256)) * 40
    assert link_with(ep).write(data) == len(data)
    assert bytes(ep.taken) == data                       # nothing repeated, nothing lost


def test_a_write_waits_through_timeouts_that_moved_nothing():
    ep = StallingEndpoint(["timeout"] * 5)
    assert link_with(ep).write(b"abc") == 3 and bytes(ep.taken) == b"abc"


def test_a_write_gives_up_only_after_the_stall_limit(monkeypatch):
    clock = iter(range(0, 10_000, 100))
    monkeypatch.setattr(j1usb.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(j1usb, "WRITE_STALL", 300.0)
    ep = StallingEndpoint(["timeout"] * 50)
    with pytest.raises(usb.core.USBTimeoutError, match="accepted no data"):
        link_with(ep).write(b"abc")
    assert len(ep.script) < 50                           # it did try more than once
