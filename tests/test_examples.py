from pathlib import Path

import pytest

from j1x0_cardprinter.template import Template

EXAMPLES = Path(__file__).parent.parent / "examples"


def test_card_example():
    t = Template.from_file(EXAMPLES / "card.svg")
    assert t.fields() == ["firstName", "lastName", "cardNo"]
    assert t.layer_names() == ["Design", "K"]


def test_test_card_needs_no_fields_and_has_both_layers():
    t = Template.from_file(EXAMPLES / "test-card.svg")
    assert t.fields() == []
    assert t.layer_names() == ["Design", "K"]


@pytest.mark.parametrize("hide, shown", [(["K"], "Design"), (["Design"], "K")])
def test_test_card_layers_render_independently(hide, shown):
    svg = Template.from_file(EXAMPLES / "test-card.svg").render({}, hide_layers=hide).decode()
    assert svg.count("display:none") == 1
    assert shown in svg
