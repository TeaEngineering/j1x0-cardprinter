"""Raw USB transport for the J1x0-series card printers.

`J1x0USB` is the real-device link, modelled on rollingmill's `MdxUSB`: find the device,
detach any kernel driver, claim the printer interface, cache the bulk endpoints, then send the
two USB printer-class steps (GET_DEVICE_ID, SOFT_RESET). The VID/PID are not known yet (the
INF only has the hardware id `USBPRINT\\JAVEL_CARD_PRINTERJ1702A`), so they are required.

`J1x0MockUSB` implements the same interface without touching USB; it records what was
written so tests can inspect job streams.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Protocol

import usb.core
import usb.util

log = logging.getLogger(__name__)

ACK = 0x06
_CHUNK = 16 * 1024


class J1x0Link(Protocol):
    def write(self, data: bytes) -> int: ...
    def query(self, command: bytes, timeout: int = 2000) -> bytes: ...
    def release(self) -> None: ...
    def __enter__(self) -> "J1x0Link": ...
    def __exit__(self, *exc: Any) -> None: ...


class J1x0USB:
    """Real-device link."""

    def __init__(self, dev: Any, intf_num: int, ep_out: Any, ep_in: Any, timeout: int) -> None:
        self._dev = dev
        self._intf_num = intf_num
        self._ep_out = ep_out
        self._ep_in = ep_in
        self._timeout = timeout

    @classmethod
    def discover(cls, vid: int, pid: int, timeout: int = 5000) -> "J1x0USB":
        dev = usb.core.find(idVendor=vid, idProduct=pid)
        if dev is None:
            raise RuntimeError(f"J1x0 printer not found (VID=0x{vid:04X} PID=0x{pid:04X}). Powered on?")

        intf = _printer_interface(dev)
        intf_num = intf.bInterfaceNumber
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

        log.info("J1x0 link bus=%d addr=%d serial=%r interface=%d in=%s",
                 dev.bus, dev.address, dev.serial_number, intf_num, ep_in is not None)
        link = cls(dev, intf_num, ep_out, ep_in, timeout)
        link._printer_class_handshake()
        return link

    def _printer_class_handshake(self) -> None:
        """GET_DEVICE_ID then SOFT_RESET. Failures are non-fatal."""
        try:
            data = self._dev.ctrl_transfer(0xA1, 0x00, 0x0000, self._intf_num, 1024, timeout=2000)
            log.info("GET_DEVICE_ID: %s", bytes(data).decode("ascii", errors="replace"))
        except usb.core.USBError:
            pass
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

    def query(self, command: bytes, timeout: int = 2000) -> bytes:
        """Send a query and read until the ACK (0x06) terminator, which is stripped.

        UNVERIFIED: that replies arrive on bulk-IN and end in 0x06 comes from the Windows
        driver's GetPrinterData handling (docs/status-and-transport.md).
        """
        if self._ep_in is None:
            raise RuntimeError("Printer interface has no bulk-IN endpoint")
        self.write(command)
        reply = bytearray()
        while True:
            try:
                chunk = bytes(self._ep_in.read(self._ep_in.wMaxPacketSize, timeout=timeout))
            except usb.core.USBTimeoutError:
                break
            reply += chunk
            if ACK in chunk:
                reply = reply[: reply.index(ACK)]
                break
        return bytes(reply)


class J1x0MockUSB:
    """In-process double. Records every write; `responses` maps a query to its reply bytes."""

    def __init__(self, responses: Optional[dict[bytes, bytes]] = None) -> None:
        self.sent: list[bytes] = []
        self._responses = dict(responses or {})

    def write(self, data: bytes) -> int:
        self.sent.append(bytes(data))
        return len(data)

    def query(self, command: bytes, timeout: int = 2000) -> bytes:
        self.sent.append(bytes(command))
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

def _printer_interface(dev: Any) -> Any:
    """First interface of USB class 7 (printer), else the first interface."""
    first = None
    for cfg in dev:
        for intf in cfg:
            first = first or intf
            if intf.bInterfaceClass == 7:
                return intf
    if first is None:
        raise RuntimeError("Device has no interfaces")
    return first


def _find_endpoint(intf: Any, direction: int) -> Optional[Any]:
    for ep in intf:
        if (usb.util.endpoint_direction(ep.bEndpointAddress) == direction
                and usb.util.endpoint_type(ep.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK):
            return ep
    return None
