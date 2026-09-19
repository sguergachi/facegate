"""The user's FaceGate preferences, shared by the island and its configuration.

Stored in ~/.config/facegate/settings.json. Nothing here affects
authentication directly: turning FaceGate off only makes prompts fall back to
their usual password dialogs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

SETTINGS_FILE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "facegate" / "settings.json"

FOLLOW_POINTER = "follow"
FIXED_MONITOR = "fixed"


def load(path: Path | None = None) -> dict[str, Any]:
    try:
        value = json.loads((path or SETTINGS_FILE).read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def save(settings: dict[str, Any], path: Path | None = None) -> None:
    target = path or SETTINGS_FILE
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(settings, indent=2, sort_keys=True))
        temporary.replace(target)
    except OSError:
        pass


def update(path: Path | None = None, **changes: Any) -> dict[str, Any]:
    """Change some keys without losing the others (onboarding, placement…)."""
    settings = load(path)
    settings.update(changes)
    save(settings, path)
    return settings


def enabled(settings: dict[str, Any]) -> bool:
    return settings.get("enabled") is not False


def placement(settings: dict[str, Any]) -> tuple[str, str]:
    """Where the island appears: following the pointer, or on one monitor."""
    monitor = settings.get("monitor")
    monitor = monitor if isinstance(monitor, str) else ""
    if settings.get("placement") == FIXED_MONITOR and monitor:
        return FIXED_MONITOR, monitor
    return FOLLOW_POINTER, monitor
