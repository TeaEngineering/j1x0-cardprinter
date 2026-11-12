# Choosing the SVG renderer

**Decision: Inkscape is the renderer, cairosvg stays optional.** cairosvg is about 20 times faster
as a one-shot call, but it silently ignores or mangles features that Inkscape designs use, so it
cannot be the default. Inkscape's speed problem is solved by keeping one `inkscape --shell`
process running, which renders in about 35 ms with pixel-identical output.

Measured with `bench/render_bench.py` (Inkscape 1.4.2, cairosvg 2.9.1 on cairo 1.17.6, macOS).
Every case is rendered at the printer grid, 1024 x 640, and compared with Inkscape as the
reference, because Inkscape is where the designs are made and so defines what "right" looks like.

## Speed

| | per render |
|---|---|
| Inkscape, new process each time (`InkscapeRasteriser`) | 0.27 s (median) |
| cairosvg, in process (`CairoRasteriser`) | 0.014 s |
| Inkscape, persistent `--shell` (`InkscapeShellRasteriser`) | 0.025 to 0.045 s |

A card is two renders (colour, and the K resin layer): 0.56 s sequentially with one-shot Inkscape,
0.31 s if the two run in parallel, about 0.07 s with the persistent shell. The shell output was
identical to the one-shot output (maximum pixel difference 0). The web UI uses the shell, which
makes a debounced live preview comfortable. The CLI keeps the simple one-shot renderer because it
renders once and exits.

## Quality

Pixel comparison, 5 runs per case. "Pixels off" is the share of pixels differing by more than 32 in
some channel; "planes off" is the share of printer samples (5-bit density) differing by more than
one level of 32.

| Case | Inkscape | cairosvg | Mean abs diff | Pixels off | Planes off |
|---|---|---|---|---|---|
| shapes, gradients, dashes | 0.263 s | 0.015 s | 2.4 | 1.4% | 1.4% |
| text: basic fonts | 0.286 s | 0.014 s | 14.8 | 7.5% | 7.9% |
| text: spacing, rotation, path | 0.271 s | 0.014 s | 15.0 | 7.3% | 7.7% |
| text in a shape (`shape-inside`) | 0.266 s | 0.011 s | 6.5 | 3.3% | 3.5% |
| flowed text (`flowRoot`) | 0.262 s | 0.010 s | 60.9 | 24.4% | 24.4% |
| photo with clip path | 0.393 s | 0.106 s | 3.6 | 1.8% | 3.9% |
| filters (shadow, blur) | 0.291 s | 0.012 s | 9.1 | 9.9% | 10.8% |
| mask and opacity | 0.284 s | 0.012 s | 31.3 | 33.9% | 34.5% |
| pattern, marker, blend mode | 0.260 s | 0.028 s | 12.1 | 17.0% | 14.9% |
| `examples/test-card.svg` | 0.273 s | 0.023 s | 12.8 | 8.0% | 7.9% |
| `examples/card.svg` | 0.267 s | 0.012 s | 4.2 | 2.9% | 3.5% |

The numbers alone hide what matters, so the images were inspected too (the script writes contact
sheets). The differences fall into two kinds.

**Harmless: sub-pixel placement.** Plain text lands one to three pixels away from where Inkscape
puts it (different hinting and metrics). That is most of the "mean abs diff" for the example cards
and is invisible on a card. The 1-bit resin mask has about the same number of dots from either
renderer, but they are not in the same places, so a card is not byte-comparable across renderers.

**Wrong: features cairosvg ignores or breaks.**

| Feature in the design | What cairosvg does |
|---|---|
| Filters: drop shadows, blur | ignored; the shadow or blur is simply missing |
| Masks, including gradient masks | ignored; the object is drawn unmasked |
| `mix-blend-mode` (multiply and others) | ignored |
| Pattern fills | drawn, but blurred |
| Wrapped text boxes (`shape-inside`, what Inkscape 1.x makes when you drag out a text box) | **the text is not drawn at all** |
| Old flowed text (`flowRoot`) | **a solid black rectangle, and no text** |
| Italic with a generic family | upright |
| `text-decoration`, `word-spacing`, `textLength`, `font-variant` | ignored |

The silent text loss is the dangerous case: an unskilled user would print a card with a missing
name. Both renderers agree on shapes, strokes, dashes, linear and radial gradients, embedded
photos with clip paths, opacity, bold, serif and monospace text, `text-anchor`, `tspan`s, rotated
text, `textPath` and `letter-spacing`.

## What the code does about it

- `j1x0` and the web UI use Inkscape (the UI through the persistent shell).
- `--renderer cairosvg` is available for `preview`, `render`, `print` and `ui`. It is an optional
  install (`pip install "j1x0-cardprinter[svg]"`), and it prints a warning that lists the unsupported
  features found in the design (`Template.cairosvg_problems()`). The web UI shows the same
  warning under the preview.
- Both `examples/` designs use only features both renderers handle (checked by a test).

## Layers, and a trap in the benchmark's own test design

The resin (`K`) layer is rendered on its own and thresholded. The renderer hides every other layer
and also any loose object outside all layers, since those belong to the colour artwork. Before
that fix, an image placed directly in the SVG root leaked into the resin mask and printed as black
speckle; `tests/test_template_layers.py` covers it.

## Reproducing

    pip install cairosvg
    python bench/render_bench.py OUTDIR --runs 5

It prints the tables above and writes `OUTDIR/sheet*.png` (Inkscape, cairosvg and the amplified
difference side by side).
