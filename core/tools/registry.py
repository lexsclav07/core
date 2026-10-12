"""Turn typed Python functions into tools the model can call.

A tool is an async function with type hints and a Google-style docstring:

    @tool
    async def run_command(ctx: ToolContext, command: str, timeout: int = 120) -> ToolResult:
        '''One-line summary the model sees.

        Args:
            command: What each argument means, also sent to the model.
        '''

The decorator reads the signature and docstring and builds the JSON schema
Ollama expects, so the description can never drift from the code. The `ctx`
parameter is filled in by the agent loop and hidden from the model.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, get_type_hints

if TYPE_CHECKING:
    from core.agent.risk import Assessment
    from core.tools.pty_runner import CommandResult, RunningCommand


@dataclass(frozen=True)
class ToolResult:
    text: str  # what the model sees
    # True if `text` contains outside content (a file, web page, log).
    # The loop then sends later tool calls this turn to the confirm tier.
    untrusted: bool = False


class CommandView(Protocol):
    """One command's block in the chat."""

    def attach(self, running: RunningCommand) -> None: ...
    def output(self, text: str) -> None: ...
    def finish(self, status: str, result: CommandResult | None = None) -> None: ...


class AgentUI(Protocol):
    """What tools need from the TUI. Tests use a fake."""

    async def approve(
        self, command: str, assessment: Assessment, explanation: str, undo: str
    ) -> bool: ...
    async def ask_password(self) -> str | None: ...
    def command_view(self, command: str, assessment: Assessment) -> CommandView: ...


@dataclass
class ToolContext:
    ui: AgentUI
    cwd: str
    session: str  # "local" or "remote"
    # Set once anything untrusted has entered this turn (rule 6 in CLAUDE.md).
    tainted: bool = False


ToolFunc = Callable[..., Awaitable[ToolResult]]
PY_TO_JSON = {str: "string", int: "integer", float: "number", bool: "boolean"}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    func: ToolFunc

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    async def call(self, ctx: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        """Validate the model's arguments, then run the tool."""
        props = self.parameters["properties"]
        unknown = set(arguments) - set(props)
        missing = set(self.parameters["required"]) - set(arguments)
        if unknown or missing:
            problems = [f"unknown argument {u!r}" for u in sorted(unknown)]
            problems += [f"missing argument {m!r}" for m in sorted(missing)]
            return ToolResult(f"Error calling {self.name}: {', '.join(problems)}.")
        args = {k: _coerce(v, props[k]["type"]) for k, v in arguments.items()}
        return await self.func(ctx, **args)


def tool(func: ToolFunc) -> Tool:
    """Decorator: build a Tool from a function's signature and docstring."""
    doc = inspect.getdoc(func) or ""
    summary, arg_docs = _parse_docstring(doc)
    hints = get_type_hints(func)
    props: dict[str, Any] = {}
    required: list[str] = []
    for name, param in inspect.signature(func).parameters.items():
        if name == "ctx":
            continue
        if name not in arg_docs:
            raise TypeError(f"{func.__name__}: argument {name!r} needs a line under Args:")
        props[name] = {"type": PY_TO_JSON[hints[name]], "description": arg_docs[name]}
        if param.default is inspect.Parameter.empty:
            required.append(name)
    parameters = {"type": "object", "properties": props, "required": required}
    return Tool(func.__name__, summary, parameters, func)


def _parse_docstring(doc: str) -> tuple[str, dict[str, str]]:
    """Split a docstring into its description and its Args: section."""
    if "Args:" not in doc:
        return doc.strip(), {}
    summary, args_block = doc.split("Args:", 1)
    args: dict[str, str] = {}
    current = None
    for line in args_block.splitlines():
        m = re.match(r"^\s{2,}(\w+):\s*(.*)$", line)
        if m and (current is None or not line.startswith(" " * 8)):
            current = m.group(1)
            args[current] = m.group(2)
        elif current and line.strip():
            args[current] += " " + line.strip()
    return " ".join(summary.split()), args


def _coerce(value: Any, json_type: str) -> Any:
    """Small models sometimes send "120" for an integer; accept that."""
    try:
        if json_type == "integer" and not isinstance(value, bool):
            return int(value)
        if json_type == "number":
            return float(value)
        if json_type == "string" and not isinstance(value, str):
            return str(value)
    except (TypeError, ValueError):
        pass
    return value
