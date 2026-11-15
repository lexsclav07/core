"""Load CORE's settings from ~/.config/core/config.toml.

The file is created with defaults on first run so Lex can see every knob.
Secrets never go here; they live in GNOME Keyring (see CLAUDE.md).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TOML = """\
# CORE configuration. Secrets do NOT belong here; they go in GNOME Keyring.

[model]
main = "qwen3:8b"
# Used for "look at my screen / webcam". Swapped in and out of the GPU on demand.
vision = "qwen3-vl:8b"
# Speech to text for push-to-talk (Ctrl+T), on the CPU: tiny, base, small, medium.
whisper = "small"
# Context window in tokens. 8k keeps the model on the GPU even with Chrome and
# Discord using ~1.4 GB of VRAM (16k pushed 15% of it onto the CPU: 25 vs 37 tok/s).
num_ctx = 8192
# qwen3 can "think" before answering. Off by default: in testing (evals/) it was
# 10x slower and less reliable with thinking on (68% vs 88% of tasks done right).
think = false

[ollama]
url = "http://127.0.0.1:11434"

[ui]
terminal = "ptyxis"
# Leave empty to follow the GNOME accent colour.
accent = ""
# "auto" follows GNOME's light/dark style (Settings → Appearance), or "dark" / "light".
mode = "auto"

[history]
# Save chats to ~/.local/share/core/history.db so the History tab (F3) can
# reopen them. Only on this PC; set to false to keep nothing.
save = true

# Gesture mode: hand signs in front of the webcam run your macros. Ctrl+G turns
# it on and off (only you can, not the model). The webcam light stays on while
# it's on. Edit this, then press Ctrl+G again: no restart needed.
[gestures]
# Seconds you hold a sign before it fires, and the gap before the next one.
hold = 0.6
cooldown = 2.0
# Leave empty to use the first webcam that works, or e.g. "/dev/video0".
camera = ""
# Frames per second to look at. 10 is plenty and keeps CPU use low.
fps = 10
# Turns itself off after this many minutes. 0 = stay on until Ctrl+G.
auto_off = 60

# sign = "command". {terminal} is the terminal from [ui]. Signs: open_palm,
# fist, peace, point, thumbs_up, thumbs_down, rock, call_me, three, or your own
# from [gestures.shapes]. Commands that change things get one y/n each time
# gesture mode turns on; after that they run straight away.
[gestures.macros]
peace = "{terminal} --new-window"
# thumbs_up = "playerctl play-pause"
# fist = "loginctl lock-session"

# Your own signs: name = "the fingers that are up" (the rest are down).
[gestures.shapes]
# gun = "thumb index"
"""

# GNOME's named accent colours (GNOME 47+), mapped to the hex values libadwaita uses.
GNOME_ACCENTS = {
    "blue": "#3584e4",
    "teal": "#2190a4",
    "green": "#3a944a",
    "yellow": "#c88800",
    "orange": "#ed5b00",
    "red": "#e62d42",
    "pink": "#d56199",
    "purple": "#9141ac",
    "slate": "#6f8396",
}
FALLBACK_ACCENT = GNOME_ACCENTS["blue"]


@dataclass(frozen=True)
class ModelConfig:
    main: str = "qwen3:8b"
    vision: str = "qwen3-vl:8b"
    whisper: str = "small"
    num_ctx: int = 8192
    think: bool = False


@dataclass(frozen=True)
class OllamaConfig:
    url: str = "http://127.0.0.1:11434"


@dataclass(frozen=True)
class UIConfig:
    terminal: str = "ptyxis"
    accent: str = ""
    mode: str = "auto"


@dataclass(frozen=True)
class HistoryConfig:
    save: bool = True


def _default_macros() -> dict[str, object]:
    return {"peace": "{terminal} --new-window"}


@dataclass(frozen=True)
class GestureConfig:
    hold: float = 0.6
    cooldown: float = 2.0
    camera: str = ""
    fps: float = 10
    auto_off: float = 60  # minutes; 0 = never
    # Checked when gesture mode starts (core/tools/macros.py, signs.py), so a
    # typo there is reported to Lex instead of stopping CORE from starting.
    macros: dict[str, object] = field(default_factory=_default_macros)
    shapes: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Config:
    model: ModelConfig = field(default_factory=ModelConfig)
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    gestures: GestureConfig = field(default_factory=GestureConfig)
    history: HistoryConfig = field(default_factory=HistoryConfig)


def config_path() -> Path:
    """Respect XDG_CONFIG_HOME so tests (and unusual setups) can redirect it."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "core" / "config.toml"


def load_config(path: Path | None = None) -> Config:
    """Read config.toml, writing the default file first if it doesn't exist.

    Unknown keys are ignored and missing keys fall back to defaults, so an old
    config file keeps working after CORE gains new settings.
    """
    path = path or config_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_TOML)

    with path.open("rb") as f:
        data = tomllib.load(f)

    return Config(
        model=_section(ModelConfig, data.get("model", {})),
        ollama=_section(OllamaConfig, data.get("ollama", {})),
        ui=_section(UIConfig, data.get("ui", {})),
        gestures=_section(GestureConfig, data.get("gestures", {})),
        history=_section(HistoryConfig, data.get("history", {})),
    )


def save_setting(
    section: str, key: str, value: str | bool | int | float, path: Path | None = None
) -> None:
    """Change one setting in config.toml (for the Settings tab), keeping comments.

    The edit is checked by reading the file back: if the new value doesn't come
    out exactly, or anything else changed, nothing is written (ValueError).
    Only simple top-level sections like [model] and [ui], not [gestures.macros].
    """
    if not re.fullmatch(r"\w+", section) or not re.fullmatch(r"\w+", key):
        raise ValueError(f"can't save [{section}] {key} from the Settings tab")
    path = path or config_path()
    if not path.exists():
        load_config(path)  # writes the default file first
    old_text = path.read_text()
    new_text = _with_line(old_text, section, key, _toml_value(value))
    expected = tomllib.loads(old_text)
    expected.setdefault(section, {})[key] = value
    if tomllib.loads(new_text) != expected:  # TOMLDecodeError is a ValueError too
        raise ValueError(f"couldn't safely change [{section}] {key} in {path}")
    partial = path.with_suffix(".toml.part")
    partial.write_text(new_text)
    partial.replace(path)  # atomic: never a half-written config


def _toml_value(value: str | bool | int | float) -> str:
    if isinstance(value, bool):  # before int: True is an int too
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    return json.dumps(value, ensure_ascii=False)  # a JSON string is a valid TOML string


def _with_line(text: str, section: str, key: str, value: str) -> str:
    """`key = value` set in [section]: replaced in place, else added under the header."""
    lines = text.splitlines()
    header = next((i for i, line in enumerate(lines) if line.strip() == f"[{section}]"), None)
    if header is None:
        return text.rstrip("\n") + f"\n\n[{section}]\n{key} = {value}\n"
    end = next((i for i in range(header + 1, len(lines)) if lines[i].lstrip().startswith("[")),
               len(lines))  # fmt: skip
    setting = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for i in range(header + 1, end):
        if setting.match(lines[i]):
            lines[i] = f"{key} = {value}"
            break
    else:
        lines.insert(header + 1, f"{key} = {value}")
    return "\n".join(lines) + "\n"


def _section[T](cls: type[T], values: dict[str, object]) -> T:
    known = cls.__dataclass_fields__  # type: ignore[attr-defined]
    return cls(**{k: v for k, v in values.items() if k in known})


def resolve_accent(configured: str) -> str:
    """Return the accent colour: the config value, else GNOME's, else blue."""
    if configured:
        return configured
    return GNOME_ACCENTS.get(_gnome_setting("accent-color"), FALLBACK_ACCENT)


def resolve_dark(mode: str) -> bool:
    """Dark or light. "auto" asks GNOME; if it can't tell, dark."""
    if mode in ("dark", "light"):
        return mode == "dark"
    # GNOME's "default" means the app picks; for a terminal app that's dark.
    return _gnome_setting("color-scheme") != "prefer-light"


def _gnome_setting(key: str) -> str:
    try:
        out = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", key],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.strip().strip("'")  # gsettings prints strings quoted, e.g. 'purple'
