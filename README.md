# STRAIVER – Untersuchungs-Recorder

Nimmt NIHSS-Untersuchungen gleichzeitig mit einer Webcam und einer Prophesee-DVS-Kamera
(plus optional einem externen Mikrofon) auf. Nach jedem NIHSS-Punkt gibt der Arzt seine
Bewertung und Notizen ab; alles wird pro Patient und Punkt in einem Ordner gespeichert.

## 1. Systemvoraussetzungen

- **Aufnahme-Laptop:** Ubuntu 22.04 oder 24.04 (amd64), Python 3.10+
- **DVS-Kamera:** Prophesee EVK (IMX636) an einem **USB-3-Port** (über den Hub prüfen!)
- **Webcam:** USB-Webcam mit MJPG-Unterstützung (z. B. UGREEN)
- **Metavision SDK / OpenEB** von Prophesee – gibt es nur für Linux und Windows, nicht für macOS

## 2. Installation (Linux-Laptop)

```bash
# Systempakete
sudo apt install python3-venv python3-pyqt5 portaudio19-dev ffmpeg v4l-utils
# Metavision SDK nach Anleitung: https://docs.prophesee.ai/stable/installation/index.html

# App
git clone <repo-url> straiver-recorder && cd straiver-recorder
python3 -m venv --system-site-packages .venv     # damit Metavision gefunden wird
.venv/bin/pip install -r requirements.txt

# Einmalig: USB-Stabilität (Autosuspend aus, Software-Reset erlauben, Gruppenrechte)
bash setup/install_usb_rules.sh                    # danach ab- und wieder anmelden
```

Start: `.venv/bin/python main.py`

## 3. Bedienung – Gerätestatus („Ampel“)

Im Hauptfenster und in jedem Aufnahmefenster zeigt eine Ampel den Zustand von
Webcam, DVS-Kamera, externem Mikrofon und Speicher:

- **grün** – bereit
- **orange** – verbindet gerade / Hinweis (z. B. DVS nur mit USB 2.0, USB-Stick mit FAT32)
- **rot** – Problem. Auf **„Neu verbinden“** klicken: Die App trennt die Kamera per Software
  und verbindet sie neu (wirkt wie Aus- und Einstecken). Kabel umstecken ist nur noch nötig,
  wenn das nicht hilft.

Die Kameras bleiben während der ganzen Untersuchung verbunden; Aufnahmefenster öffnen und
schließen sie nicht mehr. Fällt eine Kamera aus, verbindet die App automatisch neu.
Ist beim Start einer Aufnahme eine Kamera nicht bereit, fragt die App nach, statt
stillschweigend nur mit einer Kamera aufzunehmen. Bricht eine Kamera während der
Aufnahme ab, erscheint ein roter Warnhinweis.

## 4. Fehlerbehebung

Wenn eine Kamera fehlt – **bevor** etwas umgesteckt wird:

```bash
bash tools/diagnose_hardware.sh > ~/diagnose_$(date +%Y%m%d_%H%M).txt 2>&1
```

Die Datei enthält USB-Baum und -Geschwindigkeit, Autosuspend-Status, Videogeräte,
Metavision-Status, Dateisystem des USB-Sticks und das Kernel-Log.

Häufige Ursachen:
- DVS hängt am Hub nur mit USB 2.0 (`lsusb -t` zeigt 480M statt 5000M) → anderer Port/Kabel
- USB-Stick mit FAT32 → Dateien max. 4 GB; besser exFAT
- Benutzer nicht in den Gruppen `video`/`plugdev` → `setup/install_usb_rules.sh`

## 5. Entwicklung auf dem Mac

Die Geräteerkennung ist plattformabhängig (Linux: V4L2, macOS: AVFoundation). Auf dem Mac
läuft die App mit Webcam, aber ohne DVS (Metavision gibt es dort nicht; die DVS-Ampel steht
dann auf grau). Für Aufnahmen auf dem Mac den Modus „Nur Webcam“ wählen.
