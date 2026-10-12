"""The command block must never say "running" or "approved" before Lex says y."""

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
        header = block._header().plain
        assert "waiting for your y/n" in header
        assert "running" not in header and "approved" not in header

        block.attach(RunningCommand(master_fd=-1))
        header = block._header().plain
        assert "running" in header and "approved" in header


async def test_denied_block_is_never_approved() -> None:
    block = CommandBlock("sudo dnf install -y htop", CONFIRM)
    async with Host(block).run_test():
        block.finish("denied")
        await asyncio.sleep(0)
        header = block._header().plain
        assert "not run" in header and "approved" not in header


async def test_blocked_block_shows_the_reason() -> None:
    block = CommandBlock("rm -rf ~", Assessment(Tier.BLOCKED, "would wipe your home folder"))
    async with Host(block).run_test():
        block.finish("blocked")
        assert "would wipe your home folder" in block._header().plain
