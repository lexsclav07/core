"""TUI pieces for running commands: the y/n box, password field, command block.

They look like Claude Code's: a command is a tool call (`⏺ Bash(…)` with its
output under `⎿`), and questions are a box at the bottom with numbered
choices.

All text that comes from the model or from commands is shown with
markup=False / rich Text, so a `[red]` or `[ -f x ]` in it is shown as-is
instead of being read as Textual styling.
"""

from __future__ import annotations

import time

from rich.syntax import Syntax
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from core.agent.risk import Assessment, Tier
from core.tools.pty_runner import CommandResult, RunningCommand, clean_output

PREVIEW_LINES = 8  # output lines shown while a block is collapsed
MAX_LINES = 2000  # lines shown when expanded
# "Yes" only counts this long after a question appears, so a key Lex was
# already typing (Enter, y) can't approve something by accident.
ARM_S = 0.5


def highlight(command: str) -> Text:
    """The command in bash colours. Only the colours change, never the text.

    "ansi_dark" uses the terminal's own palette, so it follows Lex's theme.
    """
    text = Syntax("", "bash", theme="ansi_dark").highlight(command)
    text.rstrip()
    return text


class ChoiceModal(ModalScreen[bool]):
    """A question at the bottom of the screen with numbered choices, like:

        Do you want to run it?
        ❯ 1. Yes
          2. No (esc)

    ↑/↓ and Enter, or the number, or y/n. The first choice is always the
    "yes" one, and it ignores keys for ARM_S after the question appears.
    """

    BINDINGS = [
        Binding("up,k", "move(-1)", show=False),
        Binding("down,j", "move(1)", show=False),
        Binding("enter", "choose", show=False),
        Binding("1,y", "answer(True)", show=False),
        Binding("2,n,escape", "answer(False)", show=False),
    ]
    QUESTION = "Do you want to go ahead?"
    CHOICES = ("Yes", "No (esc)")

    def __init__(self) -> None:
        super().__init__()
        self.cursor = 0
        self.shown_at = time.monotonic()

    def choices(self) -> Text:
        text = Text()
        for i, choice in enumerate(self.CHOICES):
            here = i == self.cursor
            text.append("❯ " if here else "  ", "bold")
            text.append(f"{i + 1}. {choice}", "bold" if here else "dim")
            if i < len(self.CHOICES) - 1:
                text.append("\n")
        return text

    def on_mount(self) -> None:
        self.shown_at = time.monotonic()

    def action_move(self, step: int) -> None:
        self.cursor = (self.cursor + step) % len(self.CHOICES)
        self.query_one(".ask-choices", Static).update(self.choices())

    def action_choose(self) -> None:
        self.action_answer(self.cursor == 0)

    def action_answer(self, yes: bool) -> None:
        if yes and time.monotonic() - self.shown_at < ARM_S:
            return  # too soon to be a real answer
        self.dismiss(yes)

    def on_click(self, event: events.Click) -> None:
        # Clicking a choice line picks it.
        widget = event.widget
        if isinstance(widget, Static) and widget.has_class("ask-choices"):
            self.cursor = max(0, min(event.y, len(self.CHOICES) - 1))
            self.action_choose()


class ApprovalModal(ChoiceModal):
    """Shows what a confirm-tier command does and how to undo it; waits for y/n."""

    QUESTION = "Do you want to run it?"
    CHOICES = ("Yes", "No, don't run it (esc)")

    def __init__(
        self, command: str, assessment: Assessment, explanation: str, undo: str, remote: bool
    ) -> None:
        super().__init__()
        self.command = command
        self.assessment = assessment
        self.explanation = explanation.strip() or "(CORE's model gave no explanation)"
        self.undo = undo.strip() or "(no undo steps given)"
        self.remote = remote

    def compose(self) -> ComposeResult:
        with Vertical(id="approval", classes="ask") as box:
            box.border_title = "Bash command"
            command = highlight(self.command)
            command.stylize("bold")
            yield Static(command, classes="ask-command")
            yield Static(Text(self.explanation), classes="ask-explain")
            with Grid(classes="facts"):
                for name, value in [("Why it asks", self.assessment.reason),
                                    ("To undo", self.undo)]:  # fmt: skip
                    yield Static(name, classes="fact-name")
                    yield Static(Text(value))
            if self.remote:
                yield Static("You're connected remotely: this runs on the PC.", classes="note")
            yield Static(self.QUESTION, classes="ask-question")
            yield Static(self.choices(), classes="ask-choices")


class YesNoModal(ChoiceModal):
    """A plain question for Lex, e.g. "Delete this chat?". Not for commands."""

    CHOICES = ("Yes", "No (esc)")

    def __init__(self, question: str, detail: str = "") -> None:
        super().__init__()
        self.question = question
        self.detail = detail

    def compose(self) -> ComposeResult:
        with Vertical(id="yes-no", classes="ask"):
            yield Static(Text(self.question), classes="ask-question")
            if self.detail:
                yield Static(Text(self.detail), classes="note")
            yield Static(self.choices(), classes="ask-choices")


class PasswordModal(ModalScreen[str | None]):
    """A masked field for sudo. The value goes straight back to the PTY."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="password", classes="ask") as box:
            box.border_title = "sudo password"
            yield Input(password=True, placeholder="password (esc to cancel)")
            yield Static(
                "It goes straight to sudo. CORE doesn't save it or show it to the model.",
                classes="note",
            )

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value
        event.input.value = ""  # don't leave it sitting in the widget
        self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)


# What each status says under the command, and its colour.
OUTCOMES: dict[str, tuple[str, str]] = {
    "waiting": ("Waiting for your y/n…", "yellow"),
    "running": ("Running…", "dim"),
    "blocked": ("Refused: {reason}", "red"),
    "denied": ("Not run: you said no", "dim"),
    "invalid": ("Not run: it wouldn't have worked (checked first)", "yellow"),
    "not approved": ("Not run: it needs a y/n now (ctrl+g twice)", "yellow"),
    "launched": ("Started", "green"),
    "stopped": ("Stopped", "yellow"),
    "killed by Lex": ("Stopped", "yellow"),
    "timed out": ("Timed out", "red"),
    "command not found": ("Command not found", "red"),
}
# The bullet's colour; anything not listed (a failed exit, refused) is red.
BULLET = {"exit 0": "green", "launched": "green", "running": "yellow", "waiting": "yellow",
          "stopped": "yellow", "killed by Lex": "yellow", "saved": "dim", "denied": "dim",
          "invalid": "dim", "not approved": "dim"}  # fmt: skip


class CommandBlock(Vertical):
    """One command in the chat, drawn like a tool call in Claude Code:

        ⏺ Bash(uname -r)
          ⎿  6.18.44-fc-v139

    The bullet is green when it worked, red when it failed or was refused,
    yellow while it waits or runs. Click it (or Ctrl+O for the latest) to
    see all the output. `trigger` is the hand sign that ran it, for gesture
    macros (macros.py); those never wait here, as Lex approved them when
    gesture mode turned on.
    """

    class Changed(Message):
        """Posted when the command starts or stops, so the screen can update."""

        def __init__(self, block: CommandBlock) -> None:
            super().__init__()
            self.block = block

    def __init__(
        self, command: str, assessment: Assessment, trigger: str = "", approved: bool = False
    ) -> None:
        super().__init__(classes="command")
        self.command = command
        self.assessment = assessment
        self.trigger = trigger
        self.raw = ""
        # Confirm-tier commands wait for Lex; nothing runs until attach().
        self.status = "waiting" if assessment.tier == Tier.CONFIRM and not trigger else "running"
        self.approved = approved
        self.duration: float | None = None
        self.expanded = False
        self.running: RunningCommand | None = None
        self._render_pending = False

    def compose(self) -> ComposeResult:
        yield Static(self._header(), classes="command-header")
        yield Static("", classes="command-output", markup=False)

    def on_mount(self) -> None:
        self._render_output()

    # CommandView protocol (see core/tools/registry.py) -------------------

    def attach(self, running: RunningCommand) -> None:
        self.running = running
        self.status = "running"
        self.approved = self.assessment.tier == Tier.CONFIRM
        self._refresh_all()
        self.post_message(self.Changed(self))

    def output(self, text: str) -> None:
        self.raw = (self.raw + text)[-500_000:]
        # Redraw at most 20 times a second, however fast output arrives.
        if self.is_mounted and not self._render_pending:
            self._render_pending = True
            self.set_timer(0.05, self._render_output)

    def finish(self, status: str, result: CommandResult | None = None) -> None:
        self.status = status
        self.running = None
        if result is not None:
            self.raw = result.output
            self.duration = result.duration_s
        self._refresh_all()
        self.post_message(self.Changed(self))

    # ---------------------------------------------------------------------

    def toggle(self) -> None:
        self.expanded = not self.expanded
        self._render_output()

    def on_click(self, event: events.Click) -> None:
        self.toggle()

    def summary(self) -> str:
        """The header and the outcome line, as plain text (for tests and logs)."""
        outcome = self._outcome()
        return self._header().plain + (f"\n{outcome[0]}" if outcome else "")

    def _refresh_all(self) -> None:
        if self.is_mounted:
            self.query_one(".command-header", Static).update(self._header())
            self._render_output()

    def _header(self) -> Text:
        header = Text()  # not Text("⏺ ", style): that style would colour the whole line
        header.append("⏺ ", BULLET.get(self.status, "red"))
        if self.trigger:
            header.append(f"{self.trigger} · ", "bold")
        header.append("Bash", "bold")
        header.append(f"({self.command})")
        extras = []
        if self.approved:
            extras.append("approved")
        if self.duration is not None and self.duration >= 0.1:
            extras.append(f"{self.duration:.1f}s")
        if extras:
            header.append("  " + " · ".join(extras), "dim")
        return header

    def _outcome(self) -> tuple[str, str] | None:
        """The line under the command saying how it went, if there is one."""
        if self.status in OUTCOMES:
            if self.status == "running" and self.raw:
                return None  # the output says it all
            text, style = OUTCOMES[self.status]
            return text.format(reason=self.assessment.reason), style
        if self.status.startswith("exit ") and self.status != "exit 0":
            return f"Exit {self.status.removeprefix('exit ')}", "red"
        return None

    def _render_output(self) -> None:
        self._render_pending = False
        lines = clean_output(self.raw).splitlines()
        if not self.expanded and len(lines) > PREVIEW_LINES:
            hidden = len(lines) - PREVIEW_LINES
            lines = [f"… +{hidden} earlier lines (click or ctrl+o to expand)",
                     *lines[-PREVIEW_LINES:]]  # fmt: skip
        elif self.expanded:
            lines = lines[-MAX_LINES:]
        outcome = self._outcome()
        rows: list[tuple[str, str]] = [outcome] if outcome else []
        rows += [(line, "dim") for line in lines]
        if not rows and self.status == "exit 0":
            rows = [("(no output)", "dim")]
        body = Text()
        for i, (line, style) in enumerate(rows):
            body.append("  ⎿  " if i == 0 else "     ", "dim")
            body.append(line, style)
            if i < len(rows) - 1:
                body.append("\n")
        out = self.query_one(".command-output", Static)
        out.display = bool(rows)
        out.update(body)
