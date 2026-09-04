#!/bin/bash
# aimless app. Waits for X, then launches the GUI in the foreground so supervisord
# restarts it whenever the window is closed (no tray in the container, so
# closing the window quits the app — close-to-tray is just a desktop behaviour).
export DISPLAY=:1
export HOME=/home/aimless
export AIMLESS_HOME=/data/state
export AIMLESS_CONFIG=/data/config

for i in $(seq 1 60); do
    [ -S /tmp/.X11-unix/X1 ] && break
    sleep 1
done
sleep 2   # let openbox take the root window first

cd /opt/aimless
exec /opt/aimless/aimless.pyz gui