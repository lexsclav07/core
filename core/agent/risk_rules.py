"""Per-command rules for the risk classifier.

risk.py parses a command line and calls `assess_command(name, args, ctx)` once
for every simple command it finds. This file decides, for one command:

    SAFE     read-only or user-scope: runs without asking
    CONFIRM  changes something: Lex sees an explanation and says y/n
    BLOCKED  destroys the system or exposes it: refused even if Lex asks

Anything not listed here is CONFIRM. The safe list is an allowlist on purpose:
a missing rule costs one extra y/n, while a wrong "safe" could cost the PC.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import IntEnum


class Tier(IntEnum):
    # IntEnum so tiers compare: the worst part of a command line wins.
    SAFE = 0
    CONFIRM = 1
    BLOCKED = 2


@dataclass(frozen=True)
class Assessment:
    tier: Tier
    reason: str
    # True if the output can contain text from outside (files, web, logs).
    # The agent loop then treats later tool calls in that turn as untrusted.
    reads_untrusted: bool = False


@dataclass(frozen=True)
class Arg:
    """One shell word. `static` is False if it depends on $VARS or $(...)."""

    text: str
    static: bool = True


@dataclass(frozen=True)
class Context:
    home: str
    cwd: str
    # Places CORE may write and delete in without asking.
    safe_dirs: tuple[str, ...]
    # Disks backing mounted filesystems (see disks.py). None means unknown.
    disks_in_use: frozenset[str] | None = field(default=frozenset())


def safe(reason: str = "read-only", *, untrusted: bool = False) -> Assessment:
    return Assessment(Tier.SAFE, reason, untrusted)


def confirm(reason: str, *, untrusted: bool = False) -> Assessment:
    return Assessment(Tier.CONFIRM, reason, untrusted)


def blocked(reason: str) -> Assessment:
    return Assessment(Tier.BLOCKED, reason)


def worst(items: Iterable[Assessment]) -> Assessment:
    """Combine assessments: highest tier wins, untrusted if any part is."""
    items = list(items)
    if not items:
        return safe("nothing to run")
    tier = max(a.tier for a in items)
    # List every distinct reason at the top tier, so `ls; sudo rm -r x` says
    # both "runs as root" and "deletes folders".
    reasons = dict.fromkeys(a.reason for a in items if a.tier == tier)
    return Assessment(tier, "; ".join(reasons), any(a.reads_untrusted for a in items))


# ---------------------------------------------------------------------------
# Argument helpers


def is_flag(a: Arg) -> bool:
    return a.static and a.text.startswith("-") and a.text != "-"


def has_short(args: list[Arg], letter: str) -> bool:
    """True if a short-flag group contains `letter`, e.g. -rf has 'r'."""
    return any(is_flag(a) and not a.text.startswith("--") and letter in a.text[1:] for a in args)


def has_long(args: list[Arg], *names: str) -> bool:
    """True for --name or --name=value."""
    return any(a.static and a.text.split("=", 1)[0] in names for a in args)


def positionals(args: list[Arg], value_opts: Iterable[str] = ()) -> list[Arg]:
    """Non-flag args. Options in value_opts swallow the next arg (e.g. -u root)."""
    takes_value = set(value_opts)
    out: list[Arg] = []
    it = iter(args)
    for a in it:
        if a.static and a.text == "--":
            out.extend(it)
            break
        if is_flag(a):
            if a.text in takes_value:
                next(it, None)
            continue
        out.append(a)
    return out


def words(args: list[Arg]) -> list[str]:
    return [a.text for a in positionals(args)]


def resolve(a: Arg, ctx: Context) -> str | None:
    """Absolute, normalised path for a static arg (symlinks resolved)."""
    if not a.static:
        return None
    return os.path.realpath(os.path.join(ctx.cwd, a.text))


def under(path: str, roots: Iterable[str]) -> bool:
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)


def in_safe_zone(a: Arg, ctx: Context) -> bool:
    path = resolve(a, ctx)
    return path is not None and under(path, ctx.safe_dirs)


def in_home(a: Arg, ctx: Context) -> bool:
    path = resolve(a, ctx)
    return path is not None and under(path, [ctx.home])


SYSTEM_DIRS = frozenset(
    """
    / /bin /boot /dev /etc /home /lib /lib64 /opt /proc /root /run /sbin /srv /sys /usr /var
""".split()
)
GLOB_ONLY = re.compile(r"^[*.?]+$")


def protected_target(a: Arg, ctx: Context) -> bool:
    """True for /, home, a top-level system dir, or a glob of everything in one."""
    path = resolve(a, ctx)
    if path is None:
        return False
    protected = SYSTEM_DIRS | {ctx.home}
    if path in protected:
        return True
    base = os.path.basename(a.text.rstrip("/"))
    return bool(GLOB_ONLY.match(base)) and os.path.dirname(path) in protected


DEVICE_PREFIXES = ("/dev/sd", "/dev/nvme", "/dev/vd", "/dev/hd", "/dev/mmcblk", "/dev/dm-",
                   "/dev/mapper/", "/dev/disk/", "/dev/md", "/dev/loop")  # fmt: skip
HARMLESS_DEVICES = frozenset({"/dev/null", "/dev/zero", "/dev/stdout", "/dev/stderr", "/dev/tty"})


def disk_target(path: str, ctx: Context) -> Assessment | None:
    """Blocked if `path` is an in-use disk; confirm for other disks."""
    if not path.startswith(DEVICE_PREFIXES):
        return None
    real = os.path.realpath(path)
    if ctx.disks_in_use is None:
        return blocked(f"{path} is a disk and CORE can't check whether it's in use")
    if path in ctx.disks_in_use or real in ctx.disks_in_use:
        return blocked(f"{path} holds a mounted filesystem; writing to it destroys the system")
    return confirm(f"writes raw data to the disk {path}, erasing what's on it")


def write_target(a: Arg, ctx: Context, verb: str = "writes to") -> Assessment:
    """Assess writing to a file: safe in CORE's dirs, blocked on live disks."""
    if not a.static:
        return confirm(f"{verb} a path that's only known at runtime ({a.text})")
    path = resolve(a, ctx) or a.text
    if a.text in HARMLESS_DEVICES or path in HARMLESS_DEVICES:
        return safe()
    disk = disk_target(a.text, ctx)
    if disk:
        return disk
    if under(path, ctx.safe_dirs):
        return safe(f"{verb} {a.text} (CORE's own space)")
    return confirm(f"{verb} {a.text}, which may overwrite it")


# ---------------------------------------------------------------------------
# Rules. Each takes the args after the command name.

Rule = Callable[[list[Arg], Context], Assessment]

# Plain read-only commands with no dangerous flags.
READ_ONLY = frozenset(
    """
    ls dir tac wc cut tr column nl fold fmt paste join comm diff cmp file stat du df free
    uptime uname whoami id groups users who w pwd echo printf true false : which type
    whereis realpath readlink basename dirname printenv lsblk lscpu lspci lsusb lsmod lsof
    findmnt ss ps pgrep pidof sensors nproc arch tty locale getconf seq sleep test [ md5sum
    sha1sum sha256sum sha512sum b2sum cksum strings od hexdump jq rg grep egrep fgrep zcat
    zgrep bzcat xzcat tree head tail cat getenforce sestatus ping host dig nslookup
    tracepath traceroute fc-list glxinfo vulkaninfo inxi fastfetch neofetch lshw dmidecode
    lsattr getfacl cd pushd popd set export unset shopt umask alias unalias wait
""".split()
)

# Commands whose output is outside content (files, logs, web) for the taint rule.
READS_CONTENT = frozenset(
    """
    cat tac head tail grep egrep fgrep rg zcat zgrep bzcat xzcat strings od hexdump xxd jq
    sed awk gawk mawk sort uniq cut tr column nl fold fmt paste join comm diff journalctl
    dmesg curl wget find tree
""".split()
)

INTERACTIVE = frozenset(
    """
    vi vim nvim nano emacs less more man top htop btop watch
""".split()
)


def rule_rm(args: list[Arg], ctx: Context) -> Assessment:
    targets = positionals(args)
    recursive = has_short(args, "r") or has_short(args, "R") or has_long(args, "--recursive")
    if has_long(args, "--no-preserve-root"):
        return blocked("rm --no-preserve-root is how you wipe the whole system")
    for t in targets:
        if recursive and protected_target(t, ctx):
            path = resolve(t, ctx)
            what = "your whole home folder" if path == ctx.home else "the system"
            return blocked(f"rm -r on {t.text} would wipe {what}")
    if recursive:
        return confirm("deletes folders and everything inside them")
    if targets and all(in_safe_zone(t, ctx) for t in targets):
        return safe("deletes files in CORE's own space")
    return confirm("deletes files")


def rule_permissions(args: list[Arg], ctx: Context) -> Assessment:
    recursive = has_short(args, "R") or has_long(args, "--recursive")
    if recursive and any(protected_target(t, ctx) for t in positionals(args)):
        return blocked("recursively changes permissions on / or a system directory")
    return confirm("changes file permissions or ownership")


def rule_mkdir_touch(args: list[Arg], ctx: Context) -> Assessment:
    targets = positionals(args, value_opts={"-m", "-d", "-r", "-t"})
    if targets and all(in_home(t, ctx) or in_safe_zone(t, ctx) for t in targets):
        return safe("creates files or folders in your home")
    return confirm("creates files or folders outside your home")


def rule_copy_move(args: list[Arg], ctx: Context) -> Assessment:
    pos = positionals(args, value_opts={"-t", "--target-directory", "-S", "--suffix"})
    target = next((a for a in args if a.text.startswith("--target-directory=")), None)
    if target is not None:
        dest = Arg(target.text.split("=", 1)[1], target.static)
    elif "-t" in [a.text for a in args]:
        dest = args[[a.text for a in args].index("-t") + 1]
    elif len(pos) >= 2:
        dest = pos[-1]
    else:
        return confirm("copies or moves files")
    return write_target(dest, ctx, verb="copies or moves into")


def rule_mv(args: list[Arg], ctx: Context) -> Assessment:
    sources = positionals(args)[:-1]
    if not all(in_safe_zone(s, ctx) for s in sources):
        return confirm("moves files (the originals disappear from where they were)")
    return rule_copy_move(args, ctx)


def rule_tee(args: list[Arg], ctx: Context) -> Assessment:
    return worst(write_target(t, ctx) for t in positionals(args)) if positionals(args) else safe()


def rule_dd(args: list[Arg], ctx: Context) -> Assessment:
    out = next((a for a in args if a.text.startswith("of=")), None)
    if out is None:
        return safe("dd without of= only writes to the screen")
    return write_target(Arg(out.text[3:], out.static), ctx)


def rule_disk_tool(args: list[Arg], ctx: Context) -> Assessment:
    """mkfs, wipefs, fdisk, parted…: blocked on in-use disks."""
    if has_short(args, "l") and len(positionals(args)) == 0:
        return safe("lists partitions")
    found = [disk_target(a.text, ctx) for a in positionals(args) if a.static]
    hits = [f for f in found if f]
    if hits:
        return worst(hits)
    return confirm("formats or repartitions a disk")


def rule_sed(args: list[Arg], ctx: Context) -> Assessment:
    if has_short(args, "i") or has_long(args, "--in-place"):
        return confirm("edits files in place", untrusted=True)
    if has_short(args, "f") or any(not a.static for a in args):
        return confirm("runs a sed script CORE can't read", untrusted=True)
    script = " ".join(a.text for a in positionals(args)[:1])
    # GNU sed can write files (w) and run shell commands (e).
    if re.search(r"(^|[;{}\n/])\s*[0-9$,]*\s*!?\s*[ewW](\s|$)", script):
        return confirm("this sed script writes files or runs commands", untrusted=True)
    return safe(untrusted=True)


def rule_awk(args: list[Arg], ctx: Context) -> Assessment:
    script = " ".join(a.text for a in positionals(args, value_opts={"-v", "-F"})[:1])
    if has_short(args, "f") or has_short(args, "i") or any(not a.static for a in args):
        return confirm("runs an awk program CORE can't fully read", untrusted=True)
    if re.search(r"system|getline|[|>]", script):
        return confirm("this awk program can run commands or write files", untrusted=True)
    return safe(untrusted=True)


def rule_find(args: list[Arg], ctx: Context) -> Assessment:
    risky = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0",
             "-fprintf", "-fls"}  # fmt: skip
    if any(a.text in risky for a in args):
        return confirm("find with -delete or -exec changes or runs things", untrusted=True)
    return safe(untrusted=True)


def rule_sort(args: list[Arg], ctx: Context) -> Assessment:
    if "-o" in [a.text for a in args]:
        i = [a.text for a in args].index("-o")
        return write_target(args[i + 1], ctx) if i + 1 < len(args) else confirm("sort -o")
    out = next((a for a in args if a.text.startswith("--output=")), None)
    if out:
        return write_target(Arg(out.text.split("=", 1)[1]), ctx)
    return safe(untrusted=True)


def rule_two_file(args: list[Arg], ctx: Context) -> Assessment:
    """uniq/xxd IN OUT: a second filename is written to."""
    pos = positionals(args)
    if len(pos) >= 2:
        return write_target(pos[1], ctx)
    return safe(untrusted=True)


UPLOAD_FLAGS = tuple(
    """
    -d --data --data-raw --data-binary --data-urlencode -F --form -T --upload-file --json
    -K --config -u --user
""".split()
)


def rule_curl(args: list[Arg], ctx: Context) -> Assessment:
    if any(not a.static for a in args):
        return confirm("builds a web request from a variable or command output", untrusted=True)
    if has_long(args, *UPLOAD_FLAGS) or any(a.text.startswith(("-d", "-F", "-T")) for a in args):
        return confirm("sends data to a web server", untrusted=True)
    texts = [a.text for a in args]
    for flag in ("-X", "--request"):
        i = texts.index(flag) if flag in texts else -1
        if 0 <= i < len(texts) - 1 and texts[i + 1].upper() not in ("GET", "HEAD"):
            return confirm("sends a non-GET web request", untrusted=True)
    for flag in ("-o", "--output"):
        if flag in texts and texts.index(flag) + 1 < len(texts):
            return _untrusted(write_target(args[texts.index(flag) + 1], ctx))
    if has_short(args, "O") or has_long(args, "--remote-name"):
        return _untrusted(write_target(Arg(ctx.cwd), ctx, verb="downloads into"))
    return safe("downloads to the screen only", untrusted=True)


def rule_wget(args: list[Arg], ctx: Context) -> Assessment:
    if any(not a.static for a in args):
        return confirm("builds a web request from a variable or command output", untrusted=True)
    if has_long(args, "--post-data", "--post-file", "--body-data", "--body-file", "--method"):
        return confirm("sends data to a web server", untrusted=True)
    texts = [a.text for a in args]
    if "--spider" in texts or "-O-" in texts or "-qO-" in texts or ("-O" in texts and "-" in texts):
        return safe("downloads to the screen only", untrusted=True)
    if "-O" in texts and texts.index("-O") + 1 < len(texts):
        return _untrusted(write_target(args[texts.index("-O") + 1], ctx))
    return _untrusted(write_target(Arg(ctx.cwd), ctx, verb="downloads into"))


def _untrusted(a: Assessment) -> Assessment:
    return Assessment(a.tier, a.reason, True)


def verb_rule(safe_verbs: set[str], reason: str, value_opts: Iterable[str] = ()) -> Rule:
    """Safe if the first positional word (the subcommand) is read-only."""
    opts = set(value_opts)

    def rule(args: list[Arg], ctx: Context) -> Assessment:
        pos = positionals(args, value_opts=opts)
        if not pos or (pos[0].static and pos[0].text in safe_verbs):
            return safe()
        return confirm(reason)

    return rule


def rule_systemctl(args: list[Arg], ctx: Context) -> Assessment:
    read = {"status", "is-active", "is-enabled", "is-failed", "is-system-running", "list-units",
            "list-unit-files", "list-sockets", "list-timers", "list-jobs", "list-dependencies",
            "show", "cat", "help", "get-default", "show-environment"}  # fmt: skip
    pos = positionals(args, value_opts={"-t", "--type", "-p", "--property", "-n", "-o", "-H", "-M"})
    if not pos or pos[0].text in read:
        return safe("shows service status")
    scope = "your user services" if has_long(args, "--user") else "system services"
    return confirm(f"starts, stops, or enables {scope}")


def rule_flatpak(args: list[Arg], ctx: Context) -> Assessment:
    pos = words(args)
    verb = pos[0] if pos else ""
    if verb in {"", "list", "info", "search", "remotes", "remote-ls", "remote-info", "history",
                "ps", "documents", "permissions", "permission-show", "run"}:  # fmt: skip
        return safe()
    if verb in {"install", "update", "upgrade", "uninstall", "remove"}:
        if has_long(args, "--user"):
            return safe("installs or removes an app just for you (--user)")
        return confirm("installs or removes an app for every user on the PC")
    return confirm("changes Flatpak remotes or settings")


def rule_dnf(args: list[Arg], ctx: Context) -> Assessment:
    pos = words(args)
    read = {"list", "info", "search", "repolist", "repoquery", "provides", "whatprovides",
            "check-update", "check-upgrade", "deplist", "repoinfo", "help", "updateinfo",
            "advisory"}  # fmt: skip
    if not pos and has_long(args, "--version"):
        return safe()
    if pos and pos[0] in read:
        return safe()
    if pos and pos[0] == "history" and (len(pos) == 1 or pos[1] in {"list", "info"}):
        return safe()
    return confirm("installs, removes, or upgrades system software, or changes repos")


def rule_rpm(args: list[Arg], ctx: Context) -> Assessment:
    texts = [a.text for a in args if is_flag(a)]
    if texts and (texts[0].startswith("-q") or texts[0] in {"--query", "-V", "--verify"}):
        return safe()
    return confirm("installs, removes, or changes system packages")


def rule_git(args: list[Arg], ctx: Context) -> Assessment:
    pos = positionals(args, value_opts={"-C", "-c", "--git-dir", "--work-tree"})
    if not pos:
        return safe()
    verb, rest = pos[0].text, [a.text for a in pos[1:]]
    if verb in {"log", "show", "diff", "blame", "grep", "cat-file", "shortlog", "reflog"}:
        return safe(untrusted=True)
    if verb in {"status", "rev-parse", "ls-files", "ls-tree", "describe", "show-ref", "help",
                "version"}:  # fmt: skip
        return safe()
    read_when_bare = {"branch", "tag", "remote", "stash"}
    if verb in read_when_bare and (not rest or rest[0] in {"list", "show", "get-url", "-v"}):
        return safe()
    if verb == "config" and has_long(args, "--get", "--list", "--get-all", "--get-regexp", "-l"):
        return safe()
    return confirm(f"git {verb} changes the repository or talks to a remote")


def rule_nmcli(args: list[Arg], ctx: Context) -> Assessment:
    mutating = {"up", "down", "modify", "add", "delete", "connect", "disconnect", "reload",
                "set", "on", "off", "import", "edit", "clone", "reapply"}  # fmt: skip
    pos = words(args)
    if any(w in mutating for w in pos) or ("hostname" in pos and pos[-1] != "hostname"):
        return confirm("changes network settings")
    return safe()


def rule_ip(args: list[Arg], ctx: Context) -> Assessment:
    pos = words(args)
    if len(pos) < 2 or pos[1] in {"show", "list", "ls", "lst", "get", "help"}:
        return safe()
    return confirm("changes network settings")


def rule_flag_blacklist(flags: set[str], reason: str, *, untrusted: bool = False) -> Rule:
    """Safe unless a flag (or --flag=value) from `flags` is used."""

    def rule(args: list[Arg], ctx: Context) -> Assessment:
        if any(a.static and a.text.split("=", 1)[0] in flags for a in args):
            return confirm(reason, untrusted=untrusted)
        return safe(untrusted=untrusted)

    return rule


def rule_firewall(args: list[Arg], ctx: Context) -> Assessment:
    read_prefixes = ("--list", "--get", "--query", "--state", "--info", "--help", "--version",
                     "--zone=", "--permanent")  # fmt: skip
    if all(a.text.startswith(read_prefixes) for a in args):
        return safe()
    return confirm("changes the firewall")


def rule_tailscale(args: list[Arg], ctx: Context) -> Assessment:
    pos = words(args)
    verb = pos[0] if pos else ""
    if verb == "funnel" and pos[1:2] != ["status"]:
        return blocked("tailscale funnel publishes this PC to the public internet")
    if verb in {"", "status", "ip", "ping", "netcheck", "version", "whois", "dns"}:
        return safe()
    if verb == "serve" and pos[1:2] == ["status"]:
        return safe()
    return confirm("changes Tailscale (remote access) settings")


def rule_tunnel(args: list[Arg], ctx: Context) -> Assessment:
    """ngrok / cloudflared / localtunnel: public tunnels into this PC."""
    texts = [a.text for a in args]
    if texts in (["version"], ["help"], ["--version"], ["--help"]):
        return safe("only prints version or help")
    return blocked("opens a public internet tunnel into this PC")


def rule_cloudflared(args: list[Arg], ctx: Context) -> Assessment:
    if "tunnel" in words(args) or has_long(args, "--url"):
        return blocked("opens a public internet tunnel into this PC")
    return confirm("runs cloudflared")


def rule_upnpc(args: list[Arg], ctx: Context) -> Assessment:
    if any(a.text in {"-a", "-r", "-n", "-A", "-N"} for a in args):
        return blocked("forwards a port on the router to the public internet")
    return safe()


def rule_ssh(args: list[Arg], ctx: Context) -> Assessment:
    texts = [a.text for a in args]
    public = ("serveo.net", "localhost.run", "pinggy.io")
    if "-R" in texts:
        spec = texts[texts.index("-R") + 1] if texts.index("-R") + 1 < len(texts) else ""
        if spec.startswith(("0.0.0.0:", "*:")) or any(p in t for t in texts for p in public):
            return blocked("publishes a port on this PC to the internet through SSH")
    return confirm("connects to another machine over SSH")


def rule_hostname(args: list[Arg], ctx: Context) -> Assessment:
    return safe() if not positionals(args) else confirm("changes the computer's name")


def rule_date(args: list[Arg], ctx: Context) -> Assessment:
    if has_short(args, "s") or has_long(args, "--set"):
        return confirm("changes the system clock")
    return safe()


def rule_mount(args: list[Arg], ctx: Context) -> Assessment:
    return safe() if not args or [a.text for a in args] == ["-l"] else confirm("mounts a disk")


def rule_top(args: list[Arg], ctx: Context) -> Assessment:
    if has_short(args, "b"):
        return safe()
    return confirm("top is interactive; use `top -b -n 1` instead")


def rule_tar(args: list[Arg], ctx: Context) -> Assessment:
    first = args[0].text if args else ""
    if has_long(args, "--list") or ("t" in first.lstrip("-") and not first.startswith("--")):
        return safe()
    return confirm("creates or extracts an archive, writing files")


def rule_setenforce(args: list[Arg], ctx: Context) -> Assessment:
    return confirm("switches SELinux mode until the next reboot")


def rule_gnome_ext(args: list[Arg], ctx: Context) -> Assessment:
    pos = words(args)
    if not pos or pos[0] in {"list", "info", "show", "version", "help"}:
        return safe()
    return confirm("enables, disables, or installs GNOME extensions")


RULES: dict[str, Rule] = {
    "rm": rule_rm,
    "rmdir": rule_rm,
    "unlink": rule_rm,
    "chmod": rule_permissions,
    "chown": rule_permissions,
    "chgrp": rule_permissions,
    "setfacl": rule_permissions,
    "mkdir": rule_mkdir_touch,
    "touch": rule_mkdir_touch,
    "cp": rule_copy_move,
    "ln": rule_copy_move,
    "install": rule_copy_move,
    "mv": rule_mv,
    "tee": rule_tee,
    "dd": rule_dd,
    "sed": rule_sed,
    "awk": rule_awk,
    "gawk": rule_awk,
    "mawk": rule_awk,
    "find": rule_find,
    "sort": rule_sort,
    "uniq": rule_two_file,
    "xxd": rule_two_file,
    "curl": rule_curl,
    "wget": rule_wget,
    "systemctl": rule_systemctl,
    "flatpak": rule_flatpak,
    "dnf": rule_dnf,
    "dnf5": rule_dnf,
    "yum": rule_dnf,
    "rpm": rule_rpm,
    "git": rule_git,
    "nmcli": rule_nmcli,
    "ip": rule_ip,
    "firewall-cmd": rule_firewall,
    "tailscale": rule_tailscale,
    "ngrok": rule_tunnel,
    "lt": rule_tunnel,
    "localtunnel": rule_tunnel,
    "bore": rule_tunnel,
    "cloudflared": rule_cloudflared,
    "upnpc": rule_upnpc,
    "ssh": rule_ssh,
    "hostname": rule_hostname,
    "date": rule_date,
    "mount": rule_mount,
    "top": rule_top,
    "tar": rule_tar,
    "setenforce": rule_setenforce,
    "gnome-extensions": rule_gnome_ext,
    "gsettings": verb_rule(
        {
            "get",
            "list-schemas",
            "list-relocatable-schemas",
            "list-keys",
            "list-children",
            "list-recursively",
            "range",
            "describe",
            "writable",
            "monitor",
            "help",
        },
        "changes a GNOME setting",
    ),  # fmt: skip
    "dconf": verb_rule({"read", "list", "dump", "watch", "help"}, "changes GNOME settings"),
    "hostnamectl": verb_rule({"status"}, "changes the computer's name or info"),
    "timedatectl": verb_rule({"status", "show", "list-timezones"}, "changes the time settings"),
    "localectl": verb_rule({"status", "list-locales", "list-keymaps"}, "changes the locale"),
    "loginctl": verb_rule(
        {
            "list-sessions",
            "list-users",
            "list-seats",
            "show-session",
            "show-user",
            "session-status",
            "user-status",
        },
        "changes login sessions",
    ),  # fmt: skip
    "journalctl": rule_flag_blacklist(
        {
            "--vacuum-size",
            "--vacuum-time",
            "--vacuum-files",
            "--rotate",
            "--flush",
            "--relinquish-var",
            "--sync",
            "--setup-keys",
            "--update-catalog",
        },
        "deletes or rotates system logs",
        untrusted=True,
    ),  # fmt: skip
    "dmesg": rule_flag_blacklist(
        {"-C", "--clear", "-c", "--read-clear", "-n", "--console-level", "-D", "-E"},
        "clears or changes the kernel log",
        untrusted=True,
    ),
    "nvidia-smi": rule_flag_blacklist(
        {
            "-pm",
            "--persistence-mode",
            "-pl",
            "--power-limit",
            "-r",
            "--gpu-reset",
            "-c",
            "--compute-mode",
            "-e",
            "--ecc-config",
            "-ac",
            "--applications-clocks",
            "-rac",
            "-lgc",
            "--lock-gpu-clocks",
            "-rgc",
            "-lmc",
            "-rmc",
            "-mig",
        },
        "changes GPU power, clock, or mode settings",
    ),  # fmt: skip
}

for _tool in ("mkfs", "mke2fs", "mkswap", "wipefs", "fdisk", "sfdisk", "gdisk", "sgdisk",
              "cfdisk", "parted", "blkdiscard", "shred", "cryptsetup"):  # fmt: skip
    RULES[_tool] = rule_disk_tool


def assess_command(name: str, args: list[Arg], ctx: Context) -> Assessment:
    """Assess one simple command (wrappers like sudo already peeled off)."""
    base = name
    if name.startswith(("mkfs.", "mkfs-")):
        base = "mkfs"
    rule = RULES.get(base)
    if rule is not None:
        result = rule(args, ctx)
    elif base in INTERACTIVE:
        result = confirm(f"{base} is interactive and can't be driven from here")
    elif base in READ_ONLY:
        result = safe()
    elif [a.text for a in args] in (["--version"], ["--help"]):
        result = safe("only prints version or help")
    else:
        result = confirm(f"`{base}` isn't on CORE's list of known-safe commands")
    if base in READS_CONTENT and not result.reads_untrusted:
        result = Assessment(result.tier, result.reason, True)
    return result
