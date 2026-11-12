"""Raw USB transport for the J1x0-series card printers.

`J1x0USB` is the real-device link: find the device, detach any kernel driver, claim the printer
interface and cache the bulk endpoints.

The J110i is `0a5f:005c` (`KNOWN_PRINTERS`). Other models are found without a VID/PID:
`list_printers` enumerates every USB printer-class device and matches its strings or IEEE-1284 id
(the INF only has the hardware id `USBPRINT\\JAVEL_CARD_PRINTERJ1702A`, which Windows derives from
that id).

`J1x0MockUSB` implements the same interface without touching USB; it records what was written so
tests can inspect job streams.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol

import usb.core
import usb.util

from .status import ACK, NAK, PortStatus, show_bytes

log = logging.getLogger(__name__)

PRINTER_CLASS = 7
_CHUNK = 16 * 1024

# The printer works on each panel while it receives the job (it prints a panel, rewinds the card, then
# takes the next one), and stops reading USB for many seconds at a time while it does. A write waits
# for that: it gives up only after this long without a single byte being accepted.
WRITE_STALL = 300.0

# The Windows monitor follows every query with `ESC E 2 CR` on USB ports (`ESC E 1 CR` otherwise)
# and then polls for the reply (docs/status-and-transport.md). CONFIRMED on a J110i: without it the
# printer sends no reply at all.
QUERY_SUFFIX = b"\x1bE 2\r"

# Substrings that mark a printer's strings or IEEE-1284 id as ours.
_LIKELY = re.compile(r"javel|j1[012]0", re.IGNORECASE)

# (vendor id, product id) of units seen in the wild. The vendor id is Zebra's. This printer ignores
# the printer-class GET_DEVICE_ID, so its strings (manufacturer "JAVELIN") or this table identify it.
KNOWN_PRINTERS: dict[tuple[int, int], str] = {
    (0x0A5F, 0x005C): "J110i",
}

# Models confirmed not to answer the printer-class GET_PORT_STATUS: asking only costs a timeout.
NO_STATUS_REQUEST: frozenset[tuple[int, int]] = frozenset({(0x0A5F, 0x005C)})


class J1x0Link(Protocol):
    info: "PrinterInfo | None"

    def write(self, data: bytes) -> int: ...
    def port_status(self) -> PortStatus: ...
    def query(self, command: bytes, timeout: int = 2000) -> bytes: ...
    def enquire(self, timeout: int = 2000) -> bytes: ...
    def release(self) -> None: ...
    def __enter__(self) -> "J1x0Link": ...
    def __exit__(self, *exc: Any) -> None: ...


class DeviceSelectionError(RuntimeError):
    """No device, or more than one candidate, and no VID/PID to choose between them."""


# ── Enumeration ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PrinterInfo:
    vid: int
    pid: int
    bus: int
    address: int
    interface: int
    manufacturer: str = ""
    product: str = ""
    serial: str = ""
    device_id: str = ""             # the IEEE-1284 string, e.g. "MFG:...;MDL:...;CMD:...;"

    @property
    def id_fields(self) -> dict[str, str]:
        return parse_device_id(self.device_id)

    @property
    def likely(self) -> bool:
        if (self.vid, self.pid) in KNOWN_PRINTERS:
            return True
        return bool(_LIKELY.search(" ".join((self.device_id, self.manufacturer, self.product))))

    def summary(self) -> str:
        name = " ".join(x for x in (self.manufacturer, self.product) if x) or "(no name)"
        return f"bus {self.bus} addr {self.address}  {self.vid:04x}:{self.pid:04x}  {name}"


_ID_ALIASES = {"MANUFACTURER": "MFG", "MODEL": "MDL", "COMMAND SET": "CMD", "CLASS": "CLS",
               "DESCRIPTION": "DES"}


def parse_device_id(text: str) -> dict[str, str]:
    """Parse an IEEE-1284 device id: semicolon-separated KEY:VALUE pairs."""
    fields: dict[str, str] = {}
    for part in text.split(";"):
        key, sep, value = part.partition(":")
        if sep:
            key = key.strip().upper()
            fields[_ID_ALIASES.get(key, key)] = value.strip()
    return fields


def get_device_id(dev: Any, intf_num: int, alt: int = 0, timeout: int = 500) -> str:
    """USB printer-class GET_DEVICE_ID. Returns "" if the device does not answer.

    wValue is the configuration, wIndex is (interface << 8 | alternate setting) per the printer
    class spec; the interface-number-only form is tried as a fallback.
    A J110i ignores this request altogether, so a timeout ends the attempt at once.
    """
    try:
        config = dev.get_active_configuration().bConfigurationValue
    except usb.core.USBError:
        config = 0
    for value, index in ((config, (intf_num << 8) | alt), (0, intf_num)):
        try:
            data = bytes(dev.ctrl_transfer(0xA1, 0x00, value, index, 1024, timeout=timeout))
        except usb.core.USBTimeoutError:
            return ""
        except usb.core.USBError:
            continue
        if len(data) >= 2:
            length = (data[0] << 8) | data[1]       # big-endian, includes the 2 length bytes
            body = data[2:length] if 2 < length <= len(data) else data[2:]
            return body.decode("ascii", errors="replace").strip("\x00 ")
    return ""


def _string(dev: Any, index: int) -> str:
    """A USB string descriptor, or "" if the device will not give it.

    pyusb's `get_string` first asks for the list of language ids, and a J110i ignores that
    request (it times out), although it answers a string request that names English (0x0409)
    directly. So ask for English first and fall back to pyusb.
    """
    if not index:
        return ""
    try:
        data = bytes(dev.ctrl_transfer(0x80, 0x06, 0x0300 | index, 0x0409, 255, timeout=500))
    except usb.core.USBTimeoutError:
        return ""                       # this string is not answered; pyusb's route would only wait longer
    except usb.core.USBError:
        data = b""
    if len(data) >= 2 and data[1] == 3:
        return data[2:data[0]].decode("utf-16-le", errors="replace")
    try:
        return usb.util.get_string(dev, index) or ""
    except (usb.core.USBError, ValueError, NotImplementedError):
        return ""


def describe(dev: Any, intf: Any) -> PrinterInfo:
    known = (dev.idVendor, dev.idProduct) in KNOWN_PRINTERS
    return PrinterInfo(
        vid=dev.idVendor, pid=dev.idProduct, bus=dev.bus, address=dev.address,
        interface=intf.bInterfaceNumber,
        manufacturer=_string(dev, dev.iManufacturer), product=_string(dev, dev.iProduct),
        # A known model does not answer for its serial string or its 1284 id; each costs a
        # timeout. The printer's own serial number comes from the `ESC %SERIE` query instead.
        serial="" if known else _string(dev, dev.iSerialNumber),
        device_id="" if known else get_device_id(dev, intf.bInterfaceNumber, intf.bAlternateSetting),
    )


def list_printers(find: Callable[..., Any] | None = None) -> list[PrinterInfo]:
    """Every USB device exposing a printer-class interface, with its identity strings.

    Raises RuntimeError if libusb is not available.
    """
    find = find or usb.core.find
    printers = []
    try:
        devices = list(find(find_all=True))
    except usb.core.NoBackendError as exc:
        raise RuntimeError("libusb not found (macOS: brew install libusb)") from exc
    for dev in devices:
        try:
            intf = _printer_class_interface(dev)
            if intf is not None:
                printers.append(describe(dev, intf))
        except usb.core.USBError as exc:
            log.debug("skipping %04x:%04x: %s", dev.idVendor, dev.idProduct, exc)
    return printers


def choose(printers: list[PrinterInfo]) -> PrinterInfo:
    """Pick the one printer that looks like a J1x0, or explain why that is not possible."""
    likely = [p for p in printers if p.likely]
    if len(likely) == 1:
        return likely[0]
    if not printers:
        raise DeviceSelectionError("no USB printers found. Is it powered on and connected?")
    listing = "\n".join(f"  {p.summary()}" for p in printers)
    if not likely:
        raise DeviceSelectionError(
            f"no USB printer looks like a J1x0; pass --vid/--pid. Printers seen:\n{listing}")
    raise DeviceSelectionError(f"several printers look like a J1x0; pass --vid/--pid:\n{listing}")


# ── Real device ──────────────────────────────────────────────────────────────

class J1x0USB:
    """Real-device link."""

    def __init__(self, dev: Any, intf_num: int, ep_out: Any, ep_in: Any, timeout: int,
                 info: PrinterInfo | None = None) -> None:
        self._dev = dev
        self._intf_num = intf_num
        self._ep_out = ep_out
        self._ep_in = ep_in
        self._timeout = timeout
        self.info = info
        self._status_unsupported = (
            getattr(dev, "idVendor", None), getattr(dev, "idProduct", None)) in NO_STATUS_REQUEST

    @classmethod
    def discover(cls, vid: int | None = None, pid: int | None = None, timeout: int = 5000,
                 soft_reset: bool = False) -> "J1x0USB":
        """Open the printer. With no VID/PID, find the one printer that identifies as a J1x0.

        `soft_reset` sends the printer-class SOFT_RESET after opening. It can interrupt a job in
        progress, so it is off by default (the Windows monitor sends none).
        """
        info = None
        if vid is None or pid is None:
            info = choose(list_printers())              # already described: do not read it twice
            vid, pid = info.vid, info.pid
        dev = usb.core.find(idVendor=vid, idProduct=pid)
        if dev is None:
            raise RuntimeError(f"J1x0 printer not found (VID=0x{vid:04X} PID=0x{pid:04X}). Powered on?")

        intf = _printer_interface(dev)
        intf_num = intf.bInterfaceNumber
        info = info or describe(dev, intf)
        try:
            if dev.is_kernel_driver_active(intf_num):
                dev.detach_kernel_driver(intf_num)
        except (usb.core.USBError, NotImplementedError):
            pass
        usb.util.claim_interface(dev, intf_num)

        ep_out = _find_endpoint(intf, usb.util.ENDPOINT_OUT)
        ep_in = _find_endpoint(intf, usb.util.ENDPOINT_IN)
        if ep_out is None:
            usb.util.release_interface(dev, intf_num)
            raise RuntimeError("No bulk-OUT endpoint found on the printer interface")

        log.info("J1x0 link %s interface=%d in=%s", info.summary(), intf_num, ep_in is not None)
        link = cls(dev, intf_num, ep_out, ep_in, timeout, info)
        if soft_reset:
            link._soft_reset()
        return link

    def _soft_reset(self) -> None:
        try:
            self._dev.ctrl_transfer(0x21, 0x02, 0x0000, self._intf_num, 0, timeout=2000)
        except usb.core.USBError:
            pass

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def release(self) -> None:
        if self._dev is None:
            return
        try:
            usb.util.release_interface(self._dev, self._intf_num)
        except usb.core.USBError:
            pass
        self._dev = None

    def __enter__(self) -> "J1x0USB":
        return self

    def __exit__(self, *_: Any) -> None:
        self.release()

    # ── Transfers ────────────────────────────────────────────────────────────

    def write(self, data: bytes) -> int:
        """Stream bytes to bulk-OUT, waiting out the printer's pauses. Returns the number of bytes written.

        A timed-out transfer that moved some bytes returns that partial count (pyusb does not raise),
        and one that moved none raises; either way the rest is sent after the pause, from where it
        stopped. Raises the timeout only after `WRITE_STALL` seconds with no progress.
        """
        sent, progress_at, stalled = 0, time.monotonic(), False
        while sent < len(data):
            piece = data[sent:sent + _CHUNK]
            try:
                done = int(self._ep_out.write(piece, timeout=self._timeout))
            except usb.core.USBTimeoutError:
                done = 0
            if done:
                sent += done
                progress_at = time.monotonic()
                if stalled and sent < len(data):
                    log.info("the printer is accepting data again")
                    stalled = False
                continue
            waited = time.monotonic() - progress_at
            if waited >= WRITE_STALL:
                raise usb.core.USBTimeoutError(
                    f"the printer accepted no data for {waited:.0f} s ({sent} of {len(data)} bytes of this write)")
            if not stalled:
                log.info("the printer is not accepting data (it is printing or moving the card); waiting")
                stalled = True
        log.debug("OUT %6d B  %s", sent, show_bytes(data))
        return sent

    def port_status(self, timeout: int = 700) -> PortStatus:
        """Printer-class GET_PORT_STATUS: one status byte.

        The Windows monitor gets this through IOCTL_USBPRINT_GET_LPT_STATUS (0x160004) before it
        opens the port, which usbprint.sys answers with this class request. A J110i does not
        answer it (the request times out), so after one timeout this link stops asking and raises
        at once; callers fall back to a bulk query (`probe.quick_check`).
        """
        if self._status_unsupported:
            raise RuntimeError("this printer does not answer GET_PORT_STATUS")
        try:
            data = self._dev.ctrl_transfer(0xA1, 0x01, 0x0000, self._intf_num, 1, timeout=timeout)
        except usb.core.USBTimeoutError:
            self._status_unsupported = True
            raise
        return PortStatus(raw=int(data[0]))

    def query(self, command: bytes, timeout: int = 2000) -> bytes:
        """Send a query and read its reply. Returns the raw bytes (parse with `parse_reply`).

        Mirrors the Windows monitor: the command, then `ESC E 2 CR` as a second write, then poll
        bulk-IN until the reply ends in ACK (0x06) or is an error (NAK + two digits). Returns what
        arrived (possibly nothing) at the timeout. Confirmed on a J110i: `ESC V CR` answers
        `J110i V1.18.25` + ACK, and sends nothing if the `ESC E 2 CR` is left out.
        """
        self.write(command)
        return self.enquire(timeout)

    def enquire(self, timeout: int = 2000) -> bytes:
        """Send `ESC E 2 CR` and read the answer: ACK, or an error (NAK + two digits), or nothing.

        This is the Windows monitor's status poll. After a query it makes the printer answer; after a
        chunk of job data it is the "ready for more?" handshake (`sender.send_job`).
        """
        if self._ep_in is None:
            raise RuntimeError("Printer interface has no bulk-IN endpoint")
        self.write(QUERY_SUFFIX)
        reply = bytearray()
        while True:
            try:
                chunk = bytes(self._ep_in.read(self._ep_in.wMaxPacketSize, timeout=timeout))
            except usb.core.USBTimeoutError:
                log.debug("IN  timeout after %d ms (have %d B)", timeout, len(reply))
                break
            log.debug("IN  %6d B  %s", len(chunk), show_bytes(chunk))
            reply += chunk
            if ACK in reply or (reply[:1] == bytes((NAK,)) and len(reply) >= 3):
                break
        return bytes(reply)


class J1x0MockUSB:
    """In-process double. `.sent` records writes, `.queries` records query commands.

    By default the mock looks like an idle printer with a YMCKO ribbon; override with `status` and
    `responses` (command bytes -> raw reply bytes).
    """

    DEFAULT_RESPONSES = {
        b"\x1bV\r": b"J1x0 MOCK 1.0\x06",
        b"\x1b!RIBBON\r": b"2\x06",
        b"\x1b!RIBLEN\r": b"100\x06",
        b"\x1b%SERIE\r": b"MOCK0001\x06",
        b"\x1b!V 15\r": b"26\x06",
        b"\x1b!V 20\r": b"1\x06",
        b"\x1b!NL\r": b"!1234 \x06",
    }

    def __init__(self, responses: Optional[dict[bytes, bytes]] = None, status: int = 0x18,
                 info: PrinterInfo | None = None, enquiry_replies: list[bytes] | None = None) -> None:
        self.sent: list[bytes] = []
        self.queries: list[bytes] = []
        self.enquiries = 0
        self.events: list[tuple] = []           # ("write", n) and ("enquire",) in the order they happened
        self._responses = {**self.DEFAULT_RESPONSES, **(responses or {})}
        self._status = status
        self._enquiry_replies = list(enquiry_replies or [])
        self.info = info

    def write(self, data: bytes) -> int:
        self.sent.append(bytes(data))
        self.events.append(("write", len(data)))
        return len(data)

    def port_status(self) -> PortStatus:
        return PortStatus(raw=self._status)

    def query(self, command: bytes, timeout: int = 2000) -> bytes:
        self.queries.append(bytes(command))
        return self._responses.get(command, b"")

    def enquire(self, timeout: int = 2000) -> bytes:
        """The answer to `ESC E 2 CR`: ACK, unless `enquiry_replies` scripts something else."""
        self.enquiries += 1
        self.events.append(("enquire",))
        return self._enquiry_replies.pop(0) if self._enquiry_replies else bytes((ACK,))

    @property
    def stream(self) -> bytes:
        return b"".join(self.sent)

    def release(self) -> None:
        pass

    def __enter__(self) -> "J1x0MockUSB":
        return self

    def __exit__(self, *_: Any) -> None:
        pass


# ── Internal helpers ─────────────────────────────────────────────────────────

def _printer_class_interface(dev: Any) -> Optional[Any]:
    """First interface of USB class 7 (printer), or None."""
    for cfg in dev:
        for intf in cfg:
            if intf.bInterfaceClass == PRINTER_CLASS:
                return intf
    return None


def _printer_interface(dev: Any) -> Any:
    """The printer-class interface, else the first interface."""
    intf = _printer_class_interface(dev)
    if intf is not None:
        return intf
    for cfg in dev:
        for first in cfg:
            return first
    raise RuntimeError("Device has no interfaces")


def _find_endpoint(intf: Any, direction: int) -> Optional[Any]:
    for ep in intf:
        if (usb.util.endpoint_direction(ep.bEndpointAddress) == direction
                and usb.util.endpoint_type(ep.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK):
            return ep
    return None
