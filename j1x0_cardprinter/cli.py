"""Command line: list fields, preview, render a job file, send it, or do both with `print`.

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
"""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
from pathlib import Path

import numpy as np
import usb.core
from PIL import Image

from .encode import Orientation
from .printer import Printer
from .probe import check_link
from .raster import InkscapeRasteriser, render_card
from .template import Template
from .usb import J1x0USB

JOB_PREFIX = b"\x1bMASTER\r"


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


def _render_job(args: argparse.Namespace) -> bytes:
    template, values = _load_card(args)
    printer = Printer(
        rasteriser=InkscapeRasteriser(),
        orientation=Orientation(flip_lines=args.flip_lines, flip_dots=args.flip_dots),
        seed=args.seed,
    )
    return printer.build(template, values)


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
    try:
        with J1x0USB.discover(args.vid, args.pid, soft_reset=args.soft_reset) as link:
            if not args.no_check:
                report = check_link(link)
                print("\n".join(report.lines()))
                if not report.ready and not (args.force and path is not None):
                    if path is not None:
                        print("not sending; fix the problem above or use --force", file=sys.stderr)
                    return 1
            if path is None:
                return 0
            sent = link.write(data)
    except (RuntimeError, usb.core.USBError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"sent {sent} bytes from {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="j1x0", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    def card_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("template", type=Path)
        p.add_argument("-s", "--set", action="append", default=[], metavar="NAME=VALUE")

    def render_args(p: argparse.ArgumentParser) -> None:
        card_args(p)
        p.add_argument("--flip-lines", action="store_true")
        p.add_argument("--flip-dots", action="store_true")
        p.add_argument("--seed", type=int, default=None,
                       help="seed the dither so repeated renders are byte-identical")

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

    p = sub.add_parser("fields", help="list $token$ fields and layers in a template")
    p.add_argument("template", type=Path)

    p = sub.add_parser("preview", help="write the colour and K renders as PNGs")
    card_args(p)
    p.add_argument("-o", "--out", type=Path, default=Path("."))

    p = sub.add_parser("render", aliases=["build"], help="write the printer job to a file (no printer needed)")
    render_args(p)
    p.add_argument("-o", "--out", type=Path, required=True)

    p = sub.add_parser("send", help="check the printer, then send a job file (no Inkscape needed)")
    p.add_argument("job", type=Path, nargs="?", default=None,
                   help="job file from `render`; omit to only check the link and exit")
    usb_args(p)

    p = sub.add_parser("print", help="render a card and send it (render + send)")
    render_args(p)
    usb_args(p)
    p.add_argument("-o", "--save", type=Path, default=None,
                   help="keep the rendered job at this path (default: a temporary file)")

    args = parser.parse_args(argv)
    if args.command == "build":
        args.command = "render"
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")

    if args.command == "send":
        return _send_file(args.job, args)

    if args.command == "fields":
        template = Template.from_file(args.template)
        print("fields:", ", ".join(template.fields()) or "(none)")
        print("layers:", ", ".join(template.layer_names()) or "(none)")
        return 0

    if args.command == "preview":
        template, values = _load_card(args)
        card = render_card(template, values, InkscapeRasteriser())
        args.out.mkdir(parents=True, exist_ok=True)
        card.colour.save(args.out / "colour.png")
        if card.k_mask is not None:
            Image.fromarray(np.where(card.k_mask, 0, 255).astype(np.uint8)).save(args.out / "k.png")
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
