#!/bin/bash
# VNC server. Password comes from the VNC_PASS env (set in docker-compose).
export DISPLAY=:1
export HOME=/home/aimless
for i in $(seq 1 60); do
    [ -S /tmp/.X11-unix/X1 ] && break
    sleep 1
done
sleep 2

if [ -n "${VNC_PASS:-}" ]; then
    mkdir -p "$HOME/.vnc"
    x11vnc -storepasswd "$VNC_PASS" "$HOME/.vnc/passwd" >/dev/null 2>&1
    exec x11vnc -forever -shared -rfbauth "$HOME/.vnc/passwd" -rfbport 5900 -display :1 -noxdamage -o /tmp/x11vnc.log
else
    exec x11vnc -forever -shared -rfbport 5900 -display :1 -noxdamage -o /tmp/x11vnc.log
fi