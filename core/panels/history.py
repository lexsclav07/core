"""The History tab (F3): saved chats, newest first. Enter opens one.

Opening a chat puts it back on the Chat tab and CORE carries on from where
it was, with everything it knew then (see history.py and Conversation.load).
Type to search titles and everything said; Delete removes a chat after a
y/n.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, cast

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import DataTable, Input, Static

from core.history import ChatSummary
from core.panels import when
from core.widgets import YesNoModal

if TYPE_CHECKING:
    from core.app import CoreApp


class HistoryPanel(Vertical):
    # Delete only, not a letter: letters belong to the search box.
    BINDINGS = [Binding("delete", "delete", "Delete chat")]

    class Open(Message):
        """Lex picked a chat to carry on."""

        def __init__(self, chat_id: int) -> None:
            super().__init__()
            self.chat_id = chat_id

    def __init__(self) -> None:
        super().__init__(id="history")
        self.chats: list[ChatSummary] = []

    def compose(self) -> ComposeResult:
        yield Input(placeholder="Search your chats…", id="history-search")
        yield DataTable(id="history-table", cursor_type="row", zebra_stripes=True)
        yield Static(id="history-note", classes="note")

    def on_mount(self) -> None:
        self.query_one(DataTable).add_columns("When", "Chat", "Messages")
        self.reload()

    @property
    def core(self) -> CoreApp:
        return cast("CoreApp", self.app)

    def reload(self) -> None:
        note = self.query_one("#history-note", Static)
        search = self.query_one("#history-search", Input).value
        try:
            self.chats = self.core.store.chats(search)
        except sqlite3.Error as e:
            self.chats = []
            note.update(f"Couldn't read the saved chats: {e}")
            return
        table = self.query_one(DataTable)
        table.clear()
        for chat in self.chats:
            table.add_row(when(chat.updated), chat.title, str(chat.turns), key=str(chat.id))
        if not self.core.config.history.save:
            note.update("Saving chats is off ([history] save = false), so new chats won't appear.")
        elif self.chats:
            note.update("Enter opens a chat and CORE carries on from there. Delete removes one.")
        elif search:
            note.update("No saved chat mentions that.")
        else:
            note.update("No saved chats yet. Each chat is saved here after CORE's first reply.")

    def on_input_changed(self, event: Input.Changed) -> None:
        self.reload()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.query_one(DataTable).focus()  # Enter in the search box: go pick one

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key.value is not None:
            self.post_message(self.Open(int(event.row_key.value)))

    def action_delete(self) -> None:
        table = self.query_one(DataTable)
        if not self.chats or not 0 <= table.cursor_row < len(self.chats):
            return
        self._delete(self.chats[table.cursor_row])

    def _delete(self, chat: ChatSummary) -> None:
        def answered(yes: bool | None) -> None:
            if yes:
                self.core.store.delete(chat.id)
                self.reload()
                self.notify(f"Deleted “{chat.title}”.")

        self.app.push_screen(YesNoModal(f"Delete “{chat.title}”?",
                                        "It's gone for good, from this PC."), answered)  # fmt: skip

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "delete":
            return bool(self.chats)
        return True
