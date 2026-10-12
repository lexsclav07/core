"""Take a screenshot through xdg-desktop-portal.

GNOME on Wayland doesn't let apps read the screen directly. Instead we ask the
portal (a system service) over D-Bus, and GNOME takes the picture. The first
time, GNOME shows a dialog asking whether CORE may take screenshots; that
choice is remembered.

Portal calls are asynchronous in a D-Bus way: Screenshot() returns at once
with a "request" object path, and the answer arrives later as a Response
signal on that path. So we subscribe to the signal *before* calling.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from pathlib import Path
from urllib.parse import unquote, urlparse

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus

PORTAL = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
RESPONSE_TIMEOUT_S = 120  # long enough for Lex to answer GNOME's permission dialog


class PortalError(Exception):
    """The screenshot couldn't be taken (denied, cancelled, or no portal)."""


async def take_screenshot() -> Path:
    """Capture all monitors; return the path of the PNG GNOME saved."""
    _ensure_session_bus()
    try:
        bus = await MessageBus(bus_type=BusType.SESSION).connect()
    except Exception as e:  # noqa: BLE001 - any connection failure means no portal
        raise PortalError(f"can't reach the desktop session bus: {e}") from e
    try:
        token = "core_" + secrets.token_hex(6)
        sender = (bus.unique_name or "").lstrip(":").replace(".", "_")
        request_path = f"{PORTAL_PATH}/request/{sender}/{token}"

        answer: asyncio.Future[list[object]] = asyncio.get_running_loop().create_future()

        def on_message(msg: Message) -> None:
            if (
                msg.message_type == MessageType.SIGNAL
                and msg.path == request_path
                and msg.member == "Response"
                and not answer.done()
            ):
                answer.set_result(msg.body)

        bus.add_message_handler(on_message)
        match = (
            "type='signal',interface='org.freedesktop.portal.Request',"
            f"member='Response',path='{request_path}'"
        )
        await bus.call(
            Message(
                destination="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus",
                member="AddMatch",
                signature="s",
                body=[match],
            )
        )
        reply = await bus.call(
            Message(
                destination=PORTAL,
                path=PORTAL_PATH,
                interface="org.freedesktop.portal.Screenshot",
                member="Screenshot",
                signature="sa{sv}",
                # interactive=False: no area picker, just grab every monitor.
                body=[
                    "",
                    {"handle_token": Variant("s", token), "interactive": Variant("b", False)},
                ],
            )
        )
        if reply is None or reply.message_type == MessageType.ERROR:
            error = reply.body if reply else "no reply"
            raise PortalError(f"the screenshot portal refused: {error}")

        try:
            code, results = await asyncio.wait_for(answer, RESPONSE_TIMEOUT_S)
        except TimeoutError as e:
            raise PortalError("no answer from GNOME (permission dialog not answered?)") from e
        if code != 0:
            why = "you said no in GNOME's dialog" if code == 1 else "GNOME couldn't take it"
            raise PortalError(f"screenshot not taken: {why}")
        assert isinstance(results, dict)
        uri = results["uri"].value
        return Path(unquote(urlparse(uri).path))
    finally:
        bus.disconnect()


def _ensure_session_bus() -> None:
    """Over SSH the session bus address may be unset; it lives at a fixed path."""
    if "DBUS_SESSION_BUS_ADDRESS" not in os.environ:
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{os.getuid()}/bus"
