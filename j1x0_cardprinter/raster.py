"""SVG -> pixels.

The design is rendered to the printer's native 1024 x 640 grid (CR80 is 85.6 x 54 mm, an
aspect of 1.585 against 1.6, so the image is stretched by under 1%, as the Windows driver does).

Two renders are made per card:
  * colour: everything except the layer named `K`  (becomes the Y/M/C planes)
  * K mask: only the layer named `K`, thresholded  (becomes the black resin bitmap)
If the template has no `K` layer there is no resin bitmap and black is printed from Y/M/C.

Rasterisers (docs/rendering.md has the benchmark behind this choice):
  * `InkscapeRasteriser`       one Inkscape process per render (~0.27 s). Simple and robust.
  * `InkscapeShellRasteriser`  one long-lived `inkscape --shell` (~0.03 s per render), pixel-for-pixel
                               identical to the above. Used by the web UI.
  * `CairoRasteriser`          cairosvg (~0.015 s) but it silently ignores filters, masks, blend
                               modes, wrapped text boxes and more. Optional, with a warning.

`make_rasteriser("auto")` picks Inkscape when it is installed (`find_inkscape`) and cairosvg otherwise.
"""

from __future__ import annotations

import atexit
import io
import os
import queue
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol

from PIL import Image

from .template import Template

CARD_W = 1024
CARD_H = 640


class Rasteriser(Protocol):
    def render(self, svg: bytes, width: int, height: int) -> Image.Image: ...


# Where Inkscape lives when it is not on the PATH: the download from inkscape.org, and Homebrew. An app
# started from the Finder has no useful PATH, so these matter there.
INKSCAPE_LOCATIONS = (
    "/Applications/Inkscape.app/Contents/MacOS/inkscape",
    "~/Applications/Inkscape.app/Contents/MacOS/inkscape",
    "/opt/homebrew/bin/inkscape",
    "/usr/local/bin/inkscape",
)


def find_inkscape() -> str | None:
    """The Inkscape command: on the PATH, or in one of `INKSCAPE_LOCATIONS`; None if it is not installed."""
    found = shutil.which("inkscape")
    if found:
        return found
    for location in INKSCAPE_LOCATIONS:
        path = Path(location).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


class InkscapeRasteriser:
    """Rasterise with the Inkscape command line. Renders on an opaque white background."""

    def __init__(self, binary: str | None = None) -> None:
        self.binary = binary or find_inkscape() or "inkscape"

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


class _ShellDied(Exception):
    pass


class InkscapeShellRasteriser:
    """Rasterise through one persistent `inkscape --shell` process.

    Starting Inkscape dominates the cost of a render, so keeping it running makes each render
    about 7x faster than `InkscapeRasteriser` and gives identical pixels. The process is started
    on first use, restarted once if it dies, and stopped at interpreter exit or `close()`.
    Thread-safe: renders are serialised.
    """

    TIMEOUT = 60.0

    def __init__(self, binary: str | None = None) -> None:
        self.binary = binary or find_inkscape() or "inkscape"
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._chunks: queue.Queue[bytes | None] = queue.Queue()
        self._tmp: Path | None = None
        self._registered = False

    # ── Public ───────────────────────────────────────────────────────────────

    def render(self, svg: bytes, width: int, height: int) -> Image.Image:
        with self._lock:
            for attempt in (1, 2):
                try:
                    return self._render(svg, width, height)
                except _ShellDied:
                    self._stop()
                    if attempt == 2:
                        raise RuntimeError("Inkscape keeps exiting while rendering") from None
        raise AssertionError("unreachable")

    def close(self) -> None:
        with self._lock:
            self._stop()

    # ── Internals ────────────────────────────────────────────────────────────

    def _render(self, svg: bytes, width: int, height: int) -> Image.Image:
        self._ensure_running()
        src, dst = self._tmp / "card.svg", self._tmp / "card.png"
        dst.unlink(missing_ok=True)
        src.write_bytes(svg)
        command = (f"file-open:{src}; export-type:png; export-filename:{dst}; "
                   f"export-width:{width}; export-height:{height}; "
                   "export-background:white; export-background-opacity:1; export-do; file-close\n")
        try:
            self._proc.stdin.write(command.encode())
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError):
            raise _ShellDied from None
        output = self._read_until_prompt()
        if not dst.exists() or dst.stat().st_size == 0:
            tail = output.decode(errors="replace").strip().splitlines()[-3:]
            raise RuntimeError("Inkscape could not render the card: " + " | ".join(tail))
        image = Image.open(dst)
        image.load()
        return image.convert("RGB")

    def _ensure_running(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        self._stop()
        if shutil.which(self.binary) is None:
            raise RuntimeError(f"Inkscape not found ({self.binary!r} is not on the PATH)")
        self._tmp = Path(tempfile.mkdtemp(prefix="j1x0-shell-"))
        chunks: queue.Queue[bytes | None] = queue.Queue()
        self._chunks = chunks
        proc = subprocess.Popen([self.binary, "--shell"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
        self._proc = proc

        def pump() -> None:
            while True:
                data = proc.stdout.read(4096)
                chunks.put(data or None)
                if not data:
                    return

        threading.Thread(target=pump, daemon=True).start()
        self._read_until_prompt()                   # the banner ends with the first prompt
        if not self._registered:
            atexit.register(self.close)
            self._registered = True

    def _read_until_prompt(self) -> bytes:
        """Read shell output up to the `> ` prompt that follows every command."""
        buf = b""
        while True:
            try:
                chunk = self._chunks.get(timeout=self.TIMEOUT)
            except queue.Empty:
                self._stop()
                raise RuntimeError(f"Inkscape did not respond within {self.TIMEOUT:.0f} s") from None
            if chunk is None:
                raise _ShellDied
            buf += chunk
            if buf == b"> " or buf.endswith(b"\n> "):
                return buf

    def _stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            try:
                proc.stdin.write(b"quit\n")
                proc.stdin.flush()
                proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                proc.kill()
        if self._tmp is not None:
            shutil.rmtree(self._tmp, ignore_errors=True)
            self._tmp = None


class CairoRasteriser:
    """Rasterise with cairosvg (`pip install j1x0-cardprinter[svg]`).

    Fast, but it ignores or mangles SVG features that Inkscape designs use; see
    `Template.cairosvg_problems()` and docs/rendering.md.
    """

    def render(self, svg: bytes, width: int, height: int) -> Image.Image:
        import cairosvg  # optional dependency

        png = cairosvg.svg2png(
            bytestring=svg, output_width=width, output_height=height, background_color="white"
        )
        return Image.open(io.BytesIO(png)).convert("RGB")


RENDERERS = ("inkscape", "inkscape-shell", "cairosvg", "auto")


def _have_cairosvg() -> bool:
    """True if cairosvg and the cairo library behind it can be loaded."""
    try:
        import cairosvg  # noqa: F401
    except (ImportError, OSError):
        return False
    return True


def make_rasteriser(name: str = "inkscape") -> Rasteriser:
    if name == "auto":                      # Inkscape if it is installed, else cairosvg (with its limits)
        if find_inkscape():
            return InkscapeShellRasteriser()
        if _have_cairosvg():
            return CairoRasteriser()
        raise RuntimeError("no SVG renderer is available: install Inkscape (https://inkscape.org), "
                           "or `pip install j1x0-cardprinter[svg]`")
    if name == "inkscape":
        return InkscapeRasteriser()
    if name == "inkscape-shell":
        return InkscapeShellRasteriser()
    if name in ("cairosvg", "cairo"):
        return CairoRasteriser()
    raise ValueError(f"unknown renderer {name!r}; choose from {', '.join(RENDERERS)}")


@dataclass
class CardRaster:
    colour: Image.Image                 # RGB, CARD_W x CARD_H
    k_mask: Image.Image | None          # mode "L", CARD_W x CARD_H; 255 = resin dot, 0 = none


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
        mask = grey.convert("L").point([255 if v < threshold else 0 for v in range(256)])
        k_mask = mask if mask.getbbox() else None            # no dots at all: no resin panel
    return CardRaster(colour=colour, k_mask=k_mask)


def composite(card: CardRaster) -> Image.Image:
    """The colour render with the resin layer overprinted in black: what the card should look like."""
    image = card.colour.convert("RGB").copy()
    if card.k_mask is not None:
        image.paste((0, 0, 0), mask=card.k_mask)
    return image
