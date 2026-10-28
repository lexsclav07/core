"""Gesture mode and its macros, with the webcam and hand tracker faked.

The macro commands really run (harmless ones), so this also checks that they
start detached and that the audit log gets every run.
"""

import asyncio
import dataclasses
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from core.agent.risk import Assessment, Tier, classify, default_context
from core.config import GestureConfig, load_config
from core.tools import gestures, macros
from core.tools.macros import Macro
from core.tools.signs import all_signs

PEACE = next(
    p["hand"]
    for p in json.loads((Path(__file__).parent / "data" / "hands.json").read_text())
    if p["sign"] == "peace"
)


class FakeView:
    def __init__(self, ui: "FakeUI", sign: str, command: str, approved: bool) -> None:
        self.ui, self.entry = ui, {"sign": sign, "command": command, "approved": approved}

    def attach(self, running: Any) -> None:
        raise AssertionError("macros don't run in a PTY")

    def output(self, text: str) -> None:
        pass

    def finish(self, status: str, result: Any = None) -> None:
        self.ui.views.append({**self.entry, "status": status})


class FakeUI:
    def __init__(self, approve: bool = True) -> None:
        self.answer = approve
        self.asked: list[str] = []
        self.notes: list[str] = []
        self.views: list[dict[str, Any]] = []
        self.senses: list[tuple[str, bool]] = []
        self.signs: list[str | None] = []
        self.progress: list[float] = []

    async def approve(self, command: str, a: Assessment, explanation: str, undo: str) -> bool:
        self.asked.append(command)
        return self.answer

    async def macro_view(
        self, sign: str, command: str, assessment: Assessment, approved: bool
    ) -> FakeView:
        return FakeView(self, sign, command, approved)

    def gesture_note(self, text: str, warning: bool = False) -> None:
        self.notes.append(text)

    def sense(self, name: str, active: bool) -> None:
        self.senses.append((name, active))

    def show_sign(self, sign: str | None) -> None:
        self.signs.append(sign)

    def show_hand(self, hand: list[list[float]] | None, sign: str | None, progress: float) -> None:
        self.progress.append(progress)


@pytest.fixture(autouse=True)
def isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(gestures, "notify_desktop", lambda message: None)


def audit_log(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "data/core/audit.log"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


# Reading the config ---------------------------------------------------------


def test_default_config_has_the_peace_terminal_macro(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "config.toml").gestures
    assert cfg.macros == {"peace": "{terminal} --new-window"} and cfg.shapes == {}
    found, problems = macros.from_config(cfg.macros, all_signs(), "ptyxis")
    assert found == [Macro("peace", "ptyxis --new-window")] and problems == []


def test_bad_macros_are_reported() -> None:
    raw = {"peace": "ls", "wave": "ls", "fist": 3, "rock": "  "}
    found, problems = macros.from_config(raw, all_signs(), "ptyxis")
    assert [m.sign for m in found] == ["peace"]
    assert len(problems) == 3
    assert "wave: no such sign" in problems[0] and "thumbs_up" in problems[0]


# Arming: what's checked when gesture mode turns on ------------------------


async def test_arming_asks_once_per_confirm_tier_macro(tmp_path: Path) -> None:
    ui = FakeUI(approve=True)
    wanted = [
        Macro("peace", "ptyxis --new-window"),  # confirm: unknown command
        Macro("point", "ls"),  # safe
        Macro("fist", "rm -rf /"),  # blocked
        Macro("rock", "sudo dnf upgrade -y"),  # needs a terminal for the password
    ]
    armed = await macros.arm(ui, wanted, "local")
    assert ui.asked == ["ptyxis --new-window"]
    assert armed == {
        "peace": Macro("peace", "ptyxis --new-window", approved=True),
        "point": Macro("point", "ls"),
    }
    assert any("✊ fist macro refused" in n for n in ui.notes)
    assert any("sudo can't ask for your password" in n for n in ui.notes)
    approvals = {e["command"]: e["approval"] for e in audit_log(tmp_path)}
    assert approvals == {"rm -rf /": "blocked", "sudo dnf upgrade -y": "invalid"}


async def test_a_no_leaves_that_sign_off(tmp_path: Path) -> None:
    ui = FakeUI(approve=False)
    armed = await macros.arm(ui, [Macro("peace", "ptyxis --new-window")], "local")
    assert armed == {}
    assert audit_log(tmp_path)[0]["approval"] == "denied"


# Firing --------------------------------------------------------------------


async def test_fire_runs_detached_and_is_audited(tmp_path: Path) -> None:
    out = tmp_path / "sid"
    # Field 6 of /proc/<pid>/stat is the session ID.
    macro = Macro("point", f"cut -d' ' -f6 /proc/$$/stat > {out}")
    ui = FakeUI()
    await macros.fire(ui, macro, "local")
    assert ui.views == [{"sign": "☝ point", "command": macro.command, "approved": False,
                         "status": "exit 0"}]  # fmt: skip
    assert int(out.read_text()) != os.getsid(0)  # its own session: Ctrl+C can't reach it
    [entry] = audit_log(tmp_path)
    assert entry["approval"] == "auto" and entry["exit_code"] == 0
    assert entry["note"] == "gesture point"


async def test_fire_reports_a_failing_command(tmp_path: Path) -> None:
    ui = FakeUI()
    await macros.fire(ui, Macro("peace", "sh -c 'exit 3'", approved=True), "local")
    assert ui.views[0]["status"] == "exit 3" and ui.views[0]["approved"]
    assert audit_log(tmp_path)[0]["approval"] == "lex"


async def test_fire_says_when_a_command_does_not_exist() -> None:
    ui = FakeUI()
    await macros.fire(ui, Macro("peace", "ptyxsi --new-window", approved=True), "local")
    assert ui.views[0]["status"] == "command not found"


async def test_fire_leaves_an_app_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(macros, "LAUNCH_WAIT_S", 0.1)
    ui = FakeUI()
    await macros.fire(ui, Macro("rock", "sleep 0.5"), "local")
    assert ui.views[0]["status"] == "launched"
    assert "still running" in audit_log(tmp_path)[0]["note"]
    await asyncio.gather(*macros._children)  # reaped once it exits


async def test_fire_checks_the_command_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def never(command: str) -> int | None:
        raise AssertionError("must not run")

    monkeypatch.setattr(macros, "launch", never)
    ui = FakeUI()
    await macros.fire(ui, Macro("fist", "rm -rf /", approved=True), "local")
    # Was safe when gesture mode started, needs a y/n now: not run unasked.
    await macros.fire(ui, Macro("peace", "ptyxis --new-window"), "local")
    assert [v["status"] for v in ui.views] == ["blocked", "not approved"]
    assert [e["approval"] for e in audit_log(tmp_path)] == ["blocked", "denied"]


# Gesture mode ----------------------------------------------------------------


async def test_remote_sessions_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_spawn(*a: Any, **kw: Any) -> Any:
        raise AssertionError("must not start the tracker")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_spawn)
    ui = FakeUI()
    await gestures.run(ui, GestureConfig(), terminal="ptyxis", session="remote")
    assert "only works when you're at the PC" in ui.notes[0] and ui.senses == []


async def test_no_webcam(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gestures, "webcams", lambda: [])
    ui = FakeUI()
    await gestures.run(ui, GestureConfig(), terminal="ptyxis", session="local")
    assert "No webcam" in ui.notes[-1] and ui.asked == []


def model_server(body: bytes, calls: list[str]) -> httpx.AsyncClient:
    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


async def test_model_is_downloaded_once_and_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gestures, "MODEL_SHA256", hashlib.sha256(b"model").hexdigest())
    calls: list[str] = []
    ui = FakeUI()
    path = await gestures.ensure_model(ui, model_server(b"model", calls))
    assert path.read_bytes() == b"model" and calls == [gestures.MODEL_URL]
    assert await gestures.ensure_model(ui, model_server(b"model", calls)) == path
    assert len(calls) == 1  # the second time it was already there


async def test_a_wrong_model_is_never_saved() -> None:
    with pytest.raises(gestures.GestureError, match="expected file"):
        await gestures.ensure_model(FakeUI(), model_server(b"not the model", []))
    assert not gestures.model_path().exists()


def fake_tracker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lines: list[object]) -> None:
    """Stand in for hand_worker.py: print these lines, then wait to be stopped."""
    script = tmp_path / "lines.jsonl"
    script.write_text("".join(json.dumps(line) + "\n" for line in lines) + "not json\n")
    code = f"import sys, time; sys.stdout.write(open({str(script)!r}).read()); sys.stdout.flush(); time.sleep(30)"  # noqa: E501

    monkeypatch.setattr(gestures, "worker_command", lambda model, cfg: [sys.executable, "-c", code])


def peace_for(seconds: float, start: float = 0) -> list[object]:
    return [{"t": start + i / 10, "hand": PEACE} for i in range(int(seconds * 10))]


async def test_a_held_sign_fires_its_macro_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines = [{"ready": "/dev/video0"}, *peace_for(3), {"t": 3.0, "hand": None}]
    fake_tracker(tmp_path, monkeypatch, lines)
    fired: list[Macro] = []

    async def fire(ui: Any, macro: Macro, session: str) -> None:
        fired.append(macro)

    monkeypatch.setattr(gestures.macros, "fire", fire)
    ui = FakeUI()
    armed = {"peace": Macro("peace", "ptyxis --new-window", approved=True)}
    cfg = dataclasses.replace(GestureConfig(), auto_off=0)
    task = asyncio.create_task(gestures.watch(ui, cfg, Path("m"), {}, armed, "local"))
    async with asyncio.timeout(10):
        while ui.signs[-1:] != [None] or len(ui.signs) < 2:
            await asyncio.sleep(0.05)
    task.cancel()  # Ctrl+G again
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fired == [armed["peace"]]
    assert ui.progress[0] == 0 and ui.progress[6:30] == [1.0] * 24  # the hold bar
    assert ui.signs == ["peace", None, None]  # last one: cleared when it stopped
    assert ui.senses == [("gestures", True), ("gestures", False)]
    assert "watching /dev/video0" in ui.notes[0]


async def test_tracker_errors_are_shown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_tracker(tmp_path, monkeypatch, [{"error": "the webcam stopped sending pictures"}])
    ui = FakeUI()
    await gestures.watch(ui, GestureConfig(), Path("m"), {}, {}, "local")
    assert ui.notes == ["Gesture mode stopped: the webcam stopped sending pictures."]
    assert ui.senses[-1] == ("gestures", False)


async def test_auto_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = asyncio.get_running_loop().time()  # same clock as time.monotonic()
    fake_tracker(tmp_path, monkeypatch, [{"t": now + 61, "hand": None}])
    ui = FakeUI()
    cfg = dataclasses.replace(GestureConfig(), auto_off=1)
    await gestures.watch(ui, cfg, Path("m"), {}, {}, "local")
    assert "turned itself off after 1 minutes" in ui.notes[-1]


def test_assessment_tiers_used_here() -> None:
    """The tests above rely on these; fail loudly if risk_rules changes them."""
    ctx = default_context(str(Path.home()))
    tiers = {c: classify(c, ctx).tier for c in ["ls", "ptyxis --new-window", "rm -rf /"]}
    assert tiers == {"ls": Tier.SAFE, "ptyxis --new-window": Tier.CONFIRM, "rm -rf /": Tier.BLOCKED}
