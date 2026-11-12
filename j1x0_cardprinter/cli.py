"""Command line and web UI for the card printer.

    j1x0                                         # open the web UI (designs from ~/.j1x0/*.svg)
    j1x0 fields  card.svg
    j1x0 preview card.svg -s firstName=Ada -s lastName=Lovelace -o out/
    j1x0 render  card.svg -s firstName=Ada -s lastName=Lovelace -o job.bin   # no printer needed
    j1x0 send                                                                # link check only
    j1x0 send    job.bin                                                     # check, then send
    j1x0 print   card.svg -s firstName=Ada                                   # render + send

`render` and `send` can run on different machines. `print` renders to a temporary job file and
sends that file, so it behaves exactly like `render` followed by `send`.

`send` always opens the link first and checks it (USB status, model, ribbon). With no job file
that is all it does, then it exits: 0 if the printer is ready, 1 if not. With a job file it
stops without sending if the printer is not ready (override with --force). The printer is found
automatically from its IEEE-1284 id; pass --vid/--pid if that picks the wrong one or none.

A job is sent the way the Windows driver does: in chunks of up to 4096 bytes, each followed by an
`ESC E 2` enquiry that the printer must ACK, then the end-of-card commands (`--raw` writes the
file as it is instead). `-v` logs the protocol: every command as it is sent, annotated. `-vv` also
logs every USB transfer and every acknowledgement with timing. Either can go before or after the
sub-command.
"""

from __future__ import annotations

import argparse
import faulthandler
import logging
import shutil
import sys
import tempfile
import time
from pathlib import Path

import usb.core
from PIL import ImageOps

from .library import default_dir
from .printer import Printer
from .probe import check_link
from .raster import RENDERERS, composite, make_rasteriser, render_card
from .sender import PrinterError, send_job
from .protocol import cmd
from .status import show_bytes
from .template import Template
from .usb import J1x0USB

JOB_PREFIX = b"\x1bMASTER\r"
HANG_DUMP_SECONDS = 120         # a USB call blocked this long gets its stack written to the log (the printer pauses for tens of seconds between panels)

log = logging.getLogger(__name__)


def _parse_sets(pairs: list[str]) -> dict[str, str]:
    values = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if not sep:
            raise SystemExit(f"--set expects name=value, got {pair!r}")
        values[name] = value
    return values


def _load_card(args: argparse.Namespace) -> tuple[Template, dict[str, str]]:
    """Parse the template and values; exit with status 2 if any field has no value."""
    template = Template.from_file(args.template)
    values = _parse_sets(args.set)
    missing = [f for f in template.fields() if f not in values]
    if missing:
        print(f"missing values for: {', '.join(missing)}", file=sys.stderr)
        raise SystemExit(2)
    return template, values


def _rasteriser(args: argparse.Namespace, template: Template):
    """The chosen renderer; warns when cairosvg is chosen for a design it cannot draw properly."""
    if args.renderer == "cairosvg":
        problems = template.cairosvg_problems()
        if problems:
            print("warning: cairosvg cannot draw: " + ", ".join(problems)
                  + " (use --renderer inkscape for a faithful render)", file=sys.stderr)
    return make_rasteriser(args.renderer)


def _render_job(args: argparse.Namespace) -> bytes:
    template, values = _load_card(args)
    printer = Printer(
        rasteriser=_rasteriser(args, template),
        seed=args.seed,
        sharpening=args.sharpening,
    )
    return printer.build(template, values)


def _setup_logging(verbose: int, logfile: Path | None = None) -> logging.Handler | None:
    """0: warnings only; 1 (`-v`): the protocol, command by command; 2 (`-vv`): every transfer.

    With `logfile` everything is also written there at full detail, flushed line by line so the trail
    survives a hang or a kill. Returns the file handler, if any.
    """
    level = logging.WARNING if verbose <= 0 else logging.INFO if verbose == 1 else logging.DEBUG
    formatter = logging.Formatter("%(relativeCreated)7.0f ms  %(message)s")
    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(formatter)
    console.addFilter(lambda record: not getattr(record, "file_only", False))
    handlers: list[logging.Handler] = [console]
    file_handler = None
    if logfile is not None:
        file_handler = logging.FileHandler(logfile, mode="w", encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)
    package = logging.getLogger("j1x0_cardprinter")
    package.handlers[:] = handlers
    package.setLevel(logging.DEBUG if logfile is not None else level)
    package.propagate = False
    return file_handler


def _progress():
    """A progress callback that prints a line per 10% (used when not logging the protocol)."""
    last = [-1]

    def show(sent: int, total: int) -> None:
        percent = sent * 10 // total * 10
        if percent != last[0]:
            last[0] = percent
            print(f"  {percent:3d}%  ({sent // 1024} of {total // 1024} KB)", flush=True)

    return show


def _send_file(path: Path | None, args: argparse.Namespace) -> int:
    """Open the link, check it, and (if there is a job and the printer is ready) send the job.

    Exit status: 0 on success or a ready printer (link check only), 1 otherwise.
    """
    data = b""
    if path is not None:
        data = path.read_bytes()
        if not data:
            raise SystemExit(f"{path} is empty")
        if not data.startswith(JOB_PREFIX):
            print(f"warning: {path} does not start with ESC MASTER; sending anyway", file=sys.stderr)
    report = None
    watch = False
    if args.verbose or args.log:                          # see where a hang is, if there is one
        stream = args._log_handler.stream if args._log_handler else sys.stderr
        try:
            faulthandler.dump_traceback_later(HANG_DUMP_SECONDS, repeat=True, file=stream)
            watch = True
        except (AttributeError, ValueError, OSError):     # a stream with no file descriptor
            log.debug("no hang dump: the log stream has no file descriptor")
    try:
        with J1x0USB.discover(args.vid, args.pid, soft_reset=args.soft_reset) as link:
            if not args.no_check:
                report = check_link(link)
                print("\n".join(report.lines()), flush=True)
                for line in report.lines():
                    log.info("check  %s", line)
                if not report.ready and not (args.force and path is not None):
                    if path is not None:
                        print("not sending; fix the problem above or use --force", file=sys.stderr)
                    return 1
            if path is None:
                return 0
            if args.raw:
                sent = link.write(data)
                summary = f"sent {sent} bytes from {path} (raw: no pacing, no end-of-card commands)"
            else:
                result = send_job(link, data, end=not args.no_end, chunk=args.chunk,
                                  progress=None if args.verbose else _progress())
                summary = (f"sent {result.bytes_sent} bytes from {path} in {result.chunks} chunks "
                           f"({result.seconds:.1f} s)")
                if not args.no_end:
                    _watch_printer(link)
    except (RuntimeError, usb.core.USBError) as exc:              # includes PrinterError
        print(f"error: {exc}", file=sys.stderr, flush=True)
        log.error("error: %s", exc, extra={"file_only": True})      # the console already has it
        return 1
    except KeyboardInterrupt:
        print("interrupted: the printer may be left part-way through a job and need a power cycle",
              file=sys.stderr)
        return 130
    finally:
        if watch:
            faulthandler.cancel_dump_traceback_later()
    print(summary)
    return 0


AFTERMATH_POLLS = 8
AFTERMATH_INTERVAL = 2.0


def _watch_printer(link) -> None:
    """After the job, ask the printer how it is for a few seconds and log what it says.

    `ESC !NL` is the panels-printed counter: if it goes up from the pre-flight value the printer
    treated the data as a complete card. Silence means it is still busy, or wedged.
    """
    for attempt in range(1, AFTERMATH_POLLS + 1):
        try:
            reply = link.query(cmd("!NL"), 1000)
        except (RuntimeError, usb.core.USBError) as exc:
            log.info("after the job: %s", exc)
            return
        text = show_bytes(reply) if reply else "no answer"
        log.info("after the job (%d/%d): ESC !NL -> %s", attempt, AFTERMATH_POLLS, text)
        print(f"printer after the job: {text}", flush=True)
        time.sleep(AFTERMATH_INTERVAL)


def _run_ui(args: argparse.Namespace) -> int:
    from .webui import CardApp, serve                  # imported late: the CLI needs no web code

    if args.renderer.startswith("inkscape") and shutil.which("inkscape") is None:
        print("warning: Inkscape was not found on the PATH, so cards cannot be drawn.\n"
              "         Install it from https://inkscape.org, or use --renderer cairosvg.",
              file=sys.stderr)
    designs = Path(args.templates).expanduser() if args.templates else default_dir()
    app = CardApp(designs, rasteriser=make_rasteriser(args.renderer),
                  link_factory=lambda: J1x0USB.discover(args.vid, args.pid))
    serve(app, host=args.host, port=args.port, open_browser=not args.no_browser)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        argv = ["ui"]                                  # `j1x0` on its own opens the web UI
    parser = argparse.ArgumentParser(prog="j1x0", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="log the protocol (-vv: every USB transfer and acknowledgement)")
    common = argparse.ArgumentParser(add_help=False)              # the same -v after the sub-command
    common.add_argument("-v", "--verbose", action="count", default=argparse.SUPPRESS,
                        help="log the protocol (-vv: every USB transfer and acknowledgement)")
    sub = parser.add_subparsers(dest="command", required=True)

    def card_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("template", type=Path)
        p.add_argument("-s", "--set", action="append", default=[], metavar="NAME=VALUE")
        p.add_argument("--renderer", choices=("inkscape", "cairosvg"), default="inkscape",
                       help="SVG renderer (default: inkscape, which is the faithful one)")

    def render_args(p: argparse.ArgumentParser) -> None:
        card_args(p)
        p.add_argument("--seed", type=int, default=None,
                       help="seed the dither so repeated renders are byte-identical")
        p.add_argument("--sharpening", type=int, default=0, choices=range(0, 6), metavar="0-5",
                       help="edge sharpening of the colour planes: 0 off (default), 1 what the Windows driver does, "
                            "2 to 5 stronger. It puts a thin halo at hard edges, e.g. between gradient steps")

    def usb_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--vid", type=lambda s: int(s, 0), default=None,
                       help="USB vendor id (default: find the printer by its IEEE-1284 id)")
        p.add_argument("--pid", type=lambda s: int(s, 0), default=None)
        p.add_argument("--no-check", action="store_true",
                       help="skip the status and identity checks before sending")
        p.add_argument("--force", action="store_true",
                       help="send even if the check says the printer is not ready")
        p.add_argument("--soft-reset", action="store_true",
                       help="send a USB printer-class SOFT_RESET after opening (may abort a job)")
        p.add_argument("--raw", action="store_true",
                       help="write the file as it is: no pacing, no acknowledgements, no end-of-card commands")
        p.add_argument("--no-end", action="store_true",
                       help="do not add the end-of-card commands (for data that already has them)")
        p.add_argument("--chunk", type=int, default=None, metavar="BYTES",
                       help="bytes per chunk (default 4096, what the Windows monitor uses)")
        p.add_argument("--log", type=Path, default=None, metavar="FILE",
                       help="also write a full-detail log to FILE (flushed line by line; includes a stack "
                            "dump if a USB call hangs)")

    p = sub.add_parser("ui", parents=[common], help="open the web UI (this is what plain `j1x0` does)")
    usb_args_ui = p.add_argument_group("printer")
    usb_args_ui.add_argument("--vid", type=lambda s: int(s, 0), default=None)
    usb_args_ui.add_argument("--pid", type=lambda s: int(s, 0), default=None)
    p.add_argument("--templates", metavar="DIR", default=None,
                   help="folder of .svg card designs (default: $J1X0_TEMPLATES or ~/.j1x0)")
    p.add_argument("--renderer", choices=RENDERERS, default="inkscape-shell",
                   help="SVG renderer (default: inkscape-shell, a persistent Inkscape)")
    p.add_argument("--host", default="127.0.0.1",
                   help="address to listen on (default: this computer only)")
    p.add_argument("--port", type=int, default=8730)
    p.add_argument("--no-browser", action="store_true", help="do not open a browser window")

    p = sub.add_parser("fields", parents=[common], help="list $token$ fields and layers in a template")
    p.add_argument("template", type=Path)

    p = sub.add_parser("preview", parents=[common],
                       help="write the colour and K renders, and the two together, as PNGs")
    card_args(p)
    p.add_argument("-o", "--out", type=Path, default=Path("."))
    p.add_argument("--composite", action=argparse.BooleanOptionalAction, default=True,
                   help="also write composite.png, the colour render with the resin layer over it, "
                        "which is the preview the web UI shows (default; --no-composite to skip)")

    p = sub.add_parser("render", parents=[common], aliases=["build"], help="write the printer job to a file (no printer needed)")
    render_args(p)
    p.add_argument("-o", "--out", type=Path, required=True)

    p = sub.add_parser("send", parents=[common], help="check the printer, then send a job file (no Inkscape needed)")
    p.add_argument("job", type=Path, nargs="?", default=None,
                   help="job file from `render`; omit to only check the link and exit")
    usb_args(p)

    p = sub.add_parser("print", parents=[common], help="render a card and send it (render + send)")
    render_args(p)
    usb_args(p)
    p.add_argument("-o", "--save", type=Path, default=None,
                   help="keep the rendered job at this path (default: a temporary file)")

    args = parser.parse_args(argv)
    if args.command == "build":
        args.command = "render"
    args._log_handler = _setup_logging(args.verbose, getattr(args, "log", None))

    if args.command == "ui":
        return _run_ui(args)

    if args.command == "send":
        return _send_file(args.job, args)

    if args.command == "fields":
        template = Template.from_file(args.template)
        print("fields:", ", ".join(template.fields()) or "(none)")
        print("layers:", ", ".join(template.layer_names()) or "(none)")
        return 0

    if args.command == "preview":
        template, values = _load_card(args)
        card = render_card(template, values, _rasteriser(args, template))
        args.out.mkdir(parents=True, exist_ok=True)
        card.colour.save(args.out / "colour.png")
        if card.k_mask is not None:
            ImageOps.invert(card.k_mask).save(args.out / "k.png")           # black dots on white
        if args.composite:
            composite(card).save(args.out / "composite.png")
        return 0

    job = _render_job(args)
    if args.command == "render":
        args.out.write_bytes(job)
        print(f"wrote {args.out} ({len(job)} bytes)")
        return 0

    # print: render to a job file, then send that file
    if args.save is not None:
        args.save.write_bytes(job)
        return _send_file(args.save, args)
    with tempfile.TemporaryDirectory(prefix="j1x0-") as tmp:
        path = Path(tmp) / "job.bin"
        path.write_bytes(job)
        return _send_file(path, args)


if __name__ == "__main__":
    raise SystemExit(main())
