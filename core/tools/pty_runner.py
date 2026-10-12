"""Run one shell command inside a pseudo-terminal (PTY).

Why a PTY instead of plain pipes: programs behave as if a person is at a
terminal. sudo can prompt for a password, progress bars work, and prompts
like dnf's "Is this ok [y/N]" appear so Lex can answer them.

How sudo passwords stay secret:
  * We set SUDO_PROMPT to a marker string. When the marker shows up in the
    output, we ask the TUI for the password (a masked field) and write it
    straight into the PTY. sudo turns echo off, so it never comes back out.
  * The password is never stored, logged, put in history, or sent to the model.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import fcntl
import os
import pty
import re
import signal
import struct
import termios
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

SUDO_MARKER = "[[CORE-SUDO-PASSWORD]]"
KILL_GRACE_S = 2.0
MAX_KEPT = 1_000_000  # characters of raw output kept in memory


@dataclass(frozen=True)
class CommandResult:
    exit_code: int | None  # None if killed or timed out
    output: str  # cleaned: no colour codes, progress bars collapsed
    duration_s: float
    timed_out: bool = False
    killed: bool = False


class RunningCommand:
    """A live command. The TUI uses this to type into it or stop it."""

    def __init__(self, master_fd: int) -> None:
        self._fd = master_fd
        self.kill_requested = asyncio.Event()

    def send_line(self, text: str) -> None:
        """Type a line into the command, as if at the keyboard."""
        os.write(self._fd, text.encode() + b"\n")

    def kill(self) -> None:
        self.kill_requested.set()


async def run_in_pty(
    command: str,
    *,
    cwd: str,
    time_limit: float,
    on_output: Callable[[str], None],
    ask_password: Callable[[], Awaitable[str | None]],
    on_start: Callable[[RunningCommand], None] = lambda _: None,
) -> CommandResult:
    """Run `command` with bash in a fresh PTY and wait for it to finish."""
    master, slave = pty.openpty()
    _set_size(slave, rows=50, cols=160)
    env = {
        **os.environ,
        "TERM": "dumb",  # fewer escape codes to clean up
        "NO_COLOR": "1",
        "PAGER": "cat",  # pagers would sit waiting for a keypress
        "GIT_PAGER": "cat",
        "MANPAGER": "cat",
        "SYSTEMD_PAGER": "",
        "SUDO_PROMPT": SUDO_MARKER,
    }
    started = time.monotonic()
    # `setsid --ctty` gives the command its own session with this PTY as its
    # controlling terminal. sudo reads passwords from the controlling
    # terminal, and the session lets us kill the whole process group.
    proc = await asyncio.create_subprocess_exec(
        "setsid", "--ctty", "bash", "-c", command,
        stdin=slave, stdout=slave, stderr=slave, cwd=cwd, env=env,
    )  # fmt: skip
    os.close(slave)
    running = RunningCommand(master)
    on_start(running)

    loop = asyncio.get_running_loop()
    chunks: asyncio.Queue[bytes | None] = asyncio.Queue()

    def readable() -> None:
        try:
            data = os.read(master, 65536)
        except OSError:  # EIO: every process holding the PTY has exited
            data = b""
        if not data:
            loop.remove_reader(master)
        chunks.put_nowait(data or None)

    loop.add_reader(master, readable)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    raw = _Capped(MAX_KEPT)
    pending = ""  # text held back because it might be the start of the marker
    deadline = loop.time() + time_limit
    exited = asyncio.ensure_future(proc.wait())
    killer = asyncio.ensure_future(running.kill_requested.wait())
    timed_out = killed = False

    try:
        while True:
            get = asyncio.ensure_future(chunks.get())
            done, _ = await asyncio.wait(
                {get, exited, killer},
                timeout=max(0.0, deadline - loop.time()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if get in done:
                data = get.result()
                if data is None:
                    break
                text = pending + decoder.decode(data)
                while SUDO_MARKER in text:
                    before, text = text.split(SUDO_MARKER, 1)
                    _emit(before, raw, on_output)
                    waited = time.monotonic()
                    password = await ask_password()
                    deadline += time.monotonic() - waited  # typing time doesn't count
                    # Ctrl+C makes sudo give up if Lex cancelled the prompt.
                    os.write(master, password.encode() + b"\n" if password is not None else b"\x03")
                    del password
                text, pending = _hold_back(text)
                _emit(text, raw, on_output)
                continue
            get.cancel()
            if exited in done:
                await _drain(chunks, decoder, raw, on_output)
                break
            killed = killer in done
            timed_out = not killed
            await _stop(proc)
            break
    finally:
        loop.remove_reader(master)
        killer.cancel()
        os.close(master)

    _emit(pending, raw, on_output)
    code = await proc.wait()
    if code < 0:
        code = 128 - code  # killed by signal N: report it like bash does (128 + N)
    return CommandResult(
        exit_code=None if (killed or timed_out) else code,
        output=clean_output(raw.text()),
        duration_s=round(time.monotonic() - started, 2),
        timed_out=timed_out,
        killed=killed,
    )


def _emit(text: str, raw: _Capped, on_output: Callable[[str], None]) -> None:
    if text:
        raw.add(text)
        on_output(text)


def _hold_back(text: str) -> tuple[str, str]:
    """Split off a tail that could be the first part of SUDO_MARKER."""
    for n in range(min(len(text), len(SUDO_MARKER) - 1), 0, -1):
        if SUDO_MARKER.startswith(text[-n:]):
            return text[:-n], text[-n:]
    return text, ""


async def _drain(
    chunks: asyncio.Queue[bytes | None],
    decoder: codecs.IncrementalDecoder,
    raw: _Capped,
    on_output: Callable[[str], None],
) -> None:
    """After exit, collect what's left (a background job may hold the PTY open)."""
    while True:
        try:
            data = await asyncio.wait_for(chunks.get(), timeout=0.2)
        except TimeoutError:
            return
        if data is None:
            return
        _emit(decoder.decode(data), raw, on_output)


async def _stop(proc: asyncio.subprocess.Process) -> None:
    """SIGTERM the command's whole process group, then SIGKILL if needed."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, sig)
        try:
            await asyncio.wait_for(proc.wait(), timeout=KILL_GRACE_S)
            return
        except TimeoutError:
            continue


def _set_size(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class _Capped:
    """Keeps the start and the most recent part of very long output."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.head = ""
        self.tail = ""
        self.dropped = False

    def add(self, text: str) -> None:
        if len(self.head) < self.limit // 4:
            take = self.limit // 4 - len(self.head)
            self.head += text[:take]
            text = text[take:]
        self.tail += text
        if len(self.tail) > self.limit:
            self.tail = self.tail[-self.limit :]
            self.dropped = True

    def text(self) -> str:
        gap = "\n[… output trimmed …]\n" if self.dropped else ""
        return self.head + gap + self.tail


_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


def clean_output(raw: str) -> str:
    """Strip colour codes; for progress bars keep only the final state."""
    text = _ANSI.sub("", raw).replace("\r\n", "\n")
    lines = [line.rsplit("\r", 1)[-1] for line in text.split("\n")]
    return "\n".join(lines).strip("\n")


def summarise(output: str, head: int = 1500, tail: int = 2500) -> str:
    """Trim output for the model: the start and the end matter most."""
    if len(output) <= head + tail:
        return output
    skipped = len(output) - head - tail
    return f"{output[:head]}\n[… {skipped} characters skipped …]\n{output[-tail:]}"
