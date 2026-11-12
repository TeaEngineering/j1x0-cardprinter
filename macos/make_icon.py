#!/usr/bin/env python3
"""Draw the app icon and write it as an .icns file:  python macos/make_icon.py OUT.icns

A tilted card with the four print colours (cyan, magenta, yellow, black) on a rounded square. Needs Pillow, and
the macOS `iconutil` command.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

SIZE = 1024
SCALE = 2                                        # draw at twice the size, then shrink: smooth edges


def rounded(draw: ImageDraw.ImageDraw, box, radius, **kwargs) -> None:
    draw.rounded_rectangle(box, radius=radius, **kwargs)


def gradient(size, top, bottom) -> Image.Image:
    column = Image.linear_gradient("L").resize((1, size[1]))
    ramp = Image.merge("RGB", [column.point([int(t + (b - t) * v / 255) for v in range(256)])
                               for t, b in zip(top, bottom)])
    return ramp.resize(size)


def draw_icon() -> Image.Image:
    s = SIZE * SCALE
    canvas = Image.new("RGBA", (s, s), (0, 0, 0, 0))

    # the rounded square macOS expects: artwork 824 of 1024, with a soft shadow
    inset, radius = 100 * SCALE, 185 * SCALE
    plate = gradient((s - 2 * inset,) * 2, (46, 62, 112), (22, 29, 62)).convert("RGBA")
    mask = Image.new("L", plate.size, 0)
    rounded(ImageDraw.Draw(mask), (0, 0, *plate.size), radius, fill=255)
    shadow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    shadow.paste((0, 0, 0, 120), (inset, inset + 10 * SCALE), mask)
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(14 * SCALE)))
    canvas.paste(plate, (inset, inset), mask)

    # the card, drawn flat then tilted
    cw, ch = 560 * SCALE, 352 * SCALE
    card = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    d = ImageDraw.Draw(card)
    rounded(d, (0, 0, cw - 1, ch - 1), 36 * SCALE, fill=(250, 250, 248, 255))
    rounded(d, (0, 0, cw - 1, ch - 1), 36 * SCALE, outline=(205, 208, 214, 255), width=3 * SCALE)
    d.ellipse((48 * SCALE, 52 * SCALE, 168 * SCALE, 172 * SCALE), fill=(205, 213, 230, 255))   # the photo
    for y, w in ((70, 250), (112, 190), (154, 130)):                                        # lines of text
        rounded(d, (200 * SCALE, y * SCALE, (200 + w) * SCALE, (y + 20) * SCALE), 10 * SCALE, fill=(60, 66, 84, 255))
    colours = [(0, 190, 232), (230, 0, 126), (255, 224, 0), (30, 30, 34)]                  # C, M, Y, K
    bar_w = (cw - 96 * SCALE) // 4
    for i, colour in enumerate(colours):
        x0 = 48 * SCALE + i * bar_w
        d.rectangle((x0, 232 * SCALE, x0 + bar_w, 296 * SCALE), fill=colour + (255,))
    clip = Image.new("L", (cw, ch), 0)
    rounded(ImageDraw.Draw(clip), (0, 0, cw - 1, ch - 1), 36 * SCALE, fill=255)
    card.putalpha(Image.composite(card.getchannel("A"), Image.new("L", (cw, ch), 0), clip))
    card = card.rotate(14, resample=Image.BICUBIC, expand=True)

    card_shadow = Image.new("RGBA", card.size, (0, 0, 0, 0))
    card_shadow.paste((0, 0, 0, 110), (0, 0), card.getchannel("A"))
    pos = ((s - card.width) // 2, (s - card.height) // 2)
    canvas.alpha_composite(card_shadow.filter(ImageFilter.GaussianBlur(12 * SCALE)), (pos[0], pos[1] + 14 * SCALE))
    canvas.alpha_composite(card, pos)
    return canvas.resize((SIZE, SIZE), Image.LANCZOS)


def write_icns(out: Path) -> None:
    master = draw_icon()
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "AppIcon.iconset"
        iconset.mkdir()
        for base in (16, 32, 128, 256, 512):
            for factor in (1, 2):
                px = base * factor
                name = f"icon_{base}x{base}{'@2x' if factor == 2 else ''}.png"
                master.resize((px, px), Image.LANCZOS).save(iconset / name)
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)], check=True)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    write_icns(Path(sys.argv[1]))
    print("wrote", sys.argv[1])
