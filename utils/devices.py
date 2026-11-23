#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Geräteerkennung für Webcam und DVS-Kamera – plattformabhängig.

Linux:  Webcams werden über /sys/class/video4linux gefunden, ohne sie zu öffnen
        (kein Blockieren, keine Metadaten-Knoten). Geöffnet wird über den stabilen
        Pfad /dev/v4l/by-id/..., der sich beim Umstecken nicht ändert.
macOS:  Webcams werden über AVFoundation-Indizes geprüft (für die Entwicklung).
Windows: DirectShow-Indizes.

Außerdem: Software-USB-Reset (Linux), damit niemand Kabel umstecken muss.
"""

import fcntl
import glob
import os
import platform
import subprocess
import time
from dataclasses import dataclass, field

SYSTEM = platform.system()
IS_LINUX = SYSTEM == "Linux"
IS_MAC = SYSTEM == "Darwin"
IS_WINDOWS = SYSTEM == "Windows"

# Prophesee-EVKs melden sich je nach Generation mit der Prophesee-Vendor-ID
# oder mit der Cypress-FX3-Vendor-ID (04b4) und einer EVK-Produkt-ID.
PROPHESEE_VENDOR_IDS = {"31f7"}
CYPRESS_VENDOR_ID = "04b4"
PROPHESEE_CYPRESS_PRODUCT_IDS = {"00f3", "00f4", "00f5"}

PREFERRED_CAMERA_KEYWORDS = ("ugreen",)


@dataclass
class CameraInfo:
    """Beschreibt eine Webcam unabhängig von der Plattform."""

    key: str  # stabile Kennung (z. B. by-id-Name), für Einstellungen
    name: str  # Anzeigename
    source: object  # Argument für cv2.VideoCapture (Pfad oder Index)
    usb_path: str = ""  # sysfs-Pfad des USB-Geräts (nur Linux)
    extra: dict = field(default_factory=dict)

    def __str__(self):
        return self.name


# ---------------------------------------------------------------------------
# OpenCV-Backend
# ---------------------------------------------------------------------------


def opencv_backend():
    """Liefert das passende OpenCV-Capture-Backend für die Plattform."""
    import cv2

    if IS_LINUX:
        return cv2.CAP_V4L2
    if IS_MAC:
        return cv2.CAP_AVFOUNDATION
    if IS_WINDOWS:
        return cv2.CAP_DSHOW
    return cv2.CAP_ANY


# ---------------------------------------------------------------------------
# Webcams
# ---------------------------------------------------------------------------


def _read(path, default=""):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read().strip()
    except OSError:
        return default


def _usb_device_dir(sysfs_path):
    """Läuft von einem sysfs-Pfad nach oben bis zum USB-Gerät (hat idVendor)."""
    path = os.path.realpath(sysfs_path)
    while path and path != "/":
        if os.path.isfile(os.path.join(path, "idVendor")):
            return path
        path = os.path.dirname(path)
    return ""


def _by_id_links():
    """Mappt /dev/videoN -> /dev/v4l/by-id/... (bevorzugt -video-index0)."""
    mapping = {}
    for link in sorted(glob.glob("/dev/v4l/by-id/*")):
        target = os.path.realpath(link)
        # index0 ist der eigentliche Bildknoten, index1 meist Metadaten
        if target not in mapping or link.endswith("index0"):
            mapping[target] = link
    return mapping


def _is_prophesee_usb(usb_dir):
    vid = _read(os.path.join(usb_dir, "idVendor")).lower()
    pid = _read(os.path.join(usb_dir, "idProduct")).lower()
    text = (
        _read(os.path.join(usb_dir, "manufacturer"))
        + " "
        + _read(os.path.join(usb_dir, "product"))
    ).lower()
    if vid in PROPHESEE_VENDOR_IDS or "prophesee" in text:
        return True
    return vid == CYPRESS_VENDOR_ID and (
        pid in PROPHESEE_CYPRESS_PRODUCT_IDS or "evk" in text
    )


def _video_index(node):
    try:
        return int(os.path.basename(node)[len("video"):])
    except ValueError:
        return 10**6


def _list_cameras_linux(sysfs_root="/sys/class/video4linux", dev_root="/dev"):
    cameras = []
    seen_usb = set()
    by_id = _by_id_links()

    # numerisch sortieren (video2 vor video10)
    for node in sorted(glob.glob(os.path.join(sysfs_root, "video*")), key=_video_index):
        dev_name = os.path.basename(node)  # video0
        dev_path = os.path.join(dev_root, dev_name)
        if not os.path.exists(dev_path):
            continue

        # Nur den Bildknoten eines Geräts nehmen (index 0); Metadaten-Knoten
        # (index 1) liefern keine Bilder und würden beim Öffnen nur stören.
        if _read(os.path.join(node, "index"), "0") != "0":
            continue

        usb_dir = _usb_device_dir(os.path.join(node, "device"))
        if usb_dir and _is_prophesee_usb(usb_dir):
            continue  # DVS taucht nicht als Webcam auf
        # Manche Webcams (z. B. Logitech C920) haben mehrere Bildknoten –
        # pro USB-Gerät nur den ersten anzeigen.
        if usb_dir:
            if usb_dir in seen_usb:
                continue
            seen_usb.add(usb_dir)

        # Anzeigename: USB-Produktname (vollständig), sonst V4L2-Name
        # (der ist auf 32 Zeichen gekürzt und oft doppelt: "Kamera: Kamer").
        v4l_name = _read(os.path.join(node, "name")).split(":")[0].strip()
        product = _read(os.path.join(usb_dir, "product")) if usb_dir else ""
        name = " ".join((product or v4l_name or dev_name).split())

        stable = by_id.get(os.path.realpath(dev_path), "")
        key = os.path.basename(stable) if stable else (
            os.path.basename(usb_dir) + ":" + name if usb_dir else dev_name
        )
        cameras.append(
            CameraInfo(
                key=key,
                name=f"{name} ({dev_name})",
                source=stable or dev_path,
                usb_path=usb_dir,
            )
        )
    return cameras


def _mac_camera_names():
    """Kamera-Namen über system_profiler (Reihenfolge ~ AVFoundation-Index)."""
    try:
        import json

        out = subprocess.run(
            ["system_profiler", "SPCameraDataType", "-json"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
        return [c.get("_name", "") for c in json.loads(out).get("SPCameraDataType", [])]
    except Exception:
        return []


def _list_cameras_by_index(max_index=5):
    import cv2

    names = _mac_camera_names() if IS_MAC else []
    cameras = []
    for idx in range(max_index):
        cap = cv2.VideoCapture(idx, opencv_backend())
        try:
            if cap.isOpened():
                name = names[idx] if idx < len(names) and names[idx] else f"Kamera {idx}"
                cameras.append(
                    CameraInfo(key=f"{name}#{idx}", name=f"{name} ({idx})", source=idx)
                )
        finally:
            cap.release()
    return cameras


def list_cameras():
    """Gibt alle verfügbaren Webcams zurück, bevorzugte (UGREEN) zuerst."""
    try:
        cameras = _list_cameras_linux() if IS_LINUX else _list_cameras_by_index()
    except Exception as e:  # Erkennung darf die App nie abstürzen lassen
        print(f"Fehler bei der Kameraerkennung: {e}")
        cameras = []

    def rank(cam):
        lname = cam.name.lower()
        return 0 if any(k in lname for k in PREFERRED_CAMERA_KEYWORDS) else 1

    return sorted(cameras, key=rank)


# ---------------------------------------------------------------------------
# DVS (Prophesee) und USB-Reset
# ---------------------------------------------------------------------------


def find_prophesee_usb_devices():
    """sysfs-Pfade aller angeschlossenen Prophesee-Kameras (nur Linux)."""
    if not IS_LINUX:
        return []
    return [
        d
        for d in sorted(glob.glob("/sys/bus/usb/devices/*"))
        if os.path.isfile(os.path.join(d, "idVendor")) and _is_prophesee_usb(d)
    ]


def usb_speed_mbit(usb_dir):
    """USB-Geschwindigkeit in Mbit/s (480 = USB 2, 5000+ = USB 3) oder 0."""
    try:
        return int(float(_read(os.path.join(usb_dir, "speed"), "0")))
    except ValueError:
        return 0


USBDEVFS_RESET = 0x5514  # _IO('U', 20) aus linux/usbdevice_fs.h


def usb_port_reset(usb_dir):
    """
    Setzt den USB-Port eines Geräts zurück (wie das Tool 'usbreset').
    Die Kamera-Firmware startet dabei neu – das löst auch festgefahrene
    Zustände, die ein reines Ab-/Anmelden nicht löst. Braucht Schreibrecht
    auf /dev/bus/usb/BBB/DDD (für Prophesee-Kameras setzen das die
    Prophesee-udev-Regeln).
    """
    bus = int(_read(os.path.join(usb_dir, "busnum"), "0") or 0)
    dev = int(_read(os.path.join(usb_dir, "devnum"), "0") or 0)
    if not bus or not dev:
        raise OSError("Bus-/Gerätenummer nicht lesbar")
    fd = os.open(f"/dev/bus/usb/{bus:03d}/{dev:03d}", os.O_WRONLY)
    try:
        fcntl.ioctl(fd, USBDEVFS_RESET, 0)
    finally:
        os.close(fd)


def usb_reset(usb_dir, off_seconds=1.0):
    """
    Setzt ein USB-Gerät per Software zurück – wirkt wie Aus- und Einstecken.
    1. Versuch: Port-Reset (startet die Geräte-Firmware neu).
    2. Versuch: Gerät über <usb_dir>/authorized ab- und wieder anmelden
       (siehe setup/91-straiver-usb-reset.rules).

    Returns: (erfolg: bool, meldung: str)
    """
    if not IS_LINUX:
        return False, "USB-Reset ist nur unter Linux möglich."
    if not usb_dir:
        return False, "USB-Gerät nicht gefunden."
    try:
        usb_port_reset(usb_dir)
        return True, "USB-Port wurde zurückgesetzt."
    except OSError as e:
        print(f"[USB] Port-Reset nicht möglich ({e}) – versuche Ab-/Anmelden")

    auth = os.path.join(usb_dir, "authorized")
    try:
        with open(auth, "w") as f:
            f.write("0")
        time.sleep(off_seconds)
        with open(auth, "w") as f:
            f.write("1")
        return True, "USB-Gerät wurde neu verbunden."
    except PermissionError:
        return False, (
            "Keine Berechtigung für den USB-Reset. Bitte einmalig "
            "'bash setup/install_usb_rules.sh' ausführen – bis dahin das Kabel "
            "der Kamera kurz aus- und wieder einstecken."
        )
    except OSError as e:
        return False, f"USB-Reset fehlgeschlagen: {e}"
