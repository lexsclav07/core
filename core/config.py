"""Load CORE's settings from ~/.config/core/config.toml.

The file is created with defaults on first run so Lex can see every knob.
Secrets never go here; they live in GNOME Keyring (see CLAUDE.md).
"""

from __future__ import annotations

import os
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TOML = """\
# CORE configuration. Secrets do NOT belong here; they go in GNOME Keyring.

[model]
main = "qwen3:8b"
# Context window in tokens. 16k fits on the 8 GB 3060 with Ollama's q8_0 KV cache.
num_ctx = 16384
# qwen3 can "think" before answering. Off by default: in testing (evals/) it was
# 10x slower and less reliable with thinking on (68% vs 88% of tasks done right).
think = false

[ollama]
url = "http://127.0.0.1:11434"

[ui]
terminal = "ptyxis"
# Leave empty to follow the GNOME accent colour.
accent = ""
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
    num_ctx: int = 16384
    think: bool = False


@dataclass(frozen=True)
class OllamaConfig:
    url: str = "http://127.0.0.1:11434"


@dataclass(frozen=True)
class UIConfig:
    terminal: str = "ptyxis"
    accent: str = ""


@dataclass(frozen=True)
class Config:
    model: ModelConfig = field(default_factory=ModelConfig)
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    ui: UIConfig = field(default_factory=UIConfig)


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
    )


def _section[T](cls: type[T], values: dict[str, object]) -> T:
    known = cls.__dataclass_fields__  # type: ignore[attr-defined]
    return cls(**{k: v for k, v in values.items() if k in known})


def resolve_accent(configured: str) -> str:
    """Return the accent colour: the config value, else GNOME's, else blue."""
    if configured:
        return configured
    try:
        out = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "accent-color"],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return FALLBACK_ACCENT
    # gsettings prints the value quoted, e.g. 'purple'
    return GNOME_ACCENTS.get(out.strip().strip("'"), FALLBACK_ACCENT)
