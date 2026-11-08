#!/usr/bin/env bash
# Makes sure the app is running (it records the log and feeds the overlays)
# without opening anything if it already is: starts the app window
# (start-app.sh) only when neither it nor the browser version (start-web.sh)
# is running. Used by the overlay launchers.
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if ! pgrep -f "uvicorn eq_log_suite.web.app:app" > /dev/null && ! pgrep -f "eq_log_suite\.desktop" > /dev/null; then
    "$PROJECT_DIR/bin/start-app.sh"
fi
