"""Watch a webcam and report what the hand is doing, frame by frame.

    from handsigns import HandTracker

    with HandTracker() as hands:
        for frame in hands:
            if frame.fired == "peace":
                print("✌")

Every Frame has the 21 hand points (or None), the sign the hand is making in
that frame, `fired` on the one frame where a held sign counts (see
signs.Debouncer), and `progress` from 0 to 1 while a sign is held.

MediaPipe runs on the CPU, about 20 ms a frame at 640×480, and 10 frames a
second is plenty for signs you hold. The camera can be "" (the first one that
works), a device like "/dev/video0" (Linux), a camera number like "0"
(Windows, macOS), or a video file, which is handy for testing without a webcam.
Video files go as fast as the CPU allows and use the video's own clock, so
holds and cooldowns behave as they would live.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

from handsigns.model import ensure_model
from handsigns.signs import Debouncer, classify, parse_shapes

FRAME_WIDTH = 640  # plenty for a hand at desk distance, and cheap on the CPU


class CameraError(Exception):
    pass


@dataclass(frozen=True)
class Frame:
    t: float  # seconds: time.monotonic() for a webcam, the position in a video file
    hand: list[list[float]] | None  # 21 [x, y, z] points in pixels, or None
    sign: str | None  # what the hand shows in this frame
    fired: str | None  # the sign, on the one frame a held sign counts; else None
    progress: float  # how close `sign` is to firing: 0 to 1 (1 = fired, still held)


class SignReader:
    """The part of HandTracker that needs no camera: hand points in, Frames out.

    Use it on points from anywhere (another tracker, a recording, a test).
    `shapes` are your own signs: {"gun": "thumb index"} (the fingers that are up).
    """

    def __init__(
        self, hold: float = 0.6, cooldown: float = 2.0, shapes: Mapping[str, str] | None = None
    ) -> None:
        self.debouncer = Debouncer(hold, cooldown)
        self.shapes, problems = parse_shapes(shapes or {})
        if problems:
            raise ValueError("; ".join(problems))

    def read(self, hand: list[list[float]] | None, t: float) -> Frame:
        sign = classify(hand, self.shapes) if hand else None
        fired = self.debouncer.update(sign, t)
        return Frame(t, hand, sign, fired, self.debouncer.progress(sign, t))


class HandTracker:
    """A webcam (or video file) plus MediaPipe's hand tracker. Iterate it for Frames."""

    def __init__(
        self,
        camera: str = "",
        *,
        fps: float = 10,
        hold: float = 0.6,
        cooldown: float = 2.0,
        shapes: Mapping[str, str] | None = None,
        model: Path | None = None,
    ) -> None:
        self.camera = camera
        self.fps = fps
        self.reader = SignReader(hold, cooldown, shapes)
        self.model = model
        self.device = ""  # the camera actually opened
        self._cap: Any = None
        self._tracker: Any = None
        self._is_file = False

    def open(self) -> None:
        os.environ.setdefault("GLOG_minloglevel", "2")  # quieter MediaPipe logs
        import cv2
        from mediapipe.tasks.python import BaseOptions, vision

        # Camera first: a wrong --camera fails at once, before any download.
        self.device, self._cap, self._is_file = open_camera(cv2, self.camera)
        try:
            model = ensure_model(self.model)
        except BaseException:
            self.close()
            raise
        options = vision.HandLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(model)),
            running_mode=vision.RunningMode.VIDEO,  # tracks between frames: faster, steadier
            num_hands=1,
            min_hand_detection_confidence=0.6,
            min_hand_presence_confidence=0.6,
            min_tracking_confidence=0.6,
        )
        self._tracker = vision.HandLandmarker.create_from_options(options)

    def close(self) -> None:
        if self._tracker is not None:
            self._tracker.close()
            self._tracker = None
        if self._cap is not None:
            self._cap.release()  # the webcam light goes off
            self._cap = None

    def __enter__(self) -> HandTracker:
        self.open()
        return self

    def __exit__(
        self, kind: type[BaseException] | None, e: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()

    def __iter__(self) -> Iterator[Frame]:
        import cv2
        import mediapipe as mp

        if self._cap is None:
            self.open()
        last_ms = -1
        while True:
            started = time.monotonic()
            ok, image = self._cap.read()
            if not ok:
                if self._is_file:
                    return  # end of the video
                raise CameraError("the webcam stopped sending pictures")
            t = self._cap.get(cv2.CAP_PROP_POS_MSEC) / 1000 if self._is_file else started
            ms = max(int(t * 1000), last_ms + 1)  # MediaPipe needs rising timestamps
            last_ms = ms
            rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            found = self._tracker.detect_for_video(
                mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), ms
            )
            hand = pixels(found.hand_landmarks[0], rgb) if found.hand_landmarks else None
            yield self.reader.read(hand, t)
            if not self._is_file:
                time.sleep(max(0.0, 1 / self.fps - (time.monotonic() - started)))


def read_photo(path: Path, model: Path | None = None) -> list[tuple[str | None, list[list[float]]]]:
    """The sign and points of every hand (up to two) in a still photo."""
    os.environ.setdefault("GLOG_minloglevel", "2")
    import mediapipe as mp
    from mediapipe.tasks.python import BaseOptions, vision

    options = vision.HandLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=str(ensure_model(model))),
        running_mode=vision.RunningMode.IMAGE,
        num_hands=2,
    )
    with vision.HandLandmarker.create_from_options(options) as tracker:
        image = mp.Image.create_from_file(str(path))
        found = tracker.detect(image)
    hands = [pixels(points, image.numpy_view()) for points in found.hand_landmarks]
    return [(classify(hand), hand) for hand in hands]


def pixels(points: Any, image: Any) -> list[list[float]]:
    """MediaPipe's 0..1 coordinates → pixels, so a 16:9 camera doesn't squash
    distances. Its z (depth) is already on the same scale as x."""
    h, w = image.shape[:2]
    return [[round(p.x * w, 1), round(p.y * h, 1), round(p.z * w, 1)] for p in points]


def open_camera(cv2: Any, camera: str) -> tuple[str, Any, bool]:
    """Open the camera asked for, or the first that gives a picture.

    Returns (what was opened, the capture, whether it's a video file).
    """
    if camera:
        choices = [camera]
    elif sys.platform.startswith("linux"):
        # A webcam usually has extra /dev/video nodes that are metadata, not video.
        choices = [str(p) for p in sorted(Path("/dev").glob("video[0-9]*"))]
    else:
        choices = ["0", "1", "2"]
    for choice in choices:
        is_file = not choice.isdigit() and not choice.startswith("/dev/")
        if is_file and not Path(choice).exists():
            raise CameraError(f"no such video file: {choice}")
        if choice.isdigit():
            cap = cv2.VideoCapture(int(choice))
        elif is_file:
            cap = cv2.VideoCapture(choice)
        else:
            cap = cv2.VideoCapture(choice, cv2.CAP_V4L2)
        if not is_file:
            # MJPEG: raw video can be very slow over USB 2 (~5 fps at 1080p).
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter.fourcc(*"MJPG"))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_WIDTH * 3 // 4)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # always the newest frame, not a queued one
        if cap.isOpened() and cap.read()[0]:
            if is_file:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)  # back to the start
            return choice, cap, is_file
        cap.release()
    if not choices:
        raise CameraError("no webcam is connected (no /dev/video* device)")
    raise CameraError(f"no picture from {', '.join(choices)} (is another app using the camera?)")
