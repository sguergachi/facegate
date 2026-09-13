"""Loops the island's Add Face flow against the real camera, without root.

It streams through the same socket protocol as FaceGate Settings, and uses
Howdy-style face detection, but never loads Howdy's recognition models or
writes a face: samples are counted, not encoded. Escape or Cancel stops a
scan; a new one starts a few seconds after each ending.
"""

from __future__ import annotations

import glob
import json
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any

from gi.repository import GLib

from . import faces
from .edge_hud import EdgeHud, HudServer
from .protocol import PREVIEW_HEIGHT, PREVIEW_WIDTH, encode

PAUSE_SECONDS = 4.0


def find_camera() -> str | None:
    """Prefer an infrared sensor: its frames have no colour."""
    import cv2

    fallback = None
    for path in sorted(glob.glob("/dev/v4l/by-id/*-index0") + glob.glob("/dev/v4l/by-id/*-index2")) or sorted(glob.glob("/dev/video*")):
        capture = cv2.VideoCapture(path)
        ok, frame = capture.read()
        capture.release()
        if not ok or frame is None:
            continue
        fallback = fallback or path
        if (frame[:, :, 0] == frame[:, :, 2]).all():
            return path
    return fallback


class DemoClient:
    def __init__(self, socket_path: Path, camera: str) -> None:
        self.socket_path = socket_path
        self.camera = camera

    def start(self) -> None:
        threading.Thread(target=self._loop, name="facegate-enroll-demo", daemon=True).start()

    def _loop(self) -> None:
        import cv2

        while True:
            time.sleep(1.0)
            capture = cv2.VideoCapture(self.camera)
            try:
                self._scan(capture)
            except OSError as error:
                print("[demo] stream ended: %s" % error, flush=True)
            finally:
                capture.release()
            time.sleep(PAUSE_SECONDS)

    def _scan(self, capture: Any) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.connect(str(self.socket_path))
        connection.sendall(encode({"phase": "enroll"}))
        reply = json.loads(connection.recv(4_096).split(b"\n", 1)[0])
        if not reply.get("ok"):
            print("[demo] island busy", flush=True)
            connection.close()
            return
        connection.setblocking(False)
        print("[demo] scanning with %s" % self.camera, flush=True)

        def stopped() -> bool:
            try:
                connection.recv(4_096)  # Cancel, or the island went away.
            except BlockingIOError:
                return False
            return True

        def read_frame() -> Any:
            ok, frame = capture.read()
            return frame if ok else None

        scanner = faces.Scanner(read_frame, 320.0, 60.0, None)
        result: dict[str, Any] = {"result": "failure", "detail": "Stopped."}
        try:
            for event in scanner.scan(PREVIEW_WIDTH, PREVIEW_HEIGHT, stopped):
                kind = event["event"]
                if kind == "frame":
                    message = {"frame": event["data"], "w": event["w"], "h": event["h"]}
                elif kind == "hint":
                    message = {"hint": event["text"]}
                elif kind == "progress":
                    message = {"progress": event["value"]}
                else:
                    result = {"result": "success"}
                    break
                connection.setblocking(True)
                connection.sendall(encode(message))
                connection.setblocking(False)
            else:
                print("[demo] cancelled", flush=True)
                connection.close()
                return
        except faces.EnrollError as error:
            result = {"result": "failure", "detail": str(error)}
        connection.setblocking(True)
        connection.sendall(encode(result))
        print("[demo] %s" % result, flush=True)
        time.sleep(0.2)
        connection.close()


def main(camera: str | None) -> int:
    camera = camera or find_camera()
    if not camera:
        print("[demo] no camera found", flush=True)
        return 1
    socket_path = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / ("facegate-enroll-demo-%d.sock" % os.getpid())
    hud = EdgeHud()
    server = HudServer(socket_path, hud)
    server.start()
    DemoClient(socket_path, camera).start()
    print("[demo] FaceGate Add Face test mode — Cancel or Esc stops a scan. Ctrl+C quits.", flush=True)
    try:
        GLib.MainLoop().run()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
    return 0
