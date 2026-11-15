"""Saved chats (core/history.py) and carrying one on in a conversation."""

from pathlib import Path

import pytest

from core.agent.loop import Conversation
from core.agent.ollama_client import OllamaClient
from core.history import AUTOMATIC, ChatStore, Shown, outcome, replay, title_for


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


def run(command: str) -> dict[str, object]:
    args = {"command": command, "explanation": "x", "undo": "x"}
    return {"function": {"name": "run_command", "arguments": args}}


def test_replay_puts_a_chat_back_as_it_looked() -> None:
    messages = [
        {"role": "user", "content": "what kernel?"},
        {"role": "assistant", "content": "Run this:\n```bash\nuname -r\n```"},  # redone
        {"role": "user", "content": f"{AUTOMATIC}) do it yourself"},
        {"role": "assistant", "content": "", "tool_calls": [run("uname -r"), run("whoami")]},
        {"role": "tool", "content": "Exit code 0.\n6.18.44"},
        {"role": "tool", "content": "Exit code 0.\nlex"},
        {"role": "assistant", "content": "Kernel 6.18.44."},
        {"role": "user", "content": "look at my screen"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "look_at_screen", "arguments": {"question": "what's open"}}}
            ],
        },
        {"role": "tool", "content": "a terminal"},
    ]
    assert replay(messages) == [
        Shown("you", "what kernel?"),
        Shown("core"),
        Shown("command", "uname -r", "Exit code 0.\n6.18.44"),
        Shown("command", "whoami", "Exit code 0.\nlex"),
        Shown("reply", "Kernel 6.18.44."),
        Shown("you", "look at my screen"),
        Shown("core"),
        Shown("command", 'look_at_screen(question="what\'s open")', "a terminal"),
    ]


@pytest.mark.parametrize(
    ("saved", "status", "shown"),
    [
        ("Exit code 0.\n6.18.44", "exit 0", "6.18.44"),
        ("Exit code 127.\nbash: ptyxsi: command not found", "exit 127",
         "bash: ptyxsi: command not found"),
        ("BLOCKED by CORE's safety rules: rm -r on / would wipe the system. This can't be run,"
         " even if Lex asks.", "blocked", "rm -r on / would wipe the system"),
        ("Not run: Lex pressed n in CORE's approval dialog, so…", "denied", ""),
        ("Not run: the GNOME setting x y doesn't exist.", "invalid", ""),
        ("Timed out after 120s and was stopped.\npartial", "timed out", "partial"),
        ("Lex stopped this command.\npartial", "stopped", "partial"),
        ("What the vision model saw: a terminal", "saved", "What the vision model saw: a terminal"),
    ],
)  # fmt: skip
def test_saved_outcomes_read_like_they_did_live(saved: str, status: str, shown: str) -> None:
    assert outcome(saved) == (status, shown)
