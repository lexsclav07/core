"""Saved chats (core/history.py) and carrying one on in a conversation."""

from pathlib import Path

import pytest

from core.agent.loop import Conversation
from core.agent.ollama_client import OllamaClient
from core.history import AUTOMATIC, ChatStore, title_for


def chat(*said: str) -> list[dict[str, str]]:
    messages = []
    for text in said:
        messages += [{"role": "user", "content": text}, {"role": "assistant", "content": "ok"}]
    return messages


def test_save_list_load_delete(tmp_path: Path) -> None:
    store = ChatStore(tmp_path / "history.db")
    first = store.save(None, chat("what GPU do I have?"))
    second = store.save(None, chat("install htop", "thanks"))
    assert [c.title for c in store.chats()] == ["install htop", "what GPU do I have?"]
    assert store.chats()[0].turns == 2
    assert store.load(second) == chat("install htop", "thanks")
    store.delete(second)
    assert [c.id for c in store.chats()] == [first]
    with pytest.raises(KeyError):
        store.load(second)


def test_saving_again_updates_the_same_chat(tmp_path: Path) -> None:
    store = ChatStore(tmp_path / "history.db")
    chat_id = store.save(None, chat("hi"))
    assert store.save(chat_id, chat("hi", "and again")) == chat_id
    [only] = store.chats()
    assert only.turns == 2
    assert store.save(999, chat("gone")) != 999  # a deleted chat starts afresh


def test_search_looks_inside_the_chat(tmp_path: Path) -> None:
    store = ChatStore(tmp_path / "history.db")
    store.save(None, chat("install htop"))
    store.save(None, [{"role": "user", "content": "fix wifi"},
                      {"role": "assistant", "content": "NetworkManager restarted"}])  # fmt: skip
    assert [c.title for c in store.chats("networkmanager")] == ["fix wifi"]
    assert [c.title for c in store.chats("100%")] == []  # no SQL wildcards


def test_titles_skip_cores_own_nudges() -> None:
    messages = [{"role": "user", "content": f"{AUTOMATIC}) do it"},
                {"role": "user", "content": "  turn on\n dark mode  "}]  # fmt: skip
    assert title_for(messages) == "turn on dark mode"
    assert title_for([]) == "(empty chat)"
    assert title_for(chat("x" * 200)).endswith("…")


def test_conversation_carries_on_a_saved_chat() -> None:
    convo = Conversation(OllamaClient("http://x"), "m", num_ctx=8192, think=False)
    system = convo.history[0]
    convo.load([{"role": "system", "content": "old prompt"}, *chat("hi")])
    assert convo.history == [system, *chat("hi")]  # today's prompt, not the saved one
