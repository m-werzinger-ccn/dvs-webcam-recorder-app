#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Kameras ausrichten: DVS-Events werden über das Webcam-Bild gelegt.

Der gelbe Rahmen ist der Teil des Webcam-Bilds, der dem Bildfeld der DVS
entspricht. Man verschiebt ihn mit der Maus und vergrößert/verkleinert ihn mit
dem Mausrad, bis die türkisen Events auf den passenden Kanten im Webcam-Bild
liegen – oder lässt die App das automatisch machen.

Sieht das DVS-Objektiv mehr als die Webcam, ragt der Rahmen über das
Webcam-Bild hinaus (grauer Rand) – dann wird später die DVS auf das gemeinsame
Bildfeld beschnitten.

Die Ausrichtung gilt pro Objektiv-Profil für alle Untersuchungen (bei
Objektivwechsel Profil wählen oder neu anlegen) und stimmt exakt für die
Entfernung, in der ausgerichtet wurde – also am besten an der Untersuchungsposition.
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
    QSlider,
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

    def _to_webcam(self, pos):
        n = self._to_norm(pos)
        return self.dialog.view_to_webcam(n) if n else None

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._last = self._to_webcam(event.pos())
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if self._last is None:
            return
        cur = self._to_webcam(event.pos())
        if cur:
            self.dialog.move_rect(cur[0] - self._last[0], cur[1] - self._last[1])
            self._last = cur

    def mouseReleaseEvent(self, event):
        self._last = None
        self.setCursor(Qt.OpenHandCursor)
        self.dialog.update_view()

    def wheelEvent(self, event):
        steps = event.angleDelta().y() / 120.0
        self.dialog.scale_rect(0.97 ** steps, self._to_webcam(event.pos()))
        self.dialog.update_view()

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
            return
        self.dialog.update_view()


class AlignmentDialog(QDialog):
    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
        self.store = session.alignment_store
        self.rect = session.alignment
        self.full_size = (1280, 720)
        self._auto = None  # (MotionAccumulator, Endzeit) während der Auto-Ausrichtung
        self._note = ("", 0.0)  # (Text, sichtbar bis)
        # angezeigter Ausschnitt in Webcam-Koordinaten (Webcam-Bild + Rahmen + Rand);
        # wird nicht während des Ziehens geändert, damit nichts "wegrutscht"
        self.view = (0.0, 0.0, 1.0, 1.0)

        self.setWindowTitle("Kameras ausrichten")
        self._build()
        self.update_view()
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
            "zeigt, wo das Bildfeld der DVS im Webcam-Bild liegt – er darf über das Webcam-Bild "
            "hinausragen, wenn das DVS-Objektiv mehr sieht.<br>"
            "Rahmen mit der Maus <b>verschieben</b>, mit dem <b>Mausrad</b> oder dem Regler unten vergrößern/verkleinern "
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

        size_row = QHBoxLayout()
        size_row.addWidget(QLabel("Größe des DVS-Bildfelds:"))
        self.size_slider = QSlider(Qt.Horizontal)
        self.size_slider.setRange(20, 300)  # Prozent der Webcam-Breite
        self.size_slider.setToolTip(
            "Unter 100 %: die DVS sieht einen Ausschnitt der Webcam.\n"
            "Über 100 %: die DVS sieht mehr als die Webcam (weiteres Objektiv)."
        )
        self.size_slider.valueChanged.connect(self._on_size_slider)
        size_row.addWidget(self.size_slider, 1)
        self.size_label = QLabel("")
        self.size_label.setMinimumWidth(230)
        size_row.addWidget(self.size_label)
        layout.addLayout(size_row)
        self._sync_slider()

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
        self.rect = al.keep_aspect(al.clamp_alignment(rect), w, h)
        self._sync_slider()
        self.update_view()

    def move_rect(self, dx, dy):
        x, y, w, h = self.rect
        self.rect = al.clamp_alignment((x + dx, y + dy, w, h))

    def scale_rect(self, factor, center=None):
        x, y, w, h = self.rect
        cx, cy = center if center else (x + w / 2, y + h / 2)
        nw = min(max(w * factor, 0.2), 3.0)
        nh = h * nw / w
        nx = cx - (cx - x) * nw / w
        ny = cy - (cy - y) * nh / h
        self.rect = al.clamp_alignment((nx, ny, nw, nh))
        self._sync_slider()

    def _sync_slider(self):
        if not hasattr(self, "size_slider"):
            return
        pct = int(round(self.rect[2] * 100))
        self.size_slider.blockSignals(True)
        self.size_slider.setValue(pct)
        self.size_slider.blockSignals(False)
        if pct < 98:
            hint = "DVS sieht einen Ausschnitt der Webcam"
        elif pct > 102:
            hint = "DVS sieht mehr als die Webcam"
        else:
            hint = "gleich groß"
        self.size_label.setText(f"{pct} % der Webcam-Breite ({hint})")

    def _on_size_slider(self, value):
        self.scale_rect(value / 100.0 / self.rect[2])
        self.update_view()

    def update_view(self):
        """Angezeigten Bereich so wählen, dass Webcam-Bild und Rahmen hineinpassen."""
        x0, y0 = min(0.0, self.rect[0]), min(0.0, self.rect[1])
        x1 = max(1.0, self.rect[0] + self.rect[2])
        y1 = max(1.0, self.rect[1] + self.rect[3])
        pad_x, pad_y = 0.04 * (x1 - x0), 0.04 * (y1 - y0)
        if x0 < 0 or x1 > 1 or y0 < 0 or y1 > 1:
            x0, x1, y0, y1 = x0 - pad_x, x1 + pad_x, y0 - pad_y, y1 + pad_y
        # gleiches Seitenverhältnis wie das Webcam-Bild beibehalten
        w, h = x1 - x0, y1 - y0
        if w > h:
            y0 -= (w - h) / 2
            h = w
        else:
            x0 -= (h - w) / 2
            w = h
        self.view = (x0, y0, w, h)

    def view_to_webcam(self, n):
        vx, vy, vw, vh = self.view
        return vx + n[0] * vw, vy + n[1] * vh

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
        H, W = cam.shape[:2]
        if (W, H) != self.full_size:
            self.full_size = (W, H)
            self.update_view()

        # Leinwand für den angezeigten Ausschnitt (Webcam + ggf. grauer Rand)
        vx, vy, vw, vh = self.view
        out_w = 1280
        scale = out_w / (vw * W)
        out_h = int(round(vh * H * scale))
        canvas = np.full((out_h, out_w, 3), 70, dtype=np.float32)

        def to_px(nx, ny):
            return int(round((nx - vx) * W * scale)), int(round((ny - vy) * H * scale))

        cx0, cy0 = to_px(0, 0)
        cx1, cy1 = to_px(1, 1)
        cam_small = cv2.resize(cam, (cx1 - cx0, cy1 - cy0), interpolation=cv2.INTER_AREA)
        self._paste(canvas, cam_small.astype(np.float32) * 0.75, cx0, cy0)

        rx0, ry0 = to_px(self.rect[0], self.rect[1])
        rx1, ry1 = to_px(self.rect[0] + self.rect[2], self.rect[1] + self.rect[3])
        if dvs is not None and rx1 - rx0 > 4 and ry1 - ry0 > 4:
            mask = cv2.resize(al.event_mask(dvs), (rx1 - rx0, ry1 - ry0), interpolation=cv2.INTER_AREA)
            alpha = np.clip(mask * 1.5, 0, 1)[..., None]
            self._blend(canvas, alpha, rx0, ry0)
        img = canvas.clip(0, 255).astype(np.uint8)
        cv2.rectangle(img, (cx0, cy0), (cx1 - 1, cy1 - 1), (200, 200, 200), 1)
        cv2.rectangle(img, (rx0, ry0), (rx1 - 1, ry1 - 1), (0, 193, 255), 2)
        x, y, w, h = al.to_pixels(al.intersect(self.rect, al.FULL) or al.FULL, W, H)

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
            common = al.common_fov(self.rect)
            extra = ""
            if not al.is_full(common):
                extra = f" · DVS wird auf {common[2] * common[3]:.0%} ihres Bildfelds beschnitten"
            self.info_label.setText(
                f"Gemeinsames Bildfeld in der Webcam: {w}×{h} von {W}×{H} Pixeln{extra}  {status}"
            )

        rgb = np.ascontiguousarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
        self.canvas.setImage(QImage(rgb.data, rgb.shape[1], rgb.shape[0], 3 * rgb.shape[1],
                                    QImage.Format_RGB888).copy())

    @staticmethod
    def _clip_box(canvas, x, y, w, h):
        H, W = canvas.shape[:2]
        x0, y0, x1, y1 = max(x, 0), max(y, 0), min(x + w, W), min(y + h, H)
        return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else None

    def _paste(self, canvas, img, x, y):
        box = self._clip_box(canvas, x, y, img.shape[1], img.shape[0])
        if box:
            x0, y0, x1, y1 = box
            canvas[y0:y1, x0:x1] = img[y0 - y : y1 - y, x0 - x : x1 - x]

    def _blend(self, canvas, alpha, x, y):
        box = self._clip_box(canvas, x, y, alpha.shape[1], alpha.shape[0])
        if box:
            x0, y0, x1, y1 = box
            a = alpha[y0 - y : y1 - y, x0 - x : x1 - x]
            region = canvas[y0:y1, x0:x1]
            region[:] = region * (1 - a) + OVERLAY_COLOR * a

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
