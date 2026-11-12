"""Finding Inkscape, and choosing a renderer automatically."""

import pytest

from j1x0_cardprinter import raster
from j1x0_cardprinter.raster import (
    CairoRasteriser, InkscapeRasteriser, InkscapeShellRasteriser, find_inkscape, make_rasteriser,
)


def fake_inkscape(path):
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def nowhere(monkeypatch, tmp_path):
    """No Inkscape on the PATH and none in the usual places."""
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(raster, "INKSCAPE_LOCATIONS", (str(tmp_path / "missing" / "inkscape"),))


def test_inkscape_on_the_path_is_found(monkeypatch, tmp_path):
    binary = fake_inkscape(tmp_path / "inkscape")
    monkeypatch.setenv("PATH", str(tmp_path))
    assert find_inkscape() == str(binary)


def test_inkscape_is_found_where_the_download_puts_it_when_there_is_no_path(monkeypatch, tmp_path, nowhere):
    app = fake_inkscape(tmp_path / "Inkscape.app-inkscape")
    monkeypatch.setattr(raster, "INKSCAPE_LOCATIONS", (str(tmp_path / "missing" / "inkscape"), str(app)))
    assert find_inkscape() == str(app)


def test_a_missing_or_non_executable_inkscape_is_not_found(monkeypatch, tmp_path, nowhere):
    assert find_inkscape() is None
    plain = tmp_path / "inkscape"
    plain.write_text("not executable")
    monkeypatch.setattr(raster, "INKSCAPE_LOCATIONS", (str(plain),))
    assert find_inkscape() is None


def test_the_inkscape_rasterisers_use_the_found_binary(monkeypatch, tmp_path, nowhere):
    app = fake_inkscape(tmp_path / "found")
    monkeypatch.setattr(raster, "INKSCAPE_LOCATIONS", (str(app),))
    assert InkscapeRasteriser().binary == str(app)
    assert InkscapeShellRasteriser().binary == str(app)
    assert InkscapeRasteriser("custom").binary == "custom"              # an explicit binary still wins


def test_auto_prefers_inkscape(monkeypatch):
    monkeypatch.setattr(raster, "find_inkscape", lambda: "/somewhere/inkscape")
    monkeypatch.setattr(raster, "_have_cairosvg", lambda: True)
    assert isinstance(make_rasteriser("auto"), InkscapeShellRasteriser)


def test_auto_falls_back_to_cairosvg(monkeypatch):
    monkeypatch.setattr(raster, "find_inkscape", lambda: None)
    monkeypatch.setattr(raster, "_have_cairosvg", lambda: True)
    assert isinstance(make_rasteriser("auto"), CairoRasteriser)


def test_auto_with_nothing_available_says_what_to_install(monkeypatch):
    monkeypatch.setattr(raster, "find_inkscape", lambda: None)
    monkeypatch.setattr(raster, "_have_cairosvg", lambda: False)
    with pytest.raises(RuntimeError, match="Inkscape"):
        make_rasteriser("auto")


def test_auto_is_a_known_renderer_name():
    assert "auto" in raster.RENDERERS
