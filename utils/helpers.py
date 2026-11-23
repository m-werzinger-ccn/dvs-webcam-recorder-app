#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Hilfsfunktionen für die Webcam-Aufnahme-Anwendung.
"""

import os
import sys
import cv2
import time
import subprocess
import shutil
import numpy as np

# Import von config-Werten ohne zirkulären Import
from config import APP_NAME, APP_ICON

# PyAudio für Mikrofon-Erkennung
try:
    import pyaudio
    PYAUDIO_AVAILABLE = True
except ImportError:
    PYAUDIO_AVAILABLE = False
    print("PyAudio nicht verfügbar - Mikrofon-Erkennung deaktiviert")


def create_desktop_shortcut():
    """Erstellt eine Desktop-Verknüpfung für die Anwendung (nur Linux)"""
    if not sys.platform.startswith("linux"):
        return None
    # Unterstützung für verschiedene Desktop-Namen (Englisch und Deutsch)
    potential_desktop_paths = [
        os.path.join(os.path.expanduser("~"), "Desktop"),
        os.path.join(os.path.expanduser("~"), "Schreibtisch"),
    ]

    desktop_path = None
    for path in potential_desktop_paths:
        if os.path.exists(path) and os.path.isdir(path):
            desktop_path = path
            break

    if desktop_path is None:
        print("Desktop-Verzeichnis konnte nicht gefunden werden.")
        desktop_path = os.path.expanduser("~")  # Fallback auf Home-Verzeichnis

    shortcut_path = os.path.join(desktop_path, "WebcamRecorder.desktop")

    # Aktuellen Pfad der Anwendung
    app_path = os.path.abspath(sys.argv[0])

    with open(shortcut_path, "w") as f:
        f.write(
            f"""[Desktop Entry]
Version=1.0
Type=Application
Name={APP_NAME}
Comment=Aufnahme von Webcam-Videos
Exec=python3 {app_path}
Icon={APP_ICON}
Terminal=false
Categories=Video;AudioVideo;
"""
        )

    # Ausführbar machen
    os.chmod(shortcut_path, 0o755)
    return shortcut_path


def check_available_codecs():
    """Überprüft, welche Videocodecs auf dem System verfügbar sind"""
    available_codecs = []

    # Liste der zu testenden Codecs
    codecs_to_test = [
        ("mp4v", "MP4 (H.264)"),
        ("avc1", "MP4 (H.264 alternative)"),
        ("X264", "MP4 (X264)"),
        ("XVID", "AVI (XVID)"),
        ("MJPG", "AVI (Motion JPEG)"),
        ("DIVX", "AVI (DivX)"),
    ]

    # Temporäre Datei für Tests
    temp_dir = os.path.join(os.path.expanduser("~"), ".webcam_recorder_temp")
    os.makedirs(temp_dir, exist_ok=True)

    # Testbild erstellen (einfarbig)
    test_frame = np.zeros((480, 640, 3), dtype=np.uint8)

    for codec_id, codec_name in codecs_to_test:
        try:
            # Dateierweiterung basierend auf Codec bestimmen
            if codec_id in ["mp4v", "avc1", "X264"]:
                ext = ".mp4"
            else:
                ext = ".avi"

            test_file = os.path.join(temp_dir, f"test_{codec_id}{ext}")

            # VideoWriter mit diesem Codec testen
            fourcc = cv2.VideoWriter_fourcc(*codec_id)
            writer = cv2.VideoWriter(test_file, fourcc, 30.0, (640, 480))

            if writer.isOpened():
                # Ein Frame schreiben und Writer schließen
                writer.write(test_frame)
                writer.release()

                # Prüfen, ob die Datei existiert und größer als 0 Bytes ist
                if os.path.exists(test_file) and os.path.getsize(test_file) > 0:
                    available_codecs.append((codec_id, codec_name, ext))

                # Testdatei löschen
                if os.path.exists(test_file):
                    os.remove(test_file)

        except Exception as e:
            print(f"Codec {codec_id} nicht verfügbar: {e}")

    # Temporäres Verzeichnis aufräumen
    try:
        shutil.rmtree(temp_dir)
    except:
        pass

    return available_codecs


def get_best_codec():
    """Ermittelt den besten verfügbaren Codec für die Videoaufnahme"""
    available_codecs = check_available_codecs()

    # Priorisierte Liste von Codecs
    preferred_order = ["mp4v", "X264", "avc1", "XVID", "MJPG", "DIVX"]

    # Besten verfügbaren Codec basierend auf der Priorität finden
    for codec in preferred_order:
        for available_codec, codec_name, ext in available_codecs:
            if available_codec == codec:
                return available_codec, ext

    # Fallback auf XVID/AVI, wenn kein anderer Codec verfügbar ist
    return "XVID", ".avi"


def get_available_microphones():
    """
    Ermittelt verfügbare Audio-Input-Geräte (Mikrofone) und gibt eine Liste zurück.

    Returns:
        Liste von Tupeln: [(device_index, device_name), ...]
    """
    available_microphones = []

    if not PYAUDIO_AVAILABLE:
        print("PyAudio nicht verfügbar - keine Mikrofone erkannt")
        return available_microphones

    try:
        audio = pyaudio.PyAudio()

        # Alle Audio-Geräte durchgehen
        device_count = audio.get_device_count()
        print(f"Gefundene Audio-Geräte: {device_count}")

        for i in range(device_count):
            try:
                device_info = audio.get_device_info_by_index(i)

                # Nur Input-Geräte (Mikrofone) berücksichtigen
                max_input_channels = device_info.get('maxInputChannels', 0)

                if max_input_channels > 0:
                    device_name = device_info.get('name', f'Mikrofon {i}')

                    # Gerätename bereinigen (manchmal gibt es Encoding-Probleme)
                    try:
                        device_name = device_name.encode('utf-8', errors='ignore').decode('utf-8')
                    except:
                        device_name = f'Mikrofon {i}'

                    available_microphones.append((i, device_name))
                    print(f"  Mikrofon gefunden: [{i}] {device_name} (Kanäle: {max_input_channels})")

            except Exception as e:
                print(f"  Fehler beim Lesen von Gerät {i}: {e}")

        audio.terminate()

    except Exception as e:
        print(f"Fehler bei der Mikrofon-Erkennung: {e}")

    return available_microphones
