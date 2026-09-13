"""The deliberately small, local-only protocol shared by PAM and the HUD."""

from __future__ import annotations

import json
from typing import Any

MAX_MESSAGE_BYTES = 4_096
# An enrollment keeps its connection open and streams preview frames: one
# greyscale PREVIEW_WIDTH x PREVIEW_HEIGHT image, base64, per line.
MAX_STREAM_BYTES = 256 * 1_024
PREVIEW_WIDTH = 312
PREVIEW_HEIGHT = 208


def encode(message: dict[str, Any]) -> bytes:
    """Encode one newline-delimited local-socket message."""
    return (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")


def decode(raw: bytes, limit: int = MAX_MESSAGE_BYTES) -> dict[str, Any]:
    """Decode and minimally validate an inbound local-socket message."""
    if len(raw) > limit:
        raise ValueError("FaceGate message exceeds the %d byte limit" % limit)
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("FaceGate messages must be JSON objects")
    return value
