"""Translate Gaze's authorized D-Bus API into FaceGate Settings events.

This process runs as the logged-in user. It never reads or writes biometric
templates, opens the camera, or becomes root. ``gazed`` independently applies
polkit to enrollment and deletion before it changes anything.
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
    """Line reader over stdin that can be polled during Gaze enrollment."""

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
    def __init__(
        self,
        user: str,
        output: BinaryIO,
        client: Any | None = None,
        metadata_file: Any | None = None,
    ) -> None:
        self.user = user
        self.output = output
        self.client = client or faces.GazeClient()
        self.metadata_file = metadata_file
        self.commands = Commands()

    def send(self, event: dict[str, Any]) -> None:
        self.output.write((json.dumps(event, separators=(",", ":")) + "\n").encode("utf-8"))
        self.output.flush()

    def records(self) -> list[dict[str, Any]]:
        return faces.summarize(
            self.client.list_faces(self.user),
            faces.load_metadata(self.metadata_file),
        )

    def send_faces(self, event: str = "faces", **extra: Any) -> None:
        self.send({"event": event, "faces": self.records(), **extra})

    def run(self, args: list[str]) -> int:
        try:
            if args == ["list"]:
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
        except faces.GazeError as error:
            self.send({"event": "error", "message": str(error)})
            return 1
        return 0

    def handle(self, command: dict[str, Any]) -> None:
        name = command.get("cmd")
        if name == "list":
            self.send_faces()
        elif name == "remove":
            face_name = faces.clean_label(command.get("id"))
            known = {record[0] for record in self.client.list_faces(self.user)}
            if not face_name or face_name not in known:
                self.send({"event": "error", "message": "No such face."})
                return
            self.client.delete_face(self.user, face_name)
            metadata = faces.load_metadata(self.metadata_file)
            metadata.pop(face_name, None)
            faces.save_metadata(metadata, self.metadata_file)
            self.send_faces(removed=face_name)
        elif name == "rename":
            old_name = faces.clean_label(command.get("id"))
            new_name = faces.clean_label(command.get("label"))
            known = {record[0] for record in self.client.list_faces(self.user)}
            if not old_name or old_name not in known:
                self.send({"event": "error", "message": "No such face."})
                return
            if not new_name or (new_name != old_name and new_name in known):
                self.send({"event": "error", "message": "Choose a different face name."})
                return
            if new_name != old_name:
                self.client.rename_face(self.user, old_name, new_name)
                metadata = faces.load_metadata(self.metadata_file)
                if old_name in metadata:
                    metadata[new_name] = metadata.pop(old_name)
                    faces.save_metadata(metadata, self.metadata_file)
            self.send_faces(renamed=new_name)
        elif name == "enroll":
            self.enroll(command)

    def enroll(self, command: dict[str, Any]) -> None:
        try:
            width = int(command.get("width", PREVIEW_WIDTH))
            height = int(command.get("height", PREVIEW_HEIGHT))
        except (TypeError, ValueError):
            width, height = PREVIEW_WIDTH, PREVIEW_HEIGHT
        if width < 16 or height < 16 or width * height > MAX_PREVIEW_PIXELS:
            self.send({"event": "error", "message": "Unsupported preview size."})
            return

        try:
            records = self.records()
        except faces.GazeError as error:
            self.send({"event": "error", "message": str(error)})
            return
        label = faces.clean_label(command.get("label")) or faces.next_label(records)
        cancelled = False

        def stopped() -> bool:
            nonlocal cancelled
            while not cancelled:
                pending = self.commands.read(0)
                if pending is None:
                    return self.commands.closed
                cancelled = pending.get("cmd") == "cancel"
            return True

        try:
            mesh = faces.load_face_mesh()
            for event in self.client.enroll(self.user, label, stopped, width, height, mesh=mesh):
                if event.get("event") == "completed":
                    metadata = faces.load_metadata(self.metadata_file)
                    metadata[label] = int(time.time())
                    faces.save_metadata(metadata, self.metadata_file)
                    self.send_faces("added", id=label, label=label)
                    return
                self.send(event)
        except faces.GazeError as error:
            self.send({"event": "error", "message": str(error)})
            return
        if cancelled or self.commands.closed:
            self.send({"event": "cancelled"})


def calling_user() -> str:
    """The helper must stay unprivileged; Gaze authorizes changes itself."""
    if os.geteuid() == 0:
        raise SystemExit("facegate-faces must run as your normal user, not as root")
    return pwd.getpwuid(os.geteuid()).pw_name


def main(args: list[str] | None = None) -> int:
    output = os.fdopen(os.dup(1), "wb")
    try:
        return Helper(calling_user(), output).run(sys.argv[1:] if args is None else args)
    except BrokenPipeError:
        return 1
    except faces.GazeError as error:
        output.write((json.dumps({"event": "error", "message": str(error)}) + "\n").encode())
        output.flush()
        return 1


if __name__ == "__main__":
    sys.exit(main())
