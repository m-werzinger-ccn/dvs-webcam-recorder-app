#!/usr/bin/env bash
# Einmalig auf dem Linux-Laptop ausführen:  bash setup/install_usb_rules.sh
#  - schaltet USB-Autosuspend ab (Kameras werden nicht mehr "schlafen gelegt")
#  - erlaubt der App den Software-USB-Reset („Neu verbinden“)
#  - fügt den Benutzer den Gruppen video und plugdev hinzu
set -euo pipefail
cd "$(dirname "$0")"
sudo cp 90-straiver-usb-power.rules 91-straiver-usb-reset.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=usb --action=add
sudo usermod -aG video,plugdev "$USER"
echo "Fertig. Bitte einmal ab- und wieder anmelden (Gruppenrechte) bzw. neu starten."
