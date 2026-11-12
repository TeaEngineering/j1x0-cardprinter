"""SVG -> pixels.

The design is rendered to the printer's native 1024 x 640 grid (CR80 is 85.6 x 54 mm, an
aspect of 1.585 against 1.6, so the image is stretched by under 1%, as the Windows driver does).

Two renders are made per card:
  * colour: everything except the layer named `K`  (becomes the Y/M/C planes)
  * K mask: only the layer named `K`, thresholded  (becomes the black resin bitmap)
If the template has no `K` layer there is no resin bitmap and black is printed from Y/M/C.
"""

from __future__ import annotations

import io
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol

import numpy as np
from PIL import Image

from .template import Template

CARD_W = 1024
CARD_H = 640


class Rasteriser(Protocol):
    def render(self, svg: bytes, width: int, height: int) -> Image.Image: ...


class InkscapeRasteriser:
    """Rasterise with the Inkscape command line. Renders on an opaque white background."""

    def __init__(self, binary: str = "inkscape") -> None:
        self.binary = binary

    def render(self, svg: bytes, width: int, height: int) -> Image.Image:
        with tempfile.TemporaryDirectory(prefix="j1x0-") as tmp:
            src, dst = Path(tmp, "card.svg"), Path(tmp, "card.png")
            src.write_bytes(svg)
            subprocess.run(
                [
                    self.binary, str(src),
                    "--export-type=png", f"--export-filename={dst}",
                    f"--export-width={width}", f"--export-height={height}",
                    "--export-background=white", "--export-background-opacity=1.0",
                ],
                check=True, capture_output=True,
            )
            image = Image.open(dst)
            image.load()
        return image.convert("RGB")


class CairoRasteriser:
    """Rasterise with cairosvg (`pip install j1x0-cardprinter[svg]`)."""

    def render(self, svg: bytes, width: int, height: int) -> Image.Image:
        import cairosvg  # optional dependency

        png = cairosvg.svg2png(
            bytestring=svg, output_width=width, output_height=height, background_color="white"
        )
        return Image.open(io.BytesIO(png)).convert("RGB")


@dataclass
class CardRaster:
    colour: Image.Image                 # RGB, CARD_W x CARD_H
    k_mask: np.ndarray | None           # bool, shape (CARD_H, CARD_W); True = resin dot


def render_card(
    template: Template,
    values: Mapping[str, object],
    rasteriser: Rasteriser,
    *,
    k_layer: str = "K",
    threshold: int = 128,
) -> CardRaster:
    has_k = k_layer in template.layer_names()
    colour = rasteriser.render(
        template.render(values, hide_layers=[k_layer] if has_k else []), CARD_W, CARD_H
    )
    k_mask = None
    if has_k:
        grey = rasteriser.render(template.render(values, only_layers=[k_layer]), CARD_W, CARD_H)
        mask = np.asarray(grey.convert("L")) < threshold
        k_mask = mask if mask.any() else None
    return CardRaster(colour=colour, k_mask=k_mask)
