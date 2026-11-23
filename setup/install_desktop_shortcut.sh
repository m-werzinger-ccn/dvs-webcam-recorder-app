#!/usr/bin/env bash
# Legt ein Icon für die App auf den Schreibtisch und ins App-Menü (Ubuntu/GNOME).
# Aufruf:  bash setup/install_desktop_shortcut.sh
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
NAME="straiver-recorder.desktop"
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
MENU_DIR="$HOME/.local/share/applications"
mkdir -p "$DESKTOP_DIR" "$MENU_DIR"

ENTRY="[Desktop Entry]
Type=Application
Version=1.0
Name=STRAIVER Recorder
Comment=NIHSS-Untersuchungen mit Webcam und DVS aufnehmen
Exec=bash \"$APP_DIR/setup/run.sh\"
Path=$APP_DIR
Icon=$APP_DIR/assets/desktop-icon.png
Terminal=false
Categories=AudioVideo;Video;
StartupNotify=true
"

echo "$ENTRY" > "$MENU_DIR/$NAME"
echo "$ENTRY" > "$DESKTOP_DIR/$NAME"
chmod +x "$MENU_DIR/$NAME" "$DESKTOP_DIR/$NAME"
# GNOME: Icon auf dem Schreibtisch als vertrauenswürdig markieren (sonst nur "Start erlauben")
gio set "$DESKTOP_DIR/$NAME" metadata::trusted true 2>/dev/null || true

# Alte Verknüpfung, die frühere App-Versionen selbst angelegt haben, entfernen
OLD="$DESKTOP_DIR/WebcamRecorder.desktop"
if [ -f "$OLD" ] && grep -q "main.py" "$OLD"; then
  rm -f "$OLD"
  echo "Alte Verknüpfung WebcamRecorder.desktop entfernt."
fi

update-desktop-database "$MENU_DIR" 2>/dev/null || true
echo "Fertig: Icon „STRAIVER Recorder“ auf dem Schreibtisch ($DESKTOP_DIR) und im App-Menü."
echo "Falls es auf dem Schreibtisch ein Warnsymbol zeigt: Rechtsklick → „Start erlauben“."
