"""handsigns: name hand signs from a webcam (✌ peace, 👍 thumbs_up, …) with MediaPipe.

    from handsigns import HandTracker

    with HandTracker() as hands:      # first webcam that works
        for frame in hands:
            if frame.fired == "peace":
                print("✌")

Without a camera, `classify` names the sign in 21 hand points you already
have, and `Debouncer` decides when a held sign counts.
"""

from handsigns.model import ModelError, ensure_model
from handsigns.signs import (
    EMOJI,
    FINGERS,
    SIGNS,
    THUMB_SIGNS,
    Debouncer,
    all_signs,
    classify,
    fingers_up,
    label,
    parse_shapes,
)
from handsigns.tracker import CameraError, Frame, HandTracker, SignReader, read_photo

__all__ = [
    "EMOJI", "FINGERS", "SIGNS", "THUMB_SIGNS", "CameraError", "Debouncer", "Frame",
    "HandTracker", "ModelError", "SignReader", "all_signs", "classify", "ensure_model",
    "fingers_up", "label", "parse_shapes", "read_photo",
]  # fmt: skip
