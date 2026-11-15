from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from core.logo import PLACEHOLDER, load_logo, load_small_logo
from core.logo.from_image import QUADRANTS, to_blocks


def test_bundled_logos_fit_an_80x24_terminal() -> None:
    [splash] = load_logo()
    rows = splash.split("\n")
    assert len(rows) <= 16 and max(len(r) for r in rows) <= 40
    assert set(splash) <= set(QUADRANTS + "\n")  # only block characters
    assert len(load_small_logo().split("\n")) <= 8


def test_bundled_logo_matches_the_image() -> None:
    """logo.txt is what from_image makes of docs/logo.png (regenerate it if not)."""
    with Image.open(Path(__file__).parents[1] / "docs" / "logo.png") as image:
        assert to_blocks(image, 40) == load_logo()[0]


def two_squares(background: str, ink: str, gap: int) -> Image.Image:
    """Two 100x100 squares side by side with a thin gap between them."""
    image = Image.new("RGB", (300, 200), background)
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 50, 139, 149), fill=ink)
    draw.rectangle((140 + gap, 50, 239 + gap, 149), fill=ink)
    return image


@pytest.mark.parametrize(("background", "ink"), [("black", "white"), ("white", "black")])
def test_from_image_keeps_thin_gaps(background: str, ink: str) -> None:
    # A 3 px gap is far less than one dot at this size, but it must not vanish:
    # it's the empty left half of each ▐.
    text = to_blocks(two_squares(background, ink, gap=3), width=10)
    assert text == "█████▐████▌\n█████▐████▌\n▀▀▀▀▀▝▀▀▀▀▘"


def test_from_image_squashes_for_tall_cells() -> None:
    # A square is twice as many characters wide as tall, so it looks square.
    image = Image.new("L", (120, 120), 0)
    ImageDraw.Draw(image).rectangle((10, 10, 109, 109), fill=255)
    rows = to_blocks(image, width=20).split("\n")
    assert len(rows) == 10 and all(row == "█" * 20 for row in rows)


def test_from_image_needs_a_logo() -> None:
    with pytest.raises(ValueError, match="no logo found"):
        to_blocks(Image.new("L", (50, 50), 0), width=10)


def test_small_logo_falls_back_to_the_splash_logo(tmp_path: Path) -> None:
    (tmp_path / "logo.txt").write_text("big")
    assert load_small_logo(tmp_path) == "big"
    (tmp_path / "small.txt").write_text("small\n")
    assert load_small_logo(tmp_path) == "small"


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
