"""The enrollment preview as starlight: particles that gather into your face.

Like the particle cloud of Apple Watch pairing, the camera is never shown as a
picture. As soon as the preview opens, thousands of tiny stars drift in a slow,
wandering cloud. When the camera finds a face, each star flies to its own spot
and takes that spot's brightness, so the bright ones form the face and the
rest dim away; they keep following the face as it moves, and fall back into
the cloud if it's lost. Given the face's surface points instead of a frame,
each point takes its own star, and the stars left over go dark. To coach a pose,
some of the spare stars stream into a 3D arrow: an arc swinging around the
head for a turn or a tilt, a straight arrow for a move. When the scan ends they take the result's colour and
wink out, in random order.

The camera gives a lit frame about every 70 ms, but the stars move on springs
every display frame, so motion is as smooth as the display.

Nothing is rasterised by Python drawing code: NumPy integrates the stars and
splats them as points into one small texture at the display's own pixel
density, and render nodes (drawn on the GPU by GTK's renderer, Vulkan for the
island) tint that texture and bloom it through two blurs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Gtk", "4.0")
import numpy  # noqa: E402
from gi.repository import Gdk, GLib, Graphene, Gsk, Gtk  # noqa: E402


# The coaching arrow, in preview points. A turn is an arc of `ARROW_RADIUS`
# orbiting the head under the chin, a tilt an arc up the side of the head, and a
# move a straight arrow beside the face; each is seen `ARROW_VIEW` radians off its
# plane with perspective from `ARROW_FOCAL` away. The shaft streams toward a
# chevron head at `ARROW_FLOW` shaft lengths a second, and the whole arrow nudges
# its way every `ARROW_NUDGE_SECONDS`.
ARROW_STARS = 420
ARROW_HEAD_SHARE = 0.26
ARROW_SHAFT_WIDTH = 0.8
ARROW_HEAD_WIDTH = 17.0
ARROW_HEAD_LENGTH = 21.0
ARROW_RADIUS = 118.0
ARROW_SWEEP = {"turn": 0.85, "tilt": 0.6}
ARROW_VIEW = 0.38
ARROW_FOCAL = 420.0
ARROW_LENGTH = 54.0
ARROW_ASIDE = 98.0
ARROW_FLOW = 0.6
ARROW_NUDGE_SECONDS = 1.1
ARROW_FADE_SECONDS = 0.22
ARROWS = {"turn-left", "turn-right", "tilt-up", "tilt-down", "move-left", "move-right", "move-up", "move-down"}


def rect(x: float, y: float, width: float, height: float) -> Graphene.Rect:
    return Graphene.Rect().init(x, y, width, height)


def star_spots(width: int, height: int, pitch: float, seed: int = 7) -> numpy.ndarray:
    """Each star's place in the face: a jittered hexagonal field over the frame."""
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


@dataclass
class Look:
    """What changes from frame to frame: the stars' colours and the shake."""

    tint: tuple[float, float, float] = (1.0, 1.0, 1.0)
    glow: tuple[float, float, float] = (1.0, 1.0, 1.0)
    shake: float = 0.0


class Starlight:
    """Every star's motion and light, recorded as render nodes each frame."""

    def __init__(self, width: int, height: int, style: dict[str, float]) -> None:
        self.width, self.height, self.style = width, height, style
        self.spots = star_spots(width, height, style["pitch"])
        count = len(self.spots)
        random = numpy.random.default_rng(11)
        # Five taps across each spot soften the camera's grain.
        x = numpy.clip(self.spots[:, 0].astype(int), 0, width - 1)
        y = numpy.clip(self.spots[:, 1].astype(int), 0, height - 1)
        taps = [(0, 0), (-2, 0), (2, 0), (0, -2), (0, 2)]
        self.taps = numpy.stack([numpy.clip(y + dy, 0, height - 1) * width + numpy.clip(x + dx, 0, width - 1) for dx, dy in taps])
        self.weights = numpy.array([0.4, 0.15, 0.15, 0.15, 0.15], numpy.float32)[:, None]
        # The cloud: each star wanders around its own anchor, on two slow
        # waves per axis, so the whole cloud drifts without ever repeating.
        angle = random.uniform(0, math.tau, count)
        distance = numpy.sqrt(random.uniform(0, 1, count))
        self.anchor = numpy.stack([
            width / 2 + numpy.cos(angle) * distance * style["cloud_width"] / 2,
            height / 2 + numpy.sin(angle) * distance * style["cloud_height"] / 2,
        ], axis=1).astype(numpy.float32)
        self.wander_rate = random.uniform(0.08, 0.35, (count, 4)).astype(numpy.float32) * math.tau
        self.wander_phase = random.uniform(0, math.tau, (count, 4)).astype(numpy.float32)
        self.cloud_light = random.uniform(0.35, 0.85, count).astype(numpy.float32)
        # Each star's springiness varies a little, so they arrive as a swarm.
        self.stiffness = random.uniform(0.7, 1.3, count).astype(numpy.float32) * style["stiffness"]
        self.twinkle_rate = random.uniform(0.6, 1.8, count).astype(numpy.float32) * math.tau
        self.twinkle_phase = random.uniform(0, math.tau, count).astype(numpy.float32)
        self.threshold = random.uniform(0, 1, count).astype(numpy.float32)
        # Which star takes which surface point, and where a point's second and
        # third stars sit around it, so the mesh reads as dense starlight.
        self.mesh_order = random.permutation(count)
        self.mesh_jitter = random.uniform(-1.3, 1.3, (count, 2)).astype(numpy.float32)
        # The arrow's stars are the last in that order, so the mesh never takes them.
        arrow_count = min(ARROW_STARS, count // 4)
        self.arrow_stars = self.mesh_order[count - arrow_count:]
        # Shaft stars sit anywhere along it; head stars on the chevron's two wings.
        self.arrow_head = random.uniform(0, 1, arrow_count) < ARROW_HEAD_SHARE
        self.arrow_along = random.uniform(0, 1, arrow_count).astype(numpy.float32)
        self.arrow_wing = random.uniform(0, 1, arrow_count).astype(numpy.float32)
        self.arrow_side = numpy.where(random.uniform(0, 1, arrow_count) < 0.5, -1.0, 1.0).astype(numpy.float32)
        self.arrow_jitter = random.normal(0, 0.5, (arrow_count, 2)).astype(numpy.float32)
        self.reset()

    def reset(self) -> None:
        """A new preview: the cloud appears at once and waits for a face."""
        count = len(self.spots)
        self.clock = 0.0
        self.position = self._cloud().copy()
        self.velocity = numpy.zeros((count, 2), numpy.float32)
        self.levels = numpy.zeros(count, numpy.float32)
        self.targets = numpy.zeros(count, numpy.float32)
        self.face = False
        # Where each star goes on the face's mesh, when there is one.
        self.mesh: numpy.ndarray | None = None
        # The coaching arrow showing (kept while it fades) and how present it is.
        self.arrow = ""
        self.arrow_target = 0.0
        self.arrow_amount = 0.0
        # 0..1 how gathered into the face the stars are, and how alive the field is.
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
        self.mesh = None

    def submit_mesh(self, points: numpy.ndarray) -> None:
        """The face's surface points as (x, y, light) rows; none means no face."""
        if len(points) == 0:
            self.face = False
            return
        count, total = len(points), len(self.spots)
        places = self.spots.copy()
        targets = numpy.zeros(total, numpy.float32)
        # Up to three stars per point: one on it at full light, the rest
        # scattered just around it, dimmer.
        taken = min(total - len(self.arrow_stars), count * 3)
        stars = self.mesh_order[:taken]
        which = numpy.arange(taken) % count
        extra = numpy.arange(taken) >= count
        places[stars] = points[which, :2] + self.mesh_jitter[stars] * extra[:, None]
        light = points[which, 2] * numpy.where(extra, 0.3, 1.0)
        # Levels are drawn through a square root.
        targets[stars] = light * light
        self.mesh = places.astype(numpy.float32)
        self.targets = targets
        self.face = True

    def set_arrow(self, name: str) -> None:
        """Show the coaching arrow ("turn-left", "tilt-up", "move-right", …), or none."""
        if name in ARROWS:
            self.arrow = name
            self.arrow_target = 1.0
        else:
            self.arrow_target = 0.0

    def arrow_shape(self, name: str, clock: float) -> tuple[numpy.ndarray, numpy.ndarray]:
        """Where each arrow star is at `clock`, in preview points, and its light."""
        kind, direction = name.split("-")
        head = self.arrow_head
        # 0..1 along the arrow: the shaft streams toward the head; the chevron's
        # wings sweep back from the tip at 1.
        length = ARROW_LENGTH if kind == "move" else 2 * ARROW_SWEEP[kind] * ARROW_RADIUS
        head_share = ARROW_HEAD_LENGTH / length
        shaft_end = 1 - head_share * 0.5
        stream = (self.arrow_along + clock * ARROW_FLOW) % 1.0 * shaft_end
        along = numpy.where(head, 1 - self.arrow_wing * head_share, stream)
        across = numpy.where(head, self.arrow_side * self.arrow_wing * ARROW_HEAD_WIDTH, 0.0) + self.arrow_jitter[:, 0] * numpy.where(head, 0.35, ARROW_SHAFT_WIDTH)
        depth = self.arrow_jitter[:, 1]
        # A nudge its way: quick out, slow back.
        phase = (clock / ARROW_NUDGE_SECONDS) % 1.0
        nudge = math.sin(math.pi * phase ** 0.6) ** 2
        forward = 1.0 if direction in ("right", "down") else -1.0
        if kind == "move":
            offset = ((along - 0.5) * ARROW_LENGTH + nudge * 8) * forward
            if direction in ("left", "right"):
                # Beside the face, on the side it points to.
                x, y, z = forward * ARROW_ASIDE + offset, across, depth
                yaw, pitch = 0.5 * math.sin(clock * 1.4), 0.0
            else:
                x, y, z = ARROW_ASIDE + across, offset, depth
                yaw, pitch = 0.0, 0.5 * math.sin(clock * 1.4)
        else:
            angle = forward * ((along * 2 - 1) * ARROW_SWEEP[kind] + nudge * 0.12)
            ring = ARROW_RADIUS + depth
            if kind == "turn":
                # Orbiting the head's vertical axis, seen from above, so its
                # front passes under the chin.
                x, y, z = ring * numpy.sin(angle), across, ring * numpy.cos(angle)
                yaw, pitch = 0.0, ARROW_VIEW
            else:
                # Orbiting the ear-to-ear axis, seen from the side, so its
                # front passes beside the face.
                x, y, z = across, ring * numpy.sin(angle), ring * numpy.cos(angle)
                yaw, pitch = ARROW_VIEW, 0.0
        # Seen `pitch` from above and `yaw` from the side.
        y, z = y * math.cos(pitch) + z * math.sin(pitch), z * math.cos(pitch) - y * math.sin(pitch)
        x, z = x * math.cos(yaw) + z * math.sin(yaw), z * math.cos(yaw) - x * math.sin(yaw)
        # Nudged clear of the face: the turn's front just under the mouth, the
        # tilt's front beside the cheek.
        if kind == "turn":
            y = y + 24
        elif kind == "tilt":
            x = x + 31
        scale = ARROW_FOCAL / numpy.maximum(ARROW_FOCAL - z, 60.0)
        x = numpy.broadcast_to(x, along.shape)
        y = numpy.broadcast_to(y, along.shape)
        points = numpy.stack([self.width / 2 + x * scale, self.height / 2 + y * scale], axis=1).astype(numpy.float32)
        # Brighter toward the head and nearer the viewer; the stream fades in at
        # the tail and out where it meets the head.
        shaft = numpy.clip(along / 0.1, 0, 1) * numpy.clip((shaft_end - along) / 0.06, 0, 1)
        near = numpy.clip(scale, 0.6, 1.4) ** 1.5
        light = numpy.where(head, 0.85, (0.3 + 0.7 * along / shaft_end) * shaft) * near
        return points, numpy.clip(light, 0, 1.2).astype(numpy.float32)

    def fade_out(self) -> None:
        """The scan is over: the stars wink out."""
        self.presence_target = 0.0

    # Motion ---------------------------------------------------------------------

    def _cloud(self) -> numpy.ndarray:
        wave = numpy.sin(self.wander_rate * self.clock + self.wander_phase)
        drift = numpy.stack([wave[:, 0] + 0.6 * wave[:, 1], wave[:, 2] + 0.6 * wave[:, 3]], axis=1)
        return self.anchor + drift * self.style["wander"]

    def advance(self, elapsed: float) -> None:
        style = self.style
        elapsed = min(elapsed, 1 / 20)
        self.clock += elapsed
        # Light follows the camera closely; gathering eases in and out.
        self.levels += (self.targets - self.levels) * (1 - math.exp(-elapsed / style["follow_seconds"]))
        goal = 1.0 if self.face else 0.0
        self.gathered += (goal - self.gathered) * (1 - math.exp(-elapsed / style["gather_seconds"]))
        seconds = style["appear_seconds"] if self.presence_target > self.presence else style["vanish_seconds"]
        change = elapsed / seconds
        self.presence = min(self.presence_target, self.presence + change) if self.presence_target > self.presence else max(self.presence_target, self.presence - change)

        # Stars spring toward the face's spots or back into the cloud.
        target = (self.spots if self.mesh is None else self.mesh) if self.face else self._cloud()
        stiffness = self.stiffness[:, None]
        damping = 2 * 0.82 * numpy.sqrt(stiffness)
        self.velocity += ((target - self.position) * stiffness - self.velocity * damping) * elapsed
        self.position += self.velocity * elapsed

        self.arrow_amount += (self.arrow_target - self.arrow_amount) * (1 - math.exp(-elapsed / ARROW_FADE_SECONDS))
        arrow = self.arrow_amount > 0.01 and self.arrow in ARROWS
        if arrow:
            # Arrow stars follow their stream closely rather than on springs,
            # which would smear it; they fly in from wherever they were.
            stars = self.arrow_stars
            shape, arrow_light = self.arrow_shape(self.arrow, self.clock)
            follow = 1 - math.exp(-elapsed * 18)
            self.position[stars] += (shape - self.position[stars]) * follow
            self.velocity[stars] = 0

        # A star is alive once the field's presence passes its threshold.
        alive = numpy.clip((self.presence * 1.25 - self.threshold) / 0.25, 0, 1)
        alive = alive * alive * (3 - 2 * alive)
        twinkle = 1 - style["twinkle"] * (0.5 + 0.5 * numpy.sin(self.twinkle_rate * self.clock + self.twinkle_phase))
        # Cloud light gives way quickly as the stars gather, so stars bound for
        # the dark around a face fade on the way rather than filling the frame.
        light = self.cloud_light * (1 - self.gathered) ** 3 + numpy.sqrt(self.levels) * self.gathered
        if arrow:
            stars = self.arrow_stars
            light[stars] = light[stars] * (1 - self.arrow_amount) + arrow_light * self.arrow_amount
        self.light = light * alive * twinkle * feather(self.position, self.width, self.height, style["corner"], style["feather"])

    @property
    def visible(self) -> bool:
        return self.presence > 0.001 or self.presence_target > 0

    # Nodes ----------------------------------------------------------------------

    def splat(self, device_scale: float) -> Gdk.Texture:
        """Every star as a soft point, at the display's own pixel density."""
        scale = max(1, round(device_scale))
        width, height = self.width * scale, self.height * scale
        x = self.position[:, 0] * scale - 0.5
        y = self.position[:, 1] * scale - 0.5
        left, top = numpy.floor(x), numpy.floor(y)
        fx, fy = x - left, y - top
        left, top = left.astype(numpy.int64), top.astype(numpy.int64)
        light = self.light * self.style["core"]
        # Each star shares its light between the four pixels around it, all
        # gathered in one pass.
        columns = numpy.concatenate([left, left + 1, left, left + 1])
        rows = numpy.concatenate([top, top, top + 1, top + 1])
        shares = numpy.concatenate([(1 - fx) * (1 - fy), fx * (1 - fy), (1 - fx) * fy, fx * fy]) * numpy.tile(light, 4)
        inside = (columns >= 0) & (columns < width) & (rows >= 0) & (rows < height) & (shares > 0.002)
        image = numpy.bincount(rows[inside] * width + columns[inside], shares[inside], minlength=width * height)
        pixels = numpy.clip(image * 255, 0, 255).astype(numpy.uint8)
        return Gdk.MemoryTexture.new(width, height, Gdk.MemoryFormat.A8, GLib.Bytes.new(pixels.tobytes()), width)

    def stars(self, device_scale: float, look: Look) -> Gsk.RenderNode | None:
        if not self.visible:
            return None
        texture = self.splat(device_scale)
        bounds = rect(0, 0, self.width, self.height)

        def lit(color: tuple[float, float, float]) -> Gsk.RenderNode:
            colored = Gtk.Snapshot()
            colored.push_mask(Gsk.MaskMode.ALPHA)
            colored.append_texture(texture, bounds)
            colored.pop()
            colored.append_color(Gdk.RGBA(red=color[0], green=color[1], blue=color[2], alpha=1), bounds)
            colored.pop()
            return colored.to_node()

        glow = lit(look.glow)
        snapshot = Gtk.Snapshot()
        # Starlight: each point's light spread into a wide faint bloom and a
        # tighter bright glow, under the sharp points themselves.
        for radius, alpha in ((self.style["bloom"], self.style["bloom_alpha"]), (self.style["glow"], self.style["glow_alpha"])):
            snapshot.push_opacity(alpha)
            snapshot.push_blur(radius)
            snapshot.append_node(glow)
            snapshot.pop()
            snapshot.pop()
        snapshot.append_node(lit(look.tint))
        return snapshot.to_node()

    def snapshot(self, snapshot: Gtk.Snapshot, device_scale: float, look: Look) -> None:
        stars = self.stars(device_scale, look)
        if stars is not None:
            snapshot.append_node(stars)
