"""The enrollment preview's halftone, as GSK render nodes for the Vulkan renderer.

The camera arrives as a small greyscale frame. The dots are never drawn in
Python: each frame becomes two tiny textures holding one value per dot, and
everything per pixel is expressed as render nodes that GTK's renderer runs on
the GPU (Vulkan, for FaceGate's island):

- Each dot's size lives in a texture with one texel per dot, one texture for
  even rows and one for odd, drawn with nearest-neighbour scaling so every
  texel fills its dot's cell.
- A small tile holding the distance to the nearest dot centre repeats across
  the whole preview.
- A screen blend puts size and distance into separate channels, and a colour
  matrix turns (size - distance) into each dot's anti-aliased edge, in device
  pixels.
- A cross-fade blends the last two frames over the time frames really take,
  a mask feathers the rounded rectangle, and a gradient through the dots makes
  the sweep of light.

Per camera frame NumPy gathers about 2,000 values; per display frame Python
only records a couple of dozen nodes.
"""

from __future__ import annotations

import math
import time
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


def rect(x: float, y: float, width: float, height: float) -> Graphene.Rect:
    return Graphene.Rect().init(x, y, width, height)


def dot_grid(width: int, height: int, pitch: float) -> dict[str, float]:
    """A hexagonal grid centred in the frame: every other row shifts half a pitch."""
    row_height = pitch * math.sqrt(3) / 2
    rows = int((height - pitch) // row_height) + 1
    columns = [int((width - pitch - (pitch / 2 if parity else 0)) // pitch) + 1 for parity in (0, 1)]
    lefts = [(width - (count - 1) * pitch) / 2 for count in columns]
    return {
        "pitch": pitch, "row_height": row_height, "top": (height - (rows - 1) * row_height) / 2, "rows": rows,
        "left_even": lefts[0], "left_odd": lefts[1], "columns_even": columns[0], "columns_odd": columns[1],
    }


def dot_centres(grid: dict[str, float]) -> list[tuple[float, float]]:
    return [
        (grid["left_odd" if row % 2 else "left_even"] + column * grid["pitch"], grid["top"] + row * grid["row_height"])
        for row in range(grid["rows"])
        for column in range(grid["columns_odd" if row % 2 else "columns_even"])
    ]


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


def distance_tile(pitch: float, row_height: float, max_radius: float, texels: int = TILE_TEXELS) -> numpy.ndarray:
    """One repeat of the grid: distance to its row's nearest dot, over max_radius, clamped."""
    width, height = round(pitch * texels), round(2 * row_height * texels)
    ys, xs = numpy.mgrid[0:height, 0:width].astype(numpy.float32)
    x = (xs + 0.5) / width * pitch
    y = (ys + 0.5) / height * 2 * row_height
    # Tile origin sits half a pitch left and half a row above an even dot: an
    # even dot at (pitch/2, row/2), and odd dots at the corners of the next row.
    even = numpy.hypot(x - pitch / 2, y - row_height / 2)
    odd = numpy.minimum(numpy.hypot(x, y - 1.5 * row_height), numpy.hypot(x - pitch, y - 1.5 * row_height))
    distance = numpy.where(y < row_height, even, odd)
    return numpy.round(255 * numpy.clip(distance / max_radius, 0, 1)).astype(numpy.uint8)


@dataclass
class Look:
    """What changes from frame to frame: colours, the sweep, the shake."""

    tint: tuple[float, float, float] = (1.0, 1.0, 1.0)
    band_color: tuple[float, float, float] = (0.0, 0.0, 0.0)
    band_middle: float = -100.0
    band_alpha: float = 0.0
    shake: float = 0.0


class Halftone:
    """Turns frames into dot-size textures, and records the dots as render nodes."""

    def __init__(self, width: int, height: int, style: dict[str, float]) -> None:
        self.width, self.height, self.style = width, height, style
        self.grid = grid = dot_grid(width, height, style["pitch"])
        centres = numpy.array(dot_centres(grid), numpy.float32)
        rows = numpy.round((centres[:, 1] - grid["top"]) / grid["row_height"]).astype(int)
        parity = rows % 2
        columns = numpy.round((centres[:, 0] - numpy.where(parity, grid["left_odd"], grid["left_even"])) / grid["pitch"]).astype(int)
        self.slots = (rows, columns, parity)
        # Five taps across each dot's cell soften the camera's grain.
        column, row = centres[:, 0].astype(int), centres[:, 1].astype(int)
        taps = [(0, 0), (-2, 0), (2, 0), (0, -2), (0, 2)]
        self.taps = numpy.stack([
            numpy.clip(row + dy, 0, height - 1) * width + numpy.clip(column + dx, 0, width - 1) for dx, dy in taps
        ])
        self.weights = numpy.array([0.4, 0.15, 0.15, 0.15, 0.15], numpy.float32)[:, None]
        tile = distance_tile(grid["pitch"], grid["row_height"], style["max_radius"])
        rgba = numpy.zeros(tile.shape + (4,), numpy.uint8)
        rgba[..., 1], rgba[..., 3] = tile, 255
        self.tile = memory_texture(rgba, Gdk.MemoryFormat.R8G8B8A8, 4)
        self.feather = memory_texture(feather_alpha(width, height, style["corner"], style["feather"]), Gdk.MemoryFormat.A8, 1)
        self.reset()

    def reset(self) -> None:
        self.frames: list[tuple[Gdk.Texture, Gdk.Texture]] = []
        self.arrived = 0.0
        self.interval = 1 / 30

    # Frames ---------------------------------------------------------------------

    def sizes(self, pixels: bytes) -> numpy.ndarray:
        """Each dot's radius as a share of the largest: levels, noise floor, curve."""
        frame = numpy.frombuffer(pixels, numpy.uint8).astype(numpy.float32)
        values = (frame[self.taps] * self.weights).sum(axis=0)
        low, high = numpy.percentile(values, (self.style["low_share"] * 100, self.style["high_share"] * 100))
        stretched = numpy.clip((values - low) / max(high - low, 5.0), 0, 1)
        level = numpy.clip((stretched - self.style["floor"]) / (1 - self.style["floor"]), 0, 1) ** self.style["gamma"]
        size = numpy.sqrt(level)
        # Specks smaller than a device pixel only shimmer.
        size[size * self.style["max_radius"] < 0.45] = 0
        return size

    def submit(self, pixels: bytes) -> None:
        if len(pixels) != self.width * self.height:
            raise ValueError("frame size does not match its dimensions")
        size = self.sizes(pixels)
        rows, columns, parity = self.slots
        textures = []
        for odd, key in ((0, "columns_even"), (1, "columns_odd")):
            chosen = parity == odd
            # Rows of the other parity stay transparent, so both textures stack.
            texels = numpy.zeros((self.grid["rows"], int(self.grid[key]), 4), numpy.uint8)
            texels[rows[chosen], columns[chosen], 0] = numpy.round(size[chosen] * 255)
            texels[rows[chosen], columns[chosen], 3] = 255
            textures.append(memory_texture(texels, Gdk.MemoryFormat.R8G8B8A8, 4))
        now = time.monotonic()
        if self.frames:
            # Blend over the time frames really take, so each blend ends as the next frame lands.
            self.interval += (min(max(now - self.arrived, 1 / 120), 0.25) - self.interval) * 0.3
        self.arrived = now
        self.frames = (self.frames + [tuple(textures)])[-2:]

    # Nodes ----------------------------------------------------------------------

    def _sizes(self, snapshot: Gtk.Snapshot, textures: tuple[Gdk.Texture, Gdk.Texture]) -> None:
        grid = self.grid
        pitch, row_height = grid["pitch"], grid["row_height"]
        snapshot.append_color(Gdk.RGBA(red=0, green=0, blue=0, alpha=1), rect(0, 0, self.width, self.height))
        for texture, left, key in ((textures[0], grid["left_even"], "columns_even"), (textures[1], grid["left_odd"], "columns_odd")):
            bounds = rect(left - pitch / 2, grid["top"] - row_height / 2, grid[key] * pitch, grid["rows"] * row_height)
            snapshot.append_scaled_texture(texture, Gsk.ScalingFilter.NEAREST, bounds)

    def dots(self, device_scale: float, tint: tuple[float, float, float]) -> Gsk.RenderNode | None:
        """The dots in `tint`, with edges anti-aliased at `device_scale`."""
        if not self.frames:
            return None
        grid, style = self.grid, self.style
        snapshot = Gtk.Snapshot()
        # alpha = (size - distance) in device pixels, centred on the edge.
        gain = style["max_radius"] * device_scale
        matrix = Graphene.Matrix().init_from_float([0, 0, 0, gain, 0, 0, 0, -gain, 0, 0, 0, 0, 0, 0, 0, 0])
        snapshot.push_color_matrix(matrix, Graphene.Vec4().init(tint[0], tint[1], tint[2], 0.5))
        snapshot.push_blend(Gsk.BlendMode.SCREEN)
        older, newer = self.frames[0], self.frames[-1]
        blend = min(1.0, (time.monotonic() - self.arrived) / self.interval) if older is not newer else 1.0
        snapshot.push_cross_fade(blend)
        self._sizes(snapshot, older)
        snapshot.pop()
        self._sizes(snapshot, newer)
        snapshot.pop()
        snapshot.pop()
        tile = rect(grid["left_even"] - grid["pitch"] / 2, grid["top"] - grid["row_height"] / 2, grid["pitch"], 2 * grid["row_height"])
        snapshot.push_repeat(rect(0, 0, self.width, self.height), tile)
        snapshot.append_texture(self.tile, tile)
        snapshot.pop()
        snapshot.pop()
        snapshot.pop()
        return snapshot.to_node()

    def snapshot(self, snapshot: Gtk.Snapshot, device_scale: float, look: Look) -> None:
        dots = self.dots(device_scale, look.tint)
        if dots is None:
            return
        bounds = rect(0, 0, self.width, self.height)
        snapshot.push_mask(Gsk.MaskMode.ALPHA)
        snapshot.append_texture(self.feather, bounds)
        snapshot.pop()
        snapshot.append_node(dots)
        if look.band_alpha > 0.002:
            # The sweep: a band of colour seen only through the dots.
            snapshot.push_mask(Gsk.MaskMode.ALPHA)
            snapshot.append_node(dots)
            snapshot.pop()
            r, g, b = look.band_color
            stops = []
            for offset, alpha in ((0.0, 0.0), (0.5, look.band_alpha), (1.0, 0.0)):
                stop = Gsk.ColorStop()
                stop.offset, stop.color = offset, Gdk.RGBA(red=r, green=g, blue=b, alpha=alpha)
                stops.append(stop)
            middle = look.band_middle
            snapshot.append_linear_gradient(
                rect(0, middle - 36, self.width, 72),
                Graphene.Point().init(0, middle - 36), Graphene.Point().init(0, middle + 36), stops,
            )
            snapshot.pop()
        snapshot.pop()
