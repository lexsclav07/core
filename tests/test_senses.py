"""Screen, webcam and mic, with the hardware and vision model faked out."""

import base64
import io
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from core.agent.risk import Assessment
from core.tools import mic, senses, vision
from core.tools.registry import ToolContext


class FakeUI:
    def __init__(self, approve: bool) -> None:
        self.answer = approve
        self.asked: list[str] = []
        self.senses: list[tuple[str, bool]] = []

    async def approve(self, command: str, a: Assessment, explanation: str, undo: str) -> bool:
        self.asked.append(explanation)
        return self.answer

    async def ask_password(self) -> str | None:
        return None

    async def command_view(self, command: str, assessment: Assessment) -> Any:
        raise AssertionError("senses don't create command blocks")

    def sense(self, name: str, active: bool) -> None:
        self.senses.append((name, active))


@pytest.fixture(autouse=True)
def isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(senses, "KEEP_DIR", tmp_path / "kept")
    monkeypatch.setattr(senses, "_notify", lambda message: None)


def fake_screenshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    shot = tmp_path / "Screenshot.png"
    Image.new("RGBA", (64, 32)).save(shot)

    async def take() -> Path:
        return shot

    async def describe(path: Path, question: str, **kw: Any) -> str:
        assert path.exists()
        return "a terminal running htop"

    monkeypatch.setattr(senses, "take_screenshot", take)
    monkeypatch.setattr(senses, "describe", describe)
    return shot


def ctx(ui: FakeUI, session: str = "local") -> ToolContext:
    return ToolContext(ui, ".", session)


async def test_screenshot_needs_a_yes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def never() -> Path:
        raise AssertionError("must not capture without approval")

    monkeypatch.setattr(senses, "take_screenshot", never)
    ui = FakeUI(approve=False)
    result = await senses.look_at_screen.call(ctx(ui), {"question": "what's open?"})
    assert "said no" in result.text and ui.senses == []
    log = json.loads((tmp_path / "data/core/audit.log").read_text())
    assert log["approval"] == "denied"


async def test_screenshot_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shot = fake_screenshot(tmp_path, monkeypatch)
    ui = FakeUI(approve=True)
    result = await senses.look_at_screen.call(ctx(ui), {"question": "what's open?"})
    assert "htop" in result.text
    assert result.untrusted  # rule 6: what's on screen is outside content
    assert ui.senses == [("screen", True), ("screen", False)]
    assert not shot.exists()  # moved out of ~/Pictures, then deleted
    assert not (tmp_path / "kept").exists()


async def test_keep_saves_the_screenshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_screenshot(tmp_path, monkeypatch)
    args = {"question": "x", "keep": True}
    result = await senses.look_at_screen.call(ctx(FakeUI(approve=True)), args)
    kept = list((tmp_path / "kept").glob("screen-*.png"))
    assert len(kept) == 1 and str(kept[0]) in result.text


async def test_remote_session_says_its_the_pc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_screenshot(tmp_path, monkeypatch)
    ui = FakeUI(approve=True)
    await senses.look_at_screen.call(ctx(ui, session="remote"), {"question": "x"})
    assert "PC's, not your laptop's" in ui.asked[0]


async def test_no_webcam_means_no_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(senses, "webcams", lambda: [])
    ui = FakeUI(approve=True)
    result = await senses.look_at_webcam.call(ctx(ui), {"question": "x"})
    assert "No webcam" in result.text and ui.asked == []


def test_images_are_scaled_for_the_gpu(tmp_path: Path) -> None:
    big = tmp_path / "dual-monitor.png"
    Image.new("RGBA", (4480, 1440)).save(big)
    with Image.open(io.BytesIO(base64.b64decode(vision.prepare(big)))) as img:
        w, h = img.size
        assert img.format == "JPEG" and img.mode == "RGB"
    assert w * h <= vision.MAX_PIXELS and abs(w / h - 4480 / 1440) < 0.01


PW_DUMP = [
    {"info": {"props": {"media.class": "Audio/Sink", "node.description": "Speakers"}}},
    {"info": {"props": {"media.class": "Audio/Source", "node.description": "USB Mic"}}},
    {"type": "PipeWire:Interface:Client", "info": {}},
]


def test_microphones_are_found_in_pw_dump(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(*a: Any, **kw: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(a, 0, stdout=json.dumps(PW_DUMP))

    monkeypatch.setattr(subprocess, "run", run)
    assert mic.microphones() == ["USB Mic"]


async def test_recording_without_a_mic_fails_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mic, "microphones", lambda: [])
    recorder = mic.Recorder()
    with pytest.raises(mic.MicError, match="no microphone"):
        await recorder.start()
    recorder.discard()
