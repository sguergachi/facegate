"""facegate-faces: the root side of FaceGate's in-island face management.

Each run does one thing for the user who ran pkexec, and writes JSON events,
one per line, to stdout:

    facegate-faces-list        -> {"event": "faces", ...}          (no prompt)
    facegate-faces remove ID   -> {"event": "faces", ...}          (admin)
    facegate-faces rename ID L -> {"event": "faces", ...}          (admin)
    facegate-faces enroll      -> frame/hint/progress..., then     (admin)
                                  "added", "cancelled" or "error"

Listing only reads labels and dates, so its polkit action needs no
authentication. Changing faces does, every time. During a scan, a
{"cmd": "cancel"} line on stdin stops it.
"""

from __future__ import annotations

import json
import os
import pwd
import select
import sys
import time
from typing import Any, BinaryIO

from . import faces
from .protocol import PREVIEW_HEIGHT, PREVIEW_WIDTH

MAX_COMMAND_BYTES = 4_096
MAX_PREVIEW_PIXELS = 640 * 480


class Commands:
    """Line reader over stdin that can be polled between camera frames."""

    def __init__(self) -> None:
        self.buffer = b""
        self.closed = False

    def read(self, timeout: float | None) -> dict[str, Any] | None:
        while b"\n" not in self.buffer:
            if self.closed:
                return None
            ready, _, _ = select.select([0], [], [], timeout)
            if not ready:
                return None
            chunk = os.read(0, MAX_COMMAND_BYTES)
            if not chunk:
                self.closed = True
                return None
            self.buffer += chunk
            if len(self.buffer) > MAX_COMMAND_BYTES and b"\n" not in self.buffer:
                self.buffer = b""
        line, self.buffer = self.buffer.split(b"\n", 1)
        try:
            value = json.loads(line)
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}


class Helper:
    def __init__(self, user: str, output: BinaryIO, howdy_dir: str = faces.HOWDY_DIR) -> None:
        self.user = user
        self.output = output
        self.howdy_dir = howdy_dir
        self.path = faces.models_path(user, howdy_dir)
        self.commands = Commands()

    def send(self, event: dict[str, Any]) -> None:
        self.output.write((json.dumps(event, separators=(",", ":")) + "\n").encode("utf-8"))
        self.output.flush()

    def send_faces(self, event: str = "faces", **extra: Any) -> None:
        self.send({"event": event, "faces": faces.summarize(faces.load_models(self.path)), **extra})

    def run(self, args: list[str]) -> int:
        if args[:1] == ["list"]:
            self.handle({"cmd": "list"})
        elif args[:1] == ["remove"] and len(args) == 2:
            self.handle({"cmd": "remove", "id": args[1]})
        elif args[:1] == ["rename"] and len(args) == 3:
            self.handle({"cmd": "rename", "id": args[1], "label": args[2]})
        elif args[:1] == ["enroll"] and len(args) <= 2:
            self.handle({"cmd": "enroll", "label": args[1] if len(args) == 2 else ""})
        else:
            self.send({"event": "error", "message": "Unknown command."})
            return 2
        return 0

    def handle(self, command: dict[str, Any]) -> None:
        name = command.get("cmd")
        if name == "list":
            self.send_faces()
        elif name in ("remove", "rename"):
            try:
                model_id = int(command.get("id"))
            except (TypeError, ValueError):
                self.send({"event": "error", "message": "No such face."})
                return
            models = faces.load_models(self.path)
            if name == "remove":
                faces.save_models(self.path, faces.remove_model(models, model_id))
                self.send_faces(removed=model_id)
                return
            label = faces.clean_label(command.get("label"))
            if not label:
                self.send({"event": "error", "message": "A face needs a name."})
                return
            faces.save_models(self.path, faces.rename_model(models, model_id, label))
            self.send_faces(renamed=model_id)
        elif name == "enroll":
            self.enroll(command)

    def enroll(self, command: dict[str, Any]) -> None:
        try:
            width, height = int(command.get("width", PREVIEW_WIDTH)), int(command.get("height", PREVIEW_HEIGHT))
        except (TypeError, ValueError):
            width, height = PREVIEW_WIDTH, PREVIEW_HEIGHT
        if width < 16 or height < 16 or width * height > MAX_PREVIEW_PIXELS:
            self.send({"event": "error", "message": "Unsupported preview size."})
            return
        cancelled = False

        def stopped() -> bool:
            nonlocal cancelled
            while not cancelled:
                pending = self.commands.read(0)
                if pending is None:
                    return self.commands.closed
                cancelled = pending.get("cmd") == "cancel"
            return True

        release = None
        try:
            scanner, release = faces.howdy_scanner(self.howdy_dir)
            for event in scanner.scan(width, height, stopped):
                if event["event"] == "captured":
                    models = faces.load_models(self.path)
                    label = faces.clean_label(command.get("label")) or faces.next_label(models)
                    model = faces.new_model(models, label, event["encodings"], time.time())
                    faces.save_models(self.path, models + [model])
                    self.send_faces("added", id=model["id"], label=label)
                    return
                self.send(event)
        except faces.EnrollError as error:
            self.send({"event": "error", "message": str(error)})
            return
        finally:
            if release:
                release()
        if cancelled:
            self.send({"event": "cancelled"})


def calling_user() -> str:
    uid = os.environ.get("PKEXEC_UID") or os.environ.get("SUDO_UID")
    if os.geteuid() != 0 or not uid:
        raise SystemExit("facegate-faces must be started with pkexec")
    return pwd.getpwuid(int(uid)).pw_name


def main(args: list[str] | None = None) -> int:
    user = calling_user()
    # Howdy and OpenCV print to stdout; keep it for the protocol only.
    output = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    try:
        return Helper(user, output).run(sys.argv[1:] if args is None else args)
    except BrokenPipeError:
        return 1


if __name__ == "__main__":
    sys.exit(main())
