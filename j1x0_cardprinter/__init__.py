"""Print SVG card designs on a J1x0-series (J100i/J110i/J120i) card printer.

Pipeline (see docs/protocol.md for the wire format):

    Template (SVG + $tokens$)  ->  raster (Y/M/C colour + K mask)
        ->  encode (packed 5-bit planes, 1-bit K bitmap)
        ->  protocol (ESC command stream)  ->  usb (libusb bulk write)
"""

from .printer import Printer
from .protocol import JobOptions, Overlay, Tuning
from .template import MissingField, Template
from .usb import J1x0MockUSB, J1x0USB

__all__ = [
    "J1x0MockUSB",
    "J1x0USB",
    "JobOptions",
    "MissingField",
    "Overlay",
    "Printer",
    "Template",
    "Tuning",
]
