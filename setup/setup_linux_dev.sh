#!/usr/bin/env bash
# Richtet ein Ubuntu-System (22.04/24.04/26.04, amd64 ODER arm64 – z. B. Parallels-VM
# auf dem Mac) komplett für die Aufnahme-App ein:
#   1. Systempakete
#   2. OpenEB (offener Teil des Metavision SDK) aus dem Quellcode bauen
#   3. Prophesee-udev-Regeln + USB-Regeln der App
#   4. Python-Umgebung ~/straiver-venv
#
# Aufruf (aus dem App-Ordner):  bash setup/setup_linux_dev.sh
# Dauer: ca. 30–60 min (der OpenEB-Build dauert am längsten).
# Danach starten mit:            bash setup/run.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SRC_DIR="${OPENEB_SRC_DIR:-$HOME/openeb}"
VENV="${STRAIVER_VENV:-$HOME/straiver-venv}"
JOBS="$(nproc)"

step() { echo; echo "=== $* ==="; }

step "1/5 Systempakete"
PKGS=(
  build-essential cmake git wget unzip curl pkg-config
  libopencv-dev libboost-all-dev libusb-1.0-0-dev libprotobuf-dev protobuf-compiler
  libhdf5-dev hdf5-tools libglew-dev libglfw3-dev libcanberra-gtk-module libcanberra-gtk3-module
  python3-dev python3-venv python3-pip pybind11-dev
  python3-pyqt5 python3-pyaudio python3-numpy python3-opencv python3-h5py
  portaudio19-dev ffmpeg v4l-utils usbutils
)
sudo apt-get update
# Je nach Ubuntu-Version fehlen einzelne Pakete -> nur installieren, was es gibt
AVAILABLE=(); MISSING=()
for p in "${PKGS[@]}"; do
  # Ausgabe erst speichern: "| grep -q" + pipefail würde sonst immer fehlschlagen
  POLICY="$(LC_ALL=C apt-cache policy "$p" 2>/dev/null || true)"
  if grep -q 'Candidate: [^(]' <<<"$POLICY"; then
    AVAILABLE+=("$p")
  else
    MISSING+=("$p")
  fi
done
[ ${#MISSING[@]} -gt 0 ] && echo "Nicht verfügbar (übersprungen): ${MISSING[*]}"
sudo apt-get -y install "${AVAILABLE[@]}"

step "2/5 OpenEB-Quellcode"
if [ ! -d "$SRC_DIR/.git" ]; then
  # neueste Release-Version von OpenEB ermitteln (überschreibbar mit OPENEB_VERSION=x.y.z)
  VERSION="${OPENEB_VERSION:-$(git ls-remote --tags --refs https://github.com/prophesee-ai/openeb.git \
    | sed 's#.*/##' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -1)}"
  echo "OpenEB-Version: $VERSION"
  git clone --depth 1 --branch "$VERSION" https://github.com/prophesee-ai/openeb.git "$SRC_DIR"
else
  echo "Vorhanden: $SRC_DIR"
fi

step "3/5 Python-Umgebung $VENV"
# --system-site-packages: PyQt5/PyAudio/OpenCV kommen aus apt (für arm64 gibt es
# keine PyQt5-Pakete auf pip), OpenEB wird systemweit installiert.
[ -d "$VENV" ] || python3 -m venv --system-site-packages "$VENV"
"$VENV/bin/pip" install --upgrade pip
# OpenEBs eigene requirements-Datei NICHT installieren: sie pinnt uralte
# Versionen (z. B. opencv-python 4.5, numba < 0.60), die mit neuem Python
# nicht bauen. Die Python-Module von OpenEB brauchen nur numpy und h5py (apt).
"$VENV/bin/pip" install reportlab

step "4/5 OpenEB bauen und installieren (dauert)"
mkdir -p "$SRC_DIR/build"
cd "$SRC_DIR/build"
# "-include cstdint": OpenEB 5.x vergisst in einigen Headern <cstdint>;
# ab GCC 15 (Ubuntu 26.04) ist das ein Fehler.
cmake .. -Wno-dev -DBUILD_TESTING=OFF -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_CXX_FLAGS="-include cstdint" \
  -DPython3_EXECUTABLE="$(command -v python3)"
cmake --build . --config Release -- -j"$JOBS"
sudo cmake --build . --target install
sudo ldconfig

# Python-Module von OpenEB der venv bekannt machen
SITE="$("$VENV/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
MV_PKG="$(find /usr/local /usr/lib -type d -name metavision_core -path '*-packages*' 2>/dev/null | head -1 || true)"
if [ -n "$MV_PKG" ]; then
  MV_DIR="$(dirname "$MV_PKG")"
  echo "$MV_DIR" > "$SITE/openeb.pth"
  echo "OpenEB-Python-Module: $MV_DIR"
else
  echo "WARNUNG: metavision_core nicht gefunden – Python-Anbindung prüfen"
fi

step "5/5 udev-Regeln (Prophesee + App) und Gruppen"
sudo cp "$SRC_DIR"/hal_psee_plugins/resources/rules/*.rules /etc/udev/rules.d/ 2>/dev/null || \
  echo "Hinweis: Prophesee-udev-Regeln nicht gefunden"
bash "$APP_DIR/setup/install_usb_rules.sh"

echo
echo "Test der Python-Anbindung:"
"$VENV/bin/python" -c "import metavision_core, metavision_sdk_core, PyQt5, cv2; print('OK – Metavision, PyQt5 und OpenCV gefunden')" \
  || echo "FEHLER: Import fehlgeschlagen (Ausgabe oben an Claude schicken)"
echo
echo "Fertig. Einmal ab- und wieder anmelden (Gruppenrechte), Kameras einstecken, dann:"
echo "  metavision_platform_info      # zeigt die DVS-Kamera?"
echo "  bash $APP_DIR/setup/run.sh"
