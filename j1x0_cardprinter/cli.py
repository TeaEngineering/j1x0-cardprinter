"""Command line: list fields, preview, build a job file, or print.

    j1x0 fields card.svg
    j1x0 preview card.svg -s firstName=Ada -s lastName=Lovelace -o out/
    j1x0 build   card.svg -s firstName=Ada -s lastName=Lovelace -o job.bin
    j1x0 print   card.svg -s firstName=Ada --vid 0x1234 --pid 0x5678
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import numpy as np
from PIL import Image

from .encode import Orientation
from .printer import Printer
from .raster import InkscapeRasteriser, render_card
from .template import MissingField, Template
from .usb import J1x0USB


def _parse_sets(pairs: list[str]) -> dict[str, str]:
    values = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if not sep:
            raise SystemExit(f"--set expects name=value, got {pair!r}")
        values[name] = value
    return values


def _orientation(args: argparse.Namespace) -> Orientation:
    return Orientation(flip_lines=args.flip_lines, flip_dots=args.flip_dots)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="j1x0", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    def card_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("template", type=Path)
        p.add_argument("-s", "--set", action="append", default=[], metavar="NAME=VALUE")

    def orient_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--flip-lines", action="store_true")
        p.add_argument("--flip-dots", action="store_true")

    p = sub.add_parser("fields", help="list $token$ fields and layers in a template")
    p.add_argument("template", type=Path)

    p = sub.add_parser("preview", help="write the colour and K renders as PNGs")
    card_args(p)
    p.add_argument("-o", "--out", type=Path, default=Path("."))

    p = sub.add_parser("build", help="write the printer job to a file (no hardware)")
    card_args(p)
    orient_args(p)
    p.add_argument("-o", "--out", type=Path, required=True)

    p = sub.add_parser("print", help="print a card")
    card_args(p)
    orient_args(p)
    p.add_argument("--vid", type=lambda s: int(s, 0), required=True)
    p.add_argument("--pid", type=lambda s: int(s, 0), required=True)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")

    template = Template.from_file(args.template)
    if args.command == "fields":
        print("fields:", ", ".join(template.fields()) or "(none)")
        print("layers:", ", ".join(template.layer_names()) or "(none)")
        return 0

    values = _parse_sets(args.set)
    missing = [f for f in template.fields() if f not in values]
    if missing:
        print(f"missing values for: {', '.join(missing)}", file=sys.stderr)
        return 2

    try:
        if args.command == "preview":
            card = render_card(template, values, InkscapeRasteriser())
            args.out.mkdir(parents=True, exist_ok=True)
            card.colour.save(args.out / "colour.png")
            if card.k_mask is not None:
                Image.fromarray(np.where(card.k_mask, 0, 255).astype(np.uint8)).save(args.out / "k.png")
            return 0

        printer = Printer(orientation=_orientation(args))
        if args.command == "build":
            args.out.write_bytes(printer.build(template, values))
            print(f"wrote {args.out} ({args.out.stat().st_size} bytes)")
            return 0

        with J1x0USB.discover(args.vid, args.pid) as link:
            printer.link = link
            sent = printer.print_card(template, values)
        print(f"sent {sent} bytes")
        return 0
    except MissingField as exc:
        print(f"missing value for field {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
