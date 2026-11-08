#!/usr/bin/env bash
# Launches the app (if not already running) plus the native Vulkan
# overlay's companion app, for positioning its in-game boxes (see README:
# "The in-game overlay").
#
# Requires the layer to be installed first (overlay-native/install.sh) and
# EQLOG_OVERLAY_ENABLE=1 added to the game's Steam launch options for the
# in-game boxes themselves to render -- this script only starts the app
# and the position-control companion window, not the layer (which loads
# automatically into the game process via the Vulkan loader once installed
# and enabled).
#
# Safe to run repeatedly -- won't start a second app or companion.
set -e
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
mkdir -p logs

# The app records the selected log and feeds the overlay socket -- started
# (as the app window) only if it isn't already running.
"$PROJECT_DIR/bin/ensure-app.sh"

if ! pgrep -f "eq_overlay_companion" > /dev/null; then
    nohup distrobox enter eq-overlay-dev -- "$PROJECT_DIR/overlay-native/build/companion/eq_overlay_companion" \
        > logs/overlay_native_companion.log 2>&1 &
fi
