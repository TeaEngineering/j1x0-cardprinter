"""Link-layer check: is the USB path alive and is the printer ready to take a job?

Runs the same reads the Windows monitor does when it opens the port (docs/status-and-transport.md):
the printer-class status byte, then `V` (model/firmware), `!RIBBON` and `!RIBLEN`. It also reads the
other fields of the Windows "Device Information" dialog (serial, head resistance, magnetic
encoder, panels printed, smart-card type). Nothing is printed and no job is started (no `MASTER`).

Readiness depends on the first three queries, and on the status byte when the printer provides
one. A printer that does not answer the USB status request is not penalised for it: the printer's
own replies decide, and the `port status` line just says it is unavailable. The Device Information fields are informational and
show as N/A when the printer does not answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .status import RIBBONS, PortStatus, Reply, parse_panels, parse_reply, smart_card_type
from .usb import J1x0Link, PrinterInfo

CMD_VERSION = b"\x1bV\r"
CMD_RIBBON = b"\x1b!RIBBON\r"
CMD_RIBBON_LEFT = b"\x1b!RIBLEN\r"
CMD_SERIAL = b"\x1b%SERIE\r"
CMD_HEAD_RESISTANCE = b"\x1b!V 15\r"
CMD_MAGNETIC = b"\x1b!V 20\r"
CMD_PANELS = b"\x1b!NL\r"
LOW_RIBBON = (20, 10, 5)        # the Windows monitor pops up a warning at these counts


@dataclass
class LinkReport:
    info: PrinterInfo | None = None
    status: PortStatus | None = None
    status_error: str = ""
    model: Reply | None = None
    ribbon: Reply | None = None
    remaining: Reply | None = None
    serial: Reply | None = None
    head_resistance: Reply | None = None
    magnetic: Reply | None = None
    panels: Reply | None = None
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not self.problems

    @property
    def smart_card(self) -> str:
        """Encoder type from the `ESC V` reply; "N/A" if there was no usable reply."""
        if self.model is None or not self.model.ok:
            return "N/A"
        return smart_card_type(self.model.text)

    @property
    def magnetic_encoder(self) -> str:
        if self.magnetic is None or not self.magnetic.ok:
            return "N/A"
        return "Yes" if self.magnetic.text.startswith("1") else "No"

    @property
    def panels_printed(self) -> str:
        if self.panels is None or not self.panels.ok:
            return "N/A"
        count = parse_panels(self.panels.text)
        return str(count) if count is not None else self.panels.text or "N/A"

    def lines(self) -> list[str]:
        out = []
        if self.info is not None:
            out.append(f"device:           {self.info.summary()}")
            if self.info.device_id:
                out.append(f"1284 id:          {self.info.device_id}")
            if self.info.serial:
                out.append(f"usb serial:       {self.info.serial}")
        if self.status is not None:
            out.append(f"port status:      {self.status.describe()}")
        elif self.status_error:
            out.append(f"port status:      unavailable ({self.status_error})")
        # The Device Information fields, in the order of the Windows dialog / help page.
        out.append(f"model/firmware:   {_show(self.model)}")
        out.append(f"serial number:    {_show(self.serial)}")
        out.append(f"head resistance:  {_show(self.head_resistance)}")
        out.append(f"magnetic encoder: {self.magnetic_encoder}")
        out.append(f"smart card:       {self.smart_card}")
        out.append(f"ribbon type:      {self._ribbon_text()}")
        out.append(f"remaining count:  {self._remaining_text()}")
        out.append(f"panels printed:   {self.panels_printed}")
        out += [f"warning:          {w}" for w in self.warnings]
        out.append("result:           READY" if self.ready
                   else "result:           NOT READY - " + "; ".join(self.problems))
        return out

    def _ribbon_text(self) -> str:
        if self.ribbon is None or not self.ribbon.ok:
            return _show(self.ribbon)
        number = self.ribbon.number
        if number is None:
            return str(self.ribbon)
        return f"{number} ({RIBBONS.get(number, 'unrecognised id')})"

    def _remaining_text(self) -> str:
        if self.remaining is None or not self.remaining.ok:
            return "N/A"
        return "N/A (monochrome ribbon)" if self.remaining.number == 0 else str(self.remaining)


def _show(reply: Reply | None) -> str:
    """The reply text, or N/A with the reason when it failed."""
    if reply is None:
        return "N/A"
    if reply.ok:
        return str(reply)
    reason = reply.message if reply.error_id is None else f"{reply.message}, error {reply.error_id}"
    return f"N/A ({reason})"


def _ask(link: J1x0Link, command: bytes, timeout: int) -> Reply:
    try:
        return parse_reply(link.query(command, timeout))
    except RuntimeError as exc:                     # e.g. no bulk-IN endpoint
        return Reply(ok=False, message=str(exc))


def quick_check(link: J1x0Link, timeout: int = 1500) -> tuple[bool, str]:
    """A cheap readiness test for polling: `(ready, reason)`.

    Uses the USB status byte when the printer provides one, otherwise a single version query.
    """
    try:
        status = link.port_status()
    except Exception:                               # not supported, or no answer
        reply = _ask(link, CMD_VERSION, timeout)
        if reply.ok:
            return True, ""
        return False, f"it is not answering ({reply.message}); it may be busy printing"
    if status.ok_to_open:
        return True, ""
    return False, "it reports an error or is offline"


def check_link(link: J1x0Link, timeout: int = 2000, details: bool = True) -> LinkReport:
    """Read status and identity from an open link. Never raises for a printer that is merely
    unresponsive; those become `problems`.

    `details=False` skips the informational Device Information queries, so a silent printer costs
    three timeouts instead of seven (used as the gate before printing).
    """
    report = LinkReport(info=link.info)

    try:
        report.status = link.port_status()
    except Exception as exc:                        # usb.core.USBError and friends
        report.status_error = str(exc)
    else:
        if not report.status.ok_to_open:
            report.problems.append("printer reports an error or is offline")

    report.model = _ask(link, CMD_VERSION, timeout)
    if not report.model.ok:
        report.problems.append(f"no valid reply to the version query ({report.model.message})")

    report.ribbon = _ask(link, CMD_RIBBON, timeout)
    if report.ribbon.ok:
        if report.ribbon.number == 0:
            report.problems.append("no ribbon installed")
    else:
        report.warnings.append(f"ribbon query failed ({report.ribbon.message})")

    report.remaining = _ask(link, CMD_RIBBON_LEFT, timeout)
    if report.remaining.ok:
        left = report.remaining.number
        if left is not None and left in LOW_RIBBON:
            report.warnings.append(f"ribbon is running low ({left} left)")

    if not details:
        return report

    # Informational fields (the rest of the Windows Device Information dialog).
    report.serial = _ask(link, CMD_SERIAL, timeout)
    report.head_resistance = _ask(link, CMD_HEAD_RESISTANCE, timeout)
    report.magnetic = _ask(link, CMD_MAGNETIC, timeout)
    report.panels = _ask(link, CMD_PANELS, timeout)
    return report
