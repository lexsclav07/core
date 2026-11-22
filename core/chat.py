"""The main screen, laid out like Claude Code's terminal UI:

    ╭─── CORE v0.1.0 ─────────────────────────────────╮
    │  Good evening, Lex.  │ Tips for getting started  │   Welcome (welcome.py),
    ╰─────────────────────────────────────────────────╯   until the first message
    > what kernel am I on?                                 your message
    ⏺ Bash(uname -r)                                       a command (widgets.py)
      ⎿  6.18.44
    ⏺ Your kernel is 6.18.44.                              CORE's reply
                                         ╭ hand signs ╮    HandView, in gesture
    ✻ Tinkering… (3s · ↓ 40 tokens · esc to interrupt)     mode (handview.py)
    ╭─────────────────────────────────────────────────╮    Spinner (spinner.py)
    │ > ▏                                              │    the input; type / for
    ╰─────────────────────────────────────────────────╯    commands (commands.py)
    ? for shortcuts            ● qwen3:8b · 1.6k/8k ctx    StatusLine (status.py)

History, Activity and Settings (panels/) are full views of their own: /resume,
/activity and /settings (or F3–F5) open them and Esc comes back. They're
tabs underneath, with the tab bar hidden.

The screen is also the AgentUI the tools talk to (approvals, command blocks,
senses; see tools/registry.py) and the GestureUI for gesture mode. Approvals
pop up at the bottom over whichever view is open.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from typing import TYPE_CHECKING, Any, cast

from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Collapsible, Input, Markdown, Select, Static, TabbedContent, TabPane
from textual.widgets.markdown import MarkdownStream
from textual.worker import Worker, WorkerState

from core.agent import audit
from core.agent.ollama_client import OllamaError
from core.agent.risk import Assessment, Tier
from core.commands import CommandMenu, command_for
from core.config import load_config
from core.handview import HandView
from core.history import ChatSummary, from_lex, outcome, replay, title_for
from core.logo import load_small_logo
from core.panels.activity import ActivityPanel
from core.panels.history import HistoryPanel
from core.panels.settings import SettingsPanel
from core.spinner import Spinner
from core.status import Snapshot, render, session_kind, vram_usage
from core.tools import gestures
from core.tools.mic import MicError, Recorder, transcribe
from core.tools.pty_runner import RunningCommand
from core.tools.senses import notify_desktop
from core.welcome import SUGGESTIONS, HelpScreen, Welcome
from core.widgets import ApprovalModal, CommandBlock, PasswordModal

if TYPE_CHECKING:
    from core.app import CoreApp  # only for types: core.app imports this module

STATUS_REFRESH_SECONDS = 5
HISTORY_SIZE = 100  # messages ↑ can bring back
PROMPT_IDLE = 'Try "how much free disk space do I have?"'
PROMPT_REPLYING = "CORE is working: esc to interrupt"
PROMPT_RUNNING = "A command is running: type here to answer it"
PROMPT_RECORDING = "Recording… ctrl+t or enter to stop"
PROMPT_TRANSCRIBING = "Transcribing…"
HINT_IDLE = "? for shortcuts · / for commands"
LONG_REPLY_S = 10  # a reply this slow gets a desktop notification if Lex looked away
# The views other than the chat, with the title line each one shows.
VIEWS = {
    "history": "[b $primary]Resume a chat[/]  [$text-muted]enter opens · delete removes[/]",
    "activity": "[b $primary]Activity[/]  [$text-muted]what CORE ran, asked about or refused[/]",
    "settings": "[b $primary]Settings[/]  [$text-muted]saved to config.toml as you change them[/]",
}
FROM_HISTORY = Assessment(Tier.SAFE, "from a saved chat")  # for blocks put back on screen


def turn_summary(seconds: float, tokens: int, writing_s: float) -> str:
    """How the last reply went, for the line under the input: '2.6s · 37 tok/s'."""
    parts = [f"{seconds:.1f}s"]
    if tokens and writing_s > 0:
        parts.append(f"{tokens / writing_s:.0f} tok/s")
    return " · ".join(parts)


def you_said(text: str) -> Horizontal:
    """Lex's message in the chat: `> text`, quiet, like Claude Code shows it."""
    return Horizontal(Static("> ", classes="mark"), Static(text, classes="said", markup=False),
                      classes="user")  # fmt: skip


class StatusLine(Horizontal):
    """The line under the input: hints on the left, status.py's line on the right."""

    def __init__(self) -> None:
        super().__init__(id="status")
        self.snap = Snapshot()
        self.hint = HINT_IDLE

    def compose(self) -> ComposeResult:
        yield Static(self.hint, id="status-hint")
        yield Static(id="status-info")

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

    def set_hint(self, hint: str) -> None:
        self.hint = hint
        self.query_one("#status-hint", Static).update(hint)

    def redraw(self) -> None:
        """Cheap (no queries), so senses and signs can call it many times a second."""
        self.snap.senses = set(cast("CoreApp", self.app).active_senses)
        self.query_one("#status-info", Static).update(render(self.snap, self.size.width or 120))

    def on_resize(self) -> None:
        self.redraw()


class ReplyBlock:
    """One model reply: an optional thinking line, then the answer after a ⏺.

    A turn has one of these per round; after the model runs commands, the
    next reply goes in a fresh block below the command output.
    """

    def __init__(self, messages: VerticalScroll) -> None:
        self.messages = messages
        self.answer: Horizontal | None = None
        self.answer_stream: MarkdownStream | None = None
        self.thinking_box: Collapsible | None = None
        self.thinking_stream: MarkdownStream | None = None
        self.started = time.monotonic()

    async def thinking(self, text: str) -> None:
        if self.thinking_stream is None:
            md = Markdown(classes="thinking")
            self.thinking_box = Collapsible(md, title="✻ Thinking…", collapsed=True)
            await self.messages.mount(self.thinking_box)
            self.thinking_stream = Markdown.get_stream(md)
        await self.thinking_stream.write(text)

    def done_thinking(self) -> None:
        if self.thinking_box is not None and self.thinking_box.title == "✻ Thinking…":
            self.thinking_box.title = f"✻ Thought for {time.monotonic() - self.started:.1f}s"

    async def content(self, text: str) -> None:
        self.done_thinking()
        if self.answer_stream is None:
            md = Markdown(classes="assistant")
            self.answer = Horizontal(Static("⏺", classes="bullet"), md, classes="reply")
            await self.messages.mount(self.answer)
            self.answer_stream = Markdown.get_stream(md)
        await self.answer_stream.write(text)

    async def retract(self) -> None:
        """Take this reply off the screen (the model is about to redo it)."""
        await self.close()
        for widget in (self.answer, self.thinking_box):
            if widget is not None and widget.is_mounted:
                await widget.remove()

    async def close(self) -> None:
        self.done_thinking()
        for stream in (self.answer_stream, self.thinking_stream):
            if stream is not None:
                await stream.stop()


class ChatScreen(Screen[None]):
    BINDINGS = [
        Binding("ctrl+c", "cancel", "Stop", priority=True),
        Binding("escape", "escape", "Back", show=False),
        Binding("ctrl+t", "talk", "Talk", priority=True),
        Binding("ctrl+g", "gestures", "Hand signs", priority=True),
        Binding("ctrl+l", "new_chat", "New chat", priority=True),
        Binding("ctrl+o", "toggle_output", "Output"),
        Binding("f1", "help", "Shortcuts"),
        Binding("f2", "show_tab('chat')", "Chat", show=False),
        Binding("f3", "show_tab('history')", "History", show=False),
        Binding("f4", "show_tab('activity')", "Activity", show=False),
        Binding("f5", "show_tab('settings')", "Settings", show=False),
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
        self.chat_id: int | None = None  # this chat's row in History
        self.last_turn = ""  # e.g. "2.6s · 37 tok/s", shown while idle
        self.sent: list[str] = []  # for ↑/↓
        self.history_at = 0
        self.draft = ""  # what was typed before ↑

    def compose(self) -> ComposeResult:
        yield StatusLine()
        with TabbedContent(id="tabs", initial="tab-chat"):
            with TabPane("Chat", id="tab-chat"):
                # One docked box for everything under the chat: widgets docked
                # to the same edge would overlap.
                with Vertical(id="composer"):
                    yield Spinner()
                    with Horizontal(id="prompt-box"):
                        yield Static(">", id="prompt-mark")
                        yield Input(placeholder=PROMPT_IDLE, id="prompt")
                    yield CommandMenu(id="menu")
                yield HandView(id="hand")
                yield VerticalScroll(self._welcome(), id="messages")
            for view, title in VIEWS.items():
                with TabPane(view.title(), id=f"tab-{view}"):
                    yield Static(f"{title} [$text-muted]· esc to go back[/]", classes="view-title")
                    if view == "history":
                        yield HistoryPanel()
                    elif view == "activity":
                        yield ActivityPanel()
                    else:
                        yield SettingsPanel()

    def _welcome(self) -> Welcome:
        # Commands run in the home folder, so that's CORE's "cwd".
        about = f"{self.core.config.model.main} · {session_kind()} · ~"
        try:
            recent: list[ChatSummary] = self.core.store.chats(limit=3)
        except sqlite3.Error:
            recent = []
        return Welcome(load_small_logo(), about, recent)

    def on_mount(self) -> None:
        self.query_one(CommandMenu).display = False
        self.prompt.focus()

    def on_resize(self, event: events.Resize) -> None:
        for welcome in self.query(Welcome):
            welcome.fit(event.size.width, event.size.height)

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
    def menu(self) -> CommandMenu:
        return self.query_one(CommandMenu)

    @property
    def status(self) -> StatusLine:
        return self.query_one(StatusLine)

    @property
    def running(self) -> RunningCommand | None:
        return self.last_block.running if self.last_block else None

    @property
    def replying(self) -> bool:
        return self.reply_worker is not None and self.reply_worker.state == WorkerState.RUNNING

    @property
    def tab(self) -> str:
        return (self.query_one(TabbedContent).active or "tab-chat").removeprefix("tab-")

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Keys that would do nothing right now don't fire (and aren't offered)."""
        if action == "cancel":
            return self.running is not None or self.replying
        if action == "escape":
            busy = self.running is not None or self.replying
            return busy or self.tab != "chat" or bool(self.menu.display)
        if action == "toggle_output":
            return self.last_block is not None and self.tab == "chat"
        return True

    # Views ---------------------------------------------------------------

    def action_show_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = f"tab-{tab}"

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        tab = (event.pane.id or "").removeprefix("tab-")
        # Focus has to move into the new view: TabbedContent switches to
        # whichever tab holds the focused widget, so the input would pull it back.
        if tab == "chat":
            self.prompt.focus()
        elif tab == "history":
            panel = self.query_one(HistoryPanel)
            panel.reload()
            panel.query_one("#history-search").focus()
        elif tab == "activity":
            activity = self.query_one(ActivityPanel)
            activity.reload()
            activity.query_one("#activity-table").focus()
        elif tab == "settings":
            settings = self.query_one(SettingsPanel)
            settings.refresh_models()
            settings.focus()
        self._changed()

    def action_escape(self) -> None:
        """Esc: close the / menu, stop what's running, or go back to the chat."""
        if self.menu.display:
            self.prompt.value = ""
        elif self.running is not None or self.replying:
            self.action_cancel()
        else:
            self.action_show_tab("chat")

    def action_pick_model(self) -> None:
        """/model: the Settings view with the model list open."""
        self.action_show_tab("settings")
        picker = self.query_one("#set-model", Select)
        picker.focus()
        picker.expanded = True

    def _changed(self) -> None:
        """Something started or stopped: update the input, the hint and the spinner."""
        self.prompt.placeholder = (
            PROMPT_RECORDING if self.recorder
            else PROMPT_TRANSCRIBING if self.transcribing
            else PROMPT_RUNNING if self.running
            else PROMPT_REPLYING if self.replying
            else PROMPT_IDLE
        )  # fmt: skip
        if self.recorder:
            hint = "[b $error]● recording[/] · ctrl+t or enter to stop"
        elif self.transcribing:
            hint = "transcribing…"
        elif self.tab != "chat":
            hint = "esc to go back"
        elif self.running:
            hint = "type to answer the command · ctrl+c stops it"
        elif self.replying:
            hint = "esc to interrupt"
        else:
            room = self.size.width >= 100  # on a narrow screen the right side needs it
            hint = HINT_IDLE + (
                f" · last reply {self.last_turn}" if self.last_turn and room else ""
            )
        self.status.set_hint(hint)
        self.refresh_bindings()

    def _update_context(self) -> None:
        status = self.status
        conversation = self.core.conversation
        status.snap.context = (conversation.context_used(), conversation.num_ctx)
        status.redraw()

    # Typing and sending --------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input is not self.prompt or self.running is not None:
            return
        if event.value == "?":  # like Claude Code: ? in an empty box shows the shortcuts
            self.prompt.value = ""
            self.action_help()
            return
        self.menu.show_for(event.value)
        self.refresh_bindings()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input is not self.prompt:
            return
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
        command = command_for(text) or (self.menu.chosen if text.startswith("/") else None)
        if command is not None:
            event.input.clear()
            await self.app.run_action(command.action, self)
            return
        if self.replying:
            self.notify("Still replying. Press esc to interrupt it first.")
            return
        event.input.clear()
        self._remember(text)
        await self.query(Welcome).remove()
        await self.messages.mount(you_said(text))
        self.reply_worker = self.stream_reply(text)

    @work(exclusive=True, group="reply")
    async def stream_reply(self, text: str) -> None:
        messages = self.messages
        messages.anchor()  # keep scrolled to the bottom while text arrives
        spinner = self.query_one(Spinner)
        spinner.start()
        reply = self.reply = ReplyBlock(messages)
        started, tokens, writing_s = time.monotonic(), 0, 0.0
        try:
            async for chunk in self.core.conversation.send(text):
                if chunk.kind == "stats" and chunk.stats:
                    tokens += chunk.stats.reply_tokens
                    writing_s += chunk.stats.reply_seconds
                    spinner.tokens = tokens  # Ollama's exact count for the rounds so far
                    continue
                if chunk.kind in ("break", "retract"):
                    await (reply.close() if chunk.kind == "break" else reply.retract())
                    reply = self.reply = ReplyBlock(messages)
                    continue
                spinner.tokens += 1  # about a token per streamed piece
                if chunk.kind == "thinking":
                    await reply.thinking(chunk.text)
                else:
                    await reply.content(chunk.text)
        except OllamaError as e:
            await reply.content(f"**Error:** {e}")
        except asyncio.CancelledError:
            await reply.content("\n\n*(interrupted)*")
            raise
        finally:
            spinner.stop()
            await reply.close()
            self.reply = None
            took = time.monotonic() - started
            if took >= LONG_REPLY_S:
                self._tap_shoulder(f"Done ({took:.0f}s). The reply is in the terminal.")
            self.last_turn = turn_summary(took, tokens, writing_s)
            self._update_context()
            self._save_chat()

    def _save_chat(self) -> None:
        """Keep this chat for /resume (unless [history] save = false)."""
        messages = self.core.conversation.history[1:]  # not the system prompt
        if not self.core.config.history.save or not from_lex(messages):
            return
        try:
            self.chat_id = self.core.store.save(self.chat_id, messages)
        except (sqlite3.Error, OSError) as e:
            self.notify(f"Couldn't save this chat: {e}", severity="warning", markup=False)

    async def on_history_panel_open(self, event: HistoryPanel.Open) -> None:
        await self.action_open_chat(event.chat_id)

    async def action_open_chat(self, chat_id: int) -> None:
        """Carry on a saved chat: back on screen, and back in CORE's memory."""
        if self.running is not None or self.replying:
            self.notify("CORE is still working on the last message. Esc stops it first.")
            return
        try:
            messages = self.core.store.load(chat_id)
        except (KeyError, sqlite3.Error) as e:
            self.notify(f"Couldn't open that chat: {e!r}", severity="warning", markup=False)
            return
        self.core.conversation.load(messages)
        self.chat_id = chat_id
        self.last_block = None
        await self.show_saved(messages)
        self._update_context()
        self.action_show_tab("chat")
        self.notify(f"Carrying on: {title_for(messages)}", markup=False)

    async def show_saved(self, messages: list[dict[str, object]]) -> None:
        """Put a saved chat back on screen, as it looked."""
        area = self.messages
        await area.remove_children()
        widgets: list[Horizontal | CommandBlock] = []
        for item in replay(messages):
            if item.kind == "you":
                widgets.append(you_said(item.text))
            elif item.kind == "reply":
                widgets.append(Horizontal(Static("⏺", classes="bullet"),
                                          Markdown(item.text, classes="assistant"),
                                          classes="reply"))  # fmt: skip
            elif item.kind == "command":
                status, shown = outcome(item.output)
                blocked = Assessment(Tier.BLOCKED, shown)
                block = CommandBlock(item.text, blocked if status == "blocked" else FROM_HISTORY)
                block.raw = "" if status == "blocked" else shown
                block.status = status
                widgets.append(block)
        await area.mount_all(widgets)
        area.scroll_end(animate=False)
        area.anchor()

    def model_changed(self, model: str) -> None:
        """Settings picked another model."""
        self.status.snap.model = model
        self.status.refresh_status()

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker is self.reply_worker:
            self._changed()  # the reply started or finished

    def _remember(self, text: str) -> None:
        if not self.sent or self.sent[-1] != text:
            self.sent.append(text)
            del self.sent[:-HISTORY_SIZE]
        self.history_at, self.draft = len(self.sent), ""

    def action_history(self, step: int) -> None:
        """↑/↓ move through the / menu if it's open, else bring back earlier messages."""
        if self.menu.display:
            if step < 0:
                self.menu.action_cursor_up()
            else:
                self.menu.action_cursor_down()
            return
        if not self.sent or self.running is not None or self.recorder is not None:
            return
        if self.history_at == len(self.sent):
            self.draft = self.prompt.value
        self.history_at = max(0, min(len(self.sent), self.history_at + step))
        at_end = self.history_at == len(self.sent)
        self.prompt.value = self.draft if at_end else self.sent[self.history_at]
        self.prompt.cursor_position = len(self.prompt.value)

    def action_suggest(self, index: int) -> None:
        """A tip on the welcome box was clicked: put it in the box (not sent)."""
        self.prompt.value = SUGGESTIONS[index]
        self.prompt.cursor_position = len(self.prompt.value)
        self.prompt.focus()

    async def on_option_list_option_selected(self, event: CommandMenu.OptionSelected) -> None:
        """A / command was clicked in the menu."""
        command = self.menu.chosen
        self.prompt.value = ""
        if command is not None:
            await self.app.run_action(command.action, self)

    # Keys ----------------------------------------------------------------

    def action_cancel(self) -> None:
        # Like a terminal: Ctrl+C stops the running command first. CORE's
        # model is told, and can react. Pressing it again stops the reply.
        if self.running is not None:
            self.running.kill()
        elif self.reply_worker and self.reply_worker.state == WorkerState.RUNNING:
            self.reply_worker.cancel()

    async def action_new_chat(self) -> None:
        """/clear or Ctrl+L: forget this conversation. Frees the context window."""
        if self.running is not None or self.replying:
            self.notify("CORE is still working on the last message. Esc stops it first.")
            return
        self.core.conversation.reset()
        self.chat_id = None  # the old one stays in /resume
        self.last_block = None
        self.last_turn = ""
        self.action_show_tab("chat")
        self.messages.anchor(False)  # back to the top, where the welcome box goes
        await self.messages.remove_children()
        await self.messages.mount(self._welcome())
        self._update_context()
        self._changed()

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
            spinner = self.query_one(Spinner)
            spinner.doing = f"Running {event.block.command[:40]}" if self.running else ""
            self._changed()

    # AgentUI (see core/tools/registry.py) ---------------------------------

    async def approve(
        self, command: str, assessment: Assessment, explanation: str, undo: str
    ) -> bool:
        if self.reply:
            self.reply.done_thinking()
        self._tap_shoulder(f"Needs your y/n to run: {command[:80]}", icon="dialog-question")
        remote = session_kind() == "remote"
        spinner = self.query_one(Spinner)
        spinner.stop()  # the question is what's happening now
        try:
            modal = ApprovalModal(command, assessment, explanation, undo, remote)
            return bool(await self._wait_for(modal))
        finally:
            spinner.display = self.replying

    async def ask_password(self) -> str | None:
        result = await self._wait_for(PasswordModal())
        return result if isinstance(result, str) else None

    async def command_view(self, command: str, assessment: Assessment) -> CommandBlock:
        if self.reply:
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
        self.status.redraw()

    # GestureUI (see core/tools/gestures.py) -------------------------------

    def show_sign(self, sign: str | None) -> None:
        self.status.snap.sign = sign
        self.status.redraw()

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

    def _tap_shoulder(self, message: str, icon: str = "utilities-terminal") -> None:
        """A GNOME notification, but only if Lex is in another window at the PC.

        app_focus comes from the terminal's focus reports; terminals that don't
        send them leave it True, so they never get notifications.
        """
        if not self.app.app_focus and session_kind() == "local":
            notify_desktop(message, icon=icon)

    async def _wait_for(self, modal: ModalScreen[Any]) -> Any:
        """Show a dialog and wait for its answer; close it if the reply is cancelled."""
        try:
            return await self.app.push_screen_wait(modal)
        except asyncio.CancelledError:
            if modal.is_current:
                self.app.pop_screen()
            raise
