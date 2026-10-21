"""The `handsigns` command.

    handsigns watch              print each sign as it fires: peace, thumbs_up, …
    handsigns watch --json       one JSON line per frame, for other programs
    handsigns preview            a live drawing of your hand in the terminal
    handsigns photo hand.jpg     name the sign in still photos
    handsigns signs              list the built-in signs

`watch` is made for pipes, so any language or shell script can react to signs:

    handsigns watch | while read sign; do
        [ "$sign" = peace ] && notify-send "✌"
    done
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from handsigns.draw import caption, draw, fingers
from handsigns.model import ModelError
from handsigns.signs import EMOJI, SIGNS, THUMB_SIGNS, label
from handsigns.tracker import CameraError, HandTracker, read_photo

HIDE_CURSOR, SHOW_CURSOR, HOME_AND_CLEAR = "\x1b[?25l", "\x1b[?25h", "\x1b[H\x1b[J"


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return int(args.run(args))
    except (CameraError, ModelError, ValueError) as e:  # ValueError: a bad --shape
        print(f"handsigns: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    except BrokenPipeError:
        return 0  # whatever we were piped into has stopped reading


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="handsigns", description="Hand signs from a webcam.")
    sub = parser.add_subparsers(required=True, metavar="command")

    def tracking(p: argparse.ArgumentParser) -> None:
        p.add_argument("--camera", default="",
                       help='"/dev/video0", "0", or a video file (default: the first)')  # fmt: skip
        p.add_argument("--fps", type=float, default=10, help="frames a second (default 10)")
        p.add_argument("--hold", type=float, default=0.6, help="seconds to hold a sign")
        p.add_argument("--cooldown", type=float, default=2.0, help="seconds between signs")
        p.add_argument("--shape", action="append", default=[], metavar='NAME="FINGERS"',
                       help='your own sign, e.g. --shape gun="thumb index"')  # fmt: skip

    watch = sub.add_parser("watch", help="print signs as they fire")
    tracking(watch)
    watch.add_argument("--json", action="store_true", help="every frame as a JSON line")
    watch.set_defaults(run=_watch)

    preview = sub.add_parser("preview", help="a live drawing of your hand")
    tracking(preview)
    preview.set_defaults(run=_preview)

    photo = sub.add_parser("photo", help="name the sign in still photos")
    photo.add_argument("images", nargs="+", type=Path)
    photo.set_defaults(run=_photo)

    signs = sub.add_parser("signs", help="list the built-in signs")
    signs.set_defaults(run=_signs)
    return parser


def _tracker(args: argparse.Namespace) -> HandTracker:
    shapes = {}
    for given in args.shape:
        name, _, fingers_up = given.partition("=")
        shapes[name] = fingers_up.strip("\"'")
    return HandTracker(args.camera, fps=args.fps, hold=args.hold, cooldown=args.cooldown,
                       shapes=shapes)  # fmt: skip


def _watch(args: argparse.Namespace) -> int:
    with _tracker(args) as hands:
        print(f"handsigns: watching {hands.device}", file=sys.stderr)
        for frame in hands:
            if args.json:
                record = {"t": round(frame.t, 3), "hand": frame.hand, "sign": frame.sign,
                          "fired": frame.fired, "progress": round(frame.progress, 2)}  # fmt: skip
                print(json.dumps(record), flush=True)
            elif frame.fired:
                print(frame.fired, flush=True)
    return 0


def _preview(args: argparse.Namespace) -> int:
    colour = sys.stdout.isatty()
    with _tracker(args) as hands:
        sys.stdout.write(HIDE_CURSOR)
        try:
            for frame in hands:
                lines = [
                    f"handsigns · {hands.device} · Ctrl+C to stop", "",
                    draw(frame.hand, colour=colour), "",
                    caption(frame.sign, frame.progress, colour=colour),
                    fingers(frame.hand, colour=colour) if frame.hand else "show me your hand",
                    f"fired: {label(frame.fired)}" if frame.fired else "",
                ]  # fmt: skip
                sys.stdout.write(HOME_AND_CLEAR + "\n".join(lines) + "\n")
                sys.stdout.flush()
        finally:
            sys.stdout.write(SHOW_CURSOR)
    return 0


def _photo(args: argparse.Namespace) -> int:
    for image in args.images:
        if not image.exists():
            print(f"handsigns: no such file: {image}", file=sys.stderr)
            return 1
        found = read_photo(image)
        names = ", ".join(label(sign) if sign else "(no sign)" for sign, _ in found)
        print(f"{image}: {names or 'no hand'}")
    return 0


def _signs(args: argparse.Namespace) -> int:
    for name, pattern in SIGNS.items():
        up = [f for f, want in zip(("thumb", "index", "middle", "ring", "pinky"), pattern,
                                   strict=True) if want == "U"]  # fmt: skip
        either = " (thumb either way)" if pattern[0] == "?" else ""
        print(f"{EMOJI.get(name, ' '):2} {name:12} {' '.join(up) or 'none up'}{either}")
    for name in THUMB_SIGNS:
        print(f"{EMOJI[name]:2} {name:12} thumb {'up' if name == 'thumbs_up' else 'down'}, fist")
    return 0
