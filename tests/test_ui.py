"""The look: the line under the input, welcome box, / menu, spinner, hand view,
command blocks. Pure functions, plus runs of the real chat screen against a
fake Ollama."""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from textual.markup import to_content

from core.agent.ollama_client import OllamaClient
from core.app import CoreApp
from core.chat import ChatScreen, StatusLine, turn_summary
from core.commands import command_for, matching
from core.config import Config
from core.handview import render as draw_hand
from core.history import ChatSummary
from core.spinner import spinner_line
from core.status import Snapshot, escape, level, render, short
from core.welcome import Welcome, greeting, recent_text, tips_text
from core.widgets import highlight

HANDS = json.loads((Path(__file__).parent / "data" / "hands.json").read_text())


def plain(markup: str) -> str:
    return to_content(markup).plain


# The line under the input ------------------------------------------------------


def test_status_line_wide_and_narrow() -> None:
    snap = Snapshot(model="qwen3:8b", model_state="gpu", vram_mb=(6144, 8192),
                    context=(1640, 8192))  # fmt: skip
    assert plain(render(snap, 140)) == "● qwen3:8b · 1.6k/8k ctx · 6.0/8.0G VRAM · local"
    assert plain(render(snap, 80)) == "● qwen3:8b · 1.6k/8k ctx · local"  # no room for VRAM


def test_colour_only_when_it_needs_attention() -> None:
    assert [level(f) for f in (0.5, 0.8, 0.95)] == ["", "$warning", "$error"]
    calm = render(Snapshot(model="m", model_state="idle", context=(1000, 8192)), 140)
    assert "$warning" not in calm and "$error" not in calm


def test_full_context_says_what_to_do() -> None:
    snap = Snapshot(context=(7800, 8192))
    assert "/clear" in plain(render(snap, 140))
    assert "/clear" not in plain(render(Snapshot(context=(2000, 8192)), 140))


def test_senses_are_red_with_the_sign() -> None:
    markup = render(Snapshot(senses={"gestures", "mic"}, sign="peace"), 140)
    assert markup.startswith("[b $error]● ✋ gestures 🎤 mic ✌ peace[/]")


def test_odd_names_cannot_break_the_markup() -> None:
    snap = Snapshot(model="[ weird ]", model_state="gpu", senses={"gestures"}, sign="[b]x")
    text = plain(render(snap, 140))
    assert "[ weird ]" in text and "[b]x" in text


def test_short_token_counts() -> None:
    assert [short(n) for n in (512, 1640, 8192, 16384)] == ["512", "1.6k", "8k", "16k"]


def test_escape_handles_what_textual_misses() -> None:
    assert plain(f"[b]{escape('[ CORE ]')}[/] ok") == "[ CORE ] ok"


# Welcome and help ------------------------------------------------------------


@pytest.mark.parametrize(
    ("hour", "words"),
    [(6, "Good morning"), (13, "Good afternoon"), (21, "Good evening"), (2, "Up late")],
)
def test_greeting(hour: int, words: str) -> None:
    assert greeting(datetime(2026, 1, 1, hour)).startswith(words)


def test_welcome_tips_and_recent_chats() -> None:
    assert "› install prism launcher" in plain(tips_text())
    chat = ChatSummary(7, "install htop", datetime(2026, 1, 1, 9), datetime(2026, 1, 1, 21, 4), 2)
    recent = recent_text([chat], now=datetime(2026, 1, 1, 22))
    assert "screen.open_chat(7)" in recent  # a click carries it on
    assert plain(recent).splitlines() == ["Recent chats", "› install htop 21:04",
                                          "/resume for more"]  # fmt: skip
    assert "None yet" in plain(recent_text([]))


@pytest.mark.parametrize(("size", "shown"), [((120, 40), True), ((80, 24), False)])
async def test_welcome_logo_only_when_there_is_room(
    app: CoreApp, size: tuple[int, int], shown: bool
) -> None:
    async with app.run_test(size=size) as pilot:
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert app.screen.query_one("#welcome-logo").display is shown


def test_turn_summary() -> None:
    assert turn_summary(2.61, 74, 2.0) == "2.6s · 37 tok/s"
    assert turn_summary(0.4, 0, 0) == "0.4s"


def test_spinner_line() -> None:
    line = plain(spinner_line(4, "Tinkering", 12.3, 340))
    assert line == "✻ Tinkering… (12s · ↓ 340 tokens · esc to interrupt)"
    assert plain(spinner_line(0, "Running [x]", 0, 0)) == "· Running [x]… (0s · esc to interrupt)"


# Slash commands ----------------------------------------------------------------


def test_slash_menu_matches_as_you_type() -> None:
    assert [c.name for c in matching("/")][:3] == ["help", "clear", "resume"]
    assert [c.name for c in matching("/cl")] == ["clear"]
    assert [c.name for c in matching("/s")] == ["settings", "signs", "resume"]  # starts, contains


@pytest.mark.parametrize(
    ("message", "runs"),
    [("/clear", "clear"), ("/model qwen", "model"), ("/CLEAR", "clear"),
     ("/etc/fstab looks wrong", None), ("/", None), ("hello /clear", None)],
)  # fmt: skip
def test_only_known_commands_run(message: str, runs: str | None) -> None:
    command = command_for(message)
    assert (command.name if command else None) == runs


# Hand view and highlighting --------------------------------------------------


def test_hand_view_draws_the_hand_and_the_hold_bar() -> None:
    peace = next(h["hand"] for h in HANDS if h["sign"] == "peace")
    text = plain(draw_hand(peace, "peace", 0.5))
    picture, caption, letters = text.rsplit("\n", 2)
    assert any("\u2800" < ch <= "\u28ff" for ch in picture)  # braille dots
    assert caption == "✌ peace ━━━━────"
    assert letters == "t I M r p"  # index and middle up


def test_hand_view_without_a_hand() -> None:
    assert "show me your hand" in plain(draw_hand(None, None, 0))


@pytest.mark.parametrize(
    "command",
    ["sudo dnf install -y htop", "echo '[red]not markup[/]' > /tmp/x", "ls ~/Документы && cd -"],
)
def test_highlighting_never_changes_the_command(command: str) -> None:
    assert highlight(command).plain == command  # the dialog shows exactly what runs


# The real chat screen against a fake Ollama ------------------------------------


def fake_ollama(reply: str = "Hello Lex.") -> OllamaClient:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.13.0"})
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        done = {"message": {"role": "assistant", "content": reply}, "done": True,
                "eval_count": 3, "eval_duration": 10**8, "prompt_eval_count": 900}  # fmt: skip
        return httpx.Response(200, content=(json.dumps(done) + "\n").encode())

    return OllamaClient("http://ollama", transport=httpx.MockTransport(handle))


@pytest.fixture
def app(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> CoreApp:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    core = CoreApp(Config())
    core.client = core.conversation.client = fake_ollama()
    return core


async def send(pilot: Any, text: str) -> None:
    await pilot.press(*text, "enter")
    for _ in range(50):
        await pilot.pause(0.05)
        screen = pilot.app.screen
        if isinstance(screen, ChatScreen) and not screen.replying and screen.last_turn:
            return


async def test_chat_round_trip(app: CoreApp) -> None:
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("escape")  # skip the splash
        chat = app.screen
        assert isinstance(chat, ChatScreen) and chat.query(Welcome)
        await send(pilot, "hi")
        assert not chat.query(Welcome)  # gone once the chat starts
        assert str(chat.query_one(".user .said").render()) == "hi"
        assert str(chat.query_one(".reply .bullet").render()) == "⏺"
        assert re.fullmatch(r"\d+\.\ds · 30 tok/s", chat.last_turn)
        assert "last reply" in chat.status.hint
        used, total = chat.query_one(StatusLine).snap.context
        assert used >= 903 and total == app.config.model.num_ctx


async def test_up_and_down_bring_back_messages(app: CoreApp) -> None:
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("escape")
        chat = app.screen
        assert isinstance(chat, ChatScreen)
        await send(pilot, "first")
        await send(pilot, "second")
        await pilot.press(*"draft")
        await pilot.press("up")
        assert chat.prompt.value == "second"
        await pilot.press("up", "up")  # stops at the oldest
        assert chat.prompt.value == "first"
        await pilot.press("down", "down")
        assert chat.prompt.value == "draft"  # what was being typed comes back


async def test_new_chat_forgets_everything(app: CoreApp) -> None:
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("escape")
        chat = app.screen
        assert isinstance(chat, ChatScreen)
        await send(pilot, "remember this")
        assert len(app.conversation.history) > 1
        await pilot.press("ctrl+l")
        await pilot.pause(0.1)
        assert len(app.conversation.history) == 1  # only the system prompt
        assert chat.query(Welcome) and not chat.query(".user")


async def test_keys_only_fire_when_they_do_something(app: CoreApp) -> None:
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("escape")
        chat = app.screen
        assert isinstance(chat, ChatScreen)
        assert chat.check_action("cancel", ()) is False
        assert chat.check_action("toggle_output", ()) is False  # no command yet
        assert chat.check_action("new_chat", ()) is True


async def test_clicking_a_suggestion_fills_the_box(app: CoreApp) -> None:
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("escape")
        chat = app.screen
        assert isinstance(chat, ChatScreen)
        chat.action_suggest(4)
        assert chat.prompt.value == "install prism launcher"
        assert len(app.conversation.history) == 1  # nothing was sent


async def test_help_opens_and_closes(app: CoreApp) -> None:
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("escape", "f1")
        assert type(app.screen).__name__ == "HelpScreen"
        await pilot.press("escape")
        assert isinstance(app.screen, ChatScreen)


async def test_desktop_nudge_only_when_lex_looked_away(
    app: CoreApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    import core.chat
    from core.agent.risk import Assessment, Tier

    sent: list[str] = []
    monkeypatch.setattr(core.chat, "notify_desktop", lambda message, icon="": sent.append(message))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("escape")
        chat = app.screen
        assert isinstance(chat, ChatScreen)
        confirm = Assessment(Tier.CONFIRM, "runs as root (sudo)")
        for focused in (True, False):
            app.app_focus = focused
            # Dialogs need a Textual worker, like the reply worker in real use.
            asking = chat.run_worker(chat.approve("sudo dnf install -y htop", confirm, "", ""))
            await pilot.pause(0.1)
            await pilot.press("n")
            await asking.wait()
            assert asking.result is False
        assert sent == ["Needs your y/n to run: sudo dnf install -y htop"]  # only the 2nd


async def test_slash_commands_run_instead_of_going_to_the_model(app: CoreApp) -> None:
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("escape")
        chat = app.screen
        assert isinstance(chat, ChatScreen)
        await pilot.press("/", "r", "e")
        assert chat.menu.display and chat.menu.chosen is not None
        assert chat.menu.chosen.name == "resume"
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert chat.tab == "history" and len(app.conversation.history) == 1  # nothing sent
        await pilot.press("escape")
        assert chat.tab == "chat"


async def test_question_mark_opens_the_shortcuts(app: CoreApp) -> None:
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("escape", "question_mark")
        await pilot.pause(0.1)
        assert type(app.screen).__name__ == "HelpScreen"
        await pilot.press("escape")
        chat = app.screen
        assert isinstance(chat, ChatScreen) and chat.prompt.value == ""


async def test_a_yes_typed_too_soon_does_not_approve(app: CoreApp) -> None:
    from core.agent.risk import Assessment, Tier

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("escape")
        chat = app.screen
        assert isinstance(chat, ChatScreen)
        confirm = Assessment(Tier.CONFIRM, "runs as root (sudo)")
        asking = chat.run_worker(chat.approve("sudo dnf install -y htop", confirm, "", ""))
        await pilot.pause(0.05)
        await pilot.press("y", "enter")  # already typing when it popped up
        await pilot.pause(0.1)
        assert asking.is_running  # still asking
        await pilot.pause(0.5)
        await pilot.press("enter")  # now it's a real answer (Yes is highlighted)
        await asking.wait()
        assert asking.result is True
