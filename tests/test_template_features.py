from pathlib import Path

import pytest

from j1x0_cardprinter.template import Template

EXAMPLES = Path(__file__).parent.parent / "examples"


def tpl(body: str) -> Template:
    return Template.from_string(
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
        + body + "</svg>")


def test_image_fields_are_the_tokens_used_in_an_href():
    template = tpl('<text>$name$</text><image xlink:href="$photo$"/><image href="$logo$"/>')
    assert template.image_fields() == ["photo", "logo"]
    assert template.fields() == ["name", "photo", "logo"]


@pytest.mark.parametrize("body, problem", [
    ('<filter id="f"/>', "filters"),
    ('<rect filter="url(#f)"/>', "filters"),
    ('<rect style="fill:red;filter:url(#f)"/>', "filters"),
    ('<mask id="m"/>', "masks"),
    ('<pattern id="p"/>', "pattern fills"),
    ('<flowRoot/>', "flowed text"),
    ('<text style="shape-inside:url(#r)">x</text>', "wrapped text boxes"),
    ('<rect style="mix-blend-mode:multiply"/>', "blend modes"),
    ('<text style="font-style:italic">x</text>', "italic"),
    ('<text font-style="oblique">x</text>', "italic"),
    ('<text text-decoration="underline">x</text>', "underline"),
    ('<text word-spacing="2">x</text>', "word spacing"),
    ('<text textLength="10">x</text>', "textLength"),
    ('<text style="font-variant:small-caps">x</text>', "small caps"),
])
def test_cairosvg_problems_are_reported(body, problem):
    assert any(problem in p for p in tpl(body).cairosvg_problems())


def test_a_plain_design_has_no_cairosvg_problems():
    template = tpl('<rect fill="red"/>'
                   '<text font-weight="bold" style="font-style:normal;filter:none;font-variant:normal">x</text>')
    assert template.cairosvg_problems() == []


@pytest.mark.parametrize("name", ["card.svg", "test-card.svg"])
def test_the_examples_only_use_features_cairosvg_handles(name):
    assert Template.from_file(EXAMPLES / name).cairosvg_problems() == []
