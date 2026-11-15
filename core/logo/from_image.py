"""Turn a logo image into block-character text for the splash screen.

    uv run python -m core.logo.from_image docs/logo.png --width 40 > core/logo/logo.txt

Each character holds 2×2 "dots" (the quadrant blocks ▘▝▖▗▀▄▌▐▚▞▙▛▜▟█), so
a 40-character-wide logo is 80 dots across. Terminals draw these glyphs
themselves, so they join up with no gaps between characters.

A terminal cell is about twice as tall as it is wide, so each dot is too:
the image is squashed to half height before it's cut into dots, or the
logo would come out stretched.

Logos made of separate pieces (like CORE's 2×2 letters) are split along
their empty bands and each piece is scaled on its own, keeping at least one
dot of gap. Otherwise a thin gap can vanish when the image is shrunk.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageOps

# Bits: 1 = top left, 2 = top right, 4 = bottom left, 8 = bottom right.
QUADRANTS = " ▘▝▀▖▌▞▛▗▚▐▜▄▙▟█"


def to_blocks(image: Image.Image, width: int, threshold: float = 0.5) -> str:
    """The logo as lines of quadrant blocks, about `width` characters wide."""
    ink = _ink(image)
    box = ink.getbbox()
    if box is None:
        raise ValueError("no logo found: the image is all one colour")
    ink = ink.crop(box)
    cols, rows = _bands(ink, axis=0), _bands(ink, axis=1)
    scale = 2 * width / ink.width  # dots per pixel across
    widths = [max(1, round((b - a) * scale)) for a, b in cols]
    heights = [max(1, round((b - a) * scale / 2)) for a, b in rows]  # tall dots
    gap_x = [max(1, round((c[0] - p[1]) * scale)) for p, c in zip(cols, cols[1:], strict=False)]
    gap_y = [max(1, round((c[0] - p[1]) * scale / 2)) for p, c in zip(rows, rows[1:], strict=False)]

    dots_w, dots_h = sum(widths) + sum(gap_x), sum(heights) + sum(gap_y)
    canvas = Image.new("L", (dots_w + dots_w % 2, dots_h + dots_h % 2), 0)
    y = 0
    for (top_px, bottom_px), h, gy in zip(rows, heights, [*gap_y, 0], strict=True):
        x = 0
        for (left_px, right_px), w, gx in zip(cols, widths, [*gap_x, 0], strict=True):
            piece = ink.crop((left_px, top_px, right_px, bottom_px)).resize(
                (w, h), Image.Resampling.BOX
            )
            canvas.paste(piece, (x, y))
            x += w + gx
        y += h + gy

    cut = int(threshold * 255)
    pixels = canvas.tobytes()
    on = [[pixels[y * canvas.width + x] > cut for x in range(canvas.width)]
          for y in range(canvas.height)]  # fmt: skip
    lines = []
    for y in range(0, canvas.height, 2):
        line = ""
        for x in range(0, canvas.width, 2):
            bits = on[y][x] | on[y][x + 1] << 1 | on[y + 1][x] << 2 | on[y + 1][x + 1] << 3
            line += QUADRANTS[bits]
        lines.append(line.rstrip())
    return "\n".join(lines)


def _ink(image: Image.Image) -> Image.Image:
    """Black and white, with the logo white, whichever way round the image was.

    Pure black and white (not grey) so faint noise in the background, common
    in saved logos, doesn't count as part of the logo.
    """
    grey = ImageOps.grayscale(image.convert("RGB"))
    corner = grey.getpixel((0, 0))
    if isinstance(corner, int) and corner > 127:  # light background
        grey = ImageOps.invert(grey)
    return grey.point(lambda v: 255 if v > 127 else 0)


def _bands(ink: Image.Image, axis: int) -> list[tuple[int, int]]:
    """Runs of columns (axis 0) or rows (axis 1) that have some ink in them."""
    # Shrinking to one row (or column) averages each column (or row).
    size = (ink.width, 1) if axis == 0 else (1, ink.height)
    profile = list(ink.resize(size, Image.Resampling.BOX).tobytes())  # one byte per pixel
    bands: list[tuple[int, int]] = []
    start = None
    for i, value in enumerate([*profile, 0]):
        if value > 8 and start is None:
            start = i
        elif value <= 8 and start is not None:
            bands.append((start, i))
            start = None
    return bands


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Logo image → block-character text.")
    parser.add_argument("image", type=Path)
    parser.add_argument("--width", type=int, default=40, help="characters across (default 40)")
    parser.add_argument("--threshold", type=float, default=0.5,
                        help="how much of a dot must be ink to draw it (0-1)")  # fmt: skip
    args = parser.parse_args(argv)
    with Image.open(args.image) as image:
        print(to_blocks(image, args.width, args.threshold))
    return 0


if __name__ == "__main__":
    sys.exit(main())
