"""Hand signs: turn the 21 points of a tracked hand into a name like "peace".

MediaPipe's hand tracker (tracker.py) gives 21 points per hand, numbered
like this (palm facing the camera):

         8  12  16  20      fingertips
         7  11  15  19
     4   6  10  14  18      middle joints (PIP)
     3   5   9  13  17      knuckles (MCP)
      2
       1      0             0 = wrist, 1-4 = thumb

A finger counts as up when its tip is well past its middle joint, measured
from the wrist. Distances (not "tip above joint") mean it works however the
hand is turned. The thumb is measured from the pinky knuckle instead,
because a tucked thumb lies across the palm rather than curling up.

The thresholds come from MediaPipe's sample photos (tests/data/hands.json):
fingers that are up score 1.2 or more, curled ones 0.85 or less.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import chain

Point = Sequence[float]  # x right, y down, z depth; in pixels (z on the x scale)
FINGERS = ("thumb", "index", "middle", "ring", "pinky")
TIPS = (8, 12, 16, 20)  # index..pinky; each finger's middle joint is tip - 2

FINGER_UP = 1.05  # wrist→tip ÷ wrist→middle joint
THUMB_OUT = 1.2  # thumb tip→pinky knuckle ÷ thumb base→pinky knuckle

# One letter per finger, thumb first: U = up, D = down, ? = either.
# The thumb is "either" in most signs because people hold it both ways.
SIGNS = {
    "open_palm": "?UUUU",
    "peace": "?UUDD",
    "three": "?UUUD",
    "point": "?UDDD",
    "rock": "?UDDU",
    "call_me": "UDDDU",
    "fist": "DDDDD",
}
# Thumb up with a fist is thumbs_up or thumbs_down, depending on where it points.
THUMB_SIGNS = ("thumbs_up", "thumbs_down")
EMOJI = {
    "open_palm": "✋",
    "peace": "✌",
    "point": "☝",
    "rock": "🤘",
    "call_me": "🤙",
    "fist": "✊",
    "thumbs_up": "👍",
    "thumbs_down": "👎",
}


def label(sign: str) -> str:
    """'peace' → '✌ peace', for the status bar and chat."""
    emoji = EMOJI.get(sign)
    return f"{emoji} {sign}" if emoji else sign


def fingers_up(hand: Sequence[Point]) -> tuple[bool, ...]:
    """(thumb, index, middle, ring, pinky): True for each finger that's up."""
    wrist = hand[0]
    thumb = math.dist(hand[4], hand[17]) > THUMB_OUT * math.dist(hand[2], hand[17])
    others = (math.dist(wrist, hand[t]) > FINGER_UP * math.dist(wrist, hand[t - 2]) for t in TIPS)
    return (thumb, *others)


def classify(hand: Sequence[Point], shapes: Mapping[str, str] | None = None) -> str | None:
    """Name the sign the hand is making, or None.

    `shapes` are your own signs (see parse_shapes). They're checked first, so
    they win over a built-in sign with the same fingers.
    """
    if len(hand) != 21:
        return None
    up = fingers_up(hand)
    custom = shapes or {}
    builtin = ((n, p) for n, p in SIGNS.items() if n not in custom)
    for name, pattern in chain(custom.items(), builtin):
        pairs = zip(pattern, up, strict=True)
        if all(want == "?" or (want == "U") == is_up for want, is_up in pairs):
            return name
    if up == (True, False, False, False, False):
        ys = [p[1] for p in hand]
        if hand[4][1] == min(ys):  # thumb tip is the highest point of the hand
            return "thumbs_up"
        if hand[4][1] == max(ys):
            return "thumbs_down"
    return None


def parse_shapes(raw: Mapping[str, object]) -> tuple[dict[str, str], list[str]]:
    """Make your own signs: {name: "the fingers that are up"}.

    e.g. {"gun": "thumb index"} means thumb and index up, the rest down.
    Returns the patterns plus a message for each entry that doesn't make sense.
    """
    shapes: dict[str, str] = {}
    problems: list[str] = []
    for name, value in raw.items():
        words = value.split() if isinstance(value, str) else []
        if not words or any(w not in FINGERS for w in words):
            problems.append(
                f"shape {name!r}: list the fingers that are up, "
                f'from {", ".join(FINGERS)} (e.g. "thumb index")'
            )
            continue
        shapes[name] = "".join("U" if f in words else "D" for f in FINGERS)
    return shapes, problems


def all_signs(shapes: Iterable[str] = ()) -> list[str]:
    return [*SIGNS, *THUMB_SIGNS, *(s for s in shapes if s not in SIGNS)]


@dataclass
class Debouncer:
    """Decide when a sign counts, so a hand passing through one doesn't fire it.

    A sign fires once it has been seen in every frame for `hold` seconds. It
    can't fire again until it's been gone for `release` seconds (so holding
    it doesn't repeat, and a one-frame tracking glitch doesn't count as
    letting go), and nothing fires within `cooldown` seconds of the last one.
    """

    hold: float = 0.6
    cooldown: float = 2.0
    release: float = 0.5
    _candidate: str | None = None
    _since: float = 0.0
    _fired: str | None = None  # the last sign that fired, until it's let go
    _fired_seen: float = 0.0
    _last_fire: float = field(default=-math.inf)

    def update(self, sign: str | None, now: float) -> str | None:
        """Feed one frame's sign (None = no sign); returns a sign when it fires."""
        if self._fired is not None:
            if sign == self._fired:
                self._fired_seen = now
                self._candidate = None
                return None
            if now - self._fired_seen >= self.release:
                self._fired = None
        if sign != self._candidate:
            self._candidate, self._since = sign, now
            return None
        if sign is None or now - self._since < self.hold or now - self._last_fire < self.cooldown:
            return None
        self._candidate = None
        self._fired, self._fired_seen, self._last_fire = sign, now, now
        return sign

    def progress(self, sign: str | None, now: float) -> float:
        """How close `sign` (this frame's, after update) is to firing: 0 to 1.

        1 means it has fired and is still being held. Handy for a hold bar.
        """
        if sign is None:
            return 0.0
        if sign == self._fired:
            return 1.0
        if sign != self._candidate or self.hold <= 0:
            return 0.0
        return min((now - self._since) / self.hold, 1.0)
