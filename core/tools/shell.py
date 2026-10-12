"""The run_command tool: CORE's only way to touch the machine (for now).

Every call goes through the same gate, in code, not just in the prompt:

    classify (risk.py) → taint check → refuse / ask Lex / run → audit log

The model has to explain each command and say how to undo it. The
confirmation dialog shows that explanation next to the *raw* command, so a
vague or wrong explanation can't hide what actually runs.
"""

from __future__ import annotations

import asyncio

from core.agent import audit
from core.agent.risk import Assessment, Tier, classify, default_context
from core.tools.pty_runner import run_in_pty, summarise
from core.tools.registry import ToolContext, ToolResult, tool

MAX_TIMEOUT = 1800


@tool
async def run_command(
    ctx: ToolContext,
    command: str,
    explanation: str,
    undo: str,
    timeout: int = 120,  # noqa: ASYNC109 (the model sees this name; it's not an asyncio timeout)
) -> ToolResult:
    """Run a bash command on Lex's Fedora PC and get its output and exit code.

    Each call runs in a fresh bash shell in Lex's home folder, so `cd` does not
    carry over between calls; use absolute paths or `cd dir && cmd`. Read-only
    commands run straight away. Anything that changes the system is shown to
    Lex for approval first, and destructive commands are refused. Don't use
    interactive programs (vim, less, top without -b).

    Args:
        command: The bash command line to run.
        explanation: Plain English: what this does and what it changes. Lex reads
            this before approving.
        undo: How to undo it, e.g. "sudo dnf remove htop". Use "nothing to undo"
            for read-only commands.
        timeout: Seconds before the command is stopped. Default 120, max 1800.
    """
    assessment = classify(command, default_context(ctx.cwd))
    if ctx.tainted and assessment.tier == Tier.SAFE:
        # Rule 6: once outside content is in play this turn, it might be
        # steering the model, so nothing runs without Lex seeing it.
        assessment = Assessment(
            Tier.CONFIRM,
            "earlier output this turn came from outside content (files, web, logs)",
            assessment.reads_untrusted,
        )
    view = await ctx.ui.command_view(command, assessment)

    def log(
        approval: audit.Approval,
        exit_code: int | None = None,
        duration_s: float | None = None,
        note: str = "",
    ) -> None:
        tier, reason = assessment.tier.name, assessment.reason
        audit.record(
            audit.AuditEntry(
                command, tier, reason, approval, ctx.session, exit_code, duration_s, note
            )
        )

    if assessment.tier == Tier.BLOCKED:
        view.finish("blocked")
        log("blocked")
        return ToolResult(
            f"BLOCKED by CORE's safety rules: {assessment.reason}. This can't be run, even if "
            "Lex asks. Don't try another way to do the same thing; explain the risk to Lex."
        )

    approval: audit.Approval = "auto"
    if assessment.tier == Tier.CONFIRM:
        if not await ctx.ui.approve(command, assessment, explanation, undo):
            view.finish("denied")
            log("denied")
            return ToolResult(
                "Not run: Lex pressed n in CORE's approval dialog, so they've already "
                "answered. Don't ask for approval again or retry it. Say briefly that "
                "you didn't run it, and offer an alternative only if there's a good one."
            )
        approval = "lex"

    try:
        result = await run_in_pty(
            command,
            cwd=ctx.cwd,
            time_limit=max(1, min(timeout, MAX_TIMEOUT)),
            on_output=view.output,
            ask_password=ctx.ui.ask_password,
            on_start=view.attach,
        )
    except asyncio.CancelledError:
        view.finish("stopped")
        log(approval, note="reply cancelled by Lex")
        raise
    note = "timed out" if result.timed_out else "killed by Lex" if result.killed else ""
    view.finish(note or f"exit {result.exit_code}", result)
    log(approval, exit_code=result.exit_code, duration_s=result.duration_s, note=note)

    status = (
        f"Timed out after {timeout}s and was stopped."
        if result.timed_out
        else "Lex stopped this command."
        if result.killed
        else f"Exit code {result.exit_code}."
    )
    body = summarise(result.output) or "(no output)"
    return ToolResult(f"{status}\n{body}", untrusted=assessment.reads_untrusted)
