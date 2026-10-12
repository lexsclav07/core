"""TUI pieces for running commands: approval dialog, password field, command block.

All text that comes from the model or from commands is shown with
markup=False / rich Text, so a `[red]` or `[ -f x ]` in it is shown as-is
instead of being read as Textual styling.
"""

from __future__ import annotations

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from core.agent.risk import Assessment, Tier
from core.tools.pty_runner import CommandResult, RunningCommand, clean_output

PREVIEW_LINES = 12  # output lines shown while a block is collapsed
MAX_LINES = 2000  # lines shown when expanded


class ApprovalModal(ModalScreen[bool]):
    """Shows what a confirm-tier command does and waits for y/n."""

    BINDINGS = [
        Binding("y", "answer(True)", "Run it"),
        Binding("n,escape", "answer(False)", "Don't run"),
    ]

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
        with Vertical(id="approval"):
            yield Static("CORE wants to run", classes="modal-title")
            yield Static(Text(f"$ {self.command}"), classes="modal-command")
            yield Static(Text.assemble(("Why it's asking  ", "bold"), self.assessment.reason))
            yield Static(Text.assemble(("What it does     ", "bold"), self.explanation))
            yield Static(Text.assemble(("How to undo      ", "bold"), self.undo))
            if self.remote:
                yield Static("You're connected remotely: this runs on the PC.", classes="note")
            with Horizontal(classes="modal-buttons"):
                yield Button("\\[y] Run it", id="yes", variant="primary")
                yield Button("\\[n] Don't run", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_answer(self, run: bool) -> None:
        self.dismiss(run)


class PasswordModal(ModalScreen[str | None]):
    """A masked field for sudo. The value goes straight back to the PTY."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="password"):
            yield Static("sudo needs your password", classes="modal-title")
            yield Input(password=True, placeholder="password (Esc to cancel)")
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


class CommandBlock(Vertical):
    """One command in the chat: `$ cmd` with a status badge, then its output.

    Click the header (or Ctrl+O for the latest block) to expand or collapse.
    """

    class Changed(Message):
        """Posted when the command starts or stops, so the screen can update."""

        def __init__(self, block: CommandBlock) -> None:
            super().__init__()
            self.block = block

    def __init__(self, command: str, assessment: Assessment) -> None:
        super().__init__(classes="command")
        self.command = command
        self.assessment = assessment
        self.raw = ""
        # Confirm-tier commands wait for Lex; nothing runs until attach().
        self.status = "waiting" if assessment.tier == Tier.CONFIRM else "running"
        self.approved = False
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
        if self.is_mounted:
            self.query_one(".command-header", Static).update(self._header())
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
        if self.is_mounted:
            self.query_one(".command-header", Static).update(self._header())
            self._render_output()
        self.post_message(self.Changed(self))

    # ---------------------------------------------------------------------

    def toggle(self) -> None:
        self.expanded = not self.expanded
        self._render_output()

    def on_click(self, event: events.Click) -> None:
        self.toggle()

    def _header(self) -> Text:
        badge, style = {
            "waiting": ("⏸ waiting for your y/n", "yellow"),
            "running": ("● running", "yellow"),
            "blocked": ("⛔ blocked", "bold red"),
            "denied": ("✗ not run", "red"),
            "invalid": ("✗ won't work (checked)", "yellow"),
            "exit 0": ("✓", "green"),
        }.get(self.status, (f"✗ {self.status}", "red"))
        header = Text.assemble(("$ ", "dim"), (self.command, "bold"), "  ", (badge, style))
        if self.status == "blocked":
            header.append(f"\n  {self.assessment.reason}", "red")
        elif self.approved:
            header.append("  approved", "dim")
        return header

    def _render_output(self) -> None:
        self._render_pending = False
        lines = clean_output(self.raw).splitlines()
        out = self.query_one(".command-output", Static)
        out.display = bool(lines)
        if self.expanded:
            shown = lines[-MAX_LINES:]
        else:
            shown = lines[-PREVIEW_LINES:]
            if len(lines) > PREVIEW_LINES:
                hidden = len(lines) - PREVIEW_LINES
                shown = [f"… {hidden} earlier lines (click or Ctrl+O to expand)", *shown]
        out.update("\n".join(shown))
