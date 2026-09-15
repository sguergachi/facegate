"""Unprivileged Gaze client used by FaceGate Settings.

Gaze's root-owned ``gazed`` daemon owns cameras, face recognition, templates,
and authorization. This module only translates its system D-Bus API into the
small event stream consumed by FaceGate's existing settings UI.
"""

from __future__ import annotations

import base64
import json
import os
import time
from collections import deque
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import gi
import numpy

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, Gio, GLib  # noqa: E402

from .protocol import PREVIEW_HEIGHT, PREVIEW_WIDTH

GAZE_SERVICE = "com.gundulabs.Gaze"
GAZE_PATH = "/com/gundulabs/Gaze"
GAZE_INTERFACE = "com.gundulabs.Gaze"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
# Gaze's Config property is (inference, security, cameras, ...); FaceGate only
# ever touches the cameras struct and passes everything else back untouched.
CONFIG_PREFIX = "((ss)(sssdds)(ssbys)"
CAMERAS_INDEX = 2
CAMERA_FIELDS = ("rgb", "ir", "emitter_enabled", "dark_luma", "parallel")
# Mean channel difference above which a preview is a colour frame; a grey IR
# frame only differs by JPEG rounding.
COLOUR_SPREAD = 3.0
# The colour spotlight, as fractions of the frame: an oval 60% as wide as the
# frame, taller than it, centred a little low so it takes the shoulders too,
# fully lit inside 0.3 of its radius and dark beyond 0.85.
SPOTLIGHT_WIDTH = 0.6
SPOTLIGHT_HEIGHT = 1.3
SPOTLIGHT_CENTRE = 0.62
SPOTLIGHT_EDGE = (0.3, 0.85)
DBUS_TIMEOUT_MS = 35_000
ENROLL_TIMEOUT_SECONDS = 90
LABEL_CHARS = 24


class GazeError(Exception):
    """A user-facing failure from the Gaze daemon or system bus."""


def clean_label(text: object) -> str:
    return "".join(char for char in str(text or "") if char.isprintable()).strip()[:LABEL_CHARS]


def next_label(records: list[dict[str, Any]]) -> str:
    """Return ``Face``, then ``Face 2``, skipping names already in use."""
    taken = {str(record.get("label", "")) for record in records}
    if "Face" not in taken:
        return "Face"
    number = 2
    while f"Face {number}" in taken:
        number += 1
    return f"Face {number}"


def metadata_path() -> Path:
    override = os.environ.get("FACEGATE_FACE_METADATA")
    if override:
        return Path(override)
    data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return data / "facegate" / "faces.json"


def load_metadata(path: Path | None = None) -> dict[str, int]:
    try:
        value = json.loads((path or metadata_path()).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        str(name): int(added)
        for name, added in value.items()
        if isinstance(name, str) and isinstance(added, (int, float)) and added > 0
    }


def save_metadata(metadata: dict[str, int], path: Path | None = None) -> None:
    target = path or metadata_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(metadata, separators=(",", ":")))
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)


def summarize(
    records: list[tuple[str, int, bool, bool]], metadata: dict[str, int] | None = None
) -> list[dict[str, Any]]:
    """Translate Gaze face records without exposing embeddings or paths."""
    added = metadata if metadata is not None else load_metadata()
    return [
        {
            "id": name,
            "label": name,
            "time": int(added.get(name, 0)),
            "samples": int(samples),
            "rgb": bool(rgb),
            "ir": bool(ir),
        }
        for name, samples, rgb, ir in sorted(records, key=lambda record: record[0].casefold())
    ]


CAPTURE_HINTS = {
    "unused": "Waiting for the camera…",
    "camera is not in use...": "Waiting for the camera…",
    "no-face": "Look straight at the camera.",
    "please look at the camera...": "Look straight at the camera.",
    "too-dark": "Too dark. Move a little closer.",
    "need more light...": "Too dark. Move a little closer.",
    "clipped": "Move a little farther from the camera.",
    "face is clipped. please move back...": "Move a little farther from the camera.",
    "not-centered": "Center your face in the camera.",
    "please center your face...": "Center your face in the camera.",
    "too-far": "Move a little closer.",
    "please come closer...": "Move a little closer.",
    "too-close": "Move a little farther away.",
    "please back up...": "Move a little farther away.",
    "ready": "Hold still and look at the camera.",
    "usable": "Hold still and look at the camera.",
    "hold still...": "Hold still and look at the camera.",
}

ENROLL_HINTS = {
    "look-straight": "Hold still and look at the camera.",
    "face the camera": "Hold still and look at the camera.",
    "look-up": "Tilt your face slightly up.",
    "tilt your face slightly up": "Tilt your face slightly up.",
    "look-down": "Tilt your face slightly down.",
    "tilt your face slightly down": "Tilt your face slightly down.",
    "look-left": "Turn your face slightly left.",
    "turn your face slightly left": "Turn your face slightly left.",
    "look-right": "Turn your face slightly right.",
    "turn your face slightly right": "Turn your face slightly right.",
}

FAILED_PROMPTS = {
    "db-failed": "Gaze couldn't save your face.",
    "database error during enrollment": "Gaze couldn't save your face.",
    "camera-failed": "The camera stopped sending frames.",
    "camera error during enrollment": "The camera stopped sending frames.",
}


def _normal(value: object) -> str:
    return str(value or "").strip().casefold()


def _jpeg_to_greyscale(jpeg: bytes, width: int, height: int) -> bytes:
    """Decode Gaze's preview into the island's luma, the way IR sees a face.

    Starlight puts its bright stars where the frame is bright. The IR emitter
    lights the face against a dark room, which is what it's drawn for; a colour
    camera sees the opposite, a darker face against a bright room, so colour
    frames are inverted. Gaze sends IR frames as grey JPEGs, which tells the
    two apart.

    Every per-pixel step stays in C or NumPy: libjpeg scales while decoding and
    GdkPixbuf desaturates. A pure-Python pixel loop cannot keep up with a live
    camera.
    """
    loader = GdkPixbuf.PixbufLoader.new_with_type("jpeg")
    loader.connect("size-prepared", lambda source, _w, _h: source.set_size(width, height))
    loader.write(jpeg)
    loader.close()
    pixbuf = loader.get_pixbuf()
    if pixbuf is None:
        raise GazeError("Gaze sent an unreadable camera preview.")
    if (pixbuf.get_width(), pixbuf.get_height()) != (width, height):
        pixbuf = pixbuf.scale_simple(width, height, GdkPixbuf.InterpType.BILINEAR)
        if pixbuf is None:
            raise GazeError("Gaze couldn't resize the camera preview.")
    channels = pixbuf.get_n_channels()
    stride = pixbuf.get_rowstride()
    rows = numpy.frombuffer(pixbuf.get_pixels(), numpy.uint8)
    rows = numpy.pad(rows, (0, stride * height - len(rows))).reshape(height, stride)
    rgb = rows[:, : width * channels].reshape(height, width, channels)[:, :, :3].astype(numpy.int16)
    colour = numpy.abs(rgb[:, :, 0] - rgb[:, :, 1]).mean() + numpy.abs(rgb[:, :, 1] - rgb[:, :, 2]).mean()
    luma = (77 * rgb[:, :, 0] + 150 * rgb[:, :, 1] + 29 * rgb[:, :, 2]) >> 8
    if colour > COLOUR_SPREAD:
        luma = (255 - luma) * _spotlight(width, height)
    return luma.astype(numpy.uint8).tobytes()


_spotlights: dict[tuple[int, int], numpy.ndarray] = {}


def _spotlight(width: int, height: int) -> numpy.ndarray:
    """A soft oval over where Gaze asks for the head and shoulders.

    IR light falls off with distance, so the room behind a face goes dark;
    an inverted colour frame has no such falloff and lights whatever is dark
    in the room. The spotlight stands in for the emitter.
    """
    key = (width, height)
    if key not in _spotlights:
        y, x = numpy.mgrid[0:height, 0:width]
        distance = numpy.hypot(
            (x + 0.5 - width / 2) / (width / 2 * SPOTLIGHT_WIDTH),
            (y + 0.5 - height * SPOTLIGHT_CENTRE) / (height / 2 * SPOTLIGHT_HEIGHT),
        )
        inner, outer = SPOTLIGHT_EDGE
        amount = numpy.clip((outer - distance) / (outer - inner), 0, 1)
        _spotlights[key] = amount * amount * (3 - 2 * amount)
    return _spotlights[key]


def drain_main_context(context: GLib.MainContext, limit: int = 32) -> int:
    """Dispatch a bounded signal batch so a live camera cannot starve output."""
    dispatched = 0
    while dispatched < limit and context.pending():
        context.iteration(False)
        dispatched += 1
    return dispatched


class GazeClient:
    """Synchronous facade over Gaze's destination-scoped system D-Bus API."""

    def __init__(
        self,
        bus: Gio.DBusConnection | None = None,
        timeout_ms: int = DBUS_TIMEOUT_MS,
    ) -> None:
        try:
            self.bus = bus or Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except GLib.Error as error:
            raise GazeError("Couldn't connect to the Gaze daemon.") from error
        self.timeout_ms = max(250, min(int(timeout_ms), DBUS_TIMEOUT_MS))

    def _call(
        self, method: str, parameters: GLib.Variant, reply: str, interface: str = GAZE_INTERFACE
    ) -> tuple[Any, ...]:
        result = self._call_variant(method, parameters, reply, interface)
        return result.unpack()

    def _call_variant(
        self, method: str, parameters: GLib.Variant, reply: str, interface: str = GAZE_INTERFACE
    ) -> GLib.Variant:
        try:
            result = self.bus.call_sync(
                GAZE_SERVICE,
                GAZE_PATH,
                interface,
                method,
                parameters,
                GLib.VariantType.new(reply),
                Gio.DBusCallFlags.ALLOW_INTERACTIVE_AUTHORIZATION,
                self.timeout_ms,
                None,
            )
        except GLib.Error as error:
            message = error.message.rsplit(":", 1)[-1].strip()
            raise GazeError(message or "Gaze didn't answer.") from error
        return result

    def _config(self) -> GLib.Variant:
        return self._call_variant(
            "Get", GLib.Variant("(ss)", (GAZE_INTERFACE, "Config")), "(v)", PROPERTIES_INTERFACE
        ).get_child_value(0).get_variant()

    def cameras(self) -> dict[str, Any]:
        """Gaze's camera settings: rgb, ir, emitter_enabled, dark_luma, parallel."""
        config = self._config()
        if not config.get_type_string().startswith(CONFIG_PREFIX):
            raise GazeError("This Gaze version's settings aren't supported.")
        return dict(zip(CAMERA_FIELDS, config.unpack()[CAMERAS_INDEX]))

    def set_cameras(self, **changes: Any) -> None:
        """Change camera settings through Gaze, which validates and saves them."""
        config = self._config()
        signature = config.get_type_string()
        if not signature.startswith(CONFIG_PREFIX):
            raise GazeError("This Gaze version's settings aren't supported.")
        values = list(config.unpack())
        cameras = dict(zip(CAMERA_FIELDS, values[CAMERAS_INDEX]))
        cameras.update(changes)
        values[CAMERAS_INDEX] = tuple(cameras[field] for field in CAMERA_FIELDS)
        self._call(
            "Set",
            GLib.Variant("(ssv)", (GAZE_INTERFACE, "Config", GLib.Variant(signature, tuple(values)))),
            "()",
            PROPERTIES_INTERFACE,
        )

    def list_faces(self, username: str) -> list[tuple[str, int, bool, bool]]:
        try:
            values = self._call("ListFaces", GLib.Variant("(s)", (username,)), "(a(subb))")[0]
        except GazeError as error:
            # Gaze represents a user with no database entry as an error on its
            # D-Bus API. Its own CLI deliberately presents that state as an
            # empty list, so mirror that one response without hiding genuine
            # daemon, authorization, or storage failures.
            if str(error) == f"User '{username}' not found":
                return []
            raise
        return [(str(name), int(samples), bool(rgb), bool(ir)) for name, samples, rgb, ir in values]

    def delete_face(self, username: str, face_name: str) -> bool:
        return bool(
            self._call(
                "DeleteFace",
                GLib.Variant("(ss)", (username, face_name)),
                "(b)",
            )[0]
        )

    def rename_face(self, username: str, old_name: str, new_name: str) -> bool:
        return bool(
            self._call(
                "RenameFace",
                GLib.Variant("(sss)", (username, old_name, new_name)),
                "(b)",
            )[0]
        )

    def _void(self, method: str, parameters: GLib.Variant | None = None) -> None:
        self._call(method, parameters or GLib.Variant("()", ()), "()")

    def _subscribe(self, member: str, callback: Callable[[GLib.Variant], None]) -> int:
        def receive(
            _connection: Gio.DBusConnection,
            _sender: str,
            _path: str,
            _interface: str,
            _member: str,
            parameters: GLib.Variant,
            *_data: object,
        ) -> None:
            callback(parameters)

        return self.bus.signal_subscribe(
            GAZE_SERVICE,
            GAZE_INTERFACE,
            member,
            GAZE_PATH,
            None,
            Gio.DBusSignalFlags.NONE,
            receive,
        )

    def enroll(
        self,
        username: str,
        face_name: str,
        stopped: Callable[[], bool],
        width: int = PREVIEW_WIDTH,
        height: int = PREVIEW_HEIGHT,
    ) -> Iterator[dict[str, Any]]:
        events: deque[tuple[str, tuple[Any, ...]]] = deque()
        # Preview frames arrive faster than they can be shown, so only the
        # newest is kept: a queue of stale frames would grow without bound and
        # hold enrollment progress behind it. The JPEG is read straight from
        # the variant; unpack() would build a Python int per byte.
        latest_preview: list[GLib.Variant | None] = [None]

        def keep_preview(parameters: GLib.Variant) -> None:
            latest_preview[0] = parameters

        subscriptions = [
            self._subscribe("EnrollStatus", lambda parameters: events.append(("enroll", parameters.unpack()))),
            self._subscribe("FaceStatus", lambda parameters: events.append(("face", parameters.unpack()))),
            self._subscribe("PreviewFrame", keep_preview),
        ]
        claimed = False
        completed = False
        cancelled = False
        context = GLib.MainContext.default()
        deadline = time.monotonic() + ENROLL_TIMEOUT_SECONDS
        try:
            self._void("Claim", GLib.Variant("(s)", (username,)))
            claimed = True
            self._void("EnrollStart", GLib.Variant("(s)", (face_name,)))
            yield {"event": "hint", "text": "Waiting for the camera…"}
            while not completed:
                drain_main_context(context)
                preview, latest_preview[0] = latest_preview[0], None
                if preview is not None:
                    try:
                        jpeg = preview.get_child_value(0).get_data_as_bytes().get_data()
                        raw = _jpeg_to_greyscale(jpeg, width, height)
                    except (GLib.Error, GazeError, TypeError, ValueError):
                        raw = None
                    if raw is not None:
                        yield {
                            "event": "frame",
                            "w": width,
                            "h": height,
                            "data": base64.b64encode(raw).decode("ascii"),
                        }
                while events:
                    kind, values = events.popleft()
                    if kind == "face":
                        status = _normal(values[0] if values else "")
                        yield {
                            "event": "hint",
                            "text": CAPTURE_HINTS.get(status, "Hold still and look at the camera."),
                        }
                    elif kind == "enroll" and len(values) >= 6:
                        _name, progress, maximum, is_done, prompt, _remaining = values[:6]
                        prompt_key = _normal(prompt)
                        if maximum:
                            yield {
                                "event": "progress",
                                "value": min(1.0, float(progress) / float(maximum)),
                            }
                        if prompt_key in ENROLL_HINTS:
                            yield {"event": "hint", "text": ENROLL_HINTS[prompt_key]}
                        if prompt_key in FAILED_PROMPTS:
                            raise GazeError(FAILED_PROMPTS[prompt_key])
                        if prompt_key in {"cancelled", "enrollment cancelled"}:
                            cancelled = True
                            yield {"event": "cancelled"}
                            return
                        if bool(is_done):
                            if prompt_key not in {"completed", "enrollment completed"}:
                                raise GazeError("Gaze finished without saving your face.")
                            completed = True
                            break
                if completed:
                    break
                if time.monotonic() >= deadline:
                    cancelled = True
                    raise GazeError("Gaze timed out while adding your face.")
                if stopped():
                    cancelled = True
                    return
                time.sleep(0.03)
            yield {"event": "completed"}
        finally:
            for subscription in subscriptions:
                self.bus.signal_unsubscribe(subscription)
            if claimed:
                if cancelled and not completed:
                    try:
                        self._void("EnrollStop")
                    except GazeError:
                        pass
                try:
                    self._void("Release")
                except GazeError:
                    pass
