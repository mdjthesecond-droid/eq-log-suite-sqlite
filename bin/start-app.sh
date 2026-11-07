#!/usr/bin/env bash
# Launches the app in its own window (eq_log_suite/desktop.py) -- same pages
# as start-web.sh, without a browser. If the app is already running, opens a
# window onto it instead of starting a second copy. Closing the last window
# stops it (unless it was already running from start-web.sh).
set -e
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
mkdir -p logs
nohup "$PROJECT_DIR/.venv/bin/python" -m eq_log_suite.desktop "$@" > logs/app.log 2>&1 &
