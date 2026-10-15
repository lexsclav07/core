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

## Hand signs (gesture mode)

Press **Ctrl+G** and CORE watches the webcam for hand signs. Each sign runs a
macro, a command you set in `~/.config/core/config.toml`. Out of the box,
✌ peace opens a new terminal window. Press Ctrl+G again to turn it off. It
also turns itself off after an hour (`auto_off`).

```toml
[gestures.macros]
peace = "{terminal} --new-window"      # {terminal} is the one in [ui]
thumbs_up = "playerctl play-pause"
fist = "loginctl lock-session"
gun = "flatpak run org.mozilla.firefox"

[gestures.shapes]                      # your own signs: the fingers that are up
gun = "thumb index"
```

Signs: ✋ `open_palm`, ✊ `fist`, ✌ `peace`, ☝ `point`, 👍 `thumbs_up`,
👎 `thumbs_down`, 🤘 `rock`, 🤙 `call_me`, `three`, plus your own shapes.
Hold a sign for about half a second and it fires once. To fire it again, drop
your hand and make the sign again. The status bar shows the sign CORE sees
right now, which helps when a sign isn't being picked up. After editing the
config, press Ctrl+G twice; you don't need to restart CORE.

- **Safety**: macros go through the same checks as every other command.
  Macros that change something (opening an app counts) get one y/n each when
  gesture mode turns on, and after that they run as soon as you make the sign.
  Blocked commands are refused, and so are `sudo` macros, because there's no
  terminal for the password. Every run shows in the chat and goes in the audit log.
- **Privacy**: tracking runs on the CPU in a separate process
  (`core/tools/hand_worker.py`), and frames are never saved or sent anywhere.
  The first time, CORE downloads MediaPipe's hand model (7.8 MB) and checks
  it against a fixed SHA-256.
- **Only at the PC**: CORE refuses gesture mode over SSH, because whoever is in
  front of the PC's webcam would be the one making the signs.
- **One app at a time**: the webcam is busy while gesture mode is on, so turn
  it off before asking "what's on my webcam".
- **No webcam to test with?** Set `camera = "some-video.mp4"` under
  `[gestures]` and it reads the video instead.

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
