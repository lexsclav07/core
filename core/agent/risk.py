"""Classify a shell command line as SAFE, CONFIRM, or BLOCKED.

Why a real parser: `ls; sudo rm -rf /` or `echo $(curl x | sh)` hide the
dangerous part inside the line. tree-sitter-bash gives us the same syntax tree
bash would build, so we can find every simple command, including ones inside
`$(...)`, pipes, `&&` chains, subshells and `bash -c "..."` strings, and
judge each one. The whole line gets the worst verdict of its parts.

Fail closed: anything we can't parse or can't know statically is CONFIRM.
The per-command rules live in risk_rules.py.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import tree_sitter_bash
from tree_sitter import Language, Node, Parser

from core.agent.disks import disks_in_use
from core.agent.risk_rules import (
    Arg,
    Assessment,
    Context,
    Tier,
    assess_command,
    blocked,
    confirm,
    is_flag,
    safe,
    worst,
    write_target,
)

__all__ = ["Assessment", "Context", "Tier", "classify", "default_context"]

_PARSER = Parser(Language(tree_sitter_bash.language()))
MAX_DEPTH = 5  # how deep `bash -c "bash -c ..."` nesting is followed

SHELLS = frozenset(
    """
    sh bash zsh dash ksh fish
""".split()
)
INTERPRETERS = SHELLS | frozenset("python python3 perl ruby node".split())
FETCHERS = frozenset(
    """
    curl wget fetch aria2c http https
""".split()
)
TRUSTED_BIN_DIRS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin", "/usr/local/bin")
# Setting these changes which code a "safe" command actually loads.
DANGEROUS_VARS = frozenset(
    """
    LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT BASH_ENV ENV PATH PROMPT_COMMAND PYTHONPATH
    PYTHONSTARTUP GIT_SSH_COMMAND GIT_EXTERNAL_DIFF SUDO_ASKPASS
""".split()
)
WRITE_REDIRECTS = frozenset({">", ">>", "&>", "&>>", ">|", ">&"})


def default_context(cwd: str | None = None) -> Context:
    """Context for the real machine: your home, CORE's dirs, live disk info."""
    home = str(Path.home())
    safe_dirs = (
        "/tmp",
        f"{home}/.local/share/core",
        f"{home}/.config/core",
        f"{home}/Documents/core",
        f"{home}/Videos/core",
    )
    return Context(home, cwd or os.getcwd(), safe_dirs, disks_in_use())


def classify(command: str, ctx: Context | None = None) -> Assessment:
    """Classify a full command line."""
    ctx = ctx or default_context()
    if not command.strip():
        return safe("nothing to run")
    result = _classify_line(command, ctx, depth=0, root=False)
    return _selinux_net(command, result)


def _classify_line(command: str, ctx: Context, depth: int, root: bool) -> Assessment:
    if depth > MAX_DEPTH:
        return confirm("too many nested shells to check")
    tree = _PARSER.parse(command.encode())
    return worst(_walk(tree.root_node, ctx, depth, root))


def _walk(node: Node, ctx: Context, depth: int, root: bool) -> list[Assessment]:
    """Visit every node; collect an assessment for each risky-looking one."""
    found: list[Assessment] = []
    if node.type == "ERROR" or node.is_missing:
        found.append(confirm("CORE couldn't fully parse this command"))
    elif node.type == "command":
        found.append(_command(node, ctx, depth, root))
    elif node.type == "file_redirect":
        found.append(_redirect(node, ctx))
    elif node.type == "variable_assignment":
        found.append(_assignment(node))
    elif node.type == "function_definition":
        found.append(confirm("defines a shell function"))
    elif node.type == "pipeline":
        found.append(_pipeline(node, ctx))
    for child in node.children:
        found.extend(_walk(child, ctx, depth, root))
    return found


# ---------------------------------------------------------------------------
# Words


def _word(node: Node, ctx: Context) -> Arg:
    """Turn a word node into its value, as far as it can be known statically."""
    text = _text(node)
    match node.type:
        case "word" | "number":
            value = text.replace("\\", "")
            if value == "~" or value.startswith("~/"):
                value = ctx.home + value[1:]
            return Arg(value)
        case "raw_string":
            return Arg(text[1:-1])
        case "string":
            parts = [_word(c, ctx) for c in node.children if c.type != '"']
            return Arg("".join(p.text for p in parts), all(p.static for p in parts))
        case "string_content":
            return Arg(text)
        case "concatenation":
            parts = [_word(c, ctx) for c in node.children]
            return Arg("".join(p.text for p in parts), all(p.static for p in parts))
        case "simple_expansion" | "expansion" if text in ("$HOME", "${HOME}"):
            return Arg(ctx.home)
        case _:
            return Arg(text, static=False)


def _text(node: Node) -> str:
    return (node.text or b"").decode(errors="replace")


# ---------------------------------------------------------------------------
# Commands and wrappers


def _command(node: Node, ctx: Context, depth: int, root: bool) -> Assessment:
    argv = _command_argv(node, ctx)
    if not argv:
        return safe("variable assignment only")
    result = _argv(argv, ctx, depth, root)
    # `sudo bash -c "$(curl …)"` or `sudo sh <(curl …)`: a remote script as root.
    as_root, name = _peel(argv)
    if (as_root or root) and name in INTERPRETERS and _contains_fetch(node):
        return blocked("runs a script straight from the internet as root")
    return result


def _command_argv(node: Node, ctx: Context) -> list[Arg]:
    """[name, arg1, arg2, …] for a `command` node."""
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return []
    argv = [_word(name_node.children[0] if name_node.children else name_node, ctx)]
    return argv + [_word(c, ctx) for c in node.children_by_field_name("argument")]


def _argv(argv: list[Arg], ctx: Context, depth: int, root: bool) -> Assessment:
    """Assess a command given as a list of words, peeling off wrappers."""
    if not argv:
        return safe("nothing to run")
    name_arg, args = argv[0], argv[1:]
    if not name_arg.static:
        return confirm("the command name is only known at runtime")
    name = name_arg.text
    if "/" in name:
        if os.path.dirname(name) not in TRUSTED_BIN_DIRS:
            return confirm(f"runs the program {name} by path")
        name = os.path.basename(name)

    match name:
        case "sudo" | "doas":
            rest = _after_options(args, {"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-T"})
            reason = f"runs as root ({name})"
            if not rest:
                return confirm(reason)
            return worst([confirm(reason), _argv(rest, ctx, depth, root=True)])
        case "pkexec":
            rest = _after_options(args, {"--user"})
            note = "runs as root (pkexec shows a GNOME password popup, which won't work over SSH)"
            return worst([confirm(note), _argv(rest, ctx, depth, root=True)])
        case "su":
            texts = [a.text for a in args]
            if "-c" in texts and texts.index("-c") + 1 < len(args):
                inner = args[texts.index("-c") + 1]
                return worst([confirm("runs as another user (su)"),
                              _string(inner, ctx, depth, root=True)])  # fmt: skip
            return confirm("opens a shell as another user (su)")
        case "env":
            if any(a.text in ("-S", "--split-string") for a in args):
                return confirm("env -S runs a command CORE can't split safely")
            rest = _after_options(args, {"-u", "--unset", "-C", "--chdir"})
            while rest and rest[0].static and "=" in rest[0].text and not is_flag(rest[0]):
                if rest[0].text.split("=", 1)[0] in DANGEROUS_VARS:
                    return confirm("changes how programs load code")
                rest = rest[1:]
            return _argv(rest, ctx, depth, root) if rest else safe("prints the environment")
        case "nice" | "nohup" | "time" | "stdbuf" | "command" | "builtin" | "exec":
            if name == "command" and args[:1] and args[0].text in ("-v", "-V"):
                return safe()
            rest = _after_options(args, {"-n", "-i", "-o", "-e"})
            return _argv(rest, ctx, depth, root) if rest else safe()
        case "timeout":
            rest = _after_options(args, {"-s", "--signal", "-k", "--kill-after"})
            return _argv(rest[1:], ctx, depth, root) if len(rest) > 1 else safe()
        case "xargs":
            value_opts = {"-I", "-n", "-P", "-d", "-L", "-a", "-E", "-s", "--max-args"}
            rest = _after_options(args, value_opts) or [Arg("echo")]
            # xargs appends words from its input, which CORE can't see.
            return _argv([*rest, Arg("<input>", static=False)], ctx, depth, root)
        case "eval":
            if all(a.static for a in args):
                return _classify_line(" ".join(a.text for a in args), ctx, depth + 1, root)
            return confirm("eval runs text that's only known at runtime")
        case "source" | ".":
            return confirm("runs a script file in the shell")
        case _ if name in SHELLS:
            return _shell(name, args, ctx, depth, root)
        case _ if name in INTERPRETERS:
            if [a.text for a in args] in (["--version"], ["-V"]):
                return safe()
            return confirm(f"runs {name} code CORE can't check")

    return assess_command(name, args, ctx)


def _shell(name: str, args: list[Arg], ctx: Context, depth: int, root: bool) -> Assessment:
    texts = [a.text for a in args]
    if texts in (["--version"], ["-c", "exit"]):
        return safe()
    for i, t in enumerate(texts):
        # -c, or a short-flag group containing c (like -ec)
        if is_flag(args[i]) and not t.startswith("--") and "c" in t[1:]:
            if i + 1 >= len(args):
                return confirm(f"{name} -c with nothing to run")
            return _string(args[i + 1], ctx, depth, root)
    if any(not is_flag(a) for a in args):
        return confirm(f"runs a script file with {name}")
    return confirm(f"runs a script that {name} reads from its input")


def _string(arg: Arg, ctx: Context, depth: int, root: bool) -> Assessment:
    """Classify a command passed as a string, like bash -c "…"."""
    if not arg.static:
        return confirm("runs a command string that's only known at runtime")
    return _classify_line(arg.text, ctx, depth + 1, root)


def _after_options(args: list[Arg], value_opts: set[str]) -> list[Arg]:
    """Drop a wrapper's own options; return the wrapped command."""
    i = 0
    while i < len(args) and is_flag(args[i]):
        if args[i].text == "--":
            return args[i + 1 :]
        i += 2 if args[i].text in value_opts else 1
    return args[i:]


def _peel(argv: list[Arg]) -> tuple[bool, str]:
    """(runs as root?, real command name) after stripping sudo/env/etc."""
    root = False
    while argv and argv[0].static:
        name = os.path.basename(argv[0].text)
        if name in ("sudo", "doas", "pkexec", "su"):
            root = True
            argv = _after_options(argv[1:], {"-u", "-g", "-p", "-C", "--user"})
        elif name in ("env", "nice", "nohup", "time", "command", "exec", "stdbuf"):
            argv = _after_options(argv[1:], {"-u", "-n"})
            while argv and argv[0].static and "=" in argv[0].text:
                argv = argv[1:]
        else:
            return root, name
    return root, ""


def _contains_fetch(node: Node) -> bool:
    for child in node.children:
        if child.type == "command":
            name = child.child_by_field_name("name")
            if name is not None and os.path.basename(_text(name)) in FETCHERS:
                return True
        if _contains_fetch(child):
            return True
    return False


# ---------------------------------------------------------------------------
# Pipes, redirects, assignments


def _pipeline(node: Node, ctx: Context) -> Assessment:
    """Catch `curl … | sh` and especially `curl … | sudo bash`."""
    stages = [c for c in node.children if c.type in ("command", "redirected_statement")]
    fetched = False
    for stage in stages:
        cmd = stage if stage.type == "command" else stage.child_by_field_name("body")
        if cmd is None or cmd.type != "command":
            continue
        root, name = _peel(_command_argv(cmd, ctx))
        if name in FETCHERS:
            fetched = True
        elif fetched and name in INTERPRETERS:
            if root:
                return blocked("pipes a script from the internet straight into a root shell")
            return confirm("runs a script downloaded from the internet")
    return safe()


def _redirect(node: Node, ctx: Context) -> Assessment:
    op = next((c for c in node.children if c.type in WRITE_REDIRECTS), None)
    dest = node.child_by_field_name("destination")
    if op is None or dest is None:
        return safe()
    if op.type == ">&" and dest.type == "number":
        return safe()  # 2>&1 just joins output streams
    return write_target(_word(dest, ctx), ctx, verb="writes output to")


def _assignment(node: Node) -> Assessment:
    name = node.child_by_field_name("name")
    if name is not None and _text(name) in DANGEROUS_VARS:
        return confirm(f"sets {_text(name)}, which changes how programs load code")
    return safe()


# ---------------------------------------------------------------------------
# Line-level safety net

_SELINUX_OFF = re.compile(r"\bselinux=0\b|\benforcing=0\b", re.IGNORECASE)


def _selinux_net(command: str, result: Assessment) -> Assessment:
    """Block permanently disabling SELinux, however the command is spelled.

    Only applies to commands that change something, so reading the config
    (`grep SELINUX= /etc/selinux/config`) stays safe.
    """
    if result.tier == Tier.SAFE:
        return result
    edits_config = "selinux/config" in command and re.search(r"disabled", command, re.I)
    if edits_config or _SELINUX_OFF.search(command):
        return blocked("permanently disables SELinux, the main security layer on Fedora")
    return result
