#!/usr/bin/env python3
"""FaceGate's user-session HUD service.

The service owns a 0600 UNIX socket in the logged-in user's runtime directory.
PAM asks it for consent; a successful consent only starts Howdy.  It is never an
authentication decision, so an untrusted client cannot obtain sudo by replying
to this socket.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PyQt6.QtCore import (
    QEasingCurve,
    QObject,
    QPointF,
    QRect,
    QRectF,
    Qt,
    QTimer,
    QPropertyAnimation,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .protocol import MAX_MESSAGE_BYTES, decode, encode


SOCKET_NAME = "facegate.sock"


def default_socket_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime:
        runtime = "/run/user/%d" % os.getuid()
    return Path(runtime) / SOCKET_NAME


def approval_timeout_ms(value: object) -> int:
    """Keep a PAM-provided consent timeout inside a humane, safe range."""
    return max(1_000, min(int(value), 30_000))


class FaceGlyph(QWidget):
    """A lightweight animated scanner glyph: idle, scanning, success, error."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(128, 128)
        self._state = "approval"
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(16)

    def set_state(self, state: str) -> None:
        self._state = state
        self.update()

    def _tick(self) -> None:
        self._phase = (self._phase + 0.012) % 1.0
        self.update()

    def paintEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(13, 13, 102, 102)
        colors = {
            "approval": QColor("#f5f5f7"),
            "scanning": QColor("#b7f7ff"),
            "success": QColor("#e9fff0"),
            "failure": QColor("#ffb4bd"),
        }
        accent = colors.get(self._state, colors["approval"])

        glow = QLinearGradient(0, 0, 0, self.height())
        glow.setColorAt(0, QColor(accent.red(), accent.green(), accent.blue(), 45))
        glow.setColorAt(1, QColor(accent.red(), accent.green(), accent.blue(), 0))
        painter.setBrush(glow)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(rect.adjusted(-7, -7, 7, 7))

        pen = QPen(accent, 3.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        corner = 21.0
        # The four open scanner corners.
        for x, y, dx, dy in ((13, 13, 1, 1), (115, 13, -1, 1), (13, 115, 1, -1), (115, 115, -1, -1)):
            painter.drawLine(QPointF(x, y), QPointF(x + dx * corner, y))
            painter.drawLine(QPointF(x, y), QPointF(x, y + dy * corner))

        if self._state == "success":
            painter.drawLine(QPointF(39, 66), QPointF(58, 85))
            painter.drawLine(QPointF(58, 85), QPointF(94, 44))
            return
        if self._state == "failure":
            painter.drawLine(QPointF(45, 45), QPointF(83, 83))
            painter.drawLine(QPointF(83, 45), QPointF(45, 83))
            return

        # A friendly, abstract face instead of a camera preview.
        face = QPainterPath()
        face.addRoundedRect(QRectF(43, 36, 42, 54), 19, 19)
        painter.setPen(QPen(QColor(232, 245, 255, 210), 2.2))
        painter.drawPath(face)
        painter.drawPoint(QPointF(56, 58))
        painter.drawPoint(QPointF(72, 58))
        painter.drawArc(QRectF(55, 63, 18, 13), 10 * 16, 160 * 16)
        if self._state == "scanning":
            scan_y = 27 + self._phase * 74
            scan = QLinearGradient(25, scan_y, 103, scan_y)
            scan.setColorAt(0, QColor(accent.red(), accent.green(), accent.blue(), 0))
            scan.setColorAt(0.5, accent)
            scan.setColorAt(1, QColor(accent.red(), accent.green(), accent.blue(), 0))
            painter.setPen(QPen(scan, 2.4))
            painter.drawLine(QPointF(25, scan_y), QPointF(103, scan_y))


@dataclass
class PendingApproval:
    connection: socket.socket
    request_id: str


class FaceGateDialog(QDialog):
    approved = pyqtSignal()
    rejected = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("FaceGate")
        self.setModal(False)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(416)
        self.setMinimumHeight(52)
        self.setMaximumHeight(390)
        self._expanded_height = 376
        self._collapsed_height = 52
        self._geometry_animation = QPropertyAnimation(self, b"geometry", self)
        self._geometry_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._content_animation: QPropertyAnimation | None = None
        self._exit_animation: QPropertyAnimation | None = None

        shell = QFrame(self)
        shell.setObjectName("shell")
        shell.setStyleSheet(
            "QFrame#shell { background: #000000; border: 1px solid rgba(255, 255, 255, 38); border-radius: 26px; }"
            "QLabel { color: #f5f5f7; }"
            "QPushButton { border-radius: 15px; padding: 12px 20px; font-weight: 600; font-size: 14px; }"
            "QPushButton#approve { background: #f5f5f7; color: #000000; }"
            "QPushButton#approve:hover { background: #ffffff; }"
            "QPushButton#approve:pressed { background: #d5d5d7; }"
            "QPushButton#cancel { background: #1c1c1e; color: #f5f5f7; }"
            "QPushButton#cancel:hover { background: #2c2c2e; }"
        )
        layout = QVBoxLayout(shell)
        layout.setContentsMargins(32, 18, 32, 24)
        layout.setSpacing(10)
        self.content = QWidget()
        self.content_effect = QGraphicsOpacityEffect(self.content)
        self.content_effect.setOpacity(1.0)
        self.content.setGraphicsEffect(self.content_effect)
        content_layout = QVBoxLayout(self.content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(10)
        self.eyebrow = QLabel("FACE VERIFICATION")
        self.eyebrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.eyebrow.setStyleSheet("color: #8e8e93; font-size: 10px; font-weight: 700; letter-spacing: 1.6px;")
        content_layout.addWidget(self.eyebrow)
        self.glyph = FaceGlyph()
        content_layout.addWidget(self.glyph, 0, Qt.AlignmentFlag.AlignHCenter)
        self.title = QLabel("Approve face verification")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title.setFont(QFont("Sans Serif", 20, QFont.Weight.DemiBold))
        content_layout.addWidget(self.title)
        self.detail = QLabel("FaceGate will use your camera only after you approve.")
        self.detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail.setWordWrap(True)
        self.detail.setStyleSheet("color: #a1a1a6; font-size: 13px;")
        content_layout.addWidget(self.detail)
        content_layout.addSpacing(4)
        self.buttons = QWidget()
        row = QHBoxLayout(self.buttons)
        row.setContentsMargins(0, 0, 0, 0)
        self.cancel = QPushButton("Use password")
        self.cancel.setObjectName("cancel")
        self.approve = QPushButton("Approve")
        self.approve.setObjectName("approve")
        row.addWidget(self.cancel)
        row.addWidget(self.approve)
        content_layout.addWidget(self.buttons)
        layout.addWidget(self.content)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(shell)
        self.approve.clicked.connect(self.approved)
        self.cancel.clicked.connect(self.rejected)

    def _geometry(self, height: int, y_offset: int = 0) -> QRect:
        screen = self.screen() or QApplication.primaryScreen()
        area = screen.availableGeometry()
        return QRect(area.x() + (area.width() - self.width()) // 2, area.y() + 8 + y_offset, self.width(), height)

    def _set_copy(self, state: str, title: str, detail: str, buttons: bool, animate: bool = True) -> None:
        def apply() -> None:
            self.glyph.set_state(state)
            self.title.setText(title)
            self.detail.setText(detail)
            self.buttons.setVisible(buttons)

        if not animate or not self.isVisible():
            apply()
            return
        if self._content_animation:
            self._content_animation.stop()
        fade_out = QPropertyAnimation(self.content_effect, b"opacity", self)
        fade_out.setDuration(115)
        fade_out.setStartValue(self.content_effect.opacity())
        fade_out.setEndValue(0.18)
        fade_out.setEasingCurve(QEasingCurve.Type.InOutCubic)

        def fade_in() -> None:
            apply()
            fade = QPropertyAnimation(self.content_effect, b"opacity", self)
            fade.setDuration(210)
            fade.setStartValue(0.18)
            fade.setEndValue(1.0)
            fade.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._content_animation = fade
            fade.start()

        fade_out.finished.connect(fade_in)
        self._content_animation = fade_out
        fade_out.start()

    def _arrive(self) -> None:
        self._exit_animation = None
        self.content_effect.setOpacity(0.0)
        self.setGeometry(self._geometry(self._collapsed_height, -32))
        self.show()
        self.raise_()
        self.activateWindow()
        self._geometry_animation.stop()
        self._geometry_animation.setDuration(620)
        self._geometry_animation.setStartValue(self.geometry())
        self._geometry_animation.setEndValue(self._geometry(self._expanded_height))
        self._geometry_animation.start()
        fade = QPropertyAnimation(self.content_effect, b"opacity", self)
        fade.setDuration(340)
        fade.setStartValue(0.0)
        fade.setEndValue(1.0)
        fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._content_animation = fade
        QTimer.singleShot(170, fade.start)

    def _depart(self) -> None:
        self._geometry_animation.stop()
        self._geometry_animation.setDuration(380)
        self._geometry_animation.setStartValue(self.geometry())
        self._geometry_animation.setEndValue(self._geometry(self._collapsed_height, -30))
        self._geometry_animation.setEasingCurve(QEasingCurve.Type.InCubic)
        self._geometry_animation.finished.connect(self.close)
        self._geometry_animation.start()
        fade = QPropertyAnimation(self.content_effect, b"opacity", self)
        fade.setDuration(180)
        fade.setStartValue(self.content_effect.opacity())
        fade.setEndValue(0.0)
        self._exit_animation = fade
        fade.start()

    def show_approval(self) -> None:
        self._set_copy("approval", "Approve face verification", "Your camera stays off until you approve.", True, self.isVisible())
        if not self.isVisible():
            self._arrive()

    def show_scanning(self) -> None:
        self._set_copy("scanning", "Looking for you", "Hold still for a moment.", False)
        if not self.isVisible():
            self._arrive()

    def show_result(self, success: bool) -> None:
        self._set_copy(
            "success" if success else "failure",
            "Verified" if success else "Not recognized",
            "Continuing securely." if success else "Use your password instead.",
            False,
        )
        QTimer.singleShot(880 if success else 1350, self._depart)


class HudServer(QObject):
    request_received = pyqtSignal(object, object)

    def __init__(self, socket_path: Path) -> None:
        super().__init__()
        self.socket_path = socket_path
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(socket_path))
        os.chmod(socket_path, 0o600)
        self.listener.listen(8)
        self.listener.settimeout(0.4)
        self.running = True
        self._thread = threading.Thread(target=self._accept_loop, name="facegate-socket", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self.running = False
        self.listener.close()
        self.socket_path.unlink(missing_ok=True)

    def _accept_loop(self) -> None:
        while self.running:
            try:
                connection, _ = self.listener.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._read_one, args=(connection,), daemon=True).start()

    def _read_one(self, connection: socket.socket) -> None:
        try:
            connection.settimeout(25)
            raw = connection.recv(MAX_MESSAGE_BYTES + 1)
            message = decode(raw.split(b"\n", 1)[0])
            self.request_received.emit(message, connection)
        except (OSError, ValueError):
            connection.close()


class FaceGateApp(QObject):
    def __init__(self, server: HudServer) -> None:
        super().__init__()
        self.server = server
        self.dialog = FaceGateDialog()
        self.pending: PendingApproval | None = None
        self.approval_timeout = QTimer(self)
        self.approval_timeout.setSingleShot(True)
        self.approval_timeout.timeout.connect(self.reject)
        self.server.request_received.connect(self.on_request)
        self.dialog.approved.connect(self.approve)
        self.dialog.rejected.connect(self.reject)

    @staticmethod
    def reply(connection: socket.socket, payload: dict[str, Any]) -> None:
        try:
            connection.sendall(encode(payload))
        finally:
            connection.close()

    def on_request(self, message: dict[str, Any], connection: socket.socket) -> None:
        phase = message.get("phase")
        request_id = str(message.get("id", ""))
        if phase == "approval":
            if self.pending:
                self.reply(connection, {"approved": False, "reason": "busy"})
                return
            self.pending = PendingApproval(connection, request_id)
            self.dialog.show_approval()
            self.approval_timeout.start(approval_timeout_ms(message.get("timeout_ms", 12_000)))
            return
        if phase == "scanning":
            self.reply(connection, {"ok": True})
            self.dialog.show_scanning()
            return
        if phase == "result":
            self.reply(connection, {"ok": True})
            self.dialog.show_result(message.get("result") == "success")
            return
        self.reply(connection, {"ok": False, "reason": "unknown-phase"})

    def approve(self) -> None:
        if not self.pending:
            return
        pending, self.pending = self.pending, None
        self.approval_timeout.stop()
        self.reply(pending.connection, {"id": pending.request_id, "approved": True})
        self.dialog.show_scanning()

    def reject(self) -> None:
        if not self.pending:
            return
        pending, self.pending = self.pending, None
        self.approval_timeout.stop()
        self.reply(pending.connection, {"id": pending.request_id, "approved": False, "reason": "rejected"})
        self.dialog.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="FaceGate HUD service")
    parser.add_argument("--socket", type=Path, default=default_socket_path())
    args = parser.parse_args()
    args.socket.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    args.socket.unlink(missing_ok=True)
    app = QApplication(sys.argv)
    server = HudServer(args.socket)
    app.aboutToQuit.connect(server.close)
    app.controller = FaceGateApp(server)  # Keep the Python signal receiver alive.
    server.start()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
