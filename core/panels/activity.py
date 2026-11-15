"""The Activity tab (F4): everything CORE ran, asked about or refused.

It reads the audit log (~/.local/share/core/audit.log, see agent/audit.py),
newest first, and reads it again whenever the file changes while the tab is
open. Type in the filter box to narrow it down; the row under the cursor is
shown in full underneath.

Commands are shown as plain text (rich Text, never markup): a command can
contain anything, including text that came from outside.
"""

from __future__ import annotations

import json
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import DataTable, Input, Static

from core.agent.audit import audit_path
from core.panels import when

LIMIT = 1000  # newest entries shown; the file itself keeps everything
COMMAND_WIDTH = 70
OUTCOME = {
    "auto": "ran: read-only",
    "lex": "ran: you said y",
    "denied": "you said n",
    "blocked": "refused",
    "invalid": "wouldn't work",
}
TIER_STYLE = {"SAFE": "green", "CONFIRM": "yellow", "BLOCKED": "bold red"}


def read_entries(path: Path, limit: int = LIMIT) -> list[dict[str, Any]]:
    """The newest `limit` audit entries, newest first. Broken lines are skipped."""
    if not path.exists():
        return []
    with path.open(encoding="utf-8", errors="replace") as f:
        lines = deque(f, maxlen=limit)
    entries = []
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and "command" in entry:
            entries.append(entry)
    return entries


def matches(entry: dict[str, Any], words: str) -> bool:
    """Every word of the filter appears in the command, tier, outcome or note."""
    haystack = " ".join([
        str(entry.get("command", "")), str(entry.get("tier", "")),
        OUTCOME.get(str(entry.get("approval")), ""), str(entry.get("note", "")),
    ]).lower()  # fmt: skip
    return all(word in haystack for word in words.lower().split())


def detail(entry: dict[str, Any]) -> Text:
    exit_code = entry.get("exit_code")
    lines = [
        ("Command  ", str(entry.get("command", ""))),
        ("Why      ", f"{entry.get('tier', '?')}: {entry.get('reason', '')}"),
        ("Outcome  ", OUTCOME.get(str(entry.get("approval")), str(entry.get("approval")))),
        ("Exit     ", "—" if exit_code is None else str(exit_code)),
        ("Took     ", f"{entry['duration_s']:.1f}s" if entry.get("duration_s") else "—"),
        ("When     ", f"{_long_time(entry.get('time'))} ({entry.get('session', '?')} session)"),
    ]
    if entry.get("note"):
        lines.append(("Note     ", str(entry["note"])))
    text = Text()
    for i, (name, value) in enumerate(lines):
        text.append(name, "bold")
        text.append(value + ("\n" if i < len(lines) - 1 else ""))
    return text


def _long_time(value: object) -> str:
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    return f"{moment:%a} {moment.day} {moment:%b %Y, %H:%M:%S}"


class ActivityPanel(Vertical):
    def __init__(self, path: Path | None = None) -> None:
        super().__init__(id="activity")
        self.path = path or audit_path()
        self.entries: list[dict[str, Any]] = []
        self.seen_mtime = -1.0

    def compose(self) -> ComposeResult:
        yield Input(placeholder="Filter: a command, SAFE / CONFIRM / BLOCKED, refused…",
                    id="activity-filter")  # fmt: skip
        yield DataTable(id="activity-table", cursor_type="row", zebra_stripes=True)
        yield Static(id="activity-detail")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("When", "Tier", "Outcome", "Exit", "Command")
        self.reload()
        self.set_interval(2, self._reload_if_changed)

    def _reload_if_changed(self) -> None:
        if not self.display or not self.is_on_screen:
            return  # only while the tab is open
        mtime = self.path.stat().st_mtime if self.path.exists() else 0.0
        if mtime != self.seen_mtime:
            self.reload()

    def reload(self) -> None:
        self.seen_mtime = self.path.stat().st_mtime if self.path.exists() else 0.0
        words = self.query_one("#activity-filter", Input).value
        self.entries = [e for e in read_entries(self.path) if matches(e, words)]
        table = self.query_one(DataTable)
        table.clear()
        for entry in self.entries:
            command = " ".join(str(entry.get("command", "")).split())
            if len(command) > COMMAND_WIDTH:
                command = command[: COMMAND_WIDTH - 1] + "…"
            tier = str(entry.get("tier", "?"))
            exit_code = entry.get("exit_code")
            table.add_row(
                self._when(entry), Text(tier, TIER_STYLE.get(tier, "")),
                OUTCOME.get(str(entry.get("approval")), str(entry.get("approval"))),
                "—" if exit_code is None else str(exit_code), Text(command),
            )  # fmt: skip
        empty = "Nothing yet: commands CORE runs, asks about or refuses show up here."
        self._show(self.entries[0] if self.entries else None, empty)

    def on_input_changed(self, event: Input.Changed) -> None:
        self.reload()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if 0 <= event.cursor_row < len(self.entries):
            self._show(self.entries[event.cursor_row])

    def _show(self, entry: dict[str, Any] | None, empty: str = "") -> None:
        self.query_one("#activity-detail", Static).update(detail(entry) if entry else empty)

    @staticmethod
    def _when(entry: dict[str, Any]) -> str:
        try:
            return when(datetime.fromisoformat(str(entry.get("time"))))
        except ValueError:
            return "?"
