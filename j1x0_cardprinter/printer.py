"""High-level entry point: template + values -> job bytes -> printer."""

from __future__ import annotations

from typing import Mapping

import numpy as np

from .encode import Orientation, colour_planes, encode_plane, pack_mono, prepare_colour
from .protocol import JobOptions, build_job
from .raster import InkscapeRasteriser, Rasteriser, render_card
from .template import Template
from .usb import J1x0Link


class Printer:
    def __init__(
        self,
        link: J1x0Link | None = None,
        rasteriser: Rasteriser | None = None,
        orientation: Orientation = Orientation(),
        seed: int | None = None,
    ) -> None:
        self.link = link
        self.rasteriser = rasteriser or InkscapeRasteriser()
        self.orientation = orientation
        self._rng = np.random.default_rng(seed)

    def build(
        self, template: Template, values: Mapping[str, object], options: JobOptions | None = None
    ) -> bytes:
        """Render the card and return the complete job byte stream (no hardware needed)."""
        card = render_card(template, values, self.rasteriser)
        prepared = prepare_colour(np.asarray(card.colour), rng=self._rng)
        planes = [encode_plane(p, self.orientation) for p in colour_planes(prepared)]
        k_payload = pack_mono(card.k_mask, self.orientation) if card.k_mask is not None else None
        return build_job(planes, k_payload, options)

    def print_card(
        self, template: Template, values: Mapping[str, object], options: JobOptions | None = None
    ) -> int:
        if self.link is None:
            raise RuntimeError("Printer has no link; use build() for a dry run")
        return self.link.write(self.build(template, values, options))
