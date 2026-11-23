#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Webcam-Recorder mit digitalem Zoom, Audio und automatischer Wiederverbindung.

Stabilität:
- MJPG wird angefordert (unkomprimiertes 720p30 passt nicht durch USB 2.0
  und blockiert sonst Bandbreite am Hub).
- Geöffnet wird über den stabilen Pfad /dev/v4l/by-id/... (Linux).
- Fällt die Kamera aus, wird automatisch neu verbunden; der Zustand wird über
  status_changed(state, detail) gemeldet.
"""

import os
import cv2
import time
import datetime
import threading
import numpy as np
import wave
import subprocess
import tempfile
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QObject
from PyQt5.QtGui import QImage
import config
from utils.devices import opencv_backend

# PyAudio optional importieren
try:
    import pyaudio
    PYAUDIO_AVAILABLE = True
except ImportError:
    PYAUDIO_AVAILABLE = False
    print("PyAudio nicht verfügbar - Audio-Aufnahme deaktiviert")


class AudioRecorder(QThread):
    """Thread für die Audioaufnahme mit präzisem Timing"""

    def __init__(self, audio_file_path, channels=1, rate=44100, chunk=1024):
        super().__init__()
        self.audio_file_path = audio_file_path
        self.channels = channels
        self.rate = rate
        self.chunk = chunk
        self.recording = False
        self.frames = []
        self.start_time = None

        self.pyaudio = None
        self.stream = None

    def set_start_time(self, start_time):
        """Setzt den gemeinsamen Startzeitpunkt"""
        self.start_time = start_time

    def run(self):
        """Startet die Audioaufnahme im eigenen Thread"""
        if not PYAUDIO_AVAILABLE:
            print("PyAudio nicht verfügbar - Audio-Aufnahme übersprungen")
            return

        try:
            self.pyaudio = pyaudio.PyAudio()

            self.stream = self.pyaudio.open(
                format=pyaudio.paInt16,
                channels=self.channels,
                rate=self.rate,
                input=True,
                frames_per_buffer=self.chunk,
            )

            while self.start_time is None and not self.recording:
                time.sleep(0.001)

            self.recording = True
            print("Audioaufnahme gestartet...")
            self.frames = []

            while self.recording:
                try:
                    data = self.stream.read(self.chunk, exception_on_overflow=False)
                    self.frames.append(data)
                except Exception as e:
                    print(f"Fehler beim Audio-Frame-Lesen: {e}")

            self.save_audio()

        except Exception as e:
            print(f"Fehler bei der Audioaufnahme: {e}")
        finally:
            self.cleanup()

    def stop(self):
        """Stoppt die Audioaufnahme"""
        self.recording = False
        self.wait()

    def save_audio(self):
        """Speichert die aufgenommenen Audiodaten"""
        if not PYAUDIO_AVAILABLE or self.pyaudio is None:
            return

        try:
            os.makedirs(os.path.dirname(self.audio_file_path), exist_ok=True)

            wf = wave.open(self.audio_file_path, "wb")
            wf.setnchannels(self.channels)
            wf.setsampwidth(self.pyaudio.get_sample_size(pyaudio.paInt16))
            wf.setframerate(self.rate)
            wf.writeframes(b"".join(self.frames))
            wf.close()
            print(f"Audiodatei gespeichert: {self.audio_file_path}")
        except Exception as e:
            print(f"Fehler beim Speichern der Audiodatei: {e}")

    def cleanup(self):
        """Gibt alle Ressourcen frei"""
        try:
            if self.stream is not None:
                self.stream.stop_stream()
                self.stream.close()
                self.stream = None

            if self.pyaudio is not None:
                self.pyaudio.terminate()
                self.pyaudio = None
        except Exception as e:
            print(f"Fehler beim Freigeben der Audio-Ressourcen: {e}")


class ExternalAudioRecorder(QThread):
    """Thread für die Audioaufnahme über ein externes USB-Mikrofon"""

    recording_finished = pyqtSignal(bool, str)  # Erfolg, Pfad/Fehlermeldung

    def __init__(self, audio_file_path, device_index, channels=None, rate=None, chunk=1024):
        super().__init__()
        self.audio_file_path = audio_file_path
        self.device_index = device_index  # Spezifisches USB-Mikrofon
        self.channels = channels  # None = automatisch vom Gerät ermitteln
        self.rate = rate  # None = automatisch vom Gerät ermitteln
        self.chunk = chunk
        self.recording = False
        self.frames = []
        self.start_time = None

        self.pyaudio = None
        self.stream = None

    def set_start_time(self, start_time):
        """Setzt den gemeinsamen Startzeitpunkt für Synchronisation"""
        self.start_time = start_time

    def run(self):
        """Startet die Audioaufnahme im eigenen Thread"""
        if not PYAUDIO_AVAILABLE:
            print("PyAudio nicht verfügbar - Externe Audio-Aufnahme übersprungen")
            self.recording_finished.emit(False, "PyAudio nicht installiert")
            return

        try:
            self.pyaudio = pyaudio.PyAudio()

            # Geräteinformationen abrufen und native Einstellungen verwenden
            try:
                device_info = self.pyaudio.get_device_info_by_index(self.device_index)
                device_name = device_info.get('name', 'Unbekannt')

                # Kanäle vom Gerät übernehmen wenn nicht explizit angegeben
                if self.channels is None:
                    self.channels = int(device_info.get('maxInputChannels', 2))

                if self.rate is None:
                    self.rate = 44100  # Startwert; die tatsächliche Rate wird unten ermittelt

                print(f"Externes Mikrofon: {device_name}")
                print(f"  Kanäle: {self.channels}, Sample-Rate: {self.rate} Hz")

            except Exception as e:
                print(f"Fehler beim Abrufen der Geräteinformationen: {e}")
                self.recording_finished.emit(False, f"Mikrofon nicht verfügbar: {e}")
                return

            # Stream mit spezifischem Gerät und korrekten Parametern öffnen
            # Versuche zuerst höhere Raten (Yeti unterstützt bis 96 kHz)
            rates_to_try = [96000, 48000, 44100, 32000, 22050, 16000]
            stream_opened = False

            for rate in rates_to_try:
                try:
                    self.stream = self.pyaudio.open(
                        format=pyaudio.paInt16,
                        channels=self.channels,
                        rate=rate,
                        input=True,
                        input_device_index=self.device_index,
                        frames_per_buffer=self.chunk,
                    )
                    self.rate = rate  # Tatsächlich verwendete Rate speichern
                    stream_opened = True
                    print(f"  Stream geöffnet mit {rate} Hz")
                    break
                except Exception as e:
                    print(f"  Rate {rate} Hz fehlgeschlagen")
                    continue

            if not stream_opened:
                raise Exception("Keine unterstützte Sample-Rate gefunden")

            # Warten auf start_time (Synchronisation mit Video)
            while self.start_time is None and not self.recording:
                time.sleep(0.001)

            self.recording = True
            print(f"Externe Audioaufnahme gestartet (Gerät {self.device_index})...")
            self.frames = []

            while self.recording:
                try:
                    data = self.stream.read(self.chunk, exception_on_overflow=False)
                    self.frames.append(data)
                except Exception as e:
                    print(f"Fehler beim Audio-Frame-Lesen (extern): {e}")

            self.save_audio()

        except Exception as e:
            print(f"Fehler bei der externen Audioaufnahme: {e}")
            self.recording_finished.emit(False, str(e))
        finally:
            self.cleanup()

    def stop(self):
        """Stoppt die Audioaufnahme"""
        self.recording = False
        self.wait()

    def save_audio(self):
        """Speichert die aufgenommenen Audiodaten als WAV"""
        if not PYAUDIO_AVAILABLE or self.pyaudio is None:
            return

        try:
            os.makedirs(os.path.dirname(self.audio_file_path), exist_ok=True)

            # Diagnose: Berechne die tatsächliche Dauer
            total_bytes = sum(len(f) for f in self.frames)
            bytes_per_sample = 2  # 16-bit = 2 bytes
            total_samples = total_bytes // (bytes_per_sample * self.channels)
            calculated_duration = total_samples / self.rate
            print(f"  Audio-Diagnose:")
            print(f"    Frames: {len(self.frames)}, Total Bytes: {total_bytes}")
            print(f"    Kanäle: {self.channels}, Rate: {self.rate} Hz")
            print(f"    Samples: {total_samples}, Berechnete Dauer: {calculated_duration:.2f}s")

            wf = wave.open(self.audio_file_path, "wb")
            wf.setnchannels(self.channels)
            wf.setsampwidth(self.pyaudio.get_sample_size(pyaudio.paInt16))
            wf.setframerate(self.rate)
            wf.writeframes(b"".join(self.frames))
            wf.close()

            print(f"Externe Audiodatei gespeichert: {self.audio_file_path}")
            self.recording_finished.emit(True, self.audio_file_path)

        except Exception as e:
            print(f"Fehler beim Speichern der externen Audiodatei: {e}")
            self.recording_finished.emit(False, str(e))

    def cleanup(self):
        """Gibt alle Ressourcen frei"""
        try:
            if self.stream is not None:
                self.stream.stop_stream()
                self.stream.close()
                self.stream = None

            if self.pyaudio is not None:
                self.pyaudio.terminate()
                self.pyaudio = None
        except Exception as e:
            print(f"Fehler beim Freigeben der externen Audio-Ressourcen: {e}")


class VideoProcessingWorker(QObject):
    """Worker-Klasse für die Video-/Audio-Verarbeitung"""

    processing_finished = pyqtSignal(bool, str)
    processing_progress = pyqtSignal(str)
    processing_completed = pyqtSignal()

    def __init__(
        self,
        temp_video_path,
        temp_audio_path,
        final_output_path,
        has_audio,
        actual_fps=None,
    ):
        super().__init__()
        self.temp_video_path = temp_video_path
        self.temp_audio_path = temp_audio_path
        self.final_output_path = final_output_path
        self.has_audio = has_audio
        self.actual_fps = actual_fps

    def process_video(self):
        """Verarbeitet Video und Audio in einem separaten Thread"""
        try:
            self.processing_progress.emit("Verarbeite Video und Audio...")

            if (
                self.has_audio
                and os.path.exists(self.temp_video_path)
                and os.path.exists(self.temp_audio_path)
            ):
                success = self.combine_video_audio()
                if success:
                    self.processing_progress.emit(
                        "Video mit Audio erfolgreich kombiniert"
                    )
                    self.processing_finished.emit(
                        True,
                        f"Video mit Audio erfolgreich kombiniert: {self.final_output_path}",
                    )
                    self.processing_completed.emit()
                else:
                    self.processing_progress.emit(
                        "Fehler beim Kombinieren - kopiere nur Video"
                    )
                    self.convert_video_only()
                    self.processing_finished.emit(
                        True, f"Nur Video konvertiert: {self.final_output_path}"
                    )
                    self.processing_completed.emit()
            else:
                if os.path.exists(self.temp_video_path):
                    self.processing_progress.emit("Konvertiere Video...")
                    self.convert_video_only()
                    self.processing_finished.emit(
                        True, f"Video konvertiert: {self.final_output_path}"
                    )
                    self.processing_completed.emit()
                else:
                    self.processing_finished.emit(False, "Keine Videodatei gefunden")

            self.cleanup_temp_files()

        except Exception as e:
            self.processing_finished.emit(False, f"Fehler bei der Verarbeitung: {e}")
            import traceback

            traceback.print_exc()

    def convert_video_only(self):
        """Konvertiert nur das Video ohne Audio"""
        try:
            try:
                subprocess.run(
                    ["ffmpeg", "-version"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=True,
                )
            except:
                print("FFmpeg nicht gefunden. Versuche direkte Kopie...")
                import shutil

                shutil.copy(self.temp_video_path, self.final_output_path)
                return

            fps = self.actual_fps if self.actual_fps else config.DEFAULT_FPS

            codecs_to_try = ["libx264", "mp4v", "h264"]

            for codec in codecs_to_try:
                try:
                    print(f"Versuche Konvertierung mit Codec {codec}, FPS: {fps}...")
                    cmd = [
                        "ffmpeg",
                        "-r",
                        str(fps),
                        "-i",
                        self.temp_video_path,
                        "-c:v",
                        codec,
                        "-r",
                        str(fps),
                        "-vf",
                        "format=yuv420p",
                        "-y",
                        self.final_output_path,
                    ]

                    process = subprocess.run(
                        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
                    )

                    if (
                        process.returncode == 0
                        and os.path.exists(self.final_output_path)
                        and os.path.getsize(self.final_output_path) > 0
                    ):
                        print(f"Video erfolgreich mit {codec} konvertiert")
                        return
                except Exception as e:
                    print(f"Fehler bei der Konvertierung mit {codec}: {e}")

            # Fallback
            try:
                import shutil

                self.final_output_path = os.path.join(
                    os.path.dirname(self.final_output_path),
                    os.path.splitext(os.path.basename(self.final_output_path))[0]
                    + ".avi",
                )
                shutil.copy(self.temp_video_path, self.final_output_path)
            except Exception as e:
                print(f"Fehler beim Kopieren: {e}")

        except Exception as e:
            print(f"Fehler bei der Video-Konvertierung: {e}")

    def combine_video_audio(self):
        """Kombiniert Video und Audio mit verbesserter Synchronisation"""
        try:
            try:
                subprocess.run(
                    ["ffmpeg", "-version"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=True,
                )
            except:
                print("FFmpeg nicht gefunden")
                return False

            fps = self.actual_fps if self.actual_fps else config.DEFAULT_FPS

            configs = [
                [
                    "ffmpeg",
                    "-r",
                    str(fps),
                    "-i",
                    self.temp_video_path,
                    "-i",
                    self.temp_audio_path,
                    "-c:v",
                    "libx264",
                    "-r",
                    str(fps),
                    "-vf",
                    "format=yuv420p",
                    "-c:a",
                    "aac",
                    "-strict",
                    "experimental",
                    "-async",
                    "1",
                    "-vsync",
                    "cfr",
                    "-y",
                    self.final_output_path,
                ],
                [
                    "ffmpeg",
                    "-r",
                    str(fps),
                    "-i",
                    self.temp_video_path,
                    "-i",
                    self.temp_audio_path,
                    "-c:v",
                    "libx264",
                    "-r",
                    str(fps),
                    "-vf",
                    "format=yuv420p,setpts=PTS-STARTPTS",
                    "-c:a",
                    "aac",
                    "-af",
                    "asetpts=PTS-STARTPTS",
                    "-strict",
                    "experimental",
                    "-y",
                    self.final_output_path,
                ],
            ]

            for i, cmd in enumerate(configs):
                print(f"Versuche FFmpeg-Methode {i+1}...")
                process = subprocess.run(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )

                if (
                    process.returncode == 0
                    and os.path.exists(self.final_output_path)
                    and os.path.getsize(self.final_output_path) > 0
                ):
                    print(f"Video und Audio erfolgreich kombiniert (Methode {i+1})")
                    return True
                else:
                    error_msg = process.stderr.decode()
                    print(f"FFmpeg-Fehler (Methode {i+1}): {error_msg}")

            return False

        except Exception as e:
            print(f"Fehler beim Kombinieren von Video und Audio: {e}")
            return False

    def cleanup_temp_files(self):
        """Löscht temporäre Dateien"""
        try:
            if os.path.exists(self.temp_video_path):
                os.remove(self.temp_video_path)
                print(f"Temporäre Videodatei gelöscht")

            if os.path.exists(self.temp_audio_path):
                os.remove(self.temp_audio_path)
                print(f"Temporäre Audiodatei gelöscht")
        except Exception as e:
            print(f"Fehler beim Löschen der temporären Dateien: {e}")


RECONNECT_INTERVAL_S = 3.0
FRAME_TIMEOUT_S = 2.0  # ohne Bild so lange -> Kamera gilt als ausgefallen


class VideoRecorder(QThread):
    """Thread für Vorschau und Aufnahme der Webcam."""

    update_frame = pyqtSignal(QImage)
    error_signal = pyqtSignal(str)
    status_changed = pyqtSignal(str, str)  # state, detail
    processing_status = pyqtSignal(str)
    video_processing_completed = pyqtSignal()
    zoom_changed = pyqtSignal(float)
    recording_interrupted = pyqtSignal(str)

    def __init__(self, source=0, save_dir="", file_name="", audio_enabled=True,
                 zoom_factor=1.5, camera_name=""):
        super().__init__()
        self.source = source
        self.camera_name = camera_name or str(source)
        self._pending_source = None
        self.save_dir = save_dir
        self.file_name = file_name
        self.running = False
        self.recording = False
        self.video_writer = None
        self.cap = None

        # Zustand
        self.state = "connecting"
        self.state_detail = ""
        self.last_frame_time = 0.0
        self._last_open_attempt = 0.0
        self._interrupt_reported = False
        self.frame_size = (1280, 720)
        self.measured_fps = 0.0
        self._fps_count = 0
        self._fps_t0 = time.monotonic()

        # Audio
        self.audio_recorder = None
        self.has_audio = audio_enabled
        self.temp_video_path = ""
        self.temp_audio_path = ""
        self.final_output_path = ""

        # Timing
        self.start_time = None
        self.frame_count = 0
        self.target_fps = config.DEFAULT_FPS
        self.actual_fps = None

        # Zoom
        self.zoom_factor = max(1.0, min(3.0, zoom_factor))
        self.zoom_enabled = True

        self.mutex = threading.Lock()

        self.processing_thread = None
        self.processing_worker = None

        # Höchstens ein Vorschaubild gleichzeitig unterwegs zur Oberfläche.
        # Kommt die GUI nicht hinterher, werden Vorschaubilder verworfen statt
        # sich im Speicher zu stauen (die Aufnahme selbst ist davon unberührt).
        self._frame_in_flight = False

    def frame_consumed(self):
        """Wird von der GUI aufgerufen, sobald ein Vorschaubild verarbeitet ist."""
        self._frame_in_flight = False

    def _emit_preview(self, rgb):
        if self._frame_in_flight:
            return
        self._frame_in_flight = True
        h, w = rgb.shape[:2]
        # .copy(): das QImage darf nicht auf den numpy-Puffer zeigen
        self.update_frame.emit(QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy())

    # ----------------------------------------------------------------- Zoom

    def set_zoom(self, zoom_factor):
        with self.mutex:
            self.zoom_factor = max(1.0, min(3.0, zoom_factor))
        self.zoom_changed.emit(self.zoom_factor)

    def set_zoom_enabled(self, enabled):
        with self.mutex:
            self.zoom_enabled = enabled

    def apply_zoom(self, frame):
        if not self.zoom_enabled or self.zoom_factor <= 1.0:
            return frame
        h, w = frame.shape[:2]
        new_w, new_h = int(w / self.zoom_factor), int(h / self.zoom_factor)
        x, y = (w - new_w) // 2, (h - new_h) // 2
        cropped = frame[y : y + new_h, x : x + new_w]
        return cv2.resize(cropped, (w, h), interpolation=cv2.INTER_LINEAR)

    # --------------------------------------------------------------- Status

    def _set_state(self, state, detail=""):
        if (state, detail) != (self.state, self.state_detail):
            self.state, self.state_detail = state, detail
            self.status_changed.emit(state, detail)

    def is_ready(self):
        return (
            self.cap is not None
            and time.monotonic() - self.last_frame_time < FRAME_TIMEOUT_S
        )

    # ------------------------------------------------------------- Kamera

    def set_source(self, source, camera_name=""):
        """Wechselt die Kamera (nicht während einer Aufnahme)."""
        if self.recording:
            return False
        self.camera_name = camera_name or str(source)
        self._pending_source = source
        return True

    def open_camera(self):
        self._release_cap()
        self._last_open_attempt = time.monotonic()
        self._set_state("connecting", f"Verbinde mit {self.camera_name}…")

        cap = cv2.VideoCapture(self.source, opencv_backend())
        if not cap.isOpened():
            cap.release()
            self._set_state(
                "error",
                f"Webcam nicht gefunden – neuer Versuch in {RECONNECT_INTERVAL_S:.0f} s",
            )
            return False

        # MJPG zuerst setzen, dann Auflösung – sonst wählt der Treiber
        # unkomprimiertes YUYV mit zu hoher USB-Bandbreite.
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        cap.set(cv2.CAP_PROP_FPS, self.target_fps)
        if hasattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC"):
            # Ohne Timeout blockiert read() bei abgezogener Kamera bis zu 10 s
            cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, int(FRAME_TIMEOUT_S * 1000))

        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
        fourcc_str = "".join(chr((fourcc >> 8 * i) & 0xFF) for i in range(4)).strip()
        print(f"[Webcam] {self.camera_name}: {w}x{h} @ {cap.get(cv2.CAP_PROP_FPS):.0f} fps, Format {fourcc_str or '?'}")

        self.cap = cap
        self._interrupt_reported = False
        self._set_state("connecting", "Warte auf erstes Bild…")
        return True

    # Kompatibilität mit älterem Code
    reopen_camera = open_camera

    def _release_cap(self):
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None

    # ------------------------------------------------------- Hauptschleife

    def run(self):
        self.running = True
        try:
            self.open_camera()
            while self.running:
                if self._pending_source is not None:
                    self.source, self._pending_source = self._pending_source, None
                    self.open_camera()

                ok, frame = (False, None)
                if self.cap is not None:
                    try:
                        ok, frame = self.cap.read()
                    except cv2.error as e:
                        print(f"[Webcam] read-Fehler: {e}")
                        ok = False

                if not ok or frame is None:
                    self._handle_missing_frame()
                    continue

                self.last_frame_time = time.monotonic()
                frame = self.apply_zoom(frame)
                self.frame_size = (frame.shape[1], frame.shape[0])

                with self.mutex:
                    if self.recording and self.video_writer is not None:
                        self.video_writer.write(frame)
                        self.frame_count += 1

                self._update_fps()
                if not self._frame_in_flight:
                    self._emit_preview(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        except Exception as e:
            self.error_signal.emit(f"Kritischer Webcam-Fehler: {e}")
            print(f"[Webcam] Kritischer Fehler: {e}")
        finally:
            self.cleanup_resources()

    def _handle_missing_frame(self):
        since_last = time.monotonic() - self.last_frame_time
        if self.cap is not None and since_last < FRAME_TIMEOUT_S:
            time.sleep(0.01)  # kurzer Aussetzer, Kamera noch nicht aufgeben
            return

        if self.recording and not self._interrupt_reported:
            self._interrupt_reported = True
            msg = "Webcam liefert während der Aufnahme keine Bilder mehr!"
            self.recording_interrupted.emit(msg)
            self.error_signal.emit(msg)

        self._emit_placeholder()
        if time.monotonic() - self._last_open_attempt >= RECONNECT_INTERVAL_S:
            if self.cap is not None:
                self._set_state("error", "Keine Bilder von der Webcam – verbinde neu…")
            self.open_camera()
        else:
            time.sleep(0.2)

    def _update_fps(self):
        self._fps_count += 1
        now = time.monotonic()
        if now - self._fps_t0 >= 1.0:
            self.measured_fps = self._fps_count / (now - self._fps_t0)
            self._fps_count, self._fps_t0 = 0, now
            w, h = self.frame_size
            detail = f"{w}×{h} · {self.measured_fps:.0f} fps"
            if self.recording:
                detail += " · REC"
            self._set_state("ok", detail)

    def _emit_placeholder(self):
        frame = np.full((360, 640, 3), 40, dtype=np.uint8)
        for i, line in enumerate(("Webcam nicht verbunden", "Verbinde automatisch neu...")):
            cv2.putText(frame, line, (40, 170 + 40 * i), cv2.FONT_HERSHEY_SIMPLEX,
                        0.8, (230, 230, 230), 2)
        self._emit_preview(frame)

    # ------------------------------------------------------------ Aufnahme

    def start_recording(self, save_dir=None, file_name=None):
        """Startet die Aufnahme. Gibt (erfolg, meldung) zurück."""
        if not self.running:
            return False, "Webcam-Thread läuft nicht"
        with self.mutex:
            if self.recording:
                return False, "Aufnahme läuft bereits"
        if save_dir:
            self.save_dir = save_dir
        if file_name:
            self.file_name = file_name
        if not self.save_dir or not self.file_name:
            return False, "Kein Speicherort oder Dateiname angegeben"
        if not self.is_ready():
            return False, "Webcam liefert keine Bilder"

        try:
            os.makedirs(self.save_dir, exist_ok=True)
            temp_dir = tempfile.gettempdir()
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            self.temp_video_path = os.path.join(temp_dir, f"temp_video_{timestamp}.avi")
            self.temp_audio_path = os.path.join(temp_dir, f"temp_audio_{timestamp}.wav")
            self.final_output_path = os.path.join(
                self.save_dir, f"{self.file_name}{config.DEFAULT_VIDEO_EXTENSION}"
            )

            with self.mutex:
                self.video_writer = cv2.VideoWriter(
                    self.temp_video_path,
                    cv2.VideoWriter_fourcc(*"MJPG"),
                    self.target_fps,
                    self.frame_size,
                )
                if not self.video_writer.isOpened():
                    self.video_writer = None
                    return False, "Video-Datei konnte nicht angelegt werden"

                self.start_time = time.perf_counter()
                self.frame_count = 0
                self._interrupt_reported = False

                if self.has_audio:
                    self.audio_recorder = AudioRecorder(self.temp_audio_path)
                    self.audio_recorder.set_start_time(self.start_time)
                    self.audio_recorder.start()

                self.recording = True
            print(f"[Webcam] Aufnahme gestartet ({self.frame_size[0]}x{self.frame_size[1]}, Zoom {self.zoom_factor:.2f}x)")
            return True, ""
        except Exception as e:
            with self.mutex:
                if self.video_writer is not None:
                    self.video_writer.release()
                    self.video_writer = None
            if self.audio_recorder is not None:
                self.audio_recorder.stop()
                self.audio_recorder = None
            return False, str(e)

    def stop_recording(self):
        """Stoppt die Aufnahme und startet die Nachverarbeitung (ffmpeg)."""
        try:
            with self.mutex:
                if not self.recording:
                    return False
                self.recording = False

                if self.start_time and self.frame_count > 0:
                    duration = time.perf_counter() - self.start_time
                    self.actual_fps = self.frame_count / duration
                    print(f"[Webcam] {self.frame_count} Frames in {duration:.2f}s = {self.actual_fps:.2f} FPS")
                else:
                    self.actual_fps = self.target_fps

                if self.video_writer is not None:
                    self.video_writer.release()
                    self.video_writer = None

            if self.audio_recorder is not None:
                self.audio_recorder.stop()
                self.audio_recorder = None

            self.start_video_processing()
            return True
        except Exception as e:
            self.error_signal.emit(f"Fehler beim Stoppen: {e}")
            self.recording = False
            return False

    def start_video_processing(self):
        try:
            self.processing_worker = VideoProcessingWorker(
                self.temp_video_path,
                self.temp_audio_path,
                self.final_output_path,
                self.has_audio,
                self.actual_fps,
            )
            self.processing_thread = QThread()
            self.processing_worker.moveToThread(self.processing_thread)
            self.processing_worker.processing_progress.connect(self.processing_status.emit)
            self.processing_worker.processing_finished.connect(self.on_processing_finished)
            self.processing_worker.processing_completed.connect(
                self.video_processing_completed.emit
            )
            self.processing_thread.started.connect(self.processing_worker.process_video)
            self.processing_thread.start()
        except Exception as e:
            print(f"Fehler beim Starten der Verarbeitung: {e}")
            self.error_signal.emit(f"Fehler: {e}")

    def on_processing_finished(self, success, message):
        try:
            if success:
                print(f"Verarbeitung erfolgreich: {message}")
                self.processing_status.emit("Video erfolgreich gespeichert")
            else:
                print(f"Verarbeitung fehlgeschlagen: {message}")
                self.error_signal.emit(f"Verarbeitung fehlgeschlagen: {message}")
            if self.processing_thread:
                self.processing_thread.quit()
                self.processing_thread.wait()
                self.processing_thread = None
            self.processing_worker = None
        except Exception as e:
            print(f"Fehler beim Abschließen: {e}")

    def set_audio_enabled(self, enabled):
        self.has_audio = enabled

    # ---------------------------------------------------------------- Ende

    def stop(self, timeout_ms=5000):
        if self.recording:
            self.stop_recording()
        self.running = False
        if not self.wait(timeout_ms):
            print("[Webcam] Warnung: Thread hat sich nicht rechtzeitig beendet")
        # Laufende ffmpeg-Nachverarbeitung abwarten, damit kein Video verloren geht
        if self.processing_thread is not None:
            self.processing_thread.quit()
            self.processing_thread.wait()
            self.processing_thread = None

    def cleanup_resources(self):
        """Gibt Kamera und Writer frei (läuft im Worker-Thread)."""
        try:
            with self.mutex:
                if self.video_writer is not None:
                    self.video_writer.release()
                    self.video_writer = None
            if self.audio_recorder is not None:
                self.audio_recorder.stop()
                self.audio_recorder = None
            self._release_cap()
        except Exception as e:
            print(f"Fehler beim Freigeben der Ressourcen: {e}")
