"""The command block must never say "running" or "approved" before Lex says y.

summary() is what's on screen as text: the `⏺ Bash(…)` line and the outcome
line under it.
"""

import asyncio

from textual.app import App, ComposeResult

from core.agent.risk import Assessment, Tier
from core.tools.pty_runner import RunningCommand
from core.widgets import CommandBlock

CONFIRM = Assessment(Tier.CONFIRM, "runs as root (sudo)")


class Host(App[None]):
    def __init__(self, block: CommandBlock) -> None:
        super().__init__()
        self.block = block

    def compose(self) -> ComposeResult:
        yield self.block


async def test_confirm_block_waits_then_shows_approved() -> None:
    block = CommandBlock("sudo dnf install -y htop", CONFIRM)
    async with Host(block).run_test():
        shown = block.summary()
        assert shown == "⏺ Bash(sudo dnf install -y htop)\nWaiting for your y/n…"
        assert "Running" not in shown and "approved" not in shown

        block.attach(RunningCommand(master_fd=-1))
        shown = block.summary()
        assert "Running" in shown and "approved" in shown


async def test_denied_block_is_never_approved() -> None:
    block = CommandBlock("sudo dnf install -y htop", CONFIRM)
    async with Host(block).run_test():
        block.finish("denied")
        await asyncio.sleep(0)
        shown = block.summary()
        assert "Not run" in shown and "approved" not in shown


async def test_blocked_block_shows_the_reason() -> None:
    block = CommandBlock("rm -rf ~", Assessment(Tier.BLOCKED, "would wipe your home folder"))
    async with Host(block).run_test():
        block.finish("blocked")
        assert "Refused: would wipe your home folder" in block.summary()


async def test_macro_block_never_waits_and_shows_the_sign() -> None:
    block = CommandBlock("ptyxis --new-window", CONFIRM, trigger="✌ peace", approved=True)
    async with Host(block).run_test():
        shown = block.summary()
        assert shown.startswith("⏺ ✌ peace · Bash(") and "approved" in shown
        assert "Waiting" not in shown
        block.finish("launched")
        assert block.summary().endswith("Started")


async def test_unapproved_macro_block_never_says_approved() -> None:
    block = CommandBlock("ptyxis --new-window", CONFIRM, trigger="✌ peace")
    async with Host(block).run_test():
        block.finish("not approved")
        shown = block.summary()
        assert "Not run" in shown and "approved" not in shown


async def test_failed_command_says_so_and_output_hangs_off_it() -> None:
    from core.tools.pty_runner import CommandResult

    block = CommandBlock("false", Assessment(Tier.SAFE, "read-only"))
    async with Host(block).run_test():
        block.finish("exit 1", CommandResult(1, "it broke", 0.2))
        assert block.summary() == "⏺ Bash(false)  0.2s\nExit 1"
        body = str(block.query_one(".command-output").render())
        assert body == "  ⎿  Exit 1\n     it broke"
