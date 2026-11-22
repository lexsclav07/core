"""The quiet line under the input, like Claude Code's:

    ? for shortcuts                       ● qwen3:8b · 1.6k/8k ctx · 6.1/8.0G VRAM · local

Hints on the left (set by the chat screen: what keys do right now), and on
the right the model, how full its context window is, VRAM, and anything
that needs attention: a sense that's on (red), a model spilling onto the CPU
or a nearly full context (yellow, then red). It's built by render() from a
plain Snapshot, so it can be tested without a running app.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field

from core.tools.signs import label

SENSE_ICONS = {"screen": "💻", "webcam": "📷", "mic": "🎤", "gestures": "✋"}
WIDE = 100  # columns needed to show VRAM as well


@dataclass
class Snapshot:
    """Everything the bar shows. The app fills it in; render() draws it."""

    model: str = ""
    model_state: str = "unknown"  # gpu / split / idle / down / unknown
    gpu_percent: int = 100  # when split: how much of the model is on the GPU
    vram_mb: tuple[int, int] | None = None  # used, total
    context: tuple[int, int] = (0, 0)  # tokens used, window size
    senses: set[str] = field(default_factory=set)
    sign: str | None = None  # what gesture mode sees right now
    session: str = "local"


def escape(text: str) -> str:
    """Make text safe to put in Textual markup.

    Not textual.markup.escape: it leaves "[ CORE ]" alone (spaces inside, so
    not tag-like to it), but Textual's parser still reads that as a tag.
    """
    return text.replace("[", "\\[")


def render(s: Snapshot, width: int) -> str:
    """Textual markup for the right-hand side of the line."""
    parts = [
        _senses(s.senses, s.sign),
        _model(s),
        _context(*s.context),
        _vram(s.vram_mb) if width >= WIDE else "",
        "[$warning]remote[/]" if s.session == "remote" else "local",
    ]
    return " [$text-muted]·[/] ".join(p for p in parts if p)


def short(tokens: int) -> str:
    """1640 → '1.6k', 8192 → '8k'."""
    if tokens < 1000:
        return str(tokens)
    return f"{tokens / 1024:.1f}k".replace(".0k", "k")


def level(fraction: float) -> str:
    """No colour while there's room; yellow, then red, as it fills."""
    return "" if fraction < 0.75 else "$warning" if fraction < 0.9 else "$error"


def _paint(text: str, colour: str) -> str:
    return f"[{colour}]{text}[/]" if colour else text


def _model(s: Snapshot) -> str:
    name = escape(s.model)
    return {
        "gpu": f"[$success]●[/] {name}",
        # Part of the model spilled into system RAM: works, but much slower.
        "split": f"[$warning]● {name} {s.gpu_percent}% GPU[/]",
        "idle": f"○ {name}",
        "down": "[$error]● Ollama isn't running[/]",
    }.get(s.model_state, f"○ {name}")


def _context(used: int, total: int) -> str:
    if not total:
        return ""
    text = f"{short(used)}/{short(total)} ctx"
    # Past the window, Ollama drops the oldest messages: /clear starts afresh.
    return _paint(text + (" · /clear" if used >= total * 0.9 else ""), level(used / total))


def _vram(vram: tuple[int, int] | None) -> str:
    if vram is None:
        return ""
    used, total = vram
    return _paint(f"{used / 1024:.1f}/{total / 1024:.1f}G VRAM", level(used / total))


def _senses(senses: set[str], sign: str | None) -> str:
    if not senses:
        return ""
    on = " ".join(f"{SENSE_ICONS.get(n, '●')} {n}" for n in sorted(senses))
    seen = f" {escape(label(sign))}" if sign and "gestures" in senses else ""
    return f"[b $error]● {on}{seen}[/]"


async def vram_usage() -> tuple[int, int] | None:
    """GPU memory (used, total) in MB from nvidia-smi, or None if it can't tell."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi", "--query-gpu=memory.used,memory.total",
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )  # fmt: skip
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
        used, total = (int(x) for x in out.decode().splitlines()[0].split(","))
    except (OSError, ValueError, IndexError, TimeoutError):
        return None
    return used, total


def session_kind() -> str:
    # SSH sets SSH_CONNECTION. Phase 3 (tmux started at boot) will need to ask
    # tmux which client is attached instead, since the env is fixed at start.
    return "remote" if os.environ.get("SSH_CONNECTION") else "local"
