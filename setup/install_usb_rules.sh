#!/usr/bin/env bash
# Einmalig auf dem Linux-Laptop ausführen:  bash setup/install_usb_rules.sh
set -euo pipefail
cd "$(dirname "$0")"
sudo cp 90-straiver-usb-power.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=usb
sudo usermod -aG video,plugdev "$USER"
echo "Fertig. Bitte einmal ab- und wieder anmelden (Gruppenrechte) bzw. neu starten."
