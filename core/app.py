"""The Textual app: logo splash, then the chat screen with a status bar.

Layout of the chat screen:
    ┌──────────────────────────────┐
    │ messages (scrolls)           │
    ├──────────────────────────────┤
    │ > input                      │
    ├──────────────────────────────┤
    │ status bar                   │
    └──────────────────────────────┘
The collapsible command/output panel and confirmation modals arrive in phase 2.
"""

from __future__ import annotations

import asyncio
import os
import time

from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Center, Middle, VerticalScroll
from textual.screen import Screen
from textual.theme import Theme
from textual.widgets import Collapsible, Input, LoadingIndicator, Markdown, Static
from textual.worker import Worker, WorkerState

from core.agent.loop import Conversation
from core.agent.ollama_client import OllamaClient, OllamaError
from core.config import Config, load_config, resolve_accent
from core.logo import load_logo

STATUS_REFRESH_SECONDS = 5


class SplashScreen(Screen[None]):
    """Shows the logo while checking that Ollama is up. Any key skips it."""

    def compose(self) -> ComposeResult:
        with Middle():
            with Center():
                yield Static(id="logo", markup=False)
            with Center():
                yield Static("connecting to Ollama…", id="splash-status")

    def on_mount(self) -> None:
        self.frames = load_logo()
        self.frame = 0
        self.query_one("#logo", Static).update(self.frames[0])
        if len(self.frames) > 1:
            self.set_interval(0.15, self._next_frame)
        self.set_timer(1.5, self._done)
        self._check_ollama()

    def _next_frame(self) -> None:
        self.frame = (self.frame + 1) % len(self.frames)
        self.query_one("#logo", Static).update(self.frames[self.frame])

    @work(exclusive=True)
    async def _check_ollama(self) -> None:
        status = self.query_one("#splash-status", Static)
        app = self.app
        assert isinstance(app, CoreApp)
        try:
            version = await app.client.version()
            status.update(f"Ollama {version} · {app.config.model.main}")
        except OllamaError:
            status.update("[b red]Ollama not reachable[/] (is ollama.service running?)")

    def on_key(self, event: events.Key) -> None:
        event.stop()
        self._done()

    def _done(self) -> None:
        if self.is_current:
            self.dismiss()


class StatusBar(Static):
    """Model state · VRAM · senses · local/remote."""

    def on_mount(self) -> None:
        self.update("…")
        self.refresh_status()
        self.set_interval(STATUS_REFRESH_SECONDS, self.refresh_status)

    @work(exclusive=True, group="status")
    async def refresh_status(self) -> None:
        app = self.app
        assert isinstance(app, CoreApp)
        model = app.config.model.main
        try:
            loaded = {m.name: m for m in await app.client.loaded_models()}
            m = loaded.get(model)
            if m is None:
                model_part = f"{model} (idle)"
            elif m.fully_on_gpu:
                model_part = f"{model} [green]●[/] GPU"
            else:
                # Part of the model spilled into system RAM: works, but much slower.
                pct = 100 * m.size_vram // max(m.size, 1)
                model_part = f"{model} [yellow]●[/] {pct}% GPU"
        except OllamaError:
            model_part = f"{model} [red]● Ollama down[/]"

        parts = [model_part, await vram_usage(), "senses: off", session_kind()]
        self.update(" │ ".join(parts))


async def vram_usage() -> str:
    """Read GPU memory use from nvidia-smi, e.g. 'VRAM 6.8/8.0 GB'."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi",
            "--query-gpu=memory.used,memory.total",
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
        used, total = (int(x) for x in out.decode().splitlines()[0].split(","))
    except (OSError, ValueError, IndexError, TimeoutError):
        return "VRAM ?"
    return f"VRAM {used / 1024:.1f}/{total / 1024:.1f} GB"


def session_kind() -> str:
    # SSH sets SSH_CONNECTION. Phase 3 (tmux started at boot) will need to ask
    # tmux which client is attached instead, since the env is fixed at start.
    return "remote" if os.environ.get("SSH_CONNECTION") else "local"


class ChatScreen(Screen[None]):
    BINDINGS = [
        Binding("ctrl+c", "cancel", "Stop reply", priority=True),
        Binding("escape", "cancel", "Stop reply", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="messages")
        yield Input(placeholder="Ask CORE…  (Ctrl+C stops a reply, Ctrl+Q quits)", id="prompt")
        yield StatusBar(id="status")

    def on_mount(self) -> None:
        self.query_one("#prompt", Input).focus()
        self.reply_worker: Worker[None] | None = None

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        if self.reply_worker and self.reply_worker.state == WorkerState.RUNNING:
            self.notify("Still replying. Press Ctrl+C to stop it first.")
            return
        event.input.clear()

        messages = self.query_one("#messages", VerticalScroll)
        await messages.mount(Static(text, classes="user", markup=False))
        self.reply_worker = self.stream_reply(text)

    @work(exclusive=True, group="reply")
    async def stream_reply(self, text: str) -> None:
        app = self.app
        assert isinstance(app, CoreApp)
        messages = self.query_one("#messages", VerticalScroll)

        # Animated dots until the first token arrives, so it never looks frozen.
        pending = LoadingIndicator(classes="pending")
        answer = Markdown(classes="assistant")
        await messages.mount(pending, answer)
        answer_stream = Markdown.get_stream(answer)
        messages.anchor()  # keep scrolled to the bottom while text arrives

        # Thinking goes in a one-line collapsed block, only made if the model thinks.
        thinking_stream = None
        box: Collapsible | None = None
        started = time.monotonic()

        try:
            async for chunk in app.conversation.send(text):
                if pending.is_mounted:
                    await pending.remove()
                if chunk.kind == "thinking":
                    if thinking_stream is None:
                        thinking_md = Markdown(classes="thinking")
                        box = Collapsible(thinking_md, title="thinking…", collapsed=True)
                        await messages.mount(box, before=answer)
                        thinking_stream = Markdown.get_stream(thinking_md)
                    await thinking_stream.write(chunk.text)
                else:
                    if box is not None and box.title == "thinking…":
                        box.title = f"thought for {time.monotonic() - started:.1f}s"
                    await answer_stream.write(chunk.text)
        except OllamaError as e:
            await answer_stream.write(f"\n\n**Error:** {e}")
        except asyncio.CancelledError:
            await answer_stream.write("\n\n*(stopped)*")
            raise
        finally:
            if pending.is_mounted:
                await pending.remove()
            await answer_stream.stop()
            if thinking_stream is not None:
                await thinking_stream.stop()

    def action_cancel(self) -> None:
        if self.reply_worker and self.reply_worker.state == WorkerState.RUNNING:
            self.reply_worker.cancel()


class CoreApp(App[None]):
    TITLE = "CORE"
    CSS = """
    #logo { text-style: bold; color: $primary; content-align: center middle; }
    #splash-status { width: auto; color: $text-muted; margin-top: 1; }
    #messages { padding: 0 1; }
    .user {
        margin: 1 0 0 0; padding: 0 1;
        border-left: thick $primary; background: $boost;
    }
    .assistant { margin: 1 0 0 0; }
    .pending { height: 1; width: 8; margin: 1 0 0 0; color: $primary; }
    /* Thinking: a quiet one-liner, not a big box. */
    Collapsible {
        margin: 1 0 0 0; padding: 0; border: none; background: transparent;
    }
    Collapsible CollapsibleTitle { padding: 0 1; color: $text-muted; text-style: italic; }
    Collapsible Contents { padding: 0 0 0 3; }
    .thinking { color: $text-muted; }
    #prompt { dock: bottom; margin-bottom: 1; }
    #status { dock: bottom; height: 1; padding: 0 1; background: $panel; color: $text-muted; }
    """

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self.client = OllamaClient(config.ollama.url)
        self.conversation = Conversation(
            self.client,
            config.model.main,
            num_ctx=config.model.num_ctx,
            think=config.model.think,
        )

    def on_mount(self) -> None:
        accent = resolve_accent(self.config.ui.accent)
        self.register_theme(Theme(name="core", primary=accent, dark=True))
        self.theme = "core"
        self.push_screen(ChatScreen())
        self.push_screen(SplashScreen())

    async def on_unmount(self) -> None:
        await self.client.aclose()


def main() -> None:
    CoreApp(load_config()).run()
