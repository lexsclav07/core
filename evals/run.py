"""Measure how reliably CORE handles real requests, against the real model.

    uv run python -m evals.run                 # every task, 3 tries each
    uv run python -m evals.run --think off     # compare thinking on/off
    uv run python -m evals.run --only 5 --trials 5 -v

Purely read-only commands really run. Anything that would change the PC is
auto-denied, including user-scope actions that are normally safe tier (like
`flatpak install --user`), so a run never changes anything; we only record
what CORE *proposed*. The audit log goes to a temp dir so the real one stays clean.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import tempfile
import time
from pathlib import Path

import core.tools.shell
from core.agent.loop import Conversation
from core.agent.ollama_client import OllamaClient, OllamaError
from core.agent.risk import Assessment, Tier, classify
from core.config import load_config
from core.tools.pty_runner import CommandResult, RunningCommand
from core.tools.shell import run_command
from evals.tasks import TASKS, Run, Task

# Safe-tier reasons that mean "only reads". Every other safe action is a change.
READ_ONLY_REASONS = {
    "read-only",
    "shows service status",
    "only prints version or help",
    "downloads to the screen only",
    "lists partitions",
    "prints the environment",
}


def eval_classify(command: str, ctx: object = None) -> Assessment:
    """Like risk.classify, but user-scope changes go to the (auto-denied) confirm tier."""
    a = classify(command, ctx)  # type: ignore[arg-type]
    if a.tier == Tier.SAFE and any(r not in READ_ONLY_REASONS for r in a.reason.split("; ")):
        return Assessment(Tier.CONFIRM, f"eval: {a.reason}", a.reads_untrusted)
    return a


class RecordingView:
    def __init__(self, run: Run, command: str, assessment: Assessment) -> None:
        self.run, self.command, self.tier = run, command, assessment.tier

    def attach(self, running: RunningCommand) -> None:
        pass

    def output(self, text: str) -> None:
        pass

    def finish(self, status: str, result: CommandResult | None = None) -> None:
        if status == "blocked":
            self.run.blocked.append(self.command)
        elif status == "denied":
            self.run.proposed.append(self.command)
        else:
            self.run.ran.append(self.command)


class AutoDenyUI:
    """Says n to everything that needs approval, and records it."""

    def __init__(self, run: Run) -> None:
        self.run = run

    async def approve(self, command: str, a: Assessment, explanation: str, undo: str) -> bool:
        return False

    async def ask_password(self) -> str | None:
        return None

    async def command_view(self, command: str, assessment: Assessment) -> RecordingView:
        return RecordingView(self.run, command, assessment)


async def attempt(client: OllamaClient, task: Task, think: bool) -> tuple[Run, float]:
    cfg = load_config()
    run = Run()
    convo = Conversation(
        client,
        cfg.model.main,
        num_ctx=cfg.model.num_ctx,
        think=think,
        tools=[run_command],
        ui=AutoDenyUI(run),
        cwd=str(Path.home()),
    )
    started = time.monotonic()
    try:
        parts = [c.text async for c in convo.send(task.prompt) if c.kind == "content"]
        run.reply = "".join(parts)
    except OllamaError as e:
        run.reply = f"(error: {e})"
    return run, time.monotonic() - started


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--think", choices=["on", "off", "both"], default="on")
    parser.add_argument("--only", type=int, nargs="*", help="task numbers to run")
    parser.add_argument("-v", "--verbose", action="store_true", help="show every attempt")
    args = parser.parse_args()

    os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp(prefix="core-eval-")
    core.tools.shell.classify = eval_classify  # type: ignore[assignment]
    modes = {"on": [True], "off": [False], "both": [True, False]}[args.think]
    picked = [(i, t) for i, t in enumerate(TASKS, 1) if not args.only or i in args.only]
    client = OllamaClient(load_config().ollama.url)

    for think in modes:
        print(f"\n=== thinking {'on' if think else 'off'}, {args.trials} tries per task ===")
        total = passed = 0
        seconds = 0.0
        for i, task in picked:
            results = [await attempt(client, task, think) for _ in range(args.trials)]
            ok = [task.check(r) for r, _ in results]
            avg = sum(s for _, s in results) / len(results)
            total += len(ok)
            passed += sum(ok)
            seconds += sum(s for _, s in results)
            mark = "✓" if all(ok) else "~" if any(ok) else "✗"
            print(f"{mark} {i:2}. {sum(ok)}/{len(ok)}  {avg:5.1f}s  {task.prompt}")
            for (run, _), good in zip(results, ok, strict=True):
                if args.verbose or not good:
                    status = "ok  " if good else "FAIL"
                    print(f"      {status} ran={run.ran} proposed={run.proposed} "
                          f"blocked={run.blocked}")  # fmt: skip
                    if not good:
                        print(f"           want: {task.note}")
                        print(f"           reply: {run.reply[:160]!r}")
        print(f"--- {passed}/{total} passed ({100 * passed // max(total, 1)}%), "
              f"avg {seconds / max(total, 1):.1f}s per task")  # fmt: skip
    await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
