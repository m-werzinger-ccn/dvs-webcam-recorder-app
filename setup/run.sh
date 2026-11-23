#!/usr/bin/env bash
# Startet die Aufnahme-App mit der venv aus setup/setup_linux_dev.sh
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${STRAIVER_VENV:-$HOME/straiver-venv}"
cd "$APP_DIR"
# -u: Ausgaben sofort schreiben (sonst ist die Reihenfolge im Log durcheinander)
exec "$VENV/bin/python" -u main.py "$@"
