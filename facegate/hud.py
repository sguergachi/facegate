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
    QRectF,
    Qt,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .protocol import MAX_MESSAGE_BYTES, decode, encode


SOCKET_NAME = "facegate.sock"


def default_socket_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime:
        runtime = "/run/user/%d" % os.getuid()
    return Path(runtime) / SOCKET_NAME


class FaceGlyph(QWidget):
    """A lightweight animated scanner glyph: idle, scanning, success, error."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(152, 152)
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
        rect = QRectF(15, 15, 122, 122)
        colors = {
            "approval": QColor("#8bb8ff"),
            "scanning": QColor("#5eead4"),
            "success": QColor("#72e0a1"),
            "failure": QColor("#ff8a9a"),
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
        corner = 23.0
        # The four open scanner corners.
        for x, y, dx, dy in ((15, 15, 1, 1), (137, 15, -1, 1), (15, 137, 1, -1), (137, 137, -1, -1)):
            painter.drawLine(QPointF(x, y), QPointF(x + dx * corner, y))
            painter.drawLine(QPointF(x, y), QPointF(x, y + dy * corner))

        if self._state == "success":
            painter.drawLine(QPointF(47, 79), QPointF(68, 100))
            painter.drawLine(QPointF(68, 100), QPointF(108, 55))
            return
        if self._state == "failure":
            painter.drawLine(QPointF(55, 55), QPointF(97, 97))
            painter.drawLine(QPointF(97, 55), QPointF(55, 97))
            return

        # A friendly, abstract face instead of a camera preview.
        face = QPainterPath()
        face.addRoundedRect(QRectF(51, 44, 50, 63), 22, 22)
        painter.setPen(QPen(QColor(232, 245, 255, 210), 2.2))
        painter.drawPath(face)
        painter.drawPoint(QPointF(67, 70))
        painter.drawPoint(QPointF(85, 70))
        painter.drawArc(QRectF(66, 75, 20, 15), 10 * 16, 160 * 16)
        if self._state == "scanning":
            scan_y = 32 + self._phase * 89
            scan = QLinearGradient(28, scan_y, 124, scan_y)
            scan.setColorAt(0, QColor(accent.red(), accent.green(), accent.blue(), 0))
            scan.setColorAt(0.5, accent)
            scan.setColorAt(1, QColor(accent.red(), accent.green(), accent.blue(), 0))
            painter.setPen(QPen(scan, 2.4))
            painter.drawLine(QPointF(28, scan_y), QPointF(124, scan_y))


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
        self.setFixedSize(390, 454)

        shell = QFrame(self)
        shell.setObjectName("shell")
        shell.setStyleSheet(
            "QFrame#shell { background: rgba(13, 20, 34, 246); border: 1px solid rgba(166, 205, 255, 100);"
            " border-radius: 28px; } QLabel { color: #f4f8ff; }"
            "QPushButton { border-radius: 14px; padding: 12px 22px; font-weight: 650; font-size: 15px; }"
            "QPushButton#approve { background: #dff2ff; color: #102033; }"
            "QPushButton#approve:hover { background: #ffffff; }"
            "QPushButton#cancel { background: transparent; color: #b6c5d9; }"
        )
        layout = QVBoxLayout(shell)
        layout.setContentsMargins(34, 28, 34, 28)
        layout.setSpacing(12)
        self.glyph = FaceGlyph()
        layout.addWidget(self.glyph, 0, Qt.AlignmentFlag.AlignHCenter)
        self.title = QLabel("Approve face verification")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title.setFont(QFont("Sans Serif", 19, QFont.Weight.DemiBold))
        layout.addWidget(self.title)
        self.detail = QLabel("FaceGate will use your camera only after you approve.")
        self.detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail.setWordWrap(True)
        self.detail.setStyleSheet("color: #b9c7d9; font-size: 13px;")
        layout.addWidget(self.detail)
        layout.addStretch(1)
        self.buttons = QWidget()
        row = QHBoxLayout(self.buttons)
        row.setContentsMargins(0, 0, 0, 0)
        self.cancel = QPushButton("Use password")
        self.cancel.setObjectName("cancel")
        self.approve = QPushButton("Approve")
        self.approve.setObjectName("approve")
        row.addWidget(self.cancel)
        row.addWidget(self.approve)
        layout.addWidget(self.buttons)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(shell)
        self.approve.clicked.connect(self.approved)
        self.cancel.clicked.connect(self.rejected)

    def show_approval(self) -> None:
        self.glyph.set_state("approval")
        self.title.setText("Approve face verification")
        self.detail.setText("FaceGate will use your camera only after you approve.")
        self.buttons.show()
        self.show()
        self.raise_()
        self.activateWindow()

    def show_scanning(self) -> None:
        self.glyph.set_state("scanning")
        self.title.setText("Looking for you")
        self.detail.setText("Keep your face in view. You can still use your password if this does not work.")
        self.buttons.hide()
        self.show()

    def show_result(self, success: bool) -> None:
        self.glyph.set_state("success" if success else "failure")
        self.title.setText("Verified" if success else "Not recognized")
        self.detail.setText("Continuing securely." if success else "Face verification ended. Your password prompt is ready.")
        self.buttons.hide()
        QTimer.singleShot(900 if success else 1400, self.close)


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
            self.approval_timeout.start(max(1_000, min(int(message.get("timeout_ms", 12_000), 30_000))))
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
