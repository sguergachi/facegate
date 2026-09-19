"""KDE tray companion for opening FaceGate's in-island configuration."""

from __future__ import annotations

import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AppIndicator3", "0.1")
from gi.repository import AppIndicator3, Gtk  # noqa: E402

from .control import open_config


class FaceGateTray:
    def __init__(self) -> None:
        self.indicator = AppIndicator3.Indicator.new(
            "facegate",
            "facegate-symbolic",
            AppIndicator3.IndicatorCategory.SYSTEM_SERVICES,
        )
        self.indicator.set_icon_theme_path("/usr/local/share/icons/hicolor/scalable/apps")
        self.indicator.set_title("FaceGate")
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)

        menu = Gtk.Menu()
        open_item = Gtk.MenuItem.new_with_label("Open FaceGate")
        open_item.connect("activate", self._open)
        menu.append(open_item)
        menu.show_all()
        self.indicator.set_menu(menu)
        self.indicator.set_secondary_activate_target(open_item)

    @staticmethod
    def _open(*_args: object) -> None:
        if not open_config(start_service=False):
            print("[facegate-tray] FaceGate service is unavailable", file=sys.stderr, flush=True)


def main() -> int:
    # Keep the wrapper (and therefore the indicator/menu) alive for the whole
    # GTK loop; otherwise Python can collect it before KDE registers the item.
    tray = FaceGateTray()
    try:
        Gtk.main()
    except KeyboardInterrupt:
        pass
    del tray
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
