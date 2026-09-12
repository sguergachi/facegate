#!/usr/bin/env python3
"""A true Wayland layer-shell HUD for FaceGate.

Unlike an xdg-shell application window, this surface is anchored by KWin to
the physical top edge of the monitor.  The only visible pixels are the black
island itself; the full-width layer surface behind it stays transparent.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import socket
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gdk, GLib, Gtk, Gtk4LayerShell  # noqa: E402

from .protocol import MAX_MESSAGE_BYTES, decode, encode


ISLAND_WIDTH = 416
ISLAND_HEIGHT = 376
COLLAPSED_WIDTH = 172
COLLAPSED_HEIGHT = 48
SOCKET_NAME = "facegate.sock"


def default_socket_path() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid())) / SOCKET_NAME


def rounded_path(context: Any, x: float, y: float, width: float, height: float, radius: float) -> None:
    radius = min(radius, width / 2, height / 2)
    context.new_sub_path()
    context.arc(x + width - radius, y + radius, radius, -math.pi / 2, 0)
    context.arc(x + width - radius, y + height - radius, radius, 0, math.pi / 2)
    context.arc(x + radius, y + height - radius, radius, math.pi / 2, math.pi)
    context.arc(x + radius, y + radius, radius, math.pi, math.pi * 1.5)
    context.close_path()


class LivingFace(Gtk.DrawingArea):
    """The iconic FaceGate character, animated at the display's frame cadence."""

    PROFILES = {
        "approval": ((0.96, 0.96, 0.98), 0.18, 1.0, 2.0, 0.0),
        "scanning": ((0.72, 0.97, 1.00), 0.04, 0.92, 8.0, 1.0),
        "success": ((0.88, 1.00, 0.92), 0.95, 0.48, 0.0, 0.18),
        "failure": ((1.00, 0.71, 0.75), -0.16, 0.75, 1.5, 0.0),
    }

    def __init__(self) -> None:
        super().__init__()
        self.set_content_width(128)
        self.set_content_height(128)
        self.phase = 0.0
        self.state = "approval"
        self.previous_state = "approval"
        self.transition = 1.0
        self.set_draw_func(self._draw)

    def set_state(self, state: str) -> None:
        if state != self.state:
            self.previous_state = self.state
            self.state = state
            self.transition = 0.0

    def advance(self, elapsed: float) -> None:
        self.phase = (self.phase + elapsed * (1.05 if self.state == "scanning" else 0.48)) % 1.0
        self.transition = min(1.0, self.transition + elapsed * 5.2)
        self.queue_draw()

    def _values(self) -> tuple[tuple[float, float, float], float, float, float, float]:
        before = self.PROFILES[self.previous_state]
        after = self.PROFILES[self.state]
        mix = lambda left, right: left + (right - left) * self.transition
        color = tuple(mix(before[0][index], after[0][index]) for index in range(3))
        return color, mix(before[1], after[1]), mix(before[2], after[2]), mix(before[3], after[3]), mix(before[4], after[4])

    def _draw(self, _area: Gtk.DrawingArea, context: Any, width: int, height: int) -> None:
        color, smile, open_eyes, look_range, scan = self._values()
        orbit = self.phase * math.tau
        bob = math.sin(orbit * 0.62) * (1.0 + scan * 0.8)
        tilt = math.sin(orbit * (0.75 + scan * 1.6)) * (0.7 + scan * 1.5)
        look = math.sin(orbit * (0.7 + scan * 1.8)) * look_range
        context.translate(width / 2, height / 2 + bob)
        context.rotate(math.radians(tilt))
        context.translate(-64, -64)

        # Subtle halo; it breathes rather than flashing.
        context.set_source_rgba(*color, 0.10 + scan * 0.13)
        context.arc(64, 64, 58 + math.sin(orbit) * 1.5, 0, math.tau)
        context.fill()

        context.set_line_cap(1)
        context.set_line_width(3.2)
        context.set_source_rgba(*color, 1.0)
        for x, y, dx, dy in ((14, 14, 1, 1), (114, 14, -1, 1), (14, 114, 1, -1), (114, 114, -1, -1)):
            context.move_to(x, y)
            context.line_to(x + dx * 21, y)
            context.move_to(x, y)
            context.line_to(x, y + dy * 21)
        context.stroke()

        # Friendly, abstract face: it never previews or stores camera imagery.
        rounded_path(context, 42, 35, 44, 57, 20)
        context.set_source_rgba(0.025, 0.025, 0.03, 1.0)
        context.fill_preserve()
        context.set_line_width(2.0)
        context.set_source_rgba(0.96, 0.96, 0.98, 0.88)
        context.stroke()
        eye_height = max(3.0, 12 * open_eyes)
        for eye_x in (56, 72):
            rounded_path(context, eye_x - 10, 58 - eye_height / 2, 20, eye_height, eye_height / 2)
            context.set_source_rgba(0.025, 0.025, 0.03, 1.0)
            context.fill_preserve()
            context.set_line_width(1.7)
            context.set_source_rgba(*color, 0.9)
            context.stroke()
            context.arc(eye_x + look * 0.22, 58, 2.6, 0, math.tau)
            context.set_source_rgba(*color, 1.0)
            context.fill()
        context.move_to(54, 76)
        context.curve_to(59, 76 + 8 * smile, 69, 76 + 8 * smile, 74, 76)
        context.set_line_width(2.0)
        context.set_source_rgba(*color, 1.0)
        context.stroke()

        if scan > 0.01:
            context.save()
            context.translate(64, 64)
            context.rotate(orbit)
            context.translate(-64, -64)
            context.arc(64, 64, 40, math.radians(-38), math.radians(50))
            context.set_line_width(1.6)
            context.set_source_rgba(*color, 0.82 * scan)
            context.stroke()
            context.restore()
            y = 27 + self.phase * 74
            gradient = __import__("cairo").LinearGradient(25, y, 103, y)
            gradient.add_color_stop_rgba(0.0, *color, 0.0)
            gradient.add_color_stop_rgba(0.5, *color, scan)
            gradient.add_color_stop_rgba(1.0, *color, 0.0)
            context.set_source(gradient)
            context.set_line_width(2.4)
            context.move_to(25, y)
            context.line_to(103, y)
            context.stroke()


@dataclass
class PendingApproval:
    connection: socket.socket
    request_id: str


class EdgeHud:
    def __init__(self) -> None:
        self.window = Gtk.Window()
        self.window.set_decorated(False)
        self.window.set_resizable(False)
        self.window.set_default_size(1, ISLAND_HEIGHT)
        Gtk4LayerShell.init_for_window(self.window)
        Gtk4LayerShell.set_layer(self.window, Gtk4LayerShell.Layer.OVERLAY)
        Gtk4LayerShell.set_anchor(self.window, Gtk4LayerShell.Edge.TOP, True)
        Gtk4LayerShell.set_anchor(self.window, Gtk4LayerShell.Edge.LEFT, True)
        Gtk4LayerShell.set_anchor(self.window, Gtk4LayerShell.Edge.RIGHT, True)
        Gtk4LayerShell.set_exclusive_zone(self.window, -1)
        Gtk4LayerShell.set_keyboard_mode(self.window, Gtk4LayerShell.KeyboardMode.ON_DEMAND)

        self.reveal = 0.0
        self.departing = False
        self.visible = False
        self.last_tick = GLib.get_monotonic_time()
        self.pending: PendingApproval | None = None
        self.approval_timeout: int | None = None

        self._install_css()
        root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        root.set_halign(Gtk.Align.CENTER)
        root.set_valign(Gtk.Align.START)
        self.canvas = Gtk.Overlay()
        self.canvas.set_size_request(ISLAND_WIDTH, ISLAND_HEIGHT)
        self.backdrop = Gtk.DrawingArea()
        self.backdrop.set_draw_func(self._draw_island)
        self.canvas.set_child(self.backdrop)
        root.append(self.canvas)
        self.window.set_child(root)

        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.content.set_size_request(ISLAND_WIDTH, ISLAND_HEIGHT)
        self.content.set_halign(Gtk.Align.CENTER)
        self.content.set_valign(Gtk.Align.START)
        self.content.set_margin_top(20)
        self.content.set_margin_start(32)
        self.content.set_margin_end(32)
        self.canvas.add_overlay(self.content)
        self.eyebrow = Gtk.Label(label="FACE VERIFICATION")
        self.eyebrow.add_css_class("eyebrow")
        self.content.append(self.eyebrow)
        self.face = LivingFace()
        self.face.set_halign(Gtk.Align.CENTER)
        self.content.append(self.face)
        self.title = Gtk.Label(label="Approve face verification")
        self.title.add_css_class("title")
        self.content.append(self.title)
        self.detail = Gtk.Label(label="Your camera stays off until you approve.")
        self.detail.set_wrap(True)
        self.detail.set_justify(Gtk.Justification.CENTER)
        self.detail.add_css_class("detail")
        self.content.append(self.detail)
        self.content.append(Gtk.Box())
        actions = Gtk.Box(spacing=10)
        self.password = Gtk.Button(label="Use password")
        self.password.add_css_class("password")
        self.approve = Gtk.Button(label="Approve")
        self.approve.add_css_class("approve")
        self.password.set_hexpand(True)
        self.approve.set_hexpand(True)
        actions.append(self.password)
        actions.append(self.approve)
        self.content.append(actions)
        self.approve.connect("clicked", lambda *_: self.resolve(True))
        self.password.connect("clicked", lambda *_: self.resolve(False))

    @staticmethod
    def _install_css() -> None:
        css = Gtk.CssProvider()
        css.load_from_string("""
            window { background: transparent; }
            label { color: #f5f5f7; }
            .eyebrow { color: #8e8e93; font-size: 10px; font-weight: 700; letter-spacing: 1.6px; }
            .title { font-size: 20px; font-weight: 700; }
            .detail { color: #a1a1a6; font-size: 13px; }
            button { min-height: 44px; border-radius: 15px; font-size: 14px; font-weight: 600; }
            button.approve { background: #f5f5f7; color: #000000; border: none; }
            button.approve:hover { background: #ffffff; }
            button.password { background: #1c1c1e; color: #f5f5f7; border: none; }
            button.password:hover { background: #2c2c2e; }
        """)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _draw_island(self, _area: Gtk.DrawingArea, context: Any, width: int, _height: int) -> None:
        eased = self.reveal * self.reveal * (3 - 2 * self.reveal)
        island_width = COLLAPSED_WIDTH + (ISLAND_WIDTH - COLLAPSED_WIDTH) * eased
        island_height = COLLAPSED_HEIGHT + (ISLAND_HEIGHT - COLLAPSED_HEIGHT) * eased
        x = (width - island_width) / 2
        y = -34 * (1 - eased)
        rounded_path(context, x, y, island_width, island_height, 26)
        context.set_source_rgb(0.0, 0.0, 0.0)
        context.fill()

    def _tick(self) -> bool:
        now = GLib.get_monotonic_time()
        elapsed = min((now - self.last_tick) / 1_000_000, 0.05)
        self.last_tick = now
        direction = -1 if self.departing else 1
        self.reveal = max(0.0, min(1.0, self.reveal + direction * elapsed / (0.38 if self.departing else 0.62)))
        self.face.advance(elapsed)
        content_alpha = max(0.0, min(1.0, (self.reveal - 0.22) / 0.45))
        self.content.set_opacity(content_alpha)
        self.backdrop.queue_draw()
        if self.departing and self.reveal <= 0:
            self.window.set_visible(False)
            self.visible = False
            self.departing = False
            return False
        return self.visible

    def _show(self) -> None:
        if self.visible:
            return
        self.visible = True
        self.departing = False
        self.reveal = 0.0
        self.last_tick = GLib.get_monotonic_time()
        self.window.present()
        GLib.timeout_add(16, self._tick)

    def present_approval(self) -> None:
        self.face.set_state("approval")
        self.title.set_text("Approve face verification")
        self.detail.set_text("Your camera stays off until you approve.")
        self.password.set_visible(True)
        self.approve.set_visible(True)
        self._show()

    def present_scanning(self) -> None:
        self.face.set_state("scanning")
        self.title.set_text("Looking for you")
        self.detail.set_text("Hold still for a moment.")
        self.password.set_visible(False)
        self.approve.set_visible(False)
        self._show()

    def present_result(self, success: bool) -> None:
        self.face.set_state("success" if success else "failure")
        self.title.set_text("Verified" if success else "Not recognized")
        self.detail.set_text("Continuing securely." if success else "Use your password instead.")
        self.password.set_visible(False)
        self.approve.set_visible(False)
        GLib.timeout_add(880 if success else 1350, self.dismiss)

    def dismiss(self) -> bool:
        self.departing = True
        self.last_tick = GLib.get_monotonic_time()
        return False

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

    def handle_request(self, message: dict[str, Any], connection: socket.socket) -> bool:
        phase = message.get("phase")
        if phase == "approval":
            if self.pending:
                self.reply(connection, {"approved": False, "reason": "busy"})
                return False
            self.pending = PendingApproval(connection, str(message.get("id", "")))
            self.present_approval()
            timeout = max(1_000, min(int(message.get("timeout_ms", 12_000)), 30_000))
            self.approval_timeout = GLib.timeout_add(timeout, lambda: (self.resolve(False), False)[1])
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


def main() -> int:
    parser = argparse.ArgumentParser(description="FaceGate top-edge layer-shell HUD")
    parser.add_argument("--socket", type=Path, default=default_socket_path())
    args = parser.parse_args()
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
