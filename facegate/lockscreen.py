"""Let the island appear over the KDE lock screen.

KWin shows a surface above the lock screen only when its executable is
trusted (see native/facegate-hud.c) and the surface was marked before its
first buffer. The native launcher does the Wayland work; this finds it. Run
any other way (a checkout, tests), the island simply stays under the lock.
"""

from __future__ import annotations

import ctypes
from typing import Any

try:
    _allow = ctypes.CDLL(None).facegate_allow_over_lock_screen
    _allow.argtypes = [ctypes.c_void_p]
    _allow.restype = ctypes.c_int
except (AttributeError, OSError):
    _allow = None

_capsule_pointer = ctypes.pythonapi.PyCapsule_GetPointer
_capsule_pointer.argtypes = [ctypes.py_object, ctypes.c_char_p]
_capsule_pointer.restype = ctypes.c_void_p


def allow(window: Any) -> bool:
    """Mark the window's new surface as allowed over the lock screen."""
    surface = window.get_surface() if _allow else None
    if surface is None:
        return False
    return bool(_allow(_capsule_pointer(surface.__gpointer__, None)))
