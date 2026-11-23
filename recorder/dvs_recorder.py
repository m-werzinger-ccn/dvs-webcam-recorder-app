#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
DVS-Kamera-Recorder (Prophesee, Metavision SDK).

Aufbau für Stabilität:
- Alle Zugriffe auf das Gerät passieren ausschließlich im Worker-Thread.
  Start/Stopp der Aufnahme werden als Befehl in den Thread gereicht.
- Kann die Kamera nicht geöffnet werden oder bricht die Verbindung ab, wird
  automatisch alle paar Sekunden neu verbunden – kein Umstecken nötig.
- Der Zustand wird über status_changed(state, detail) gemeldet:
  "connecting" | "ok" | "error" | "unavailable".
- Bricht die Verbindung während einer Aufnahme ab, kommt recording_interrupted.
"""

import gc
import os
import queue
import threading
import time

import cv2
import numpy as np
from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtGui import QImage

try:
    from metavision_core.event_io import EventsIterator
    from metavision_core.event_io.raw_reader import initiate_device
    from metavision_sdk_core import PeriodicFrameGenerationAlgorithm, ColorPalette

    HAS_METAVISION = True
    try:
        from metavision_hal import I_ROI
    except ImportError:  # sehr alte SDK-Versionen
        I_ROI = None
except ImportError:
    HAS_METAVISION = False

RETRY_INTERVAL_S = 3.0
PREVIEW_FPS = 25


def _clean_error(e):
    """Mehrzeilige HAL/LibUSB-Meldungen auf eine lesbare Zeile kürzen."""
    lines = [l.strip() for l in str(e).splitlines() if l.strip(" -")]
    text = " ".join(l for l in lines if l != "LibUSB") or e.__class__.__name__
    return text.replace("LibUSB connection error: ", "")


class DVSRecorder(QThread):
    """Thread für Vorschau und RAW-Aufnahme der DVS-Kamera."""

    update_frame = pyqtSignal(QImage)
    status_changed = pyqtSignal(str, str)  # state, detail
    error_signal = pyqtSignal(str)
    recording_interrupted = pyqtSignal(str)

    def __init__(self, serial=""):
        super().__init__()
        self.serial = serial
        self.running = False
        self.recording = False
        self.raw_path = ""
        self.width, self.height = 1280, 720
        self.state = "connecting"
        self.state_detail = ""

        self._commands = queue.Queue()
        # Zeitpunkt des letzten echten Kamerabilds (für die Hänger-Erkennung)
        self.last_frame_time = 0.0
        # Höchstens ein Vorschaubild gleichzeitig unterwegs zur Oberfläche
        self._frame_in_flight = False

        # Hardware-ROI (x, y, w, h in Sensor-Pixeln) oder None = ganzer Sensor.
        # Ereignisse außerhalb verwirft schon der Sensor -> sie fehlen auch in der RAW-Datei.
        self.roi_window = None
        self.roi_active = False  # wurde das ROI vom Gerät tatsächlich übernommen?
        # Für die Ausrichtungs-Ansicht: letztes volles Vorschaubild behalten
        self.capture_full = False
        self.latest_full_frame = None

        self._event_count = 0
        self._rate_t0 = time.monotonic()
        self.event_rate_mev = 0.0

    # ------------------------------------------------------------------ Status

    def _set_state(self, state, detail=""):
        if (state, detail) != (self.state, self.state_detail):
            self.state, self.state_detail = state, detail
            self.status_changed.emit(state, detail)

    def is_ready(self):
        return self.state == "ok"

    # --------------------------------------------------------- Hauptschleife

    def run(self):
        self.running = True

        if not HAS_METAVISION:
            self._set_state(
                "unavailable",
                "Metavision SDK nicht installiert – DVS auf diesem System nicht verfügbar",
            )
            self._idle_loop("DVS nicht verfügbar\n(Metavision SDK fehlt)")
            return

        attempt = 0
        while self.running:
            attempt += 1
            self._set_state(
                "connecting", "Verbinde…" if attempt == 1 else f"Verbinde… (Versuch {attempt})"
            )
            device = iterator = None
            try:
                device = initiate_device(self.serial)
                if device is None:
                    raise RuntimeError("Keine DVS-Kamera gefunden")
                iterator = EventsIterator.from_device(device=device)
                self.height, self.width = iterator.get_size()
                attempt = 0
                self._stream(device, iterator)
            except Exception as e:
                msg = _clean_error(e)
                print(f"[DVS] {msg}")
                self._set_state("error", f"{msg} – neuer Versuch in {RETRY_INTERVAL_S:.0f} s")
            finally:
                self._stop_logging(device, interrupted=self.running)
                # Gerät im selben Thread freigeben, bevor neu geöffnet wird
                del iterator
                del device
                gc.collect()

            if self.running:
                self._send_placeholder("DVS-Kamera nicht verbunden\nVerbinde automatisch neu…")
                self._sleep(RETRY_INTERVAL_S)

        self._set_state("connecting", "gestoppt")

    def _stream(self, device, iterator):
        frame_gen = PeriodicFrameGenerationAlgorithm(
            sensor_width=self.width,
            sensor_height=self.height,
            fps=PREVIEW_FPS,
            palette=ColorPalette.Dark,
        )
        frame_gen.set_output_callback(self._on_frame)
        self.last_frame_time = time.monotonic()
        self._set_state("ok", f"{self.width}×{self.height}")
        print(f"[DVS] verbunden: {self.width}x{self.height}")
        self._apply_roi(device)

        for evs in iterator:
            if not self.running:
                return
            self._process_commands(device)
            self._count_events(len(evs))
            frame_gen.process_events(evs)

        # Iterator ist von selbst zu Ende gegangen -> Kamera weg
        raise RuntimeError("Verbindung zur DVS-Kamera verloren")

    def _count_events(self, n):
        self._event_count += n
        now = time.monotonic()
        if now - self._rate_t0 >= 1.0:
            self.event_rate_mev = self._event_count / (now - self._rate_t0) / 1e6
            self._event_count, self._rate_t0 = 0, now
            detail = f"{self.width}×{self.height} · {self.event_rate_mev:.2f} Mev/s"
            if self.roi_active and self.roi_window:
                detail += f" · Bereich {self.roi_window[2]}×{self.roi_window[3]}"
            if self.recording:
                detail += " · REC"
            self._set_state("ok", detail)

    def _on_frame(self, ts, cd_frame):
        if not self.running:
            return
        self.last_frame_time = time.monotonic()
        if self.capture_full:
            self.latest_full_frame = cd_frame.copy()  # Puffer wird vom SDK wiederverwendet
        if self._frame_in_flight:
            return  # GUI ist noch beim letzten Bild -> dieses verwerfen
        win = self.roi_window
        if win and not self.capture_full:
            x, y, w, h = win
            cd_frame = cd_frame[y : y + h, x : x + w]
        self._emit_rgb(cv2.cvtColor(cd_frame, cv2.COLOR_BGR2RGB))

    def frame_consumed(self):
        """Wird von der GUI aufgerufen, sobald ein Vorschaubild verarbeitet ist."""
        self._frame_in_flight = False

    def _emit_rgb(self, rgb):
        if self._frame_in_flight:
            return
        self._frame_in_flight = True
        rgb = np.ascontiguousarray(rgb)
        h, w = rgb.shape[:2]
        self.update_frame.emit(QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy())

    def _send_placeholder(self, text):
        frame = np.full((360, 640, 3), 40, dtype=np.uint8)
        for i, line in enumerate(text.split("\n")):
            cv2.putText(frame, line, (40, 170 + 40 * i), cv2.FONT_HERSHEY_SIMPLEX,
                        0.8, (230, 230, 230), 2)
        self._emit_rgb(frame)

    def _idle_loop(self, text):
        while self.running:
            self._fail_pending_commands("DVS nicht verfügbar")
            self._send_placeholder(text)
            self._sleep(1.0)

    def _sleep(self, seconds):
        end = time.monotonic() + seconds
        while self.running and time.monotonic() < end:
            self._fail_pending_commands("DVS-Kamera nicht verbunden")
            time.sleep(0.05)

    # ------------------------------------------------------ Aufnahme-Befehle

    def _process_commands(self, device):
        while True:
            try:
                cmd, arg, reply = self._commands.get_nowait()
            except queue.Empty:
                return
            ok, msg = False, ""
            try:
                stream = device.get_i_events_stream()
                if cmd == "start":
                    os.makedirs(os.path.dirname(arg), exist_ok=True)
                    stream.log_raw_data(arg)
                    self.raw_path, self.recording, ok = arg, True, True
                    print(f"[DVS] Aufnahme gestartet: {arg}")
                elif cmd == "roi":
                    self.roi_window = arg
                    ok = self._apply_roi(device)
                    if not ok:
                        msg = "Diese DVS-Kamera unterstützt kein Hardware-ROI"
                elif cmd == "stop":
                    if self.recording:
                        stream.stop_log_raw_data()
                        print(f"[DVS] Aufnahme gestoppt: {self.raw_path}")
                    self.recording, ok = False, True
            except Exception as e:
                msg = str(e)
                print(f"[DVS] Befehl '{cmd}' fehlgeschlagen: {e}")
            reply.put((ok, msg))

    def _apply_roi(self, device):
        """Setzt das Hardware-ROI am Gerät (läuft im Worker-Thread)."""
        try:
            roi = device.get_i_roi()
        except AttributeError:
            roi = None
        if roi is None or I_ROI is None:
            self.roi_active = False
            if self.roi_window:
                print("[DVS] Hardware-ROI wird nicht unterstützt – nehme den ganzen Sensor auf")
            return self.roi_window is None
        if self.roi_window is None:
            roi.enable(False)
            self.roi_active = False
            print("[DVS] Hardware-ROI aus (ganzer Sensor)")
            return True
        x, y, w, h = self.roi_window
        try:
            roi.set_mode(I_ROI.Mode.ROI)
        except Exception:
            pass  # ältere SDKs kennen set_mode nicht, ROI ist dort Standard
        roi.set_window(I_ROI.Window(int(x), int(y), int(w), int(h)))
        roi.enable(True)
        self.roi_active = True
        print(f"[DVS] Hardware-ROI: x={x} y={y} {w}×{h}")
        return True

    def set_roi(self, window):
        """Hardware-ROI setzen (x, y, w, h) oder None. Nicht während einer Aufnahme."""
        if self.recording:
            return False, "Während einer Aufnahme nicht möglich"
        window = tuple(int(v) for v in window) if window else None
        if self.state != "ok" or not self.isRunning():
            self.roi_window = window  # wird beim nächsten Verbinden gesetzt
            return True, ""
        return self._send_command("roi", window)

    def _fail_pending_commands(self, msg):
        while True:
            try:
                cmd, arg, reply = self._commands.get_nowait()
            except queue.Empty:
                return
            if cmd == "roi":
                self.roi_window = arg  # wird beim nächsten Verbinden gesetzt
                reply.put((True, ""))
            elif cmd == "stop":
                self.recording = False
                reply.put((True, ""))
            else:
                reply.put((False, msg))

    def _stop_logging(self, device, interrupted):
        if not self.recording:
            return
        try:
            if device is not None:
                device.get_i_events_stream().stop_log_raw_data()
        except Exception:
            pass
        self.recording = False
        if interrupted:
            msg = f"DVS-Verbindung während der Aufnahme verloren – Datei unvollständig: {os.path.basename(self.raw_path)}"
            self.recording_interrupted.emit(msg)
            self.error_signal.emit(msg)

    def _send_command(self, cmd, arg=None, timeout=5.0):
        if not self.running or not self.isRunning():
            return False, "DVS-Thread läuft nicht"
        reply = queue.Queue()
        self._commands.put((cmd, arg, reply))
        try:
            return reply.get(timeout=timeout)
        except queue.Empty:
            return False, "DVS-Kamera reagiert nicht"

    def start_recording(self, save_dir, file_name):
        """Startet die RAW-Aufnahme. Gibt (erfolg, meldung) zurück."""
        if not self.is_ready():
            return False, self.state_detail or "DVS-Kamera nicht bereit"
        path = os.path.join(save_dir, f"{file_name}.raw")
        return self._send_command("start", path)

    def stop_recording(self):
        if not self.recording:
            return True, ""
        return self._send_command("stop")

    # ---------------------------------------------------------------- Ende

    def stop(self, timeout_ms=5000):
        """Stoppt den Thread; das Gerät wird im Thread selbst freigegeben."""
        if self.recording:
            self.stop_recording()
        self.running = False
        if not self.wait(timeout_ms):
            print("[DVS] Warnung: Thread hat sich nicht rechtzeitig beendet")
