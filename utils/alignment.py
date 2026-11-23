#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Ausrichtung Webcam <-> DVS und Bereichsauswahl.

Alle Bereiche sind normierte Rechtecke (x, y, w, h) mit Werten 0..1.

- Ausrichtung (pro Objektiv-Profil, gilt für alle Untersuchungen): wo das
  Bildfeld der DVS im Webcam-Bild liegt, in Webcam-Koordinaten. Das Rechteck darf
  größer als das Webcam-Bild sein (Werte < 0 oder > 1), wenn das DVS-Objektiv
  mehr sieht als die Webcam. Die Webcam hängt direkt über der DVS -> Skalierung +
  Verschiebung reicht (exakt für die eingestellte Entfernung).
- Gemeinsames Bildfeld: der Teil, den BEIDE Kameras sehen. "Ganzes Bild" heißt
  ab jetzt immer: gemeinsames Bildfeld. Sieht die DVS mehr, wird sie per
  Hardware-ROI darauf beschnitten; sieht die Webcam mehr, wird sie zugeschnitten.
- Bereich (ROI, optional pro NIHSS-Punkt): Teil des gemeinsamen Bildfelds.

Daraus folgen:
- Webcam-Zuschnitt  = Bereich, umgerechnet in Webcam-Koordinaten
- DVS-Hardware-ROI  = Bereich in DVS-Pixeln (None, wenn das der ganze Sensor ist)
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


def clamp_alignment(rect):
    """Ausrichtungsrechteck: darf über das Webcam-Bild hinausragen, muss sich
    aber mindestens um 10 % mit ihm überlappen."""
    x, y, w, h = (float(v) for v in rect)
    w = min(max(w, 0.1), 4.0)
    h = min(max(h, 0.1), 4.0)
    x = min(max(x, -w + 0.1), 0.9)
    y = min(max(y, -h + 0.1), 0.9)
    return (x, y, w, h)


def intersect(a, b):
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1 - x0, y1 - y0)


def is_full(rect, tol=1e-3):
    return rect is None or all(abs(a - b) < tol for a, b in zip(rect, FULL))


def common_fov(alignment):
    """Gemeinsames Bildfeld beider Kameras, in DVS-Koordinaten (0..1)."""
    ax, ay, aw, ah = alignment
    webcam_in_dvs = (-ax / aw, -ay / ah, 1.0 / aw, 1.0 / ah)
    return intersect(webcam_in_dvs, FULL) or FULL


def region_in_dvs(alignment, roi):
    """Bereich (relativ zum gemeinsamen Bildfeld) -> DVS-Koordinaten."""
    cx, cy, cw, ch = common_fov(alignment)
    rx, ry, rw, rh = roi or FULL
    return (cx + rx * cw, cy + ry * ch, rw * cw, rh * ch)


def compose(alignment, roi):
    """Bereich (relativ zum gemeinsamen Bildfeld) -> Rechteck im Webcam-Bild."""
    ax, ay, aw, ah = alignment
    dx, dy, dw, dh = region_in_dvs(alignment, roi)
    rect = (ax + dx * aw, ay + dy * ah, dw * aw, dh * ah)
    return intersect(rect, FULL) or FULL


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


def dvs_window(rect, sensor=DVS_SIZE):
    """Rechteck in DVS-Koordinaten -> Pixelfenster, oder None für 'ganzer Sensor'."""
    if is_full(rect):
        return None
    return to_pixels(rect, sensor[0], sensor[1])


def dvs_region_window(alignment, roi, sensor=DVS_SIZE):
    """Hardware-ROI der DVS für Ausrichtung + Bereich, oder None für 'ganzer Sensor'."""
    return dvs_window(region_in_dvs(alignment, roi), sensor)


def keep_aspect(rect, frame_w, frame_h, target_aspect=16 / 9):
    """Passt die Höhe eines Ausrichtungsrechtecks an das DVS-Seitenverhältnis an."""
    x, y, w, h = rect
    h = w * frame_w / frame_h / target_aspect
    return clamp_alignment((x, y + (rect[3] - h) / 2, w, h))


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
        return clamp_alignment(p.get("alignment", DEFAULT_ALIGNMENT))

    def set_alignment(self, rect, profile=None):
        p = self.data["profiles"].setdefault(profile or self.active_profile, {})
        p["alignment"] = [round(v, 5) for v in clamp_alignment(rect)]
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
        mask = cv2.resize(event_mask(dvs_frame), (320, 180), interpolation=cv2.INTER_AREA)
        if self._prev is not None:
            motion = np.abs(gray - self._prev)
            self.webcam_acc = motion if self.webcam_acc is None else self.webcam_acc + motion
            self.dvs_acc = mask if self.dvs_acc is None else self.dvs_acc + mask
            self.samples += 1
        self._prev = gray

    def solve(self, min_scale=0.25, max_scale=2.5, steps=90):
        """
        Sucht Skalierung + Verschiebung, bei der DVS- und Webcam-Bewegung am
        besten übereinanderliegen (Template-Matching über mehrere Skalen).
        scale = Breite des DVS-Bildfelds relativ zur Webcam-Breite:
        < 1: DVS sieht einen Ausschnitt der Webcam -> DVS-Bild in Webcam suchen
        > 1: DVS sieht mehr als die Webcam        -> Webcam-Bild in DVS suchen
        Returns: (Ausrichtungsrechteck, Güte 0..1) oder (None, 0).
        """
        if self.samples < 5 or self.webcam_acc is None:
            return None, 0.0
        cam = cv2.GaussianBlur(self.webcam_acc, (5, 5), 0)
        dvs = cv2.GaussianBlur(self.dvs_acc, (5, 5), 0)
        if cam.std() < 1e-6 or dvs.std() < 1e-6:
            return None, 0.0
        H, W = cam.shape
        Hd, Wd = dvs.shape
        best = (None, -1.0)
        for scale in np.linspace(min_scale, max_scale, steps):
            if scale <= 1.0:
                tw = int(round(W * scale))
                th = int(round(tw * Hd / Wd))
                if tw < 16 or th < 9 or tw > W or th > H:
                    continue
                tmpl = cv2.resize(dvs, (tw, th), interpolation=cv2.INTER_AREA)
                if tmpl.std() < 1e-6:
                    continue
                res = cv2.matchTemplate(cam, tmpl, cv2.TM_CCOEFF_NORMED)
                _, score, _, loc = cv2.minMaxLoc(res)
                rect = (loc[0] / W, loc[1] / H, tw / W, th / H)
            else:
                tw = int(round(Wd / scale))
                th = int(round(tw * H / W))
                if tw < 16 or th < 9 or tw > Wd or th > Hd:
                    continue
                tmpl = cv2.resize(cam, (tw, th), interpolation=cv2.INTER_AREA)
                if tmpl.std() < 1e-6:
                    continue
                res = cv2.matchTemplate(dvs, tmpl, cv2.TM_CCOEFF_NORMED)
                _, score, _, loc = cv2.minMaxLoc(res)
                # Webcam-Bild in DVS-Koordinaten -> umkehren zur Ausrichtung
                wx, wy, ww, wh = loc[0] / Wd, loc[1] / Hd, tw / Wd, th / Hd
                aw, ah = 1.0 / ww, 1.0 / wh
                rect = (-wx * aw, -wy * ah, aw, ah)
            if score > best[1]:
                best = (rect, float(score))
        rect, score = best
        return (clamp_alignment(rect) if rect else None), max(score, 0.0)
