"""The line above the input while CORE works, like Claude Code's:

    ✻ Tinkering… (12s · ↓ 340 tokens · esc to interrupt)

The star pulses, the seconds count up, and the token count goes up as the
reply streams in (Ollama sends about one token per piece). While a command
runs it says what's running instead of a verb.
"""

from __future__ import annotations

import random
import time

from textual.widgets import Static

from core.status import escape

FRAMES = "·✢✳✶✻✽✻✶✳✢"  # grows and shrinks
VERBS = ("Thinking", "Working", "Pondering", "Tinkering", "Crunching", "Computing", "Mulling")
TICK_S = 0.12


def spinner_line(frame: int, doing: str, seconds: float, tokens: int) -> str:
    """Textual markup for one frame of the line."""
    stats = [f"{seconds:.0f}s"]
    if tokens:
        stats.append(f"↓ {tokens} tokens")
    stats.append("esc to interrupt")
    glyph = FRAMES[frame % len(FRAMES)]
    return f"[$primary]{glyph} {escape(doing)}…[/] [$text-muted]({' · '.join(stats)})[/]"


class Spinner(Static):
    def __init__(self) -> None:
        super().__init__(id="spinner")
        self.verb = VERBS[0]
        self.doing = ""  # e.g. "Running uname -r"; empty for the verb
        self.tokens = 0
        self.started = time.monotonic()
        self.frame = 0
        self.display = False

    def on_mount(self) -> None:
        self.set_interval(TICK_S, self._tick)

    def start(self) -> None:
        """A new turn: a fresh verb, the clock and tokens from zero."""
        self.verb = random.choice(VERBS)
        self.doing, self.tokens, self.started = "", 0, time.monotonic()
        self.display = True
        self._tick()

    def stop(self) -> None:
        self.display = False

    def _tick(self) -> None:
        if self.display:
            self.frame += 1
            seconds = time.monotonic() - self.started
            self.update(spinner_line(self.frame, self.doing or self.verb, seconds, self.tokens))
