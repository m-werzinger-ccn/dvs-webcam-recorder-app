#!/usr/bin/env bash
# Startet die Aufnahme-App mit der venv aus setup/setup_linux_dev.sh.
# Jede Sitzung wird automatisch in logs/app_DATUM.log mitgeschrieben
# (auch beim Start über das Desktop-Icon) – für die Fehlersuche.
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${STRAIVER_VENV:-$HOME/straiver-venv}"
cd "$APP_DIR"
mkdir -p "$APP_DIR/logs"
LOG="$APP_DIR/logs/app_$(date +%Y%m%d_%H%M%S).log"
# nur die letzten 30 Logs behalten
ls -1t "$APP_DIR"/logs/app_*.log 2>/dev/null | tail -n +30 | xargs -r rm -f
exec > >(tee -a "$LOG") 2>&1
# -u: Ausgaben sofort schreiben (sonst ist die Reihenfolge im Log durcheinander)
exec "$VENV/bin/python" -u main.py "$@"
