"""Slash commands: type / in the input box for a menu of them, like Claude Code.

A message whose first word is a known command runs it instead of going to
the model. Anything else that starts with "/" (say "/etc/fstab looks
wrong") still goes to the model as normal.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from textual.widgets import OptionList
from textual.widgets.option_list import Option


@dataclass(frozen=True)
class Command:
    name: str  # without the slash
    about: str
    action: str  # what the chat screen runs, e.g. "new_chat" or "show_tab('history')"


COMMANDS = [
    Command("help", "keys, approvals and where CORE keeps things", "help"),
    Command("clear", "start a new chat (this one stays in /resume)", "new_chat"),
    Command("resume", "carry on a saved chat", "show_tab('history')"),
    Command("activity", "everything CORE ran, asked about or refused", "show_tab('activity')"),
    Command("settings", "model, thinking and colours", "show_tab('settings')"),
    Command("model", "pick the chat model", "pick_model"),
    Command("talk", "push-to-talk from the mic (ctrl+t)", "talk"),
    Command("signs", "gesture mode on or off (ctrl+g)", "gestures"),
    Command("quit", "close CORE (ctrl+q)", "app.quit"),
]


def matching(typed: str) -> list[Command]:
    """Commands for what's typed so far: "/cl" → clear. Name starts first, then contains."""
    word = typed.removeprefix("/").strip().lower()
    starts = [c for c in COMMANDS if c.name.startswith(word)]
    return starts + [c for c in COMMANDS if word in c.name and c not in starts]


def command_for(message: str) -> Command | None:
    """The command a message runs, if its first word is one ("/clear", "/model x")."""
    if not message.startswith("/") or not message[1:].strip():
        return None
    first = message.split()[0][1:].lower()
    return next((c for c in COMMANDS if c.name == first), None)


class CommandMenu(OptionList):
    """The list under the input while a / command is typed. ↑/↓ pick, Enter runs."""

    def show_for(self, typed: str) -> None:
        """Fill the menu for what's typed (or hide it when it isn't a command)."""
        found = matching(typed) if typed.startswith("/") and " " not in typed else []
        self.clear_options()
        for command in found:
            line = Text.assemble((f"/{command.name}", "bold"), "  ", (command.about, "dim"))
            self.add_option(Option(line, id=command.name))
        self.display = bool(found)
        if found:
            self.highlighted = 0

    @property
    def chosen(self) -> Command | None:
        """The highlighted command, if the menu is showing."""
        if not self.display or self.highlighted is None:
            return None
        name = self.get_option_at_index(self.highlighted).id
        return next((c for c in COMMANDS if c.name == name), None)
