"""SVG card templates: discover `$token$` fields and substitute values.

Tokens may appear in text (including text that Inkscape has split across several
`<tspan>`s) and in any attribute. When a token sits in an `href` and the substituted
value is a path to an existing file, the file is embedded as a data URI so the
rasteriser does not depend on the working directory.

Inkscape layers (groups with `inkscape:groupmode="layer"`) can be hidden at render
time; `raster.render_card` uses this to split the K (black resin) layer from colour.
"""

from __future__ import annotations

import base64
import copy
import mimetypes
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable, Mapping

SVG = "http://www.w3.org/2000/svg"
INKSCAPE = "http://www.inkscape.org/namespaces/inkscape"
SODIPODI = "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"
XLINK = "http://www.w3.org/1999/xlink"

for _prefix, _uri in (("", SVG), ("inkscape", INKSCAPE), ("sodipodi", SODIPODI), ("xlink", XLINK)):
    ET.register_namespace(_prefix, _uri)

TOKEN = re.compile(r"\$(\w+)\$")

_TEXT_BLOCKS = {f"{{{SVG}}}text", f"{{{SVG}}}flowRoot"}
_HREF = {f"{{{XLINK}}}href", "href"}
_LAYER_MODE = f"{{{INKSCAPE}}}groupmode"
_LAYER_LABEL = f"{{{INKSCAPE}}}label"
# Root children that draw nothing; they must stay visible (gradients, styles, ...) when hiding the rest.
_NON_GRAPHIC = {"defs", "metadata", "namedview", "style", "title", "desc", "script"}


class MissingField(KeyError):
    """A `$token$` in the template has no value."""


class Template:
    """A parsed SVG card design."""

    def __init__(self, root: ET.Element, base_dir: Path | None = None) -> None:
        self._root = root
        self.base_dir = base_dir

    # ── Construction ─────────────────────────────────────────────────────────

    @classmethod
    def from_file(cls, path: str | Path) -> "Template":
        path = Path(path)
        return cls(ET.parse(path).getroot(), base_dir=path.resolve().parent)

    @classmethod
    def from_string(cls, svg: str | bytes, base_dir: Path | None = None) -> "Template":
        return cls(ET.fromstring(svg), base_dir=base_dir)

    # ── Introspection ────────────────────────────────────────────────────────

    def fields(self) -> list[str]:
        """Token names in first-seen order."""
        seen: dict[str, None] = {}
        for elem in self._root.iter():
            if elem.tag in _TEXT_BLOCKS:
                combined = "".join(self._text_slots_values(elem))
                for name in TOKEN.findall(combined):
                    seen.setdefault(name)
            for value in elem.attrib.values():
                for name in TOKEN.findall(value):
                    seen.setdefault(name)
        return list(seen)

    def layer_names(self) -> list[str]:
        return [g.get(_LAYER_LABEL, "") for g in self._layers(self._root)]

    def image_fields(self) -> list[str]:
        """Fields used in an `href` (a photo or logo): the value is a path to an image file."""
        names: dict[str, None] = {}
        for elem in self._root.iter():
            for key, value in elem.attrib.items():
                if key in _HREF:
                    for name in TOKEN.findall(value):
                        names.setdefault(name)
        return list(names)

    def cairosvg_problems(self) -> list[str]:
        """SVG features in this design that cairosvg ignores or draws wrongly.

        Found by comparing it with Inkscape (docs/rendering.md). An empty list means the design
        only uses features both renderers handle, not that the output is pixel-identical.
        """
        found: set[str] = set()
        by_tag = {"filter": "filters (blur, drop shadow)", "mask": "masks",
                  "pattern": "pattern fills", "flowRoot": "flowed text (flowRoot)"}
        by_property = {
            "filter": "filters (blur, drop shadow)", "mask": "masks",
            "shape-inside": "wrapped text boxes", "mix-blend-mode": "blend modes",
            "word-spacing": "word spacing", "font-variant": "small caps",
        }
        for elem in self._root.iter():
            tag = elem.tag.rsplit("}", 1)[-1]
            if tag in by_tag:
                found.add(by_tag[tag])
            props = dict(elem.attrib)
            for declaration in elem.get("style", "").split(";"):
                name, sep, value = declaration.partition(":")
                if sep:
                    props[name.strip()] = value.strip()
            for name, label in by_property.items():
                value = props.get(name)
                if value is None or value in ("none", "normal", "inherit"):
                    continue
                if name == "font-variant" and value != "small-caps":
                    continue
                found.add(label)
            if props.get("text-decoration", "none") not in ("none", "inherit"):
                found.add("underline / strikethrough")
            if props.get("font-style", "normal") in ("italic", "oblique"):
                found.add("italic text")
            if "textLength" in props:
                found.add("textLength")
        return sorted(found)

    # ── Rendering ────────────────────────────────────────────────────────────

    def render(
        self,
        values: Mapping[str, object],
        *,
        only_layers: Iterable[str] | None = None,
        hide_layers: Iterable[str] = (),
    ) -> bytes:
        """Return the SVG with tokens substituted.

        `only_layers` shows only the listed layers: every other layer is hidden, and so is any
        loose object outside all layers (it belongs to the colour artwork). `hide_layers` hides
        the named layers.
        """
        root = copy.deepcopy(self._root)
        for elem in root.iter():
            if elem.tag in _TEXT_BLOCKS:
                self._fill_text_block(elem, values)
            for key, value in list(elem.attrib.items()):
                if TOKEN.search(value):
                    new = self._substitute(value, values)
                    if key in _HREF:
                        new = self._embed_if_file(new)
                    elem.set(key, new)

        only = None if only_layers is None else set(only_layers)
        hide = set(hide_layers)
        for layer in self._layers(root):
            label = layer.get(_LAYER_LABEL, "")
            if (only is not None and label not in only) or label in hide:
                self._hide(layer)
        if only is not None:
            for child in root:
                if child.tag.rsplit("}", 1)[-1] in _NON_GRAPHIC:
                    continue
                if not any(g.get(_LAYER_LABEL, "") in only for g in self._layers(child)):
                    self._hide(child)

        if self.base_dir is not None:
            root.set(f"{{{SODIPODI}}}docbase", str(self.base_dir))
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    # ── Internals ────────────────────────────────────────────────────────────

    @staticmethod
    def _layers(root: ET.Element) -> list[ET.Element]:
        return [g for g in root.iter(f"{{{SVG}}}g") if g.get(_LAYER_MODE) == "layer"]

    @staticmethod
    def _hide(elem: ET.Element) -> None:
        style = elem.get("style", "")
        if "display:none" not in style:
            elem.set("style", f"{style};display:none" if style else "display:none")

    @staticmethod
    def _text_slots(block: ET.Element) -> list[tuple[ET.Element, str]]:
        """(element, 'text'|'tail') slots holding character data inside `block`, in order."""
        slots: list[tuple[ET.Element, str]] = []

        def walk(elem: ET.Element) -> None:
            if elem.text:
                slots.append((elem, "text"))
            for child in elem:
                walk(child)
                if child.tail:
                    slots.append((child, "tail"))

        walk(block)
        return slots

    def _text_slots_values(self, block: ET.Element) -> list[str]:
        return [getattr(e, attr) for e, attr in self._text_slots(block)]

    @staticmethod
    def _substitute(text: str, values: Mapping[str, object]) -> str:
        def repl(match: re.Match) -> str:
            name = match.group(1)
            if name not in values:
                raise MissingField(name)
            return str(values[name])

        return TOKEN.sub(repl, text)

    def _fill_text_block(self, block: ET.Element, values: Mapping[str, object]) -> None:
        slots = self._text_slots(block)
        pieces = [getattr(e, attr) for e, attr in slots]
        combined = "".join(pieces)
        if not TOKEN.search(combined):
            return
        if sum(len(TOKEN.findall(p)) for p in pieces) == len(TOKEN.findall(combined)):
            for (elem, attr), piece in zip(slots, pieces):
                setattr(elem, attr, self._substitute(piece, values))
        else:
            # A token is split across tspans: put the whole string in the first run (keeps
            # that run's style) and blank the rest.
            first, *rest = slots
            setattr(first[0], first[1], self._substitute(combined, values))
            for elem, attr in rest:
                setattr(elem, attr, "")

    def _embed_if_file(self, value: str) -> str:
        path = Path(value).expanduser()
        if not path.is_absolute() and self.base_dir is not None:
            path = self.base_dir / path
        if not path.is_file():
            return value
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
