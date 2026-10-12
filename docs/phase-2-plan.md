# Plan: CORE Phase 2 (shell tool, risk classifier, confirmations, audit log)

Decisions from Lex (2026-10-12):
- Untrusted content: **taint by command**. Output from content-reading commands (cat, head, tail, less, grep, rg, journalctl, curl, wget, find, git log/show/diff, …) taints the turn, and every later tool call in that turn goes to confirm. Status commands (nvidia-smi, systemctl status, …) don't taint.
- Unknown commands: **confirm**. The safe tier is an allowlist.

## Modules
1. `core/agent/risk.py`: parse with tree-sitter-bash (MIT). Classify each simple command, recursing into `bash -c` / `sh -c` / `eval` / `sudo` / `$(…)` / backticks. The line gets the worst tier of its parts. A parse error means confirm.
   - Safe: allowlist plus flag rules (`find -delete/-exec`, `sed -i`, `>` outside /tmp and CORE's own dirs all go to confirm).
   - Blocked:
     - `rm -r` on system paths or home, or `--no-preserve-root`
     - `dd`/`mkfs` onto a mounted or system disk (checked with lsblk)
     - permanently disabling SELinux
     - `chmod -R 777 /`
     - remote script piped into a root shell
     - exposing ports publicly (tailscale funnel, ngrok, cloudflared tunnel, UPnP forwards, opening ports in the public firewall zone)
   - Tests: `tests/test_risk.py`, table-driven, about 100 cases.
2. `core/agent/audit.py`: JSON lines in `~/.local/share/core/audit.log` with time, command, tier, reason, approval (auto/lex/denied/blocked), local or remote, exit code, duration.
3. `core/tools/shell.py`: `run_command(command, explanation, undo, timeout=120)` in a stdlib pty with asyncio.
   - Pagers off (`PAGER=cat` etc.).
   - Live output streamed to the TUI; the model gets a trimmed head+tail copy of about 4k characters.
   - Timeout or silent wait for input: Lex can send a line or kill the command.
   - Sudo prompt: detected, then a masked modal writes the password straight to the PTY. Never stored, logged or sent to the model, and the prompt line is stripped from output.
   - pkexec over SSH: CORE says it won't work and suggests sudo.
4. `core/tools/registry.py`: a `@tool` decorator that builds the Ollama schema from type hints and the docstring.
5. `core/agent/loop.py`: the tool loop (tool_calls → risk → confirm → run → audit → tool message), capped at 10 rounds.
6. `core/app.py`:
   - Confirm modal: raw command, tier, reason, explanation, undo, y/n. Blocked commands get a refusal card.
   - Collapsible command panel (Ctrl+O) with exit-code badges.

## Order
risk + tests → audit → PTY runner + tests → registry + loop → TUI → end-to-end test: "what GPU do I have" runs automatically, "install htop" asks first, `rm -rf ~` is refused.
