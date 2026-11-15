"""Load CORE's logo from text files in this folder.

logo.txt is the splash-screen logo and small.txt the one on the welcome
panel. Optional frame_01.txt, frame_02.txt, ... are splash animation frames,
played in name order. Swap these files to change the logo, no code needed.

They're made from the logo image (docs/logo.png) by from_image.py:
    uv run python -m core.logo.from_image docs/logo.png --width 40 > core/logo/logo.txt
    uv run python -m core.logo.from_image docs/logo.png --width 20 > core/logo/small.txt
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


def load_small_logo(directory: Path = LOGO_DIR) -> str:
    """The welcome panel's logo: small.txt, else the splash logo."""
    small = directory / "small.txt"
    if small.exists() and small.read_text().strip():
        return small.read_text().rstrip("\n")
    return load_logo(directory)[0]
