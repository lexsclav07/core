from pathlib import Path

from core.config import FALLBACK_ACCENT, load_config, resolve_accent


def test_first_run_writes_defaults(tmp_path: Path) -> None:
    path = tmp_path / "core" / "config.toml"
    cfg = load_config(path)
    assert path.exists()
    assert cfg.model.main == "qwen3:8b"
    assert cfg.model.num_ctx == 8192
    assert cfg.ollama.url == "http://127.0.0.1:11434"


def test_partial_file_keeps_defaults_and_ignores_unknown_keys(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[model]\nmain = "llama3.1:8b"\nnot_a_setting = 1\n')
    cfg = load_config(path)
    assert cfg.model.main == "llama3.1:8b"
    assert cfg.model.num_ctx == 8192
    assert cfg.ui.terminal == "ptyxis"


def test_configured_accent_wins() -> None:
    assert resolve_accent("#ff00ff") == "#ff00ff"


def test_accent_falls_back_without_gsettings(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("PATH", "")
    assert resolve_accent("") == FALLBACK_ACCENT
