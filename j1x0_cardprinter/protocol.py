"""Printer command stream (docs/protocol.md, docs/commands.md).

Commands are ESC + ASCII + CR. Only the single-sided YMCKO flow is implemented. The sequence
follows `SendJobHeader` / `SendPageData` case 0 in the Windows driver and was checked against a
USB capture of the real driver and port monitor printing a card (docs/captured-job.md).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from .encode import DOTS, LEVELS, LINES

ESC = b"\x1b"
CR = b"\r"


def cmd(body: str) -> bytes:
    """`ESC body CR`."""
    return ESC + body.encode("ascii") + CR


# ── Pieces ───────────────────────────────────────────────────────────────────

@dataclass
class Tuning:
    """Head tuning values sent after the job header; None = omit.

    The driver reads these from its settings block. The defaults are what the Windows driver
    sent in the captured job (every value 5). Per-panel values are indexed 0, 1, 2 = Y, M, C.
    """

    c: tuple[int | None, int | None, int | None] = (5, 5, 5)    # ESC +$C p v
    l: tuple[int | None, int | None, int | None] = (5, 5, 5)    # ESC +$L p v
    common: int | None = 5                                       # ESC +C v
    vertical: int | None = 5                                     # ESC +CV v

    def commands(self) -> list[bytes]:
        out = [cmd(f"+$C {p} {v}") for p, v in enumerate(self.c) if v is not None]
        out += [cmd(f"+$L {p} {v}") for p, v in enumerate(self.l) if v is not None]
        if self.common is not None:
            out.append(cmd(f"+C {self.common}"))
        if self.vertical is not None:
            out.append(cmd(f"+CV {self.vertical}"))
        return out


class Overlay(enum.Enum):
    NONE = "none"
    FULL = "full"       # edge-to-edge varnish (driver default)


def job_header(tuning: Tuning | None = None) -> bytes:
    """MASTER twice, JOB 0 (non-duplex), SXY 1, then tuning.

    The capture shows `ESC MASTER CR` twice in a row: the port monitor writes one when it opens the
    port and the driver writes the other. We send both so the stream is the same.
    """
    tuning = Tuning() if tuning is None else tuning
    return b"".join([cmd("MASTER"), cmd("MASTER"), cmd("JOB 0"), cmd("SXY 1"), *tuning.commands()])


def plane_block(plane: int, payload: bytes) -> bytes:
    """[ESC $F CR (plane 0 only)] ESC GS p 32 0 0 1024 640 SP, <payload> (encode.pack_plane), CR, ESC IS p CR."""
    header = ESC + f"GS {plane} {LEVELS} 0 0 {LINES} {DOTS} ".encode("ascii")
    if plane == 0:
        header = ESC + b"$F" + CR + header
    return header + payload + CR + cmd(f"IS {plane}")


def mono_block(payload: bytes) -> bytes:
    """K resin bitmap: ESC F CR, ESC G 0 0 10 640 1024 1 CR, ESC Z, 1024 x 80 bytes, CR, ESC I 10 CR."""
    header = ESC + b"F" + CR + ESC + f"G 0 0 10 {DOTS} {LINES} 1".encode("ascii") + CR + ESC + b"Z"
    return header + payload + CR + cmd("I 10")


NO_RESIN = ESC + b"F" + CR + cmd("!FF 1")


def end_of_card() -> bytes:
    """What the port monitor writes after the last data of a card: `ESC MO CR` (move the card out).

    The captured job on a YMCKO ribbon ends with that alone. (The Ghidra reading of `EndDocPort` also
    had `CR` and `ESC !FF 0 CR` for panel ribbons; the capture shows neither.)
    """
    return cmd("MO")


def overlay_block(overlay: Overlay) -> bytes:
    if overlay is Overlay.NONE:
        return b""
    return cmd("vF") + cmd("IV 30")


# ── Whole job ────────────────────────────────────────────────────────────────

@dataclass
class JobOptions:
    tuning: Tuning | None = None
    overlay: Overlay = Overlay.FULL
    # Extra bytes at the end of the data. The end-of-card commands are NOT part of it: the port
    # monitor adds them, and `sender.send_job` does the same (`end_of_card`).
    trailer: bytes = field(default=b"")


def build_job(
    plane_payloads: list[bytes], k_payload: bytes | None, options: JobOptions | None = None
) -> bytes:
    """Assemble a single-sided YMCKO job from already-encoded payloads."""
    if len(plane_payloads) != 3:
        raise ValueError("expected Y, M, C payloads")
    options = options or JobOptions()
    parts = [job_header(options.tuning)]
    parts += [plane_block(p, payload) for p, payload in enumerate(plane_payloads)]
    parts.append(mono_block(k_payload) if k_payload is not None else NO_RESIN)
    parts.append(overlay_block(options.overlay))
    parts.append(options.trailer)
    return b"".join(parts)
