# aimless-webtop — run aimless in a browser, supervised like the bitmessage project.
#
# Minimal Alpine + Xvfb + openbox + x11vnc + noVNC, every process under supervisord
# with autorestart, so closing the aimless window just restarts it.
#
# Everything runs as the non-root user `aimless` (uid 1000); the entrypoint only
# briefly runs as root to fix ownership of the bind-mounted /data volume.
#
# This image is an "environment": the per-release deliverable is the client pyz
# and the daemon binary, which the entrypoint fetches from the git dist artifacts
# into /data/bin on first start (pinned by AIMLESS_VERSION). No rebuild of this
# image is needed for a new aimless release.

FROM alpine:3.20

ENV LANG=C.UTF-8

# Runtime stack: Python + GTK3 + PyGObject, virtual X + light WM, VNC, supervisor,
# fonts, ca-certificates (for the first-start download from GitHub). Everything
# comes from Alpine packages — no pip installs, no build deps.
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
        adwaita-icon-theme \
        ca-certificates

# Modern noVNC client served through websockify (same approach as bitmessage-docker).
RUN wget -q https://github.com/novnc/noVNC/archive/refs/tags/v1.5.0.tar.gz -O /tmp/novnc.tgz && \
    mkdir -p /opt/noVNC && \
    tar xzf /tmp/novnc.tgz -C /opt/noVNC --strip-components=1 && \
    rm /tmp/novnc.tgz

# Non-root runtime user.
RUN adduser -D -u 1000 aimless

# Supervision config + helper scripts. The pyz + daemon are fetched at first
# start into /data/bin (bind mount), so nothing per-release is baked here.
COPY supervisord.conf /etc/supervisord.conf
COPY docker-entrypoint.sh run-xvfb.sh run-desktop.sh run-vnc.sh run-novnc.sh run-aimless.sh /opt/aimless/
RUN chmod +x /opt/aimless/run-*.sh /opt/aimless/docker-entrypoint.sh

EXPOSE 8080 5900

ENTRYPOINT ["/opt/aimless/docker-entrypoint.sh"]