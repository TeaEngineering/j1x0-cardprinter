import pytest
from PIL import Image

from j1x0_cardprinter import cli, webui
from j1x0_cardprinter.library import default_dir
from j1x0_cardprinter.raster import CairoRasteriser, InkscapeShellRasteriser


class FakeRasteriser:
    def render(self, svg, width, height):
        return Image.new("RGB", (width, height), "white")


@pytest.fixture
def served(monkeypatch):
    """Capture what `j1x0 ui` would serve instead of starting a server."""
    seen = {}

    def fake_serve(app, **options):
        seen["app"], seen["options"] = app, options

    monkeypatch.setattr(webui, "serve", fake_serve)
    yield seen
    if "app" in seen:
        seen["app"].close()


def test_no_arguments_opens_the_ui(served, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("J1X0_TEMPLATES", raising=False)
    assert cli.main([]) == 0
    assert served["app"].designs_dir == tmp_path / ".j1x0" == default_dir()
    assert served["options"] == {"host": "127.0.0.1", "port": 8730, "open_browser": True}
    assert isinstance(served["app"].rasteriser, InkscapeShellRasteriser)


def test_ui_options(served, tmp_path):
    assert cli.main(["ui", "--templates", str(tmp_path), "--no-browser", "--port", "9000",
                     "--host", "0.0.0.0", "--renderer", "cairosvg"]) == 0
    assert served["app"].designs_dir == tmp_path
    assert served["options"] == {"host": "0.0.0.0", "port": 9000, "open_browser": False}
    assert isinstance(served["app"].rasteriser, CairoRasteriser)


def test_ui_honours_the_templates_environment_variable(served, monkeypatch, tmp_path):
    monkeypatch.setenv("J1X0_TEMPLATES", str(tmp_path / "cards"))
    cli.main(["ui", "--no-browser"])
    assert served["app"].designs_dir == tmp_path / "cards"


def test_ui_warns_when_inkscape_is_missing(served, monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    cli.main(["ui", "--no-browser"])
    assert "Inkscape was not found" in capsys.readouterr().err


def test_ui_does_not_warn_about_inkscape_for_cairosvg(served, monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    cli.main(["ui", "--no-browser", "--renderer", "cairosvg"])
    assert capsys.readouterr().err == ""


def test_choosing_cairosvg_warns_about_unsupported_features(monkeypatch, tmp_path, capsys):
    design = tmp_path / "shadow.svg"
    design.write_text('<svg xmlns="http://www.w3.org/2000/svg"><filter id="f"/><rect filter="url(#f)"/></svg>')
    monkeypatch.setattr(cli, "make_rasteriser", lambda name: FakeRasteriser())
    assert cli.main(["render", str(design), "--renderer", "cairosvg", "-o", str(tmp_path / "j.bin")]) == 0
    assert "cairosvg cannot draw: filters" in capsys.readouterr().err


def test_inkscape_never_warns_about_features(monkeypatch, tmp_path, capsys):
    design = tmp_path / "shadow.svg"
    design.write_text('<svg xmlns="http://www.w3.org/2000/svg"><filter id="f"/><rect filter="url(#f)"/></svg>')
    monkeypatch.setattr(cli, "make_rasteriser", lambda name: FakeRasteriser())
    cli.main(["render", str(design), "-o", str(tmp_path / "j.bin")])
    assert "cairosvg" not in capsys.readouterr().err
