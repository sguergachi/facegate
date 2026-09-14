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
import threading
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


def rename_model(models: list[dict[str, Any]], model_id: int, label: str) -> list[dict[str, Any]]:
    return [dict(model, label=label) if int(model.get("id", index)) == model_id else model for index, model in enumerate(models)]


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

    def scan(self, width: int, height: int, stopped: Callable[[], bool], clock: Callable[[], float] = time.monotonic) -> Iterator[dict[str, Any]]:
        """Yield frame/hint/progress events, then one `captured` event.

        The preview runs at the camera's own rate. A camera thread reads every
        frame and publishes a cropped preview of each lit one at once; an
        analysis thread takes the newest lit frame whenever it is free, finds
        the face (which the crop follows) and collects samples. Face detection
        and recognition take far longer than a frame, but never hold the
        preview up. Only the newest preview is ever sent, so a slow reader
        sees the present rather than a backlog.
        """
        scan = _Scan(self, width, height, clock)
        workers = [threading.Thread(target=scan.read_camera, daemon=True), threading.Thread(target=scan.analyse, daemon=True)]
        yield {"event": "hint", "text": HINTS["camera"]}
        for worker in workers:
            worker.start()
        try:
            while not stopped():
                preview, events, failure = scan.take()
                if preview is not None:
                    yield preview
                for event in events:
                    yield event
                    if event["event"] == "captured":
                        return
                if failure:
                    raise EnrollError(failure)
        finally:
            scan.finish()
            for worker in workers:
                worker.join(timeout=2)


class _Scan:
    """What the camera and analysis threads share during one scan."""

    def __init__(self, scanner: Scanner, width: int, height: int, clock: Callable[[], float]) -> None:
        self.scanner, self.width, self.height, self.clock = scanner, width, height, clock
        self.aspect = width / height
        # The scan opens by saying it's waiting for the camera.
        self.state = ScanState(hint="camera")
        self.follow = Follow()
        self.changed = threading.Condition()
        self.stop = False
        self.started = clock()
        # The newest lit frame for analysis (frame, grey, number), and the face in it.
        self.latest: tuple[Any, Any, int] | None = None
        self.face_box: tuple[float, float, float, float] | None = None
        self.preview: dict[str, Any] | None = None
        self.events: list[dict[str, Any]] = []
        self.failure = ""

    # Shared ---------------------------------------------------------------------

    def post(self, event: dict[str, Any]) -> None:
        with self.changed:
            self.events.append(event)
            self.changed.notify_all()

    def hint(self, key: str) -> None:
        with self.changed:
            if self.state.hint != key:
                self.state.hint = key
                self.events.append({"event": "hint", "text": HINTS[key]})
                self.changed.notify_all()

    def fail(self, message: str) -> None:
        with self.changed:
            self.failure = self.failure or message
            self.changed.notify_all()

    def take(self) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str]:
        """Wait briefly for anything new, then hand it all over."""
        with self.changed:
            self.changed.wait_for(lambda: self.preview is not None or self.events or self.failure or self.stop, timeout=0.05)
            preview, self.preview = self.preview, None
            events, self.events = self.events, []
            failure = self.failure
            if not failure and self.clock() - self.started > SCAN_SECONDS:
                state = self.state
                failure = FAILURES["black"] if state.frames and state.black_frames == state.frames else FAILURES[state.trouble]
        return preview, events, failure

    def finish(self) -> None:
        with self.changed:
            self.stop = True
            self.changed.notify_all()

    # Camera thread ------------------------------------------------------------------

    def read_camera(self) -> None:
        cv2 = self.scanner.cv2
        last = self.clock()
        while not self.stop:
            frame = self.scanner.read_frame()
            now = self.clock()
            elapsed, last = now - last, now
            if frame is None:
                self.fail("The camera stopped sending frames.")
                return
            gray = self.scanner.clahe.apply(frame if frame.ndim == 2 else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
            hist = cv2.calcHist([gray], [0], None, [8], [0, 256])
            total = float(hist.sum())
            darkness = float(hist.ravel()[0]) / total * 100 if total else 100.0
            state = self.state
            with self.changed:
                state.frames += 1
                lit = darkness < 100 and darkness <= self.scanner.dark_threshold
                if not lit:
                    # Many IR emitters strobe, lighting every other frame: skip the
                    # unlit ones silently, and only complain when the light is gone.
                    state.black_frames += darkness >= 100
                    unlit_for = now - (state.last_lit or self.started)
                else:
                    state.last_lit = now
                    self.latest = (frame, gray, state.frames)
                face_box = self.face_box
            if not lit:
                if unlit_for > UNLIT_GRACE:
                    state.trouble = "black" if darkness >= 100 else "dark"
                    self.hint(state.trouble)
                continue
            frame_height, frame_width = gray.shape[:2]
            self.follow.step(crop_target(frame_width, frame_height, face_box, self.aspect), elapsed)
            x, y, crop_width, crop_height = crop_rect(self.follow.center_x, self.follow.center_y, self.follow.height, frame_width, frame_height, self.aspect)
            preview = cv2.resize(gray[y:y + crop_height, x:x + crop_width], (self.width, self.height), interpolation=cv2.INTER_AREA)
            event = {"event": "frame", "w": self.width, "h": self.height, "data": base64.b64encode(preview.tobytes()).decode("ascii")}
            with self.changed:
                self.preview = event
                self.changed.notify_all()

    # Analysis thread ----------------------------------------------------------------

    def analyse(self) -> None:
        scanner, cv2, state = self.scanner, self.scanner.cv2, self.state
        seen = 0
        while True:
            with self.changed:
                self.changed.wait_for(lambda: self.stop or (self.latest is not None and self.latest[2] != seen), timeout=0.5)
                if self.stop:
                    return
                if self.latest is None or self.latest[2] == seen:
                    continue
                frame, gray, seen = self.latest
            try:
                if self._analyse_frame(scanner, cv2, state, frame, gray):
                    return
            except EnrollError as error:
                self.fail(str(error))
                return

    def _analyse_frame(self, scanner: Scanner, cv2: Any, state: ScanState, frame: Any, gray: Any) -> bool:
        """Find the face and maybe take a sample; True once enough are captured."""
        now = self.clock()
        frame_height = gray.shape[0]
        scale = (scanner.max_height / frame_height) if scanner.max_height and frame_height > scanner.max_height else 1.0
        small = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale != 1 else gray
        faces = list(scanner.detector(small, 0)) or list(scanner.detector(small, 1))
        if len(faces) == 1:
            rect = faces[0]
            self.face_box = (rect.left() / scale, rect.top() / scale, rect.right() / scale, rect.bottom() / scale)
            state.last_face = now
        else:
            self.face_box = None
        if len(faces) > 1:
            state.trouble = "many"
            self.hint("many")
            return False
        if not faces:
            if now - state.last_face > NO_FACE_GRACE:
                state.trouble = "none"
                self.hint("none")
            return False
        rect = faces[0]
        if rect.height() < MIN_FACE_FRACTION * small.shape[0]:
            state.trouble = "far"
            self.hint("far")
            return False
        self.hint("move" if state.samples else "hold")
        if now - state.last_sample < SAMPLE_SPACING:
            return False
        if scanner.encode:
            color = frame if frame.ndim == 3 else cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
            color = cv2.resize(color, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale != 1 else color
            encoding = scanner.encode(color, rect)
        else:
            # Test mode: identical samples, so they arrive at the patient pace.
            encoding = [0.0]
        if not accepts(state, encoding, now):
            return False
        state.samples.append(encoding)
        state.last_sample = now
        self.post({"event": "progress", "value": len(state.samples) / SAMPLES})
        if len(state.samples) >= SAMPLES:
            self.post({"event": "captured", "encodings": state.samples})
            return True
        return False


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

    # Read frames as they arrive rather than from a queue of stale ones.
    import cv2

    capture.internal.set(cv2.CAP_PROP_BUFFERSIZE, 1)

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
