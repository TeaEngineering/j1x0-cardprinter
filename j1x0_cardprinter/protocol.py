"""Printer command stream (docs/protocol.md, docs/commands.md).

Commands are ESC + ASCII + CR. Only the single-sided YMCKO flow is implemented; the
sequence follows `SendJobHeader` / `SendPageData` case 0 in the Windows driver. Parts the
driver analysis has not settled are marked UNVERIFIED and are the first things to check
against hardware (docs/open-questions.md).
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
    """Head tuning bytes sent after the job header. Defaults are unknown (Q8): None = omit.

    The driver reads these from its settings block; the help file calls them contrast and
    intensity. Per-panel values are indexed 0, 1, 2 = Y, M, C.
    """

    c: tuple[int | None, int | None, int | None] = (None, None, None)    # ESC +$C p v
    l: tuple[int | None, int | None, int | None] = (None, None, None)    # ESC +$L p v
    common: int | None = None                                            # ESC +C v
    vertical: int | None = None                                          # ESC +CV v

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
    """MASTER, JOB 0 (non-duplex), SXY 1, then tuning."""
    parts = [cmd("MASTER"), cmd("JOB 0"), cmd("SXY 1")]
    if tuning is not None:
        parts += tuning.commands()
    return b"".join(parts)


def plane_block(plane: int, payload: bytes) -> bytes:
    """ESC $F CR ESC GS p 32 0 0 1024 640 SP <payload> CR ESC IS p CR."""
    header = ESC + b"$F" + CR + ESC + f"GS {plane} {LEVELS} 0 0 {LINES} {DOTS} ".encode("ascii")
    return header + payload + CR + cmd(f"IS {plane}")


def mono_block(payload: bytes) -> bytes:
    """K resin bitmap. UNVERIFIED: the header's `10` is a literal copied from the driver."""
    header = ESC + b"F" + CR + ESC + f"G 0 0 10 {DOTS} {LINES} 1".encode("ascii") + CR + ESC + b"Z"
    return header + payload + CR + cmd("I 10")


NO_RESIN = ESC + b"F" + CR + cmd("!FF 1")


def overlay_block(overlay: Overlay) -> bytes:
    if overlay is Overlay.NONE:
        return b""
    return cmd("vF") + cmd("IV 30")


# ── Whole job ────────────────────────────────────────────────────────────────

@dataclass
class JobOptions:
    tuning: Tuning | None = None
    overlay: Overlay = Overlay.FULL
    # What follows the overlay (feed/eject) is not settled (Q7). `cmd("MO")` ejects the card;
    # left empty by default so nothing unverified is sent.
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
