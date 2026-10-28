"""A live drawing of the tracked hand for gesture mode, in braille dots.

Each terminal cell holds a 2×4 grid of braille dots, so a 26×10 cell box is
a 52×40 dot canvas: enough for the 21 hand points and the bones between
them. The drawing is mirrored like a mirror (raise your right hand and the
right side moves), fingers that are up are bright, and the bar underneath
fills while you hold a sign and turns green when it fires. It's there for
tuning: if a sign won't fire, you can see which finger CORE reads wrong.
"""

from __future__ import annotations

from collections.abc import Sequence

from textual.widgets import Static

from core.tools.signs import FINGERS, Point, fingers_up, label

# MediaPipe's bones: (from point, to point, finger index or None for the palm).
BONES = [
    (0, 5, None), (5, 9, None), (9, 13, None), (13, 17, None), (0, 17, None),
    (0, 1, 0), (1, 2, 0), (2, 3, 0), (3, 4, 0),
    (5, 6, 1), (6, 7, 1), (7, 8, 1),
    (9, 10, 2), (10, 11, 2), (11, 12, 2),
    (13, 14, 3), (14, 15, 3), (15, 16, 3),
    (17, 18, 4), (18, 19, 4), (19, 20, 4),
]  # fmt: skip
# Bit for each dot in a braille cell, by (x, y) inside the 2×4 cell.
DOT_BITS = {(0, 0): 0x01, (0, 1): 0x02, (0, 2): 0x04, (0, 3): 0x40,
            (1, 0): 0x08, (1, 1): 0x10, (1, 2): 0x20, (1, 3): 0x80}  # fmt: skip
UP, DOWN = "$success", "$text-muted"
COLS, ROWS = 26, 10


def render(
    hand: Sequence[Point] | None, sign: str | None, progress: float, cols: int = COLS,
    rows: int = ROWS,
) -> str:  # fmt: skip
    """Textual markup: the drawing, then the sign and its hold bar."""
    if not hand or len(hand) != 21:
        blank = "\n".join(" " * cols for _ in range(rows))
        return f"{blank}\n[$text-muted]{'show me your hand'.center(cols)}[/]\n "
    up = fingers_up(hand)
    picture = _draw(hand, up, cols, rows)
    letters = " ".join(
        f"[b {UP}]{f[0].upper()}[/]" if is_up else f"[{DOWN}]{f[0]}[/]"
        for f, is_up in zip(FINGERS, up, strict=True)
    )
    if sign is None:
        caption = "[$text-muted]no sign[/]"
    else:
        cells = 8
        filled = round(progress * cells)
        colour = "$success" if progress >= 1 else "$warning"
        bar = f"[{colour}]{'━' * filled}[/][$text-muted]{'─' * (cells - filled)}[/]"
        caption = f"[b]{label(sign)}[/] {bar}"
    return f"{picture}\n{caption}\n{letters}"


def _draw(hand: Sequence[Point], up: tuple[bool, ...], cols: int, rows: int) -> str:
    width, height = cols * 2, rows * 4  # in dots
    xs = [-p[0] for p in hand]  # mirrored
    ys = [p[1] for p in hand]
    span = max(max(xs) - min(xs), max(ys) - min(ys), 1e-6)
    scale = (min(width, height) - 1) / span
    # Centre the hand in the canvas.
    off_x = (width - 1 - (max(xs) - min(xs)) * scale) / 2 - min(xs) * scale
    off_y = (height - 1 - (max(ys) - min(ys)) * scale) / 2 - min(ys) * scale
    dots = [
        (round(x * scale + off_x), round(y * scale + off_y)) for x, y in zip(xs, ys, strict=True)
    ]

    bits: dict[tuple[int, int], int] = {}
    colour: dict[tuple[int, int], str] = {}
    for a, b, finger in BONES:  # palm first, so fingers are drawn on top
        style = DOWN if finger is None or not up[finger] else UP
        for x, y in _line(dots[a], dots[b]):
            cell = (x // 2, y // 4)
            bits[cell] = bits.get(cell, 0) | DOT_BITS[(x % 2, y % 4)]
            colour[cell] = style
    lines = []
    for row in range(rows):
        line = []
        for col in range(cols):
            cell = (col, row)
            if cell in bits:
                line.append(f"[{colour[cell]}]{chr(0x2800 + bits[cell])}[/]")
            else:
                line.append(" ")
        lines.append("".join(line))
    return "\n".join(lines)


def _line(start: tuple[int, int], end: tuple[int, int]) -> list[tuple[int, int]]:
    """Every dot on the straight line between two dots (Bresenham's algorithm)."""
    (x0, y0), (x1, y1) = start, end
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
    err = dx + dy
    points = []
    while True:
        points.append((x0, y0))
        if (x0, y0) == (x1, y1):
            return points
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


class HandView(Static):
    """Docked at the right while gesture mode is on."""

    def on_mount(self) -> None:
        self.border_title = "hand signs"
        self.update(render(None, None, 0))

    def show(self, hand: Sequence[Point] | None, sign: str | None, progress: float) -> None:
        self.update(render(hand, sign, progress))
