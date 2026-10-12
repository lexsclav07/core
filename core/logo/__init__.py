"""Load the splash logo from text files in this folder.

logo.txt is the still logo. Optional frame_01.txt, frame_02.txt, ... are
animation frames played in name order. Lex can swap these files for the real
logo later without touching any code.
"""

from __future__ import annotations

from pathlib import Path

LOGO_DIR = Path(__file__).parent
PLACEHOLDER = "[ CORE ]"


def load_logo(directory: Path = LOGO_DIR) -> list[str]:
    """Return the logo frames. One frame means a still logo.

    Falls back to the placeholder if logo.txt is missing, so a bad logo
    edit can never stop CORE from starting.
    """
    frames = [p.read_text().rstrip("\n") for p in sorted(directory.glob("frame_*.txt"))]
    if frames:
        return frames
    logo = directory / "logo.txt"
    if logo.exists() and logo.read_text().strip():
        return [logo.read_text().rstrip("\n")]
    return [PLACEHOLDER]
