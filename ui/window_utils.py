#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fenstergrößen an den tatsächlichen Bildschirm anpassen."""

from PyQt5.QtWidgets import QApplication


def fit_to_screen(widget, width_ratio=0.95, height_ratio=0.92):
    """Fenster auf einen Anteil der verfügbaren Bildschirmfläche setzen und zentrieren."""
    screen = None
    if hasattr(widget, "screen"):
        try:
            screen = widget.screen()
        except Exception:
            screen = None
    if screen is None:
        screen = QApplication.primaryScreen()
    if screen is None:
        return
    area = screen.availableGeometry()
    w = int(area.width() * width_ratio)
    h = int(area.height() * height_ratio)
    widget.resize(w, h)
    widget.move(area.x() + (area.width() - w) // 2, area.y() + (area.height() - h) // 2)
