# CORE

A local AI assistant for Fedora that lives in the terminal. See `CLAUDE.md` for the full design.

## Setup (phase 1)

1. Ollama with CUDA, running as `ollama.service`, with the drop-in
   `/etc/systemd/system/ollama.service.d/core.conf`:
   ```ini
   [Service]
   Environment="OLLAMA_FLASH_ATTENTION=1"
   Environment="OLLAMA_KV_CACHE_TYPE=q8_0"
   Environment="OLLAMA_KEEP_ALIVE=30m"
   ```
2. `ollama pull qwen3:8b`
3. `uv sync`

## Run

```sh
uv run core
```

The first run writes `~/.config/core/config.toml` with the defaults.

Keys: Enter sends, Ctrl+C (or Esc) stops a reply, Ctrl+Q quits.

## Check

```sh
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict core/agent
uv run pytest
```
