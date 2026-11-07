#!/usr/bin/env bash
# Launches the web UI (if not already running) and opens it in your browser.
set -e
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
mkdir -p logs

# Either version of the app (browser here, or the window -- start-app.sh)
# already serves it: don't start a second.
if ! pgrep -f "uvicorn eq_log_suite.web.app:app" > /dev/null && ! pgrep -f "eq_log_suite\.desktop" > /dev/null; then
    nohup "$PROJECT_DIR/.venv/bin/uvicorn" eq_log_suite.web.app:app --port 8000 > logs/web.log 2>&1 &
    sleep 1
fi

xdg-open http://localhost:8000/ 2>/dev/null || true
