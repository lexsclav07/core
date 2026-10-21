"""The welcome panel (shown while the chat is empty) and the help screen (F1).

Suggestions are clickable: a click puts the text in the input box, so Lex
can tweak it before pressing Enter. Nothing is sent on its own.
"""

from __future__ import annotations

from datetime import datetime

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from core.status import escape
from core.tools.signs import EMOJI, SIGNS, THUMB_SIGNS

SUGGESTIONS = [
    "what GPU do I have, and is the driver working?",
    "how much free disk space do I have?",
    "turn on dark mode",
    "look at my screen and tell me what you see",
    "install prism launcher",
]


def greeting(now: datetime | None = None) -> str:
    hour = (now or datetime.now()).hour
    if hour < 5:
        return "Up late, Lex?"
    part = "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"
    return f"Good {part}, Lex."


def welcome_text(logo: str, now: datetime | None = None) -> str:
    lines = [
        f"[b $primary]{escape(logo)}[/]",
        "",
        f"[b]{greeting(now)}[/] I run on this PC's GPU and can work the machine for you.",
        "Anything that changes the system asks you first.",
        "",
        "[$text-muted]Try:[/]",
    ]
    for i, s in enumerate(SUGGESTIONS):
        # The click passes an index, not the text: no quoting games in markup.
        lines.append(f"  [@click=screen.suggest({i})][$accent]›[/] {escape(s)}[/]")
    lines += [
        "",
        "[$text-muted]Ctrl+T talk · Ctrl+G hand signs · F1 help · Ctrl+P all commands[/]",
    ]
    return "\n".join(lines)


class Welcome(Static):
    """Removed when the first message is sent; back after Ctrl+L."""

    def __init__(self, logo: str) -> None:
        super().__init__(welcome_text(logo), id="welcome")


SIGN_LIST = "  ".join(f"{EMOJI.get(s, '')} {s}".strip() for s in (*SIGNS, *THUMB_SIGNS))
HELP = f"""\
[b $primary]Keys[/]
  [b]Enter[/]      send          [b]↑ / ↓[/]    earlier messages
  [b]Ctrl+C[/]     stop the running command, then the reply   [b]Esc[/] same
  [b]Ctrl+O[/]     expand the latest command's output (or click it)
  [b]Ctrl+T[/]     talk: press to record, again (or Enter) to stop
  [b]Ctrl+G[/]     gesture mode on/off
  [b]Ctrl+L[/]     new chat (CORE forgets this one)
  [b]Ctrl+P[/]     all commands      [b]Ctrl+Q[/]  quit

[b $primary]Approvals[/]
  Read-only commands run straight away. Anything that changes the PC shows
  what it does and how to undo it, and waits for [b]y[/] or [b]n[/]. Some things
  are never run, even if you ask (wiping the system, writing to live disks).

[b $primary]Status bar[/]
  [b]ctx[/] is how full CORE's memory of this chat is. When it fills up, the
  oldest messages fall out; press Ctrl+L to start fresh.
  Red [b]●[/] means a sense (screen, webcam, mic, gestures) is on right now.

[b $primary]Hand signs[/] (Ctrl+G)
  {SIGN_LIST}
  Each sign runs a macro from [b]\\[gestures.macros][/] in ~/.config/core/config.toml.

[b $primary]Files[/]
  config     ~/.config/core/config.toml
  audit log  ~/.local/share/core/audit.log   (every command CORE ran or refused)

[$text-muted]Esc or F1 to close[/]"""


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,f1,q", "close", "Close")]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help"):
            yield Static(HELP)

    def action_close(self) -> None:
        self.dismiss()
