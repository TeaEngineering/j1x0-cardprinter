"""Benchmark cairosvg against Inkscape as card rasterisers.

    python scripts/render_bench.py OUTDIR [--runs N]

For each test SVG it renders at the printer grid (1024 x 640) with both renderers, times them,
compares pixels (Inkscape is the reference) and writes a contact sheet to OUTDIR. It also
compares the result after conversion to the printer's 5-bit planes, which is what matters in
the end: a difference of one level in 32 is invisible on a dye-sub card.

Needs numpy (`pip install numpy`), which the package itself no longer depends on.
"""

from __future__ import annotations

import argparse
import base64
import io
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

W, H = 1024, 640
HERE = Path(__file__).parent
EXAMPLES = HERE.parent / "examples"

HEAD = ('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
        'xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd" '
        'width="85.6mm" height="54mm" viewBox="0 0 85.6 54">')


def portrait_png() -> str:
    """A synthetic 'photo': smooth gradients, a face-like blob and noise."""
    rng = np.random.default_rng(1)
    y, x = np.mgrid[0:240, 0:200]
    img = np.zeros((240, 200, 3), float)
    img[..., 0] = 90 + 100 * x / 200
    img[..., 1] = 120 + 80 * y / 240
    img[..., 2] = 200 - 90 * x / 200
    face = ((x - 100) ** 2 / 55 ** 2 + (y - 110) ** 2 / 75 ** 2) < 1
    img[face] = (224, 172, 140)
    img += rng.normal(0, 6, img.shape)
    buf = io.BytesIO()
    Image.fromarray(np.clip(img, 0, 255).astype(np.uint8)).save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


CASES: dict[str, str] = {
    "shapes_gradients": HEAD + """
      <defs>
        <linearGradient id="a" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#fff"/><stop offset="1" stop-color="#c00"/></linearGradient>
        <radialGradient id="b" cx="0.5" cy="0.5" r="0.5"><stop offset="0" stop-color="#ff0"/><stop offset="1" stop-color="#00f"/></radialGradient>
      </defs>
      <rect width="85.6" height="54" fill="#fff"/>
      <rect x="4" y="4" width="36" height="14" fill="url(#a)" stroke="#000" stroke-width="0.3"/>
      <circle cx="62" cy="14" r="10" fill="url(#b)"/>
      <path d="M4 30 C 20 20, 30 50, 50 36 S 80 30, 82 46" fill="none" stroke="#080" stroke-width="1.2" stroke-dasharray="3 1.5"/>
      <polygon points="8,50 20,38 32,50" fill="#f80" fill-opacity="0.6" stroke="#000" stroke-width="0.2"/>""" + "</svg>",

    "text_basic": HEAD + """
      <rect width="85.6" height="54" fill="#fff"/>
      <text x="4" y="9" font-family="sans-serif" font-size="5">Sans-serif 5mm</text>
      <text x="4" y="17" font-family="serif" font-size="5">Serif 5mm Agjy</text>
      <text x="4" y="24" font-family="monospace" font-size="4">Monospace 0123456789</text>
      <text x="4" y="31" font-family="sans-serif" font-size="5" font-weight="bold">Bold sans</text>
      <text x="45" y="31" font-family="sans-serif" font-size="5" font-style="italic">Italic sans</text>
      <text x="42.8" y="38" font-family="sans-serif" font-size="4" text-anchor="middle">Centred text</text>
      <text x="82" y="38" font-family="sans-serif" font-size="4" text-anchor="end">Right</text>
      <text x="4" y="46" font-family="Helvetica, Arial, sans-serif" font-size="3"><tspan>Tiny </tspan><tspan font-weight="bold">mixed</tspan><tspan dy="-0.8"> raised</tspan></text>
      <text x="4" y="52" font-family="sans-serif" font-size="2">Two millimetre text, accents: Zoë Café Ångström</text>""" + "</svg>",

    "text_fancy": HEAD + """
      <defs><path id="p" d="M4 40 Q 42 22, 82 40"/></defs>
      <rect width="85.6" height="54" fill="#fff"/>
      <text x="4" y="8" font-family="sans-serif" font-size="4" letter-spacing="0.6">Letter spacing</text>
      <text x="4" y="15" font-family="sans-serif" font-size="4" word-spacing="3">Word spacing here</text>
      <text x="4" y="22" font-family="sans-serif" font-size="4" textLength="60" lengthAdjust="spacingAndGlyphs">textLength stretched</text>
      <text x="4" y="29" font-family="sans-serif" font-size="4" text-decoration="underline">Underlined</text>
      <text x="55" y="29" font-family="sans-serif" font-size="4" transform="rotate(-8 55 29)">Rotated</text>
      <text font-family="sans-serif" font-size="4"><textPath xlink:href="#p">Text on a curved path</textPath></text>
      <text x="4" y="50" font-family="sans-serif" font-size="4" font-variant="small-caps">Small Caps Variant</text>""" + "</svg>",

    "text_wrapped_svg2": HEAD + """
      <defs><rect id="box" x="4" y="4" width="40" height="30"/></defs>
      <rect width="85.6" height="54" fill="#fff"/>
      <text xml:space="preserve" style="font-size:3.5px;font-family:sans-serif;shape-inside:url(#box)">Text in a shape, wrapped by Inkscape 1.x using shape-inside, as when you drag out a text box.</text>
      <rect x="4" y="4" width="40" height="30" fill="none" stroke="#999" stroke-width="0.2"/>""" + "</svg>",

    "text_flowroot": HEAD + """
      <rect width="85.6" height="54" fill="#fff"/>
      <flowRoot xml:space="preserve" style="font-size:3.5px;font-family:sans-serif"><flowRegion><rect x="4" y="4" width="40" height="30"/></flowRegion><flowPara>Flowed text from Inkscape 0.92 (flowRoot), wrapped inside its region.</flowPara></flowRoot>""" + "</svg>",

    "photo_clip": HEAD + """
      <defs><clipPath id="c"><circle cx="22" cy="27" r="19"/></clipPath></defs>
      <rect width="85.6" height="54" fill="#eef"/>
      <image x="3" y="8" width="38" height="46" xlink:href="PHOTO" preserveAspectRatio="xMidYMid slice" clip-path="url(#c)"/>
      <circle cx="22" cy="27" r="19" fill="none" stroke="#000" stroke-width="0.4"/>
      <image x="48" y="8" width="34" height="40" xlink:href="PHOTO" preserveAspectRatio="xMidYMid meet" opacity="0.9"/>""" + "</svg>",

    "filter_shadow": HEAD + """
      <defs><filter id="s" x="-0.3" y="-0.3" width="1.6" height="1.6"><feGaussianBlur in="SourceAlpha" stdDeviation="0.8"/><feOffset dx="1" dy="1"/><feComponentTransfer><feFuncA type="linear" slope="0.5"/></feComponentTransfer><feMerge><feMergeNode/><feMergeNode in="SourceGraphic"/></feMerge></filter>
            <filter id="b"><feGaussianBlur stdDeviation="1.2"/></filter></defs>
      <rect width="85.6" height="54" fill="#fff"/>
      <rect x="6" y="6" width="34" height="18" fill="#2a6" filter="url(#s)"/>
      <text x="50" y="18" font-family="sans-serif" font-size="8" fill="#a22" filter="url(#s)">Shadow</text>
      <circle cx="22" cy="40" r="8" fill="#06c" filter="url(#b)"/>""" + "</svg>",

    "mask_opacity": HEAD + """
      <defs><linearGradient id="g" x1="0" x2="1"><stop offset="0" stop-color="#000"/><stop offset="1" stop-color="#fff"/></linearGradient>
            <mask id="m"><rect width="85.6" height="54" fill="url(#g)"/></mask></defs>
      <rect width="85.6" height="54" fill="#fff"/>
      <rect x="4" y="4" width="78" height="22" fill="#c30" mask="url(#m)"/>
      <g opacity="0.5"><circle cx="30" cy="40" r="10" fill="#00c"/><circle cx="40" cy="40" r="10" fill="#0a0"/></g>
      <rect x="55" y="30" width="26" height="18" fill="#000" fill-opacity="0.25"/>""" + "</svg>",

    "pattern_marker": HEAD + """
      <defs><pattern id="p" width="3" height="3" patternUnits="userSpaceOnUse"><rect width="1.5" height="1.5" fill="#06c"/></pattern>
            <marker id="m" markerWidth="4" markerHeight="4" refX="2" refY="2" orient="auto"><path d="M0 0 L4 2 L0 4 z" fill="#c00"/></marker></defs>
      <rect width="85.6" height="54" fill="#fff"/>
      <rect x="4" y="4" width="40" height="22" fill="url(#p)" stroke="#000" stroke-width="0.2"/>
      <line x1="50" y1="10" x2="80" y2="10" stroke="#000" stroke-width="0.4" marker-end="url(#m)"/>
      <rect x="4" y="32" width="78" height="16" fill="#fc0" style="mix-blend-mode:multiply"/>
      <circle cx="43" cy="40" r="9" fill="#0cf" style="mix-blend-mode:multiply"/>""" + "</svg>",
}


def load_cases() -> dict[str, bytes]:
    photo = portrait_png()
    cases = {name: svg.replace("PHOTO", photo).encode() for name, svg in CASES.items()}
    cases["example_test_card"] = (EXAMPLES / "test-card.svg").read_bytes()
    card = (EXAMPLES / "card.svg").read_text()
    cases["example_card"] = (card.replace("$firstName$", "Ada").replace("$lastName$", "Lovelace")
                             .replace("$cardNo$", "0042")).encode()
    return cases


def inkscape_render(svg: bytes, tmp: Path) -> Image.Image:
    src, dst = tmp / "in.svg", tmp / "out.png"
    src.write_bytes(svg)
    subprocess.run(["inkscape", str(src), "--export-type=png", f"--export-filename={dst}",
                    f"--export-width={W}", f"--export-height={H}",
                    "--export-background=white", "--export-background-opacity=1.0"],
                   check=True, capture_output=True)
    image = Image.open(dst)
    image.load()
    return image.convert("RGB")


def cairo_render(svg: bytes, tmp: Path) -> Image.Image:
    import cairosvg
    png = cairosvg.svg2png(bytestring=svg, output_width=W, output_height=H, background_color="white")
    return Image.open(io.BytesIO(png)).convert("RGB")


def planes(image: Image.Image) -> np.ndarray:
    """The printer's 5-bit densities, without dither: (255 - channel) >> 3 per channel."""
    return (255 - np.asarray(image).astype(np.int32)) >> 3


def timed(fn, svg, tmp, runs):
    fn(svg, tmp)                                    # warm-up (font caches, page cache)
    times = []
    for _ in range(runs):
        t0 = time.perf_counter()
        fn(svg, tmp)
        times.append(time.perf_counter() - t0)
    return times


def label(image: Image.Image, text: str) -> Image.Image:
    out = image.copy()
    ImageDraw.Draw(out).rectangle([0, 0, 8 + 7 * len(text), 16], fill="#000")
    ImageDraw.Draw(out).text((4, 2), text, fill="#fff")
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("outdir", type=Path)
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args()
    args.outdir.mkdir(parents=True, exist_ok=True)

    rows, sheet_rows = [], []
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        for name, svg in load_cases().items():
            ink_t = timed(inkscape_render, svg, tmp, args.runs)
            try:
                cai_t = timed(cairo_render, svg, tmp, args.runs)
                ink, cai = inkscape_render(svg, tmp), cairo_render(svg, tmp)
            except Exception as exc:                       # a renderer failing is a result
                rows.append((name, statistics.median(ink_t), None, f"cairosvg FAILED: {exc}", "", ""))
                continue
            a, b = np.asarray(ink).astype(int), np.asarray(cai).astype(int)
            diff = np.abs(a - b)
            mad = diff.mean()
            big = (diff.max(axis=2) > 32).mean() * 100
            pd = np.abs(planes(ink) - planes(cai))
            plane_big = (pd > 1).mean() * 100
            rows.append((name, statistics.median(ink_t), statistics.median(cai_t),
                         f"{mad:.2f}", f"{big:.2f}", f"{plane_big:.2f}"))
            amplified = Image.fromarray(np.clip(255 - diff * 4, 0, 255).astype(np.uint8))
            sheet_rows.append((name, ink, cai, amplified))
            ink.save(args.outdir / f"{name}.inkscape.png")
            cai.save(args.outdir / f"{name}.cairosvg.png")

    print(f"{'case':22s} {'inkscape s':>10s} {'cairosvg s':>10s} {'speedup':>8s} "
          f"{'mean|d|':>8s} {'%px>32':>7s} {'%plane>1':>9s}")
    for name, ti, tc, mad, big, pbig in rows:
        if tc is None:
            print(f"{name:22s} {ti:10.3f} {'-':>10s} {'-':>8s} {mad}")
        else:
            print(f"{name:22s} {ti:10.3f} {tc:10.3f} {ti / tc:7.0f}x {mad:>8s} {big:>7s} {pbig:>9s}")
    ok = [r for r in rows if r[2] is not None]
    print(f"\nmedian render time: inkscape {statistics.median(r[1] for r in ok):.3f}s, "
          f"cairosvg {statistics.median(r[2] for r in ok):.3f}s")

    # contact sheets: inkscape | cairosvg | diff x4 per case, 3 cases per sheet
    for n in range(0, len(sheet_rows), 3):
        chunk = sheet_rows[n:n + 3]
        sheet = Image.new("RGB", (3 * W // 2 + 20, len(chunk) * (H // 2 + 8)), "#777")
        for i, (name, ink, cai, dif) in enumerate(chunk):
            for j, (img, tag) in enumerate(((ink, "inkscape"), (cai, "cairosvg"), (dif, "diff x4"))):
                small = label(img, f"{name}: {tag}").resize((W // 2, H // 2), Image.LANCZOS)
                sheet.paste(small, (j * (W // 2 + 10), i * (H // 2 + 8)))
        sheet.save(args.outdir / f"sheet{n // 3 + 1}.png")


if __name__ == "__main__":
    sys.exit(main())
