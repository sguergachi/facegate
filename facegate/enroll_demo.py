"""Loop the island's enrollment presentation without changing Gaze data.

This optional developer demo reads a local camera with OpenCV, detects rather
than recognizes a face, and streams the same preview/hint/progress messages the
Settings UI consumes. It never creates an embedding or writes a template.
"""

from __future__ import annotations

import base64
import glob
import json
import os
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from gi.repository import GLib

from .edge_hud import EdgeHud, HudServer
from .protocol import PREVIEW_HEIGHT, PREVIEW_WIDTH, encode

PAUSE_SECONDS = 4.0
SCAN_SECONDS = 30.0
SAMPLE_SECONDS = 0.8
SAMPLES = 5


def opencv() -> Any:
    try:
        import cv2
    except ImportError as error:
        raise RuntimeError(
            "The optional camera demo needs python-opencv (sudo pacman -S python-opencv)."
        ) from error
    return cv2


def find_camera() -> str | None:
    """Prefer an infrared sensor: its frames have no colour."""
    cv2 = opencv()
    candidates = sorted(
        glob.glob("/dev/v4l/by-id/*-index0") + glob.glob("/dev/v4l/by-id/*-index2")
    ) or sorted(glob.glob("/dev/video*"))
    fallback = None
    for path in candidates:
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
        cv2 = opencv()
        while True:
            time.sleep(1.0)
            capture = cv2.VideoCapture(self.camera)
            try:
                self._scan(capture)
            except (OSError, RuntimeError) as error:
                print(f"[demo] stream ended: {error}", flush=True)
            finally:
                capture.release()
            time.sleep(PAUSE_SECONDS)

    @staticmethod
    def _events(capture: Any, stopped: Any) -> Iterator[dict[str, Any]]:
        cv2 = opencv()
        cascade_path = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
        detector = cv2.CascadeClassifier(cascade_path)
        if detector.empty():
            raise RuntimeError("OpenCV's frontal-face detector is unavailable.")
        started = time.monotonic()
        last_sample = started - SAMPLE_SECONDS
        samples = 0
        last_hint = ""
        while not stopped():
            if time.monotonic() - started > SCAN_SECONDS:
                raise RuntimeError("No face was held in view long enough.")
            ok, frame = capture.read()
            if not ok or frame is None:
                raise RuntimeError("The camera stopped sending frames.")
            grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            found = detector.detectMultiScale(grey, scaleFactor=1.12, minNeighbors=5)
            preview = cv2.resize(grey, (PREVIEW_WIDTH, PREVIEW_HEIGHT), interpolation=cv2.INTER_AREA)
            yield {
                "frame": base64.b64encode(preview.tobytes()).decode("ascii"),
                "w": PREVIEW_WIDTH,
                "h": PREVIEW_HEIGHT,
            }
            hint = (
                "Look straight at the camera."
                if len(found) == 0
                else "Make sure only you are in view."
                if len(found) > 1
                else "Hold still and slowly move your head."
            )
            if hint != last_hint:
                last_hint = hint
                yield {"hint": hint}
            now = time.monotonic()
            if len(found) == 1 and now - last_sample >= SAMPLE_SECONDS:
                samples += 1
                last_sample = now
                yield {"progress": samples / SAMPLES}
                if samples == SAMPLES:
                    return

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
        print(f"[demo] scanning with {self.camera}", flush=True)

        def stopped() -> bool:
            try:
                connection.recv(4_096)
                return True
            except BlockingIOError:
                return False

        result: dict[str, Any] = {"result": "failure", "detail": "Stopped."}
        try:
            for message in self._events(capture, stopped):
                connection.setblocking(True)
                connection.sendall(encode(message))
                connection.setblocking(False)
            if stopped():
                connection.close()
                return
            result = {"result": "success"}
        except RuntimeError as error:
            result = {"result": "failure", "detail": str(error)}
        connection.setblocking(True)
        connection.sendall(encode(result))
        print(f"[demo] {result}", flush=True)
        time.sleep(0.2)
        connection.close()


def main(camera: str | None) -> int:
    try:
        camera = camera or find_camera()
    except RuntimeError as error:
        print(f"[demo] {error}", flush=True)
        return 1
    if not camera:
        print("[demo] no camera found", flush=True)
        return 1
    socket_path = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / f"facegate-enroll-demo-{os.getpid()}.sock"
    hud = EdgeHud()
    server = HudServer(socket_path, hud)
    server.start()
    DemoClient(socket_path, camera).start()
    print("[demo] FaceGate enrollment test — Cancel or Esc stops a scan. Ctrl+C quits.", flush=True)
    try:
        GLib.MainLoop().run()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
    return 0
