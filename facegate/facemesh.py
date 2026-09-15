"""The face's structure in a preview frame, for starlight to gather on.

A colour camera lights the room as brightly as the face, so its brightness
alone makes a poor portrait in stars. Instead the preview finds the face's
478 surface points with MediaPipe's face detector and Face Mesh, and each star
takes one point, lit by how near it is to the camera: the shape IR shows
without IR.

This is presentation only. Gaze does its own detection and recognition; these
points are never sent to it and never stored. ONNX Runtime is optional: without
it, or without the models, the preview falls back to the frame's brightness.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy

MODELS = Path(__file__).resolve().parent / "models"
DETECTOR_SIZE = 128
LANDMARKS_SIZE = 256
DETECTION_SCORE = 0.5
PRESENCE_SCORE = 0.5
# MediaPipe crops a square 1.5 times the detected box, turned upright by the eyes.
ROI_SCALE = 1.5


def _anchors() -> numpy.ndarray:
    """BlazeFace short-range anchors: 2 per cell at stride 8, 6 at stride 16."""
    centres = []
    for stride, count in ((8, 2), (16, 6)):
        cells = DETECTOR_SIZE // stride
        for y in range(cells):
            for x in range(cells):
                centres.extend([((x + 0.5) / cells, (y + 0.5) / cells)] * count)
    return numpy.array(centres, numpy.float32)


def warp(image: numpy.ndarray, cx: float, cy: float, size: float, angle: float, out: int) -> numpy.ndarray:
    """A bilinear ``out``×``out`` crop: a square of ``size`` centred and turned by ``angle``."""
    height, width = image.shape[:2]
    steps = (numpy.arange(out, dtype=numpy.float32) + 0.5) / out - 0.5
    gx, gy = numpy.meshgrid(steps, steps)
    cos, sin = math.cos(angle), math.sin(angle)
    xs = cx + (gx * cos - gy * sin) * size - 0.5
    ys = cy + (gx * sin + gy * cos) * size - 0.5
    left, top = numpy.floor(xs), numpy.floor(ys)
    fx, fy = (xs - left)[..., None], (ys - top)[..., None]
    x0 = numpy.clip(left.astype(int), 0, width - 1)
    y0 = numpy.clip(top.astype(int), 0, height - 1)
    x1 = numpy.clip(x0 + 1, 0, width - 1)
    y1 = numpy.clip(y0 + 1, 0, height - 1)
    pixels = image.astype(numpy.float32)
    return (
        pixels[y0, x0] * (1 - fx) * (1 - fy)
        + pixels[y0, x1] * fx * (1 - fy)
        + pixels[y1, x0] * (1 - fx) * fy
        + pixels[y1, x1] * fx * fy
    )


def upright(angle: float) -> float:
    """The eye line's angle, folded to within a quarter turn.

    Gaze mirrors its previews, so the detector's "right eye" may be on either
    side; a face is never upside down in a webcam, so a half turn is always a
    mirrored eye pair rather than a rolled head.
    """
    return (angle + math.pi / 2) % math.pi - math.pi / 2


def _sigmoid(value: float | numpy.ndarray) -> float | numpy.ndarray:
    return 1 / (1 + numpy.exp(-numpy.clip(value, -60, 60)))


class FaceMesh:
    """MediaPipe's face detector and Face Mesh, run with ONNX Runtime on the CPU."""

    def __init__(self, models: Path = MODELS) -> None:
        import onnxruntime

        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = 2
        options.log_severity_level = 3

        def session(name: str) -> onnxruntime.InferenceSession:
            return onnxruntime.InferenceSession(str(models / name), options, providers=["CPUExecutionProvider"])

        self.detector = session("face_detector.onnx")
        self.landmarks = session("face_landmarks.onnx")
        self.detector_input = self.detector.get_inputs()[0].name
        self.landmarks_input = self.landmarks.get_inputs()[0].name
        self.anchors = _anchors()

    def detect(self, image: numpy.ndarray) -> tuple[float, float, float, float] | None:
        """The most confident face: centre, square side and eye angle, in image pixels."""
        height, width = image.shape[:2]
        side = max(width, height)
        crop = warp(image, width / 2, height / 2, side, 0.0, DETECTOR_SIZE)
        boxes, scores = self.detector.run(None, {self.detector_input: (crop[None] / 127.5 - 1).astype(numpy.float32)})
        scores = _sigmoid(scores[0, :, 0])
        best = int(scores.argmax())
        if scores[best] < DETECTION_SCORE:
            return None
        box = boxes[0, best] / DETECTOR_SIZE
        anchor_x, anchor_y = self.anchors[best]
        # Keypoints 0 and 1 are the eyes.
        eye_x = box[6] - box[4]
        eye_y = box[7] - box[5]
        return (
            width / 2 + (anchor_x + box[0] - 0.5) * side,
            height / 2 + (anchor_y + box[1] - 0.5) * side,
            float(max(box[2], box[3])) * side * ROI_SCALE,
            upright(math.atan2(eye_y, eye_x)),
        )

    def find(self, image: numpy.ndarray) -> numpy.ndarray | None:
        """Each surface point as (x, y, nearness 0..1) in image pixels, or None without a face.

        ``image`` is an RGB array, height × width × 3.
        """
        found = self.detect(image)
        if found is None:
            return None
        cx, cy, size, angle = found
        crop = warp(image, cx, cy, size, angle, LANDMARKS_SIZE)
        outputs = self.landmarks.run(None, {self.landmarks_input: (crop[None] / 255.0).astype(numpy.float32)})
        if _sigmoid(float(outputs[1].ravel()[0])) < PRESENCE_SCORE:
            return None
        points = outputs[0].reshape(-1, 3)
        u = points[:, 0] / LANDMARKS_SIZE - 0.5
        v = points[:, 1] / LANDMARKS_SIZE - 0.5
        cos, sin = math.cos(angle), math.sin(angle)
        # Nearness over the face's surface; the irises' depth runs off its range.
        depth = points[:, 2]
        surface = depth[:468]
        near = numpy.clip((surface.max() - depth) / max(float(surface.max() - surface.min()), 1e-6), 0, 1)
        return numpy.stack([cx + (u * cos - v * sin) * size, cy + (u * sin + v * cos) * size, near], axis=1).astype(numpy.float32)


class Framing:
    """Where the preview looks: the face, eased, filling the frame's height.

    Gaze switches between its colour and IR cameras at every pose, and they
    differ in resolution and field of view, so the same face lands on other
    pixels. The view is therefore kept relative to the face (its offset and
    zoom in face heights) and carried across a camera switch in those terms,
    so the preview never jumps.
    """

    # The face mesh spans forehead to chin; the preview shows it this many
    # times over, so the brow and jaw clear the rounded corners.
    ZOOM = 1.35
    EASE = 0.35

    def __init__(self, width: int, height: int) -> None:
        self.width, self.height = width, height
        self.view: numpy.ndarray | None = None
        self.source: tuple[int, int] | None = None
        # The view as (x offset, y offset, span), in face heights from the face's centre.
        self.relative: numpy.ndarray | None = None

    def reset(self) -> None:
        self.view = self.source = self.relative = None

    def place(self, mesh: numpy.ndarray, source: tuple[int, int] | None = None) -> numpy.ndarray:
        """The mesh in preview pixels: x, y, and each point's nearness 0..1.

        ``source`` is the camera frame's size, telling cameras apart.
        """
        low, high = mesh[:, :2].min(axis=0), mesh[:, :2].max(axis=0)
        centre = (low + high) / 2
        face = max(float(high[1] - low[1]), 1.0)
        target = numpy.array([*centre, face * self.ZOOM], numpy.float32)
        if self.view is None or self.relative is None:
            self.view = target
        elif source != self.source:
            self.view = numpy.array([*(centre + self.relative[:2] * face), self.relative[2] * face], numpy.float32)
        else:
            self.view += (target - self.view) * self.EASE
        self.source = source
        self.relative = numpy.array([*(self.view[:2] - centre) / face, self.view[2] / face], numpy.float32)
        cx, cy, span = self.view
        scale = self.height / max(float(span), 1.0)
        return numpy.stack([
            (mesh[:, 0] - cx) * scale + self.width / 2,
            (mesh[:, 1] - cy) * scale + self.height / 2,
            mesh[:, 2],
        ], axis=1).astype(numpy.float32)
