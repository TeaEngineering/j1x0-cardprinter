import xml.etree.ElementTree as ET

from j1x0_cardprinter.template import Template

SVG = """<svg xmlns="http://www.w3.org/2000/svg"
  xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"
  xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd">
  <defs id="defs"><linearGradient id="g"/></defs>
  <sodipodi:namedview id="view"/>
  <rect id="loose" width="5" height="5"/>
  <g id="design" inkscape:groupmode="layer" inkscape:label="Design"><rect id="art" width="9" height="9"/></g>
  <g id="k" inkscape:groupmode="layer" inkscape:label="K"><text id="resin">x</text></g>
</svg>"""


def hidden(svg: bytes) -> list[str]:
    """Ids of the elements that carry display:none, in document order."""
    root = ET.fromstring(svg)
    return [e.get("id") for e in root.iter() if "display:none" in e.get("style", "")]


def test_k_only_render_hides_other_layers_and_loose_objects():
    out = Template.from_string(SVG).render({}, only_layers=["K"])
    assert hidden(out) == ["loose", "design"]
    assert out.count(b"display:none") == 2                   # hidden once each, not twice


def test_the_defs_and_view_survive_so_styling_still_works():
    out = Template.from_string(SVG).render({}, only_layers=["K"])
    assert "defs" not in hidden(out) and "view" not in hidden(out)
    assert b"linearGradient" in out and b"namedview" in out


def test_colour_render_keeps_loose_objects_and_hides_only_the_named_layer():
    out = Template.from_string(SVG).render({}, hide_layers=["K"])
    assert hidden(out) == ["k"]


def test_a_full_render_hides_nothing():
    assert hidden(Template.from_string(SVG).render({})) == []


def test_a_nested_layer_keeps_its_container_visible():
    nested = """<svg xmlns="http://www.w3.org/2000/svg" xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape">
      <g id="wrapper"><g inkscape:groupmode="layer" inkscape:label="K"><text>x</text></g></g>
    </svg>"""
    assert hidden(Template.from_string(nested).render({}, only_layers=["K"])) == []
