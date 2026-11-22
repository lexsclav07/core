"""The welcome box (while the chat is empty) and the shortcuts screen (? or F1).

Tips are clickable: a click puts the text in the input box, so Lex can
tweak it before pressing Enter. Nothing is sent on its own. Recent chats are
clickable too: a click carries that chat on.
"""

from __future__ import annotations

from datetime import datetime
from importlib import metadata

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from core.history import ChatSummary
from core.panels import when
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


def version() -> str:
    try:
        return metadata.version("core-assistant")
    except metadata.PackageNotFoundError:
        return ""


def tips_text() -> str:
    """The suggestions: a click puts one in the input box (it isn't sent)."""
    lines = ["[b $primary]Tips for getting started[/]"]
    for i, s in enumerate(SUGGESTIONS):
        # The click passes an index, not the text: no quoting games in markup.
        lines.append(f"[@click=screen.suggest({i})][$text-muted]›[/] {escape(s)}[/]")
    return "\n".join(lines)


def recent_text(recent: list[ChatSummary], now: datetime | None = None) -> str:
    """The last few saved chats; a click carries one on."""
    lines = ["[b $primary]Recent chats[/]"]
    for chat in recent:
        title, ago = escape(chat.title), when(chat.updated, now)
        lines.append(f"[@click=screen.open_chat({chat.id})][$text-muted]›[/] {title}[/] "
                     f"[$text-muted]{ago}[/]")  # fmt: skip
    lines.append("[$text-muted]/resume for more[/]" if recent else "[$text-muted]None yet[/]")
    return "\n".join(lines)


class Welcome(Vertical):
    """The box at the top of a new chat, laid out like Claude Code's welcome:

        ╭─── CORE v0.1.0 ────────────────────────────────────────╮
        │      Good evening, Lex.     │ Tips for getting started │
        │                             │ › what GPU do I have…    │
        │        (small logo)         │ ───────────────────────  │
        │                             │ Recent chats             │
        │   qwen3:8b · local · ~      │ › install htop   21:04   │
        ╰────────────────────────────────────────────────────────╯

    Removed when the first message is sent; back after /clear. In a short
    window the logo goes, and in a narrow one the halves stack.
    """

    MIN_HEIGHT_FOR_LOGO = 32  # rows: logo + tips + the input and status line
    MIN_WIDTH_FOR_COLUMNS = 90

    def __init__(self, logo: str, about: str, recent: list[ChatSummary]) -> None:
        super().__init__(id="welcome")
        self.logo = logo
        self.about = about  # e.g. "qwen3:8b · local · ~"
        self.recent = recent

    def compose(self) -> ComposeResult:
        self.border_title = f"CORE v{version()}" if version() else "CORE"
        with Horizontal(id="welcome-cols"):
            with Vertical(id="welcome-left"):
                yield Static(f"[b]{greeting()}[/]", classes="welcome-centre")
                yield Static(self.logo, id="welcome-logo", markup=False)
                yield Static(f"[$text-muted]{escape(self.about)}[/]", classes="welcome-centre")
            with Vertical(id="welcome-right"):
                yield Static(tips_text(), id="welcome-tips")
                yield Static(recent_text(self.recent), id="welcome-recent")

    def on_mount(self) -> None:
        self.fit(self.app.size.width, self.app.size.height)

    def fit(self, width: int, height: int) -> None:
        self.query_one("#welcome-logo").display = height >= self.MIN_HEIGHT_FOR_LOGO
        side_by_side = width >= self.MIN_WIDTH_FOR_COLUMNS
        self.query_one("#welcome-cols").styles.layout = "horizontal" if side_by_side else "vertical"
        self.set_class(not side_by_side, "-narrow")


SIGN_LIST = "  ".join(f"{EMOJI.get(s, '')} {s}".strip() for s in (*SIGNS, *THUMB_SIGNS))
HELP = f"""\
[b $primary]Shortcuts[/]
  [b]enter[/]        send                     [b]↑ / ↓[/]      earlier messages
  [b]/[/]            commands                 [b]? or F1[/]    this screen
  [b]esc[/]          interrupt CORE, or go back to the chat
  [b]ctrl+c[/]       stop the running command, then the reply
  [b]ctrl+o[/]       expand the latest command's output (or click it)
  [b]ctrl+t[/]       talk: press to record, again (or enter) to stop
  [b]ctrl+g[/]       gesture mode on/off
  [b]ctrl+l[/]       new chat (/clear)        [b]ctrl+q[/]     quit
  [b]ctrl+p[/]       every command, and Textual's theme picker

[b $primary]Views[/]  (esc comes back to the chat)
  [b]/resume[/]  F3   saved chats: enter carries one on, delete removes it
  [b]/activity[/] F4  everything CORE ran, asked about or refused
  [b]/settings[/] F5  model, thinking, colours (saved to config.toml)

[b $primary]Approvals[/]
  Read-only commands run straight away. Anything that changes the PC shows
  what it does and how to undo it, and waits for [b]1[/]/[b]y[/] or [b]2[/]/[b]n[/]. Some things are
  never run, even if you ask (wiping the system, writing to live disks).

[b $primary]The line under the input[/]
  [b]ctx[/] is how full CORE's memory of this chat is. When it fills up, the oldest
  messages fall out: /clear starts afresh. A red [b]●[/] means a sense (screen,
  webcam, mic, gestures) is on right now.

[b $primary]Hand signs[/] (ctrl+g)
  {SIGN_LIST}
  Each sign runs a macro from [b]\\[gestures.macros][/] in ~/.config/core/config.toml.

[b $primary]Files[/]
  config     ~/.config/core/config.toml
  audit log  ~/.local/share/core/audit.log   (every command CORE ran or refused)
  chats      ~/.local/share/core/history.db  (/resume)

[$text-muted]esc to close[/]"""


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,f1,q,question_mark", "close", "Close")]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help"):
            yield Static(HELP)

    def action_close(self) -> None:
        self.dismiss()
