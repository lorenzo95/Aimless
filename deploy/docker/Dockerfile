# aimless-webtop — run aimless in a browser, supervised like the bitmessage project.
#
# Minimal Alpine + Xvfb + openbox + x11vnc + noVNC, every process under supervisord
# with autorestart, so closing the aimless window just restarts it.
#
# Everything runs as the non-root user `aimless` (uid 1000); the entrypoint only
# briefly runs as root to fix ownership of the bind-mounted /data volume.

# The Go daemon is built CGO-free so it is a static musl binary (a glibc build
# cannot exec on Alpine). Mirrors deploy/Dockerfile.
FROM golang:1.27-alpine AS build
WORKDIR /src
COPY daemon/go.mod daemon/go.sum ./
RUN go mod download
COPY daemon/*.go ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags "-s -w" -o /aimlessd .

FROM alpine:3.20

ENV LANG=C.UTF-8

# Runtime stack: Python + GTK3 + PyGObject, virtual X + light WM, VNC, supervisor, fonts.
# Everything comes from Alpine packages — no pip installs, no build deps.
RUN apk add --no-cache \
        python3 \
        py3-gobject3 \
        gtk+3.0 \
        py3-pynacl \
        xvfb \
        x11vnc \
        openbox \
        websockify \
        su-exec \
        supervisor \
        bash \
        font-dejavu \
        xkeyboard-config \
        adwaita-icon-theme

# Modern noVNC client served through websockify (same approach as bitmessage-docker).
RUN wget -q https://github.com/novnc/noVNC/archive/refs/tags/v1.5.0.tar.gz -O /tmp/novnc.tgz && \
    mkdir -p /opt/noVNC && \
    tar xzf /tmp/novnc.tgz -C /opt/noVNC --strip-components=1 && \
    rm /tmp/novnc.tgz

# Non-root runtime user.
RUN adduser -D -u 1000 aimless

# aimless app + daemon, built above (static, musl-compatible). daemon sits next to
# the pyz so the app finds it.
COPY --from=build /aimlessd /opt/aimless/aimlessd-linux-amd64
COPY dist/aimless.pyz /opt/aimless/
RUN chmod +x /opt/aimless/aimless.pyz /opt/aimless/aimlessd-linux-amd64

# Supervision config + helper scripts.
COPY supervisord.conf /etc/supervisord.conf
COPY docker-entrypoint.sh run-xvfb.sh run-desktop.sh run-vnc.sh run-novnc.sh run-aimless.sh /opt/aimless/
RUN chmod +x /opt/aimless/run-*.sh /opt/aimless/docker-entrypoint.sh

EXPOSE 8080 5900

ENTRYPOINT ["/opt/aimless/docker-entrypoint.sh"]