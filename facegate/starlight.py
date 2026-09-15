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
faint distant stars that shifts against the head's turn, a back light that
separates the head from the dark and breathes slowly, haze orbiting through
it, the steadily illuminated face itself, the
coaching guide, and a few out-of-focus motes drifting past the lens.

To coach, a guide appears in precise hairline geometry with its own dust
streaming along it: an arc orbiting the head for a turn or tilt, a straight
arrow for a move, and a head contour approaching or receding for closer or back.
Arrowheads lie flat on the screen so they read at any angle, and the
face keeps its light as the guide changes. When the scan ends the scene takes the
result's colour and winks out, in random order.

Without a face mesh (no ONNX Runtime), each mote instead takes a spot on a
field over the frame and that spot's brightness, as before.

The camera gives a frame about every 70 ms, but the dust moves on springs
every display frame, so motion is as smooth as the display. Nothing is
rasterised by Python drawing code: NumPy integrates the motes and splats
them into two small textures at the display's own pixel density (every mote,
and the bright ones for glow), and render nodes (drawn on the GPU by GTK's
renderer, Vulkan for the island) tint them and bloom the glow through two
blurs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Gtk", "4.0")
import numpy  # noqa: E402
from gi.repository import Gdk, GLib, Graphene, Gsk, Gtk  # noqa: E402

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
# How bright the nebula is, and on the face how far a mote drifts, in points.
NEBULA_LIGHT = 0.45
DRIFT = 0.08

# The scene around the face, back to front, in preview points. A deep field of
# faint distant stars shifts `PARALLAX` points against the head's turn (the
# foreground bokeh the other way), a back light glows behind the head and
# breathes every `BREATH_SECONDS`, haze motes orbit slowly through it, and a
# few large out-of-focus motes drift past the lens.
PARALLAX = 7.0
BREATH_SECONDS = 7.0
HALO = (220.0, 260.0, 0.035)
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
# A head silhouette moves through depth: larger means closer to the lens.
# A quiet destination silhouette and explicit radial chevrons explain scale.
DISTANCE_RADIUS = (69.0, 81.0)
DISTANCE_SECONDS = 1.8
DISTANCE_SCALE = (0.84, 1.12)
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
        self.haze_radius = (0.55 + 0.6 * numpy.sqrt(random.uniform(0, 1, HAZE_MOTES))).astype(numpy.float32)
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

    def submit_mesh(self, points: numpy.ndarray) -> None:
        """The face mesh's points as (x, y, z) in preview pixels rows; none means no face."""
        if len(points) == 0:
            self.face = False
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
        return normals / numpy.maximum(length, 1e-6)

    def _face_dust(self) -> tuple[numpy.ndarray, numpy.ndarray]:
        """Where each face mote rests on the newest face, and how lit it is there."""
        surface, points = self.surface, self.vertices
        corners, weights = surface["corners"], surface["weights"][:, :, None]
        span = float(numpy.ptp(points[:, 1])) or 1.0
        rest = (weights * points[corners, :2]).sum(axis=1) + surface["scatter"] * span
        normals = self._normals(points)
        normal = (weights * normals[corners]).sum(axis=1)
        normal /= numpy.maximum(numpy.linalg.norm(normal, axis=1, keepdims=True), 1e-6)
        def direction(value):
            vector = numpy.asarray(value, numpy.float32)
            return vector / numpy.linalg.norm(vector)

        key_direction = direction(KEY_LIGHT)
        key = numpy.maximum(normal @ key_direction, 0)
        fill = numpy.maximum(normal @ direction(FILL_LIGHT), 0)
        rim = numpy.maximum(normal @ direction(RIM_DIRECTION), 0) ** 1.5
        # Broad diffuse planes plus a restrained view-dependent highlight.
        # The specular lobe moves across the cheeks/nose as the head turns.
        half_vector = direction(key_direction + [0, 0, 1])
        specular = numpy.maximum(normal @ half_vector, 0) ** 12 * key
        shade = 0.13 + 0.64 * key + 0.22 * fill + 0.15 * rim
        feature = surface["feature"]
        shade = numpy.where(feature, shade * 1.12, shade)
        visibility = smoothstep(0.02, 0.20, normal[:, 2])
        # Every surface grain remains present. Lighting controls radiance,
        # not occupancy: changing density with normals made moving shadows
        # look like holes in the user's face.
        magnitude = self.magnitude[self.face_stars]
        radiance = (shade ** 1.15 + 0.12 * specular) * GRAIN * (0.18 + 0.82 * magnitude)
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
        low, high = DISTANCE_SCALE
        scale = low + (high - low) * (progress if closer else 1 - progress)
        destination = high if closer else low
        alpha = float(smoothstep(0.0, 0.12, cycle) * (1 - smoothstep(0.72, 1.0, cycle)))
        centre = numpy.array([self.width / 2, self.height / 2], numpy.float32)

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
            (target, numpy.full(len(target), 0.16, numpy.float32)),
            (moving, numpy.full(len(moving), 0.52 * alpha, numpy.float32)),
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
        self.scene += ((1.0 if self.face else 0.0) - self.scene) * (1 - math.exp(-elapsed / 0.8))
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
            attached = smoothstep(0.65, 1.0, arrival)[:, None]
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

    def _advance_dust(self, elapsed: float, cloud: numpy.ndarray) -> tuple[numpy.ndarray, numpy.ndarray]:
        """Targets and light while the face is dust: condensing, resting, or spreading."""
        if self.face:
            self.formed += elapsed
        else:
            self.formed = max(0.0, self.formed - elapsed * LEAVE_SPEED)
        # Saturate at the final mote's arrival, so losing a long-held face
        # starts releasing dust immediately instead of waiting seconds.
        self.formed = min(self.formed, float(self.surface["delay"].max()) + ARRIVE_SECONDS)
        # Once spread back out, the next face condenses afresh.
        if not self.face and self.formed <= 0.0:
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
        sigma = 0.30 * scale
        radius = max(1, math.ceil(2 * sigma - 0.5))
        offsets = numpy.arange(-radius, radius + 1)
        columns_x = numpy.rint(x).astype(numpy.int64)[:, None] + offsets
        rows_y = numpy.rint(y).astype(numpy.int64)[:, None] + offsets
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
            linear = numpy.bincount(index, energy, minlength=width * height)
            # A soft exposure shoulder retains highlight structure instead of
            # clipping overlapping grains into opaque white blobs.
            pixels = numpy.rint(-numpy.expm1(-linear) * 255).astype(numpy.uint8)
            textures.append(Gdk.MemoryTexture.new(width, height, Gdk.MemoryFormat.A8, GLib.Bytes.new(pixels.tobytes()), width))
        return textures

    def _tinted(self, texture: Gdk.Texture, bounds: Graphene.Rect, color: tuple[float, float, float]) -> Gsk.RenderNode:
        colored = Gtk.Snapshot()
        colored.push_mask(Gsk.MaskMode.ALPHA)
        colored.append_texture(texture, bounds)
        colored.pop()
        colored.append_color(Gdk.RGBA(red=color[0], green=color[1], blue=color[2], alpha=1), bounds)
        colored.pop()
        return colored.to_node()

    def _disc(self, snapshot: Gtk.Snapshot, x: float, y: float, width: float, height: float, alpha: float, color: tuple[float, float, float]) -> None:
        if alpha <= 0.002:
            return
        snapshot.push_opacity(min(1.0, alpha))
        snapshot.append_node(self._tinted(self.disc, rect(x - width / 2, y - height / 2, width, height), color))
        snapshot.pop()

    def _haze(self, device_scale: float) -> Gdk.Texture:
        """The haze motes orbiting the head, splatted for blurring."""
        angle = self.haze_angle + self.haze_speed * self.clock
        breathe = 1 + 0.04 * math.sin(self.clock * math.tau / BREATH_SECONDS)
        centre = numpy.array([self.width / 2, self.height / 2 - 6]) - self.parallax * PARALLAX * 0.5
        positions = centre + numpy.stack([numpy.cos(angle) * 78, numpy.sin(angle) * 96], axis=1) * (self.haze_radius * breathe)[:, None]
        saved = self.position
        self.position = positions.astype(numpy.float32)
        try:
            return self.splat(0.5, self.haze_light, minimum_scale=0.5)[0]
        finally:
            self.position = saved

    def stars(self, device_scale: float, look: Look) -> Gsk.RenderNode | None:
        """The scene, back to front: back light, haze, dust, guide, bokeh."""
        if not self.visible:
            return None
        width, height = self.width, self.height
        bounds = rect(0, 0, width, height)
        presence = min(1.0, self.presence)
        snapshot = Gtk.Snapshot()
        breath = 0.5 + 0.5 * math.sin(self.clock * math.tau / BREATH_SECONDS)

        # The back light: a soft glow behind the head that separates it from the dark.
        halo_width, halo_height, halo_alpha = HALO
        self._disc(
            snapshot, width / 2 - self.parallax[0] * PARALLAX * 0.5, height / 2 - 10,
            halo_width, halo_height, halo_alpha * (0.35 + 0.65 * self.scene) * (0.85 + 0.15 * breath) * presence, look.glow,
        )
        # Haze lit by it: motes softened out of focus.
        snapshot.push_opacity(0.55 * (0.4 + 0.6 * self.scene) * presence)
        snapshot.push_blur(2.5)
        snapshot.append_node(self._tinted(self._haze(device_scale), bounds, look.glow))
        snapshot.pop()
        snapshot.pop()

        # The dust: bright motes spread into a wide faint bloom and a tighter
        # glow, under every grain itself. Only bright motes glow, so faint dust
        # stays fine and crisp.
        grains, glowing = self.splat(device_scale, self.light, self.light * self.glow_share)
        glow = self._tinted(glowing, bounds, look.glow)
        for radius, alpha in ((self.style["bloom"], self.style["bloom_alpha"]), (self.style["glow"], self.style["glow_alpha"])):
            snapshot.push_opacity(alpha)
            snapshot.push_blur(radius)
            snapshot.append_node(glow)
            snapshot.pop()
            snapshot.pop()
        snapshot.append_node(self._tinted(grains, bounds, look.tint))

        # The guide's hairlines: precise geometry under its dust, the chevron
        # the brightest point of the scene.
        amount = self.arrow_amount * presence
        if amount > 0.01 and self.guide_lines:
            lines = Gtk.Snapshot()
            # Segments share one path per step of opacity: a fading line in a
            # handful of strokes rather than one per segment.
            steps: dict[int, Gsk.PathBuilder] = {}
            for points, alphas in self.guide_lines:
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
                lines.append_stroke(builder.to_path(), stroke, Gdk.RGBA(red=look.tint[0], green=look.tint[1], blue=look.tint[2], alpha=min(1.0, step / 12)))
            node = lines.to_node()
            if node is not None:
                snapshot.push_opacity(amount)
                snapshot.push_opacity(0.8)
                snapshot.push_blur(2.0)
                snapshot.append_node(node)
                snapshot.pop()
                snapshot.pop()
                snapshot.append_node(node)
                snapshot.pop()

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
