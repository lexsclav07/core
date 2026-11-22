<p align="center">
  <img src="docs/logo.png" alt="CORE logo" width="240">
</p>

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

It looks and works like Claude Code: black with a light green accent, your
messages as `> …`, CORE's replies after a `⏺`, and each command it runs as
`⏺ Bash(…)` with its output under `⎿`. While it works, a line above the input
says so: `✻ Thinking… (12s · ↓ 340 tokens · esc to interrupt)`.

| Key | |
|---|---|
| Enter | send (click a tip on the welcome box to fill the input first) |
| / | the command menu: ↑/↓ to pick, Enter to run |
| ? or F1 | shortcuts |
| ↑ / ↓ | bring back earlier messages |
| Esc | interrupt CORE, close the / menu, or go back to the chat |
| Ctrl+C | stop the running command, then the reply |
| Ctrl+O | expand the latest command's output (or click it) |
| Ctrl+T | push-to-talk |
| Ctrl+G | gesture mode |
| Ctrl+L | new chat (same as /clear) |
| Ctrl+P | every command, plus Textual's theme picker |
| Ctrl+Q | quit |

### Commands

| | |
|---|---|
| `/clear` | new chat; CORE forgets this one and frees its memory (it stays in /resume) |
| `/resume` (F3) | every saved chat: type to search what was said, Enter to carry one on (CORE remembers it all again), Delete to remove one |
| `/activity` (F4) | the audit log as a table: everything CORE ran, asked about or refused, with a filter box and the full details of the highlighted row |
| `/settings` (F5) | the model, thinking, light/dark and the accent colour, saved to `config.toml` with its comments kept |
| `/model` | /settings with the model list open |
| `/talk`, `/signs` | push-to-talk, gesture mode |
| `/help`, `/quit` | |

Chats are saved in `~/.local/share/core/history.db` after each reply, on this
PC only; `save = false` under `[history]` turns that off. Gesture macros stay
hand-edited in `config.toml` on purpose, so a click in /settings can never add
one.

Anything else starting with `/` (say "/etc/fstab looks wrong") goes to CORE as
a normal message. While a command is running, whatever you type goes to that
command (e.g. to answer `[y/N]`).

When CORE wants to run something that changes the PC, a box at the bottom
shows the command, what it does, why it's asking and how to undo it, with
`1. Yes` / `2. No`. A Yes only counts after half a second, so a key you were
already typing can't approve it by accident.

The line under the input shows hints on the left and, on the right, the model
(green dot: fully on the GPU; yellow: part of it spilled into system RAM, so
it's slower), **ctx** (how much of the chat CORE can still keep in mind;
yellow, then red with a /clear hint, as it fills) and VRAM. Red means a sense
is on. After a reply it also says how long it took and how fast the model wrote.

If you're in another window, CORE sends a GNOME notification when it needs a
y/n, or when a reply that took over 10 seconds is ready.

### The logo

`docs/logo.png` is the logo. The splash screen and welcome box show it as
block characters, made from the image (40 and 20 characters wide):

```sh
uv run python -m core.logo.from_image docs/logo.png --width 40 > core/logo/logo.txt
uv run python -m core.logo.from_image docs/logo.png --width 20 > core/logo/small.txt
```

Change the image and run those again; CORE picks up the new files on its next
start. Extra `core/logo/frame_01.txt`, `frame_02.txt`, … play as a splash
animation.

### Ricing the TUI

CORE is black with a light green accent (`#9be9a8`, the lightest square of a
GitHub contribution graph). In /settings, or under `[ui]` in `config.toml`:
`accent = "gnome"` follows your GNOME accent colour, `accent = "#hex"` picks your
own, and `mode = "light"` or `"auto"` (GNOME's style) changes the black.
Everything else about the look is in `core/theme.tcss` (Textual CSS). To see
your edits live:

```sh
uv run textual run --dev core.app:CoreApp
```

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
