"""The look: status bar, welcome panel, hand view, highlighting. Pure functions,
plus a few runs of the real chat screen against a fake Ollama."""

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
from core.chat import ChatScreen, StatusBar, turn_summary
from core.config import Config
from core.handview import render as draw_hand
from core.status import Snapshot, escape, meter, render, short
from core.welcome import Welcome, greeting, welcome_text
from core.widgets import highlight

HANDS = json.loads((Path(__file__).parent / "data" / "hands.json").read_text())


def plain(markup: str) -> str:
    return to_content(markup).plain


# Status bar ------------------------------------------------------------------


def test_status_bar_wide_and_narrow() -> None:
    snap = Snapshot(model="qwen3:8b", model_state="gpu", vram_mb=(6144, 8192),
                    context=(1640, 8192))  # fmt: skip
    wide = plain(render(snap, 140, datetime(2026, 10, 21, 21, 4)))
    assert wide.startswith(" ◆ CORE ")
    assert "VRAM ━━━━━━━━── 6.0/8.0G" in wide  # 75%: 8 of 10 cells
    assert "ctx ━━────── 1.6k/8k" in wide
    assert wide.endswith("⌂ local  21:04")
    narrow = plain(render(snap, 80))
    assert "VRAM 6.0/8.0G" in narrow and "━" not in narrow


def test_meter_colours() -> None:
    assert meter(0.5, 4).startswith("[$success]━━[/]")
    assert meter(0.8, 4).startswith("[$warning]")
    assert meter(0.95, 4).startswith("[$error]")
    assert meter(2, 4) == "[$error]━━━━[/][$text-muted][/]"  # clamped


def test_full_context_says_what_to_do() -> None:
    snap = Snapshot(context=(7800, 8192))
    assert "Ctrl+L" in plain(render(snap, 140))
    assert "Ctrl+L" not in plain(render(Snapshot(context=(2000, 8192)), 140))


def test_senses_are_red_with_the_sign() -> None:
    markup = render(Snapshot(senses={"gestures", "mic"}, sign="peace"), 140)
    assert "[b $error]● ✋ gestures 🎤 mic[/] [b]✌ peace[/]" in markup


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


def test_welcome_shows_the_logo_and_suggestions() -> None:
    text = plain(welcome_text("[ CORE ]"))
    assert text.startswith("[ CORE ]")
    assert "› install prism launcher" in text


def test_turn_summary() -> None:
    assert turn_summary(2.61, 74, 2.0) == "2.6s · 74 tokens · 37 tok/s"
    assert turn_summary(0.4, 0, 0) == "0.4s"


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
        if isinstance(screen, ChatScreen) and not screen.replying and screen.query(".reply-meta"):
            return


async def test_chat_round_trip(app: CoreApp) -> None:
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("escape")  # skip the splash
        chat = app.screen
        assert isinstance(chat, ChatScreen) and chat.query(Welcome)
        await send(pilot, "hi")
        assert not chat.query(Welcome)  # gone once the chat starts
        texts = [str(w.render()) for w in chat.query(".who")]
        assert re.match(r"you · \d\d:\d\d", texts[0]) and texts[1] == "◆ CORE"
        assert "3 tokens · 30 tok/s" in str(chat.query_one(".reply-meta").render())
        used, total = chat.query_one(StatusBar).snap.context
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
        assert chat.query(Welcome) and not chat.query(".who")


async def test_footer_only_offers_stop_while_busy(app: CoreApp) -> None:
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
