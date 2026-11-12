import pytest

from j1x0_cardprinter.template import MissingField, Template

SVG = """<svg xmlns="http://www.w3.org/2000/svg"
  xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"
  xmlns:xlink="http://www.w3.org/1999/xlink">
  <g inkscape:groupmode="layer" inkscape:label="Design">
    <text><tspan>$firstName$</tspan><tspan> $last</tspan><tspan>Name$</tspan></text>
    <image xlink:href="$photo$"/>
  </g>
  <g inkscape:groupmode="layer" inkscape:label="K"><text>ID $cardNo$</text></g>
</svg>"""


def test_fields_in_order_including_split_tokens_and_attributes():
    assert Template.from_string(SVG).fields() == ["firstName", "lastName", "photo", "cardNo"]


def test_layer_names():
    assert Template.from_string(SVG).layer_names() == ["Design", "K"]


def test_render_substitutes_and_escapes():
    t = Template.from_string(SVG)
    out = t.render({"firstName": "A&B", "lastName": "<x>", "photo": "nope.png", "cardNo": "7"}).decode()
    assert "A&amp;B" in out
    assert "&lt;x&gt;" in out
    assert "$" not in out
    assert "ID 7" in out


def test_split_token_keeps_first_run():
    t = Template.from_string(SVG)
    out = t.render({"firstName": "Ada", "lastName": "Lovelace", "photo": "p", "cardNo": "1"}).decode()
    assert "Ada" in out and " Lovelace" in out


def test_missing_field_raises():
    with pytest.raises(MissingField):
        Template.from_string(SVG).render({"firstName": "x"})


def test_href_embeds_existing_file(tmp_path):
    (tmp_path / "p.png").write_bytes(b"\x89PNG")
    t = Template.from_string(SVG, base_dir=tmp_path)
    out = t.render({"firstName": "a", "lastName": "b", "photo": "p.png", "cardNo": "1"}).decode()
    assert "data:image/png;base64," in out


def test_layer_hiding():
    t = Template.from_string(SVG)
    values = {"firstName": "a", "lastName": "b", "photo": "p", "cardNo": "1"}
    assert t.render(values, hide_layers=["K"]).decode().count("display:none") == 1
    assert t.render(values, only_layers=["K"]).decode().count("display:none") == 1
