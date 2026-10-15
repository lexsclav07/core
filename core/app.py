"""The Textual app: logo splash, then the chat screen with a status bar.

Layout of the chat screen:
    ┌──────────────────────────────┐
    │ messages (scrolls)           │
    ├──────────────────────────────┤
    │ > input                      │
    ├──────────────────────────────┤
    │ status bar                   │
    └──────────────────────────────┘
Commands appear inline in the chat as collapsible blocks (core/widgets.py);
confirm-tier commands pop up an approval dialog first.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from rich.markup import escape
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.theme import Theme
from textual.widgets import Collapsible, Input, LoadingIndicator, Markdown, Static
from textual.widgets.markdown import MarkdownStream
from textual.worker import Worker, WorkerState

from core.agent import audit
from core.agent.loop import Conversation
from core.agent.ollama_client import OllamaClient, OllamaError
from core.agent.risk import Assessment
from core.config import Config, load_config, resolve_accent
from core.logo import load_logo
from core.tools import gestures
from core.tools.mic import MicError, Recorder, transcribe
from core.tools.pty_runner import RunningCommand
from core.tools.senses import look_at_screen, look_at_webcam
from core.tools.shell import run_command
from core.tools.signs import label
from core.widgets import ApprovalModal, CommandBlock, PasswordModal

STATUS_REFRESH_SECONDS = 5


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
            status.update("[b red]Ollama not reachable[/] (is ollama.service running?)")

    def on_key(self, event: events.Key) -> None:
        event.stop()
        self._done()

    def _done(self) -> None:
        if self.is_current:
            self.dismiss()


class StatusBar(Static):
    """Model state · VRAM · senses · local/remote."""

    parts: tuple[str, ...] = ("…",)  # model and VRAM, refreshed every few seconds
    sign: str | None = None  # the hand sign gesture mode sees right now

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
        self.parts = (model_part, await vram_usage())
        self.redraw()

    def redraw(self) -> None:
        """Cheap: no queries, so senses and signs can update it many times a second."""
        app = self.app
        assert isinstance(app, CoreApp)
        senses = app.active_senses
        sense_part = f"[b red]● {' + '.join(sorted(senses))}[/]" if senses else "senses: off"
        if self.sign and "gestures" in senses:
            sense_part += f" {escape(label(self.sign))}"  # Lex's own sign names may hold [ ]
        self.update(" │ ".join([*self.parts, sense_part, session_kind()]))


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


PROMPT_IDLE = (
    "Ask CORE…  (Ctrl+T talk, Ctrl+G hand signs, Ctrl+C stop, Ctrl+O expand output, Ctrl+Q quit)"
)
PROMPT_RUNNING = "A command is running: type here to answer it, Ctrl+C stops it"
PROMPT_RECORDING = "● Recording… press Ctrl+T or Enter to stop"
PROMPT_TRANSCRIBING = "Transcribing…"


class ReplyBlock:
    """The widgets for one model reply: loading dots, thinking line, answer.

    A turn has one of these per round; after the model runs commands, the
    next reply goes in a fresh block below the command output.
    """

    def __init__(self, messages: VerticalScroll) -> None:
        self.messages = messages
        self.pending: LoadingIndicator | None = None
        self.answer: Markdown | None = None
        self.answer_stream: MarkdownStream | None = None
        self.thinking_box: Collapsible | None = None
        self.thinking_stream: MarkdownStream | None = None
        self.started = time.monotonic()

    async def start(self) -> None:
        # Animated dots until something arrives, so it never looks frozen.
        self.pending = LoadingIndicator(classes="pending")
        await self.messages.mount(self.pending)

    def drop_pending(self) -> None:
        if self.pending is not None and self.pending.is_mounted:
            self.pending.remove()
        self.pending = None

    async def thinking(self, text: str) -> None:
        if self.thinking_stream is None:
            md = Markdown(classes="thinking")
            self.thinking_box = Collapsible(md, title="thinking…", collapsed=True)
            await self.messages.mount(self.thinking_box)
            self.thinking_stream = Markdown.get_stream(md)
        await self.thinking_stream.write(text)

    def done_thinking(self) -> None:
        if self.thinking_box is not None and self.thinking_box.title == "thinking…":
            self.thinking_box.title = f"thought for {time.monotonic() - self.started:.1f}s"

    async def content(self, text: str) -> None:
        self.done_thinking()
        if self.answer_stream is None:
            self.answer = Markdown(classes="assistant")
            await self.messages.mount(self.answer)
            self.answer_stream = Markdown.get_stream(self.answer)
        await self.answer_stream.write(text)

    async def retract(self) -> None:
        """Take this reply off the screen (the model is about to redo it)."""
        await self.close()
        for widget in (self.answer, self.thinking_box):
            if widget is not None and widget.is_mounted:
                await widget.remove()

    async def close(self) -> None:
        self.drop_pending()
        self.done_thinking()
        for stream in (self.answer_stream, self.thinking_stream):
            if stream is not None:
                await stream.stop()


class ChatScreen(Screen[None]):
    """The main screen. It is also the AgentUI the tools talk to."""

    BINDINGS = [
        Binding("ctrl+c", "cancel", "Stop", priority=True),
        Binding("escape", "cancel", "Stop", show=False),
        Binding("ctrl+o", "toggle_output", "Expand output"),
        Binding("ctrl+t", "talk", "Talk", priority=True),
        Binding("ctrl+g", "gestures", "Hand signs", priority=True),
    ]

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="messages")
        yield Input(placeholder=PROMPT_IDLE, id="prompt")
        yield StatusBar(id="status")

    def on_mount(self) -> None:
        self.query_one("#prompt", Input).focus()
        self.reply_worker: Worker[None] | None = None
        self.reply: ReplyBlock | None = None
        self.last_block: CommandBlock | None = None
        self.recorder: Recorder | None = None
        self.gesture_worker: Worker[None] | None = None

    @property
    def running(self) -> RunningCommand | None:
        return self.last_block.running if self.last_block else None

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if self.recorder is not None:
            await self.action_talk()  # Enter also stops recording
            return
        if self.running is not None:
            # Answer the command's prompt (e.g. "[y/N]"); the model doesn't see this.
            self.running.send_line(event.value)
            event.input.clear()
            return
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
        messages.anchor()  # keep scrolled to the bottom while text arrives
        reply = self.reply = ReplyBlock(messages)
        await reply.start()
        try:
            async for chunk in app.conversation.send(text):
                if chunk.kind in ("break", "retract"):
                    await (reply.close() if chunk.kind == "break" else reply.retract())
                    reply = self.reply = ReplyBlock(messages)
                    await reply.start()
                    continue
                reply.drop_pending()
                if chunk.kind == "thinking":
                    await reply.thinking(chunk.text)
                else:
                    await reply.content(chunk.text)
        except OllamaError as e:
            await reply.content(f"\n\n**Error:** {e}")
        except asyncio.CancelledError:
            await reply.content("\n\n*(stopped)*")
            raise
        finally:
            await reply.close()
            self.reply = None

    def action_cancel(self) -> None:
        # Like a terminal: Ctrl+C stops the running command first. CORE's
        # model is told, and can react. Pressing it again stops the reply.
        if self.running is not None:
            self.running.kill()
        elif self.reply_worker and self.reply_worker.state == WorkerState.RUNNING:
            self.reply_worker.cancel()

    async def action_talk(self) -> None:
        """Push-to-talk: first press starts recording, second press stops it."""
        prompt = self.query_one("#prompt", Input)
        if self.recorder is None:
            recorder = Recorder()
            try:
                await recorder.start()
            except MicError as e:
                recorder.discard()
                self.notify(f"Can't record: {e}.", severity="warning")
                return
            self.recorder = recorder
            self.sense("mic", True)
            prompt.placeholder = PROMPT_RECORDING
            if session_kind() == "remote":
                self.notify("Recording from the PC's microphone, not your laptop's.")
            return
        recorder, self.recorder = self.recorder, None
        self.sense("mic", False)
        self._transcribe(recorder)

    @work(exclusive=True, group="mic")
    async def _transcribe(self, recorder: Recorder) -> None:
        app = self.app
        assert isinstance(app, CoreApp)
        prompt = self.query_one("#prompt", Input)
        prompt.placeholder = PROMPT_TRANSCRIBING
        try:
            text = await transcribe(await recorder.stop(), app.config.model.whisper)
        except MicError as e:
            self.notify(f"Didn't catch that: {e}.", severity="warning")
            text = ""
        finally:
            recorder.discard()  # the audio is never kept
            prompt.placeholder = PROMPT_IDLE
            entry = audit.AuditEntry("🎤 push-to-talk recording", "CONFIRM",
                                     "Lex pressed the talk key", "lex", session_kind())  # fmt: skip
            audit.record(entry)
        # Put it in the box so Lex can check it before sending.
        prompt.value = (prompt.value + " " + text).strip()
        prompt.cursor_position = len(prompt.value)
        prompt.focus()

    def action_gestures(self) -> None:
        """Gesture mode on/off. Like the mic, only Lex can turn it on."""
        worker = self.gesture_worker
        if worker is not None and worker.state in (WorkerState.PENDING, WorkerState.RUNNING):
            worker.cancel()
            self.notify("Gesture mode off.")
            return
        self.gesture_worker = self._run_gestures()

    @work(exclusive=True, group="gestures")
    async def _run_gestures(self) -> None:
        try:
            # Read the file again, so edited macros apply without restarting CORE.
            config = load_config()
        except (OSError, ValueError) as e:  # TOMLDecodeError is a ValueError
            self.gesture_note(f"Can't read config.toml: {e}", warning=True)
            return
        try:
            await gestures.run(
                self, config.gestures, terminal=config.ui.terminal, session=session_kind()
            )
        except Exception as e:  # e.g. hold = "fast" in config: don't take all of CORE down
            self.gesture_note(f"Gesture mode stopped after an error: {e!r}", warning=True)

    def action_toggle_output(self) -> None:
        if self.last_block is not None:
            self.last_block.toggle()

    def on_command_block_changed(self, event: CommandBlock.Changed) -> None:
        if event.block is not self.last_block:
            return  # e.g. a gesture macro; it never takes typed input
        prompt = self.query_one("#prompt", Input)
        prompt.placeholder = PROMPT_RUNNING if self.running else PROMPT_IDLE

    # AgentUI (see core/tools/registry.py) ---------------------------------

    async def approve(
        self, command: str, assessment: Assessment, explanation: str, undo: str
    ) -> bool:
        if self.reply:
            self.reply.drop_pending()
        remote = session_kind() == "remote"
        modal = ApprovalModal(command, assessment, explanation, undo, remote)
        return bool(await self._wait_for(modal))

    async def ask_password(self) -> str | None:
        result = await self._wait_for(PasswordModal())
        return result if isinstance(result, str) else None

    async def command_view(self, command: str, assessment: Assessment) -> CommandBlock:
        if self.reply:
            self.reply.drop_pending()
            self.reply.done_thinking()
        block = self.last_block = CommandBlock(command, assessment)
        await self.query_one("#messages", VerticalScroll).mount(block)
        return block

    def sense(self, name: str, active: bool) -> None:
        app = self.app
        assert isinstance(app, CoreApp)
        if active:
            app.active_senses.add(name)
        else:
            app.active_senses.discard(name)
        self.query_one(StatusBar).redraw()

    # GestureUI (see core/tools/gestures.py) -------------------------------

    def show_sign(self, sign: str | None) -> None:
        status = self.query_one(StatusBar)
        status.sign = sign
        status.redraw()

    def gesture_note(self, text: str, warning: bool = False) -> None:
        if warning:
            self.notify(text, severity="warning", timeout=8, markup=False)
        else:
            self.notify(text, timeout=4, markup=False)

    async def macro_view(
        self, sign: str, command: str, assessment: Assessment, approved: bool
    ) -> CommandBlock:
        # Not self.last_block: typing and Ctrl+C keep going to the model's command.
        block = CommandBlock(command, assessment, trigger=sign, approved=approved)
        messages = self.query_one("#messages", VerticalScroll)
        await messages.mount(block)
        messages.scroll_end(animate=False)
        return block

    async def _wait_for(self, modal: ModalScreen[Any]) -> Any:
        """Show a dialog and wait for its answer; close it if the reply is cancelled."""
        try:
            return await self.app.push_screen_wait(modal)
        except asyncio.CancelledError:
            if modal.is_current:
                self.app.pop_screen()
            raise


class CoreApp(App[None]):
    TITLE = "CORE"
    CSS = """
    SplashScreen { align: center middle; }
    #logo { width: 100%; content-align: center middle; text-style: bold; color: $primary; }
    #splash-status { width: 100%; content-align: center middle; color: $text-muted; margin-top: 1; }
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

    /* Commands, inline in the chat */
    .command { height: auto; margin: 1 0 0 0; padding: 0 1; background: $boost; }
    .command-output { color: $text-muted; padding: 0 0 0 2; }

    /* Approval and password dialogs */
    ApprovalModal, PasswordModal { align: center middle; }
    #approval, #password {
        width: 80%; max-width: 100; height: auto; padding: 1 2;
        border: thick $primary; background: $surface;
    }
    #approval Static { margin-bottom: 1; }
    .modal-title { text-style: bold; color: $primary; }
    .modal-command { padding: 0 1; background: $boost; text-style: bold; }
    .modal-buttons { height: auto; align-horizontal: right; }
    .modal-buttons Button { margin-left: 2; }
    .note { color: $text-muted; }
    """

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
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
        self.register_theme(Theme(name="core", primary=accent, dark=True))
        self.theme = "core"
        chat = ChatScreen()
        self.conversation.ui = chat
        self.push_screen(chat)
        self.push_screen(SplashScreen())

    async def on_unmount(self) -> None:
        await self.client.aclose()


def main() -> None:
    CoreApp(load_config()).run()
