import json
import subprocess
from typing import Any

import pytest

from core.agent import disks

EFI = disks.EFI_SYSTEM
LSBLK = {
    "blockdevices": [
        # Fedora: mounted
        {"name": "/dev/sdb", "mountpoints": [None], "children": [
            {"name": "/dev/sdb1", "mountpoints": ["/boot/efi"], "parttype": EFI},
            {"name": "/dev/sdb3", "mountpoints": ["/", "/home"]},
        ]},
        # Windows: never mounted, but has an EFI partition
        {"name": "/dev/nvme0n1", "mountpoints": [None], "children": [
            {"name": "/dev/nvme0n1p1", "mountpoints": [None], "parttype": EFI},
            {"name": "/dev/nvme0n1p3", "mountpoints": [None]},
        ]},
        # A blank USB stick
        {"name": "/dev/sdc", "mountpoints": [None], "children": [
            {"name": "/dev/sdc1", "mountpoints": [None]},
        ]},
    ]
}  # fmt: skip


def fake_run(stdout: str) -> Any:
    def run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout=stdout)

    return run


def test_mounted_and_os_disks_are_in_use(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", fake_run(json.dumps(LSBLK)))
    found = disks.disks_in_use()
    assert found is not None
    assert {"/dev/sdb", "/dev/sdb3", "/dev/nvme0n1", "/dev/nvme0n1p3"} <= found
    assert "/dev/sdc" not in found and "/dev/sdc1" not in found


def test_lsblk_failure_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", fake_run("not json"))
    assert disks.disks_in_use() is None
