"""Hand tracker for gesture mode: webcam frames in, hand points out.

gestures.py starts this as its own process (`python -m core.tools.hand_worker`)
rather than a thread, for two reasons: MediaPipe's C++ code prints log lines
straight to the terminal, which would scribble over the TUI, and if it
crashes it shouldn't take CORE down with it. Frames never leave this process;
only the 21 hand points do.

It prints one JSON line per message on stdout:
    {"ready": "/dev/video0"}                     once, when tracking starts
    {"t": 812.4, "hand": [[x, y, z], ...]}       every frame ("hand": null if none)
    {"error": "why it stopped"}                  then it exits
"t" is time.monotonic(), which is the same clock in every process.

Try it by hand (Ctrl+C to stop):
    uv run python -m core.tools.hand_worker --model ~/.local/share/core/models/hand_landmarker.task
--camera also takes a video file, handy for testing signs without a webcam.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import IO, Any

FRAME_WIDTH = 640  # plenty for a hand at desk distance, and cheap on the CPU


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gesture mode hand tracker for CORE.")
    parser.add_argument("--model", required=True, help="hand_landmarker.task file")
    parser.add_argument("--camera", default="", help="e.g. /dev/video0 (default: first that works)")
    parser.add_argument("--fps", type=float, default=10)
    args = parser.parse_args(argv)

    # Keep stdout for our JSON only: anything a library prints goes to stderr.
    out = os.fdopen(os.dup(1), "w", buffering=1)
    os.dup2(2, 1)
    os.environ.setdefault("GLOG_minloglevel", "2")  # quieter MediaPipe logs
    try:
        track(args.model, args.camera, args.fps, out)
    except (BrokenPipeError, KeyboardInterrupt):
        return 0  # CORE closed the pipe or Lex pressed Ctrl+C: just stop
    except Exception as e:  # noqa: BLE001 - report anything, CORE shows it to Lex
        _emit(out, {"error": str(e) or type(e).__name__})
        return 1
    return 0


def track(model: str, camera: str, fps: float, out: IO[str]) -> None:
    import cv2
    import mediapipe as mp
    from mediapipe.tasks.python import BaseOptions, vision

    device, cap = _open_camera(cv2, camera)
    options = vision.HandLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=model),
        running_mode=vision.RunningMode.VIDEO,  # tracks between frames: faster, steadier
        num_hands=1,
        min_hand_detection_confidence=0.6,
        min_hand_presence_confidence=0.6,
        min_tracking_confidence=0.6,
    )
    try:
        with vision.HandLandmarker.create_from_options(options) as tracker:
            _emit(out, {"ready": device})
            while True:
                start = time.monotonic()
                ok, frame = cap.read()
                if not ok:
                    raise RuntimeError("the webcam stopped sending pictures")
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                found = tracker.detect_for_video(image, int(start * 1000))
                hand = _pixels(found.hand_landmarks[0], rgb) if found.hand_landmarks else None
                _emit(out, {"t": round(start, 3), "hand": hand})
                time.sleep(max(0.0, 1 / fps - (time.monotonic() - start)))
    finally:
        cap.release()


def _open_camera(cv2: Any, camera: str) -> tuple[str, Any]:
    """Open the camera Lex chose, or the first /dev/video* that gives a picture.

    A webcam usually has extra /dev/video nodes that are metadata, not video.
    """
    devices = [camera] if camera else [str(p) for p in sorted(Path("/dev").glob("video[0-9]*"))]
    for device in devices:
        backend = cv2.CAP_V4L2 if device.startswith("/dev/") else cv2.CAP_ANY  # file = test video
        cap = cv2.VideoCapture(device, backend)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_WIDTH * 3 // 4)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # always the newest frame, not a queued one
        if cap.isOpened() and cap.read()[0]:
            return device, cap
        cap.release()
    if not devices:
        raise RuntimeError("no webcam is connected (no /dev/video* device)")
    raise RuntimeError(f"no picture from {', '.join(devices)} (is another app using the webcam?)")


def _pixels(points: Any, image: Any) -> list[list[float]]:
    """MediaPipe's 0..1 coordinates → pixels, so a 16:9 camera doesn't squash
    distances. Its z (depth) is already on the same scale as x."""
    h, w = image.shape[:2]
    return [[round(p.x * w, 1), round(p.y * h, 1), round(p.z * w, 1)] for p in points]


def _emit(out: IO[str], message: dict[str, Any]) -> None:
    out.write(json.dumps(message) + "\n")


if __name__ == "__main__":
    sys.exit(main())
