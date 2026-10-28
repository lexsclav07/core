"""The Textual app: logo splash, then the chat screen (core/chat.py).

The look lives in core/theme.tcss. Colours follow GNOME: the accent colour
from Settings → Appearance becomes $primary, and light/dark follows the
desktop style unless config.toml says otherwise.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from textual import events, work
from textual.app import App, ComposeResult, SystemCommand
from textual.screen import Screen
from textual.theme import Theme
from textual.widgets import Static

from core.agent.loop import Conversation
from core.agent.ollama_client import OllamaClient, OllamaError
from core.chat import ChatScreen
from core.config import Config, load_config, resolve_accent, resolve_dark
from core.logo import load_logo
from core.status import session_kind
from core.tools.senses import look_at_screen, look_at_webcam
from core.tools.shell import run_command


class SplashScreen(Screen[None]):
    """Shows the logo while checking that Ollama is up. Any key skips it."""

    def compose(self) -> ComposeResult:
        yield Static(id="logo", markup=False)
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
            status.update("[b $error]Ollama not reachable[/] (is ollama.service running?)")

    def on_key(self, event: events.Key) -> None:
        event.stop()
        self._done()

    def _done(self) -> None:
        if self.is_current:
            self.dismiss()


# GNOME's (libadwaita's) green, yellow and red for each mode. A Textual theme
# given only `primary` makes these from it too: a blue accent would make the
# red "a sense is on" dot blue, which is exactly what must stand out.
STATE_COLOURS = {  # success, warning, error
    True: ("#57e389", "#f6d32d", "#ff7b63"),  # dark
    False: ("#1b8553", "#9c6e03", "#c01c28"),  # light
}


class CoreApp(App[None]):
    TITLE = "CORE"
    CSS_PATH = "theme.tcss"

    def __init__(self, config: Config | None = None) -> None:
        super().__init__()
        # No config: read it (lets `textual run --dev core.app:CoreApp` work).
        self.config = config = config or load_config()
        self.active_senses: set[str] = set()  # shown in red in the status bar
        self.client = OllamaClient(config.ollama.url)
        self.conversation = Conversation(
            self.client,
            config.model.main,
            num_ctx=config.model.num_ctx,
            think=config.model.think,
            tools=[run_command, look_at_screen, look_at_webcam],
            cwd=str(Path.home()),
            session=session_kind(),
            vision_model=config.model.vision,
        )

    def on_mount(self) -> None:
        accent = resolve_accent(self.config.ui.accent)
        dark = resolve_dark(self.config.ui.mode)
        success, warning, error = STATE_COLOURS[dark]
        theme = Theme("core", accent, success=success, warning=warning, error=error, dark=dark)
        self.register_theme(theme)
        self.theme = "core"
        chat = ChatScreen()
        self.conversation.ui = chat
        self.push_screen(chat)
        self.push_screen(SplashScreen())

    def get_system_commands(self, screen: Screen[Any]) -> Iterable[SystemCommand]:
        """CORE's own entries in the Ctrl+P command palette, above Textual's."""
        if isinstance(screen, ChatScreen):
            yield SystemCommand("New chat", "Forget this chat and start afresh (Ctrl+L)",
                                screen.action_new_chat)  # fmt: skip
            yield SystemCommand("Hand signs", "Gesture mode on or off (Ctrl+G)",
                                screen.action_gestures)  # fmt: skip
            yield SystemCommand("Talk", "Push-to-talk: record, then press again (Ctrl+T)",
                                screen.action_talk)  # fmt: skip
            yield SystemCommand("Help", "Keys, approvals, and where CORE keeps things (F1)",
                                screen.action_help)  # fmt: skip
        yield from super().get_system_commands(screen)

    async def on_unmount(self) -> None:
        await self.client.aclose()


def main() -> None:
    CoreApp().run()
