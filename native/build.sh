#!/usr/bin/env bash
# Build the native island launcher into $1 (default: ./build).
set -euo pipefail
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
out=${1:-${source_dir}/build}
mkdir -p "${out}"
# plasma-wayland-protocols ships CMake files, not a .pc file.
protocol=/usr/share/plasma-wayland-protocols/kde-lockscreen-overlay-v1.xml
wayland-scanner client-header "${protocol}" "${out}/kde-lockscreen-overlay-v1-client-protocol.h"
wayland-scanner private-code "${protocol}" "${out}/kde-lockscreen-overlay-v1-protocol.c"
# -rdynamic exports facegate_allow_over_lock_screen to the island's ctypes.
# shellcheck disable=SC2046
cc -std=c11 -O2 -Wall -Wextra -Werror -rdynamic -Wl,-z,relro,-z,now \
  -I"${out}" -o "${out}/facegate-hud" \
  "${source_dir}/facegate-hud.c" "${out}/kde-lockscreen-overlay-v1-protocol.c" \
  $(pkg-config --cflags --libs python3-embed gtk4-wayland wayland-client)
