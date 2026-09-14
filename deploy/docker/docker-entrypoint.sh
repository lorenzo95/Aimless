#!/bin/sh
# Fix up the bind-mounted /data volume (host dirs are initially root-owned),
# fetch the per-release aimless artifacts if needed, then drop to the non-root
# user and either run the webtop stack (default) or a headless daemon-only
# container (AIMLESS_MODE=daemon).
set -e

mkdir -p /data/logs /data/daemon /data/bin

VERSION="${AIMLESS_VERSION:-main}"
BASE="https://raw.githubusercontent.com/lorenzo95/Aimless/$VERSION/dist"

fetch() {
    target="$1"
    url="$2"
    marker="$target.version"
    if [ "$AIMLESS_FETCH" = "always" ] \
       || [ ! -f "$target" ] \
       || [ ! -f "$marker" ] \
       || [ "$(cat "$marker" 2>/dev/null)" != "$VERSION" ]; then
        echo "fetching $(basename "$target") from $VERSION …"
        wget -qO "$target.tmp" "$url"
        mv -f "$target.tmp" "$target"
        printf '%s\n' "$VERSION" > "$marker"
        chmod +x "$target"
    fi
}

# static daemon + client pyz - both fetched from the git dist artifacts
fetch /data/bin/aimless.pyz          "$BASE/aimless.pyz"
fetch /data/bin/aimlessd-linux-amd64 "$BASE/aimlessd-linux-amd64"

if [ "$AIMLESS_MODE" = "daemon" ]; then
    # Daemon-only: no X/VNC/supervisord, no ports. The client reaches this
    # daemon's API socket over SSH by bind-mounting /data to the host.
    # Run as the owner of the bind-mounted /data (i.e. your host user) so the
    # socket is readable by your SSH login - no need to pass uid/gid.
    UID_RUN="${AIMLESS_UID:-$(stat -c '%u' /data 2>/dev/null || echo 1000)}"
    GID_RUN="${AIMLESS_GID:-$(stat -c '%g' /data 2>/dev/null || echo 1000)}"
    case "$UID_RUN" in ""|0) UID_RUN=1000 ;; esac
    case "$GID_RUN" in ""|0) GID_RUN=1000 ;; esac
    chown -R "$UID_RUN:$GID_RUN" /data
    echo "starting aimlessd (daemon-only) as $UID_RUN:$GID_RUN"
    exec su-exec "$UID_RUN:$GID_RUN" /data/bin/aimlessd-linux-amd64 -datadir /data/daemon
fi

chown -R aimless:aimless /data
exec su-exec aimless:aimless /usr/bin/supervisord -n -c /etc/supervisord.conf