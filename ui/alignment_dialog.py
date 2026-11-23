#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Kameras ausrichten: DVS-Events werden über das Webcam-Bild gelegt.

Der gelbe Rahmen ist der Teil des Webcam-Bilds, der dem Bildfeld der DVS
entspricht. Man verschiebt ihn mit der Maus und vergrößert/verkleinert ihn mit
dem Mausrad, bis die türkisen Events auf den passenden Kanten im Webcam-Bild
liegen – oder lässt die App das automatisch machen.

Die Ausrichtung gilt pro Objektiv-Profil (bei Objektivwechsel Profil wählen
oder neu anlegen) und stimmt exakt für die Entfernung, in der ausgerichtet
wurde – also am besten mit dem Patientenbett/der Untersuchungsposition.
"""

import time

import cv2
import numpy as np
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QFont, QImage
from PyQt5.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from ui.video_widget import VideoDisplayWidget
from ui.window_utils import fit_to_screen
from utils import alignment as al

AUTO_SECONDS = 4.0
MIN_AUTO_SCORE = 0.3
OVERLAY_COLOR = np.array([255, 220, 0], dtype=np.float32)  # türkis (BGR)


class AlignmentCanvas(VideoDisplayWidget):
    """Vorschau, in der der Ausrichtungsrahmen verschoben/skaliert wird."""

    def __init__(self, dialog):
        super().__init__()
        self.dialog = dialog
        self._last = None
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.OpenHandCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._last = self._to_norm(event.pos())
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if self._last is None:
            return
        cur = self._to_norm(event.pos())
        if cur:
            self.dialog.move_rect(cur[0] - self._last[0], cur[1] - self._last[1])
            self._last = cur

    def mouseReleaseEvent(self, event):
        self._last = None
        self.setCursor(Qt.OpenHandCursor)

    def wheelEvent(self, event):
        steps = event.angleDelta().y() / 120.0
        self.dialog.scale_rect(0.97 ** steps, self._to_norm(event.pos()))

    def keyPressEvent(self, event):
        step = 0.002 if event.modifiers() & Qt.ShiftModifier else 0.01
        keys = {
            Qt.Key_Left: (-step, 0), Qt.Key_Right: (step, 0),
            Qt.Key_Up: (0, -step), Qt.Key_Down: (0, step),
        }
        if event.key() in keys:
            self.dialog.move_rect(*keys[event.key()])
        elif event.key() in (Qt.Key_Plus, Qt.Key_Equal):
            self.dialog.scale_rect(0.98)
        elif event.key() == Qt.Key_Minus:
            self.dialog.scale_rect(1 / 0.98)
        else:
            super().keyPressEvent(event)


class AlignmentDialog(QDialog):
    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
        self.store = session.alignment_store
        self.rect = session.alignment
        self.full_size = (1280, 720)
        self._auto = None  # (MotionAccumulator, Endzeit) während der Auto-Ausrichtung
        self._note = ("", 0.0)  # (Text, sichtbar bis)

        self.setWindowTitle("Kameras ausrichten")
        self._build()
        fit_to_screen(self, 0.85, 0.85)

        session.begin_alignment()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(40)

    # ------------------------------------------------------------------- UI

    def _build(self):
        layout = QVBoxLayout(self)
        title = QLabel("Kameras ausrichten")
        f = QFont()
        f.setBold(True)
        f.setPointSize(14)
        title.setFont(f)
        layout.addWidget(title)

        help_text = QLabel(
            "Die <b>türkisen Punkte</b> sind die Bewegungen, die die DVS sieht. Der <b>gelbe Rahmen</b> "
            "ist der Teil des Webcam-Bilds, der dem DVS-Bildfeld entspricht.<br>"
            "Rahmen mit der Maus <b>verschieben</b>, mit dem <b>Mausrad</b> vergrößern/verkleinern "
            "(Feinjustierung: Pfeiltasten, mit Shift kleinere Schritte), bis die türkisen Punkte auf "
            "den passenden Stellen im Webcam-Bild liegen. Oder auf <b>„Automatisch ausrichten“</b> "
            "klicken und die Hand vor beiden Kameras bewegen.<br>"
            "Am besten in der Entfernung ausrichten, in der später untersucht wird."
        )
        help_text.setWordWrap(True)
        layout.addWidget(help_text)

        profile_row = QHBoxLayout()
        profile_row.addWidget(QLabel("Objektiv-Profil:"))
        self.profile_combo = QComboBox()
        self._fill_profiles(self.store.active_profile)
        self.profile_combo.currentTextChanged.connect(self._on_profile_changed)
        profile_row.addWidget(self.profile_combo, 1)
        new_btn = QPushButton("Neues Profil…")
        new_btn.clicked.connect(self._new_profile)
        profile_row.addWidget(new_btn)
        layout.addLayout(profile_row)

        self.canvas = AlignmentCanvas(self)
        layout.addWidget(self.canvas, 1)

        self.info_label = QLabel("")
        layout.addWidget(self.info_label)

        buttons = QHBoxLayout()
        self.auto_btn = QPushButton("Automatisch ausrichten")
        self.auto_btn.clicked.connect(self._start_auto)
        buttons.addWidget(self.auto_btn)
        reset_btn = QPushButton("Zurücksetzen")
        reset_btn.setToolTip("Mittiger Ausschnitt (1,5-fach), wie früher der Standard-Zoom")
        reset_btn.clicked.connect(lambda: self._set_rect(al.DEFAULT_ALIGNMENT))
        buttons.addWidget(reset_btn)
        buttons.addStretch()
        cancel_btn = QPushButton("Abbrechen")
        cancel_btn.clicked.connect(self.reject)
        buttons.addWidget(cancel_btn)
        save_btn = QPushButton("Speichern")
        save_btn.setDefault(True)
        save_btn.setStyleSheet("font-weight:bold;")
        save_btn.clicked.connect(self._save)
        buttons.addWidget(save_btn)
        layout.addLayout(buttons)

    def _fill_profiles(self, select):
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        for name in self.store.profiles:
            self.profile_combo.addItem(name)
        self.profile_combo.setCurrentText(select)
        self.profile_combo.blockSignals(False)

    def _on_profile_changed(self, name):
        if name:
            self._set_rect(self.store.alignment(name))

    def _new_profile(self):
        name, ok = QInputDialog.getText(
            self, "Neues Objektiv-Profil", "Name (z. B. „12 mm Objektiv“):"
        )
        name = name.strip()
        if ok and name:
            if name not in self.store.profiles:
                self.store.data["profiles"][name] = {"alignment": list(self.rect)}
            self._fill_profiles(name)

    # ------------------------------------------------------------ Rahmen

    def _set_rect(self, rect):
        w, h = self.full_size
        self.rect = al.keep_aspect(al.clamp_rect(rect), w, h)

    def move_rect(self, dx, dy):
        x, y, w, h = self.rect
        self.rect = al.clamp_rect((x + dx, y + dy, w, h))

    def scale_rect(self, factor, center=None):
        x, y, w, h = self.rect
        cx, cy = center if center else (x + w / 2, y + h / 2)
        nw = min(max(w * factor, 0.1), 1.0)
        nh = h * nw / w
        nx = cx - (cx - x) * nw / w
        ny = cy - (cy - y) * nh / h
        self.rect = al.clamp_rect((nx, ny, nw, nh))

    # ------------------------------------------------------------ Vorschau

    def _frames(self):
        w = self.session.webcam.latest_full_frame if self.session.webcam else None
        d = self.session.dvs.latest_full_frame if self.session.dvs else None
        return w, d

    def _tick(self):
        cam, dvs = self._frames()
        if cam is None:
            self.canvas.setText("Warte auf Webcam-Bild…")
            return
        self.full_size = (cam.shape[1], cam.shape[0])
        img = cam.astype(np.float32) * 0.75
        x, y, w, h = al.to_pixels(self.rect, cam.shape[1], cam.shape[0])
        if dvs is not None:
            mask = cv2.resize(al.event_mask(dvs), (w, h), interpolation=cv2.INTER_AREA)
            region = img[y : y + h, x : x + w]
            alpha = np.clip(mask * 1.5, 0, 1)[..., None]
            region[:] = region * (1 - alpha) + OVERLAY_COLOR * alpha
        img = img.clip(0, 255).astype(np.uint8)
        cv2.rectangle(img, (x, y), (x + w - 1, y + h - 1), (0, 193, 255), 2)

        if self._auto is not None:
            acc, end = self._auto
            if dvs is not None:
                acc.add(cam, dvs)
            remaining = end - time.monotonic()
            if remaining > 0:
                self.info_label.setText(
                    f"<b>Bitte jetzt die Hand vor beiden Kameras bewegen …</b> noch {remaining:.0f} s"
                )
            else:
                self._finish_auto()
        elif time.monotonic() < self._note[1]:
            self.info_label.setText(self._note[0])
        else:
            status = "DVS liefert keine Bilder – ist sie verbunden?" if dvs is None else ""
            self.info_label.setText(
                f"Ausschnitt: x={x}, y={y}, {w}×{h} Pixel von {cam.shape[1]}×{cam.shape[0]}  {status}"
            )

        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        self.canvas.setImage(QImage(rgb.data, rgb.shape[1], rgb.shape[0], 3 * rgb.shape[1],
                                    QImage.Format_RGB888).copy())

    # ---------------------------------------------------- Auto-Ausrichtung

    def _start_auto(self):
        self._auto = (al.MotionAccumulator(), time.monotonic() + AUTO_SECONDS)
        self.auto_btn.setEnabled(False)

    def _finish_auto(self):
        acc, _ = self._auto
        self._auto = None
        self.auto_btn.setEnabled(True)
        rect, score = acc.solve()
        if rect is None or score < MIN_AUTO_SCORE:
            QMessageBox.information(
                self,
                "Automatisch ausrichten",
                "Die Ausrichtung konnte nicht sicher bestimmt werden "
                f"(Übereinstimmung {score:.0%}).\n\n"
                "Bitte noch einmal versuchen und dabei deutlicher die Hand oder einen "
                "Gegenstand vor beiden Kameras bewegen – oder den Rahmen von Hand einstellen.",
            )
            return
        self._set_rect(rect)
        self._note = (f"<b>Automatisch ausgerichtet</b> (Übereinstimmung {score:.0%}) – bitte prüfen und speichern.",
                      time.monotonic() + 8)

    # --------------------------------------------------------------- Ende

    def _save(self):
        name = self.profile_combo.currentText() or al.DEFAULT_PROFILE
        self.store.set_alignment(self.rect, name)
        self.session.set_profile(name)
        self.accept()

    def done(self, result):
        self._timer.stop()
        self.session.end_alignment()
        super().done(result)
