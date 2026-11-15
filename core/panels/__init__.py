"""The tabs next to the chat: History (F3), Activity (F4) and Settings (F5).

Each tab is one widget in its own module. They talk to the rest of CORE
through the app (CoreApp in core/app.py) and by posting messages that the
chat screen handles, so none of them needs to know how the chat works.
"""

from __future__ import annotations

from datetime import datetime, timedelta


def when(moment: datetime, now: datetime | None = None) -> str:
    """Short and human: '21:04', 'Yesterday 21:04', 'Mon 21:04', 'Nov 3', '2025-11-03'."""
    now = now or datetime.now(moment.tzinfo)
    days = (now.date() - moment.date()).days
    if days == 0:
        return f"{moment:%H:%M}"
    if days == 1:
        return f"Yesterday {moment:%H:%M}"
    if 1 < days < 7:
        return f"{moment:%a %H:%M}"
    if moment.year == now.year:
        return f"{moment:%b} {moment.day}"
    return f"{moment:%Y-%m-%d}"


def ago(moment: datetime, now: datetime | None = None) -> timedelta:
    return (now or datetime.now(moment.tzinfo)) - moment
