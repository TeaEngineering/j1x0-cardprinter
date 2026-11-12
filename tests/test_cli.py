from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from j1x0_cardprinter import cli
from j1x0_cardprinter.usb import J1x0MockUSB

EXAMPLES = Path(__file__).parent.parent / "examples"
TEST_CARD = str(EXAMPLES / "test-card.svg")
CARD = str(EXAMPLES / "card.svg")
VALUES = ["-s", "firstName=Ada", "-s", "lastName=Lovelace", "-s", "cardNo=1"]


class FakeRasteriser:
    """Stands in for Inkscape: a horizontal gradient, so the dither has something to randomise."""

    def render(self, svg, width, height):
        row = np.linspace(0, 255, width).astype(np.uint8)
        return Image.fromarray(np.tile(row, (height, 1))).convert("RGB")


@pytest.fixture
def fakes(monkeypatch):
    link = J1x0MockUSB()
    monkeypatch.setattr(cli, "InkscapeRasteriser", FakeRasteriser)
    monkeypatch.setattr(cli.J1x0USB, "discover",
                        classmethod(lambda cls, vid=None, pid=None, timeout=5000, soft_reset=False: link))
    return link


def test_render_writes_a_job_file(fakes, tmp_path, capsys):
    out = tmp_path / "job.bin"
    assert cli.main(["render", TEST_CARD, "-o", str(out)]) == 0
    assert out.read_bytes().startswith(cli.JOB_PREFIX)
    assert "wrote" in capsys.readouterr().out
    assert fakes.sent == []                      # render never touches the printer


def test_build_is_an_alias_for_render(fakes, tmp_path):
    out = tmp_path / "job.bin"
    assert cli.main(["build", TEST_CARD, "-o", str(out)]) == 0
    assert out.exists()


def test_seed_makes_renders_repeatable(fakes, tmp_path):
    a, b, c = (tmp_path / n for n in ("a.bin", "b.bin", "c.bin"))
    cli.main(["render", TEST_CARD, "--seed", "7", "-o", str(a)])
    cli.main(["render", TEST_CARD, "--seed", "7", "-o", str(b)])
    cli.main(["render", TEST_CARD, "--seed", "8", "-o", str(c)])
    assert a.read_bytes() == b.read_bytes()
    assert a.read_bytes() != c.read_bytes()


def test_send_writes_the_file_exactly(fakes, tmp_path):
    job = tmp_path / "job.bin"
    job.write_bytes(cli.JOB_PREFIX + b"payload")
    assert cli.main(["send", str(job), "--vid", "0x1", "--pid", "0x2"]) == 0
    assert fakes.stream == job.read_bytes()
    assert len(fakes.queries) == 7                  # the link was checked first


def test_send_without_a_job_only_checks_the_link(fakes, capsys):
    assert cli.main(["send"]) == 0                  # no VID/PID needed either
    out = capsys.readouterr().out
    assert "port status" in out and out.rstrip().endswith("READY")
    assert fakes.sent == [] and len(fakes.queries) == 7


def test_check_only_exits_1_when_not_ready(fakes, capsys):
    fakes._status = 0x00
    assert cli.main(["send"]) == 1
    assert "NOT READY" in capsys.readouterr().out
    assert fakes.sent == []


def test_not_ready_blocks_the_job_unless_forced(fakes, tmp_path, capsys):
    fakes._status = 0x00
    job = tmp_path / "job.bin"
    job.write_bytes(cli.JOB_PREFIX + b"payload")
    assert cli.main(["send", str(job)]) == 1
    assert fakes.sent == [] and "--force" in capsys.readouterr().err
    assert cli.main(["send", str(job), "--force"]) == 0
    assert fakes.stream == job.read_bytes()


def test_no_check_skips_the_queries(fakes, tmp_path):
    job = tmp_path / "job.bin"
    job.write_bytes(cli.JOB_PREFIX)
    assert cli.main(["send", str(job), "--no-check"]) == 0
    assert fakes.queries == [] and fakes.stream == cli.JOB_PREFIX


def test_autodetect_failure_is_reported_cleanly(monkeypatch, capsys):
    def none_found(cls, vid=None, pid=None, timeout=5000, soft_reset=False):
        raise RuntimeError("no USB printers found")

    monkeypatch.setattr(cli.J1x0USB, "discover", classmethod(none_found))
    assert cli.main(["send"]) == 1
    assert "no USB printers found" in capsys.readouterr().err


def test_send_warns_about_unrecognised_files(fakes, tmp_path, capsys):
    job = tmp_path / "other.bin"
    job.write_bytes(b"not a job")
    assert cli.main(["send", str(job), "--vid", "1", "--pid", "2"]) == 0
    assert "does not start with ESC MASTER" in capsys.readouterr().err
    assert fakes.stream == b"not a job"


def test_send_rejects_an_empty_file(fakes, tmp_path):
    job = tmp_path / "empty.bin"
    job.write_bytes(b"")
    with pytest.raises(SystemExit):
        cli.main(["send", str(job), "--vid", "1", "--pid", "2"])
    assert fakes.sent == []


def test_send_reports_a_missing_printer_without_a_traceback(monkeypatch, tmp_path, capsys):
    def not_found(cls, vid=None, pid=None, timeout=5000, soft_reset=False):
        raise RuntimeError("J1x0 printer not found")

    monkeypatch.setattr(cli.J1x0USB, "discover", classmethod(not_found))
    job = tmp_path / "job.bin"
    job.write_bytes(cli.JOB_PREFIX)
    assert cli.main(["send", str(job), "--vid", "1", "--pid", "2"]) == 1
    assert "not found" in capsys.readouterr().err


def test_print_equals_render_then_send(fakes, tmp_path):
    rendered = tmp_path / "rendered.bin"
    saved = tmp_path / "saved.bin"
    cli.main(["render", TEST_CARD, "--seed", "3", "-o", str(rendered)])
    assert cli.main(["print", TEST_CARD, "--seed", "3", "-o", str(saved),
                     "--vid", "1", "--pid", "2"]) == 0
    assert saved.read_bytes() == rendered.read_bytes()
    assert fakes.stream == rendered.read_bytes()


def test_print_without_save_leaves_no_file_behind(fakes, tmp_path, capsys):
    assert cli.main(["print", TEST_CARD, "--vid", "1", "--pid", "2"]) == 0
    assert fakes.stream.startswith(cli.JOB_PREFIX)
    sent_from = capsys.readouterr().out.split(" from ")[-1].strip()
    assert not Path(sent_from).exists()


def test_missing_field_exits_with_status_2(fakes, tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["render", CARD, "-o", str(tmp_path / "x.bin")])
    assert exc.value.code == 2
    assert "firstName" in capsys.readouterr().err


def test_render_with_fields(fakes, tmp_path):
    out = tmp_path / "card.bin"
    assert cli.main(["render", CARD, *VALUES, "-o", str(out)]) == 0
    assert out.stat().st_size > 1000
