"""The enrollment preview as star dust that gathers into your face.

Like the particle cloud of Apple Watch pairing, the camera is never shown as a
picture. As soon as the preview opens, thousands of motes drift in a slow,
wandering nebula. When the camera finds a face, the dust condenses onto it
from the middle outward, each mote swirling in on its own path and flaring as
it settles, until the face stands as a lit sculpture of dust.

The motes don't sit on the face mesh's points, which would draw its grid.
Each has its own place on the face's surface: most scattered across its
triangles, some along the eyes, brows, lips and nose, so features are drawn
by light rather than by lines. The surface is shaded by a soft key light from
above left and dissolves at the silhouette. Motes differ like real dust: most
are faint grains, some are stars, a few are sparks that glint and bloom. They
never quite hold still: each drifts a fraction of a point and scintillates.

The face stands in a scene built in layers, back to front: a deep field of
faint distant stars that shifts against the head's turn, a concentrated
source behind the head that the star dust seems to come from, haze orbiting
through it, the steadily illuminated face itself, the coaching guide, and a
few out-of-focus motes drifting past the lens.

To coach, a guide appears in precise hairline geometry with its own dust
streaming along it: an arc orbiting the head for a turn or tilt, a straight
arrow for a move, and a head contour approaching or receding for closer or back.
Arrowheads lie flat on the screen so they read at any angle, and the
face keeps its light as the guide changes. When the scan ends the scene takes the
result's colour and winks out, in random order.

Without a face mesh (no ONNX Runtime), each mote instead takes a spot on a
field over the frame and that spot's brightness, as before.

Gaze's infrared bursts between poses release the dust; it gathers into a
tilted ring of dust with a check floating in its mouth, then condenses again
when the colour camera returns. The camera gives a frame about every 70 ms,
but the dust moves on springs every display frame, so motion is as smooth as
the display. Nothing is
rasterised by Python drawing code: NumPy integrates the motes and splats
them into two small textures at the display's own pixel density (every mote,
and the bright ones for glow), and render nodes (drawn on the GPU by GTK's
renderer, Vulkan for the island) tint them and bloom the glow through two
blurs.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Gtk", "4.0")
import numpy  # noqa: E402
from gi.repository import Gdk, GLib, Graphene, Gsk, Gtk  # noqa: E402

from .facemesh import Framing  # noqa: E402

# Canonical MediaPipe connectivity over all 468 surface landmarks. A
# triangulation fitted to one camera frame can omit the forehead or bridge
# eye/mouth openings; topology must stay anatomical across every pose.
TRIANGLES = Path(__file__).resolve().parent / "models" / "face_triangles.npy"
# Face Mesh's features, drawn as soft strokes of dust: (point indices along the
# stroke, share of the feature dust, scatter in face heights). The upper lids and the line of the mouth
# carry a face, not full outlines, which read as a cartoon.
FEATURES = (
    ((33, 246, 161, 160, 159, 158, 157, 173, 133), 0.12, 0.009),
    ((263, 466, 388, 387, 386, 385, 384, 398, 362), 0.12, 0.009),
    ((70, 63, 105, 66, 107), 0.08, 0.014),
    ((300, 293, 334, 296, 336), 0.08, 0.014),
    ((78, 191, 80, 81, 82, 13, 312, 311, 310, 415, 308), 0.06, 0.008),
    ((78, 95, 88, 178, 87, 14, 317, 402, 318, 324, 308), 0.06, 0.008),
    ((61, 185, 40, 39, 37, 0, 267, 269, 270, 409, 291), 0.04, 0.008),
    ((61, 146, 91, 181, 84, 17, 314, 405, 321, 375, 291), 0.05, 0.01),
)
# A glint of dust in each iris: its centre and the ring around it.
IRISES = ((468, 469, 470, 471, 472), (473, 474, 475, 476, 477))
# Motes per iris glint.
GLINT_MOTES = 5
NOSE_TIP = 1

# Roles: the arrow's dust, dust that only ever drifts around the face, and the
# face's dust, which is everything else.
ARROW_MOTES = 420
AMBIENT_MOTES = 240
# A small share supports the lids/lips; the rest follows the anatomical surface.
FEATURE_SHARE = 0.045
# A large soft key, a quiet opposing fill, and a grazing rim separate planes.
KEY_LIGHT = (-0.28, -0.35, 0.90)
FILL_LIGHT = (0.45, 0.05, 0.89)
RIM_DIRECTION = (0.85, -0.15, -0.50)
GRAIN = 0.72

# Condensing: a mote starts after its delay, earlier near the nose, and
# settles over `ARRIVE_SECONDS`, swirling up to `SWIRL` points off its path.
# Dust leaves faster than it comes.
ARRIVE_SECONDS = 0.7
SWIRL = 18.0
LEAVE_SPEED = 1.8
# Between poses the face's dust becomes a seal: a thick ring, a faint inner
# volume, then a check written in front. Shares of face motes; geometry in
# 208-tall preview points, y down, +z toward the camera. It arrives already
# tilted and keeps turning (fast at first, easing out) until the colour
# camera returns and the dust becomes the face.
MARK_SECONDS = 0.14
MARK_RING = 0.80
MARK_FILL = 0.03
MARK_RADIUS = 62.0
MARK_TUBE = 5.4
MARK_TILT_FROM = 0.46
MARK_TILT_AWAY = -0.26
MARK_TURN_SECONDS = 0.28
MARK_FOCAL = 340.0
# Yaw, tilt of the seal. Amount starts at FROM and eases toward AWAY, so
# the sign is the direction the head just turned: left yaws leftward, up
# nods upward (mirrored, as the user sees themself).
MARK_AXIS = {
    "left": (1.0, 0.0),
    "right": (-1.0, 0.0),
    "up": (0.0, -1.0),
    "down": (0.0, 1.0),
    "straight": (0.0, 1.0),
}
MARK_CHECK = ((-22.0, 7.0, 8.0), (-5.5, 20.0, 9.0), (30.0, -22.0, 10.0))
MARK_CHECK_SHORT = 0.32
# How bright the nebula is, and on the face how far a mote drifts, in points.
NEBULA_LIGHT = 0.45
DRIFT = 0.08

# The scene around the face, back to front, in preview points. A deep field of
# faint distant stars shifts `PARALLAX` points against the head's turn (the
# foreground bokeh the other way). Behind the head sits the source the dust
# is lit by: a small core, a corona that wraps the silhouette, and a faint
# wash, breathing every `BREATH_SECONDS`. Sizes are at the "close enough"
# fill and scale with the face. Haze motes orbit through it, brighter near
# the core, and a few large out-of-focus motes drift past the lens.
PARALLAX = 7.0
BREATH_SECONDS = 7.0
SOURCE = (
    (52.0, 58.0, 0.16),
    (168.0, 200.0, 0.09),
    (236.0, 272.0, 0.020),
)
HAZE_MOTES = 90
BOKEH = ((0.12, 0.2, 13.0), (0.86, 0.14, 9.0), (0.93, 0.78, 16.0), (0.06, 0.84, 11.0), (0.7, 0.95, 7.0))
BOKEH_ALPHA = 0.07

# The coaching guide, in preview points, drawn twice: a hairline of precise
# geometry that carries the reading, and dust that carries the life.
# A turn is an arc of `GUIDE_RADIUS` orbiting the head under the chin, from its
# tail behind the head (`GUIDE_TAIL` radians) to its head just past the front
# (`GUIDE_HEAD` radians), where the arc moves across the view rather than into
# it; a tilt is the same arc up the side of the head. Each is seen `GUIDE_VIEW`
# radians off its plane, with perspective from `GUIDE_FOCAL` away. A move is a
# straight arrow beside the face. Arrowheads are laid out flat on the screen,
# so they read at any angle. Closer and back use a head silhouette changing scale with radial chevrons.
GUIDE_TAIL = -1.55
GUIDE_HEAD = 0.5
# A tilt's arc starts this much nearer the front, so it stays within the frame's height.
GUIDE_TILT_REACH = 0.7
GUIDE_RADIUS = 104.0
GUIDE_VIEW = 0.42
GUIDE_FOCAL = 420.0
GUIDE_LENGTH = 62.0
GUIDE_ASIDE = 98.0
GUIDE_HEAD_WIDTH = 5.0
GUIDE_HEAD_LENGTH = 8.0
GUIDE_TAIL_SPREAD = 3.0
GUIDE_FLOW = 0.45
GUIDE_NUDGE_SECONDS = 1.6
GUIDE_FADE_SECONDS = 0.3
# Guide grains and hairlines take the guide colour this far toward white, so
# they stay luminous; their glow keeps the pure colour.
GUIDE_LIFT = 0.3
# A head silhouette moves through depth: larger means closer to the lens.
# It starts on the face itself (its outline this much outside the face) and
# grows or shrinks to a quiet destination silhouette with explicit radial
# chevrons, so the guide can never contradict the face's own size. Closer's
# destination is the face's size once close enough (the preview frames it so,
# see facemesh.Framing), so the face fills it as the instruction ends; back's
# is this step smaller. Without a face it takes a head's size where "closer"
# begins.
DISTANCE_RADIUS = (69.0, 81.0)
DISTANCE_PAD = 1.08
DISTANCE_NOMINAL = 0.7
DISTANCE_SECONDS = 1.8
DISTANCE_STEP = (0.85, 1.15)
ARROW_WING_SHARE = 0.16
ARROWS = {
    "turn-left", "turn-right", "tilt-up", "tilt-down",
    "move-left", "move-right", "move-up", "move-down", "closer", "back",
}

def rect(x: float, y: float, width: float, height: float) -> Graphene.Rect:
    return Graphene.Rect().init(x, y, width, height)


def star_spots(width: int, height: int, pitch: float, seed: int = 7) -> numpy.ndarray:
    """Each mote's place in a frame-lit face: a jittered hexagonal field over the frame."""
    row_height = pitch * math.sqrt(3) / 2
    rows = int((height - pitch) // row_height) + 1
    top = (height - (rows - 1) * row_height) / 2
    spots = []
    for row in range(rows):
        shift = pitch / 2 if row % 2 else 0.0
        columns = int((width - pitch - shift) // pitch) + 1
        left = (width - (columns - 1) * pitch) / 2
        spots.extend((left + column * pitch, top + row * row_height) for column in range(columns))
    random = numpy.random.default_rng(seed)
    spots = numpy.array(spots, numpy.float32)
    spots[:, 0] += random.uniform(-0.3, 0.3, len(spots)) * pitch
    spots[:, 1] += random.uniform(-0.3, 0.3, len(spots)) * row_height
    return spots


def feather(points: numpy.ndarray, width: float, height: float, radius: float, reach: float) -> numpy.ndarray:
    """How visible each point is inside a rounded rectangle whose last `reach` fades out."""
    qx = numpy.abs(points[:, 0] - width / 2) - (width / 2 - radius)
    qy = numpy.abs(points[:, 1] - height / 2) - (height / 2 - radius)
    edge = numpy.hypot(numpy.maximum(qx, 0), numpy.maximum(qy, 0)) + numpy.minimum(numpy.maximum(qx, qy), 0) - radius
    amount = numpy.clip(-edge / reach, 0, 1)
    return amount * amount * (3 - 2 * amount)


def drift_of(wander: numpy.ndarray, share: float) -> numpy.ndarray:
    return wander * share


def mix(first: float, second: float, amount: float) -> float:
    return first + (second - first) * amount


def gaussian_blur(image: numpy.ndarray, sigma: float) -> numpy.ndarray:
    """A separable Gaussian blur of a 2D float32 image, in pixels, edges dark."""
    if sigma < 0.3:
        return image
    reach = max(1, math.ceil(3 * sigma))
    kernel = numpy.exp(-numpy.arange(-reach, reach + 1, dtype=numpy.float32) ** 2 / (2 * sigma * sigma))
    kernel /= kernel.sum()
    height, width = image.shape
    padded = numpy.zeros((height, width + 2 * reach), numpy.float32)
    padded[:, reach:reach + width] = image
    rows = numpy.zeros_like(image)
    for offset, weight in enumerate(kernel):
        rows += weight * padded[:, offset:offset + width]
    padded = numpy.zeros((height + 2 * reach, width), numpy.float32)
    padded[reach:reach + height] = rows
    out = numpy.zeros_like(image)
    for offset, weight in enumerate(kernel):
        out += weight * padded[offset:offset + height]
    return out


def shoulder(linear: numpy.ndarray) -> numpy.ndarray:
    """Light to 8-bit alpha through a soft exposure shoulder, in place, float32."""
    numpy.negative(linear, out=linear)
    numpy.exp(linear, out=linear)
    numpy.multiply(linear, -255, out=linear)
    numpy.add(linear, 255.5, out=linear)
    return linear.astype(numpy.uint8)


def smoothstep(edge0: float, edge1: float, value: numpy.ndarray) -> numpy.ndarray:
    amount = numpy.clip((value - edge0) / (edge1 - edge0), 0, 1)
    return amount * amount * (3 - 2 * amount)


def soft_disc(size: int) -> Gdk.Texture:
    """A soft round falloff as an alpha texture, for glows and out-of-focus motes."""
    steps = (numpy.arange(size) + 0.5) / size * 2 - 1
    radius = numpy.hypot(*numpy.meshgrid(steps, steps))
    alpha = numpy.clip(1 - radius, 0, 1) ** 2 * (3 - 2 * numpy.clip(1 - radius, 0, 1))
    pixels = (alpha * 255).astype(numpy.uint8)
    return Gdk.MemoryTexture.new(size, size, Gdk.MemoryFormat.A8, GLib.Bytes.new(pixels.tobytes()), size)


def load_triangles() -> numpy.ndarray | None:
    try:
        triangles = numpy.load(TRIANGLES).astype(numpy.int64)
    except (OSError, ValueError):
        return None
    return triangles if triangles.ndim == 2 and triangles.shape[1] == 3 and triangles.max() < 468 else None


def load_surface() -> numpy.ndarray | None:
    """Canonical coordinates for stable 3D area sampling; optional with the mesh."""
    try:
        surface = numpy.load(TRIANGLES.with_name("face_surface.npy")).astype(numpy.float32)
    except (OSError, ValueError):
        return None
    return surface if surface.shape == (468, 3) and numpy.isfinite(surface).all() else None


@dataclass
class Look:
    """What changes from frame to frame: the dust's colours and the shake."""

    tint: tuple[float, float, float] = (1.0, 1.0, 1.0)
    glow: tuple[float, float, float] = (1.0, 1.0, 1.0)
    # The guide is an instruction, not part of the reflection, so it has a
    # colour of its own.
    guide: tuple[float, float, float] = (1.0, 1.0, 1.0)
    shake: float = 0.0


class Starlight:
    """Every mote's motion and light, recorded as render nodes each frame."""

    def __init__(self, width: int, height: int, style: dict[str, float]) -> None:
        self.width, self.height, self.style = width, height, style
        self.spots = star_spots(width, height, style["pitch"])
        count = len(self.spots)
        random = numpy.random.default_rng(11)
        self.random = random
        # Five taps across each spot soften the camera's grain.
        x = numpy.clip(self.spots[:, 0].astype(int), 0, width - 1)
        y = numpy.clip(self.spots[:, 1].astype(int), 0, height - 1)
        taps = [(0, 0), (-2, 0), (2, 0), (0, -2), (0, 2)]
        self.taps = numpy.stack([numpy.clip(y + dy, 0, height - 1) * width + numpy.clip(x + dx, 0, width - 1) for dx, dy in taps])
        self.weights = numpy.array([0.4, 0.15, 0.15, 0.15, 0.15], numpy.float32)[:, None]

        # How bright each mote can be: mostly faint grains, some stars, a few sparks.
        self.magnitude = (0.14 + 0.86 * random.uniform(0, 1, count) ** 3.4).astype(numpy.float32)
        self.spark = self.magnitude > 0.72
        # The nebula: each mote wanders around its own anchor, on two slow
        # waves per axis, so the whole cloud drifts without ever repeating.
        angle = random.uniform(0, math.tau, count)
        distance = numpy.sqrt(random.uniform(0, 1, count))
        self.anchor = numpy.stack([
            width / 2 + numpy.cos(angle) * distance * style["cloud_width"] / 2,
            height / 2 + numpy.sin(angle) * distance * style["cloud_height"] / 2,
        ], axis=1).astype(numpy.float32)
        self.wander_rate = random.uniform(0.08, 0.35, (count, 4)).astype(numpy.float32) * math.tau
        self.wander_phase = random.uniform(0, math.tau, (count, 4)).astype(numpy.float32)
        self.cloud_light = (random.uniform(0.2, 0.7, count) * self.magnitude ** 0.7).astype(numpy.float32)
        # Each mote's springiness varies a little, so they move as a swarm.
        self.stiffness = random.uniform(0.7, 1.3, count).astype(numpy.float32) * style["stiffness"]
        self.twinkle_rate = random.uniform(0.6, 1.8, count).astype(numpy.float32) * math.tau
        self.twinkle_phase = random.uniform(0, math.tau, count).astype(numpy.float32)
        self.glint_rate = random.uniform(0.15, 0.45, count).astype(numpy.float32) * math.tau
        self.threshold = random.uniform(0, 1, count).astype(numpy.float32)

        order = random.permutation(count)
        self.arrow_stars = order[count - min(ARROW_MOTES, count // 4):]
        ambient = order[count - len(self.arrow_stars) - min(AMBIENT_MOTES, count // 8):count - len(self.arrow_stars)]
        self.ambient_stars = ambient
        self.face_stars = order[:count - len(self.arrow_stars) - len(ambient)]
        # Ambient dust spreads over the whole preview, faint.
        self.anchor[ambient] = random.uniform((0, 0), (width, height), (len(ambient), 2))
        self.magnitude[ambient] *= 0.45
        self.spark[ambient] = False
        self._brighten()

        self.canonical = load_surface()
        self.triangles = load_triangles() if self.canonical is not None else None
        self.surface: dict[str, numpy.ndarray] | None = None
        self.swirl_side = numpy.where(random.uniform(0, 1, count) < 0.5, -1.0, 1.0).astype(numpy.float32)

        # The atmosphere: haze motes orbiting the head, and soft discs for the
        # back light and the bokeh.
        self.haze_angle = random.uniform(0, math.tau, HAZE_MOTES).astype(numpy.float32)
        self.haze_radius = (1.30 * numpy.sqrt(random.uniform(0, 1, HAZE_MOTES))).astype(numpy.float32)
        self.haze_speed = random.uniform(0.015, 0.05, HAZE_MOTES).astype(numpy.float32) * numpy.where(random.uniform(0, 1, HAZE_MOTES) < 0.5, -1, 1)
        self.haze_light = random.uniform(0.2, 0.6, HAZE_MOTES).astype(numpy.float32)
        self.disc = soft_disc(64)
        self.bokeh_drift = random.uniform(-1, 1, (len(BOKEH), 2)).astype(numpy.float32) * [2.5, 1.2]

        arrows = len(self.arrow_stars)
        self.arrow_wing_star = random.uniform(0, 1, arrows) < ARROW_WING_SHARE
        self.arrow_seed = random.uniform(0, 1, arrows).astype(numpy.float32)
        self.arrow_side = numpy.where(random.uniform(0, 1, arrows) < 0.5, -1.0, 1.0).astype(numpy.float32)
        self.arrow_scatter = random.normal(0, 0.45, (arrows, 2)).astype(numpy.float32)
        self.arrow_wobble = random.uniform(0, math.tau, arrows).astype(numpy.float32)
        faces = len(self.face_stars)
        self.mark_along = random.uniform(0, 1, faces).astype(numpy.float32)
        self.mark_width = random.normal(0, 1, faces).astype(numpy.float32)
        self.mark_phi = random.uniform(0, math.tau, faces).astype(numpy.float32)
        along = self.mark_along
        ring_t = numpy.clip(along / MARK_RING, 0, 1)
        fill_t = numpy.clip((along - MARK_RING) / MARK_FILL, 0, 1)
        check_t = numpy.clip((along - MARK_RING - MARK_FILL) / (1 - MARK_RING - MARK_FILL), 0, 1)
        self.mark_delay = numpy.where(
            along < MARK_RING,
            0.01 * ring_t,
            numpy.where(along < MARK_RING + MARK_FILL, 0.02 + 0.02 * fill_t, 0.04 + 0.06 * check_t),
        ).astype(numpy.float32)
        self.reset()

    def reset(self) -> None:
        """A new preview: the nebula appears at once and waits for a face."""
        count = len(self.spots)
        self.clock = 0.0
        self.position = self._cloud().copy()
        self.velocity = numpy.zeros((count, 2), numpy.float32)
        self.levels = numpy.zeros(count, numpy.float32)
        self.targets = numpy.zeros(count, numpy.float32)
        self.face = False
        # The newest face mesh's points (x, y, z), when the face is dust,
        # and where on it each face mote rests and how lit it is there.
        self.vertices: numpy.ndarray | None = None
        self.mesh_target: numpy.ndarray | None = None
        self.surface = None
        self.dust: tuple[numpy.ndarray, numpy.ndarray] | None = None
        # Seconds the face has been condensing, eased back down when it's lost.
        self.formed = 0.0
        # Between poses the dust writes a check instead of spreading to the cloud.
        self.mark = False
        self.marked = 0.0
        self.mark_spin = 0.0
        self.mark_axis = "straight"
        self.arrow = ""
        self.pending_arrow = ""
        self.arrow_target = 0.0
        self.arrow_amount = 0.0
        self.guide_lines: list[tuple[numpy.ndarray, numpy.ndarray]] = []
        # How the head is turned (-1..1 each way), eased, for parallax; and how
        # formed the scene around the face is.
        self.parallax = numpy.zeros(2, numpy.float32)
        self.parallax_target = numpy.zeros(2, numpy.float32)
        self.scene = 0.0
        # 0..1 how gathered into a frame-lit face the motes are, and how alive the field is.
        self.gathered = 0.0
        self.presence = 0.0
        self.presence_target = 1.0
        self.light = numpy.zeros(count, numpy.float32)

    # Input ----------------------------------------------------------------------

    def brightness(self, pixels: bytes) -> numpy.ndarray:
        """Each spot's brightness: the frame's own range, a noise floor, a curve."""
        if len(pixels) != self.width * self.height:
            raise ValueError("frame size does not match its dimensions")
        frame = numpy.frombuffer(pixels, numpy.uint8).astype(numpy.float32)
        values = (frame[self.taps] * self.weights).sum(axis=0)
        low, high = numpy.percentile(values, (self.style["low_share"] * 100, self.style["high_share"] * 100))
        stretched = numpy.clip((values - low) / max(high - low, 5.0), 0, 1)
        return numpy.clip((stretched - self.style["floor"]) / (1 - self.style["floor"]), 0, 1) ** self.style["gamma"]

    def submit(self, pixels: bytes, face: bool = True) -> None:
        """A camera frame, and whether a face was found in it."""
        self.targets = self.brightness(pixels).astype(numpy.float32)
        self.face = face
        self.vertices = None

    def submit_mesh(self, points: numpy.ndarray, mark: bool = False, axis: str = "") -> None:
        """The face mesh's points as (x, y, z) in preview pixels rows; none means no face.

        ``mark`` is an IR burst between poses: the face's dust writes a check
        instead of spreading into the cloud. ``axis`` is the pose just captured
        (left, right, up, down, straight), so the seal turns the same way.
        """
        if len(points) == 0:
            self.face = False
            if mark and not self.mark:
                self.mark_spin = 0.0
                self.mark_axis = axis if axis in MARK_AXIS else "straight"
            self.mark = bool(mark)
            if self.mark:
                self.set_arrow("")
            return
        if self.triangles is None or len(points) < 468:
            return
        points = numpy.asarray(points, numpy.float32)
        if self.surface is None:
            self._place_dust(points)
        self.mesh_target = points.copy()
        if self.vertices is None:
            self.vertices = points.copy()
            self.dust = self._face_dust()
        # The display clock advances one common 3D pose for both position and
        # lighting; neither channel independently chases the next camera pose.
        low, high = points[:468, :2].min(axis=0), points[:468, :2].max(axis=0)
        size = numpy.maximum(high - low, 1.0)
        self.parallax_target = numpy.clip((points[NOSE_TIP, :2] - (low + high) / 2) / size * 5, -1, 1).astype(numpy.float32)
        self.face = True
        self.mark = False

    def set_arrow(self, name: str) -> None:
        """Show the coaching arrow ("turn-left", "tilt-up", "move-right", …), or none."""
        self.pending_arrow = name if name in ARROWS else ""
        if self.pending_arrow and (not self.arrow or self.arrow == self.pending_arrow):
            self.arrow = self.pending_arrow
            self.arrow_target = 1.0
        else:
            # Let the old geometry dissolve before the new direction gathers.
            self.arrow_target = 0.0

    def fade_out(self) -> None:
        """The scan is over: the dust winks out."""
        self.presence_target = 0.0
        self.face = False
        self.mark = False
        self.set_arrow("")

    # The face's dust --------------------------------------------------------------

    def _brighten(self) -> None:
        """How much of each mote's light glows: none for faint grains, most for sparks."""
        self.glow_share = (numpy.clip((self.magnitude - 0.25) / 0.6, 0, 1) ** 1.5 * 1.6).astype(numpy.float32)

    def _place_dust(self, points: numpy.ndarray) -> None:
        """Give each face mote its own place on the surface, once, from the first face."""
        random = self.random
        stars = self.face_stars
        count = len(stars)
        features = int(count * FEATURE_SHARE)
        xy = points[:, :2]

        surface = count - features
        # Sample the canonical 3D surface, not a flattened first camera frame.
        # Stratified area allocation keeps dust out of random clumps and avoids
        # starving a cheek when the first frame happens to be a turned head.
        canonical = self.canonical
        solid = canonical[self.triangles]
        areas = numpy.linalg.norm(numpy.cross(solid[:, 1] - solid[:, 0], solid[:, 2] - solid[:, 0]), axis=1)
        cdf = numpy.cumsum(areas) / areas.sum()
        sample = (numpy.arange(surface) + random.uniform(0, 1, surface)) / surface
        triangle = numpy.searchsorted(cdf, sample)
        first = (numpy.arange(surface) * 0.754877666 + random.uniform()) % 1
        second = (numpy.arange(surface) * 0.569840291 + random.uniform()) % 1
        root = numpy.sqrt(first)
        surface_corners = self.triangles[triangle]
        surface_weights = numpy.stack([1 - root, root * (1 - second), root * second], axis=1)

        # Strokes: a point along one segment of a feature, by the feature's share
        # and each segment's length within it.
        segments, chances, spreads = [], [], []
        for line, share, spread in FEATURES:
            pairs = [(line[i], line[i + 1]) for i in range(len(line) - 1)]
            lengths = numpy.linalg.norm(xy[[b for _, b in pairs]] - xy[[a for a, _ in pairs]], axis=1) + 1e-6
            segments.extend(pairs)
            chances.extend(share * lengths / lengths.sum())
            spreads.extend([spread] * len(pairs))
        segments, chances, spreads = numpy.array(segments), numpy.array(chances), numpy.array(spreads)
        # Glints: a point in a small disc around each iris centre.
        glints = [(iris[0], iris[1 + i], iris[1 + (i + 1) % 4]) for iris in IRISES for i in range(4)]
        glint_count = GLINT_MOTES * len(IRISES)
        stroke_count = features - glint_count
        segment = random.choice(len(segments), stroke_count, p=chances / chances.sum())
        along = random.uniform(0, 1, stroke_count)
        glint = random.choice(len(glints), glint_count)
        g1, g2 = random.uniform(0, 1, (2, glint_count))
        g_flip = g1 + g2 > 1
        g1[g_flip], g2[g_flip] = 1 - g1[g_flip], 1 - g2[g_flip]
        g1, g2 = g1 * 0.55, g2 * 0.55  # the inner part of the iris
        feature_corners = numpy.concatenate([
            numpy.concatenate([segments[segment], segments[segment][:, :1]], axis=1),
            numpy.array(glints)[glint],
        ])
        feature_weights = numpy.concatenate([
            numpy.stack([1 - along, along, numpy.zeros(stroke_count)], axis=1),
            numpy.stack([1 - g1 - g2, g1, g2], axis=1),
        ])
        corners_of = numpy.concatenate([surface_corners, feature_corners]).astype(numpy.int64)
        weights = numpy.concatenate([surface_weights, feature_weights]).astype(numpy.float32)
        feature = numpy.concatenate([numpy.zeros(count - features, bool), numpy.ones(features, bool)])
        glinting = numpy.concatenate([numpy.zeros(count - glint_count, bool), numpy.ones(glint_count, bool)])
        # Feature dust scatters a little off its stroke, in face heights; glints stay put.
        spread = numpy.concatenate([numpy.zeros(surface), spreads[segment], numpy.full(glint_count, 0.002)])
        scatter = (random.normal(0, 1, (count, 2)) * spread[:, None]).astype(numpy.float32)
        rest = (weights[:, :, None] * xy[corners_of]).sum(axis=1)
        span = float(numpy.ptp(xy[:, 1])) or 1.0
        reach = numpy.linalg.norm(rest - xy[NOSE_TIP], axis=1) / span
        delay = 0.05 + 0.45 * numpy.clip(reach / 0.6, 0, 1) + random.uniform(0, 0.25, count)
        magnitude = self.magnitude[stars]
        self.magnitude[stars[feature]] = numpy.maximum(magnitude[feature], 0.3)
        # An iris glint is a catchlight: its dust is bright and sparks.
        self.magnitude[stars[glinting]] = numpy.maximum(magnitude[glinting], 0.8)
        self.spark[stars[glinting]] = random.uniform(0, 1, glint_count) < 0.5
        self._brighten()
        self.surface = {
            "corners": corners_of,
            "weights": weights,
            "blend": (
                tuple(numpy.ascontiguousarray(corners_of[:, corner]) for corner in range(3)),
                tuple(numpy.ascontiguousarray(weights[:, corner:corner + 1]) for corner in range(3)),
            ),
            "feature": feature,
            "scatter": scatter,
            "delay": delay.astype(numpy.float32),
        }

    def _normals(self, points: numpy.ndarray) -> numpy.ndarray:
        """Outward surface normals in screen XYZ, including signed back faces."""
        corners = points[self.triangles]
        faces = numpy.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
        # Canonical winding is clockwise after converting y to screen-down.
        # Keep signed normals: flipping every triangle toward the viewer lit
        # the hidden cheek and flattened the volume at a turn.
        faces *= -1
        normals = numpy.stack([
            sum(numpy.bincount(self.triangles[:, corner], faces[:, axis], minlength=len(points)) for corner in range(3))
            for axis in range(3)
        ], axis=1)
        # Iris landmarks are outside the 468-vertex surface topology. Borrow
        # the eye socket's orientation so catchlight motes have a valid normal
        # instead of silently disappearing with a zero-length vector.
        if len(points) >= 478:
            for iris, socket in zip(IRISES, ((33, 133, 159, 145), (263, 362, 386, 374))):
                normals[list(iris)] = normals[list(socket)].mean(axis=0)
        length = numpy.linalg.norm(normals, axis=1, keepdims=True)
        return (normals / numpy.maximum(length, 1e-6)).astype(numpy.float32)

    def _blend(self, values: numpy.ndarray) -> numpy.ndarray:
        """Each face mote's barycentric mix of per-vertex ``values``.

        Three contiguous gathers, several times faster than one (n, 3, k)
        gather and sum, every frame.
        """
        (c0, c1, c2), (w0, w1, w2) = self.surface["blend"]
        return w0 * values[c0] + w1 * values[c1] + w2 * values[c2]

    def _face_dust(self) -> tuple[numpy.ndarray, numpy.ndarray]:
        """Where each face mote rests on the newest face, and how lit it is there."""
        surface, points = self.surface, self.vertices
        span = float(numpy.ptp(points[:, 1])) or 1.0
        rest = self._blend(points[:, :2]) + surface["scatter"] * span
        normal = self._blend(self._normals(points))
        normal /= numpy.maximum(numpy.linalg.norm(normal, axis=1, keepdims=True), 1e-6)
        def direction(value):
            vector = numpy.asarray(value, numpy.float32)
            return vector / numpy.linalg.norm(vector)

        key_direction = direction(KEY_LIGHT)
        key = numpy.maximum(normal @ key_direction, 0)
        fill = numpy.maximum(normal @ direction(FILL_LIGHT), 0)
        rim = numpy.maximum(normal @ direction(RIM_DIRECTION), 0) ** 1.5
        # Light wrapping from the source behind the head, strongest where
        # the dust is seen edge-on, so the silhouette catches it.
        wrap = (1 - numpy.clip(normal[:, 2], 0, 1)) ** 2
        # Broad diffuse planes plus a restrained view-dependent highlight.
        # The specular lobe moves across the cheeks/nose as the head turns.
        half_vector = direction(key_direction + [0, 0, 1])
        specular = numpy.maximum(normal @ half_vector, 0) ** 12 * key
        shade = 0.13 + 0.64 * key + 0.22 * fill + 0.15 * rim + 0.14 * wrap
        feature = surface["feature"]
        shade = numpy.where(feature, shade * 1.12, shade)
        visibility = smoothstep(0.02, 0.20, normal[:, 2])
        # Every surface grain remains present. Lighting controls radiance,
        # not occupancy: changing density with normals made moving shadows
        # look like holes in the user's face.
        magnitude = self.magnitude[self.face_stars]
        radiance = (shade ** 1.15 + 0.12 * specular) * GRAIN * (0.10 + 0.90 * magnitude)
        return rest.astype(numpy.float32), (radiance * visibility).astype(numpy.float32)

    # The arrow's dust ---------------------------------------------------------------

    def _project(self, kind: str, solid: numpy.ndarray) -> tuple[numpy.ndarray, numpy.ndarray]:
        """A guide's 3D points on the screen, and the perspective scale at each."""
        x, y, z = solid[:, 0], solid[:, 1], solid[:, 2]
        view = GUIDE_VIEW
        if kind == "turn":
            # Orbiting the head's vertical axis, seen from above: the front of
            # the orbit passes under the chin.
            y, z = y * math.cos(view) + z * math.sin(view), z * math.cos(view) - y * math.sin(view)
            y = y + 30
        elif kind == "tilt":
            # Orbiting the ear-to-ear axis, seen from the side: the front of
            # the orbit passes beside the cheek.
            x, z = x * math.cos(view) + z * math.sin(view), z * math.cos(view) - x * math.sin(view)
            x = x + 26
        scale = GUIDE_FOCAL / numpy.maximum(GUIDE_FOCAL - z, 60.0)
        points = numpy.stack([self.width / 2 + x * scale, self.height / 2 + y * scale], axis=1)
        return points.astype(numpy.float32), scale.astype(numpy.float32)

    def arrow_shape(self, name: str, clock: float) -> tuple[numpy.ndarray, numpy.ndarray]:
        """Where each guide mote is at `clock`, in preview points, and its light.

        Also leaves the guide's hairlines in `guide_lines`: (points, alphas) polylines.
        """
        seed, side, scatter = self.arrow_seed, self.arrow_side, self.arrow_scatter
        phase = (clock / GUIDE_NUDGE_SECONDS) % 1.0
        # A nudge its way: quick out, slow back.
        nudge = math.sin(math.pi * phase ** 0.6) ** 2
        if name in ("closer", "back"):
            return self._distance_guide(name, clock)
        kind, direction = name.split("-")
        forward = 1.0 if direction in ("right", "down") else -1.0

        def spine(along: numpy.ndarray) -> numpy.ndarray:
            """The guide's centre line at 0..1 from tail to tip, in 3D."""
            zero = numpy.zeros_like(along)
            if kind == "move":
                offset = ((along - 0.5) * GUIDE_LENGTH + nudge * 6) * forward
                if direction in ("left", "right"):
                    return numpy.stack([forward * GUIDE_ASIDE + offset, zero, zero], axis=1)
                return numpy.stack([zero + GUIDE_ASIDE, offset, zero], axis=1)
            reach = GUIDE_TILT_REACH if kind == "tilt" else 1.0
            angle = forward * (GUIDE_TAIL * reach + (GUIDE_HEAD - GUIDE_TAIL * reach) * along + nudge * 0.09)
            sweep, depth = GUIDE_RADIUS * numpy.sin(angle), GUIDE_RADIUS * numpy.cos(angle)
            if kind == "turn":
                return numpy.stack([sweep, zero, depth], axis=1)
            return numpy.stack([zero, sweep, depth], axis=1)

        project = lambda solid: self._project(kind, solid)  # noqa: E731
        # The head, flat on the screen along the arc's last stretch.
        ends, _ = project(spine(numpy.array([0.97, 1.0])))
        tip = ends[1]
        heading = ends[1] - ends[0]
        heading /= max(float(numpy.linalg.norm(heading)), 1e-6)
        normal = numpy.array([-heading[1], heading[0]])
        shaft_end = 0.97

        # The stream: faint and loose at the tail, drawn tight toward the head.
        flow = (seed + clock * GUIDE_FLOW) % 1.0
        loose = GUIDE_TAIL_SPREAD * (1 - flow) ** 1.6 + 0.7
        solid = spine(flow * shaft_end)
        wobble = numpy.sin(clock * 1.9 + self.arrow_wobble) * (1 - flow)
        across_axis = 1 if kind in ("turn",) or (kind == "move" and direction in ("left", "right")) else 0
        solid[:, across_axis] += scatter[:, 0] * loose + wobble
        solid[:, 2] += scatter[:, 1] * loose
        stream, depth_scale = project(solid)
        stream_light = (0.08 + 0.92 * flow ** 1.5) * smoothstep(0.0, 0.12, flow) * (1 - smoothstep(0.85, 1.0, flow))
        stream_light = stream_light * numpy.clip(depth_scale, 0.55, 1.3) ** 2

        # The chevron: dust runs back from the tip along both wings.
        run = (seed + clock * GUIDE_FLOW * 0.7) % 1.0
        wings = (
            tip
            - heading * (run * GUIDE_HEAD_LENGTH)[:, None]
            + normal * (side * run * GUIDE_HEAD_WIDTH)[:, None]
            + scatter * (0.35 + 0.9 * run)[:, None]
        )
        wing_light = 0.45 * smoothstep(0.0, 0.08, run + 0.04) * (1 - smoothstep(0.8, 1.0, run))

        wing = self.arrow_wing_star
        points = numpy.where(wing[:, None], wings, stream).astype(numpy.float32)
        light = numpy.where(wing, wing_light, stream_light) * (0.35 + 0.9 * self.magnitude[self.arrow_stars])

        # Hairlines: the path, fading in from its tail, and the chevron.
        along = numpy.linspace(0, 1.0, 48)
        path, path_scale = project(spine(along))
        path_alpha = 0.34 * smoothstep(0.1, 0.75, along) * numpy.clip(path_scale, 0.5, 1.2) ** 2
        left = tip - heading * GUIDE_HEAD_LENGTH + normal * GUIDE_HEAD_WIDTH
        right = tip - heading * GUIDE_HEAD_LENGTH - normal * GUIDE_HEAD_WIDTH
        chevron = numpy.array([left, tip, right], numpy.float32)
        self.guide_lines = [(path, path_alpha), (chevron, numpy.full(3, 0.72, numpy.float32))]
        return points, numpy.clip(light, 0, 1.3).astype(numpy.float32)

    def _distance_guide(self, name: str, clock: float) -> tuple[numpy.ndarray, numpy.ndarray]:
        """A head contour approaches the viewer for closer, recedes for back."""
        cycle = (clock / DISTANCE_SECONDS) % 1.0
        progress = 1 - (1 - cycle) ** 3
        closer = name == "closer"
        centre = numpy.array([self.width / 2, self.height / 2], numpy.float32)
        size = DISTANCE_NOMINAL
        if self.vertices is not None:
            low, high = self.vertices[:468, :2].min(axis=0), self.vertices[:468, :2].max(axis=0)
            centre = ((low + high) / 2).astype(numpy.float32)
            size = float(high[1] - low[1]) * DISTANCE_PAD / (2 * DISTANCE_RADIUS[1])
        enough = self.height / Framing.ZOOM * DISTANCE_PAD / (2 * DISTANCE_RADIUS[1])
        step = size * DISTANCE_STEP[1 if closer else 0]
        # Too close spills past the preview, so back can only show a step.
        destination = max(enough, step) if closer else step
        # The destination stays within the preview.
        room = (min(float(centre[1]), self.height - float(centre[1])) - 1) / DISTANCE_RADIUS[1]
        destination = min(destination, max(room, 0.1))
        scale = size + (destination - size) * progress
        alpha = float(smoothstep(0.0, 0.12, cycle) * (1 - smoothstep(0.72, 1.0, cycle)))

        def contour(angle: numpy.ndarray, size: float) -> numpy.ndarray:
            # Broad forehead, narrower jaw, rounded chin: a head, not a ring.
            width = DISTANCE_RADIUS[0] * (0.88 + 0.12 * numpy.cos(angle))
            return centre + size * numpy.stack([
                width * numpy.sin(angle), -DISTANCE_RADIUS[1] * numpy.cos(angle),
            ], axis=1)

        angles = numpy.linspace(0, math.tau, 97)
        target = contour(angles, destination)
        moving = contour(angles, scale)
        self.guide_lines = [
            (target, numpy.full(len(target), 0.11, numpy.float32)),
            (moving, numpy.full(len(moving), 0.36 * alpha, numpy.float32)),
        ]
        # Outward/inward chevrons make direction legible even in a still frame.
        direction = 1 if closer else -1
        tips = []
        for side in (-1, 1):
            tip = centre + [side * (DISTANCE_RADIUS[0] * scale + 13), 0]
            heading = numpy.array([side * direction, 0], numpy.float32)
            wings = numpy.array([tip - heading * 6 + [0, -4], tip, tip - heading * 6 + [0, 4]], numpy.float32)
            self.guide_lines.append((wings, numpy.full(3, 0.8 * alpha, numpy.float32)))
            tips.append((tip, heading))

        # Dust runs along the changing silhouette; some collects on each arrow.
        angle = math.tau * ((self.arrow_seed + clock * 0.07) % 1)
        points = contour(angle, scale) + self.arrow_scatter * 0.5
        light = alpha * (0.18 + 0.35 * self.magnitude[self.arrow_stars])
        wing = self.arrow_wing_star
        for side, (tip, heading) in zip((-1, 1), tips):
            selected = wing & (self.arrow_side == side)
            run = (self.arrow_seed[selected] * 7) % 1
            wing_side = numpy.where(self.arrow_scatter[selected, 0] < 0, -1, 1)
            points[selected] = tip - heading * (run * 6)[:, None] + numpy.stack([numpy.zeros(len(run)), wing_side * run * 4], axis=1)
            light[selected] = alpha * 0.55
        return points.astype(numpy.float32), light.astype(numpy.float32)

    # Motion ---------------------------------------------------------------------

    def _cloud(self) -> numpy.ndarray:
        wave = numpy.sin(self.wander_rate * self.clock + self.wander_phase)
        drift = numpy.stack([wave[:, 0] + 0.6 * wave[:, 1], wave[:, 2] + 0.6 * wave[:, 3]], axis=1)
        return self.anchor + drift * self.style["wander"]

    def advance(self, elapsed: float) -> None:
        style = self.style
        elapsed = min(elapsed, 1 / 20)
        self.clock += elapsed
        seconds = style["appear_seconds"] if self.presence_target > self.presence else style["vanish_seconds"]
        change = elapsed / seconds
        self.presence = min(self.presence_target, self.presence + change) if self.presence_target > self.presence else max(self.presence_target, self.presence - change)

        self.parallax += (self.parallax_target * (1.0 if self.face else 0.0) - self.parallax) * (1 - math.exp(-elapsed / 0.4))
        self.scene += ((1.0 if (self.face or self.mark or self.marked > 0.02) else 0.0) - self.scene) * (1 - math.exp(-elapsed / 0.8))
        cloud = self._cloud()
        twinkle = 1 - style["twinkle"] * (0.5 + 0.5 * numpy.sin(self.twinkle_rate * self.clock + self.twinkle_phase))
        if self.vertices is not None and self.surface is not None:
            if self.mesh_target is not None and self.face:
                delta = self.mesh_target - self.vertices
                span = max(float(numpy.ptp(self.vertices[:468, 1])), 1.0)
                motion = float(numpy.median(numpy.linalg.norm(delta[:468], axis=1))) / span
                # Smooth small estimation noise, follow a deliberate turn
                # promptly. One coefficient for XYZ avoids shearing the head.
                response = 0.14 - 0.09 * float(smoothstep(0.006, 0.035, motion))
                self.vertices += delta * (1 - math.exp(-elapsed / response))
                self.dust = self._face_dust()
            target, light = self._advance_dust(elapsed, cloud)
            # Scintillation belongs to the free dust. Once on the face, grains
            # carry stable light so eyes/cheeks do not flicker independently.
            face_light = light[self.face_stars].copy()
            glint = numpy.maximum(numpy.sin(self.glint_rate * self.clock + self.twinkle_phase), 0) ** 16
            light = light * (1 - (1 - twinkle) * (1.2 - self.magnitude)) + glint * self.spark * numpy.minimum(light * 2.0, 0.38) * min(1.0, self.formed)
            light[self.face_stars] = face_light
        else:
            # A frame-lit face: light follows the camera closely; gathering eases.
            self.levels += (self.targets - self.levels) * (1 - math.exp(-elapsed / style["follow_seconds"]))
            goal = 1.0 if self.face else 0.0
            self.gathered += (goal - self.gathered) * (1 - math.exp(-elapsed / style["gather_seconds"]))
            target = self.spots if self.face else cloud
            # Cloud light gives way quickly as the motes gather, so those bound
            # for the dark around a face fade on the way.
            light = (NEBULA_LIGHT * self.cloud_light * (1 - self.gathered) ** 3 + numpy.sqrt(self.levels) * self.gathered) * twinkle

        if self.arrow_target == 0.0 and self.arrow_amount < 0.08:
            self.arrow = self.pending_arrow
            self.arrow_target = 1.0 if self.arrow else 0.0
        self.arrow_amount += (self.arrow_target - self.arrow_amount) * (1 - math.exp(-elapsed / GUIDE_FADE_SECONDS))
        stars = self.arrow_stars
        arrow = self.arrow_amount > 0.01 and self.arrow in ARROWS
        if arrow:
            shape, arrow_light = self.arrow_shape(self.arrow, self.clock)
            target[stars] = shape
            light[stars] = light[stars] * (1 - self.arrow_amount) + arrow_light * self.arrow_amount

        stiffness = self.stiffness[:, None]
        damping = 2 * 0.82 * numpy.sqrt(stiffness)
        self.velocity += ((target - self.position) * stiffness - self.velocity * damping) * elapsed
        self.position += self.velocity * elapsed
        if self.vertices is not None and self.surface is not None:
            arrival = numpy.clip((self.formed - self.surface["delay"]) / ARRIVE_SECONDS, 0, 1)
            mark_arrival = numpy.clip((self.marked - self.mark_delay) / MARK_SECONDS, 0, 1)
            attached = numpy.maximum(
                smoothstep(0.65, 1.0, arrival) * (1 - mark_arrival),
                smoothstep(0.65, 1.0, mark_arrival),
            )[:, None]
            stars = self.face_stars
            self.position[stars] += (target[stars] - self.position[stars]) * attached
            self.velocity[stars] *= 1 - attached
        if arrow:
            stars = self.arrow_stars
            # Arrow dust follows its stream closely rather than on its spring,
            # which would smear it, once it has flown in.
            follow = 1 - math.exp(-elapsed * 30 * self.arrow_amount ** 2)
            self.position[stars] += (shape - self.position[stars]) * follow
            self.velocity[stars] *= 1 - follow

        # A mote is alive once the field's presence passes its threshold.
        alive = numpy.clip((self.presence * 1.25 - self.threshold) / 0.25, 0, 1)
        alive = alive * alive * (3 - 2 * alive)
        self.light = light * alive * feather(self.position, self.width, self.height, style["corner"], style["feather"])

    def _mark_basis(self) -> tuple[float, float]:
        """Yaw and tilt along the pose just captured: starts fast and eases out."""
        yaw_axis, tilt_axis = MARK_AXIS.get(self.mark_axis, MARK_AXIS["straight"])
        amount = MARK_TILT_AWAY + (MARK_TILT_FROM - MARK_TILT_AWAY) * math.exp(-self.mark_spin / MARK_TURN_SECONDS)
        return amount * yaw_axis, amount * tilt_axis

    def _mark_project(self, local: numpy.ndarray, tilt: float | None = None, yaw: float | None = None) -> tuple[numpy.ndarray, numpy.ndarray, numpy.ndarray]:
        """Project local 208-pt XYZ (y down, +z toward camera) into preview points."""
        base_yaw, base_tilt = self._mark_basis()
        yaw = base_yaw if yaw is None else yaw
        tilt = base_tilt if tilt is None else tilt
        cy, sy = math.cos(yaw), math.sin(yaw)
        ct, st = math.cos(tilt), math.sin(tilt)
        x, y, z = local[:, 0], local[:, 1], local[:, 2]
        x1 = x * cy + z * sy
        z1 = -x * sy + z * cy
        y1 = y * ct - z1 * st
        z2 = y * st + z1 * ct
        persp = MARK_FOCAL / numpy.maximum(MARK_FOCAL - z2, 80.0)
        scale = (self.height / 208.0) * persp
        centre = numpy.array([self.width / 2, self.height / 2 + 2], numpy.float32)
        xy = centre + numpy.stack([x1 * scale, y1 * scale], axis=1)
        return xy.astype(numpy.float32), z2.astype(numpy.float32), persp.astype(numpy.float32)

    def _mark_points(self) -> tuple[numpy.ndarray, numpy.ndarray, numpy.ndarray]:
        """The check's short start, corner, and long end, in preview points."""
        local = numpy.array(MARK_CHECK, numpy.float32)
        xy, _, _ = self._mark_project(local)
        xy = xy + (self._mark_ring_line().mean(axis=0) - xy.mean(axis=0))
        return xy[0], xy[1], xy[2]

    def _mark_ring_line(self, steps: int = 64) -> numpy.ndarray:
        """The ring's centreline as a closed polyline in preview points."""
        theta = numpy.linspace(0, math.tau, steps, dtype=numpy.float32)
        local = numpy.stack([MARK_RADIUS * numpy.cos(theta), MARK_RADIUS * numpy.sin(theta), numpy.zeros(steps, numpy.float32)], axis=1)
        xy, _, _ = self._mark_project(local)
        return xy

    def _mark_dust(self) -> tuple[numpy.ndarray, numpy.ndarray]:
        """Where each face mote rests on the seal, and how lit it is there."""
        along = self.mark_along
        width = self.mark_width
        phi = self.mark_phi
        on_ring = along < MARK_RING
        on_fill = (along >= MARK_RING) & (along < MARK_RING + MARK_FILL)
        ring_t = numpy.clip(along / MARK_RING, 0, 1)
        fill_t = numpy.clip((along - MARK_RING) / MARK_FILL, 0, 1)
        check_t = numpy.clip((along - MARK_RING - MARK_FILL) / (1 - MARK_RING - MARK_FILL), 0, 1)
        theta = ring_t * math.tau
        radial = MARK_RADIUS + MARK_TUBE * numpy.cos(phi)
        ring = numpy.stack([radial * numpy.cos(theta), radial * numpy.sin(theta), MARK_TUBE * numpy.sin(phi)], axis=1)
        fill_r = MARK_RADIUS * 0.62 * numpy.sqrt(numpy.clip(fill_t, 0, 1))
        fill_a = fill_t * math.tau * 7.13
        fill = numpy.stack([fill_r * numpy.cos(fill_a), fill_r * numpy.sin(fill_a), -3.0 + 2.0 * width], axis=1)
        short = MARK_CHECK_SHORT
        on_short = check_t < short
        u = numpy.where(on_short, check_t / short, (check_t - short) / (1 - short))
        a = numpy.array(MARK_CHECK[0], numpy.float32)
        b = numpy.array(MARK_CHECK[1], numpy.float32)
        c = numpy.array(MARK_CHECK[2], numpy.float32)
        start = numpy.where(on_short[:, None], a, b)
        end = numpy.where(on_short[:, None], b, c)
        tangent = end - start
        tangent /= numpy.maximum(numpy.linalg.norm(tangent, axis=1, keepdims=True), 1e-3)
        # Screen-plane normal for stroke thickness, plus a little depth.
        normal = numpy.stack([-tangent[:, 1], tangent[:, 0], numpy.zeros(len(tangent))], axis=1)
        check = start + (end - start) * u[:, None] + normal * (numpy.clip(width, -2.4, 2.4) * 3.8)[:, None]
        check[:, 2] += 8.0 + 0.6 * width
        ring_xy, ring_depth, ring_persp = self._mark_project(ring)
        fill_xy, fill_depth, fill_persp = self._mark_project(fill)
        check_xy, check_depth, check_persp = self._mark_project(check)
        on_check = ~on_ring & ~on_fill
        offset = ring_xy[on_ring].mean(axis=0) - check_xy[on_check].mean(axis=0)
        check_xy = check_xy + offset
        xy = numpy.where(on_ring[:, None], ring_xy, numpy.where(on_fill[:, None], fill_xy, check_xy))
        depth = numpy.where(on_ring, ring_depth, numpy.where(on_fill, fill_depth, check_depth))
        persp = numpy.where(on_ring, ring_persp, numpy.where(on_fill, fill_persp, check_persp))
        # Torus normals, rotated with the seal, lit by the same key as the face.
        nx = numpy.cos(phi) * numpy.cos(theta)
        ny = numpy.cos(phi) * numpy.sin(theta)
        nz = numpy.sin(phi)
        yaw, tilt = self._mark_basis()
        cy, sy = math.cos(yaw), math.sin(yaw)
        ct, st = math.cos(tilt), math.sin(tilt)
        nx1 = nx * cy + nz * sy
        nz1 = -nx * sy + nz * cy
        ny1 = ny * ct - nz1 * st
        nz2 = ny * st + nz1 * ct
        key = numpy.asarray(KEY_LIGHT, numpy.float32)
        key = key / numpy.linalg.norm(key)
        shade = 0.10 + 0.72 * numpy.maximum(nx1 * key[0] + ny1 * key[1] + nz2 * key[2], 0) + 0.38 * numpy.maximum(nz2, 0)
        facing = smoothstep(-6.0, 28.0, depth)
        mag = self.magnitude[self.face_stars]
        ring_light = (0.32 + 0.85 * mag) * shade * (0.38 + 0.62 * facing) * (0.85 + 0.25 * persp)
        fill_light = (0.05 + 0.08 * mag) * (0.55 + 0.45 * facing)
        check_light = (0.22 + 0.38 * mag) * (0.70 + 0.20 * check_t)
        light = numpy.where(on_ring, ring_light, numpy.where(on_fill, fill_light, check_light))
        return xy.astype(numpy.float32), numpy.clip(light, 0, 1.45).astype(numpy.float32)

    def _advance_dust(self, elapsed: float, cloud: numpy.ndarray) -> tuple[numpy.ndarray, numpy.ndarray]:
        """Targets and light while the face is dust: condensing, resting, or spreading."""
        if self.face:
            self.formed += elapsed
            self.marked = max(0.0, self.marked - elapsed * LEAVE_SPEED)
        elif self.mark:
            self.formed = max(0.0, self.formed - elapsed * LEAVE_SPEED * 1.7)
            self.marked += elapsed
        else:
            self.formed = max(0.0, self.formed - elapsed * LEAVE_SPEED)
            self.marked = max(0.0, self.marked - elapsed * LEAVE_SPEED)
        if self.mark or self.marked > 0.0:
            self.mark_spin += elapsed
        # Saturate at the final mote's arrival, so losing a long-held face
        # starts releasing dust immediately instead of waiting seconds.
        self.formed = min(self.formed, float(self.surface["delay"].max()) + ARRIVE_SECONDS)
        self.marked = min(self.marked, MARK_SECONDS + 0.20)
        # Once spread back out, the next face condenses afresh. A check keeps
        # the face's rest so the dust can gather there again.
        if not self.face and not self.mark and self.formed <= 0.0 and self.marked <= 0.0:
            self.vertices = None

        target = cloud.copy()
        light = self.cloud_light * NEBULA_LIGHT
        stars = self.face_stars
        surface = self.surface
        rest, lit = self.dust
        arrival = numpy.clip((self.formed - surface["delay"]) / ARRIVE_SECONDS, 0, 1)
        eased = 1 - (1 - arrival) ** 3
        # Each mote swirls in off the straight line, to one side of it.
        start = cloud[stars]
        path = rest - start
        sideways = numpy.stack([-path[:, 1], path[:, 0]], axis=1) / numpy.maximum(numpy.linalg.norm(path, axis=1, keepdims=True), 1e-3)
        swirl = numpy.sin(math.pi * eased)[:, None] * sideways * SWIRL * self.swirl_side[stars, None]
        # At rest, a mote drifts a fraction of a point.
        drift = (cloud[stars] - self.anchor[stars]) / self.style["wander"] * DRIFT
        target[stars] = start + path * eased[:, None] + swirl + drift
        flare = (4 * eased * (1 - eased)) ** 2 * 0.25 * self.magnitude[stars]
        settled = lit * eased
        light[stars] = self.cloud_light[stars] * NEBULA_LIGHT * (1 - eased) ** 2 + settled + flare
        mark_arrival = numpy.clip((self.marked - self.mark_delay) / MARK_SECONDS, 0, 1)
        mark_eased = 1 - (1 - mark_arrival) ** 3
        if float(mark_eased.max()) > 0.002:
            check, check_light = self._mark_dust()
            check_path = check - target[stars]
            check_side = numpy.stack([-check_path[:, 1], check_path[:, 0]], axis=1)
            check_side /= numpy.maximum(numpy.linalg.norm(check_side, axis=1, keepdims=True), 1e-3)
            mark_swirl = numpy.sin(math.pi * mark_eased)[:, None] * check_side * (SWIRL * 0.45) * self.swirl_side[stars, None]
            target[stars] = target[stars] + check_path * mark_eased[:, None] + mark_swirl
            mark_flare = (4 * mark_eased * (1 - mark_eased)) ** 2 * 0.22 * self.magnitude[stars]
            light[stars] = light[stars] * (1 - mark_eased) + check_light * mark_eased + mark_flare
        # The deep field: distant stars, nearly still, shifting against the head.
        ambient = self.ambient_stars
        target[ambient] = self.anchor[ambient] + drift_of(cloud[ambient] - self.anchor[ambient], 0.08) - self.parallax * PARALLAX
        light[ambient] = self.cloud_light[ambient] * (0.25 + 0.3 * self.scene)
        return target, light

    @property
    def visible(self) -> bool:
        return self.presence > 0.001 or self.presence_target > 0

    # Nodes ----------------------------------------------------------------------

    def splat(self, device_scale: float, *lights: numpy.ndarray, minimum_scale: float = 2.0) -> list[Gdk.Texture]:
        """Motes as soft points at the display's own pixel density, one texture per light.

        Without lights, one texture of every mote's own light.
        """
        return self._splat(device_scale, lights, minimum_scale, crop=False)[0]

    def splat_cropped(self, device_scale: float, *lights: numpy.ndarray) -> tuple[list[Gdk.Texture], Graphene.Rect] | None:
        """``splat`` into just the box around the lit motes, and that box in points.

        A few hundred motes then cost a few hundred motes, not a whole
        canvas of shoulder curve and upload; none lit gives None.
        """
        return self._splat(device_scale, lights, 2.0, crop=True)

    def _splat(self, device_scale: float, lights: tuple[numpy.ndarray, ...], minimum_scale: float, crop: bool) -> tuple[list[Gdk.Texture], Graphene.Rect] | None:
        # At least 2x supersampling resolves sub-point grains. Preserve the
        # actual fractional scale instead of rounding 1.25/1.5 to an integer.
        scale = max(minimum_scale, float(device_scale))
        width, height = math.ceil(self.width * scale), math.ceil(self.height * scale)
        lights = lights or (self.light,)
        lit = numpy.flatnonzero(numpy.max(lights, axis=0) > 0.004)
        x = self.position[lit, 0] * scale - 0.5
        y = self.position[lit, 1] * scale - 0.5
        # A compact optical point-spread function replaces four square taps.
        # Width is in logical points, so dust keeps its size on every monitor.
        # Stable grain identities vary the optical footprint without animated
        # sparkle or pose-dependent changes in particle size.
        sigma = numpy.full((len(lit), 1), 0.30 * scale, numpy.float32)
        if len(self.position) == len(self.magnitude):
            sigma[:, 0] = (0.23 + 0.11 * numpy.sqrt(self.magnitude[lit])) * scale
        radius = max(1, math.ceil(2 * 0.34 * scale - 0.5))
        offsets = numpy.arange(-radius, radius + 1)
        columns_x = numpy.rint(x).astype(numpy.int64)[:, None] + offsets
        rows_y = numpy.rint(y).astype(numpy.int64)[:, None] + offsets
        left = top = 0
        if crop:
            if not len(lit):
                return None
            left, top = max(0, int(columns_x.min())), max(0, int(rows_y.min()))
            right, bottom = min(width, int(columns_x.max()) + 1), min(height, int(rows_y.max()) + 1)
            if right <= left or bottom <= top:
                return None
            width, height = right - left, bottom - top
            columns_x -= left
            rows_y -= top
            x, y = x - left, y - top
        # Separable Gaussian: only 2K exponentials per particle, not K².
        wx = numpy.exp(-(columns_x.astype(numpy.float32) - x[:, None]) ** 2 / (2 * sigma ** 2))
        wy = numpy.exp(-(rows_y.astype(numpy.float32) - y[:, None]) ** 2 / (2 * sigma ** 2))
        wx /= numpy.maximum(wx.sum(axis=1, keepdims=True), 1e-9)
        wy /= numpy.maximum(wy.sum(axis=1, keepdims=True), 1e-9)
        weights = wy[:, :, None] * wx[:, None, :] * (self.style["core"] * scale ** 2)
        shape = weights.shape
        columns = numpy.broadcast_to(columns_x[:, None, :], shape)
        rows = numpy.broadcast_to(rows_y[:, :, None], shape)
        inside = (columns >= 0) & (columns < width) & (rows >= 0) & (rows < height)
        index = rows[inside] * width + columns[inside]
        textures = []
        for light in lights:
            energy = (weights * light[lit, None, None])[inside]
            linear = numpy.bincount(index, energy, minlength=width * height).astype(numpy.float32)
            # A soft exposure shoulder retains highlight structure instead of
            # clipping overlapping grains into opaque white blobs.
            pixels = shoulder(linear)
            textures.append(Gdk.MemoryTexture.new(width, height, Gdk.MemoryFormat.A8, GLib.Bytes.new(pixels.tobytes()), width))
        return textures, rect(left / scale, top / scale, width / scale, height / scale)

    def _tinted(self, texture: Gdk.Texture, bounds: Graphene.Rect, color: tuple[float, float, float]) -> Gsk.RenderNode:
        colored = Gtk.Snapshot()
        colored.push_mask(Gsk.MaskMode.ALPHA)
        colored.append_texture(texture, bounds)
        colored.pop()
        colored.append_color(Gdk.RGBA(red=color[0], green=color[1], blue=color[2], alpha=1), bounds)
        colored.pop()
        return colored.to_node()

    def _glowing(self, snapshot: Gtk.Snapshot, bounds: Graphene.Rect, grains: Gdk.Texture, glowing: tuple[Gdk.Texture, Graphene.Rect] | None, tint: tuple[float, float, float], glow: tuple[float, float, float]) -> None:
        """Grains over the glow of their bright motes (see ``glow``)."""
        if glowing is not None:
            snapshot.append_node(self._tinted(glowing[0], glowing[1], glow))
        snapshot.append_node(self._tinted(grains, bounds, tint))

    def glow(self, light: numpy.ndarray, box: Graphene.Rect | None = None) -> tuple[Gdk.Texture, Graphene.Rect] | None:
        """Bright motes' wide faint bloom and tighter glow, as one texture.

        Blurred here, at one pixel a point, rather than as GPU blur nodes: two
        full-scene blurs a frame cost the display more than all the dust.
        Motes deposit bilinearly, so they glide across pixels rather than
        stepping. ``box`` limits it to a region, in points.
        """
        pad = math.ceil(3 * self.style["bloom"] / 2) + 1
        left, top = (0, 0) if box is None else (max(0, int(box.get_x()) - pad), max(0, int(box.get_y()) - pad))
        right = self.width if box is None else min(self.width, math.ceil(box.get_x() + box.get_width()) + pad)
        bottom = self.height if box is None else min(self.height, math.ceil(box.get_y() + box.get_height()) + pad)
        width, height = right - left, bottom - top
        lit = numpy.flatnonzero(light > 0.004)
        if not len(lit) or width <= 0 or height <= 0:
            return None
        x = self.position[lit, 0] - 0.5 - left
        y = self.position[lit, 1] - 0.5 - top
        x0, y0 = numpy.floor(x).astype(numpy.int64), numpy.floor(y).astype(numpy.int64)
        fx, fy = (x - x0).astype(numpy.float32), (y - y0).astype(numpy.float32)
        energy = light[lit].astype(numpy.float32) * self.style["core"]
        linear = numpy.zeros(width * height, numpy.float32)
        for dx, dy, share in ((0, 0, (1 - fx) * (1 - fy)), (1, 0, fx * (1 - fy)), (0, 1, (1 - fx) * fy), (1, 1, fx * fy)):
            columns, rows = x0 + dx, y0 + dy
            inside = (columns >= 0) & (columns < width) & (rows >= 0) & (rows < height)
            linear += numpy.bincount(rows[inside] * width + columns[inside], (energy * share)[inside], minlength=width * height).astype(numpy.float32)
        base = -numpy.expm1(-linear.reshape(height, width))
        # GSK blurs take a radius; their Gaussian's sigma is half of it.
        halo = self.style["bloom_alpha"] * gaussian_blur(base, self.style["bloom"] / 2)
        halo += self.style["glow_alpha"] * gaussian_blur(base, self.style["glow"] / 2)
        pixels = numpy.rint(numpy.clip(halo, 0, 1) * 255).astype(numpy.uint8)
        texture = Gdk.MemoryTexture.new(width, height, Gdk.MemoryFormat.A8, GLib.Bytes.new(pixels.tobytes()), width)
        return texture, rect(left, top, width, height)

    def _hairlines(self, snapshot: Gtk.Snapshot, lines: list[tuple[numpy.ndarray, numpy.ndarray]], color: tuple[float, float, float], opacity: float) -> None:
        """Polylines as a round stroke, one path per step of opacity."""
        if opacity <= 0.01:
            return
        drawn = Gtk.Snapshot()
        steps: dict[int, Gsk.PathBuilder] = {}
        for points, alphas in lines:
            for index in range(len(points) - 1):
                step = round(float(alphas[index] + alphas[index + 1]) / 2 * 12)
                if step <= 0:
                    continue
                builder = steps.setdefault(step, Gsk.PathBuilder.new())
                builder.move_to(float(points[index][0]), float(points[index][1]))
                builder.line_to(float(points[index + 1][0]), float(points[index + 1][1]))
        stroke = Gsk.Stroke.new(1.0)
        stroke.set_line_cap(Gsk.LineCap.ROUND)
        stroke.set_line_join(Gsk.LineJoin.ROUND)
        for step, builder in steps.items():
            drawn.append_stroke(builder.to_path(), stroke, Gdk.RGBA(red=color[0], green=color[1], blue=color[2], alpha=min(1.0, step / 12)))
        node = drawn.to_node()
        if node is None:
            return
        snapshot.push_opacity(opacity)
        snapshot.push_opacity(0.8)
        snapshot.push_blur(2.0)
        snapshot.append_node(node)
        snapshot.pop()
        snapshot.pop()
        snapshot.append_node(node)
        snapshot.pop()

    def _disc(self, snapshot: Gtk.Snapshot, x: float, y: float, width: float, height: float, alpha: float, color: tuple[float, float, float]) -> None:
        if alpha <= 0.002:
            return
        snapshot.push_opacity(min(1.0, alpha))
        snapshot.append_node(self._tinted(self.disc, rect(x - width / 2, y - height / 2, width, height), color))
        snapshot.pop()

    def _source_centre(self) -> tuple[float, float]:
        """The source sits behind the head, a little above its centre.

        Drawn first, so the face occludes the core and light leaks around the
        crown rather than through the features.
        """
        if self.vertices is not None:
            low, high = self.vertices[:468, :2].min(axis=0), self.vertices[:468, :2].max(axis=0)
            centre = (low + high) / 2
            height = float(high[1] - low[1])
            return (
                float(centre[0]) - float(self.parallax[0]) * PARALLAX * 0.5,
                float(centre[1]) - 0.22 * height - float(self.parallax[1]) * PARALLAX * 0.35,
            )
        return (
            self.width / 2 - float(self.parallax[0]) * PARALLAX * 0.5,
            self.height / 2 - 10,
        )

    def _source_scale(self) -> float:
        """Corona size follows the face so it wraps this head, not a fixed oval."""
        if self.vertices is None:
            return 1.0
        height = float(numpy.ptp(self.vertices[:468, 1]))
        return max(0.55, min(1.35, height / (self.height / Framing.ZOOM)))

    def _source(self, snapshot: Gtk.Snapshot, look: Look, presence: float, breath: float) -> None:
        """The concentrated glow behind the head: core, corona, wash."""
        x, y = self._source_centre()
        scale = self._source_scale()
        amount = (0.40 + 0.60 * self.scene) * (0.88 + 0.12 * breath) * presence
        for width, height, alpha in SOURCE:
            self._disc(snapshot, x, y, width * scale, height * scale, alpha * amount, look.glow)

    def _haze(self, device_scale: float) -> Gdk.Texture:
        """The haze motes orbiting the source, splatted at half scale and blurred out of focus."""
        angle = self.haze_angle + self.haze_speed * self.clock
        breathe = 1 + 0.04 * math.sin(self.clock * math.tau / BREATH_SECONDS)
        centre = numpy.array(self._source_centre(), numpy.float32)
        positions = centre + numpy.stack([numpy.cos(angle) * 78, numpy.sin(angle) * 96], axis=1) * (self.haze_radius * breathe)[:, None]
        # Brighter near the core, still present at the edge of the corona.
        dist = numpy.hypot(positions[:, 0] - centre[0], positions[:, 1] - centre[1])
        falloff = 0.40 + 0.60 * numpy.clip(1 - dist / 130.0, 0, 1) ** 1.4
        saved = self.position
        self.position = positions.astype(numpy.float32)
        try:
            scale = 0.5
            width, height = math.ceil(self.width * scale), math.ceil(self.height * scale)
            x = numpy.clip((self.position[:, 0] * scale).astype(numpy.int64), 0, width - 1)
            y = numpy.clip((self.position[:, 1] * scale).astype(numpy.int64), 0, height - 1)
            linear = numpy.bincount(y * width + x, self.haze_light * falloff * self.style["core"], minlength=width * height).astype(numpy.float32)
            # A 2.5 pt blur radius: sigma 1.25 pt, at half scale.
            blurred = gaussian_blur(linear.reshape(height, width), 1.25 * scale * 2)
            pixels = shoulder(numpy.ascontiguousarray(blurred).ravel())
            return Gdk.MemoryTexture.new(width, height, Gdk.MemoryFormat.A8, GLib.Bytes.new(pixels.tobytes()), width)
        finally:
            self.position = saved

    def stars(self, device_scale: float, look: Look) -> Gsk.RenderNode | None:
        """The scene, back to front: source, haze, dust, guide, bokeh."""
        if not self.visible:
            return None
        width, height = self.width, self.height
        bounds = rect(0, 0, width, height)
        presence = min(1.0, self.presence)
        snapshot = Gtk.Snapshot()
        breath = 0.5 + 0.5 * math.sin(self.clock * math.tau / BREATH_SECONDS)

        # The source: a glow behind the head that the star dust is lit by.
        self._source(snapshot, look, presence, breath)
        # Haze lit by it: motes softened out of focus.
        snapshot.push_opacity(0.38 * (0.4 + 0.6 * self.scene) * presence)
        snapshot.append_node(self._tinted(self._haze(device_scale), bounds, look.glow))
        snapshot.pop()

        mark_amount = min(1.0, self.marked / 0.16) * presence
        if mark_amount > 0.02:
            ring = self._mark_ring_line()
            cx, cy = float(ring[:, 0].mean()), float(ring[:, 1].mean())
            rx = float(numpy.ptp(ring[:, 0])) * 0.72
            ry = float(numpy.ptp(ring[:, 1])) * 0.72
            self._disc(snapshot, cx, cy, rx, ry, 0.10 * mark_amount, look.glow)

        # The dust: bright motes spread into a wide faint bloom and a tighter
        # glow, under every grain itself. Only bright motes glow, so faint dust
        # stays fine and crisp.
        amount = self.arrow_amount * presence
        if amount > 0.01:
            # The guide's dust is drawn apart from the face's, in the guide's
            # colour, lifted toward white so the chevron stays the brightest
            # point. It blends back to the face's tint as the guide fades.
            guide = numpy.zeros(len(self.light), bool)
            guide[self.arrow_stars] = True
            dust = numpy.where(guide, 0, self.light)
            led = numpy.where(guide, self.light, 0)
            # The guide's pass only touches its own few hundred motes.
            (grains,) = self.splat(device_scale, dust)
            glowing = self.glow(dust * self.glow_share)
            guided = self.splat_cropped(device_scale, led)
            if guided is not None:
                (guide_grains,), guide_bounds = guided
                guide_glowing = self.glow(led * self.glow_share, guide_bounds)
            guide_glow = tuple(mix(look.glow[index], look.guide[index], self.arrow_amount) for index in range(3))
            guide_tint = tuple(mix(look.tint[index], mix(look.guide[index], 1.0, GUIDE_LIFT), self.arrow_amount) for index in range(3))
        else:
            (grains,) = self.splat(device_scale, self.light)
            glowing = self.glow(self.light * self.glow_share)
        self._glowing(snapshot, bounds, grains, glowing, look.tint, look.glow)

        # The seal's hairlines: the ring first, then the check as it writes.
        if mark_amount > 0.02:
            ring = self._mark_ring_line()
            self._hairlines(snapshot, [(ring, numpy.full(len(ring), 0.22, numpy.float32))], look.tint, mark_amount * 0.35)
            check_amount = min(1.0, max(0.0, (self.marked - 0.10) / 0.16)) * presence
            if check_amount > 0.04:
                start, corner, end = self._mark_points()
                self._hairlines(
                    snapshot,
                    [
                        (numpy.stack([start, corner]), numpy.full(2, 0.28, numpy.float32)),
                        (numpy.stack([corner, end]), numpy.full(2, 0.36, numpy.float32)),
                    ],
                    look.tint,
                    check_amount,
                )

        # The guide's hairlines: precise geometry under its dust, the chevron
        # the brightest point of the scene.
        if amount > 0.01 and self.guide_lines:
            self._hairlines(snapshot, self.guide_lines, guide_tint, amount)
        if amount > 0.01 and guided is not None:
            self._glowing(snapshot, guide_bounds, guide_grains, guide_glowing, guide_tint, guide_glow)

        # Bokeh: a few large motes out of focus, drifting past the lens.
        for index, (x, y, size) in enumerate(BOKEH):
            drift = self.bokeh_drift[index] * self.clock
            bx = (x * width + drift[0] + self.parallax[0] * PARALLAX * 1.6) % (width + 40) - 20
            by = (y * height + drift[1]) % (height + 40) - 20
            edge = float(feather(numpy.array([[bx, by]]), width, height, self.style["corner"], self.style["feather"] * 0.6)[0])
            flicker = 0.75 + 0.25 * math.sin(self.clock * 0.7 + index * 1.9)
            self._disc(snapshot, bx, by, size, size, BOKEH_ALPHA * flicker * (0.3 + 0.7 * edge) * presence, look.glow)
        return snapshot.to_node()

    def snapshot(self, snapshot: Gtk.Snapshot, device_scale: float, look: Look) -> None:
        stars = self.stars(device_scale, look)
        if stars is not None:
            snapshot.append_node(stars)


class StarField:
    """A ``Starlight`` simulated and drawn on its own thread.

    Stepping 8,000 motes and splatting them costs several milliseconds a
    frame, more than a 165 Hz display's whole frame, so on the GTK thread it
    held the island to about 20 frames a second. Here the GTK thread only
    queues what changed and appends the newest finished scene; the worker
    steps the dust on its own clock and records the next one meanwhile.
    numpy releases the interpreter lock for its large operations, so the two
    run side by side. The scene shown is at most one step old.

    Only the worker touches the ``Starlight``. Everything the GTK thread says
    is queued and applied in order before the next step.
    """

    def __init__(self, field: Starlight) -> None:
        self.field = field
        self.commands: deque[tuple[str, tuple]] = deque()
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.node: Gsk.RenderNode | None = None
        self.view: tuple[float, Look] = (1.0, Look())
        self.thread: threading.Thread | None = None
        self.last: float | None = None

    # The GTK thread ------------------------------------------------------------

    def submit(self, pixels: bytes, face: bool = True) -> None:
        self.commands.append(("submit", (pixels, face)))

    def submit_mesh(self, points: numpy.ndarray, mark: bool = False, axis: str = "") -> None:
        self.commands.append(("submit_mesh", (points, mark, axis)))

    def set_arrow(self, name: str) -> None:
        self.commands.append(("set_arrow", (name,)))

    def fade_out(self) -> None:
        self.commands.append(("fade_out", ()))

    def reset(self) -> None:
        self.commands.clear()
        self.commands.append(("reset", ()))
        with self.lock:
            self.node = None
        self.last = None

    def advance(self, _elapsed: float) -> None:
        """Ask for the next scene; the worker keeps its own clock."""
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="facegate-starlight", daemon=True)
            self.thread.start()
        self.wake.set()

    def snapshot(self, snapshot: Gtk.Snapshot, device_scale: float, look: Look) -> None:
        self.view = (device_scale, replace(look))
        with self.lock:
            node = self.node
        if node is not None:
            snapshot.append_node(node)

    def _disc(self, *args, **kwargs) -> None:
        # Reads only the disc texture, which never changes.
        self.field._disc(*args, **kwargs)

    # The worker -----------------------------------------------------------------

    def _run(self) -> None:
        field = self.field
        while True:
            self.wake.wait()
            self.wake.clear()
            while self.commands:
                name, args = self.commands.popleft()
                getattr(field, name)(*args)
            now = time.monotonic()
            if self.last is None:
                self.last = now
            elapsed, self.last = now - self.last, now
            field.advance(elapsed)
            scale, look = self.view
            node = field.stars(scale, look)
            with self.lock:
                self.node = node
