#!/bin/bash
# aimless app. Waits for X, then launches the GUI in the foreground so supervisord
# restarts it whenever the window is closed (no tray in the container, so
# closing the window quits the app — close-to-tray is just a desktop behaviour).
#
# AIMLESS_MODE=daemon_only: run the static daemon alone (no X, no GUI) as a
# 24/7 remote mailbox. The daemon writes its api.sock to <datadir>/api.sock
# (= /data/state/api.sock on the bind mount), which a laptop GUI can reach over
# an SSH tunnel.
export HOME=/home/aimless
export AIMLESS_HOME=/data/state
export AIMLESS_CONFIG=/data/config

if [ "${AIMLESS_MODE:-webtop}" = "daemon_only" ]; then
    exec /data/bin/aimlessd-linux-amd64 -datadir /data/state
fi

export DISPLAY=:1

for i in $(seq 1 60); do
    [ -S /tmp/.X11-unix/X1 ] && break
    sleep 1
done
sleep 2   # let openbox take the root window first

cd /data/bin
exec /data/bin/aimless.pyz gui