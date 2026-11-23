#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Ausrichtung Webcam <-> DVS und Bereichsauswahl.

Alle Bereiche sind normierte Rechtecke (x, y, w, h) mit Werten 0..1.

- Ausrichtung (pro Objektiv-Profil): der Ausschnitt des Webcam-Bilds, der dem
  vollen Bildfeld der DVS entspricht. Die Webcam hängt direkt über der DVS und
  sieht fast dasselbe, nur mit anderem Objektiv -> Skalierung + Verschiebung
  reicht (exakt für die eingestellte Entfernung, also das Patientenbett).
- Bereich (ROI): Teil des DVS-Bildfelds, z. B. Gesicht oder Arm.

Daraus folgen:
- Webcam-Zuschnitt  = Bereich, umgerechnet in Webcam-Koordinaten
- DVS-Hardware-ROI  = Bereich in DVS-Pixeln
"""

import json
import os

import cv2
import numpy as np

FULL = (0.0, 0.0, 1.0, 1.0)
DVS_SIZE = (1280, 720)
# bisheriges Standardverhalten der App: mittiger 1,5-facher Zoom
DEFAULT_ALIGNMENT = ((1 - 1 / 1.5) / 2, (1 - 1 / 1.5) / 2, 1 / 1.5, 1 / 1.5)
DEFAULT_PROFILE = "Standard"
MIN_ROI_SIZE = 0.05  # Bereiche kleiner als 5 % des Bildfelds werden ignoriert


# ------------------------------------------------------------------ Geometrie


def clamp_rect(rect, min_size=0.01):
    x, y, w, h = (float(v) for v in rect)
    w = min(max(w, min_size), 1.0)
    h = min(max(h, min_size), 1.0)
    x = min(max(x, 0.0), 1.0 - w)
    y = min(max(y, 0.0), 1.0 - h)
    return (x, y, w, h)


def is_full(rect, tol=1e-3):
    return rect is None or all(abs(a - b) < tol for a, b in zip(rect, FULL))


def compose(alignment, roi):
    """Bereich (in DVS-Koordinaten) -> Rechteck im Webcam-Bild."""
    ax, ay, aw, ah = alignment
    rx, ry, rw, rh = roi or FULL
    return (ax + rx * aw, ay + ry * ah, rw * aw, rh * ah)


def to_pixels(rect, width, height, even=False):
    """Normiertes Rechteck -> Pixel (x, y, w, h), innerhalb des Bilds."""
    x, y, w, h = rect
    px, py = int(round(x * width)), int(round(y * height))
    pw, ph = int(round(w * width)), int(round(h * height))
    px, py = max(0, min(px, width - 2)), max(0, min(py, height - 2))
    pw, ph = max(2, min(pw, width - px)), max(2, min(ph, height - py))
    if even:  # H.264 (yuv420p) braucht gerade Kantenlängen
        pw -= pw % 2
        ph -= ph % 2
    return px, py, pw, ph


def crop(frame, rect):
    """Schneidet ein normiertes Rechteck aus einem Bild (gerade Kantenlängen)."""
    if rect is None or is_full(rect):
        h, w = frame.shape[:2]
        if w % 2 == 0 and h % 2 == 0:
            return frame
        return frame[: h - h % 2, : w - w % 2]
    h, w = frame.shape[:2]
    x, y, cw, ch = to_pixels(rect, w, h, even=True)
    return frame[y : y + ch, x : x + cw]


def dvs_window(roi, sensor=DVS_SIZE):
    """Bereich -> Hardware-ROI-Fenster der DVS in Pixeln, oder None für 'alles'."""
    if is_full(roi):
        return None
    return to_pixels(roi, sensor[0], sensor[1])


def keep_aspect(rect, frame_w, frame_h, target_aspect=16 / 9):
    """Passt die Höhe eines Ausrichtungsrechtecks an das DVS-Seitenverhältnis an."""
    x, y, w, h = rect
    h = w * frame_w / frame_h / target_aspect
    return clamp_rect((x, y + (rect[3] - h) / 2, w, h))


# ------------------------------------------------------------ Profile (Datei)


def _config_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "straiver-recorder", "alignment.json")


class AlignmentStore:
    """Speichert Ausrichtung pro Objektiv-Profil und zuletzt benutzte Bereiche
    pro NIHSS-Punkt in ~/.config/straiver-recorder/alignment.json."""

    def __init__(self, path=None):
        self.path = path or _config_path()
        self.data = {"active_profile": DEFAULT_PROFILE, "profiles": {}}
        self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                self.data.update(loaded)
        except (OSError, ValueError):
            pass
        self.data.setdefault("profiles", {})
        self.data["profiles"].setdefault(DEFAULT_PROFILE, {"alignment": list(DEFAULT_ALIGNMENT)})

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    @property
    def profiles(self):
        return sorted(self.data["profiles"])

    @property
    def active_profile(self):
        name = self.data.get("active_profile", DEFAULT_PROFILE)
        return name if name in self.data["profiles"] else DEFAULT_PROFILE

    def set_active_profile(self, name):
        self.data["profiles"].setdefault(name, {"alignment": list(self.alignment())})
        self.data["active_profile"] = name
        self.save()

    def alignment(self, profile=None):
        p = self.data["profiles"].get(profile or self.active_profile, {})
        return clamp_rect(p.get("alignment", DEFAULT_ALIGNMENT))

    def set_alignment(self, rect, profile=None):
        p = self.data["profiles"].setdefault(profile or self.active_profile, {})
        p["alignment"] = [round(v, 5) for v in clamp_rect(rect)]
        self.save()

    def roi(self, item, profile=None):
        p = self.data["profiles"].get(profile or self.active_profile, {})
        r = p.get("rois", {}).get(item)
        return clamp_rect(r) if r else None

    def set_roi(self, item, rect, profile=None):
        p = self.data["profiles"].setdefault(profile or self.active_profile, {})
        rois = p.setdefault("rois", {})
        if rect is None or is_full(rect):
            rois.pop(item, None)
        else:
            rois[item] = [round(v, 5) for v in clamp_rect(rect)]
        self.save()


# ------------------------------------------------------- Auto-Ausrichtung


def event_mask(dvs_frame):
    """Maske der Pixel mit Events aus einem DVS-Vorschaubild (Hintergrund = häufigste Farbe)."""
    small = dvs_frame[::8, ::8].reshape(-1, dvs_frame.shape[2]).astype(np.int16)
    background = np.median(small, axis=0)
    diff = np.abs(dvs_frame.astype(np.int16) - background).sum(axis=2)
    return (diff > 40).astype(np.float32)


class MotionAccumulator:
    """Sammelt Bewegung in Webcam (Bilddifferenzen) und DVS (Events) für die Auto-Ausrichtung."""

    WORK_WIDTH = 320  # Arbeitsauflösung der Webcam (Breite)

    def __init__(self):
        self.webcam_acc = None
        self.dvs_acc = None
        self._prev = None
        self.samples = 0

    def add(self, webcam_bgr, dvs_frame):
        h, w = webcam_bgr.shape[:2]
        work_h = int(round(self.WORK_WIDTH * h / w))
        gray = cv2.cvtColor(cv2.resize(webcam_bgr, (self.WORK_WIDTH, work_h), interpolation=cv2.INTER_AREA),
                            cv2.COLOR_BGR2GRAY).astype(np.float32)
        gray = cv2.GaussianBlur(gray, (3, 3), 0)
        mask = cv2.resize(event_mask(dvs_frame), (160, 90), interpolation=cv2.INTER_AREA)
        if self._prev is not None:
            motion = np.abs(gray - self._prev)
            self.webcam_acc = motion if self.webcam_acc is None else self.webcam_acc + motion
            self.dvs_acc = mask if self.dvs_acc is None else self.dvs_acc + mask
            self.samples += 1
        self._prev = gray

    def solve(self, min_scale=0.25, max_scale=1.0, steps=60):
        """
        Sucht Skalierung + Verschiebung, bei der das DVS-Bewegungsbild am besten
        zur Webcam-Bewegung passt (Template-Matching über mehrere Skalen).
        Returns: (rect normiert im Webcam-Bild, Güte 0..1) oder (None, 0).
        """
        if self.samples < 5 or self.webcam_acc is None:
            return None, 0.0
        cam = cv2.GaussianBlur(self.webcam_acc, (5, 5), 0)
        dvs = cv2.GaussianBlur(self.dvs_acc, (3, 3), 0)
        if cam.std() < 1e-6 or dvs.std() < 1e-6:
            return None, 0.0
        H, W = cam.shape
        best = (None, -1.0)
        for scale in np.linspace(min_scale, max_scale, steps):
            tw = int(round(W * scale))
            th = int(round(tw * 9 / 16))
            if tw < 16 or th < 9 or tw > W or th > H:
                continue
            tmpl = cv2.resize(dvs, (tw, th), interpolation=cv2.INTER_AREA)
            if tmpl.std() < 1e-6:
                continue
            res = cv2.matchTemplate(cam, tmpl, cv2.TM_CCOEFF_NORMED)
            _, score, _, loc = cv2.minMaxLoc(res)
            if score > best[1]:
                best = ((loc[0] / W, loc[1] / H, tw / W, th / H), float(score))
        rect, score = best
        return (clamp_rect(rect) if rect else None), max(score, 0.0)
