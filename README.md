# handsigns

Name hand signs from a webcam (✌ peace, 👍 thumbs up, ✊ fist, …) using Google's
MediaPipe hand tracker. It runs on the CPU, works on Linux, Windows and macOS,
and nothing leaves your computer: frames are processed in memory and never
saved.

This is the hand recognition from CORE's gesture mode (branch
`claude/gesture-macros` of this repo), pulled out so it can be used on its own.

## Install

In another project:

```sh
uv add "handsigns @ git+https://github.com/lexsclav07/core@hand-signs"
# or: pip install "git+https://github.com/lexsclav07/core@hand-signs"
```

To work on it, clone this branch and run `uv sync`.

The first run downloads MediaPipe's hand model (7.8 MB) into
`~/.cache/handsigns/` (on Windows `%LOCALAPPDATA%\handsigns\`) and checks it
against a fixed SHA-256. On Linux, MediaPipe needs `libEGL.so.1`, which any
desktop already has (`mesa-libEGL` on Fedora).

## Try it

```sh
uv run handsigns preview          # a live drawing of your hand in the terminal
uv run handsigns watch            # prints each sign as it fires
uv run handsigns signs            # the built-in signs
uv run handsigns photo hand.jpg   # name the sign in a photo
```

`--camera` picks the camera: `/dev/video0` on Linux, `0`/`1` on Windows and
macOS, or a video file (handy for testing without a webcam). By default it
uses the first camera that works.

## Use it from Python

```python
from handsigns import HandTracker

with HandTracker() as hands:  # webcam light on
    for frame in hands:  # about 10 frames a second
        if frame.fired == "peace":  # held for 0.6 s, fires once per hold
            print("✌")
```

Each `Frame` has:

| field | |
|---|---|
| `hand` | 21 `[x, y, z]` points in pixels, or `None` with no hand in view |
| `sign` | what the hand shows in this frame (`"peace"`, …) or `None` |
| `fired` | the sign, on the one frame where a held sign counts; else `None` |
| `progress` | 0 to 1 while a sign is held (1 = fired and still held), for a hold bar |
| `t` | seconds (`time.monotonic()` live, the position in a video file) |

Options: `HandTracker(camera="", fps=10, hold=0.6, cooldown=2.0, shapes=None)`.
`hold` is how long a sign must be held before it fires, and `cooldown` is the
gap before the next one can fire. To fire the same sign again, drop your hand
first.

The loop blocks. To use it next to other code, run it in a thread
(`threading.Thread(target=..., daemon=True)`).

### Without a camera

If you already have hand points (from your own MediaPipe code, a recording, or
a test), skip the camera:

```python
import time

from handsigns import classify, SignReader

classify(points)  # "peace", "fist", … or None
reader = SignReader(hold=0.6)  # adds the hold/cooldown logic
frame = reader.read(points, t=time.monotonic())
```

### Your own signs

A sign is the set of fingers that are up (the rest are down):

```python
HandTracker(shapes={"gun": "thumb index", "middle": "middle"})
```

or `handsigns watch --shape gun="thumb index"`. Your shapes are checked before
the built-in ones, so they win when the fingers match.

## From other languages and scripts

`watch` prints one line per sign, so anything can react to it:

```sh
handsigns watch | while read sign; do
    case "$sign" in
        peace) ptyxis --new-window ;;
        thumbs_up) playerctl play-pause ;;
    esac
done
```

`handsigns watch --json` prints every frame as a JSON line
(`{"t", "hand", "sign", "fired", "progress"}`) for programs that want the points.

Careful: a script like that runs commands for anyone in front of the camera,
with no confirmation. CORE's gesture mode puts every macro through its safety
checks first; this package doesn't, so keep the commands harmless.

## Signs

| | sign | fingers up |
|---|---|---|
| ✋ | `open_palm` | all four (thumb either way) |
| ✌ | `peace` | index, middle |
| | `three` | index, middle, ring |
| ☝ | `point` | index |
| 🤘 | `rock` | index, pinky |
| 🤙 | `call_me` | thumb, pinky |
| ✊ | `fist` | none |
| 👍 | `thumbs_up` | thumb, pointing up |
| 👎 | `thumbs_down` | thumb, pointing down |

## How it works

MediaPipe finds 21 points on the hand. `signs.py` counts a finger as up when
its tip is well past its middle joint, measured from the wrist, so it works
however the hand is turned. The thumb is measured from the pinky knuckle
instead, because a tucked thumb lies across the palm rather than curling up.
The thresholds come from MediaPipe's own sample photos; their hand points are
in `tests/data/hands.json`, and the tests check every sign against them.

## Develop

```sh
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy handsigns
```
