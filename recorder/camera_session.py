#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Kamera-Sitzung: hält Webcam und DVS für die GANZE Untersuchung offen.

Früher wurde für jede NIHSS-Karte ein neuer Recorder erzeugt und die DVS jedes
Mal geöffnet und wieder freigegeben. Das schnelle Öffnen/Schließen war eine
Hauptursache dafür, dass Kameras "plötzlich weg" waren. Jetzt gibt es genau
eine Sitzung pro Untersuchung; die Aufnahmefenster hängen sich nur an.
"""

import time

from PyQt5.QtCore import QObject, QTimer, pyqtSignal
from PyQt5.QtGui import QImage

from recorder.dvs_recorder import DVSRecorder
from recorder.video_recorder import VideoRecorder
from utils.devices import find_prophesee_usb_devices, usb_reset, usb_speed_mbit
from utils.storage import check_storage

DVS_STALL_TIMEOUT_S = 4.0
STORAGE_CHECK_INTERVAL_S = 15

DEVICE_LABELS = {
    "webcam": "Webcam",
    "dvs": "DVS-Kamera",
    "mic": "Ext. Mikrofon",
    "storage": "Speicher",
}


class CameraSession(QObject):
    webcam_frame = pyqtSignal(QImage)
    dvs_frame = pyqtSignal(QImage)
    status_changed = pyqtSignal(str, str, str)  # Gerät, Zustand, Detail
    processing_status = pyqtSignal(str)
    video_processing_completed = pyqtSignal()
    recording_interrupted = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, camera=None, save_dir="", audio_enabled=True, zoom_factor=1.5,
                 external_mic_enabled=False, external_mic_id=None, parent=None):
        super().__init__(parent)
        self.camera = camera
        self.save_dir = save_dir
        self.audio_enabled = audio_enabled
        self.zoom_factor = zoom_factor
        self.external_mic_enabled = external_mic_enabled
        self.external_mic_id = external_mic_id

        self.webcam = None
        self.dvs = None
        self.recording = False
        self.recording_with_dvs = False

        self.states = {d: ("connecting", "") for d in DEVICE_LABELS}
        self._storage_counter = 0
        self._dvs_usb2 = None
        # Threads, die sich nicht rechtzeitig beendet haben (z. B. in einem
        # USB-Aufruf hängen). Referenz halten, sonst stürzt Qt beim Aufräumen ab.
        self._zombies = []

        self._watchdog = QTimer(self)
        self._watchdog.setInterval(1000)
        self._watchdog.timeout.connect(self._on_watchdog)

    # ------------------------------------------------------------ Lebenszyklus

    def start(self):
        self._start_webcam()
        self._start_dvs()
        self.check_storage()
        self.check_microphone()
        self._watchdog.start()

    def stop(self):
        self._watchdog.stop()
        if self.recording:
            self.stop_recording()
        self._stop_webcam()
        self._stop_dvs()
        for t in self._zombies:
            if t.isRunning():
                print("[Session] Beende hängenden Kamera-Thread hart")
                t.terminate()
                t.wait(2000)
        self._zombies.clear()

    def _retire(self, thread):
        """Thread stoppen; hängt er, Signale trennen und Referenz behalten."""
        thread.stop()
        if thread.isRunning():
            for sig in (thread.update_frame, thread.status_changed,
                        thread.recording_interrupted, thread.error_signal):
                try:
                    sig.disconnect()
                except (TypeError, RuntimeError):
                    pass
            self._zombies.append(thread)
        self._zombies = [t for t in self._zombies if t.isRunning()]

    # ----------------------------------------------------------------- Status

    def _set_status(self, device, state, detail=""):
        if self.states.get(device) != (state, detail):
            self.states[device] = (state, detail)
            self.status_changed.emit(device, state, detail)

    def status(self, device):
        return self.states.get(device, ("unavailable", ""))

    def is_ready(self, device):
        return self.states.get(device, ("",))[0] == "ok"

    def check_storage(self):
        st = check_storage(self.save_dir)
        self._set_status("storage", st.state, st.message)
        return st

    def check_microphone(self):
        if not self.external_mic_enabled or self.external_mic_id is None:
            self._set_status("mic", "unavailable", "nicht verwendet")
            return
        try:
            from utils import get_available_microphones

            ids = {mid for mid, _ in get_available_microphones()}
        except Exception:
            ids = set()
        if self.external_mic_id in ids:
            self._set_status("mic", "ok", "bereit")
        else:
            self._set_status("mic", "error", "Mikrofon nicht gefunden – neu einstecken")

    def _on_watchdog(self):
        # DVS meldet "ok", liefert aber keine Bilder mehr -> hängt
        if self.dvs is not None and self.dvs.state == "ok":
            stalled = time.monotonic() - self.dvs.last_frame_time > DVS_STALL_TIMEOUT_S
            if stalled:
                self._set_status(
                    "dvs", "error", "Keine Daten von der DVS-Kamera – bitte „Neu verbinden“"
                )
        self._storage_counter += 1
        if self._storage_counter >= STORAGE_CHECK_INTERVAL_S:
            self._storage_counter = 0
            self.check_storage()

    # ----------------------------------------------------------------- Webcam

    def _start_webcam(self):
        if self.camera is None:
            self._set_status("webcam", "error", "Keine Webcam gefunden – einstecken und „Neu verbinden“")
            return
        self.webcam = VideoRecorder(
            source=self.camera.source,
            camera_name=self.camera.name,
            save_dir=self.save_dir,
            audio_enabled=self.audio_enabled,
            zoom_factor=self.zoom_factor,
        )
        w = self.webcam
        w.update_frame.connect(self.webcam_frame.emit)
        w.status_changed.connect(lambda s, d: self._set_status("webcam", s, d))
        w.processing_status.connect(self.processing_status.emit)
        w.video_processing_completed.connect(self.video_processing_completed.emit)
        w.recording_interrupted.connect(self.recording_interrupted.emit)
        w.error_signal.connect(self.error.emit)
        w.start()

    def _stop_webcam(self):
        if self.webcam is not None:
            self._retire(self.webcam)
            self.webcam = None

    def set_camera(self, camera):
        """Andere Webcam wählen (nicht während einer Aufnahme)."""
        if self.recording:
            return False
        self.camera = camera
        if self.webcam is not None and camera is not None:
            return self.webcam.set_source(camera.source, camera.name)
        self._stop_webcam()
        self._start_webcam()
        return True

    def reconnect_webcam(self, camera=None):
        """Webcam neu verbinden; unter Linux inkl. Software-USB-Reset."""
        if self.recording:
            return False, "Während einer Aufnahme nicht möglich"
        if camera is not None:
            self.camera = camera
        self._stop_webcam()
        msg = ""
        if self.camera is not None and self.camera.usb_path:
            reset_ok, reset_msg = usb_reset(self.camera.usb_path)
            msg = "" if reset_ok else reset_msg
        self._set_status("webcam", "connecting", "Wird neu verbunden…")
        QTimer.singleShot(2500, self._start_webcam)
        return True, msg

    # -------------------------------------------------------------------- DVS

    def _start_dvs(self):
        self.dvs = DVSRecorder()
        d = self.dvs
        d.update_frame.connect(self.dvs_frame.emit)
        d.status_changed.connect(self._on_dvs_status)
        d.recording_interrupted.connect(self.recording_interrupted.emit)
        d.error_signal.connect(self.error.emit)
        d.start()

    def _on_dvs_status(self, state, detail):
        if state == "ok":
            if self._dvs_usb2 is None:  # einmal pro Verbindung prüfen
                self._dvs_usb2 = any(
                    0 < usb_speed_mbit(p) < 5000 for p in find_prophesee_usb_devices()
                )
            if self._dvs_usb2:
                state, detail = "warn", detail + " · nur USB 2.0 – USB-3-Port/Kabel verwenden!"
        else:
            self._dvs_usb2 = None
        self._set_status("dvs", state, detail)

    def _stop_dvs(self):
        if self.dvs is not None:
            self._retire(self.dvs)
            self.dvs = None

    def reconnect_dvs(self):
        """DVS-Thread neu starten; unter Linux vorher Software-USB-Reset."""
        if self.recording:
            return False, "Während einer Aufnahme nicht möglich"
        self._stop_dvs()
        messages = []
        for dev in find_prophesee_usb_devices():
            ok, msg = usb_reset(dev)
            if not ok:
                messages.append(msg)
        self._set_status("dvs", "connecting", "Wird neu verbunden…")
        QTimer.singleShot(3000, self._start_dvs)
        return True, " ".join(messages)

    # ------------------------------------------------------------------- Zoom

    def set_zoom(self, factor):
        self.zoom_factor = factor
        if self.webcam is not None:
            self.webcam.set_zoom(factor)

    def set_audio_enabled(self, enabled):
        self.audio_enabled = enabled
        if self.webcam is not None:
            self.webcam.has_audio = enabled

    # --------------------------------------------------------------- Aufnahme

    def start_recording(self, save_dir, file_name, use_dvs=True, webcam_audio=None):
        """
        Startet die Aufnahme auf allen gewünschten Kameras.
        Returns: dict Gerät -> (erfolg, meldung)
        """
        results = {}
        if self.recording:
            return {"webcam": (False, "Aufnahme läuft bereits")}

        if self.webcam is not None:
            if webcam_audio is not None:
                self.webcam.has_audio = webcam_audio
            results["webcam"] = self.webcam.start_recording(save_dir, file_name)
        else:
            results["webcam"] = (False, "Keine Webcam verbunden")

        if use_dvs:
            if self.dvs is not None and self.status("dvs")[0] in ("ok", "warn"):
                results["dvs"] = self.dvs.start_recording(save_dir, file_name)
            else:
                results["dvs"] = (False, self.status("dvs")[1] or "DVS-Kamera nicht bereit")

        self.recording = any(ok for ok, _ in results.values())
        self.recording_with_dvs = bool(results.get("dvs", (False,))[0])
        return results

    def stop_recording(self):
        results = {}
        if self.webcam is not None and self.webcam.recording:
            results["webcam"] = (self.webcam.stop_recording(), "")
        if self.dvs is not None and self.recording_with_dvs:
            results["dvs"] = self.dvs.stop_recording()
        self.recording = False
        self.recording_with_dvs = False
        # Webcam-Audio-Einstellung wiederherstellen (kann für ext. Mikro aus sein)
        if self.webcam is not None:
            self.webcam.has_audio = self.audio_enabled
        return results
