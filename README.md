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

Keys: Enter sends, Ctrl+C (or Esc) stops the running command, then the reply.
Ctrl+O expands the latest command's output, Ctrl+Q quits. While a command is
running, whatever you type goes to that command (e.g. to answer `[y/N]`).

## Senses

All off until you ask, shown in red in the status bar while on.

- **Screen**: "look at my screen…". CORE asks y/n, GNOME takes a screenshot of
  all monitors (the first time GNOME asks whether CORE may take screenshots),
  the local vision model (`qwen3-vl:8b`) looks at it, and it's deleted.
  Say "and keep it" to save it in `~/Pictures/core/`.
- **Webcam**: "what's on my webcam…". Same flow, one photo.
- **Mic**: press **Ctrl+T** to start recording, Ctrl+T or Enter to stop. Your
  words are transcribed on the CPU and appear in the input box so you can check
  them before sending. The model can't turn the mic on; only you can.

The vision model and the chat model don't both fit in 8 GB, so the first look
takes a few seconds while Ollama swaps them.

## Safety

Every command CORE runs is checked by `core/agent/risk.py`:

- **safe** (read-only or user-scope) runs straight away
- **confirm** shows a dialog with the command, why it's asking, what it does
  and how to undo it, and waits for y/n
- **blocked** is refused (wiping the system, writing to live disks, turning
  SELinux off for good, `curl | sudo sh`, public tunnels)

After CORE reads outside content (files, web pages, logs) in a turn, every
later command in that turn needs a y/n. Everything is logged to
`~/.local/share/core/audit.log`:

```sh
tail -f ~/.local/share/core/audit.log
```

## Check

```sh
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict core/agent
uv run pytest
```

## Evals (how reliable is CORE?)

`evals/` runs real requests against the real model and checks what CORE did.
Read-only commands really run; anything that would change the PC is auto-denied
and only recorded, so an eval run never changes anything.

```sh
uv run python -m evals.run                # all tasks, 3 tries each
uv run python -m evals.run --think both   # compare thinking on vs off
```

Add a task to `evals/tasks.py` whenever CORE gets something wrong in real use.
