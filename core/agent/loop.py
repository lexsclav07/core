"""The agent loop: chat with the model and run the tools it asks for.

One turn (one message from Lex) goes like this:

    1. send history + tool schemas to Ollama, stream the reply
    2. no tool calls?   → done, the reply is the answer
    3. tool calls?      → run each (shell.py does risk / approval / audit),
                          add the results to history, go back to 1

It stops after MAX_ROUNDS so a confused model can't loop forever.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from core.agent.ollama_client import Chunk, Message, OllamaClient, ToolCall
from core.agent.prompts import SYSTEM_PROMPT
from core.tools.registry import AgentUI, Tool, ToolContext, ToolResult

MAX_ROUNDS = 10


class Conversation:
    def __init__(
        self,
        client: OllamaClient,
        model: str,
        *,
        num_ctx: int,
        think: bool,
        tools: Sequence[Tool] = (),
        ui: AgentUI | None = None,
        cwd: str = ".",
        session: str = "local",
    ) -> None:
        self.client = client
        self.model = model
        self.num_ctx = num_ctx
        self.think = think
        self.tools = {t.name: t for t in tools}
        self.ui = ui
        self.cwd = cwd
        self.session = session
        self.history: list[Message] = [{"role": "system", "content": SYSTEM_PROMPT}]

    async def send(self, text: str) -> AsyncIterator[Chunk]:
        """Run one turn, streaming text chunks to the caller.

        History always stays valid for Ollama, even if Lex cancels halfway: a
        partial reply is kept, and any tool call that didn't get to run gets
        a "cancelled" result. If nothing came back at all, the turn is dropped.
        """
        start = len(self.history)
        self.history.append({"role": "user", "content": text})
        ctx = ToolContext(self.ui, self.cwd, self.session) if self.ui else None
        try:
            for _ in range(MAX_ROUNDS):
                calls: list[ToolCall] = []
                async for chunk in self._stream_reply(calls):
                    yield chunk
                if not calls:
                    return
                await self._run_calls(calls, ctx)
                yield Chunk("break")
            yield Chunk("content", f"\n\n*(Stopped after {MAX_ROUNDS} rounds of commands.)*")
        finally:
            if len(self.history) == start + 1:
                del self.history[start:]

    async def _stream_reply(self, calls: list[ToolCall]) -> AsyncIterator[Chunk]:
        """Stream one model reply; collect its tool calls into `calls`."""
        reply: list[str] = []
        schemas = [t.schema() for t in self.tools.values()]
        try:
            async for chunk in self.client.chat(
                self.model, self.history, num_ctx=self.num_ctx, think=self.think, tools=schemas
            ):
                if chunk.kind == "tool_call" and chunk.call:
                    calls.append(chunk.call)
                    continue
                if chunk.kind == "content":
                    reply.append(chunk.text)
                yield chunk
        finally:
            # Thinking isn't saved: Ollama drops it from past turns anyway,
            # and it would eat the context window.
            if reply or calls:
                msg: dict[str, Any] = {"role": "assistant", "content": "".join(reply)}
                if calls:
                    msg["tool_calls"] = [
                        {"function": {"name": c.name, "arguments": c.arguments}} for c in calls
                    ]
                self.history.append(msg)

    async def _run_calls(self, calls: list[ToolCall], ctx: ToolContext | None) -> None:
        done = 0
        try:
            for call in calls:
                result = await self._run_one(call, ctx)
                if ctx and result.untrusted:
                    ctx.tainted = True
                self._add_result(call, result.text)
                done += 1
        finally:
            for call in calls[done:]:
                self._add_result(call, "Cancelled: Lex stopped CORE before this ran.")

    async def _run_one(self, call: ToolCall, ctx: ToolContext | None) -> ToolResult:
        tool = self.tools.get(call.name)
        if tool is None or ctx is None:
            names = ", ".join(self.tools) or "none"
            return ToolResult(f"Error: there is no tool called {call.name!r}. Tools: {names}.")
        return await tool.call(ctx, call.arguments)

    def _add_result(self, call: ToolCall, text: str) -> None:
        self.history.append({"role": "tool", "tool_name": call.name, "content": text})
