#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Prüft den Speicherort (USB-Stick): beschreibbar, freier Platz, Dateisystem."""

import os
import shutil
import tempfile
from dataclasses import dataclass

WARN_FREE_GB = 10.0  # darunter gelb
MIN_FREE_GB = 2.0  # darunter rot


@dataclass
class StorageStatus:
    state: str  # "ok" | "warn" | "error"
    message: str
    free_gb: float = 0.0
    fstype: str = ""


def _filesystem_type(path):
    """Dateisystem-Typ über /proc/mounts (Linux); sonst leer."""
    try:
        path = os.path.realpath(path)
        best, fstype = "", ""
        with open("/proc/mounts", "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mount = parts[1].replace("\\040", " ")
                if (path == mount or path.startswith(mount.rstrip("/") + "/")) and len(
                    mount
                ) > len(best):
                    best, fstype = mount, parts[2]
        return fstype
    except OSError:
        return ""


def check_storage(path):
    if not path or not os.path.isdir(path):
        return StorageStatus("error", "Speicherort nicht gefunden – USB-Stick eingesteckt?")

    try:
        with tempfile.NamedTemporaryFile(dir=path, prefix=".schreibtest_", delete=True):
            pass
    except OSError as e:
        return StorageStatus("error", f"Speicherort nicht beschreibbar: {e}")

    free_gb = shutil.disk_usage(path).free / 1e9
    fstype = _filesystem_type(path)
    hints = []
    state = "ok"

    if free_gb < MIN_FREE_GB:
        state = "error"
        hints.append("fast voll")
    elif free_gb < WARN_FREE_GB:
        state = "warn"
        hints.append("wenig Platz")

    if fstype in ("vfat", "msdos", "fat"):
        if state == "ok":
            state = "warn"
        hints.append("FAT32: max. 4 GB pro Datei – bitte exFAT verwenden")

    msg = f"{free_gb:.1f} GB frei"
    if fstype:
        msg += f" ({fstype})"
    if hints:
        msg += " – " + ", ".join(hints)
    return StorageStatus(state, msg, free_gb, fstype)
