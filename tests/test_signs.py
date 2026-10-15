"""Hand signs from hand points, and the debouncer that decides when one fires.

tests/data/hands.json holds the 21 points MediaPipe found in its own sample
photos (victory.jpg, thumbs_up.jpg, …), in pixels. Only the points are kept.
"""

import json
import math
from pathlib import Path

import pytest

from core.tools.signs import Debouncer, classify, label, parse_shapes

REAL = json.loads((Path(__file__).parent / "data" / "hands.json").read_text())

Hand = list[list[float]]


@pytest.mark.parametrize("photo", REAL, ids=[f"{p['photo']}-{p['sign']}" for p in REAL])
def test_real_photos(photo: dict[str, object]) -> None:
    hand = photo["hand"]
    assert isinstance(hand, list)
    assert classify(hand) == photo["sign"]


def rotate(hand: Hand, degrees: float) -> Hand:
    a = math.radians(degrees)
    return [[x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a), z]
            for x, y, z in hand]  # fmt: skip


def real(sign: str) -> Hand:
    return next(p["hand"] for p in REAL if p["sign"] == sign)


@pytest.mark.parametrize("degrees", [30, 90, -90, 180])
def test_finger_signs_work_whichever_way_the_hand_is_turned(degrees: float) -> None:
    assert classify(rotate(real("peace"), degrees)) == "peace"
    assert classify(rotate(real("point"), degrees)) == "point"


def test_thumbs_up_upside_down_is_thumbs_down() -> None:
    flipped = [[x, -y, z] for x, y, z in real("thumbs_up")]
    assert classify(flipped) == "thumbs_down"


def test_thumb_pointing_sideways_is_not_thumbs_up() -> None:
    assert classify(rotate(real("thumbs_up"), 90)) is None


def synthetic(up: str, thumb: str = "tucked") -> Hand:
    """An upright hand, palm to the camera, y down. `up`: which fingers are up."""
    hand: Hand = [[0, 100, 0]] * 21
    hand[1], hand[2] = [-30, 85, 0], [-45, 70, 0]  # thumb base
    hand[3], hand[4] = {"out": ([-80, 45, 0], [-110, 20, 0]),
                        "tucked": ([-20, 45, -10], [5, 35, -15])}[thumb]  # fmt: skip
    for finger, x in zip(["index", "middle", "ring", "pinky"], [-30, -10, 10, 30], strict=True):
        mcp = 5 + 4 * ["index", "middle", "ring", "pinky"].index(finger)
        if finger in up.split():
            joints = [[x, 0, 0], [x, -25, 0], [x, -50, 0], [x, -75, 0]]
        else:  # curled into the palm
            joints = [[x, 0, 0], [x, -20, -15], [x, -5, -30], [x, 10, -15]]
        hand[mcp : mcp + 4] = joints
    return hand


@pytest.mark.parametrize(
    ("up", "thumb", "sign"),
    [
        ("index middle ring pinky", "out", "open_palm"),
        ("index middle ring pinky", "tucked", "open_palm"),
        ("index middle", "tucked", "peace"),
        ("index middle", "out", "peace"),
        ("index middle ring", "tucked", "three"),
        ("index", "tucked", "point"),
        ("index", "out", "point"),
        ("index pinky", "tucked", "rock"),
        ("pinky", "out", "call_me"),
        ("", "tucked", "fist"),
        ("", "out", None),  # thumb out sideways: not thumbs up or down
        ("middle", "tucked", None),
        ("ring pinky", "tucked", None),
    ],
)
def test_synthetic_signs(up: str, thumb: str, sign: str | None) -> None:
    assert classify(synthetic(up, thumb)) == sign


def test_not_a_whole_hand() -> None:
    assert classify([[0, 0, 0]] * 5) is None


def test_own_shapes_are_checked_first() -> None:
    shapes, problems = parse_shapes({"gun": "thumb index", "middle": "middle"})
    assert problems == []
    assert classify(synthetic("index", "out"), shapes) == "gun"  # not the built-in point
    assert classify(synthetic("index", "tucked"), shapes) == "point"
    assert classify(synthetic("middle", "tucked"), shapes) == "middle"


def test_own_shape_can_replace_a_built_in_sign() -> None:
    shapes, _ = parse_shapes({"peace": "index middle ring"})
    assert classify(synthetic("index middle ring"), shapes) == "peace"
    assert classify(synthetic("index middle"), shapes) is None


@pytest.mark.parametrize("value", ["", "index elbow", 3, "Index"])
def test_bad_shapes_are_reported(value: object) -> None:
    shapes, problems = parse_shapes({"oops": value})
    assert shapes == {} and len(problems) == 1 and "oops" in problems[0]


def test_labels() -> None:
    assert label("peace") == "✌ peace"
    assert label("gun") == "gun"


def run(frames: list[tuple[float, str | None]], **kw: float) -> list[tuple[float, str]]:
    d = Debouncer(**kw)
    return [(t, s) for t, sign in frames if (s := d.update(sign, t)) is not None]


def frames(sign: str | None, start: float, end: float) -> list[tuple[float, str | None]]:
    """One frame every 0.1 s (10 fps), from start to end inclusive."""
    return [(round(start + i / 10, 1), sign) for i in range(round((end - start) * 10) + 1)]


def test_fires_once_held_long_enough() -> None:
    assert run(frames("peace", 0, 1)) == [(0.6, "peace")]


def test_passing_through_a_sign_does_nothing() -> None:
    assert run(frames("peace", 0, 0.3) + frames(None, 0.4, 2)) == []


def test_holding_a_sign_does_not_repeat() -> None:
    assert run(frames("peace", 0, 10)) == [(0.6, "peace")]


def test_a_one_frame_glitch_is_not_letting_go() -> None:
    seq = frames("peace", 0, 1) + [(1.1, None), (1.2, "three")] + frames("peace", 1.3, 5)
    assert run(seq) == [(0.6, "peace")]


def test_let_go_then_sign_again_fires_again() -> None:
    seq = frames("peace", 0, 1) + frames(None, 1.1, 2) + frames("peace", 2.1, 3.5)
    assert run(seq) == [(0.6, "peace"), (2.7, "peace")]


def test_cooldown_between_different_signs() -> None:
    seq = frames("peace", 0, 0.7) + frames("thumbs_up", 0.8, 3)
    assert run(seq) == [(0.6, "peace"), (2.6, "thumbs_up")]


def test_no_hand_never_fires() -> None:
    assert run(frames(None, 0, 5)) == []
