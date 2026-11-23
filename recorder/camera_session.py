#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Kamera-Sitzung: hält Webcam und DVS für die GANZE Untersuchung offen.

Früher wurde für jede NIHSS-Karte ein neuer Recorder erzeugt und die DVS jedes
Mal geöffnet und wieder freigegeben. Das schnelle Öffnen/Schließen war eine
Hauptursache dafür, dass Kameras "plötzlich weg" waren. Jetzt gibt es genau
eine Sitzung pro Untersuchung; die Aufnahmefenster hängen sich nur an.
"""

import datetime
import json
import os
import time

from PyQt5.QtCore import QObject, QTimer, pyqtSignal
from PyQt5.QtGui import QImage

from recorder.dvs_recorder import DVSRecorder
from recorder.video_recorder import VideoRecorder
from utils.devices import find_prophesee_usb_devices, usb_reset, usb_speed_mbit
from utils.storage import check_storage
from utils import alignment as al
import config

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

    def __init__(self, camera=None, save_dir="", audio_enabled=True,
                 external_mic_enabled=False, external_mic_id=None, alignment_store=None,
                 parent=None):
        super().__init__(parent)
        self.camera = camera
        self.save_dir = save_dir
        self.audio_enabled = audio_enabled
        # Ausrichtung (pro Objektiv-Profil) und aktueller Bereich, siehe utils/alignment.py
        self.alignment_store = alignment_store or al.AlignmentStore()
        self.alignment = self.alignment_store.alignment()
        self.roi = None
        self._roi_before_alignment = None
        self._meta = None
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
        self._dvs_restart_deadline = 0.0

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

    def _retire(self, thread, already_stopped=False):
        """Thread stoppen; hängt er, Signale trennen und Referenz behalten."""
        if not already_stopped:
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

    @staticmethod
    def _forward_frame(signal, recorder, image):
        """Vorschaubild an die Fenster weitergeben und dem Recorder melden,
        dass das nächste geschickt werden darf (läuft im GUI-Thread)."""
        try:
            signal.emit(image)
        finally:
            recorder.frame_consumed()

    def _log_status(self):
        """Alle 10 s eine Statuszeile ins Log (für die Fehlersuche)."""
        parts = []
        if self.webcam is not None:
            age = time.monotonic() - self.webcam.last_frame_time
            parts.append(f"Webcam {self.webcam.measured_fps:.0f} fps (letztes Bild vor {age:.1f} s)")
        if self.dvs is not None:
            age = time.monotonic() - self.dvs.last_frame_time
            parts.append(f"DVS {self.dvs.event_rate_mev:.2f} Mev/s, Zustand {self.dvs.state} (letztes Bild vor {age:.1f} s)")
        try:
            with open("/proc/self/statm") as f:
                rss_mb = int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1e6
            parts.append(f"RAM {rss_mb:.0f} MB")
        except (OSError, ValueError):
            pass
        if self._zombies:
            parts.append(f"{len(self._zombies)} hängende Threads")
        print("[Status] " + " | ".join(parts), flush=True)

    def _write_metadata(self, webcam_recorded, stop_results):
        """Schreibt <datei>_meta.json: Zeitstempel, Ausrichtung, Bereich, Zuschnitt."""
        meta, self._meta = self._meta, None
        if not meta:
            return
        w, d = self.webcam, self.dvs
        meta["stop"] = datetime.datetime.now().astimezone().isoformat(timespec="milliseconds")
        meta["app_version"] = config.APP_VERSION
        meta["lens_profile"] = self.profile
        meta["alignment_webcam_rect_norm"] = [round(v, 5) for v in self.alignment]
        meta["common_fov_in_dvs_norm"] = [round(v, 5) for v in al.common_fov(self.alignment)]
        meta["roi_norm_in_common_fov"] = [round(v, 5) for v in (self.roi or al.FULL)]
        meta["region_in_dvs_norm"] = [round(v, 5) for v in al.region_in_dvs(self.alignment, self.roi)]
        if w is not None and webcam_recorded:
            meta["webcam"] = {
                "file": os.path.basename(w.final_output_path),
                "camera": self.camera.name if self.camera else "",
                "source": str(self.camera.source) if self.camera else "",
                "full_resolution": list(w.full_size),
                "crop_rect_norm": [round(v, 5) for v in w.crop_rect],
                "crop_px_xywh": list(w.crop_info() or []),
                "recorded_size": list(w.frame_size),
                "frames": w.frame_count,
                "fps_actual": round(w.actual_fps or 0, 3),
            }
        if d is not None and "dvs" in stop_results:
            meta["dvs"] = {
                "file": os.path.basename(d.raw_path),
                "sensor_size": [d.width, d.height],
                "hardware_roi_px_xywh": list(d.roi_window) if d.roi_window else None,
                "hardware_roi_active": bool(d.roi_active),
            }
        path = os.path.join(meta.pop("save_dir"), f"{meta['file_name']}_meta.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2, ensure_ascii=False)
        except OSError as e:
            self.error.emit(f"Metadaten konnten nicht gespeichert werden: {e}")

    def _on_watchdog(self):
        # DVS meldet "ok", liefert aber keine Bilder mehr -> hängt
        if self.dvs is not None and self.dvs.state == "ok":
            stalled = time.monotonic() - self.dvs.last_frame_time > DVS_STALL_TIMEOUT_S
            if stalled:
                self._set_status(
                    "dvs", "error", "Keine Daten von der DVS-Kamera – bitte „Neu verbinden“"
                )
        self._log_counter = getattr(self, "_log_counter", 0) + 1
        if self._log_counter % 10 == 0:
            self._log_status()
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
            crop_rect=self.webcam_crop(),
        )
        w = self.webcam
        w.update_frame.connect(lambda img, r=w: self._forward_frame(self.webcam_frame, r, img))
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
        d.roi_window = al.dvs_region_window(self.alignment, self.roi)
        d.update_frame.connect(lambda img, r=d: self._forward_frame(self.dvs_frame, r, img))
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
        """
        DVS neu verbinden. Ablauf:
        1. Thread stoppen (hängt er in einem USB-Aufruf, wird er "geparkt").
        2. USB-Port der Kamera zurücksetzen – das beendet auch den hängenden
           Aufruf, und die Firmware startet sauber neu.
        3. Erst wenn kein alter DVS-Thread mehr läuft, neu verbinden.
           (Vorher öffnen kollidiert mit dem alten Zugriff -> Timeouts.)
        """
        if self.recording:
            return False, "Während einer Aufnahme nicht möglich"
        self._set_status("dvs", "connecting", "Wird neu verbunden…")
        if self.dvs is not None:
            old, self.dvs = self.dvs, None
            old.running = False
            if not old.wait(1500):
                self._retire(old, already_stopped=True)
        messages = []
        for dev in find_prophesee_usb_devices():
            ok, msg = usb_reset(dev)
            if not ok:
                messages.append(msg)
        self._dvs_restart_deadline = time.monotonic() + 10.0
        QTimer.singleShot(2500, self._start_dvs_when_free)
        return True, " ".join(messages)

    def _start_dvs_when_free(self):
        busy = [t for t in self._zombies if isinstance(t, DVSRecorder) and t.isRunning()]
        if busy and time.monotonic() < self._dvs_restart_deadline:
            QTimer.singleShot(500, self._start_dvs_when_free)
            return
        if busy:
            print("[Session] Alter DVS-Thread hängt weiter – verbinde trotzdem neu")
        self._zombies = [t for t in self._zombies if t.isRunning()]
        if self.dvs is None:
            self._start_dvs()

    # ------------------------------------------------- Ausrichtung / Bereich

    @property
    def profile(self):
        return self.alignment_store.active_profile

    def webcam_crop(self):
        return al.compose(self.alignment, self.roi)

    def _apply_geometry(self):
        if self.webcam is not None:
            self.webcam.set_crop(self.webcam_crop())
        if self.dvs is not None:
            ok, msg = self.dvs.set_roi(al.dvs_region_window(self.alignment, self.roi))
            if not ok:
                self.error.emit(msg)

    def set_roi(self, rect):
        """Bereich (normiert im DVS-Bildfeld) setzen; None = ganzes Bild."""
        if self.recording:
            return False
        rect = None if rect is None or al.is_full(rect) else al.clamp_rect(rect)
        if rect is not None and min(rect[2], rect[3]) < al.MIN_ROI_SIZE:
            return False
        self.roi = rect
        self._apply_geometry()
        return True

    def set_alignment(self, rect, save=True):
        if self.recording:
            return False
        self.alignment = al.clamp_alignment(rect)
        if save:
            self.alignment_store.set_alignment(self.alignment)
        self._apply_geometry()
        return True

    def set_profile(self, name):
        if self.recording:
            return False
        self.alignment_store.set_active_profile(name)
        self.alignment = self.alignment_store.alignment()
        self._apply_geometry()
        return True

    def begin_alignment(self):
        """Für die Ausrichtungs-Ansicht: volles Bild beider Kameras."""
        self._roi_before_alignment = self.roi
        self.roi = None
        if self.webcam is not None:
            self.webcam.set_crop(al.FULL)
        if self.dvs is not None:
            self.dvs.set_roi(None)
            self.dvs.capture_full = True

    def end_alignment(self):
        if self.dvs is not None:
            self.dvs.capture_full = False
        self.roi = self._roi_before_alignment
        self._apply_geometry()

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

        start_unix = {}
        if self.webcam is not None:
            if webcam_audio is not None:
                self.webcam.has_audio = webcam_audio
            start_unix["webcam"] = time.time()
            results["webcam"] = self.webcam.start_recording(save_dir, file_name)
        else:
            results["webcam"] = (False, "Keine Webcam verbunden")

        if use_dvs:
            if self.dvs is not None and self.status("dvs")[0] in ("ok", "warn"):
                start_unix["dvs"] = time.time()
                results["dvs"] = self.dvs.start_recording(save_dir, file_name)
            else:
                results["dvs"] = (False, self.status("dvs")[1] or "DVS-Kamera nicht bereit")

        self.recording = any(ok for ok, _ in results.values())
        self.recording_with_dvs = bool(results.get("dvs", (False,))[0])
        if self.recording:
            self._meta = {
                "save_dir": save_dir,
                "file_name": file_name,
                "start": datetime.datetime.now().astimezone().isoformat(timespec="milliseconds"),
                "start_unix": start_unix,
                "results": {d: {"ok": ok, "message": m} for d, (ok, m) in results.items()},
            }
        return results

    def stop_recording(self):
        results = {}
        webcam_was_recording = self.webcam is not None and self.webcam.recording
        if webcam_was_recording:
            results["webcam"] = (self.webcam.stop_recording(), "")
        if self.dvs is not None and self.recording_with_dvs:
            results["dvs"] = self.dvs.stop_recording()
        self._write_metadata(webcam_was_recording, results)
        self.recording = False
        self.recording_with_dvs = False
        # Webcam-Audio-Einstellung wiederherstellen (kann für ext. Mikro aus sein)
        if self.webcam is not None:
            self.webcam.has_audio = self.audio_enabled
        return results
