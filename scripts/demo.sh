#!/usr/bin/env bash
# Runs the FaceGate HUD from this checkout in test mode: no PAM, no camera,
# and no interference with the installed service's socket.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
export WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-wayland-0}
export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
export GDK_BACKEND=wayland
# gtk4-layer-shell must load before GTK connects to Wayland.
export LD_PRELOAD=/usr/lib/libgtk4-layer-shell.so.0
export PYTHONUNBUFFERED=1
exec python3 -m facegate.edge_hud --demo
