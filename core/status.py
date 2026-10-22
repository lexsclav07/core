"""The status bar along the top: model · VRAM · context · senses · session · clock.

    ◆ CORE  ● qwen3:8b  VRAM ━━━━━━──── 6.1/8.0G  ctx ━━────── 1.6k/8k  senses off  ⌂ local  21:04

The text is built by render() from a plain Snapshot, so it can be tested
without a running app. On a narrow terminal (an SSH window on the laptop,
say) the meters are dropped and only the numbers stay.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime

from core.tools.signs import label

SENSE_ICONS = {"screen": "💻", "webcam": "📷", "mic": "🎤", "gestures": "✋"}
WIDE = 110  # columns needed for the meters


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


def render(s: Snapshot, width: int, now: datetime | None = None) -> str:
    """Textual markup for the whole bar."""
    wide = width >= WIDE
    parts = [
        "[b reverse $primary] ◆ CORE [/]",
        _model(s),
        _vram(s.vram_mb, wide),
        _context(*s.context, wide),
        _senses(s.senses, s.sign),
        "[$warning]⇄ remote[/]" if s.session == "remote" else "[$text-muted]⌂ local[/]",
        f"[$text-muted]{(now or datetime.now()):%H:%M}[/]",
    ]
    return "  ".join(p for p in parts if p)


def meter(fraction: float, cells: int) -> str:
    """A bar like ━━━━━─────, coloured by how full it is."""
    fraction = min(max(fraction, 0.0), 1.0)
    filled = round(fraction * cells)
    colour = "$success" if fraction < 0.75 else "$warning" if fraction < 0.9 else "$error"
    return f"[{colour}]{'━' * filled}[/][$text-muted]{'─' * (cells - filled)}[/]"


def short(tokens: int) -> str:
    """1640 → '1.6k', 8192 → '8k'."""
    if tokens < 1000:
        return str(tokens)
    return f"{tokens / 1024:.1f}k".replace(".0k", "k")


def _model(s: Snapshot) -> str:
    name = escape(s.model)
    return {
        "gpu": f"[$success]●[/] {name}",
        "split": f"[$warning]●[/] {name} [$warning]{s.gpu_percent}% GPU[/]",
        "idle": f"[$text-muted]○ {name} idle[/]",
        "down": "[$error]● Ollama down[/]",
    }.get(s.model_state, f"[$text-muted]○ {name}[/]")


def _vram(vram: tuple[int, int] | None, wide: bool) -> str:
    if vram is None:
        return "[$text-muted]VRAM ?[/]"
    used, total = vram
    numbers = f"{used / 1024:.1f}/{total / 1024:.1f}G"
    return f"VRAM {meter(used / total, 10)} {numbers}" if wide else f"VRAM {numbers}"


def _context(used: int, total: int, wide: bool) -> str:
    if not total:
        return ""
    numbers = f"{short(used)}/{short(total)}"
    # Past the window, Ollama drops the oldest messages: Ctrl+L starts afresh.
    hint = " [$error]Ctrl+L[/]" if used >= total * 0.9 else ""
    return f"ctx {meter(used / total, 8)} {numbers}{hint}" if wide else f"ctx {numbers}{hint}"


def _senses(senses: set[str], sign: str | None) -> str:
    if not senses:
        return "[$text-muted]senses off[/]"
    on = " ".join(f"{SENSE_ICONS.get(n, '●')} {n}" for n in sorted(senses))
    seen = f" [b]{escape(label(sign))}[/]" if sign and "gestures" in senses else ""
    return f"[b $error]● {on}[/]{seen}"


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
