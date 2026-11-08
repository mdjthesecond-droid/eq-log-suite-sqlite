#!/usr/bin/env bash
# Launches the app (if not already running) plus the GTK overlay window.
# Safe to run repeatedly -- won't start a second app or overlay.
set -e
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
mkdir -p logs

# The app records the selected log and feeds the overlay socket -- started
# (as the app window) only if it isn't already running.
"$PROJECT_DIR/bin/ensure-app.sh"

if ! pgrep -f "eq_log_suite\.overlay\.window" > /dev/null; then
    nohup "$PROJECT_DIR/.venv/bin/python" -m eq_log_suite.overlay.window > logs/overlay.log 2>&1 &
fi
