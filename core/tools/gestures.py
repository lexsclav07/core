"""Gesture mode: hand signs in front of the webcam run Lex's macros.

Ctrl+G turns it on and off; the model can't. While it's on:

    hand_worker.py (its own process): webcam frame → 21 hand points
      → signs.classify → "peace" → Debouncer: held long enough?
      → macros.fire → `ptyxis --new-window`

Same rules as the other senses (CLAUDE.md): off until Lex asks, a red
indicator in the status bar plus a GNOME notification while it's on, and
frames never leave the PC (or even the worker process). Local sessions
only: over SSH, whoever is sitting at the PC would be the one making signs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Protocol

import httpx

from core.agent import audit
from core.config import GestureConfig
from core.tools import macros
from core.tools.senses import notify_desktop, webcams
from core.tools.signs import Debouncer, all_signs, classify, parse_shapes

# MediaPipe's hand landmark model (Apache 2.0). Pinned to version 1 and
# checked, so a changed or corrupted download is never loaded.
MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/"
    "hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task"
)
MODEL_SHA256 = "fbc2a30080c3c557093b5ddfc334698132eb341044ccee322ccf8bcf3607cde1"
STOP_GRACE_S = 2.0
PROJECT_ROOT = Path(__file__).resolve().parents[2]  # so `-m core.tools...` always resolves


class GestureUI(macros.MacroUI, Protocol):
    def sense(self, name: str, active: bool) -> None: ...
    def show_sign(self, sign: str | None) -> None:
        """Show the sign CORE sees right now (None = no sign) in the status bar."""

    def show_hand(self, hand: list[list[float]] | None, sign: str | None, progress: float) -> None:
        """Every frame: the hand points, its sign, and how close that is to firing."""


class GestureError(Exception):
    pass


async def run(ui: GestureUI, cfg: GestureConfig, *, terminal: str, session: str) -> None:
    """Gesture mode, from Ctrl+G until it's cancelled (Ctrl+G again) or stops."""
    if session == "remote":
        ui.gesture_note(
            "Gesture mode only works when you're at the PC. Over SSH, whoever is in "
            "front of the PC's webcam would be the one making the signs.",
            warning=True,
        )
        return
    shapes, problems = parse_shapes(cfg.shapes)
    found, more = macros.from_config(cfg.macros, all_signs(shapes), terminal)
    for problem in problems + more:
        ui.gesture_note(problem, warning=True)
    if not cfg.camera and not webcams():
        ui.gesture_note("No webcam is connected to the PC (no /dev/video* device).", warning=True)
        return
    try:
        model = await ensure_model(ui)
    except GestureError as e:
        ui.gesture_note(f"Can't start gesture mode: {e}.", warning=True)
        return
    armed = await macros.arm(ui, found, session)
    if not armed:
        ui.gesture_note("No macros are on, so signs only show in the status bar.")
    entry = audit.AuditEntry("✋ gesture mode (webcam)", "CONFIRM", "Lex pressed Ctrl+G",
                             "lex", session)  # fmt: skip
    audit.record(entry)
    await watch(ui, cfg, model, shapes, armed, session)


async def watch(
    ui: GestureUI,
    cfg: GestureConfig,
    model: Path,
    shapes: dict[str, str],
    armed: dict[str, macros.Macro],
    session: str,
) -> None:
    """Run the hand tracker and fire macros until it's cancelled or stops."""
    proc = await asyncio.create_subprocess_exec(
        *worker_command(model, cfg), cwd=PROJECT_ROOT,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )  # fmt: skip
    assert proc.stdout is not None
    ui.sense("gestures", True)
    notify_desktop("CORE is watching the webcam for hand signs")
    debouncer = Debouncer(cfg.hold, cfg.cooldown)
    shown: str | None = None
    off_at = time.monotonic() + cfg.auto_off * 60 if cfg.auto_off > 0 else math.inf
    try:
        async for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue  # not one of ours
            if "error" in msg:
                ui.gesture_note(f"Gesture mode stopped: {msg['error']}.", warning=True)
                return
            if "ready" in msg:
                ui.gesture_note(f"Gesture mode on, watching {msg['ready']}. Ctrl+G turns it off.")
                continue
            now = float(msg["t"])
            sign = classify(msg["hand"], shapes) if msg.get("hand") else None
            if sign != shown:
                shown = sign
                ui.show_sign(sign)
            fired = debouncer.update(sign, now)
            ui.show_hand(msg.get("hand"), sign, debouncer.progress(sign, now))
            if fired is not None and fired in armed:
                await macros.fire(ui, armed[fired], session)
            if now >= off_at:
                minutes = f"{cfg.auto_off:g} minutes (auto_off in config.toml)"
                ui.gesture_note(f"Gesture mode turned itself off after {minutes}.")
                return
        ui.gesture_note("Gesture mode stopped: the hand tracker quit unexpectedly.", warning=True)
    finally:
        await _stop(proc)
        ui.show_sign(None)
        ui.sense("gestures", False)


def worker_command(model: Path, cfg: GestureConfig) -> list[str]:
    return [
        sys.executable, "-m", "core.tools.hand_worker",
        "--model", str(model), "--camera", cfg.camera, "--fps", str(cfg.fps),
    ]  # fmt: skip


async def _stop(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    proc.terminate()  # the camera is released when the process exits
    try:
        await asyncio.wait_for(proc.wait(), STOP_GRACE_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()


def model_path() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "core" / "models" / "hand_landmarker.task"


async def ensure_model(ui: GestureUI, client: httpx.AsyncClient | None = None) -> Path:
    """The hand model: downloaded the first time (7.8 MB from Google), then checked."""
    path = model_path()
    if path.exists() and _sha256(path.read_bytes()) == MODEL_SHA256:  # ~10 ms
        return path
    ui.gesture_note("Downloading the hand-tracking model (7.8 MB, only this once)…")
    try:
        async with client or httpx.AsyncClient(timeout=60, follow_redirects=True) as http:
            response = await http.get(MODEL_URL)
            response.raise_for_status()
    except httpx.HTTPError as e:
        raise GestureError(f"couldn't download the hand-tracking model ({e})") from e
    if _sha256(response.content) != MODEL_SHA256:
        raise GestureError("the downloaded hand-tracking model wasn't the expected file")
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".part")
    partial.write_bytes(response.content)
    partial.replace(path)  # atomic: never a half-written model at `path`
    return path


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
