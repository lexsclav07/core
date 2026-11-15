"""The History, Activity and Settings tabs, driven through the real screen."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from textual.widgets import DataTable, Input, Select, Switch, TabbedContent

from core.agent import audit
from core.agent.ollama_client import OllamaClient
from core.app import CoreApp
from core.chat import ChatScreen
from core.config import load_config
from core.panels.activity import ActivityPanel
from core.widgets import CommandBlock

TAGS = {"models": [{"name": "qwen3:8b", "size": 5_200_000_000},
                   {"name": "llama3.1:8b", "size": 4_900_000_000}]}  # fmt: skip


def fake_ollama() -> OllamaClient:
    """Asks to run `echo hi` once per message, then answers."""

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/version":
            return httpx.Response(200, json={"version": "0.13.0"})
        if path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        if path == "/api/tags":
            return httpx.Response(200, json=TAGS)
        messages = json.loads(request.content)["messages"]
        if messages[-1]["role"] == "user":
            args = {"command": "echo hi", "explanation": "x", "undo": "nothing to undo"}
            msg = {"content": "", "tool_calls": [{"function": {"name": "run_command",
                                                               "arguments": args}}]}  # fmt: skip
        else:
            msg = {"content": "Said hi."}
        line = json.dumps({"message": {"role": "assistant", **msg}, "done": True})
        return httpx.Response(200, content=(line + "\n").encode())

    return OllamaClient("http://ollama", transport=httpx.MockTransport(handle))


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CoreApp:
    # Both: the Settings tab writes config.toml, chats go in history.db.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    core = CoreApp(load_config())
    core.client = core.conversation.client = fake_ollama()
    return core


def screen(app: CoreApp) -> ChatScreen:
    assert isinstance(app.screen, ChatScreen)
    return app.screen


async def send(pilot: Any, text: str) -> None:
    await pilot.press(*text, "enter")
    for _ in range(100):
        await pilot.pause(0.05)
        if not screen(pilot.app).replying and pilot.app.conversation.history[-1]["content"]:
            return


async def test_f_keys_switch_tabs_and_esc_comes_back(app: CoreApp) -> None:
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("escape")  # the splash
        tabs = screen(app).query_one(TabbedContent)
        for key, tab in [("f3", "tab-history"), ("f4", "tab-activity"), ("f5", "tab-settings")]:
            await pilot.press(key)
            await pilot.pause(0.1)
            assert tabs.active == tab
        await pilot.press("escape")
        assert tabs.active == "tab-chat" and isinstance(app.focused, Input)


async def test_chats_are_saved_and_carried_on(app: CoreApp) -> None:
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("escape")
        await send(pilot, "say hi")
        first = screen(app).chat_id
        assert first is not None
        await pilot.press("ctrl+l")
        await send(pilot, "say hi again")
        assert screen(app).chat_id not in (None, first)  # a new chat, the old one kept

        await pilot.press("f3")
        await pilot.pause(0.1)
        table = screen(app).query_one("#history-table", DataTable)
        assert table.row_count == 2
        await pilot.press("tab", "down", "enter")  # the older chat
        await pilot.pause(0.3)
        assert screen(app).query_one(TabbedContent).active == "tab-chat"
        assert screen(app).chat_id == first
        said = [m["content"] for m in app.conversation.history if m["role"] == "user"]
        assert said == ["say hi"]  # CORE remembers that chat, not the other one
        [block] = screen(app).query(CommandBlock)
        assert block.command == "echo hi" and block.status == "exit 0"


async def test_nothing_is_saved_when_history_is_off(app: CoreApp) -> None:
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("escape")
        app.change_setting("history", "save", False)
        await send(pilot, "say hi")
        assert app.store.chats() == []


async def test_activity_lists_the_audit_log(app: CoreApp) -> None:
    for command, tier, approval in [("ls", "SAFE", "auto"), ("rm -rf /", "BLOCKED", "blocked"),
                                    ("sudo dnf install -y htop", "CONFIRM", "denied")]:  # fmt: skip
        audit.record(audit.AuditEntry(command, tier, "a reason", approval, "local"))  # type: ignore[arg-type]
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("escape", "f4")
        await pilot.pause(0.1)
        panel = screen(app).query_one(ActivityPanel)
        assert [e["command"] for e in panel.entries][0] == "sudo dnf install -y htop"  # newest
        await pilot.press("shift+tab", *"refused")
        await pilot.pause(0.1)
        assert [e["command"] for e in panel.entries] == ["rm -rf /"]
        assert "a reason" in str(panel.query_one("#activity-detail").render())


async def test_settings_change_the_model_and_save_it(app: CoreApp) -> None:
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("escape", "f5")
        await pilot.pause(0.3)
        picker = screen(app).query_one("#set-model", Select)
        assert [value for _, value in picker._options] == ["llama3.1:8b", "qwen3:8b"]
        picker.value = "llama3.1:8b"
        await pilot.pause(0.1)
        assert app.conversation.model == "llama3.1:8b"
        assert load_config().model.main == "llama3.1:8b"  # saved to config.toml
        screen(app).query_one("#set-think", Switch).value = True
        await pilot.pause(0.1)
        assert app.conversation.think is True and load_config().model.think is True


async def test_settings_change_the_look_at_once(app: CoreApp) -> None:
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("escape", "f5")
        await pilot.pause(0.1)
        screen(app).query_one("#set-mode", Select).value = "light"
        await pilot.pause(0.1)
        assert app.theme.startswith("core-light") and load_config().ui.mode == "light"
        screen(app).query_one("#set-accent", Select).value = "#9141ac"
        await pilot.pause(0.1)
        assert app.theme == "core-light-9141ac"
