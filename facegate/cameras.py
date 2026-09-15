"""Find the infrared camera and point Gaze at it.

Gaze ships with only a colour camera configured, so a fresh install never uses
IR even on a Windows Hello camera. FaceGate's installer finds the IR node here
and hands it to Gaze through Gaze's own polkit-guarded ``Config`` property:
Gaze validates, saves, and applies it. A camera the user already chose for IR
is never replaced.
"""

from __future__ import annotations

import fcntl
import os
import re
import struct
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path

VIDIOC_QUERYCAP = 0x80685600
VIDIOC_ENUM_FMT = 0xC0405602
CAPABILITY = struct.Struct("16s32s32sIII12x")
FMTDESC = struct.Struct("III32sII12x")
CAP_VIDEO_CAPTURE = 0x00000001
CAP_DEVICE_CAPS = 0x80000000
BUF_TYPE_VIDEO_CAPTURE = 1
# Luma-only formats. An IR sensor offers nothing else; a colour node always
# offers at least one colour format beside any of these.
MONO_FORMATS = {b"GREY", b"Y10 ", b"Y12 ", b"Y14 ", b"Y16 ", b"Y8I ", b"Y12I"}
SYSFS_VIDEO = Path("/sys/class/video4linux")
GAZE_WAIT_SECONDS = 15


def fourccs(node: str) -> list[bytes]:
    """The capture pixel formats a V4L2 node offers, or none if it can't capture."""
    try:
        descriptor = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return []
    try:
        caps = bytearray(CAPABILITY.size)
        fcntl.ioctl(descriptor, VIDIOC_QUERYCAP, caps)
        _driver, _card, _bus, _version, capabilities, device_caps = CAPABILITY.unpack(caps)
        if not (device_caps if capabilities & CAP_DEVICE_CAPS else capabilities) & CAP_VIDEO_CAPTURE:
            return []
        formats = []
        for index in range(64):
            description = bytearray(FMTDESC.pack(index, BUF_TYPE_VIDEO_CAPTURE, 0, b"", 0, 0))
            try:
                fcntl.ioctl(descriptor, VIDIOC_ENUM_FMT, description)
            except OSError:
                break
            formats.append(struct.pack("<I", FMTDESC.unpack(description)[4]))
        return formats
    except OSError:
        return []
    finally:
        os.close(descriptor)


def usb_spec(name: str, sysfs: Path = SYSFS_VIDEO) -> str | None:
    """``usb:VVVV:PPPP`` for a video node, the form Gaze resolves to its IR node."""
    try:
        modalias = (sysfs / name / "device" / "modalias").read_text()
    except OSError:
        return None
    match = re.match(r"usb:v([0-9A-Fa-f]{4})p([0-9A-Fa-f]{4})", modalias.strip())
    return f"usb:{match[1].lower()}:{match[2].lower()}" if match else None


def find_ir_camera(
    sysfs: Path = SYSFS_VIDEO,
    formats_of: Callable[[str], Iterable[bytes]] = fourccs,
) -> str | None:
    """The first capture node that offers only luma formats, as a Gaze source.

    A USB id survives renumbering, so it is preferred over the raw node path.
    """
    try:
        names = sorted(
            (entry.name for entry in sysfs.iterdir() if re.fullmatch(r"video\d+", entry.name)),
            key=lambda name: int(name[5:]),
        )
    except OSError:
        return None
    for name in names:
        formats = set(formats_of(f"/dev/{name}"))
        if formats and formats <= MONO_FORMATS:
            return usb_spec(name, sysfs) or f"/dev/{name}"
    return None


def configure_ir_camera(client, detect: Callable[[], str | None] = find_ir_camera) -> str:
    """Give Gaze the IR camera if it has none; returns what happened, for the installer."""
    cameras = client.cameras()
    if cameras["ir"]:
        return f"Gaze already uses {cameras['ir']} for infrared."
    source = detect()
    if not source:
        return "No infrared camera found; Gaze will use the colour camera only."
    client.set_cameras(ir=source, emitter_enabled=True)
    return f"Gaze now uses the infrared camera {source}."


def main() -> int:
    from . import faces

    deadline = time.monotonic() + GAZE_WAIT_SECONDS
    while True:
        try:
            print(configure_ir_camera(faces.GazeClient()))
            return 0
        except faces.GazeError as error:
            # gazed was just (re)started and may not own its bus name yet.
            if time.monotonic() >= deadline:
                print(f"Couldn't configure Gaze's infrared camera: {error}", file=sys.stderr)
                return 1
            time.sleep(0.5)


if __name__ == "__main__":
    raise SystemExit(main())
