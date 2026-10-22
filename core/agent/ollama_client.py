"""A small async client for Ollama's HTTP API.

We talk to the API directly with httpx instead of using the `ollama` package:
it's only three endpoints, and seeing the raw JSON makes the phase-2 tool loop
easier to debug.

Streaming: /api/chat returns one JSON object per line (NDJSON). Each line has a
piece of the reply in message.content (and message.thinking when thinking is
on). Tool calls arrive whole, in message.tool_calls. The last line has
"done": true, plus token counts and timings that we pass on as a "stats" chunk.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

import httpx

Message = dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Stats:
    """From the last line of a reply. Ollama reports times in nanoseconds."""

    prompt_tokens: int  # tokens it read (may leave out ones it had cached)
    reply_tokens: int
    reply_seconds: float  # time spent writing the reply, not reading the prompt

    @property
    def tokens_per_second(self) -> float:
        return self.reply_tokens / self.reply_seconds if self.reply_seconds > 0 else 0.0


@dataclass(frozen=True)
class Chunk:
    """One streamed piece of a reply.

    "break" and "retract" aren't from Ollama; the agent loop sends them.
    break: tools ran, so the TUI starts a fresh text block below their output.
    retract: the reply so far handed Lex commands instead of running them, so
    the TUI removes it while the model tries again.
    stats: the reply is finished; `stats` says how big and how fast it was.
    """

    kind: Literal["thinking", "content", "tool_call", "break", "retract", "stats"]
    text: str = ""
    call: ToolCall | None = None
    stats: Stats | None = None


@dataclass(frozen=True)
class LoadedModel:
    """A model Ollama currently has in memory (from /api/ps)."""

    name: str
    size_vram: int  # bytes on the GPU
    size: int  # total bytes; if larger than size_vram, part is on the CPU

    @property
    def fully_on_gpu(self) -> bool:
        return self.size_vram >= self.size


class OllamaError(Exception):
    """Ollama is unreachable or returned an error."""


class OllamaClient:
    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        # No overall timeout: a long answer can stream for minutes. We still
        # fail fast if the server isn't there at all.
        timeout = httpx.Timeout(None, connect=5.0)
        self.base_url = base_url
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=transport)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def version(self) -> str:
        """Return Ollama's version string; raises OllamaError if unreachable."""
        data = await self._get("/api/version")
        return str(data.get("version", "?"))

    async def loaded_models(self) -> list[LoadedModel]:
        """Return the models currently loaded in memory."""
        data = await self._get("/api/ps")
        return [
            LoadedModel(m["name"], int(m.get("size_vram", 0)), int(m.get("size", 0)))
            for m in data.get("models", [])
        ]

    async def chat(
        self,
        model: str,
        messages: list[Message],
        *,
        num_ctx: int,
        think: bool,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[Chunk]:
        """Stream a chat reply as thinking, content, and tool_call chunks."""
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "think": think,
            "options": {"num_ctx": num_ctx},
        }
        if tools:
            body["tools"] = tools
        try:
            async with self._http.stream("POST", "/api/chat", json=body) as resp:
                if resp.status_code != 200:
                    await resp.aread()
                    raise OllamaError(_error_text(resp))
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    data = json.loads(line)
                    if "error" in data:
                        raise OllamaError(str(data["error"]))
                    msg = data.get("message", {})
                    if msg.get("thinking"):
                        yield Chunk("thinking", msg["thinking"])
                    if msg.get("content"):
                        yield Chunk("content", msg["content"])
                    for tc in msg.get("tool_calls") or []:
                        fn = tc.get("function", {})
                        args = fn.get("arguments") or {}
                        if isinstance(args, str):  # some models send a JSON string
                            args = _json_or_empty(args)
                        yield Chunk("tool_call", call=ToolCall(fn.get("name", ""), args))
                    if data.get("done"):
                        if "eval_count" in data:
                            yield Chunk("stats", stats=_stats(data))
                        return
        except httpx.HTTPError as e:
            raise OllamaError(f"can't reach Ollama: {e}") from e

    async def _get(self, path: str) -> dict[str, Any]:
        try:
            resp = await self._http.get(path)
        except httpx.HTTPError as e:
            raise OllamaError(f"can't reach Ollama: {e}") from e
        if resp.status_code != 200:
            raise OllamaError(_error_text(resp))
        result: dict[str, Any] = resp.json()
        return result


def _stats(data: dict[str, Any]) -> Stats:
    return Stats(
        int(data.get("prompt_eval_count", 0)),
        int(data.get("eval_count", 0)),
        int(data.get("eval_duration", 0)) / 1e9,
    )


def _json_or_empty(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _error_text(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("error", resp.text))
    except ValueError:
        return f"HTTP {resp.status_code}: {resp.text}"
