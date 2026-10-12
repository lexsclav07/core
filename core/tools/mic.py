"""Push-to-talk: record from the mic, transcribe with faster-whisper.

Only Lex can turn the mic on (a key in the TUI). There's deliberately no tool
for it, so the model can never start listening by itself.

Terminals don't report key *releases*, so "hold to talk" isn't possible;
it's press to start, press again (or Enter) to stop.

Recording uses pw-record (PipeWire, always on Fedora). Whisper runs on the
CPU with int8 weights so the GPU stays with the language model. The first
use downloads the whisper model (~460 MB for "small") from Hugging Face.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
import wave
from functools import cache
from pathlib import Path
from typing import Any

SAMPLE_RATE = 16_000  # what whisper expects


class MicError(Exception):
    pass


def microphones() -> list[str]:
    """Names of PipeWire audio sources (mics). Empty if none are connected."""
    try:
        out = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5).stdout
        nodes = json.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    names = []
    for node in nodes:
        props = (node.get("info") or {}).get("props") or {}
        # Virtual sources count too (e.g. EasyEffects noise cancelling).
        if str(props.get("media.class", "")).startswith("Audio/Source"):
            names.append(str(props.get("node.description") or props.get("node.name")))
    return names


class Recorder:
    """One recording. start() → stop() → transcribe()."""

    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="core-mic-"))
        self.path = self.dir / "speech.wav"
        self.proc: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        if not microphones():
            raise MicError("no microphone is connected")
        self.proc = await asyncio.create_subprocess_exec(
            "pw-record", "--rate", str(SAMPLE_RATE), "--channels", "1", "--format", "s16",
            str(self.path), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )  # fmt: skip

    async def stop(self) -> Path:
        if self.proc is None:
            raise MicError("not recording")
        self.proc.terminate()  # pw-record finishes the WAV header on SIGTERM
        await self.proc.wait()
        if not self.path.exists() or self.path.stat().st_size < 1000:
            raise MicError("nothing was recorded")
        return self.path

    def discard(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


@cache
def _model(size: str) -> Any:
    from faster_whisper import WhisperModel  # slow import; only when the mic is used

    return WhisperModel(size, device="cpu", compute_type="int8")


def load_wav(path: Path) -> Any:
    """Read our 16 kHz mono 16-bit WAV as the float array whisper wants.

    We decode it ourselves instead of letting faster-whisper use PyAV: its
    PyAV call breaks on PyAV 19 (an argument was removed), and our recordings
    are always the same simple format anyway.
    """
    import numpy as np

    with wave.open(str(path), "rb") as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (SAMPLE_RATE, 1, 2):
            raise MicError("unexpected audio format")
        frames = w.readframes(w.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


async def transcribe(path: Path, size: str = "small") -> str:
    """Speech to text. Runs in a thread so the TUI stays responsive."""

    def run() -> str:
        segments, _info = _model(size).transcribe(load_wav(path), vad_filter=True)
        return " ".join(s.text.strip() for s in segments).strip()

    return await asyncio.to_thread(run)
