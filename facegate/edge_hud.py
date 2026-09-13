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
import math
import os
import socket
import threading
from dataclasses import dataclass
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

from .protocol import MAX_MESSAGE_BYTES, decode, encode

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
BUTTON_HEIGHT = 44
BUTTON_GAP = 8
STATUS_BOTTOM = 28
CLOSE_SIZE = 28
ISLAND_RADIUS = BUTTON_HEIGHT // 2 + INSET
SHOULDER = 10
COLLAPSED_WIDTH = 124
CONTENT_WIDTH = ISLAND_WIDTH - 2 * INSET
TEXT_WIDTH = ISLAND_WIDTH - 2 * 32

# The surface leaves room for the concave shoulders and spring overshoot.
SURFACE_WIDTH = ISLAND_WIDTH + 2 * SHOULDER + 24
SURFACE_HEIGHT = 320

WHITE = (0.96, 0.96, 0.97)


def default_socket_path() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid())) / SOCKET_NAME


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def mix(first: float, second: float, amount: float) -> float:
    return first + (second - first) * amount


def ease_out(progress: float) -> float:
    return 1 - (1 - progress) ** 4


def ease_in(progress: float) -> float:
    return progress * progress


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
        progress = (now - self.start) / self.duration
        curve = ease_out if self.target > self.origin else ease_in
        return mix(self.origin, self.target, curve(progress))

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


@dataclass(frozen=True)
class View:
    state: str
    title: str
    detail: str
    actions: bool
    command: str = ""


APPROVAL_HINT = "Approve to verify with your face."
APPROVAL = View("approval", "Verify with Face", APPROVAL_HINT, True)


def _display_text(value: object, limit: int) -> str:
    text = "".join(char if char.isprintable() else " " for char in str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def approval_view(requester: object) -> View:
    """Say who is asking, as reported by the root-side PAM adapter.

    This is informational: a process can rename itself, so the command and
    app name help the user decide, but never grant anything.
    """
    if not isinstance(requester, dict):
        return APPROVAL
    app = _display_text(requester.get("app"), 32)
    host = _display_text(requester.get("host"), 32)
    command = _display_text(requester.get("command"), 160)
    if not app and not command:
        return APPROVAL
    title = "%s wants sudo" % app if app else "Sudo requested"
    # Apple-style: say who is asking, then how approving allows it.
    detail = "via %s\n%s" % (host, APPROVAL_HINT) if host else APPROVAL_HINT
    return View("approval", title, detail, True, command)
SCANNING = View("scanning", "Looking for you", "Hold still for a moment.", False)
SUCCESS = View("success", "Verified", "Continuing securely.", False)
FAILURE = View("failure", "Not recognized", "Use your password instead.", False)


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
        self.approve.connect("clicked", lambda *_: self.resolve(True))
        self.deny.connect("clicked", lambda *_: self.resolve(False))

        self.mark_item = Item(self.mark, MARK_SIZE + 2 * MARK_BLEED)
        self.mark_item.height = MARK_SIZE + 2 * MARK_BLEED
        self.mark_item.y = TOP_INSET - MARK_BLEED
        self.title_item = Item(self.title, TEXT_WIDTH)
        self.detail_item = Item(self.detail, TEXT_WIDTH)
        self.command_item = Item(command_row, CONTENT_WIDTH)
        self.actions_item = Item(actions, CONTENT_WIDTH)
        self.actions_item.height = BUTTON_HEIGHT
        self.items = (self.mark_item, self.title_item, self.command_item, self.detail_item, self.actions_item)
        for item in self.items:
            self.layer.put(item.widget, 0, 0)
            item.widget.set_opacity(0)

        # A quiet escape hatch that appears only while the pointer is over the
        # island, so it never competes with the primary choice.
        glyph = Gtk.DrawingArea()
        glyph.set_content_width(CLOSE_SIZE)
        glyph.set_content_height(CLOSE_SIZE)
        glyph.set_draw_func(self._draw_close_glyph)
        self.close = Gtk.Button(child=glyph)
        self.close.add_css_class("close")
        self.close.set_size_request(CLOSE_SIZE, CLOSE_SIZE)
        self.close.set_focus_on_click(False)
        self.close.set_can_target(False)
        self.close.update_property([Gtk.AccessibleProperty.LABEL], ["Close"])
        self.close.connect("clicked", lambda *_: self.cancel())
        self.close_alpha = Tween()
        self.layer.put(self.close, 0, 0)
        self.close.set_opacity(0)

        hover = Gtk.EventControllerMotion()
        # Reveal on real pointer movement, not on "enter": the island growing
        # under a resting cursor also produces an enter event.
        hover.connect("motion", lambda *_: self._set_hover(True))
        hover.connect("leave", lambda *_: self._set_hover(False))
        self.window.add_controller(hover)

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
                transition: background-color 180ms ease-out, color 180ms ease-out;
            }
            button.primary { background-color: #f5f5f7; }
            button.primary label { color: #000000; }
            button.primary:hover { background-color: #ffffff; }
            button.primary:active { background-color: #d1d1d6; }
            button.secondary { background-color: #1c1c1e; }
            button.secondary label { color: #f5f5f7; }
            button.secondary:hover { background-color: #2c2c2e; }
            button.secondary:active { background-color: #3a3a3c; }
            button:focus-visible { outline: 2px solid rgba(255, 255, 255, 0.4); outline-offset: 2px; }
            button.close {
                min-width: 28px;
                min-height: 28px;
                padding: 0;
                border-radius: 14px;
                background-color: #1c1c1e;
                color: #98989d;
            }
            button.close:hover { background-color: #2c2c2e; color: #f5f5f7; }
            button.close:active { background-color: #3a3a3c; color: #f5f5f7; }
        """)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    # Layout ---------------------------------------------------------------

    def _layout(self, view: View) -> float:
        """Apply copy and return the island height that exactly fits it."""
        self.title.set_text(view.title)
        self.detail.set_text(view.detail)
        self.title_item.height = self.title.measure(Gtk.Orientation.VERTICAL, TEXT_WIDTH)[1]
        self.detail_item.height = self.detail.measure(Gtk.Orientation.VERTICAL, TEXT_WIDTH)[1]
        self.title_item.y = TOP_INSET + MARK_SIZE + MARK_TO_TITLE
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
        self.actions_item.y = copy_bottom + COPY_TO_ACTIONS
        if view.actions:
            return self.actions_item.y + BUTTON_HEIGHT + INSET
        return copy_bottom + STATUS_BOTTOM

    def _place(self, item: Item) -> None:
        alpha = item.alpha.value(self.now)
        # Content settles downward into place, following the island's motion.
        offset = (1 - alpha) * -8
        scale = 0.96 + 0.04 * alpha
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

    def _place_close(self) -> None:
        x = (SURFACE_WIDTH + ISLAND_WIDTH) / 2 - INSET - CLOSE_SIZE
        self.layer.set_child_transform(self.close, Gsk.Transform().translate(Graphene.Point().init(x, INSET)))
        covered_down = clamp((self.height.value - INSET - CLOSE_SIZE) / 12 + 1)
        covered_across = clamp((self.width.value - ISLAND_WIDTH) / 16 + 1)
        self.close.set_opacity(self.close_alpha.value(self.now) * covered_down * covered_across)

    def _set_hover(self, hovered: bool) -> None:
        if not self.visible:
            return
        shown = hovered and not self.departing
        if shown == (self.close_alpha.target == 1.0):
            return
        self.close_alpha.to(1.0 if shown else 0.0, self.now, 0.18 if shown else 0.24, 0.0 if shown else 0.12)
        self.close.set_can_target(shown)

    @staticmethod
    def _draw_close_glyph(area: Gtk.DrawingArea, context: Any, width: int, height: int) -> None:
        color = area.get_color()
        context.set_source_rgba(color.red, color.green, color.blue, color.alpha)
        context.set_line_width(1.75)
        context.set_line_cap(cairo.LINE_CAP_ROUND)
        half = 4.5
        context.move_to(width / 2 - half, height / 2 - half)
        context.line_to(width / 2 + half, height / 2 + half)
        context.move_to(width / 2 + half, height / 2 - half)
        context.line_to(width / 2 - half, height / 2 + half)
        context.stroke()

    # Choreography -----------------------------------------------------------

    def _at(self, delay: float, action: Callable[[float], None]) -> None:
        self.scheduled.append((self.now + delay, action))

    def _present(self, view: View) -> None:
        if self.dismiss_timeout:
            GLib.source_remove(self.dismiss_timeout)
            self.dismiss_timeout = None
        self.approve.set_sensitive(view.actions)
        self.deny.set_sensitive(view.actions)
        if not self.visible:
            self._arrive(view)
            return
        previous, self.view = self.view, view
        self.departing = False
        self.scheduled.clear()
        self.mark.set_state(view.state)
        self.mark_item.alpha.to(1, self.now, 0.3)
        self.width.configure(0.5, 0.86)
        self.width.target = ISLAND_WIDTH
        if previous and (previous.title, previous.command, previous.actions) == (view.title, view.command, view.actions):
            return
        # Outgoing copy leaves quickly; the island then reshapes around the
        # incoming copy while it settles in.
        for item in (self.title_item, self.command_item, self.detail_item, self.actions_item):
            item.alpha.to(0, self.now, 0.14)

        def swap(now: float) -> None:
            self.height.configure(0.5, 0.86)
            self.height.target = self._layout(view)
            incoming = [self.title_item] + ([self.command_item] if view.command else [])
            incoming += ([self.detail_item] if view.detail else []) + ([self.actions_item] if view.actions else [])
            for index, item in enumerate(incoming):
                item.alpha.to(1, now, 0.36, index * 0.04)

        self._at(0.15, swap)

    def _arrive(self, view: View) -> None:
        self.visible = True
        self.departing = False
        self.view = view
        self.scheduled.clear()
        self.last_frame = None
        self.mark.set_state(view.state, animate=False)
        for item in self.items:
            item.alpha = Tween()
        self.close_alpha = Tween()
        self.close.set_can_target(False)
        self.width.value, self.width.velocity = COLLAPSED_WIDTH, 0.0
        self.height.value, self.height.velocity = 0.0, 0.0
        self.width.configure(0.5, 0.8)
        self.height.configure(0.56, 0.8)
        self.width.target = ISLAND_WIDTH
        self.height.target = self._layout(view)
        stagger = [self.mark_item, self.title_item] + ([self.command_item] if view.command else [])
        stagger += ([self.detail_item] if view.detail else []) + ([self.actions_item] if view.actions else [])
        for index, item in enumerate(stagger):
            item.alpha.to(1, self.now, 0.42, 0.12 + index * 0.045)
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
        self.close.set_can_target(False)
        for item in self.items:
            item.alpha.to(0, self.now, 0.16)
        self.close_alpha.to(0, self.now, 0.16)

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
            for tween in [item.alpha for item in self.items] + [self.close_alpha]:
                tween.start += shift
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
        for item in self.items:
            self._place(item)
        self._place_close()
        self.backdrop.queue_draw()
        self._update_input_region()

        if (
            self.departing
            and not self.scheduled
            and self.height.target == 0
            and self.height.value < 0.5
            and all(item.alpha.done(now) for item in self.items)
            and self.close_alpha.done(now)
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
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and self.pending:
            self.resolve(True)
            return True
        if keyval == Gdk.KEY_Escape and self.visible:
            self.cancel()
            return True
        return False

    def cancel(self) -> None:
        """Close on the user's request: decline any pending approval and stay
        hidden for the rest of this attempt, even if Howdy reports back later."""
        self.silenced = True
        if self.dismiss_timeout:
            GLib.source_remove(self.dismiss_timeout)
            self.dismiss_timeout = None
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

    def present_result(self, success: bool) -> None:
        if self.silenced:
            return
        self._present(SUCCESS if success else FAILURE)
        self.dismiss_timeout = GLib.timeout_add(1100 if success else 1600, self.dismiss)

    @staticmethod
    def reply(connection: socket.socket, payload: dict[str, Any]) -> None:
        try:
            connection.sendall(encode(payload))
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
        else:
            self.dismiss()

    def _approval_timed_out(self) -> bool:
        self.approval_timeout = None
        self.resolve(False)
        return False

    def handle_request(self, message: dict[str, Any], connection: socket.socket) -> bool:
        phase = message.get("phase")
        if phase == "approval":
            if self.pending:
                self.reply(connection, {"approved": False, "reason": "busy"})
                return False
            self.pending = PendingApproval(connection, str(message.get("id", "")))
            self.present_approval(message.get("requester"))
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
            message = decode(connection.recv(MAX_MESSAGE_BYTES + 1).split(b"\n", 1)[0])
            GLib.idle_add(self.hud.handle_request, message, connection)
        except (OSError, ValueError):
            connection.close()


class Demo:
    """Loops the real request flow without PAM, Howdy, or the camera.

    Approve pretends to scan, then alternates success and failure. Use
    Password, close, or Escape decline. A new request follows each ending.
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
        print("[demo] FaceGate test mode — Approve, Deny, hover to close, or Esc. Ctrl+C quits.", flush=True)
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
    args = parser.parse_args()
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
    try:
        return GLib.MainLoop().run() or 0
    finally:
        server.close()


if __name__ == "__main__":
    raise SystemExit(main())
