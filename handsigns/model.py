"""MediaPipe's hand landmark model: downloaded once, then checked every time.

The model (7.8 MB, Apache 2.0) is pinned to version 1 and its SHA-256, so a
changed or corrupted download is never loaded. It's cached in
~/.cache/handsigns/ (or $XDG_CACHE_HOME/handsigns/; on Windows, %LOCALAPPDATA%).
"""

from __future__ import annotations

import hashlib
import os
import urllib.request
from collections.abc import Callable
from pathlib import Path

MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/"
    "hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task"
)
MODEL_SHA256 = "fbc2a30080c3c557093b5ddfc334698132eb341044ccee322ccf8bcf3607cde1"


class ModelError(Exception):
    pass


def default_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "handsigns" / "hand_landmarker.task"


def _download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - fixed https URL
        data: bytes = response.read()
        return data


def ensure_model(
    path: Path | None = None,
    fetch: Callable[[str], bytes] = _download,
    on_download: Callable[[], None] = lambda: None,
) -> Path:
    """Path to a checked copy of the model, downloading it the first time.

    `on_download` is called just before downloading, e.g. to print a message.
    """
    path = path or default_path()
    if path.exists() and _sha256(path.read_bytes()) == MODEL_SHA256:  # ~10 ms
        return path
    on_download()
    try:
        data = fetch(MODEL_URL)
    except OSError as e:  # URLError is an OSError
        raise ModelError(f"couldn't download the hand model: {e}") from e
    if _sha256(data) != MODEL_SHA256:
        raise ModelError("the downloaded hand model wasn't the expected file, so it wasn't used")
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".part")
    partial.write_bytes(data)
    partial.replace(path)  # atomic: never a half-written model at `path`
    return path


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
