"""The card design library: the SVG files in `~/.j1x0`.

Each `*.svg` directly inside the folder is offered as a design. The folder can be changed with
the `J1X0_TEMPLATES` environment variable or `j1x0 --templates DIR`. Images a design links to
by relative path are looked up next to the SVG.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .template import Template

ENV_VAR = "J1X0_TEMPLATES"


def default_dir() -> Path:
    return Path(os.environ.get(ENV_VAR) or Path.home() / ".j1x0").expanduser()


def humanise(name: str) -> str:
    """A readable label for a field or file name: `firstName` -> `First name`, `staff_badge` ->
    `Staff badge`. Words in capitals (`ID`, `URL`) are kept."""
    spaced = re.sub(r"[_\-\s]+", " ", name)
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", spaced)        # camelCase boundaries
    words = spaced.split()
    if not words:
        return name
    first, rest = words[0], words[1:]
    label = [first[:1].upper() + first[1:]]
    label += [w if (w.isupper() and len(w) > 1) else w.lower() for w in rest]
    return " ".join(label)


@dataclass(frozen=True)
class Design:
    name: str           # the file name without .svg; unique within the folder
    title: str          # what the user sees
    path: Path

    def load(self) -> Template:
        return Template.from_file(self.path)


def discover(directory: Path) -> list[Design]:
    """The designs in `directory`, sorted by title. Missing folder = no designs."""
    if not directory.is_dir():
        return []
    designs = [
        Design(name=p.stem, title=humanise(p.stem), path=p)
        for p in directory.glob("*.svg")
        if p.is_file() and not p.name.startswith(".")
    ]
    return sorted(designs, key=lambda d: d.title.lower())
