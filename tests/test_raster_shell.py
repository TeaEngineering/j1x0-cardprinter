"""InkscapeShellRasteriser against a fake `inkscape --shell` that speaks the same prompt protocol."""

import os
import stat
import sys
import threading

import pytest

from j1x0_cardprinter.raster import InkscapeShellRasteriser, RENDERERS, make_rasteriser

FAKE = '''#!{python}
import os, re, sys
from PIL import Image

mode = os.environ.get("FAKE_MODE", "normal")
marker = os.environ.get("FAKE_MARKER", "")

def prompt():
    sys.stdout.write("> ")
    sys.stdout.flush()

sys.stdout.write("Inkscape interactive shell mode.\\n")
prompt()
for line in sys.stdin:
    line = line.strip()
    if line == "quit":
        break
    if mode == "die_once" and marker and not os.path.exists(marker):
        open(marker, "w").close()
        sys.exit(1)
    if mode == "fail":
        print("file_open: boom")
        prompt()
        continue
    out = re.search(r"export-filename:([^;]+)", line).group(1).strip()
    width = int(re.search(r"export-width:(\\d+)", line).group(1))
    height = int(re.search(r"export-height:(\\d+)", line).group(1))
    Image.new("RGB", (width, height), (10, 20, 30)).save(out)
    print(line)
    prompt()
'''


@pytest.fixture
def fake_inkscape(tmp_path):
    script = tmp_path / "inkscape"
    script.write_text(FAKE.format(python=sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


@pytest.fixture
def shell(fake_inkscape):
    rasteriser = InkscapeShellRasteriser(binary=fake_inkscape)
    yield rasteriser
    rasteriser.close()


def test_renders_the_requested_size(shell):
    image = shell.render(b"<svg/>", 64, 40)
    assert image.size == (64, 40) and image.getpixel((0, 0)) == (10, 20, 30)


def test_one_process_serves_many_renders(shell):
    shell.render(b"<svg/>", 8, 8)
    pid = shell._proc.pid
    for _ in range(3):
        shell.render(b"<svg/>", 8, 8)
    assert shell._proc.pid == pid


def test_close_stops_the_process_and_cleans_up(shell):
    shell.render(b"<svg/>", 8, 8)
    proc, tmp = shell._proc, shell._tmp
    shell.close()
    assert proc.poll() is not None and not tmp.exists()
    assert shell.render(b"<svg/>", 8, 8).size == (8, 8)        # starts again on demand


def test_a_failed_export_is_reported_with_inkscapes_message(monkeypatch, shell):
    monkeypatch.setenv("FAKE_MODE", "fail")
    with pytest.raises(RuntimeError, match="boom"):
        shell.render(b"<svg/>", 8, 8)


def test_a_dead_process_is_restarted_once(monkeypatch, shell, tmp_path):
    monkeypatch.setenv("FAKE_MODE", "die_once")
    monkeypatch.setenv("FAKE_MARKER", str(tmp_path / "died"))
    assert shell.render(b"<svg/>", 8, 8).size == (8, 8)
    assert (tmp_path / "died").exists()


def test_a_missing_inkscape_is_a_clear_error():
    with pytest.raises(RuntimeError, match="not found"):
        InkscapeShellRasteriser(binary="definitely-not-installed-inkscape").render(b"<svg/>", 8, 8)


def test_concurrent_renders_are_serialised(shell):
    sizes, errors = [], []

    def work(n):
        try:
            sizes.append(shell.render(b"<svg/>", 10 + n, 10).size)
        except Exception as exc:                        # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and sorted(sizes) == [(10, 10), (11, 10), (12, 10), (13, 10)]


def test_make_rasteriser():
    assert RENDERERS == ("inkscape", "inkscape-shell", "cairosvg", "auto")
    assert type(make_rasteriser("inkscape-shell")).__name__ == "InkscapeShellRasteriser"
    assert type(make_rasteriser("cairosvg")).__name__ == "CairoRasteriser"
    with pytest.raises(ValueError):
        make_rasteriser("nope")
