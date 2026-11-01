#!/usr/bin/env python3
"""Self-driving FaceGate walkthrough for screen recording.

Drives the REAL production HUD with no PAM, camera, privileged command, or
user present: this script answers approvals and the password field on a fixed
timeline, so scan outcomes are STAGED, while every pixel is production UI on
a real desktop. The real-face performance remains the release-grade asset;
this exists so re-records never need a human.

The terminal is driven separately (Konsole sendText); this only plays the
island. Gate file /tmp/fg-walk/start rolls the clock. Prints DONE and exits.
"""

import socket
import time
from pathlib import Path

from facegate.edge_hud import EdgeHud, Gdk, GLib, Gtk, Gtk4LayerShell

START = Path("/tmp/fg-walk/start")
START.unlink(missing_ok=True)

PASSWORD_DOTS = 12


class Driver:
    def __init__(self):
        self.t0 = None
        self.events = []
        self.hud = EdgeHud()
        # Never disturb the session's polkit ownership; this driver only
        # plays the island.
        self.hud.polkit = None
        self._schedule()
        GLib.timeout_add(50, self.tick)

    def at(self, seconds, fn):
        self.events.append((seconds, fn))

    def _schedule(self):
        self.at(2.0, self.approval_1)
        self.at(4.5, self.approve)
        self.at(7.5, lambda: self.hud.present_result(True))
        self.at(12.0, self.approval_2)
        self.at(14.5, self.approve)
        self.at(17.5, lambda: self.hud.present_result(False))
        self.at(19.0, self.password_phase)
        self.at(25.5, self.escape)
        self.at(27.5, self.quit)

    def _sock(self):
        writer, reader = socket.socketpair()
        reader.setblocking(False)
        return writer, reader

    def approval_1(self):
        writer, self.reader = self._sock()
        self.hud.handle_request(
            {
                "id": "walk-1",
                "phase": "approval",
                "timeout_ms": 30_000,
                "requester": {"app": "Konsole", "command": "sudo -v"},
            },
            writer,
        )

    def approval_2(self):
        writer, self.reader = self._sock()
        self.hud.handle_request(
            {
                "id": "walk-2",
                "phase": "approval",
                "timeout_ms": 30_000,
                "requester": {"app": "Konsole", "command": "sudo -v"},
            },
            writer,
        )

    def approve(self):
        # Same handler the Approve button calls; like PAM, the scan is
        # announced right after the approval reply.
        self.hud._primary()
        self.hud.handle_request({"phase": "scanning"}, self._sock()[0])

    def password_phase(self):
        writer, self.reader = self._sock()
        self.hud.handle_request(
            {
                "id": "walk-2",
                "phase": "password",
                "requester": {"app": "Konsole", "command": "sudo -v"},
            },
            writer,
        )
        for i in range(1, PASSWORD_DOTS + 1):
            GLib.timeout_add(150 * i, self._dot, i)

    def _dot(self, count):
        # Content never leaves this process (Escape, not submit); the entry
        # masks it into dots on screen either way.
        self.hud.entry.set_text("x" * count)
        return False

    def escape(self):
        # Same path as the Escape key: back to the terminal prompt, with
        # nothing submitted anywhere.
        self.hud.cancel()

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
