import pytest

from j1x0_cardprinter.library import ENV_VAR, default_dir, discover, humanise


@pytest.mark.parametrize("raw, label", [
    ("firstName", "First name"),
    ("cardNo", "Card no"),
    ("staff_badge", "Staff badge"),
    ("ID", "ID"),
    ("photoURL", "Photo URL"),
    ("visitor-pass 2024", "Visitor pass 2024"),
    ("a", "A"),
])
def test_humanise(raw, label):
    assert humanise(raw) == label


def test_discover_lists_top_level_svgs_sorted_by_title(tmp_path):
    (tmp_path / "b_badge.svg").write_text("<svg/>")
    (tmp_path / "Alpha.svg").write_text("<svg/>")
    (tmp_path / "notes.txt").write_text("not a design")
    (tmp_path / ".hidden.svg").write_text("<svg/>")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.svg").write_text("<svg/>")
    designs = discover(tmp_path)
    assert [(d.name, d.title) for d in designs] == [("Alpha", "Alpha"), ("b_badge", "B badge")]
    assert designs[0].path == tmp_path / "Alpha.svg"


def test_missing_folder_means_no_designs(tmp_path):
    assert discover(tmp_path / "nope") == []


def test_design_loads_its_template(tmp_path):
    (tmp_path / "card.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><text>$name$</text></svg>')
    assert discover(tmp_path)[0].load().fields() == ["name"]


def test_default_dir_honours_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv(ENV_VAR, str(tmp_path / "designs"))
    assert default_dir() == tmp_path / "designs"
    monkeypatch.delenv(ENV_VAR)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert default_dir() == tmp_path / ".j1x0"
