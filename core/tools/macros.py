"""Gesture macros: Lex's own commands from config.toml, run by a hand sign.

They go through CORE's safety rules like everything else (CLAUDE.md):

  * risk.py classifies every macro. Blocked ones never run.
  * Confirm-tier macros are shown to Lex when gesture mode turns on, with a
    y/n each, and only the ones Lex says y to will fire. Asking again at the
    moment of the sign would defeat the point of a macro, and it's the exact
    same command every time. Turning gesture mode off and on asks again.
  * Every run is shown in the chat and written to the audit log.

The model isn't involved: a sign maps straight to Lex's command, so there's
no outside content in the loop that could steer it.

Macros start detached (their own session, no terminal) and CORE doesn't wait
for them, so a macro can open an app that stays open. The catch: with no
terminal, sudo can't ask for a password, so sudo macros are turned down.
"""

from __future__ import annotations

import asyncio
import shlex
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from core.agent import audit
from core.agent.risk import Assessment, Tier, classify, default_context
from core.tools.registry import CommandView
from core.tools.signs import label

LAUNCH_WAIT_S = 1.5  # long enough to catch a typo'd command failing straight away
NEEDS_TERMINAL = frozenset({"sudo", "pkexec", "su"})

_children: set[asyncio.Task[int]] = set()  # macros still running


class MacroUI(Protocol):
    async def approve(
        self, command: str, assessment: Assessment, explanation: str, undo: str
    ) -> bool: ...
    async def macro_view(
        self, sign: str, command: str, assessment: Assessment, approved: bool
    ) -> CommandView:
        """Show a macro run in the chat (it never waits for y/n)."""

    def gesture_note(self, text: str, warning: bool = False) -> None:
        """A short message for Lex, e.g. a toast."""


@dataclass(frozen=True)
class Macro:
    sign: str
    command: str
    approved: bool = False  # Lex said y to it when gesture mode turned on


def from_config(
    raw: Mapping[str, object], signs: list[str], terminal: str
) -> tuple[list[Macro], list[str]]:
    """Read [gestures.macros]; {terminal} becomes the terminal from [ui]."""
    found: list[Macro] = []
    problems: list[str] = []
    for sign, command in raw.items():
        if sign not in signs:
            problems.append(f"[gestures.macros] {sign}: no such sign. Signs: {', '.join(signs)}")
        elif not isinstance(command, str) or not command.strip():
            problems.append(f"[gestures.macros] {sign}: the macro must be a command in quotes")
        else:
            found.append(Macro(sign, command.replace("{terminal}", terminal).strip()))
    return found, problems


async def arm(ui: MacroUI, macros: list[Macro], session: str) -> dict[str, Macro]:
    """Check each macro and ask Lex about confirm-tier ones. Returns sign → macro."""
    armed: dict[str, Macro] = {}
    for macro in macros:
        assessment = _classify(macro.command)
        name = label(macro.sign)
        if assessment.tier == Tier.BLOCKED:
            ui.gesture_note(f"{name} macro refused: {assessment.reason}.", warning=True)
            _log(macro, assessment, "blocked", session, note="gesture macro")
            continue
        if _needs_terminal(macro.command):
            ui.gesture_note(
                f"{name} macro turned down: macros run without a terminal, so sudo "
                "can't ask for your password.",
                warning=True,
            )
            _log(macro, assessment, "invalid", session, note="gesture macro: sudo needs a terminal")
            continue
        if assessment.tier == Tier.CONFIRM:
            explanation = (
                f"Your macro for the {name} sign (from config.toml). Say y and it runs "
                "every time you make the sign, without asking, until gesture mode turns off."
            )
            undo = "Press n to leave this sign off. Ctrl+G turns gesture mode off."
            if not await ui.approve(macro.command, assessment, explanation, undo):
                _log(macro, assessment, "denied", session, note="gesture macro left off")
                continue
            macro = replace(macro, approved=True)
        armed[macro.sign] = macro
    return armed


async def fire(ui: MacroUI, macro: Macro, session: str) -> None:
    """Run one macro because its sign was made."""
    # Classify again: disks and files can change while gesture mode is on.
    assessment = _classify(macro.command)
    approved = macro.approved and assessment.tier == Tier.CONFIRM
    view = await ui.macro_view(label(macro.sign), macro.command, assessment, approved)
    if assessment.tier == Tier.BLOCKED:
        view.finish("blocked")
        _log(macro, assessment, "blocked", session, note="gesture macro")
        return
    if assessment.tier == Tier.CONFIRM and not macro.approved:
        # It was safe when gesture mode started and isn't now. Never run it unasked.
        view.finish("not approved")
        _log(macro, assessment, "denied", session, note="gesture macro, needs a y/n now")
        return
    started = time.monotonic()
    code = await launch(macro.command)
    duration = round(time.monotonic() - started, 2)
    # 127 is bash for "no such command": most likely a typo in config.toml.
    view.finish(
        "launched" if code is None else "command not found" if code == 127 else f"exit {code}"
    )
    approval: audit.Approval = "lex" if macro.approved else "auto"
    still = ", still running" if code is None else ""
    note = f"gesture {macro.sign}{still}"
    _log(macro, assessment, approval, session, exit_code=code, duration_s=duration, note=note)


async def launch(command: str) -> int | None:
    """Start `command` detached. Its exit code if it ends quickly, else None."""
    proc = await asyncio.create_subprocess_exec(
        "bash", "-c", command, cwd=Path.home(),
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,  # its own session: it outlives CORE, and Ctrl+C can't hit it
    )  # fmt: skip
    waiting = asyncio.create_task(proc.wait())
    # Hold on to it until the process ends (maybe long after), so it gets reaped.
    _children.add(waiting)
    waiting.add_done_callback(_children.discard)
    done, _ = await asyncio.wait({waiting}, timeout=LAUNCH_WAIT_S)
    return waiting.result() if done else None  # None: still running, e.g. an app it opened


def _classify(command: str) -> Assessment:
    return classify(command, default_context(str(Path.home())))


def _needs_terminal(command: str) -> bool:
    try:
        return bool(NEEDS_TERMINAL & set(shlex.split(command)))
    except ValueError:
        return False  # odd quoting: bash will complain when it runs


def _log(
    macro: Macro,
    assessment: Assessment,
    approval: audit.Approval,
    session: str,
    exit_code: int | None = None,
    duration_s: float | None = None,
    note: str = "",
) -> None:
    entry = audit.AuditEntry(
        macro.command, assessment.tier.name, assessment.reason, approval, session,
        exit_code, duration_s, note,
    )  # fmt: skip
    audit.record(entry)
