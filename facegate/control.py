"""Small socket client used by the app launcher and tray companion."""

from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .protocol import MAX_MESSAGE_BYTES, decode, default_socket_path, encode


def request_config(welcome: bool = False, socket_path: Path | None = None) -> dict[str, Any]:
    """Ask the resident HUD to open configuration in its island."""
    path = socket_path or default_socket_path()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(2.0)
        connection.connect(str(path))
        connection.sendall(encode({"phase": "config", "welcome": welcome}))
        raw = b""
        while b"\n" not in raw:
            chunk = connection.recv(MAX_MESSAGE_BYTES + 1)
            if not chunk:
                raise ConnectionError("FaceGate closed without replying")
            raw += chunk
            if len(raw) > MAX_MESSAGE_BYTES:
                raise ValueError("FaceGate reply is too long")
    return decode(raw.split(b"\n", 1)[0])


def open_config(welcome: bool = False, start_service: bool = True) -> bool:
    """Open configuration, starting the user service once if necessary."""
    try:
        return request_config(welcome).get("ok") is True
    except (ConnectionError, FileNotFoundError, OSError, ValueError):
        if not start_service:
            return False
    subprocess.run(
        ["systemctl", "--user", "start", "facegate.service"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    for _ in range(20):
        time.sleep(0.05)
        try:
            return request_config(welcome).get("ok") is True
        except (ConnectionError, FileNotFoundError, OSError, ValueError):
            continue
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open FaceGate configuration in the top-edge island")
    parser.add_argument("--welcome", action="store_true", help="replay onboarding")
    args = parser.parse_args(argv)
    if open_config(args.welcome):
        return 0
    print("facegate-settings: the FaceGate service is unavailable", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
