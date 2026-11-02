#!/usr/bin/env python3
"""Film FaceGate's settings panel and enrollment scan for the walkthrough.

Drives the REAL production island on a real desktop with no user present:
the settings panel is navigated through its own page methods (the same code
the buttons call), and the enrollment scan renders the REAL starlight
pipeline. The scan's mesh comes from FaceGate's own bundled face-surface
template instead of a live camera, and no biometric data is written anywhere:
this exists so re-records never need a human face. The real-face performance
remains the release-grade asset.

Gate file /tmp/fg-walk/start-panel rolls the clock. Prints DONE and exits.
"""

import base64
import math
import socket
import time
from pathlib import Path

import numpy

from facegate.edge_hud import EdgeHud, Gdk, GLib, Gtk, Gtk4LayerShell
from facegate.protocol import PREVIEW_HEIGHT, PREVIEW_WIDTH

START = Path("/tmp/fg-walk/start-panel")
START.unlink(missing_ok=True)

SURFACE = Path(__file__).resolve().parent.parent / "facegate" / "models" / "face_surface.npy"

MESH_FPS = 10
SCAN_FRAMES = 80  # 8 seconds of slowly turning head


def with_irises(surface: numpy.ndarray) -> numpy.ndarray:
    """468 surface points plus MediaPipe's 10 iris landmarks.

    The iris centers ride the middle of their eye sockets (landmarks the
    renderer itself names); the contour is a small diamond around each.
    """
    left = surface[[33, 133, 159, 145]].mean(axis=0)
    right = surface[[263, 362, 386, 374]].mean(axis=0)
    ring = numpy.array(
        [[0, 0, 0.8], [0.55, 0, 0.6], [-0.55, 0, 0.6], [0, 0.55, 0.6], [0, -0.55, 0.6]],
        numpy.float32,
    )
    return numpy.concatenate([surface, left + ring, right + ring]).astype(numpy.float32)


def portraits() -> list[str]:
    """The bundled face-surface template, posed in preview pixels."""
    model = with_irises(numpy.load(str(SURFACE)).astype(numpy.float32))
    frames = []
    for index in range(SCAN_FRAMES):
        yaw = 0.22 * math.sin(2 * math.pi * index / SCAN_FRAMES)
        pitch = 0.08 * math.sin(4 * math.pi * index / SCAN_FRAMES + 1.0)
        points = model.copy()
        x, z = points[:, 0], points[:, 2]
        points[:, 0] = x * math.cos(yaw) + z * math.sin(yaw)
        points[:, 2] = -x * math.sin(yaw) + z * math.cos(yaw)
        y, z = points[:, 1], points[:, 2]
        points[:, 1] = y * math.cos(pitch) - z * math.sin(pitch)
        points[:, 2] = y * math.sin(pitch) + z * math.cos(pitch)
        pixels = numpy.empty_like(points)
        pixels[:, 0] = PREVIEW_WIDTH / 2 + points[:, 0] * 9.0
        pixels[:, 1] = PREVIEW_HEIGHT / 2 + points[:, 1] * 6.8
        pixels[:, 2] = points[:, 2] * 6.8
        frames.append(base64.b64encode(numpy.ascontiguousarray(pixels, numpy.float32).tobytes()).decode("ascii"))
    return frames


class Driver:
    def __init__(self):
        self.t0 = None
        self.events = []
        self.mesh_frames = portraits()
        self.mesh_index = 0
        self.hud = EdgeHud()
        # Never disturb the session's polkit ownership; this driver only
        # plays the island.
        self.hud.polkit = None
        self._schedule()
        GLib.timeout_add(50, self.tick)

    def at(self, seconds, fn):
        self.events.append((seconds, fn))

    def _schedule(self):
        self.at(0.5, self.open_home)
        self.at(3.0, self.replay_welcome)
        self.at(5.2, lambda: self.panel._go("how"))
        self.at(7.2, lambda: self.panel._go("privacy"))
        self.at(9.2, lambda: self.panel._show_page("home", direction=-1))
        self.at(11.0, lambda: self.panel._show_page("manage"))
        self.at(15.0, lambda: self.panel._show_page("view"))
        self.at(17.5, lambda: self.panel._show_page("home", direction=-1))
        self.at(19.0, self.close_panel)
        self.at(20.5, self.begin_scan)
        self.at(23.0, self.begin_mesh)
        self.at(32.5, self.finish_scan)
        self.at(36.0, self.quit)

    def open_home(self):
        self.hud.open_config()
        self.panel = self.hud.config_panel

    def replay_welcome(self):
        # Same entry as the panel's Replay Welcome link.
        self.panel._replay_onboarding()

    def close_panel(self):
        self.hud.close_config()

    def begin_scan(self):
        writer, self.reader = socket.socketpair()
        self.conn = writer
        GLib.idle_add(self.hud.begin_enrollment, writer)
        self.hud.enrollment_message(writer, {"hint": "Look straight at the camera."})

    def begin_mesh(self):
        self.hud.enrollment_message(
            self.conn, {"hint": "Hold still and slowly move your head."}
        )
        GLib.timeout_add(1000 // MESH_FPS, self._mesh_frame)

    def _mesh_frame(self):
        if self.mesh_index >= len(self.mesh_frames):
            return False
        self.hud.enrollment_message(
            self.conn,
            {
                "mesh": self.mesh_frames[self.mesh_index],
                "w": PREVIEW_WIDTH,
                "h": PREVIEW_HEIGHT,
                "space": "xyz",
                "progress": (self.mesh_index + 1) / len(self.mesh_frames),
            },
        )
        self.mesh_index += 1
        return self.mesh_index < len(self.mesh_frames)

    def finish_scan(self):
        # No template is written: the result message only plays the island's
        # success state, exactly as staged sudo outcomes do.
        self.hud.enrollment_message(self.conn, {"result": "success"})

    def quit(self):
        try:
            self.reader.close()
        except OSError:
            pass
        print("DONE", flush=True)
        MAIN_LOOP.quit()

    def tick(self):
        if self.t0 is None:
            if START.exists():
                self.t0 = time.monotonic()
            return True
        t = time.monotonic() - self.t0
        while self.events and self.events[0][0] <= t:
            _, fn = self.events.pop(0)
            fn()
        return True


MAIN_LOOP = GLib.MainLoop()


def main() -> int:
    Driver()
    MAIN_LOOP.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
