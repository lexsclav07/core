"""These run real (harmless) commands in a PTY."""

import os

from core.tools.pty_runner import (
    SUDO_MARKER,
    CommandResult,
    RunningCommand,
    clean_output,
    run_in_pty,
    summarise,
)

CWD = os.getcwd()


async def run(
    command: str, time_limit: float = 10, password: str | None = None, **kw
) -> tuple[CommandResult, list[str], int]:  # type: ignore[no-untyped-def]
    seen: list[str] = []
    asked = 0

    async def ask() -> str | None:
        nonlocal asked
        asked += 1
        return password

    result = await run_in_pty(
        command, cwd=CWD, time_limit=time_limit, on_output=seen.append, ask_password=ask, **kw
    )
    return result, seen, asked


async def test_output_and_exit_code() -> None:
    result, seen, _ = await run("echo hello; echo oops >&2; exit 3")
    assert result.exit_code == 3
    assert result.output == "hello\noops"
    assert "hello" in "".join(seen)  # streamed live too


async def test_has_a_controlling_terminal() -> None:
    result, _, _ = await run("tty")
    assert result.output.startswith("/dev/pts/")


async def test_timeout_kills_the_process_group() -> None:
    result, _, _ = await run("sleep 30 & sleep 30", time_limit=0.5)
    assert result.timed_out and result.exit_code is None
    assert result.duration_s < 5


async def test_kill_from_the_tui() -> None:
    result, _, _ = await run("sleep 30", on_start=lambda r: r.kill())
    assert result.killed and result.exit_code is None


async def test_send_line_answers_a_prompt() -> None:
    def answer(r: RunningCommand) -> None:
        r.send_line("y")

    result, _, _ = await run('read -r answer; echo "got $answer"', on_start=answer)
    assert result.output.endswith("got y")


async def test_password_prompt_goes_to_the_pty_not_the_output() -> None:
    # Stand-in for sudo: print the prompt, read a hidden line, report its length.
    script = 'printf "%s" "$SUDO_PROMPT"; read -rs pw; echo "len=${#pw}"'
    result, seen, asked = await run(script, password="hunter2")
    assert asked == 1
    assert "len=7" in result.output
    assert SUDO_MARKER not in result.output and SUDO_MARKER not in "".join(seen)
    assert "hunter2" not in result.output


async def test_cancelled_password_sends_ctrl_c() -> None:
    script = 'printf "%s" "$SUDO_PROMPT"; read -rs pw; echo should-not-print'
    result, _, _ = await run(script, password=None)
    assert result.exit_code == 130  # interrupted by Ctrl+C
    assert "should-not-print" not in result.output


async def test_pagers_are_disabled() -> None:
    result, _, _ = await run("echo $PAGER $GIT_PAGER")
    assert result.output == "cat cat"


def test_clean_output_strips_colour_and_progress_bars() -> None:
    raw = "\x1b[32mok\x1b[0m\r\n 10%\r 50%\r100%\r\ndone"
    assert clean_output(raw) == "ok\n100%\ndone"


def test_summarise_keeps_head_and_tail() -> None:
    text = "a" * 3000 + "b" * 3000
    out = summarise(text, head=100, tail=100)
    assert out.startswith("a" * 100) and out.endswith("b" * 100)
    assert "5800 characters skipped" in out
