"""Saved chats, so the History tab (F3) can bring one back.

One SQLite file, ~/.local/share/core/history.db, with one row per chat: a
title (the first thing Lex asked), when it started and was last used, and
the messages as JSON, exactly as they go to Ollama. It stays on this PC, and
`[history] save = false` in config.toml turns it off.

What's saved is what the model saw: Lex's messages, CORE's replies, and the
commands with their output. sudo passwords never reach the messages (they go
straight to the PTY), so they're never saved either.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

Message = dict[str, Any]
AUTOMATIC = "(Automatic message from CORE"  # the loop's nudges, not Lex's words
TITLE_LENGTH = 70

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    started TEXT NOT NULL,
    updated TEXT NOT NULL,
    turns INTEGER NOT NULL,
    messages TEXT NOT NULL
)
"""


@dataclass(frozen=True)
class ChatSummary:
    id: int
    title: str
    started: datetime
    updated: datetime
    turns: int  # how many messages Lex sent


def history_path() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "core" / "history.db"


def from_lex(messages: list[Message]) -> list[str]:
    """What Lex actually typed or said, without CORE's automatic nudges."""
    return [
        str(m.get("content", ""))
        for m in messages
        if m.get("role") == "user" and not str(m.get("content", "")).startswith(AUTOMATIC)
    ]


def title_for(messages: list[Message]) -> str:
    said = from_lex(messages)
    first = " ".join(said[0].split()) if said else "(empty chat)"
    return first if len(first) <= TITLE_LENGTH else first[: TITLE_LENGTH - 1] + "…"


class ChatStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or history_path()

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:  # `with db` commits
            db.execute(SCHEMA)
            yield db

    def save(self, chat_id: int | None, messages: list[Message]) -> int:
        """Save a chat (no system prompt). Returns its id: new if chat_id is None."""
        now = datetime.now().isoformat(timespec="seconds")
        values = (title_for(messages), len(from_lex(messages)),
                  json.dumps(messages, ensure_ascii=False))  # fmt: skip
        with self._db() as db:
            if chat_id is not None:
                updated = db.execute(
                    "UPDATE chats SET title = ?, turns = ?, messages = ?, updated = ? WHERE id = ?",
                    (*values, now, chat_id),
                ).rowcount
                if updated:
                    return chat_id
            cursor = db.execute(
                "INSERT INTO chats (title, turns, messages, started, updated) "
                "VALUES (?, ?, ?, ?, ?)",
                (*values, now, now),
            )
            return int(cursor.lastrowid or 0)

    def chats(self, search: str = "", limit: int = 500) -> list[ChatSummary]:
        """Newest first. `search` matches the title or anything said in the chat."""
        with self._db() as db:
            rows = db.execute(
                "SELECT id, title, started, updated, turns FROM chats "
                "WHERE instr(lower(title || ' ' || messages), lower(?)) > 0 "
                "ORDER BY updated DESC, id DESC LIMIT ?",
                (search.strip(), limit),
            ).fetchall()
        return [
            ChatSummary(i, title, datetime.fromisoformat(s), datetime.fromisoformat(u), turns)
            for i, title, s, u, turns in rows
        ]

    def load(self, chat_id: int) -> list[Message]:
        with self._db() as db:
            row = db.execute("SELECT messages FROM chats WHERE id = ?", (chat_id,)).fetchone()
        if row is None:
            raise KeyError(chat_id)
        messages: list[Message] = json.loads(row[0])
        return messages

    def delete(self, chat_id: int) -> None:
        with self._db() as db:
            db.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
