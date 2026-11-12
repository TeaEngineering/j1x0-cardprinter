"""Raw USB transport for the J1x0-series card printers.

`J1x0USB` is the real-device link, modelled on rollingmill's `MdxUSB`: find the device, detach any
kernel driver, claim the printer interface and cache the bulk endpoints.

The VID/PID are not known yet (the INF only has the hardware id
`USBPRINT\\JAVEL_CARD_PRINTERJ1702A`, which Windows derives from the printer's IEEE-1284 device
id). `list_printers` therefore enumerates every USB printer-class device and reads that id, so the
printer can be found without a VID/PID.

`J1x0MockUSB` implements the same interface without touching USB; it records what was written so
tests can inspect job streams.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol

import usb.core
import usb.util

from .status import ACK, NAK, PortStatus

log = logging.getLogger(__name__)

PRINTER_CLASS = 7
_CHUNK = 16 * 1024

# The Windows monitor follows every query with `ESC E 2 CR` on USB ports (`ESC E 1 CR` otherwise)
# and then polls for the reply (docs/status-and-transport.md). UNVERIFIED on hardware.
QUERY_SUFFIX = b"\x1bE 2\r"

# Substrings that mark a printer's IEEE-1284 id as ours. The INF shows ids built from
# "JAVEL_CARD_PRINTER" and model codes J100/J110/J120. UNVERIFIED against a real id string.
_LIKELY = re.compile(r"javel|j1[012]0", re.IGNORECASE)


class J1x0Link(Protocol):
    info: "PrinterInfo | None"

    def write(self, data: bytes) -> int: ...
    def port_status(self) -> PortStatus: ...
    def query(self, command: bytes, timeout: int = 2000) -> bytes: ...
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


def get_device_id(dev: Any, intf_num: int, alt: int = 0) -> str:
    """USB printer-class GET_DEVICE_ID. Returns "" if the device does not answer.

    wValue is the configuration, wIndex is (interface << 8 | alternate setting) per the printer
    class spec; the interface-number-only form is tried as a fallback (what rollingmill uses).
    """
    try:
        config = dev.get_active_configuration().bConfigurationValue
    except usb.core.USBError:
        config = 0
    for value, index in ((config, (intf_num << 8) | alt), (0, intf_num)):
        try:
            data = bytes(dev.ctrl_transfer(0xA1, 0x00, value, index, 1024, timeout=2000))
        except usb.core.USBError:
            continue
        if len(data) >= 2:
            length = (data[0] << 8) | data[1]       # big-endian, includes the 2 length bytes
            body = data[2:length] if 2 < length <= len(data) else data[2:]
            return body.decode("ascii", errors="replace").strip("\x00 ")
    return ""


def _string(dev: Any, index: int) -> str:
    if not index:
        return ""
    try:
        return usb.util.get_string(dev, index) or ""
    except (usb.core.USBError, ValueError, NotImplementedError):
        return ""


def describe(dev: Any, intf: Any) -> PrinterInfo:
    return PrinterInfo(
        vid=dev.idVendor, pid=dev.idProduct, bus=dev.bus, address=dev.address,
        interface=intf.bInterfaceNumber,
        manufacturer=_string(dev, dev.iManufacturer), product=_string(dev, dev.iProduct),
        serial=_string(dev, dev.iSerialNumber),
        device_id=get_device_id(dev, intf.bInterfaceNumber, intf.bAlternateSetting),
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

    @classmethod
    def discover(cls, vid: int | None = None, pid: int | None = None, timeout: int = 5000,
                 soft_reset: bool = False) -> "J1x0USB":
        """Open the printer. With no VID/PID, find the one printer that identifies as a J1x0.

        `soft_reset` sends the printer-class SOFT_RESET after opening (rollingmill does this for
        the MDX). It can interrupt a job in progress, so it is off by default.
        """
        if vid is None or pid is None:
            chosen = choose(list_printers())
            vid, pid = chosen.vid, chosen.pid
        dev = usb.core.find(idVendor=vid, idProduct=pid)
        if dev is None:
            raise RuntimeError(f"J1x0 printer not found (VID=0x{vid:04X} PID=0x{pid:04X}). Powered on?")

        intf = _printer_interface(dev)
        intf_num = intf.bInterfaceNumber
        info = describe(dev, intf)
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
        """Stream bytes to bulk-OUT in chunks. Returns the number of bytes written."""
        total = 0
        for start in range(0, len(data), _CHUNK):
            total += int(self._ep_out.write(data[start:start + _CHUNK], timeout=self._timeout))
        return total

    def port_status(self) -> PortStatus:
        """Printer-class GET_PORT_STATUS: one status byte.

        The Windows monitor gets this through IOCTL_USBPRINT_GET_LPT_STATUS (0x160004) before it
        opens the port, which usbprint.sys answers with this class request.
        """
        data = self._dev.ctrl_transfer(0xA1, 0x01, 0x0000, self._intf_num, 1, timeout=2000)
        return PortStatus(raw=int(data[0]))

    def query(self, command: bytes, timeout: int = 2000) -> bytes:
        """Send a query and read its reply. Returns the raw bytes (parse with `parse_reply`).

        Mirrors the Windows monitor: command, then `ESC E 2 CR`, then poll bulk-IN until the reply
        ends in ACK (0x06) or is an error (NAK + two digits). Returns what arrived (possibly
        nothing) at the timeout. UNVERIFIED on hardware.
        """
        if self._ep_in is None:
            raise RuntimeError("Printer interface has no bulk-IN endpoint")
        self.write(command + QUERY_SUFFIX)
        reply = bytearray()
        while True:
            try:
                reply += bytes(self._ep_in.read(self._ep_in.wMaxPacketSize, timeout=timeout))
            except usb.core.USBTimeoutError:
                break
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
                 info: PrinterInfo | None = None) -> None:
        self.sent: list[bytes] = []
        self.queries: list[bytes] = []
        self._responses = {**self.DEFAULT_RESPONSES, **(responses or {})}
        self._status = status
        self.info = info

    def write(self, data: bytes) -> int:
        self.sent.append(bytes(data))
        return len(data)

    def port_status(self) -> PortStatus:
        return PortStatus(raw=self._status)

    def query(self, command: bytes, timeout: int = 2000) -> bytes:
        self.queries.append(bytes(command))
        return self._responses.get(command, b"")

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
