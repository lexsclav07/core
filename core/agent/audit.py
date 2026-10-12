"""Append-only audit log of every command CORE runs, refuses, or is denied.

One JSON object per line in ~/.local/share/core/audit.log, so it's easy to
read with `tail` and easy to parse later (e.g. for the phase 7 fine-tune).
Passwords never reach this file: they go straight to the PTY (see shell.py)
and are never part of the command text.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

# invalid = preflight.py found it can't work, so Lex was never asked.
Approval = Literal["auto", "lex", "denied", "blocked", "invalid"]


@dataclass(frozen=True)
class AuditEntry:
    command: str
    tier: str  # SAFE / CONFIRM / BLOCKED
    reason: str
    approval: Approval  # auto = safe tier, lex = Lex said y
    session: str  # local / remote
    exit_code: int | None = None  # None if it never ran, was killed, or timed out
    duration_s: float | None = None
    note: str = ""  # e.g. "timed out", "killed by Lex"


def audit_path() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "core" / "audit.log"


def record(entry: AuditEntry, path: Path | None = None) -> None:
    path = path or audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = {"time": datetime.now().astimezone().isoformat(timespec="seconds"), **asdict(entry)}
    with path.open("a") as f:
        f.write(json.dumps(line) + "\n")
