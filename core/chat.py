"""The chat screen: status bar on top, the conversation, the input box, the keys.

    ┌─────────────────────────────────────────────────┐
    │ ◆ CORE  ● qwen3:8b  VRAM ━━━━──  ctx ━──  …     │  StatusBar (status.py)
    │ you · 21:04                       ╭ hand signs ╮│
    │ ▌ what kernel am I on?            │   ⡇ ⢠⠃     ││  HandView, only in
    │ ◆ CORE                            │ ✌ peace ━━ ││  gesture mode
    │ $ uname -r  ✓                     ╰────────────╯│  (handview.py)
    │ Your kernel is 6.18.                            │
    │ 2.6s · 74 tokens · 37 tok/s                     │
    │ ▏Ask CORE anything…                             │  Input
    │ ^t Talk  ^g Hand signs  ^l New chat  F1 Help    │  Footer: only keys that
    └─────────────────────────────────────────────────┘  do something right now

The screen is also the AgentUI the tools talk to (approvals, command blocks,
senses; see tools/registry.py) and the GestureUI for gesture mode.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Collapsible, Footer, Input, LoadingIndicator, Markdown, Static
from textual.widgets.markdown import MarkdownStream
from textual.worker import Worker, WorkerState

from core.agent import audit
from core.agent.ollama_client import OllamaError
from core.agent.risk import Assessment
from core.config import load_config
from core.handview import HandView
from core.logo import load_logo
from core.status import Snapshot, render, session_kind, vram_usage
from core.tools import gestures
from core.tools.mic import MicError, Recorder, transcribe
from core.tools.pty_runner import RunningCommand
from core.welcome import SUGGESTIONS, HelpScreen, Welcome
from core.widgets import ApprovalModal, CommandBlock, PasswordModal

if TYPE_CHECKING:
    from core.app import CoreApp  # only for types: core.app imports this module

STATUS_REFRESH_SECONDS = 5
HISTORY_SIZE = 100  # messages ↑ can bring back
PROMPT_IDLE = "Ask CORE anything…"
PROMPT_REPLYING = "CORE is replying… Ctrl+C stops it"
PROMPT_RUNNING = "A command is running: type here to answer it, Ctrl+C stops it"
PROMPT_RECORDING = "● Recording… press Ctrl+T or Enter to stop"
PROMPT_TRANSCRIBING = "Transcribing…"


def turn_summary(seconds: float, tokens: int, writing_s: float) -> str:
    """The quiet line under a reply, e.g. '2.6s · 74 tokens · 37 tok/s'."""
    parts = [f"{seconds:.1f}s"]
    if tokens and writing_s > 0:
        parts += [f"{tokens} tokens", f"{tokens / writing_s:.0f} tok/s"]
    return " · ".join(parts)


class StatusBar(Static):
    """The bar along the top. What it shows is drawn by core/status.py."""

    def __init__(self) -> None:
        super().__init__(id="status")
        self.snap = Snapshot()

    def on_mount(self) -> None:
        app = cast("CoreApp", self.app)
        self.snap.model = app.config.model.main
        self.snap.session = session_kind()
        self.snap.context = (app.conversation.context_used(), app.config.model.num_ctx)
        self.redraw()
        self.refresh_status()
        self.set_interval(STATUS_REFRESH_SECONDS, self.refresh_status)

    @work(exclusive=True, group="status")
    async def refresh_status(self) -> None:
        app = cast("CoreApp", self.app)
        try:
            loaded = {m.name: m for m in await app.client.loaded_models()}
        except OllamaError:
            self.snap.model_state = "down"
        else:
            m = loaded.get(self.snap.model)
            if m is None:
                self.snap.model_state = "idle"
            elif m.fully_on_gpu:
                self.snap.model_state = "gpu"
            else:
                # Part of the model spilled into system RAM: works, but much slower.
                self.snap.model_state = "split"
                self.snap.gpu_percent = 100 * m.size_vram // max(m.size, 1)
        self.snap.vram_mb = await vram_usage()
        self.redraw()

    def redraw(self) -> None:
        """Cheap (no queries), so senses and signs can call it many times a second."""
        self.snap.senses = set(cast("CoreApp", self.app).active_senses)
        self.update(render(self.snap, self.size.width or 120))

    def on_resize(self) -> None:
        self.redraw()


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
    BINDINGS = [
        Binding("ctrl+c", "cancel", "Stop", priority=True),
        Binding("escape", "cancel", "Stop", show=False),
        Binding("ctrl+t", "talk", "Talk", priority=True),
        Binding("ctrl+g", "gestures", "Hand signs", priority=True),
        Binding("ctrl+l", "new_chat", "New chat", priority=True),
        Binding("ctrl+o", "toggle_output", "Output"),
        Binding("f1", "help", "Help"),
        Binding("up", "history(-1)", show=False),
        Binding("down", "history(1)", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.reply_worker: Worker[None] | None = None
        self.reply: ReplyBlock | None = None
        self.last_block: CommandBlock | None = None
        self.recorder: Recorder | None = None
        self.transcribing = False
        self.gesture_worker: Worker[None] | None = None
        self.sent: list[str] = []  # for ↑/↓
        self.history_at = 0
        self.draft = ""  # what was typed before ↑

    def compose(self) -> ComposeResult:
        # Docked widgets take their edge in this order, so the footer ends up
        # below the input and the hand view only takes space beside the chat.
        yield StatusBar()
        yield Input(placeholder=PROMPT_IDLE, id="prompt")
        yield Footer()
        yield HandView(id="hand")
        yield VerticalScroll(Welcome(load_logo()[0]), id="messages")

    def on_mount(self) -> None:
        self.prompt.focus()

    @property
    def core(self) -> CoreApp:
        return cast("CoreApp", self.app)

    @property
    def prompt(self) -> Input:
        return self.query_one("#prompt", Input)

    @property
    def messages(self) -> VerticalScroll:
        return self.query_one("#messages", VerticalScroll)

    @property
    def running(self) -> RunningCommand | None:
        return self.last_block.running if self.last_block else None

    @property
    def replying(self) -> bool:
        return self.reply_worker is not None and self.reply_worker.state == WorkerState.RUNNING

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Keep the footer honest: hide keys that would do nothing right now."""
        if action == "cancel":
            return self.running is not None or self.replying
        if action == "toggle_output":
            return self.last_block is not None
        return True

    def _changed(self) -> None:
        """Something started or stopped: update the input hint and the footer."""
        self.prompt.placeholder = (
            PROMPT_RECORDING if self.recorder
            else PROMPT_TRANSCRIBING if self.transcribing
            else PROMPT_RUNNING if self.running
            else PROMPT_REPLYING if self.replying
            else PROMPT_IDLE
        )  # fmt: skip
        self.refresh_bindings()

    def _update_context(self) -> None:
        status = self.query_one(StatusBar)
        conversation = self.core.conversation
        status.snap.context = (conversation.context_used(), conversation.num_ctx)
        status.redraw()

    # Sending -------------------------------------------------------------

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
        if self.replying:
            self.notify("Still replying. Press Ctrl+C to stop it first.")
            return
        event.input.clear()
        self._remember(text)
        await self.query(Welcome).remove()
        who = f"[b $primary]you[/] [$text-muted]· {datetime.now():%H:%M}[/]"
        await self.messages.mount(
            Static(who, classes="who"), Static(text, classes="user", markup=False)
        )
        self.reply_worker = self.stream_reply(text)

    @work(exclusive=True, group="reply")
    async def stream_reply(self, text: str) -> None:
        messages = self.messages
        messages.anchor()  # keep scrolled to the bottom while text arrives
        await messages.mount(Static("[b $accent]◆ CORE[/]", classes="who"))
        reply = self.reply = ReplyBlock(messages)
        await reply.start()
        started, tokens, writing_s = time.monotonic(), 0, 0.0
        try:
            async for chunk in self.core.conversation.send(text):
                if chunk.kind == "stats" and chunk.stats:
                    tokens += chunk.stats.reply_tokens
                    writing_s += chunk.stats.reply_seconds
                    continue
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
            summary = turn_summary(time.monotonic() - started, tokens, writing_s)
            await messages.mount(Static(summary, classes="reply-meta"))
            self._update_context()

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker is self.reply_worker:
            self._changed()  # the reply started or finished

    def _remember(self, text: str) -> None:
        if not self.sent or self.sent[-1] != text:
            self.sent.append(text)
            del self.sent[:-HISTORY_SIZE]
        self.history_at, self.draft = len(self.sent), ""

    def action_history(self, step: int) -> None:
        """↑/↓ bring back earlier messages, like in a shell."""
        if not self.sent or self.running is not None or self.recorder is not None:
            return
        if self.history_at == len(self.sent):
            self.draft = self.prompt.value
        self.history_at = max(0, min(len(self.sent), self.history_at + step))
        at_end = self.history_at == len(self.sent)
        self.prompt.value = self.draft if at_end else self.sent[self.history_at]
        self.prompt.cursor_position = len(self.prompt.value)

    def action_suggest(self, index: int) -> None:
        """A suggestion on the welcome panel was clicked: put it in the box."""
        self.prompt.value = SUGGESTIONS[index]
        self.prompt.cursor_position = len(self.prompt.value)
        self.prompt.focus()

    # Keys ----------------------------------------------------------------

    def action_cancel(self) -> None:
        # Like a terminal: Ctrl+C stops the running command first. CORE's
        # model is told, and can react. Pressing it again stops the reply.
        if self.running is not None:
            self.running.kill()
        elif self.reply_worker and self.reply_worker.state == WorkerState.RUNNING:
            self.reply_worker.cancel()

    async def action_new_chat(self) -> None:
        """Ctrl+L: forget this conversation. Frees the context window."""
        if self.running is not None or self.replying:
            self.notify("CORE is still working on the last message. Ctrl+C stops it first.")
            return
        self.core.conversation.reset()
        self.last_block = None
        self.messages.anchor(False)  # back to the top, where the welcome panel goes
        await self.messages.remove_children()
        await self.messages.mount(Welcome(load_logo()[0]))
        self._update_context()
        self._changed()
        self.notify("New chat. CORE has forgotten the last one.")

    def action_help(self) -> None:
        self.app.push_screen(HelpScreen())

    def action_toggle_output(self) -> None:
        if self.last_block is not None:
            self.last_block.toggle()

    async def action_talk(self) -> None:
        """Push-to-talk: first press starts recording, second press stops it."""
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
            self._changed()
            if session_kind() == "remote":
                self.notify("Recording from the PC's microphone, not your laptop's.")
            return
        recorder, self.recorder = self.recorder, None
        self.sense("mic", False)
        self._transcribe(recorder)

    @work(exclusive=True, group="mic")
    async def _transcribe(self, recorder: Recorder) -> None:
        self.transcribing = True
        self._changed()
        try:
            text = await transcribe(await recorder.stop(), self.core.config.model.whisper)
        except MicError as e:
            self.notify(f"Didn't catch that: {e}.", severity="warning")
            text = ""
        finally:
            recorder.discard()  # the audio is never kept
            self.transcribing = False
            self._changed()
            entry = audit.AuditEntry("🎤 push-to-talk recording", "CONFIRM",
                                     "Lex pressed the talk key", "lex", session_kind())  # fmt: skip
            audit.record(entry)
        # Put it in the box so Lex can check it before sending.
        self.prompt.value = (self.prompt.value + " " + text).strip()
        self.prompt.cursor_position = len(self.prompt.value)
        self.prompt.focus()

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

    def on_command_block_changed(self, event: CommandBlock.Changed) -> None:
        if event.block is self.last_block:  # not a gesture macro: those take no input
            self._changed()

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
        await self.messages.mount(block)
        return block

    def sense(self, name: str, active: bool) -> None:
        senses = self.core.active_senses
        if active:
            senses.add(name)
        else:
            senses.discard(name)
        if name == "gestures":
            self.query_one(HandView).set_class(active, "-on")
        self.query_one(StatusBar).redraw()

    # GestureUI (see core/tools/gestures.py) -------------------------------

    def show_sign(self, sign: str | None) -> None:
        status = self.query_one(StatusBar)
        status.snap.sign = sign
        status.redraw()

    def show_hand(self, hand: list[list[float]] | None, sign: str | None, progress: float) -> None:
        self.query_one(HandView).show(hand, sign, progress)

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
        await self.query(Welcome).remove()
        await self.messages.mount(block)
        self.messages.scroll_end(animate=False)
        return block

    async def _wait_for(self, modal: ModalScreen[Any]) -> Any:
        """Show a dialog and wait for its answer; close it if the reply is cancelled."""
        try:
            return await self.app.push_screen_wait(modal)
        except asyncio.CancelledError:
            if modal.is_current:
                self.app.pop_screen()
            raise
