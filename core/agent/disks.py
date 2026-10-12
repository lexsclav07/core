"""Which block devices are in use, so risk.py can block wiping them.

A disk counts as "in use" if it, or anything on it (partition, LUKS volume,
LVM volume), is mounted or used as swap. Writing raw bytes to such a disk
with dd/mkfs destroys a live filesystem, so those commands are blocked.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any


def disks_in_use() -> frozenset[str] | None:
    """Device paths that must never be overwritten, or None if we can't tell.

    None makes risk.py fail closed: it then blocks raw writes to every disk.
    """
    try:
        out = subprocess.run(
            ["lsblk", "--json", "--paths", "--output", "NAME,MOUNTPOINTS"],
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        ).stdout
        tree = json.loads(out)["blockdevices"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        return None

    in_use: set[str] = set()
    for disk in tree:
        paths = list(_subtree_paths(disk))
        if any(_mounted(node) for node in _subtree(disk)):
            in_use.update(paths)
    # /dev/disk/by-uuid/... and /dev/mapper/... are symlinks; add their targets.
    return frozenset(in_use | {os.path.realpath(p) for p in in_use})


def _subtree(node: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = [node]
    for child in node.get("children", []):
        nodes.extend(_subtree(child))
    return nodes


def _subtree_paths(node: dict[str, Any]) -> list[str]:
    return [n["name"] for n in _subtree(node)]


def _mounted(node: dict[str, Any]) -> bool:
    return any(m for m in node.get("mountpoints") or [])
