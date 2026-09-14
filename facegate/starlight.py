"""The enrollment preview as starlight: particles that form the face.

Like the particle cloud of Apple Watch pairing, the camera is never shown as a
picture. Thousands of tiny stars sit in a jittered field; each one's size and
brightness follow the face beneath it in real time, every star twinkles on its
own phase, and they sparkle into being as the scan starts and wink out again,
in random order, when it ends.

The stars are never drawn in Python. Each display frame NumPy writes one value
per star into two tiny textures (even and odd rows of the field), and the rest
is render nodes that GTK's renderer runs on the GPU (Vulkan, for FaceGate's
island):

- The textures are drawn nearest-neighbour, so each texel fills its star's
  cell: red holds the core's radius and blue marks that a star is lit.
- A tile holding the distance to each cell's star, jittered and periodic,
  repeats across the preview in green.
- A screen blend stacks those channels, and a colour matrix turns them into
  each star's sharp core (radius minus distance, in device pixels). A GPU
  blur of the cores is their glow, free to spread past each cell, and a
  mask feathers the edges.
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

# The distance tile and the feather are drawn this many texels per point.
TILE_TEXELS = 12
FEATHER_TEXELS = 2
# The jitter repeats every this many cells each way (even, to keep row parity).
JITTER_PERIOD = 8


def rect(x: float, y: float, width: float, height: float) -> Graphene.Rect:
    return Graphene.Rect().init(x, y, width, height)


def star_grid(width: int, height: int, pitch: float) -> dict[str, float]:
    """Cells on a hexagonal lattice centred in the frame: every other row shifts half a pitch."""
    row_height = pitch * math.sqrt(3) / 2
    rows = int((height - pitch) // row_height) + 1
    columns = [int((width - pitch - (pitch / 2 if parity else 0)) // pitch) + 1 for parity in (0, 1)]
    lefts = [(width - (count - 1) * pitch) / 2 for count in columns]
    return {
        "pitch": pitch, "row_height": row_height, "top": (height - (rows - 1) * row_height) / 2, "rows": rows,
        "left_even": lefts[0], "left_odd": lefts[1], "columns_even": columns[0], "columns_odd": columns[1],
    }


def star_cells(grid: dict[str, float]) -> list[tuple[int, int]]:
    """Every cell as (row, column), row by row."""
    return [(row, column) for row in range(grid["rows"]) for column in range(grid["columns_odd" if row % 2 else "columns_even"])]


def cell_centre(grid: dict[str, float], row: int, column: int) -> tuple[float, float]:
    return grid["left_odd" if row % 2 else "left_even"] + column * grid["pitch"], grid["top"] + row * grid["row_height"]


def jitter_pattern(pitch: float, row_height: float, seed: int = 7) -> numpy.ndarray:
    """A periodic (JITTER_PERIOD x JITTER_PERIOD x 2) offset for each star, kept inside its cell."""
    random = numpy.random.default_rng(seed)
    offsets = random.uniform(-1, 1, (JITTER_PERIOD, JITTER_PERIOD, 2))
    # Enough to scatter the lattice into a field, little enough that a core
    # stays inside its cell (the glow is a blur, and may spread past it).
    offsets[..., 0] *= 0.26 * pitch
    offsets[..., 1] *= 0.22 * row_height
    return offsets


def memory_texture(pixels: numpy.ndarray, memory_format: Gdk.MemoryFormat, channels: int) -> Gdk.Texture:
    height, width = pixels.shape[:2]
    data = numpy.ascontiguousarray(pixels, dtype=numpy.uint8)
    return Gdk.MemoryTexture.new(width, height, memory_format, GLib.Bytes.new(data.tobytes()), width * channels)


def feather_alpha(width: int, height: int, radius: float, feather: float, texels: int = FEATHER_TEXELS) -> numpy.ndarray:
    """A rounded rectangle whose last `feather` points fade to nothing, as 0..255."""
    ys, xs = numpy.mgrid[0:height * texels, 0:width * texels].astype(numpy.float32)
    x = (xs + 0.5) / texels - width / 2
    y = (ys + 0.5) / texels - height / 2
    qx, qy = numpy.abs(x) - (width / 2 - radius), numpy.abs(y) - (height / 2 - radius)
    edge = numpy.hypot(numpy.maximum(qx, 0), numpy.maximum(qy, 0)) + numpy.minimum(numpy.maximum(qx, qy), 0) - radius
    amount = numpy.clip(-edge / feather, 0, 1)
    return numpy.round(255 * amount * amount * (3 - 2 * amount)).astype(numpy.uint8)


def distance_tile(pitch: float, row_height: float, reach: float, jitter: numpy.ndarray, texels: int = TILE_TEXELS) -> numpy.ndarray:
    """One repeat of the field: each point's distance to its own cell's star, over `reach`.

    The tile starts half a pitch left of, and half a row above, an even-row
    cell, and spans JITTER_PERIOD cells each way, so it repeats seamlessly.
    Cells are assigned exactly as the per-star textures are drawn.
    """
    span_x, span_y = JITTER_PERIOD * pitch, JITTER_PERIOD * row_height
    width, height = round(span_x * texels), round(span_y * texels)
    ys, xs = numpy.mgrid[0:height, 0:width].astype(numpy.float32)
    x = (xs + 0.5) / width * span_x
    y = (ys + 0.5) / height * span_y
    row = numpy.minimum((y / row_height).astype(int), JITTER_PERIOD - 1)
    odd = row % 2 == 1
    # Even rows' stars sit at half a pitch into each cell; odd rows' on the cell lines.
    column = numpy.where(odd, numpy.floor(x / pitch + 0.5), numpy.floor(x / pitch)).astype(int)
    centre_x = numpy.where(odd, column * pitch, (column + 0.5) * pitch)
    centre_y = (row + 0.5) * row_height
    offsets = jitter[row, column % JITTER_PERIOD]
    distance = numpy.hypot(x - centre_x - offsets[..., 0], y - centre_y - offsets[..., 1])
    return numpy.round(255 * numpy.clip(distance / reach, 0, 1)).astype(numpy.uint8)


@dataclass
class Look:
    """What changes from frame to frame: the stars' colour and the shake."""

    tint: tuple[float, float, float] = (1.0, 1.0, 1.0)
    glow: tuple[float, float, float] = (1.0, 1.0, 1.0)
    shake: float = 0.0


class Starlight:
    """Keeps every star's state, and records the stars as render nodes."""

    def __init__(self, width: int, height: int, style: dict[str, float]) -> None:
        self.width, self.height, self.style = width, height, style
        self.grid = grid = star_grid(width, height, style["pitch"])
        cells = numpy.array(star_cells(grid))
        rows, columns = cells[:, 0], cells[:, 1]
        self.slots = (rows, columns, rows % 2)
        centres = numpy.array([cell_centre(grid, row, column) for row, column in cells], numpy.float32)
        # Five taps across each star's cell soften the camera's grain.
        x, y = centres[:, 0].astype(int), centres[:, 1].astype(int)
        taps = [(0, 0), (-2, 0), (2, 0), (0, -2), (0, 2)]
        self.taps = numpy.stack([numpy.clip(y + dy, 0, height - 1) * width + numpy.clip(x + dx, 0, width - 1) for dx, dy in taps])
        self.weights = numpy.array([0.4, 0.15, 0.15, 0.15, 0.15], numpy.float32)[:, None]
        # Each star's own twinkle, and the moment it appears as the field comes alive.
        random = numpy.random.default_rng(11)
        count = len(cells)
        self.phase = random.uniform(0, math.tau, count).astype(numpy.float32)
        self.rate = random.uniform(0.6, 1.8, count).astype(numpy.float32) * math.tau
        self.threshold = random.uniform(0, 1, count).astype(numpy.float32)
        jitter = jitter_pattern(grid["pitch"], grid["row_height"])
        tile = distance_tile(grid["pitch"], grid["row_height"], style["reach"], jitter)
        rgba = numpy.zeros(tile.shape + (4,), numpy.uint8)
        rgba[..., 1], rgba[..., 3] = tile, 255
        self.tile = memory_texture(rgba, Gdk.MemoryFormat.R8G8B8A8, 4)
        self.feather = memory_texture(feather_alpha(width, height, style["corner"], style["feather"]), Gdk.MemoryFormat.A8, 1)
        self.reset()

    def reset(self) -> None:
        count = len(self.threshold)
        self.levels = numpy.zeros(count, numpy.float32)
        self.targets = numpy.zeros(count, numpy.float32)
        self.has_frame = False
        # 0..1: how much of the field is alive. It rises as the scan starts and
        # falls when it ends; each star lives above its own threshold.
        self.presence = 0.0
        self.presence_target = 0.0
        self.clock = 0.0
        self.textures: tuple[Gdk.Texture, Gdk.Texture] | None = None

    # Frames and time -------------------------------------------------------------

    def brightness(self, pixels: bytes) -> numpy.ndarray:
        """Each star's brightness from the face: the frame's own range, a noise floor, a curve."""
        if len(pixels) != self.width * self.height:
            raise ValueError("frame size does not match its dimensions")
        frame = numpy.frombuffer(pixels, numpy.uint8).astype(numpy.float32)
        values = (frame[self.taps] * self.weights).sum(axis=0)
        low, high = numpy.percentile(values, (self.style["low_share"] * 100, self.style["high_share"] * 100))
        stretched = numpy.clip((values - low) / max(high - low, 5.0), 0, 1)
        return numpy.clip((stretched - self.style["floor"]) / (1 - self.style["floor"]), 0, 1) ** self.style["gamma"]

    def submit(self, pixels: bytes) -> None:
        self.targets = self.brightness(pixels).astype(numpy.float32)
        if not self.has_frame:
            self.has_frame = True
            self.presence_target = 1.0

    def fade_out(self) -> None:
        """The scan is over: the stars wink out."""
        self.presence_target = 0.0

    def advance(self, elapsed: float) -> None:
        style = self.style
        self.clock += elapsed
        # Stars follow the face closely, so the field tracks it in real time.
        self.levels += (self.targets - self.levels) * (1 - math.exp(-elapsed / style["follow_seconds"]))
        seconds = style["appear_seconds"] if self.presence_target > self.presence else style["vanish_seconds"]
        step = elapsed / seconds
        self.presence = min(self.presence_target, self.presence + step) if self.presence_target > self.presence else max(self.presence_target, self.presence - step)
        if not self.has_frame:
            return
        # A star is alive once the field's presence passes its threshold, and
        # swells into place over the next fifth of the way.
        alive = numpy.clip((self.presence * 1.25 - self.threshold) / 0.25, 0, 1)
        alive = alive * alive * (3 - 2 * alive)
        twinkle = 1 - style["twinkle"] * (0.5 + 0.5 * numpy.sin(self.rate * self.clock + self.phase))
        core = numpy.sqrt(self.levels) * alive * twinkle * (style["core"] / style["reach"])
        # Specks smaller than a device pixel only shimmer.
        lit = core * style["reach"] >= 0.3
        self.textures = self._textures(numpy.where(lit, core, 0), lit)

    def _textures(self, core: numpy.ndarray, lit: numpy.ndarray) -> tuple[Gdk.Texture, Gdk.Texture]:
        rows, columns, parity = self.slots
        textures = []
        for odd, key in ((0, "columns_even"), (1, "columns_odd")):
            chosen = parity == odd
            # Rows of the other parity stay transparent, so both textures stack.
            texels = numpy.zeros((self.grid["rows"], int(self.grid[key]), 4), numpy.uint8)
            texels[rows[chosen], columns[chosen], 0] = numpy.round(core[chosen] * 255)
            texels[rows[chosen], columns[chosen], 2] = lit[chosen] * 255
            texels[rows[chosen], columns[chosen], 3] = 255
            textures.append(memory_texture(texels, Gdk.MemoryFormat.R8G8B8A8, 4))
        return textures[0], textures[1]

    @property
    def visible(self) -> bool:
        return self.textures is not None and (self.presence > 0.001 or self.presence_target > 0)

    # Nodes ----------------------------------------------------------------------

    def _field(self, snapshot: Gtk.Snapshot) -> None:
        """Core radius (red), lit flag (blue) and distance to the star (green), stacked."""
        grid = self.grid
        pitch, row_height = grid["pitch"], grid["row_height"]
        snapshot.push_blend(Gsk.BlendMode.SCREEN)
        snapshot.append_color(Gdk.RGBA(red=0, green=0, blue=0, alpha=1), rect(0, 0, self.width, self.height))
        for texture, left, key in ((self.textures[0], grid["left_even"], "columns_even"), (self.textures[1], grid["left_odd"], "columns_odd")):
            bounds = rect(left - pitch / 2, grid["top"] - row_height / 2, grid[key] * pitch, grid["rows"] * row_height)
            snapshot.append_scaled_texture(texture, Gsk.ScalingFilter.NEAREST, bounds)
        snapshot.pop()
        tile = rect(grid["left_even"] - pitch / 2, grid["top"] - row_height / 2, JITTER_PERIOD * pitch, JITTER_PERIOD * row_height)
        snapshot.push_repeat(rect(0, 0, self.width, self.height), tile)
        snapshot.append_texture(self.tile, tile)
        snapshot.pop()
        snapshot.pop()

    def stars(self, device_scale: float, look: Look) -> Gsk.RenderNode | None:
        if not self.visible:
            return None
        style = self.style
        field = Gtk.Snapshot()
        self._field(field)
        # The cores: alpha = (radius - distance) in device pixels, centred on
        # each edge, only where a star is lit.
        gain = style["reach"] * device_scale
        matrix = Graphene.Matrix().init_from_float([0, 0, 0, gain, 0, 0, 0, -gain, 0, 0, 0, 0.5, 0, 0, 0, 0])
        cores = Gtk.Snapshot()
        cores.push_color_matrix(matrix, Graphene.Vec4().init(look.tint[0], look.tint[1], look.tint[2], 0))
        cores.append_node(field.to_node())
        cores.pop()
        core_node = cores.to_node()
        glow = Gtk.Snapshot()
        glow.push_color_matrix(Graphene.Matrix().init_from_float([0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1]), Graphene.Vec4().init(look.glow[0], look.glow[1], look.glow[2], 0))
        glow.append_node(core_node)
        glow.pop()
        glow_node = glow.to_node()
        snapshot = Gtk.Snapshot()
        # Starlight: each core's light spread into a soft glow, twice over for
        # a wide faint bloom and a tighter bright one.
        for radius, alpha in ((style["bloom"], style["bloom_alpha"]), (style["glow"], style["glow_alpha"])):
            snapshot.push_opacity(alpha)
            snapshot.push_blur(radius)
            snapshot.append_node(glow_node)
            snapshot.pop()
            snapshot.pop()
        snapshot.append_node(core_node)
        return snapshot.to_node()

    def snapshot(self, snapshot: Gtk.Snapshot, device_scale: float, look: Look) -> None:
        stars = self.stars(device_scale, look)
        if stars is None:
            return
        snapshot.push_mask(Gsk.MaskMode.ALPHA)
        snapshot.append_texture(self.feather, rect(0, 0, self.width, self.height))
        snapshot.pop()
        snapshot.append_node(stars)
        snapshot.pop()
