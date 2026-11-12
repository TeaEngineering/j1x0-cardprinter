"""Send a job the way the Windows port monitor does (docs/status-and-transport.md section 8).

Writing the whole file in one go is not enough for a J110i: it was left showing "DOWNLOADING DATA"
and printed nothing. The Windows monitor (`jvlLMON.DLL`) instead

  * writes at most 4096 bytes at a time,
  * waits 30 to 50 ms after each write, then (when the USB status byte does not say "ready") asks the
    printer `ESC E 2 CR` and waits for a one-byte ACK (0x06) before sending more. It asks again if
    nothing comes back (up to 5 times), and stops on a NAK, which carries an error number,
  * and after the last data waits 2 s and writes the end-of-card commands (`protocol.end_of_card`).

`send_job` does the same, asking where the monitor asked in a captured print (docs/captured-job.md): after
each colour plane's data (before its `ESC IS p`), after every command that follows image data
(`ESC IS p`, `ESC I 10`, `ESC IV 30`, but not `ESC vF`) and after the final `ESC MO`. Never during the job
header and never inside an image block, where the printer would read the enquiry as pixels.

Logging: at INFO each command is logged as the stream reaches it; at DEBUG every chunk, every ACK and
every USB transfer.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from .jobparse import parse_job
from .protocol import end_of_card
from .status import ACK, NAK, parse_reply, show_bytes
from .usb import J1x0Link

log = logging.getLogger(__name__)

CHUNK = 4096            # the monitor never writes more than 0x1000 bytes per call
SETTLE = 0.05           # the monitor sleeps 30 to 50 ms after a write before asking
END_DELAY = 2.0         # and 2 s after the last data before the end-of-card commands
ACK_TIMEOUT = 10000     # ms to wait for each answer
ACK_TRIES = 18          # the monitor asks 5 times, but a panel takes the printer well over 15 s to print and rewind
REPORT_EVERY = 32768    # log progress at least this often, so a printer fault can be placed by byte offset


class PrinterError(RuntimeError):
    """The printer reported an error, or stopped answering, while a job was being sent."""

    def __init__(self, message: str, offset: int, code: int | None = None) -> None:
        super().__init__(f"{message} (after {offset} bytes were sent)")
        self.offset = offset
        self.code = code


@dataclass
class SendResult:
    bytes_sent: int
    chunks: int
    enquiries: int
    seconds: float


def _await_ack(link: J1x0Link, sent: int) -> int:
    """Ask `ESC E 2 CR` until the printer says ACK. Returns how many times it was asked."""
    for attempt in range(1, ACK_TRIES + 1):
        started = time.monotonic()
        reply = link.enquire(ACK_TIMEOUT)
        log.debug("ESC E 2 -> %s  (%.0f ms, attempt %d)", show_bytes(reply) or "nothing",
                  (time.monotonic() - started) * 1000, attempt)
        if not reply:
            continue
        if reply[0] == NAK:
            error = parse_reply(reply)
            raise PrinterError(f"the printer reports an error: {error.message}", sent, error.error_id)
        if reply[-1] == ACK:
            return attempt
        raise PrinterError(f"unexpected reply {show_bytes(reply)}", sent)
    raise PrinterError(f"the printer stopped answering ({ACK_TRIES} tries, {ACK_TIMEOUT / 1000:.0f} s each)", sent)


def _ack_points(data: bytes, segments: list) -> list[int]:
    """Offsets after which the printer is asked for an ACK, ending at the end of the data."""
    if not segments:
        return [len(data)]
    points: list[int] = []
    seen_image = False
    for seg in segments:
        if seg.kind == "image":
            seen_image = True
            points.append(seg.end)                       # the data and its CR, before ESC IS
        elif seen_image and seg.text != "ESC vF":       # ESC vF and ESC IV 30 are asked about once
            points.append(seg.end)
    if not points or points[-1] != len(data):
        points.append(len(data))
    return points


def send_job(
    link: J1x0Link,
    data: bytes,
    *,
    end: bool = True,
    chunk: int | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> SendResult:
    """Send `data` in paced chunks, then the end-of-card command.

    Pass `end=False` for data that already contains it. `progress(sent, total)` is called after each chunk.
    """
    chunk = chunk or CHUNK
    total = len(data)
    segments = parse_job(data)
    log_segments = segments if log.isEnabledFor(logging.INFO) else []
    next_segment = 0
    next_report = 0
    started = time.monotonic()
    enquiries = chunks = 0

    log.info("sending %d bytes in chunks of up to %d, then %s", total, chunk,
             "the end-of-card commands" if end else "nothing more")
    unit_start = 0
    for unit_end in _ack_points(data, segments):
        for offset in range(unit_start, unit_end, chunk):
            piece = data[offset:min(offset + chunk, unit_end)]
            sent_after = offset + len(piece)
            while next_segment < len(log_segments) and log_segments[next_segment].start < sent_after:
                seg = log_segments[next_segment]
                log.info("%8d  %s%s", seg.start, seg.text, "" if seg.ok else "   <-- PROBLEM")
                next_segment += 1
            written = link.write(piece)
            if written != len(piece):
                raise PrinterError(f"short write ({written} of {len(piece)} bytes)", offset + written)
            time.sleep(SETTLE)
            chunks += 1
            log.debug("chunk %d: %d B written (%d of %d)", chunks, len(piece), sent_after, total)
            if sent_after >= next_report:
                log.info("%8d  %d%% sent", sent_after, sent_after * 100 // total)
                next_report = sent_after + min(max(total // 10, 1), REPORT_EVERY)
            if progress:
                progress(sent_after, total)
        log.debug("end of block at byte %d: asking the printer", unit_end)
        enquiries += _await_ack(link, unit_end)
        unit_start = unit_end

    if end:
        time.sleep(END_DELAY)
        tail = end_of_card()
        log.info("%8d  end of card: %s", total, show_bytes(tail))
        link.write(tail)
        enquiries += _await_ack(link, total + len(tail))
    seconds = time.monotonic() - started
    log.info("done: %d bytes, %d chunks, %d acknowledgements, %.1f s", total, chunks, enquiries, seconds)
    return SendResult(total, chunks, enquiries, seconds)
