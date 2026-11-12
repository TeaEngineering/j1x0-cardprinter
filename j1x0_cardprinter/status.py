"""Decoding of printer status, replies and error codes (docs/status-and-transport.md).

Everything here is lifted from the Windows language monitor (`jvlLMON.DLL`): the error table at
`0x18001d320`, the reply framing in `FUN_180004e50`, and the port-status test in `FUN_180005ca0`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ACK = 0x06
NAK = 0x15

# ── USB printer-class port status byte ───────────────────────────────────────

@dataclass(frozen=True)
class PortStatus:
    """The 1-byte GET_PORT_STATUS value (the LPT-style status register)."""

    raw: int

    @property
    def paper_empty(self) -> bool:      # bit 5: the card printer uses it for "needs attention"
        return bool(self.raw & 0x20)

    @property
    def selected(self) -> bool:         # bit 4
        return bool(self.raw & 0x10)

    @property
    def not_error(self) -> bool:        # bit 3
        return bool(self.raw & 0x08)

    @property
    def ok_to_open(self) -> bool:
        """The Windows monitor's test for opening the port: bit 5 set, or selected and no error."""
        return self.paper_empty or (self.raw & 0x38) == 0x18

    def describe(self) -> str:
        flags = [
            "selected" if self.selected else "not selected",
            "no error" if self.not_error else "ERROR",
        ]
        if self.paper_empty:
            flags.append("paper-empty/attention")
        return f"0x{self.raw:02X} ({', '.join(flags)})"


# ── Replies ──────────────────────────────────────────────────────────────────

# Printer error id -> message, from the monitor's table (ids with no entry are unassigned).
ERRORS: dict[int, str] = {
    1: "OUT OF RIBBON", 2: "TEMPERATURE", 3: "MECHANICAL ERROR", 4: "OUT OF CARD",
    5: "CARD_IN_ENCODER", 6: "CARD_NOT_IN_ENCODER", 7: "HEAD OPEN", 8: "HEAD OPEN",
    9: "REMOVE RIBBON", 10: "COMMAND ERROR", 11: "COMMAND ERROR", 12: "COMMAND ERROR",
    13: "COMMAND ERROR", 14: "COMMAND ERROR", 16: "INSERT CARD", 20: "COMMAND ERROR",
    21: "COMMAND ERROR", 22: "COMMAND ERROR", 30: "COMMAND ERROR", 31: "COMMAND ERROR",
    32: "COMMAND ERROR", 33: "COMMAND ERROR", 34: "COMMAND ERROR", 35: "CHECK RIBBON",
    40: "PARAMETERS ERROR", 41: "ENCODING ERROR", 42: "READING ERROR", 43: "MECHANICAL ERROR",
    44: "MAGNETIC ERROR", 45: "BLANK TRACK", 47: "ROTATION ERROR", 49: "ENCODING ERROR",
    50: "MAGNETIC ERROR", 51: "COVER OPEN", 53: "NO ACCESS", 54: "SEQUENCE_ERROR",
    55: "ERROR PROX", 56: "ERROR CONTACT DATA", 57: "ERROR PROX DATA", 60: "FLASH ERROR",
}

# Ribbon id -> name, from jvRIBBON.INI (the ids the printer reports for !RIBBON).
RIBBONS: dict[int, str] = {
    0: "No Ribbon", 1: "Unknown ribbon", 2: "YMCKO", 3: "YMCK", 4: "YMCKK", 5: "YMCKOK",
    6: "YMC", 7: "K", 8: "K Red", 9: "K Green", 10: "K Blue", 11: "K Gold", 12: "K Silver",
    13: "K White", 14: "YMCKO", 15: "YMCK", 16: "KdO", 17: "KrO", 18: "Scratch off",
    19: "Hologram Genuine Secure", 20: "Hologram Pcard", 21: "Clear Ribbon", 22: "YMCKUv",
    23: "YMCUvK", 24: "YMCUvK", 25: "YMCKUv", 26: "YMCUvK", 27: "YMCUvK", 28: "Thermal",
    30: "1/2 Panel YMCKO",
}


# Smart-card encoder type: substrings of the `ESC V` reply, checked in this order, as the Windows
# Device Information dialog does (`FillDeviceInfoDialog` in jvlUI.DLL). No match = no encoder.
SMART_CARDS: tuple[tuple[str, str], ...] = (
    ("0iB", "Contact Encoder"),
    ("0iC", "Contact Encoder and HID iCLASS"),
    ("0iD", "Contact Encoder and MIFARE"),
    ("0iE", "Contact Station"),
    ("0iF", "HID iCLASS"),
    ("0iH", "MIFARE contactless"),
)


def smart_card_type(version_text: str) -> str:
    """Name of the smart-card encoder advertised in the `ESC V` reply, or "No"."""
    for token, name in SMART_CARDS:
        if token in version_text:
            return name
    return "No"


def parse_panels(text: str) -> int | None:
    """Panels-printed count from the `ESC !NL` reply: an optional `!`, then an integer."""
    match = re.match(r"\s*!?\s*(\d+)", text)
    return int(match.group(1)) if match else None



def show_bytes(data: bytes, limit: int = 40) -> str:
    """Printable rendering of protocol bytes for logs: `<ESC>V<CR>`, `<ACK>`, `\\x1f`, ..."""
    names = {0x1B: "<ESC>", 0x0D: "<CR>", 0x06: "<ACK>", 0x15: "<NAK>", 0x0A: "<LF>"}
    out = []
    for byte in data[:limit]:
        out.append(names.get(byte) or (chr(byte) if 0x20 <= byte < 0x7F else f"\\x{byte:02x}"))
    more = f"...(+{len(data) - limit} B)" if len(data) > limit else ""
    return "".join(out) + more


@dataclass(frozen=True)
class Reply:
    """A parsed reply to a query command.

    Success replies are text ending in ACK (0x06). Errors start with NAK (0x15) followed by two
    decimal digits (an index into `ERRORS`).
    """

    ok: bool
    text: str = ""
    error_id: int | None = None
    message: str = ""

    @property
    def number(self) -> int | None:
        """Leading decimal integer of the text, if any."""
        match = re.match(r"\s*(\d+)", self.text)
        return int(match.group(1)) if match else None

    def __str__(self) -> str:
        return self.text if self.ok else f"{self.message} (error {self.error_id})"


def parse_reply(raw: bytes) -> Reply:
    """Interpret the bytes read after a query. Empty input is a failed (no reply) result."""
    if not raw:
        return Reply(ok=False, message="NO REPLY")
    if raw[0] == NAK:
        digits = raw[1:3]
        if len(digits) == 2 and all(0x30 <= b <= 0x39 for b in digits):
            error_id = (digits[0] & 0x0F) * 10 + (digits[1] & 0x0F)
            return Reply(ok=False, error_id=error_id,
                         message=ERRORS.get(error_id, f"UNKNOWN ERROR {error_id}"))
        return Reply(ok=False, message="MALFORMED ERROR REPLY")
    if ACK not in raw:
        return Reply(ok=False, message="UNTERMINATED REPLY")
    body = raw[: raw.index(ACK)]
    return Reply(ok=True, text=body.decode("ascii", errors="replace").strip(" \r\n"))
