from pathlib import Path

import pytest

from core.config import FALLBACK_ACCENT, load_config, resolve_accent, save_setting


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


def test_light_or_dark(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from core.config import resolve_dark

    assert resolve_dark("dark") and not resolve_dark("light")
    monkeypatch.setenv("PATH", "")  # no gsettings: CORE can't ask GNOME
    assert resolve_dark("auto")


def test_save_setting_keeps_comments_and_everything_else(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    load_config(path)  # the default file, full of comments
    before = path.read_text()
    save_setting("model", "main", "llama3.1:8b", path)
    save_setting("model", "think", True, path)
    save_setting("ui", "mode", "light", path)
    cfg = load_config(path)
    assert (cfg.model.main, cfg.model.think, cfg.ui.mode) == ("llama3.1:8b", True, "light")
    assert cfg.gestures.macros == {"peace": "{terminal} --new-window"}  # untouched
    after = path.read_text()
    assert after.count("#") == before.count("#")  # every comment still there
    assert 'main = "llama3.1:8b"' in after


def test_save_setting_adds_missing_keys_and_sections(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[model]\nmain = "qwen3:8b"\n')
    save_setting("model", "num_ctx", 4096, path)
    save_setting("history", "save", False, path)
    cfg = load_config(path)
    assert cfg.model.num_ctx == 4096 and cfg.history.save is False


def test_save_setting_refuses_what_it_cannot_do_safely(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[model]\nmain = "qwen3:8b"\n')
    with pytest.raises(ValueError):
        save_setting("gestures.macros", "peace", "rm -rf ~", path)  # nested: not from here
    assert path.read_text() == '[model]\nmain = "qwen3:8b"\n'


def test_save_setting_quotes_strings_properly(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[ui]\n")
    tricky = 'say "hi" \\ — ünïcode'
    save_setting("ui", "terminal", tricky, path)
    assert load_config(path).ui.terminal == tricky
