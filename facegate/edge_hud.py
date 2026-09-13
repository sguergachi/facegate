#!/usr/bin/env python3
"""A true Wayland layer-shell HUD for FaceGate.

Unlike an xdg-shell application window, this surface is anchored by KWin to
the physical top edge of the monitor.  The only visible pixels are the black
island itself; the rest of the small layer surface stays transparent and is
excluded from input.

Motion is driven by the monitor's frame clock (not a 60 Hz timer), with
spring physics for the island's shape and short, staggered fades for its
content, so every frame on a high-refresh display is a real step.
"""

from __future__ import annotations

import argparse
import base64
import math
import os
import socket
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gtk4LayerShell", "1.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, Gio, GLib, Graphene, Gsk, Gtk, Gtk4LayerShell, Pango  # noqa: E402

from .protocol import MAX_MESSAGE_BYTES, MAX_STREAM_BYTES, PREVIEW_HEIGHT, PREVIEW_WIDTH, decode, encode

SOCKET_NAME = "facegate.sock"
SCAN_WATCHDOG_MS = 20_000

# Layout contract (see DESIGN.md).  Everything lives on a 4/8-point grid and
# the bottom corners are concentric with the pill buttons:
# ISLAND_RADIUS = BUTTON_HEIGHT / 2 + INSET.
ISLAND_WIDTH = 344
INSET = 16
TOP_INSET = 24
MARK_SIZE = 56
MARK_BLEED = 8
MARK_TO_TITLE = 16
TITLE_TO_DETAIL = 4
COMMAND_GAP = 8
COPY_TO_ACTIONS = 24
FIELD_GAP = 16
BUTTON_HEIGHT = 44
BUTTON_GAP = 8
STATUS_BOTTOM = 28
ISLAND_RADIUS = BUTTON_HEIGHT // 2 + INSET
SHOULDER = 10
COLLAPSED_WIDTH = 124
CONTENT_WIDTH = ISLAND_WIDTH - 2 * INSET
TEXT_WIDTH = ISLAND_WIDTH - 2 * 32

# The surface leaves room for the concave shoulders and spring overshoot.
SURFACE_WIDTH = ISLAND_WIDTH + 2 * SHOULDER + 24
SURFACE_HEIGHT = 440

WHITE = (0.96, 0.96, 0.97)


def default_socket_path() -> Path:
    if os.environ.get("FACEGATE_SOCKET"):
        # Lets a checkout's HUD and Settings talk without touching the installed one.
        return Path(os.environ["FACEGATE_SOCKET"])
    return Path(os.environ.get("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid())) / SOCKET_NAME


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def mix(first: float, second: float, amount: float) -> float:
    return first + (second - first) * amount


def cubic_bezier(x1: float, y1: float, x2: float, y2: float) -> Callable[[float], float]:
    """A CSS cubic-bezier timing function: progress in, eased progress out."""

    def sample(a: float, b: float, t: float) -> float:
        return 3 * a * (1 - t) ** 2 * t + 3 * b * (1 - t) * t * t + t ** 3

    def curve(progress: float) -> float:
        if progress <= 0.0 or progress >= 1.0:
            return clamp(progress)
        # Solve x(t) = progress by bisection; the curves are monotonic in x.
        low, high = 0.0, 1.0
        for _ in range(24):
            middle = (low + high) / 2
            if sample(x1, x2, middle) < progress:
                low = middle
            else:
                high = middle
        return sample(y1, y2, (low + high) / 2)

    return curve


# Emil Kowalski's strong UI curves: built-in easings are too weak to feel
# intentional, and ease-in delays the very moment someone is watching.
EASE_OUT = cubic_bezier(0.23, 1, 0.32, 1)
EASE_IN_OUT = cubic_bezier(0.77, 0, 0.175, 1)
EASE_OUT_CSS = "cubic-bezier(0.23, 1, 0.32, 1)"


def reduced_motion() -> bool:
    """Plasma's "Animation speed: Instant" turns GTK animations off.

    Reduced motion means gentler, not none: keep fades and colour changes,
    drop travel, scaling and bounce.
    """
    settings = Gtk.Settings.get_default()
    return bool(settings) and not settings.get_property("gtk-enable-animations")


def smoothstep(progress: float) -> float:
    return progress * progress * (3 - 2 * progress)


class Spring:
    """A damped spring parameterised like SwiftUI: response and damping ratio."""

    def __init__(self, value: float) -> None:
        self.value = value
        self.target = value
        self.velocity = 0.0
        self.stiffness = 0.0
        self.damping = 0.0
        self.configure(0.5, 1.0)

    def configure(self, response: float, damping_ratio: float) -> None:
        self.stiffness = (math.tau / response) ** 2
        self.damping = 4 * math.pi * damping_ratio / response

    def step(self, elapsed: float) -> None:
        while elapsed > 0:
            dt = min(elapsed, 1 / 480)
            force = -self.stiffness * (self.value - self.target) - self.damping * self.velocity
            self.velocity += force * dt
            self.value += self.velocity * dt
            elapsed -= dt


class Tween:
    """A delayed, eased scalar used for content opacity."""

    def __init__(self, value: float = 0.0) -> None:
        self.origin = value
        self.target = value
        self.start = 0.0
        self.duration = 0.0

    def to(self, target: float, now: float, duration: float, delay: float = 0.0) -> None:
        self.origin = self.value(now)
        self.target = target
        self.start = now + delay
        self.duration = duration

    def value(self, now: float) -> float:
        if self.duration <= 0 or now >= self.start + self.duration:
            return self.target
        if now <= self.start:
            return self.origin
        # Appearing and leaving both start fast: ease-in is never used on UI.
        return mix(self.origin, self.target, EASE_OUT((now - self.start) / self.duration))

    def done(self, now: float) -> bool:
        return self.duration <= 0 or now >= self.start + self.duration


def island_path(context: Any, center: float, width: float, height: float) -> None:
    """A notch flush with the top edge: concave shoulders, round lower corners."""
    if height <= 0.01 or width <= 0.01:
        return
    half = width / 2
    shoulder = min(SHOULDER, height / 3)
    radius = min(ISLAND_RADIUS, half, max(0.0, height - shoulder))
    left, right = center - half, center + half
    context.new_path()
    context.move_to(left - shoulder, 0)
    context.line_to(right + shoulder, 0)
    context.arc_negative(right + shoulder, shoulder, shoulder, -math.pi / 2, -math.pi)
    context.line_to(right, height - radius)
    context.arc(right - radius, height - radius, radius, 0, math.pi / 2)
    context.line_to(left + radius, height)
    context.arc(left + radius, height - radius, radius, math.pi / 2, math.pi)
    context.line_to(left, shoulder)
    context.arc_negative(left - shoulder, shoulder, shoulder, 0, -math.pi / 2)
    context.close_path()


MARK_STATE_SECONDS = 0.55
RING_SECTORS = 72

# r, g, b, idle ring alpha, arc length (turns), arc alpha, solidity (0 = comet
# tail, 1 = even ring), smile, eye openness, spin (turns/s)
MARK_PROFILES = {
    "approval": (*WHITE, 0.16, 0.0, 0.0, 0.0, 0.55, 1.0, 0.0),
    "password": (*WHITE, 0.16, 0.0, 0.0, 0.0, 0.3, 1.0, 0.0),
    "scanning": (0.39, 0.82, 1.00, 0.08, 0.28, 1.0, 0.0, 0.2, 0.92, 0.85),
    "success": (0.19, 0.82, 0.35, 0.0, 1.0, 1.0, 1.0, 1.0, 0.9, 0.0),
    "failure": (1.00, 0.27, 0.23, 0.0, 1.0, 0.9, 1.0, -0.3, 0.85, 0.0),
}


@dataclass
class MarkModel:
    """Everything needed to paint one frame of the presence mark."""

    state: str = "approval"
    # The values on screen when the state last changed, so a new state always
    # blends from exactly what was visible, even mid-transition.
    origin: tuple[float, ...] | None = None
    transition: float = 1.0
    elapsed: float = 0.0
    state_elapsed: float = 10.0
    spin: float = 0.0


def mark_values(model: MarkModel) -> tuple[float, ...]:
    target = MARK_PROFILES[model.state]
    origin = model.origin or target
    amount = smoothstep(clamp(model.transition))
    return tuple(mix(before, after, amount) for before, after in zip(origin, target))


def ring_head(spin: float, arc_length: float) -> float:
    """The arc is anchored at its tail and grows forward from it.

    Screen coordinates point down, so increasing angles run clockwise: the
    scan spins clockwise and a growing arc closes the ring clockwise too.
    """
    tail = spin * math.tau - math.pi / 2
    return tail + min(arc_length, 1.0) * math.tau


def ring_pattern(color: tuple[float, float, float], head: float, length: float, alpha: float, solidity: float, radius: float) -> Any:
    """A conic gradient along the ring, brightest at the head.

    One continuous mesh means one stroke: there are no segment joints, and a
    comet that grows to a full, even ring is the same drawing throughout.
    """
    def opacity(behind: float) -> float:
        if length <= 0:
            return 0.0
        comet = clamp(1 - behind / length) ** 1.8
        # The tail edge glides continuously instead of stepping by sector.
        edge = clamp((length - behind) * RING_SECTORS + 1)
        return alpha * mix(comet, 1.0, solidity) * edge

    inner, outer = radius - 3, radius + 3
    mesh = cairo.MeshPattern()
    previous_angle, previous_alpha = head, opacity(0.0)
    for index in range(1, RING_SECTORS + 1):
        behind = index / RING_SECTORS
        angle = head - behind * math.tau
        value = opacity(behind) if index < RING_SECTORS or length >= 1 else 0.0
        mesh.begin_patch()
        mesh.move_to(math.cos(previous_angle) * outer, math.sin(previous_angle) * outer)
        mesh.line_to(math.cos(angle) * outer, math.sin(angle) * outer)
        mesh.line_to(math.cos(angle) * inner, math.sin(angle) * inner)
        mesh.line_to(math.cos(previous_angle) * inner, math.sin(previous_angle) * inner)
        for corner, corner_alpha in ((0, previous_alpha), (1, value), (2, value), (3, previous_alpha)):
            mesh.set_corner_color_rgba(corner, *color, corner_alpha)
        mesh.end_patch()
        previous_angle, previous_alpha = angle, value
    return mesh


def paint_mark(context: Any, size: float, model: MarkModel) -> None:
    """Paint a friendly abstract sensing mark: a lens ring with two lights."""
    r, g, b, ring_alpha, arc_length, arc_alpha, solidity, smile, eyes, _spin = mark_values(model)
    color = (r, g, b)
    face = tuple(mix(WHITE[index], color[index], 0.3) for index in range(3))
    unit = MARK_SIZE + 2 * MARK_BLEED
    context.save()
    context.scale(size / unit, size / unit)

    shake = 0.0
    scale = 1.0 + 0.012 * math.sin(model.elapsed * math.tau / 3.4)
    if model.state == "failure":
        shake = 4.0 * math.sin(model.state_elapsed * math.tau * 5.5) * math.exp(-model.state_elapsed * 6.5)
    elif model.state == "success":
        # The pulse lands as the ring closes, not before.
        scale += 0.04 * math.sin(math.pi * clamp((model.state_elapsed - 0.32) / 0.45))
    context.translate(unit / 2 + shake, unit / 2)
    context.scale(scale, scale)

    ring = MARK_SIZE / 2 - 2
    if ring_alpha > 0.005:
        context.arc(0, 0, ring, 0, math.tau)
        context.set_line_width(1.5)
        context.set_source_rgba(*color, ring_alpha)
        context.stroke()
    if arc_alpha > 0.005 and arc_length > 0.002:
        head = ring_head(model.spin, arc_length)
        context.set_source(ring_pattern(color, head, min(arc_length, 1.0), arc_alpha, solidity, ring))
        context.set_line_width(2.5)
        context.arc(0, 0, ring, 0, math.tau)
        context.stroke()
        # A rounded head, fading away as the ring becomes even.
        cap = arc_alpha * (1 - solidity) * clamp(arc_length * 20)
        if cap > 0.005:
            context.arc(math.cos(head) * ring, math.sin(head) * ring, 1.25, 0, math.tau)
            context.set_source_rgba(*color, cap)
            context.fill()

    blink = 1.0
    phase = (model.elapsed % 4.6) / 4.6
    if 0.9 < phase < 0.95:
        # Results settle the eyes open smoothly rather than snapping mid-blink.
        blink = mix(max(0.1, abs(phase - 0.925) / 0.025), 1.0, solidity)
    look = math.sin(model.elapsed * 1.7) * 1.4 * arc_alpha * (1 - arc_length)
    for eye_x in (-6.5, 6.5):
        context.save()
        context.translate(eye_x + look, -4)
        context.scale(1.0, max(0.12, eyes * blink))
        context.arc(0, 0, 2.6, 0, math.tau)
        context.set_source_rgba(*face, 1.0)
        context.fill()
        context.restore()
    context.set_line_cap(cairo.LINE_CAP_ROUND)
    context.move_to(-7 + look * 0.5, 6)
    context.curve_to(-3.5 + look * 0.5, 6 + smile * 5, 3.5 + look * 0.5, 6 + smile * 5, 7 + look * 0.5, 6)
    context.set_line_width(2.0)
    context.set_source_rgba(*face, 1.0)
    context.stroke()
    context.restore()


class PresenceMark(Gtk.DrawingArea):
    def __init__(self) -> None:
        super().__init__()
        size = MARK_SIZE + 2 * MARK_BLEED
        self.set_content_width(size)
        self.set_content_height(size)
        self.model = MarkModel()
        self.set_draw_func(lambda _area, context, width, _height: paint_mark(context, width, self.model))

    def set_state(self, state: str, animate: bool = True) -> None:
        model = self.model
        if state == model.state:
            return
        model.origin = mark_values(model) if animate else None
        model.state = state
        model.transition = 0.0 if animate else 1.0
        model.state_elapsed = 0.0

    def advance(self, elapsed: float) -> None:
        model = self.model
        speed = mark_values(model)[-1]
        model.elapsed += elapsed
        model.state_elapsed += elapsed
        model.spin = (model.spin + speed * elapsed) % 1.0
        model.transition = min(1.0, model.transition + elapsed / MARK_STATE_SECONDS)
        self.queue_draw()


# Enrollment preview ---------------------------------------------------------

PREVIEW_RADIUS = 30
PREVIEW_FEATHER = 36
PREVIEW_TO_TITLE = 16
# The progress trace outlines the rim, where the image has faded to nothing;
# the bleed leaves room for its glow.
TRACE_INSET = 2
PREVIEW_BLEED = 6
# Lit IR frames often arrive at half the camera rate; each fades into the next.
FRAME_BLEND_SECONDS = 0.07
IR_TINT = (0.84, 0.94, 1.0)
PREVIEW_COLORS = {"scanning": MARK_PROFILES["scanning"][:3], "success": MARK_PROFILES["success"][:3], "failure": MARK_PROFILES["failure"][:3]}

_feather_masks: dict[tuple[int, int], cairo.ImageSurface] = {}


def feather_mask(width: int, height: int) -> cairo.ImageSurface:
    """An A8 rounded rectangle whose last PREVIEW_FEATHER pixels fade to nothing."""
    key = (width, height)
    if key in _feather_masks:
        return _feather_masks[key]
    stride = cairo.ImageSurface.format_stride_for_width(cairo.FORMAT_A8, width)
    data = bytearray(stride * height)
    radius = min(PREVIEW_RADIUS, width / 2, height / 2)
    half_width, half_height = width / 2 - radius, height / 2 - radius
    for y in range(height):
        qy = abs(y + 0.5 - height / 2) - half_height
        row = y * stride
        for x in range(width):
            qx = abs(x + 0.5 - width / 2) - half_width
            # Signed distance to the rounded rectangle, negative inside.
            outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
            edge = outside + min(max(qx, qy), 0.0) - radius
            data[row + x] = round(255 * smoothstep(clamp(-edge / PREVIEW_FEATHER)))
    mask = cairo.ImageSurface.create_for_data(data, cairo.FORMAT_A8, width, height, stride)
    _feather_masks[key] = mask
    return mask


def grey_surface(pixels: bytes, width: int, height: int) -> cairo.ImageSurface:
    """Wrap 8-bit greyscale rows as an A8 surface: luminance becomes coverage."""
    stride = cairo.ImageSurface.format_stride_for_width(cairo.FORMAT_A8, width)
    if len(pixels) != width * height:
        raise ValueError("frame size does not match its dimensions")
    if stride == width:
        data = bytearray(pixels)
    else:
        padding = bytes(stride - width)
        data = bytearray(b"".join(pixels[row * width:(row + 1) * width] + padding for row in range(height)))
    return cairo.ImageSurface.create_for_data(data, cairo.FORMAT_A8, width, height, stride)


def rounded_rect_path(context: Any, x: float, y: float, width: float, height: float, radius: float) -> float:
    """A clockwise rounded rectangle starting at top center; returns its length."""
    radius = max(0.0, min(radius, width / 2, height / 2))
    right, bottom = x + width, y + height
    context.new_path()
    context.move_to(x + width / 2, y)
    context.line_to(right - radius, y)
    context.arc(right - radius, y + radius, radius, -math.pi / 2, 0)
    context.line_to(right, bottom - radius)
    context.arc(right - radius, bottom - radius, radius, 0, math.pi / 2)
    context.line_to(x + radius, bottom)
    context.arc(x + radius, bottom - radius, radius, math.pi / 2, math.pi)
    context.line_to(x, y + radius)
    context.arc(x + radius, y + radius, radius, math.pi, 3 * math.pi / 2)
    context.close_path()
    return 2 * (width - 2 * radius) + 2 * (height - 2 * radius) + math.tau * radius


@dataclass
class PreviewModel:
    """One frame of the enrollment preview."""

    state: str = "scanning"
    progress: float = 0.0
    # 0 while scanning, rising to 1 as the result colour takes over.
    result: float = 0.0
    # A brief brightening each time a sample is captured.
    pulse: float = 0.0
    elapsed: float = 0.0
    state_elapsed: float = 0.0
    # How far the newest frame has faded in over the one before it.
    blend: float = 1.0


def paint_preview(context: Any, width: float, height: float, frame: cairo.ImageSurface | None, model: PreviewModel, previous: cairo.ImageSurface | None = None) -> None:
    """The IR image feathered into a rounded rectangle, and the progress trace."""
    color = PREVIEW_COLORS.get(model.state, PREVIEW_COLORS["scanning"])
    context.save()
    if model.state == "failure":
        context.translate(6 * math.sin(model.state_elapsed * math.tau * 5.5) * math.exp(-model.state_elapsed * 6.5), 0)
    if frame is not None:
        glow = 0.9 + 0.1 * model.pulse
        tint = tuple(mix(IR_TINT[index], color[index], 0.25 * model.result) * glow for index in range(3))
        context.push_group()
        # Adding weighted frames is an exact cross-fade of their luminance.
        context.set_operator(cairo.OPERATOR_ADD)
        blend = clamp(model.blend) if previous is not None else 1.0
        for image, weight in ((previous, 1 - blend), (frame, blend)):
            if image is None or weight <= 0.001:
                continue
            context.save()
            context.scale(width / image.get_width(), height / image.get_height())
            pattern = cairo.SurfacePattern(image)
            pattern.set_filter(cairo.FILTER_GOOD)
            context.set_source_rgba(*tint, weight)
            context.mask(pattern)
            context.restore()
        context.set_operator(cairo.OPERATOR_OVER)
        if model.pulse > 0.01:
            context.set_source_rgba(*color, 0.1 * model.pulse)
            context.paint()
        context.pop_group_to_source()
        context.mask_surface(feather_mask(round(width), round(height)), 0, 0)

    length = rounded_rect_path(context, TRACE_INSET, TRACE_INSET, width - 2 * TRACE_INSET, height - 2 * TRACE_INSET, PREVIEW_RADIUS - TRACE_INSET)
    context.set_line_cap(cairo.LINE_CAP_ROUND)
    searching = (1 - model.progress) * (1 - model.result)
    if searching > 0.01:
        # A short comet circles until the first sample lands.
        phase = (model.elapsed * 0.4) % 1.0
        context.set_dash([length * 0.12, length * 0.88], (1 - phase) * length)
        context.set_source_rgba(*color, 0.55 * searching)
        context.set_line_width(2.0)
        context.stroke_preserve()
    progress = max(model.progress, model.result if model.state == "success" else 0.0)
    if progress > 0.002:
        # Captured samples fill the rim clockwise from the top.
        context.set_dash([] if progress >= 0.999 else [length * progress, length * 2], 0)
        for line_width, alpha in ((7.0, 0.14 + 0.22 * model.pulse), (2.0, 0.95)):
            context.set_line_width(line_width)
            context.set_source_rgba(*color, alpha)
            context.stroke_preserve()
    context.new_path()
    context.restore()


class PreviewArea(Gtk.DrawingArea):
    def __init__(self) -> None:
        super().__init__()
        self.set_content_width(PREVIEW_WIDTH + 2 * PREVIEW_BLEED)
        self.set_content_height(PREVIEW_HEIGHT + 2 * PREVIEW_BLEED)
        self.model = PreviewModel()
        self.progress = Spring(0.0)
        self.progress.configure(0.45, 0.9)
        # Written by the socket thread, picked up on the frame clock.
        self.latest: cairo.ImageSurface | None = None
        self.shown: cairo.ImageSurface | None = None
        self.previous: cairo.ImageSurface | None = None
        self.set_draw_func(self._draw)

    def _draw(self, _area: Gtk.DrawingArea, context: Any, _width: int, _height: int) -> None:
        context.translate(PREVIEW_BLEED, PREVIEW_BLEED)
        paint_preview(context, PREVIEW_WIDTH, PREVIEW_HEIGHT, self.shown, self.model, self.previous)

    def reset(self) -> None:
        self.model = PreviewModel()
        self.progress.value = self.progress.target = self.progress.velocity = 0.0
        self.latest = self.shown = self.previous = None

    def set_progress(self, value: float) -> None:
        value = clamp(value)
        if value > self.progress.target:
            self.model.pulse = 1.0
        self.progress.target = value

    def set_state(self, state: str) -> None:
        if state != self.model.state:
            self.model.state = state
            self.model.state_elapsed = 0.0
            if state == "success":
                self.progress.target = 1.0
                self.model.pulse = 1.0

    def advance(self, elapsed: float) -> None:
        model = self.model
        latest = self.latest
        if latest is not self.shown:
            self.previous, self.shown = self.shown, latest
            model.blend = 0.0
        model.blend = min(1.0, model.blend + elapsed / FRAME_BLEND_SECONDS)
        model.elapsed += elapsed
        model.state_elapsed += elapsed
        self.progress.step(elapsed)
        model.progress = clamp(self.progress.value)
        model.pulse = max(0.0, model.pulse - elapsed / 0.45)
        target = 0.0 if model.state == "scanning" else 1.0
        model.result += (target - model.result) * (1 - math.exp(-elapsed * 8))
        self.queue_draw()


@dataclass(frozen=True)
class View:
    state: str
    title: str
    detail: str
    actions: bool
    command: str = ""
    password: bool = False
    primary: str = "Approve"
    secondary: str = "Deny"
    # Enrollment shows the live camera preview in place of the mark.
    preview: bool = False


APPROVAL_HINT = "Approve to verify with your face."
APPROVAL = View("approval", "Verify with Face", APPROVAL_HINT, True)


def _display_text(value: object, limit: int) -> str:
    text = "".join(char if char.isprintable() else " " for char in str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


PASSWORD_HINT = "Enter your password to allow this."
PASSWORD_ERROR = "Incorrect password. Try again."


def _request_copy(requester: object) -> tuple[str, str, list[str]]:
    """Title, command, and context lines shared by the approval and password views."""
    if not isinstance(requester, dict):
        return "Verify with Face", "", []
    app = _display_text(requester.get("app"), 32)
    host = _display_text(requester.get("host"), 32)
    command = _display_text(requester.get("command"), 160)
    # polkit explains itself ("Authentication is required to ...") when there is no command.
    message = "" if command else _display_text(requester.get("message"), 120)
    wants = "admin access" if requester.get("service") == "polkit-1" else "sudo"
    if app:
        title = "%s wants %s" % (app, wants)
    elif wants == "sudo" and not command:
        title = "Verify with Face"
    else:
        title = "%s requested" % wants.capitalize()
    lines = (["via %s" % host] if host else []) + ([message] if message else [])
    return title, command, lines


def password_view(requester: object, error: bool = False) -> View:
    title, command, lines = _request_copy(requester)
    if title == "Verify with Face":
        title = "Password required"
    detail = "\n".join(lines + [PASSWORD_ERROR if error else PASSWORD_HINT])
    return View("password", title, detail, True, command, password=True, primary="Allow", secondary="Cancel")


def approval_view(requester: object) -> View:
    """Say who is asking, as reported by the root-side PAM adapter.

    This is informational: a process can rename itself, so the command and
    app name help the user decide, but never grant anything.
    """
    if not isinstance(requester, dict):
        return APPROVAL
    title, command, lines = _request_copy(requester)
    if title == "Verify with Face" and not lines:
        return APPROVAL
    # Apple-style: say who is asking, then how approving allows it.
    return View("approval", title, "\n".join(lines + [APPROVAL_HINT]), True, command)


SCANNING = View("scanning", "Looking for you", "Hold still for a moment.", False)
SUCCESS = View("success", "Verified", "Continuing securely.", False)
FAILURE = View("failure", "Not recognized", "Use your password instead.", False)
ENROLLING = View("scanning", "Adding your face", "Waiting for the camera…", True, primary="", secondary="Cancel", preview=True)
ENROLLED = View("success", "Face added", "You can now approve with a glance.", False, preview=True)


def enroll_failed(detail: object) -> View:
    return View("failure", "Couldn't add your face", _display_text(detail, 120) or "Please try again.", False, preview=True)


@dataclass
class Enrollment:
    connection: socket.socket
    finished: bool = False


class Item:
    """One vertically stacked piece of content with its own fade."""

    def __init__(self, widget: Gtk.Widget, width: float) -> None:
        self.widget = widget
        self.width = width
        self.height = 0.0
        self.y = 0.0
        self.alpha = Tween()


@dataclass
class PendingApproval:
    connection: socket.socket
    request_id: str
    polkit: bool = False


class EdgeHud:
    def __init__(self) -> None:
        self.window = Gtk.Window()
        self.window.set_decorated(False)
        self.window.set_resizable(False)
        self.window.set_default_size(SURFACE_WIDTH, SURFACE_HEIGHT)
        Gtk4LayerShell.init_for_window(self.window)
        Gtk4LayerShell.set_layer(self.window, Gtk4LayerShell.Layer.OVERLAY)
        Gtk4LayerShell.set_anchor(self.window, Gtk4LayerShell.Edge.TOP, True)
        Gtk4LayerShell.set_exclusive_zone(self.window, -1)
        Gtk4LayerShell.set_keyboard_mode(self.window, Gtk4LayerShell.KeyboardMode.ON_DEMAND)
        self.positioned = False
        self.window.connect("map", self._position_on_mapped_monitor)

        self.pending: PendingApproval | None = None
        self.enrollment: Enrollment | None = None
        self.approval_timeout: int | None = None
        self.dismiss_timeout: int | None = None
        self.silenced = False
        self.visible = False
        self.departing = False
        self.view: View | None = None
        self.scheduled: list[tuple[float, Callable[[float], None]]] = []
        self.now = 0.0
        self.last_frame: float | None = None
        self.input_rect: tuple[int, int] | None = None
        self.width = Spring(COLLAPSED_WIDTH)
        self.height = Spring(0.0)

        self._install_css()
        overlay = Gtk.Overlay()
        self.backdrop = Gtk.DrawingArea()
        self.backdrop.set_size_request(SURFACE_WIDTH, SURFACE_HEIGHT)
        self.backdrop.set_draw_func(self._draw_island)
        overlay.set_child(self.backdrop)
        self.layer = Gtk.Fixed()
        self.layer.set_overflow(Gtk.Overflow.HIDDEN)
        overlay.add_overlay(self.layer)
        self.window.set_child(overlay)

        self.mark = PresenceMark()
        self.title = self._label("title")
        self.detail = self._label("detail")
        # The exact command, as a single ellipsized line that hugs its text.
        self.command = Gtk.Label()
        self.command.add_css_class("command")
        self.command.set_single_line_mode(True)
        self.command.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.command.set_halign(Gtk.Align.CENTER)
        command_row = Gtk.Box()
        command_row.set_size_request(CONTENT_WIDTH, -1)
        command_row.append(self.command)
        self.command.set_hexpand(True)
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=BUTTON_GAP, homogeneous=True)
        actions.set_size_request(CONTENT_WIDTH, BUTTON_HEIGHT)
        self.deny = Gtk.Button(label="Deny")
        self.deny.add_css_class("secondary")
        self.approve = Gtk.Button(label="Approve")
        self.approve.add_css_class("primary")
        for button in (self.deny, self.approve):
            button.set_size_request(-1, BUTTON_HEIGHT)
            button.set_focus_on_click(False)
            actions.append(button)
        self.approve.connect("clicked", lambda *_: self._primary())
        self.deny.connect("clicked", lambda *_: self._secondary())
        self.entry = Gtk.PasswordEntry()
        self.entry.add_css_class("password")
        self.entry.set_show_peek_icon(True)
        self.entry.set_property("placeholder-text", "Password")
        self.entry.set_size_request(CONTENT_WIDTH, BUTTON_HEIGHT)
        self.entry.connect("activate", lambda *_: self._primary())
        # Set by the polkit agent while it shows the password view.
        self.on_password: Callable[[str], None] | None = None
        self.on_password_cancel: Callable[[], None] | None = None
        self.polkit: Any = None

        self.preview = PreviewArea()
        self.preview_item = Item(self.preview, PREVIEW_WIDTH + 2 * PREVIEW_BLEED)
        self.preview_item.height = PREVIEW_HEIGHT + 2 * PREVIEW_BLEED
        self.preview_item.y = TOP_INSET - PREVIEW_BLEED
        self.mark_item = Item(self.mark, MARK_SIZE + 2 * MARK_BLEED)
        self.mark_item.height = MARK_SIZE + 2 * MARK_BLEED
        self.mark_item.y = TOP_INSET - MARK_BLEED
        self.title_item = Item(self.title, TEXT_WIDTH)
        self.detail_item = Item(self.detail, TEXT_WIDTH)
        self.command_item = Item(command_row, CONTENT_WIDTH)
        self.actions_item = Item(actions, CONTENT_WIDTH)
        self.actions_item.height = BUTTON_HEIGHT
        self.entry_item = Item(self.entry, CONTENT_WIDTH)
        self.entry_item.height = BUTTON_HEIGHT
        self.items = (self.mark_item, self.preview_item, self.title_item, self.command_item, self.detail_item, self.entry_item, self.actions_item)
        for item in self.items:
            self.layer.put(item.widget, 0, 0)
            item.widget.set_opacity(0)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.window.add_controller(keys)

    @staticmethod
    def _label(css_class: str) -> Gtk.Label:
        label = Gtk.Label()
        label.add_css_class(css_class)
        label.set_size_request(TEXT_WIDTH, -1)
        label.set_wrap(True)
        label.set_justify(Gtk.Justification.CENTER)
        label.set_xalign(0.5)
        return label

    @staticmethod
    def _monitor_under_pointer() -> Gdk.Monitor | None:
        """Wayland hides the global pointer from clients, so ask KWin which
        output is active; by default that is the one under the mouse."""
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            reply = bus.call_sync(
                "org.kde.KWin", "/KWin", "org.kde.KWin", "activeOutputName",
                None, GLib.VariantType.new("(s)"), Gio.DBusCallFlags.NONE, 200, None,
            )
        except GLib.Error:
            return None
        connector = reply.unpack()[0]
        monitors = Gdk.Display.get_default().get_monitors()
        for index in range(monitors.get_n_items()):
            monitor = monitors.get_item(index)
            if monitor.get_connector() == connector:
                return monitor
        return None

    def _pin_to(self, monitor: Gdk.Monitor) -> None:
        """Pin the layer surface to the true center of the monitor's top edge."""
        geometry = monitor.get_geometry()
        margin = max(0, (geometry.width - SURFACE_WIDTH) // 2)
        Gtk4LayerShell.set_monitor(self.window, monitor)
        Gtk4LayerShell.set_anchor(self.window, Gtk4LayerShell.Edge.LEFT, True)
        Gtk4LayerShell.set_margin(self.window, Gtk4LayerShell.Edge.LEFT, margin)
        self.positioned = True
        print("[facegate] showing on %s (%dx%d, left margin %d)" % (monitor.get_connector(), geometry.width, geometry.height, margin), flush=True)

    def _position_on_mapped_monitor(self, *_args: Any) -> None:
        """Fallback without KWin: center on whichever output the compositor chose."""
        if self.positioned:
            return
        surface = self.window.get_surface()
        monitor = Gdk.Display.get_default().get_monitor_at_surface(surface) if surface else None
        if not monitor:
            GLib.idle_add(self._position_on_mapped_monitor)
            return
        self._pin_to(monitor)

    @staticmethod
    def _install_css() -> None:
        css = Gtk.CssProvider()
        css.load_from_string("""
            window, overlay, fixed {
                background: transparent;
                background-image: none;
                box-shadow: none;
            }
            label {
                font-family: "Inter", "SF Pro Text", sans-serif;
                font-feature-settings: "tnum";
            }
            .title { color: #f5f5f7; font-size: 17px; font-weight: 600; letter-spacing: -0.2px; }
            .detail { color: #98989d; font-size: 13px; font-weight: 400; }
            .command {
                font-family: "JetBrains Mono", "Fira Code", monospace;
                font-feature-settings: "calt" 0;
                font-size: 12px;
                color: #f5f5f7;
                background-color: #1c1c1e;
                border-radius: 8px;
                padding: 4px 10px;
            }
            button {
                min-height: 44px;
                padding: 0 16px;
                border: none;
                border-radius: 22px;
                outline: none;
                box-shadow: none;
                background-image: none;
                text-shadow: none;
                font-size: 15px;
                font-weight: 600;
                transition: background-color 180ms ease, color 180ms ease, transform 160ms cubic-bezier(0.23, 1, 0.32, 1);
            }
            /* Press feedback: the button gives under the pointer. */
            button:active { transform: scale(0.97); }
            button.primary { background-color: #f5f5f7; }
            button.primary label { color: #000000; }
            button.primary:hover { background-color: #ffffff; }
            button.primary:active { background-color: #d1d1d6; }
            button.secondary { background-color: #1c1c1e; }
            button.secondary label { color: #f5f5f7; }
            button.secondary:hover { background-color: #2c2c2e; }
            button.secondary:active { background-color: #3a3a3c; }
            button:focus-visible { outline: 2px solid rgba(255, 255, 255, 0.4); outline-offset: 2px; }
            passwordentry.password {
                min-height: 44px;
                padding: 0 14px;
                border: none;
                border-radius: 12px;
                outline: none;
                box-shadow: none;
                background-color: #1c1c1e;
                color: #f5f5f7;
                font-size: 15px;
                transition: box-shadow 180ms ease;
            }
            passwordentry.password:focus-within { box-shadow: inset 0 0 0 1.5px rgba(255, 255, 255, 0.28); }
            passwordentry.password text { color: #f5f5f7; caret-color: #f5f5f7; }
            passwordentry.password text placeholder { color: #6e6e73; }
            passwordentry.password image { color: #8e8e93; }
        """)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    # Layout ---------------------------------------------------------------

    def _layout(self, view: View) -> float:
        """Apply copy and return the island height that exactly fits it."""
        self.title.set_text(view.title)
        self.detail.set_text(view.detail)
        self.title_item.height = self.title.measure(Gtk.Orientation.VERTICAL, TEXT_WIDTH)[1]
        self.detail_item.height = self.detail.measure(Gtk.Orientation.VERTICAL, TEXT_WIDTH)[1]
        self.title_item.y = TOP_INSET + (PREVIEW_HEIGHT + PREVIEW_TO_TITLE if view.preview else MARK_SIZE + MARK_TO_TITLE)
        copy_bottom = self.title_item.y + self.title_item.height
        if view.command:
            self.command.set_text(view.command)
            self.command.set_tooltip_text(view.command)
            self.command_item.height = self.command_item.widget.measure(Gtk.Orientation.VERTICAL, CONTENT_WIDTH)[1]
            self.command_item.y = copy_bottom + COMMAND_GAP
            copy_bottom = self.command_item.y + self.command_item.height
        if view.detail:
            self.detail_item.y = copy_bottom + (COMMAND_GAP if view.command else TITLE_TO_DETAIL)
            copy_bottom = self.detail_item.y + self.detail_item.height
        self.approve.set_label(view.primary)
        self.approve.set_visible(bool(view.primary))
        self.deny.set_label(view.secondary)
        if view.password:
            self.entry_item.y = copy_bottom + FIELD_GAP
            self.actions_item.y = self.entry_item.y + BUTTON_HEIGHT + FIELD_GAP
            return self.actions_item.y + BUTTON_HEIGHT + INSET
        self.actions_item.y = copy_bottom + COPY_TO_ACTIONS
        if view.actions:
            return self.actions_item.y + BUTTON_HEIGHT + INSET
        return copy_bottom + STATUS_BOTTOM

    def _place(self, item: Item) -> None:
        alpha = item.alpha.value(self.now)
        # Content settles downward into place, following the island's motion.
        travel = 0.0 if reduced_motion() else 1 - alpha
        offset = travel * -8
        scale = 1 - 0.04 * travel
        x = (SURFACE_WIDTH - item.width) / 2
        transform = (
            Gsk.Transform()
            .translate(Graphene.Point().init(x + item.width / 2, item.y + offset + item.height / 2))
            .scale(scale, scale)
            .translate(Graphene.Point().init(-item.width / 2, -item.height / 2))
        )
        self.layer.set_child_transform(item.widget, transform)
        # Content is revealed by the island, never drawn over the desktop.
        covered_down = clamp((self.height.value - item.y - item.height) / 12 + 1)
        covered_across = clamp((self.width.value - item.width) / 32 + 1)
        item.widget.set_opacity(alpha * covered_down * covered_across)

    # Choreography -----------------------------------------------------------

    def _at(self, delay: float, action: Callable[[float], None]) -> None:
        self.scheduled.append((self.now + delay, action))

    def _present(self, view: View) -> None:
        if self.dismiss_timeout:
            GLib.source_remove(self.dismiss_timeout)
            self.dismiss_timeout = None
        self.approve.set_sensitive(view.actions)
        self.deny.set_sensitive(view.actions)
        self.entry.set_sensitive(view.password)
        if view.password and view != self.view:
            self.entry.set_text("")
        # Typing a password needs the keyboard without a click first.
        Gtk4LayerShell.set_keyboard_mode(
            self.window,
            Gtk4LayerShell.KeyboardMode.EXCLUSIVE if view.password else Gtk4LayerShell.KeyboardMode.ON_DEMAND,
        )
        if not self.visible:
            self._arrive(view)
            return
        if view == self.view and not self.departing:
            # A repeat of the state already showing (Approve switches to
            # scanning locally, then PAM announces scanning too) must not
            # cancel the transition that is still settling.
            return
        previous, self.view = self.view, view
        self.departing = False
        self.scheduled.clear()
        self.mark.set_state(view.state)
        self.preview.set_state(view.state)
        top, other = self._top_items(view)
        top.alpha.to(1, self.now, 0.24)
        other.alpha.to(0, self.now, 0.14)
        self.width.configure(0.5, 0.86)
        self.width.target = ISLAND_WIDTH
        copy = lambda shown: (shown.title, shown.command, shown.detail, shown.actions, shown.password, shown.primary, shown.secondary, shown.preview)
        if previous and copy(previous) == copy(view):
            # Only the mark changes (for example success after a password check).
            return
        # Outgoing copy leaves quickly; the island then reshapes around the
        # incoming copy while it settles in.
        for item in (self.title_item, self.command_item, self.detail_item, self.entry_item, self.actions_item):
            item.alpha.to(0, self.now, 0.14)

        def swap(now: float) -> None:
            self.height.configure(0.5, 0.86)
            self.height.target = self._layout(view)
            incoming = [self.title_item] + ([self.command_item] if view.command else [])
            incoming += ([self.detail_item] if view.detail else []) + ([self.entry_item] if view.password else [])
            incoming += [self.actions_item] if view.actions else []
            for index, item in enumerate(incoming):
                item.alpha.to(1, now, 0.26, index * 0.04)
            if view.password:
                self.entry.grab_focus()

        self._at(0.15, swap)

    def _top_items(self, view: View) -> tuple[Item, Item]:
        """The item heading the island for this view, and the one it replaces."""
        return (self.preview_item, self.mark_item) if view.preview else (self.mark_item, self.preview_item)

    def update_detail(self, detail: str) -> None:
        """Swap only the detail line, easing the island to its new height."""
        view = self.view
        if not view or self.departing or view.detail == detail:
            return
        self.view = replace(view, detail=detail)
        self.detail_item.alpha.to(0, self.now, 0.1)

        def swap(now: float) -> None:
            if self.view and self.view.detail == detail:
                self.height.configure(0.45, 0.9)
                self.height.target = self._layout(self.view)
                self.detail_item.alpha.to(1, now, 0.24)

        self._at(0.1, swap)

    def _arrive(self, view: View) -> None:
        self.visible = True
        self.departing = False
        self.view = view
        self.scheduled.clear()
        self.last_frame = None
        self.mark.set_state(view.state, animate=False)
        for item in self.items:
            item.alpha = Tween()
        self.width.value, self.width.velocity = COLLAPSED_WIDTH, 0.0
        self.height.value, self.height.velocity = 0.0, 0.0
        self.width.configure(0.5, 0.8)
        self.height.configure(0.56, 0.8)
        self.width.target = ISLAND_WIDTH
        self.height.target = self._layout(view)
        stagger = [self._top_items(view)[0], self.title_item] + ([self.command_item] if view.command else [])
        stagger += ([self.detail_item] if view.detail else []) + ([self.entry_item] if view.password else [])
        stagger += [self.actions_item] if view.actions else []
        for index, item in enumerate(stagger):
            item.alpha.to(1, self.now, 0.3, 0.1 + index * 0.04)
        if view.password:
            GLib.idle_add(lambda: (self.entry.grab_focus(), False)[1])
        # Follow the pointer: choose the output afresh every time we appear.
        monitor = self._monitor_under_pointer()
        if monitor:
            self._pin_to(monitor)
        else:
            self.positioned = False
        self.window.present()
        self.window.add_tick_callback(self._on_frame)

    def dismiss(self) -> bool:
        self.dismiss_timeout = None
        if not self.visible or self.departing:
            return False
        self.departing = True
        self.scheduled.clear()
        self.approve.set_sensitive(False)
        self.deny.set_sensitive(False)
        self.entry.set_sensitive(False)
        self.entry.set_text("")
        self.on_password = self.on_password_cancel = None
        Gtk4LayerShell.set_keyboard_mode(self.window, Gtk4LayerShell.KeyboardMode.ON_DEMAND)
        for item in self.items:
            item.alpha.to(0, self.now, 0.16)

        def collapse(_now: float) -> None:
            self.width.configure(0.42, 1.0)
            self.height.configure(0.4, 1.0)
            self.width.target = COLLAPSED_WIDTH
            self.height.target = 0.0

        self._at(0.08, collapse)
        return False

    def _on_frame(self, _widget: Gtk.Widget, clock: Gdk.FrameClock) -> bool:
        now = clock.get_frame_time() / 1_000_000
        if self.last_frame is None:
            # Re-base the timeline on the first real frame after presenting.
            shift = now - self.now
            self.scheduled = [(time + shift, action) for time, action in self.scheduled]
            for item in self.items:
                item.alpha.start += shift
            self.last_frame = now
        elapsed = min(now - self.last_frame, 1 / 30)
        self.last_frame = now
        self.now = now
        due = [entry for entry in self.scheduled if entry[0] <= now]
        self.scheduled = [entry for entry in self.scheduled if entry[0] > now]
        for _time, action in due:
            action(now)

        self.width.step(elapsed)
        self.height.step(elapsed)
        self.mark.advance(elapsed)
        if self.view and self.view.preview:
            self.preview.advance(elapsed)
        for item in self.items:
            self._place(item)
        self.backdrop.queue_draw()
        self._update_input_region()

        if (
            self.departing
            and not self.scheduled
            and self.height.target == 0
            and self.height.value < 0.5
            and all(item.alpha.done(now) for item in self.items)
        ):
            self.window.set_visible(False)
            self.visible = False
            self.departing = False
            self.view = None
            self.input_rect = None
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _update_input_region(self) -> None:
        width = max(0, round(self.width.value))
        height = max(0, round(self.height.value))
        if self.input_rect == (width, height):
            return
        surface = self.window.get_surface()
        if not surface:
            return
        self.input_rect = (width, height)
        rect = cairo.RectangleInt((SURFACE_WIDTH - width) // 2, 0, width, height)
        try:
            surface.set_input_region(cairo.Region(rect))
        except TypeError:
            pass

    def _draw_island(self, _area: Gtk.DrawingArea, context: Any, width: int, _height: int) -> None:
        island_path(context, width / 2, max(0.0, self.width.value), clamp(self.height.value, 0, SURFACE_HEIGHT))
        context.set_source_rgb(0.0, 0.0, 0.0)
        context.fill()

    def _on_key(self, _controller: Any, keyval: int, _keycode: int, _state: Any) -> bool:
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and (self.pending or self.on_password):
            self._primary()
            return True
        if keyval == Gdk.KEY_Escape and self.visible:
            self.cancel()
            return True
        return False

    def _primary(self) -> None:
        if self.view and self.view.password:
            if self.on_password and self.entry.get_text():
                submit, password = self.on_password, self.entry.get_text()
                self.password_busy(True)
                submit(password)
            return
        self.resolve(True)

    def _secondary(self) -> None:
        if self.view and (self.view.password or self.view.preview):
            self.cancel()
            return
        self.resolve(False)

    def present_password(self, requester: object, on_password: Callable[[str], None], on_cancel: Callable[[], None], error: bool = False) -> None:
        """The polkit fallback after Deny, a timeout, or no face match."""
        self.silenced = False
        self.on_password, self.on_password_cancel = on_password, on_cancel
        self._present(password_view(requester, error))
        self.password_busy(False)
        if error:
            # The mark shakes, then settles back to waiting for the password.
            self.mark.set_state("failure")
            GLib.timeout_add(700, lambda: (self.view and self.view.password and self.mark.set_state("password"), False)[1])

    def password_busy(self, busy: bool) -> None:
        """While PAM checks the password the field locks and the ring spins."""
        # Locking with editable rather than sensitive keeps keyboard focus in place.
        self.entry.set_editable(not busy)
        self.approve.set_sensitive(not busy)
        self.deny.set_sensitive(True)
        self.mark.set_state("scanning" if busy else "password")
        if not busy:
            self.entry.grab_focus()

    def cancel(self) -> None:
        """Close on the user's request: decline any pending approval and stay
        hidden for the rest of this attempt, even if Howdy reports back later."""
        self.silenced = True
        if self.dismiss_timeout:
            GLib.source_remove(self.dismiss_timeout)
            self.dismiss_timeout = None
        if self.enrollment:
            # Settings stops the scan; its closing the stream ends the session.
            enrollment, self.enrollment = self.enrollment, None
            if not enrollment.finished:
                self._send(enrollment.connection, {"cancel": True})
            self.dismiss()
            return
        if self.polkit and self.polkit.active:
            # Cancelling a polkit request cancels the whole authentication.
            self.polkit.cancel_active()
            return
        if self.pending:
            self.resolve(False)
        else:
            self.dismiss()

    # Protocol ---------------------------------------------------------------

    def present_approval(self, requester: object = None) -> None:
        self.silenced = False
        self._present(approval_view(requester))

    def present_scanning(self) -> None:
        # Howdy cannot be interrupted once running, so no action is offered.
        # If sudo is cancelled mid-scan no result ever arrives; never linger.
        if self.silenced:
            return
        self._present(SCANNING)
        self.dismiss_timeout = GLib.timeout_add(SCAN_WATCHDOG_MS, self.dismiss)

    def present_result(self, success: bool, title: str | None = None, detail: str | None = None) -> None:
        if self.silenced:
            return
        view = SUCCESS if success else FAILURE
        if title or detail:
            view = View(view.state, title or view.title, detail or view.detail, False)
        self._present(view)
        self.dismiss_timeout = GLib.timeout_add(1100 if success else 1600, self.dismiss)

    @staticmethod
    def reply(connection: socket.socket, payload: dict[str, Any]) -> None:
        try:
            connection.sendall(encode(payload))
        except OSError:
            # PAM may already have hung up; the HUD state must still change.
            pass
        finally:
            connection.close()

    def resolve(self, approved: bool) -> None:
        if not self.pending:
            return
        pending, self.pending = self.pending, None
        if self.approval_timeout:
            GLib.source_remove(self.approval_timeout)
            self.approval_timeout = None
        self.reply(pending.connection, {"id": pending.request_id, "approved": approved})
        if approved:
            self.present_scanning()
        elif pending.polkit and self.polkit and self.polkit.active and not self.silenced:
            # PAM moves on to the password; the agent will turn the island into
            # the password field in a moment, so don't collapse in between.
            self.dismiss_timeout = GLib.timeout_add(3_000, self.dismiss)
        else:
            self.dismiss()

    @staticmethod
    def _send(connection: socket.socket, payload: dict[str, Any]) -> None:
        try:
            connection.sendall(encode(payload))
        except OSError:
            pass

    # Enrollment: FaceGate Settings streams the scan while it adds a face.

    @property
    def enrolling(self) -> bool:
        return bool(self.enrollment and not self.enrollment.finished)

    def begin_enrollment(self, connection: socket.socket) -> bool:
        if self.pending or self.enrolling or (self.polkit and self.polkit.active):
            self.reply(connection, {"ok": False, "reason": "busy"})
            return False
        self._send(connection, {"ok": True})
        # A retry may start while the last result is still showing.
        self.enrollment = Enrollment(connection)
        self.silenced = False
        self.preview.reset()
        self._present(ENROLLING)
        return False

    def enrollment_message(self, connection: socket.socket, message: dict[str, Any]) -> bool:
        enrollment = self.enrollment
        if not enrollment or enrollment.connection is not connection or enrollment.finished:
            return False
        if "progress" in message:
            try:
                self.preview.set_progress(float(message["progress"]))
            except (TypeError, ValueError):
                pass
        if isinstance(message.get("hint"), str):
            self.update_detail(_display_text(message["hint"], 120))
        result = message.get("result")
        if result in ("success", "failure"):
            enrollment.finished = True
            self._present(ENROLLED if result == "success" else enroll_failed(message.get("detail")))
            self.dismiss_timeout = GLib.timeout_add(1_600 if result == "success" else 2_600, self._end_enrollment_view)
        return False

    def _end_enrollment_view(self) -> bool:
        if self.enrollment and self.enrollment.finished:
            self.enrollment = None
        return self.dismiss()

    def end_enrollment(self, connection: socket.socket) -> bool:
        """The stream closed. Without a result, Settings went away mid-scan."""
        enrollment = self.enrollment
        connection.close()
        if enrollment and enrollment.connection is connection and not enrollment.finished:
            self.enrollment = None
            self.dismiss()
        return False

    def _approval_timed_out(self) -> bool:
        self.approval_timeout = None
        self.resolve(False)
        return False

    def handle_request(self, message: dict[str, Any], connection: socket.socket) -> bool:
        phase = message.get("phase")
        if phase == "approval":
            if self.pending or self.enrolling:
                self.reply(connection, {"approved": False, "reason": "busy"})
                return False
            requester = message.get("requester")
            polkit = message.get("service") == "polkit-1"
            active = self.polkit.active if (polkit and self.polkit) else None
            if active is not None:
                if active.password_mode:
                    # After Deny or a wrong password, retries go straight to the password.
                    self.reply(connection, {"approved": False, "reason": "password"})
                    return False
                # The agent knows the real caller; PAM only guessed.
                requester = active.requester
            self.pending = PendingApproval(connection, str(message.get("id", "")), polkit)
            self.present_approval(requester)
            try:
                requested = int(message.get("timeout_ms", 12_000))
            except (TypeError, ValueError):
                requested = 12_000
            self.approval_timeout = GLib.timeout_add(max(1_000, min(requested, 30_000)), self._approval_timed_out)
        elif phase == "scanning":
            self.reply(connection, {"ok": True})
            self.present_scanning()
        elif phase == "result":
            self.reply(connection, {"ok": True})
            self.present_result(message.get("result") == "success")
        else:
            self.reply(connection, {"ok": False, "reason": "unknown-phase"})
        return False


class HudServer:
    def __init__(self, socket_path: Path, hud: EdgeHud) -> None:
        self.socket_path = socket_path
        self.hud = hud
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(socket_path))
        os.chmod(socket_path, 0o600)
        self.listener.listen(8)
        self.listener.settimeout(0.4)
        self.running = True
        self.thread = threading.Thread(target=self._accept_loop, name="facegate-socket", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.running = False
        self.listener.close()
        self.socket_path.unlink(missing_ok=True)

    def _accept_loop(self) -> None:
        while self.running:
            try:
                connection, _ = self.listener.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._read_one, args=(connection,), daemon=True).start()

    def _read_one(self, connection: socket.socket) -> None:
        try:
            connection.settimeout(25)
            first, rest = self._first_line(connection)
            message = decode(first)
        except (OSError, ValueError):
            connection.close()
            return
        if message.get("phase") == "enroll":
            self._stream(connection, rest)
            return
        GLib.idle_add(self.hud.handle_request, message, connection)

    @staticmethod
    def _first_line(connection: socket.socket) -> tuple[bytes, bytes]:
        buffer = b""
        while b"\n" not in buffer:
            chunk = connection.recv(MAX_MESSAGE_BYTES + 1)
            if not chunk:
                raise ValueError("connection closed before a message")
            buffer += chunk
            if len(buffer) > MAX_MESSAGE_BYTES and b"\n" not in buffer:
                raise ValueError("first message too long")
        first, rest = buffer.split(b"\n", 1)
        return first, rest

    def _stream(self, connection: socket.socket, buffer: bytes) -> None:
        """Follow one enrollment until Settings closes it.

        Frames are decoded here, off the main loop, and only the newest one is
        kept: the island draws whatever is latest on its next frame.
        """
        GLib.idle_add(self.hud.begin_enrollment, connection)
        connection.settimeout(None)
        try:
            while True:
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    message = decode(line, MAX_STREAM_BYTES)
                    if "frame" in message:
                        self._take_frame(message)
                    else:
                        GLib.idle_add(self.hud.enrollment_message, connection, message)
                if len(buffer) > MAX_STREAM_BYTES:
                    raise ValueError("stream message too long")
                chunk = connection.recv(65_536)
                if not chunk:
                    break
                buffer += chunk
        except (OSError, ValueError):
            pass
        GLib.idle_add(self.hud.end_enrollment, connection)

    def _take_frame(self, message: dict[str, Any]) -> None:
        try:
            width, height = int(message["w"]), int(message["h"])
            if (width, height) != (PREVIEW_WIDTH, PREVIEW_HEIGHT):
                return
            self.hud.preview.latest = grey_surface(base64.b64decode(message["frame"]), width, height)
        except (KeyError, TypeError, ValueError):
            pass


class Demo:
    """Loops the real request flow without PAM, Howdy, or the camera.

    Approve pretends to scan, then alternates success and failure. Use
    Deny or Escape decline. A new request follows each ending.
    """

    SCAN_MS = 2_400
    REQUESTERS = (
        {"app": "Konsole", "command": "sudo pacman -Syu"},
        {"app": "Claude Code", "host": "T3 Code", "command": "sudo systemctl restart facegate.service"},
        {"app": "python3", "host": "VS Code", "command": "sudo python3 scripts/flash_firmware.py --device /dev/ttyUSB0 --baud 115200 --verify"},
    )
    PAUSE_MS = 2_500

    def __init__(self, hud: EdgeHud) -> None:
        self.hud = hud
        self.succeed = True
        self.round = 0
        self.reader: socket.socket | None = None

    def start(self) -> None:
        print("[demo] FaceGate test mode — Approve, Deny, or Esc. Ctrl+C quits.", flush=True)
        GLib.timeout_add(600, self._request)

    def _request(self) -> bool:
        writer, self.reader = socket.socketpair()
        self.reader.setblocking(False)
        print("[demo] requesting approval", flush=True)
        requester = self.REQUESTERS[self.round % len(self.REQUESTERS)]
        self.round += 1
        self.hud.handle_request({"id": "demo", "phase": "approval", "timeout_ms": 30_000, "requester": requester}, writer)
        GLib.timeout_add(50, self._await_reply)
        return False

    def _await_reply(self) -> bool:
        try:
            raw = self.reader.recv(MAX_MESSAGE_BYTES)
        except BlockingIOError:
            return True
        self.reader.close()
        approved = bool(raw) and decode(raw.split(b"\n", 1)[0]).get("approved") is True
        if not approved:
            print("[demo] declined — next request shortly", flush=True)
            GLib.timeout_add(self.PAUSE_MS, self._request)
            return False
        print("[demo] approved — simulating a scan", flush=True)
        # Like PAM, announce scanning right after the approval reply.
        self.hud.handle_request({"phase": "scanning"}, socket.socketpair()[0])
        GLib.timeout_add(self.SCAN_MS, self._finish)
        return False

    def _finish(self) -> bool:
        success, self.succeed = self.succeed, not self.succeed
        print("[demo] result: %s" % ("success" if success else "failure"), flush=True)
        self.hud.present_result(success)
        GLib.timeout_add(self.PAUSE_MS + 1_600, self._request)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="FaceGate top-edge layer-shell HUD")
    parser.add_argument("--socket", type=Path, default=default_socket_path())
    parser.add_argument("--demo", action="store_true", help="loop the HUD flow without PAM or the camera")
    parser.add_argument("--demo-enroll", action="store_true", help="loop the Add Face scan with the real camera, saving nothing")
    parser.add_argument("--camera", help="camera for --demo-enroll (default: the first infrared one)")
    parser.add_argument("--no-polkit-agent", action="store_true", help="leave polkit prompts to the desktop's own agent")
    args = parser.parse_args()
    if args.demo_enroll:
        from .enroll_demo import main as enroll_demo

        return enroll_demo(args.camera)
    if args.demo:
        hud = EdgeHud()
        Demo(hud).start()
        try:
            return GLib.MainLoop().run() or 0
        except KeyboardInterrupt:
            return 0
    args.socket.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    args.socket.unlink(missing_ok=True)
    hud = EdgeHud()
    server = HudServer(args.socket, hud)
    server.start()
    agent = None
    if not args.no_polkit_agent:
        try:
            from .polkit_agent import FaceGatePolkitAgent

            agent = FaceGatePolkitAgent(hud)
            agent.start()
        except (ImportError, ValueError, GLib.Error) as error:
            # Without the agent, sudo still works; polkit keeps the desktop's own dialog.
            print("[facegate] polkit agent unavailable: %s" % error, flush=True)
    try:
        return GLib.MainLoop().run() or 0
    finally:
        if agent:
            agent.stop()
        server.close()


if __name__ == "__main__":
    raise SystemExit(main())
