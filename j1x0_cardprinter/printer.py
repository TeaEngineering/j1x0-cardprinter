"""High-level entry point: template + values -> job bytes -> printer."""

from __future__ import annotations

from typing import Mapping

import numpy as np

from .encode import colour_planes, encode_plane, pack_mono, prepare_colour
from .protocol import JobOptions, build_job
from .raster import CardRaster, InkscapeRasteriser, Rasteriser, render_card
from .sender import send_job
from .template import Template
from .usb import J1x0Link


class Printer:
    def __init__(
        self,
        link: J1x0Link | None = None,
        rasteriser: Rasteriser | None = None,
        seed: int | None = None,
        sharpening: int = 0,
    ) -> None:
        self.link = link
        self.rasteriser = rasteriser or InkscapeRasteriser()
        self.sharpening = sharpening
        self._rng = np.random.default_rng(seed)

    def encode(self, card: CardRaster, options: JobOptions | None = None) -> bytes:
        """Turn an already-rendered card into the complete job byte stream."""
        prepared = prepare_colour(np.asarray(card.colour), sharpening=self.sharpening, rng=self._rng)
        planes = [encode_plane(p) for p in colour_planes(prepared)]
        k_payload = pack_mono(card.k_mask) if card.k_mask is not None else None
        return build_job(planes, k_payload, options)

    def build(
        self, template: Template, values: Mapping[str, object], options: JobOptions | None = None
    ) -> bytes:
        """Render the card and return the complete job byte stream (no hardware needed)."""
        return self.encode(render_card(template, values, self.rasteriser), options)

    def print_card(
        self, template: Template, values: Mapping[str, object], options: JobOptions | None = None
    ) -> int:
        if self.link is None:
            raise RuntimeError("Printer has no link; use build() for a dry run")
        return send_job(self.link, self.build(template, values, options)).bytes_sent
