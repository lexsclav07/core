from pathlib import Path

from core.logo import PLACEHOLDER, load_logo


def test_bundled_logo_is_placeholder() -> None:
    assert load_logo() == ["[ CORE ]"]


def test_multiline_logo(tmp_path: Path) -> None:
    (tmp_path / "logo.txt").write_text(" /\\ \n/__\\\n")
    assert load_logo(tmp_path) == [" /\\ \n/__\\"]


def test_frames_play_in_name_order(tmp_path: Path) -> None:
    (tmp_path / "logo.txt").write_text("still")
    (tmp_path / "frame_02.txt").write_text("b")
    (tmp_path / "frame_01.txt").write_text("a")
    assert load_logo(tmp_path) == ["a", "b"]


def test_missing_or_empty_logo_falls_back(tmp_path: Path) -> None:
    assert load_logo(tmp_path) == [PLACEHOLDER]
    (tmp_path / "logo.txt").write_text("  \n")
    assert load_logo(tmp_path) == [PLACEHOLDER]
