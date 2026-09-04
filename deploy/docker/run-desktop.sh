#!/bin/bash
# Minimal window manager so the aimless window gets borders and focus.
export DISPLAY=:1
export HOME=/home/aimless
for i in $(seq 1 60); do
    [ -S /tmp/.X11-unix/X1 ] && break
    sleep 1
done
pkill -f "openbox" 2>/dev/null || true
sleep 1
exec openbox