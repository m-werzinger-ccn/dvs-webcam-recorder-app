#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Widget für die Videoanzeige. Zeichnet Frames direkt (schneller als QLabel) und
kann optional einen Bereich per Maus aufziehen lassen.
"""

from PyQt5.QtCore import QPoint, QRect, QSize, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import QSizePolicy, QWidget


class VideoDisplayWidget(QWidget):
    """Zeigt Videoframes an; im Auswahlmodus lässt sich ein Rechteck aufziehen."""

    # normiertes Rechteck (x, y, w, h) bezogen auf das angezeigte Bild
    region_selected = pyqtSignal(tuple)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pixmap = None
        self.setMinimumSize(320, 180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self._target = QRect()
        self._selecting = False
        self._drag_start = None
        self._drag_end = None
        self._overlay = None  # normiertes Rechteck, das zusätzlich eingezeichnet wird

    # --------------------------------------------------------------- Bilder

    def setImage(self, image):
        if image.isNull():
            return
        self.pixmap = QPixmap.fromImage(image)
        self.update()

    def setPixmap(self, pixmap):
        if pixmap.isNull():
            return
        self.pixmap = pixmap
        self.update()

    def clear(self):
        self.pixmap = None
        self.update()

    def setText(self, text):
        """Kompatibilität mit QLabel: zeigt Text auf schwarzem Hintergrund."""
        pixmap = QPixmap(max(self.width(), 320), max(self.height(), 180))
        pixmap.fill(Qt.black)
        painter = QPainter(pixmap)
        painter.setPen(Qt.white)
        painter.drawText(pixmap.rect(), Qt.AlignCenter, text)
        painter.end()
        self.setPixmap(pixmap)

    # ------------------------------------------------------------- Auswahl

    def set_selection_enabled(self, enabled):
        self._selecting = enabled
        self._drag_start = self._drag_end = None
        self.setCursor(Qt.CrossCursor if enabled else Qt.ArrowCursor)
        self.update()

    def set_overlay_rect(self, rect):
        self._overlay = rect
        self.update()

    def _to_norm(self, point):
        t = self._target
        if t.width() <= 0 or t.height() <= 0:
            return None
        x = (point.x() - t.x()) / t.width()
        y = (point.y() - t.y()) / t.height()
        return min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0)

    def mousePressEvent(self, event):
        if self._selecting and event.button() == Qt.LeftButton:
            self._drag_start = self._drag_end = event.pos()
            self.update()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._selecting and self._drag_start is not None:
            self._drag_end = event.pos()
            self.update()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._selecting and self._drag_start is not None and event.button() == Qt.LeftButton:
            a, b = self._to_norm(self._drag_start), self._to_norm(event.pos())
            self._drag_start = self._drag_end = None
            self.update()
            if a and b:
                x0, x1 = sorted((a[0], b[0]))
                y0, y1 = sorted((a[1], b[1]))
                self.region_selected.emit((x0, y0, x1 - x0, y1 - y0))
        else:
            super().mouseReleaseEvent(event)

    # ------------------------------------------------------------ Zeichnen

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.black)
        if self.pixmap is None:
            return

        # Zielrechteck mit gleichem Seitenverhältnis, zentriert. Direkt in das
        # Rechteck zeichnen statt jedes Mal eine skalierte Kopie anzulegen.
        pw, ph = self.pixmap.width(), self.pixmap.height()
        scale = min(self.width() / pw, self.height() / ph)
        w, h = int(pw * scale), int(ph * scale)
        self._target = QRect((self.width() - w) // 2, (self.height() - h) // 2, w, h)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.drawPixmap(self._target, self.pixmap)

        t = self._target
        if self._overlay is not None:
            x, y, ow, oh = self._overlay
            painter.setPen(QPen(QColor("#ffc107"), 2, Qt.DashLine))
            painter.drawRect(QRect(int(t.x() + x * t.width()), int(t.y() + y * t.height()),
                                   int(ow * t.width()), int(oh * t.height())))
        if self._selecting:
            painter.setPen(QPen(QColor("#ffc107"), 2))
            if self._drag_start is not None and self._drag_end is not None:
                painter.fillRect(QRect(self._drag_start, self._drag_end).normalized(),
                                 QColor(255, 193, 7, 50))
                painter.drawRect(QRect(self._drag_start, self._drag_end).normalized())
            else:
                painter.drawText(t.adjusted(8, 8, -8, -8), Qt.AlignTop | Qt.AlignLeft,
                                 "Bereich mit der Maus aufziehen")

    def sizeHint(self):
        return QSize(640, 360)

    def minimumSizeHint(self):
        return QSize(320, 180)
