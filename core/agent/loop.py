"""The conversation loop.

Phase 1: plain chat. It keeps the history and streams replies. Phase 2 turns
this into the tool-calling loop: send history + tool schemas, run any tool
calls through risk.py, feed the results back, and repeat.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from core.agent.ollama_client import Chunk, Message, OllamaClient
from core.agent.prompts import SYSTEM_PROMPT


class Conversation:
    def __init__(self, client: OllamaClient, model: str, *, num_ctx: int, think: bool) -> None:
        self.client = client
        self.model = model
        self.num_ctx = num_ctx
        self.think = think
        self.history: list[Message] = [{"role": "system", "content": SYSTEM_PROMPT}]

    async def send(self, text: str) -> AsyncIterator[Chunk]:
        """Add Lex's message, stream the reply, and record it in history.

        If the stream is cancelled (Ctrl+C) or fails partway, whatever arrived
        is still saved, so the model knows what it already said. If nothing
        arrived at all, the whole turn is dropped so history never holds an
        empty reply.
        """
        self.history.append({"role": "user", "content": text})
        reply: list[str] = []
        try:
            async for chunk in self.client.chat(
                self.model, self.history, num_ctx=self.num_ctx, think=self.think
            ):
                if chunk.kind == "content":
                    reply.append(chunk.text)
                yield chunk
        finally:
            # Thinking isn't saved: Ollama's qwen3 template drops it from past
            # turns anyway, and it would eat the context window.
            if reply:
                self.history.append({"role": "assistant", "content": "".join(reply)})
            else:
                self.history.pop()
