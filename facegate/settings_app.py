"""FaceGate's in-island configuration panel.

The first time the island opens it explains FaceGate over three pages, then
walks you through adding a face. Afterwards it opens a small main menu with a
global switch and separate Face Approval and View Options destinations.

Reading the list needs no prompt. Adding or removing a face runs the root
facegate-faces helper through pkexec, so every change asks for admin approval
in the same island. Adding one hands the camera preview to the island too.
"""

from __future__ import annotations

import json
import math
import os
import shlex
import sys
import time
from typing import Any, Callable

import difflib
from dataclasses import dataclass

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, Gio, GLib, Graphene, Gsk, Gtk, Pango, PangoCairo  # noqa: E402

from . import __version__, preferences  # noqa: E402
from .edge_hud import (  # noqa: E402
    ISLAND_WIDTH,
    MARK_PROFILES,
    MarkModel,
    EASE_IN_OUT,
    EASE_OUT,
    EASE_OUT_CSS,
    Spring,
    Tween,
    clamp,
    island_path,
    mark_values,
    mix,
    paint_mark,
    reduced_motion,
    smoothstep,
)

LIST_HELPER = "/usr/local/lib/facegate/facegate-faces-list"
CHANGE_HELPER = "/usr/local/lib/facegate/facegate-faces"

# Spacing system (see DESIGN.md): an 8-point rhythm with 4-point half steps.
# Every length below is a multiple of 4; structure (margins, radii, rows,
# buttons, gaps between groups) is a multiple of 8.
GRID = 4
SPACE_XS = 4     # label to its own caption
SPACE_S = 8      # things that belong together: rows, paired buttons, title to body
SPACE_M = 12     # a control and its label, icon to text
SPACE_L = 16     # parts of one group
SPACE_XL = 24    # groups: margins, art to title, copy to content
SPACE_XXL = 32   # the top bar's inset

PANEL_WIDTH = 424
PANEL_HEIGHT = 680
# Concentric corners: the expanded island's lower radius is the primary button's radius plus
# the margin around it, and the close button sits on the same corner's centre.
PAGE_MARGIN = SPACE_XL
BUTTON_HEIGHT = 48
BUTTON_GAP = SPACE_S
PANEL_RADIUS = BUTTON_HEIGHT // 2 + PAGE_MARGIN
CLOSE_SIZE = 32
CLOSE_INSET = PANEL_RADIUS - CLOSE_SIZE // 2
CONTENT_WIDTH = PANEL_WIDTH - 2 * PAGE_MARGIN

# The list page's hero is composed exactly like an onboarding step (the same
# 144 mark, 24 to a 28 title, 8 to a one-line body), so the hand-over from
# onboarding keeps its proportions. The mark clears the close button's row.
HERO_SIZE = 144
HERO_TOP = CLOSE_INSET + CLOSE_SIZE - SPACE_S
MARK_TO_TITLE = SPACE_XL
TITLE_TO_DETAIL = SPACE_S
# With the 16 below the header, the "Faces" heading sits a group gap (24)
# under the detail.
HERO_BOTTOM = SPACE_S
# Collapsed, the header is a 96 bar: the mark shrinks to 32 at the top, the
# page margin down, and the title sits snug beneath it, the pair centred as one.
BAR_HEIGHT = 2 * PANEL_RADIUS
BAR_MARK = 32
BAR_MARK_TOP = SPACE_XL
TITLE_PX = 28
BAR_TITLE_PX = 17
HEADER_FEATHER = SPACE_L
# How far a scroll must travel one way before the header changes state.
SCROLL_INTENT = SPACE_XL
SECTION_TO_ROWS = SPACE_S
# Content fades out over the feather, then clears the button by the gap.
BOTTOM_FEATHER = SPACE_XXL
BUTTON_CLEARANCE = SPACE_L
BOTTOM_BAR = BOTTOM_FEATHER + BUTTON_CLEARANCE + BUTTON_HEIGHT + PAGE_MARGIN

# Rows: a 40 avatar and 40 row button inside 12 padding make a 64 row whose
# corners are concentric with both (40 / 2 + 12 = 32).
ROW_PADDING = SPACE_M
AVATAR_SIZE = 40
ROW_HEIGHT = AVATAR_SIZE + 2 * ROW_PADDING
ROW_RADIUS = AVATAR_SIZE // 2 + ROW_PADDING
ROW_GAP = SPACE_S
# Text that leads a row, with no icon before it, starts clear of the rounded
# end: at the title's height the curve reaches ~6 pt in, so 24 leaves the same
# visible breathing room a 40 icon gets from 12 padding. Section headings share it.
ROW_TEXT_INSET = SPACE_XL
ROW_REVEAL_MS = 260
CONFIRM_SECONDS = 4
RESULT_SECONDS = 1.6
BACK_TARGETS = {"how": "welcome", "privacy": "how", "added": "privacy"}

# Home and preference pages deliberately reuse the face-row proportions: a
# 40-point control inside 12-point padding makes one calm, concentric 64-point
# row. Separate groups remain 24 points apart.
PREFERENCE_GROUP_GAP = SPACE_XL
PREFERENCE_ICON_SIZE = AVATAR_SIZE
# Trailing controls sit on the centre of the row's rounded end.
CHEVRON_SIZE = 24
RADIO_SIZE = 16
SWITCH_WIDTH = 52
SWITCH_HEIGHT = 32
SWITCH_THUMB = 28


@dataclass(frozen=True)
class MonitorInfo:
    """The stable identity and human-readable copy for one connected output."""

    connector: str
    title: str
    detail: str


def _monitor_value(monitor: Any, getter: str) -> str:
    try:
        value = getattr(monitor, getter)()
    except (AttributeError, TypeError):
        return ""
    return value.strip() if isinstance(value, str) else ""


def monitor_info(monitor: Any) -> MonitorInfo | None:
    """Turn a GDK monitor into copy without depending on a particular vendor."""
    connector = _monitor_value(monitor, "get_connector")
    if not connector:
        # KWin placement is connector-based. An output without that stable key
        # cannot honestly be offered as a lock target.
        return None
    title = _monitor_value(monitor, "get_description") or _monitor_value(monitor, "get_model") or connector
    try:
        geometry = monitor.get_geometry()
        resolution = "%d × %d" % (geometry.width, geometry.height)
    except (AttributeError, TypeError):
        resolution = "Display"
    return MonitorInfo(connector, title, "%s · %s" % (resolution, connector))


def available_monitors(display: Gdk.Display | None = None) -> list[MonitorInfo]:
    display = display or Gdk.Display.get_default()
    model = display.get_monitors() if display else None
    choices: list[MonitorInfo] = []
    for index in range(model.get_n_items() if model else 0):
        info = monitor_info(model.get_item(index))
        if info:
            choices.append(info)
    return choices


def placement_summary(settings: dict[str, Any], monitors: list[MonitorInfo]) -> str:
    mode, connector = preferences.placement(settings)
    if mode == preferences.FOLLOW_POINTER:
        return "Follows the pointer"
    for monitor in monitors:
        if monitor.connector == connector:
            return "Locked to %s" % monitor.title
    return "Locked to %s · not connected" % connector


def setup_auth_copy(faces_loaded: bool, has_faces: bool) -> tuple[str, str, str]:
    """Title, body, and compact status while admin authentication is pending."""
    if faces_loaded and not has_faces:
        return (
            "Enter your sudo password",
            "Your first face can’t approve itself, so FaceGate asks for your password first.",
            "Enter your sudo password to add your first face.",
        )
    return (
        "Authenticate to add a face",
        "Adding a face needs admin approval first.",
        "Approve to add a face.",
    )


def helper_command(args: list[str]) -> list[str]:
    # Development can point both helpers at another launcher taking these args.
    override = os.environ.get("FACEGATE_FACES_HELPER")
    if override:
        return shlex.split(override) + args
    if args == ["list"]:
        return ["pkexec", LIST_HELPER]
    return ["pkexec", CHANGE_HELPER] + args


def rounded_rect(context: Any, x: float, y: float, width: float, height: float, radius: float) -> None:
    radius = min(radius, width / 2, height / 2)
    context.new_sub_path()
    context.arc(x + width - radius, y + radius, radius, -1.5708, 0)
    context.arc(x + width - radius, y + height - radius, radius, 0, 1.5708)
    context.arc(x + radius, y + height - radius, radius, 1.5708, 3.1416)
    context.arc(x + radius, y + radius, radius, 3.1416, 4.7124)
    context.close_path()


def advance_mark(model: MarkModel, elapsed: float) -> None:
    model.elapsed += elapsed
    model.state_elapsed += elapsed
    model.spin = (model.spin + mark_values(model)[-1] * elapsed) % 1.0
    model.transition = min(1.0, model.transition + elapsed / 0.55)


class Mark(Gtk.DrawingArea):
    """The island's presence mark, living at any size."""

    def __init__(self, size: int, animated: bool = True) -> None:
        super().__init__()
        self.set_content_width(size)
        self.set_content_height(size)
        self.model = MarkModel()
        self.last: int | None = None
        self.set_draw_func(lambda _area, context, width, _height: paint_mark(context, width, self.model))
        if animated:
            self.add_tick_callback(self._tick)

    def set_state(self, state: str) -> None:
        model = self.model
        if state == model.state or state not in MARK_PROFILES:
            return
        model.origin = mark_values(model)
        model.state = state
        model.transition = 0.0
        model.state_elapsed = 0.0

    def _tick(self, _widget: Gtk.Widget, clock: Gdk.FrameClock) -> bool:
        now = clock.get_frame_time()
        elapsed = 0.0 if self.last is None else min((now - self.last) / 1_000_000, 1 / 30)
        self.last = now
        advance_mark(self.model, elapsed)
        self.queue_draw()
        return GLib.SOURCE_CONTINUE


class Fade:
    """Cross-fade a label to new text on the frame clock: quick out, swap, in."""

    OUT_SECONDS = 0.1
    IN_SECONDS = 0.18

    def __init__(self, label: Gtk.Label) -> None:
        self.label = label
        self.target = label.get_text()
        self.ticking = False
        self.phase = "idle"
        self.elapsed = 0.0
        self.last: int | None = None

    def to(self, text: str) -> None:
        self.target = text
        if text == self.label.get_text() and self.phase == "idle":
            return
        if not self.label.get_mapped():
            self.label.set_text(text)
            self.label.set_opacity(1.0)
            return
        if self.phase != "out":
            # Leave from whatever opacity is showing, so interruptions never jump.
            self.phase, self.elapsed = "out", (1 - self.label.get_opacity()) * self.OUT_SECONDS
        if not self.ticking:
            self.ticking, self.last = True, None
            self.label.add_tick_callback(self._tick)

    def _tick(self, _widget: Gtk.Widget, clock: Gdk.FrameClock) -> bool:
        now = clock.get_frame_time()
        self.elapsed += 0.0 if self.last is None else min((now - self.last) / 1_000_000, 1 / 30)
        self.last = now
        if self.phase == "out":
            progress = clamp(self.elapsed / self.OUT_SECONDS)
            self.label.set_opacity(1 - EASE_OUT(progress))
            if progress >= 1:
                self.label.set_text(self.target)
                self.phase, self.elapsed = "in", 0.0
            return GLib.SOURCE_CONTINUE
        progress = clamp(self.elapsed / self.IN_SECONDS)
        self.label.set_opacity(EASE_OUT(progress))
        if progress < 1:
            return GLib.SOURCE_CONTINUE
        self.phase, self.ticking = "idle", False
        if self.label.get_text() != self.target:
            self.to(self.target)
        return GLib.SOURCE_REMOVE


class HelperRun:
    """One facegate-faces run: its JSON events, then its exit status."""

    def __init__(self, args: list[str], on_event: Callable[[dict[str, Any]], None], on_exit: Callable[[int], None]) -> None:
        self.args = args
        self.on_event = on_event
        self.on_exit = on_exit
        self.process: Gio.Subprocess | None = None
        self.stdin: Gio.OutputStream | None = None

    def start(self) -> None:
        flags = Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDOUT_PIPE
        try:
            self.process = Gio.Subprocess.new(helper_command(self.args), flags)
        except GLib.Error as error:
            print("[facegate-settings] cannot start helper: %s" % error.message, file=sys.stderr)
            GLib.idle_add(lambda: (self.on_exit(127), False)[1])
            return
        self.stdin = self.process.get_stdin_pipe()
        self._read(Gio.DataInputStream.new(self.process.get_stdout_pipe()))
        self.process.wait_async(None, self._exited)

    def _read(self, stream: Gio.DataInputStream) -> None:
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self._on_line)

    def _on_line(self, stream: Gio.DataInputStream, result: Gio.AsyncResult) -> None:
        try:
            line, _length = stream.read_line_finish_utf8(result)
        except GLib.Error:
            return
        if line is None:
            return
        try:
            event = json.loads(line)
        except ValueError:
            event = None
        if isinstance(event, dict):
            self.on_event(event)
        self._read(stream)

    def _exited(self, process: Gio.Subprocess, result: Gio.AsyncResult) -> None:
        try:
            process.wait_finish(result)
            status = process.get_exit_status() if process.get_if_exited() else 1
        except GLib.Error:
            status = 1
        self.process = self.stdin = None
        # Let any last buffered events land before reporting the exit.
        GLib.timeout_add(50, lambda: (self.on_exit(status), False)[1])

    def send(self, command: dict[str, Any]) -> None:
        if not self.stdin:
            return
        try:
            self.stdin.write_all((json.dumps(command) + "\n").encode("utf-8"), None)
        except GLib.Error:
            pass

    def stop(self) -> None:
        if self.stdin:
            # The helper runs as root; closing its input is how it is told to go.
            self.send({"cmd": "cancel"})
            self.stdin.close(None)

    def abort(self) -> None:
        """Stop pkexec while it is still waiting for admin approval."""
        if self.process:
            self.process.force_exit()


class IslandBridge:
    """Hands the panel's scan to the island it lives in.

    Panel and island share one GTK main loop, so this is a direct call, never
    a socket round-trip: a blocking handshake would wait for a reply that
    this same loop has to dispatch. The scan is announced before admin
    approval, so the approval itself turns into the preview.
    """

    def __init__(self, hud: Any, on_cancel: Callable[[], None]) -> None:
        self.hud = hud
        self.on_cancel = on_cancel
        self.token: object | None = None

    def expect(self) -> None:
        self.close()
        self.token = self.hud.expect_enrollment(self.on_cancel)

    def open(self) -> bool:
        return self.token is not None and self.hud.begin_embedded_enrollment(self.token)

    def send(self, message: dict[str, Any]) -> None:
        if self.token is not None:
            self.hud.enrollment_message(self.token, message)

    def close(self) -> None:
        token, self.token = self.token, None
        if token is not None:
            self.hud.end_enrollment(token)


def draw_close_glyph(area: Gtk.DrawingArea, context: Any, width: int, height: int) -> None:
    color = area.get_color()
    context.set_source_rgba(color.red, color.green, color.blue, color.alpha)
    context.set_line_width(1.75)
    context.set_line_cap(cairo.LINE_CAP_ROUND)
    half = 4.5
    context.move_to(width / 2 - half, height / 2 - half)
    context.line_to(width / 2 + half, height / 2 + half)
    context.move_to(width / 2 + half, height / 2 - half)
    context.line_to(width / 2 - half, height / 2 + half)
    context.stroke()


def draw_back_glyph(area: Gtk.DrawingArea, context: Any, width: int, height: int) -> None:
    """A restrained chevron with the close glyph's exact visual weight."""
    color = area.get_color()
    context.set_source_rgba(color.red, color.green, color.blue, color.alpha)
    context.set_line_width(1.75)
    context.set_line_cap(cairo.LINE_CAP_ROUND)
    context.set_line_join(cairo.LINE_JOIN_ROUND)
    context.move_to(width / 2 + 2.5, height / 2 - 5)
    context.line_to(width / 2 - 2.5, height / 2)
    context.line_to(width / 2 + 2.5, height / 2 + 5)
    context.stroke()


def label(text: str, css_class: str, wrap: bool = False, xalign: float = 0.5) -> Gtk.Label:
    widget = Gtk.Label(label=text, xalign=xalign)
    widget.add_css_class(css_class)
    if wrap:
        widget.set_wrap(True)
        widget.set_natural_wrap_mode(Gtk.NaturalWrapMode.WORD)
        if xalign == 0.5:
            widget.set_justify(Gtk.Justification.CENTER)
    return widget


# Settings controls -----------------------------------------------------------


def draw_chevron_glyph(area: Gtk.DrawingArea, context: Any, width: int, height: int) -> None:
    color = area.get_color()
    context.set_source_rgba(color.red, color.green, color.blue, color.alpha)
    context.set_line_width(1.75)
    context.set_line_cap(cairo.LINE_CAP_ROUND)
    context.set_line_join(cairo.LINE_JOIN_ROUND)
    context.move_to(width / 2 - 2.5, height / 2 - 5)
    context.line_to(width / 2 + 2.5, height / 2)
    context.line_to(width / 2 - 2.5, height / 2 + 5)
    context.stroke()


def row_copy(title: str, detail: str) -> tuple[Gtk.Box, Gtk.Label]:
    copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    copy.set_valign(Gtk.Align.CENTER)
    copy.set_hexpand(True)
    title_label = label(title, "row-title", xalign=0)
    title_label.set_ellipsize(Pango.EllipsizeMode.END)
    title_label.set_single_line_mode(True)
    detail_label = label(detail, "row-detail", xalign=0)
    detail_label.set_ellipsize(Pango.EllipsizeMode.END)
    detail_label.set_single_line_mode(True)
    copy.append(title_label)
    copy.append(detail_label)
    return copy, detail_label


def pill_layout(title: str, detail: str, leading: Gtk.Widget | None = None, trailing: Gtk.Widget | None = None, trailing_radius: float = 0.0) -> tuple[Gtk.Box, Gtk.Label]:
    """The one layout every 64-point settings row uses (inside 12 padding).

    A leading 40 icon sits in the padding, concentric with the rounded end;
    text with nothing before it starts 24 in. A trailing control is centred on
    the other end's centre, 32 from the edge, whatever its size.
    """
    content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=SPACE_M)
    if leading:
        leading.set_valign(Gtk.Align.CENTER)
        content.append(leading)
    copy, detail_label = row_copy(title, detail)
    if not leading:
        copy.set_margin_start(ROW_TEXT_INSET - ROW_PADDING)
    content.append(copy)
    if trailing:
        trailing.set_valign(Gtk.Align.CENTER)
        trailing.set_margin_end(round(ROW_RADIUS - ROW_PADDING - trailing_radius))
        content.append(trailing)
    return content, detail_label


class NavigationRow(Gtk.Button):
    """A full-row settings destination: icon, title and detail, chevron."""

    def __init__(self, title: str, icon: Gtk.Widget, on_click: Callable[[], None]) -> None:
        super().__init__()
        self.add_css_class("preference-row")
        self.set_hexpand(True)
        self.set_focus_on_click(False)
        self.update_property([Gtk.AccessibleProperty.LABEL], [title])
        self.connect("clicked", lambda *_: on_click())

        chevron = Gtk.DrawingArea()
        chevron.add_css_class("chevron")
        chevron.set_content_width(CHEVRON_SIZE)
        chevron.set_content_height(CHEVRON_SIZE)
        chevron.set_draw_func(draw_chevron_glyph)
        content, self.detail = pill_layout(title, "", icon, chevron, CHEVRON_SIZE / 2)
        self.set_child(content)


class GlobalSwitch(Gtk.Button):
    """A 52 × 32 switch whose thumb travels on a spring, drawn rather than laid out."""

    def __init__(self, on_change: Callable[[bool], None]) -> None:
        super().__init__(accessible_role=Gtk.AccessibleRole.SWITCH)
        self.add_css_class("global-switch")
        self.set_focus_on_click(False)
        self.on_change = on_change
        self.active = False
        self.position = Spring(0.0)
        self.position.configure(0.35, 1.0)
        self.last: int | None = None
        area = Gtk.DrawingArea()
        area.set_content_width(SWITCH_WIDTH)
        area.set_content_height(SWITCH_HEIGHT)
        area.set_draw_func(self._draw)
        self.area = area
        self.set_child(area)
        self.connect("clicked", lambda *_: self._flip())
        self.add_tick_callback(self._tick)

    def set_active(self, active: bool, animate: bool = False) -> None:
        self.active = active
        self.position.target = 1.0 if active else 0.0
        if not animate or reduced_motion():
            self.position.value, self.position.velocity = self.position.target, 0.0
        self.update_state([Gtk.AccessibleState.CHECKED], [int(Gtk.AccessibleTristate.TRUE if active else Gtk.AccessibleTristate.FALSE)])
        self.area.queue_draw()

    def _flip(self) -> None:
        self.set_active(not self.active, animate=True)
        self.on_change(self.active)

    def _tick(self, _widget: Gtk.Widget, clock: Gdk.FrameClock) -> bool:
        frame = clock.get_frame_time()
        elapsed = 0.0 if self.last is None else min((frame - self.last) / 1_000_000, 1 / 30)
        self.last = frame
        if abs(self.position.value - self.position.target) > 0.001 or abs(self.position.velocity) > 0.001:
            self.position.step(elapsed)
            self.area.queue_draw()
        return GLib.SOURCE_CONTINUE

    def _draw(self, _area: Gtk.DrawingArea, context: Any, width: int, height: int) -> None:
        amount = clamp(self.position.value)
        # Off is a pressed fill; on is success green, because FaceGate is working.
        off, on = (0.23, 0.23, 0.24), (0.19, 0.82, 0.35)
        rounded_rect(context, 0, 0, width, height, height / 2)
        context.set_source_rgb(*(mix(off[index], on[index], amount) for index in range(3)))
        context.fill()
        inset = (height - SWITCH_THUMB) / 2
        x = mix(inset + SWITCH_THUMB / 2, width - inset - SWITCH_THUMB / 2, amount)
        context.arc(x, height / 2, SWITCH_THUMB / 2, 0, math.tau)
        context.set_source_rgb(0.96, 0.96, 0.97)
        context.fill()


class ToggleRow(Gtk.Box):
    """The global FaceGate switch."""

    def __init__(self, on_change: Callable[[bool], None]) -> None:
        super().__init__()
        self.add_css_class("preference-row")
        self.set_hexpand(True)
        self.switch = GlobalSwitch(on_change)
        self.switch.update_property([Gtk.AccessibleProperty.LABEL], ["Use FaceGate"])
        # The thumb, not the track, is what sits on the end cap's centre.
        content, self.detail = pill_layout("Use FaceGate", "", trailing=self.switch, trailing_radius=SWITCH_HEIGHT / 2)
        content.set_hexpand(True)
        self.append(content)


class ChoiceIndicator(Gtk.DrawingArea):
    """A paint-only radio mark, so choosing never changes layout."""

    def __init__(self, selected: bool) -> None:
        super().__init__()
        self.set_content_width(CHEVRON_SIZE)
        self.set_content_height(CHEVRON_SIZE)
        self.selected = selected
        self.set_draw_func(self._draw)

    def set_selected(self, selected: bool) -> None:
        if selected != self.selected:
            self.selected = selected
            self.queue_draw()

    def _draw(self, _area: Gtk.DrawingArea, context: Any, width: int, height: int) -> None:
        context.arc(width / 2, height / 2, RADIO_SIZE / 2 - 0.75, 0, math.tau)
        context.set_line_width(1.5)
        context.set_source_rgba(0.96, 0.96, 0.97, 1.0 if self.selected else 0.24)
        context.stroke()
        if self.selected:
            context.arc(width / 2, height / 2, RADIO_SIZE / 4, 0, math.tau)
            context.fill()


class ChoiceRow(Gtk.Button):
    """One immediate, mutually exclusive placement choice."""

    def __init__(self, title: str, detail: str, selected: bool, on_click: Callable[[], None]) -> None:
        super().__init__()
        self.add_css_class("preference-row")
        self.set_hexpand(True)
        self.set_focus_on_click(False)
        self.update_property([Gtk.AccessibleProperty.LABEL], [title])
        self.connect("clicked", lambda *_: on_click())

        self.indicator = ChoiceIndicator(selected)
        content, _detail = pill_layout(title, detail, trailing=self.indicator, trailing_radius=CHEVRON_SIZE / 2)
        self.set_child(content)


class DisplayGlyph(Gtk.DrawingArea):
    """A display with the island at its top edge, drawn in the mark's language.

    It shares the mark's 72-unit square, faint outline and white features, so
    it can stand in for the mark as hero art, in the collapsed bar and as a
    row icon.
    """

    def __init__(self, size: int) -> None:
        super().__init__()
        self.set_content_width(size)
        self.set_content_height(size)
        self.set_draw_func(self._draw)

    @staticmethod
    def _draw(_area: Gtk.DrawingArea, context: Any, width: int, _height: int) -> None:
        context.scale(width / 72, width / 72)
        context.set_line_width(1.5)
        context.set_line_cap(cairo.LINE_CAP_ROUND)
        # The screen's bottom lands where the mark's ring does, so the art
        # sits the same distance above its title.
        rounded_rect(context, 10, 18, 52, 34, 6)
        context.move_to(36, 52)
        context.line_to(36, 60)
        context.move_to(27, 60)
        context.line_to(45, 60)
        context.set_source_rgba(0.96, 0.96, 0.97, 0.4)
        context.stroke()
        # The island, hanging from the screen's top edge.
        rounded_rect(context, 28, 18, 16, 6, 3)
        context.rectangle(28, 18, 16, 3)
        context.set_source_rgb(0.96, 0.96, 0.97)
        context.fill()


# Face rows -----------------------------------------------------------------


class FaceRow(Gtk.Revealer):
    def __init__(self, face: dict[str, Any], on_remove: Callable[["FaceRow"], None]) -> None:
        super().__init__()
        self.face_id = int(face["id"])
        self.on_remove = on_remove
        self.revert: int | None = None
        self.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.set_transition_duration(ROW_REVEAL_MS)

        self.actions = Gtk.Stack()
        self.actions.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.actions.set_transition_duration(180)
        self.actions.set_hhomogeneous(False)
        self.actions.set_interpolate_size(True)
        self.actions.set_valign(Gtk.Align.CENTER)
        trash = Gtk.Button.new_from_icon_name("user-trash-symbolic")
        trash.add_css_class("icon")
        trash.set_tooltip_text("Remove this face")
        trash.connect("clicked", lambda *_: self._ask())
        confirm = Gtk.Button(label="Remove")
        confirm.add_css_class("destructive")
        confirm.connect("clicked", lambda *_: self._confirm())
        spinner = Gtk.Spinner()
        spinner.set_size_request(AVATAR_SIZE, AVATAR_SIZE)
        spinner.start()
        self.actions.add_named(trash, "trash")
        self.actions.add_named(confirm, "confirm")
        self.actions.add_named(spinner, "busy")
        added = face.get("time") or 0
        row, _detail = pill_layout(
            face.get("label") or "Face",
            time.strftime("Added %b %-d, %Y", time.localtime(added)) if added else "Added with Howdy",
            Mark(AVATAR_SIZE, animated=False),
            self.actions,
            AVATAR_SIZE / 2,
        )
        row.add_css_class("face-row")
        self.row = row
        # Rows are spaced by their own bottom margin, so the gap collapses with them.
        holder = Gtk.Box()
        holder.set_margin_bottom(ROW_GAP)
        holder.append(row)
        row.set_hexpand(True)
        self.set_child(holder)

    def _ask(self) -> None:
        self.actions.set_visible_child_name("confirm")
        self.revert = GLib.timeout_add_seconds(CONFIRM_SECONDS, self._settle)

    def _settle(self) -> bool:
        self.revert = None
        self.actions.set_visible_child_name("trash")
        return False

    def _confirm(self) -> None:
        if self.revert:
            GLib.source_remove(self.revert)
            self.revert = None
        self.on_remove(self)

    def set_busy(self, busy: bool) -> None:
        self.actions.set_visible_child_name("busy" if busy else "trash")
        self.row.set_sensitive(not busy)

    def celebrate(self) -> None:
        """A new face arrives lit, then settles to the resting row colour."""
        self.row.add_css_class("fresh")
        GLib.timeout_add(1_200, lambda: (self.row.remove_css_class("fresh"), False)[1])


# Onboarding illustration ------------------------------------------------------

STORY_WIDTH = PANEL_WIDTH - 2 * PAGE_MARGIN
STORY_HEIGHT = 176
STORY_SCALE = 0.6
# When each phase starts in the loop, the island height it springs to, and the
# step it illustrates (1-based, 0 for none).
STORY = (
    (0.0, "hidden", 0, 0),
    (0.4, "approval", 286, 1),
    (2.8, "scanning", 168, 2),
    (4.3, "success", 168, 3),
    (5.8, "hidden", 0, 3),
)
STORY_SECONDS = 7.0


class IslandStory(Gtk.DrawingArea):
    """A miniature desktop whose island asks, scans, and verifies, on a loop."""

    def __init__(self, on_step: Callable[[int], None]) -> None:
        super().__init__()
        self.set_content_width(STORY_WIDTH)
        self.set_content_height(STORY_HEIGHT)
        self.on_step = on_step
        self.clock = 0.0
        self.last: int | None = None
        self.phase = -1
        self.mark = MarkModel()
        self.width = Spring(124.0)
        self.height = Spring(0.0)
        self.content = 0.0
        self.set_draw_func(self._draw)
        self.add_tick_callback(self._tick)

    def restart(self) -> None:
        self.clock = 0.0
        self.phase = -1
        self.height.value = self.height.velocity = 0.0
        self.width.value, self.width.velocity = 124.0, 0.0
        self.content = 0.0

    def _tick(self, _widget: Gtk.Widget, frame_clock: Gdk.FrameClock) -> bool:
        now = frame_clock.get_frame_time()
        elapsed = 0.0 if self.last is None else min((now - self.last) / 1_000_000, 1 / 30)
        self.last = now
        self.clock = (self.clock + elapsed) % STORY_SECONDS
        index = max(i for i, entry in enumerate(STORY) if entry[0] <= self.clock)
        if index != self.phase:
            self.phase = index
            _start, state, height, step = STORY[index]
            visible = state != "hidden"
            self.width.configure(0.5, 0.8 if visible else 1.0)
            self.height.configure(0.56 if visible else 0.4, 0.82 if visible else 1.0)
            self.width.target = ISLAND_WIDTH if visible else 124.0
            self.height.target = height
            if visible and self.mark.state != state:
                blend = self.content > 0.5
                self.mark.origin = mark_values(self.mark) if blend else None
                self.mark.state = state
                self.mark.transition = 0.0 if blend else 1.0
                self.mark.state_elapsed = 0.0
            self.on_step(step)
        self.width.step(elapsed)
        self.height.step(elapsed)
        visible = STORY[self.phase][1] != "hidden"
        self.content = clamp(self.content + (elapsed / 0.3 if visible else -elapsed / 0.12))
        advance_mark(self.mark, elapsed)
        self.queue_draw()
        return GLib.SOURCE_CONTINUE

    def _draw(self, _area: Gtk.DrawingArea, context: Any, width: int, height: int) -> None:
        # The desktop: a rounded screen with a faint wallpaper glow.
        rounded_rect(context, 0.5, 0.5, width - 1, height - 1, 22)
        context.save()
        context.clip_preserve()
        glow = cairo.LinearGradient(0, 0, 0, height)
        glow.add_color_stop_rgb(0, 0.09, 0.10, 0.13)
        glow.add_color_stop_rgb(1, 0.05, 0.05, 0.07)
        context.set_source(glow)
        context.fill()
        # A terminal window waiting on sudo.
        rounded_rect(context, 22, height - 70, 150, 52, 9)
        context.set_source_rgba(1, 1, 1, 0.06)
        context.fill()
        for y, bar_width, alpha in ((height - 56, 96, 0.28), (height - 42, 60, 0.16)):
            rounded_rect(context, 34, y, bar_width, 6, 3)
            context.set_source_rgba(1, 1, 1, alpha)
            context.fill()

        state = STORY[self.phase][1] if self.phase >= 0 else "hidden"
        island_height = max(0.0, self.height.value)
        context.translate(width / 2, 0)
        context.scale(STORY_SCALE, STORY_SCALE)
        island_path(context, 0, self.width.value, island_height)
        context.set_source_rgb(0, 0, 0)
        context.fill()

        alpha = smoothstep(self.content)
        if alpha > 0.01 and island_height > 40:
            context.push_group()
            context.save()
            context.translate(-36, 16)
            paint_mark(context, 72, self.mark)
            context.restore()
            bars = [(96, 150, 13, 0.9), (120, 196, 9, 0.4)]
            if self.mark.state == "success":
                bars = [(96, 96, 13, 0.9), (120, 150, 9, 0.4)]
            for y, bar_width, bar_height, bar_alpha in bars:
                rounded_rect(context, -bar_width / 2, y, bar_width, bar_height, bar_height / 2)
                context.set_source_rgba(1, 1, 1, bar_alpha)
                context.fill()
            if state == "approval":
                for x, color in ((-156, (0.11, 0.11, 0.12)), (4, (0.96, 0.96, 0.97))):
                    rounded_rect(context, x, 160, 152, 44, 22)
                    context.set_source_rgb(*color)
                    context.fill()
            context.pop_group_to_source()
            context.paint_with_alpha(alpha)
        context.restore()
        rounded_rect(context, 0.5, 0.5, width - 1, height - 1, 22)
        context.set_source_rgba(1, 1, 1, 0.08)
        context.set_line_width(1)
        context.stroke()


# Collapsing header -------------------------------------------------------------


class CollapsingHeader:
    """A large mark and title that become a compact bar once content scrolls.

    The header is only ever full or a bar, never in between. Scrolling down
    collapses it and scrolling back into the top of the list expands it; each
    change runs on a spring while the list travels with it, so rows arrive
    exactly below the header. A list too short to reach the bar keeps the
    full header and scrolls beneath it.

    The visual layer never takes input, so the list scrolls beneath it; a
    transparent scroll target the size of the header sits above it.
    """

    def __init__(self, scroller: Gtk.ScrolledWindow, title: str = "Face Approval", art: Gtk.Widget | None = None) -> None:
        self.scroller = scroller
        self.visual = Gtk.Fixed()
        self.visual.set_can_target(False)
        self.visual.set_valign(Gtk.Align.START)
        self.backdrop = Gtk.DrawingArea()
        self.backdrop.set_draw_func(self._draw_backdrop)
        # The art (the mark, unless a page brings its own 144-point square)
        # shrinks into the bar beside the title.
        self.mark = art or Mark(HERO_SIZE)
        self.title = label(title, "hero-title")
        # One line, always: the header's height never changes with its copy.
        self.detail = label("", "hero-detail")
        self.detail.set_ellipsize(Pango.EllipsizeMode.END)
        self.detail.set_size_request(PANEL_WIDTH - 2 * PAGE_MARGIN, -1)
        self.detail_fade = Fade(self.detail)
        for widget in (self.backdrop, self.mark, self.title, self.detail):
            self.visual.put(widget, 0, 0)

        self.title_width = self.title.measure(Gtk.Orientation.HORIZONTAL, -1)[1]
        self.title_height = self.title.measure(Gtk.Orientation.VERTICAL, self.title_width)[1]
        probe = label("One line", "hero-detail")
        detail_height = probe.measure(Gtk.Orientation.VERTICAL, -1)[1]
        self.title_top = HERO_TOP + HERO_SIZE + MARK_TO_TITLE
        self.detail_top = self.title_top + self.title_height + TITLE_TO_DETAIL
        self.full = self.detail_top + detail_height + HERO_BOTTOM
        self.backdrop.set_size_request(PANEL_WIDTH, self.full + HEADER_FEATHER)
        self.progress = 0.0
        self.height = float(self.full)
        self.collapsed = False
        # While the spring runs it owns the scroll offset, travelling from one
        # rest offset to the other in step with the header.
        self.spring = Spring(0.0)
        self.spring.configure(0.35, 1.0)
        self.steering = False
        self.writing = False
        self.journey = (0.0, 0.0, 0.0)
        self.last_offset = self.anchor = 0.0
        self.direction = 0
        self.last_frame: int | None = None

        # The visual layer ignores input; this forwards scrolling over it.
        self.scroll_target = Gtk.Box()
        self.scroll_target.set_valign(Gtk.Align.START)
        scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._forward_scroll)
        self.scroll_target.add_controller(scroll)

        adjustment = scroller.get_vadjustment()
        adjustment.connect("value-changed", self._on_scroll)
        # A list that shrinks (a face removed) may no longer be able to hold the bar.
        adjustment.connect("changed", self._on_scroll)
        self.update(0.0)

    def _forward_scroll(self, controller: Gtk.EventControllerScroll, _dx: float, dy: float) -> bool:
        adjustment = self.scroller.get_vadjustment()
        step = dy * 48 if controller.get_unit() == Gdk.ScrollUnit.WHEEL else dy
        adjustment.set_value(adjustment.get_value() + step)
        return True

    def _on_scroll(self, adjustment: Gtk.Adjustment) -> None:
        offset = adjustment.get_value()
        if self.writing or self.steering:
            # The header's own travel is not a gesture; intent starts afresh after it.
            self.last_offset = self.anchor = offset
            self.direction = 0
            return
        direction = (offset > self.last_offset) - (offset < self.last_offset)
        if direction and direction != self.direction:
            # A new gesture is measured from where it turned.
            self.direction, self.anchor = direction, self.last_offset
        self.last_offset = offset
        travel = offset - self.anchor
        # Collapsing hides 196 of header, so the list moves by exactly that
        # from where the gesture began: the rows in view stay in view.
        point = self.full - BAR_HEIGHT
        limit = adjustment.get_upper() - adjustment.get_page_size()
        reachable = limit >= point
        if not self.collapsed and reachable and travel >= SCROLL_INTENT:
            self._switch(True, min(limit, self.anchor + point))
        elif self.collapsed and (not reachable or travel <= -SCROLL_INTENT):
            self._switch(False, max(0.0, self.anchor - point))

    def _switch(self, collapsed: bool, offset: float) -> None:
        self.collapsed = collapsed
        self.spring.target = 1.0 if collapsed else 0.0
        self.journey = (self.scroller.get_vadjustment().get_value(), offset, self.spring.value)
        if reduced_motion() or not self.visual.get_mapped():
            self.spring.value, self.spring.velocity = self.spring.target, 0.0
            self._steer()
        elif not self.steering:
            self.steering = True
            self.last_frame = None
            self.visual.add_tick_callback(self._tick)

    def _tick(self, _widget: Gtk.Widget, clock: Gdk.FrameClock) -> bool:
        frame = clock.get_frame_time()
        elapsed = min((frame - self.last_frame) / 1_000_000, 1 / 30) if self.last_frame else 0.0
        self.last_frame = frame
        self.spring.step(elapsed)
        self._steer()
        # Frames are only requested while the header is changing.
        return GLib.SOURCE_CONTINUE if self.steering else GLib.SOURCE_REMOVE

    def _steer(self) -> None:
        start_offset, end_offset, start_progress = self.journey
        target = self.spring.target
        settled = abs(self.spring.value - target) < 0.002 and abs(self.spring.velocity) < 0.01
        if settled:
            self.spring.value, self.spring.velocity = target, 0.0
            self.steering = False
        span = target - start_progress
        amount = 1.0 if settled or abs(span) < 1e-6 else clamp((self.spring.value - start_progress) / span)
        self.writing = True
        self.scroller.get_vadjustment().set_value(mix(start_offset, end_offset, amount))
        self.writing = False
        self.update(self.spring.value)

    def update(self, progress: float) -> None:
        t = clamp(progress)
        self.progress = t
        width = PANEL_WIDTH
        bar_scale = BAR_TITLE_PX / TITLE_PX
        title_scale = mix(1.0, bar_scale, t)
        full_title_y = self.title_top + self.title_height / 2
        # Everything stays centred; only size and height change.
        title_y = mix(full_title_y, BAR_MARK_TOP + BAR_MARK + self.title_height * bar_scale / 2, t)
        mark_size = mix(HERO_SIZE, BAR_MARK, t)
        mark_y = mix(HERO_TOP + HERO_SIZE / 2, BAR_MARK_TOP + BAR_MARK / 2, t)
        self._place(self.mark, (width - mark_size) / 2, mark_y - mark_size / 2, mark_size / HERO_SIZE)
        self._place(self.title, (width - self.title_width * title_scale) / 2, title_y - self.title_height * title_scale / 2, title_scale)

        # The detail rides up with the title and is gone before it reaches the bar.
        self._place(self.detail, PAGE_MARGIN, self.detail_top - (full_title_y - title_y), 1.0)
        self.detail.set_opacity(clamp(1 - t / 0.45))

        self.height = mix(self.full, BAR_HEIGHT, t)
        self.scroll_target.set_size_request(width, round(self.height))
        self.backdrop.queue_draw()

    def _place(self, widget: Gtk.Widget, x: float, y: float, scale: float) -> None:
        transform = Gsk.Transform().translate(Graphene.Point().init(x, y)).scale(scale, scale)
        self.visual.set_child_transform(widget, transform)

    def _draw_backdrop(self, _area: Gtk.DrawingArea, context: Any, width: int, _height: int) -> None:
        height = self.height
        context.rectangle(0, 0, width, height)
        context.set_source_rgb(0, 0, 0)
        context.fill()
        feather = cairo.LinearGradient(0, height, 0, height + HEADER_FEATHER)
        feather.add_color_stop_rgba(0, 0, 0, 0, 1)
        feather.add_color_stop_rgba(1, 0, 0, 0, 0)
        context.rectangle(0, height, width, HEADER_FEATHER)
        context.set_source(feather)
        context.fill()
        # A hairline appears once the header has become a bar.
        line = clamp((self.progress - 0.85) / 0.15)
        if line > 0:
            context.rectangle(PAGE_MARGIN, height - 1, width - 2 * PAGE_MARGIN, 1)
            context.set_source_rgba(1, 1, 1, 0.08 * line)
            context.fill()


# Styles ------------------------------------------------------------------------

CSS = """
.settings-panel { color: #f5f5f7; }
.settings-panel label { font-family: "Inter", "SF Pro Text", sans-serif; }
/* Line heights are set on the 4-point grid, so every text box is too. */
.hero-title { font-size: %(title)dpx; line-height: 32px; font-weight: 700; letter-spacing: -0.5px; color: #f5f5f7; }
.hero-detail { font-size: 15px; line-height: 20px; color: #98989d; }
.page-title { font-size: 28px; line-height: 32px; font-weight: 700; letter-spacing: -0.5px; color: #f5f5f7; }
.page-body { font-size: 15px; line-height: 20px; color: #98989d; }
.section { font-size: 13px; line-height: 16px; font-weight: 600; color: #8e8e93; }
.preference-row {
    background-color: #1c1c1e;
    min-height: %(row)dpx;
    border-radius: %(row_radius)dpx;
    padding: 0 %(row_padding)dpx;
}
.settings-panel button.preference-row:hover { background-color: #2c2c2e; }
.settings-panel button.preference-row:active { background-color: #3a3a3c; }
.chevron { color: #6e6e73; }
.settings-panel button.global-switch {
    min-width: 0; min-height: 0; padding: 0; border-radius: %(switch_radius)dpx; background: none;
}
.face-row {
    background-color: #1c1c1e;
    min-height: %(row)dpx;
    border-radius: %(row_radius)dpx;
    padding: 0 %(row_padding)dpx;
    /* A rare celebration: the new face's glow lingers, longer than UI motion. */
    transition: background-color 900ms ease;
}
.face-row.fresh { background-color: rgba(48, 209, 88, 0.22); transition: background-color 160ms %(ease_out)s; }
.row-title { font-size: 15px; line-height: 20px; font-weight: 600; color: #f5f5f7; }
.row-detail { font-size: 12px; line-height: 16px; font-weight: 400; color: #8e8e93; }
.empty { font-size: 13px; line-height: 16px; color: #6e6e73; padding: 24px 12px; }
.status { font-size: 13px; line-height: 16px; color: #ff453a; transition: opacity 200ms %(ease_out)s; }
.status.hidden { opacity: 0; transition: opacity 140ms %(ease_out)s; }
.status.good { color: #30d158; }
.bottom-bar {
    padding-top: %(bottom_feather)dpx;
    background-image: linear-gradient(to bottom, rgba(0, 0, 0, 0), #000000 %(bottom_feather)dpx);
}
/* The scroll indicator lives in the right gutter, beside the rows, never over them. */
.settings-scroller scrollbar,
.settings-scroller scrollbar range,
.settings-scroller scrollbar trough {
    background: none; border: none; box-shadow: none; padding: 0; margin: 0; min-width: 0;
}
.settings-scroller scrollbar.vertical { margin: 0 %(gutter)dpx 0 0; }
.settings-scroller scrollbar slider {
    min-width: 4px; min-height: 40px; margin: 0; border: none; border-radius: 2px;
    background-color: rgba(255, 255, 255, 0.24);
    transition: min-width 160ms ease, background-color 160ms ease;
}
.settings-scroller scrollbar.hovering slider { min-width: 6px; border-radius: 3px; background-color: rgba(255, 255, 255, 0.42); }
.step { transition: opacity 250ms ease; }
.step.dim { opacity: 0.38; }
.step-number {
    min-width: 24px; min-height: 20px; border-radius: 10px;
    background-color: #1c1c1e; color: #f5f5f7; font-size: 12px; line-height: 20px; font-weight: 600;
}
.step-text { font-size: 14px; line-height: 20px; color: #f5f5f7; }
.promise-icon { color: #64d2ff; }
.promise-title { font-size: 15px; line-height: 20px; font-weight: 600; color: #f5f5f7; }
.promise-text { font-size: 13px; line-height: 20px; color: #98989d; }
.settings-panel button {
    border: none;
    outline: none;
    box-shadow: none;
    background-image: none;
    text-shadow: none;
    font-weight: 600;
    transition: background-color 180ms ease, color 180ms ease, opacity 180ms ease, transform 160ms %(ease_out)s;
}
/* Press feedback: every button gives slightly under the pointer. */
.settings-panel button:active { transform: scale(0.97); }
.settings-panel button.close, .settings-panel button.back { min-width: %(close)dpx; min-height: %(close)dpx; padding: 0; border-radius: %(close_radius)dpx; background-color: #1c1c1e; color: #98989d; }
.settings-panel button.close:hover, .settings-panel button.back:hover { background-color: #2c2c2e; color: #f5f5f7; }
.settings-panel button.close:active, .settings-panel button.back:active { background-color: #3a3a3c; color: #f5f5f7; }
.settings-panel button.primary { min-height: %(button)dpx; padding: 0 %(space_l)dpx; border-radius: %(button_radius)dpx; font-size: 15px; background-color: #f5f5f7; color: #000000; }
.settings-panel button.primary:disabled { color: #8e8e93; }
.settings-panel button.primary.secondary { color: #f5f5f7; }
.settings-panel button.primary label { color: #000000; }
.settings-panel button.primary:hover { background-color: #ffffff; }
.settings-panel button.primary:active { background-color: #d1d1d6; }
.settings-panel button.primary:disabled { background-color: #1c1c1e; }
.settings-panel button.primary:disabled label { color: #8e8e93; }
.settings-panel button.primary.secondary { background-color: #1c1c1e; }
.settings-panel button.primary.secondary label { color: #f5f5f7; }
.settings-panel button.primary.secondary:hover { background-color: #2c2c2e; }
.settings-panel button.primary.secondary:active { background-color: #3a3a3c; }
.settings-panel button.icon { min-width: %(avatar)dpx; min-height: %(avatar)dpx; padding: 0; border-radius: %(avatar_radius)dpx; background-color: transparent; color: #8e8e93; }
.settings-panel button.icon:hover { background-color: #2c2c2e; color: #ff453a; }
.settings-panel button.destructive { min-height: %(avatar)dpx; padding: 0 16px; border-radius: %(avatar_radius)dpx; background-color: #ff453a; color: #ffffff; font-size: 14px; }
.settings-panel button.destructive label { color: #ffffff; }
.settings-panel button.destructive:hover { background-color: #ff5e55; }
""" % {
    "title": TITLE_PX, "close": CLOSE_SIZE, "close_radius": CLOSE_SIZE // 2,
    "button": BUTTON_HEIGHT, "button_radius": BUTTON_HEIGHT // 2, "bottom_feather": BOTTOM_FEATHER,
    "ease_out": EASE_OUT_CSS,
    "space_l": SPACE_L,
    "row": ROW_HEIGHT, "row_radius": ROW_RADIUS, "row_padding": ROW_PADDING,
    "switch_radius": SWITCH_HEIGHT // 2,
    "avatar": AVATAR_SIZE, "avatar_radius": AVATAR_SIZE // 2,
    # Centred in the gap between the rows' right edge and the island's.
    "gutter": (PAGE_MARGIN - 4) // 2,
}

_css_installed = False


def install_settings_css() -> None:
    """Install the panel's scoped styles once in the island process."""
    global _css_installed
    if _css_installed:
        return
    provider = Gtk.CssProvider()
    provider.load_from_string(CSS)
    Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    settings = Gtk.Settings.get_default()
    settings.set_property("gtk-application-prefer-dark-theme", True)
    settings.set_property("gtk-overlay-scrolling", True)
    _css_installed = True

ONBOARDING = ("welcome", "how", "privacy")

# Onboarding frame. Only the title, body and extra content of a step move; the
# art slot, page dots and buttons persist and change in place.
# The art slot starts below the top bar (the close button's row).
FLOW_TOP = BAR_HEIGHT
ART_HEIGHT = STORY_HEIGHT
ART_TO_TITLE = SPACE_XL
TITLE_TO_BODY = SPACE_S
COPY_TO_EXTRA = SPACE_XL
LIST_GAP = SPACE_L
ICON_SIZE = 24
ICON_TO_TEXT = SPACE_M
DOTS_TO_BUTTON = SPACE_XL
ART_MARK = 144
# Where a short step's group sits in its spare space: a little above centre,
# which reads as centred once the heavier buttons below are counted.
OPTICAL_CENTRE = 0.4
# Incoming content travels this far in the direction of travel; outgoing less.
ENTER_SHIFT = 24
LEAVE_SHIFT = 16
# Under 300 ms: UI motion that runs longer feels slow, not smooth.
MORPH_SECONDS = 0.3
COPY_OUT_SECONDS = 0.14
COPY_IN_SECONDS = 0.26
COPY_IN_DELAY = 0.13
STAGGER_SECONDS = 0.04
ART_OUT_SECONDS = 0.15
ART_IN_SECONDS = 0.28
ART_SCALE_FROM = 0.95
# Mid-transition copy and art blur slightly, so a cross-fade reads as one
# thing changing rather than two things overlapping.
TRANSITION_BLUR = 4.0


class MorphLabel(Gtk.DrawingArea):
    """Text that morphs rather than swaps.

    Letters the old and new text share glide from their old places to their
    new ones; the others fade out upward and in from below. At rest the whole
    string is drawn as one layout, so kerning is exact.
    """

    def __init__(self) -> None:
        super().__init__()
        self.add_css_class("morph")
        self.set_hexpand(True)
        self.set_content_height(20)
        self.text = ""
        self.before = ""
        self.progress = 1.0
        self.last: int | None = None
        self.ticking = False
        self.set_draw_func(self._draw)

    def set_text(self, text: str, animate: bool = True) -> None:
        if text == self.text:
            return
        if animate and self.get_mapped() and self.text:
            # Morph from whatever is on screen, even mid-morph.
            self.before = self.text if self.progress >= 0.5 else self.before
            self.progress = 0.0
            if not self.ticking:
                self.ticking = True
                self.last = None
                self.add_tick_callback(self._tick)
        else:
            self.progress = 1.0
        self.text = text
        self.update_property([Gtk.AccessibleProperty.LABEL], [text])
        self.queue_draw()

    def _tick(self, _widget: Gtk.Widget, clock: Gdk.FrameClock) -> bool:
        now = clock.get_frame_time()
        elapsed = 0.0 if self.last is None else min((now - self.last) / 1_000_000, 1 / 30)
        self.last = now
        self.progress = min(1.0, self.progress + elapsed / MORPH_SECONDS)
        self.queue_draw()
        if self.progress >= 1.0:
            self.ticking = False
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _glyphs(self, text: str, width: float) -> tuple[Any, list[tuple[Any, float]]]:
        layout = self.create_pango_layout(text)
        text_width, _height = layout.get_pixel_size()
        left = (width - text_width) / 2
        glyphs = []
        offset = 0
        for char in text:
            x = layout.index_to_pos(offset).x / Pango.SCALE
            glyphs.append((self.create_pango_layout(char), left + x))
            offset += len(char.encode("utf-8"))
        return layout, glyphs

    def _draw(self, _area: Gtk.DrawingArea, context: Any, width: int, height: int) -> None:
        color = self.get_color()
        layout = self.create_pango_layout(self.text)
        text_width, text_height = layout.get_pixel_size()
        top = (height - text_height) / 2
        if self.progress >= 1.0 or not self.before:
            context.set_source_rgba(color.red, color.green, color.blue, color.alpha)
            context.move_to((width - text_width) / 2, top)
            PangoCairo.show_layout(context, layout)
            return
        # Shared letters move on screen (ease-in-out); the rest fade (ease-out).
        eased = 0.0 if reduced_motion() else EASE_IN_OUT(self.progress)
        _old_layout, old = self._glyphs(self.before, width)
        _new_layout, new = self._glyphs(self.text, width)
        matched_old, matched_new = set(), set()
        pairs = []
        for block in difflib.SequenceMatcher(None, self.before, self.text, autojunk=False).get_matching_blocks():
            # Only runs of shared letters glide, and only a short way: lone
            # letters flying across the button read as noise, not continuity.
            if block.size < 2 or abs(old[block.a][1] - new[block.b][1]) > width * 0.3 or reduced_motion():
                continue
            for index in range(block.size):
                matched_old.add(block.a + index)
                matched_new.add(block.b + index)
                pairs.append((block.a + index, block.b + index))

        def show(glyph: Any, x: float, y: float, alpha: float) -> None:
            if alpha <= 0.01:
                return
            context.set_source_rgba(color.red, color.green, color.blue, color.alpha * alpha)
            context.move_to(x, y)
            PangoCairo.show_layout(context, glyph)

        for old_index, new_index in pairs:
            show(new[new_index][0], mix(old[old_index][1], new[new_index][1], eased), top, 1.0)
        travel = 0.0 if reduced_motion() else 4.0
        leaving = 1 - EASE_OUT(clamp(self.progress / 0.4))
        for index, (glyph, x) in enumerate(old):
            if index not in matched_old:
                show(glyph, x, top - travel * (1 - leaving), leaving)
        arriving = EASE_OUT(clamp((self.progress - 0.3) / 0.7))
        for index, (glyph, x) in enumerate(new):
            if index not in matched_new:
                show(glyph, x, top + travel * (1 - arriving), arriving)


class MorphButton(Gtk.Button):
    def __init__(self, css_class: str, on_click: Callable[[], None]) -> None:
        super().__init__()
        self.add_css_class(css_class)
        self.label = MorphLabel()
        self.set_child(self.label)
        self.connect("clicked", lambda *_: on_click())

    def set_text(self, text: str, animate: bool = True) -> None:
        self.label.set_text(text, animate)


class Piece:
    """One independently animated element of the onboarding frame."""

    def __init__(self, widget: Gtk.Widget, width: float) -> None:
        self.widget = widget
        self.width = width
        self.height = 0.0
        self.alpha = Tween(0.0)
        self.top = Spring(0.0)
        self.top.configure(0.5, 0.9)
        self.shift = Spring(0.0)
        self.lift = Spring(0.0)
        self.scale = Spring(1.0)
        self.start = 0.0
        widget.set_opacity(0.0)

    def measure(self) -> float:
        self.height = float(self.widget.measure(Gtk.Orientation.VERTICAL, round(self.width))[1])
        return self.height


class FlowCanvas(Gtk.Widget):
    """Lays out pieces at sub-pixel positions with transforms, like the island."""

    def __init__(self) -> None:
        super().__init__()
        self.pieces: list[Piece] = []

    def add(self, piece: Piece) -> Piece:
        piece.widget.set_parent(self)
        self.pieces.append(piece)
        return piece

    def do_measure(self, orientation: Gtk.Orientation, _for_size: int) -> tuple[int, int, int, int]:
        return 0, PANEL_WIDTH if orientation == Gtk.Orientation.HORIZONTAL else PANEL_HEIGHT, -1, -1

    def do_size_allocate(self, width: int, _height: int, _baseline: int) -> None:
        for piece in self.pieces:
            piece_width = round(piece.width)
            piece_height = max(1, round(piece.measure()))
            x = (width - piece_width) / 2 + piece.shift.value
            y = piece.top.value + piece.lift.value
            scale = piece.scale.value
            transform = Gsk.Transform().translate(Graphene.Point().init(x + piece_width / 2, y + piece_height / 2))
            transform = transform.scale(scale, scale).translate(Graphene.Point().init(-piece_width / 2, -piece_height / 2))
            piece.widget.allocate(piece_width, piece_height, -1, transform)

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        for piece in self.pieces:
            opacity = piece.widget.get_opacity()
            if opacity <= 0.002:
                continue
            blur = TRANSITION_BLUR * (1 - opacity)
            if blur > 0.05:
                snapshot.push_blur(blur)
                self.snapshot_child(piece.widget, snapshot)
                snapshot.pop()
            else:
                self.snapshot_child(piece.widget, snapshot)


@dataclass
class Step:
    art: str
    pieces: list[Piece]
    dots: int = -1


class ActionRow(Gtk.Widget):
    """The onboarding's bottom row: a primary button, joined by a secondary.

    Alone, the primary spans the row. When a step offers a second choice, the
    primary narrows to the right half while the secondary fades in on the left
    (8 apart), like the island's Deny / Approve. The primary is the same button
    throughout, so it never jumps; its width eases on a spring.
    """

    def __init__(self, on_primary: Callable[[], None], on_secondary: Callable[[], None]) -> None:
        super().__init__()
        self.primary = MorphButton("primary", on_primary)
        self.secondary = MorphButton("primary", on_secondary)
        self.secondary.add_css_class("secondary")
        self.secondary.set_parent(self)
        self.primary.set_parent(self)
        self.split = Spring(0.0)
        self.split.configure(0.4, 1.0)
        self.secondary.set_opacity(0.0)
        self.secondary.set_can_target(False)

    def set_paired(self, paired: bool, animate: bool) -> None:
        self.split.target = 1.0 if paired else 0.0
        if not animate:
            self.split.value, self.split.velocity = self.split.target, 0.0
        self.secondary.set_can_target(paired)
        self.advance(0.0)

    def advance(self, elapsed: float) -> None:
        self.split.step(elapsed)
        # The secondary leaves quicker than it arrives.
        amount = clamp(self.split.value)
        self.secondary.set_opacity(EASE_OUT(amount) if self.split.target > 0 else amount * amount)
        self.queue_allocate()

    def do_measure(self, orientation: Gtk.Orientation, _for_size: int) -> tuple[int, int, int, int]:
        size = CONTENT_WIDTH if orientation == Gtk.Orientation.HORIZONTAL else BUTTON_HEIGHT
        return size, size, -1, -1

    def do_size_allocate(self, width: int, height: int, _baseline: int) -> None:
        amount = clamp(self.split.value)
        half = (width - BUTTON_GAP) / 2
        primary_width = mix(width, half, amount)
        self.primary.allocate(round(primary_width), height, -1, Gsk.Transform().translate(Graphene.Point().init(width - round(primary_width), 0)))
        scale = mix(0.97, 1.0, amount) if not reduced_motion() else 1.0
        transform = Gsk.Transform().translate(Graphene.Point().init(half / 2, height / 2)).scale(scale, scale).translate(Graphene.Point().init(-half / 2, -height / 2))
        self.secondary.allocate(round(half), height, -1, transform)

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        if self.secondary.get_opacity() > 0.002:
            self.snapshot_child(self.secondary, snapshot)
        self.snapshot_child(self.primary, snapshot)


class PageDots(Gtk.DrawingArea):
    """Page dots whose current page is a capsule; the capsule's growth is paint, not layout."""

    DOT = 8.0
    ACTIVE = 24.0
    GAP = 8.0

    def __init__(self, count: int) -> None:
        super().__init__()
        self.count = count
        self.set_halign(Gtk.Align.CENTER)
        self.set_content_width(round(self.ACTIVE + (count - 1) * self.DOT + (count - 1) * self.GAP))
        self.set_content_height(round(self.DOT))
        self.widths = [Spring(self.DOT) for _ in range(count)]
        for spring in self.widths:
            spring.configure(0.35, 1.0)
        self.set_draw_func(self._draw)

    def select(self, index: int) -> None:
        for position, spring in enumerate(self.widths):
            spring.target = self.ACTIVE if position == index else self.DOT
            if reduced_motion() or not self.get_mapped():
                spring.value, spring.velocity = spring.target, 0.0

    def advance(self, elapsed: float) -> None:
        for spring in self.widths:
            spring.step(elapsed)
        self.queue_draw()

    def _draw(self, _area: Gtk.DrawingArea, context: Any, width: int, height: int) -> None:
        widths = [max(self.DOT, spring.value) for spring in self.widths]
        x = (width - sum(widths) - self.GAP * (self.count - 1)) / 2
        for spring, dot_width in zip(self.widths, widths):
            # Grey grows toward white as a dot becomes the current page.
            amount = clamp((dot_width - self.DOT) / (self.ACTIVE - self.DOT))
            shade = mix(0.23, 0.96, amount)
            rounded_rect(context, x, 0, dot_width, height, height / 2)
            context.set_source_rgb(shade, shade, mix(0.24, 0.97, amount))
            context.fill()
            x += dot_width + self.GAP


class FlowView(Gtk.Overlay):
    """The onboarding frame: persistent art, dots and buttons; moving copy."""

    def __init__(self) -> None:
        super().__init__()
        self.canvas = FlowCanvas()
        self.set_child(self.canvas)
        self.arts: dict[str, Piece] = {}
        self.steps: dict[str, Step] = {}
        self.current: str | None = None
        self.now = 0.0
        self.last: int | None = None
        self.scheduled: list[tuple[float, Callable[[float], None]]] = []

        # The dots and the action row are pinned independently: a step without
        # dots leaves the buttons exactly where they were.
        self.on_primary: Callable[[], None] = lambda: None
        self.on_secondary: Callable[[], None] = lambda: None
        self.on_back: Callable[[], None] = lambda: None
        back_glyph = Gtk.DrawingArea()
        back_glyph.set_content_width(CLOSE_SIZE)
        back_glyph.set_content_height(CLOSE_SIZE)
        back_glyph.set_draw_func(draw_back_glyph)
        self.back = Gtk.Button(child=back_glyph)
        self.back.add_css_class("back")
        self.back.set_focus_on_click(False)
        self.back.update_property([Gtk.AccessibleProperty.LABEL], ["Back"])
        self.back.set_halign(Gtk.Align.START)
        self.back.set_valign(Gtk.Align.START)
        self.back.set_margin_top(CLOSE_INSET)
        self.back.set_margin_start(CLOSE_INSET)
        self.back.connect("clicked", lambda *_: self.on_back())
        self.back.set_can_target(False)
        self.back_alpha = Tween(0.0)
        self.row = ActionRow(lambda: self.on_primary(), lambda: self.on_secondary())
        self.row.set_valign(Gtk.Align.END)
        self.row.set_halign(Gtk.Align.CENTER)
        self.row.set_margin_bottom(PAGE_MARGIN)
        self.primary = self.row.primary
        self.dots = PageDots(len(ONBOARDING))
        self.dots.set_valign(Gtk.Align.END)
        self.dots.set_margin_bottom(PAGE_MARGIN + BUTTON_HEIGHT + DOTS_TO_BUTTON)
        self.dots.set_can_target(False)
        self.dots_alpha = Tween(0.0)
        self.add_overlay(self.back)
        self.add_overlay(self.dots)
        self.add_overlay(self.row)
        self.add_tick_callback(self._tick)

    # Building ---------------------------------------------------------------

    def add_art(self, name: str, widget: Gtk.Widget) -> None:
        piece = self.canvas.add(Piece(widget, widget.measure(Gtk.Orientation.HORIZONTAL, -1)[1]))
        piece.top.value = piece.top.target = FLOW_TOP
        piece.top.configure(0.45, 1.0)
        self.arts[name] = piece

    def add_step(self, name: str, art: str, widgets: list[Gtk.Widget], dots: int = -1) -> list[Piece]:
        pieces = [self.canvas.add(Piece(widget, CONTENT_WIDTH)) for widget in widgets]
        self.steps[name] = Step(art, pieces, dots)
        return pieces

    def _layout(self, name: str, snap: bool, snap_art: bool = True) -> None:
        """Place a step's art and copy as one group, balanced in its space.

        Gaps follow the rhythm: 24 art to title, 8 title to body, 24 copy to
        content. The group sits a little above centre between the top bar and
        whatever is above the buttons (the dots, or the buttons themselves),
        so short steps don't leave a hole above the buttons. Every position
        lands on the 4-point grid.
        """
        step = self.steps[name]
        pieces = step.pieces
        gaps = [TITLE_TO_BODY if following.widget.has_css_class("page-body") else COPY_TO_EXTRA for following in pieces[1:]]
        copy_height = sum(piece.measure() for piece in pieces) + sum(gaps)
        above_buttons = DOTS_TO_BUTTON + PageDots.DOT if step.dots >= 0 else 0
        bottom = PANEL_HEIGHT - PAGE_MARGIN - BUTTON_HEIGHT - above_buttons - SPACE_XL
        # Spacing is measured from the art's visible edge, never from a slot
        # around it: a 144 mark and a 64 shield both sit 24 above the title.
        art = self.arts[step.art]
        art_height = art.measure()
        spare = max(0.0, bottom - FLOW_TOP - (art_height + ART_TO_TITLE + copy_height))
        top = FLOW_TOP + GRID * round(spare * OPTICAL_CENTRE / GRID)
        art.top.target = top
        if snap_art:
            art.top.value, art.top.velocity = art.top.target, 0.0
        y = top + art_height + ART_TO_TITLE
        for index, piece in enumerate(pieces):
            piece.top.target = y
            if snap:
                piece.top.value, piece.top.velocity = y, 0.0
            y += piece.height + (gaps[index] if index < len(gaps) else 0)

    # Motion -------------------------------------------------------------------

    def go(self, name: str, direction: int = 1, animate: bool = True) -> None:
        if name == self.current:
            return
        now, previous = self.now, self.current
        animate = animate and previous is not None and self.get_mapped()
        travel = 0.0 if reduced_motion() else 1.0
        self.scheduled.clear()
        if previous:
            for piece in self.steps[previous].pieces:
                piece.alpha.to(0.0, now, COPY_OUT_SECONDS if animate else 0.0)
                piece.shift.configure(0.4, 1.0)
                piece.shift.target = -LEAVE_SHIFT * direction * travel if animate else 0.0
        # Art shared with the previous step glides to its new place; new art
        # simply appears where it belongs.
        same_art = previous is not None and self.steps[previous].art == self.steps[name].art
        self._layout(name, snap=True, snap_art=not (same_art and animate) or reduced_motion())
        for index, piece in enumerate(self.steps[name].pieces):
            # Enter once the outgoing copy has mostly left, so lines never overlap.
            delay = COPY_IN_DELAY + index * STAGGER_SECONDS if animate else 0.0
            piece.shift.configure(0.45, 0.9)
            piece.shift.value, piece.shift.velocity, piece.shift.target = (ENTER_SHIFT * direction * travel if animate else 0.0), 0.0, 0.0
            piece.lift.value = piece.lift.target = 0.0
            piece.start = now + delay
            piece.alpha.to(1.0, now, COPY_IN_SECONDS if animate else 0.0, delay)

        art = self.steps[name].art
        if previous is None or self.steps[previous].art != art:
            if previous:
                leaving = self.arts[self.steps[previous].art]
                leaving.alpha.to(0.0, now, ART_OUT_SECONDS if animate else 0.0)
                leaving.scale.configure(0.35, 1.0)
                leaving.scale.target = mix(1.0, ART_SCALE_FROM, travel) if animate else 1.0
            arriving = self.arts[art]
            # A barely-there bounce: alive, not playful.
            arriving.scale.configure(0.45, 0.86)
            arriving.scale.value, arriving.scale.velocity, arriving.scale.target = (mix(1.0, ART_SCALE_FROM, travel) if animate else 1.0), 0.0, 1.0
            arriving.start = now + (0.08 if animate else 0.0)
            arriving.alpha.to(1.0, now, ART_IN_SECONDS if animate else 0.0, 0.08 if animate else 0.0)

        dots = self.steps[name].dots
        self.dots.select(dots)
        shown = 1.0 if dots >= 0 else 0.0
        if self.dots_alpha.target != shown:
            self.dots_alpha.to(shown, now, 0.24 if animate else 0.0)
        self.current = name

    def retext(self, piece: Piece, label_widget: Gtk.Label, text: str) -> None:
        """Change a visible line of copy: out, swap, settle up into place."""
        if label_widget.get_text() == text:
            return
        step = next((name for name, entry in self.steps.items() if piece in entry.pieces), None)
        if step != self.current or not self.get_mapped():
            label_widget.set_text(text)
            if step:
                self._layout(step, snap=True, snap_art=step != self.current)
            return
        piece.alpha.to(0.0, self.now, 0.1)

        def swap(now: float) -> None:
            label_widget.set_text(text)
            self._layout(step, snap=reduced_motion(), snap_art=reduced_motion())
            piece.lift.configure(0.4, 1.0)
            piece.lift.value, piece.lift.velocity, piece.lift.target = (0.0 if reduced_motion() else 6.0), 0.0, 0.0
            piece.alpha.to(1.0, now, 0.22)

        self.scheduled.append((self.now + 0.1, swap))

    def actions(self, primary: str, on_primary: Callable[[], None] | None, secondary: str = "", on_secondary: Callable[[], None] | None = None, muted: bool = False) -> None:
        """Set the step's buttons. `muted` styles a lone primary as secondary (Cancel)."""
        animate = self.get_mapped()
        self.primary.set_text(primary, animate)
        self.primary.set_sensitive(on_primary is not None)
        (self.primary.add_css_class if muted else self.primary.remove_css_class)("secondary")
        self.on_primary = on_primary or (lambda: None)
        if secondary:
            self.row.secondary.set_text(secondary, animate and self.row.split.target > 0)
        self.on_secondary = on_secondary or (lambda: None)
        self.row.set_paired(bool(secondary), animate and not reduced_motion())

    def set_back(self, on_back: Callable[[], None] | None) -> None:
        """Show Back in the persistent top-left slot after the first step."""
        shown = on_back is not None
        self.on_back = on_back or (lambda: None)
        self.back.set_can_target(shown)
        self.back_alpha.to(1.0 if shown else 0.0, self.now, 0.18 if self.get_mapped() else 0.0)

    def _tick(self, _widget: Gtk.Widget, clock: Gdk.FrameClock) -> bool:
        frame = clock.get_frame_time()
        elapsed = 0.0 if self.last is None else min((frame - self.last) / 1_000_000, 1 / 30)
        self.last = frame
        self.now += elapsed
        now = self.now
        due = [entry for entry in self.scheduled if entry[0] <= now]
        self.scheduled = [entry for entry in self.scheduled if entry[0] > now]
        for _time, action in due:
            action(now)
        for piece in self.canvas.pieces:
            piece.top.step(elapsed)
            piece.lift.step(elapsed)
            if now >= piece.start:
                piece.shift.step(elapsed)
                piece.scale.step(elapsed)
            piece.widget.set_opacity(piece.alpha.value(now))
        self.dots.set_opacity(self.dots_alpha.value(now))
        self.back.set_opacity(self.back_alpha.value(now))
        self.dots.advance(elapsed)
        self.row.advance(elapsed)
        self.canvas.queue_allocate()
        return GLib.SOURCE_CONTINUE


class PageStack(Gtk.Widget):
    """Onboarding, the main screen and its destinations, as one continuous space.

    Pages move like onboarding copy: forward, the old page leaves 16 pt to the
    left while the new one enters 24 pt from the right a beat later (Back
    mirrors it). Onboarding hands over to the main screen with a crossfade.
    Pages keep their full size throughout; only transform and opacity move.
    """

    def __init__(self) -> None:
        super().__init__()
        self.pages: dict[str, Piece] = {}
        self.current: str | None = None
        self.now = 0.0
        self.last: int | None = None
        # Frames are only laid out while a page is moving.
        self.moving_until: float | None = None
        self.add_tick_callback(self._tick)

    def add_named(self, widget: Gtk.Widget, name: str) -> None:
        widget.set_parent(self)
        widget.set_visible(False)
        self.pages[name] = Piece(widget, PANEL_WIDTH)

    def get_visible_child_name(self) -> str | None:
        return self.current

    def show(self, name: str, direction: int = 1, animate: bool = True) -> None:
        if name == self.current:
            return
        now, previous = self.now, self.current
        animate = animate and previous is not None and self.get_mapped()
        travel = 0.0 if reduced_motion() or not animate else float(direction)
        # Travel sequences the pages; a crossfade overlaps them.
        fade_out, fade_in, delay = (COPY_OUT_SECONDS, COPY_IN_SECONDS, COPY_IN_DELAY) if direction else (0.25, 0.25, 0.0)
        if not animate:
            fade_out = fade_in = delay = 0.0
        if previous:
            leaving = self.pages[previous]
            leaving.widget.set_can_target(False)
            leaving.alpha.to(0.0, now, fade_out)
            leaving.shift.configure(0.4, 1.0)
            leaving.shift.target = -LEAVE_SHIFT * travel
        arriving = self.pages[name]
        arriving.widget.set_visible(True)
        arriving.widget.set_can_target(True)
        arriving.shift.configure(0.45, 0.9)
        arriving.shift.value, arriving.shift.velocity, arriving.shift.target = ENTER_SHIFT * travel, 0.0, 0.0
        arriving.start = now + delay
        arriving.alpha.to(1.0, now, fade_in, delay)
        self.current = name
        self.moving_until = now + delay + (0.8 if animate else 0.0)
        self._tick(self, None)

    def _tick(self, _widget: Gtk.Widget, clock: Gdk.FrameClock | None) -> bool:
        if clock is not None:
            frame = clock.get_frame_time()
            elapsed = 0.0 if self.last is None else min((frame - self.last) / 1_000_000, 1 / 30)
            self.last = frame
            self.now += elapsed
        else:
            elapsed = 0.0
        if self.moving_until is None:
            return GLib.SOURCE_CONTINUE
        settled = self.now > self.moving_until
        if settled:
            self.moving_until = None
        for name, page in self.pages.items():
            if settled:
                page.shift.value, page.shift.velocity = page.shift.target, 0.0
            elif self.now >= page.start:
                page.shift.step(elapsed)
            opacity = page.alpha.value(self.now)
            page.widget.set_opacity(opacity)
            # A page that has left is hidden, so it takes no focus or input.
            if name != self.current and page.alpha.done(self.now):
                page.widget.set_visible(False)
        self.queue_allocate()
        return GLib.SOURCE_CONTINUE

    def do_measure(self, orientation: Gtk.Orientation, _for_size: int) -> tuple[int, int, int, int]:
        size = PANEL_WIDTH if orientation == Gtk.Orientation.HORIZONTAL else PANEL_HEIGHT
        return size, size, -1, -1

    def do_size_allocate(self, width: int, height: int, _baseline: int) -> None:
        for page in self.pages.values():
            if page.widget.get_visible():
                page.widget.allocate(width, height, -1, Gsk.Transform().translate(Graphene.Point().init(page.shift.value, 0)))

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        for page in self.pages.values():
            opacity = page.widget.get_opacity()
            if not page.widget.get_visible() or opacity <= 0.002:
                continue
            blur = TRANSITION_BLUR * (1 - opacity)
            if blur > 0.05:
                snapshot.push_blur(blur)
                self.snapshot_child(page.widget, snapshot)
                snapshot.pop()
            else:
                self.snapshot_child(page.widget, snapshot)


class SettingsPanel(Gtk.Overlay):
    """The complete configuration journey, embedded in the layer-shell island."""

    def __init__(self, hud: Any, on_close: Callable[[], None], welcome: bool = False) -> None:
        super().__init__()
        install_settings_css()
        self.add_css_class("settings-panel")
        self.set_size_request(PANEL_WIDTH, PANEL_HEIGHT)
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.hud = hud
        self.on_close = on_close
        self.closed = False

        self.settings = preferences.load()
        self.faces: list[dict[str, Any]] = []
        self.faces_loaded = False
        self.rows: dict[int, FaceRow] = {}
        self.fresh_id: int | None = None
        self.result_timer: int | None = None
        self.island = IslandBridge(hud, self._cancel_enroll)
        # One change at a time: an enrollment or a removal.
        self.run: HelperRun | None = None
        # None, "approving" (polkit is asking), "scanning", or "failed".
        self.enroll_phase: str | None = None
        # Where the enrollment was started: "setup" (onboarding) or "manage".
        self.enroll_from = "manage"
        self.enroll_error = ""

        self.stack = PageStack()
        self._build_onboarding()
        self._build_home()
        self._build_manage()
        self._build_view_options()

        # Setting destinations mirror the close button with Back at top-left.
        self.back = Gtk.Button(child=self._back_glyph())
        self.back.add_css_class("back")
        self.back.set_focus_on_click(False)
        self.back.set_can_target(False)
        self.back.update_property([Gtk.AccessibleProperty.LABEL], ["Back"])
        self.back.connect("clicked", lambda *_: self._show_page("home", direction=-1))
        self.back_revealer = Gtk.Revealer()
        self.back_revealer.set_transition_type(Gtk.RevealerTransitionType.CROSSFADE)
        self.back_revealer.set_transition_duration(180)
        self.back_revealer.set_halign(Gtk.Align.START)
        self.back_revealer.set_valign(Gtk.Align.START)
        self.back_revealer.set_margin_top(CLOSE_INSET)
        self.back_revealer.set_margin_start(CLOSE_INSET)
        self.back_revealer.set_child(self.back)

        close = Gtk.Button(child=self._close_glyph())
        close.add_css_class("close")
        close.set_focus_on_click(False)
        close.update_property([Gtk.AccessibleProperty.LABEL], ["Close"])
        close.set_halign(Gtk.Align.END)
        close.set_valign(Gtk.Align.START)
        close.set_margin_top(CLOSE_INSET)
        close.set_margin_end(CLOSE_INSET)
        close.connect("clicked", lambda *_: self.request_close())

        self.set_child(self.stack)
        self.add_overlay(self.back_revealer)
        self.add_overlay(close)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        onboarded = self.settings.get("onboarded") and not welcome
        self.flow.go("welcome", animate=False)
        self._show_page("home" if onboarded else "flow", animate=False)
        self._render()
        self._load_faces()

    @staticmethod
    def _close_glyph() -> Gtk.DrawingArea:
        glyph = Gtk.DrawingArea()
        glyph.set_content_width(CLOSE_SIZE)
        glyph.set_content_height(CLOSE_SIZE)
        glyph.set_draw_func(draw_close_glyph)
        return glyph

    @staticmethod
    def _back_glyph() -> Gtk.DrawingArea:
        glyph = Gtk.DrawingArea()
        glyph.set_content_width(CLOSE_SIZE)
        glyph.set_content_height(CLOSE_SIZE)
        glyph.set_draw_func(draw_back_glyph)
        return glyph

    # Building --------------------------------------------------------------

    def _build_onboarding(self) -> None:
        flow = FlowView()
        self.flow = flow
        self.mark = Mark(ART_MARK)
        flow.add_art("mark", self.mark)
        self.story = IslandStory(self._highlight_step)
        flow.add_art("story", self.story)
        shield = Gtk.Image.new_from_icon_name("security-high-symbolic")
        shield.set_pixel_size(64)
        shield.add_css_class("promise-icon")
        flow.add_art("shield", shield)

        flow.add_step("welcome", "mark", [
            label("Meet FaceGate", "page-title", wrap=True),
            label("Approve sudo and admin prompts with a glance. Your password always works too.", "page-body", wrap=True),
        ], dots=0)

        self.steps: list[Gtk.Box] = []
        steps = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=LIST_GAP)
        for number, text in (
            ("1", "When something asks for admin access, the island drops down and says who's asking."),
            ("2", "Approve, and your camera looks for you. It stays off until you do."),
            ("3", "Recognized, it continues. If not, just use your password."),
        ):
            step = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=ICON_TO_TEXT)
            step.add_css_class("step")
            badge = label(number, "step-number")
            badge.set_valign(Gtk.Align.START)
            step.append(badge)
            copy = label(text, "step-text", wrap=True, xalign=0)
            copy.set_hexpand(True)
            step.append(copy)
            steps.append(step)
            self.steps.append(step)
        flow.add_step("how", "story", [label("How it works", "page-title", wrap=True), steps], dots=1)

        promises = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=LIST_GAP)
        for icon, title, text in (
            ("camera-web-symbolic", "Camera only when you approve", "Nothing looks at you until you say so."),
            ("computer-symbolic", "Stays on this computer", "Your face data is never uploaded."),
            ("dialog-password-symbolic", "Changes need admin", "Adding or removing a face asks first."),
        ):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=ICON_TO_TEXT)
            image = Gtk.Image.new_from_icon_name(icon)
            # Icons are 20 on a 24 line box, level with the 20-high title line.
            image.set_pixel_size(20)
            image.set_size_request(ICON_SIZE, 20)
            image.add_css_class("promise-icon")
            image.set_valign(Gtk.Align.START)
            row.append(image)
            copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            copy.set_hexpand(True)
            copy.append(label(title, "promise-title", xalign=0))
            copy.append(label(text, "promise-text", wrap=True, xalign=0))
            row.append(copy)
            promises.append(row)
        flow.add_step("privacy", "shield", [label("Private by design", "page-title", wrap=True), promises], dots=2)

        self.setup_title = label("Authenticate to add a face", "page-title", wrap=True)
        self.setup_body = label("Adding a face needs admin approval first.", "page-body", wrap=True)
        self.setup_pieces = flow.add_step("setup", "mark", [self.setup_title, self.setup_body])
        flow.add_step("added", "mark", [
            label("Face added", "page-title", wrap=True),
            label("FaceGate will recognize you from now on. Add another look, like with glasses, to help it in more situations.", "page-body", wrap=True),
        ])
        self.stack.add_named(flow, "flow")

    @staticmethod
    def _section(title: str, rows: list[Gtk.Widget], margin_top: int = 0) -> Gtk.Box:
        section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        section.set_margin_top(margin_top)
        heading = label(title, "section", xalign=0)
        heading.set_margin_start(ROW_TEXT_INSET)
        heading.set_margin_bottom(SECTION_TO_ROWS)
        section.append(heading)
        for index, row in enumerate(rows):
            if index:
                row.set_margin_top(ROW_GAP)
            section.append(row)
        return section

    def _scroll_page(self, name: str, title: str, detail: str, art: Gtk.Widget | None = None) -> tuple[Gtk.Overlay, CollapsingHeader, Gtk.Box]:
        """A settings page: a hero that collapses over a scrolling body.

        Returns the page, its header and the body, which already starts
        clear of the header's fade and keeps the page margin.
        """
        overlay = Gtk.Overlay()
        scroller = Gtk.ScrolledWindow()
        scroller.add_css_class("settings-scroller")
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_overlay_scrolling(True)
        header = CollapsingHeader(scroller, title, art)
        header.detail.set_text(detail)
        header.detail_fade.target = detail

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        spacer = Gtk.Box()
        spacer.set_size_request(-1, header.full)
        content.append(spacer)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.set_margin_top(HEADER_FEATHER)
        body.set_margin_start(PAGE_MARGIN)
        body.set_margin_end(PAGE_MARGIN)
        content.append(body)
        scroller.set_child(content)

        overlay.set_child(scroller)
        overlay.add_overlay(header.visual)
        overlay.add_overlay(header.scroll_target)
        self.stack.add_named(overlay, name)
        return overlay, header, body

    def _build_home(self) -> None:
        """The front door: the global switch, then the two setting areas."""
        _page, self.home_header, body = self._scroll_page("home", "FaceGate", "Version %s" % __version__)
        self.enabled_row = ToggleRow(self._toggle_enabled)
        self.face_entry = NavigationRow("Face Approval", Mark(PREFERENCE_ICON_SIZE, animated=False), lambda: self._show_page("manage"))
        self.view_entry = NavigationRow("View Options", DisplayGlyph(PREFERENCE_ICON_SIZE), lambda: self._show_page("view"))
        body.append(self._section("General", [self.enabled_row]))
        body.append(self._section("Settings", [self.face_entry, self.view_entry], PREFERENCE_GROUP_GAP))
        body.set_margin_bottom(PAGE_MARGIN)

    def _build_manage(self) -> None:
        overlay, self.header, body = self._scroll_page("manage", "Face Approval", "")
        section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=SECTION_TO_ROWS)
        heading = label("Faces", "section", xalign=0)
        # Headings share the text inset of rows, clear of their rounded ends.
        heading.set_margin_start(ROW_TEXT_INSET)
        section.append(heading)
        self.rows_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        section.append(self.rows_box)
        self.empty = Gtk.Revealer()
        self.empty.set_transition_type(Gtk.RevealerTransitionType.CROSSFADE)
        self.empty.set_transition_duration(220)
        self.empty.set_child(label("No faces yet", "empty"))
        section.append(self.empty)
        body.append(section)
        body.set_margin_bottom(BOTTOM_BAR)

        bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        bar.add_css_class("bottom-bar")
        bar.set_valign(Gtk.Align.END)
        # The status line sits in the clearance above the button and fades in
        # place, so showing it never pushes anything.
        self.status = label("", "status", wrap=True)
        self.status.add_css_class("hidden")
        self.status.set_valign(Gtk.Align.END)
        self.status.set_margin_start(PAGE_MARGIN)
        self.status.set_margin_end(PAGE_MARGIN)
        self.status.set_margin_bottom(PAGE_MARGIN + BUTTON_HEIGHT + SPACE_M)
        self.status.set_can_target(False)
        self.primary = MorphButton("primary", self._manage_primary)
        self.primary.set_margin_top(BUTTON_CLEARANCE)
        self.primary.set_margin_start(PAGE_MARGIN)
        self.primary.set_margin_end(PAGE_MARGIN)
        self.primary.set_margin_bottom(PAGE_MARGIN)
        bar.append(self.primary)
        overlay.add_overlay(bar)
        overlay.add_overlay(self.status)

    def _build_view_options(self) -> None:
        _page, self.view_header, self.view_body = self._scroll_page("view", "View Options", "Choose where the island appears.", DisplayGlyph(HERO_SIZE))
        self.view_body.set_margin_bottom(PAGE_MARGIN)
        self.choices: dict[tuple[str, str], ChoiceRow] = {}
        self._refresh_monitor_rows()
        # Plugging a monitor in or out updates the choices while they're open.
        self.monitors = Gdk.Display.get_default().get_monitors()
        self.monitors_changed = self.monitors.connect("items-changed", self._refresh_monitor_rows)

    # Navigation ------------------------------------------------------------

    def _show_page(self, page: str, direction: int = 1, animate: bool = True) -> None:
        """Move within the one configuration surface; the close button stays put."""
        if page == "view":
            self._refresh_monitor_rows()
        elif page == "home":
            self._update_home()
        self.stack.show(page, direction, animate)
        shown = page in ("manage", "view")
        self.back.set_can_target(shown)
        self.back_revealer.set_reveal_child(shown)

    def _go(self, page: str, direction: int = 1) -> None:
        if page == "how":
            self.story.restart()
        self.flow.go(page, direction=direction)
        self._render()

    def _back_onboarding(self) -> None:
        step = self.flow.current
        if step == "setup":
            phase = self.enroll_phase
            self.enroll_phase = None
            self.enroll_error = ""
            self.island.close()
            if self.run:
                # Once scanning, let the root helper release the camera
                # cleanly. Before that it is only pkexec waiting on polkit.
                (self.run.stop if phase == "scanning" else self.run.abort)()
            self._go("privacy", direction=-1)
            return
        target = BACK_TARGETS.get(str(step))
        if target:
            self._go(target, direction=-1)

    def _highlight_step(self, step: int) -> None:
        for index, row in enumerate(self.steps, start=1):
            (row.remove_css_class if step in (0, index) else row.add_css_class)("dim")

    def _finish_onboarding(self) -> None:
        self.settings = preferences.update(onboarded=True)
        self.enroll_phase = None
        self._show_page("home", direction=0)
        self._render()

    def _on_key(self, _controller: Any, keyval: int, _keycode: int, _state: Any) -> bool:
        if keyval == Gdk.KEY_Escape:
            # Back out one level at a time, unless a face is being added:
            # then Escape still stops it by closing, as it always has.
            if self.stack.current in ("manage", "view") and not self.run:
                self._show_page("home", direction=-1)
            else:
                self.request_close()
            return True
        return False

    # Preferences -----------------------------------------------------------

    def _toggle_enabled(self, enabled: bool) -> None:
        self.hud.set_enabled(enabled)
        self.settings = preferences.load()
        self._update_home()

    def _update_home(self) -> None:
        enabled = preferences.enabled(self.settings)
        if self.enabled_row.switch.active != enabled:
            self.enabled_row.switch.set_active(enabled)
        self.enabled_row.detail.set_text("Approves sudo and admin prompts" if enabled else "sudo and admin prompts work as usual")
        self.home_header.mark.set_state("approval" if enabled else "off")

        if not self.faces_loaded:
            face_detail = "Loading faces…"
        elif self.faces:
            face_detail = "1 face" if len(self.faces) == 1 else "%d faces" % len(self.faces)
        else:
            face_detail = "No faces yet"
        self.face_entry.detail.set_text(face_detail)
        self.view_entry.detail.set_text(placement_summary(self.settings, available_monitors()))

    def _refresh_monitor_rows(self, *_args: Any) -> None:
        """Rebuild the choices when the connected monitors change."""
        child = self.view_body.get_first_child()
        while child:
            following = child.get_next_sibling()
            self.view_body.remove(child)
            child = following

        monitors = available_monitors()
        mode, connector = preferences.placement(self.settings)
        self.choices = {}

        def choice(title: str, detail: str, choice_mode: str, choice_connector: str) -> ChoiceRow:
            row = ChoiceRow(title, detail, (mode, connector) == (choice_mode, choice_connector), lambda: self._select_placement(choice_mode, choice_connector))
            self.choices[(choice_mode, choice_connector)] = row
            return row

        follow = choice("Follow Pointer", "Shows on the monitor you're using", preferences.FOLLOW_POINTER, "")
        self.view_body.append(self._section("Automatic", [follow]))
        rows: list[Gtk.Widget] = [choice(monitor.title, monitor.detail, preferences.FIXED_MONITOR, monitor.connector) for monitor in monitors]
        if mode == preferences.FIXED_MONITOR and not any(monitor.connector == connector for monitor in monitors):
            # Keep an unplugged choice visible, and say what happens meanwhile.
            rows.append(choice(connector, "Not connected · following the pointer", preferences.FIXED_MONITOR, connector))
        self.view_body.append(self._section("Lock to a monitor", rows or [label("No monitors found", "empty", xalign=0)], PREFERENCE_GROUP_GAP))

    def _select_placement(self, mode: str, connector: str) -> None:
        """Choosing applies at once: the open island moves to its new monitor."""
        self.settings = preferences.update(placement=mode, monitor=connector)
        for key, row in self.choices.items():
            row.indicator.set_selected(key == (mode, connector))
        self._update_home()
        self.hud.relocate_config()

    # State -----------------------------------------------------------------

    def _render(self) -> None:
        self._update_home()
        phase, has_faces = self.enroll_phase, bool(self.faces)
        from_manage = self.enroll_from == "manage"
        setup_title, setup_body, setup_detail = setup_auth_copy(self.faces_loaded, has_faces)
        # Manage page.
        if phase == "approving" and from_manage:
            detail, button, mark = setup_detail, None, "approval"
        elif phase == "scanning" and from_manage:
            detail, button, mark = "Keep your face in view.", "Cancel", "scanning"
        elif has_faces:
            detail, button, mark = "Your face approves sudo and admin prompts.", "Add a Face", "approval"
        else:
            detail, button, mark = "Approve sudo and admin prompts with a glance.", "Set Up Face", "approval"
        if not self.result_timer:
            self.header.mark.set_state(mark)
        self.header.detail_fade.to(detail)
        busy = self.run is not None
        self.primary.set_text(button or "Waiting for approval…")
        self.primary.set_sensitive(bool(button) and (not busy or phase == "scanning"))
        (self.primary.add_css_class if phase == "scanning" and from_manage else self.primary.remove_css_class)("secondary")
        self.empty.set_reveal_child(not has_faces)

        # Onboarding: the persistent mark and buttons follow the current step.
        flow, step = self.flow, self.flow.current
        flow.set_back(self._back_onboarding if step != "welcome" else None)
        title, body = self.setup_pieces
        if step == "welcome":
            flow.actions("Continue", lambda: self._go("how"))
            self.mark.set_state("approval")
        elif step == "how":
            flow.actions("Continue", lambda: self._go("privacy"))
        elif step == "privacy":
            flow.actions("Set Up Face", lambda: self._enroll("setup"), "Not Now", self._finish_onboarding)
        elif step == "added":
            flow.actions("Done", self._finish_onboarding, "Add Another Look", lambda: self._enroll("setup"))
            self.mark.set_state("success")
        elif step == "setup":
            if phase == "scanning":
                flow.retext(title, self.setup_title, "Look at the camera")
                flow.retext(body, self.setup_body, "Keep your face in view while FaceGate learns it.")
                flow.actions("Cancel", self._cancel_enroll, muted=True)
                self.mark.set_state("scanning")
            elif phase == "failed":
                flow.retext(title, self.setup_title, "Face not added")
                flow.retext(body, self.setup_body, self.enroll_error or "Let's try that again.")
                flow.actions("Try Again", lambda: self._enroll("setup"), "Not Now", self._finish_onboarding)
                self.mark.set_state("failure")
            else:
                flow.retext(title, self.setup_title, setup_title)
                flow.retext(body, self.setup_body, setup_body)
                flow.actions("Waiting for approval…", None)
                self.mark.set_state("approval")

    def _say(self, text: str, good: bool = False) -> None:
        if text:
            self.status.set_text(text)
            (self.status.add_css_class if good else self.status.remove_css_class)("good")
        (self.status.remove_css_class if text else self.status.add_css_class)("hidden")

    def _flash_mark(self, state: str) -> None:
        if self.result_timer:
            GLib.source_remove(self.result_timer)
        self.header.mark.set_state(state)

        def settle() -> bool:
            self.result_timer = None
            self._render()
            return False

        self.result_timer = GLib.timeout_add(int(RESULT_SECONDS * 1000), settle)

    # Faces -----------------------------------------------------------------

    def _load_faces(self) -> None:
        def on_event(event: dict[str, Any]) -> None:
            if event.get("event") == "faces":
                self._show_faces(event.get("faces") or [])

        def on_exit(status: int) -> None:
            if status != 0:
                self._say("Couldn't read your faces. Is FaceGate installed?")

        HelperRun(["list"], on_event, on_exit).start()

    def _show_faces(self, faces: list[dict[str, Any]]) -> None:
        self.faces_loaded = True
        self.faces = [face for face in faces if isinstance(face, dict) and "id" in face]
        current = {int(face["id"]) for face in self.faces}
        for face_id, row in list(self.rows.items()):
            if face_id not in current:
                del self.rows[face_id]
                self._drop_row(row)
        for face in self.faces:
            face_id = int(face["id"])
            if face_id in self.rows:
                continue
            row = FaceRow(face, self._remove)
            self.rows[face_id] = row
            self.rows_box.append(row)
            # Reveal on the next frame so the row slides in rather than appearing.
            GLib.idle_add(lambda row=row: (row.set_reveal_child(True), False)[1])
            if face_id == self.fresh_id:
                row.celebrate()
        self._render()

    def _drop_row(self, row: FaceRow) -> None:
        row.set_reveal_child(False)

        def detach() -> bool:
            if row.get_parent() is self.rows_box:
                self.rows_box.remove(row)
            return False

        GLib.timeout_add(ROW_REVEAL_MS + 40, detach)

    def _remove(self, row: FaceRow) -> None:
        if self.run:
            return
        self._say("")
        row.set_busy(True)
        removed = False

        def on_event(event: dict[str, Any]) -> None:
            nonlocal removed
            if event.get("event") == "faces":
                removed = True
                self._show_faces(event.get("faces") or [])

        def on_exit(_status: int) -> None:
            self.run = None
            if not removed:
                row.set_busy(False)
                self._say("Not approved, so the face was kept.")
            self._render()

        self.run = HelperRun(["remove", str(row.face_id)], on_event, on_exit)
        self.run.start()
        self._render()

    # Enrollment --------------------------------------------------------------

    def _manage_primary(self) -> None:
        if self.enroll_phase == "scanning":
            self._cancel_enroll()
        elif not self.run:
            self._enroll("manage")

    def _enroll(self, origin: str) -> None:
        if self.run:
            return
        self.enroll_from = origin
        self.enroll_phase = "approving"
        self.enroll_error = ""
        self._say("")
        if origin == "setup":
            self.flow.go("setup")
        self.island.expect()
        self.run = HelperRun(["enroll"], self._on_enroll_event, self._on_enroll_exit)
        self.run.start()
        self._render()

    def _cancel_enroll(self) -> None:
        if self.run and self.enroll_phase == "scanning":
            self.run.send({"cmd": "cancel"})
        elif self.run and self.enroll_phase == "approving":
            # Approved, but the helper is still opening the camera: closing
            # its input stops the scan before the first frame.
            self.run.stop()

    def _on_enroll_event(self, event: dict[str, Any]) -> None:
        kind = event.get("event")
        if kind in ("frame", "hint", "progress"):
            if self.enroll_phase == "approving":
                # The scan has begun; the island is already showing its preview.
                self.enroll_phase = "scanning"
                self.island.open()
                self._render()
            if kind == "frame":
                self.island.send({"frame": event.get("data"), "w": event.get("w"), "h": event.get("h")})
            elif kind == "hint":
                self.island.send({"hint": event.get("text")})
            else:
                self.island.send({"progress": event.get("value")})
            return
        if kind == "added":
            self.island.send({"result": "success"})
            self.island.close()
            self.enroll_phase = None
            self.fresh_id = event.get("id")
            self._show_faces(event.get("faces") or [])
            if self.enroll_from == "setup":
                self.flow.go("added")
            else:
                self._flash_mark("success")
                self._say("%s added." % (event.get("label") or "Face"), good=True)
        elif kind == "cancelled":
            self.island.close()
            self.enroll_phase = "failed" if self.enroll_from == "setup" else None
            self.enroll_error = "You cancelled the scan."
        elif kind == "error":
            message = str(event.get("message") or "Something went wrong.")
            self.island.send({"result": "failure", "detail": message})
            self.island.close()
            self.enroll_phase, self.enroll_error = "failed", message
            if self.enroll_from == "manage":
                self.enroll_phase = None
                self._flash_mark("failure")
                self._say(message)
        self._render()

    def _on_enroll_exit(self, _status: int) -> None:
        self.run = None
        self.island.close()
        if self.enroll_phase in ("approving", "scanning"):
            # pkexec ended before a result: approval was declined or failed.
            message = "Not approved, so no face was added." if self.enroll_phase == "approving" else "The scan stopped unexpectedly."
            self.enroll_phase, self.enroll_error = "failed", message
            if self.enroll_from == "manage":
                self.enroll_phase = None
                self._say(message)
        self._render()

    def request_close(self) -> None:
        if not self.closed:
            self.on_close()

    def shutdown(self) -> None:
        """Release camera/helper work before the panel is detached."""
        if self.closed:
            return
        self.closed = True
        self.monitors.disconnect(self.monitors_changed)
        self.island.close()
        if self.run:
            (self.run.stop if self.enroll_phase == "scanning" else self.run.abort)()


def main() -> int:
    # Kept for source-tree callers; the installed launcher imports control
    # directly so opening FaceGate never constructs another GTK window.
    from .control import main as control_main

    return control_main()


if __name__ == "__main__":
    raise SystemExit(main())
