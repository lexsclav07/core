"""SignReader (the camera-free part of HandTracker) and the model download."""

import hashlib
import json
from pathlib import Path

import pytest

from handsigns import model
from handsigns.model import ModelError, ensure_model
from handsigns.tracker import CameraError, SignReader, open_camera

REAL = json.loads((Path(__file__).parent / "data" / "hands.json").read_text())
PEACE = next(p["hand"] for p in REAL if p["sign"] == "peace")


def test_reader_fires_a_held_sign_once() -> None:
    reader = SignReader(hold=0.6)
    frames = [reader.read(PEACE, i / 10) for i in range(20)] + [reader.read(None, 2.0)]
    assert [f.t for f in frames if f.fired] == [0.6]
    assert {f.sign for f in frames[:-1]} == {"peace"} and frames[-1].sign is None
    assert frames[3].progress == pytest.approx(0.5) and frames[10].progress == 1.0


def test_reader_uses_your_own_shapes() -> None:
    reader = SignReader(shapes={"victory": "index middle"})
    assert reader.read(PEACE, 0).sign == "victory"


def test_bad_shapes_are_refused() -> None:
    with pytest.raises(ValueError, match="list the fingers"):
        SignReader(shapes={"oops": "index elbow"})


def test_model_is_downloaded_once_and_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(model, "MODEL_SHA256", hashlib.sha256(b"model").hexdigest())
    fetched: list[str] = []

    def fetch(url: str) -> bytes:
        fetched.append(url)
        return b"model"

    path = ensure_model(tmp_path / "m.task", fetch=fetch)
    assert path.read_bytes() == b"model" and fetched == [model.MODEL_URL]
    assert ensure_model(tmp_path / "m.task", fetch=fetch) == path
    assert len(fetched) == 1  # already there the second time


def test_a_wrong_model_is_never_saved(tmp_path: Path) -> None:
    with pytest.raises(ModelError, match="expected file"):
        ensure_model(tmp_path / "m.task", fetch=lambda url: b"something else")
    assert not (tmp_path / "m.task").exists()


def test_download_errors_are_explained(tmp_path: Path) -> None:
    def offline(url: str) -> bytes:
        raise OSError("network is unreachable")

    with pytest.raises(ModelError, match="couldn't download"):
        ensure_model(tmp_path / "m.task", fetch=offline)


def test_cache_dir_follows_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert model.default_path() == tmp_path / "handsigns" / "hand_landmarker.task"


def test_missing_video_file_is_a_clear_error() -> None:
    with pytest.raises(CameraError, match="no such video file"):
        open_camera(object(), "nope.mp4")
