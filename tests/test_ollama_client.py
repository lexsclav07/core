"""Tests run against a fake Ollama (httpx.MockTransport), no server needed."""

import json

import httpx
import pytest

from core.agent.loop import Conversation
from core.agent.ollama_client import Chunk, OllamaClient, OllamaError


def ndjson(*objs: dict[str, object]) -> bytes:
    return "".join(json.dumps(o) + "\n" for o in objs).encode()


def fake_ollama(handler) -> OllamaClient:  # type: ignore[no-untyped-def]
    return OllamaClient("http://ollama", transport=httpx.MockTransport(handler))


async def test_chat_streams_thinking_then_content() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        body = ndjson(
            {"message": {"role": "assistant", "content": "", "thinking": "hmm"}, "done": False},
            {"message": {"role": "assistant", "content": "Hel"}, "done": False},
            {"message": {"role": "assistant", "content": "lo"}, "done": False},
            {"message": {"role": "assistant", "content": ""}, "done": True},
        )
        return httpx.Response(200, content=body)

    client = fake_ollama(handler)
    chunks = [c async for c in client.chat("m", [], num_ctx=1024, think=True)]
    assert chunks == [Chunk("thinking", "hmm"), Chunk("content", "Hel"), Chunk("content", "lo")]
    assert seen["options"] == {"num_ctx": 1024}
    assert seen["think"] is True and seen["stream"] is True


async def test_chat_error_line_raises() -> None:
    client = fake_ollama(lambda r: httpx.Response(200, content=ndjson({"error": "boom"})))
    with pytest.raises(OllamaError, match="boom"):
        [c async for c in client.chat("m", [], num_ctx=1024, think=False)]


async def test_chat_http_error_raises() -> None:
    client = fake_ollama(lambda r: httpx.Response(404, json={"error": "model not found"}))
    with pytest.raises(OllamaError, match="model not found"):
        [c async for c in client.chat("m", [], num_ctx=1024, think=False)]


async def test_unreachable_raises_ollama_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(OllamaError, match="can't reach"):
        await fake_ollama(handler).version()


async def test_loaded_models_reports_gpu_split() -> None:
    body = {
        "models": [
            {"name": "a", "size": 100, "size_vram": 100},
            {"name": "b", "size": 100, "size_vram": 60},
        ]
    }
    client = fake_ollama(lambda r: httpx.Response(200, json=body))
    a, b = await client.loaded_models()
    assert a.fully_on_gpu and not b.fully_on_gpu


async def test_conversation_records_history() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=ndjson(
                {"message": {"content": "", "thinking": "x"}, "done": False},
                {"message": {"content": "hi"}, "done": True},
            ),
        )

    convo = Conversation(fake_ollama(handler), "m", num_ctx=1024, think=True)
    [c async for c in convo.send("hello")]
    assert [m["role"] for m in convo.history] == ["system", "user", "assistant"]
    assert convo.history[-1]["content"] == "hi"  # thinking is not stored


async def test_failed_turn_is_dropped_from_history() -> None:
    client = fake_ollama(lambda r: httpx.Response(404, json={"error": "model not found"}))
    convo = Conversation(client, "m", num_ctx=1024, think=False)
    with pytest.raises(OllamaError):
        [c async for c in convo.send("hello")]
    assert [m["role"] for m in convo.history] == ["system"]
