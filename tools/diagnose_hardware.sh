#!/usr/bin/env bash
# Hardware-Diagnose für den STRAIVER-Aufnahme-Laptop (Ubuntu).
# Ausführen, wenn eine Kamera nicht erkannt wird – VOR dem Aus-/Einstecken:
#   bash tools/diagnose_hardware.sh > ~/diagnose_$(date +%Y%m%d_%H%M).txt 2>&1
set -u
section() { echo; echo "===== $1 ====="; }

section "System"
date; uname -a; lsb_release -ds 2>/dev/null

section "USB-Baum (480M = USB 2.0, 5000M/10000M = USB 3.x – die DVS braucht USB 3)"
lsusb -t

section "USB-Geräte"
lsusb

section "USB-Autosuspend (global: -1 = aus | pro Gerät: power/control=on ist gut, auto = darf schlafen)"
cat /sys/module/usbcore/parameters/autosuspend
for d in /sys/bus/usb/devices/*; do
  [ -f "$d/idVendor" ] || continue
  printf '%-10s %s:%s  %-32s speed=%-6s power/control=%s\n' \
    "$(basename "$d")" "$(cat "$d/idVendor")" "$(cat "$d/idProduct")" \
    "$(cat "$d/product" 2>/dev/null)" "$(cat "$d/speed" 2>/dev/null)" \
    "$(cat "$d/power/control" 2>/dev/null)"
done

section "Video-Geräte (V4L2) – stabile Namen unter /dev/v4l/by-id"
v4l2-ctl --list-devices 2>&1
ls -l /dev/v4l/by-id/ 2>&1
for dev in /dev/video*; do
  echo "--- $dev"
  v4l2-ctl -d "$dev" --list-formats-ext 2>&1 | grep -E "Type|\[|Size|Interval" | head -30
done

section "Prophesee / Metavision"
if command -v metavision_platform_info >/dev/null; then
  timeout 20 metavision_platform_info 2>&1 | head -60
else
  echo "metavision_platform_info nicht gefunden"
fi
python3 -c "import metavision_core, metavision_hal; print('Metavision-Python OK:', metavision_core.__file__)" 2>&1

section "Audio-Eingänge"
arecord -l 2>&1

section "Datenträger (FSTYPE vfat = FAT32 -> max. 4 GB pro Datei!)"
lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINT,MODEL,TRAN
df -h 2>/dev/null | grep -E "/media|/mnt|/run/media|Filesystem|Dateisystem"

section "Benutzergruppen (video und plugdev sollten dabei sein)"
id

section "Kernel-Log: USB/Video (letzte 80 Treffer)"
(journalctl -k -n 600 --no-pager 2>/dev/null || dmesg 2>/dev/null) \
  | grep -iE "usb|uvc|video|xhci|reset|disconnect" | tail -80
