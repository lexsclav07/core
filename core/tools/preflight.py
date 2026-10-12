"""Catch commands that can't work before Lex is asked to approve them.

A small model often guesses: a GNOME settings key that doesn't exist, or a
Flathub app ID it made up. Running those just fails, after Lex has already
said yes. So for these two common cases we check first (with read-only
commands) and send the model back to look it up instead.
"""

from __future__ import annotations

import re
import shlex
import subprocess

APP_ID = re.compile(r"^[A-Za-z][\w-]*(\.[\w-]+){2,}$")  # e.g. org.prismlauncher.PrismLauncher


def problems(command: str) -> str | None:
    """A message for the model if the command is sure to fail, else None."""
    for part in re.split(r"&&|;|\|\|", command):
        try:
            words = shlex.split(part)
        except ValueError:
            return None  # odd quoting: leave it to the real shell
        if words[:1] == ["sudo"]:
            words = words[1:]
        if words[:2] == ["gsettings", "set"] and len(words) >= 4:
            found = _gsettings_key(words[2], words[3])
            if found is not None:
                return found
        if words[:2] == ["flatpak", "install"]:
            found = _flatpak_app(words[2:])
            if found is not None:
                return found
    return None


def _gsettings_key(schema: str, key: str) -> str | None:
    if _ok(["gsettings", "range", schema, key]):
        return None
    return (
        f"Not run: the GNOME setting {schema} {key} doesn't exist. Find the right "
        f"schema and key first, e.g. `gsettings list-recursively | grep -i {key.split('-')[0]}`."
    )


def _flatpak_app(args: list[str]) -> str | None:
    ids = [a for a in args if APP_ID.match(a)]
    if not ids:
        return None
    scope = "--user" if "--user" in args else "--system"
    remote = "flathub"
    for app in ids:
        if not _ok(["flatpak", "remote-info", scope, remote, app]):
            name = app.rsplit(".", 1)[-1]
            return (
                f"Not run: there's no app {app} on Flathub. Find the real ID with "
                f"`flatpak search {name}` and use the Application ID column."
            )
    return None


def _ok(cmd: list[str]) -> bool:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return True  # can't check: don't block the command over it
