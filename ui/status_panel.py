#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Ampel-Anzeige für Webcam, DVS, Mikrofon und Speicher – mit „Neu verbinden“."""

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
)

from recorder.camera_session import DEVICE_LABELS

STATE_COLORS = {
    "ok": "#198754",
    "warn": "#fd7e14",
    "connecting": "#fd7e14",
    "error": "#dc3545",
    "unavailable": "#adb5bd",
}
STATE_TEXT = {
    "ok": "bereit",
    "warn": "Hinweis",
    "connecting": "verbinde…",
    "error": "Problem",
    "unavailable": "aus",
}


class DeviceStatusPanel(QFrame):
    """Zeigt pro Gerät einen farbigen Punkt, Text und ggf. einen Reconnect-Knopf."""

    def __init__(self, session, compact=False, parent=None):
        super().__init__(parent)
        self.session = session
        self.compact = compact
        self.rows = {}
        self.setFrameStyle(QFrame.StyledPanel)
        self._build()
        for dev in DEVICE_LABELS:
            self._update(dev, *session.status(dev))
        session.status_changed.connect(self._update)

    def _build(self):
        layout = QHBoxLayout() if self.compact else QGridLayout()
        layout.setContentsMargins(8, 4, 8, 4)
        for i, (dev, label) in enumerate(DEVICE_LABELS.items()):
            dot = QLabel("●")
            dot.setStyleSheet("font-size: 18px;")
            name = QLabel(f"<b>{label}</b>")
            detail = QLabel("")
            detail.setWordWrap(not self.compact)
            detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
            button = None
            if dev in ("webcam", "dvs"):
                button = QPushButton("Neu verbinden")
                button.setToolTip("Verbindung zur Kamera trennen und neu aufbauen")
                button.clicked.connect(lambda _=False, d=dev: self._reconnect(d))
            elif dev in ("storage", "mic"):
                button = QPushButton("Prüfen")
                button.clicked.connect(lambda _=False, d=dev: self._recheck(d))

            if self.compact:
                for w in (dot, name, detail):
                    layout.addWidget(w)
                if button is not None and dev in ("webcam", "dvs"):
                    layout.addWidget(button)
                layout.addSpacing(16)
            else:
                layout.addWidget(dot, i, 0)
                layout.addWidget(name, i, 1)
                layout.addWidget(detail, i, 2)
                if button is not None:
                    layout.addWidget(button, i, 3)
            self.rows[dev] = (dot, detail, button)

        if self.compact:
            layout.addStretch()
        else:
            layout.setColumnStretch(2, 1)
        self.setLayout(layout)

    def _update(self, device, state, detail):
        if device not in self.rows:
            return
        dot, detail_label, button = self.rows[device]
        color = STATE_COLORS.get(state, "#adb5bd")
        dot.setStyleSheet(f"font-size: 18px; color: {color};")
        dot.setToolTip(STATE_TEXT.get(state, state))
        text = detail or STATE_TEXT.get(state, state)
        detail_label.setText(text)
        detail_label.setToolTip(text)
        if button is not None and device in ("webcam", "dvs"):
            button.setEnabled(not self.session.recording and state != "unavailable")

    def _reconnect(self, device):
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            if device == "webcam":
                ok, msg = self.session.reconnect_webcam()
            else:
                ok, msg = self.session.reconnect_dvs()
        finally:
            QApplication.restoreOverrideCursor()
        if not ok or msg:
            QMessageBox.information(self, DEVICE_LABELS[device], msg or "Nicht möglich.")

    def _recheck(self, device):
        if device == "storage":
            self.session.check_storage()
        else:
            self.session.check_microphone()
