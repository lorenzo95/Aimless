#!/bin/bash
# Virtual X display (supervised on its own so it restarts if it dies).
export HOME=/home/aimless
pkill -f "Xvfb :1" 2>/dev/null || true
rm -f /tmp/.X1-lock /tmp/.X11-unix/X1
sleep 1
exec Xvfb :1 -screen 0 1366x768x24 -nolisten tcp