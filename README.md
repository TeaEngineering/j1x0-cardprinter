j1x0-cardprinter
================

Design a card in Inkscape, put `$tokens$` in it, and print it on a J1x0-series
(J100i / J110i / J120i) card printer over raw USB. No Windows driver required.

![Python versions](https://img.shields.io/pypi/pyversions/j1x0-cardprinter.svg) ![GitHub Actions Workflow Status](https://img.shields.io/github/actions/workflow/status/TeaEngineering/j1x0-cardprinter/ci.yml) [![PyPI version](https://badge.fury.io/py/j1x0-cardprinter.svg)](https://badge.fury.io/py/j1x0-cardprinter)

> **Status: pre-alpha, not yet tested on a printer.** The print protocol was reverse
> engineered from the vendor's Windows driver by static analysis alone. The job builder
> follows it faithfully, but no card has been printed yet. Expect the first prints to need
> adjustment (see [Orientation](#orientation) and [docs/open-questions.md](docs/open-questions.md)).

How it works
------------

```
card.svg + values  ->  render (Inkscape)  ->  Y/M/C planes + K bitmap  ->  ESC commands  ->  USB
```

1. `$tokens$` in the SVG are replaced with your values.
2. The card is rendered at the printer's native 1024 x 640 grid, in two passes: colour artwork
   (printed with the yellow / magenta / cyan ribbon panels) and the layer named `K`
   (printed with the black resin panel).
3. The images are encoded and wrapped in the printer's command language, then written to the
   printer's USB bulk endpoint with libusb.

Install
-------

    $ pip install j1x0-cardprinter

You also need an SVG rasteriser. The default is the [Inkscape](https://inkscape.org) command
line (`inkscape` on your `PATH`). To use [CairoSVG](https://cairosvg.org) instead:

    $ pip install "j1x0-cardprinter[svg]"

On macOS and Linux `pyusb` needs the libusb library (`brew install libusb`, or your
distribution's `libusb-1.0` package).

Quick start
-----------

    $ j1x0 fields examples/card.svg
    fields: firstName, lastName, cardNo
    layers: Design, K

    $ j1x0 preview examples/card.svg -s firstName=Ada -s lastName=Lovelace -s cardNo=0042 -o out/
    $ j1x0 render  examples/card.svg -s firstName=Ada -s lastName=Lovelace -s cardNo=0042 -o job.bin
    $ j1x0 send                                          # check the printer, then stop
    $ j1x0 send    job.bin                               # check the printer, then send
    $ j1x0 print   examples/card.svg -s firstName=Ada -s lastName=Lovelace -s cardNo=0042

| Command | Needs | What it does |
|---|---|---|
| `fields` | | List the `$token$` fields and layer names in a template. |
| `preview` | Inkscape | Write `colour.png` and `k.png` so you can check the two renders. |
| `render` | Inkscape | Render a card to a printer job file. No printer needed. (`build` is an alias.) |
| `send` | printer | Check the printer. With a job file, then send it; without one, stop there. No Inkscape needed. |
| `print` | both | `render` then `send`, through a temporary job file. `-o job.bin` keeps the file. |

`render` and `send` can run on different machines: render where your design tools and fonts
are, send from the computer the printer is plugged into. Rendering is dithered randomly, so
add `--seed N` if you want repeated renders to be byte-identical.

Every field in the template needs a `-s NAME=VALUE`; the command stops and lists any that are
missing.

### Checking the printer

`send` always opens the USB link and checks it before it sends anything, using the same reads
the Windows driver makes when it starts a document:

    $ j1x0 send
    device:           bus 1 addr 7  04f9:0123  ...
    1284 id:          MFG:...;MDL:...;
    usb serial:       ...
    port status:      0x18 (selected, no error)
    model/firmware:   ...
    serial number:    ...
    head resistance:  ...
    magnetic encoder: Yes
    smart card:       HID iCLASS
    ribbon type:      2 (YMCKO)
    remaining count:  87
    panels printed:   1234
    result:           READY

From `model/firmware` down, the fields are the ones on the Windows driver's "Device Information"
page. A field the printer does not answer shows as `N/A` and does not stop a job. The `smart card`
line is worked out from the model/firmware reply (for example HID iCLASS or MIFARE contactless),
which makes it a quick way to see whether a unit has an RFID encoder.

With no job file that is all it does, and it exits 0 if the printer is ready and 1 if not, so
it works as a health check in scripts. With a job file it refuses to send to a printer that is
not ready (offline, error, no ribbon, no reply); `--force` overrides that and `--no-check`
skips the checks. Nothing is printed and no job is started by the check.

The output above is illustrative: none of this has run against a printer yet.

### Finding the printer

By default the printer is found automatically: every USB printer-class device is listed and the
one whose IEEE-1284 id looks like a J1x0 is used. If that finds nothing, or several, `send`
prints what it saw and you can pick one with the IDs from `system_profiler SPUSBDataType`
(macOS) or `lsusb` (Linux):

    $ j1x0 send --vid 0x1234 --pid 0x5678

If you find the IDs, please open an issue or pull request so they can become defaults.

First test print
----------------

Start with [`examples/test-card.svg`](examples/test-card.svg). It has no fields:

    $ j1x0 preview examples/test-card.svg -o out/        # check out/colour.png and out/k.png
    $ j1x0 render  examples/test-card.svg -o test.bin
    $ j1x0 send                                          # first, just check the link
    $ j1x0 send    test.bin

It is designed to answer the open questions about a new printer:

* **Edge bands**, one per plane (top cyan, right magenta, bottom yellow, left grey), with mm
  ticks starting at the card edge. A white gap means that plane is missing or shifted; the
  last visible tick shows how much of the image is clipped.
* **Corner markers** that are all different (filled square, hollow square, X, circle), so a
  rotated or mirrored card is obvious. See [Orientation](#orientation).
* **Gradients** for cyan, magenta, yellow and composite black, each driving its own plane,
  with a 16-step strip beneath. Banding or a plane that prints too light shows up here.
* **Registration**: black resin lines and a crosshair run across the colour bars, so any
  offset between the resin panel and the dye panels is visible.
* **Resin detail**: text at four sizes and blocks of 0.1 to 0.3 mm lines.

Writing a template
------------------

* Draw the card at CR80 size (85.6 x 54 mm). It is rendered to 1024 x 640 pixels, which
  stretches it by under 1%, as the vendor driver does.
* Put `$name$` anywhere in a text object or an attribute. Names are letters, digits and
  underscores. A token may be split across several `<tspan>`s by Inkscape; that works.
* For a photo, set an image's `xlink:href` to `$photo$` and pass a file path as the value.
  The file is embedded in the render.
* Put black resin artwork (text, barcodes) in a layer named exactly **`K`**. It is printed with
  the black resin panel, which is sharper and more durable than black built from Y/M/C.
  Without a `K` layer the card is printed from the colour panels only.
* If you write SVG by hand, add `xml:space="preserve"` to text elements that contain spaces
  between tspans. Inkscape adds it automatically when it saves.

See [`examples/card.svg`](examples/card.svg).

Python API
----------

```python
from j1x0_cardprinter import J1x0USB, Printer, Template
from j1x0_cardprinter.probe import check_link

template = Template.from_file("examples/card.svg")
values = {"firstName": "Ada", "lastName": "Lovelace", "cardNo": "0042"}

printer = Printer()
job = printer.build(template, values)          # bytes; no hardware needed

with J1x0USB.discover() as link:               # or discover(vid=..., pid=...)
    report = check_link(link)                  # USB status, model, ribbon
    print("\n".join(report.lines()))
    if report.ready:
        link.write(job)
```

`J1x0MockUSB` records writes (`.sent`) and queries (`.queries`) instead of using USB, and lets
you set the status byte and replies, which is handy for tests.

Orientation
-----------

How the image is mirrored or rotated in the data stream has not been confirmed against a
printer. If your first test card comes out rotated or mirrored, try the flags on `render` and
`print` (`Orientation` in the Python API):

    $ j1x0 print card.svg ... --flip-lines --flip-dots     # a 180 degree turn

An asymmetric test card, such as a large letter F in one corner, makes this quick to settle.

Documentation
-------------

The print protocol is documented in [docs/](docs/), starting with
[docs/protocol.md](docs/protocol.md). Each statement there is marked as read from the
decompiled code, inferred, or guessed, and [docs/open-questions.md](docs/open-questions.md)
lists what needs checking on hardware.

Development
-----------

    $ git clone git@github.com:TeaEngineering/j1x0-cardprinter.git
    $ cd j1x0-cardprinter
    $ pip install -e ".[dev]"
    $ python -m pytest

The tests need no printer and no Inkscape. CI runs them on Python 3.10 to 3.13.

### Releasing

The version comes from the git tag (via `hatch-vcs`). Publishing a GitHub release runs
`.github/workflows/publish.yml`, which builds the sdist and wheel, checks the metadata, runs
the tests, and uploads to PyPI with
[trusted publishing](https://docs.pypi.org/trusted-publishers/). One-time setup on PyPI: add a
trusted publisher for owner `TeaEngineering`, repository `j1x0-cardprinter`, workflow
`publish.yml`, environment `pypi`.

Licence
-------

MIT; see [LICENSE](LICENSE).

Disclaimer
----------

This is an independent project. It is not affiliated with, authorised or approved by NBS
Technologies, the maker of the Javelin printers. Product names are used only to say which
printers the software is intended for. The protocol was worked out by analysing the
vendor's Windows driver; none of the vendor's code or files is included in this repository.
