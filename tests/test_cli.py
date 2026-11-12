from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from j1x0_cardprinter import cli
from j1x0_cardprinter.protocol import end_of_card
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
    monkeypatch.setattr(cli, "make_rasteriser", lambda name: FakeRasteriser())
    monkeypatch.setattr(cli.J1x0USB, "discover",
                        classmethod(lambda cls, vid=None, pid=None, timeout=5000, soft_reset=False: link))
    return link


def test_render_writes_a_job_file(fakes, tmp_path, capsys):
    out = tmp_path / "job.bin"
    assert cli.main(["render", TEST_CARD, "-o", str(out)]) == 0
    assert out.read_bytes().startswith(cli.JOB_PREFIX)
    assert "wrote" in capsys.readouterr().out
    assert fakes.sent == []                      # render never touches the printer


def test_preview_writes_colour_k_and_composite_by_default(fakes, tmp_path):
    assert cli.main(["preview", TEST_CARD, "-o", str(tmp_path)]) == 0
    assert sorted(p.name for p in tmp_path.glob("*.png")) == ["colour.png", "composite.png", "k.png"]
    colour, k, both = (np.asarray(Image.open(tmp_path / n).convert("RGB")) for n in
                       ("colour.png", "k.png", "composite.png"))
    assert colour.shape == k.shape == both.shape == (640, 1024, 3)
    black = (k == 0).all(axis=2)                       # the resin dots
    assert black.any() and (both[black] == 0).all()    # the composite has them in black
    assert (both[~black] == colour[~black]).all()      # and is the colour render everywhere else


def test_preview_can_skip_the_composite(fakes, tmp_path):
    assert cli.main(["preview", TEST_CARD, "--no-composite", "-o", str(tmp_path)]) == 0
    assert sorted(p.name for p in tmp_path.glob("*.png")) == ["colour.png", "k.png"]


def test_preview_of_a_design_without_resin_has_no_k_file(fakes, tmp_path):
    svg = tmp_path / "plain.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="85.6mm" height="54mm" '
                   'viewBox="0 0 85.6 54"><rect width="85.6" height="54" fill="#fff"/></svg>')
    out = tmp_path / "out"
    assert cli.main(["preview", str(svg), "-o", str(out)]) == 0
    assert sorted(p.name for p in out.glob("*.png")) == ["colour.png", "composite.png"]


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
    assert fakes.stream == job.read_bytes() + end_of_card()     # the file, then the end of the card
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
    assert fakes.stream == job.read_bytes() + end_of_card()


def test_no_check_skips_the_queries(fakes, tmp_path):
    job = tmp_path / "job.bin"
    job.write_bytes(cli.JOB_PREFIX)
    assert cli.main(["send", str(job), "--no-check"]) == 0
    assert fakes.queries == [] and fakes.stream == cli.JOB_PREFIX + end_of_card()


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
    assert fakes.stream == b"not a job" + end_of_card()


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
    assert fakes.stream == rendered.read_bytes() + end_of_card()


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


# ── Paced sending and protocol logging ───────────────────────────────────────

def small_job(tmp_path):
    job = tmp_path / "job.bin"
    job.write_bytes(cli.JOB_PREFIX + b"\x1bJOB 0\r\x1bIV 30\r")
    return job


def test_send_writes_in_chunks_and_asks_after_the_data_and_the_end_of_card(fakes, tmp_path):
    job = tmp_path / "big.bin"
    job.write_bytes(cli.JOB_PREFIX + b"x" * 9000)
    assert cli.main(["send", str(job)]) == 0
    assert [e for e in fakes.events if e[0] == "write"][:3] == [("write", 4096)] * 2 + [("write", 816)]
    assert fakes.enquiries == 2                       # after the data, and after ESC MO


def test_chunk_size_option(fakes, tmp_path):
    job = small_job(tmp_path)
    assert cli.main(["send", str(job), "--chunk", "10"]) == 0
    assert [e for e in fakes.events if e[0] == "write"][:3] == [("write", 10), ("write", 10), ("write", 2)]


def test_raw_writes_the_file_unpaced_and_unended(fakes, tmp_path, capsys):
    job = small_job(tmp_path)
    assert cli.main(["send", str(job), "--raw"]) == 0
    assert fakes.stream == job.read_bytes() and fakes.enquiries == 0
    assert "raw" in capsys.readouterr().out


def test_no_end_leaves_off_the_end_of_card_commands(fakes, tmp_path):
    job = small_job(tmp_path)
    assert cli.main(["send", str(job), "--no-end"]) == 0
    assert fakes.stream == job.read_bytes() and fakes.enquiries == 1


def test_whatever_the_ribbon_the_card_ends_with_mo(fakes, tmp_path):
    fakes._responses[b"\x1b!RIBBON\r"] = b"7\x06"
    job = small_job(tmp_path)
    assert cli.main(["send", str(job)]) == 0
    assert fakes.stream == job.read_bytes() + b"\x1bMO\r"


def test_a_printer_error_stops_the_send_with_a_clear_message(fakes, tmp_path, capsys):
    fakes._enquiry_replies = [b"\x1551"]
    job = small_job(tmp_path)
    assert cli.main(["send", str(job)]) == 1
    err = capsys.readouterr().err
    assert "COVER OPEN" in err and "after 22 bytes were sent" in err
    assert fakes.stream == job.read_bytes()           # the end-of-card commands were not sent


def test_dash_v_logs_the_commands_before_or_after_the_subcommand(fakes, tmp_path, capsys):
    job = small_job(tmp_path)
    for argv in (["send", str(job), "-v"], ["-v", "send", str(job)]):
        assert cli.main(argv) == 0
        err = capsys.readouterr().err
        assert "ESC MASTER" in err and "ESC JOB 0" in err and "ESC IV 30" in err
        assert "end of card" in err and "done: 22 bytes" in err
        assert "ESC E 2 ->" not in err                # that detail is for -vv


def test_dash_vv_also_logs_each_acknowledgement(fakes, tmp_path, capsys):
    job = small_job(tmp_path)
    assert cli.main(["send", str(job), "-vv"]) == 0
    err = capsys.readouterr().err
    assert "ESC E 2 -> <ACK>" in err and "chunk 1: 22 B written (22 of 22)" in err
    assert "end of block at byte 22: asking the printer" in err


def test_without_v_there_is_progress_but_no_protocol_log(fakes, tmp_path, capsys):
    job = small_job(tmp_path)
    assert cli.main(["send", str(job)]) == 0
    captured = capsys.readouterr()
    assert "100%" in captured.out and "ESC MASTER" not in captured.err


def test_log_file_gets_the_full_detail_even_without_v(fakes, tmp_path, capsys):
    job = small_job(tmp_path)
    log_file = tmp_path / "sending.log"
    assert cli.main(["send", str(job), "--log", str(log_file)]) == 0
    text = log_file.read_text()
    assert "check  result:           READY" in text                       # the pre-flight report
    assert "ESC MASTER" in text and "ESC E 2 -> <ACK>" in text and "done: 22 bytes" in text
    assert "ESC MASTER" not in capsys.readouterr().err                    # the console stays quiet


def test_log_file_records_a_failure(fakes, tmp_path, capsys):
    fakes._enquiry_replies = [b"\x1551"]
    job = small_job(tmp_path)
    log_file = tmp_path / "sending.log"
    assert cli.main(["send", str(job), "--log", str(log_file)]) == 1
    assert "error: the printer reports an error: COVER OPEN (after 22 bytes were sent)" in log_file.read_text()
