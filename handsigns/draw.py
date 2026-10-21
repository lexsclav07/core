"""Draw a tracked hand in the terminal with braille dots.

Each character cell holds a 2×4 grid of braille dots, so a 26×10 cell box is
a 52×40 dot canvas: enough for the 21 hand points and the bones between
them. The drawing is mirrored like a mirror (raise your right hand and the
right side moves). With colour on, fingers that are up are green and the
rest are dim, using plain ANSI codes that Linux terminals and Windows
Terminal both understand.
"""

from __future__ import annotations

from collections.abc import Sequence

from handsigns.signs import FINGERS, Point, fingers_up, label

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
GREEN, YELLOW, DIM, BOLD, RESET = "\x1b[32m", "\x1b[33m", "\x1b[2m", "\x1b[1m", "\x1b[0m"


def draw(hand: Sequence[Point] | None, cols: int = 26, rows: int = 10, colour: bool = False) -> str:
    """The hand as `rows` lines of braille, `cols` wide. Blank if there's no hand."""
    if not hand or len(hand) != 21:
        return "\n".join(" " * cols for _ in range(rows))
    up = fingers_up(hand)
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
    bright: dict[tuple[int, int], bool] = {}
    for a, b, finger in BONES:  # palm first, so fingers are drawn on top
        for x, y in line(dots[a], dots[b]):
            cell = (x // 2, y // 4)
            bits[cell] = bits.get(cell, 0) | DOT_BITS[(x % 2, y % 4)]
            bright[cell] = finger is not None and up[finger]
    lines = []
    for row in range(rows):
        chars = []
        for col in range(cols):
            cell = (col, row)
            char = chr(0x2800 + bits[cell]) if cell in bits else " "
            if colour and cell in bits:
                char = f"{GREEN if bright[cell] else DIM}{char}{RESET}"
            chars.append(char)
        lines.append("".join(chars))
    return "\n".join(lines)


def fingers(hand: Sequence[Point], colour: bool = False) -> str:
    """'t I M r p': capitals (green with colour on) for the fingers that are up."""
    out = []
    for name, is_up in zip(FINGERS, fingers_up(hand), strict=True):
        letter = name[0].upper() if is_up else name[0]
        out.append(f"{(GREEN + BOLD) if is_up else DIM}{letter}{RESET}" if colour else letter)
    return " ".join(out)


def caption(sign: str | None, progress: float, colour: bool = False, cells: int = 8) -> str:
    """'✌ peace ━━━━────': the sign and a bar that fills while it's held."""
    if sign is None:
        return f"{DIM}no sign{RESET}" if colour else "no sign"
    filled = round(min(max(progress, 0.0), 1.0) * cells)
    done, left = "━" * filled, "─" * (cells - filled)
    if not colour:
        return f"{label(sign)} {done}{left}"
    tint = GREEN if progress >= 1 else YELLOW
    return f"{BOLD}{label(sign)}{RESET} {tint}{done}{RESET}{DIM}{left}{RESET}"


def line(start: tuple[int, int], end: tuple[int, int]) -> list[tuple[int, int]]:
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
