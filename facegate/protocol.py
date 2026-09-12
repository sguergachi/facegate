"""The deliberately small, local-only protocol shared by PAM and the HUD."""

from __future__ import annotations

import json
from typing import Any

MAX_MESSAGE_BYTES = 4_096


def encode(message: dict[str, Any]) -> bytes:
    """Encode one newline-delimited local-socket message."""
    return (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")


def decode(raw: bytes) -> dict[str, Any]:
    """Decode and minimally validate an inbound local-socket message."""
    if len(raw) > MAX_MESSAGE_BYTES:
        raise ValueError("FaceGate message exceeds the 4 KiB limit")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("FaceGate messages must be JSON objects")
    return value
