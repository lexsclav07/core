"""preflight.py, with the real gsettings/flatpak lookups faked."""

import pytest

from core.tools import preflight

KNOWN = {
    ("gsettings", "range", "org.gnome.desktop.session", "idle-delay"),
    ("flatpak", "remote-info", "--user", "flathub", "org.prismlauncher.PrismLauncher"),
}


@pytest.fixture(autouse=True)
def fake_lookups(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight, "_ok", lambda cmd: tuple(cmd) in KNOWN)


@pytest.mark.parametrize(
    ("command", "fails"),
    [
        ("gsettings set org.gnome.desktop.session idle-delay 900", False),
        ("gsettings set org.gnome.desktop.screensaver idle-delay 900", True),
        ("gsettings get org.gnome.desktop.screensaver idle-delay", False),  # only set is checked
        ("ls && gsettings set org.gnome.desktop.screensaver idle-delay 900", True),
        ("flatpak install --user -y flathub org.prismlauncher.PrismLauncher", False),
        ("flatpak install --user -y flathub io.prismlauncher.Prism", True),
        ("sudo dnf install -y htop", False),
        ("echo 'unbalanced", False),
    ],
)
def test_problems(command: str, fails: bool) -> None:
    assert (preflight.problems(command) is not None) is fails


def test_message_points_to_the_fix() -> None:
    msg = preflight.problems("flatpak install --user flathub io.prismlauncher.Prism")
    assert msg is not None and "flatpak search Prism" in msg
