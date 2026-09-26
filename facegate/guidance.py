"""Live coaching through Gaze's enrollment poses.

Gaze asks for five poses (straight, up, down, left, right) and says each once;
it never says how close you are. It judges a pose from five face points: the
nose's offset from the eye line, as a share of the eye distance (yaw) and of
the eyes-to-mouth distance (pitch), relative to the straight pose it captured.
The preview's face mesh has the same points, so FaceGate measures the same
two numbers on the same mirrored frames and says what to change.

This is guidance only; Gaze alone decides when a pose is captured.
"""

from __future__ import annotations

import time
from collections import deque

import numpy

# Face Mesh indices standing in for Gaze's five points.
IRISES = (468, 473)
NOSE_TIP = 1
MOUTH_CORNERS = (61, 291)

# Gaze's pose rules (gaze-vision face.rs).
STRAIGHT_YAW = 0.18
STRAIGHT_PITCH = (0.2, 0.8)
TURN = 0.16
TILT = 0.07
# A face's box must span this share of the frame's short side (enrollment).
FACE_SIZE = (0.25, 0.78)
STEADY_YAW = 0.08
STEADY_PITCH = 0.06

# A new instruction must hold this long before it replaces the one showing,
# so a wavering head doesn't make the words flicker. Problems show at once.
SETTLE_SECONDS = 0.4
# Poses from about this long before a capture set the straight baseline.
BASELINE_SECONDS = 0.6
# Coaching distance starts this far inside Gaze's face size limits, so the
# user settles safely within them.
SIZE_MARGIN = (1.15, 0.92)
# Holding a pose this long without Gaze capturing it usually means the face is
# too small for one of the cameras (the IR camera sees a narrower frame), so
# FaceGate asks to come closer, while there's room to.
STALL_SECONDS = 3.0
STALL_ROOM = 0.5

LOOK = "Look straight at the camera."
HOLD = "Hold still…"
HOLD_THERE = "Hold it there…"
GOT_IT = "Got it."

PROMPTS = {
    "look-straight": "straight",
    "face the camera": "straight",
    "look-up": "up",
    "tilt your face slightly up": "up",
    "look-down": "down",
    "tilt your face slightly down": "down",
    "look-left": "left",
    "turn your face slightly left": "left",
    "look-right": "right",
    "turn your face slightly right": "right",
}
CAPTURED = {"captured", "face captured"}

# Gaze's camera problems, which outrank any pose advice.
PROBLEMS = {
    "too-dark": "Too dark. Face a light, or move closer.",
    "need more light...": "Too dark. Face a light, or move closer.",
    "clipped": "Move back so your whole face is in view.",
    "face is clipped. please move back...": "Move back so your whole face is in view.",
    "not-centered": "Move your face to the middle of the camera.",
    "please center your face...": "Move your face to the middle of the camera.",
    "too-far": "Move a little closer.",
    "please come closer...": "Move a little closer.",
    "too-close": "Move a little farther away.",
    "please back up...": "Move a little farther away.",
}

# Which star arrow each instruction shows: a turn, a tilt, or a move, and its
# direction as seen in the mirrored preview.
CLOSER = "Move a little closer."
FARTHER = "Move a little farther away."
TURN_LEFT_TO_FACE = "Turn a little to the left, to face the camera."
TURN_RIGHT_TO_FACE = "Turn a little to the right, to face the camera."
CHIN_DOWN = "Lower your chin a little."
CHIN_UP = "Raise your chin a little."
MOVES = {
    "left": "Move a little to the left.",
    "right": "Move a little to the right.",
    "up": "Move up a little.",
    "down": "Move down a little.",
}

START = {
    "up": "Slowly tilt your chin up.",
    "down": "Slowly tilt your chin down.",
    "left": "Slowly turn your head to the left.",
    "right": "Slowly turn your head to the right.",
}
MORE = {
    "up": "A little higher…",
    "down": "A little lower…",
    "left": "A little more to the left…",
    "right": "A little more to the right…",
}


def arrow_for(text: str) -> str:
    """The star arrow for an instruction: "turn-left", "tilt-up", "move-right", or ""."""
    for direction in ("left", "right"):
        if text in (START[direction], MORE[direction]):
            return f"turn-{direction}"
    for direction in ("up", "down"):
        if text in (START[direction], MORE[direction]):
            return f"tilt-{direction}"
    for direction, move in MOVES.items():
        if text == move:
            return f"move-{direction}"
    if text == CLOSER:
        return "closer"
    if text == FARTHER:
        return "back"
    return {TURN_LEFT_TO_FACE: "turn-left", TURN_RIGHT_TO_FACE: "turn-right", CHIN_DOWN: "tilt-down", CHIN_UP: "tilt-up"}.get(text, "")


def head_pose(points: numpy.ndarray) -> tuple[float, float] | None:
    """Gaze's yaw and pitch from face mesh points (x, y, …) in frame pixels."""
    if len(points) <= max(IRISES):
        return None
    irises = points[list(IRISES), :2]
    # Gaze's first eye point is the one on the frame's left.
    left, right = sorted(irises.tolist())
    nose = points[NOSE_TIP, :2]
    mouth = points[list(MOUTH_CORNERS), :2].mean(axis=0)
    eye_line = numpy.subtract(right, left)
    eye_distance = float(numpy.hypot(*eye_line))
    if eye_distance <= 1e-6:
        return None
    horizontal = eye_line / eye_distance
    vertical = numpy.array([-horizontal[1], horizontal[0]])
    centre = numpy.add(left, right) / 2
    mouth_distance = float((mouth - centre) @ vertical)
    if mouth_distance <= 1e-6:
        return None
    nose_from_eyes = nose - centre
    return float(nose_from_eyes @ horizontal) / eye_distance, float(nose_from_eyes @ vertical) / mouth_distance


class Guide:
    """What to tell the user, from Gaze's prompts and status and the measured pose."""

    def __init__(self, clock=time.monotonic) -> None:
        self.clock = clock
        self.pose_name: str | None = None
        self.completed_pose: str | None = None
        self.problem: str | None = None
        self.captured_at = -1.0
        self.baseline: tuple[float, float] | None = None
        self.poses: deque[tuple[float, float, float]] = deque()
        self.face = False
        # Where the face's centre sits in its camera frame, 0..1 on each axis.
        self.placement: tuple[float, float] | None = None
        self.status_key = ""
        # The face box's share of the frame's short side, and since when a
        # reached pose has been held without a capture.
        self.face_size: float | None = None
        self.holding_since: float | None = None
        self.shown = ""
        self.pending = ""
        self.pending_since = 0.0

    # Input ----------------------------------------------------------------------

    def prompt(self, key: str) -> None:
        """An EnrollStatus prompt from Gaze."""
        if key in CAPTURED:
            self.completed_pose = self.pose_name
            if self.pose_name == "straight" and self.baseline is None:
                recent = [(yaw, pitch) for when, yaw, pitch in self.poses if when >= self.clock() - BASELINE_SECONDS]
                # Gaze captured a straight pose, so poses that weren't are stale.
                straight = [pose for pose in recent if abs(pose[0]) < STRAIGHT_YAW and STRAIGHT_PITCH[0] < pose[1] < STRAIGHT_PITCH[1]]
                recent = straight or recent
                if recent:
                    self.baseline = tuple(numpy.median(numpy.array(recent), axis=0).tolist())
            self.captured_at = self.clock()
            self.poses.clear()
            self.holding_since = None
        elif key in PROMPTS:
            self.pose_name = PROMPTS[key]

    def mark_pose(self) -> str:
        """The pose the between-pose seal should turn with.

        IR often arrives before Gaze says "captured", so the pose being held
        is the one. Once a capture lands, that pose wins for a moment even if
        Gaze has already asked for the next.
        """
        if self.completed_pose and self.clock() - self.captured_at < 2.0:
            return self.completed_pose
        return self.pose_name or self.completed_pose or ""

    def status(self, key: str) -> None:
        """A FaceStatus from Gaze."""
        self.status_key = key
        self.problem = PROBLEMS.get(key)

    def pose(self, points: numpy.ndarray | None, source: tuple[int, int] | None = None) -> None:
        """The newest preview's face mesh, or None when it shows no face.

        ``source`` is the camera frame's (width, height).
        """
        measured = head_pose(points) if points is not None else None
        self.face = measured is not None
        if measured is not None and source:
            low, high = points[:, :2].min(axis=0), points[:, :2].max(axis=0)
            centre = (low + high) / 2
            self.placement = (float(centre[0]) / source[0], float(centre[1]) / source[1])
            self.face_size = float((high - low).max()) / min(source)
        if measured is not None:
            now = self.clock()
            self.poses.append((now, *measured))
            while self.poses and self.poses[0][0] < now - 1.0:
                self.poses.popleft()

    # Output ---------------------------------------------------------------------

    def advice(self) -> str:
        """The instruction for this moment, before settling."""
        if self.clock() - self.captured_at < 0.5:
            return GOT_IT
        if self.problem:
            if self.status_key in ("not-centered", "please center your face...") and self.placement:
                return self._centre()
            return self.problem
        if self.face and self.face_size is not None:
            if self.face_size < FACE_SIZE[0] * SIZE_MARGIN[0]:
                return CLOSER
            if self.face_size > FACE_SIZE[1] * SIZE_MARGIN[1]:
                return FARTHER
        pose = self._pose()
        if pose not in (HOLD, HOLD_THERE):
            self.holding_since = None
            return pose
        now = self.clock()
        if self.holding_since is None:
            self.holding_since = now
        if now - self.holding_since >= STALL_SECONDS and (self.face_size or 0) < STALL_ROOM:
            return CLOSER
        return pose

    def _pose(self) -> str:
        """What the current pose needs, from the measured head pose."""
        if not self.face or not self.poses or self.pose_name is None:
            return LOOK if self.pose_name in (None, "straight") else START.get(self.pose_name, LOOK)
        _, yaw, pitch = self.poses[-1]
        name = self.pose_name
        if name == "straight":
            # Seen in the mirror: a nose right of centre means turn left.
            if yaw > STRAIGHT_YAW:
                return TURN_LEFT_TO_FACE
            if yaw < -STRAIGHT_YAW:
                return TURN_RIGHT_TO_FACE
            if pitch < STRAIGHT_PITCH[0]:
                return CHIN_DOWN
            if pitch > STRAIGHT_PITCH[1]:
                return CHIN_UP
            return HOLD
        if self.baseline is None:
            return START[name]
        base_yaw, base_pitch = self.baseline
        if name in ("up", "down"):
            if abs(yaw - base_yaw) > TURN:
                return "Keep your head facing forward as you tilt."
            moved = (base_pitch - pitch) if name == "up" else (pitch - base_pitch)
            needed = TILT
        else:
            moved = (base_yaw - yaw) if name == "left" else (yaw - base_yaw)
            needed = TURN
        if moved >= needed:
            return HOLD_THERE
        if moved >= needed * 0.4:
            return MORE[name]
        return START[name]

    def _centre(self) -> str:
        """Which way to move a face that's off-centre, as seen in the mirror."""
        x, y = self.placement
        dx, dy = x - 0.5, y - 0.5
        if abs(dx) >= abs(dy):
            return MOVES["left" if dx > 0 else "right"]
        return MOVES["up" if dy > 0 else "down"]

    def hint(self) -> str | None:
        """A new instruction to show, or None to keep the current one."""
        now = self.clock()
        advice = self.advice()
        if advice == self.shown:
            self.pending = ""
            return None
        urgent = advice in (GOT_IT, self.problem) or not self.shown
        if advice != self.pending:
            self.pending, self.pending_since = advice, now
        if urgent or now - self.pending_since >= SETTLE_SECONDS:
            self.shown, self.pending = advice, ""
            return advice
        return None
