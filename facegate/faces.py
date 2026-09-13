"""Howdy's face models, and the enrollment scan behind "Add Face".

The models are Howdy's own JSON files, so `howdy list`, `howdy remove` and
compare.py keep working with faces added here. Reading and writing them needs
root, which is why this runs inside the pkexec'd facegate-faces helper.

The scan is Howdy's `add` with a better conversation: it streams a cropped,
face-following IR preview for the island, says what it needs ("move closer"),
and keeps several encodings a moment apart rather than a single frame.
"""

from __future__ import annotations

import base64
import json
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

HOWDY_DIR = "/usr/lib/security/howdy"
LABEL_CHARS = 24

SAMPLES = 5
SAMPLE_SPACING = 0.35
# Encodings of a perfectly still face sit ~0.03 apart; accept a new one when it
# adds something, or after a while anyway so a still user still finishes.
SAMPLE_NOVELTY = 0.06
SAMPLE_PATIENCE = 0.9
MIN_FACE_FRACTION = 0.2
SCAN_SECONDS = 30.0
NO_FACE_GRACE = 0.8
UNLIT_GRACE = 0.6

# The preview keeps the face at about half its height, eyes a little above center.
FACE_FILL = 0.5
FOLLOW_RATE = 7.0


# Models ----------------------------------------------------------------------


def models_path(user: str, howdy_dir: str = HOWDY_DIR) -> str:
    return os.path.join(howdy_dir, "models", user + ".dat")


def load_models(path: str) -> list[dict[str, Any]]:
    try:
        with open(path) as models:
            value = json.load(models)
    except FileNotFoundError:
        return []
    return [model for model in value if isinstance(model, dict)] if isinstance(value, list) else []


def save_models(path: str, models: list[dict[str, Any]]) -> None:
    """Write atomically; like Howdy, no models means no file at all."""
    if not models:
        if os.path.exists(path):
            os.remove(path)
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".facegate-tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(models, output)
    os.replace(temporary, path)


def summarize(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"id": int(model.get("id", index)), "label": str(model.get("label", "")), "time": int(model.get("time", 0))}
        for index, model in enumerate(models)
    ]


def remove_model(models: list[dict[str, Any]], model_id: int) -> list[dict[str, Any]]:
    return [model for index, model in enumerate(models) if int(model.get("id", index)) != model_id]


def clean_label(text: object) -> str:
    return "".join(char for char in str(text or "") if char.isprintable()).strip()[:LABEL_CHARS]


def next_label(models: list[dict[str, Any]]) -> str:
    """"Face", then "Face 2", "Face 3"... skipping names already taken."""
    taken = {str(model.get("label", "")) for model in models}
    if "Face" not in taken and not models:
        return "Face"
    number = 2
    while "Face %d" % number in taken:
        number += 1
    return "Face %d" % number


def new_model(models: list[dict[str, Any]], label: str, encodings: list[list[float]], now: float) -> dict[str, Any]:
    # Howdy numbers models by count, which repeats an id after a removal.
    ids = [int(model.get("id", index)) for index, model in enumerate(models)]
    return {"time": int(now), "label": label, "id": max(ids) + 1 if ids else 0, "data": encodings}


# Preview framing -------------------------------------------------------------


Box = tuple[float, float, float, float]  # left, top, right, bottom


def crop_target(frame_width: int, frame_height: int, face: Box | None, aspect: float) -> tuple[float, float, float]:
    """Center and height of the crop that frames the face (or the whole view)."""
    widest = min(float(frame_height), frame_width / aspect)
    if face is None:
        return frame_width / 2, frame_height / 2, widest
    left, top, right, bottom = face
    face_height = bottom - top
    height = min(widest, max(face_height * 1.25, face_height / FACE_FILL))
    return (left + right) / 2, (top + bottom) / 2 + face_height * 0.08, height


def crop_rect(center_x: float, center_y: float, height: float, frame_width: int, frame_height: int, aspect: float) -> tuple[int, int, int, int]:
    """Integer x, y, width, height of the crop, slid back inside the frame."""
    width = min(float(frame_width), height * aspect)
    height = min(float(frame_height), width / aspect)
    x = min(max(center_x - width / 2, 0.0), frame_width - width)
    y = min(max(center_y - height / 2, 0.0), frame_height - height)
    return round(x), round(y), max(1, round(width)), max(1, round(height))


@dataclass
class Follow:
    """The crop eases toward the face, so the preview glides instead of jumping."""

    center_x: float = 0.0
    center_y: float = 0.0
    height: float = 0.0
    ready: bool = False

    def step(self, target: tuple[float, float, float], elapsed: float) -> None:
        if not self.ready:
            self.center_x, self.center_y, self.height = target
            self.ready = True
            return
        amount = 1 - math.exp(-FOLLOW_RATE * elapsed)
        self.center_x += (target[0] - self.center_x) * amount
        self.center_y += (target[1] - self.center_y) * amount
        self.height += (target[2] - self.height) * amount


# Scan ------------------------------------------------------------------------


class EnrollError(Exception):
    pass


HINTS = {
    "camera": "Waiting for the camera…",
    "black": "The camera only sees black.",
    "dark": "Too dark. Move a little closer.",
    "none": "Look straight at the camera.",
    "many": "Make sure only you are in view.",
    "far": "Move a little closer.",
    "hold": "Hold still and look at the camera.",
    "move": "Now slowly move your head in a circle.",
}
FAILURES = {
    "black": "The camera only saw black. Is the IR emitter working?",
    "dark": "It was too dark to see you.",
    "many": "More than one face was in view.",
    "none": "No face was found.",
    "far": "Your face was too far from the camera.",
}


@dataclass
class ScanState:
    samples: list[list[float]] = field(default_factory=list)
    last_sample: float = -1.0
    hint: str = ""
    last_face: float = 0.0
    trouble: str = "none"
    black_frames: int = 0
    last_lit: float = 0.0
    frames: int = 0


def distance(first: list[float], second: list[float]) -> float:
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(first, second)))


def accepts(state: ScanState, encoding: list[float], now: float) -> bool:
    if not state.samples:
        return True
    novel = min(distance(encoding, sample) for sample in state.samples) > SAMPLE_NOVELTY
    return novel or now - state.last_sample > SAMPLE_PATIENCE


class Scanner:
    """Reads the camera, frames the face, and collects encodings.

    `encode` turns (scaled BGR frame, face rectangle) into an encoding; without
    it (test mode) samples are simply counted as the face holds steady.
    """

    def __init__(self, read_frame: Callable[[], Any], max_height: float, dark_threshold: float, encode: Callable[[Any, Any], list[float]] | None) -> None:
        import dlib  # noqa: F401  dlib must load before OpenCV, as in Howdy.
        import cv2

        self.cv2 = cv2
        self.detector = dlib.get_frontal_face_detector()
        self.read_frame = read_frame
        self.max_height = max_height
        self.dark_threshold = dark_threshold
        self.encode = encode
        self.clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))

    def _hint(self, state: ScanState, key: str) -> Iterator[dict[str, Any]]:
        if state.hint != key:
            state.hint = key
            yield {"event": "hint", "text": HINTS[key]}

    def scan(self, width: int, height: int, stopped: Callable[[], bool], clock: Callable[[], float] = time.monotonic) -> Iterator[dict[str, Any]]:
        """Yield frame/hint/progress events, then one `captured` event."""
        cv2 = self.cv2
        state = ScanState()
        follow = Follow()
        aspect = width / height
        started = last = clock()
        yield from self._hint(state, "camera")
        while not stopped():
            now = clock()
            elapsed, last = now - last, now
            if now - started > SCAN_SECONDS:
                if state.frames and state.black_frames == state.frames:
                    raise EnrollError(FAILURES["black"])
                raise EnrollError(FAILURES[state.trouble])
            frame = self.read_frame()
            if frame is None:
                raise EnrollError("The camera stopped sending frames.")
            state.frames += 1
            gray = self.clahe.apply(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
            frame_height, frame_width = gray.shape[:2]
            hist = cv2.calcHist([gray], [0], None, [8], [0, 256])
            total = float(hist.sum())
            darkness = float(hist.ravel()[0]) / total * 100 if total else 100.0
            if darkness >= 100 or darkness > self.dark_threshold:
                # Many IR emitters strobe, lighting every other frame: skip the
                # unlit ones silently, and only complain when the light is gone.
                black = darkness >= 100
                state.black_frames += black
                if now - (state.last_lit or started) > UNLIT_GRACE:
                    state.trouble = "black" if black else "dark"
                    yield from self._hint(state, state.trouble)
                continue
            state.last_lit = now

            scale = (self.max_height / frame_height) if self.max_height and frame_height > self.max_height else 1.0
            small = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale != 1 else gray
            faces = list(self.detector(small, 0)) or list(self.detector(small, 1))

            face_box = None
            if len(faces) == 1:
                rect = faces[0]
                face_box = (rect.left() / scale, rect.top() / scale, rect.right() / scale, rect.bottom() / scale)
                state.last_face = now
            follow.step(crop_target(frame_width, frame_height, face_box, aspect), elapsed)
            x, y, crop_width, crop_height = crop_rect(follow.center_x, follow.center_y, follow.height, frame_width, frame_height, aspect)
            preview = cv2.resize(gray[y:y + crop_height, x:x + crop_width], (width, height), interpolation=cv2.INTER_AREA)
            yield {"event": "frame", "w": width, "h": height, "data": base64.b64encode(preview.tobytes()).decode("ascii")}

            if len(faces) > 1:
                state.trouble = "many"
                yield from self._hint(state, "many")
                continue
            if not faces:
                if now - state.last_face > NO_FACE_GRACE:
                    state.trouble = "none"
                    yield from self._hint(state, "none")
                continue
            rect = faces[0]
            if rect.height() < MIN_FACE_FRACTION * small.shape[0]:
                state.trouble = "far"
                yield from self._hint(state, "far")
                continue
            yield from self._hint(state, "move" if state.samples else "hold")
            if now - state.last_sample < SAMPLE_SPACING:
                continue
            if self.encode:
                color = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale != 1 else frame
                encoding = self.encode(color, rect)
            else:
                # Test mode: identical samples, so they arrive at the patient pace.
                encoding = [0.0]
            if not accepts(state, encoding, now):
                continue
            state.samples.append(encoding)
            state.last_sample = now
            yield {"event": "progress", "value": len(state.samples) / SAMPLES}
            if len(state.samples) >= SAMPLES:
                yield {"event": "captured", "encodings": state.samples}
                return


def howdy_scanner(howdy_dir: str = HOWDY_DIR) -> tuple[Scanner, Callable[[], None]]:
    """A scanner reading Howdy's camera settings and recognition models."""
    import configparser
    import sys

    data = os.path.join(howdy_dir, "dlib-data")
    if not os.path.isfile(os.path.join(data, "dlib_face_recognition_resnet_model_v1.dat")):
        raise EnrollError("Howdy's face data files are missing.")
    config = configparser.ConfigParser()
    config.read(os.path.join(howdy_dir, "config.ini"))
    device = config.get("video", "device_path", fallback="none")
    if not os.path.exists(device):
        raise EnrollError("Howdy has no camera configured.")
    sys.path.insert(0, howdy_dir)
    import dlib
    from recorders.video_capture import VideoCapture

    pose = dlib.shape_predictor(os.path.join(data, "shape_predictor_5_face_landmarks.dat"))
    encoder = dlib.face_recognition_model_v1(os.path.join(data, "dlib_face_recognition_resnet_model_v1.dat"))

    def encode(frame: Any, rect: Any) -> list[float]:
        # The same scaled color frame and landmarks compare.py uses.
        return list(encoder.compute_face_descriptor(frame, pose(frame, rect), 1))

    try:
        capture = VideoCapture(config)
    except SystemExit as error:
        raise EnrollError("Howdy couldn't open the camera.") from error

    def read_frame() -> Any:
        ok, frame = capture.internal.read()
        return frame if ok else None

    scanner = Scanner(
        read_frame,
        config.getfloat("video", "max_height", fallback=320.0),
        config.getfloat("video", "dark_threshold", fallback=50.0),
        encode,
    )
    return scanner, capture.release
