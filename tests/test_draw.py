"""The braille hand drawing and the command line."""

import json
from pathlib import Path

import pytest

from handsigns.cli import main
from handsigns.draw import caption, draw, fingers, line

REAL = json.loads((Path(__file__).parent / "data" / "hands.json").read_text())


@pytest.mark.parametrize("photo", REAL, ids=[p["photo"] for p in REAL])
def test_every_hand_draws_in_braille(photo: dict[str, list[list[float]]]) -> None:
    rows = draw(photo["hand"]).split("\n")
    assert len(rows) == 10 and all(len(r) == 26 for r in rows)
    assert sum("⠀" < ch <= "⣿" for r in rows for ch in r) > 20


def test_no_hand_is_blank() -> None:
    assert draw(None).strip() == ""


def test_colour_marks_fingers_that_are_up() -> None:
    peace = next(p["hand"] for p in REAL if p["sign"] == "peace")
    assert fingers(peace) == "t I M r p"
    assert "\x1b[32m" in draw(peace, colour=True)  # green for the raised fingers
    assert "\x1b" not in draw(peace)


def test_caption_bar() -> None:
    assert caption("peace", 0.5) == "✌ peace ━━━━────"
    assert caption("peace", 1.0) == "✌ peace ━━━━━━━━"
    assert caption(None, 0) == "no sign"


def test_line_covers_both_ends() -> None:
    assert line((0, 0), (3, 1))[0] == (0, 0) and line((0, 0), (3, 1))[-1] == (3, 1)
    assert line((2, 2), (2, 2)) == [(2, 2)]


def test_cli_lists_signs(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["signs"]) == 0
    out = capsys.readouterr().out
    assert "peace" in out and "thumbs_down" in out


def test_cli_reports_a_missing_video(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["watch", "--camera", "missing.mp4"]) == 1
    assert "no such video file" in capsys.readouterr().err


def test_cli_reports_a_bad_shape(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["watch", "--shape", "oops=elbow", "--camera", "x.mp4"]) == 1
    assert "list the fingers" in capsys.readouterr().err
