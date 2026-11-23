#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Aufnahmefenster für einen NIHSS-Punkt.

Die Kameras gehören der CameraSession (bleiben über die ganze Untersuchung
offen). Dieses Fenster zeigt nur Vorschau und Status an und startet/stoppt
die Aufnahme. Mit use_dvs=False wird nur die Webcam verwendet.
"""

import datetime
import os

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QApplication,
    QDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

import config
from recorder import ExternalAudioRecorder
from recorder.camera_session import DEVICE_LABELS
from ui.rating_dialog import RatingDialog
from ui.status_panel import DeviceStatusPanel
from ui.video_widget import VideoDisplayWidget
from ui.window_utils import fit_to_screen
from ui.alignment_dialog import AlignmentDialog
from utils import alignment as al


class DualCameraView(QDialog):
    """Vorschau + Aufnahmesteuerung für Webcam (und optional DVS)."""

    recording_state_changed = pyqtSignal(int, bool)
    recording_completed = pyqtSignal(int)

    def __init__(self, card_id, session, save_dir, file_name, use_dvs=True, parent=None):
        super().__init__(parent)
        self.card_id = card_id
        self.session = session
        self.save_dir = save_dir
        self.file_name = file_name
        self.use_dvs = use_dvs
        self.recording = False
        self.processing = False
        self.awaiting_processing = False
        self.audio_enabled = session.audio_enabled
        self.is_closing = False
        self._showing_dialog = False
        self._record_started_at = None
        self.webcam_recording = False

        self.external_mic_enabled = False
        self.external_mic_id = None
        self.external_audio_recorder = None

        if self.card_id < len(config.EXAMINATION_CATEGORIES):
            self.category = config.EXAMINATION_CATEGORIES[self.card_id]
        else:
            self.category = {"title": f"Aufnahme {self.card_id + 1}", "description": ""}

        self.init_ui()
        fit_to_screen(self)
        self._connect_session()
        for w in self._preview_widgets():
            w.region_selected.connect(self._on_region_selected)
        self._apply_saved_region()

        self._clock = QTimer(self)
        self._clock.timeout.connect(self._tick)
        self._clock.start(500)

    # ------------------------------------------------------------------- UI

    def init_ui(self):
        mode = "Webcam + DVS" if self.use_dvs else "Nur Webcam"
        self.setWindowTitle(f"{self.category['title']} – {mode}")
        self.setMinimumSize(800, 500)

        main_layout = QVBoxLayout()

        title_font = QFont()
        title_font.setBold(True)
        title_font.setPointSize(14)
        self.title_label = QLabel(self.category["title"])
        self.title_label.setFont(title_font)
        self.title_label.setAlignment(Qt.AlignCenter)
        main_layout.addWidget(self.title_label)

        if self.category.get("description"):
            desc = QLabel(self.category["description"])
            desc.setAlignment(Qt.AlignCenter)
            desc.setWordWrap(True)
            main_layout.addWidget(desc)

        # Gerätestatus (Ampel)
        self.status_panel = DeviceStatusPanel(self.session, compact=True)
        main_layout.addWidget(self.status_panel)

        # Warnbanner bei Verbindungsabbruch während der Aufnahme
        self.alert_label = QLabel("")
        self.alert_label.setWordWrap(True)
        self.alert_label.setStyleSheet(
            "background:#dc3545; color:white; font-weight:bold; padding:8px; border-radius:4px;"
        )
        self.alert_label.setVisible(False)
        main_layout.addWidget(self.alert_label)

        self.splitter = QSplitter(Qt.Horizontal)

        # Webcam
        webcam_container = QWidget()
        webcam_layout = QVBoxLayout(webcam_container)
        webcam_layout.addWidget(self._section_title("Webcam"))
        self.webcam_label = VideoDisplayWidget()
        self.webcam_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.webcam_label.setMinimumSize(320, 180)
        self.webcam_label.setText("Webcam wird verbunden…")
        webcam_layout.addWidget(self.webcam_label)
        self.splitter.addWidget(webcam_container)

        # DVS
        self.dvs_label = None
        if self.use_dvs:
            dvs_container = QWidget()
            dvs_layout = QVBoxLayout(dvs_container)
            dvs_layout.addWidget(self._section_title("DVS-Kamera (Prophesee)"))
            self.dvs_label = VideoDisplayWidget()
            self.dvs_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            self.dvs_label.setMinimumSize(320, 180)
            self.dvs_label.setText("DVS-Kamera wird verbunden…")
            dvs_layout.addWidget(self.dvs_label)
            self.splitter.addWidget(dvs_container)

        main_layout.addWidget(self.splitter, 1)
        main_layout.addWidget(self._build_region_group())

        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setRange(0, 0)
        main_layout.addWidget(self.progress_bar)

        info_group = QGroupBox("Aufnahme")
        info_layout = QGridLayout()
        self.status_label = QLabel("Bereit")
        info_layout.addWidget(QLabel("Status:"), 0, 0)
        info_layout.addWidget(self.status_label, 0, 1)
        self.file_label = QLabel(self.file_name)
        info_layout.addWidget(QLabel("Dateiname:"), 0, 2)
        info_layout.addWidget(self.file_label, 0, 3)
        self.duration_label = QLabel("00:00")
        self.duration_label.setStyleSheet("font-weight:bold;")
        info_layout.addWidget(QLabel("Dauer:"), 1, 0)
        info_layout.addWidget(self.duration_label, 1, 1)
        self.audio_label = QLabel("")
        info_layout.addWidget(QLabel("Audio:"), 1, 2)
        info_layout.addWidget(self.audio_label, 1, 3)
        info_layout.setColumnStretch(1, 1)
        info_layout.setColumnStretch(3, 1)
        info_group.setLayout(info_layout)
        main_layout.addWidget(info_group)
        self._update_audio_label()

        button_layout = QHBoxLayout()
        self.record_button = QPushButton("Aufnahme starten")
        self.record_button.setMinimumHeight(44)
        self.record_button.setStyleSheet("font-size:15px; font-weight:bold;")
        self.record_button.clicked.connect(self.toggle_recording)
        button_layout.addWidget(self.record_button, 3)
        self.close_button = QPushButton("Schließen")
        self.close_button.setMinimumHeight(44)
        self.close_button.clicked.connect(self.safe_close)
        button_layout.addWidget(self.close_button, 1)
        main_layout.addLayout(button_layout)

        self.setLayout(main_layout)

    def _section_title(self, text):
        label = QLabel(text)
        label.setAlignment(Qt.AlignCenter)
        font = QFont()
        font.setBold(True)
        font.setPointSize(11)
        label.setFont(font)
        return label

    def _build_region_group(self):
        self.region_group = QGroupBox("Bildausschnitt")
        outer = QVBoxLayout()
        self.region_label = QLabel("")
        self.region_label.setWordWrap(True)
        outer.addWidget(self.region_label)
        row = QHBoxLayout()
        self.select_button = QPushButton("Bereich wählen")
        self.select_button.setCheckable(True)
        self.select_button.setToolTip(
            "Rechteck in einem der Bilder aufziehen – z. B. um Gesicht oder Arm. "
            "Beide Kameras nehmen dann nur diesen Bereich auf."
        )
        self.select_button.toggled.connect(self._on_select_toggled)
        row.addWidget(self.select_button)
        self.full_button = QPushButton("Ganzes Bild")
        self.full_button.clicked.connect(lambda: self._set_region(None))
        row.addWidget(self.full_button)
        align_button = QPushButton("Kameras ausrichten…")
        align_button.setToolTip("Webcam-Ausschnitt an das Bildfeld der DVS anpassen (pro Objektiv)")
        align_button.clicked.connect(self._open_alignment)
        row.addWidget(align_button)
        row.addStretch()
        outer.addLayout(row)
        self.region_group.setLayout(outer)
        return self.region_group

    # ----------------------------------------------------- Bildausschnitt

    def _region_key(self):
        return self.category["title"]

    def _apply_saved_region(self):
        self.session.set_roi(self.session.alignment_store.roi(self._region_key()))
        self._update_region_label()

    def _update_region_label(self):
        roi = self.session.roi
        common = al.common_fov(self.session.alignment)
        text = (f"Ausrichtung <i>(gilt für alle Untersuchungen)</i>: Profil <b>{self.session.profile}</b>"
                f"<br>Bereich für diesen Punkt: ")
        if roi is None:
            text += "<b>ganzes gemeinsames Bild</b>"
        else:
            text += f"<b>{roi[2] * roi[3]:.0%}</b> des Bilds"
        win = al.dvs_region_window(self.session.alignment, roi)
        if self.use_dvs and win:
            text += f" · DVS nimmt {win[2]}×{win[3]} von 1280×720 Pixeln auf"
        if roi is None and not al.is_full(common):
            text += " (DVS sieht mehr als die Webcam und wird auf deren Bildfeld beschnitten)"
        self.region_label.setText(text)
        self.full_button.setEnabled(roi is not None)

    def _preview_widgets(self):
        return [w for w in (self.webcam_label, self.dvs_label) if w is not None]

    def _on_select_toggled(self, active):
        if active:
            # Zum Auswählen das ganze Bildfeld zeigen
            self._roi_before_select = self.session.roi
            self.session.set_roi(None)
        elif getattr(self, "_roi_before_select", None) is not None and self.session.roi is None:
            self.session.set_roi(self._roi_before_select)  # abgebrochen -> alten Bereich zurück
        for w in self._preview_widgets():
            w.set_selection_enabled(active)
        self.select_button.setText("Abbrechen" if active else "Bereich wählen")
        self._update_region_label()

    def _on_region_selected(self, rect):
        if min(rect[2], rect[3]) < al.MIN_ROI_SIZE:
            return  # versehentlicher Klick
        self._roi_before_select = None
        self._set_region(rect)
        self.select_button.setChecked(False)

    def _set_region(self, rect):
        if self.recording:
            return
        if self.select_button.isChecked():
            self._roi_before_select = None
            self.select_button.setChecked(False)
        if self.session.set_roi(rect):
            self.session.alignment_store.set_roi(self._region_key(), rect)
        self._update_region_label()

    def _open_alignment(self):
        if self.recording:
            return
        if self.select_button.isChecked():
            self.select_button.setChecked(False)
        AlignmentDialog(self.session, self).exec_()
        self._update_region_label()

    def _update_audio_label(self):
        if self.external_mic_enabled and self.external_mic_id is not None:
            text = "Externes Mikrofon"
        elif self.audio_enabled:
            text = "Standard-Mikrofon des Systems"
        else:
            text = "aus"
        self.audio_label.setText(text)

    # -------------------------------------------------------------- Session

    def _connect_session(self):
        s = self.session
        s.webcam_frame.connect(self.update_webcam_frame)
        if self.use_dvs:
            s.dvs_frame.connect(self.update_dvs_frame)
        s.processing_status.connect(self.on_processing_status)
        s.recording_interrupted.connect(self.on_recording_interrupted)

    def _disconnect_session(self):
        s = self.session
        pairs = [
            (s.webcam_frame, self.update_webcam_frame),
            (s.dvs_frame, self.update_dvs_frame),
            (s.processing_status, self.on_processing_status),
            (s.recording_interrupted, self.on_recording_interrupted),
        ]
        for signal, slot in pairs:
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass

    def update_webcam_frame(self, image):
        if not self.is_closing:
            self.webcam_label.setImage(image)

    def update_dvs_frame(self, image):
        if not self.is_closing and self.dvs_label is not None:
            self.dvs_label.setImage(image)

    # --------------------------------------------------------- Einstellungen

    def set_audio_enabled(self, enabled):
        self.audio_enabled = enabled
        self._update_audio_label()

    def set_external_mic(self, enabled, mic_id=None):
        self.external_mic_enabled = enabled
        self.external_mic_id = mic_id
        self._update_audio_label()

    def set_file_name(self, file_name):
        self.file_name = file_name
        self.file_label.setText(file_name)

    def set_save_directory(self, directory):
        self.save_dir = directory

    # -------------------------------------------------------------- Aufnahme

    def _not_ready_devices(self):
        missing = []
        if not self.session.is_ready("webcam"):
            missing.append("webcam")
        if self.use_dvs and self.session.status("dvs")[0] not in ("ok", "warn"):
            missing.append("dvs")
        return missing

    def toggle_recording(self):
        if self.recording:
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self):
        if self.select_button.isChecked():
            self.select_button.setChecked(False)
        storage = self.session.check_storage()
        if storage.state == "error":
            QMessageBox.critical(self, "Speicher", f"Aufnahme nicht möglich:\n{storage.message}")
            return

        missing = self._not_ready_devices()
        wanted = ["webcam"] + (["dvs"] if self.use_dvs else [])
        if set(missing) == set(wanted):
            QMessageBox.critical(
                self,
                "Keine Kamera bereit",
                "Keine Kamera liefert gerade ein Bild.\n\n"
                "Bitte oben bei der roten Kamera auf „Neu verbinden“ klicken.",
            )
            return
        if missing:
            names = ", ".join(DEVICE_LABELS[m] for m in missing)
            reply = QMessageBox.warning(
                self,
                "Kamera nicht bereit",
                f"{names} ist nicht bereit:\n"
                + "\n".join(f"• {DEVICE_LABELS[m]}: {self.session.status(m)[1]}" for m in missing)
                + "\n\nEmpfohlen: Abbrechen und „Neu verbinden“ klicken.\n"
                "Trotzdem nur mit den übrigen Kameras aufnehmen?",
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if reply != QMessageBox.Yes:
                return

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        if self.file_name == f"recording_{self.card_id}" or not self.file_name:
            category_name = os.path.basename(self.save_dir)
            self.file_name = "".join(
                c for c in f"{category_name}_{timestamp}" if c.isalnum() or c in "_-"
            )
            self.file_label.setText(self.file_name)
        os.makedirs(self.save_dir, exist_ok=True)

        use_ext_mic = self.external_mic_enabled and self.external_mic_id is not None
        # ALSA kann meist nicht zwei Audio-Streams parallel -> Standard-Mikro aus
        webcam_audio = False if use_ext_mic else self.audio_enabled

        results = self.session.start_recording(
            self.save_dir, self.file_name, use_dvs=self.use_dvs, webcam_audio=webcam_audio
        )
        failed = {d: msg for d, (ok, msg) in results.items() if not ok}
        unexpected = {d: m for d, m in failed.items() if d not in missing}
        if not self.session.recording:
            QMessageBox.critical(
                self,
                "Aufnahme fehlgeschlagen",
                "Die Aufnahme konnte nicht gestartet werden:\n"
                + "\n".join(f"• {DEVICE_LABELS[d]}: {m}" for d, m in failed.items()),
            )
            return
        if unexpected:
            QMessageBox.warning(
                self,
                "Teilweise gestartet",
                "Achtung, nicht alle Kameras nehmen auf:\n"
                + "\n".join(f"• {DEVICE_LABELS[d]}: {m}" for d, m in unexpected.items()),
            )

        self.webcam_recording = bool(results.get("webcam", (False,))[0])

        if use_ext_mic:
            path = os.path.join(self.save_dir, f"{self.file_name}_audio.wav")
            self.external_audio_recorder = ExternalAudioRecorder(path, self.external_mic_id)
            webcam = self.session.webcam
            if webcam is not None and webcam.start_time:
                self.external_audio_recorder.set_start_time(webcam.start_time)
            self.external_audio_recorder.start()

        self.recording = True
        self._record_started_at = datetime.datetime.now()
        self.alert_label.setVisible(False)
        active = [DEVICE_LABELS[d] for d, (ok, _) in results.items() if ok]
        self.status_label.setText("● Aufnahme läuft: " + " + ".join(active))
        self.status_label.setStyleSheet("color:#dc3545; font-weight:bold;")
        self.record_button.setText("■ Aufnahme stoppen")
        self.record_button.setStyleSheet(
            "font-size:15px; font-weight:bold; background:#dc3545; color:white;"
        )
        self.region_group.setEnabled(False)
        self.recording_state_changed.emit(self.card_id, True)

    def _stop_recording(self):
        self.record_button.setEnabled(False)
        self.status_label.setText("Aufnahme wird gestoppt…")
        self.status_label.setStyleSheet("")
        QApplication.processEvents()

        if self.external_audio_recorder is not None:
            self.external_audio_recorder.stop()
            self.external_audio_recorder = None

        self.awaiting_processing = self.webcam_recording
        results = self.session.stop_recording()
        self.recording = False
        self._record_started_at = None
        self.record_button.setText("Aufnahme starten")
        self.record_button.setStyleSheet("font-size:15px; font-weight:bold;")
        self.record_button.setEnabled(True)
        self.region_group.setEnabled(True)
        self.recording_state_changed.emit(self.card_id, False)

        dvs_ok, dvs_msg = results.get("dvs", (True, ""))
        if not dvs_ok:
            QMessageBox.warning(self, "DVS", f"DVS-Aufnahme konnte nicht sauber beendet werden:\n{dvs_msg}")

        if not self.awaiting_processing:
            # Ohne Webcam-Video gibt es keine Nachverarbeitung -> direkt bewerten
            self.status_label.setText("Aufnahme gespeichert")
            QTimer.singleShot(100, self.show_rating_dialog)

    def on_recording_interrupted(self, message):
        if not self.recording:
            return
        self.alert_label.setText(f"⚠ {message}")
        self.alert_label.setVisible(True)
        QApplication.beep()

    def on_processing_status(self, status_msg):
        if not self.awaiting_processing:
            return
        self.status_label.setText(status_msg)
        if "Verarbeite" in status_msg or "Konvertiere" in status_msg:
            self.processing = True
            self.progress_bar.setVisible(True)
            self.record_button.setEnabled(False)
            self.region_group.setEnabled(False)
        elif "erfolgreich" in status_msg or "gespeichert" in status_msg:
            self.processing = False
            self.awaiting_processing = False
            self.progress_bar.setVisible(False)
            self.record_button.setEnabled(True)
            self.region_group.setEnabled(True)
            QTimer.singleShot(100, self.show_rating_dialog)

    def _tick(self):
        if self._record_started_at is not None:
            secs = int((datetime.datetime.now() - self._record_started_at).total_seconds())
            self.duration_label.setText(f"{secs // 60:02d}:{secs % 60:02d}")

    def show_rating_dialog(self):
        if self._showing_dialog or self.is_closing:
            return
        self._showing_dialog = True
        try:
            dialog = RatingDialog(
                category=self.category,
                save_dir=self.save_dir,
                file_name=self.file_name,
                parent=self,
            )
            dialog.rating_saved.connect(lambda: self.recording_completed.emit(self.card_id))
            if dialog.exec_() == QDialog.Accepted:
                self.status_label.setText("Bewertung gespeichert – Punkt abgeschlossen")
            else:
                self.status_label.setText("Bewertung abgebrochen")
        finally:
            self._showing_dialog = False

    # ---------------------------------------------------------- Schließen

    def closeEvent(self, event):
        if not self.is_closing:
            event.ignore()
            self.safe_close()
        else:
            super().closeEvent(event)

    def safe_close(self):
        if self.processing:
            reply = QMessageBox.question(
                self,
                "Video wird verarbeitet",
                "Das Video wird gerade gespeichert. Trotzdem schließen?\n"
                "Die Speicherung läuft im Hintergrund weiter, die Bewertung "
                "muss dann über „Erneut aufnehmen“ nachgeholt werden.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply == QMessageBox.No:
                return
        if self.recording:
            self._stop_recording()
        if self.select_button.isChecked():
            self.select_button.setChecked(False)
        self._clock.stop()
        self._disconnect_session()
        self.is_closing = True
        self.close()
