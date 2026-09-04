#!/bin/bash
# noVNC websocket proxy. Waits for the VNC server to be reachable first.
cd /opt/noVNC
for i in $(seq 1 60); do
    (exec 3<>/dev/tcp/127.0.0.1/5900) 2>/dev/null && break
    sleep 1
done
exec websockify --web /opt/noVNC 8080 localhost:5900