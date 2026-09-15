"""The enrollment preview as starlight: particles that gather into your face.

Like the particle cloud of Apple Watch pairing, the camera is never shown as a
picture. As soon as the preview opens, thousands of tiny stars drift in a slow,
wandering cloud. When the camera finds a face, each star flies to its own spot
and takes that spot's brightness, so the bright ones form the face and the
rest dim away; they keep following the face as it moves, and fall back into
the cloud if it's lost. When the scan ends they take the result's colour and
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
        target = self.spots if self.face else self._cloud()
        stiffness = self.stiffness[:, None]
        damping = 2 * 0.82 * numpy.sqrt(stiffness)
        self.velocity += ((target - self.position) * stiffness - self.velocity * damping) * elapsed
        self.position += self.velocity * elapsed

        # A star is alive once the field's presence passes its threshold.
        alive = numpy.clip((self.presence * 1.25 - self.threshold) / 0.25, 0, 1)
        alive = alive * alive * (3 - 2 * alive)
        twinkle = 1 - style["twinkle"] * (0.5 + 0.5 * numpy.sin(self.twinkle_rate * self.clock + self.twinkle_phase))
        # Cloud light gives way quickly as the stars gather, so stars bound for
        # the dark around a face fade on the way rather than filling the frame.
        light = self.cloud_light * (1 - self.gathered) ** 3 + numpy.sqrt(self.levels) * self.gathered
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
