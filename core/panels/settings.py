"""The Settings tab (F5): change the model, thinking and the look without
editing config.toml by hand.

A change applies at once and is saved to config.toml (config.save_setting,
which keeps the file's comments). The rest of the settings are listed with
where to change them: some (like the context size) need a restart, and the
gesture macros stay hand-edited on purpose, so a click can never add one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Label, Select, Static, Switch

from core.agent.audit import audit_path
from core.agent.ollama_client import OllamaError
from core.config import GNOME_ACCENTS, config_path
from core.history import history_path
from core.tools.signs import label

if TYPE_CHECKING:
    from core.app import CoreApp

MODES = [("Follow GNOME", "auto"), ("Dark", "dark"), ("Light", "light")]


def accent_options(current: str) -> list[tuple[str, str]]:
    options = [("Follow GNOME", "")] + [
        (name.title(), hex_) for name, hex_ in GNOME_ACCENTS.items()
    ]
    if current and current not in dict(options).values():
        options.append((f"Custom ({current})", current))  # set by hand in config.toml
    return options


def size(num_bytes: int) -> str:
    return f"{num_bytes / 1e9:.1f} GB"


class SettingsPanel(VerticalScroll):
    def __init__(self) -> None:
        super().__init__(id="settings")

    @property
    def core(self) -> CoreApp:
        return cast("CoreApp", self.app)

    def compose(self) -> ComposeResult:
        config = self.core.config
        yield Static("Model", classes="settings-heading")
        with Horizontal(classes="setting"):
            yield Label("Chat model")
            yield Select([(config.model.main, config.model.main)], value=config.model.main,
                         allow_blank=False, id="set-model")  # fmt: skip
        yield Static("Models you've pulled with `ollama pull`. It has to fit in the "
                     "3060's 8 GB next to Chrome and Discord: about 8B at Q4.",
                     classes="note")  # fmt: skip
        with Horizontal(classes="setting"):
            yield Label("Think before answering")
            yield Switch(value=config.model.think, id="set-think")
        yield Static("Slower, and in CORE's evals less reliable with qwen3: off is "
                     "recommended.", classes="note")  # fmt: skip

        yield Static("Look", classes="settings-heading")
        with Horizontal(classes="setting"):
            yield Label("Light or dark")
            yield Select(MODES, value=config.ui.mode, allow_blank=False, id="set-mode")
        with Horizontal(classes="setting"):
            yield Label("Accent colour")
            yield Select(accent_options(config.ui.accent), value=config.ui.accent,
                         allow_blank=False, id="set-accent")  # fmt: skip

        yield Static("Privacy", classes="settings-heading")
        with Horizontal(classes="setting"):
            yield Label("Save chats for History")
            yield Switch(value=config.history.save, id="set-history")
        yield Static("Kept only on this PC. Turning it off doesn't delete saved chats; "
                     "do that in the History tab.", classes="note")  # fmt: skip

        yield Static("Everything else", classes="settings-heading")
        yield Static(self._other(), id="settings-other")

    def _other(self) -> Text:
        config = self.core.config
        terminal = config.ui.terminal
        rows = [
            ("Context window", f"{config.model.num_ctx} tokens ([model] num_ctx; restart CORE)"),
            ("Vision model", f"{config.model.vision} ([model] vision)"),
            ("Speech to text", f"whisper {config.model.whisper}, on the CPU ([model] whisper)"),
            ("Terminal", f"{terminal} ([ui] terminal)"),
            ("Config file", str(config_path())),
            ("Audit log", str(audit_path())),
            ("Saved chats", str(history_path())),
        ]
        text = Text()
        for name, value in rows:
            text.append(f"{name:<16}", "bold")
            text.append(value + "\n")
        text.append("\nGesture macros", "bold")
        text.append(" (edit [gestures.macros] in the config file, then Ctrl+G)\n")
        for sign, command in config.gestures.macros.items():
            shown = str(command).replace("{terminal}", terminal)
            text.append(f"  {label(sign):<16}{shown}\n")
        if not config.gestures.macros:
            text.append("  none yet\n")
        return text

    def on_mount(self) -> None:
        self.refresh_models()

    @work(exclusive=True, group="models")
    async def refresh_models(self) -> None:
        """Fill the model picker from Ollama (again each time the tab opens)."""
        current = self.core.conversation.model
        picker = self.query_one("#set-model", Select)
        try:
            installed = await self.core.client.installed_models()
        except OllamaError:
            picker.set_options([(f"{current} (Ollama isn't answering)", current)])
            picker.value = current
            return
        options = []
        for m in installed:
            about = " · ".join(x for x in (m.parameters, m.quantization, size(m.size)) if x)
            options.append((f"{m.name}  {about}", m.name))
        if current not in [name for _, name in options]:
            options.insert(0, (f"{current} (not pulled yet)", current))
        with picker.prevent(Select.Changed):  # filling the list isn't Lex choosing
            picker.set_options(options)
            picker.value = current

    def on_select_changed(self, event: Select.Changed) -> None:
        setting = {"set-model": ("model", "main"), "set-mode": ("ui", "mode"),
                   "set-accent": ("ui", "accent")}.get(event.select.id or "")  # fmt: skip
        if setting is not None and event.value is not Select.BLANK:
            self._change(*setting, str(event.value))

    def on_switch_changed(self, event: Switch.Changed) -> None:
        setting = {"set-think": ("model", "think"),
                   "set-history": ("history", "save")}.get(event.switch.id or "")  # fmt: skip
        if setting is not None:
            self._change(*setting, event.value)

    def _change(self, section: str, key: str, value: Any) -> None:
        if getattr(getattr(self.core.config, section), key) == value:
            return  # nothing changed (e.g. the widget was just set up)
        try:
            self.core.change_setting(section, key, value)
        except (OSError, ValueError) as e:
            self.notify(f"Couldn't save that: {e}", severity="warning", markup=False)
            return
        self.notify(f"Saved: [{section}] {key} = {value!r}", markup=False, timeout=3)
